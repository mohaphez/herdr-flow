---
name: herdr-flow-plan
description: Start a Herdr Flow task from Codex and route planning to the global default coordinator. Use when asked to plan, coordinate, or start a multi-agent implementation using Herdr Flow; invoke explicitly with $herdr-flow-plan followed by the request. Never implement from the launcher when another coordinator owns the task.
compatibility: Requires Herdr 0.9.1+, herdr-flow CLI, Git, and a Herdr-managed pane.
---

# Start Herdr Flow planning

Use the user's request following `$herdr-flow-plan` as the original task brief. If no request was provided, ask the user before creating anything. Read the shared coordinator contract at `~/.agents/skills/herdr-flow/references/planning.md`.

1. Confirm `HERDR_ENV=1`. Call `herdr-flow defaults` to inspect the *global* coordinator default; do not assume this Codex session owns coordination. Derive a meaningful short task title from the request. If an existing task already represents this request, inspect its status instead of creating another.
2. Run `herdr-flow create --title "<meaningful title>"` **once** (include `--plan-only` if the user requests planning without implementation). Never supply `--coordinator-*` unless the user explicitly requests a per-task override. Write the complete original request to the new task's `brief.md`, replacing the placeholder, without secrets. Use a file-editing tool rather than interpolating untrusted user text into a shell command. Keep the task ID returned by create.
3. If the selected default is an unpinned Claude coordinator and you are not Claude, call `herdr-flow start-coordinator --task <id>` from this Herdr pane. If the default is Codex or OpenCode (or pinned Claude), call `start-coordinator` as well. The spawned pinned session, not this launcher, owns planning. Report its pane from `herdr-flow status --task <id>`, direct the user there for questions and do not send a second planning prompt.
4. The task-owned coordinator reads `brief.md` and the shared contract, writes `handoff.md`, and runs `herdr-flow run` once only when the user requested execution. A task created with `--plan-only` leaves automation off. Do not implement code as the launcher.
5. If creation, prompt delivery, or start is uncertain, inspect state and the live pane; never repeat create or resend a possibly delivered prompt.
