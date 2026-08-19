#!/usr/bin/env python3
"""Shared team state for multi-agent situational awareness.

Manages the composed team state: shared fields live in
world/team-state.yaml (the core file); each agent's agent_status row lives
in its OWN file world/team-state/agents/<name>.yaml (g-328-27 sharding —
no two agents ever write the same object, so heartbeat/claim stamps never
contend). Reads compose the two; writes route by field via _team_state.
Locked writes via _fileops prevent concurrent modification per file.

Subcommands:
  update            — Update a specific field (set, append, remove); routes
                      agent_status.<name>[...] to that agent's row file
  init              — Create team-state.yaml + rows dir if missing
  in-flight         — Mark agent as in-flight on a goal (row file)
  clear-in-flight   — Remove the in_flight block from an agent's status (row file)
  clear-body-row    — Remove one in_flight_bodies.<sid> entry + null siblings
  retire-agent      — Remove an agent's presence (archive-before-delete gated)
  migrate-shard     — One-shot cleanup: move core agent_status residuals to rows
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml

from _paths import WORLD_DIR
from _fileops import locked_modify_yaml, locked_write_yaml
from _team_state import (
    body_row_shard_present,
    compose_state,
    core_residual,
    make_clear_body_row_modifier,
    make_clear_in_flight_modifier,
    retire_agent,
    route_field,
    row_path,
    rows_dir,
    stamp_row_metadata,
    _entry_ts,
)

TEAM_STATE_PATH = WORLD_DIR / "team-state.yaml"

EMPTY_STATE = {
    "last_updated": None,
    "last_updated_by": None,
    "strategic_focus": {
        "primary": None,
        "rationale": None,
        "set_by": None,
        "set_at": None,
        "acknowledged_by": [],
    },
    "active_blockers": [],
    "recent_completions": [],
    "agent_status": {},
    "critical_blockers": [],
    # Inbox-derived backlog counter (). null when zero matching
    # goals; else {count, oldest_age_hours, oldest_goal_id, updated_at}.
    # Written by core/scripts/inbox-backlog-update.py (atomic, via this
    # module's `update` CLI); read by aspirations-precheck Phase 0-pre.0.
    "inbox_alert_backlog": None,
}

MAX_RECENT_COMPLETIONS = 50

def read_state():
    """Read the composed team state (core file + per-agent row files),
    returning empty structure if missing. Row files win newest-wins over
    core residuals — see _team_state module docstring (g-328-27)."""
    if not TEAM_STATE_PATH.exists():
        data = dict(EMPTY_STATE)
    else:
        with open(TEAM_STATE_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not data:
            data = dict(EMPTY_STATE)
    # Schema migration: backfill any keys added to EMPTY_STATE since file was created
    for key, default in EMPTY_STATE.items():
        if key not in data:
            data[key] = default if not isinstance(default, (list, dict)) else type(default)()
    return compose_state(data, WORLD_DIR)

def _stamp_metadata(data, agent_name):
    """Stamp last_updated + last_updated_by on every write. Called by
    modifier closures passed to locked_modify_yaml so these fields are
    inside the lock alongside the real mutation."""
    data["last_updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    data["last_updated_by"] = agent_name
    return data

def _agent_name():
    return os.environ.get("MIND_AGENT", "system")

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_update(args):
    """Update a specific field in team state."""
    _validate_field_path(args.field)
    agent = args.author or _agent_name()
    field = args.field
    value = args.value

    # Parse value as JSON if possible, else keep as string
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        parsed = value

    #  sharding: agent_status.<name>[...] writes land in that
    # agent's OWN row file — never the shared core file — so heartbeat /
    # focus / in-flight stamps from N agents no longer contend on one
    # object. Everything else keeps the legacy core-file path below.
    scope, row_agent, subpath = route_field(field)
    if scope == "row":
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        def _row_modifier(row):
            if not isinstance(row, dict):
                row = {}
            if subpath == "":
                # Whole-row set (e.g. consolidate's session-end snapshot).
                # append/remove make no sense against a whole row.
                if args.operation != "set":
                    sys.exit("team-state: agent_status.<name> whole-row "
                             "supports only --operation set")
                if not isinstance(parsed, dict):
                    sys.exit("team-state: whole-row value must be a JSON object")
                row = dict(parsed)
            elif args.operation == "set":
                _set_nested(row, subpath, parsed)
            elif args.operation == "append":
                _append_nested(row, subpath, parsed)
            elif args.operation == "remove":
                _remove_nested(row, subpath, parsed)
            return stamp_row_metadata(row, agent, now)

        locked_modify_yaml(row_path(WORLD_DIR, row_agent), _row_modifier,
                           initial=core_residual(TEAM_STATE_PATH, row_agent))
        print(f"Updated {field}")
        return

    def _modifier(state):
        # Schema-migration backfill (previously in read_state); we must do
        # it here because locked_modify_yaml reads the raw file without
        # knowing about EMPTY_STATE.
        for key, default in EMPTY_STATE.items():
            if key not in state:
                state[key] = default if not isinstance(default, (list, dict)) else type(default)()
        if args.operation == "set":
            _set_nested(state, field, parsed)
        elif args.operation == "append":
            _append_nested(state, field, parsed)
        elif args.operation == "remove":
            _remove_nested(state, field, parsed)
        #  / guard-1153: strategic_focus is LWW-ordered on set_at by
        # coordination_merge._merge_strategic_focus, and guard-1153 permits LWW
        # only on a timestamp written BY THE SAME MUTATION that writes the field.
        # It was not: live team-state carried set_at 2026-07-04T13:45:00 while
        # `primary` had been amended 2026-08-03, so a cross-box merge saw EQUAL
        # timestamps and fell through to _order_by_ts's _canon content tiebreak —
        # whether the amendment survived was decided by JSON string comparison,
        # not recency. It happened to win by being longer. Luck, not design.
        #
        # The bump lives HERE, not at the call sites, deliberately: every
        # amendment routes through this one branch, and the 2026-08-03 amendment
        # forgot the bump precisely because bumping was a caller's job.
        #
        # An explicit set_at supplied BY the mutation is respected and never
        # overwritten — that is the only way to restate a historical stamp
        # (migrations, backfills).
        if field == "strategic_focus" or field.startswith("strategic_focus."):
            mutation_sets_set_at = (
                field == "strategic_focus.set_at"
                or (field == "strategic_focus"
                    and isinstance(parsed, dict) and "set_at" in parsed))
            if not mutation_sets_set_at:
                _set_nested(state, "strategic_focus.set_at",
                            datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        # Enforce ring buffer on recent_completions
        if "recent_completions" in state:
            state["recent_completions"] = state["recent_completions"][-MAX_RECENT_COMPLETIONS:]
        return _stamp_metadata(state, agent)

    locked_modify_yaml(TEAM_STATE_PATH, _modifier, initial=dict(EMPTY_STATE))
    print(f"Updated {field}")

def cmd_init(args):
    """Initialize team-state.yaml if it doesn't exist. Always ensures the
    per-agent rows directory exists (idempotent), so aged deployments gain
    the sharded layout on their next init call (g-328-27)."""
    rows_dir(WORLD_DIR).mkdir(parents=True, exist_ok=True)
    if TEAM_STATE_PATH.exists():
        print("team-state.yaml already exists")
        return
    TEAM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    agent = args.author or _agent_name()
    state = _stamp_metadata(dict(EMPTY_STATE), agent)
    # Init is single-writer (we just checked the file doesn't exist, and
    # two racing inits would both see False before either wrote), so use
    # locked_write_yaml. If a race DID happen, the second writer would
    # just overwrite with identical empty state — no data loss.
    locked_write_yaml(TEAM_STATE_PATH, state)
    print(f"Created {TEAM_STATE_PATH}")

def cmd_in_flight(args):
    """Mark an agent as in-flight on a goal. Auto-stamps claimed_at AND last_active.

    Bumping last_active here closes the silence-detection drift where a long-running
    Phase-4 goal made the running agent look silent for hours: last_active was only
    written by iteration-close.sh do_state_update (Phase 8), so a partner reading
    team-state.yaml mid-execution saw a stale timestamp from the previous goal's
    completion. Claim is unambiguous proof of liveness — write both fields together.
    """
    _validate_agent_name(args.agent, "in-flight")
    target_agent = args.agent
    agent_author = args.author or _agent_name()
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    # current_focus: lane indicator for partner Theory-of-Mind ().
    # Aspiration (lane) parsed from goal_id (g-NNN-MM -> asp-NNN) + title, so
    # partners track the actual lane instead of inferring from lagging
    # completions. Persists across clear-in-flight (the last-claimed lane).
    # MUST stay byte-identical to mind_api/src/world/team_state_write.py
    # in_flight() (guard-742 dual-write).
    _gp = (args.goal_id or "").split("-")
    _asp = ("asp-" + _gp[1]) if len(_gp) >= 3 and _gp[0] == "g" and _gp[1].isdigit() else ""
    if args.title and _asp:
        _focus = _asp + ": " + args.title
    elif args.title:
        _focus = args.title
    else:
        _focus = _asp or args.goal_id

    #  sharding: the claim stamp lands in the agent's OWN row file.
    def _row_modifier(row):
        if not isinstance(row, dict):
            row = {}
        row["in_flight"] = {
            "goal_id": args.goal_id,
            "title": args.title,
            "claimed_at": now,
            "phase": args.phase,
        }
        row["last_active"] = now
        row["current_focus"] = _focus
        row["current_focus_updated_at"] = now
        return stamp_row_metadata(row, agent_author, now)

    locked_modify_yaml(row_path(WORLD_DIR, target_agent), _row_modifier,
                       initial=core_residual(TEAM_STATE_PATH, target_agent))
    print(f"in_flight set for {target_agent}: {args.goal_id} phase={args.phase}")

def cmd_clear_in_flight(args):
    """Remove the in_flight block from an agent's status. Bumps last_active.

    Symmetric to cmd_in_flight: completing/releasing/skipping a goal is also
    liveness evidence. Without this, the only writer of last_active is
    iteration-close.sh do_state_update — which fires on completed goals but NOT
    on release/skip paths, leaving last_active stale after a release.
    """
    _validate_agent_name(args.agent, "clear-in-flight")
    target_agent = args.agent
    agent_author = args.author or _agent_name()
    # Track whether any mutation happened so we can print the right message
    # AFTER the locked_modify_yaml call. Using a closure variable avoids
    # doing a second read just to check.
    status = {"cleared": False, "skipped_goal_id": None, "row_survived": False}

    #  sharding: clear operates on the agent's OWN row file. The
    # core_residual seed matters here: an un-migrated deployment whose
    # in_flight still lives in the core file gets its row seeded from that
    # residual first, so the pop below actually clears it in the composed
    # view (newest-wins prefers the freshly-stamped row).
    #
    # The modifier is SHARED with the daemon twin (guard-2323 / guard-547) and
    # carries the --if-goal compare-and-swap (guard-2474 clause 2, );
    # it runs inside locked_modify_yaml's lock, so the comparison is atomic
    # against a concurrent in-flight set on the same row file.
    #
    # The raw value is passed deliberately (): normalization lives in
    # the modifier so this twin and the daemon cannot disagree about what a
    # blank --if-goal means. This side never stripped, which is why it was the
    # accidentally-SAFE one — `--if-goal '  '` declined the CAS and preserved the
    # row, while the daemon's `or None` wiped it. Do not re-add normalization
    # here; a blank-but-supplied value must reach the modifier so it can raise.
    # sys.exit, NOT `return` — main() does `dispatch[args.command](args)` and
    # DISCARDS the return value, so a returned exit code would be swallowed and
    # the script would exit 0 on a caller bug. That is the same silent-success
    # shape this goal exists to remove. Matches the file's existing convention
    # (cmd_update / cmd_clear_body_row both sys.exit on bad input).
    try:
        _row_modifier = make_clear_in_flight_modifier(
            agent_author, if_goal=getattr(args, "if_goal", None), status=status)
    except ValueError as e:
        sys.exit(f"team-state clear-in-flight: {e}")

    locked_modify_yaml(row_path(WORLD_DIR, target_agent), _row_modifier,
                       initial=core_residual(TEAM_STATE_PATH, target_agent))
    if status["cleared"]:
        print(f"in_flight cleared for {target_agent}")
    elif status["skipped_goal_id"]:
        print(f"in_flight left alone for {target_agent}: row holds "
              f"{status['skipped_goal_id']}, not {args.if_goal}")
    elif status.get("row_survived") is True:
        # A row IS still standing — it just carried no goal_id to compare, so
        # the CAS declined it unverified. Falling through to "already absent"
        # here asserted the row was gone while it was not ().
        print(f"in_flight left alone for {target_agent}: row present but "
              f"carries no comparable goal_id (not cleared)")
    else:
        print(f"in_flight already absent for {target_agent}")

def cmd_migrate_shard(args):
    """One-shot cleanup: move core-file agent_status residuals into per-agent
    row files, then empty the core file's agent_status map (g-328-27).

    OPTIONAL — the sharded write path self-seeds each row from its core
    residual on first write, so correctness never depends on running this.
    What it buys: a clean core file (no permanently-stale residual rows
    confusing raw readers of the un-composed file). Idempotent; newest-wins
    on collision with an already-written row file.

    Durability note (guard-832): on an own-cloud deployment the new row
    files land on S3 via the next sweep/push — run a sync before treating
    the migration as fleet-visible.
    """
    agent = args.author or _agent_name()
    state = {}
    if TEAM_STATE_PATH.exists():
        with open(TEAM_STATE_PATH, "r", encoding="utf-8") as f:
            state = yaml.safe_load(f) or {}
    residuals = state.get("agent_status") or {}
    if not isinstance(residuals, dict) or not residuals:
        rows_dir(WORLD_DIR).mkdir(parents=True, exist_ok=True)
        print("migrate-shard: no core agent_status residuals — nothing to move")
        return
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    moved = []
    for name, entry in sorted(residuals.items()):
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            continue

        def _row_modifier(row, _entry=entry):
            if not isinstance(row, dict) or not row:
                return dict(_entry)
            # Row already exists — keep whichever snapshot is newer
            # (same comparison compose uses), so re-running the migration
            # can never roll a live row back to the stale residual.
            return row if _entry_ts(row) >= _entry_ts(_entry) else dict(_entry)

        locked_modify_yaml(row_path(WORLD_DIR, name), _row_modifier,
                           initial=dict(entry))
        moved.append(name)

    def _core_modifier(st):
        st["agent_status"] = {}
        return _stamp_metadata(st, agent)

    locked_modify_yaml(TEAM_STATE_PATH, _core_modifier, initial=dict(EMPTY_STATE))
    print(f"migrate-shard: moved {len(moved)} row(s) to {rows_dir(WORLD_DIR)}: "
          f"{', '.join(moved)}")
    print("migrate-shard: core agent_status emptied (composed reads now serve rows)")


def cmd_clear_body_row(args):
    """Remove agent_status.<agent>.in_flight_bodies.<sid>, sweeping null siblings.

    The dict-key REMOVE path g-306-186 found missing: the generic
    `--operation remove` is list-only, so clearing a body row could only be
    faked by setting it null, leaving one permanent null key per SID an agent
    had ever run. Delegates to the shared
    _team_state.make_clear_body_row_modifier (guard-742 parity by construction —
    the daemon endpoint builds the same modifier).

    Reachable by an operator as well as by the turn-end worker path, and that
    second caller is why this subcommand is not speculative: residue accumulated
    BEFORE the fix belongs to sessions that are gone, so it can only be drained
    by naming the agent and sid from outside. Passing a sid that is already
    absent is a supported no-op — the null-sweep still runs, so
    `--sid <anything>` drains a stale map.
    """
    _validate_agent_name(args.agent, "clear-body-row")
    if not args.sid:
        sys.exit("team-state clear-body-row: empty --sid")
    agent_author = args.author or _agent_name()
    status = {"removed": False, "nulls_swept": 0, "remaining": 0}

    # Nothing to clear from a shard that does not exist — and writing anyway
    # would CREATE it (guard-2611). Shared predicate, and it materializes the
    # shard before asking, so a partner's shard this box has never pulled does
    # not read as absent (; "shared" describes where the code lives,
    # not what it reads — it IS an .exists(), just not a bare one).
    if not body_row_shard_present(WORLD_DIR, args.agent):
        print(json.dumps({"ok": True, "agent": args.agent, "sid": args.sid,
                          "removed": False, "nulls_swept": 0, "remaining": 0,
                          "no_shard": True}, ensure_ascii=False))
        return

    _row_modifier = make_clear_body_row_modifier(agent_author, args.sid,
                                                 status=status)
    # No core_residual seed — see the daemon twin: in_flight_bodies is a
    # post-sharding field that never lived in the core file.
    locked_modify_yaml(row_path(WORLD_DIR, args.agent), _row_modifier)
    print(json.dumps({"ok": True, "agent": args.agent, "sid": args.sid,
                      "removed": status["removed"],
                      "nulls_swept": status["nulls_swept"],
                      "remaining": status["remaining"]}, ensure_ascii=False))


def cmd_retire_agent(args):
    """Sanctioned removal of an agent's team-state presence (core-file
    agent_status residual + per-agent shard), gated by archive-before-delete.
    The REMOVE path g-115-1909 found missing. Delegates to the shared
    _team_state.retire_agent (guard-742 parity by construction — the daemon
    endpoint calls the same function). Prints the JSON result."""
    _validate_agent_name(args.agent, "retire-agent")
    author = args.author or _agent_name()
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    result = retire_agent(WORLD_DIR, TEAM_STATE_PATH, args.agent, author, now,
                          source=args.source, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Write-boundary guards. Every write path validates here rather than trusting
# callers, because a shell caller with an unset $MIND_AGENT produces an
# empty-string agent name or a "agent_status..field" dot-path that would
# silently corrupt the YAML (create a "" key under agent_status). Fail loudly
# at the boundary so no new caller can reintroduce the gap.

def _validate_field_path(field):
    if not field:
        sys.exit("team-state: empty --field")
    if any(p == "" for p in field.split(".")):
        sys.exit(
            f"team-state: malformed --field {field!r} — empty segment "
            f"(likely unset env var like $MIND_AGENT)"
        )

def _validate_agent_name(agent, cmd_name):
    if not agent:
        sys.exit(
            f"team-state {cmd_name}: empty --agent "
            f"(likely unset env var like $MIND_AGENT)"
        )

def _set_nested(data, field, value):
    """Set a value at a dot-notation path, creating intermediate dicts."""
    parts = field.split(".")
    target = data
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            target[part] = {}
        target = target[part]
    target[parts[-1]] = value

def _append_nested(data, field, value):
    """Append a value to a list at a dot-notation path."""
    parts = field.split(".")
    target = data
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            target[part] = {}
        target = target[part]
    key = parts[-1]
    if key not in target or not isinstance(target[key], list):
        target[key] = []
    target[key].append(value)

def _remove_nested(data, field, value):
    """Remove an item from a list at a dot-notation path (by id or value match)."""
    parts = field.split(".")
    target = data
    for part in parts[:-1]:
        if not isinstance(target, dict) or part not in target:
            return
        target = target[part]
    key = parts[-1]
    if key not in target or not isinstance(target[key], list):
        return
    lst = target[key]
    # Remove by id if value is a string and items have id fields
    if isinstance(value, str):
        target[key] = [item for item in lst
                       if not (isinstance(item, dict) and item.get("id") == value)
                       and item != value]
    else:
        target[key] = [item for item in lst if item != value]

# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Shared team state management")
    sub = parser.add_subparsers(dest="command", required=True)

    # update
    update_p = sub.add_parser("update", help="Update a field in team state")
    update_p.add_argument("--field", required=True,
                          help="Dot-notation field path")
    update_p.add_argument("--value", required=True,
                          help="Value to set/append/remove (JSON or string)")
    update_p.add_argument("--operation", choices=["set", "append", "remove"],
                          default="set", help="Operation type (default: set)")
    update_p.add_argument("--author", help="Author name (defaults to MIND_AGENT)")

    # init
    init_p = sub.add_parser("init", help="Initialize team-state.yaml")
    init_p.add_argument("--author", help="Author name (defaults to MIND_AGENT)")

    # in-flight
    inflight_p = sub.add_parser("in-flight",
                                help="Mark agent as in-flight on a goal (auto-stamps claimed_at)")
    inflight_p.add_argument("--agent", required=True,
                            help="Agent name to mark in-flight (alpha, bravo, ...)")
    inflight_p.add_argument("--goal-id", required=True, help="Goal id (e.g., g-001-99)")
    inflight_p.add_argument("--title", required=True, help="Short goal title")
    inflight_p.add_argument("--phase", required=True,
                            help="Aspirations-loop phase number (e.g., 4)")
    inflight_p.add_argument("--author", help="Author name (defaults to MIND_AGENT)")

    # clear-in-flight
    clear_p = sub.add_parser("clear-in-flight",
                             help="Remove the in_flight block from an agent's status")
    clear_p.add_argument("--agent", required=True,
                         help="Agent name to clear (alpha, bravo, ...)")
    clear_p.add_argument("--if-goal", dest="if_goal", default=None,
                         help="Compare-and-swap: clear ONLY if the live "
                              "in_flight row names this goal_id. Omit for an "
                              "unconditional clear (recovery/retire/release).")
    clear_p.add_argument("--author", help="Author name (defaults to MIND_AGENT)")

    # clear-body-row () — the dict-key REMOVE path
    body_p = sub.add_parser("clear-body-row",
                            help="Remove one in_flight_bodies.<sid> entry "
                                 "(and any null siblings) from an agent's row")
    body_p.add_argument("--agent", required=True,
                        help="Agent name whose body row is being cleared")
    body_p.add_argument("--sid", required=True,
                        help="Session id keying the body row. Already-absent "
                             "is a supported no-op; the null-sweep still runs.")
    body_p.add_argument("--author", help="Author name (defaults to MIND_AGENT)")

    # migrate-shard ()
    migrate_p = sub.add_parser("migrate-shard",
                               help="One-shot: move core agent_status rows into "
                                    "per-agent row files (optional cleanup)")
    migrate_p.add_argument("--author", help="Author name (defaults to MIND_AGENT)")

    # retire-agent () — the sanctioned REMOVE path
    retire_p = sub.add_parser("retire-agent",
                              help="Remove an agent's team-state presence "
                                   "(core residual + shard), archive-before-delete gated")
    retire_p.add_argument("--agent", required=True,
                          help="Agent name to retire (e.g., charlie)")
    retire_p.add_argument("--source",
                          help="Free-text provenance recorded in the archive "
                               "(e.g., goal id / merge event)")
    retire_p.add_argument("--dry-run", action="store_true",
                          help="Report what would be removed without archiving "
                               "or deleting")
    retire_p.add_argument("--author", help="Author name (defaults to MIND_AGENT)")

    return parser

def main():
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "update": cmd_update,
        "init": cmd_init,
        "in-flight": cmd_in_flight,
        "clear-in-flight": cmd_clear_in_flight,
        "clear-body-row": cmd_clear_body_row,
        "migrate-shard": cmd_migrate_shard,
        "retire-agent": cmd_retire_agent,
    }
    dispatch[args.command](args)

if __name__ == "__main__":
    main()
