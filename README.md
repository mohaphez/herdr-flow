# Herdr Flow

File-backed, persistent multi-agent development workflow for [Herdr](https://herdr.dev). A Claude Code, Codex, OpenCode, or Pi coordinator plans a task and performs its final release review. Independent OpenCode/Codex/Pi sessions implement, review, and repair in a task-specific Git worktree. **No automatic commit or merge.** Worktree cleanup requires a verified merge, a clean checkout, and ownership checks.

> **v0.9.0 prerelease:** Pi and role-default switching are covered by automated tests, but full live four-harness acceptance in `ACCEPTANCE.md` remains to be performed inside Herdr. Earlier live first-run Codex adoption/prompt handoff was observed; Pi live acceptance has **not** been claimed. The marketplace is an **unreviewed community index**, not a security or quality endorsement. Do not assume race-free operation or unattended production readiness from unit tests alone.

## Workflow at a glance

```mermaid
flowchart TD
    A["Inside Herdr: /ai-plan, $herdr-flow-plan, or /skill:herdr-flow-plan"] --> B["Task + original brief.md"]
    B --> C["Selected coordinator: investigate + handoff.md"]
    C -->|Plan only| P["Stop: await user approval to run"]
    C -->|Start implementation| W["Persistent worker: implement + implementation.md"]
    W --> R["Independent reviewer: review.md PASS / FAIL"]
    R -->|FAIL| F["Repair: persistent worker or configured fixer"]
    F --> R
    R -->|PASS| G["Same coordinator: final-review.md PASS / FAIL"]
    G -->|FAIL| F
    G -->|PASS| D["DONE: no automatic commit or merge"]
    D --> H["Human commits and merges task branch"]
    H -->|Verified merge + clean, owned worktree| X["Guarded worktree cleanup / finalize"]
    W -.->|Blocked or uncertain prompt| U["Wait: user inspects the same session"]
    R -.->|Blocked or uncertain prompt| U
    G -.->|Blocked or uncertain prompt| U
```

**Example — add OAuth login**

1. From a Herdr-managed Claude or OpenCode pane, enter `/ai-plan Add OAuth login` (Codex: `$herdr-flow-plan Add OAuth login`; Pi: `/skill:herdr-flow-plan Add OAuth login`). The launcher creates one task and saves the request in `brief.md`.
2. The **globally selected coordinator** investigates, asks about unresolved decisions, and writes `handoff.md`. If another harness was selected, it works in its own pane. For a plan-only request, it stops here; otherwise it calls `herdr-flow run` once.
3. A persistent worker implements in the task worktree. An independent reviewer checks the result; FAIL routes findings to the worker or configured fixer, followed by another review.
4. After reviewer PASS, the **original coordinator** performs final review. A final FAIL enters the repair/review loop; PASS moves the task to DONE.
5. **You** commit and merge. Only a verified merge and clean, owned worktree permit guarded cleanup. The branch and task records remain.

A blocked agent stays in its existing pane. An uncertain prompt is **never** automatically resent. For an externally attached Claude coordinator, final review targets the recorded pane only after matching its recorded native session and agent kind; a lost or changed display name alone does not transfer ownership. A changed session stops for inspection.

## Requirements

Linux, Python 3.10+ (tested with 3.14), Git, Herdr 0.9.1+, `jq`, and the CLIs for the harnesses you select (Claude Code, OpenCode, Codex, or Pi). Model IDs must be available in each selected harness's installed catalog. The plugin uses `hessam.herdr-flow` as its stable Herdr identifier; it does not contain credentials.

## Install from the Herdr marketplace (recommended for new users)

The marketplace lists public repositories with the `herdr-plugin` GitHub topic and a valid manifest on their default branch. It does not host, vet, or sandbox plugin code. Inspect the [manifest](herdr-plugin.toml), [setup script](install.sh), and source before installing. From a **Herdr-managed pane** on Linux, with the requirements above installed:

```bash
herdr plugin install mohaphez/herdr-flow --ref v0.9.0
herdr plugin action invoke hessam.herdr-flow.setup
herdr plugin log list --plugin hessam.herdr-flow --limit 5
herdr-flow doctor
```

`plugin install` clones and registers the manifest actions, events, and status pane. It **does not** add `herdr-flow` to your shell or install agent CLIs, commands, or skills. The explicit `setup` action runs this repository's [install.sh](install.sh) as your user, from Herdr: it checks existing ownership, seeds private config if missing, and links the CLI and agent entry points. It is safe to rerun for the same plugin root and refuses to replace an unrelated or stale link. Action invocation returns a log record before the command may finish; check the plugin log and that `herdr-flow` is available before running `doctor`. `doctor` reports any unset models; set them in your private config before creating tasks. No build/startup hook silently edits your home directory.

Herdr exposes the plugin without setup through `herdr plugin action list --plugin hessam.herdr-flow` and `herdr plugin pane open --plugin hessam.herdr-flow --entrypoint status`. Once setup completes and models are configured, start a task using the planning commands below or `herdr-flow create` **inside Herdr**. The [marketplace documentation](https://herdr.dev/docs/marketplace/) explains discovery: the `herdr-plugin` topic and this default-branch manifest drive indexing, not the GitHub release or tag; refreshes occur about every 30 minutes. A listing is not a Herdr review or endorsement.

Use `herdr plugin config-dir hessam.herdr-flow` to locate the private config directory. After an upgrade/reinstall, inspect existing tasks first and rerun `setup` if the managed checkout path changed; if old symlinks still point at a retired checkout, inspect their ownership and remove only verified stale links before setup. Never install over a different linked checkout or interrupt its live tasks. `--ref v0.9.0` pins this prerelease; omit `--ref` only when you deliberately want the current default-branch version. Herdr has no separate `plugin update` in v1: it reinstalls from GitHub.

### Local development or standalone checkout

Clone into a **standalone repository**, for example:

```bash
git clone https://github.com/mohaphez/herdr-flow.git ~/projects/github/herdr-flow
```

From a **Herdr-managed pane**, run `~/projects/github/herdr-flow/install.sh`. The same guarded setup script links the local repository as the Herdr plugin if necessary and links CLI, command, and skill entry points; it does not copy plugin source, overwrite another installation, or rewrite existing user-owned files. Never run it from an external terminal to manipulate live Herdr sessions. If another Herdr Flow installation is active, complete/inspect its tasks before explicitly migrating from a Herdr pane; do not change its live ownership or delete its worktrees.

Herdr Flow keeps a **private** runtime config at `~/.config/herdr/plugins/config/hessam.herdr-flow/config.json` (or use `HERDR_PLUGIN_CONFIG_DIR=/absolute/path/to/private-config-directory`). The installer creates that file with mode `0600` only when it does not already exist. The tracked `config.json` is a portable template, **not** your credential or model catalog. Set the worker and reviewer to exact models available on your machine before creating tasks; optionally enable an escalation fixer and change the default coordinator. Example (replace model IDs with real catalog IDs):

```json
{
  "agents": {
    "coordinator": {"kind": "claude", "model": null, "variant": null},
    "worker": {"kind": "opencode", "model": "YOUR_PROVIDER/YOUR_WORKER_MODEL", "variant": null},
    "reviewer": {"kind": "opencode", "model": "YOUR_PROVIDER/YOUR_REVIEWER_MODEL", "variant": null}
  },
  "repair": {
    "escalation": {
      "enabled": false,
      "after_failures": 3,
      "agent": {"kind": "codex", "model": null, "variant": null}
    }
  }
}
```

**Edit the copied full config**, not just the partial example above. `herdr-flow doctor` validates model availability and configuration. `herdr-flow defaults` prints effective selections for all roles. `herdr-flow switch` saves a selected role's model/variant in a private, untracked override for **future tasks only**. No model/API keys are committed or needed in the repository; each harness uses its own configured authentication.

### Switch defaults without editing Git

`herdr-flow switch` interactively asks for role (coordinator, worker, reviewer, escalation), harness, an installed catalog model, and a supported thinking/variant level. Pi supports exact `provider/model` IDs from `pi --list-models`; its `--thinking` levels are `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max` for thinking-capable models, or `off`/default for non-thinking models. OpenCode offers only variants advertised by its effective catalog; Codex validates levels against its model catalog. There is no automatic model substitution.

```bash
herdr-flow switch --role worker --kind pi --model 'YOUR_PROVIDER/YOUR_MODEL' --variant high
herdr-flow switch --role reviewer --kind opencode --model 'YOUR_PROVIDER/YOUR_MODEL' --variant default
herdr-flow switch --role worker --reset
herdr-flow defaults
```

Selections are written atomically with mode `0600` to `~/.local/state/herdr/plugins/hessam.herdr-flow/defaults.json` (or `HERDR_PLUGIN_STATE_DIR/defaults.json`), outside Git. Explicit per-task flags take precedence; existing tasks retain their pinned selections and live sessions are never swapped. `--reset` removes only one role override. The tracked `config.json` is the portable fallback; no private model is hard-coded in this repository. A dedicated ordinary fixer is configured per task, not in this global picker. Do not change the active plugin checkout under a running task.

### Migrating from another linked checkout

This install intentionally refuses to replace another registered plugin or command/skill symlink. From within Herdr, first confirm no live task depends on the old installation, retain its task state and worktrees, and explicitly unlink the old plugin. Copy any private config to a private file outside the old checkout, then remove **only symlinks verified to point to the old plugin** before running the standalone installer. A repository clone or external terminal must not silently take over existing panes, sessions, or workspaces.

## Command reference

Run CLI commands that inspect or control live sessions **inside a Herdr-managed pane**; `defaults` and `doctor` are read-only. The `--task` value is the ID returned by `create` (for example `task-001`). Slash commands are entered in the named agent, not in a shell.

| Command | Where / when | What it does |
| --- | --- | --- |
| `/ai-plan <request>` | Claude or OpenCode agent | Create one task, record the original request, and plan with the global coordinator. |
| `$herdr-flow-plan <request>` | Codex agent | Preferred Codex planning skill; `/prompts:ai-plan <request>` is a deprecated compatibility shortcut. |
| `/skill:herdr-flow-plan <request>` | Pi agent | Planning skill discovered via `~/.agents/skills/`; launch from a Herdr pane. |
| `/ai-run <task-id>` / `/prompts:ai-run <task-id>` | Claude/OpenCode / Codex **original coordinator** | Start implementation after a plan-only handoff; a launcher must not claim another session. |
| `herdr-flow defaults` | Shell, read-only | Show all effective role defaults and their models/variants. |
| `herdr-flow switch` | Shell, local configuration | Pick role, harness, model, and thinking/variant for future tasks; `--reset` restores repository fallback. |
| `herdr-flow doctor` | Shell, read-only | Validate tools, installed model IDs, and private configuration. |
| `herdr-flow create --title "<title>"` | Herdr pane, manual path | Create a task and empty handoff/brief; add `--plan-only` to leave implementation off. Set `--branch-type refactor` (or another allowed type) to override inference; explicit agent flags override defaults. |
| `herdr-flow start-coordinator --task <id>` | Herdr pane, after filling `brief.md` | Launch the recorded dedicated coordinator once; not needed for an attached, unpinned Claude coordinator. |
| `herdr-flow run --task <id>` | **Recorded coordinator pane only** | Begin automatic worker → reviewer → repair → final-review handoffs after the handoff is ready. |
| `herdr-flow status --task <id>` | Herdr pane | Inspect phase, agent/pane identity, review gates, instruction drift, and attention; add `--json` for structured output. |
| `herdr-flow focus --task <id> --role <role>` | Herdr pane | Focus the existing coordinator, worker, reviewer, or fixer session. |
| `herdr-flow inspect --task <id> --role <role>` | Herdr pane | Read the recorded live agent before considering recovery or a deliberate resend. |
| `herdr-flow resume --task <id>` | Herdr pane, recovery | Reuse existing sessions; recover only genuinely missing worker/reviewer sessions. |
| `herdr-flow advance --task <id>` | Herdr pane, exceptional reconciliation | Reconcile an already-written report without repeating an implementation prompt. |
| `herdr-flow adopt-startup --task <id> --pane <id> --model gpt-6-sol --variant high --yes` | Herdr pane, only after verifying an existing first-run Codex escalation pane | Adopt the already-idle named Codex session after a blocked startup; require matching worktree, pane, terminal, and latest on-screen GPT-6-Sol high label. Sends no prompt; after status inspection, advance once. |
| `herdr-flow refresh-instructions --task <id> --yes` | Herdr pane, after review, agents idle | Refresh owned worktree instruction snapshots if main-checkout instructions changed. |
| `herdr-flow reconcile-branch --task <id> --branch <name> --yes` | Herdr pane, after an unprovable rename in DONE | Explicitly acknowledge the *already checked-out* task branch after inspecting Git; changes only task metadata, never Git refs. |
| `herdr-flow finalize --task <id>` | Herdr pane, **after human merge** | Remove only a verified merged, clean, owned task worktree; keep branch and task records. |

Agents normally call `herdr-flow finish`, `review-result`, and `final-result` themselves after writing evidence-backed reports. Manual `implement`, `review`, `fix`, and `configure-agent` are available for recovery or pre-session role selection. Never use `resend --yes` before `inspect`, or use any command to bypass a blocked agent.

## Start a task

Inside a Herdr-managed project pane:

- **Claude Code / OpenCode:** `/ai-plan Add OAuth login`
- **Codex:** `$herdr-flow-plan Add OAuth login`; `/prompts:ai-plan` is a deprecated Codex custom-prompt compatibility shortcut (open a new Codex session after installing it).
- **Pi:** `/skill:herdr-flow-plan Add OAuth login` (from a Herdr-managed Pi pane; reload skills or open a new Pi session after setup).

The launcher creates one task, stores the original safe request in `brief.md`, and consults the **global** default coordinator. When unpinned Claude launches its own task, it may coordinate from that pane. A Codex/OpenCode/Pi (or pinned Claude) coordinator is a dedicated model-pinned Herdr session; the launcher does not take over its identity. The coordinator writes `handoff.md` and, unless the task is `--plan-only`, runs `herdr-flow run` **from its own pane**. The controller then routes implementation → independent review → repair if needed → final review back to the same coordinator.

Manual CLI path:

```bash
herdr-flow create --title "Add OAuth login"
$EDITOR .ai/workflow/task-001/brief.md  # Replace the placeholder with a non-secret request.
herdr-flow start-coordinator --task task-001  # From inside Herdr; required for managed coordinators.
herdr-flow status --task task-001
```

You can pin roles on `create` using `--coordinator-kind`, `--coordinator-model`, `--coordinator-variant`, `--worker-*`, `--reviewer-*`, `--fixer-*`, and `--escalation-*`; omit coordinator flags to use the global default. Task selections are frozen in `state.json` and do not change when defaults change. Effective defaults come from the private full config plus local `switch` overrides, not a project's `.ai/workflow/config.json` snapshot. Worker/reviewer/fixer sessions retain their model and identity once launched.

Task state lives in the source checkout under `.ai/workflow/<task-id>/`; the task worktree has an ignored marker and symlink to it. In the source checkout, the plugin snapshots **only** ignored root `AGENTS.md`/`CLAUDE.md` into the worktree when absent; it does not copy `.env`, `.agents/`, `.claude/`, or arbitrary ignored files. `.herdr-flow-context.md` warns agents that commands and Docker/Compose mounts from the main checkout may not work safely in an isolated worktree. Source drift or modified snapshots require inspection and, for changes to the source files, an explicit `herdr-flow refresh-instructions --task <id> --yes` from an idle Herdr task.

### Standard branch names

New tasks use `feat/<slug>`, `fix/<slug>`, `refactor/<slug>`, `docs/<slug>`, `test/<slug>`, or `chore/<slug>` based on keywords in the **task title**. For example, **“Optimize FormBuilder queries and caching”** produces `refactor/optimize-formbuilder-queries-and-caching`. A neutral title defaults to `feat/`. In command-driven planning, choose a meaningful action verb in the title; if inference is wrong, specify `--branch-type <type>` when creating the task. Herdr Flow adds `-task-<id>` only when the short name collides with a local/remote branch or another task record. It never renames an existing branch just because the naming policy changed.

If you rename a task branch in Git after creation, `finalize` accepts the new checked-out name **only** when the original branch no longer exists and Git's reflog proves the exact rename. It records the change in `project.branch_history` after all merge, cleanliness, and ownership checks pass. If proof is missing or ambiguous, it refuses cleanup with an actionable message: inspect the worktree and, from Herdr, run `herdr-flow reconcile-branch --task <id> --branch <actual-checked-out-name> --yes`, then `finalize`. Reconciliation requires a DONE task and matching task ownership marker; it never renames Git refs or overrides a foreign worktree.

### Review, recovery, and cleanup

When a *new* Codex escalation fixer is blocked on its first-run trust/model prompts, the start call can fail even though the agent remains alive. **Do not** run `resume`, `configure-agent`, or `resend` over that unrecorded session. Inspect the named agent and its pane from Herdr. This prerelease's recovery path is intentionally limited to a verified **GPT-6-Sol high** session: select that model in the *same* Codex pane, check its latest displayed label, then use `adopt-startup` with that pane ID and `--yes` from another Herdr pane. Adoption records the actual selection and terminal ownership without sending a prompt. Check status before running `advance` once; if already FIXING, do not advance again. Any mismatch halts for human inspection. Ordinary tasks never require adoption.

The independent reviewer must report evidence-backed PASS/FAIL in `review.md`; the coordinator does the same in `final-review.md`. Failures route to the original worker or dedicated fixer. Optional cumulative escalation starts only after the configured number of failures and reuses its own persistent session. Kimi, if chosen as a reviewer model, runs **inside OpenCode**, not Kimi Code CLI.

A blocked session waits for the user. Prompt delivery is fingerprinted and uncertain sends are **never** automatically replayed. Use `herdr-flow status`, `focus`, `inspect`, and `resume` from Herdr; only deliberately `resend --yes` after inspecting an uncertain prompt. Git commit/merge is a human action. After a real merge, `herdr-flow finalize --task <id>` checks branch ancestry, the clean worktree, ignored-file ownership, and idle task agents before non-forced removal of the workspace. The branch and task reports remain. Squash/rebase merges without ancestry cannot be verified automatically.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile src/herdr_flow.py
bash -n install.sh
```

Read `ACCEPTANCE.md` for the **live Herdr-pane** test; unit tests use a fake adapter and do not prove live harness behavior. The design and known concurrency/recovery limitations are described in `COORDINATION-DESIGN.md`.

## License

[MIT](LICENSE) © 2026 Hessam Taghvaei.
