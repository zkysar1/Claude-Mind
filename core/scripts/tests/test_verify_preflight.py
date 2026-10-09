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
- An artifact last modified before the unit's claim is WARNED about and never
  refused, and a claim time that cannot be read for this goal reads "currency not
  checked", never current. Only the mtime is compared, so file names play no part.

The harness runs the real gates and records the writes instead of making them,
so no test touches a live checkpoint, diary or working memory.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
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
# Per-session scratch paths, built from the dir name the advisory's predicate reads.
from _paths import SESSIONS_DIRNAME  # noqa: E402
SCRATCH = f"agents/charlie/{SESSIONS_DIRNAME}/57c55134e5b5429cbfa2dbd142b9574d/scratch/run.log"
PROBE_OUT = f"agents/charlie/{SESSIONS_DIRNAME}/57c55134e5b5429cbfa2dbd142b9574d/scratch/probe.out"
CLAIMED = "2026-10-05T00:44:08"  # a checkpoint's selected_at: naive UTC


class Harness:
    """A pre-flight Runner. Gates run for real against tmp fixtures; the writes
    (checkpoint, diary, working memory) are recorded, never made."""

    def __init__(self, tmp_path, note=GOOD_NOTE, checks=(), anchor="g-1-1", overrides=None,
                 selected_at=None):
        self.tmp = tmp_path
        self.checks = list(checks)
        self.anchor = anchor
        self.selected_at = selected_at    # the checkpoint's claim time; absent when None
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
            cp = {"goal_id": self.anchor, **({"selected_at": self.selected_at} if self.selected_at else {})}
            return (0, json.dumps(cp), "") if self.anchor else (1, "null", "")
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


def stamp(path, iso):
    """Set a file's modification time to a naive-UTC ISO time, as a checkpoint states one."""
    t = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()
    os.utime(path, (t, t))
    return str(path)


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


def test_a_q4_refusal_gives_each_finding_kind_its_own_remedy(tmp_path):
    """. One shared remedy line came before repeat Q4 FAILs: 3 of the 6
    reruns beyond the skill's single rerun, over 28 worker closes. The two
    citation kinds are fixed in opposite ways (guard-6180), only a whole-file
    Read clears a decorative one, and a curl fetch is recorded since
    g-115-9263, so the old "cat or curl is invisible" was half false."""
    r = run(Harness(tmp_path), [art(tmp_path, "uncited.md", UNCITED)])
    remedy = r["results"]["q4"]["remedy"]
    assert "missing-citation:" in remedy and "decorative-citation:" in remedy, remedy
    assert "with no offset or limit" in remedy, remedy
    assert "cat or curl is invisible" not in remedy, remedy


# ─── the session-scratch advisory reaches the closer () ────────────

def test_each_session_scratch_citation_is_a_warning_and_a_cited_note_adds_none(tmp_path):
    """The gate's advisory () prints on stderr, which the pre-flight reads
    only for unreadable output, so the closer never saw it while the note could
    still change. Now each citation in the gate's JSON line is one warning naming
    the paragraph, the path and what is missing, and the verdict is unchanged: the
    advisory never refuses. The control, on the same harness, is a note with the
    lines quoted beside the path and the host named, and it adds no warning
    (guard-4166)."""
    bare = f"{GOOD_NOTE}\n\nThe run is kept at {SCRATCH}.\n\nThe probe output is {PROBE_OUT}."
    r = run(Harness(tmp_path, note=bare))
    ce = r["results"]["closure-evidence"]
    assert (ce["state"], r["rc"]) == ("PASS", 0)
    assert ce["findings"] == [
        f"warning: paragraph 2 cites session scratch no other box can open: {SCRATCH} "
        "(no lines inline beside it; the note names no host)",
        f"warning: paragraph 3 cites session scratch no other box can open: {PROBE_OUT} "
        "(no lines inline beside it; the note names no host)",
        "warning: keep each path above as a pointer; beside it, quote in backticks the lines the "
        f"claim rests on, and name the box as \"hostname {socket.gethostname()}\" (g-375-52, guard-7485)"]
    cited = (f"{GOOD_NOTE}\n\nMeasured on hostname box-7: `VERDICT: CLEAN  TOTAL: 42 passed` "
             f"by the suite run kept at {SCRATCH}.")
    r = run(Harness(tmp_path, note=cited))
    assert (r["results"]["closure-evidence"]["state"], r["rc"]) == ("PASS", 0)
    assert r["results"]["closure-evidence"]["findings"] == []


def test_a_skipped_table_check_keeps_its_warnings_and_a_faulted_advisory_is_one(
        tmp_path, monkeypatch, capsys):
    """The advisory reads the note whatever the table check decided, so a goal with
    nothing to evidence still shows its warnings, and still reads SKIPPED, never
    PASS. A faulted advisory is a warning too, so a check that did not run never
    reads as a clean note (guard-2421). The faulted line comes from the real gate,
    run in-process with its predicate made to raise as the gate's own test does,
    so renaming the field on either side turns this red."""
    h = Harness(tmp_path)
    h.goal_json.write_text(json.dumps({"id": "g-1-1", "outcome_note": f"Kept at {SCRATCH} for review."}),
                           encoding="utf-8")
    r = run(h)
    ce = r["results"]["closure-evidence"]
    assert ce["state"] == "SKIPPED"
    assert ce["findings"][0] == (f"warning: paragraph 1 cites session scratch no other box can open: "
                                 f"{SCRATCH} (no lines inline beside it; the note names no host)")
    spec = importlib.util.spec_from_file_location("closure_evidence_gate_cli",
                                                  CORE_SCRIPTS / "closure-evidence-gate.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(cli, "scratch_citations", boom)
    monkeypatch.setattr(cli, "_gate_log", lambda *a, **k: None)  # no gate-firing row is written
    faulted = tmp_path / "faulted.json"
    faulted.write_text(json.dumps({**GOAL, "outcome_note": f"{GOOD_NOTE}\n\nKept at {SCRATCH}."}),
                       encoding="utf-8")
    assert cli.main(["--goal", "g-1-1", "--goal-json", str(faulted)]) == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    r = run(Harness(tmp_path, overrides={"closure-evidence-gate.py": (0, line, "")}))
    ce = r["results"]["closure-evidence"]
    assert (ce["state"], ce["findings"]) == ("PASS", [
        "warning: the session-scratch advisory did not run (boom), so its silence says nothing "
        "about the note"])


def test_advisory_entries_this_cannot_read_are_one_warning_never_a_crash(tmp_path):
    """Every other gate output this cannot read becomes a verdict, never a crash,
    because a crash costs every other check's verdict (guard-2298). Advisory entries
    of a shape it cannot read are one warning, and the table's verdict stands. The
    real gate never writes these shapes, so the lines are canned; the well-formed
    control is the first test in this section."""
    for bad in (["x"], [{"paragraph": 1, "paths": [None], "missing": ["excerpt"]}], {"paragraph": 1}):
        line = {"gate": "closure-evidence-gate", "decision": "pass", "goal_id": "g-1-1",
                "note_source": "the record's outcome_note", "problems": [], "warnings": [],
                "reason": None, "scratch_citations": bad}
        r = run(Harness(tmp_path, overrides={"closure-evidence-gate.py": (0, json.dumps(line), "")}))
        ce = r["results"]["closure-evidence"]
        assert (ce["state"], r["rc"], len(ce["findings"])) == ("PASS", 0, 1), bad
        assert ce["findings"][0].startswith(
            "warning: the session-scratch advisory's entries could not be read ("), bad


def test_a_refused_close_prints_the_gate_fix_commands_as_given(tmp_path):
    """. The real gate's fix commands ride its JSON line and close the
    FAIL remedy unindented, since a here-document's closing word must be at
    column 0. A remedy of a shape this cannot read costs only the commands."""
    r = run(Harness(tmp_path, note="OUTCOME 1: NOT MET — the widget does not build."))
    ce = r["results"]["closure-evidence"]
    assert (ce["state"], r["rc"]) == ("FAIL", 3)
    fix = ce["remedy"].split("\n")
    assert fix[0].startswith("write or correct the evidence table in the outcome note")
    assert "bash core/scripts/aspirations-add-goal.sh --source world asp-1 <<'GOAL'" in fix
    assert ("OUTCOME 1 (corrected): NOT MET - <what is missing>; deferred to <live goal-id>"
            in fix)
    assert "\nGOAL\n" in vp.render(r) and "\nROWS\n" in vp.render(r)
    line = {"gate": "closure-evidence-gate", "decision": "block", "goal_id": "g-1-1",
            "note_source": "the record's outcome_note", "problems": ["x"], "warnings": [],
            "reason": None, "remedy": "not-a-list"}
    r = run(Harness(tmp_path, overrides={"closure-evidence-gate.py": (3, json.dumps(line), "")}))
    ce = r["results"]["closure-evidence"]
    assert (ce["state"], ce["findings"]) == ("FAIL", ["x"])
    assert "\n" not in ce["remedy"] and ce["remedy"].startswith("write or correct")


# ─── an artifact from before the claim is warned about (artifact currency) ──

PLAIN = "Plain prose with no entity facts.\n"


def test_an_artifact_that_predates_the_claim_is_warned_and_one_made_after_it_is_not(tmp_path):
    """Q1's artifact is copied onto the goal verbatim, and a log left by an earlier run
    passes the existence check. A found artifact last modified before the checkpoint's
    claim time draws a warning, and a diary line because the copy is made either way.
    Nothing else changes: the verdict, the rc and the stamped pair are what they were.
    The control, on the same harness and claim, is a file modified after the claim: no
    warning and no diary line."""
    old = stamp(art(tmp_path, "cycle203.log", PLAIN), "2026-10-04T07:41:05")
    h = Harness(tmp_path, selected_at=CLAIMED)
    r = run(h, [old])
    a = r["results"]["artifact"]
    assert (a["state"], r["rc"], a["data"]["stale"]) == ("PASS", 0, [old])
    assert len(a["findings"]) == 1 and a["findings"][0].startswith(
        f"warning: {old} predates this unit's claim (modified 2026-10-04T07:41:05, "
        f"claimed {CLAIMED}, 17 h 3 min earlier)"), a["findings"]
    assert a["summary"].endswith("; 1 predate this unit's claim")
    assert ["phase_progress.q1_passed=true", "--set", f"phase_progress.q1_artifact={old}"] in h.checkpoint_sets()
    assert f"Q1 artifact predates this unit's claim: {old}" in h.diary()
    new = stamp(art(tmp_path, "cycle220.log", PLAIN), "2026-10-05T00:50:00")
    h = Harness(tmp_path, selected_at=CLAIMED)
    r = run(h, [new])
    a = r["results"]["artifact"]
    assert (a["state"], r["rc"], a["findings"], a["data"]["stale"]) == ("PASS", 0, [], [])
    assert a["summary"] == f"1 file(s) exist: {new}"
    assert not any(d.startswith("Q1 artifact predates") for d in h.diary())


def test_currency_follows_the_modification_time_never_the_file_name_or_its_order(tmp_path):
    """The observed value was the EARLIER of two logs in one directory, and a first guess
    was that the lexicographically first name wins. Both assignments of the older mtime
    (to the lower name, then to the higher) are run with the files passed in both orders:
    it is always the older file, and only it, that draws the warning."""
    lo, hi = str(tmp_path / "cycle203.log"), str(tmp_path / "cycle220.log")
    for older, newer in ((lo, hi), (hi, lo)):
        stamp(art(tmp_path, Path(older).name, PLAIN), "2026-10-04T07:41:05")
        stamp(art(tmp_path, Path(newer).name, PLAIN), "2026-10-05T00:50:00")
        for passed in ([lo, hi], [hi, lo]):
            a = run(Harness(tmp_path, selected_at=CLAIMED), passed)["results"]["artifact"]
            assert a["data"]["stale"] == [older], (older, passed)
            assert len(a["findings"]) == 1 and a["findings"][0].startswith(
                f"warning: {older} predates this unit's claim"), (older, passed)


def test_a_file_modified_at_the_claim_is_current_and_one_second_before_it_is_not(tmp_path):
    """The boundary: an artifact is current from the second of the claim on."""
    at = stamp(art(tmp_path, "at.log", PLAIN), CLAIMED)
    before = stamp(art(tmp_path, "before.log", PLAIN), "2026-10-05T00:44:07")
    a = run(Harness(tmp_path, selected_at=CLAIMED), [at])["results"]["artifact"]
    assert (a["findings"], a["data"]["stale"]) == ([], [])
    a = run(Harness(tmp_path, selected_at=CLAIMED), [before])["results"]["artifact"]
    assert a["data"]["stale"] == [before]
    assert "0 h 0 min earlier" in a["findings"][0]


@pytest.mark.parametrize("anchor,selected_at,why", [
    ("g-1-1", None, "the checkpoint's selected_at None is not a time"),
    ("g-1-1", "yesterday", "the checkpoint's selected_at 'yesterday' is not a time"),
    ("g-9-9", CLAIMED, "the checkpoint anchors g-9-9, not g-1-1"),
    (None, CLAIMED, "no checkpoint anchors this goal"),
])
def test_a_claim_time_that_cannot_be_read_for_this_goal_is_said_and_never_read_as_current(
        tmp_path, anchor, selected_at, why):
    """The file is dated 2020, so a claim time would flag it. With none readable for THIS
    goal the check says it did not run: it neither flags the file nor calls it current,
    and it never refuses. Each case is paired with the readable-claim control in the
    first test of this section."""
    old = stamp(art(tmp_path, "cycle203.log", PLAIN), "2020-01-01T00:00:00")
    h = Harness(tmp_path, anchor=anchor, selected_at=selected_at)
    r = run(h, [old])
    a = r["results"]["artifact"]
    assert (a["state"], r["rc"], a["data"]["stale"]) == ("PASS", 0, [])
    assert a["findings"] == [f"warning: currency not checked ({why}), so a file left by an "
                             "earlier run would read the same as this unit's"]
    assert not any(d.startswith("Q1 artifact predates") for d in h.diary())


@pytest.mark.parametrize("raw,expected", [
    ("2026-10-05T00:44:08", datetime(2026, 10, 5, 0, 44, 8)),
    ("2026-10-05T00:44:08Z", datetime(2026, 10, 5, 0, 44, 8)),
    ("2026-10-05T02:44:08+02:00", datetime(2026, 10, 5, 0, 44, 8)),
    ("2026-10-05T00:44:08.250000", datetime(2026, 10, 5, 0, 44, 8, 250000)),
])
def test_the_claim_time_is_read_as_naive_utc(raw, expected):
    assert vp.claim_time({"goal_id": "g-1-1", "selected_at": raw}, "g-1-1") == (expected, "")


def test_a_no_write_run_still_checks_currency_and_writes_nothing(tmp_path):
    """--no-write is for a reader or a goal you are not closing; it still reads the
    checkpoint, so one hand-picked artifact can be checked before it is stamped."""
    old = stamp(art(tmp_path, "cycle203.log", PLAIN), "2026-10-04T07:41:05")
    h = Harness(tmp_path, selected_at=CLAIMED)
    r = run(h, [old], write=False)
    assert r["results"]["artifact"]["data"]["stale"] == [old]
    assert [h.writes(n) for n in ("loop-state-save.sh", "execution-diary.sh", "wm-append.sh")] == [[], [], []]


def test_an_mtime_this_cannot_convert_is_one_warning_never_a_crash(tmp_path, monkeypatch):
    """A crash here would cost every other check's verdict (guard-2298). The file reports a
    modification time past the year 9999, which no datetime holds. It is planted through
    stat and not os.utime, because a filesystem may clamp a far-future utime to a time it
    can hold (ext4 stops at 2446) and then nothing fails there: this test was green on
    tmpfs and red on ext4 until then. The control, on the same harness, is a file with an
    ordinary mtime and no such warning."""
    odd = art(tmp_path, "odd.log", PLAIN)
    real_stat = Path.stat

    def stat(self, *a, **k):
        st = real_stat(self, *a, **k)
        if str(self) != odd:
            return st
        return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
                               st.st_size, int(st.st_atime), 2 ** 40, int(st.st_ctime)))
    monkeypatch.setattr(Path, "stat", stat)
    r = run(Harness(tmp_path, selected_at=CLAIMED), [odd])
    a = r["results"]["artifact"]
    assert (a["state"], r["rc"]) == ("PASS", 0)
    assert len(a["findings"]) == 1 and a["findings"][0].startswith(
        f"warning: currency not checked for {odd} ("), a["findings"]
    ok = stamp(art(tmp_path, "ok.log", PLAIN), "2026-10-05T00:50:00")
    assert run(Harness(tmp_path, selected_at=CLAIMED), [ok])["results"]["artifact"]["findings"] == []


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
    # The checkpoint is READ, since its claim time feeds the artifact's currency; a read is no write.
    assert [h.writes(n) for n in ("loop-state-save.sh", "execution-diary.sh", "wm-append.sh")] == [[], [], []]


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
