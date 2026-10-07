"""Local EmbeddingGemma 2 backend: runtime contract, separate indexes, validation, switching."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import random
import signal
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest
from PIL import Image

from latent import embeddinggemma
from latent.cli import main
from latent.embedding_backends import (
    EMBEDDINGGEMMA,
    EMBEDDINGGEMMA_PROFILE,
    GEMINI,
    GEMINI_PROFILE,
    BackendSelection,
    EmbeddingEngine,
    current_validation,
    engine_dirs,
    validate_index,
)
from latent.embedding_runs import EmbeddingRuns
from latent.embeddinggemma import (
    ARTIFACTS,
    QUERY_PREFIX,
    Artifact,
    EmbeddingGemmaEncoder,
    EmbeddingGemmaRuntime,
    RuntimeConfig,
    fetch_artifacts,
    save_runtime_config,
)
from latent.embeddings import (
    EmbeddingStore,
    EmbeddingWorker,
    load_embedding_assets,
    serialize_float16_vector,
)
from latent.errors import ConfigurationError, EmbeddingServiceError
from latent.models import PreviewLocation, ProbeResult, RemoteAsset
from latent.storage import CacheManager, StateStore
from latent.web_server import LibraryServer

FAKE_LLAMA_SERVER = r'''
import hashlib, json, math, os, random, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

args = sys.argv[1:]
def arg(name):
    return args[args.index(name) + 1]
mode = os.environ.get("FAKE_LLAMA_MODE", "normal")
log = os.environ.get("FAKE_LLAMA_LOG")
def record(entry):
    if log:
        with open(log, "a") as output:
            output.write(json.dumps(entry) + "\n")
record({"pid": os.getpid(), "args": args})
if mode == "unsupported":
    print("llama_model_load: error loading model architecture: "
          "unknown model architecture: 'gemma-embedding2'", flush=True)
    sys.exit(1)
dimensions = 3072 if mode == "wrong_dims" else 768
token = arg("--api-key")
started = time.monotonic()

def vector(seed):
    generator = random.Random(hashlib.sha256(seed).digest())
    values = [generator.gauss(0, 1) for _ in range(dimensions)]
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        loading = time.monotonic() - started < 0.3
        self.send(503 if loading else 200, {"status": "loading" if loading else "ok"})

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer " + token:
            return self.send(401, {"error": "unauthorized"})
        item = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["input"][0]
        if isinstance(item, str):
            record({"text": item})
            seed = item.encode()
        else:
            url = item["content"][0]["image_url"]["url"]
            record({"image": url[:23], "parts": len(item["content"])})
            seed = url.encode()
        self.send(200, {"data": [{"index": 0, "embedding": vector(seed)}]})

ThreadingHTTPServer((arg("--host"), int(arg("--port"))), Handler).serve_forever()
'''


def _vector(seed: bytes, dimensions: int) -> list[float]:
    generator = random.Random(hashlib.sha256(seed).digest())
    values = [generator.gauss(0, 1) for _ in range(dimensions)]
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


class FakeEncoder:
    """Deterministic per-input vectors, so a photo always re-encodes to its stored vector."""

    def __init__(self, model_id: str, dimensions: int, *, image_text_queries: bool = True):
        self.model_id = model_id
        self.dimensions = dimensions
        self.image_text_queries = image_text_queries
        self.text_calls: list[str] = []
        self.image_calls = 0
        self.after_image = None

    def encode_images(self, paths, batch_size):
        vectors = []
        for path in paths:
            vectors.append(_vector(path.read_bytes(), self.dimensions))
            self.image_calls += 1
            if self.after_image is not None:
                self.after_image()
        return vectors

    def encode_texts(self, texts):
        self.text_calls.extend(texts)
        return [_vector(text.encode(), self.dimensions) for text in texts]

    def encode_image_query(self, data, query=""):
        if query and not self.image_text_queries:
            raise ValueError("unsupported")
        return _vector(data + query.encode(), self.dimensions)


def _jpeg(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (96, 64), color).save(output, format="JPEG", quality=90)
    return output.getvalue()


def _seed(state_dir: Path, count: int, *, start: int = 1) -> None:
    with StateStore(state_dir) as store:
        cache = CacheManager(store, {"contact": 10**7, "preview": 10**7, "temporary": 0})
        for index in range(start, start + count):
            asset = RemoteAsset(
                provider="test", remote_id=f"asset-{index}",
                remote_path=f"/archive/2025/2025-12-{index:02d}/DSC{index:05d}.ARW",
                name=f"DSC{index:05d}.ARW", size_bytes=70_000_000 + index,
                write_time=f"2025-12-{index:02d}T14:30:00+08:00", file_hashes={"2": f"h{index}"},
            )
            asset_id = store.upsert_asset(asset)
            store.update_probe(
                asset_id,
                ProbeResult(
                    location=PreviewLocation("PreviewImage", 512, 1000),
                    metadata={"DateTimeOriginal": f"2025:12:{index:02d} 14:30:00"},
                ),
                width=1600, height=1067,
            )
            cache.put(
                asset_id=asset_id, variant="contact", fingerprint=asset.fingerprint,
                data=_jpeg((index * 37 % 256, index * 91 % 256, index * 53 % 256)),
                width=96, height=64,
            )


def _build(engine: EmbeddingEngine, state_dir: Path, encoder: FakeEncoder) -> None:
    with engine.store() as store:
        store.sync_assets(load_embedding_assets(state_dir))
        EmbeddingWorker(store, encoder, state_dir / "cache").run(max_jobs=100)


def _engine(profile, directory: Path, encoder: FakeEncoder) -> EmbeddingEngine:
    return EmbeddingEngine(profile, directory, lambda: encoder)


@pytest.fixture
def runtime_files(tmp_path, monkeypatch):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    artifacts = []
    for artifact in ARTIFACTS:
        data = artifact.name.encode() * 16
        (model_dir / artifact.name).write_bytes(data)
        artifacts.append(Artifact(artifact.name, len(data), hashlib.sha256(data).hexdigest()))
    monkeypatch.setattr(embeddinggemma, "ARTIFACTS", tuple(artifacts))
    server = tmp_path / "llama-server"
    server.write_text(f"#!{sys.executable}\n{FAKE_LLAMA_SERVER}")
    server.chmod(0o755)
    log = tmp_path / "llama.log"
    monkeypatch.setenv("FAKE_LLAMA_LOG", str(log))
    root = tmp_path / "embeddings"
    root.mkdir()
    save_runtime_config(root, RuntimeConfig(server, model_dir))
    return SimpleNamespace(root=root, model_dir=model_dir, log=log, artifacts=artifacts)


def _log(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait(condition, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def test_local_encoder_prefixes_text_queries_and_sends_images_unprefixed(
    runtime_files, tmp_path
):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(_jpeg((10, 120, 200)))
    runtime = EmbeddingGemmaRuntime(runtime_files.root)
    encoder = EmbeddingGemmaEncoder(runtime)
    try:
        text = encoder.encode_texts(["snow"])[0]
        indexed = encoder.encode_images([photo], 8)[0]
        query = encoder.encode_image_query(photo.read_bytes())
        assert encoder.encode_texts(["snow"])[0] == text  # cached, no second request
        with pytest.raises(ValueError, match="not available with the local encoder"):
            encoder.encode_image_query(photo.read_bytes(), "at dusk")
        assert runtime.status()["state"] == "ready"
    finally:
        runtime.close()
    for vector in (text, indexed, query):
        assert len(vector) == 768
        assert abs(math.sqrt(sum(value * value for value in vector)) - 1) < 1e-6
    assert query == indexed  # image queries and indexing share preprocessing
    entries = _log(runtime_files.log)
    arguments = entries[0]["args"]
    for flag, value in (("--pooling", "mean"), ("--image-min-tokens", "280"),
                        ("--image-max-tokens", "280"), ("-ctk", "f32"), ("-ctv", "f32"),
                        ("-fa", "off"), ("--host", "127.0.0.1")):
        assert arguments[arguments.index(flag) + 1] == value
    texts = [entry["text"] for entry in entries if "text" in entry]
    assert texts.count(QUERY_PREFIX + "snow") == 1
    images = [entry for entry in entries if "image" in entry]
    assert {(entry["image"], entry["parts"]) for entry in images} == {
        ("data:image/jpeg;base64,", 1)
    }
    assert runtime.status()["state"] == "stopped"
    assert not _alive(entries[0]["pid"])


def test_incompatible_llama_cpp_is_reported_without_retrying_silently(
    runtime_files, monkeypatch
):
    monkeypatch.setenv("FAKE_LLAMA_MODE", "unsupported")
    runtime = EmbeddingGemmaRuntime(runtime_files.root)
    try:
        with pytest.raises(EmbeddingServiceError, match="cannot load EmbeddingGemma 2"):
            EmbeddingGemmaEncoder(runtime).encode_texts(["snow"])
        status = runtime.status()
        assert status["state"] == "error" and "4fbc76dec51d" in status["error"]
    finally:
        runtime.close()


def test_wrong_vector_dimensions_fail_the_startup_check(runtime_files, monkeypatch):
    monkeypatch.setenv("FAKE_LLAMA_MODE", "wrong_dims")
    runtime = EmbeddingGemmaRuntime(runtime_files.root)
    try:
        with pytest.raises(EmbeddingServiceError, match="expected 768"):
            EmbeddingGemmaEncoder(runtime).encode_texts(["snow"])
    finally:
        runtime.close()
    assert not any(_alive(entry["pid"]) for entry in _log(runtime_files.log) if "pid" in entry)


def test_changed_model_files_are_rejected_before_llama_cpp_starts(runtime_files):
    model = runtime_files.model_dir / ARTIFACTS[0].name
    data = bytearray(model.read_bytes())
    data[0] ^= 1
    model.write_bytes(bytes(data))
    runtime = EmbeddingGemmaRuntime(runtime_files.root)
    try:
        with pytest.raises(EmbeddingServiceError, match="does not match revision"):
            EmbeddingGemmaEncoder(runtime).encode_texts(["snow"])
    finally:
        runtime.close()
    assert not _log(runtime_files.log)


def test_idle_runtime_unloads_the_model_and_restarts_on_demand(runtime_files):
    runtime = EmbeddingGemmaRuntime(runtime_files.root, idle_seconds=0.3)
    encoder = EmbeddingGemmaEncoder(runtime)
    try:
        encoder.encode_texts(["first"])
        first_pid = _log(runtime_files.log)[0]["pid"]
        assert _wait(lambda: runtime.status()["state"] == "stopped")
        assert _wait(lambda: not _alive(first_pid))
        encoder.encode_texts(["second"])
        pids = [entry["pid"] for entry in _log(runtime_files.log) if "pid" in entry]
        assert len(pids) == 2 and _alive(pids[1])
    finally:
        runtime.close()


def test_llama_server_stops_when_its_owner_is_killed(runtime_files, tmp_path):
    script = tmp_path / "owner.py"
    script.write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "from latent import embeddinggemma as eg\n"
        "eg.ARTIFACTS = tuple(eg.Artifact(*item) for item in json.loads(sys.argv[2]))\n"
        "runtime = eg.EmbeddingGemmaRuntime(Path(sys.argv[1]))\n"
        "eg.EmbeddingGemmaEncoder(runtime).encode_texts(['owner'])\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    artifacts = json.dumps([[a.name, a.size, a.sha256] for a in runtime_files.artifacts])
    owner = subprocess.Popen(
        [sys.executable, str(script), str(runtime_files.root), artifacts],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert owner.stdout.readline().strip() == "ready"
        server_pid = _log(runtime_files.log)[0]["pid"]
        assert _alive(server_pid)
        owner.send_signal(signal.SIGKILL)
        owner.wait(5)
        assert _wait(lambda: not _alive(server_pid))
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.stdout.close()


def test_fetch_downloads_only_files_matching_their_pinned_checksums(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    good = Artifact("model.gguf", 12, hashlib.sha256(b"model bytes!").hexdigest())
    (served / good.name).write_bytes(b"model bytes!")
    bad = Artifact("projector.gguf", 9, hashlib.sha256(b"expected!").hexdigest())
    (served / bad.name).write_bytes(b"tampered!")
    handler = partial(SimpleHTTPRequestHandler, directory=str(served))
    handler.log_message = lambda *_: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    target = tmp_path / "models"
    try:
        assert fetch_artifacts(target, artifacts=[good], base_url=base) == [
            {"file": "model.gguf", "downloaded": True}
        ]
        assert fetch_artifacts(target, artifacts=[good], base_url=base) == [
            {"file": "model.gguf", "downloaded": False}
        ]
        with pytest.raises(ConfigurationError, match="pinned checksum"):
            fetch_artifacts(target, artifacts=[bad], base_url=base)
    finally:
        server.shutdown()
        server.server_close()
    assert sorted(path.name for path in target.iterdir()) == ["model.gguf"]


def test_each_index_generation_records_and_enforces_its_preprocessing(tmp_path):
    directory = tmp_path / "index"
    with EmbeddingStore(directory, model_id="m", dimensions=4, metadata={"pooling": "mean"}):
        pass
    for metadata in ({"pooling": "cls"}, None, {"pooling": "mean", "extra": "1"}):
        with pytest.raises(ConfigurationError, match="configuration mismatch"):
            EmbeddingStore(directory, model_id="m", dimensions=4, metadata=metadata)
    with EmbeddingStore(directory, model_id="m", dimensions=4, metadata={"pooling": "mean"}):
        pass
    # Gemini keeps its existing store layout and saved-query identity.
    assert GEMINI_PROFILE.space_id == "gemini-embedding-2"
    assert not GEMINI_PROFILE.metadata
    assert EMBEDDINGGEMMA_PROFILE.space_id.startswith("embeddinggemma-2@")
    assert EMBEDDINGGEMMA_PROFILE.metadata["query_prefix"] == QUERY_PREFIX
    assert EMBEDDINGGEMMA_PROFILE.metadata["artifact_revision"] == embeddinggemma.ARTIFACT_REVISION


def test_activation_requires_a_current_passing_validation(tmp_path):
    state = tmp_path / "state"
    _seed(state, 4)
    dirs = engine_dirs(tmp_path / "embeddings" / GEMINI_PROFILE.store_name)
    encoder = FakeEncoder(EMBEDDINGGEMMA_PROFILE.model_id, 768)
    engine = _engine(EMBEDDINGGEMMA_PROFILE, dirs[EMBEDDINGGEMMA], encoder)
    selection = BackendSelection(tmp_path / "embeddings")
    with pytest.raises(ValueError, match="Validate"):
        selection.activate(engine)
    _build(engine, state, encoder)
    with pytest.raises(ValueError, match="Validate"):
        selection.activate(engine)

    report = validate_index(engine, state, encoder)
    assert report["passed"], report["checks"]
    assert {check["name"] for check in report["checks"]} == {
        "configuration", "integrity", "vectors", "coverage", "text_query", "self_retrieval",
    }
    assert report["indexed_assets"] == report["library_assets"] == 4
    assert selection.activate(engine)["validation_id"] == report["id"]
    assert selection.read()["backend"] == EMBEDDINGGEMMA

    _seed(state, 1, start=5)  # new vectors make the earlier validation stale
    _build(engine, state, encoder)
    assert current_validation(engine)["stale"] is True
    with pytest.raises(ValueError, match="Validate"):
        selection.activate(engine)

    # A vector that does not round-trip from its contact JPEG fails validation.
    with engine.store() as store:
        row = store.connection.execute("SELECT job_id FROM embeddings LIMIT 1").fetchone()
        store.connection.execute(
            "UPDATE embeddings SET vector=? WHERE job_id=?",
            (serialize_float16_vector(_vector(b"other", 768), 768), row[0]),
        )
        store.connection.commit()
    failed = validate_index(engine, state, encoder)
    assert not failed["passed"]
    assert [c["name"] for c in failed["checks"] if not c["passed"]] == ["self_retrieval"]


def test_local_generation_is_scoped_resumable_and_never_touches_the_gemini_index(tmp_path):
    state, workspace = tmp_path / "state", tmp_path / "workspace"
    _seed(state, 3)
    dirs = engine_dirs(tmp_path / "embeddings" / GEMINI_PROFILE.store_name)
    gemini_encoder = FakeEncoder(GEMINI_PROFILE.model_id, GEMINI_PROFILE.dimensions)
    gemini = _engine(GEMINI_PROFILE, dirs[GEMINI], gemini_encoder)
    _build(gemini, state, gemini_encoder)
    with gemini.store() as store:
        gemini_rows = store.vector_rows()
    local_encoder = FakeEncoder(EMBEDDINGGEMMA_PROFILE.model_id, 768)
    runs = EmbeddingRuns(
        state, workspace,
        engines={GEMINI: gemini, EMBEDDINGGEMMA: _engine(
            EMBEDDINGGEMMA_PROFILE, dirs[EMBEDDINGGEMMA], local_encoder
        )},
    )
    try:
        plan = runs.prepare(str(uuid4()), "all", backend=EMBEDDINGGEMMA)
        assert plan["backend"] == EMBEDDINGGEMMA and plan["local"] is True
        assert plan["to_generate"] == 3
        assert plan["estimated_cost_usd"] == 0 and plan["upload_bytes"] == 0
        assert plan["estimated_seconds"] == math.ceil(3 / 1.45)
        # Pause after the first photo, as the AI Search pause button does.
        local_encoder.after_image = runs.stop.set
        runs.action(plan["id"], "start", plan["revision"])
        runs.worker.join(10)
        paused = runs.get(plan["id"])
        assert paused["status"] == "paused" and paused["succeeded"] == 1
        local_encoder.after_image = None
        runs.action(plan["id"], "resume", plan["revision"])
        runs.worker.join(10)
        assert runs.get(plan["id"])["status"] == "complete"
        assert local_encoder.image_calls == 3
        assert gemini_encoder.image_calls == 3  # only the earlier direct Gemini build
        # A default-scope review still targets the active engine.
        assert runs.prepare(str(uuid4()), "all")["backend"] == GEMINI
    finally:
        runs.close()
    with gemini.store() as store:
        assert store.vector_rows() == gemini_rows
    with EmbeddingStore(
        dirs[EMBEDDINGGEMMA], model_id=EMBEDDINGGEMMA_PROFILE.model_id, dimensions=768,
        metadata=EMBEDDINGGEMMA_PROFILE.metadata,
    ) as store:
        assert len(store.vector_rows()) == 3


def _http(base: str, path: str, payload: dict | None = None, *, method: str | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(base + path, data=data, method=method,
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def _http_error(base: str, path: str, payload: dict) -> tuple[int, str]:
    with pytest.raises(HTTPError) as raised:
        _http(base, path, payload)
    body = json.load(raised.value)
    raised.value.close()
    return raised.value.code, body["message"]


def test_service_switches_engines_explicitly_and_keeps_vector_spaces_apart(tmp_path):
    state, workspace = tmp_path / "state", tmp_path / "workspace"
    _seed(state, 3)
    gemini_dir = tmp_path / "embeddings" / GEMINI_PROFILE.store_name
    dirs = engine_dirs(gemini_dir)
    gemini_encoder = FakeEncoder(GEMINI_PROFILE.model_id, GEMINI_PROFILE.dimensions)
    local_encoder = FakeEncoder(EMBEDDINGGEMMA_PROFILE.model_id, 768, image_text_queries=False)
    _build(_engine(GEMINI_PROFILE, dirs[GEMINI], gemini_encoder), state, gemini_encoder)
    _build(_engine(EMBEDDINGGEMMA_PROFILE, dirs[EMBEDDINGGEMMA], local_encoder), state,
           local_encoder)

    server = LibraryServer(
        ("127.0.0.1", 0), state, embedding_dir=gemini_dir, workspace_dir=workspace
    )
    server.engines[GEMINI].encoder_factory = lambda: gemini_encoder
    server.engines[GEMINI].preflight = None
    server.engines[EMBEDDINGGEMMA].encoder_factory = lambda: local_encoder
    server.engines[EMBEDDINGGEMMA].preflight = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        data_id = _http(base, "/health")["data_id"]
        backends = _http(base, "/api/search-backends")
        assert backends["active"] == GEMINI and backends["switchable"]
        local = next(b for b in backends["backends"] if b["key"] == EMBEDDINGGEMMA)
        assert local["validation"]["status"] == "not_run"
        assert local["index"]["indexed_assets"] == 3 and local["local"] is True
        gemini_search = _http(base, "/api/search?q=snow")
        assert gemini_search["backend"] == GEMINI and gemini_encoder.text_calls == ["snow"]

        status, message = _http_error(base, "/api/search-backends/activate", {
            "expected_data_id": data_id, "backend": EMBEDDINGGEMMA, "validation_id": None,
        })
        assert status == 400 and "validation" in message.lower()

        _http(base, f"/api/search-backends/{EMBEDDINGGEMMA}/validate",
              {"expected_data_id": data_id})

        def validation():
            entry = next(b for b in _http(base, "/api/search-backends")["backends"]
                         if b["key"] == EMBEDDINGGEMMA)
            return entry["validation"]

        assert _wait(lambda: validation()["status"] != "running")
        report = validation()
        assert report["status"] == "passed", report
        switched = _http(base, "/api/search-backends/activate", {
            "expected_data_id": data_id, "backend": EMBEDDINGGEMMA,
            "validation_id": report["id"],
        })
        assert switched["active"] == EMBEDDINGGEMMA
        assert _http(base, "/health")["data_id"] == data_id
        summary = _http(base, "/api/library")["embedding_index"]
        assert summary["backend"] == EMBEDDINGGEMMA and summary["dimensions"] == 768
        assert summary["image_text_queries"] is False

        local_search = _http(base, "/api/search?q=snow")
        assert local_search["backend"] == EMBEDDINGGEMMA
        assert local_search["query_cached"] is False  # never reuses the Gemini vector
        assert "snow" in local_encoder.text_calls
        history = _http(base, "/api/search/history")["entries"]
        assert sorted(entry["reusable"] for entry in history) == [False, True]

        reference = (state / "cache" / load_embedding_assets(state)[0].contact_relative_path)
        image_body = {
            "image_base64": base64.b64encode(reference.read_bytes()).decode(), "query": "at dusk",
        }
        status, message = _http_error(base, "/api/search/image", image_body)
        assert status == 400 and "not available" in message
        image_body["query"] = ""
        image_search = _http(base, "/api/search/image", image_body)
        assert image_search["backend"] == EMBEDDINGGEMMA and image_search["results"]

        back = _http(base, "/api/search-backends/activate", {
            "expected_data_id": data_id, "backend": GEMINI,
        })
        assert back["active"] == GEMINI
        again = _http(base, "/api/search?q=snow")
        assert again["backend"] == GEMINI and again["query_cached"] is True
        assert gemini_encoder.text_calls == ["snow"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_cli_refuses_to_activate_an_unvalidated_local_index(tmp_path, capsys):
    state = tmp_path / "state"
    _seed(state, 1)
    common = ["--state-dir", str(state)]
    assert main(["embedding-status", *common, "--backend", EMBEDDINGGEMMA, "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["exists"] is False and status["backend"] == EMBEDDINGGEMMA
    assert status["dimensions"] == 768
    assert main(["search-backend", "activate", EMBEDDINGGEMMA, *common]) == 2
    assert "Validate" in capsys.readouterr().err
    assert main(["local-encoder", "status", *common, "--json"]) == 0
    local = json.loads(capsys.readouterr().out)
    assert local["configured"] is False and local["active_backend"] == GEMINI
