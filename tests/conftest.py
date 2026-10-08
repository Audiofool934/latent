"""Shared fixtures. Tests never read or write the real login Keychain."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from latent import gemini

# Emulates the three /usr/bin/security forms Latent uses, storing items in a JSON file and
# recording every argument list so tests can prove a key never appears in one.
FAKE_SECURITY = r'''
import json, os, shlex, sys
store = os.environ["FAKE_KEYCHAIN"]
with open(store + ".argv", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
items = json.load(open(store)) if os.path.exists(store) else {}

def options(tokens):
    values, index = {}, 0
    while index < len(tokens):
        if tokens[index] in ("-s", "-a", "-l", "-w") and index + 1 < len(tokens):
            values[tokens[index]] = tokens[index + 1]
            index += 2
        else:
            index += 1
    return values

args = sys.argv[1:]
if args == ["-i"]:
    for line in sys.stdin:
        tokens = shlex.split(line)
        if tokens and tokens[0] == "add-generic-password":
            found = options(tokens[1:])
            items[found["-s"] + "/" + found["-a"]] = found["-w"]
    json.dump(items, open(store, "w"))
elif args[0] == "find-generic-password":
    found = options(args[1:])
    value = items.get(found["-s"] + "/" + found["-a"])
    if value is None:
        sys.exit(44)
    print(value)
elif args[0] == "delete-generic-password":
    found = options(args[1:])
    if items.pop(found["-s"] + "/" + found["-a"], None) is None:
        sys.exit(44)
    json.dump(items, open(store, "w"))
    print("password has been deleted.")
'''


@pytest.fixture(scope="session")
def fake_security_tool(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("security") / "security"
    path.write_text(f"#!{sys.executable}\n{FAKE_SECURITY}")
    path.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def fake_keychain(tmp_path, monkeypatch, fake_security_tool) -> Path:
    store = tmp_path / "fake-keychain.json"
    monkeypatch.setattr(gemini, "_SECURITY", str(fake_security_tool))
    monkeypatch.setenv("FAKE_KEYCHAIN", str(store))
    gemini._forget_keychain_key()
    yield store
    gemini._forget_keychain_key()


def security_calls(store: Path) -> list[list[str]]:
    log = Path(str(store) + ".argv")
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


# Test jobs normally finish in a second or two. The limit is generous so that a busy machine
# cannot fail a test. It only stops a hung worker from hanging the whole run.
WORKER_TIMEOUT = 60


def join_worker(worker: threading.Thread) -> None:
    worker.join(WORKER_TIMEOUT)
    assert not worker.is_alive(), f"{worker.name} did not finish in {WORKER_TIMEOUT} seconds"
