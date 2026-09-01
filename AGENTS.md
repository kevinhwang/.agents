# ~/.agents/AGENTS.md

User-tier global rules for agents:

## Assistant Conversational Style

- Be terse and pragmatic
- Don't restate context
- Thinking / reasoning prose:
  - No "First, let me..." — just state what you're doing, and be concise.
  - No "I'll search the code to keep this grounded rather than guessing" — just say "Exploring foo…"
  - No "Rhetorical question? Answer."
  - No "You're right to push back" meta-narrative.
  - You *may* meta-narrate reasoning as it relates to the conversation if it's relevant and important, e.g., "You're right, I overstated X," or if issuing corrections or clarifications.
  - If you're not sure or confident, or you would like to push back, state that.
- No flowery narrative: drop "the smoking gun", "one minor wrinkle", "the key insight", "the load bearing assumption", "interestingly", and similar — just state the observation / claim, but be honest about level of confidence (if applicable).
- Sacrifice grammatical / literary elegance for concision and clarity.
- Use logical paragraphs, bullet points, ordered lists, and tables to structure prose and data for clarity.

## Code Style

DRY, but not prematurely (YAGNI) — only when a pattern / workflow is multiply repeated or overly complicated and readability could be helped should you break things up into logical components and *tasteful* (KISS) abstractions, refactoring existing code if necessary.

For new code, comments, docs, PR descriptions, etc., do NOT arbitrarily wrap at 80 characters — default to the 120-char width standard, unless following a language or codebase convention. You can still wrap earlier at logical break points (e.g., paragraph boundaries, for pretty formatting, etc.).

See `/authoring-technical-docs` skill for more guidance on writing technical prose (e.g., Confluence / Google Docs, GitHub PR descriptions, Markdown files, etc.).

### Comments

- Comments should explain *why*, not *what*.
- Focus on non-obvious details, relevant design, intentional tradeoffs, or important context.
- Do not add comments that just summarize or repeat the code.
- If the code is self-explanatory, no comment is needed.

### Variables

- Inline single use variables at their use sites if the expression is straightforward.
- Intermediate variables are useful when they clarify intent or when the expression is complex.
- Prefer defining and documenting constants over inline use of constant literals. The exception is for simple, trivial constants (e.g., `0`) if their use is self-explanatory.

### Control Flow

Prefer the early return / guard clauses pattern over deeply nested conditionals.

## Tests

Always add or update tests when appropriate for a change.

The majority of your tests should be unit tests covering representative, realistic flows, as well as representative and important edge cases.

Tests should be human-readable and easy to follow along, easily understood by a reader to be correct by visual inspection ⇒ Don't add too many abstractions or too much indirection in your tests.

For test cases covering one logical concept but with multiple potential sub-cases to be covered, follow the [subtest pattern](https://go.dev/blog/subtests) to parameterize a test case.

Follow the "arrange, act, assert" pattern — comment to indicate where each phase starts where appropriate to help with readability (e.g., in larger tests).

## Workflow Rules

Explore, interview, ask clarifying questions (if necessary), *then* plan. If there are open questions and multiple approaches, make a recommendation and explain tradeoffs.

Where appropriate, break up work into tasks. Use *background* (i.e., non-blocking, async) subagents where appropriate to parallelize work or avoid bloating main session context or blocking the main conversation.

### Making changes

- Always format (e.g, `go fmt`, `terraform fmt`, etc.) changed files.
- Always run all tests for affected packages.

### GitHub

When using `gh` CLI, note which user you're auth'd as:

- kevinhwang_dbx is my Dropbox EMU account, used with anything in the https://github.com/dropbox-internal org.
- kevinhwang is my personal GitHub account, for use with all others.

If `gh` is currently pointed at the wrong account, `gh auth switch`.

### Submitting changes

Your commit messages and PR descriptions must be **concise**, **high level** summaries, not verbose implementation worklogs.

Example PR description:

```markdown
## Summary

**Short, concise, HIGH LEVEL** summary of the change.

## Context

Provide context, describe the problem, high level goal, and motivating factors.

## Approach

Summarize any key design decisions made and any principles involved, alternatives explored and any tradeoffs or limitations, if applicable.

## Key Changes

Summarize the key changes—existing behavior if relevant, new behavior. Keep it big picture, do not go into implementation details, do not excerpt actual code changes.

Do NOT add fluff about: "Added: full test coverage" or "Test plan: add unit tests, all tests passed."
It is taken for granted every change comes with relevant tests, and they all pass.
Only call out changes to tests if it's relevant, e.g., fixing broken or incorrect tests, fixing gaps, etc.

## Follow-ups

Optional, only if relevant.
```

For each of these sections, only use them if they're relevant to the change.

Do NOT go into specific lines of code, specific files or line numbers. Avoid going into implementation details like specific classes or methods unless it's really important to know.
Keep it high level, focused on high level concepts, semantics, logical components of the systems being modified, and external systems if involved.

## Prose

Follow the `authoring-technical-docs` skill for guidance on technical prose.

Do NOT include AI attribution such as "Generated / co-authored by <model or agent>" or similar in any output — do not add it to comments, docs, or conversations.

## Environment notes

- **Coreutils are GNU-flavored here**, not BSD/macOS — despite the platform being darwin.
  - Homebrew's GNU tools (`sed`, `grep`, `find`, `date`, `stat`, `tar`, etc.) are on `PATH` ahead of `/usr/bin`, so a bare command resolves to the GNU build.
  - Use GNU flag formats, not BSD/darwin ones — e.g. in-place edit is GNU `sed -i 's/a/b/g' f`, **not** BSD `sed -i '' 's/a/b/g' f`.
- **In scripts, prefer a `#!/usr/bin/env bash` hashbang** hardcoded `#!/bin/bash`. Same for Python, etc.
  - An unqualified `bash` call already resolves via `PATH`, so this only matters for hashbangs.
- Do NOT run commands in interactive / login shells unless requested.
