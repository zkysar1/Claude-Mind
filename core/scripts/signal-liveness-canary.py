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
import subprocess
import sys
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
# TWO INVENTORY MEMBERS ARE DELIBERATELY NOT REGISTERED HERE, and saying so is part
# of the row set rather than an omission:
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


def _probe(argv: list[str], stdin_text: str | None = None) -> tuple[int, str, str]:
    """Run an instrument and return (rc, stdout, stderr). Raises if it cannot run.

    `stdin_text` exists for the PreToolUse-hook gates, which take their whole
    input as JSON on stdin rather than in argv. Default None keeps every
    pre-existing call byte-identical.

    Every probe carries LIVENESS_PROBE_ENV so the probed gate does not log its
    must-trip refusal as a production firing (g-318-168, _gate_log docstring).
    """
    r = subprocess.run(argv, input=stdin_text, capture_output=True, text=True,
                       timeout=PROBE_TIMEOUT_S,
                       env={**os.environ, LIVENESS_PROBE_ENV: "signal-liveness-canary"})
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
                            refusal_check=None):
    """ONE shape, four rows (V-1..V-4): a Q1/Q2 gate whose rc=0 means BOTH 'evidence
    sufficient' AND 'no trigger matched'.

    `stdin_text` is for a gate that takes its input as JSON on stdin rather than in
    argv (blocker-create-gate.py, g-318-168). Default None keeps every argv-shaped row
    byte-identical.

    `refusal_check` is for a gate that runs SEVERAL checks, where rc=1 says only that
    SOME check refused (guard-1082: assert the specific refusal, never the coarse rc).
    It reads the refusal stdout and returns None when the watched check is the one
    refusing, else the (verdict, detail) to report. Default None keeps rc=1 sufficient.

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
