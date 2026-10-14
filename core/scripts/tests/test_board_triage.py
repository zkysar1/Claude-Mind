"""Fixture tests for board_triage.py over a PLANTED board in a tmp world.

Never touches the live board or the live world.  STORAGE_BACKEND=local
is pinned for every test run (see autouse fixture).

Verification outcomes covered:
  1. FYI post is marked HANDLED with no reply.
  2. DO NOW answer is posted with --reply-to and counts toward the budget,
     and a fourth DO NOW-eligible ask in one iteration stays unhandled and
     is handled the next iteration.
  3. A work ask from another deployment becomes a goal carrying the peer's
     deadline as resolves_by, and an ask that touches code, money,
     production or owner approval is refused as DO NOW.

Test scenarios (a)-(g):
  (a) is_peer_author: @env comparison with ENVIRONMENT_ID, unknown bare -> PEER
  (b) extract_deadline: expires:<ISO> tag parsing, missing -> None
  (c) dispose DO_NOW: io["post_reply"] called with reply_to=msg_id
  (d) dispose GOAL: io["file_goal"] called, body has origin_signal + resolves_by
  (e) budget persistence: write/read/reset with tmp_path, staleness reset
  (f) classify_disposition: deadline appears in result when tag present
  (g) scan --since, DEFAULT_ACTIONABLE_TYPES includes blocker

g-115-12248-b.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path setup -- import board_triage from core/scripts/
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import board_triage  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def pin_storage_backend(monkeypatch):
    """Pin STORAGE_BACKEND=local for every test."""
    monkeypatch.setenv("STORAGE_BACKEND", "local")


@pytest.fixture()
def local_roster():
    """A small local roster for testing."""
    return {"alpha", "bravo", "zeta"}


@pytest.fixture()
def budget_dir(tmp_path, monkeypatch):
    """Monkeypatch _budget_path to use a tmp directory."""
    budget_file = tmp_path / "board-triage-budget.json"

    def _patched_budget_path(agent):
        return budget_file

    monkeypatch.setattr(board_triage, "_budget_path", _patched_budget_path)
    return budget_file


def _make_post(msg_id, author, ptype="status", text="", tags=None,
               reply_to=None):
    """Build a minimal board post dict."""
    p = {
        "id": msg_id,
        "author": author,
        "type": ptype,
        "text": text,
    }
    if tags:
        p["tags"] = tags
    if reply_to:
        p["reply_to"] = reply_to
    return p


# ---------------------------------------------------------------------------
# IO stub that records side effects
# ---------------------------------------------------------------------------

class IORecorder:
    """Replaces the real IO dict for testing."""

    def __init__(self, existing_goals=None, file_goal_result="auto"):
        self.handled = []      # (channel, msg_id)
        self.replies = []      # (channel, reply_to, text)
        self.goals_filed = []  # goal_body dicts
        self.notes = []        # (goal_id, msg_id, text)
        self.events = []       # ordered log of every side effect
        self.existing = dict(existing_goals or {})  # origin_signal -> goal id
        self.file_goal_result = file_goal_result    # "auto" or None (refused)

    def mark_handled(self, channel, msg_id):
        self.events.append("handled")
        self.handled.append((channel, msg_id))

    def post_reply(self, channel, reply_to, text):
        self.events.append("reply")
        self.replies.append((channel, reply_to, text))

    def file_goal(self, body):
        self.events.append("file_goal")
        self.goals_filed.append(body)
        if self.file_goal_result is None:
            return None
        return "g-test-{}".format(len(self.goals_filed))

    def find_goal(self, origin):
        self.events.append("find_goal")
        return self.existing.get(origin)

    def note_goal(self, goal_id, msg_id, text):
        self.events.append("note")
        self.notes.append((goal_id, msg_id, text))

    def as_dict(self):
        return {
            "mark_handled": self.mark_handled,
            "post_reply": self.post_reply,
            "file_goal": self.file_goal,
            "find_goal": self.find_goal,
            "note_goal": self.note_goal,
        }


# ---------------------------------------------------------------------------
# (a) is_peer_author: @env comparison with ENVIRONMENT_ID
# ---------------------------------------------------------------------------

class TestIsPeerAuthor:
    """is_peer_author compares @env with ENVIRONMENT_ID, unknown bare -> PEER."""

    def test_qualified_same_env_is_local(self, local_roster):
        """agent@local-env where env matches ENVIRONMENT_ID -> local."""
        assert not board_triage.is_peer_author(
            "alpha@my-env", local_roster, environment_id="my-env")

    def test_qualified_different_env_is_peer(self, local_roster):
        """agent@other-env where env differs -> peer."""
        assert board_triage.is_peer_author(
            "alpha@other-env", local_roster, environment_id="my-env")

    def test_qualified_no_local_env_is_peer(self, local_roster):
        """agent@env with no local ENVIRONMENT_ID -> peer (conservative)."""
        assert board_triage.is_peer_author(
            "agent-x@some-env", local_roster, environment_id=None)

    def test_bare_name_in_roster_is_local(self, local_roster):
        """A bare name present in the local roster -> local."""
        assert not board_triage.is_peer_author("alpha", local_roster)

    def test_bare_name_not_in_roster_is_peer(self, local_roster):
        """A bare name NOT in the roster -> PEER (conservative)."""
        assert board_triage.is_peer_author("unknown-agent", local_roster)

    def test_bare_name_case_insensitive(self, local_roster):
        """Roster comparison is case-insensitive."""
        assert not board_triage.is_peer_author("Alpha", local_roster)


# ---------------------------------------------------------------------------
# (b) extract_deadline: expires:<ISO> tag parsing
# ---------------------------------------------------------------------------

class TestExtractDeadline:
    """extract_deadline parses expires:<ISO> tags."""

    def test_extracts_iso_deadline(self):
        post = _make_post("msg-1", "a", tags=["expires:2026-12-31T23:59:59"])
        assert board_triage.extract_deadline(post) == "2026-12-31T23:59:59"

    def test_no_expires_tag_returns_none(self):
        post = _make_post("msg-2", "a", tags=["requires_action_by:zeta"])
        assert board_triage.extract_deadline(post) is None

    def test_no_tags_returns_none(self):
        post = _make_post("msg-3", "a")
        assert board_triage.extract_deadline(post) is None

    def test_empty_expires_value_returns_none(self):
        post = _make_post("msg-4", "a", tags=["expires:"])
        assert board_triage.extract_deadline(post) is None

    def test_case_insensitive_prefix(self):
        post = _make_post("msg-5", "a", tags=["Expires:2026-06-15"])
        assert board_triage.extract_deadline(post) == "2026-06-15"


# ---------------------------------------------------------------------------
# (c) dispose DO_NOW: post_reply called with reply_to=msg_id
# ---------------------------------------------------------------------------

class TestDisposeDONOW:
    """dispose() for DO_NOW calls mark_handled and post_reply."""

    def test_do_now_calls_post_reply_with_reply_to(self):
        """DO_NOW with reply_text calls post_reply(channel, msg_id, text)."""
        post = _make_post("msg-10", "bravo", ptype="request")
        disp = {"disposition": "DO_NOW"}
        io = IORecorder()
        result = board_triage.dispose(
            post, disp, io.as_dict(),
            reply_text="done, see attached",
        )
        assert ("coordination", "msg-10") in io.handled
        assert len(io.replies) == 1
        assert io.replies[0] == ("coordination", "msg-10", "done, see attached")
        assert result["disposition"] == "DO_NOW"
        assert "posted reply" in result["actions"]

    def test_do_now_with_neither_reply_nor_note_is_refused(self):
        """A DO NOW that does nothing is an FYI: refused, nothing marked."""
        post = _make_post("msg-11", "bravo", ptype="request")
        io = IORecorder()
        with pytest.raises(board_triage.DisposeError):
            board_triage.dispose(post, {"disposition": "DO_NOW"}, io.as_dict())
        assert io.events == []

    def test_do_now_goal_note_then_reply_then_receipt(self):
        """A DO NOW may note a goal and reply; the receipt comes LAST."""
        post = _make_post("msg-12", "bravo", ptype="request")
        io = IORecorder()
        board_triage.dispose(post, {"disposition": "DO_NOW"}, io.as_dict(),
                             reply_text="noted on g-1", note_goal="g-1",
                             note_text="bravo asked X")
        assert io.events == ["note", "reply", "handled"]
        assert io.notes == [("g-1", "msg-12", "bravo asked X")]

    def test_failed_reply_leaves_the_post_unhandled(self):
        """If the reply step raises, no receipt is written."""
        post = _make_post("msg-13", "bravo", ptype="request")
        io = IORecorder()

        def boom(channel, reply_to, text):
            raise OSError("board-post down")
        d = io.as_dict()
        d["post_reply"] = boom
        with pytest.raises(OSError):
            board_triage.dispose(post, {"disposition": "DO_NOW"}, d,
                                 reply_text="x")
        assert io.handled == []


# ---------------------------------------------------------------------------
# (d) dispose GOAL: file_goal called with origin_signal + resolves_by
# ---------------------------------------------------------------------------

class TestDisposeGOAL:
    """dispose() for GOAL calls file_goal with the right body."""

    def test_new_goal_is_filed_then_replied_naming_it_then_receipt(self):
        """A new goal: look up by origin, file, reply naming the id, receipt."""
        post = _make_post("msg-20", "agent-x@peer-env", ptype="directive",
                          text="Run the weekly report")
        body = {"title": "Weekly report", "origin_signal": "board_post:msg-20",
                "resolves_by": "2026-12-31T00:00:00"}
        io = IORecorder()
        result = board_triage.dispose(post, {"disposition": "GOAL"},
                                      io.as_dict(), goal_body=body)
        assert io.events == ["find_goal", "file_goal", "reply", "handled"]
        assert io.goals_filed[0]["resolves_by"] == "2026-12-31T00:00:00"
        assert result["goal_id"] == "g-test-1"
        # The reply goes to the post it answers and names the goal.
        assert io.replies[0][1] == "msg-20"
        assert "g-test-1" in io.replies[0][2]

    def test_reply_placeholder_is_replaced_by_the_goal_id(self):
        post = _make_post("msg-21", "agent-x@peer-env", ptype="request")
        io = IORecorder()
        board_triage.dispose(post, {"disposition": "GOAL"}, io.as_dict(),
                             goal_body={"title": "T"},
                             reply_text="Filed as {goal}, owner assigned.")
        assert io.replies[0][2] == "Filed as g-test-1, owner assigned."

    def test_existing_goal_with_same_origin_is_reused_not_refiled(self):
        """A retry after a partial failure must not file a duplicate."""
        post = _make_post("msg-22", "agent-x@peer-env", ptype="directive")
        io = IORecorder(existing_goals={"board_post:msg-22": "g-77-01"})
        result = board_triage.dispose(post, {"disposition": "GOAL"},
                                      io.as_dict(), goal_body={"title": "T"})
        assert io.goals_filed == []
        assert result["goal_id"] == "g-77-01"
        assert "g-77-01" in io.replies[0][2]
        assert io.handled == [("coordination", "msg-22")]

    def test_goal_not_filed_leaves_post_unhandled(self):
        """A refused filing raises, posts no reply and writes no receipt."""
        post = _make_post("msg-23", "agent-x@peer-env", ptype="directive")
        io = IORecorder(file_goal_result=None)
        with pytest.raises(board_triage.DisposeError):
            board_triage.dispose(post, {"disposition": "GOAL"}, io.as_dict(),
                                 goal_body={"title": "T"})
        assert io.replies == [] and io.handled == []

    def test_covers_notes_the_existing_goal_and_files_nothing(self):
        post = _make_post("msg-24", "bravo", ptype="request")
        io = IORecorder()
        result = board_triage.dispose(post, {"disposition": "GOAL"},
                                      io.as_dict(), covers="g-5-5")
        assert io.goals_filed == [] and io.replies == []
        assert io.notes[0][:2] == ("g-5-5", "msg-24")
        assert io.events == ["note", "handled"]
        assert result["goal_id"] == "g-5-5"

    def test_goal_without_covers_or_body_is_refused(self):
        post = _make_post("msg-25", "bravo", ptype="request")
        io = IORecorder()
        with pytest.raises(board_triage.DisposeError):
            board_triage.dispose(post, {"disposition": "GOAL"}, io.as_dict())
        assert io.events == []


# ---------------------------------------------------------------------------
# (e) budget persistence: write/read/reset, staleness
# ---------------------------------------------------------------------------

class TestBudgetPersistence:
    """Budget is persisted to JSON and resets correctly."""

    def test_write_and_read(self, budget_dir):
        board_triage.write_budget("zeta", {
            "iteration_id": "iter-1",
            "do_now_used": 2,
            "updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        })
        data = board_triage.read_budget("zeta")
        assert data["do_now_used"] == 2
        assert data["iteration_id"] == "iter-1"

    def test_reset_budget(self, budget_dir):
        board_triage.write_budget("zeta", {
            "iteration_id": "old",
            "do_now_used": 3,
            "updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        })
        data = board_triage.reset_budget("zeta", "iter-new")
        assert data["do_now_used"] == 0
        assert data["iteration_id"] == "iter-new"
        # Verify persisted.
        reread = board_triage.read_budget("zeta")
        assert reread["do_now_used"] == 0

    def test_staleness_reset(self, budget_dir):
        """Budget >2h old resets to empty on read."""
        old_ts = (
            datetime.now() - timedelta(hours=3)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        board_triage.write_budget("zeta", {
            "iteration_id": "old",
            "do_now_used": 3,
            "updated": old_ts,
        })
        data = board_triage.read_budget("zeta")
        assert data["do_now_used"] == 0  # safety-net reset

    def test_missing_file_returns_empty(self, budget_dir):
        data = board_triage.read_budget("zeta")
        assert data["do_now_used"] == 0

    def test_bump_and_budget_left(self, budget_dir):
        board_triage.reset_budget("zeta", "iter-1")
        assert board_triage.budget_left("zeta") == 3
        assert board_triage.bump_budget("zeta") == 1
        assert board_triage.bump_budget("zeta") == 2
        assert board_triage.budget_left("zeta") == 1
        assert board_triage.bump_budget("zeta") == 3
        assert board_triage.budget_left("zeta") == 0
        # The count survives between calls: it is read back from the file.
        assert json.loads(budget_dir.read_text())["do_now_used"] == 3


# ---------------------------------------------------------------------------
# (f) classify_disposition: deadline in result when tag present
# ---------------------------------------------------------------------------

class TestClassifyDeadline:
    """classify_disposition includes deadline from expires:<ISO> tag."""

    def test_deadline_in_result(self, local_roster):
        post = _make_post("msg-30", "bravo", ptype="request",
                          tags=["requires_action_by:zeta",
                                "expires:2026-12-25T10:00:00"])
        disp = board_triage.classify_disposition(
            post, "requires_action_by", do_now_remaining=3,
            local_roster=local_roster,
        )
        assert disp["deadline"] == "2026-12-25T10:00:00"

    def test_no_deadline_no_key(self, local_roster):
        post = _make_post("msg-31", "bravo", ptype="request",
                          tags=["requires_action_by:zeta"])
        disp = board_triage.classify_disposition(
            post, "requires_action_by", do_now_remaining=3,
            local_roster=local_roster,
        )
        assert "deadline" not in disp

    def test_peer_goal_with_deadline(self, local_roster):
        """Peer work ask -> GOAL, deadline carried through."""
        post = _make_post("msg-32", "agent-x@peer-env", ptype="directive",
                          tags=["expires:2026-07-01T00:00:00"])
        disp = board_triage.classify_disposition(
            post, "requires_action_by", do_now_remaining=3,
            local_roster=local_roster, environment_id="my-env",
        )
        assert disp["disposition"] == "GOAL"
        assert disp["deadline"] == "2026-07-01T00:00:00"


# ---------------------------------------------------------------------------
# (g) scan --since, DEFAULT_ACTIONABLE_TYPES includes blocker
# ---------------------------------------------------------------------------

class TestActionableTypes:
    """ACTIONABLE_TYPES reuses DEFAULT_ACTIONABLE_TYPES and includes blocker."""

    def test_blocker_is_actionable(self):
        """The 'blocker' type is in ACTIONABLE_TYPES."""
        assert "blocker" in board_triage.ACTIONABLE_TYPES

    def test_all_default_types_present(self):
        """All types from DEFAULT_ACTIONABLE_TYPES are in ACTIONABLE_TYPES."""
        from generation_phase_gate import DEFAULT_ACTIONABLE_TYPES as canonical
        for t in canonical:
            assert t in board_triage.ACTIONABLE_TYPES

    def test_blocker_post_classified_as_actionable(self, local_roster):
        """A blocker post addressed to this agent -> DO_NOW (not FYI)."""
        post = _make_post("msg-40", "bravo", ptype="blocker",
                          tags=["requires_action_by:zeta"])
        disp = board_triage.classify_disposition(
            post, "requires_action_by", do_now_remaining=3,
            local_roster=local_roster,
        )
        assert disp["disposition"] == "DO_NOW"


# ---------------------------------------------------------------------------
# Test: FYI post becomes HANDLED with no reply (verification outcome 1)
# ---------------------------------------------------------------------------

class TestFYIPost:
    """An FYI post is marked HANDLED with no reply."""

    def test_fyi_non_actionable_type_handled_no_reply(self, local_roster):
        """A status post addressed to this agent -> FYI, marked handled,
        no reply posted."""
        post = _make_post("msg-100", "bravo", ptype="status",
                          text="all good here", tags=["zeta"])
        posts = [post]
        results = board_triage.scan(posts, "zeta", set())
        assert len(results) == 1

        disp = board_triage.classify_disposition(
            post, "bare-agent-tag", do_now_remaining=3,
            local_roster=local_roster,
        )
        assert disp["disposition"] == "FYI"

        io = IORecorder()
        result = board_triage.dispose(post, disp, io.as_dict())
        assert ("coordination", "msg-100") in io.handled
        assert len(io.replies) == 0
        assert len(io.goals_filed) == 0
        assert result["disposition"] == "FYI"
        assert io.events == ["handled"]

    def test_status_post_with_a_demand_tag_is_an_ask_not_an_fyi(
            self, local_roster):
        """A status-shaped post carrying requires_action_by is an explicit
        ask, so it is DO NOW eligible rather than silently FYI."""
        post = _make_post("msg-101", "bravo", ptype="status",
                          tags=["requires_action_by:zeta"])
        disp = board_triage.classify_disposition(
            post, "requires_action_by", do_now_remaining=3,
            local_roster=local_roster)
        assert disp["disposition"] == "DO_NOW"


# ---------------------------------------------------------------------------
# Test: DO NOW budget (verification outcome 2)
# ---------------------------------------------------------------------------

class TestDONOWBudget:
    """DO NOW counts toward the budget; a fourth ask stays unhandled."""

    def test_do_now_decrements_budget(self, local_roster):
        post = _make_post("msg-110", "bravo", ptype="request",
                          tags=["requires_action_by:zeta"])
        disp = board_triage.classify_disposition(
            post, "requires_action_by", do_now_remaining=3,
            local_roster=local_roster,
        )
        assert disp["disposition"] == "DO_NOW"
        assert disp["do_now_remaining"] == 2

    def test_fourth_do_now_stays_unhandled(self, local_roster):
        posts = [
            _make_post("msg-{}".format(i), "bravo", ptype="request",
                       tags=["requires_action_by:zeta"])
            for i in range(120, 124)
        ]
        remaining = 3
        handled_count = 0
        over_budget_ids = []

        for p in posts:
            disp = board_triage.classify_disposition(
                p, "requires_action_by", do_now_remaining=remaining,
                local_roster=local_roster,
            )
            remaining = disp["do_now_remaining"]
            if disp["disposition"] == "DO_NOW":
                handled_count += 1
            elif disp["disposition"] == "OVER_BUDGET":
                over_budget_ids.append(p["id"])

        assert handled_count == 3
        assert over_budget_ids == ["msg-123"]

    def test_over_budget_not_marked_handled(self, local_roster):
        post = _make_post("msg-130", "bravo", ptype="request",
                          tags=["requires_action_by:zeta"])
        disp = board_triage.classify_disposition(
            post, "requires_action_by", do_now_remaining=0,
            local_roster=local_roster,
        )
        assert disp["disposition"] == "OVER_BUDGET"

        io = IORecorder()
        result = board_triage.dispose(post, disp, io.as_dict())
        assert len(io.handled) == 0
        assert "left unhandled" in result["actions"][0]

    def test_over_budget_handled_next_iteration(self, local_roster):
        """Fourth ask stays unhandled in iteration 1,
        handled in iteration 2 with a fresh budget."""
        posts = [
            _make_post("msg-14{}".format(i), "bravo", ptype="request",
                       tags=["requires_action_by:zeta"])
            for i in range(4)
        ]

        remaining = 3
        it1_over = []
        for p in posts:
            disp = board_triage.classify_disposition(
                p, "requires_action_by", do_now_remaining=remaining,
                local_roster=local_roster,
            )
            remaining = disp["do_now_remaining"]
            if disp["disposition"] == "OVER_BUDGET":
                it1_over.append(p)

        assert len(it1_over) == 1

        # Iteration 2: fresh budget.
        disp2 = board_triage.classify_disposition(
            it1_over[0], "requires_action_by", do_now_remaining=3,
            local_roster=local_roster,
        )
        assert disp2["disposition"] == "DO_NOW"


# ---------------------------------------------------------------------------
# Test: Peer work ask -> GOAL (verification outcome 3)
# ---------------------------------------------------------------------------

class TestPeerWorkAsk:
    """A work ask from another deployment becomes a GOAL."""

    def test_peer_directive_becomes_goal(self, local_roster):
        post = _make_post("msg-150", "service-a@peer-deployment",
                          ptype="directive",
                          text="Please run the weekly report")
        disp = board_triage.classify_disposition(
            post, "requires_action_by", do_now_remaining=3,
            local_roster=local_roster,
        )
        assert disp["disposition"] == "GOAL"
        assert disp["is_peer"] is True

    def test_peer_request_becomes_goal(self, local_roster):
        post = _make_post("msg-151", "agent-b@peer-env",
                          ptype="request",
                          text="Can you check the pipeline?")
        disp = board_triage.classify_disposition(
            post, "first-line-address", do_now_remaining=3,
            local_roster=local_roster,
        )
        assert disp["disposition"] == "GOAL"
        assert disp["is_peer"] is True


# ---------------------------------------------------------------------------
# Test: code/money/production/owner-approval refused as DO NOW (outcome 3)
# ---------------------------------------------------------------------------

class TestNeverDONOW:
    """An ask that touches code, money, production or owner approval is
    refused as DO NOW."""

    def test_code_touch_becomes_goal(self, local_roster):
        post = _make_post("msg-160", "bravo", ptype="request",
                          tags=["requires_action_by:zeta"])
        disp = board_triage.classify_disposition(
            post, "requires_action_by", do_now_remaining=3,
            touches={"code"}, local_roster=local_roster,
        )
        assert disp["disposition"] == "GOAL"
        assert "code" in disp["note"]
        assert disp["do_now_remaining"] == 3  # budget not consumed

    def test_money_touch_becomes_goal(self, local_roster):
        post = _make_post("msg-161", "bravo", ptype="request",
                          tags=["requires_action_by:zeta"])
        disp = board_triage.classify_disposition(
            post, "requires_action_by", do_now_remaining=3,
            touches={"money"}, local_roster=local_roster,
        )
        assert disp["disposition"] == "GOAL"

    def test_production_touch_becomes_goal(self, local_roster):
        post = _make_post("msg-162", "bravo", ptype="directive",
                          tags=["requires_action_by:zeta"])
        disp = board_triage.classify_disposition(
            post, "requires_action_by", do_now_remaining=3,
            touches={"production"}, local_roster=local_roster,
        )
        assert disp["disposition"] == "GOAL"


# ---------------------------------------------------------------------------
# Test: scan addressing logic
# ---------------------------------------------------------------------------

class TestScan:
    """Scan picks up the right posts."""

    def test_scan_requires_action_by(self):
        posts = [
            _make_post("msg-170", "alpha", ptype="request",
                       tags=["requires_action_by:zeta"]),
            _make_post("msg-171", "alpha", ptype="request",
                       tags=["requires_action_by:bravo"]),
        ]
        results = board_triage.scan(posts, "zeta")
        assert len(results) == 1
        assert results[0][0]["id"] == "msg-170"

    def test_scan_everyone_no_routing_tag(self):
        post = _make_post("msg-172", "alpha", ptype="request",
                          text="all agents please note")
        results = board_triage.scan([post], "zeta")
        assert len(results) == 1
        assert results[0][1] == "everyone-no-tag"

    def test_scan_skips_own_posts(self):
        post = _make_post("msg-173", "zeta", ptype="request",
                          tags=["requires_action_by:zeta"])
        results = board_triage.scan([post], "zeta")
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Test: dispose side effects
# ---------------------------------------------------------------------------

class TestDispose:
    """Dispose records the right side effects."""

    def test_fyi_dispose_marks_handled(self):
        post = _make_post("msg-180", "alpha", ptype="status")
        disp = {"disposition": "FYI"}
        io = IORecorder()
        result = board_triage.dispose(post, disp, io.as_dict())
        assert ("coordination", "msg-180") in io.handled
        assert result["disposition"] == "FYI"

    def test_over_budget_does_not_mark_handled(self):
        post = _make_post("msg-181", "alpha", ptype="request")
        disp = {"disposition": "OVER_BUDGET"}
        io = IORecorder()
        board_triage.dispose(post, disp, io.as_dict())
        assert len(io.handled) == 0

    def test_noted_not_filing_marks_handled(self):
        post = _make_post("msg-182", "alpha", ptype="request")
        disp = {"disposition": "NOTED_NOT_FILING"}
        io = IORecorder()
        board_triage.dispose(post, disp, io.as_dict(),
                             reason="someday: not this quarter")
        assert ("coordination", "msg-182") in io.handled

    def test_noted_not_filing_needs_a_reason(self):
        post = _make_post("msg-183", "alpha", ptype="request")
        io = IORecorder()
        with pytest.raises(board_triage.DisposeError):
            board_triage.dispose(post, {"disposition": "NOTED_NOT_FILING"},
                                 io.as_dict(), reason="  ")
        assert io.events == []


# ---------------------------------------------------------------------------
# (h) A PLANTED board in a tmp world: real HANDLED receipts, never the live one
# ---------------------------------------------------------------------------

@pytest.fixture()
def planted_board(tmp_path, monkeypatch):
    """board.BOARD_DIR pointed at a tmp dir; exposes the REAL receipt writer."""
    import board
    board_dir = tmp_path / "board"
    board_dir.mkdir()
    monkeypatch.setattr(board, "BOARD_DIR", board_dir)

    class Planted:
        channel = "coordination"

        def mark_handled(self, channel, msg_id):
            board._append_receipts(channel, "zeta", "sid-test", [msg_id],
                                   "handled")

        def handled_ids(self):
            return board._load_read_msg_ids(self.channel, "zeta", "handled")

        def rows(self):
            path = board.reads_sidecar_path(self.channel)
            if not path.exists():
                return []
            return [json.loads(ln) for ln in path.read_text().splitlines()
                    if ln.strip()]

    return Planted()


def _run_dispose(monkeypatch, planted, rec, post, flags, diaries=None):
    """Drive the real dispose CLI path; the goal/reply/note seams are the
    recorder's, the HANDLED receipt is the planted board's real writer."""
    io = rec.as_dict()
    io["mark_handled"] = planted.mark_handled
    monkeypatch.setattr(board_triage, "_read_post_by_id",
                        lambda channel, msg_id, since: post)
    monkeypatch.setattr(board_triage, "_real_io", lambda: io)
    monkeypatch.setattr(board_triage, "_get_local_roster",
                        lambda: {"alpha", "bravo", "zeta"})
    monkeypatch.setattr(board_triage, "_get_environment_id",
                        lambda: "local-env")
    log = [] if diaries is None else diaries
    monkeypatch.setattr(board_triage, "_append_diary",
                        lambda *a, **k: log.append(a) or True)
    args = board_triage._build_parser().parse_args(
        ["dispose", "--msg-id", post["id"]] + flags)
    try:
        board_triage._cmd_dispose(args, "zeta")
    except SystemExit as exc:
        return exc.code
    return 0


class TestPlantedBoard:
    """End to end over a planted tmp board: the receipts are real rows."""

    def test_fyi_writes_one_handled_receipt_and_no_reply(
            self, planted_board, monkeypatch):
        post = _make_post("msg-p1", "bravo", ptype="status", tags=["zeta"])
        rec = IORecorder()
        assert _run_dispose(monkeypatch, planted_board, rec, post,
                            ["--disposition", "FYI"]) == 0
        rows = planted_board.rows()
        assert [(r["msg_id"], r["reader_agent"], r.get("kind"))
                for r in rows] == [("msg-p1", "zeta", "handled")]
        assert rec.replies == []

    def test_fourth_do_now_waits_and_is_handled_next_iteration(
            self, planted_board, budget_dir, tmp_path, monkeypatch):
        reply = tmp_path / "r.txt"
        reply.write_text("answered from the records")
        flags = ["--disposition", "DO_NOW", "--touches", "records",
                 "--reply-file", str(reply)]
        board_triage.reset_budget("zeta", "it-1")
        posts = [_make_post("msg-q{}".format(i), "bravo", ptype="request",
                            tags=["requires_action_by:zeta"])
                 for i in range(4)]
        codes, recs, diaries = [], [], []
        for p in posts:
            rec = IORecorder()
            recs.append(rec)
            codes.append(_run_dispose(monkeypatch, planted_board, rec, p,
                                      flags, diaries))
        assert codes == [0, 0, 0, 3]
        # The answer went out with --reply-to the post; the fourth did nothing.
        assert recs[0].replies[0][:2] == ("coordination", "msg-q0")
        assert recs[3].replies == []
        assert planted_board.handled_ids() == {"msg-q0", "msg-q1", "msg-q2"}
        assert len(diaries) == 3          # one diary line per DO NOW
        assert board_triage.budget_left("zeta") == 0
        # The next iteration (scan --begin-iteration) refills the budget.
        board_triage.reset_budget("zeta", "it-2")
        assert _run_dispose(monkeypatch, planted_board, IORecorder(),
                            posts[3], flags, diaries) == 0
        assert "msg-q3" in planted_board.handled_ids()

    def test_peer_work_ask_becomes_goal_with_deadline_and_named_reply(
            self, planted_board, tmp_path, monkeypatch):
        desc = tmp_path / "d.txt"
        desc.write_text("Do the thing the peer asked for.")
        post = _make_post("msg-peer1", "agent-x@other-env", ptype="directive",
                          tags=["requires_action_by:zeta",
                                "expires:2026-11-01T00:00:00"])
        rec = IORecorder()
        assert _run_dispose(
            monkeypatch, planted_board, rec, post,
            ["--disposition", "GOAL", "--title", "Peer ask",
             "--description-file", str(desc), "--aspiration", "asp-1"]) == 0
        body = rec.goals_filed[0]
        assert body["origin_signal"] == "board_post:msg-peer1"
        assert body["resolves_by"] == "2026-11-01T00:00:00"
        assert body["aspiration"] == "asp-1" and body["description"]
        assert rec.replies[0][1] == "msg-peer1"
        assert "g-test-1" in rec.replies[0][2]
        assert planted_board.handled_ids() == {"msg-peer1"}

    def test_peer_work_ask_is_never_do_now_fyi_or_noted(
            self, planted_board, budget_dir, monkeypatch):
        post = _make_post("msg-peer2", "agent-x@other-env", ptype="request")
        for flags in (["--disposition", "DO_NOW", "--touches", "records"],
                      ["--disposition", "FYI"],
                      ["--disposition", "NOTED_NOT_FILING", "--reason", "x"]):
            assert _run_dispose(monkeypatch, planted_board, IORecorder(),
                                post, flags) == 2
        assert planted_board.handled_ids() == set()

    @pytest.mark.parametrize("touch", ["code", "money", "production",
                                       "owner-approval"])
    def test_touching_code_money_production_approval_refuses_do_now(
            self, touch, planted_board, budget_dir, tmp_path, monkeypatch):
        reply = tmp_path / "r.txt"
        reply.write_text("x")
        board_triage.reset_budget("zeta", "it-1")
        post = _make_post("msg-t1", "bravo", ptype="request",
                          tags=["requires_action_by:zeta"])
        assert _run_dispose(monkeypatch, planted_board, IORecorder(), post,
                            ["--disposition", "DO_NOW", "--touches", touch,
                             "--reply-file", str(reply)]) == 2
        assert planted_board.handled_ids() == set()
        assert board_triage.budget_left("zeta") == 3   # budget not consumed

    def test_flag_validation_exit_codes(self, planted_board, budget_dir,
                                        tmp_path, monkeypatch):
        post = _make_post("msg-v1", "bravo", ptype="request")
        run = lambda flags: _run_dispose(  # noqa: E731
            monkeypatch, planted_board, IORecorder(), post, flags)
        assert run(["--disposition", "DO_NOW"]) == 2                  # no --touches
        assert run(["--disposition", "DO_NOW", "--touches", "typo"]) == 2
        assert run(["--disposition", "GOAL"]) == 2                    # no target
        assert run(["--disposition", "GOAL", "--covers", "g-1-1",
                    "--title", "T"]) == 2                              # two targets
        assert run(["--disposition", "NOTED_NOT_FILING"]) == 2        # no reason
        assert planted_board.handled_ids() == set()


# ---------------------------------------------------------------------------
# (i) The REAL wrappers: argv and parsing, with subprocess.run recorded
# ---------------------------------------------------------------------------

class TestRealWrappers:
    @staticmethod
    def _fake_run(monkeypatch, stdout="", returncode=0):
        calls = []

        class Done:
            pass

        def fake(cmd, **kw):
            calls.append((cmd, kw))
            done = Done()
            done.stdout, done.stderr, done.returncode = stdout, "", returncode
            return done
        monkeypatch.setattr("subprocess.run", fake)
        return calls

    def test_file_goal_reads_the_id_and_strips_aspiration(self, monkeypatch):
        calls = self._fake_run(monkeypatch, stdout='{"id": "g-9-9"}')
        assert board_triage._real_file_goal(
            {"title": "T", "aspiration": "asp-1"}) == "g-9-9"
        cmd, kw = calls[0]
        assert "aspirations-add-goal.sh" in cmd[1]
        assert cmd[-2:] == ["--aspiration", "asp-1"]
        assert "aspiration" not in json.loads(kw["input"])

    def test_file_goal_refusal_returns_none(self, monkeypatch):
        self._fake_run(monkeypatch, stdout='{"error": "goal_duplication_blocked"}',
                       returncode=1)
        assert board_triage._real_file_goal({"title": "T"}) is None

    def test_post_reply_is_a_reply_to_with_the_text_on_stdin(self, monkeypatch):
        calls = self._fake_run(monkeypatch)
        board_triage._real_post_reply("coordination", "msg-1", "hello")
        cmd, kw = calls[0]
        assert "board-post.sh" in cmd[1]
        assert cmd[cmd.index("--reply-to") + 1] == "msg-1"
        assert kw["input"] == "hello"

    def test_note_goal_marker_comes_from_the_message_id(self, monkeypatch):
        calls = self._fake_run(monkeypatch)
        board_triage._real_note_goal("g-1-1", "msg-2026/10", "a note")
        cmd, kw = calls[0]
        assert "goal-field-append.sh" in cmd[1]
        assert cmd[-6:] == ["--source", "world", "g-1-1", "progress_note",
                            "board-msg-2026-10", "--value-stdin"]
        assert kw["input"] == "a note"

    def test_find_goal_matches_on_origin_signal(self, monkeypatch):
        self._fake_run(monkeypatch, stdout=json.dumps([
            {"id": "g-3-3", "origin_signal": "board_post:msg-9"}]))
        assert board_triage._real_find_goal("board_post:msg-9") == "g-3-3"
        self._fake_run(monkeypatch, stdout="[]")
        assert board_triage._real_find_goal("board_post:msg-9") is None


# ---------------------------------------------------------------------------
# (j) Gaps a fresh-eyes mutation pass found (-b): each test below
# fails against the named mutation of board_triage.py.
# ---------------------------------------------------------------------------

class FailingReplyRecorder(IORecorder):
    """Records the reply attempt, then fails it like a refused board post."""

    def post_reply(self, channel, reply_to, text):
        super().post_reply(channel, reply_to, text)
        raise board_triage.DisposeError("reply refused")


class TestPlantedBoardFailures:
    def test_failed_do_now_leaves_post_unhandled_and_budget_untouched(
            self, planted_board, budget_dir, tmp_path, monkeypatch):
        # Mutation: bump_budget before the action instead of after success.
        reply = tmp_path / "r.txt"
        reply.write_text("answered from the records")
        flags = ["--disposition", "DO_NOW", "--touches", "records",
                 "--reply-file", str(reply)]
        board_triage.reset_budget("zeta", "it-1")
        post = _make_post("msg-f1", "bravo", ptype="request",
                          tags=["requires_action_by:zeta"])
        diaries = []
        assert _run_dispose(monkeypatch, planted_board, FailingReplyRecorder(),
                            post, flags, diaries) == 1
        assert planted_board.handled_ids() == set()
        assert board_triage.budget_left("zeta") == 3   # nothing consumed
        assert diaries == []                           # no diary line either
        # The retry that succeeds consumes exactly one.
        assert _run_dispose(monkeypatch, planted_board, IORecorder(), post,
                            flags, diaries) == 0
        assert board_triage.budget_left("zeta") == 2
        assert len(diaries) == 1

    def test_peer_question_is_answered_do_now_not_forced_to_goal(
            self, planted_board, budget_dir, tmp_path, monkeypatch):
        # Mutation: every question counts as a work ask.
        reply = tmp_path / "r.txt"
        reply.write_text("answered from the records")
        board_triage.reset_budget("zeta", "it-1")
        post = _make_post("msg-pq", "agent-x@other-env", ptype="question",
                          tags=["requires_action_by:zeta"])
        rec = IORecorder()
        assert _run_dispose(
            monkeypatch, planted_board, rec, post,
            ["--disposition", "DO_NOW", "--touches", "records",
             "--reply-file", str(reply)]) == 0
        assert rec.replies[0][1] == "msg-pq"
        assert planted_board.handled_ids() == {"msg-pq"}


class TestIsWorkAsk:
    @pytest.mark.parametrize("ptype", [
        "request", "directive", "blocker", "decision-needed", "escalation",
        "review-request"])
    def test_work_ask_types_are_work_asks(self, ptype):
        assert board_triage.is_work_ask(_make_post("m", "a", ptype=ptype))

    def test_a_question_is_not_a_work_ask_even_with_a_demand_tag(self):
        post = _make_post("m", "a", ptype="question",
                          tags=["requires_action_by:zeta"])
        assert board_triage.is_work_ask(post) is False

    def test_a_status_post_is_a_work_ask_only_with_a_demand_tag(self):
        demand = _make_post("m", "a", ptype="status",
                            tags=["requires_action_by:zeta"])
        assert board_triage.is_work_ask(demand) is True
        assert board_triage.is_work_ask(_make_post("m", "a", ptype="status")) \
            is False


class TestAppendDiary:
    def test_entry_is_one_decision_line_with_the_goal_id(self, monkeypatch):
        # Mutation: the real function raises (every other test stubs it).
        calls = TestRealWrappers._fake_run(monkeypatch)
        assert board_triage._append_diary(
            "msg-9", "DO_NOW", "replied; noted", "g-1-1") is True
        cmd, kw = calls[0]
        assert "execution-diary.sh" in cmd[1] and cmd[-1] == "append"
        assert json.loads(kw["input"]) == {
            "entry_type": "decision", "goal_id": "g-1-1",
            "content": "board-disposition DO_NOW msg-9: replied; noted"}

    def test_goal_id_is_omitted_and_the_text_is_ascii(self, monkeypatch):
        calls = TestRealWrappers._fake_run(monkeypatch)
        board_triage._append_diary(
            "msg-9", "NOTED_NOT_FILING", "café  spaced\nout")
        entry = json.loads(calls[0][1]["input"])
        assert "goal_id" not in entry
        assert entry["content"] == \
            "board-disposition NOTED_NOT_FILING msg-9: caf? spaced out"

    def test_a_failing_append_warns_on_stderr_and_returns_false(
            self, monkeypatch, capsys):
        TestRealWrappers._fake_run(monkeypatch, returncode=1)
        assert board_triage._append_diary("msg-9", "DO_NOW") is False
        assert "execution-diary append failed (rc=1)" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# (k) Defects the same fresh-eyes pass found in the engine itself
# ---------------------------------------------------------------------------

class TestBudgetBump:
    def test_concurrent_bumps_are_all_counted(self, budget_dir):
        # Mutation: bump_budget reads outside the lock (a lost update).
        import threading
        board_triage.reset_budget("zeta", "it-1")
        workers, each = 6, 4
        gate = threading.Barrier(workers)
        errors = []

        def work():
            try:
                gate.wait()
                for _ in range(each):
                    board_triage.bump_budget("zeta")
            except Exception as exc:  # reported by the assert below
                errors.append(repr(exc))

        threads = [threading.Thread(target=work) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert board_triage.read_budget("zeta")["do_now_used"] == workers * each

    def test_a_stale_budget_restarts_at_one(self, budget_dir):
        old = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
        board_triage.write_budget(
            "zeta", {"iteration_id": "old", "do_now_used": 3, "updated": old})
        assert board_triage.bump_budget("zeta") == 1

    def test_a_corrupt_budget_file_fails_open(self, budget_dir):
        budget_dir.write_text("{not json")
        assert board_triage.bump_budget("zeta") == 1
        assert json.loads(budget_dir.read_text())["do_now_used"] == 1


class TestDisposeRefusesUnreadableInput:
    """A bad file flag is a refusal (exit 2) before any effect, not a traceback."""

    @pytest.mark.parametrize("flags", [
        ["--disposition", "DO_NOW", "--touches", "records",
         "--reply-file", "MISSING"],
        ["--disposition", "DO_NOW", "--touches", "records",
         "--note-goal", "g-9-9", "--note-file", "MISSING"],
        ["--disposition", "GOAL", "--covers", "g-9-9",
         "--reply-file", "MISSING"],
        ["--disposition", "GOAL", "--covers", "g-9-9",
         "--note-file", "MISSING"],
        ["--disposition", "GOAL", "--file-goal", "MISSING"],
        ["--disposition", "GOAL", "--file-goal", "BADJSON"],
        ["--disposition", "GOAL", "--title", "t", "--aspiration", "asp-1",
         "--description-file", "MISSING"],
    ])
    def test_unreadable_file_flag_is_refused_with_no_effect(
            self, planted_board, budget_dir, tmp_path, monkeypatch, capsys,
            flags):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        subst = {"MISSING": str(tmp_path / "missing.txt"), "BADJSON": str(bad)}
        flags = [subst.get(f, f) for f in flags]
        board_triage.reset_budget("zeta", "it-1")
        post = _make_post("msg-bf", "bravo", ptype="request",
                          tags=["requires_action_by:zeta"])
        rec = IORecorder()
        assert _run_dispose(monkeypatch, planted_board, rec, post, flags) == 2
        assert "cannot read" in capsys.readouterr().err
        assert rec.events == []
        assert planted_board.handled_ids() == set()
        assert board_triage.budget_left("zeta") == 3
