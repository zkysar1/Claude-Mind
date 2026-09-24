"""Pins the closure-evidence gate ().

THE DEFECT: g-373-125 closed on narrative. Its outcome 2 required a unit to land
"within 30s of the POST"; the note said "landed within ~5s" with no timestamp
(the first check ran ~115 s after the POST). It called a session-scratch file
"owncloud push OK", and session scratch is machine-local, so the store never
had it. The own-unit verify passed both.

Every refusal below is paired with a control on the same fixture that must pass
(guard-1082: a bare `rc != 0` is also satisfied by a usage error). The module
tests inject the store probe; the CLI tests run the real script against a tmp
world through the MIND_WORLD seam with STORAGE_BACKEND=local.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from gates.closure_evidence import (  # noqa: E402
    classify, evaluate, parse_rows, refusal_text, timestamps)

ROOTS = {"project": Path("/opt/mind"), "world": Path("/opt/mind/.mind-data/world"),
         "meta": Path("/opt/mind/.mind-data/meta"), "agents": Path("/opt/mind/agents")}
SCRATCH = ("agents/charlie/sessions/57c55134e5b5429cbfa2dbd142b9574d/scratch/"
           "g-373-125-root-cause-analysis.md")
G373125 = {"id": "g-373-125", "verification": {"outcomes": [
    "Root cause named from IntentEngineVerticle/SeedGetterVerticle code + a live or replayed log",
    "On a fresh fileworld-smoke vessel, the unit appears in /reportapi/units within 30s of "
    "the POST, no JVM restart - measured live, dev-first"]}}


def _probe(store=None, local=None, machine_local=("/sessions/",)):
    """store/local: sets of path suffixes present there (None = everything)."""
    def probe(path, kind):
        s = str(path).replace("\\", "/")
        ml = any(m in s for m in machine_local)
        here = local is None or any(s.endswith(x) for x in local)
        there = store is None or any(s.endswith(x) for x in store)
        return {"local": here, "store": (None if ml else there), "machine_local": ml,
                "is_dir": False}
    return probe


def _eval(note, goal=G373125, **kw):
    return evaluate(goal, note, roots=ROOTS, probe=kw.pop("probe", _probe()), **kw)


# ─── the two  shapes, and their controls ─────────────────────────

def test_a_scratch_file_called_pushed_is_refused():
    note = (f"OUTCOME 1: MET — gated at IntentEngineVerticle.java:2514. Evidence: {SCRATCH} "
            f"(9334 bytes, owncloud push OK).\n\nOUTCOME 2: MET — POST 08:50:29, seen 08:50:33.")
    r = _eval(note)
    assert r["decision"] == "block"
    assert "machine-local" in r["problems"][0] and "store claim cannot be true" in r["problems"][0]
    assert len(r["problems"]) == 1, r["problems"]


def test_the_same_scratch_file_without_a_store_claim_passes():
    note = (f"OUTCOME 1: MET — gated at IntentEngineVerticle.java:2514. Analysis kept at "
            f"{SCRATCH} (9334 bytes).\n\nOUTCOME 2: MET — POST 08:50:29, seen 08:50:33.")
    assert _eval(note)["decision"] == "pass"


def test_a_time_bound_outcome_answered_with_an_approximate_interval_is_refused():
    note = ("OUTCOME 1: MET — replayed log 1790139089616.jsonl, 2,453 records.\n\n"
            "OUTCOME 2: MET — unit rca-verify.md landed within ~5s via GET /reportapi/units.")
    r = _eval(note)
    assert r["decision"] == "block"
    (p,) = r["problems"]
    assert p.startswith("OUTCOME 2 (MET): outcome 2 sets a time bound ('within 30s')")
    assert "cites 0 timestamp(s)" in p


def test_the_measured_remeasurement_with_epoch_stamps_passes():
    """echo's real re-run of outcome 2: three epoch-ms stamps behind the interval."""
    note = ("OUTCOME 1: MET — replayed log 1790139089616.jsonl, 2,453 records, md5 55a2099b.\n\n"
            "OUTCOME 2: MET — POST 1790162002705, unit seen 1790162005298 (poll 3), changeLog "
            "stamped 1790162005263 -> 2.56 s / 2.77 s.")
    assert _eval(note)["decision"] == "pass"


def test_an_approximate_interval_needs_timestamps_even_without_a_bound():
    goal = {"id": "g-1-1", "verification": {"outcomes": ["the cache refreshes"]}}
    assert _eval("OUTCOME 1: MET — refreshed in about 3 minutes, see run 88.", goal=goal)["decision"] == "block"
    assert _eval("OUTCOME 1: MET — refreshed about 3 minutes after the edit: edit 10:01:05, "
                 "refresh 10:04:11.", goal=goal)["decision"] == "pass"


# ─── store and path checks ───────────────────────────────────────────────

GOAL1 = {"id": "g-1-1", "verification": {"outcomes": ["the node is written"]}}


def test_a_governed_path_in_neither_store_nor_disk_is_refused():
    r = _eval("OUTCOME 1: MET — wrote world/knowledge/tree/x.md (3 KB).", goal=GOAL1,
              probe=_probe(store=set(), local=set()))
    assert r["decision"] == "block"
    assert "world/knowledge/tree/x.md is in neither the store nor this box" in r["problems"][0]


def test_a_governed_path_in_the_store_passes():
    assert _eval("OUTCOME 1: MET — wrote world/knowledge/tree/x.md (3 KB).", goal=GOAL1,
                 probe=_probe(store={"x.md"}, local=set()))["decision"] == "pass"


def test_a_local_only_path_warns_and_refuses_only_under_a_store_claim():
    probe = _probe(store=set(), local={"x.md"})
    quiet = _eval("OUTCOME 1: MET — wrote world/knowledge/tree/x.md (3 KB).", goal=GOAL1, probe=probe)
    assert quiet["decision"] == "pass"
    assert "only on this box" in quiet["warnings"][0]
    claimed = _eval("OUTCOME 1: MET — wrote world/knowledge/tree/x.md, synced to the store (3 KB).",
                    goal=GOAL1, probe=probe)
    assert claimed["decision"] == "block"
    assert "the row says it does" in claimed["problems"][0]


def test_a_row_that_asserts_absence_is_not_refused_for_a_missing_path():
    goal = {"id": "g-1-1", "verification": {"outcomes": ["the dead script is removed"]}}
    r = _eval("OUTCOME 1: MET — core/scripts/old-thing.sh removed in 1a2b3c4d.", goal=goal,
              probe=_probe(store=set(), local=set()))
    assert r["decision"] == "pass"
    assert "row asserts absence" in r["warnings"][0]


def test_paths_this_box_cannot_resolve_are_left_alone():
    """A product repo, another host's log, an endpoint: no check, so no refusal."""
    note = ("OUTCOME 1: MET — /opt/GitHub/Ayoai/Server/build.gradle.kts edited, "
            "/var/log/zakcode/alpha.log line 6257, GET /reportapi/units, origin/dev at 2541c4f5.")
    assert _eval(note, goal=GOAL1, probe=_probe(store=set(), local=set()))["decision"] == "pass"


def test_classify_governed_before_project_and_maps_msys_paths():
    assert classify("world/a.md", ROOTS) == ("governed", ROOTS["world"] / "a.md")
    assert classify("/opt/mind/agents/x/y.md", ROOTS)[0] == "governed"
    assert classify("/opt/mind/core/scripts/z.py", ROOTS)[0] == "framework"
    assert classify("core/scripts/z.py", ROOTS) == ("framework", ROOTS["project"] / "core/scripts/z.py")
    assert classify("ops/mind-sidecar/provision-env.sh", ROOTS) == ("unverifiable", None)
    win = {"project": Path("C:/Mind"), "world": Path("C:/Cache/World"), "meta": None,
           "agents": Path("C:/Mind/agents"), "msys": True}
    assert classify("/c/Mind/agents/a/s.md", win)[0] == "governed"


def test_a_dot_prefixed_framework_path_is_checked_like_core():
    """path_tokens stripped the leading dot, so a .claude/ path classified as
    unverifiable and was never checked, while a core/ path was (g-001-12 review).
    Dot-prefixed after a space, a backtick and a paren; the word prefix; and a
    '.claude' embedded in a longer token, which must stay unverifiable (guard-495)."""
    missing = _probe(store=set(), local=set())
    for path in ("core/scripts/new-thing.py", ".claude/rules/new-rule.md",
                 "`.claude/skills/new-skill/SKILL.md`", "(.claude/settings.json)"):
        r = _eval(f"OUTCOME 1: MET — added {path}, 40 lines.", goal=GOAL1, probe=missing)
        assert r["decision"] == "block", (path, r)
        assert f"{path.strip('`()')} does not exist on this box" in r["problems"][0]
    present = _probe(store=set(), local={"SKILL.md"})
    assert _eval("OUTCOME 1: MET — added `.claude/skills/new-skill/SKILL.md`, 40 lines.",
                 goal=GOAL1, probe=present)["decision"] == "pass"
    assert _eval("OUTCOME 1: MET — see widget.claude/notes.md, 40 lines.",
                 goal=GOAL1, probe=missing)["decision"] == "pass"


# ─── the table's structure ────────────────────────────────────────────────

def test_no_table_is_refused_and_names_the_outcome_count():
    r = _eval("Root cause found and live-verified. Unit landed within ~5s.")
    assert r["decision"] == "block"
    assert r["problems"] == ["the note has no evidence table: 2 outcome(s) need one OUTCOME row each"]


def test_a_missing_row_is_named_with_its_outcome():
    r = _eval("OUTCOME 1: MET — replayed log 1790139089616.jsonl.")
    assert r["decision"] == "block"
    assert r["problems"][0].startswith("OUTCOME 2 has no row (\"On a fresh fileworld-smoke vessel")


def test_a_status_other_than_met_or_not_met_is_refused():
    """sig-40: a PASS the gate declined to read would be a check that never ran."""
    r = _eval("OUTCOME 1: PASS — replayed log 1790139089616.jsonl.\n\nOUTCOME 2: MET — 08:50:29 -> 08:50:33.")
    assert r["decision"] == "block"
    assert "must be MET or NOT MET" in r["problems"][0]


def test_prose_that_starts_with_outcome_is_not_a_row():
    """Measured on 182 completed notes: these lines were read as rows when the
    separator after the number was optional."""
    note = ("OUTCOME 1: MET — replayed log 1790139089616.jsonl.\n"
            "Outcome 1's decision stands.\n\n"
            "OUTCOME 5 RATCHET: scope to environments/id, reads 5 today.\n\n"
            "OUTCOME 2: MET — POST 08:50:29, seen 08:50:33.")
    parsed = parse_rows(note)
    assert [r["n"] for r in parsed["rows"]] == [1, 2] and parsed["malformed"] == []
    assert _eval(note)["decision"] == "pass"


def test_bold_and_restated_headers_parse():
    note = ("**OUTCOME 1** (root cause: named): **MET** — replayed 1790139089616.jsonl.\n\n"
            "- OUTCOME 2 (lands within 30s) — MET: POST 08:50:29, seen 08:50:33.")
    assert [(r["n"], r["status"]) for r in parse_rows(note)["rows"]] == [(1, "MET"), (2, "MET")]
    assert _eval(note)["decision"] == "pass"


def test_a_blank_line_ends_a_row():
    r = _eval("OUTCOME 1: MET — and it stands.\n\nThe log 1790139089616.jsonl shows it.\n\n"
              "OUTCOME 2: MET — POST 08:50:29, seen 08:50:33.")
    assert r["decision"] == "block"
    assert r["problems"] == ["OUTCOME 1 (MET): MET with no measured value. Cite the value and its "
                             "source (an output excerpt, a path, a sha, timestamps)"]


def test_not_met_must_be_deferred_to_a_goal():
    base = "OUTCOME 1: MET — replayed log 1790139089616.jsonl.\n\nOUTCOME 2: NOT MET — no vessel"
    assert _eval(base + " was free.")["decision"] == "block"
    assert _eval(base + " was free; deferred to g-373-130.")["decision"] == "pass"


def test_recurring_and_outcome_less_goals_are_noops():
    assert _eval("", goal={"id": "g-1-1", "recurring": True, **G373125})["decision"] == "noop"
    assert _eval("", goal={"id": "g-1-1", "verification": {"outcomes": []}})["decision"] == "noop"


def test_an_agent_leg_close_indexes_the_agent_leg_outcomes():
    goal = {"id": "g-1-1", "participants": ["agent", "user"], "verification": {
        "outcomes": ["a", "b", "c"], "outcomes_agent_leg": ["the draft is in world/x.md"]}}
    note = "agent-leg-complete; user-leg pending\nOUTCOME 1: MET — world/x.md written, 4 KB."
    r = _eval(note, goal=goal)
    assert (r["decision"], r["field"], r["required"]) == ("pass", "outcomes_agent_leg", 1)


def test_timestamps_counts_an_iso_stamp_once():
    assert timestamps("2026-09-23T08:52:24Z and 08:50:29 and 1790162002705") == [
        "2026-09-23T08:52:24Z", "08:50:29", "1790162002705"]


def test_the_refusal_is_short_and_carries_the_format_and_the_retry():
    note = (f"OUTCOME 1: MET — Evidence: {SCRATCH} (owncloud push OK).\n\n"
            f"OUTCOME 2: MET — landed within ~5s.")
    text = refusal_text("g-373-125", _eval(note), "the record's outcome_note")
    assert len(text.encode("utf-8")) <= 2048
    assert "OUTCOME <n>: MET" in text and "deferred to <goal-id>" in text
    assert "--outcome-note-file" in text and "--override-closure-evidence" in text
    # The remedy must be reachable (guard-1532): the file REPLACES the note, and a
    # table-only replacement of a long note is a shrink the daemon refuses.
    assert "keep the note's current text below" in text
    assert "aspirations-query.sh --goal-field id g-373-125 --full" in text


# ─── the CLI ──────────────────────────────────────────────────────────────

GATE = CORE_SCRIPTS / "closure-evidence-gate.py"


def _cli(tmp_path, goal, *args, stdin="", extra_env=None):
    world, meta = tmp_path / "world", tmp_path / "meta"
    (world / "knowledge").mkdir(parents=True, exist_ok=True)
    meta.mkdir(exist_ok=True)
    gj = tmp_path / "goal.json"
    gj.write_text(json.dumps(goal), encoding="utf-8")
    env = dict(os.environ)
    env.update({"MIND_WORLD": str(world), "MIND_META": str(meta), "MIND_AGENT": "testagent",
                "STORAGE_BACKEND": "local", "CLOSURE_EVIDENCE_LEDGER_DIR": str(tmp_path)})
    env.update(extra_env or {})
    proc = subprocess.run([sys.executable, str(GATE), "--goal", goal["id"], "--source", "world",
                           "--goal-json", str(gj), *args],
                          input=stdin, cwd=str(PROJECT_ROOT), env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, f"one JSON line expected, got {proc.stdout!r} / {proc.stderr!r}"
    return proc.returncode, json.loads(lines[0]), proc.stderr, world


def test_cli_refuses_a_store_key_that_does_not_exist_and_passes_once_it_does(tmp_path):
    goal = {**GOAL1, "outcome_note": "OUTCOME 1: MET — wrote world/knowledge/n.md (2 KB)."}
    rc, doc, err, world = _cli(tmp_path, goal)
    assert (rc, doc["decision"]) == (3, "block")
    assert "REFUSED. g-1-1 status was NOT changed" in err and "knowledge/n.md" in err
    (world / "knowledge" / "n.md").write_text("x", encoding="utf-8")
    rc, doc, _, _ = _cli(tmp_path, goal)
    assert (rc, doc["decision"]) == (0, "pass")


def test_cli_reads_the_note_that_will_land(tmp_path):
    good = "OUTCOME 1: MET — build 42 green, sha 1a2b3c4d."
    bad = "done, all good"
    # 1. no record note: the summary on stdin is what lands
    rc, doc, _, _ = _cli(tmp_path, GOAL1, "--summary-stdin", stdin=good)
    assert rc == 0 and doc["note_source"].startswith("the close's --summary")
    # 2. a record note stays; the summary is never written, so it is not what is checked
    rc, doc, _, _ = _cli(tmp_path, {**GOAL1, "outcome_note": bad}, "--summary-stdin", stdin=good)
    assert rc == 3 and doc["note_source"].startswith("the record's outcome_note")
    # 3. --outcome-note-file replaces the record's note, so it wins
    f = tmp_path / "note.md"
    f.write_text(good, encoding="utf-8")
    rc, doc, _, _ = _cli(tmp_path, {**GOAL1, "outcome_note": bad}, "--outcome-note-file", str(f))
    assert rc == 0 and doc["note_source"].startswith("--outcome-note-file")


def test_cli_override_passes_and_writes_one_ledger_row(tmp_path):
    rc, doc, _, _ = _cli(tmp_path, {**GOAL1, "outcome_note": "done"}, "--override", "legacy close")
    assert (rc, doc["decision"]) == (0, "override")
    rows = (tmp_path / "closure-evidence-overrides.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["justification"] == "legacy close"


# A legacy-codepage host (Windows without UTF-8 mode). An uncaught codec error
# exits 1, a crash do_verify fails open on, so each test needs its decision on
# the one JSON line, not just an rc.
CP1252 = {"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252"}


def test_cli_decodes_a_utf8_summary_on_a_legacy_codepage(tmp_path):
    # U+201D is UTF-8 E2 80 9D, and cp1252 has no byte 0x9d.
    note = "OUTCOME 1: MET — the “node” is written, 42 lines."
    rc, doc, _, _ = _cli(tmp_path, GOAL1, "--summary-stdin", stdin=note, extra_env=CP1252)
    assert (rc, doc["decision"]) == (0, "pass")


def test_cli_emits_its_json_line_on_a_legacy_codepage(tmp_path):
    # A missing row's problem quotes its outcome, and cp1252 cannot encode U+2264.
    goal = {"id": "g-1-1", "verification": {"outcomes": [
        "the node is written", "latency ≤ 30s per call"]}}
    rc, doc, err, _ = _cli(tmp_path, goal, "--summary-stdin",
                           stdin="OUTCOME 1: MET — 42 lines written.", extra_env=CP1252)
    assert (rc, doc["decision"]) == (3, "block")
    assert doc["problems"] == ['OUTCOME 2 has no row ("latency ≤ 30s per call")']
    assert "Traceback" not in err


def test_cli_fails_open_when_the_goal_record_is_unavailable(tmp_path):
    env = dict(os.environ, STORAGE_BACKEND="local", MIND_WORLD=str(tmp_path),
               CLOSURE_EVIDENCE_LEDGER_DIR=str(tmp_path))
    proc = subprocess.run([sys.executable, str(GATE), "--goal", "g-1-1", "--goal-json",
                           str(tmp_path / "absent.json"), "--summary-stdin"], input="x",
                          cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0 and json.loads(proc.stdout)["decision"] == "error"
    # do_verify sends stdout to a log, so the fault must also reach stderr (guard-2168).
    assert "g-1-1 closure evidence NOT checked, fail-open" in proc.stderr


def test_a_gate_that_cannot_run_exits_1_never_the_refusal_code(tmp_path):
    """guard-5430: tests and Bodies stage scripts without their siblings. A staged
    gate cannot import, and that crash must not read as a refusal of every close."""
    rc, doc, _, _ = _cli(tmp_path, GOAL1, "--summary-stdin", stdin="done")
    assert (rc, doc["decision"]) == (3, "block")  # control: the same note, a working gate
    staged = tmp_path / "staged" / "core" / "scripts"
    staged.mkdir(parents=True)
    shutil.copy2(GATE, staged / GATE.name)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, str(staged / GATE.name), "--goal", "g-1-1",
                           "--goal-json", str(tmp_path / "goal.json"), "--summary-stdin"],
                          input="done", cwd=str(tmp_path), env=dict(env, STORAGE_BACKEND="local"),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1 and "ModuleNotFoundError" in proc.stderr, proc.stderr


def test_a_store_the_gate_cannot_open_is_unknown_not_absent(tmp_path):
    """guard-2168: an instrument failure must not read as a true negative. The
    control is the first CLI test: the same note refuses when the store opens."""
    goal = {**GOAL1, "outcome_note": "OUTCOME 1: MET — wrote world/knowledge/n.md (2 KB)."}
    rc, doc, _, _ = _cli(tmp_path, goal, extra_env={"STORAGE_BACKEND": "no-such-backend"})
    assert (rc, doc["decision"]) == (0, "pass")
    assert doc["warnings"] == ["OUTCOME 1: world/knowledge/n.md: store unreadable and not on this box"]


# ─── the wiring: one place, both roles (guard-5132) ──────────────────────

def test_iteration_close_runs_the_gate_before_the_status_write_for_every_role():
    src = (CORE_SCRIPTS / "iteration-close.sh").read_text(encoding="utf-8")
    start = src.index("do_verify() {")
    gate = src.index("closure-evidence-gate.py", start)
    write = src.index('update_cmd=("bash" "$SCRIPT_DIR/aspirations-update-goal.sh"', start)
    assert start < gate < write, "the gate must run inside do_verify, before the status write"
    block = src[src.rindex("\n    if ", start, gate):gate]
    assert "BODY_ROLE" not in block, "the worker and the reducer must both pass through it"
    assert "if [[ $_ceg_rc -eq 3 ]]; then" in src, "refuse on the dedicated code only (guard-5430)"
    assert "--override-closure-evidence)" in src
    assert '--override-closure-evidence \\"$OVERRIDE_CLOSURE_EVIDENCE\\"' in src
