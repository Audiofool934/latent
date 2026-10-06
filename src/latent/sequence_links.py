"""User-triggered filesystem projection of sequence folders and original-photo links."""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import suppress
from pathlib import Path

from .filters import matching_ids
from .locations import LocationsStore, check_folder, contained_path
from .storage import StateStore
from .workspace import WorkspaceStore, write_workspace_export


def filename(name: str) -> str:
    clean = re.sub(r"[/\\:\x00-\x1f]", "_", name).strip().strip(".")
    return clean.encode("utf-8")[:160].decode("utf-8", errors="ignore") or "Untitled"


class SequenceLinks:
    def __init__(self, locations: LocationsStore) -> None:
        self.locations = locations
        self.lock = threading.Lock()

    def sync(self, expected_revision: str) -> dict:
        with self.lock:
            data = self.locations.get()
            if data["revision"] != expected_revision:
                raise ValueError("Locations changed. Refresh before updating sequence links")
            if not data["sequence_folder"]:
                raise ValueError("Choose a parent folder for sequences in Locations")
            parent = check_folder(data["sequence_folder"])
            root = contained_path(parent, "sequences")
            marker = root / ".latent-sequences.json"
            if root.exists() and not marker.is_file():
                raise ValueError(
                    "This sequences folder already contains unmanaged files. "
                    "Choose another parent folder"
                )
            if marker.is_symlink():
                raise ValueError("The sequence manifest must not be a symbolic link")
            previous = (
                json.loads(marker.read_text())
                if marker.exists()
                else {"library_id": data["library_id"], "links": {}, "directories": []}
            )
            if previous.get("library_id") != data["library_id"]:
                raise ValueError(
                    "This sequences folder belongs to another library. Choose another parent folder"
                )
            desired: dict[str, str] = {}
            directories = set()
            with WorkspaceStore(self.locations.path.parent) as workspace:
                folders = workspace.list_folders()
                sequences = workspace.list_sequences()
                nodes = [{**f, "parent": f["parent_id"]} for f in folders] + [
                    {**s, "parent": s["folder_id"]} for s in sequences
                ]
                labels = {}
                for node in nodes:
                    base = filename(node["name"])
                    duplicates = [
                        n
                        for n in nodes
                        if n["parent"] == node["parent"]
                        and filename(n["name"]).casefold() == base.casefold()
                    ]
                    labels[node["id"]] = base + (
                        f" [{node['id'][:8]}]" if len(duplicates) > 1 else ""
                    )
                by_id = {f["id"]: f for f in folders}

                def folder_path(identifier: str | None) -> Path:
                    parts = []
                    seen = set()
                    while identifier:
                        if identifier in seen or identifier not in by_id:
                            raise ValueError("Sequence folder hierarchy is invalid")
                        seen.add(identifier)
                        parts.append(labels[identifier])
                        identifier = by_id[identifier]["parent_id"]
                    return Path(*reversed(parts))

                for folder in folders:
                    directories.add(folder_path(folder["id"]).as_posix())
                for summary in sequences:
                    directory = folder_path(summary["folder_id"]) / labels[summary["id"]]
                    directories.add(directory.as_posix())
                    sequence = workspace.get_sequence(summary["id"])
                    with StateStore(self.locations.state_dir) as store:
                        if sequence.get("smart_filters") is not None:
                            ids = matching_ids(
                                store, self.locations.path.parent, sequence["smart_filters"]
                            )
                            sequence["items"] = [
                                record
                                for start in range(0, len(ids), 500)
                                for record in store.library_assets_by_ids(ids[start : start + 500])
                            ]
                        hidden = {
                            (r["provider"], r["remote_path"])
                            for r in store.connection.execute(
                                "SELECT provider, remote_path FROM hidden_assets"
                            )
                        }
                    for position, item in enumerate(sequence["items"], 1):
                        if (item["provider"], item["remote_path"]) in hidden:
                            continue
                        reference = self.locations.reference(item)
                        source = reference["source"]
                        if not source["folder"]:
                            raise ValueError(
                                f"Connect the original folder for {source['name']} "
                                "before creating links"
                            )
                        target = contained_path(
                            check_folder(source["folder"]), reference["archive_relative"]
                        )
                        if not target.is_file():
                            raise ValueError(f"Original photo is unavailable: {target}")
                        name = f"{position:04d}-{filename(item['name'])}"
                        desired[(directory / name).as_posix()] = str(target)
            # Preflight all conflicts before removing or writing anything.
            for relative in desired:
                link = root / relative
                contained_path(root, str(Path(relative).parent))
                if os.path.lexists(link):
                    old_target = previous["links"].get(relative)
                    if not link.is_symlink() or os.readlink(link) != old_target:
                        raise ValueError(f"An unmanaged file blocks this sequence link: {link}")
            for relative in directories:
                path = contained_path(root, relative)
                if path.exists() and not path.is_dir():
                    raise ValueError(f"A file blocks this sequence folder: {path}")
            root.mkdir(exist_ok=True)
            write_workspace_export(marker, previous)
            for relative, target in list(previous["links"].items()):
                if desired.get(relative) == target:
                    continue
                link = root / relative
                contained_path(root, str(Path(relative).parent))
                if link.is_symlink() and os.readlink(link) == target:
                    link.unlink()
                previous["links"].pop(relative)
                write_workspace_export(marker, previous)
            for relative in sorted(directories, key=lambda p: len(Path(p).parts)):
                check_folder(data["sequence_folder"])
                contained_path(root, relative).mkdir(parents=True, exist_ok=True)
                if relative not in previous["directories"]:
                    previous["directories"].append(relative)
                    write_workspace_export(marker, previous)
            for relative, target in desired.items():
                check_folder(data["sequence_folder"])
                link = root / relative
                contained_path(root, str(Path(relative).parent))
                previous["links"][relative] = target
                write_workspace_export(marker, previous)
                if not os.path.lexists(link):
                    link.symlink_to(target)
            for relative in sorted(
                set(previous["directories"]) - directories,
                key=lambda p: len(Path(p).parts),
                reverse=True,
            ):
                # Preserve unrelated user files, including files in renamed folders.
                with suppress(OSError):
                    contained_path(root, relative).rmdir()
            write_workspace_export(
                marker,
                {
                    "library_id": data["library_id"],
                    "links": desired,
                    "directories": sorted(directories),
                },
            )
            return {"folder": str(root), "links": len(desired), "sequences": len(sequences)}
