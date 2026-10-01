---
description: Resume a paused Herdr Flow task without duplicating live sessions or prompts
---

Resume task `$ARGUMENTS` (omit the argument to use the current task).

1. Run `herdr-flow status --task <task-id>`.
2. Run `herdr-flow resume --task <task-id>`.
3. Reuse every live recorded session.
4. Create a recovery session only when the required session is genuinely absent.
5. If a prompt is recorded as submitted or uncertain, inspect the live agent with `herdr-flow inspect` before considering `herdr-flow resend --yes`.
6. If phase is `WAITING_FOR_USER`, focus the same blocked session rather than replacing it.
