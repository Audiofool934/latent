# Adding JPEG originals to an existing CloudDrive library

The original `scan` and `scan-tree` commands catalog ARW files.
Use `scan-jpeg` to add independent JPEG originals without rebuilding the existing RAW library.
The native client displays these photos through the same Library, ratings, Sequences, and editing interfaces.

## Preview the additions

Pass the CloudDrive API path of a photo directory, and repeat `--path` for additional dates.
The default operation only lists remote metadata and reads the local catalog.
It does not download original images, change the catalog, or call an AI provider.

```bash
uv run latent scan-jpeg \
  --path '/CloudName/archive/2025/2025-06-01' \
  --recursive --report ./jpeg-plan.json --json
```

JPEG and RAW files with the same folder and case-insensitive filename stem are treated as pairs, leaving the RAW entry in the library.
The same filename on a different date remains independent.
A possible RAW match elsewhere within the same date folder is held for review.
DxO exports, named LRT/time-lapse output, video thumbnails, and nested-folder JPEGs are also held for review.
These are conservative filename and folder checks, not a claim that every derivative has been identified.
Identical JPEG content hashes are reported as copies rather than separate new candidates.
Already indexed sources are retained; changed sources require review.
The JSON report records each decision and source path, including files that were held back.
`--recursive` inspects nested folders for this report without automatically importing their images.

## Add and resume previews

Repeat the same scan with `--apply` to enqueue only its independent candidates.

```bash
uv run latent scan-jpeg \
  --path '/CloudName/archive/2025/2025-06-01' \
  --recursive --apply --report ./jpeg-applied.json --json
uv run latent work --max-jobs 25 --json
```

Repeat `work` to continue the durable queue.
Use `--retry-failed` for an intentional retry after resolving a provider failure.
Completed unchanged photos retain their identities, previews, and queue state.
The worker reads JPEG/MPO EXIF from a 64 KiB header, expanding only when needed up to 512 KiB.
It downloads the provider's display-ready preview with a 4 MiB limit, without falling back to a full original-image download.
Missing or oversized cloud previews fail explicitly and can be retried later.
Only generated contact and display previews are cached locally.
The original EXIF capture time, camera, lens, and orientation remain in the catalog.
Provider preview URLs remain in memory and are not saved in the catalog or report.

Refresh the native Library after adding photos to reload its date sidebar.
New photos can be browsed and organized immediately after their previews are ready.
Gemini semantic-search coverage is separate: `embedding-sync` prepares the local queue, and `embedding-build` uploads contact previews and incurs the configured provider's charges.
The existing embedding commands remain incremental and do not re-encode successful unchanged RAW entries.

The filesystem **Scan for photos** flow still reads full JPEG files from the selected folder.
Use this bounded CloudDrive backfill for a large cloud archive.

## Review regular interval shooting

Folder and filename hints can miss time-lapse sequences stored alongside ordinary photos.
Use the [local EXIF time-lapse audit](timelapse-audit.md) to identify candidate shooting segments among already indexed RAW and JPEG photos.
It groups equipment and separates shooting stages before measuring cadence, and reports uncertain device identity explicitly.
The audit produces a review report without changing the JPEG import decisions or existing Library contents.
