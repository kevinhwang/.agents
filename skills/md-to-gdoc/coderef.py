#!/usr/bin/env python3
"""Expand repo-relative code references in Markdown links into full code-host URLs.

A PRE-PROCESSOR for the md-to-gdoc pipeline. It rewrites Markdown links whose href is a
repo-relative path (not already a URL) into a full link to the code host:

  - Repos under the `dropbox-internal` GitHub org (e.g. the rSERVER `server` repo)
    -> Dropbox Sourcegraph    https://dropbox.sourcegraphcloud.com/github.com/<org>/<repo>/-/{blob|tree}/<path>
  - Any other org/repo
    -> generic GitHub          https://github.com/<org>/<repo>/{blob|tree}/<branch>/<path>

This supports the preferred authoring style: write a readable prose anchor with a bare
repo path as the href, e.g.

    the [document resolver](go/src/dropbox/dash/kafka/consumer_handlers/document_resolver)

and let this tool turn the href into the correct full URL. The author never pastes long
URLs or bare code paths into the prose.

Href forms recognized (anything else — http(s)://, #anchors, mailto:, tel:, site-absolute
/paths, and image `![]()` sources — is left untouched):

  some/repo/relative/path                 -> uses --repo (default github.com/dropbox-internal/server)
  github.com/<org>/<repo>/some/path        -> that explicit org/repo (org decides Sourcegraph vs GitHub)
  //go/src/foo/bar                         -> leading bazel-style `//` is stripped, then --repo

blob vs tree is inferred: a final segment containing a dot (a file extension) -> blob;
otherwise -> tree. A trailing slash forces tree.

Usage:
    python3 coderef.py INPUT.md            # rewritten markdown to stdout
    python3 coderef.py INPUT.md -o OUT.md
    python3 coderef.py INPUT.md --repo github.com/dropbox-internal/server --summary
    cat INPUT.md | python3 coderef.py -    # stdin

Pure Python 3 stdlib. No third-party deps, no network.
"""
import argparse
import re
import sys

DROPBOX_SG_HOST = "https://dropbox.sourcegraphcloud.com"
DROPBOX_ORG = "dropbox-internal"
GITHUB_HOST = "https://github.com"
DEFAULT_REPO = "github.com/dropbox-internal/server"
DEFAULT_GITHUB_BRANCH = "main"

# [text](href) or [text](href "title"); a leading '!' (image) is captured so we can skip it.
_LINK_RE = re.compile(r'(!?)\[([^\]]*)\]\(\s*([^)\s]+?)(?:\s+"[^"]*")?\s*\)')
# Hrefs we must NOT treat as repo paths. A single leading '/' is a site-absolute URL (skip);
# a bazel-style '//' prefix IS a supported repo-ref form and is handled before this guard.
_NON_PATH_PREFIX = re.compile(r'^(?:[a-z][a-z0-9+.-]*://|#|mailto:|tel:|/(?!/))', re.IGNORECASE)


def _split_repo(spec):
    """'github.com/org/repo' or 'org/repo' -> ('org', 'repo'). Returns None if malformed."""
    spec = spec.strip().removeprefix("github.com/").strip("/")
    parts = spec.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def _is_blob(path):
    """File (blob) if the last path segment has an extension; dir (tree) otherwise."""
    if path.endswith("/"):
        return False
    last = path.rstrip("/").split("/")[-1]
    return "." in last and not last.startswith(".")  # ".github" dir stays a tree


def build_url(href, default_repo, branch=None):
    """Repo-relative href -> full code-host URL, or None to leave the link unchanged."""
    if not href or _NON_PATH_PREFIX.match(href):
        return None

    href = href.strip()
    # Explicit github.com/<org>/<repo>/<path...> form overrides --repo.
    if href.startswith("github.com/"):
        rest = href[len("github.com/"):]
        bits = rest.split("/")
        if len(bits) < 3:
            return None  # need org/repo/<at least one path segment>
        org, repo = bits[0], bits[1]
        path = "/".join(bits[2:])
    else:
        rr = _split_repo(default_repo)
        if rr is None:
            return None
        org, repo = rr
        path = href.lstrip("/")          # strip bazel-style leading //
        path = path.split(":", 1)[0]     # drop any bazel :target / :line suffix

    path = path.strip("/")
    if not path:
        return None

    kind = "blob" if _is_blob(href if href.startswith("github.com/") else path) else "tree"

    if org == DROPBOX_ORG:
        # Sourcegraph: omit @rev by default so it tracks the default branch (good for living
        # docs). Pin only when an explicit branch/rev is requested.
        rev = f"@{branch}" if branch else ""
        return f"{DROPBOX_SG_HOST}/github.com/{org}/{repo}{rev}/-/{kind}/{path}"
    # Generic GitHub always needs a branch in the path.
    return f"{GITHUB_HOST}/{org}/{repo}/{kind}/{branch or DEFAULT_GITHUB_BRANCH}/{path}"


def rewrite(md, default_repo=DEFAULT_REPO, branch=None):
    """Rewrite repo-relative link hrefs in `md`. Returns (new_md, [(anchor, old, new), ...])."""
    changes = []

    def repl(m):
        bang, text, href = m.group(1), m.group(2), m.group(3)
        if bang:                          # image source, never a code ref
            return m.group(0)
        url = build_url(href, default_repo, branch)
        if not url:
            return m.group(0)
        changes.append((text, href, url))
        return f"[{text}]({url})"

    return _LINK_RE.sub(repl, md), changes


def main():
    ap = argparse.ArgumentParser(description="Expand repo-relative code refs in Markdown links to full URLs.")
    ap.add_argument("input", help="input .md (or - for stdin)")
    ap.add_argument("-o", "--out", default="-", help="output path (default stdout)")
    ap.add_argument("--repo", default=DEFAULT_REPO,
                    help=f"default repo for bare paths, 'github.com/org/repo' or 'org/repo' (default {DEFAULT_REPO})")
    ap.add_argument("--branch", default=None,
                    help="branch/rev. Sourcegraph: omitted => default branch (recommended for living docs). "
                         f"GitHub: defaults to '{DEFAULT_GITHUB_BRANCH}'.")
    ap.add_argument("--summary", action="store_true", help="print rewrites to stderr")
    args = ap.parse_args()

    md = sys.stdin.read() if args.input == "-" else open(args.input).read()
    out, changes = rewrite(md, default_repo=args.repo, branch=args.branch)

    if args.out == "-":
        sys.stdout.write(out)
    else:
        with open(args.out, "w") as f:
            f.write(out)

    if args.summary:
        print(f"coderef: rewrote {len(changes)} link(s)", file=sys.stderr)
        for text, old, new in changes:
            print(f"  [{text}]  {old}  ->  {new}", file=sys.stderr)


if __name__ == "__main__":
    main()
