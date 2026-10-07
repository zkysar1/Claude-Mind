"""precheck-deferrable-battery — one call running every DEFERRABLE-tier precheck lane
under the budget meter, bounded in time and in output (g-115-8001, strangler step 3).

WHY THIS EXISTS. `iteration-open.sh` dispatches the always-run and medium tiers and
used to stop there, so every deferrable lane ran only when a model remembered to
hand-run it. Measured across five agents on five boxes (g-115-8001 progress note):
the tier executed in 4 of 14 iterations that reached it on one box (the other 10
were meter-dropped wholesale at zone tight because the precheck SKILL.md load itself
flips the zone), and when it did run, the cost was not script time but LLM context --
about 25,000 tokens of reducer context to read 44 raw outputs, in ~15 separate Bash
calls. This battery is the same move as precheck-medium-battery (guard-399
amendment 2: change WHO executes it, not the wording of an instruction): iteration-
open runs it as a STAGE inside its own meter window, so no election happens anywhere.

WHAT IT COSTS, MEASURED. Serial: 549-552 s on foxtrot (44-47 lanes); seven lanes were
78% of that (dropped-field-audit 141 s, stalled-goal-ratchet 90 s, displaced-id-audit
56 s, recurring-precondition-sweep 54 s, check-tests-no-live-agent-wm 40 s,
guardrail-pair-audit 29 s, narrative-clobber-audit 20 s). Two groups, run as three
independent hand-runners converged on: the mutating/state lanes (group A) run
SEQUENTIALLY in protocol order; the read-only audits (group B) run in PARALLEL, slowest
first, so the long lanes overlap the short ones. echo measured 41 lanes in ~90 s this
way (8 mutating in 10 s, 13 audits in 6 s, 20 ratchets/audits in 70 s); bravo 44 lanes
in ~62 s wall at parallelism 5.

BOUNDED, NOT HOPED-FOR. iteration-open kills a stage at 180 s and then reports the
WHOLE stage BLIND, which would discard every finding the lanes had already produced. So
this battery enforces its OWN wall-clock budget (`_BUDGET_S`): a lane not STARTED by the
deadline is reported under `dropped` (reason: over budget), and a lane still running at
the deadline is killed and reported BLIND (rc=124). Both are visible rows -- never a
silent omission (guard-1760) -- and both degrade `completeness`, so a slow box reads
`partial`, never `complete`. That is the bound g-115-7844 asked for (its --apply rc=124
on WSL2 came from an unbounded wrapper): the new stage cannot extend a run past its
own budget plus kill latency.

OUTPUT IS BOUNDED TOO. One lane (hardcoded-scope-audit) overflowed a 300 KB capture
cap and came back as unparseable JSON, blinding the whole hand-run. Each lane's stdout
is read up to `_OUT_CAP` bytes; a truncated JSON lane is BLIND with the cause named, and
only a short excerpt (never the raw body) enters the report.

THE GOAL-CLOSING SWEEPS RUN DRY. 0.5b.6 / 0.5b.7 / 0.5b.8 (parent-supersession-sweep,
unblock-parent-status-sweep, routing-audit-target-status-sweep) bare-replace a goal's
`outcome_note` (guard-4033: a 5,748 B note destroyed; `_compose_note` is still absent
from all three -- measured 0 / 0 / 0 on 2026-10-06 -- while monitor-stale-check has it).
precheck-medium-battery excludes them for the same reason and its docstring states the
admission predicate: "does it bare-replace a field another agent owns?". So they are
DISPATCHED here (their candidates are findings the reducer must see) but never given
`--apply`; the reducer applies them deliberately via the tier table's Invocation.

REDUCER-ONLY. The lanes' apply forms mutate shared reclaim state and the budget meter's
state file is agent-wide and syncable, so a worker Body gets an explicit SKIPPED report
(never a clean one). Same ruling as iteration-open._meter and precheck-medium-battery.

THE METER IS TOUCHED FROM ONE THREAD. `meter check` / `meter executed` are
read-modify-write on one JSON file, so a parallel lane calling them would lose records.
Every meter call happens on the scheduler (main) thread; workers only run subprocesses.

Every lane's stdin is /dev/null. A hand-runner's `while read ... done < table` loop let
one lane's command consume the loop's stdin and the LAST row never ran while the summary
looked complete (guard-4527 / guard-2044 / guard-3851). Every registered lane also gets
a row in `lanes[]`, and the report self-checks attempted-vs-registered, so a skipped lane
is a visible missing row rather than a quiet gap.

Output (guard-424 fail-loud-on-stderr; guard-614 structured on EVERY exit path):
  default -- one human line per lane with a finding, blind, dropped or held, then:
      [deferrable-battery] N finding / M lanes (mode=apply, completeness=complete, ...)
  --json  -- {checked_at, mode, status, completeness, lanes_registered, budget_s, jobs,
              elapsed_ms, findings, blind, dropped, held, uninterpreted, executed,
              lanes, skipped?, error?}

`status` and `completeness` stay ORTHOGONAL (guard-4093): `status` is whether anything
was found; `completeness` is whether every lane that was supposed to run was seen. A lane
with no finding spec is listed under `uninterpreted` with its signature and is NEVER
counted as clean.

Fail-open: any error prints the structured report (also to stderr) and exits 0. The
battery must never block the loop -- an entry gate that can refuse entry is worse than
the drift it corrects.

Invocation: `bash core/scripts/precheck-deferrable-battery.sh [--apply] [--json]
[--only a,b] [--skip a,b] [--budget-s N] [--jobs N] [--save-dir DIR]`.
"""

from __future__ import annotations

import argparse
import concurrent.futures as _cf
import datetime as _dt
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

_METER = "aspirations-precheck-budget-meter.sh"
# Total wall budget for the lane phase. iteration-open kills a stage at 180 s; this
# leaves room for the meter calls and the report under that cap.
_BUDGET_S = 100
# Parallel read-only lanes. echo used 3, bravo 5; 4 is the middle of two measured runs.
_JOBS = 4
# A lane is not worth STARTING with less than this left: it would be killed at once.
_MIN_START_S = 3
# Bytes of one lane's stdout the battery will PARSE. hardcoded-scope-audit's body measured
# 398 KB here (144 KB in the digest's day, 300 KB overflowed a hand-runner's capture), so
# the cap sits well above the largest measured lane; a lane over it is BLIND with the cause
# named, never read as empty. Only a projection or a short excerpt enters the report.
_OUT_CAP = 4_000_000
_SIG_CHARS = 160
_EXCERPT_CHARS = 400
_IDS_SHOWN = 8


# --- the registry -----------------------------------------------------------
# INLINE ON PURPOSE, same reasoning as the sibling batteries: one consumer, so a
# registry module would be the single-use abstraction implementation-discipline.md
# rule 3 forbids. `name` is the tier-table sweep name -- it is the coverage key AND
# the meter's `sweep_tier()` key, which is why it is never derived from the script.
#
#   group      "A" sequential in protocol order (state-changing lanes), "B" parallel
#              read-only audits scheduled slowest-first by `est_s`
#   args       the invocation WITHOUT any apply flag
#   apply_args appended only in --apply mode (a lane with none runs identically in both)
#   dry_args   appended only OUTSIDE --apply: the lane's own non-recording flag. A
#              ratchet's plain run RECORDS a baseline in meta/audit-baselines.yaml, so
#              a dry battery run must not give it the plain form
#   apply_only the lane has NO non-recording form (measured: its script has no
#              --dry-run), so outside --apply it is HELD rather than silently recording
#   timeout    hard per-lane cap, further clamped to the remaining budget
#   est_s      measured typical seconds, ONLY used to order group B (slowest first)
#   hold       the lane is registered and named but NOT run; the reason is printed
#   covers     tier-table rows this lane accounts for when it is not 1:1 (cadence)
def _lane(name, phase, script, *args, group="B", timeout=120, est_s=3,
          apply_args=(), dry_args=(), apply_only=False, hold=None, covers=None,
          meter=True):
    return {
        "name": name, "phase": phase, "script": script, "args": tuple(args),
        "group": group, "timeout": timeout, "est_s": est_s,
        "apply_args": tuple(apply_args), "dry_args": tuple(dry_args),
        "apply_only": apply_only, "hold": hold,
        "covers": tuple(covers) if covers else (name,),
        "meter_name": name if meter else None,
    }


LANES = (
    # --- group A: state lanes, protocol order -----------------------------------
    _lane("pending-questions-sweep", "0.5b.5", "pending-questions-sweep.sh", "sweep",
          "--all-agents", group="A", apply_args=("--apply",)),
    # guard-4033: dispatched DRY. See the module docstring.
    _lane("parent-supersession-sweep", "0.5b.6", "parent-supersession-sweep.sh",
          "--max-age-hours", "24", "--min-siblings", "2", group="A"),
    _lane("unblock-parent-status-sweep", "0.5b.7", "unblock-parent-status-sweep.sh",
          group="A"),
    _lane("routing-audit-target-status-sweep", "0.5b.8",
          "routing-audit-target-status-sweep.sh", group="A"),
    _lane("credential-defer-recheck", "0.5b.9", "credential-defer-recheck.sh",
          group="A", apply_args=("--apply",)),
    _lane("defer-drift-check", "0.5b.10", "defer-drift-check.sh", "--output", "json",
          group="A", apply_args=("--apply",)),
    _lane("reason-less-blocked-check", "0.5b.11", "reason-less-blocked-check.sh",
          "--output", "json", group="A", apply_args=("--apply",)),
    # The cadence battery owns its own seven-ritual registry and its own stateful
    # consecutive-FIRE counters, so it is ONE lane here that accounts for the seven
    # tier-table rows. It deliberately does not read the meter (its checks are cheap;
    # the meter gates the expensive SKILL invocation at dispatch time).
    _lane("cadence-battery", "0.5e", "precheck-cadence-battery.sh", "--json", group="A",
          meter=False,
          covers=("fresh-eyes-cadence", "fresh-eyes-program-cadence",
                  "fresh-eyes-tree-cadence", "strategic-scan-cadence",
                  "felt-sense-cadence", "curriculum-cadence", "evolution-cadence")),
    # self-acting board posts only under --apply, like the apply flag of its siblings
    _lane("l1-skew-cadence", "0.5g", "l1-skew-check.sh", "--cadence", group="A",
          apply_args=("--post-board",)),
    _lane("scar-tissue-cadence", "0.5g.5", "scar-tissue-check.sh", "--cadence",
          group="A", apply_args=("--post-board",)),
    _lane("completed-not-closed-cadence", "0.5g.6", "completed-not-closed-triage.sh",
          "--cadence", group="A", apply_args=("--post-board",)),
    # --- group B: read-only audits, parallel -------------------------------------
    _lane("blocked-signal-resolution-check", "0.5b.12",
          "blocked-signal-resolution-check.sh", "--post-routing", "--output", "json"),
    _lane("reclaim-defer-audit", "0.5b.13", "audit-deferred-defers.sh", "--output", "json"),
    _lane("reclaim-user-participant-audit", "0.5b.14", "audit-user-to-agent.sh",
          "--output", "json"),
    _lane("human-blocked-defer-join", "0.5b.15", "human-blocked-defer-join.sh",
          "--output", "json"),
    _lane("dependency-cycle-check", "0.5b.16", "dependency-cycle-check.sh",
          "--output", "json"),
    _lane("hypothesis-terminal-goal-check", "0.5b.17", "hypothesis-terminal-goal-check.sh",
          "--output", "json"),
    _lane("locus-sweep", "0.5b.18", "locus-sweep.sh", "--output", "json"),
    _lane("self-blocked-defer-sweep", "0.5b.19", "self-blocked-defer-sweep.py", "--json"),
    _lane("phantom-goal-audit", "0.5b.20", "phantom-goal-audit.py", "audit"),
    _lane("hardcoded-scope-audit", "0.5b.21", "hardcoded-scope-audit.py", "--json"),
    _lane("closed-against-own-note-check", "0.5b.22", "closed-against-own-note-check.sh",
          "--min-confidence", "high"),
    _lane("abandoned-claim-check", "0.5b.23", "abandoned-claim-check.sh"),
    _lane("recurring-precondition-sweep", "0.5c", "recurring-precondition-sweep.py",
          timeout=120, est_s=25, dry_args=("--dry-run",)),
    _lane("health-regression-cadence", "0.5h", "health-regression-check.sh", "--json"),
    _lane("check-stderr-json-merge", "0.5k.1", "check-stderr-json-merge.py"),
    _lane("check-uncommitted-edits-log-freshness", "0.5k.2",
          "check-uncommitted-edits-log-freshness.py"),
    _lane("role-multiplier-coverage-audit", "0.5k.3", "role-multiplier-coverage-audit.py"),
    _lane("verify-rb-type-parity", "0.5k.4", "verify-rb-type-parity.py"),
    _lane("hand-command-audit", "0.5k.5", "hand-command-audit.py"),
    _lane("check-agents-parent-dir-sync", "0.5k.6", "check-agents-parent-dir-sync.py"),
    _lane("fromisoformat-idiom-guard", "0.5k.7", "fromisoformat-idiom-guard.py"),
    _lane("hook-slot-contract-check", "0.5k.8", "hook-slot-contract-check.py"),
    _lane("narrative-clobber-audit", "0.5k.9", "narrative-clobber-audit.py",
          timeout=90, est_s=10),
    _lane("guardrail-pair-audit", "0.5k.10", "guardrail-pair-audit.sh",
          timeout=90, est_s=12),
    _lane("dropped-field-audit", "0.5k.11", "dropped-field-audit.py",
          timeout=170, est_s=60),
    _lane("unchecked-write-ratchet", "0.5k.12", "unchecked-write-ratchet.sh",
          apply_only=True),
    _lane("tree-last-updated-drift-check", "0.5k.13", "tree-last-updated-drift-check.py"),
    _lane("goal-field-census-ratchet", "0.5k.14", "goal-field-census-ratchet.sh",
          dry_args=("--dry-run",)),
    _lane("check-tests-no-live-agent-wm", "0.5k.15", "check-tests-no-live-agent-wm.py",
          timeout=90, est_s=15),
    _lane("embedded-python-audit", "0.5k.16", "embedded-python-audit.py"),
    _lane("tree-adjudication-scan", "0.5k.17", "tree-adjudication-scan.py"),
    _lane("displaced-id-audit", "0.5k.18", "displaced-id-audit.py", timeout=90, est_s=25),
    _lane("repo-hygiene-sweep", "0.5k.19", "repo-hygiene-sweep.sh", est_s=60,
          hold="held until g-115-8556 lands (guard-5885); the table Invocation is the "
               "manual path"),
    _lane("stalled-goal-ratchet", "0.5k.20", "stalled-goal-ratchet.sh",
          timeout=150, est_s=40, dry_args=("--dry-run",)),
    _lane("domain-term-ratchet", "0.5k.21", "domain-term-ratchet.sh",
          timeout=150, est_s=60, apply_only=True),
)

# Per-lane finding specs, kept apart from the invocation registry so adding a spec is a
# one-line data edit. Every entry below was read off the lane's REAL output in the
# 2026-10-06 survey (cc-15), not guessed from its name. Keys (all optional):
#   counts  int key, >0 is a finding        lists  list OR dict key, non-empty is a finding
#   ids     list key, non-empty is a finding AND the first ids are printed
#   false   bool key, FALSE is a finding    true   bool key, TRUE is a finding
#   zero    int key that must be >0 for the lane to have read anything (0 / absent = finding)
#   text    regexes over stdout+stderr; a matching line is a finding
#   rcs     return codes that ARE a finding (not a failure to run)
#   ok_rcs  extra return codes that are a clean no-op (documented, e.g. a silent noop)
#   project JSON keys kept for the signature (the raw body is far too large to carry)
#   census  the lane reports a population and has no finding semantics of its own
# Counts and lists accept a dotted path into a nested dict ("by_category.c").
# A lane with NO entry is reported under `uninterpreted`, never as clean.
#
# STANDING FINDINGS ARE EXPECTED. A lane whose finding is a standing population (e.g.
# reclaim-user-participant-audit's lanes) reports it every iteration until delta-against-
# the-last-reading lands (the follow-up this goal's progress note names: persist the
# last per-lane signature and print CHANGED rows first). One line per lane is still the
# 25,000-token reading it replaces.
_FINDS: dict = {
    # --- JSON lanes ---------------------------------------------------------------
    "blocked-signal-resolution-check": {
        "counts": ("all_resolved_count",),
        "lists": ("all_resolved", "disagreement", "dangling_ref", "undecidable",
                  "naive_would_unblock", "pq_unreadable_agents", "archive_read_failed"),
        "true": ("archive_degraded", "routing_cooldown_degraded"),
        "false": ("pq_corpus_complete",)},
    "check-stderr-json-merge": {"lists": ("flagged",)},
    "credential-defer-recheck": {"counts": ("cleared", "skipped_probe_fail"),
                                 "lists": ("would_clear",)},
    "defer-drift-check": {"counts": ("drift_count",), "lists": ("drifted", "uncovered_ids")},
    "dependency-cycle-check": {"counts": ("cycles_found", "dangling_count"),
                               "lists": ("cycles", "dangling_edges"),
                               "true": ("archive_degraded",)},
    "hardcoded-scope-audit": {
        # The digest's own projection: without `source _paths.sh` the world half of the
        # corpus is dropped and the verdict is SCANNED_PARTIAL (counts are a FLOOR).
        "zero": ("files_scanned",), "lists": ("roots_skipped",),
        "counts": ("tier_counts.active-scope",),
        "project": ("verdict", "files_scanned", "roots_skipped", "tier_counts")},
    "health-regression-cadence": {"true": ("tripped",)},
    "human-blocked-defer-join": {"counts": ("deterministic_count",),
                                 "lists": ("shared_premise_clusters", "errors")},
    "hypothesis-terminal-goal-check": {
        "counts": ("terminal_count",),
        "lists": ("hypothesis_terminal", "hypothesis_dangling", "stage_conflicts",
                  "pipeline_read_failed", "goal_read_failed"),
        "true": ("degraded",)},
    "locus-sweep": {"census": True},
    "parent-supersession-sweep": {"lists": ("candidates",)},
    "pending-questions-sweep": {"lists": ("flags",), "rcs": (1,)},
    "phantom-goal-audit": {"counts": ("live_phantoms",), "lists": ("flags", "phantoms"),
                           "false": ("schema_verified",)},
    "reason-less-blocked-check": {"counts": ("reason_less_count",),
                                  "lists": ("reason_less", "uncovered_ids")},
    # category c is "the actionable finding" in the lane's own words; unknown is a lane
    # that could not classify a defer.
    "reclaim-defer-audit": {"counts": ("by_category.c", "by_category.unknown")},
    # `actionable` / `candidates` are the lanes' own "work waiting" counters; the rest of
    # each lane dict is bookkeeping that is non-empty every iteration.
    "reclaim-user-participant-audit": {
        "counts": ("drop_lane.actionable", "promote_lane.candidates"),
        "lists": ("promote_lane.reclassified", "promote_lane.errors")},
    "routing-audit-target-status-sweep": {"lists": ("candidates",)},
    "self-blocked-defer-sweep": {"lists": ("self_blocked_candidates",)},
    "tree-adjudication-scan": {"lists": ("adjudicated",)},
    "tree-last-updated-drift-check": {"counts": ("desynced", "index_ahead", "index_stale"),
                                      "lists": ("errors",)},
    "unblock-parent-status-sweep": {"lists": ("candidates",),
                                    "true": ("archive_degraded",)},
    # --- text lanes -----------------------------------------------------------------
    "abandoned-claim-check": {"text": (r"^\s*RELEASABLE ",)},
    "check-agents-parent-dir-sync": {"rcs": (1,)},
    "check-tests-no-live-agent-wm": {"rcs": (1,)},
    "check-uncommitted-edits-log-freshness": {"rcs": (1,)},
    "closed-against-own-note-check": {"text": (r"flagged\(>=high\)=[1-9]",)},
    "completed-not-closed-cadence": {"text": (r"^\s*\[cnc-triage\](?!.*noop)",)},
    "displaced-id-audit": {"text": (r"^\s*DANGLING\s+[1-9]",
                                    r"STALE CITATIONS OF UNRELATED-CLASS IDS: [1-9]")},
    "domain-term-ratchet": {"text": (r"(?i)regress",), "rcs": (1,)},
    "dropped-field-audit": {"text": (r"\blive=[1-9]", r"unparseable=[1-9]",
                                     r"unreadable=[1-9]")},
    "embedded-python-audit": {"rcs": (1,)},
    "fromisoformat-idiom-guard": {"rcs": (1,)},
    "goal-field-census-ratchet": {"text": (r"(?i)regress",), "rcs": (1,)},
    "guardrail-pair-audit": {"text": (r"findings=[1-9]",)},
    "hand-command-audit": {"text": (r"handed to the user: [1-9]",)},
    "hook-slot-contract-check": {"rcs": (1,)},
    # a silent rc=1 / zero-output exit is this lane's documented noop; any output is a fire
    "l1-skew-cadence": {"text": (r"\S",), "ok_rcs": (1,)},
    "narrative-clobber-audit": {"text": (r"CLOBBERED=[1-9]",), "rcs": (1,)},
    "recurring-precondition-sweep": {"text": (r"skipped_on_error=[1-9]",)},
    "role-multiplier-coverage-audit": {"rcs": (1,)},
    "scar-tissue-cadence": {"text": (r"^\s*\[scar-tissue-check\](?!.*noop)",)},
    "stalled-goal-ratchet": {"text": (r"(?i)regress",), "rcs": (1,)},
    "unchecked-write-ratchet": {"text": (r"(?i)regress",), "rcs": (1,)},
    "verify-rb-type-parity": {"rcs": (1,)},
}


def covered_names():
    """Every tier-table sweep name this battery accounts for (held lanes included)."""
    out = []
    for lane in LANES:
        out.extend(lane["covers"])
    return tuple(out)


# --- execution --------------------------------------------------------------
def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _command(argv):
    """argv in registry form (script name first) -> the real command. `.sh` goes through
    the shared bash resolver (guard-580 / guard-581); `.py` through this interpreter."""
    script, rest = argv[0], list(argv[1:])
    if script.endswith(".py"):
        return [sys.executable, (SCRIPT_DIR / script).as_posix(), *rest]
    from _runtime_bash import bash_cmd

    return bash_cmd(SCRIPT_DIR / script, *rest)


def _run_script(argv, timeout):
    """Run one registry-form argv. Return (rc, stdout, err_or_None, stderr).

    err set == the lane could not be RUN to completion (timeout / spawn failure): a
    BLIND lane, never a clean one. On timeout the WHOLE process group is killed --
    subprocess.run's own timeout kills only the direct child, and a grandchild holding
    the pipe would then block the cleanup read past the budget this battery promises.
    stdin is /dev/null so a lane can never consume a caller's input.
    """
    kw = {"start_new_session": True} if os.name != "nt" else {}
    try:
        p = subprocess.Popen(
            _command(argv), cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            errors="replace", **kw)
    except Exception as exc:
        return None, "", f"{argv[0]}: {exc}", ""
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, None, err
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(p.pid, signal.SIGKILL)
            else:
                p.kill()
        except Exception:
            pass
        try:
            out, err = p.communicate(timeout=5)
        except Exception:
            out = err = ""
        return 124, out or "", f"{argv[0]}: timeout after {timeout}s", err or ""


def _is_worker_body(env=None):
    """True when this process is a worker Body -- the same BODY_ROLE signal as
    iteration-open._is_worker_body and precheck-medium-battery._is_worker_body."""
    e = env if env is not None else os.environ
    return (e.get("BODY_ROLE") or "").strip().lower() == "worker"


def _meter(action, runner, sweep=None):
    """Call the budget meter; None means RUN (a meter that cannot be reached must never
    silently disable a lane). Called ONLY from the scheduler thread -- the state file is
    one JSON document updated read-modify-write."""
    argv = [_METER, action] + ([sweep] if sweep else [])
    _res = runner(argv, 30)
    if _res[2] is not None:
        return None
    return (_res[1] or "").strip() or None


# --- judging one lane's result ------------------------------------------------
def _signature(payload, text):
    """One short line naming the shape of a result, so an uninterpreted lane is still a
    readable row. Scalars verbatim, containers by length."""
    if isinstance(payload, dict):
        parts = []
        for k, v in list(payload.items())[:14]:
            if isinstance(v, (bool, int, float)) or v is None:
                parts.append(f"{k}={v}")
            elif isinstance(v, str):
                if len(v) <= 24:
                    parts.append(f"{k}={v}")
            elif isinstance(v, (list, dict)):
                parts.append(f"{k}[{len(v)}]")
        return " ".join(parts)[:_SIG_CHARS]
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    return line[:_SIG_CHARS]


def _dig(payload, key):
    """payload[key], where key may be a dotted path into nested dicts (None if absent)."""
    cur = payload
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _spec_details(spec, payload):
    out = []
    for k in spec.get("counts", ()):
        v = _dig(payload, k)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            out.append(f"{k}={v}")
    for k in spec.get("lists", ()):
        v = _dig(payload, k)
        if isinstance(v, list) and v:
            out.append(f"{k}={len(v)}")
        elif isinstance(v, dict) and v:
            out.append(f"{k}={json.dumps(v, ensure_ascii=False)[:80]}")
    for k in spec.get("ids", ()):
        v = _dig(payload, k)
        if isinstance(v, list) and v:
            shown = ", ".join(str(x) for x in v[:_IDS_SHOWN])
            more = f" (+{len(v) - _IDS_SHOWN} more)" if len(v) > _IDS_SHOWN else ""
            out.append(f"{k}={len(v)}: {shown}{more}")
    for k in spec.get("false", ()):
        if _dig(payload, k) is False:
            out.append(f"{k}=False")
    for k in spec.get("true", ()):
        if _dig(payload, k) is True:
            out.append(f"{k}=True")
    # `zero`: a count that must be positive for the lane to have READ anything. Zero or
    # an ABSENT key is a read failure, not a clean census (guard-1091 / rb-245).
    for k in spec.get("zero", ()):
        v = _dig(payload, k)
        if v is None or (isinstance(v, int) and not isinstance(v, bool) and v == 0):
            out.append(f"{k}={v!r} (the lane read nothing)")
    failed = payload.get("failed")
    if isinstance(failed, list) and failed:
        out.append(f"failed={len(failed)}")
    return out


def _judge_cadence(payload):
    """The cadence battery's own report: each FIRED ritual is a finding that names the
    skill to dispatch, and an escalation is a louder one. The SKILL dispatch loop keeps
    ownership of the invocation (the battery's own design)."""
    out = []
    for e in payload.get("fired") or []:
        if isinstance(e, dict):
            out.append(f"CADENCE FIRE {e.get('name')} (phase {e.get('phase')}) -> "
                       f"{e.get('dispatch')}")
    esc = payload.get("escalation")
    if isinstance(esc, dict) and esc.get("dispatch_one"):
        d = esc["dispatch_one"]
        out.append(f"CADENCE ESCALATION: {d.get('name')} starved {esc.get('threshold')}x -- "
                   f"dispatch it UNCONDITIONALLY ({d.get('dispatch')})")
    return out


def _judge(lane, res):
    """Classify one completed lane run.

    Returns {kind: 'blind'|'ok', reason|detail, sig, uninterpreted, excerpt}.
    BLIND is anything that stops us from SEEING the lane's result: it could not run, it
    exited with a code the spec does not name, or its JSON could not be read. Never
    folded into a zero (guard-1091 / guard-4093).
    """
    rc, out, err, lane_err = res["rc"], res["out"], res["err"], res["stderr"]
    spec = _FINDS.get(lane["name"], {})
    tail = (lane_err or "").strip()[-300:]
    if err is not None:
        return {"kind": "blind", "reason": err + (f" | stderr: {tail}" if tail else "")}
    # A traceback is a lane that CRASHED, whatever its rc: for the lanes whose rc=1 means
    # "violations found", a crash is byte-identical to a finding unless it is named.
    if "Traceback (most recent call last)" in (out + "\n" + (lane_err or "")):
        return {"kind": "blind", "reason": f"lane raised an exception (rc={rc}) | stderr: {tail}"}
    finding_rcs = tuple(spec.get("rcs", ()))
    clean_rcs = (0,) + tuple(spec.get("ok_rcs", ()))
    if rc not in clean_rcs and rc not in finding_rcs:
        return {"kind": "blind",
                "reason": f"unexpected rc={rc}" + (f" | stderr: {tail}" if tail else "")}

    truncated = len(out.encode("utf-8", "replace")) > _OUT_CAP
    body = out[:_OUT_CAP]
    wants_json = lane["name"] == "cadence-battery" or any(
        k in spec for k in ("counts", "lists", "ids", "false", "true", "zero", "project")) \
        or "--json" in lane["args"] \
        or ("--output" in lane["args"] and "json" in lane["args"])
    payload = None
    if wants_json:
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
        except Exception as exc:
            why = (f"output exceeded {_OUT_CAP} bytes and was truncated -- the JSON is "
                   f"unreadable, not empty" if truncated
                   else f"unparseable output (rc={rc}): {exc}")
            return {"kind": "blind",
                    "reason": why + (f" | stderr: {tail}" if tail else "")}

    detail = []
    if lane["name"] == "cadence-battery" and payload is not None:
        detail = _judge_cadence(payload)
    elif payload is not None:
        detail = _spec_details(spec, payload)
    combined = body + "\n" + (lane_err or "")
    for rx in spec.get("text", ()):
        hits = [ln.strip() for ln in combined.splitlines() if re.search(rx, ln)]
        if hits:
            detail.append(f"{len(hits)} line(s) match /{rx}/: {hits[0][:120]}")
    if rc in finding_rcs:
        first = "" if payload is not None else next(
            (ln.strip() for ln in combined.splitlines() if ln.strip()), "")
        detail.append(f"rc={rc} (a finding for this lane) {first[:120]}".rstrip())

    interpreted = bool(spec) or lane["name"] == "cadence-battery"
    return {
        "kind": "ok", "detail": detail,
        "sig": _signature({k: payload.get(k) for k in spec["project"]}
                          if payload is not None and spec.get("project") else payload,
                          combined),
        "uninterpreted": not interpreted,
        "excerpt": "" if payload is not None else body.strip()[:_EXCERPT_CHARS],
    }


# --- the run -------------------------------------------------------------------
def _exec_lane(lane, runner, apply, deadline, clock):
    """Run one lane (thread-safe: no meter, no report mutation). Honors the deadline."""
    remaining = deadline - clock()
    if remaining < _MIN_START_S:
        return {"state": "over-budget"}
    argv = [lane["script"], *lane["args"],
            *(lane["apply_args"] if apply else lane["dry_args"])]
    t0 = clock()
    _res = runner(argv, max(1, min(lane["timeout"], int(remaining))))
    return {
        "state": "ran", "rc": _res[0], "out": _res[1] or "", "err": _res[2],
        "stderr": _res[3] if len(_res) > 3 else "",
        "ms": int((clock() - t0) * 1000),
    }


def _emit(report, as_json):
    if report.get("error"):
        print(f"[deferrable-battery] {report['error']}", file=sys.stderr)
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
        return
    for f in report.get("findings", []):
        print(f"▸ FINDING: {f['name']} (phase {f['phase']}) {'; '.join(f['detail'])}")
    for b in report.get("blind", []):
        print(f"▸ BLIND: {b['name']} (phase {b['phase']}) — {b['reason']}")
    for d in report.get("dropped", []):
        print(f"▸ DROPPED: {d['name']} (phase {d['phase']}) — {d['reason']}")
    for h in report.get("held", []):
        print(f"▸ HELD: {h['name']} (phase {h['phase']}) — {h['reason']}")
    for u in report.get("uninterpreted", []):
        print(f"▸ UNINTERPRETED: {u['name']} (phase {u['phase']}) ran, no finding spec — "
              f"sig: {u['sig']}")
    if report.get("skipped"):
        print(f"[deferrable-battery] SKIPPED — {report['skipped']}")
        return
    n_find = len(report.get("findings", []))
    n_reg = report.get("lanes_registered", 0)
    mode, comp = report.get("mode"), report.get("completeness")
    ran, held = len(report.get("executed", [])), len(report.get("held", []))
    tail = (f"mode={mode}, completeness={comp}, ran {ran}/{n_reg}, {held} held, "
            f"{report.get('elapsed_ms', 0) // 1000}s of {report.get('budget_s')}s budget")
    unseen = (len(report.get("blind", [])) + len(report.get("dropped", []))
              + len(report.get("uninterpreted", []))
              + sum(1 for h in report.get("held", []) if not h.get("standing")))
    if n_find == 0 and comp == "complete":
        print(f"[deferrable-battery] no findings in the {ran} lane(s) that ran ({tail})")
    elif n_find == 0:
        # guard-4093: this sentence must NOT read like "all clear".
        print(f"[deferrable-battery] NO FINDINGS REACHED — {unseen} lane(s) blind, dropped, "
              f"held or uninterpreted of {n_reg}, so this is NOT clean ({tail})")
    else:
        print(f"[deferrable-battery] {n_find} finding / {n_reg} lanes ({tail})")


def run(as_json=False, apply=False, runner=None, only=None, skip=None, budget_s=None,
        jobs=None, save_dir=None, clock=time.monotonic, env=None) -> int:
    """Run every registered deferrable lane the meter allows; report what happened.

    runner: injectable (argv, timeout) -> (rc, stdout, err_or_None, stderr) for tests;
    it is called for lanes AND for meter calls, from worker threads for group B lanes.
    """
    runner = runner or _run_script
    budget = _BUDGET_S if budget_s is None else budget_s
    n_jobs = max(1, _JOBS if jobs is None else jobs)
    report = {
        "checked_at": _now_iso(), "mode": "apply" if apply else "dry_run",
        "lanes_registered": len(LANES), "budget_s": budget, "jobs": n_jobs,
        "findings": [], "blind": [], "dropped": [], "held": [], "uninterpreted": [],
        "executed": [], "lanes": [],
    }
    if _is_worker_body(env):
        report["skipped"] = ("worker Body — the deferrable tier is reducer-side (its apply "
                             "forms mutate shared reclaim state and the meter state file "
                             "is agent-wide). Nothing ran; this is NOT a clean report.")
        report["completeness"], report["status"] = "partial", "clean"
        _emit(report, as_json)
        return 0

    errors = []
    t_start = clock()
    deadline = t_start + budget
    chosen = [l for l in LANES
              if (not only or l["name"] in only) and not (skip and l["name"] in skip)]
    to_run = []
    for lane in chosen:
        hold = lane["hold"] or ("apply-only lane: it has no non-recording form, so a dry "
                                "run does not run it" if lane["apply_only"] and not apply
                                else None)
        if hold:
            # `standing`: a registry decision (named hold), not a mode limitation, so it
            # does not turn an otherwise-complete run into a partial one -- but it stays
            # listed in every report (guard-1760: say what did not run).
            report["held"].append({"name": lane["name"], "phase": lane["phase"],
                                   "reason": hold, "standing": bool(lane["hold"])})
            report["lanes"].append({"name": lane["name"], "state": "held"})
            continue
        # HONORED, not telemetry: drop only when the meter says so (zone tight). None
        # (unreachable meter) means RUN. Scheduler thread only.
        decision = _meter("check", runner, lane["meter_name"]) if lane["meter_name"] else None
        if decision == "drop":
            report["dropped"].append({"name": lane["name"], "phase": lane["phase"],
                                      "reason": "budget meter returned drop"})
            report["lanes"].append({"name": lane["name"], "state": "dropped"})
            continue
        to_run.append(lane)

    group_a = [l for l in to_run if l["group"] == "A"]
    group_b = sorted((l for l in to_run if l["group"] == "B"), key=lambda l: -l["est_s"])

    def _record(lane, res):
        row = {"name": lane["name"], "state": res["state"]}
        if res["state"] == "over-budget":
            report["dropped"].append({
                "name": lane["name"], "phase": lane["phase"],
                "reason": f"over budget ({budget}s) — not started"})
            report["lanes"].append(row)
            return
        row.update({"rc": res["rc"], "ms": res["ms"],
                    "out_bytes": len(res["out"].encode("utf-8", "replace")),
                    "err_bytes": len((res["stderr"] or "").encode("utf-8", "replace"))})
        if save_dir:
            try:
                d = Path(save_dir)
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{lane['name']}.out").write_text(res["out"], encoding="utf-8")
                (d / f"{lane['name']}.err").write_text(res["stderr"] or "", encoding="utf-8")
            except OSError as exc:
                errors.append(f"save-dir: {exc}")
        verdict = _judge(lane, res)
        if verdict["kind"] == "blind":
            row["state"] = "blind"
            report["blind"].append({"name": lane["name"], "phase": lane["phase"],
                                    "reason": verdict["reason"]})
            errors.append(f"{lane['name']}: {verdict['reason']}")
            report["lanes"].append(row)
            return
        # The lane ran to completion and was read: THIS is when the execution record is
        # earned (guard-399 witness corollary) -- a blind or dropped lane did not execute.
        if lane["meter_name"]:
            _meter("executed", runner, lane["meter_name"])
        report["executed"].append(lane["name"])
        row["sig"] = verdict["sig"]
        report["lanes"].append(row)
        if verdict["detail"]:
            item = {"name": lane["name"], "phase": lane["phase"], "detail": verdict["detail"]}
            if verdict.get("excerpt"):
                item["excerpt"] = verdict["excerpt"]
            report["findings"].append(item)
        elif verdict["uninterpreted"]:
            report["uninterpreted"].append({"name": lane["name"], "phase": lane["phase"],
                                            "sig": verdict["sig"]})

    for lane in group_a:
        _record(lane, _exec_lane(lane, runner, apply, deadline, clock))
    if group_b:
        with _cf.ThreadPoolExecutor(max_workers=n_jobs) as pool:
            futures = {pool.submit(_exec_lane, l, runner, apply, deadline, clock): l
                       for l in group_b}
            for fut in _cf.as_completed(futures):
                lane = futures[fut]
                try:
                    res = fut.result()
                except Exception as exc:  # a crashed worker thread is a BLIND lane
                    res = {"state": "ran", "rc": None, "out": "", "stderr": "",
                           "err": f"{lane['script']}: worker raised {exc}", "ms": 0}
                _record(lane, res)

    # Verify the loop by ATTEMPTED count against the registry, never by failure count: a
    # lane whose command ate the loop's stdin once left a summary that looked complete.
    seen = {r["name"] for r in report["lanes"]}
    missing = [l["name"] for l in chosen if l["name"] not in seen]
    if missing:
        errors.append("lanes with no row: " + ", ".join(missing))
        for n in missing:
            report["blind"].append({"name": n, "phase": "?", "reason": "no result recorded"})

    report["elapsed_ms"] = int((clock() - t_start) * 1000)
    # guard-4093: two ORTHOGONAL fields. A held, dropped, blind or uninterpreted lane is a
    # lane we did not fully see, so each degrades completeness.
    report["completeness"] = ("partial" if (
        report["blind"] or report["dropped"] or report["uninterpreted"]
        or any(not h.get("standing") for h in report["held"])) else "complete")
    report["status"] = "findings" if report["findings"] else "clean"
    if errors:
        report["error"] = "lane_errors: " + "; ".join(errors)
    _emit(report, as_json)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--json", action="store_true", help="emit one JSON object")
    p.add_argument("--apply", action="store_true",
                   help="pass the apply flag to the lanes that take one (default: dry-run). "
                        "The loop entry path uses this; manual invocation usually should not.")
    p.add_argument("--only", default="", help="comma-separated lane names to run")
    p.add_argument("--skip", default="", help="comma-separated lane names to leave out")
    p.add_argument("--budget-s", type=int, default=None,
                   help=f"wall-clock budget for the lane phase (default {_BUDGET_S})")
    p.add_argument("--jobs", type=int, default=None,
                   help=f"parallel read-only lanes (default {_JOBS})")
    p.add_argument("--save-dir", default=None,
                   help="also write each lane's raw stdout/stderr here (survey use)")
    args = p.parse_args()
    split = lambda s: {x.strip() for x in s.split(",") if x.strip()} or None
    try:
        return run(as_json=args.json, apply=args.apply, only=split(args.only),
                   skip=split(args.skip), budget_s=args.budget_s, jobs=args.jobs,
                   save_dir=args.save_dir)
    except Exception as exc:  # fail-open: the battery must never block the loop
        rep = {"checked_at": _now_iso(), "mode": "apply" if args.apply else "dry_run",
               "lanes_registered": len(LANES), "findings": [], "blind": [], "dropped": [],
               "held": [], "uninterpreted": [], "executed": [], "lanes": [],
               "completeness": "partial", "status": "clean",
               "error": f"battery_failed: {exc}"}
        _emit(rep, args.json)
        return 0


if __name__ == "__main__":
    sys.exit(main())
