from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from latent.cli import main
from latent.errors import ConfigurationError
from latent.timelapse import AuditOptions, analyze_timelapse, audit_catalog


def frames(count=40, interval=10, *, start=0, first_id=1, serial="body-a", lens="35mm",
           directory="/archive/2025-06-01", prefix="DSC", subsecond=True):
    result = []
    origin = datetime(2025, 6, 1, 12)
    for i in range(count):
        capture = origin + timedelta(seconds=start + i * interval)
        result.append({
            "id": first_id + i,
            "provider": "test",
            "remote_path": f"{directory}/{prefix}{first_id + i:05d}.JPG",
            "camera_model": "Same Camera Model",
            "lens_model": lens,
            "capture_at": capture.strftime("%Y:%m:%d %H:%M:%S"),
            "metadata": {
                "BodySerialNumber": serial,
                **({"SubSecTimeOriginal": f"{capture.microsecond:06d}"} if subsecond else {}),
            },
        })
    return result


def candidates(rows, **options):
    return analyze_timelapse(rows, options=AuditOptions(**options))["candidates"]


def test_regular_run_allows_an_occasional_missing_frame():
    rows = frames(50)
    del rows[25]
    result, = candidates(rows)
    assert result["frame_count"] == 49
    assert result["interval_seconds"] == 10
    assert result["duration_seconds"] == 490
    assert result["possible_missing_frames"] == 1
    assert result["body_serial_available"] is True


def test_same_model_and_lens_from_two_interleaved_bodies_stay_separate():
    first = frames(serial="body-a")
    second = frames(start=5, first_id=101, serial="body-b")
    result = candidates(first + second)
    assert len(result) == 2
    assert [item["interval_seconds"] for item in result] == [10, 10]
    assert {tuple(item["asset_ids"]) for item in result} == {
        tuple(row["id"] for row in first), tuple(row["id"] for row in second),
    }


def test_switching_lenses_and_back_creates_three_stages():
    rows = (frames(lens="35mm") + frames(start=400, first_id=41, lens="85mm")
            + frames(start=800, first_id=81, lens="35mm"))
    result = candidates(rows)
    assert [item["lens_model"] for item in result] == ["35mm", "85mm", "35mm"]
    assert [item["frame_count"] for item in result] == [40, 40, 40]
    assert result[2]["stage_boundary"] == "lens changed"


@pytest.mark.parametrize(("second_start", "second_interval"), [(1600, 10), (410, 20)])
def test_pause_or_sustained_doubled_cadence_does_not_join_shooting_stages(
    second_start, second_interval,
):
    rows = frames() + frames(start=second_start, interval=second_interval, first_id=41)
    result = candidates(rows)
    assert [item["frame_count"] for item in result] == [40, 40]
    assert [item["interval_seconds"] for item in result] == [10, second_interval]
    assert result[1]["possible_missing_frames"] == 0


def test_clock_reset_is_preserved_by_camera_filename_order():
    rows = frames() + frames(start=-3600, first_id=41)
    result = candidates(rows)
    assert [item["frame_count"] for item in result] == [40, 40]
    assert result[1]["stage_boundary"] == "clock reset or unresolved simultaneous captures"


def test_folder_boundaries_and_unknown_body_identity_are_not_silently_merged():
    assert candidates(frames(20) + frames(20, start=200, directory="/archive/second-stage")) == []
    result, = candidates(frames(serial="", subsecond=False))
    assert result["body_serial_available"] is False
    assert any("two bodies" in warning for warning in result["warnings"])
    assert any("one-second" in warning for warning in result["warnings"])
    mixed = candidates(frames(serial="") + frames(start=400, first_id=41, serial="body-a"))
    assert len(mixed) == 2


def test_burst_timestamps_short_runs_and_irregular_shooting_are_not_candidates():
    assert candidates(frames(100, interval=0.2, subsecond=False)) == []
    assert candidates(frames(40, interval=0.2)) == []
    assert candidates(frames(20, interval=10)) == []
    rows = []
    offset = 0
    for i in range(120):
        rows += frames(1, start=offset, first_id=i + 1)
        offset += [2, 5, 19, 1, 45, 3][i % 6]
    assert candidates(rows) == []


def test_raw_and_jpeg_pairs_count_as_one_exposure():
    jpeg = frames()
    raw = [{**row, "id": row["id"] + 1000,
            "remote_path": row["remote_path"].replace(".JPG", ".ARW")} for row in jpeg]
    result = analyze_timelapse(jpeg + raw)
    assert result["summary"]["duplicate_representations"] == 40
    run, = result["candidates"]
    assert run["frame_count"] == 40
    assert len(run["asset_ids"]) == 80
    assert candidates(jpeg[:15] + raw[:15]) == []


def test_subsecond_and_timezone_metadata_keep_fractional_intervals():
    rows = frames(interval=4.25)
    for row in rows:
        row["metadata"]["OffsetTimeOriginal"] = "+08:00"
    result, = candidates(rows)
    assert result["interval_seconds"] == 4.25
    assert result["median_absolute_deviation_seconds"] == 0
    assert not any("one-second" in warning for warning in result["warnings"])


def test_missing_metadata_is_reported_without_breaking_other_candidates():
    rows = frames()
    rows += [{**rows[0], "id": 1000, "capture_at": "bad"},
             {**rows[0], "id": 1001, "lens_model": None}]
    result = analyze_timelapse(rows)
    assert result["summary"]["missing_or_invalid_metadata"] == 2
    assert result["summary"]["candidate_segments"] == 1


def test_local_audit_and_cli_are_read_only_and_paths_have_directory_boundaries(tmp_path, capsys):
    db = tmp_path / "index.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("""CREATE TABLE assets (
            id INTEGER,provider TEXT,remote_path TEXT,capture_at TEXT,
            camera_model TEXT,lens_model TEXT,exif_json TEXT)
        """)
        for row in frames() + frames(directory="/archive/2025-06-010", first_id=101):
            connection.execute("INSERT INTO assets VALUES (?,?,?,?,?,?,?)", (
                row["id"], row["provider"], row["remote_path"], row["capture_at"],
                row["camera_model"], row["lens_model"], json.dumps(row["metadata"]),
            ))
    original = db.read_bytes()
    result = audit_catalog(tmp_path, paths=["/archive/2025-06-01"])
    assert result["summary"]["scanned_assets"] == 40
    report = tmp_path / "report.json"
    assert main(["timelapse-audit", "--state-dir", str(tmp_path),
                 "--report", str(report), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["candidate_segments"] == 2
    assert json.loads(report.read_text())["summary"]["archive_bytes_read"] == 0
    assert db.read_bytes() == original
    assert main(["timelapse-audit", "--state-dir", str(tmp_path), "--report", str(db)]) == 2
    assert db.read_bytes() == original
    with pytest.raises(ConfigurationError, match="No local catalog"):
        audit_catalog(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("options", [AuditOptions(min_frames=2),
                                    AuditOptions(min_duration_seconds=float("nan")),
                                    AuditOptions(max_interval_seconds=0.5)])
def test_invalid_thresholds_fail_explicitly(options):
    with pytest.raises(ConfigurationError):
        analyze_timelapse([], options=options)
