"""test_completed_not_closed_slate.py —  drain slate.

Pins the pure filter/rank/bound (`build_slate`) and the two report contracts a
bounded lane must keep: the population is reported BESIDE the batch (guard-3830)
and rows the age gate holds back are counted, not vanished. Also pins that the
wiring exists — a correct slate nobody calls is the g-306-227 shape.
"""
from __future__ import annotations

import importlib.util
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "core" / "scripts" / "completed-not-closed-slate.py"
PRECHECK = ROOT / ".claude" / "skills" / "aspirations-precheck" / "SKILL.md"
METER = ROOT / "core" / "scripts" / "aspirations-precheck-budget-meter.sh"
WORKER_LOOP = ROOT / ".claude" / "skills" / "worker-loop" / "SKILL.md"


def _load():
    spec = importlib.util.spec_from_file_location("cnc_slate", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


NOW = datetime(2026, 8, 16, 12, 0, 0)


def _row(gid, *, agent="alpha", age_h=100.0, note="DONE. measured 3/3.",
         status="in-progress", sid="aaaaaaaa-1", asp="asp-115", source="world"):
    return {
        "goal_id": gid, "id": gid, "asp_id": asp, "source": source,
        "status": status, "claimed_by": agent, "claimed_by_sid": sid,
        "claimed_at": (NOW - timedelta(hours=age_h)).strftime("%Y-%m-%dT%H:%M:%S"),
        "outcome_note": note, "title": f"title {gid}",
    }


def test_slate_is_oldest_first_bounded_and_reports_dropped():
    m = _load()
    rows = [_row("g-1-01", age_h=10), _row("g-1-02", age_h=200),
            _row("g-1-03", age_h=50), _row("g-1-04", age_h=150)]
    out = m.build_slate(rows, "alpha", limit=2, min_age_hours=6, now=NOW)
    assert [r["goal_id"] for r in out["slate"]] == ["g-1-02", "g-1-04"]
    assert out["dropped"] == 2
    assert out["population"]["mine_noted_in_progress"] == 4
    assert out["population"]["mine_eligible"] == 4


def test_population_counts_fleet_and_holder_separately():
    m = _load()
    rows = [_row("g-1-01"), _row("g-1-02", agent="echo"),
            _row("g-1-03", note=""), _row("g-1-04", status="pending", age_h=80)]
    out = m.build_slate(rows, "alpha", limit=5, min_age_hours=6, now=NOW)
    pop = out["population"]
    # widened predicate (2026-08-16): a PENDING row that still carries a claim
    # and a note is population too. The strict in-progress halves are kept
    # beside the widened totals so old consumers keep their meaning.
    assert pop["fleet_noted"] == 3
    assert pop["fleet_noted_in_progress"] == 2   # alpha + echo, noted, in-progress
    assert pop["fleet_noted_pending"] == 1
    assert pop["mine_noted"] == 2
    assert pop["mine_noted_in_progress"] == 1
    assert pop["mine_noted_pending"] == 1
    assert [r["goal_id"] for r in out["slate"]] == ["g-1-01", "g-1-04"]
    assert out["slate"][1]["status"] == "pending"
    assert out["slate"][1]["holder_via"] == "claim"


# ── 2026-08-16 predicate widening (zeta msg-20260816-195023-zeta-5111) ────────
# The first cut's population predicate (in-progress AND claimed_by == me) was
# NARROWER than the path that creates the population: a worker's unit ends at
# `pending` as often as `in-progress`, and a released row keeps its note but
# loses its holder — so the lane reported ~clean while 220 pending goals
# carried an outcome_note (guard-1802: predicate narrower than the creating gate).

def _released(gid, *, executed_by="alpha", age_h=100.0, note="DONE. measured 3/3.",
              defer=None, recurring=False, asp="asp-115"):
    """A pending row whose claim was released: no claimed_by/claimed_at, but
    executed_by (stamped at claim time, E1) and outcome_note survive."""
    return {
        "goal_id": gid, "id": gid, "asp_id": asp, "source": "world",
        "status": "pending", "claimed_by": None, "claimed_by_sid": None,
        "claimed_at": None, "executed_by": executed_by,
        "last_modified": (NOW - timedelta(hours=age_h)).strftime("%Y-%m-%dT%H:%M:%S"),
        "outcome_note": note, "title": f"title {gid}",
        "defer_reason": defer, "recurring": recurring,
    }


def test_predicate_is_the_ssot_and_covers_the_creating_path():
    m = _load()
    assert m.is_drain_candidate(_row("g-3-01"))                                # in-progress + claim
    assert m.is_drain_candidate(_row("g-3-02", status="pending"))              # pending + claim
    assert m.is_drain_candidate(_released("g-3-03"))                           # pending, released
    assert not m.is_drain_candidate(_row("g-3-04", note="   "))                # no evidence
    assert not m.is_drain_candidate(_row("g-3-05", status="completed"))        # closed
    assert not m.is_drain_candidate(_released("g-3-06", defer="precondition_unmet: window"))
    assert not m.is_drain_candidate(_released("g-3-07", recurring=True))
    assert m.holder_of(_row("g-3-08", agent="echo")) == "echo"
    assert m.holder_of(_released("g-3-09", executed_by="bravo")) == "bravo"
    assert m.holder_of(_released("g-3-10", executed_by=None)) == "(unattributed)"


def test_released_pending_rows_reach_their_executor_and_peer_legs():
    m = _load()
    rows = [
        _released("g-4-01"),                                  # mine via executed_by
        _released("g-4-02", executed_by="bravo"),             # bravo's, unclaimed
        _released("g-4-03", defer="precondition_unmet: x"),   # parked: defer lane owns it
        _released("g-4-04", recurring=True),                  # a note between cycles is normal
        _released("g-4-05", executed_by=None),                # nobody to offer it to
        _row("g-4-06", age_h=150),                            # mine via claim, oldest
    ]
    out = m.build_slate(rows, "alpha", limit=5, min_age_hours=6, now=NOW)
    pop = out["population"]
    assert pop["fleet_noted"] == 4                 # 01, 02, 05, 06
    assert pop["fleet_noted_pending"] == 3
    assert pop["mine_noted"] == 2 and pop["mine_noted_pending"] == 1
    assert [r["goal_id"] for r in out["slate"]] == ["g-4-06", "g-4-01"]
    assert out["slate"][1]["holder_via"] == "executed_by"
    assert out["slate"][1]["status"] == "pending"
    bh = pop["by_holder"]
    assert bh["bravo"] == {"noted": 1, "oldest_claim_age_h": 100.0, "unclaimed": 1}
    assert bh["(unattributed)"]["noted"] == 1
    assert bh["alpha"]["unclaimed"] == 1
    # the released row must NOT be attributed to the agent that merely reads it
    out_b = m.build_slate(rows, "bravo", limit=5, min_age_hours=6, now=NOW)
    assert [r["goal_id"] for r in out_b["slate"]] == ["g-4-02"]


def test_released_pending_row_still_honours_the_age_gate_via_last_modified():
    """A released row has no claimed_at; the age gate falls back to
    last_modified so a just-released unit is still held back (Body mid-close),
    not offered as backlog."""
    m = _load()
    rows = [_released("g-5-01", age_h=1.0), _released("g-5-02", age_h=9.0)]
    out = m.build_slate(rows, "alpha", limit=5, min_age_hours=6, now=NOW)
    assert [r["goal_id"] for r in out["slate"]] == ["g-5-02"]
    assert out["population"]["mine_held_back_fresh"] == 1


def test_loader_queries_both_open_statuses():
    """The row loader must ask for BOTH halves; a widened predicate over a
    single-status query is the same blind spot with better documentation."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert 'DRAIN_STATUSES = ("in-progress", "pending")' in src
    assert "for status in DRAIN_STATUSES:" in src


def test_age_gate_holds_fresh_rows_back_and_counts_them():
    """A fresh noted-but-open row is a Body mid-close (worker-loop Phase 4a), not
    backlog. It must be COUNTED as held back — a silent drop would let a zero
    slate read as a drained backlog (guard-3830)."""
    m = _load()
    rows = [_row("g-1-01", age_h=1.0), _row("g-1-02", age_h=7.0)]
    out = m.build_slate(rows, "alpha", limit=5, min_age_hours=6, now=NOW)
    assert [r["goal_id"] for r in out["slate"]] == ["g-1-02"]
    assert out["population"]["mine_held_back_fresh"] == 1
    assert out["population"]["mine_noted_in_progress"] == 2


def test_own_sid_is_excluded():
    """The reducer's own in-flight goal shares claimed_by with the backlog; it is
    excluded by SID so a reducer never triages the goal it is executing."""
    m = _load()
    rows = [_row("g-1-01", sid="me-sid"), _row("g-1-02", sid="other")]
    out = m.build_slate(rows, "alpha", limit=5, min_age_hours=6, now=NOW,
                        own_sid="me-sid")
    assert [r["goal_id"] for r in out["slate"]] == ["g-1-02"]
    assert out["population"]["mine_held_back_own_sid"] == 1


def test_note_head_is_first_nonempty_line_and_cross_record_ids_exclude_self():
    m = _load()
    note = "\n\n  DIAGNOSIS COMPLETE, FIX NOT DONE — folded into g-115-5583.\nmore g-115-6230 g-115-5583"
    assert m.note_head(note).startswith("DIAGNOSIS COMPLETE, FIX NOT DONE")
    assert m.cross_record_ids(note, "g-115-6230") == ["g-115-5583"]
    long = "x" * 500
    assert len(m.note_head(long)) <= 240


def test_unknown_age_rows_sort_last_not_first():
    m = _load()
    r_unknown = _row("g-1-09")
    r_unknown["claimed_at"] = None
    r_unknown["last_modified"] = None
    rows = [r_unknown, _row("g-1-02", age_h=30)]
    out = m.build_slate(rows, "alpha", limit=5, min_age_hours=6, now=NOW)
    assert [r["goal_id"] for r in out["slate"]] == ["g-1-02", "g-1-09"]
    assert out["slate"][1]["claim_age_h"] is None


def test_wiring_precheck_calls_the_slate_and_meter_registers_it_always_run():
    src = PRECHECK.read_text(encoding="utf-8")
    assert "completed-not-closed-slate.sh" in src, (
        "precheck no longer calls the drain slate — the report lane then has no "
        "consumer again (the g-115-6337 defect)")
    assert "## Phase 0.5g.7" in src
    assert re.search(r"\|\s*0\.5g\.7\s*\|\s*completed-not-closed-drain\s*\|\s*always-run\s*\|", src), (
        "tier table row for completed-not-closed-drain must be always-run")
    meter = METER.read_text(encoding="utf-8")
    # MEMBERSHIP, NOT POSITION. This asserted `completed-not-closed-drain\)` --
    # i.e. that the sweep was the LAST alternative before the case arm's closing
    # paren. That is a positional predicate wearing a membership predicate's
    # error message, and it fired a FALSE failure the first time a correct change
    # appended a new always-run sweep after it ( / world-script-crlf-check).
    # Same brittle-enumeration class as the hardcoded lane count in
    # test_precheck_always_run_battery.py. Parse the arm and test membership, so
    # the assertion tracks its own stated intent and survives the next append.
    arm = next((ln for ln in meter.splitlines()
                if not ln.lstrip().startswith("#") and "tree-debt-gate|" in ln), None)
    assert arm is not None, "could not find sweep_tier()'s always-run case arm"
    members = {a.strip() for a in arm.strip().rstrip(")").split("|")}
    assert "completed-not-closed-drain" in members, (
        "completed-not-closed-drain must be registered in sweep_tier() (always-run); "
        f"arm members: {sorted(members)}")


def test_slate_has_no_apply_flag():
    """The slate is report-only by contract; the closer is the canonical writer."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert '"--apply"' not in src and "'--apply'" not in src


def test_worker_loop_closes_its_own_unit_so_steady_state_inflow_is_zero():
    """The drain is the backstop; the primary fix is that a worker records the
    status it judged at end of unit (Phase 4a). Both halves are one change."""
    src = WORKER_LOOP.read_text(encoding="utf-8")
    assert "iteration-close.sh --phase verify" in src
    assert "wm-append.sh goals_completed_this_session" in src


# ── 2026-08-16 fresh-eyes review additions: hold ledger + compact reader ──────

def test_recent_hold_holds_row_back_and_counts_it_then_expires():
    """A HOLD that wrote nothing was re-served every iteration; with the slate
    oldest-first and bounded, three permanent holds starved every row behind
    them. A hold is a LEASE (guard-3419): held back for hold_ttl_hours, counted,
    then resurfacing with its count — never a permanent exclusion."""
    m = _load()
    rows = [_row("g-1-01", age_h=200), _row("g-1-02", age_h=150), _row("g-1-03", age_h=100)]
    fresh_hold = {"goal_id": "g-1-01", "held_at": (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),
                  "reason": "STRANDED per Phase 3.7"}
    out = m.build_slate(rows, "alpha", limit=2, min_age_hours=6, now=NOW,
                        holds=[fresh_hold], hold_ttl_hours=24)
    assert [r["goal_id"] for r in out["slate"]] == ["g-1-02", "g-1-03"], \
        "a fresh hold must let the rows BEHIND the held one reach the slate"
    assert out["population"]["mine_held_back_recent_hold"] == 1
    # expiry: the same hold 30h old is past the 24h TTL -> the row resurfaces
    old_hold = dict(fresh_hold, held_at=(NOW - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S"))
    out2 = m.build_slate(rows, "alpha", limit=2, min_age_hours=6, now=NOW,
                         holds=[old_hold], hold_ttl_hours=24)
    assert [r["goal_id"] for r in out2["slate"]] == ["g-1-01", "g-1-02"]
    assert out2["population"]["mine_held_back_recent_hold"] == 0
    assert out2["slate"][0]["hold_count"] == 1
    assert out2["slate"][0]["last_hold_reason"].startswith("STRANDED")


def test_hold_ledger_roundtrip_and_retention(tmp_path):
    m = _load()
    ledger = tmp_path / "cnc-drain-holds.jsonl"
    m.record_hold(ledger, goal_id="g-9-01", reason="ambiguous", agent="alpha", sid="abc", now=NOW)
    m.record_hold(ledger, goal_id="g-9-01", reason="still ambiguous", agent="alpha", sid="abc",
                  now=NOW + timedelta(hours=25))
    holds = m.load_holds(ledger)
    assert [h["goal_id"] for h in holds] == ["g-9-01", "g-9-01"]
    assert holds[-1]["reason"] == "still ambiguous"
    # retention: an entry older than the retention window is dropped on the next append
    stale = NOW + timedelta(days=40)
    m.record_hold(ledger, goal_id="g-9-02", reason="new", agent="alpha", sid="abc", now=stale)
    left = m.load_holds(ledger)
    assert [h["goal_id"] for h in left] == ["g-9-02"], "entries past retention must be pruned"
    # a missing ledger is an EMPTY hold set (fail-open: never hides rows)
    assert m.load_holds(tmp_path / "nope.jsonl") == []


def test_render_show_is_compact_and_paged():
    m = _load()
    g = _row("g-2-01", note="VERDICT LINE\n" + ("x" * 5000))
    g["verification"] = {"outcomes": ["o1", "o2"], "checks": ["c1"]}
    g["description"] = "d" * 2000
    text = m.render_show(g, note_from=0, note_chars=100)
    assert text.startswith("id=g-2-01 status=in-progress claimed_by=alpha")
    assert "verification.outcomes (2):" in text and "verification.checks (1):" in text
    assert "outcome_note: total 5013 chars; showing [0:100]" in text
    assert "re-run with --note-from 100" in text
    assert len(text) < 2500, "the reader must stay compact — a raw dump is what thrashed context"
    page2 = m.render_show(g, note_from=100, note_chars=100)
    assert "showing [100:200]" in page2


def test_cli_wires_show_and_hold_flags():
    """guard-3893: a flag the parser accepts is not a flag the script wires."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert '"--show"' in src and "render_show(" in src and "_load_one(" in src
    assert '"--hold"' in src and "record_hold(" in src and '"--reason"' in src
    assert "hold_ttl_hours" in src


def test_precheck_reads_through_show_and_records_holds():
    src = PRECHECK.read_text(encoding="utf-8")
    assert "completed-not-closed-slate.sh --show <goal-id>" in src, (
        "Phase 0.5g.7 must read records through the compact reader, not --full")
    assert "completed-not-closed-slate.sh --hold <goal-id> --reason" in src, (
        "Phase 0.5g.7 HOLD must be recorded or the bounded slate starves")
    l = src.find("## Phase 0.5g.7"); block = src[l:l + 9000]
    assert "aspirations-query.sh --goal-field id <goal-id> --full" not in block, (
        "the raw --full read must not be prescribed inside Phase 0.5g.7")


def test_population_carries_a_per_holder_breakdown_for_the_peer_leg():
    """"Completions across agents": a dormant/retired holder's finished work
    has no drainer — the slate must at least SHOW it so precheck 0.5g.7's peer
    leg can act (liveness-gated there, never here)."""
    m = _load()
    rows = [_row("g-1-01", age_h=10), _row("g-1-02", agent="echo", age_h=80),
            _row("g-1-03", agent="echo", age_h=30), _row("g-1-04", agent="zeta", note="")]
    out = m.build_slate(rows, "alpha", limit=5, min_age_hours=6, now=NOW)
    bh = out["population"]["by_holder"]
    assert bh["alpha"]["noted"] == 1 and bh["echo"]["noted"] == 2
    assert bh["echo"]["oldest_claim_age_h"] == 80.0
    assert "zeta" not in bh, "an un-noted in-progress row is not part of this population"


def test_precheck_peer_leg_is_liveness_gated():
    src = PRECHECK.read_text(encoding="utf-8")
    l = src.find("## Phase 0.5g.7"); block = src[l:l + 12000]
    assert "liveness-check.sh --agent <peer> --json" in block
    assert "completed-not-closed-slate.sh --agent <peer> --min-age-hours 48" in block
    assert "NEVER on `alive` or `unknown`" in block, (
        "the peer leg must act only on dormant/retired — draining an alive peer's "
        "units races its own Phase 4a; `unknown` is not evidence of absence")


# ── : the hold ledger is the ACTING agent's, not the queried holder's ──

def test_hold_ledger_is_keyed_by_acting_agent_not_queried_holder(
        tmp_path, monkeypatch, capsys):
    """The peer / "(unattributed)" lane READ the ledger under the QUERIED HOLDER
    while `--hold` WROTE under the ACTING agent, so a 24h hold suppressed nothing
    on every lane but the agent's own. Measured 2026-08-17 (zeta, cc-02): two
    rows held at 01:13 were re-served at 03:49 with `mine_held_back_recent_hold:
    0` and six records sitting in the ledger.

    It failed in the direction that MANUFACTURES work: the write half worked, so
    hold_count incremented ("hold #2") while suppression did nothing, and the
    protocol escalates to an Investigate on the third hold — an escalation driven
    entirely by a broken read.

    Pins the invariant that fixes it: main() resolves ONE ledger path from the
    ACTING agent, so read and write cannot diverge whatever arg shape the caller
    uses. Every pre-existing test drains the agent's OWN lane, which is the one
    lane where the two paths coincide — so none of them can fail on this.
    """
    m = _load()
    seen = []

    def _fake_holds_path(agent):
        seen.append(agent)
        return tmp_path / f"ledger-{agent}.jsonl"

    monkeypatch.setattr(m, "holds_path", _fake_holds_path)
    monkeypatch.setenv("MIND_AGENT", "alpha")
    monkeypatch.setenv("MIND_SID", "cd5fd3b9")

    # WRITE half — hold a row while querying somebody else's lane.
    monkeypatch.setattr("sys.argv", ["cnc", "--agent", "(unattributed)",
                                     "--hold", "g-9-77", "--reason", "peer leg"])
    assert m.main() == 0
    capsys.readouterr()
    assert seen == ["alpha"], f"ledger must key on the ACTING agent; got {seen}"
    assert not (tmp_path / "ledger-(unattributed).jsonl").exists(), (
        "keying by the queried holder sends record_hold — which mkdir(parents=True)s "
        "— at agents/(unattributed)/session/, manufacturing a directory for a bucket "
        "key that is not an agent (.claude/rules/path-resolution.md L1 cruft)")
    held = m.load_holds(tmp_path / "ledger-alpha.jsonl")
    assert [h["goal_id"] for h in held] == ["g-9-77"]
    assert held[0]["agent"] == "alpha", (
        "provenance is who DECIDED; '(unattributed)' is not an agent and decided nothing")

    # READ half — the goal's literal VERIFY: re-run the SAME slate command and
    # the held row is absent from `slate` AND counted as held back.
    rows = [_released("g-9-77", executed_by=None, age_h=100),
            _released("g-9-78", executed_by=None, age_h=90)]
    monkeypatch.setattr(m, "_load_rows", lambda timeout: rows)
    seen.clear()
    monkeypatch.setattr("sys.argv", ["cnc", "--agent", "(unattributed)",
                                     "--min-age-hours", "48", "--json"])
    assert m.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert seen == ["alpha"], "the READ must resolve the same ledger the write used"
    assert [r["goal_id"] for r in out["slate"]] == ["g-9-78"], (
        "the held row must be suppressed on the very next run of the same command")
    assert out["population"]["mine_held_back_recent_hold"] == 1


def test_own_lane_hold_behaviour_is_unchanged(tmp_path, monkeypatch, capsys):
    """The fix must not move the ONE lane that already worked: with no --agent,
    the acting agent and the queried holder are the same and nothing changes."""
    m = _load()
    seen = []
    monkeypatch.setattr(m, "holds_path",
                        lambda agent: (seen.append(agent),
                                       tmp_path / f"ledger-{agent}.jsonl")[1])
    monkeypatch.setenv("MIND_AGENT", "alpha")
    monkeypatch.setenv("MIND_SID", "cd5fd3b9")
    monkeypatch.setattr("sys.argv", ["cnc", "--hold", "g-9-01", "--reason", "own lane"])
    assert m.main() == 0
    capsys.readouterr()
    assert seen == ["alpha"]
    assert [h["goal_id"] for h in m.load_holds(tmp_path / "ledger-alpha.jsonl")] == ["g-9-01"]


# ── Content-keyed hold: the lane's EXIT () ──────────────────────────
# The TTL hold above is a LEASE, so a row correctly judged not-cnc re-qualifies
# every time the lease lapses — forever, because `is_drain_candidate` cannot tell
# a PROGRESS note from a completion note. Measured 2026-08-27 (zeta, cc-02): the
# (unattributed) lane served 3 rows, all re-serves, 13 prior dispositions between
# them, all reaching the same verdict, while 14 unlooked-at rows queued behind.
# Both axes pinned (guard-2319): an UNCHANGED note suppresses, a CHANGED one does
# not — the second is what keeps the lane able to catch a real completion note.

_JUDGED = "PROGRESS: 2 of 5 outcomes met; still open."


def _expired(gid, sha=None, *, hours=72):
    h = {"goal_id": gid,
         "held_at": (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S"),
         "reason": "verified NOT cnc — progress note on open work"}
    if sha is not None:
        h["note_sha"] = sha
    return h


def test_note_sha_normalises_whitespace_and_empties():
    m = _load()
    assert m._note_sha("a  b\n c") == m._note_sha("a b c"), "reflow must not read as new evidence"
    assert m._note_sha("") == "" and m._note_sha("   ") == "", "empty note is never a hold key"
    assert m._note_sha("a b") != m._note_sha("a c")


def test_unchanged_note_is_suppressed_past_ttl_and_stays_counted():
    """The EXIT: a long-expired hold still suppresses while the note is identical."""
    m = _load()
    rows = [_row("g-1-01", age_h=200, note=_JUDGED), _row("g-1-02", age_h=150)]
    hold = _expired("g-1-01", m._note_sha(_JUDGED))
    out = m.build_slate(rows, "alpha", limit=5, min_age_hours=6, now=NOW,
                        holds=[hold], hold_ttl_hours=24)
    assert [r["goal_id"] for r in out["slate"]] == ["g-1-02"], "re-serve of an already-judged row"
    pop = out["population"]
    assert pop["mine_held_back_note_unchanged"] == 1
    assert pop["mine_held_back_recent_hold"] == 0, "TTL lapsed — suppression is content-keyed"
    # never a SILENT keep (guard-3628): counted AND named
    assert pop["note_unchanged_goal_ids"] == ["g-1-01"]


def test_changed_note_resurfaces_immediately_with_no_ttl_wait():
    """The other axis: the completion note this lane exists to catch must get through."""
    m = _load()
    rows = [_row("g-1-01", age_h=200, note="DONE — all 5 outcomes met, ready to close.")]
    hold = _expired("g-1-01", m._note_sha(_JUDGED))   # judged against the OLD note
    out = m.build_slate(rows, "alpha", limit=5, min_age_hours=6, now=NOW,
                        holds=[hold], hold_ttl_hours=24)
    assert [r["goal_id"] for r in out["slate"]] == ["g-1-01"], \
        "a rewritten outcome_note must resurface the row on the very next run"
    assert out["population"]["mine_held_back_note_unchanged"] == 0
    assert out["slate"][0]["hold_count"] == 1, "prior judgement stays visible to the reader"


def test_legacy_hold_without_note_sha_is_ttl_only():
    """Backward compatibility: pre-existing ledger entries behave exactly as before."""
    m = _load()
    rows = [_row("g-1-01", age_h=200, note=_JUDGED)]
    fresh = dict(_expired("g-1-01", None, hours=2))
    out = m.build_slate(rows, "alpha", limit=5, min_age_hours=6, now=NOW,
                        holds=[fresh], hold_ttl_hours=24)
    assert out["population"]["mine_held_back_recent_hold"] == 1
    assert out["population"]["mine_held_back_note_unchanged"] == 0
    out2 = m.build_slate(rows, "alpha", limit=5, min_age_hours=6, now=NOW,
                         holds=[_expired("g-1-01", None)], hold_ttl_hours=24)
    assert [r["goal_id"] for r in out2["slate"]] == ["g-1-01"], "no digest -> old TTL behaviour"
    assert out2["population"]["mine_held_back_note_unchanged"] == 0


def test_record_hold_omits_note_sha_when_note_unreadable(tmp_path):
    """Fail-open: a hold must never be blocked by an unreadable goal, and an
    absent digest must not suppress the row against a digest of nothing."""
    m = _load()
    led = tmp_path / "cnc-drain-holds.jsonl"
    rec = m.record_hold(led, goal_id="g-1-09", reason="why", agent="alpha", sid="s",
                        now=NOW, note_sha="")
    assert "note_sha" not in rec, "empty digest must be OMITTED, not written empty"
    rec2 = m.record_hold(led, goal_id="g-1-10", reason="why", agent="alpha", sid="s",
                         now=NOW, note_sha=m._note_sha(_JUDGED))
    assert rec2["note_sha"] == m._note_sha(_JUDGED)
    assert len(m.load_holds(led)) == 2


# ── The >=3-hold escalation must not claim a rewrite that never happened ──────
# ()  left the escalation text asserting that a REPEAT hold
# means the note was REWRITTEN between judgements. That is true only once a PRIOR
# hold on the row carried a digest, and it is FALSE throughout the migration
# window — where it is also loudest, because the no-migration choice (holds
# predating the change carry no note_sha) helps the LONGEST-SERVING rows LAST.
# Measured 2026-08-28 (zeta, cc-02, 6.8.0-137-generic): live ledger 81 holds / 6
# with note_sha; the three lane rows read holds=6/4/6 with sha=0/0/0. Telling
# that reader to "read the note diff" sends them after a diff that cannot exist.
# BOTH branches are pinned — a one-sided test would pass against a hardcoded
# message (guard-2319).

def _seed_holds(m, ledger, gid, n, *, sha=None):
    """Append n holds for gid directly, bypassing record_hold's digest logic."""
    import json as _j
    with open(ledger, "a", encoding="utf-8") as fh:
        for i in range(n):
            rec = {"goal_id": gid, "held_at": "2026-08-20T0%d:00:00" % i,
                   "reason": "prior", "agent": "alpha", "sid": "abc"}
            if sha:
                rec["note_sha"] = sha
            fh.write(_j.dumps(rec) + "\n")


def _run_hold(m, tmp_path, monkeypatch, capsys, gid, note):
    monkeypatch.setattr(m, "holds_path", lambda agent: tmp_path / "ledger.jsonl")
    monkeypatch.setattr(m, "_load_one", lambda g, t: {"goal_id": g, "outcome_note": note})
    monkeypatch.setenv("MIND_AGENT", "alpha")
    monkeypatch.setenv("MIND_SID", "cd5fd3b9")
    monkeypatch.setattr("sys.argv", ["cnc", "--hold", gid, "--reason", "r"])
    assert m.main() == 0
    return capsys.readouterr().out


def test_escalation_says_clock_recycle_when_no_prior_hold_carried_a_digest(
        tmp_path, monkeypatch, capsys):
    m = _load()
    ledger = tmp_path / "ledger.jsonl"
    _seed_holds(m, ledger, "g-9-05", 5)                    # 5 pre- holds
    out = _run_hold(m, tmp_path, monkeypatch, capsys, "g-9-05", _JUDGED)
    assert "held 6x" in out
    assert "NO prior hold" in out and "CLOCK recycle" in out, (
        "with every prior hold sha-less the count records a clock recycle; "
        "claiming the note was rewritten sends the reader after a diff that "
        "cannot exist (g-115-6641)")
    assert "REWRITTEN between judgements" not in out


def test_escalation_says_rewrite_when_a_prior_hold_did_carry_a_digest(
        tmp_path, monkeypatch, capsys):
    m = _load()
    ledger = tmp_path / "ledger.jsonl"
    _seed_holds(m, ledger, "g-9-06", 2, sha=m._note_sha("an OLDER note"))
    out = _run_hold(m, tmp_path, monkeypatch, capsys, "g-9-06", _JUDGED)
    assert "held 3x" in out
    assert "REWRITTEN between judgements" in out, (
        "once a prior hold carried a digest, a repeat hold really does mean the "
        "note changed — that branch must survive")
    assert "NO prior hold" not in out


def test_prior_keyed_is_sampled_before_the_write_not_after(tmp_path, monkeypatch, capsys):
    """The regression this test exists for: load_holds re-reads from disk and
    returns fresh dicts, so an identity test against the record just written can
    never exclude it — every hold would then see its OWN digest as `prior` and
    the clock-recycle branch would be unreachable. Sampling before record_hold is
    the whole fix; this pins it with the exact input that defeats the naive form."""
    m = _load()
    ledger = tmp_path / "ledger.jsonl"
    _seed_holds(m, ledger, "g-9-07", 3)                    # sha-less priors only
    out = _run_hold(m, tmp_path, monkeypatch, capsys, "g-9-07", _JUDGED)
    written = [h for h in m.load_holds(ledger) if h["goal_id"] == "g-9-07"]
    assert len(written) == 4 and written[-1].get("note_sha"), (
        "this hold must itself be content-keyed — otherwise the test proves nothing")
    assert "NO prior hold" in out, (
        "the freshly-written digest must NOT count as a prior one")
