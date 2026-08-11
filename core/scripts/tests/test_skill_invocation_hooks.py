"""Behavioural pins for the two skill-invocation telemetry hooks.

`context-reads-skill-gate.sh` (PreToolUse[Skill], model-invoked path) and
`user-prompt-skill-record.sh` (UserPromptExpansion, user-typed path) both append
to `agents/<agent>/skill-invocations.jsonl`. Both carry the IRREDUCIBLY LOCAL
banner on line 2, and both had NO behavioural test until g-115-4675 — which is
why two distinct silent failures survived in them:

  1. g-306-118-e routed the telemetry append through
     `_fileops.locked_append_jsonl`, which under own-cloud is a force-fresh GET
     plus a full-file PUT on every hook fire. Measured on cc-04 against a
     size-matched 486KB ledger: 700ms median vs 0.09ms for a bare append
     (~7,435x), on a path that fires on every user prompt.
  2. `user-prompt-skill-record.sh` read AGENT_DIR from `_paths.sh`, which
     populates it from MIND_AGENT — a var UserPromptExpansion hooks are never
     given. So it exited 0 before writing on EVERY real fire. Measured
     2026-08-02 across all five agents: 16,600 rows, 100% invocation_source
     "model", zero "user". The hook had never recorded a single row.

WHY A TMP *TREE* AND NOT A TMP AGENT_DIR (guard-920 / guard-1742).
The obvious harness — pass `AGENT_DIR=<tmp>` and assert a row lands there — does
not work and is actively harmful. `_paths.sh` RECOMPUTES AGENT_DIR from
MIND_AGENT on every source, overwriting whatever the caller passed. A probe
written that way reports success while writing to the REAL agent's ledger
(measured 2026-08-02: it appended a spurious row to alpha's live ledger and
reset alpha's live read-manifest). Both hooks resolve their own paths from their
own on-disk location, so the only shape that both isolates them and exercises
the real resolution code is a throwaway PROJECT_ROOT containing the scripts.

Every test scrubs MIND_AGENT and AGENT_DIR so the hooks run in the environment
the harness actually gives them, and pins STORAGE_BACKEND=local (guard-955).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = PROJECT_ROOT / "core" / "scripts"

GATE_HOOK = "context-reads-skill-gate.sh"
RECORD_HOOK = "user-prompt-skill-record.sh"

# Everything the two hooks reach transitively. All stdlib-only beyond these.
_TREE_FILES = (
    "_paths.sh",
    "_paths.py",
    "_platform.sh",
    "_session_binding.py",
    "session-binding-read.sh",
    "context-reads.py",
    GATE_HOOK,
    RECORD_HOOK,
)

AGENT = "testagent"
SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
STUB_SKILL = "reflect"

# guard-580/581: never build a subprocess argv whose argv[0] is a bare "bash".
BASH = shutil.which("bash") or "/bin/bash"


@pytest.fixture
def hook_tree(tmp_path):
    """A throwaway PROJECT_ROOT the hooks resolve themselves from."""
    root = tmp_path / "root"
    scripts = root / "core" / "scripts"
    scripts.mkdir(parents=True)
    for name in _TREE_FILES:
        shutil.copy2(SCRIPTS / name, scripts / name)

    agent = root / "agents" / AGENT
    (agent / "sessions" / SID).mkdir(parents=True)
    (agent / "session").mkdir(parents=True)
    (root / "world").mkdir()
    (root / "meta").mkdir()
    (agent / "local-paths.conf").write_text(
        f"WORLD_PATH={root}/world\nMETA_PATH={root}/meta\n", encoding="utf-8"
    )
    (agent / "sessions" / SID / "binding.yaml").write_text(
        f"session_id: {SID}\nagent: {AGENT}\nmode: autonomous\n"
        "started_at: '2026-08-02T00:00:00'\nstarted_by: claude-code\n",
        encoding="utf-8",
    )

    # The gate hook exits 0 early unless the SKILL.md it would inject exists.
    skill_md = root / ".claude" / "skills" / STUB_SKILL / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# stub skill\n", encoding="utf-8")
    return root


def _run(root, script, payload):
    """Invoke a hook in its production environment shape."""
    env = os.environ.copy()
    env["STORAGE_BACKEND"] = "local"  # guard-955
    # The harness gives hooks NEITHER of these. Scrubbing them is the whole
    # point: with them set, both hooks pass for the wrong reason.
    env.pop("MIND_AGENT", None)
    env.pop("AGENT_DIR", None)
    return subprocess.run(
        [BASH, str(root / "core" / "scripts" / script)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(root),
        env=env,
        timeout=60,
    )


def _code_lines(path):
    """Source with whole-line comments removed.

    Both bash and the embedded python use `#`, so one rule covers the file.
    Deliberately conservative: a line carrying code AND a trailing comment is
    kept whole, so the pin can still over-match there rather than silently
    under-matching — a false alarm is recoverable, a false all-clear is not.
    """
    return "\n".join(
        ln for ln in path.read_text(encoding="utf-8").splitlines()
        if not ln.strip().startswith("#")
    )


def _ledger(root):
    p = root / "agents" / AGENT / "skill-invocations.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _slash(command, sid=SID):
    return json.dumps(
        {"expansion_type": "slash_command", "command_name": command, "session_id": sid}
    )


def _skill_call(skill, sid=SID):
    return json.dumps({"session_id": sid, "tool_input": {"skill": skill}})


# ---------------------------------------------------------------- record hook


def test_user_prompt_hook_records_row_in_production_shape(hook_tree):
    """The user-invocation path records a row with no AGENT_DIR handed to it.

    This is the regression pin for the 16,600-row / zero-user defect: the hook
    must resolve its own agent from the payload's session_id.
    """
    res = _run(hook_tree, RECORD_HOOK, _slash("research-topic"))
    assert res.returncode == 0, res.stderr

    rows = _ledger(hook_tree)
    assert len(rows) == 1, f"expected exactly one row, got {rows}"
    assert rows[0]["invocation_source"] == "user"
    assert rows[0]["skill"] == "research-topic"
    assert rows[0]["agent"] == AGENT, "agent must be resolved from the binding"
    assert rows[0]["sid"] == SID


def test_user_prompt_hook_ignores_non_slash_command(hook_tree):
    """NEGATIVE CONTROL: proves the assertion above is not vacuous.

    Identical harness, identical invocation, only expansion_type differs — and
    no row appears. If this test and the one above both passed with the hook
    writing unconditionally, or both failed with it never writing, the pair
    would not discriminate. They do.
    """
    res = _run(hook_tree, RECORD_HOOK, json.dumps(
        {"expansion_type": "mcp_prompt", "command_name": "research-topic", "session_id": SID}
    ))
    assert res.returncode == 0, res.stderr
    assert _ledger(hook_tree) == []


def test_user_prompt_hook_unresolvable_sid_is_silent_and_fail_open(hook_tree):
    """NEGATIVE CONTROL: an unknown session writes nothing and still exits 0.

    Fail-open is load-bearing (guard-141) — this hook sits on the user's prompt
    path, so a resolution failure must degrade to a no-op, never to an error.
    """
    res = _run(hook_tree, RECORD_HOOK, _slash("research-topic", sid="no-such-session"))
    assert res.returncode == 0, res.stderr
    assert _ledger(hook_tree) == []


# ------------------------------------------------------------------ gate hook


def test_gate_hook_records_row_in_production_shape(hook_tree):
    """The model-invocation path records a row and allows the skill."""
    res = _run(hook_tree, GATE_HOOK, _skill_call(STUB_SKILL))
    assert res.returncode == 0, res.stderr

    rows = _ledger(hook_tree)
    assert len(rows) == 1, f"expected exactly one row, got {rows}"
    assert rows[0]["invocation_source"] == "model"
    assert rows[0]["skill"] == STUB_SKILL
    assert rows[0]["agent"] == AGENT


def test_gate_hook_ignores_unknown_skill(hook_tree):
    """NEGATIVE CONTROL: a skill with no SKILL.md writes nothing."""
    res = _run(hook_tree, GATE_HOOK, _skill_call("no-such-skill-zzz"))
    assert res.returncode == 0, res.stderr
    assert _ledger(hook_tree) == []


# ------------------------------------------------- the  latency pin


@pytest.mark.parametrize("script", [GATE_HOOK, RECORD_HOOK])
def test_hook_telemetry_does_not_route_through_the_storage_backend(script):
    """Neither hook may reach the storage backend on its telemetry write.

    Both carry the IRREDUCIBLY LOCAL banner, which forbids remote indirection.
    `_fileops.locked_append_jsonl` is a force-fresh GET + full-file PUT under
    own-cloud (~700ms measured vs 0.09ms bare). Durability for this store comes
    from its merge REGISTRATION in coordination_merge — owncloud_sync
    ._try_merge_put pushes the union on the sync sweep — not from the per-fire
    write path, so routing through the backend buys latency and nothing else.

    A source-level pin because the cost only manifests under own-cloud, and the
    suite runs with STORAGE_BACKEND=local where a reintroduced round trip would
    be invisible.
    """
    code = _code_lines(SCRIPTS / script)

    # POSITIVE CONTROL for the comment stripper (guard-1665): if it ever strips
    # everything, the two assertions below would pass vacuously forever. Both
    # hooks must still show their actual append as live code.
    assert "open(" in code and "json.dumps(row)" in code, (
        f"comment stripper ate the code in {script} — the pin below would be vacuous"
    )

    # Anchored to NON-COMMENT lines on purpose. Both hooks carry comments that
    # name locked_append_jsonl to explain why it is NOT used, and an unanchored
    # match cannot tell "doing this" from "writing this down" (guard-1099 — the
    # same defect bit /verify-learning's glob check, there as a false PASS).
    assert "locked_append_jsonl" not in code, (
        f"{script} routes its telemetry append through the storage backend; "
        "the IRREDUCIBLY LOCAL banner forbids it (g-115-4675)"
    )
    assert "import _fileops" not in code, (
        f"{script} imports _fileops on a hook hot path (g-115-4675)"
    )


@pytest.mark.parametrize("script", [GATE_HOOK, RECORD_HOOK])
def test_hook_banner_is_intact(script):
    """The banner is the contract the pin above enforces — keep them together."""
    head = (SCRIPTS / script).read_text(encoding="utf-8").splitlines()[:3]
    assert any("IRREDUCIBLY LOCAL" in ln for ln in head), (
        f"{script} lost its IRREDUCIBLY LOCAL banner"
    )
