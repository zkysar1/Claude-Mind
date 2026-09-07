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
    # The ADDITION case deliberately uses a non-skill framework path: a NEW
    # .claude/skills/<name>/ is a destination-owned forged body and is exempt by
    # design () — pinned separately below.
    root = _repo(tmp_path)
    (root / "core" / "scripts" / "added.py").write_text("print(3)\n", encoding="utf-8")
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


# ------------------------------------------- forged-skill carve-out ()
#
# The promotion train does not carry forged skill bodies — seed-manifest.yaml
# excludes them, _seed_engine._is_protected_dest_skill refuses to delete them as
# orphans, promotion-preflight.py buckets .claude/skills/** out of blocking drift.
# The blanket ".claude/" refusal disagreed, so a forged skill had no home on any
# framework_origin deployment (measured 2026-09-05, coach@zc-03: the Write refused,
# the registry row written anyway, zero skill forged — rb-10227).
#
# Every ALLOW case below is paired with the base-skill DENY that must NOT flip:
# the carve-out's whole risk is re-opening the worksheet hole the refusal closed
# (guard-4166 — a fix whose effect is an absence needs its positive control).

def _forged_skill(root: Path, name: str, tag: str = "forged: true") -> Path:
    d = root / ".claude" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n{tag}\n---\n# body\n", encoding="utf-8")
    return d / "SKILL.md"


@pytest.mark.parametrize("setup,rel,expected", [
    # a skill upstream does not have: nothing to clobber
    ("none", ".claude/skills/brand-new/SKILL.md", True),
    # the `mkdir -p` residue the measured run left behind is still "new"
    ("empty-dir", ".claude/skills/brand-new/SKILL.md", True),
    # a body this deployment forged: revising it is local work
    ("forged", ".claude/skills/brand-new/SKILL.md", True),
    ("forged", ".claude/skills/brand-new/reference.md", True),
    # a BASE skill the train owns — the measured worksheet target
    ("base", ".claude/skills/brand-new/SKILL.md", False),
    ("base", ".claude/skills/brand-new/notes.md", False),
    # not a skill path at all
    ("none", "core/scripts/aspirations.py", False),
    ("none", "CLAUDE.md", False),
    ("none", ".claude/skills/", False),
    ("none", ".claude/skills/../../etc/passwd", False),
])
def test_is_forged_skill_body(tmp_path, setup, rel, expected):
    from _framework_origin import is_forged_skill_body
    (tmp_path / ".claude" / "skills").mkdir(parents=True)  # a real checkout has one
    if setup == "empty-dir":
        (tmp_path / ".claude" / "skills" / "brand-new").mkdir(parents=True)
    elif setup == "forged":
        _forged_skill(tmp_path, "brand-new")
    elif setup == "base":
        _forged_skill(tmp_path, "brand-new", tag="description: a base skill")
    assert is_forged_skill_body(rel, tmp_path) is expected


def test_is_forged_skill_body_fails_toward_refusal(tmp_path):
    """Fail direction is REFUSE here, unlike framework_origin()'s fail-open — a wrong
    ALLOW re-opens the worksheet hole, a wrong refusal only asks for another name."""
    from _framework_origin import is_forged_skill_body
    # unusable project_root: Path(None) raises, and the except must not allow
    assert is_forged_skill_body(".claude/skills/x/SKILL.md", None) is False
    # a root with no .claude/skills at all is not a checkout — every skill path
    # would otherwise read as brand-new and exempt the whole surface
    assert is_forged_skill_body(".claude/skills/x/SKILL.md", tmp_path) is False


def test_path_resolution_hook_allows_a_new_forged_skill_body(tmp_path):
    root = _project(tmp_path)
    _registry(root)
    r = _hook(root, str(root / ".claude/skills/query-scouting-matchups/SKILL.md"), tool="Write")
    assert r["decision"] == "approve", r


def test_path_resolution_hook_allows_revising_a_forged_skill_body(tmp_path):
    root = _project(tmp_path)
    _registry(root)
    body = _forged_skill(root, "query-scouting-matchups")
    assert _hook(root, str(body))["decision"] == "approve"


def test_path_resolution_hook_still_denies_the_base_skill_worksheet_edit(tmp_path):
    """Positive control for the carve-out: the measured 2026-08-30 edit stays denied."""
    root = _project(tmp_path)
    _registry(root)
    r = _hook(root, str(root / ".claude/skills/curriculum-gates/SKILL.md"))
    assert r["decision"] == "deny", r
    # ... and so does a NEW file smuggled into a base skill's dir, which the
    # "does it exist yet" question alone would have let through.
    r2 = _hook(root, str(root / ".claude/skills/curriculum-gates/results.md"), tool="Write")
    assert r2["decision"] == "deny", r2


def test_path_resolution_hook_deny_text_names_the_real_forged_home(tmp_path):
    root = _project(tmp_path)
    _registry(root)
    reason = _hook(root, str(root / ".claude/skills/curriculum-gates/SKILL.md"))["reason"]
    assert ".claude/skills/<new-name>/SKILL.md IS " in reason
    assert "no writable home" not in reason


def test_gate_allows_a_forged_skill_addition_but_not_a_base_skill_edit(tmp_path):
    root = _repo(tmp_path)
    _forged_skill(root, "query-scouting-matchups")
    _git(root, "add", "-A")
    first = _gate(root)
    assert first.returncode == 0, first.stderr
    # positive control: a base skill in the same commit still refuses
    _forged_skill(root, "curriculum-gates", tag="description: a base skill")
    _git(root, "add", "-A")
    assert _gate(root).returncode == 1


def test_gate_still_refuses_deleting_a_forged_skill_body(tmp_path):
    """Un-forging is a deliberate act — and at a staged delete the body is already
    gone, so 'absent means new' cannot tell a forged retirement from a base removal."""
    root = _repo(tmp_path)
    _forged_skill(root, "query-scouting-matchups")
    _git(root, "add", "-A")
    assert _git(root, "commit", "-q", "-m", "forge").returncode == 0
    _git(root, "rm", "-q", ".claude/skills/query-scouting-matchups/SKILL.md")
    assert _gate(root).returncode == 1
