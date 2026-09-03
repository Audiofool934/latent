"""Local-index-only HTTP shell for the Phase 1 contact sheet."""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .curator import build_grounded_curator_report
from .errors import ConfigurationError
from .search import (
    DEFAULT_SIGLIP2_MODEL_CACHE,
    LazySigLIP2TextEncoder,
    SemanticSearch,
    VectorIndex,
)
from .storage import StateStore

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_SEARCH_LENGTH = 200
_STATIC_FILES = {"/": "index.html", "/app.css": "app.css", "/app.js": "app.js"}
_SIMILAR_PATH = re.compile(r"^/api/assets/(\d+)/similar$")
_CURATOR_PATH = re.compile(r"^/api/assets/(\d+)/curator$")


class LibraryServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        state_dir: Path,
        *,
        vector_index: VectorIndex | None = None,
        semantic_search: SemanticSearch | None = None,
        embedding_dir: Path | None = None,
        model_cache: Path = DEFAULT_SIGLIP2_MODEL_CACHE,
        semantic_device: str = "auto",
    ) -> None:
        self.state_dir = state_dir.expanduser().resolve()
        self.cache_root = (self.state_dir / "cache").resolve()
        resolved_embedding_dir = (
            embedding_dir.expanduser().resolve()
            if embedding_dir is not None
            else self.state_dir / "embeddings/siglip2-base"
        )
        self.vector_index = vector_index or VectorIndex(resolved_embedding_dir)
        self._semantic_search = semantic_search
        self._semantic_lock = threading.Lock()
        self.model_cache = model_cache.expanduser().resolve()
        self.semantic_device = semantic_device
        super().__init__(address, LibraryRequestHandler)

    def get_semantic_search(self) -> SemanticSearch:
        if self._semantic_search is not None:
            return self._semantic_search
        with self._semantic_lock:
            if self._semantic_search is None:
                self._semantic_search = SemanticSearch(
                    self.vector_index,
                    LazySigLIP2TextEncoder(
                        self.model_cache,
                        device=self.semantic_device,
                    ),
                )
        return self._semantic_search


class LibraryRequestHandler(BaseHTTPRequestHandler):
    server: LibraryServer

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        try:
            if parsed.path in _STATIC_FILES:
                self._serve_static(_STATIC_FILES[parsed.path])
            elif parsed.path == "/health":
                self._send_json({"status": "ok", "cloud_access": False})
            elif parsed.path == "/api/library":
                self._serve_library()
            elif parsed.path == "/api/assets":
                self._serve_assets(parse_qs(parsed.query))
            elif parsed.path == "/api/search":
                self._serve_semantic_search(parse_qs(parsed.query))
            elif match := _SIMILAR_PATH.fullmatch(parsed.path):
                self._serve_similar(int(match.group(1)), parse_qs(parsed.query))
            elif match := _CURATOR_PATH.fullmatch(parsed.path):
                self._serve_curator(int(match.group(1)), parse_qs(parsed.query))
            elif parsed.path.startswith("/media/"):
                self._serve_media(parsed.path.removeprefix("/media/"))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            return
        except ConfigurationError as error:
            self._send_json(
                {"error": type(error).__name__, "message": str(error)},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
        except KeyError as error:
            self._send_json(
                {"error": type(error).__name__, "message": str(error)},
                status=HTTPStatus.NOT_FOUND,
            )
        except (OSError, ValueError) as error:
            self._send_json(
                {"error": type(error).__name__, "message": str(error)},
                status=HTTPStatus.BAD_REQUEST,
            )

    def log_message(self, format: str, *args: object) -> None:
        return None

    def _serve_static(self, name: str) -> None:
        resource = files("latent").joinpath("web_assets", name)
        data = resource.read_bytes()
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self._send_bytes(data, content_type, cache_control="no-cache")

    def _serve_library(self) -> None:
        with StateStore(self.server.state_dir) as store:
            dates = store.library_dates()
            jobs = store.preview_job_counts()
        try:
            indexed_assets = self.server.vector_index.size
        except ConfigurationError:
            indexed_assets = 0
        self._send_json(
            {
                "dates": dates,
                "cached_assets": sum(int(item["asset_count"]) for item in dates),
                "preview_jobs": jobs,
                "embedding_index": {
                    "indexed_assets": indexed_assets,
                    "semantic_ready": indexed_assets > 0,
                },
                "cloud_access": False,
            }
        )

    def _serve_assets(self, query: dict[str, list[str]]) -> None:
        capture_date = _single(query, "date")
        if capture_date is not None and _DATE_PATTERN.fullmatch(capture_date) is None:
            raise ValueError("date must use YYYY-MM-DD")
        search_query = (_single(query, "q") or "").strip()
        if len(search_query) > _MAX_SEARCH_LENGTH:
            raise ValueError(f"search query must be {_MAX_SEARCH_LENGTH} characters or fewer")
        limit = _bounded_int(_single(query, "limit"), default=250, minimum=1, maximum=500)
        offset = _bounded_int(_single(query, "offset"), default=0, minimum=0, maximum=100_000)
        with StateStore(self.server.state_dir) as store:
            assets = store.library_assets(
                capture_date=capture_date,
                search_query=search_query or None,
                limit=limit,
                offset=offset,
            )
            total = store.library_asset_count(
                capture_date=capture_date,
                search_query=search_query or None,
            )
        payload = [self._asset_payload(asset) for asset in assets]
        next_offset = offset + len(payload)
        has_more = next_offset < total
        self._send_json(
            {
                "date": capture_date,
                "query": search_query,
                "limit": limit,
                "offset": offset,
                "total": total,
                "has_more": has_more,
                "next_offset": next_offset if has_more else None,
                "assets": payload,
                "cloud_access": False,
            }
        )

    def _serve_semantic_search(self, query: dict[str, list[str]]) -> None:
        search_query = (_single(query, "q") or "").strip()
        if not search_query:
            raise ValueError("semantic query cannot be empty")
        if len(search_query) > _MAX_SEARCH_LENGTH:
            raise ValueError(f"semantic query must be {_MAX_SEARCH_LENGTH} characters or fewer")
        limit = _bounded_int(_single(query, "limit"), default=50, minimum=1, maximum=100)
        if self.server.vector_index.size == 0:
            raise ConfigurationError("semantic index has no completed vectors")
        matches = self.server.get_semantic_search().search(search_query, limit=limit)
        self._send_matches(
            matches,
            {
                "query": search_query,
                "mode": "semantic",
            },
        )

    def _serve_similar(
        self,
        asset_id: int,
        query: dict[str, list[str]],
    ) -> None:
        limit = _bounded_int(_single(query, "limit"), default=50, minimum=1, maximum=100)
        if self.server.vector_index.size == 0:
            raise ConfigurationError("similarity index has no completed vectors")
        matches = self.server.vector_index.similar(asset_id, limit=limit)
        self._send_matches(
            matches,
            {
                "source_asset_id": asset_id,
                "mode": "visual_similarity",
            },
        )

    def _serve_curator(
        self,
        asset_id: int,
        query: dict[str, list[str]],
    ) -> None:
        limit = _bounded_int(_single(query, "limit"), default=12, minimum=1, maximum=50)
        if self.server.vector_index.size == 0:
            raise ConfigurationError("curator index has no completed vectors")
        matches = self.server.vector_index.similar(asset_id, limit=limit)
        source_assets = self._asset_payloads_by_ids([asset_id])
        if not source_assets:
            raise KeyError(f"asset {asset_id} is not available in the local library")
        neighbors = self._hydrate_matches(matches)
        report = build_grounded_curator_report(source_assets[0], neighbors)
        self._send_json(
            {
                "mode": "grounded_curator",
                "source": source_assets[0],
                "neighbors": neighbors,
                "report": report,
                "basis": self._relation_basis(),
                "cloud_access": False,
            }
        )

    def _send_matches(self, matches: list[Any], context: dict[str, Any]) -> None:
        results = self._hydrate_matches(matches)
        self._send_json(
            {
                **context,
                "total": len(results),
                "results": results,
                "basis": self._relation_basis(),
                "cloud_access": False,
            }
        )

    def _hydrate_matches(self, matches: list[Any]) -> list[dict[str, Any]]:
        asset_ids = [int(match.asset_id) for match in matches]
        assets = self._asset_payloads_by_ids(asset_ids)
        by_id = {int(asset["id"]): asset for asset in assets}
        results = []
        for match in matches:
            asset = by_id.get(int(match.asset_id))
            if asset is None:
                continue
            results.append(
                {
                    **asset,
                    "rank": int(match.rank),
                    "similarity": float(match.score),
                }
            )
        return results

    def _asset_payloads_by_ids(self, asset_ids: list[int]) -> list[dict[str, Any]]:
        with StateStore(self.server.state_dir) as store:
            assets = store.library_assets_by_ids(asset_ids)
        return [self._asset_payload(asset) for asset in assets]

    def _relation_basis(self) -> dict[str, str]:
        return {
            "model_id": self.server.vector_index.model_id,
            "metric": "cosine_similarity",
            "source": "local_contact_embeddings",
            "interpretation": (
                "Similarity is model evidence, not proof of place, identity, or story."
            ),
        }

    def _asset_payload(self, asset: dict[str, Any]) -> dict[str, Any]:
        preview_path = asset["preview_path"] or asset["contact_path"]
        return {
            "id": asset["id"],
            "name": asset["name"],
            "remote_path": asset["remote_path"],
            "size_bytes": asset["size_bytes"],
            "capture_at": asset["capture_at"],
            "camera_model": asset["camera_model"],
            "lens_model": asset["lens_model"],
            "preview_width": asset["preview_width"],
            "preview_height": asset["preview_height"],
            "contact_url": "/media/" + quote(str(asset["contact_path"]), safe="/"),
            "preview_url": "/media/" + quote(str(preview_path), safe="/"),
            "preview_available": asset["preview_path"] is not None,
        }

    def _serve_media(self, encoded_path: str) -> None:
        relative_path = unquote(encoded_path)
        if not relative_path or Path(relative_path).is_absolute():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        target = (self.server.cache_root / relative_path).resolve()
        if not target.is_relative_to(self.server.cache_root) or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send_bytes(
            target.read_bytes(),
            "image/jpeg",
            cache_control="public, max-age=31536000, immutable",
        )

    def _send_json(self, payload: object, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self._send_bytes(data, "application/json; charset=utf-8", status=status)

    def _send_bytes(
        self,
        data: bytes,
        content_type: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Security-Policy", _content_security_policy())
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)


def serve_library(
    state_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_remote: bool = False,
    embedding_dir: Path | None = None,
    model_cache: Path = DEFAULT_SIGLIP2_MODEL_CACHE,
    semantic_device: str = "auto",
) -> None:
    if not allow_remote and not _is_loopback(host):
        raise ConfigurationError("refusing a non-loopback bind without --allow-remote")
    server = LibraryServer(
        (host, port),
        state_dir,
        embedding_dir=embedding_dir,
        model_cache=model_cache,
        semantic_device=semantic_device,
    )
    actual_host, actual_port = server.server_address[:2]
    print(f"Latent contact sheet: http://{actual_host}:{actual_port}", flush=True)
    print("CloudDrive access: disabled in this server", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


def _single(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _bounded_int(
    raw: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = default if raw is None else int(raw)
    if value < minimum or value > maximum:
        raise ValueError(f"value must be between {minimum} and {maximum}")
    return value


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _content_security_policy() -> str:
    return (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self'; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    )
