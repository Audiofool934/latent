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


def test_vector_index_builds_deterministic_spherical_motif_clusters(tmp_path: Path) -> None:
    embedding_dir = tmp_path / "embeddings"
    vectors = {
        1: [1.0, 0.0, 0.0, 0.0],
        2: [0.98, 0.10, 0.0, 0.0],
        3: [0.96, -0.08, 0.0, 0.0],
        4: [0.99, 0.03, 0.0, 0.0],
        5: [0.0, 1.0, 0.0, 0.0],
        6: [0.10, 0.98, 0.0, 0.0],
        7: [-0.08, 0.96, 0.0, 0.0],
        8: [0.03, 0.99, 0.0, 0.0],
    }
    with EmbeddingStore(embedding_dir, dimensions=4) as store:
        store.sync_assets([_asset(asset_id) for asset_id in vectors])
        jobs = store.claim_jobs(len(vectors))
        store.finish_jobs(
            [(job, serialize_float16_vector(vectors[job.asset_id], 4)) for job in jobs]
        )

    index = VectorIndex(embedding_dir, dimensions=4)
    first = index.motif_clusters(cluster_count=2)
    second = index.motif_clusters(cluster_count=2)

    assert first == second
    assert len(first) == 2
    assert {frozenset(cluster.member_asset_ids) for cluster in first} == {
        frozenset({1, 2, 3, 4}),
        frozenset({5, 6, 7, 8}),
    }
    assert all(cluster.cohesion > 0.99 for cluster in first)
    assert all(
        list(cluster.centroid_similarities) == sorted(cluster.centroid_similarities, reverse=True)
        for cluster in first
    )

    with EmbeddingStore(embedding_dir, dimensions=4) as store:
        added = store.sync_assets([_asset(asset_id) for asset_id in (*vectors, 9)])
        assert added.enqueued == 1
        job = store.claim_jobs(1)[0]
        assert job.asset_id == 9
        store.finish_jobs([(job, serialize_float16_vector([0.97, 0.04, 0.0, 0.0], 4))])

    refreshed = index.motif_clusters(cluster_count=2)
    assert sum(len(cluster.member_asset_ids) for cluster in refreshed) == 9
    assert any(9 in cluster.member_asset_ids for cluster in refreshed)


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


def test_variety_promotes_a_distinct_frame_without_changing_cosine_scores(tmp_path: Path) -> None:
    vectors = {
        1: [0.8, 0.6, 0.0, 0.0],
        2: [0.8, 0.6, 0.0, 0.0],  # Another frame from the same burst.
        3: [0.79, 0.0, 0.61, 0.0],  # A similarly relevant but visually different frame.
        4: [-1.0, 0.0, 0.0, 0.0],
    }
    with EmbeddingStore(tmp_path, dimensions=4) as store:
        store.sync_assets([_asset(i) for i in vectors])
        store.finish_jobs(
            [
                (job, serialize_float16_vector(vectors[job.asset_id], 4))
                for job in store.claim_jobs(4)
            ]
        )
    index = VectorIndex(tmp_path, dimensions=4)
    search = SemanticSearch(index, FakeTextEncoder([1.0, 0.0, 0.0, 0.0]))
    closest = search.search("winter landscape", limit=3)
    varied = search.search("winter landscape", limit=3, variety=True)
    assert [m.asset_id for m in closest] == [1, 2, 3]
    assert [m.asset_id for m in varied] == [1, 3, 2]
    assert [m.rank for m in varied] == [1, 2, 3]
    assert {m.asset_id: m.score for m in varied} == {m.asset_id: m.score for m in closest}
    assert search.search("winter landscape", limit=2, variety=True) == varied[:2]
    assert search.search("winter landscape", limit=3, variety=True) == varied
    assert len(search.search("winter landscape", limit=50, variety=True)) == 4
    assert index.search_vector([1, 0, 0, 0], variety=True, exclude_asset_ids=(1, 2, 3, 4)) == []
    assert [
        m.asset_id
        for m in index.search_vector([1, 0, 0, 0], variety=True, exclude_asset_ids=(1, 2, 4))
    ] == [3]


def test_variety_stays_within_the_closest_candidate_pool(tmp_path: Path) -> None:
    with EmbeddingStore(tmp_path, dimensions=4) as store:
        store.sync_assets([_asset(i) for i in range(1, 502)])
        store.finish_jobs(
            [
                (
                    job,
                    serialize_float16_vector(
                        [0.8, 0.6, 0, 0] if job.asset_id <= 500 else [0.79, 0, 0.61, 0], 4
                    ),
                )
                for job in store.claim_jobs(501)
            ]
        )
    index = VectorIndex(tmp_path, dimensions=4)
    matches = index.search_vector([1, 0, 0, 0], limit=4, variety=True)
    assert [m.asset_id for m in matches] == [1, 2, 3, 4]


def test_least_similar_ranks_the_entire_index_and_excludes_the_reference(tmp_path: Path) -> None:
    _seed_vectors(tmp_path)
    index = VectorIndex(tmp_path, dimensions=4)
    closest = index.search_vector([1, 0, 0, 0], limit=2)
    least = index.search_vector([1, 0, 0, 0], limit=2, order="least_similar")
    assert [m.asset_id for m in closest] == [1, 3]
    assert [m.asset_id for m in least] == [2, 3]
    assert [m.asset_id for m in index.similar(1, order="least_similar")] == [2, 3]
    assert least[0].score < least[1].score
