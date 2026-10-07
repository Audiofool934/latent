from __future__ import annotations

import base64
import io
import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import HTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4

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

    def encode_image_query(self, data: bytes, query: str = "") -> list[float]:
        with Image.open(io.BytesIO(data)) as image:
            assert image.format == "JPEG" and max(image.size) <= 512
        return [1.0, 0.0, 0.0, 0.0]

    def encode_texts(self, texts: list[str]) -> list[list[float]]:
        assert texts
        return [[1.0, 0.0, 0.0, 0.0] for _text in texts]


def test_saved_searches_replay_after_restart_without_encoder_calls(tmp_path: Path) -> None:
    _seed_pageable_library(tmp_path)
    embedding_dir = tmp_path / "embeddings"
    with EmbeddingStore(embedding_dir, dimensions=4) as store:
        store.sync_assets([
            EmbeddingAsset(
                asset_id=i, provider="test", remote_path=f"/archive/{i}.ARW",
                fingerprint=f"fingerprint-{i}", capture_at="2025:12:25 14:30:00",
                contact_relative_path=f"contact/{i}.jpg",
            ) for i in (1, 2, 3)
        ])
        store.finish_jobs([
            (job, serialize_float16_vector([1.0 / job.asset_id, 1.0, 0.0, 0.0], 4))
            for job in store.claim_jobs(3)
        ])
    index = VectorIndex(embedding_dir, dimensions=4)

    class CountingEncoder(FakeTextEncoder):
        text_calls = 0
        image_calls = 0
        offline = False

        def encode_texts(self, texts: list[str]) -> list[list[float]]:
            assert not self.offline, "Saved searches must work without the encoder"
            self.text_calls += 1
            return super().encode_texts(texts)

        def encode_image_query(self, data: bytes, query: str = "") -> list[float]:
            assert not self.offline, "Saved image searches must work without the encoder"
            self.image_calls += 1
            return super().encode_image_query(data, query)

    encoder = CountingEncoder()
    semantic = SemanticSearch(index, encoder)
    reference = _jpeg_bytes((320, 240))
    image_payload = {
        "image_base64": base64.b64encode(reference).decode(), "image_name": "reference.jpg",
        "query": "blue snow", "order": "closest",
    }
    with _running_server(tmp_path, vector_index=index, semantic_search=semantic) as base:
        identity, _ = _get_json(f"{base}/health")
        first, _ = _get_json(f"{base}/api/search?q=blue%20snow")
        repeat, _ = _get_json(f"{base}/api/search?q=blue%20snow&order=variety&rating_min=4")
        image, _ = _request_json(f"{base}/api/search/image", method="POST", payload=image_payload)
        assert not first["query_cached"] and not image["query_cached"]
        assert repeat["query_cached"] and repeat["cloud_access"] is False
        assert repeat["results"] == [] and first["history_id"] == repeat["history_id"]
        with pytest.raises(HTTPError) as invalid:
            _get_json(f"{base}/api/search?q=uncached&date_from=2026-02-01&date_to=2025-01-01")
        assert invalid.value.code == 400
        assert (encoder.text_calls, encoder.image_calls) == (1, 1)

    encoder.offline = True
    with _running_server(tmp_path, vector_index=index, semantic_search=semantic) as base:
        entries, _ = _get_json(f"{base}/api/search/history")
        assert len(entries["entries"]) == 2
        assert {entry["kind"] for entry in entries["entries"]} == {"text", "image"}
        image_entry = next(entry for entry in entries["entries"] if entry["kind"] == "image")
        assert image_entry["image_name"] == "reference.jpg"
        with urlopen(base + image_entry["image_url"], timeout=2) as response:
            assert response.read() == reference
        repeat, _ = _get_json(f"{base}/api/search?q=blue%20snow&order=least_similar")
        assert repeat["query_cached"] and repeat["cloud_access"] is False
        repeat_image, _ = _request_json(
            f"{base}/api/search/image", method="POST",
            payload={**image_payload, "order": "variety"},
        )
        assert repeat_image["query_cached"] and repeat_image["cloud_access"] is False
        payload = {"filters": {}, "order": "closest", "expected_data_id": identity["data_id"]}
        for identifier in (first["history_id"], image["history_id"]):
            replay, _ = _request_json(
                f"{base}/api/search/history/{identifier}/replay", method="POST", payload=payload,
            )
            assert replay["query_cached"] and replay["total"] == 3
            assert not replay["cloud_access"] and not replay["archive_cloud_access"]
        with pytest.raises(HTTPError) as mismatch:
            _request_json(
                f"{base}/api/search/history/{image['history_id']}/replay", method="POST",
                payload={**payload, "expected_data_id": "different-library"},
            )
        assert mismatch.value.code == 400
        for route in ("/api/search/history", image_entry["image_url"]):
            with pytest.raises(HTTPError) as denied:
                urlopen(Request(base + route, headers={"Sec-Fetch-Site": "cross-site"}))
            assert denied.value.code == 403
        _request_json(
            f"{base}/api/search/history/{image['history_id']}", method="DELETE",
            payload={"expected_data_id": identity["data_id"]},
        )
        with pytest.raises(HTTPError) as removed:
            _request_json(
                f"{base}/api/search/history/{image['history_id']}/replay",
                method="POST", payload=payload,
            )
        assert removed.value.code == 404
        remaining, _ = _get_json(f"{base}/api/search/history")
        assert len(remaining["entries"]) == 1
        assert (encoder.text_calls, encoder.image_calls) == (1, 1)


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
    workspace_dir: Path | None = None,
) -> Iterator[str]:
    server: HTTPServer = LibraryServer(
        ("127.0.0.1", 0),
        state_dir,
        vector_index=vector_index,
        semantic_search=semantic_search,
        workspace_dir=workspace_dir or state_dir / "workspace",
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


def test_location_changes_require_current_library_and_revision(tmp_path: Path) -> None:
    original = tmp_path / "originals"
    original.mkdir()
    with _running_server(tmp_path / "state") as base_url:
        current, _ = _get_json(f"{base_url}/api/locations")
        payload = {
            "action": "add_source",
            "path": str(original),
            "expected_data_id": current["data_id"],
            "expected_revision": current["revision"],
        }
        with pytest.raises(HTTPError) as error:
            _request_json(
                f"{base_url}/api/locations",
                method="POST",
                payload={**payload, "expected_data_id": "another-library"},
            )
        assert error.value.code == 400
        changed, _ = _request_json(f"{base_url}/api/locations", method="POST", payload=payload)
        assert changed["sources"][0]["folder"]["path"] == str(original)
        with pytest.raises(HTTPError) as error:
            _request_json(f"{base_url}/api/locations", method="POST", payload=payload)
        assert error.value.code == 400
        request = Request(
            f"{base_url}/api/locations",
            data=json.dumps({**payload, "expected_revision": changed["revision"]}).encode(),
            headers={"Content-Type": "application/json", "Origin": "https://example.com"},
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request)
        assert error.value.code == 403


@pytest.mark.parametrize("dates", [
    ("2025-11-12", "2025-12-12", "2026-01-01"),
    ("2025-12-12", "2025-12-25", "2026-01-01"),
])
def test_archive_periods_include_all_days_and_keep_pagination(tmp_path: Path, dates) -> None:
    _seed_pageable_library(tmp_path, capture_dates=dates)
    with _running_server(tmp_path) as base_url:
        for period in ["2025", "2025-11", "2025-12", "2025-12-12", "2026", "2024"]:
            expected = [i for i, day in enumerate(dates, 1) if day.startswith(period)]
            ids = []
            offset = 0
            while True:
                page, _ = _get_json(f"{base_url}/api/assets?date={period}&limit=1&offset={offset}")
                assert page["total"] == len(expected)
                ids.extend(asset["id"] for asset in page["assets"])
                if not page["has_more"]:
                    assert page["next_offset"] is None
                    break
                offset = page["next_offset"]
            assert ids == expected
        filtered, _ = _get_json(f"{base_url}/api/assets?date=2025&q=DSC00002")
        assert filtered["total"] == 1
        assert [asset["id"] for asset in filtered["assets"]] == [2]
        for invalid in ["202", "2025-1", "2025-13", "2025-02-29", "0000", "2025-01-01-extra"]:
            with pytest.raises(HTTPError) as error:
                _get_json(f"{base_url}/api/assets?date={invalid}")
            assert error.value.code == 400


def test_sequence_deletion_checks_library_and_keeps_photos_and_other_work(tmp_path: Path) -> None:
    _seed_pageable_library(tmp_path)
    with _running_server(tmp_path) as base_url:
        health, _ = _get_json(f"{base_url}/health")
        folder, _ = _request_json(f"{base_url}/api/sequence-folders", method="POST",
                                  payload={"name": "Studies"})
        sequences = []
        for name in ["Delete this study", "Keep this study"]:
            created, _ = _request_json(f"{base_url}/api/sequences", method="POST",
                payload={"name": name, "folder_id": folder["folder"]["id"]})
            sequence_id = created["sequence"]["id"]
            sequences.append(sequence_id)
            _request_json(f"{base_url}/api/sequences/{sequence_id}/items", method="POST",
                          payload={"asset_ids": [2, 1]})
        _request_json(f"{base_url}/api/annotations", method="PATCH",
                      payload={"asset_ids": [1], "rating": 5, "caption": "Keep this caption"})
        before, _ = _get_json(f"{base_url}/api/assets")
        kept, _ = _get_json(f"{base_url}/api/sequences/{sequences[1]}")
        editing_file = tmp_path / "workspace" / "editing" / "retained" / "working.jpg"
        editing_file.parent.mkdir(parents=True)
        editing_file.write_bytes(b"retained editing output")
        with pytest.raises(HTTPError) as error:
            _request_json(f"{base_url}/api/sequences/{sequences[0]}", method="DELETE",
                          payload={"expected_data_id": "another-library"})
        assert error.value.code == 400
        rejected, _ = _get_json(f"{base_url}/api/sequences")
        assert len(rejected["sequences"]) == 2
        deleted, _ = _request_json(f"{base_url}/api/sequences/{sequences[0]}", method="DELETE",
                                  payload={"expected_data_id": health["data_id"]})
        assert deleted == {"deleted": True, "archive_modified": False}
        after, _ = _get_json(f"{base_url}/api/assets")
        assert after == before
        after_kept, _ = _get_json(f"{base_url}/api/sequences/{sequences[1]}")
        assert after_kept == kept
        remaining, _ = _get_json(f"{base_url}/api/sequences")
        assert [sequence["id"] for sequence in remaining["sequences"]] == [sequences[1]]
        assert remaining["folders"][0]["id"] == folder["folder"]["id"]
        assert editing_file.read_bytes() == b"retained editing output"


def test_library_summary_counts_photos_without_capture_dates(tmp_path: Path) -> None:
    _seed_library(tmp_path)
    with StateStore(tmp_path) as store:
        store.connection.execute("UPDATE assets SET capture_at=NULL")
        store.connection.commit()
    with _running_server(tmp_path) as base_url:
        result, _ = _get_json(f"{base_url}/api/library")
        assert result["cached_assets"] == 1
        assert result["dates"] == []
        assert result["embedding_index"]["total_assets"] == 1


def test_sequence_folder_api_preserves_photo_contents_and_checks_library(tmp_path: Path) -> None:
    _seed_library(tmp_path)
    with _running_server(tmp_path) as base_url:
        health, _ = _get_json(f"{base_url}/health")
        expected = {"expected_data_id": health["data_id"]}
        folder, status = _request_json(
            f"{base_url}/api/sequence-folders",
            method="POST",
            payload={"name": "Projects", "folder_id": str(uuid4()), **expected},
        )
        assert status == 201
        parent = folder["folder"]["id"]
        child, _ = _request_json(
            f"{base_url}/api/sequence-folders",
            method="POST",
            payload={"name": "Portraits", "parent_id": parent, **expected},
        )
        child_id = child["folder"]["id"]
        sequence, _ = _request_json(
            f"{base_url}/api/sequences",
            method="POST",
            payload={"name": "Edit", "folder_id": child_id, **expected},
        )
        seq_id = sequence["sequence"]["id"]
        added, _ = _request_json(
            f"{base_url}/api/sequences/{seq_id}/items",
            method="POST",
            payload={"asset_ids": [1]},
        )
        for bad_payload in [
            {"parent_id": child_id, **expected},
            {"name": "Wrong library", "expected_data_id": "another-library"},
        ]:
            with pytest.raises(HTTPError) as error:
                _request_json(
                    f"{base_url}/api/sequence-folders/{parent}",
                    method="PATCH",
                    payload=bad_payload,
                )
            assert error.value.code == 400
        _request_json(
            f"{base_url}/api/sequence-folders/{child_id}",
            method="DELETE",
            payload=expected,
        )
        after, _ = _get_json(f"{base_url}/api/sequences/{seq_id}")
        assert after["sequence"]["folder_id"] == parent
        assert after["sequence"]["items"] == added["sequence"]["items"]
        moved, _ = _request_json(
            f"{base_url}/api/sequences/{seq_id}",
            method="PATCH",
            payload={"folder_id": None, **expected},
        )
        assert moved["sequence"]["folder_id"] is None
        library, _ = _get_json(f"{base_url}/api/sequences")
        assert [f["id"] for f in library["folders"]] == [parent]


def test_server_reads_only_seeded_local_index_and_cache(tmp_path: Path) -> None:
    asset, contact, preview = _seed_library(tmp_path)

    with _running_server(tmp_path) as base_url:
        health, _ = _get_json(f"{base_url}/health")
        assert health["status"] == "ok"
        assert health["cloud_access"] is False
        assert health["service"] == "latent"
        assert health["protocol_version"] == 1
        assert health == _get_json(f"{base_url}/health")[0]

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


def test_cached_shooting_settings_survive_library_and_annotation_payloads(tmp_path: Path) -> None:
    _seed_library(tmp_path)
    with StateStore(tmp_path) as store:
        store.connection.execute("UPDATE assets SET exif_json=?", (json.dumps({
            "ExposureTime": "1/500", "FNumber": 6.3, "ISO": "250",
            "FocalLength": "400 mm", "BodySerialNumber": "private", "GPSLatitude": 12.3,
        }),))
        store.connection.commit()
    expected = {"exposure_time": 0.002, "f_number": 6.3, "iso": 250, "focal_length": 400}
    with _running_server(tmp_path) as base_url:
        payload, _ = _get_json(f"{base_url}/api/assets")
        asset = payload["assets"][0]
        assert asset["exif"] == expected
        assert "exif_json" not in asset
        # The by-ID lookup is also used when a rating updates the displayed photo.
        with StateStore(tmp_path) as store:
            record = store.library_assets_by_ids([asset["id"]])[0]
            handler = object.__new__(LibraryRequestHandler)
            assert handler._asset_payload(record)["exif"] == expected


@pytest.mark.parametrize("raw", [
    None, "broken", "[]",
    '{"ISO":true,"FNumber":-1,"ExposureTime":"1/0","FocalLength":"nan"}',
])
def test_missing_or_invalid_exif_does_not_break_browsing(raw) -> None:
    from latent.web_server import _cached_photo_exif

    assert _cached_photo_exif(raw) == {}


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
            "queued_assets": 3,
            "total_assets": 3,
            "remaining_assets": 0,
            "stale_jobs": 0,
            "jobs": {"pending": 0, "running": 0, "succeeded": 3, "failed": 0},
            "phase": "complete",
            "semantic_ready": True,
            "model_id": DEFAULT_EMBEDDING_MODEL,
            "dimensions": 4,
            "provider": "fixed",
            "backend": "fixed",
            "engine": DEFAULT_EMBEDDING_MODEL,
            "image_text_queries": True,
        }

        for headers in (
            {"Sec-Fetch-Site": "cross-site"},
            {"Origin": "https://unrelated.example"},
            {"Host": "unrelated.example"},
        ):
            request = Request(f"{base_url}/api/search?q=snow", headers=headers)
            with pytest.raises(HTTPError) as denied:
                urlopen(request)
            assert denied.value.code == 403

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
        assert semantic["cloud_access"] is True
        assert semantic["ranking"] == "relevance"

        image_query = {
            "image_base64": base64.b64encode(_jpeg_bytes((320, 240))).decode(),
            "query": "blue snow",
            "limit": 2,
            "order": "closest",
        }
        image_results, _ = _request_json(
            f"{base_url}/api/search/image",
            method="POST",
            payload=image_query,
        )
        assert [a["id"] for a in image_results["results"]] == [1, 3]
        assert image_results["cloud_access"] is True
        assert image_results["archive_cloud_access"] is False
        least, _ = _request_json(
            f"{base_url}/api/search/image",
            method="POST",
            payload={**image_query, "order": "least_similar"},
        )
        assert [a["id"] for a in least["results"]] == [2, 3]
        assert least["results"][0]["similarity"] < least["results"][1]["similarity"]
        for invalid in (
            {"order": "other"},
            {"image_base64": "not base64"},
            {"limit": 101},
            {"image_base64": "x" * 1_398_105},
            {"query": "a" * 201},
        ):
            with pytest.raises(HTTPError) as rejected:
                _request_json(
                    f"{base_url}/api/search/image",
                    method="POST",
                    payload={**image_query, **invalid},
                )
            assert rejected.value.code == 400
        for headers in (
            {"Sec-Fetch-Site": "cross-site"},
            {"Origin": "https://unrelated.example"},
            {"Host": "unrelated.example"},
        ):
            request = Request(
                f"{base_url}/api/search/image",
                method="POST",
                data=json.dumps(image_query).encode(),
                headers={"Content-Type": "application/json", **headers},
            )
            with pytest.raises(HTTPError) as rejected:
                urlopen(request, timeout=2)
            assert rejected.value.code == 403

        varied, _ = _get_json(f"{base_url}/api/search?q=blue%20snow&limit=2&variety=1")
        assert varied["ranking"] == "relevance_with_variety"
        assert varied["results"][0]["id"] == semantic["results"][0]["id"]
        with pytest.raises(HTTPError) as invalid_variety:
            urlopen(f"{base_url}/api/search?q=snow&variety=unexpected", timeout=2)
        assert invalid_variety.value.code == 400

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

        motifs, _ = _get_json(f"{base_url}/api/curator/motifs?clusters=1&limit=3")
        assert motifs["mode"] == "grounded_motif_clusters"
        assert motifs["indexed_assets"] == 3
        assert motifs["cluster_count"] == 1
        assert motifs["basis"]["algorithm"] == "deterministic_spherical_kmeans"
        assert motifs["basis"]["labeling"] == "unlabeled"
        assert motifs["archive_modified"] is False
        motif = motifs["motifs"][0]
        assert motif["representative_asset_id"] == 3
        assert motif["report"]["member_count"] == 3
        assert motif["report"]["cross_year"] is True
        assert motif["report"]["years"] == ["2025", "2026"]
        assert [asset["id"] for asset in motif["assets"]] == [3, 1, 2]
        assert motif["report"]["limitations"][-1] == (
            "It does not establish place, identity, event, intention, or story."
        )

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

        with pytest.raises(HTTPError) as unavailable_motifs:
            urlopen(f"{base_url}/api/curator/motifs", timeout=2)
        assert unavailable_motifs.value.code == 503


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


def test_sequence_api_survives_a_service_restart(tmp_path: Path) -> None:
    _seed_pageable_library(
        tmp_path,
        capture_dates=("2024-02-03", "2025-07-08", "2026-08-29"),
    )

    with _running_server(tmp_path) as base_url:
        created, status = _request_json(
            f"{base_url}/api/sequences",
            method="POST",
            payload={"name": "Cross-year light", "note": "Restart evidence"},
        )
        assert status == 201
        sequence_id = created["sequence"]["id"]
        added, _ = _request_json(
            f"{base_url}/api/sequences/{sequence_id}/items",
            method="POST",
            payload={"asset_ids": [3, 1, 2]},
        )
        expected_item_ids = [item["id"] for item in added["sequence"]["items"]]
        assert [item["capture_at"][:10] for item in added["sequence"]["items"]] == [
            "2026:08:29",
            "2024:02:03",
            "2025:07:08",
        ]

    assert (tmp_path / "workspace/workspace.sqlite").is_file()

    with _running_server(tmp_path) as restarted_url:
        listing, _ = _get_json(f"{restarted_url}/api/sequences")
        assert [(sequence["id"], sequence["item_count"]) for sequence in listing["sequences"]] == [
            (sequence_id, 3)
        ]

        persisted, _ = _get_json(f"{restarted_url}/api/sequences/{sequence_id}")
        assert persisted["sequence"]["name"] == "Cross-year light"
        assert persisted["sequence"]["note"] == "Restart evidence"
        assert [item["id"] for item in persisted["sequence"]["items"]] == expected_item_ids
        assert all(item["library_status"] == "current" for item in persisted["sequence"]["items"])
        assert persisted["archive_modified"] is False


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
    handler.server = SimpleNamespace(service_info={"status": "ok"})
    handler.path = "/health"

    def disconnect(*_args: object, **_kwargs: object) -> None:
        raise BrokenPipeError

    handler._send_json = disconnect
    handler.do_GET()


def test_live_embedding_status_tracks_resume_without_loading_vectors(tmp_path, monkeypatch):
    _seed_pageable_library(tmp_path)
    embedding_dir = tmp_path / "embeddings"
    index = VectorIndex(embedding_dir, dimensions=4)
    monkeypatch.setattr(
        index, "refresh", lambda **_: pytest.fail("status loaded the vector matrix")
    )
    assets = [
        EmbeddingAsset(i, "test", f"/{i}.ARW", str(i), None, f"contact/{i}.jpg")
        for i in range(1, 4)
    ]
    with _running_server(tmp_path, vector_index=index) as base_url:

        def status():
            value, _ = _get_json(f"{base_url}/api/embedding-status")
            assert value["cloud_access"] is False
            assert value["total_assets"] == 3
            assert "database_path" not in value
            return value

        assert status()["phase"] == "not_started"
        assert not embedding_dir.exists()
        with EmbeddingStore(embedding_dir, dimensions=4) as store:
            store.sync_assets(assets)
            assert status()["phase"] == "paused"
            jobs = store.claim_jobs(2)
            vector = serialize_float16_vector([1.0, 0, 0, 0], 4)
            store.finish_jobs([(jobs[0], vector)])
            current = status()
            assert current["phase"] == "indexing"
            assert current["indexed_assets"] == 1
            assert current["remaining_assets"] == 2
            store.connection.execute(
                "UPDATE embedding_jobs SET claimed_at='2000-01-01T00:00:00' WHERE status='running'"
            )
            store.connection.commit()
            interrupted = status()
            assert interrupted["phase"] == "needs_attention"
            assert interrupted["stale_jobs"] == 1
            store.release_jobs([jobs[1].id])
            assert status()["phase"] == "paused"
            remaining = store.claim_jobs(2)
            store.finish_jobs([(job, vector) for job in remaining])
            completed = status()
            assert completed["phase"] == "complete"
            assert completed["indexed_assets"] == 3
            library, _ = _get_json(f"{base_url}/api/library")
            assert library["embedding_index"]["phase"] == "complete"


def test_library_browsing_survives_incompatible_embedding_store(tmp_path):
    _seed_library(tmp_path)
    embedding_dir = tmp_path / "embeddings"
    with EmbeddingStore(embedding_dir, model_id="retired-model", dimensions=4):
        pass
    index = VectorIndex(embedding_dir, dimensions=4)
    with _running_server(tmp_path, vector_index=index) as base_url:
        library, _ = _get_json(f"{base_url}/api/library")
        assert library["cached_assets"] == 1
        assert library["embedding_index"]["phase"] == "unavailable"
        assert library["embedding_index"]["semantic_ready"] is False


def test_annotations_are_persistent_and_starred_photos_use_stable_identity(tmp_path: Path) -> None:
    _seed_library(tmp_path)
    with _running_server(tmp_path) as base_url:
        page, _ = _get_json(f"{base_url}/api/assets")
        photo = page["assets"][0]
        updated, _ = _request_json(
            f"{base_url}/api/annotations",
            method="PATCH",
            payload={"asset_ids": [photo["id"]], "rating": 4, "caption": "Look again at the light"},
        )
        assert updated["assets"][0]["caption"] == "Look again at the light"
        starred, _ = _get_json(f"{base_url}/api/starred")
        assert starred["total"] == 1
        assert starred["assets"][0]["rating"] == 4
        batches, _ = _get_json(f"{base_url}/api/editing")
        assert batches == {"batches": []}
        request = Request(
            f"{base_url}/api/editing",
            method="POST",
            data=b'{"asset_ids":[1]}',
            headers={"Content-Type": "application/json", "Origin": "https://example.com"},
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request)
        assert error.value.code == 403
        cleared, _ = _request_json(
            f"{base_url}/api/annotations",
            method="PATCH",
            payload={"asset_ids": [photo["id"]], "rating": 0},
        )
        assert cleared["assets"][0]["caption"] == "Look again at the light"
        empty, _ = _get_json(f"{base_url}/api/starred")
        assert empty["total"] == 0


def test_annotation_replay_checks_library_and_stable_photo_identity(tmp_path: Path) -> None:
    asset, _, _ = _seed_library(tmp_path)
    with _running_server(tmp_path) as base_url:
        health, _ = _get_json(f"{base_url}/health")
        page, _ = _get_json(f"{base_url}/api/assets")
        photo = page["assets"][0]
        assert photo["provider"] == asset.provider
        assert photo["fingerprint"] == asset.fingerprint
        target = {key: photo[key] for key in ("id", "provider", "remote_path", "fingerprint")}
        expected = {"data_id": health["data_id"], "assets": [target]}
        updated, _ = _request_json(
            f"{base_url}/api/annotations",
            method="PATCH",
            payload={
                "asset_ids": [photo["id"]],
                "rating": 4,
                "caption": "Keep this",
                "expected": expected,
            },
        )
        assert updated["assets"][0]["rating"] == 4
        for flag in ("pick", "unmarked", "reject"):
            flagged, _ = _request_json(
                f"{base_url}/api/annotations",
                method="PATCH",
                payload={"asset_ids": [photo["id"]], "flag": flag, "expected": expected},
            )
            assert flagged["assets"][0]["flag"] == flag
            assert flagged["assets"][0]["rating"] == 4
            assert flagged["assets"][0]["caption"] == "Keep this"
            assert flagged["archive_modified"] is False
        # At-least-once replay includes explicit clearing values and remains field-idempotent.
        for _ in range(2):
            cleared, _ = _request_json(
                f"{base_url}/api/annotations",
                method="PATCH",
                payload={
                    "asset_ids": [photo["id"]],
                    "rating": 0,
                    "caption": "",
                    "expected": expected,
                },
            )
            assert cleared["assets"][0]["rating"] == 0
            assert cleared["assets"][0]["caption"] == ""
        for field in ("data_id", "provider", "remote_path", "fingerprint"):
            invalid = {"data_id": health["data_id"], "assets": [dict(target)]}
            if field == "data_id":
                invalid[field] = "another-library"
            else:
                invalid["assets"][0][field] = "another-photo"
            with pytest.raises(HTTPError) as error:
                _request_json(
                    f"{base_url}/api/annotations",
                    method="PATCH",
                    payload={"asset_ids": [photo["id"]], "rating": 5, "expected": invalid},
                )
            assert error.value.code == 400
            assert "no annotations were changed" in json.load(error.value)["message"]
        # A rebuild can reuse a numeric ID without changing the directory-derived data_id.
        with StateStore(tmp_path) as store:
            store.connection.execute(
                "UPDATE assets SET remote_path=? WHERE id=?",
                ("/archive/replacement.ARW", photo["id"]),
            )
            store.connection.commit()
        with pytest.raises(HTTPError) as error:
            _request_json(
                f"{base_url}/api/annotations",
                method="PATCH",
                payload={"asset_ids": [photo["id"]], "rating": 5, "expected": expected},
            )
        assert error.value.code == 400
        with WorkspaceStore(tmp_path / "workspace") as workspace:
            annotations = workspace.annotations()
            assert len(annotations) == 1
            assert annotations[0]["remote_path"] == asset.remote_path
            assert annotations[0]["rating"] == 0


def test_annotation_replay_validates_the_whole_batch_before_writing(tmp_path: Path) -> None:
    _seed_pageable_library(tmp_path)
    with _running_server(tmp_path) as base_url:
        health, _ = _get_json(f"{base_url}/health")
        page, _ = _get_json(f"{base_url}/api/assets")
        photos = page["assets"][:2]
        targets = [
            {key: p[key] for key in ("id", "provider", "remote_path", "fingerprint")}
            for p in photos
        ]
        malformed = [
            None,
            {},
            {"data_id": health["data_id"], "assets": targets[:1]},
            {"data_id": health["data_id"], "assets": [targets[0], targets[0]]},
            {
                "data_id": health["data_id"],
                "assets": [targets[0], {**targets[1], "fingerprint": "changed"}],
            },
            {"data_id": health["data_id"], "assets": [targets[0], {**targets[1], "id": []}]},
        ]
        for expected in malformed:
            with pytest.raises(HTTPError) as error:
                _request_json(
                    f"{base_url}/api/annotations",
                    method="PATCH",
                    payload={
                        "asset_ids": [p["id"] for p in photos],
                        "rating": 5,
                        "expected": expected,
                    },
                )
            assert error.value.code == 400
        with WorkspaceStore(tmp_path / "workspace") as workspace:
            assert workspace.annotations() == []


def test_sequence_draft_replay_creates_once_and_preserves_later_changes(tmp_path: Path) -> None:
    _seed_library(tmp_path)
    with _running_server(tmp_path) as base_url:
        health, _ = _get_json(f"{base_url}/health")
        identifier = str(uuid4())
        payload = {
            "name": "First name",
            "note": "Original note",
            "sequence_id": identifier,
            "expected_data_id": health["data_id"],
        }
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(
                executor.map(
                    lambda _: _request_json(
                        f"{base_url}/api/sequences", method="POST", payload=payload
                    ),
                    range(2),
                )
            )
        assert all(response[0]["sequence"]["id"] == identifier for response in responses)
        _request_json(
            f"{base_url}/api/sequences/{identifier}",
            method="PATCH",
            payload={"name": "Renamed elsewhere", "expected_data_id": health["data_id"]},
        )
        _request_json(
            f"{base_url}/api/sequences/{identifier}/items",
            method="POST",
            payload={"asset_ids": [1]},
        )
        replayed, _ = _request_json(f"{base_url}/api/sequences", method="POST", payload=payload)
        assert replayed["sequence"]["name"] == "Renamed elsewhere"
        assert replayed["sequence"]["item_count"] == 1
        sequences, _ = _get_json(f"{base_url}/api/sequences")
        assert len(sequences["sequences"]) == 1


def test_sequence_draft_writes_reject_another_library_or_reused_photo(tmp_path: Path) -> None:
    _seed_library(tmp_path)
    with _running_server(tmp_path) as base_url:
        health, _ = _get_json(f"{base_url}/health")
        identifier = str(uuid4())
        with pytest.raises(HTTPError) as error:
            _request_json(
                f"{base_url}/api/sequences",
                method="POST",
                payload={
                    "name": "Wrong library",
                    "sequence_id": identifier,
                    "expected_data_id": "another-library",
                },
            )
        assert error.value.code == 400
        sequences, _ = _get_json(f"{base_url}/api/sequences")
        assert sequences["sequences"] == []
        _request_json(
            f"{base_url}/api/sequences",
            method="POST",
            payload={
                "name": "Keep",
                "sequence_id": identifier,
                "expected_data_id": health["data_id"],
            },
        )
        with pytest.raises(HTTPError) as error:
            _request_json(
                f"{base_url}/api/sequences/{identifier}",
                method="PATCH",
                payload={"name": "Wrong write", "expected_data_id": "another-library"},
            )
        assert error.value.code == 400
        page, _ = _get_json(f"{base_url}/api/assets")
        target = {
            key: page["assets"][0][key] for key in ("id", "provider", "remote_path", "fingerprint")
        }
        target["fingerprint"] = "replacement-photo"
        with pytest.raises(HTTPError) as error:
            _request_json(
                f"{base_url}/api/sequences/{identifier}/items",
                method="POST",
                payload={
                    "asset_ids": [1],
                    "expected": {"data_id": health["data_id"], "assets": [target]},
                },
            )
        assert error.value.code == 400
        sequence, _ = _get_json(f"{base_url}/api/sequences/{identifier}")
        assert sequence["sequence"]["name"] == "Keep"
        assert sequence["sequence"]["items"] == []
