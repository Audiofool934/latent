"""Find reviewable interval-shooting segments from local EXIF metadata."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from statistics import mean, median
from typing import Any

from .errors import ConfigurationError

_FRAME_NUMBER = re.compile(r"^(.*?)(\d+)(\D*)$")
_SEED_GAPS = 8


@dataclass(frozen=True)
class AuditOptions:
    min_frames: int = 30
    min_duration_seconds: float = 120
    min_interval_seconds: float = 1
    max_interval_seconds: float = 120

    def validate(self) -> None:
        if self.min_frames < _SEED_GAPS + 1:
            raise ConfigurationError(f"min-frames must be at least {_SEED_GAPS + 1}")
        values = (
            self.min_duration_seconds, self.min_interval_seconds, self.max_interval_seconds,
        )
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ConfigurationError("time-lapse timing bounds must be positive and finite")
        if self.max_interval_seconds < self.min_interval_seconds:
            raise ConfigurationError("maximum interval must be at least the minimum interval")


@dataclass(frozen=True)
class _Frame:
    asset_ids: tuple[int, ...]
    provider: str
    path: str
    camera: str
    serial: str
    lens: str
    lens_serial: str
    captured: str
    seconds: float
    resolution: float
    offset: str
    number: int | None
    pattern: tuple[str, str]

    @property
    def parent(self) -> str:
        return str(PurePosixPath(self.path).parent)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _serial(value: Any) -> str:
    result = _text(value)
    if result.casefold() in {"", "unknown", "none", "n/a", "0"} or set(result) == {"0"}:
        return ""
    return result


def _capture(value: Any, metadata: Mapping[str, Any]) -> tuple[float, float, str]:
    text = _text(value)
    if len(text) < 19:
        raise ValueError("capture time is missing or incomplete")
    normalized = text[:10].replace(":", "-") + text[10:]
    captured = datetime.fromisoformat(normalized)
    resolution = 1.0
    fraction = re.search(r"\.(\d+)", normalized)
    subsecond = _text(metadata.get("SubSecTimeOriginal"))
    if subsecond.isdecimal():
        digits = subsecond[:6]
        captured = captured.replace(microsecond=int(digits.ljust(6, "0")))
        resolution = 10 ** -len(digits)
    elif fraction:
        resolution = 10 ** -min(6, len(fraction.group(1)))
    offset = _text(metadata.get("OffsetTimeOriginal"))
    if captured.tzinfo is None and offset:
        captured = datetime.fromisoformat(captured.isoformat() + offset)
    # Missing timezone is camera-local time, independent of the host's timezone.
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=UTC)
        offset_key = "unknown"
    else:
        offset_key = str(captured.utcoffset())
    return captured.timestamp(), resolution, offset_key


def _frame(row: Mapping[str, Any]) -> _Frame:
    metadata = row.get("metadata")
    if metadata is None:
        metadata = json.loads(row.get("exif_json") or "{}")
    if not isinstance(metadata, dict):
        raise ValueError("EXIF metadata is not an object")
    camera = _text(row.get("camera_model") or metadata.get("Model"))
    lens = _text(row.get("lens_model") or metadata.get("LensModel"))
    if not camera or not lens:
        raise ValueError("camera or lens is missing")
    capture = row.get("capture_at") or metadata.get("DateTimeOriginal")
    seconds, resolution, offset = _capture(capture, metadata)
    path = str(row["remote_path"])
    match = _FRAME_NUMBER.fullmatch(PurePosixPath(path).stem.casefold())
    serial = next((
        value for key in ("BodySerialNumber", "SerialNumber", "InternalSerialNumber")
        if (value := _serial(metadata.get(key)))
    ), "")
    return _Frame(
        (int(row["id"]),), str(row["provider"]), path, camera, serial, lens,
        _serial(metadata.get("LensSerialNumber")), _text(capture), seconds, resolution, offset,
        int(match.group(2)) if match else None,
        (match.group(1), match.group(3)) if match else ("", ""),
    )


def _stages(frames: list[_Frame]) -> Iterable[tuple[list[_Frame], str]]:
    # Camera counters preserve clock resets that a pure timestamp sort would hide.
    numbered = all(frame.number is not None for frame in frames)
    frames.sort(key=lambda frame: (
        frame.number if numbered else frame.seconds, frame.seconds, frame.path,
    ))
    stage: list[_Frame] = []
    reason = "directory/device/naming stream"
    for frame in frames:
        boundary = ""
        if stage:
            previous = stage[-1]
            if (frame.lens, frame.lens_serial) != (previous.lens, previous.lens_serial):
                boundary = "lens changed"
            elif frame.seconds <= previous.seconds:
                boundary = "clock reset or unresolved simultaneous captures"
            elif numbered and frame.number - previous.number > 10:
                boundary = "filename counter gap"
        if boundary:
            yield stage, reason
            stage, reason = [], boundary
        stage.append(frame)
    if stage:
        yield stage, reason


def _tolerance(interval: float, resolution: float) -> float:
    return max(0.05, interval * 0.05, min(resolution * 0.75, interval * 0.2))


def _multiple(gap: float, interval: float, tolerance: float) -> int:
    multiple = round(gap / interval)
    if 1 <= multiple <= 3 and abs(gap - multiple * interval) <= tolerance + 1e-6:
        return multiple
    return 0


def _runs(frames: list[_Frame], options: AuditOptions) -> Iterable[tuple[list[_Frame], float]]:
    gaps = [b.seconds - a.seconds for a, b in zip(frames, frames[1:], strict=False)]
    start = 0
    while start + _SEED_GAPS <= len(gaps):
        seed = gaps[start:start + _SEED_GAPS]
        interval = median(seed)
        resolution = max(frame.resolution for frame in frames[start:start + _SEED_GAPS + 1])
        tolerance = _tolerance(interval, resolution)
        if not options.min_interval_seconds <= interval <= options.max_interval_seconds:
            start += 1
            continue
        multiples = [_multiple(gap, interval, tolerance) for gap in seed]
        if 0 in multiples or multiples.count(1) < _SEED_GAPS - 1:
            start += 1
            continue
        end = start + _SEED_GAPS
        while end < len(gaps):
            multiple = _multiple(gaps[end], interval, tolerance)
            if not multiple:
                break
            # A sustained doubled/tripled cadence is a new stage, not endless dropped frames.
            if multiple > 1 and end + 3 <= len(gaps) and all(
                _multiple(gap, interval, tolerance) == multiple for gap in gaps[end:end + 3]
            ):
                break
            multiples.append(multiple)
            end += 1
        run = frames[start:end + 1]
        duration = run[-1].seconds - run[0].seconds
        if (len(run) >= options.min_frames and duration >= options.min_duration_seconds
                and multiples.count(1) / len(multiples) >= 0.9):
            yield run, interval
        # Each frame belongs to at most one candidate segment.
        start = end + 1


def _candidate(frames: list[_Frame], interval: float, boundary: str) -> dict[str, Any]:
    first, last = frames[0], frames[-1]
    gaps = [b.seconds - a.seconds for a, b in zip(frames, frames[1:], strict=False)]
    multiples = [max(1, round(gap / interval)) for gap in gaps]
    duration = last.seconds - first.seconds
    estimated = duration / sum(multiples)
    adjusted = [gap / multiple for gap, multiple in zip(gaps, multiples, strict=True)]
    deviations = [abs(value - estimated) for value in adjusted]
    warnings = ["Regular timing is candidate evidence; visual/session review is still required."]
    if not first.serial:
        warnings.append("Camera model only: two bodies of the same model cannot be distinguished.")
    if max(frame.resolution for frame in frames) >= 1:
        warnings.append("Capture timestamps have only one-second precision.")
    if first.number is None:
        warnings.append(
            "No filename counter was available to check shooting order or clock resets."
        )
    return {
        "classification": "candidate",
        "provider": first.provider,
        "directory": first.parent,
        "camera_model": first.camera,
        "body_serial_available": bool(first.serial),
        "lens_model": first.lens,
        "lens_serial_available": bool(first.lens_serial),
        "start_capture_at": first.captured,
        "end_capture_at": last.captured,
        "first_file": PurePosixPath(first.path).name,
        "last_file": PurePosixPath(last.path).name,
        "frame_count": len(frames),
        "asset_ids": [asset_id for frame in frames for asset_id in frame.asset_ids],
        "duration_seconds": round(duration, 3),
        "interval_seconds": round(estimated, 3),
        "median_absolute_deviation_seconds": round(median(deviations), 3),
        "direct_interval_fraction": round(mean(multiple == 1 for multiple in multiples), 4),
        "possible_missing_frames": sum(multiple - 1 for multiple in multiples),
        "stage_boundary": boundary,
        "warnings": warnings,
    }


def analyze_timelapse(
    rows: Iterable[Mapping[str, Any]], *, options: AuditOptions | None = None,
) -> dict[str, Any]:
    """Analyze supplied metadata only; never classify, hide, or enqueue library assets."""
    options = options or AuditOptions()
    options.validate()
    started = time.perf_counter()
    counts: Counter[str] = Counter()
    groups: dict[tuple, dict[tuple, _Frame]] = defaultdict(dict)
    for row in rows:
        counts["scanned_assets"] += 1
        try:
            frame = _frame(row)
        except (ValueError, TypeError, KeyError, OverflowError):
            counts["missing_or_invalid_metadata"] += 1
            continue
        counts["usable_assets"] += 1
        counts["assets_with_body_serial"] += bool(frame.serial)
        counts["assets_with_subsecond_time"] += frame.resolution < 1
        stream = (
            frame.provider, frame.parent, frame.camera.casefold(), frame.serial,
            frame.pattern, frame.offset,
        )
        # RAW/JPEG representations of one exposure must not inflate the frame count.
        key = (PurePosixPath(frame.path).stem.casefold(), frame.seconds,
               frame.lens, frame.lens_serial)
        existing = groups[stream].get(key)
        if existing:
            frame = replace(existing, asset_ids=existing.asset_ids + frame.asset_ids)
            counts["duplicate_representations"] += 1
        groups[stream][key] = frame
    candidates = []
    for group in groups.values():
        for stage, boundary in _stages(list(group.values())):
            for index, (run, interval) in enumerate(_runs(stage, options)):
                candidates.append(_candidate(
                    run, interval, boundary if index == 0 else "pause or cadence change",
                ))
    candidates.sort(key=lambda item: (item["directory"], item["first_file"]))
    return {
        "summary": {
            **{key: counts[key] for key in (
                "scanned_assets", "usable_assets", "missing_or_invalid_metadata",
                "assets_with_body_serial", "assets_with_subsecond_time",
                "duplicate_representations",
            )},
            "candidate_segments": len(candidates),
            "candidate_frames": sum(item["frame_count"] for item in candidates),
            "elapsed_seconds": round(time.perf_counter() - started, 4),
            "archive_bytes_read": 0,
            "api_requests": 0,
            "catalog_modified": False,
            "archive_modified": False,
        },
        "parameters": vars(options),
        "candidates": candidates,
    }


def audit_catalog(
    state_dir: Path, *, paths: list[str] | None = None, options: AuditOptions | None = None,
) -> dict[str, Any]:
    database = state_dir.expanduser().resolve() / "index.sqlite"
    if not database.is_file():
        raise ConfigurationError("No local catalog exists; index photos before auditing EXIF")
    roots = [path.rstrip("/") + "/" for path in (paths or [])]
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        records = connection.execute("""
            SELECT id,provider,remote_path,capture_at,camera_model,lens_model,exif_json FROM assets
        """)
        return analyze_timelapse(
            (dict(row) for row in records
             if not roots or any(row["remote_path"].startswith(root) for root in roots)),
            options=options,
        )
