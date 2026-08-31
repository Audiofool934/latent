"""Read-only byte-range providers for archived media."""

from __future__ import annotations

import plistlib
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .errors import ArchiveSafetyError, ConfigurationError
from .models import RemoteAsset

DEFAULT_CLOUDDRIVE_ENDPOINT = "127.0.0.1:29798"
DEFAULT_CLOUDDRIVE_PLIST = Path.home() / (
    "Library/Group Containers/"
    "group.com.clouddrive2.CloudDrive2/Library/Preferences/"
    "group.com.clouddrive2.CloudDrive2.plist"
)
_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")


class RangeSource(Protocol):
    asset: RemoteAsset
    range_requests: int
    bytes_transferred: int
    metadata_elapsed_ms: int

    def read_range(self, start: int, length: int) -> bytes: ...


class CloudDriveRangeSource:
    """Reads exact HTTP ranges through CloudDrive without touching the mount."""

    def __init__(
        self,
        remote_path: str,
        *,
        endpoint: str = DEFAULT_CLOUDDRIVE_ENDPOINT,
        plist_path: Path = DEFAULT_CLOUDDRIVE_PLIST,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.remote_path = remote_path
        self.endpoint, self.http_scheme = _normalize_endpoint(endpoint)
        self.plist_path = plist_path.expanduser()
        self.timeout_seconds = timeout_seconds
        self._token = _load_device_token(self.plist_path)
        self.range_requests = 0
        self.bytes_transferred = 0
        metadata_started = time.perf_counter()
        self.asset = self._load_asset()
        self.metadata_elapsed_ms = round((time.perf_counter() - metadata_started) * 1000)

    def __enter__(self) -> CloudDriveRangeSource:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def _client(self):
        from clouddrive2_client import CloudDriveClient

        client = CloudDriveClient(self.endpoint)
        client.jwt_token = self._token
        return client

    def _load_asset(self) -> RemoteAsset:
        client = self._client()
        try:
            remote = client.find_file_by_path(self.remote_path)
        finally:
            client.close()
        if not remote or not remote.name:
            raise ConfigurationError(
                f"CloudDrive did not find the requested path: {self.remote_path}"
            )
        if remote.isDirectory:
            raise ConfigurationError(
                f"expected a file but received a directory: {self.remote_path}"
            )
        hashes = {str(key): str(value).lower() for key, value in remote.fileHashes.items()}
        provider = remote.CloudAPI.name or "CloudDrive"
        return RemoteAsset(
            provider=provider,
            remote_id=remote.id,
            remote_path=self.remote_path,
            name=remote.name,
            size_bytes=int(remote.size),
            write_time=_timestamp_text(remote.writeTime),
            file_hashes=hashes,
        )

    def read_range(self, start: int, length: int) -> bytes:
        if start < 0 or length <= 0:
            raise ValueError("range start must be non-negative and length must be positive")
        if start >= self.asset.size_bytes:
            raise ArchiveSafetyError("range starts beyond the remote file")
        expected_length = min(length, self.asset.size_bytes - start)
        end = start + expected_length - 1
        info = self._download_info()
        url = self._download_url(info, preview=False)
        headers = dict(info.additionalHeaders)
        if info.HasField("userAgent") and info.userAgent:
            headers["User-Agent"] = info.userAgent
        headers["Range"] = f"bytes={start}-{end}"
        headers["Accept-Encoding"] = "identity"
        request = Request(url, headers=headers)
        with urlopen(request, timeout=self.timeout_seconds) as response:
            if response.status != 206:
                raise ArchiveSafetyError(
                    "CloudDrive ignored the byte range; refusing a possible full-file download"
                )
            content_range = response.headers.get("Content-Range", "")
            match = _CONTENT_RANGE.fullmatch(content_range)
            if match is None:
                raise ArchiveSafetyError("CloudDrive returned an invalid Content-Range header")
            actual_start, actual_end = int(match.group(1)), int(match.group(2))
            if actual_start != start or actual_end != end:
                raise ArchiveSafetyError(
                    "CloudDrive returned a different byte range than requested"
                )
            content_length = response.headers.get("Content-Length")
            if content_length is None or int(content_length) != expected_length:
                raise ArchiveSafetyError("CloudDrive returned an unexpected range length")
            data = response.read(expected_length + 1)
        if len(data) != expected_length:
            raise ArchiveSafetyError("CloudDrive range body length did not match its headers")
        self.range_requests += 1
        self.bytes_transferred += len(data)
        return data

    def _download_info(self):
        client = self._client()
        try:
            return client.get_download_url(
                self.remote_path,
                preview=False,
                lazy_read=True,
                get_direct_url=True,
            )
        finally:
            client.close()

    def _download_url(self, info, *, preview: bool) -> str:
        if info.HasField("directUrl") and info.directUrl:
            direct = urlsplit(info.directUrl)
            if direct.scheme in {"http", "https"}:
                return info.directUrl
        relative = (
            info.downloadUrlPath.replace("{SCHEME}", self.http_scheme)
            .replace("{HOST}", self.endpoint)
            .replace("{PREVIEW}", str(preview).lower())
        )
        if not relative.startswith("/"):
            relative = "/" + relative
        return f"{self.http_scheme}://{self.endpoint}{relative}"


class MemoryRangeSource:
    """Deterministic in-memory provider used by automated tests."""

    def __init__(self, asset: RemoteAsset, data: bytes) -> None:
        if asset.size_bytes != len(data):
            raise ValueError("asset size must match the in-memory body")
        self.asset = asset
        self.data = data
        self.range_requests = 0
        self.bytes_transferred = 0
        self.metadata_elapsed_ms = 0

    def read_range(self, start: int, length: int) -> bytes:
        body = self.data[start : start + length]
        self.range_requests += 1
        self.bytes_transferred += len(body)
        return body


def _normalize_endpoint(endpoint: str) -> tuple[str, str]:
    if "://" not in endpoint:
        return endpoint.rstrip("/"), "http"
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError("CloudDrive endpoint must be an HTTP(S) host and port")
    return parsed.netloc, parsed.scheme


def _load_device_token(plist_path: Path) -> str:
    if not plist_path.is_file():
        raise ConfigurationError(f"CloudDrive preferences were not found: {plist_path}")
    with plist_path.open("rb") as handle:
        preferences: Mapping[str, object] = plistlib.load(handle)
    token = preferences.get("cloudapi.deviceToken")
    if not isinstance(token, str) or not token:
        raise ConfigurationError("CloudDrive device token is missing from local preferences")
    return token


def _timestamp_text(timestamp: object) -> str | None:
    seconds = getattr(timestamp, "seconds", 0)
    nanos = getattr(timestamp, "nanos", 0)
    if not seconds and not nanos:
        return None
    from datetime import UTC, datetime

    return datetime.fromtimestamp(seconds + nanos / 1_000_000_000, UTC).isoformat()
