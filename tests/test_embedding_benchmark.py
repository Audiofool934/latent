from __future__ import annotations

import json
from pathlib import Path

import pytest

from latent.embedding_benchmark import (
    SampleAsset,
    balanced_date_sample,
    directory_size,
    evenly_spaced_indices,
    write_json_atomic,
)


def _asset(asset_id: int, capture_date: str) -> SampleAsset:
    return SampleAsset(
        asset_id=asset_id,
        name=f"DSC{asset_id:05d}.ARW",
        capture_at=f"{capture_date} 12:00:00",
        fingerprint=f"fingerprint-{asset_id}",
        contact_path=f"contact/{asset_id}.jpg",
    )


@pytest.mark.parametrize(
    ("total", "count", "expected"),
    [
        (0, 1, []),
        (10, 0, []),
        (10, 1, [5]),
        (10, 3, [0, 4, 9]),
        (3, 3, [0, 1, 2]),
        (3, 4, [0, 1, 2]),
    ],
)
def test_evenly_spaced_indices(total: int, count: int, expected: list[int]) -> None:
    assert evenly_spaced_indices(total, count) == expected


def test_balanced_date_sample_spans_the_archive_deterministically() -> None:
    records = [
        *(_asset(index, "2024-01-01") for index in range(1, 6)),
        *(_asset(index, "2025-06-15") for index in range(6, 11)),
        *(_asset(index, "2026-08-29") for index in range(11, 16)),
    ]

    first = balanced_date_sample(records, 6)
    second = balanced_date_sample(records, 6)

    assert first == second
    assert len(first) == 6
    assert {asset.capture_date for asset in first} == {
        "2024-01-01",
        "2025-06-15",
        "2026-08-29",
    }
    assert len({asset.asset_id for asset in first}) == 6


def test_balanced_date_sample_fills_short_date_groups() -> None:
    records = [
        _asset(1, "2024-01-01"),
        *(_asset(index, "2025-06-15") for index in range(2, 10)),
        _asset(10, "2026-08-29"),
    ]

    sample = balanced_date_sample(records, 8)

    assert len(sample) == 8
    assert len({asset.asset_id for asset in sample}) == 8
    assert {asset.capture_date for asset in sample} == {
        "2024-01-01",
        "2025-06-15",
        "2026-08-29",
    }


def test_balanced_date_sample_rejects_non_positive_size() -> None:
    with pytest.raises(ValueError, match="positive"):
        balanced_date_sample([_asset(1, "2026-08-29")], 0)


def test_write_json_atomic_replaces_existing_payload(tmp_path: Path) -> None:
    output = tmp_path / "results" / "benchmark.json"
    output.parent.mkdir()
    output.write_text('{"old": true}\n', encoding="utf-8")

    write_json_atomic(output, {"model": "example", "queries": ["月亮"]})

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "model": "example",
        "queries": ["月亮"],
    }
    assert not output.with_suffix(".json.tmp").exists()


def test_directory_size_does_not_double_count_snapshot_symlinks(tmp_path: Path) -> None:
    blob = tmp_path / "blobs" / "weights.bin"
    blob.parent.mkdir()
    blob.write_bytes(b"model-weights")
    snapshot = tmp_path / "snapshots" / "main" / "weights.bin"
    snapshot.parent.mkdir(parents=True)
    snapshot.symlink_to(blob)

    assert directory_size(tmp_path) == len(b"model-weights")
