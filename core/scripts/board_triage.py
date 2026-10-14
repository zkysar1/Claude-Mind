#!/usr/bin/env python3
"""board_triage.py -- scan and dispose of board posts addressed to an agent.

Subcommands (via board-triage.sh):
  scan    -- list unhandled posts addressed to this agent
  dispose -- execute one disposition for a post (mark handled + side effects)

Budget (decided in g-115-12248-b): the per-iteration DO NOW count lives in ONE
small file, agents/<agent>/session/board-triage-budget.json (the agent's
cross-session state dir), shape {"iteration_id", "do_now_used", "updated"}.
WHY a file: the caller is a model running one bash call per post, so a count
held in its head or passed as a parameter is never enforced; it must survive
between calls. WHY it resets only on ``scan --begin-iteration``: the skill text
(g-115-12248-c) calls that once at the top of Phase 2.07, so a post-compaction
re-entry that runs a plain ``scan`` cannot refill the budget (an under-count is
the unsafe direction). Safety net: a count untouched for more than 2 hours is
stale and reads as 0, so a skill that forgets the flag cannot starve DO NOW.

Order of effects: every side effect (note, reply, goal) runs BEFORE the HANDLED
receipt, and the count is bumped only after the action succeeded, so a failed
step leaves the post unhandled for the next iteration instead of lost. A new
goal is looked up by its origin_signal first, so a retry after a partial
failure reuses the goal rather than filing a duplicate (guard-1677).

Authority: a post is cited as the request, never as the permission; nothing
here runs a command because a post says to. Whether a DO NOW action touches
code, money, production or owner approval is the caller's declaration
(``--touches``, required, validated); a peer deployment's work ask is refused
as DO NOW by its type or its requires_action_by tag.

g-115-12248-b.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup (same pattern as generation_phase_gate.py)
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from generation_phase_gate import (  # noqa: E402
    DEFAULT_ACTIONABLE_TYPES,
    addressed_reason,
)
from peer_surface import split_author  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402  (guard-580: never a bare "bash" argv0)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DO_NOW_BUDGET = 3
BUDGET_STALENESS_HOURS = 2

# Actions that are NEVER DO NOW regardless of budget (design item 3).
NEVER_DO_NOW_TOUCHES = frozenset({
    "code", "money", "production", "owner-approval",
})

# Reuse the canonical actionable-type set from generation_phase_gate.
ACTIONABLE_TYPES = frozenset(DEFAULT_ACTIONABLE_TYPES)

# A peer deployment's ask for work always becomes a goal; a plain question may
# be answered on the spot (design item 4).
WORK_ASK_TYPES = ACTIONABLE_TYPES - {"question"}

# The caller's declaration for a DO NOW action that touches records only.
TOUCHES_RECORDS = "records"


# ---------------------------------------------------------------------------
# Peer detection
# ---------------------------------------------------------------------------

def _get_environment_id():
    """Return the local ENVIRONMENT_ID (deferred import to stay lightweight)."""
    try:
        from _paths import ENVIRONMENT_ID  # noqa: F811
        return ENVIRONMENT_ID
    except Exception:
        return None


def _get_local_roster():
    """Return the set of agent names in the local deployment."""
    try:
        from _agents import get_active_agents  # type: ignore
        return set(get_active_agents())
    except Exception:
        return set()


def is_peer_author(author, local_roster=None, environment_id=None):
    """True when *author* belongs to a peer deployment, not the local one.

    - ``agent@env`` where *env* matches local ENVIRONMENT_ID -> local.
    - ``agent@env`` where *env* differs -> peer.
    - Bare name present in the local roster -> local.
    - Bare name NOT in the roster -> PEER (conservative direction).
    """
    if local_roster is None:
        local_roster = _get_local_roster()
    if environment_id is None:
        environment_id = _get_environment_id()

    agent, env = split_author(str(author or ""))
    if env is not None:
        # Qualified name: compare env with local ENVIRONMENT_ID.
        if environment_id and env == environment_id:
            return False  # local agent posting with qualifier
        return True  # different env or unknown local env -> peer
    # Bare name: local only if in the roster; unknown -> PEER (conservative).
    return agent.lower() not in {n.lower() for n in local_roster}


# ---------------------------------------------------------------------------
# Deadline extraction
# ---------------------------------------------------------------------------

def extract_deadline(post):
    """Extract deadline from ``expires:<ISO>`` tags.  Returns str or None.

    Never invents a deadline.  The ``expires:<ISO>`` format is the board
    convention (see board.md).
    """
    for tag in (post.get("tags") or []):
        tag_s = str(tag).strip()
        if tag_s.lower().startswith("expires:"):
            val = tag_s[len("expires:"):]
            if val:
                return val
    return None


def is_work_ask(post):
    """True when *post* asks for work: a work-ask type, or a status-shaped post
    carrying a requires_action_by tag (the board's own explicit demand)."""
    ptype = str(post.get("type", "")).strip().lower()
    if ptype in WORK_ASK_TYPES:
        return True
    if ptype == "question":
        return False
    return any(str(t).strip().lower().startswith("requires_action_by:")
               for t in (post.get("tags") or []))


# ---------------------------------------------------------------------------
# Budget persistence
# ---------------------------------------------------------------------------

def _budget_path(agent):
    """Path to the per-agent budget JSON file (cross-session state dir)."""
    from _paths import agent_state_dir
    return Path(agent_state_dir(agent)) / "board-triage-budget.json"


def _empty_budget():
    return {"iteration_id": "", "do_now_used": 0, "updated": ""}


def _fresh(data):
    """*data*, or the empty budget when it is >2 h old (safety-net reset)."""
    updated = data.get("updated", "")
    if updated:
        try:
            ts = datetime.fromisoformat(updated)
            if datetime.now() - ts > timedelta(hours=BUDGET_STALENESS_HOURS):
                return _empty_budget()
        except (ValueError, TypeError):
            pass
    return data


def read_budget(agent):
    """Read the budget; safety-net reset when >2 h old."""
    path = _budget_path(agent)
    if not path.exists():
        return _empty_budget()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return _empty_budget()
    return _fresh(data)


def write_budget(agent, data):
    """Persist the budget state."""
    from _fileops import locked_write_json
    path = _budget_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    locked_write_json(path, data)


def reset_budget(agent, iteration_id=""):
    """Reset the budget for a new iteration."""
    data = {
        "iteration_id": iteration_id,
        "do_now_used": 0,
        "updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    write_budget(agent, data)
    return data


def budget_left(agent):
    """DO NOW actions still available this iteration (0 when exhausted)."""
    return max(0, DO_NOW_BUDGET - int(read_budget(agent).get("do_now_used", 0)))


def bump_budget(agent):
    """Record one DO NOW action that SUCCEEDED; returns do_now_used. The read,
    the increment and the write all sit under the file lock (rb-427), so
    concurrent callers each count."""
    from _fileops import locked_modify_json

    def _increment(data):
        data = {**_empty_budget(), **_fresh(data)}
        data["do_now_used"] = int(data["do_now_used"]) + 1
        data["updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        return data

    try:
        return locked_modify_json(_budget_path(agent),
                                  _increment)["do_now_used"]
    except ValueError:  # a corrupt file fails open, as read_budget does
        data = _increment({})
        write_budget(agent, data)
        return data["do_now_used"]


# ---------------------------------------------------------------------------
# Scan -- return unhandled posts addressed to this agent
# ---------------------------------------------------------------------------

def scan(posts, me, my_post_ids=None):
    """Return ``[(post, reason), ...]`` for posts addressed to *me*.

    *posts* must already be filtered to unhandled-only by the caller
    (board-read.sh --unhandled-only).  This function applies the addressing
    predicate (reusing generation_phase_gate.addressed_reason) plus the
    "everyone with no routing tag" rule.
    """
    if my_post_ids is None:
        my_post_ids = set()
    me_lower = (me or "").strip().lower()
    if not me_lower:
        return []

    results = []
    for p in posts:
        if not isinstance(p, dict):
            continue
        # Skip own posts.
        author_agent = split_author(str(p.get("author", "")))[0].lower()
        if author_agent == me_lower:
            continue

        reason = addressed_reason(p, me_lower, my_post_ids)
        if reason:
            results.append((p, reason))
            continue

        # "requests sent to everyone with no routing tag" (design item 1).
        tags = p.get("tags") or []
        has_routing_tag = any(
            str(t).strip().lower().startswith("requires_action_by:")
            for t in tags
        )
        if not has_routing_tag and _is_actionable_type(p):
            bare_addressees = [
                t for t in tags
                if not str(t).strip().lower().startswith("requires_action_by:")
                and ":" not in str(t) and "@" not in str(t)
                and " " not in str(t) and str(t).strip()
            ]
            if not bare_addressees:
                results.append((p, "everyone-no-tag"))

    return results


def _is_actionable_type(post):
    """True when the post's type is in the actionable set."""
    return str(post.get("type", "")).strip().lower() in ACTIONABLE_TYPES


# ---------------------------------------------------------------------------
# Disposition classification (pure logic)
# ---------------------------------------------------------------------------

def classify_disposition(post, reason, do_now_remaining,
                         touches=None, local_roster=None,
                         environment_id=None):
    """Classify a single post into a disposition.

    Returns a dict::

        {
            "disposition":      "FYI" | "DO_NOW" | "GOAL" | "NOTED_NOT_FILING"
                                | "OVER_BUDGET",
            "do_now_remaining": <int>,
            "is_peer":          <bool>,
            "reason":           <str>,
            "note":             <str>,
            "deadline":         <str or absent>,
        }
    """
    if touches is None:
        touches = set()
    if local_roster is None:
        local_roster = _get_local_roster()

    author = str(post.get("author", ""))
    peer = is_peer_author(author, local_roster, environment_id)
    ptype = str(post.get("type", "")).strip().lower()
    deadline = extract_deadline(post)

    base = {"is_peer": peer, "reason": reason}
    if deadline:
        base["deadline"] = deadline

    # A non-actionable type with no explicit demand is an FYI.
    if ptype not in ACTIONABLE_TYPES and not is_work_ask(post):
        return {**base, "disposition": "FYI",
                "do_now_remaining": do_now_remaining,
                "note": "non-actionable type"}

    # Peer-deployment work asks always become a goal.
    if peer and is_work_ask(post):
        return {**base, "disposition": "GOAL",
                "do_now_remaining": do_now_remaining,
                "note": "peer work ask -> goal"}

    # Actions touching code/money/production/owner-approval -> never DO NOW.
    if touches & NEVER_DO_NOW_TOUCHES:
        return {**base, "disposition": "GOAL",
                "do_now_remaining": do_now_remaining,
                "note": "touches " + ", ".join(sorted(
                    touches & NEVER_DO_NOW_TOUCHES))}

    # Budget check for DO NOW.
    if do_now_remaining > 0:
        return {**base, "disposition": "DO_NOW",
                "do_now_remaining": do_now_remaining - 1,
                "note": "within budget"}

    # Over budget: stays unhandled for the next iteration.
    return {**base, "disposition": "OVER_BUDGET",
            "do_now_remaining": do_now_remaining,
            "note": "DO NOW budget exhausted; stays unhandled"}


# ---------------------------------------------------------------------------
# Dispose -- execute one disposition
# ---------------------------------------------------------------------------

class DisposeError(Exception):
    """A disposition could not be carried out; the post was NOT marked handled."""


def goal_origin(msg_id):
    """origin_signal of a goal filed from a board post (design item 2)."""
    return "board_post:{}".format(msg_id)


def dispose(post, disposition, io, channel="coordination", reply_text="",
            goal_body=None, covers=None, note_goal=None, note_text="",
            reason=""):
    """Execute one disposition for *post*; the HANDLED receipt is written LAST.

    *io* is the only side-effect seam (tests stub it), a dict of callables::

        mark_handled(channel, msg_id)
        post_reply(channel, reply_to_id, text)
        file_goal(goal_body_dict) -> goal id, or None when it was not filed
        find_goal(origin_signal)  -> id of an existing goal with it, or None
        note_goal(goal_id, msg_id, text)      # idempotent per (goal, msg id)

    FYI: receipt only. DO_NOW: a goal note (*note_goal* + *note_text*) and/or a
    reply, at least one. GOAL: *covers* an existing goal (note, optional reply),
    or *goal_body* a new goal (reused when one with the same origin_signal
    exists) always followed by a reply naming the goal (``{goal}`` in
    *reply_text* is replaced by its id, else "Tracked as <id>." is appended).
    NOTED_NOT_FILING: needs *reason*. Raises DisposeError, with the post left
    unhandled, when a step cannot be carried out.
    """
    msg_id = str(post.get("id", ""))
    disp = disposition["disposition"]
    actions = []
    goal_id = None

    if disp == "OVER_BUDGET":
        return {"msg_id": msg_id, "disposition": disp, "goal_id": None,
                "actions": ["left unhandled (over budget)"]}

    if disp == "FYI":
        pass
    elif disp == "DO_NOW":
        has_note = bool(note_goal and note_text)
        if not reply_text and not has_note:
            raise DisposeError(
                "DO_NOW needs a reply (--reply-file) or a goal note "
                "(--note-goal and --note-file); with neither it is an FYI")
        if has_note:
            io["note_goal"](note_goal, msg_id, note_text)
            actions.append("noted {}".format(note_goal))
        if reply_text:
            io["post_reply"](channel, msg_id, reply_text)
            actions.append("posted reply")
    elif disp == "GOAL":
        if covers:
            goal_id = covers
            io["note_goal"](covers, msg_id, note_text or
                            "Board post {} is covered by this goal.".format(msg_id))
            actions.append("noted covering goal {}".format(covers))
            if reply_text:
                io["post_reply"](channel, msg_id,
                                 reply_text.replace("{goal}", covers))
                actions.append("posted reply")
        else:
            if not goal_body:
                raise DisposeError(
                    "GOAL needs --covers <goal id> or a new goal "
                    "(--title with --description-file, or --file-goal)")
            origin = goal_body.get("origin_signal") or goal_origin(msg_id)
            goal_id = io["find_goal"](origin)
            if goal_id:
                actions.append("reused goal {} (same origin_signal)".format(goal_id))
            else:
                goal_id = io["file_goal"](goal_body)
                if not goal_id:
                    raise DisposeError(
                        "the goal was not filed or its id could not be read; do "
                        "NOT re-file blindly: query the store by origin_signal "
                        "{} first".format(origin))
                actions.append("filed goal {}".format(goal_id))
            if "{goal}" in reply_text:
                text = reply_text.replace("{goal}", goal_id)
            else:
                text = (reply_text + "\n" if reply_text else "") + \
                    "Tracked as {}.".format(goal_id)
            io["post_reply"](channel, msg_id, text)
            actions.append("posted reply naming {}".format(goal_id))
    elif disp == "NOTED_NOT_FILING":
        if not reason.strip():
            raise DisposeError("NOTED_NOT_FILING needs a one-line --reason")
    else:
        raise DisposeError("unknown disposition {!r}".format(disp))

    io["mark_handled"](channel, msg_id)
    actions.append("marked handled ({})".format(disp))
    return {"msg_id": msg_id, "disposition": disp, "goal_id": goal_id,
            "actions": actions}


# ---------------------------------------------------------------------------
# Real IO implementations (used by the CLI)
# ---------------------------------------------------------------------------

def _real_mark_handled(channel, msg_id):
    """Mark a post as HANDLED via board.py mark-read --kind handled."""
    import subprocess
    project = _SCRIPT_DIR.parent.parent
    cmd = [
        sys.executable,
        str(project / "core" / "scripts" / "board.py"),
        "mark-read",
        "--channel", channel,
        "--ids", msg_id,
        "--kind", "handled",
    ]
    agent = os.environ.get("MIND_AGENT", "")
    if agent:
        cmd.extend(["--reader", agent])
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def _real_post_reply(channel, reply_to_id, text):
    """Post a reply via board-post.sh (message via STDIN per rb-3642); the reply
    itself writes the HANDLED receipt for the post it answers."""
    import subprocess
    cmd = bash_cmd(str(_SCRIPT_DIR / "board-post.sh"),
                   "--channel", channel, "--reply-to", reply_to_id,
                   "--type", "status")
    subprocess.run(cmd, input=text, check=True, capture_output=True, text=True)


def _real_find_goal(origin):
    """Id of an existing goal, in ANY status, carrying this origin_signal."""
    import subprocess
    cmd = bash_cmd(str(_SCRIPT_DIR / "aspirations-query.sh"),
                   "--goal-status",
                   "pending,in-progress,completed,blocked,skipped,expired,"
                   "decomposed,superseded",
                   "--goal-field", "origin_signal", origin, "--full")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise DisposeError("aspirations-query.sh failed (rc={}): {}".format(
            proc.returncode, proc.stderr.strip()[:300]))
    try:
        rows = json.loads(proc.stdout or "[]")
    except ValueError:
        raise DisposeError("aspirations-query.sh output was not JSON")
    for g in (rows if isinstance(rows, list) else []):
        if str(g.get("origin_signal", "")) == origin and g.get("id"):
            return str(g["id"])
    return None


def _real_file_goal(body):
    """File a goal via aspirations-add-goal.sh (JSON body on STDIN).

    Returns the new goal's id, or None when the wrapper refused or its output
    carried no id (guard-1677: a refusal is valid JSON too; the caller must not
    re-run blindly, it queries by origin_signal first)."""
    import re
    import subprocess
    send_body = {k: v for k, v in body.items() if k != "aspiration"}
    cmd = bash_cmd(str(_SCRIPT_DIR / "aspirations-add-goal.sh"),
                   "--source", "world")
    if body.get("aspiration"):
        cmd += ["--aspiration", str(body["aspiration"])]
    proc = subprocess.run(cmd, input=json.dumps(send_body),
                          capture_output=True, text=True)
    if proc.returncode != 0:
        print("aspirations-add-goal.sh refused (rc={}): {} {}".format(
            proc.returncode, proc.stdout.strip()[:300],
            proc.stderr.strip()[:300]), file=sys.stderr)
        return None
    try:
        gid = json.loads(proc.stdout).get("id")
    except (ValueError, AttributeError):
        gid = None
    if not gid:
        m = re.search(r"\bg-\d+-\d+(?:-[a-z])?\b", proc.stdout)
        gid = m.group(0) if m else None
    return gid


def _real_note_goal(goal_id, msg_id, text):
    """Append *text* to the goal's progress_note once per post (the marker is
    derived from the message id, so a retry cannot double-append)."""
    import re
    import subprocess
    marker = "board-" + re.sub(r"[^A-Za-z0-9._-]", "-", msg_id)
    cmd = bash_cmd(str(_SCRIPT_DIR / "goal-field-append.sh"), "--source",
                   "world", goal_id, "progress_note", marker, "--value-stdin")
    subprocess.run(cmd, input=text, check=True, capture_output=True, text=True)


def _real_io():
    """Build the real IO dict for CLI use."""
    return {
        "mark_handled": _real_mark_handled,
        "post_reply": _real_post_reply,
        "file_goal": _real_file_goal,
        "find_goal": _real_find_goal,
        "note_goal": _real_note_goal,
    }


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _board_read(channel, since_hours, unhandled_only=False):
    """Posts on *channel* from the last *since_hours*, via board-read.sh --json
    (one JSON object per line). Raises DisposeError when the read fails."""
    import subprocess
    since_iso = (datetime.now() - timedelta(hours=since_hours)
                 ).strftime("%Y-%m-%dT%H:%M:%S")
    cmd = bash_cmd(str(_SCRIPT_DIR / "board-read.sh"), "--channel", channel,
                   "--since", since_iso, "--json")
    if unhandled_only:
        cmd.append("--unhandled-only")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise DisposeError("board-read.sh failed (rc={}): {}".format(
            proc.returncode, proc.stderr.strip()[:300]))
    posts = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            posts.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return posts


def _read_post_by_id(channel, msg_id, since_hours=168):
    """The post with this id from the last *since_hours*, or None."""
    for p in _board_read(channel, since_hours):
        if str(p.get("id", "")) == msg_id:
            return p
    return None


def _append_diary(msg_id, disposition, detail="", about_goal=None):
    """Append ONE execution-diary decision line for a DO NOW or NOTED action.
    Returns True when it landed; a failure is reported, never swallowed."""
    import subprocess
    content = "board-disposition {} {}".format(disposition, msg_id)
    if detail:
        content += ": " + " ".join(str(detail).split())
    entry = {"entry_type": "decision",
             "content": content.encode("ascii", "replace").decode("ascii")[:600]}
    if about_goal:
        entry["goal_id"] = about_goal
    proc = subprocess.run(
        bash_cmd(str(_SCRIPT_DIR / "execution-diary.sh"), "append"),
        input=json.dumps(entry), capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        print("WARNING: execution-diary append failed (rc={}): {}".format(
            proc.returncode, proc.stderr.strip()[:200]), file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------

def _build_parser():
    import argparse
    p = argparse.ArgumentParser(
        description="Board triage: scan and dispose of addressed posts.",
    )
    sub = p.add_subparsers(dest="command")

    sc = sub.add_parser("scan", help="List unhandled addressed posts")
    sc.add_argument("--channel", default="coordination")
    sc.add_argument("--agent", default=None,
                    help="Agent name (default: MIND_AGENT env)")
    sc.add_argument("--json", action="store_true", dest="as_json")
    sc.add_argument("--since", type=int, default=72,
                    help="Hours to look back (default: 72)")
    sc.add_argument("--begin-iteration", action="store_true",
                    help="Reset the DO NOW budget for a new iteration")

    dp = sub.add_parser("dispose", help="Execute one disposition")
    dp.add_argument("--channel", default="coordination")
    dp.add_argument("--agent", default=None)
    dp.add_argument("--msg-id", required=True)
    dp.add_argument("--disposition", required=True,
                    choices=["FYI", "DO_NOW", "GOAL", "NOTED_NOT_FILING"])
    dp.add_argument("--since", type=int, default=168,
                    help="Hours to search for the post (default: 168)")
    dp.add_argument("--reply-file", default=None,
                    help="Reply text file (DO_NOW; GOAL, where {goal} becomes "
                         "the goal id)")
    dp.add_argument("--touches", default=None,
                    help="DO_NOW, REQUIRED: what the action touches, comma list "
                         "of records,code,money,production,owner-approval")
    dp.add_argument("--note-goal", default=None,
                    help="DO_NOW: goal id the action adds a note to")
    dp.add_argument("--note-file", default=None,
                    help="Note text file (DO_NOW with --note-goal; GOAL with "
                         "--covers)")
    dp.add_argument("--covers", default=None,
                    help="GOAL: id of an existing goal that covers the post")
    dp.add_argument("--title", default=None, help="GOAL: title of a new goal")
    dp.add_argument("--description-file", default=None,
                    help="GOAL: description text file (with --title)")
    dp.add_argument("--aspiration", default=None,
                    help="GOAL: aspiration id a new goal is filed under")
    dp.add_argument("--priority", default="MEDIUM",
                    choices=["HIGH", "MEDIUM", "LOW"])
    dp.add_argument("--file-goal", default=None,
                    help="GOAL: full goal body JSON file, instead of --title")
    dp.add_argument("--reason", default=None,
                    help="NOTED_NOT_FILING: one line of reason")
    dp.add_argument("--about-goal", default=None,
                    help="Goal the diary line is about (optional)")

    return p


def main():
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    agent = args.agent or os.environ.get("MIND_AGENT", "")
    if not agent:
        print("Error: --agent or MIND_AGENT required", file=sys.stderr)
        sys.exit(1)

    if args.command == "scan":
        _cmd_scan(args, agent)
    elif args.command == "dispose":
        _cmd_dispose(args, agent)


def _cmd_scan(args, agent):
    """Execute the scan subcommand (read-only unless --begin-iteration)."""
    if args.begin_iteration:
        reset_budget(agent, datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))

    try:
        posts = _board_read(args.channel, args.since, unhandled_only=True)
    except DisposeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    # An author never HANDLES its own post, so the unhandled window already
    # holds every id of this agent's that a reply could point at.
    my_ids = {
        str(p.get("id")) for p in posts
        if split_author(str(p.get("author", "")))[0].lower() == agent.lower()
    }

    results = scan(posts, agent, my_ids)
    roster, env_id = _get_local_roster(), _get_environment_id()

    if args.as_json:
        out = []
        for post, reason in results:
            out.append({
                "id": post.get("id"),
                "author": post.get("author"),
                "type": post.get("type"),
                "text": (post.get("text") or "")[:200],
                "addressed_reason": reason,
                "is_peer": is_peer_author(post.get("author"), roster, env_id),
                "work_ask": is_work_ask(post),
                "deadline": extract_deadline(post),
            })
        json.dump(out, sys.stdout, indent=2)
        print()
    else:
        print("{} unhandled addressed post(s); DO NOW used {} of {} this "
              "iteration".format(
                  len(results),
                  int(read_budget(agent).get("do_now_used", 0)), DO_NOW_BUDGET))
        for post, reason in results:
            pid = post.get("id", "?")
            pauthor = post.get("author", "?")
            ptype = post.get("type", "?")
            snippet = (post.get("text") or "")[:80].replace("\n", " ")
            print("  {}  [{}] by {} ({}): {}".format(
                pid, ptype, pauthor, reason, snippet))


def _cmd_dispose(args, agent):
    """Execute the dispose subcommand.  Exit codes: 2 = refused by policy or
    bad flags, 3 = DO NOW budget exhausted (post stays unhandled), 1 = a step
    failed (post stays unhandled)."""
    import subprocess

    def refuse(msg, code=2):
        print("Error: " + msg, file=sys.stderr)
        sys.exit(code)

    try:
        post = _read_post_by_id(args.channel, args.msg_id, args.since)
    except DisposeError as exc:
        refuse(str(exc), 1)
    if post is None:
        refuse("post {} not found in the last {}h (pass --since <hours>)"
               .format(args.msg_id, args.since), 1)

    name = args.disposition
    if is_peer_author(post.get("author")) and is_work_ask(post) \
            and name != "GOAL":
        refuse("a peer deployment's work ask always becomes a goal; dispose "
               "it as GOAL, never {}".format(name))

    kwargs = {}
    if name == "DO_NOW":
        raw = [t.strip().lower() for t in (args.touches or "").split(",")
               if t.strip()]
        valid = NEVER_DO_NOW_TOUCHES | {TOUCHES_RECORDS}
        if not raw:
            refuse("--touches is required for DO_NOW: {} when the action "
                   "touches records only, else any of {}".format(
                       TOUCHES_RECORDS, ", ".join(sorted(NEVER_DO_NOW_TOUCHES))))
        bad = [t for t in raw if t not in valid]
        if bad:
            refuse("unknown --touches value(s) {}; valid: {}".format(
                ", ".join(bad), ", ".join(sorted(valid))))
        verdict = classify_disposition(post, "cli", budget_left(agent),
                                       touches=set(raw))
        if verdict["disposition"] == "OVER_BUDGET":
            refuse("OVER_BUDGET: {} of {} DO NOW used this iteration; the post "
                   "stays unhandled for the next iteration".format(
                       int(read_budget(agent).get("do_now_used", 0)),
                       DO_NOW_BUDGET), 3)
        if verdict["disposition"] == "GOAL":
            refuse("not DO NOW ({}); dispose it as GOAL".format(verdict["note"]))
        kwargs = {"reply_text": _read_text(args.reply_file, refuse),
                  "note_goal": args.note_goal,
                  "note_text": _read_text(args.note_file, refuse)}
    elif name == "GOAL":
        if sum([bool(args.covers), bool(args.file_goal), bool(args.title)]) != 1:
            refuse("GOAL needs exactly one of --covers <goal id>, --file-goal "
                   "<json file>, or --title with --description-file")
        kwargs = {"reply_text": _read_text(args.reply_file, refuse)}
        if args.covers:
            kwargs.update(covers=args.covers,
                          note_text=_read_text(args.note_file, refuse))
        else:
            kwargs["goal_body"] = _new_goal_body(args, post, refuse)
    elif name == "NOTED_NOT_FILING":
        if not (args.reason or "").strip():
            refuse("NOTED_NOT_FILING needs --reason <one line>")
        kwargs = {"reason": args.reason}

    try:
        result = dispose(post, {"disposition": name}, _real_io(),
                         channel=args.channel, **kwargs)
    except (DisposeError, subprocess.CalledProcessError, OSError) as exc:
        refuse("{} failed, post left unhandled: {} {}".format(
            name, exc, (getattr(exc, "stderr", "") or "").strip()[:300]), 1)

    if name == "DO_NOW":
        result["do_now_used"] = bump_budget(agent)
        _append_diary(args.msg_id, "DO_NOW", "; ".join(result["actions"]),
                      args.about_goal or args.note_goal)
    elif name == "NOTED_NOT_FILING":
        _append_diary(args.msg_id, "NOTED_NOT_FILING", args.reason,
                      args.about_goal)
    print(json.dumps(result, indent=2))


def _read_text(path, refuse):
    """Stripped text of a file, or "" when no path was given. An unreadable
    file is a bad flag: refused (exit 2) before any effect."""
    if not path:
        return ""
    try:
        return Path(path).read_text().strip()
    except (OSError, ValueError) as exc:
        refuse("cannot read {}: {}".format(path, exc))


def _new_goal_body(args, post, refuse):
    """Goal body for a GOAL that files a new goal; origin_signal is always the
    post, and a peer's deadline (its expires tag) becomes resolves_by."""
    if args.file_goal:
        try:
            body = json.loads(Path(args.file_goal).read_text())
        except (OSError, ValueError) as exc:
            refuse("cannot read --file-goal {}: {}".format(args.file_goal, exc))
        if not isinstance(body, dict):
            refuse("--file-goal must hold one JSON object")
    else:
        description = _read_text(args.description_file, refuse)
        if not description:
            refuse("--title needs a non-empty --description-file")
        body = {"title": args.title, "description": description,
                "priority": args.priority, "participants": ["agent"]}
    if args.aspiration:
        body["aspiration"] = args.aspiration
    if not body.get("aspiration"):
        refuse("a new goal needs --aspiration <asp id> (or an 'aspiration' key "
               "in the --file-goal body)")
    body.setdefault("origin_signal", goal_origin(args.msg_id))
    deadline = extract_deadline(post)
    if deadline and is_peer_author(post.get("author")):
        body.setdefault("resolves_by", deadline)
    return body


if __name__ == "__main__":
    main()
