"""Command-line entry point for the Phase 0 read-only spike."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import LatentError
from .preview import MIB, ExifToolProbe, PreviewPipeline
from .provider import (
    DEFAULT_CLOUDDRIVE_ENDPOINT,
    DEFAULT_CLOUDDRIVE_PLIST,
    CloudDriveRangeSource,
)
from .storage import GIB, CacheManager, StateStore

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

    status = subparsers.add_parser("status", help="inspect the local index and cache")
    status.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=_run_status)
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
        for variant, values in payload["cache"].items():
            print(f"Cache {variant}: {values['entries']} entries, {_format_bytes(values['bytes'])}")
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


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024 or unit == "TiB":
            return f"{amount:.2f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
