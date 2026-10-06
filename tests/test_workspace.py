from __future__ import annotations

import json
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from copy import deepcopy
from pathlib import Path

import pytest

from latent.cli import main
from latent.workspace import (
    WorkspaceAsset,
    WorkspaceStore,
    load_workspace_export,
    write_workspace_export,
)


def _asset(asset_id: int) -> WorkspaceAsset:
    return WorkspaceAsset(
        provider="test",
        remote_path=f"/archive/2026/2026-08-29/DSC{asset_id:05d}.ARW",
        fingerprint=f"fingerprint-{asset_id}",
        name=f"DSC{asset_id:05d}.ARW",
        capture_at=f"2026:08:29 12:00:{asset_id:02d}",
        camera_model="ILCE-7RM5",
        lens_model="E 50-400mm F4.5-6.3 A067",
    )


def test_workspace_sequence_crud_is_persistent_and_ordered(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspace"
    with WorkspaceStore(workspace_dir) as store:
        sequence = store.create_sequence("Morning ride", note="First pass")
        sequence_id = sequence["id"]
        added = store.add_items(sequence_id, [_asset(1), _asset(2), _asset(3)])
        duplicate = store.add_items(sequence_id, [_asset(2)])

        assert added.added == 3
        assert added.skipped == 0
        assert duplicate.added == 0
        assert duplicate.skipped == 1

        current = store.update_sequence(sequence_id, name="Field motion", note="Keep it spare")
        assert current["name"] == "Field motion"
        assert current["note"] == "Keep it spare"

        reversed_ids = [item["id"] for item in reversed(current["items"])]
        reordered = store.reorder_items(sequence_id, reversed_ids)
        assert [item["name"] for item in reordered["items"]] == [
            "DSC00003.ARW",
            "DSC00002.ARW",
            "DSC00001.ARW",
        ]
        assert [item["position"] for item in reordered["items"]] == [0, 1, 2]

        assert store.remove_item(sequence_id, reordered["items"][1]["id"])
        assert not store.remove_item(sequence_id, "missing-item")
        remaining = store.get_sequence(sequence_id)
        assert [item["name"] for item in remaining["items"]] == [
            "DSC00003.ARW",
            "DSC00001.ARW",
        ]
        assert [item["position"] for item in remaining["items"]] == [0, 1]
        assert store.status() == {
            "schema_version": 5,
            "database_path": str(workspace_dir / "workspace.sqlite"),
            "database_integrity": "ok",
            "sequences": 1,
            "items": 2,
            "annotations": 0,
            "archive_modified": False,
        }

    with WorkspaceStore(workspace_dir) as reopened:
        persisted = reopened.get_sequence(sequence_id)
        assert persisted["name"] == "Field motion"
        assert [item["name"] for item in persisted["items"]] == [
            "DSC00003.ARW",
            "DSC00001.ARW",
        ]
        assert reopened.delete_sequence(sequence_id)
        assert reopened.status()["items"] == 0


@pytest.mark.parametrize("field", ["name", "note"])
def test_partial_sequence_update_preserves_an_interleaved_change(
    tmp_path: Path, field: str
) -> None:
    with WorkspaceStore(tmp_path) as first, WorkspaceStore(tmp_path) as second:
        sequence = first.create_sequence("Original name", note="Original note")
        get_original = first.get_sequence
        other_field = "note" if field == "name" else "name"
        armed = True

        def interleave(identifier):
            nonlocal armed
            before = get_original(identifier)
            if armed:
                armed = False
                second.update_sequence(identifier, **{other_field: "Changed elsewhere"})
            return before

        first.get_sequence = interleave
        result = first.update_sequence(sequence["id"], **{field: "This edit"})
        assert result[field] == "This edit"
        assert result[other_field] == "Changed elsewhere"


def test_concurrent_first_open_cannot_observe_a_partial_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema_created = threading.Event()
    finish_initialization = threading.Event()
    second_started = threading.Event()
    connect = sqlite3.connect
    first = True

    class PausedConnection(sqlite3.Connection):
        def executescript(self, script):
            result = super().executescript(script)
            schema_created.set()
            assert finish_initialization.wait(timeout=3)
            return result

    def connect_once(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            kwargs["factory"] = PausedConnection
        return connect(*args, **kwargs)

    monkeypatch.setattr("latent.workspace.sqlite3.connect", connect_once)

    def open_workspace(*, second=False):
        if second:
            second_started.set()
        with WorkspaceStore(tmp_path) as store:
            return store.status()["database_integrity"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        opening = executor.submit(open_workspace)
        try:
            assert schema_created.wait(timeout=3)
            overlapping = executor.submit(open_workspace, second=True)
            assert second_started.wait(timeout=3)
            # Let the overlapping caller encounter the deliberately incomplete schema.
            # A serialized initializer waits here instead of rejecting a fresh database.
            with suppress(TimeoutError):
                overlapping.result(timeout=0.1)
        finally:
            finish_initialization.set()
        assert opening.result(timeout=3) == "ok"
        assert overlapping.result(timeout=3) == "ok"


def test_workspace_export_is_atomic_and_merge_import_is_non_overwriting(
    tmp_path: Path,
) -> None:
    with WorkspaceStore(tmp_path / "source") as source:
        sequence = source.create_sequence("Cross-year blue", origin="curator")
        source.add_items(sequence["id"], [_asset(1), _asset(2)])
        payload = source.export_payload()

    export_path = write_workspace_export(tmp_path / "exports" / "workspace.json", payload)
    assert stat.S_IMODE(export_path.stat().st_mode) == 0o600
    loaded = load_workspace_export(export_path)
    assert loaded == json.loads(export_path.read_text(encoding="utf-8"))

    with WorkspaceStore(tmp_path / "target") as target:
        first = target.merge_import(loaded)
        second = target.merge_import(loaded)
        conflict_payload = deepcopy(loaded)
        conflict_payload["sequences"][0]["name"] = "Conflicting edit"
        conflict = target.merge_import(conflict_payload)

        assert first.as_dict() == {"imported": 1, "unchanged": 0, "conflicts": 0}
        assert second.as_dict() == {"imported": 0, "unchanged": 1, "conflicts": 0}
        assert conflict.as_dict() == {"imported": 0, "unchanged": 0, "conflicts": 1}
        imported = target.get_sequence(loaded["sequences"][0]["id"])
        assert imported["name"] == "Cross-year blue"
        assert imported["item_count"] == 2


def test_workspace_import_rejects_invalid_or_non_contiguous_payloads(tmp_path: Path) -> None:
    with WorkspaceStore(tmp_path / "source") as source:
        sequence = source.create_sequence("Test")
        source.add_items(sequence["id"], [_asset(1)])
        payload = source.export_payload()

    with WorkspaceStore(tmp_path / "target") as target:
        invalid_format = deepcopy(payload)
        invalid_format["format"] = "unknown"
        with pytest.raises(ValueError, match="format"):
            target.merge_import(invalid_format)

        invalid_position = deepcopy(payload)
        invalid_position["sequences"][0]["items"][0]["position"] = 4
        with pytest.raises(ValueError, match="contiguous"):
            target.merge_import(invalid_position)
        assert target.status()["sequences"] == 0


def test_workspace_reorder_requires_exact_item_set(tmp_path: Path) -> None:
    with WorkspaceStore(tmp_path) as store:
        sequence = store.create_sequence("Test")
        store.add_items(sequence["id"], [_asset(1), _asset(2)])
        current = store.get_sequence(sequence["id"])

        with pytest.raises(ValueError, match="every sequence item"):
            store.reorder_items(sequence["id"], [current["items"][0]["id"]])
        with pytest.raises(ValueError, match="unique"):
            store.reorder_items(
                sequence["id"],
                [current["items"][0]["id"], current["items"][0]["id"]],
            )


def test_workspace_cli_status_export_and_merge_import(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    export_path = tmp_path / "workspace.json"
    with WorkspaceStore(source_dir) as source:
        sequence = source.create_sequence("CLI sequence")
        source.add_items(sequence["id"], [_asset(1)])

    assert main(["workspace-status", "--workspace-dir", str(source_dir), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["sequences"] == 1
    assert status["items"] == 1

    assert (
        main(
            [
                "workspace-export",
                "--workspace-dir",
                str(source_dir),
                "--output",
                str(export_path),
                "--json",
            ]
        )
        == 0
    )
    exported = json.loads(capsys.readouterr().out)
    assert exported["output"] == str(export_path)
    assert exported["archive_modified"] is False

    assert (
        main(
            [
                "workspace-import",
                "--workspace-dir",
                str(target_dir),
                "--input",
                str(export_path),
                "--json",
            ]
        )
        == 0
    )
    imported = json.loads(capsys.readouterr().out)
    assert imported["import"]["imported"] == 1
    assert imported["conflicts_overwritten"] == 0
    assert imported["archive_modified"] is False


def test_workspace_cli_reports_invalid_import_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]", encoding="utf-8")

    result = main(
        [
            "workspace-import",
            "--workspace-dir",
            str(tmp_path / "workspace"),
            "--input",
            str(invalid),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "workspace import failed" in captured.err
    assert "Traceback" not in captured.err


def test_annotations_survive_migration_and_export(tmp_path: Path) -> None:
    with WorkspaceStore(tmp_path / "source") as store:
        store.create_sequence("Keep this")
        store.connection.execute("UPDATE workspace_meta SET value='1' WHERE key='schema_version'")
        store.connection.commit()
    with WorkspaceStore(tmp_path / "source") as store:
        store.annotate([_asset(1), _asset(2)], rating=4)
        store.annotate([_asset(1)], caption="Revisit the light")
        assert store.annotations([_asset(1).identity])[0]["rating"] == 4
        payload = store.export_payload()
    with WorkspaceStore(tmp_path / "target") as store:
        assert store.merge_import(payload).imported == 3
        assert store.merge_import(payload).unchanged == 3
        assert store.annotations([_asset(1).identity])[0]["caption"] == "Revisit the light"
        assert store.list_sequences()[0]["name"] == "Keep this"
        with pytest.raises(ValueError, match="rating"):
            store.annotate([_asset(1)], rating=True)
        legacy = {**payload, "schema_version": 1}
        del legacy["annotations"]
        assert store.merge_import(legacy).unchanged == 1


def test_flags_migrate_from_v2_and_round_trip_independently_of_stars(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with sqlite3.connect(source / "workspace.sqlite") as database:
        database.executescript("""
            CREATE TABLE workspace_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO workspace_meta VALUES ('schema_version', '2');
            CREATE TABLE photo_annotations (
                provider TEXT NOT NULL, remote_path TEXT NOT NULL, fingerprint TEXT NOT NULL,
                name TEXT NOT NULL, rating INTEGER NOT NULL DEFAULT 0,
                caption TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                PRIMARY KEY(provider, remote_path)
            );
        """)
        asset = _asset(1)
        database.execute("INSERT INTO photo_annotations VALUES (?, ?, ?, ?, 4, 'Keep text', 'now')",
                         (asset.provider, asset.remote_path, asset.fingerprint, asset.name))
    with WorkspaceStore(source) as workspace:
        assert workspace.annotations()[0]["flag"] == "unmarked"
        workspace.annotate([asset], flag="pick")
        workspace.annotate([asset], flag="unmarked")
        workspace.annotate([asset], flag="reject")
        annotation = workspace.annotations()[0]
        assert (annotation["rating"], annotation["caption"], annotation["flag"]) == (
            4, "Keep text", "reject"
        )
        payload = workspace.export_payload()
        assert payload["schema_version"] == 5
        with pytest.raises(ValueError, match="flag"):
            workspace.annotate([asset], flag="delete")
    with WorkspaceStore(tmp_path / "restored") as restored:
        assert restored.merge_import(payload).imported == 1
        assert restored.annotations()[0] == annotation
    # Older exports still import as unmarked, with their ratings intact.
    payload["schema_version"] = 2
    del payload["annotations"][0]["flag"]
    with WorkspaceStore(tmp_path / "legacy") as restored:
        restored.merge_import(payload)
        assert restored.annotations()[0]["flag"] == "unmarked"
        assert restored.annotations()[0]["rating"] == 4
