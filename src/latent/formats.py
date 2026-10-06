"""Photo formats accepted by local import and preview generation."""

HEIF_EXTENSIONS = {"hif", "heic", "heif"}
PHOTO_EXTENSIONS = {"arw", "jpg", "jpeg", "png", "tif", "tiff"} | HEIF_EXTENSIONS
SIDECAR_EXTENSIONS = {"dop", "xmp"}
