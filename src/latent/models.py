"""Shared immutable values for providers, indexing, and preview generation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RemoteAsset:
    provider: str
    remote_id: str
    remote_path: str
    name: str
    size_bytes: int
    write_time: str | None = None
    file_hashes: Mapping[str, str] = field(default_factory=dict)

    @property
    def extension(self) -> str:
        return Path(self.name).suffix.lower().lstrip(".")

    @property
    def fingerprint(self) -> str:
        identity = {
            "provider": self.provider,
            "remote_id": self.remote_id,
            "remote_path": self.remote_path,
            "size_bytes": self.size_bytes,
            "write_time": self.write_time,
            "file_hashes": dict(sorted(self.file_hashes.items())),
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PreviewLocation:
    tag: str
    offset: int
    length: int


@dataclass(frozen=True)
class ProbeResult:
    location: PreviewLocation
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class CacheEntry:
    asset_id: int
    variant: str
    fingerprint: str
    path: Path
    size_bytes: int
    width: int
    height: int


@dataclass(frozen=True)
class PipelineResult:
    asset_id: int
    remote_path: str
    source_size_bytes: int
    cache_hit: bool
    range_requests: int
    bytes_transferred: int
    provider_metadata_ms: int
    elapsed_ms: int
    preview_path: Path
    preview_size_bytes: int
    preview_width: int
    preview_height: int
    contact_path: Path
    contact_size_bytes: int
    contact_width: int
    contact_height: int
    preview_tag: str | None
    preview_offset: int | None
    embedded_preview_bytes: int | None
    evicted_paths: tuple[Path, ...] = ()

    @property
    def transfer_ratio(self) -> float:
        if self.source_size_bytes <= 0:
            return 0.0
        return self.bytes_transferred / self.source_size_bytes

    @property
    def end_to_end_ms(self) -> int:
        return self.provider_metadata_ms + self.elapsed_ms

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "remote_path": self.remote_path,
            "source_size_bytes": self.source_size_bytes,
            "cache_hit": self.cache_hit,
            "range_requests": self.range_requests,
            "bytes_transferred": self.bytes_transferred,
            "transfer_ratio": self.transfer_ratio,
            "provider_metadata_ms": self.provider_metadata_ms,
            "pipeline_elapsed_ms": self.elapsed_ms,
            "end_to_end_ms": self.end_to_end_ms,
            "preview": {
                "path": str(self.preview_path),
                "size_bytes": self.preview_size_bytes,
                "width": self.preview_width,
                "height": self.preview_height,
                "embedded_tag": self.preview_tag,
                "embedded_offset": self.preview_offset,
                "embedded_size_bytes": self.embedded_preview_bytes,
            },
            "contact": {
                "path": str(self.contact_path),
                "size_bytes": self.contact_size_bytes,
                "width": self.contact_width,
                "height": self.contact_height,
            },
            "evicted_paths": [str(path) for path in self.evicted_paths],
            "archive_modified": False,
        }
