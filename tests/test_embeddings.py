from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from latent.cli import build_parser, main
from latent.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingAsset,
    EmbeddingStore,
    EmbeddingWorker,
    load_embedding_assets,
    serialize_float16_vector,
)
from latent.errors import ConfigurationError
from latent.models import RemoteAsset
from latent.storage import CacheManager, StateStore


class FakeEncoder:
    model_id = DEFAULT_EMBEDDING_MODEL

    def __init__(
        self,
        *,
        fail_batches: bool = False,
        fail_names: set[str] | None = None,
    ) -> None:
        self.fail_batches = fail_batches
        self.fail_names = set() if fail_names is None else set(fail_names)
        self.calls: list[list[str]] = []

    def encode_images(self, paths: list[Path], batch_size: int) -> list[list[float]]:
        assert batch_size > 0
        self.calls.append([path.name for path in paths])
        if self.fail_batches and len(paths) > 1:
            raise RuntimeError("batch failed")
        if any(path.name in self.fail_names for path in paths):
            raise RuntimeError("bad contact image")
        return [[float(index), 0.25, -0.5, 1.0] for index, _path in enumerate(paths, start=1)]


def _asset(index: int, *, fingerprint: str | None = None) -> EmbeddingAsset:
    return EmbeddingAsset(
        asset_id=index,
        provider="test",
        remote_path=f"/archive/2026-01-0{index}/DSC{index:05d}.ARW",
        fingerprint=fingerprint or f"fingerprint-{index}",
        capture_at=f"2026:01:0{index} 12:00:00",
        contact_relative_path=f"contact/{index}.jpg",
    )


def _write_contacts(contact_root: Path, assets: list[EmbeddingAsset]) -> None:
    for asset in assets:
        path = contact_root / asset.contact_relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"contact-{asset.asset_id}".encode())


def test_embedding_store_sync_is_incremental_and_fingerprint_aware(tmp_path: Path) -> None:
    first = _asset(1)
    second = _asset(2)
    embedding_dir = tmp_path / "embeddings"

    with EmbeddingStore(embedding_dir, dimensions=4) as store:
        initial = store.sync_assets([first, second])
        assert initial.as_dict() == {
            "discovered": 2,
            "enqueued": 2,
            "unchanged": 0,
            "removed": 0,
            "pending_jobs": 2,
        }

        claimed = store.claim_jobs(1)
        assert len(claimed) == 1
        store.finish_jobs([(claimed[0], serialize_float16_vector([1, 0, 0, 0], 4))])
        assert store.status()["vectors"] == 1

        repeated = store.sync_assets([first, second])
        assert repeated.enqueued == 0
        assert repeated.unchanged == 2
        assert store.job_counts() == {
            "pending": 1,
            "running": 0,
            "succeeded": 1,
            "failed": 0,
        }

        completed = next(
            asset
            for asset in (first, second)
            if asset.identity == (claimed[0].provider, claimed[0].remote_path)
        )
        changed = replace(completed, fingerprint="changed-fingerprint")
        refreshed_assets = [
            changed if asset.identity == completed.identity else asset for asset in (first, second)
        ]
        refreshed = store.sync_assets(refreshed_assets)
        assert refreshed.enqueued == 1
        assert refreshed.unchanged == 1
        assert store.status()["vectors"] == 0

        pruned = store.sync_assets([changed])
        assert pruned.removed == 1
        assert store.job_counts()["pending"] == 1


def test_embedding_worker_resumes_without_reencoding_completed_vectors(tmp_path: Path) -> None:
    assets = [_asset(index) for index in (1, 2, 3)]
    contact_root = tmp_path / "cache"
    embedding_dir = tmp_path / "embeddings"
    _write_contacts(contact_root, assets)

    with EmbeddingStore(embedding_dir, dimensions=4) as store:
        store.sync_assets(assets)
        first = EmbeddingWorker(store, FakeEncoder(), contact_root).run(
            max_jobs=1,
            batch_size=1,
        )
        assert (first.processed, first.succeeded, first.failed) == (1, 1, 0)
        assert store.job_counts()["pending"] == 2

    with EmbeddingStore(embedding_dir, dimensions=4) as store:
        second = EmbeddingWorker(store, FakeEncoder(), contact_root).run(
            max_jobs=10,
            batch_size=2,
        )
        assert (second.processed, second.succeeded, second.failed) == (2, 2, 0)
        status = store.status()
        assert status["jobs"] == {
            "pending": 0,
            "running": 0,
            "succeeded": 3,
            "failed": 0,
        }
        assert status["vectors"] == 3
        assert status["vector_bytes"] == 3 * 4 * 2
        assert status["photo_network_bytes"] == 0
        assert status["archive_modified"] is False


def test_embedding_worker_isolates_one_bad_image_after_batch_failure(tmp_path: Path) -> None:
    assets = [_asset(index) for index in (1, 2, 3)]
    contact_root = tmp_path / "cache"
    _write_contacts(contact_root, assets)
    encoder = FakeEncoder(fail_batches=True, fail_names={"2.jpg"})

    with EmbeddingStore(tmp_path / "embeddings", dimensions=4) as store:
        store.sync_assets(assets)
        result = EmbeddingWorker(store, encoder, contact_root).run(
            max_jobs=3,
            batch_size=3,
        )

        assert (result.processed, result.succeeded, result.failed) == (3, 2, 1)
        assert result.batch_fallbacks == 1
        assert result.failures[0].remote_path == assets[1].remote_path
        assert "bad contact image" in result.failures[0].error
        assert store.job_counts() == {
            "pending": 0,
            "running": 0,
            "succeeded": 2,
            "failed": 1,
        }
        assert store.status()["vectors"] == 2


def test_running_embedding_jobs_can_be_recovered(tmp_path: Path) -> None:
    with EmbeddingStore(tmp_path / "embeddings", dimensions=4) as store:
        store.sync_assets([_asset(1)])
        assert len(store.claim_jobs(1)) == 1
        assert store.job_counts()["running"] == 1

        recovered = store.requeue_running_jobs(before="9999-01-01T00:00:00+00:00")

        assert recovered == 1
        assert store.job_counts()["pending"] == 1
        assert store.job_counts()["running"] == 0


def test_embedding_failure_circuit_breaker_releases_unprocessed_claims(
    tmp_path: Path,
) -> None:
    assets = [_asset(index) for index in (1, 2, 3, 4, 5, 6)]
    contact_root = tmp_path / "cache"
    _write_contacts(contact_root, assets)
    encoder = FakeEncoder(
        fail_batches=True,
        fail_names={f"{index}.jpg" for index in (1, 2, 3, 4, 5, 6)},
    )

    with EmbeddingStore(tmp_path / "embeddings", dimensions=4) as store:
        store.sync_assets(assets)
        result = EmbeddingWorker(store, encoder, contact_root).run(
            max_jobs=6,
            batch_size=6,
            max_consecutive_failures=5,
        )

        assert (result.processed, result.failed, result.succeeded) == (5, 5, 0)
        assert result.stopped_after_consecutive_failures is True
        assert store.job_counts() == {
            "pending": 1,
            "running": 0,
            "succeeded": 0,
            "failed": 5,
        }


def test_embedding_store_rejects_a_different_model_contract(tmp_path: Path) -> None:
    embedding_dir = tmp_path / "embeddings"
    with EmbeddingStore(embedding_dir, dimensions=4):
        pass

    with pytest.raises(ConfigurationError, match="configuration mismatch"):
        EmbeddingStore(embedding_dir, dimensions=8)


def test_library_embedding_source_requires_current_contact_fingerprint(tmp_path: Path) -> None:
    original = RemoteAsset(
        provider="test",
        remote_id="asset-1",
        remote_path="/archive/2026/2026-01-01/DSC00001.ARW",
        name="DSC00001.ARW",
        size_bytes=1000,
        write_time="2026-01-01T12:00:00+00:00",
        file_hashes={"2": "first"},
    )
    with StateStore(tmp_path) as store:
        asset_id = store.upsert_asset(original)
        CacheManager(
            store,
            {"contact": 10_000, "preview": 0, "temporary": 0},
        ).put(
            asset_id=asset_id,
            variant="contact",
            fingerprint=original.fingerprint,
            data=b"contact",
            width=4,
            height=3,
        )

    current = load_embedding_assets(tmp_path)
    assert len(current) == 1
    assert current[0].asset_id == asset_id
    assert current[0].contact_relative_path.startswith("contact/")

    changed = RemoteAsset(
        provider=original.provider,
        remote_id=original.remote_id,
        remote_path=original.remote_path,
        name=original.name,
        size_bytes=original.size_bytes,
        write_time="2026-01-02T12:00:00+00:00",
        file_hashes={"2": "changed"},
    )
    with StateStore(tmp_path) as store:
        store.upsert_asset(changed)

    assert load_embedding_assets(tmp_path) == []


def test_float16_serialization_rejects_wrong_shape_and_non_finite_values() -> None:
    assert len(serialize_float16_vector([1.0, 0.5, -0.25, 0.0], 4)) == 8
    with pytest.raises(ValueError, match="dimensions"):
        serialize_float16_vector([1.0], 4)
    with pytest.raises(ValueError, match="non-finite"):
        serialize_float16_vector([1.0, float("nan"), 0.0, 0.0], 4)


def test_embedding_status_does_not_initialize_a_missing_store(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir = tmp_path / "state"
    embedding_dir = tmp_path / "embeddings"

    result = main(
        [
            "embedding-status",
            "--state-dir",
            str(state_dir),
            "--embedding-dir",
            str(embedding_dir),
            "--json",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exists"] is False
    assert payload["photo_network_bytes"] == 0
    assert not embedding_dir.exists()


def test_embedding_sync_cli_is_local_and_idempotent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir = tmp_path / "state"
    embedding_dir = tmp_path / "embeddings"
    asset = RemoteAsset(
        provider="test",
        remote_id="asset-1",
        remote_path="/archive/2026/2026-01-01/DSC00001.ARW",
        name="DSC00001.ARW",
        size_bytes=1000,
        write_time="2026-01-01T12:00:00+00:00",
        file_hashes={"2": "first"},
    )
    with StateStore(state_dir) as store:
        asset_id = store.upsert_asset(asset)
        CacheManager(
            store,
            {"contact": 10_000, "preview": 0, "temporary": 0},
        ).put(
            asset_id=asset_id,
            variant="contact",
            fingerprint=asset.fingerprint,
            data=b"contact",
            width=4,
            height=3,
        )

    arguments = [
        "embedding-sync",
        "--state-dir",
        str(state_dir),
        "--embedding-dir",
        str(embedding_dir),
        "--json",
    ]
    assert main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["sync"] == {
        "discovered": 1,
        "enqueued": 1,
        "pending_jobs": 1,
        "removed": 0,
        "unchanged": 0,
    }
    assert first["status"]["photo_network_bytes"] == 0
    assert first["status"]["archive_modified"] is False

    assert main(arguments) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["sync"]["enqueued"] == 0
    assert second["sync"]["unchanged"] == 1
    assert second["status"]["jobs"]["pending"] == 1


def test_embedding_build_cli_requires_an_explicit_job_cap() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["embedding-build"])
