---
name: md-to-gdoc
description: "Convert Markdown into a Google Docs batchUpdate plan (pure, offline). Pairs with gdoc-upsert to create or update real Docs. Use when asked to turn a markdown doc/report into a Google Doc, or to render markdown with real headings, tables, links, code, and lists in Docs."
metadata:
  author: Kevin Hwang (https://kevinhwang.dev)
---

# Markdown → Google Docs plan

Converts a Markdown file into a JSON **plan** of Google Docs `batchUpdate` requests. This step is **pure and offline** — it touches no network and no document. Hand the plan to the `gdoc-upsert` skill to create a new Doc or update an existing one.

Splitting conversion (this skill) from execution (`gdoc-upsert`) keeps the fiddly markdown parsing / index math testable in isolation, and lets the same plan target create-vs-update.

## When to use
- "Turn this markdown into a Google Doc" / "make a Google Doc from this report".
- Any time you need markdown rendered in Docs with **real** structure: headings, native tables, live hyperlinks, monospace code with a grey chip, fenced code blocks, lists, and standalone diagrams/images.
- Always pair with `gdoc-upsert` (this skill alone produces only a plan file).

## Run conversion + verification in a background subagent
Conversion output and the structural sanity-check are verbose and burn main-session context. **Run this skill — and the `gdoc-upsert` execution — inside a single non-blocking background subagent** (`run_in_background: true`), not in the main session. Brief it with: input markdown path, the `coderef` `--repo` (if not the rSERVER default), the target (`create --title ...` or `update --doc-id ...`), and the verification checklist. Have it return only a concise pass/fail summary — never stream plan JSON or full doc dumps back. The subagent's job:
1. run `coderef.py` (when the doc has repo-relative code links), then `convert.py`;
2. **sanity-check the generated plan before upload** — headings, tables (row×col), lists, links, fenced blocks, and images/fallbacks all survived; nothing dropped or mangled (a table fallen back to pipe text, a link missing its href, an image reduced to alt text, a truncated body);
3. hand off to `gdoc-upsert`, then verify the read-back doc.

## Usage
Optionally expand repo-relative code links first (see below), then convert under `uv run --with marko` (marko is the only dependency; nothing to pre-install):
```bash
# 0. (optional) expand repo-relative code refs -> full Sourcegraph/GitHub URLs:
python3 ~/.agents/skills/md-to-gdoc/coderef.py INPUT.md -o INPUT.linked.md --summary
# 1. convert markdown -> plan:
uv run --with marko python3 ~/.agents/skills/md-to-gdoc/convert.py INPUT.linked.md --plan-out plan.json --summary
# standalone SVG images are rasterized into plan.json.assets/ by default when --plan-out is a file
uv run --with marko python3 ~/.agents/skills/md-to-gdoc/convert.py INPUT.md --plan-out plan.json --summary
# optional auto-linkification of bare `code` spans:
uv run --with marko python3 ~/.agents/skills/md-to-gdoc/convert.py INPUT.md --plan-out plan.json --links links.json
```
The executor (`gdoc-upsert`) runs under plain `python3` — it does NOT need marko, because table cells are pre-parsed into the plan here.
`--links` takes a JSON map `{"exact code-span text": "https://url"}`. Any inline `` `code` `` whose text exactly matches a key becomes a monospace **link**. Explicit `[text](url)` markdown links always work without it. Omit `--links` for no auto-linking.

### Repo-relative code links → `coderef.py` (preprocessor)
Preferred authoring style attaches a code link to readable prose — `the [document resolver](go/src/dropbox/.../document_resolver)` — rather than pasting a bare path or long URL. `coderef.py` rewrites such links (href = a repo-relative path, not a URL) into the right code-host URL, by GitHub org:
- **`dropbox-internal` org** (e.g. the rSERVER `server` repo) → **Dropbox Sourcegraph** (`dropbox.sourcegraphcloud.com/...`).
- **Any other org/repo** → **generic GitHub** (`github.com/<org>/<repo>/{blob|tree}/<branch>/...`).

It infers `blob` (file, last segment has an extension) vs `tree` (directory), strips bazel-style `//` and `:target` suffixes, and leaves untouched anything already a URL (`http(s)://`), an `#anchor`, a `mailto:`/`tel:`, a site-absolute `/path`, or an `![image]()` source.
```bash
python3 coderef.py INPUT.md -o OUT.md --summary           # default repo: github.com/dropbox-internal/server
python3 coderef.py INPUT.md --repo github.com/org/repo    # bare paths resolve against this repo
python3 coderef.py INPUT.md --branch BRANCH               # Sourcegraph: pins @BRANCH (omit => default branch, best for living docs); GitHub: defaults to main
```
Explicit `github.com/<org>/<repo>/<path>` hrefs override `--repo` and choose Sourcegraph-vs-GitHub by their own org. Omit this step entirely if the doc has no repo-relative links.

Then execute with the companion skill:
```bash
python3 ~/.agents/skills/gdoc-upsert/upsert.py update --doc-id DOCID --plan plan.json --go
# or
python3 ~/.agents/skills/gdoc-upsert/upsert.py create --title "My Doc" --plan plan.json --go
```

## Supported markdown
Parsed by `marko` (CommonMark + GFM), so the full common grammar works: ATX headings; paragraphs (hard-wrapped lines collapse to one paragraph); ordered/unordered lists; fenced & indented code; thematic breaks (`---`); GFM tables; standalone Markdown images; and inline `**bold**`, `*italic*`/`_italic_`, `` `code` ``, `[text](url)`, escapes (`\*`), and arbitrary nesting (a link inside bold, code inside a link, etc.). Edge cases the previous hand-rolled parser silently mangled (hard wraps, escapes, nested emphasis) now parse to spec.

### Standalone diagrams/images
`convert.py` handles standalone Markdown images; it does not render Mermaid fenced blocks. For a native Mermaid diagram, extract the block to a `.mmd` file and render it before conversion. Try Mermaid CLI before creating or hand-editing SVG:
```bash
# Prefer installed mmdc; use npx -p @mermaid-js/mermaid-cli mmdc if it is unavailable.
mmdc -i diagram.mmd -o diagram.png -b transparent -s 2
```
Embed the rendered PNG as a normal standalone image, optionally preceded by a Google Docs directive:
```markdown
<!-- gdoc:image width=full fallback=./codex-hooks-lifecycle-diagram.txt -->
![Codex hooks lifecycle](./diagram.png)
```

For Markdown that already references an SVG, keep that SVG as the source; the converter rasterizes it to PNG for Google Docs. Hand-edited SVG remains appropriate for deliberately custom artwork, not as the default conversion path for native Mermaid. JPEG is also accepted as a rendered source image. Keep the ASCII `fallback` directive when using the default image mode or when public image staging is unavailable.

Rendering behavior:
- A paragraph containing only an image becomes an `images[]` plan entry plus a `ZZIMG*` placeholder line, preserving document order.
- Local SVG images are rasterized to PNG when `--plan-out` is a file; output goes to `PLAN.json.assets/` unless `--image-assets-dir DIR` is provided.
- Local PNG/JPEG/GIF images are referenced directly in the plan.
- Remote `http(s)` image URLs are preserved as `public_uri`.
- `fallback=path.txt` embeds an ASCII fallback in the plan. This is the safest path for Google Docs because it requires no public image staging.
- Inline images inside prose are not promoted; only standalone image paragraphs are treated as diagrams.

## Plan shape
```
{ "text": "<body blob; one placeholder line per table/image>",
  "paragraph_styles": [ updateParagraphStyle... ],   # absolute indices, insert_at=1
  "text_styles":      [ updateTextStyle... ],
  "list_blocks":      [ {"kind":"numbered"|"bullet","start":idx,"end":idx} ],
  "tables": [ {"placeholder","header":[{"text","runs"}],"rows":[[{"text","runs"}]]} ],
  "images": [ {"placeholder","src","alt","source_path","rendered_path","fallback_text"} ],
  "meta": {...} }
```
Table cells are PRE-PARSED into `{text, runs}` (a run = `{s,e,bold,italic,mono,link}`) so the executor needs no markdown parser.

## Rendering decisions (and the hard-won reasons behind them)
These are deliberate; do not "fix" them without re-reading why:

- **Lists render as real native Docs lists, scoped PER CONTIGUOUS BLOCK.** The plan carries a `list_blocks` array (each `{kind, start, end}`); the executor applies one `createParagraphBullets` per block, so each list gets its own `listId` and numbering **restarts per list**. Do NOT lump all items into one list — that makes numbering run continuously across the whole document (1..N down the page). Do NOT also emit literal `1. `/`• ` prefixes — a native glyph plus a literal prefix renders doubled ("4. 1. ..."). Contiguous same-kind items (bullet/numbered) form one block; any intervening non-list block ends the run.
- **Inline `code` gets monospace font AND a light-grey `backgroundColor`** (the chip look). Font alone looks unfinished. The chip range hugs the code text exactly (no trailing whitespace), so it reads as a word-width chip even on a bullet whose whole content is one code span — apply it uniformly; do not special-case whole-line code spans.
- **Fenced code blocks → monospace paragraphs + a grey paragraph `shading`** across the block. The Google Docs API **cannot create the native code-block widget** (it's UI/import-only; it isn't even round-tripped as a distinct element on read). Grey shading is the faithful, API-achievable equivalent. Whitespace/alignment is preserved exactly — never reflow a code block.
- **Tables become placeholder lines** in the text blob; `gdoc-upsert` swaps each for a real native `insertTable` and fills cells. Table index math must be done against the live doc, so the plan carries only the table spec.
- **Images become placeholder lines** in the text blob. `gdoc-upsert` either replaces them with ASCII fallback text or, with explicit opt-in, stages a raster image publicly long enough for Docs `insertInlineImage` to fetch it. The Docs API cannot consume local image bytes, SVG files, or base64 data URIs directly.

## Notes
- `convert.py` needs `marko`; SVG rasterization uses `rsvg-convert` when available, then falls back to ImageMagick `magick`. `coderef.py` is pure stdlib. `convert.py` is importable (`md_to_plan(md, link_map, base_dir, image_assets_dir)`); `coderef.py` too (`rewrite(md, default_repo, branch)`).
- Constants (mono font, grey shade, link color, placeholder prefixes) live at the top of `convert.py`; code-host hosts/org/default-repo at the top of `coderef.py`.
- For the inverse direction or other Google Docs ops, see the `gws-docs` family of skills.
```
