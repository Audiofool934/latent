# Local time-lapse candidate audit

`timelapse-audit` finds regular interval-shooting segments in the existing local catalog.
It is an on-demand review tool and is not called by the Library, search, or import queue.
It reads SQLite metadata without downloading original images, calling an AI provider, or modifying the catalog.
It does not automatically hide, exclude, delete, or reclassify photos.

```bash
uv run latent timelapse-audit --report ./timelapse-candidates.json --json
uv run latent timelapse-audit \
  --path '/CloudName/archive/2025/2025-10-02' \
  --report ./october-candidates.json
```

The JSON report contains candidate asset IDs, source folders, first and last filenames, equipment information, interval estimates, duration, missing-frame estimates, and limitations.
`--json` prints a compact summary; `--report` saves the complete evidence.
Without `--path`, all currently catalogued photos are considered.
Files previously held outside the catalog are not covered by this audit.

## Grouping and shooting stages

Photos stay within their source directory, camera model, known body serial, filename stream, and timezone metadata group.
When serial numbers are missing, the report explicitly says that two bodies of the same model cannot be distinguished.
Missing identity is never silently promoted to a confirmed device match.
Known and unknown serial-number groups remain separate.

Camera filename counters preserve acquisition order when available, allowing clock resets to split a sequence.
Lens changes, non-increasing timestamps, and large filename-counter gaps split shooting stages.
A pause or sustained cadence change creates another segment during interval analysis.
Switching to a different lens and then returning to the first does not join the earlier and later stages.
RAW and JPEG representations with the same filename stem, capture time, and equipment count as one exposure.

The default evidence threshold is at least 30 frames spanning at least 120 seconds, with a base interval between 1 and 120 seconds.
An eight-interval seed must have stable timing before a run can grow.
Tolerance accounts for timestamp precision and is capped relative to the interval.
At least 90 percent of observed gaps must match the base cadence directly.
Occasional two- or three-interval gaps can represent missing frames; three successive doubled or tripled gaps start a new stage.
Exposure and aperture changes do not by themselves split a segment, so exposure ramping remains possible.

Thresholds can be adjusted with `--min-frames`, `--min-duration-seconds`, `--min-interval-seconds`, and `--max-interval-seconds`.
The detector reports candidates, not a calibrated probability of time-lapse photography.
Regular repeated shooting, bracketing, tracking, or other capture programs can also produce a regular cadence.
Review source context and representative previews before using the report to exclude photos.

## Metadata and limitations

Future preview indexing retains body and lens serial numbers, original subseconds, and timezone offsets when those fields are available in the already-read EXIF header.
This does not increase the configured JPEG or RAW header-read bounds.
JPEG vendor MakerNotes are not separately decoded to recover serial numbers absent from standard EXIF fields.
Previously cached records are not silently re-read or rewritten.
The initial library audit therefore operates with the timestamp precision and device identity already available locally.

Short or irregular time-lapses, large missing sections, captures crossing source-folder boundaries, and sequences with incomplete EXIF can be missed.
Without filename counters, chronological ordering cannot reliably reveal a camera clock reset.
Matching model and lens names alone cannot establish that all frames came from the same physical body.
Visual similarity is not currently calculated by the detector; representative images can be reviewed separately.

The analysis uses only the Python standard library, with memory proportional to the metadata being audited and sorting within camera streams.
The detector introduces no persistent background process or model weights.

## Optional native organizer

Latent 0.1.1 (build 3) adds **View > Time-lapse organizer**, also available from the puzzle-piece button at the bottom of the sidebar.
The organizer is optional; ordinary import and browsing do not run detection.
**Find candidates** explicitly scans local metadata and lists proposals for review.
**Group** accepts a candidate, while **Ungroup** returns it to the candidate list and restores its individual photos in Timeline.
Neither action modifies original files, ratings, flags, captions, saved Sequences, or embeddings.

**Collapse time-lapse groups in Timeline** is off by default and is saved separately for each connected library.
When enabled, a confirmed shoot occupies one representative card with a frame count.
Double-click that card or choose **View frames** to open the full, paginated shoot.
**Back to photos** returns to the previous scope.
Structured Timeline filters select an eligible representative before grouping and pagination, so rated photos are not lost behind an ineligible cover.
Search results, Starred and saved Sequences retain their individual photos.
Actions on a representative card apply to that one photo; open its group before selecting several frames.

Confirmed groups use provider, source path and fingerprint references rather than numeric catalog IDs.
Rebuilding an index preserves matching groups; a changed file does not silently inherit the old file's membership.
An unavailable or changed candidate must be reviewed again before confirmation.
Re-running detection preserves earlier decisions and does not merge or extend a confirmed group.

Group decisions and their detection evidence are stored in `workspace/timelapse.sqlite`, beside the existing `workspace.sqlite`.
Back up the complete workspace directory to retain both; the existing Sequence-only `workspace-export` JSON does not include the add-on database.
The toggle lives in the native app's per-library preferences and does not affect the stored group decisions.
The standalone `timelapse-audit` command above remains read-only and does not write grouping decisions.
