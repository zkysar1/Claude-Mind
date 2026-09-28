"""Pins verify-preflight (): one call runs a goal's mechanical verify checks.

It replaces separate model calls with one script, so the things that must not
change are pinned here:

- EACH INCLUDED CHECK'S REFUSAL STILL REFUSES through the pre-flight. Every
  refusal is produced by the REAL gate CLI on a tmp fixture (guard-920: the
  production call shape), and each is paired with a control on the same fixture
  that passes (guard-1082), so a FAIL here is the gate's refusal and not a usage
  error or a crash.
- A check that did not apply reads SKIPPED, and a gate that could not run reads
  ERROR. Neither is ever a PASS (guard-5501); a Q4 "skipped" is not a pass.
- The state writes the replaced skill steps made are still made, with the same
  keys and diary text (guard-1867), and none lands on a checkpoint that anchors
  another goal.

The harness runs the real gates and records the writes instead of making them,
so no test touches a live checkpoint, diary or working memory.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location("verify_preflight", CORE_SCRIPTS / "verify-preflight.py")
vp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vp)

GOAL = {"id": "g-1-1", "verification": {"outcomes": ["the widget builds"]}}
GOOD_NOTE = "OUTCOME 1: MET — build 42 green, sha 1a2b3c4d."
BAD_NOTE = "done, all good"
GENUINE_FAIL = {"type": "file_check", "path": "no-such-file-zzz-g37548"}
PASSING = {"type": "file_check", "path": "CLAUDE.md"}
UNEVALUATABLE = {"type": "code_check", "target": "x"}
UNCITED = "Widget Industries employs 12,000 people across its plants.\n"
CITED_UNRESOLVABLE = ("Acme Corporation reported revenue of 4.2 billion in 2024,\n"
                      "per https://example.invalid/some-url.\n")


class Harness:
    """A pre-flight Runner. Gates run for real against tmp fixtures; the writes
    (checkpoint, diary, working memory) are recorded, never made."""

    def __init__(self, tmp_path, note=GOOD_NOTE, checks=(), anchor="g-1-1", overrides=None):
        self.tmp = tmp_path
        self.checks = list(checks)
        self.anchor = anchor
        self.overrides = overrides or {}  # script name -> (rc, stdout, stderr)
        self.calls = []                   # (script name, argv, stdin)
        world, meta = tmp_path / "world", tmp_path / "meta"
        (world / "knowledge").mkdir(parents=True, exist_ok=True)
        meta.mkdir(exist_ok=True)
        self.goal_json = tmp_path / "goal.json"
        self.goal_json.write_text(json.dumps({**GOAL, "outcome_note": note}), encoding="utf-8")
        self.env = {**os.environ, "MIND_WORLD": str(world), "MIND_META": str(meta),
                    "MIND_AGENT": "testagent", "STORAGE_BACKEND": "local",
                    "CLOSURE_EVIDENCE_LEDGER_DIR": str(tmp_path), "MIND_SID": "no-such-session"}

    def _real(self, argv, stdin=None):
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, encoding="utf-8",
                           cwd=str(PROJECT_ROOT), env=self.env, timeout=120)
        return p.returncode, p.stdout, p.stderr

    def __call__(self, argv, stdin=None):
        name = Path(argv[1]).name
        self.calls.append((name, list(argv), stdin))
        if name in self.overrides:
            return self.overrides[name]
        if name == "verify-check-eval.sh":
            # The production call looks the goal up; the real CLI's --checks mode
            # evaluates the fixture's checks[] and prints the same document.
            return self._real([sys.executable, str(CORE_SCRIPTS / "verify-check-eval.py"),
                               "--checks", json.dumps(self.checks), "--all"])
        if name == "closure-evidence-gate.py":
            return self._real(list(argv) + ["--goal-json", str(self.goal_json)])
        if name in ("positive-state-gate.py", "q4-provenance-sample.sh"):
            return self._real(list(argv))
        if name == "loop-state-save.sh" and argv[2] == "read":
            return (0, json.dumps({"goal_id": self.anchor}), "") if self.anchor else (1, "null", "")
        if name in ("loop-state-save.sh", "execution-diary.sh", "wm-append.sh"):
            return 0, "", ""
        raise AssertionError(f"unexpected script {name}: {argv}")

    def writes(self, name):
        return [(a, s) for n, a, s in self.calls if n == name and not (n == "loop-state-save.sh"
                                                                       and a[2] == "read")]

    def checkpoint_sets(self):
        return [a[a.index("--set") + 1:] for a, _ in self.writes("loop-state-save.sh")]

    def diary(self):
        return [json.loads(s)["content"] for _, s in self.writes("execution-diary.sh")]

    def gaps(self):
        return [json.loads(s) for _, s in self.writes("wm-append.sh")]


def run(h, artifacts=(), **kw):
    return vp.preflight("g-1-1", "world", list(artifacts), run=h, **kw)


def art(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# ─── each included check's refusal still refuses, with a passing control ────

def test_a_failing_structured_check_refuses_and_a_passing_one_does_not(tmp_path):
    r = run(Harness(tmp_path, checks=[GENUINE_FAIL, PASSING]))
    assert (r["results"]["checks"]["state"], r["rc"]) == ("FAIL", 3)
    assert r["results"]["checks"]["findings"] == ["check 1 (file_check): found 0, need 1"]
    r = run(Harness(tmp_path, checks=[PASSING]))
    assert (r["results"]["checks"]["state"], r["rc"]) == ("PASS", 0)


def test_an_unevaluatable_check_is_skipped_not_failed_and_no_checks_is_skipped(tmp_path):
    r = run(Harness(tmp_path, checks=[UNEVALUATABLE]))
    assert (r["results"]["checks"]["state"], r["rc"]) == ("SKIPPED", 0)
    r = run(Harness(tmp_path, checks=[]))
    assert r["results"]["checks"]["state"] == "SKIPPED"
    assert "no structured checks" in r["results"]["checks"]["summary"]


def test_a_refused_closure_table_refuses_and_q1_fails_with_it(tmp_path):
    r = run(Harness(tmp_path, note=BAD_NOTE))
    ce = r["results"]["closure-evidence"]
    assert (ce["state"], r["rc"]) == ("FAIL", 3)
    assert any("no evidence table" in f for f in ce["findings"])
    assert r["results"]["q1"]["state"] == "FAIL"
    r = run(Harness(tmp_path, note=GOOD_NOTE))
    assert (r["results"]["closure-evidence"]["state"], r["results"]["q1"]["state"], r["rc"]) == (
        "PASS", "PASS", 0)


def test_a_missing_artifact_refuses_and_an_existing_one_passes(tmp_path):
    r = run(Harness(tmp_path), [str(tmp_path / "no-such-artifact.md")])
    assert (r["results"]["artifact"]["state"], r["results"]["q1"]["state"], r["rc"]) == (
        "FAIL", "FAIL", 3)
    r = run(Harness(tmp_path), [art(tmp_path, "tagged.md", "Plain prose with no entity facts.\n")])
    assert r["results"]["artifact"]["state"] == "PASS"
    # A directory is refused as well, and the finding says it is a directory, not that it is absent.
    r = run(Harness(tmp_path), [str(tmp_path)])
    assert r["results"]["artifact"]["state"] == "FAIL"
    assert r["results"]["artifact"]["findings"] == [f"{tmp_path}: a directory, not a file ({tmp_path})"]


def test_an_unread_file_claim_refuses_and_the_same_claim_with_its_read_passes(tmp_path):
    claim = "notes.md contains the plan"
    r = run(Harness(tmp_path), claim=claim, evidence="")
    ps = r["results"]["positive-state"]
    assert (ps["state"], r["results"]["q1"]["state"], r["rc"]) == ("FAIL", "FAIL", 3)
    assert ps["findings"] == ["not in the evidence: notes.md"]
    r = run(Harness(tmp_path), claim=claim, evidence="$ cat notes.md\n# the plan\n")
    assert (r["results"]["positive-state"]["state"], r["rc"]) == ("PASS", 0)
    r = run(Harness(tmp_path))
    assert r["results"]["positive-state"]["state"] == "SKIPPED"


def test_a_q4_refusal_refuses_and_its_pass_does_not(tmp_path, monkeypatch):
    r = run(Harness(tmp_path), [art(tmp_path, "uncited.md", UNCITED)])
    q4 = r["results"]["q4"]
    assert (q4["state"], r["rc"]) == ("FAIL", 3)
    assert q4["findings"] and "missing-citation" in q4["findings"][0]
    # Control: the real sampler's PASS document, produced in-process with a
    # manifest that holds the cited source (its CLI prints this same dict).
    import q4_provenance_sample as q4m
    cited = art(tmp_path, "cited.md", "Acme Corporation reported revenue of 4.2 billion "
                "in 2024,\nper https://example.invalid/actually-fetched.\n")
    monkeypatch.setattr(q4m, "retrieved_predicate", lambda sid: (lambda k, v: "actually-fetched" in str(v)))
    doc = q4m.run("g-1-1", [cited], n=5, session_id="x")
    assert doc["verdict"] == "pass"
    h = Harness(tmp_path, overrides={"q4-provenance-sample.sh": (0, json.dumps(doc), "")})
    r = run(h, [cited])
    assert (r["results"]["q4"]["state"], r["rc"]) == ("PASS", 0)


# ─── not-applied and could-not-run are never a pass ─────────────────────────

def test_a_skipped_q4_is_not_a_pass_and_is_recorded_as_skipped(tmp_path):
    h = Harness(tmp_path)
    r = run(h, [art(tmp_path, "cited.md", CITED_UNRESOLVABLE)])
    assert r["results"]["q4"]["state"] == "SKIPPED"
    assert "NOT a pass" in r["results"]["q4"]["summary"]
    assert ["phase_progress.q4_passed=false", "--set", "phase_progress.q4_verdict=skipped"] in h.checkpoint_sets()


def test_a_crashed_gate_is_an_error_never_a_pass_or_a_refusal(tmp_path):
    crash = (1, "", "Traceback (most recent call last):\nImportError: boom")
    h = Harness(tmp_path, overrides={"closure-evidence-gate.py": crash})
    r = run(h)
    assert (r["results"]["closure-evidence"]["state"], r["verdict"], r["rc"]) == ("ERROR", "ERROR", 4)
    assert r["results"]["q1"]["state"] == "OPEN"  # judged by the closer, not passed
    assert not any("q1_passed" in " ".join(s) for s in h.checkpoint_sets())


def test_verdict_read_from_output_not_exit_code(tmp_path):
    # rc 0 with a "fail" verdict is a disagreement: ERROR, not a pass.
    doc = {"verdict": "fail", "sampled_count": 1, "clusters_total": 1, "findings": []}
    h = Harness(tmp_path, overrides={"q4-provenance-sample.sh": (0, json.dumps(doc), "")})
    r = run(h, [art(tmp_path, "a.md", UNCITED)])
    assert r["results"]["q4"]["state"] == "ERROR"


# ─── the replaced steps' state writes are still made (guard-1867) ───────────

def test_a_clean_close_makes_the_same_writes_the_skill_steps_made(tmp_path, monkeypatch):
    import q4_provenance_sample as q4m
    a = art(tmp_path, "cited.md", "Acme Corporation reported revenue of 4.2 billion in 2024,\n"
            "per https://example.invalid/actually-fetched.\n")
    monkeypatch.setattr(q4m, "retrieved_predicate", lambda sid: (lambda k, v: "actually-fetched" in str(v)))
    doc = q4m.run("g-1-1", [a], n=5, session_id="x")
    h = Harness(tmp_path, checks=[PASSING], overrides={"q4-provenance-sample.sh": (0, json.dumps(doc), "")})
    r = run(h, [a])
    assert r["rc"] == 0 and r["write_failures"] == [] and r["not_written"] == []
    assert h.checkpoint_sets() == [
        ["phase_progress.standard_checks_passed=1/1"],
        ["phase_progress.q1_passed=true", "--set", f"phase_progress.q1_artifact={a}"],
        ["phase_progress.q4_passed=true", "--set", "phase_progress.q4_verdict=pass"]]
    assert h.diary() == ["Verify: 1/1 standard checks passed", f"Q1 passed: artifact={a}",
                         "Q4 provenance: pass, 1/1 cluster(s)"]
    assert h.gaps() == []


def test_failures_write_verification_gaps_and_no_q1_pass(tmp_path):
    h = Harness(tmp_path)
    r = run(h, [art(tmp_path, "uncited.md", UNCITED)], claim="notes.md contains the plan")
    assert r["rc"] == 3
    gaps = h.gaps()
    assert [g["observation"].split(" FAIL")[0] for g in gaps] == [
        "verify pre-flight positive-state", "verify pre-flight q4"]
    assert all(g["type"] == "verification_gap" and g["source_goal"] == "g-1-1" for g in gaps)
    assert not any("q1_passed" in " ".join(s) for s in h.checkpoint_sets())
    assert ["phase_progress.q4_passed=false", "--set", "phase_progress.q4_verdict=fail"] in h.checkpoint_sets()


def test_no_checkpoint_key_lands_on_a_checkpoint_anchoring_another_goal(tmp_path):
    h = Harness(tmp_path, anchor="g-9-9")
    r = run(h)
    assert r["results"]["q1"]["state"] == "PASS"
    assert h.checkpoint_sets() == []
    assert any("anchors g-9-9" in x for x in r["not_written"])
    assert h.diary() == ["Q1 passed: artifact=closure-evidence table"]
    # Control: an ABSENT checkpoint still gets the update, whose missing-checkpoint
    # warning and ledger row are how a skipped anchor is detected.
    h = Harness(tmp_path, anchor=None)
    run(h)
    assert h.checkpoint_sets() == [["phase_progress.q1_passed=true", "--set",
                                    "phase_progress.q1_artifact=closure-evidence table"]]


def test_no_write_makes_no_write_at_all(tmp_path):
    h = Harness(tmp_path, checks=[GENUINE_FAIL])
    r = run(h, [art(tmp_path, "uncited.md", UNCITED)], claim="notes.md contains the plan", write=False)
    assert r["rc"] == 3
    names = {n for n, _, _ in h.calls}
    assert not names & {"loop-state-save.sh", "execution-diary.sh", "wm-append.sh"}


# ─── the production call shapes, and the CLI ────────────────────────────────

def test_each_gate_is_called_the_way_the_skill_called_it(tmp_path):
    h = Harness(tmp_path)
    a = art(tmp_path, "uncited.md", UNCITED)
    run(h, [a], claim="notes.md contains the plan", evidence="x", source_file=a)
    argv = {n: a_[2:] for n, a_, _ in h.calls}
    assert argv["verify-check-eval.sh"] == ["--goal", "g-1-1", "--all"]
    assert argv["closure-evidence-gate.py"] == ["--goal", "g-1-1", "--source", "world"]
    assert argv["positive-state-gate.py"] == ["--claim", "notes.md contains the plan", "--evidence", "x"]
    assert argv["q4-provenance-sample.sh"] == ["--goal", "g-1-1", "--artifact", a, "--json",
                                              "--source-file", a]


@pytest.mark.parametrize("args", [["--source", "world"], ["--goal", "g-1-1", "--evidence", "x"]])
def test_usage_errors_exit_2_and_never_run_a_check(args):
    p = subprocess.run([sys.executable, str(CORE_SCRIPTS / "verify-preflight.py"), *args],
                       capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=60)
    assert p.returncode == 2 and "VERIFY PRE-FLIGHT" not in p.stdout


def test_the_verify_skill_runs_the_preflight():
    """rb-9476: a correct script no phase invokes is inert."""
    skill = (PROJECT_ROOT / ".claude" / "skills" / "aspirations-verify" / "SKILL.md").read_text(
        encoding="utf-8")
    assert "bash core/scripts/verify-preflight.sh --goal <goal-id>" in skill
