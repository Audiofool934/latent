"""Optional, reversible time-lapse groups with fingerprinted archive references."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

from .errors import ConfigurationError
from .storage import StateStore
from .timelapse import audit_catalog
from .workspace import utc_now

_INITIALIZATION_LOCK = threading.Lock()


class TimelapseGroups:
    """User decisions live beside the workspace; the catalog and vectors stay intact."""

    def __init__(self, workspace_dir: Path):
        workspace_dir = workspace_dir.expanduser().resolve()
        workspace_dir.mkdir(parents=True, exist_ok=True)
        self.path = workspace_dir / "timelapse.sqlite"
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        with _INITIALIZATION_LOCK:
            if self.connection.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                self.connection.close()
                raise ConfigurationError("The time-lapse workspace version is not supported")
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS groups (
                    id TEXT PRIMARY KEY,
                    confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0,1)),
                    revision INTEGER NOT NULL DEFAULT 1,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS members (
                    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    remote_path TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    PRIMARY KEY(group_id, position),
                    UNIQUE(group_id, provider, remote_path)
                );
                CREATE INDEX IF NOT EXISTS member_identity
                    ON members(provider, remote_path, fingerprint);
                PRAGMA user_version=1;
            """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.connection.close()

    def audit(self, state_dir: Path) -> dict:
        report = audit_catalog(state_dir)
        self.stage(state_dir, report["candidates"])
        return report["summary"]

    def stage(self, state_dir: Path, candidates: list[dict]) -> None:
        """Keep reviewed decisions on repeat scans and never merge confirmed groups."""
        with StateStore(state_dir) as catalog:
            records = {
                int(row["id"]): dict(row)
                for row in catalog.connection.execute(
                    "SELECT id,provider,remote_path,fingerprint FROM assets"
                )
            }
        with self.connection:
            for candidate in candidates:
                ids = candidate["asset_ids"]
                if not ids or len(set(ids)) != len(ids) or any(i not in records for i in ids):
                    raise ValueError("Candidate photos changed; find candidates again")
                refs = [(records[i]["provider"], records[i]["remote_path"],
                         records[i]["fingerprint"]) for i in ids]
                key = hashlib.sha256(json.dumps(refs, ensure_ascii=False).encode()).hexdigest()
                if self.connection.execute("SELECT 1 FROM groups WHERE id=?", (key,)).fetchone():
                    continue
                # A new audit may extend or split a run. Its overlapping proposal must
                # not silently change a group the user already accepted.
                if any(self.connection.execute("""
                    SELECT 1 FROM members m JOIN groups g ON g.id=m.group_id
                    WHERE m.provider=? AND m.remote_path=? AND g.confirmed=1
                """, ref[:2]).fetchone() for ref in refs):
                    continue
                evidence = {k: v for k, v in candidate.items() if k != "asset_ids"}
                now = utc_now()
                self.connection.execute(
                    "INSERT INTO groups(id,evidence_json,created_at,updated_at) VALUES(?,?,?,?)",
                    (key, json.dumps(evidence, ensure_ascii=False), now, now),
                )
                self.connection.executemany(
                    "INSERT INTO members VALUES(?,?,?,?,?)",
                    [(key, position, *ref) for position, ref in enumerate(refs)],
                )

    def _members(self, catalog: StateStore, *, confirmed_only: bool = False) -> list[dict]:
        # Catalog filtering uses a TEMP table inside its own transaction. Read the
        # two durable databases on a separate connection so DETACH cannot hold up
        # the caller's filter transaction or commit unrelated catalog work.
        connection = sqlite3.connect(catalog.database_path.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(
                "ATTACH DATABASE ? AS timelapse_groups", (self.path.as_uri() + "?mode=ro",),
            )
            return [dict(row) for row in connection.execute(f"""
                SELECT a.id AS asset_id, m.group_id, m.position
                FROM timelapse_groups.members m
                JOIN timelapse_groups.groups g ON g.id=m.group_id
                JOIN assets a ON a.provider=m.provider AND a.remote_path=m.remote_path
                    AND a.fingerprint=m.fingerprint
                JOIN cache_entries c ON c.asset_id=a.id AND c.variant='contact'
                WHERE {catalog.visible_condition()} {'AND g.confirmed=1' if confirmed_only else ''}
                ORDER BY m.group_id, m.position
            """)]
        finally:
            connection.close()

    def list(self, state_dir: Path) -> list[dict]:
        with StateStore(state_dir) as catalog:
            members = self._members(catalog)
        by_group: dict[str, list[int]] = {}
        for member in members:
            by_group.setdefault(member["group_id"], []).append(member["asset_id"])
        result = []
        for row in self.connection.execute("""
            SELECT g.*, COUNT(m.position) AS photo_count FROM groups g
            JOIN members m ON m.group_id=g.id GROUP BY g.id
            ORDER BY json_extract(g.evidence_json,'$.start_capture_at') DESC, g.id
        """):
            ids = by_group.get(row["id"], [])
            result.append({
                **json.loads(row["evidence_json"]),
                "id": row["id"], "confirmed": bool(row["confirmed"]),
                "revision": row["revision"], "photo_count": row["photo_count"],
                "available_count": len(ids), "cover_asset_id": ids[0] if ids else None,
            })
        return result

    def set_confirmed(self, state_dir: Path, group_id: str, *, confirmed: bool,
                      expected_revision: int) -> None:
        if type(confirmed) is not bool or type(expected_revision) is not int:
            raise ValueError("A grouping decision and its current revision are required")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.connection.execute(
                "SELECT * FROM groups WHERE id=?", (group_id,),
            ).fetchone()
            if current is None:
                raise KeyError("Time-lapse group was not found")
            if current["revision"] != expected_revision:
                raise ValueError("This group changed. Refresh the organizer before editing it")
            if confirmed:
                with StateStore(state_dir) as catalog:
                    available = sum(m["group_id"] == group_id for m in self._members(catalog))
                expected = self.connection.execute(
                    "SELECT COUNT(*) FROM members WHERE group_id=?", (group_id,),
                ).fetchone()[0]
                if available != expected:
                    raise ValueError("Photos changed or are unavailable. Find candidates again")
                overlapping = self.connection.execute("""
                    SELECT 1 FROM members a JOIN members b
                        ON a.provider=b.provider AND a.remote_path=b.remote_path
                    JOIN groups g ON g.id=b.group_id AND g.confirmed=1
                    WHERE a.group_id=? AND b.group_id!=? LIMIT 1
                """, (group_id, group_id)).fetchone()
                if overlapping:
                    raise ValueError("These photos already belong to another confirmed group")
            self.connection.execute(
                "UPDATE groups SET confirmed=?,revision=revision+1,updated_at=? WHERE id=?",
                (int(confirmed), utc_now(), group_id),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def presentation(self, catalog: StateStore, ordered_ids: list[int], *,
                     group_id: str | None = None) -> tuple[list[int], dict[int, dict]]:
        """Collapse only after filtering, before pagination; expanded groups keep every frame."""
        if group_id is not None:
            found = self.connection.execute(
                "SELECT 1 FROM groups WHERE id=?", (group_id,),
            ).fetchone()
            if not found:
                raise KeyError("Time-lapse group was not found")
            members = {m["asset_id"] for m in self._members(catalog) if m["group_id"] == group_id}
            return [i for i in ordered_ids if i in members], {}
        members = self._members(catalog, confirmed_only=True)
        membership = {m["asset_id"]: m["group_id"] for m in members}
        counts: dict[str, int] = {}
        for identifier in ordered_ids:
            if key := membership.get(identifier):
                counts[key] = counts.get(key, 0) + 1
        seen: set[str] = set()
        visible, badges = [], {}
        for identifier in ordered_ids:
            key = membership.get(identifier)
            if key and key in seen:
                continue
            visible.append(identifier)
            if key:
                seen.add(key)
                badges[identifier] = {"id": key, "photo_count": counts[key]}
        return visible, badges
