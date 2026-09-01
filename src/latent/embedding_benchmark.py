"""Benchmark local image-text encoders against cached Latent contact images."""

from __future__ import annotations

import argparse
import json
import resource
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, Protocol

DEFAULT_QUERIES = (
    "snowy blue landscape with a bridge",
    "蓝色雪景中的桥",
    "bird flying in the sky",
    "天空中飞翔的鸟",
    "a close portrait of a person",
    "人物近景肖像",
    "the moon against a dark sky",
    "黑色天空中的月亮",
    "city lights at night",
    "夜晚的城市灯光",
    "mountains and water",
    "山水风景",
)


@dataclass(frozen=True)
class SampleAsset:
    asset_id: int
    name: str
    capture_at: str
    fingerprint: str
    contact_path: str

    @property
    def capture_date(self) -> str:
        return self.capture_at[:10].replace(":", "-")


class EmbeddingEncoder(Protocol):
    model_id: str
    device: str
    load_seconds: float

    def encode_images(self, paths: Sequence[Path], batch_size: int) -> Any: ...

    def encode_texts(self, texts: Sequence[str]) -> Any: ...

    def runtime_memory(self) -> dict[str, float | None]: ...


class MobileCLIP2Encoder:
    model_id = "MobileCLIP2-S2/dfndr2b"

    def __init__(self, *, cache_dir: Path, device: str) -> None:
        import open_clip
        import torch

        started = time.perf_counter()
        self._torch = torch
        self.device = device
        self._model, _, self._preprocess = open_clip.create_model_and_transforms(
            "MobileCLIP2-S2",
            pretrained="dfndr2b",
            cache_dir=str(cache_dir),
        )
        self._tokenizer = open_clip.get_tokenizer(
            "MobileCLIP2-S2",
            cache_dir=str(cache_dir),
        )
        self._model.eval().to(device)
        self.load_seconds = time.perf_counter() - started

    def encode_images(self, paths: Sequence[Path], batch_size: int) -> Any:
        from PIL import Image

        batches = []
        with self._torch.inference_mode():
            for start in range(0, len(paths), batch_size):
                images = []
                for path in paths[start : start + batch_size]:
                    with Image.open(path) as image:
                        images.append(self._preprocess(image.convert("RGB")))
                inputs = self._torch.stack(images).to(self.device)
                features = self._model.encode_image(inputs)
                batches.append(_normalize(features, self._torch).cpu())
        return self._torch.cat(batches).numpy()

    def encode_texts(self, texts: Sequence[str]) -> Any:
        with self._torch.inference_mode():
            tokens = self._tokenizer(list(texts)).to(self.device)
            features = self._model.encode_text(tokens)
            return _normalize(features, self._torch).cpu().numpy()

    def runtime_memory(self) -> dict[str, float | None]:
        return _runtime_memory(self._torch, self.device)


class SigLIP2Encoder:
    model_id = "google/siglip2-base-patch16-224"

    def __init__(self, *, cache_dir: Path, device: str) -> None:
        import torch
        from transformers import AutoModel, AutoProcessor

        started = time.perf_counter()
        self._torch = torch
        self.device = device
        self._model = AutoModel.from_pretrained(self.model_id, cache_dir=cache_dir)
        self._processor = AutoProcessor.from_pretrained(self.model_id, cache_dir=cache_dir)
        self._model.eval().to(device)
        self.load_seconds = time.perf_counter() - started

    def encode_images(self, paths: Sequence[Path], batch_size: int) -> Any:
        from PIL import Image

        batches = []
        with self._torch.inference_mode():
            for start in range(0, len(paths), batch_size):
                images = []
                for path in paths[start : start + batch_size]:
                    with Image.open(path) as image:
                        images.append(image.convert("RGB"))
                inputs = self._processor(images=images, return_tensors="pt")
                inputs = {key: value.to(self.device) for key, value in inputs.items()}
                features = _feature_tensor(self._model.get_image_features(**inputs))
                batches.append(_normalize(features, self._torch).cpu())
        return self._torch.cat(batches).numpy()

    def encode_texts(self, texts: Sequence[str]) -> Any:
        with self._torch.inference_mode():
            inputs = self._processor(
                text=list(texts),
                padding="max_length",
                truncation=True,
                max_length=64,
                return_tensors="pt",
            )
            inputs = {key: value.to(self.device) for key, value in inputs.items()}
            features = _feature_tensor(self._model.get_text_features(**inputs))
            return _normalize(features, self._torch).cpu().numpy()

    def runtime_memory(self) -> dict[str, float | None]:
        return _runtime_memory(self._torch, self.device)


def load_sample(state_dir: Path, sample_size: int) -> list[SampleAsset]:
    database_path = state_dir / "index.sqlite"
    cache_root = (state_dir / "cache").resolve()
    if not database_path.is_file():
        raise FileNotFoundError(f"Latent index was not found: {database_path}")
    with sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            """
            SELECT a.id, a.name, a.capture_at, a.fingerprint, contact.relative_path
            FROM assets AS a
            JOIN cache_entries AS contact
              ON contact.asset_id = a.id AND contact.variant = 'contact'
            WHERE a.capture_at IS NOT NULL
            ORDER BY a.capture_at ASC, a.name ASC, a.id ASC
            """
        ).fetchall()
    records = [
        SampleAsset(
            asset_id=int(row[0]),
            name=str(row[1]),
            capture_at=str(row[2]),
            fingerprint=str(row[3]),
            contact_path=str(row[4]),
        )
        for row in rows
    ]
    if not records:
        raise RuntimeError("Latent index has no cached contact images with capture dates")
    selected = balanced_date_sample(records, sample_size)
    for asset in selected:
        path = (cache_root / asset.contact_path).resolve()
        if not path.is_relative_to(cache_root) or not path.is_file():
            raise FileNotFoundError(f"Cached contact image was not found: {asset.contact_path}")
    return selected


def balanced_date_sample(records: Sequence[SampleAsset], sample_size: int) -> list[SampleAsset]:
    if sample_size < 1:
        raise ValueError("sample size must be positive")
    if not records:
        return []
    if sample_size >= len(records):
        return list(records)
    groups: dict[str, list[SampleAsset]] = defaultdict(list)
    for record in records:
        groups[record.capture_date].append(record)
    dates = sorted(groups)
    chosen_dates = dates
    if sample_size < len(dates):
        chosen_dates = [dates[index] for index in evenly_spaced_indices(len(dates), sample_size)]
    base, remainder = divmod(sample_size, len(chosen_dates))
    extra_dates = set(evenly_spaced_indices(len(chosen_dates), remainder)) if remainder else set()
    selected = []
    for date_index, capture_date in enumerate(chosen_dates):
        group = groups[capture_date]
        allocation = min(len(group), base + (date_index in extra_dates))
        selected.extend(group[index] for index in evenly_spaced_indices(len(group), allocation))
    if len(selected) < sample_size:
        selected_ids = {record.asset_id for record in selected}
        remaining = [record for record in records if record.asset_id not in selected_ids]
        needed = sample_size - len(selected)
        selected.extend(remaining[index] for index in evenly_spaced_indices(len(remaining), needed))
    return sorted(selected, key=lambda item: (item.capture_at, item.name, item.asset_id))


def evenly_spaced_indices(total: int, count: int) -> list[int]:
    if count < 1 or total < 1:
        return []
    if count >= total:
        return list(range(total))
    if count == 1:
        return [total // 2]
    return [round(index * (total - 1) / (count - 1)) for index in range(count)]


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    import numpy as np

    state_dir = args.state_dir.expanduser().resolve()
    cache_dir = args.cache_dir.expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    sample = load_sample(state_dir, args.sample_size)
    contact_root = state_dir / "cache"
    paths = [(contact_root / asset.contact_path).resolve() for asset in sample]
    device = resolve_device(args.device)
    encoder = create_encoder(args.model, cache_dir=cache_dir, device=device)

    encoder.encode_images(paths[: args.batch_size], args.batch_size)
    image_runs = []
    image_embeddings = None
    for _ in range(args.repeats):
        image_started = time.perf_counter()
        image_embeddings = encoder.encode_images(paths, args.batch_size)
        image_runs.append(time.perf_counter() - image_started)
    assert image_embeddings is not None
    image_seconds = median(image_runs)
    text_started = time.perf_counter()
    text_embeddings = encoder.encode_texts(args.query)
    text_seconds = time.perf_counter() - text_started

    scores = text_embeddings @ image_embeddings.T
    result_sets = []
    rankings = []
    for query_index, query in enumerate(args.query):
        ranking = np.argsort(-scores[query_index], kind="stable")[: args.top_k]
        ranking_ids = ranking.tolist()
        rankings.append(ranking_ids)
        result_sets.append(
            {
                "query": query,
                "results": [
                    {
                        "rank": rank,
                        "score": float(scores[query_index, asset_index]),
                        **asdict(sample[asset_index]),
                    }
                    for rank, asset_index in enumerate(ranking_ids, start=1)
                ],
            }
        )

    query_pairs = []
    for left in range(0, len(args.query) - 1, 2):
        right = left + 1
        overlap = len(set(rankings[left]) & set(rankings[right]))
        query_pairs.append(
            {
                "query_a": args.query[left],
                "query_b": args.query[right],
                "text_embedding_cosine": float(text_embeddings[left] @ text_embeddings[right]),
                "result_overlap_count": overlap,
                "result_overlap_rate": overlap / args.top_k,
            }
        )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": args.model,
        "model_id": encoder.model_id,
        "device": device,
        "sample_count": len(sample),
        "sample_date_count": len({asset.capture_date for asset in sample}),
        "embedding_dimensions": int(image_embeddings.shape[1]),
        "batch_size": args.batch_size,
        "repeats": args.repeats,
        "top_k": args.top_k,
        "timings": {
            "model_load_seconds": encoder.load_seconds,
            "image_encode_seconds": image_seconds,
            "image_encode_runs_seconds": image_runs,
            "image_throughput_per_second": len(sample) / image_seconds,
            "text_encode_seconds": text_seconds,
        },
        "memory": encoder.runtime_memory(),
        "model_cache_bytes": directory_size(cache_dir),
        "query_pairs": query_pairs,
        "queries": result_sets,
    }


def create_encoder(model: str, *, cache_dir: Path, device: str) -> EmbeddingEncoder:
    if model == "mobileclip2-s2":
        return MobileCLIP2Encoder(cache_dir=cache_dir, device=device)
    if model == "siglip2-base":
        return SigLIP2Encoder(cache_dir=cache_dir, device=device)
    raise ValueError(f"unsupported model: {model}")


def resolve_device(requested: str) -> str:
    import torch

    if requested == "auto":
        return "mps" if torch.backends.mps.is_available() else "cpu"
    if requested == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("MPS was requested but is not available")
    return requested


def directory_size(path: Path) -> int:
    return sum(
        item.stat().st_size for item in path.rglob("*") if item.is_file() and not item.is_symlink()
    )


def write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _normalize(features: Any, torch: Any) -> Any:
    return torch.nn.functional.normalize(features, p=2, dim=-1)


def _feature_tensor(features: Any) -> Any:
    return getattr(features, "pooler_output", features)


def _runtime_memory(torch: Any, device: str) -> dict[str, float | None]:
    peak_rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform != "darwin":
        peak_rss *= 1024
    payload: dict[str, float | None] = {"peak_rss_mib": peak_rss / (1024**2)}
    if device == "mps":
        payload["mps_current_allocated_mib"] = torch.mps.current_allocated_memory() / (1024**2)
        payload["mps_driver_allocated_mib"] = torch.mps.driver_allocated_memory() / (1024**2)
    else:
        payload["mps_current_allocated_mib"] = None
        payload["mps_driver_allocated_mib"] = None
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--model",
        choices=("mobileclip2-s2", "siglip2-base"),
        required=True,
    )
    parser.add_argument("--sample-size", type=int, default=256)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    parser.add_argument("--query", action="append", default=[])
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if args.top_k < 1:
        parser.error("--top-k must be positive")
    if not args.query:
        args.query = list(DEFAULT_QUERIES)
    payload = run_benchmark(args)
    write_json_atomic(args.output.expanduser().resolve(), payload)
    print(json.dumps({key: payload[key] for key in payload if key != "queries"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
