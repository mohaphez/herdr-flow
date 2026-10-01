---
description: Start a completed Herdr Flow handoff from its recorded coordinator
argument-hint: <task-id>
---

Task: $ARGUMENTS

Read `herdr-flow status --task <id>` and the task's completed `handoff.md`. Run `herdr-flow run --task <id>` exactly once only if this is the original recorded coordinator session inside Herdr. Otherwise focus/report that session rather than claiming ownership. If the task already runs, report status instead of resending any prompt.
