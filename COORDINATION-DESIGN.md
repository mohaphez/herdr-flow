# Multi-harness coordination design and rollout

## Contract

- Each task pins coordinator kind/model/effort at creation. Claude (unselected default) remains attachable to a human's existing Claude pane. Pinned Claude, Codex, OpenCode, and Pi coordinators use a task-owned Herdr pane and persistent session.
- One coordinator plans and performs the final evidence-based review; separate worker, independent reviewer, and optional repair sessions implement/review/fix. Using one harness or even one model in different roles does not share a session. Human authorization remains required for commits and merges.
- Reports and state belong to a project/task directory. The coordinator's planning prompt creates a handoff and calls `run` from its own pane; final review returns to that original pane/session. No prompt is repeated after uncertain delivery.
- The coordinator selection and ownership cannot change after its session starts. Missing/blocked coordinators demand inspection and human recovery, not automatic replacement.

## Concurrency and recovery

- State transitions take a per-task advisory file lock. The entire `advance` driver uses a separate per-task drive lock so concurrent settled events and completion commands cannot both launch the next phase. The registry is separately locked, keyed by absolute state path, and event delivery can match multiple tasks sharing an external Claude pane.
- Agent names include a stable source-root digest to distinguish identical task IDs in different projects. All Herdr mutations require `HERDR_ENV=1`; no outside-pane live operations. Existing task records retain their recorded names and legacy registry entries are migrated lazily.
- A managed coordinator owns the initial worktree pane; implementation uses another tab. Cleanup includes its pane and waits until it is idle, requires both PASS gates, a clean worktree and a verified human merge, and retains the task branch and records.

## Acceptance gates

1. Unit tests: Claude default compatibility; pinned Codex/OpenCode/Pi coordinator model and thinking flags, original-session ownership, distinct worker pane; identical task IDs in separate repositories; simultaneous `advance` calls submit one review prompt; prior repair/review/finalization safeguards.
2. Local checks: Python compile, unit suite, `herdr-flow doctor`, Herdr plugin configuration/installation check.
3. Live acceptance **inside Herdr**, in a disposable repo: test Codex, OpenCode, and Pi as coordinators, planning → worker → reviewer FAIL → repair → PASS → coordinator final FAIL/PASS; start two projects with the same task ID at once; induce blocked and lost sessions; verify no cross-task messages or duplicate prompt; merge manually and finalize a clean task only after verification. This gate is not satisfied by unit tests.

## Harness entry points and global coordinator default

- The private full plugin config (copied from the portable `config.json` template at install) provides the fallback; an untracked local `defaults.json` supplies optional per-role overrides. Explicit task flags take precedence, and new tasks pin their kind/model/variant in `state.json`. `.ai/workflow/config.json` is a project snapshot, **not** a coordinator override. The shipped default remains unpinned Claude; worker/reviewer models must be selected in the private config before creating tasks.
- Claude/OpenCode `/ai-plan`, Codex `$herdr-flow-plan` (plus deprecated `/prompts:ai-plan` compatibility), and Pi `/skill:herdr-flow-plan` act as launchers. A non-Claude/pinned default gets its own session regardless of the initiating harness; the launcher cannot claim that session. The complete original non-secret request is stored in `brief.md`, and a shared planning contract guides the actual coordinator. A plan-only task records `planning.auto_start=false` and cannot automatically start implementation.
- Neither skills nor slash prompts are an authority for session ownership: `run_task` checks the recorded kind, pane, and session. Live acceptance must confirm models obey the instructions, and uncertain prompt delivery never justifies replay.

## Worktree instruction snapshots

- Copy only ignored root `AGENTS.md` and `CLAUDE.md` when missing from the task checkout; never mirror `.agents/`, `.claude/`, environment files, or unowned files. Reject credentials, symlinks, oversized instructions, and non-ignored untracked output. Write an owned `.herdr-flow-context.md` explaining the main-checkout/worktree distinction.
- Hash and record snapshots per task. Explicitly review and refresh after main-checkout drift, only when agents are idle. Block when a worktree snapshot was modified; never overwrite it or remove it during post-merge cleanup. Worktree-local Docker/Compose mounts, databases, and service ports require validation or user approval, not blind command replay.
- The standalone installer links the checkout only from a Herdr-managed pane and refuses to replace another active installation; no external process should control live Herdr agents/panes to deploy or validate this change. Live acceptance from a managed pane remains outstanding.

## Limitations / next steps

- The current rollout permits Codex/OpenCode/Pi coordination and OpenCode/Codex/Pi worker/reviewer/fixer roles; Pi live acceptance is still outstanding. Supporting Claude as a *worker or independent reviewer* is a separate policy and prompt-contract change, not implied by coordinator selection.
- A crash between worktree creation and saving its state, or between a prompt's acceptance and persisting submitted status, intentionally halts for inspection rather than guessing. Production-hardening would reconcile Herdr's worktree/agent inventory and use durable intent records before any retry.
- Cross-process races between a human-issued manual phase command and an event driver still require a unified command-level driver transaction; do not claim full linearizability from the `advance` lock alone. Restrict live use to one coordinator per task until that gate is completed.
