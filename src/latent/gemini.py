"""Gemini image and query embeddings; credentials and payloads stay out of logs."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image

from .errors import EmbeddingServiceError

GEMINI_MODEL = "gemini-embedding-2"
GEMINI_DIMENSIONS = 3072
GEMINI_STORE_NAME = "gemini-embedding-2"
GEMINI_IMAGE_ESTIMATE_USD = 0.00012
MAX_BATCH_SIZE = 64
_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}"
_MAX_IMAGE_BYTES = 1024 * 1024
_MAX_BODY_BYTES = 20 * 1024 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# Apps opened from the Dock do not inherit a shell environment, so the key can also live in
# the login Keychain. Only /usr/bin/security creates and reads the item, which keeps its
# access list on that one tool and avoids permission prompts for the service.
KEYCHAIN_SERVICE = "Latent Gemini API key"
KEYCHAIN_ACCOUNT = "gemini"
_SECURITY = "/usr/bin/security"
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{20,200}$")
_KEYCHAIN_TTL_SECONDS = 60
_keychain_lock = threading.Lock()
_keychain_cache: dict[str, Any] = {"value": None, "checked": None}
MISSING_KEY_MESSAGE = (
    "Add a Gemini API key in AI Search, or run `latent gemini-key set`. "
    "GEMINI_API_KEY in the service environment also works"
)


def api_key() -> str | None:
    """The environment wins; otherwise use the login Keychain item, if any."""
    return _environment_key() or _keychain_key()


def credential_source() -> str | None:
    if _environment_key():
        return "environment"
    return "keychain" if _keychain_key() else None


def store_keychain_key(key: str) -> None:
    """Save the key without placing it in any process's arguments."""
    key = key.strip()
    if not _KEY_PATTERN.fullmatch(key):
        raise ValueError("That does not look like a Gemini API key")
    command = (
        f'add-generic-password -U -s "{KEYCHAIN_SERVICE}" -a "{KEYCHAIN_ACCOUNT}" '
        f'-l "{KEYCHAIN_SERVICE}" -w "{key}"\n'
    )
    # `security -i` reads commands from stdin, so the key never appears in argv.
    result = _run_security(["-i"], stdin=command)
    if result.returncode != 0 or result.stdout.strip() or result.stderr.strip():
        raise EmbeddingServiceError(
            "The login Keychain did not save the key. Unlock it and try again"
        )
    _forget_keychain_key()
    if _keychain_key() != key:
        raise EmbeddingServiceError("The login Keychain did not return the saved key")


def delete_keychain_key() -> bool:
    result = _run_security(
        ["delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT]
    )
    _forget_keychain_key()
    if result.returncode not in (0, 44):  # 44: the item does not exist
        raise EmbeddingServiceError("The login Keychain did not remove the key")
    return result.returncode == 0


def _environment_key() -> str | None:
    value = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    return value.strip() or None


def _keychain_key() -> str | None:
    with _keychain_lock:
        checked = _keychain_cache["checked"]
        if checked is not None and time.monotonic() - checked < _KEYCHAIN_TTL_SECONDS:
            return _keychain_cache["value"]
        result = _run_security(
            ["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"]
        )
        value = result.stdout.strip() if result.returncode == 0 else ""
        _keychain_cache.update(value=value or None, checked=time.monotonic())
        return _keychain_cache["value"]


def _forget_keychain_key() -> None:
    with _keychain_lock:
        _keychain_cache.update(value=None, checked=None)


def _run_security(arguments: list[str], *, stdin: str | None = None):
    try:
        return subprocess.run(
            [_SECURITY, *arguments], input=stdin, capture_output=True, text=True, timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(arguments, 1, "", "security unavailable")


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GeminiEmbeddingEncoder:
    """One image per Content, with ordered batch results and bounded retries."""

    model_id = GEMINI_MODEL

    def __init__(self, *, timeout_seconds: float = 60, max_retries: int = 3) -> None:
        if timeout_seconds <= 0 or max_retries < 0 or max_retries > 5:
            raise ValueError("invalid Gemini timeout or retry count")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._opener = build_opener(_NoRedirects())
        self._query_lock = threading.Lock()
        self._query_cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._image_query_cache: OrderedDict[tuple[str, str], tuple[float, ...]] = OrderedDict()
        self.api_requests = 0
        self.image_submissions = 0
        self.photo_upload_bytes = 0
        self.request_body_bytes = 0
        self.input_tokens = 0

    @staticmethod
    def credentials_available() -> bool:
        return api_key() is not None

    def encode_images(self, paths: Sequence[Path], batch_size: int) -> list[list[float]]:
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise ValueError(f"Gemini batch size must be between 1 and {MAX_BATCH_SIZE}")
        vectors = []
        for start in range(0, len(paths), batch_size):
            requests = []
            image_bytes = 0
            for path in paths[start : start + batch_size]:
                if path.stat().st_size > _MAX_IMAGE_BYTES:
                    raise ValueError("Gemini accepts contact previews up to 1 MiB only")
                data = path.read_bytes()
                with Image.open(path) as image:
                    if image.format != "JPEG" or max(image.size) > 512:
                        raise ValueError(
                            "Gemini uploads must be JPEG contact previews up to 512 px"
                        )
                    image.verify()
                image_bytes += len(data)
                requests.append(
                    self._request(
                        {
                            "inlineData": {
                                "mimeType": "image/jpeg",
                                "data": base64.b64encode(data).decode("ascii"),
                            }
                        }
                    )
                )
            vectors.extend(self._embed(requests, image_bytes=image_bytes))
        return vectors

    def encode_texts(self, texts: Sequence[str]) -> list[list[float]]:
        # Serialize cache misses so concurrent identical searches cause one API call.
        with self._query_lock:
            results = []
            for text in texts:
                text = text.strip()
                if text not in self._query_cache:
                    self._query_cache[text] = self._query(text)
                    if len(self._query_cache) > 128:
                        self._query_cache.popitem(last=False)
                self._query_cache.move_to_end(text)
                results.append(list(self._query_cache[text]))
            return results

    def encode_image_query(self, data: bytes, query: str = "") -> list[float]:
        if not isinstance(query, str) or len(query.strip()) > 200:
            raise ValueError("image search text must be at most 200 characters")
        query = query.strip()
        if not data or len(data) > _MAX_IMAGE_BYTES:
            raise ValueError("image search requires a JPEG preview up to 1 MiB")
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "JPEG" or max(image.size) > 512:
                    raise ValueError("image search requires a JPEG preview up to 512 px")
                image.verify()
        except (OSError, Image.DecompressionBombError) as error:
            raise ValueError("image search requires a readable JPEG preview") from error
        key = (hashlib.sha256(data).hexdigest(), query)
        # Retain vectors only, never uploaded image bytes. Sorting reuses this embedding.
        with self._query_lock:
            if key not in self._image_query_cache:
                request = self._request({"inlineData": {
                    "mimeType": "image/jpeg", "data": base64.b64encode(data).decode("ascii"),
                }})
                if query:
                    # Multimodal contents use plain text, with no task-type prefix.
                    request["content"]["parts"].insert(0, {"text": query})
                self._image_query_cache[key] = tuple(
                    self._embed([request], image_bytes=len(data))[0]
                )
                if len(self._image_query_cache) > 32:
                    self._image_query_cache.popitem(last=False)
            self._image_query_cache.move_to_end(key)
            return list(self._image_query_cache[key])

    def _query(self, text: str) -> tuple[float, ...]:
        if not text or len(text) > 200:
            raise ValueError("Gemini search queries must contain 1 to 200 characters")
        request = self._request({"text": f"task: search result | query: {text}"})
        return tuple(self._embed([request])[0])

    @staticmethod
    def _request(part: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": f"models/{GEMINI_MODEL}",
            "content": {"parts": [part]},
            "outputDimensionality": GEMINI_DIMENSIONS,
        }

    def _embed(self, requests: list[dict[str, Any]], *, image_bytes: int = 0) -> list[list[float]]:
        key = api_key()
        if not key:
            raise EmbeddingServiceError(MISSING_KEY_MESSAGE)
        body = json.dumps({"requests": requests}, separators=(",", ":")).encode()
        if len(body) > _MAX_BODY_BYTES:
            raise ValueError("Gemini request exceeds 20 MiB; reduce --batch-size")
        payload = None
        for attempt in range(self.max_retries + 1):
            request = Request(
                _ENDPOINT + ":batchEmbedContents",
                data=body,
                headers={"Content-Type": "application/json", "x-goog-api-key": key},
                method="POST",
            )
            self.api_requests += 1
            self.image_submissions += len(requests) if image_bytes else 0
            self.photo_upload_bytes += image_bytes
            self.request_body_bytes += len(body)
            try:
                with self._opener.open(request, timeout=self.timeout_seconds) as response:
                    data = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(data) > _MAX_RESPONSE_BYTES:
                    raise EmbeddingServiceError(
                        "Gemini response exceeded the configured size limit"
                    )
                payload = json.loads(data)
                break
            except HTTPError as error:
                code = error.code
                retry_after = error.headers.get("Retry-After", "") if error.headers else ""
                quota_summary = ""
                try:
                    details = json.loads(error.read(65536)).get("error", {}).get("details", [])
                    for detail in details:
                        delay = detail.get("retryDelay", "")
                        if isinstance(delay, str) and delay.endswith("s") and not retry_after:
                            retry_after = delay[:-1]
                        for violation in detail.get("violations", []):
                            metric = str(violation.get("quotaMetric", "")).lower()
                            quota = str(violation.get("quotaId", "")).lower()
                            value = str(violation.get("quotaValue", ""))
                            if value.isdecimal():
                                period = "daily" if "perday" in quota else "per-minute"
                                unit = "tokens" if "token" in metric else "requests"
                                quota_summary = f" ({period} quota: {value} {unit})"
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
                finally:
                    error.close()
                if code == 429 and not retry_after:
                    retry_after = "30"
                if code not in (408, 429, 500, 502, 503, 504) or attempt == self.max_retries:
                    raise EmbeddingServiceError(
                        f"Gemini HTTP {code}{quota_summary}; "
                        "check API access, billing, quota, or service status. "
                        "Unfinished jobs remain pending."
                    ) from None
                self._backoff(attempt, retry_after)
            except (URLError, TimeoutError, ConnectionError, OSError):
                if attempt == self.max_retries:
                    raise EmbeddingServiceError(
                        "Gemini connection failed after bounded retries; "
                        "unfinished jobs remain pending"
                    ) from None
                self._backoff(attempt, "")
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise EmbeddingServiceError("Gemini returned an invalid JSON response") from None
        try:
            values = payload["embeddings"]
            if len(values) != len(requests):
                raise ValueError
            vectors = []
            for embedding in values:
                vector = embedding["values"]
                if len(vector) != GEMINI_DIMENSIONS:
                    raise ValueError
                if any(not isinstance(v, int | float) or not math.isfinite(v) for v in vector):
                    raise ValueError
                norm = math.sqrt(sum(float(v) ** 2 for v in vector))
                if norm <= 0 or not math.isfinite(norm):
                    raise ValueError
                vectors.append([float(v) / norm for v in vector])
            self.input_tokens += int(payload.get("usageMetadata", {}).get("promptTokenCount", 0))
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            raise EmbeddingServiceError(
                "Gemini returned invalid or mismatched embedding vectors"
            ) from None
        return vectors

    @staticmethod
    def _backoff(attempt: int, retry_after: str) -> None:
        try:
            seconds = float(retry_after)
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError
        except ValueError:
            seconds = 2 ** (attempt + 1)
        if seconds > 60:
            raise EmbeddingServiceError(
                "Gemini requested a long retry delay; resume the pending queue later"
            )
        time.sleep(max(seconds, 1))

    def usage(self) -> dict[str, int | float]:
        return {
            "api_requests": self.api_requests,
            "image_submissions": self.image_submissions,
            "photo_upload_bytes": self.photo_upload_bytes,
            "request_body_bytes": self.request_body_bytes,
            "input_tokens": self.input_tokens,
            "estimated_image_cost_usd": round(
                self.image_submissions * GEMINI_IMAGE_ESTIMATE_USD, 6
            ),
        }
