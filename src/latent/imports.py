"""Explicit folder imports with resumable previews and verified archive copies."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from .archive_copies import ArchiveCopies
from .editing import EditingCloud, file_hashes
from .errors import ConfigurationError
from .filesystem import FilesystemRangeSource, filesystem_asset
from .folder_output import copy_finished_photo, sha256
from .folder_output import destination as copy_destination
from .formats import PHOTO_EXTENSIONS, SIDECAR_EXTENSIONS
from .locations import LocationsStore, check_folder, contained_path, folder_identity, row_asset
from .preview import PreviewPipeline
from .sequence_links import filename
from .storage import CacheManager, StateStore
from .workspace import utc_now


class ImportPaused(Exception):
    pass


class CloudConfirmationPending(Exception):
    pass


class ImportManager:
    def __init__(
        self,
        locations: LocationsStore,
        *,
        cloud=None,
        verification_timeout: float = 90,
        verification_interval: float = 3,
    ) -> None:
        self.locations = locations
        self.path = locations.path.parent / "imports.sqlite"
        self.copies = ArchiveCopies(locations.state_dir)
        self.cloud = cloud or EditingCloud()
        self.verification_timeout = verification_timeout
        self.verification_interval = verification_interval
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None
        self.active_id: str | None = None
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, revision TEXT NOT NULL,
                    status TEXT NOT NULL, phase TEXT NOT NULL, sources_json TEXT NOT NULL,
                    destination_json TEXT, error TEXT, current_path TEXT NOT NULL DEFAULT '',
                    ignored INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
                    source_id TEXT NOT NULL, relative TEXT NOT NULL, name TEXT NOT NULL,
                    extension TEXT NOT NULL, role TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL, asset_id INTEGER, capture_day TEXT,
                    preview_state TEXT NOT NULL DEFAULT 'pending', preview_error TEXT,
                    archive_state TEXT NOT NULL DEFAULT 'pending', archive_error TEXT,
                    archive_relative TEXT, hashes_json TEXT,
                    UNIQUE(batch_id, source_id, relative)
                );
                CREATE INDEX IF NOT EXISTS import_files_batch ON files(batch_id, id);
                CREATE TABLE IF NOT EXISTS import_requests (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
                    paths_json TEXT NOT NULL
                );
            """)
            db.execute(
                "UPDATE batches SET status='interrupted', "
                "error='Import interrupted. Resume when the source is available.' "
                "WHERE status IN ('indexing','archiving','verifying')"
            )
        self.reconcile_cached_previews()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @property
    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def close(self) -> None:
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=35)

    def _batch(self, identifier: str) -> dict:
        if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
            raise ValueError("Invalid import ID")
        with self._db() as db:
            row = db.execute("SELECT * FROM batches WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError("Import batch not found")
        batch = dict(row)
        batch["sources"] = json.loads(batch.pop("sources_json"))
        batch["destination"] = json.loads(batch.pop("destination_json") or "null")
        return batch

    def files(self, identifier: str) -> list[dict]:
        with self._db() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM files WHERE batch_id=? ORDER BY id", (identifier,)
                )
            ]

    def asset_ids(self, identifier: str) -> list[int]:
        self._batch(identifier)
        with self._db() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT asset_id FROM files WHERE batch_id=? "
                    "AND asset_id IS NOT NULL ORDER BY id",
                    (identifier,),
                )
            ]

    def get(self, identifier: str) -> dict:
        batch = self._batch(identifier)
        items = self.files(identifier)
        photos = [i for i in items if i["role"] == "photo"]
        batch.update(
            file_count=len(items),
            photo_count=len(photos),
            sidecar_count=sum(i["role"] == "sidecar" for i in items),
            other_count=sum(i["role"] == "other" for i in items),
            bytes_total=sum(i["size_bytes"] for i in items),
            preview_ready=sum(i["preview_state"] == "ready" for i in photos),
            preview_failed=sum(i["preview_state"] == "failed" for i in photos),
            archived=sum(i["archive_state"] == "verified" for i in items),
            skipped=sum(i["archive_state"] == "skipped" for i in items),
            copied=sum(i["archive_state"] == "copied" for i in items),
            archived_bytes=sum(i["size_bytes"] for i in items if i["archive_state"] == "verified"),
            undated=sum(i["role"] == "photo" and not i["capture_day"] for i in items),
            failures=[
                {"name": i["name"], "message": i["archive_error"] or i["preview_error"]}
                for i in items
                if i["archive_error"] or i["preview_error"]
            ][:12],
            extensions=dict(Counter(i["extension"] for i in items)),
        )
        return batch

    def batches(self) -> list[dict]:
        with self._db() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM batches ORDER BY created_at DESC")]
        return [self.get(identifier) for identifier in ids]

    def reconcile_cached_previews(self) -> None:
        """Adopt matching local previews, including work done by the former folder scanner."""
        with self._db() as db:
            identifiers = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM batches WHERE phase='preview' AND status!='indexing'"
                )
            ]
        for identifier in identifiers:
            self._reuse_cached_previews(identifier)

    def _reuse_cached_previews(self, identifier: str) -> None:
        batch = self._batch(identifier)
        sources = {source["id"]: source for source in batch["sources"]}
        pending = [
            item
            for item in self.files(identifier)
            if item["role"] == "photo" and item["preview_state"] in {"pending", "ready"}
        ]
        if not pending:
            return
        ready = []
        unavailable = []
        with StateStore(self.locations.state_dir) as store:
            cache_root = (self.locations.state_dir / "cache").resolve()
            for item in pending:
                if item["preview_state"] == "ready":
                    unavailable.append((item["id"],))
                source = sources[item["source_id"]]
                if self._hidden(store, source, item):
                    continue
                remote_path = str(PurePosixPath(source["logical_root"]) / item["relative"])
                row = store.connection.execute(
                    "SELECT a.*,c.relative_path AS contact_path,c.size_bytes AS contact_bytes "
                    "FROM assets a JOIN cache_entries c ON c.asset_id=a.id "
                    "AND c.variant='contact' AND c.fingerprint=a.fingerprint "
                    "WHERE a.provider=? AND a.remote_path=? AND a.size_bytes=?",
                    (source["provider"], remote_path, item["size_bytes"]),
                ).fetchone()
                if row is None:
                    continue
                hashes = json.loads(row["file_hashes_json"])
                if not source["legacy"] and hashes.get("mtime_ns") != str(item["mtime_ns"]):
                    continue
                path = (cache_root / row["contact_path"]).resolve()
                if not path.is_relative_to(cache_root):
                    continue
                try:
                    if not path.is_file() or path.stat().st_size != row["contact_bytes"]:
                        continue
                except OSError:
                    continue
                ready.append((row["id"], self._capture_day(row["capture_at"]), item["id"]))
                if item["preview_state"] == "ready":
                    unavailable.pop()
        with self._db() as db:
            db.executemany(
                "UPDATE files SET preview_state='pending',asset_id=NULL,capture_day=NULL "
                "WHERE id=?",
                unavailable,
            )
            db.executemany(
                "UPDATE files SET preview_state='ready',preview_error=NULL,asset_id=?,"
                "capture_day=? WHERE id=? AND preview_state='pending'",
                ready,
            )
            remaining = db.execute(
                "SELECT count(*) FROM files WHERE batch_id=? AND role='photo' "
                "AND preview_state NOT IN ('ready','skipped')",
                (identifier,),
            ).fetchone()[0]
            if not remaining:
                db.execute(
                    "UPDATE batches SET status='ready',error=NULL,current_path='' "
                    "WHERE id=? AND phase='preview' AND status!='indexing'",
                    (identifier,),
                )
            elif unavailable:
                db.execute(
                    "UPDATE batches SET status='prepared' WHERE id=? AND phase='preview' "
                    "AND status='ready'",
                    (identifier,),
                )

    @staticmethod
    def _capture_day(value: str | None) -> str | None:
        day = (value or "")[:10].replace(":", "-")
        return day if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else None

    def prepare(self, identifier: str, paths: list[str]) -> dict:
        if not paths or len(paths) > 16 or any(not isinstance(p, str) for p in paths):
            raise ValueError("Choose between one and sixteen source folders")
        if str(UUID(identifier)) != identifier:
            raise ValueError("Invalid import ID")
        with self.locations.lock, self.lock:
            if self.busy:
                raise ValueError("Pause the active import before adding another batch")
            roots = [folder_identity(path) for path in paths]
            normalized = [r["path"] for r in roots]
            if len(set(normalized)) != len(normalized) or any(
                Path(a).is_relative_to(Path(b)) for a in normalized for b in normalized if a != b
            ):
                raise ValueError("Choose distinct source folders that do not contain one another")
            with self._db() as db:
                request = db.execute(
                    "SELECT * FROM import_requests WHERE id=?", (identifier,)
                ).fetchone()
            if request:
                if json.loads(request["paths_json"]) != normalized:
                    raise ValueError("This import request already refers to different folders")
                return self.get(request["batch_id"])
            try:
                existing = self._batch(identifier)
            except KeyError:
                existing = None
            if existing:
                if [s["folder"]["path"] for s in existing["sources"]] != normalized:
                    raise ValueError("This import request already refers to different folders")
                return self.get(identifier)
            sources = []
            all_files = []
            ignored = 0
            archived_paths = self.copies.paths()
            for root in roots:
                config = self.locations.update(
                    {
                        "action": "add_source",
                        "path": root["path"],
                        "expected_revision": self.locations.get()["revision"],
                    }
                )
                source = dict(self.locations.source(config["active_source_id"], config))
                source["label"] = filename(source["name"])
                sources.append(source)
            labels = Counter(s["label"].casefold() for s in sources)
            for source in sources:
                if labels[source["label"].casefold()] > 1:
                    source["label"] += "-" + source["id"][:8]
                root = check_folder(source["folder"])
                exclusions = self.locations.exclusions(self.locations.get())

                def walk_error(error):
                    raise error

                for directory, names, files in os.walk(root, followlinks=False, onerror=walk_error):
                    names[:] = sorted(
                        n
                        for n in names
                        if not n.startswith((".", "_"))
                        and n.casefold() != "sequences"
                        and not (Path(directory) / n).is_symlink()
                        and not any((Path(directory) / n).is_relative_to(p) for p in exclusions)
                    )
                    for name in sorted(files):
                        path = Path(directory) / name
                        if (
                            name.startswith(".")
                            or path.is_symlink()
                            or str(path) in archived_paths
                            or any(path.is_relative_to(p) for p in exclusions)
                        ):
                            ignored += 1
                            continue
                        info = path.stat()
                        if not path.is_file():
                            continue
                        ext = path.suffix.lower().lstrip(".")
                        role = (
                            "photo"
                            if ext in PHOTO_EXTENSIONS
                            else "sidecar"
                            if ext in SIDECAR_EXTENSIONS
                            else "other"
                        )
                        all_files.append(
                            (
                                identifier,
                                source["id"],
                                path.relative_to(root).as_posix(),
                                name,
                                ext,
                                role,
                                info.st_size,
                                info.st_mtime_ns,
                            )
                        )
                        if len(all_files) > 100_000:
                            raise ValueError(
                                "Choose a smaller import batch (at most 100,000 files)"
                            )
            if not any(item[5] == "photo" for item in all_files):
                raise ValueError("No supported photos were found in these folders")
            # The same unchanged folders reopen their existing preview batch.
            # New or changed files produce a new, frozen inventory.
            source_ids = {source["id"] for source in sources}
            inventory = {tuple(item[1:]) for item in all_files}
            for previous in self.batches():
                if previous["phase"] != "preview" or previous["destination"] is not None:
                    continue
                if {source["id"] for source in previous["sources"]} != source_ids:
                    continue
                prior_inventory = {
                    tuple(
                        item[key]
                        for key in (
                            "source_id",
                            "relative",
                            "name",
                            "extension",
                            "role",
                            "size_bytes",
                            "mtime_ns",
                        )
                    )
                    for item in self.files(previous["id"])
                }
                if prior_inventory == inventory:
                    with self._db() as db:
                        db.execute(
                            "INSERT INTO import_requests(id,batch_id,paths_json) VALUES (?,?,?)",
                            (identifier, previous["id"], json.dumps(normalized)),
                        )
                    self._reuse_cached_previews(previous["id"])
                    return self.get(previous["id"])
            with self._db() as db:
                db.execute(
                    "INSERT INTO batches(id,created_at,revision,status,phase,sources_json,ignored) "
                    "VALUES (?,?,?,'prepared','preview',?,?)",
                    (identifier, utc_now(), str(uuid4()), json.dumps(sources), ignored),
                )
                db.executemany(
                    "INSERT INTO files(batch_id,source_id,relative,name,extension,role,"
                    "size_bytes,mtime_ns) VALUES (?,?,?,?,?,?,?,?)",
                    all_files,
                )
            self._reuse_cached_previews(identifier)
            return self.get(identifier)

    def action(self, identifier: str, action: str, revision: str, path: str | None = None) -> dict:
        with self.lock:
            batch = self._batch(identifier)
            if batch["revision"] != revision:
                raise ValueError("Import settings changed. Refresh the batch before continuing")
            if action == "pause":
                if self.active_id == identifier:
                    self.stop.set()
                return self.get(identifier)
            if self.busy:
                if self.active_id == identifier and action in {"start", "resume"}:
                    return self.get(identifier)
                raise ValueError("Pause the current import before starting another operation")
            if action == "destination":
                if not isinstance(path, str):
                    raise ValueError("Choose an archive destination folder")
                if any(
                    i["archive_state"] in {"copied", "verified"} for i in self.files(identifier)
                ):
                    raise ValueError(
                        "This batch has already started archiving. Resume its recorded destination"
                    )
                destination = folder_identity(path)
                root = Path(destination["path"])
                for source in batch["sources"]:
                    original = Path(source["folder"]["path"])
                    if root.is_relative_to(original) or original.is_relative_to(root):
                        raise ValueError(
                            "Choose an archive folder separate from the import sources"
                        )
                if any(
                    root.is_relative_to(p) or p.is_relative_to(root)
                    for p in (self.locations.path.parent, self.locations.state_dir)
                ):
                    raise ValueError(
                        "The archive destination must be outside Latent's data folders"
                    )
                self._update(
                    identifier, destination_json=json.dumps(destination), revision=str(uuid4())
                )
                with self._db() as db:
                    db.execute(
                        "UPDATE files SET archive_relative=NULL WHERE batch_id=?", (identifier,)
                    )
                return self.get(identifier)
            if action in {"start", "retry_previews"}:
                phase = "preview"
            elif action == "archive":
                if batch["destination"] is None:
                    raise ValueError("Choose an archive destination first")
                if any(
                    i["role"] == "photo" and i["preview_state"] == "pending"
                    for i in self.files(identifier)
                ):
                    raise ValueError(
                        "Finish generating previews before reviewing the archive destination"
                    )
                check_folder(batch["destination"])
                phase = "archive"
            elif action == "resume":
                phase = batch["phase"]
            else:
                raise ValueError("Unknown import action")
            self.stop.clear()
            self.active_id = identifier
            self._update(
                identifier,
                phase=phase,
                status="indexing" if phase == "preview" else "archiving",
                error=None,
                current_path="",
            )

            def run():
                try:
                    if phase == "preview":
                        self._previews(identifier)
                        self.reconcile_cached_previews()
                    else:
                        self._archive(identifier)
                except ImportPaused:
                    self._update(identifier, status="paused", error=None, current_path="")
                except CloudConfirmationPending:
                    self._update(
                        identifier,
                        status="waiting_for_cloud",
                        error=(
                            "The copy is saved, but cloud confirmation is still pending. "
                            "Resume to verify it."
                        ),
                    )
                except Exception as error:
                    self._update(identifier, status="needs_attention", error=str(error))
                finally:
                    self.active_id = None

            self.worker = threading.Thread(target=run, name="latent-import", daemon=True)
            self.worker.start()
            return self.get(identifier)

    def _checkpoint(self) -> None:
        if self.stop.is_set():
            raise ImportPaused()

    def _update(self, identifier: str, **values) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE batches SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                (*values.values(), identifier),
            )

    def _file_update(self, identifier: int, **values) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE files SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                (*values.values(), identifier),
            )

    def _source(self, batch: dict, item: dict) -> dict:
        original = next(s for s in batch["sources"] if s["id"] == item["source_id"])
        current = self.locations.source(original["id"], self.locations.get())
        if (current["provider"], current["logical_root"]) != (
            original["provider"],
            original["logical_root"],
        ):
            raise ConfigurationError("The import source identity changed")
        return {**current, "label": original["label"]}

    def _local(self, source: dict, item: dict) -> Path:
        path = contained_path(check_folder(source["folder"]), item["relative"])
        info = path.stat()
        if not path.is_file() or (info.st_size, info.st_mtime_ns) != (
            item["size_bytes"],
            item["mtime_ns"],
        ):
            raise ConfigurationError(
                f"File changed after import review: {item['name']}. "
                "Add a new batch for the changed files"
            )
        return path

    @staticmethod
    def _hidden(store: StateStore, source: dict, item: dict) -> bool:
        return (
            store.connection.execute(
                "SELECT 1 FROM hidden_assets WHERE provider=? AND remote_path=?",
                (source["provider"], str(PurePosixPath(source["logical_root"]) / item["relative"])),
            ).fetchone()
            is not None
        )

    def _previews(self, identifier: str) -> None:
        batch = self._batch(identifier)
        with StateStore(self.locations.state_dir) as store:
            pipeline = PreviewPipeline(store, CacheManager(store))
            for item in self.files(identifier):
                self._checkpoint()
                if item["role"] != "photo" or item["preview_state"] == "ready":
                    continue
                source = self._source(batch, item)
                if self._hidden(store, source, item):
                    self._file_update(item["id"], preview_state="skipped", archive_state="skipped")
                    continue
                self._update(identifier, current_path=item["name"])
                try:
                    self._local(source, item)
                    asset = filesystem_asset(source, item["relative"])
                    if source["legacy"]:
                        existing = store.connection.execute(
                            "SELECT * FROM assets WHERE provider=? AND remote_path=?",
                            (source["provider"], asset.remote_path),
                        ).fetchone()
                        if existing and existing["size_bytes"] == asset.size_bytes:
                            asset = row_asset(existing)
                    result = pipeline.run(FilesystemRangeSource(source, asset), reuse_contact=True)
                    row = store.connection.execute(
                        "SELECT capture_at FROM assets WHERE id=?", (result.asset_id,)
                    ).fetchone()
                    day = self._capture_day(row[0])
                    self._file_update(
                        item["id"],
                        preview_state="ready",
                        preview_error=None,
                        asset_id=result.asset_id,
                        capture_day=day,
                    )
                except Exception as error:
                    self._file_update(item["id"], preview_state="failed", preview_error=str(error))
            failed = self.get(identifier)["preview_failed"]
            self._update(
                identifier,
                status="needs_attention" if failed else "ready",
                current_path="",
                error=f"{failed} photos need preview attention. Other photos are available."
                if failed
                else None,
            )

    @staticmethod
    def _companion(item: dict, items: list[dict]) -> dict | None:
        if item["role"] != "sidecar":
            return None
        relative = PurePosixPath(item["relative"])
        candidates = [
            i
            for i in items
            if i["role"] == "photo"
            and i["source_id"] == item["source_id"]
            and PurePosixPath(i["relative"]).parent == relative.parent
        ]
        if item["extension"] == "dop":
            return next(
                (i for i in candidates if i["name"].casefold() == relative.stem.casefold()), None
            )
        return next(
            (
                i
                for i in sorted(candidates, key=lambda i: i["extension"] != "arw")
                if PurePosixPath(i["relative"]).stem.casefold() == relative.stem.casefold()
            ),
            None,
        )

    def _archive(self, identifier: str) -> None:
        batch = self._batch(identifier)
        destination = batch["destination"]
        if destination is None:
            raise ValueError("Choose an archive destination first")
        root = check_folder(destination)
        items = sorted(self.files(identifier), key=lambda i: (i["role"] == "sidecar", i["id"]))
        cloud = destination.get("cloud")
        with StateStore(self.locations.state_dir) as store:
            for item in items:
                self._checkpoint()
                if item["archive_state"] == "verified":
                    continue
                source = self._source(batch, item)
                companion = self._companion(item, items)
                if self._hidden(store, source, companion or item):
                    self._file_update(item["id"], archive_state="skipped", archive_error=None)
                    continue
                self._update(identifier, status="archiving", current_path=item["name"], error=None)
                try:
                    if item["archive_state"] != "copied":
                        local = self._local(source, item)
                        hashes = file_hashes(local)
                        self._checkpoint()
                        self._local(source, item)
                        day = (companion or item)["capture_day"]
                        prefix = f"{day[:4]}/{day}" if day else "Undated"
                        relative = str(PurePosixPath(prefix) / source["label"] / item["relative"])
                        if companion and companion.get("archive_relative"):
                            original = PurePosixPath(companion["archive_relative"])
                            relative = (
                                str(original) + ".dop"
                                if item["extension"] == "dop"
                                else str(original.with_suffix(".xmp"))
                            )
                        target = (
                            str(contained_path(root, item["archive_relative"]))
                            if item["archive_relative"]
                            else copy_destination(
                                {"folder": destination, "prefix": ""}, relative, hashes["sha256"]
                            )
                        )
                        relative = Path(target).relative_to(root).as_posix()
                        # Record the destination before publishing so retries reuse it.
                        self._file_update(
                            item["id"],
                            archive_relative=relative,
                            hashes_json=json.dumps(hashes),
                            archive_error=None,
                        )
                        item.update(archive_relative=relative, hashes_json=json.dumps(hashes))
                        # CloudDrive's upload cache and local staging share the Mac's disk budget.
                        if shutil.disk_usage(root).free < item["size_bytes"] + 512 * 1024 * 1024:
                            raise ConfigurationError(
                                "Not enough free space to archive the next file"
                            )
                        if (
                            cloud
                            and shutil.disk_usage(self.locations.state_dir).free
                            < item["size_bytes"] + 1024**3
                        ):
                            raise ConfigurationError(
                                "Free space on this Mac is too low for the cloud upload cache"
                            )
                        copy_finished_photo(
                            local,
                            target,
                            {"folder": destination},
                            hashes["sha256"],
                            checkpoint=self._checkpoint,
                        )
                        self._file_update(item["id"], archive_state="copied")
                        item["archive_state"] = "copied"
                    hashes = json.loads(item["hashes_json"])
                    if cloud:
                        remote = str(PurePosixPath(cloud["remote_path"]) / item["archive_relative"])
                        self._update(identifier, status="verifying")
                        deadline = time.monotonic() + self.verification_timeout
                        while not self.cloud.verified(remote, item["size_bytes"], hashes):
                            self._checkpoint()
                            if time.monotonic() >= deadline:
                                raise CloudConfirmationPending()
                            if self.stop.wait(
                                min(self.verification_interval, max(0, deadline - time.monotonic()))
                            ):
                                raise ImportPaused()
                    elif (
                        sha256(contained_path(check_folder(destination), item["archive_relative"]))
                        != hashes["sha256"]
                    ):
                        raise ConfigurationError("The archive copy changed before verification")
                    self._checkpoint()
                    if item["asset_id"] is not None:
                        row = store.connection.execute(
                            "SELECT * FROM assets WHERE id=?", (item["asset_id"],)
                        ).fetchone()
                        asset = row_asset(row)
                    else:
                        # Sidecars retain logical source paths too, enabling later editing recovery.
                        logical = str(PurePosixPath(source["logical_root"]) / item["relative"])
                        from .models import RemoteAsset

                        asset = RemoteAsset(
                            source["provider"],
                            logical,
                            logical,
                            item["name"],
                            item["size_bytes"],
                            file_hashes=hashes,
                        )
                    self.copies.register(asset, destination, item["archive_relative"], hashes)
                    self._file_update(item["id"], archive_state="verified", archive_error=None)
                    item["archive_state"] = "verified"
                except (ImportPaused, CloudConfirmationPending):
                    raise
                except Exception as error:
                    self._file_update(item["id"], archive_error=str(error))
                    raise
            self._update(identifier, status="archived", current_path="", error=None)
