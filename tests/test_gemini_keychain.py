"""The Gemini key can live in the login Keychain for apps that have no shell environment."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from conftest import security_calls

from latent import gemini
from latent.cli import main
from latent.gemini import GeminiEmbeddingEncoder
from latent.web_server import LibraryServer

KEY = "AIzaFakeKeyForTests_0123456789abcdefghij"


class StubHTTP:
    def __init__(self):
        self.headers = []

    def open(self, request, timeout):
        self.headers.append(dict(request.header_items()))
        body = json.dumps({"embeddings": [{"values": [1.0] + [0.0] * 3071}]}).encode()
        return io.BytesIO(body)


@pytest.fixture
def no_environment_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


def test_keychain_key_is_used_when_the_environment_has_none(no_environment_key, monkeypatch):
    assert gemini.api_key() is None and gemini.credential_source() is None
    assert not GeminiEmbeddingEncoder.credentials_available()
    gemini.store_keychain_key(f"  {KEY}\n")
    assert gemini.credential_source() == "keychain"
    encoder = GeminiEmbeddingEncoder()
    encoder._opener = http = StubHTTP()
    encoder.encode_texts(["snow"])
    assert http.headers[0]["X-goog-api-key"] == KEY
    monkeypatch.setenv("GEMINI_API_KEY", "environment-key-wins-0123456789")
    assert gemini.api_key() == "environment-key-wins-0123456789"
    assert gemini.credential_source() == "environment"


def test_storing_a_key_never_puts_it_in_process_arguments(no_environment_key, fake_keychain):
    gemini.store_keychain_key(KEY)
    gemini.store_keychain_key(KEY.replace("Fake", "Next"))  # -U replaces the existing item
    assert gemini.api_key() == KEY.replace("Fake", "Next")
    calls = security_calls(fake_keychain)
    assert ["-i"] in calls
    assert all(KEY not in " ".join(call) for call in calls)
    assert all("Next" not in " ".join(call) for call in calls)


def test_malformed_keys_are_rejected_before_the_keychain_is_touched(
    no_environment_key, fake_keychain
):
    for key in ("", "short", 'has"quote-0123456789012345', "has space 0123456789012345"):
        with pytest.raises(ValueError, match="does not look like"):
            gemini.store_keychain_key(key)
    assert security_calls(fake_keychain) == []


def test_deleting_reports_whether_a_key_was_removed(no_environment_key):
    assert gemini.delete_keychain_key() is False
    gemini.store_keychain_key(KEY)
    assert gemini.delete_keychain_key() is True
    assert gemini.api_key() is None


def _request(base, path, payload=None, *, method=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(base + path, data=data, method=method,
                      headers={"Content-Type": "application/json", **(headers or {})})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def test_service_saves_and_removes_the_key_without_ever_returning_it(
    tmp_path: Path, no_environment_key
):
    server = LibraryServer(("127.0.0.1", 0), tmp_path / "state", workspace_dir=tmp_path / "ws")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        data_id = _request(base, "/health")["data_id"]

        def gemini_entry(payload):
            return next(b for b in payload["backends"] if b["key"] == "gemini")

        before = gemini_entry(_request(base, "/api/search-backends"))
        assert before["credential_source"] is None and not before["available"]
        assert "Add a Gemini API key" in before["availability"]

        with pytest.raises(HTTPError) as rejected:
            _request(base, "/api/gemini-key", {"expected_data_id": data_id, "key": KEY},
                     headers={"Origin": "https://unrelated.example"})
        assert rejected.value.code == 403
        rejected.value.close()
        assert gemini.api_key() is None

        saved = _request(base, "/api/gemini-key", {"expected_data_id": data_id, "key": KEY})
        assert gemini_entry(saved)["credential_source"] == "keychain"
        assert gemini_entry(saved)["available"]
        assert KEY not in json.dumps(saved)
        assert KEY not in json.dumps(_request(base, "/api/search-backends"))

        removed = _request(base, "/api/gemini-key", {"expected_data_id": data_id},
                           method="DELETE")
        assert gemini_entry(removed)["credential_source"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_cli_saves_from_stdin_and_reports_the_source(
    no_environment_key, monkeypatch, capsys
):
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    assert main(["gemini-key", "set"]) == 0
    assert KEY not in capsys.readouterr().out
    assert main(["gemini-key", "status"]) == 0
    assert "login Keychain" in capsys.readouterr().out
    assert main(["gemini-key", "delete"]) == 0
    assert main(["gemini-key", "status"]) == 1
    assert "not configured" in capsys.readouterr().out
