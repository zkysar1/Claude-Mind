"""Tests for the deferrable-tier precheck battery (, strangler step 3).

The runner is injected, so no lane executes. What these pin is the contract the
battery exists to keep: every tier-table lane is accounted for, nothing that did not
run can read as clean, the budget bounds the run, the meter is touched from one
thread, and a spec can never silently fail to fire (the measured-shape fixture).
"""

import importlib.util
import json
import re
import sys
import threading
import time
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
_SHAPES = Path(__file__).resolve().parent / "fixtures" / "deferrable-battery-output-shapes.json"


def _load(stem, filename):
    spec = importlib.util.spec_from_file_location(stem, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_SCRIPTS))
    spec.loader.exec_module(mod)
    return mod


db = _load("precheck_deferrable_battery", "precheck-deferrable-battery.py")
io_mod = _load("iteration_open_for_deferrable", "iteration-open.py")
BY_SCRIPT = {l["script"]: l for l in db.LANES}
BY_NAME = {l["name"]: l for l in db.LANES}
JSON_KINDS = ("counts", "lists", "ids", "false", "true", "zero", "project")


def _clean_output(lane):
    """The output a lane prints when it has nothing to report, in the lane's own shape."""
    spec = db._FINDS.get(lane["name"], {})
    if lane["name"] == "cadence-battery":
        return json.dumps({"fired": [], "escalation": None})
    if any(k in spec for k in JSON_KINDS) or "--json" in lane["args"] or (
            "--output" in lane["args"] and "json" in lane["args"]):
        payload = {}
        for key in spec.get("zero", ()):
            payload[key] = 5  # a lane that read something
        return json.dumps(payload)
    return ""


class Rec:
    """A thread-safe injected runner recording every call."""

    def __init__(self, responses=None, meter="run"):
        self.responses, self.meter, self.calls = responses or {}, meter, []
        self._lock = threading.Lock()

    def __call__(self, argv, timeout):
        with self._lock:
            self.calls.append((threading.get_ident(), list(argv), timeout))
        if argv[0] == db._METER:
            if argv[1] == "check":
                m = self.meter(argv[2]) if callable(self.meter) else self.meter
                return 0, m + "\n", None, ""
            return 0, "", None, ""
        if argv[0] in self.responses:
            r = self.responses[argv[0]]
            return r(argv) if callable(r) else r
        return 0, _clean_output(BY_SCRIPT[argv[0]]), None, ""

    def lane_argvs(self):
        return {c[1][0]: c[1] for c in self.calls if c[1][0] != db._METER}


def _run(capsys, **kw):
    kw.setdefault("runner", Rec())
    kw.setdefault("env", {})
    db.run(as_json=True, **kw)
    return json.loads(capsys.readouterr().out)


# -- the registry -------------------------------------------------------------

def test_registry_covers_exactly_the_deferrable_tier_table_rows():
    names = [l["name"] for l in db.LANES]
    assert len(names) == len(set(names)), "duplicate lane name"
    covered = db.covered_names()
    assert len(covered) == len(set(covered)), "a sweep is accounted for twice"
    table = {r["sweep"] for r in io_mod.parse_tier_table() if r["tier"] == "deferrable"}
    assert set(covered) == table, (sorted(set(covered) ^ table))


def test_every_metered_lane_is_in_the_meters_deferrable_arm():
    text = (_SCRIPTS / "aspirations-precheck-budget-meter.sh").read_text(encoding="utf-8")
    body = re.search(r"sweep_tier\(\) \{.*?\n\}", text, re.S).group(0)
    arm = re.search(r"\n\s+([a-z0-9|.-]+)\)\n\s+echo \"deferrable\"", body).group(1).split("|")
    metered = {l["meter_name"] for l in db.LANES if l["meter_name"]}
    # An unknown name WARN-defaults to `medium`, which never drops: the lane would be
    # silently exempt from the tight-zone drop (test_budget_meter_sweep_tier_parity).
    assert metered <= set(arm), sorted(metered - set(arm))


def test_only_the_cadence_battery_is_unmetered():
    assert [l["name"] for l in db.LANES if not l["meter_name"]] == ["cadence-battery"]


def test_every_lane_has_a_spec_unless_it_is_judged_specially_or_held():
    unspecced = {l["name"] for l in db.LANES if l["name"] not in db._FINDS}
    assert unspecced == {"cadence-battery", "repo-hygiene-sweep"}
    assert BY_NAME["repo-hygiene-sweep"]["hold"], "its only exemption is an explicit hold"


def test_every_json_spec_key_exists_with_its_type_in_the_measured_shapes():
    """The positive control for the specs (guard-2421). A spec naming a key the lane does
    not emit, or typing it wrongly, can never fire -- and reads as a clean lane."""
    shapes = json.loads(_SHAPES.read_text(encoding="utf-8"))["lanes"]
    want = {"counts": {"int"}, "lists": {"list", "dict"}, "ids": {"list"},
            "false": {"bool"}, "true": {"bool"}, "zero": {"int"}}
    for name, spec in db._FINDS.items():
        keys = [(kind, k) for kind in want for k in spec.get(kind, ())]
        if not keys:
            continue
        assert name in shapes, f"{name} has a JSON spec but no measured shape"
        for kind, k in keys:
            assert shapes[name].get(k) in want[kind], (name, kind, k, shapes[name].get(k))


def test_the_specs_regexes_compile_and_use_known_keys():
    known = {"counts", "lists", "ids", "false", "true", "zero", "text", "rcs", "ok_rcs",
            "project", "census"}
    for name, spec in db._FINDS.items():
        assert set(spec) <= known, (name, set(spec) - known)
        for rx in spec.get("text", ()):
            re.compile(rx)


# -- running ------------------------------------------------------------------

def test_a_clean_apply_run_is_complete_and_every_lane_has_a_row(capsys):
    rep = _run(capsys, apply=True)
    assert rep["status"] == "clean" and rep["completeness"] == "complete", rep
    assert {r["name"] for r in rep["lanes"]} == set(BY_NAME)
    assert [h["name"] for h in rep["held"]] == ["repo-hygiene-sweep"]
    assert rep["held"][0]["standing"] is True
    assert len(rep["executed"]) == len(db.LANES) - 1
    assert "error" not in rep


def test_dry_mode_never_applies_and_apply_reaches_only_the_lanes_that_take_it(capsys):
    dry, app = Rec(), Rec()
    _run(capsys, apply=False, runner=dry)
    _run(capsys, apply=True, runner=app)
    flat = lambda r: [t for a in r.lane_argvs().values() for t in a]
    assert "--apply" not in flat(dry) and "--post-board" not in flat(dry)
    for script in ("pending-questions-sweep.sh", "credential-defer-recheck.sh",
                   "defer-drift-check.sh", "reason-less-blocked-check.sh"):
        assert "--apply" in app.lane_argvs()[script], script
    for script in ("l1-skew-check.sh", "scar-tissue-check.sh", "completed-not-closed-triage.sh"):
        assert "--post-board" in app.lane_argvs()[script], script
    # guard-4033: the goal-closing sweeps bare-replace outcome_note -- NEVER --apply
    for script in ("parent-supersession-sweep.sh", "unblock-parent-status-sweep.sh",
                   "routing-audit-target-status-sweep.sh"):
        assert "--apply" not in dry.lane_argvs()[script] + app.lane_argvs()[script], script


def test_ratchets_record_only_under_apply(capsys):
    dry, app = Rec(), Rec()
    rep = _run(capsys, apply=False, runner=dry)
    _run(capsys, apply=True, runner=app)
    for script in ("stalled-goal-ratchet.sh", "goal-field-census-ratchet.sh",
                   "recurring-precondition-sweep.py"):
        assert "--dry-run" in dry.lane_argvs()[script]
        assert "--dry-run" not in app.lane_argvs()[script]
    # no non-recording form exists: held in a dry run, run under apply
    for script in ("unchecked-write-ratchet.sh", "domain-term-ratchet.sh"):
        assert script not in dry.lane_argvs() and script in app.lane_argvs()
    held = {h["name"]: h for h in rep["held"]}
    assert held["domain-term-ratchet"]["standing"] is False
    assert rep["completeness"] == "partial"  # a dry run did not see them


def test_a_worker_body_gets_an_explicit_skip_and_runs_nothing(capsys):
    rec = Rec()
    rep = _run(capsys, runner=rec, env={"BODY_ROLE": "worker"})
    assert rec.calls == [] and "worker Body" in rep["skipped"]
    assert rep["completeness"] == "partial" and rep["findings"] == []


def test_a_meter_drop_is_honored_and_reported(capsys):
    rec = Rec(meter=lambda lane: "drop" if lane == "stalled-goal-ratchet" else "run")
    rep = _run(capsys, runner=rec)
    assert [d["name"] for d in rep["dropped"]] == ["stalled-goal-ratchet"]
    assert "stalled-goal-ratchet.sh" not in rec.lane_argvs()
    assert rep["completeness"] == "partial"


def test_the_meter_is_only_touched_from_the_scheduler_thread(capsys):
    rec = Rec()
    _run(capsys, runner=rec, jobs=4)
    meter_threads = {c[0] for c in rec.calls if c[1][0] == db._METER}
    assert meter_threads == {threading.get_ident()}


def test_executed_is_witnessed_only_for_lanes_that_ran_to_completion(capsys):
    rec = Rec(responses={"locus-sweep.sh": (None, "", "locus-sweep.sh: spawn failed", "")})
    rep = _run(capsys, runner=rec)
    witnessed = {c[1][2] for c in rec.calls if c[1][0] == db._METER and c[1][1] == "executed"}
    assert "locus-sweep" not in witnessed and "dropped-field-audit" in witnessed
    assert [b["name"] for b in rep["blind"]] == ["locus-sweep"]


def test_lanes_not_started_by_the_deadline_are_dropped_visibly(capsys):
    now = [0.0]

    def jump(argv):
        now[0] += 1000  # the first lane eats the whole budget
        return 0, _clean_output(BY_SCRIPT[argv[0]]), None, ""

    rec = Rec(responses={"pending-questions-sweep.sh": jump})
    rep = _run(capsys, runner=rec, clock=lambda: now[0], budget_s=100)
    dropped = {d["name"] for d in rep["dropped"]}
    assert "stalled-goal-ratchet" in dropped and "pending-questions-sweep" not in dropped
    assert all("over budget" in d["reason"] for d in rep["dropped"])
    assert {r["name"] for r in rep["lanes"]} == set(BY_NAME)  # still a row for each
    assert rep["completeness"] == "partial"


def test_a_lane_timeout_is_blind_and_names_the_cause(capsys):
    rec = Rec(responses={"locus-sweep.sh": (124, "", "locus-sweep.sh: timeout after 9s", "")})
    rep = _run(capsys, runner=rec)
    assert "timeout after 9s" in rep["blind"][0]["reason"]
    assert rep["status"] == "clean" and rep["completeness"] == "partial"


# -- judging a lane -----------------------------------------------------------

def test_unexpected_rc_is_blind_a_spec_rc_is_a_finding_an_ok_rc_is_clean(capsys):
    rep = _run(capsys, runner=Rec(responses={
        "locus-sweep.sh": (3, "{}", None, "boom"),
        "stalled-goal-ratchet.sh": (1, "", None, "REGRESSED stalled_goals=30"),
        "l1-skew-check.sh": (1, "", None, ""),
    }))
    assert [b["name"] for b in rep["blind"]] == ["locus-sweep"]
    assert "unexpected rc=3" in rep["blind"][0]["reason"]
    names = {f["name"]: f["detail"] for f in rep["findings"]}
    assert "stalled-goal-ratchet" in names and "l1-skew-cadence" not in names
    assert any("REGRESSED" in d for d in names["stalled-goal-ratchet"])


def test_a_traceback_is_blind_even_when_rc_is_a_finding_code(capsys):
    rep = _run(capsys, runner=Rec(responses={
        "embedded-python-audit.py": (1, "", None, "Traceback (most recent call last):\n  boom")}))
    assert [b["name"] for b in rep["blind"]] == ["embedded-python-audit"]
    assert "raised an exception" in rep["blind"][0]["reason"]
    assert rep["findings"] == []


def test_truncated_json_is_blind_naming_the_cap(capsys, monkeypatch):
    monkeypatch.setattr(db, "_OUT_CAP", 50)
    big = json.dumps({"flagged": [], "pad": "x" * 200})
    rep = _run(capsys, runner=Rec(responses={"check-stderr-json-merge.py": (0, big, None, "")}))
    assert [b["name"] for b in rep["blind"]] == ["check-stderr-json-merge"]
    assert "exceeded 50 bytes" in rep["blind"][0]["reason"]


def test_projection_keeps_only_the_named_keys_in_the_signature(capsys):
    body = json.dumps({"verdict": "SCANNED", "files_scanned": 9, "roots_skipped": [],
                       "tier_counts": {"active-scope": 0}, "rows": ["x" * 50] * 40})
    rep = _run(capsys, runner=Rec(responses={"hardcoded-scope-audit.py": (0, body, None, "")}))
    sig = next(r["sig"] for r in rep["lanes"] if r["name"] == "hardcoded-scope-audit")
    assert "verdict=SCANNED" in sig and "rows" not in sig


def test_a_zero_count_or_absent_key_is_a_read_failure_not_a_clean_census(capsys):
    for body in (json.dumps({"files_scanned": 0}), json.dumps({"verdict": "x"})):
        rep = _run(capsys, runner=Rec(responses={"hardcoded-scope-audit.py": (0, body, None, "")}))
        f = {x["name"]: x["detail"] for x in rep["findings"]}
        assert any("read nothing" in d for d in f["hardcoded-scope-audit"]), body


def test_a_dict_valued_key_is_a_finding_when_non_empty(capsys):
    rep = _run(capsys, runner=Rec(responses={
        "tree-last-updated-drift-check.py": (0, json.dumps({"errors": {"no_front_matter": 7}}), None, "")}))
    f = {x["name"]: x["detail"] for x in rep["findings"]}
    assert "no_front_matter" in f["tree-last-updated-drift-check"][0]


def test_an_uninterpreted_lane_is_listed_and_never_reads_as_clean(capsys, monkeypatch):
    monkeypatch.delitem(db._FINDS, "locus-sweep")
    rep = _run(capsys, apply=True)
    assert [u["name"] for u in rep["uninterpreted"]] == ["locus-sweep"]
    assert rep["completeness"] == "partial"
    db.run(as_json=False, apply=True, runner=Rec(), env={})
    out = capsys.readouterr().out
    assert "UNINTERPRETED: locus-sweep" in out and "NOT clean" in out


def test_cadence_fires_are_findings_that_name_the_skill_to_dispatch(capsys):
    body = json.dumps({"fired": [{"name": "felt-sense-cadence", "phase": "0.5f",
                                  "dispatch": "Skill(felt-sense)"}],
                       "escalation": {"threshold": 5, "dispatch_one": {
                           "name": "felt-sense-cadence", "dispatch": "Skill(felt-sense)"}}})
    rep = _run(capsys, runner=Rec(responses={"precheck-cadence-battery.sh": (0, body, None, "")}))
    detail = {f["name"]: f["detail"] for f in rep["findings"]}["cadence-battery"]
    assert "CADENCE FIRE felt-sense-cadence (phase 0.5f) -> Skill(felt-sense)" in detail[0]
    assert "ESCALATION" in detail[1]


def test_a_finding_is_a_finding_in_the_human_line_too(capsys):
    db.run(as_json=False, apply=True, env={}, runner=Rec(responses={
        "abandoned-claim-check.sh": (0, "  RELEASABLE g-1-1 claimed_by=a\n", None, "")}))
    out = capsys.readouterr().out
    assert "▸ FINDING: abandoned-claim-check (phase 0.5b.23)" in out
    assert "1 finding / " in out


def test_save_dir_keeps_each_lanes_raw_output(capsys, tmp_path):
    _run(capsys, runner=Rec(responses={"locus-sweep.sh": (0, '{"population": 1}', None, "w")}),
         save_dir=str(tmp_path))
    assert (tmp_path / "locus-sweep.out").read_text(encoding="utf-8") == '{"population": 1}'
    assert (tmp_path / "locus-sweep.err").read_text(encoding="utf-8") == "w"


# -- the real runner ------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX-only")
def test_a_timeout_kills_the_whole_process_group_and_returns_in_time(monkeypatch):
    monkeypatch.setattr(db, "_command", lambda argv: [
        sys.executable, "-c",
        "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"])
    t0 = time.time()
    rc, out, err, _stderr = db._run_script(["x.py"], 1)
    assert rc == 124 and "timeout after 1s" in err
    assert time.time() - t0 < 15


def test_a_lane_can_never_consume_the_callers_stdin(monkeypatch):
    """Asserted on the Popen kwargs, not on what a child happens to read: a child reading
    an already-empty inherited stdin would pass this even with the guard removed."""
    import subprocess

    seen = {}
    real = subprocess.Popen

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(db.subprocess, "Popen", spy)
    monkeypatch.setattr(db, "_command", lambda argv: [sys.executable, "-c", "pass"])
    assert db._run_script(["x.py"], 20)[0] == 0
    assert seen["stdin"] == subprocess.DEVNULL


def test_main_exits_zero_with_a_structured_report_when_the_run_crashes(monkeypatch, capsys):
    def boom(**kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(db, "run", boom)
    monkeypatch.setattr(sys, "argv", ["x", "--json"])
    assert db.main() == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["completeness"] == "partial" and "battery_failed: kaboom" in rep["error"]
