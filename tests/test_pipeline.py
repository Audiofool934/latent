from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from latent.errors import ConfigurationError, PreviewDecodeError
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


def test_large_png_decodes_within_its_bounded_limit_and_keeps_metadata(tmp_path, monkeypatch):
    # Scale byte limits down while exercising the same boundary and real PNG decoder.
    monkeypatch.setattr("latent.preview.MIB", 1024)
    image = Image.new("RGB", (90, 60), "navy")
    exif = Image.Exif()
    exif[272] = "Original Camera"
    body = io.BytesIO()
    image.save(body, format="PNG", exif=exif)
    data = body.getvalue().ljust(200 * 1024, b"\0")

    class Source(MemoryRangeSource):
        def read_preview(self, maximum_bytes):
            raise AssertionError("Decode PNG metadata from the bounded original")

    source = Source(make_asset("large.png", data), data)
    with StateStore(tmp_path) as store:
        result = PreviewPipeline(store, CacheManager(store)).run(source)
        assert result.preview_tag == "OriginalImage"
        assert result.range_requests == 50
        assert result.bytes_transferred == len(data)
        assert result.preview_path.is_file() and result.contact_path.is_file()
        assert store.connection.execute("SELECT camera_model FROM assets").fetchone()[0] == (
            "Original Camera"
        )


@pytest.mark.parametrize("extension,limit", [
    ("png", 256), ("tif", 128), ("tiff", 128), ("heic", 128),
])
def test_images_over_their_input_limit_are_rejected_before_reading(tmp_path, monkeypatch,
                                                                 extension, limit):
    monkeypatch.setattr("latent.preview.MIB", 1024)

    class Source:
        asset = RemoteAsset("test", "large", f"/large.{extension}", f"large.{extension}",
                            (limit + 1) * 1024)
        range_requests = bytes_transferred = metadata_elapsed_ms = 0

        def read_range(self, start, length):
            raise AssertionError("The oversized original must not be downloaded")

    with StateStore(tmp_path) as store:
        with pytest.raises(ConfigurationError, match=f"{limit} MB"):
            PreviewPipeline(store, CacheManager(store)).run(Source())
        assert store.cache_total("preview") == 0


@pytest.mark.parametrize("extension,image_format", [
    ("png", "PNG"), ("tif", "TIFF"), ("tiff", "TIFF"),
])
def test_full_images_use_exact_chunks_and_preserve_metadata_and_cache(
    tmp_path, monkeypatch, extension, image_format
):
    monkeypatch.setattr("latent.preview.MIB", 1024)
    image = Image.new("RGB", (40, 24), "navy")
    exif = Image.Exif()
    exif[272] = "Full Original Camera"
    exif[34665] = {36867: "2026:03:30 12:34:56", 42036: "Original Lens"}
    body = io.BytesIO()
    image.save(body, format=image_format, exif=exif.tobytes())
    data = body.getvalue().ljust(10 * 1024, b"\0")
    assert len(data) == 10 * 1024
    reads = []
    decoded = []
    original_open = Image.open

    class Source(MemoryRangeSource):
        def read_range(self, start, length):
            assert length <= 4 * 1024
            reads.append((start, length))
            return super().read_range(start, length)

        def read_preview(self, maximum_bytes):
            raise AssertionError("Full images must retain the original metadata")

    def open_original(stream, *args, **kwargs):
        assert stream.getvalue() == data
        decoded.append(stream.getvalue())
        return original_open(stream, *args, **kwargs)

    monkeypatch.setattr("latent.preview.Image.open", open_original)
    source = Source(make_asset(f"original.{extension}", data), data)
    with StateStore(tmp_path) as store:
        pipeline = PreviewPipeline(store, CacheManager(store))
        result = pipeline.run(source)
        assert reads == [(0, 4096), (4096, 4096), (8192, 2048)]
        assert decoded == [data, data]
        assert result.range_requests == 3
        assert result.bytes_transferred == sum(length for _, length in reads) == len(data)
        assert result.preview_tag == "OriginalImage"
        assert (result.preview_width, result.preview_height) == (40, 24)
        assert (result.contact_width, result.contact_height) == (40, 24)
        for path in (result.preview_path, result.contact_path):
            with original_open(path) as cached:
                assert cached.format == "JPEG" and cached.size == (40, 24)
                cached.verify()
        row = store.connection.execute("SELECT * FROM assets").fetchone()
        assert row["capture_at"] == "2026:03:30 12:34:56"
        assert row["camera_model"] == "Full Original Camera"
        assert row["lens_model"] == "Original Lens"
        cached = pipeline.run(source)
        assert cached.cache_hit and cached.range_requests == cached.bytes_transferred == 0
        assert len(reads) == 3 and len(decoded) == 2


@pytest.mark.parametrize("extension", ["png", "tif", "tiff"])
def test_full_image_short_middle_chunk_stops_before_decode_or_cache(
    tmp_path, monkeypatch, extension
):
    monkeypatch.setattr("latent.preview.MIB", 1024)
    data = bytes(range(256)) * 40
    reads = []

    class Source(MemoryRangeSource):
        def read_range(self, start, length):
            assert length <= 4 * 1024
            reads.append((start, length))
            return super().read_range(start, length - 1 if start == 4096 else length)

    def decode(*args, **kwargs):
        raise AssertionError("An incomplete original must not reach the image decoder")

    monkeypatch.setattr("latent.preview.Image.open", decode)
    source = Source(make_asset(f"short.{extension}", data), data)
    with StateStore(tmp_path) as store:
        with pytest.raises(PreviewDecodeError, match="original could not be read completely"):
            PreviewPipeline(store, CacheManager(store)).run(source)
        assert reads == [(0, 4096), (4096, 4096)]
        assert source.bytes_transferred == 8191
        assert store.cache_total("preview") == store.cache_total("contact") == 0
        assert store.connection.execute("SELECT count(*) FROM cache_entries").fetchone()[0] == 0
        assert store.connection.execute("SELECT count(*) FROM fetch_runs").fetchone()[0] == 0


def test_heif_reads_bounded_chunks_and_preserves_original_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr("latent.preview.MIB", 1024)
    data = bytes(range(256)) * 40
    reads = []
    decoded = []
    metadata = {
        "DateTimeOriginal": "2026:09:29 12:34:56",
        "SubSecTimeOriginal": "123",
        "OffsetTimeOriginal": "+08:00",
        "Model": "Original HEIC Camera",
        "Orientation": 6,
    }

    class Source(MemoryRangeSource):
        def read_range(self, start, length):
            assert length <= 4 * 1024
            reads.append((start, length))
            return super().read_range(start, length)

        def read_preview(self, maximum_bytes):
            raise AssertionError("HEIF decoding must retain the original metadata")

    def decode(original, extension):
        assert original == data
        assert extension == "heic"
        decoded.append(original)
        return make_jpeg((640, 427)), metadata

    monkeypatch.setattr("latent.preview.decode_heif", decode)
    source = Source(make_asset("large.heic", data), data)
    with StateStore(tmp_path) as store:
        result = PreviewPipeline(store, CacheManager(store)).run(source)
        assert reads == [(0, 4096), (4096, 4096), (8192, 2048)]
        assert decoded == [data]
        assert result.range_requests == 3
        assert result.bytes_transferred == sum(length for _, length in reads) == len(data)
        assert result.preview_tag == "HEIFImage"
        assert (result.preview_width, result.preview_height) == (640, 427)
        assert result.preview_path.is_file() and result.contact_path.is_file()
        row = store.connection.execute("SELECT * FROM assets").fetchone()
        assert row["capture_at"] == metadata["DateTimeOriginal"]
        assert row["camera_model"] == metadata["Model"]
        assert json.loads(row["exif_json"]) == metadata


def test_heif_short_middle_chunk_stops_before_decode_or_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("latent.preview.MIB", 1024)
    data = bytes(range(256)) * 40
    reads = []

    class Source(MemoryRangeSource):
        def read_range(self, start, length):
            assert length <= 4 * 1024
            reads.append((start, length))
            return super().read_range(start, length - 1 if start == 4096 else length)

    def decode(*_):
        raise AssertionError("An incomplete HEIF must not reach the decoder")

    monkeypatch.setattr("latent.preview.decode_heif", decode)
    source = Source(make_asset("short.heic", data), data)
    with StateStore(tmp_path) as store:
        with pytest.raises(PreviewDecodeError, match="HEIF original could not be read completely"):
            PreviewPipeline(store, CacheManager(store)).run(source)
        assert reads == [(0, 4096), (4096, 4096)]
        assert source.bytes_transferred == 8191
        assert store.cache_total("preview") == store.cache_total("contact") == 0
        assert store.connection.execute("SELECT count(*) FROM cache_entries").fetchone()[0] == 0
        assert store.connection.execute("SELECT count(*) FROM fetch_runs").fetchone()[0] == 0


def test_cloud_jpeg_uses_header_and_display_ready_preview_without_full_download(tmp_path):
    original = Image.new("RGB", (1600, 1000), "navy")
    exif = Image.Exif()
    exif[272] = "Test Camera"
    exif[274] = 6
    exif[34665] = {
        36867: "2025:06:01 12:34:56", 42036: "Test Lens",
        37521: "012", 36881: "+08:00", 42033: "body-123", 42037: "lens-456",
    }
    body = io.BytesIO()
    original.save(body, format="JPEG", exif=exif)
    original_bytes = body.getvalue() + bytes(8 * 1024 * 1024)
    preview_bytes = make_jpeg((500, 800))

    class CloudJPEGSource:
        asset = make_asset("original.JPG", original_bytes)
        range_requests = bytes_transferred = metadata_elapsed_ms = 0
        preview_reads = 0

        def read_range(self, start, length):
            assert start + length <= 512 * 1024
            self.range_requests += 1
            self.bytes_transferred += length
            return original_bytes[start : start + length]

        def read_preview(self, maximum_bytes):
            assert len(preview_bytes) <= maximum_bytes
            self.preview_reads += 1
            self.bytes_transferred += len(preview_bytes)
            return preview_bytes

    source = CloudJPEGSource()
    with StateStore(tmp_path) as store:
        pipeline = PreviewPipeline(store, CacheManager(store))
        result = pipeline.run(source)
        assert result.bytes_transferred == 65536 + len(preview_bytes)
        assert (result.preview_width, result.preview_height) == (500, 800)
        assert result.preview_tag == "ProviderPreview"
        row = store.connection.execute("SELECT * FROM assets").fetchone()
        assert row["capture_at"] == "2025:06:01 12:34:56"
        assert row["camera_model"] == "Test Camera"
        assert row["lens_model"] == "Test Lens"
        metadata = json.loads(row["exif_json"])
        assert metadata["SubSecTimeOriginal"] == "012"
        assert metadata["OffsetTimeOriginal"] == "+08:00"
        assert metadata["BodySerialNumber"] == "body-123"
        assert metadata["LensSerialNumber"] == "lens-456"
        second = pipeline.run(source)
        assert second.cache_hit and second.bytes_transferred == 0
        assert source.preview_reads == 1


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
