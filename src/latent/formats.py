"""Photo formats accepted by local import and preview generation."""

import re

HEIF_EXTENSIONS = {"hif", "heic", "heif"}
JPEG_EXTENSIONS = {"jpg", "jpeg"}
# Latent previews only these RAW formats, so only they can represent a paired camera JPEG.
RAW_PHOTO_EXTENSIONS = {"arw"}
PHOTO_EXTENSIONS = RAW_PHOTO_EXTENSIONS | JPEG_EXTENSIONS | {"png", "tif", "tiff"} | HEIF_EXTENSIONS
SIDECAR_EXTENSIONS = {"dop", "xmp"}
# Images derived from an original are archived but never shown as photos: Sony video
# thumbnails (C0423T01.JPG) and DxO PhotoLab exports (DSC00605_DxO.jpg, DSC00605_DxO_1.tif).
VIDEO_THUMBNAIL_STEM = re.compile(r"^c\d+t\d+$", re.IGNORECASE)
DXO_EXPORT_SUFFIX = re.compile(r"_dxo(?:[-_\d].*)?$", re.IGNORECASE)


def is_derivative(stem: str) -> bool:
    return bool(VIDEO_THUMBNAIL_STEM.fullmatch(stem) or DXO_EXPORT_SUFFIX.search(stem))
