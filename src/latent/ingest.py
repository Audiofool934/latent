"""Compatibility for older folder-scan callers, using the shared import worker."""

from __future__ import annotations

import json
from uuid import uuid4

from .imports import NO_PHOTOS, ImportManager
from .locations import LocationsStore
from .workspace import write_workspace_export


class FolderIndexer:
    """A legacy API adapter. It never creates an independent preview worker."""

    def __init__(self, locations: LocationsStore, imports: ImportManager | None = None) -> None:
        self.locations = locations
        self.imports = imports or ImportManager(locations)
        self.owns_imports = imports is None
        self.path = locations.path.parent / "folder-indexing.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {"status": "idle"}
        if self.state["status"] == "scanning":
            self.state["status"] = "interrupted"
            write_workspace_export(self.path, self.state)

    @property
    def worker(self):
        return self.imports.worker

    @property
    def busy(self) -> bool:
        return self.imports.busy

    def status(self) -> dict:
        identifier = self.state.get("import_id")
        if identifier is None:
            return dict(self.state)
        identifier = self.imports.resolve(identifier)
        try:
            batch = self.imports.get(identifier)
        except KeyError:
            # A canceled folder scan leaves no batch behind.
            return {"status": "idle"}
        if batch["status"] == "scan_failed" and batch["error"] == NO_PHOTOS:
            status = "complete"
        else:
            status = {
                "indexing": "scanning",
                "ready": "complete",
                "paused": "interrupted",
            }.get(batch["status"], batch["status"])
        return {
            "status": status,
            "import_id": identifier,
            "source_id": self.state.get("source_id"),
            "discovered": batch["photo_count"],
            "indexed": batch["preview_ready"],
            "failed": batch["preview_failed"],
            "error": batch["error"],
            "current_path": batch["current_path"],
        }

    def start(self, expected_revision: str) -> dict:
        with self.locations.lock, self.imports.lock:
            data = self.locations.get()
            if data["revision"] != expected_revision:
                raise ValueError("Locations changed. Refresh before importing")
            source = self.locations.source(data["active_source_id"], data)
            if not source["folder"]:
                raise ValueError("Connect this archive's mounted folder before importing")
            # The import worker scans the folder, then builds its previews.
            batch = self.imports.prepare(
                str(uuid4()), [source["folder"]["path"]], start_previews=True
            )
            self.state = {
                "status": batch["status"],
                "source_id": source["id"],
                "import_id": batch["id"],
            }
            write_workspace_export(self.path, self.state)
            return self.status()

    def cancel(self) -> dict:
        if identifier := self.state.get("import_id"):
            try:
                batch = self.imports.get(self.imports.resolve(identifier))
            except KeyError:
                return self.status()
            if batch["status"] in {"scanning", "canceling"}:
                self.imports.action(batch["id"], "cancel", batch["revision"])
            elif batch["status"] != "scan_failed":
                self.imports.action(batch["id"], "pause", batch["revision"])
        return self.status()

    def close(self) -> None:
        if self.owns_imports:
            self.imports.close()
