"""Photo embeddings run only for an explicitly reviewed, durable user selection."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from .embedding_backends import GEMINI, EmbeddingEngine
from .embeddings import EmbeddingWorker, load_embedding_assets
from .errors import ConfigurationError
from .workspace import utc_now, write_workspace_export


class EmbeddingRuns:
    """Each reviewed selection is bound to one engine and writes only that engine's index."""

    def __init__(
        self,
        state_dir: Path,
        workspace_dir: Path,
        *,
        engines: Mapping[str, EmbeddingEngine],
        default_backend: Callable[[], str] = lambda: GEMINI,
    ) -> None:
        self.state_dir = state_dir
        self.root = workspace_dir / "embedding-runs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.engines = dict(engines)
        self.default_backend = default_backend
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None
        self.active_id: str | None = None
        self.active_backend: str | None = None
        for path in self.root.glob("*/status.json"):
            state = json.loads(path.read_text())
            if state["status"] == "running":
                state.update(
                    status="interrupted",
                    error="Generation interrupted. Resume this selection when ready.",
                )
                write_workspace_export(path, state)

    @property
    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def close(self) -> None:
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=35)

    def _folder(self, identifier: str) -> Path:
        if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
            raise ValueError("Invalid embedding request ID")
        return self.root / identifier

    def get(self, identifier: str) -> dict:
        path = self._folder(identifier) / "status.json"
        if not path.is_file():
            raise KeyError("Embedding request not found")
        return json.loads(path.read_text())

    def runs(self) -> list[dict]:
        return sorted(
            (json.loads(p.read_text()) for p in self.root.glob("*/status.json")),
            key=lambda s: s["created_at"],
            reverse=True,
        )

    def _save(self, state: dict) -> None:
        with self.lock:
            write_workspace_export(self._folder(state["id"]) / "status.json", state)

    def _assets(self):
        assets = load_embedding_assets(self.state_dir)
        db = sqlite3.connect((self.state_dir / "index.sqlite").as_uri() + "?mode=ro", uri=True)
        try:
            hidden = set(db.execute("SELECT provider,remote_path FROM hidden_assets"))
        finally:
            db.close()
        return [a for a in assets if a.identity not in hidden]

    def engine(self, backend: str) -> EmbeddingEngine:
        try:
            return self.engines[backend]
        except KeyError:
            raise ValueError(f"Unknown search engine: {backend}") from None

    @staticmethod
    def _valid(engine: EmbeddingEngine) -> set[tuple[int, str]]:
        path = engine.embedding_dir / "index.sqlite"
        if not path.is_file():
            return set()
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            metadata = dict(db.execute("SELECT key,value FROM embedding_meta"))
            expected = {
                "model_id": engine.profile.model_id,
                "dimensions": str(engine.profile.dimensions),
                **engine.profile.metadata,
            }
            if any(metadata.get(key) != value for key, value in expected.items()):
                raise ConfigurationError(
                    "The embedding index uses a different model. "
                    "Choose the matching index before reviewing photo embeddings"
                )
            return set(
                db.execute(
                    "SELECT j.asset_id,e.fingerprint FROM embedding_jobs j "
                    "JOIN embeddings e ON e.job_id=j.id WHERE j.status='succeeded' "
                    "AND j.fingerprint=e.fingerprint"
                )
            )
        finally:
            db.close()

    def prepare(
        self,
        identifier: str,
        scope: str,
        asset_ids: list[int] | None = None,
        *,
        batch_id: str | None = None,
        backend: str | None = None,
    ) -> dict:
        with self.lock:
            folder = self._folder(identifier)
            engine = self.engine(backend or self.default_backend())
            if scope not in {"all", "incremental", "selected", "import"}:
                raise ValueError(
                    "Choose all photos, incremental, selected photos or an import batch"
                )
            requested = sorted(set(asset_ids or []))
            request = {"scope": scope, "asset_ids": requested, "batch_id": batch_id}
            if engine.profile.key != GEMINI:
                request["backend"] = engine.profile.key
            if (folder / "plan.json").is_file():
                previous = json.loads((folder / "plan.json").read_text())
                if previous["request"] != request:
                    raise ValueError(
                        "This request already refers to a different embedding selection"
                    )
                return self.get(identifier)
            assets = self._assets()
            valid = self._valid(engine)
            if scope in {"selected", "import"}:
                if not requested:
                    raise ValueError("Select at least one photo with a ready preview")
                chosen = set(requested)
                assets = [a for a in assets if a.asset_id in chosen]
                if scope == "selected" and chosen != {a.asset_id for a in assets}:
                    raise ValueError(
                        "Selected photos changed or do not have ready previews. "
                        "Refresh the selection"
                    )
            elif requested:
                raise ValueError("Only selected photos and import scopes accept asset IDs")
            if scope == "incremental":
                assets = [a for a in assets if (a.asset_id, a.fingerprint) not in valid]
            candidates = [a for a in assets if (a.asset_id, a.fingerprint) not in valid]
            total_bytes = sum(
                (self.state_dir / "cache" / a.contact_relative_path).stat().st_size
                for a in candidates
            )
            profile = engine.profile
            state = {
                "id": identifier,
                "created_at": utc_now(),
                "revision": str(uuid4()),
                "scope": scope,
                "batch_id": batch_id,
                "status": "prepared",
                "error": None,
                "selected": len(assets),
                "reused": len(assets) - len(candidates),
                "to_generate": len(candidates),
                "succeeded": 0,
                "failed": 0,
                "upload_bytes": total_bytes if profile.sends_previews else 0,
                "estimated_cost_usd": round(len(candidates) * profile.image_cost_usd, 6),
                "estimated_seconds": math.ceil(len(candidates) / profile.images_per_second)
                if profile.images_per_second else None,
                "model": profile.model_id,
                "backend": profile.key,
                "engine": profile.display_name,
                "provider": profile.provider,
                "local": not profile.sends_previews,
            }
            write_workspace_export(
                folder / "plan.json",
                {
                    "request": request,
                    "photos": [asdict(a) for a in candidates],
                    "model": profile.model_id,
                    "dimensions": profile.dimensions,
                    "backend": profile.key,
                    "space_id": profile.space_id,
                },
            )
            self._save(state)
            return state

    def action(self, identifier: str, action: str, revision: str) -> dict:
        with self.lock:
            state = self.get(identifier)
            if state["revision"] != revision:
                raise ValueError("The embedding selection changed. Review it again")
            if action == "pause":
                if self.active_id == identifier:
                    self.stop.set()
                return state
            if action not in {"start", "resume"}:
                raise ValueError("Unknown embedding action")
            if self.busy:
                raise ValueError("Another embedding selection is running. Pause it first")
            plan = json.loads((self._folder(identifier) / "plan.json").read_text())
            engine = self.engine(plan.get("backend", GEMINI))
            profile = engine.profile
            if (plan["model"], plan["dimensions"], plan.get("space_id", profile.space_id)) != (
                profile.model_id, profile.dimensions, profile.space_id
            ):
                raise ValueError("The embedding model changed. Review a new selection")
            available = {a.asset_id: a for a in self._assets()}
            for photo in plan["photos"]:
                current = available.get(photo["asset_id"])
                if current is None or asdict(current) != photo:
                    raise ValueError(
                        "A reviewed photo was removed or changed. "
                        "Review a new selection before generating"
                    )
            valid = self._valid(engine)
            assets = [
                available[p["asset_id"]]
                for p in plan["photos"]
                if (p["asset_id"], p["fingerprint"]) not in valid
            ]
            if not assets:
                state.update(
                    status="complete", succeeded=state["to_generate"], failed=0, error=None
                )
                self._save(state)
                return state
            if engine.preflight is not None:
                engine.preflight()
            self.stop.clear()
            self.active_id = identifier
            self.active_backend = profile.key
            completed_before = state["to_generate"] - len(assets)
            state.update(status="running", error=None, succeeded=completed_before, failed=0)
            self._save(state)

            def run():
                try:
                    encoder = engine.encoder_factory()
                    with engine.store() as store:
                        store.sync_assets(assets, retry_failed=True, prune_missing=False)

                        def progress(result):
                            state.update(
                                succeeded=completed_before + result.succeeded, failed=result.failed
                            )
                            self._save(state)

                        with store.job_scope([a.asset_id for a in assets]):
                            result = EmbeddingWorker(store, encoder, self.state_dir / "cache").run(
                                max_jobs=len(assets),
                                batch_size=profile.batch_size,
                                stale_after=timedelta(seconds=0),
                                progress=progress,
                                stop_requested=self.stop.is_set,
                            )
                        progress(result)
                    remaining = state["to_generate"] - state["succeeded"]
                    state.update(
                        status="paused"
                        if self.stop.is_set()
                        else "needs_attention"
                        if remaining
                        else "complete",
                        error=f"{remaining} photos remain. Resume this selection to retry."
                        if remaining and not self.stop.is_set()
                        else None,
                    )
                except Exception as error:
                    state.update(status="needs_attention", error=str(error))
                finally:
                    self._save(state)
                    self.active_id = None
                    self.active_backend = None

            self.worker = threading.Thread(target=run, name="latent-user-embeddings", daemon=True)
            self.worker.start()
            return state
