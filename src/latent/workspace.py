"""Durable user-authored workspace state, separate from the rebuildable library."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import ConfigurationError

WORKSPACE_SCHEMA_VERSION = 1
WORKSPACE_EXPORT_FORMAT = "latent-workspace"
DEFAULT_WORKSPACE_DIR = Path.home() / "Library/Application Support/Latent/workspace"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class WorkspaceAsset:
    provider: str
    remote_path: str
    fingerprint: str
    name: str
    capture_at: str | None = None
    camera_model: str | None = None
    lens_model: str | None = None

    @property
    def identity(self) -> tuple[str, str]:
        return self.provider, self.remote_path


@dataclass(frozen=True)
class AddItemsResult:
    added: int
    skipped: int
    item_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ImportResult:
    imported: int
    unchanged: int
    conflicts: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class WorkspaceStore:
    """Owns user-created sequences and their stable archive references."""

    def __init__(self, workspace_dir: Path = DEFAULT_WORKSPACE_DIR) -> None:
        self.workspace_dir = workspace_dir.expanduser().resolve()
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.workspace_dir / "workspace.sqlite"
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        try:
            self._migrate()
            self._validate_configuration()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> WorkspaceStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspace_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sequences (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                origin TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sequence_items (
                id TEXT PRIMARY KEY,
                sequence_id TEXT NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
                position INTEGER NOT NULL CHECK(position >= 0),
                provider TEXT NOT NULL,
                remote_path TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                name TEXT NOT NULL,
                capture_at TEXT,
                camera_model TEXT,
                lens_model TEXT,
                note TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL,
                UNIQUE(sequence_id, position),
                UNIQUE(sequence_id, provider, remote_path)
            );

            CREATE INDEX IF NOT EXISTS idx_sequence_items_order
            ON sequence_items(sequence_id, position);
            """
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO workspace_meta(key, value) VALUES ('schema_version', ?)",
            (str(WORKSPACE_SCHEMA_VERSION),),
        )
        self.connection.commit()

    def _validate_configuration(self) -> None:
        row = self.connection.execute(
            "SELECT value FROM workspace_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None or str(row["value"]) != str(WORKSPACE_SCHEMA_VERSION):
            actual = None if row is None else str(row["value"])
            raise ConfigurationError(
                f"workspace schema version {actual!r} does not match {WORKSPACE_SCHEMA_VERSION!r}"
            )

    def create_sequence(
        self,
        name: str,
        *,
        note: str = "",
        origin: str = "manual",
        sequence_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_name = _required_text(name, "sequence name", maximum=120)
        normalized_note = _optional_text(note, "sequence note", maximum=10_000)
        normalized_origin = _required_text(origin, "sequence origin", maximum=50)
        identifier = sequence_id or str(uuid4())
        _required_text(identifier, "sequence id", maximum=100)
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO sequences(id, name, note, origin, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (identifier, normalized_name, normalized_note, normalized_origin, now, now),
        )
        self.connection.commit()
        return self.get_sequence(identifier)

    def list_sequences(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT s.*, COUNT(i.id) AS item_count
            FROM sequences AS s
            LEFT JOIN sequence_items AS i ON i.sequence_id = s.id
            GROUP BY s.id
            ORDER BY s.updated_at DESC, s.name ASC, s.id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def get_sequence(self, sequence_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM sequences WHERE id=?",
            (sequence_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"sequence {sequence_id!r} was not found")
        sequence = dict(row)
        sequence["items"] = [
            dict(item)
            for item in self.connection.execute(
                """
                SELECT * FROM sequence_items
                WHERE sequence_id=?
                ORDER BY position ASC, id ASC
                """,
                (sequence_id,),
            )
        ]
        sequence["item_count"] = len(sequence["items"])
        return sequence

    def update_sequence(
        self,
        sequence_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        current = self.get_sequence(sequence_id)
        next_name = (
            _required_text(name, "sequence name", maximum=120)
            if name is not None
            else str(current["name"])
        )
        next_note = (
            _optional_text(note, "sequence note", maximum=10_000)
            if note is not None
            else str(current["note"])
        )
        self.connection.execute(
            "UPDATE sequences SET name=?, note=?, updated_at=? WHERE id=?",
            (next_name, next_note, utc_now(), sequence_id),
        )
        self.connection.commit()
        return self.get_sequence(sequence_id)

    def delete_sequence(self, sequence_id: str) -> bool:
        cursor = self.connection.execute("DELETE FROM sequences WHERE id=?", (sequence_id,))
        self.connection.commit()
        return bool(cursor.rowcount)

    def add_items(
        self,
        sequence_id: str,
        assets: Sequence[WorkspaceAsset],
    ) -> AddItemsResult:
        source = list(assets)
        identities = [asset.identity for asset in source]
        if len(set(identities)) != len(identities):
            raise ValueError("sequence asset identities must be unique")
        for asset in source:
            _validate_asset(asset)

        now = utc_now()
        item_ids = []
        skipped = 0
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.get_sequence(sequence_id)
            existing = {
                (str(row["provider"]), str(row["remote_path"]))
                for row in self.connection.execute(
                    "SELECT provider, remote_path FROM sequence_items WHERE sequence_id=?",
                    (sequence_id,),
                )
            }
            row = self.connection.execute(
                """
                SELECT COALESCE(MAX(position), -1) AS position
                FROM sequence_items WHERE sequence_id=?
                """,
                (sequence_id,),
            ).fetchone()
            position = int(row["position"]) + 1
            for asset in source:
                if asset.identity in existing:
                    skipped += 1
                    continue
                item_id = str(uuid4())
                self.connection.execute(
                    """
                    INSERT INTO sequence_items(
                        id, sequence_id, position, provider, remote_path, fingerprint,
                        name, capture_at, camera_model, lens_model, note, added_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)
                    """,
                    (
                        item_id,
                        sequence_id,
                        position,
                        asset.provider,
                        asset.remote_path,
                        asset.fingerprint,
                        asset.name,
                        asset.capture_at,
                        asset.camera_model,
                        asset.lens_model,
                        now,
                    ),
                )
                item_ids.append(item_id)
                existing.add(asset.identity)
                position += 1
            if item_ids:
                self.connection.execute(
                    "UPDATE sequences SET updated_at=? WHERE id=?",
                    (now, sequence_id),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return AddItemsResult(len(item_ids), skipped, tuple(item_ids))

    def reorder_items(self, sequence_id: str, item_ids: Sequence[str]) -> dict[str, Any]:
        wanted = list(item_ids)
        if len(wanted) != len(set(wanted)):
            raise ValueError("reordered item IDs must be unique")
        now = utc_now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self.get_sequence(sequence_id)
            current_ids = [str(item["id"]) for item in current["items"]]
            if set(wanted) != set(current_ids):
                raise ValueError("reordered item IDs must include every sequence item exactly once")
            offset = len(wanted) + 1
            self.connection.execute(
                "UPDATE sequence_items SET position=position+? WHERE sequence_id=?",
                (offset, sequence_id),
            )
            self.connection.executemany(
                "UPDATE sequence_items SET position=? WHERE id=? AND sequence_id=?",
                ((position, item_id, sequence_id) for position, item_id in enumerate(wanted)),
            )
            self.connection.execute(
                "UPDATE sequences SET updated_at=? WHERE id=?",
                (now, sequence_id),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.get_sequence(sequence_id)

    def remove_item(self, sequence_id: str, item_id: str) -> bool:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.get_sequence(sequence_id)
            cursor = self.connection.execute(
                "DELETE FROM sequence_items WHERE id=? AND sequence_id=?",
                (item_id, sequence_id),
            )
            if cursor.rowcount:
                rows = self.connection.execute(
                    "SELECT id FROM sequence_items WHERE sequence_id=? ORDER BY position ASC",
                    (sequence_id,),
                ).fetchall()
                offset = len(rows) + 1
                self.connection.execute(
                    "UPDATE sequence_items SET position=position+? WHERE sequence_id=?",
                    (offset, sequence_id),
                )
                self.connection.executemany(
                    "UPDATE sequence_items SET position=? WHERE id=?",
                    ((position, str(row["id"])) for position, row in enumerate(rows)),
                )
                self.connection.execute(
                    "UPDATE sequences SET updated_at=? WHERE id=?",
                    (utc_now(), sequence_id),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return bool(cursor.rowcount)

    def export_payload(self) -> dict[str, Any]:
        return {
            "format": WORKSPACE_EXPORT_FORMAT,
            "schema_version": WORKSPACE_SCHEMA_VERSION,
            "exported_at": utc_now(),
            "sequences": [
                self.get_sequence(str(sequence["id"])) for sequence in self.list_sequences()
            ],
        }

    def merge_import(self, payload: Mapping[str, Any]) -> ImportResult:
        sequences = _validated_import_sequences(payload)
        imported = 0
        unchanged = 0
        conflicts = 0
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for sequence in sequences:
                existing = self.connection.execute(
                    "SELECT 1 FROM sequences WHERE id=?",
                    (sequence["id"],),
                ).fetchone()
                if existing is not None:
                    current = self.get_sequence(str(sequence["id"]))
                    if _comparable_sequence(current) == _comparable_sequence(sequence):
                        unchanged += 1
                    else:
                        conflicts += 1
                    continue
                self._insert_imported_sequence(sequence)
                imported += 1
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return ImportResult(imported, unchanged, conflicts)

    def _insert_imported_sequence(self, sequence: Mapping[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO sequences(id, name, note, origin, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                sequence["id"],
                sequence["name"],
                sequence["note"],
                sequence["origin"],
                sequence["created_at"],
                sequence["updated_at"],
            ),
        )
        self.connection.executemany(
            """
            INSERT INTO sequence_items(
                id, sequence_id, position, provider, remote_path, fingerprint,
                name, capture_at, camera_model, lens_model, note, added_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    item["id"],
                    sequence["id"],
                    item["position"],
                    item["provider"],
                    item["remote_path"],
                    item["fingerprint"],
                    item["name"],
                    item["capture_at"],
                    item["camera_model"],
                    item["lens_model"],
                    item["note"],
                    item["added_at"],
                )
                for item in sequence["items"]
            ),
        )

    def status(self) -> dict[str, Any]:
        sequence_count = int(
            self.connection.execute("SELECT COUNT(*) FROM sequences").fetchone()[0]
        )
        item_count = int(
            self.connection.execute("SELECT COUNT(*) FROM sequence_items").fetchone()[0]
        )
        integrity = str(self.connection.execute("PRAGMA integrity_check").fetchone()[0])
        return {
            "schema_version": WORKSPACE_SCHEMA_VERSION,
            "database_path": str(self.database_path),
            "database_integrity": integrity,
            "sequences": sequence_count,
            "items": item_count,
            "archive_modified": False,
        }


def write_workspace_export(path: Path, payload: Mapping[str, Any]) -> Path:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            os.chmod(temporary_path, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return destination


def load_workspace_export(path: Path) -> dict[str, Any]:
    source = path.expanduser().resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("workspace export root must be an object")
    return payload


def _validate_asset(asset: WorkspaceAsset) -> None:
    _required_text(asset.provider, "asset provider", maximum=120)
    _required_text(asset.remote_path, "asset remote path", maximum=4096)
    _required_text(asset.fingerprint, "asset fingerprint", maximum=2048)
    _required_text(asset.name, "asset name", maximum=1024)
    _optional_text(asset.capture_at, "asset capture time", maximum=120)
    _optional_text(asset.camera_model, "asset camera model", maximum=1024)
    _optional_text(asset.lens_model, "asset lens model", maximum=1024)


def _validated_import_sequences(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    if payload.get("format") != WORKSPACE_EXPORT_FORMAT:
        raise ValueError("workspace export format is not supported")
    if payload.get("schema_version") != WORKSPACE_SCHEMA_VERSION:
        raise ValueError("workspace export schema version is not supported")
    source = payload.get("sequences")
    if not isinstance(source, list):
        raise ValueError("workspace export sequences must be a list")
    sequence_ids: set[str] = set()
    item_ids: set[str] = set()
    validated = []
    for raw_sequence in source:
        if not isinstance(raw_sequence, Mapping):
            raise ValueError("workspace export sequence must be an object")
        sequence_id = _required_text(raw_sequence.get("id"), "sequence id", maximum=100)
        if sequence_id in sequence_ids:
            raise ValueError("workspace export sequence IDs must be unique")
        sequence_ids.add(sequence_id)
        raw_items = raw_sequence.get("items")
        if not isinstance(raw_items, list):
            raise ValueError("workspace export sequence items must be a list")
        items = []
        identities: set[tuple[str, str]] = set()
        for expected_position, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, Mapping):
                raise ValueError("workspace export sequence item must be an object")
            item_id = _required_text(raw_item.get("id"), "sequence item id", maximum=100)
            if item_id in item_ids:
                raise ValueError("workspace export item IDs must be unique")
            item_ids.add(item_id)
            position = raw_item.get("position")
            if position != expected_position:
                raise ValueError("workspace export item positions must be contiguous from zero")
            provider = _required_text(raw_item.get("provider"), "asset provider", maximum=120)
            remote_path = _required_text(
                raw_item.get("remote_path"),
                "asset remote path",
                maximum=4096,
            )
            identity = (provider, remote_path)
            if identity in identities:
                raise ValueError("workspace export asset identities must be unique per sequence")
            identities.add(identity)
            items.append(
                {
                    "id": item_id,
                    "sequence_id": sequence_id,
                    "position": expected_position,
                    "provider": provider,
                    "remote_path": remote_path,
                    "fingerprint": _required_text(
                        raw_item.get("fingerprint"),
                        "asset fingerprint",
                        maximum=2048,
                    ),
                    "name": _required_text(raw_item.get("name"), "asset name", maximum=1024),
                    "capture_at": _optional_text(
                        raw_item.get("capture_at"),
                        "asset capture time",
                        maximum=120,
                    ),
                    "camera_model": _optional_text(
                        raw_item.get("camera_model"),
                        "asset camera model",
                        maximum=1024,
                    ),
                    "lens_model": _optional_text(
                        raw_item.get("lens_model"),
                        "asset lens model",
                        maximum=1024,
                    ),
                    "note": _optional_text(
                        raw_item.get("note", ""),
                        "sequence item note",
                        maximum=5_000,
                    ),
                    "added_at": _required_text(
                        raw_item.get("added_at"),
                        "sequence item added time",
                        maximum=120,
                    ),
                }
            )
        validated.append(
            {
                "id": sequence_id,
                "name": _required_text(
                    raw_sequence.get("name"),
                    "sequence name",
                    maximum=120,
                ),
                "note": _optional_text(
                    raw_sequence.get("note", ""),
                    "sequence note",
                    maximum=10_000,
                ),
                "origin": _required_text(
                    raw_sequence.get("origin", "imported"),
                    "sequence origin",
                    maximum=50,
                ),
                "created_at": _required_text(
                    raw_sequence.get("created_at"),
                    "sequence created time",
                    maximum=120,
                ),
                "updated_at": _required_text(
                    raw_sequence.get("updated_at"),
                    "sequence updated time",
                    maximum=120,
                ),
                "items": items,
                "item_count": len(items),
            }
        )
    return validated


def _comparable_sequence(sequence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: sequence[key]
        for key in ("id", "name", "note", "origin", "created_at", "updated_at", "items")
    }


def _required_text(value: Any, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} cannot be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field} must be {maximum} characters or fewer")
    return normalized


def _optional_text(value: Any, field: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{field} must be {maximum} characters or fewer")
    return normalized
