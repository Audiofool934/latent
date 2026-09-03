"""Deterministic curator observations grounded in local model and EXIF evidence."""

from __future__ import annotations

import math
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


def build_grounded_motif_report(
    members: Sequence[Mapping[str, Any]],
    *,
    representative_asset_id: int,
    centroid_similarities: Mapping[int, float],
    sequence_limit: int = 8,
) -> dict[str, Any]:
    """Describe an unlabeled visual cluster using only vectors and recorded metadata."""
    if sequence_limit < 1 or sequence_limit > 100:
        raise ValueError("sequence limit must be between 1 and 100")
    source = list(members)
    if not source:
        raise ValueError("a motif report requires at least one member")
    by_id = {_asset_id(member): member for member in source}
    if len(by_id) != len(source):
        raise ValueError("motif member asset IDs must be unique")
    if representative_asset_id not in by_id:
        raise ValueError("motif representative must be one of its members")
    if set(centroid_similarities) != set(by_id):
        raise ValueError("motif centroid similarities must cover every member")
    raw_scores = {asset_id: float(score) for asset_id, score in centroid_similarities.items()}
    if not all(
        math.isfinite(score) and -1.0001 <= score <= 1.0001 for score in raw_scores.values()
    ):
        raise ValueError("motif centroid similarities must be between -1 and 1")
    scores = {asset_id: max(-1.0, min(1.0, score)) for asset_id, score in raw_scores.items()}

    ranked_ids = sorted(by_id, key=lambda asset_id: (-scores[asset_id], asset_id))
    capture_dates = sorted(
        {
            capture_date
            for member in source
            if (capture_date := _capture_date(member.get("capture_at"))) is not None
        }
    )
    years = sorted({capture_date[:4] for capture_date in capture_dates})
    observations = [
        {
            "kind": "visual_cluster",
            "statement": (
                f"{len(source)} indexed frames share a spherical embedding cluster; "
                f"mean cosine to its centroid is {sum(scores.values()) / len(scores):.3f}."
            ),
            "asset_ids": ranked_ids,
            "facts": {
                "algorithm": "deterministic_spherical_kmeans",
                "metric": "cosine_similarity_to_centroid",
                "member_count": len(source),
                "representative_asset_id": representative_asset_id,
                "mean_centroid_similarity": sum(scores.values()) / len(scores),
            },
        }
    ]
    if capture_dates:
        observations.append(
            {
                "kind": "time",
                "statement": (
                    f"Recorded capture dates span {len(capture_dates)} dates "
                    f"and {len(years)} years "
                    f"from {capture_dates[0]} to {capture_dates[-1]}."
                ),
                "asset_ids": [
                    asset_id
                    for asset_id in ranked_ids
                    if _capture_date(by_id[asset_id].get("capture_at")) is not None
                ],
                "facts": {"capture_dates": capture_dates, "years": years},
            }
        )

    for kind, field, noun in (
        ("camera", "camera_model", "camera"),
        ("lens", "lens_model", "lens"),
    ):
        values = [str(member[field]).strip() for member in source if member.get(field)]
        if not values:
            continue
        value, count = sorted(Counter(values).items(), key=lambda item: (-item[1], item[0]))[0]
        if count < 2:
            continue
        matching_ids = [
            asset_id
            for asset_id in ranked_ids
            if str(by_id[asset_id].get(field, "")).strip() == value
        ]
        observations.append(
            {
                "kind": kind,
                "statement": f'{count} of {len(source)} frames report {noun} "{value}".',
                "asset_ids": matching_ids,
                "facts": {"value": value, "matching_frames": count},
            }
        )

    sequence_items = _motif_sequence_items(
        by_id,
        ranked_ids,
        representative_asset_id=representative_asset_id,
        scores=scores,
        limit=sequence_limit,
    )
    if len(years) > 1:
        headline = f"Unlabeled visual motif across {len(years)} recorded years"
    elif years:
        headline = f"Unlabeled visual motif within {years[0]}"
    else:
        headline = "Unlabeled visual motif with no recorded capture year"
    return {
        "headline": headline,
        "cross_year": len(years) > 1,
        "member_count": len(source),
        "capture_date_count": len(capture_dates),
        "years": years,
        "observations": observations,
        "sequence_seed": {
            "ordering": "representative_then_distinct_years_then_centroid_similarity",
            "description": (
                "Centroid representative first, then strongest evidence from distinct recorded "
                "years, then remaining members by centroid cosine."
            ),
            "items": sequence_items,
        },
        "limitations": [
            "This is an unlabeled grouping from local image embeddings and recorded EXIF only.",
            "A cluster is a visual hypothesis, not a named subject or an artistic conclusion.",
            "It does not establish place, identity, event, intention, or story.",
        ],
    }


def _motif_sequence_items(
    members: Mapping[int, Mapping[str, Any]],
    ranked_ids: Sequence[int],
    *,
    representative_asset_id: int,
    scores: Mapping[int, float],
    limit: int,
) -> list[dict[str, Any]]:
    selected = [representative_asset_id]
    roles = {representative_asset_id: "centroid_representative"}
    representative_date = _capture_date(members[representative_asset_id].get("capture_at"))
    used_years = {representative_date[:4]} if representative_date else set()
    for asset_id in ranked_ids:
        capture_date = _capture_date(members[asset_id].get("capture_at"))
        if asset_id in selected or capture_date is None or capture_date[:4] in used_years:
            continue
        selected.append(asset_id)
        roles[asset_id] = "distinct_year_evidence"
        used_years.add(capture_date[:4])
        if len(selected) >= limit:
            break
    for asset_id in ranked_ids:
        if len(selected) >= limit:
            break
        if asset_id not in selected:
            selected.append(asset_id)
            roles[asset_id] = "visual_cluster_member"
    return [
        {
            "position": position,
            "asset_id": asset_id,
            "role": roles[asset_id],
            "centroid_similarity": scores[asset_id],
        }
        for position, asset_id in enumerate(selected, start=1)
    ]


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
