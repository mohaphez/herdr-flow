---
description: Start a completed Herdr Flow plan from its original coordinator session
---

Task: $ARGUMENTS

Check `herdr-flow status --task <id>` and read the completed `handoff.md`. Only the **recorded coordinator** in its original Herdr pane may run `herdr-flow run --task <id>` exactly once. If this OpenCode session is merely the launcher, report/focus the recorded coordinator instead of trying to take ownership, duplicate the session, or issue `run` here. If a prompt is submitted or uncertain, inspect the live session; do not resend blindly.
