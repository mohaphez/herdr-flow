---
name: herdr-flow
description: Coordinate persistent multi-agent development tasks through the local Herdr Flow plugin. Use when planning, implementing, reviewing, fixing, resuming, or checking a `.ai/workflow` task whose worker and reviewer sessions must be reused.
compatibility: Requires Herdr 0.9.1+, OpenCode, Claude Code, Git, and the local `herdr-flow` command.
metadata:
  owner: local
  workflow: persistent-multi-agent
---

# Herdr Flow

Herdr Flow keeps task state in `.ai/workflow/<task-id>/` and keeps live worker/reviewer processes visible in Herdr. New tasks preserve the original request in `brief.md`; managed coordinators will not start while it is a placeholder. The shared planning/final-review contracts are in `references/planning.md` and `references/final-review.md`.

## Invariants

1. One task owns one persistent implementation worker.
2. Ordinary repair failures reuse that worker unless the task defines a dedicated fixer. Automatic escalation creates/reuses one separate escalation fixer after the configured cumulative failure threshold; it never destroys the cheaper session.
3. The independent reviewer is a separate persistent OpenCode or Codex session. Choose an installed model in the private config before creating tasks; Kimi, if selected, is an OpenCode model, never Kimi Code CLI.
4. Files are the record; prompts are short references.
5. A blocked agent waits for the user in the same pane.
6. A replacement session is allowed only when the prior session is genuinely absent.
7. Never auto-merge, force-push, delete branches, or discard unrelated work. Cleanup is allowed only after both gates PASS, a verified merge into the source checkout, and a clean worktree with no unowned files/panes.
8. Never put credentials or `.env` contents in workflow files.
9. Read the task worktree's `.herdr-flow-context.md` plus applicable `AGENTS.md`/`CLAUDE.md` before working. Copied ignored root instructions are snapshots of the main checkout, but Docker commands, paths, and service assumptions may not apply in a worktree. Verify before running; ask rather than touching the main checkout or shared runtime blindly. If instructions drift, inspect both versions and use `refresh-instructions --yes` from Herdr only when agents are idle; never silently overwrite an edited snapshot.

## Coordinator lifecycle

```text
herdr-flow create --title "Readable task title"
# Default Claude: fill handoff.md in its Herdr pane, then start once:
herdr-flow run --task task-001
# The controller advances from READY/PASS/FAIL reports when agents settle.
# It prompts the same coordinator session for final review.
# After the human commits and merges, from a Herdr pane:
herdr-flow finalize --task task-001
```

The branch uses the title plus task id (e.g. `ai/readable-task-title-task-001`). Automation does not invent decisions, bypass blocked sessions, repeat uncertain prompts, or perform Git commits/merges. Its event-driven cleanup also checks for a verified merge; `finalize` is the deterministic fallback when Git itself emitted no Herdr event.

Run `herdr-flow defaults` to inspect the single global coordinator default (private Herdr plugin config, initialized from the repository template). New tasks inherit it without coordinator flags; explicit flags win and existing task selections never change. From Claude/OpenCode use `/ai-plan <request>`; from Codex use `$herdr-flow-plan <request>` or compatibility `/prompts:ai-plan <request>`. A launcher from a different harness starts a dedicated coordinator and does not become its owner. Coordinator, worker, dedicated fixer, escalation fixer, and reviewer kind/model/variant may be selected per task. For a Codex or OpenCode coordinator, create with `--coordinator-kind codex|opencode --coordinator-model <exact-id> [--coordinator-variant <effort>]` and run `herdr-flow start-coordinator --task <id>` inside Herdr. It launches a task-owned coordinator to plan and call `herdr-flow run` itself. The coordinator must be a separate session/pane from the worker, even if using the same harness. An unpinned Claude coordinator can still use the existing attached pane. Never replace a live coordinator or blindly resend its prompt. Use exact model IDs and configure a role only before its persistent session starts. Omitting a dedicated fixer reuses the worker for ordinary repairs. When optional escalation is enabled, failures beyond the configured threshold route to one persistent escalation fixer; subsequent repairs reuse that session.

Use `herdr-flow status`, `focus`, `inspect`, and `resume` for human interaction and recovery. Never resend a possibly delivered prompt without first using `inspect`; `resend --yes` is deliberately guarded.
