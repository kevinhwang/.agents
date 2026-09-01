#!/usr/bin/env python3
"""Convert Markdown to a Google Docs batchUpdate "plan" (pure, no network).

The plan is a JSON object describing how to materialize the markdown as Google Docs
content via documents.batchUpdate, with all indices computed relative to an insertion
point of 1 (the start of an empty body). The companion `gdoc-upsert` skill executes
the plan against a real document; this module never touches the network.

Parsing uses the `marko` CommonMark library (GFM extension) for a real AST — headings,
paragraphs (hard-wrapped lines collapse correctly), nested emphasis, escapes, ordered/
unordered lists, fenced code, thematic breaks, and GFM tables all parse to spec. Run this
module under `uv run --with marko` (the executor `gdoc-upsert` does NOT need marko — table
cells are pre-parsed into the plan here).

Design notes / hard-won gotchas baked in:
  * Lists become REAL native Docs lists, scoped per contiguous block (see build_plan):
    one createParagraphBullets per block so numbering restarts per list. Never emit literal
    "1. "/"• " prefixes alongside a native glyph (renders doubled, "4. 1. ...").
  * Inline `code` gets BOTH a monospace font and a light-grey background (the chip look).
  * Fenced code blocks become monospace paragraphs with a light-grey paragraph shading
    (the Docs API cannot CREATE the native code-block widget; shading is the faithful
    fallback). Whitespace/alignment is preserved exactly.
  * Tables are emitted as placeholder paragraphs; the executor replaces each with a real
    native insertTable and fills cells. (insertTable index math must be done live, so the
    plan only carries the table spec, not absolute table indices.)
  * Standalone Markdown images are emitted as placeholder paragraphs plus image specs. SVGs
    can be rasterized locally to PNG assets for the executor, while ASCII fallbacks stay as
    local text for environments where public image staging is not acceptable.

Output plan shape:

```json
{
  "text": "<full body text blob, with one placeholder line per table>",
  "paragraph_styles": [ <updateParagraphStyle request>, ... ],   # absolute indices (insert_at=1)
  "text_styles":      [ <updateTextStyle request>, ... ],         # absolute indices
  "list_blocks": [ {"kind": "numbered"|"bullet", "start": idx, "end": idx}, ... ],
  "tables": [ {"placeholder": "ZZTBL0",
               "header": [ {"text": "..", "runs": [..]} ],            # pre-parsed cells
               "rows": [ [ {"text": "..", "runs": [..]} ] ] } ],
  "images": [ {"placeholder": "ZZIMG0", "src": "./diagram.svg", "alt": "...",
               "rendered_path": "assets/ZZIMG0.png", "fallback_text": "..."} ],
  "meta": {"insert_at": 1, "mono_font": "Consolas", ...}
}
```

A "run" is {s,e,bold,italic,mono,link} with offsets relative to its cell/paragraph text.

CLI:
  convert.py INPUT.md [--plan-out plan.json] [--links links.json]   (run under `uv run --with marko`)
    --links: optional JSON map {"repo/path/or/token": "https://url", ...}. Any inline
             `code span` whose exact text is a key is rendered as a monospace link.
             Omit for no auto-linkification (explicit [text](url) links always work).
"""

import argparse
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import marko

MONO_FONT = "Consolas"
CODE_BG = {"red": 0.945, "green": 0.945, "blue": 0.945}      # inline code chip + code block
LINK_COLOR = {"red": 0.07, "green": 0.33, "blue": 0.8}
HR_COLOR = {"red": 0.7, "green": 0.7, "blue": 0.7}           # horizontal-rule bottom border
TABLE_PLACEHOLDER_PREFIX = "ZZTBL"                            # unlikely to occur in prose
IMAGE_PLACEHOLDER_PREFIX = "ZZIMG"
Gdoc_IMAGE_DIRECTIVE_RE = re.compile(r"<!--\s*gdoc:image\s+(?P<body>.*?)\s*-->", re.I | re.S)
DIRECTIVE_ATTR_RE = re.compile(r"(?P<key>[A-Za-z_][\w-]*)=(?P<value>\"[^\"]*\"|'[^']*'|[^\s]+)")
IMAGE_MIMES = {
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


# ----------------------------------------------------------------- inline parsing (marko AST)
def parse_inline(node_or_str, link_map=None):
    """Flatten a marko inline-bearing node (or a raw string, parsed as one paragraph) into
    (plain_text, runs). Each run: {s,e,bold,italic,mono,link} with offsets relative to text.

    Handles nested emphasis (a link inside bold, code inside a link, etc.) by carrying the
    active style flags down the AST. Hard-wrapped lines (LineBreak) collapse to a space."""
    link_map = link_map or {}
    if isinstance(node_or_str, str):
        node_or_str = marko.Markdown(extensions=["gfm"]).parse(node_or_str).children[0]

    out, runs, pos = [], [], 0

    def add(txt, bold, italic, mono, link):
        nonlocal pos
        if not txt:
            return
        s0 = pos
        out.append(txt)
        pos += len(txt)
        if mono and not link:
            link = link_map.get(txt)          # auto-linkify a bare known `code` span
        if bold or italic or mono or link:
            runs.append({"s": s0, "e": pos, "bold": bold, "italic": italic,
                         "mono": mono, "link": link})

    def walk(node, bold=False, italic=False, mono=False, link=None):
        name = type(node).__name__
        if name in ("RawText", "Literal"):
            ch = node.children
            add(ch if isinstance(ch, str) else "", bold, italic, mono, link)
            return
        if name == "LineBreak":
            add(" ", bold, italic, mono, link)   # soft/hard wrap -> single space
            return
        if name == "CodeSpan":
            add(node.children, bold, italic, True, link)
            return
        if name in ("StrongEmphasis",):
            bold = True
        elif name in ("Emphasis",):
            italic = True
        elif name == "Link":
            link = node.dest
        children = getattr(node, "children", None)
        if isinstance(children, str):
            add(children, bold, italic, mono, link)
        elif isinstance(children, list):
            for c in children:
                walk(c, bold, italic, mono, link)

    children = getattr(node_or_str, "children", [])
    if isinstance(children, str):
        add(children, False, False, False, None)
    else:
        for c in children:
            walk(c)
    return "".join(out), runs


def _cell(node, link_map):
    """A table cell (TableCell node) -> {text, runs} with pre-parsed inline runs."""
    text, runs = parse_inline(node, link_map)
    return {"text": text, "runs": runs}


# ----------------------------------------------------------------- block parsing (marko AST)
def _html_text(node):
    body = getattr(node, "body", None)
    if isinstance(body, str):
        return body
    children = getattr(node, "children", "")
    if isinstance(children, str):
        return children
    if isinstance(children, list):
        return "".join(getattr(c, "children", "") for c in children)
    return ""


def _parse_image_directive(text):
    match = Gdoc_IMAGE_DIRECTIVE_RE.search(text or "")
    if not match:
        return None
    attrs = {}
    for attr in DIRECTIVE_ATTR_RE.finditer(match.group("body")):
        value = attr.group("value").strip("\"'")
        attrs[attr.group("key").replace("-", "_")] = value
    return attrs


def _standalone_image(node):
    children = [c for c in getattr(node, "children", []) if type(c).__name__ != "LineBreak"]
    if len(children) != 1 or type(children[0]).__name__ != "Image":
        return None
    image = children[0]
    alt, _ = parse_inline(image)
    return {
        "type": "image",
        "src": image.dest,
        "alt": alt,
        "title": getattr(image, "title", None),
    }


def parse_blocks(md, link_map=None):
    """Parse markdown via marko into ordered block dicts. Types:
    heading{level,inline}, para{inline}, bullet{inline}, numbered{inline},
    code{text}, hr, table{header,rows}, image{src,alt,title} — where `inline` is a
    marko node and table header/rows are lists of pre-parsed {text,runs} cells."""
    doc = marko.Markdown(extensions=["gfm"]).parse(md)
    blocks = []
    pending_image_directive = None

    def emit_list(list_node):
        kind = "numbered" if list_node.ordered else "bullet"
        for item in list_node.children:           # ListItem
            # take the item's first paragraph/inline content; flatten nested blocks to text
            inline = None
            for child in item.children:
                if type(child).__name__ in ("Paragraph",):
                    inline = child
                    break
            if inline is None and item.children:
                inline = item.children[0]
            blocks.append({"type": kind, "inline": inline})

    for node in doc.children:
        name = type(node).__name__
        if name == "Heading":
            pending_image_directive = None
            blocks.append({"type": "heading", "level": node.level, "inline": node})
        elif name == "Paragraph":
            image = _standalone_image(node)
            if image:
                image.update(pending_image_directive or {})
                blocks.append(image)
            else:
                blocks.append({"type": "para", "inline": node})
            pending_image_directive = None
        elif name == "List":
            pending_image_directive = None
            emit_list(node)
        elif name in ("FencedCode", "CodeBlock"):
            pending_image_directive = None
            raw = node.children[0].children if node.children else ""
            blocks.append({"type": "code", "text": raw.rstrip("\n")})
        elif name == "ThematicBreak":
            pending_image_directive = None
            blocks.append({"type": "hr"})
        elif name == "Table":
            pending_image_directive = None
            rows = list(node.children)            # TableRow nodes
            header = [_cell(c, link_map) for c in rows[0].children] if rows else []
            body = [[_cell(c, link_map) for c in r.children] for r in rows[1:]]
            blocks.append({"type": "table", "header": header, "rows": body})
        elif name == "HTMLBlock":
            directive = _parse_image_directive(_html_text(node))
            if directive is not None:
                pending_image_directive = directive
            else:
                pending_image_directive = None
        elif name == "BlankLine":
            continue
        else:
            pending_image_directive = None
            # Fallback: treat any other block as a paragraph if it has inline content.
            if getattr(node, "children", None):
                blocks.append({"type": "para", "inline": node})
    return blocks


# ----------------------------------------------------------------- plan building
HEAD = {1: "HEADING_1", 2: "HEADING_2", 3: "HEADING_3",
        4: "HEADING_4", 5: "HEADING_5", 6: "HEADING_6"}


def _text_style_req(run):
    """run carries ABSOLUTE s/e. Emit an updateTextStyle request (or None)."""
    ts, fields = {}, []
    if run.get("bold"):
        ts["bold"] = True; fields.append("bold")
    if run.get("italic"):
        ts["italic"] = True; fields.append("italic")
    if run.get("mono"):
        ts["weightedFontFamily"] = {"fontFamily": MONO_FONT}; fields.append("weightedFontFamily")
        ts["backgroundColor"] = {"color": {"rgbColor": CODE_BG}}; fields.append("backgroundColor")
    if run.get("link"):
        ts["link"] = {"url": run["link"]}; fields.append("link")
        ts["foregroundColor"] = {"color": {"rgbColor": LINK_COLOR}}; fields.append("foregroundColor")
        ts["underline"] = True; fields.append("underline")
    if not fields:
        return None
    return {"updateTextStyle": {
        "range": {"startIndex": run["s"], "endIndex": run["e"]},
        "textStyle": ts, "fields": ",".join(fields)}}


def build_plan(blocks, link_map=None, insert_at=1):
    text_parts, cursor = [], insert_at
    para_reqs, text_reqs, tables, images = [], [], [], []
    tbl_idx = img_idx = 0

    def emit(s):
        nonlocal cursor
        start = cursor
        text_parts.append(s)
        cursor += len(s)
        return start

    def emit_para(inline, named=None, prefix="", is_list_item=False):
        p_start = cursor
        if prefix:
            emit(prefix)
        txt, runs = parse_inline(inline, link_map)
        # A Docs bullet glyph inherits the text style of the paragraph's FIRST character. When a
        # list item starts with an inline-`code` chip (grey background at offset 0), that grey
        # bleeds onto the bullet glyph. The batchUpdate API has no way to style the glyph
        # independently (it is always auto-derived from the leading character), so the workaround
        # is to prepend one unstyled space: the glyph inherits the plain space, the chip still hugs
        # the word. Tradeoff: a ~1-space indent before such bullets vs. plain-text bullets. Only
        # list items have a glyph, so scope it there.
        if is_list_item and runs and runs[0]["s"] == 0 and runs[0].get("mono"):
            emit(" ")
        base = cursor
        emit(txt)
        emit("\n")
        p_end = cursor
        for r in runs:
            req = _text_style_req({**r, "s": r["s"] + base, "e": r["e"] + base})
            if req:
                text_reqs.append(req)
        if named:
            para_reqs.append({"updateParagraphStyle": {
                "range": {"startIndex": p_start, "endIndex": p_end},
                "paragraphStyle": {"namedStyleType": named},
                "fields": "namedStyleType"}})
        return p_start, p_end

    # Contiguous list blocks get their OWN native list (one createParagraphBullets each), so
    # numbering restarts per list instead of running continuously across the whole document.
    # We record (kind, start, end) ranges here and the executor applies bullets after insert.
    list_blocks = []
    cur_list = None  # {"kind": "bullet"|"numbered", "start": idx, "end": idx}

    def flush_list():
        nonlocal cur_list
        if cur_list:
            list_blocks.append(cur_list)
            cur_list = None

    for b in blocks:
        t = b["type"]
        if t in ("bullet", "numbered"):
            p_start, p_end = emit_para(b["inline"], is_list_item=True)
            if cur_list and cur_list["kind"] == t:
                cur_list["end"] = p_end
            else:
                flush_list()
                cur_list = {"kind": t, "start": p_start, "end": p_end}
            continue
        flush_list()  # any non-list block ends the current run
        if t == "heading":
            emit_para(b["inline"], HEAD[b["level"]])
        elif t == "para":
            emit_para(b["inline"])
        elif t == "hr":
            # The Docs API has no insertHorizontalRule. The faithful equivalent is an empty
            # paragraph carrying a bottom border, which renders as a full-width rule.
            p_start = cursor
            emit("\n")
            para_reqs.append({"updateParagraphStyle": {
                "range": {"startIndex": p_start, "endIndex": cursor},
                "paragraphStyle": {"borderBottom": {
                    "width": {"magnitude": 1, "unit": "PT"},
                    "padding": {"magnitude": 0, "unit": "PT"},
                    "dashStyle": "SOLID",
                    "color": {"color": {"rgbColor": HR_COLOR}}}},
                "fields": "borderBottom"}})
        elif t == "code":
            # monospace paragraphs + grey paragraph shading across the whole block.
            # Separation from surrounding text uses BLANK UNSHADED spacer paragraphs before and
            # after the block — NOT spaceAbove/spaceBelow on the shaded lines, because paragraph
            # shading in Docs fills the spacing region too (it would just make the grey panel
            # taller, with no visible gap). An empty unshaded paragraph renders real whitespace.
            emit("\n")            # spacer paragraph above (unshaded)
            block_start = cursor
            for ln in b["text"].split("\n"):
                st = emit(ln); en = cursor; emit("\n")
                if en > st:
                    text_reqs.append({"updateTextStyle": {
                        "range": {"startIndex": st, "endIndex": en},
                        "textStyle": {"weightedFontFamily": {"fontFamily": MONO_FONT}},
                        "fields": "weightedFontFamily"}})
            block_end = cursor
            emit("\n")            # spacer paragraph below (unshaded)
            # shading across ONLY the block lines (the spacer paragraphs stay white)
            para_reqs.append({"updateParagraphStyle": {
                "range": {"startIndex": block_start, "endIndex": block_end},
                "paragraphStyle": {"shading": {"backgroundColor": {"color": {"rgbColor": CODE_BG}}}},
                "fields": "shading.backgroundColor"}})
        elif t == "table":
            placeholder = f"{TABLE_PLACEHOLDER_PREFIX}{tbl_idx}"
            emit(placeholder); emit("\n")
            tables.append({"placeholder": placeholder,
                           "header": b["header"], "rows": b["rows"]})
            tbl_idx += 1
        elif t == "image":
            placeholder = f"{IMAGE_PLACEHOLDER_PREFIX}{img_idx}"
            emit(placeholder); emit("\n")
            images.append({
                "placeholder": placeholder,
                "src": b["src"],
                "alt": b.get("alt") or "",
                "title": b.get("title"),
                "fallback": b.get("fallback"),
                "width": b.get("width"),
            })
            img_idx += 1

    flush_list()  # trailing list run, if the document ends on a list

    return {
        "text": "".join(text_parts),
        "paragraph_styles": para_reqs,
        "text_styles": text_reqs,
        "list_blocks": list_blocks,
        "tables": tables,
        "images": images,
        "meta": {"insert_at": insert_at, "mono_font": MONO_FONT,
                 "table_placeholder_prefix": TABLE_PLACEHOLDER_PREFIX,
                 "image_placeholder_prefix": IMAGE_PLACEHOLDER_PREFIX,
                 "code_bg": CODE_BG, "link_color": LINK_COLOR,
                 "bullet_preset": "BULLET_DISC_CIRCLE_SQUARE",
                 "numbered_preset": "NUMBERED_DECIMAL_ALPHA_ROMAN"},
    }


def _is_url(src):
    return src.startswith(("http://", "https://"))


def _mime_for(path):
    return IMAGE_MIMES.get(path.suffix.lower()) or mimetypes.guess_type(str(path))[0]


def _rasterize_svg(src, out):
    out.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("rsvg-convert"):
        subprocess.run(["rsvg-convert", "-f", "png", "-o", str(out), str(src)], check=True)
        return
    if shutil.which("magick"):
        subprocess.run(["magick", str(src), str(out)], check=True)
        return
    raise RuntimeError("SVG image requires rasterization, but neither rsvg-convert nor magick is available")


def enrich_images(plan, base_dir=None, image_assets_dir=None):
    base = Path(base_dir or os.getcwd())
    assets = Path(image_assets_dir) if image_assets_dir else None
    for idx, image in enumerate(plan.get("images", [])):
        src = image.get("src") or ""
        if _is_url(src):
            image["public_uri"] = src
            continue

        source = (base / src).resolve()
        image["source_path"] = str(source)
        if not source.exists():
            raise FileNotFoundError(f"image source not found: {src}")

        fallback = image.get("fallback")
        if fallback:
            fallback_path = (base / fallback).resolve()
            if not fallback_path.exists():
                raise FileNotFoundError(f"image fallback not found: {fallback}")
            image["fallback_path"] = str(fallback_path)
            image["fallback_text"] = fallback_path.read_text()

        mime = _mime_for(source)
        image["source_mime_type"] = mime
        if source.suffix.lower() == ".svg":
            if assets:
                out = assets / f"{image.get('placeholder') or IMAGE_PLACEHOLDER_PREFIX + str(idx)}.png"
                _rasterize_svg(source, out)
                image["rendered_path"] = str(out.resolve())
                image["rendered_mime_type"] = "image/png"
        elif mime in IMAGE_MIMES.values():
            image["rendered_path"] = str(source)
            image["rendered_mime_type"] = mime
    return plan


def md_to_plan(md, link_map=None, base_dir=None, image_assets_dir=None):
    plan = build_plan(parse_blocks(md, link_map=link_map), link_map=link_map)
    return enrich_images(plan, base_dir=base_dir, image_assets_dir=image_assets_dir)


def main():
    ap = argparse.ArgumentParser(description="Markdown -> Google Docs batchUpdate plan (JSON).")
    ap.add_argument("input", help="input .md file (or - for stdin)")
    ap.add_argument("--plan-out", default="-", help="output plan JSON path (default stdout)")
    ap.add_argument("--links", help="optional JSON map of code-span text -> URL for auto-linkification")
    ap.add_argument("--image-assets-dir",
                    help="optional directory for rendered image assets; SVG images become PNG files here")
    ap.add_argument("--summary", action="store_true", help="print a human summary to stderr")
    args = ap.parse_args()

    md = sys.stdin.read() if args.input == "-" else open(args.input).read()
    link_map = json.load(open(args.links)) if args.links else None
    base_dir = os.getcwd() if args.input == "-" else os.path.dirname(os.path.abspath(args.input))
    image_assets_dir = args.image_assets_dir
    if image_assets_dir is None and args.plan_out != "-":
        image_assets_dir = f"{args.plan_out}.assets"
    plan = md_to_plan(md, link_map=link_map, base_dir=base_dir, image_assets_dir=image_assets_dir)

    if args.summary:
        nlink = sum(1 for r in plan["text_styles"]
                    if r.get("updateTextStyle", {}).get("textStyle", {}).get("link"))
        print(f"plan: {len(plan['text'])} chars | {len(plan['paragraph_styles'])} para-styles "
              f"| {len(plan['text_styles'])} text-styles ({nlink} links) "
              f"| {len(plan['tables'])} tables | {len(plan.get('images', []))} images",
              file=sys.stderr)

    out = json.dumps(plan, ensure_ascii=False, indent=1)
    if args.plan_out == "-":
        print(out)
    else:
        open(args.plan_out, "w").write(out)


if __name__ == "__main__":
    main()
