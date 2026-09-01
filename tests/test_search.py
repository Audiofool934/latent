from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from latent.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingAsset,
    EmbeddingStore,
    serialize_float16_vector,
)
from latent.search import SemanticSearch, VectorIndex


class FakeTextEncoder:
    model_id = DEFAULT_EMBEDDING_MODEL

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.queries: list[str] = []

    def encode_texts(self, texts: list[str]) -> list[list[float]]:
        self.queries.extend(texts)
        return [self.vector for _text in texts]


def _asset(asset_id: int) -> EmbeddingAsset:
    return EmbeddingAsset(
        asset_id=asset_id,
        provider="test",
        remote_path=f"/archive/DSC{asset_id:05d}.ARW",
        fingerprint=f"fingerprint-{asset_id}",
        capture_at=f"2026:01:0{asset_id} 12:00:00",
        contact_relative_path=f"contact/{asset_id}.jpg",
    )


def _seed_vectors(
    embedding_dir: Path,
) -> tuple[list[EmbeddingAsset], dict[int, list[float]]]:
    assets = [_asset(asset_id) for asset_id in (1, 2, 3)]
    vectors = {
        1: [1.0, 0.0, 0.0, 0.0],
        2: [0.0, 1.0, 0.0, 0.0],
        3: [0.8, 0.2, 0.0, 0.0],
    }
    with EmbeddingStore(embedding_dir, dimensions=4) as store:
        store.sync_assets(assets)
        jobs = store.claim_jobs(3)
        store.finish_jobs(
            [(job, serialize_float16_vector(vectors[job.asset_id], 4)) for job in jobs]
        )
    return assets, vectors


def test_vector_index_searches_and_excludes_the_source_asset(tmp_path: Path) -> None:
    embedding_dir = tmp_path / "embeddings"
    _seed_vectors(embedding_dir)
    index = VectorIndex(embedding_dir, dimensions=4)

    matches = index.search_vector([1.0, 0.0, 0.0, 0.0], limit=3)
    similar = index.similar(1, limit=2)

    assert [match.asset_id for match in matches] == [1, 3, 2]
    assert matches[0].score == pytest.approx(1.0)
    assert matches[1].score == pytest.approx(0.9701, abs=0.001)
    assert [match.asset_id for match in similar] == [3, 2]
    assert all(match.asset_id != 1 for match in similar)


def test_vector_index_refreshes_after_a_fingerprint_update(tmp_path: Path) -> None:
    embedding_dir = tmp_path / "embeddings"
    assets, _vectors = _seed_vectors(embedding_dir)
    index = VectorIndex(embedding_dir, dimensions=4)
    assert [match.asset_id for match in index.similar(1, limit=2)] == [3, 2]

    changed = replace(assets[2], fingerprint="changed")
    with EmbeddingStore(embedding_dir, dimensions=4) as store:
        sync = store.sync_assets([assets[0], assets[1], changed])
        assert sync.enqueued == 1
        job = store.claim_jobs(1)[0]
        assert job.asset_id == 3
        store.finish_jobs([(job, serialize_float16_vector([0.0, 1.0, 0.0, 0.0], 4))])

    refreshed = index.similar(1, limit=2)
    assert [match.asset_id for match in refreshed] == [2, 3]
    assert all(match.score == pytest.approx(0.0) for match in refreshed)


def test_vector_index_refreshes_after_an_asset_id_remap(tmp_path: Path) -> None:
    embedding_dir = tmp_path / "embeddings"
    assets, _vectors = _seed_vectors(embedding_dir)
    index = VectorIndex(embedding_dir, dimensions=4)
    assert [match.asset_id for match in index.search_vector([0.8, 0.2, 0.0, 0.0])] == [
        3,
        1,
        2,
    ]

    remapped = replace(assets[2], asset_id=30)
    with EmbeddingStore(embedding_dir, dimensions=4) as store:
        sync = store.sync_assets([assets[0], assets[1], remapped])
        assert sync.enqueued == 0
        assert sync.unchanged == 3

    refreshed = index.search_vector([0.8, 0.2, 0.0, 0.0])
    assert [match.asset_id for match in refreshed] == [30, 1, 2]


def test_semantic_search_encodes_one_normalized_query(tmp_path: Path) -> None:
    embedding_dir = tmp_path / "embeddings"
    _seed_vectors(embedding_dir)
    encoder = FakeTextEncoder([1.0, 0.0, 0.0, 0.0])
    search = SemanticSearch(
        VectorIndex(embedding_dir, dimensions=4),
        encoder,
    )

    matches = search.search("  blue snow  ", limit=2)

    assert encoder.queries == ["blue snow"]
    assert [match.asset_id for match in matches] == [1, 3]


def test_vector_and_semantic_queries_reject_invalid_inputs(tmp_path: Path) -> None:
    embedding_dir = tmp_path / "embeddings"
    _seed_vectors(embedding_dir)
    index = VectorIndex(embedding_dir, dimensions=4)

    with pytest.raises(ValueError, match="shape"):
        index.search_vector([1.0], limit=2)
    with pytest.raises(ValueError, match="zero length"):
        index.search_vector([0.0, 0.0, 0.0, 0.0], limit=2)
    with pytest.raises(ValueError, match="between 1 and 500"):
        index.search_vector([1.0, 0.0, 0.0, 0.0], limit=501)
    with pytest.raises(ValueError, match="cannot be empty"):
        SemanticSearch(index, FakeTextEncoder([1.0, 0.0, 0.0, 0.0])).search(" ")
