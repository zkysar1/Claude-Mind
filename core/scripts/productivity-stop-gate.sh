#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# productivity-stop-gate.sh — script-gated self-stop when productivity is low.
#
# Invoked by iteration-close.sh --phase productivity-check at the end of every
# iteration. Reads loop_state from working memory, computes a composite
# productivity score, and if the score is below threshold AND the session has
# run at least `min_iterations` goals, sets `stop-requested` (after writing
# `stop-target-mode` per the /stop skill's invariant).
#
# This is the "inverse of can't escape work" — prevents padding sessions with
# ritual routine work. Per rule exception in .claude/rules/stop-hook-compliance.md
# (dated 2026-04-18 for productivity-stop-gate): this is the only authorized
# caller of session-signal-set.sh stop-requested outside /stop.
#
# Composite productivity score (0.0 clean routine to 1.0 all-deep-encoded):
#   routine_ratio  = min(1, routine_count_total / goals_completed)
#     — clamped because routine_count_total is session-cumulative and can
#     exceed goals_completed when recurring goals fire multiple times.
#     Without the clamp, (1 - routine_ratio) goes negative and the score
#     stays sub-threshold no matter how much productive work follows
#     (2026-05-11 bravo: routine=28, completed=13 → score never recovered).
#   encoding_ratio = min(1, weighted_artifacts / goals_completed)
#     where weighted_artifacts = sum(count[k] * weight[k]) across the
#     per-key counts emitted by session_artifacts_count.py — keys are the
#     ARTIFACT_KEYS tuple below (single source of truth; do not list them
#     twice in a comment that drifts every time a new axis lands).
#     Weights live in productivity_gate.artifact_weights (aspirations.yaml);
#     the config block documents the value hierarchy, exclusions, and the
#     bravo session-56 calibration that motivated Fix D (2026-04-22).
#     Adding a new artifact kind requires coordinated edits to THREE places:
#       1. session_artifacts_count.py — counter output
#       2. aspirations.yaml artifact_weights — weight
#       3. ARTIFACT_KEYS tuple in this file — contract
#     Missing any of the three causes a KeyError on config load and a
#     fail-open exit (no silent unweighted fallback, by design).
#   deep_ratio     = min(1, productive_goals / goals_completed)
#     — clamped for symmetry with the other two ratios; productive_goals
#     should be <= goals_completed by definition.
#   score = (1 - routine_ratio) * 0.4 + encoding_ratio * 0.3 + deep_ratio * 0.3
#
# Trigger: goals_completed >= min_iterations AND score < stop_threshold
#          AND no stop-requested already pending (idempotent).
#
# Cool-down ladder (Fix E, 2026-04-22): when the score first drops below
# threshold, write blocked_sleep_until (reuses Phase -0.5e's sleep-wake
# machinery) and increment productivity_cooldown_streak instead of firing
# stop-requested. Stop only fires after the streak saturates the ladder
# (default: 3 sub-threshold reads across 30m/1h/2h cool-downs). A single
# recovery iteration (score >= threshold) resets the streak to 0.
# Ladder durations live in productivity_gate.cooldown_ladder.ladder_seconds
# in aspirations.yaml. Other parameters live in core/config/aspirations.yaml
# § productivity_gate.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
cd "$PROJECT_ROOT"

AGENT="${MIND_AGENT:-}"
if [[ -z "$AGENT" ]]; then
    # No agent bound — can't read WM, nothing to do.
    exit 0
fi
AGENT_DIR="$(agent_dir "$AGENT")"

# Idempotent: if stop-requested already set, no-op.
if bash "$SCRIPT_DIR/session-signal-exists.sh" stop-requested 2>/dev/null; then
    exit 0
fi

# Delegate the math + state read to Python (simpler than bash arithmetic).
python3 - <<'PYEOF'
import json, os, subprocess, sys, yaml
from pathlib import Path
sys.path.insert(0, 'core/scripts')
import _paths

agent = os.environ.get("MIND_AGENT", "")
if not agent:
    sys.exit(0)
agent_dir = _paths.agent_dir(agent)

# Artifact keys — explicit contract between this gate, session_artifacts_count.py,
# and the config's artifact_weights block. Adding a new artifact kind requires
# updating all three together (by design — the gate should never silently
# revert to unweighted when config drifts).
ARTIFACT_KEYS = (
    "tree_writes", "guards", "rb", "patsigs",
    "hyp_created", "hyp_resolved", "experience", "forges",
    "structural_progress",  # Lane D Magic Wand #1 (2026-05-08): credit tree-maintain ops + work_class=framework goals
)

# Slot name hardcoded — must match the session_signals initializer in
# .claude/skills/aspirations/SKILL.md Phase -0.5. Not a tunable: making it
# configurable would let config and SKILL.md drift silently.
COOLDOWN_SLOT = "productivity_cooldown_streak"

# Load parameters. Missing required config keys raise KeyError; caught below
# and treated as fail-open (exit without stopping). Single source of truth:
# all thresholds and weights live in core/config/aspirations.yaml.
try:
    with open(_paths.CONFIG_DIR / "aspirations.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    pg = cfg["productivity_gate"]
    min_iterations = int(pg["min_iterations"])
    stop_threshold = float(pg["stop_threshold"])
    weights_cfg = pg["artifact_weights"]
    weights = {k: float(weights_cfg[k]) for k in ARTIFACT_KEYS}
    # Cool-down ladder (Fix E). Sub-threshold iterations first trigger a
    # sleep via blocked_sleep_until; stop-requested only fires after the
    # streak saturates the ladder.
    cd_cfg = pg["cooldown_ladder"]
    cooldown_ladder = list(cd_cfg["ladder_seconds"])
except Exception as e:
    # Config read/schema failure — fail-open (don't stop). Loud stderr so
    # the operator sees config drift; silent exit would mask the problem.
    print(f"[productivity-gate] config invalid or missing keys ({e}) — fail-open", file=sys.stderr)
    sys.exit(0)

# Read loop_state directly from working-memory.yaml — avoids wm-read.sh
# subprocess (bash+python startup on Windows was hitting the 10s timeout;
# see , fixed iter 54). WM file is <10KB and YAML-parseable inline.
wm_file = agent_dir / "session" / "working-memory.yaml"
try:
    if not wm_file.exists():
        sys.exit(0)  # no WM yet
    with open(wm_file, encoding="utf-8") as f:
        wm = yaml.safe_load(f) or {}
    # Slots live under wm['slots']; loop_state is stored as a dict directly.
    slots = wm.get("slots") or {}
    loop_state = slots.get("loop_state")
    if loop_state is None or loop_state == "null":
        sys.exit(0)  # no loop_state yet — first iteration
    if isinstance(loop_state, str):
        # Schema-shape race: loop_state is supposed to be a YAML mapping, but
        # under concurrent writers (or if a writer truncated the slot) we may
        # observe it as a string — empty, "null", or a JSON-encoded form.
        # All three should fail-open cleanly. The bare json.loads here was
        # the source of the iter-107/108 noise documented in .
        s = loop_state.strip()
        if not s or s == "null":
            sys.exit(0)  # empty/null string — race-safe early exit
        try:
            loop_state = json.loads(s)
        except json.JSONDecodeError:
            # Instrumentation per : log size + first 32 chars so the
            # next occurrence has enough evidence to identify the writer.
            try:
                wm_size = wm_file.stat().st_size
            except Exception:
                wm_size = -1
            print(
                f"[productivity-gate] loop_state malformed string "
                f"(len={len(s)}, prefix={s[:32]!r}, wm_file_size={wm_size}) — "
                f"fail-open this iter; cooldown evaluation skipped",
                file=sys.stderr,
            )
            sys.exit(0)
except Exception as e:
    # Instrumentation: include file size + first 32 bytes so the next
    # iter-107-style failure has evidence pointing at the offending writer.
    try:
        wm_size = wm_file.stat().st_size if wm_file.exists() else -1
        with open(wm_file, "rb") as _diag:
            _prefix = _diag.read(32)
    except Exception:
        wm_size = -1
        _prefix = b""
    print(
        f"[productivity-gate] wm read failed: {e} "
        f"(wm_file_size={wm_size}, prefix={_prefix!r})",
        file=sys.stderr,
    )
    sys.exit(0)

def _to_int_count(v, default=0):
    """Handle schema variance: `goals_completed` ships as either an int counter
    or a list of completed goal IDs (different sessions have used both shapes).
    Accept both; fail-open to `default` on anything else. Prior behavior
    (plain `int(v)`) crashed with TypeError every iteration when v was a list,
    silently skipping the productivity-gate evaluation for the entire session
    (g-115-171, rb-428 bash-consolidation-drift family). Fix: normalize here."""
    if isinstance(v, list):
        return len(v)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v)
        except (ValueError, TypeError):
            return default
    return default

goals_completed = _to_int_count(loop_state.get("goals_completed"), 0)
productive_goals = _to_int_count(loop_state.get("productive_goals"), 0)
signals = loop_state.get("signals", {}) or {}
routine_count_total = _to_int_count(signals.get("routine_count_total"), 0)

# Min-iterations gate
if goals_completed < min_iterations:
    sys.exit(0)

# encoding_ratio: session-total artifacts / goals_completed. Invokes
# session_artifacts_count.py directly (NOT via bash wrapper) — same
# Windows bash-avoidance pattern used for session.py / board.py below
# (see ). Helper exits 2 when session_start is unset; we treat
# that as 0 artifacts (starvation direction) — correct for a stop gate.
# The unset case should not happen in practice because /start seeds
# session_start on every session entry; if it does, the stderr from the
# helper surfaces the real issue.
artifact_py = _paths.CORE_ROOT / "scripts" / "session_artifacts_count.py"
ar = subprocess.run(
    [sys.executable, str(artifact_py)],
    capture_output=True, text=True, timeout=15,
)
if ar.stderr:
    sys.stderr.write(ar.stderr)
counts = {k: 0 for k in ARTIFACT_KEYS}
total_artifacts = 0  # unweighted sum, kept for logging
# 9: a counter FAILURE is not a measurement of ZERO. Previously, a
# nonzero exit (e.g. ONE tree node with malformed YAML front matter — the whole
# shared tree is one bad node away from this) left `counts` all-zero and the gate
# went on to print `encoding_ratio=0.00` as if it had measured something. That is
# a FALSE MEASUREMENT THAT READS AS REAL: it tells an agent it encoded nothing
# when it encoded plenty, and because encoding_ratio carries weight 0.3, it can
# drag a healthy score below stop_threshold and FALSELY STOP THE AGENT. Track the
# degradation explicitly and refuse to stop on it (see the stop branch below).
encoding_degraded = ""
if ar.returncode == 0 and ar.stdout.strip():
    payload = json.loads(ar.stdout.strip())
    for k in ARTIFACT_KEYS:
        counts[k] = int(payload.get(k, 0))
    total_artifacts = int(payload.get("total", sum(counts.values())))
    _bad = payload.get("fm_parse_errors") or []
    if _bad:
        # Counts are a LOWER BOUND — some tree nodes could not be read.
        encoding_degraded = (f"{len(_bad)} tree node(s) have unparseable front "
                             f"matter (counts are a LOWER BOUND): "
                             f"{', '.join(str(b) for b in _bad[:3])}")
else:
    encoding_degraded = (f"session_artifacts_count.py FAILED (rc={ar.returncode}) "
                         f"— the encoding term is UNKNOWN, not zero")

# Weighted sum — tree_writes dominates, hyp_* / experience half-weighted.
# See header comment for the value hierarchy and bravo session-56 calibration.
weighted_artifacts = sum(counts[k] * weights[k] for k in ARTIFACT_KEYS)

# Compute composite score — goals_completed >= min_iterations per the
# gate above, so the denominators here are always positive.
routine_ratio = min(1.0, routine_count_total / goals_completed)
encoding_ratio = min(1.0, weighted_artifacts / goals_completed)
deep_ratio = min(1.0, productive_goals / goals_completed)
score = (1 - routine_ratio) * 0.4 + encoding_ratio * 0.3 + deep_ratio * 0.3

# Per-key breakdown in the log line makes stop-gate decisions auditable
# without re-running session_artifacts_count.py. Short keys keep the line
# under terminal width. KEEP IN SYNC WITH ARTIFACT_KEYS — every key in
# ARTIFACT_KEYS must appear here or its contribution disappears from the
# breakdown log even though it is still weighted into the score.
# Mapping: tw=tree_writes gd=guards rb=rb pt=patsigs hc=hyp_created
#          hr=hyp_resolved ex=experience fg=forges sp=structural_progress
breakdown = (f"tw={counts['tree_writes']} gd={counts['guards']} "
             f"rb={counts['rb']} pt={counts['patsigs']} "
             f"hc={counts['hyp_created']} hr={counts['hyp_resolved']} "
             f"ex={counts['experience']} fg={counts['forges']} "
             f"sp={counts['structural_progress']}")
print(f"[productivity-gate] score={score:.2f} routine_ratio={routine_ratio:.2f} "
      f"encoding_ratio={encoding_ratio:.2f} "
      f"(weighted={weighted_artifacts:.2f}, raw_total={total_artifacts}, {breakdown}) "
      f"deep_ratio={deep_ratio:.2f} goals={goals_completed} threshold={stop_threshold}")
if encoding_degraded:
    # Loud + ATTRIBUTED. The prior failure mode printed a bare `encoding_ratio=0.00`
    # next to an unattributed YAML traceback, which reads as "you encoded nothing"
    # rather than "I could not count". Never let a broken counter masquerade as a
    # measurement (9 / guard-1090).
    print(f"[productivity-gate] *** ENCODING SIGNAL DEGRADED — encoding_ratio above is "
          f"NOT a measurement *** {encoding_degraded}", file=sys.stderr)

# --- Routine-streak surface ( / US-08) -------------------------------
# Make the auto-deep flip counter visible at iteration close so the agent
# catches the silent flip BEFORE it fires. The flip itself lives in
# aspirations-loop-digest.md Phase 4.1 Block C (routine_streak_global >=
# routine_streak_global_ceiling -> the outcome is reclassified deep). This is
# the close-side twin of aspirations-precheck Phase 0-pre.0b's boredom surface,
# which shows the same counter at iteration START. Reuses the loop_state /
# signals / cfg already loaded above (no extra read). The ceiling is read with
# a .get() default OUTSIDE the critical config block (lines 103-120) so a
# missing `recurring` key never fail-opens the stop gate itself — at worst the
# surface falls back to the documented default of 5.
routine_streak_global = _to_int_count(signals.get("routine_streak_global"), 0)
rs_ceiling = int((cfg.get("recurring") or {}).get("routine_streak_global_ceiling", 5))
routine_streaks = loop_state.get("routine_streaks", {}) or {}
_rs_top = sorted(
    ((g, int(n)) for g, n in routine_streaks.items()
     if isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0),
    key=lambda kv: kv[1], reverse=True,
)[:3]
_rs_per_goal = ", ".join(f"{g}={n}" for g, n in _rs_top) or "none"
# Near-threshold warning fires within 2 of the ceiling (matches the ceiling-5
# default to a 1-iteration heads-up before the flip; relative so it tracks any
# retuned ceiling). Plain ASCII -- no em-dash on the Windows stdout path.
if rs_ceiling > 0 and routine_streak_global >= rs_ceiling - 2:
    print(f"[routine-streak] WARN global={routine_streak_global}/{rs_ceiling} -- "
          f"auto-deep flip at {rs_ceiling}: pick a deep goal next or let the flip "
          f"carry you | per-goal: {_rs_per_goal}")
else:
    print(f"[routine-streak] global={routine_streak_global}/{rs_ceiling} "
          f"(auto-deep at {rs_ceiling}) | per-goal: {_rs_per_goal}")

# --- G4: Productivity-score persistence (per world/conventions/self-program-evolution.md) ---
# Append one line per iteration's productivity evaluation to
# world/productivity-snapshots.jsonl. Signal #15 of the Self/Program evolution
# metric vector (§7.1) reads this file — "productivity composite over rolling
# window" tracks whether the agent's score is trending up or down after a
# Self/Program edit (downtrend within tolerance = monitor regression).
#
# Fail-silent observability. The decision field captures gate intent based on
# the current observation; the actual outcome (persisted wm state, stop-requested
# signal) may differ if downstream writes fail, but those failures are logged
# separately to stderr. This file's role is "what did the math compute and
# what was the gate about to do?"
try:
    from datetime import datetime as _dt_g4
    _g4_pending_streak = int(signals.get(COOLDOWN_SLOT, 0))
    _g4_cd_len = len(cooldown_ladder)
    if score >= stop_threshold:
        _g4_decision = "above_threshold"
    elif _g4_pending_streak < _g4_cd_len:
        _g4_decision = f"cooldown_pending_level_{_g4_pending_streak + 1}_of_{_g4_cd_len}"
    else:
        _g4_decision = "stop_will_fire"
    snapshot = {
        "ts": _dt_g4.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "agent": agent,
        "score": round(score, 4),
        "threshold": stop_threshold,
        "decision": _g4_decision,
        "breakdown": {
            "routine_ratio": round(routine_ratio, 4),
            "encoding_ratio": round(encoding_ratio, 4),
            "deep_ratio": round(deep_ratio, 4),
            "weighted_artifacts": round(weighted_artifacts, 4),
            "raw_total_artifacts": total_artifacts,
            "counts": {k: int(counts[k]) for k in ARTIFACT_KEYS},
        },
        "goals_completed": goals_completed,
        "productive_goals": productive_goals,
        "routine_count_total": routine_count_total,
        "current_streak": _g4_pending_streak,
        "cooldown_ladder_len": _g4_cd_len,
    }
    snap_path = _paths.WORLD_DIR / "productivity-snapshots.jsonl"
    # Same best-effort single-line append pattern as _record_fallback_hit
    # in _fileops.py — single-line JSON under PIPE_BUF is single-write atomic
    # on most filesystems. Torn-line risk is observability-grade.
    with open(snap_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=True) + "\n")
except Exception as _g4_e:
    # Best-effort: never crash the gate on telemetry-write failure.
    print(f"[productivity-gate] G4 snapshot write failed: {_g4_e}", file=sys.stderr)

# --- Cool-down ladder (Fix E, 2026-04-22) -----------------------------------
# Read current streak from loop_state.signals (same container as B7's
# consecutive_blocked_sleeps). Missing = 0, which is the correct starting
# point for a session that never hit sub-threshold before.
current_streak = int(signals.get(COOLDOWN_SLOT, 0))
wm_py = _paths.CORE_ROOT / "scripts" / "wm.py"

def _persist_loop_state(new_streak):
    """Merge new_streak into loop_state.signals and persist via wm.py.

    Returns True on success. False on WM write failure — callers MUST
    abort cooldown logic on False (silently proceeding would desynchronize
    the on-disk streak from what the gate thinks it wrote, and the ladder
    never escalates).

    DO NOT replace this with a direct YAML write to working-memory.yaml —
    bypassing wm.py skips slot_meta.updated_at and accessed_at bookkeeping,
    which WM pruning relies on.
    """
    merged = dict(loop_state)
    sig = dict(signals)
    sig[COOLDOWN_SLOT] = new_streak
    merged["signals"] = sig
    r = subprocess.run(
        [sys.executable, str(wm_py), "set", "loop_state"],
        input=json.dumps(merged), text=True,
        capture_output=True, timeout=10,
    )
    if r.returncode != 0:
        print(f"[productivity-gate] loop_state write failed (rc={r.returncode}): "
              f"{r.stderr.strip()}", file=sys.stderr)
        return False
    return True

def _set_wm_slot(slot, value):
    """Set a single WM slot. Returns True on success."""
    r = subprocess.run(
        [sys.executable, str(wm_py), "set", slot],
        input=json.dumps(value), text=True,
        capture_output=True, timeout=10,
    )
    if r.returncode != 0:
        print(f"[productivity-gate] {slot} write failed (rc={r.returncode}): "
              f"{r.stderr.strip()}", file=sys.stderr)
        return False
    return True


def _write_blocked_sleep_until(seconds):
    """Write blocked_sleep_until = now + seconds AND mark cooldown_active=true.

    Returns ISO string or None on failure.

    Sentinel reused from B7 backoff — safe because B7 runs at selector-empty
    (no goal executed) and this gate runs at iteration-close (after a goal
    completes). The two paths are mutually exclusive per iteration, so the
    shared slot is never concurrently written.

    g-251-08: also sets `cooldown_active` sibling WM slot to true so the
    light-precheck handler in blocked-sleep-recovery-digest.md can
    distinguish a productivity-cooldown wake from a real B7 backoff wake.
    Without this flag, a wake during cooldown that finds any unresolved
    known_blocker fires a "still blocked" proactive_escalation notification
    at the user — even though the actual reason for the sleep was the
    productivity gate, not a blocker. The digest skips the notification
    block when cooldown_active is true. Cleared on all blocked_sleep_until
    cleanup paths in the digest.

    Write order: blocked_sleep_until FIRST, then cooldown_active. If the
    timestamp write fails we bail without setting the flag (clean state).
    If the flag write fails AFTER the timestamp succeeds, the wake will
    fall through to the legacy notification path — accurate-but-noisy
    rather than silently swallowed. Same fail-loud principle as the
    streak/timestamp ordering above.
    """
    from datetime import datetime, timedelta
    wake_at = (datetime.now() + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    if not _set_wm_slot("blocked_sleep_until", wake_at):
        return None
    if not _set_wm_slot("cooldown_active", True):
        # Timestamp landed but flag did not. Don't fail the cooldown — the
        # legacy notify path is accurate, just noisy. Log loudly so drift is
        # visible.
        print("[productivity-gate] WARN: cooldown_active flag write failed — "
              "wake may emit spurious 'still blocked' notification (g-251-08).",
              file=sys.stderr)
    return wake_at

if score >= stop_threshold:
    # Session is productive. Reset streak so prior cool-down pressure
    # doesn't accumulate across recovery. A single recovering iteration
    # fully clears the ladder — the ladder IS the hysteresis.
    if current_streak > 0:
        if _persist_loop_state(0):
            print(f"[productivity-gate] streak reset 0 (was {current_streak}) — score recovered")
    sys.exit(0)

# 9: below threshold — but is the score TRUSTWORTHY? encoding_ratio carries
# weight 0.3, so a failed/degraded counter can subtract up to 0.30 from the score and
# push a genuinely-productive agent under stop_threshold. Stopping an agent because a
# YAML parse error in ONE shared tree node blinded the counter is a catastrophic
# false positive. Fail OPEN: never stop on an untrustworthy signal. The DEGRADED
# banner above already names the cause loudly so it gets fixed rather than tolerated.
if encoding_degraded:
    print(f"[productivity-gate] STOP SUPPRESSED — score={score:.2f} is below "
          f"threshold={stop_threshold} but the encoding term is DEGRADED, so the "
          f"score is not trustworthy. Fix the cause, do not tolerate it: "
          f"{encoding_degraded}", file=sys.stderr)
    sys.exit(0)

# Below threshold. If the streak has not yet saturated the ladder, sleep
# instead of stopping.
if current_streak < len(cooldown_ladder):
    sleep_seconds = int(cooldown_ladder[current_streak])
    # Write order: streak FIRST, then blocked_sleep_until. If the streak
    # write fails we bail without scheduling sleep — next iteration re-reads
    # score and tries the same level. If the sleep write fails AFTER streak
    # succeeds, next iteration runs immediately at level+1 (slightly more
    # conservative). Either failure mode is recoverable; the reverse order
    # can produce a stuck level-0 loop (sleep scheduled, streak not
    # advanced, wake fires gate again at level 0).
    if not _persist_loop_state(current_streak + 1):
        sys.exit(0)
    wake_at = _write_blocked_sleep_until(sleep_seconds)
    if not wake_at:
        sys.exit(0)
    print(f"[productivity-gate] COOLDOWN level {current_streak + 1}/{len(cooldown_ladder)} "
          f"— sleeping {sleep_seconds}s (until {wake_at}). "
          f"Stop fires on next sub-threshold read once ladder saturates.")
    from datetime import datetime
    now_ts = datetime.now()
    jpath = agent_dir / "journal" / now_ts.strftime("%Y") / now_ts.strftime("%m") / f"{now_ts:%Y-%m-%d}.md"
    jpath.parent.mkdir(parents=True, exist_ok=True)
    with open(jpath, "a", encoding="utf-8") as f:
        f.write(f"\n## {now_ts:%H:%M} — Productivity cool-down level {current_streak + 1}\n"
                f"Score {score:.2f} < {stop_threshold}. Sleeping {sleep_seconds}s (until {wake_at}).\n"
                f"Breakdown: {breakdown}. Stop fires if {len(cooldown_ladder)} consecutive sub-threshold reads.\n")
    sys.exit(0)

# Ladder saturated. Reset streak and fall through to the existing stop
# path. Reset happens BEFORE stop so a future re-armed session starts
# fresh rather than at ladder-max. Reset failure is non-fatal here — we're
# stopping anyway; next session initializes fresh via aspirations/SKILL.md
# Phase -0.5.
if current_streak > 0:
    _persist_loop_state(0)
    print(f"[productivity-gate] ladder saturated at level {current_streak} — proceeding to hard stop")

# ORDER CRITICAL — /stop Phase -1.4 reads stop-target-mode with no fallback;
# the file MUST exist before stop-requested is set. Do NOT reorder these
# two writes. Same invariant as the /stop skill (.claude/skills/stop/SKILL.md
# step 1 "Write target mode ALWAYS").
mode_file = agent_dir / "session" / "stop-target-mode"
mode_file.parent.mkdir(parents=True, exist_ok=True)
mode_file.write_text("assistant", encoding="utf-8")

# Windows-bash path handling (): the original call was
# `subprocess.run(["bash", str(_paths.CORE_ROOT / ...))])` which failed on
# Windows because subprocess.run(["bash", ...]) invokes
# C:\Windows\System32\bash.exe (WSL bash), whose filesystem view is
# /mnt/c/... — neither the backslash form nor the forward-slash C:/ form
# resolves. Bypass the shell entirely: session.py is Python; call it
# through sys.executable (the exact interpreter currently running) so
# there's no bash-dialect translation layer.
session_py = (_paths.CORE_ROOT / "scripts" / "session.py")
result = subprocess.run(
    [sys.executable, str(session_py), "signal", "set", "stop-requested"],
    capture_output=True, text=True,
)

# If session-signal-set failed, clean up the dangling stop-target-mode file
# rather than leaving the /stop Phase -1.4 invariant broken. The file should
# only exist when a stop is actually in progress.
if result.returncode != 0:
    print(
        f"[productivity-gate] session-signal-set.sh failed "
        f"(rc={result.returncode}): {result.stderr.strip()}. "
        f"Reverting stop-target-mode to keep invariant intact.",
        file=sys.stderr,
    )
    try:
        mode_file.unlink()
    except OSError:
        pass
    sys.exit(0)

msg = f"Self-stop: productivity score {score:.2f} < {stop_threshold} after {goals_completed} iterations"
print(f"[productivity-gate] TRIGGERED — {msg}")

# Board post — type=status (documented vocabulary in board.py), tag=self-stop
# for filtering. board.py documents: claim, complete, blocked, encoding,
# finding, status — `self-stop` is not a documented type, so use status + tag.
try:
    # Same bash-avoidance as above — board.py is Python, call via sys.executable.
    board_py = (_paths.CORE_ROOT / "scripts" / "board.py")
    subprocess.run(
        [sys.executable, str(board_py), "post",
         "--channel", "coordination", "--type", "status", "--tags", "self-stop"],
        input=msg, text=True, capture_output=True, timeout=15,
    )
except Exception:
    pass

# Journal append
# User-visible notification path: board post (self-stop type, above) is read
# by the next LLM turn and by any reader-mode observer session; the journal
# entry is the permanent audit trail. No separate notify-user call — that
# skill is LLM-invoked, not shell-callable, and adding a fallback here would
# create a silent failure mode that hides the real notification channel.
from datetime import datetime
now = datetime.now()
jpath = agent_dir / "journal" / now.strftime("%Y") / now.strftime("%m") / f"{now:%Y-%m-%d}.md"
jpath.parent.mkdir(parents=True, exist_ok=True)
with open(jpath, "a", encoding="utf-8") as f:
    f.write(f"\n## {now:%H:%M} — Productivity self-stop\n{msg}\n")
PYEOF
