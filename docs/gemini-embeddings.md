# Gemini Embedding 2

Updated: 2026-10-04.

Latent uses the Gemini Developer API for photo embeddings, natural-language queries, and reference-image queries with optional text.
The model is `gemini-embedding-2`, with 3,072 dimensions stored as normalized float16 vectors.
Cosine ranking, similar-photo discovery, motif clustering, Sequences, and photo browsing remain local.

## Data and credentials

Photo encoding uploads the existing JPEG contact previews, limited to 512 pixels and 1 MiB per image.
Each photo occupies a separate Content request and produces exactly one vector.
Latent does not send archive paths, file names, EXIF, RAW files, or sidecars with these requests.
Text search sends the query with Google's `task: search result | query:` prefix.
The most recent 128 query vectors are cached in memory for the lifetime of the server.
`/api/search` accepts `order=closest`, `order=least_similar`, or `order=variety`; the existing `variety=1` parameter remains compatible.
Variety reranks the closest 500 candidates locally, while Least similar ranks the entire index in ascending cosine order.
Changing order uses the same query-vector cache and does not re-encode archive photos.

`POST /api/search/image` accepts `image_base64`, optional `query`, `order`, and `limit` from 1 to 100.
The native picker accepts images up to 100 MiB, applies orientation locally, and creates a fresh JPEG of at most 512 pixels and 1 MiB without original GPS, capture-time, or camera metadata.
The server validates JPEG dimensions and size and limits the complete JSON request to 2 MiB.
Only that preview and optional plain query text are sent to Gemini, without the local filename or path.
Image and text occupy the same Content to produce a single query vector; multimodal text does not use a task prefix, following Google's [embedding aggregation guidance](https://ai.google.dev/gemini-api/docs/embeddings#embedding-aggregation).
The most recent 32 image-query vectors are cached by image digest and text for the server lifetime; uploaded image bytes are not retained in the cache.
Changing sort order reuses this vector, while changing the reference image or refining its text requires a new embedding.

The API key is read lazily from `GEMINI_API_KEY`, with `GOOGLE_API_KEY` as a fallback.
No command-line key argument is accepted, and keys and provider response bodies are excluded from logs and errors.
Requests use HTTPS to Google's fixed endpoint and do not follow redirects.
Library browsing, stored-vector similarity, and Curator do not require an API key or network access.
Gemini text and image queries are disabled on non-loopback server binds and reject cross-site browser requests.

## Index and recovery

The active store is `~/Library/Application Support/Latent/phase0/embeddings/gemini-embedding-2/index.sqlite`.
The model ID and dimensions are validated on opening, so old SigLIP2 vectors cannot be silently mixed in.
The Library index, contact cache, and writable Sequence workspace retain their existing locations.

```bash
uv sync --group dev
uv run latent embedding-status --json
uv run latent embedding-sync --json
uv run latent embedding-build --max-jobs 64 --batch-size 32 --workers 1 --progress --json
uv run latent serve
```

`embedding-sync` only reads local catalog metadata and updates the queue.
`embedding-build` uploads at most `--max-jobs` photos, prioritizing recent capture dates.
`--batch-size` accepts 1 to 64 separate photo requests per HTTP call.
`--workers` accepts 1 to 8 concurrent requests, defaults to 1, and shares the total `--max-jobs` budget across workers.
Reduce concurrency if the project's API quota causes sustained throttling.
These synchronous `batchEmbedContents` calls use standard pricing, not the discounted asynchronous Batch API.
Completed vectors and job status are committed atomically in SQLite after every batch.

HTTP rate limits, transient server errors, and connection failures receive at most three retries with bounded backoff.
Rate-limit retries honor Google's `Retry-After` header or JSON `RetryInfo`, with a 30-second fallback.
A provider failure after retries stops the worker and releases its unfinished claims to pending, without retrying the entire batch one photo at a time.
An unusually long provider retry delay also pauses processing rather than holding claims indefinitely.
Locally invalid previews are isolated through the existing per-image fallback.
Ctrl-C stops new claims and waits for in-flight requests to finish or release their claims.
Claims abandoned by a terminated process are recovered after 30 minutes on the next run.
The same build command resumes the remaining queue without re-encoding successful unchanged photos.

`--progress` writes periodic JSON to stderr; the final result on stdout includes request attempts, image submissions, attempted JPEG upload bytes, request-body bytes, reported input tokens, and an estimated image cost.
Exhausted API retries now also produce a final JSON report, including `run.provider_errors` and the persisted queue status, with exit code 2.
Successful runs return 0; locally failed photo jobs return 1.
Retries can increase submissions and charges, so the estimate is not a billing statement.
No output claims that cloud encoding uses zero photo-network bytes.
Archive reads and writes remain zero during embedding generation.

## Live search coverage

`GET /api/embedding-status` reads local queue counts and valid-vector coverage without generating embeddings or loading the vector matrix.
It includes cached photos that have not entered the embedding queue yet in its total.
The Library polls this endpoint every 10 seconds while visible, preserving the current query, selection, and results.
It displays searchable photos out of the total alongside `Indexing`, `Indexing paused`, `Needs attention`, or `Complete`.
Claims older than 30 minutes are treated as needing attention rather than evidence of a live worker.
Status requests time out after eight seconds; a failed request shows `Status unavailable` while retaining the last known coverage.
Browsing remains available if an old embedding store has an incompatible configuration.
Search is labeled `Search indexed photos` until the entire library is covered.

## Initial budget

The initial library contains 25,793 contact previews with 608,759,497 bytes of JPEG content.
Google's published standard image price is $0.45 per million input tokens, approximately $0.00012 per image.
At the published per-image estimate, one pass is approximately $3.10 before retries, query text, and taxes.
Request encoding adds base64 and JSON overhead to upload traffic.
Raw vector storage is 158,472,192 bytes, approximately 151 MiB, with additional SQLite and journal overhead.
The model weights are hosted by Google; Latent no longer needs a local model runtime.
These estimates assume paid-tier pricing; Latent does not inspect the account's billing configuration.
Google's pricing page states that paid-tier inputs are not used to improve its products.

The user's API direction was authorized on 2026-09-05; prices were rechecked on 2026-09-27 and remain unchanged.
A future asynchronous Batch implementation could use Google's discounted rate, but is not part of this migration.

## Migration and verification

The local SigLIP2 and MobileCLIP benchmark module and its runtime dependencies have been removed.
Historical benchmark results and documentation are retained as evidence, not as the active provider configuration.
The Latent-specific model cache and isolated benchmark environment were removed: 1,932,921,020 bytes of weights and 754,884,366 bytes in the isolated environment, measured as logical file sizes.
The empty SigLIP2 queue was removed after confirming that it contained no vectors.
Shared model or package caches belonging to other projects are outside the cleanup scope.

A live smoke test returned three image vectors and one Chinese query vector, all 3,072-dimensional.
Automated tests cover separate image requests, response ordering and dimensions, malformed vectors, query caching, lazy credentials, safe error messages, bounded retry behavior, and recovery of the entire claimed batch after an API outage.
Validation passed 74 tests, Ruff, JavaScript syntax checks, and wheel/source-distribution builds.
Live desktop and 390-pixel mobile browser checks exercised Chinese search, the inspector, stored-vector similarity, and motif discovery without console errors.
The catalog, Sequence workspace, and Gemini index passed SQLite integrity checks.
The first production batch stored 128 vectors successfully in 35.95 seconds.
The following paragraphs record the September 5 handoff; current runtime information is in [project status](project-status.md).
Inspect `latent embedding-status --json` for current completion instead of treating this migration record as proof of completion.
At background handoff, 1,984 of 25,793 vectors were stored and the remaining 23,809 jobs were pending, with no failed jobs.
Four-worker generation encountered sustained HTTP 429 responses, including a single-image diagnostic request, and stopped cleanly with its remaining jobs pending.
A single-image request succeeded again after cooling down, so the remaining build was resumed in tmux session `latent-gemini-index` with one worker and 32-photo requests.
The tmux pane remains available after exit; if bounded retries are exhausted, inspect its exit status and logs before resuming the pending queue.
The local Library server used `http://127.0.0.1:8765` at that handoff.

On September 27, the old worker was confirmed stopped after a further HTTP 429, with 2,016 stored vectors and 23,777 pending jobs.
Live tests added 32 photos in 4.21 seconds and a further 256 photos in 22.11 seconds, both without retries.
The remaining 23,489 photos were then resumed with one worker and 32-photo requests.
The current Library uses port 8766 because another project occupies port 8765.
The continuation passed 77 tests, including live-status transitions, incompatible-store browsing, and JSON reporting plus exact resumption after an API failure.

On October 1, completion was verified against the current local catalog: all 25,793 photos have successful jobs and current 3,072-dimensional vectors, with no pending, running, or failed jobs.
Every vector's asset identity and fingerprint matched the catalog, all contact files were present, and all vector values were finite with norms within 0.0001 of one.
The final September 27 worker report recorded 17,697 successful photos in 1,720.227 seconds, with no retries or provider errors during that last segment.
That segment's reported estimate is not the total migration bill.
Current completion evidence and retrieval observations are recorded in [project status](project-status.md).

## Official references

- [Model and input formats](https://ai.google.dev/gemini-api/docs/embeddings)
- [REST embedding API](https://ai.google.dev/api/embeddings)
- [Current pricing](https://ai.google.dev/gemini-api/docs/pricing#gemini-embedding-2)
- [Quota and rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
