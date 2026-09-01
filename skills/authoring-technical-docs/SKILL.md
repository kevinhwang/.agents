---
name: authoring-technical-docs
description: "Guidance for how to author and edit technical prose, e.g. for design docs, investigation writeups, and architecture / decision docs, PR descriptions and discussion."
metadata:
  author: Kevin Hwang (https://kevinhwang.dev)
---

# Authoring technical docs

Guidance for writing and editing technical prose — design docs, RFCs, investigation/root-cause writeups, architecture and decision records. The goal is a document that is **technical and precise but high-altitude**, fast to read, and durable as the system evolves.

## When to use

- Writing a new design doc, proposal, RFC, or investigation/analysis writeup.
- Editing or restructuring an existing engineering doc.
- Any "living" doc that describes how a system works now and where it is going.
- PR descriptions
- Technical discussions, e.g., on Slack, PR comments

---

## Altitude: write at the concept level

Aim at domain concepts, systems, features, behaviors, and semantics — not implementation mechanics. The reader should understand *what the system does and why*, not read the code through prose.

- **Describe behavior and intent**, not control flow. No code excerpts. If proposing new logic and code is actually required, sketch it in a few lines of **concise pseudocode**, not real code.
- **No literal line references** (`//foo/bar/baz:103-120`) — they rot immediately.
- **No section-number cross-references into other docs** ("see §4.3," "per §8 of the design") — they break when the other doc is restructured. Link to the doc and describe what it covers ("the design's cross-namespace dependency"), so the reference survives renumbering.
- **Reference durable symbols only by necessity**: a service name, a proto message/service, a package, or an interface — when naming it genuinely aids understanding. Prefer the smallest such reference that carries the meaning.
  - Note: When the point *is* code — e.g., introducing or modifying interfaces, classes, or durable API methods, especially in the context of in a PR — you *should* name symbols (usually codefenced) directly, rather than just natural langauge prose.
  - The test: 1) If it aids in reader clarity about what is changing or what is responsible, and 2) It's the appropriate level of abstraction given the nature of the communication — e.g., if a doc is about refactoring code, or if referring to a system that has a formal name (e.g, `FooService`).
- **Attach code links to prose**, not bare paths or URLs. Write "the [document resolver](path) builds the auth," not "see `go/src/.../document_resolver`." Use a bare path or URL only when the intent is to call out that path/URL *by name*, or it has no natural prose anchor.

## Doc Structure: summary first, detail later

Lead with the conclusion; expand underneath. A reader should get the gist from the top and drill down only as needed.

1. **TL;DR / Summary** — deliberately short and terse. State the outcome, the key decisions, and the headline. Use a table or a few bullets, not paragraphs. This is the part most people read.
2. **Context / Background** — *high-level* orientation: what a reader must know to understand the rest and why it matters. Enough to motivate; not a history lesson.
3. **Detail sections** — the full breakdown, one concept/domain per section.
4. **Open questions / decisions / next steps** — where relevant.

Break content by concept with logical headings. Keep paragraphs focused on one idea.

## Visual aids: tables and diagrams

- **Tables/matrices** for comparisons, contrasts, and executive summaries — field-by-field, option-by-option, behavior-by-status. Far more scannable than prose for structured data.
- **Diagrams** for architecture, data flow, system relationships, or sequence — whenever they help a reader build a mental model faster than prose would.
  - Use native Mermaid fenced blocks for Markdown-first diagrams when the medium (e.g., GitHub Markdown, Confluence) supports Mermaid. Before creating or hand-editing an SVG, render with installed `mmdc`: `mmdc -i diagram.mmd -o diagram.png -b transparent -s 2`; if unavailable, use `npx -p @mermaid-js/mermaid-cli mmdc` with the same arguments. Use PNG by default for Google Docs; JPEG is also suitable when needed.
  - Keep hand-authored SVG as an option for diagrams that need custom layout or styling. If Markdown already references an SVG rather than native Mermaid, retain it as the source and rasterize it to PNG/JPEG for Google Docs content.
  - Use ASCII art when raster images cannot be staged in Google Docs or when a text fallback is more appropriate.
  - Prefer native Mermaid for a simple workflow, sequence, or linear timeline with straightforward top-to-bottom or left-to-right flow. Use it when automatic layout is sufficient: no subgraphs, custom placement, fixed positioning, or elaborate connector treatment.
  - Use hand-authored SVG for more complicated diagrams: multiple trust boundaries or regions, non-linear topology, exact spatial composition, custom routing, or presentation-quality typography and labels. Do not force Mermaid to approximate a layout it cannot control.
  - `openai-hugging-face-incident.mmd` and `openai-hugging-face-incident.svg` are a paired reference: raw Mermaid beside a presentation-quality SVG rendition. The Mermaid example intentionally uses `block-beta` to show its fixed-grid limit; treat that as the point where custom SVG may be the better final artifact. The Mermaid reference uses a transparent canvas; the SVG intentionally owns a `#1D1D1D` canvas so light text stays legible when embedded in a white document. Retain system sans typography, rounded dark nodes, high-contrast light text, muted dark fills, and brighter semantic outlines. Green = evaluation environment, blue = public read-back, ochre = external/perimeter access, purple = network pivot, pink = source-control impact. `block-beta` does not portably theme link strokes or edge-label backgrounds; include the dark edge-label CSS when labels are needed, but use custom SVG when connector styling must be exact.
  - See `agent-loop-diagram.svg` / `codex-hooks-lifecycle-diagram.svg` / `claude-code-hooks-lifecycle-diagram.svg` for additional custom-SVG examples.
- **Emojis sparingly and only when informational** — e.g., status signals in a summary table (🔴 broken / 🟡 partial / 🟢 done / ⚠️ risk) earn their place. Decoration does not. Keep it professional.

## Prose mechanics: tight and professional

**Voice — newspaper-headline brevity.**

- Omit articles (the/a/an), conjunctions (where clarity holds), first personal pronouns when the antecedent is clear from context.
- Use of commas, semicolons, colons, dashes, other symbols for separation.
- Sacrifice grammatical / literary elegance for concision and clarity.

Examples:

| Scenario                         | Wrong 🚫                                                                                                              | Right ✅                                                         |
|----------------------------------|----------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------|
| Investigation                    | We found that the connector emits no ancestor metadata, so the resolver then enters the fallback path.               | Connector emits no ancestor metadata → resolver enters fallback |
| Design doc ("Objective" section) | This doc outlines the proposal for migration plan for the Foo service, the Bar service, and the Baz service.         | Migration plan for Foo, Bar, Baz services                       |
| PR description / commit message  | Added fuzz testing for FooBarBaz service in //foo/bar/baz package along with deflaking existing mutation test fixes. | Add fuzz tests for FooBarBaz; deflake mutation tests            |

**No flowery or narrative language.**
- No "the smoking gun," "one minor wrinkle," "the key insight," "the load-bearing assumption," "interestingly," and similar.
- Just state the claim — you can still be honest about level of confidence if applicable.
- No ChatGPT-isms — No "Rhetorical question? Answer" or dramatic rhetorical devices like "Not X. Not Y. Z." or "It's not X. It's Y."
- No idioms or corporate jargon.

**Symbols for logical flow:**
- `→` for sequence / temporal progression / "X then Y" / "X leads to Y" (workflow steps, cause → effect over time).
- `⇒` for logical implication / "therefore", i.e. "because of antecedent, therefore conclude/do X" — entailment, not temporal.
- `~` for "approximately" on numerical figures, e.g., "~50%".

**Abbreviations**:
- Use e.g., i.e., etc. where appropriate.
- Prefer *common* abbreviations and initialisms over their expansions when clear from context. E.g., "document" → "doc", "Kubernetes" → "K8s"
  - Use tastefully, don't overdo it.

**Lists over run-ons.** Convert long comma-separated series into bullets or an ordered list. Break run-on sentences and paragraphs. One idea per paragraph.

**Don't restate a set's cardinality in prose.** Refer to a set by name or by "these/them" ("none of these fields," "the audited fields," "this audit's scope"), not by counting it ("none of the six fields," "the only one of the three"). The count is redundant when the set is clear from context, carries no information, and rots the moment the set changes — risking internal inconsistency if some mentions update and others don't. State a count only when the number itself is the point (e.g., "fans out to 200 namespaces").

## Living-doc discipline (especially when editing a doc you/we authored)

A doc describes the system **as it is now** and **where it is going** — present tense, forward-looking.
- **No retrospective framing, addenda, or worklog/meta-narrative.** Do not narrate the investigation or the editing process ("we first thought X, then found Y," "updated to correct…," "originally this said…").
- **When new understanding arrives, rewrite the affected section** so it reads as current truth — do not bolt on a correction or changelog entry.
- The reader should never be able to tell the doc was revised; it should read as if authored fresh against today's reality.

---

## Publishing to Google Docs

Default workflow: **write the doc as Markdown to a local file first** (a temp dir is good) for local review before any upload. Once reviewed, render it to a real Google Doc with the two companion skills:
- **`md-to-gdoc`** — converts Markdown → a Google Docs `batchUpdate` plan (real headings, native tables, lists, fenced code, inline-code chips, live links). Includes `coderef.py`, which expands repo-relative code links in prose to full Sourcegraph (dropbox-internal repos) or GitHub (others) URLs — matching the "links on prose" style above.
- **`gdoc-upsert`** — executes the plan to create a new pageless Doc or update an existing one in place (preserving its layout).

**Run conversion, upload, and the output verification / round-trip sanity-check in a non-blocking background subagent** (`run_in_background: true`) so the verbose tool output does not consume the main session's context window. The subagent converts, sanity-checks the generated structure, uploads, re-fetches the live doc to confirm nothing was dropped or mangled, and returns only a concise pass/fail summary. See those skills for the exact commands and checklists.

---

## Conversational Prose

Conversational prose (e.g., in Slack, GitHub, Jira comments discussions) is not a doc — it must be much more terse and skimmable. No essays, no long, flowing paragraphs. Where appropriate:
- Sentence fragments > paragraphs and complete sentences.
- Informal / casual voice > verbose formalities.
- "Ack" or "LGTM" or "👍" if that's all that's needed.
