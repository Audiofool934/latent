# Project status

Updated: 2026-10-06.
Live figures come from read-only API checks of the running service at about 16:20 UTC.

This page records Latent's current state.
Dated progress notes from August 31 to October 4 remain in git history (`git log -p -- docs/project-status.md`).
Feature behavior and verification detail live in the linked guides.

## Ownership

Everett took over development from a previous agent on 2026-10-06.
That agent had left the work from September 4 to October 6 uncommitted.
It is now in commits `97c477a` through `a661a2a`, each of which passes the test suite.
The takeover fixed a failing folder-workflow test helper and a macOS test script that could not run under Xcode.
Claims in older notes that this page does not repeat should be treated as unverified.
The repository has no git remote.

## Live runtime

The app in use is `var/native/Latent.app`, version 0.1.1 (build 3), an ad hoc signed development build.
The previous agent built it at 15:50 UTC on October 6, after the last Swift source change; it has not been rebuilt since.
Opening it from Finder or the Dock starts a service on `127.0.0.1:8766` using this checkout's `.venv/bin/python`, and quitting the app stops that service.
The service imports `src/latent` from this checkout when it starts, so source changes reach the live library on the next app launch.
See [native macOS client](native-macos.md) for build, launch, and ownership details.

| Data | Location |
| --- | --- |
| Catalog and preview caches (5.4 GB) | `~/Library/Application Support/Latent/phase0/` |
| Gemini vectors | `~/Library/Application Support/Latent/phase0/embeddings/gemini-embedding-2/` |
| Workspace: Sequences, annotations, imports, Locations, search history, timelapse groups | `~/Library/Application Support/Latent/workspace/` |
| Editing batches | `~/Pictures/Latent/` |
| Native unsaved-edit drafts | `~/Library/Application Support/Latent/pending-edits/` |

The library identity reported by `/health` matches the previous agent's October 5 and 6 receipts.
Semantic search needs `GEMINI_API_KEY` in the service environment.
It is currently set in the login session's launchd environment, but no LaunchAgent sets it.
After logging out or restarting, an app-launched service will probably lack the key until it is set again.

## Library

| Item | Live value |
| --- | ---: |
| Catalog photos | 39,078 |
| Photos with Gemini vectors | 37,623 |
| Photos without vectors | 1,455 |
| Embedding jobs pending, running, or failed | 0 |
| Sequences | 2, in 1 folder, holding 12 photos |
| Confirmed timelapse groups | 38, holding 9,870 photos |
| Editing batches | 2 |
| Trash batches | 0 |

The 1,455 photos without vectors all come from the October 6 imports.
Embedding runs only start after an explicit review in **AI Search**, and none has been created yet.
At the configured estimate of $0.00012 per image, embedding all 2,683 imported photos would cost about $0.32.

The catalog holds 25,793 Sony ARW files indexed through September 27, plus 11,830 JPEGs added by the October 5 [JPEG backfill](jpeg-backfill.md).
Timeline collapses the confirmed [timelapse groups](timelapse-audit.md) into single entries.
Editing batch `2026-10-04-98abb2ed` is cloud verified, and `2026-10-05-3b7c3e12` is ready for editing.

## Imports in progress

Batch `cd985f45` (07:00 UTC) imported camera folders `r2` and `r5` from `/Volumes/Audioroom`.
Its 341 `r2` photos are ready.
All 2,342 `r5` previews failed with "Folder or volume changed" after the volume remounted with a new device number.
Batch `b93caae7` (16:05 UTC) imports `r5` again and had 1,380 of 2,342 previews ready at this check.
No archive copy has been made for either batch.

## Pending decisions

- The JPEG backfill held back 8,943 timelapse frames in 19 folders and 619 other JPEGs in 47 folders.
  Lists are in `var/reports/jpeg-backfill-20261005/`.
- 38 JPEGs from folders dated 2025-05-23 to 2025-05-26 carry 2015-01-01 camera dates.
  They currently appear in January 2015, with EXIF unchanged; whether to prefer folder dates is undecided.
- Embeddings for the two October 6 imports need an explicit AI Search review.
- Batch `cd985f45` still reports 2,342 failed previews that `b93caae7` replaces.

## Known issues

- Local-folder sources are identified by device and inode.
  macOS can assign a new device number when an external volume remounts, which makes Latent treat the folder as changed.
  CloudDrive folder identity was made stable across remounts on October 5; local volumes still need the equivalent.
- The Gemini key is not persisted for app-launched services, as described above.
- The app depends on this checkout's `.venv` and does not bundle Python.
  Moving the project or recreating the environment requires rebuilding the app.
- `ruff format` is not enforced, and 19 files differ from its output.
- macOS 27 behavior is unverified.

## Verification baseline

Checked on 2026-10-07 with the local search engine added:

| Check | Result |
| --- | --- |
| `uv run --frozen pytest` | 223 passed, including the Node navigation regressions |
| `uv run --frozen ruff check .` | Clean |
| `scripts/test-macos.sh` (Xcode, Swift 6.4) | 138 passed, 1 opt-in test skipped |
| `LATENT_TEST_PYTHON=$PWD/.venv/bin/python scripts/test-macos.sh --filter nativeOwnerStarts` | Passed |

The opt-in test starts, reuses, and stops a real Python service against temporary data.

## Local evidence

`var/` is ignored by git.
`var/backups/` holds the real catalog, workspace, embedding, and app snapshots taken around each change; its `README.md` lists them.
`var/reports/` holds the JPEG backfill and timelapse review lists.
The previous agent's other QA screenshots, logs, and fixtures are not kept.

## Milestones

Dates follow the earlier notes, which used Asia/Singapore time.

| Date | Milestone |
| --- | --- |
| 2026-08-31 | Phase 0: read-only CloudDrive metadata, embedded ARW previews, SQLite catalog, resumable archive scans ([results](phase-0-results.md)) |
| 2026-09-02 | Contact-sheet pagination, local encoder benchmark, resumable embedding queue ([phase 1](phase-1-library-slice.md)) |
| 2026-09-04 | Grounded Curator, cross-year motifs, durable Sequence workspace ([workspace](writable-workspace.md)) |
| 2026-09-05 | Gemini Embedding 2 replaces local models ([Gemini embeddings](gemini-embeddings.md)) |
| 2026-09-27 | Full index of 25,793 ARW files complete |
| 2026-10-01 | Native macOS client; More variety search |
| 2026-10-04 | Grading keys, inline preview, reference-image search, nested Sequence folders (schemas 3 and 4), editing in `~/Pictures/Latent`, Locations, filters, Trash ([editing](archived-photo-editing.md)) |
| 2026-10-05 | JPEG backfill, timelapse organizer, review controls and info panel, search history |
| 2026-10-06 | Unified [Imports](import-workflow.md), Sequence drag and drop, app-owned service startup |

## Local search engine

Since 2026-10-07, Latent can search with [EmbeddingGemma 2 on this Mac](local-embeddings.md) as an alternative to Gemini.
It uses a separate index and becomes active only after validation and an explicit switch.
The live library has the local encoder configured since 2026-10-07, but no local index yet, so Gemini remains active.
Latent uses a self-contained copy of the llama.cpp build from the support commit, in `~/Library/Application Support/Latent/llama.cpp/4fbc76dec51d/`, with relative library paths.
The pinned model files are APFS clones in `~/Library/Application Support/Latent/models/embeddinggemma-2-ba3888272494/`.
Homebrew's `llama.cpp` 0.6.0 cannot load the model yet, and its `--HEAD` build fails against Homebrew's `ggml` 0.26.0.
A full local index of the current 40,306 photos would take about 7.7 hours at the measured 1.45 photos per second.

## Candidate next work

These are open options for Everett to prioritize, not commitments.

- Keep local-folder sources stable across volume remounts.
- Persist the Gemini key for app-launched services.
- Resolve the JPEG backfill holds and the 2015-dated JPEGs.
- Judge search quality on a small set of real queries, then consider date or camera constraints.
- Plan standalone service packaging so the app no longer depends on this checkout.
