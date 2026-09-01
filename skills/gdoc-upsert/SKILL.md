---
name: gdoc-upsert
description: "Create a new Google Doc or update an existing one in place from a md-to-gdoc plan, via the gws CLI. Create defaults to pageless (wide) layout; update preserves the doc's existing layout. Use when asked to publish/sync markdown to a Google Doc, create a Doc from a report, or update a Doc without breaking its formatting."
metadata:
  author: Kevin Hwang (https://kevinhwang.dev)
---

# Google Docs create / update (upsert)

Executes a `md-to-gdoc` plan against Google Docs using the authenticated `gws` CLI. Two modes:

- **create** — make a NEW blank doc with a title, set it **PAGELESS** (wide text width), then fill it.
- **update** — replace an EXISTING doc's body **in place**, **preserving its current layout** (pageless or paged) and title.
- **images** — native Mermaid must be rendered to a standalone raster image by the `md-to-gdoc` workflow before planning: prefer installed `mmdc`, then fall back to `npx -p @mermaid-js/mermaid-cli mmdc`. Existing SVG sources are rasterized upstream; standalone images default to ASCII fallback here, and raster insertion requires explicit public staging opt-in.

First produce a plan with the `md-to-gdoc` skill, then run this.

## When to use
- "Publish/sync this markdown to a Google Doc", "create a Google Doc from this", "update that Doc with the latest version".
- Whenever a Doc must keep its **pageless** layout through an update (the naive HTML-import method silently breaks it — see Gotchas).

## Run upload + read-back verification in a background subagent
The `--go` execution output and the post-upsert read-back checks are verbose and burn main-session context. **Run this skill in a non-blocking background subagent** (`run_in_background: true`) — typically the same one that ran `md-to-gdoc`. Brief it with the plan path, the target (`create --title ...` or `update --doc-id ...`), and the verification checklist; have it return only a concise pass/fail summary (doc id/URL, `documentMode`, table dims, link/heading counts, any deviations). It should:
1. run the upsert with `--go`;
2. **re-fetch the live doc and verify the round-trip** — title and (for update) `documentMode` preserved; tables native (row×col) not pipe text; links live with real hrefs; headings styled; fenced blocks shaded; images inserted or ASCII fallbacks rendered; no leftover literal markdown (`##`, `|---|`, stray backticks, bare URLs, `ZZIMG*` placeholders).

Never stream the full read-back doc JSON to the main session.

## Usage
```bash
# Create a new pageless doc:
python3 ~/.agents/skills/gdoc-upsert/upsert.py create --title "My Doc" --plan plan.json [--folder DRIVE_FOLDER_ID] --go

# Update an existing doc in place (layout preserved):
python3 ~/.agents/skills/gdoc-upsert/upsert.py update --doc-id DOCID --plan plan.json --go

# Insert rasterized diagrams by temporary public Drive staging:
python3 ~/.agents/skills/gdoc-upsert/upsert.py create --title "My Doc" --plan plan.json \
  --image-mode public-staged --allow-public-image-staging --go
```
- Without `--go`: prints what it would do and makes **no changes** (update's dry mode doesn't even read the doc).
- With `--go`: executes, then prints the doc URL and a verification line (documentMode, heading/table/image counts, and native-list-paragraph count which must be 0).
- `--links links.json` (optional) auto-linkifies matching `code` spans inside table cells, mirroring `md-to-gdoc`.
- `--image-mode ascii` is the default. Every image must have `fallback_text` from `md-to-gdoc` (usually via `<!-- gdoc:image fallback=diagram.txt -->`), or the command fails clearly.
- `--image-mode public-staged` uploads local rendered PNG/JPEG/GIF assets to Drive, grants temporary `anyone` reader access, calls Docs `insertInlineImage`, then deletes the staged files unless `--keep-staged-images` is set.
- `--allow-public-image-staging` is required for local files because the Docs API cannot insert local bytes, SVG, or base64 data URIs; it fetches PNG/JPEG/GIF images from public URLs.
- `--image-max-width-pt` controls inserted raster width for `width=full`/`width=wide` image directives; default is `468`. A directive can also use a point value such as `width=360` or `width=360pt`.

`gdoc-upsert` consumes only the finished plan; it does not compile Mermaid or edit SVG. Use the Mermaid CLI path in `md-to-gdoc` for native Mermaid; retain a Markdown-origin SVG as source and let the upstream converter rasterize it. ASCII fallback remains the default when public staging is not acceptable.

## How it works (and why this way)
All edits go through `documents.batchUpdate`, never Drive media-upload.
1. **create** calls `documents.create` (title only — it ignores any content), sets `documentFormat.documentMode = PAGELESS` via `updateDocumentStyle`, optionally moves it into `--folder`, then fills it.
2. **update** GETs the doc, deletes the existing body range `[1, lastEnd-1)`, then fills — leaving `documentStyle` (hence pageless/paged) untouched.
3. **Fill** = insert the whole text blob + all paragraph/text styles in one batch, then per table: `insertTable` at the placeholder, delete the placeholder text, re-GET, and populate cells **last-cell-first** so indices stay valid within the batch.
4. **Images** = find `ZZIMG*` placeholders and replace them with either shaded monospace ASCII fallback text or `insertInlineImage` requests.

## Gotchas (learned the hard way — heed these)
- **Never use Drive HTML media-upload to (re)write a pageless doc.** `PATCH .../upload/drive/v3/files/{id}?uploadType=media` with `text/html` converts HTML→Doc but **silently flips `documentMode` PAGELESS → PAGES**. In-place `batchUpdate` (what this skill does) preserves it. Verified empirically.
- **`batchUpdate` body goes in `--json`; `documentId` goes in `--params`.** Putting the requests array in `--params` gives a cryptic `411 Length Required`. (`gws docs documents batchUpdate --help`.)
- **Docs `insertInlineImage` cannot use base64 or local files.** It requires a public URI for PNG/JPEG/GIF, with a small URI length limit. Use ASCII fallback by default, or `--image-mode public-staged --allow-public-image-staging` when temporary public staging is acceptable.
- **The Docs API cannot create the native code-block widget**, and doesn't even expose it on read — a manually-inserted code block comes back as a plain paragraph. Use grey paragraph shading (the `md-to-gdoc` plan already does). If a true native code block is required, it's a manual one-click step in the Docs UI.
- **Index math:** indices shift as you insert. Insert the full blob first, compute style ranges against it, and do tables last; within a single batch, process inserts **highest-index-first** so earlier ops don't invalidate later indices. `updateTextStyle` / `deleteParagraphBullets` don't shift indices; `insertText` / `insertTable` / `deleteContentRange` do.
- **Test layout-affecting experiments on a throwaway copy first:** `gws drive files copy --params '{"fileId":"SRC"}'` (a copy inherits pageless); delete with `gws drive files delete`.
- **Pageless IS recoverable** if ever lost: `updateDocumentStyle` with `fields:"documentFormat.documentMode"` can set PAGELESS back — but the point of update-mode is to never disturb it.

## Verification
After `--go`, the printed summary confirms `documentMode` (with the expected value), heading/table/image counts, and that native-list paragraphs == 0. For deeper checks, GET the doc and inspect: inline-code runs carry Consolas + a `backgroundColor`; links have a real `link.url`; code-block paragraphs carry `shading.backgroundColor`; table cells are populated; no `ZZIMG*` placeholders remain.

## Requirements
- `gws` CLI authenticated for Google Docs + Drive.
- Python 3 stdlib only — no third-party deps, no markdown parser. `upsert.py` is fully standalone: it consumes a finished plan (table cells and image metadata arrive pre-parsed from `md-to-gdoc`), so it does not import the converter or need `marko`.
```
