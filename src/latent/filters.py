"""Validated photo filters shared by browsing, semantic search and smart sequences."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .storage import StateStore
from .workspace import WorkspaceStore


def photo_filters(values: dict) -> dict:
    allowed = {"date_from", "date_to", "rating_min", "starred", "flag"}
    if not isinstance(values, dict) or set(values) - allowed:
        raise ValueError("Unknown photo filter")
    result = {}
    for key in ("date_from", "date_to"):
        value = values.get(key)
        if value is not None and value != "":
            if not isinstance(value, str) or len(value) != 10:
                raise ValueError(f"{key} must use YYYY-MM-DD")
            result[key] = date.fromisoformat(value).isoformat()
    if result.get("date_from", "") > result.get("date_to", "9999-12-31"):
        raise ValueError("The start date must be on or before the end date")
    rating = values.get("rating_min", 0)
    if isinstance(rating, str) and rating in {str(n) for n in range(6)}:
        rating = int(rating)
    if type(rating) is not int or not 0 <= rating <= 5:
        raise ValueError("rating_min must be between 0 and 5")
    if rating:
        result["rating_min"] = rating
    for key, choices in (
        ("starred", {"any", "yes", "no"}),
        ("flag", {"any", "pick", "reject", "unmarked"}),
    ):
        value = values.get(key, "any")
        if not isinstance(value, str) or value not in choices:
            raise ValueError(f"Invalid {key} filter")
        if value != "any":
            result[key] = value
    return result


def matching_ids(store: StateStore, workspace_dir: Path, values: dict) -> list[int]:
    filters = photo_filters(values)
    with WorkspaceStore(workspace_dir) as workspace:
        path = str(workspace.database_path)
    store.connection.execute("ATTACH DATABASE ? AS filter_workspace", (path,))
    try:
        conditions = [store.visible_condition()]
        parameters = []
        day = "replace(substr(a.capture_at, 1, 10), ':', '-')"
        for key, operator in (("date_from", ">="), ("date_to", "<=")):
            if key in filters:
                conditions.append(f"{day} {operator} ?")
                parameters.append(filters[key])
        if "rating_min" in filters:
            conditions.append("COALESCE(n.rating, 0) >= ?")
            parameters.append(filters["rating_min"])
        if "starred" in filters:
            conditions.append(
                "COALESCE(n.rating, 0) " + ("> 0" if filters["starred"] == "yes" else "= 0")
            )
        if "flag" in filters:
            conditions.append("COALESCE(n.flag, 'unmarked') = ?")
            parameters.append(filters["flag"])
        return [
            int(row[0])
            for row in store.connection.execute(
                f"""SELECT a.id FROM assets a
            JOIN cache_entries c ON c.asset_id=a.id AND c.variant='contact'
            LEFT JOIN filter_workspace.photo_annotations n
              ON n.provider=a.provider AND n.remote_path=a.remote_path
            WHERE {" AND ".join(conditions)}
            ORDER BY a.capture_at ASC, a.name ASC, a.id ASC""",
                parameters,
            )
        ]
    finally:
        store.connection.execute("DETACH DATABASE filter_workspace")
