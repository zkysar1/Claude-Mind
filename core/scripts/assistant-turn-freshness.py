#!/usr/bin/env python3
# domain-leak-exempt: framework recovery infra; row-type names are Claude Code transcript literals, not domain terms
"""Assistant-turn liveness probe for recovery-gate Path D (g-115-6253).

Reports whether the bound agent's runner session emitted an assistant turn
recently, read from the Claude Code session transcript at
``~/.claude/projects/<dashified-project-root>/<sid>.jsonl``.

WHY THIS SIGNAL AND NOT ANOTHER (evidence: board msg-20260814-154537-alpha-5088,
investigation g-115-6242 -- do not re-derive). Path D has false-fired 5 times
across 4 agents, and three successive narrowings (g-328-25 threshold
calibration, g-328-45 runner-age gate, g-115-5227 liveness veto) were EACH
followed by another firing. NO DIARY-DERIVED FIX CAN REACH THIS: a live user
conversation emits ZERO execution-diary writes BY CONSTRUCTION, because the
diary records loop phases and findings, not conversational turns. Measured on
foxtrot 2026-08-14 from the store of record: last diary write 08:33:58, then
5h35m of total diary silence while the heartbeat stayed fresh. The g-115-5227
liveness veto did not malfunction -- there genuinely was no diary activity. So
"wedged" and "alive-but-off-loop" are IDENTICAL in diary+heartbeat space, and
every future diary-based refinement misses the same way. The transcript is
independent of BOTH the diary and the heartbeat, which is the whole point.

WHY NOT A STALE-HEARTBEAT DISCRIMINATOR: Paths A and C already require one.
Path D exists precisely because that requirement let the 2026-07-04 fleet-wedge
through. Adding it here would be a DELETION of Path D, not a weakening.

DIRECTION CHECK (guard-3802). That guardrail warns that a suppression predicate
evaluated over a window whose LENGTH IS the severity metric silences the worst
cases most reliably -- the canonical case being ``_has_session_gap``, which asks
"was there ever a >= T gap inside the window" where the window is the overrun
itself, so P(suppress) rises monotonically with severity. This predicate is NOT
that shape: it is point-freshness against a FIXED threshold ("is the newest
assistant turn younger than N minutes"), so as a wedge grows longer the newest
assistant turn only gets OLDER and P(suppress) DECREASES monotonically with
severity. The alarm gets louder exactly where it matters more. Per that
guardrail's remedy clause the verdict also CARRIES the age it measured, so a
suppression is never a bare silence -- the severity it suppressed is
re-derivable downstream from the JSON.

ABSENT IS NOT EVIDENCE OF LIVENESS -- the load-bearing distinction, and the one
that keeps Path D alive rather than deleting it. Measured on cc-02 2026-08-15:
of five fleet agents only the box-RESIDENT one has a ``running-session-id`` and
a transcript here; the other four have neither. So "no transcript" must mean
"this probe has nothing to say", never "a turn happened". Only a transcript that
EXISTS and cannot be READ is an error (rc=2), and only that case inherits
guard-487's fail-closed-as-suppressed. This mirrors the runner-token gate's own
reasoning in recovery-gate.sh: an undeterminable signal must not silently
disable the genuine-wedge path, which is the costlier failure.

TAIL ONLY, never a full parse. Measured on cc-02: a 400 KB tail of a 281 MB
live transcript resolves the newest assistant timestamp in 1.6 ms (cc-08
measured 21 ms on a 60 MB file), which is well inside a SessionStart hook
budget. Parsing the whole file is not.

Exit codes (recovery-gate Path D gates on these):
  0  = a recent assistant turn EXISTS  -> SUPPRESS Path D (no recovery)
  1  = no recent assistant turn, or this probe has nothing to say
       (no running-session-id / no transcript) -> do NOT suppress
  2  = error (transcript present but unreadable) -> caller SUPPRESSES
       (guard-487 fail-closed-as-suppressed; agrees with this script family's
       documented fail-open-to-no-recovery contract, so the two rules do not
       conflict here)
JSON verdict on stdout in all cases.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import AGENT_DIR, PROJECT_ROOT  # noqa: E402
from _dt import parse_naive_iso  # noqa: E402 -- guard-1398 SSOT, never raises

# 60, deliberately EQUAL to runner_heartbeat.stale_minutes and strictly BELOW
# runner_heartbeat.wedge_stale_minutes (65).
#
# INVARIANT: assistant_turn_fresh_minutes < wedge_stale_minutes. WHY: Path D
# only reaches this probe once a phase_start has been unclosed for longer than
# wedge_stale (65). If this window were >= that, an assistant turn from the very
# START of the wedge window -- plausibly the turn that WEDGED -- would suppress
# the recovery of its own wedge. Keeping it strictly below guarantees the
# suppressing turn is strictly MORE RECENT than the oldest part of the wedge
# window, so a suppression always rests on activity that post-dates the wedge
# beginning. Guarded by
# test_assistant_turn_freshness.py::test_config_invariant_fresh_below_wedge_stale.
#
# 60 rather than a fourth independent constant: it reuses the fleet's existing
# "how long may a healthy thing go quiet" value (stale_minutes), and a live
# human conversation that has gone a full hour without an assistant turn is not
# meaningfully distinguishable from an absent one.
# Env override: ASSISTANT_TURN_FRESH_MINUTES.
DEFAULT_FRESH_MINUTES = 60.0

# 400 KB. Sized from measurement, not guessed: a 400 KB tail of the live 281 MB
# transcript on cc-02 held 67 assistant rows -- two orders of magnitude more
# than the one needed. A tail that captured ZERO assistant rows would read as
# "no recent turn" and fail toward RECOVERY, so the margin is deliberate.
TAIL_BYTES = 400_000


def fresh_threshold_minutes():
    """Env override wins; else aspirations.yaml
    runner_heartbeat.assistant_turn_fresh_minutes; else DEFAULT_FRESH_MINUTES.
    Any error falls back to the default (never raises)."""
    env = os.environ.get("ASSISTANT_TURN_FRESH_MINUTES")
    if env:
        try:
            return float(env)
        except (ValueError, TypeError):
            pass
    try:
        import yaml  # local import keeps module load light + optional
        cfg = SCRIPT_DIR.parent / "config" / "aspirations.yaml"
        with open(cfg, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        val = (data.get("runner_heartbeat") or {}).get("assistant_turn_fresh_minutes")
        if val is not None:
            return float(val)
    except Exception:
        pass
    return DEFAULT_FRESH_MINUTES


def default_transcripts_dir(project_root):
    """``~/.claude/projects/<dashified-project-root>``.

    Claude Code dashifies the absolute project path (``/opt/ayoai-mind`` ->
    ``-opt-ayoai-mind``). Same convention aspirations-rejection-audit.py uses;
    kept as a local helper rather than imported because that module's filename
    is hyphenated and importing it by path to reach one 3-line function would
    cost more than it saves.
    """
    dashified = str(project_root).replace("/", "-").replace("\\", "-")
    return Path(os.path.expanduser("~/.claude/projects")) / dashified


def newest_assistant_timestamp(transcript_path, now, tail_bytes=TAIL_BYTES):
    """Newest CREDIBLE assistant-turn timestamp in the tail, or None.

    Scans the tail backwards and returns on the first match, so cost is
    independent of file size.

    Every row-level hazard here is the one phase-wedge-check.py's
    ``last_diary_activity`` documents, re-checked against THIS store rather than
    inherited (the two stores have different writers and different shapes):

    - The FIRST line of the tail is discarded: a byte-offset seek lands
      mid-line, and half a JSON object is not a row.
    - Non-dict rows are skipped -- a bare JSON scalar/array raises
      ``AttributeError`` on ``.get`` otherwise.
    - Rows timestamped AFTER ``now`` are ignored. A future-dated row is not
      evidence anything is alive, and admitting one would permanently suppress
      Path D for this agent.
    - Timestamps are parsed with ``parse_naive_iso`` (guard-1398 SSOT), which is
      load-bearing HERE in a way it is not for the diary: transcript rows are
      tz-AWARE (``2026-08-15T03:49:22.637Z``) while the diary's are naive, so a
      hand-rolled ``fromisoformat`` + naive ``now`` comparison raises
      ``TypeError: can't compare offset-naive and offset-aware`` -- the exact
      regression the diary veto shipped and had to fix. Verified on this box:
      the helper returns UTC-naive for ``Z``, ``+00:00`` and offset forms alike.
    """
    transcript_path = Path(transcript_path)
    size = transcript_path.stat().st_size
    n = min(tail_bytes, size)
    with open(transcript_path, "rb") as f:
        if size > n:
            f.seek(size - n)
        chunk = f.read(n)
    lines = chunk.split(b"\n")
    if size > n:
        lines = lines[1:]          # partial first line -- not a row
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            e = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(e, dict):
            continue
        if e.get("type") != "assistant":
            continue
        ts = parse_naive_iso(e.get("timestamp"))
        if ts is None or ts > now:
            continue               # unparseable, or future-dated -> not credible
        return ts
    return None


def check(agent_dir=None, transcripts_dir=None, now=None, threshold=None):
    """Return (verdict_dict, exit_code). Never raises."""
    agent_dir = Path(agent_dir) if agent_dir else Path(AGENT_DIR)
    now = now or datetime.now()
    threshold = fresh_threshold_minutes() if threshold is None else float(threshold)
    out = {"threshold_minutes": threshold, "suppress": False,
           "newest_assistant_at": None, "age_minutes": None}

    sid_file = agent_dir / "session" / "running-session-id"
    try:
        sid = sid_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        sid = ""
    if not sid:
        # Nothing to say -- NOT an error, and NOT evidence of a turn.
        out["verdict"] = "no_running_session_id"
        return out, 1
    out["session_id"] = sid

    tdir = Path(transcripts_dir) if transcripts_dir else default_transcripts_dir(PROJECT_ROOT)
    transcript = tdir / ("%s.jsonl" % sid)
    if not transcript.is_file():
        # The common cross-box case: this agent's runner is not on this machine.
        # Absence is not liveness -- do NOT suppress (see module docstring).
        out["verdict"] = "no_transcript"
        out["transcript"] = str(transcript)
        return out, 1
    out["transcript"] = str(transcript)

    try:
        ts = newest_assistant_timestamp(transcript, now)
    except Exception as exc:
        # Present but unreadable -> guard-487 fail-closed-as-suppressed.
        out["verdict"] = "unreadable"
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        return out, 2

    if ts is None:
        out["verdict"] = "no_assistant_turn_in_tail"
        return out, 1

    age = (now - ts).total_seconds() / 60.0
    out["newest_assistant_at"] = ts.isoformat()
    out["age_minutes"] = round(age, 2)
    if age <= threshold:
        out["verdict"] = "recent_assistant_turn"
        out["suppress"] = True
        return out, 0
    out["verdict"] = "no_recent_assistant_turn"
    return out, 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent-dir", default=None, help="override bound agent dir (tests)")
    ap.add_argument("--transcripts-dir", default=None, help="override projects dir (tests)")
    ap.add_argument("--now", default=None, help="override now, naive ISO (tests)")
    ap.add_argument("--threshold-minutes", default=None, type=float)
    args = ap.parse_args()
    now = parse_naive_iso(args.now) if args.now else None
    try:
        verdict, rc = check(agent_dir=args.agent_dir,
                            transcripts_dir=args.transcripts_dir,
                            now=now, threshold=args.threshold_minutes)
    except Exception as exc:   # belt-and-braces: this probe must never traceback
        verdict, rc = {"verdict": "unreadable", "suppress": False,
                       "error": "%s: %s" % (type(exc).__name__, exc)}, 2
    print(json.dumps(verdict))
    return rc


if __name__ == "__main__":
    sys.exit(main())
