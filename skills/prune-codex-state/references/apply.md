# Apply and verify

The runner uses Python 3.11+ on local macOS and Linux Codex homes. It can remove rollouts, session-index entries, and empty
projectless workspaces while Codex runs. It applies the supported raw SQLite edits and Desktop JSON edits after
shutdown. Logs can be buffered and history projections can be rebuilt while Codex runs, so raw SQLite transactions
alone cannot guarantee those rows stay removed.

## Prepare the manifest

Keep the inventory, account breakdown, and ownership evidence in the private review bundle. Give each executable
candidate a verified owner ID. Record worktrees, nonempty workspaces, backup bundles, and other unhandled candidate
types as excluded from the executable manifest. Use an absolute, canonical local path for each approved path.

The executable manifest uses the following fields. Include every field and use an empty array for an unused section.

| Field | Contents |
|---|---|
| `version`, `host_id` | `1` and `local` |
| `codex_home`, `account_mode` | Canonical Codex home and the selected account mode |
| `desktop_owner_sha256` | JSON value hash of `electron-renderer-content-owner` in the primary Desktop state file |
| `rollouts` | Entries with `path`, `thread_id`, and SHA-256 of the original file bytes as `sha256` |
| `workspaces` | Entries with `path`, `thread_ids` for all owners, and `directories` containing the root and all approved nested empty directories |
| `index` | Entries with `thread_id`, `sha256` of the original index line including its newline, and occurrence `count` |
| `databases` | Output of `fingerprint-databases` |
| `desktop` | Entries with `thread_id`, a supported JSON key path as `keys`, and the expected JSON value hash as `sha256` |

Use lowercase UUIDs and hex digests. JSON value hashes use SHA-256 of compact, sorted-key, ASCII-escaped JSON
(`desktop_state.value_hash`). A projectless thread list selector uses the observed list index in `keys` and hashes
the thread ID value. The patcher resolves the ID in the latest list at apply time.

For SQLite, write a private selectors file with entries of the form
`{"name":"state_5.sqlite","rows":[{"table":"threads","thread_id":"<UUID>"}]}` in an array. Use only the reviewed
table and owner pairs. The helper returns exact row counts, row-set digests, and schema fingerprints.

```sh
python3 scripts/prune.py fingerprint-databases --codex-home "$codex_home" --selectors "$selectors"
```

The supported tables are `state_5.sqlite` `threads`, `thread_history_1.sqlite` `thread_turns`,
`thread_items`, and `thread_history_projection_state`, `logs_2.sqlite` `logs`, and `queue_1.sqlite`
`queued_thread_revisions`. List related rows outside this set as exclusions. Exclude an affected thread when
removing its listed rows would also require changing those relationships. Review the generated counts before
approval. Present the SHA-256 of the exact manifest bytes alongside the review. A change to the approved manifest
requires a new review, while a fresh preflight can narrow its candidates.

## Apply and resume

Refresh both complete MCP listings and verify the active account, host, and applicable sign-out status. Recheck
ownership, the full dependency graph, writers, schemas, and any age cutoff. Write a private preflight JSON file
with these fields.

- `manifest_sha256`, `host_id`, and `account_mode` bind the recheck to the reviewed manifest.
- `observed_at` is the timezone-aware ISO timestamp of the refresh.
- `unarchived_ids` and `archived_ids` contain the full results of the two MCP listings.
- `protected_ids` contains additional protected thread IDs from the dependency and runtime checks.
- `eligible_ids` contains only approved thread IDs whose eligibility still holds.

The runner consumes this preflight within five minutes. Use the full skill path from an external terminal or run
these commands from the skill directory. Use an absolute path for the private run directory outside the Codex home
and approved workspaces. Keep it for backups and recovery.

```sh
python3 scripts/prune.py apply --manifest "$manifest" --approved-sha256 "$approved_sha256" \
  --preflight "$preflight" --run-dir "$run_dir"
```

The live phase acquires Codex's writer and index locks, checks rollout references again, and saves originals
before mutation. A busy writer, unexpected reference, changed fingerprint, or unapproved workspace contents
stops the affected run. Refresh preflight and reuse the same manifest digest and run directory to resume. Backup
records distinguish verified completed operations from artifacts that disappeared before the runner could back them up.

If database or Desktop edits remain, fully quit Codex and stop its other local writers, then run the following
command. Refresh the live preflight again before quitting if account or thread state has changed since apply.

```sh
python3 scripts/prune.py finish-desktop-state --manifest "$manifest" --approved-sha256 "$approved_sha256" \
  --run-dir "$run_dir"
```

This command checks that Codex has exited, rechecks the saved scope and runtime references, and resumes any pending
file work. It uses SQLite's backup API, applies only fingerprint-matched row sets, and checks for unexpected
effects before committing. The Desktop patcher loads both the primary and any existing fallback from their latest
on-disk contents, verifies the approved edits, backs up each original, and atomically replaces each changed file.
It preserves unrelated changes flushed at quit. Rerun the same command after a partial failure.

## Verify and recover

After apply and after any relaunch, compare both MCP sets with preflight and account for new chats. Check retained
files against their digests where applicable. Codex may recompress a retained plain rollout, so compare decompressed
bytes if its storage format changed. Check SQLite integrity and foreign keys, confirm approved rows and files have
not returned, and check Desktop JSON edits in both copies.

Keep the run directory until verification. If rollback is needed, quit Codex and restore matching database
snapshots and artifacts together. Keep unrelated WAL files out of a database restore. Merge backed-up index or
Desktop values into newer state if unrelated changes have been made since the backups.
