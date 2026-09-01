# Inventory and ownership

Determine the target host and Codex home before reading any stores. Resolve `${CODEX_HOME:-$HOME/.codex}` on that host.
Use read-only SQLite connections that include committed WAL state. Inspect table definitions before building any row
queries. Select IDs, paths, relationship fields, and counts. Keep transcript bodies, cached item payloads, and secrets
out of the model context and review report.

## Account scope

Confirm which account and host the MCP listings cover using trusted app metadata. If the listings omit account IDs,
corroborate the active Desktop account scope with creator metadata on the calling or other listed local Codex chats.
A shared Codex home can hold data from multiple accounts, including accounts that have signed out.

The union of the current unarchived and archived MCP listings defines the live threads. Keep every listed chat, its
descendants and artifacts, and its transitive dependencies in both modes, regardless of the chat’s creator. Resolve
dependencies across accounts before selecting candidates. A chat that exists only in local metadata or rollout files
can be eligible if its account is in scope and it has no link to a kept chat.

| Local Codex data | `accounts=active` | `accounts=include-signed-out` |
|---|---|---|
| A listed chat, data derived from it, or anything it depends on | Keep | Keep |
| Active-account data with no link to a kept chat | Eligible | Eligible |
| Verified signed-out account data with no link to a kept chat | Keep | Eligible |

Establish each candidate’s ownership from its metadata or verified lineage. For signed-out data, confirm sign-in status
from app metadata or the user’s explicit confirmation for the target host. Confirmation can identify the signed-out
accounts or establish the full set of signed-in accounts. Hold data from other signed-in accounts, data they still use,
and candidates whose ownership or applicable sign-in status is unresolved. Record account evidence and list any eligible
signed-out chats, including local archives, in the manifest.

Apply the same runtime, age, and store-specific checks in both modes. A live writer or automation can block removal.
Apply needs approval for the concrete manifest.

### Account breakdown

Record the selected account mode and scope evidence in the private manifest. Give each identified account a stable
reference within the manifest. Show a verified email or recognizable label when app metadata ties it to the same account
and user identifiers used for attribution. A cached profile can provide the label. Verify sign-in status independently.
Use a distinct fallback label such as `Other account 1` when the identity is unavailable. Keep raw account identifiers
out of the review summary.

- For each account, show its label, sign-in status, and candidate and held counts by entity type. Count unarchived and
  archived chats separately, and give estimated file bytes where available. Include accounts whose data was held in
  the default mode if the inventory found them. Summarize why items were held.
- Give each exact candidate an account reference and the evidence for ownership, directly or through its verified
  owning thread. Keep shared and unattributed items in separate groups. List known accounts involved in a shared item
  and count that item only once.
- Use the same labels and counts in the review summary so the user can match it to the detailed manifest.

## Thread graph

- Index plain and compressed rollouts under `sessions/` and `archived_sessions/`. Run
  `python3 scripts/rollouts.py inventory --codex-home PATH` from the skill directory and redirect the JSON to a private
  file. Read aggregate counts and selected rows from the report. The helper reads a bounded first metadata record and
  reports anything it cannot classify. Compressed files require Python with `compression.zstd` or the `zstd` CLI.
- Distinguish the stable thread ID from the physical rollout ID. A reverted thread can have multiple physical rollouts.
  `history_base.thread_id` in a header identifies a physical rollout, sometimes with a different ID from its owner.
  Preserve all files belonging to kept threads, including any existing plain/compressed siblings, and recursively
  resolve history bases. Check fork parents, session roots, spawn edges, attached threads, and any other recorded
  dependencies. Preserve linked descendants when they belong to a kept session. Keep conflicting references protected
  until resolved.
- Corroborate ownership with the state DB and known Desktop references. Use local catalog rows, archive flags, and
  rollouts to identify ownership and dependencies. If local evidence calls the MCP listings’ coverage into question,
  resolve the discrepancy before pruning. Known clutter such as `.DS_Store` can be ignored when tracing dependencies.
- Trace each candidate to its owning thread or session. A Guardian review or other descendant is eligible when every
  lineage ends at a verified in-scope owner outside the keeplist and no kept thread depends on it. A true
  `codex-writing-block-deleted-thread-v1:<id>` marker in the matching Desktop owner scope can corroborate a deleted
  owner’s identity. Preserve the marker. Skip conflicting or out-of-scope owner links. Evaluate missing owner links
  under the metadata-stub rule.

### Metadata stubs with missing owners

A recognized internal row, such as a Guardian review with no parent ID, can enter the normal manifest as an empty
metadata stub if all of these checks pass:

- The row is absent from both complete MCP listings. Evidence places it under an account eligible in the selected
  mode on the target host, and runtime and automation metadata show it is unused.
- The rollout inventory and applicable stores show no surviving content for the row. Check plain and compressed
  rollouts, cached turns and items, review feedback, and any other thread-owned payloads or artifacts.
- A reverse-reference check finds no dependency from any kept chat or its protected artifacts. Check known structured
  links in the state and history stores, retained rollouts, and relevant Desktop metadata. Extract only matching IDs
  and counts. Keep transcript and payload text out of the report.

Record the checks in the manifest and repeat them at apply time. If surviving content exists or a relevant check
is incomplete, skip the row and report the reason.

## Minimum age

When enabled, compute one UTC cutoff at the start of the inventory by subtracting `N` periods of 24 hours. An entity
meets the age gate when its newest reliable timestamp is at or before that cutoff. Evaluate age after collecting the
full ownership graph, including newer rollouts and artifacts.

- For threads, use the newest recorded creation, update, or activity time from the relevant stores. For rollouts,
  include the rollout timestamp and file modification time. For managed artifacts, use recorded creation or activity
  and the file modification time where applicable. For recursive deletion, check the directory and its contents.
- If an age-bearing entity has no reliable timestamp, preserve it and report why. Preserve younger entities and
  anything they depend on. If deletions must happen together to keep state consistent, the youngest member governs
  the group.
- Records with no meaningful standalone age follow the disposition of their owning entity when there is one.
  Age does not independently restrict an eligible reference to a missing owner or an empty ancestor removed after
  approved rollout deletion. Avoid inferring per-record age from the modification time of a shared database or JSON file.

Record the cutoff, timestamp evidence for age-gated candidates, and counts skipped for youth or unknown age in the
manifest. With the gate off, use the ordinary ownership and dependency rules.

## Candidate stores

| Store | Ownership and scope |
|---|---|
| Rollouts | Bounded `session_meta` header plus filename, state rows, and dependency graph. Preserve unrecognized formats for review. |
| Visualizations and generated images | Check `visualizations/` and `generated_images/` for verified thread-scoped directories or records. Preserve shared assets and outputs saved outside managed roots. |
| Shell snapshots and other runtime artifacts | Include snapshots with explicit stale owner IDs. Check other caches only when their schema proves thread ownership. Leave live or unattributed entries alone. |
| Managed Git worktrees | Resolve `[desktop].git-worktree-root` in the target config, defaulting to `CODEX_HOME/worktrees`. Require Git registration, verified Codex ownership such as the worktree Git dir's `codex-thread.json`, and no retained chat reference. Report repository, path, owner, and HEAD. Preserve locked, dirty, untracked, ignored, or submodule changes, commits with no other durable ref, and unbound or ambiguous checkouts. |
| Projectless workspaces | Resolve the effective `[desktop].projectlessWorkspaceRoot` from the Codex home's `config.toml`, including a managed `application.storage.projectless_workspace_root` override when applicable. Default to `~/Documents/Codex`. Require evidence that Codex created the specific directory and that every owner is eligible. Cross-check Desktop workspace/output hints and thread or rollout metadata. A path under the root, a date-based name, or a thread's CWD alone is insufficient. |
| SQLite | Discover installed state, history, Desktop catalog, queue, goals, diagnostics, summary, and memory stores. Include positively attributed rows such as cached turns and items, per-thread summaries and memory, receipts, relationship and attachment records, and queue or run records. Keep automation definitions and any targets they still use. Follow the installed schema's foreign keys and cascades. |
| Session index and Desktop JSON | Prune known thread-owned index entries, sidebar membership, tabs, and workspace hints for approved IDs. Use typed keys and values. Shared settings, live bindings, and unrelated IDs stay intact. |

Resolve managed directories canonically and verify containment for every filesystem candidate. Check for retained
references across the entire tree of each candidate directory and across all kept threads and dependencies. Skip
symlinks, nested repositories or worktrees, and shared paths until their ownership is clear. Never delete user-chosen workspaces.
Separate each eligible projectless workspace in the manifest and describe outputs, prior backups, and size. Treat an
attachment record and its target as separate ownership decisions. Preserve consolidated memory, shared caches,
credentials, skills, plugins, locks, sockets, and migration cursors by default.
