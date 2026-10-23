# Latent Project Page

This folder holds Latent's project page.
Its first section is a search over 21 sample photographs, ranked by Latent's own engine.
The page runs no model and makes no network requests beyond its own files, and its content security policy enforces that.

## Run it

`site/public` is the whole page, with no build step.
Serve it with any static file server:

```bash
python3 -m http.server 8768 --directory site/public
```

Then open <http://localhost:8768>.

## How the search works

- `sample.toml` lists the photos, a description of each for screen readers, and the searches.
- `prepare.py` renders each photo through Latent's preview code, the same path the app takes for a local JPEG or HEIF file.
- EmbeddingGemma 2 encodes the 512-pixel contact previews and the searches on this Mac.
- Latent's ranking code orders the photos for every search and every photo, in all three sort orders.
- `prepare.py` writes `public/archive.js`, which holds those orders and scores, and two WebP images for each photo.
  The images keep their color profile and drop all other metadata.

The page only replays those results.
Opening a photo and choosing **Similar photos** uses the photo's own vector, like the app does.

## Change the photos or the searches

You need the source photos and on-device search set up; see [local search](../docs/local-embeddings.md).
Edit `sample.toml`, then run:

```bash
uv run --locked python site/prepare.py ~/path/to/sample-photos
```

The script replaces everything in `public/photos` and rewrites `public/archive.js` and `public/link-preview.jpg`.
`public/library.webp` is a link to the README screenshot, so update that file in `assets/screenshots`.

## Publishing

`.github/workflows/pages.yml` publishes `site/public` to GitHub Pages when it changes on `main`.
The custom domain is `latent.audiofool.ai`, set in the repository's Pages settings.

## Rights

The photographs are by Audiofool.
They are not covered by the Apache License that covers the rest of this repository.
