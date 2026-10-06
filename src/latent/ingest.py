"""Compatibility for older folder-scan callers, using the shared import worker."""

from __future__ import annotations

import json
from uuid import uuid4

from .imports import ImportManager
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
        batch = self.imports.get(identifier)
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
            try:
                batch = self.imports.prepare(str(uuid4()), [source["folder"]["path"]])
            except ValueError as error:
                if str(error) != "No supported photos were found in these folders":
                    raise
                self.state = {
                    "status": "complete",
                    "source_id": source["id"],
                    "discovered": 0,
                    "indexed": 0,
                    "failed": 0,
                    "error": None,
                }
                write_workspace_export(self.path, self.state)
                return self.status()
            self.state = {
                "status": batch["status"],
                "source_id": source["id"],
                "import_id": batch["id"],
            }
            write_workspace_export(self.path, self.state)
            self.imports.action(batch["id"], "start", batch["revision"])
            return self.status()

    def cancel(self) -> dict:
        if identifier := self.state.get("import_id"):
            batch = self.imports.get(identifier)
            self.imports.action(identifier, "pause", batch["revision"])
        return self.status()

    def close(self) -> None:
        if self.owns_imports:
            self.imports.close()
