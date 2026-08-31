from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from latent.models import PreviewLocation, ProbeResult, RemoteAsset
from latent.preview import PreviewPipeline
from latent.provider import MemoryRangeSource
from latent.storage import CacheManager, StateStore


class StaticProbe:
    def __init__(self, location: PreviewLocation) -> None:
        self.location = location

    def probe(self, data: bytes, suffix: str) -> ProbeResult:
        assert suffix == ".arw"
        assert data
        return ProbeResult(
            location=self.location,
            metadata={
                "DateTimeOriginal": "2026:08:29 16:05:00",
                "Model": "ILCE-7RM5",
                "LensModel": "Test Lens",
                "Orientation": 1,
            },
        )


def make_jpeg(size: tuple[int, int] = (1600, 1000), color: str = "#39576b") -> bytes:
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()


def make_asset(name: str, body: bytes, *, remote_id: str = "asset-1") -> RemoteAsset:
    return RemoteAsset(
        provider="test",
        remote_id=remote_id,
        remote_path=f"/archive/{name}",
        name=name,
        size_bytes=len(body),
        write_time="2026-08-29T08:05:00+00:00",
        file_hashes={"2": remote_id},
    )


def test_pipeline_fetches_bounded_ranges_then_hits_cache(tmp_path: Path) -> None:
    jpeg = make_jpeg()
    offset = 400
    body = bytes(offset) + jpeg + bytes(2048)
    asset = make_asset("DSC00001.ARW", body)
    location = PreviewLocation("PreviewImage", offset, len(jpeg))

    with StateStore(tmp_path) as store:
        cache = CacheManager(store, {"preview": 10**7, "contact": 10**7, "temporary": 0})
        pipeline = PreviewPipeline(
            store,
            cache,
            StaticProbe(location),
            initial_prefix_bytes=256,
            maximum_prefix_bytes=1024,
        )
        first_source = MemoryRangeSource(asset, body)
        first = pipeline.run(first_source)

        assert first.cache_hit is False
        assert first.range_requests == 2
        assert first.bytes_transferred == 256 + len(jpeg)
        assert first.bytes_transferred < len(body)
        assert first.preview_path.is_file()
        assert (first.preview_width, first.preview_height) == (1600, 1000)
        assert first.contact_path.is_file()
        assert (first.contact_width, first.contact_height) == (512, 320)

        second_source = MemoryRangeSource(asset, body)
        second = pipeline.run(second_source)

        assert second.cache_hit is True
        assert second.range_requests == 0
        assert second.bytes_transferred == 0
        assert second.preview_path == first.preview_path
        assert second.contact_path == first.contact_path
        assert second.preview_tag == "PreviewImage"
        assert second.preview_offset == offset
        assert second.embedded_preview_bytes == len(jpeg)

        status = store.status()
        assert status["database_integrity"] == "ok"
        assert status["assets"] == 1
        assert status["fetch_runs"] == 2
        assert status["cache"]["preview"]["entries"] == 1
        assert status["cache"]["contact"]["entries"] == 1


def test_lru_budget_evicts_the_older_entry(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        cache = CacheManager(store, {"preview": 15, "contact": 15, "temporary": 0})
        first_asset = make_asset("DSC00001.ARW", b"a", remote_id="first")
        second_asset = make_asset("DSC00002.ARW", b"b", remote_id="second")
        first_id = store.upsert_asset(first_asset)
        second_id = store.upsert_asset(second_asset)

        first, first_evicted = cache.put(
            asset_id=first_id,
            variant="preview",
            fingerprint=first_asset.fingerprint,
            data=b"a" * 10,
            width=1,
            height=1,
        )
        second, second_evicted = cache.put(
            asset_id=second_id,
            variant="preview",
            fingerprint=second_asset.fingerprint,
            data=b"b" * 10,
            width=1,
            height=1,
        )

        assert first_evicted == ()
        assert second_evicted == (first.path,)
        assert not first.path.exists()
        assert second.path.exists()
        assert store.cache_total("preview") == 10


def test_changed_remote_fingerprint_invalidates_cached_file(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        cache = CacheManager(store, {"preview": 100, "contact": 100, "temporary": 0})
        original = make_asset("DSC00001.ARW", b"a", remote_id="same-id")
        asset_id = store.upsert_asset(original)
        entry, _ = cache.put(
            asset_id=asset_id,
            variant="preview",
            fingerprint=original.fingerprint,
            data=b"cached",
            width=1,
            height=1,
        )
        changed = RemoteAsset(
            provider=original.provider,
            remote_id=original.remote_id,
            remote_path=original.remote_path,
            name=original.name,
            size_bytes=original.size_bytes,
            write_time="2026-08-30T08:05:00+00:00",
            file_hashes={"2": "changed"},
        )
        store.upsert_asset(changed)

        assert cache.get(asset_id, "preview", changed.fingerprint) is None
        assert not entry.path.exists()
        assert store.cache_total("preview") == 0
