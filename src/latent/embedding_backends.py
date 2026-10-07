"""Embedding backends, their separate vector spaces, and the explicit active selection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

from .embeddinggemma import (
    EMBEDDINGGEMMA_DIMENSIONS,
    EMBEDDINGGEMMA_MODEL,
    ESTIMATED_IMAGES_PER_SECOND,
    INDEX_METADATA,
)
from .embeddings import EmbeddingStore
from .errors import ConfigurationError
from .gemini import GEMINI_DIMENSIONS, GEMINI_IMAGE_ESTIMATE_USD, GEMINI_MODEL, GEMINI_STORE_NAME
from .workspace import utc_now, write_workspace_export

GEMINI = "gemini"
EMBEDDINGGEMMA = "embeddinggemma"
SELECTION_NAME = "active-backend.json"
VALIDATION_NAME = "validation.json"


@dataclass(frozen=True)
class EmbeddingProfile:
    key: str
    display_name: str
    model_id: str
    dimensions: int
    store_name: str
    provider: str
    metadata: Mapping[str, str] = field(default_factory=dict)
    image_cost_usd: float = 0.0
    images_per_second: float | None = None
    image_text_queries: bool = True
    sends_previews: bool = False
    requires_validation: bool = False
    # Photos per encoder call; pausing takes effect between calls.
    batch_size: int = 8

    @property
    def space_id(self) -> str:
        """Identifies one vector space; caches and saved queries never cross it."""
        if not self.metadata:
            return self.model_id
        encoded = json.dumps(sorted(self.metadata.items()), separators=(",", ":"))
        return f"{self.model_id}@{hashlib.sha256(encoded.encode()).hexdigest()[:16]}"

    def public(self) -> dict[str, object]:
        return {
            "key": self.key, "display_name": self.display_name, "model_id": self.model_id,
            "dimensions": self.dimensions, "space_id": self.space_id, "provider": self.provider,
            "local": not self.sends_previews, "sends_previews": self.sends_previews,
            "image_text_queries": self.image_text_queries,
            "requires_validation": self.requires_validation,
            "image_cost_usd": self.image_cost_usd,
            "images_per_second": self.images_per_second,
        }


GEMINI_PROFILE = EmbeddingProfile(
    key=GEMINI, display_name="Gemini Embedding 2", model_id=GEMINI_MODEL,
    dimensions=GEMINI_DIMENSIONS, store_name=GEMINI_STORE_NAME, provider="gemini_api",
    image_cost_usd=GEMINI_IMAGE_ESTIMATE_USD, sends_previews=True,
)
EMBEDDINGGEMMA_PROFILE = EmbeddingProfile(
    key=EMBEDDINGGEMMA, display_name="EmbeddingGemma 2 (on this Mac)",
    model_id=EMBEDDINGGEMMA_MODEL, dimensions=EMBEDDINGGEMMA_DIMENSIONS,
    store_name=f"{EMBEDDINGGEMMA_MODEL}-q8_0-{INDEX_METADATA['artifact_revision'][:12]}",
    provider="local_llama_cpp", metadata=INDEX_METADATA,
    images_per_second=ESTIMATED_IMAGES_PER_SECOND, image_text_queries=False,
    requires_validation=True, batch_size=1,
)
PROFILES = {profile.key: profile for profile in (GEMINI_PROFILE, EMBEDDINGGEMMA_PROFILE)}


def profile(key: str) -> EmbeddingProfile:
    try:
        return PROFILES[key]
    except KeyError:
        raise ValueError(f"Unknown search engine: {key}") from None


@dataclass
class EmbeddingEngine:
    """One backend bound to its own index directory and encoder."""

    profile: EmbeddingProfile
    embedding_dir: Path
    encoder_factory: Callable[[], Any]
    # Raises a user-facing error when the backend cannot start encoding now.
    preflight: Callable[[], None] | None = None

    def store(self) -> EmbeddingStore:
        return EmbeddingStore(
            self.embedding_dir, model_id=self.profile.model_id,
            dimensions=self.profile.dimensions, metadata=self.profile.metadata,
        )

    def has_index(self) -> bool:
        return (self.embedding_dir / "index.sqlite").is_file()


def engine_dirs(gemini_dir: Path) -> dict[str, Path]:
    """The configured Gemini index anchors library identity; other indexes sit beside it."""
    root = gemini_dir.parent
    return {GEMINI: gemini_dir, EMBEDDINGGEMMA: root / EMBEDDINGGEMMA_PROFILE.store_name}


class BackendSelection:
    """The active search engine changes only through an explicit activation."""

    def __init__(self, embeddings_root: Path) -> None:
        self.path = embeddings_root / SELECTION_NAME

    def read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"backend": GEMINI, "activated_at": None, "validation_id": None}
        try:
            data = json.loads(self.path.read_text())
            profile(data["backend"])
            return data
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ConfigurationError(
                f"The search engine selection is unreadable: {self.path}"
            ) from error

    def stamp(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def activate(self, engine: EmbeddingEngine) -> dict[str, Any]:
        validation_id = None
        if engine.profile.requires_validation:
            report = current_validation(engine)
            if report is None or not report["passed"]:
                raise ValueError(
                    f"Validate the {engine.profile.display_name} index before using it"
                )
            validation_id = report["id"]
        elif not engine.has_index():
            raise ConfigurationError(f"{engine.profile.display_name} has no index to search")
        selection = {
            "backend": engine.profile.key, "space_id": engine.profile.space_id,
            "activated_at": utc_now(), "validation_id": validation_id,
        }
        write_workspace_export(self.path, selection)
        return selection


def index_revision(engine: EmbeddingEngine) -> list:
    with engine.store() as store:
        return list(store.vector_revision())


def current_validation(engine: EmbeddingEngine) -> dict[str, Any] | None:
    """A report counts only for the exact index contents it checked."""
    path = engine.embedding_dir / VALIDATION_NAME
    if not path.is_file() or not engine.has_index():
        return None
    report = json.loads(path.read_text())
    if report.get("space_id") != engine.profile.space_id:
        return None
    if report.get("index_revision") != index_revision(engine):
        return {**report, "passed": False, "stale": True}
    return report


def validate_index(
    engine: EmbeddingEngine,
    state_dir: Path,
    encoder: Any,
    *,
    sample_size: int = 12,
) -> dict[str, Any]:
    """Check one index generation end to end before it may be activated."""
    checks: list[dict[str, object]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    if not engine.has_index():
        raise ConfigurationError(f"{engine.profile.display_name} has no index yet")
    with engine.store() as store:  # raises on any model, dimension, or metadata mismatch
        integrity = str(store.connection.execute("PRAGMA integrity_check").fetchone()[0])
        rows = store.vector_rows()
        revision = list(store.vector_revision())
    check("configuration", True, f"{engine.profile.space_id}, {engine.profile.dimensions}D")
    check("integrity", integrity == "ok", f"SQLite integrity: {integrity}")

    ids = np.asarray([int(row["asset_id"]) for row in rows], dtype=np.int64)
    dimensions = engine.profile.dimensions
    matrix = (
        np.vstack([np.frombuffer(row["vector"], dtype="<f2", count=dimensions) for row in rows])
        .astype(np.float32) if rows else np.empty((0, dimensions), dtype=np.float32)
    )
    norms = np.linalg.vector_norm(matrix, axis=1) if rows else np.empty(0)
    finite = bool(np.isfinite(matrix).all())
    unit = bool(rows) and finite and bool(np.all(np.abs(norms - 1) < 0.02))
    check("vectors", unit, f"{len(rows)} vectors, {'all' if finite else 'not all'} finite, "
          f"norm range {norms.min():.4f}-{norms.max():.4f}" if rows else "no vectors")
    library = _visible_asset_count(state_dir)
    check("coverage", len(rows) > 0, f"{len(rows)} of {library} library photos indexed")

    first = np.asarray(encoder.encode_texts(["latent validation query"])[0], dtype=np.float32)
    again = np.asarray(encoder.encode_texts(["latent validation query"])[0], dtype=np.float32)
    check(
        "text_query",
        first.shape == (dimensions,) and np.isfinite(first).all() and
        float(first @ again) > 0.999,
        f"{first.shape[0]}D query vector, repeat cosine {float(first @ again):.6f}",
    )

    if rows and unit:
        unit_matrix = matrix / norms[:, None]
        positions = np.unique(np.linspace(0, len(rows) - 1, min(sample_size, len(rows)))
                              .round().astype(int))
        contact_root = (state_dir / "cache").resolve()
        failures = []
        for position in positions.tolist():
            row = rows[position]
            relative = _contact_path(state_dir, int(row["asset_id"]))
            data = (contact_root / relative).read_bytes()
            query = np.asarray(encoder.encode_image_query(data), dtype=np.float32)
            scores = unit_matrix @ query
            own = float(scores[position])
            rank = int(np.sum(scores > own)) + 1
            if own < 0.98 or rank > 3:
                failures.append(f"photo {int(ids[position])}: rank {rank}, cosine {own:.4f}")
        check(
            "self_retrieval", not failures,
            f"{len(positions) - len(failures)} of {len(positions)} indexed photos re-encoded "
            "from their contact JPEGs retrieved themselves"
            + (f"; {'; '.join(failures[:3])}" if failures else ""),
        )
    else:
        check("self_retrieval", False, "skipped because the stored vectors are invalid")

    report = {
        "id": str(uuid4()), "backend": engine.profile.key, "space_id": engine.profile.space_id,
        "created_at": utc_now(), "index_revision": revision, "indexed_assets": len(rows),
        "library_assets": library, "checks": checks,
        "passed": all(item["passed"] for item in checks),
    }
    write_workspace_export(engine.embedding_dir / VALIDATION_NAME, report)
    return report


def _visible_asset_count(state_dir: Path) -> int:
    db = sqlite3.connect((state_dir / "index.sqlite").as_uri() + "?mode=ro", uri=True)
    try:
        return int(db.execute(
            "SELECT COUNT(*) FROM assets a WHERE NOT EXISTS (SELECT 1 FROM hidden_assets h "
            "WHERE h.provider=a.provider AND h.remote_path=a.remote_path)"
        ).fetchone()[0])
    finally:
        db.close()


def _contact_path(state_dir: Path, asset_id: int) -> str:
    db = sqlite3.connect((state_dir / "index.sqlite").as_uri() + "?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT c.relative_path FROM cache_entries c JOIN assets a ON a.id=c.asset_id "
            "WHERE c.asset_id=? AND c.variant='contact' AND c.fingerprint=a.fingerprint",
            (asset_id,),
        ).fetchone()
    finally:
        db.close()
    if row is None:
        raise ConfigurationError(f"Contact preview for photo {asset_id} is missing")
    path = Path(str(row[0]))
    if path.is_absolute() or ".." in path.parts:
        raise ConfigurationError(f"invalid contact cache path for photo {asset_id}")
    return str(path)
