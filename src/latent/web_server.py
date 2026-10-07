"""Local library discovery, durable workspace edits, and explicit cloud editing transfers."""

from __future__ import annotations

import base64
import ipaddress
import json
import math
import mimetypes
import re
import sys
import threading
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import UUID

from .curator import build_grounded_curator_report, build_grounded_motif_report
from .editing import DEFAULT_EDITING_DIR, EditingManager
from .embedding_backends import (
    EMBEDDINGGEMMA,
    EMBEDDINGGEMMA_PROFILE,
    GEMINI,
    GEMINI_PROFILE,
    BackendSelection,
    EmbeddingEngine,
    EmbeddingProfile,
    current_validation,
    engine_dirs,
    validate_index,
)
from .embedding_runs import EmbeddingRuns
from .embeddinggemma import (
    ARTIFACT_REPOSITORY,
    ARTIFACT_REVISION,
    EmbeddingGemmaEncoder,
    EmbeddingGemmaRuntime,
    check_files,
    load_runtime_config,
)
from .errors import ConfigurationError
from .filters import matching_ids, photo_filters
from .gemini import GEMINI_STORE_NAME, GeminiEmbeddingEncoder
from .imports import ImportManager
from .ingest import FolderIndexer
from .locations import LocationsStore
from .search import SemanticSearch, VectorIndex
from .search_history import SearchHistory
from .sequence_links import SequenceLinks
from .service_runtime import service_identity, workspace_service_lease
from .storage import StateStore
from .timelapse_groups import TimelapseGroups
from .trash import PhotoTrash
from .workspace import DEFAULT_WORKSPACE_DIR, WorkspaceAsset, WorkspaceStore

_DATE_PATTERN = re.compile(r"^[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?$")
_MAX_SEARCH_LENGTH = 200
_SEARCH_HISTORY_PATH = re.compile(r"^/api/search/history/([0-9a-f]{64})(?:/(image|replay))?$")
_STATIC_FILES = {"/": "index.html", "/app.css": "app.css", "/app.js": "app.js"}
_SIMILAR_PATH = re.compile(r"^/api/assets/(\d+)/similar$")
_CURATOR_PATH = re.compile(r"^/api/assets/(\d+)/curator$")
_SEQUENCE_PATH = re.compile(r"^/api/sequences/([^/]+)$")
_SEQUENCE_FOLDER_PATH = re.compile(r"^/api/sequence-folders/([^/]+)$")
_SEQUENCE_ITEMS_PATH = re.compile(r"^/api/sequences/([^/]+)/items$")
_SEQUENCE_ORDER_PATH = re.compile(r"^/api/sequences/([^/]+)/items/order$")
_SEQUENCE_ITEM_PATH = re.compile(r"^/api/sequences/([^/]+)/items/([^/]+)$")
_EDIT_BATCH_PATH = re.compile(
    r"^/api/editing/([a-f0-9-]+)(?:/(resume|prepare|finish|map|verify))?$"
)
_TRASH_PATH = re.compile(r"^/api/trash/([a-f0-9-]+)/(restore|resume)$")
_TIMELAPSE_PATH = re.compile(r"^/api/timelapse/([a-f0-9]{64})$")
_IMPORT_PATH = re.compile(
    r"^/api/imports/([a-f0-9-]+)/(start|resume|pause|destination|archive|retry_previews)$"
)
_EMBEDDING_RUN_PATH = re.compile(r"^/api/embedding-runs/([a-f0-9-]+)/(start|resume|pause)$")
_BACKEND_VALIDATE_PATH = re.compile(r"^/api/search-backends/([a-z0-9-]+)/validate$")
_MAX_JSON_BODY_BYTES = 64 * 1024


@dataclass(frozen=True)
class ActiveSearch:
    """One vector space per request: its index, saved query vectors, and encoder."""

    engine: EmbeddingEngine
    vector_index: VectorIndex
    history: SearchHistory
    semantic: SemanticSearch


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
        editing_dir: Path | None = None,
    ) -> None:
        self.state_dir = state_dir.expanduser().resolve()
        self.cache_root = (self.state_dir / "cache").resolve()
        self.workspace_dir = (
            workspace_dir.expanduser().resolve()
            if workspace_dir is not None
            else (self.state_dir.parent / "workspace").resolve()
        )
        self.workspace_writes_enabled = _is_loopback(address[0])
        gemini_dir = (
            embedding_dir.expanduser().resolve()
            if embedding_dir is not None
            else self.state_dir / "embeddings" / GEMINI_STORE_NAME
        )
        # The configured Gemini index anchors library identity, so switching engines
        # never changes data_id; the local index lives beside it.
        self.embeddings_root = gemini_dir.parent
        self.local_runtime = EmbeddingGemmaRuntime(self.embeddings_root)
        dirs = engine_dirs(gemini_dir)
        self.engines: dict[str, EmbeddingEngine] = {
            GEMINI: EmbeddingEngine(
                GEMINI_PROFILE, dirs[GEMINI], GeminiEmbeddingEncoder, _gemini_preflight
            ),
            EMBEDDINGGEMMA: EmbeddingEngine(
                EMBEDDINGGEMMA_PROFILE, dirs[EMBEDDINGGEMMA],
                lambda: EmbeddingGemmaEncoder(self.local_runtime), self._local_preflight,
            ),
        }
        self.selection: BackendSelection | None = BackendSelection(self.embeddings_root)
        self._fixed_search: tuple[VectorIndex, SemanticSearch | None] | None = None
        identity_dir = gemini_dir
        if vector_index is not None:
            # A supplied index (tests and tools) is the only engine and cannot be switched.
            fixed = EmbeddingProfile(
                key="fixed", display_name=vector_index.model_id, model_id=vector_index.model_id,
                dimensions=vector_index.dimensions, store_name=vector_index.embedding_dir.name,
                provider="fixed", metadata=vector_index.metadata,
            )
            self.engines = {
                "fixed": EmbeddingEngine(fixed, vector_index.embedding_dir, GeminiEmbeddingEncoder)
            }
            self.selection = None
            self._fixed_search = (vector_index, semantic_search)
            identity_dir = vector_index.embedding_dir
        self.service_info = service_identity(self.state_dir, identity_dir, self.workspace_dir)
        self._search_lock = threading.Lock()
        self._search: ActiveSearch | None = None
        self._search_stamp: object = object()
        self._validations: dict[str, threading.Thread] = {}
        self._validation_errors: dict[str, str] = {}
        super().__init__(address, LibraryRequestHandler)
        try:
            self.locations = LocationsStore(self.workspace_dir, self.state_dir)
            self.sequence_links = SequenceLinks(self.locations)
            self.editing = EditingManager(
                self.workspace_dir, files_dir=editing_dir, locations=self.locations
            )
            self.trash = PhotoTrash(self.locations)
            self.imports = ImportManager(self.locations)
            self.folder_indexer = FolderIndexer(self.locations, self.imports)
            self.embedding_runs = EmbeddingRuns(
                self.state_dir, self.workspace_dir, engines=self.engines,
                default_backend=lambda: self.search.engine.profile.key,
            )
        except Exception:
            super().server_close()
            raise

    def server_close(self) -> None:
        if imports := getattr(self, "imports", None):
            imports.close()
        if embeddings := getattr(self, "embedding_runs", None):
            embeddings.close()
        if trash := getattr(self, "trash", None):
            trash.close()
        if indexer := getattr(self, "folder_indexer", None):
            indexer.close()
        if editing := getattr(self, "editing", None):
            editing.close()
        if runtime := getattr(self, "local_runtime", None):
            runtime.close()
        super().server_close()

    @property
    def search(self) -> ActiveSearch:
        """The active engine, reloaded only after an explicit activation changes it."""
        with self._search_lock:
            if self._fixed_search is not None:
                if self._search is None:
                    index, semantic = self._fixed_search
                    engine = self.engines["fixed"]
                    self._search = self._build_search(engine, index, semantic)
                return self._search
            assert self.selection is not None
            stamp = self.selection.stamp()
            if self._search is None or stamp != self._search_stamp:
                engine = self.engines[self.selection.read()["backend"]]
                if self._search is None or self._search.engine is not engine:
                    self._search = self._build_search(engine)
                self._search_stamp = stamp
            return self._search

    def _build_search(
        self,
        engine: EmbeddingEngine,
        index: VectorIndex | None = None,
        semantic: SemanticSearch | None = None,
    ) -> ActiveSearch:
        profile = engine.profile
        index = index or VectorIndex(
            engine.embedding_dir, model_id=profile.model_id, dimensions=profile.dimensions,
            metadata=profile.metadata,
        )
        history = SearchHistory(
            self.workspace_dir / "search-history.sqlite", library_id=self.service_info["data_id"],
            model_id=profile.space_id, dimensions=profile.dimensions,
        )
        return ActiveSearch(engine, index, history, semantic or SemanticSearch(
            index, engine.encoder_factory()
        ))

    @property
    def vector_index(self) -> VectorIndex:
        return self.search.vector_index

    @property
    def search_history(self) -> SearchHistory:
        return self.search.history

    def _local_preflight(self) -> None:
        config = load_runtime_config(self.embeddings_root)
        if config is None:
            raise ConfigurationError(
                "Set up the local encoder first with `latent local-encoder setup`"
            )
        check_files(config)

    def search_backends(self) -> dict[str, object]:
        active = self.search.engine
        backends = []
        with StateStore(self.state_dir) as store:
            total_assets = store.library_asset_count()
        for key, engine in self.engines.items():
            entry: dict[str, object] = {
                **engine.profile.public(),
                "active": engine is active,
                "index": self.embedding_progress(engine, total_assets=total_assets),
            }
            try:
                if engine.preflight is not None:
                    engine.preflight()
                entry.update(available=True, availability=None)
            except ConfigurationError as error:
                entry.update(available=False, availability=str(error))
            if key == EMBEDDINGGEMMA:
                config = load_runtime_config(self.embeddings_root) if entry["available"] else None
                entry["runtime"] = {
                    **self.local_runtime.status(),
                    "llama_server": str(config.llama_server) if config else None,
                    "model_dir": str(config.model_dir) if config else None,
                }
                entry["artifact"] = {
                    "repository": ARTIFACT_REPOSITORY, "revision": ARTIFACT_REVISION,
                }
            if engine.profile.requires_validation:
                entry["validation"] = self._validation_status(engine)
            backends.append(entry)
        return {
            "active": active.profile.key,
            "switchable": self.selection is not None,
            "backends": backends,
            "data_id": self.service_info["data_id"],
        }

    def _validation_status(self, engine: EmbeddingEngine) -> dict[str, object]:
        key = engine.profile.key
        thread = self._validations.get(key)
        if thread is not None and thread.is_alive():
            return {"status": "running"}
        if error := self._validation_errors.get(key):
            return {"status": "error", "error": error}
        report = current_validation(engine)
        if report is None:
            return {"status": "not_run"}
        status = "stale" if report.get("stale") else "passed" if report["passed"] else "failed"
        return {**report, "status": status}

    def start_validation(self, backend: str) -> dict[str, object]:
        engine = self.engines.get(backend)
        if engine is None or not engine.profile.requires_validation:
            raise ValueError("This search engine does not use local validation")
        with self._search_lock:
            thread = self._validations.get(backend)
            if thread is not None and thread.is_alive():
                return self._validation_status(engine)
            if self.embedding_runs.active_backend == backend:
                raise ValueError("Pause or finish the running AI Search generation first")
            if engine.preflight is not None:
                engine.preflight()
            self._validation_errors.pop(backend, None)

            def run() -> None:
                try:
                    validate_index(engine, self.state_dir, engine.encoder_factory())
                except Exception as error:
                    self._validation_errors[backend] = str(error)

            thread = threading.Thread(target=run, name="latent-index-validation", daemon=True)
            self._validations[backend] = thread
            thread.start()
        return {"status": "running"}

    def activate_backend(self, backend: str, validation_id: object) -> dict[str, object]:
        if self.selection is None:
            raise ValueError("This service uses a fixed search index")
        engine = self.engines.get(backend)
        if engine is None:
            raise ValueError(f"Unknown search engine: {backend}")
        if engine.profile.requires_validation:
            report = current_validation(engine)
            if report is None or report.get("id") != validation_id:
                raise ValueError("The validation changed. Review the latest result first")
        with self._search_lock:
            self.selection.activate(engine)
        return self.search_backends()

    def embedding_progress(
        self, engine: EmbeddingEngine | None = None, *, total_assets: int | None = None,
    ) -> dict[str, object]:
        with StateStore(self.state_dir) as store:
            hidden_ids = [
                int(row[0])
                for row in store.connection.execute(
                    "SELECT a.id FROM assets a JOIN hidden_assets h "
                    "ON h.provider=a.provider AND h.remote_path=a.remote_path"
                )
            ]
        if total_assets is None:
            with StateStore(self.state_dir) as store:
                total_assets = store.library_asset_count()
        engine = engine or self.search.engine
        profile = engine.profile
        progress = {
            "queued_assets": 0,
            "indexed_assets": 0,
            "stale_jobs": 0,
            "jobs": dict.fromkeys(("pending", "running", "succeeded", "failed"), 0),
        }
        unavailable = False
        if engine.has_index():
            try:
                with engine.store() as store:
                    progress = store.progress(exclude_asset_ids=hidden_ids)
            except ConfigurationError:
                unavailable = True
        jobs = progress["jobs"]
        indexed = progress["indexed_assets"]
        total_assets = max(total_assets, progress["queued_assets"])
        if unavailable:
            phase = "unavailable"
        elif total_assets > 0 and indexed == total_assets:
            phase = "complete"
        elif jobs["running"] > progress["stale_jobs"]:
            phase = "indexing"
        elif progress["stale_jobs"] or jobs["failed"]:
            phase = "needs_attention"
        elif progress["queued_assets"]:
            phase = "paused"
        else:
            phase = "not_started"
        return {
            **progress,
            "total_assets": total_assets,
            "remaining_assets": max(0, total_assets - indexed),
            "phase": phase,
            "semantic_ready": indexed > 0,
            "model_id": profile.model_id,
            "dimensions": profile.dimensions,
            "provider": profile.provider,
            "backend": profile.key,
            "engine": profile.display_name,
            "image_text_queries": profile.image_text_queries,
        }


def _gemini_preflight() -> None:
    if not GeminiEmbeddingEncoder.credentials_available():
        raise ConfigurationError("Configure a Gemini API key before generating photo embeddings")


class LibraryRequestHandler(BaseHTTPRequestHandler):
    server: LibraryServer

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        try:
            if parsed.path in _STATIC_FILES:
                self._serve_static(_STATIC_FILES[parsed.path])
            elif parsed.path == "/health":
                self._send_json(self.server.service_info)
            elif parsed.path == "/api/library":
                self._serve_library()
            elif parsed.path == "/api/embedding-status":
                self._send_json({**self.server.embedding_progress(), "cloud_access": False})
            elif parsed.path == "/api/search-backends":
                self._send_json(self.server.search_backends())
            elif parsed.path == "/api/assets":
                self._serve_assets(parse_qs(parsed.query))
            elif parsed.path == "/api/timelapse":
                self._serve_timelapse()
            elif parsed.path == "/api/starred":
                self._serve_starred(parse_qs(parsed.query))
            elif parsed.path == "/api/search":
                self._serve_semantic_search(parse_qs(parsed.query))
            elif parsed.path == "/api/search/history":
                self._require_search_access()
                self._send_json({"entries": self.server.search_history.list()})
            elif match := _SEARCH_HISTORY_PATH.fullmatch(parsed.path):
                self._require_search_access()
                if match.group(2) != "image":
                    self.send_error(HTTPStatus.NOT_FOUND)
                else:
                    self._send_bytes(self.server.search_history.image(match.group(1)), "image/jpeg")
            elif match := _SIMILAR_PATH.fullmatch(parsed.path):
                self._serve_similar(int(match.group(1)), parse_qs(parsed.query))
            elif match := _CURATOR_PATH.fullmatch(parsed.path):
                self._serve_curator(int(match.group(1)), parse_qs(parsed.query))
            elif parsed.path == "/api/curator/motifs":
                self._serve_curator_motifs(parse_qs(parsed.query))
            elif parsed.path == "/api/editing":
                self._send_json({"batches": self.server.editing.batches()})
            elif parsed.path == "/api/trash":
                self._send_json({"batches": self.server.trash.batches()})
            elif parsed.path == "/api/locations":
                self._send_json(self._locations_payload())
            elif parsed.path == "/api/imports":
                self._send_json(
                    {
                        "batches": self.server.imports.batches(),
                        "data_id": self.server.service_info["data_id"],
                    }
                )
            elif parsed.path == "/api/embedding-runs":
                self._send_json(
                    {
                        "runs": self.server.embedding_runs.runs(),
                        "data_id": self.server.service_info["data_id"],
                    }
                )
            elif match := _EDIT_BATCH_PATH.fullmatch(parsed.path):
                self._send_json({"batch": self.server.editing.get(match.group(1))})
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
            host = self.headers.get("Host", "")
            if not _is_loopback(urlsplit("http://" + host).hostname or ""):
                raise PermissionError("editing and workspace requests require a loopback Host")
            origin = self.headers.get("Origin")
            if self.headers.get("Sec-Fetch-Site") == "cross-site" or (
                origin and origin != f"http://{host}"
            ):
                raise PermissionError("cross-site workspace and editing requests are disabled")
            if method == "POST" and parsed.path == "/api/search/image":
                self._serve_image_search(self._read_json_body(maximum=2 * 1024 * 1024))
            elif match := _SEARCH_HISTORY_PATH.fullmatch(parsed.path):
                identifier, action = match.groups()
                if method == "POST" and action == "replay":
                    self._replay_search(identifier, self._read_json_body())
                elif method == "DELETE" and action is None:
                    payload = self._read_json_body()
                    _validate_json_keys(
                        payload, allowed={"expected_data_id"}, required={"expected_data_id"}
                    )
                    self._validate_workspace_identity(payload)
                    self.server.search_history.delete(identifier)
                    self._send_json({"deleted": identifier})
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            elif method == "POST" and (
                parsed.path == "/api/imports" or _IMPORT_PATH.fullmatch(parsed.path)
            ):
                payload = self._read_json_body()
                self._validate_workspace_identity(payload)
                with self.server.locations.lock:
                    if self.server.trash.busy:
                        raise ValueError("Wait for the photo move to finish before importing")
                    if parsed.path == "/api/imports":
                        _validate_json_keys(
                            payload,
                            allowed={"expected_data_id", "request_id", "paths"},
                            required={"expected_data_id", "request_id", "paths"},
                        )
                        result = self.server.imports.prepare(
                            payload["request_id"],
                            _string_list(payload["paths"], "paths", maximum=16),
                        )
                    else:
                        _validate_json_keys(
                            payload,
                            allowed={"expected_data_id", "expected_revision", "path"},
                            required={"expected_data_id", "expected_revision"},
                        )
                        match = _IMPORT_PATH.fullmatch(parsed.path)
                        result = self.server.imports.action(
                            match.group(1),
                            match.group(2),
                            payload["expected_revision"],
                            payload.get("path"),
                        )
                self._send_json({"batch": result, "data_id": self.server.service_info["data_id"]})
            elif method == "POST" and (
                parsed.path == "/api/embedding-runs" or _EMBEDDING_RUN_PATH.fullmatch(parsed.path)
            ):
                payload = self._read_json_body()
                self._validate_workspace_identity(payload)
                with self.server.locations.lock:
                    if self.server.trash.busy:
                        raise ValueError("Wait for the photo move before generating embeddings")
                    if parsed.path == "/api/embedding-runs":
                        _validate_json_keys(
                            payload,
                            allowed={
                                "expected_data_id",
                                "request_id",
                                "scope",
                                "asset_ids",
                                "batch_id",
                                "backend",
                            },
                            required={"expected_data_id", "request_id", "scope"},
                        )
                        ids = (
                            _integer_list(payload["asset_ids"], "asset_ids", maximum=5000)
                            if payload.get("asset_ids") is not None
                            else None
                        )
                        batch_id = payload.get("batch_id")
                        if payload["scope"] == "import":
                            if ids is not None:
                                raise ValueError("Import scope uses its recorded batch photos")
                            ids = self.server.imports.asset_ids(batch_id)
                        elif batch_id is not None:
                            raise ValueError("Only import scope accepts a batch ID")
                        backend = payload.get("backend")
                        if backend is not None and not isinstance(backend, str):
                            raise ValueError("backend must be a string")
                        result = self.server.embedding_runs.prepare(
                            payload["request_id"], payload["scope"], ids, batch_id=batch_id,
                            backend=backend,
                        )
                    else:
                        _validate_json_keys(
                            payload,
                            allowed={"expected_data_id", "expected_revision"},
                            required={"expected_data_id", "expected_revision"},
                        )
                        match = _EMBEDDING_RUN_PATH.fullmatch(parsed.path)
                        result = self.server.embedding_runs.action(
                            match.group(1), match.group(2), payload["expected_revision"]
                        )
                self._send_json({"run": result, "data_id": self.server.service_info["data_id"]})
            elif method == "POST" and (match := _BACKEND_VALIDATE_PATH.fullmatch(parsed.path)):
                payload = self._read_json_body()
                _validate_json_keys(
                    payload, allowed={"expected_data_id"}, required={"expected_data_id"}
                )
                self._validate_workspace_identity(payload)
                self._send_json({"validation": self.server.start_validation(match.group(1))})
            elif method == "POST" and parsed.path == "/api/search-backends/activate":
                payload = self._read_json_body()
                _validate_json_keys(
                    payload,
                    allowed={"expected_data_id", "backend", "validation_id"},
                    required={"expected_data_id", "backend"},
                )
                self._validate_workspace_identity(payload)
                if not isinstance(payload["backend"], str):
                    raise ValueError("backend must be a string")
                self._send_json(self.server.activate_backend(
                    payload["backend"], payload.get("validation_id")
                ))
            elif method == "POST" and parsed.path == "/api/timelapse/audit":
                payload = self._read_json_body()
                _validate_json_keys(
                    payload,
                    allowed={"expected_data_id"},
                    required={"expected_data_id"},
                )
                self._validate_workspace_identity(payload)
                with TimelapseGroups(self.server.workspace_dir) as groups:
                    audit = groups.audit(self.server.state_dir)
                self._serve_timelapse(audit=audit)
            elif method == "PATCH" and (match := _TIMELAPSE_PATH.fullmatch(parsed.path)):
                payload = self._read_json_body()
                fields = {"expected_data_id", "expected_revision", "confirmed"}
                _validate_json_keys(payload, allowed=fields, required=fields)
                self._validate_workspace_identity(payload)
                with TimelapseGroups(self.server.workspace_dir) as groups:
                    groups.set_confirmed(
                        self.server.state_dir, match.group(1), confirmed=payload["confirmed"],
                        expected_revision=payload["expected_revision"],
                    )
                self._serve_timelapse()
            elif method == "POST" and parsed.path in {
                "/api/locations",
                "/api/locations/scan",
                "/api/locations/cancel",
                "/api/locations/sequence-links",
            }:
                payload = self._read_json_body()
                _validate_json_keys(
                    payload,
                    allowed={
                        "action",
                        "path",
                        "source_id",
                        "expected_revision",
                        "expected_data_id",
                    },
                    required={"expected_revision", "expected_data_id"},
                )
                self._validate_workspace_identity(payload)
                with self.server.locations.lock:
                    if self.server.trash.busy:
                        raise ValueError(
                            "Wait for the photo move to finish before changing locations"
                        )
                    if parsed.path == "/api/locations":
                        if self.server.imports.busy:
                            raise ValueError("Pause the active import before changing locations")
                        self.server.locations.update(payload)
                    elif parsed.path.endswith("/scan"):
                        self.server.folder_indexer.start(payload["expected_revision"])
                    elif parsed.path.endswith("/cancel"):
                        self.server.folder_indexer.cancel()
                    else:
                        result = self.server.sequence_links.sync(payload["expected_revision"])
                        self._send_json({**self._locations_payload(), "sequence_result": result})
                        return
                self._send_json(self._locations_payload())
            elif method == "POST" and parsed.path == "/api/trash":
                self._trash_photos(self._read_json_body())
            elif method == "POST" and (match := _TRASH_PATH.fullmatch(parsed.path)):
                payload = self._read_json_body()
                _validate_json_keys(
                    payload, allowed={"expected_data_id"}, required={"expected_data_id"}
                )
                self._validate_workspace_identity(payload)
                self._check_photo_moves_available()
                batch = self.server.trash.action(
                    match.group(1), restore=match.group(2) == "restore"
                )
                self._send_json({"batch": batch})
            elif method == "POST" and parsed.path == "/api/editing":
                if self.server.trash.busy:
                    raise ValueError("Wait for the photo move to finish before editing")
                payload = self._read_json_body()
                _validate_json_keys(payload, allowed={"asset_ids"}, required={"asset_ids"})
                self._send_json(
                    {"batch": self.server.editing.create(self._editing_records(payload))},
                    status=HTTPStatus.CREATED,
                )
            elif method == "POST" and (match := _EDIT_BATCH_PATH.fullmatch(parsed.path)):
                payload = self._read_json_body()
                identifier, action = match.groups()
                _validate_json_keys(
                    payload,
                    allowed=(
                        {"cleanup_policy", "exports"}
                        if action == "finish"
                        else {"exports"}
                        if action == "map"
                        else set()
                    )
                    | {"expected_data_id", "expected_updated_at"},
                )
                self._validate_workspace_identity(payload)
                batch = self.server.editing.apply_action(
                    identifier,
                    action,
                    policy=payload.get("cleanup_policy", ""),
                    expected_updated_at=payload.get("expected_updated_at"),
                    exports=payload.get("exports"),
                )
                self._send_json({"batch": batch})
            elif method == "PATCH" and parsed.path == "/api/annotations":
                self._annotate(self._read_json_body())
            elif method == "POST" and parsed.path == "/api/sequence-folders":
                self._create_sequence_folder(self._read_json_body())
            elif method in {"PATCH", "DELETE"} and (
                match := _SEQUENCE_FOLDER_PATH.fullmatch(parsed.path)
            ):
                self._change_sequence_folder(
                    unquote(match.group(1)), self._read_json_body(), remove=method == "DELETE"
                )
            elif method == "POST" and parsed.path == "/api/sequences":
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
                payload = (
                    self._read_json_body() if int(self.headers.get("Content-Length", "0")) else {}
                )
                self._delete_sequence(unquote(match.group(1)), payload)
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

    def _locations_payload(self) -> dict:
        return {
            **self.server.locations.get(),
            "indexing": self.server.folder_indexer.status(),
            "data_id": self.server.service_info["data_id"],
        }

    def _serve_static(self, name: str) -> None:
        resource = files("latent").joinpath("web_assets", name)
        data = resource.read_bytes()
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self._send_bytes(data, content_type, cache_control="no-cache")

    def _serve_library(self) -> None:
        with StateStore(self.server.state_dir) as store:
            dates = store.library_dates()
            jobs = store.preview_job_counts()
            cached_assets = store.library_asset_count()
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            workspace_status = workspace.status()
        self._send_json(
            {
                "dates": dates,
                "cached_assets": cached_assets,
                "preview_jobs": jobs,
                "embedding_index": self.server.embedding_progress(total_assets=cached_assets),
                "workspace": {
                    "sequences": workspace_status["sequences"],
                    "items": workspace_status["items"],
                    "writable": self.server.workspace_writes_enabled,
                },
                "cloud_access": False,
            }
        )

    def _serve_timelapse(self, *, audit: dict | None = None) -> None:
        with TimelapseGroups(self.server.workspace_dir) as store:
            groups = store.list(self.server.state_dir)
        ids = [g["cover_asset_id"] for g in groups if g["cover_asset_id"] is not None]
        with StateStore(self.server.state_dir) as catalog:
            covers = {a["id"]: a for a in self._annotated_payloads(
                catalog.library_assets_by_ids(ids),
            )}
        self._send_json({
            "groups": [{**g, "cover": covers.get(g["cover_asset_id"])} for g in groups],
            "audit": audit, "archive_modified": False, "cloud_access": False,
        })

    def _serve_assets(self, query: dict[str, list[str]]) -> None:
        filters = self._query_filters(query)
        capture_date = _single(query, "date")
        if capture_date is not None:
            if _DATE_PATTERN.fullmatch(capture_date) is None:
                raise ValueError("date must use YYYY, YYYY-MM, or YYYY-MM-DD")
            parts = [int(part) for part in capture_date.split("-")]
            date(*(parts + [1] * (3 - len(parts))))
        search_query = (_single(query, "q") or "").strip()
        if len(search_query) > _MAX_SEARCH_LENGTH:
            raise ValueError(f"search query must be {_MAX_SEARCH_LENGTH} characters or fewer")
        limit = _bounded_int(_single(query, "limit"), default=250, minimum=1, maximum=500)
        offset = _bounded_int(_single(query, "offset"), default=0, minimum=0, maximum=100_000)
        collapsed = _single(query, "collapse_timelapses")
        if collapsed not in {None, "0", "1"}:
            raise ValueError("collapse_timelapses must be 0 or 1")
        group_id = _single(query, "timelapse_group")
        badges: dict[int, dict] = {}
        expanded_total = None
        with StateStore(self.server.state_dir) as store:
            allowed = matching_ids(store, self.server.workspace_dir, filters) if filters else None
            if import_id := _single(query, "import_id"):
                imported = set(self.server.imports.asset_ids(import_id))
                allowed = list(imported if allowed is None else imported.intersection(allowed))
            if collapsed == "1" or group_id is not None:
                ordered = store.library_asset_ids(
                    allowed_asset_ids=allowed, capture_date=capture_date,
                    search_query=search_query or None,
                )
                with TimelapseGroups(self.server.workspace_dir) as groups:
                    allowed, badges = groups.presentation(store, ordered, group_id=group_id)
                expanded_total = len(ordered) if group_id is None else len(allowed)
            assets = store.library_assets(
                allowed_asset_ids=allowed,
                capture_date=capture_date,
                search_query=search_query or None,
                limit=limit,
                offset=offset,
            )
            total = store.library_asset_count(
                allowed_asset_ids=allowed,
                capture_date=capture_date,
                search_query=search_query or None,
            )
        payload = self._annotated_payloads(assets)
        for asset in payload:
            if badge := badges.get(asset["id"]):
                asset["timelapse"] = badge
        next_offset = offset + len(payload)
        has_more = next_offset < total
        self._send_json(
            {
                "date": capture_date,
                "query": search_query,
                "limit": limit,
                "offset": offset,
                "total": total,
                "expanded_total": expanded_total,
                "has_more": has_more,
                "next_offset": next_offset if has_more else None,
                "assets": payload,
                "cloud_access": False,
            }
        )

    @staticmethod
    def _query_filters(query: dict[str, list[str]]) -> dict:
        return photo_filters(
            {
                key: _single(query, key)
                for key in ("date_from", "date_to", "rating_min", "starred", "flag")
                if key in query
            }
        )

    def _matching_ids(self, filters: dict) -> list[int]:
        with StateStore(self.server.state_dir) as store:
            return matching_ids(store, self.server.workspace_dir, filters)

    def _annotated_payloads(self, assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            annotations = workspace.annotations(
                [(asset["provider"], asset["remote_path"]) for asset in assets]
            )
        by_identity = {(a["provider"], a["remote_path"]): a for a in annotations}
        result = []
        for asset in assets:
            annotation = by_identity.get((asset["provider"], asset["remote_path"]), {})
            result.append(
                {
                    **self._asset_payload(asset),
                    "rating": annotation.get("rating", 0),
                    "caption": annotation.get("caption", ""),
                    "flag": annotation.get("flag", "unmarked"),
                }
            )
        return result

    def _selected_records(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        ids = _integer_list(payload["asset_ids"], "asset_ids", maximum=500)
        if not ids:
            raise ValueError("select at least one photo")
        with StateStore(self.server.state_dir) as store:
            records = store.library_assets_by_ids(ids)
        if len(records) != len(ids):
            raise KeyError("one or more selected photos are missing from the library")
        return records

    def _editing_records(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        records = self._selected_records(payload)
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            annotations = workspace.annotations(
                [(r["provider"], r["remote_path"]) for r in records]
            )
        by_identity = {(a["provider"], a["remote_path"]): a for a in annotations}
        return [
            {**record, "annotation": by_identity.get((record["provider"], record["remote_path"]))}
            for record in records
        ]

    def _check_photo_moves_available(self) -> None:
        if (self.server.imports.busy
                or self.server.embedding_runs.busy
                or (self.server.editing.worker is not None
                    and self.server.editing.worker.is_alive())):
            raise ValueError(
                "Pause imports or embedding generation, or wait for the editing transfer, "
                "before moving originals"
            )

    def _trash_photos(self, payload: dict) -> None:
        _validate_json_keys(
            payload,
            allowed={"request_id", "asset_ids", "expected"},
            required={"request_id", "asset_ids", "expected"},
        )
        self._check_photo_moves_available()
        try:
            previous = self.server.trash.get(payload["request_id"])
        except KeyError:
            previous = None
        records = previous["items"] if previous else self._selected_records(payload)
        if set(_integer_list(payload["asset_ids"], "asset_ids", maximum=500)) != {
            r["id"] for r in records
        }:
            raise ValueError("Trash request photos changed")
        self._validate_annotation_targets(payload["expected"], records)
        batch = self.server.trash.create(payload["request_id"], records)
        self._send_json({"batch": batch})

    def _annotate(self, payload: dict[str, Any]) -> None:
        _validate_json_keys(
            payload,
            allowed={"asset_ids", "rating", "caption", "flag", "expected"},
            required={"asset_ids"},
        )
        records = self._selected_records(payload)
        if "expected" in payload:
            self._validate_annotation_targets(payload["expected"], records)
        assets = [
            WorkspaceAsset(
                provider=r["provider"],
                remote_path=r["remote_path"],
                fingerprint=r["fingerprint"],
                name=r["name"],
            )
            for r in records
        ]
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            workspace.annotate(
                assets,
                rating=payload.get("rating"),
                caption=payload.get("caption"),
                flag=payload.get("flag"),
            )
        self._send_json({"assets": self._annotated_payloads(records), "archive_modified": False})

    def _validate_annotation_targets(self, expected: object, records: list[dict[str, Any]]) -> None:
        if not isinstance(expected, dict):
            raise ValueError("expected must identify the library and selected photos")
        _validate_json_keys(expected, allowed={"data_id", "assets"}, required={"data_id", "assets"})
        if expected["data_id"] != self.server.service_info["data_id"]:
            raise ValueError("Pending edits belong to another library; no annotations were changed")
        targets = expected["assets"]
        if not isinstance(targets, list) or len(targets) != len(records):
            raise ValueError("expected assets must identify every selected photo")
        fields = {"id", "provider", "remote_path", "fingerprint"}
        by_id = {record["id"]: record for record in records}
        seen = set()
        for target in targets:
            if not isinstance(target, dict):
                raise ValueError("expected assets must be photo identities")
            _validate_json_keys(target, allowed=fields, required=fields)
            identifier = target["id"]
            if type(identifier) is not int or identifier in seen or identifier not in by_id:
                raise ValueError("expected assets must identify every selected photo exactly once")
            seen.add(identifier)
            if any(target[field] != by_id[identifier][field] for field in fields):
                raise ValueError(
                    "A photo changed in the library; no annotations were changed. "
                    "Refresh the library and review the pending edits"
                )

    def _serve_starred(self, query: dict[str, list[str]]) -> None:
        offset = _bounded_int(_single(query, "offset"), default=0, minimum=0, maximum=100_000)
        limit = _bounded_int(_single(query, "limit"), default=250, minimum=1, maximum=500)
        minimum = _bounded_int(_single(query, "rating"), default=1, minimum=1, maximum=5)
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            annotations = sorted(
                (a for a in workspace.annotations() if a["rating"] >= minimum),
                key=lambda a: (a["rating"], a["updated_at"]),
                reverse=True,
            )
        identities = [(a["provider"], a["remote_path"]) for a in annotations]
        records = []
        with StateStore(self.server.state_dir) as store:
            for start in range(0, len(identities), 500):
                records.extend(store.library_assets_by_identities(identities[start : start + 500]))
        by_identity = {(r["provider"], r["remote_path"]): r for r in records}
        records = [by_identity[i] for i in identities if i in by_identity]
        page = records[offset : offset + limit]
        next_offset = offset + len(page)
        self._send_json(
            {
                "assets": self._annotated_payloads(page),
                "total": len(records),
                "has_more": next_offset < len(records),
                "next_offset": next_offset if next_offset < len(records) else None,
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
        variety = _single(query, "variety") or "0"
        if variety not in ("0", "1"):
            raise ValueError("variety must be 0 or 1")
        search = self.server.search
        if search.vector_index.size == 0:
            raise ConfigurationError("semantic index has no completed vectors")
        self._require_search_access()
        order = _search_order(query, variety=variety == "1")
        filters = self._query_filters(query)
        allowed = self._matching_ids(filters)
        entry, vector, cached = search.history.resolve(
            query=search_query, image=None, image_name=None, filters=filters, order=order,
            encode=lambda: search.semantic.text_vector(search_query),
        )
        matches = search.vector_index.search_vector(
            vector, limit=limit, order=order, allowed_asset_ids=allowed,
        )
        self._send_matches(
            matches,
            {
                "query": search_query, "mode": "semantic", "history_id": entry["id"],
                "query_cached": cached,
                "ranking": "relevance_with_variety" if order == "variety" else "relevance",
                "order": order,
                "backend": search.engine.profile.key,
            },
        )

    def _require_search_access(self) -> None:
        if not self.server.workspace_writes_enabled:
            raise PermissionError("Gemini queries are disabled on non-loopback binds")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise PermissionError("cross-site Gemini queries are disabled")
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and origin != f"http://{host}":
            raise PermissionError("cross-origin Gemini queries are disabled")
        if not _is_loopback(urlsplit(f"http://{host}").hostname or ""):
            raise PermissionError("Gemini queries require a loopback Host")

    def _serve_image_search(self, payload: dict[str, Any]) -> None:
        _validate_json_keys(
            payload,
            allowed={"image_base64", "image_name", "query", "limit", "order", "filters"},
            required={"image_base64"},
        )
        encoded = payload["image_base64"]
        if not isinstance(encoded, str) or len(encoded) > 1_398_104:
            raise ValueError("image search requires a base64 JPEG preview up to 1 MiB")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise ValueError("image search requires valid base64 image data") from error
        query = payload.get("query", "")
        if not isinstance(query, str) or len(query.strip()) > _MAX_SEARCH_LENGTH:
            raise ValueError("image search text must be at most 200 characters")
        query = query.strip()
        image_name = payload.get("image_name", "Reference image")
        if not isinstance(image_name, str) or not 1 <= len(image_name) <= 255:
            raise ValueError("image name must be between 1 and 255 characters")
        limit = payload.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("image search limit must be between 1 and 100")
        order = payload.get("order", "closest")
        if order not in ("closest", "least_similar", "variety"):
            raise ValueError("order must be closest, least_similar, or variety")
        search = self.server.search
        if query and not search.engine.profile.image_text_queries:
            raise ValueError(
                f"Adding words to a reference image is not available with "
                f"{search.engine.profile.display_name} yet"
            )
        if search.vector_index.size == 0:
            raise ConfigurationError("semantic index has no completed vectors")
        filters = photo_filters(payload.get("filters", {}))
        allowed = self._matching_ids(filters)
        entry, vector, cached = search.history.resolve(
            query=query, image=data, image_name=image_name, filters=filters, order=order,
            encode=lambda: search.semantic.encoder.encode_image_query(data, query),
        )
        matches = search.vector_index.search_vector(
            vector, limit=limit, order=order, allowed_asset_ids=allowed,
        )
        self._send_matches(matches, {
            "mode": "image_search", "query": query, "order": order,
            "history_id": entry["id"], "query_cached": cached,
            "backend": search.engine.profile.key,
        })

    def _replay_search(self, identifier: str, payload: dict[str, Any]) -> None:
        _validate_json_keys(
            payload, allowed={"filters", "order", "expected_data_id"},
            required={"filters", "order", "expected_data_id"},
        )
        self._validate_workspace_identity(payload)
        order = payload["order"]
        if order not in ("closest", "least_similar", "variety"):
            raise ValueError("order must be closest, least_similar, or variety")
        filters = photo_filters(payload["filters"])
        allowed = self._matching_ids(filters)
        search = self.server.search
        entry, vector = search.history.replay(identifier, filters=filters, order=order)
        matches = search.vector_index.search_vector(
            vector, limit=100, order=order, allowed_asset_ids=allowed,
        )
        self._send_matches(matches, {
            "mode": "image_search" if entry["kind"] == "image" else "semantic",
            "query": entry["query"], "order": order, "history_id": identifier, "query_cached": True,
            "backend": search.engine.profile.key,
        })

    def _serve_similar(
        self,
        asset_id: int,
        query: dict[str, list[str]],
    ) -> None:
        limit = _bounded_int(_single(query, "limit"), default=50, minimum=1, maximum=100)
        if self.server.vector_index.size == 0:
            raise ConfigurationError("similarity index has no completed vectors")
        order = _search_order(query)
        matches = self.server.vector_index.similar(
            asset_id,
            limit=limit,
            order=order,
            allowed_asset_ids=self._matching_ids(self._query_filters(query)),
        )
        self._send_matches(
            matches,
            {
                "source_asset_id": asset_id,
                "mode": "visual_similarity",
                "order": order,
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
        matches = self.server.vector_index.similar(
            asset_id, limit=limit, allowed_asset_ids=self._matching_ids({})
        )
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
            folders = workspace.list_folders()
            status = workspace.status()
        for sequence in sequences:
            if sequence.get("smart_filters") is not None:
                sequence["item_count"] = len(self._matching_ids(sequence["smart_filters"]))
        self._send_json(
            {
                "sequences": sequences,
                "folders": folders,
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
        _validate_json_keys(
            payload,
            allowed={
                "name",
                "note",
                "sequence_id",
                "folder_id",
                "smart_filters",
                "expected_data_id",
            },
            required={"name"},
        )
        self._validate_workspace_identity(payload)
        identifier = payload.get("sequence_id")
        if "sequence_id" in payload and (
            not isinstance(identifier, str) or str(UUID(identifier)) != identifier
        ):
            raise ValueError("sequence_id must be a canonical UUID")
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            sequence = workspace.create_sequence(
                payload["name"],
                note=payload.get("note", ""),
                origin="manual",
                sequence_id=identifier,
                reuse_existing=identifier is not None,
                folder_id=payload.get("folder_id"),
                smart_filters=payload.get("smart_filters"),
            )
        self._send_json(
            {
                "sequence": self._hydrate_sequence(sequence),
                "archive_modified": False,
            },
            status=HTTPStatus.CREATED,
        )

    def _update_sequence(self, sequence_id: str, payload: dict[str, Any]) -> None:
        _validate_json_keys(
            payload, allowed={"name", "note", "folder_id", "smart_filters", "expected_data_id"}
        )
        self._validate_workspace_identity(payload)
        if not {"name", "note", "folder_id", "smart_filters"}.intersection(payload):
            raise ValueError("sequence update must include name, note, or folder_id")
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            sequence = workspace.update_sequence(
                sequence_id,
                name=payload.get("name"),
                note=payload.get("note"),
                **({"folder_id": payload["folder_id"]} if "folder_id" in payload else {}),
                **(
                    {"smart_filters": photo_filters(payload["smart_filters"])}
                    if "smart_filters" in payload
                    else {}
                ),
            )
        self._send_json(
            {
                "sequence": self._hydrate_sequence(sequence),
                "archive_modified": False,
            }
        )

    def _create_sequence_folder(self, payload: dict[str, Any]) -> None:
        _validate_json_keys(
            payload,
            allowed={"name", "parent_id", "folder_id", "expected_data_id"},
            required={"name"},
        )
        self._validate_workspace_identity(payload)
        identifier = payload.get("folder_id")
        if "folder_id" in payload and (
            not isinstance(identifier, str) or str(UUID(identifier)) != identifier
        ):
            raise ValueError("folder_id must be a canonical UUID")
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            folder = workspace.create_folder(
                payload["name"],
                parent_id=payload.get("parent_id"),
                folder_id=identifier,
                reuse_existing=identifier is not None,
            )
        self._send_json({"folder": folder, "archive_modified": False}, status=HTTPStatus.CREATED)

    def _change_sequence_folder(
        self,
        folder_id: str,
        payload: dict[str, Any],
        *,
        remove: bool,
    ) -> None:
        allowed = {"expected_data_id"} if remove else {"name", "parent_id", "expected_data_id"}
        _validate_json_keys(payload, allowed=allowed)
        self._validate_workspace_identity(payload)
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            if remove:
                workspace.remove_folder(folder_id)
                self._send_json({"removed": True, "archive_modified": False})
                return
            if not {"name", "parent_id"}.intersection(payload):
                raise ValueError("folder update must include name or parent_id")
            folder = workspace.update_folder(
                folder_id,
                name=payload.get("name"),
                **({"parent_id": payload["parent_id"]} if "parent_id" in payload else {}),
            )
        self._send_json({"folder": folder, "archive_modified": False})

    def _add_sequence_items(self, sequence_id: str, payload: dict[str, Any]) -> None:
        _validate_json_keys(payload, allowed={"asset_ids", "expected"}, required={"asset_ids"})
        asset_ids = _integer_list(payload["asset_ids"], "asset_ids", maximum=100)
        with StateStore(self.server.state_dir) as store:
            records = store.library_assets_by_ids(asset_ids)
        if len(records) != len(asset_ids):
            found_ids = {int(record["id"]) for record in records}
            missing = [asset_id for asset_id in asset_ids if asset_id not in found_ids]
            raise KeyError(f"local library assets were not found: {missing}")
        if "expected" in payload:
            self._validate_annotation_targets(payload["expected"], records)
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

    def _validate_workspace_identity(self, payload: dict[str, Any]) -> None:
        if "expected_data_id" in payload and (
            payload["expected_data_id"] != self.server.service_info["data_id"]
        ):
            raise ValueError("This draft belongs to another library; no changes were made")

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

    def _delete_sequence(self, sequence_id: str, payload: dict[str, Any]) -> None:
        _validate_json_keys(payload, allowed={"expected_data_id"})
        self._validate_workspace_identity(payload)
        with WorkspaceStore(self.server.workspace_dir) as workspace:
            deleted = workspace.delete_sequence(sequence_id)
        if not deleted:
            raise KeyError(f"sequence {sequence_id!r} was not found")
        self._send_json({"deleted": True, "archive_modified": False})

    def _hydrate_sequence(self, sequence: dict[str, Any]) -> dict[str, Any]:
        if sequence.get("smart_filters") is not None:
            ids = self._matching_ids(sequence["smart_filters"])
            with StateStore(self.server.state_dir) as store:
                assets = self._annotated_payloads(store.library_assets_by_ids(ids[:250]))
            return {
                **sequence,
                "item_count": len(ids),
                "next_offset": 250 if len(ids) > 250 else None,
                "items": [
                    {
                        "id": f"smart-{asset['id']}",
                        "name": asset["name"],
                        "asset": asset,
                        "library_status": "available",
                    }
                    for asset in assets
                ],
            }
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
        annotated = {item["id"]: item for item in self._annotated_payloads(current_assets)}
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
                    "asset": annotated[current["id"]] if current is not None else None,
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
                "cloud_access": context.get("mode") in {"semantic", "image_search"}
                and not context.get("query_cached", False),
                "archive_cloud_access": False,
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
        return self._annotated_payloads(assets)

    def _relation_basis(self) -> dict[str, str]:
        return {
            "model_id": self.server.search.engine.profile.model_id,
            "metric": "cosine_similarity",
            "source": "local_contact_embeddings",
            "embedding_provider": self.server.search.engine.profile.provider,
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
            "provider": asset["provider"],
            "fingerprint": asset["fingerprint"],
            "size_bytes": asset["size_bytes"],
            "capture_at": asset["capture_at"],
            "camera_model": asset["camera_model"],
            "lens_model": asset["lens_model"],
            "exif": _cached_photo_exif(asset.get("exif_json")),
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

    def _read_json_body(self, *, maximum: int = _MAX_JSON_BODY_BYTES) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("workspace mutations require application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("workspace mutations require Content-Length")
        length = int(raw_length)
        if length < 1 or length > maximum:
            raise ValueError(f"JSON body must be between 1 and {maximum} bytes")
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
    editing_dir: Path = DEFAULT_EDITING_DIR,
    ready_json: bool = False,
    exit_on_stdin_close: bool = False,
) -> None:
    if not allow_remote and not _is_loopback(host):
        raise ConfigurationError("refusing a non-loopback bind without --allow-remote")
    with workspace_service_lease(workspace_dir):
        try:
            server = LibraryServer(
                (host, port),
                state_dir,
                embedding_dir=embedding_dir,
                workspace_dir=workspace_dir,
                editing_dir=editing_dir,
            )
        except OSError as error:
            raise ConfigurationError(
                f"Cannot start the library at {host}:{port}: {error.strerror}. "
                "Check the address and port; no existing process was stopped."
            ) from error
        try:
            if exit_on_stdin_close:
                # Only an explicitly managed service watches its owner's pipe.
                # EOF also arrives if the owner crashes; there is no saved PID to reuse.
                def wait_for_owner() -> None:
                    while sys.stdin.buffer.read(1024):
                        pass
                    server.shutdown()

                threading.Thread(target=wait_for_owner, daemon=True).start()
            actual_host, actual_port = server.server_address[:2]
            address = f"http://{actual_host}:{actual_port}"
            if ready_json:
                ready = {**server.service_info, "event": "ready", "url": address}
                print(json.dumps(ready), flush=True)
            else:
                print(f"Latent contact sheet: {address}", flush=True)
                print(
                    "Browsing: local cache; editing transfers: explicit; "
                    "semantic queries: Gemini API",
                    flush=True,
                )
                writable = "enabled" if server.workspace_writes_enabled else "disabled"
                print(f"Workspace writes: {writable}", flush=True)
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


def _cached_photo_exif(raw: str | None) -> dict[str, float]:
    """Expose shooting settings already indexed locally, without serials or GPS."""
    try:
        metadata = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(metadata, dict):
        return {}
    result = {}
    for source, field in (
        ("ExposureTime", "exposure_time"), ("FNumber", "f_number"),
        ("ISO", "iso"), ("FocalLength", "focal_length"),
        ("ImageWidth", "image_width"), ("ImageHeight", "image_height"),
    ):
        value = metadata.get(source)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        try:
            number = float(Fraction(value.split()[0])) if isinstance(value, str) else float(value)
        except (ValueError, IndexError, ZeroDivisionError, OverflowError):
            continue
        if math.isfinite(number) and 0 < number <= 1_000_000_000:
            result[field] = number
    return result


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


def _search_order(query: dict[str, list[str]], *, variety: bool = False) -> str:
    order = _single(query, "order") or ("variety" if variety else "closest")
    if order not in ("closest", "least_similar", "variety"):
        raise ValueError("order must be closest, least_similar, or variety")
    return order
