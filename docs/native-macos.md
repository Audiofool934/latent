# Native macOS client

The native visual verification baseline is from 2026-10-01 with macOS 26.6.2, Swift 6.3.3, and the macOS 26.5 SDK.
Service readiness, navigation recovery, durable annotations, disconnected-sheet recovery, and preview paging passed isolated automated checks on 2026-10-02.
The retained search-card fix was visually accepted and activated on 2026-10-03.
The 2026-10-04 update adds inline grading, range selection, complete multi-photo Sequence creation, reference-image search, and nested Sequence folders.
The subsequent Locations update adds filesystem input and output selection, local and mounted-folder scanning, selected finished-photo exports with original directory structure, and Sequence folders made from symbolic links to originals.
The latest update consolidates navigation under Timeline and Sequences, adds structured search filters and smart Sequences, and supports recoverable deletion of originals on connected folders, including CloudDrive2 mounts.
Version 0.1.1 (build 3) adds the optional [time-lapse organizer](timelapse-audit.md#optional-native-organizer), with reversible groups, Timeline collapse and full-frame browsing.
The current [editing guide](archived-photo-editing.md) describes configuration, overlap protection, recovery, and the cloud verification boundary.
The first native client uses SwiftUI for the window, sidebar, controls, and sheets, with AppKit for the reusable masonry collection and reliable toolbar text focus.
It connects to the existing local Python service and uses the current catalog, cached JPEGs, Gemini search index, and independent Sequence workspace.

## Run

Build with the installed Apple Command Line Tools or Xcode:

```bash
scripts/build-macos.sh
open var/native/Latent.app
```

The build script creates an ad hoc signed local application at `var/native/Latent.app`.
This is a development build, not a notarized distribution release.
The app icon uses `assets/brand/Latent-Mark-v1.svg`, packaged at standard and Retina sizes in `macos/Resources/Latent.icns`.
The export places the original artwork on an inset white tile with continuous rounded corners, transparent margins, and a subtle shadow so its Dock footprint matches other macOS apps.
The app refreshes its Dock image from the bundled icon at launch to avoid stale artwork after local rebuilds.
After changing the artwork, run `scripts/build-macos-icon.sh` with librsvg's `rsvg-convert` available, then rebuild the app.
Normal app builds copy the packaged icon without requiring librsvg.
The application requires macOS 26 or newer and defaults to `http://127.0.0.1:8766`.
The **Imports** sidebar opens the [daily import workflow](import-workflow.md), including resumable previews, verified archive copies and explicitly confirmed photo embeddings.
The build requires the project's existing `.venv/bin/python`, created with `uv sync`.
It packages that runtime path and the standard catalog, embedding and workspace paths in `Contents/Resources/LocalService.json`.
Opening the app from Finder or the Dock starts its local library service when needed, and quitting the app stops that owned service.
A compatible service already running for the same library is reused and left running on quit.
Rebuild the app if the project or its Python environment moves.
This local build depends on that project environment; it does not bundle a standalone Python distribution.

For an externally managed service, start it separately and explicitly select its address:

```bash
uv run latent serve --port 8766
open var/native/Latent.app --args --server-url http://127.0.0.1:8766
```

Semantic search requires the server process to receive `GEMINI_API_KEY` or `GOOGLE_API_KEY` through its environment.
The native app never reads or stores that key.
Browsing dates, loading cached previews, similarity search, and Sequence editing use the local service.
A new semantic query uses the server's existing Gemini query embedding path.

An alternate loopback service can be selected at launch:

```bash
open var/native/Latent.app --args --server-url http://127.0.0.1:8765
```

`LATENT_SERVER_URL` also supplies the endpoint when launching the executable directly.
Either explicit endpoint option disables the bundled automatic startup configuration unless all four managed-service path arguments are also supplied.
Non-loopback endpoints are rejected.
Quit an already running copy before changing launch arguments.

### Service readiness and ownership

The app verifies `/health` before loading the library. The response identifies
Latent, protocol version 1, the service instance, and an opaque identity for its
catalog, embedding directory, and workspace. An unrelated service, an older
service without this contract, or an incompatible protocol produces a connection
message and **Retry connection**. Updating files does not update an already-running
Python process: arrange a controlled service restart before launching the rebuilt
native app. Do not stop a process merely because it occupies the configured port.

Connection attempts are bounded and concurrent retries share one attempt. A
disconnection disables library mutations and requires an explicit retry; there is
no automatic restart loop or persistent background autostart.

An explicit development launch can override the bundled local configuration.
Supply all four absolute paths as launch arguments, alongside an explicit `--server-url http://127.0.0.1:<port>`:

```text
--service-python /absolute/path/to/.venv/bin/python
--service-state-dir /absolute/path/to/existing/catalog
--service-embedding-dir /absolute/path/to/existing/embedding-index
--service-workspace-dir /absolute/path/to/workspace
```

Keep the virtual environment's `bin/python` path rather than resolving its symlink
to the base interpreter. No runtime is downloaded or installed by these options.
This is a launch contract for a supplied runtime, not a standalone distribution.

A compatible service already at that address is reused, provided its data identity
matches the configured directories. Only a refused connection allows a new child;
an occupied or unresponsive port is not replaced. A kernel lock allows one service
per writable workspace, including across different ports. The lock file remains
after exit; a crashed process releases the kernel lock automatically.
Services running older code do not hold this lock. Before enabling managed mode
for an existing workspace, arrange for its older service to be stopped through
its existing owner and restarted with the updated code.

The app gives its child a private stdin pipe. Closing the app closes that pipe,
which shuts down the child; an app crash also closes it. A reused service never
receives that pipe. No process is killed by a saved PID, port lookup, or process
name. Retry waits for a stopping child to exit before considering another launch.
An ongoing editing transfer may need time to finish stopping before retry can
start a replacement.

For other supervisors, `latent serve --ready-json` emits one JSON readiness record
after binding. `--exit-on-stdin-close` enables the explicit owner-pipe contract;
both flags are off for an ordinary external service.

The lifecycle tests use temporary workspaces and mocked endpoints. To additionally
exercise the real Swift-to-Python launch, reuse, and shutdown path without real
photos or model calls, run from the repository:

```bash
LATENT_TEST_PYTHON="$PWD/.venv/bin/python" PYTHONPATH="$PWD/src" \
  scripts/test-macos.sh -c release
```

## Current interaction

- The sidebar provides a collapsible Timeline with Year / Month / Day navigation, Starred, Editing, Locations, and Sequences containing both folders and saved sequences.
- Clicking Timeline opens the whole library; the footer provides Trash and sequence or folder creation.
- Months and dates are newest first, month rows show their total photo count, and the current date's year and month expand automatically.
- Cmd-click toggles individual photos; Shift-click selects the contiguous range from the selection anchor in displayed order, and Cmd-Shift-click adds that range to the current selection.
- Number keys 1 through 5 rate the selected gallery photos or the current inline preview; 0 clears the rating.
- Q toggles the green Pick flag; R clears an existing Pick on the first press and marks Reject on the next press, or marks Reject immediately when the photo is not picked.
- Pick and Reject are independent of ratings and never delete archive files.
- W and E move to the previous or next photo in either the gallery or preview; Shift-W and Shift-E extend gallery selection.
- Captions save explicitly with Save or Return; a successful save returns keyboard focus to photo review.
  Wide windows keep the caption field in the bottom bar, while compact windows open a small editor above it.
  The Edit in PhotoLab action downloads the selection into a persistent editing batch.
- The [archived-photo editing workflow](archived-photo-editing.md) covers PhotoLab handoff, completion review, uploads, and local retention.
- Masonry columns adapt to available width with a default minimum column width of 224 points.
- Photo height follows its oriented preview dimensions; there is no square crop, fixed image height, or aspect-fill rendering.
- Every photo displays its filename and capture date.
  The bottom review bar keeps the selected filename, shooting summary, ratings, flags, caption, and photo actions in stable positions.
  Copy source path, sequence ordering, sequence removal, and Trash are available in Photo actions.
- The transparent title area contains the global search field without a Latent heading or an opaque toolbar background.
- Search submits on Return and offers Most similar, Least similar, or More variety ordering for text queries, reference images, and stored-photo similarity.
- The filter button beside search combines inclusive capture-date bounds, a minimum rating, Starred or not starred, and Pick / Reject / Unmarked flags.
- Structured filters work without a query and restrict eligible photos before semantic ranking when combined with text, an image, or Find similar.
- The image button or a file dropped onto search accepts a local image, including phone JPEGs or HEICs, and optional text refines the image query.
- A local ImageIO conversion applies orientation, limits the preview to 512 pixels and 1 MiB, and omits original GPS, capture-time, and camera metadata before the server sends it to Gemini.
- Least similar selects the lowest cosine scores across the entire indexed library; switching order reuses the same cached query vector.
- Find similar uses local image vectors, and large date galleries append 250 photos at a time.
- Clicking a Timeline year, month or day displays all photos in that period, with pagination and restart restoration preserving the selected scope.
- Timeline disclosure arrows expand and collapse the sidebar independently of the selected gallery.
- Space, Return, or a double-click opens a proportional cached preview in the main image area above the review bar, keeping the sidebar, search, and context header visible.
- Space toggles back to the gallery, Escape closes the preview, and W/E or left/right arrow keys move through it.
- Next in a Library or Starred preview loads the next page at the 250-photo boundary, keeping the current photo visible until it succeeds.
- A failed preview page offers explicit retry, and closing the preview or changing results cancels its pending navigation.
- Search, similar-photo, and ordinary Sequence previews remain within their finite result sets; filtered galleries and smart Sequences load additional pages when needed.
- Sequences support creation, names, notes, adding photos, moving photos earlier or later, and removing references from a sequence.
- **Delete sequence** is available in the sidebar and Sequences manager context menus, with a confirmation that leaves originals, annotations and editing files intact.
- Deleting the currently displayed sequence returns to its containing folder; a newer navigation choice is preserved if the deletion is still pending.
- Sequence folders support arbitrary nesting, with native disclosure controls and expansion state saved separately for each library.
- All Sequences and folder views display an expandable tree with indentation, branch guides, and empty-folder drop targets.
- Main-view and sidebar disclosure controls share the same saved expansion state; breadcrumbs can focus any subtree.
- Drag a sequence or folder onto a folder in either view to move it; drop on Sequences or All sequences to move it to the root.
- Drag payloads retain their library identity, and moves reject missing items, invalid destinations, and folder cycles.
- **New folder**, **Rename**, and **Move to** are available through context menus; the manager also has creation buttons.
- **Move to** browses destinations one level at a time and excludes a folder's own descendants.
- **Remove folder** promotes its subfolders and sequences to the parent after confirmation, preserving all sequence items.
- The **Add to sequence** menu follows the same folder hierarchy, and new-sequence drafts retain their chosen destination.
- Movement in this release uses **Move to**; drag and drop is not enabled.
- New sequence captures every selected photo in displayed order when the sheet opens and retains that selection across dismissal, changes in gallery selection, and partial-save retries.
- Smart Sequences save structured filters in any Sequence folder and update their matching photos when ratings or flags change.
- Cmd-Backspace or Photo actions > Move selected photos to Trash opens a confirmation before moving originals to Latent Trash; the sidebar Trash button provides Restore.

Cmd-F selects the search text, Cmd-R refreshes the library, and Cmd-plus / Cmd-minus adjust the minimum thumbnail width.
The View menu exposes the same commands.
The search field uses AppKit first-responder handling because the initial SwiftUI toolbar implementation did not reliably transfer focus from the gallery.
Gallery shortcuts leave search and caption text entry, sheets, and Editing controls to their normal keyboard handling.

### Stable review controls and photo info

The gallery stays mounted beneath the inline preview, preserving its scroll position when Space returns to the same photo.
The review bar reserves a constant 80-point bottom region, with a 56-point glass control surface; selection, ratings, flags, and preview changes do not resize it.
The context header and search stay in place in both gallery and preview, and the search field adapts to narrow windows without moving into the toolbar overflow.
Click Photo info or press Cmd-I to toggle a 312-point information card, initially above the bar.
It stays open through mouse selection, W/E navigation, ratings, and Space preview changes; Escape retains its existing preview-dismissal behavior.
Drag the card's header to place it anywhere on a connected display, including outside the library window.
The card is a nonactivating native panel, so dragging it does not take the gallery's keyboard focus or resize the photo canvas.
Its visibility and dragged position survive app restarts; disconnecting a display brings it back onto an available screen.
Right-click Photo info and choose **Reset info position** to return it to the gallery corner.
Cmd-I and the card's close button hide it without forgetting its position.
Multiple selections show common values or Multiple values, plus their combined file size.
Shooting settings and original image dimensions come only from EXIF already saved in the local catalog, with unavailable values labeled explicitly.
Opening info never downloads or probes an original photo, and the API exposes only shooting settings and dimensions from the cached EXIF object.
The application follows system light and dark appearance, using native materials for controls and an opaque photo canvas.
The compact caption editor also stays inside the main window; typing does not invoke gallery shortcuts, Return saves and closes it, and Escape closes it.

### Filters and smart Sequences

Starred means a rating greater than zero; Pick and Reject remain separate flags.
Opening filters from a selected Timeline period starts with that period's date bounds.
The applied filters persist while paging, refreshing, or recovering a disconnected service.
Selecting a different Timeline period, Starred, or another Sequence starts that destination's own scope.
Date bounds use complete editable date fields with calendar buttons; unchecked bounds mean any date.
An inverted date range disables Apply and saving a smart Sequence until corrected.

### Search history

The clock beside search opens the most recent 100 distinct text and reference-image queries for this library.
History retains the text, a small reference JPEG when present, filters, result order, and query embedding in the local workspace's `search-history.sqlite`.
It survives app and service restarts and begins with searches made after this update.
Reopening a saved query or changing only its filters or result order ranks the current library with its cached vector, without another Gemini request.
Submitting the same text or prepared reference image again also reuses the vector while it remains in history.
Changing the text or reference image creates a new query that can call Gemini.
History replay never silently calls the encoder if its record is missing or belongs to an incompatible model.
The remove button deletes that history entry, stored reference, and query vector; submitting it again can require a new API request.
Search results continue to reflect current ratings, flags, and available photos instead of a stale saved list.

### Saving smart Sequences

Choose **Save filters as smart sequence…** from the filter popover or **New smart sequence** from sequence creation controls.
The editor saves dates, minimum rating, Starred status, and flags, plus a name, note, and destination folder.
It does not save the natural-language query or reference image.
Membership is computed from the current catalog and annotations, including newly indexed photos that match the rules.
Smart Sequences cannot be manually reordered or have individual references added or removed; edit their rules or the photos' annotations instead.
Ordinary Sequences keep their explicit photo order and references.
**Update sequence links** resolves the current matching photos when projecting a smart Sequence to a folder.

### Recoverable photo deletion

Deletion moves each selected original into `.Latent Trash/<batch-id>/<original-relative-path>` on the same connected source.
The confirmation identifies the selected count and explains that originals move while editing copies remain.
The service checks the displayed library and photo identities before accepting the batch and verifies the original before moving it.
Trashed photos disappear from Timeline, search, Starred, and smart Sequence membership without deleting their ratings, captions, embeddings, or ordinary Sequence references.
Restore returns them to their original paths and makes those retained records available again.
Neither deletion nor restoration overwrites another file; a conflict remains visible for recovery.
Interrupted batches retain a manifest and can be resumed from Trash.
Working copies, finished exports, and adjacent sidecars are left in place.
There is no permanent-delete or empty-trash command in this release.

New editing batches put working RAWs, sidecars, and exports in `~/Pictures/Latent/<date>-<short-batch-id>/`.
Durable manifests remain in `~/Library/Application Support/Latent/workspace/editing/<batch-id>/manifest.json` and retain each batch's exact working folder.
The service's `--editing-dir` option changes where future batches are created, while existing batches remain at their saved locations.
The Editing view shows the path, Show folder opens it in Finder, and Open in PhotoLab opens the folder as a PhotoLab document so its browser can select the batch.
All editing data remains independent of the application bundle location.

The canvas remains quiet and opaque while standard sidebar, toolbar, sheet, and button treatments provide system materials and rounded controls.
No simulated glass shader, custom bounce animation, or webview is involved.
System components are intended to follow macOS appearance changes, but runtime testing has only covered macOS 26.6.2.
macOS 27 has not been tested locally.

### Saving ratings, flags, and captions

Rapid rating, flag, and caption saves are queued per photo, preserving the latest value for each field while earlier requests finish.
Navigation and sequence saves do not discard pending edits.
Starred invalidates its old page cursor and refreshes after queued saves finish, keeping photos and selection available during that refresh.

Pending fields are written to an atomic local draft before requests are sent.
The app stores drafts under `~/Library/Application Support/Latent/pending-edits/`, with a separate file for each settings suite.
`--annotation-drafts /absolute/path/to/drafts.json` selects an explicit file for isolated validation.
Only rating, flag, and caption edits use this queue; it does not queue archive transfers or sequence operations.
New drafts use version 2 to preserve flag fields, while existing version 1 drafts remain readable.
Ordinary pending saves use a compact progress indicator; failures and restored drafts retain the explicit recovery banner.

Failures and restored drafts require **Retry saving** and remain visible across navigation.
The pending-edits banner describes the retained edits without claiming that a healthy library is disconnected during startup.
An explicit **Discard pending edits** confirmation removes the draft; values already saved in the library remain.
If the draft cannot be written locally, the app retains the edit in memory, shows a persistent warning, and asks before ordinary quit because those edits would be lost on exit.
A process or system crash can still lose edits that could not reach disk.

Each replay verifies the library identity and each photo's provider, path, and fingerprint before the server writes any part of the batch.
A rebuilt catalog reusing an asset ID therefore cannot redirect a pending edit to another photo.
Requests stay within the service's 500-photo and 64 KiB body limits.
An older service must be updated before using these annotation checks; pending edits remain available when a request is rejected.

### Sequence and editing recovery

Sequence names and notes remain in a session draft when a sheet is closed, a save fails, or the service disconnects.
The sheet shows its own connection and save errors, and **Retry connection** does not resubmit a mutation.
Reopening the same sequence or ordered photo selection restores its draft; the disconnected library banner also provides **Resume draft**.
These sequence drafts last while the app is open, unlike the annotation drafts that also survive restart.
**Discard draft** removes unsaved text after confirmation and leaves any sequence or photo references already saved on the server intact.

Creation retries keep a stable UUID, send the current corrected fields, and reuse a sequence whose earlier response was lost.
After a partial save, retries write only fields the user changed and resume photo addition without creating another sequence.
Photo additions are chunked to the service's 100-photo and 64 KiB request limits while preserving selection order.
Each draft remains bound to its original library, and source-derived drafts use the identity that loaded the displayed content during reconnect.
Drafts with matching source keys in different libraries cannot coexist; the older draft must be saved or explicitly discarded before making another for that key.

The Editing sheet keeps the selected local-retention choice for the same reviewed file set when it closes or disconnects.
Editing remains available in the sidebar while disconnected, so the retained sheet can be reopened without sending requests.
Library navigation and editing mutations remain unavailable until the connection recovers.
Refreshing the review requires a new choice.
Before a retry, the app reads the current batch; the server checks the library and review revision again while accepting the action.
If an earlier action succeeded but its response was lost, **Check status** shows the current batch without automatically repeating preparation or transfer.
The existing upload verification and local-cleanup workflow remains in the editing service.

## Performance design

`NSCollectionView` reuses visible cells rather than constructing a view for every photo in the library.
Masonry geometry is rebuilt when the data or width changes, while viewport lookup uses a binary search within each column.
Appending a page preserves existing photo positions at the same width.
Changing results immediately discards obsolete layout geometry.
During a collection reload, item layout attributes are withheld until AppKit accepts the matching item count, including a smart Sequence becoming empty after an annotation change.
At end-display, cells clear their image, background, labels, selection styling and action, and accessibility identity, including while AppKit keeps the hidden item cached without calling `prepareForReuse`.
Later selection and annotation updates leave that retired presentation blank; redisplay restores current model data and loading without restarting an already active image request.
An item can also be released without another end-display callback while its view remains attached to the collection.
Its main-actor deinitializer retires and detaches only that already-loaded view and cancels its image consumer.
Image tasks keep the owner weak across contact and large-preview awaits, so outstanding image work cannot postpone this cleanup.
Composited GUI acceptance of the release cleanup passed before the October 3 activation.
Requests run asynchronously, navigation cancels obsolete work, and generation checks prevent late responses from replacing a newer gallery.

The image pipeline coalesces duplicate requests, limits local HTTP connections to six and ImageIO decode workers to four, and decodes off the main actor.
Decoded images use an explicit 128 MiB LRU budget.
Offscreen work is canceled, with a maximum 24-item prefetch lookahead.
The budget covers cached decoded images, not total process memory or images still retained by visible views.
The app uses an existing larger cached preview when available and otherwise displays the contact JPEG without fetching a RAW file.
The preview footer reports the pixels actually decoded and distinguishes contact previews from larger cached previews.

Measured on the real 25,793-photo catalog:

| Measurement | Observed |
| --- | ---: |
| Build full-catalog masonry geometry | 0.944 ms |
| Mean viewport lookup, 10,000 positions | 0.968 microseconds |
| First batch of 24 local contact JPEG fetches and decodes | 43.79 ms |
| Same 24 images from the decoded cache | 0.109 ms |
| Decoded cache after a 256-image stress pass | 133,640,192 bytes of 134,217,728 |
| Local similarity request, first / warm | 879.9 / 222.6 ms |

These are component measurements, not a frame-rate result or a cold-filesystem benchmark.
The benchmark separately fetched the entire catalog in 21.18 seconds through 104 API pages; the application does not perform that full-catalog fetch at startup.
Provider latency for a new semantic query remains separate from native rendering speed.

Reproduce against a running loopback server:

```bash
swift run --package-path macos -c release LatentBenchmark http://127.0.0.1:8766
```

This benchmark reads the local catalog, local JPEG cache, and local similarity endpoint.
It does not call Gemini or access the original archive.

## Validation

```bash
scripts/test-macos.sh --filter GalleryOwnershipTests
scripts/test-macos.sh -c release
git diff --check
```

The October 4 validation copy passed 113 Swift Testing checks covering geometry, proportional sizing, pagination, API contracts, image cancellation and cache bounds, navigation races, service readiness and ownership, annotation persistence, and sheet recovery.
The actual repository checkout subsequently passed all 113 tests in 13 suites using Apple Swift 6.3.3; the package declares Swift tools version 6.2.
The earlier `sending 'item' risks causing data races` compile error at `Mutex(item)` was reproduced in this checkout and repaired with a private, release-only `@unchecked Sendable` test wrapper held by `Mutex`.
The wrapper exposes no access to `PhotoItem` state and preserves the deliberate detached final release, production actor isolation, and existing view-retirement assertions.
The initial isolated-copy workaround remains historical validation evidence.
The focused ownership run passed 2 tests in 1 suite, with both argument cases of the shared-image test passing.
The full Release run below includes the current Archive and Sequence-folder coverage.

| October 4 local run | Exact command from the repository root | Result |
| --- | --- | --- |
| Before repair | `scripts/test-macos.sh --filter GalleryOwnershipTests > var/native-ownership-repair-20261004/baseline.log 2>&1` | Compile failure reproduced, exit 1 |
| Focused after repair | `scripts/test-macos.sh --filter GalleryOwnershipTests > var/native-ownership-repair-20261004/focused.log 2>&1` | 2 tests in 1 suite passed, exit 0 |
| Full after repair | `scripts/test-macos.sh -c release > var/native-ownership-repair-20261004/full-release.log 2>&1` | 113 tests in 13 suites passed, exit 0 |

The production release build compiles and is ad hoc signed successfully.
Annotation regressions include rapid per-photo edits, failures and retry, disk failure, interrupted requests and restoration, stale reads, Starred selection, concurrent sequence saves, and bounded bulk requests.
Sheet regressions cover offline startup, dismissal and reopening, repeated retries, partial and uncertain saves, corrected input, changed libraries, retained review choices, and obsolete responses.
Preview regressions cover Library and Starred page boundaries, coalesced input, dismissal and navigation races, failed-page retry, terminal pages, and clearing only the relevant error banner.
The GUI follow-up regressions cover reused-cell residue, obsolete layout attributes after shrinking or clearing results, offline Editing availability, and restored annotation status during the startup health check.
Two further regressions use a SwiftUI-hosted collection in a never-shown test window so AppKit installs its real scroll observers.
They exercise actual cell retirement, late state updates, empty results, restored results after resize, and redisplay with current annotations and selection.
They inspect cached view content as well as active items; they do not validate compositor pixels or the search error banner.
Three release regressions additionally cover a detached/inert orphan view with an unchanged selected sibling, an off-actor final release, and held contact/preview requests through the real shared image pipeline.
The image tests use generated PNGs on an owned loopback listener and require the surviving cell to finish its shared large-preview request.
The Python suite has 116 passing tests, including annotation identity checks, flag migration/import/export, image-query caching and validation, whole-index least-similar ranking, idempotent sequence creation, partial metadata updates, guarded editing actions, external working-folder recovery and cleanup, and concurrent first-workspace initialization.
The test script supplies the bundled Swift Testing framework paths when using Command Line Tools without full Xcode.

The 2026-10-01 native UI checks exercised semantic searches, both result orderings, mouse and keyboard photo selection, proportional preview navigation, similarity search, sequence creation/addition/reordering/renaming, restart persistence, date pagination, window resizing, and thumbnail resizing.
The rendered gallery was inspected at approximately 1291 x 949 and 972 x 692 points, including mixed landscape and portrait photos.
Sequence mutations used a separate workspace at `var/native-verification/workspace` on test port 8876.
The normal workspace was checked afterward and still contained zero sequences.

Local evidence lives in `var/native-verification/`, with build and test logs in `var/native-build.log` and `var/native-tests-release.log`.
These ignored artifacts include private photo metadata and screenshots.
The temporary test service and test app are stopped after verification.
The normal app and the existing port-8766 library service can remain open for evaluation; Cmd-Q closes the app, and `tmux kill-session -t latent-library-server` stops that explicitly retained service.

The 2026-10-03 GUI run used 260 generated JPEGs and a separate catalog, embedding directory, workspace, settings suite, and annotation draft.
It verified offline recovery, explicit annotation retry and restart persistence, both preview page boundaries and retry, sequence partial-save recovery, uncertain editing-action reconciliation, Starred membership, and search navigation.
It found a retained card behind empty/error search results, an unavailable Editing row while offline, and misleading restored-edit status text.
Subsequent targeted retests passed two offline Editing reopens, reconnect, and accurate restored-edit wording with explicit annotation retry.
The stale card still survived empty/error searches, recovered results, and resize after the earlier layout, reuse, and end-display fixes.
Passive diagnostics and paired screenshots identified a live `PhotoCell` with the stale border/name/date beneath the collection after its item controller had deallocated.
The trace contained no later display or retirement callback after its last configuration; the exact caller that requested that temporary item was not captured.
Controller-release teardown and weak image-task ownership now have failing-before and passing-after regression coverage.
The subsequent targeted visual acceptance passed, and the gallery fix was activated on October 3.
Headless checks do not replace visual verification.
The run did not capture every intermediate delayed state or test the browser against this fixture, and independent sheet identity was tested with a separate draft because creating another photo-derived draft was disabled during the first save.

The October 4 workflow run reproduced the old modal-preview rating and Space-key failures before the change.
Its isolated 260-photo native fixture then verified inline preview, 1-5 ratings, Q/R transitions, W/E navigation, Space dismissal, and text entry without accidental grading.
The New sequence flow saved both selected photos through the actual sheet, and the API confirmed their order.
An oversized generated reference image exercised the native image picker, preparation, and all three search orderings with a stubbed provider; the encoder recorded only one image request across order changes.
Native regressions exercise actual Shift/Cmd mouse events, ordered multi-photo draft recovery, independent flags and ratings, and reference-image orientation and metadata removal.
After service activation, a generated 512-pixel image exercised a real Gemini query against all 25,793 indexed photos.
The three orders returned 100 results each, closest and least-similar were correctly sorted and disjoint, and varied order retained the first closest match.
Final read-only native checks against the real library verified the transparent search toolbar, inline preview, W/E navigation, and Space dismissal.
The Archive hierarchy follow-up passed a release build and native visual checks of independent month expansion, month totals, initial selection visibility, and date navigation across months and years.
Evidence, source snapshots, and the consistent workspace backup are under `var/workflow-qa-20261003/`.
These ignored files can contain private metadata and should remain local.

The October 4 year/month navigation and Sequence deletion follow-up passed 137 Python tests and 117 native tests in 14 suites.
Its native fixture verified direct year and month selection, independent disclosure arrows, cancellation, confirmed deletion, and return to the containing folder while all six original-file hashes and the other sequence stayed unchanged.
The updated normal app displayed 7,536 photos for 2026 and 1,053 for August 2026, with 250-photo pagination.
The existing 25,793-photo catalog, saved sequences, editing batch and locations were preserved through service activation.
Source snapshots, local backups and verification logs are under `var/navigation-delete-20261004/`; the temporary app and service were stopped.

## Remaining scope

The app does not yet bundle a Python runtime, replace the backend in Swift, implement a RAW processing engine, or expose every existing web Curator/motif workflow.
Explicit ownership of a supplied Python runtime is supported as described above.
Those workflows remain available through the existing web interface and CLI where already implemented.
Future performance claims about continuous scrolling should use frame pacing and Instruments on the target machine, including memory pressure and cold-cache conditions.

The October 4 folder verification used the isolated synthetic workspace under `var/sequence-hierarchy-20261004/`.
Native UI checks created `Projects / Travel / 2026 / Mountain study`, renamed Travel to Journeys, restored the expanded hierarchy after restarting the app, collapsed and reopened the parent, added photos through the nested menu, and moved the sequence through the destination picker without changing its photo contents.
Model tests cover 1,100 nested folders, sibling ordering, explicit JSON null when moving to the root, per-library expansion persistence, stale-library draft rejection, and ordered additions split into requests of at most 100 photos.
Backend tests additionally cover concurrent moves, cycle rejection, folder removal preserving item order, schema 3 migration, and non-recursive import/export at 1,100 levels.
The sidebar caps visual indentation to retain readable labels and shows a depth indicator beyond that point; the stored hierarchy and manager navigation have no fixed depth limit.
