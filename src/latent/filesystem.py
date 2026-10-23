"""Read-only local and mounted-folder media access."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .errors import ConfigurationError
from .formats import PHOTO_EXTENSIONS
from .locations import check_folder, contained_path, row_asset
from .models import RemoteAsset
from .provider import CloudDriveRangeSource, RangeSource
from .storage import StateStore

SUPPORTED_EXTENSIONS = PHOTO_EXTENSIONS


def filesystem_asset(source: dict, relative: str) -> RemoteAsset:
    path = contained_path(check_folder(source["folder"]), relative)
    info = path.stat()
    if not path.is_file():
        raise ValueError("Expected an original photo file")
    logical = str(PurePosixPath(source["logical_root"]) / relative)
    return RemoteAsset(
        source["provider"],
        logical,
        logical,
        path.name,
        info.st_size,
        datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
        {"mtime_ns": str(info.st_mtime_ns)},
    )


class FilesystemRangeSource:
    def __init__(self, source: dict, asset: RemoteAsset) -> None:
        self.source = source
        self.asset = asset
        self.relative = str(PurePosixPath(asset.remote_path).relative_to(source["logical_root"]))
        self.path = contained_path(check_folder(source["folder"]), self.relative)
        self.initial_stat = self.path.stat()
        if self.initial_stat.st_size != asset.size_bytes:
            raise ConfigurationError(
                "Original photo changed since indexing. Scan this source again"
            )
        if (
            not source["legacy"]
            and filesystem_asset(source, self.relative).fingerprint != asset.fingerprint
        ):
            raise ConfigurationError(
                "Original photo changed since indexing. Scan this source again"
            )
        self.range_requests = self.bytes_transferred = self.metadata_elapsed_ms = 0

    def read_range(self, start: int, length: int) -> bytes:
        if start < 0 or length <= 0 or start >= self.asset.size_bytes:
            raise ValueError("Invalid photo byte range")
        check_folder(self.source["folder"])
        contained_path(Path(self.source["folder"]["path"]), self.relative)
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                self.initial_stat.st_dev,
                self.initial_stat.st_ino,
                self.initial_stat.st_size,
                self.initial_stat.st_mtime_ns,
            ):
                raise ConfigurationError("Original changed during reading; retry after scanning")
            handle.seek(start)
            data = handle.read(min(length, self.asset.size_bytes - start))
        self.range_requests += 1
        self.bytes_transferred += len(data)
        return data


def range_source(source: dict, asset: RemoteAsset) -> RangeSource:
    """Read a CloudDrive-mounted folder through CloudDrive's API, never through the mount."""
    if (source.get("folder") or {}).get("cloud"):
        return CloudFolderRangeSource(source, asset)
    return FilesystemRangeSource(source, asset)


class CloudFolderRangeSource:
    """Reads a photo in a CloudDrive-mounted folder through CloudDrive's API.

    The photo keeps its Locations identity. Reads are exact byte ranges, and a JPEG uses
    CloudDrive's display-ready preview, so a preview never needs the whole original.
    """

    def __init__(self, source: dict, asset: RemoteAsset) -> None:
        relative = PurePosixPath(asset.remote_path).relative_to(source["logical_root"])
        self.cloud = CloudDriveRangeSource(
            str(PurePosixPath(source["folder"]["cloud"]["remote_path"]) / relative)
        )
        if self.cloud.asset.size_bytes != asset.size_bytes:
            raise ConfigurationError(
                "Original photo changed since indexing. Scan this source again"
            )
        self.asset = asset

    @property
    def range_requests(self) -> int:
        return self.cloud.range_requests

    @property
    def bytes_transferred(self) -> int:
        return self.cloud.bytes_transferred

    @property
    def metadata_elapsed_ms(self) -> int:
        return self.cloud.metadata_elapsed_ms

    def read_range(self, start: int, length: int) -> bytes:
        return self.cloud.read_range(start, length)

    def read_preview(self, maximum_bytes: int) -> bytes:
        return self.cloud.read_preview(maximum_bytes)


class FilesystemEditingSource:
    def __init__(self, source: dict, state_dir: Path) -> None:
        self.config = source
        self.state_dir = state_dir

    def source(self, path: str) -> FilesystemRangeSource:
        with StateStore(self.state_dir) as store:
            row = store.connection.execute(
                "SELECT * FROM assets WHERE provider=? AND remote_path=?",
                (self.config["provider"], path),
            ).fetchone()
        relative = str(PurePosixPath(path).relative_to(self.config["logical_root"]))
        asset = row_asset(row) if row else filesystem_asset(self.config, relative)
        return FilesystemRangeSource(self.config, asset)

    def sidecars(self, path: str) -> list[str]:
        relative = PurePosixPath(path).relative_to(self.config["logical_root"])
        root = check_folder(self.config["folder"])
        original = contained_path(root, str(relative))
        names = {original.name.casefold() + ".dop", original.stem.casefold() + ".xmp"}
        return [
            str(PurePosixPath(self.config["logical_root"]) / p.relative_to(root).as_posix())
            for p in original.parent.iterdir()
            if p.name.casefold() in names and not p.is_symlink() and p.is_file()
        ]
