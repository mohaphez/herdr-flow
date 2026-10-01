---
description: Start the implementation handoffs from the original Herdr Flow coordinator
---

Task: $ARGUMENTS

Run `herdr-flow status --task <id>` and read the completed `handoff.md`. Only the **original recorded coordinator** inside its Herdr pane may run `herdr-flow run --task <id>` exactly once. If a separate Codex/OpenCode/pinned Claude session owns this task, report or focus it; do not claim its ownership from this Claude launcher. If already running, report its status instead of repeating the command. Blocked/missing sessions and uncertain prompts require inspection, not a duplicate prompt. After DONE, leave commit/merge human-controlled and retain task files until verified merged cleanup.
