"""Deterministic curator observations grounded in local model and EXIF evidence."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

_CAPTURE_DATE_PATTERN = re.compile(r"^(\d{4})[:-](\d{2})[:-](\d{2})")


def build_grounded_curator_report(
    source: Mapping[str, Any],
    neighbors: Sequence[Mapping[str, Any]],
    *,
    sequence_limit: int = 6,
) -> dict[str, Any]:
    """Build factual observations without inferring identity, place, or story."""
    if sequence_limit < 1 or sequence_limit > 100:
        raise ValueError("sequence limit must be between 1 and 100")
    source_id = _asset_id(source)
    ranked_neighbors = list(neighbors)
    for neighbor in ranked_neighbors:
        _asset_id(neighbor)
        similarity = neighbor.get("similarity")
        if not isinstance(similarity, int | float):
            raise ValueError("every curator neighbor must include numeric similarity")

    assets = [source, *ranked_neighbors]
    capture_dates = sorted(
        {
            capture_date
            for asset in assets
            if (capture_date := _capture_date(asset.get("capture_at"))) is not None
        }
    )
    observations: list[dict[str, Any]] = []

    if ranked_neighbors:
        scores = [float(neighbor["similarity"]) for neighbor in ranked_neighbors]
        observations.append(
            {
                "kind": "visual",
                "statement": (
                    f"The closest indexed neighbor has cosine {scores[0]:.3f}; "
                    f"{len(scores)} ranked visual neighbors are included."
                ),
                "asset_ids": [_asset_id(neighbor) for neighbor in ranked_neighbors],
                "facts": {
                    "metric": "cosine_similarity",
                    "nearest": scores[0],
                    "lowest_included": scores[-1],
                },
            }
        )

    if capture_dates:
        if len(capture_dates) == 1:
            statement = f"All {len(assets)} indexed frames were captured on {capture_dates[0]}."
        else:
            statement = (
                f"These {len(assets)} indexed frames span {len(capture_dates)} capture dates "
                f"from {capture_dates[0]} to {capture_dates[-1]}."
            )
        observations.append(
            {
                "kind": "time",
                "statement": statement,
                "asset_ids": [_asset_id(asset) for asset in assets],
                "facts": {"capture_dates": capture_dates},
            }
        )

    for kind, field, noun in (
        ("camera", "camera_model", "camera"),
        ("lens", "lens_model", "lens"),
    ):
        values = [str(asset[field]).strip() for asset in assets if asset.get(field)]
        if not values:
            continue
        value, count = sorted(Counter(values).items(), key=lambda item: (-item[1], item[0]))[0]
        if count < 2:
            continue
        matching_ids = [
            _asset_id(asset) for asset in assets if str(asset.get(field, "")).strip() == value
        ]
        observations.append(
            {
                "kind": kind,
                "statement": f'{count} of {len(assets)} frames report {noun} "{value}".',
                "asset_ids": matching_ids,
                "facts": {"value": value, "matching_frames": count},
            }
        )

    sequence_assets = assets[:sequence_limit]
    sequence_items = []
    for position, asset in enumerate(sequence_assets, start=1):
        item = {
            "position": position,
            "asset_id": _asset_id(asset),
            "role": "source" if position == 1 else "visual_neighbor",
        }
        if position > 1:
            item["similarity"] = float(asset["similarity"])
        sequence_items.append(item)

    if capture_dates:
        date_phrase = (
            f"{len(capture_dates)} capture date"
            if len(capture_dates) == 1
            else f"{len(capture_dates)} capture dates"
        )
        headline = f"Visual neighborhood across {date_phrase}"
    else:
        headline = "Visual neighborhood with no recorded capture date"

    return {
        "source_asset_id": source_id,
        "headline": headline,
        "observations": observations,
        "sequence_seed": {
            "ordering": "source_then_similarity",
            "description": "Source first, then visual neighbors in descending cosine order.",
            "items": sequence_items,
        },
        "limitations": [
            "This report describes local embedding distance and recorded EXIF only.",
            "It does not establish place, identity, event, intention, or story.",
        ],
    }


def _asset_id(asset: Mapping[str, Any]) -> int:
    asset_id = asset.get("id")
    if not isinstance(asset_id, int):
        raise ValueError("every curator asset must include an integer id")
    return asset_id


def _capture_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _CAPTURE_DATE_PATTERN.match(value)
    if match is None:
        return None
    return "-".join(match.groups())
