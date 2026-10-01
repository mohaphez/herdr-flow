---
description: Route a failed review to the persisted ordinary or escalation fixer
---

Coordinate repair for Herdr Flow task `$ARGUMENTS` (omit the task ID to use the current task).

1. Parse an optional repair selection from the request. A direct `--kind`, `--model`, and `--variant` selection defines a dedicated fixer only before any repair has started. `--escalation-kind`, `--escalation-model`, `--escalation-variant`, and `--escalate-after` configure the later escalation tier before that session starts.
2. Run `herdr-flow status --task <task-id>` and require `REVIEW_FAILED` or `FINAL_REVIEW_FAILED`. Report cumulative failures, fix attempts, base repair role, escalation threshold, and whether escalation is active.
3. Run `herdr-flow fix --task <task-id>` with only the explicit options requested by the user. With no options, trust the persisted policy. Without a dedicated fixer or enabled escalation policy, repairs reuse the original implementation worker. If escalation is configured, follow its persisted failure threshold and model.
4. Never terminate, overwrite, or silently replace a cheaper live worker/fixer. The escalation session is additional and remains persistent for every later repair.
5. If the selected repair agent is blocked, focus that exact session and let the user answer it. Do not answer high-impact questions on the user's behalf.
6. Report the active repair role, logical agent name, pane, kind, exact model, effort/variant, cumulative failure count, and current phase.
