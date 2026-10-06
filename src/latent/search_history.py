"""Local search history and reusable query vectors, independent of result ranking."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import numpy as np


class SearchHistory:
    def __init__(self, path: Path, *, library_id: str, model_id: str, dimensions: int) -> None:
        self.path = path
        self.library_id = library_id
        self.model_id = model_id
        self.dimensions = dimensions
        self.lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS searches (
                    id TEXT PRIMARY KEY, library_id TEXT NOT NULL, model_id TEXT NOT NULL,
                    dimensions INTEGER NOT NULL, kind TEXT NOT NULL, query TEXT NOT NULL,
                    image_name TEXT, image BLOB, vector BLOB NOT NULL,
                    filters_json TEXT NOT NULL, result_order TEXT NOT NULL,
                    created_at TEXT NOT NULL, used_at TEXT NOT NULL
                )
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def resolve(
        self, *, query: str, image: bytes | None, image_name: str | None,
        filters: dict, order: str, encode: Callable[[], object],
    ) -> tuple[dict, np.ndarray, bool]:
        kind = "image" if image is not None else "text"
        query = query.strip()
        signature = json.dumps([
            "query-v1", self.library_id, self.model_id, self.dimensions, kind, query,
            hashlib.sha256(image).hexdigest() if image is not None else None,
        ], ensure_ascii=False, separators=(",", ":"))
        identifier = hashlib.sha256(signature.encode()).hexdigest()
        # Coalesce simultaneous submissions before the paid encoder is called.
        with self.lock, self._connect() as db:
            row = db.execute("SELECT * FROM searches WHERE id=?", (identifier,)).fetchone()
            cached = row is not None
            if row is None:
                vector = self._vector(encode())
                now = datetime.now(UTC).isoformat()
                db.execute(
                    "INSERT INTO searches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (identifier, self.library_id, self.model_id, self.dimensions, kind, query,
                     image_name, image, vector.tobytes(), json.dumps(filters), order, now, now),
                )
            else:
                vector = self._vector(np.frombuffer(row["vector"], dtype="<f4"))
            self._touch(db, identifier, filters, order)
            if image_name:
                db.execute("UPDATE searches SET image_name=? WHERE id=?", (image_name, identifier))
            db.execute(
                "DELETE FROM searches WHERE library_id=? AND id NOT IN "
                "(SELECT id FROM searches WHERE library_id=? ORDER BY used_at DESC LIMIT 100)",
                (self.library_id, self.library_id),
            )
            row = db.execute("SELECT * FROM searches WHERE id=?", (identifier,)).fetchone()
            return self._public(row), vector, cached

    def replay(self, identifier: str, *, filters: dict, order: str) -> tuple[dict, np.ndarray]:
        with self.lock, self._connect() as db:
            row = self._row(db, identifier)
            if row["model_id"] != self.model_id or row["dimensions"] != self.dimensions:
                raise ValueError(
                    "This saved search uses a different model. Submit a new search to update it"
                )
            vector = self._vector(np.frombuffer(row["vector"], dtype="<f4"))
            self._touch(db, identifier, filters, order)
            row = self._row(db, identifier)
            return self._public(row), vector

    def list(self) -> list[dict]:
        with self.lock, self._connect() as db:
            return [self._public(row) for row in db.execute(
                "SELECT * FROM searches WHERE library_id=? ORDER BY used_at DESC LIMIT 100",
                (self.library_id,),
            )]

    def image(self, identifier: str) -> bytes:
        with self.lock, self._connect() as db:
            row = self._row(db, identifier)
            if row["image"] is None:
                raise KeyError("This saved search has no reference image")
            return row["image"]

    def delete(self, identifier: str) -> None:
        with self.lock, self._connect() as db:
            self._row(db, identifier)
            db.execute("DELETE FROM searches WHERE id=?", (identifier,))

    def _row(self, db: sqlite3.Connection, identifier: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM searches WHERE id=? AND library_id=?", (identifier, self.library_id)
        ).fetchone()
        if row is None:
            raise KeyError("Saved search is no longer available. No new API request was made")
        return row

    @staticmethod
    def _touch(db: sqlite3.Connection, identifier: str, filters: dict, order: str) -> None:
        db.execute(
            "UPDATE searches SET filters_json=?, result_order=?, used_at=? WHERE id=?",
            (json.dumps(filters), order, datetime.now(UTC).isoformat(), identifier),
        )

    def _vector(self, value: object) -> np.ndarray:
        vector = np.asarray(value, dtype="<f4")
        if vector.shape != (self.dimensions,) or not np.isfinite(vector).all():
            raise ValueError("Query vector is invalid for the current search index")
        if float(np.linalg.vector_norm(vector)) <= 0:
            raise ValueError("Query vector has zero length")
        return vector

    def _public(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"], "kind": row["kind"], "query": row["query"],
            "image_name": row["image_name"], "filters": json.loads(row["filters_json"]),
            "order": row["result_order"], "used_at": row["used_at"],
            "reusable": row["model_id"] == self.model_id and row["dimensions"] == self.dimensions,
            "image_url": f"/api/search/history/{row['id']}/image" if row["image"] else None,
        }
