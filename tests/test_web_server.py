from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest
from PIL import Image

from latent.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingAsset,
    EmbeddingStore,
    serialize_float16_vector,
)
from latent.models import PreviewLocation, ProbeResult, RemoteAsset
from latent.search import SemanticSearch, VectorIndex
from latent.storage import CacheManager, StateStore
from latent.web_server import LibraryRequestHandler, LibraryServer, _is_loopback
from latent.workspace import WorkspaceStore


class FakeTextEncoder:
    model_id = DEFAULT_EMBEDDING_MODEL

    def encode_texts(self, texts: list[str]) -> list[list[float]]:
        assert texts
        return [[1.0, 0.0, 0.0, 0.0] for _text in texts]


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


def _seed_pageable_library(
    state_dir: Path,
    *,
    capture_dates: tuple[str, str, str] = ("2025-12-25",) * 3,
) -> None:
    contact = _jpeg_bytes((480, 320))
    rows = (
        ("DSC00001.ARW", "ILCE-7RM5", "FE 24mm F1.4 GM"),
        ("DSC00002.ARW", "ILCE-7RM5", "FE 35mm F1.4 GM"),
        ("DSC00003.ARW", "ILCE-7RM2", "FE 70-200mm F2.8 GM"),
    )
    with StateStore(state_dir) as store:
        cache = CacheManager(
            store,
            {"contact": 10**7, "preview": 10**7, "temporary": 0},
        )
        for index, ((name, camera, lens), capture_date) in enumerate(
            zip(rows, capture_dates, strict=True),
            start=1,
        ):
            exif_date = capture_date.replace("-", ":")
            asset = RemoteAsset(
                provider="test",
                remote_id=f"asset-{index}",
                remote_path=f"/archive/{capture_date[:4]}/{capture_date}/{name}",
                name=name,
                size_bytes=70_000_000 + index,
                write_time=f"{capture_date}T14:30:{index:02d}+08:00",
                file_hashes={"2": f"hash-{index}"},
            )
            asset_id = store.upsert_asset(asset)
            store.update_probe(
                asset_id,
                ProbeResult(
                    location=PreviewLocation("PreviewImage", 512, len(contact)),
                    metadata={
                        "DateTimeOriginal": f"{exif_date} 14:30:{index:02d}",
                        "Model": camera,
                        "LensModel": lens,
                    },
                ),
                width=1600,
                height=1067,
            )
            cache.put(
                asset_id=asset_id,
                variant="contact",
                fingerprint=asset.fingerprint,
                data=contact,
                width=480,
                height=320,
            )


@contextmanager
def _running_server(
    state_dir: Path,
    *,
    vector_index: VectorIndex | None = None,
    semantic_search: SemanticSearch | None = None,
) -> Iterator[str]:
    server: HTTPServer = LibraryServer(
        ("127.0.0.1", 0),
        state_dir,
        vector_index=vector_index,
        semantic_search=semantic_search,
        workspace_dir=state_dir / "workspace",
    )
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


def _request_json(
    url: str,
    *,
    method: str,
    payload: dict[str, object] | None = None,
) -> tuple[dict[str, object], int]:
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urlopen(request, timeout=2) as response:
        return json.loads(response.read()), response.status


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
        assert payload["total"] == 1
        assert payload["has_more"] is False
        assert payload["next_offset"] is None
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

        query = quote("x" * 201)
        with pytest.raises(HTTPError) as oversized_search:
            urlopen(f"{base_url}/api/assets?date=2025-12-25&q={query}", timeout=2)
        assert oversized_search.value.code == 400


def test_asset_api_reports_page_boundaries_and_searches_full_date(tmp_path: Path) -> None:
    _seed_pageable_library(tmp_path)

    with _running_server(tmp_path) as base_url:
        first, _ = _get_json(f"{base_url}/api/assets?date=2025-12-25&limit=2")
        assert first["total"] == 3
        assert first["has_more"] is True
        assert first["next_offset"] == 2
        assert [asset["name"] for asset in first["assets"]] == [
            "DSC00001.ARW",
            "DSC00002.ARW",
        ]

        last, _ = _get_json(f"{base_url}/api/assets?date=2025-12-25&limit=2&offset=2")
        assert last["total"] == 3
        assert last["has_more"] is False
        assert last["next_offset"] is None
        assert [asset["name"] for asset in last["assets"]] == ["DSC00003.ARW"]

        search, _ = _get_json(f"{base_url}/api/assets?date=2025-12-25&q={quote('70-200MM')}")
        assert search["query"] == "70-200MM"
        assert search["total"] == 1
        assert search["has_more"] is False
        assert [asset["name"] for asset in search["assets"]] == ["DSC00003.ARW"]


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


def test_semantic_and_visual_similarity_apis_return_ranked_local_assets(
    tmp_path: Path,
) -> None:
    _seed_pageable_library(
        tmp_path,
        capture_dates=("2025-12-25", "2025-12-25", "2026-08-29"),
    )
    embedding_dir = tmp_path / "embeddings"
    embedding_assets = [
        EmbeddingAsset(
            asset_id=asset_id,
            provider="test",
            remote_path=f"/archive/2025/2025-12-25/DSC{asset_id:05d}.ARW",
            fingerprint=f"fingerprint-{asset_id}",
            capture_at=(
                f"2026:08:29 14:30:{asset_id:02d}"
                if asset_id == 3
                else f"2025:12:25 14:30:{asset_id:02d}"
            ),
            contact_relative_path=f"contact/{asset_id}.jpg",
        )
        for asset_id in (1, 2, 3)
    ]
    vectors = {
        1: [1.0, 0.0, 0.0, 0.0],
        2: [0.0, 1.0, 0.0, 0.0],
        3: [0.8, 0.2, 0.0, 0.0],
    }
    with EmbeddingStore(embedding_dir, dimensions=4) as store:
        store.sync_assets(embedding_assets)
        jobs = store.claim_jobs(3)
        store.finish_jobs(
            [(job, serialize_float16_vector(vectors[job.asset_id], 4)) for job in jobs]
        )
    vector_index = VectorIndex(embedding_dir, dimensions=4)
    semantic_search = SemanticSearch(vector_index, FakeTextEncoder())

    with _running_server(
        tmp_path,
        vector_index=vector_index,
        semantic_search=semantic_search,
    ) as base_url:
        library, _ = _get_json(f"{base_url}/api/library")
        assert library["embedding_index"] == {
            "indexed_assets": 3,
            "semantic_ready": True,
        }

        semantic, _ = _get_json(f"{base_url}/api/search?q=blue%20snow&limit=2")
        assert semantic["mode"] == "semantic"
        assert semantic["query"] == "blue snow"
        assert [asset["id"] for asset in semantic["results"]] == [1, 3]
        assert [asset["capture_at"][:10] for asset in semantic["results"]] == [
            "2025:12:25",
            "2026:08:29",
        ]
        assert semantic["results"][0]["rank"] == 1
        assert semantic["results"][0]["similarity"] == pytest.approx(1.0)
        assert semantic["basis"]["metric"] == "cosine_similarity"
        assert "not proof" in semantic["basis"]["interpretation"]
        assert semantic["cloud_access"] is False

        similar, _ = _get_json(f"{base_url}/api/assets/1/similar?limit=2")
        assert similar["mode"] == "visual_similarity"
        assert similar["source_asset_id"] == 1
        assert [asset["id"] for asset in similar["results"]] == [3, 2]
        assert all(asset["id"] != 1 for asset in similar["results"])

        curator, _ = _get_json(f"{base_url}/api/assets/1/curator?limit=2")
        assert curator["mode"] == "grounded_curator"
        assert curator["source"]["id"] == 1
        assert [asset["id"] for asset in curator["neighbors"]] == [3, 2]
        assert curator["report"]["headline"] == "Visual neighborhood across 2 capture dates"
        assert [item["asset_id"] for item in curator["report"]["sequence_seed"]["items"]] == [
            1,
            3,
            2,
        ]
        assert "does not establish place" in curator["report"]["limitations"][1]
        assert curator["cloud_access"] is False

        with pytest.raises(HTTPError) as missing_embedding:
            urlopen(f"{base_url}/api/assets/999/similar", timeout=2)
        assert missing_embedding.value.code == 404


def test_semantic_apis_fail_closed_until_vectors_exist(tmp_path: Path) -> None:
    _seed_library(tmp_path)

    with _running_server(tmp_path) as base_url:
        with pytest.raises(HTTPError) as empty_query:
            urlopen(f"{base_url}/api/search?q=", timeout=2)
        assert empty_query.value.code == 400

        with pytest.raises(HTTPError) as unavailable_search:
            urlopen(f"{base_url}/api/search?q=moon", timeout=2)
        assert unavailable_search.value.code == 503

        with pytest.raises(HTTPError) as unavailable_similar:
            urlopen(f"{base_url}/api/assets/1/similar", timeout=2)
        assert unavailable_similar.value.code == 503

        with pytest.raises(HTTPError) as unavailable_curator:
            urlopen(f"{base_url}/api/assets/1/curator", timeout=2)
        assert unavailable_curator.value.code == 503


def test_sequence_api_persists_stable_archive_references_and_order(tmp_path: Path) -> None:
    _seed_pageable_library(tmp_path)

    with _running_server(tmp_path) as base_url:
        initial, _ = _get_json(f"{base_url}/api/sequences")
        assert initial["sequences"] == []
        library, _ = _get_json(f"{base_url}/api/library")
        assert library["workspace"] == {"sequences": 0, "items": 0, "writable": True}

        created, status = _request_json(
            f"{base_url}/api/sequences",
            method="POST",
            payload={"name": "Field motion", "note": "First pass"},
        )
        assert status == 201
        sequence_id = created["sequence"]["id"]
        assert created["archive_modified"] is False

        added, _ = _request_json(
            f"{base_url}/api/sequences/{sequence_id}/items",
            method="POST",
            payload={"asset_ids": [1, 3]},
        )
        assert added["added"] == 2
        assert added["skipped"] == 0
        assert [item["name"] for item in added["sequence"]["items"]] == [
            "DSC00001.ARW",
            "DSC00003.ARW",
        ]
        assert all(item["library_status"] == "current" for item in added["sequence"]["items"])
        assert all(item["asset"]["contact_url"] for item in added["sequence"]["items"])

        updated, _ = _request_json(
            f"{base_url}/api/sequences/{sequence_id}",
            method="PATCH",
            payload={"name": "Field rhythm", "note": "Source and echo"},
        )
        assert updated["sequence"]["name"] == "Field rhythm"
        item_ids = [item["id"] for item in updated["sequence"]["items"]]

        reordered, _ = _request_json(
            f"{base_url}/api/sequences/{sequence_id}/items/order",
            method="PUT",
            payload={"item_ids": list(reversed(item_ids))},
        )
        assert [item["id"] for item in reordered["sequence"]["items"]] == list(reversed(item_ids))

        removed, _ = _request_json(
            f"{base_url}/api/sequences/{sequence_id}/items/{item_ids[0]}",
            method="DELETE",
        )
        assert removed["sequence"]["item_count"] == 1

        deleted, _ = _request_json(
            f"{base_url}/api/sequences/{sequence_id}",
            method="DELETE",
        )
        assert deleted == {"deleted": True, "archive_modified": False}

    with WorkspaceStore(tmp_path / "workspace") as workspace:
        assert workspace.status()["sequences"] == 0


def test_sequence_api_rejects_non_json_and_missing_assets(tmp_path: Path) -> None:
    _seed_library(tmp_path)

    with _running_server(tmp_path) as base_url:
        request = Request(
            f"{base_url}/api/sequences",
            data=b"name=unsafe",
            method="POST",
        )
        with pytest.raises(HTTPError) as non_json:
            urlopen(request, timeout=2)
        assert non_json.value.code == 400

        created, _ = _request_json(
            f"{base_url}/api/sequences",
            method="POST",
            payload={"name": "Test"},
        )
        sequence_id = created["sequence"]["id"]
        with pytest.raises(HTTPError) as missing_asset:
            _request_json(
                f"{base_url}/api/sequences/{sequence_id}/items",
                method="POST",
                payload={"asset_ids": [999]},
            )
        assert missing_asset.value.code == 404


def test_loopback_detection_is_explicit() -> None:
    assert _is_loopback("127.0.0.1")
    assert _is_loopback("::1")
    assert _is_loopback("localhost")
    assert not _is_loopback("0.0.0.0")
    assert not _is_loopback("example.test")


def test_remote_bound_server_disables_workspace_mutations(tmp_path: Path) -> None:
    server = LibraryServer(
        ("0.0.0.0", 0),
        tmp_path,
        workspace_dir=tmp_path / "workspace",
    )
    try:
        assert server.workspace_writes_enabled is False
    finally:
        server.server_close()


def test_handler_silently_stops_after_client_disconnect() -> None:
    handler = LibraryRequestHandler.__new__(LibraryRequestHandler)
    handler.path = "/health"

    def disconnect(*_args: object, **_kwargs: object) -> None:
        raise BrokenPipeError

    handler._send_json = disconnect
    handler.do_GET()
