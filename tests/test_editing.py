from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from latent.editing import EditingManager
from latent.models import RemoteAsset
from latent.provider import MemoryRangeSource


class FakeCloud:
    def __init__(self):
        self.bodies = {"/archive/DSC001.ARW": b"original RAW bytes"}
        self.confirmed = True
        self.sources = []
        self.uploads = []

    def source(self, path):
        self.sources.append(path)
        body = self.bodies[path]
        asset = RemoteAsset(
            "cloud",
            path,
            path,
            Path(path).name,
            len(body),
            file_hashes={"2": hashlib.sha1(body).hexdigest()},
        )
        return MemoryRangeSource(asset, body)

    def sidecars(self, path):
        return [name for name in self.bodies if name == path + ".dop"]

    def ensure_folder(self, parent, name):
        return parent + "/" + name

    def upload(self, local, destination, expected):
        self.uploads.append(destination)
        self.bodies[destination] = local.read_bytes()

    def verified(self, destination, size, hashes):
        body = self.bodies.get(destination, b"")
        return (
            self.confirmed
            and len(body) == size
            and hashlib.sha256(body).hexdigest() == hashes["sha256"]
        )


def ready_batch(manager, cloud):
    source = cloud.source("/archive/DSC001.ARW")
    batch = manager.create(
        [
            {
                "id": 1,
                "remote_path": source.asset.remote_path,
                "name": source.asset.name,
                "fingerprint": source.asset.fingerprint,
                "size_bytes": source.asset.size_bytes,
            }
        ]
    )
    manager.worker.join(2)
    batch = manager.get(batch["id"])
    assert batch["status"] == "ready", batch
    return batch


def add_edits(batch):
    folder = Path(batch["folder"])
    (folder / "DSC001.ARW.dop").write_bytes(b"PhotoLab edits")
    (folder / "exports").mkdir()
    (folder / "exports" / "DSC001.jpg").write_bytes(b"finished JPEG")
    return folder


def test_editing_revision_allows_one_prepare_and_requires_a_new_review_for_finish(tmp_path):
    cloud = FakeCloud()
    manager = EditingManager(tmp_path, cloud=cloud)
    try:
        batch = ready_batch(manager, cloud)
        folder = add_edits(batch)

        def prepare_once(_):
            try:
                return manager.apply_action(
                    batch["id"], "prepare", expected_updated_at=batch["updated_at"]
                )
            except ValueError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(prepare_once, range(2)))
        prepared = [response for response in responses if isinstance(response, dict)]
        rejected = [response for response in responses if isinstance(response, str)]
        assert len(prepared) == len(rejected) == 1
        assert "changed" in rejected[0]
        first = prepared[0]
        second = manager.apply_action(
            batch["id"], "prepare", expected_updated_at=first["updated_at"]
        )
        assert second["upload_destination"] != first["upload_destination"]
        with pytest.raises(ValueError, match="changed"):
            manager.apply_action(
                batch["id"], "finish", policy="clear_batch", expected_updated_at=first["updated_at"]
            )
        assert manager.get(batch["id"])["status"] == "prepared"
        assert cloud.uploads == []
        assert (folder / "DSC001.ARW").read_bytes() == b"original RAW bytes"
    finally:
        manager.close()


def test_verified_upload_removes_raw_but_keeps_exports_and_restores_edits(tmp_path):
    cloud = FakeCloud()
    manager = EditingManager(tmp_path, cloud=cloud)
    try:
        batch = ready_batch(manager, cloud)
        folder = add_edits(batch)
        prepared = manager.prepare(batch["id"])
        assert len(prepared["uploads"]) == 2
        assert all("/_Latent Edits/" in item["remote_path"] for item in prepared["uploads"])
        manager.finish(batch["id"], "keep_exports")
        manager.worker.join(2)
        assert manager.get(batch["id"])["status"] == "complete"
        assert not (folder / "DSC001.ARW").exists()
        assert (folder / "exports" / "DSC001.jpg").read_bytes() == b"finished JPEG"
        assert cloud.bodies["/archive/DSC001.ARW"] == b"original RAW bytes"
        reopened = ready_batch(manager, cloud)
        assert (Path(reopened["folder"]) / "DSC001.ARW.dop").read_bytes() == b"PhotoLab edits"
    finally:
        manager.close()


def test_external_working_folder_survives_restart_cleanup_and_destination_changes(tmp_path):
    cloud = FakeCloud()
    workspace = tmp_path / "workspace"
    pictures = tmp_path / "Pictures" / "Latent"
    manager = EditingManager(workspace, files_dir=pictures, cloud=cloud)
    try:
        batch = ready_batch(manager, cloud)
        folder = add_edits(batch)
        assert folder.parent == pictures
        assert folder.name == f"{batch['created_at'][:10]}-{batch['id'][:8]}"
        assert (workspace / "editing" / batch["id"] / "manifest.json").is_file()
        assert not (workspace / "editing" / batch["id"] / "files").exists()
        manager.prepare(batch["id"])
    finally:
        manager.close()

    later_destination = tmp_path / "Other Pictures"
    reopened = EditingManager(workspace, files_dir=later_destination, cloud=cloud)
    try:
        restored = reopened.get(batch["id"])
        assert restored["folder"] == str(folder)
        assert restored["status"] == "prepared"
        reopened.finish(batch["id"], "keep_exports")
        reopened.worker.join(2)
        assert reopened.get(batch["id"])["status"] == "complete"
        assert not (folder / "DSC001.ARW").exists()
        assert (folder / "exports" / "DSC001.jpg").read_bytes() == b"finished JPEG"
        new_batch = ready_batch(reopened, cloud)
        new_folder = Path(new_batch["folder"])
        assert new_folder.parent == later_destination
        assert (new_folder / "DSC001.ARW.dop").read_bytes() == b"PhotoLab edits"
        assert cloud.bodies["/archive/DSC001.ARW"] == b"original RAW bytes"
    finally:
        reopened.close()


def test_legacy_batch_stays_in_place_until_its_saved_folder_is_relocated(tmp_path):
    cloud = FakeCloud()
    workspace = tmp_path / "workspace"
    manager = EditingManager(workspace, cloud=cloud)
    try:
        batch = ready_batch(manager, cloud)
        folder = add_edits(batch)
    finally:
        manager.close()
    manifest = workspace / "editing" / batch["id"] / "manifest.json"
    saved = json.loads(manifest.read_text())
    saved.pop("folder")
    manifest.write_text(json.dumps(saved))

    pictures = tmp_path / "Pictures" / "Latent"
    reopened = EditingManager(workspace, files_dir=pictures, cloud=cloud)
    try:
        assert reopened.get(batch["id"])["folder"] == str(folder)
        assert not pictures.exists()
    finally:
        reopened.close()

    # An explicit offline move can retain the same durable batch and all edits.
    destination = pictures / f"{batch['created_at'][:10]}-{batch['id'][:8]}"
    destination.parent.mkdir(parents=True)
    folder.rename(destination)
    saved["folder"] = str(destination)
    manifest.write_text(json.dumps(saved))
    relocated = EditingManager(workspace, files_dir=pictures, cloud=cloud)
    try:
        prepared = relocated.prepare(batch["id"])
        assert prepared["folder"] == str(destination)
        assert {item["local_path"] for item in prepared["uploads"]} == {
            "DSC001.ARW.dop",
            "exports/DSC001.jpg",
        }
        assert (destination / "DSC001.ARW").read_bytes() == b"original RAW bytes"
        assert cloud.uploads == []
    finally:
        relocated.close()


def test_unconfirmed_cloud_copy_waits_then_verification_keeps_every_local_file(tmp_path):
    cloud = FakeCloud()
    cloud.confirmed = False
    manager = EditingManager(tmp_path, cloud=cloud, verification_timeout=0)
    try:
        batch = ready_batch(manager, cloud)
        folder = add_edits(batch)
        manager.prepare(batch["id"])
        manager.finish(batch["id"], "clear_batch")
        manager.worker.join(2)
        assert manager.get(batch["id"])["status"] == "waiting_cloud"
        assert manager.get(batch["id"])["error"] is None
        assert len([p for p in folder.rglob("*") if p.is_file()]) == 3
        cloud.confirmed = True
        manager.verify(batch["id"])
        manager.worker.join(2)
        assert manager.get(batch["id"])["status"] == "cloud_verified"
        assert len([p for p in folder.rglob("*") if p.is_file()]) == 3
        assert len(cloud.uploads) == 2
    finally:
        manager.close()


def test_edit_changed_after_review_is_kept_and_can_be_reviewed_again(tmp_path):
    cloud = FakeCloud()
    manager = EditingManager(tmp_path, cloud=cloud)
    try:
        batch = ready_batch(manager, cloud)
        folder = add_edits(batch)
        first = manager.prepare(batch["id"])
        (folder / "DSC001.ARW.dop").write_bytes(b"new unsaved snapshot")
        manager.finish(batch["id"], "keep_exports")
        manager.worker.join(2)
        assert manager.get(batch["id"])["status"] == "needs_attention"
        assert (folder / "DSC001.ARW").exists()
        second = manager.prepare(batch["id"])
        assert first["uploads"][0]["remote_path"] != second["uploads"][0]["remote_path"]
        with pytest.raises(ValueError, match="Choose"):
            manager.finish(batch["id"], "")
        assert (folder / "DSC001.ARW").exists()
    finally:
        manager.close()


def test_cloud_confirmation_is_polled_before_cleanup(tmp_path):
    cloud = FakeCloud()
    original_verified = cloud.verified
    calls = []

    def delayed(destination, size, hashes):
        calls.append(destination)
        return len(calls) > 2 and original_verified(destination, size, hashes)

    cloud.verified = delayed
    manager = EditingManager(
        tmp_path, cloud=cloud, verification_timeout=0.2, verification_interval=0.001
    )
    try:
        batch = ready_batch(manager, cloud)
        folder = add_edits(batch)
        manager.prepare(batch["id"])
        manager.finish(batch["id"], "keep_exports")
        manager.worker.join(2)
        assert manager.get(batch["id"])["status"] == "complete"
        assert len(calls) >= 6
        assert not (folder / "DSC001.ARW").exists()
        assert (folder / "exports/DSC001.jpg").is_file()
    finally:
        manager.close()


def test_legacy_hash_error_can_be_verified_without_upload_or_cleanup(tmp_path):
    cloud = FakeCloud()
    cloud.confirmed = False
    manager = EditingManager(tmp_path, cloud=cloud, verification_timeout=0)
    try:
        batch = ready_batch(manager, cloud)
        folder = add_edits(batch)
        manager.prepare(batch["id"])
        manager.finish(batch["id"], "clear_batch")
        manager.worker.join(2)
        failed = manager.get(batch["id"])
        failed.update(
            status="needs_attention", error="CloudDrive has not confirmed all cloud hashes"
        )
        manager._save(failed)
        before = {
            str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()
        }
        cloud.confirmed = True
        manager.apply_action(batch["id"], "verify", expected_updated_at=failed["updated_at"])
        manager.worker.join(2)
        assert manager.get(batch["id"])["status"] == "cloud_verified"
        assert manager.get(batch["id"])["error"] is None
        assert {
            str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()
        } == before
        assert len(cloud.uploads) == 2
    finally:
        manager.close()
    reopened = EditingManager(tmp_path, cloud=cloud)
    try:
        assert reopened.get(batch["id"])["status"] == "cloud_verified"
    finally:
        reopened.close()


def test_partial_download_never_becomes_ready_and_can_resume_after_restart(tmp_path):
    cloud = FakeCloud()
    original_source = cloud.source

    class BrokenSource(MemoryRangeSource):
        def read_range(self, start, length):
            return b"short"

    source = original_source("/archive/DSC001.ARW")
    cloud.source = lambda _: BrokenSource(source.asset, source.data)
    manager = EditingManager(tmp_path, cloud=cloud)
    batch = manager.create(
        [
            {
                "id": 1,
                "remote_path": source.asset.remote_path,
                "name": source.asset.name,
                "fingerprint": source.asset.fingerprint,
                "size_bytes": source.asset.size_bytes,
            }
        ]
    )
    manager.worker.join(2)
    assert manager.get(batch["id"])["status"] == "needs_attention"
    assert not (Path(manager.get(batch["id"])["folder"]) / "DSC001.ARW").exists()
    manager.close()
    cloud.source = original_source
    reopened = EditingManager(tmp_path, cloud=cloud)
    try:
        reopened.resume(batch["id"])
        reopened.worker.join(2)
        assert reopened.get(batch["id"])["status"] == "ready"
    finally:
        reopened.close()


def test_duplicate_basenames_share_one_folder_without_overwriting(tmp_path):
    cloud = FakeCloud()
    cloud.bodies["/archive/older/DSC001.ARW"] = b"different RAW bytes"
    records = []
    for index, path in enumerate(cloud.bodies, start=1):
        source = cloud.source(path)
        records.append(
            {
                "id": index,
                "remote_path": path,
                "name": source.asset.name,
                "fingerprint": source.asset.fingerprint,
                "size_bytes": source.asset.size_bytes,
            }
        )
    manager = EditingManager(tmp_path, cloud=cloud)
    try:
        batch = manager.create(records)
        manager.worker.join(2)
        batch = manager.get(batch["id"])
        assert batch["status"] == "ready"
        names = [item["local_path"] for item in batch["items"]]
        assert len(set(names)) == 2
        assert all(Path(name).parent == Path(".") for name in names)
        assert {p.read_bytes() for p in Path(batch["folder"]).glob("*.ARW")} == set(
            cloud.bodies.values()
        )
    finally:
        manager.close()


@pytest.mark.parametrize("saved_status", ["needs_attention", "uploaded", "cleaning"])
def test_cleanup_can_resume_after_restart_without_redownloading_raws(tmp_path, saved_status):
    cloud = FakeCloud()
    manager = EditingManager(tmp_path, cloud=cloud)
    batch = ready_batch(manager, cloud)
    folder = add_edits(batch)
    manager.prepare(batch["id"])
    actual_cleanup = manager._cleanup
    manager._cleanup = lambda batch: (_ for _ in ()).throw(OSError("Simulated stop before cleanup"))
    manager.finish(batch["id"], "keep_exports")
    manager.worker.join(2)
    assert manager.get(batch["id"])["phase"] == "cleanup"
    interrupted = manager.get(batch["id"])
    interrupted["status"] = saved_status
    manager._save(interrupted)
    manager._cleanup = actual_cleanup
    manager.close()
    before = len(cloud.sources)
    reopened = EditingManager(tmp_path, cloud=cloud)
    try:
        reopened.resume(batch["id"])
        reopened.worker.join(2)
        assert reopened.get(batch["id"])["status"] == "complete"
        assert len(cloud.sources) == before
        assert not (folder / "DSC001.ARW").exists()
        assert (folder / "exports/DSC001.jpg").is_file()
    finally:
        reopened.close()


def test_xmp_rating_caption_transfer_preserves_original_and_existing_metadata(tmp_path):
    import json
    import shutil
    import subprocess

    from PIL import Image

    if not shutil.which("exiftool"):
        pytest.skip("ExifTool is required for the real XMP integration check")
    original = tmp_path / "source.jpg"
    Image.new("RGB", (8, 8), color="red").save(original)
    subprocess.run(
        ["exiftool", "-overwrite_original", "-XMP-xmp:Rating=0", str(original)],
        capture_output=True,
        check=True,
    )
    before = original.read_bytes()
    manager = EditingManager(tmp_path / "workspace")
    try:
        manager._write_metadata({"annotation": {"rating": 4, "caption": "Revisit 光"}}, original)
        result = subprocess.run(
            ["exiftool", "-j", "-Rating", "-Description", str(original.with_suffix(".xmp"))],
            capture_output=True,
            text=True,
            check=True,
        )
        fields = json.loads(result.stdout)[0]
        assert fields["Rating"] == 4
        assert fields["Description"] == "Revisit 光"
        manager._write_metadata({"annotation": {"rating": 3, "caption": "Second pass"}}, original)
        assert original.read_bytes() == before
        assert not original.with_suffix(".xmp_original").exists()
    finally:
        manager.close()
