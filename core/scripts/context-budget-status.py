#!/usr/bin/env python3
"""Status line script — captures context window metrics and writes budget file.

Reads the status line JSON payload from stdin, extracts context_window fields,
writes to <agent>/session/context-budget.json, and prints a one-line status to
stdout.

Zones are anchored on *distance to autocompact*, not raw usage. Autocompact
fires at (CLAUDE_CODE_AUTO_COMPACT_WINDOW x CLAUDE_AUTOCOMPACT_PCT_OVERRIDE).
Raw usage % of the model window is not the right signal: if the user sets an
effective window of 500K on a 1M model, autocompact triggers at 40% raw — and
anything gated on "raw >= 85" never fires proactively.

Design-intent note (pq-034, 2026-04-19): effective windows narrower than the
raw model window are INTENTIONAL on some configurations — they lower per-call
cost AND keep signal strength high by compacting stale context earlier.
Future maintainers should NOT "correct" a user's small effective_window by
raising it to match the model capacity. The script adapts to whatever the
user sets; the only invariant is that zones anchor on pct_to_autocompact so
the brakes actually fire before autocompact.

Zone thresholds (applied to pct_to_autocompact):
  fresh:  pct_to_autocompact < 50   (more than half the autocompact budget left)
  normal: 50 <= pct_to_autocompact < 85
  tight:  pct_to_autocompact >= 85  (within 15% of autocompact — real brake)

If the two env vars are unset, the script falls back to (window_size, 90) —
Claude Code's stock behavior — so this is safe on agents without overrides.
"""

import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

# Self-destruct after 10s -- prevents zombie py.exe orphans when the parent
# session dies without closing stdin (Windows doesn't propagate EOF to
# orphaned children). daemon=True so normal exit isn't blocked on the timer.
# Armed HERE (before the _stdio/_paths imports + reconfigure_stdio below) so
# the guard covers the ENTIRE module-import window:  found a 57h
# orphan (pid-19476) that hung before the timer armed when this block sat
# AFTER reconfigure_stdio().
_timer = threading.Timer(10, lambda: os._exit(0))
_timer.daemon = True
_timer.start()

#  / : force utf-8 on stdin/stdout/stderr (covers Windows
# cp1252 fallback when callers bypass the _platform.sh PYTHONIOENCODING=utf-8
# shim). Closes acceptance (4) of  -- stdin-ingest sweep.
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import AGENT_DIR, PROJECT_ROOT, agent_dir as _agent_dir, agents_root

# Status-line subprocesses don't inherit MIND_AGENT, so _paths.AGENT_DIR is
# typically None here. Resolution via session_id (from the status payload) maps
# to the same `.active-agent-<session_id>` binding file that /start writes —
# giving the writer a deterministic target even without the env var. Without
# this, context-budget.json would never be written and goal-selector's
# zone-aware batching silently degrades (see  + rb-215).

BUDGET_PATH = AGENT_DIR / "session" / "context-budget.json" if AGENT_DIR else None


def _resolve_budget_path_from_session(session_id):
    """Fallback agent resolution for status-line subprocesses without MIND_AGENT.

    Tries Phase 2.6 binding (agents/<name>/sessions/<SID>/binding.yaml) first,
    then falls back to legacy PROJECT_ROOT/.active-agent-<session_id>. Returns
    the corresponding <agent>/session/context-budget.json path, or None if no
    binding can be resolved.
    """
    if not session_id:
        return None
    if any(c in session_id for c in ("/", "\\", "\n", "\r", " ")) or ".." in session_id:
        return None
    name = ""
    # Phase 2.6 (preferred): agents/<name>/sessions/<SID>/binding.yaml.
    # Without this, status-line subprocesses (which don't inherit MIND_AGENT)
    # cannot resolve the agent → context-budget.json is never written →
    # zone-aware batching in goal-selector silently degrades.
    # : use agents_root() instead of hardcoded "agents" literal so
    # Phase 2.6+ AGENTS_PARENT_DIR relocations don't silently regress this
    # status-line resolver (pre-Phase-2.5.D shape would re-emerge otherwise).
    agents_parent = agents_root()
    if agents_parent.is_dir():
        try:
            for child in agents_parent.iterdir():
                if not child.is_dir():
                    continue
                binding_p26 = child / "sessions" / session_id / "binding.yaml"
                if binding_p26.is_file():
                    name = child.name
                    break
        except OSError:
            pass
    # Legacy fallback: pre-Phase-2.6 .active-agent-<SID>.
    if not name:
        binding = PROJECT_ROOT / f".active-agent-{session_id}"
        if not binding.is_file():
            return None
        try:
            name = binding.read_text(encoding="utf-8").strip()
        except Exception:
            return None
    if not name:
        return None
    candidate = _agent_dir(name) / "session" / "context-budget.json"
    if not candidate.parent.is_dir():
        return None
    return candidate


def classify_zone(pct_to_autocompact):
    # DO NOT revert to used_pct. Zones are anchored on distance-to-autocompact so
    # they stay correct when the user sets CLAUDE_AUTOCOMPACT_PCT_OVERRIDE /
    # CLAUDE_CODE_AUTO_COMPACT_WINDOW below stock values. Using raw used_pct
    # makes the "tight" zone unreachable whenever autocompact fires below 85%
    # of the raw window — the whole bug this fixed.
    if pct_to_autocompact < 50:
        return "fresh"
    if pct_to_autocompact < 85:
        return "normal"
    return "tight"


def _int_env(name, default=None):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return

    cw = payload.get("context_window")
    if not cw:
        return

    used_pct = cw.get("used_percentage", 0)
    remaining_pct = cw.get("remaining_percentage", 100)
    window_size = cw.get("context_window_size", 200000)
    usage = cw.get("current_usage") or {}
    input_tokens = usage.get("input_tokens", 0)

    # DERIVE input_tokens FROM used_percentage WHEN THE COUNTER IS DEAD
    # ( class, measured 2026-09-03 on foxtrot / LAPTOP-3IOFCNEO).
    #
    # Everything below — headroom, pct_to_autocompact, zone — is computed from
    # input_tokens ALONE. used_pct is recorded but never consumed. On a live
    # session the statusLine payload delivered `used_percentage: 36` and
    # `context_window_size: 1000000` beside `current_usage.input_tokens: 2`, so
    # the record contained two mutually-falsifying halves: 36% of a 1M window is
    # ~360k tokens in use, and 2 is not that. The zone read `fresh` with
    # `headroom_tokens: 479998` and `pct_to_autocompact: 0.0` right up to hard
    # context exhaustion, because every one of those three came off the dead
    # half. Consequence: EVERY soft-degradation path keyed on the zone
    # (aspirations Phase 8.8 evolution skip, aspirations-select batch sizing,
    # aspirations-execute episode-chain capping, the abbreviation policy's
    # zone==tight precondition) saw `fresh` and throttled nothing. The session
    # ran to zero tokens and then livelocked against the stop hook, which
    # correctly refuses a text-only turn-end but cannot be satisfied by a
    # session with no budget left to act.
    #
    # This does NOT revert the zone to used_pct — see classify_zone's comment,
    # which is still correct and still binding: anchoring zones on raw
    # used_pct makes `tight` unreachable whenever autocompact fires below 85%
    # of the raw window. The repair is to the INPUT, not the anchor.
    #
    # guard-2018 ("an absent counter field can BE the zero") is the reason this
    # takes max() rather than replacing outright: a genuine early-session zero
    # must stay zero. It survives because used_pct is then also ~0, so the
    # derived figure is ~0 too. Overstating usage is the fail-safe direction —
    # it degrades earlier than needed; understating is the failure above.
    if used_pct and window_size:
        derived_input_tokens = int(used_pct / 100 * window_size)
        input_tokens = max(input_tokens, derived_input_tokens)

    # Autocompact anchors. CLAUDE_CODE_AUTO_COMPACT_WINDOW sets the effective
    # window Claude Code measures against; CLAUDE_AUTOCOMPACT_PCT_OVERRIDE sets
    # the trigger %. Defaults match stock Claude Code behavior when unset.
    effective_window = _int_env("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or window_size
    autocompact_pct = _int_env("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", 90)
    autocompact_token_limit = int(effective_window * autocompact_pct / 100)
    # No guard against autocompact_token_limit <= 0. If the user sets
    # CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=0 the division below raises and the
    # outer try/except in __main__ swallows it — status line stays blank,
    # budget JSON stays stale. That's a visible signal the env is wrong,
    # which is better than fabricating a bogus limit that silently lies.
    headroom_tokens = max(0, autocompact_token_limit - input_tokens)
    pct_to_autocompact = min(100.0, input_tokens / autocompact_token_limit * 100)

    zone = classify_zone(pct_to_autocompact)

    # env_seen is the receipt — the RAW strings the statusLine subprocess saw
    # at write time. Distinguishes "env var was set to X" from "env var was
    # unset and default fired". If a value here is null on an agent that's
    # supposed to have overrides, env propagation is broken.
    env_seen = {
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"),
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"),
    }

    budget = {
        "used_pct": round(used_pct, 1),
        "remaining_pct": round(remaining_pct, 1),
        "window_size": window_size,
        "input_tokens": input_tokens,
        "effective_window": effective_window,
        "autocompact_pct": autocompact_pct,
        "autocompact_token_limit": autocompact_token_limit,
        "headroom_tokens": headroom_tokens,
        "pct_to_autocompact": round(pct_to_autocompact, 1),
        "zone": zone,
        "env_seen": env_seen,
        "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    # Only write if <agent>/session/ already exists — do NOT mkdir here.
    # The status line fires every prompt, even when session dir doesn't exist.
    # Using mkdir would silently recreate the session dir when it shouldn't exist.
    # Resolution order:
    #   1. BUDGET_PATH (set at import if MIND_AGENT was present)
    #   2. Session-ID-based fallback via the .active-agent-<id> binding file
    #      (the usual path, since status-line subprocesses don't inherit env vars)
    target = BUDGET_PATH
    if target is None or not target.parent.is_dir():
        target = _resolve_budget_path_from_session(payload.get("session_id"))
    if target and target.parent.is_dir():
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(budget), encoding="utf-8")
        os.replace(str(tmp), str(target))

    # Status line output — two numbers make the distinction visible:
    # left = raw % of the model window, right = % of autocompact trigger.
    print(f"CTX: {used_pct:.0f}%/{pct_to_autocompact:.0f}% [{zone}]")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # Status line must not crash
