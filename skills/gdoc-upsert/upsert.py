#!/usr/bin/env python3
"""Create or update a Google Doc from a md-to-gdoc plan, via the `gws` CLI.

Two modes:
  create:  make a NEW blank doc with the given title, set it PAGELESS (wide text width),
           then fill it from the plan.
  update:  replace the body of an EXISTING doc in place, PRESERVING its current layout
           (pageless or paged) and title. Never touches documentStyle.

Why in-place batchUpdate (and never Drive HTML media-upload) for update: media-upload
HTML->Doc conversion silently flips documentMode PAGELESS -> PAGES. batchUpdate body edits
leave documentStyle untouched, so pageless survives. (Verified empirically.)

The plan JSON is produced by the md-to-gdoc skill's convert.py:
  {text, paragraph_styles, text_styles, tables, images, meta}

CLI:
  upsert.py create --title "T" --plan plan.json [--folder FOLDER_ID] [--go]
  upsert.py update --doc-id DOCID --plan plan.json [--go]
Without --go: prints what it would do and validates; makes NO changes.
With --go: executes. Prints the doc id/url and a verification summary.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

PUBLIC_IMAGE_URI = "https://drive.google.com/uc?export=download&id={file_id}"
SUPPORTED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif"}
DEFAULT_IMAGE_MAX_WIDTH_PT = 468


# ----------------------------------------------------------------- gws helpers
def gws(args, body=None, params=None, cwd=None):
    cmd = ["gws"] + args
    if params is not None:
        cmd += ["--params", json.dumps(params)]
    if body is not None:
        cmd += ["--json", json.dumps(body)]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"gws {' '.join(args)} failed:\n{r.stderr}\n{r.stdout}")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return r.stdout


def doc_get(doc_id):
    return gws(["docs", "documents", "get"], params={"documentId": doc_id})


def doc_batch(doc_id, requests, label=""):
    if not requests:
        return None
    return gws(["docs", "documents", "batchUpdate"],
               params={"documentId": doc_id}, body={"requests": requests})


def doc_mode(doc):
    return doc.get("documentStyle", {}).get("documentFormat", {}).get("documentMode") \
        or doc.get("documentStyle", {}).get("documentMode")


# ----------------------------------------------------------------- table phase
def find_placeholder(doc, placeholder):
    """Return (start, end, para_start) of the placeholder text in a top-level paragraph."""
    for el in doc["body"]["content"]:
        p = el.get("paragraph")
        if not p:
            continue
        for e in p.get("elements", []):
            tr = e.get("textRun")
            if tr and placeholder in tr.get("content", ""):
                off = tr["content"].index(placeholder)
                s = e["startIndex"] + off
                return s, s + len(placeholder), el["startIndex"]
    return None


def find_empty_table(doc):
    for el in doc["body"]["content"]:
        tb = el.get("table")
        if not tb:
            continue
        empty = True
        for r in tb.get("tableRows", []):
            for c in r.get("tableCells", []):
                txt = "".join(
                    pe.get("textRun", {}).get("content", "")
                    for ce in c.get("content", [])
                    for pe in ce.get("paragraph", {}).get("elements", []))
                if txt.strip():
                    empty = False
        if empty:
            return tb
    return None


def _run_style_req(run, start, mono, code_bg, link_color):
    """Build an updateTextStyle request for a pre-parsed run, offsets relative to `start`."""
    ts, fields = {}, []
    if run.get("bold"):
        ts["bold"] = True; fields.append("bold")
    if run.get("italic"):
        ts["italic"] = True; fields.append("italic")
    if run.get("mono"):
        ts["weightedFontFamily"] = {"fontFamily": mono}; fields.append("weightedFontFamily")
        ts["backgroundColor"] = {"color": {"rgbColor": code_bg}}; fields.append("backgroundColor")
    if run.get("link"):
        ts["link"] = {"url": run["link"]}; fields.append("link")
        ts["foregroundColor"] = {"color": {"rgbColor": link_color}}; fields.append("foregroundColor")
        ts["underline"] = True; fields.append("underline")
    if not fields:
        return None
    return {"updateTextStyle": {
        "range": {"startIndex": run["s"] + start, "endIndex": run["e"] + start},
        "textStyle": ts, "fields": ",".join(fields)}}


def cell_fill_ops(table, header, rows, mono, code_bg, link_color):
    """Ordered insert+style ops, processed LAST cell first so indices stay valid in-batch.
    Cells are PRE-PARSED {text, runs} from the plan (no markdown parsing needed here)."""
    grid = [header] + rows
    cells = []
    for ri, r in enumerate(table["tableRows"]):
        for ci, c in enumerate(r["tableCells"]):
            start = c["content"][0]["startIndex"]
            cell = grid[ri][ci] if ri < len(grid) and ci < len(grid[ri]) else {"text": "", "runs": []}
            cells.append((start, ri, ci, cell))
    cells.sort(key=lambda x: x[0], reverse=True)

    ops = []
    for start, ri, ci, cell in cells:
        txt = cell.get("text", "")
        if not txt:
            continue
        ops.append({"insertText": {"location": {"index": start}, "text": txt}})
        if ri == 0:  # header row: bold the whole cell
            ops.append({"updateTextStyle": {
                "range": {"startIndex": start, "endIndex": start + len(txt)},
                "textStyle": {"bold": True}, "fields": "bold"}})
        for run in cell.get("runs", []):
            req = _run_style_req(run, start, mono, code_bg, link_color)
            if req:
                ops.append(req)
    return ops


# ----------------------------------------------------------------- image phase
def _image_count(plan):
    return len(plan.get("images", []))


def _fallback_text(spec):
    text = spec.get("fallback_text")
    if text:
        return text.rstrip("\n")
    raise RuntimeError(
        f"image {spec.get('src')!r} has no ASCII fallback; add "
        "`<!-- gdoc:image fallback=path.txt -->` or use --image-mode=public-staged")


def _image_file(spec):
    path = spec.get("rendered_path") or spec.get("source_path")
    if not path:
        raise RuntimeError(f"image {spec.get('src')!r} has no local rendered image path")
    mime = spec.get("rendered_mime_type") or spec.get("source_mime_type")
    if mime not in SUPPORTED_IMAGE_MIMES:
        raise RuntimeError(
            f"image {spec.get('src')!r} has MIME {mime!r}; Docs insertInlineImage needs PNG, JPEG, or GIF")
    return Path(path), mime


def validate_images(plan, args):
    if not _image_count(plan):
        return
    if args.image_mode == "ascii":
        for spec in plan.get("images", []):
            _fallback_text(spec)
        return
    if args.image_mode == "public-staged":
        for spec in plan.get("images", []):
            if spec.get("public_uri"):
                continue
            _image_file(spec)
        if not args.allow_public_image_staging:
            raise RuntimeError(
                "--image-mode=public-staged requires --allow-public-image-staging because Docs "
                "can only fetch images from public URLs")


def _insert_ascii_image_fallback(doc_id, spec, mono, code_bg):
    doc = doc_get(doc_id)
    loc = find_placeholder(doc, spec["placeholder"])
    if not loc:
        raise RuntimeError(f"placeholder {spec['placeholder']!r} not found")
    start, end, _ = loc
    text = _fallback_text(spec)
    doc_batch(doc_id, [{"deleteContentRange": {"range": {"startIndex": start, "endIndex": end}}}],
              "del-image-placeholder")
    reqs = [{"insertText": {"location": {"index": start}, "text": text}}]
    if text:
        reqs.append({"updateTextStyle": {
            "range": {"startIndex": start, "endIndex": start + len(text)},
            "textStyle": {"weightedFontFamily": {"fontFamily": mono}},
            "fields": "weightedFontFamily"}})
        reqs.append({"updateParagraphStyle": {
            "range": {"startIndex": start, "endIndex": start + len(text) + 1},
            "paragraphStyle": {"shading": {"backgroundColor": {"color": {"rgbColor": code_bg}}}},
            "fields": "shading.backgroundColor"}})
    doc_batch(doc_id, reqs, "image-ascii-fallback")


def _stage_public_image(path, mime):
    created = gws(["drive", "files", "create",
                   "--upload", path.name,
                   "--upload-content-type", mime],
                  body={"name": path.name, "mimeType": mime},
                  params={"fields": "id,name,mimeType,webViewLink"},
                  cwd=str(path.parent))
    file_id = created["id"]
    gws(["drive", "permissions", "create"],
        body={"role": "reader", "type": "anyone"},
        params={"fileId": file_id, "fields": "id"})
    return file_id, PUBLIC_IMAGE_URI.format(file_id=file_id)


def _delete_drive_file(file_id):
    return gws(["drive", "files", "delete"], params={"fileId": file_id})


def _width_pt(spec, default_width_pt):
    width = str(spec.get("width") or "").strip().lower()
    if not width or width in {"full", "wide"}:
        return default_width_pt
    if width.endswith("pt"):
        width = width[:-2]
    try:
        return float(width)
    except ValueError as e:
        raise RuntimeError(f"unsupported image width {spec.get('width')!r}; use a point value or 'full'") from e


def _insert_public_image(doc_id, spec, default_width_pt, keep_staged_images):
    staged_file_id = None
    if spec.get("public_uri"):
        uri = spec["public_uri"]
    else:
        path, mime = _image_file(spec)
        staged_file_id, uri = _stage_public_image(path, mime)
    try:
        doc = doc_get(doc_id)
        loc = find_placeholder(doc, spec["placeholder"])
        if not loc:
            raise RuntimeError(f"placeholder {spec['placeholder']!r} not found")
        start, end, _ = loc
        doc_batch(doc_id, [{"deleteContentRange": {"range": {"startIndex": start, "endIndex": end}}}],
                  "del-image-placeholder")
        doc_batch(doc_id, [{"insertInlineImage": {
            "uri": uri,
            "location": {"index": start},
            "objectSize": {"width": {"magnitude": _width_pt(spec, default_width_pt), "unit": "PT"}},
        }}], "insertInlineImage")
    finally:
        if staged_file_id and not keep_staged_images:
            _delete_drive_file(staged_file_id)


def fill_images_from_plan(doc_id, plan, args, mono, code_bg):
    if not _image_count(plan):
        return
    validate_images(plan, args)
    for spec in reversed(plan.get("images", [])):
        if args.image_mode == "ascii":
            _insert_ascii_image_fallback(doc_id, spec, mono, code_bg)
        elif args.image_mode == "public-staged":
            _insert_public_image(doc_id, spec, args.image_max_width_pt, args.keep_staged_images)
        else:
            raise RuntimeError(f"unknown image mode: {args.image_mode}")


# ----------------------------------------------------------------- fill (shared)
def fill_from_plan(doc_id, plan, args):
    meta = plan.get("meta", {})
    mono = meta.get("mono_font", "Consolas")
    code_bg = meta.get("code_bg", {"red": 0.945, "green": 0.945, "blue": 0.945})
    link_color = meta.get("link_color", {"red": 0.07, "green": 0.33, "blue": 0.8})

    # Phase 1: insert the whole text blob, then paragraph + text styles (one batch).
    reqs = [{"insertText": {"location": {"index": meta.get("insert_at", 1)},
                            "text": plan["text"]}}]
    reqs += plan.get("paragraph_styles", [])
    reqs += plan.get("text_styles", [])
    doc_batch(doc_id, reqs, "insert+style")

    # Phase 1b: native lists. One createParagraphBullets per contiguous block so each list
    # gets its OWN listId and numbers restart per list (not continuously across the doc).
    # Applied before tables, while the plan's blob-relative ranges are still valid.
    bullet_preset = meta.get("bullet_preset", "BULLET_DISC_CIRCLE_SQUARE")
    numbered_preset = meta.get("numbered_preset", "NUMBERED_DECIMAL_ALPHA_ROMAN")
    list_reqs = []
    for blk in plan.get("list_blocks", []):
        preset = numbered_preset if blk["kind"] == "numbered" else bullet_preset
        list_reqs.append({"createParagraphBullets": {
            "range": {"startIndex": blk["start"], "endIndex": blk["end"]},
            "bulletPreset": preset}})
    if list_reqs:
        doc_batch(doc_id, list_reqs, "lists")

    # Phase 2: tables, LAST first so earlier placeholder indices stay valid.
    for spec in reversed(plan.get("tables", [])):
        doc = doc_get(doc_id)
        loc = find_placeholder(doc, spec["placeholder"])
        if not loc:
            raise RuntimeError(f"placeholder {spec['placeholder']!r} not found")
        _, _, para_start = loc
        ncols = len(spec["header"])
        nrows = 1 + len(spec["rows"])
        doc_batch(doc_id, [{"insertTable": {
            "rows": nrows, "columns": ncols, "location": {"index": para_start}}}],
            "insertTable")
        doc = doc_get(doc_id)
        loc2 = find_placeholder(doc, spec["placeholder"])
        if loc2:
            ps, pe, _ = loc2
            doc_batch(doc_id, [{"deleteContentRange": {
                "range": {"startIndex": ps, "endIndex": pe}}}], "del-placeholder")
        doc = doc_get(doc_id)
        table = find_empty_table(doc)
        if table is None:
            raise RuntimeError("freshly inserted empty table not found")
        ops = cell_fill_ops(table, spec["header"], spec["rows"],
                            mono, code_bg, link_color)
        doc_batch(doc_id, ops, "fill-cells")

    # Phase 3: standalone images. Defaults to ASCII fallback; public staging is explicit because
    # Docs insertInlineImage requires a public URI and cannot consume local bytes or data URIs.
    fill_images_from_plan(doc_id, plan, args, mono, code_bg)


# ----------------------------------------------------------------- modes
def do_create(args, plan):
    validate_images(plan, args)
    if not args.go:
        print(f"[dry] would CREATE doc titled {args.title!r}, set PAGELESS, fill from plan "
              f"({len(plan['text'])} chars, {len(plan['tables'])} tables, "
              f"{_image_count(plan)} images via {args.image_mode}).")
        return
    created = gws(["docs", "documents", "create"], body={"title": args.title})
    doc_id = created["documentId"]
    # set pageless (wide text width). updateDocumentStyle with documentFormat.documentMode.
    doc_batch(doc_id, [{"updateDocumentStyle": {
        "documentStyle": {"documentFormat": {"documentMode": "PAGELESS"}},
        "fields": "documentFormat.documentMode"}}], "set-pageless")
    if args.folder:
        gws(["drive", "files", "update"],
            params={"fileId": doc_id, "addParents": args.folder})
    fill_from_plan(doc_id, plan, args)
    _report(doc_id, expect_mode="PAGELESS")


def do_update(args, plan):
    validate_images(plan, args)
    if not args.go:
        print(f"[dry] would UPDATE {args.doc_id}: GET current layout, delete existing body, "
              f"refill from plan ({len(plan['text'])} chars, {len(plan['tables'])} tables, "
              f"{_image_count(plan)} images via {args.image_mode}). "
              f"documentStyle untouched -> existing layout (pageless/paged) preserved.")
        return
    doc = doc_get(args.doc_id)
    before = doc_mode(doc)
    last_end = doc["body"]["content"][-1]["endIndex"]
    if last_end > 2:
        doc_batch(args.doc_id, [{"deleteContentRange": {
            "range": {"startIndex": 1, "endIndex": last_end - 1}}}], "delete-body")
    fill_from_plan(args.doc_id, plan, args)
    _report(args.doc_id, expect_mode=before)


def _report(doc_id, expect_mode=None):
    doc = doc_get(doc_id)
    mode = doc_mode(doc)
    body = doc["body"]["content"]
    heads = sum(1 for el in body if el.get("paragraph", {}).get("paragraphStyle", {})
                .get("namedStyleType", "").startswith("HEADING"))
    tables = sum(1 for el in body if "table" in el)
    bulleted = sum(1 for el in body if "bullet" in el.get("paragraph", {}))
    images = len(doc.get("inlineObjects", {}))
    url = f"https://docs.google.com/document/d/{doc_id}/edit"
    print(f"doc: {url}")
    print(f"  documentMode={mode}" + (f" (expected {expect_mode})" if expect_mode else ""))
    print(f"  headings={heads} tables={tables} images={images} native-list-items={bulleted}")
    if expect_mode and mode != expect_mode:
        print("  WARNING: documentMode changed unexpectedly!", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Create/update a Google Doc from a md-to-gdoc plan.")
    sub = ap.add_subparsers(dest="mode", required=True)

    def add_common(p):
        p.add_argument("--image-mode", choices=("ascii", "public-staged"), default="ascii",
                       help="how to handle standalone Markdown images from the plan")
        p.add_argument("--image-max-width-pt", type=float, default=DEFAULT_IMAGE_MAX_WIDTH_PT,
                       help="rendered image width for insertInlineImage")
        p.add_argument("--allow-public-image-staging", action="store_true",
                       help="allow temporary public Drive staging for local images")
        p.add_argument("--keep-staged-images", action="store_true",
                       help="do not delete temporary staged Drive images after insertion")

    c = sub.add_parser("create", help="create a new pageless doc")
    c.add_argument("--title", required=True)
    c.add_argument("--plan", required=True, help="plan JSON from md-to-gdoc convert.py")
    c.add_argument("--folder", help="optional Drive folder id to place the doc in")
    c.add_argument("--go", action="store_true")
    add_common(c)

    u = sub.add_parser("update", help="replace an existing doc's body in place")
    u.add_argument("--doc-id", required=True)
    u.add_argument("--plan", required=True)
    u.add_argument("--go", action="store_true")
    add_common(u)

    args = ap.parse_args()
    plan = json.load(open(args.plan))

    if args.mode == "create":
        do_create(args, plan)
    else:
        do_update(args, plan)


if __name__ == "__main__":
    main()
