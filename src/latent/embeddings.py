"""Durable local image embedding queue and vector store."""

from __future__ import annotations

import math
import sqlite3
import struct
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .errors import ConfigurationError, EmbeddingServiceError
from .gemini import GEMINI_DIMENSIONS, GEMINI_MODEL

EMBEDDING_SCHEMA_VERSION = 1
DEFAULT_EMBEDDING_MODEL = GEMINI_MODEL
DEFAULT_EMBEDDING_DIMENSIONS = GEMINI_DIMENSIONS
EMBEDDING_DTYPE = "float16"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class EmbeddingAsset:
    asset_id: int
    provider: str
    remote_path: str
    fingerprint: str
    capture_at: str | None
    contact_relative_path: str

    @property
    def identity(self) -> tuple[str, str]:
        return self.provider, self.remote_path


@dataclass(frozen=True)
class EmbeddingJob:
    id: int
    asset_id: int
    provider: str
    remote_path: str
    fingerprint: str
    capture_at: str | None
    contact_relative_path: str
    attempts: int


@dataclass(frozen=True)
class EmbeddingSyncResult:
    discovered: int
    enqueued: int
    unchanged: int
    removed: int
    pending_jobs: int

    def as_dict(self) -> dict[str, int]:
        return {
            "discovered": self.discovered,
            "enqueued": self.enqueued,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "pending_jobs": self.pending_jobs,
        }


@dataclass(frozen=True)
class EmbeddingFailure:
    remote_path: str
    error: str


@dataclass
class EmbeddingRunResult:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    recovered_jobs: int = 0
    requeued_failed_jobs: int = 0
    vector_bytes_written: int = 0
    elapsed_ms: int = 0
    batch_fallbacks: int = 0
    stopped_after_consecutive_failures: bool = False
    failures: list[EmbeddingFailure] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "processed": self.processed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "recovered_jobs": self.recovered_jobs,
            "requeued_failed_jobs": self.requeued_failed_jobs,
            "vector_bytes_written": self.vector_bytes_written,
            "elapsed_ms": self.elapsed_ms,
            "batch_fallbacks": self.batch_fallbacks,
            "stopped_after_consecutive_failures": self.stopped_after_consecutive_failures,
            "failures": [failure.__dict__ for failure in self.failures],
            "source": "local_contact_cache",
            "archive_network_bytes": 0,
            "archive_modified": False,
        }


class ImageEmbeddingEncoder(Protocol):
    model_id: str

    def encode_images(self, paths: Sequence[Path], batch_size: int) -> Any: ...


class EmbeddingStore:
    """Owns one model-specific, rebuildable embedding database."""

    def __init__(
        self,
        embedding_dir: Path,
        *,
        model_id: str = DEFAULT_EMBEDDING_MODEL,
        dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        if dimensions < 1:
            raise ValueError("embedding dimensions must be positive")
        self.metadata = dict(metadata or {})
        reserved = {"schema_version", "model_id", "dimensions", "dtype"} & self.metadata.keys()
        if reserved:
            raise ValueError(f"embedding metadata cannot override {sorted(reserved)}")
        self.embedding_dir = embedding_dir.expanduser().resolve()
        self.embedding_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.embedding_dir / "index.sqlite"
        self.model_id = model_id
        self.dimensions = dimensions
        self._scope_sql = ""
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        try:
            self._migrate()
            self._validate_configuration()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> EmbeddingStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS embedding_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS embedding_jobs (
                id INTEGER PRIMARY KEY,
                asset_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                remote_path TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                capture_at TEXT,
                contact_relative_path TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK(status IN ('pending', 'running', 'succeeded', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                claimed_at TEXT,
                finished_at TEXT,
                last_error TEXT,
                UNIQUE(provider, remote_path)
            );

            CREATE INDEX IF NOT EXISTS idx_embedding_jobs_claim
            ON embedding_jobs(status, capture_at DESC, remote_path ASC);

            CREATE TABLE IF NOT EXISTS embeddings (
                job_id INTEGER PRIMARY KEY
                    REFERENCES embedding_jobs(id) ON DELETE CASCADE,
                fingerprint TEXT NOT NULL,
                dimensions INTEGER NOT NULL CHECK(dimensions > 0),
                dtype TEXT NOT NULL CHECK(dtype = 'float16'),
                vector BLOB NOT NULL CHECK(length(vector) > 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def _validate_configuration(self) -> None:
        # Extra metadata records the artifact and preprocessing of one index generation.
        expected = {
            "schema_version": str(EMBEDDING_SCHEMA_VERSION),
            "model_id": self.model_id,
            "dimensions": str(self.dimensions),
            "dtype": EMBEDDING_DTYPE,
            **{key: str(value) for key, value in self.metadata.items()},
        }
        rows = {
            str(row["key"]): str(row["value"])
            for row in self.connection.execute("SELECT key, value FROM embedding_meta")
        }
        if not rows:
            self.connection.executemany(
                "INSERT INTO embedding_meta (key, value) VALUES (?, ?)",
                expected.items(),
            )
            self.connection.commit()
            return
        mismatches = {
            key: (rows.get(key), expected.get(key))
            for key in expected.keys() | rows.keys()
            if rows.get(key) != expected.get(key)
        }
        if mismatches:
            details = ", ".join(
                f"{key}={actual!r}, expected {wanted!r}"
                for key, (actual, wanted) in sorted(mismatches.items())
            )
            raise ConfigurationError(f"embedding store configuration mismatch: {details}")

    def sync_assets(
        self,
        assets: Sequence[EmbeddingAsset],
        *,
        retry_failed: bool = False,
        prune_missing: bool = True,
    ) -> EmbeddingSyncResult:
        source = list(assets)
        identities = [asset.identity for asset in source]
        if len(set(identities)) != len(identities):
            raise ValueError("embedding asset identities must be unique")
        for asset in source:
            _validate_contact_relative_path(asset.contact_relative_path)

        enqueued = 0
        unchanged = 0
        removed_ids: list[int] = []
        now = utc_now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = {
                (str(row["provider"]), str(row["remote_path"])): row
                for row in self.connection.execute(
                    """
                    SELECT j.*, e.fingerprint AS vector_fingerprint
                    FROM embedding_jobs AS j
                    LEFT JOIN embeddings AS e ON e.job_id = j.id
                    """
                )
            }
            for asset in source:
                row = existing.get(asset.identity)
                if row is None:
                    self.connection.execute(
                        """
                        INSERT INTO embedding_jobs (
                            asset_id, provider, remote_path, fingerprint, capture_at,
                            contact_relative_path, status, attempts, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                        """,
                        (
                            asset.asset_id,
                            asset.provider,
                            asset.remote_path,
                            asset.fingerprint,
                            asset.capture_at,
                            asset.contact_relative_path,
                            now,
                            now,
                        ),
                    )
                    enqueued += 1
                    continue

                fingerprint_changed = str(row["fingerprint"]) != asset.fingerprint
                invalid_succeeded = (
                    str(row["status"]) == "succeeded"
                    and row["vector_fingerprint"] != asset.fingerprint
                )
                should_retry = retry_failed and str(row["status"]) == "failed"
                reset = fingerprint_changed or invalid_succeeded or should_retry
                if reset:
                    self.connection.execute(
                        "DELETE FROM embeddings WHERE job_id=?",
                        (row["id"],),
                    )
                    self.connection.execute(
                        """
                        UPDATE embedding_jobs SET
                            asset_id=?, fingerprint=?, capture_at=?, contact_relative_path=?,
                            status='pending', attempts=0, updated_at=?, claimed_at=NULL,
                            finished_at=NULL, last_error=NULL
                        WHERE id=?
                        """,
                        (
                            asset.asset_id,
                            asset.fingerprint,
                            asset.capture_at,
                            asset.contact_relative_path,
                            now,
                            row["id"],
                        ),
                    )
                    enqueued += 1
                else:
                    self.connection.execute(
                        """
                        UPDATE embedding_jobs SET
                            asset_id=?, capture_at=?, contact_relative_path=?, updated_at=?
                        WHERE id=?
                        """,
                        (
                            asset.asset_id,
                            asset.capture_at,
                            asset.contact_relative_path,
                            now,
                            row["id"],
                        ),
                    )
                    unchanged += 1

            source_identities = set(identities)
            removed_ids = [
                int(row["id"])
                for identity, row in existing.items()
                if prune_missing and identity not in source_identities
            ]
            if removed_ids:
                self.connection.executemany(
                    "DELETE FROM embedding_jobs WHERE id=?",
                    ((job_id,) for job_id in removed_ids),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

        counts = self.job_counts()
        return EmbeddingSyncResult(
            discovered=len(source),
            enqueued=enqueued,
            unchanged=unchanged,
            removed=len(removed_ids),
            pending_jobs=counts["pending"],
        )

    @contextmanager
    def job_scope(self, asset_ids: Sequence[int]):
        """A confirmed selection cannot claim or recover jobs outside that selection."""
        if self._scope_sql:
            raise ValueError("An embedding job scope is already active")
        self.connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS requested_assets(id INTEGER PRIMARY KEY)"
        )
        self.connection.execute("DELETE FROM requested_assets")
        self.connection.executemany("INSERT INTO requested_assets VALUES (?)",
                                    ((identifier,) for identifier in sorted(set(asset_ids))))
        self.connection.commit()
        self._scope_sql = " AND asset_id IN (SELECT id FROM requested_assets)"
        try:
            yield
        finally:
            self._scope_sql = ""

    def claim_jobs(self, limit: int) -> list[EmbeddingJob]:
        if limit < 1:
            raise ValueError("claim limit must be positive")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            rows = self.connection.execute(
                f"""
                SELECT *
                FROM embedding_jobs
                WHERE status='pending' {self._scope_sql}
                ORDER BY capture_at DESC, remote_path ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            if not rows:
                self.connection.commit()
                return []
            now = utc_now()
            self.connection.executemany(
                """
                UPDATE embedding_jobs SET
                    status='running', attempts=attempts + 1,
                    claimed_at=?, updated_at=?, last_error=NULL
                WHERE id=? AND status='pending'
                """,
                ((now, now, row["id"]) for row in rows),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return [
            EmbeddingJob(
                id=int(row["id"]),
                asset_id=int(row["asset_id"]),
                provider=str(row["provider"]),
                remote_path=str(row["remote_path"]),
                fingerprint=str(row["fingerprint"]),
                capture_at=str(row["capture_at"]) if row["capture_at"] else None,
                contact_relative_path=str(row["contact_relative_path"]),
                attempts=int(row["attempts"]) + 1,
            )
            for row in rows
        ]

    def finish_jobs(self, completed: Sequence[tuple[EmbeddingJob, bytes]]) -> None:
        if not completed:
            return
        now = utc_now()
        expected_bytes = self.dimensions * 2
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for job, vector in completed:
                if len(vector) != expected_bytes:
                    raise ValueError(
                        f"embedding vector has {len(vector)} bytes, expected {expected_bytes}"
                    )
                row = self.connection.execute(
                    "SELECT status, fingerprint FROM embedding_jobs WHERE id=?",
                    (job.id,),
                ).fetchone()
                if row is None or str(row["status"]) != "running":
                    raise RuntimeError(f"embedding job {job.id} is not running")
                if str(row["fingerprint"]) != job.fingerprint:
                    raise RuntimeError(f"embedding job {job.id} fingerprint changed while running")
                self.connection.execute(
                    """
                    INSERT INTO embeddings (
                        job_id, fingerprint, dimensions, dtype, vector,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        fingerprint=excluded.fingerprint,
                        dimensions=excluded.dimensions,
                        dtype=excluded.dtype,
                        vector=excluded.vector,
                        updated_at=excluded.updated_at
                    """,
                    (
                        job.id,
                        job.fingerprint,
                        self.dimensions,
                        EMBEDDING_DTYPE,
                        vector,
                        now,
                        now,
                    ),
                )
                self.connection.execute(
                    """
                    UPDATE embedding_jobs SET
                        status='succeeded', updated_at=?, finished_at=?, last_error=NULL
                    WHERE id=? AND status='running'
                    """,
                    (now, now, job.id),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def fail_job(self, job_id: int, error: str) -> None:
        now = utc_now()
        self.connection.execute(
            """
            UPDATE embedding_jobs SET
                status='failed', updated_at=?, finished_at=?, last_error=?
            WHERE id=? AND status='running'
            """,
            (now, now, error[:1000], job_id),
        )
        self.connection.commit()

    def release_jobs(self, job_ids: Sequence[int]) -> int:
        if not job_ids:
            return 0
        now = utc_now()
        cursor_count = 0
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for job_id in job_ids:
                cursor = self.connection.execute(
                    """
                    UPDATE embedding_jobs SET
                        status='pending', updated_at=?, claimed_at=NULL,
                        finished_at=NULL,
                        last_error='worker interrupted before completion'
                    WHERE id=? AND status='running'
                    """,
                    (now, job_id),
                )
                cursor_count += int(cursor.rowcount)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return cursor_count

    def requeue_running_jobs(self, *, before: str) -> int:
        cursor = self.connection.execute(
            f"""
            UPDATE embedding_jobs SET
                status='pending', updated_at=?, claimed_at=NULL,
                finished_at=NULL, last_error='worker interrupted before completion'
            WHERE status='running' AND claimed_at < ? {self._scope_sql}
            """,
            (utc_now(), before),
        )
        self.connection.commit()
        return int(cursor.rowcount)

    def requeue_failed_jobs(self) -> int:
        cursor = self.connection.execute(
            f"""
            UPDATE embedding_jobs SET
                status='pending', updated_at=?, claimed_at=NULL,
                finished_at=NULL, last_error=NULL
            WHERE status='failed' {self._scope_sql}
            """,
            (utc_now(),),
        )
        self.connection.commit()
        return int(cursor.rowcount)

    def job_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in ("pending", "running", "succeeded", "failed")}
        for row in self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM embedding_jobs GROUP BY status"
        ):
            counts[str(row["status"])] = int(row["count"])
        return counts

    def vector_rows(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT j.asset_id, j.provider, j.remote_path, j.fingerprint,
                       j.capture_at, e.vector
                FROM embeddings AS e
                JOIN embedding_jobs AS j ON j.id = e.job_id
                WHERE j.status='succeeded' AND e.fingerprint = j.fingerprint
                ORDER BY j.asset_id ASC
                """
            )
        ]

    def vector_revision(self) -> tuple[int, str, str, int, int]:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count,
                   COALESCE(MAX(e.updated_at), '') AS vector_updated_at,
                   COALESCE(MAX(j.updated_at), '') AS job_updated_at,
                   COALESCE(SUM(length(e.vector)), 0) AS bytes,
                   COALESCE(SUM(j.asset_id), 0) AS asset_id_sum
            FROM embeddings AS e
            JOIN embedding_jobs AS j ON j.id = e.job_id
            WHERE j.status='succeeded' AND e.fingerprint = j.fingerprint
            """
        ).fetchone()
        return (
            int(row["count"]),
            str(row["vector_updated_at"]),
            str(row["job_updated_at"]),
            int(row["bytes"]),
            int(row["asset_id_sum"]),
        )

    def status(self) -> dict[str, object]:
        # Hold one read snapshot while API workers commit new batches concurrently.
        self.connection.execute("SAVEPOINT embedding_status")
        try:
            return self._status_snapshot()
        finally:
            self.connection.execute("RELEASE embedding_status")

    def progress(self, *, exclude_asset_ids: Sequence[int] = ()) -> dict[str, object]:
        """Read queue coverage in one snapshot, without loading vectors or checking integrity."""
        stale_before = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
        condition = ""
        if exclude_asset_ids:
            self.connection.execute("CREATE TEMP TABLE excluded_assets(id INTEGER PRIMARY KEY)")
            self.connection.executemany(
                "INSERT OR IGNORE INTO excluded_assets VALUES (?)",
                ((identifier,) for identifier in exclude_asset_ids),
            )
            condition = "WHERE j.asset_id NOT IN (SELECT id FROM excluded_assets)"
        row = self.connection.execute(
            f"""
            SELECT COUNT(*) AS total,
                   COUNT(CASE WHEN j.status='pending' THEN 1 END) AS pending,
                   COUNT(CASE WHEN j.status='running' THEN 1 END) AS running,
                   COUNT(CASE WHEN j.status='succeeded' THEN 1 END) AS succeeded,
                   COUNT(CASE WHEN j.status='failed' THEN 1 END) AS failed,
                   COUNT(CASE WHEN j.status='running'
                       AND (j.claimed_at IS NULL OR j.claimed_at < ?) THEN 1 END) AS stale,
                   COUNT(CASE WHEN j.status='succeeded'
                       AND e.fingerprint=j.fingerprint THEN 1 END) AS indexed
            FROM embedding_jobs AS j
            LEFT JOIN embeddings AS e ON e.job_id=j.id
            {condition}
            """,
            (stale_before,),
        ).fetchone()
        if exclude_asset_ids:
            self.connection.execute("DROP TABLE excluded_assets")
        return {
            "queued_assets": int(row["total"]),
            "indexed_assets": int(row["indexed"]),
            "stale_jobs": int(row["stale"]),
            "jobs": {
                name: int(row[name]) for name in ("pending", "running", "succeeded", "failed")
            },
        }

    def _status_snapshot(self) -> dict[str, object]:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(length(vector)), 0) AS bytes FROM embeddings"
        ).fetchone()
        integrity = str(self.connection.execute("PRAGMA integrity_check").fetchone()[0])
        return {
            "schema_version": EMBEDDING_SCHEMA_VERSION,
            "model_id": self.model_id,
            "dimensions": self.dimensions,
            "dtype": EMBEDDING_DTYPE,
            "metadata": dict(self.metadata),
            "database_path": str(self.database_path),
            "database_integrity": integrity,
            "jobs": self.job_counts(),
            "vectors": int(row["count"]),
            "vector_bytes": int(row["bytes"]),
            "source": "local_contact_cache",
            "archive_network_bytes": 0,
            "archive_modified": False,
        }


class EmbeddingWorker:
    def __init__(
        self,
        store: EmbeddingStore,
        encoder: ImageEmbeddingEncoder,
        contact_root: Path,
    ) -> None:
        if encoder.model_id != store.model_id:
            raise ConfigurationError(
                f"encoder model {encoder.model_id!r} does not match store {store.model_id!r}"
            )
        self.store = store
        self.encoder = encoder
        self.contact_root = contact_root.expanduser().resolve()

    def run(
        self,
        *,
        max_jobs: int,
        batch_size: int = 8,
        stale_after: timedelta = timedelta(minutes=30),
        max_consecutive_failures: int = 5,
        retry_failed: bool = False,
        progress: Callable[[EmbeddingRunResult], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> EmbeddingRunResult:
        if max_jobs < 1:
            raise ValueError("max_jobs must be positive")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be positive")
        started = time.perf_counter()
        stale_before = (datetime.now(UTC) - stale_after).isoformat()
        outcome = EmbeddingRunResult(
            recovered_jobs=self.store.requeue_running_jobs(before=stale_before),
            requeued_failed_jobs=self.store.requeue_failed_jobs() if retry_failed else 0,
        )
        consecutive_failures = 0
        while outcome.processed < max_jobs:
            if stop_requested is not None and stop_requested():
                break
            remaining = max_jobs - outcome.processed
            jobs = self.store.claim_jobs(min(batch_size, remaining))
            if not jobs:
                break
            try:
                paths = [self._contact_path(job) for job in jobs]
                vectors = self.encoder.encode_images(paths, batch_size)
                completed = self._serialize_batch(jobs, vectors)
                self.store.finish_jobs(completed)
                outcome.processed += len(jobs)
                outcome.succeeded += len(jobs)
                outcome.vector_bytes_written += sum(len(vector) for _, vector in completed)
                consecutive_failures = 0
            except (KeyboardInterrupt, EmbeddingServiceError):
                self.store.release_jobs([job.id for job in jobs])
                raise
            except Exception:
                outcome.batch_fallbacks += 1
                consecutive_failures = self._process_individually(
                    jobs,
                    outcome,
                    consecutive_failures=consecutive_failures,
                    max_consecutive_failures=max_consecutive_failures,
                    stop_requested=stop_requested,
                )
                if outcome.stopped_after_consecutive_failures:
                    break
            if progress is not None:
                outcome.elapsed_ms = round((time.perf_counter() - started) * 1000)
                progress(outcome)
        outcome.elapsed_ms = round((time.perf_counter() - started) * 1000)
        return outcome

    def _process_individually(
        self,
        jobs: Sequence[EmbeddingJob],
        outcome: EmbeddingRunResult,
        *,
        consecutive_failures: int,
        max_consecutive_failures: int,
        stop_requested: Callable[[], bool] | None = None,
    ) -> int:
        for index, job in enumerate(jobs):
            if stop_requested is not None and stop_requested():
                self.store.release_jobs([remaining.id for remaining in jobs[index:]])
                break
            if consecutive_failures >= max_consecutive_failures:
                self.store.release_jobs([remaining.id for remaining in jobs[index:]])
                outcome.stopped_after_consecutive_failures = True
                break
            try:
                path = self._contact_path(job)
                vectors = self.encoder.encode_images([path], 1)
                completed = self._serialize_batch([job], vectors)
                self.store.finish_jobs(completed)
                outcome.succeeded += 1
                outcome.vector_bytes_written += len(completed[0][1])
                consecutive_failures = 0
            except (KeyboardInterrupt, EmbeddingServiceError):
                self.store.release_jobs([remaining.id for remaining in jobs[index:]])
                raise
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                self.store.fail_job(job.id, message)
                outcome.failed += 1
                outcome.failures.append(EmbeddingFailure(job.remote_path, message))
                consecutive_failures += 1
            finally:
                outcome.processed += 1
        if consecutive_failures >= max_consecutive_failures:
            outcome.stopped_after_consecutive_failures = True
        return consecutive_failures

    def _contact_path(self, job: EmbeddingJob) -> Path:
        path = (self.contact_root / job.contact_relative_path).resolve()
        if not path.is_relative_to(self.contact_root):
            raise ConfigurationError(
                f"contact cache path leaves the cache root: {job.contact_relative_path}"
            )
        if not path.is_file():
            raise FileNotFoundError(f"contact cache image was not found: {path}")
        return path

    def _serialize_batch(
        self,
        jobs: Sequence[EmbeddingJob],
        vectors: Any,
    ) -> list[tuple[EmbeddingJob, bytes]]:
        if len(vectors) != len(jobs):
            raise ValueError(f"encoder returned {len(vectors)} vectors for {len(jobs)} image jobs")
        return [
            (job, serialize_float16_vector(vector, self.store.dimensions))
            for job, vector in zip(jobs, vectors, strict=True)
        ]


def load_embedding_assets(state_dir: Path) -> list[EmbeddingAsset]:
    resolved_state = state_dir.expanduser().resolve()
    database_path = resolved_state / "index.sqlite"
    if not database_path.is_file():
        raise ConfigurationError(f"Latent library index was not found: {database_path}")
    cache_root = (resolved_state / "cache").resolve()
    with sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            """
            SELECT a.id, a.provider, a.remote_path, a.fingerprint, a.capture_at,
                   contact.relative_path
            FROM assets AS a
            JOIN cache_entries AS contact
              ON contact.asset_id = a.id
             AND contact.variant = 'contact'
             AND contact.fingerprint = a.fingerprint
            ORDER BY a.capture_at DESC, a.remote_path ASC
            """
        ).fetchall()
    assets = []
    for row in rows:
        relative_path = str(row[5])
        _validate_contact_relative_path(relative_path)
        resolved_path = (cache_root / relative_path).resolve()
        if not resolved_path.is_relative_to(cache_root):
            raise ConfigurationError(f"contact cache path leaves the cache root: {relative_path}")
        assets.append(
            EmbeddingAsset(
                asset_id=int(row[0]),
                provider=str(row[1]),
                remote_path=str(row[2]),
                fingerprint=str(row[3]),
                capture_at=str(row[4]) if row[4] else None,
                contact_relative_path=relative_path,
            )
        )
    return assets


def serialize_float16_vector(vector: Sequence[float], dimensions: int) -> bytes:
    values = [float(value) for value in vector]
    if len(values) != dimensions:
        raise ValueError(f"embedding has {len(values)} dimensions, expected {dimensions}")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("embedding contains a non-finite value")
    return struct.pack(f"<{dimensions}e", *values)


def _validate_contact_relative_path(relative_path: str) -> None:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ConfigurationError(f"invalid relative contact cache path: {relative_path}")
