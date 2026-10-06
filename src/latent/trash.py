"""Recoverable, journaled moves inside each original-photo volume."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from uuid import UUID

from .filesystem import filesystem_asset
from .folder_output import output_parent, publish_without_replacing
from .locations import TRASH_FOLDER, LocationsStore, check_folder, contained_path, row_asset
from .provider import CloudDriveRangeSource
from .storage import StateStore
from .workspace import utc_now, write_workspace_export


class PhotoTrash:
    def __init__(self, locations: LocationsStore) -> None:
        self.locations = locations
        self.root = locations.path.parent / "trash"
        self.root.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.worker: threading.Thread | None = None
        self.stop = threading.Event()
        for path in self.root.glob("*.json"):
            batch = json.loads(path.read_text())
            if batch["status"] in {"queued", "moving", "restoring"}:
                batch.update(
                    status="interrupted", error="The move was interrupted. Restore or retry it."
                )
                self._save(batch)
            self._visibility(batch)

    @property
    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def close(self) -> None:
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=35)

    def _path(self, identifier: str) -> Path:
        if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
            raise ValueError("Invalid trash request ID")
        return self.root / f"{identifier}.json"

    def _save(self, batch: dict) -> None:
        batch["updated_at"] = utc_now()
        write_workspace_export(self._path(batch["id"]), batch)

    def get(self, identifier: str) -> dict:
        with self.lock:
            path = self._path(identifier)
            if not path.is_file():
                raise KeyError("Trash entry not found")
            return json.loads(path.read_text())

    def batches(self) -> list[dict]:
        with self.lock:
            return sorted(
                (self.get(p.stem) for p in self.root.glob("*.json")),
                key=lambda b: b["created_at"],
                reverse=True,
            )

    def _visibility(self, batch: dict) -> None:
        with StateStore(self.locations.state_dir) as store:
            for item in batch["items"]:
                if item["state"] == "restored":
                    store.connection.execute(
                        "DELETE FROM hidden_assets "
                        "WHERE provider=? AND remote_path=? AND trash_id=?",
                        (item["provider"], item["remote_path"], batch["id"]),
                    )
                else:
                    store.connection.execute(
                        "INSERT OR REPLACE INTO hidden_assets VALUES (?, ?, ?)",
                        (item["provider"], item["remote_path"], batch["id"]),
                    )
            store.connection.commit()

    def create(self, identifier: str, records: list[dict]) -> dict:
        with self.lock, self.locations.lock:
            if self._path(identifier).exists():
                existing = self.get(identifier)
                wanted = {(r["provider"], r["remote_path"], r["fingerprint"]) for r in records}
                saved = {
                    (r["provider"], r["remote_path"], r["fingerprint"]) for r in existing["items"]
                }
                if wanted != saved:
                    raise ValueError("This trash request already refers to different photos")
                return existing
            if self.busy:
                raise ValueError("Wait for the current trash operation to finish")
            items = []
            for record in records:
                reference = self.locations.reference(record)
                source = reference["source"]
                if not source["folder"]:
                    raise ValueError(
                        "Connect the original mounted folder in Locations before deleting photos"
                    )
                root = check_folder(source["folder"])
                relative = reference["archive_relative"]
                original = contained_path(root, relative)
                if original.stat().st_size != record["size_bytes"] or not original.is_file():
                    raise ValueError(
                        "An original changed since indexing. Scan the folder before deleting it"
                    )
                items.append(
                    {
                        **record,
                        "source": source,
                        "relative": relative,
                        "trash_relative": f"{TRASH_FOLDER}/{identifier}/{relative}",
                        "state": "pending",
                    }
                )
            batch = {
                "id": identifier,
                "created_at": utc_now(),
                "status": "queued",
                "items": items,
                "error": None,
                "operation": "trash",
            }
            self._save(batch)
            self._visibility(batch)
            self._start(identifier, restore=False)
            return self.get(identifier)

    def action(self, identifier: str, *, restore: bool) -> dict:
        with self.lock:
            batch = self.get(identifier)
            if self.busy:
                raise ValueError("Wait for the current trash operation to finish")
            if batch["status"] == "restored" or (not restore and batch["status"] == "trashed"):
                return batch
            self._start(identifier, restore=restore)
            return self.get(identifier)

    def _start(self, identifier: str, *, restore: bool) -> None:
        batch = self.get(identifier)
        batch.update(
            status="restoring" if restore else "moving",
            error=None,
            operation="restore" if restore else "trash",
        )
        self._save(batch)

        def run():
            try:
                for item in batch["items"]:
                    if self.stop.is_set():
                        raise ValueError("The move was interrupted. Restore or retry it.")
                    self._move(batch, item, restore=restore)
                    with self.lock:
                        self._save(batch)
                        self._visibility(batch)
                batch.update(status="restored" if restore else "trashed", error=None)
            except Exception as error:
                batch.update(status="needs_attention", error=str(error))
            finally:
                with self.lock:
                    self._save(batch)
                    self._visibility(batch)

        self.worker = threading.Thread(target=run, name="latent-photo-trash", daemon=True)
        self.worker.start()

    def _check_original(self, item: dict, source: dict) -> None:
        if source["legacy"]:
            # Use cloud metadata only. Deleting a RAW must not download it first.
            current = CloudDriveRangeSource(item["remote_path"]).asset
            with StateStore(self.locations.state_dir) as store:
                row = store.connection.execute(
                    "SELECT * FROM assets WHERE provider=? AND remote_path=?",
                    (item["provider"], item["remote_path"]),
                ).fetchone()
            if row is None or row["fingerprint"] != item["fingerprint"]:
                raise ValueError("The indexed photo changed. Refresh before deleting it")
            expected = row_asset(row)
            keys = set(expected.file_hashes) & {"1", "2"}
            if (
                current.size_bytes != expected.size_bytes
                or not keys
                or any(current.file_hashes.get(k) != expected.file_hashes[k] for k in keys)
            ):
                raise ValueError("CloudDrive could not confirm the original photo identity")
        elif filesystem_asset(source, item["relative"]).fingerprint != item["fingerprint"]:
            raise ValueError("The original changed since indexing. Scan before deleting it")

    def _move(self, batch: dict, item: dict, *, restore: bool) -> None:
        with self.locations.lock:
            source = self.locations.source(item["source"]["id"], self.locations.get())
            if (
                source["logical_root"] != item["source"]["logical_root"]
                or source["provider"] != item["provider"]
            ):
                raise ValueError("The photo source changed. Reconnect its original folder")
            root = check_folder(source["folder"])
            original = contained_path(root, item["relative"])
            trash = contained_path(root, item["trash_relative"])
            if restore and item["state"] == "restored":
                return
            if original.exists() and trash.exists():
                raise ValueError(
                    f"An existing file blocks recovery. Both copies are kept: {item['name']}"
                )
            if restore and not trash.exists():
                if original.is_file():
                    if item["state"] != "pending":
                        self._check_original(item, source)
                    item["state"] = "restored"
                    return
                raise ValueError(f"The trashed photo is unavailable: {item['name']}")
            if not restore and trash.is_file() and not original.exists():
                if trash.stat().st_size != item["size_bytes"]:
                    raise ValueError(f"The trashed file size changed: {item['name']}")
                item["state"] = "trashed"
                return
            if not restore:
                self._check_original(item, source)
            from_relative = item["trash_relative"] if restore else item["relative"]
            to_relative = item["relative"] if restore else item["trash_relative"]
            # Descriptor-relative, no-follow traversal and exclusive rename protect both paths.
            with (
                output_parent(source["folder"], from_relative) as (from_fd, from_name),
                output_parent(source["folder"], to_relative) as (to_fd, to_name),
            ):
                before = os.stat(from_name, dir_fd=from_fd, follow_symlinks=False)
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("Only original photo files can be moved to Trash")
                check_folder(source["folder"])
                publish_without_replacing(from_fd, from_name, to_name, destination_parent=to_fd)
                after = os.stat(to_name, dir_fd=to_fd, follow_symlinks=False)
                if after.st_size != before.st_size:
                    raise ValueError("The mounted folder has not confirmed the complete move")
            item["state"] = "restored" if restore else "trashed"
