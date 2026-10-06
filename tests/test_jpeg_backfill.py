from __future__ import annotations

import hashlib
from pathlib import Path

from latent.jpeg_backfill import enqueue_jpeg_plan, plan_jpeg_backfill
from latent.models import DirectoryListing, RemoteAsset
from latent.storage import StateStore


def asset(path: str, *, content: str | None = None) -> RemoteAsset:
    return RemoteAsset(
        "archive",
        path,
        path,
        Path(path).name,
        1_000_000,
        file_hashes={"sha1": hashlib.sha1((content or path).encode()).hexdigest()},
    )


class Catalog:
    def __init__(self, listings: dict[str, DirectoryListing]) -> None:
        self.listings = listings

    def list_directory_contents(self, path, **kwargs):
        return self.listings[path]


def test_plan_is_read_only_and_separates_pairs_exports_and_nested_photos(tmp_path):
    root = "/archive/2025-06-01"
    child = root + "/finished-selection"
    photos = tuple(
        asset(root + "/" + name)
        for name in (
            "one.ARW",
            "one.JPG",
            "two.JPEG",
            "three.NEF",
            "three.jpg",
            "two_DxO.jpg",
            "LRT_00001.jpg",
            "C0001T01.JPG",
        )
    )
    catalog = Catalog(
        {
            root: DirectoryListing(root, (child,), photos),
            child: DirectoryListing(child, (), (asset(child + "/selection.jpg"),)),
        }
    )
    state = tmp_path / "state"
    plan = plan_jpeg_backfill(catalog, [root], state, recursive=True)
    assert not state.exists()
    assert plan.summary() | {} == {
        "jpeg_files": 7,
        "candidates": 1,
        "raw_pairs": 2,
        "already_indexed": 0,
        "identical_copies": 0,
        "needs_review": 4,
        "directories": 2,
        "unscanned_directories": 0,
        "candidate_original_bytes": 1_000_000,
        "archive_modified": False,
    }
    assert next(e for e in plan.entries if e.decision == "candidate").asset.name == "two.JPEG"


def test_import_is_resumable_and_same_filename_on_other_dates_is_preserved(tmp_path):
    roots = ["/archive/2025-06-01", "/archive/2025-06-02"]
    catalog = Catalog(
        {root: DirectoryListing(root, (), (asset(root + "/DSC00001.JPG"),)) for root in roots}
    )
    state = tmp_path / "state"
    plan = plan_jpeg_backfill(catalog, roots, state)
    with StateStore(state) as store:
        assert enqueue_jpeg_plan(plan, store) == {"enqueued": 2, "unchanged": 0}
        before = [tuple(r) for r in store.connection.execute("SELECT * FROM assets")]
        assert enqueue_jpeg_plan(plan, store) == {"enqueued": 0, "unchanged": 2}
        assert before == [tuple(r) for r in store.connection.execute("SELECT * FROM assets")]
        assert store.preview_job_counts()["pending"] == 2
    second = plan_jpeg_backfill(catalog, roots, state)
    assert second.summary()["already_indexed"] == 2
    assert second.summary()["candidates"] == 0


def test_changed_sources_are_held_and_cross_folder_raw_names_need_review(tmp_path):
    root = "/archive/2025-06-01"
    state = tmp_path / "state"
    with StateStore(state) as store:
        store.upsert_asset(asset(root + "/changed.jpg", content="old"))
        store.upsert_asset(asset(root + "/RAW/paired.ARW"))
        store.upsert_asset(asset("/archive/2025-05-01/unrelated.ARW"))
    catalog = Catalog(
        {
            root: DirectoryListing(
                root,
                (),
                tuple(
                    asset(root + "/" + name)
                    for name in ("changed.jpg", "paired.jpg", "unrelated.jpg")
                ),
            )
        }
    )
    plan = plan_jpeg_backfill(catalog, [root], state)
    assert {e.asset.name: e.decision for e in plan.entries} == {
        "changed.jpg": "review",
        "paired.jpg": "review",
        "unrelated.jpg": "candidate",
    }
    with StateStore(state) as store:
        assert enqueue_jpeg_plan(plan, store)["enqueued"] == 1
        row = store.connection.execute(
            "SELECT fingerprint FROM assets WHERE name='changed.jpg'"
        ).fetchone()
        assert row[0] == asset(root + "/changed.jpg", content="old").fingerprint


def test_only_matching_content_hashes_collapse_copies(tmp_path):
    root = "/archive/2025-06-01"
    catalog = Catalog(
        {
            root: DirectoryListing(
                root,
                (),
                (
                    asset(root + "/a.jpg", content="same"),
                    asset(root + "/b.jpg", content="same"),
                    asset(root + "/c.jpg", content="different"),
                ),
            )
        }
    )
    plan = plan_jpeg_backfill(catalog, [root], tmp_path / "state")
    assert plan.summary()["candidates"] == 2
    assert plan.summary()["identical_copies"] == 1
