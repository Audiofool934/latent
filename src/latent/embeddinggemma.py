"""Local EmbeddingGemma 2 encoding through an owned, loopback-only llama.cpp server."""

from __future__ import annotations

import base64
import collections
import contextlib
import hashlib
import io
import json
import math
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from PIL import Image

from .errors import ConfigurationError, EmbeddingServiceError

EMBEDDINGGEMMA_MODEL = "embeddinggemma-2"
EMBEDDINGGEMMA_DIMENSIONS = 768
ARTIFACT_REPOSITORY = "unsloth/embeddinggemma-2-GGUF"
ARTIFACT_REVISION = "ba3888272494be64ed88c9eb536ddc61a1be73d5"
LLAMA_CPP_MINIMUM_COMMIT = "4fbc76dec51d0add466f0210855c0596589b60d4"
# The model card's retrieval prefix applies to text only; images are passed unprefixed.
QUERY_PREFIX = "task: search result | query: "
IMAGE_TOKENS = 280
# Measured on an M2 Pro with this configuration; used only for time estimates.
ESTIMATED_IMAGES_PER_SECOND = 1.45
RUNTIME_CONFIG_NAME = "embeddinggemma-runtime.json"
_MAX_IMAGE_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Artifact:
    name: str
    size: int
    sha256: str


# The model and projector must stay paired at the same revision.
ARTIFACTS = (
    Artifact(
        "embeddinggemma-2-Q8_0.gguf",
        309_855_520,
        "6f1bd4ac6c5df7444f9cca7ca36cafe6cfa34cd6f49fefb1e0b4be8143aed8bc",
    ),
    Artifact(
        "mmproj-Q8_0.gguf",
        554_821_120,
        "90e7b0238009e2954f856f2081dcf7f35af026b64c765e98f4777053e1754460",
    ),
)

# Everything that changes vectors is recorded with each index generation.
INDEX_METADATA = {
    "artifact_repository": ARTIFACT_REPOSITORY,
    "artifact_revision": ARTIFACT_REVISION,
    "model_sha256": ARTIFACTS[0].sha256,
    "projector_sha256": ARTIFACTS[1].sha256,
    "quantization": "Q8_0",
    "runtime": "llama.cpp",
    "pooling": "mean",
    "image_tokens": str(IMAGE_TOKENS),
    "image_input": "contact JPEG up to 512 px, no prefix",
    "query_prefix": QUERY_PREFIX,
    "kv_cache_type": "f32",
    "flash_attention": "off",
    "normalization": "l2",
}

# Matches the bounded trial. F32 KV cache and no flash attention avoid FP16 activations,
# which the model card warns can silently degrade EmbeddingGemma 2 output.
_SERVER_ARGUMENTS = (
    "--embedding", "--pooling", "mean",
    "-c", "2048", "-b", "1024", "-ub", "1024",
    "--image-min-tokens", str(IMAGE_TOKENS), "--image-max-tokens", str(IMAGE_TOKENS),
    "-np", "1", "-t", "2", "-tb", "2",
    "-fa", "off", "-ctk", "f32", "-ctv", "f32",
    "--no-webui", "-ngl", "99",
)


@dataclass(frozen=True)
class RuntimeConfig:
    llama_server: Path
    model_dir: Path

    @property
    def model(self) -> Path:
        return self.model_dir / ARTIFACTS[0].name

    @property
    def projector(self) -> Path:
        return self.model_dir / ARTIFACTS[1].name


def load_runtime_config(embeddings_root: Path) -> RuntimeConfig | None:
    path = embeddings_root / RUNTIME_CONFIG_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return RuntimeConfig(Path(data["llama_server"]), Path(data["model_dir"]))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ConfigurationError(
            f"The local encoder configuration is unreadable: {path}"
        ) from error


def save_runtime_config(embeddings_root: Path, config: RuntimeConfig) -> Path:
    from .workspace import write_workspace_export

    path = embeddings_root / RUNTIME_CONFIG_NAME
    write_workspace_export(path, {
        "llama_server": str(config.llama_server),
        "model_dir": str(config.model_dir),
        "artifact_repository": ARTIFACT_REPOSITORY,
        "artifact_revision": ARTIFACT_REVISION,
        "configured_at": datetime.now(UTC).isoformat(),
    })
    return path


def check_files(config: RuntimeConfig) -> None:
    """Cheap presence and size checks; hashing happens when the runtime starts."""
    if not os.access(config.llama_server, os.X_OK) or not config.llama_server.is_file():
        raise ConfigurationError(f"llama-server was not found or is not executable: "
                                 f"{config.llama_server}")
    for artifact in ARTIFACTS:
        path = config.model_dir / artifact.name
        if not path.is_file():
            raise ConfigurationError(f"Model file is missing: {path}")
        if path.stat().st_size != artifact.size:
            raise ConfigurationError(f"Model file has an unexpected size: {path}")


def verify_artifacts(model_dir: Path, artifacts: Sequence[Artifact] | None = None) -> list[dict]:
    results = []
    for artifact in ARTIFACTS if artifacts is None else artifacts:
        path = model_dir / artifact.name
        if not path.is_file():
            raise ConfigurationError(f"Model file is missing: {path}")
        if path.stat().st_size != artifact.size:
            raise ConfigurationError(f"Model file has an unexpected size: {path}")
        if _sha256(path) != artifact.sha256:
            raise ConfigurationError(
                f"Model file does not match revision {ARTIFACT_REVISION[:12]}: {path}"
            )
        results.append({"file": artifact.name, "bytes": artifact.size, "sha256": artifact.sha256})
    return results


def fetch_artifacts(
    model_dir: Path,
    *,
    artifacts: Sequence[Artifact] | None = None,
    base_url: str = f"https://huggingface.co/{ARTIFACT_REPOSITORY}/resolve/{ARTIFACT_REVISION}",
    progress: Callable[[str, int, int], None] | None = None,
) -> list[dict]:
    """Download the pinned files only when explicitly requested; never sends photos."""
    model_dir.mkdir(parents=True, exist_ok=True)
    opener = build_opener()
    results = []
    for artifact in ARTIFACTS if artifacts is None else artifacts:
        target = model_dir / artifact.name
        if (
            target.is_file()
            and target.stat().st_size == artifact.size
            and _sha256(target) == artifact.sha256
        ):
            results.append({"file": artifact.name, "downloaded": False})
            continue
        partial = target.with_name(target.name + ".partial")
        digest = hashlib.sha256()
        received = 0
        try:
            with (
                opener.open(f"{base_url}/{artifact.name}", timeout=60) as response,
                partial.open("wb") as output,
            ):
                while chunk := response.read(1024 * 1024):
                    received += len(chunk)
                    if received > artifact.size:
                        raise ConfigurationError(f"{artifact.name} is larger than expected")
                    digest.update(chunk)
                    output.write(chunk)
                    if progress is not None:
                        progress(artifact.name, received, artifact.size)
                output.flush()
                os.fsync(output.fileno())
            if received != artifact.size or digest.hexdigest() != artifact.sha256:
                raise ConfigurationError(f"{artifact.name} did not match its pinned checksum")
            os.replace(partial, target)
        except (OSError, URLError) as error:
            raise ConfigurationError(f"Downloading {artifact.name} failed: {error}") from error
        finally:
            partial.unlink(missing_ok=True)
        results.append({"file": artifact.name, "downloaded": True})
    return results


class EmbeddingGemmaRuntime:
    """Starts one llama-server child on demand and stops it when idle or closed.

    The child runs under a small guard process holding a pipe from this process, so the
    model is unloaded even if the owning service exits without cleanup.
    """

    def __init__(
        self,
        embeddings_root: Path,
        *,
        idle_seconds: float = 900,
        startup_timeout: float = 180,
        request_timeout: float = 120,
        config: RuntimeConfig | None = None,
    ) -> None:
        self.embeddings_root = embeddings_root
        self._config = config  # an explicit candidate, used before it is saved
        self.idle_seconds = idle_seconds
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state = {"state": "stopped", "detail": None, "error": None, "started_at": None}
        self._process: subprocess.Popen | None = None
        self._port = 0
        self._token = ""
        self._log: collections.deque[str] = collections.deque(maxlen=200)
        self._last_used = 0.0
        self._closed = False
        self._opener = build_opener(ProxyHandler({}))
        self.requests = 0

    def status(self) -> dict:
        with self._state_lock:
            return dict(self._state)

    def embed(self, item: str | dict) -> list[float]:
        with self._lock:
            if self._closed:
                raise EmbeddingServiceError("The local encoder is shutting down")
            self._ensure_started()
            try:
                return self._post(item)
            finally:
                self._last_used = time.monotonic()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop()

    def _set(self, **values: Any) -> None:
        with self._state_lock:
            self._state.update(values)

    def _ensure_started(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if self._process is not None:
            self._stop(error="The local encoder stopped unexpectedly. It will restart on demand")
        try:
            self._set(state="starting", detail="Checking model files", error=None)
            config = self._config or load_runtime_config(self.embeddings_root)
            if config is None:
                raise ConfigurationError(
                    "Set up the local encoder first with `latent local-encoder setup`"
                )
            check_files(config)
            verify_artifacts(config.model_dir)
            self._set(detail="Loading EmbeddingGemma 2")
            self._spawn(config)
            self._wait_until_healthy()
            self._set(detail="Checking encoder output")
            _check_vector(self._post(QUERY_PREFIX + "latent runtime check"), strict_norm=True)
            _check_vector(self._post(_image_item(_probe_jpeg())), strict_norm=True)
        except Exception as error:
            message = str(error) if isinstance(error, ConfigurationError) else (
                f"The local encoder could not start: {error}"
            )
            self._stop(error=message)
            raise EmbeddingServiceError(message) from error
        self._last_used = time.monotonic()
        self._set(state="ready", detail=None, error=None,
                  started_at=datetime.now(UTC).isoformat())
        threading.Thread(
            target=self._idle_monitor, args=(self._process,), name="latent-local-encoder-idle",
            daemon=True,
        ).start()

    def _spawn(self, config: RuntimeConfig) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self._port = probe.getsockname()[1]
        self._token = secrets.token_hex(16)
        command = [
            str(config.llama_server), "-m", str(config.model), "--mmproj", str(config.projector),
            "--host", "127.0.0.1", "--port", str(self._port), "--api-key", self._token,
            *_SERVER_ARGUMENTS,
        ]
        self._log.clear()
        self._process = subprocess.Popen(
            [sys.executable, "-m", "latent.embeddinggemma", "guard", "--", *command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        process = self._process
        threading.Thread(
            target=self._read_log, args=(process,), name="latent-local-encoder-log", daemon=True
        ).start()

    def _read_log(self, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            self._log.append(line.decode(errors="replace").rstrip())

    def _wait_until_healthy(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._process is None or self._process.poll() is not None:
                raise ConfigurationError(self._failure_summary())
            try:
                request = Request(f"http://127.0.0.1:{self._port}/health")
                with self._opener.open(request, timeout=2) as response:
                    if response.status == 200:
                        return
            except HTTPError as error:
                error.close()  # 503 while the model is loading
            except (URLError, OSError):
                pass
            time.sleep(0.25)
        raise ConfigurationError("The local encoder did not become ready in time")

    def _failure_summary(self) -> str:
        time.sleep(0.2)  # let the log reader drain the last lines
        text = "\n".join(self._log).lower()
        markers = ("unknown model architecture", "unknown projector", "failed to load mmproj",
                   "failed to load clip")
        if any(marker in text for marker in markers):
            return (
                "This llama.cpp build cannot load EmbeddingGemma 2. Use llama-server built "
                f"from commit {LLAMA_CPP_MINIMUM_COMMIT[:12]} or later"
            )
        tail = next((line for line in reversed(self._log) if line.strip()), "no output")
        return f"llama-server exited during startup: {tail[-300:]}"

    def _post(self, item: str | dict) -> list[float]:
        body = json.dumps({"input": [item], "encoding_format": "float"}).encode()
        request = Request(
            f"http://127.0.0.1:{self._port}/v1/embeddings",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._token}"},
            method="POST",
        )
        self.requests += 1
        try:
            with self._opener.open(request, timeout=self.request_timeout) as response:
                data = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            detail = error.read(2000).decode(errors="replace")
            error.close()
            raise EmbeddingServiceError(
                f"The local encoder rejected the request (HTTP {error.code}): {detail[:300]}"
            ) from None
        except (URLError, OSError) as error:
            raise EmbeddingServiceError(
                "The local encoder stopped responding. Unfinished jobs remain pending"
            ) from error
        if len(data) > _MAX_RESPONSE_BYTES:
            raise EmbeddingServiceError("The local encoder response exceeded its size limit")
        try:
            vector = json.loads(data)["data"][0]["embedding"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise EmbeddingServiceError("The local encoder returned an invalid response") from error
        return _check_vector(vector)

    def _idle_monitor(self, process: subprocess.Popen) -> None:
        while process.poll() is None:
            time.sleep(min(30, max(self.idle_seconds / 4, 0.05)))
            if time.monotonic() - self._last_used < self.idle_seconds:
                continue
            # A busy request holds the lock; never stop the model underneath it.
            if self._lock.acquire(blocking=False):
                try:
                    if self._process is process and process.poll() is None:
                        self._stop()
                finally:
                    self._lock.release()
                return

    def _stop(self, *, error: str | None = None) -> None:
        process, self._process = self._process, None
        if process is not None:
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()  # the guard stops llama-server on EOF
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        self._set(state="error" if error else "stopped", detail=None, error=error,
                  started_at=None)


class EmbeddingGemmaEncoder:
    """Image, text, and image-query embeddings that never leave this Mac."""

    model_id = EMBEDDINGGEMMA_MODEL
    image_text_queries = False

    def __init__(self, runtime: EmbeddingGemmaRuntime) -> None:
        self.runtime = runtime
        self._lock = threading.Lock()
        self._query_cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._image_query_cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self.images_encoded = 0
        self.image_bytes = 0

    def encode_images(self, paths: Sequence[Path], batch_size: int) -> list[list[float]]:
        if batch_size < 1:
            raise ValueError("batch size must be positive")
        vectors = []
        for path in paths:
            if path.stat().st_size > _MAX_IMAGE_BYTES:
                raise ValueError("Local encoding accepts contact previews up to 1 MiB only")
            data = path.read_bytes()
            _validate_jpeg(data)
            vectors.append(self.runtime.embed(_image_item(data)))
            self.images_encoded += 1
            self.image_bytes += len(data)
        return vectors

    def encode_texts(self, texts: Sequence[str]) -> list[list[float]]:
        with self._lock:
            results = []
            for text in texts:
                text = text.strip()
                if not text or len(text) > 200:
                    raise ValueError("Search queries must contain 1 to 200 characters")
                if text not in self._query_cache:
                    self._query_cache[text] = tuple(self.runtime.embed(QUERY_PREFIX + text))
                    if len(self._query_cache) > 128:
                        self._query_cache.popitem(last=False)
                self._query_cache.move_to_end(text)
                results.append(list(self._query_cache[text]))
            return results

    def encode_image_query(self, data: bytes, query: str = "") -> list[float]:
        if not isinstance(query, str) or len(query.strip()) > 200:
            raise ValueError("image search text must be at most 200 characters")
        if query.strip():
            # Interleaved image and text input has not been validated for retrieval yet.
            raise ValueError(
                "Adding words to a reference image is not available with the local encoder yet"
            )
        if not data or len(data) > _MAX_IMAGE_BYTES:
            raise ValueError("image search requires a JPEG preview up to 1 MiB")
        _validate_jpeg(data, label="image search")
        key = hashlib.sha256(data).hexdigest()
        with self._lock:
            if key not in self._image_query_cache:
                self._image_query_cache[key] = tuple(self.runtime.embed(_image_item(data)))
                if len(self._image_query_cache) > 32:
                    self._image_query_cache.popitem(last=False)
            self._image_query_cache.move_to_end(key)
            return list(self._image_query_cache[key])

    def usage(self) -> dict[str, int]:
        return {"images_encoded": self.images_encoded, "image_bytes": self.image_bytes,
                "local_requests": self.runtime.requests}


def _image_item(data: bytes) -> dict:
    url = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
    return {"content": [{"type": "image_url", "image_url": {"url": url}}]}


def _validate_jpeg(data: bytes, *, label: str = "Local encoding") -> None:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG" or max(image.size) > 512:
                raise ValueError(f"{label} requires a JPEG preview up to 512 px")
            image.verify()
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError(f"{label} requires a readable JPEG preview") from error


def _check_vector(values: object, *, strict_norm: bool = False) -> list[float]:
    try:
        vector = [float(value) for value in values]  # type: ignore[union-attr]
    except (TypeError, ValueError) as error:
        raise EmbeddingServiceError("The local encoder returned a non-numeric vector") from error
    if len(vector) != EMBEDDINGGEMMA_DIMENSIONS:
        raise EmbeddingServiceError(
            f"The local encoder returned {len(vector)} dimensions, "
            f"expected {EMBEDDINGGEMMA_DIMENSIONS}"
        )
    if not all(math.isfinite(value) for value in vector):
        raise EmbeddingServiceError("The local encoder returned a non-finite vector")
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        raise EmbeddingServiceError("The local encoder returned a zero-length vector")
    if strict_norm and not 0.99 < norm < 1.01:
        raise EmbeddingServiceError(
            "The local encoder is not configured for normalized mean-pooled embeddings"
        )
    return [value / norm for value in vector]


def _probe_jpeg() -> bytes:
    image = Image.new("RGB", (64, 48))
    image.putdata([(x * 4, y * 5, 128) for y in range(48) for x in range(64)])
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _guard(command: list[str]) -> int:
    """Run llama-server until this process's stdin closes, then stop it."""
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL)

    def stop(*_: object) -> None:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()

    def watch_owner() -> None:
        # Unbuffered reads keep interpreter shutdown free of stdin lock contention.
        while os.read(0, 4096):
            pass
        stop()

    signal.signal(signal.SIGTERM, lambda *_: (stop(), sys.exit(0)))
    threading.Thread(target=watch_owner, daemon=True).start()
    return child.wait()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1:3] == ["guard", "--"]:
        raise SystemExit(_guard(sys.argv[3:]))
    raise SystemExit("usage: python -m latent.embeddinggemma guard -- COMMAND")
