"""Apply one reply-to-close decision through the canonical goal writers ().

Decision logic lives in `reply_to_close` (pure, unit-tested). This module does the
I/O half: decode, look up the idempotency cursor, apply, and leave a trace.

WHY THERE IS NO NEW LEDGER FILE. The goal text says "appends to the user-blocker
digest ledger", but that lane deliberately keeps none -- see the comment at
user-blocker-escalation-check.py:94 ("no ledger to keep in sync,
communication-clarity rule 5"); its decisions/coordination BOARD POST is both the
artifact and the schedule cursor. Adding a parallel ledger would put two sources of
truth on one lane, which is the thing that comment refuses. So the board post is the
ledger: it carries the message id, and the idempotency check reads it back. One
artifact, two jobs, exactly as the digest already does for scheduling.

Never edits a store directly (constraint 5) -- every write goes through
aspirations-update-goal.sh / agent-aspirations-update-goal.sh.
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _runtime_bash import bash_cmd  # noqa: E402
import reply_to_close as rtc  # noqa: E402

BOARD_LOOKBACK = "720h"          # 30d: comfortably past any digest cadence
_MARK = "reply-to-close msg="    # the idempotency cursor token in board text


def _run(script, *args, stdin=None, timeout=180):
    return subprocess.run(bash_cmd(script, *args), capture_output=True, text=True,
                          input=stdin, encoding="utf-8", errors="replace",
                          timeout=timeout)


def decode_slices(raw_path):
    """Split one raw MIME message into its new-text and full-body slices."""
    r = _run("core/scripts/email-body-decode.sh", "--file", raw_path,
             "--format", "json")
    if r.returncode != 0:
        raise RuntimeError("email-body-decode failed rc=%d: %s"
                           % (r.returncode, (r.stderr or "")[:300]))
    d = json.loads(r.stdout)
    return d.get("new") or "", d.get("full") or ""


def already_processed(message_id):
    """True when a prior board post already recorded this message id.

    FAIL-CLOSED ON AN UNREADABLE BOARD. If the board cannot be read we return
    True (treat as already processed) rather than False. The asymmetry is
    deliberate: a missed close costs one more digest line and the user can reply
    again, while a double-apply on an unreadable cursor could re-close a goal the
    owner already reopened. Refusing to act on an unknown cursor is the safe
    direction (guard-1227 -- a wrong terminal write is the expensive error).
    """
    r = _run("core/scripts/board-read.sh", "--channel", "decisions",
             "--since", BOARD_LOOKBACK)
    if r.returncode != 0:
        return True, "board unreadable rc=%d" % r.returncode
    return (_MARK + message_id) in (r.stdout or ""), "board read ok"


def resolve_owner():
    """The owner address this lane will act for, via the canonical secrets reader.

    NEVER print or log the returned value. The user's personal address enters
    durable stores in SHAPE only (`z***@g***.com`) per guard-4061 / g-115-6433 --
    this function hands the raw value to `decide()` for an in-memory comparison and
    nothing else. Returns None when the key is absent, which makes every reply a
    no-op rather than defaulting to some other address.
    """
    r = _run("core/scripts/env-read.sh", "value", "USER_EMAIL")
    if r.returncode != 0:
        return None
    val = (r.stdout or "").strip()
    return val or None


def redact(addr):
    """`someone@example.com` -> `s***@e***.com`. For anything that gets written."""
    if not addr or "@" not in addr:
        return "(unknown)"
    local, _, domain = addr.partition("@")
    host, _, tld = domain.rpartition(".")
    return "%s***@%s***%s" % (local[:1], host[:1], ("." + tld) if tld else "")


def _goal_record(goal_id):
    """Return (record, source) for `goal_id`, or (None, None).

    ONE query covers BOTH queues. Verified 2026-08-24: aspirations-query.sh spans
    the world AND agent stores and stamps `source` on each record, so the caller
    does not choose a queue -- the record says which it came from, and that is what
    picks the writer below. (An earlier draft here called a per-queue
    `agent-aspirations-query.sh`; no such script exists -- the agent side has
    `-read.sh`, which is keyed by ASPIRATION id and could not answer this lookup at
    all. Caught by probing the interface instead of assuming the naming pattern
    held.)
    """
    r = _run("core/scripts/aspirations-query.sh", "--goal-field", "id", goal_id,
             "--full")
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None, None
    try:
        rows = json.loads(r.stdout)
    except ValueError:
        return None, None
    for g in (rows if isinstance(rows, list) else [rows]):
        if (g.get("id") or "").lower() == goal_id.lower():
            # `source` is authoritative; default to world only if it is absent.
            return g, (g.get("source") or "world")
    return None, None


def apply_decision(decision, *, dry_run=True):
    """Carry out an applied decision. Returns a result dict; never raises on a
    missing goal -- an unknown id becomes a reportable no-op, not a crash."""
    out = dict(decision)
    out["applied"] = False
    if decision["action"] == rtc.ACTION_NOOP:
        return out

    goal_id = decision["goal_id"]
    goal, source = _goal_record(goal_id)
    if goal is None:
        out["action"] = rtc.ACTION_NOOP
        out["reason"] = "goal id %s not found in the world or agent queue" % goal_id
        out["ack"] = True
        return out

    participants = [p for p in (goal.get("participants") or [])]
    user_only = participants == ["user"]
    out["source"], out["user_only"] = source, user_only
    out["prior_status"] = goal.get("status")
    writer = ("core/scripts/aspirations-update-goal.sh" if source == "world"
              else "core/scripts/agent-aspirations-update-goal.sh")

    # ORDER IS LOAD-BEARING: the outcome_note goes FIRST, the status change LAST.
    # These are separate writer calls, so either can fail independently. Writing the
    # status first and failing on the note would leave the goal TERMINAL with no
    # record of why -- precisely the silent deletion guard-1227 exists to prevent,
    # since a terminal goal drops out of both the selector list and the blocked
    # list. Reversed, a partial failure leaves a note and a live status: visible,
    # explicable, and re-runnable. This is not hypothetical -- the uncommitted-work
    # gate refused exactly this status write during the  build.
    writes = [("outcome_note", rtc.outcome_note(decision, user_only=user_only))]
    if decision["action"] == rtc.ACTION_COMPLETE:
        writes.append(("status", "completed"))
    elif user_only:
        # A user-only goal is SKIPPED, not reassigned. Rewriting participants to
        # ["agent"] here would hand the agent work the owner just said is not
        # needed, which is the opposite of what "not needed" asked for.
        writes.append(("status", "skipped"))
    else:
        writes.append(("participants",
                       json.dumps([p for p in participants if p != "user"] or ["agent"])))

    out["writes"] = [w[0] for w in writes]
    if dry_run:
        out["reason"] += " [dry-run: no writes performed]"
        return out

    for field, value in writes:
        r = _run(writer, goal_id, field, value)
        if r.returncode != 0:
            out["error"] = "writer failed on %s rc=%d: %s" % (
                field, r.returncode, (r.stderr or "")[:200])
            return out
    out["applied"] = True
    return out


def board_record(result):
    """One decisions-board line per applied action. This IS the ledger + cursor."""
    text = ("%s%s | goal=%s action=%s | %s"
            % (_MARK, result.get("message_id"), result.get("goal_id"),
               result.get("action"), result.get("reason")))
    r = _run("core/scripts/board-post.sh", "--channel", "decisions",
             "--type", "decision", "--tags",
             "reply-to-close,owner-decision,%s" % (result.get("goal_id") or "none"),
             stdin=text)
    return r.returncode == 0
