from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from latent.archive_copies import ArchiveCopies
from latent.editing import EditingManager
from latent.embedding_runs import EmbeddingRuns
from latent.embeddings import EmbeddingStore
from latent.errors import ConfigurationError
from latent.formats import PHOTO_EXTENSIONS
from latent.imports import ImportManager
from latent.ingest import FolderIndexer
from latent.locations import LocationsStore
from latent.storage import StateStore


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
    batch = manager.prepare(str(uuid4()), [str(p) for p in roots])
    return locations, roots, output, manager, batch


def action(manager, batch, name, path=None):
    result = manager.action(batch["id"], name, manager.get(batch["id"])["revision"], path)
    if manager.worker:
        manager.worker.join(10)
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
                scanner.worker.join(10)
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
        editor.worker.join(10)
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
    runs = EmbeddingRuns(
        locations.state_dir,
        embedding_dir,
        locations.path.parent,
        encoder_factory=lambda: encoder,
        model_id="fixture",
        dimensions=4,
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
        runs.worker.join(10)
        assert runs.get(plan["id"])["status"] == "complete"
        assert len(encoder.calls) == 1
        with EmbeddingStore(embedding_dir, model_id="fixture", dimensions=4) as store:
            assert store.job_counts() == {"succeeded": 1, "pending": 1, "failed": 0, "running": 0}
        second = runs.prepare(str(uuid4()), "selected", [assets[1].asset_id])
        runs.action(second["id"], "start", second["revision"])
        runs.worker.join(10)
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
    runs = EmbeddingRuns(
        locations.state_dir,
        tmp_path / "embeddings",
        locations.path.parent,
        encoder_factory=lambda: encoder,
        model_id="fixture",
        dimensions=4,
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
    runs = EmbeddingRuns(
        locations.state_dir,
        tmp_path / "vectors",
        locations.path.parent,
        model_id="current-model",
        dimensions=4,
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
    server.embedding_runs.encoder_factory = lambda: encoder
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
        server.imports.worker.join(10)
        second = request(
            "/api/imports",
            {
                **payload,
                "request_id": str(uuid4()),
                "paths": [str(roots[1])],
            },
        )["batch"]
        request(
            f"/api/imports/{second['id']}/start",
            {**expected, "expected_revision": second["revision"]},
        )
        server.imports.worker.join(10)
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
        server.embedding_runs.worker.join(10)
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
    runs = EmbeddingRuns(
        locations.state_dir,
        tmp_path / "embeddings",
        locations.path.parent,
        encoder_factory=lambda: encoder,
        model_id="fixture",
        dimensions=4,
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
        runs.worker.join(10)
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
        manager.worker.join(10)
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
        repeated = manager.prepare(request_id, [str(p) for p in reversed(roots)])
        assert repeated["id"] == batch["id"]
        assert len(manager.batches()) == 1
        assert manager.prepare(request_id, [str(p) for p in reversed(roots)])["id"] == batch["id"]
        with pytest.raises(ValueError, match="different folders"):
            manager.prepare(request_id, [str(roots[0])])
        action(manager, batch, "start")
        photo(roots[0] / "new.jpg")
        updated = manager.prepare(str(uuid4()), [str(p) for p in roots])
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
        reused = manager.prepare(str(uuid4()), [str(p) for p in roots])
        assert reused["id"] == batch["id"] and reused["preview_ready"] == 1
        assert reused["status"] == "prepared"
        assert action(manager, reused, "start")["preview_ready"] == 2
        photo(roots[0] / "DSC00001.JPG", "red")
        changed = manager.prepare(str(uuid4()), [str(p) for p in roots])
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
        locations = request("/api/locations")
        legacy = request(
            "/api/locations/scan", {**expected, "expected_revision": locations["revision"]}
        )
        assert entered.wait(3)
        assert legacy["indexing"]["import_id"] == batch["id"]
        assert server.folder_indexer.imports is server.imports
        preview_worker = server.imports.worker
        body = {**expected, "expected_revision": batch["revision"]}
        assert request(f"/api/imports/{batch['id']}/start", body)["batch"]["status"] == "indexing"
        assert server.imports.worker is preview_worker
        request(f"/api/imports/{batch['id']}/pause", body)
        release.set()
        preview_worker.join(10)
        paused = request("/api/imports")["batches"][0]
        assert paused["status"] == "paused" and paused["preview_ready"] == 1
        request(f"/api/imports/{batch['id']}/resume", body)
        server.imports.worker.join(10)
        complete = request("/api/imports")["batches"][0]
        assert complete["status"] == "ready" and complete["preview_ready"] == 3
        assert request("/api/embedding-runs")["runs"] == []
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(5)
