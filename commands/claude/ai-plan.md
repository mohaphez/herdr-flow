---
description: Plan a Herdr Flow task with the globally selected coordinator
---

Original request: $ARGUMENTS

If no request was supplied, ask the user for the task before creating anything.

Start only inside a Herdr-managed project pane. Read `~/.agents/skills/herdr-flow/references/planning.md` and run `herdr-flow defaults`; this global default, not the current Claude session, decides who coordinates a **new** task. If an existing task already represents this request, inspect its status and do not create a duplicate.

1. Derive a meaningful short title with an action verb so the branch type can be inferred (e.g. “Optimize ...” → `refactor/`); if the requested type differs, add `--branch-type <type>` (one of feat, fix, refactor, docs, test, chore). Run `herdr-flow create --title "<title>"` exactly once (include `--plan-only` when the user asks for planning without implementation). Add worker/reviewer/fixer or coordinator flags only when the user expressly chooses them. Replace the returned `brief.md` placeholder with the full original request using file editing, never untrusted shell interpolation. Keep credentials and `.env` values out of workflow files.
2. If the selected coordinator is **unpinned Claude**, this Claude session is the coordinator: investigate the project following the shared planning contract, complete `handoff.md` and record decisions. Ask about material unknowns. Do not write production code. If this is a `--plan-only` task, stop; otherwise run `herdr-flow run --task <id>` once from this pane.
3. If the selected coordinator is **Codex, OpenCode, or pinned Claude**, this Claude session is only the launcher. Run `herdr-flow start-coordinator --task <id>` once; the separate pinned session reads the brief and shared contract and owns planning, user questions, and final review. Report its pane via `herdr-flow status --task <id>`; do not plan, run, implement, or send a second prompt here.
4. Report task ID, brief and handoff paths, selected coordinator and pane, and whether automation started. If delivery is uncertain, inspect the existing session instead of recreating the task or resending.
