from __future__ import annotations

import json
import time
from urllib.error import HTTPError
from uuid import uuid4

import pytest
from test_folder_workflow import change, image, records, scan, setup_library
from test_search import _seed_vectors
from test_web_server import (
    FakeTextEncoder,
    _get_json,
    _request_json,
    _running_server,
    _seed_pageable_library,
)

from latent.filters import photo_filters
from latent.search import SemanticSearch, VectorIndex
from latent.sequence_links import SequenceLinks
from latent.trash import PhotoTrash
from latent.workspace import WorkspaceAsset, WorkspaceStore


def post(url, payload):
    return _request_json(url, method="POST", payload=payload)[0]


def test_filters_rank_only_eligible_photos_and_smart_sequences_update(tmp_path):
    _seed_pageable_library(tmp_path, capture_dates=("2025-12-25", "2026-08-28", "2026-08-29"))
    embedding_dir = tmp_path / "embeddings"
    _seed_vectors(embedding_dir)
    index = VectorIndex(embedding_dir, dimensions=4)
    with _running_server(
        tmp_path, vector_index=index, semantic_search=SemanticSearch(index, FakeTextEncoder())
    ) as base:
        for identifier, rating, flag in [(1, 5, "pick"), (2, 0, "reject"), (3, 4, "pick")]:
            _request_json(
                base + "/api/annotations",
                method="PATCH",
                payload={"asset_ids": [identifier], "rating": rating, "flag": flag},
            )
        query = "date_from=2026-08-29&date_to=2026-08-29&rating_min=4&starred=yes&flag=pick"
        page, _ = _get_json(base + "/api/assets?" + query)
        assert [p["id"] for p in page["assets"]] == [3]
        result, _ = _get_json(base + "/api/search?q=light&limit=1&" + query)
        assert [p["id"] for p in result["results"]] == [3]
        assert result["results"][0]["rank"] == 1
        unstarred, _ = _get_json(base + "/api/assets?starred=no&flag=reject")
        assert [p["id"] for p in unstarred["assets"]] == [2]

        folder = post(base + "/api/sequence-folders", {"name": "Favourites"})["folder"]
        smart = post(
            base + "/api/sequences",
            {
                "name": "Top picks",
                "folder_id": folder["id"],
                "smart_filters": {"rating_min": 4, "flag": "pick"},
            },
        )["sequence"]
        assert smart["folder_id"] == folder["id"]
        assert smart["item_count"] == 2
        with pytest.raises(HTTPError) as error:
            post(base + f"/api/sequences/{smart['id']}/items", {"asset_ids": [2]})
        assert error.value.code == 400
        _request_json(
            base + "/api/annotations",
            method="PATCH",
            payload={"asset_ids": [2], "rating": 5, "flag": "pick"},
        )
        _request_json(
            base + "/api/annotations", method="PATCH", payload={"asset_ids": [1], "rating": 1}
        )
        current, _ = _get_json(base + f"/api/sequences/{smart['id']}")
        assert [item["asset"]["id"] for item in current["sequence"]["items"]] == [2, 3]
        summary, _ = _get_json(base + "/api/sequences")
        assert summary["sequences"][0]["item_count"] == 2
        _request_json(
            base + f"/api/sequences/{smart['id']}",
            method="PATCH",
            payload={"smart_filters": {"rating_min": 5, "flag": "pick"}},
        )
    with WorkspaceStore(tmp_path / "workspace") as workspace:
        payload = workspace.export_payload()
    with WorkspaceStore(tmp_path / "imported") as workspace:
        assert workspace.merge_import(payload).imported == 5  # folder, sequence, 3 annotations
        assert workspace.get_sequence(smart["id"])["smart_filters"] == {
            "rating_min": 5,
            "flag": "pick",
        }
        assert workspace.merge_import(payload).conflicts == 0
    with _running_server(tmp_path) as base:
        current, _ = _get_json(base + f"/api/sequences/{smart['id']}")
        assert [item["asset"]["id"] for item in current["sequence"]["items"]] == [2]


@pytest.mark.parametrize(
    "value",
    [
        {"date_from": "2026-02-30"},
        {"date_from": "2026-08-30", "date_to": "2026-08-29"},
        {"rating_min": True},
        {"rating_min": 6},
        {"flag": "maybe"},
        {"sql": "anything"},
    ],
)
def test_invalid_smart_filters_are_rejected(value):
    with pytest.raises(ValueError):
        photo_filters(value)


@pytest.mark.parametrize("order", ["closest", "least_similar", "variety"])
def test_candidate_filter_is_applied_before_limit_in_every_order(tmp_path, order):
    _seed_vectors(tmp_path)
    index = VectorIndex(tmp_path, dimensions=4)
    matches = index.search_vector([1, 0, 0, 0], limit=1, order=order, allowed_asset_ids=[2])
    assert [m.asset_id for m in matches] == [2]
    assert index.search_vector([1, 0, 0, 0], allowed_asset_ids=[]) == []


def test_trash_moves_original_restores_annotations_and_survives_interruption(tmp_path):
    locations, original, _ = setup_library(tmp_path)
    photo = original / "2026" / "one.jpg"
    image(photo)
    scan(locations)
    selected = records(locations)
    contents = photo.read_bytes()
    with WorkspaceStore(locations.path.parent) as workspace:
        asset = WorkspaceAsset(
            **{key: selected[0][key] for key in ("provider", "remote_path", "fingerprint", "name")}
        )
        workspace.annotate([asset], rating=5, flag="pick")
        sequence = workspace.create_sequence("Keep membership")
        workspace.add_items(sequence["id"], [asset])
    manager = PhotoTrash(locations)
    identifier = str(uuid4())
    try:
        batch = manager.create(identifier, selected)
        manager.worker.join(3)
        assert manager.get(identifier)["status"] == "trashed"
        target = original / batch["items"][0]["trash_relative"]
        assert target.read_bytes() == contents and not photo.exists()
        assert records(locations) == []
        assert scan(locations)["discovered"] == 0
        assert manager.create(identifier, selected)["status"] == "trashed"
    finally:
        manager.close()
    # Reproduce a crash after rename but before the journal records that item's move.
    manifest = manager.root / f"{identifier}.json"
    saved = json.loads(manifest.read_text())
    saved["status"] = "moving"
    saved["items"][0]["state"] = "pending"
    manifest.write_text(json.dumps(saved))
    reopened = PhotoTrash(locations)
    try:
        assert reopened.get(identifier)["status"] == "interrupted"
        reopened.action(identifier, restore=False)
        reopened.worker.join(3)
        assert reopened.get(identifier)["status"] == "trashed"
        photo.write_bytes(b"new unrelated file")
        reopened.action(identifier, restore=True)
        reopened.worker.join(3)
        assert reopened.get(identifier)["status"] == "needs_attention"
        assert photo.read_bytes() == b"new unrelated file" and target.read_bytes() == contents
        photo.unlink()
        reopened.action(identifier, restore=True)
        reopened.worker.join(3)
        assert reopened.get(identifier)["status"] == "restored"
        assert photo.read_bytes() == contents
        assert records(locations)[0]["id"] == selected[0]["id"]
        with WorkspaceStore(locations.path.parent) as workspace:
            assert workspace.annotations()[0]["rating"] == 5
            assert workspace.get_sequence(sequence["id"])["item_count"] == 1
    finally:
        reopened.close()


def test_changed_original_and_symlink_trash_never_move_other_data(tmp_path):
    locations, original, _ = setup_library(tmp_path)
    photo = original / "one.jpg"
    image(photo)
    scan(locations)
    selected = records(locations)
    image(photo, "gold")
    modified = photo.read_bytes()
    manager = PhotoTrash(locations)
    try:
        # A same-size rewrite is still rejected by the indexed mtime/fingerprint.
        selected[0]["size_bytes"] = len(modified)
        batch = manager.create(str(uuid4()), selected)
        manager.worker.join(3)
        assert manager.get(batch["id"])["status"] == "needs_attention"
        assert photo.read_bytes() == modified
        manager.action(batch["id"], restore=True)
        manager.worker.join(3)
        assert manager.get(batch["id"])["status"] == "restored"
        scan(locations)
        outside = tmp_path / "outside"
        outside.mkdir()
        (original / ".Latent Trash").symlink_to(outside, target_is_directory=True)
        batch = manager.create(str(uuid4()), records(locations))
        manager.worker.join(3)
        assert manager.get(batch["id"])["status"] == "needs_attention"
        assert list(outside.iterdir()) == [] and photo.read_bytes() == modified
    finally:
        manager.close()


def test_trash_api_requires_matching_identity_and_replays_without_deleting_again(tmp_path):
    locations, original, _ = setup_library(tmp_path)
    photo = original / "one.jpg"
    image(photo)
    scan(locations)
    record = records(locations)[0]
    with _running_server(locations.state_dir, workspace_dir=locations.path.parent) as base:
        health, _ = _get_json(base + "/health")
        expectation = {key: record[key] for key in ("id", "provider", "remote_path", "fingerprint")}
        payload = {
            "request_id": str(uuid4()),
            "asset_ids": [record["id"]],
            "expected": {"data_id": "other", "assets": [expectation]},
        }
        with pytest.raises(HTTPError) as error:
            post(base + "/api/trash", payload)
        assert error.value.code == 400 and photo.is_file()
        payload["expected"]["data_id"] = health["data_id"]
        post(base + "/api/trash", payload)
        deadline = time.monotonic() + 3
        while True:
            batches, _ = _get_json(base + "/api/trash")
            if batches["batches"][0]["status"] == "trashed":
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        replay = post(base + "/api/trash", payload)
        assert replay["batch"]["id"] == payload["request_id"]
        page, _ = _get_json(base + "/api/assets")
        assert page["total"] == 0
        assert not photo.exists()


def test_all_trashed_photos_can_restore_after_source_is_reconnected(tmp_path):
    locations, original, _ = setup_library(tmp_path)
    image(original / "one.jpg")
    scan(locations)
    selected = records(locations)
    manager = PhotoTrash(locations)
    try:
        batch = manager.create(str(uuid4()), selected)
        manager.worker.join(3)
        assert manager.get(batch["id"])["status"] == "trashed"
        relocated = tmp_path / "remounted-originals"
        original.rename(relocated)
        change(
            locations,
            "reconnect_source",
            source_id=locations.get()["sources"][0]["id"],
            path=str(relocated),
        )
        manager.action(batch["id"], restore=True)
        manager.worker.join(3)
        assert manager.get(batch["id"])["status"] == "restored"
        assert (relocated / "one.jpg").is_file()
        assert records(locations)[0]["id"] == selected[0]["id"]
    finally:
        manager.close()


def test_smart_sequence_folder_links_follow_updated_ratings(tmp_path):
    locations, original, _ = setup_library(tmp_path)
    image(original / "one.jpg")
    scan(locations)
    links = tmp_path / "sequence-parent"
    links.mkdir()
    change(locations, "set_sequence_folder", path=str(links))
    record = records(locations)[0]
    asset = WorkspaceAsset(
        **{key: record[key] for key in ("provider", "remote_path", "fingerprint", "name")}
    )
    with WorkspaceStore(locations.path.parent) as workspace:
        folder = workspace.create_folder("Favourites")
        workspace.create_sequence(
            "Top picks", folder_id=folder["id"], smart_filters={"rating_min": 4}
        )
        workspace.annotate([asset], rating=5)
    projection = SequenceLinks(locations)
    assert projection.sync(locations.get()["revision"])["links"] == 1
    target = links / "sequences/Favourites/Top picks/0001-one.jpg"
    assert target.is_symlink() and target.resolve() == original / "one.jpg"
    with WorkspaceStore(locations.path.parent) as workspace:
        workspace.annotate([asset], rating=3)
    assert projection.sync(locations.get()["revision"])["links"] == 0
    assert not target.exists() and (original / "one.jpg").is_file()
