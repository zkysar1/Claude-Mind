"""Pins the closure-evidence gate () and its session-scratch advisory ().

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

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import _body_stamp  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402
from gates import intake_route, origin_signal  # noqa: E402
from gates.closure_evidence import (  # noqa: E402
    advisory_text, classify, deferred_carriers, evaluate, parse_rows, refusal_text, remedy_lines,
    scratch_citations, timestamps)


def _load_script(name: str):
    """A hyphenated script, loaded for its functions."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_")[:-3], CORE_SCRIPTS / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GFA = _load_script("goal-field-append.py")  # compose() and sentinel_for(): the append's own shape

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


# ─── per-row carriers and the stored-note fix () ─────────────────
#
# Measured 2026-10-08 on a worker Body. Its stored note had a NOT MET row with no
# carrier; the note writer never overwrites, and the refusal's only fix was a
# hand-rebuilt 3,021-character note. Its outcome 3 also deferred to a goal that
# was already completed, which the residual-work gate let through once any other
# row named a live goal. The fixtures are synthetic, in the same shape.

G4 = {"id": "g-9-22", "priority": "HIGH", "category": "infra", "verification": {"outcomes": [
    "the unit builds", "the probe answers", "the host is reprovisioned", "the soak runs a day"]}}
ROWS_1_2 = ("OUTCOME 1: MET - build 42 green, sha 1a2b3c4d.\n"
            "OUTCOME 2: MET - probe answered 200 in 41 ms.\n")
STATUS = {"g-9-6": "completed", "g-9-30": "pending", "g-9-31": "in-progress",
          "g-9-40": "blocked", "g-9-99": None}


def _status(table=None):
    """carrier_status over `table`; an id it lacks raises, as an unreadable store does."""
    table = STATUS if table is None else table
    calls = []

    def look(gid):
        calls.append(gid)
        if gid not in table:
            raise LookupError("store unreadable")
        return table[gid]
    look.calls = calls
    return look


def test_each_not_met_row_needs_its_own_live_carrier():
    """Outcome 4's live carrier must not carry outcome 3."""
    note = (ROWS_1_2 + "OUTCOME 3: NOT MET - reprovision blocked; deferred to g-9-6\n"
            "OUTCOME 4: NOT MET - soak not run; deferred to g-9-30")
    r = _eval(note, goal=G4, carrier_status=_status())
    assert r["decision"] == "block"
    assert r["problems"] == ["OUTCOME 3 (NOT MET): it defers to g-9-6 (completed), and none of them "
                             "is live (pending or in-progress), so nothing owns this gap. Name a "
                             "live goal that does"]
    assert r["fix"] == [{"n": 3, "status": "NOT MET", "needs_carrier": True}]
    # Controls: a live carrier on outcome 3 passes, and a caller that passes no
    # lookup (the pure path) looks nothing up.
    assert _eval(note.replace("g-9-6", "g-9-31"), goal=G4,
                 carrier_status=_status())["decision"] == "pass"
    assert _eval(note, goal=G4)["decision"] == "pass"


def test_a_carrier_that_is_missing_blocked_or_the_goal_itself_is_not_live():
    base = (ROWS_1_2 + "OUTCOME 3: NOT MET - reprovision blocked; deferred to {}\n"
            "OUTCOME 4: NOT MET - soak not run; deferred to g-9-30")
    for carrier, said in (("g-9-99", "g-9-99 (not found)"), ("g-9-40", "g-9-40 (blocked)"),
                          ("g-9-22", "g-9-22 (this goal)")):
        look = _status()
        r = _eval(base.format(carrier), goal=G4, carrier_status=look)
        assert r["decision"] == "block", carrier
        assert f"it defers to {said}, and none of them is live" in r["problems"][0]
    assert "g-9-22" not in look.calls  # the goal itself is never looked up
    # Nor when its id is stored in another case: the row's ids come back lowered.
    look = _status({**STATUS, "g-9-22": "in-progress"})
    r = _eval(base.format("g-9-22"), goal={**G4, "id": "G-9-22"}, carrier_status=look)
    assert r["decision"] == "block" and "g-9-22" not in look.calls


def test_any_live_carrier_on_the_row_counts_and_a_lettered_child_is_looked_up_as_itself():
    """guard-2414: a pattern that drops "-b" looks up the parent, here a completed goal."""
    look = _status({**STATUS, "g-9-6-b": "pending"})
    note = (ROWS_1_2 + "OUTCOME 3: NOT MET - x; deferred to g-9-6 and g-9-30\n"
            "OUTCOME 4: NOT MET - y; deferred to g-9-6-b")
    assert _eval(note, goal=G4, carrier_status=look)["decision"] == "pass"
    assert "g-9-6-b" in look.calls
    assert deferred_carriers("NOT MET - y; Deferred to G-9-6-b, then g-9-7") == ["g-9-6-b", "g-9-7"]


def test_a_carrier_lookup_that_cannot_run_warns_and_never_refuses():
    note = (ROWS_1_2 + "OUTCOME 3: NOT MET - x; deferred to g-9-7\n"
            "OUTCOME 4: NOT MET - y; deferred to g-9-30")
    r = _eval(note, goal=G4, carrier_status=_status())  # g-9-7 is not in the table: it raises
    assert r["decision"] == "pass"
    assert r["warnings"] == ["OUTCOME 3: carrier lookup failed, so its liveness is unchecked: "
                             "g-9-7 (store unreadable)"]
    # Control: the same row, with a lookup that answers, is refused.
    assert _eval(note, goal=G4, carrier_status=_status({**STATUS, "g-9-7": "completed"})
                 )["decision"] == "block"


def test_an_appended_corrected_row_replaces_the_stored_one():
    stored = (ROWS_1_2 + "OUTCOME 3: NOT MET - x; deferred to g-9-30\n"
              "OUTCOME 4: NOT MET - soak not run.\n\nThe narrative the closer wrote.")
    assert _eval(stored, goal=G4, carrier_status=_status())["decision"] == "block"
    fixed = GFA.compose(stored, "OUTCOME 4 (corrected): NOT MET - soak not run; deferred to "
                                "g-9-31", "closure-fix-1")
    r = _eval(fixed, goal=G4, carrier_status=_status())
    assert r["decision"] == "pass", r["problems"]
    assert [(e["n"], e["corrected"]) for e in parse_rows(fixed)["superseded"]] == [(4, False)]
    # Control: the same append without "(corrected)" is a second row, refused as one.
    plain = GFA.compose(stored, "OUTCOME 4: NOT MET - soak not run; deferred to g-9-31",
                        "closure-fix-1")
    assert _eval(plain, goal=G4, carrier_status=_status())["problems"] == [
        "OUTCOME 4 has 2 rows. Write one"]


def test_a_corrected_row_counts_only_in_its_header_and_only_when_it_comes_last():
    fixed = "OUTCOME 1: PASS - done\n\nOUTCOME 1 (corrected): MET - 42 lines written."
    assert _eval(fixed, goal=GOAL1)["decision"] == "pass"  # it replaces a malformed header too
    assert "must be MET or NOT MET" in _eval("OUTCOME 1: PASS - done", goal=GOAL1)["problems"][0]
    not_last = "OUTCOME 1 (corrected): MET - 42 lines written.\n\nOUTCOME 1: MET - 43 lines written."
    in_text = "OUTCOME 1: MET - 42 lines.\n\nOUTCOME 1: MET - 43 lines (corrected count)."
    for note in (not_last, in_text):
        assert _eval(note, goal=GOAL1)["problems"] == ["OUTCOME 1 has 2 rows. Write one"], note


def test_an_append_sentinel_or_a_body_signature_ends_a_row_and_lends_it_no_evidence(monkeypatch):
    """Both follow a row with no blank line between, and both carry digits that
    would pass a MET row's evidence check: a marker, and a sid and host."""
    monkeypatch.setenv("BODY_ROLE", "worker")
    monkeypatch.setenv("MIND_AGENT", "alpha")
    monkeypatch.setenv("MIND_SID", "5af0e4c17d")
    tails = (GFA.sentinel_for("closure-fix-1"), _body_stamp.stamp_line())
    assert tails[1] and "5af0e4c1" in tails[1]  # the writers' own line, digits included
    for tail in tails:
        assert _eval(f"OUTCOME 1: MET - the node is written.\n{tail}", goal=GOAL1)["problems"] == [
            "OUTCOME 1 (MET): MET with no measured value. Cite the value and its source (an "
            "output excerpt, a path, a sha, timestamps)"], tail
        # Control: a measured value passes with either line below it.
        assert _eval(f"OUTCOME 1: MET - the node is written, 42 lines.\n{tail}",
                     goal=GOAL1)["decision"] == "pass", tail


STORED_ZC = (ROWS_1_2 + "OUTCOME 3: NOT MET - reprovision blocked; deferred to g-9-6\n"
             "OUTCOME 4: NOT MET - soak not run.\n\n" + "The narrative the closer wrote. " * 90)


def test_the_refusal_hands_over_the_carrier_filing_and_the_one_append_that_fixes_the_note():
    r = _eval(STORED_ZC, goal=G4, carrier_status=_status())
    assert [(f["n"], f["status"], f["needs_carrier"]) for f in r["fix"]] == [
        (3, "NOT MET", True), (4, "NOT MET", True)]
    lines = remedy_lines("g-9-22", r, goal=G4, note=STORED_ZC, stored=True, source="world")
    text = refusal_text("g-9-22", r, "the record's outcome_note", remedy=lines, stored=True)
    assert len(text.encode("utf-8")) <= 2048, len(text.encode("utf-8"))
    # The filing: its body is JSON that intake files as pending, so the carrier is live.
    i = lines.index("bash core/scripts/aspirations-add-goal.sh --source world asp-9 <<'GOAL'")
    body = json.loads(lines[i + 1])
    assert lines[i + 2] == "GOAL"
    assert (body["priority"], body["category"], body["participants"]) == ("HIGH", "infra", ["agent"])
    assert origin_signal.is_valid(body["origin_signal"])
    assert intake_route.route_intake(body, config=intake_route.load_config(PROJECT_ROOT),
                                     user_context=False) == "pending"
    # The append: one corrected row per failing outcome, then the closing word.
    j = lines.index("bash core/scripts/goal-field-append.sh --source world g-9-22 outcome_note "
                    "closure-fix-1 --value-stdin <<'ROWS'")
    assert lines[j + 1:j + 4] == [
        "OUTCOME 3 (corrected): NOT MET - <what is missing>; deferred to <live goal-id>",
        "OUTCOME 4 (corrected): NOT MET - <what is missing>; deferred to <live goal-id>", "ROWS"]
    # Replayed: the closer fills the rows in and appends them, and the same close passes.
    rows = "\n".join(lines[j + 1:j + 3]).replace("<what is missing>", "not run").replace(
        "<live goal-id>", "g-9-30")
    assert _eval(GFA.compose(STORED_ZC, rows, "closure-fix-1"), goal=G4,
                 carrier_status=_status())["decision"] == "pass"
    # The rows stand in for the format; the full rewrite stays on offer.
    assert "Format:" not in text and "as it was. Or rewrite the whole note" in text


def test_the_printed_commands_run_as_shell(tmp_path):
    """Each command with its script swapped for cat: its stdin must be exactly the
    body or the rows. A closing word off column 0 would swallow the rest instead."""
    r = _eval(STORED_ZC, goal=G4, carrier_status=_status())
    lines = remedy_lines("g-9-22", r, goal=G4, note=STORED_ZC, stored=True)
    for head, end in (("bash core/scripts/aspirations-add-goal.sh", "GOAL"),
                      ("bash core/scripts/goal-field-append.sh", "ROWS")):
        i = next(n for n, ln in enumerate(lines) if ln.startswith(head))
        k = lines.index(end, i)
        script = tmp_path / f"{end}.sh"
        script.write_text("\n".join(["cat" + lines[i][lines[i].index(" <<"):]] + lines[i + 1:k + 1]
                                    + ["echo after"]) + "\n", encoding="utf-8")
        out = subprocess.run(bash_cmd(script), capture_output=True, text=True, timeout=60).stdout
        assert out == "\n".join(lines[i + 1:k]) + "\nafter\n", out


def test_a_stored_note_with_no_table_keeps_the_format_with_both_forms():
    """The append rows carry the format only for outcomes whose status is known.
    With no table they show the MET form alone, so the format stays beside them."""
    note = "Done. The narrative only."
    r = _eval(note, goal=G4)
    lines = remedy_lines("g-9-22", r, goal=G4, note=note, stored=True)
    text = refusal_text("g-9-22", r, "the record's outcome_note", remedy=lines, stored=True)
    assert "Format:" in text and "NOT MET - <what is missing>; deferred to <goal-id>" in text
    assert "OUTCOME 4: MET - <measured value>" in text
    assert not [ln for ln in lines if ln.startswith("OUTCOME") and "(corrected)" in ln]
    assert len(text.encode("utf-8")) <= 2048


def test_a_note_that_is_not_stored_gets_the_format_and_each_fix_a_new_marker():
    note = "OUTCOME 1: MET - the node is written."
    r = _eval(note, goal=GOAL1)
    assert remedy_lines("g-1-1", r, goal=GOAL1, note=note, stored=False) == []
    text = refusal_text("g-1-1", r, "the close's --summary text", remedy=[], stored=False)
    assert "Format:" in text and "goal-field-append" not in text
    # A stored note that already holds closure-fix-1 gets closure-fix-2: the marker
    # is the append's idempotency key, so a repeat would store nothing.
    again = GFA.compose(note, "OUTCOME 1 (corrected): MET - the node is written.", "closure-fix-1")
    lines = remedy_lines("g-1-1", _eval(again, goal=GOAL1), goal=GOAL1, note=again, stored=True)
    assert lines[1].endswith(" outcome_note closure-fix-2 --value-stdin <<'ROWS'"), lines
    assert lines[2] == ("OUTCOME 1 (corrected): MET - <measured value>. Source: <command + "
                        "output | path | sha>")


# ─── the session-scratch advisory () ─────────────────────────────
#
# Measured 2026-09-27: 14 of 39 close reviews flagged evidence that only the
# closer's box could open. The positive cases cite a per-session path with
# nothing a reviewer elsewhere can check; each quiet case is the same paragraph
# with the missing part supplied, so a predicate that never fires fails here.

GATED = "OUTCOME 1: MET — 42 lines written."
LINES = "`TOTAL: 5112 passed, 0 failed`"


def _cite(note, host=""):
    return scratch_citations(note, sessions_dirname="sessions", hostname=host)


def test_a_paragraph_citing_session_scratch_without_its_lines_or_a_host_fires():
    found = _cite(f"{GATED}\n\nThe suite log is kept at {SCRATCH}.")
    assert found == [{"paragraph": 2, "paths": [SCRATCH], "missing": ["excerpt", "host"]}]


def test_the_verdict_line_and_the_host_beside_the_path_quiet_it():
    """The goal's negative control."""
    note = (f"{GATED}\n\nFull suite on hostname box-a: `VERDICT: CLEAN  TOTAL: 5112 passed, "
            f"0 failed` from `bash core/scripts/run-full-suite.sh`, log kept at {SCRATCH}.")
    assert _cite(note) == []


def test_each_leg_fires_alone_and_a_bare_name_is_not_an_excerpt():
    assert [f["missing"] for f in _cite(f"{GATED}\n\n{LINES} (log {SCRATCH}).")] == [["host"]]
    host_only = f"{GATED}\n\nOn hostname box-a the log is {SCRATCH}."
    assert [f["missing"] for f in _cite(host_only)] == [["excerpt"]]
    a_name = f"{GATED}\n\nOn hostname box-a, `run_full_suite_v2` wrote {SCRATCH}."
    assert [f["missing"] for f in _cite(a_name)] == [["excerpt"]]
    # The lines must sit beside the path they back, not in another paragraph.
    apart = f"{GATED}\n\n{LINES} on hostname box-a.\n\nLog: {SCRATCH}."
    assert [(f["paragraph"], f["missing"]) for f in _cite(apart)] == [(3, ["excerpt"])]


def test_this_box_hostname_names_the_host_as_a_whole_word():
    note = f"{GATED}\n\nMeasured on box-q7: {LINES}, log {SCRATCH}."
    assert [f["missing"] for f in _cite(note)] == [["host"]]
    assert _cite(note, host="box-q7") == []
    assert [f["missing"] for f in _cite(note, host="box-q")] == [["host"]]


def test_a_host_flag_in_a_command_names_no_host():
    flag = f"{GATED}\n\nRan `srv --host=127.0.0.1 --port 80`, log {SCRATCH}."
    assert [f["missing"] for f in _cite(flag)] == [["host"]]
    assert _cite(flag + " Measured on host=box-a.") == []  # control: a host label does


def test_a_crlf_note_splits_into_the_same_paragraphs():
    """A note written on Windows, or a CRLF summary on stdin, must not read as
    one paragraph: the lines in paragraph 2 would then excuse the path in 3."""
    apart = f"{GATED}\n\n{LINES} on hostname box-a.\n\nLog: {SCRATCH}."
    lf = _cite(apart)
    assert [(f["paragraph"], f["missing"]) for f in lf] == [(3, ["excerpt"])]
    assert _cite(apart.replace("\n", "\r\n")) == lf


def test_only_a_path_into_a_real_session_dir_fires():
    fires = (SCRATCH, "agents/charlie/sessions/57c55134-e5b5-429c-bfa2-dbd142b9574d/cycle/run.log",
             r"C:\mind\agents\charlie\sessions\57c55134e5b5429cbfa2dbd142b9574d\scratch\run.log")
    for path in fires:
        assert _cite(f"{GATED}\n\nSee {path} for details."), path
    quiet = ("agents/<agent>/sessions/<SID>/scratch/suite.log",   # the convention, described
             "agents/charlie/session/handoff.yaml",               # the synced cross-session dir
             "src/app/sessions/handlers/login.ts",                # a product repo
             "agents/charlie/sessions/57c55134e5b5429cbfa2dbd142b9574d/")  # the dir, not evidence
    for path in quiet:
        assert _cite(f"{GATED}\n\nSee {path} for details.") == [], path


def test_the_advisory_text_names_the_paths_what_is_missing_and_the_fix():
    text = advisory_text("g-1-1", _cite(f"{GATED}\n\nThe suite log is kept at {SCRATCH}."), "box-a")
    assert "ADVISORY (g-375-52, never refuses)" in text and SCRATCH in text
    assert "no lines inline beside it" in text and "The note names no host." in text
    assert '"hostname box-a"' in text and "guard-7485" in text
    assert len(text.encode("utf-8")) <= 2048


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


def test_cli_advisory_rides_stderr_and_never_changes_the_verdict(tmp_path):
    """. _cli asserts ONE JSON line on stdout, so an advisory printed
    there fails every case below."""
    rc, doc, err, _ = _cli(tmp_path, {**GOAL1, "outcome_note": f"{GATED}\n\nLog: {SCRATCH}."})
    assert (rc, doc["decision"]) == (0, "pass")
    assert "ADVISORY (g-375-52" in err and SCRATCH in err
    cite = doc["scratch_citations"]
    assert [c["paths"] for c in cite] == [[SCRATCH]] and "excerpt" in cite[0]["missing"]
    # Control: the same close with the lines and the host beside the path is quiet.
    quiet = f"{GATED}\n\nOn hostname box-a: {LINES}, log {SCRATCH}."
    rc, doc, err, _ = _cli(tmp_path, {**GOAL1, "outcome_note": quiet})
    assert (rc, doc["decision"]) == (0, "pass")
    assert "ADVISORY" not in err and "scratch_citations" not in doc
    # A refused close stays refused, and the refusal keeps the first screen.
    rc, doc, err, _ = _cli(tmp_path, {**GOAL1, "outcome_note": f"done\n\nLog: {SCRATCH}."})
    assert (rc, doc["decision"]) == (3, "block")
    assert err.index("REFUSED") < err.index("ADVISORY (g-375-52")


def test_a_fault_in_the_advisory_cannot_cost_the_verdict_and_never_reads_as_quiet(
        tmp_path, monkeypatch, capsys):
    """A raise there would exit 1, which do_verify reads as a gate fault and
    proceeds past, so a refusal would be lost with it (guard-5430). And the JSON
    line must tell a skipped check from a quiet one (guard-2421)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("closure_evidence_gate_cli", GATE)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    cited = f"{GATED}\n\nLog: {SCRATCH}."
    found, text, error = cli.scratch_advisory("g-1-1", cited)
    assert found and "ADVISORY (g-375-52" in text and error == ""  # control: healthy

    def boom(*a, **k):
        raise RuntimeError("bad pattern")
    monkeypatch.setattr(cli, "scratch_citations", boom)
    assert cli.scratch_advisory("g-1-1", cited) == (
        [], "closure-evidence-gate: session-scratch advisory skipped (bad pattern)", "bad pattern")
    # Through main(), in-process; the gate-firing ledger is stubbed so no store is written.
    monkeypatch.setattr(cli, "_gate_log", lambda *a, **k: None)
    gj = tmp_path / "goal.json"
    gj.write_text(json.dumps({**GOAL1, "outcome_note": cited}), encoding="utf-8")
    assert cli.main(["--goal", "g-1-1", "--goal-json", str(gj)]) == 0
    out = capsys.readouterr()
    doc = json.loads(out.out.strip().splitlines()[-1])
    assert doc["decision"] == "pass" and doc["scratch_advisory_error"] == "bad pattern"
    assert "scratch_citations" not in doc and "advisory skipped (bad pattern)" in out.err


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


def test_cli_checks_each_carrier_and_hands_over_the_fix(tmp_path):
    """, through the real script with the --carriers-json seam."""
    goal = {**G4, "outcome_note": ROWS_1_2 + "OUTCOME 3: NOT MET - x; deferred to g-9-6\n"
                                             "OUTCOME 4: NOT MET - y; deferred to g-9-30"}
    carriers = tmp_path / "carriers.json"
    carriers.write_text(json.dumps(STATUS), encoding="utf-8")
    rc, doc, err, _ = _cli(tmp_path, goal, "--carriers-json", str(carriers))
    assert (rc, doc["decision"]) == (3, "block")
    assert "g-9-6 (completed)" in doc["problems"][0]
    assert "bash core/scripts/aspirations-add-goal.sh --source world asp-9 <<'GOAL'" in doc["remedy"]
    assert ("bash core/scripts/goal-field-append.sh --source world g-9-22 outcome_note "
            "closure-fix-1 --value-stdin <<'ROWS'") in doc["remedy"]
    assert "\nROWS\n" in err and err.index("REFUSED") < err.index("aspirations-add-goal.sh")
    # Control: with the carrier live, the same close passes and carries no remedy.
    carriers.write_text(json.dumps({**STATUS, "g-9-6": "pending"}), encoding="utf-8")
    rc, doc, _, _ = _cli(tmp_path, goal, "--carriers-json", str(carriers))
    assert (rc, doc["decision"]) == (0, "pass") and "remedy" not in doc
    # A carrier the lookup cannot answer is a warning, never a refusal.
    carriers.write_text(json.dumps({"g-9-30": "pending"}), encoding="utf-8")
    rc, doc, _, _ = _cli(tmp_path, goal, "--carriers-json", str(carriers))
    assert (rc, doc["decision"]) == (0, "pass")
    assert doc["warnings"] == ["OUTCOME 3: carrier lookup failed, so its liveness is unchecked: "
                               "g-9-6 (g-9-6 is not in --carriers-json)"]


def test_cli_store_lookup_counts_a_live_match_in_either_queue(monkeypatch):
    """aspirations-query is union-only, so one id can come back twice. No store is
    read: _run_json is replaced, and a --goal-json run builds no lookup at all."""
    cli = _load_script("closure-evidence-gate.py")
    answers = {"g-9-6": [{"status": "completed"}, {"status": "pending"}], "g-9-7": [],
               "g-9-8": None}
    monkeypatch.setattr(cli, "_run_json", lambda script, *a: answers[a[2]])
    look = cli.carrier_lookup(None, None)
    assert (look("g-9-6"), look("g-9-7")) == ("pending", None)
    with pytest.raises(LookupError):
        look("g-9-8")  # the query failed: unknown, which the check turns into a warning
    assert cli.carrier_lookup(None, "goal.json") is None


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
    # stdout goes to the log; stderr must reach the closer, because the 
    # advisory rides it.
    call = src[gate:src.index("|| _ceg_rc=$?", gate)]
    assert ">>" in call and "2>" not in call and "&>" not in call, call
