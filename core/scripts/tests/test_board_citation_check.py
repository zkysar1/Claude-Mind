#!/usr/bin/env python3
# domain-leak-exempt: every msg-<date>-<time>-<agent>-<n> literal below is a
# FIXTURE describing the board-id shape, never a functional identifier.
"""Pins for board-citation-check.py (g-115-4405).

The defect: /fresh-eyes-review's encode step wrote a board receipt id that was
never a message into a LIVE tree node. Nothing resolved it, so a plausible-shaped
citation for a nonexistent post sat in a surface readers reach by grep.

These pins read the SHIPPED module (guard-920) rather than re-declaring its
regexes or its loader, so a change to the real file is what they measure.

The three that matter most are the FAIL-SAFE ones, because this tool's dangerous
failure is not missing a dangling citation -- it is reporting every citation as
dangling because the board never loaded, which would send an agent to "fix" 200
correct references. test_board_json_is_read_as_jsonl and
test_empty_board_refuses_to_interpret pin that directly.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pathlib
import sys as _sys
_sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _verify_corpus  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

_spec = importlib.util.spec_from_file_location(
    "board_citation_check", SCRIPT_DIR / "board-citation-check.py")
bcc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bcc)


def _world(tmp_path, messages, channel="findings"):
    """A world whose board carries `messages` as JSONL -- the real on-disk shape."""
    board = tmp_path / "world" / "board"
    board.mkdir(parents=True)
    (board / f"{channel}.jsonl").write_text(
        "".join(json.dumps({"id": m, "channel": channel, "text": "x"}) + "\n"
                for m in messages),
        encoding="utf-8")
    return tmp_path / "world"


def _root(tmp_path, node_text, rel="conventions/probe.md"):
    """A project root with one live surface file."""
    root = tmp_path / "proj"
    p = root / ".mind-data" / "world" / rel  # unused shape; real surfaces come from world
    p.parent.mkdir(parents=True, exist_ok=True)
    (root / "core" / "config").mkdir(parents=True, exist_ok=True)
    (root / "core" / "config" / "probe.md").write_text(node_text, encoding="utf-8")
    return root


# --- the goal's explicit checks ---------------------------------------------

def test_positive_control_bogus_id_is_flagged(tmp_path):
    """A deliberately bogus id must be reported. This is the goal's check 2."""
    world = _world(tmp_path, ["msg-20260801-042100-alpha-5510"])
    root = _root(tmp_path, "see findings post msg-20260801-042738-alpha-611 for detail\n")
    r = bcc.scan(root, world)
    ids = [f["id"] for f in r["dangling"]]
    assert "msg-20260801-042738-alpha-611" in ids, (
        f"the bogus id from the founding incident was not flagged; got {r}")


def test_real_id_is_not_flagged(tmp_path):
    """Discriminating power: a citation that DOES resolve must stay silent.

    Without this the tool could flag everything and still pass the check above.
    """
    real = "msg-20260801-042100-alpha-5510"
    world = _world(tmp_path, [real])
    root = _root(tmp_path, f"see findings post {real} for detail\n")
    r = bcc.scan(root, world)
    assert r["dangling"] == [], f"a resolving citation was reported dangling: {r['dangling']}"
    assert r["citations_seen"] == 1, "the citation must still be COUNTED, only not flagged"


# --- fail-safe: the failure that would be worse than the defect --------------

def test_board_json_is_read_as_jsonl(tmp_path):
    """Board files are ONE OBJECT PER LINE, never a JSON array.

    A whole-file json.load raises on the second line, the id set comes back empty,
    and EVERY citation reads as dangling -- a false mass-negative that would send
    an agent to rewrite correct references. Pinned with >1 message so the
    array-parse assumption cannot pass.
    """
    ids = [f"msg-2026080{i}-0421{i}{i}-alpha-55{i}{i}" for i in range(1, 5)]
    world = _world(tmp_path, ids)
    loaded = bcc.load_board_ids(world)
    assert loaded == set(ids), f"JSONL loader lost ids: expected {set(ids)}, got {loaded}"


def test_empty_board_refuses_to_interpret(tmp_path, capsys):
    """Zero known board ids must not render as 'everything is dangling'.

    An unloadable corpus makes every citation UNEVALUABLE, not false. The CLI
    refuses rather than printing a confident list (the general form of the
    learning-routing mass-null incident: an empty id-set silently licensing mass
    invalidation).
    """
    world = tmp_path / "world"
    (world / "board").mkdir(parents=True)
    root = _root(tmp_path, "cites msg-20260801-042738-alpha-611 here\n")
    rc = bcc.main.__wrapped__ if hasattr(bcc.main, "__wrapped__") else None  # not wrapped
    sys.argv = ["board-citation-check.py", "--root", str(root), "--world", str(world)]
    out_rc = bcc.main()
    captured = capsys.readouterr()
    assert out_rc == 0
    assert "zero board ids loaded" in captured.err, (
        "an empty board must produce an explicit refusal, not a dangling list")
    assert "DANGLING msg-" not in captured.out, "refused runs must not print findings"


# --- classification ----------------------------------------------------------

def test_schema_example_is_not_counted_dangling(tmp_path):
    """A format illustration inside a record shape is not a claim about a post."""
    world = _world(tmp_path, ["msg-20260801-042100-alpha-5510"])
    root = _root(tmp_path, '  "id": "msg-20260326-143000-alpha-001",\n')
    r = bcc.scan(root, world)
    assert r["dangling"] == [], "a schema example must not be reported as dangling"
    assert len(r["examples"]) == 1, f"it must still be REPORTED, as an example: {r}"


def test_exempt_file_is_skipped(tmp_path):
    """A file documenting the id format opts out wholesale."""
    world = _world(tmp_path, ["msg-20260801-042100-alpha-5510"])
    root = _root(tmp_path,
                 "board-citation-exempt: this file documents the id shape\n"
                 "msg-20260801-042738-alpha-611\n")
    r = bcc.scan(root, world)
    assert r["dangling"] == [], "an exempt file must not be scanned"
    assert r["files_scanned"] == 0


def test_hyphenated_agent_name_parses(tmp_path):
    """A hyphenated agent name (meta-tiebreaker is real) must still parse.

    THE RATIONALE THIS TEST SHIPPED WITH WAS WRONG, and the correction is the
    reason it is worth reading. It claimed to pin the regex's LAZY quantifier --
    "a greedy segment would swallow the counter". Mutation-tested: swapping in the
    greedy form breaks NO test and changes no real match, because backtracking
    lands both forms on the same split for every id this board contains. So that
    control was VACUOUS as declared.

    What it actually covers, and does cover: an id whose agent segment contains a
    hyphen is recognised as a citation AND resolves. That is real -- a pattern
    stopping at the first hyphen would report every meta-tiebreaker citation as
    dangling. Retitled to claim only that.
    """
    real = "msg-20260801-042738-meta-tiebreaker-611"
    world = _world(tmp_path, [real])
    root = _root(tmp_path, f"cites {real}\n")
    r = bcc.scan(root, world)
    assert r["citations_seen"] == 1, "hyphenated-agent id was not recognised as a citation"
    assert r["dangling"] == [], f"hyphenated-agent id failed to resolve: {r['dangling']}"


# --- the SKILL.md half of the fix (guard-1475 regression pin) ----------------

def test_fresh_eyes_review_forbids_minting_a_receipt_id():
    """/fresh-eyes-review Phase 5.6 must forbid citing a board receipt at encode time.

    Its ONLY board-post is its LAST tool call, so at encode time no receipt exists
    to capture -- any id written there is necessarily invented. Remove the
    instruction and this pin fails (the goal's check 1).
    """
    skill = (SCRIPT_DIR.parent.parent / ".claude" / "skills"
             / "fresh-eyes-review" / "SKILL.md")
    # Reads the FILE, deliberately. This is the fresh-eyes-review half of the
    # pair; only its sibling below moved to the corpus (g-115-6689). Pointing
    # this one at the verify-learning corpus would make it pass on the
    # sibling's evidence — one canary silently covering two call sites is the
    # exact failure this test's docstring exists to prevent.
    text = skill.read_text(encoding="utf-8")
    assert "board-citation-check.py" in text or "never mint a board receipt id" in text, (
        "fresh-eyes-review Phase 5.6 lost its no-invented-receipt-id instruction")


def test_verify_learning_still_calls_the_checker():
    """The OTHER call site. This detector has exactly two, and both were lost at once.

    Measured 2026-08-12 (g-115-6052, alpha worker Body, hostname cc-07): merge
    0dadcff34 -- "keep the reducer's check, keep mine" -- dropped this script's
    wiring from BOTH .claude/skills/{fresh-eyes-review,verify-learning}/SKILL.md,
    leaving an 11kB tool and 8 pins with ZERO callers. `git log --full-history -S`
    reported only the ADD and no removal, because a merge whose result equals its
    first parent is not a content change; the drop was found by comparing the
    merge's RESULT against each PARENT.

    Its sibling pin above caught the fresh-eyes-review half. Nothing watched this
    half, so it was invisible -- a presence check on the script itself passes
    forever while it never runs (reclaim-routed-work.md, orphaned-sweep). That
    asymmetry is the whole reason this pin exists: one canary per call site, or
    the unwatched one is the one that goes.
    """
    skill = (SCRIPT_DIR.parent.parent / ".claude" / "skills"
             / "verify-learning" / "SKILL.md")
    # Corpus, not the file: the verify-learning check corpus moved to
    # core/config/verify-learning-checks.jsonl on 2026-08-18 (g-115-6689).
    # This canary pins a CALL SITE, and the call site moved with it.
    text = _verify_corpus.corpus_text()
    assert "board-citation-check.py" in text, (
        "verify-learning lost its board-citation-resolvable check -- "
        "board-citation-check.py now has one fewer call site, and a detector "
        "with no call site is indistinguishable from one that always returns clean")
