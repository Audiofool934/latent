from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

import pytest

from latent.cli import build_parser
from latent.service_runtime import service_identity


def _command(root: Path, port: int = 0) -> list[str]:
    return [
        sys.executable, "-m", "latent", "serve", "--host", "127.0.0.1", "--port", str(port),
        "--state-dir", str(root / "state"), "--embedding-dir", str(root / "embeddings"),
        "--workspace-dir", str(root / "workspace"), "--ready-json", "--exit-on-stdin-close",
        "--editing-dir", str(root / "Pictures" / "Latent"),
    ]


@contextmanager
def _child(root: Path, port: int = 0):
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    process = subprocess.Popen(
        _command(root, port), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=environment,
    )
    try:
        yield process
    finally:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Only this test's Popen child; never discover or signal an unrelated PID.
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def _ready(process: subprocess.Popen[str]) -> dict:
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=8), "Fixture service did not report readiness"
    line = process.stdout.readline()
    assert line, process.stderr.read()
    payload = json.loads(line)
    assert payload["event"] == "ready"
    assert payload["pid"] == process.pid
    with urlopen(payload["url"] + "/health", timeout=2) as response:
        health = {k: v for k, v in payload.items() if k not in {"event", "url"}}
        assert json.load(response) == health
    return payload


def test_identity_identifies_protocol_instance_and_data_without_exposing_paths(tmp_path):
    args = (tmp_path / "state", tmp_path / "embeddings", tmp_path / "workspace")
    first = service_identity(*args)
    second = service_identity(*args)
    assert first["service"] == "latent"
    assert first["protocol_version"] == 1
    assert first["data_id"] == second["data_id"]
    assert first["instance_id"] != second["instance_id"]
    assert str(tmp_path) not in json.dumps(first)
    assert first["data_id"] != service_identity(*args[:2], tmp_path / "other")["data_id"]
    contract = service_identity(Path("/catalog"), Path("/embeddings"), Path("/workspace"))
    assert contract["data_id"] == (
        "d974d771dd92aaaea452e3dda192b74fba294fda0ffd9bd736c0509d6161a887"
    )


def test_managed_readiness_flags_are_opt_in():
    ordinary = build_parser().parse_args(["serve"])
    assert not ordinary.ready_json and not ordinary.exit_on_stdin_close
    assert ordinary.editing_dir == Path.home() / "Pictures" / "Latent"
    managed = build_parser().parse_args(["serve", "--ready-json", "--exit-on-stdin-close"])
    assert managed.ready_json and managed.exit_on_stdin_close
    custom = build_parser().parse_args(["serve", "--editing-dir", "/custom/editing"])
    assert custom.editing_dir == Path("/custom/editing")


def test_readiness_and_owner_pipe_shutdown_leave_no_running_service(tmp_path):
    with _child(tmp_path) as process:
        ready = _ready(process)
        assert ready["protocol_version"] == 1
        assert ready["cloud_access"] is False
        assert process.poll() is None
        process.stdin.close()
        assert process.wait(timeout=5) == 0


def test_duplicate_workspace_service_is_refused_without_stopping_owner(tmp_path):
    with _child(tmp_path) as owner:
        ready = _ready(owner)
        with _child(tmp_path) as duplicate:
            assert duplicate.wait(timeout=5) == 2
            assert duplicate.stdout.read() == ""
            assert "Another Latent service is using this workspace" in duplicate.stderr.read()
        assert owner.poll() is None
        with urlopen(ready["url"] + "/health", timeout=2) as response:
            assert json.load(response)["pid"] == owner.pid


def test_occupied_port_preserves_occupant_and_releases_failed_launch_lease(tmp_path):
    batch_id = "f699d3ac-607a-4c96-9bf9-14cd7206feaa"
    manifest = tmp_path / "other" / "workspace" / "editing" / batch_id / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"id": batch_id, "status": "downloading"}))
    before = manifest.read_bytes()
    with _child(tmp_path / "owner") as owner:
        ready = _ready(owner)
        port = int(ready["url"].rsplit(":", 1)[1])
        with _child(tmp_path / "other", port) as blocked:
            assert blocked.wait(timeout=5) == 2
            assert "Cannot start the library" in blocked.stderr.read()
            assert blocked.stdout.read() == ""
        assert manifest.read_bytes() == before, "A failed bind must not recover editing manifests"
        assert owner.poll() is None
        with _child(tmp_path / "other") as replacement:
            assert _ready(replacement)["pid"] != owner.pid


@pytest.mark.skipif(sys.platform == "win32", reason="Unix kernel lock and process semantics")
def test_crash_releases_kernel_lease_without_a_stale_pid_cleanup(tmp_path):
    with _child(tmp_path) as first:
        old = _ready(first)
        first.kill()
        first.wait(timeout=5)
        with _child(tmp_path) as replacement:
            new = _ready(replacement)
            assert new["instance_id"] != old["instance_id"]
            assert new["data_id"] == old["data_id"]
