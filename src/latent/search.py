"""In-memory cosine search over the durable local embedding store."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .embeddings import (
    DEFAULT_EMBEDDING_DIMENSIONS,
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingStore,
)
from .errors import ConfigurationError


@dataclass(frozen=True)
class VectorMatch:
    asset_id: int
    rank: int
    score: float

    def as_dict(self) -> dict[str, int | float]:
        return {"asset_id": self.asset_id, "rank": self.rank, "score": self.score}


@dataclass(frozen=True)
class VisualMotifCluster:
    member_asset_ids: tuple[int, ...]
    centroid_similarities: tuple[float, ...]
    cohesion: float

    @property
    def representative_asset_id(self) -> int:
        return self.member_asset_ids[0]


class TextEmbeddingEncoder(Protocol):
    model_id: str

    def encode_texts(self, texts: Sequence[str]) -> Any: ...

    def encode_image_query(self, data: bytes, query: str = "") -> Sequence[float]: ...


class VectorIndex:
    """Caches normalized float32 vectors and refreshes after store updates."""

    def __init__(
        self,
        embedding_dir: Path,
        *,
        model_id: str = DEFAULT_EMBEDDING_MODEL,
        dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
    ) -> None:
        self.embedding_dir = embedding_dir.expanduser().resolve()
        self.model_id = model_id
        self.dimensions = dimensions
        self._lock = threading.RLock()
        self._revision: tuple[int, str, str, int, int] | None = None
        self._asset_ids = np.empty((0,), dtype=np.int64)
        self._matrix = np.empty((0, dimensions), dtype=np.float32)
        self._asset_positions: dict[int, int] = {}
        self._motif_cache: dict[
            tuple[tuple[int, str, str, int, int], int, int],
            tuple[VisualMotifCluster, ...],
        ] = {}

    @property
    def size(self) -> int:
        self.refresh()
        return int(self._asset_ids.size)

    def refresh(self, *, force: bool = False) -> bool:
        database_path = self.embedding_dir / "index.sqlite"
        if not database_path.is_file():
            raise ConfigurationError(f"embedding store was not found: {database_path}")
        with (
            self._lock,
            EmbeddingStore(
                self.embedding_dir,
                model_id=self.model_id,
                dimensions=self.dimensions,
            ) as store,
        ):
            revision = store.vector_revision()
            if not force and revision == self._revision:
                return False
            rows = store.vector_rows()

        asset_ids = np.asarray([int(row["asset_id"]) for row in rows], dtype=np.int64)
        if rows:
            matrix = np.vstack(
                [
                    np.frombuffer(row["vector"], dtype="<f2", count=self.dimensions).astype(
                        np.float32
                    )
                    for row in rows
                ]
            )
            if matrix.shape != (len(rows), self.dimensions):
                raise ConfigurationError(
                    f"embedding matrix shape {matrix.shape} does not match "
                    f"({len(rows)}, {self.dimensions})"
                )
            if not np.isfinite(matrix).all():
                raise ConfigurationError("embedding store contains a non-finite vector")
            norms = np.linalg.vector_norm(matrix, axis=1, keepdims=True)
            if np.any(norms <= 0):
                raise ConfigurationError("embedding store contains a zero-length vector")
            matrix = matrix / norms
        else:
            matrix = np.empty((0, self.dimensions), dtype=np.float32)

        with self._lock:
            self._asset_ids = asset_ids
            self._matrix = matrix
            self._asset_positions = {
                int(asset_id): position for position, asset_id in enumerate(asset_ids.tolist())
            }
            self._revision = revision
            self._motif_cache.clear()
        return True

    def search_vector(
        self,
        vector: Sequence[float] | Any,
        *,
        limit: int = 50,
        exclude_asset_ids: Sequence[int] = (),
        variety: bool = False,
        order: str = "closest",
        allowed_asset_ids: Sequence[int] | None = None,
    ) -> list[VectorMatch]:
        if order not in {"closest", "least_similar", "variety"}:
            raise ValueError("order must be closest, least_similar, or variety")
        if variety and order == "least_similar":
            raise ValueError("variety cannot be combined with least_similar")
        if limit < 1 or limit > 500:
            raise ValueError("search limit must be between 1 and 500")
        self.refresh()
        query = np.asarray(vector, dtype=np.float32)
        if query.shape != (self.dimensions,):
            raise ValueError(
                f"query embedding shape {query.shape} does not match ({self.dimensions},)"
            )
        if not np.isfinite(query).all():
            raise ValueError("query embedding contains a non-finite value")
        norm = float(np.linalg.vector_norm(query))
        if norm <= 0:
            raise ValueError("query embedding has zero length")
        query = query / norm

        with self._lock:
            asset_ids = self._asset_ids
            matrix = self._matrix
            if asset_ids.size == 0:
                return []
            scores = matrix @ query
            if allowed_asset_ids is not None:
                scores[~np.isin(asset_ids, np.asarray(allowed_asset_ids, dtype=np.int64))] = np.nan
            if exclude_asset_ids:
                excluded = np.isin(asset_ids, np.asarray(exclude_asset_ids, dtype=np.int64))
                scores = scores.copy()
                scores[excluded] = np.nan
            ranking = np.argsort(scores if order == "least_similar" else -scores, kind="stable")
            if variety or order == "variety":
                # Bound reranking to the 500 closest candidates, regardless of page size.
                # This keeps the first results stable when callers request different limits.
                candidates = ranking[np.isfinite(scores[ranking])][:500]
                ranking = _varied_ranking(matrix, scores, candidates, limit)
            matches = []
            for position in ranking.tolist():
                score = float(scores[position])
                if not np.isfinite(score):
                    continue
                matches.append(
                    VectorMatch(
                        asset_id=int(asset_ids[position]),
                        rank=len(matches) + 1,
                        score=score,
                    )
                )
                if len(matches) >= limit:
                    break
            return matches

    def similar(
        self,
        asset_id: int,
        *,
        limit: int = 50,
        order: str = "closest",
        allowed_asset_ids: Sequence[int] | None = None,
    ) -> list[VectorMatch]:
        self.refresh()
        with self._lock:
            position = self._asset_positions.get(asset_id)
            if position is None:
                raise KeyError(f"asset {asset_id} does not have a current embedding")
            vector = self._matrix[position].copy()
        return self.search_vector(
            vector,
            limit=limit,
            exclude_asset_ids=(asset_id,),
            order=order,
            allowed_asset_ids=allowed_asset_ids,
        )

    def motif_clusters(
        self,
        *,
        cluster_count: int = 12,
        iterations: int = 6,
    ) -> list[VisualMotifCluster]:
        """Build deterministic spherical clusters over current local image vectors."""
        if cluster_count < 1 or cluster_count > 50:
            raise ValueError("motif cluster count must be between 1 and 50")
        if iterations < 1 or iterations > 20:
            raise ValueError("motif iterations must be between 1 and 20")
        self.refresh()
        with self._lock:
            if self._asset_ids.size == 0 or self._revision is None:
                return []
            effective_count = min(cluster_count, int(self._asset_ids.size))
            cache_key = (self._revision, effective_count, iterations)
            cached = self._motif_cache.get(cache_key)
            if cached is not None:
                return list(cached)
            asset_ids = self._asset_ids
            matrix = self._matrix

        centers = _initial_motif_centers(matrix, effective_count)
        assignments: np.ndarray | None = None
        for _iteration in range(iterations):
            next_assignments = np.argmax(matrix @ centers.T, axis=1)
            if assignments is not None and np.array_equal(next_assignments, assignments):
                break
            assignments = next_assignments
            for cluster_index in range(effective_count):
                members = matrix[assignments == cluster_index]
                if members.size == 0:
                    continue
                center = members.mean(axis=0)
                norm = float(np.linalg.vector_norm(center))
                if norm > 0:
                    centers[cluster_index] = center / norm

        scores = matrix @ centers.T
        assignments = np.argmax(scores, axis=1)
        clusters = []
        for cluster_index in range(effective_count):
            positions = np.flatnonzero(assignments == cluster_index)
            if positions.size == 0:
                continue
            similarities = scores[positions, cluster_index]
            order = np.lexsort((asset_ids[positions], -similarities))
            ordered_positions = positions[order]
            ordered_scores = similarities[order]
            clusters.append(
                VisualMotifCluster(
                    member_asset_ids=tuple(int(value) for value in asset_ids[ordered_positions]),
                    centroid_similarities=tuple(
                        max(-1.0, min(1.0, float(value))) for value in ordered_scores
                    ),
                    cohesion=max(-1.0, min(1.0, float(np.mean(ordered_scores)))),
                )
            )
        clusters.sort(
            key=lambda cluster: (
                -len(cluster.member_asset_ids),
                -cluster.cohesion,
                cluster.representative_asset_id,
            )
        )
        result = tuple(clusters)
        with self._lock:
            if self._revision == cache_key[0]:
                self._motif_cache[cache_key] = result
        return list(result)


class SemanticSearch:
    def __init__(self, vector_index: VectorIndex, encoder: TextEmbeddingEncoder) -> None:
        if encoder.model_id != vector_index.model_id:
            raise ConfigurationError(
                f"text encoder model {encoder.model_id!r} does not match "
                f"vector index {vector_index.model_id!r}"
            )
        self.vector_index = vector_index
        self.encoder = encoder

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        variety: bool = False,
        order: str = "closest",
        allowed_asset_ids: Sequence[int] | None = None,
    ) -> list[VectorMatch]:
        vector = self.text_vector(query)
        return self.vector_index.search_vector(
            vector, limit=limit, variety=variety, order=order,
            allowed_asset_ids=allowed_asset_ids,
        )

    def text_vector(self, query: str) -> Any:
        normalized = query.strip()
        if not normalized:
            raise ValueError("semantic query cannot be empty")
        if len(normalized) > 200:
            raise ValueError("semantic query must be 200 characters or fewer")
        vectors = self.encoder.encode_texts([normalized])
        if len(vectors) != 1:
            raise RuntimeError(f"text encoder returned {len(vectors)} vectors for one query")
        return vectors[0]

    def search_image(
        self,
        data: bytes,
        *,
        query: str = "",
        limit: int = 50,
        order: str = "closest",
        allowed_asset_ids: Sequence[int] | None = None,
    ) -> list[VectorMatch]:
        vector = self.encoder.encode_image_query(data, query)
        return self.vector_index.search_vector(
            vector, limit=limit, order=order, allowed_asset_ids=allowed_asset_ids
        )


def _varied_ranking(
    matrix: np.ndarray, scores: np.ndarray, candidates: np.ndarray, limit: int
) -> np.ndarray:
    """Greedy relevance/diversity balance; keep original cosine scores for display."""
    if candidates.size == 0:
        return candidates
    vectors = matrix[candidates]
    relevance = scores[candidates]
    selected = [0]  # Always retain the closest match first.
    available = np.ones(len(candidates), dtype=bool)
    available[0] = False
    nearest_selected = np.full(len(candidates), -1.0, dtype=np.float32)
    while len(selected) < min(limit, len(candidates)):
        nearest_selected = np.maximum(nearest_selected, vectors @ vectors[selected[-1]])
        priorities = 0.8 * relevance - 0.2 * nearest_selected
        priorities[~available] = -np.inf
        chosen = int(np.argmax(priorities))
        selected.append(chosen)
        available[chosen] = False
    return candidates[np.asarray(selected)]


def _initial_motif_centers(matrix: np.ndarray, count: int) -> np.ndarray:
    mean = matrix.mean(axis=0)
    norm = float(np.linalg.vector_norm(mean))
    first = 0 if norm <= 0 else int(np.argmax(matrix @ (mean / norm)))
    selected = [first]
    while len(selected) < count:
        nearest_similarity = np.max(matrix @ matrix[selected].T, axis=1)
        nearest_similarity[np.asarray(selected, dtype=np.int64)] = np.inf
        selected.append(int(np.argmin(nearest_similarity)))
    return matrix[np.asarray(selected, dtype=np.int64)].copy()
