"""Plan conservative JPEG additions and use the existing resumable preview queue."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, deque
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from .jobs import TreeCatalog
from .models import RemoteAsset
from .storage import StateStore

JPEG_EXTENSIONS = {"jpg", "jpeg"}
RAW_EXTENSIONS = {
    "arw",
    "sr2",
    "srf",
    "cr2",
    "cr3",
    "crw",
    "nef",
    "nrw",
    "orf",
    "rw2",
    "raf",
    "dng",
    "pef",
    "srw",
    "3fr",
    "fff",
    "iiq",
    "mos",
    "mrw",
    "rwl",
    "x3f",
    "erf",
    "mef",
    "kdc",
    "dcr",
    "raw",
}
_DERIVATIVE_NAME = re.compile(r"(?:^lrt_|_dxo(?:[-_\d]|$)|^c\d+t\d+$)", re.IGNORECASE)
_DERIVATIVE_FOLDER = re.compile(
    r"(?:^lrt(?:_|$)|^exports?$|^edited$|^finished$|^sequences$|延时|成片|修图|timelapse)",
    re.IGNORECASE,
)
_DATE_FOLDER = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class JPEGEntry:
    asset: RemoteAsset
    decision: str
    reason: str


@dataclass
class JPEGPlan:
    entries: list[JPEGEntry]
    directories: list[str]
    unscanned_directories: list[str]

    def summary(self) -> dict:
        counts = Counter(entry.decision for entry in self.entries)
        return {
            "jpeg_files": len(self.entries),
            "candidates": counts["candidate"],
            "raw_pairs": counts["raw_pair"],
            "already_indexed": counts["already_indexed"],
            "identical_copies": counts["identical_copy"],
            "needs_review": counts["review"],
            "directories": len(self.directories),
            "unscanned_directories": len(self.unscanned_directories),
            "candidate_original_bytes": sum(
                entry.asset.size_bytes for entry in self.entries if entry.decision == "candidate"
            ),
            "archive_modified": False,
        }

    def as_dict(self) -> dict:
        return {
            **self.summary(),
            "scanned_paths": self.directories,
            "unscanned_paths": self.unscanned_directories,
            "entries": [asdict(entry) for entry in self.entries],
        }


def _content_hash(asset: RemoteAsset) -> tuple[str, str] | None:
    for kind, value in sorted(asset.file_hashes.items()):
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
            return kind, value.lower()
    return None


def _date_group(path: str) -> str | None:
    value = PurePosixPath(path).parent
    for parent in (value, *value.parents):
        if _DATE_FOLDER.fullmatch(parent.name):
            return str(parent)
    return None


def plan_jpeg_backfill(
    catalog: TreeCatalog,
    paths: list[str],
    state_dir: Path,
    *,
    recursive: bool = False,
    force_refresh: bool = False,
) -> JPEGPlan:
    """Only list remote metadata and read the catalog; never open original photo bodies."""
    existing: dict[tuple[str, str], str] = {}
    raw_groups: set[tuple[str, str, str]] = set()
    seen_hashes: set[tuple[str, str]] = set()
    database = state_dir.expanduser().resolve() / "index.sqlite"
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            for row in connection.execute("SELECT * FROM assets"):
                existing[row["provider"], row["remote_path"]] = row["fingerprint"]
                if row["extension"] in RAW_EXTENSIONS:
                    group = _date_group(row["remote_path"])
                    if group:
                        raw_groups.add((row["provider"], group, Path(row["name"]).stem.casefold()))
                elif row["extension"] in JPEG_EXTENSIONS:
                    for kind, value in json.loads(row["file_hashes_json"]).items():
                        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
                            seen_hashes.add((kind, value.lower()))

    plan = JPEGPlan([], [], [])
    pending = deque((path.rstrip("/"), False) for path in dict.fromkeys(paths))
    seen_directories: set[str] = set()
    while pending:
        directory, nested = pending.popleft()
        if directory in seen_directories:
            continue
        seen_directories.add(directory)
        listing = catalog.list_directory_contents(directory, force_refresh=force_refresh)
        plan.directories.append(directory)
        children = [
            child
            for child in listing.directories
            if not PurePosixPath(child).name.startswith((".", "_"))
        ]
        if recursive:
            pending.extend((child, True) for child in children)
        else:
            plan.unscanned_directories.extend(children)
        raw_stems = {
            (asset.provider, Path(asset.name).stem.casefold())
            for asset in listing.assets
            if asset.extension in RAW_EXTENSIONS
        }
        for asset in sorted(listing.assets, key=lambda a: a.name.casefold()):
            if asset.extension not in JPEG_EXTENSIONS:
                continue
            stem = Path(asset.name).stem.casefold()
            old_fingerprint = existing.get((asset.provider, asset.remote_path))
            content_hash = _content_hash(asset)
            group = _date_group(asset.remote_path)
            if old_fingerprint == asset.fingerprint:
                decision, reason = "already_indexed", "This exact source is already catalogued"
            elif old_fingerprint is not None:
                decision, reason = (
                    "review",
                    "An indexed source has changed; retain its existing state",
                )
            elif (asset.provider, stem) in raw_stems:
                decision, reason = "raw_pair", "Same folder and file stem as a RAW original"
            elif group and (asset.provider, group, stem) in raw_groups:
                decision, reason = "review", "Possible RAW pair elsewhere in this date folder"
            elif _DERIVATIVE_NAME.search(stem) or any(
                _DERIVATIVE_FOLDER.search(part) for part in PurePosixPath(directory).parts
            ):
                decision, reason = "review", "Possible edited export, video thumbnail or time-lapse"
            elif nested:
                decision, reason = (
                    "review",
                    "Nested folder may contain exports or duplicate originals",
                )
            elif content_hash and content_hash in seen_hashes:
                decision, reason = "identical_copy", "Identical content hash to another JPEG"
            else:
                decision, reason = "candidate", "JPEG without a same-folder RAW pair"
                if content_hash:
                    seen_hashes.add(content_hash)
            plan.entries.append(JPEGEntry(asset, decision, reason))
    return plan


def enqueue_jpeg_plan(plan: JPEGPlan, store: StateStore) -> dict[str, int]:
    enqueued = unchanged = 0
    for entry in plan.entries:
        if entry.decision != "candidate":
            continue
        asset = entry.asset
        row = store.connection.execute(
            "SELECT id, fingerprint FROM assets WHERE provider=? AND remote_path=?",
            (asset.provider, asset.remote_path),
        ).fetchone()
        if row is not None:
            if row["fingerprint"] != asset.fingerprint:
                raise ValueError("The catalog changed after planning; generate a fresh JPEG plan")
            asset_id = int(row["id"])
        else:
            asset_id = store.upsert_asset(asset)
        if store.enqueue_preview_job(asset_id, asset.fingerprint):
            enqueued += 1
        else:
            unchanged += 1
    return {"enqueued": enqueued, "unchanged": unchanged}
