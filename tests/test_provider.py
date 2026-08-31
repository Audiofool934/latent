from __future__ import annotations

from email.message import Message

import pytest

from latent.errors import ArchiveSafetyError
from latent.models import RemoteAsset
from latent.provider import CloudDriveRangeSource


class FakeDownloadInfo:
    downloadUrlPath = "/static/{SCHEME}/{HOST}/{PREVIEW}/sample.ARW"
    additionalHeaders: dict[str, str] = {}
    directUrl = ""
    userAgent = ""

    @staticmethod
    def HasField(name: str) -> bool:
        return False


class FullBodyResponse:
    status = 200

    def __init__(self) -> None:
        self.headers = Message()
        self.read_called = False

    def __enter__(self) -> FullBodyResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, length: int) -> bytes:
        self.read_called = True
        return b"x" * length


def test_range_source_refuses_full_body_before_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    source = object.__new__(CloudDriveRangeSource)
    source.remote_path = "/cloud/sample.ARW"
    source.endpoint = "127.0.0.1:29798"
    source.http_scheme = "http"
    source.timeout_seconds = 30.0
    source.range_requests = 0
    source.bytes_transferred = 0
    source.metadata_elapsed_ms = 0
    source.asset = RemoteAsset(
        provider="test",
        remote_id="sample",
        remote_path=source.remote_path,
        name="sample.ARW",
        size_bytes=10_000,
    )
    source._download_info = lambda: FakeDownloadInfo()
    response = FullBodyResponse()
    monkeypatch.setattr("latent.provider.urlopen", lambda *args, **kwargs: response)

    with pytest.raises(ArchiveSafetyError, match="possible full-file download"):
        source.read_range(0, 512)

    assert response.read_called is False
    assert source.range_requests == 0
    assert source.bytes_transferred == 0
