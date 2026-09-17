#!/usr/bin/env python3
"""Stop-complete (quiesce) predicate: has this agent finished stopping on THIS box?

`agent-state: IDLE` is NOT stop-complete. `/stop` writes `agent-state` at
graceful-stop D1 and `agent-mode` at D7 (stop/SKILL.md:312), and the obligation
tail between them — consolidate, handoff, session-summary, board posts — keeps
writing world stores. MEASURED 2026-09-14 (g-372-08, the first window this
runbook was executed under): laptop **foxtrot** flipped IDLE at 19:03Z and wrote
world stores until **19:43Z**; cc-04 released its runner claim only at stop
completion. The cutover driver's `verify` read IDLE on every box and passed. The
delta copy's deep diff — not the verify — found the six mismatches.

This module is the decision half of the fix. :func:`decide` is pure so every
branch is testable without a fleet, a daemon, or a live window.

FAIL-SAFE DIRECTION — the whole design. This is a GATE in front of a destructive,
hours-long, human-scheduled operation, so it resolves EVERY ambiguity toward
REFUSE. That is the opposite default from :mod:`reducer_self_fence` (which holds
a healthy loop on a plumbing fault) and it is deliberate: a needless extra
terminal check costs minutes, a copy opened on a moving tree costs the window.
Only a fully-positive reading on every signal returns QUIESCED.

THE THREE SIGNALS, and what each is actually worth (this ordering is measured,
not the order the runbook prose lists them in):

  3. **A live `claude` process on the box** — LOAD-BEARING, and on a worker box
     it is the ONLY signal with teeth. The other two read agent-WIDE files, and
     `agents/*/session/` is gitignored (`.gitignore:149 **/session/`), so each
     box carries its own copy. Measured on cc-09 2026-09-17 while this very
     session was writing: `agent-state` read `IDLE` and `agent-mode` read
     `autonomous` (mode newer than state) — both file signals said "stopped"
     about a box that was mid-unit. A worker Body is exactly the writer a
     window must quiesce and exactly the one the file signals cannot see.
  2. **The runner claim** — decisive in ONE direction only. `runner-claim.sh
     status` rc=0 means SOMEONE holds a live RUNNING claim for this agent, which
     is positive evidence the agent has not stopped. rc=4 is NOT a tombstone:
     its contract is `ABSENT | NOT-RUNNING | STALE | REFUSE (unverifiable)`
     (measured, and carried in `reducer_self_fence`'s own rc table). Reading
     rc=4 as proof of stop-completion is fail-OPEN — a broken claim writer, a
     refusing daemon and a genuinely-released claim are one value. rc 1/2/3
     (daemon error / no daemon) are likewise no evidence of anything.
  1. **`agent-mode` newer than `agent-state`** — catches a stop caught BETWEEN
     D1 and D7 on this box. A stale `agent-mode` beside a fresh `agent-state`
     is a stop in flight; an absent one is a stop that never reached D7.

None of the three is sufficient alone. The conjunction is the predicate.

WHAT THIS DOES NOT COVER, deliberately: the DERIVED writers — orphan daemons
(`pgrep -f '[p]ython3 -m mind_api.src'` against `mind_api/state/daemon.pid`) and
host timers (`systemctl list-timers`, `schtasks`). Those are a per-MACHINE census
with no agent to key on, they are enumerated in the runbook's §14.8.1 item 2, and
folding them in here would let a green agent row read as a quiet machine. A
quiesced verdict from this module means "this AGENT has stopped on this box", never
"this machine is not writing".

Source: g-372-21 outcome 3 (re-scoped — the one-off driver that was to carry this
lived only in a session scratch dir and is gone from every box and every git ref).
Prose twin: `world/conventions/aws-exit-cutover-runbook.md` §14.8.1 item 1.
Related: rb-10943, guard-5660 (quiescence is not completion — never substitute a
no-growth reading for a terminal marker; the three signals above ARE terminal
markers, a store re-enumeration is a corroborator and never a replacement).
"""

QUIESCED = "quiesced"
WRITING = "writing"
UNKNOWN = "unknown"

# Only rc=0 authorizes opening the window. Every other value refuses, including
# the internal-error one — a gate that cannot evaluate itself must not pass.
RC = {QUIESCED: 0, WRITING: 1, UNKNOWN: 2}

CLAUDE_COMMS = ("claude", "claude.exe")


def live_claude_pids(proc_table, self_pid):
    """Foreign `claude` processes on this box — self's own tree excluded.

    `proc_table` is an iterable of `(pid, ppid, comm)`. Matching is on COMM, not
    on the command line: `pgrep -f claude` matches its own invocation and every
    command line that merely mentions the string. Comm-matching has the mirror
    hazard — the CLI execs its bundled search binary, which keeps comm
    `claude.exe` while carrying someone else's argv (measured on cc-09) — so the
    checker's OWN tree has to come out by ancestry, not by pid equality.

    Excluded: the nearest `claude` ancestor of `self_pid` and its whole subtree.
    Everything else counts. A checker running outside any claude process excludes
    nothing, which is the correct reading for a driver invoked over ssh.
    """
    parent = {}
    comm = {}
    for pid, ppid, cm in proc_table:
        parent[pid] = ppid
        comm[pid] = cm

    # Walk up from self to the nearest claude-comm ancestor (self included).
    root = None
    seen = set()
    cur = self_pid
    while cur in comm and cur not in seen:
        seen.add(cur)
        if comm[cur] in CLAUDE_COMMS:
            root = cur
            break
        cur = parent.get(cur)

    excluded = set()
    if root is not None:
        excluded.add(root)
        # Descendants of root, by repeated sweep — the table is small and this
        # needs no ordering assumption about pid numbering.
        changed = True
        while changed:
            changed = False
            for pid, ppid, _cm in proc_table:
                if ppid in excluded and pid not in excluded:
                    excluded.add(pid)
                    changed = True

    return sorted(pid for pid, _ppid, cm in proc_table
                  if cm in CLAUDE_COMMS and pid not in excluded)


def decide_agent(row):
    """Verdict for one agent on one box. Pure.

    `row` keys (any of which may be None, meaning "could not read"):
      agent        str
      agent_state  "IDLE" | "RUNNING" | other | None
      state_mtime  float | None   mtime of session/agent-state
      mode_mtime   float | None   mtime of session/agent-mode (None = absent)
      claim_rc     int   | None   exit code of `runner-claim.sh status`
      claude_pids  list  | None   foreign claude pids on this box
    """
    agent = row.get("agent")
    state = row.get("agent_state")
    state_mtime = row.get("state_mtime")
    mode_mtime = row.get("mode_mtime")
    claim_rc = row.get("claim_rc")
    pids = row.get("claude_pids")

    # ---- decisive WRITING, in the order a reader most needs to hear it ------
    if pids:
        return _v(agent, WRITING, "live claude process on this box: %s"
                  % ",".join(str(p) for p in pids), row)
    if claim_rc == 0:
        return _v(agent, WRITING, "runner claim is LIVE (rc=0) — the agent is "
                                  "still RUNNING somewhere in the fleet", row)
    if state == "RUNNING":
        return _v(agent, WRITING, "agent-state reads RUNNING on this box", row)
    if state_mtime is not None and mode_mtime is None:
        return _v(agent, WRITING, "agent-mode absent beside an agent-state — "
                                  "the stop never reached D7", row)
    if (state_mtime is not None and mode_mtime is not None
            and mode_mtime < state_mtime):
        return _v(agent, WRITING,
                  "agent-mode is STALE against agent-state (mode %.0f < state "
                  "%.0f) — a stop is in flight between D1 and D7"
                  % (mode_mtime, state_mtime), row)

    # ---- UNKNOWN: nothing decisive said writing, but a signal is unreadable --
    if pids is None:
        return _v(agent, UNKNOWN, "could not enumerate processes on this box", row)
    if claim_rc is None:
        return _v(agent, UNKNOWN, "could not run runner-claim.sh status", row)
    if claim_rc != 4:
        return _v(agent, UNKNOWN,
                  "runner-claim.sh status rc=%s (daemon error / no daemon) — no "
                  "evidence either way" % claim_rc, row)
    if state is None or state_mtime is None:
        return _v(agent, UNKNOWN, "could not read session/agent-state", row)
    if state != "IDLE":
        return _v(agent, UNKNOWN, "agent-state reads %r — not a state this "
                                  "predicate recognises" % state, row)

    # ---- every signal positive ---------------------------------------------
    # claim_rc == 4 is NECESSARY, never sufficient: it conflates released with
    # unverifiable. It passes here only alongside a quiet box and a D7-complete
    # mode write.
    return _v(agent, QUIESCED,
              "agent-state IDLE, agent-mode written after it, no live claim "
              "(rc=4), no foreign claude process on this box", row)


def _v(agent, verdict, reason, row):
    return {
        "agent": agent,
        "verdict": verdict,
        "reason": reason,
        "signals": {
            "agent_state": row.get("agent_state"),
            "state_mtime": row.get("state_mtime"),
            "mode_mtime": row.get("mode_mtime"),
            "claim_rc": row.get("claim_rc"),
            "claude_pids": row.get("claude_pids"),
        },
    }


def decide(rows):
    """Box-level verdict over per-agent rows. Pure.

    Precedence WRITING > UNKNOWN > QUIESCED: a decisive "still writing" is more
    useful to the operator than "a signal was unreadable", and both refuse. An
    EMPTY row set is UNKNOWN, never quiesced — nothing was measured.
    """
    results = [decide_agent(r) for r in rows]
    if not results:
        verdict = UNKNOWN
        reason = "no agents measured on this box — nothing was checked"
    elif any(r["verdict"] == WRITING for r in results):
        verdict = WRITING
        reason = "; ".join("%s: %s" % (r["agent"], r["reason"])
                           for r in results if r["verdict"] == WRITING)
    elif any(r["verdict"] == UNKNOWN for r in results):
        verdict = UNKNOWN
        reason = "; ".join("%s: %s" % (r["agent"], r["reason"])
                           for r in results if r["verdict"] == UNKNOWN)
    else:
        verdict = QUIESCED
        reason = "all %d agent(s) quiesced on this box" % len(results)
    return {"verdict": verdict, "rc": RC[verdict], "reason": reason,
            "agents": results}
