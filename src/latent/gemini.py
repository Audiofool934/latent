"""Gemini image and query embeddings; credentials and payloads stay out of logs."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
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
        return bool(
            (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
        )

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
        api_key = (
            os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
        ).strip()
        if not api_key:
            raise EmbeddingServiceError(
                "Set GEMINI_API_KEY (or GOOGLE_API_KEY) to use Gemini embeddings"
            )
        body = json.dumps({"requests": requests}, separators=(",", ":")).encode()
        if len(body) > _MAX_BODY_BYTES:
            raise ValueError("Gemini request exceeds 20 MiB; reduce --batch-size")
        payload = None
        for attempt in range(self.max_retries + 1):
            request = Request(
                _ENDPOINT + ":batchEmbedContents",
                data=body,
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
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
