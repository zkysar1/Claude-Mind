"""Every advisory ratchet must report `error` when the baseline write FAILS.

The defect (g-115-4275, found by adversarial fresh-eyes during g-115-3946):
each ratchet computes its verdict inside `_modify`, which runs INSIDE
`locked_modify_yaml` and fills a `captured` dict BEFORE the write lands. The
error path then called `captured.setdefault("verdict", "error")` -- a NO-OP
once `_modify` has run. So a write that failed after the modifier still
reported seeded/ratcheted/stable, and /verify-learning recorded a baseline
write that never happened.

WHY THIS TEST USES A MODIFIER-THEN-RAISE SHIM, and not the "make the baseline
path a directory" trick the reference implementation's own test uses: an
unwritable path fails at READ time, BEFORE `_modify` is ever called, so
`captured` is empty and `setdefault` behaves correctly. That scenario passes
identically on the broken code -- measured 2026-08-27, pre-patch
experience-orphan-ratchet returned verdict='error' under it. A test for this
defect MUST fail the write AFTER the modifier has run, or it is vacuous.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent

# (script stem, argv for JSON output)
FAMILY = [
    ("temp-citation-ratchet", ["--json"]),          # the reference implementation
    ("unchecked-write-ratchet", ["--json"]),
    ("session-manifest-orphan-ratchet", ["--json"]),
    ("learning-routing-ratchet", ["--json"]),
    ("goal-field-census-ratchet", ["--json"]),
    ("experience-orphan-ratchet", ["--json"]),
    ("eviction-conservation-ratchet", ["--json"]),
    ("verification-check-validity-ratchet", ["--json"]),
    ("skillmd-flag-audit", ["--output", "json", "--ratchet"]),
    # NOT domain-term-ratchet: its measurement (domain-term-census.py) requires
    # a FULL world -- forged-skills.yaml, program.md, conventions/ -- and voids
    # itself on any missing class-A registry (guard-2081). Satisfying that here
    # would bloat this deliberately-minimal fixture for one member's benefit.
    # Its persist-failure path is pinned in test_domain_term_ratchet.py, which
    # stubs the census instead of running it.
]

SITECUSTOMIZE = '''
import os, sys
sys.path.insert(0, os.environ["RATCHET_SCRIPTS_DIR"])
import _fileops

def _write_fails_after_modifier(path, modifier_fn, initial=None):
    # Run the modifier exactly as a real cycle would, so `captured` is
    # populated with the COMPUTED verdict, then fail the write.
    try:
        modifier_fn(dict(initial or {}))
    except Exception:
        pass
    raise OSError("simulated write failure AFTER the modifier ran")

_fileops.locked_modify_yaml = _write_fails_after_modifier
'''

# THE RATCHET'S INPUT MUST COME FROM THIS FIXTURE TOO. A ratchet that finds nothing to measure
# prints `skipped` and returns before the write, so the shim above never fires and the test pins
# nothing. Three members read their input from outside the temp world: the live goal queue behind
# aspirations-query.sh, and the checkout's own agent dirs. Only the dev origin has those; a
# deployment-shaped checkout has no daemon, no goals and no agent dirs (rb-12992). Each input is
# pinned where the script reads it, on the module object the script imports (rb-13019), and only
# for the stems that need it, so the other members keep running their real readers.
_QUERY_ROWS = '''
import json, subprocess
_real_run = subprocess.run

def _run(cmd, *a, **k):
    cmd = list(cmd)
    if any(str(c).endswith("aspirations-query.sh") for c in cmd) and "--goal-status" in cmd:
        rows = [{"id": "g-1-1", "source": "world", "status": "pending", "title": "t",
                 "verification": {"checks": ["a prose check"]}}]
        if cmd[cmd.index("--goal-status") + 1] != "pending":
            rows = []
        return subprocess.CompletedProcess(cmd, 0, json.dumps(rows), "")
    return _real_run(cmd, *a, **k)

subprocess.run = _run
'''

_AGENT_DIRS = '''
import os
from pathlib import Path
import _paths

_agents = Path(os.environ["RATCHET_AGENTS_ROOT"])
_paths.agents_root = lambda: _agents
'''

# stem -> the extra sitecustomize source that gives that ratchet something to measure
SEAMS = {
    "goal-field-census-ratchet": _QUERY_ROWS,
    "verification-check-validity-ratchet": _QUERY_ROWS,
    "experience-orphan-ratchet": _AGENT_DIRS,
}


def _world(tmp: Path) -> Path:
    world = tmp / "world"
    (world / "knowledge" / "tree").mkdir(parents=True)
    (world / "knowledge" / "tree" / "n.md").write_text(
        "# Node\n\nEvidence: agents/alpha/temp/a.md\n", encoding="utf-8")
    for store in ("reasoning-bank", "guardrails", "pattern-signatures"):
        (world / f"{store}.jsonl").write_text(
            json.dumps({"id": "x-0", "category": "c",
                        "content": "see agents/alpha/temp/a.md"}) + "\n",
            encoding="utf-8")
    (world / "aspirations.jsonl").write_text(
        json.dumps({"id": "asp-1", "goals": [
            {"id": "g-1-1", "status": "pending", "title": "t"}]}) + "\n",
        encoding="utf-8")
    return world


def _agents(tmp: Path) -> Path:
    """One agent holding two traces and an index row for one of them: what the orphan ratchet measures."""
    agent = tmp / "agents" / "a1"
    (agent / "experience").mkdir(parents=True)
    for name in ("exp-1", "exp-2"):
        (agent / "experience" / f"{name}.md").write_text("# trace\n", encoding="utf-8")
    (agent / "experience.jsonl").write_text(json.dumps({"id": "exp-1"}) + "\n", encoding="utf-8")
    return tmp / "agents"


@pytest.mark.parametrize("stem,argv", FAMILY, ids=[s for s, _ in FAMILY])
def test_failed_persist_never_reports_the_computed_verdict(stem, argv):
    script = SCRIPTS / f"{stem}.py"
    assert script.is_file(), f"{script} missing -- family list is stale"
    with tempfile.TemporaryDirectory(prefix="ratchetfam_") as d:
        tmp = Path(d)
        meta = tmp / "meta"
        meta.mkdir()
        shim = tmp / "shim"
        shim.mkdir()
        (shim / "sitecustomize.py").write_text(SITECUSTOMIZE + SEAMS.get(stem, ""),
                                               encoding="utf-8")

        env = os.environ.copy()
        env["MIND_WORLD"] = str(_world(tmp))
        env["MIND_META"] = str(meta)
        env["RATCHET_AGENTS_ROOT"] = str(_agents(tmp))
        env["STORAGE_BACKEND"] = "local"     # guard-955: never touch the real store
        env["RATCHET_SCRIPTS_DIR"] = str(SCRIPTS)
        env["PYTHONPATH"] = str(shim) + os.pathsep + env.get("PYTHONPATH", "")
        env.pop("VERIFY_LEARNING_DRIFT_HARD_GATE", None)

        r = subprocess.run([sys.executable, str(script), *argv],
                           capture_output=True, text=True, encoding="utf-8",
                           env=env, cwd=str(SCRIPTS.parent.parent))
        assert r.stdout.strip(), (
            f"{stem} emitted no stdout (rc={r.returncode}); stderr:\n{r.stderr[-800:]}")
        out = json.loads(r.stdout)

        assert out["verdict"] == "error", (
            f"{stem}: a FAILED baseline write reported verdict="
            f"{out['verdict']!r} -- the caller records a write that never "
            "happened, and stderr is the only contradicting signal")
        assert out.get("baseline") is None, (
            f"{stem}: reported baseline={out.get('baseline')!r} for a write "
            "that failed")
        assert "FAILED" in (out.get("message") or ""), (
            f"{stem}: message does not disclose the failure: "
            f"{out.get('message')!r}")
