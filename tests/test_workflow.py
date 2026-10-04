#!/usr/bin/env python3
from __future__ import annotations

import json
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from herdr_flow import BRIEF_PLACEHOLDER, FlowError, HerdrAdapter, HerdrError, Workflow, load_config, main, parse_args  # noqa: E402


class FakeHerdr:
    def __init__(self, root: Path):
        self.root = root
        self.agents_by_name = {}
        self.start_count = {"worker": 0, "fixer": 0, "escalation_fixer": 0, "reviewer": 0, "coordinator": 0}
        self.prompts = []
        self.start_args = {}
        self.pane_number = 1
        self.workspace = "w-test"
        self.agents_by_name["claude-test"] = {
            "name": "claude-test", "agent": "claude", "agent_status": "idle",
            "pane_id": "w-coordinator:p1", "workspace_id": "w-coordinator",
            "agent_session": {"kind": "id", "value": "session-claude-1"},
        }

    def _pane(self):
        pane = f"{self.workspace}:p{self.pane_number}"
        self.pane_number += 1
        return pane

    def create_worktree(self, source, branch, base, label):
        path = self.root / "worktrees" / label
        path.mkdir(parents=True, exist_ok=True)
        return {
            "workspace": {"workspace_id": self.workspace},
            "root_pane": {"pane_id": self._pane()},
            "worktree": {"path": str(path), "branch": branch},
        }

    def open_worktree(self, source, *, path=None, branch=None, label):
        target = Path(path) if path else self.root / "worktrees" / label
        target.mkdir(parents=True, exist_ok=True)
        return {
            "workspace": {"workspace_id": self.workspace},
            "root_pane": {"pane_id": self._pane()},
            "worktree": {"path": str(target), "branch": branch},
        }

    def create_tab(self, workspace, cwd, label):
        return {"root_pane": {"pane_id": self._pane()}}

    def split(self, pane, cwd):
        return {"pane_id": self._pane()}

    def start(self, name, kind, pane, args):
        if name.startswith("coordinator-"):
            role = "coordinator"
        elif name.startswith("reviewer-"):
            role = "reviewer"
        elif name.startswith("fixer-"):
            role = "fixer"
        elif name.startswith("escalator-") or name.startswith("escalation_fixer-"):
            role = "escalation_fixer"
        else:
            role = "worker"
        self.start_count[role] += 1
        self.start_args[role] = list(args)
        agent = {
            "name": name,
            "agent": kind,
            "agent_status": "idle",
            "pane_id": pane,
            "workspace_id": self.workspace,
            "agent_session": {"source": f"test:{kind}", "agent": kind, "kind": "id", "value": f"session-{role}-{self.start_count[role]}"},
        }
        self.agents_by_name[name] = agent
        return dict(agent)

    def prompt(self, target, text):
        self.prompts.append((target, text))
        agent = self.agent(target)
        if not agent:
            raise AssertionError(f"Cannot prompt absent fake agent {target}")
        agent["agent_status"] = "working"
        original = next(item for item in self.agents_by_name.values() if item["pane_id"] == agent["pane_id"])
        original["agent_status"] = "working"
        return dict(agent)

    def workspaces(self):
        return [{"workspace_id": self.workspace}]

    def agent(self, target):
        value = self.agents_by_name.get(target)
        if not value:
            value = next((a for a in self.agents_by_name.values() if a["pane_id"] == target), None)
        return dict(value) if value else None

    def read_agent(self, target, lines=100):
        return f"terminal output for {target}"

    def focus(self, target):
        return self.agent(target)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.validation_patch = patch("herdr_flow.validate_agent_selection")
        self.validation_patch.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("test\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.repo, check=True)
        self.previous_config_env = os.environ.get("HERDR_FLOW_CONFIG")
        os.environ["HERDR_FLOW_CONFIG"] = str(ROOT / "config.json")
        self.previous_plugin_config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        config_dir = self.root / "config"
        config_dir.mkdir()
        fixture = json.loads((ROOT / "config.json").read_text())
        fixture["agents"]["worker"]["model"] = "example/worker"
        fixture["agents"]["reviewer"]["model"] = "example/reviewer"
        (config_dir / "config.json").write_text(json.dumps(fixture))
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = str(config_dir)
        os.environ["HERDR_ENV"] = "1"
        os.environ["HERDR_PLUGIN_STATE_DIR"] = str(self.root / "plugin-state")
        os.environ["HERDR_PANE_ID"] = "w-coordinator:p1"
        self.fake = FakeHerdr(self.root)
        self.workflow = Workflow.discover(self.repo, create=True, herdr=self.fake)
        # Exercise escalation policy without requiring a paid or personal gateway in public defaults.
        self.workflow.config["repair"]["escalation"] = {
            "enabled": True, "after_failures": 3,
            "agent": {"kind": "codex", "model": "gpt-5.6-sol", "variant": "high"},
        }
        self.store = self.workflow.create("Acceptance test", "task-test-001")
        handoff = self.store.root / "handoff.md"
        handoff.write_text(handoff.read_text().replace("<!-- Claude coordinator: replace this placeholder. -->", "Implement a safe test change."))

    def tearDown(self):
        self.validation_patch.stop()
        if self.previous_plugin_config_dir is None:
            os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)
        else:
            os.environ["HERDR_PLUGIN_CONFIG_DIR"] = self.previous_plugin_config_dir
        if self.previous_config_env is None:
            os.environ.pop("HERDR_FLOW_CONFIG", None)
        else:
            os.environ["HERDR_FLOW_CONFIG"] = self.previous_config_env
        os.environ.pop("HERDR_ENV", None)
        os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
        os.environ.pop("HERDR_PANE_ID", None)
        self.tmp.cleanup()

    def mark_implementation_ready(self):
        path = self.store.root / "implementation.md"
        path.write_text(path.read_text().replace("PENDING", "READY", 1))
        self.workflow.finish(self.store)

    def write_review(self, status):
        path = self.store.root / "review.md"
        text = path.read_text()
        text = re.sub(r"(?im)(^## Status\s*\n+\s*)(PENDING|PASS|FAIL)", rf"\g<1>{status}", text, count=1)
        path.write_text(text)

    def write_final(self, status):
        path = self.store.root / "final-review.md"
        text = path.read_text()
        text = re.sub(r"(?im)(^## Status\s*\n+\s*)(PENDING|PASS|FAIL)", rf"\g<1>{status}", text, count=1)
        path.write_text(text)

    def settle(self, role):
        state = self.store.read()
        name = state[role]["name"]
        agent = self.fake.agents_by_name[name]
        agent["agent_status"] = "idle"
        self.workflow.handle_event({"event": "pane.agent_status_changed", "data": {"pane_id": agent["pane_id"], "agent_status": "idle"}})
        return self.store.read()

    def test_codex_coordinator_is_task_owned_and_does_not_share_worker_pane(self):
        store = self.workflow.create("Codex coordination", "task-codex", coordinator={"kind": "codex", "model": "gpt-6-sol", "variant": "high"}, worker={"kind": "codex", "model": "gpt-6-sol"})
        handoff = store.root / "handoff.md"
        handoff.write_text(handoff.read_text().replace("<!-- Claude coordinator: replace this placeholder. -->", "Implement feature."))
        with self.assertRaises(FlowError):
            self.workflow.run_task(store)
        with self.assertRaisesRegex(FlowError, "brief.md"):
            self.workflow.start_coordinator(store)
        (store.root / "brief.md").write_text("Implement the full original Codex request.\n")
        self.workflow.start_coordinator(store)
        state = store.read()
        self.assertEqual(["-m", "gpt-6-sol", "-c", 'model_reasoning_effort="high"'], self.fake.start_args["coordinator"])
        self.assertEqual(1, self.fake.start_count["coordinator"])
        self.workflow.start_coordinator(store)  # Same live session, no duplicate prompt.
        self.assertEqual(1, self.fake.start_count["coordinator"])
        self.assertEqual(1, len(self.fake.prompts))
        os.environ["HERDR_PANE_ID"] = state["coordinator"]["pane"]
        self.workflow.run_task(store)
        state = store.read()
        self.assertNotEqual(state["worker"]["pane"], state["coordinator"]["pane"])
        self.assertEqual("IMPLEMENTING", state["phase"])
        with self.assertRaises(FlowError):
            self.workflow.run_task(self.store)  # Wrong coordinator kind for the Claude task.

    def test_managed_coordinator_receives_final_review_not_external_claude(self):
        store = self.workflow.create("Final gate", "task-gate", coordinator={"kind": "opencode", "model": "example/kimi-test"})
        handoff = store.root / "handoff.md"
        handoff.write_text(handoff.read_text().replace("<!-- Claude coordinator: replace this placeholder. -->", "Deliver the feature."))
        (store.root / "brief.md").write_text("Deliver the full final-gate request.\n")
        self.workflow.start_coordinator(store)
        coordinator = store.read()["coordinator"]
        os.environ["HERDR_PANE_ID"] = coordinator["pane"]
        self.workflow.run_task(store)
        self.fake.agents_by_name[coordinator["name"]]["agent_status"] = "idle"
        self.workflow.handle_event({"event": "pane.agent_status_changed", "data": {"pane_id": coordinator["pane"], "agent_status": "idle"}})
        implementation = store.root / "implementation.md"
        implementation.write_text(implementation.read_text().replace("PENDING", "READY", 1))
        worker = store.read()["worker"]
        self.workflow.handle_event({"event": "pane.agent_status_changed", "data": {"pane_id": worker["pane"], "agent_status": "idle"}})
        review = store.root / "review.md"
        review.write_text(review.read_text().replace("PENDING", "PASS", 1))
        reviewer = store.read()["reviewer"]
        self.workflow.handle_event({"event": "pane.agent_status_changed", "data": {"pane_id": reviewer["pane"], "agent_status": "idle"}})
        self.assertEqual("FINAL_REVIEW", store.read()["phase"])
        self.assertEqual(coordinator["name"], self.fake.prompts[-1][0])
        self.assertIn("references/final-review.md", self.fake.prompts[-1][1])
        self.assertEqual(1, self.fake.start_count["coordinator"])
        self.assertEqual(1, len([target for target, prompt in self.fake.prompts if "Act as final release gate" in prompt]))

    def test_global_coordinator_default_is_frozen_per_task(self):
        self.workflow.config["agents"]["coordinator"] = {"kind": "codex", "model": "gpt-6-sol", "variant": "high"}
        codex_task = self.workflow.create("Use the default", "task-default-codex")
        self.assertEqual("codex", codex_task.read()["coordinator"]["kind"])
        self.assertEqual("gpt-6-sol", codex_task.read()["coordinator"]["model"])
        self.workflow.config["agents"]["coordinator"] = {"kind": "opencode", "model": "example/kimi-test", "variant": None}
        opencode_task = self.workflow.create("New global default", "task-default-opencode")
        self.assertEqual("opencode", opencode_task.read()["coordinator"]["kind"])
        self.assertEqual("codex", codex_task.read()["coordinator"]["kind"])
        changed_kind = self.workflow._selection(self.workflow.config["agents"]["coordinator"], {"kind": "codex"})
        self.assertIsNone(changed_kind["model"])

    def test_plan_only_coordinator_stops_after_handoff(self):
        store = self.workflow.create("Only plan", "task-plan-only", coordinator={"kind": "opencode", "model": "example/kimi-test"}, plan_only=True)
        (store.root / "brief.md").write_text("Write a plan; do not implement yet.\n")
        self.workflow.start_coordinator(store)
        self.assertFalse(store.read()["planning"]["auto_start"])
        self.assertIn("PLAN ONLY", self.fake.prompts[-1][1])
        self.assertNotIn("When the handoff is complete, run herdr-flow run", self.fake.prompts[-1][1])
        self.assertEqual("PLANNED", store.read()["phase"])

    def test_managed_coordinator_requires_original_request_and_references_shared_contract(self):
        store = self.workflow.create("Brief required", "task-brief", coordinator={"kind": "codex", "model": "gpt-6-sol"})
        self.assertIn(BRIEF_PLACEHOLDER, (store.root / "brief.md").read_text())
        with self.assertRaisesRegex(FlowError, "brief.md"):
            self.workflow.start_coordinator(store)
        (store.root / "brief.md").write_text("api_key=abcdef0123456789abcdef0123456789\n")
        with self.assertRaisesRegex(FlowError, "brief.md"):
            self.workflow.start_coordinator(store)
        (store.root / "brief.md").write_text("Make a safe feature with no secret data.\n")
        self.workflow.start_coordinator(store)
        self.assertIn(str(store.root / "brief.md"), self.fake.prompts[-1][1])
        self.assertIn("references/planning.md", self.fake.prompts[-1][1])
        self.assertEqual(1, len(self.fake.prompts))

    def test_harness_entry_points_exist_and_defaults_is_read_only(self):
        self.assertEqual("defaults", parse_args(["defaults"]).command)
        self.assertTrue(parse_args(["create", "--title", "Plan only", "--plan-only"]).plan_only)
        self.assertEqual("refactor", parse_args(["create", "--title", "Optimize queries", "--branch-type", "refactor"]).branch_type)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, main(["defaults"]))
        self.assertEqual("claude", json.loads(output.getvalue())["coordinator"]["kind"])
        root = ROOT
        for name in ("claude/ai-plan.md", "opencode/ai-plan.md", "opencode/ai-run.md", "codex/ai-plan.md", "codex/ai-run.md"):
            path = root / "commands" / name
            self.assertTrue(path.is_file(), name)
            if name.endswith("ai-plan.md") and not name.startswith("codex/"):
                self.assertIn("brief.md", path.read_text())
        for name in ("claude/ai-plan.md", "opencode/ai-plan.md"):
            content = (root / "commands" / name).read_text()
            self.assertIn("herdr-flow defaults", content)
            self.assertIn("--plan-only", content)
        self.assertIn("$herdr-flow-plan", (root / "commands/codex/ai-plan.md").read_text())
        self.assertTrue((root / "skills/herdr-flow-plan/SKILL.md").is_file())
        self.assertTrue((root / "skills/herdr-flow/references/planning.md").is_file())

    def test_opencode_coordinator_uses_pinned_model(self):
        store = self.workflow.create("OpenCode coordination", "task-opencode", coordinator={"kind": "opencode", "model": "example/kimi-test", "variant": "high"})
        (store.root / "brief.md").write_text("Full original OpenCode request.\n")
        self.workflow.start_coordinator(store)
        self.assertEqual(["--model", "example/kimi-test", "--variant", "high"], self.fake.start_args["coordinator"])
        self.assertEqual("opencode", store.read()["coordinator"]["kind"])

    def test_two_projects_same_task_id_have_distinct_agents_and_registry_entries(self):
        other_repo = self.root / "other"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=other_repo, check=True)
        second = Workflow.discover(other_repo, create=True, herdr=self.fake)
        other = second.create("Second project", self.store.task_id)
        self.assertNotEqual(self.store.read()["worker"]["name"], other.read()["worker"]["name"])
        self.workflow.run_task(self.store)
        second.run_task(other)
        self.assertEqual(2, len([item for item in self.workflow.registry.find_by_pane("w-coordinator:p1") if item[1] == "coordinator"]))

    def test_concurrent_advance_submits_review_once(self):
        self.workflow.run_task(self.store)
        report = self.store.root / "implementation.md"
        report.write_text(report.read_text().replace("PENDING", "READY", 1))
        with self.store.locked() as state:
            state["worker"]["status"] = "idle"
        # Multiple event/command drivers must not create multiple reviewer sessions.
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: self.workflow.advance(self.store), range(12)))
        self.assertEqual(1, self.fake.start_count["reviewer"])
        self.assertEqual(1, len([name for name, _ in self.fake.prompts if name == self.store.read()["reviewer"]["name"]]))

    def test_ignored_instructions_are_snapshotted_with_context_and_explicit_refresh(self):
        (self.repo / ".gitignore").write_text("/AGENTS.md\n/CLAUDE.md\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "ignore local instructions"], cwd=self.repo, check=True)
        (self.repo / "AGENTS.md").write_text("# Local rules\nUse the project conventions.\n")
        (self.repo / "CLAUDE.md").write_text("# Claude rules\nValidate commands before running.\n")
        checkout = self.root / "instructions-worktree"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "ai/instructions", str(checkout), "HEAD"], cwd=self.repo, check=True)
        with self.store.locked() as state:
            state["project"]["worktree_path"] = str(checkout)
            self.workflow._link_task_marker(checkout, state)
            self.workflow._snapshot_instructions(state, checkout)
        self.assertEqual((self.repo / "AGENTS.md").read_text(), (checkout / "AGENTS.md").read_text())
        self.assertEqual((self.repo / "CLAUDE.md").read_text(), (checkout / "CLAUDE.md").read_text())
        self.assertIn("Docker/Compose", (checkout / ".herdr-flow-context.md").read_text())
        self.assertEqual("", subprocess.run(["git", "status", "--porcelain"], cwd=checkout, capture_output=True, text=True, check=True).stdout)
        (self.repo / "AGENTS.md").write_text("# Updated rules\n")
        self.assertTrue(self.workflow.status(self.store)["instruction_notices"])
        with self.store.locked() as state:
            state["phase"] = "IMPLEMENTATION_DONE"
        with self.assertRaisesRegex(FlowError, "Instruction snapshot needs attention"):
            self.workflow.review(self.store)
        with self.store.locked() as state:
            with self.assertRaises(FlowError):
                self.workflow._snapshot_instructions(state, checkout)
        with self.assertRaises(FlowError):
            self.workflow.refresh_instructions(self.store, yes=False)
        self.workflow.refresh_instructions(self.store, yes=True)
        self.assertEqual([], self.workflow.status(self.store)["instruction_notices"])
        self.assertEqual("# Updated rules\n", (checkout / "AGENTS.md").read_text())
        (checkout / "AGENTS.md").write_text("# User edit, do not discard\n")
        with self.assertRaises(FlowError):
            self.workflow.refresh_instructions(self.store, yes=True)

    def test_new_instruction_requires_explicit_refresh_and_secret_is_rejected(self):
        (self.repo / ".gitignore").write_text("/AGENTS.md\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "ignore instructions"], cwd=self.repo, check=True)
        checkout = self.root / "new-instructions-worktree"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "ai/new-instructions", str(checkout), "HEAD"], cwd=self.repo, check=True)
        with self.store.locked() as state:
            state["project"]["worktree_path"] = str(checkout)
            self.workflow._link_task_marker(checkout, state)
            self.workflow._snapshot_instructions(state, checkout)
        (self.repo / "AGENTS.md").write_text("# New rules\n")
        with self.assertRaisesRegex(FlowError, "New instruction source"):
            with self.store.locked() as state:
                self.workflow._snapshot_instructions(state, checkout)
        self.workflow.refresh_instructions(self.store, yes=True)
        self.assertEqual("# New rules\n", (checkout / "AGENTS.md").read_text())
        (self.repo / "AGENTS.md").write_text("api_key=abcdef0123456789abcdef0123456789\n")
        with self.assertRaisesRegex(FlowError, "Potential secret"):
            self.workflow.refresh_instructions(self.store, yes=True)

    def test_existing_active_task_backfills_instructions_before_next_handoff(self):
        self.workflow.run_task(self.store)
        (self.repo / ".gitignore").write_text("/AGENTS.md\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "ignore instructions"], cwd=self.repo, check=True)
        (self.repo / "AGENTS.md").write_text("# Original project rules\n")
        path = self.root / "existing-task-worktree"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "ai/backfill", str(path), "HEAD"], cwd=self.repo, check=True)
        with self.store.locked() as state:
            state["project"]["worktree_path"] = str(path)
            state["project"].pop("instruction_snapshots", None)
            state["phase"] = "IMPLEMENTATION_DONE"
            self.workflow._link_task_marker(path, state)
        self.workflow.advance(self.store)
        self.assertEqual("# Original project rules\n", (path / "AGENTS.md").read_text())
        self.assertEqual("REVIEWING", self.store.read()["phase"], self.store.read()["automation"])
        self.assertIn(str(path / ".herdr-flow-context.md"), self.fake.prompts[-1][1])

    def test_tracked_worktree_instructions_are_never_replaced(self):
        (self.repo / "AGENTS.md").write_text("# Tracked rules\n")
        subprocess.run(["git", "add", "AGENTS.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "tracked instructions"], cwd=self.repo, check=True)
        checkout = self.root / "tracked-worktree"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "ai/tracked", str(checkout), "HEAD"], cwd=self.repo, check=True)
        (checkout / "AGENTS.md").write_text("# Worktree-specific rules\n")
        with self.store.locked() as state:
            self.workflow._snapshot_instructions(state, checkout)
        self.assertEqual("# Worktree-specific rules\n", (checkout / "AGENTS.md").read_text())
        self.assertNotIn("AGENTS.md", self.store.read()["project"]["instruction_snapshots"])

    def test_instruction_copy_refuses_unignored_untracked_file(self):
        (self.repo / "AGENTS.md").write_text("# Local rules\n")
        checkout = self.root / "unignored-worktree"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "ai/unignored", str(checkout), "HEAD"], cwd=self.repo, check=True)
        with self.store.locked() as state:
            with self.assertRaises(FlowError):
                self.workflow._snapshot_instructions(state, checkout)
        self.assertFalse((checkout / "AGENTS.md").exists())

    def test_worker_completion_command_triggers_next_phase_without_idle_event(self):
        self.workflow.run_task(self.store)
        report = self.store.root / "implementation.md"
        report.write_text(report.read_text().replace("PENDING", "READY", 1))
        self.workflow.finish(self.store)
        self.assertEqual("IMPLEMENTATION_DONE", self.store.read()["phase"])
        # The CLI chains this after finish even if Herdr has not published idle.
        self.workflow.advance(self.store)
        self.assertEqual("REVIEWING", self.store.read()["phase"])
        self.assertEqual(1, self.fake.start_count["reviewer"])

    def test_coordinator_events_reach_all_tasks_sharing_its_pane(self):
        self.workflow.run_task(self.store)
        other = self.workflow.create("Another feature", "task-test-002")
        handoff = other.root / "handoff.md"
        handoff.write_text(handoff.read_text().replace("<!-- Claude coordinator: replace this placeholder. -->", "Another task."))
        self.workflow.run_task(other)
        matches = self.workflow.registry.find_by_pane("w-coordinator:p1")
        self.assertEqual({self.store.task_id, other.task_id}, {store.task_id for store, role in matches if role == "coordinator"})
        self.workflow.handle_event({"event": "pane.agent_status_changed", "data": {"pane_id": "w-coordinator:p1", "agent_status": "idle"}})
        self.assertEqual("idle", self.store.read()["coordinator"]["status"])
        self.assertEqual("idle", other.read()["coordinator"]["status"])

    def test_settled_event_advances_even_if_agent_snapshot_is_stale(self):
        self.workflow.run_task(self.store)
        report = self.store.root / "implementation.md"
        report.write_text(report.read_text().replace("PENDING", "READY", 1))
        pane = self.store.read()["worker"]["pane"]
        # Herdr's agent.get can lag behind the delivered idle event. The event is
        # authoritative for this pane; a second snapshot must not stall the task.
        self.assertEqual("working", self.fake.agent(self.store.read()["worker"]["name"])["agent_status"])
        self.workflow.handle_event({"event": "pane.agent_status_changed", "data": {"pane_id": pane, "agent_status": "idle"}})
        self.assertEqual("REVIEWING", self.store.read()["phase"])
        self.assertEqual(1, self.fake.start_count["reviewer"])

    def test_automation_does_not_bypass_blocked_worker_or_resend_prompt(self):
        self.workflow.run_task(self.store)
        original = self.store.read()
        pane = original["worker"]["pane"]
        self.workflow.handle_event({"event": "pane.agent_status_changed", "data": {"pane_id": pane, "agent_status": "blocked"}})
        blocked = self.store.read()
        self.assertEqual("WAITING_FOR_USER", blocked["phase"])
        self.assertEqual(1, len(self.fake.prompts))
        self.workflow.advance(self.store)
        self.assertEqual(1, len(self.fake.prompts))
        self.assertEqual("WAITING_FOR_USER", self.store.read()["phase"])

    def test_final_review_waits_for_existing_coordinator_to_settle(self):
        self.workflow.run_task(self.store)
        report = self.store.root / "implementation.md"
        report.write_text(report.read_text().replace("PENDING", "READY", 1))
        self.settle("worker")
        self.fake.agents_by_name["claude-test"]["agent_status"] = "working"
        self.write_review("PASS")
        state = self.settle("reviewer")
        self.assertEqual("FINAL_REVIEW", state["phase"])
        self.assertIn("become idle", state["automation"]["attention"])
        self.assertNotEqual("claude-test", self.fake.prompts[-1][0])
        self.settle("coordinator")
        self.assertEqual("w-coordinator:p1", self.fake.prompts[-1][0])

    def _ready_for_final_review(self):
        self.workflow.run_task(self.store)
        report = self.store.root / "implementation.md"
        report.write_text(report.read_text().replace("PENDING", "READY", 1))
        self.settle("worker")
        self.write_review("PASS")
        self.workflow.review_result(self.store, "PASS")
        self.assertEqual("FINAL_REVIEW", self.store.read()["phase"])

    def test_attached_coordinator_without_name_receives_final_review_by_verified_pane(self):
        self._ready_for_final_review()
        live = self.fake.agents_by_name.pop("claude-test")
        live.pop("name")
        self.fake.agents_by_name["unnamed"] = live
        self.assertTrue(self.workflow.status(self.store)["coordinator"]["live"])
        self.assertIn("terminal output", self.workflow.inspect_agent(self.store, "coordinator"))
        self.assertEqual("w-coordinator:p1", self.workflow.focus(self.store, "coordinator")["pane_id"])
        state = self.workflow.advance(self.store)
        self.assertEqual(1, state["final_review"]["attempt"])
        self.assertIsNone(state["automation"]["attention"])
        self.assertEqual("w-coordinator:p1", self.fake.prompts[-1][0])
        count = len(self.fake.prompts)
        self.workflow.advance(self.store)
        self.assertEqual(count, len(self.fake.prompts))

    def test_attached_coordinator_renamed_in_same_session_still_receives_final_review(self):
        self._ready_for_final_review()
        live = self.fake.agents_by_name.pop("claude-test")
        live["name"] = "claude"
        self.fake.agents_by_name["claude"] = live
        state = self.workflow.advance(self.store)
        self.assertEqual(1, state["final_review"]["attempt"])
        self.assertEqual("w-coordinator:p1", self.fake.prompts[-1][0])

    def test_attached_coordinator_session_change_blocks_final_review(self):
        self._ready_for_final_review()
        live = self.fake.agents_by_name["claude-test"]
        live["agent_session"] = {"kind": "id", "value": "different-session"}
        self.assertFalse(self.workflow.status(self.store)["coordinator"]["live"])
        with self.assertRaisesRegex(FlowError, "Original coordinator pane or session changed"):
            self.workflow.inspect_agent(self.store, "coordinator")
        state = self.workflow.advance(self.store)
        self.assertIn("session cannot be verified", state["automation"]["attention"])
        self.assertEqual(0, state["final_review"]["attempt"])
        self.assertEqual(0, len([p for _, p in self.fake.prompts if "Act as final release gate" in p]))
        with self.assertRaisesRegex(FlowError, "refusing to focus"):
            self.workflow.focus(self.store, "coordinator")
        with self.assertRaisesRegex(FlowError, "Original attached coordinator"):
            self.workflow.run_task(self.store)

    def test_coordinator_prompt_identity_change_is_uncertain_not_reassigned(self):
        self._ready_for_final_review()
        original = self.store.read()["coordinator"]["session"]
        prompt = self.fake.prompt
        def changed(target, text):
            reply = prompt(target, text)
            reply["agent_session"] = {"kind": "id", "value": "changed-after-prompt"}
            return reply
        self.fake.prompt = changed
        state = self.workflow.advance(self.store)
        self.assertIn("identity changed during prompt submission", state["automation"]["attention"])
        self.assertEqual("uncertain", state["prompts"]["coordinator"]["status"])
        self.assertEqual(original, state["coordinator"]["session"])
        count = len(self.fake.prompts)
        self.workflow.advance(self.store)
        self.assertEqual(count, len(self.fake.prompts))

    def test_title_branch_is_readable_and_unique(self):
        self.assertEqual("test/acceptance-test", self.store.read()["project"]["branch"])
        store = self.workflow.create("Acceptance test", "task-test-002")
        self.assertEqual("test/acceptance-test-task-test-002", store.read()["project"]["branch"])
        refactor = self.workflow.create("Optimize FormBuilder queries and caching", "task-optimize")
        self.assertEqual("refactor/optimize-formbuilder-queries-and-caching", refactor.read()["project"]["branch"])
        fixed = self.workflow.create("Fix login error", "task-fix")
        self.assertEqual("fix/fix-login-error", fixed.read()["project"]["branch"])
        override = self.workflow.create("Optimize login", "task-feature", branch_type="feat")
        self.assertEqual("feat/optimize-login", override.read()["project"]["branch"])
        with self.assertRaisesRegex(FlowError, "Branch type"):
            self.workflow.create("Bad type", "task-invalid", branch_type="ai")
        self.assertFalse((self.workflow.workflow_root / "task-invalid").exists())

    def test_automatic_handoffs_from_reports_and_coordinator(self):
        state = self.workflow.run_task(self.store)
        self.assertEqual("IMPLEMENTING", state["phase"])
        self.assertEqual("w-coordinator:p1", state["coordinator"]["pane"])
        self.assertEqual(1, self.fake.start_count["worker"])
        path = self.store.root / "implementation.md"
        path.write_text(path.read_text().replace("PENDING", "READY", 1))
        self.assertEqual("REVIEWING", self.settle("worker")["phase"])
        self.write_review("FAIL")
        failed = self.settle("reviewer")
        self.assertEqual("FIXING", failed["phase"])
        self.assertEqual(1, failed["repair"]["failure_count"])
        self.assertEqual(1, self.fake.start_count["worker"])
        self.assertIn("PENDING", path.read_text())
        path.write_text(path.read_text().replace("PENDING", "READY", 1))
        self.assertEqual("REVIEWING", self.settle("worker")["phase"])
        self.assertIn("PENDING", (self.store.root / "review.md").read_text())
        self.write_review("PASS")
        passed = self.settle("reviewer")
        self.assertEqual("FINAL_REVIEW", passed["phase"])
        self.assertEqual(1, self.fake.start_count["reviewer"])
        self.assertEqual("w-coordinator:p1", self.fake.prompts[-1][0])
        sent = len(self.fake.prompts)
        self.settle("reviewer")
        self.assertEqual(sent, len(self.fake.prompts))
        self.write_final("PASS")
        self.assertEqual("DONE", self.settle("coordinator")["phase"])
        self.assertEqual(1, self.fake.start_count["worker"])

    def test_branch_conflicts_with_remote_and_namespace_are_guarded(self):
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip()
        subprocess.run(["git", "update-ref", "refs/remotes/origin/feat/add-dashboard", head], cwd=self.repo, check=True)
        task = self.workflow.create("Add dashboard", "task-dashboard")
        self.assertEqual("feat/add-dashboard-task-dashboard", task.read()["project"]["branch"])
        subprocess.run(["git", "update-ref", "refs/heads/docs", head], cwd=self.repo, check=True)
        with self.assertRaisesRegex(FlowError, "namespace docs/"):
            self.workflow.create("Document dashboard", "task-docs")

    def _prepare_merged_renamed_worktree(self):
        path = self.root / "renamed-task-worktree"
        original = self.store.read()["project"]["branch"]
        renamed = "refactor/acceptance-test"
        subprocess.run(["git", "worktree", "add", "-q", "-b", original, str(path), "HEAD"], cwd=self.repo, check=True)
        with self.store.locked() as state:
            state["phase"] = "DONE"
            state["review"]["status"] = "PASS"
            state["final_review"]["status"] = "PASS"
            state["project"]["worktree_path"] = str(path)
            state["project"]["workspace_id"] = self.fake.workspace
            self.workflow._link_task_marker(path, state)
        (path / "README.md").write_text("implemented\n")
        subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-qm", "implementation"], cwd=path, check=True)
        subprocess.run(["git", "branch", "-m", renamed], cwd=path, check=True)
        subprocess.run(["git", "merge", "-q", "--ff-only", renamed], cwd=self.repo, check=True)
        self.fake.panes = lambda: []
        self.fake.remove_worktree = lambda workspace: subprocess.run(["git", "worktree", "remove", str(path)], cwd=self.repo, check=True)
        return path, original, renamed

    def test_finalize_recognizes_verified_git_branch_rename(self):
        path, original, renamed = self._prepare_merged_renamed_worktree()
        state = self.workflow.finalize(self.store)
        self.assertTrue(state["cleanup"]["completed"])
        self.assertFalse(path.exists())
        self.assertEqual(renamed, state["project"]["branch"])
        self.assertEqual({"from": original, "to": renamed, "reason": "git_reflog"},
                         {key: state["project"]["branch_history"][0][key] for key in ("from", "to", "reason")})

    def test_ambiguous_rename_requires_explicit_confirmation_and_ownership(self):
        path, original, renamed = self._prepare_merged_renamed_worktree()
        subprocess.run(["git", "branch", original, "HEAD"], cwd=self.repo, check=True)
        with self.assertRaisesRegex(FlowError, "still exists"):
            self.workflow.finalize(self.store)
        with self.assertRaisesRegex(FlowError, "requires --yes"):
            self.workflow.reconcile_branch(self.store, renamed)
        with self.assertRaisesRegex(FlowError, "not the requested"):
            self.workflow.reconcile_branch(self.store, "feat/other", yes=True)
        marker = path / ".herdr-flow-task.json"
        data = json.loads(marker.read_text())
        data["task_id"] = "task-other"
        marker.write_text(json.dumps(data))
        with self.assertRaisesRegex(FlowError, "ownership"):
            self.workflow.reconcile_branch(self.store, renamed, yes=True)
        data["task_id"] = self.store.task_id
        marker.write_text(json.dumps(data))
        other = self.workflow.create("Another task", "task-branch-owner")
        with other.locked() as owned:
            owned["project"]["branch"] = renamed
        with self.assertRaisesRegex(FlowError, "another Herdr Flow task"):
            self.workflow.reconcile_branch(self.store, renamed, yes=True)
        with other.locked() as owned:
            owned["project"]["branch"] = "feat/another-task"
        reconciled = self.workflow.reconcile_branch(self.store, renamed, yes=True)
        self.assertEqual(renamed, reconciled["project"]["branch"])
        self.assertEqual("user_confirmed", reconciled["project"]["branch_history"][0]["reason"])
        self.assertTrue(self.workflow.finalize(self.store)["cleanup"]["completed"])

    def test_finalize_rejects_unmerged_or_dirty_and_removes_clean_merged_worktree(self):
        path = self.root / "task-checkout"
        branch = self.store.read()["project"]["branch"]
        subprocess.run(["git", "worktree", "add", "-q", "-b", branch, str(path), "HEAD"], cwd=self.repo, check=True)
        (path / "README.md").write_text("implemented\n")
        subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-qm", "implementation"], cwd=path, check=True)
        with self.store.locked() as state:
            state["phase"] = "DONE"
            state["review"]["status"] = "PASS"
            state["final_review"]["status"] = "PASS"
            state["project"]["worktree_path"] = str(path)
            state["project"]["workspace_id"] = self.fake.workspace
        with self.assertRaisesRegex(FlowError, "not merged"):
            self.workflow.finalize(self.store)
        subprocess.run(["git", "merge", "-q", "--ff-only", branch], cwd=self.repo, check=True)
        (path / "untracked.txt").write_text("keep\n")
        with self.assertRaisesRegex(FlowError, "uncommitted or untracked"):
            self.workflow.finalize(self.store)
        (path / "untracked.txt").unlink()
        (path / ".herdr-flow-task.json").write_text("{}\n")
        (path / ".ai").mkdir()
        (path / ".ai" / "workflow").symlink_to(self.store.workflow_root, target_is_directory=True)
        (path / "ignored-cache").mkdir()
        (path / "ignored-cache" / "important.bin").write_text("keep\n")
        (path / ".gitignore").write_text("ignored-cache/\n.herdr-flow-task.json\n.ai/workflow\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-qm", "ignore generated cache"], cwd=path, check=True)
        subprocess.run(["git", "merge", "-q", "--ff-only", branch], cwd=self.repo, check=True)
        with self.assertRaisesRegex(FlowError, "ignored files"):
            self.workflow.finalize(self.store)
        (path / "ignored-cache" / "important.bin").unlink()
        (path / "ignored-cache").rmdir()
        self.fake.panes = lambda: []
        self.fake.remove_worktree = lambda workspace: subprocess.run(
            ["git", "worktree", "remove", str(path)], cwd=self.repo, check=True
        )
        state = self.workflow.finalize(self.store)
        self.assertTrue(state["cleanup"]["completed"])
        self.assertFalse(path.exists())

    def test_finalize_preserves_modified_instruction_snapshot(self):
        (self.repo / ".gitignore").write_text("/AGENTS.md\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "ignore instruction"], cwd=self.repo, check=True)
        (self.repo / "AGENTS.md").write_text("# Local rules\n")
        path = self.root / "finish-instructions"
        branch = self.store.read()["project"]["branch"]
        subprocess.run(["git", "worktree", "add", "-q", "-b", branch, str(path), "HEAD"], cwd=self.repo, check=True)
        with self.store.locked() as state:
            state["phase"] = "DONE"
            state["review"]["status"] = "PASS"
            state["final_review"]["status"] = "PASS"
            state["project"]["worktree_path"] = str(path)
            state["project"]["workspace_id"] = self.fake.workspace
            self.workflow._link_task_marker(path, state)
            self.workflow._snapshot_instructions(state, path)
        (path / "README.md").write_text("done\n")
        subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-qm", "done"], cwd=path, check=True)
        subprocess.run(["git", "merge", "-q", "--ff-only", branch], cwd=self.repo, check=True)
        (path / "AGENTS.md").write_text("# User changed this\n")
        with self.assertRaisesRegex(FlowError, "Owned instruction file changed"):
            self.workflow.finalize(self.store)
        (path / "AGENTS.md").write_text("# Local rules\n")
        self.fake.panes = lambda: []
        self.fake.remove_worktree = lambda workspace: subprocess.run(["git", "worktree", "remove", str(path)], cwd=self.repo, check=True)
        self.assertTrue(self.workflow.finalize(self.store)["cleanup"]["completed"])

    def test_create_persists_per_task_agent_overrides(self):
        store = self.workflow.create(
            "Configured task",
            "task-configured-002",
            worker={"kind": "codex", "model": "gpt-5.6-sol", "variant": "high"},
            reviewer={"kind": "opencode", "model": "example/kimi-test"},
        )
        state = store.read()
        self.assertEqual({"kind": "codex", "model": "gpt-5.6-sol", "variant": "high"}, {key: state["worker"].get(key) for key in ("kind", "model", "variant")})
        self.assertEqual("opencode", state["reviewer"]["kind"])
        self.assertEqual("example/kimi-test", state["reviewer"]["model"])

    def test_codex_worker_receives_model_and_reasoning_effort(self):
        self.workflow.configure_role(self.store, "worker", kind="codex", model="gpt-5.6-sol", variant="high", variant_set=True)
        self.workflow.implement(self.store)
        self.assertEqual(["-m", "gpt-5.6-sol", "-c", 'model_reasoning_effort="high"'], self.fake.start_args["worker"])

    def test_opencode_reviewer_receives_model_and_variant(self):
        self.workflow.configure_role(self.store, "reviewer", kind="opencode", model="example/kimi-test", variant="high", variant_set=True)
        self.workflow.implement(self.store)
        self.mark_implementation_ready()
        self.workflow.review(self.store)
        self.assertEqual(["--model", "example/kimi-test", "--variant", "high"], self.fake.start_args["reviewer"])

    def test_agent_selection_is_immutable_after_session_start(self):
        self.workflow.implement(self.store)
        with self.assertRaisesRegex(FlowError, "immutable"):
            self.workflow.configure_role(self.store, "worker", kind="codex", model="gpt-5.6-sol")

    def test_dedicated_fixer_can_be_separate_from_implementation_worker(self):
        self.workflow.configure_role(
            self.store,
            "fixer",
            kind="codex",
            model="gpt-5.6-sol",
            variant="high",
            variant_set=True,
        )
        implemented = self.workflow.implement(self.store)
        worker_session = implemented["worker"]["session"]["value"]
        self.mark_implementation_ready()
        self.workflow.review(self.store)
        self.write_review("FAIL")
        self.workflow.review_result(self.store, "FAIL")
        fixed = self.workflow.implement(self.store)
        self.assertEqual("fixer", fixed["repair"]["active_role"])
        self.assertEqual(worker_session, fixed["worker"]["session"]["value"])
        self.assertEqual(1, self.fake.start_count["worker"])
        self.assertEqual(1, self.fake.start_count["fixer"])
        self.assertEqual(["-m", "gpt-5.6-sol", "-c", 'model_reasoning_effort="high"'], self.fake.start_args["fixer"])

    def test_matching_dedicated_fixer_is_reused_at_escalation_threshold(self):
        self.workflow.configure_role(
            self.store,
            "fixer",
            kind="codex",
            model="gpt-5.6-sol",
            variant="high",
            variant_set=True,
        )
        self.workflow.implement(self.store)
        for _ in range(4):
            self.mark_implementation_ready()
            self.workflow.review(self.store)
            self.write_review("FAIL")
            self.workflow.review_result(self.store, "FAIL")
            fixed = self.workflow.implement(self.store)
        self.assertTrue(fixed["repair"]["escalated"])
        self.assertEqual("fixer", fixed["repair"]["active_role"])
        self.assertEqual(1, self.fake.start_count["fixer"])
        self.assertEqual(0, self.fake.start_count["escalation_fixer"])

    def _stalled_first_run_codex(self, displayed="GPT-6-Sol high"):
        self.workflow.configure_role(self.store, "escalation_fixer", kind="codex", model="gpt-5.6-sol", variant="high", variant_set=True)
        self.workflow.run_task(self.store)
        with self.store.locked() as state:
            state["phase"] = "REVIEW_FAILED"
            state["repair"]["failure_count"] = 3
            state["repair"]["escalation"]["after_failures"] = 2
            state["repair"]["active_role"] = "escalation_fixer"
            state["escalation_fixer"]["status"] = "needs_fix"
            state["automation"]["attention"] = "agent_not_ready: blocked during startup"
            name = state["escalation_fixer"]["name"]
            path = state["project"]["worktree_path"]
        self.fake.agents_by_name[name] = {
            "name": name, "agent": "codex", "agent_status": "idle", "interactive_ready": True,
            "pane_id": "w-test:p3", "workspace_id": "w-test", "terminal_id": "term-first-run",
            "cwd": path,
        }
        self.fake.read_agent = lambda target, lines=100: f"• Model changed to GPT-6-Sol medium\n{displayed} · {path}"
        return name

    def test_adapter_reads_raw_herdr_agent_text_and_preserves_errors(self):
        adapter = HerdrAdapter(binary="/fake/herdr")
        args = ["/fake/herdr", "agent", "read", "escalator-test", "--source", "recent-unwrapped", "--lines", "80"]
        screen = "Codex screen\nGPT-6-Sol high · task worktree\n"
        with patch("herdr_flow.subprocess.run", return_value=subprocess.CompletedProcess(args, 0, screen, "")) as invoked:
            self.assertEqual(screen, adapter.read_agent("escalator-test", lines=80))
            invoked.assert_called_once_with(args, text=True, capture_output=True)
        error = '{"error":{"code":"agent_not_found","message":"Agent is gone"}}'
        with patch("herdr_flow.subprocess.run", return_value=subprocess.CompletedProcess(args, 1, "", error)):
            with self.assertRaisesRegex(HerdrError, "agent_not_found: Agent is gone"):
                adapter.read_agent("escalator-test", lines=80)

    def test_adopt_blocked_first_run_codex_without_new_session_or_prompt_replay(self):
        name = self._stalled_first_run_codex()
        sent = len(self.fake.prompts)
        pending = self.workflow.advance(self.store)
        self.assertIn("adopt-startup", pending["automation"]["attention"])
        self.assertEqual(sent, len(self.fake.prompts))
        with self.assertRaisesRegex(FlowError, "adopt-startup"):
            self.workflow.configure_role(self.store, "escalation_fixer", model="gpt-6-sol")
        with self.assertRaisesRegex(FlowError, "--yes"):
            self.workflow.adopt_startup(self.store, pane="w-test:p3", model="gpt-6-sol", variant="high")
        adopted = self.workflow.adopt_startup(self.store, pane="w-test:p3", model="gpt-6-sol", variant="high", yes=True)
        self.assertEqual("REVIEW_FAILED", adopted["phase"])
        self.assertEqual(sent, len(self.fake.prompts))
        self.assertEqual("gpt-6-sol", adopted["escalation_fixer"]["model"])
        self.assertEqual("term-first-run", adopted["escalation_fixer"]["terminal_id"])
        self.assertIsNone(adopted["prompts"]["escalation_fixer"])
        with self.assertRaisesRegex(FlowError, "already has recorded identity"):
            self.workflow.adopt_startup(self.store, pane="w-test:p3", model="gpt-6-sol", variant="high", yes=True)
        fixed = self.workflow.advance(self.store)
        self.assertEqual("FIXING", fixed["phase"])
        self.assertEqual(0, self.fake.start_count["escalation_fixer"])
        self.assertEqual(1, len([p for _, p in self.fake.prompts if "escalation fixer" in p]))
        self.assertEqual(name, self.fake.prompts[-1][0])
        self.assertIn("model gpt-6-sol with high", self.fake.prompts[-1][1])
        self.workflow.advance(self.store)
        self.assertEqual(sent + 1, len(self.fake.prompts))

    def test_adoption_rejects_wrong_model_screen_and_changed_terminal(self):
        name = self._stalled_first_run_codex(displayed="GPT-6-Sol medium")
        with self.assertRaisesRegex(FlowError, "does not confirm"):
            self.workflow.adopt_startup(self.store, pane="w-test:p3", model="gpt-6-sol", variant="high", yes=True)
        self.assertIsNone(self.store.read()["escalation_fixer"]["pane"])
        self.fake.read_agent = lambda target, lines=100: "GPT-6-Sol high"
        with self.assertRaisesRegex(FlowError, "confirmed task pane"):
            self.workflow.adopt_startup(self.store, pane="w-test:p4", model="gpt-6-sol", variant="high", yes=True)
        self.workflow.adopt_startup(self.store, pane="w-test:p3", model="gpt-6-sol", variant="high", yes=True)
        self.fake.agents_by_name[name]["terminal_id"] = "term-replacement"
        before = len(self.fake.prompts)
        blocked = self.workflow.advance(self.store)
        self.assertEqual("REVIEW_FAILED", blocked["phase"])
        self.assertIn("identity changed", blocked["automation"]["attention"])
        self.assertEqual(before, len(self.fake.prompts))

    def test_adopted_codex_model_drift_blocks_first_prompt(self):
        self._stalled_first_run_codex()
        self.workflow.adopt_startup(self.store, pane="w-test:p3", model="gpt-6-sol", variant="high", yes=True)
        self.fake.read_agent = lambda target, lines=100: "GPT-6-Sol medium"
        before = len(self.fake.prompts)
        blocked = self.workflow.advance(self.store)
        self.assertEqual("REVIEW_FAILED", blocked["phase"])
        self.assertIn("no longer confirms GPT-6-Sol high", blocked["automation"]["attention"])
        self.assertEqual(before, len(self.fake.prompts))

    def test_fourth_cumulative_failure_escalates_and_reuses_codex_fixer(self):
        original = self.workflow.implement(self.store)
        worker_session = original["worker"]["session"]["value"]
        for failure in range(1, 5):
            self.mark_implementation_ready()
            self.workflow.review(self.store)
            self.write_review("FAIL")
            failed = self.workflow.review_result(self.store, "FAIL")
            self.assertEqual(failure, failed["repair"]["failure_count"])
            fixed = self.workflow.implement(self.store)
            expected_role = "worker" if failure <= 3 else "escalation_fixer"
            self.assertEqual(expected_role, fixed["repair"]["active_role"])
        self.assertEqual(worker_session, fixed["worker"]["session"]["value"])
        self.assertEqual(1, self.fake.start_count["worker"])
        self.assertEqual(1, self.fake.start_count["escalation_fixer"])
        self.assertEqual(["-m", "gpt-5.6-sol", "-c", 'model_reasoning_effort="high"'], self.fake.start_args["escalation_fixer"])

        escalation_session = fixed["escalation_fixer"]["session"]["value"]
        self.mark_implementation_ready()
        self.workflow.review(self.store)
        self.write_review("FAIL")
        self.workflow.review_result(self.store, "FAIL")
        later = self.workflow.implement(self.store)
        self.assertEqual("escalation_fixer", later["repair"]["active_role"])
        self.assertEqual(escalation_session, later["escalation_fixer"]["session"]["value"])
        self.assertEqual(1, self.fake.start_count["escalation_fixer"])

    def test_final_review_failure_counts_toward_escalation(self):
        self.workflow.implement(self.store)
        self.mark_implementation_ready()
        self.workflow.review(self.store)
        self.write_review("PASS")
        self.workflow.review_result(self.store, "PASS")
        self.workflow.final_start(self.store)
        self.write_final("FAIL")
        failed = self.workflow.final_result(self.store, "FAIL")
        self.assertEqual(1, failed["repair"]["failure_count"])
        self.assertEqual("worker", failed["repair"]["active_role"])

    def test_same_worker_is_reused_after_review_failure(self):
        first = self.workflow.implement(self.store)
        original_name = first["worker"]["name"]
        original_pane = first["worker"]["pane"]
        original_session = first["worker"]["session"]["value"]
        self.mark_implementation_ready()
        self.workflow.review(self.store)
        self.write_review("FAIL")
        self.workflow.review_result(self.store, "FAIL")
        fixed = self.workflow.implement(self.store)
        self.assertEqual(original_name, fixed["worker"]["name"])
        self.assertEqual(original_pane, fixed["worker"]["pane"])
        self.assertEqual(original_session, fixed["worker"]["session"]["value"])
        self.assertEqual(1, self.fake.start_count["worker"])
        self.assertEqual("FIXING", fixed["phase"])

    def test_reviewer_prompt_is_a_strict_commit_merge_gate(self):
        self.workflow.implement(self.store)
        self.mark_implementation_ready()
        self.workflow.review(self.store)
        prompt = self.fake.prompts[-1][1]
        self.assertIn("fully satisfies the handoff", prompt)
        self.assertIn("ready to commit and merge", prompt)
        self.assertIn("Trace every handoff requirement", prompt)
        self.assertIn("Mark anything you cannot verify as NOT VERIFIED", prompt)
        self.assertIn("PASS only when", prompt)
        self.assertIn("Do not modify implementation files", prompt)
        template = (self.store.root / "review.md").read_text()
        self.assertIn("## Handoff Coverage", template)
        self.assertIn("## Validation Evidence", template)
        self.assertIn("## Gaps and Unverified Assumptions", template)
        self.assertIn("## Commit and Merge Readiness", template)

    def test_reviewer_is_independent_and_reused(self):
        self.workflow.implement(self.store)
        self.mark_implementation_ready()
        first_review = self.workflow.review(self.store)
        reviewer = first_review["reviewer"]["name"]
        self.assertNotEqual(first_review["worker"]["pane"], first_review["reviewer"]["pane"])
        self.write_review("FAIL")
        self.workflow.review_result(self.store, "FAIL")
        self.workflow.implement(self.store)
        self.mark_implementation_ready()
        second_review = self.workflow.review(self.store)
        self.assertEqual(reviewer, second_review["reviewer"]["name"])
        self.assertEqual(1, self.fake.start_count["reviewer"])

    def test_blocked_worker_waits_for_user_and_resumes_same_session(self):
        state = self.workflow.implement(self.store)
        pane = state["worker"]["pane"]
        self.workflow.handle_event({"event": "pane.agent_status_changed", "data": {"pane_id": pane, "agent_status": "blocked"}})
        blocked = self.store.read()
        self.assertEqual("WAITING_FOR_USER", blocked["phase"])
        self.assertEqual(state["worker"]["name"], blocked["user_interaction"]["blocked_agent"])
        self.workflow.handle_event({"event": "pane.agent_status_changed", "data": {"pane_id": pane, "agent_status": "working"}})
        resumed = self.store.read()
        self.assertEqual("IMPLEMENTING", resumed["phase"])
        self.assertEqual(1, self.fake.start_count["worker"])

    def test_lost_worker_is_recovered_only_after_resume(self):
        original = self.workflow.implement(self.store)
        old_name = original["worker"]["name"]
        self.fake.agents_by_name.pop(old_name)
        still_recorded = self.store.read()
        self.assertEqual(old_name, still_recorded["worker"]["name"])
        recovered = self.workflow.resume(self.store)
        self.assertNotEqual(old_name, recovered["worker"]["name"])
        self.assertTrue(recovered["worker"]["name"].endswith("-r1"))
        self.assertEqual(2, self.fake.start_count["worker"])

    def test_duplicate_prompt_is_refused_until_inspected(self):
        self.workflow.implement(self.store)
        with self.assertRaisesRegex(FlowError, "already submitted"):
            self.workflow.implement(self.store)
        self.assertEqual(1, len(self.fake.prompts))
        output = self.workflow.inspect_agent(self.store, "worker")
        self.assertIn("terminal output", output)
        self.workflow.resend(self.store, "worker", True)
        self.assertEqual(2, len(self.fake.prompts))

    def test_complete_lifecycle_reaches_done(self):
        self.workflow.implement(self.store)
        self.mark_implementation_ready()
        self.workflow.review(self.store)
        self.write_review("PASS")
        reviewed = self.workflow.review_result(self.store, "PASS")
        self.assertEqual("FINAL_REVIEW", reviewed["phase"])
        self.workflow.final_start(self.store)
        self.write_final("PASS")
        done = self.workflow.final_result(self.store, "PASS")
        self.assertEqual("DONE", done["phase"])
        self.assertEqual("PASS", done["review"]["status"])
        self.assertEqual("PASS", done["final_review"]["status"])


    def test_pi_interactive_picker_uses_installed_model_and_thinking(self):
        from herdr_flow import defaults_override_path
        with patch("herdr_flow.sys.stdin.isatty", return_value=True), \
             patch("herdr_flow._catalog", return_value=["example/think"]), \
             patch("herdr_flow.pi_models", return_value={"example/think": True}), \
             patch("herdr_flow.validate_agent_selection"), \
             patch("builtins.input", side_effect=["2", "3", "think", "1", "1", "y"]), \
             redirect_stdout(io.StringIO()):
            main(["switch"])
        self.assertEqual({"kind": "pi", "model": "example/think", "variant": None},
                         json.loads(defaults_override_path().read_text())["roles"]["worker"])

    def test_pi_role_launch_and_future_task_switch(self):
        from herdr_flow import defaults_override_path
        original = self.store.read()["reviewer"].copy()
        store = self.workflow.create("Pi coordination", "task-pi", coordinator={"kind": "pi", "model": "example/think", "variant": "max"}, worker={"kind": "pi", "model": "example/think", "variant": "off"})
        (store.root / "brief.md").write_text("Implement the Pi request.\n")
        self.workflow.start_coordinator(store)
        coordinator = store.read()["coordinator"]
        self.assertEqual(["--model", "example/think", "--thinking", "max"], self.fake.start_args["coordinator"])
        os.environ["HERDR_PANE_ID"] = coordinator["pane"]
        self.workflow.run_task(store)
        self.workflow.implement(store)
        self.assertEqual(["--model", "example/think", "--thinking", "off"], self.fake.start_args["worker"])
        self.assertNotEqual(coordinator["pane"], store.read()["worker"]["pane"])
        with redirect_stdout(io.StringIO()):
            main(["switch", "--role", "reviewer", "--kind", "pi", "--model", "example/think", "--variant", "high"])
        self.assertEqual("pi", load_config()["agents"]["reviewer"]["kind"])
        self.assertEqual(original, self.store.read()["reviewer"])
        self.assertEqual("pi", Workflow.discover(self.repo, herdr=self.fake).create("New task", "task-after-switch").read()["reviewer"]["kind"])
        self.assertEqual(0o600, defaults_override_path().stat().st_mode & 0o777)
        with redirect_stdout(io.StringIO()):
            main(["switch", "--role", "reviewer", "--reset"])
        self.assertEqual(original["kind"], load_config()["agents"]["reviewer"]["kind"])


class PiCatalogTests(unittest.TestCase):
    def test_exact_provider_model_and_thinking_validation(self):
        from herdr_flow import pi_models, validate_agent_selection, _catalog
        output = "provider  model  context  max-out  thinking  images\nexample  think  200K  32K  yes  no\nexample  tiny  8K  2K  no  no\n"
        with patch("herdr_flow.run", return_value=subprocess.CompletedProcess([], 0, output, "")):
            self.assertEqual({"example/think": True, "example/tiny": False}, pi_models())
            self.assertIn("example/think", _catalog("pi"))
            validate_agent_selection("pi", "example/think", "high")
            validate_agent_selection("pi", "example/tiny", "off")
            with self.assertRaisesRegex(FlowError, "does not advertise thinking"):
                validate_agent_selection("pi", "example/tiny", "high")
            with self.assertRaisesRegex(FlowError, "not available"):
                validate_agent_selection("pi", "think")
            with self.assertRaisesRegex(FlowError, "does not support thinking"):
                validate_agent_selection("pi", "example/think", "ultra")

    def test_missing_catalog_fails_closed(self):
        from herdr_flow import pi_models
        with patch("herdr_flow.run", return_value=subprocess.CompletedProcess([], 0, "invalid", "")):
            with self.assertRaisesRegex(FlowError, "no parseable"):
                pi_models()


if __name__ == "__main__":
    unittest.main(verbosity=2)
