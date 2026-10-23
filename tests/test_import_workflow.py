from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import join_worker
from PIL import Image

from latent.archive_copies import ArchiveCopies
from latent.editing import EditingManager
from latent.embedding_backends import EmbeddingEngine, EmbeddingProfile
from latent.embedding_runs import EmbeddingRuns
from latent.embeddings import EmbeddingStore
from latent.errors import ConfigurationError
from latent.formats import PHOTO_EXTENSIONS
from latent.imports import ImportManager
from latent.ingest import FolderIndexer
from latent.locations import LocationsStore
from latent.storage import StateStore


def fixture_runs(state_dir, embedding_dir, workspace_dir, encoder=None, *, model_id="fixture"):
    profile = EmbeddingProfile(
        key="fixture", display_name="Fixture", model_id=model_id, dimensions=4,
        store_name=embedding_dir.name, provider="fixture",
    )
    engine = EmbeddingEngine(profile, embedding_dir, lambda: encoder)
    return EmbeddingRuns(
        state_dir, workspace_dir, engines={"fixture": engine}, default_backend=lambda: "fixture"
    )


def photo(path, color="navy"):
    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    exif[34665] = {36867: "2026:10:05 12:30:00"}
    Image.new("RGB", (90, 60), color).save(path, exif=exif)


def setup(tmp_path):
    locations = LocationsStore(tmp_path / "workspace", tmp_path / "state")
    roots = [tmp_path / "r2", tmp_path / "r5"]
    for index, root in enumerate(roots):
        photo(root / "DSC00001.JPG", "navy" if index == 0 else "gold")
        (root / "DSC00001.JPG.dop").write_text(f"camera {index} edits")
    output = tmp_path / "archive"
    output.mkdir()
    manager = ImportManager(locations)
    batch = prepared(manager, [str(p) for p in roots])
    return locations, roots, output, manager, batch


def prepared(manager, paths, request_id=None):
    """Add folders, wait for their background scan, and return the batch the request produced."""
    request_id = request_id or str(uuid4())
    manager.prepare(request_id, paths)
    if manager.worker:
        join_worker(manager.worker)
    return manager.get(manager.resolve(request_id))


def blocked_walk(monkeypatch, folder_name):
    """Hold the scan when it reaches one folder, like a slow cloud listing."""
    entered, release = threading.Event(), threading.Event()
    walk = os.walk

    def slow(top, *args, **kwargs):
        if Path(top).name == folder_name:
            entered.set()
            assert release.wait(10)
        yield from walk(top, *args, **kwargs)

    monkeypatch.setattr("latent.imports.os.walk", slow)
    return entered, release


def promptly(function):
    """Run a call that must not wait for a folder scan."""
    outcome = {}

    def run():
        try:
            outcome["value"] = function()
        except Exception as error:
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "the call waited for the folder scan"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def action(manager, batch, name, path=None):
    result = manager.action(batch["id"], name, manager.get(batch["id"])["revision"], path)
    if manager.worker:
        join_worker(manager.worker)
        assert not manager.busy
    return manager.get(result["id"])


def snapshots(roots):
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for root in roots
        for p in root.rglob("*")
        if p.is_file()
    }


def test_two_camera_import_keeps_sources_sidecars_identity_and_has_no_automatic_embeddings(
    tmp_path,
):
    locations, roots, output, manager, batch = setup(tmp_path)
    before = snapshots(roots)
    try:
        assert batch["photo_count"] == batch["sidecar_count"] == 2
        assert manager.prepare(batch["id"], [str(p) for p in roots])["id"] == batch["id"]
        assert not (locations.state_dir / "embeddings").exists()
        ready = action(manager, batch, "start")
        assert ready["preview_ready"] == 2
        ids = manager.asset_ids(batch["id"])
        with StateStore(locations.state_dir) as store:
            identities = [
                tuple(row)
                for row in store.connection.execute(
                    "SELECT id,provider,remote_path,fingerprint FROM assets ORDER BY id"
                )
            ]
        action(manager, batch, "destination", str(output))
        archived = action(manager, batch, "archive")
        assert archived["status"] == "archived", archived
        assert archived["archived"] == 4
        for root in roots:
            for original in root.iterdir():
                target = output / "2026/2026-10-05" / root.name / original.name
                assert target.read_bytes() == original.read_bytes()
        assert snapshots(roots) == before
        assert manager.asset_ids(batch["id"]) == ids
        assert action(manager, batch, "archive")["archived"] == 4
        with StateStore(locations.state_dir) as store:
            assert [
                tuple(row)
                for row in store.connection.execute(
                    "SELECT id,provider,remote_path,fingerprint FROM assets ORDER BY id"
                )
            ] == identities
        assert not (locations.state_dir / "embeddings").exists()
        assert len(list(output.rglob("*.JPG"))) == 2

        # A later scan of the archive must not create a second photo for each copy.
        locations.update(
            {
                "action": "add_source",
                "path": str(output),
                "expected_revision": locations.get()["revision"],
            }
        )
        scanner = FolderIndexer(locations)
        try:
            scanner.start(locations.get()["revision"])
            if scanner.worker:
                join_worker(scanner.worker)
            assert scanner.status()["indexed"] == 0
            with StateStore(locations.state_dir) as store:
                assert store.library_asset_count() == 2
        finally:
            scanner.close()
    finally:
        manager.close()


def test_import_collision_keeps_existing_file_and_associated_dop_name(tmp_path):
    _, roots, output, manager, batch = setup(tmp_path)
    blocked = output / "2026/2026-10-05/r2/DSC00001.JPG"
    photo(blocked, "red")
    old = blocked.read_bytes()
    try:
        action(manager, batch, "start")
        action(manager, batch, "destination", str(output))
        result = action(manager, batch, "archive")
        assert result["status"] == "archived"
        assert blocked.read_bytes() == old
        renamed = next(blocked.parent.glob("DSC00001~*.JPG"))
        assert renamed.read_bytes() == (roots[0] / "DSC00001.JPG").read_bytes()
        assert Path(str(renamed) + ".dop").read_text() == "camera 0 edits"
    finally:
        manager.close()


def test_pause_after_copy_resumes_without_copying_again(tmp_path, monkeypatch):
    locations, _, output, manager, batch = setup(tmp_path)
    from latent import imports

    original_copy = imports.copy_finished_photo
    calls = []

    def pause_once(*args, **kwargs):
        calls.append(args[1])
        original_copy(*args, **kwargs)
        if len(calls) == 1:
            manager.stop.set()

    monkeypatch.setattr(imports, "copy_finished_photo", pause_once)
    try:
        action(manager, batch, "start")
        action(manager, batch, "destination", str(output))
        paused = action(manager, batch, "archive")
        assert paused["status"] == "paused"
        assert paused["copied"] == 1
        manager.close()
        manager = ImportManager(locations)
        result = action(manager, batch, "resume")
        assert result["status"] == "archived", result
        assert len(calls) == len(set(calls)) == 4
    finally:
        manager.close()


def test_archiving_waits_for_cloud_hash_and_blocks_following_copies(tmp_path, monkeypatch):
    locations, _, output, manager, batch = setup(tmp_path)
    cloud_identity = {
        "mount_point": str(output),
        "remote_path": "/drive/photos",
        "remote_id": "folder",
    }
    monkeypatch.setattr(
        "latent.locations._cloud_folder_identity",
        lambda path: cloud_identity if path == output else None,
    )

    class Cloud:
        ready = False

        def verified(self, path, size, hashes):
            assert path.startswith("/drive/photos/")
            assert len(hashes["2"]) == 40
            return self.ready

    cloud = Cloud()
    manager.cloud = cloud
    manager.verification_timeout = 0
    try:
        action(manager, batch, "start")
        action(manager, batch, "destination", str(output))
        result = action(manager, batch, "archive")
        assert result["status"] == "waiting_for_cloud"
        assert result["archived"] == 0 and result["copied"] == 1
        assert len([p for p in output.rglob("*") if p.is_file()]) == 1
        assert not ArchiveCopies(locations.state_dir).paths()
        cloud.ready = True
        result = action(manager, batch, "resume")
        assert result["archived"] == 4
        assert len(ArchiveCopies(locations.state_dir).paths(cloud=True)) == 4
    finally:
        manager.close()


def test_changed_source_is_not_archived_and_trashed_photo_and_sidecar_are_skipped(tmp_path):
    locations, roots, output, manager, batch = setup(tmp_path)
    try:
        action(manager, batch, "start")
        action(manager, batch, "destination", str(output))
        with StateStore(locations.state_dir) as store:
            first = store.connection.execute("SELECT * FROM assets ORDER BY id").fetchone()
            store.connection.execute(
                "INSERT INTO hidden_assets VALUES (?,?,?)",
                (first["provider"], first["remote_path"], str(uuid4())),
            )
            store.connection.commit()
        photo(roots[1] / "DSC00001.JPG", "pink")
        failed = action(manager, batch, "archive")
        assert failed["status"] == "needs_attention"
        assert "changed" in failed["error"]
        assert not list(output.rglob("*.JPG"))
        assert manager.files(batch["id"])[0]["archive_state"] == "skipped"
    finally:
        manager.close()


def test_verified_archive_is_used_for_editing_when_portable_drive_is_offline(tmp_path):
    locations, roots, output, manager, batch = setup(tmp_path)
    editor = None
    try:
        action(manager, batch, "start")
        action(manager, batch, "destination", str(output))
        assert action(manager, batch, "archive")["status"] == "archived"
        with StateStore(locations.state_dir) as store:
            records = store.library_assets_by_ids(manager.asset_ids(batch["id"]))
        for root in roots:
            root.rename(root.with_name(root.name + "-offline"))
        editor = EditingManager(
            locations.path.parent, locations=locations, files_dir=tmp_path / "working"
        )
        edit = editor.create(records)
        join_worker(editor.worker)
        edit = editor.get(edit["id"])
        assert edit["status"] == "ready", edit
        for item in edit["items"]:
            assert Path(edit["folder"], item["local_path"] + ".dop").is_file()
    finally:
        if editor:
            editor.close()
        manager.close()


class Encoder:
    model_id = "fixture"

    def __init__(self):
        self.calls = []

    def encode_images(self, paths, batch_size):
        self.calls.extend(paths)
        return [[1, 0, 0, 0] for _ in paths]


def test_selected_embeddings_never_encode_unselected_pending_jobs_or_prune_existing_vectors(
    tmp_path,
):
    locations, _, _, manager, batch = setup(tmp_path)
    encoder = Encoder()
    embedding_dir = tmp_path / "embeddings"
    runs = fixture_runs(
        locations.state_dir, embedding_dir, locations.path.parent, encoder
    )
    try:
        action(manager, batch, "start")
        assets = runs._assets()
        with EmbeddingStore(embedding_dir, model_id="fixture", dimensions=4) as store:
            store.sync_assets(assets)
        plan = runs.prepare(str(uuid4()), "selected", [assets[0].asset_id])
        assert plan["to_generate"] == 1
        assert not encoder.calls
        runs.action(plan["id"], "start", plan["revision"])
        join_worker(runs.worker)
        assert runs.get(plan["id"])["status"] == "complete"
        assert len(encoder.calls) == 1
        with EmbeddingStore(embedding_dir, model_id="fixture", dimensions=4) as store:
            assert store.job_counts() == {"succeeded": 1, "pending": 1, "failed": 0, "running": 0}
        second = runs.prepare(str(uuid4()), "selected", [assets[1].asset_id])
        runs.action(second["id"], "start", second["revision"])
        join_worker(runs.worker)
        with EmbeddingStore(embedding_dir, model_id="fixture", dimensions=4) as store:
            assert len(store.vector_rows()) == 2
        all_photos = runs.prepare(str(uuid4()), "all")
        assert all_photos["reused"] == 2 and all_photos["to_generate"] == 0
        assert runs.prepare(str(uuid4()), "incremental")["selected"] == 0
    finally:
        manager.close()
        runs.close()


def test_embedding_review_rejects_removed_photo_without_any_api_call(tmp_path):
    locations, _, _, manager, batch = setup(tmp_path)
    encoder = Encoder()
    runs = fixture_runs(
        locations.state_dir, tmp_path / "embeddings", locations.path.parent, encoder
    )
    try:
        action(manager, batch, "start")
        plan = runs.prepare(str(uuid4()), "all")
        with StateStore(locations.state_dir) as store:
            row = store.connection.execute("SELECT * FROM assets LIMIT 1").fetchone()
            store.connection.execute(
                "INSERT INTO hidden_assets VALUES (?,?,?)",
                (row["provider"], row["remote_path"], str(uuid4())),
            )
            store.connection.commit()
        with pytest.raises(ValueError, match="removed or changed"):
            runs.action(plan["id"], "start", plan["revision"])
        assert not encoder.calls
    finally:
        manager.close()
        runs.close()


def test_heif_extensions_are_importable():
    assert {"hif", "heif", "heic"} <= PHOTO_EXTENSIONS


def test_embedding_review_rejects_an_index_from_another_model(tmp_path):
    locations, _, _, manager, batch = setup(tmp_path)
    runs = fixture_runs(
        locations.state_dir, tmp_path / "vectors", locations.path.parent,
        model_id="current-model",
    )
    try:
        action(manager, batch, "start")
        with EmbeddingStore(tmp_path / "vectors", model_id="old-model", dimensions=4):
            pass
        with pytest.raises(ConfigurationError, match="different model"):
            runs.prepare(str(uuid4()), "all")
        assert not list(runs.root.glob("*/plan.json"))
    finally:
        manager.close()
        runs.close()


def test_http_import_scope_and_embedding_confirmation(tmp_path):
    import json
    import threading
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from latent.search import VectorIndex
    from latent.web_server import LibraryServer

    roots = [tmp_path / "r2", tmp_path / "r5"]
    for root in roots:
        photo(root / "DSC00001.JPG")
    encoder = Encoder()
    server = LibraryServer(
        ("127.0.0.1", 0),
        tmp_path / "state",
        workspace_dir=tmp_path / "workspace",
        vector_index=VectorIndex(tmp_path / "vectors", model_id="fixture", dimensions=4),
    )
    server.engines["fixed"].encoder_factory = lambda: encoder
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(path, payload=None, headers=None):
        data = json.dumps(payload).encode() if payload is not None else None
        with urlopen(
            Request(
                base + path,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    **(headers or {}),
                },
            ),
            timeout=5,
        ) as response:
            return json.load(response)

    try:
        identity = request("/health")["data_id"]
        expected = {"expected_data_id": identity}
        payload = {**expected, "request_id": str(uuid4()), "paths": [str(roots[0])]}
        with pytest.raises(HTTPError) as denied:
            request("/api/imports", payload, {"Origin": "https://example.com"})
        assert denied.value.code == 403
        assert request("/api/imports", payload)["batch"]["status"] == "scanning"
        join_worker(server.imports.worker)
        # Repeating the request returns the batch its finished scan produced.
        batch = request("/api/imports", payload)["batch"]
        assert batch["status"] == "prepared"
        assert request("/api/assets")["total"] == 0
        action_body = {**expected, "expected_revision": batch["revision"]}
        with pytest.raises(HTTPError) as stale:
            request(
                f"/api/imports/{batch['id']}/start",
                {**action_body, "expected_data_id": "different"},
            )
        assert stale.value.code == 400
        request(f"/api/imports/{batch['id']}/start", action_body)
        join_worker(server.imports.worker)
        second_payload = {**payload, "request_id": str(uuid4()), "paths": [str(roots[1])]}
        request("/api/imports", second_payload)
        join_worker(server.imports.worker)
        second = request("/api/imports", second_payload)["batch"]
        request(
            f"/api/imports/{second['id']}/start",
            {**expected, "expected_revision": second["revision"]},
        )
        join_worker(server.imports.worker)
        assert request("/api/assets")["total"] == 2
        scoped = request(f"/api/assets?import_id={batch['id']}")
        assert scoped["total"] == 1
        assert request(f"/api/assets?import_id={batch['id']}&rating_min=4")["total"] == 0
        plan = request(
            "/api/embedding-runs",
            {
                **expected,
                "request_id": str(uuid4()),
                "scope": "import",
                "batch_id": batch["id"],
            },
        )["run"]
        assert plan["selected"] == plan["to_generate"] == 1
        assert not encoder.calls
        request(
            f"/api/embedding-runs/{plan['id']}/start",
            {**expected, "expected_revision": plan["revision"]},
        )
        join_worker(server.embedding_runs.worker)
        assert request("/api/embedding-runs")["runs"][0]["status"] == "complete"
        assert len(encoder.calls) == 1
        assert request("/api/imports")["data_id"] == identity
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


def test_embedding_pause_during_failed_batch_does_not_start_fallback_requests(tmp_path):
    locations, _, _, manager, batch = setup(tmp_path)
    encoder = Encoder()
    runs = fixture_runs(
        locations.state_dir, tmp_path / "embeddings", locations.path.parent, encoder
    )

    def interrupted(paths, batch_size):
        encoder.calls.append(list(paths))
        runs.stop.set()
        raise RuntimeError("The request failed while pausing")

    encoder.encode_images = interrupted
    try:
        action(manager, batch, "start")
        plan = runs.prepare(str(uuid4()), "all")
        runs.action(plan["id"], "start", plan["revision"])
        join_worker(runs.worker)
        assert runs.get(plan["id"])["status"] == "paused"
        assert len(encoder.calls) == 1
        with EmbeddingStore(tmp_path / "embeddings", model_id="fixture", dimensions=4) as store:
            assert store.job_counts()["pending"] == 2
    finally:
        manager.close()
        runs.close()


def test_previews_are_shared_between_legacy_scan_and_import_after_restart(tmp_path):
    locations, roots, _, manager, batch = setup(tmp_path)
    try:
        data = locations.get()
        locations.update(
            {
                "action": "select_source",
                "source_id": batch["sources"][0]["id"],
                "expected_revision": data["revision"],
            }
        )
        scanner = FolderIndexer(locations, manager)
        assert scanner.imports is manager
        scanner.start(locations.get()["revision"])
        join_worker(manager.worker)
        assert scanner.status()["indexed"] == 1
        assert manager.get(batch["id"])["preview_ready"] == 1
        with StateStore(locations.state_dir) as store:
            before = [
                tuple(row)
                for row in store.connection.execute("SELECT id,fingerprint FROM assets ORDER BY id")
            ]
        # Reproduce a prepared batch whose previews were made by the old scanner.
        with manager._db() as db:
            db.execute(
                "UPDATE files SET preview_state='pending',asset_id=NULL WHERE batch_id=?",
                (batch["id"],),
            )
        manager.close()
        manager = ImportManager(locations)
        assert manager.get(batch["id"])["preview_ready"] == 1
        with StateStore(locations.state_dir) as store:
            assert [
                tuple(row)
                for row in store.connection.execute("SELECT id,fingerprint FROM assets ORDER BY id")
            ] == before
            assert store.connection.execute("SELECT count(*) FROM fetch_runs").fetchone()[0] == 1
        assert not (locations.path.parent / "embedding-runs").exists()
        assert action(manager, batch, "start")["preview_ready"] == 2
        with StateStore(locations.state_dir) as store:
            assert store.connection.execute("SELECT count(*) FROM fetch_runs").fetchone()[0] == 2
    finally:
        manager.close()


def test_repeated_folder_selection_reuses_batch_and_binds_each_request(tmp_path):
    locations, roots, _, manager, batch = setup(tmp_path)
    request_id = str(uuid4())
    try:
        repeated = prepared(manager, [str(p) for p in reversed(roots)], request_id)
        assert repeated["id"] == batch["id"]
        assert len(manager.batches()) == 1
        assert manager.prepare(request_id, [str(p) for p in reversed(roots)])["id"] == batch["id"]
        with pytest.raises(ValueError, match="different folders"):
            manager.prepare(request_id, [str(roots[0])])
        action(manager, batch, "start")
        photo(roots[0] / "new.jpg")
        updated = prepared(manager, [str(p) for p in roots])
        assert updated["id"] != batch["id"]
        assert updated["photo_count"] == 3 and updated["preview_ready"] == 2
        assert updated["status"] == "prepared"
    finally:
        manager.close()


def test_missing_contacts_and_changed_originals_are_not_reported_as_reusable(tmp_path):
    locations, roots, _, manager, batch = setup(tmp_path)
    try:
        action(manager, batch, "start")
        with StateStore(locations.state_dir) as store:
            path = store.connection.execute(
                "SELECT relative_path FROM cache_entries WHERE variant='contact' ORDER BY asset_id"
            ).fetchone()[0]
        (locations.state_dir / "cache" / path).unlink()
        reused = prepared(manager, [str(p) for p in roots])
        assert reused["id"] == batch["id"] and reused["preview_ready"] == 1
        assert reused["status"] == "prepared"
        assert action(manager, reused, "start")["preview_ready"] == 2
        photo(roots[0] / "DSC00001.JPG", "red")
        changed = prepared(manager, [str(p) for p in roots])
        assert changed["id"] != batch["id"] and changed["preview_ready"] == 1
    finally:
        manager.close()


def test_legacy_scan_can_be_paused_and_resumed_through_import_http(tmp_path, monkeypatch):
    import json
    import threading
    from urllib.request import Request, urlopen

    from latent.preview import PreviewPipeline
    from latent.search import VectorIndex
    from latent.web_server import LibraryServer

    source = tmp_path / "camera"
    for index in range(3):
        photo(source / f"{index}.jpg")
    entered, release = threading.Event(), threading.Event()
    original_run = PreviewPipeline.run

    def blocked_run(self, *args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original_run(self, *args, **kwargs)

    monkeypatch.setattr(PreviewPipeline, "run", blocked_run)
    server = LibraryServer(
        ("127.0.0.1", 0),
        tmp_path / "state",
        workspace_dir=tmp_path / "workspace",
        vector_index=VectorIndex(tmp_path / "vectors", model_id="fixture", dimensions=4),
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def request(path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        with urlopen(
            Request(
                f"http://127.0.0.1:{server.server_port}" + path,
                data=data,
                headers={"Content-Type": "application/json"},
            ),
            timeout=5,
        ) as response:
            return json.load(response)

    try:
        expected = {"expected_data_id": request("/health")["data_id"]}
        batch = request(
            "/api/imports", {**expected, "request_id": str(uuid4()), "paths": [str(source)]}
        )["batch"]
        join_worker(server.imports.worker)
        locations = request("/api/locations")
        request("/api/locations/scan", {**expected, "expected_revision": locations["revision"]})
        assert entered.wait(3)
        # The legacy scan reopens the unchanged batch, then builds its previews.
        assert request("/api/locations")["indexing"]["import_id"] == batch["id"]
        assert server.folder_indexer.imports is server.imports
        preview_worker = server.imports.worker
        body = {**expected, "expected_revision": batch["revision"]}
        assert request(f"/api/imports/{batch['id']}/start", body)["batch"]["status"] == "indexing"
        assert server.imports.worker is preview_worker
        request(f"/api/imports/{batch['id']}/pause", body)
        release.set()
        join_worker(preview_worker)
        paused = request("/api/imports")["batches"][0]
        assert paused["status"] == "paused" and paused["preview_ready"] == 1
        request(f"/api/imports/{batch['id']}/resume", body)
        join_worker(server.imports.worker)
        complete = request("/api/imports")["batches"][0]
        assert complete["status"] == "ready" and complete["preview_ready"] == 3
        assert request("/api/embedding-runs")["runs"] == []
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(5)


def test_folder_scan_runs_in_the_background_and_reports_progress(tmp_path, monkeypatch):
    locations = LocationsStore(tmp_path / "workspace", tmp_path / "state")
    roots = [tmp_path / "r2", tmp_path / "r5"]
    for root in roots:
        photo(root / "DSC00001.JPG")
        (root / "DSC00001.JPG.dop").write_text("edits")
    entered, release = blocked_walk(monkeypatch, "r5")
    manager = ImportManager(locations)
    try:
        batch = promptly(lambda: manager.prepare(str(uuid4()), [str(p) for p in roots]))
        assert batch["status"] == "scanning"
        assert [source["name"] for source in batch["sources"]] == ["r2", "r5"]
        assert entered.wait(5)
        # A slow listing holds neither Locations nor the import list.
        promptly(locations.get)
        progress = promptly(lambda: manager.get(batch["id"]))
        assert (progress["scanned_files"], progress["scanned_photos"]) == (2, 1)
        assert progress["current_path"] == "r5"
        with pytest.raises(ValueError, match="Wait for the folder scan"):
            promptly(lambda: manager.prepare(str(uuid4()), [str(tmp_path / "r2")]))
        with pytest.raises(ValueError, match="Wait for the folder scan"):
            manager.action(batch["id"], "start", batch["revision"])
        release.set()
        join_worker(manager.worker)
        done = manager.get(batch["id"])
        assert done["status"] == "prepared"
        assert done["photo_count"] == done["sidecar_count"] == 2
        assert (done["scanned_files"], done["scanned_photos"]) == (4, 2)
        assert done["current_path"] == "" and not manager.busy
    finally:
        release.set()
        manager.close()


def test_canceling_a_folder_scan_discards_its_batch(tmp_path, monkeypatch):
    locations = LocationsStore(tmp_path / "workspace", tmp_path / "state")
    camera = tmp_path / "camera"
    photo(camera / "DSC00001.JPG")
    entered, release = blocked_walk(monkeypatch, "camera")
    manager = ImportManager(locations)
    try:
        batch = manager.prepare(str(uuid4()), [str(camera)])
        assert entered.wait(5)
        canceling = promptly(lambda: manager.action(batch["id"], "cancel", batch["revision"]))
        assert canceling["status"] == "canceling"
        release.set()
        join_worker(manager.worker)
        assert manager.batches() == [] and not manager.busy
        assert prepared(manager, [str(camera)])["status"] == "prepared"
    finally:
        release.set()
        manager.close()


def test_failed_or_stopped_scans_explain_themselves_and_can_be_removed(tmp_path, monkeypatch):
    locations = LocationsStore(tmp_path / "workspace", tmp_path / "state")
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "notes.txt").write_text("no photos here")
    camera = tmp_path / "camera"
    photo(camera / "DSC00001.JPG")
    manager = ImportManager(locations)
    release = threading.Event()
    try:
        failed = prepared(manager, [str(empty)])
        assert failed["status"] == "scan_failed"
        assert failed["error"] == "No supported photos were found in these folders"
        with pytest.raises(ValueError, match="Remove this batch"):
            manager.action(failed["id"], "start", failed["revision"])
        assert manager.action(failed["id"], "remove", failed["revision"])["status"] == "removed"
        assert manager.batches() == []

        entered, release = blocked_walk(monkeypatch, "camera")
        stopped = manager.prepare(str(uuid4()), [str(camera)])
        assert entered.wait(5)
        scan = manager.worker
        # Quitting must not wait for a slow listing to return.
        promptly(manager.close)
        manager = ImportManager(locations)
        release.set()
        join_worker(scan)
        stopped = manager.get(stopped["id"])
        assert stopped["status"] == "scan_failed"
        assert "stopped before it finished" in stopped["error"]
        assert manager.action(stopped["id"], "remove", stopped["revision"])["status"] == "removed"
        assert manager.batches() == []
    finally:
        release.set()
        manager.close()


def test_http_folder_scan_answers_at_once_and_keeps_locations_responsive(tmp_path, monkeypatch):
    import json
    from urllib.request import Request, urlopen

    from latent.search import VectorIndex
    from latent.web_server import LibraryServer

    camera = tmp_path / "camera"
    photo(camera / "DSC00001.JPG")
    entered, release = blocked_walk(monkeypatch, "camera")
    server = LibraryServer(
        ("127.0.0.1", 0),
        tmp_path / "state",
        workspace_dir=tmp_path / "workspace",
        vector_index=VectorIndex(tmp_path / "vectors", model_id="fixture", dimensions=4),
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def request(path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        with urlopen(
            Request(
                f"http://127.0.0.1:{server.server_port}" + path,
                data=data,
                headers={"Content-Type": "application/json"},
            ),
            timeout=5,
        ) as response:
            return json.load(response)

    try:
        expected = {"expected_data_id": request("/health")["data_id"]}
        payload = {**expected, "request_id": str(uuid4()), "paths": [str(camera)]}
        batch = request("/api/imports", payload)["batch"]
        assert batch["status"] == "scanning"
        assert entered.wait(5)
        # The app reads Locations and the import list while a slow folder is still listing.
        assert [source["name"] for source in request("/api/locations")["sources"]] == ["camera"]
        assert request("/api/imports")["batches"][0]["status"] == "scanning"
        body = {**expected, "expected_revision": batch["revision"]}
        canceled = request(f"/api/imports/{batch['id']}/cancel", body)["batch"]
        assert canceled["status"] == "canceling"
        release.set()
        join_worker(server.imports.worker)
        assert request("/api/imports")["batches"] == []
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(5)


def jpeg_bytes(size, color="navy"):
    import io

    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="JPEG", quality=90)
    return output.getvalue()


def fake_arw(path, embedded):
    """A RAW stand-in: a 1 KiB header, its embedded JPEG preview, then image data never read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"II*\x00" + b"\x00" * 1020 + embedded + b"\x00" * (3 * 1024 * 1024))


def stand_in_exiftool(monkeypatch, embedded):
    from latent.models import PreviewLocation, ProbeResult

    def probe(self, data, suffix):
        assert suffix == ".arw"
        return ProbeResult(
            PreviewLocation("PreviewImage", 1024, len(embedded)),
            {"DateTimeOriginal": "2026:08:29 16:05:00", "Model": "ILCE-7RM5", "Orientation": 1},
        )

    monkeypatch.setattr("latent.preview.ExifToolProbe.probe", probe)


def test_raw_jpeg_pairs_become_one_photo_and_archive_together(tmp_path, monkeypatch):
    embedded = jpeg_bytes((1616, 1080))
    stand_in_exiftool(monkeypatch, embedded)
    camera = tmp_path / "r5"
    fake_arw(camera / "DSC00001.ARW", embedded)
    photo(camera / "DSC00001.JPG", "gold")
    (camera / "DSC00001.ARW.dop").write_text("edits")
    # A JPEG without a RAW of the same name stays its own photo.
    photo(camera / "DSC00002.jpg", "navy")
    output = tmp_path / "archive"
    output.mkdir()
    locations = LocationsStore(tmp_path / "workspace", tmp_path / "state")
    manager = ImportManager(locations)
    try:
        batch = prepared(manager, [str(camera)])
        counts = (batch["photo_count"], batch["paired_count"], batch["sidecar_count"])
        assert counts == (2, 1, 1)
        ready = action(manager, batch, "start")
        assert ready["preview_ready"] == 2 and ready["preview_failed"] == 0, ready["failures"]
        with StateStore(locations.state_dir) as store:
            names = sorted(row[0] for row in store.connection.execute("SELECT name FROM assets"))
        assert names == ["DSC00001.ARW", "DSC00002.jpg"]
        action(manager, batch, "destination", str(output))
        archived = action(manager, batch, "archive")
        assert archived["status"] == "archived" and archived["archived"] == 4, archived
        # The camera's JPEG follows its RAW's capture date and name, not its own EXIF.
        shot = output / "2026/2026-08-29/r5"
        assert sorted(p.name for p in shot.iterdir()) == [
            "DSC00001.ARW",
            "DSC00001.ARW.dop",
            "DSC00001.JPG",
        ]
        assert (shot / "DSC00001.JPG").read_bytes() == (camera / "DSC00001.JPG").read_bytes()
        assert (output / "2026/2026-10-05/r5/DSC00002.jpg").is_file()
    finally:
        manager.close()


def test_cloud_folder_previews_read_only_bounded_ranges_and_cloud_previews(
    tmp_path, monkeypatch
):
    from pathlib import PurePosixPath

    from latent.models import RemoteAsset

    embedded = jpeg_bytes((1616, 1080))
    stand_in_exiftool(monkeypatch, embedded)
    mount = tmp_path / "CloudDrive" / "Photography"
    day = mount / "2026-08-29"
    fake_arw(day / "DSC00001.ARW", embedded)
    photo(day / "DSC00001.JPG", "gold")
    standalone = day / "IMG_0001.JPG"
    exif = Image.Exif()
    exif[34665] = {36867: "2026:08:29 18:00:00"}
    # Noise keeps this JPEG larger than the 64 KiB header read.
    Image.frombytes("RGB", (400, 300), os.urandom(400 * 300 * 3)).save(
        standalone, exif=exif, quality=95
    )
    cloud = {
        "mount_point": str(tmp_path / "CloudDrive"),
        "source_root": "/aDrive",
        "remote_path": "/aDrive/Photography",
        "remote_id": "folder-1",
    }
    monkeypatch.setattr(
        "latent.locations._cloud_folder_identity",
        lambda path: dict(cloud) if path == mount.resolve() else None,
    )
    reads = []
    display_preview = jpeg_bytes((640, 427), "gold")

    class FakeCloudFile:
        """CloudDrive's API: exact ranges from the cloud copy and a display-ready preview."""

        def __init__(self, remote_path):
            self.local = mount.resolve() / PurePosixPath(remote_path).relative_to(
                cloud["remote_path"]
            )
            self.asset = RemoteAsset(
                provider="cloud",
                remote_id=remote_path,
                remote_path=remote_path,
                name=self.local.name,
                size_bytes=self.local.stat().st_size,
                write_time="2026-08-29T10:00:00+00:00",
                file_hashes={},
            )
            self.range_requests = self.bytes_transferred = self.metadata_elapsed_ms = 0

        def read_range(self, start, length):
            reads.append((self.local.name, "range", length))
            self.range_requests += 1
            self.bytes_transferred += length
            return self.local.read_bytes()[start : start + length]

        def read_preview(self, maximum_bytes):
            reads.append((self.local.name, "preview", len(display_preview)))
            self.bytes_transferred += len(display_preview)
            return display_preview

    def through_the_mount(*_):
        raise AssertionError("a cloud original was read through the mount")

    monkeypatch.setattr("latent.filesystem.CloudDriveRangeSource", FakeCloudFile)
    monkeypatch.setattr("latent.filesystem.FilesystemRangeSource.read_range", through_the_mount)
    locations = LocationsStore(tmp_path / "workspace", tmp_path / "state")
    manager = ImportManager(locations)
    try:
        batch = prepared(manager, [str(mount)])
        assert (batch["photo_count"], batch["paired_count"]) == (2, 1)
        ready = action(manager, batch, "start")
        assert ready["preview_ready"] == 2 and ready["preview_failed"] == 0, ready["failures"]
        by_file = {}
        for name, kind, size in reads:
            by_file.setdefault(name, []).append((kind, size))
        assert "DSC00001.JPG" not in by_file
        raw = by_file["DSC00001.ARW"]
        assert {kind for kind, _ in raw} == {"range"}
        assert sum(size for _, size in raw) <= 512 * 1024 < (day / "DSC00001.ARW").stat().st_size
        header, preview = by_file["IMG_0001.JPG"]
        assert header == ("range", 64 * 1024) and preview[0] == "preview"
        assert header[1] < standalone.stat().st_size
    finally:
        manager.close()


def test_video_thumbnails_and_dxo_exports_are_archived_but_not_shown(tmp_path, monkeypatch):
    embedded = jpeg_bytes((1616, 1080))
    stand_in_exiftool(monkeypatch, embedded)
    camera = tmp_path / "r5"
    fake_arw(camera / "DSC00001.ARW", embedded)
    photo(camera / "DSC00001_DxO.jpg", "gold")
    Image.new("RGB", (90, 60), "gold").save(camera / "DSC00009_DxO_1.TIF")
    photo(camera / "C0423T01.JPG", "gold")
    (camera / "C0423.MP4").write_bytes(b"video")
    photo(camera / "DSC00002.jpg", "navy")
    # Time-lapse frames are camera originals and stay photos.
    photo(camera / "颐和园日落-延时" / "DSC05150.JPG", "navy")
    output = tmp_path / "archive"
    output.mkdir()
    locations = LocationsStore(tmp_path / "workspace", tmp_path / "state")
    manager = ImportManager(locations)
    try:
        batch = prepared(manager, [str(camera)])
        assert (batch["photo_count"], batch["derivative_count"], batch["other_count"]) == (3, 3, 1)
        ready = action(manager, batch, "start")
        assert ready["preview_ready"] == 3 and ready["preview_failed"] == 0, ready["failures"]
        with StateStore(locations.state_dir) as store:
            names = sorted(row[0] for row in store.connection.execute("SELECT name FROM assets"))
        assert names == ["DSC00001.ARW", "DSC00002.jpg", "DSC05150.JPG"]
        action(manager, batch, "destination", str(output))
        archived = action(manager, batch, "archive")
        assert archived["status"] == "archived" and archived["archived"] == 7, archived
        archived_files = sorted(
            p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()
        )
        assert archived_files == [
            # A DxO export follows its RAW; one without a RAW has no date to follow.
            "2026/2026-08-29/r5/DSC00001.ARW",
            "2026/2026-08-29/r5/DSC00001_DxO.jpg",
            "2026/2026-10-05/r5/DSC00002.jpg",
            "2026/2026-10-05/r5/颐和园日落-延时/DSC05150.JPG",
            "Undated/r5/C0423.MP4",
            "Undated/r5/C0423T01.JPG",
            "Undated/r5/DSC00009_DxO_1.TIF",
        ]
    finally:
        manager.close()
