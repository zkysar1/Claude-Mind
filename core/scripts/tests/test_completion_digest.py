"""completion_digest.py -- the user-facing digest the completion report emails.

User direction 2026-08-17: "make them easier to read, be sure everything I need
to quickly understand how it has been going is in there". These pin the parts
that carry that: the asks come first and are NAMED, batch closes are labelled
rather than counted as throughput, and the digest is deterministic from the
stores. Hermetic: a tmp world, agents_root/pending-questions/pipeline/team-state
stubbed so nothing live is read.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import completion_digest as cd  # noqa: E402

NOW = datetime(2026, 8, 17, 1, 0, 0)
SINCE = NOW - timedelta(hours=48)


def _goal(gid, status="completed", **kw):
    g = {"id": gid, "title": kw.pop("title", f"Goal {gid}"), "status": status, "priority": "MEDIUM",
         "created_at": (NOW - timedelta(days=kw.pop("age_d", 3))).isoformat()}
    g.update(kw)
    return g


class _Ran:
    """A subprocess result that SUCCEEDED and returned `out`."""

    def __init__(self, out: str = "", rc: int = 0):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


def _bash_ok_empty(script, *args, **kw):
    """Default stub: the pending-questions read RUNS CLEAN and finds nothing.

    It must be distinguishable from a FAILED read (g-001-04 / zeta F2): returning
    None for every call — as this fixture did until 2026-09-20 — meant every test
    in this file exercised the DEGRADED path while asserting the all-clear text,
    so the laundering the fix removes was the pinned behaviour. Pipeline and
    team-state stay dead (None); no test here asserts on them.
    """
    if str(script).endswith("pending-questions-read.sh"):
        return _Ran("[]")
    return None


@pytest.fixture
def world(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    (agents / "alpha").mkdir(parents=True)
    (agents / "alpha" / "aspirations.jsonl").write_text("")
    monkeypatch.setattr(cd, "agents_root", lambda: agents)
    monkeypatch.setattr(cd, "_bash", _bash_ok_empty)  # pqs clean-empty; pipeline/team-state dead
    w = tmp_path / "world"
    w.mkdir()
    return w


def _write(world, asps):
    (world / "aspirations.jsonl").write_text("\n".join(json.dumps(a) for a in asps) + "\n")


def test_asks_are_named_first_and_oldest_first(world):
    _write(world, [{
        "id": "asp-1", "title": "Ship it", "status": "active", "goals": [
            _goal("g-1-1", "pending", participants=["agent", "user"], user_leg_scope="credential", age_d=10),
            _goal("g-1-2", "pending", participants=["agent", "user"], age_d=2),
            _goal("g-1-3", "pending", defer_reason="human_blocked: needs your GUI click", age_d=5),
            _goal("g-1-4", "completed", completed_at=(NOW - timedelta(hours=3)).isoformat(), completed_by="alpha",
                  outcome_class="deep", completed_by_sid="aaaa1111"),
        ]}])
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    assert [n["id"] for n in data["needs"]] == ["g-1-1", "g-1-3", "g-1-2"]
    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert md.index("## Needs you") < md.index("## Done this window")
    assert "1. **g-1-1**" in md and "NEEDS FROM YOU: credential" in md
    assert "human-gated" in md and "needs your GUI click" in md
    assert "our bug" in md  # g-1-2 has no scope recorded -- say so, do not hide it
    assert "- Done: **1** goals" in md and "alpha 1" in md


def test_batch_close_is_labelled_not_counted_as_throughput(world):
    goals = [_goal(f"g-2-{i}", "completed", completed_at=(NOW - timedelta(hours=5, minutes=i // 4)).isoformat(),
                   completed_by="alpha", completed_by_sid="bulk0001") for i in range(40)]
    goals += [_goal(f"g-2-9{i}", "completed", completed_at=(NOW - timedelta(hours=20 + i)).isoformat(),
                    completed_by="zeta", completed_by_sid=f"z{i}") for i in range(3)]
    _write(world, [{"id": "asp-2", "title": "Bulk", "status": "active", "goals": goals}])
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    assert len(data["batches"]) == 1 and data["batches"][0]["n"] == 40 and data["batches"][0]["by"] == "alpha"
    assert sum(1 for d in data["done"] if d["batch"]) == 40
    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "- Done: **3** goals" in md and "zeta 3" in md
    assert "Also **40** batch-closed" in md and "not today's throughput" in md
    assert "## Done this window (3 + 40 batch-closed)" in md


def test_blocked_shows_cause_and_what_it_holds_up(world):
    _write(world, [{"id": "asp-3", "title": "Deps", "status": "active", "goals": [
        _goal("g-3-1", "blocked", title="Root cause goal", defer_reason="precondition_unmet: vendor outage"),
        _goal("g-3-2", "pending", blocked_by=["g-3-1"]),
        _goal("g-3-3", "pending", blocked_by=["g-3-1"]),
    ]}])
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "## Blocked" in md and "g-3-1" in md and "holds up 2 goal(s)" in md and "vendor outage" in md


def test_notes_are_bounded_and_nothing_waiting_is_said_plainly(world):
    _write(world, [{"id": "asp-4", "title": "Quiet", "status": "active", "goals": [_goal("g-4-1", "pending")]}])
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    # 50 > _NOTES_MAX_LINES (40), so the bound is actually exercised. This pinned
    # 30-vs-12 until 2026-09-16:  RAISED the cap 12 -> 40 after a
    # production email truncated mid-word, and left this assertion behind — so the
    # test was red on HEAD and pinning a bound the code had deliberately retired.
    # Both halves are asserted now, because guard-3976/guard-3698 made the MARKER
    # the load-bearing half: an unannounced cut is the defect, not the cut itself.
    notes = "\n".join(f"note line {i}" for i in range(50))
    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes=notes, max_items=10)
    assert "Nothing is waiting on you right now." in md
    assert "note line 39" in md and "note line 40" not in md  # <=40 lines
    assert "TRUNCATED" in md  # the cut is announced, never silent


def test_cli_writes_out_file_and_wrapper_exists(world, tmp_path):
    _write(world, [{"id": "asp-5", "title": "CLI", "status": "active", "goals": [
        _goal("g-5-1", "completed", completed_at=(NOW - timedelta(hours=1)).isoformat(), completed_by="echo")]}])
    out = tmp_path / "digest.md"
    rc = cd.main(["--agent", "echo", "--since", SINCE.isoformat(), "--world", str(world), "--out", str(out)])
    assert rc == 0 and out.exists()
    assert out.read_text().startswith("# Fleet digest — ")
    assert (SCRIPTS / "completion-digest.sh").exists()


def test_skill_wiring_sends_user_digest_through_the_dispatcher():
    root = SCRIPTS.parent.parent
    skill = (root / ".claude" / "skills" / "agent-completion-report" / "SKILL.md").read_text(encoding="utf-8")
    assert "core/scripts/completion-digest.sh" in skill
    assert "core/scripts/notify-user.sh" in skill and "--category user-digest" in skill
    # the status-blurb category is the one the routing gate suppresses -- the
    # digest must NOT be sent under it
    assert "--category completion" not in skill


def test_html_twin_is_balanced_escaped_and_carries_the_same_asks(world):
    _write(world, [{
        "id": "asp-6", "title": "Html <b>bold</b>", "status": "active", "goals": [
            _goal("g-6-1", "pending", title="Fix <script>alert(1)</script> thing", participants=["agent", "user"],
                  user_leg_scope="credential", age_d=4),
            _goal("g-6-2", "completed", completed_at=(NOW - timedelta(hours=2)).isoformat(), completed_by="echo"),
        ]}])
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    html = cd.render_html(data, agent="alpha", since=SINCE, now=NOW, notes="one note", max_items=10)
    assert html.lstrip().startswith("<html>")
    assert "<script>" not in html and "&lt;script&gt;" in html  # store text is escaped, never rendered
    assert "g-6-1" in html and "Needs from you:" in html and "credential" in html
    assert "g-6-2" in html and "one note" in html
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.bad = [], []

        def handle_starttag(self, t, a):
            if t not in ("br", "meta", "img", "hr"):
                self.stack.append(t)

        def handle_endtag(self, t):
            if self.stack and self.stack[-1] == t:
                self.stack.pop()
            else:
                self.bad.append(t)
    p = P()
    p.feed(html)
    assert not p.bad and not p.stack


def test_new_asks_are_marked_and_corrected_hypotheses_listed(world, monkeypatch):
    _write(world, [{"id": "asp-7", "title": "New", "status": "active", "goals": [
        _goal("g-7-1", "pending", participants=["agent", "user"], age_d=1),   # inside the 48h window -> NEW
        _goal("g-7-2", "pending", participants=["agent", "user"], age_d=9),
    ]}])
    import subprocess as sp

    def fake_bash(script, *args, timeout=60):
        if script.endswith("pipeline-read.sh") and "--stage" in args:
            return sp.CompletedProcess(args, 0, json.dumps([
                {"id": "h1", "title": "The cache is always warm", "outcome": "CORRECTED", "resolved_at": (NOW - timedelta(hours=5)).isoformat()},
                {"id": "h2", "title": "x", "outcome": "CONFIRMED", "resolved_at": (NOW - timedelta(hours=5)).isoformat()}]), "")
        return None
    monkeypatch.setattr(cd, "_bash", fake_bash)
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    assert [n["id"] for n in data["needs"] if n["new"]] == ["g-7-1"]
    assert data["hyp"]["corrected"][0]["title"] == "The cache is always warm"
    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "**g-7-1** NEW" in md and "New asks this window: **1**" in md
    assert "## What we got wrong" in md and "The cache is always warm" in md


def test_cost_hook_slot_feeds_the_spend_card_and_failures_omit_it(world):
    _write(world, [{"id": "asp-8", "title": "Cost", "status": "active", "goals": [_goal("g-8-1", "pending")]}])
    (world / "scripts").mkdir()
    slot = world / "scripts" / "digest-cost.sh"
    slot.write_text('#!/usr/bin/env bash\necho \'{"headline":"Cloud $6.83 yesterday","tiles":[{"label":"Cloud yesterday","value":"$6.83","sub":"2026-08-16"}],'
                    '"lines":["Inference APIs: not measured"],"note":"n","as_of":"2026-08-17 00:10 UTC","stale":false}\'\n')
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    assert data["cost"]["headline"].startswith("Cloud $6.83")
    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "## Spend — Cloud $6.83 yesterday" in md and "Cloud yesterday: **$6.83**" in md and "Inference APIs: not measured" in md
    html = cd.render_html(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "Spend — Cloud $6.83 yesterday" in html and "$6.83" in html
    # a broken slot never breaks the digest: card omitted, everything else renders
    slot.write_text("#!/usr/bin/env bash\necho not-json; exit 1\n")
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    assert data["cost"] == {}
    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "## Spend" not in md and "## Needs you" in md
    # no slot at all: same
    slot.unlink()
    assert cd.gather(world, "alpha", SINCE, NOW, 10)["cost"] == {}


def test_window_clamp_is_visible_when_queue_retention_is_shorter_than_since(world):
    """The digest counts only the LIVE queue, which is retention-pruned (guard-4085).

    Pre-fix, the Window label and the /day denominator both used the REQUESTED
    window while the rows came from whatever the queue still retained, so a
    301h-labelled report over 76h of data understated every rate ~4x and a quiet
    agent whose closes had archived read as idle (g-115-9405). The clamp must be
    ANNOUNCED, not applied silently -- a silent clamp is worse than the original
    bug, because the number stops being wrong and starts being unfalsifiable
    (guard-2131: never present a window whose recency you have not measured).
    """
    oldest = NOW - timedelta(hours=6)
    _write(world, [{
        "id": "asp-1", "title": "Retention", "status": "active", "goals": [
            _goal("g-1-1", "completed", completed_at=oldest.isoformat(),
                  completed_by="alpha", outcome_class="deep", completed_by_sid="aaaa1111"),
            _goal("g-1-2", "completed", completed_at=(NOW - timedelta(hours=2)).isoformat(),
                  completed_by="bravo", outcome_class="deep", completed_by_sid="bbbb2222"),
        ]}])
    # SINCE is 48h back; the queue retains nothing older than 6h.
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    cov = data["coverage"]
    assert cov["clamped"] is True
    assert cov["queue_oldest_completed"] == oldest.isoformat()
    assert cov["covered_from"] == oldest.isoformat()
    # THE PIN: covered span must be the data's, not the request's.
    assert 5.9 < cov["covered_hours"] < 6.1, cov["covered_hours"]
    assert cov["covered_hours"] < cd._hours(SINCE, NOW)
    # The unread archive is stated at every call site, not left to be inferred.
    assert "archive" in cov["source"]

    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "DATA COVERS ONLY" in md, "clamp applied silently -- the number is now unfalsifiable"
    # 2 closes over ~6h is ~8/day; over the requested 48h it would read ~1/day.
    assert "/day" in md and "(~1.0/day)" not in md


def test_fully_covered_window_does_not_clamp(world):
    """Negative control: an unconditional clamp would pass the test above alone."""
    _write(world, [{
        "id": "asp-1", "title": "Covered", "status": "active", "goals": [
            _goal("g-1-1", "completed", completed_at=(NOW - timedelta(hours=3)).isoformat(),
                  completed_by="alpha", outcome_class="deep", completed_by_sid="aaaa1111"),
        ]}])
    # Ask for 2h of window; the only close is 3h old, so nothing is IN window and
    # the queue floor does not predate the request in the clamping direction.
    since = NOW - timedelta(hours=48)
    data = cd.gather(world, "alpha", since, NOW, 10)
    assert data["coverage"]["clamped"] is True  # floor (3h) is newer than since (48h)

    # Now a request the queue genuinely covers end-to-end.
    since_covered = NOW - timedelta(hours=1)
    data2 = cd.gather(world, "alpha", since_covered, NOW, 10)
    assert data2["coverage"]["clamped"] is False, "clamp fired on a fully-covered window"
    md = cd.render(data2, agent="alpha", since=since_covered, now=NOW, notes="", max_items=10)
    assert "DATA COVERS ONLY" not in md


def test_recurring_firings_count_the_world_queue_not_only_the_agent_queues(world):
    """The recurring-sweep count must cover BOTH stores.

    Regression pin for g-001-04 occ103 (2026-09-18): the counter iterated
    `agent_files` only, so every WORLD-level sensor was invisible. It passed the
    per-agent scope leg perfectly -- the agent-queue subtotal was exactly right --
    and still understated the live fleet by 72%, because 101 of 109 recurring
    sensors lived in the world queue. The digest published an agent-private count
    as a claim about the fleet, in the one number the user reads.

    Eleven tests passed while that was live and not one of them said the word
    `recurring`. So this asserts the SUM across both stores: an assertion scoped
    to the agent side alone is exactly what the defect already satisfied.
    """
    fired = (NOW - timedelta(hours=2)).isoformat()
    stale = (SINCE - timedelta(hours=5)).isoformat()  # before the window -- must not count

    agent_queue = cd.agents_root() / "alpha" / "aspirations.jsonl"
    agent_queue.write_text(json.dumps({
        "id": "asp-001", "title": "Agent upkeep", "status": "active", "goals": [
            _goal("g-001-04", "pending", recurring=True, lastAchievedAt=fired),
            _goal("g-001-09", "pending", recurring=True, lastAchievedAt=stale),
        ]}) + "\n")

    _write(world, [{
        "id": "asp-115", "title": "Recurring infrastructure monitoring", "status": "active", "goals": [
            _goal("g-115-817", "pending", recurring=True, lastAchievedAt=fired),
            _goal("g-115-105", "pending", recurring=True, lastAchievedAt=fired),
            _goal("g-115-999", "pending", recurring=True, lastAchievedAt=stale),
            _goal("g-115-000", "pending", recurring=False, lastAchievedAt=fired),
        ]}])

    data = cd.gather(world, "alpha", SINCE, NOW, 10)

    # 2 world + 1 agent. The pre-fix code returns 1 here.
    assert data["recurring"] == 3, (
        f"expected 3 firings (2 world + 1 agent), got {data['recurring']} -- "
        "a result of 1 means the world queue is not being scanned"
    )
    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "+3 recurring sweeps" in md


# ---------------------------------------------------------------------------
#  / zeta fresh-eyes F1+F2 (msg-20260920-040010, msg-20260920-040028):
# the owner-facing digest understated every lane by computing its lifetime
# fraction from the retention-pruned `goals` array, and rendered a FAILED
# pending-questions read as an affirmative "nothing is waiting on you".
# ---------------------------------------------------------------------------


def test_lifetime_fraction_reads_the_counter_not_the_pruned_goals_array(world):
    """The pruned array understates; the authoritative counter is the source.

    Measured on the live store 2026-09-20: asp-115 rendered 107/2607 (4%) from
    the array against 6727/10333 (65%) from its counter. The array is an
    ACTIVITY projection, not the population (guard-3410 / guard-4963 /
    guard-3833), so a fraction over it inverts the consolidate-before-expand
    signal on the surface the owner reads.
    """
    _write(world, [{
        "id": "asp-900", "title": "Pruned lane", "status": "active",
        # the live queue retains 2 of the lane's goals; 1 of them is completed
        "goals": [_goal("g-900-1", "completed",
                        completed_at=(NOW - timedelta(hours=2)).isoformat(), completed_by="echo"),
                  _goal("g-900-2", "pending")],
        # ...while the lane has actually filed 1000 and completed 650
        "progress": {"completed_goals": 650, "total_goals": 1000},
    }])
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    row = [a for a in data["active_asps"] if a["id"] == "asp-900"][0]
    assert (row["done"], row["total"], row["source"]) == (650, 1000, "counter")

    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "650/1000 (65%)" in md
    assert "1/2 (50%)" not in md, "the pruned-array fraction reached the owner's table"
    # the column says what it MEANS (guard-5368: terminal-not-complete goals are
    # in the denominator), so 65% is not misread as 'of the work left, 65% done'
    assert "over all goals ever filed in the lane" in md
    # a counter-sourced row carries no understatement label
    assert "UNDERSTATED" not in md


def test_zero_counter_lane_falls_back_to_the_array_and_says_so(world):
    """guard-3602: a 0/0 counter beside a real goals array must not blank the row.

    This is the case a naive `len(goals)` -> `progress.*` swap breaks, and it
    breaks it by DISAPPEARANCE rather than by a wrong number: render()'s
    `if a["total"]` filter drops any row whose total is 0, so the lane silently
    leaves the owner's table. Fall back to the array, and label the row -- an
    unlabelled fallback mixes two populations in one column invisibly.
    """
    _write(world, [{
        "id": "asp-901", "title": "Counter never populated", "status": "active",
        "goals": [_goal("g-901-1", "completed",
                        completed_at=(NOW - timedelta(hours=3)).isoformat(), completed_by="echo"),
                  _goal("g-901-2", "pending"),
                  _goal("g-901-3", "pending")],
        "progress": {"completed_goals": 0, "total_goals": 0},
    }])
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    row = [a for a in data["active_asps"] if a["id"] == "asp-901"][0]
    assert (row["done"], row["total"], row["source"]) == (1, 3, "array")

    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "asp-901" in md, "a 0/0-counter lane vanished from the owner's table"
    assert "1/3 (33%)" in md
    assert "UNDERSTATED" in md, "the array fallback was not labelled"
    html = cd.render_html(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "live queue only" in html


def test_missing_progress_key_falls_back_without_raising(world):
    """An aspiration with no `progress` at all must degrade, not crash."""
    _write(world, [{"id": "asp-902", "title": "No counter key", "status": "active",
                    "goals": [_goal("g-902-1", "pending"), _goal("g-902-2", "pending")]}])
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    row = [a for a in data["active_asps"] if a["id"] == "asp-902"][0]
    assert (row["done"], row["total"], row["source"]) == (0, 2, "array")


@pytest.mark.parametrize("stub,why", [
    (lambda script, *a, **k: None, "subprocess never ran (import error / spawn failure / timeout)"),
    (lambda script, *a, **k: _Ran("", rc=1), "non-zero exit"),
    (lambda script, *a, **k: _Ran("not json at all"), "unparseable stdout"),
])
def test_failed_pending_questions_read_is_announced_not_rendered_as_zero(world, monkeypatch, stub, why):
    """A degraded read must never reach the owner as an affirmative all-clear.

    `pqs` falls to [] on three indistinguishable failures. The digest is EMAILED
    to the owner, so rendering "0 open question(s)" + "Nothing is waiting on you
    right now." tells a human that nothing needs them on the strength of a read
    that did not happen. verify-before-assuming.md rule 4: a try/except around a
    parse is ZERO signals, not one.
    """
    monkeypatch.setattr(cd, "_bash", stub)
    _write(world, [{"id": "asp-903", "title": "Quiet lane", "status": "active",
                    "goals": [_goal("g-903-1", "pending")]}])
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    assert data["pqs_read_ok"] is False, why

    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "Nothing is waiting on you right now." not in md, (
        "a failed read rendered as an affirmative all-clear (%s)" % why)
    assert "READ FAILED" in md and "UNKNOWN" in md
    assert "0 open question(s)" not in md

    html = cd.render_html(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "Nothing is waiting on you right now." not in html
    assert "READ FAILED" in html


def test_clean_empty_pending_questions_read_still_says_nothing_is_waiting(world):
    """The positive control: a read that RAN and found nothing is still an all-clear.

    Without this, the fix above could be satisfied by never printing the
    reassurance at all -- which would be a different defect, not a fix.
    """
    _write(world, [{"id": "asp-904", "title": "Genuinely quiet", "status": "active",
                    "goals": [_goal("g-904-1", "pending")]}])
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    assert data["pqs_read_ok"] is True
    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "Nothing is waiting on you right now." in md
    assert "READ FAILED" not in md


def test_degraded_read_flags_are_surfaced_for_audit(world, monkeypatch):
    """pulse / hyp / outcome get the same auditability the predicate flag has."""
    monkeypatch.setattr(cd, "_bash", lambda script, *a, **k: None)
    _write(world, [{"id": "asp-905", "title": "All reads dead", "status": "active", "goals": []}])
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    for flag in ("pqs_read_ok", "pulse_read_ok", "hyp_read_ok"):
        assert data[flag] is False, f"{flag} did not report the dead read"
    # outcome-metrics.yaml is absent by DESIGN in this world, which is not a
    # degraded read -- the flag must distinguish the two.
    assert data["outcome_read_ok"] is True


# ---------------------------------------------------------------------------
#  -- the FILE read that 's four subprocess flags missed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("break_it, why", [
    (lambda w: None,
     "store MISSING entirely -- _load_jsonl returned [] for a file that is not there"),
    (lambda w: (w / "aspirations.jsonl").write_text('{"id": "asp-906"\nnot json at all\n'),
     "store PRESENT but unparseable -- the old bare `except: continue` dropped both lines"),
])
def test_unreadable_aspirations_store_is_not_rendered_as_zero_blocked(world, break_it, why):
    """The aspirations read feeds the table, the blocked tally AND the needs-you list.

    Rendering its fail-to-[] as "Blocked: **0** goal(s)" and "Nothing is waiting on
    you right now." tells the owner nothing needs him on the strength of a read that
    did not happen -- the same defect g-001-04 fixed for the four reads that SHELL
    OUT, on the one that reads a FILE. Both twins must say so (guard-5392).
    """
    break_it(world)
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    assert data["asps_read_ok"] is False, why

    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "Blocked: **0** goal(s)" not in md, f"affirmative zero survived ({why})"
    assert "Nothing is waiting on you right now." not in md, f"affirmative all-clear survived ({why})"
    assert "Nothing blocked." not in md, f"affirmative all-clear survived ({why})"
    assert "aspirations store could not be read" in md

    html = cd.render_html(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "Nothing is waiting on you right now." not in html, f"the EMAIL twin still said it ({why})"
    assert "Nothing blocked." not in html, f"the EMAIL twin still said it ({why})"
    assert "aspirations store could not be read" in html
    assert "READ FAILED" in html


def test_readable_but_empty_aspirations_store_still_says_nothing_is_waiting(world):
    """The positive control, twin of the pending-questions one above.

    A file that EXISTS and parses cleanly is `ok` even at zero rows, so a genuinely
    empty world still renders its all-clear -- the fix cannot be satisfied by never
    printing the reassurance.
    """
    (world / "aspirations.jsonl").write_text("")
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    assert data["asps_read_ok"] is True
    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "Nothing is waiting on you right now." in md
    assert "aspirations store could not be read" not in md


def test_a_true_zero_total_lane_is_absent_from_BOTH_twins(world):
    """guard-4392: a filter applied to one consumer reads as applied to both.

    The markdown filtered `if a["total"]`; the HTML did not, so a lane whose counter
    is 0/0 AND whose goals array is empty rendered in the owner's email as
    "0/0 (0%)" -- an affirmative "no progress" -- and nowhere in the markdown. No
    prior test built one: the nearest put a 0/0 counter beside a THREE-goal array.
    """
    _write(world, [
        {"id": "asp-907", "title": "Truly zero lane", "status": "active",
         "goals": [], "progress": {"total_goals": 0, "completed_goals": 0}},
        {"id": "asp-908", "title": "Real lane", "status": "active",
         "goals": [_goal("g-908-1", "completed"), _goal("g-908-2", "pending")]},
    ])
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    ids = [a["id"] for a in data["active_asps"]]
    assert "asp-907" not in ids, "the 0/0 lane reached the shared population"
    assert "asp-908" in ids, "positive control: the real lane must survive the filter"

    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    html = cd.render_html(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    for surface, name in ((md, "markdown"), (html, "html")):
        assert "asp-907" not in surface, f"the 0/0 lane rendered in the {name} twin"
        assert "asp-908" in surface, f"positive control missing from the {name} twin"


# ---------------------------------------------------------------------------
#  -- the fresh-eyes pass over 's own commit.
# One test per FAILURE MODE (rb-11412), not one per fix.
# ---------------------------------------------------------------------------

def test_absent_read_flag_degrades_in_both_twins_like_its_sibling(world):
    """F-1 FAIL DIRECTION: an ABSENT `asps_read_ok` must read as NOT-ok.

    The flag's entire job is to suppress an all-clear the read cannot support, so a
    fail-OPEN default poisons every assertion of its own value (guard-1718). Its
    sibling `pqs_read_ok` -- same module, same two functions, written a day earlier
    for the identical purpose -- already defaults closed; this pins the pair
    together so they cannot drift apart again.

    gather() writes the key unconditionally (traced: assigned at the top of the
    function, returned in the one return dict), so no live path reaches the default.
    That is exactly why a test is the only thing that can hold the direction: the
    defect is invisible in production until someone hand-builds a `data`.
    """
    _write(world, [{"id": "asp-909", "title": "Real lane", "status": "active",
                    "goals": [_goal("g-909-1", "blocked")]}])
    data = cd.gather(world, "alpha", SINCE, NOW, 10)
    assert data.pop("asps_read_ok") is True, "positive control: the live read IS ok"
    assert "asps_read_ok" not in data

    for render, name in ((cd.render, "markdown"), (cd.render_html, "html")):
        surface = render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
        assert "Nothing is waiting on you right now." not in surface, \
            f"{name} twin gave the all-clear on an ABSENT read flag"
        assert "Nothing blocked." not in surface, \
            f"{name} twin gave the all-clear on an ABSENT read flag"
        assert "aspirations store could not be read" in surface, \
            f"{name} twin did not announce the degradation"
    # The sibling it must match: absent key -> degraded, same as this one.
    data.pop("pqs_read_ok", None)
    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes="", max_items=10)
    assert "READ FAILED" in md, "the pqs_read_ok sibling changed direction"


def test_a_failed_line_count_degrades_the_flag_and_keeps_the_recovered_rows(world, monkeypatch):
    """F-2: a failed measurement is not a measurement (guard-1091).

    The count is a SECOND open() of a file on the synced mount. When it shared the
    recovery read's try, a count failure landed in the except-branch, DISCARDED rows
    that had already been read correctly, and re-parsed with the weaker fallback --
    which cannot reach a history snapshot, so it can return FEWER records and can
    even report ok over a store the recovery reader had flagged.

    The stub returns THREE rows for a ONE-line file, which is the history-snapshot
    recovery shape. That makes the discriminator sharp: if the fallback ran, only
    one row survives.
    """
    import _fileops

    path = world / "aspirations.jsonl"
    path.write_text('{"id": "asp-910"}\n')
    recovered = [{"id": "asp-910"}, {"id": "asp-911"}, {"id": "asp-912"}]
    monkeypatch.setattr(_fileops, "read_jsonl_with_recovery", lambda p: list(recovered))

    # Positive control FIRST: with open() working, the same stub reports ok.
    rows_ok, ok = cd._load_jsonl_checked(path)
    assert rows_ok == recovered and ok is True, "control: 3 recovered rows >= 1 line is ok"

    real_open = Path.open

    def _open_raises(self, *a, **kw):
        if self == path:
            raise OSError("transient synced-mount failure during the count")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", _open_raises)
    rows, ok = cd._load_jsonl_checked(path)
    assert rows == recovered, "the already-recovered rows were discarded by a COUNT failure"
    assert ok is False, "an unverifiable count must degrade the flag"


def test_the_flag_free_wrapper_does_not_pay_for_a_count_it_discards(world, monkeypatch):
    """F-3: `_load_jsonl` throws the flag away, so it must not buy one.

    Measured 840,626 B of redundant reads per digest run across the five agent
    queues, and those files only grow. The contract when the count is skipped is
    ok=False meaning UNVERIFIED -- never a cheerful True nobody measured
    (guard-7131).
    """
    import _fileops

    path = world / "aspirations.jsonl"
    path.write_text('{"id": "asp-913"}\n')
    monkeypatch.setattr(_fileops, "read_jsonl_with_recovery", lambda p: [{"id": "asp-913"}])

    opens = []
    real_open = Path.open

    def _counting_open(self, *a, **kw):
        if self == path:
            opens.append(str(self))
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", _counting_open)

    assert cd._load_jsonl(path) == [{"id": "asp-913"}]
    assert opens == [], "the flag-free wrapper still paid for the count pass"

    opens.clear()
    rows, ok = cd._load_jsonl_checked(path)
    assert ok is True and len(opens) == 1, \
        "positive control: the checked caller must still take exactly one count pass"
    assert cd._load_jsonl_checked(path, count_lines=False)[1] is False, \
        "an unmeasured flag must read UNVERIFIED, not healthy"


def test_a_peer_append_between_the_count_and_the_read_does_not_flip_the_flag(world, monkeypatch):
    """: the two reads are unlocked, so their ORDER decides the lean.

    Five agents append to world/aspirations.jsonl continuously. Under
    read-then-count, one such append made `rows` the OLD short list and `n_lines`
    the NEW long count, so 100 >= 101 was False and the digest told the owner the
    aspirations store had failed to read -- about a store that read perfectly.

    guard-5074: a test that mocks a predicate production re-evaluates across a side
    effect cannot see an ordering bug, and it reads as clean. So the stub does not
    return a constant -- it delegates to the real recovery reader and performs the
    peer's append itself.

    THE APPEND MUST LAND *AFTER* THE READER HAS TAKEN ITS DATA, and that detail is
    the entire discriminating power of this test. A first draft appended BEFORE
    delegating, so under either order the reader saw the appended row and both
    counts agreed -- the test passed against the pre-fix code and pinned nothing
    (caught by the order-swap mutation, not by review; guard-3952: an assertion can
    prove the consumer SURVIVED the data rather than READ it). Appending after the
    read models the race truthfully:
      count-then-read (fixed): count 2, read 2  -> 2 >= 2 -> ok
      read-then-count (pre-fix): read 2, count 3 -> 2 >= 3 -> FALSE ALARM
    """
    import _fileops

    path = world / "aspirations.jsonl"
    path.write_text('{"id": "asp-920"}\n{"id": "asp-921"}\n')
    real_reader = _fileops.read_jsonl_with_recovery

    # Positive control FIRST: undisturbed, this file reads ok.
    assert cd._load_jsonl_checked(path)[1] is True, "control: an undisturbed read is ok"

    appended = []

    def _read_then_append(p):
        rows_seen = real_reader(p)
        # The peer's append lands HERE -- after we have our data, and therefore
        # before any count that is taken later.
        with Path(p).open("a", encoding="utf-8") as fh:
            fh.write('{"id": "asp-922"}\n')
        appended.append(str(p))
        return rows_seen

    monkeypatch.setattr(_fileops, "read_jsonl_with_recovery", _read_then_append)

    rows, ok = cd._load_jsonl_checked(path)
    assert appended, "the stub never ran -- this test proved nothing"
    assert len(rows) == 2, "the read saw the file as it was before the peer's append"
    assert path.read_text().count("asp-922") == 1, \
        "precondition: the peer's append must actually have landed on disk"
    assert ok is True, "a concurrent APPEND must not read as a failed store"


def test_a_shrink_between_the_count_and_the_read_still_alarms(world, monkeypatch):
    """The control that stops the fix trading a false positive for a false negative.

    Tolerating growth is only safe if LOSS still alarms, so this direction must be
    pinned separately. guard-5867: a clean reading is evidence about a mechanism
    only if its triggering precondition was actually met inside the sample -- so the
    file genuinely LOSES lines across the side effect here rather than the test
    asserting over an undisturbed read.

    This case also discriminates the order a second time, in the opposite direction:
    read-then-count saw the shrunken file BOTH times and reported ok, so the reorder
    closes a false PASS as well as the false alarm above.
    """
    import _fileops

    path = world / "aspirations.jsonl"
    path.write_text('{"id": "asp-930"}\n{"id": "asp-931"}\n{"id": "asp-932"}\n')
    real_reader = _fileops.read_jsonl_with_recovery

    assert cd._load_jsonl_checked(path)[1] is True, "control: the intact file reads ok"

    shrunk = []

    def _truncate_then_read(p):
        Path(p).write_text('{"id": "asp-930"}\n', encoding="utf-8")
        shrunk.append(str(p))
        return real_reader(p)

    monkeypatch.setattr(_fileops, "read_jsonl_with_recovery", _truncate_then_read)

    rows, ok = cd._load_jsonl_checked(path)
    assert shrunk, "the stub never ran -- this test proved nothing"
    assert len(rows) == 1, "the read must see the shrunken file"
    assert ok is False, "a store that LOST lines must still degrade the flag"


def test_a_second_path_failure_in_the_fallback_degrades_instead_of_raising(world, monkeypatch, capsys):
    """: the except-fallback re-reads the SAME path, so a PATH failure
    defeats it twice -- and before the fix the second failure RAISED.

    The fallback exists for a parse the recovery reader could not do; it re-reads
    the same file with a plain json.loads per line. That buys parser independence
    and NOT path independence (guard-6944). So the one exception class most likely
    to land in it -- an I/O error on the path -- is exactly the one it could not
    survive: `read_text` sat outside any try, the OSError escaped
    `_load_jsonl_checked`, and neither `gather()` nor `main()` guards the call, so
    a store that vanished or rotated mid-read killed the entire owner digest. The
    flag exists so a bad read still RENDERS, saying the store read FAILED; raising
    is strictly worse than the failure it was built to report.

    Both orders, traced before writing the stub (rb-11415 -- a test must be shown
    to discriminate, not assumed to):
      pre-fix:  reader raises -> read_text raises OSError -> ESCAPES (this call errors)
      fixed:    reader raises -> read_text raises OSError -> caught -> ([], False)
    So reaching the assertions at all is itself part of the discrimination.

    The stderr line is asserted too, not as decoration: `not path.exists()` returns
    this identical tuple for a different reason, and two causes behind one silent
    value make the step undiagnosable from its own output (guard-2586).
    """
    import _fileops

    path = world / "aspirations.jsonl"
    path.write_text('{"id": "asp-940"}\n{"id": "asp-941"}\n')

    assert cd._load_jsonl_checked(path)[1] is True, "control: the intact file reads ok"

    raised = []

    def _vanish_then_raise(p):
        # The realistic trigger, not a synthetic one: the reader fails BECAUSE the
        # path went away under it (the synced mount rb-2970 records as transiently
        # misbehaving). Deleting here is what makes the fallback's own read fail.
        Path(p).unlink()
        raised.append(str(p))
        raise OSError("simulated reader failure on the synced mount")

    monkeypatch.setattr(_fileops, "read_jsonl_with_recovery", _vanish_then_raise)

    rows, ok = cd._load_jsonl_checked(path)
    assert raised, "the stub never ran -- this test proved nothing"
    assert not path.exists(), "precondition: the path must really be gone for the fallback to fail"
    assert rows == [], "an unreadable store yields no rows"
    assert ok is False, "an unreadable store must degrade the FLAG, not raise"
    assert "fallback read" in capsys.readouterr().err, \
        "the failure must name itself on stderr, not return a silent tuple"
