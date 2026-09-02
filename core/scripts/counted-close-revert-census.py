#!/usr/bin/env python3
"""counted-close-revert-census.py — goals counted CLOSED whose live status is NOT terminal.

A goal whose close does not persist is INVISIBLE (g-115-4302, surfaced by sq-013
during g-115-4182): it is counted done in working memory, carries no blocker,
appears in no blocked tally, and returns to the pool as normal-looking pending
work. Every instance so far was caught by accident, when the goal happened to
rank near the top and a reader recognised the title. One of them (alpha's
g-335-751, 2026-08-06) ranked #1 at 14.80 and was RE-EXECUTED by a worker Body
before anyone noticed — so the cost is duplicate work, not only invisibility.

  Bash: py -3 core/scripts/counted-close-revert-census.py [--json] [--file-investigate]

Exit codes — this is a DETECTOR, so a finding is not an error (same convention
as close-phase-skip-check.py):

  0  ran; read `status` for the verdict (`clean` or `findings`)
  2  could not run (query unreadable, no agents enumerable) — NOT a clean result

NOT the same check as close-phase-skip-check.py, and the two run in opposite
directions. That one starts from COMPLETED goals and looks for missing close
stamps (outcome_class / completed_by_role). A reverted goal reads `pending`, so
it is not in that script's population at all. Neither check can see the other's
finding, which is why this one had to exist rather than extend that one.

THREE FALSE-POSITIVE SOURCES, each MEASURED rather than hypothesised. Every one
must survive any future edit to this file:

 (1) FILTER ON `recurring`. Recurring goals return to status=pending on close BY
     DESIGN and never hold a terminal status. The original hand-run reported 90
     members; 84 of them were recurring. That is an rb-245-class predicate error
     and it recurs in any reimplementation that omits this filter. The filter is
     positive-controlled at runtime — see `recurring_in_corpus` below.
 (2) SEGREGATE not-in-active-store FROM reverted. Counted ids absent from the
     store entirely (23 in the original run) are expected for goals under
     archived aspirations. That is a DIFFERENT finding. Both counts are
     reported; neither is folded into the other, and neither is dropped.
 (3) DO NOT ATTRIBUTE VIA changelog.jsonl. It is machine_local=True while the
     aspirations store is machine_local=False, so it holds only the local box's
     writes and would name the local agent 100% of the time (guard-2133 /
     rb-6099). This script therefore reports WHICH AGENT COUNTED the goal — read
     directly from that agent's own working memory — and makes no claim
     whatever about who or what reverted it.

MEASURED ON THE FIRST LIVE RUN (2026-08-30, cc-07), two facts that change how
the output must be read:

  * `not_in_store` is STRUCTURALLY LARGE, not anomalous. The active store holds
    only ~520 goals in terminal statuses and archives the rest, so any counted id
    older than that window lands here — 374 of 470 on the first clean run. This
    is exactly why must-keep (2) segregates rather than merges: folded into
    REVERTED it would have reported 374 findings, none of them real.
  * `counted_goals_this_session` can carry TEST-FIXTURE RESIDUE. This box's body
    WM listed `g-test-001`, `g-999-03`, and similar alongside real ids. Those are
    absent from the store and land in `not_in_store`, which is the correct
    bucket — but do not read that bucket as a roster of lost work.

THE FIRST DRAFT OF THIS SCRIPT HAND-PARSED THE WORKING MEMORY and is why the
read now goes through `wm-read.sh`. It guessed the slot nesting, over-collected
702 ids against the wrapper's 470, matched ZERO healthy closes, and reported one
REVERT — `g-250-124`, attributed to an agent whose working memory does not
contain that id anywhere (it lives in a different agent's WM, in a different
slot). A fabricated finding of precisely the class this detector exists to catch.
The store's own read guard refused the hand parse and named the wrapper; it was
right. `terminal_ok` is the tell that would have caught it unaided: zero healthy
matches across hundreds of examined ids is self-refuting.

SCOPE: detection only. The mechanism behind the reverts is unresolved and lives
in g-115-4182's findings (own-cloud re-sync time-travel is the live candidate).
Detection is worth having independently of the cause, and it is what will tell
whoever fixes the cause whether the fix actually worked.

BODY WORKING MEMORIES ARE INCLUDED, and that is not decoration. A worker Body
counts its goals in `sessions/<SID>/working-memory.yaml`, not the agent-wide
file, and workers now perform most closes. An agent-wide-only sweep would miss
that entire population while reporting a clean result.

Cross-agent enumeration routes through `agents_root()` per CLAUDE.md "Agent-dir
Resolution" — a hand-written `PROJECT_ROOT / name` join silently scans NOTHING
after a layout change, and that has already zeroed three separate sweeps.
TERMINAL_GOAL_STATUSES is IMPORTED, never restated, so a new status added
upstream reclassifies here automatically instead of silently becoming a
"revert".
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _paths import agents_root  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402
from aspirations import TERMINAL_GOAL_STATUSES, VALID_GOAL_STATUSES  # noqa: E402

# : never hardcode the escalation aspiration —  is the UPSTREAM
# deployment's queue and does not exist elsewhere, so a literal files nothing
# (every add dies aspiration_not_found).
# GUARDED BY DESIGN, and the guard is not a guard-391 violation: that rule bans
# a SILENT fallback, while this arm LABELS its degradation and the label is
# printed at the filing site below (guard-1753 — a fail-open reader's verdict
# must be able to express its own failure). The try/except is additionally
# REQUIRED by test_escalation_target_imports_are_fail_open, because this
# detector runs on the iteration-close path where an ImportError would be worse
# than a labelled degradation. `agents_root` stays a BARE import above: it is a
# hard dependency of the census itself, and guarding it would let a _paths
# failure silently degrade the whole scan instead of crashing loud.
try:
    from _escalation_target import resolve as _resolve_asp, source_flag as _asp_source
    from _paths import AGENT_DIR, CORE_ROOT, WORLD_DIR  # noqa: E402
    ESCALATION_ASP, _ESCALATION_ASP_VIA = _resolve_asp(CORE_ROOT, WORLD_DIR, AGENT_DIR)
    ESCALATION_SOURCE = _asp_source(ESCALATION_ASP, WORLD_DIR, AGENT_DIR)
except Exception:
    ESCALATION_ASP, _ESCALATION_ASP_VIA, ESCALATION_SOURCE = (
        "asp-115", "fallback:import-failed", "world")

_ROOT = _HERE.parent.parent

# Derived, never restated — a status added upstream lands on the correct side
# here without an edit. NON_TERMINAL is the population a revert can hide in.
_NON_TERMINAL = sorted(set(VALID_GOAL_STATUSES) - set(TERMINAL_GOAL_STATUSES))
_TERMINAL = sorted(TERMINAL_GOAL_STATUSES)


def _query(statuses, full):
    """Run the framework query wrapper. Never a hand parse of the store file."""
    argv = ["core/scripts/aspirations-query.sh", "--goal-status", ",".join(statuses)]
    if full:
        argv.append("--full")
    proc = subprocess.run(bash_cmd(*argv), capture_output=True, text=True,
                          cwd=str(_ROOT))
    raw = proc.stdout or ""
    if not raw.strip():
        raise RuntimeError(
            "aspirations-query.sh returned EMPTY for statuses=%s (rc=%d, %d bytes). "
            "That is a malfunction, not an empty queue — refusing to launder it into "
            "a clean zero. stderr: %s"
            % (",".join(statuses), proc.returncode, len(raw), (proc.stderr or "")[:300]))
    data = json.loads(raw)
    return data if isinstance(data, list) else (data.get("goals") or [])


def _gid(row):
    return row.get("goal_id") or row.get("id")


def _load_live():
    """(suspects, terminal_ids, recurring_in_corpus).

    Two calls rather than one: `--full` is needed only for the non-terminal
    population (that is where `recurring` has to be read), and the terminal
    population is needed only as an ID SET, which the six-key default projection
    already carries. One --full call over every status would move ~15 MB per run
    to learn nothing extra.
    """
    suspects = {}
    recurring_in_corpus = 0
    for row in _query(_NON_TERMINAL, full=True):
        gid = _gid(row)
        if not gid:
            continue
        rec = bool(row.get("recurring"))
        recurring_in_corpus += rec
        suspects[gid] = {"status": row.get("status"), "recurring": rec,
                         "title": (row.get("title") or "")[:110]}
    terminal_ids = {_gid(r) for r in _query(_TERMINAL, full=False) if _gid(r)}
    return suspects, terminal_ids, recurring_in_corpus


# A SID that cannot name a real per-session dir. The daemon resolves the WM as
# "body WM if sessions/<unit_key>/working-memory.yaml exists, else agent-wide"
# (mind_api/src/agent_paths.py:wm_path), so a SID that cannot exist is the
# deterministic way to address the AGENT-WIDE file — including for an agent that
# also has a live body WM. Unsetting MIND_SID does NOT work: measured on this
# box it returns null rather than falling back.
_AGENT_WIDE_SENTINEL = "agent-wide-sentinel"


def _read_loop_state(agent, sid):
    """loop_state for one (agent, WM), through the framework wrapper.

    Never yaml.safe_load over the file. The store's own read guard names
    `wm-read.sh <slot> --json` as the wrapper, and its stated reason applies
    exactly here: "a hand parser over the raw file guesses the schema". The
    first draft of this script did guess, and guessed the slot nesting wrong.
    The cross-agent form (MIND_AGENT in the ENVIRONMENT, never in argv) is the
    probe CLAUDE.md sanctions for reading another agent's state.
    """
    import os
    env = dict(os.environ)
    env["MIND_AGENT"] = agent
    env["MIND_SID"] = sid
    proc = subprocess.run(bash_cmd("core/scripts/wm-read.sh", "loop_state", "--json"),
                          capture_output=True, text=True, cwd=str(_ROOT), env=env)
    if proc.returncode != 0:
        raise RuntimeError("wm-read.sh rc=%d for %s/%s: %s"
                           % (proc.returncode, agent, sid, (proc.stderr or "")[:160]))
    raw = (proc.stdout or "").strip()
    if not raw:
        return {}          # slot absent — a real empty, distinct from the rc!=0 above
    return json.loads(raw) or {}


def _counted_by_agent():
    """agent -> {wm-label: [counted goal ids]}, over agent-wide AND body WMs.

    Enumeration is a filesystem PRESENCE check (which WMs exist), which the
    store guard explicitly permits; every READ of contents goes through
    `wm-read.sh`. `agents_root()` is mandatory here per CLAUDE.md "Agent-dir
    Resolution" — a hardcoded join silently enumerates nothing after a
    relocation, and a cross-agent glob is the exact consumer class that has
    already been zeroed that way three times.
    """
    root = agents_root()
    targets = []
    for wm in sorted(root.glob("*/session/working-memory.yaml")):
        targets.append((wm.parent.parent.name, "agent-wide", _AGENT_WIDE_SENTINEL))
    for wm in sorted(root.glob("*/sessions/*/working-memory.yaml")):
        targets.append((wm.parent.parent.parent.name,
                        "body:" + wm.parent.name[:8], wm.parent.name))
    if not targets:
        raise RuntimeError(
            "no working memories found under %s — refusing to report a clean "
            "census over an empty enumeration" % root)

    found = {}
    for agent, label, sid in targets:
        try:
            ls = _read_loop_state(agent, sid)
        except Exception as exc:      # an unreadable WM is NOT an empty one
            found.setdefault(agent, {})[label] = {"__error__": str(exc)[:140]}
            continue
        ids = ls.get("counted_goals_this_session") if isinstance(ls, dict) else None
        found.setdefault(agent, {})[label] = list(ids) if isinstance(ids, list) else []
    return found


def _classify(counted, suspects, terminal_ids):
    reverted, recurring_excluded, not_in_store, unreadable = [], [], [], []
    terminal_ok = 0
    for agent, per_wm in sorted(counted.items()):
        for label, ids in sorted(per_wm.items()):
            if isinstance(ids, dict) and "__error__" in ids:
                unreadable.append({"agent": agent, "wm": label,
                                   "error": ids["__error__"]})
                continue
            for gid in ids:
                hit = suspects.get(gid)
                if hit is None:
                    if gid in terminal_ids:
                        terminal_ok += 1
                    else:
                        not_in_store.append({"agent": agent, "wm": label,
                                             "goal_id": gid})
                elif hit["recurring"]:
                    recurring_excluded.append({"agent": agent, "wm": label,
                                               "goal_id": gid})
                else:
                    reverted.append({"agent": agent, "wm": label, "goal_id": gid,
                                     "live_status": hit["status"],
                                     "title": hit["title"]})
    return reverted, recurring_excluded, not_in_store, terminal_ok, unreadable


def _file_investigate(reverted, result):
    ids = ", ".join(sorted({r["goal_id"] for r in reverted}))
    agents = ", ".join(sorted({r["agent"] for r in reverted}))
    counts = {k: v for k, v in result.items()
              if k.endswith("_count") or k == "counted_total"}
    payload = {
        "title": "Investigate: %d goal(s) counted CLOSED but live status is non-terminal"
                 % len(reverted),
        "description": (
            "Filed automatically by core/scripts/counted-close-revert-census.py, "
            "the detector promoted in g-115-4302.\n\n"
            "MEMBERS: %s\nCOUNTED BY: %s\n\n"
            "Each was counted as closed in a working memory while its live record "
            "reads a NON-TERMINAL status. Recurring goals are already excluded (they "
            "return to pending on close by design) and ids absent from the store are "
            "reported separately, so these members are neither of those artifacts.\n\n"
            "ATTRIBUTION CAUTION: the agent named is WHO COUNTED the goal, read from "
            "that agent's own working memory. It is NOT a claim about who or what "
            "reverted it. Do not attribute via changelog.jsonl — it is machine-local "
            "while the aspirations store is not, so it will name the local agent every "
            "time (guard-2133 / rb-6099).\n\n"
            "BEFORE RE-EXECUTING ANY MEMBER, READ ITS outcome_note. g-335-751 was "
            "reverted, re-ranked #1, and re-executed by a worker Body that discovered "
            "the duplication only at the 'the test already exists' step.\n\n"
            "The mechanism is tracked in g-115-4182's findings (own-cloud re-sync "
            "time-travel is the live candidate). This detector is DETECTION ONLY; its "
            "value is making the population visible and telling whoever fixes the "
            "cause whether the fix worked.\n\nCensus at filing time: %s"
            % (ids, agents, json.dumps(counts))),
        "priority": "MEDIUM",
        "status": "pending",
        "category": "framework-hygiene",
        "work_class": "framework",
        "participants": ["agent"],
        "origin_signal": "investigate:counted-close-reverted",
    }
    proc = subprocess.run(
        bash_cmd("core/scripts/aspirations-add-goal.sh",
                 "--source", ESCALATION_SOURCE, ESCALATION_ASP),
        input=json.dumps(payload), capture_output=True, text=True, cwd=str(_ROOT))
    if proc.returncode == 0:
        # Surface the resolution label: a degraded resolve (config:stale,
        # fallback:*) must announce itself rather than read as a normal file.
        print("  filed Investigate goal into %s (%s): %s"
              % (ESCALATION_ASP, _ESCALATION_ASP_VIA,
                 (proc.stdout or "").strip()[-90:]))
    else:
        print("  FAILED to file Investigate (rc=%d): %s"
              % (proc.returncode, (proc.stderr or "")[:200]), file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--file-investigate", action="store_true",
                    help="file an Investigate goal when the reverted count is non-zero")
    args = ap.parse_args()

    try:
        suspects, terminal_ids, recurring_in_corpus = _load_live()
        counted = _counted_by_agent()
    except Exception as exc:
        print("counted-close-revert-census: CANNOT RUN — %s" % exc, file=sys.stderr)
        return 2

    reverted, recurring_excluded, not_in_store, terminal_ok, unreadable = _classify(
        counted, suspects, terminal_ids)

    result = {
        "status": "findings" if reverted else "clean",
        "reverted_count": len(reverted),
        "reverted": reverted,
        "recurring_excluded_count": len(recurring_excluded),
        "not_in_store_count": len(not_in_store),
        "not_in_store": not_in_store,
        "terminal_ok_count": terminal_ok,
        "counted_total": (terminal_ok + len(reverted) + len(recurring_excluded)
                          + len(not_in_store)),
        "working_memories_read": sum(len(v) for v in counted.values()),
        "unreadable_working_memories": unreadable,
        # Positive control for must-keep (1): if the corpus itself carries no
        # recurring goals, the exclusion filter matched nothing and a clean
        # result would be a predicate failure wearing the costume of a pass.
        "recurring_in_corpus": recurring_in_corpus,
        "live_non_terminal_rows": len(suspects),
        "live_terminal_ids": len(terminal_ids),
        "attribution_note": (
            "`agent` is WHO COUNTED the goal, read from that agent's own working "
            "memory. It is NOT a claim about who reverted it — changelog.jsonl is "
            "machine-local and would name the local agent 100% of the time "
            "(guard-2133 / rb-6099)."),
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("counted-close-revert-census: %s" % result["status"].upper())
        print("  counted ids examined     : %d  (%d working memories; live corpus "
              "%d non-terminal / %d terminal, %d recurring)"
              % (result["counted_total"], result["working_memories_read"],
                 len(suspects), len(terminal_ids), recurring_in_corpus))
        print("  terminal (healthy)       : %d" % terminal_ok)
        print("  recurring (excluded, (1)): %d" % len(recurring_excluded))
        print("  not in active store, (2) : %d" % len(not_in_store))
        for n in not_in_store:
            print("    %-14s counted-by %s/%s" % (n["goal_id"], n["agent"], n["wm"]))
        print("  REVERTED                 : %d" % len(reverted))
        for r in reverted:
            print("    %-14s %-12s counted-by %s/%s :: %s"
                  % (r["goal_id"], r["live_status"], r["agent"], r["wm"], r["title"]))
        if recurring_in_corpus == 0:
            print("  WARNING: zero recurring goals in the live corpus — the (1) filter "
                  "matched nothing. Treat this run's verdict as UNVERIFIED.")
        if unreadable:
            print("  UNREADABLE working memories: %d (unreadable is not empty)"
                  % len(unreadable))
            for u in unreadable:
                print("    %s/%s: %s" % (u["agent"], u["wm"], u["error"]))

    if reverted and args.file_investigate:
        _file_investigate(reverted, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
