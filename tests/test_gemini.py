from __future__ import annotations

import io
import json
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
from PIL import Image

from latent.cli import build_parser
from latent.embeddings import EmbeddingAsset, EmbeddingStore, EmbeddingWorker
from latent.errors import EmbeddingServiceError
from latent.gemini import GEMINI_DIMENSIONS, GEMINI_MODEL, GeminiEmbeddingEncoder


class StubHTTP:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(json.dumps(result).encode())


def response(*positions: int) -> dict:
    return {
        "embeddings": [
            {"values": [1.0 if i == position else 0.0 for i in range(GEMINI_DIMENSIONS)]}
            for position in positions
        ],
        "usageMetadata": {"promptTokenCount": 258 * len(positions)},
    }


@pytest.fixture(autouse=True)
def stub_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-for-network")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


def test_each_photo_gets_a_separate_ordered_embedding(tmp_path: Path):
    paths = [tmp_path / f"{n}.jpg" for n in range(2)]
    for path in paths:
        Image.new("RGB", (20, 10), "blue").save(path)
    encoder = GeminiEmbeddingEncoder()
    http = StubHTTP([response(2, 7)])
    encoder._opener = http
    vectors = encoder.encode_images(paths, 2)
    body = json.loads(http.requests[0].data)
    assert len(body["requests"]) == 2
    for item in body["requests"]:
        assert item["model"] == f"models/{GEMINI_MODEL}"
        assert item["outputDimensionality"] == 3072
        assert len(item["content"]["parts"]) == 1
        assert set(item["content"]["parts"][0]) == {"inlineData"}
    assert vectors[0][2] == vectors[1][7] == 1.0
    assert str(tmp_path) not in http.requests[0].data.decode()
    assert "test-key" not in http.requests[0].full_url
    assert encoder.usage()["photo_upload_bytes"] == sum(p.stat().st_size for p in paths)
    assert encoder.usage()["input_tokens"] == 516


def test_query_uses_retrieval_prefix_and_caches_repeated_searches():
    encoder = GeminiEmbeddingEncoder()
    http = StubHTTP([response(1)])
    encoder._opener = http
    first = encoder.encode_texts([" 雪山 "])
    first[0][1] = 42
    assert encoder.encode_texts(["雪山"])[0][1] == 1
    assert len(http.requests) == 1
    body = json.loads(http.requests[0].data)
    assert body["requests"][0]["content"]["parts"] == [
        {"text": "task: search result | query: 雪山"}
    ]
    assert encoder.usage()["photo_upload_bytes"] == 0


def test_authentication_is_lazy_and_missing_key_never_opens_network(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    encoder = GeminiEmbeddingEncoder()
    http = StubHTTP([])
    encoder._opener = http
    assert not encoder.credentials_available()
    with pytest.raises(EmbeddingServiceError, match="GEMINI_API_KEY"):
        encoder.encode_texts(["snow"])
    assert http.requests == []


def test_retry_after_is_honored_and_errors_never_echo_credentials(monkeypatch):
    headers = Message()
    headers["Retry-After"] = "3"
    error = HTTPError(
        "https://example.invalid",
        429,
        "test-key-not-for-network",
        headers,
        io.BytesIO(b"sensitive response body"),
    )
    encoder = GeminiEmbeddingEncoder()
    http = StubHTTP([error, response(0)])
    encoder._opener = http
    delays = []
    monkeypatch.setattr("latent.gemini.time.sleep", delays.append)
    assert encoder.encode_texts(["snow"])[0][0] == 1
    assert delays == [3]
    assert len(http.requests) == 2
    assert encoder.usage()["api_requests"] == 2


@pytest.mark.parametrize("retry_delay,expected", [("12.5s", 12.5), (None, 30)])
def test_google_json_retry_info_and_rate_limit_fallback(monkeypatch, retry_delay, expected):
    details = [{"retryDelay": retry_delay}] if retry_delay else []
    error = HTTPError(
        "https://example.invalid",
        429,
        "quota",
        {},
        io.BytesIO(json.dumps({"error": {"details": details}}).encode()),
    )
    encoder = GeminiEmbeddingEncoder()
    encoder._opener = StubHTTP([error, response(0)])
    delays = []
    monkeypatch.setattr("latent.gemini.time.sleep", delays.append)
    encoder.encode_texts(["snow"])
    assert delays == [expected]


def test_quota_error_reports_only_numeric_limit():
    body = {
        "error": {
            "message": "private-account-and-key",
            "details": [
                {
                    "violations": [
                        {
                            "quotaMetric": (
                                "generativelanguage.googleapis.com/embed_content_requests"
                            ),
                            "quotaId": "EmbedContentRequestsPerDayPerProject",
                            "quotaValue": "1000",
                            "quotaDimensions": {"project": "private-account-and-key"},
                        }
                    ]
                }
            ],
        }
    }
    encoder = GeminiEmbeddingEncoder(max_retries=0)
    encoder._opener = StubHTTP(
        [
            HTTPError(
                "https://example.invalid", 429, "quota", {}, io.BytesIO(json.dumps(body).encode())
            )
        ]
    )
    with pytest.raises(EmbeddingServiceError) as caught:
        encoder.encode_texts(["snow"])
    assert "daily quota: 1000 requests" in str(caught.value)
    assert "private-account" not in str(caught.value)


@pytest.mark.parametrize("code", [400, 401, 403, 404, 429, 503])
def test_final_http_error_has_no_provider_body_or_key(code):
    encoder = GeminiEmbeddingEncoder(max_retries=0)
    encoder._opener = StubHTTP(
        [
            HTTPError(
                "https://example.invalid",
                code,
                "test-key-not-for-network",
                {},
                io.BytesIO(b"private server response"),
            )
        ]
    )
    with pytest.raises(EmbeddingServiceError) as caught:
        encoder.encode_texts(["snow"])
    assert f"HTTP {code}" in str(caught.value)
    assert "test-key" not in str(caught.value)
    assert "private" not in str(caught.value)


def test_network_failure_is_bounded(monkeypatch):
    encoder = GeminiEmbeddingEncoder(max_retries=1)
    http = StubHTTP([URLError("secret"), URLError("secret")])
    encoder._opener = http
    monkeypatch.setattr("latent.gemini.time.sleep", lambda _: None)
    with pytest.raises(EmbeddingServiceError, match="bounded retries"):
        encoder.encode_texts(["snow"])
    assert len(http.requests) == 2


@pytest.mark.parametrize(
    "payload",
    [
        response(),
        {"embeddings": [{"values": [0.0]}]},
        {"embeddings": [{"values": [0.0] * GEMINI_DIMENSIONS}]},
        {"embeddings": [{"values": [float("nan")] * GEMINI_DIMENSIONS}]},
    ],
)
def test_invalid_vectors_are_rejected_before_persistence(payload):
    encoder = GeminiEmbeddingEncoder()
    encoder._opener = StubHTTP([payload])
    with pytest.raises(EmbeddingServiceError, match="invalid or mismatched"):
        encoder.encode_texts(["snow"])


def test_raw_or_oversized_preview_never_uploads(tmp_path: Path):
    path = tmp_path / "large.jpg"
    Image.new("RGB", (1024, 768)).save(path)
    encoder = GeminiEmbeddingEncoder()
    encoder._opener = StubHTTP([])
    with pytest.raises(ValueError, match="512 px"):
        encoder.encode_images([path], 1)
    assert encoder.usage()["api_requests"] == 0


def test_api_outage_releases_entire_claim_without_individual_retries(tmp_path: Path):
    contacts = tmp_path / "cache"
    (contacts / "contact").mkdir(parents=True)
    assets = []
    for i in range(3):
        Image.new("RGB", (20, 10)).save(contacts / f"contact/{i}.jpg")
        assets.append(EmbeddingAsset(i, "test", f"/{i}.ARW", str(i), None, f"contact/{i}.jpg"))
    encoder = GeminiEmbeddingEncoder(max_retries=0)
    http = StubHTTP([HTTPError("https://example.invalid", 429, "quota", {}, io.BytesIO())])
    encoder._opener = http
    with EmbeddingStore(tmp_path / "embeddings") as store:
        store.sync_assets(assets)
        with pytest.raises(EmbeddingServiceError):
            EmbeddingWorker(store, encoder, contacts).run(max_jobs=3, batch_size=3)
        assert store.job_counts() == {"pending": 3, "running": 0, "succeeded": 0, "failed": 0}
        assert store.status()["vectors"] == 0
    assert len(http.requests) == 1


def test_cli_rejects_unbounded_api_batches_and_retired_local_options():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["embedding-build", "--max-jobs", "10", "--batch-size", "65"])
    with pytest.raises(SystemExit):
        parser.parse_args(["embedding-build", "--max-jobs", "10", "--device", "mps"])


def test_parallel_workers_share_claims_and_obey_total_upload_cap(tmp_path: Path, monkeypatch):
    import threading
    import time
    from argparse import Namespace

    from latent.cli import _run_api_workers

    lock = threading.Lock()
    seen = []

    class FakeAPI:
        model_id = GEMINI_MODEL

        def __init__(self, **_):
            self.count = 0

        def encode_images(self, paths, batch_size):
            with lock:
                seen.extend(p.name for p in paths)
            self.count += len(paths)
            time.sleep(0.01)
            return [[1.0] + [0.0] * (GEMINI_DIMENSIONS - 1) for _ in paths]

        def usage(self):
            return dict.fromkeys(
                (
                    "api_requests",
                    "image_submissions",
                    "photo_upload_bytes",
                    "request_body_bytes",
                    "input_tokens",
                    "estimated_image_cost_usd",
                ),
                self.count,
            )

    contacts = tmp_path / "cache/contact"
    contacts.mkdir(parents=True)
    assets = []
    for i in range(20):
        (contacts / f"{i}.jpg").write_bytes(b"fake contact")
        assets.append(EmbeddingAsset(i, "test", f"/{i}.ARW", str(i), None, f"contact/{i}.jpg"))
    embedding_dir = tmp_path / "embeddings"
    with EmbeddingStore(embedding_dir) as store:
        store.sync_assets(assets)
    monkeypatch.setattr("latent.cli.GeminiEmbeddingEncoder", FakeAPI)
    result = _run_api_workers(
        Namespace(
            workers=4,
            max_jobs=12,
            timeout_seconds=60,
            state_dir=tmp_path,
            batch_size=2,
            max_consecutive_failures=5,
            progress=False,
        ),
        embedding_dir,
    )
    assert result["succeeded"] == 12
    assert result["failed"] == 0
    assert len(seen) == len(set(seen)) == 12
    with EmbeddingStore(embedding_dir) as store:
        assert store.job_counts() == {"pending": 8, "running": 0, "succeeded": 12, "failed": 0}


def test_cli_recovers_stale_claim_when_no_pending_jobs_remain(tmp_path, monkeypatch, capsys):
    from latent.cli import main

    contacts = tmp_path / "cache/contact"
    contacts.mkdir(parents=True)
    Image.new("RGB", (20, 10)).save(contacts / "1.jpg")
    asset = EmbeddingAsset(1, "test", "/1.ARW", "one", None, "contact/1.jpg")
    embedding_dir = tmp_path / "embeddings"
    with EmbeddingStore(embedding_dir) as store:
        store.sync_assets([asset])
        store.claim_jobs(1)
        store.connection.execute("UPDATE embedding_jobs SET claimed_at='2000-01-01T00:00:00'")
        store.connection.commit()
    encoder = GeminiEmbeddingEncoder()
    http = StubHTTP([response(0)])
    encoder._opener = http
    monkeypatch.setattr("latent.cli.load_embedding_assets", lambda _: [asset])
    monkeypatch.setattr("latent.cli.GeminiEmbeddingEncoder", lambda **_: encoder)
    assert (
        main(
            [
                "embedding-build",
                "--state-dir",
                str(tmp_path),
                "--embedding-dir",
                str(embedding_dir),
                "--max-jobs",
                "1",
                "--workers",
                "1",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["run"]["recovered_jobs"] == 1
    assert payload["status"]["vectors"] == 1
    assert len(http.requests) == 1


def test_cli_provider_failure_preserves_final_report_and_resumes(tmp_path, monkeypatch, capsys):
    from latent.cli import main

    contacts = tmp_path / "cache/contact"
    contacts.mkdir(parents=True)
    assets = []
    for i in range(2):
        Image.new("RGB", (20, 10)).save(contacts / f"{i}.jpg")
        assets.append(EmbeddingAsset(i, "test", f"/{i}.ARW", str(i), None, f"contact/{i}.jpg"))
    encoder = GeminiEmbeddingEncoder(max_retries=0)
    encoder._opener = StubHTTP(
        [
            response(0),
            HTTPError("https://example.invalid", 429, "secret", {}, io.BytesIO(b"secret")),
        ]
    )
    monkeypatch.setattr("latent.cli.load_embedding_assets", lambda _: assets)
    monkeypatch.setattr("latent.cli.GeminiEmbeddingEncoder", lambda **_: encoder)
    arguments = [
        "embedding-build",
        "--state-dir",
        str(tmp_path),
        "--max-jobs",
        "2",
        "--batch-size",
        "1",
        "--workers",
        "1",
        "--json",
    ]
    assert main(arguments) == 2
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert "secret" not in output
    assert payload["run"]["succeeded"] == 1
    assert payload["run"]["api_requests"] == 2
    assert "HTTP 429" in payload["run"]["provider_errors"][0]
    assert payload["status"]["jobs"] == {
        "pending": 1,
        "running": 0,
        "succeeded": 1,
        "failed": 0,
    }
    encoder._opener = StubHTTP([response(1)])
    assert main(arguments) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["run"]["succeeded"] == 1
    assert resumed["run"]["provider_errors"] == []
    assert resumed["status"]["vectors"] == 2
    assert len(encoder._opener.requests) == 1


def test_image_query_caches_vectors_and_combines_plain_text_without_metadata(tmp_path: Path):
    path = tmp_path / "reference.jpg"
    Image.new("RGB", (320, 240), "blue").save(path)
    data = path.read_bytes()
    encoder = GeminiEmbeddingEncoder()
    http = StubHTTP([response(2), response(3)])
    encoder._opener = http
    first = encoder.encode_image_query(data)
    first[2] = 42
    assert encoder.encode_image_query(data)[2] == 1
    assert len(http.requests) == 1
    encoder.encode_image_query(data, " quiet snow ")
    encoder.encode_image_query(data, "quiet snow")
    assert len(http.requests) == 2
    parts = json.loads(http.requests[1].data)["requests"][0]["content"]["parts"]
    assert parts[0] == {"text": "quiet snow"}
    assert parts[1]["inlineData"]["mimeType"] == "image/jpeg"
    assert str(path) not in http.requests[1].data.decode()
    assert encoder.usage()["image_submissions"] == 2
    assert encoder.usage()["photo_upload_bytes"] == 2 * len(data)


def test_image_query_rejects_oversized_or_unreadable_images_before_network():
    encoder = GeminiEmbeddingEncoder()
    http = StubHTTP([])
    encoder._opener = http
    cases = [b"not-an-image", b"x" * (1024 * 1024 + 1)]
    for size in [(513, 1), (1, 513)]:
        output = io.BytesIO()
        Image.new("RGB", size).save(output, format="JPEG")
        cases.append(output.getvalue())
    for data in cases:
        with pytest.raises(ValueError):
            encoder.encode_image_query(data)
    assert not http.requests
