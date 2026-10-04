#!/usr/bin/env python3
"""Herdr Flow: persistent, reuse-first multi-agent development workflows.

The implementation intentionally uses only Python's standard library. Herdr remains
responsible for visible terminal sessions; this controller owns task files, state
transitions, idempotency, and safe session resolution.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

PLUGIN_ID = "hessam.herdr-flow"
SCHEMA_VERSION = 1
TASK_RE = re.compile(r"^task-[a-z0-9][a-z0-9-]{0,22}$")
AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
AGENT_KINDS = {"opencode", "codex", "pi"}
VARIANTS = {"minimal", "low", "medium", "high", "xhigh", "max", "ultra", "off"}
PI_THINKING = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
SESSION_ROLES = ("worker", "fixer", "escalation_fixer", "reviewer")
CONFIGURABLE_ROLES = ("coordinator", "worker", "fixer", "escalation_fixer", "reviewer")
BRANCH_TYPES = ("feat", "fix", "refactor", "docs", "test", "chore")
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
CONTEXT_FILE = ".herdr-flow-context.md"
MAX_INSTRUCTION_BYTES = 256 * 1024
MAX_BRIEF_BYTES = 64 * 1024
BRIEF_PLACEHOLDER = "<!-- Replace with the original task request before launching a managed coordinator. -->"
PHASES = {
    "PLANNED",
    "IMPLEMENTING",
    "IMPLEMENTATION_DONE",
    "REVIEWING",
    "REVIEW_FAILED",
    "FIXING",
    "FINAL_REVIEW",
    "FINAL_REVIEW_FAILED",
    "DONE",
    "BLOCKED",
    "WAITING_FOR_USER",
    "FAILED",
}
SECRET_PATTERNS = [
    re.compile(r"(?i)\b(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9_./+\-=]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
TEMPLATES = {
    "brief.md": f"# Original Request\n\n{BRIEF_PLACEHOLDER}\n",
    "handoff.md": """# Implementation Handoff\n\n## Objective\n\n<!-- Claude coordinator: replace this placeholder. -->\n\n## Background\n\n## Requirements\n\n- [ ] Define the requested behavior.\n\n## Acceptance Criteria\n\n- [ ] Define observable acceptance criteria.\n\n## Definition of Done\n\n- [ ] Implementation is complete.\n- [ ] Relevant tests pass.\n- [ ] Lint, type, and build checks pass where applicable.\n- [ ] No credentials, debug artifacts, or unrelated changes were introduced.\n\n## Constraints\n\n- Do not merge, force-push, delete branches/worktrees, or discard unrelated work.\n- Keep secrets out of workflow files and prompts.\n\n## Expected Validation\n\n<!-- List project-specific commands. -->\n""",
    "implementation.md": """# Implementation Report\n\n## Status\n\nPENDING\n\n## Summary\n\n## Files Changed\n\n## Validation\n\n## Decisions and Assumptions\n\n## Remaining Warnings\n""",
    "review.md": """# Independent Review\n\n## Status\n\nPENDING\n\n## Executive Summary\n\n## Handoff Coverage\n\nFor every requirement, acceptance criterion, and Definition of Done item, record PASS, FAIL, or NOT VERIFIED with evidence.\n\n## Worktree and Diff Reviewed\n\n## Validation Evidence\n\nList commands run, results observed, and any checks that could not be completed.\n\n## Findings\n\nList findings by severity with file/line evidence, impact, and required correction. Write `None` only after completing the full review.\n\n## Regression, Security, Performance, and Maintainability\n\n## Gaps and Unverified Assumptions\n\n## Commit and Merge Readiness\n\nState whether the branch is ready to commit and merge without additional changes.\n\n## Reviewer Recommendation\n\nPENDING\n""",
    "decisions.md": """# Decisions\n\nRecord durable task decisions here. Never include credentials or secret values.\n""",
    "final-review.md": """# Final Review\n\n## Status\n\nPENDING\n\n## Requirements and Handoff Coverage\n\n## Acceptance Criteria\n\n## Definition of Done\n\n## Independent Review Findings Resolved\n\n## Validation and Regression Evidence\n\n## Security, Performance, and Maintainability\n\n## Gaps and Unverified Assumptions\n\n## Commit and Merge Readiness\n\n## Final Decision\n\nPENDING\n""",
}


class FlowError(RuntimeError):
    pass


class HerdrError(FlowError):
    def __init__(self, code: str, message: str, payload: dict[str, Any] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.payload = payload or {}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    atomic_write(path, json.dumps(data, indent=2, sort_keys=False) + "\n", mode)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        if default is not None:
            return copy.deepcopy(default)
        raise FlowError(f"Missing required file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FlowError(f"Invalid JSON in {path}: {exc}") from exc


def run(argv: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise FlowError(f"Command failed ({result.returncode}): {' '.join(argv)}\n{detail}")
    return result


def git_root(cwd: Path) -> Path:
    result = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    return Path(result.stdout.strip()).resolve()


def validate_agent_selection(kind: str, model: str, variant: str | None = None) -> None:
    if kind not in AGENT_KINDS:
        raise FlowError(f"Unsupported workflow agent kind: {kind}; choose opencode, codex, or pi")
    if not model or any(char.isspace() for char in model):
        raise FlowError("Agent model must be an exact installed model id without whitespace")
    if variant and variant not in VARIANTS:
        raise FlowError(f"Unsupported effort/variant: {variant}; choose one of {', '.join(sorted(VARIANTS))}")
    if kind == "opencode":
        provider = model.split("/", 1)[0] if "/" in model else ""
        if not provider:
            raise FlowError("OpenCode models must include their provider prefix, for example provider/model")
        result = run(["opencode", "models", provider], check=False)
        if result.returncode != 0 or model not in set(result.stdout.splitlines()):
            raise FlowError(f"OpenCode model is not available in the local catalog: {model}")
        if variant:
            supported = opencode_variants(model)
            if variant not in supported:
                raise FlowError(f"OpenCode model {model} does not advertise variant {variant}; available: {', '.join(sorted(supported)) or '(default only)'}")
        return
    if kind == "pi":
        if variant and variant not in PI_THINKING:
            raise FlowError(f"Pi does not support thinking level {variant}")
        models = pi_models()
        if model not in models:
            raise FlowError(f"Pi model is not available in the local catalog: {model}; use provider/model")
        if variant and variant != "off" and not models[model]:
            raise FlowError(f"Pi model {model} does not advertise thinking; use off or default")
        return
    if variant == "off":
        raise FlowError("Codex does not advertise reasoning effort off")
    result = run(["codex", "debug", "models"], check=False)
    if result.returncode != 0:
        raise FlowError(f"Could not inspect the Codex model catalog: {(result.stderr or result.stdout).strip()}")
    try:
        models = json.loads(result.stdout).get("models", [])
    except json.JSONDecodeError as exc:
        raise FlowError("Codex returned an invalid model catalog") from exc
    selected = next((item for item in models if item.get("slug") == model), None)
    if not selected:
        raise FlowError(f"Codex model is not available in the local catalog: {model}")
    if variant:
        supported = {item.get("effort") for item in selected.get("supported_reasoning_levels", [])}
        if variant not in supported:
            raise FlowError(f"Codex model {model} does not advertise reasoning effort {variant}; available: {', '.join(sorted(supported))}")


def plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def plugin_state_dir() -> Path:
    raw = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if raw:
        return Path(raw).resolve()
    xdg = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return xdg / "herdr" / "plugins" / PLUGIN_ID


def global_config_path() -> Path:
    raw = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if raw:
        candidate = Path(raw) / "config.json"
        if candidate.exists():
            return candidate
    return plugin_root() / "config.json"


DEFAULT_ROLES = {"coordinator", "worker", "reviewer", "escalation"}


def defaults_override_path() -> Path:
    return plugin_state_dir() / "defaults.json"


def read_defaults_override() -> dict[str, Any]:
    path = defaults_override_path()
    if not path.exists():
        return {"schema_version": 1, "roles": {}}
    data = read_json(path)
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or not isinstance(data.get("roles"), dict)
            or set(data) != {"schema_version", "roles"}
            or set(data["roles"]) - DEFAULT_ROLES):
        raise FlowError(f"Invalid local role defaults in {path}")
    for role, choice in data["roles"].items():
        if (not isinstance(choice, dict) or set(choice) != {"kind", "model", "variant"}
                or not isinstance(choice["kind"], str)
                or choice["kind"] not in ({"claude", *AGENT_KINDS} if role == "coordinator" else AGENT_KINDS)
                or (choice["kind"] != "claude" and (not isinstance(choice["model"], str) or not choice["model"] or any(c.isspace() for c in choice["model"])))
                or (choice["model"] is not None and not isinstance(choice["model"], str))
                or (choice["kind"] == "claude" and choice["model"] is not None)
                or (choice["variant"] is not None and (not isinstance(choice["variant"], str) or choice["variant"] not in VARIANTS))
                or (choice["kind"] == "claude" and choice["variant"] not in {None, "low", "medium", "high", "xhigh", "max"})
                or (choice["kind"] == "pi" and choice["variant"] not in {None, *PI_THINKING})):
            raise FlowError(f"Invalid local {role} default in {path}")
    return data


def load_config() -> dict[str, Any]:
    config = read_json(global_config_path())
    local = read_defaults_override()["roles"]
    for role, choice in local.items():
        if role == "escalation":
            config["repair"]["escalation"]["agent"] = choice.copy()
        else:
            config["agents"][role] = choice.copy()
    if config.get("schema_version") != SCHEMA_VERSION:
        raise FlowError(f"Unsupported config schema in {global_config_path()}")
    coordinator = config.get("agents", {}).get("coordinator", {})
    kind, model, variant = coordinator.get("kind"), coordinator.get("model"), coordinator.get("variant")
    if kind not in {"claude", *AGENT_KINDS}:
        raise FlowError("agents.coordinator.kind must be claude, opencode, codex, or pi")
    if kind != "claude" and (not isinstance(model, str) or not model or any(char.isspace() for char in model)):
        raise FlowError("agents.coordinator.model must be an exact installed model ID for a non-Claude default")
    if variant and (variant not in VARIANTS or (kind == "claude" and variant not in {"low", "medium", "high", "xhigh", "max"})):
        raise FlowError("agents.coordinator.variant is unsupported")
    reviewer = config.get("agents", {}).get("reviewer", {})
    if reviewer.get("kind") not in AGENT_KINDS:
        raise FlowError("agents.reviewer.kind must be opencode, codex, or pi")
    if not isinstance(reviewer.get("model"), str) or not reviewer["model"] or any(c.isspace() for c in reviewer["model"]):
        raise FlowError("agents.reviewer.model must be an exact installed model ID")
    if reviewer.get("variant") and reviewer["variant"] not in VARIANTS:
        raise FlowError("agents.reviewer.variant is unsupported")
    escalation = config.get("repair", {}).get("escalation", {})
    if escalation.get("enabled"):
        after = escalation.get("after_failures")
        if not isinstance(after, int) or after < 0:
            raise FlowError("repair.escalation.after_failures must be a non-negative integer")
        agent = escalation.get("agent", {})
        if agent.get("kind") not in AGENT_KINDS or not agent.get("model"):
            raise FlowError("repair.escalation.agent must define an opencode/codex/pi kind and exact model")
        if agent.get("variant") and agent["variant"] not in VARIANTS:
            raise FlowError("repair.escalation.agent.variant is unsupported")
    return config


def context_cwd() -> Path:
    raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON")
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
        for key in ("focused_pane_cwd", "pane_cwd", "cwd"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return Path(value).expanduser().resolve()
        worktree = data.get("worktree")
        if isinstance(worktree, dict) and isinstance(worktree.get("path"), str):
            return Path(worktree["path"]).expanduser().resolve()
    active = os.environ.get("HERDR_ACTIVE_PANE_CWD")
    return Path(active).expanduser().resolve() if active else Path.cwd().resolve()


def workflow_root_from(start: Path, *, create: bool = False) -> tuple[Path, Path]:
    marker = find_marker(start)
    if marker:
        payload = read_json(marker)
        workflow = Path(payload["workflow_root"]).resolve()
        source = Path(payload["source_root"]).resolve()
        return workflow, source
    source = git_root(start)
    workflow = source / ".ai" / "workflow"
    if create:
        workflow.mkdir(parents=True, exist_ok=True)
    if not workflow.exists():
        raise FlowError(f"Workflow is not initialized in {source}; run `herdr-flow init` or `herdr-flow create`")
    return workflow.resolve(), source


def find_marker(start: Path) -> Path | None:
    current = start if start.is_dir() else start.parent
    for candidate in (current, *current.parents):
        marker = candidate / ".herdr-flow-task.json"
        if marker.is_file():
            return marker
    return None


def append_git_exclude(source_root: Path) -> None:
    path = Path(run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=source_root).stdout.strip())
    if not path.is_absolute():
        path = source_root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    begin = "# BEGIN herdr-flow"
    end = "# END herdr-flow"
    block = f"{begin}\n.ai/workflow\n.herdr-flow-task.json\n.herdr-flow-context.md\n{end}\n"
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if begin not in current:
        with path.open("a", encoding="utf-8") as handle:
            if current and not current.endswith("\n"):
                handle.write("\n")
            handle.write(block)
    else:
        updated = current
        if ".ai/workflow\n" not in updated:
            updated = updated.replace(begin + "\n", begin + "\n.ai/workflow\n", 1)
        if ".herdr-flow-context.md" not in updated:
            updated = updated.replace(begin + "\n", begin + "\n.herdr-flow-context.md\n", 1)
        if updated != current:
            atomic_write(path, updated, path.stat().st_mode & 0o777)


def detect_secret(text: str) -> str | None:
    for pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)[:48]
    return None


def validate_report(path: Path, expected: str) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if detect_secret(text):
        raise FlowError(f"Potential secret detected in {path}; remove it before updating workflow state")
    match = re.search(r"(?im)^## Status\s*\n+\s*(PASS|FAIL)\s*$", text)
    if not match or match.group(1).upper() != expected:
        raise FlowError(f"{path} must set `## Status` to exactly {expected}")


def validate_implementation(path: Path) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if detect_secret(text):
        raise FlowError(f"Potential secret detected in {path}; remove it before updating workflow state")
    match = re.search(r"(?im)^## Status\s*\n+\s*([A-Z_-]+)", text)
    if not match or match.group(1).upper() not in {"READY", "COMPLETE", "COMPLETED", "DONE"}:
        raise FlowError(f"{path} must set `## Status` to READY, COMPLETE, COMPLETED, or DONE")


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class TaskStore:
    def __init__(self, workflow_root: Path, task_id: str):
        if not TASK_RE.fullmatch(task_id):
            raise FlowError(f"Invalid task id: {task_id}")
        self.workflow_root = workflow_root
        self.task_id = task_id
        self.root = workflow_root / task_id
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / ".lock"

    @contextlib.contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        with file_lock(self.lock_path):
            state = read_json(self.state_path)
            yield state
            state["updated_at"] = utc_now()
            self._validate(state)
            write_json(self.state_path, state)

    def read(self) -> dict[str, Any]:
        state = read_json(self.state_path)
        self._validate(state)
        return state

    def event(self, state: dict[str, Any], event: str, role: str, message: str) -> None:
        entry = {
            "timestamp": utc_now(),
            "task_id": self.task_id,
            "role": role,
            "iteration": state.get("iteration", 0),
            "state": state.get("phase"),
            "event": event,
            "message": message.replace("\n", " ")[:500],
        }
        with (self.root / "events.log").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")

    @staticmethod
    def _validate(state: dict[str, Any]) -> None:
        if state.get("schema_version") != SCHEMA_VERSION:
            raise FlowError("Unsupported task state schema")
        if state.get("phase") not in PHASES:
            raise FlowError(f"Invalid task phase: {state.get('phase')}")
        if not TASK_RE.fullmatch(str(state.get("task_id", ""))):
            raise FlowError("Invalid task id in state")
        for role in SESSION_ROLES:
            record = state.get(role)
            if record is None:
                continue
            name = record.get("name")
            if name and not AGENT_NAME_RE.fullmatch(name):
                raise FlowError(f"Invalid {role} agent name: {name}")


class Registry:
    def __init__(self, root: Path | None = None):
        self.root = root or plugin_state_dir()
        self.path = self.root / "registry.json"
        self.lock = self.root / ".registry.lock"

    def update(self, state: dict[str, Any], state_path: Path) -> None:
        with file_lock(self.lock):
            data = read_json(self.path, {"schema_version": 1, "tasks": {}})
            item = {"state_path": str(state_path.resolve()), "coordinator_pane": state.get("coordinator", {}).get("pane")}
            for role in SESSION_ROLES:
                record = state.get(role) or {}
                item[f"{role}_name"] = record.get("name")
                item[f"{role}_pane"] = record.get("pane")
            # Task ids repeat across repositories; the absolute state path is the identity.
            key = str(state_path.resolve())
            legacy = data["tasks"].get(state["task_id"])
            if legacy and legacy.get("state_path") == key:
                del data["tasks"][state["task_id"]]
            data["tasks"][key] = item
            write_json(self.path, data)

    def find_by_pane(self, pane_id: str) -> list[tuple[TaskStore, str]]:
        with file_lock(self.lock):
            data = read_json(self.path, {"schema_version": 1, "tasks": {}})
        matches: list[tuple[TaskStore, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in data.get("tasks", {}).values():
            state_path = Path(item["state_path"])
            task_id = state_path.parent.name
            store = TaskStore(state_path.parent.parent, task_id)
            if not state_path.exists():
                continue
            for role in ("coordinator", *SESSION_ROLES):
                pane = item.get("coordinator_pane") if role == "coordinator" else item.get(f"{role}_pane")
                identity = (str(state_path), role)
                if pane == pane_id and identity not in seen:
                    seen.add(identity)
                    matches.append((store, role))
        return matches


class HerdrAdapter:
    def __init__(self, binary: str | None = None):
        self.binary = binary or os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or "herdr"

    @staticmethod
    def require_context() -> None:
        if os.environ.get("HERDR_ENV") != "1":
            raise FlowError(
                "Live agent operations require a Herdr-managed pane (HERDR_ENV=1). "
                "Open this project in Herdr and run the command there."
            )

    def call(self, args: list[str]) -> dict[str, Any]:
        self.require_context()
        result = subprocess.run([self.binary, *args], text=True, capture_output=True)
        payload = self._payload(result.stdout) or self._payload(result.stderr)
        if result.returncode != 0 or (payload and "error" in payload):
            error = (payload or {}).get("error", {})
            code = str(error.get("code") or f"herdr_exit_{result.returncode}")
            message = str(error.get("message") or result.stderr.strip() or result.stdout.strip())
            raise HerdrError(code, message, payload)
        if not payload:
            raise HerdrError("invalid_response", f"Herdr returned no JSON for: {' '.join(args)}")
        return payload

    @staticmethod
    def _payload(text: str) -> dict[str, Any] | None:
        text = text.strip()
        if not text:
            return None
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            for line in reversed(text.splitlines()):
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        return value
                except json.JSONDecodeError:
                    continue
        return None

    def agents(self) -> list[dict[str, Any]]:
        return self.call(["agent", "list"])["result"]["agents"]

    def workspaces(self) -> list[dict[str, Any]]:
        return self.call(["workspace", "list"])["result"]["workspaces"]

    def agent(self, target: str) -> dict[str, Any] | None:
        try:
            return self.call(["agent", "get", target])["result"]["agent"]
        except HerdrError as exc:
            if exc.code in {"agent_not_found", "agent_name_not_found", "pane_not_found"}:
                return None
            raise

    def read_agent(self, target: str, lines: int = 100) -> str:
        # Unlike agent.get, Herdr's agent.read writes terminal text directly to stdout.
        self.require_context()
        args = ["agent", "read", target, "--source", "recent-unwrapped", "--lines", str(lines)]
        result = subprocess.run([self.binary, *args], text=True, capture_output=True)
        if result.returncode != 0:
            payload = self._payload(result.stderr) or self._payload(result.stdout)
            error = (payload or {}).get("error", {})
            code = str(error.get("code") or f"herdr_exit_{result.returncode}")
            message = str(error.get("message") or result.stderr.strip() or result.stdout.strip())
            raise HerdrError(code, message, payload)
        return result.stdout

    def create_worktree(self, source: Path, branch: str, base: str, label: str) -> dict[str, Any]:
        return self.call([
            "worktree", "create", "--cwd", str(source), "--branch", branch,
            "--base", base, "--label", label, "--no-focus",
        ])["result"]

    def open_worktree(self, source: Path, *, path: str | None = None, branch: str | None = None, label: str) -> dict[str, Any]:
        args = ["worktree", "open", "--cwd", str(source)]
        args += ["--path", path] if path else ["--branch", str(branch)]
        args += ["--label", label, "--no-focus"]
        return self.call(args)["result"]

    def create_tab(self, workspace: str, cwd: Path, label: str) -> dict[str, Any]:
        return self.call([
            "tab", "create", "--workspace", workspace, "--cwd", str(cwd),
            "--label", label, "--no-focus",
        ])["result"]

    def split(self, pane: str, cwd: Path) -> dict[str, Any]:
        return self.call([
            "pane", "split", pane, "--direction", "right", "--ratio", "0.5",
            "--cwd", str(cwd), "--no-focus",
        ])["result"]["pane"]

    def start(self, name: str, kind: str, pane: str, args: list[str]) -> dict[str, Any]:
        command = ["agent", "start", name, "--kind", kind, "--pane", pane, "--timeout", "120000"]
        if args:
            command += ["--", *args]
        return self.call(command)["result"]["agent"]

    def prompt(self, target: str, text: str) -> dict[str, Any]:
        return self.call(["agent", "prompt", target, text])["result"]["agent"]

    def focus(self, target: str) -> dict[str, Any]:
        return self.call(["agent", "focus", target])["result"]["agent"]

    def remove_worktree(self, workspace: str) -> dict[str, Any]:
        return self.call(["worktree", "remove", "--workspace", workspace])["result"]

    def panes(self) -> list[dict[str, Any]]:
        return self.call(["pane", "list"])["result"]["panes"]

    def status_popup(self) -> None:
        self.call([
            "plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "status",
            "--cwd", str(context_cwd()),
        ])


class Workflow:
    def __init__(self, source_root: Path, workflow_root: Path, config: dict[str, Any], herdr: HerdrAdapter | None = None):
        self.source_root = source_root.resolve()
        self.workflow_root = workflow_root.resolve()
        self.config = config
        self.herdr = herdr or HerdrAdapter()
        self.registry = Registry()

    @classmethod
    def discover(cls, start: Path, *, create: bool = False, herdr: HerdrAdapter | None = None) -> "Workflow":
        workflow_root, source_root = workflow_root_from(start, create=create)
        return cls(source_root, workflow_root, load_config(), herdr)

    def init(self) -> None:
        self.workflow_root.mkdir(parents=True, exist_ok=True)
        append_git_exclude(self.source_root)
        project_config = self.workflow_root / "config.json"
        if not project_config.exists():
            snapshot = {
                "schema_version": 1,
                "source_root": str(self.source_root),
                "worker": self.config["agents"]["worker"],
                "reviewer": self.config["agents"]["reviewer"],
                "repair": self.config["repair"],
                "git": self.config["git"],
            }
            write_json(project_config, snapshot)
        index = self.workflow_root / "index.json"
        if not index.exists():
            write_json(index, {"schema_version": 1, "next_task": 1, "current_task": None})

    def create(
        self,
        title: str,
        task_id: str | None = None,
        base_ref: str | None = None,
        worker: dict[str, Any] | None = None,
        reviewer: dict[str, Any] | None = None,
        coordinator: dict[str, Any] | None = None,
        fixer: dict[str, Any] | None = None,
        escalation_fixer: dict[str, Any] | None = None,
        escalate_after: int | None = None,
        disable_escalation: bool = False,
        plan_only: bool = False,
        branch_type: str | None = None,
    ) -> TaskStore:
        self.init()
        worker_config = self._selection(self.config["agents"]["worker"], worker)
        reviewer_config = self._selection(self.config["agents"]["reviewer"], reviewer)
        coordinator_config = self._selection(self.config["agents"]["coordinator"], coordinator)
        coordinator_kind = coordinator_config["kind"]
        if coordinator_kind not in {"claude", *AGENT_KINDS}:
            raise FlowError(f"Unsupported coordinator kind: {coordinator_kind}")
        if coordinator_kind != "claude":
            validate_agent_selection(coordinator_kind, coordinator_config.get("model"), coordinator_config.get("variant"))
        elif coordinator_config.get("variant") not in {None, "low", "medium", "high", "xhigh", "max"}:
            raise FlowError("Unsupported Claude coordinator effort")
        escalation_defaults = self.config["repair"]["escalation"]
        escalation_config = self._selection(escalation_defaults["agent"], escalation_fixer)
        escalation_enabled = bool(escalation_defaults.get("enabled")) and not disable_escalation
        escalation_after = escalation_defaults["after_failures"] if escalate_after is None else escalate_after
        if escalation_after < 0:
            raise FlowError("Escalation threshold must be zero or greater")
        validate_agent_selection(worker_config["kind"], worker_config["model"], worker_config.get("variant"))
        validate_agent_selection(reviewer_config["kind"], reviewer_config["model"], reviewer_config.get("variant"))
        if fixer:
            fixer_config = self._selection(worker_config, fixer)
            validate_agent_selection(fixer_config["kind"], fixer_config["model"], fixer_config.get("variant"))
        else:
            fixer_config = None
        if escalation_enabled:
            validate_agent_selection(escalation_config["kind"], escalation_config["model"], escalation_config.get("variant"))
        with file_lock(self.workflow_root / ".index.lock"):
            index_path = self.workflow_root / "index.json"
            index = read_json(index_path)
            number = int(index.get("next_task", 1))
            task_id = task_id or f"task-{number:03d}"
            if not TASK_RE.fullmatch(task_id):
                raise FlowError("Task ids must match task-<lowercase letters, digits, hyphens> and fit Herdr agent names")
            store = TaskStore(self.workflow_root, task_id)
            if store.root.exists():
                raise FlowError(f"Task already exists: {task_id}")
            branch = self._task_branch(title, task_id, branch_type)
            store.root.mkdir(parents=True)
            for name, content in TEMPLATES.items():
                atomic_write(store.root / name, content, 0o600)
            worker_name = self._agent_name("worker", task_id)
            fixer_name = self._agent_name("fixer", task_id)
            escalation_name = self._agent_name("escalator", task_id)
            reviewer_name = self._agent_name("reviewer", task_id)
            now = utc_now()
            state = {
                "schema_version": SCHEMA_VERSION,
                "task_id": task_id,
                "title": title.strip() or task_id,
                "phase": "PLANNED",
                "planning": {"auto_start": not plan_only},
                "iteration": 0,
                "created_at": now,
                "updated_at": now,
                "coordinator": {
                    **self._agent_state(self._agent_name("coordinator", task_id), coordinator_config),
                    "managed": False,
                },
                "project": {
                    "source_root": str(self.source_root),
                    "workflow_root": str(self.workflow_root),
                    "base_ref": base_ref or self.config["git"]["base_ref"],
                    "branch": branch,
                    "worktree_path": None,
                    "workspace_id": None,
                },
                "worker": self._agent_state(worker_name, worker_config),
                "fixer": self._agent_state(fixer_name, fixer_config) if fixer_config else None,
                "escalation_fixer": self._agent_state(escalation_name, escalation_config) if escalation_enabled else None,
                "reviewer": self._agent_state(reviewer_name, reviewer_config),
                "repair": {
                    "failure_count": 0,
                    "fix_attempts": 0,
                    "active_role": "worker",
                    "base_role": "fixer" if fixer_config else "worker",
                    "escalated": False,
                    "escalation": {
                        "enabled": escalation_enabled,
                        "after_failures": escalation_after,
                    },
                },
                "review": {"status": "pending", "attempt": 0},
                "final_review": {"status": "pending", "attempt": 0},
                "user_interaction": {"required": False, "blocked_agent": None, "return_phase": None},
                "prompts": {role: None for role in (*SESSION_ROLES, "coordinator")},
                "automation": {"enabled": False, "attention": None},
                "warnings": [],
            }
            write_json(store.state_path, state)
            store.event(state, "TASK_CREATED", "coordinator", title)
            index["next_task"] = max(number + 1, self._task_number(task_id) + 1)
            index["current_task"] = task_id
            write_json(index_path, index)
            self.registry.update(state, store.state_path)
            return store

    def resolve_task(self, task_id: str | None) -> TaskStore:
        if not task_id:
            marker = find_marker(context_cwd())
            if marker:
                task_id = str(read_json(marker)["task_id"])
            else:
                index = read_json(self.workflow_root / "index.json")
                task_id = index.get("current_task")
        if not task_id:
            raise FlowError("No current task; pass --task or create a task")
        store = TaskStore(self.workflow_root, task_id)
        if not store.state_path.exists():
            raise FlowError(f"Unknown task: {task_id}")
        return store

    def configure_role(
        self,
        store: TaskStore,
        role: str,
        *,
        kind: str | None = None,
        model: str | None = None,
        variant: str | None = None,
        variant_set: bool = False,
    ) -> dict[str, Any]:
        if role not in CONFIGURABLE_ROLES:
            raise FlowError(f"Role must be one of: {', '.join(CONFIGURABLE_ROLES)}")
        with store.locked() as state:
            self._hydrate_repair_state(state)
            record = state.get(role)
            if record is None:
                if role == "fixer":
                    if state["repair"]["fix_attempts"]:
                        raise FlowError("A dedicated fixer cannot be introduced after repair work has started")
                    default = state["worker"]
                    name = self._agent_name("fixer", state["task_id"])
                elif role == "escalation_fixer":
                    default = self.config["repair"]["escalation"]["agent"]
                    name = self._agent_name("escalator", state["task_id"])
                else:
                    raise FlowError(f"Missing required role state: {role}")
                record = self._agent_state(name, default)
                state[role] = record
            proposed = {
                "kind": kind or record["kind"],
                "model": model or record.get("model"),
                "variant": variant if variant_set else record.get("variant"),
            }
            if role == "coordinator" and proposed["kind"] == "claude":
                if proposed.get("variant") not in {None, "low", "medium", "high", "xhigh", "max"}:
                    raise FlowError("Unsupported Claude coordinator effort")
            else:
                if not proposed["model"]:
                    raise FlowError(f"{role} model is required")
                validate_agent_selection(proposed["kind"], proposed["model"], proposed.get("variant"))
            current = {key: record.get(key) for key in ("kind", "model", "variant")}
            if proposed == current:
                return state
            if role == "escalation_fixer" and not record.get("pane") and self._live_agent(record.get("name")):
                raise FlowError("Escalation fixer is already live after incomplete startup; inspect and adopt-startup instead of reconfiguring it")
            if record.get("session") or record.get("pane") or record.get("status") not in {"not_started", "needs_fix", None}:
                raise FlowError(
                    f"{role} selection is immutable after its persistent session starts. "
                    "Create a new task to use a different agent/model, or recover the recorded selection."
                )
            record.update(proposed)
            if role == "fixer":
                previous_role = state["repair"].get("active_role", "worker")
                state["repair"]["base_role"] = "fixer"
                if state["phase"] in {"REVIEW_FAILED", "FINAL_REVIEW_FAILED"}:
                    previous = state.get(previous_role)
                    if previous and previous.get("status") == "needs_fix":
                        previous["status"] = "waiting"
                    record["status"] = "needs_fix"
                    state["repair"]["active_role"] = "fixer"
            elif role == "escalation_fixer":
                state["repair"]["escalation"]["enabled"] = True
                if (
                    state["phase"] in {"REVIEW_FAILED", "FINAL_REVIEW_FAILED"}
                    and int(state["repair"].get("failure_count", 0)) > int(state["repair"]["escalation"].get("after_failures", 3))
                ):
                    previous_role = state["repair"].get("active_role", "worker")
                    selected_role = self._repair_role(state)
                    previous = state.get(previous_role)
                    if previous_role != selected_role and previous and previous.get("status") == "needs_fix":
                        previous["status"] = "waiting"
                    state[selected_role]["status"] = "needs_fix"
            store.event(
                state,
                "AGENT_CONFIGURATION_SET",
                role,
                f"{proposed['kind']} {proposed['model']} variant={proposed.get('variant') or 'default'}",
            )
            self.registry.update(state, store.state_path)
            return state

    def configure_escalation(
        self,
        store: TaskStore,
        *,
        after_failures: int | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        with store.locked() as state:
            self._hydrate_repair_state(state)
            policy = state["repair"]["escalation"]
            record = state.get("escalation_fixer")
            if record and (record.get("session") or record.get("pane") or record.get("status") not in {"not_started", "needs_fix", None}):
                raise FlowError("Escalation policy is immutable after the escalation fixer starts")
            if after_failures is not None:
                if after_failures < 0:
                    raise FlowError("Escalation threshold must be zero or greater")
                policy["after_failures"] = after_failures
            if enabled is not None:
                policy["enabled"] = enabled
            if state["phase"] in {"REVIEW_FAILED", "FINAL_REVIEW_FAILED"}:
                previous_role = state["repair"].get("active_role", "worker")
                selected_role = self._repair_role(state)
                previous = state.get(previous_role)
                if previous_role != selected_role and previous and previous.get("status") == "needs_fix":
                    previous["status"] = "waiting"
                state[selected_role]["status"] = "needs_fix"
            store.event(
                state,
                "ESCALATION_CONFIGURATION_SET",
                "coordinator",
                f"enabled={policy['enabled']} after_failures={policy['after_failures']}",
            )
            self.registry.update(state, store.state_path)
            return state

    def adopt_startup(self, store: TaskStore, *, pane: str, model: str, variant: str, yes: bool = False) -> dict[str, Any]:
        """Adopt a named Codex escalation session stalled by first-run onboarding."""
        HerdrAdapter.require_context()
        if not yes:
            raise FlowError("Inspect the live Codex pane and confirm adoption with --yes")
        if not pane or not model or variant != "high":
            raise FlowError("Specify the existing pane, exact model, and verified high reasoning level")
        validate_agent_selection("codex", model, variant)
        with file_lock(store.root / ".drive.lock"):
            with store.locked() as state:
                self._hydrate_repair_state(state)
                role = "escalation_fixer"
                record = state.get(role)
                if state["phase"] not in {"REVIEW_FAILED", "FINAL_REVIEW_FAILED"} or state["repair"].get("active_role") != role or not record:
                    raise FlowError("Only a selected, stalled escalation fixer can be adopted")
                if record.get("pane") or record.get("session") or record.get("terminal_id") or state["prompts"].get(role):
                    raise FlowError("Escalation fixer already has recorded identity or prompt; inspect before recovery")
                marker = Path(state["project"].get("worktree_path") or "") / ".herdr-flow-task.json"
                if not marker.is_file():
                    raise FlowError("Task worktree ownership marker is missing")
                owner = read_json(marker)
                if owner.get("task_id") != store.task_id or owner.get("source_root") != str(self.source_root):
                    raise FlowError("Task worktree ownership marker does not match")
                live = self._live_agent(record["name"])
                if not live or live.get("name") != record["name"] or live.get("agent") != "codex" or live.get("pane_id") != pane or live.get("workspace_id") != state["project"]["workspace_id"] or live.get("agent_status") not in {"idle", "done"} or not live.get("interactive_ready") or not live.get("terminal_id"):
                    raise FlowError("Named Codex session is not idle in the confirmed task pane; no new session was started")
                if Path(live.get("cwd") or "").resolve() != Path(state["project"]["worktree_path"]).resolve():
                    raise FlowError("Codex is not running in the task worktree")
                if model != "gpt-6-sol":
                    raise FlowError("This recovery supports only the confirmed GPT-6-Sol high selection")
                screen = self.herdr.read_agent(record["name"], lines=80)
                displayed = re.findall(r"(?i)\bGPT[- ]6[- ]Sol\s+(minimal|low|medium|high|xhigh|max|ultra)\b", screen)
                if not displayed or displayed[-1].lower() != "high":
                    raise FlowError("Codex screen does not confirm GPT-6-Sol high as its latest displayed model; change /model in the same pane first")
                record.update({"model": model, "variant": variant, "pane": pane, "workspace": live["workspace_id"],
                               "terminal_id": live["terminal_id"], "session": live.get("agent_session"),
                               "status": live["agent_status"], "last_seen_at": utc_now()})
                state["automation"]["attention"] = None
                store.event(state, "STARTUP_SESSION_ADOPTED", role, f"Verified existing Codex pane {pane}; {model} {variant}; no prompt sent")
                self.registry.update(state, store.state_path)
                return state

    def implement(self, store: TaskStore, *, force_resend: bool = False) -> dict[str, Any]:
        HerdrAdapter.require_context()
        with store.locked() as state:
            phase = state["phase"]
            if phase == "WAITING_FOR_USER":
                raise FlowError(f"{state['user_interaction']['blocked_agent']} is waiting for the user; focus that same session")
            if phase not in {"PLANNED", "IMPLEMENTING", "REVIEW_FAILED", "FIXING", "FINAL_REVIEW_FAILED"}:
                raise FlowError(f"Cannot start/fix worker while task is {phase}")
            self._hydrate_repair_state(state)
            worktree, root_pane = self._ensure_worktree(state)
            is_fix = phase in {"REVIEW_FAILED", "FIXING", "FINAL_REVIEW_FAILED"}
            role = self._repair_role(state) if is_fix else "worker"
            if role == "worker":
                agent, recovered = self._ensure_agent(state, role, root_pane)
            else:
                agent, recovered = self._ensure_repair_agent(state, role, worktree, root_pane)
            target_phase = "FIXING" if is_fix else "IMPLEMENTING"
            if state["phase"] != target_phase:
                state["phase"] = target_phase
                state["iteration"] = max(1, int(state.get("iteration", 0)) + (1 if is_fix else 0))
                if is_fix:
                    state["repair"]["fix_attempts"] = int(state["repair"].get("fix_attempts", 0)) + 1
                    state["repair"]["active_role"] = role
                store.event(state, "FIX_STARTED" if is_fix else "WORKER_STARTED", role, agent["name"])
            if is_fix and phase != "FIXING":
                self._reset_report(store, "implementation.md", state["iteration"])
            prompt = self._worker_prompt(store, state, recovered=recovered, fixing=is_fix, role=role)
            self._submit_once(store, state, role, prompt, force_resend=force_resend)
            state[role]["status"] = "working"
            self._sync_agent(state[role], agent)
            self.registry.update(state, store.state_path)
            return state

    def finish(self, store: TaskStore) -> dict[str, Any]:
        with store.locked() as state:
            if state["phase"] not in {"IMPLEMENTING", "FIXING"}:
                raise FlowError(f"Cannot finish implementation while task is {state['phase']}")
            self._ensure_instruction_context(state)
            self._verify_instructions(state)
            validate_implementation(store.root / "implementation.md")
            self._hydrate_repair_state(state)
            role = state["repair"].get("active_role", "worker") if state["phase"] == "FIXING" else "worker"
            state["phase"] = "IMPLEMENTATION_DONE"
            state[role]["status"] = "waiting_for_review"
            state["prompts"][role] = None
            store.event(state, "IMPLEMENTATION_DONE", "worker", "Implementation report marked ready")
            self.registry.update(state, store.state_path)
            return state

    def review(self, store: TaskStore, *, force_resend: bool = False) -> dict[str, Any]:
        HerdrAdapter.require_context()
        with store.locked() as state:
            if state["phase"] == "WAITING_FOR_USER":
                raise FlowError(f"{state['user_interaction']['blocked_agent']} is waiting for the user")
            if state["phase"] not in {"IMPLEMENTATION_DONE", "REVIEWING"}:
                raise FlowError(f"Review requires IMPLEMENTATION_DONE; task is {state['phase']}")
            self._ensure_instruction_context(state)
            self._verify_instructions(state)
            self._hydrate_repair_state(state)
            delivery_role = state["repair"].get("active_role", "worker")
            delivery_record = state.get(delivery_role) or state["worker"]
            delivery_agent = self._live_agent(delivery_record["name"])
            if not delivery_agent:
                raise FlowError(f"The active delivery session ({delivery_role}) is missing; run `herdr-flow resume` before review")
            agent, recovered = self._ensure_reviewer(state, Path(state["project"]["worktree_path"]), delivery_agent["pane_id"])
            if state["phase"] != "REVIEWING":
                state["phase"] = "REVIEWING"
                state["review"]["attempt"] = int(state["review"].get("attempt", 0)) + 1
                state["review"]["status"] = "pending"
                store.event(state, "REVIEW_STARTED", "reviewer", agent["name"])
            if state["review"]["attempt"] > 1 and state["prompts"].get("reviewer") is None:
                self._reset_report(store, "review.md", state["review"]["attempt"])
            prompt = self._reviewer_prompt(store, state, recovered=recovered)
            self._submit_once(store, state, "reviewer", prompt, force_resend=force_resend)
            state["reviewer"]["status"] = "working"
            self._sync_agent(state["reviewer"], agent)
            self.registry.update(state, store.state_path)
            return state

    def review_result(self, store: TaskStore, status: str) -> dict[str, Any]:
        status = status.upper()
        if status not in {"PASS", "FAIL"}:
            raise FlowError("Review result must be PASS or FAIL")
        validate_report(store.root / "review.md", status)
        with store.locked() as state:
            if state["phase"] != "REVIEWING":
                raise FlowError(f"Cannot record review result while task is {state['phase']}")
            self._ensure_instruction_context(state)
            self._verify_instructions(state)
            state["review"]["status"] = status
            state["reviewer"]["status"] = "waiting"
            state["prompts"]["reviewer"] = None
            if status == "PASS":
                state["phase"] = "FINAL_REVIEW"
                store.event(state, "REVIEW_PASSED", "reviewer", "Independent review passed")
            else:
                self._hydrate_repair_state(state)
                state["phase"] = "REVIEW_FAILED"
                state["repair"]["failure_count"] = int(state["repair"].get("failure_count", 0)) + 1
                repair_role = self._repair_role(state)
                state[repair_role]["status"] = "needs_fix"
                store.event(
                    state,
                    "REVIEW_FAILED",
                    "reviewer",
                    f"Repair {state['repair']['failure_count']} routed to {repair_role}",
                )
            self.registry.update(state, store.state_path)
            return state

    def final_start(self, store: TaskStore) -> dict[str, Any]:
        with store.locked() as state:
            if state["phase"] != "FINAL_REVIEW" or state["review"]["status"] != "PASS":
                raise FlowError("Final review requires an independent PASS")
            self._ensure_instruction_context(state)
            self._verify_instructions(state)
            state["final_review"]["attempt"] = int(state["final_review"].get("attempt", 0)) + 1
            state["final_review"]["status"] = "pending"
            if state["final_review"]["attempt"] > 1:
                self._reset_report(store, "final-review.md", state["final_review"]["attempt"])
            store.event(state, "FINAL_REVIEW_STARTED", "coordinator", "Coordinator final review started")
            return state

    def final_result(self, store: TaskStore, status: str) -> dict[str, Any]:
        status = status.upper()
        if status not in {"PASS", "FAIL"}:
            raise FlowError("Final review result must be PASS or FAIL")
        validate_report(store.root / "final-review.md", status)
        with store.locked() as state:
            if state["phase"] != "FINAL_REVIEW":
                raise FlowError(f"Cannot record final review while task is {state['phase']}")
            self._ensure_instruction_context(state)
            self._verify_instructions(state)
            state["final_review"]["status"] = status
            state.setdefault("prompts", {})["coordinator"] = None
            if status == "PASS":
                state["phase"] = "DONE"
                store.event(state, "TASK_COMPLETED", "coordinator", "All gates passed")
            else:
                self._hydrate_repair_state(state)
                state["phase"] = "FINAL_REVIEW_FAILED"
                state["repair"]["failure_count"] = int(state["repair"].get("failure_count", 0)) + 1
                repair_role = self._repair_role(state)
                state[repair_role]["status"] = "needs_fix"
                store.event(
                    state,
                    "FINAL_REVIEW_FAILED",
                    "coordinator",
                    f"Repair {state['repair']['failure_count']} routed to {repair_role}",
                )
            self.registry.update(state, store.state_path)
            return state

    def start_coordinator(self, store: TaskStore) -> dict[str, Any]:
        """Launch a task-owned coordinator with its pinned runtime/model inside Herdr."""
        HerdrAdapter.require_context()
        brief_path = store.root / "brief.md"
        if not brief_path.is_file() or brief_path.stat().st_size > MAX_BRIEF_BYTES:
            raise FlowError(f"Write a task request below {MAX_BRIEF_BYTES} bytes to {brief_path} before starting the coordinator")
        brief = brief_path.read_text(encoding="utf-8")
        if BRIEF_PLACEHOLDER in brief or not brief.strip() or detect_secret(brief):
            raise FlowError(f"Complete {brief_path} with a non-secret task request before starting the coordinator")
        with store.locked() as state:
            if state["phase"] != "PLANNED" or state["automation"]["enabled"]:
                raise FlowError("A coordinator can only be started before this task begins")
            record = state["coordinator"]
            live = self._live_agent(record.get("name"))
            if record.get("pane") and not live:
                raise FlowError("Coordinator session disappeared; inspect before starting a replacement")
            if live and record.get("session") and live.get("agent_session") != record["session"]:
                raise FlowError("Coordinator session changed; inspect before continuing")
            if not live:
                if state["worker"].get("pane") or state["reviewer"].get("pane"):
                    raise FlowError("Task agents already started; cannot claim their worktree pane for a coordinator")
                path, pane = self._ensure_worktree(state)
                if self.herdr.agent(pane):
                    raise FlowError("Worktree root pane is already occupied; inspect before starting a coordinator")
                # The root pane belongs to the coordinator; a worker gets a new tab.
                live = self.herdr.start(record["name"], record["kind"], pane, self._agent_args(record))
                self._sync_agent(record, live)
                record["managed"] = True
                write_json(store.state_path, state)
            if not record.get("managed"):
                raise FlowError("An externally attached coordinator already owns this task")
            if not state["prompts"].get("coordinator"):
                start_instruction = (
                    f"When the handoff is complete, run herdr-flow run --task {store.task_id} exactly once yourself. "
                    if state.get("planning", {}).get("auto_start", True) else
                    "This is PLAN ONLY: complete the handoff, report it, and leave automation off. Do not run herdr-flow run until the user explicitly requests it. "
                )
                prompt = (
                    f"Coordinate Herdr Flow task {store.task_id} as a planner. Read "
                    f"{store.root / 'state.json'}, {brief_path}, {store.root / 'handoff.md'}, "
                    f"and {plugin_root() / 'skills/herdr-flow/references/planning.md'}; inspect the project. "
                    f"First read {Path(state['project']['worktree_path']) / CONTEXT_FILE} and applicable worktree instructions. "
                    "Write a decision-ready handoff with objective, acceptance criteria, risks, validation, and Definition of Done. "
                    "Do not implement production code. Ask the user about material ambiguities in this same session. "
                    f"{start_instruction}"
                    "The controller then coordinates worker/reviewer/repair sessions and returns final review to this session. "
                    "For final review, inspect evidence independently, write final-review.md PASS or FAIL, and run "
                    f"herdr-flow final-result --task {store.task_id} --status PASS|FAIL. "
                    "Never commit, merge, reset, or discard unrelated work."
                )
                self._submit_once(store, state, "coordinator", prompt)
            self.registry.update(state, store.state_path)
            return state

    def run_task(self, store: TaskStore) -> dict[str, Any]:
        """Opt into automatic handoffs from the selected live coordinator pane."""
        HerdrAdapter.require_context()
        pane = os.environ.get("HERDR_PANE_ID")
        if not pane:
            raise FlowError("Run from the coordinator agent inside a Herdr pane (HERDR_PANE_ID missing)")
        live = self.herdr.agent(pane)
        with store.locked() as state:
            previous = state["coordinator"]
            if not live or live.get("agent") != previous["kind"]:
                raise FlowError(f"Run from the selected {previous['kind']} coordinator agent inside Herdr")
            if previous.get("managed"):
                if live.get("name") != previous.get("name") or live.get("agent_session") != previous.get("session") or pane != previous.get("pane"):
                    raise FlowError("Run from this task's original managed coordinator session")
            elif previous.get("model") or previous.get("variant") or previous["kind"] != "claude":
                raise FlowError("Pinned-model or non-Claude coordinators require `herdr-flow start-coordinator --task ...` first")
            elif previous.get("pane") and (pane != previous["pane"] or not previous.get("session") or live.get("agent_session") != previous["session"]):
                raise FlowError("Original attached coordinator pane or session changed; inspect before continuing")
            if state.get("automation", {}).get("enabled") and previous.get("pane") != pane:
                raise FlowError("Another coordinator pane already owns this task; stop before changing ownership")
            already_running = bool(state.get("automation", {}).get("enabled"))
            self._sync_agent(previous, live)
            state["automation"] = {"enabled": True, "attention": None}
            if not already_running:
                state.setdefault("prompts", {})["coordinator"] = None
            store.event(state, "AUTOMATION_STARTED", "coordinator", pane)
            self.registry.update(state, store.state_path)
        return self.advance(store)

    def _request_final_review(self, store: TaskStore, *, settled_role: str | None = None) -> dict[str, Any]:
        with store.locked() as state:
            self._hydrate_repair_state(state)
            if state["phase"] != "FINAL_REVIEW":
                return state
            self._ensure_instruction_context(state)
            self._verify_instructions(state)
            prior = state["prompts"].get("coordinator")
            if prior and prior.get("status") in {"sending", "submitted", "uncertain"}:
                return state
            record = state["coordinator"]
            target = record.get("name") if record.get("managed") else record.get("pane")
            live = self._live_agent(target) if target else None
            if not live:
                state["automation"]["attention"] = "Original coordinator agent is missing; inspect its recorded pane before final review"
                return state
            if live.get("pane_id") != record.get("pane") or live.get("agent") != record["kind"]:
                state["automation"]["attention"] = "Original coordinator pane or agent kind changed; inspect before final review"
                return state
            if not record.get("session") or live.get("agent_session") != record["session"]:
                state["automation"]["attention"] = "Original coordinator session cannot be verified; inspect before final review"
                return state
            status = record.get("status") if settled_role == "coordinator" else live.get("agent_status")
            if status not in {"idle", "done"}:
                state["automation"]["attention"] = "Waiting for the existing coordinator to become idle before final review"
                return state
            if not state["final_review"]["attempt"] or state["final_review"]["status"] != "pending":
                state["final_review"]["attempt"] = int(state["final_review"].get("attempt", 0)) + 1
                state["final_review"]["status"] = "pending"
                if state["final_review"]["attempt"] > 1:
                    self._reset_report(store, "final-review.md", state["final_review"]["attempt"])
                store.event(state, "FINAL_REVIEW_STARTED", "coordinator", "Automatic final review requested")
            prompt = (
                f"Herdr Flow task {store.task_id}: independent review passed. Act as final release gate. "
                f"Read {store.root / 'handoff.md'}, {store.root / 'brief.md'} if present, "
                f"{store.root / 'implementation.md'}, {store.root / 'review.md'}, "
                f"{store.root / 'decisions.md'}, and {plugin_root() / 'skills/herdr-flow/references/final-review.md'}; inspect the worktree. "
                f"Read {Path(state['project']['worktree_path']) / CONTEXT_FILE} and applicable worktree instructions; "
                f"write PASS or FAIL with evidence in {store.root / 'final-review.md'}; do not implement fixes yourself. "
                f"When done, run herdr-flow final-result --task {store.task_id} --status PASS|FAIL yourself. "
                "The controller advances automatically; an idle event also reconciles the report if needed. Do not ask the user to run a phase command. "
                "Do not commit, merge, or remove the worktree."
            )
            # Attached agents can lose their display name while keeping the same pane and native session.
            # Send by pane only after verifying both; managed coordinators retain their owned name.
            prompt_target = record["name"] if record.get("managed") and live.get("name") == record.get("name") else record["pane"]
            self._submit_once(store, state, "coordinator", prompt, target=prompt_target)
            state["automation"]["attention"] = None
            self.registry.update(state, store.state_path)
            return state

    def advance(self, store: TaskStore, *, settled_role: str | None = None) -> dict[str, Any]:
        """Serialize concurrent completion commands/events for this task only."""
        HerdrAdapter.require_context()
        with file_lock(store.root / ".drive.lock"):
            return self._advance_locked(store, settled_role=settled_role)

    def _advance_locked(self, store: TaskStore, *, settled_role: str | None = None) -> dict[str, Any]:
        """Advance from a verified report and its settled agent event; never resend a prompt."""
        state = store.read()
        if not state.get("automation", {}).get("enabled") or state["phase"] == "WAITING_FOR_USER":
            return state
        phase = state["phase"]
        try:
            if phase != "DONE":
                with store.locked() as current:
                    self._ensure_instruction_context(current)
                    self._verify_instructions(current)
                state = store.read()
            if phase in {"IMPLEMENTING", "FIXING"}:
                role = state.get("repair", {}).get("active_role", "worker") if phase == "FIXING" else "worker"
                if settled_role and settled_role != role:
                    return state
                if state[role].get("status") not in {"idle", "done"}:
                    return state
                report = (store.root / "implementation.md").read_text(encoding="utf-8")
                if not re.search(r"(?im)^## Status\s*\n+\s*(READY|COMPLETE|COMPLETED|DONE)\s*$", report):
                    return state
                validate_implementation(store.root / "implementation.md")
                self.finish(store)
                phase = "IMPLEMENTATION_DONE"
            if phase == "IMPLEMENTATION_DONE":
                return self.review(store)
            if phase == "REVIEWING":
                if settled_role and settled_role != "reviewer":
                    return state
                if state["reviewer"].get("status") not in {"idle", "done"}:
                    return state
                text = (store.root / "review.md").read_text(encoding="utf-8")
                match = re.search(r"(?im)^## Status\s*\n+\s*(PASS|FAIL)\s*$", text)
                if not match:
                    return store.read()
                self.review_result(store, match.group(1))
                phase = store.read()["phase"]
            if phase in {"REVIEW_FAILED", "FINAL_REVIEW_FAILED"}:
                return self.implement(store)
            if phase == "FINAL_REVIEW":
                coordinator = state["coordinator"]
                if state["prompts"].get("coordinator"):
                    if settled_role and settled_role != "coordinator":
                        return state
                    if coordinator.get("status") not in {"idle", "done"}:
                        return state
                text = (store.root / "final-review.md").read_text(encoding="utf-8")
                match = re.search(r"(?im)^## Status\s*\n+\s*(PASS|FAIL)\s*$", text)
                if match and store.read()["prompts"].get("coordinator"):
                    self.final_result(store, match.group(1))
                    return store.read()
                return self._request_final_review(store, settled_role=settled_role)
            if phase == "DONE":
                try:
                    return self.finalize(store)
                except (FlowError, HerdrError):
                    return store.read()  # Git emits no Herdr event; explicit finalize is the reliable fallback.
            if phase == "PLANNED":
                if "<!-- Claude coordinator: replace this placeholder. -->" in (store.root / "handoff.md").read_text(encoding="utf-8"):
                    raise FlowError("Complete the handoff before starting automated implementation")
                return self.implement(store)
            return store.read()
        except FlowError as exc:
            # An uncertain prompt or missing/blocked agent needs inspection, not a blind retry.
            with store.locked() as latest:
                latest["automation"]["attention"] = str(exc)
            return store.read()

    def _worktree_branch(self, path: Path) -> str:
        listed = run(["git", "worktree", "list", "--porcelain"], cwd=self.source_root).stdout
        stanza = next((item for item in listed.split("\n\n") if item.startswith(f"worktree {path}\n")), None)
        branches = [line.removeprefix("branch refs/heads/") for line in stanza.splitlines()
                    if line.startswith("branch refs/heads/")] if stanza else []
        if len(branches) != 1:
            raise FlowError(f"Task worktree {path} is absent or detached; inspect before cleanup")
        return branches[0]

    def _check_other_task_branch(self, task_id: str, branch: str) -> None:
        for state_path in self.workflow_root.glob("task-*/state.json"):
            if state_path.parent.name != task_id and read_json(state_path).get("project", {}).get("branch") == branch:
                raise FlowError(f"Branch {branch} is recorded by another Herdr Flow task; refusing to claim it")

    def reconcile_branch(self, store: TaskStore, branch: str, *, yes: bool = False) -> dict[str, Any]:
        """Explicitly record a checked-out task branch rename; never rename Git refs."""
        HerdrAdapter.require_context()
        if not yes:
            raise FlowError("Branch reconciliation requires --yes after inspecting the worktree and its Git history")
        with store.locked() as state:
            if state["phase"] != "DONE" or state.get("cleanup", {}).get("completed"):
                raise FlowError("Only an unfinished cleanup in DONE can reconcile a branch")
            path = state["project"].get("worktree_path")
            if not path or not Path(path).is_dir():
                raise FlowError("Recorded task worktree is missing; cannot reconcile a branch")
            path = Path(path).resolve()
            marker = path / ".herdr-flow-task.json"
            if not marker.is_file():
                raise FlowError("Task worktree has no ownership marker; refusing branch reconciliation")
            owner = read_json(marker)
            if owner.get("task_id") != store.task_id or owner.get("source_root") != str(self.source_root):
                raise FlowError("Task worktree ownership marker does not match; refusing branch reconciliation")
            actual = self._worktree_branch(path)
            if actual != branch or not self._branch_exists(branch):
                raise FlowError(f"Git worktree is on {actual}, not the requested existing branch {branch}")
            old = state["project"]["branch"]
            if old != branch:
                self._check_other_task_branch(store.task_id, branch)
                state["project"].setdefault("branch_history", []).append({"from": old, "to": branch, "at": utc_now(), "reason": "user_confirmed"})
                state["project"]["branch"] = branch
                store.event(state, "BRANCH_RECONCILED", "coordinator", f"User confirmed branch rename: {old} -> {branch}")
            return state

    def finalize(self, store: TaskStore) -> dict[str, Any]:
        """Close only an owned, idle, clean worktree whose branch reached source HEAD."""
        HerdrAdapter.require_context()
        state = store.read()
        if state["phase"] != "DONE" or state["review"]["status"] != "PASS" or state["final_review"]["status"] != "PASS":
            raise FlowError("Cleanup requires DONE and both review gates PASS")
        if state.get("cleanup", {}).get("completed"):
            return state
        project = state["project"]
        workspace, path = project.get("workspace_id"), project.get("worktree_path")
        if not workspace or not path:
            raise FlowError("Task has no recorded worktree workspace")
        path = Path(path).resolve()
        branch = project["branch"]
        actual = self._worktree_branch(path)
        if actual != branch:
            marker = path / ".herdr-flow-task.json"
            if not marker.is_file():
                raise FlowError("Task worktree has no ownership marker; refusing automatic branch reconciliation")
            owner = read_json(marker)
            if owner.get("task_id") != store.task_id or owner.get("source_root") != str(self.source_root):
                raise FlowError("Task worktree ownership marker does not match; refusing automatic branch reconciliation")
            self._check_other_task_branch(store.task_id, actual)
            if self._branch_exists(branch):
                raise FlowError(f"Recorded branch {branch} still exists but worktree uses {actual}; inspect and explicitly reconcile")
            log = run(["git", "reflog", "show", "--format=%gs", f"refs/heads/{actual}"], cwd=self.source_root, check=False)
            proof = f"Branch: renamed refs/heads/{branch} to refs/heads/{actual}"
            if log.returncode != 0 or proof not in log.stdout.splitlines():
                raise FlowError(f"Cannot verify rename {branch} -> {actual}; from a Herdr pane inspect Git history, then run `herdr-flow reconcile-branch --task {store.task_id} --branch {actual} --yes`")
            branch = actual
        source_branch = run(["git", "branch", "--show-current"], cwd=self.source_root).stdout.strip()
        if not source_branch or source_branch == branch:
            raise FlowError("Source checkout must be on its integration branch for merge verification")
        merged = run(["git", "merge-base", "--is-ancestor", f"refs/heads/{branch}", "HEAD"], cwd=self.source_root, check=False)
        if merged.returncode != 0:
            raise FlowError(f"Task branch {branch} is not merged into source HEAD; cleanup deferred")
        dirty = run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=path).stdout
        if dirty.strip():
            raise FlowError(f"Task worktree contains uncommitted or untracked files; cleanup deferred:\n{dirty[:800]}")
        owned = project.get("instruction_snapshots", {})
        for filename, snapshot in owned.items():
            candidate = path / filename
            if not candidate.is_file() or candidate.is_symlink() or self._file_sha(candidate) != snapshot["sha256"]:
                raise FlowError(f"Owned instruction file changed or disappeared: {candidate}; cleanup deferred")
        ignored = run(["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"], cwd=path).stdout
        extra_ignored = [name for name in ignored.split("\0") if name and name not in {".herdr-flow-task.json", ".ai/workflow", *owned}]
        if extra_ignored:
            raise FlowError(f"Task worktree contains ignored files not owned by Herdr Flow; cleanup deferred: {extra_ignored[:8]}")
        owned_roles = (*SESSION_ROLES, "coordinator") if state["coordinator"].get("managed") else SESSION_ROLES
        known = {record.get("pane") for role in owned_roles if (record := state.get(role))}
        extra = [p.get("pane_id") for p in self.herdr.panes() if p.get("workspace_id") == workspace and p.get("pane_id") not in known]
        if extra:
            raise FlowError(f"Unowned panes in task workspace {workspace}; cleanup deferred: {extra}")
        for role in owned_roles:
            record = state.get(role)
            if record:
                agent = self._live_agent(record.get("name"))
                if agent and agent.get("agent_status") not in {"idle", "done"}:
                    raise FlowError(f"{role} is not idle; cleanup deferred")
        # Herdr owns the panes and agent processes; never signal them from outside Herdr.
        self.herdr.remove_worktree(workspace)
        with store.locked() as locked:
            if branch != locked["project"]["branch"]:
                previous = locked["project"]["branch"]
                locked["project"].setdefault("branch_history", []).append({"from": previous, "to": branch, "at": utc_now(), "reason": "git_reflog"})
                locked["project"]["branch"] = branch
                store.event(locked, "BRANCH_RECONCILED", "coordinator", f"Verified Git branch rename: {previous} -> {branch}")
            locked["cleanup"] = {"completed": True, "at": utc_now(), "workspace": workspace, "path": str(path)}
            store.event(locked, "WORKTREE_CLEANED", "coordinator", f"Merged clean worktree removed: {path}")
            self.registry.update(locked, store.state_path)
            return locked

    def resume(self, store: TaskStore) -> dict[str, Any]:
        state = store.read()
        phase = state["phase"]
        if phase == "WAITING_FOR_USER":
            blocked = state["user_interaction"]["blocked_agent"]
            print(f"{store.task_id} is waiting for you in live agent {blocked}.")
            print(f"Focus it with: herdr-flow focus --task {store.task_id} --role {self._role_for_name(state, blocked)}")
            return state
        if phase in {"PLANNED", "IMPLEMENTING", "REVIEW_FAILED", "FIXING", "FINAL_REVIEW_FAILED"}:
            return self.implement(store)
        if phase == "IMPLEMENTATION_DONE":
            return self.review(store)
        if phase == "REVIEWING":
            with store.locked() as locked:
                reviewer = self._live_agent(locked["reviewer"]["name"])
                if reviewer:
                    print("Reviewer is still live; refusing to duplicate its prompt.")
                    return locked
                self._hydrate_repair_state(locked)
                delivery_role = locked["repair"].get("active_role", "worker")
                delivery_record = locked.get(delivery_role) or locked["worker"]
                delivery_agent = self._live_agent(delivery_record["name"])
                if not delivery_agent:
                    raise FlowError(f"Reviewer and active delivery session ({delivery_role}) are missing; recover repairs first with `herdr-flow implement`")
                agent, recovered = self._ensure_reviewer(locked, Path(locked["project"]["worktree_path"]), delivery_agent["pane_id"])
                prompt = self._reviewer_prompt(store, locked, recovered=recovered)
                self._submit_once(store, locked, "reviewer", prompt)
                self.registry.update(locked, store.state_path)
                return locked
        return state

    def inspect_agent(self, store: TaskStore, role: str) -> str:
        HerdrAdapter.require_context()
        if role not in ("coordinator", *SESSION_ROLES):
            raise FlowError(f"Role must be one of: coordinator, {', '.join(SESSION_ROLES)}")
        with store.locked() as state:
            if not state.get(role):
                raise FlowError(f"No {role} session is configured for this task")
            record = state[role]
            target = record.get("pane") if role == "coordinator" and not record.get("managed") else record["name"]
            agent = self.herdr.agent(target) if target else None
            if not agent:
                raise FlowError(f"{role} session is not live")
            if role == "coordinator" and (agent.get("pane_id") != record.get("pane") or agent.get("agent") != record["kind"] or
                                          not record.get("session") or agent.get("agent_session") != record["session"]):
                raise FlowError("Original coordinator pane or session changed; refusing to inspect a different agent")
            output = self.herdr.read_agent(target)
            prompt = state["prompts"].get(role)
            if prompt:
                prompt["inspected_at"] = utc_now()
                prompt["observed_status"] = agent.get("agent_status")
                prompt["output_sha256"] = hashlib.sha256(output.encode()).hexdigest()
            self._sync_agent(state[role], agent)
            self.registry.update(state, store.state_path)
            return output

    def resend(self, store: TaskStore, role: str, yes: bool) -> dict[str, Any]:
        if not yes:
            raise FlowError("Resend requires --yes after inspecting the existing session")
        with store.locked() as state:
            record = state["prompts"].get(role)
            if not record or not record.get("inspected_at"):
                raise FlowError(f"Run `herdr-flow inspect --role {role}` before resending")
            if record.get("status") not in {"uncertain", "submitted"}:
                raise FlowError("There is no uncertain/submitted prompt to resend")
            prompt = record["text"]
            if role == "coordinator":
                owner = state[role]
                destination = owner.get("pane") if not owner.get("managed") else owner.get("name")
                live = self._live_agent(destination) if destination else None
                if not live or live.get("pane_id") != owner.get("pane") or live.get("agent") != owner["kind"] or not owner.get("session") or live.get("agent_session") != owner["session"]:
                    raise FlowError("Original coordinator session cannot be verified; refusing resend")
            state["prompts"][role] = None
            self._submit_once(store, state, role, prompt, force_resend=True)
            return state

    def focus(self, store: TaskStore, role: str) -> dict[str, Any]:
        HerdrAdapter.require_context()
        state = store.read()
        if role not in ("coordinator", *SESSION_ROLES):
            raise FlowError(f"Role must be one of: coordinator, {', '.join(SESSION_ROLES)}")
        if not state.get(role):
            raise FlowError(f"No {role} session is configured for this task")
        record = state[role]
        target = record.get("pane") if role == "coordinator" and not record.get("managed") else record.get("name")
        if not target:
            raise FlowError(f"No {role} session is recorded")
        if role == "coordinator":
            live = self._live_agent(target)
            if not live or live.get("pane_id") != record.get("pane") or live.get("agent") != record["kind"] or not record.get("session") or live.get("agent_session") != record["session"]:
                raise FlowError("Original coordinator pane or session changed; refusing to focus a different agent")
        return self.herdr.focus(target)

    def refresh_instructions(self, store: TaskStore, *, yes: bool) -> dict[str, Any]:
        HerdrAdapter.require_context()
        if not yes:
            raise FlowError("Inspect the main checkout instructions and worktree copies, then pass --yes to refresh")
        with file_lock(store.root / ".drive.lock"):
            with store.locked() as state:
                path = state["project"].get("worktree_path")
                if not path or not Path(path).is_dir():
                    raise FlowError("This task has no active worktree")
                marker = Path(path) / ".herdr-flow-task.json"
                if not marker.is_file() or read_json(marker).get("task_id") != store.task_id or read_json(marker).get("source_root") != str(self.source_root):
                    raise FlowError("Worktree marker does not match this project and task")
                for role in ("coordinator", *SESSION_ROLES):
                    record = state.get(role)
                    if record and (agent := self._live_agent(record.get("name"))) and agent.get("agent_status") not in {"idle", "done"}:
                        raise FlowError(f"{role} is active; wait until it settles before refreshing instructions")
                self._snapshot_instructions(state, Path(path), refresh=True)
                store.event(state, "INSTRUCTIONS_REFRESHED", "coordinator", "Reviewed instruction snapshots refreshed")
                return state

    def _ensure_instruction_context(self, state: dict[str, Any]) -> None:
        project = state["project"]
        path = project.get("worktree_path")
        if not path or "instruction_snapshots" in project:
            return
        worktree = Path(path)
        marker = worktree / ".herdr-flow-task.json"
        if not marker.is_file():
            raise FlowError("Existing task worktree has no ownership marker; inspect before adding instructions")
        owner = read_json(marker)
        if owner.get("task_id") != state["task_id"] or owner.get("source_root") != str(self.source_root):
            raise FlowError("Existing worktree ownership does not match this task; instructions not copied")
        self._snapshot_instructions(state, worktree)

    def _instruction_notices(self, state: dict[str, Any]) -> list[str]:
        path = state["project"].get("worktree_path")
        recorded = state["project"].get("instruction_snapshots", {})
        if not path:
            return []
        if not recorded:
            return ["Existing task worktree has no instruction context; reconcile before the next handoff"]
        notices = []
        for filename, snapshot in recorded.items():
            if filename != CONTEXT_FILE:
                source = self.source_root / filename
                if not source.is_file() or source.is_symlink() or self._file_sha(source) != snapshot["source_sha256"]:
                    notices.append(f"{filename}: main checkout changed or disappeared; review before refresh")
            target = Path(path) / filename
            if not target.is_file() or target.is_symlink() or self._file_sha(target) != snapshot["sha256"]:
                notices.append(f"{filename}: worktree snapshot changed or disappeared; inspect manually")
        for filename in INSTRUCTION_FILES:
            if filename not in recorded and (self.source_root / filename).exists() and not (Path(path) / filename).exists():
                notices.append(f"{filename}: new main-checkout instructions; review before refresh")
        return notices

    def _verify_instructions(self, state: dict[str, Any]) -> None:
        notices = self._instruction_notices(state)
        if notices:
            raise FlowError("Instruction snapshot needs attention: " + "; ".join(notices))

    def status(self, store: TaskStore, *, live: bool = True) -> dict[str, Any]:
        state = store.read()
        self._hydrate_repair_state(state)
        state["instruction_notices"] = self._instruction_notices(state)
        if live and os.environ.get("HERDR_ENV") == "1":
            for role in ("coordinator", *SESSION_ROLES):
                record = state.get(role)
                if not record:
                    continue
                target = record.get("pane") if role == "coordinator" and not record.get("managed") else record.get("name")
                agent = self._live_agent(target)
                if role == "coordinator" and agent:
                    agent = agent if (agent.get("pane_id") == record.get("pane") and agent.get("agent") == record["kind"]
                                      and record.get("session") and agent.get("agent_session") == record["session"]) else None
                record["live"] = bool(agent)
                if agent:
                    record["live_status"] = agent.get("agent_status")
                    record["live_pane"] = agent.get("pane_id")
        return state

    def handle_event(self, event: dict[str, Any]) -> None:
        data = event.get("data", {}) if isinstance(event, dict) else {}
        pane = data.get("pane_id")
        if not isinstance(pane, str):
            nested = data.get("pane")
            if isinstance(nested, dict):
                pane = nested.get("pane_id")
        if not pane:
            return
        matches = self.registry.find_by_pane(pane)
        if not matches:
            return
        kind = str(event.get("event", ""))
        for store, role in matches:
            settled = False
            with store.locked() as state:
                if state.get("cleanup", {}).get("completed"):
                    continue
                record = state[role]
                if kind == "pane.moved" and isinstance(data.get("pane"), dict):
                    new_pane = data["pane"].get("pane_id")
                    if new_pane:
                        record["pane"] = new_pane
                elif kind == "pane.agent_status_changed":
                    status = str(data.get("agent_status", "unknown")).lower()
                    record["status"] = status
                    settled = status in {"idle", "done"}
                    if status == "blocked":
                        if state["phase"] != "WAITING_FOR_USER":
                            state["user_interaction"] = {
                                "required": True,
                                "blocked_agent": record.get("name"),
                                "return_phase": state["phase"],
                            }
                            state["phase"] = "WAITING_FOR_USER"
                            blocked_event = "COORDINATOR_BLOCKED" if role == "coordinator" else "REVIEWER_BLOCKED" if role == "reviewer" else "REPAIR_AGENT_BLOCKED" if role != "worker" else "WORKER_BLOCKED"
                            store.event(state, blocked_event, role, "Agent needs user input")
                    elif status == "working" and state["phase"] == "WAITING_FOR_USER" and state["user_interaction"].get("blocked_agent") == record.get("name"):
                        return_phase = state["user_interaction"].get("return_phase") or ("FINAL_REVIEW" if role == "coordinator" else "REVIEWING" if role == "reviewer" else "FIXING" if role != "worker" else "IMPLEMENTING")
                        state["phase"] = return_phase
                        state["user_interaction"] = {"required": False, "blocked_agent": None, "return_phase": None}
                        store.event(state, "USER_RESPONDED", role, "Same live session resumed")
                elif kind in {"pane.exited", "pane.closed"} or (kind == "pane.agent_detected" and data.get("released")):
                    record["status"] = "missing"
                    record["last_lost_at"] = utc_now()
                    lost_event = "COORDINATOR_LOST" if role == "coordinator" else "REVIEWER_LOST" if role == "reviewer" else "REPAIR_AGENT_LOST" if role != "worker" else "WORKER_LOST"
                    store.event(state, lost_event, role, "Session disappeared; no replacement started automatically")
                self.registry.update(state, store.state_path)
            if settled:
                self.advance(store, settled_role=role)

    @staticmethod
    def _reset_report(store: TaskStore, filename: str, attempt: int) -> None:
        path = store.root / filename
        previous = path.read_text(encoding="utf-8")
        archive = store.root / "attempts" / f"{Path(filename).stem}-{attempt - 1:03d}.md"
        if previous != TEMPLATES[filename] and not archive.exists():
            atomic_write(archive, previous)
        atomic_write(path, TEMPLATES[filename])

    def _hydrate_repair_state(self, state: dict[str, Any]) -> None:
        repair = state.setdefault(
            "repair",
            {
                "failure_count": 0,
                "fix_attempts": 0,
                "active_role": "worker",
                "base_role": "worker",
                "escalated": False,
                "escalation": {
                    "enabled": bool(self.config["repair"]["escalation"].get("enabled")),
                    "after_failures": int(self.config["repair"]["escalation"]["after_failures"]),
                },
            },
        )
        repair.setdefault("failure_count", 0)
        repair.setdefault("fix_attempts", 0)
        repair.setdefault("active_role", "worker")
        repair.setdefault("base_role", "fixer" if state.get("fixer") else "worker")
        repair.setdefault("escalated", False)
        escalation = repair.setdefault("escalation", {})
        escalation.setdefault("enabled", bool(self.config["repair"]["escalation"].get("enabled")))
        escalation.setdefault("after_failures", int(self.config["repair"]["escalation"]["after_failures"]))
        state.setdefault("fixer", None)
        if "escalation_fixer" not in state:
            default = self.config["repair"]["escalation"]["agent"]
            name = self._agent_name("escalator", state["task_id"])
            state["escalation_fixer"] = self._agent_state(name, default) if escalation["enabled"] else None
        prompts = state.setdefault("prompts", {})
        for role in (*SESSION_ROLES, "coordinator"):
            prompts.setdefault(role, None)
        state.setdefault("automation", {"enabled": False, "attention": None})

    @staticmethod
    def _same_selection(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
        if not left or not right:
            return False
        return all(left.get(key) == right.get(key) for key in ("kind", "model", "variant"))

    def _repair_role(self, state: dict[str, Any]) -> str:
        self._hydrate_repair_state(state)
        repair = state["repair"]
        escalation = repair["escalation"]
        if escalation.get("enabled") and int(repair.get("failure_count", 0)) > int(escalation.get("after_failures", 3)):
            escalation_record = state.get("escalation_fixer")
            if not escalation_record:
                default = self.config["repair"]["escalation"]["agent"]
                escalation_record = self._agent_state(self._agent_name("escalator", state["task_id"]), default)
                state["escalation_fixer"] = escalation_record
            if self._same_selection(state.get("fixer"), escalation_record):
                role = "fixer"
            elif self._same_selection(state.get("worker"), escalation_record):
                role = "worker"
            else:
                role = "escalation_fixer"
            repair["escalated"] = True
            repair["active_role"] = role
            return role
        role = repair.get("base_role", "worker")
        if role == "fixer" and not state.get("fixer"):
            role = "worker"
        repair["escalated"] = False
        repair["active_role"] = role
        return role

    def _ensure_worktree(self, state: dict[str, Any]) -> tuple[Path, str]:
        project = state["project"]
        path = Path(project["worktree_path"]).resolve() if project.get("worktree_path") else None
        workspace = project.get("workspace_id")
        root_pane = state["worker"].get("pane") or project.get("root_pane")
        live_workspaces = {item.get("workspace_id") for item in self.herdr.workspaces()}
        if path and path.exists() and workspace in live_workspaces and root_pane:
            self._snapshot_instructions(state, path)
            return path, root_pane
        branch = project["branch"]
        if path and path.exists():
            result = self.herdr.open_worktree(self.source_root, path=str(path), label=state["task_id"])
        elif self._branch_exists(branch):
            result = self.herdr.open_worktree(self.source_root, branch=branch, label=state["task_id"])
        else:
            result = self.herdr.create_worktree(self.source_root, branch, project["base_ref"], state["task_id"])
        path = Path(result["worktree"]["path"]).resolve()
        project["worktree_path"] = str(path)
        project["workspace_id"] = result["workspace"]["workspace_id"]
        root_pane = result["root_pane"]["pane_id"]
        project["root_pane"] = root_pane
        self._link_task_marker(path, state)
        self._snapshot_instructions(state, path)
        return path, root_pane

    @staticmethod
    def _file_sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _snapshot_instructions(self, state: dict[str, Any], worktree: Path, *, refresh: bool = False) -> None:
        """Snapshot allowlisted ignored root instructions; never overwrite unowned files."""
        project = state["project"]
        recorded = project.get("instruction_snapshots", {})
        for filename in (*INSTRUCTION_FILES, CONTEXT_FILE):
            destination = worktree / filename
            if filename in recorded:
                if not destination.is_file() or destination.is_symlink() or self._file_sha(destination) != recorded[filename]["sha256"]:
                    raise FlowError(f"Owned instruction snapshot {destination} changed or disappeared; inspect it before continuing")
        note = worktree / CONTEXT_FILE
        if CONTEXT_FILE not in recorded and (note.exists() or note.is_symlink()):
            raise FlowError(f"Refusing to overwrite existing worktree context note: {note}")
        snapshots = dict(recorded)
        pending: list[tuple[str, Path, str, str]] = []
        for filename in INSTRUCTION_FILES:
            source = self.source_root / filename
            destination = worktree / filename
            if not source.exists():
                if filename in recorded:
                    raise FlowError(f"Instruction source disappeared: {source}; inspect before continuing")
                continue
            if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_INSTRUCTION_BYTES:
                raise FlowError(f"Refusing unsafe or oversized instruction source: {source}")
            data = source.read_bytes()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise FlowError(f"Instruction source must be UTF-8: {source}") from exc
            if detect_secret(text):
                raise FlowError(f"Potential secret in {source}; refusing to copy into a worktree")
            source_sha = hashlib.sha256(data).hexdigest()
            if filename in recorded:
                if recorded[filename]["source_sha256"] == source_sha:
                    continue
                if not refresh:
                    raise FlowError(f"Instruction source changed: {source}; review and explicitly refresh before continuing")
            elif destination.exists() or destination.is_symlink():
                # Tracked worktree instructions or user-owned ignored files win.
                continue
            elif CONTEXT_FILE in recorded and not refresh:
                raise FlowError(f"New instruction source {source} appeared; review and explicitly refresh before continuing")
            elif run(["git", "check-ignore", "-q", "--no-index", filename], cwd=worktree, check=False).returncode != 0:
                raise FlowError(f"{filename} is not ignored in {worktree}; refusing an untracked instruction copy")
            pending.append((filename, destination, text, source_sha))
        for filename, destination, text, source_sha in pending:
            atomic_write(destination, text)
            snapshots[filename] = {"sha256": source_sha, "source_sha256": source_sha}
        lines = [
            "# Herdr Flow worktree context", "",
            "These are task-local snapshots of ignored project instructions, not live links. Read",
            "the root AGENTS.md and CLAUDE.md if present, and follow applicable project rules.",
            "Tracked instructions in this branch and nested directory instructions retain their own scope.", "",
            f"Main checkout: {self.source_root}",
            f"Task worktree: {worktree}", "",
            "Commands and paths in copied instructions may describe only the main checkout.",
            "Before running any command, verify its paths, dependencies, and side effects here.",
            "In particular, Docker/Compose services, mounts, volumes, databases, and caches may",
            "point at the main checkout or shared state. Do not blindly run a main-checkout",
            "command in this worktree or redirect it to the main checkout. Ask the user if",
            "isolation or an adapted validation command needs approval; report checks you cannot run.",
            "Never modify the main checkout just to satisfy a task-worktree instruction.", "",
            "Instruction sources are not automatically refreshed. Compare against the main",
            "checkout when they change, and request a reviewed refresh before proceeding.", "",
            "## Snapshotted files", "",
        ]
        lines += [f"- {name}: SHA-256 {snapshots[name]['source_sha256']}" for name in INSTRUCTION_FILES if name in snapshots]
        if not any(name in snapshots for name in INSTRUCTION_FILES):
            lines.append("- None; use tracked project instructions, if present.")
        rendered = "\n".join(lines) + "\n"
        if CONTEXT_FILE in recorded:
            if refresh and self._file_sha(note) != hashlib.sha256(rendered.encode()).hexdigest():
                atomic_write(note, rendered)
                snapshots[CONTEXT_FILE] = {"sha256": self._file_sha(note)}
        else:
            atomic_write(note, rendered)
            snapshots[CONTEXT_FILE] = {"sha256": self._file_sha(note)}
        project["instruction_snapshots"] = snapshots

    def _ensure_agent(self, state: dict[str, Any], role: str, preferred_pane: str) -> tuple[dict[str, Any], bool]:
        record = state[role]
        live = self._live_agent(record.get("name"))
        if live:
            self._sync_agent(record, live)
            return live, False
        recovered = bool(record.get("pane") or record.get("session"))
        if recovered:
            record["recovery_count"] = int(record.get("recovery_count", 0)) + 1
            record["name"] = self._recovery_name(record["name"], record["recovery_count"])
        pane = record.get("pane") or preferred_pane
        workspace = state["project"].get("workspace_id")
        worktree = Path(state["project"]["worktree_path"])
        # A managed coordinator owns the initial worktree pane. Never start a
        # worker/reviewer/fixer there, even while the coordinator is idle.
        if pane == state["coordinator"].get("pane"):
            pane = self.herdr.create_tab(workspace, worktree, role)["root_pane"]["pane_id"]
        if recovered and (not pane or not self._pane_available_for_start(pane)):
            created = self.herdr.create_tab(workspace, worktree, f"{role}-recovery")
            pane = created["root_pane"]["pane_id"]
        try:
            agent = self.herdr.start(record["name"], record["kind"], pane, self._agent_args(record))
        except HerdrError as exc:
            if not recovered or exc.code not in {"pane_not_found", "agent_pane_busy", "agent_pane_unavailable"}:
                raise
            created = self.herdr.create_tab(workspace, worktree, f"{role}-recovery")
            pane = created["root_pane"]["pane_id"]
            agent = self.herdr.start(record["name"], record["kind"], pane, self._agent_args(record))
        self._sync_agent(record, agent)
        record["status"] = "idle"
        if recovered:
            record["recovered_at"] = utc_now()
        return agent, recovered

    def _ensure_repair_agent(self, state: dict[str, Any], role: str, worktree: Path, anchor_pane: str) -> tuple[dict[str, Any], bool]:
        if role not in {"fixer", "escalation_fixer"} or not state.get(role):
            raise FlowError(f"Repair role is not configured: {role}")
        record = state[role]
        live = self._live_agent(record.get("name"))
        if role == "escalation_fixer" and live and not record.get("pane") and not record.get("session"):
            raise FlowError("Escalation fixer is live but startup identity was not recorded; inspect its pane and use adopt-startup before advancing")
        if record.get("terminal_id"):
            if not live or live.get("pane_id") != record.get("pane") or live.get("terminal_id") != record["terminal_id"] or live.get("agent") != record["kind"] or (record.get("session") and live.get("agent_session") != record["session"]):
                raise FlowError("Adopted repair session identity changed; inspect its pane, do not restart or resend")
            screen = self.herdr.read_agent(record["name"], lines=80)
            displayed = re.findall(r"(?i)\bGPT[- ]6[- ]Sol\s+(minimal|low|medium|high|xhigh|max|ultra)\b", screen)
            if not displayed or displayed[-1].lower() != "high":
                raise FlowError("Adopted Codex no longer confirms GPT-6-Sol high; inspect /model before advancing")
        if live:
            self._sync_agent(record, live)
            return live, False
        pane = record.get("pane")
        if not pane or not self._pane_available_for_start(pane):
            created = self.herdr.create_tab(state["project"]["workspace_id"], worktree, role.replace("_", "-"))
            pane = created["root_pane"]["pane_id"]
        return self._ensure_agent(state, role, pane)

    def _ensure_reviewer(self, state: dict[str, Any], worktree: Path, worker_pane: str) -> tuple[dict[str, Any], bool]:
        live = self._live_agent(state["reviewer"].get("name"))
        if live:
            self._sync_agent(state["reviewer"], live)
            return live, False
        pane = state["reviewer"].get("pane")
        if not pane or not self._pane_available_for_start(pane):
            pane = self.herdr.split(worker_pane, worktree)["pane_id"]
        return self._ensure_agent(state, "reviewer", pane)

    def _pane_available_for_start(self, pane: str) -> bool:
        # A missing agent in the recorded pane is commonly a returned shell. Herdr's
        # agent.start is the authority and will reject a genuinely busy pane.
        return bool(pane)

    def _live_agent(self, name: str | None) -> dict[str, Any] | None:
        return self.herdr.agent(name) if name else None

    def _submit_once(self, store: TaskStore, state: dict[str, Any], role: str, prompt: str, *, force_resend: bool = False, target: str | None = None) -> None:
        if role == "escalation_fixer" and state[role].get("terminal_id"):
            owner = state[role]
            live = self._live_agent(owner["name"])
            if not live or live.get("pane_id") != owner["pane"] or live.get("terminal_id") != owner["terminal_id"] or live.get("agent") != "codex" or (owner.get("session") and live.get("agent_session") != owner["session"]):
                raise FlowError("Adopted Codex session identity changed before prompt; refusing delivery")
            screen = self.herdr.read_agent(owner["name"], lines=80)
            displayed = re.findall(r"(?i)\bGPT[- ]6[- ]Sol\s+(minimal|low|medium|high|xhigh|max|ultra)\b", screen)
            if not displayed or displayed[-1].lower() != "high":
                raise FlowError("Adopted Codex no longer confirms GPT-6-Sol high; inspect /model before sending a prompt")
        if role == "coordinator" and not state[role].get("managed"):
            target = state[role].get("pane")
            live = self._live_agent(target) if target else None
            if not live or live.get("pane_id") != target or live.get("agent") != state[role]["kind"] or not state[role].get("session") or live.get("agent_session") != state[role]["session"]:
                raise FlowError("Original attached coordinator session cannot be verified; refusing prompt")
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        existing = state["prompts"].get(role)
        if existing and existing.get("sha256") == digest and existing.get("status") in {"sending", "submitted", "uncertain"} and not force_resend:
            raise FlowError(
                f"A matching prompt is already {existing['status']} for {role}. "
                f"Inspect the live session before any resend: `herdr-flow inspect --task {store.task_id} --role {role}`"
            )
        record = {
            "id": f"{role}-{state.get('iteration', 0)}-{digest[:12]}",
            "sha256": digest,
            "text": prompt,
            "status": "sending",
            "created_at": utc_now(),
            "inspected_at": None,
        }
        state["prompts"][role] = record
        write_json(store.state_path, state)
        try:
            agent = self.herdr.prompt(target or state[role]["name"], prompt)
        except HerdrError as exc:
            record["status"] = "not_sent" if exc.code == "agent_blocked" else "uncertain"
            record["error"] = {"code": exc.code, "message": exc.message}
            if exc.code == "agent_blocked":
                state["user_interaction"] = {
                    "required": True,
                    "blocked_agent": state[role]["name"],
                    "return_phase": state["phase"],
                }
                state["phase"] = "WAITING_FOR_USER"
            write_json(store.state_path, state)
            raise
        if role == "coordinator" and (agent.get("pane_id") != state[role].get("pane") or
                                      agent.get("agent_session") != state[role].get("session")):
            record["status"] = "uncertain"
            record["error"] = {"code": "coordinator_identity_changed", "message": "Prompt target changed during submission; inspect before any retry"}
            write_json(store.state_path, state)
            raise FlowError("Coordinator identity changed during prompt submission; inspect the pane before any retry")
        record["status"] = "submitted"
        record["submitted_at"] = utc_now()
        self._sync_agent(state[role], agent)
        store.event(state, "PROMPT_SUBMITTED", role, record["id"])

    def _worker_prompt(self, store: TaskStore, state: dict[str, Any], *, recovered: bool, fixing: bool, role: str) -> str:
        root = store.root.resolve()
        record = state[role]
        variant = f" with {record['variant']} effort/variant" if record.get("variant") else ""
        assignment = f"Assigned runtime: {record['kind']} model {record.get('model')}{variant}."
        if recovered:
            action = f"This is a recovery session because the previous {role} genuinely disappeared."
        elif fixing and role == "escalation_fixer":
            action = "The cheaper repair path exceeded its configured failure threshold. Take over as the persistent escalation fixer and resolve every applicable finding."
        elif fixing and role == "fixer":
            action = "Review failed. Repair every applicable finding as the dedicated persistent fixer."
        elif fixing:
            action = "Review failed. Fix every applicable finding in the original persistent implementation session."
        else:
            action = "Implement the task in this persistent implementation session."
        files = ["state.json", "handoff.md", "implementation.md", "decisions.md"]
        if fixing:
            files += ["review.md", "final-review.md"]
        refs = "\n".join(f"- {root / name}" for name in files)
        return (
            f"Task {store.task_id}. {action} {assignment}\n\nRead these source-of-truth files:\n{refs}\n"
            f"- {Path(state['project']['worktree_path']) / CONTEXT_FILE}\n"
            "Also read applicable AGENTS.md/CLAUDE.md in the worktree. Check for main-checkout-specific commands and paths before running them.\n\n"
            "Inspect the current worktree instead of asking for repository contents in the prompt. "
            "Implement changes, run relevant tests/lint/type/build checks, and update implementation.md. "
            "Mark implementation.md `## Status` READY when complete, then run "
            f"herdr-flow finish --task {store.task_id} yourself. This automatically starts the next phase; "
            "an idle event also reconciles the report if you cannot run the command. Never ask the user to trigger the next phase.\n"
            "Do not merge, force-push, delete branches/worktrees, discard unrelated work, or copy secrets into workflow files. "
            "If you need a decision or approval, ask visibly and wait in this same Herdr session. Remain alive after finishing."
        )

    def _reviewer_prompt(self, store: TaskStore, state: dict[str, Any], *, recovered: bool) -> str:
        root = store.root.resolve()
        recovery = "This replaces a genuinely lost reviewer session. " if recovered else ""
        record = state["reviewer"]
        variant = f" with {record['variant']} effort/variant" if record.get("variant") else ""
        kimi_note = " If this is a Kimi model, it runs inside OpenCode; do not invoke or install Kimi Code CLI." if "kimi" in str(record.get("model", "")).lower() else ""
        return (
            f"Task {store.task_id}. {recovery}Act as the independent reviewer and release gate. Your assigned runtime is "
            f"{record['kind']} with model {record.get('model')}{variant}.{kimi_note}\n\nRead these source-of-truth files:\n"
            f"- {root / 'state.json'}\n- {root / 'handoff.md'}\n- {root / 'implementation.md'}\n"
            f"- {root / 'decisions.md'}\n"
            f"- {Path(state['project']['worktree_path']) / CONTEXT_FILE}\n\n"
            "Read applicable AGENTS.md/CLAUDE.md in this worktree; validate any main-checkout-specific commands before running them. "
            "Review objective: determine whether the implementation fully satisfies the handoff and is ready to commit and merge without missing requirements, regressions, or unverified critical behavior. "
            "Be skeptical and evidence-driven. Independently inspect the current worktree, the complete relevant diff, repository instructions, and affected call paths. Do not rely on the implementation report or assume that a claimed test passed. "
            "Trace every handoff requirement, acceptance criterion, constraint, and Definition of Done item to code and validation evidence. Run the relevant tests, lint, type checks, builds, and focused checks that are practical in the environment. "
            "Review correctness, architecture, reuse of existing code, edge cases, error handling, backward compatibility, security, performance, memory use, maintainability, dead or obsolete code, dependency/configuration changes, and likely regressions. "
            "Check for unrelated edits, debug artifacts, generated files, credentials, incomplete migrations, missing documentation, and changes that would make the branch unsafe to commit or merge. "
            "Do not invent coverage. Mark anything you cannot verify as NOT VERIFIED and explain why. Prefer concrete findings with severity, file/line evidence, impact, and required correction. Avoid style-only findings unless they violate project rules or create a maintenance risk. "
            "PASS only when all handoff requirements and acceptance criteria are covered, required validation succeeds, no unresolved material finding remains, no critical check is unverified, and the changes are ready to commit and merge. Otherwise FAIL. "
            f"Write {root / 'review.md'} using its sections, set `## Status` to exactly PASS or FAIL, and give an explicit commit/merge-readiness decision. "
            f"After writing the report, run herdr-flow review-result --task {store.task_id} --status PASS|FAIL yourself. "
            "That command automatically routes fixes or final review; an idle event also reconciles the report if the command was not run. "
            "Never ask the user to start the next phase.\n"
            "Do not modify implementation files, fix findings, commit, merge, reset, or discard changes. Remain alive for later review iterations."
        )

    def _sync_agent(self, record: dict[str, Any], agent: dict[str, Any]) -> None:
        record["name"] = agent.get("name") or record.get("name")
        record["pane"] = agent.get("pane_id")
        record["workspace"] = agent.get("workspace_id")
        record["status"] = agent.get("agent_status", record.get("status"))
        session = agent.get("agent_session")
        if session:
            record["session"] = session
        record["last_seen_at"] = utc_now()

    def _link_task_marker(self, worktree: Path, state: dict[str, Any]) -> None:
        marker = {
            "schema_version": 1,
            "task_id": state["task_id"],
            "source_root": str(self.source_root),
            "workflow_root": str(self.workflow_root),
        }
        write_json(worktree / ".herdr-flow-task.json", marker)
        ai = worktree / ".ai"
        ai.mkdir(exist_ok=True)
        link = ai / "workflow"
        if link.exists() or link.is_symlink():
            if link.is_symlink() and link.resolve() == self.workflow_root:
                return
            if not link.is_symlink():
                raise FlowError(f"Refusing to replace existing {link}")
            link.unlink()
        link.symlink_to(self.workflow_root, target_is_directory=True)

    def _branch_exists(self, branch: str) -> bool:
        result = run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=self.source_root, check=False)
        return result.returncode == 0

    def _task_branch(self, title: str, task_id: str, branch_type: str | None = None) -> str:
        if branch_type is not None and branch_type not in BRANCH_TYPES:
            raise FlowError(f"Branch type must be one of: {', '.join(BRANCH_TYPES)}")
        words = set(re.findall(r"[a-z0-9]+", title.lower()))
        if branch_type is None:
            if words & {"fix", "bug", "bugfix", "resolve", "repair", "hotfix"}:
                branch_type = "fix"
            elif words & {"refactor", "refactoring", "optimize", "optimise", "improve", "simplify", "cleanup"}:
                branch_type = "refactor"
            elif words & {"docs", "documentation", "document", "readme"}:
                branch_type = "docs"
            elif words & {"test", "tests", "testing", "coverage"}:
                branch_type = "test"
            elif words & {"chore", "ci", "build", "deps", "dependencies", "tooling", "release"}:
                branch_type = "chore"
            else:
                branch_type = "feat"
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48].strip("-") or "task"
        candidate = f"{branch_type}/{slug}"
        existing = {item.get("project", {}).get("branch") for path in self.workflow_root.glob("task-*/state.json")
                    if isinstance((item := read_json(path)), dict)}
        refs = run(["git", "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"], cwd=self.source_root).stdout.splitlines()
        if any(ref == f"refs/heads/{branch_type}" or
               (ref.startswith("refs/remotes/") and ref.endswith(f"/{branch_type}")) for ref in refs):
            raise FlowError(f"Branch namespace {branch_type}/ conflicts with an existing branch; choose another branch type")
        if candidate in existing or any(ref == f"refs/heads/{candidate}" or
                                        (ref.startswith("refs/remotes/") and ref.endswith(f"/{candidate}")) for ref in refs):
            candidate = f"{candidate}-{task_id}"
        if candidate in existing or any(ref == f"refs/heads/{candidate}" or
                                        (ref.startswith("refs/remotes/") and ref.endswith(f"/{candidate}")) for ref in refs):
            raise FlowError(f"Branch name already exists: {candidate}; choose another title or task ID")
        return candidate

    @staticmethod
    def _task_number(task_id: str) -> int:
        match = re.fullmatch(r"task-(\d+)", task_id)
        return int(match.group(1)) if match else 0

    def _agent_name(self, role: str, task_id: str) -> str:
        digest = hashlib.sha256(str(self.source_root).encode()).hexdigest()[:7]
        prefix = f"{role}-"
        name = f"{prefix}{task_id[:32-len(prefix)-8]}-{digest}"
        if not AGENT_NAME_RE.fullmatch(name):
            raise FlowError(f"Cannot derive valid Herdr agent name from {task_id}")
        return name

    @staticmethod
    def _recovery_name(name: str, count: int) -> str:
        base = re.sub(r"-r\d+$", "", name)
        suffix = f"-r{count}"
        return f"{base[:32-len(suffix)]}{suffix}"

    @staticmethod
    def _selection(default: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
        selection = {
            "kind": default["kind"],
            "model": default.get("model"),
            "variant": default.get("variant"),
        }
        if override:
            if override.get("kind") and override["kind"] != default["kind"]:
                selection["model"] = None
                selection["variant"] = None
            selection.update({key: override[key] for key in ("kind", "model", "variant") if override.get(key) is not None})
            if override.get("clear_variant"):
                selection["variant"] = None
        return selection

    @staticmethod
    def _agent_state(name: str, config: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": name,
            "kind": config["kind"],
            "model": config.get("model"),
            "variant": config.get("variant"),
            "pane": None,
            "workspace": None,
            "session": None,
            "status": "not_started",
            "recovery_count": 0,
        }

    @staticmethod
    def _agent_args(record: dict[str, Any]) -> list[str]:
        model = record.get("model")
        variant = record.get("variant")
        if not model and record["kind"] != "claude":
            return []
        if record["kind"] == "claude":
            args = ["--model", model] if model else []
            if variant:
                args += ["--effort", variant]
            return args
        if record["kind"] == "opencode":
            args = ["--model", model]
            if variant:
                args += ["--variant", variant]
            return args
        if record["kind"] == "codex":
            args = ["-m", model]
            if variant:
                args += ["-c", f'model_reasoning_effort="{variant}"']
            return args
        if record["kind"] == "pi":
            args = ["--model", model]
            if variant:
                args += ["--thinking", variant]
            return args
        raise FlowError(f"Model arguments are not defined for agent kind {record['kind']}")

    @staticmethod
    def _role_for_name(state: dict[str, Any], name: str | None) -> str:
        for role in ("coordinator", *SESSION_ROLES):
            record = state.get(role)
            if record and record.get("name") == name:
                return role
        return "worker"


def doctor() -> int:
    problems: list[str] = []
    notes: list[str] = []
    required = ["git", "python3", "herdr"]
    try:
        selected = load_config()
        kinds = {selected["agents"][role]["kind"] for role in ("coordinator", "worker", "reviewer")}
        if selected["repair"]["escalation"].get("enabled"):
            kinds.add(selected["repair"]["escalation"]["agent"]["kind"])
        required.extend(sorted(kinds))
    except FlowError as exc:
        problems.append(str(exc))
    for command in required:
        path = shutil.which(command)
        if path:
            notes.append(f"ok  {command}: {path}")
        else:
            problems.append(f"missing command: {command}")
    try:
        config = load_config()
        coordinator = config["agents"]["coordinator"]
        if coordinator["kind"] not in {"claude", *AGENT_KINDS}:
            raise FlowError(f"Unsupported default coordinator kind: {coordinator['kind']}")
        if coordinator["kind"] != "claude":
            validate_agent_selection(coordinator["kind"], coordinator.get("model"), coordinator.get("variant"))
        notes.append(f"ok  default coordinator: {coordinator['kind']} {coordinator.get('model') or '(current session)'}")
        for role in ("worker", "reviewer"):
            selection = config["agents"][role]
            validate_agent_selection(selection["kind"], selection["model"], selection.get("variant"))
            variant = f" @{selection['variant']}" if selection.get("variant") else ""
            notes.append(f"ok  default {role}: {selection['kind']} {selection['model']}{variant}")
        if config["agents"]["reviewer"]["kind"] == "opencode" and "kimi" in config["agents"]["reviewer"]["model"].lower():
            notes.append("ok  default reviewer uses OpenCode with a Kimi model (no Kimi CLI)")
        escalation = config["repair"]["escalation"]
        if escalation.get("enabled"):
            agent = escalation["agent"]
            validate_agent_selection(agent["kind"], agent["model"], agent.get("variant"))
            notes.append(
                f"ok  escalation fixer after >{escalation['after_failures']} failures: "
                f"{agent['kind']} {agent['model']} @{agent.get('variant') or 'default'}"
            )
    except FlowError as exc:
        problems.append(str(exc))
    try:
        version = run(["herdr", "--version"]).stdout.strip()
        notes.append(f"ok  {version}")
    except FlowError as exc:
        problems.append(str(exc))
    if os.environ.get("HERDR_ENV") != "1":
        notes.append("note live agent acceptance tests require running this command inside a Herdr pane")
    print("\n".join(notes))
    if problems:
        print("\nProblems:", file=sys.stderr)
        print("\n".join(f"- {item}" for item in problems), file=sys.stderr)
        return 1
    return 0


def print_status(state: dict[str, Any]) -> None:
    print(f"{state['task_id']} — {state['title']}")
    print(f"phase: {state['phase']}  iteration: {state['iteration']}")
    automation = state.get("automation", {})
    print(f"automation: {'on' if automation.get('enabled') else 'off'}")
    if automation.get("attention"):
        print(f"ATTENTION: {automation['attention']}")
    if state.get("cleanup", {}).get("completed"):
        print("cleanup: merged task worktree removed")
    elif state["phase"] == "DONE":
        print("cleanup: awaiting verified merge and clean worktree; run herdr-flow finalize after merge if no Herdr event occurs")
    print(f"branch: {state['project']['branch']}")
    print(f"worktree: {state['project'].get('worktree_path') or '-'}")
    for notice in state.get("instruction_notices", []):
        print(f"INSTRUCTION ATTENTION: {notice}")
    print(f"review: {state['review']['status']} (attempt {state['review']['attempt']})")
    print(f"final review: {state['final_review']['status']} (attempt {state['final_review']['attempt']})")
    repair = state.get("repair", {})
    escalation = repair.get("escalation", {})
    print(
        f"repair: failures={repair.get('failure_count', 0)} fixes={repair.get('fix_attempts', 0)} "
        f"active={repair.get('active_role', 'worker')} base={repair.get('base_role', 'worker')} "
        f"escalation={'on' if escalation.get('enabled') else 'off'} after>{escalation.get('after_failures', '-')}"
    )
    for role in ("coordinator", *SESSION_ROLES):
        item = state.get(role)
        if not item:
            continue
        live = " live" if item.get("live") else ""
        status = item.get("live_status") or item.get("status")
        variant = f" @{item.get('variant')}" if item.get("variant") else ""
        print(f"{role}: {item.get('name')} [{item.get('kind')} {item.get('model') or ''}{variant}] {status}{live} pane={item.get('live_pane') or item.get('pane') or '-'}")
    if state["user_interaction"].get("required"):
        print(f"ACTION REQUIRED: answer {state['user_interaction']['blocked_agent']} in the same Herdr session")
    for role in ("coordinator", *SESSION_ROLES):
        prompt = state["prompts"].get(role)
        if prompt and prompt.get("status") in {"sending", "submitted", "uncertain"}:
            print(f"prompt guard: {role} {prompt['status']} id={prompt['id']}")


def add_agent_selection_args(parser: argparse.ArgumentParser, prefix: str = "") -> None:
    option = f"{prefix}-" if prefix else ""
    dest = f"{prefix}_" if prefix else ""
    kinds = {"claude", *AGENT_KINDS} if prefix in {"coordinator", ""} else AGENT_KINDS
    parser.add_argument(f"--{option}kind", dest=f"{dest}kind", choices=sorted(kinds))
    parser.add_argument(f"--{option}model", dest=f"{dest}model")
    parser.add_argument(
        f"--{option}variant",
        dest=f"{dest}variant",
        choices=["default", *sorted(VARIANTS)],
        help="OpenCode variant or Pi/Codex/Claude thinking effort; 'default' clears an inherited value",
    )


def pi_models() -> dict[str, bool]:
    """Read Pi's installed provider/model table and its advertised thinking flag."""
    result = run(["pi", "--list-models"], check=False)
    if result.returncode != 0:
        raise FlowError(f"Could not list Pi models: {(result.stderr or result.stdout).strip()}")
    models: dict[str, bool] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[0] != "provider" and fields[4] in {"yes", "no"}:
            models[f"{fields[0]}/{fields[1]}"] = fields[4] == "yes"
    if not models:
        raise FlowError("Pi returned no parseable provider/model rows; check pi --list-models")
    return models


def opencode_variants(model: str) -> set[str]:
    """Read effective OpenCode metadata, not a model's generic reasoning flag."""
    provider = model.split("/", 1)[0]
    if not provider:
        raise FlowError("OpenCode models require a provider prefix")
    result = run(["opencode", "models", provider, "--verbose"], check=False)
    if result.returncode != 0:
        raise FlowError(f"Could not inspect OpenCode variants: {(result.stderr or result.stdout).strip()}")
    match = re.search(rf"(?m)^{re.escape(model)}\r?\n", result.stdout)
    if not match:
        raise FlowError(f"OpenCode model not found in effective catalog: {model}")
    try:
        details, _ = json.JSONDecoder().raw_decode(result.stdout[match.end():].lstrip())
    except json.JSONDecodeError as exc:
        raise FlowError(f"Invalid OpenCode model metadata for {model}") from exc
    variants = details.get("variants")
    if not isinstance(variants, dict):
        raise FlowError(f"OpenCode did not provide variant metadata for {model}")
    return set(variants) & VARIANTS


def _menu(label: str, options: list[str]) -> str:
    if not options:
        raise FlowError(f"No choices found for {label}")
    print(f"\n{label}:")
    for number, item in enumerate(options, 1):
        print(f"  {number}. {item}")
    while True:
        try:
            answer = input("Choose number (or q to cancel): ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise FlowError("Switch cancelled; no defaults changed") from exc
        if answer.lower() == "q":
            raise FlowError("Switch cancelled; no defaults changed")
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        print("Enter a number from the list.")


def _catalog(kind: str) -> list[str]:
    if kind == "opencode":
        result = run(["opencode", "models"], check=False)
        if result.returncode != 0:
            raise FlowError(f"Could not list OpenCode models: {(result.stderr or result.stdout).strip()}")
        return sorted(set(result.stdout.splitlines()))
    if kind == "pi":
        return sorted(pi_models())
    result = run(["codex", "debug", "models"], check=False)
    if result.returncode != 0:
        raise FlowError(f"Could not list Codex models: {(result.stderr or result.stdout).strip()}")
    try:
        return [item["slug"] for item in json.loads(result.stdout)["models"]]
    except (ValueError, KeyError, TypeError) as exc:
        raise FlowError("Codex returned an invalid model catalog") from exc


def _choose_model(kind: str) -> str:
    models = _catalog(kind)
    while True:
        try:
            query = input(f"Search {kind} model (name or provider; q cancels): ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise FlowError("Switch cancelled; no defaults changed") from exc
        if query.lower() == "q":
            raise FlowError("Switch cancelled; no defaults changed")
        matching = [model for model in models if query.lower() in model.lower()]
        if 0 < len(matching) <= 30:
            return _menu("Model", matching)
        print(f"{len(matching)} matches; narrow the search to 1–30 models.")


def switch_default(args: argparse.Namespace) -> None:
    role = args.role
    if not role:
        if not sys.stdin.isatty():
            raise FlowError("Non-interactive switch requires --role")
        role = _menu("Role", ["coordinator", "worker", "reviewer", "escalation"])
    local = read_defaults_override()
    if args.reset:
        if any(value is not None for value in (args.kind, args.model, args.variant)):
            raise FlowError("--reset cannot be combined with kind, model, or variant")
        local["roles"].pop(role, None)
        write_json(defaults_override_path(), local)
        print(f"Reset {role} to the repository default; existing tasks unchanged")
        return
    interactive = sys.stdin.isatty()
    if not interactive and (not args.kind or (args.kind != "claude" and not args.model)):
        raise FlowError("Non-interactive switch requires --role, --kind, and --model (except for Claude)")
    kind = args.kind or _menu("Agent", ["claude", "codex", "opencode", "pi"] if role == "coordinator" else ["codex", "opencode", "pi"])
    if kind == "claude" and role != "coordinator":
        raise FlowError("Only the coordinator supports an unpinned Claude session")
    if kind == "claude" and args.model:
        raise FlowError("Use an unpinned Claude coordinator, without --model")
    model = None if kind == "claude" else (args.model or _choose_model(kind))
    if args.variant is not None:
        variant = None if args.variant == "default" else args.variant
    elif interactive:
        if kind == "claude":
            levels = ["default", *sorted(VARIANTS - {"minimal", "ultra"})]
        elif kind == "opencode":
            levels = ["default", *sorted(opencode_variants(model))]
        elif kind == "pi":
            levels = ["default", "off"] if not pi_models()[model] else ["default", *sorted(PI_THINKING)]
        else:
            levels = ["default", *sorted(VARIANTS)]
        variant = _menu("Thinking / variant", levels)
        variant = None if variant == "default" else variant
    else:
        variant = None
    if kind != "claude":
        validate_agent_selection(kind, model, variant)
    elif variant not in {None, "low", "medium", "high", "xhigh", "max"}:
        raise FlowError("Unsupported Claude effort")
    choice = {"kind": kind, "model": model, "variant": variant}
    if interactive:
        print(f"\nNew {role} default: {json.dumps(choice)}")
        try:
            confirm = input("Save for future tasks? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt) as exc:
            raise FlowError("Switch cancelled; no defaults changed") from exc
        if confirm not in {"y", "yes"}:
            raise FlowError("Switch cancelled; no defaults changed")
    local["roles"][role] = choice
    write_json(defaults_override_path(), local)
    print(f"Saved {role} default to {defaults_override_path()}; existing tasks unchanged")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="herdr-flow", description=__doc__)
    parser.add_argument("--project", type=Path, help="Project checkout used to discover .ai/workflow")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("defaults")
    switch = sub.add_parser("switch", help="Pick a new role default for future tasks (interactive without flags)")
    switch.add_argument("--role", choices=sorted(DEFAULT_ROLES))
    switch.add_argument("--kind", choices=["claude", "codex", "opencode", "pi"])
    switch.add_argument("--model", help="Exact model ID; prompts from installed catalog if omitted")
    switch.add_argument("--variant", choices=["default", *sorted(VARIANTS)], help="Reasoning effort or OpenCode variant; default clears it")
    switch.add_argument("--reset", action="store_true", help="Remove this role's local override")
    sub.add_parser("init")
    create = sub.add_parser("create")
    create.add_argument("--title", required=True)
    create.add_argument("--branch-type", choices=BRANCH_TYPES, help="Override the inferred feat/fix/refactor/docs/test/chore branch prefix")
    create.add_argument("--task")
    create.add_argument("--base")
    add_agent_selection_args(create, "coordinator")
    add_agent_selection_args(create, "worker")
    add_agent_selection_args(create, "fixer")
    add_agent_selection_args(create, "reviewer")
    add_agent_selection_args(create, "escalation")
    create.add_argument("--escalate-after", type=int)
    create.add_argument("--disable-escalation", action="store_true")
    create.add_argument("--plan-only", action="store_true", help="Ask a managed coordinator for a handoff without automatically starting implementation")
    for name in ("start-coordinator", "run", "advance", "finalize", "implement", "fix", "review", "finish", "resume", "final-start", "status", "refresh-instructions", "reconcile-branch"):
        item = sub.add_parser(name)
        item.add_argument("--task")
        if name in {"implement", "fix", "review"}:
            add_agent_selection_args(item)
        if name == "fix":
            add_agent_selection_args(item, "escalation")
            item.add_argument("--escalate-after", type=int)
            item.add_argument("--disable-escalation", action="store_true")
        if name == "reconcile-branch":
            item.add_argument("--branch", required=True)
            item.add_argument("--yes", action="store_true")
        if name == "refresh-instructions":
            item.add_argument("--yes", action="store_true")
        if name == "status":
            item.add_argument("--json", action="store_true")
            item.add_argument("--watch", action="store_true")
    adopt = sub.add_parser("adopt-startup")
    adopt.add_argument("--task", required=True)
    adopt.add_argument("--pane", required=True)
    adopt.add_argument("--model", required=True)
    adopt.add_argument("--variant", required=True, choices=["high"])
    adopt.add_argument("--yes", action="store_true")
    configure = sub.add_parser("configure-agent")
    configure.add_argument("--task")
    configure.add_argument("--role", required=True, choices=list(CONFIGURABLE_ROLES))
    add_agent_selection_args(configure)
    rr = sub.add_parser("review-result")
    rr.add_argument("--task")
    rr.add_argument("--status", required=True, choices=["PASS", "FAIL", "pass", "fail"])
    fr = sub.add_parser("final-result")
    fr.add_argument("--task")
    fr.add_argument("--status", required=True, choices=["PASS", "FAIL", "pass", "fail"])
    focus = sub.add_parser("focus")
    focus.add_argument("--task")
    focus.add_argument("--role", required=True, choices=["coordinator", *SESSION_ROLES])
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--task")
    inspect.add_argument("--role", required=True, choices=["coordinator", *SESSION_ROLES])
    resend = sub.add_parser("resend")
    resend.add_argument("--task")
    resend.add_argument("--role", required=True, choices=["coordinator", *SESSION_ROLES])
    resend.add_argument("--yes", action="store_true")
    sub.add_parser("status-popup")
    sub.add_parser("event")
    return parser.parse_args(argv)


def selection_from_args(args: argparse.Namespace, prefix: str = "") -> dict[str, Any] | None:
    stem = f"{prefix}_" if prefix else ""
    kind = getattr(args, f"{stem}kind", None)
    model = getattr(args, f"{stem}model", None)
    raw_variant = getattr(args, f"{stem}variant", None)
    if kind is None and model is None and raw_variant is None:
        return None
    selection: dict[str, Any] = {"kind": kind, "model": model}
    if raw_variant == "default":
        selection["clear_variant"] = True
    elif raw_variant is not None:
        selection["variant"] = raw_variant
    return selection


def configure_from_args(workflow: Workflow, store: TaskStore, role: str, args: argparse.Namespace) -> dict[str, Any] | None:
    selection = selection_from_args(args)
    if not selection:
        return None
    raw_variant = getattr(args, "variant", None)
    return workflow.configure_role(
        store,
        role,
        kind=selection.get("kind"),
        model=selection.get("model"),
        variant=selection.get("variant"),
        variant_set=raw_variant is not None,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.command == "doctor":
        return doctor()
    if args.command == "defaults":
        config = load_config()
        roles = {role: (config["repair"]["escalation"]["agent"] if role == "escalation" else config["agents"][role]) for role in ("coordinator", "worker", "reviewer", "escalation")}
        print(json.dumps({role: {key: choice.get(key) for key in ("kind", "model", "variant")} for role, choice in roles.items()}, indent=2))
        return 0
    if args.command == "switch":
        switch_default(args)
        return 0
    if args.command == "status-popup":
        HerdrAdapter().status_popup()
        return 0
    if args.command == "event":
        raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON")
        if not raw:
            return 0
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return 0
        # Event handling resolves the owning task through the plugin registry.
        dummy = Workflow(Path.cwd(), Path.cwd(), load_config())
        dummy.handle_event(event)
        return 0

    start = (args.project or context_cwd()).expanduser().resolve()
    workflow = Workflow.discover(start, create=args.command in {"init", "create"})
    if args.command == "init":
        workflow.init()
        print(workflow.workflow_root)
        return 0
    if args.command == "create":
        store = workflow.create(
            args.title,
            args.task,
            args.base,
            worker=selection_from_args(args, "worker"),
            reviewer=selection_from_args(args, "reviewer"),
            coordinator=selection_from_args(args, "coordinator"),
            fixer=selection_from_args(args, "fixer"),
            escalation_fixer=selection_from_args(args, "escalation"),
            escalate_after=args.escalate_after,
            disable_escalation=args.disable_escalation,
            plan_only=args.plan_only,
            branch_type=args.branch_type,
        )
        print(json.dumps({"task_id": store.task_id, "root": str(store.root), "brief": str(store.root / "brief.md"), "handoff": str(store.root / "handoff.md"), "coordinator": store.read()["coordinator"]["kind"]}, indent=2))
        return 0

    store = workflow.resolve_task(getattr(args, "task", None))
    if args.command == "start-coordinator":
        state = workflow.start_coordinator(store)
    elif args.command == "run":
        state = workflow.run_task(store)
    elif args.command == "advance":
        state = workflow.advance(store)
    elif args.command == "finalize":
        state = workflow.finalize(store)
    elif args.command == "reconcile-branch":
        state = workflow.reconcile_branch(store, args.branch, yes=args.yes)
    elif args.command == "refresh-instructions":
        state = workflow.refresh_instructions(store, yes=args.yes)
    elif args.command == "adopt-startup":
        state = workflow.adopt_startup(store, pane=args.pane, model=args.model, variant=args.variant, yes=args.yes)
    elif args.command == "configure-agent":
        state = configure_from_args(workflow, store, args.role, args)
        if state is None:
            raise FlowError("configure-agent requires at least one of --kind, --model, or --variant")
    elif args.command == "implement":
        configure_from_args(workflow, store, "worker", args)
        state = workflow.implement(store)
    elif args.command == "fix":
        configure_from_args(workflow, store, "fixer", args)
        escalation_selection = selection_from_args(args, "escalation")
        if escalation_selection:
            raw_variant = getattr(args, "escalation_variant", None)
            workflow.configure_role(
                store,
                "escalation_fixer",
                kind=escalation_selection.get("kind"),
                model=escalation_selection.get("model"),
                variant=escalation_selection.get("variant"),
                variant_set=raw_variant is not None,
            )
        if args.escalate_after is not None or args.disable_escalation:
            workflow.configure_escalation(
                store,
                after_failures=args.escalate_after,
                enabled=False if args.disable_escalation else None,
            )
        state = workflow.implement(store)
    elif args.command == "finish":
        state = workflow.finish(store)
    elif args.command == "review":
        configure_from_args(workflow, store, "reviewer", args)
        state = workflow.review(store)
    elif args.command == "review-result":
        state = workflow.review_result(store, args.status)
    elif args.command == "final-start":
        state = workflow.final_start(store)
    elif args.command == "final-result":
        state = workflow.final_result(store, args.status)
    elif args.command == "resume":
        state = workflow.resume(store)
    elif args.command == "focus":
        print(json.dumps(workflow.focus(store, args.role), indent=2))
        return 0
    elif args.command == "inspect":
        print(workflow.inspect_agent(store, args.role))
        return 0
    elif args.command == "resend":
        state = workflow.resend(store, args.role, args.yes)
    elif args.command == "status":
        if args.watch:
            try:
                while True:
                    os.system("clear")
                    print_status(workflow.status(store))
                    print("\nRefresh: 2s · close with Ctrl+C")
                    time.sleep(2)
            except KeyboardInterrupt:
                return 0
        state = workflow.status(store)
        if args.json:
            print(json.dumps(state, indent=2))
            return 0
    else:
        raise FlowError(f"Unsupported command: {args.command}")
    if args.command in {"finish", "review-result", "final-result"} and state.get("automation", {}).get("enabled"):
        state = workflow.advance(store)
    print_status(state)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FlowError, HerdrError) as exc:
        print(f"herdr-flow: {exc}", file=sys.stderr)
        raise SystemExit(1)
