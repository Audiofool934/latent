"""Explicit, durable RAW editing batches and verified CloudDrive uploads."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from posixpath import commonpath
from typing import Any
from uuid import UUID, uuid4

from .archive_copies import ArchiveCopies, ArchivedEditingSource
from .errors import ConfigurationError
from .filesystem import FilesystemEditingSource
from .folder_output import copy_finished_photo
from .folder_output import destination as output_destination
from .locations import LocationsStore, check_folder, contained_path
from .provider import CloudDriveCatalog, CloudDriveRangeSource
from .workspace import utc_now, write_workspace_export

CHUNK_SIZE = 4 * 1024 * 1024
DEFAULT_EDITING_DIR = Path.home() / "Pictures" / "Latent"
EXPORT_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff", ".png"}
SIDECAR_EXTENSIONS = {".dop", ".xmp"}


def file_hashes(path: Path) -> dict[str, str]:
    hashes = {"sha256": hashlib.sha256(), "2": hashlib.sha1(), "1": hashlib.md5()}
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            for digest in hashes.values():
                digest.update(chunk)
    return {name: digest.hexdigest() for name, digest in hashes.items()}


class EditingCloud:
    """Cloud access is constructed only after an explicit editing action."""

    def source(self, path: str):
        return CloudDriveRangeSource(path)

    def sidecars(self, path: str) -> list[str]:
        original = PurePosixPath(path)
        names = {original.name + ".dop", original.stem + ".xmp"}
        return [
            asset.remote_path
            for asset in CloudDriveCatalog().list_directory(
                str(original.parent), extensions={"dop", "xmp"}
            )
            if asset.name.lower() in {name.lower() for name in names}
        ]

    def _entries(self, parent: str):
        client = CloudDriveCatalog()._client()
        try:
            return list(client.get_sub_files(parent, force_refresh=True))
        finally:
            client.close()

    def ensure_folder(self, parent: str, name: str) -> str:
        destination = str(PurePosixPath(parent) / name)
        existing = next((entry for entry in self._entries(parent) if entry.name == name), None)
        if existing:
            if not existing.isDirectory:
                raise ConfigurationError(f"A file blocks the upload folder: {destination}")
            return destination
        client = CloudDriveCatalog()._client()
        try:
            client.create_folder(parent, name)
        finally:
            client.close()
        if not any(e.name == name and e.isDirectory for e in self._entries(parent)):
            raise ConfigurationError(f"CloudDrive has not confirmed the folder: {destination}")
        return destination

    def upload(self, local: Path, destination: str, expected: dict[str, str]) -> None:
        remote = PurePosixPath(destination)
        existing = next(
            (e for e in self._entries(str(remote.parent)) if e.name == remote.name), None
        )
        if existing:
            # A retry may find the previous upload. Never truncate an existing cloud file.
            if self.verified(destination, local.stat().st_size, expected):
                return
            raise ConfigurationError(f"Upload exists but is not verified yet: {destination}")
        client = CloudDriveCatalog()._client()
        handle = 0
        try:
            handle = client.create_file(str(remote.parent), remote.name)
            if handle <= 0:
                raise ConfigurationError("CloudDrive could not create the upload")
            offset = 0
            with local.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    if client.write_to_file(handle, offset, chunk) != len(chunk):
                        raise ConfigurationError("CloudDrive did not accept the complete upload")
                    offset += len(chunk)
            if not client.close_file(handle):
                raise ConfigurationError("CloudDrive could not close the upload")
            handle = 0
        finally:
            if handle:
                client.close_file(handle)
            client.close()

    def verified(self, destination: str, size: int, expected: dict[str, str]) -> bool:
        remote = PurePosixPath(destination)
        entry = next((e for e in self._entries(str(remote.parent)) if e.name == remote.name), None)
        if entry is None or not entry.isCloudFile or entry.isLocal or int(entry.size) != size:
            return False
        client = CloudDriveCatalog()._client()
        try:
            tasks = client.get_upload_file_list().uploadFiles
            if any(
                task.destPath in {destination, str(remote.parent)} and task.statusEnum not in {5, 6}
                for task in tasks
            ):
                return False
        finally:
            client.close()
        hashes = {str(k): str(v).lower() for k, v in entry.fileHashes.items()}
        supported = set(hashes) & {"1", "2"}
        return bool(supported) and all(hashes[key] == expected[key] for key in supported)


class EditingManager:
    """Owns manifests and one bounded transfer worker; failed jobs keep local files."""

    def __init__(
        self,
        workspace_dir: Path,
        *,
        files_dir: Path | None = None,
        cloud: Any = None,
        locations: LocationsStore | None = None,
        verification_timeout: float = 90,
        verification_interval: float = 3,
    ) -> None:
        self.root = workspace_dir / "editing"
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir = files_dir.expanduser().resolve() if files_dir is not None else None
        self.cloud = cloud or EditingCloud()
        self.locations = locations
        self.verification_timeout = verification_timeout
        self.verification_interval = verification_interval
        if locations:
            locations.working_roots.add(self.files_dir or self.root)
        self.lock = threading.RLock()
        self.worker: threading.Thread | None = None
        self.stop = threading.Event()
        for path in self.root.glob("*/manifest.json"):
            batch = json.loads(path.read_text())
            if batch["status"] in {
                "queued",
                "downloading",
                "uploading",
                "verifying",
                "uploaded",
                "cleaning",
                "copying",
            }:
                batch.update(status="interrupted", error="Transfer interrupted. Resume this batch.")
                self._save(batch)

    def close(self) -> None:
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=35)

    def _folder(self, identifier: str) -> Path:
        if str(UUID(identifier)) != identifier:
            raise ValueError("invalid editing batch id")
        return self.root / identifier

    def _save(self, batch: dict[str, Any]) -> None:
        batch["updated_at"] = utc_now()
        write_workspace_export(self._folder(batch["id"]) / "manifest.json", batch)

    def get(self, identifier: str) -> dict[str, Any]:
        with self.lock:
            path = self._folder(identifier) / "manifest.json"
            if not path.is_file():
                raise KeyError("editing batch not found")
            batch = json.loads(path.read_text())
            # Keep each batch's working folder durable across service restarts and
            # changes to the destination for future downloads. Older manifests
            # without a folder retain their original workspace location.
            batch.setdefault("folder", str(path.parent / "files"))
            return batch

    def batches(self) -> list[dict[str, Any]]:
        with self.lock:
            batches = [self.get(p.parent.name) for p in self.root.glob("*/manifest.json")]
        return sorted(batches, key=lambda batch: batch["created_at"], reverse=True)

    def create(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            self._check_idle()
            identities = {(record.get("provider"), record["remote_path"]) for record in records}
            for existing in self.batches():
                if existing["status"] not in {"complete", "saved"} and any(
                    (item.get("provider"), item["remote_path"]) in identities
                    or (
                        item.get("provider") is None
                        and any(p == item["remote_path"] for _, p in identities)
                    )
                    for item in existing["items"]
                ):
                    raise ValueError(
                        "A selected photo already has an editing batch. Open Editing to resume it."
                    )
            identifier = str(uuid4())
            created_at = utc_now()
            folder = (
                self.files_dir / f"{created_at[:10]}-{identifier[:8]}"
                if self.files_dir is not None
                else self._folder(identifier) / "files"
            )
            items = []
            used_names: set[str] = set()
            used_stems: set[str] = set()
            # A shared folder lets PhotoLab browse the whole selection in one filmstrip.
            for index, record in enumerate(records):
                name = str(record["name"])
                if Path(name).name != name or name in {".", ".."}:
                    raise ValueError("invalid original filename")
                relative = name
                while (
                    relative.casefold() in used_names
                    or Path(relative).stem.casefold() in used_stems
                ):
                    relative = f"{index + 1:03d}_{relative}"
                used_names.add(relative.casefold())
                used_stems.add(Path(relative).stem.casefold())
                items.append(
                    {
                        "asset_id": record["id"],
                        "provider": record.get("provider"),
                        "remote_path": record["remote_path"],
                        "name": name,
                        "fingerprint": record["fingerprint"],
                        "size_bytes": record["size_bytes"],
                        "local_path": relative,
                        "downloaded": False,
                        "annotation": record.get("annotation"),
                        **(self.locations.reference(record) if self.locations else {}),
                    }
                )
            folder.mkdir(parents=True)
            batch = {
                "id": identifier,
                "created_at": created_at,
                "folder": str(folder),
                "status": "queued",
                "error": None,
                "items": items,
                "uploads": [],
                "bytes_done": 0,
                "bytes_total": sum(item["size_bytes"] for item in items),
                "cleanup_policy": None,
                "phase": "download",
                **({"output_mode": "filesystem"} if self.locations else {}),
            }
            self._save(batch)
            self._start(identifier, self._download)
            return self.get(identifier)

    def _check_idle(self) -> None:
        if self.worker and self.worker.is_alive():
            raise ValueError("Another editing batch is transferring. Wait for it to finish.")

    def _start(self, identifier: str, operation: Any) -> None:
        self._check_idle()

        def run():
            batch = self.get(identifier)
            try:
                operation(batch)
            except Exception as error:
                batch.update(status="needs_attention", error=str(error))
                with self.lock:
                    self._save(batch)

        self.worker = threading.Thread(target=run, name="latent-editing-transfer", daemon=True)
        self.worker.start()

    def apply_action(
        self,
        identifier: str,
        action: str,
        *,
        policy: str = "",
        expected_updated_at: str | None = None,
        exports: list[dict] | None = None,
    ) -> dict[str, Any]:
        # Check and mutate under the same existing lock. A timed-out request and its
        # retry cannot both prepare a new review from the same manifest revision.
        with self.lock:
            batch = self.get(identifier)
            if expected_updated_at is not None and (
                not isinstance(expected_updated_at, str)
                or batch["updated_at"] != expected_updated_at
            ):
                raise ValueError(
                    "This editing batch changed. Check its current status before retrying"
                )
            if action == "resume":
                return self.resume(identifier)
            if action == "prepare":
                return self.prepare(identifier)
            if action == "verify":
                return self.verify(identifier)
            if action == "map":
                return self.map_exports(identifier, exports)
            if action == "finish":
                return self.finish(identifier, policy, exports=exports)
            raise ValueError("Unknown editing action")

    def resume(self, identifier: str) -> dict[str, Any]:
        with self.lock:
            batch = self.get(identifier)
            if batch["status"] not in {"needs_attention", "interrupted"}:
                raise ValueError("This batch does not need a retry")
            operation = {
                "upload": self._upload,
                "cleanup": self._cleanup,
                "copy": self._copy_exports,
            }.get(batch["phase"], self._download)
            if batch["phase"] == "download" and self.locations:
                data = self.locations.get()
                for item in batch["items"]:
                    if item.get("source"):
                        item["source"] = self.locations.source(item["source"]["id"], data)
                self._save(batch)
            self._start(identifier, operation)
            return self.get(identifier)

    def _checkpoint(self, batch: dict[str, Any]) -> None:
        if self.stop.is_set():
            raise ConfigurationError("Transfer interrupted. Resume this batch.")
        with self.lock:
            self._save(batch)

    def verify(self, identifier: str) -> dict[str, Any]:
        """Recheck an existing cloud transfer without uploading or removing local files."""
        with self.lock:
            batch = self.get(identifier)
            if batch.get("output_mode") == "filesystem" or not batch.get("uploads"):
                raise ValueError("This batch has no cloud upload to verify")
            if batch["phase"] not in {"upload", "cleanup", "verified"}:
                raise ValueError("Upload the reviewed files before verifying them")
            self._check_idle()
            self._start(identifier, self._verify_only)
            return self.get(identifier)

    def _verify_only(self, batch: dict[str, Any]) -> None:
        if self._verify_uploads(batch):
            batch.update(status="cloud_verified", error=None, phase="verified")
            self._checkpoint(batch)

    def _verify_uploads(self, batch: dict[str, Any]) -> bool:
        batch.update(status="verifying", error=None)
        self._checkpoint(batch)
        deadline = time.monotonic() + self.verification_timeout
        while True:
            for upload in batch["uploads"]:
                upload["verified"] = self.cloud.verified(
                    upload["remote_path"], upload["size_bytes"], upload["hashes"]
                )
                self._checkpoint(batch)
            if all(upload["verified"] for upload in batch["uploads"]):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                batch.update(status="waiting_cloud", error=None)
                self._checkpoint(batch)
                return False
            self.stop.wait(min(self.verification_interval, remaining))
            self._checkpoint(batch)

    def _download_file(
        self, source: Any, destination: Path, batch: dict[str, Any], *, track_progress: bool = False
    ) -> dict[str, str]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".partial")
        with temporary.open("wb") as output:
            for offset in range(0, source.asset.size_bytes, CHUNK_SIZE):
                self._checkpoint(batch)
                data = source.read_range(offset, CHUNK_SIZE)
                if len(data) != min(CHUNK_SIZE, source.asset.size_bytes - offset):
                    raise ConfigurationError("Incomplete RAW download")
                output.write(data)
                if track_progress:
                    batch["bytes_done"] = (
                        sum(i["size_bytes"] for i in batch["items"] if i["downloaded"])
                        + offset
                        + len(data)
                    )
            output.flush()
            os.fsync(output.fileno())
        hashes = file_hashes(temporary)
        expected = getattr(source, "expected_hashes", source.asset.file_hashes)
        for key in set(expected) & {"1", "2", "sha256"}:
            if hashes[key] != expected[key].lower():
                raise ConfigurationError("Downloaded file does not match the cloud hash")
        temporary.replace(destination)
        return hashes

    def _download(self, batch: dict[str, Any]) -> None:
        batch.update(status="downloading", error=None)
        folder = Path(batch["folder"])
        required = sum(i["size_bytes"] for i in batch["items"] if not i["downloaded"])
        if shutil.disk_usage(folder).free < required + 512 * 1024 * 1024:
            raise ConfigurationError("Not enough space for the selected RAW files")
        self._checkpoint(batch)
        for item in batch["items"]:
            if item["downloaded"]:
                continue
            provider = self._input_provider(item)
            source = provider.source(item["remote_path"])
            if source.asset.fingerprint != item["fingerprint"]:
                raise ConfigurationError(f"Archive file changed since indexing: {item['name']}")
            destination = folder / item["local_path"]
            item["original_hashes"] = self._download_file(
                source, destination, batch, track_progress=True
            )
            for sidecar in provider.sidecars(item["remote_path"]):
                sidecar_source = provider.source(sidecar)
                target = (
                    destination.with_name(destination.name + ".dop")
                    if sidecar_source.asset.extension == "dop"
                    else destination.with_suffix(".xmp")
                )
                self._download_file(sidecar_source, target, batch)
            self._restore_edits(item, destination, batch)
            self._write_metadata(item, destination)
            item["downloaded"] = True
            batch["bytes_done"] = sum(i["size_bytes"] for i in batch["items"] if i["downloaded"])
            self._checkpoint(batch)
        batch.update(status="ready", error=None)
        self._checkpoint(batch)

    def _input_provider(self, item: dict) -> Any:
        source = item.get("source")
        if source and source.get("folder"):
            # Refresh the root after an explicit reconnect; imported identities stay stable.
            current = self.locations.source(source["id"], self.locations.get())
            try:
                path = contained_path(check_folder(current["folder"]), item["archive_relative"])
                if path.is_file():
                    return FilesystemEditingSource(current, self.locations.state_dir)
            except (OSError, ConfigurationError):
                pass
            copies = ArchiveCopies(self.locations.state_dir)
            if copies.get(item["provider"], item["remote_path"]) is not None:
                return ArchivedEditingSource(copies, item["provider"])
            raise ConfigurationError(
                "Reconnect the original drive; no verified archive copy is available"
            )
        if source and not source.get("legacy"):
            raise ValueError("Reconnect the original photo folder in Locations")
        return self.cloud

    def _write_metadata(self, item: dict[str, Any], original: Path) -> None:
        annotation = item.get("annotation")
        if annotation is None:
            return
        sidecar = original.with_suffix(".xmp")
        arguments = [
            "exiftool",
            f"-XMP-xmp:Rating={annotation['rating']}",
            f"-XMP-dc:Description={annotation['caption']}",
        ]
        if not sidecar.exists():
            # Start with an XMP document instead of copying embedded RAW tags, which can
            # reintroduce the camera's old rating after the requested metadata values.
            sidecar.write_text(
                '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
                '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                '<rdf:Description rdf:about=""/></rdf:RDF></x:xmpmeta>'
            )
        arguments.extend(["-overwrite_original", str(sidecar)])
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            raise ConfigurationError(
                "Could not save the working copy’s XMP metadata: " + result.stderr.strip()[:500]
            )

    def _local_path(self, folder: Path, relative: str) -> Path:
        path = folder / relative
        if not path.resolve().is_relative_to(folder.resolve()) or any(
            part.is_symlink() for part in [path, *path.parents] if part != self.root.parent
        ):
            raise ValueError("Editing files must stay inside their original working folder")
        return path

    def prepare(self, identifier: str) -> dict[str, Any]:
        with self.lock:
            batch = self.get(identifier)
            self._check_idle()
            if self.locations and batch.get("phase") not in {"upload", "cleanup"}:
                return self._prepare_exports(batch)
            if batch["status"] not in {"ready", "prepared"} and not (
                batch["status"] == "needs_attention" and batch["phase"] == "upload"
            ):
                raise ValueError("Download the complete batch before finishing edits")
            folder = Path(batch["folder"])
            uploads = []
            originals = {item["local_path"]: item for item in batch["items"]}
            parent = commonpath(
                [str(PurePosixPath(i["remote_path"]).parent) for i in batch["items"]]
            )
            if parent == "/":
                raise ValueError("Choose photos from one cloud archive for each editing batch")
            remote_root = PurePosixPath(parent) / "_Latent Edits" / identifier / str(uuid4())
            for relative, item in originals.items():
                if not self._local_path(folder, relative).is_file():
                    raise ValueError(f"Working RAW is missing: {item['name']}")
            for path in sorted(folder.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Editing folders must not contain symbolic links")
                if not path.is_file() or path.name.endswith(".partial"):
                    continue
                relative = str(path.relative_to(folder))
                original = originals.get(relative)
                if (
                    original is None
                    and path.suffix.lower() not in EXPORT_EXTENSIONS | SIDECAR_EXTENSIONS
                ):
                    continue
                hashes = file_hashes(path)
                if original and hashes["sha256"] == original["original_hashes"]["sha256"]:
                    continue
                uploads.append(
                    {
                        "local_path": relative,
                        "remote_path": str(remote_root / relative),
                        "size_bytes": path.stat().st_size,
                        "hashes": hashes,
                        "verified": False,
                    }
                )
            if not uploads:
                raise ValueError(
                    "No edits found. Save PhotoLab sidecars or export into the batch folder first."
                )
            batch.update(status="prepared", uploads=uploads, upload_destination=str(remote_root))
            self._save(batch)
            return self.get(identifier)

    def finish(
        self, identifier: str, policy: str, *, exports: list[dict] | None = None
    ) -> dict[str, Any]:
        if self.get(identifier).get("output_mode") == "filesystem":
            return self._finish_exports(identifier, exports)
        if policy not in {"keep_exports", "clear_batch"}:
            raise ValueError("Choose which local files to keep before uploading")
        with self.lock:
            batch = self.get(identifier)
            if batch["status"] != "prepared":
                raise ValueError("Review the batch files before uploading")
            self._check_idle()
            batch.update(cleanup_policy=policy, phase="upload")
            self._save(batch)
            self._start(identifier, self._upload)
            return self.get(identifier)

    def _upload(self, batch: dict[str, Any]) -> None:
        batch.update(status="uploading", error=None)
        folder = Path(batch["folder"])
        for upload in batch["uploads"]:
            self._checkpoint(batch)
            path = self._local_path(folder, upload["local_path"])
            if not path.is_file() or path.is_symlink() or file_hashes(path) != upload["hashes"]:
                raise ValueError(
                    "An edit changed after review. Keep local files and review the batch again."
                )
            if upload["verified"]:
                continue
            remote = PurePosixPath(upload["remote_path"])
            # Only create the new batch tree beneath an existing archive directory.
            parts = remote.parts
            marker = len(parts) - 1 - list(reversed(parts)).index("_Latent Edits")
            parent = str(PurePosixPath(*parts[:marker]))
            for part in parts[marker:-1]:
                parent = self.cloud.ensure_folder(parent, part)
            self.cloud.upload(path, upload["remote_path"], upload["hashes"])
        if not self._verify_uploads(batch):
            return
        batch.update(status="uploaded", error=None, phase="cleanup")
        self._checkpoint(batch)
        self._cleanup(batch)

    def _prepare_exports(self, batch: dict) -> dict:
        if batch["status"] not in {"ready", "prepared", "saved", "cloud_verified"} and not (
            batch["status"] == "needs_attention" and batch["phase"] == "copy"
        ):
            raise ValueError("Finish downloading before reviewing exported photos")
        snapshot = self.locations.output_snapshot()
        if batch["status"] == "cloud_verified":
            batch["previous_cloud_uploads"] = batch["uploads"]
        folder = Path(batch["folder"])
        uploads = []
        originals = {item["local_path"]: item for item in batch["items"]}
        for item in batch["items"]:
            if "archive_relative" not in item:
                item.update(self.locations.reference(item))
            if item.get("provider") is None:
                item["provider"] = item["source"]["provider"]
        for path in sorted(folder.rglob("*")):
            if path.is_symlink():
                raise ValueError("Editing folders must not contain symbolic links")
            if not path.is_file() or path.suffix.lower() not in EXPORT_EXTENSIONS:
                continue
            relative = path.relative_to(folder).as_posix()
            self._local_path(folder, relative)
            hashes = file_hashes(path)
            original = originals.get(relative)
            if original and hashes == original["original_hashes"]:
                continue
            exact = [
                i
                for i in batch["items"]
                if Path(i["local_path"]).stem.casefold() == path.stem.casefold()
            ]
            matching = exact or [
                i
                for i in batch["items"]
                if any(
                    path.stem.casefold().startswith(
                        Path(i["local_path"]).stem.casefold() + separator
                    )
                    for separator in ("_", "-")
                )
            ]
            item = original or (matching[0] if len(matching) == 1 else None)
            upload = {
                "local_path": relative,
                "remote_path": "",
                "size_bytes": path.stat().st_size,
                "hashes": hashes,
                "verified": False,
                "asset_id": item["asset_id"] if item else None,
            }
            if item:
                upload["remote_path"] = self._export_path(snapshot, item, upload)
            uploads.append(upload)
        if not uploads:
            raise ValueError(
                "No finished photos found. "
                "Export JPEG, HEIC, TIFF or PNG into this editing folder first"
            )
        batch.update(
            status="prepared",
            output_mode="filesystem",
            output_snapshot=snapshot,
            uploads=uploads,
            phase="review",
            error=None,
            upload_destination=str(Path(snapshot["folder"]["path"]) / snapshot["prefix"]),
        )
        self._save(batch)
        return self.get(batch["id"])

    @staticmethod
    def _export_path(snapshot: dict, item: dict, upload: dict) -> str:
        relative = PurePosixPath(item["archive_relative"]).parent / Path(upload["local_path"]).name
        return output_destination(snapshot, str(relative), upload["hashes"]["sha256"])

    def _finish_exports(self, identifier: str, exports: list[dict] | None) -> dict:
        with self.lock:
            self._check_idle()
            batch = self.get(identifier)
            if batch["status"] != "prepared":
                raise ValueError("Review finished photos before saving")
            if not isinstance(exports, list) or not exports:
                raise ValueError("Select at least one finished photo to save")
            by_path = {u["local_path"]: u for u in batch["uploads"]}
            items = {i["asset_id"]: i for i in batch["items"]}
            selected = []
            used = set()
            for selection in exports:
                if not isinstance(selection, dict) or set(selection) != {"local_path", "asset_id"}:
                    raise ValueError("Each selected export needs its original photo")
                relative, asset_id = selection["local_path"], selection["asset_id"]
                if relative not in by_path or relative in used or asset_id not in items:
                    raise ValueError("The selected export or original photo is no longer available")
                used.add(relative)
                upload = dict(by_path[relative])
                if upload["asset_id"] != asset_id or not upload["remote_path"]:
                    raise ValueError(
                        "Match this export to its original and review the destination before saving"
                    )
                selected.append(upload)
            destinations = [u["remote_path"].casefold() for u in selected]
            if len(set(destinations)) != len(destinations):
                raise ValueError(
                    "Two selected photos have the same output name. "
                    "Rename an export and refresh files"
                )
            batch.update(
                uploads=selected, status="copying", phase="copy", cleanup_policy="keep_all"
            )
            self._save(batch)
            self._start(identifier, self._copy_exports)
            return self.get(identifier)

    def map_exports(self, identifier: str, exports: list[dict] | None) -> dict:
        with self.lock:
            self._check_idle()
            batch = self.get(identifier)
            if batch["status"] != "prepared" or batch.get("output_mode") != "filesystem":
                raise ValueError("Review finished photos before matching originals")
            if not isinstance(exports, list) or not exports:
                raise ValueError("Choose an original photo for this export")
            by_path = {u["local_path"]: u for u in batch["uploads"]}
            items = {i["asset_id"]: i for i in batch["items"]}
            for selection in exports:
                if not isinstance(selection, dict) or set(selection) != {"local_path", "asset_id"}:
                    raise ValueError("Each selected export needs its original photo")
                if selection["local_path"] not in by_path or selection["asset_id"] not in items:
                    raise ValueError("Choose an original from this editing batch")
                upload = by_path[selection["local_path"]]
                upload["asset_id"] = selection["asset_id"]
                upload["remote_path"] = self._export_path(
                    batch["output_snapshot"], items[upload["asset_id"]], upload
                )
            self._save(batch)
            return self.get(identifier)

    def _copy_exports(self, batch: dict) -> None:
        batch.update(status="copying", error=None)
        self._checkpoint(batch)
        for upload in batch["uploads"]:
            self._checkpoint(batch)
            local = self._local_path(Path(batch["folder"]), upload["local_path"])
            copy_finished_photo(
                local, upload["remote_path"], batch["output_snapshot"], upload["hashes"]["sha256"]
            )
            upload["verified"] = True
            self._checkpoint(batch)
        # A mount read-back proves the filesystem copy, not the provider's cloud sync.
        # Working originals, edits and exports stay available until the user removes them.
        batch.update(status="saved", error=None)
        self._checkpoint(batch)

    def _restore_edits(
        self, item: dict[str, Any], destination: Path, batch: dict[str, Any]
    ) -> None:
        for previous in self.batches():
            if previous["id"] == batch["id"] or previous["status"] not in {"complete", "saved"}:
                continue
            match = next(
                (
                    i
                    for i in previous["items"]
                    if i["remote_path"] == item["remote_path"]
                    and i.get("provider") == item.get("provider")
                    and i["fingerprint"] == item["fingerprint"]
                ),
                None,
            )
            if match is None:
                continue
            previous_raw = Path(match["local_path"])
            sidecars = {
                previous_raw.name.lower() + ".dop",
                previous_raw.with_suffix(".xmp").name.lower(),
            }
            if previous["status"] == "saved":
                previous_folder = Path(previous["folder"])
                for old, target in [
                    (
                        Path(str(previous_raw) + ".dop"),
                        destination.with_name(destination.name + ".dop"),
                    ),
                    (previous_raw.with_suffix(".xmp"), destination.with_suffix(".xmp")),
                ]:
                    local = self._local_path(previous_folder, str(old))
                    if local.is_file():
                        shutil.copyfile(local, target)
                break
            for upload in previous["uploads"]:
                relative = Path(upload["local_path"])
                if (
                    relative.parent != Path(match["local_path"]).parent
                    or relative.name.lower() not in sidecars
                ):
                    continue
                local = Path(previous["folder"]) / relative
                target = (
                    destination.with_name(destination.name + ".dop")
                    if relative.suffix.lower() == ".dop"
                    else destination.with_suffix(".xmp")
                )
                if (
                    local.is_file()
                    and not local.is_symlink()
                    and file_hashes(local) == upload["hashes"]
                ):
                    shutil.copyfile(local, target)
                else:
                    source = self.cloud.source(upload["remote_path"])
                    downloaded = self._download_file(source, target, batch)
                    if downloaded != upload["hashes"]:
                        raise ConfigurationError("Saved cloud edits changed. Local RAWs are kept.")
            break

    def _cleanup(self, batch: dict[str, Any]) -> None:
        # The explicit retention choice is captured before uploading. No recursive deletion.
        policy = batch["cleanup_policy"]
        if policy not in {"keep_exports", "clear_batch"}:
            raise ValueError("Choose local retention before cleanup")
        batch.update(status="cleaning", error=None)
        self._checkpoint(batch)
        for upload in batch["uploads"]:
            if not self.cloud.verified(
                upload["remote_path"], upload["size_bytes"], upload["hashes"]
            ):
                raise ConfigurationError("Cloud verification changed. Local files are kept.")
        folder = Path(batch["folder"])
        expected = {item["local_path"]: item["original_hashes"] for item in batch["items"]}
        expected.update({upload["local_path"]: upload["hashes"] for upload in batch["uploads"]})
        # Preflight the entire snapshot before deleting anything.
        for relative, hashes in expected.items():
            path = self._local_path(folder, relative)
            if path.exists() and (path.is_symlink() or file_hashes(path) != hashes):
                raise ValueError(
                    "A local file changed after upload. Cleanup stopped; files are kept."
                )
        removed = set(batch.get("removed_files", []))
        targets = (
            set(expected) if policy == "clear_batch" else {i["local_path"] for i in batch["items"]}
        )
        for relative in sorted(targets):
            self._checkpoint(batch)
            path = self._local_path(folder, relative)
            if path.exists():
                # Recheck immediately before deleting only this known working copy.
                if file_hashes(path) != expected[relative]:
                    raise ValueError("A file changed during cleanup. Remaining files are kept.")
                path.unlink()
            removed.add(relative)
            batch["removed_files"] = sorted(removed)
            self._checkpoint(batch)
        batch.update(status="complete", error=None)
        self._checkpoint(batch)
