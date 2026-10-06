from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from latent.workspace import WorkspaceAsset, WorkspaceStore


def test_folder_moves_and_removal_preserve_sequences_and_photo_order(tmp_path: Path) -> None:
    with WorkspaceStore(tmp_path) as store:
        root = store.create_folder("Travel")
        child = store.create_folder("2026", parent_id=root["id"])
        leaf = store.create_folder("Mountains", parent_id=child["id"])
        sequence = store.create_sequence("First edit", folder_id=child["id"])
        assets = [
            WorkspaceAsset("test", f"/photo-{i}.ARW", f"hash-{i}", f"photo-{i}.ARW")
            for i in [3, 1, 2]
        ]
        store.add_items(sequence["id"], assets)
        before = store.get_sequence(sequence["id"])
        store.update_sequence(sequence["id"], folder_id=leaf["id"])
        store.update_sequence(sequence["id"], name="Final edit")
        assert store.get_sequence(sequence["id"])["folder_id"] == leaf["id"]
        store.update_sequence(sequence["id"], folder_id=child["id"])
        store.remove_folder(child["id"])
        assert store.get_folder(leaf["id"])["parent_id"] == root["id"]
        after = store.get_sequence(sequence["id"])
        assert after["folder_id"] == root["id"]
        assert after["items"] == before["items"]
        store.update_folder(leaf["id"], name="Landscapes", parent_id=None)
    with WorkspaceStore(tmp_path) as reopened:
        assert reopened.get_folder(leaf["id"])["parent_id"] is None
        assert reopened.get_folder(leaf["id"])["name"] == "Landscapes"
        assert reopened.get_sequence(sequence["id"])["items"] == before["items"]


def test_invalid_or_concurrent_folder_moves_cannot_create_cycles(tmp_path: Path) -> None:
    with WorkspaceStore(tmp_path) as store:
        a = store.create_folder("A")
        b = store.create_folder("B", parent_id=a["id"])
        c = store.create_folder("C", parent_id=b["id"])
        before = store.list_folders()
        for parent in [a["id"], c["id"], ["not a folder id"]]:
            with pytest.raises(ValueError):
                store.update_folder(a["id"], name="Must not change", parent_id=parent)
        with pytest.raises(KeyError):
            store.update_folder(a["id"], parent_id="missing")
        assert store.list_folders() == before
        store.update_folder(b["id"], parent_id=None)
    barrier = threading.Barrier(2)

    def move(pair):
        with WorkspaceStore(tmp_path) as store:
            barrier.wait(timeout=3)
            try:
                store.update_folder(pair[0], parent_id=pair[1])
                return True
            except ValueError:
                return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(move, [(a["id"], b["id"]), (b["id"], a["id"])]))
    assert sorted(outcomes) == [False, True]


def test_deep_folder_export_import_is_atomic_and_non_overwriting(tmp_path: Path) -> None:
    with WorkspaceStore(tmp_path / "source") as source:
        payload = source.export_payload()
    payload["folders"] = [
        {"id": f"folder-{i}", "name": str(i), "parent_id": f"folder-{i-1}" if i else None,
         "created_at": "before", "updated_at": "before"}
        for i in reversed(range(1100))
    ]
    with WorkspaceStore(tmp_path / "target") as target:
        assert target.merge_import(payload).imported == 1100
        assert target.merge_import(payload).unchanged == 1100
        target.update_folder("folder-1099", name="Local name")
        assert target.merge_import(payload).conflicts == 1
        assert target.get_folder("folder-1099")["name"] == "Local name"
        exported = target.export_payload()
    with WorkspaceStore(tmp_path / "restored") as restored:
        assert restored.merge_import(exported).imported == 1100
        assert restored.list_folders() == exported["folders"]
    invalid = deepcopy(payload)
    invalid["folders"][-1]["parent_id"] = "folder-1099"
    with WorkspaceStore(tmp_path / "invalid") as store:
        with pytest.raises(ValueError, match="cycle"):
            store.merge_import(invalid)
        assert store.list_folders() == []


def test_v3_migration_keeps_existing_sequences_at_root(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "workspace.sqlite") as database:
        database.executescript("""
            CREATE TABLE workspace_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO workspace_meta VALUES ('schema_version', '3');
            CREATE TABLE sequences (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                note TEXT NOT NULL, origin TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            INSERT INTO sequences
            VALUES ('original', 'Original', 'Keep note', 'manual', 'old', 'old');
        """)
    with WorkspaceStore(tmp_path) as store:
        sequence = store.get_sequence("original")
        assert sequence["folder_id"] is None
        assert sequence["name"] == "Original" and sequence["note"] == "Keep note"
        assert sequence["created_at"] == sequence["updated_at"] == "old"
        assert store.status()["database_integrity"] == "ok"
        legacy = store.export_payload()
        legacy["schema_version"] = 3
        del legacy["folders"]
        del legacy["sequences"][0]["folder_id"]
    with WorkspaceStore(tmp_path / "legacy-import") as restored:
        assert restored.merge_import(legacy).imported == 1
        assert restored.get_sequence("original")["folder_id"] is None
