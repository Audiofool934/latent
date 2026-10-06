"""Verified archive copies retain the imported photo's catalog identity."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from .errors import ConfigurationError
from .locations import check_folder, contained_path
from .models import RemoteAsset
from .provider import CloudDriveRangeSource


class ArchiveCopies:
    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "archive-copies.sqlite"

    def register(
        self, asset: RemoteAsset, destination: dict, relative: str, hashes: dict[str, str]
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        physical = str(Path(destination["path"]) / relative)
        cloud = destination.get("cloud")
        remote = str(PurePosixPath(cloud["remote_path"]) / relative) if cloud else None
        receipt = {
            "asset": asdict(asset),
            "folder": destination,
            "relative": relative,
            "physical_path": physical,
            "cloud_path": remote,
            "hashes": hashes,
        }
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS copies (
                provider TEXT NOT NULL, logical_path TEXT NOT NULL,
                physical_path TEXT NOT NULL UNIQUE, cloud_path TEXT UNIQUE,
                receipt TEXT NOT NULL, PRIMARY KEY(provider, logical_path))""")
            db.execute(
                "INSERT INTO copies VALUES (?,?,?,?,?) ON CONFLICT(provider,logical_path) "
                "DO UPDATE SET physical_path=excluded.physical_path, "
                "cloud_path=excluded.cloud_path, receipt=excluded.receipt",
                (asset.provider, asset.remote_path, physical, remote, json.dumps(receipt)),
            )

    def _read(self, query: str, arguments: tuple = ()) -> list:
        if not self.path.is_file():
            return []
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        try:
            return db.execute(query, arguments).fetchall()
        finally:
            db.close()

    def get(self, provider: str, path: str) -> dict | None:
        rows = self._read(
            "SELECT receipt FROM copies WHERE provider=? AND logical_path=?", (provider, path)
        )
        return json.loads(rows[0][0]) if rows else None

    def paths(self, *, cloud: bool = False) -> set[str]:
        column = "cloud_path" if cloud else "physical_path"
        return {row[0] for row in self._read(f"SELECT {column} FROM copies") if row[0]}


class ArchivedRangeSource:
    """Read a verified copy while exposing the original, stable catalog identity."""

    def __init__(self, receipt: dict) -> None:
        self.receipt = receipt
        self.asset = RemoteAsset(**receipt["asset"])
        self.expected_hashes = receipt["hashes"]
        self.range_requests = self.bytes_transferred = self.metadata_elapsed_ms = 0
        self.cloud = None
        if receipt["cloud_path"]:
            self.cloud = CloudDriveRangeSource(receipt["cloud_path"])
            actual = self.cloud.asset
            keys = set(actual.file_hashes) & {"1", "2"}
            if (
                actual.size_bytes != self.asset.size_bytes
                or not keys
                or any(actual.file_hashes[k].lower() != self.expected_hashes[k] for k in keys)
            ):
                raise ConfigurationError("The cloud archive no longer matches the verified copy")
        else:
            self.path = contained_path(check_folder(receipt["folder"]), receipt["relative"])
            self.initial = self.path.stat()
            if self.initial.st_size != self.asset.size_bytes:
                raise ConfigurationError("The archive copy changed after verification")

    def read_range(self, start: int, length: int) -> bytes:
        if start < 0 or length <= 0 or start >= self.asset.size_bytes:
            raise ValueError("Invalid photo byte range")
        if self.cloud is not None:
            data = self.cloud.read_range(start, length)
        else:
            check_folder(self.receipt["folder"])
            contained_path(Path(self.receipt["folder"]["path"]), self.receipt["relative"])
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as handle:
                current = os.fstat(handle.fileno())
                if any(
                    getattr(current, key) != getattr(self.initial, key)
                    for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
                ):
                    raise ConfigurationError("The archive copy changed during reading")
                handle.seek(start)
                data = handle.read(min(length, self.asset.size_bytes - start))
        self.range_requests += 1
        self.bytes_transferred += len(data)
        return data


class ArchivedEditingSource:
    def __init__(self, copies: ArchiveCopies, provider: str) -> None:
        self.copies = copies
        self.provider = provider

    def source(self, path: str) -> ArchivedRangeSource:
        receipt = self.copies.get(self.provider, path)
        if receipt is None:
            raise ConfigurationError(
                "Reconnect the original drive; this file has no verified archive copy"
            )
        return ArchivedRangeSource(receipt)

    def sidecars(self, path: str) -> list[str]:
        original = PurePosixPath(path)
        candidates = [path + ".dop", str(original.with_suffix(".xmp"))]
        return [p for p in candidates if self.copies.get(self.provider, p) is not None]
