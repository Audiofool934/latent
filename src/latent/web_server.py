"""Archive-read-only HTTP shell for Library discovery and local workspace edits."""

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

from .curator import build_grounded_curator_report, build_grounded_motif_report
from .errors import ConfigurationError
from .search import (
    DEFAULT_SIGLIP2_MODEL_CACHE,
    LazySigLIP2TextEncoder,
    SemanticSearch,
    VectorIndex,
)
from .storage import StateStore
from .workspace import DEFAULT_WORKSPACE_DIR, WorkspaceAsset, WorkspaceStore

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_SEARCH_LENGTH = 200
_STATIC_FILES = {"/": "index.html", "/app.css": "app.css", "/app.js": "app.js"}
_SIMILAR_PATH = re.compile(r"^/api/assets/(\d+)/similar$")
_CURATOR_PATH = re.compile(r"^/api/assets/(\d+)/curator$")
_SEQUENCE_PATH = re.compile(r"^/api/sequences/([^/]+)$")
_SEQUENCE_ITEMS_PATH = re.compile(r"^/api/sequences/([^/]+)/items$")
_SEQUENCE_ORDER_PATH = re.compile(r"^/api/sequences/([^/]+)/items/order$")
_SEQUENCE_ITEM_PATH = re.compile(r"^/api/sequences/([^/]+)/items/([^/]+)$")
_MAX_JSON_BODY_BYTES = 64 * 1024


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
        workspace_dir: Path | None = None,
        model_cache: Path = DEFAULT_SIGLIP2_MODEL_CACHE,
        semantic_device: str = "auto",
    ) -> None:
        self.state_dir = state_dir.expanduser().resolve()
        self.cache_root = (self.state_dir / "cache").resolve()
        self.workspace_dir = (
            workspace_dir.expanduser().resolve()
            if workspace_dir is not None
            else (self.state_dir.parent / "workspace").resolve()
        )
        self.workspace_writes_enabled = _is_loopback(address[0])
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
            elif parsed.path == "/api/curator/motifs":
                self._serve_curator_motifs(parse_qs(parsed.query))
            elif parsed.path == "/api/sequences":
                self._serve_sequences()
            elif match := _SEQUENCE_PATH.fullmatch(parsed.path):
                self._serve_sequence(unquote(match.group(1)))
            elif parsed.path.startswith("/media/"):
                self._serve_media(parsed.path.removeprefix("/media/"))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConfigurationError,
            KeyError,
            OSError,
            ValueError,
        ) as error:
            self._handle_known_error(error)

    def do_POST(self) -> None:
        self._handle_workspace_mutation("POST")

    def do_PATCH(self) -> None:
        self._handle_workspace_mutation("PATCH")

    def do_PUT(self) -> None:
        self._handle_workspace_mutation("PUT")

    def do_DELETE(self) -> None:
        self._handle_workspace_mutation("DELETE")

    def _handle_workspace_mutation(self, method: str) -> None:
        parsed = urlsplit(self.path)
        try:
            if not self.server.workspace_writes_enabled:
                raise PermissionError("workspace mutations are disabled on non-loopback binds")
            if method == "POST" and parsed.path == "/api/sequences":
                self._create_sequence(self._read_json_body())
            elif method == "POST" and (match := _SEQUENCE_ITEMS_PATH.fullmatch(parsed.path)):
                self._add_sequence_items(unquote(match.group(1)), self._read_json_body())
            elif method == "PATCH" and (match := _SEQUENCE_PATH.fullmatch(parsed.path)):
                self._update_sequence(unquote(match.group(1)), self._read_json_body())
            elif method == "PUT" and (match := _SEQUENCE_ORDER_PATH.fullmatch(parsed.path)):
                self._reorder_sequence(unquote(match.group(1)), self._read_json_body())
            elif method == "DELETE" and (match := _SEQUENCE_ITEM_PATH.fullmatch(parsed.path)):
                self._remove_sequence_item(
                    unquote(match.group(1)),
                    unquote(match.group(2)),
                )
            elif method == "DELETE" and (match := _SEQUENCE_PATH.fullmatch(parsed.path)):
                self._delete_sequence(unquote(match.group(1)))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConfigurationError,
            KeyError,
            OSError,
            ValueError,
        ) as error:
            self._handle_known_error(error)

    def _handle_known_error(self, error: Exception) -> None:
        if isinstance(error, BrokenPipeError | ConnectionResetError):
            return
        if isinstance(error, PermissionError):
            status = HTTPStatus.FORBIDDEN
        elif isinstance(error, ConfigurationError):
            status = HTTPStatus.SERVICE_UNAVAILABLE
        elif isinstance(error, KeyError):
            status = HTTPStatus.NOT_FOUND
        else:
            status = HTTPStatus.BAD_REQUEST
        self._send_json(
            {"error": type(error).__name__, "message": str(error)},
            status=status,
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
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            workspace_status = workspace.status()
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
                "workspace": {
                    "sequences": workspace_status["sequences"],
                    "items": workspace_status["items"],
                    "writable": self.server.workspace_writes_enabled,
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

    def _serve_curator_motifs(self, query: dict[str, list[str]]) -> None:
        cluster_count = _bounded_int(
            _single(query, "clusters"),
            default=12,
            minimum=1,
            maximum=50,
        )
        sequence_limit = _bounded_int(
            _single(query, "limit"),
            default=8,
            minimum=1,
            maximum=24,
        )
        if self.server.vector_index.size == 0:
            raise ConfigurationError("curator index has no completed vectors")
        clusters = self.server.vector_index.motif_clusters(cluster_count=cluster_count)
        all_asset_ids = [asset_id for cluster in clusters for asset_id in cluster.member_asset_ids]
        assets = self._asset_payloads_by_ids(all_asset_ids)
        by_id = {int(asset["id"]): asset for asset in assets}
        motifs = []
        for cluster in clusters:
            member_scores = dict(
                zip(
                    cluster.member_asset_ids,
                    cluster.centroid_similarities,
                    strict=True,
                )
            )
            members = [
                by_id[asset_id] for asset_id in cluster.member_asset_ids if asset_id in by_id
            ]
            if not members:
                continue
            representative_asset_id = next(
                asset_id for asset_id in cluster.member_asset_ids if asset_id in by_id
            )
            available_scores = {
                int(member["id"]): member_scores[int(member["id"])] for member in members
            }
            report = build_grounded_motif_report(
                members,
                representative_asset_id=representative_asset_id,
                centroid_similarities=available_scores,
                sequence_limit=sequence_limit,
            )
            seed_ids = [item["asset_id"] for item in report["sequence_seed"]["items"]]
            seed_assets = []
            for rank, asset_id in enumerate(seed_ids, start=1):
                seed_assets.append(
                    {
                        **by_id[asset_id],
                        "rank": rank,
                        "similarity": available_scores[asset_id],
                    }
                )
            motifs.append(
                {
                    "id": f"motif-{representative_asset_id}",
                    "representative_asset_id": representative_asset_id,
                    "cohesion": cluster.cohesion,
                    "report": report,
                    "assets": seed_assets,
                }
            )
        motifs.sort(
            key=lambda motif: (
                not motif["report"]["cross_year"],
                -len(motif["report"]["years"]),
                -motif["report"]["member_count"],
                -motif["cohesion"],
                motif["representative_asset_id"],
            )
        )
        for rank, motif in enumerate(motifs, start=1):
            motif["rank"] = rank
        self._send_json(
            {
                "mode": "grounded_motif_clusters",
                "indexed_assets": self.server.vector_index.size,
                "cluster_count": len(motifs),
                "motifs": motifs,
                "basis": {
                    **self._relation_basis(),
                    "algorithm": "deterministic_spherical_kmeans",
                    "labeling": "unlabeled",
                },
                "cloud_access": False,
                "archive_modified": False,
            }
        )

    def _serve_sequences(self) -> None:
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            sequences = workspace.list_sequences()
            status = workspace.status()
        self._send_json(
            {
                "sequences": sequences,
                "workspace": {
                    "database_integrity": status["database_integrity"],
                    "items": status["items"],
                },
                "archive_modified": False,
            }
        )

    def _serve_sequence(self, sequence_id: str) -> None:
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            sequence = workspace.get_sequence(sequence_id)
        self._send_json(
            {
                "sequence": self._hydrate_sequence(sequence),
                "archive_modified": False,
            }
        )

    def _create_sequence(self, payload: dict[str, Any]) -> None:
        _validate_json_keys(payload, allowed={"name", "note"}, required={"name"})
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            sequence = workspace.create_sequence(
                payload["name"],
                note=payload.get("note", ""),
                origin="manual",
            )
        self._send_json(
            {
                "sequence": self._hydrate_sequence(sequence),
                "archive_modified": False,
            },
            status=HTTPStatus.CREATED,
        )

    def _update_sequence(self, sequence_id: str, payload: dict[str, Any]) -> None:
        _validate_json_keys(payload, allowed={"name", "note"})
        if not payload:
            raise ValueError("sequence update must include name or note")
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            sequence = workspace.update_sequence(
                sequence_id,
                name=payload.get("name"),
                note=payload.get("note"),
            )
        self._send_json(
            {
                "sequence": self._hydrate_sequence(sequence),
                "archive_modified": False,
            }
        )

    def _add_sequence_items(self, sequence_id: str, payload: dict[str, Any]) -> None:
        _validate_json_keys(payload, allowed={"asset_ids"}, required={"asset_ids"})
        asset_ids = _integer_list(payload["asset_ids"], "asset_ids", maximum=100)
        with StateStore(self.server.state_dir) as store:
            records = store.library_assets_by_ids(asset_ids)
        if len(records) != len(asset_ids):
            found_ids = {int(record["id"]) for record in records}
            missing = [asset_id for asset_id in asset_ids if asset_id not in found_ids]
            raise KeyError(f"local library assets were not found: {missing}")
        assets = [
            WorkspaceAsset(
                provider=str(record["provider"]),
                remote_path=str(record["remote_path"]),
                fingerprint=str(record["fingerprint"]),
                name=str(record["name"]),
                capture_at=record["capture_at"],
                camera_model=record["camera_model"],
                lens_model=record["lens_model"],
            )
            for record in records
        ]
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            outcome = workspace.add_items(sequence_id, assets)
            sequence = workspace.get_sequence(sequence_id)
        self._send_json(
            {
                "added": outcome.added,
                "skipped": outcome.skipped,
                "sequence": self._hydrate_sequence(sequence),
                "archive_modified": False,
            }
        )

    def _reorder_sequence(self, sequence_id: str, payload: dict[str, Any]) -> None:
        _validate_json_keys(payload, allowed={"item_ids"}, required={"item_ids"})
        item_ids = _string_list(payload["item_ids"], "item_ids", maximum=500)
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            sequence = workspace.reorder_items(sequence_id, item_ids)
        self._send_json(
            {
                "sequence": self._hydrate_sequence(sequence),
                "archive_modified": False,
            }
        )

    def _remove_sequence_item(self, sequence_id: str, item_id: str) -> None:
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            removed = workspace.remove_item(sequence_id, item_id)
            if not removed:
                raise KeyError(f"sequence item {item_id!r} was not found")
            sequence = workspace.get_sequence(sequence_id)
        self._send_json(
            {
                "sequence": self._hydrate_sequence(sequence),
                "archive_modified": False,
            }
        )

    def _delete_sequence(self, sequence_id: str) -> None:
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            deleted = workspace.delete_sequence(sequence_id)
        if not deleted:
            raise KeyError(f"sequence {sequence_id!r} was not found")
        self._send_json({"deleted": True, "archive_modified": False})

    def _hydrate_sequence(self, sequence: dict[str, Any]) -> dict[str, Any]:
        items = list(sequence["items"])
        identities = [(str(item["provider"]), str(item["remote_path"])) for item in items]
        current_assets = []
        with StateStore(self.server.state_dir) as store:
            for start in range(0, len(identities), 500):
                current_assets.extend(
                    store.library_assets_by_identities(identities[start : start + 500])
                )
        by_identity = {
            (str(asset["provider"]), str(asset["remote_path"])): asset for asset in current_assets
        }
        hydrated_items = []
        for item in items:
            identity = (str(item["provider"]), str(item["remote_path"]))
            current = by_identity.get(identity)
            hydrated_items.append(
                {
                    **item,
                    "library_status": (
                        "missing"
                        if current is None
                        else "current"
                        if str(current["fingerprint"]) == str(item["fingerprint"])
                        else "changed"
                    ),
                    "asset": self._asset_payload(current) if current is not None else None,
                }
            )
        return {**sequence, "items": hydrated_items, "item_count": len(hydrated_items)}

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
        assets = []
        with StateStore(self.server.state_dir) as store:
            for start in range(0, len(asset_ids), 500):
                assets.extend(store.library_assets_by_ids(asset_ids[start : start + 500]))
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

    def _read_json_body(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("workspace mutations require application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("workspace mutations require Content-Length")
        length = int(raw_length)
        if length < 1 or length > _MAX_JSON_BODY_BYTES:
            raise ValueError(
                f"workspace JSON body must be between 1 and {_MAX_JSON_BODY_BYTES} bytes"
            )
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("workspace JSON body must be an object")
        return payload

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
    workspace_dir: Path = DEFAULT_WORKSPACE_DIR,
    model_cache: Path = DEFAULT_SIGLIP2_MODEL_CACHE,
    semantic_device: str = "auto",
) -> None:
    if not allow_remote and not _is_loopback(host):
        raise ConfigurationError("refusing a non-loopback bind without --allow-remote")
    server = LibraryServer(
        (host, port),
        state_dir,
        embedding_dir=embedding_dir,
        workspace_dir=workspace_dir,
        model_cache=model_cache,
        semantic_device=semantic_device,
    )
    actual_host, actual_port = server.server_address[:2]
    print(f"Latent contact sheet: http://{actual_host}:{actual_port}", flush=True)
    print("CloudDrive access: disabled in this server", flush=True)
    print(
        f"Workspace writes: {'enabled' if server.workspace_writes_enabled else 'disabled'}",
        flush=True,
    )
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


def _validate_json_keys(
    payload: dict[str, Any],
    *,
    allowed: set[str],
    required: set[str] = frozenset(),
) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"unknown workspace fields: {sorted(unknown)}")
    missing = required - set(payload)
    if missing:
        raise ValueError(f"missing workspace fields: {sorted(missing)}")


def _integer_list(value: Any, field: str, *, maximum: int) -> list[int]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list")
    if len(value) > maximum:
        raise ValueError(f"{field} is limited to {maximum} values")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ValueError(f"{field} must contain integers")
    if len(value) != len(set(value)):
        raise ValueError(f"{field} values must be unique")
    return value


def _string_list(value: Any, field: str, *, maximum: int) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    if len(value) > maximum:
        raise ValueError(f"{field} is limited to {maximum} values")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{field} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{field} values must be unique")
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
