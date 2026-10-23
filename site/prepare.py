"""Prepare the sample archive on the project page.

Run it on a Mac where Latent's on-device search is set up, with a folder that holds the
photos named in site/sample.toml:

    uv run --locked python site/prepare.py ~/Pictures/latent-sample

Each photo goes through Latent's own preview code. EmbeddingGemma 2 encodes the 512-pixel
contact previews and the searches, and Latent's ranking code orders the results. The
script writes web images and those results to site/public, so the page needs no model.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms

from latent.cli import DEFAULT_STATE_DIR
from latent.embeddinggemma import (
    ARTIFACT_REVISION,
    EmbeddingGemmaEncoder,
    EmbeddingGemmaRuntime,
    load_runtime_config,
)
from latent.preview import decode_heif, render_variants
from latent.search import _varied_ranking

SITE = Path(__file__).resolve().parent
PUBLIC = SITE / "public"
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".heic", ".heif"}
GRID_WIDTH = 720
LARGE_EDGE = 2000
CAMERAS = {
    "ILCE-7RM2": "Sony α7R II",
    "ILCE-7RM5": "Sony α7R V",
    "ILCE-7CM2": "Sony α7C II",
}
EXIF_TAGS = ["DateTimeOriginal", "Model", "LensModel", "FocalLength", "FNumber",
             "ExposureTime", "ISO"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("photos", type=Path, help="folder with the photos in sample.toml")
    args = parser.parse_args()

    sample = tomllib.loads((SITE / "sample.toml").read_text(encoding="utf-8"))
    names = list(sample["photos"])
    sources = [find_photo(args.photos, name) for name in names]
    embeddings_root = DEFAULT_STATE_DIR / "embeddings"
    if load_runtime_config(embeddings_root) is None:
        sys.exit("Set up on-device search first with `latent local-encoder setup`.")

    exif = read_exif(sources)
    photos_dir = PUBLIC / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)
    for stale in photos_dir.glob("*.webp"):
        stale.unlink()

    photos = []
    with tempfile.TemporaryDirectory(prefix="latent-site-") as directory:
        contacts = []
        for name, source in zip(names, sources, strict=True):
            data = source.read_bytes()
            if source.suffix.lower() in {".heic", ".heif"}:
                data, _metadata = decode_heif(data, source.suffix.lower().lstrip("."))
            # The same path the app takes for a local photo: a 2560-pixel preview
            # for display and a 512-pixel contact preview for search.
            rendered = render_variants(data, {})
            contact = Path(directory) / f"{name}.jpg"
            contact.write_bytes(rendered.contact.data)
            contacts.append(contact)
            size = write_web_images(rendered.preview.data, photos_dir / name)
            photos.append(describe(name, sample["photos"][name], size, rendered.contact.data,
                                   exif[source]))
            if name == sample["link_preview"]:
                write_link_preview(rendered.preview.data, PUBLIC / "link-preview.jpg")
            print(f"Rendered {name}", file=sys.stderr)

        runtime = EmbeddingGemmaRuntime(embeddings_root)
        encoder = EmbeddingGemmaEncoder(runtime)
        try:
            images = encoder.encode_images(contacts, batch_size=1)
            texts = encoder.encode_texts([search["text"] for search in sample["searches"]])
        finally:
            runtime.close()

    # Latent stores vectors as float16, so rank with the values search would read.
    matrix = np.asarray(images, dtype=np.float16).astype(np.float32)
    queries = np.asarray(texts, dtype=np.float16).astype(np.float32)
    queries /= np.linalg.vector_norm(queries, axis=1, keepdims=True)

    searches = []
    for search, vector in zip(sample["searches"], queries, strict=True):
        scores = matrix @ vector
        searches.append({"text": search["text"], "lang": search.get("lang"),
                         "note": search.get("note"), **ranked(matrix, scores)})
    similar = []
    for position in range(len(names)):
        scores = matrix @ matrix[position]
        scores[position] = np.nan
        similar.append(ranked(matrix, scores))

    first = [search["text"] for search in sample["searches"]].index(sample["first"])
    write_archive(PUBLIC / "archive.js", {
        "engine": {"model": "EmbeddingGemma 2", "revision": ARTIFACT_REVISION[:12]},
        "first": first,
        "photos": photos,
        "searches": searches,
        "similar": similar,
    })
    print(f"Wrote {len(photos)} photos and {len(searches)} searches to {PUBLIC}", file=sys.stderr)
    return 0


def find_photo(folder: Path, name: str) -> Path:
    matches = [
        path for path in folder.iterdir()
        if path.stem in {name, f"{name}_DxO"} and path.suffix.lower() in PHOTO_SUFFIXES
    ]
    if len(matches) != 1:
        sys.exit(f"Expected one photo named {name} in {folder}, found {len(matches)}.")
    return matches[0]


def read_exif(sources: list[Path]) -> dict[Path, dict]:
    result = subprocess.run(
        ["exiftool", "-j", "-n", *(f"-{tag}" for tag in EXIF_TAGS), *map(str, sources)],
        capture_output=True, text=True, timeout=120, check=True,
    )
    return {Path(item["SourceFile"]): item for item in json.loads(result.stdout)}


def write_web_images(preview: bytes, stem: Path) -> tuple[int, int]:
    """Write a grid image and a large image. Only the color profile is kept."""
    with Image.open(io.BytesIO(preview)) as opened:
        image = opened.convert("RGB")
        icc_profile = opened.info.get("icc_profile")
    options = {"method": 6, "icc_profile": icc_profile} if icc_profile else {"method": 6}
    large = image.copy()
    large.thumbnail((LARGE_EDGE, LARGE_EDGE), Image.Resampling.LANCZOS)
    large.save(stem.with_name(f"{stem.name}-large.webp"), quality=84, **options)
    grid = image.resize(
        (GRID_WIDTH, round(image.height * GRID_WIDTH / image.width)), Image.Resampling.LANCZOS
    )
    grid.save(stem.with_name(f"{stem.name}-grid.webp"), quality=80, **options)
    return large.size


def write_link_preview(preview: bytes, path: Path) -> None:
    """Crop a 1200 by 630 sRGB image for link previews, which often ignore color profiles."""
    with Image.open(io.BytesIO(preview)) as opened:
        image = opened.convert("RGB")
        icc_profile = opened.info.get("icc_profile")
    if icc_profile:
        image = ImageCms.profileToProfile(
            image, ImageCms.ImageCmsProfile(io.BytesIO(icc_profile)),
            ImageCms.createProfile("sRGB"), outputMode="RGB",
        )
    scale = max(1200 / image.width, 630 / image.height)
    image = image.resize((round(image.width * scale), round(image.height * scale)),
                         Image.Resampling.LANCZOS)
    left, top = (image.width - 1200) // 2, (image.height - 630) // 2
    image.crop((left, top, left + 1200, top + 630)).save(path, quality=86, optimize=True)


def describe(name: str, alt: str, size: tuple[int, int], contact: bytes, exif: dict) -> dict:
    with Image.open(io.BytesIO(contact)) as opened:
        red, green, blue = opened.convert("RGB").resize((1, 1), Image.Resampling.BOX).getpixel(
            (0, 0)
        )
    taken = datetime.strptime(exif["DateTimeOriginal"], "%Y:%m:%d %H:%M:%S")
    return {
        "id": name,
        "alt": alt,
        "width": size[0],
        "height": size[1],
        "color": f"#{red:02x}{green:02x}{blue:02x}",
        "day": f"{taken:%b} {taken.day}, {taken.year}",
        "camera": CAMERAS.get(exif.get("Model"), exif.get("Model")),
        "lens": exif.get("LensModel"),
        "focal": f"{exif['FocalLength']:g} mm" if "FocalLength" in exif else None,
        "exposure": exposure_summary(exif),
    }


def exposure_summary(exif: dict) -> str:
    """Format exposure like the app's photo details: 1/50 s · ƒ/4.5 · ISO 2000."""
    parts = []
    exposure = exif.get("ExposureTime")
    if exposure:
        reciprocal = 1 / exposure
        denominator = round(reciprocal)
        if exposure <= 0.5 and abs(denominator - reciprocal) <= reciprocal * 0.01:
            parts.append(f"1/{denominator} s")
        else:
            parts.append(f"{exposure:g} s")
    if exif.get("FNumber"):
        parts.append(f"ƒ/{exif['FNumber']:g}")
    if exif.get("ISO"):
        parts.append(f"ISO {exif['ISO']:g}")
    return " · ".join(parts)


def ranked(matrix: np.ndarray, scores: np.ndarray) -> dict:
    """Order photos the way Latent's search does for each of its three sort orders."""
    closest = np.argsort(-scores, kind="stable")
    closest = closest[np.isfinite(scores[closest])]
    least = np.argsort(scores, kind="stable")
    least = least[np.isfinite(scores[least])]
    variety = _varied_ranking(matrix, scores, closest, len(closest))
    return {
        "scores": [None if not np.isfinite(score) else round(float(score), 4)
                   for score in scores],
        "orders": {
            "closest": closest.tolist(),
            "least_similar": least.tolist(),
            "variety": variety.tolist(),
        },
    }


def write_archive(path: Path, archive: dict) -> None:
    def line(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    lists = ("photos", "searches", "similar")
    head = {key: value for key, value in archive.items() if key not in lists}
    body = ",\n".join(
        [f"  {json.dumps(key)}: {line(value)}" for key, value in head.items()]
        + [f"  {json.dumps(key)}: [\n" + ",\n".join(f"    {line(item)}" for item in archive[key])
           + "\n  ]" for key in lists]
    )
    path.write_text(
        "// Generated by site/prepare.py from site/sample.toml. Do not edit by hand.\n"
        f"export default {{\n{body}\n}};\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
