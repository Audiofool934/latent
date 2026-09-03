from __future__ import annotations

import pytest

from latent.curator import build_grounded_curator_report, build_grounded_motif_report


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


def test_motif_report_prioritizes_cross_year_grounded_evidence() -> None:
    members = [
        {
            "id": 1,
            "capture_at": "2025:12:25 14:30:01",
            "camera_model": "ILCE-7RM5",
            "lens_model": "FE 24mm F1.4 GM",
        },
        {
            "id": 2,
            "capture_at": "2025:12:26 14:30:02",
            "camera_model": "ILCE-7RM5",
            "lens_model": "FE 35mm F1.4 GM",
        },
        {
            "id": 3,
            "capture_at": "2024:06:01 14:30:03",
            "camera_model": "ILCE-7RM5",
            "lens_model": "FE 24mm F1.4 GM",
        },
        {
            "id": 4,
            "capture_at": "2026:08:29 14:30:04",
            "camera_model": "ILCE-7RM2",
            "lens_model": "FE 24mm F1.4 GM",
        },
    ]

    report = build_grounded_motif_report(
        members,
        representative_asset_id=1,
        centroid_similarities={1: 0.99, 2: 0.98, 3: 0.95, 4: 0.90},
        sequence_limit=4,
    )

    assert report["headline"] == "Unlabeled visual motif across 3 recorded years"
    assert report["cross_year"] is True
    assert report["years"] == ["2024", "2025", "2026"]
    assert [observation["kind"] for observation in report["observations"]] == [
        "visual_cluster",
        "time",
        "camera",
        "lens",
    ]
    assert report["observations"][0]["facts"]["algorithm"] == ("deterministic_spherical_kmeans")
    assert [item["asset_id"] for item in report["sequence_seed"]["items"]] == [1, 3, 4, 2]
    assert [item["role"] for item in report["sequence_seed"]["items"]] == [
        "centroid_representative",
        "distinct_year_evidence",
        "distinct_year_evidence",
        "visual_cluster_member",
    ]
    assert report["limitations"][-1] == (
        "It does not establish place, identity, event, intention, or story."
    )


def test_motif_report_requires_complete_scored_membership() -> None:
    with pytest.raises(ValueError, match="cover every member"):
        build_grounded_motif_report(
            [{"id": 1}, {"id": 2}],
            representative_asset_id=1,
            centroid_similarities={1: 1.0},
        )
