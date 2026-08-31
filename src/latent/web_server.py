"""Local-index-only HTTP shell for the Phase 1 contact sheet."""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .errors import ConfigurationError
from .storage import StateStore

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STATIC_FILES = {"/": "index.html", "/app.css": "app.css", "/app.js": "app.js"}


class LibraryServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state_dir: Path) -> None:
        self.state_dir = state_dir.expanduser().resolve()
        self.cache_root = (self.state_dir / "cache").resolve()
        super().__init__(address, LibraryRequestHandler)


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
            elif parsed.path.startswith("/media/"):
                self._serve_media(parsed.path.removeprefix("/media/"))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
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
        self._send_json(
            {
                "dates": dates,
                "cached_assets": sum(int(item["asset_count"]) for item in dates),
                "preview_jobs": jobs,
                "cloud_access": False,
            }
        )

    def _serve_assets(self, query: dict[str, list[str]]) -> None:
        capture_date = _single(query, "date")
        if capture_date is not None and _DATE_PATTERN.fullmatch(capture_date) is None:
            raise ValueError("date must use YYYY-MM-DD")
        limit = _bounded_int(_single(query, "limit"), default=250, minimum=1, maximum=500)
        offset = _bounded_int(_single(query, "offset"), default=0, minimum=0, maximum=100_000)
        with StateStore(self.server.state_dir) as store:
            assets = store.library_assets(
                capture_date=capture_date,
                limit=limit,
                offset=offset,
            )
        payload = [self._asset_payload(asset) for asset in assets]
        self._send_json(
            {
                "date": capture_date,
                "limit": limit,
                "offset": offset,
                "assets": payload,
                "cloud_access": False,
            }
        )

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
) -> None:
    if not allow_remote and not _is_loopback(host):
        raise ConfigurationError("refusing a non-loopback bind without --allow-remote")
    server = LibraryServer((host, port), state_dir)
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
