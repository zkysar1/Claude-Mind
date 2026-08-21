#!/usr/bin/env python3
"""peer-thread-relay-sweep — surface peer-deployment thread replies that never
reached the peer, and CLOSE the relay goals the peer has acknowledged.

Predicate, evidence, and the deliberate report-only posture on the OUTBOUND half
are all documented in _peer_thread_relay.py; read that first. This file is I/O:
it gathers the live world queue and the coordination board, hands them to the
pure sweep, prints, and — with `--close-acked` — closes the `peer_acked` bucket.

WHY THIS FILE MUTATES AT ALL, given the module's report-only stance: that stance
is about SENDING relays, which is box-dependent (peer reachability differs per
machine). Closing a relay goal on a peer's written ack is NOT box-dependent —
the coordination board is world-shared, every box reads the same posts — so the
g-115-5890 constraint does not reach it. And the cost of NOT closing is measured:
five peer-acked relay goals sat non-terminal for 5 days after omni's per-id
disposition, two were mailed to the user in the user-participant digest as open
asks of him, and he read that as being ignored on questions the peer had
answered within hours (2026-08-17). The close is the fix for that.

WHAT THE CLOSE IS AND IS NOT. It closes the RELAY ARTIFACT — the goal filed here
"so the directive is not dropped" (g-115-1538 conservation). It does not assert
the underlying work is done; that lives in the peer's world (guard-3824). The
progress_note written on close says exactly that and cites the ack post id(s),
so the evidence is on the record and not on anyone's say-so. Fail-soft per goal:
one close failing never stops the others, and every close attempt is reported.
Idempotent by construction: only non-terminal goals are in the population.

CALL SITE: precheck-eval.py `peer-thread-relay` subcommand, which runs in the
scripted precheck every iteration and passes `--close-acked`. g-115-5890 asks
for a call site because reclaim-routed-work.md is blunt about the alternative:
"a sweep with no call site is indistinguishable from a sweep that always returns
clean."

EXIT: 1 when the undelivered or ambiguous bucket is non-empty, 0 when clean —
mirroring precheck-eval's `sys.exit(1 if flags else 0)`. Non-zero means "look",
never "something broke". Closes do not affect the exit code.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))

from _peer_registry import load_env_registry  # noqa: E402
from _peer_thread_relay import sweep, status_keyed_control  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402

DEFAULT_BOARD_WINDOW = "2160h"   # 90d — relays are cited long after they are sent
ESCALATE_AGE_DAYS = 1.0


def _run(script, *args, timeout=90):
    """Run a core/scripts wrapper. guard-580: never a bare "bash" argv[0].

    On FAILURE with no stdout, fall back to stderr — that is where the
    wrappers put their refusal, so returning stdout alone reported every
    failure as `output: ""`. Measured 2026-08-20 (zeta, cc-02): the
    close_acked failure row for g-115-6985 read {rc: 1, output: ""} while
    the discarded stderr carried 12,088 bytes naming the actual refusal
    (uncommitted-work-gate, stranded_would_block). A failure reporter that
    drops the failure's own diagnostic is indistinguishable from a silent
    wrapper, and sends the reader hunting a phantom.

    Deliberately NOT `2>&1` and deliberately NOT applied on success:
    guard-1963 (never merge stderr into a captured data stream) — the
    substitution fires only when rc != 0 AND stdout is empty, i.e. when
    there is no payload to corrupt. load_goals/load_board discard the rc
    and json-parse this value, but both guard the parse and skip
    unparseable input, so their behaviour is unchanged (an empty string and
    a stderr blob both fail to parse and yield the same []).
    """
    try:
        p = subprocess.run(bash_cmd(script, *args), capture_output=True,
                           text=True, timeout=timeout)
        out = p.stdout
        if p.returncode != 0 and not (out or "").strip():
            out = p.stderr or ""
        return out, p.returncode
    except (OSError, subprocess.SubprocessError):
        return "", 1


def load_goals():
    """Live world goals. NOT the compact — it carries no origin_signal, so the
    predicate is uncomputable from it (verified 2026-08-12: compact goal keys
    have title but no origin_signal)."""
    out, _ = _run("core/scripts/aspirations-read.sh", "--source", "world", "--active")
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return []
    # `--active` emits a LIST; other subcommands emit a dict. Handle both rather
    # than assuming — assuming cost an AttributeError on this exact call today.
    asps = data if isinstance(data, list) else (data.get("aspirations") or [])
    return [g for a in asps for g in (a.get("goals") or [])]


def load_board(window):
    out, _ = _run("core/scripts/board-read.sh", "--channel", "coordination",
                  "--since", window, "--json")
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except (ValueError, TypeError):
            continue
    return rows


def local_roster():
    try:
        from _agents import get_active_agents
        return set(get_active_agents())
    except Exception:
        return set()


def self_env():
    try:
        from _paths import ENVIRONMENT_ID
        return ENVIRONMENT_ID
    except Exception:
        return None


def close_note(rec):
    """The progress_note appended on a peer-ack close. Says what the close IS
    (handoff confirmed by the peer, relay artifact retired) and what it is NOT
    (verification of the peer's work), and cites the evidence by message id so
    the close rests on the record rather than on the sweep's say-so."""
    return (
        "[peer-thread-relay-sweep --close-acked] CLOSED ON PEER ACK. %s (%s) cited "
        "this goal id on the local coordination board in %s. This goal was a RELAY "
        "artifact filed so a directive addressed to a peer deployment was not dropped; "
        "the peer's written citation is direct evidence the handoff landed, so the "
        "artifact is retired. NOT asserted: that the peer has finished or agrees with "
        "the underlying work — that belongs to the peer's world (guard-3824). Left open "
        "it would resurface in user-participant digests as an open ask of the user "
        "(measured 2026-08-17)."
        % (", ".join(rec.get("peer_ack_authors") or ["peer"]),
           rec.get("peer_env") or "peer",
           ", ".join(rec.get("peer_acked_via") or []))
    )


def close_acked(acked, goals_by_id, run=_run):
    """Close each peer-acked relay goal: APPEND the evidence note, then complete.

    APPEND IS LOAD-BEARING. `aspirations-update-goal.sh <id> progress_note ...`
    is a plain replace on the daemon side (aspirations_write.py: `goal[field] =
    value`), so writing the note alone would erase every prior note on the
    record — including the very relay/handoff evidence a reader needs. The
    existing note comes from `goals_by_id` (the queue already loaded for the
    sweep, so no second read) and is carried forward verbatim ahead of ours.

    Fail-soft PER GOAL — a refused or errored close is recorded and the loop
    continues, so one bad record cannot strand the rest (that is the defect this
    exists to fix, one level down). The status write is attempted only if the
    note write succeeded: a goal completed WITHOUT its evidence note is exactly
    the kind of unexplained close this sweep is meant to end. Returns
    {"closed": [...], "failed": [...]}; failures carry the wrapper's rc + output
    head. `run` is injectable so the mutation is testable without a daemon.
    """
    out = {"closed": [], "failed": []}
    for rec in acked or ():
        gid = rec.get("goal_id")
        if not gid:
            continue
        prior = str((goals_by_id.get(gid) or {}).get("progress_note") or "").rstrip()
        note = (prior + "\n\n" + close_note(rec)) if prior else close_note(rec)
        n_out, n_rc = run("core/scripts/aspirations-update-goal.sh", gid,
                          "progress_note", note)
        if n_rc != 0:
            out["failed"].append({"goal_id": gid, "step": "progress_note",
                                  "rc": n_rc, "output": (n_out or "")[:200]})
            continue
        s_out, s_rc = run("core/scripts/aspirations-update-goal.sh", gid,
                          "status", "completed")
        if s_rc == 0:
            out["closed"].append({"goal_id": gid, "via": rec.get("peer_acked_via")})
        else:
            out["failed"].append({"goal_id": gid, "step": "status",
                                  "rc": s_rc, "output": (s_out or "")[:200]})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--board-window", default=DEFAULT_BOARD_WINDOW)
    ap.add_argument("--close-acked", action="store_true",
                    help="close relay goals a peer has acknowledged by id on the local board")
    args = ap.parse_args()

    goals = load_goals()
    board = load_board(args.board_window)
    registry = load_env_registry()
    env = self_env()
    roster = local_roster()

    result = sweep(goals, board, registry, env, roster)
    result["self_env"] = env
    result["goals_read"] = len(goals)
    result["board_rows"] = len(board)
    result["status_keyed_control"] = status_keyed_control(goals, registry, env, roster)

    und, amb = result["undelivered"], result["ambiguous"]
    rel = result["relayed"]
    acked = result.get("peer_acked") or []

    # Close the peer-acked relay artifacts. Runs BEFORE the JSON/print split so
    # both output modes report the same closes; the empty-queue guard below is
    # not needed here because an unread queue yields no `peer_acked` entries.
    result["closed_acked"] = (close_acked(acked, {g.get("id"): g for g in goals})
                              if args.close_acked else {"closed": [], "failed": [],
                                                        "skipped": "flag not set"})

    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
        return 1 if (und or amb) else 0

    # A zero here has two causes — nothing stranded, or nothing READ. Say which,
    # so an empty queue can never be confused with an unread one (guard-1419).
    if not goals:
        print("peer-thread-relay: could NOT READ the world queue — this is not a clean result")
        return 1
    print("peer-thread-relay: %d inbound peer-thread goal(s) over %d live goals, %d board rows"
          % (result["scanned"], len(goals), len(board)))

    if und:
        aged = [r for r in und if (r.get("age_days") or 0) >= ESCALATE_AGE_DAYS]
        print("  /!\\ %d NOT RELAYED to the peer (%d aged >= %.0fd):"
              % (len(und), len(aged), ESCALATE_AGE_DAYS))
        for r in und:
            print("      %-13s [%s -> %s] %sd %-6s %s"
                  % (r["goal_id"], r["peer_agent"], r["peer_env"], r["age_days"],
                     r.get("priority") or "?", (r.get("title") or "")[:46]))
        print("      ACTION: relay via core/scripts/peer-board-post.sh, or post to the")
        print("      coordination board tagged relay + forward-to:<agent>@<env> + the goal id.")
        print("      This sweep NEVER relays: reachability is box-dependent (g-115-5890).")
    if amb:
        print("  /?\\ %d AMBIGUOUS (name is both local and a peer's) — not routed:" % len(amb))
        for r in amb:
            print("      %-13s %s" % (r["goal_id"], r["reason"]))
    if acked:
        # The peer's own written citation of the goal id — the strongest
        # evidence this side can hold, and the one form of receipt that IS
        # observable here (the peer answers on THIS board because the reverse
        # route is closed). matched beside scanned, per guard-3712.
        ca = result.get("closed_acked") or {}
        print("  %d PEER-ACKED (peer-authored post cites the id; %d peer posts scanned)"
              % (len(acked), result.get("peer_ack_posts_scanned") or 0))
        for r in acked:
            print("      %-13s acked by %s in %s"
                  % (r["goal_id"], ",".join(r.get("peer_ack_authors") or []),
                     ",".join(r.get("peer_acked_via") or [])))
        if ca.get("skipped"):
            print("      not closed: %s (pass --close-acked to retire these relay artifacts)"
                  % ca["skipped"])
        else:
            print("      closed %d, failed %d" % (len(ca.get("closed") or []),
                                                    len(ca.get("failed") or [])))
            for f in ca.get("failed") or []:
                print("      !! %-13s %s rc=%s %s"
                      % (f["goal_id"], f.get("step"), f.get("rc"), (f.get("output") or "")[:80]))
    if not und and not amb:
        # "relayed", never "delivered" — for the goals that have only OUR relay
        # tag as evidence. Receipt is observable here ONLY as a peer-authored
        # citation (the peer_acked bucket above); a relay tag we wrote proves a
        # relay was ISSUED and nothing more, so this line must not say "delivered".
        print("  no strand — all %d inbound peer-thread goal(s) are peer-acked or have "
              "a relay ISSUED (a relay tag is handoff evidence, not receipt)"
              % result["scanned"])

    # THE DELIVERY-GAP SPLIT (). Printed OUTSIDE the clean branch on
    # purpose: an unrouted relay is just as invisible when the run also has
    # strands, and it is the reader's only view of it. `relayed` says a relay
    # was ISSUED; this says how many of those issued relays notify NOBODY.
    unrouted = result.get("relayed_unrouted") or 0
    if rel:
        print("  %d relayed, %d of which route to NOBODY (no tag naming a known agent)"
              % (len(rel), unrouted))
        for r in rel:
            if r.get("routing_gap"):
                print("      %-13s via %s — %s"
                      % (r["goal_id"], ",".join(r.get("relayed_via") or []), r["routing_gap"]))
        if unrouted:
            print("      A bare `relay` tag STILL SATISFIES this check by design"
                  " (documented breadth, guard-3628) — this is a report, not a refusal.")
            print("      To make one route, add requires_action_by:<agent>[@<env-id>]."
                  " `forward-to:` does NOT route: board.py parses it to the agent"
                  " `forward-to:<name>`, which matches nobody.")

    # The falsifying control, printed every run rather than only in the test:
    # the claim "status-keying misses this population" stays checkable in prod.
    print("  control: a status=='pending' predicate would find %d of these %d"
          % (result["status_keyed_control"], result["scanned"]))
    return 1 if (und or amb) else 0


if __name__ == "__main__":
    sys.exit(main())
