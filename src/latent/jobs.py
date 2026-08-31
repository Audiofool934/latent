"""Resumable directory scanning and preview-job execution."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .models import PipelineResult, RemoteAsset
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
