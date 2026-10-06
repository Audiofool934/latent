from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from PIL import Image

from latent.editing import EditingManager
from latent.filesystem import FilesystemRangeSource, filesystem_asset
from latent.folder_output import copy_finished_photo, destination, sha256
from latent.ingest import FolderIndexer
from latent.locations import LocationsStore, check_folder, folder_identity
from latent.models import RemoteAsset
from latent.sequence_links import SequenceLinks
from latent.storage import StateStore
from latent.workspace import WorkspaceAsset, WorkspaceStore


def change(locations, action, **values):
    return locations.update(
        {"expected_revision": locations.get()["revision"], "action": action, **values}
    )


def image(path, color="navy"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (240, 160), color).save(path)


def setup_library(tmp_path):
    original = tmp_path / "originals"
    original.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    locations = LocationsStore(tmp_path / "workspace", tmp_path / "index")
    change(locations, "add_source", path=str(original))
    change(locations, "set_output", path=str(output))
    return locations, original, output


def scan(locations):
    indexer = FolderIndexer(locations)
    try:
        indexer.start(locations.get()["revision"])
        # An empty folder completes without starting an import worker.
        if indexer.worker is not None:
            indexer.worker.join(10)
        assert not indexer.busy
        assert indexer.status()["status"] == "complete", indexer.status()
        return indexer.status()
    finally:
        indexer.close()


def records(locations):
    with StateStore(locations.state_dir) as store:
        ids = [row[0] for row in store.connection.execute("SELECT id FROM assets")]
        return store.library_assets_by_ids(ids)


def ready_batch(locations, tmp_path):
    manager = EditingManager(
        locations.path.parent, files_dir=tmp_path / "Pictures", locations=locations
    )
    batch = manager.create(records(locations))
    manager.worker.join(10)
    batch = manager.get(batch["id"])
    assert batch["status"] == "ready", batch
    return manager, batch


def test_local_source_scans_previews_and_keeps_identity_across_restarts(tmp_path):
    locations, original, _ = setup_library(tmp_path)
    image(original / "2026" / "trip" / "one.jpg")
    assert scan(locations)["indexed"] == 1
    before = records(locations)[0]
    reopened = LocationsStore(locations.path.parent, locations.state_dir)
    assert scan(reopened)["indexed"] == 1
    after = records(reopened)[0]
    assert (before["id"], before["fingerprint"]) == (after["id"], after["fingerprint"])
    with StateStore(locations.state_dir) as store:
        assert store.library_asset_count() == 1
        assert store.connection.execute("SELECT COUNT(*) FROM fetch_runs").fetchone()[0] == 1


def test_cloud_folder_survives_remount_but_rejects_a_different_remote_folder(tmp_path, monkeypatch):
    from latent.errors import ConfigurationError

    root = tmp_path / "mounted"
    root.mkdir()
    cloud = {"mount_point": str(root), "source_root": "/drive", "remote_id": "original-folder"}
    monkeypatch.setattr("latent.locations._cloud_folder_identity", lambda path: dict(cloud))
    identity = folder_identity(str(root))
    # CloudFS assigns fresh device and inode numbers after remounting.
    identity.update(device=-1, inode=-1)
    assert check_folder(identity) == root
    snapshot = {"folder": identity, "prefix": ""}
    local = tmp_path / "finished.jpg"
    image(local)
    copy_finished_photo(local, str(root / "trip/finished.jpg"), snapshot, sha256(local))
    assert sha256(root / "trip/finished.jpg") == sha256(local)

    cloud["remote_id"] = "replacement-folder"
    with pytest.raises(ConfigurationError, match="Folder or volume changed"):
        check_folder(identity)
    monkeypatch.setattr("latent.locations._cloud_folder_identity", lambda path: None)
    with pytest.raises(ConfigurationError, match="Folder or volume changed"):
        check_folder(identity)


def test_local_folder_still_requires_its_original_device_and_inode(tmp_path, monkeypatch):
    from latent.errors import ConfigurationError

    monkeypatch.setattr("latent.locations._cloud_folder_identity", lambda path: None)
    identity = folder_identity(str(tmp_path))
    identity["inode"] = -1
    with pytest.raises(ConfigurationError, match="Folder or volume changed"):
        check_folder(identity)


@pytest.mark.parametrize("overlap", ["same", "child", "ancestor"])
def test_output_and_sequence_trees_never_reenter_scanner(tmp_path, overlap):
    locations, original, _ = setup_library(tmp_path)
    out = {"same": original, "child": original / "finished", "ancestor": tmp_path}[overlap]
    out.mkdir(exist_ok=True)
    change(locations, "set_output", path=str(out))
    snapshot = locations.output_snapshot()
    assert snapshot["prefix"] == ("" if overlap == "child" else "_Latent Edits")
    image(original / "photos" / "one.jpg")
    image(out / snapshot["prefix"] / "photos" / "finished.jpg")
    image(original / "sequences" / "Album" / "copy.jpg")
    (original / "recursive").symlink_to(original, target_is_directory=True)
    assert scan(locations)["discovered"] == 1


def test_folder_output_selects_subset_and_preserves_original_hierarchy_and_working_files(tmp_path):
    locations, original, output = setup_library(tmp_path)
    for day in ["2025/trip", "2026/trip"]:
        image(original / day / "same.jpg")
    scan(locations)
    manager, batch = ready_batch(locations, tmp_path)
    try:
        folder = Path(batch["folder"])
        assert len({i["local_path"] for i in batch["items"]}) == 2
        for item in batch["items"]:
            image(folder / "exports" / (Path(item["local_path"]).stem + "_DxO.jpg"), "gold")
        (folder / "same.jpg.dop").write_text("edits")
        plan = manager.prepare(batch["id"])
        assert len(plan["uploads"]) == 2
        chosen = plan["uploads"][0]
        manager.finish(
            batch["id"],
            "",
            exports=[{"local_path": chosen["local_path"], "asset_id": chosen["asset_id"]}],
        )
        manager.worker.join(10)
        done = manager.get(batch["id"])
        assert done["status"] == "saved", done
        item = next(i for i in batch["items"] if i["asset_id"] == chosen["asset_id"])
        expected = output / Path(item["archive_relative"]).parent / Path(chosen["local_path"]).name
        assert expected.is_file()
        assert len(list(output.rglob("*.jpg"))) == 1
        assert all((folder / i["local_path"]).is_file() for i in batch["items"])
        assert (folder / "same.jpg.dop").read_text() == "edits"
        assert len(list(original.rglob("*.jpg"))) == 2
    finally:
        manager.close()


def test_renamed_exports_require_mapping_and_prepared_output_never_redirects(tmp_path):
    locations, original, output = setup_library(tmp_path)
    image(original / "2025" / "trip" / "one.jpg")
    scan(locations)
    manager, batch = ready_batch(locations, tmp_path)
    try:
        image(Path(batch["folder"]) / "renamed.jpg", "gold")
        plan = manager.prepare(batch["id"])
        upload = plan["uploads"][0]
        assert upload["asset_id"] is None
        selected = [{"local_path": "renamed.jpg", "asset_id": batch["items"][0]["asset_id"]}]
        with pytest.raises(ValueError, match="Match this export"):
            manager.finish(batch["id"], "", exports=selected)
        manager.map_exports(batch["id"], selected)
        other = tmp_path / "new-output"
        other.mkdir()
        change(locations, "set_output", path=str(other))
        manager.finish(batch["id"], "", exports=selected)
        manager.worker.join(10)
        assert manager.get(batch["id"])["status"] == "saved"
        assert (output / "2025/trip/renamed.jpg").is_file()
        assert list(other.iterdir()) == []
    finally:
        manager.close()


def test_copy_preserves_conflicts_and_refuses_changes_after_review(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    snapshot = {"folder": folder_identity(str(output)), "prefix": ""}
    local = tmp_path / "edited.jpg"
    local.write_bytes(b"finished")
    digest = sha256(local)
    (output / "photo.jpg").write_bytes(b"older finished version")
    target = destination(snapshot, "photo.jpg", digest)
    assert target != str(output / "photo.jpg")
    copy_finished_photo(local, target, snapshot, digest)
    copy_finished_photo(local, target, snapshot, digest)
    assert (output / "photo.jpg").read_bytes() == b"older finished version"
    assert Path(target).read_bytes() == b"finished"
    local.write_bytes(b"changed after review")
    with pytest.raises(ValueError, match="changed after review"):
        copy_finished_photo(local, target, snapshot, digest)
    assert Path(target).read_bytes() == b"finished"


def test_offline_replaced_roots_and_symlink_output_escape_are_rejected(tmp_path):
    locations, original, output = setup_library(tmp_path)
    snapshot = locations.output_snapshot()
    output.rename(tmp_path / "unmounted")
    output.mkdir()
    with pytest.raises(Exception, match="volume changed"):
        check_folder(snapshot["folder"])
    change(locations, "set_output", path=str(output))
    snapshot = locations.output_snapshot()
    (output / "escape").symlink_to(original, target_is_directory=True)
    with pytest.raises(ValueError, match="Symbolic links"):
        destination(snapshot, "escape/photo.jpg", "abc")
    assert list(original.iterdir()) == []


def test_relocated_source_keeps_identity_and_rejects_wrong_archive(tmp_path):
    locations, original, _ = setup_library(tmp_path)
    image(original / "trip/one.jpg")
    scan(locations)
    before = records(locations)[0]
    current = locations.get()
    with pytest.raises(ValueError, match="does not match"):
        change(
            locations, "reconnect_source", source_id=current["active_source_id"], path=str(tmp_path)
        )
    relocated = tmp_path / "moved-originals"
    original.rename(relocated)
    change(
        locations, "reconnect_source", source_id=current["active_source_id"], path=str(relocated)
    )
    scan(locations)
    assert records(locations)[0]["fingerprint"] == before["fingerprint"]
    assert records(locations)[0]["id"] == before["id"]


def test_legacy_mount_binding_retains_cached_fingerprint_and_asset_id(tmp_path):
    root = tmp_path / "originals"
    image(root / "2025/trip/one.jpg")
    photo = root / "2025/trip/one.jpg"
    asset = RemoteAsset(
        "cloud",
        "remote-id",
        "/Cloud/Archive/2025/trip/one.jpg",
        "one.jpg",
        photo.stat().st_size,
        "2025-01-01",
        {"2": hashlib.sha1(photo.read_bytes()).hexdigest()},
    )
    with StateStore(tmp_path / "index") as store:
        asset_id = store.upsert_asset(asset)
    locations = LocationsStore(tmp_path / "workspace", tmp_path / "index")
    # The migration infers the common containing folder from existing paths.
    data = locations.get()
    source = data["sources"][0]
    change(locations, "reconnect_source", source_id=source["id"], path=str(photo.parent))
    scan(locations)
    record = records(locations)[0]
    assert (record["id"], record["fingerprint"]) == (asset_id, asset.fingerprint)


def test_sequence_projection_preserves_hierarchy_order_and_unmanaged_files(tmp_path):
    locations, original, _ = setup_library(tmp_path)
    image(original / "2025/trip/one.jpg")
    image(original / "2026/trip/two.jpg", "gold")
    scan(locations)
    change(locations, "set_sequence_folder", path=str(tmp_path))
    with WorkspaceStore(locations.path.parent) as workspace:
        parent = workspace.create_folder("Travel")
        child = workspace.create_folder("Mountains", parent_id=parent["id"])
        sequence = workspace.create_sequence("Study", folder_id=child["id"])
        photos = [
            WorkspaceAsset(r["provider"], r["remote_path"], r["fingerprint"], r["name"])
            for r in records(locations)
        ]
        workspace.add_items(sequence["id"], photos)
    links = SequenceLinks(locations)
    result = links.sync(locations.get()["revision"])
    folder = tmp_path / "sequences/Travel/Mountains/Study"
    paths = sorted(folder.iterdir())
    assert result["links"] == 2
    assert all(path.is_symlink() for path in paths)
    assert paths[0].resolve() == original / "2025/trip/one.jpg"
    (folder / "user-notes.txt").write_text("preserve")
    with WorkspaceStore(locations.path.parent) as workspace:
        workspace.update_sequence(sequence["id"], name="Final")
    links.sync(locations.get()["revision"])
    assert (folder / "user-notes.txt").read_text() == "preserve"
    assert not any(p.is_symlink() for p in folder.iterdir())
    new_folder = folder.with_name("Final")
    assert len(list(new_folder.iterdir())) == 2
    replaced = next(new_folder.iterdir())
    replaced.unlink()
    replaced.write_text("user file")
    with pytest.raises(ValueError, match="unmanaged file"):
        links.sync(locations.get()["revision"])
    assert replaced.read_text() == "user file"


def test_filesystem_range_rejects_original_change(tmp_path):
    locations, original, _ = setup_library(tmp_path)
    image(original / "one.jpg")
    source = locations.get()["sources"][0]
    asset = filesystem_asset(source, "one.jpg")
    reader = FilesystemRangeSource(source, asset)
    os.utime(original / "one.jpg", ns=(1, 1))
    with pytest.raises(Exception, match="changed during"):
        reader.read_range(0, 100)


def test_previous_output_and_working_copies_stay_excluded_after_switching_output(tmp_path):
    locations, original, output = setup_library(tmp_path)
    finished = original / "finished"
    finished.mkdir()
    change(locations, "set_output", path=str(finished))
    image(original / "photo.jpg")
    image(finished / "export.jpg")
    change(locations, "set_output", path=str(output))
    with_locations = EditingManager(
        locations.path.parent, files_dir=original / "working", locations=locations
    )
    try:
        image(original / "working" / "downloaded.jpg")
        assert scan(locations)["discovered"] == 1
    finally:
        with_locations.close()


def test_matching_stems_get_distinct_working_sidecar_names(tmp_path):
    locations, original, _ = setup_library(tmp_path)
    image(original / "one.jpg")
    image(original / "one.png")
    scan(locations)
    manager, batch = ready_batch(locations, tmp_path)
    try:
        assert len({Path(i["local_path"]).stem for i in batch["items"]}) == 2
    finally:
        manager.close()


def test_failed_publication_never_exposes_an_incomplete_final_photo(tmp_path, monkeypatch):
    from latent import folder_output

    output = tmp_path / "output"
    output.mkdir()
    local = tmp_path / "photo.jpg"
    local.write_bytes(b"complete finished photo")
    snapshot = {"folder": folder_identity(str(output)), "prefix": ""}

    def failure(*_):
        raise OSError("Synthetic interrupted publication")

    monkeypatch.setattr(folder_output, "publish_without_replacing", failure)
    with pytest.raises(OSError, match="interrupted publication"):
        copy_finished_photo(local, str(output / "photo.jpg"), snapshot, sha256(local))
    assert list(output.iterdir()) == []
    assert local.read_bytes() == b"complete finished photo"


def test_other_workspace_cannot_take_over_managed_sequence_links(tmp_path):
    locations, _, _ = setup_library(tmp_path)
    change(locations, "set_sequence_folder", path=str(tmp_path))
    SequenceLinks(locations).sync(locations.get()["revision"])
    second = LocationsStore(tmp_path / "another-workspace", tmp_path / "another-index")
    change(second, "set_sequence_folder", path=str(tmp_path))
    with pytest.raises(ValueError, match="another library"):
        SequenceLinks(second).sync(second.get()["revision"])
