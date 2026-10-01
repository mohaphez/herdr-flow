---
description: Finish implementation and mark the current Herdr Flow task ready for review
---

Identify the current task from `.herdr-flow-task.json` or run `herdr-flow status`.

1. Complete the requested implementation; do not merely describe it.
2. Run all relevant tests, lint, type, and build checks.
3. Update `.ai/workflow/<task-id>/implementation.md` and set `## Status` to `READY`.
4. Record durable decisions in `decisions.md` without secrets.
5. Run `herdr-flow finish --task <task-id>` yourself after writing READY. In automated mode this command immediately starts independent review; an idle event is the fallback if your command is not run. Do not ask the user to run another phase command.
6. Remain alive in this same Herdr session while review runs.
