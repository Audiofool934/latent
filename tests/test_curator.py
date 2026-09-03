from __future__ import annotations

import pytest

from latent.curator import build_grounded_curator_report


def test_curator_report_is_grounded_in_similarity_time_and_exif() -> None:
    source = {
        "id": 1,
        "capture_at": "2025:12:25 14:30:01",
        "camera_model": "ILCE-7RM5",
        "lens_model": "FE 24mm F1.4 GM",
    }
    neighbors = [
        {
            "id": 3,
            "capture_at": "2026:08:29 14:30:03",
            "camera_model": "ILCE-7RM5",
            "lens_model": "FE 24mm F1.4 GM",
            "similarity": 0.91,
        },
        {
            "id": 2,
            "capture_at": "2025:12:25 14:30:02",
            "camera_model": "ILCE-7RM5",
            "lens_model": "FE 35mm F1.4 GM",
            "similarity": 0.72,
        },
    ]

    report = build_grounded_curator_report(source, neighbors)

    assert report["headline"] == "Visual neighborhood across 2 capture dates"
    assert [observation["kind"] for observation in report["observations"]] == [
        "visual",
        "time",
        "camera",
        "lens",
    ]
    assert report["observations"][0]["facts"] == {
        "metric": "cosine_similarity",
        "nearest": 0.91,
        "lowest_included": 0.72,
    }
    assert report["observations"][1]["facts"]["capture_dates"] == [
        "2025-12-25",
        "2026-08-29",
    ]
    assert report["sequence_seed"]["ordering"] == "source_then_similarity"
    assert [item["asset_id"] for item in report["sequence_seed"]["items"]] == [1, 3, 2]
    assert report["limitations"] == [
        "This report describes local embedding distance and recorded EXIF only.",
        "It does not establish place, identity, event, intention, or story.",
    ]


def test_curator_report_limits_sequence_without_inventing_missing_evidence() -> None:
    source = {"id": 1, "capture_at": None, "camera_model": None, "lens_model": None}
    neighbors = [{"id": asset_id, "similarity": 1.0 - asset_id / 100} for asset_id in range(2, 10)]

    report = build_grounded_curator_report(source, neighbors, sequence_limit=3)

    assert report["headline"] == "Visual neighborhood with no recorded capture date"
    assert [observation["kind"] for observation in report["observations"]] == ["visual"]
    assert [item["asset_id"] for item in report["sequence_seed"]["items"]] == [1, 2, 3]


def test_curator_report_rejects_unscored_neighbors() -> None:
    with pytest.raises(ValueError, match="numeric similarity"):
        build_grounded_curator_report({"id": 1}, [{"id": 2}])
