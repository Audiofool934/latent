"""ARW preview discovery, image normalization, and cache pipeline."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import ConfigurationError, PreviewDecodeError, PreviewMetadataNotFound
from .models import PipelineResult, PreviewLocation, ProbeResult
from .provider import RangeSource
from .storage import CacheManager, StateStore, utc_now

KIB = 1024
MIB = 1024**2


class PreviewProbe(Protocol):
    def probe(self, data: bytes, suffix: str) -> ProbeResult: ...


class ExifToolProbe:
    """Uses ExifTool only on a bounded local prefix, never on the mounted RAW."""

    TAGS = (
        "PreviewImageStart",
        "PreviewImageLength",
        "JpgFromRawStart",
        "JpgFromRawLength",
        "ThumbnailOffset",
        "ThumbnailLength",
        "DateTimeOriginal",
        "CreateDate",
        "Model",
        "CameraModelName",
        "LensModel",
        "Lens",
        "Orientation",
        "ISO",
        "FNumber",
        "ExposureTime",
        "FocalLength",
    )
    CANDIDATES = (
        ("PreviewImage", "PreviewImageStart", "PreviewImageLength"),
        ("JpgFromRaw", "JpgFromRawStart", "JpgFromRawLength"),
        ("Thumbnail", "ThumbnailOffset", "ThumbnailLength"),
    )

    def __init__(self, executable: str = "exiftool") -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise ConfigurationError("ExifTool is required for the Phase 0 ARW probe")
        self.executable = resolved

    def probe(self, data: bytes, suffix: str) -> ProbeResult:
        normalized_suffix = suffix if suffix.startswith(".") else f".{suffix}"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=normalized_suffix, delete=False) as handle:
                handle.write(data)
                temporary = Path(handle.name)
            command = [self.executable, "-j", "-n"]
            command.extend(f"-{tag}" for tag in self.TAGS)
            command.append(str(temporary))
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        try:
            payload = json.loads(completed.stdout)
            metadata = dict(payload[0]) if payload else {}
        except (json.JSONDecodeError, TypeError, IndexError) as error:
            raise PreviewMetadataNotFound(
                "ExifTool could not read preview metadata from the bounded RAW prefix"
            ) from error
        metadata.pop("SourceFile", None)
        for tag, offset_key, length_key in self.CANDIDATES:
            offset = _positive_int(metadata.get(offset_key), allow_zero=True)
            length = _positive_int(metadata.get(length_key), allow_zero=False)
            if offset is not None and length is not None:
                return ProbeResult(
                    location=PreviewLocation(tag=tag, offset=offset, length=length),
                    metadata=metadata,
                )
        raise PreviewMetadataNotFound(
            "the bounded RAW prefix does not contain a usable embedded JPEG location"
        )


@dataclass(frozen=True)
class EncodedImage:
    data: bytes
    width: int
    height: int


@dataclass(frozen=True)
class RenderedVariants:
    preview: EncodedImage
    contact: EncodedImage


def render_variants(
    embedded_jpeg: bytes,
    metadata: Mapping[str, Any],
    *,
    screen_max_pixels: int = 2560,
    contact_max_pixels: int = 512,
) -> RenderedVariants:
    try:
        with Image.open(io.BytesIO(embedded_jpeg)) as opened:
            opened.load()
            embedded_orientation = opened.getexif().get(274)
            image = ImageOps.exif_transpose(opened)
            raw_orientation = _positive_int(metadata.get("Orientation"), allow_zero=False)
            if embedded_orientation in (None, 1) and raw_orientation not in (None, 1):
                image = _apply_orientation(image, raw_orientation)
            image = image.convert("RGB") if image.mode != "RGB" else image.copy()
            icc_profile = opened.info.get("icc_profile")
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise PreviewDecodeError("the embedded RAW preview is not a decodable JPEG") from error

    screen = image.copy()
    screen.thumbnail((screen_max_pixels, screen_max_pixels), Image.Resampling.LANCZOS)
    contact = screen.copy()
    contact.thumbnail((contact_max_pixels, contact_max_pixels), Image.Resampling.LANCZOS)
    return RenderedVariants(
        preview=_encode_jpeg(screen, quality=92, icc_profile=icc_profile),
        contact=_encode_jpeg(contact, quality=84, icc_profile=icc_profile),
    )


class PreviewPipeline:
    """Turns one remote RAW into indexed local preview variants."""

    def __init__(
        self,
        store: StateStore,
        cache: CacheManager,
        probe: PreviewProbe,
        *,
        initial_prefix_bytes: int = 512 * KIB,
        maximum_prefix_bytes: int = 8 * MIB,
        maximum_preview_bytes: int = 16 * MIB,
    ) -> None:
        if initial_prefix_bytes <= 0 or maximum_prefix_bytes < initial_prefix_bytes:
            raise ValueError("invalid RAW prefix bounds")
        self.store = store
        self.cache = cache
        self.probe = probe
        self.initial_prefix_bytes = initial_prefix_bytes
        self.maximum_prefix_bytes = maximum_prefix_bytes
        self.maximum_preview_bytes = maximum_preview_bytes

    def run(self, source: RangeSource, *, force: bool = False) -> PipelineResult:
        started_at = utc_now()
        started = time.perf_counter()
        requests_before = source.range_requests
        bytes_before = source.bytes_transferred
        asset = source.asset
        if asset.extension != "arw":
            raise ConfigurationError("Phase 0 currently accepts Sony ARW files only")
        asset_id = self.store.upsert_asset(asset)
        if not force:
            preview_hit = self.cache.get(asset_id, "preview", asset.fingerprint)
            contact_hit = self.cache.get(asset_id, "contact", asset.fingerprint)
            if preview_hit is not None and contact_hit is not None:
                probe_details = self.store.probe_details(asset_id)
                result = PipelineResult(
                    asset_id=asset_id,
                    remote_path=asset.remote_path,
                    source_size_bytes=asset.size_bytes,
                    cache_hit=True,
                    range_requests=source.range_requests - requests_before,
                    bytes_transferred=source.bytes_transferred - bytes_before,
                    provider_metadata_ms=source.metadata_elapsed_ms,
                    elapsed_ms=_elapsed_ms(started),
                    preview_path=preview_hit.path,
                    preview_size_bytes=preview_hit.size_bytes,
                    preview_width=preview_hit.width,
                    preview_height=preview_hit.height,
                    contact_path=contact_hit.path,
                    contact_size_bytes=contact_hit.size_bytes,
                    contact_width=contact_hit.width,
                    contact_height=contact_hit.height,
                    preview_tag=probe_details.get("preview_tag"),
                    preview_offset=probe_details.get("preview_offset"),
                    embedded_preview_bytes=probe_details.get("preview_length"),
                )
                self.store.record_fetch(result, started_at)
                return result

        prefix, probe_result = self._read_probe_prefix(source)
        location = probe_result.location
        if location.length > self.maximum_preview_bytes:
            raise PreviewMetadataNotFound(
                "embedded preview exceeds the configured bounded-download limit"
            )
        embedded = self._read_preview(source, prefix, location)
        rendered = render_variants(embedded, probe_result.metadata)
        preview_entry, preview_evicted = self.cache.put(
            asset_id=asset_id,
            variant="preview",
            fingerprint=asset.fingerprint,
            data=rendered.preview.data,
            width=rendered.preview.width,
            height=rendered.preview.height,
        )
        contact_entry, contact_evicted = self.cache.put(
            asset_id=asset_id,
            variant="contact",
            fingerprint=asset.fingerprint,
            data=rendered.contact.data,
            width=rendered.contact.width,
            height=rendered.contact.height,
        )
        self.store.update_probe(
            asset_id,
            probe_result,
            width=rendered.preview.width,
            height=rendered.preview.height,
        )
        result = PipelineResult(
            asset_id=asset_id,
            remote_path=asset.remote_path,
            source_size_bytes=asset.size_bytes,
            cache_hit=False,
            range_requests=source.range_requests - requests_before,
            bytes_transferred=source.bytes_transferred - bytes_before,
            provider_metadata_ms=source.metadata_elapsed_ms,
            elapsed_ms=_elapsed_ms(started),
            preview_path=preview_entry.path,
            preview_size_bytes=preview_entry.size_bytes,
            preview_width=preview_entry.width,
            preview_height=preview_entry.height,
            contact_path=contact_entry.path,
            contact_size_bytes=contact_entry.size_bytes,
            contact_width=contact_entry.width,
            contact_height=contact_entry.height,
            preview_tag=location.tag,
            preview_offset=location.offset,
            embedded_preview_bytes=location.length,
            evicted_paths=preview_evicted + contact_evicted,
        )
        self.store.record_fetch(result, started_at)
        return result

    def _read_probe_prefix(self, source: RangeSource) -> tuple[bytes, ProbeResult]:
        asset_size = source.asset.size_bytes
        maximum = min(asset_size, self.maximum_prefix_bytes)
        target = min(asset_size, self.initial_prefix_bytes)
        prefix = b""
        while True:
            if len(prefix) < target:
                prefix += source.read_range(len(prefix), target - len(prefix))
            try:
                return prefix, self.probe.probe(prefix, f".{source.asset.extension}")
            except PreviewMetadataNotFound:
                if target >= maximum:
                    raise
                target = min(maximum, target * 2)

    @staticmethod
    def _read_preview(
        source: RangeSource,
        prefix: bytes,
        location: PreviewLocation,
    ) -> bytes:
        preview_end = location.offset + location.length
        if preview_end <= len(prefix):
            return prefix[location.offset : preview_end]
        if location.offset < len(prefix):
            available = prefix[location.offset :]
            remainder = source.read_range(len(prefix), preview_end - len(prefix))
            return available + remainder
        return source.read_range(location.offset, location.length)


def _encode_jpeg(image: Image.Image, *, quality: int, icc_profile: bytes | None) -> EncodedImage:
    output = io.BytesIO()
    save_options: dict[str, Any] = {
        "format": "JPEG",
        "quality": quality,
        "optimize": True,
        "progressive": True,
    }
    if icc_profile:
        save_options["icc_profile"] = icc_profile
    image.save(output, **save_options)
    return EncodedImage(data=output.getvalue(), width=image.width, height=image.height)


def _apply_orientation(image: Image.Image, orientation: int) -> Image.Image:
    operations = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }
    operation = operations.get(orientation)
    return image.transpose(operation) if operation is not None else image


def _positive_int(value: object, *, allow_zero: bool) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if parsed < 0 or (parsed == 0 and not allow_zero):
        return None
    return parsed


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))
