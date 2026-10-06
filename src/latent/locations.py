"""Durable library roots, independent of local disks or mounted cloud providers."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .errors import ConfigurationError
from .models import RemoteAsset
from .storage import StateStore
from .workspace import write_workspace_export

TRASH_FOLDER = ".Latent Trash"


def _cloud_folder_identity(path: Path) -> dict | None:
    """Use the provider's folder ID for CloudFS, whose device and inode change on remount."""
    if sys.platform != "darwin":
        return None
    listing = subprocess.run(
        ["/sbin/mount"], check=True, capture_output=True, text=True, timeout=5
    ).stdout
    roots = [
        Path(line.removeprefix("CloudFS on ").rsplit(" (", 1)[0])
        for line in listing.splitlines()
        if line.startswith("CloudFS on ") and " (macfuse," in line
    ]
    matches = [root for root in roots if path.is_relative_to(root)]
    if not matches:
        return None
    from .provider import cloud_folder_identity

    return cloud_folder_identity(path, max(matches, key=lambda root: len(root.parts)))


def folder_identity(value: str) -> dict:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("Choose an absolute folder path")
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise ValueError("Choose an existing folder")
    info = path.stat()
    identity = {"path": str(path), "device": info.st_dev, "inode": info.st_ino}
    if cloud := _cloud_folder_identity(path):
        identity["cloud"] = cloud
    return identity


def check_folder(identity: dict) -> Path:
    path = Path(identity["path"])
    try:
        current = folder_identity(str(path))
    except OSError as error:
        raise ConfigurationError(
            f"Folder is unavailable. Reconnect it in Locations: {path}"
        ) from error
    if identity.get("cloud"):
        matches = current["path"] == identity["path"] and current.get("cloud") == identity["cloud"]
    else:
        matches = all(current.get(key) == identity.get(key) for key in ("path", "device", "inode"))
    if not matches:
        raise ConfigurationError(f"Folder or volume changed. Choose it again in Locations: {path}")
    return path


def safe_relative(value: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(p in {".", ".."} for p in relative.parts)
    ):
        raise ValueError("Invalid relative photo path")
    return relative


def contained_path(root: Path, relative: str) -> Path:
    path = root
    for part in safe_relative(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Symbolic links are excluded from original and output paths: {path}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Photo path leaves the selected folder")
    return path


def row_asset(row) -> RemoteAsset:
    return RemoteAsset(
        provider=row["provider"],
        remote_id=row["remote_id"],
        remote_path=row["remote_path"],
        name=row["name"],
        size_bytes=row["size_bytes"],
        write_time=row["write_time"],
        file_hashes=json.loads(row["file_hashes_json"]),
    )


class LocationsStore:
    def __init__(self, workspace_dir: Path, state_dir: Path) -> None:
        self.path = workspace_dir / "locations.json"
        self.state_dir = state_dir
        self.working_roots: set[Path] = {workspace_dir / "editing"}
        self.lock = threading.RLock()
        if not self.path.exists():
            sources = []
            with StateStore(state_dir) as store:
                rows = store.connection.execute(
                    "SELECT provider, remote_path FROM assets"
                ).fetchall()
            for provider in sorted({r["provider"] for r in rows}):
                paths = [
                    str(PurePosixPath(r["remote_path"]).parent)
                    for r in rows
                    if r["provider"] == provider
                ]
                sources.append(
                    {
                        "id": str(uuid4()),
                        "name": provider,
                        "provider": provider,
                        "logical_root": os.path.commonpath(paths),
                        "folder": None,
                        "legacy": True,
                    }
                )
            self._save(
                {
                    "library_id": str(uuid4()),
                    "revision": str(uuid4()),
                    "sources": sources,
                    "active_source_id": sources[0]["id"] if sources else None,
                    "output": None,
                    "sequence_folder": None,
                }
            )
        else:
            data = self.get()
            if "library_id" not in data:
                data["library_id"] = str(uuid4())
                self._save(data)

    def _save(self, data: dict) -> None:
        write_workspace_export(self.path, data)

    def get(self) -> dict:
        with self.lock:
            return json.loads(self.path.read_text())

    def update(self, payload: dict) -> dict:
        with self.lock:
            data = self.get()
            if payload.get("expected_revision") != data["revision"]:
                raise ValueError("Locations changed. Refresh them before saving again")
            action = payload.get("action")
            if action == "select_source":
                self.source(payload["source_id"], data)
                data["active_source_id"] = payload["source_id"]
            elif action in {"add_source", "reconnect_source"}:
                folder = folder_identity(payload["path"])
                if action == "reconnect_source":
                    source = self.source(payload["source_id"], data)
                    # A different folder must be added as a new source. Relocation is
                    # deliberately explicit, and checked against indexed file sizes.
                    self._verify_binding(source, folder)
                    source["folder"] = folder
                else:
                    source = next(
                        (
                            s
                            for s in data["sources"]
                            if s["folder"] and s["folder"]["path"] == folder["path"]
                        ),
                        None,
                    )
                    if source is None:
                        identifier = str(uuid4())
                        source = {
                            "id": identifier,
                            "name": Path(folder["path"]).name,
                            "provider": f"filesystem:{identifier}",
                            "logical_root": f"/local-{identifier}",
                            "folder": folder,
                            "legacy": False,
                        }
                        data["sources"].append(source)
                    else:
                        self._verify_binding(source, folder)
                        source["folder"] = folder
                data["active_source_id"] = source["id"]
            elif action in {"set_output", "set_sequence_folder"}:
                data["output" if action == "set_output" else "sequence_folder"] = folder_identity(
                    payload["path"]
                )
                if action == "set_output":
                    history = data.setdefault("output_history", [])
                    if data["output"]["path"] not in history:
                        history.append(data["output"]["path"])
            else:
                raise ValueError("Unknown location action")
            data["revision"] = str(uuid4())
            self._save(data)
            return copy.deepcopy(data)

    def _verify_binding(self, source: dict, folder: dict) -> None:
        root = check_folder(folder)
        with StateStore(self.state_dir) as store:
            rows = store.connection.execute(
                "SELECT a.remote_path, a.size_bytes, h.trash_id FROM assets a "
                "LEFT JOIN hidden_assets h "
                "ON h.provider=a.provider AND h.remote_path=a.remote_path "
                "WHERE a.provider=? ORDER BY a.id LIMIT 20",
                (source["provider"],),
            ).fetchall()
        for row in rows:
            relative = PurePosixPath(row["remote_path"]).relative_to(source["logical_root"])
            path = contained_path(root, str(relative))
            if not path.is_file() and row["trash_id"]:
                path = contained_path(root, f"{TRASH_FOLDER}/{row['trash_id']}/{relative}")
            if not path.is_file() or path.stat().st_size != row["size_bytes"]:
                raise ValueError(
                    "This folder does not match the existing archive hierarchy. "
                    "Choose its original root, or add a new source"
                )

    @staticmethod
    def source(identifier: str, data: dict) -> dict:
        source = next((s for s in data["sources"] if s["id"] == identifier), None)
        if source is None:
            raise ValueError("Library source is missing")
        return source

    def source_for(self, remote_path: str, provider: str | None = None) -> dict:
        matches = [
            s
            for s in self.get()["sources"]
            if (provider is None or s["provider"] == provider)
            and PurePosixPath(remote_path).is_relative_to(s["logical_root"])
        ]
        if len(matches) != 1:
            raise ValueError("Connect this photo's original folder in Locations")
        return matches[0]

    def reference(self, record: dict) -> dict:
        source = self.source_for(record["remote_path"], record.get("provider"))
        relative = str(PurePosixPath(record["remote_path"]).relative_to(source["logical_root"]))
        safe_relative(relative)
        return {"source": source, "archive_relative": relative}

    def output_snapshot(self) -> dict:
        data = self.get()
        if not data["output"]:
            raise ValueError("Choose an output folder in Locations before saving finished photos")
        root = check_folder(data["output"])
        # When output is an ancestor of any input, retain a dedicated subtree.
        protected = any(
            s["folder"] and Path(s["folder"]["path"]).is_relative_to(root) for s in data["sources"]
        )
        return {
            "folder": data["output"],
            "prefix": "_Latent Edits" if protected else "",
            "revision": data["revision"],
        }

    def exclusions(self, data: dict) -> set[Path]:
        roots = {self.state_dir.resolve(), self.path.parent.resolve()}
        roots.update(p.resolve() for p in self.working_roots)
        for manifest in (self.path.parent / "editing").glob("*/manifest.json"):
            batch = json.loads(manifest.read_text())
            if batch.get("folder"):
                roots.add(Path(batch["folder"]).resolve())
        output_paths = set(data.get("output_history", []))
        if data["output"]:
            output_paths.add(data["output"]["path"])
        for output_path in output_paths:
            out = Path(output_path)
            roots.add(out / "_Latent Edits")
            if any(
                s["folder"]
                and out != Path(s["folder"]["path"])
                and out.is_relative_to(s["folder"]["path"])
                for s in data["sources"]
            ):
                roots.add(out)
        if data["sequence_folder"]:
            roots.add(Path(data["sequence_folder"]["path"]) / "sequences")
        return roots
