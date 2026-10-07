# Local EmbeddingGemma 2 search

Updated: 2026-10-07.

Latent can search with EmbeddingGemma 2 running on this Mac instead of the Gemini Embedding 2 API.
Photos and queries are encoded locally through llama.cpp; nothing is uploaded and there is no per-request charge.
The search experience stays the same, except that image search cannot yet be refined with added words.

EmbeddingGemma 2 is not compatible with Gemini vectors.
It has its own image index, saved-query cache, and derived search data, and becomes active only after an explicit validation and switch.
Gemini remains the default, its index is never modified by the local engine, and switching back is always available.

## Requirements

- Apple Silicon Mac with about 1.5 GB of free memory while the model is loaded.
- `llama-server` built from llama.cpp commit `4fbc76dec51d0add466f0210855c0596589b60d4` or later, which added EmbeddingGemma 2 support.
  As of 2026-10-07, Homebrew's stable `llama.cpp` 0.6.0 (build 11429) predates that commit and cannot load the model; Latent reports it as an incompatible build.
  Homebrew's `--HEAD` formula does not build either, because it links the separate Homebrew `ggml` 0.26.0, which lacks an API that current llama.cpp uses.
  Until a Homebrew release includes the support, use a source build of llama.cpp from that commit or later.
- The pinned model and projector from [`unsloth/embeddinggemma-2-GGUF`](https://huggingface.co/unsloth/embeddinggemma-2-GGUF/tree/ba3888272494be64ed88c9eb536ddc61a1be73d5) at revision `ba3888272494`.

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `embeddinggemma-2-Q8_0.gguf` | 309,855,520 | `6f1bd4ac6c5df7444f9cca7ca36cafe6cfa34cd6f49fefb1e0b4be8143aed8bc` |
| `mmproj-Q8_0.gguf` | 554,821,120 | `90e7b0238009e2954f856f2081dcf7f35af026b64c765e98f4777053e1754460` |

Both files are required; the main model alone cannot encode images.

## Set up

Latent never downloads weights on its own.
To download the pinned files explicitly (about 865 MB, no photos are sent):

```bash
uv run latent local-encoder fetch --model-dir ~/Library/Application\ Support/Latent/models/embeddinggemma-2
```

Then point Latent at `llama-server` and the model folder:

```bash
uv run latent local-encoder setup --llama-server /path/to/llama-server --model-dir /path/to/models
```

Setup verifies both checksums, starts the model once, and checks that it returns 768-dimensional vectors before saving `embeddinggemma-runtime.json` next to the existing indexes.
`uv run latent local-encoder status` shows the configuration, index coverage, validation state, and active engine.

## Build, validate, and switch

In the app, open **Imports**, choose **AI Search**, and use the **Search engine** section:

1. Under **Generate photo embeddings**, choose **EmbeddingGemma 2 (on this Mac)** and a scope, then **Review…**.
   The review shows the photo count and an estimated time instead of an upload size and cost.
2. Confirm to encode the reviewed photos.
   Generation is resumable; pausing takes effect after the current photo, and an interrupted run resumes without re-encoding finished photos.
3. Choose **Validate index**.
4. Choose **Use for search** once validation passes.
   If the index covers only part of the library, only indexed photos appear in search.
5. Choose **Use for search** on Gemini Embedding 2 to switch back.

The same steps are available from the command line:

```bash
uv run latent embedding-build --backend embeddinggemma --max-jobs 500 --progress
uv run latent local-encoder validate
uv run latent search-backend activate embeddinggemma
uv run latent search-backend activate gemini
uv run latent search-backend show
```

`--backend` defaults to the active engine for `embedding-status`, `embedding-sync`, and `embedding-build`.
A running service picks up a command-line switch on its next search.

## Validation

Validation is required before activation and applies only to the exact index contents it checked; adding vectors makes it stale.
It checks:

- the stored model ID, 768 dimensions, artifact revision, file checksums, and preprocessing all match this configuration;
- SQLite integrity;
- every stored vector is finite and unit length;
- coverage of the visible library;
- a repeated text query produces the same 768-dimensional vector;
- up to 12 indexed photos, spread across the index, re-encoded from their contact JPEGs retrieve themselves within the top three with cosine similarity of at least 0.98.

## How encoding works

Images are the existing 512 px contact JPEGs, sent unprefixed as JPEG data URLs.
Text queries use the model card's retrieval prefix `task: search result | query: {text}`.
Reference-image queries use the same image path as indexing, so a photo used as a reference matches its own indexed vector.
Combined image and text input is supported by the model but has not been validated for retrieval, so Latent rejects it with a clear message.

`llama-server` runs with the bounded trial's configuration: mean pooling, 280 image tokens, F32 KV cache, flash attention off, Metal offload, one slot.
The model card warns that FP16 activations silently degrade EmbeddingGemma 2; Q8_0 describes stored weights only.
Vectors are L2-normalized and stored as float16, like the Gemini index.

Every index generation records its model, revision, checksums, and preprocessing in `embedding_meta`.
Opening an index with a different configuration fails instead of mixing vector spaces.
Saved searches are keyed by the engine's vector space, so a Gemini query vector is never reused for the local index or the reverse.
Library identity (`data_id`) stays anchored to the Gemini index directory, so switching engines does not change it.

## Runtime ownership

The service starts `llama-server` on demand, on a random loopback port with a per-start API key, and unloads it after 15 minutes without requests.
The model files are re-verified each time it starts; a cold start took about 13 seconds on an M2 Pro, including Metal initialization.
`llama-server` runs under a small guard process that holds a pipe from the service.
If the service exits for any reason, including a crash or `SIGKILL`, the guard stops `llama-server`, so the model is never left loaded.

## Files

| Path under `~/Library/Application Support/Latent/phase0/embeddings/` | Contents |
| --- | --- |
| `gemini-embedding-2/` | Existing Gemini index; anchors library identity |
| `embeddinggemma-2-q8_0-ba3888272494/` | Local index and its `validation.json` |
| `embeddinggemma-runtime.json` | Paths to `llama-server` and the model folder |
| `active-backend.json` | The active engine; absent means Gemini |

## Evidence

A bounded trial on an M2 Pro (16 GiB) compared both engines on 320 contact previews and 111 human-rated query/photo pairs.
On the 13 queries with fully judged top-six results, EmbeddingGemma 2 Q8 had 36 of 78 clear matches against Gemini's 35, 54 of 78 clear or partial matches for both, and a clear first result for 8 of 13 queries on both.
Images encoded at about 1.45 per second, warm text queries took 7.9 ms median, and peak process memory was 1.43 GiB.
The sample is small and was rated by one person, so this supports a local alternative rather than general equivalence.

The integration was verified on 2026-10-07 against an isolated copy of those 320 photos, never the real library:

- vectors from Latent's adapter matched the trial's to cosine 1.000000 for text and images;
- a full local index built through the native AI Search flow, interrupted with `SIGKILL` at 103 photos, and resumed, matched the trial's vectors for all 320 photos;
- `llama-server` exited with the killed service;
- validation passed all checks, and all 40 trial queries returned the trial's top six in the same order;
- the app switched to the local engine and back without changing library identity, and saved searches stayed separated by engine.
