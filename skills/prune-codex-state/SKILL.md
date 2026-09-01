---
name: prune-codex-state
description: Prune stale local Codex thread state and managed artifacts while preserving chats in current Desktop history and their dependencies. Use for orphaned rollouts, generated files, temporary workspaces, and managed worktrees.
---

# Prune Codex state

Use current Desktop history as the keeplist in both account modes. Keep every listed chat, its descendants and artifacts,
and anything it depends on, regardless of account. `accounts=active` (default) also protects signed-out account data.
At the user’s explicit request, `accounts=include-signed-out` makes verified signed-out account data eligible for pruning
outside the keeplist and its dependencies. See [account scope](references/inventory.md#account-scope).

Accept optional `min-age=Nd` for a positive whole number of days, such as `$prune-codex-state min-age=7d`.
A natural-language equivalent such as “keep anything newer than a week” also works. Omitted or `min-age=off` disables
the gate. If a supplied value is invalid or ambiguous, clarify it before interpreting eligible candidates.

1. **Get the keeplist.** Call `mcp__codex_app__list_threads` in history mode with `source: "codex"`, first with
   `archived: false`, then with `archived: true`, on the host that owns the target files. Follow each `nextCursor` to
   exhaustion. Use the union of both results as the keeplist regardless of the listed chats’ creator, runtime status,
   or sidebar placement. Verify that both listings cover the active account. If that scope is unknown or either listing
   is unavailable or incomplete, report the limitation and keep the inventory read-only. Protect data from other hosts.
2. **Inventory and protect dependencies.** Read [inventory guidance](references/inventory.md) for storage locations,
   ownership checks, and the bounded rollout helper. Resolve the transitive closure of kept-thread dependencies before
   classifying candidates. Preserve active writers and live automation targets. Skip unresolved references. Records
   with missing owner links qualify only under the documented metadata-stub rule. Unknown formats or schemas block pruning
   wherever they could hide a dependency. If enabled, apply the [minimum age](references/inventory.md#minimum-age)
   when selecting candidates and preserve any dependencies of newly protected entities.
3. **Present a dry run.** Prepare the [runner manifest](references/apply.md#prepare-the-manifest) before asking for
   approval. Give an [account breakdown](references/inventory.md#account-breakdown) in the review summary. Include
   exact candidates, counts, estimated space, and exclusions. Flag anything the runner will defer until shutdown.
   Get approval for that manifest. Changes discovered later can narrow the set. Additions require approval.
4. **Apply the approved manifest.** Refresh both MCP lists and recheck the approved candidates. Run the bundled
   `scripts/prune.py apply` while Codex is open. Use the same private run directory on retry. If the runner reports
   deferred work, hand off its `finish-desktop-state` command for the user to run from a terminal after quitting Codex.
   See [apply and verification](references/apply.md#apply-and-resume) for commands, scope, and recovery.
5. **Verify.** Recheck both MCP lists, retained artifacts, database health, and absence of the approved stale data.
   If the run required shutdown, verify again after reopening. Report removals, skips, and the backup path.
