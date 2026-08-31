"""SQLite index and bounded local preview cache."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import CacheEntry, PipelineResult, PreviewJob, ProbeResult, RemoteAsset

GIB = 1024**3
DEFAULT_CACHE_BUDGETS = {
    "contact": 3 * GIB,
    "preview": 4 * GIB,
    "temporary": 1 * GIB,
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class StateStore:
    """Owns rebuildable Phase 0 state under one local directory."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir.expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.state_dir / "index.sqlite"
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY,
                provider TEXT NOT NULL,
                remote_id TEXT NOT NULL,
                remote_path TEXT NOT NULL,
                name TEXT NOT NULL,
                extension TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
                write_time TEXT,
                file_hashes_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                capture_at TEXT,
                camera_model TEXT,
                lens_model TEXT,
                exif_json TEXT,
                preview_tag TEXT,
                preview_offset INTEGER,
                preview_length INTEGER,
                preview_width INTEGER,
                preview_height INTEGER,
                first_indexed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(provider, remote_path)
            );

            CREATE INDEX IF NOT EXISTS idx_assets_capture_at ON assets(capture_at);
            CREATE INDEX IF NOT EXISTS idx_assets_extension ON assets(extension);

            CREATE TABLE IF NOT EXISTS cache_entries (
                asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                variant TEXT NOT NULL CHECK(variant IN ('contact', 'preview', 'temporary')),
                fingerprint TEXT NOT NULL,
                relative_path TEXT NOT NULL UNIQUE,
                size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
                width INTEGER NOT NULL CHECK(width > 0),
                height INTEGER NOT NULL CHECK(height > 0),
                created_ns INTEGER NOT NULL,
                last_accessed_ns INTEGER NOT NULL,
                PRIMARY KEY(asset_id, variant)
            );

            CREATE INDEX IF NOT EXISTS idx_cache_lru
            ON cache_entries(variant, last_accessed_ns);

            CREATE TABLE IF NOT EXISTS fetch_runs (
                id INTEGER PRIMARY KEY,
                asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                cache_hit INTEGER NOT NULL,
                range_requests INTEGER NOT NULL,
                bytes_transferred INTEGER NOT NULL,
                source_size_bytes INTEGER NOT NULL,
                provider_metadata_ms INTEGER NOT NULL DEFAULT 0,
                elapsed_ms INTEGER NOT NULL,
                archive_modified INTEGER NOT NULL DEFAULT 0 CHECK(archive_modified = 0)
            );

            CREATE TABLE IF NOT EXISTS preview_jobs (
                id INTEGER PRIMARY KEY,
                asset_id INTEGER NOT NULL UNIQUE REFERENCES assets(id) ON DELETE CASCADE,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'succeeded', 'failed')),
                priority INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                claimed_at TEXT,
                finished_at TEXT,
                last_error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_preview_jobs_claim
            ON preview_jobs(status, priority DESC, id ASC);
            """
        )
        self._ensure_column(
            "fetch_runs",
            "provider_metadata_ms",
            "INTEGER NOT NULL DEFAULT 0",
        )
        self.connection.commit()

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {
            str(row["name"]) for row in self.connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def upsert_asset(self, asset: RemoteAsset) -> int:
        now = utc_now()
        hashes = json.dumps(dict(sorted(asset.file_hashes.items())), sort_keys=True)
        self.connection.execute(
            """
            INSERT INTO assets (
                provider, remote_id, remote_path, name, extension, size_bytes,
                write_time, file_hashes_json, fingerprint, first_indexed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, remote_path) DO UPDATE SET
                remote_id=excluded.remote_id,
                name=excluded.name,
                extension=excluded.extension,
                size_bytes=excluded.size_bytes,
                write_time=excluded.write_time,
                file_hashes_json=excluded.file_hashes_json,
                fingerprint=excluded.fingerprint,
                updated_at=excluded.updated_at
            """,
            (
                asset.provider,
                asset.remote_id,
                asset.remote_path,
                asset.name,
                asset.extension,
                asset.size_bytes,
                asset.write_time,
                hashes,
                asset.fingerprint,
                now,
                now,
            ),
        )
        row = self.connection.execute(
            "SELECT id FROM assets WHERE provider=? AND remote_path=?",
            (asset.provider, asset.remote_path),
        ).fetchone()
        if row is None:
            raise RuntimeError("asset upsert did not return a row")
        self.connection.commit()
        return int(row["id"])

    def update_probe(
        self,
        asset_id: int,
        probe: ProbeResult,
        *,
        width: int,
        height: int,
    ) -> None:
        metadata = dict(probe.metadata)
        self.connection.execute(
            """
            UPDATE assets SET
                capture_at=?, camera_model=?, lens_model=?, exif_json=?,
                preview_tag=?, preview_offset=?, preview_length=?,
                preview_width=?, preview_height=?, updated_at=?
            WHERE id=?
            """,
            (
                _first(metadata, "DateTimeOriginal", "CreateDate"),
                _first(metadata, "Model", "CameraModelName"),
                _first(metadata, "LensModel", "Lens"),
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str),
                probe.location.tag,
                probe.location.offset,
                probe.location.length,
                width,
                height,
                utc_now(),
                asset_id,
            ),
        )
        self.connection.commit()

    def probe_details(self, asset_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT preview_tag, preview_offset, preview_length,
                   preview_width, preview_height
            FROM assets
            WHERE id=?
            """,
            (asset_id,),
        ).fetchone()
        return dict(row) if row is not None else {}

    def get_cache_row(self, asset_id: int, variant: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM cache_entries WHERE asset_id=? AND variant=?",
            (asset_id, variant),
        ).fetchone()

    def put_cache_row(
        self,
        *,
        asset_id: int,
        variant: str,
        fingerprint: str,
        relative_path: str,
        size_bytes: int,
        width: int,
        height: int,
    ) -> None:
        now_ns = time.time_ns()
        self.connection.execute(
            """
            INSERT INTO cache_entries (
                asset_id, variant, fingerprint, relative_path, size_bytes,
                width, height, created_ns, last_accessed_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id, variant) DO UPDATE SET
                fingerprint=excluded.fingerprint,
                relative_path=excluded.relative_path,
                size_bytes=excluded.size_bytes,
                width=excluded.width,
                height=excluded.height,
                created_ns=excluded.created_ns,
                last_accessed_ns=excluded.last_accessed_ns
            """,
            (
                asset_id,
                variant,
                fingerprint,
                relative_path,
                size_bytes,
                width,
                height,
                now_ns,
                now_ns,
            ),
        )
        self.connection.commit()

    def touch_cache_row(self, asset_id: int, variant: str) -> None:
        self.connection.execute(
            "UPDATE cache_entries SET last_accessed_ns=? WHERE asset_id=? AND variant=?",
            (time.time_ns(), asset_id, variant),
        )
        self.connection.commit()

    def delete_cache_row(self, asset_id: int, variant: str) -> None:
        self.connection.execute(
            "DELETE FROM cache_entries WHERE asset_id=? AND variant=?",
            (asset_id, variant),
        )
        self.connection.commit()

    def cache_rows_for_variant(self, variant: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM cache_entries
                WHERE variant=?
                ORDER BY last_accessed_ns ASC, relative_path ASC
                """,
                (variant,),
            )
        )

    def cache_total(self, variant: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM cache_entries WHERE variant=?",
            (variant,),
        ).fetchone()
        return int(row["total"] if row else 0)

    def record_fetch(self, result: PipelineResult, started_at: str) -> None:
        self.connection.execute(
            """
            INSERT INTO fetch_runs (
                asset_id, started_at, finished_at, cache_hit, range_requests,
                bytes_transferred, source_size_bytes, provider_metadata_ms,
                elapsed_ms, archive_modified
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                result.asset_id,
                started_at,
                utc_now(),
                int(result.cache_hit),
                result.range_requests,
                result.bytes_transferred,
                result.source_size_bytes,
                result.provider_metadata_ms,
                result.elapsed_ms,
            ),
        )
        self.connection.commit()

    def enqueue_preview_job(
        self,
        asset_id: int,
        fingerprint: str,
        *,
        priority: int = 0,
        retry_failed: bool = False,
    ) -> bool:
        row = self.connection.execute(
            "SELECT fingerprint, status FROM preview_jobs WHERE asset_id=?",
            (asset_id,),
        ).fetchone()
        now = utc_now()
        if row is None:
            self.connection.execute(
                """
                INSERT INTO preview_jobs (
                    asset_id, fingerprint, status, priority, attempts,
                    created_at, updated_at
                ) VALUES (?, ?, 'pending', ?, 0, ?, ?)
                """,
                (asset_id, fingerprint, priority, now, now),
            )
            self.connection.commit()
            return True
        fingerprint_changed = str(row["fingerprint"]) != fingerprint
        should_retry = str(row["status"]) == "failed" and retry_failed
        if not fingerprint_changed and not should_retry:
            return False
        self.connection.execute(
            """
            UPDATE preview_jobs SET
                fingerprint=?, status='pending', priority=?, attempts=0,
                updated_at=?, claimed_at=NULL, finished_at=NULL, last_error=NULL
            WHERE asset_id=?
            """,
            (fingerprint, priority, now, asset_id),
        )
        self.connection.commit()
        return True

    def claim_preview_job(self) -> PreviewJob | None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """
                SELECT j.id, j.asset_id, j.fingerprint, j.attempts, j.priority,
                       a.remote_path
                FROM preview_jobs AS j
                JOIN assets AS a ON a.id = j.asset_id
                WHERE j.status='pending'
                ORDER BY j.priority DESC, j.id ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            now = utc_now()
            self.connection.execute(
                """
                UPDATE preview_jobs SET
                    status='running', attempts=attempts + 1,
                    claimed_at=?, updated_at=?, last_error=NULL
                WHERE id=? AND status='pending'
                """,
                (now, now, row["id"]),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return PreviewJob(
            id=int(row["id"]),
            asset_id=int(row["asset_id"]),
            remote_path=str(row["remote_path"]),
            fingerprint=str(row["fingerprint"]),
            attempts=int(row["attempts"]) + 1,
            priority=int(row["priority"]),
        )

    def finish_preview_job(
        self,
        job_id: int,
        *,
        succeeded: bool,
        fingerprint: str,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        self.connection.execute(
            """
            UPDATE preview_jobs SET
                fingerprint=?, status=?, updated_at=?, finished_at=?, last_error=?
            WHERE id=? AND status='running'
            """,
            (
                fingerprint,
                "succeeded" if succeeded else "failed",
                now,
                now,
                error[:1000] if error else None,
                job_id,
            ),
        )
        self.connection.commit()

    def requeue_running_preview_jobs(self, *, before: str) -> int:
        cursor = self.connection.execute(
            """
            UPDATE preview_jobs SET
                status='pending', updated_at=?, claimed_at=NULL,
                finished_at=NULL, last_error='worker interrupted before completion'
            WHERE status='running' AND claimed_at < ?
            """,
            (utc_now(), before),
        )
        self.connection.commit()
        return int(cursor.rowcount)

    def preview_job_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in ("pending", "running", "succeeded", "failed")}
        for row in self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM preview_jobs GROUP BY status"
        ):
            counts[str(row["status"])] = int(row["count"])
        return counts

    def library_dates(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT
                    substr(a.capture_at, 1, 4) || '-' ||
                    substr(a.capture_at, 6, 2) || '-' ||
                    substr(a.capture_at, 9, 2) AS capture_date,
                    COUNT(*) AS asset_count
                FROM assets AS a
                JOIN cache_entries AS contact
                  ON contact.asset_id = a.id AND contact.variant = 'contact'
                JOIN cache_entries AS preview
                  ON preview.asset_id = a.id AND preview.variant = 'preview'
                WHERE a.capture_at IS NOT NULL
                GROUP BY capture_date
                ORDER BY capture_date DESC
                """
            )
        ]

    def library_assets(
        self,
        *,
        capture_date: str | None = None,
        limit: int = 250,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions = ["1 = 1"]
        parameters: list[object] = []
        if capture_date is not None:
            conditions.append(
                """
                substr(a.capture_at, 1, 4) || '-' ||
                substr(a.capture_at, 6, 2) || '-' ||
                substr(a.capture_at, 9, 2) = ?
                """
            )
            parameters.append(capture_date)
        parameters.extend((limit, offset))
        query = f"""
            SELECT
                a.id, a.name, a.remote_path, a.size_bytes, a.capture_at,
                a.camera_model, a.lens_model, a.preview_width, a.preview_height,
                contact.relative_path AS contact_path,
                preview.relative_path AS preview_path
            FROM assets AS a
            JOIN cache_entries AS contact
              ON contact.asset_id = a.id AND contact.variant = 'contact'
            JOIN cache_entries AS preview
              ON preview.asset_id = a.id AND preview.variant = 'preview'
            WHERE {" AND ".join(conditions)}
            ORDER BY a.capture_at ASC, a.name ASC
            LIMIT ? OFFSET ?
        """
        return [dict(row) for row in self.connection.execute(query, parameters)]

    def status(self) -> dict[str, Any]:
        asset_count = self.connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        run_count = self.connection.execute("SELECT COUNT(*) FROM fetch_runs").fetchone()[0]
        cache = {
            variant: {
                "entries": self.connection.execute(
                    "SELECT COUNT(*) FROM cache_entries WHERE variant=?", (variant,)
                ).fetchone()[0],
                "bytes": self.cache_total(variant),
            }
            for variant in DEFAULT_CACHE_BUDGETS
        }
        integrity = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        return {
            "state_dir": str(self.state_dir),
            "database_path": str(self.database_path),
            "database_integrity": integrity,
            "assets": asset_count,
            "fetch_runs": run_count,
            "preview_jobs": self.preview_job_counts(),
            "cache": cache,
        }


class CacheManager:
    """Stores generated images atomically and enforces per-variant LRU budgets."""

    def __init__(
        self,
        store: StateStore,
        budgets: Mapping[str, int] | None = None,
    ) -> None:
        self.store = store
        self.root = (store.state_dir / "cache").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.budgets = dict(DEFAULT_CACHE_BUDGETS if budgets is None else budgets)

    def get(self, asset_id: int, variant: str, fingerprint: str) -> CacheEntry | None:
        row = self.store.get_cache_row(asset_id, variant)
        if row is None:
            return None
        path = self._absolute(row["relative_path"])
        valid = (
            row["fingerprint"] == fingerprint
            and path.is_file()
            and path.stat().st_size == row["size_bytes"]
        )
        if not valid:
            self._delete_row_and_file(row)
            return None
        self.store.touch_cache_row(asset_id, variant)
        return _cache_entry_from_row(row, path)

    def put(
        self,
        *,
        asset_id: int,
        variant: str,
        fingerprint: str,
        data: bytes,
        width: int,
        height: int,
    ) -> tuple[CacheEntry, tuple[Path, ...]]:
        if variant not in DEFAULT_CACHE_BUDGETS:
            raise ValueError(f"unsupported cache variant: {variant}")
        old_row = self.store.get_cache_row(asset_id, variant)
        digest = _cache_digest(fingerprint, variant)
        relative_path = Path(variant) / digest[:2] / f"{digest}.jpg"
        target = self._absolute(relative_path.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(temporary, target)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.store.put_cache_row(
            asset_id=asset_id,
            variant=variant,
            fingerprint=fingerprint,
            relative_path=relative_path.as_posix(),
            size_bytes=len(data),
            width=width,
            height=height,
        )
        if old_row is not None and old_row["relative_path"] != relative_path.as_posix():
            self._unlink(old_row["relative_path"])
        evicted = self.prune_variant(variant, protect=relative_path.as_posix())
        entry = CacheEntry(
            asset_id=asset_id,
            variant=variant,
            fingerprint=fingerprint,
            path=target,
            size_bytes=len(data),
            width=width,
            height=height,
        )
        return entry, evicted

    def prune_variant(self, variant: str, *, protect: str | None = None) -> tuple[Path, ...]:
        budget = self.budgets.get(variant)
        if budget is None or budget < 0:
            return ()
        total = self.store.cache_total(variant)
        evicted: list[Path] = []
        for row in self.store.cache_rows_for_variant(variant):
            if total <= budget:
                break
            if row["relative_path"] == protect:
                continue
            path = self._absolute(row["relative_path"])
            size = int(row["size_bytes"])
            self._delete_row_and_file(row)
            total -= size
            evicted.append(path)
        return tuple(evicted)

    def _delete_row_and_file(self, row: sqlite3.Row) -> None:
        self._unlink(row["relative_path"])
        self.store.delete_cache_row(int(row["asset_id"]), str(row["variant"]))

    def _unlink(self, relative_path: str) -> None:
        self._absolute(relative_path).unlink(missing_ok=True)

    def _absolute(self, relative_path: str) -> Path:
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("cache path escaped the cache root")
        return path


def _first(metadata: Mapping[str, Any], *keys: str) -> Any | None:
    for key in keys:
        value = metadata.get(key)
        if value not in (None, ""):
            return value
    return None


def _cache_digest(fingerprint: str, variant: str) -> str:
    return hashlib.sha256(f"{fingerprint}:{variant}".encode()).hexdigest()


def _cache_entry_from_row(row: sqlite3.Row, path: Path) -> CacheEntry:
    return CacheEntry(
        asset_id=int(row["asset_id"]),
        variant=str(row["variant"]),
        fingerprint=str(row["fingerprint"]),
        path=path,
        size_bytes=int(row["size_bytes"]),
        width=int(row["width"]),
        height=int(row["height"]),
    )
