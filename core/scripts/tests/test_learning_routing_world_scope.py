"""World-scoping invariants for learning-routing-audit / -repair ().

These tests exist because the repair is a WRITER wired to fire automatically:
`tree.py::_post_remove_sweep_dangling` runs `learning-routing-repair.py --apply`
after every tree-node removal, and that repair NULLS whatever the audit calls
dangling. So an audit false positive is not a reporting defect here — it is data
loss, in files the audit resolves through `agents_root()`, which is
PROJECT_ROOT-based and therefore unaffected by any world override.

Two invariants, measured 2026-08-10 (cc-05) from the failure that motivated them:

  1. FOREIGN WORLD. Point MIND_WORLD at a fixture world — which every hermetic
     test does — and the real per-agent experience corpus becomes foreign to it:
     nothing in the fixture can resolve those records' hypothesis_id or
     tree_nodes_related, so every ref reads dangling. An EMPTY fixture world
     produced 2,739 dangling refs across 12 REAL agent files, every one valid.
     What prevented the mass-null was not a guard: it was a 30s subprocess
     timeout expiring during the READ phase, which is also why three ordinary
     tree tests went red. That protection is accidental and inverted — any
     performance work would have removed it and let the writes land.

  2. AN ABSENT STORE IS UNMEASURABLE, NOT EMPTY. A zero-record experience corpus
     must never license invalidating refs INTO it. That is the general form of
     the 13-day incident (a depth-1 glob read 0 records; 17,466 experience_ref
     fields were nulled, 94.7% of them valid), and fixing only the glob leaves
     the license standing for every other way a store can read back empty — an
     unmounted external path, a sync miss, a rename.

NOTHING HERE RUNS `--apply`. These assert on dry-run output by design: a test
that proves the writer is safe by invoking the writer has already lost.
"""
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parents[2]
AUDIT = REPO / "core" / "scripts" / "learning-routing-audit.py"
REPAIR = REPO / "core" / "scripts" / "learning-routing-repair.py"


def _fixture_world(tmp_path):
    """An empty-but-well-formed world: the shape a hermetic test hands over."""
    world = tmp_path / "world"
    (world / "knowledge" / "tree").mkdir(parents=True)
    (world / "knowledge" / "tree" / "_tree.yaml").write_text(
        "nodes: {}\n", encoding="utf-8")
    for name in ("pipeline.jsonl", "pattern-signatures.jsonl"):
        (world / name).write_text("", encoding="utf-8")
    # rb + guardrails must be non-empty or the audit exits 2 ("no records
    # loaded") before it reaches the report — an input-error path, which would
    # make the skip-banner test pass or fail for reasons unrelated to scoping.
    # Each carries an experience_ref that resolves NOWHERE, so if the experience
    # axis were still being evaluated these would be flagged dangling; their
    # ABSENCE from the findings is the assertion that the axis was skipped.
    (world / "reasoning-bank.jsonl").write_text(
        '{"id": "rb-fixture-1", "title": "fixture", '
        '"experience_ref": "exp-fixture-does-not-exist"}\n', encoding="utf-8")
    (world / "guardrails.jsonl").write_text(
        '{"id": "guard-fixture-1", "rule": "fixture", '
        '"experience_ref": "exp-fixture-does-not-exist"}\n', encoding="utf-8")
    (tmp_path / "meta").mkdir(parents=True, exist_ok=True)
    return world


def _run(script, tmp_path, extra_env=None):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(_fixture_world(tmp_path))
    env["MIND_META"] = str(tmp_path / "meta")
    env.pop("MIND_AGENT", None)
    env.update(extra_env or {})
    # env=env is load-bearing, not boilerplate: _paths resolves WORLD_DIR from
    # MIND_WORLD at import time in the CHILD, so omitting it silently runs the
    # audit against the REAL world and the test then asserts against production
    # data while looking hermetic.
    p = subprocess.run([sys.executable, str(script)], env=env,
                       capture_output=True, text=True, timeout=180)
    return p.returncode, p.stdout + p.stderr


def test_foreign_world_resolves_no_real_agent_file(tmp_path):
    """The load-bearing test: a fixture world must reach ZERO real agent files.

    Asserting on the resolved PATHS rather than on the dangling COUNT is
    deliberate. A count can fall for reasons that leave the hazard intact
    (a faster machine, a smaller corpus); a path under agents/ in this output
    is the writer naming a file it would rewrite, which is the actual defect.
    """
    rc, out = _run(REPAIR, tmp_path)
    offenders = [ln for ln in out.splitlines()
                 if "/agents/" in ln or ln.strip().startswith("agents/")]
    assert not offenders, "repair resolved REAL agent files from a fixture world:\n" + "\n".join(offenders[:10])
    assert rc == 0, out
    assert "CLEAN" in out, out


def test_foreign_world_skip_is_announced_not_silent(tmp_path):
    """A skipped axis must not read as a clean one (guard-1760).

    exp:0 has two causes that demand opposite responses — a foreign world
    (correct, deliberate) and an owned corpus that read back empty (a defect to
    chase). Reporting only the number collapses them, and the reader who most
    needs the distinction is the one seeing a suspiciously clean run.
    """
    rc, out = _run(AUDIT, tmp_path)
    assert "experience axis SKIPPED" in out, out
    assert "foreign world" in out, out
    # The banner is the visible half; this is the behavioural half. Both fixture
    # records point at an experience id that exists nowhere, so an evaluated
    # axis MUST flag them. Silence here is the skip actually taking effect —
    # asserting only on the banner would pass against a scanner that still
    # flags (and therefore still nulls) every one of them.
    assert "exp-fixture-does-not-exist" not in out, out


def test_ownership_predicate_discriminates():
    """Positive control: the predicate must say YES for this project's own world.

    Without this, a predicate hardcoded to False would pass both tests above
    while silently disabling the experience axis everywhere — trading a
    destructive bug for a blind one (guard-2499).
    """
    sys.path.insert(0, str(REPO / "core" / "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("lra", AUDIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.world_owns_agent_corpus() is True
    assert mod.load_all_experiences(), "real world must load a non-empty corpus"
