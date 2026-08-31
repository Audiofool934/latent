"""Command-line entry point for the Phase 0 read-only spike."""

from __future__ import annotations

import argparse
import json
import sys
from functools import partial
from pathlib import Path

from .errors import ConfigurationError, LatentError
from .jobs import ArchiveTreeScanner, DirectoryImporter, PreviewWorker
from .preview import MIB, ExifToolProbe, PreviewPipeline
from .provider import (
    DEFAULT_CLOUDDRIVE_ENDPOINT,
    DEFAULT_CLOUDDRIVE_PLIST,
    CloudDriveCatalog,
    CloudDriveRangeSource,
)
from .storage import GIB, CacheManager, StateStore
from .web_server import serve_library

DEFAULT_STATE_DIR = Path.home() / "Library/Application Support/Latent/phase0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="latent",
        description="Read-only validation tools for the Latent photographic archive.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    spike = subparsers.add_parser(
        "spike",
        help="fetch and index one remote ARW through bounded byte ranges",
    )
    spike.add_argument("--path", required=True, help="CloudDrive API path to one ARW")
    spike.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    spike.add_argument("--endpoint", default=DEFAULT_CLOUDDRIVE_ENDPOINT)
    spike.add_argument("--plist", type=Path, default=DEFAULT_CLOUDDRIVE_PLIST)
    spike.add_argument("--initial-prefix-kib", type=int, default=512)
    spike.add_argument("--maximum-prefix-mib", type=int, default=8)
    spike.add_argument("--maximum-preview-mib", type=int, default=16)
    spike.add_argument("--contact-budget-gib", type=float, default=3.0)
    spike.add_argument("--preview-budget-gib", type=float, default=4.0)
    spike.add_argument("--temporary-budget-gib", type=float, default=1.0)
    spike.add_argument("--timeout-seconds", type=float, default=30.0)
    spike.add_argument("--force", action="store_true", help="ignore an existing cache hit")
    spike.add_argument("--json", action="store_true", help="emit machine-readable evidence")
    spike.set_defaults(handler=_run_spike)

    scan = subparsers.add_parser(
        "scan",
        help="catalog ARWs in one remote directory and enqueue preview jobs",
    )
    scan.add_argument("--path", required=True, help="CloudDrive API path to one directory")
    scan.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    scan.add_argument("--endpoint", default=DEFAULT_CLOUDDRIVE_ENDPOINT)
    scan.add_argument("--plist", type=Path, default=DEFAULT_CLOUDDRIVE_PLIST)
    scan.add_argument("--limit", type=_positive_int)
    scan.add_argument("--force-refresh", action="store_true")
    scan.add_argument("--retry-failed", action="store_true")
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(handler=_run_scan)

    tree_scan = subparsers.add_parser(
        "scan-tree",
        help="incrementally scan an archive directory tree and enqueue ARW previews",
    )
    tree_source = tree_scan.add_mutually_exclusive_group(required=True)
    tree_source.add_argument("--path", help="CloudDrive API path to an archive root")
    tree_source.add_argument("--scan-id", type=_positive_int, help="resume an existing scan")
    tree_scan.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    tree_scan.add_argument("--endpoint", default=DEFAULT_CLOUDDRIVE_ENDPOINT)
    tree_scan.add_argument("--plist", type=Path, default=DEFAULT_CLOUDDRIVE_PLIST)
    tree_scan.add_argument("--max-directories", type=_positive_int, default=25)
    tree_scan.add_argument("--force-refresh", action="store_true")
    tree_scan.add_argument(
        "--include-hidden",
        action="store_true",
        help="include directories whose names start with a dot or underscore",
    )
    tree_scan.add_argument("--retry-failed", action="store_true")
    tree_scan.add_argument("--json", action="store_true")
    tree_scan.set_defaults(handler=_run_tree_scan)

    cancel_scan = subparsers.add_parser(
        "scan-tree-cancel",
        help="stop a recursive scan at its next directory boundary",
    )
    cancel_scan.add_argument("--scan-id", type=_positive_int, required=True)
    cancel_scan.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    cancel_scan.add_argument("--json", action="store_true")
    cancel_scan.set_defaults(handler=_run_tree_scan_cancel)

    worker = subparsers.add_parser(
        "work",
        help="process pending preview jobs from the durable local queue",
    )
    worker.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    worker.add_argument("--endpoint", default=DEFAULT_CLOUDDRIVE_ENDPOINT)
    worker.add_argument("--plist", type=Path, default=DEFAULT_CLOUDDRIVE_PLIST)
    worker.add_argument("--max-jobs", type=_positive_int, default=25)
    worker.add_argument("--initial-prefix-kib", type=_positive_int, default=512)
    worker.add_argument("--maximum-prefix-mib", type=_positive_int, default=8)
    worker.add_argument("--maximum-preview-mib", type=_positive_int, default=16)
    worker.add_argument("--contact-budget-gib", type=_non_negative_float, default=3.0)
    worker.add_argument("--preview-budget-gib", type=_non_negative_float, default=4.0)
    worker.add_argument("--temporary-budget-gib", type=_non_negative_float, default=1.0)
    worker.add_argument("--timeout-seconds", type=float, default=30.0)
    worker.add_argument("--max-consecutive-failures", type=_positive_int, default=5)
    worker.add_argument("--retry-failed", action="store_true")
    worker.add_argument("--json", action="store_true")
    worker.set_defaults(handler=_run_worker)

    status = subparsers.add_parser("status", help="inspect the local index and cache")
    status.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=_run_status)

    serve = subparsers.add_parser(
        "serve",
        help="serve the cached local index as a read-only contact sheet",
    )
    serve.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=_port, default=8765)
    serve.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow binding to a non-loopback interface",
    )
    serve.set_defaults(handler=_run_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except LatentError as error:
        print(f"latent: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("latent: interrupted", file=sys.stderr)
        return 130


def _run_spike(args: argparse.Namespace) -> int:
    budgets = {
        "contact": _gib(args.contact_budget_gib),
        "preview": _gib(args.preview_budget_gib),
        "temporary": _gib(args.temporary_budget_gib),
    }
    probe = ExifToolProbe()
    with StateStore(args.state_dir) as store:
        cache = CacheManager(store, budgets)
        pipeline = PreviewPipeline(
            store,
            cache,
            probe,
            initial_prefix_bytes=args.initial_prefix_kib * 1024,
            maximum_prefix_bytes=args.maximum_prefix_mib * MIB,
            maximum_preview_bytes=args.maximum_preview_mib * MIB,
        )
        with CloudDriveRangeSource(
            args.path,
            endpoint=args.endpoint,
            plist_path=args.plist,
            timeout_seconds=args.timeout_seconds,
        ) as source:
            result = pipeline.run(source, force=args.force)
    payload = result.as_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_spike(payload)
    return 0


def _run_status(args: argparse.Namespace) -> int:
    with StateStore(args.state_dir) as store:
        payload = store.status()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"State: {payload['state_dir']}")
        print(f"SQLite integrity: {payload['database_integrity']}")
        print(f"Assets: {payload['assets']}")
        print(f"Fetch runs: {payload['fetch_runs']}")
        jobs = payload["preview_jobs"]
        print(
            "Preview jobs: "
            f"{jobs['pending']} pending, {jobs['running']} running, "
            f"{jobs['succeeded']} succeeded, {jobs['failed']} failed"
        )
        for variant, values in payload["cache"].items():
            print(f"Cache {variant}: {values['entries']} entries, {_format_bytes(values['bytes'])}")
        for scan in payload["archive_scans"]:
            directories = scan["directories"]
            print(
                f"Archive scan #{scan['id']} {scan['status']}: "
                f"{directories['succeeded']} succeeded, {directories['pending']} pending, "
                f"{directories['running']} running, {directories['failed']} failed"
            )
    return 0


def _run_scan(args: argparse.Namespace) -> int:
    catalog = CloudDriveCatalog(endpoint=args.endpoint, plist_path=args.plist)
    with StateStore(args.state_dir) as store:
        result = DirectoryImporter(store, catalog).scan(
            args.path,
            limit=args.limit,
            force_refresh=args.force_refresh,
            retry_failed=args.retry_failed,
        )
        queue = store.preview_job_counts()
    payload = result.as_dict() | {"preview_jobs": queue}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Remote directory: {result.remote_path}")
        print(
            f"ARWs: {result.discovered} discovered, {result.enqueued} enqueued, "
            f"{result.unchanged} unchanged"
        )
        print(f"Queue: {queue}")
        print("Archive modified: no")
    return 0


def _run_tree_scan(args: argparse.Namespace) -> int:
    if args.scan_id is not None and (args.force_refresh or args.include_hidden):
        raise ConfigurationError(
            "--force-refresh and --include-hidden are fixed when a scan starts"
        )
    catalog = CloudDriveCatalog(endpoint=args.endpoint, plist_path=args.plist)
    try:
        with StateStore(args.state_dir) as store:
            scanner = ArchiveTreeScanner(store, catalog)
            if args.scan_id is not None:
                result = scanner.resume(
                    args.scan_id,
                    max_directories=args.max_directories,
                    retry_failed=args.retry_failed,
                )
            else:
                result = scanner.start(
                    args.path,
                    max_directories=args.max_directories,
                    force_refresh=args.force_refresh,
                    include_hidden=args.include_hidden,
                    retry_failed=args.retry_failed,
                )
            queue = store.preview_job_counts()
    except ValueError as error:
        raise ConfigurationError(str(error)) from error
    payload = result.as_dict() | {"preview_jobs": queue}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        status = result.status
        directories = status["directories"]
        print(f"Archive scan #{result.scan_id}: {status['status']}")
        print(f"Remote root: {result.remote_root}")
        print(
            f"This run: {result.processed_directories} directories, "
            f"{result.discovered_assets} ARWs discovered, "
            f"{result.enqueued_assets} enqueued"
        )
        print(
            f"Tree: {directories['succeeded']} succeeded, {directories['pending']} pending, "
            f"{directories['running']} running, {directories['failed']} failed"
        )
        print(f"Auxiliary directories excluded: {status['excluded_subdirectories']}")
        print(f"Preview queue: {queue}")
        print("Archive modified: no")
    return 0


def _run_tree_scan_cancel(args: argparse.Namespace) -> int:
    try:
        with StateStore(args.state_dir) as store:
            cancelled = store.cancel_archive_scan(args.scan_id)
            status = store.archive_scan_status(args.scan_id)
    except ValueError as error:
        raise ConfigurationError(str(error)) from error
    payload = {
        "scan_id": args.scan_id,
        "cancelled": cancelled,
        "status": status,
        "archive_modified": False,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Archive scan #{args.scan_id}: {status['status']}")
        print("Cancellation takes effect between directory listings.")
        print("Archive modified: no")
    return 0


def _run_worker(args: argparse.Namespace) -> int:
    budgets = {
        "contact": _gib(args.contact_budget_gib),
        "preview": _gib(args.preview_budget_gib),
        "temporary": _gib(args.temporary_budget_gib),
    }
    source_factory = partial(
        CloudDriveRangeSource,
        endpoint=args.endpoint,
        plist_path=args.plist,
        timeout_seconds=args.timeout_seconds,
    )
    with StateStore(args.state_dir) as store:
        cache = CacheManager(store, budgets)
        pipeline = PreviewPipeline(
            store,
            cache,
            ExifToolProbe(),
            initial_prefix_bytes=args.initial_prefix_kib * 1024,
            maximum_prefix_bytes=args.maximum_prefix_mib * MIB,
            maximum_preview_bytes=args.maximum_preview_mib * MIB,
        )
        result = PreviewWorker(store, pipeline, source_factory).run(
            max_jobs=args.max_jobs,
            max_consecutive_failures=args.max_consecutive_failures,
            retry_failed=args.retry_failed,
        )
        queue = store.preview_job_counts()
    payload = result.as_dict() | {"preview_jobs": queue}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            f"Jobs: {result.processed} processed, {result.succeeded} succeeded, "
            f"{result.failed} failed, {result.cache_hits} cache hits"
        )
        print(f"Network: {_format_bytes(result.bytes_transferred)} transferred")
        print(f"Queue ordering: {result.reprioritized_jobs} pending jobs prioritized by date")
        if result.stopped_after_consecutive_failures:
            print(f"Worker stopped after {args.max_consecutive_failures} consecutive failures")
        print(f"Queue: {queue}")
        print("Archive modified: no")
    return 0


def _run_serve(args: argparse.Namespace) -> int:
    serve_library(
        args.state_dir,
        host=args.host,
        port=args.port,
        allow_remote=args.allow_remote,
    )
    return 0


def _print_spike(payload: dict[str, object]) -> None:
    preview = payload["preview"]
    contact = payload["contact"]
    assert isinstance(preview, dict)
    assert isinstance(contact, dict)
    print(f"Remote asset: {payload['remote_path']}")
    print(f"Cache hit: {'yes' if payload['cache_hit'] else 'no'}")
    print(
        f"Network: {payload['range_requests']} range requests, "
        f"{_format_bytes(int(payload['bytes_transferred']))} transferred "
        f"from a {_format_bytes(int(payload['source_size_bytes']))} source"
    )
    print(f"Transfer ratio: {float(payload['transfer_ratio']) * 100:.3f}%")
    print(
        f"Elapsed: {payload['end_to_end_ms']} ms total "
        f"({payload['provider_metadata_ms']} ms metadata, "
        f"{payload['pipeline_elapsed_ms']} ms pipeline)"
    )
    print(
        f"Preview: {preview['width']}x{preview['height']}, "
        f"{_format_bytes(int(preview['size_bytes']))}, {preview['path']}"
    )
    print(
        f"Contact: {contact['width']}x{contact['height']}, "
        f"{_format_bytes(int(contact['size_bytes']))}, {contact['path']}"
    )
    print("Archive modified: no")


def _gib(value: float) -> int:
    if value < 0:
        raise ValueError("cache budgets cannot be negative")
    return round(value * GIB)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value cannot be negative")
    return parsed


def _port(value: str) -> int:
    parsed = int(value)
    if parsed < 0 or parsed > 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return parsed


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024 or unit == "TiB":
            return f"{amount:.2f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
