"""signal-liveness-canary — the THIRD member of the canary family (
outcome 3). Sibling of stale-sentinel-canary.py (set/clear, read from a WM slot
VALUE) and cadence-stale-canary.py (fire/noop, read from a check EXIT CODE).

WHAT IT WATCHES, and why it is a third sibling rather than a new mechanism.
g-318-156 asks: which fleet signals can no longer produce a non-clear answer?
A detector that has silently stopped being able to report renders IDENTICALLY to
a detector reporting "all clear" — that is the whole defect class. The two
existing canaries already count the set-for-N-runs shape; this one counts the
same shape over a third input: a per-signal LIVENESS ASSERTION.

THE PREDICATE IS PER-SIGNAL, AND THAT IS A DELIBERATE CORRECTION (recorded on
g-318-156 unit 3). The outcome-4 decision that authorised this canary framed the
predicate as "the answer did not CHANGE across N runs", polarity-agnostic. That
framing is wrong as stated and would false-positive on every healthy instrument:
a boolean health signal that is legitimately always-clear never changes its
answer either, so constancy alone cannot separate "healthy and quiet" from
"dead". What the siblings actually count is CONSECUTIVE RUNS STUCK IN A STATE
THAT SHOULD BE TRANSIENT. So each registered signal supplies its own assertion
naming what "this instrument can still report" means for it, and the canary
counts consecutive DEAD verdicts. The polarity-agnostic intent survives intact —
an assertion keys on the instrument's ability to report, not on the value it
reports, so it catches stuck-CLEAR and stuck-ALARM with one mechanism.

Mechanism (dead/live — the liveness analogue of the sentinel canary's set/clear):
  - Own state slot `slots.signal_liveness_canary` holds {signal_name: stuck_count}.
  - Each run, for every signal in SIGNALS:
      run its assertion
      if DEAD:  stuck_count += 1
      else:     stuck_count = 0
      if stuck_count >= threshold: file Investigate, reset to 0.
  - Threshold from config: signal_liveness.threshold_iterations (default 3),
    matching stale_cadence / stale_sentinel.

FAIL-OPEN ON AN UNEVALUATABLE ASSERTION, exactly as the cadence sibling treats a
check that could not run: an assertion returning None resets the counter and
surfaces the error. A broken assertion must never manufacture a stuck count —
that would make this canary the very always-ALARM defect g-318-156 unit 1 names
as the same class as always-CLEAR.

FIRST REGISTERED SIGNAL — the watchdog's own liveness (g-318-156 unit 1
"Finding A"). core/logs/watchdog-<agent>.jsonl is TRANSITION-ONLY, so a dead
watchdog and a quiet healthy one write the same thing: nothing. The evidence
that the watchdog still runs is the mtime of its prev-state snapshot
(agents/<agent>/session/watchdog-prev-state.json), which --tick rewrites every
pass, and unit 1 measured that NOTHING READS IT. This canary is that reader.

WHY IT RUNS ON THE REDUCER. A process cannot witness its own death, so the
assertion has to be made by something other than the watchdog. Wiring it beside
its two siblings in iteration-close do_productivity_check puts it in the
reducer's pass — the same placement argument WorkerStallProbe already won — and
a worker Body skips that phase by design.

Invocation: iteration-close.sh do_productivity_check() (fail-open), beside
stale-sentinel-canary.py and cadence-stale-canary.py.

Verification: tests/test_signal_liveness_canary.py drives run() with an injected
assertion-runner and asserts the Investigate fires at threshold, resets after,
never fires while a signal is live, and never fires on an unevaluatable
assertion.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import yaml
    from _paths import AGENT_DIR, CORE_ROOT, WORLD_DIR
    from _fileops import acquire_lock, release_lock
    from _gate_log import LIVENESS_PROBE_ENV
    from _runtime_bash import bash_cmd  # Windows-safe bash resolution ()
except Exception:
    sys.exit(0)

CANARY_SLOT = "signal_liveness_canary"

# Escalation target RESOLVED, never hardcoded ( / ) — this is a
# framework file that travels the promotion chain, so a deployment-specific
# aspiration id would break downstream or be clobbered by the next sync. Same
# resolution both siblings use.
try:
    from _escalation_target import resolve as _resolve_asp, source_flag as _asp_source
    ASP_ID, _ASP_VIA = _resolve_asp(CORE_ROOT, WORLD_DIR, AGENT_DIR)
    ASP_SOURCE = _asp_source(ASP_ID, WORLD_DIR, AGENT_DIR)
except Exception:
    ASP_ID, _ASP_VIA, ASP_SOURCE = "asp-115", "fallback:import-failed", "world"

DEFAULT_THRESHOLD = 3
# Re-file suppression window — mirrors both siblings. The stuck condition is
# INTERMITTENT (the post-fire reset re-trips every `threshold` runs while the
# signal stays dead), so without dedup this would file a byte-identical
# Investigate every few iterations.
DEDUP_HOURS = 168

# Freshness bound for artifact-mtime assertions. 6h is the fleet's documented
# "not silent" threshold (check-team-state-before-silent.md), chosen here rather
# than a tighter per-iteration bound because iteration wall-time is not fixed:
# measured worker unit gaps run 15-92 minutes (agent-watchdog's own comment), so
# anything tighter manufactures alarms on a merely-slow-but-healthy loop.
DEFAULT_STALE_MINUTES = 360.0


def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _read_threshold(override: int | None) -> int:
    if override is not None:
        return max(1, int(override))
    try:
        cfg_path = Path(CORE_ROOT) / "config" / "aspirations.yaml"
        if not cfg_path.exists():
            return DEFAULT_THRESHOLD
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return DEFAULT_THRESHOLD
    section = cfg.get("signal_liveness") or {}
    try:
        return max(1, int(section.get("threshold_iterations", DEFAULT_THRESHOLD)))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


# ─── Signal registry ──────────────────────────────────────────────────────
#
# Each entry declares ONE instrument and the assertion that says whether it can
# still report. Add a signal by adding a row here; the run loop is generic.
#
#   name       stable id, used as the WM counter key AND in origin_signal
#   what       one line: what this instrument is for
#   evidence   what artifact the assertion reads (named so a reader can re-run it)
#   assertion  () -> (dead: bool|None, detail: str)
#              dead True  == the instrument can no longer report (count it)
#              dead False == the instrument is demonstrably live (reset)
#              dead None  == could not evaluate (reset + surface the error)


def _assert_watchdog_prev_state() -> tuple[bool | None, str]:
    """Finding A ( unit 1): is agent-watchdog still running at all?

    core/logs/watchdog-<agent>.jsonl is TRANSITION-ONLY — it records changes, so
    a dead watchdog and a healthy quiet one both write nothing and render
    identically. watchdog-prev-state.json is rewritten by EVERY --tick whether or
    not anything changed, so its mtime is the one artifact that separates them.

    Returns dead=True when that snapshot is older than the staleness bound, i.e.
    the watchdog has not completed a tick in that window.
    """
    if AGENT_DIR is None:
        return None, "no agent bound"
    snap = Path(AGENT_DIR) / "session" / "watchdog-prev-state.json"
    try:
        if not snap.is_file():
            # ABSENT is not DEAD: a box whose watchdog has never ticked (a fresh
            # agent dir) would otherwise alarm on its first three closes. Report
            # unevaluatable and let the error surface.
            return None, f"no prev-state snapshot at {snap}"
        age_min = (_dt.datetime.now().timestamp() - snap.stat().st_mtime) / 60.0
    except Exception as exc:
        return None, f"stat failed: {exc}"
    if age_min > DEFAULT_STALE_MINUTES:
        return True, (
            f"watchdog-prev-state.json is {age_min:.0f} min old "
            f"(bound {DEFAULT_STALE_MINUTES:.0f} min) — no --tick has completed in that window"
        )
    return False, f"watchdog-prev-state.json is {age_min:.0f} min old — tick is live"


# ─── The "clear covers never-examined" family ( unit 6) ───────────
#
# S-3/S-4/S-5 and V-1..V-5 of the goal's inventory collapse to ONE shape, which is
# why they share one helper instead of getting eight bespoke assertions: an
# instrument whose SUCCESS channel covers BOTH "examined and clean" and "never
# examined at all". The liveness assertion for every member is the same act — drive
# the instrument with an input whose correct answer is NON-CLEAR, and ask whether it
# still said so. An instrument that comes back CLEAR on that input has stopped being
# able to report, which is the whole defect class.
#
# EVERY ROW BELOW WAS DRIVEN IN BOTH DIRECTIONS BEFORE IT WAS WRITTEN (alpha/cc-08,
# 2026-09-13); the per-assertion docstrings carry the numbers. Outcome 1 of the goal
# asks for "a positive control run, not a reading of the code", and a registry row
# whose predicate was never driven both ways is itself an always-reports-clear
# instrument — exactly what this canary exists to catch.
#
# THE INVENTORY MEMBERS BELOW ARE DELIBERATELY NOT REGISTERED HERE, and saying so is part
# of the row set rather than an omission. Together with the rows, this block IS the
# written inventory  outcome 1 asks for: every instrument that goal verdicted is
# either a row in SIGNALS or an entry here with its verdict, how it was checked, and why
# it has no row. Each verdict's full evidence is in the goal's rotated note,
# world/audit-reports/goal-note-archive/.progress_note.md, under the
# [appended:<marker>] the entry names. No member count is written here on purpose: a
# count restated beside its list goes stale silently. Count the entries.
#   heartbeat-tick.sh's agent-state=IDLE refusal (exit 2). It is the last of the ten
#        product-path gates  named, and the only one left unrowed. Measured
#        2026-09-24 (bravo, cc-05) from the script:
#        - The refusal runs only AFTER two daemon-routed team-state publishes,
#          core_hooks_path and retrieval_index. Each fires whenever the agent dir
#          lacks a fresh, equal stamp.
#        - The daemon writes the LIVE world whatever env the probe runs in.
#        - AGENTS_PARENT_DIR is hard-coded (_paths.sh), so no agent root can be
#          redirected.
#        So a contained probe needs a throwaway agent dir under the LIVE agents/
#        parent with pre-seeded stamps. Any pre-gate write added later would then
#        turn this canary silently into a live-state polluter.
#        The side-effect-free refusal (MIND_AGENT empty) guards hook injection, a
#        different contract. A row on it would read alive while the IDLE gate was
#        dead. The mechanism needed is a probe seam in heartbeat-tick that evaluates
#        the SAME gate code before any write, which is a product change, not a
#        cadence probe.
#   S-4  `aws s3api list-objects-v2` under a denied s3:ListBucket. Same shape, but
#        its assertion needs a network call and live credentials on every reducer
#        iteration, and a canary that flakes on a VPN blip manufactures the
#        always-ALARM twin of this defect. Verdict recorded in the inventory; the
#        mechanism it needs is a caller-side rule (no enumeration, no delete), not a
#        cadence probe.
#   S-5 / V-5  `q4-provenance-sample.sh` read by exit code (rc=0 covers pass AND
#        skipped). NOTHING DEGRADES here — it is a stable design property, documented
#        in the script's own header. A liveness canary counts an instrument going
#        dead; this instrument was never alive in that channel. The mechanism it
#        needs is a caller-side check that no call site branches on its rc without
#        reading the verdict line.
#   V-8  `closure-evidence-write.sh`. CANNOT via its exit code, by CONTRACT: it always
#        exits 0 and its header tells callers not to branch on rc. CAN via stdout, where
#        its never-clobber decline is announced (observed live, alpha cc-10, marker
#        alpha-unit5-cc10-verify-close-instrument-chain-20260912T2310). The same case as
#        S-5/V-5: a stable design property, not something that can go dead.
#   V-7  `dependent-unblock.sh`. CANNOT: when the audit-trail stamp (unblocked_by,
#        unblocked_summary) fails, `skipped[]` stays empty, and the only trace is
#        stamp_ok=false inside a success row. Measured twice (same marker as V-8). This is
#        a write-side defect, not a liveness question: follow-up .
#   F9   The worker deep-close delivery pair. Alpha measured both halves on cc-07
#        (marker g318156-close-gates-verdict-uncommitted-residual-alpha-cc07-20260913):
#        - gates/uncommitted_work.py, called from the daemon's _uncommitted_work_eval in
#          aspirations_write.py. CAN: UW-1..UW-6 were driven both ways. But
#          iteration-close.sh do_verify auto-sets the uncommitted override on every
#          completed deep close by a WORKER Body, so the refusal never reaches that path.
#        - worker_execute.py git_ref_delivery. It compares the remote ref to HEAD only,
#          so it CAN report an unpushed commit (stranded) and CANNOT report an
#          uncommitted edit (verified).
#        Both were re-read at HEAD on 2026-09-24, unchanged. The blindness is structural,
#        not a degradation. The relay is msg-20260914-035310-alpha-6867. It has no goal
#        of its own and is held in aged-trigger triage .
#   V-10 `wm-read.sh` called with a flag it does not accept. CANNOT on 2026-09-12: the
#        call returned a clean empty. REPAIRED since, by . Re-probed 2026-09-24
#        (bravo, cc-05):
#          `--slot <name>` -> rc=2, 0 B stdout, 318 B stderr
#          positional      -> rc=0 and the value
#        A caller that drops stderr AND ignores rc still reads empty. That is the
#        stderr-only-refusal family; the aspirations-query-refusal-channel row watches
#        the same shape in a sibling script.
#   HEALTHY, each verdicted by a two-direction positive control. None is rowed: a row
#   watches a death that would otherwise be silent, and for each of these either no
#   such death has been measured or no decision reads its output.
#     gates/deadline_date.py (DD-1..6), which shows live block and pass traffic.
#     gates/prose_verification.py (PV-1..7). Its 10-day run of zeros is explained by
#       its population, shown by a 52-goal census.
#     gates/residual_work.py (RW-1..5). It has no auto-override path, so its refusal
#       still reaches deep closes.
#       Alpha, cc-07; markers g318156-seven-gate-verdicts-alpha-cc07-20260914 and F9's.
#     `verify-check-eval.sh` (V-6). The reference shape: `all_passed: null` is its own
#       third value for "nothing was verified". Caveat: string checks read as
#       checks_total 0, and `has_string_checks` is what tells them apart.
#     worker_reducer_liveness.py. Its pure decide() was driven across 7 branches
#       (marker alpha-unit1-cc07-preamble-instrument-chain-20260912T1105), and
#       test_reducer_self_fence.py pins it against its mirror module.
#     `email-read.sh check-alerts` (S-1). It announces its own clipping as a lower
#       bound, so the risk is on the reader's side (marker
#       unit-signal-inventory-6-rows-stderr-only-refusal-class-alpha-cc08-20260912).
#     gates/defer_scope.py. A total classifier and NOT A GATE: no decision reads it,
#       so its death would change nothing.
#     The open exposure these share is the gate-decision-collapse family: when a gate's
#     non-clear decisions stop entirely, nothing reacts. That relay is
#     msg-20260914-035309-alpha-6866, also held in triage .
#   WATCHED THROUGH ANOTHER ROW, listed only so a census keyed on names does not count it
#   as unwatched: gates/blocker_create.py, via blocker-create-gate-schema-probe.
#   blocker-create-gate.py is a thin wrapper over gates.blocker_create.evaluate, and the
#   row's fixture check reads the module's _STAT_NEG_PATTERNS. Its one UNKNOWN is WIRING,
#   not liveness: CREATE_BLOCKER reaches the gate by honor system only.
#   core/config/execute-protocol-digest.md names just the bare add-goal command at Phase
#   4.0 and 4.1e (re-read 2026-09-24).
#   WRITER-WITHOUT-READER family, swept 2026-09-24 (bravo, cc-05; marker
#   bravo-g318156-writer-without-reader-family-cc05-20260924). A store whose writer is
#   live but that NO DECISION reads is this canary's class seen from the other side. Its
#   non-clear answers are written and reach nobody, so the fleet always reads it as clear.
#   A liveness row would watch the writer, and the writer is not the defect.
#     meta/missing-verification-criteria.jsonl, the Q1.5 uncovered-gap log. CANNOT, as a
#       fleet signal. The writer is live: 379 records, 281 of them this month, read from
#       the store of record. Its only references are the writer, the writer's wrapper, the
#       verify call site, a digest line and the merge handler. None of them decides
#       anything. The 2026-08-21 WIRE-IT verdict only ever existed as prose. Follow-up:
#       . Its positive control is broken separately, owned by .
#     meta/step-attribution.yaml. Attribution, not detection, so NOT a signal. Verdict:
#       REDUCE (2026-08-21). The writer is discretionary: digest Step 8.11 writes only "if
#       a score is particularly high/low". So a dark writer and a quiet one look the same,
#       and nothing decides on either. Measured: dark since 2026-08-09, and its per-step
#       map was never populated.
#     Census SCREEN of 217 top-level world+meta store files: 100 have at most one
#       non-plumbing reference. Positive control: the census found evolution-log's
#       decision reader. This is a screen, not verdicts, for two reasons:
#       - A basename grep cannot tell a writer from a reader.
#       - A store whose name is BUILT at runtime reads zero references. That covers the
#         date shards and at least four logs written in the last two days.
#       So "zero references" is not "no reader". Detector-shaped hits for the next unit:
#       goal-selector-anomalies, defer-drift-metrics, precheck-eval-log,
#       post-state-update-suppressions. Members that already have owner goals:
#       , , , .

# A title no goal can carry. Used to prove the READ channel still emits an answer;
# the point is a legitimately-EMPTY result, so this must never match. Only titles are
# searched, so prose mentioning this string elsewhere cannot break it.
QUERY_NO_MATCH_SENTINEL = "zzz-signal-liveness-canary-no-match-sentinel"
# A flag that can NEVER become valid, so the refusal row has no false-positive mode.
# Deliberately not a real-but-retired flag like --source: were that flag ever
# re-accepted, rc would go to 0 and this row would alarm on a healthy script.
QUERY_NEVER_VALID_FLAG = "--zzz-canary-not-a-flag"
PROBE_TIMEOUT_S = 45.0

# Built at runtime so this file never contains the literal marker token — see
# _assert_marker_placement_gate_denies for why that matters.
_MARKER_TOKEN = "domain-leak" + "-exempt:"
_CANARY_PROBE_NODE = "zzz-signal-liveness-canary-probe.md"


def _probe(argv: list[str], stdin_text: str | None = None,
           drop_env: tuple[str, ...] = (),
           extra_env: "dict[str, str] | None" = None) -> tuple[int, str, str]:
    """Run an instrument and return (rc, stdout, stderr). Raises if it cannot run.

    `stdin_text` exists for the PreToolUse-hook gates, which take their whole
    input as JSON on stdin rather than in argv. Default None keeps every
    pre-existing call byte-identical.

    `drop_env` names variables the CHILD must not inherit — for a row whose probe
    writes, so a regression cannot aim the write at live state (see
    _pending_obligation_assertion). Default () keeps every other call byte-identical.

    `extra_env` sets variables in the CHILD only — for a row that points its probe at a
    throwaway world through the gate's own override seams (_domain_suite_gate_assertion).
    Default None keeps every other call byte-identical.

    Every probe carries LIVENESS_PROBE_ENV so the probed gate does not log its
    must-trip refusal as a production firing (g-318-168, _gate_log docstring).
    extra_env cannot unset it: the marker is applied last.
    """
    env = {k: v for k, v in os.environ.items() if k not in drop_env}
    env.update(extra_env or {})
    r = subprocess.run(argv, input=stdin_text, capture_output=True, text=True,
                       timeout=PROBE_TIMEOUT_S,
                       env={**env, LIVENESS_PROBE_ENV: "signal-liveness-canary"})
    return r.returncode, (r.stdout or ""), (r.stderr or "")


def _assert_query_read_channel() -> tuple[bool | None, str]:
    """S-3 read channel: can aspirations-query.sh still emit an ANSWER?

    THE DISCRIMINATOR IS THE BYTE COUNT, NOT THE ROW COUNT, and that is what makes
    this row free of false alarms. Measured on alpha/cc-08 2026-09-13:
      examined-and-empty -> rc=0, stdout '[]' = 2 bytes (46 ms)
      refused call       -> rc=2, stdout 0 bytes, stderr 433 bytes
    A legitimately empty result set is still 2 bytes, so ZERO bytes at rc=0 can only
    mean the call produced no answer at all. A row-count predicate would go DEAD every
    time the query legitimately matched nothing; a byte-count predicate cannot. This
    is the same confusion that produced the S-3 incident: a parser read 0 bytes as
    "0 blocked goals" while the call had in fact been refused.
    """
    script = SCRIPT_DIR / "aspirations-query.sh"
    if not script.is_file():
        return None, f"aspirations-query.sh absent at {script}"
    try:
        rc, out, err = _probe(bash_cmd(script.as_posix(), "--title-contains", QUERY_NO_MATCH_SENTINEL))
    except Exception as exc:
        return None, f"aspirations-query.sh could not run: {exc}"
    if rc != 0:
        # A refusal is the OTHER row's subject. Unevaluatable here, never DEAD:
        # reporting dead would count one fault as two dead signals.
        return None, f"read probe returned rc={rc} (the refusal row's business): {(err or out).strip()[:200]}"
    if len(out) == 0:
        return True, (
            "aspirations-query.sh returned rc=0 with ZERO bytes on stdout. A caller parses that "
            "as an empty result set, but an examined-and-empty query emits '[]' (2 bytes, "
            "measured) — zero bytes means nothing was examined."
        )
    return False, (
        f"read channel live: rc=0 with {len(out)} byte(s) on stdout "
        f"({out.strip()[:40]!r}) for a query that matches nothing"
    )


def _assert_query_refusal_channel() -> tuple[bool | None, str]:
    """S-3 refusal channel: does a bad flag still FAIL, and still say why?

    Measured both directions on alpha/cc-08 2026-09-13:
      never-valid flag -> rc=2, stdout 0 bytes, stderr 433 bytes citing g-115-4733 (5 ms)
      valid query      -> rc=0, stderr 0 bytes
    Two ways to be dead, and both are checked: rc=0 on a bad flag (the flag is being
    swallowed again — the g-115-4733 regression, where the token after it slides into a
    positional slot and the wrong value is written at exit 0), or a non-zero rc with
    EMPTY stderr (it fails without saying why, so even a caller that IS reading stderr
    learns nothing).

    NOTE WHAT THIS CANNOT CATCH, so the row is not over-read: a CALLER's own
    `2>/dev/null` is invisible from here. This asserts the instrument still speaks, not
    that anyone is listening.
    """
    script = SCRIPT_DIR / "aspirations-query.sh"
    if not script.is_file():
        return None, f"aspirations-query.sh absent at {script}"
    try:
        rc, out, err = _probe(bash_cmd(script.as_posix(), QUERY_NEVER_VALID_FLAG))
    except Exception as exc:
        return None, f"aspirations-query.sh could not run: {exc}"
    if rc == 0:
        return True, (
            f"a flag that can never be valid ({QUERY_NEVER_VALID_FLAG}) returned rc=0 — the "
            "unknown-flag refusal has regressed to the pre-g-115-4733 passthrough, where the "
            "next argument slides into a positional slot and the wrong value is written at exit 0."
        )
    if not err.strip():
        return True, (
            f"rc={rc} on a never-valid flag but stderr is EMPTY — it fails without saying why, "
            "so the diagnosis is gone even for a caller that is reading stderr."
        )
    return False, f"refusal channel live: rc={rc} with {len(err)} byte(s) on stderr"


def _trigger_fixture_still_trips(script_name: str, args: tuple[str, ...]) -> "str | None":
    """None when this row's claim text still trips the gate's CURRENT trigger table.

    The rc-shaped sibling of `_marker_fixture_still_must_deny`, and it exists for the
    same measured reason (g-115-10364): when the watched predicate tightens and the
    fixture does not, a non-trip is CORRECT and reading it as DEAD indicts a healthy
    gate forever. V-5 got that defence because it actually rotted; these four rows are
    the identical shape and had none, which is scar tissue only where the wound was.

    PROVEN, not argued (echo/cc-03 2026-09-21, g-318-156): the real factory was driven
    twice in one process against the SAME gates — live fixture -> alive (rc=1), a
    fixture rotted only in wording -> DEAD, 2 of 2. Gate health was held constant by
    the first arm, so the false accusation is attributable to the fixture alone.

    THE CLAIM TEXT IS READ OUT OF `args`, never passed in beside it. A second copy
    would be a second source of truth, and drift between the two would silently check
    a string the gate is no longer being sent — the very defect this guards.

    All four gates expose `_detect_trigger(claim_text)`, verified per gate rather than
    inherited (this file's standing instruction): live text returns the matched pattern,
    rotted text returns None, uniform across V-1..V-4. V-6 (capability-gate) matches on
    a different contract and deliberately supplies no check rather than a guessed one.
    """
    claim = None
    for flag in ("--claim-text", "--claim"):
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                claim = args[i + 1]
            break
    if claim is None:
        return (f"{script_name}'s probe args carry no --claim-text/--claim, so this row's "
                f"fixture cannot be checked against the gate's trigger table")
    path = SCRIPT_DIR / script_name
    try:
        spec = importlib.util.spec_from_file_location(
            "_canary_probe_" + script_name.replace("-", "_")[:-3], path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:
        return f"{script_name}'s trigger SSOT could not be imported ({exc})"
    detect = getattr(module, "_detect_trigger", None)
    if detect is None:
        return f"{script_name} no longer exposes _detect_trigger, so the fixture is uncheckable"
    try:
        if detect(claim):
            return None
    except Exception as exc:
        return f"{script_name}._detect_trigger raised on the probe claim ({exc})"
    return (f"the probe claim {claim!r} no longer matches {script_name}'s trigger table "
            f"(_detect_trigger returned no match), so a non-trip is CORRECT and this row "
            f"cannot say anything about the gate. The trigger table moved under the "
            f"fixture — fix this row's args, not the gate")


def _gate_trigger_assertion(script_name: str, args: tuple[str, ...], trigger_hint: str,
                            fixture_check=None, stdin_text: "str | None" = None,
                            refusal_check=None, crash_is_dead: bool = False):
    """ONE shape, four rows (V-1..V-4): a Q1/Q2 gate whose rc=0 means BOTH 'evidence
    sufficient' AND 'no trigger matched'.

    `stdin_text` is for a gate that takes its input as JSON on stdin rather than in
    argv (blocker-create-gate.py, g-318-168). Default None keeps every argv-shaped row
    byte-identical.

    `refusal_check` is for a gate that runs SEVERAL checks, where rc=1 says only that
    SOME check refused (guard-1082: assert the specific refusal, never the coarse rc).
    It reads the refusal stdout and returns None when the watched check is the one
    refusing, else the (verdict, detail) to report. Default None keeps rc=1 sufficient.

    `crash_is_dead` is for a gate whose refusal IS silence (notification_routing_gate:
    rc=1 = SUPPRESS = the caller skips its send, g-318-168 unit 5). The rc=1 branch below
    calls a crash unevaluatable because a crashed gate's callers start refusing everything
    and so announce it. For a gate like that, refusing everything means nobody is told
    anything, so the crash is the loudest death the row can see and it is DEAD
    (guard-7231). Default False keeps every other row as it was.

    Measured on alpha/cc-08 2026-09-13, each gate driven both ways in the same pass:
      must-trip input     -> rc=1, JSON carries a non-null trigger_matched (~35 ms each)
      non-triggering text -> rc=0, JSON carries trigger_matched: null
    The rc cannot separate those two; the JSON field can. So the assertion drives the
    must-trip input and treats rc=0 as DEAD: the gate has stopped matching what it
    exists to match, and rc=0 is precisely what a caller records as a pass.

    WHY A CANARY AND NOT A TEST, measured rather than assumed: of these four gates only
    zero-count-gate has a dedicated test file (core/tests/gates/
    test_zero_count_gate_triggers.py). The other three have NONE, so nothing
    positive-controls them at all — and even for the fourth, a suite runs on a cadence
    while this runs every reducer iteration, which is the detection latency the
    "detection outranks attribution" directive asks to shorten.

    Uses sys.executable rather than a bash wrapper: these gates have no .sh wrapper, and
    a direct interpreter call avoids the whole shim/PATH failure surface.
    """
    def _assert() -> tuple[bool | None, str]:
        path = SCRIPT_DIR / script_name
        if not path.is_file():
            # ABSENT is unevaluatable, never DEAD — a merge-wedged box legitimately lacks
            # a recently-added script, and alarming there would report a tree problem as a
            # dead detector.
            return None, f"{script_name} absent at {path}"
        # A ROTTED FIXTURE IS UNEVALUATABLE, NEVER DEAD (, generalised to this
        # factory by ). Same placement and same fail direction as the hook-gate
        # sibling: a row whose fixture cannot rot supplies no check and behaves as before.
        if fixture_check is not None:
            try:
                stale = fixture_check(script_name, args)
            except Exception as exc:
                stale = f"the fixture check itself failed ({exc})"
            if stale:
                return None, f"{script_name} probe fixture is stale: {stale}"
        try:
            argv = [sys.executable, path.as_posix(), *args]
            # Pass stdin_text only when a row sets it, so an argv-shaped row keeps its
            # exact call (and a one-argument _probe stub keeps working for it).
            rc, out, err = (_probe(argv) if stdin_text is None
                            else _probe(argv, stdin_text=stdin_text))
        except Exception as exc:
            return None, f"{script_name} could not run: {exc}"
        if rc == 1:
            # rc=1 IS NOT PROOF OF LIFE — Python exits 1 on an uncaught exception too,
            # so a gate that cannot even IMPORT is byte-identical here to one that
            # refused (guard-5430, which this row was violating: "a block code of 1 is
            # byte-identical to a crash, an ImportError, or a missing gate file").
            # The discriminator is the PAYLOAD, and this file already knows that — see
            # _assert_query_read_channel, whose whole docstring is "THE DISCRIMINATOR IS
            # THE BYTE COUNT". Measured here the same way (echo/cc-03 2026-09-21,
            # fresh-eyes on 's commits): all five rc-shaped gates refusing
            # their real must-trip input -> rc=1 with 720/796/861/721/6903 stdout bytes
            # and ZERO stderr; the same gate with one bad import -> rc=1 with ZERO
            # stdout and 252 stderr bytes. Non-empty stdout is therefore safe on every
            # live row and impossible for a gate that died before argparse.
            # UNEVALUATABLE, NOT DEAD, and the fail direction is the argued half: a
            # crashed gate exits 1 at every caller, so it refuses EVERYTHING — the
            # opposite of "passes every claim it was built to refuse", and DEAD's remedy
            # would send the reader to the wrong file. It is also the guard-7231
            # audibility question answered, not skipped: unevaluatable is silent under
            # --quiet, and that is acceptable ONLY because this failure announces itself
            # through the gate's own callers (everything they gate starts refusing),
            # unlike the V-6 empty-corpus case, which was silent everywhere.
            if not out.strip() and crash_is_dead:
                return True, (
                    f"{script_name} CRASHED: rc=1 with EMPTY stdout, so it emitted no "
                    f"verdict, and its callers read rc=1 as a refusal. For this gate a "
                    f"refusal is silence, so everything it gates is being dropped and "
                    f"nothing says so. stderr={(err or '').strip()[:200]}"
                )
            if not out.strip():
                return None, (
                    f"{script_name} exited rc=1 with EMPTY stdout, so it did not refuse "
                    f"anything — it died before emitting its verdict (a live refusal on "
                    f"this input carries the gate's JSON). rc=1 cannot tell 'blocked' "
                    f"from 'could not run' (guard-5430), so this row is unevaluatable, "
                    f"not alive. stderr={(err or '').strip()[:200]}"
                )
            if refusal_check is not None:
                try:
                    verdict = refusal_check(out)
                except Exception as exc:
                    verdict = (None, f"refusal check itself failed ({exc})")
                if verdict is not None:
                    return verdict[0], f"{script_name} {verdict[1]}"
            return False, f"{script_name} still refuses its must-trip input (rc=1)"
        if rc == 0:
            return True, (
                f"{script_name} returned rc=0 on an input engineered to trip "
                f"{trigger_hint!r}. rc=0 is what a caller reads as 'evidence sufficient', so this "
                f"gate now passes every claim it was built to refuse. out={out.strip()[:200]}"
            )
        return None, f"{script_name} returned unexpected rc={rc}: {(err or out).strip()[:200]}"
    return _assert


# The blocker-create-gate row's must-trip input ( unit 2). It fails ONLY check 3
# (schema_probe): a statistical-negation failure_reason with no schema_probe_evidence.
# Two distinct non-silent evidence entries pass check 2; type "resource" skips checks
# 4-6; no affected_skills leaves check 1 nothing to check. Measured against the real
# gate (echo/cc-03 2026-09-23): rc=1, failing_count 1, the failing check schema_probe.
# The same payload with a non-statistical reason returned rc=0.
BLOCKER_STAT_NEG_PAYLOAD: dict = {
    "type": "resource",
    "failure_reason": "0 records have field utilization across all entries",
    "evidence": [
        {"tool": "jsonl-field-probe.py", "endpoint": "canary-fixture-store",
         "evidence_type": "schema-read",
         "command": "py -3 core/scripts/jsonl-field-probe.py canary-fixture-store utilization"},
        {"tool": "grep", "endpoint": "canary-fixture-store", "evidence_type": "count",
         "command": "grep -c utilization canary-fixture-store"},
    ],
}


def _stat_neg_fixture_check(payload: dict):
    """Fixture check for a stdin-fed gate: None while the payload's failure_reason still
    matches gates/blocker_create._STAT_NEG_PATTERNS.

    The stdin-shaped sibling of `_trigger_fixture_still_trips`. The reason is read from
    the SAME dict the row serialises to stdin, never from a second copy beside it, for
    the reason that function's docstring gives.

    AN EMPTY PATTERN TABLE RETURNS None, NOT "stale fixture" (guard-7231). With no
    patterns the gate can refuse no statistical negation, so the probe must run and
    report DEAD. Calling it a stale fixture would file a dead check 3 as unevaluatable,
    which is silent under --quiet.
    """
    def _check(script_name: str, args: tuple[str, ...]) -> "str | None":
        reason = str(payload.get("failure_reason") or "")
        path = SCRIPT_DIR / "gates" / "blocker_create.py"
        try:
            spec = importlib.util.spec_from_file_location(
                "_canary_probe_gates_blocker_create", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            return (f"gates/blocker_create.py, the trigger SSOT for {script_name}, could "
                    f"not be imported ({exc})")
        patterns = getattr(module, "_STAT_NEG_PATTERNS", None)
        if patterns is None:
            return ("gates/blocker_create.py no longer exposes _STAT_NEG_PATTERNS, so the "
                    "fixture is uncheckable")
        if not patterns:
            return None
        if any(p.search(reason) for p in patterns):
            return None
        return (f"the probe failure_reason {reason!r} no longer matches any "
                f"gates/blocker_create._STAT_NEG_PATTERNS entry, so a non-trip is CORRECT "
                f"and this row cannot say anything about check 3. The pattern table moved "
                f"under the fixture — fix BLOCKER_STAT_NEG_PAYLOAD, not the gate")
    return _check


def _named_check_refused(check_name: str):
    """Refusal check for a gate that reports every check in `checks[]` (guard-1082).

    blocker-create-gate runs all six checks and exits 1 if ANY fails, so rc=1 cannot
    say which one refused. Measured against the real gate (echo/cc-03 2026-09-23): a
    payload that fails only a SIBLING check (one evidence entry, non-statistical reason)
    returned rc=1 with non-empty stdout, which the coarse row read as check 3 alive.

    A watched check that PASSED while a sibling refused is DEAD: the fixture check has
    already confirmed the input still trips it. A watched check with no entry at all is
    UNEVALUATABLE, because a renamed check and a deleted one read the same here, and a
    deleted check with no sibling failing still shows DEAD through rc=0.
    """
    def _check(out: str):
        try:
            checks = json.loads(out)["checks"]
            names = [c.get("name") for c in checks]
            failing = [c.get("name") for c in checks if not c.get("passed")]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            return None, (f"refused (rc=1) but its stdout is not the checks[] verdict this "
                          f"row reads ({exc!r}), so which check refused is unknown")
        if check_name in failing:
            return None
        if check_name not in names:
            return None, (f"reports no check named {check_name!r} (checks: {names}), so "
                          f"this row cannot find what it watches. Fix the row's check "
                          f"name, not the gate")
        return True, (f"refused its must-trip input only via {failing}; {check_name} "
                      f"PASSED an input engineered to trip it, so it has stopped refusing "
                      f"and a sibling check is carrying rc=1 (guard-1082)")
    return _check


# The pending-deploys row's must-trip obligation ( unit 3). Every identifier is a
# non-numeric probe-only form no allocator can mint (guard-1094: a probe that WRITES uses a
# synthetic id, and a high round number is a guess about absence, not a guarantee). `repo`
# must still be owner/name or the gate's own writer refuses it — which is exactly how this
# row notices that its fixture has rotted.
PENDING_DEPLOYS_PROBE_ENTRY: dict = {
    "repo": "probe-only-owner/probe-only-signal-liveness-canary",
    "sha": "cafe" * 10,
    "goal_id": "probe-only-signal-liveness-canary",
}

# Stripped from BOTH probe children. With --store given the tracker never reads them; they
# go so that a regression which stops honouring --store resolves no agent and writes
# nowhere (measured: `add` with no --store and no agent env -> rc=0, nothing written),
# instead of landing the probe obligation in the LIVE store, where the closure gate would
# re-probe a repo that does not exist at every close (guard-1006). MIND_SID goes too:
# today only MIND_AGENT names the store, but a fallback chain is an enumeration claim
# (guard-3970), and a session-binding fallback would need nothing but the SID.
_AGENT_IDENTITY_ENV = ("MIND_AGENT", "MIND_SID")


def _pending_obligation_assertion(entry: dict, check_fixture: bool = True):
    """The pending-deploys row ( unit 3): can the closure gate still SEE a
    recorded deploy obligation?

    WHY THIS GATE. pending-deploys-gate.sh refuses clean-success closure while a push to a
    CI repo is unverified, and its first move is a fast exit:
        if ! python3 "$PD" --agent "$AGENT" has-pending --goal-id "$GOAL" >/dev/null 2>&1
    so EVERY non-zero rc reads as "nothing pending" and the goal closes clean. The tracker
    is fail-open by design (a read error is []), so a store it can no longer read, a module
    that no longer loads, and a refused call shape all look exactly like the healthy common
    case. That is the always-CLEAR shape on the gate that stands between a failed deploy
    and paying users, and nothing announces it: the caller discards both streams.

    THE CONTRACT IS INVERTED FROM THE rc-SHAPED FAMILY, which is why this is not a
    _gate_trigger_assertion row: there rc=1 is the trip, here rc=0 is. Measured by zeta
    on cc-02, 2026-09-23 (g-318-168), against the real tracker with an isolated store
    and the agent env stripped:
        add (owner/name repo)          -> rc=0, 0 B stderr, 190 B store       (39 ms)
        has-pending on that store      -> rc=0, 0 B stderr                    (39 ms)
        has-pending on an empty store  -> rc=1, 0 B stderr
        add with a bare repo           -> rc=2, 266 B stderr (REFUSED), no store
        a copy with a syntax error     -> rc=1, 123 B stderr, NO 'Traceback' header
        a copy with a bad import       -> rc=1, 210 B stderr (its add: rc=1, no store)
        an unknown subcommand          -> rc=2, 327 B stderr
    So stderr, not a Traceback header, is what separates a crash from an honest empty
    read — and both are DEAD, because the gate cannot tell them apart either and closes
    clean on both.

    A CRASH IS DEAD HERE — the deliberate opposite of the rc-shaped family's rule for rc=1
    with empty stdout. That rule rests on a crashed gate announcing itself through its
    callers (everything they gate starts refusing). This one announces nothing: its caller
    throws both streams away and PASSES every closure. Unevaluatable is silent under
    --quiet (guard-7231), so calling a crash unevaluatable here would make it silent
    everywhere.

    THE MUST-TRIP INPUT IS WRITTEN BY THE GATE'S OWN WRITER each run (`add`), never dumped
    from here. A hand-written store would be a second copy of the store format; when the
    format moved, the reader would honestly read [] and this row would accuse a healthy
    gate. It also makes the gate the judge of whether the fixture is still a legal
    obligation: if `add` refuses it or registers nothing, the entry has rotted and a
    non-trip says nothing about has-pending — UNEVALUATABLE, not DEAD (g-115-10364). The one
    exception is rc=1 from `add`. The tracker never returns 1 from add (argparse is 2, a
    refusal is 2, main() turns any exception in a subcommand into 0), so rc=1 means the
    module itself could not run — it failed to load, or its parser raised — which kills
    has-pending too; the reader leg is left to report it.
    `check_fixture=False` exists only for the test proving this check is what prevents the
    false accusation.

    WHAT THIS ROW DOES NOT WATCH (g-318-168), named so a green is not over-read:
      - the agent-branch store resolution (_store_path -> _agent_dir). --store bypasses
        it. The writer and reader share it, so a mis-resolution cannot split them, but a
        resolution to None silences both at once and this row cannot see that.
      - the interpreter. The gate runs `python3` after sourcing _paths.sh; this row runs
        sys.executable, as every other row does. A python3 without PyYAML would read the
        store as [] in production while this row stays green.
      - the capture layer (deploy-detect-hook.sh), and the all-sweep call that passes no
        --goal-id. A writer that silently registers nothing reads UNEVALUATABLE here.
    """
    def _assert() -> tuple[bool | None, str]:
        path = SCRIPT_DIR / "pending-deploys.py"
        if not path.is_file():
            # ABSENT is unevaluatable, never DEAD — same reasoning as every sibling row.
            return None, f"pending-deploys.py absent at {path}"
        tmp = tempfile.mkdtemp(prefix="signal-liveness-canary-pd-")
        try:
            store = Path(tmp) / "pending-deploys.yaml"
            base = [sys.executable, path.as_posix(), "--store", store.as_posix()]
            try:
                arc, _aout, aerr = _probe(
                    [*base, "add", "--repo", entry["repo"], "--sha", entry["sha"],
                     "--goal-id", entry["goal_id"]], drop_env=_AGENT_IDENTITY_ENV)
                written = store.is_file() and store.stat().st_size > 0
                if check_fixture and not written and arc != 1:
                    return None, (
                        f"pending-deploys.py probe fixture is stale: the gate's own `add` "
                        f"did not register {entry['repo']}@{entry['sha'][:7]} (rc={arc}), so "
                        f"there is no obligation for has-pending to see and a non-trip would "
                        f"say nothing about it. Fix PENDING_DEPLOYS_PROBE_ENTRY, not the gate. "
                        f"stderr={aerr.strip()[:200]}")
                rc, _out, err = _probe([*base, "has-pending", "--goal-id", entry["goal_id"]],
                                       drop_env=_AGENT_IDENTITY_ENV)
            except Exception as exc:
                return None, f"pending-deploys.py could not run: {exc}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if rc == 0:
            return False, "pending-deploys.py has-pending still sees a recorded obligation (rc=0)"
        if rc in (1, 2):
            if rc == 2:
                how = "refused the call shape pending-deploys-gate.sh uses (has-pending --goal-id)"
            elif err.strip():
                how = "CRASHED"
            else:
                how = "read the recorded obligation as nothing pending"
            store_note = ("a store its own `add` had just written" if written
                          else f"the probe store (its own `add` wrote nothing, rc={arc})")
            return True, (
                f"pending-deploys.py has-pending {how} (rc={rc}) on {store_note}. "
                f"pending-deploys-gate.sh reads every non-zero rc as 'nothing pending' and "
                f"discards both streams, so every closure now passes with its deploy "
                f"unverified, silently. stderr={err.strip()[:200]}")
        return None, (f"pending-deploys.py has-pending returned unexpected rc={rc}: "
                      f"{err.strip()[:200]}")
    return _assert


def _hook_gate_deny_assertion(script_name: str, payload: dict, what_permitted: str,
                              fixture_check=None):
    """ONE shape, two rows (V-5, V-7): a PreToolUse hook gate whose rc is 0 whether it
    DENIES or APPROVES, so only the STDOUT DECISION can tell the two apart.

    THE SECOND HOOK GATE IS WHAT LICENSED THIS FACTORY, and that was this file's own
    written instruction rather than a preference. The plain function this replaces
    carried a docstring reading "A DELIBERATELY PLAIN FUNCTION, NOT A SECOND FACTORY
    ... Make it a factory when a SECOND hook gate is registered, not before." V-7 is
    that second gate, so the extraction is now the sanctioned move, and leaving two
    near-identical plain functions side by side is the drift that instruction exists
    to prevent (implementation-discipline rule 3 cuts BOTH ways).

    THE CONTRACT IS MEASURED PER GATE AND NEVER INHERITED — the third time this check
    has changed the answer (V-5 stdout, V-6 rc, now V-7 stdout). Driven both ways for
    schedule-wakeup-gate.py on alpha/cc-08 2026-09-13:
        prompt "/aspirations loop"                -> rc=0, 1110 stdout bytes, decision "deny"
        prompt "Parked worker Body: re-enter ..."  -> rc=0, 0 stdout bytes
    and for marker-placement-gate.py the four-case table preserved in the V-5 factory
    call below. rc=0 in EVERY case: hook_helpers.emit_deny prints JSON and exits 0, so
    a non-zero rc means the hook CRASHED. Pointing the rc-shaped
    _gate_trigger_assertion at either gate would report DEAD on a perfectly healthy
    gate every single run — the always-ALARM twin of the defect this canary hunts.

    A NON-EMPTY BUT UNPARSEABLE STDOUT IS UNEVALUATABLE, NOT DEAD. This is a
    deliberate narrowing of the plain function it replaces, which mapped a JSON error
    to decision "" and thus to DEAD. A hook that prints something off-contract has
    said NOTHING about whether it would deny; calling that "the gate now permits
    everything" misnames one real defect as a different real defect and sends the
    remedy reader to the wrong file. Same fail direction the sibling already takes for
    an absent script and a crash.

    Uses sys.executable rather than the spec's literal `py -3`: these gates have no
    .sh wrapper and the direct interpreter call avoids the shim/PATH surface, which is
    the sibling factory's stated reason and keeps one invocation style in this file.
    """
    def _assert() -> tuple[bool | None, str]:
        path = SCRIPT_DIR / script_name
        if not path.is_file():
            # ABSENT is unevaluatable, never DEAD — a merge-wedged box legitimately
            # lacks a recently-added script, and alarming there would report a tree
            # problem as a dead detector.
            return None, f"{script_name} absent at {path}"
        # A ROTTED FIXTURE IS UNEVALUATABLE, NEVER DEAD (). This row's
        # whole verdict rests on the payload still being one the gate MUST refuse.
        # When the refusal predicate moves and the fixture does not, an approve is
        # CORRECT and reading it as DEAD indicts a healthy gate forever. Optional:
        # a row whose payload cannot rot supplies no check and behaves as before.
        if fixture_check is not None:
            try:
                stale = fixture_check(payload)
            except Exception as exc:
                stale = f"the fixture check itself failed ({exc})"
            if stale:
                return None, f"{script_name} probe fixture is stale: {stale}"
        try:
            rc, out, err = _probe([sys.executable, path.as_posix()],
                                  stdin_text=json.dumps(payload))
        except Exception as exc:
            return None, f"{script_name} could not run: {exc}"
        if rc != 0:
            # A crash is a real defect but NOT this row's defect, and calling it DEAD
            # would misname it. Unevaluatable, surfaced.
            return None, (f"{script_name} exited rc={rc} on the probe payload; the "
                          f"PreToolUse contract is exit 0 always. "
                          f"{(err or out).strip()[:200]}")
        if not out.strip():
            return True, (
                f"{script_name} returned NO deny — stdout was EMPTY — for a payload it "
                f"exists to refuse. Every caller reads that as approval, so this gate "
                f"now permits {what_permitted}."
            )
        try:
            decision = (json.loads(out).get("hookSpecificOutput", {})
                        .get("permissionDecision", ""))
        except Exception as exc:
            return None, (f"{script_name} printed {len(out.strip())} byte(s) that are not "
                          f"the PreToolUse JSON contract ({exc}); whether it would deny "
                          f"is unreadable from here, so this is unevaluatable.")
        if decision == "deny":
            return False, f"{script_name} still denies its must-deny payload"
        return True, (
            f"{script_name} returned decision={decision!r} instead of 'deny' for a "
            f"payload it exists to refuse, so this gate now permits {what_permitted}. "
            f"stdout_bytes={len(out.strip())}"
        )
    return _assert


def _marker_placement_probe_payload() -> dict:
    """V-5's must-deny payload, extracted so the registry row stays one readable line.

    Called at registry-construction time, so the path is resolved from the real
    SCRIPT_DIR once at import — which is correct because the factory checks the script
    for absence BEFORE it ever reads the payload, so a test that repoints SCRIPT_DIR
    returns unevaluatable without this path mattering. Stated rather than left implicit
    because "built by a function" reads like lazy resolution and is not.

    The payload names a file that does not exist and is never written: a PreToolUse
    hook only inspects the PROPOSED write.

    THE TOKEN MUST OPEN A COMMENT, AND THIS FIXTURE ONCE DID NOT (g-115-10364).
    Until 2026-09-21 the content put the bare token on a plain line, which WAS a
    must-deny payload while every consumer tested the marker as an unanchored
    substring. `57a655f3b7` (g-115-10246) converged all seven consumers onto the
    anchored predicate in `_domain_leak_marker.claims_exemption` — the token must
    OPEN a comment — and touched no fixture that DRIVES one. So this payload
    quietly stopped being must-deny, the gate correctly approved it, and V-5 read
    DEAD for three consecutive runs against a gate that was healthy the whole time:
    driven both ways 2026-09-21 (echo, cc-03), a `<!--`-opening token denies at
    1243 stdout bytes and a `#`-opening one denies identically, while the override
    and out-of-scope paths still approve. A false DEAD on the instrument that hunts
    always-reports-clear detectors is the same defect one level up, so the factory
    now takes `fixture_check` and this row supplies one.
    """
    probe_file = SCRIPT_DIR.parent / "config" / "conventions" / _CANARY_PROBE_NODE
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": probe_file.as_posix(),
            # Split so this canary's own source never carries the literal token it
            # probes for — otherwise the gate would refuse edits to THIS file and the
            # instrument would block its own maintenance.
            "content": "# signal-liveness canary probe\n\n<!-- " + _MARKER_TOKEN + " probe -->\n",
        },
    }


def _marker_fixture_still_must_deny(payload: dict) -> "str | None":
    """None when V-5's payload still claims the exemption; else WHY it no longer does.

    Asks the predicate's SSOT rather than re-implementing it, so a future move of
    `claims_exemption` reports itself HERE — as unevaluatable, naming the fixture —
    instead of arriving as a DEAD verdict against a healthy gate. That is the exact
    substitution g-115-10364 cost three runs and a HIGH goal.
    """
    try:
        from _domain_leak_marker import claims_exemption
    except Exception as exc:
        return f"the marker predicate SSOT could not be imported ({exc})"
    content = (payload.get("tool_input") or {}).get("content", "")
    if claims_exemption(content):
        return None
    return ("the V-5 probe payload no longer claims the exemption under "
            "_domain_leak_marker.claims_exemption, so it is no longer a must-deny "
            "payload and this row cannot say anything about the gate. The predicate "
            "moved under the fixture — fix _marker_placement_probe_payload, not the gate")


def _swakeup_fixture_still_must_deny(payload: dict) -> "str | None":
    """None when V-7's prompt is still one `_swakeup_predicate` says must be refused.

    Asks the SSOT the row's own remedy text already tells a human to check FIRST —
    `_swakeup_predicate.is_bad_slash_prefix`, shared with the Layer-C detective
    `aspirations-rejection-audit.py`, "so one predicate drift kills BOTH layers at
    once and neither says so". Wiring it here turns that instruction into an
    assertion: the drift now reports itself as unevaluatable NAMING the fixture,
    instead of arriving as a DEAD verdict against a healthy gate.

    WHY THIS PREDICATE SEPARATES THE TWO WORLDS AND `gates.capability.evaluate`
    WOULD NOT (the reason V-6 is deliberately still unchecked — g-318-167).
    A fixture check is only worth anything if it can distinguish "the gate died"
    from "the fixture rotted". `is_bad_slash_prefix` is a pure string property of
    the PROMPT — it stays True while the gate is broken in its payload-key read,
    its emit path, or its stop:true branch, so the two worlds come apart. V-6's
    importable `evaluate()` is the gate's WHOLE decision function: a check built
    on it returns exactly what the subprocess drive returns, so every genuine
    death would be reported as UNEVALUATABLE and MASKED. An importable predicate
    is necessary but not sufficient — it must also be NARROWER than the decision.

    Confirmed both ways (echo/cc-03 2026-09-21): '/aspirations loop' -> True;
    '/loop investigate x', '<<autonomous-loop-dynamic>>', a natural-language
    prompt and a non-string all -> False.
    """
    try:
        from _swakeup_predicate import is_bad_slash_prefix
    except Exception as exc:
        return f"the ScheduleWakeup predicate SSOT could not be imported ({exc})"
    prompt = (payload.get("tool_input") or {}).get("prompt")
    if is_bad_slash_prefix(prompt):
        return None
    return (f"the V-7 probe prompt {prompt!r} is no longer one "
            "_swakeup_predicate.is_bad_slash_prefix refuses, so it is not a must-deny "
            "payload and this row cannot say anything about the gate. The predicate "
            "moved under the fixture — fix this row's payload, not the gate")


# V-6's fixture depends on ONE fact about the capability corpus: that
# commit-and-push is still catalogued as agent-provisionable. Pinned as literal
# constants rather than derived from the probe args, deliberately — a derivation
# would have to re-implement or import the gate's keyword extractor, and that
# extractor is one of the failure modes this row exists to catch (the row's own
# remedy tells a reader to distinguish "empty matches beside populated keywords"
# = corpus, from an extractor fault). A constant cannot drift silently either,
# because the check below asserts the probe reason still contains the phrase.
_V6_FIXTURE_PHRASE = "commit and push"
_V6_FIXTURE_TOKENS = ("commit", "push")


def _capability_fixture_vocabulary_present(script_name: str,
                                           args: "tuple[str, ...]") -> "str | None":
    """None when V-6's fixture still has vocabulary in the loaded capability corpus.

    THE THIRD FIXTURE GUARD, AND THE ONE g-318-167 REFUSED TO SHIP FIRST TIME.
    The obvious wiring was `gates.capability.evaluate()` — importable, documented as
    extracted for other callers, and WRONG: it is the gate's whole decision, so a
    check built on it returns exactly what the subprocess drive returns, every genuine
    death is relabelled unevaluatable, and the row goes silent forever (guard-7228).
    This predicate is strictly narrower — it reads only the SOURCES the decision loads
    its match terms from, never the matcher, the extractor, or the verdict.

    Name a way the gate can be broken while this returns None, which is the test that
    separates a check from a mask: the keyword extractor breaks, `--failure-reason` is
    renamed, the rc convention flips, `_find_matches` breaks, the noise-phrase list
    swallows the reason, or the corpus fails to load. EVERY one of those still reaches
    a DEAD verdict, because none of them changes whether commit-and-push is CATALOGUED.
    And the converse: someone rewords the capability-routing row out from under the
    fixture while the gate is perfectly healthy — only that returns a complaint.

    AN EMPTY CORPUS RETURNS None ON PURPOSE, and this is the load-bearing branch.
    A world-path misresolution empties the corpus silently, the gate then approves a
    routing it exists to refuse, and this row's remedy names that as the FIRST thing to
    check. Under `--quiet` (the production invocation from iteration-close.sh) an
    unevaluatable row resets its stuck counter to 0 and prints NOTHING, so calling an
    empty corpus a rotted fixture would convert the loudest real failure this row can
    detect into total silence. Unevaluatable is the right answer for a rotted fixture
    and the wrong answer for a dead corpus; the emptiness test is what tells them apart.
    """
    try:
        from gates.capability import (
            _DEFAULT_SKILLS_DIR,
            _load_capability_routing,
            _load_forged_skills,
            _load_skill_md_triggers,
        )
    except Exception as exc:
        return f"the capability corpus loaders could not be imported ({exc})"

    reason = None
    if "--failure-reason" in args:
        i = args.index("--failure-reason")
        if i + 1 < len(args):
            reason = args[i + 1]
    if reason is None:
        return (f"{script_name}'s probe args carry no --failure-reason, so this row's "
                "fixture cannot be checked against the capability corpus")
    if _V6_FIXTURE_PHRASE not in reason.lower():
        return (f"this row's probe reason {reason!r} no longer contains "
                f"{_V6_FIXTURE_PHRASE!r}, which is the one corpus fact the fixture "
                "guard pins — the probe was edited without updating "
                "_V6_FIXTURE_PHRASE/_V6_FIXTURE_TOKENS beside it")

    world = Path(WORLD_DIR) if WORLD_DIR else None
    try:
        entries = (_load_forged_skills(world)
                   + _load_skill_md_triggers(_DEFAULT_SKILLS_DIR)
                   + _load_capability_routing(world))
    except Exception as exc:
        return f"the capability corpus could not be loaded ({exc})"
    if not entries:
        # NOT a fixture problem — see the docstring. Say nothing and let the row
        # reach its DEAD verdict, which is the alarm a human needs here.
        return None

    blob = " ".join(
        " ".join(str(v) for v in e.values() if isinstance(v, str))
        + " " + " ".join(str(t) for t in (e.get("triggers") or []))
        + " " + " ".join(str(s) for s in (e.get("scripts") or []))
        for e in entries if isinstance(e, dict)
    ).lower()
    missing = [t for t in _V6_FIXTURE_TOKENS if t not in blob]
    if not missing:
        return None
    return (f"the capability corpus loaded {len(entries)} entr(ies) but none mentions "
            f"{missing} — {_V6_FIXTURE_PHRASE!r} is no longer catalogued as "
            "agent-provisionable, so a non-trip here is the CORRECT answer and says "
            "nothing about the gate. The corpus moved under the fixture: fix this "
            "row's probe reason, not the gate")


# The findings-gate row's must-trip insight ( unit 4). ONE literal: the fixture
# check and the probe both read this constant, so there is no second copy to drift. It is
# prose a real close could carry, a root-cause clause with no resolution verb. The ids are
# the non-numeric probe-only forms of guard-1094, though --dry-run files nothing anyway.
FINDINGS_GATE_PROBE_INSIGHT = (
    "The liveness canary probe stalls because of a stale lock held by an earlier writer."
)
_FINDINGS_PROBE_ARGS = (
    "--goal", "probe-only-signal-liveness-canary",
    "--aspiration", "probe-only-signal-liveness-canary",
    "--category", "liveness-probe",
    "--dry-run",
)


def _findings_fixture_check(insight: str):
    """Fixture check for the findings-gate row: None while `insight` still hits one of the
    gate's OWN SIGNAL_PATTERNS match_re with no resolution_re in the match.

    That is the TABLE half of the gate's predicate, deliberately narrower than
    scan_signals itself (its negation, window and degenerate filters stay on the watched
    side). A table that moved under the literal makes a non-trip CORRECT, so it is a stale
    fixture. scan_signals rejecting a literal its own table still matches is the gate
    dying, so that stays DEAD.

    THE TABLE LIVES IN THE GATE FILE ITSELF, unlike blocker-create-gate's, so an import
    failure here is NOT a stale fixture (guard-7231). A gate file that cannot import cannot
    scan either, and calling that "stale" would file the crash as UNEVALUATABLE, which is
    silent under --quiet. It returns None so the probe runs and reports DEAD. An EMPTY
    table also returns None, for the reason _stat_neg_fixture_check gives.
    """
    def _check(script_name: str, args: tuple[str, ...]) -> "str | None":
        path = SCRIPT_DIR / script_name
        try:
            spec = importlib.util.spec_from_file_location(
                "_canary_probe_findings_gate", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            return None
        table = getattr(module, "SIGNAL_PATTERNS", None)
        if table is None:
            return (f"{script_name} no longer exposes SIGNAL_PATTERNS, so the fixture is "
                    "uncheckable")
        if not table:
            return None
        for _name, match_re, resolution_re in table:
            for m in match_re.finditer(insight):
                if not resolution_re.search(m.group(0)):
                    return None
        return (f"the probe insight {insight!r} no longer hits any {script_name} "
                "SIGNAL_PATTERNS match_re without its resolution_re, so a non-trip is "
                "CORRECT and this row cannot say anything about the scan. The table moved "
                "under the fixture: fix FINDINGS_GATE_PROBE_INSIGHT, not the gate")
    return _check


def _findings_gate_assertion(insight: str):
    """The findings-gate row ( unit 4): can the Step 8.5 scan still turn a close's
    unresolved finding into a follow-up?

    rc CARRIES NOTHING HERE. findings-gate.py exits 0 on a clean scan AND on a scan that
    found something; the verdict is the stdout line `findings_count=N`. Measured on
    bravo/cc-05 2026-09-23 against the real gate with agent identity stripped:
      must-trip insight -> rc=0, `findings_count=1 created=1`, 0 stderr bytes
      clean prose       -> rc=0, `findings_count=0 created=0`
    So N>=1 is alive, and N==0 on an insight its own table still matches is DEAD.

    A CRASH IS DEAD FOR THIS ROW, the reverse of the rc-shaped family. Those gates refuse
    everything when they crash. This one files nothing, and its callers (iteration-close.sh
    do_state_update, worker_retrospective) read "no findings" as a clean close. So a
    missing findings_count line at any rc is DEAD, not unevaluatable.

    SIDE-EFFECT FREE BY CONSTRUCTION: --dry-run creates no goals, _probe marks the child as
    a liveness probe so _gate_log records nothing, and stripping _AGENT_IDENTITY_ENV leaves
    AGENT_DIR unresolved, so load_dedup_titles returns [] before it would regenerate the
    live compact file (measured the same run: that file's mtime did not move).
    """
    script_name = "findings-gate.py"
    fixture_check = _findings_fixture_check(insight)

    def _assert() -> tuple[bool | None, str]:
        path = SCRIPT_DIR / script_name
        if not path.is_file():
            return None, f"{script_name} absent at {path}"
        try:
            stale = fixture_check(script_name, _FINDINGS_PROBE_ARGS)
        except Exception as exc:
            stale = f"the fixture check itself failed ({exc})"
        if stale:
            return None, f"{script_name} probe fixture is stale: {stale}"
        tmp = tempfile.mkdtemp(prefix="canary-findings-")
        try:
            insight_file = Path(tmp) / "insight.md"
            insight_file.write_text(insight, encoding="utf-8")
            argv = [sys.executable, path.as_posix(), *_FINDINGS_PROBE_ARGS,
                    "--insight-file", insight_file.as_posix()]
            rc, out, err = _probe(argv, drop_env=_AGENT_IDENTITY_ENV)
        except Exception as exc:
            return None, f"{script_name} could not run: {exc}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        count = None
        for line in out.splitlines():
            if line.startswith("findings_count="):
                head = line.split()[0].split("=", 1)[1]
                if head.isdigit():
                    count = int(head)
        if count is None:
            last = err.strip().splitlines()[-1][:160] if err.strip() else ""
            return True, (f"{script_name} CRASHED or changed its output: rc={rc}, no "
                          f"findings_count line (stdout {len(out)} B, stderr {len(err)} B"
                          f"{': ' + last if last else ''}). Its callers read that as a "
                          "clean close, so every finding in every close is dropped")
        if count >= 1:
            return False, f"findings_count={count} on the must-trip insight (rc={rc})"
        return True, ("findings_count=0 on an insight its own SIGNAL_PATTERNS still match: "
                      "scan_signals stopped reporting what the table says it should")
    return _assert


# The notification-routing-gate row's must-suppress input ( unit 5): a
# fleet-handleable category, and text that matches no HUMAN_ONLY_PATTERNS class. Measured
# against the real gate (bravo/cc-05 2026-09-23): rc=1, a 131-byte "SUPPRESS: ..." stdout,
# empty stderr. The fixture check reads the category and text out of THIS tuple.
ROUTING_GATE_PROBE_ARGS: tuple = (
    "--category", "info",
    "--subject", "signal-liveness canary probe: routine status report",
    "--body", "probe-only fixture, never sent",
)


def _routing_fixture_check(script_name: str, args: tuple[str, ...]) -> "str | None":
    """None while the gate's OWN tables still say to SUPPRESS this row's report.

    It reads the category and the text out of `args`, for the reason
    `_trigger_fixture_still_trips` gives. Rot means a table moved under the fixture: the
    category became always-send, it left the fleet-handleable set, or the text now
    matches a human-only class. A SEND is then correct and the row cannot judge the gate.

    TWO CASES RETURN None ON PURPOSE (guard-7231). Each IS a death this row watches, so
    the probe must run and report DEAD:
      - the module does not import. The probe then crashes, and the shell lane reads
        that rc=1 as SUPPRESS, so every notification it gates is dropped in silence.
      - FLEET_HANDLEABLE_CATEGORIES is empty. The gate then suppresses nothing, and every
        status report goes to the owner's inbox.
    """
    def _arg(flag: str) -> str:
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                return args[i + 1]
        return ""
    category = _arg("--category").strip().lower()
    try:
        spec = importlib.util.spec_from_file_location(
            "_canary_probe_notification_routing_gate", SCRIPT_DIR / script_name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        return None
    always = getattr(module, "ALWAYS_SEND_CATEGORIES", None)
    fleet = getattr(module, "FLEET_HANDLEABLE_CATEGORIES", None)
    human_only = getattr(module, "_human_only_match", None)
    if always is None or fleet is None or human_only is None:
        return (f"{script_name} no longer exposes ALWAYS_SEND_CATEGORIES, "
                f"FLEET_HANDLEABLE_CATEGORIES and _human_only_match, so the fixture is "
                f"uncheckable")
    if not fleet:
        return None
    if category in always:
        return (f"category {category!r} is now in ALWAYS_SEND_CATEGORIES, so a SEND is "
                f"CORRECT. Fix ROUTING_GATE_PROBE_ARGS, not the gate")
    if category not in fleet:
        return (f"category {category!r} left FLEET_HANDLEABLE_CATEGORIES, and an unknown "
                f"category SENDs by design, so a SEND is CORRECT. Fix "
                f"ROUTING_GATE_PROBE_ARGS, not the gate")
    matched = human_only(f"{_arg('--subject')}\n{_arg('--body')}")
    if matched:
        return (f"the probe text now matches the human-only class {matched!r}, so a SEND "
                f"is CORRECT. Fix ROUTING_GATE_PROBE_ARGS, not the gate")
    return None


def _routing_suppress_line(out: str):
    """refusal_check for the routing gate: None when stdout carries the SUPPRESS line.

    rc=1 with some other stdout is a gate that printed and then died. Its callers still
    read rc=1 as SUPPRESS, so this is the crash case and it is DEAD too.
    """
    if any(line.startswith("SUPPRESS:") for line in out.splitlines()):
        return None
    first = (out.strip().splitlines() or [""])[0][:120]
    return True, (f"exited rc=1 with no 'SUPPRESS:' verdict line (stdout {len(out)} B, "
                  f"first line {first!r}): it died after printing, and its callers read "
                  f"rc=1 as SUPPRESS")


# The domain-suite-gate row's must-refuse fixture ( unit 6): a throwaway world
# whose only test module imports a module that does not exist, so its suite cannot
# COLLECT. Measured against the real gate (bravo/cc-05 2026-09-23): rc=1 in 0.3s, one
# stdout JSON line with decision "block" and reason "domain suite could not COLLECT (rc=2:
# ...)", and the retained log landed in the probe's own DOMAIN_SUITE_LOG_DIR.
DOMAIN_SUITE_UNCOLLECTABLE_MODULE = "zzz_canary_uncollectable_module"
DOMAIN_SUITE_FIXTURE_TEST = "test_zzz_canary_uncollectable.py"
# --since skips the goal-record lookup, and --timeout 20 stays under PROBE_TIMEOUT_S, so
# the gate's own timeout (a fail-open "exceeded" error) fires before this process kills it.
DOMAIN_SUITE_PROBE_ARGS: tuple = (
    "--goal", "probe-only-signal-liveness-canary",
    "--source", "world",
    "--since", "2000-01-01T00:00:00",
    "--timeout", "20",
)


def _domain_suite_fixture_check(scripts_dir: Path, since: "_dt.datetime") -> "str | None":
    """None while the gate's OWN helpers still call the fixture a touched domain suite, and
    the module the fixture imports still does not exist.

    Rot means a helper moved under the fixture (a noop is then CORRECT), or the missing
    module now exists (the suite then collects, and a pass is CORRECT). Either way the row
    cannot judge the gate.

    AN IMPORT FAILURE RETURNS None. The probe then crashes the same way, and its rc=1 with
    no verdict line reports that crash in full (see _domain_suite_gate_assertion).
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "_canary_probe_domain_suite_gate", SCRIPT_DIR / "domain-suite-gate.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        return None
    has_tests = getattr(module, "has_domain_tests", None)
    touched = getattr(module, "touched_since", None)
    if has_tests is None or touched is None:
        return ("domain-suite-gate.py no longer exposes has_domain_tests and touched_since, "
                "so the fixture is uncheckable")
    if not has_tests(scripts_dir):
        return ("the gate's own has_domain_tests() no longer counts the fixture as a domain "
                "suite, so a noop is CORRECT. Fix the fixture layout, not the gate")
    if not touched(scripts_dir, since):
        return ("the gate's own touched_since() finds no fixture file newer than --since, so "
                "a noop is CORRECT. Fix DOMAIN_SUITE_PROBE_ARGS, not the gate")
    if importlib.util.find_spec(DOMAIN_SUITE_UNCOLLECTABLE_MODULE) is not None:
        return (f"a module named {DOMAIN_SUITE_UNCOLLECTABLE_MODULE!r} now exists, so the "
                "fixture suite collects and a pass is CORRECT. Rename "
                "DOMAIN_SUITE_UNCOLLECTABLE_MODULE")
    return None


def _domain_suite_verdict(out: str) -> "dict | None":
    """The gate's one stdout JSON line (the last one if a future version prints more)."""
    doc = None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed, _ = json.JSONDecoder().raw_decode(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("gate") == "domain-suite-gate":
            doc = parsed
    return doc


def _domain_suite_gate_assertion(check_fixture: bool = True):
    """The domain-suite-gate row ( unit 6): does the close-phase gate still REFUSE
    a close whose world domain suite cannot collect?

    iteration-close.sh do_verify refuses the close ONLY on rc=1, and fails open with a WARN
    on any other nonzero rc. So the trip is rc=1 plus a "block" verdict line. The probe
    builds a throwaway world and points the gate at it through the gate's own seams:
    MIND_WORLD for the world, DOMAIN_SUITE_LOG_DIR for the retained log. Both are removed
    afterwards.

    A CRASH IS UNEVALUATABLE FOR THIS ROW, the reverse of the notification-routing row. A
    gate that dies before main() exits 1 with no verdict line, and do_verify reads that as
    REFUSED. Every close is then refused, loudly, with a traceback: that is not silent, so
    it is out of this row's scope. The silent deaths are the ones that let a close through:
      - "pass" on a suite that cannot collect: the gate no longer sees collection errors
      - "noop" on a fixture its own helpers call a touched domain suite (checked in-process
        first): the gate no longer runs the suite at all
      - "error" from main()'s exception handler: evaluate() raises, and every close passes
        unverified
      - "block" with an rc other than 1: do_verify does not read that as a refusal
    "pass" and "block" count only when the verdict's touched list names the fixture's test
    file. Otherwise the gate measured some other world, and the row says so.

    WHAT THIS ROW DOES NOT WATCH: the goal-record claimed_at lookup (--since bypasses it),
    the world's run-domain-tests.sh hook (the fixture has none, so the default pytest runner
    runs), the baseline ratchet for ordinary reds, and override logging. A timeout or a
    pytest internal/usage error is the gate's fail-open fault branch, which says nothing
    about a close, so it is UNEVALUATABLE. So is a "block" from the credential tripwire:
    another writer moved a credential-shaped file during the window.

    SIDE-EFFECT FREE BY CONSTRUCTION: every gate write follows the two seams into the
    probe's temp dir, _probe marks the child as a liveness probe so _gate_log records
    nothing, and the probe passes no --override, so no override ledger row is written.
    """
    script_name = "domain-suite-gate.py"

    def _assert() -> tuple[bool | None, str]:
        path = SCRIPT_DIR / script_name
        if not path.is_file():
            return None, f"{script_name} absent at {path}"
        root = Path(tempfile.mkdtemp(prefix="canary-domain-suite-"))
        try:
            scripts = root / "world" / "scripts"
            (scripts / "tests").mkdir(parents=True)
            (root / "logs").mkdir()
            (scripts / "tests" / DOMAIN_SUITE_FIXTURE_TEST).write_text(
                f"import {DOMAIN_SUITE_UNCOLLECTABLE_MODULE}  # noqa: F401\n\n\n"
                "def test_never_runs():\n    assert True\n", encoding="utf-8")
            if check_fixture:
                since = _dt.datetime.fromisoformat(
                    DOMAIN_SUITE_PROBE_ARGS[DOMAIN_SUITE_PROBE_ARGS.index("--since") + 1])
                try:
                    stale = _domain_suite_fixture_check(scripts, since)
                except Exception as exc:
                    stale = f"the fixture check itself failed ({exc})"
                if stale:
                    return None, f"{script_name} probe fixture is stale: {stale}"
            argv = [sys.executable, path.as_posix(), *DOMAIN_SUITE_PROBE_ARGS]
            try:
                rc, out, err = _probe(argv, extra_env={
                    "MIND_WORLD": (root / "world").as_posix(),
                    "DOMAIN_SUITE_LOG_DIR": (root / "logs").as_posix(),
                    "STORAGE_BACKEND": "local",
                })
            except Exception as exc:
                return None, f"{script_name} could not run: {exc}"
        finally:
            shutil.rmtree(root, ignore_errors=True)
        doc = _domain_suite_verdict(out)
        if doc is None:
            last = err.strip().splitlines()[-1][:160] if err.strip() else ""
            return None, (f"{script_name} exited rc={rc} with no verdict line (stdout "
                          f"{len(out)} B{': ' + last if last else ''}). At rc=1 that is a "
                          "crash, which do_verify reads as REFUSED: every close fails "
                          "closed and loud, which is not the silent death this row watches")
        decision = doc.get("decision")
        reason = str(doc.get("reason") or "")[:200]
        measured_fixture = any(
            isinstance(t, (list, tuple)) and t and Path(str(t[0])).name == DOMAIN_SUITE_FIXTURE_TEST
            for t in (doc.get("touched") or []))
        if decision == "error":
            if reason.startswith("gate error"):
                return True, (f"evaluate() raised and main() failed open: {reason}. Every "
                              "close now passes with its domain suite unverified")
            return None, (f"the gate's fail-open fault branch fired ({reason}), which is "
                          "not a verdict on the fixture")
        if decision == "noop":
            return True, (f"noop on a fixture the gate's own helpers call a touched domain "
                          f"suite: {reason}. Every close now skips the suite")
        if not measured_fixture:
            return None, (f"decision {decision!r}, but its touched list does not name "
                          f"{DOMAIN_SUITE_FIXTURE_TEST}: the gate measured some other world, "
                          "so the MIND_WORLD seam may not have held")
        if decision == "block":
            if "credential-shaped" in reason:
                return None, ("the credential tripwire refused, not the suite check: another "
                              f"writer moved a credential-shaped file during the probe ({reason})")
            if rc != 1:
                return True, (f"printed 'block' but exited rc={rc}, and do_verify refuses "
                              "only on rc=1, so the close goes through")
            return False, f"refused the uncollectable fixture (rc=1): {reason}"
        if decision == "pass":
            return True, (f"passed a close whose domain suite cannot collect: {reason}. The "
                          "gate no longer sees a collection error")
        return None, f"unrecognised decision {decision!r} (rc={rc}): {reason}"
    return _assert


LIVENESS_PROBE_AGENT = "zzz-signal-liveness-canary-dormant-fixture"
# The fixture shard's last_active VALUE and its file mtime, both. How stale counts as
# dormant is NOT decided here: the fixture check reads the engine's own threshold.
LIVENESS_PROBE_STALE = "2000-01-01T00:00:00"
# The consumer probe: confirms_dormant as the selector and the daemon claim gate call it.
# It prints one marker line, so a crash (no line) cannot pass for a False.
_LIVENESS_CONSUMER_PROBE = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from gates.reallocation_exempt import confirms_dormant; "
    "import liveness_check; "
    "print('CONFIRMS_DORMANT=' + repr(confirms_dormant(sys.argv[2], sys.argv[3], "
    "threshold_hours=liveness_check.DEFAULT_THRESHOLD_HOURS, world_dir=sys.argv[4])))"
)


def _liveness_fixture_check(world_dir: Path, now: "_dt.datetime") -> "str | None":
    """None while the engine's OWN local reader still finds the fixture shard, and both
    stale signals are older than the engine's OWN DEFAULT_THRESHOLD_HOURS.

    Rot means the shard layout moved (the engine then reads no fixture, and "unknown" is
    CORRECT), or the threshold grew past the fixture's age (a non-dormant verdict is then
    CORRECT). Either way the row cannot judge the engine.

    Only the pure-local reader runs in THIS process. The authoritative read dispatches on
    STORAGE_BACKEND, and this process may run own-cloud, where a temp path maps onto a
    production key (guard-955). The children pin the local backend instead.

    AN IMPORT FAILURE RETURNS None. The probe then crashes the same way, and the row reports
    that crash as DEAD (see _liveness_dormant_assertion).
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "_canary_probe_liveness_check", SCRIPT_DIR / "liveness_check.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        return None
    threshold = getattr(module, "DEFAULT_THRESHOLD_HOURS", None)
    read_mtime = getattr(module, "fetch_local_shard_mtime", None)
    if threshold is None or read_mtime is None:
        return ("liveness_check.py no longer exposes DEFAULT_THRESHOLD_HOURS and "
                "fetch_local_shard_mtime, so the fixture is uncheckable")
    horizon = now - _dt.timedelta(hours=float(threshold))
    mtime = read_mtime(LIVENESS_PROBE_AGENT, str(world_dir))
    if not mtime:
        return ("the engine's own fetch_local_shard_mtime() no longer finds the fixture "
                "shard, so 'unknown' is CORRECT. Fix the fixture layout, not the engine")
    if _dt.datetime.fromisoformat(mtime) > horizon:
        return (f"the fixture shard's mtime ({mtime}) is inside the engine's {threshold:g}h "
                "threshold, so a non-dormant verdict is CORRECT")
    if _dt.datetime.fromisoformat(LIVENESS_PROBE_STALE) > horizon:
        return (f"LIVENESS_PROBE_STALE ({LIVENESS_PROBE_STALE}) is inside the engine's "
                f"{threshold:g}h threshold, so a non-dormant verdict is CORRECT")
    return None


def _liveness_dormant_assertion(check_fixture: bool = True):
    """The liveness-check row ( unit 7): does the liveness engine still CONCLUDE
    DORMANT for an agent whose every signal is decades stale, and does the consumer that
    acts on that verdict still receive it?

    DORMANT is the only verdict anything acts on. gates.reallocation_exempt.confirms_dormant
    (the selector's idle-reallocation and the daemon claim gate) hands a dead agent's routed
    goals to the running fleet only on it, and it catches EVERY exception and returns False.
    So a broken engine is not loud there: it strands every goal routed to a dead agent, and
    nothing reports it. A crash is therefore DEAD for this row, never unevaluatable
    (guard-7231).

    TWO PROBES, one fixture: a throwaway world holding one shard whose last_active VALUE and
    file mtime are both LIVENESS_PROBE_STALE, read with STORAGE_BACKEND=local.
      1. The engine's CLI, liveness_check.py --json, which liveness-check.sh execs. It names
         a verdict, so the detail says WHICH way the engine broke.
      2. confirms_dormant itself, in a child. The CLI can stay dormant while the consumer
         breaks, e.g. a decide_liveness signature change that main() absorbs raises inside
         the consumer's try and reads as "not idle" forever.
    Measured 2026-09-24 (bravo, cc-05) before this row existed: the fixture gives dormant /
    True. Each near-miss falls off it: a fresh shard mtime gives unknown / False, a fresh
    value gives alive / False, and no shard gives unknown / False.

    WHAT THIS ROW DOES NOT WATCH: liveness-check.sh's daemon-routed last_active read and its
    .env load (the probe passes --last-active and pins the local backend); the own-cloud
    fresh signal (an S3 HEAD every iteration, the S-4 exclusion's reason); the
    retirement-tombstone and cross-stamp branches (the fixture carries neither); and the
    selector's age nomination (reallocation_hours), which runs before confirms_dormant is
    ever asked.

    SIDE-EFFECT FREE: the engine only reads, both children read the temp world, and the
    temp dir is removed afterwards.
    """
    script_name = "liveness_check.py"

    def _assert() -> tuple[bool | None, str]:
        path = SCRIPT_DIR / script_name
        if not path.is_file():
            return None, f"{script_name} absent at {path}"
        root = Path(tempfile.mkdtemp(prefix="canary-liveness-"))
        env = {"STORAGE_BACKEND": "local"}
        consumer = None
        try:
            shard = root / "team-state" / "agents" / f"{LIVENESS_PROBE_AGENT}.yaml"
            shard.parent.mkdir(parents=True)
            shard.write_text(f'last_active: "{LIVENESS_PROBE_STALE}"\n', encoding="utf-8")
            stale_epoch = _dt.datetime.fromisoformat(LIVENESS_PROBE_STALE).timestamp()
            os.utime(shard, (stale_epoch, stale_epoch))
            if check_fixture:
                try:
                    stale = _liveness_fixture_check(root, _dt.datetime.now())
                except Exception as exc:
                    stale = f"the fixture check itself failed ({exc})"
                if stale:
                    return None, f"{script_name} probe fixture is stale: {stale}"
            try:
                rc, out, err = _probe([sys.executable, path.as_posix(),
                                       "--agent", LIVENESS_PROBE_AGENT,
                                       "--last-active", LIVENESS_PROBE_STALE,
                                       "--world-dir", root.as_posix(),
                                       "--backend", "local", "--json"], extra_env=env)
                doc = None
                if out.strip().startswith("{"):
                    try:
                        doc, _ = json.JSONDecoder().raw_decode(out.strip())
                    except ValueError:
                        doc = None
                if rc == 0 and isinstance(doc, dict) and doc.get("verdict") == "dormant":
                    consumer = _probe([sys.executable, "-c", _LIVENESS_CONSUMER_PROBE,
                                       SCRIPT_DIR.as_posix(), LIVENESS_PROBE_AGENT,
                                       LIVENESS_PROBE_STALE, root.as_posix()], extra_env=env)
            except Exception as exc:
                return None, f"{script_name} could not run: {exc}"
        finally:
            shutil.rmtree(root, ignore_errors=True)
        if rc != 0 or not isinstance(doc, dict):
            last = err.strip().splitlines()[-1][:160] if err.strip() else ""
            return True, (f"{script_name} CRASHED: rc={rc}, stdout {len(out)} B, no verdict "
                          f"JSON{': ' + last if last else ''}. confirms_dormant swallows the "
                          "same failure as 'not idle', so goals routed to a dead agent are "
                          "never reallocated")
        if doc.get("verdict") != "dormant":
            return True, (f"the engine returned {doc.get('verdict')!r} for an agent whose "
                          f"last_active value and shard mtime are both {LIVENESS_PROBE_STALE}: "
                          f"{str(doc.get('reason') or '')[:200]}. It can no longer conclude "
                          "dormant, so no dead agent's routed goals are ever reallocated")
        crc, cout, cerr = consumer
        line = next((ln.strip() for ln in cout.splitlines()
                     if ln.strip().startswith("CONFIRMS_DORMANT=")), None)
        if crc != 0 or line is None:
            last = cerr.strip().splitlines()[-1][:160] if cerr.strip() else ""
            return True, (f"confirms_dormant could not run (rc={crc}"
                          f"{': ' + last if last else ''}) while the engine's CLI still "
                          "concludes dormant")
        if line.split("=", 1)[1] != "True":
            return True, ("the engine's CLI concludes dormant, but gates.reallocation_exempt."
                          "confirms_dormant returned " + line.split("=", 1)[1] + " for the same "
                          "fixture: its try/except is swallowing an error, so goals routed to "
                          "a dead agent are never reallocated")
        return False, (f"concluded dormant for a shard stale since {LIVENESS_PROBE_STALE}, "
                       "and confirms_dormant agreed")
    return _assert


# ─── product-repo-freshness ( unit 8) ─────────────────────────────

# The fixture checkout's directory name. The gate reports it as the record's `name`, and
# the text channel is judged by whether its banner names this repo at all.
FRESHNESS_PROBE_REPO = "zzz-signal-liveness-canary-freshness-fixture"
# Commits the fixture's upstream holds that its checkout lacks. freshness() trips on ANY
# behind > 0 with ahead == 0 (its `elif behind:` branch; it has no threshold constant to
# read, verified 2026-09-24). 2, not 1, so a gate reporting a boolean as its count
# (True == 1) cannot pass.
FRESHNESS_PROBE_BEHIND = 2


def _freshness_git(cwd: Path, env: dict, *args: str) -> str:
    """git for the FIXTURE only (the gate runs its own). Raises on any non-zero rc."""
    r = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True,
                       text=True, timeout=PROBE_TIMEOUT_S)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} rc={r.returncode}: "
                           f"{(r.stderr or '').strip()[:200]}")
    return (r.stdout or "").strip()


def _freshness_fixture_env(root: Path) -> dict:
    """The fixture builder's git environment, and nobody else's.

    No global or system config: a box-level commit.gpgsign or core.hooksPath would fail or
    redirect the fixture's commits (guard-4761). A fixed identity, no prompt, and a ceiling
    at the temp root, so a failed init can never walk UP into an enclosing repo and commit
    there (guard-1276). The GATE is probed with the inherited environment, as production
    runs it.
    """
    cfg = root / "empty-gitconfig"
    cfg.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update({"GIT_CONFIG_GLOBAL": str(cfg), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "signal-liveness-canary",
                "GIT_AUTHOR_EMAIL": "canary@invalid",
                "GIT_COMMITTER_NAME": "signal-liveness-canary",
                "GIT_COMMITTER_EMAIL": "canary@invalid",
                "GIT_TERMINAL_PROMPT": "0", "GIT_CEILING_DIRECTORIES": str(root)})
    return env


def _freshness_fixture_build(root: Path, env: dict, behind: int) -> Path:
    """A checkout `behind` commits behind its tracked upstream and 0 ahead of it.

    The upstream is a BARE REPO ON LOCAL DISK, so the gate's own fetch in the production
    call shape stays network-free: no host, no credential, nothing for a VPN blip to flake.
    The S-4 exclusion's reason therefore does not apply to this gate.
    """
    origin, seed = root / "origin.git", root / "seed"
    repo = root / FRESHNESS_PROBE_REPO
    _freshness_git(root, env, "init", "--quiet", "--bare", "-b", "main", origin.as_posix())
    _freshness_git(root, env, "init", "--quiet", "-b", "main", seed.as_posix())
    (seed / "probe.txt").write_text("0\n", encoding="utf-8")
    _freshness_git(seed, env, "add", "probe.txt")
    _freshness_git(seed, env, "commit", "--quiet", "-m", "c0")
    _freshness_git(seed, env, "push", "--quiet", origin.as_posix(), "main")
    _freshness_git(root, env, "clone", "--quiet", origin.as_posix(), repo.as_posix())
    for i in range(1, behind + 1):
        (seed / "probe.txt").write_text(f"{i}\n", encoding="utf-8")
        _freshness_git(seed, env, "commit", "--quiet", "-am", f"c{i}")
    if behind:
        _freshness_git(seed, env, "push", "--quiet", origin.as_posix(), "main")
        _freshness_git(repo, env, "fetch", "--quiet", "origin")
    return repo


def _freshness_fixture_check(repo: Path, env: dict, behind: int) -> "str | None":
    """None while the gate's OWN selection predicate still admits the fixture, and plain git
    still measures it `behind` commits behind a tracked upstream and 0 ahead.

    THE COUNTS ARE DELIBERATELY NOT READ THROUGH THE GATE. This row asks whether freshness()
    still reports a stale checkout, so the fixture's state is measured by something that is
    not freshness(). Reading it back through the gate's own rev-list would let a gate that
    miscounts certify its own fixture. What IS read from the gate is its selection SSOT,
    _is_repo(): if that stops admitting the fixture, examining nothing is CORRECT and says
    nothing about the verdict path.

    Rot therefore means the fixture did not come out as built (a git whose clone sets no
    upstream, a changed builder), or the gate's selection moved under it. Either way a
    non-trip is CORRECT and the row cannot judge the gate.

    AN IMPORT FAILURE RETURNS None. The probe then crashes the same way, and the row reports
    that crash as DEAD (see _freshness_behind_assertion).
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "_canary_probe_product_repo_freshness", SCRIPT_DIR / "product-repo-freshness.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        return None
    is_repo = getattr(module, "_is_repo", None)
    if is_repo is None:
        return "product-repo-freshness.py no longer exposes _is_repo, so the fixture is uncheckable"
    if not is_repo(repo):
        return ("the gate's own _is_repo() no longer admits the fixture, so examining nothing "
                "is CORRECT. Fix the fixture layout, not the gate")
    try:
        top = _freshness_git(repo, env, "rev-parse", "--show-toplevel")
        _freshness_git(repo, env, "rev-parse", "--abbrev-ref", "@{upstream}")
        lag = int(_freshness_git(repo, env, "rev-list", "--count", "HEAD..@{upstream}"))
        lead = int(_freshness_git(repo, env, "rev-list", "--count", "@{upstream}..HEAD"))
    except Exception as exc:
        return (f"plain git cannot read the fixture's upstream ({exc}), so a no-upstream or "
                "unknown answer from the gate is CORRECT")
    if Path(top).resolve() != repo.resolve():
        return f"git resolves the fixture to {top}, not to itself (guard-1276)"
    if (lag, lead) != (behind, 0):
        return (f"plain git measures the fixture {lag} behind / {lead} ahead, not {behind} / 0, "
                "so the gate's answer describes another state. Fix the fixture builder, not "
                "the gate")
    return None


def _freshness_doc(out: str) -> "dict | None":
    """The gate's one --json document (pretty-printed, so it spans lines)."""
    text = out.strip()
    start = text.find("{")
    if start < 0:
        return None
    try:
        doc, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _rmtree_git(root: Path) -> None:
    """rmtree that also removes git's READ-ONLY object files. On Windows an unlink of a
    read-only file fails, so a bare rmtree(ignore_errors=True) would leave one fixture in
    the temp dir on EVERY canary run."""
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                os.chmod(os.path.join(dirpath, name), 0o600)
            except OSError:
                pass
    shutil.rmtree(root, ignore_errors=True)


def _freshness_behind_assertion(check_fixture: bool = True, build_behind: "int | None" = None):
    """The product-repo-freshness row ( unit 8): does the gate still REPORT a
    checkout that is behind its upstream, on both channels its consumers read?

    The gate is advisory and never blocks. It always exits 0, and its __main__ turns every
    exception into rc=0 plus one stderr line. So it dies not as a refusal that stops firing
    but as a stale tree that reads as current: the confident-wrong-finding class it was
    built to stop (g-115-4041). A crash is therefore DEAD here, as in unit 7.

    ONE FIXTURE, TWO PROBES, both in a production call shape:
      1. `--repo <fixture> --json`, the generate-domain-goals shape. That consumer pulls
         every record with behind > 0, so the record must say behind ==
         FRESHNESS_PROBE_BEHIND.
      2. `--repo <fixture>` on the text channel. render() is the same banner the
         pre-execution `--goal-id` call prints, and it is SILENT on a clean repo, so a banner
         that does not name the fixture reads as "in sync". It runs only after the JSON
         record is right, so the detail names the first channel that broke.
    Measured 2026-09-24 (bravo, cc-05, Linux 6.8.0-139-generic, git 2.43.0) before this row
    existed: the fixture gives cannot_check false and behind 2, with and without --no-fetch,
    in ~0.05s. Each near-miss falls off it: in-sync gives behind 0, a checkout with no
    upstream gives behind None, and MIND_AGENT unset gives cannot_check TRUE with the same
    behind-2 record (vacuity() keys on the enumeration, not on --repo).

    THE VERDICT DOES NOT DEPEND ON cannot_check. freshness() and render() never read the
    enumeration, so the fixture's record and banner are the same whether or not this agent's
    AGENT_WRITE_PATH enumerates anything. cannot_check only decides whether the JSON consumer
    reads the records at all, and it is loud when true. So a wrong record is DEAD even under
    cannot_check true (the text channel renders it regardless), while a MISSING record is
    DEAD only under cannot_check false (it says it examined, and examined nothing). Under
    cannot_check true the gate has declined out loud.

    DELIBERATELY NOT REGISTERED: `--check-read`, the read-hazard gate (exit 1 = READ
    HAZARD). Its _repo_for_path() judges only paths inside a MEMBER of the bound agent's
    AGENT_WRITE_PATH enumeration, and that enumeration is the agent's local-paths.conf under
    the real PROJECT_ROOT, with no environment seam. A temp fixture is not a member:
    measured, --check-read on it answers safe / not-a-product-repo at rc 0. A row would need
    a fixture inside a live product root or a fake agent directory, and neither is
    side-effect free. That contract differs from --repo's, so it is named here rather than
    guessed (test_prf_check_read_cannot_reach_a_temp_fixture pins the premise).

    WHAT THIS ROW DOES NOT WATCH: goal-text selection (goal_text and select_repos, the
    --goal-id path, which picks repos by directory name); the enumeration and vacuity()
    themselves (an empty enumeration is loud by design); a fetch from a real remote (the
    fixture's upstream is local, so the network-failure branch is never taken); and the
    --pull, --sweep and --list modes.

    SIDE-EFFECT FREE: the fixture lives in a temp dir, the gate's one write (its own fetch)
    lands inside it, and the temp dir is removed afterwards.
    """
    script_name = "product-repo-freshness.py"

    def _assert() -> tuple[bool | None, str]:
        path = SCRIPT_DIR / script_name
        if not path.is_file():
            return None, f"{script_name} absent at {path}"
        want = FRESHNESS_PROBE_BEHIND
        root = Path(tempfile.mkdtemp(prefix="canary-freshness-"))
        text = None
        try:
            try:
                env = _freshness_fixture_env(root)
                repo = _freshness_fixture_build(
                    root, env, want if build_behind is None else build_behind)
            except Exception as exc:
                return None, (f"{script_name} probe fixture could not be built ({exc}); that "
                              "says nothing about the gate")
            if check_fixture:
                try:
                    stale = _freshness_fixture_check(repo, env, want)
                except Exception as exc:
                    stale = f"the fixture check itself failed ({exc})"
                if stale:
                    return None, f"{script_name} probe fixture is stale: {stale}"
            argv = [sys.executable, path.as_posix(), "--repo", repo.as_posix()]
            try:
                rc, out, err = _probe(argv + ["--json"])
                doc = _freshness_doc(out)
                recs = doc.get("records") if isinstance(doc, dict) else None
                rec = recs[0] if isinstance(recs, list) and len(recs) == 1 else None
                if rc == 0 and isinstance(rec, dict) and rec.get("behind") == want:
                    text = _probe(argv)
            except Exception as exc:
                return None, f"{script_name} could not run: {exc}"
        finally:
            _rmtree_git(root)
        if rc != 0 or doc is None:
            last = err.strip().splitlines()[-1][:160] if err.strip() else ""
            return True, (f"{script_name} CRASHED: rc={rc}, stdout {len(out)} B, no JSON "
                          f"document{': ' + last if last else ''}. Its __main__ turns an "
                          "exception into rc=0 and one advisory line, so on the text channel a "
                          "crash reads as 'every repo in sync'")
        if not isinstance(recs, list) or not recs:
            if doc.get("cannot_check"):
                return None, (f"{script_name} examined nothing and said so (cannot_check: "
                              f"{doc.get('cannot_check_reason')}). Its consumer reads that as NOT "
                              "an all-clear, so this is not a silent pass")
            return True, (f"{script_name} reported cannot_check false and no record for the one "
                          "--repo it was given: it says it examined, and examined nothing")
        if rec is None:
            return True, (f"{script_name} returned {len(recs)} records for one --repo fixture, "
                          "so the fixture's own answer cannot be told apart")
        if rec.get("behind") != want:
            return True, (f"{script_name} reported behind={rec.get('behind')!r} (verdict "
                          f"{rec.get('verdict')!r}: {str(rec.get('detail') or '')[:160]}) for a "
                          f"checkout {want} commit(s) behind its upstream. Its consumers pull "
                          "only when behind > 0, so a stale tree is read as current")
        trc, tout, _terr = text
        if FRESHNESS_PROBE_REPO not in tout:
            return True, (f"{script_name}'s JSON record says the checkout is {want} behind, but "
                          f"its text banner (rc={trc}, {len(tout)} B) does not name it. render() "
                          "is silent on a clean repo, so the pre-execution --goal-id call reads "
                          "this stale checkout as in sync")
        note = (" (cannot_check was true: this agent enumerates no repo, and the verdict path "
                "does not read the enumeration)") if doc.get("cannot_check") else ""
        return False, (f"reported the fixture {want} commit(s) behind on the JSON record and "
                       f"named it on the text banner{note}")
    return _assert


# ─── hot-path-size-gate ( unit 9) ─────────────────────────────────

# The one budgeted path in the fixture repo, and the fixture's own budget registry. A
# ratchet set (no ceiling), so the cap IS the size at HEAD and any growth must refuse.
HOTPATH_PROBE_PATH = "probe/hot.md"
HOTPATH_PROBE_BUDGET = ("sets:\n  - name: signal-liveness-canary-probe\n"
                        f"    paths: [\"{HOTPATH_PROBE_PATH}\"]\n    new_file_cap: 64\n")


def _hotpath_fixture_build(root: Path, env: dict, grow: bool = True) -> tuple[Path, Path]:
    """A repo whose HEAD holds the budget and a 2-byte HOTPATH_PROBE_PATH, with that file
    STAGED at 4 bytes (unchanged when grow is False), plus a commit message with no trailer.
    Shares the unit-8 hermetic git env and teardown."""
    repo = root / "repo"
    _freshness_git(root, env, "init", "--quiet", "-b", "main", repo.as_posix())
    hot = repo / HOTPATH_PROBE_PATH
    hot.parent.mkdir(parents=True)
    hot.write_text("a\n", encoding="utf-8")
    budget = repo / "core" / "config" / "hot-path-budget.yaml"
    budget.parent.mkdir(parents=True)
    budget.write_text(HOTPATH_PROBE_BUDGET, encoding="utf-8")
    _freshness_git(repo, env, "add", "-A")
    _freshness_git(repo, env, "commit", "--quiet", "-m", "base")
    if grow:
        hot.write_text("a\nb\n", encoding="utf-8")
        _freshness_git(repo, env, "add", HOTPATH_PROBE_PATH)
    msg = root / "commit-msg.txt"
    msg.write_text("signal-liveness-canary probe\n", encoding="utf-8")
    return repo, msg


def _hotpath_fixture_check(repo: Path, env: dict) -> "str | None":
    """None while the gate's OWN loader, set lookup and decide() still call the fixture's
    staged growth a violation.

    TWO CONTROLS keep a broken gate from reading as a rotted fixture:
      - the gate's load_budget() must still load the LIVE registry. If it rejects that too,
        the loader is broken for every commit, so return None and let the probe's
        fail-open WARN report it DEAD;
      - decide() is asked about the fixture's growth AND its mirror (a shrink). Growth
        passing while the shrink is refused is a FLIPPED comparison, a gate bug, so again
        None. Growth passing with the shrink also passing is a changed ratchet rule: rot.
    Sizes are measured with plain git, never through the gate's blob_size().

    AN IMPORT FAILURE RETURNS None: the probe then crashes, which blocks every commit
    through the hook (see _hotpath_growth_assertion).
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "_canary_probe_hot_path_size_gate", SCRIPT_DIR / "hot-path-size-gate.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        return None
    load, set_for, decide = (getattr(module, n, None) for n in ("load_budget", "set_for", "decide"))
    if load is None or set_for is None or decide is None:
        return ("hot-path-size-gate.py no longer exposes load_budget, set_for and decide, so the "
                "fixture is uncheckable")
    try:
        load(Path(__file__).resolve().parents[2])
    except Exception:
        return None
    try:
        budget = load(repo)
    except Exception as exc:
        return (f"the gate's own load_budget() still loads the live registry but rejects the "
                f"fixture's ({exc}): the schema moved under HOTPATH_PROBE_BUDGET")
    s = set_for(HOTPATH_PROBE_PATH, budget)
    if s is None or s.get("ceiling") is not None:
        return (f"the gate's own set_for() no longer puts {HOTPATH_PROBE_PATH} in a ratchet set, "
                "so a pass is CORRECT. Fix HOTPATH_PROBE_BUDGET, not the gate")
    try:
        staged = int(_freshness_git(repo, env, "cat-file", "-s", f":{HOTPATH_PROBE_PATH}"))
        head = int(_freshness_git(repo, env, "cat-file", "-s", f"HEAD:{HOTPATH_PROBE_PATH}"))
    except Exception as exc:
        return f"plain git cannot size the fixture ({exc})"
    if decide(staged, head, s["new_file_cap"], None)[0] != "ok":
        return None
    if decide(head, staged, s["new_file_cap"], None)[0] != "ok":
        return None
    return (f"the gate's own decide() allows the fixture's {head} -> {staged} B growth (and the "
            "mirror shrink): the ratchet rule changed, so a pass is CORRECT")


def _hotpath_growth_assertion(check_fixture: bool = True, grow: bool = True):
    """The hot-path-size-gate row ( unit 9): does the commit-msg gate still REFUSE a
    staged hot-path file that grew?

    Production runs it from core/githooks/commit-msg through `_gate`, which blocks the commit
    on ANY non-zero rc. So a crash is fail-closed and loud: UNEVALUATABLE here, like the
    domain-suite row. The silent death is rc 0 on growth, in three shapes this row tells
    apart: the fail-open WARN (budget unreadable, evaluation raised), an OVERRIDE accepted
    from a message with no trailer, and plain silence (the comparison no longer sees it).

    THE FIXTURE is a throwaway repo carrying its own core/config/hot-path-budget.yaml, with a
    2-byte HOTPATH_PROBE_PATH at HEAD staged at 4 bytes and a trailer-free message. Measured
    2026-09-24 (bravo, cc-05) before this row existed, ~0.04s each: grown gives rc 1 naming
    the file; not grown gives rc 0 silent; no budget file gives rc 0 plus WARN; a trailer
    gives rc 0 plus OVERRIDE with the ledger row landing in the probe's temp world.

    WRITES ARE PINNED: the override branch appends to _paths.WORLD_DIR's ledger, so the probe
    runs with MIND_WORLD at a temp world and STORAGE_BACKEND=local (guard-955). A regression
    that reaches that branch writes there, never to the live ledger (measured: live ledger
    line count unchanged across the trailer case).

    WHAT THIS ROW DOES NOT WATCH: the hook's cwd-based repo resolution and GIT_INDEX_FILE
    (the probe passes --repo and uses the normal index); merge-commit exemption; the
    ceiling tier and the new-file cap; and `--check`, the corpus-total ratchet.
    """
    script_name = "hot-path-size-gate.py"

    def _assert() -> tuple[bool | None, str]:
        path = SCRIPT_DIR / script_name
        if not path.is_file():
            return None, f"{script_name} absent at {path}"
        root = Path(tempfile.mkdtemp(prefix="canary-hotpath-"))
        try:
            try:
                env = _freshness_fixture_env(root)
                repo, msg = _hotpath_fixture_build(root, env, grow)
            except Exception as exc:
                return None, (f"{script_name} probe fixture could not be built ({exc}); that "
                              "says nothing about the gate")
            if check_fixture:
                try:
                    stale = _hotpath_fixture_check(repo, env)
                except Exception as exc:
                    stale = f"the fixture check itself failed ({exc})"
                if stale:
                    return None, f"{script_name} probe fixture is stale: {stale}"
            world = root / "world"
            world.mkdir()
            try:
                rc, out, err = _probe([sys.executable, path.as_posix(), "--repo", repo.as_posix(),
                                       "--commit-msg-file", msg.as_posix()],
                                      extra_env={"MIND_WORLD": world.as_posix(),
                                                 "STORAGE_BACKEND": "local"})
            except Exception as exc:
                return None, f"{script_name} could not run: {exc}"
        finally:
            _rmtree_git(root)
        if rc == 1 and HOTPATH_PROBE_PATH in out:
            return False, f"refused the fixture's staged growth (rc=1) and named {HOTPATH_PROBE_PATH}"
        if rc != 0:
            last = (err.strip() or out.strip()).splitlines()[-1][:160] if (err.strip() or out.strip()) else ""
            return None, (f"{script_name} exited {rc} without naming the grown file"
                          f"{': ' + last if last else ''}. The hook's _gate blocks every commit on "
                          "that, so it is loud, not a silent pass")
        if "allowing commit" in out:
            return True, (f"{script_name} FAILS OPEN on a grown hot-path file: "
                          f"{out.strip().splitlines()[0][:200]}")
        if "OVERRIDE" in out:
            return True, (f"{script_name} accepted an override from a commit message with no "
                          "trailer, so every commit bypasses the budget")
        return True, (f"{script_name} passed a staged {HOTPATH_PROBE_PATH} that grew 2 -> 4 B "
                      "(rc=0, silent): it no longer sees hot-path growth")
    return _assert


# ─── goal-selector STRATEGIC-FOCUS INERT banner ( unit 10) ────────

# The aspiration of the probe's one non-lane pool row. The fixture check confirms at run
# time that no live directive lane names it, so that pool really is lane-free.
INERT_PROBE_ASP = "asp-0"
# A truthy agent name, which the floor requires. It routes nothing and claims nothing.
INERT_PROBE_AGENT = "zzz-signal-liveness-canary-inert-fixture"
# Sibling imports resolve here and never through SCRIPT_DIR, so a relocated copy of the
# selector (the mutation tests) still imports the real _paths, wm and the rest.
_INERT_IMPORT_DIR = Path(__file__).resolve().parent
# The child probe. It imports the selector, reads the directive's lanes with the selector's
# OWN loader, then asks the floor and the banner about two one-row pools: one outside every
# lane (the banner must fire) and one inside a lane (it must stay quiet). Each step prints
# one marker line, so the parent can tell an import failure from a swallowed exception from
# a silent banner. argv: import dir, selector path, agent, aspiration, team-state JSON or "".
_INERT_BANNER_PROBE = (
    "import importlib.util, json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "spec = importlib.util.spec_from_file_location('_canary_probe_goal_selector', sys.argv[2])\n"
    "m = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(m)\n"
    "print('IMPORTED=1', flush=True)\n"
    "if sys.argv[5]:\n"
    "    m._TEAM_STATE_CACHE = json.load(open(sys.argv[5], encoding='utf-8'))\n"
    "agent, asp = sys.argv[3], sys.argv[4]\n"
    "try:\n"
    "    lanes = sorted(m.load_strategic_focus()['aspirations'])\n"
    "    print('LANES=' + json.dumps(lanes))\n"
    "    print('FIXTURE_ASP_IN_LANES=' + str(asp in lanes))\n"
    "    row = {'goal_id': 'g-0-0', 'aspiration_id': asp, 'score': 1.0,\n"
    "           'title': 'signal-liveness canary non-lane row'}\n"
    "    _, trip = m.apply_strategic_focus_floor([row], agent)\n"
    "    warned = m.emit_strategic_focus_inert_banner(trip) or []\n"
    "    print('TRIP=' + json.dumps({'lanes': trip.get('lanes'),\n"
    "                               'pool_lane_rows': trip.get('pool_lane_rows'),\n"
    "                               'warnings': len(warned)}))\n"
    "    if lanes:\n"
    "        lane_row = dict(row, goal_id='g-0-1', aspiration_id=lanes[0])\n"
    "        _, hold = m.apply_strategic_focus_floor([lane_row], agent)\n"
    "        quiet = m.emit_strategic_focus_inert_banner(hold) or []\n"
    "        print('HOLD=' + json.dumps({'pool_lane_rows': hold.get('pool_lane_rows'),\n"
    "                                   'warnings': len(quiet)}))\n"
    "except Exception as exc:\n"
    "    print('PROBE_ERROR=' + type(exc).__name__ + ': ' + str(exc)[:200])\n"
)


def _strategic_focus_inert_assertion(check_fixture: bool = True,
                                     team_state: "dict | None" = None):
    """The goal-selector row ( unit 10): does the selector still SAY when the
    standing directive has gone inert?

    WHY THIS SIGNAL. g-318-156's inventory verdicted `goal-selector.sh select` on one
    non-clear output: the STRATEGIC-FOCUS INERT banner. It fires when the directive names
    lanes and not one of their goals reached the ranked pool, the state in which both the
    directive boost and the floor are provably inert. Before the banner existed that state
    was silent, and six diagnoses read DRAINED as OUTRANKED. If the banner goes quiet
    again, the owner's directive can stop steering selection with nothing saying so.

    WHICH WAY A CRASH READS. The selector wraps the floor and the banner in one try/except
    that prints a single stderr line and carries on, so an exception there is a SILENT
    death: DEAD (guard-7231). An import failure stops selection outright, which every loop
    notices at once, so it is loud: UNEVALUATABLE (rb-11742).

    THE FIXTURE is read from the selector's own predicate at run time (outcome 3). The
    lanes come from its own load_strategic_focus(), and the one pool row sits in
    INERT_PROBE_ASP, which the fixture check confirms no lane names. Rot means the directive
    names no lane (no directive, or its wording no longer carries an asp-NNN). Silence is
    then CORRECT, so the row reports UNEVALUATABLE rather than DEAD.

    THE MIRROR. The same child adds one row inside a lane and requires the banner to stay
    quiet. A banner that fires regardless (its pool_lane_rows test gone) no longer tells an
    inert directive from an honored one: DEAD.

    Measured 2026-09-24 (bravo, cc-05) before this row existed: the live directive's seven
    lanes gave one INERT warning for the lane-free pool and none once a lane row was added
    (pool_lane_rows 0 then 1, nothing picked). A team-state with no directive, and one whose
    prose names no asp-NNN, both gave lanes [] and silence. Import 1.1s, probe 1.3s.

    WHAT THIS ROW DOES NOT WATCH: goal-selector.sh's own environment setup (the child runs
    the .py); the call site that feeds the banner the REAL post-filter pool (the probe hands
    the floor a synthetic one); the warning's copy into scorer-verdict.json; the directive
    boost's scoring; and the floor's hoist with its FLOOR banner.

    SIDE-EFFECT FREE: the loader and both calls only read, and the floor's pick only
    reorders the list it is given. Measured: the working tree's status did not change
    across a live run. The probe runs in a child so the selector's imports never enter
    this module's sys.modules (guard-1435). `team_state` is the test seam: it replaces the
    selector's cached team-state read. Production never passes it.
    """
    script_name = "goal-selector.py"

    def _assert() -> tuple[bool | None, str]:
        path = SCRIPT_DIR / script_name
        if not path.is_file():
            return None, f"{script_name} absent at {path}"
        root = None
        try:
            ts_arg = ""
            if team_state is not None:
                root = Path(tempfile.mkdtemp(prefix="canary-inert-"))
                ts_file = root / "team-state.json"
                ts_file.write_text(json.dumps(team_state), encoding="utf-8")
                ts_arg = ts_file.as_posix()
            rc, out, err = _probe([sys.executable, "-c", _INERT_BANNER_PROBE,
                                   _INERT_IMPORT_DIR.as_posix(), path.as_posix(),
                                   INERT_PROBE_AGENT, INERT_PROBE_ASP, ts_arg])
        except Exception as exc:
            return None, f"{script_name} could not run: {exc}"
        finally:
            if root is not None:
                shutil.rmtree(root, ignore_errors=True)
        marks: dict[str, str] = {}
        for ln in out.splitlines():
            key, sep, val = ln.strip().partition("=")
            if sep and key.isupper() and key not in marks:
                marks[key] = val
        last = err.strip().splitlines()[-1][:160] if err.strip() else ""
        if "IMPORTED" not in marks:
            return None, (f"{script_name} could not be imported (rc={rc}"
                          f"{': ' + last if last else ''}). Selection itself stops then, "
                          "which is loud, so this row cannot judge the banner")
        if "PROBE_ERROR" in marks:
            return True, (f"the strategic-focus floor or banner raised "
                          f"{marks['PROBE_ERROR'][:200]}. The selector's own try/except turns "
                          "that into one stderr line, so the directive can go inert with no "
                          "banner")
        try:
            lanes = json.loads(marks["LANES"])
            trip = json.loads(marks["TRIP"])
            hold = json.loads(marks["HOLD"]) if "HOLD" in marks else None
        except (KeyError, ValueError):
            return None, (f"{script_name}'s probe died after import without an exception the "
                          f"selector would swallow (rc={rc}{': ' + last if last else ''}), so "
                          "production would fail loudly too")
        if check_fixture:
            if not lanes:
                return None, ("probe fixture is stale: the selector's own "
                              "load_strategic_focus() finds no asp-NNN lane in team-state "
                              "strategic_focus (no directive, or its wording names none), so "
                              "there is nothing to be inert about and silence is CORRECT")
            if marks.get("FIXTURE_ASP_IN_LANES") == "True":
                return None, (f"probe fixture is stale: the non-lane row's {INERT_PROBE_ASP} "
                              "is itself a directive lane, so the pool is not lane-free. "
                              "Change INERT_PROBE_ASP, not the selector")
        if not trip.get("warnings"):
            return True, (f"the directive names {len(lanes)} lane(s) and the pool held "
                          f"{trip.get('pool_lane_rows')} lane row(s) (the floor saw lanes "
                          f"{trip.get('lanes')}), yet emit_strategic_focus_inert_banner stayed "
                          "silent. The directive can now go inert with nothing saying so")
        if hold is not None and hold.get("warnings"):
            return True, (f"the INERT banner also fired for a pool holding a lane row "
                          f"(pool_lane_rows={hold.get('pool_lane_rows')}), so it no longer "
                          "tells an inert directive from an honored one")
        return False, (f"fired for a pool outside all {len(lanes)} directive lane(s) and stayed "
                       "quiet once a lane row was added")
    return _assert


SIGNALS: list[dict] = [
    {
        "name": "agent-watchdog-tick",
        "what": "agent-watchdog --tick, the per-iteration box-level probe sweep",
        "evidence": "agents/<agent>/session/watchdog-prev-state.json mtime",
        "assertion": _assert_watchdog_prev_state,
        "remedy": (
            "Check whether iteration-close do_productivity_check still invokes "
            "`py -3 core/scripts/agent-watchdog.py --tick`, and whether that "
            "invocation is erroring. The watchdog log is transition-only, so its "
            "silence is NOT evidence either way — the snapshot mtime is."
        ),
    },
    {
        "name": "aspirations-query-read-channel",
        "what": "aspirations-query.sh's stdout — the fleet's goal-store read wrapper (inventory S-3)",
        "evidence": "stdout BYTE COUNT of a --title-contains query that matches nothing",
        "assertion": _assert_query_read_channel,
        "remedy": (
            "rc=0 with zero bytes means the wrapper produced no answer at all. Re-run the same "
            "query WITHOUT 2>/dev/null and read the refusal; check whether the daemon endpoint "
            "or the accepted-flag surface changed. Callers can defend themselves with the same "
            "check this row uses: an examined-and-empty result is '[]', never 0 bytes."
        ),
    },
    {
        "name": "aspirations-query-refusal-channel",
        "what": "aspirations-query.sh's unknown-flag refusal (inventory S-3, the g-115-4733 guard)",
        "evidence": "rc + stderr byte count for a flag that can never be valid",
        "assertion": _assert_query_refusal_channel,
        "remedy": (
            "If rc went to 0, the unknown-flag refusal has regressed to the pre-g-115-4733 "
            "passthrough: the token after the bad flag slides into a positional slot and the "
            "wrong value is written at exit 0. Restore the refusal in the arg loop. This row "
            "cannot see a CALLER's own stderr suppression; that stays a review matter."
        ),
    },
    {
        "name": "exhaustive-search-gate-trigger",
        "what": "exhaustive-search-gate.py, the Q2 gate on capability-absence negations (inventory V-1)",
        "evidence": "rc of a must-trip claim ('is not built', 1 tier / 1 query)",
        "assertion": _gate_trigger_assertion(
            "exhaustive-search-gate.py",
            ("--claim-text", "this capability is not built and does not exist anywhere",
             "--tiers-used", "tree", "--queries-count", "1"),
            "is not built",
            fixture_check=_trigger_fixture_still_trips,
        ),
        "remedy": (
            "Read trigger_matched in the emitted JSON. Null on this input means the trigger "
            "table no longer matches 'is not built' — look for an edit to the pattern list or a "
            "changed --claim-text contract. This gate has NO dedicated test file, so this row is "
            "its only positive control."
        ),
    },
    {
        "name": "verify-before-assuming-gate-trigger",
        "what": "verify-before-assuming-gate.py, the Q2 gate on infrastructure negations (inventory V-2)",
        "evidence": "rc of a must-trip claim ('is down', signals=1)",
        "assertion": _gate_trigger_assertion(
            "verify-before-assuming-gate.py",
            ("--claim-text", "the operator service is down and not responding",
             "--signals-count", "1"),
            "is down",
            fixture_check=_trigger_fixture_still_trips,
        ),
        "remedy": (
            "Read trigger_matched in the emitted JSON. Null means 'is down' stopped matching. "
            "This gate has NO dedicated test file, so this row is its only positive control."
        ),
    },
    {
        "name": "zero-count-gate-trigger",
        "what": "zero-count-gate.py, the Q2 gate on statistical/audit negations (inventory V-3, rb-245)",
        "evidence": "rc of a must-trip claim ('0 records have', probe-result=missing)",
        "assertion": _gate_trigger_assertion(
            "zero-count-gate.py",
            ("--claim-text", "0 records have field utilization across all entries",
             "--file-probed", "core/config/aspirations.yaml",
             "--field-probed", "utilization", "--probe-result", "missing"),
            "0 records have",
            fixture_check=_trigger_fixture_still_trips,
        ),
        "remedy": (
            "Read trigger_matched. This is the one gate of the four that DOES have a test "
            "(core/tests/gates/test_zero_count_gate_triggers.py) — run it; if it is green while "
            "this row is dead, the difference is the invocation shape, not the trigger table. "
            "The probed path is a config file on purpose: the trigger is the CLAIM TEXT, "
            "measured identical for a real path, a config path and a nonexistent one."
        ),
    },
    {
        "name": "positive-state-gate-trigger",
        "what": "positive-state-gate.py, the Q1 gate on positive file-state claims (inventory V-4)",
        "evidence": "rc of a must-trip claim ('handoff.yaml reflects ...') with EMPTY evidence",
        "assertion": _gate_trigger_assertion(
            "positive-state-gate.py",
            ("--claim", "handoff.yaml reflects session 50", "--evidence", ""),
            "handoff.yaml reflects",
            fixture_check=_trigger_fixture_still_trips,
        ),
        "remedy": (
            "Read trigger_matched and paths_extracted in the emitted JSON. rc=0 with EMPTY "
            "evidence means the gate would now accept any narrated file-state claim. This gate "
            "has NO dedicated test file, so this row is its only positive control."
        ),
    },
    {
        "name": "marker-placement-gate-deny-channel",
        "what": "marker-placement-gate.py, the Layer-B gate on domain-marker placement (inventory V-5)",
        "evidence": "stdout permissionDecision for a must-deny PreToolUse payload (the rc cannot tell)",
        # FIRST use of the hook-shaped factory, and the reason it exists. The measured
        # four-case table that put this row on stdout rather than rc, preserved from the
        # plain function this replaced (alpha/cc-07 2026-09-13, all four driven in one
        # pass) — it is the EVIDENCE for the contract choice and must not be lost in the
        # move:
        #     must-deny payload          -> rc=0, 1242 stdout bytes, decision "deny"
        #     marker carries an override -> rc=0, 0 stdout bytes
        #     path out of scope          -> rc=0, 0 stdout bytes
        #     path on the ALLOWLIST      -> rc=0, 0 stdout bytes
        # WHY THIS GATE EARNED A ROW: a live Layer-B precommit-path gate
        # (skill-edit-precommit-gate.py calls it) whose refusal rests on the
        # IN_SCOPE_PATTERNS table, and NOTHING anywhere positive-controls it — measured
        # zero mentions across core/scripts/tests, core/tests/gates and mind_api/tests,
        # against a control query for a covered gate that returned one. Its structure is
        # the always-reports-clear shape in concentrated form: SEVEN early
        # approve_no_mutation() exits plus a bottom `except: sys.exit(0)` catch-all, so
        # every way it can fail, it fails toward allow, silently.
        "assertion": _hook_gate_deny_assertion(
            "marker-placement-gate.py",
            _marker_placement_probe_payload(),
            "every marker placement it exists to refuse",
            fixture_check=_marker_fixture_still_must_deny,
        ),
        "remedy": (
            "An empty stdout means the gate approved a placement it exists to refuse. Check "
            "IN_SCOPE_PATTERNS first (a path-shape drift silently takes every file out of "
            "scope), then the ALLOWLIST, then whether the PreToolUse payload keys this gate "
            "reads (tool_name / tool_input) still match what the harness sends. Do NOT read a "
            "non-zero rc as the failure: this hook exits 0 on deny AND on approve."
        ),
    },
    {
        "name": "capability-gate-trigger",
        "what": (
            "capability-gate.py, the capability-routing chokepoint that refuses a "
            "participants:[user] or a defer_reason naming work the agent can do itself "
            "(inventory V-6)"
        ),
        "evidence": "rc of a must-trip failure_reason ('commit and push', intended user)",
        # FIFTH reuse of the rc-shaped helper, and it was verified to FIT rather than
        # assumed: driven both ways on alpha/cc-07 2026-09-13, a must-block reason returns
        # rc=1 with a populated `matches`, and a genuinely-human one ("a physical hardware
        # token inserted by a person") returns rc=0 with `matches: []`. That two-way
        # discrimination is what the marker-placement row could NOT get from rc, which is
        # why that one reads stdout instead. Check the contract per gate; do not inherit it.
        "assertion": _gate_trigger_assertion(
            "capability-gate.py",
            ("--failure-reason", "blocked on user to commit and push the change",
             "--intended-participants", "user", "--output", "json"),
            "commit and push",
            fixture_check=_capability_fixture_vocabulary_present,
        ),
        "remedy": (
            "rc=0 here means the gate APPROVED a routing it exists to refuse, so every "
            "defer_reason and every participants:[user] now passes unchallenged. Read "
            "`matches` and `keywords_extracted` in the emitted JSON: an empty `matches` "
            "beside populated keywords points at the capability CORPUS "
            "(world/conventions/capability-routing.md, world/forged-skills.yaml) having "
            "moved or failed to load, not at the keyword extractor — the corpus lives on "
            "an EXTERNAL path, so a world-path misresolution empties it silently. This "
            "gate guards THREE call sites (CREATE_BLOCKER Step 2.6, aspirations.py "
            "cmd_update_goal on defer_reason, and /notify-user Step 1.5), so one silent "
            "failure reopens all three at once. Note rc=1 is treated as ALIVE, so a "
            "usage error that also exits 1 would read as healthy: if this row never goes "
            "DEAD across a corpus change you expected it to catch, check the invocation "
            "before trusting the green."
        ),
    },
    {
        "name": "schedule-wakeup-gate-deny-channel",
        "what": (
            "schedule-wakeup-gate.py, the PreToolUse gate on ScheduleWakeup prompts "
            "(inventory V-7)"
        ),
        "evidence": "stdout permissionDecision for a must-deny prompt (the rc cannot tell)",
        # SECOND hook-shaped row, and the one that licensed the factory above. It earns a
        # row on the same argument V-5 did: its refusal is the ONLY thing standing between
        # the loop and two measured loop-killers — a slash-prefix prompt that fires as USER
        # input and is rejected at the user-invocable gate (the 2026-05-18 origin incident),
        # and a `stop: true` cancel of the deadman net on a RUNNING agent, which converts a
        # recoverable text-death into a hard stop needing a human. It is also the gate EVERY
        # worker turn in this fleet passes through, since the worker net arms a
        # natural-language prompt THROUGH it.
        #
        # THE OTHER REFUSAL IS DELIBERATELY NOT THE PROBE. The stop:true-while-RUNNING
        # branch depends on live agent-state, so it cannot be a deterministic canary input —
        # a row whose verdict moves with the box's own state is the always-ALARM twin of
        # this defect. One row, one argument-driven predicate.
        "assertion": _hook_gate_deny_assertion(
            "schedule-wakeup-gate.py",
            {"tool_name": "ScheduleWakeup",
             "tool_input": {"prompt": "/aspirations loop", "delaySeconds": 600}},
            "every slash-prefix prompt it exists to refuse",
            fixture_check=_swakeup_fixture_still_must_deny,
        ),
        "remedy": (
            "An empty stdout means the gate would now APPROVE a slash-prefix prompt it "
            "exists to refuse, which re-arms the 2026-05-18 loop-killer: the prompt fires as "
            "user input and the slash-command resolver rejects it, burning the turn the loop "
            "needed to continue. Check _swakeup_predicate.py FIRST — it is the SSOT shared "
            "with the Layer-C detective aspirations-rejection-audit.py, so one predicate "
            "drift kills BOTH layers at once and neither says so. Then check whether the "
            "payload keys this gate reads (tool_name / tool_input.prompt) still match what "
            "the harness sends. Do NOT read rc: this hook exits 0 on deny AND on approve."
        ),
    },
    {
        "name": "blocker-create-gate-schema-probe",
        "what": (
            "blocker-create-gate.py check 3, the CREATE_BLOCKER Step 2.55 refusal of a "
            "statistical negation filed without schema_probe_evidence (rb-245)"
        ),
        "evidence": "schema_probe's own passed flag in checks[], for a must-trip blocker JSON on stdin",
        # The first PRODUCT-PATH row of : if this refusal dies, a blocker built
        # on an unverified "0 records have X" is filed, and the goals it blocks stop for
        # a reason nothing ever checked. rc=1 alone would also be carried by a sibling
        # check, so the row reads schema_probe's own entry (guard-1082).
        "assertion": _gate_trigger_assertion(
            "blocker-create-gate.py",
            (),
            "a statistical negation without schema_probe_evidence",
            fixture_check=_stat_neg_fixture_check(BLOCKER_STAT_NEG_PAYLOAD),
            stdin_text=json.dumps(BLOCKER_STAT_NEG_PAYLOAD),
            refusal_check=_named_check_refused("schema_probe"),
        ),
        "remedy": (
            "Read checks[] in the emitted JSON. schema_probe passed:true on this payload "
            "means _check_schema_probe no longer refuses a statistical negation without "
            "schema_probe_evidence. Check gates/blocker_create._STAT_NEG_PATTERNS first: an "
            "EMPTY table reports DEAD here on purpose. The row reads schema_probe's own "
            "entry, so a sibling check failing on this payload cannot hide a dead check 3."
        ),
    },
    {
        "name": "pending-deploys-has-pending",
        "what": (
            "pending-deploys.py has-pending, the fast exit of the closure gate that refuses "
            "clean success while a CI deploy is unverified (pending-deploys-gate.sh, guard-119)"
        ),
        "evidence": "has-pending rc on an isolated store the tracker's own `add` has just written",
        # The second PRODUCT-PATH row of , and the one nearest paying users: if
        # this dies, a goal that pushed a failing deploy closes clean and the failure
        # waits for a human to notice. rc=0 is the trip here, the reverse of the
        # rc-shaped family, so it has its own assertion (see its docstring).
        "assertion": _pending_obligation_assertion(PENDING_DEPLOYS_PROBE_ENTRY),
        "remedy": (
            "Run `python3 core/scripts/pending-deploys.py --store <tmp> add ...` then "
            "`has-pending --goal-id <id>` by hand, WITHOUT discarding stderr. Non-empty stderr "
            "at rc=1 means the module no longer loads; empty stderr means _load() read the "
            "writer's own store as [] — check that _save and _load still agree on the format "
            "and that the goal-id filter still matches what add stores. The gate discards "
            "both streams, so nothing else in the fleet will say this."
        ),
    },
    {
        "name": "findings-gate-signal-scan",
        "what": (
            "findings-gate.py, the Step 8.5 scan that turns a close's unresolved finding "
            "into a follow-up goal (iteration-close.sh do_state_update, worker_retrospective)"
        ),
        "evidence": "the stdout findings_count line for a must-trip insight under --dry-run",
        # The third PRODUCT-PATH row of : if this scan dies, every close that names
        # a defect it did not fix closes clean and the follow-up is never filed. rc is 0
        # either way, so the row reads the count line (see its docstring).
        "assertion": _findings_gate_assertion(FINDINGS_GATE_PROBE_INSIGHT),
        "remedy": (
            "Run `python3 core/scripts/findings-gate.py --goal probe-only-x --aspiration "
            "probe-only-x --category probe --dry-run --insight-file <file>` by hand with "
            "FINDINGS_GATE_PROBE_INSIGHT in the file, WITHOUT discarding stderr. No "
            "findings_count line means the script dies before it reports; findings_count=0 "
            "means scan_signals rejected an insight its own SIGNAL_PATTERNS still match, so "
            "read its negation, window and degenerate filters first."
        ),
    },
    {
        "name": "notification-routing-gate-suppress",
        "what": (
            "notification_routing_gate.py, the owner-inbox guard that SUPPRESSes a "
            "fleet-handleable status report (the notify path's routing step, and the shell "
            "lane via notification-routing-gate.sh)"
        ),
        "evidence": "rc=1 plus the stdout 'SUPPRESS:' line for a must-suppress 'info' report",
        # The fourth PRODUCT-PATH row of . Both deaths are silent. If the gate
        # stops suppressing, every status report reaches the owner's inbox. If it crashes,
        # the shell lane reads the crash's rc=1 as SUPPRESS and drops everything, even
        # decision-needed mail. So a crash is DEAD here, not unevaluatable.
        "assertion": _gate_trigger_assertion(
            "notification_routing_gate.py",
            ROUTING_GATE_PROBE_ARGS,
            "a fleet-handleable status report",
            fixture_check=_routing_fixture_check,
            refusal_check=_routing_suppress_line,
            crash_is_dead=True,
        ),
        "remedy": (
            "rc=0 means the gate now SENDs a report it exists to suppress; read the 'SEND:' "
            "reason in the detail. 'routing gate raised' there means decide() threw and "
            "decide_and_log's fail-safe sent. Otherwise check FLEET_HANDLEABLE_CATEGORIES "
            "and HUMAN_ONLY_PATTERNS for an edit that now matches everything. CRASHED means "
            "the module no longer runs: run `python3 core/scripts/notification_routing_gate.py "
            "--category info --subject x` by hand WITHOUT discarding stderr. Until that is "
            "fixed, notification-routing-gate.sh passes the crash's rc=1 through as "
            "SUPPRESS, so shell-lane notifications are dropped, decision-needed included."
        ),
    },
    {
        "name": "domain-suite-gate-collect-refusal",
        "what": (
            "domain-suite-gate.py, the close-phase gate that refuses status=completed while "
            "the world's domain test suite cannot collect or is newly red "
            "(iteration-close.sh do_verify)"
        ),
        "evidence": "rc=1 plus a 'block' verdict line for a throwaway world whose suite cannot collect",
        # The fifth PRODUCT-PATH row of . do_verify refuses only on rc=1 and fails
        # open on everything else, so each silent death lets a close through with a broken
        # domain suite. A crash fails closed and loud there, so it is UNEVALUATABLE here.
        "assertion": _domain_suite_gate_assertion(),
        "remedy": (
            "Build a scratch world whose scripts/tests/ holds one test importing a missing "
            "module, then run `MIND_WORLD=<scratch>/world DOMAIN_SUITE_LOG_DIR=<scratch>/logs "
            "STORAGE_BACKEND=local python3 core/scripts/domain-suite-gate.py --goal probe-x "
            "--since 2000-01-01T00:00:00 --timeout 20` by hand, WITHOUT discarding stderr. "
            "'pass' means run_suite or the rc==2 branch in evaluate() no longer sees a "
            "collection error. 'noop' means evaluate() stopped using has_domain_tests or "
            "touched_since. 'gate error, fail-open' names the exception evaluate() raises."
        ),
    },
    {
        "name": "liveness-check-dormant-verdict",
        "what": (
            "liveness_check.py, the engine liveness-check.sh execs, and the consumer that acts "
            "on its one actionable verdict: gates.reallocation_exempt.confirms_dormant (the "
            "selector's idle-reallocation and the daemon claim gate)"
        ),
        "evidence": "verdict 'dormant' from the CLI and True from confirms_dormant for a throwaway shard stale since 2000",
        # The sixth PRODUCT-PATH row of . DORMANT is the only verdict anything acts
        # on, and the consumer that acts on it swallows every exception as "not idle", so
        # each death strands goals routed to a dead agent without a sound. A crash is DEAD.
        "assertion": _liveness_dormant_assertion(),
        "remedy": (
            "Build a scratch world holding team-state/agents/<a>.yaml with last_active "
            "\"2000-01-01T00:00:00\" and a 2000-01-01 mtime, then run `STORAGE_BACKEND=local "
            "python3 core/scripts/liveness_check.py --agent <a> --last-active "
            "2000-01-01T00:00:00 --world-dir <scratch> --backend local --json` by hand, WITHOUT "
            "discarding stderr. A verdict other than dormant names its decide_liveness branch "
            "in 'reason'. If the CLI says dormant but confirms_dormant returned False, call "
            "gates.reallocation_exempt.confirms_dormant with the same inputs with its "
            "try/except removed in a local copy: the exception it was swallowing is the defect."
        ),
    },
    {
        "name": "product-repo-freshness-behind",
        "what": (
            "product-repo-freshness.py, the advisory that tells a goal its product checkout is "
            "behind its upstream before the goal reads or edits it (the --repo --json record "
            "and the text banner)"
        ),
        "evidence": "a behind-2 record and a banner naming a throwaway checkout 2 commits behind its local bare upstream",
        # The seventh PRODUCT-PATH row of . The gate never blocks and turns every
        # exception into rc=0, so each death reads as "in sync" and a stale tree is read as
        # current. A crash is DEAD. --check-read is deliberately NOT registered: see
        # _freshness_behind_assertion's docstring.
        "assertion": _freshness_behind_assertion(),
        "remedy": (
            "Build a scratch clone 2 commits behind a local bare repo (git init --bare; clone; "
            "commit twice in a second clone and push; fetch in the first), then run `python3 "
            "core/scripts/product-repo-freshness.py --repo <clone> --json` and the same without "
            "--json by hand, WITHOUT discarding stderr. behind 0 with ahead 2 means the "
            "rev-list range in freshness() is reversed. No JSON on stdout plus an 'advisory "
            "probe failed' line names the exception __main__ swallowed. A right record with a "
            "banner that omits the repo means render() now counts 'behind' as clean."
        ),
    },
    {
        "name": "hot-path-size-gate-growth-refusal",
        "what": (
            "hot-path-size-gate.py, the commit-msg gate that refuses a commit leaving an "
            "always-loaded prose file larger than at HEAD"
        ),
        "evidence": "rc=1 naming the file for a throwaway repo whose budgeted file is staged 2 -> 4 B with no override trailer",
        # The eighth PRODUCT-PATH row of . The hook blocks on any non-zero rc, so a
        # crash is loud: UNEVALUATABLE. rc 0 on growth is the silent death, in three shapes.
        "assertion": _hotpath_growth_assertion(),
        "remedy": (
            "Build a scratch repo with core/config/hot-path-budget.yaml naming one file, commit "
            "it small, stage it larger, and run `MIND_WORLD=<scratch>/world STORAGE_BACKEND=local "
            "python3 core/scripts/hot-path-size-gate.py --repo <scratch> --commit-msg-file <msg>` "
            "by hand. A WARN line names the exception load_budget or evaluate raised. OVERRIDE "
            "means parse_override matched a line with no trailer. Silence means decide() or "
            "staged_changes() no longer sees the growth."
        ),
    },
    {
        "name": "goal-selector-strategic-focus-inert",
        "what": (
            "goal-selector.py's STRATEGIC-FOCUS INERT banner, the one line that says the "
            "standing directive names lanes none of whose goals reached the ranked pool"
        ),
        "evidence": "one INERT warning for a one-row pool outside every live directive lane, and none once a lane row is added",
        # The ninth PRODUCT-PATH row of . The selector wraps the floor and the
        # banner in a try/except that prints one stderr line, so an exception there is DEAD.
        # An import failure stops selection outright, so it is loud: UNEVALUATABLE.
        "assertion": _strategic_focus_inert_assertion(),
        "remedy": (
            "Import core/scripts/goal-selector.py with importlib in a python3 shell, then call "
            "apply_strategic_focus_floor([{'goal_id': 'g-0-0', 'aspiration_id': 'asp-0', "
            "'score': 1.0}], 'x') and pass its status to emit_strategic_focus_inert_banner by "
            "hand. Empty status lanes mean load_strategic_focus() no longer parses team-state "
            "strategic_focus. pool_lane_rows above 0 means the floor counts non-lane rows as "
            "lane rows. An empty return with lanes and 0 pool rows means the banner's own "
            "predicate changed. A banner that also fires once a lane row is in the pool has "
            "lost its pool_lane_rows test."
        ),
    },
]


def _run_assertion(signal: dict) -> tuple[bool | None, str]:
    try:
        return signal["assertion"]()
    except Exception as exc:  # a broken assertion must never manufacture a count
        return None, f"assertion raised: {exc}"


def _recent_investigate_exists(signal_name: str, since_hours: int = DEDUP_HOURS) -> bool:
    """Suppress a duplicate canary Investigate (mirrors both siblings).

    guard-487 (suppression gates fail CLOSED): on any queue READ error, return
    True (suppress). A swallowed error mapping to "no duplicate found" would
    re-enable the spam this gate exists to stop. The post-fire counter reset
    makes a genuinely-dead signal re-trip and re-alert once the read recovers.
    """
    origin = f"investigate:signal-liveness-canary:{signal_name}"
    cutoff = _dt.datetime.now() - _dt.timedelta(hours=since_hours)
    candidates = []
    if WORLD_DIR is not None:
        candidates.append(Path(WORLD_DIR) / "aspirations.jsonl")
    if AGENT_DIR is not None:
        candidates.append(Path(AGENT_DIR) / "aspirations.jsonl")
    for path in candidates:
        try:
            if not path.exists():
                continue
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        asp = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # one bad line — skip, not a gate-disabling error
                    for g in asp.get("goals", []):
                        if (g.get("origin_signal") or "") != origin:
                            continue
                        if g.get("status") in ("pending", "in-progress"):
                            return True
                        created = (g.get("created_at") or g.get("created_date")
                                   or g.get("created"))
                        if not created:
                            return True  # origin match without date — assume recent
                        try:
                            c_dt = _dt.datetime.fromisoformat(str(created))
                        except (ValueError, TypeError):
                            continue
                        if c_dt > cutoff:
                            return True
        except Exception:
            return True  # guard-487: fail CLOSED
    return False


def _file_investigate(signal: dict, stuck: int, detail: str, dry_run: bool) -> dict:
    """File the dead-signal goal. `_file_investigate` is the fleet-wide canary
    helper name (same in both siblings, fleet-config-parity, completed-not-committed).
    """
    name = signal["name"]
    title = (
        f"Investigate: the {name} signal has stopped being able to report — "
        f"dead for {stuck} consecutive canary runs"
    )
    description = (
        f"WHAT IS DEAD: {signal.get('what', name)}.\n"
        f"EVIDENCE READ: {signal.get('evidence', 'see the assertion')}.\n"
        f"ASSERTION OUTPUT AT FIRE TIME: {detail}\n\n"
        f"WHY THIS IS WORTH A GOAL. A detector that can no longer produce a "
        f"non-clear answer renders IDENTICALLY to one reporting all-clear, so "
        f"nothing downstream can tell them apart and the fleet loses the signal "
        f"silently. That is the g-318-156 class, and the 2026-08-11 owner "
        f"directive puts time-to-detection above everything else.\n\n"
        f"FIRST STEP: {signal.get('remedy', 'Re-run the assertion by hand and confirm the artifact it reads.')}\n\n"
        f"READ THE VERDICT, NOT THE SILENCE. This canary counts CONSECUTIVE DEAD "
        f"verdicts ({stuck} of them here) and resets on the first live one, so a "
        f"single blip cannot file this goal. An assertion that could not be "
        f"EVALUATED also resets the counter and never fires — so this goal means "
        f"the assertion ran and said dead, {stuck} times in a row.\n\n"
        f"Filed by signal-liveness-canary (g-318-156 outcome 3); threshold "
        f"`signal_liveness.threshold_iterations` controls sensitivity."
    )
    payload = {
        "title": title,
        "description": description,
        "priority": "HIGH",
        "participants": ["agent"],
        "category": "framework-architecture",
        "origin_signal": f"investigate:signal-liveness-canary:{name}",
        "work_class": "framework",
        "intended_agent": "either",
        "tags": [
            "signal-liveness",
            "signal-liveness-canary",
            "defense-in-depth",
            f"signal:{name}",
        ],
    }
    if dry_run:
        return {
            "dry_run": True,
            "payload_title": title,
            "payload_description": description,
        }

    # SCRIPT_DIR is ALWAYS absolute and cwd-independent, unlike PROJECT_ROOT,
    # which can resolve relative inside a nested subprocess (this canary runs
    # nested: iteration-close do_productivity_check -> here). Mirrors both siblings.
    script_path = (SCRIPT_DIR / "aspirations-add-goal.sh").as_posix()

    def _run_add(extra):
        return subprocess.run(
            bash_cmd(script_path, "--source", ASP_SOURCE, ASP_ID, *extra),
            input=json.dumps(payload),
            capture_output=True, text=True, timeout=30,
        )

    try:
        result = _run_add([])
    except Exception as exc:
        return {"error": "subprocess_failed", "detail": str(exc)}
    # The duplication gate's prose-overlap heuristic false-positives on canary
    # Investigates BY CONSTRUCTION (shared vocabulary with any goal about the same
    # signal). This canary runs UNATTENDED, so a bare rc-nonzero return silently
    # drops the alert. Machine-key dedup already ran upstream on the exact
    # origin_signal, fail-closed, so reaching here means the signal is genuinely
    # new and any dup-block is structural-overlap-only. Mirrors .
    if result.returncode != 0 and "goal_duplication_blocked" in (
        (result.stdout or "") + (result.stderr or "")
    ):
        reason = (
            f"signal-liveness-canary: origin_signal "
            f"investigate:signal-liveness-canary:{name} already dedup'd upstream "
            f"(_recent_investigate_exists, fail-closed); dup-gate prose-overlap on "
            f"canary vocabulary is a structural false-positive (mirrors g-115-2504)"
        )
        try:
            result = _run_add(["--override-duplication", reason])
        except Exception as exc:
            return {"error": "subprocess_failed", "detail": str(exc)}
    if result.returncode != 0:
        return {
            "error": "add_goal_rc_nonzero",
            "rc": result.returncode,
            "stderr": result.stderr.strip()[:300],
        }
    try:
        return {"ok": True, "goal": json.loads(result.stdout.strip())}
    except json.JSONDecodeError:
        return {"ok": True, "stdout": result.stdout.strip()[:300]}


def run(threshold: int, dry_run: bool, assertion_runner=None, signals=None) -> dict:
    """Evaluate every registered signal, update counters under the WM lock, file.

    assertion_runner: injectable (signal) -> (dead|None, detail) for tests.
    signals: injectable registry for tests; defaults to SIGNALS.

    Assertions run BEFORE the WM lock is taken, mirroring the cadence sibling:
    an assertion is free to read working memory, and doing so while holding the
    lock would deadlock against its own read.
    """
    runner = assertion_runner or _run_assertion
    registry = SIGNALS if signals is None else signals
    report: dict = {
        "checked_at": _now_iso(),
        "threshold_iterations": threshold,
        "dry_run": dry_run,
        "signals": {},
        "investigate_goals_filed": [],
        "investigate_goals_suppressed": [],
    }

    if AGENT_DIR is None:
        report["skipped"] = "no_agent_bound"
        return report

    # LOCK-FREE assertion collection (see docstring).
    states: list[tuple[dict, bool | None, str]] = []
    for sig in registry:
        dead, detail = runner(sig)
        states.append((sig, dead, detail))

    from wm import wm_path as _resolve_wm_path  # Phase 1A per-Body WM routing ()
    wm_path = _resolve_wm_path()
    if not wm_path.exists():
        report["skipped"] = "no_working_memory_file"
        return report

    lock_path = wm_path.with_suffix(".lock")
    try:
        acquire_lock(lock_path, stale_seconds=10)  # mirrors wm.py wm_lock
    except Exception as exc:
        report["skipped"] = f"lock_acquire_failed: {exc}"
        return report

    fired_records: list[tuple[dict, int, str]] = []
    try:
        try:
            data = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            report["skipped"] = f"yaml_load_failed: {exc}"
            return report

        slots = data.setdefault("slots", {})
        counters = slots.get(CANARY_SLOT)
        if not isinstance(counters, dict):
            counters = {}

        for sig, dead, detail in states:
            name = sig["name"]
            prev = int(counters.get(name, 0) or 0)
            entry: dict = {
                "dead": dead,
                "detail": detail,
                "prev_stuck_count": prev,
                "new_stuck_count": 0,
                "fired": False,
            }
            if dead is None:
                # Unevaluatable — fail-open: reset and surface. A broken assertion
                # must never manufacture a stuck count (always-ALARM is the same
                # defect as always-CLEAR;  unit 1 Finding B).
                new_count = 0
            elif dead:
                new_count = prev + 1
            else:
                new_count = 0

            entry["new_stuck_count"] = new_count

            if new_count >= threshold:
                entry["fired"] = True
                fired_records.append((sig, new_count, detail))
                new_count = 0  # reset post-fire so the next death re-starts counting

            counters[name] = new_count
            report["signals"][name] = entry

        if not dry_run:
            slots[CANARY_SLOT] = counters
            tmp = wm_path.with_suffix(".yaml.tmp")  # atomic rename, matches wm.py
            tmp.write_text(
                yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            tmp.replace(wm_path)
    finally:
        try:
            release_lock(lock_path)
        except Exception:
            pass

    # File AFTER releasing the WM lock so the aspirations-add-goal subprocess
    # cannot deadlock against any nested WM access during validation.
    for sig, stuck, detail in fired_records:
        name = sig["name"]
        if not dry_run and _recent_investigate_exists(name):
            report["signals"][name]["filing_result"] = {"suppressed_dedup": True}
            report["investigate_goals_suppressed"].append({
                "signal": name,
                "stuck_count": stuck,
                "reason": "open_or_recent_duplicate",
            })
            continue
        filing = _file_investigate(sig, stuck, detail, dry_run)
        report["signals"][name]["filing_result"] = filing
        report["investigate_goals_filed"].append({
            "signal": name,
            "stuck_count": stuck,
            "result": filing,
        })

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Signal-liveness canary (g-318-156 outcome 3)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Do not file Investigate goals and do not persist counters.")
    parser.add_argument(
        "--threshold", type=int, default=None,
        help="Override signal_liveness.threshold_iterations from aspirations.yaml.")
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress JSON output on stdout when no Investigate goals fired.")
    args = parser.parse_args(argv)

    threshold = _read_threshold(args.threshold)
    report = run(threshold=threshold, dry_run=args.dry_run)

    if args.quiet and not report.get("investigate_goals_filed"):
        return 0

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
