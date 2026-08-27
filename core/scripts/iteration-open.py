"""iteration-open — the bash-driven loop ENTRY, twin of iteration-close ().

WHY THIS EXISTS. The framework already solved this shape for the BACK half of an
iteration: `iteration-close.sh --phase verify|state-update|learning-gate|
productivity-check` is bash-driven with a small LLM contribution, and
Skill(aspirations-verify/state-update/learning-gate) were invoked 0 times in 25h
without harm. The FRONT half is the outlier -- 41 lanes still driven by a
174 KB SKILL.md, invoked in 12 of 52 iterations over 25h. Measured on the cc-04
reducer transcript (session ed59f154, 6 days, 203 autocompacts): what survives
compaction is what a SCRIPT enumerates (orchestrator-entry-battery 95%,
precheck-sentinel-battery 80%), what dies is what the LLM must remember to READ
(loop digest 28%, Skill(aspirations-select) 12%). So the fix is not more prose
telling the LLM to run the lanes; it is one command that runs them and prints
what needs judgment.

COMPOSE, NEVER RE-IMPLEMENT -- there is exactly ONE lane registry per lane set.
This script runs NO lane directly. It dispatches the existing batteries, each of
which owns its own lanes:

    orchestrator-entry-battery.sh   entry checks (READ-ONLY by contract)
    precheck-sentinel-battery.sh    the 4 always-run lanes with no standalone script
    precheck-always-run-battery.sh  the 5 always-run lanes that do have one

Folding those registries in here would create a second definition of the same
lane set, which drifts the first time one side is edited and nothing fails when
it does (guard-2676, the no-transcription contract). The surface boundary was
worked out on the coordination board before this file existed
(msg-20260817-162235-alpha-5364 / ...-164703-alpha-5373).

THE TIER TABLE IS THE LANE SSOT, AND IT IS PARSED, NOT COPIED. `--dry-run` reads
the `| Phase | Sweep name | Tier | Invocation |` table out of
aspirations-precheck/SKILL.md and prints every row it finds. A lane added to that
table therefore shows up here immediately, and a lane this script does not yet
dispatch is printed as `not-yet-wired` rather than silently omitted -- that is the
loud diff the goal's check asks for. Copying the 41 rows into a Python tuple would
have made the diff silent, which is the failure this check exists to catch.

A ZERO-ROW PARSE IS AN ERROR, NEVER AN EMPTY TABLE (guard-1641/2421). If the
SKILL.md moves or its table is reformatted, `parse_tier_table` raises and the
report carries the reason. A registry that silently reads as empty would render
as "0 lanes, all clean" -- the exact unreachable-vs-empty collapse guard-4093
names, and the reason `status` and `completeness` stay orthogonal here as they do
in the always-run battery.

STAGE 1 IS THE ALWAYS-RUN 9, DELIBERATELY. The goal prescribes a strangler, not a
rewrite: ship the always-run tier first, confirm precheck-gap-check reads 0 across
3 compactions, then bring the 7 medium and 25 deferrable lanes under the meter.
So `coverage` in the report names wired vs unwired counts explicitly. Reporting
only what ran is guard-1760's blind spot; this script reports what it declined to
look at too.

WHY THE METER STAMPS MATTER MORE THAN THEY LOOK. `precheck-gap-check` reads
exactly the meter's `start` and `end` stamps. Measured on the reducer: `start`
fired ~105% of closes and `end` 82% -- start-without-end is the abbreviated-
precheck signature. A battery that runs the lanes but skips the stamps leaves the
banner firing forever; one that writes them clears it by construction.

Output (guard-424 fail-loud-on-stderr; guard-614 structured on every exit path):
  default -- a per-STAGE rc table first (so a stage that did not run is visible),
             then FINDINGS ONLY, then selection candidates, then the imperative:
      [iteration-open] NEXT ACTION REQUIRED: ...
  --json  -- {checked_at, mode, status, completeness, stages, findings, blind,
              coverage, candidates, error?}

Fail-open throughout: every stage is independently timed and trapped, a timeout
renders as rc=124 in the table, and the script exits 0 even when a stage dies. It
must never block the loop entry -- an entry gate that can refuse entry is worse
than the drift it corrects.

Invocation: `bash core/scripts/iteration-open.sh [--apply] [--json] [--dry-run]`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

TIER_TABLE_MD = (
    PROJECT_ROOT / ".claude" / "skills" / "aspirations-precheck" / "SKILL.md"
)

# The always-run battery's own slowest lane scanned 2,616 records in 7.9s; the
# sentinel battery and selector are comparable. 180s per STAGE leaves headroom
# for a cold daemon without letting one wedged stage hold loop entry open.
_STAGE_TIMEOUT_S = 180
_METER = "aspirations-precheck-budget-meter.sh"


# --- the stage registry ------------------------------------------------------
# Stages, NOT lanes. Each entry names a battery that owns its own lane registry;
# `covers` lists the tier-table sweep names that battery dispatches, and is used
# ONLY to compute coverage in the report -- never to run anything. Keeping it
# declarative means a newly-wired battery is a data edit here and a registry edit
# there, with the coverage arithmetic catching any disagreement between the two.
STAGES = (
    {
        "key": "entry-checks",
        "script": "orchestrator-entry-battery.sh",
        "args": ("--json",),
        "apply_flag": False,
        "covers": (),
        "note": "read-only by contract",
    },
    {
        "key": "sentinel-battery",
        "script": "precheck-sentinel-battery.sh",
        "args": ("--json",),
        "apply_flag": False,
        "covers": (
            "tree-debt-gate",
            "experience-archival-gate",
            "evolution-finalize-gate",
            "fresh-eyes-code-gate",
        ),
        "note": "the 4 always-run lanes with no standalone script",
    },
    {
        "key": "always-run-battery",
        "script": "precheck-always-run-battery.sh",
        "args": ("--json",),
        "apply_flag": True,
        "covers": (
            "inbox-alert-age-check",
            "user-blocker-escalation-check",
            "dependency-timeout-check",
            "handoff-aging-check",
            "completed-not-closed-drain",
            # Registered by the battery since  but absent from this
            # tuple until . The STAGES header promises "the coverage
            # arithmetic catching any disagreement between the two" — nothing
            # actually compared them, so the lane RAN every iteration while
            # `--dry-run` printed it unwired and not_yet_wired_count was inflated
            # by one. Now pinned by test_iteration_open_stage_registry_parity.
            "world-script-crlf-check",
        ),
        # Count re-derived from the tuple, never re-typed: the sibling battery's
        # own docstring records seven stale "five lanes" claims from doing that.
        "note": "the always-run lanes that have a standalone script",
    },
    {
        # Strangler step 2 (). The medium tier ran nowhere between
        # 2026-08-17 (the day this script landed and took over loop entry) and
        # this stage: 208h dark on cc-04, 94.3h on cc-02, with sweeps_ran ==
        # always_run_count on every row of the final 24h. The cause was NOT that
        # anyone disagreed the tail should run -- SKILL.md Step 0-open says to
        # resume at it in prose. The cause is that prose and a `Bash:` line are
        # the same enforcement class (guard-399 amendment 2): both need a model to
        # elect them, and the imperative below elects SELECTION instead. Moving
        # the call into a script the flow already runs is the only remedy that
        # changes WHO executes it. ~16.1 s for all 7 lanes, measured.
        "key": "medium-battery",
        "script": "precheck-medium-battery.sh",
        "args": ("--json",),
        "apply_flag": True,
        "covers": (
            "aspirations-recover-recurring",
            "monitor-stale-check",
            "precheck-eval",
            "blocker-recheck",
            "defer-recheck",
            "precondition-defer-recheck",
            "recurring-starvation-check",
        ),
        "note": "the 7 medium-tier lanes",
    },
)


def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def parse_tier_table(md_path=None):
    """Return the tier table's rows as dicts: {phase, sweep, tier, invocation}.

    The table is the lane SSOT. Raises on a zero-row parse rather than returning
    [] -- an empty registry that renders as "all clean" is the unreachable-vs-empty
    collapse this whole script is built to avoid (guard-1641/2421/4093).
    """
    p = Path(md_path) if md_path else TIER_TABLE_MD
    if not p.exists():
        raise FileNotFoundError(f"tier table source not found: {p}")

    rows = []
    # Header is `| Phase | Sweep name (for `meter check`) | Tier | Invocation ...`
    # Rows are matched on the TIER cell, which is a closed vocabulary -- far more
    # stable than counting pipes, since the invocation column contains pipes of
    # its own inside backticked commands.
    tier_re = re.compile(r"^\|([^|]+)\|([^|]+)\|\s*(always-run|medium|deferrable)\s*\|(.*)$")
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = tier_re.match(line.strip())
        if not m:
            continue
        rows.append({
            "phase": m.group(1).strip(),
            "sweep": m.group(2).strip(),
            "tier": m.group(3).strip(),
            "invocation": m.group(4).strip().rstrip("|").strip(),
        })
    if not rows:
        raise ValueError(
            f"tier table parsed to ZERO rows from {p} — the table moved or was "
            f"reformatted. Refusing to report an empty registry as a clean one."
        )
    return rows


def _run_bash(argv, timeout):
    """Run a core/scripts bash script. Return (rc, stdout, elapsed_ms, err_or_None).

    err set == the stage could not be RUN at all (timeout / spawn failure). That is
    a BLIND stage, not a clean one -- the caller must never fold it into a zero.
    """
    from _runtime_bash import bash_cmd  # guard-580 + guard-581

    full = bash_cmd(SCRIPT_DIR / argv[0], *argv[1:])
    t0 = time.time()
    try:
        r = subprocess.run(
            full, cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, r.stdout, int((time.time() - t0) * 1000), None
    except subprocess.TimeoutExpired:
        # rc=124 is the shell's timeout convention; printing it in the table is
        # the goal's explicit requirement that a lane rc != 0 is never swallowed.
        return 124, "", int((time.time() - t0) * 1000), f"{argv[0]}: timeout after {timeout}s"
    except Exception as exc:
        return None, "", int((time.time() - t0) * 1000), f"{argv[0]}: {exc}"


def _is_worker_body(env=None):
    """True when this process is a worker Body rather than the reducer.

    Same predicate and same signal as agent-watchdog.is_worker_body -- BODY_ROLE,
    injected by the PreToolUse bash hook and already consumed by six scripts. Read
    directly rather than imported because agent-watchdog is a 3k-line module whose
    import is not free; this is a one-line env read of an established signal, not a
    second definition of the role.
    """
    import os
    e = env if env is not None else os.environ
    return (e.get("BODY_ROLE") or "").strip().lower() == "worker"


def _meter(action, runner, sweep=None):
    """Call the budget meter. Telemetry only -- precheck-gap-check reads these
    stamps, so skipping them leaves its banner firing forever. Fail-open.

    REFUSED ON A WORKER BODY, and this is not a courtesy. The state file is
    `agents/<agent>/session/precheck-budget-state.json` -- agent-wide, and MEASURED
    SYNCABLE (owncloud_sync._is_machine_local returns False for it, checked
    2026-08-17 on cc-07). `end` UNLINKS that file. So a worker Body running this
    battery would destroy the REDUCER's in-flight meter session cross-box, and the
    damage lands on precheck-gap-check's stamps -- the very signal outcome 2 of
    this goal measures. Same family as the standing rule that a worker Body must
    never write the agent-wide working memory.

    This is the answer to the goal's design input (g), "check whether iteration-open
    applies to worker Bodies too; do not assume": the LANES are safe to run from
    either role, the agent-wide METER WRITE is not. A worker gets the findings and
    the selection candidates; the reducer additionally gets the stamps.
    """
    if _is_worker_body():
        return "skipped-worker-body"
    argv = [_METER, action] + ([sweep] if sweep else [])
    rc, out, _ms, err = runner(argv, 30)
    return None if err is not None else (out or "").strip() or None


def _findings_from(stage_key, payload):
    """Lift the findings a composed battery already computed. This script does not
    re-derive them: each battery owns the semantics of its own lanes, and a second
    interpretation here would be the duplicate-registry defect in another costume."""
    out = []
    if not isinstance(payload, dict):
        return out
    for f in payload.get("findings", []) or []:
        if isinstance(f, dict):
            name = f.get("name") or f.get("sentinel") or f.get("lane") or stage_key
            detail = f.get("detail") or f.get("reason") or f.get("message") or ""
            if isinstance(detail, list):
                detail = ", ".join(str(d) for d in detail)
            out.append({"stage": stage_key, "name": name, "detail": str(detail)})
        else:
            out.append({"stage": stage_key, "name": stage_key, "detail": str(f)})
    return out


def _blind_from(stage_key, payload):
    """Lift a battery's blind lanes -- AND its dropped ones.

    A budget-dropped lane did not run, so this script did not see what it would
    have found. The drop was deliberate; the resulting ignorance is not, and
    folding it into a `complete` report is precisely the guard-4093 collapse
    ("found nothing" rendering identically to "could not look"). Only the medium
    battery emits `dropped` today, so this is a no-op for every other stage.
    """
    out = []
    if not isinstance(payload, dict):
        return out
    for d in payload.get("dropped", []) or []:
        if isinstance(d, dict):
            out.append({
                "stage": stage_key,
                "name": d.get("name", stage_key),
                "reason": "dropped: " + str(d.get("reason", "budget meter")),
            })
        else:
            out.append({
                "stage": stage_key, "name": stage_key, "reason": f"dropped: {d}",
            })
    for b in payload.get("blind", []) or []:
        if isinstance(b, dict):
            out.append({
                "stage": stage_key,
                "name": b.get("name", stage_key),
                "reason": str(b.get("reason", "")),
            })
        else:
            out.append({"stage": stage_key, "name": stage_key, "reason": str(b)})
    return out


def _coverage(rows):
    """Which tier-table lanes this script currently dispatches, and which it does
    not. Reporting only what ran is guard-1760's blind spot -- name the rest."""
    wired = set()
    for s in STAGES:
        wired.update(s["covers"])
    by_tier = {}
    for r in rows:
        by_tier.setdefault(r["tier"], []).append(r["sweep"])
    unwired = [r["sweep"] for r in rows if r["sweep"] not in wired]
    return {
        "table_rows": len(rows),
        "wired": sorted(wired),
        "wired_count": len(wired),
        "not_yet_wired_count": len(unwired),
        "by_tier": {k: len(v) for k, v in sorted(by_tier.items())},
        "stage": "always-run + medium (strangler step 2 — deferrable follows)",
    }


def _emit(report, as_json):
    if report.get("error"):
        print(f"[iteration-open] {report['error']}", file=sys.stderr)
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
        return

    # (1) Per-stage rc table FIRST. A stage that did not run must be visible
    # before any findings are read (guard-1760).
    print("STAGE                 rc   elapsed   note")
    for s in report.get("stages", []):
        rc = "-" if s.get("rc") is None else str(s["rc"])
        print("%-20s %4s %7sms   %s" % (
            s["key"], rc, s.get("elapsed_ms", "?"), s.get("note", "")))

    cov = report.get("coverage") or {}
    if cov:
        print("\nCOVERAGE: %d/%d tier-table lanes dispatched (%s); %d not yet wired"
              % (cov.get("wired_count", 0), cov.get("table_rows", 0),
                 cov.get("stage", ""), cov.get("not_yet_wired_count", 0)))

    met = report.get("meter") or {}
    if met.get("start") == "skipped-worker-body":
        print("METER: stamps SKIPPED — worker Body. The state file is agent-wide "
              "and syncable, and `end` unlinks it, so writing it here would "
              "destroy the reducer's in-flight meter session cross-box. "
              "precheck-gap-check is a REDUCER-side measurement.")

    # (2) FINDINGS ONLY — silent for clean lanes.
    findings = report.get("findings", [])
    blind = report.get("blind", [])
    if findings or blind:
        print("\nFINDINGS")
        for f in findings:
            print("  ▸ %s/%s: %s" % (f["stage"], f["name"], f["detail"]))
        for b in blind:
            print("  ▸ BLIND %s/%s: %s" % (b["stage"], b["name"], b["reason"]))

    # (3) Selection candidates.
    cands = report.get("candidates")
    if cands is not None:
        if cands.get("all_blocked"):
            # Every goal is blocked. Surface the routing information the
            # all-blocked handler needs (blocked_count / by_reason) instead of
            # printing "0 candidate(s); top: (none)", which reads as a quiet
            # nothing-to-do and hides the fact that the queue is wedged.
            by_reason = cands.get("by_reason") or {}

            def _n(v):
                return v.get("count", 0) if isinstance(v, dict) else (v or 0)

            # Sort by count so the 5 shown are the LARGEST reasons, not the
            # first 5 in dict order -- and say so when the rest are elided. A
            # silently-truncated list reads as the complete picture, which is
            # exactly wrong when the operator is deciding how a wedged queue
            # got that way.
            ordered = sorted(by_reason.items(), key=lambda kv: -_n(kv[1]))
            shown, rest = ordered[:5], ordered[5:]
            top_reasons = ", ".join("%s=%s" % (k, _n(v)) for k, v in shown) \
                or "(none reported)"
            if rest:
                top_reasons += " (+%d more reason(s) not shown)" % len(rest)
            print("\nSELECTION: ALL BLOCKED — %s blocked goal(s); by_reason: %s"
                  % (cands.get("blocked_count", "?"), top_reasons))
            print("[iteration-open] every goal is blocked — route to the "
                  "all-blocked handler (Skill(aspirations-all-blocked)), not to "
                  "a claim.")
        else:
            top = cands.get("top") or "(none)"
            print("\nSELECTION: %s candidate(s); top: %s"
                  % (cands.get("count", "?"), top))
            # PREMISE-SUPERSESSION CHECK on the top candidate (gap-142, satisfied
            # by extension rather than by SKILL.md #130 against a 100-skill cap).
            # Placed HERE for the reason the medium-battery comment above gives:
            # prose and a `Bash:` line are the same enforcement class, so the only
            # remedy that changes WHO executes it is a call inside a script the
            # flow already runs. A goal's cited measurement is a premise with an
            # expiry; this surfaces the citations and their age at the one moment
            # the reader is deciding whether to claim.
            # FAIL-OPEN by contract: loop entry must never break on this.
            m = re.match(r'\s*(g-\d+-\d+)', str(top))
            if m:
                try:
                    r = subprocess.run(
                        [sys.executable,
                         str(SCRIPT_DIR / "premise_supersession_check.py"),
                         m.group(1), "--json", "--root", str(PROJECT_ROOT)],
                        capture_output=True, text=True, timeout=45)
                    # A NON-ZERO CHILD IS A FAILURE THE PARENT MUST VOICE
                    # ( fresh-eyes F-001). The child is LOUD BY
                    # CONTRACT — on any load failure it writes its diagnostic to
                    # STDERR and exits 2 with stdout EMPTY. Gating only on
                    # `stdout.strip()` therefore printed NOTHING on every such
                    # failure, and the `except` below never fires because
                    # subprocess.run itself succeeded. That is byte-identical to
                    # a dead call site — the precise failure this whole advisory
                    # exists to prevent, reproduced in its own caller. It is not
                    # hypothetical: the child's bare-`.sh` argv[0] (F-002, fixed
                    # alongside) makes rc=2 the PERMANENT state on any box where
                    # Windows CreateProcess cannot exec a shell script.
                    if r.returncode not in (0, 1) or not r.stdout.strip():
                        why = (r.stderr or "").strip().splitlines()
                        print("[premise-supersession] check FAILED (rc=%s) — the top "
                              "candidate's premise is UNVERIFIED, not clean.%s"
                              % (r.returncode,
                                 ("  " + why[0]) if why else ""))
                    else:
                        d = json.loads(r.stdout)
                        if d.get("verdict") == "RE-MEASURE-BEFORE-EXECUTING":
                            print("[premise-supersession] %s: filed %s (%sd ago) with "
                                  "%d cited measurement(s) — RE-MEASURE BEFORE EXECUTING."
                                  % (d["goal_id"], d.get("filed") or "?",
                                     d.get("age_days"), d["cited_measurement_count"]))
                            for c in d["cited_measurements"][:6]:
                                print("    cites: %s" % c)
                            if d.get("commits_touching_named_paths_since_filing"):
                                print("    %d commit(s) touched its named paths since "
                                      "filing — may already be remediated."
                                      % len(d["commits_touching_named_paths_since_filing"]))
                        elif d.get("own_record_fields_present"):
                            print("[premise-supersession] %s carries %s — read it before "
                                  "any scope reasoning (guard-2803)."
                                  % (d["goal_id"],
                                     ", ".join(d["own_record_fields_present"])))
                        else:
                            # THE QUIET BRANCH IS PRINTED ON PURPOSE ().
                            # Without this line the clean case emits nothing, which is
                            # byte-identical to the block never executing — and the
                            # handler below is fail-open, so a dead call site produces
                            # no error either. Measured: on the first live --apply entry
                            # after wiring, the top candidate was 0d old with no
                            # outcome_note, both branches above were correctly silent,
                            # and there was no way to tell working from dead. An
                            # advisory built to fight "green is its only observable
                            # state" must not itself have only one observable state.
                            print("[premise-supersession] %s: %dd old, %d cited "
                                  "measurement(s), no prior execution note — nothing to "
                                  "re-measure." % (d["goal_id"], d.get("age_days") or 0,
                                                   d.get("cited_measurement_count", 0)))
                except Exception as e:
                    # FAIL-OPEN, NOT SILENT. A bare `pass` here already hid one
                    # real defect during authoring (a missing import raised
                    # NameError and the check simply never fired, with green as
                    # its only observable state — guard-1977). Behaviour stays
                    # fail-open: loop entry is never blocked. But say so.
                    print("[premise-supersession] check did not run (%s: %s) — "
                          "the top candidate's premise is UNVERIFIED, not clean."
                          % (type(e).__name__, str(e)[:120]))

    # (4) The imperative, mirroring iteration-close's ═══ ITERATION COMPLETE ═══.
    n_find = len(findings)
    comp = report.get("completeness")
    if n_find == 0 and comp == "complete":
        verdict = "no findings; all dispatched lanes clean"
    elif n_find == 0:
        # guard-4093: this sentence must NOT read like an all-clear.
        verdict = ("NO FINDINGS REACHED — %d stage(s)/lane(s) blind, so this is "
                   "UNREACHABLE, not clean" % len(blind))
    else:
        verdict = "%d finding(s) need disposition" % n_find

    print("\n[iteration-open] ═══ ITERATION OPEN ═══ (%s)" % verdict)
    # Name the residue, ABOVE the imperative. The imperative must remain the
    # LAST line (test_terminal_line_is_the_next_action_imperative pins it as
    # the line that survives summarization), so this states what did not run
    # without displacing what to do next.
    #
    # It does not ASK for anything. This line does not ASK for anything -- asking is what
    # failed for 208h (guard-399 amendment 2: re-wording an instruction does not
    # change who executes it), and the always-run + medium tiers above now run
    # without being asked. What it prevents is the reader concluding, from an
    # imperative that mentions only SELECTION, that loop entry is COMPLETE. It is
    # not: the deferrable tier is still unwired, and a reader who wants those
    # lanes must invoke them deliberately (guard-1760 -- report what did NOT run).
    unwired = (cov or {}).get("not_yet_wired_count")
    if unwired:
        print("[iteration-open] NOT COVERED BY THIS ENTRY: %d tier-table lane(s) "
              "remain unwired (the deferrable tier). They did not run and nothing "
              "above reflects them — `--dry-run` lists them by name." % unwired)
    print("[iteration-open] NEXT ACTION REQUIRED: dispose the findings above "
          "(each becomes a goal, a defer, or an explicit no-op). THEN, AS THE "
          "REDUCER, RESUME aspirations-precheck AT ITS FIRST DEFERRABLE SWEEP — "
          "this entry ran the always-run AND medium tiers, but the deferrable "
          "tier is still unwired, so going straight to SELECTION silently skips "
          "it and the meter still reads sweeps_dropped=0 (g-115-7847). Only "
          "after that tail, claim from SELECTION and enter execution — "
          "Skill(aspirations-execute). A worker Body runs NO precheck tail: "
          "dispose, then worker-loop Phase 2 CLAIM.")


def _selection(runner):
    """goal-selector.sh candidates. Its wrapper already fails LOUD on the
    g-115-6146 silent-empty signature, so an empty stdout here is its error, not
    a legitimate 'no candidates' -- do not paper over it."""
    rc, out, _ms, err = runner(["goal-selector.sh"], _STAGE_TIMEOUT_S)
    if err is not None:
        return {"count": None, "top": None, "error": err}
    try:
        d = json.loads(out)
    except Exception as exc:
        return {"count": None, "top": None, "error": f"unparseable selector output: {exc}"}
    # goal-selector cmd_select emits TWO legitimate top-level shapes. Normal case:
    # a bare LIST of ranked candidates. ALL-BLOCKED case: a DICT carrying
    # all_blocked/blocked_count/by_reason (goal-selector.py cmd_select, the
    # collect_blocked branch). Rejecting the dict made this stage report an ERROR
    # in exactly the state whose signal the iteration most needs -- every goal
    # blocked -- and the all-blocked routing information was discarded with it
    # (; settled as a legitimate branch, NOT producer drift, by
    # ). Shape check precedes any type-specific .get() call (guard-3075).
    #
    # count 0 here is a MEASURED zero (the selector ran and found no eligible
    # candidates), and it must stay distinguishable from the count None returned
    # on every error path above and below -- a failed measurement is not a
    # measurement of zero (guard-1091). That is why this branch sets count and
    # never sets "error".
    if isinstance(d, dict) and d.get("all_blocked"):
        cands = d.get("candidates") or []
        return {
            "count": len(cands),
            "top": None,
            "all_blocked": True,
            "blocked_count": d.get("blocked_count"),
            "by_reason": d.get("by_reason"),
        }
    if not isinstance(d, list):
        # Still an error for genuinely unexpected shapes -- a dict WITHOUT
        # all_blocked is not a shape either producer branch emits.
        return {"count": None, "top": None, "error": f"expected a list, got {type(d).__name__}"}
    top = d[0] if d else None
    return {
        "count": len(d),
        "top": ("%s (%.2f) %s" % (top.get("goal_id"), top.get("score", 0.0),
                                  str(top.get("title", ""))[:70])) if top else None,
    }


def run(as_json=False, apply=False, runner=None, md_path=None) -> int:
    """Run every wired stage under the meter; report rc table, findings, candidates."""
    runner = runner or _run_bash
    report = {
        "checked_at": _now_iso(),
        "mode": "apply" if apply else "dry_run",
        "stages": [],
        "findings": [],
        "blind": [],
    }
    errors = []

    try:
        rows = parse_tier_table(md_path)
        report["coverage"] = _coverage(rows)
    except Exception as exc:
        # A registry we cannot read is BLIND, never empty.
        report["coverage"] = None
        errors.append(f"tier_table: {exc}")
        report["blind"].append({
            "stage": "tier-table", "name": "lane-registry", "reason": str(exc),
        })

    # Recorded, not discarded: a skipped meter must be VISIBLE in the report, or
    # a reader cannot tell "stamps written" from "stamps deliberately not written"
    # (guard-1760 — a battery that reports only what it did is the blind spot).
    report["meter"] = {"start": _meter("start", runner)}

    for stage in STAGES:
        argv = [stage["script"], *stage["args"]]
        if apply and stage["apply_flag"]:
            argv.append("--apply")
        rc, out, ms, err = runner(argv, _STAGE_TIMEOUT_S)
        row = {"key": stage["key"], "rc": rc, "elapsed_ms": ms, "note": stage["note"]}

        if err is not None:
            row["note"] = err
            report["blind"].append({
                "stage": stage["key"], "name": stage["script"], "reason": err,
            })
            errors.append(err)
            report["stages"].append(row)
            continue

        try:
            payload = json.loads(out)
        except Exception as exc:
            # Unparseable output is BLIND, never clean — the battery broke or
            # changed shape, and either way its findings were not seen.
            reason = f"unparseable output (rc={rc}): {exc}"
            row["note"] = reason
            report["blind"].append({
                "stage": stage["key"], "name": stage["script"], "reason": reason,
            })
            errors.append(f"{stage['key']}: {reason}")
            report["stages"].append(row)
            continue

        report["findings"].extend(_findings_from(stage["key"], payload))
        report["blind"].extend(_blind_from(stage["key"], payload))
        report["stages"].append(row)

    report["candidates"] = _selection(runner)
    if report["candidates"].get("error"):
        errors.append("selector: " + report["candidates"]["error"])

    report["meter"]["end"] = _meter("end", runner)

    # guard-4093: two ORTHOGONAL fields, never collapsed. ANY blind -> partial.
    report["completeness"] = "partial" if report["blind"] else "complete"
    report["status"] = "findings" if report["findings"] else "clean"
    if errors:
        report["error"] = "stage_errors: " + "; ".join(errors)

    _emit(report, as_json)
    return 0


def dry_run(as_json=False, md_path=None) -> int:
    """List every lane in the tier table with its wiring status, and the count.

    The count is what makes a missing lane a loud diff: it is derived from the
    table on every call, never from a copy kept here.
    """
    try:
        rows = parse_tier_table(md_path)
    except Exception as exc:
        print(f"[iteration-open] tier table unreadable: {exc}", file=sys.stderr)
        if as_json:
            print(json.dumps({"error": str(exc), "lanes": [], "lane_count": 0}))
        return 1  # a registry we cannot read is a hard error in dry-run

    wired = set()
    for s in STAGES:
        wired.update(s["covers"])
    lanes = [{**r, "wired": r["sweep"] in wired} for r in rows]

    if as_json:
        print(json.dumps({
            "lane_count": len(lanes),
            "wired_count": sum(1 for l in lanes if l["wired"]),
            "lanes": lanes,
        }, ensure_ascii=False))
        return 0

    print("PHASE            TIER         WIRED  SWEEP")
    for l in lanes:
        print("%-16s %-12s %-6s %s" % (
            l["phase"][:16], l["tier"], "yes" if l["wired"] else "-", l["sweep"]))
    print("\n[iteration-open] %d lanes in the tier table; %d dispatched by this "
          "battery (always-run + medium; the deferrable tier is the next "
          "strangler step)"
          % (len(lanes), sum(1 for l in lanes if l["wired"])))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--json", action="store_true", help="emit one JSON object")
    p.add_argument("--apply", action="store_true",
                   help="pass --apply to the batteries whose lanes escalate "
                        "(default: dry-run). The loop entry path uses this.")
    p.add_argument("--dry-run", action="store_true",
                   help="list every tier-table lane and its wiring status; run nothing")
    p.add_argument("--tier-table", default=None,
                   help="override the tier-table source path (tests only)")
    args = p.parse_args()

    if args.dry_run:
        return dry_run(as_json=args.json, md_path=args.tier_table)
    try:
        return run(as_json=args.json, apply=args.apply, md_path=args.tier_table)
    except Exception as exc:  # fail-open: loop entry must never be blocked
        rep = {
            "checked_at": _now_iso(), "mode": "apply" if args.apply else "dry_run",
            "stages": [], "findings": [], "blind": [], "coverage": None,
            "candidates": None, "completeness": "partial", "status": "clean",
            "error": f"iteration_open_failed: {exc}",
        }
        _emit(rep, args.json)
        return 0


if __name__ == "__main__":
    sys.exit(main())
