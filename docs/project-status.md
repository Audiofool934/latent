# Project status

Updated: 2026-10-04, Asia/Singapore.
The native workflow update is built and ad hoc signed, and its matching local service has been activated on port 8766.
The gallery release cleanup passed visual acceptance and was activated on October 3.
The live status endpoint reports subsequent library changes.

## October 4 Pictures editing folders

New RAW editing batches use `~/Pictures/Latent/<date>-<short-batch-id>/`, with no extra `files` directory.
Batch manifests stay in the writable workspace and persist their working-folder locations across restarts and changes to the service's `--editing-dir` setting.
Existing batches remain in place until explicitly relocated with the editor and service closed.
The current two-photo batch was moved to `~/Pictures/Latent/2026-10-04-98abb2ed/`; both RAWs and all four sidecars matched their original hashes, and its ready state and batch identity were preserved.

Open in PhotoLab now sends the working folder instead of separate RAW file URLs.
The old request was reproduced leaving PhotoLab at `0/0 images`; the updated signed app selected the new batch directory and showed both images in the filmstrip.
The matching port-8766 service is active with the same library identity and the Pictures destination for future batches.
Validation passed 116 Python tests, Ruff, and all 113 native tests in 13 suites in the actual checkout.
The previously documented ownership-test compile blocker was fixed in the separate, bounded test-fixture repair before this change; the earlier isolated-copy results below are historical.
Migration receipts, manifest backup, and test/build logs are under `var/pictures-editing-20261004/`.

## October 4 Sequence folders

Sequence organization now supports arbitrary user-defined folder depth, native expand/collapse controls, persisted expansion per library, and a breadcrumb manager for navigating deep hierarchies.
Creation, renaming, movement, and folder removal are available through context menus; movement uses an explicit destination picker.
Removing a folder promotes its contents one level and retains every sequence and ordered photo reference.
The Add to sequence menu follows the same hierarchy, and sequence drafts retain their chosen destination and full photo selection.
Photo additions now respect the service's 100-photo limit as well as its 64 KiB body limit.

The Python suite passes 114 tests and Ruff passes.
The isolated native validation copy passes 113 tests with the previously documented test-only workaround; the original unrelated concurrency test remains unchanged.
Native checks covered three nested folders, creation in the deepest folder, rename, collapse/expand, restart persistence, nested photo addition, and moving a sequence without changing its photos.
Tests also exercise 1,100 folder levels, concurrent cycle prevention, root moves, old-schema migration, and hierarchy export/import.
The matching service and ad hoc signed release have been activated against the real library.
Schema 3 migrated to 4 after a consistent SQLite backup; the existing two sequences and twelve ordered references remain at the root, with annotations, three ready editing batches, and all 25,793 vectors preserved.
SQLite integrity and library identity were verified after migration, and the real native manager was visually checked without creating or reclassifying user content.
Task receipts and the migration backup are under `var/sequence-hierarchy-20261004/`.

## October 4 grading and image search

Preview now fills the main image area above the metadata shelf, preserving the sidebar.
Space opens and closes it, 0-5 changes ratings, W/E moves through photos, and Q/R controls independent Pick/Reject flags with the requested two-step removal of an existing Pick.
Shift-click selects a displayed range, Cmd-click toggles individual photos, and New sequence retains the whole ordered selection across partial-save recovery.
The transparent toolbar contains global search without the Latent title.
Reference-image search accepts phone images and optional text, with Most similar, Least similar, and More variety sorting over the existing embedding index.
Image preparation and cache behavior are documented in [Gemini embeddings](gemini-embeddings.md).

The real workspace migrated from schema 2 to 3 after a consistent SQLite backup.
The two existing annotation records, two Sequences with twelve items, and three ready editing batches were preserved exactly; library identity is unchanged and SQLite integrity passes.
All 25,793 vectors remain complete, with no pending, running, stale, or failed jobs.
Editing batches already live under `~/Library/Application Support/Latent/workspace/editing/`; the native Editing view now exposes the actual path.
The application remains at `var/native/Latent.app`; neither `/Applications/Latent.app` nor `~/Applications/Latent.app` was present at this check.

The full Python suite passes 109 tests and Ruff passes.
An isolated native validation copy passes 108 tests after a test-only workaround for a pre-existing Swift 6.2 concurrency error in `GalleryOwnershipTests.swift`; the original unrelated test is unchanged pending authorization.
The release application builds successfully without that workaround.
Synthetic native GUI checks cover the requested grading keys, inline preview, complete two-photo Sequence creation, and image search across all three orderings without real annotation changes or provider calls.
After activation, a generated 512-pixel reference exercised the real Gemini image-query path and all three orders against the complete library.
Most-similar and least-similar each returned 100 correctly ordered, disjoint results; varied order preserved the strongest first match and reused the query cache.
The final native application also passed read-only real-library checks of inline preview, W/E navigation, Space dismissal, and the transparent search toolbar.
Local receipts and the pre-migration backup are under `var/workflow-qa-20261003/`.

## October 2 implementation continuation

This section records the earlier implementation and acceptance sequence; the October 4 state above supersedes its pending handoff notes.

The web date sidebar now follows the active result mode during global search, including the mobile selector.
Clearing search returns to the previously browsed date.
Seven JavaScript regressions and desktop/mobile browser checks passed using generated images, including date filters, empty results, repeated input, and navigation while results were pending.

The native client verifies service readiness and library identity, retains external-service ownership by default, and supports an explicitly configured owned Python child.
Ownership tests exercise an isolated Swift-to-Python launch, reuse, and shutdown without real photos.
No Python runtime was installed or bundled.

Startup metadata no longer replaces a newer navigation choice, and Starred invalidates stale pagination after edits while preserving usable selection.
Ratings and captions retain the latest intended value per photo in atomic local drafts, including across failed requests and app restart.
Restored or failed edits require explicit retry, with library and photo identities checked before writes.

Sequence sheets retain session drafts across dismissal and reconnect, reuse uncertain creations, and retry only changed fields without duplicate sequences.
Editing sheets retain choices for the same review and check the current batch before another action.
Library and review checks prevent a retained draft or an obsolete action from writing into a different context.
See [native macOS client](native-macos.md) for recovery behavior and limits.

Preview navigation now crosses the 250-photo Library and Starred page boundary while retaining the current photo during loading or failure.
Repeated Next input shares the pending request and advances once; dismissal or changed results invalidate its navigation.
Explicit retry clears its own error banner, and finite search, similarity, and Sequence results do not request unrelated library pages.

The cumulative isolated suite passes 102 native checks and 105 Python tests, plus Ruff and the web navigation checks.
Independent review covered service ownership, annotation races, draft identity, partial saves, editing retries, and preview pagination races.
The morning acceptance fixture contains 260 generated JPEGs, its own catalog and workspace, and synthetic editing actions that only update fixture JSON.
It never calls an encoder or provider or transfers real photos.

These source changes have not replaced the retained Python process or user app.
The last read-only embedding-status check still showed all 25,793 vectors complete, with zero pending, running, stale, or failed jobs.
No reindex, paid Gemini request, cloud upload, original-photo edit, real annotation change, commit, push, or user-service restart was performed for this continuation.
All pre-existing uncommitted work was preserved with fresh source hashes checked before each owned patch.

The Mac was available for synthetic native GUI validation on October 3.
That run verified offline recovery, preview pagination and retry, sequence recovery, editing-action reconciliation, durable annotation retry, Starred membership, and search navigation.
Follow-up fixes keep retained Editing accessible while offline and avoid misleading disconnected wording for restored edits.
The first gallery fix cleared layout and reuse state, but the targeted GUI retest still showed the same card behind empty and failed searches.
End-display cleanup handles hidden cached items, but did not resolve the visible ghost.
Paired screenshots and passive view/layer checkpoints then identified the surviving `PhotoCell` under the collection after its `PhotoItem` had deallocated, with no end-display callback after its last configuration.
Item teardown now retires and detaches its own loaded view on the main actor.
Image loading keeps the owner weak across both awaits so a held preview cannot prevent that teardown; another cell sharing the request continues loading.
Three new regressions cover attached-view release with a retained sibling, contact/preview requests held during release, and off-actor final release.
The existing lifecycle checks still cover real scrolling, empty results, and redisplay; headless tests do not validate composited pixels.
The rebuilt QA app needs targeted visual acceptance for missing/error search, recovered results, and resize.
The original synthetic QA profile and GUI evidence are preserved for that retest.
After acceptance, arrange controlled activation of matching service and app versions through their owner; newer app readiness checks intentionally reject the retained older service contract.
The integrated release candidate is built and ad hoc signed in an isolated workspace.
After native visual acceptance, the next useful slice is a bounded standalone-service packaging plan; thumbnail resolution changes need visual evidence first.

## Current slice

All 25,793 photos in the current catalog have valid Gemini Embedding 2 vectors.
There are no pending, running, stale, or failed embedding jobs.
The library covers 100 capture dates: 18,257 photos from 2025 and 7,536 from 2026.
Every asset identity and fingerprint matched the catalog, every contact preview was present, and every 3,072-dimensional vector was finite and normalized within float16 precision.
SQLite integrity checks passed.
The immutable archive, rebuildable local index, and separate Sequence workspace retain their existing boundaries.

The September 27 worker completed the remaining queue.
Its final segment processed 17,697 photos in 1,720.227 seconds without retries or provider errors.
The final report is `var/resume-20260927/index-result.json`; earlier interrupted September 5 runs do not represent current completion.
No embedding worker was restarted on October 1 because the index is complete.

## October 1 search and visual discovery

Semantic search now offers a **More variety** toggle.
The default remains closest-match order; the toggle balances relevance with visual differences so burst frames do not dominate the first results.
The strongest match stays first, and displayed scores remain query cosine similarities rather than reranking scores.
The setting is retained in the search URL as `variety=1`, survives reloads, and can be toggled by mouse, touch, or keyboard.
It only applies to semantic search; date browsing, similar-photo results, motifs, and saved Sequence ordering are unchanged.

The local reranker examines at most the 500 closest candidates and greedily balances 80% query relevance with a 20% penalty for similarity to already selected results.
The candidate pool is independent of requested result count, so shorter responses are prefixes of longer ones.
It does not upload photos, generate replacement vectors, or make an additional model request beyond the normal query embedding.
Repeated queries use the existing in-memory Gemini query cache.
Variety can trade some relevance for exploration; it does not remove duplicates from the archive or guarantee a different date for every result.

Measured against the completed library, the first 12 results covered these capture dates:

| Query | Closest order | More variety |
| --- | ---: | ---: |
| 雪山 | 1 | 3 |
| 夜晚的城市灯光 | 3 | 11 |
| 骑摩托车的人 | 4 | 10 |

Mean pairwise image similarity decreased for all three samples, while the first match stayed unchanged.
Warm varied-search responses took 0.156-0.198 seconds in this run; initial query/API timing is separate and varies with the provider.
These observations are a small retrieval check, not a labeled accuracy benchmark.
The snow query includes general winter scenes as well as mountain views, so exact subject precision still warrants user evaluation.

The existing motif discovery covered all 25,793 photos in 12 groups, with 10 groups spanning both years.
Measured discovery took 3.10 seconds initially and 0.54 seconds with clusters cached.
Motif grouping and evidence remain local and unlabeled.

## Open and inspect

- Native app: `var/native/Latent.app`; build, usage, and performance evidence are in [native macOS client](native-macos.md).
- Library: [http://127.0.0.1:8766](http://127.0.0.1:8766).
- Local status: [embedding status](http://127.0.0.1:8766/api/embedding-status).
- API contract and recovery: [Gemini embeddings](gemini-embeddings.md).
- Sequence persistence and import/export: [writable workspace](writable-workspace.md).

```bash
.venv/bin/python -m latent embedding-status --json
tmux list-panes -t latent-library-server -F '#{pane_pid} #{pane_current_command}'
```

Verification artifacts are under `var/verify-20261001/`.
`index-audit.json` records catalog/vector checks, `api-checks.json` records coverage and motif timing, and `variety-evaluation.json` records both retrieval orderings.
Desktop and mobile screenshots record the actual browser state before and after the toggle.
These local artifacts are ignored by Git and contain private library metadata.

## Runtime ownership

On October 1, no previous Latent server, embedding worker, or tmux session was running.
The Library was restarted on port 8766 in tmux session `latent-library-server` and reloaded with the search change.
This server intentionally stays running for the user to browse and evaluate results; it makes no API calls while idle.
The user or a later authorized task can stop it with `tmux kill-session -t latent-library-server` when it is no longer needed.
No recurring scheduler or indexing restart loop was installed.
The task-owned Chrome session `latent-verify-20261001` was stopped after browser verification, and its process ownership/check results are saved with the verification artifacts.

## Validation and next work

The October 1 Python/web baseline passed 79 tests, Ruff, JavaScript syntax checks, and diff whitespace checks.
Regression tests cover redundant-frame reranking, unchanged relevance scores, deterministic prefixes, candidate bounds, exclusions, and API parameter validation.
Desktop 1440 x 900 and mobile 390 x 844 browser checks verified the toggle, keyboard operation, URL restoration, rendered results, and no horizontal overflow or console errors.
Original RAW files and the writable Sequence workspace were not changed.
The repository remains on `main` with the existing migration and continuation changes uncommitted; no commit or push was requested.

The next useful search work is a small user-judged set of real queries, followed by date/camera constraints if they help narrow intent.
The October 1 date-selection ambiguity during global search was fixed and verified in the October 2 continuation above.

## Native macOS continuation

The selected desktop stack is now SwiftUI + AppKit, with a working local application under `macos/`.
The approved layout combines a labeled sidebar, an uncropped masonry gallery, visible photo captions, and a bottom metadata shelf.
System materials and rounded controls are confined to navigation and actions.
The native sidebar correctly selects Library during a global semantic search.

The first native client supports date browsing, semantic and similarity search, both result orderings, keyboard preview, and Sequence creation, addition, ordering, naming, and notes.
It preserves the existing Python service and data boundaries rather than migrating storage or rebuilding the index.
The October 1 baseline passed seventeen release-mode Swift tests, with native UI checks covering the main retrieval and sequence flows, pagination, and responsive resizing.
The real 25,793-photo geometry took 0.944 ms to calculate, with a 0.968-microsecond mean viewport lookup; these are component timings rather than a frame-rate measurement.

Native verification used port 8876 and a separate temporary Sequence workspace.
The production workspace remained unchanged.
The test app and server are closed after checking, while the final app is opened against the existing port-8766 service for user evaluation.
macOS 26.6.2 is verified; macOS 27 runtime behavior and standalone service packaging remain unverified.
See [native macOS client](native-macos.md) for reproducible commands, measurements, and explicit remaining scope.
