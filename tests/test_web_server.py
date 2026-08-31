from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from PIL import Image

from latent.models import PreviewLocation, ProbeResult, RemoteAsset
from latent.storage import CacheManager, StateStore
from latent.web_server import LibraryServer, _is_loopback


def _jpeg_bytes(size: tuple[int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "#456270").save(output, format="JPEG", quality=88)
    return output.getvalue()


def _seed_library(
    state_dir: Path,
    *,
    include_preview: bool = True,
) -> tuple[RemoteAsset, bytes, bytes]:
    asset = RemoteAsset(
        provider="test",
        remote_id="asset-1",
        remote_path="/archive/2025/2025-12-25/DSC00001.ARW",
        name="DSC00001.ARW",
        size_bytes=77_541_376,
        write_time="2025-12-25T14:30:00+08:00",
        file_hashes={"2": "example-hash"},
    )
    contact = _jpeg_bytes((480, 320))
    preview = _jpeg_bytes((1600, 1067))
    with StateStore(state_dir) as store:
        asset_id = store.upsert_asset(asset)
        store.update_probe(
            asset_id,
            ProbeResult(
                location=PreviewLocation("PreviewImage", 512, len(preview)),
                metadata={
                    "DateTimeOriginal": "2025:12:25 14:30:00",
                    "Model": "ILCE-7RM5",
                    "LensModel": "FE 24mm F1.4 GM",
                },
            ),
            width=1600,
            height=1067,
        )
        cache = CacheManager(
            store,
            {"contact": 10**7, "preview": 10**7, "temporary": 0},
        )
        cache.put(
            asset_id=asset_id,
            variant="contact",
            fingerprint=asset.fingerprint,
            data=contact,
            width=480,
            height=320,
        )
        if include_preview:
            cache.put(
                asset_id=asset_id,
                variant="preview",
                fingerprint=asset.fingerprint,
                data=preview,
                width=1600,
                height=1067,
            )
    return asset, contact, preview


@contextmanager
def _running_server(state_dir: Path) -> Iterator[str]:
    server: HTTPServer = LibraryServer(("127.0.0.1", 0), state_dir)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get_json(url: str) -> tuple[dict[str, object], dict[str, str]]:
    with urlopen(url, timeout=2) as response:
        payload = json.loads(response.read())
        headers = dict(response.headers.items())
    return payload, headers


def test_server_reads_only_seeded_local_index_and_cache(tmp_path: Path) -> None:
    asset, contact, preview = _seed_library(tmp_path)

    with _running_server(tmp_path) as base_url:
        health, _ = _get_json(f"{base_url}/health")
        assert health == {"status": "ok", "cloud_access": False}

        library, headers = _get_json(f"{base_url}/api/library")
        assert library["dates"] == [{"capture_date": "2025-12-25", "asset_count": 1}]
        assert library["cached_assets"] == 1
        assert library["cloud_access"] is False
        assert "default-src 'self'" in headers["Content-Security-Policy"]

        payload, _ = _get_json(f"{base_url}/api/assets?date=2025-12-25")
        assets = payload["assets"]
        assert isinstance(assets, list)
        assert len(assets) == 1
        indexed_asset = assets[0]
        assert indexed_asset["name"] == asset.name
        assert indexed_asset["camera_model"] == "ILCE-7RM5"
        assert indexed_asset["lens_model"] == "FE 24mm F1.4 GM"

        with urlopen(f"{base_url}{indexed_asset['contact_url']}", timeout=2) as response:
            assert response.read() == contact
            assert response.headers["Content-Type"] == "image/jpeg"
        with urlopen(f"{base_url}{indexed_asset['preview_url']}", timeout=2) as response:
            assert response.read() == preview

        with urlopen(f"{base_url}/", timeout=2) as response:
            assert b"LATENT" in response.read()
            assert response.headers["Cache-Control"] == "no-cache"


def test_server_rejects_invalid_queries_and_cache_traversal(tmp_path: Path) -> None:
    _seed_library(tmp_path)

    with _running_server(tmp_path) as base_url:
        with pytest.raises(HTTPError) as invalid_date:
            urlopen(f"{base_url}/api/assets?date=not-a-date", timeout=2)
        assert invalid_date.value.code == 400

        with pytest.raises(HTTPError) as traversal:
            urlopen(f"{base_url}/media/%2e%2e/index.sqlite", timeout=2)
        assert traversal.value.code == 404


def test_contact_only_asset_remains_visible_after_preview_eviction(tmp_path: Path) -> None:
    _, contact, _ = _seed_library(tmp_path, include_preview=False)

    with _running_server(tmp_path) as base_url:
        library, _ = _get_json(f"{base_url}/api/library")
        assert library["cached_assets"] == 1

        payload, _ = _get_json(f"{base_url}/api/assets?date=2025-12-25")
        indexed_asset = payload["assets"][0]
        assert indexed_asset["preview_available"] is False
        assert indexed_asset["preview_url"] == indexed_asset["contact_url"]
        with urlopen(f"{base_url}{indexed_asset['preview_url']}", timeout=2) as response:
            assert response.read() == contact


def test_loopback_detection_is_explicit() -> None:
    assert _is_loopback("127.0.0.1")
    assert _is_loopback("::1")
    assert _is_loopback("localhost")
    assert not _is_loopback("0.0.0.0")
    assert not _is_loopback("example.test")
