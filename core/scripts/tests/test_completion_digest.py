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


@pytest.fixture
def world(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    (agents / "alpha").mkdir(parents=True)
    (agents / "alpha" / "aspirations.jsonl").write_text("")
    monkeypatch.setattr(cd, "agents_root", lambda: agents)
    monkeypatch.setattr(cd, "_bash", lambda *a, **k: None)  # no pqs / pipeline / team-state
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
    notes = "\n".join(f"note line {i}" for i in range(30))
    md = cd.render(data, agent="alpha", since=SINCE, now=NOW, notes=notes, max_items=10)
    assert "Nothing is waiting on you right now." in md
    assert "note line 11" in md and "note line 12" not in md  # <=12 lines


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
