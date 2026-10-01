---
description: Perform Claude's final review after the independent reviewer passes
---

Perform final review for task `$ARGUMENTS` (omit the argument to use the current task).

1. Run `herdr-flow final-start --task <task-id>`.
2. Read `state.json`, `handoff.md`, `implementation.md`, `review.md`, and `decisions.md` from the task directory.
3. Act as the final release gate. Independently inspect the current worktree and complete relevant diff. Verify every handoff requirement, acceptance criterion, Definition of Done item, test claim, prior reviewer finding, regression risk, security concern, performance/memory consideration, repository rule, and architectural constraint with concrete evidence. Do not rely only on reports. Mark unverified critical checks and FAIL when they block confidence.
4. Write `final-review.md` with an explicit `## Status` of PASS or FAIL, requirement and handoff coverage, evidence, remaining gaps or unverified assumptions, and an explicit statement about whether the changes are ready to commit and merge. PASS only when no unresolved material issue remains.
5. Run `herdr-flow final-result --task <task-id> --status PASS|FAIL`.
6. On FAIL, use `herdr-flow fix --task <task-id>`. Final-review failures count toward the same cumulative escalation threshold as independent-review failures, and the controller selects/reuses the correct repair session. Follow every repair with independent review again.
7. Do not merge or delete the worktree.
