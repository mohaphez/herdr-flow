---
description: Create a Herdr Flow task and route planning to the global coordinator
---

Original request: $ARGUMENTS

If no request was supplied, ask the user for the task before creating anything.

You are the **launcher**, not necessarily the coordinator. Work only from a Herdr-managed pane (`HERDR_ENV=1`). Read `~/.agents/skills/herdr-flow/references/planning.md` for the shared planning contract and run `herdr-flow defaults` to see the global coordinator kind/model. Do not infer the default from your current OpenCode session.

Derive a meaningful short title. If this request already has a task, report its status; never create a duplicate. Otherwise run `herdr-flow create --title "<title>"` exactly once (with `--plan-only` if the user asks for planning without implementation) (add role flags only when the user explicitly requested them). Replace the returned task's `brief.md` placeholder with the complete original request using file editing, not shell interpolation; omit secrets. For **any** default selected from an OpenCode launcher, run `herdr-flow start-coordinator --task <id>` exactly once. The task-owned coordinator receives the brief and shared planning contract; do not plan or implement in this launcher. Report the task ID, handoff path, selected coordinator, and pane from `herdr-flow status --task <id>`. The `--plan-only` setting instructs the coordinator to leave automation off. If delivery is uncertain, inspect its existing session rather than resending.
