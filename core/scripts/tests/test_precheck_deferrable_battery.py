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
import tempfile
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
    """A thread-safe injected runner recording every call ().

    It ALSO emulates the budget meter's one-shot state file, faithfully, so the
    `executed` witness (g-115-12183) can be tested against the meter's real
    semantics: `check` opens the window and appends the permission row;
    `executed` appends ONLY while the window is open (the meter's fail-open no-op
    is exit 0 with no row otherwise); `end` unlinks the window. Without this the
    witness's landing check would read a file no test ever wrote.
    """

    def __init__(self, responses=None, meter="run", agent_dir=None):
        self.responses, self.meter, self.calls = responses or {}, meter, []
        self.agent_dir = Path(agent_dir) if agent_dir else None
        self._lock = threading.Lock()
        self._window_open = False

    @property
    def state_file(self):
        if self.agent_dir is None:
            return None
        (self.agent_dir / "session").mkdir(parents=True, exist_ok=True)
        return self.agent_dir / "session" / "precheck-budget-state.json"

    def _meter(self, argv):
        with self._lock:
            if argv[1] == "check":
                m = self.meter(argv[2]) if callable(self.meter) else self.meter
                sf = self.state_file
                if sf is not None:
                    if not self._window_open:
                        sf.write_text(json.dumps({
                            "disabled": False, "start_ms": 0, "cap_ms": 90000,
                            "zone": "fresh", "zone_drop_rules": {}, "sweeps": []}),
                            encoding="utf-8")
                        self._window_open = True
                    if sf.is_file():
                        state = json.loads(sf.read_text(encoding="utf-8"))
                        state.setdefault("sweeps", []).append(
                            {"sweep": argv[2], "tier": "deferrable",
                             "decision": m, "reason": "test"})
                        sf.write_text(json.dumps(state), encoding="utf-8")
                else:
                    self._window_open = False
                return 0, m + "\n", None, ""
            if argv[1] == "executed":
                sf = self.state_file
                if sf is None or not sf.is_file():
                    return 0, "", None, ""  # the meter's fail-open no-op
                try:
                    state = json.loads(sf.read_text(encoding="utf-8"))
                except Exception:
                    return 0, "", None, ""
                if not isinstance(state, dict) or state.get("disabled"):
                    return 0, "", None, ""
                state.setdefault("sweeps", []).append(
                    {"sweep": argv[2], "tier": "deferrable",
                     "decision": "executed", "reason": "lane-completed"})
                sf.write_text(json.dumps(state), encoding="utf-8")
                return 0, "", None, ""
            if argv[1] == "end":
                sf = self.state_file
                if sf is not None and sf.is_file():
                    sf.unlink()
                self._window_open = False
            return 0, "", None, ""

    def __call__(self, argv, timeout):
        with self._lock:
            self.calls.append((threading.get_ident(), list(argv), timeout))
        if argv[0] == db._METER:
            return self._meter(argv)
        if argv[0] in self.responses:
            r = self.responses[argv[0]]
            return r(argv) if callable(r) else r
        return 0, _clean_output(BY_SCRIPT[argv[0]]), None, ""

    def lane_argvs(self):
        return {c[1][0]: c[1] for c in self.calls if c[1][0] != db._METER}


def _fresh_agent_dir():
    """A throwaway agent dir so the persisted end record () lands in
    temp, never in a live agent's session/. MIND_AGENT_DIR is the override seam
    _paths.py and the battery both honour (guard-2985)."""
    d = Path(tempfile.mkdtemp(prefix="deferrable-battery-test-"))
    (d / "session").mkdir()
    return d


def _run(capsys, **kw):
    kw.setdefault("runner", Rec())
    env = dict(kw.get("env") or {})
    env.setdefault("MIND_AGENT_DIR", str(_fresh_agent_dir()))
    kw["env"] = env
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


def _role_multiplier_audit_rc(monkeypatch, capsys, outcome):
    """The exit code role-multiplier-coverage-audit.py ACTUALLY returns for `outcome`.

    Read by running the script's own main() with its `audit` stubbed to the outcome,
    so the expectation comes from the producer at runtime and not from this test's
    belief about it (guard-1220). The battery's spec for this lane assumed the
    generic rc 1 = finding; the script exits 2 for MISSING and 1 for ERROR.
    """
    ra = _load("role_multiplier_audit_for_battery", "role-multiplier-coverage-audit.py")
    if outcome == "error":
        def unreadable(*_a):
            raise RuntimeError("strategy file unreadable")
        monkeypatch.setattr(ra, "audit", unreadable)
    else:
        monkeypatch.setattr(ra, "audit", lambda *_a: {
            "active_agents": ["a"], "multiplier_keys": [] if outcome == "missing" else ["a"],
            "missing": ["a"] if outcome == "missing" else [], "stale": [],
            "ok": outcome == "ok"})
    rc = ra.main([])
    capsys.readouterr()  # drain the script's own report: _run() parses only the battery's
    return rc


@pytest.mark.parametrize("outcome,bucket", [
    ("ok", "clean"),         # the control: clean under the old mapping too ()
    ("missing", "finding"),  # pre-fix: BLIND (unexpected rc=2)
    ("error", "blind"),      # pre-fix: a FINDING (rc=1 was the spec's finding code)
])
def test_role_multiplier_audit_exit_codes_land_in_the_bucket_the_script_means(
        monkeypatch, capsys, outcome, bucket):
    rc = _role_multiplier_audit_rc(monkeypatch, capsys, outcome)
    name = "role-multiplier-coverage-audit"
    rep = _run(capsys, runner=Rec(responses={
        "role-multiplier-coverage-audit.py": (rc, "", None, f"audit says {outcome}")}))
    found = {f["name"] for f in rep["findings"]}
    blind = {b["name"] for b in rep["blind"]}
    got = "finding" if name in found else "blind" if name in blind else "clean"
    assert got == bucket, (outcome, rc, rep["findings"], rep["blind"])


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
    db.run(as_json=False, apply=True, runner=Rec(),
           env={"MIND_AGENT_DIR": str(_fresh_agent_dir())})
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
    db.run(as_json=False, apply=True,
           env={"MIND_AGENT_DIR": str(_fresh_agent_dir())},
           runner=Rec(responses={
               "abandoned-claim-check.sh": (0, "  RELEASABLE g-1-1 claimed_by=a\n", None, "")}))
    out = capsys.readouterr().out
    assert "▸ FINDING: abandoned-claim-check (phase 0.5b.23)" in out
    assert "1 finding / " in out


def test_save_dir_keeps_each_lanes_raw_output(capsys, tmp_path):
    _run(capsys, runner=Rec(responses={"locus-sweep.sh": (0, '{"population": 1}', None, "w")}),
         save_dir=str(tmp_path))
    assert (tmp_path / "locus-sweep.out").read_text(encoding="utf-8") == '{"population": 1}'
    assert (tmp_path / "locus-sweep.err").read_text(encoding="utf-8") == "w"


# -- the persisted end record + the signature diff () -----------------

def _drop_rows(agent_dir):
    log = Path(agent_dir) / "session" / "precheck-drops.jsonl"
    assert log.is_file(), "the battery persisted no deferrable-battery-end row"
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_a_clean_run_persists_its_end_record_beside_the_precheck_tail(capsys, tmp_path):
    """Outcome 1: dropped/BLIND/held (names AND reasons) plus the battery's own
    executed count survive in a durable, fleet-synced store (precheck-drops.jsonl
    is in test_owncloud_sync's synced ephemeral list), readable from another
    box (g-115-12183)."""
    ag = tmp_path / "alpha"
    rec = Rec(agent_dir=ag)
    rep = _run(capsys, apply=True, runner=rec, env={"MIND_AGENT_DIR": str(ag)})
    rows = [r for r in _drop_rows(ag) if r.get("event") == db._END_EVENT]
    assert len(rows) == 1
    row = rows[0]
    # the meter's own rows coexist in the same log; the battery row is filterable
    assert row["checked_at"] == rep["checked_at"]
    assert row["mode"] == "apply" and row["status"] == "clean" and row["completeness"] == "complete"
    assert row["executed"] == rep["executed"]
    assert row["executed_count"] == len(rep["executed"])
    assert row["witness_failed"] == []
    # dropped/blind/held carry names and reasons, held keeps its standing flag
    assert row["held"] == [{"name": "repo-hygiene-sweep", "phase": BY_NAME["repo-hygiene-sweep"]["phase"],
                            "reason": BY_NAME["repo-hygiene-sweep"]["hold"], "standing": True}]
    assert row["dropped"] == [] and row["blind"] == []
    # every lane that ran carries its signature, and the diff against NOTHING is
    # the FIRST apply: every signed lane is new
    assert set(row["sigs"]) == set(rep["sigs"]) and row["sigs"]
    assert all(c["prev"] is None for c in rep["sig_diff"])
    assert {c["name"] for c in rep["sig_diff"]} == set(row["sigs"])
    assert "prev_checked_at" not in rep


def test_a_meter_drop_and_a_blind_lane_land_in_the_end_record_with_reasons(capsys, tmp_path):
    ag = tmp_path / "alpha"
    rec = Rec(agent_dir=ag, meter=lambda lane: "drop" if lane == "locus-sweep" else "run",
              responses={"phantom-goal-audit.py": (0, "not json at all", None, "")})
    rep = _run(capsys, apply=True, runner=rec, env={"MIND_AGENT_DIR": str(ag)})
    row = next(r for r in _drop_rows(ag) if r.get("event") == db._END_EVENT)
    assert [d["name"] for d in row["dropped"]] == ["locus-sweep"]
    assert all("budget meter returned drop" == d["reason"] for d in row["dropped"])
    assert [b["name"] for b in row["blind"]] == ["phantom-goal-audit"]
    assert row["blind"][0]["reason"].startswith("unparseable output")
    # the drop log already carried the meter's drop decision; the battery record
    # is the stage-level companion, not a duplicate of it
    assert row["executed_count"] == len(rep["executed"])


def test_an_over_budget_drop_and_a_witness_failure_are_each_their_own_class(capsys, tmp_path):
    """The 2026-10-07 shape () had three classes and only one
    (over-budget) had a home: meter-decided drops. The other two --
    battery-over-budget and the swallowed witness -- now persist with reasons
    (g-115-12183)."""
    ag = tmp_path / "alpha"
    now = [0.0]

    def eat(argv):
        now[0] += 1000
        return 0, _clean_output(BY_SCRIPT[argv[0]]), None, ""

    rec = Rec(agent_dir=ag, responses={"pending-questions-sweep.sh": eat})

    def broken(argv, timeout):
        # a broken meter: rc=0 but it never appends (the fail-open no-op)
        if argv[0] == db._METER and argv[1] == "executed":
            return 0, "", None, ""
        return rec(argv, timeout)

    rep = _run(capsys, apply=True, runner=broken, env={"MIND_AGENT_DIR": str(ag)},
               clock=lambda: now[0], budget_s=100)
    row = next(r for r in _drop_rows(ag) if r.get("event") == db._END_EVENT)
    over = {d["name"] for d in row["dropped"] if "over budget" in d["reason"]}
    assert "stalled-goal-ratchet" in over, row["dropped"]
    # every lane that ran completed but its witness never landed
    assert {w["name"] for w in row["witness_failed"]} == set(row["executed"])
    assert all("appended no executed record" in w["why"] for w in row["witness_failed"])
    # it still counts as executed -- the lane RAN; it only lacks the meter's record
    assert row["executed_count"] == len(row["executed"])
    # the report's error line does not hide behind a clean status
    assert rep["status"] == "clean" and row["completeness"] == "partial"


def test_the_witness_uses_landing_not_exit_code(tmp_path, monkeypatch):
    """rc=0 with a no-op is FAILED: the meter's fail-open arms (no state, disabled,
    bad state, write failure) all exit 0, and in each the lane ran but
    tail_executed will not carry it."""
    ag = tmp_path / "alpha"
    ag.joinpath("session").mkdir(parents=True)
    state = ag / "session" / "precheck-budget-state.json"
    state.write_text(json.dumps({"disabled": False, "sweeps": []}), encoding="utf-8")
    env = {"MIND_AGENT_DIR": str(ag)}

    def call(rec):
        return db._witness_executed(rec, "locus-sweep", env)

    assert call(Rec(agent_dir=ag)) == (True, "")  # window open: the row lands

    state.unlink()
    ok, why = call(Rec(agent_dir=ag))
    assert not ok and "state file absent" in why

    state.write_text(json.dumps({"disabled": True, "sweeps": []}), encoding="utf-8")
    ok, why = call(Rec(agent_dir=ag))
    assert not ok and "disabled" in why  # the meter's own no-op arm

    state.write_text("{corrupt", encoding="utf-8")
    ok, why = call(Rec(agent_dir=ag))
    assert not ok and "unreadable" in why

    monkeypatch.setattr(db, "_state_file_path", lambda env=None: None)
    ok, why = call(Rec(agent_dir=ag))
    assert not ok and "unresolvable" in why


def test_classify_shortfall_explains_the_2026_10_07_shape_from_stored_data_alone():
    """Outcome 2: tail_considered above tail_executed with sweeps_dropped 0 must be
    classified from the two stored rows alone, and each class is pinned by name."""
    # the 2026-10-07T01:08:22 shape: 53 considered, 22 executed, 0 dropped
    end = {"event": "precheck-end", "tail_considered": 53, "tail_executed": 22,
           "sweeps_dropped": 0}
    battery = {
        "event": db._END_EVENT, "executed": ["m1", "m2", "m3"], "executed_count": 3,
        "witness_failed": [{"name": "locus-sweep", "why": "x"}],
        "dropped": [{"name": "stalled-goal-ratchet", "reason": "over budget (400s) — not started"}],
        "blind": [{"name": "phantom-goal-audit", "reason": "unparseable output"}],
        "held": [{"name": "repo-hygiene-sweep", "reason": "held lane", "standing": True}],
    }
    out = db.classify_shortfall(end, battery)
    assert out["shortfall"] == 31 and out["battery_record"] == "present"
    assert out["over_budget_dropped"] == ["stalled-goal-ratchet"]
    assert out["blind"] == ["phantom-goal-audit"]
    assert out["unwitnessed"] == ["locus-sweep"]
    # the classes name every considered-but-unwitnessed lane; the held lane is NOT
    # one of them (it never asked the meter)
    assert out["residual"] == 31 - 3 and out["exhausted"] is False
    assert out["battery_considered"] == 3 + 1 + 1 + 1  # executed + each class, no held

    # an EXHAUSTED row: the three classes account for the whole shortfall
    end2 = {"tail_considered": 3, "tail_executed": 0, "sweeps_dropped": 0}
    out2 = db.classify_shortfall(end2, battery)
    assert out2["shortfall"] == 3 and out2["exhausted"] is True and out2["residual"] == 0

    # the meter's own drops are a fourth, already-named class: they count too
    end3 = {"tail_considered": 5, "tail_executed": 0, "sweeps_dropped": 2}
    battery3 = dict(battery, dropped=battery["dropped"] + [
        {"name": "a", "reason": "budget meter returned drop"},
        {"name": "b", "reason": "budget meter returned drop"}])
    out3 = db.classify_shortfall(end3, battery3)
    assert out3["exhausted"] is True  # 2 meter-drops + 3 battery classes = 5


def test_classify_shortfall_never_counts_held_lanes_and_never_guesses_the_residual():
    end = {"tail_considered": 5, "tail_executed": 4, "sweeps_dropped": 0}
    battery = {"event": db._END_EVENT, "executed": ["m"], "executed_count": 4,
               "witness_failed": [], "dropped": [], "blind": [],
               "held": [{"name": "h", "reason": "r", "standing": False}] * 3}
    out = db.classify_shortfall(end, battery)
    assert out["shortfall"] == 1 and out["exhausted"] is False
    assert out["unwitnessed"] == [] and out["blind"] == [] and out["over_budget_dropped"] == []
    # the residual is the MEDIUM/digest side's share (or a meter-lost witness) --
    # named, not guessed
    assert out["residual"] == 1

    # a battery record that never ran: the shortfall is UNEXPLAINED, and that is
    # the verdict, not a guess ()
    out2 = db.classify_shortfall(end, None)
    assert out2["battery_record"] == "absent" and "unexplained" in out2["error"]


def test_classify_shortfall_rejects_rows_without_a_tail_count():
    out = db.classify_shortfall({"event": "precheck-end"}, {"event": db._END_EVENT})
    assert out["error"] == "the precheck-end row is missing a tail count"
    out = db.classify_shortfall({"tail_considered": 1, "tail_executed": 0, "sweeps_dropped": 0},
                                {"event": db._END_EVENT})  # no executed_count key
    assert "missing executed_count" in out["error"]


def _reset_log_to_first_end_row(agent_dir):
    """The log is append-only in production; a test may rewind its OWN fixture.
    Truncates to the first end row INCLUSIVE so the next run's diff base is it."""
    log = Path(agent_dir) / "session" / "precheck-drops.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines(keepends=True)
    keep = next(i for i, ln in enumerate(lines)
                if json.loads(ln).get("event") == db._END_EVENT)
    log.write_text("".join(lines[:keep + 1]), encoding="utf-8")


def test_sig_diff_prints_changed_lanes_first_and_unchanged_as_one_line_rows(capsys, tmp_path):
    """Outcome 3: lane signatures persist, and the stage prints changed lanes
    first (new / gone / value changed), unchanged lanes as one-line rows."""
    ag = tmp_path / "alpha"
    env = {"MIND_AGENT_DIR": str(ag)}
    changed = {"verdict": "CHANGED", "files_scanned": 99, "roots_skipped": ["x"],
               "tier_counts": {"active-scope": 0}}
    # apply 1: a known baseline
    _run(capsys, apply=True, env=env)
    row1 = next(r for r in _drop_rows(ag) if r.get("event") == db._END_EVENT)

    # apply 2 (JSON): one lane's value changes, cadence-battery is skipped (gone)
    rep2 = _run(capsys, apply=True, env=env, skip={"cadence-battery"},
                runner=Rec(responses={"hardcoded-scope-audit.py":
                                      (0, json.dumps(changed), None, "")}))
    assert rep2["prev_checked_at"] == row1["checked_at"]
    diff = {c["name"]: c for c in rep2["sig_diff"]}
    # changed: value change; gone: the skipped lane; everything else unchanged
    assert "hardcoded-scope-audit" in diff and diff["hardcoded-scope-audit"]["prev"] is not None
    assert diff["hardcoded-scope-audit"]["now"] != diff["hardcoded-scope-audit"]["prev"]
    assert "cadence-battery" in diff and diff["cadence-battery"]["now"] is None
    unchanged = set(row1["sigs"]) - {"hardcoded-scope-audit", "cadence-battery"}
    assert unchanged and unchanged.isdisjoint(diff.keys())

    # rewind to the baseline so the HUMAN render below diffs against row1
    _reset_log_to_first_end_row(ag)

    # the human output: the CHANGED block before the = rows, one line each
    db.run(as_json=False, apply=True, env=env, skip={"cadence-battery"},
           runner=Rec(responses={"hardcoded-scope-audit.py":
                                 (0, json.dumps(changed), None, "")}))
    out = capsys.readouterr().out
    ci = out.index("  CHANGED (2):")
    first_eq = out.index("    = ")
    assert ci < first_eq  # changed lanes print FIRST
    assert "  ~ hardcoded-scope-audit" in out
    assert "  - cadence-battery" in out
    for name in unchanged:
        line = next(ln for ln in out.splitlines()
                    if ln.startswith(f"    = {name} ("))
        assert line == f"    = {name} (phase {BY_NAME[name]['phase']}) {row1['sigs'][name]}"

    # apply 3 (human, nothing changed since the previous row): no CHANGED block
    db.run(as_json=False, apply=True, env=env, skip={"cadence-battery"},
           runner=Rec(responses={"hardcoded-scope-audit.py":
                                 (0, json.dumps(changed), None, "")}))
    out3 = capsys.readouterr().out
    assert "  CHANGED (" not in out3
    assert out3.count("    = ") == len(row1["sigs"]) - 1  # every lane but the gone one

    # apply 4 (human, full set): the returned cadence-battery is a NEW lane,
    # printed first as a change, the rest as one-line rows. Same `changed`
    # response as apply 3, so hardcoded-scope-audit is NOT a second change and
    # the diff is exactly the returned lane.
    db.run(as_json=False, apply=True, env=env,
           runner=Rec(responses={"hardcoded-scope-audit.py":
                                 (0, json.dumps(changed), None, "")}))
    out4 = capsys.readouterr().out
    assert "  CHANGED (1):" in out4
    assert "+ cadence-battery (phase" in out4
    assert out4.index("+ cadence-battery") < out4.index("    = ")
    assert out4.count("    = ") == len(row1["sigs"]) - 1


def test_first_apply_prints_every_signed_lane_as_new(capsys, tmp_path):
    ag = tmp_path / "alpha"
    env = {"MIND_AGENT_DIR": str(ag)}
    db.run(as_json=False, apply=True, env=env, runner=Rec())
    out = capsys.readouterr().out
    assert "SIGNATURES (first apply — no previous record):" in out
    assert "  CHANGED (" in out  # and every new lane is a changed lane
    assert "+ cadence-battery (phase 0.5e) new —" in out  # a new lane is printed first
    # the JSON report for a SECOND, fresh log: every diff entry is new (prev None)
    ag2 = tmp_path / "beta"
    rep = _run(capsys, apply=True, env={"MIND_AGENT_DIR": str(ag2)})
    assert {c["name"] for c in rep["sig_diff"]} == set(rep["sigs"])
    assert all(c["prev"] is None and c["now"] is not None for c in rep["sig_diff"])


def test_a_worker_body_skips_and_persists_nothing(capsys, tmp_path):
    ag = tmp_path / "alpha"
    rep = _run(capsys, runner=Rec(), env={"BODY_ROLE": "worker", "MIND_AGENT_DIR": str(ag)})
    assert "worker Body" in rep["skipped"]
    assert not (ag / "session" / "precheck-drops.jsonl").exists()
    assert rep["sig_diff"] is None


def test_an_unwritable_drop_log_is_an_error_line_not_a_crash(capsys, tmp_path, monkeypatch):
    """Fail-open: persist failure is an ERROR LINE (), never a
    raised exception and never a blocked loop; the report still carries the
    full stage result."""
    ag = tmp_path / "alpha"
    env = {"MIND_AGENT_DIR": str(ag)}
    # point the log at a path whose parent cannot be created (a file, not a dir):
    # persist hits OSError, the run continues
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(db, "_drop_log_path",
                        lambda env=None: blocker / "precheck-drops.jsonl")
    rep = _run(capsys, apply=True, runner=Rec(), env=env)
    assert "error" in rep and "drop-log persist" in rep["error"]
    # the run itself is intact: every lane accounted for, verdicts unaffected
    assert {r["name"] for r in rep["lanes"]} == set(BY_NAME)
    assert rep["status"] == "clean"
    # the diff base read is equally fail-open: a missing log means first apply
    assert "prev_checked_at" not in rep


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
