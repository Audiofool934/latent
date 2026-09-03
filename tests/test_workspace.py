from __future__ import annotations

import json
import stat
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
            "schema_version": 1,
            "database_path": str(workspace_dir / "workspace.sqlite"),
            "database_integrity": "ok",
            "sequences": 1,
            "items": 2,
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
