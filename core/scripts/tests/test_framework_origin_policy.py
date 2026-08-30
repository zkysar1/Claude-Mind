"""test_framework_origin_policy.py — `framework_origin` refuses LOCAL framework writes
on a deployment that takes its framework from another one (pull-promotion.md § g).

Three surfaces, each with its positive control:
  - _framework_origin.py policy + path set (unit)
  - path-resolution-hook.py L1 deny (subprocess, synthetic Edit payload)
  - check-framework-origin-writes.py pre-commit Gate 15 (subprocess, real tmp git repo)

The measured shape (2026-08-30, coach@zc-03): a Body edit_file'd its step results
into .claude/skills/curriculum-gates/SKILL.md. The first hook case replays it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
REPO = CORE_SCRIPTS.parent.parent
HOOK = CORE_SCRIPTS / "path-resolution-hook.py"
GATE = CORE_SCRIPTS / "check-framework-origin-writes.py"
sys.path.insert(0, str(CORE_SCRIPTS))
from _framework_origin import framework_origin, is_framework_path  # noqa: E402

ENV = "testenv-downstream"


def _registry(root: Path, env_id: str = ENV, origin: str | None = "testenv-origin") -> None:
    d = root / "core" / "config" / "environments"
    d.mkdir(parents=True, exist_ok=True)
    body = f"environment_id: {env_id}\nbackend: local\n"
    if origin is not None:
        body += f"framework_origin: {origin}\n"
    (d / f"{env_id}.yaml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------- unit: path set + policy

@pytest.mark.parametrize("rel,expected", [
    (".claude/skills/curriculum-gates/SKILL.md", True),
    ("core/scripts/aspirations.py", True),
    ("./core/config/aspirations.yaml", True),
    ("mind_api/src/server.py", True),
    ("CLAUDE.md", True),
    ("core/logs/watchdog-coach.jsonl", False),
    ("mind_api/state/daemon.port", False),
    ("agents/coach/self.md", False),
    ("readme/x.md", False),
    (".env.local", False),
    ("yahoo/client.py", False),
    ("", False),
])
def test_is_framework_path(rel, expected):
    assert is_framework_path(rel) is expected


def test_origin_resolves_from_registry(tmp_path):
    _registry(tmp_path)
    assert framework_origin(tmp_path, ENV) == "testenv-origin"


def test_absent_field_means_origin(tmp_path):
    _registry(tmp_path, origin=None)
    assert framework_origin(tmp_path, ENV) is None


def test_self_referencing_and_blank_and_unknown_env_are_origin(tmp_path):
    _registry(tmp_path, origin=ENV)
    assert framework_origin(tmp_path, ENV) is None
    _registry(tmp_path, origin="")
    assert framework_origin(tmp_path, ENV) is None
    _registry(tmp_path)
    assert framework_origin(tmp_path, "never-registered") is None
    assert framework_origin(tmp_path, "") is None


def test_unreadable_registry_fails_open(tmp_path):
    d = tmp_path / "core" / "config" / "environments"
    d.mkdir(parents=True)
    (d / f"{ENV}.yaml").write_text("environment_id: [unterminated\n", encoding="utf-8")
    assert framework_origin(tmp_path, ENV) is None
    assert framework_origin(tmp_path / "does-not-exist", ENV) is None


# ---------------------------------------------------------------- L1 hook

def _hook(root: Path, file_path: str, env_id: str = ENV, tool: str = "Edit") -> dict:
    payload = json.dumps({"tool_name": tool, "tool_input": {"file_path": file_path},
                          "session_id": "test-session"})
    env = os.environ.copy()
    env["PROJECT_ROOT"] = str(root)
    env["MIND_AGENT"] = "agentx"
    env["ENVIRONMENT_ID"] = env_id
    res = subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True,
                         text=True, env=env, cwd=str(root), timeout=60)
    if not res.stdout.strip():
        return {"decision": "approve", "reason": ""}
    hs = json.loads(res.stdout).get("hookSpecificOutput", {})
    return {"decision": hs.get("permissionDecision", "approve"),
            "reason": hs.get("permissionDecisionReason", "")}


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    for sub in (".claude/skills/curriculum-gates", "core/scripts", "core/logs",
                "agents/agentx/temp", "mind_api/src"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / ".claude/skills/curriculum-gates/SKILL.md").write_text("# skill\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("# claude\n", encoding="utf-8")
    return root


def test_hook_denies_the_measured_skill_worksheet_edit(tmp_path):
    root = _project(tmp_path)
    _registry(root)
    r = _hook(root, str(root / ".claude/skills/curriculum-gates/SKILL.md"))
    assert r["decision"] == "deny"
    assert "framework_origin: testenv-origin" in r["reason"]
    assert "worksheet" in r["reason"]
    assert "cross-world-inject-goal.sh --target testenv-origin" in r["reason"]


@pytest.mark.parametrize("rel", ["core/scripts/new-tool.py", "CLAUDE.md", "mind_api/src/x.py"])
def test_hook_denies_every_framework_surface(tmp_path, rel):
    root = _project(tmp_path)
    _registry(root)
    assert _hook(root, str(root / rel), tool="Write")["decision"] == "deny"


def test_hook_positive_control_origin_deployment_still_edits_framework(tmp_path):
    root = _project(tmp_path)
    _registry(root, origin=None)
    r = _hook(root, str(root / ".claude/skills/curriculum-gates/SKILL.md"))
    assert r["decision"] == "approve", r


def test_hook_leaves_non_framework_and_runtime_paths_alone(tmp_path):
    root = _project(tmp_path)
    _registry(root)
    assert _hook(root, str(root / "agents/agentx/temp/notes.md"), tool="Write")["decision"] == "approve"
    assert _hook(root, str(root / "core/logs/watchdog-agentx.jsonl"), tool="Write")["decision"] == "approve"


def test_hook_unknown_env_fails_open(tmp_path):
    root = _project(tmp_path)
    _registry(root)
    r = _hook(root, str(root / "core/scripts/new-tool.py"), env_id="not-registered", tool="Write")
    assert r["decision"] == "approve", r


# ---------------------------------------------------------------- pre-commit Gate 15

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)


def _repo(tmp_path: Path, origin: str | None = "testenv-origin") -> Path:
    root = tmp_path / "repo"
    (root / "core" / "scripts").mkdir(parents=True)
    (root / "agents" / "agentx").mkdir(parents=True)
    (root / "core" / "scripts" / "tool.py").write_text("print(1)\n", encoding="utf-8")
    (root / "agents" / "agentx" / "self.md").write_text("# me\n", encoding="utf-8")
    _registry(root, origin=origin)
    assert _git(root, "init", "-q").returncode == 0
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "-A")
    assert _git(root, "commit", "-q", "-m", "init").returncode == 0
    return root


def _gate(root: Path, env_id: str = ENV, override: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["ENVIRONMENT_ID"] = env_id
    env.pop("FRAMEWORK_WRITE_OVERRIDE", None)
    if override is not None:
        env["FRAMEWORK_WRITE_OVERRIDE"] = override
    return subprocess.run([sys.executable, str(GATE), str(root)], capture_output=True,
                          text=True, env=env, cwd=str(root), timeout=60)


def test_gate_refuses_staged_framework_modification_with_routing(tmp_path):
    root = _repo(tmp_path)
    (root / "core" / "scripts" / "tool.py").write_text("print(2)\n", encoding="utf-8")
    _git(root, "add", "-A")
    r = _gate(root)
    assert r.returncode == 1
    assert "core/scripts/tool.py" in r.stderr
    assert "framework_origin: testenv-origin" in r.stderr
    assert "git checkout HEAD --" in r.stderr
    assert "cross-world-inject-goal.sh --target testenv-origin" in r.stderr


def test_gate_refuses_framework_addition_and_deletion(tmp_path):
    root = _repo(tmp_path)
    (root / ".claude" / "skills" / "x").mkdir(parents=True)
    (root / ".claude" / "skills" / "x" / "SKILL.md").write_text("# x\n", encoding="utf-8")
    _git(root, "add", "-A")
    assert _gate(root).returncode == 1
    _git(root, "reset", "-q")
    _git(root, "rm", "-q", "core/scripts/tool.py")
    assert _gate(root).returncode == 1


def test_gate_positive_control_origin_deployment_passes(tmp_path):
    root = _repo(tmp_path, origin=None)
    (root / "core" / "scripts" / "tool.py").write_text("print(2)\n", encoding="utf-8")
    _git(root, "add", "-A")
    assert _gate(root).returncode == 0


def test_gate_non_framework_change_passes(tmp_path):
    root = _repo(tmp_path)
    (root / "agents" / "agentx" / "self.md").write_text("# me v2\n", encoding="utf-8")
    _git(root, "add", "-A")
    r = _gate(root)
    assert r.returncode == 0, r.stderr


def test_gate_override_passes_and_blank_override_refuses(tmp_path):
    root = _repo(tmp_path)
    (root / "core" / "scripts" / "tool.py").write_text("print(2)\n", encoding="utf-8")
    _git(root, "add", "-A")
    r = _gate(root, override="seed-transplant plant")
    assert r.returncode == 0
    assert "OVERRIDDEN" in r.stderr
    assert _gate(root, override="   ").returncode == 1


def test_gate_nothing_staged_passes(tmp_path):
    root = _repo(tmp_path)
    assert _gate(root).returncode == 0


def test_gate_is_wired_into_pre_commit_and_plant_carries_override():
    hook = (REPO / "core" / "githooks" / "pre-commit").read_text(encoding="utf-8")
    assert "check-framework-origin-writes.py" in hook
    plant = (REPO / "core" / "scripts" / "seed-transplant.sh").read_text(encoding="utf-8")
    assert 'FRAMEWORK_WRITE_OVERRIDE="seed-transplant plant' in plant
