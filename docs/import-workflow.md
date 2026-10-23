# Daily photo imports

Use **Imports** in the native sidebar, or **View > Import photos** (`Shift-Command-I`).
The daily workflow treats folders from different cameras as new shoots, without running whole-library deduplication.
Historical cross-drive consolidation remains a separate advanced workflow.
**Locations** manages connected source paths, finished-photo output, and sequence links.
Its **Open Imports** button leads to the single place for adding photo folders and building previews.

## Import and review

Select one or more camera folders in **Add folders**, then choose **Build previews**.
The batch records its source folders and file inventory before starting.

### Folder scan

The inventory is built by a folder scan in the service, so **Add folders** returns at once.
A cloud or network folder can take minutes to list; the batch shows the folder being read and the files and photos found so far.
The scan holds no Locations lock, so Locations and the import list stay readable while it runs.
Other imports and Locations changes wait until the scan finishes or is canceled.
**Cancel scan** discards the batch after the current folder listing returns; the chosen folders stay in Locations.
A scan that finds no supported photos, exceeds 100,000 files, or cannot read a folder ends as **Scan failed** with the reason.
Quitting Latent stops a scan, and the next launch reports it as stopped.
Choose **Remove** for a failed or stopped scan, then add the folders again.

### Previews

ARW, JPEG, HEIF/HIF, PNG and TIFF receive local previews; DOP and XMP files are kept with their originals for archiving.
HEIF decoding uses macOS ImageIO through `sips`, and ExifTool preserves capture metadata.
Matching local previews, including previews built by the former Locations scanner, are reused and counted in the batch.
Selecting the same unchanged folders reopens their existing preview batch; new or changed files create a new inventory while reusing matching previews.
The legacy scan API delegates to the same import worker, which scans the folder and then builds its previews, so its work appears and can be paused in Imports.

A JPEG beside an ARW with the same filename stem, such as `DSC00001.JPG` and `DSC00001.ARW`, is the camera's JPEG of that shot.
The ARW shows the shot in the library with its embedded preview, and the JPEG gets no separate photo or preview.
The batch counts these pairs, and the JPEG is still archived with its RAW.
Sony video thumbnails such as `C0423T01.JPG` and DxO exports such as `DSC00605_DxO.jpg` or `DSC00605_DxO_1.tif` are derived files.
They get no photo or preview, but they are archived.
A DxO export is archived beside the RAW it came from; one without its RAW in the folder, and every video thumbnail, goes to `Undated`, as videos do.
Other JPEGs stay separate photos, including photos in time-lapse folders.

Folders on a CloudDrive mount are read through CloudDrive's API, not through the mount.
An ARW reads only its header and embedded preview, in exact byte ranges.
A JPEG reads its EXIF header (64 KiB, expanding to at most 512 KiB) and CloudDrive's display-ready preview (at most 4 MiB).
If CloudDrive offers no preview, that photo fails with an explicit message and can be retried; it never falls back to downloading the original.
HEIF, PNG and TIFF files are still read in full.
Local folders are read directly.

**View photos** opens only that batch's available photos, including while previews are still being generated.
Ratings, picks, rejection flags, captions and sequences use the same stable photo identities as the rest of the library.
**Refresh photos** brings newly available previews into the batch gallery without silently changing a selection during review.
Pause and resume are explicit, and restarting the service leaves interrupted work available to resume.
Failed previews are listed and can be retried separately.

## Archive originals

Choose a separate archive folder, review its path and file count, then choose **Copy & verify**.
The destination can be a local or external disk, or a mounted CloudDrive folder recognized by the existing Locations integration.
The layout is `YYYY/YYYY-MM-DD/source-folder/original-relative-path`.
Photos without a capture date go into `Undated`, with the count shown before confirmation.
Sidecars and paired camera JPEGs follow their associated photo's date and any collision rename.
DxO exports follow their RAW's date and folder but keep their own names.

Copies never replace a different existing file.
Local copies are checked by SHA-256; CloudDrive copies additionally require a matching cloud hash and size before they are marked verified.
Only one file is copied at a time, and a pending cloud confirmation stops further copying until resumed.
This limits unconfirmed uploads on a Mac with little free disk space.
The source file is always preserved; this workflow does not delete card contents or clear CloudDrive's cache.

Each verified archive copy retains its original catalog identity.
Later scans skip registered copies, and editing can retrieve the verified archive when the original portable drive is offline.
The recorded archive destination becomes fixed once copying has started.

## Explicit photo embeddings

The **AI Search** tab offers all photos, incremental missing embeddings, or the current gallery selection.
An individual import also offers **Review AI search** for its ready photos.
Reviewing creates a durable local plan with photo count, existing embeddings, preview bytes and estimated API cost.
It makes no embedding API request.
The user must press **Generate** to send that selection's small JPEG previews to Gemini.
Existing vectors are reused, and jobs outside the frozen selection are neither processed nor pruned.
Removed or changed photos invalidate the reviewed selection before another generation request starts.

Generation can pause after the current request and resume after another explicit confirmation.
An import, archive copy, service restart or application launch never starts photo embeddings automatically.
The estimate uses the configured per-image rate and excludes retry overhead; actual API charges may differ.

## State and verification

Import batches and files are stored in the workspace's `imports.sqlite`.
Verified copy receipts are stored in the catalog's `archive-copies.sqlite`.
Embedding plans and progress are stored under `workspace/embedding-runs/<request-id>/`.
All mutations require the current library identity; actions also require the reviewed batch or plan revision.

`tests/test_import_workflow.py` covers two-camera imports, background folder scans with progress, cancellation and restart reporting, collision names and sidecars, pause/restart recovery, cloud confirmation backpressure, source changes, hidden photos, offline archive editing, scoped embeddings and HTTP request boundaries.
Native UI verification uses a separate catalog, workspace, settings suite and app bundle, with real RAW, JPEG and HIF samples copied into the test directory.
Actual cloud transfer must be verified against the intended mounted destination; a passing fake-provider test is not evidence that a real upload completed.
