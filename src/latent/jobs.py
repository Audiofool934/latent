"""Resumable directory scanning and preview-job execution."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Protocol

from .models import DirectoryListing, PipelineResult, RemoteAsset
from .preview import PreviewPipeline
from .provider import RangeSource
from .storage import StateStore


class Catalog(Protocol):
    def list_directory(
        self,
        remote_path: str,
        *,
        extensions: set[str] | None = None,
        force_refresh: bool = False,
        limit: int | None = None,
    ) -> list[RemoteAsset]: ...


class TreeCatalog(Protocol):
    def list_directory_contents(
        self,
        remote_path: str,
        *,
        extensions: set[str] | None = None,
        force_refresh: bool = False,
    ) -> DirectoryListing: ...


@dataclass(frozen=True)
class DirectoryScanResult:
    remote_path: str
    discovered: int
    enqueued: int
    unchanged: int
    elapsed_ms: int

    def as_dict(self) -> dict[str, object]:
        return {
            "remote_path": self.remote_path,
            "discovered": self.discovered,
            "enqueued": self.enqueued,
            "unchanged": self.unchanged,
            "elapsed_ms": self.elapsed_ms,
            "archive_modified": False,
        }


@dataclass(frozen=True)
class WorkerFailure:
    remote_path: str
    error: str


@dataclass
class WorkerRunResult:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    cache_hits: int = 0
    bytes_transferred: int = 0
    elapsed_ms: int = 0
    recovered_jobs: int = 0
    failures: list[WorkerFailure] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "processed": self.processed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "cache_hits": self.cache_hits,
            "bytes_transferred": self.bytes_transferred,
            "elapsed_ms": self.elapsed_ms,
            "recovered_jobs": self.recovered_jobs,
            "failures": [failure.__dict__ for failure in self.failures],
            "archive_modified": False,
        }


@dataclass(frozen=True)
class ArchiveScanFailure:
    remote_path: str
    error: str


@dataclass
class ArchiveScanRunResult:
    scan_id: int
    remote_root: str
    resumed: bool
    processed_directories: int = 0
    succeeded_directories: int = 0
    failed_directories: int = 0
    recovered_directories: int = 0
    discovered_assets: int = 0
    enqueued_assets: int = 0
    unchanged_assets: int = 0
    excluded_directories: int = 0
    elapsed_ms: int = 0
    status: dict[str, object] = field(default_factory=dict)
    failures: list[ArchiveScanFailure] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "scan_id": self.scan_id,
            "remote_root": self.remote_root,
            "resumed": self.resumed,
            "processed_directories": self.processed_directories,
            "succeeded_directories": self.succeeded_directories,
            "failed_directories": self.failed_directories,
            "recovered_directories": self.recovered_directories,
            "discovered_assets": self.discovered_assets,
            "enqueued_assets": self.enqueued_assets,
            "unchanged_assets": self.unchanged_assets,
            "excluded_directories": self.excluded_directories,
            "elapsed_ms": self.elapsed_ms,
            "status": self.status,
            "failures": [failure.__dict__ for failure in self.failures],
            "archive_modified": False,
        }


class DirectoryImporter:
    def __init__(self, store: StateStore, catalog: Catalog) -> None:
        self.store = store
        self.catalog = catalog

    def scan(
        self,
        remote_path: str,
        *,
        limit: int | None = None,
        force_refresh: bool = False,
        retry_failed: bool = False,
    ) -> DirectoryScanResult:
        started = time.perf_counter()
        assets = self.catalog.list_directory(
            remote_path,
            extensions={"arw"},
            force_refresh=force_refresh,
            limit=limit,
        )
        enqueued = 0
        for priority, asset in enumerate(reversed(assets)):
            asset_id = self.store.upsert_asset(asset)
            if self.store.enqueue_preview_job(
                asset_id,
                asset.fingerprint,
                priority=priority,
                retry_failed=retry_failed,
            ):
                enqueued += 1
        return DirectoryScanResult(
            remote_path=remote_path,
            discovered=len(assets),
            enqueued=enqueued,
            unchanged=len(assets) - enqueued,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
        )


class ArchiveTreeScanner:
    def __init__(self, store: StateStore, catalog: TreeCatalog) -> None:
        self.store = store
        self.catalog = catalog

    def start(
        self,
        remote_root: str,
        *,
        max_directories: int,
        force_refresh: bool = False,
        include_hidden: bool = False,
        retry_failed: bool = False,
        stale_after: timedelta = timedelta(minutes=30),
    ) -> ArchiveScanRunResult:
        scan_id, resumed = self.store.start_archive_scan(
            remote_root,
            force_refresh=force_refresh,
            include_hidden=include_hidden,
        )
        return self._run(
            scan_id,
            resumed=resumed,
            max_directories=max_directories,
            retry_failed=retry_failed,
            stale_after=stale_after,
        )

    def resume(
        self,
        scan_id: int,
        *,
        max_directories: int,
        retry_failed: bool = False,
        stale_after: timedelta = timedelta(minutes=30),
    ) -> ArchiveScanRunResult:
        self.store.resume_archive_scan(scan_id, retry_failed=retry_failed)
        return self._run(
            scan_id,
            resumed=True,
            max_directories=max_directories,
            retry_failed=retry_failed,
            stale_after=stale_after,
        )

    def _run(
        self,
        scan_id: int,
        *,
        resumed: bool,
        max_directories: int,
        retry_failed: bool,
        stale_after: timedelta,
    ) -> ArchiveScanRunResult:
        if max_directories <= 0:
            raise ValueError("max_directories must be positive")
        started = time.perf_counter()
        config = self.store.archive_scan_config(scan_id)
        stale_before = (datetime.now(UTC) - stale_after).isoformat()
        outcome = ArchiveScanRunResult(
            scan_id=scan_id,
            remote_root=str(config["remote_root"]),
            resumed=resumed,
            recovered_directories=self.store.requeue_running_archive_directories(
                scan_id,
                before=stale_before,
            ),
        )
        while outcome.processed_directories < max_directories:
            if self.store.archive_scan_config(scan_id)["status"] != "active":
                break
            job = self.store.claim_archive_directory(scan_id)
            if job is None:
                break
            outcome.processed_directories += 1
            try:
                listing = self.catalog.list_directory_contents(
                    job.remote_path,
                    extensions={"arw"},
                    force_refresh=bool(config["force_refresh"]),
                )
                included_directories, excluded_count = _filter_directories(
                    listing.directories,
                    include_hidden=bool(config["include_hidden"]),
                )
                for directory in included_directories:
                    self.store.enqueue_archive_directory(
                        scan_id,
                        directory,
                        job.depth + 1,
                    )
                enqueued = 0
                for priority, asset in enumerate(reversed(listing.assets)):
                    asset_id = self.store.upsert_asset(asset)
                    if self.store.enqueue_preview_job(
                        asset_id,
                        asset.fingerprint,
                        priority=priority,
                        retry_failed=retry_failed,
                    ):
                        enqueued += 1
                discovered = len(listing.assets)
                unchanged = discovered - enqueued
                self.store.finish_archive_directory(
                    job.id,
                    succeeded=True,
                    discovered_subdirectories=len(listing.directories),
                    excluded_subdirectories=excluded_count,
                    discovered_assets=discovered,
                    enqueued_assets=enqueued,
                    unchanged_assets=unchanged,
                )
                outcome.succeeded_directories += 1
                outcome.discovered_assets += discovered
                outcome.enqueued_assets += enqueued
                outcome.unchanged_assets += unchanged
                outcome.excluded_directories += excluded_count
            except KeyboardInterrupt:
                self.store.release_archive_directory(job.id)
                raise
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                self.store.finish_archive_directory(
                    job.id,
                    succeeded=False,
                    error=message,
                )
                outcome.failed_directories += 1
                outcome.failures.append(ArchiveScanFailure(job.remote_path, message))
        outcome.status = self.store.finalize_archive_scan(scan_id)
        outcome.elapsed_ms = round((time.perf_counter() - started) * 1000)
        return outcome


class PreviewWorker:
    def __init__(
        self,
        store: StateStore,
        pipeline: PreviewPipeline,
        source_factory: Callable[[str], AbstractContextManager[RangeSource]],
    ) -> None:
        self.store = store
        self.pipeline = pipeline
        self.source_factory = source_factory

    def run(
        self, *, max_jobs: int, stale_after: timedelta = timedelta(minutes=30)
    ) -> WorkerRunResult:
        if max_jobs <= 0:
            raise ValueError("max_jobs must be positive")
        started = time.perf_counter()
        stale_before = (datetime.now(UTC) - stale_after).isoformat()
        outcome = WorkerRunResult(
            recovered_jobs=self.store.requeue_running_preview_jobs(before=stale_before)
        )
        while outcome.processed < max_jobs:
            job = self.store.claim_preview_job()
            if job is None:
                break
            outcome.processed += 1
            fingerprint = job.fingerprint
            try:
                with self.source_factory(job.remote_path) as source:
                    result = self.pipeline.run(source)
                    fingerprint = source.asset.fingerprint
                self.store.finish_preview_job(
                    job.id,
                    succeeded=True,
                    fingerprint=fingerprint,
                )
                self._record_success(outcome, result)
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                self.store.finish_preview_job(
                    job.id,
                    succeeded=False,
                    fingerprint=fingerprint,
                    error=message,
                )
                outcome.failed += 1
                outcome.failures.append(WorkerFailure(job.remote_path, message))
        outcome.elapsed_ms = round((time.perf_counter() - started) * 1000)
        return outcome

    @staticmethod
    def _record_success(outcome: WorkerRunResult, result: PipelineResult) -> None:
        outcome.succeeded += 1
        outcome.cache_hits += int(result.cache_hit)
        outcome.bytes_transferred += result.bytes_transferred


def _filter_directories(
    directories: tuple[str, ...],
    *,
    include_hidden: bool,
) -> tuple[tuple[str, ...], int]:
    if include_hidden:
        return directories, 0
    included = tuple(
        directory
        for directory in directories
        if not PurePosixPath(directory).name.startswith((".", "_"))
    )
    return included, len(directories) - len(included)
