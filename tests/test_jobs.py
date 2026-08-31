from __future__ import annotations

import io
from contextlib import nullcontext
from pathlib import Path

import pytest
from PIL import Image

from latent.jobs import (
    ArchiveTreeScanner,
    DirectoryImporter,
    PreviewWorker,
    reprioritize_preview_jobs,
)
from latent.models import DirectoryListing, PreviewLocation, ProbeResult, RemoteAsset
from latent.preview import PreviewPipeline
from latent.provider import MemoryRangeSource
from latent.storage import CacheManager, StateStore


class FakeCatalog:
    def __init__(self, assets: list[RemoteAsset]) -> None:
        self.assets = assets

    def list_directory(
        self,
        remote_path: str,
        *,
        extensions: set[str] | None = None,
        force_refresh: bool = False,
        limit: int | None = None,
    ) -> list[RemoteAsset]:
        assert remote_path == "/archive/2026-01-01"
        assert extensions == {"arw"}
        assert force_refresh is False
        return self.assets[:limit]


class FakeTreeCatalog:
    def __init__(
        self,
        listings: dict[str, DirectoryListing],
        *,
        fail_once: set[str] | None = None,
    ) -> None:
        self.listings = listings
        self.fail_once = set() if fail_once is None else set(fail_once)
        self.calls: list[str] = []

    def list_directory_contents(
        self,
        remote_path: str,
        *,
        extensions: set[str] | None = None,
        force_refresh: bool = False,
    ) -> DirectoryListing:
        assert extensions == {"arw"}
        assert force_refresh is False
        self.calls.append(remote_path)
        if remote_path in self.fail_once:
            self.fail_once.remove(remote_path)
            raise RuntimeError("temporary catalog failure")
        return self.listings[remote_path]


class StaticProbe:
    def __init__(self, offset: int, length: int) -> None:
        self.offset = offset
        self.length = length

    def probe(self, data: bytes, suffix: str) -> ProbeResult:
        assert data
        assert suffix == ".arw"
        return ProbeResult(
            location=PreviewLocation("PreviewImage", self.offset, self.length),
            metadata={"DateTimeOriginal": "2026:01:01 12:00:00", "Orientation": 1},
        )


def jpeg_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (800, 600), "#456270").save(output, format="JPEG", quality=88)
    return output.getvalue()


def remote_asset(remote_path: str, remote_id: str) -> RemoteAsset:
    return RemoteAsset(
        provider="test",
        remote_id=remote_id,
        remote_path=remote_path,
        name=Path(remote_path).name,
        size_bytes=1000,
        write_time="2026-01-01T12:00:00+00:00",
        file_hashes={"2": f"hash-{remote_id}"},
    )


def test_directory_queue_resumes_and_does_not_duplicate_completed_jobs(tmp_path: Path) -> None:
    jpeg = jpeg_bytes()
    offset = 300
    bodies = {
        f"/archive/2026-01-01/DSC0000{index}.ARW": bytes(offset) + jpeg + bytes(index)
        for index in (1, 2)
    }
    assets = [
        RemoteAsset(
            provider="test",
            remote_id=f"asset-{index}",
            remote_path=remote_path,
            name=Path(remote_path).name,
            size_bytes=len(body),
            write_time="2026-01-01T12:00:00+00:00",
            file_hashes={"2": f"hash-{index}"},
        )
        for index, (remote_path, body) in enumerate(bodies.items(), start=1)
    ]
    catalog = FakeCatalog(assets)

    with StateStore(tmp_path) as store:
        scan = DirectoryImporter(store, catalog).scan("/archive/2026-01-01")
        assert (scan.discovered, scan.enqueued, scan.unchanged) == (2, 2, 0)
        pipeline = PreviewPipeline(
            store,
            CacheManager(store, {"preview": 10**7, "contact": 10**7, "temporary": 0}),
            StaticProbe(offset, len(jpeg)),
            initial_prefix_bytes=128,
            maximum_prefix_bytes=512,
        )

        def source_factory(remote_path: str):
            asset = next(asset for asset in assets if asset.remote_path == remote_path)
            return nullcontext(MemoryRangeSource(asset, bodies[remote_path]))

        first_run = PreviewWorker(store, pipeline, source_factory).run(max_jobs=1)
        assert (first_run.processed, first_run.succeeded, first_run.failed) == (1, 1, 0)
        assert store.preview_job_counts() == {
            "pending": 1,
            "running": 0,
            "succeeded": 1,
            "failed": 0,
        }

    with StateStore(tmp_path) as store:
        pipeline = PreviewPipeline(
            store,
            CacheManager(store, {"preview": 10**7, "contact": 10**7, "temporary": 0}),
            StaticProbe(offset, len(jpeg)),
            initial_prefix_bytes=128,
            maximum_prefix_bytes=512,
        )

        def resumed_source_factory(remote_path: str):
            asset = next(asset for asset in assets if asset.remote_path == remote_path)
            return nullcontext(MemoryRangeSource(asset, bodies[remote_path]))

        second_run = PreviewWorker(store, pipeline, resumed_source_factory).run(max_jobs=5)
        assert (second_run.processed, second_run.succeeded, second_run.failed) == (1, 1, 0)
        assert store.preview_job_counts()["succeeded"] == 2

        repeated_scan = DirectoryImporter(store, catalog).scan("/archive/2026-01-01")
        assert (repeated_scan.enqueued, repeated_scan.unchanged) == (0, 2)


def test_interrupted_running_job_can_be_requeued(tmp_path: Path) -> None:
    body = b"raw"
    asset = RemoteAsset(
        provider="test",
        remote_id="asset",
        remote_path="/archive/2026-01-01/DSC00001.ARW",
        name="DSC00001.ARW",
        size_bytes=len(body),
    )
    with StateStore(tmp_path) as store:
        asset_id = store.upsert_asset(asset)
        assert store.enqueue_preview_job(asset_id, asset.fingerprint)
        assert store.claim_preview_job() is not None
        assert store.preview_job_counts()["running"] == 1

        recovered = store.requeue_running_preview_jobs(before="9999-01-01T00:00:00+00:00")

        assert recovered == 1
        assert store.preview_job_counts()["pending"] == 1
        assert store.preview_job_counts()["running"] == 0


def test_archive_tree_scan_resumes_skips_auxiliary_directories_and_is_idempotent(
    tmp_path: Path,
) -> None:
    root = "/archive"
    day_one = remote_asset(f"{root}/2026/2026-01-01/DSC00001.ARW", "one")
    day_two = remote_asset(f"{root}/2026/2026-01-02/DSC00002.ARW", "two")
    listings = {
        root: DirectoryListing(
            root,
            (f"{root}/2026", f"{root}/_inbox"),
            (),
        ),
        f"{root}/2026": DirectoryListing(
            f"{root}/2026",
            (f"{root}/2026/2026-01-01", f"{root}/2026/2026-01-02"),
            (),
        ),
        f"{root}/2026/2026-01-01": DirectoryListing(
            f"{root}/2026/2026-01-01",
            (),
            (day_one,),
        ),
        f"{root}/2026/2026-01-02": DirectoryListing(
            f"{root}/2026/2026-01-02",
            (),
            (day_two,),
        ),
        f"{root}/_inbox": DirectoryListing(f"{root}/_inbox", (), ()),
    }
    catalog = FakeTreeCatalog(listings)

    with StateStore(tmp_path) as store:
        scanner = ArchiveTreeScanner(store, catalog)
        first = scanner.start(root, max_directories=1)
        assert first.status["status"] == "active"
        assert first.status["directories"] == {
            "pending": 1,
            "running": 0,
            "succeeded": 1,
            "failed": 0,
        }
        assert first.status["excluded_subdirectories"] == 1
        assert f"{root}/_inbox" not in catalog.calls

        assert store.cancel_archive_scan(first.scan_id)
        assert store.archive_scan_status(first.scan_id)["status"] == "cancelled"

        middle = scanner.resume(first.scan_id, max_directories=1)
        assert middle.status["status"] == "active"
        assert middle.status["directories"]["pending"] == 2

        completed = scanner.resume(first.scan_id, max_directories=10)
        assert completed.status["status"] == "completed"
        assert completed.status["discovered_assets"] == 2
        assert store.preview_job_counts()["pending"] == 2

        repeated = scanner.start(root, max_directories=10)
        assert repeated.scan_id != first.scan_id
        assert repeated.status["status"] == "completed"
        assert repeated.discovered_assets == 2
        assert repeated.enqueued_assets == 0
        assert repeated.unchanged_assets == 2


def test_failed_archive_directory_can_be_retried(tmp_path: Path) -> None:
    root = "/archive"
    failed_path = f"{root}/2026"
    listings = {
        root: DirectoryListing(root, (failed_path,), ()),
        failed_path: DirectoryListing(failed_path, (), ()),
    }
    catalog = FakeTreeCatalog(listings, fail_once={failed_path})

    with StateStore(tmp_path) as store:
        scanner = ArchiveTreeScanner(store, catalog)
        failed = scanner.start(root, max_directories=10)
        assert failed.status["status"] == "failed"
        assert failed.status["directories"]["failed"] == 1

        resumed = scanner.resume(failed.scan_id, max_directories=10, retry_failed=True)
        assert resumed.status["status"] == "completed"
        assert resumed.status["directories"]["failed"] == 0
        assert catalog.calls.count(failed_path) == 2


def test_preview_jobs_are_reprioritized_by_archive_date(tmp_path: Path) -> None:
    older = remote_asset("/archive/2025/2025-12-31/DSC00001.ARW", "older")
    newer = remote_asset("/archive/2026/2026-08-29/DSC00002.ARW", "newer")

    with StateStore(tmp_path) as store:
        older_id = store.upsert_asset(older)
        newer_id = store.upsert_asset(newer)
        assert store.enqueue_preview_job(older_id, older.fingerprint, priority=999)
        assert store.enqueue_preview_job(newer_id, newer.fingerprint, priority=0)

        assert reprioritize_preview_jobs(store) == 2
        claimed = store.claim_preview_job()

        assert claimed is not None
        assert claimed.remote_path == newer.remote_path
        store.release_preview_job(claimed.id)


def test_preview_worker_releases_current_job_when_interrupted(tmp_path: Path) -> None:
    asset = remote_asset("/archive/2026/2026-08-29/DSC00001.ARW", "interrupt")

    with StateStore(tmp_path) as store:
        asset_id = store.upsert_asset(asset)
        assert store.enqueue_preview_job(asset_id, asset.fingerprint)
        pipeline = PreviewPipeline(
            store,
            CacheManager(store, {"preview": 10**7, "contact": 10**7, "temporary": 0}),
            StaticProbe(0, 100),
            initial_prefix_bytes=128,
            maximum_prefix_bytes=512,
        )

        def interrupting_source_factory(_: str):
            raise KeyboardInterrupt

        worker = PreviewWorker(store, pipeline, interrupting_source_factory)
        with pytest.raises(KeyboardInterrupt):
            worker.run(max_jobs=1)

        assert store.preview_job_counts() == {
            "pending": 1,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
        }


def test_preview_worker_stops_after_consecutive_failures(tmp_path: Path) -> None:
    assets = [
        remote_asset(f"/archive/2026/2026-08-29/DSC0000{index}.ARW", f"fail-{index}")
        for index in range(1, 4)
    ]

    with StateStore(tmp_path) as store:
        for asset in assets:
            asset_id = store.upsert_asset(asset)
            assert store.enqueue_preview_job(asset_id, asset.fingerprint)
        pipeline = PreviewPipeline(
            store,
            CacheManager(store, {"preview": 10**7, "contact": 10**7, "temporary": 0}),
            StaticProbe(0, 100),
            initial_prefix_bytes=128,
            maximum_prefix_bytes=512,
        )

        def failing_source_factory(_: str):
            raise RuntimeError("provider unavailable")

        result = PreviewWorker(store, pipeline, failing_source_factory).run(
            max_jobs=3,
            max_consecutive_failures=2,
        )

        assert result.processed == 2
        assert result.failed == 2
        assert result.stopped_after_consecutive_failures
        assert store.preview_job_counts() == {
            "pending": 1,
            "running": 0,
            "succeeded": 0,
            "failed": 2,
        }
