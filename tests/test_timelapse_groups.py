from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from urllib.error import HTTPError

import pytest
from test_web_server import _get_json, _jpeg_bytes, _request_json, _running_server

from latent.models import PreviewLocation, ProbeResult, RemoteAsset
from latent.storage import CacheManager, StateStore
from latent.timelapse_groups import TimelapseGroups
from latent.workspace import WorkspaceStore


def seed(state, *, first=0):
    jpeg = _jpeg_bytes((480, 320))
    with StateStore(state) as store:
        cache = CacheManager(store, {"contact": 10**7, "preview": 0, "temporary": 0})
        for i in range(first, 42):
            # Forty interval frames followed by two ordinary photos.
            capture = datetime(2025, 6, 1, 12) + timedelta(seconds=i * 10 if i < 40 else i * 101)
            photo = RemoteAsset(
                provider="test", remote_id=str(i), remote_path=f"/archive/DSC{i:05}.JPG",
                name=f"DSC{i:05}.JPG", size_bytes=10_000, write_time="2025-06-01",
            )
            identifier = store.upsert_asset(photo)
            store.update_probe(identifier, ProbeResult(
                location=PreviewLocation("PreviewImage", 0, len(jpeg)),
                metadata={"DateTimeOriginal": capture.strftime("%Y:%m:%d %H:%M:%S"),
                          "Model": "Camera", "LensModel": "35mm", "BodySerialNumber": "one"},
            ), width=480, height=320)
            cache.put(asset_id=identifier, variant="contact", fingerprint=photo.fingerprint,
                      data=jpeg, width=480, height=320)


def change(base, group, decision, **overrides):
    health, _ = _get_json(base + "/health")
    return _request_json(base + "/api/timelapse/" + group["id"], method="PATCH", payload={
        "confirmed": decision, "expected_revision": group["revision"],
        "expected_data_id": health["data_id"], **overrides,
    })[0]


def audit(base):
    health, _ = _get_json(base + "/health")
    return _request_json(base + "/api/timelapse/audit", method="POST", payload={
        "expected_data_id": health["data_id"],
    })[0]


def test_opt_in_confirm_expand_paginate_restart_and_ungroup(tmp_path):
    seed(tmp_path)
    digest = hashlib.sha256((tmp_path / "index.sqlite").read_bytes()).hexdigest()
    with _running_server(tmp_path) as base:
        page, _ = _get_json(base + "/api/assets")
        assert page["total"] == 42
        assert not (tmp_path / "workspace/timelapse.sqlite").exists()
        group, = audit(base)["groups"]
        assert group["confirmed"] is False and group["photo_count"] == 40
        page, _ = _get_json(base + "/api/assets?collapse_timelapses=1")
        assert page["total"] == 42
        confirmed, = change(base, group, True)["groups"]
        page, _ = _get_json(base + "/api/assets?collapse_timelapses=1&limit=1")
        assert page["total"] == 3 and page["expanded_total"] == 42
        assert page["assets"][0]["timelapse"]["photo_count"] == 40
        ids = [page["assets"][0]["id"]]
        for offset in (1, 2):
            page, _ = _get_json(base + f"/api/assets?collapse_timelapses=1&limit=1&offset={offset}")
            ids.extend(a["id"] for a in page["assets"])
        assert ids == [1, 41, 42]
        expanded, _ = _get_json(base + "/api/assets?timelapse_group=" + group["id"])
        assert [a["id"] for a in expanded["assets"]] == list(range(1, 41))
        assert not any("timelapse" in a for a in expanded["assets"])
        plain, _ = _get_json(base + "/api/assets")
        assert plain["total"] == 42
        # Auditing again preserves a decision and its optimistic revision.
        same, = audit(base)["groups"]
        assert same["confirmed"] and same["revision"] == confirmed["revision"]
    with _running_server(tmp_path) as base:
        restarted, _ = _get_json(base + "/api/timelapse")
        assert restarted["groups"][0]["confirmed"]
        change(base, restarted["groups"][0], False)
        restored, _ = _get_json(base + "/api/assets?collapse_timelapses=1")
        assert restored["total"] == 42
    assert hashlib.sha256((tmp_path / "index.sqlite").read_bytes()).hexdigest() == digest
    with WorkspaceStore(tmp_path / "workspace") as workspace:
        assert workspace.status()["sequences"] == 0
        assert workspace.annotations() == []


def test_filtering_chooses_an_eligible_representative_and_preserves_ratings(tmp_path):
    seed(tmp_path)
    with _running_server(tmp_path) as base:
        group, = audit(base)["groups"]
        change(base, group, True)
        _request_json(base + "/api/annotations", method="PATCH",
                      payload={"asset_ids": [30, 35, 42], "rating": 5, "flag": "pick"})
        page, _ = _get_json(base + "/api/assets?collapse_timelapses=1&rating_min=5&flag=pick")
        assert [a["id"] for a in page["assets"]] == [30, 42]
        assert page["expanded_total"] == 3
        assert page["assets"][0]["timelapse"]["photo_count"] == 2
        starred, _ = _get_json(base + "/api/starred")
        assert {a["id"] for a in starred["assets"]} == {30, 35, 42}
        sequence, _ = _request_json(base + "/api/sequences", method="POST",
                                   payload={"name": "Selected"})
        _request_json(base + f"/api/sequences/{sequence['sequence']['id']}/items", method="POST",
                      payload={"asset_ids": [30, 35]})
        sequences, _ = _get_json(base + "/api/sequences")
        assert sequences["sequences"][0]["item_count"] == 2


def test_stale_decisions_wrong_library_and_changed_fingerprints_are_rejected(tmp_path):
    seed(tmp_path)
    with _running_server(tmp_path) as base:
        group, = audit(base)["groups"]
        for override in ({"expected_data_id": "wrong"}, {"expected_revision": 0},
                         {"confirmed": "yes"}):
            with pytest.raises(HTTPError) as error:
                change(base, group, True, **override)
            assert error.value.code == 400
        confirmed, = change(base, group, True)["groups"]
        with StateStore(tmp_path) as catalog:
            catalog.connection.execute("UPDATE assets SET fingerprint='changed' WHERE id=1")
            catalog.connection.commit()
        page, _ = _get_json(base + "/api/assets?collapse_timelapses=1")
        assert [a["id"] for a in page["assets"]] == [1, 2, 41, 42]
        assert "timelapse" not in page["assets"][0]
        candidate, = change(base, confirmed, False)["groups"]
        assert candidate["available_count"] == 39
        with pytest.raises(HTTPError) as error:
            change(base, candidate, True)
        assert error.value.code == 400


def test_unknown_group_is_not_the_whole_library(tmp_path):
    seed(tmp_path)
    with _running_server(tmp_path) as base:
        with pytest.raises(HTTPError) as error:
            _get_json(base + "/api/assets?timelapse_group=missing")
        assert error.value.code == 404


def test_archive_reindex_uses_stable_references(tmp_path):
    original = tmp_path / "old"
    replacement = tmp_path / "new"
    workspace = tmp_path / "workspace"
    seed(original)
    with TimelapseGroups(workspace) as groups:
        groups.audit(original)
        group, = groups.list(original)
        groups.set_confirmed(original, group["id"], confirmed=True, expected_revision=1)
        # The same assets acquire different numeric IDs in a rebuilt catalog.
        seed(replacement, first=40)
        seed(replacement)
        with StateStore(replacement) as catalog:
            visible, badges = groups.presentation(catalog, catalog.library_asset_ids())
        assert visible == [3, 1, 2]
        assert badges[3]["photo_count"] == 40
