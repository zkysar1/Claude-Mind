#!/usr/bin/env python3
"""quiescence-gate.py — script-gated "confidently idle" decision.

Decides whether the autonomous loop has earned the right to sleep longer than
the default B7 backoff because the queue is HONESTLY user-gated (every blocked
goal carries a structured blocker_ref with an observable external_id). The LLM
does NOT decide quiescence; this script computes it from structured data.

Prevents the narrative-laundering escape: without this gate, the agent could
write defer_reason="awaiting user feedback" on every deep goal and fabricate
all-blocked state. Change 1 (aspirations.py) forces every narrative defer to
carry a typed blocker_ref. This gate enforces that every blocked goal reaching
it has such a ref, with a future expires_at.

Two modes
---------
--check         Evaluate eligibility BEFORE B7 backoff.
                Exit 0: quiescence approved. stdout JSON carries sleep_seconds,
                        log record, and wake snapshot.
                Exit 1: quiescence denied. stdout JSON carries structured reason.
                        Caller falls back to current B7 behavior.

--verify-wake   POST-sleep audit. Compares current external_ids against the
                snapshot written at quiescence entry. If any blocker changed
                state but was not resolved, emit a quiescence-miss record.
                Exit 0: clean (no miss, or quiescence was not active).
                Exit 2: miss detected. stdout JSON identifies the drifted ref
                        and caller should file an Investigate goal.

Gate conditions (all must hold)
-------------------------------
  C1. zero executable goals (caller asserts this via --all-blocked flag)
  C2. every blocked goal has blocker_ref (from goal-selector collect_blocked)
  C3. every blocker_ref.expires_at is in the future
  C4. same blocker-set hash persisted for >= hysteresis_iters iterations
  C5. cumulative quiescent_sleep_s < budget_pct * (now - session_start_at)
  C6. consecutive wake-misses < wake_miss_cooldown

Exit code summary
-----------------
0 = pass (quiescence approved / clean wake-verify)
1 = denied (gate condition failed; reason in stdout JSON)
2 = wake-miss detected (only from --verify-wake)

State location
--------------
  loop_state.signals.quiescence (dict) in <agent>/session/working-memory.yaml
    session_start_at: ISO timestamp of the session owning the per-session
                      counters below. On every --check, we compare this to
                      loop_state.session_start_at; if it differs, we zero
                      sleep_total_s + misses and record the new start
                      (cross-session rollover). current_hash + streak
                      intentionally survive — stable blocker-set across
                      sessions IS signal.
    current_hash:    sha256 of sorted external_ids at last --check
    streak:          consecutive iterations with same hash
    iters:           total quiescence entries this session
    sleep_total_s:   cumulative wall-clock slept in quiescence this session
    misses:          consecutive wake-miss count
    active_snapshot: {entered_at, sleep_seconds, refs: [...]}
                     Populated on entry, cleared on wake-verify.

Decision trail
--------------
  <agent>/session/quiescence-log.jsonl  — one record per --check decision
  <agent>/session/quiescence-audit.jsonl — one record per wake-miss
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from _paths import AGENT_DIR, CONFIG_DIR, CORE_ROOT  # noqa: E402
from _dt import parse_naive_iso  # noqa: E402  (shared tzinfo-stripping naive-ISO parse — guard-4372)
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)
import _reflectable  # : reflectable-vs-backlog split for --unreflected


# Bare goal-id-shaped tag (e.g. ""): a finding carrying a bare goal
# id IS goal-linked, even without the "goal_id:" prefix. The drainable detector
# (_count_actionable_findings_without_goal) must treat it as has_goal, or it
# over-counts actionable-without-goal findings and fires approved_but_drainable
# on a false set every quiescence cycle (rb-3014 / bravo finding msg-2962,
# empirically confirmed 2026-07-10: 3 of 5 flagged findings were bare-tag
# goal-linked). Shape per CLAUDE.md ID Formats: g-<aspiration digits>-<goal
# digits>, INCLUDING the optional "-<letter>" decomposition suffix
# (-d, -b) and the g-xw-<ts>-NN cross-world form — i.e. the
# canonical goal-id shape of aspirations.py GOAL_ID_RE (SSOT). The earlier
# "^g-\d+-\d+$" missed the "-<letter>" suffix, so a finding tagged e.g.
# "-d" (a real completed goal) read as UN-linked and fired a false
# approved_but_drainable every quiescence cycle (, alpha finding
# msg-4248 incident 2026-07-25). Still fully anchored so g-prefixed non-goal
# tags (git-sync, cc-05) do NOT match.
_GOAL_ID_TAG_RE = re.compile(r"^g-(?:\d+-\d+(?:-[a-z])?|xw-\d{8}T\d{6}-\d{2})$")


# --- Config ------------------------------------------------------------------

def _load_config():
    """Read quiescence_gate block from aspirations.yaml.

    Fails loud if the block is missing — config is the single source of truth.
    Routes through _config_overlay.merged_config so meta/config-overrides.yaml
    keys `aspirations.quiescence_gate.<k>` (e.g. sleep_seconds_max 7200 for a
    deployment that sleeps in 2-hour blocks — user directive 2026-09-03,
    g-357-90) take effect; a malformed override fails loud there too (rb-215).
    """
    from _config_overlay import merged_config
    cfg = merged_config("aspirations.yaml") or {}
    qg = cfg.get("quiescence_gate")
    if qg is None:
        raise RuntimeError(
            "aspirations.yaml missing 'quiescence_gate' block. "
            "See core/config/aspirations.yaml — Change 2 must be configured."
        )
    return qg


# --- WM read/merge/write ------------------------------------------------------
#
# wm read goes through _rt.wm_read (daemon client). wm set still uses
# subprocess (wm.py set is an ALIVE subcommand). Both route through the
# daemon endpoint so schema stays in one place.

def _wm_read_loop_state():
    """Return loop_state dict (or {} if unset/missing).

    Uses _rt.wm_read (daemon client) — the wm.py read CLI was deleted in
    the 2026-05-14 cutover.

    Decode tolerance (g-115-766 / g-115-944 / g-115-797-A6): replaces the
    prior bare json.loads + silent-collapse-to-{} with the corruption-
    tolerant pattern from consolidation-health.py::_tolerant_decode
    (landed via g-115-796). Empty body returns {} (valid empty state — a
    fresh WM has no loop_state yet). JSONDecodeError OR non-dict aggregate
    emits a stderr diagnostic and sys.exit(1) — a silent {} would mask
    quiescence drift verify (the consumer treats {} as "no prior loop_state"
    and proceeds, which is the wrong fall-back when the body is corrupt
    rather than missing). RtError stays soft (return {}) because the
    quiescence gate is a script-gated decision over which the caller has
    a fail-open wrapper; corruption is qualitatively different from
    daemon-unreachable.
    """
    try:
        raw = _rt.wm_read(slot="loop_state", as_json=True)
    except _rt.RtError as e:
        print(f"[quiescence-gate] wm read failed: {e.body or e}", file=sys.stderr)
        return {}
    stripped = (raw or "").lstrip()
    if not stripped:
        return {}  # valid empty state — not a corruption signal
    try:
        obj, _consumed = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError as exc:
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            f"quiescence-gate: loop_state JSONDecodeError ({exc}); body prefix: {body_prefix!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    if obj is None:
        # JSON null literal — canonical "empty slot" serialization from
        # wm-set.sh (e.g., `echo 'null' | wm-set.sh loop_state`). Match
        # legacy silent-empty-state behavior for this specific shape;
        # corruption is still fatal at the JSONDecodeError + non-dict
        # branches above and below.
        return {}
    if not isinstance(obj, dict):
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            f"quiescence-gate: loop_state non-dict aggregate (type={type(obj).__name__}); "
            f"body prefix: {body_prefix!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    return obj


def _wm_write_loop_state(loop_state):
    """Overwrite the loop_state slot with the caller's fully-merged dict.

    CRITICAL contract — do NOT change without reading this entire comment.
    This is a pure writer. The caller (cmd_check / cmd_verify_wake) is
    responsible for doing read-merge-write at its level: read current,
    mutate `signals.quiescence`, preserve every sibling field, pass the
    whole dict here. A prior version did an inner read + discard, which
    both wasted a wm.py subprocess call and lied about the semantics.
    If you add a second caller, match the same discipline — do not
    attempt to merge inside this function.
    """
    payload = json.dumps(loop_state, ensure_ascii=False, default=str)
    py = sys.executable
    script = CORE_ROOT / "scripts" / "wm.py"
    try:
        r = subprocess.run(
            [py, str(script), "set", "loop_state"],
            input=payload, capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
    except Exception as e:
        print(f"[quiescence-gate] wm.py write failed: {e}", file=sys.stderr)
        return False
    if r.returncode != 0:
        print(f"[quiescence-gate] wm.py set rc={r.returncode}: "
              f"{(r.stderr or '').strip()}", file=sys.stderr)
        return False
    return True


# --- Blocked-goal enumeration -------------------------------------------------

class _GoalSelectorUnavailable(RuntimeError):
    """Raised when the goal-selector module cannot be imported ().

    Lets cmd_check / cmd_verify_wake fail-open with a noop JSON instead of
    the gate crashing with an uncaught traceback. The B6.5 caller
    (aspirations-all-blocked Step B6.5) parses stdout JSON AND branches on
    exit code; an uncaught ImportError emits NO JSON and a nonzero exit,
    breaking that contract right before the B7.2 RETURN — the exact
    all-blocked path g-115-770 hardens. g-115-763 fixed the specific
    selector trigger; this makes the gate fail-open against the NEXT
    transitive import break.
    """


def _collect_blocked_entries():
    """Load live aspirations and call goal-selector.collect_blocked().

    Direct module import (not subprocess) so blocker_ref dicts come back as
    Python objects — the hash check in cmd_check needs that.

    Raises _GoalSelectorUnavailable if goal-selector cannot be imported so
    callers can emit a noop instead of crashing the gate (g-115-770).
    """
    # Late import so this script can be invoked even before goal-selector.py
    # loads its own heavy dependencies.
    import importlib
    try:
        gs = importlib.import_module("goal-selector")
    except ImportError as e:
        raise _GoalSelectorUnavailable(str(e)) from e
    from _paths import WORLD_DIR

    asps = []
    asps.extend(_load_aspirations_from(
        AGENT_DIR / "aspirations.jsonl" if AGENT_DIR else None
    ))
    asps.extend(_load_aspirations_from(
        WORLD_DIR / "aspirations.jsonl" if WORLD_DIR else None
    ))

    return gs.collect_blocked(
        asps,
        known_blockers=_known_blockers(),
        defer_reason_timeout_hours=120,
        dependency_timeout_hours=48,
    )


def _compute_hash(blocker_refs):
    """Stable hash of the blocker-set. Same set across iterations → same hash."""
    ext_ids = sorted(
        ref.get("external_id", "") for ref in blocker_refs
        if isinstance(ref, dict)
    )
    h = hashlib.sha256("\n".join(ext_ids).encode("utf-8")).hexdigest()[:16]
    return h


# Cache filename for the cross-iteration quiescence short-circuit ().
# MUST equal quiescence-cycle-cache.py CACHE_NAME — a drift only causes a
# fail-open cache MISS (the safe direction: the full cycle runs), never a
# wrong short-circuit.
CYCLE_CACHE_NAME = "quiescence-last-cycle.json"


def _write_cycle_cache(current_hash, refs_for_hash, sleep_seconds, goal_count, now,
                       earliest_wake_at=None):
    """Write the approved-cycle cache consumed by quiescence-cycle-cache.py.

    Single writer of <agent>/session/quiescence-last-cycle.json. Called ONLY on
    the approved path of cmd_check (guarded by `if approved:`), so the cache
    reflects the LAST GATE-APPROVED quiescence cycle. The fast-path script
    (quiescence-cycle-cache.py) re-validates this snapshot (same blocker hash,
    no blocker expired, no new work, no pending signal, cycle_count < cap) and
    on a HIT increments cycle_count + re-sleeps WITHOUT reloading the heavy
    skill chain.

    cycle_count=0 here resets the consecutive-short-circuit counter on every
    fresh gate approval. A changed blocker set yields a new current_hash, which
    rewrites the cache with a 0 counter — the exact drift-reset the design
    requires ("Reset the counter on any blocker_set_hash change").

    Atomic tempfile+rename. Fail-soft: any error is logged to stderr and
    swallowed — a cache write failure must never break the gate's approval
    (the next fast-path check simply MISSES and runs the full cycle).
    """
    if AGENT_DIR is None:
        return
    payload = {
        "blocker_set_hash": current_hash,
        "blocker_refs": [
            {"external_id": (r or {}).get("external_id"),
             "expires_at": (r or {}).get("expires_at")}
            for r in refs_for_hash if isinstance(r, dict)
        ],
        "sleep_seconds": sleep_seconds,
        "goal_count": goal_count,
        "cycle_count": 0,
        "wake_outcome": None,
        "earliest_wake_at": earliest_wake_at,
        "approved_at": now.isoformat(timespec="seconds"),
    }
    p = AGENT_DIR / "session" / CYCLE_CACHE_NAME
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str),
                       encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        print(f"[quiescence-gate] cycle-cache write failed: {e}", file=sys.stderr)


# --- : prolonged-quiescence escalating user-ping --------------------
#
# Once an agent is in steady quiescence (same blocker_set hash for HOURS) AND
# every blocked goal is gated by a user-only blocker_ref, silent sleep leaves
# the user no signal about what they could clear. This emits ONE focused
# escalation (the single highest-leverage blocker) per blocker_set_hash per
# throttle window — NOT the email-flood pattern alpha flagged as miscalibrated.
# The orchestrator (aspirations-all-blocked B6.5) reads prolonged_quiescence +
# should_notify and fires /notify-user exactly once.

# blocker_ref types ONLY the user can clear (mirror of capability-before-user.md
# "human-only"). When EVERY blocked goal is gated by one of these, the queue is
# genuinely user-bound and an escalating ping is warranted.
USER_ONLY_BLOCKER_TYPES = frozenset({
    "user_action", "credentials-required", "security-trust", "physical-hardware",
})

# Per-blocker_set_hash throttle file (recovery_action: clear on /start --recover).
PROLONGED_PING_NAME = "prolonged-quiescence-pinged.json"


def _read_prolonged_pinged():
    """Read the {blocker_set_hash: last_pinged_iso} throttle map. {} on any error."""
    if AGENT_DIR is None:
        return {}
    p = AGENT_DIR / "session" / PROLONGED_PING_NAME
    try:
        if not p.exists():
            return {}
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"[quiescence-gate] prolonged-ping read failed: {e}", file=sys.stderr)
        return {}


def _write_prolonged_pinged(pinged):
    """Atomic write of the throttle map. Fail-soft (mirrors _write_cycle_cache)."""
    if AGENT_DIR is None:
        return
    p = AGENT_DIR / "session" / PROLONGED_PING_NAME
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(pinged, ensure_ascii=False, default=str),
                       encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        print(f"[quiescence-gate] prolonged-ping write failed: {e}", file=sys.stderr)


def _evaluate_prolonged_quiescence(blocked_entries, current_hash,
                                   hash_first_seen_at, now, cfg):
    """Decide whether a prolonged, all-user-gated quiescence warrants ONE
    focused escalating user-ping (g-303-11).

    Fires only when (a) the SAME blocker set has persisted >= prolonged_hours of
    wall-clock (`hash_first_seen_at`), AND (b) EVERY blocked goal is gated by a
    user-only blocker_ref type. Throttled per blocker_set_hash via a dedup file
    so a long quiescence window produces at most ONE notification (the
    anti-email-flood requirement that motivated this goal).

    Returns a dict merged into the gate's stdout. `should_notify` is True at most
    once per (hash, throttle window); the orchestrator reads it and fires
    /notify-user. Pure decision + a single fail-soft file write — never raises.
    """
    out = {"prolonged_quiescence": False, "should_notify": False}
    if not blocked_entries:
        return out
    hours = _hours_since(hash_first_seen_at)
    threshold_h = float(cfg.get("prolonged_quiescence_hours", 4.0))
    if hours is None or hours < threshold_h:
        return out
    # Every blocked goal must be gated by a user-only blocker_ref type.
    all_user_gated = all(
        isinstance(e.get("blocker_ref"), dict)
        and e["blocker_ref"].get("type") in USER_ONLY_BLOCKER_TYPES
        for e in blocked_entries
    )
    if not all_user_gated:
        return out
    # Highest-leverage blocker = the external_id gating the most blocked goals.
    leverage = {}
    titles_by_ext = {}
    for e in blocked_entries:
        ext = (e.get("blocker_ref") or {}).get("external_id") or "(unknown)"
        leverage[ext] = leverage.get(ext, 0) + 1
        titles_by_ext.setdefault(ext, []).append(
            str(e.get("title") or e.get("goal_id") or "")[:80])
    hi_ext = max(leverage, key=lambda k: leverage[k])
    out["prolonged_quiescence"] = True
    out["prolonged_payload"] = {
        "highest_leverage_blocker_id": hi_ext,
        "blocker_count": len(blocked_entries),
        "distinct_blocker_count": len(leverage),
        "hours_in_quiescence": round(hours, 1),
        "sample_blocked_goal_titles": titles_by_ext.get(hi_ext, [])[:5],
    }
    # Throttle per blocker_set_hash: at most one ping per hash per window.
    throttle_h = float(cfg.get("prolonged_throttle_hours", 12.0))
    pinged = _read_prolonged_pinged()
    last_h = _hours_since(pinged.get(current_hash))
    if last_h is not None and last_h < throttle_h:
        out["throttled_hours_remaining"] = round(throttle_h - last_h, 1)
        return out  # should_notify stays False — already pinged this window
    # Fire: record the ping (prune entries older than 2x window to stay bounded).
    cutoff_h = throttle_h * 2
    pinged = {h: t for h, t in pinged.items()
              if h == current_hash or ((_hours_since(t) or 0.0) < cutoff_h)}
    pinged[current_hash] = now.isoformat(timespec="seconds")
    _write_prolonged_pinged(pinged)
    out["should_notify"] = True
    return out


# --- Magic Wand 2: newly-arrived-work detection ------------------------------
#
# At quiescence-entry (cmd_check), capture total goal count in the active
# snapshot. At wake-verify (cmd_verify_wake), re-count and re-run the
# selector when the delta exceeds min_delta. If the selector returns
# >= min_executable candidates, flag drift via the synthetic external_id
# "__new_work_arrived__". Two-tier intentionally: cheap count delta avoids
# the expensive selector when nothing arrived; selector only fires when
# there are at least min_delta new goals that MIGHT be executable.

def _known_blockers():
    """Read known_blockers from working memory.

    Uses _rt.wm_read (daemon client) — the wm.py read CLI was deleted in
    the 2026-05-14 cutover.

    Decode tolerance (g-115-766 / g-115-944 / g-115-797-A6): replaces the
    prior bare json.loads + bare-`except Exception: pass` + silent-[]
    with the corruption-tolerant pattern from
    consolidation-health.py::_tolerant_decode (landed via g-115-796).
    Empty body returns [] (valid empty state — no known blockers). RtError
    is logged and returns [] (daemon-unreachable is a soft signal; matches
    the historical contract that quiescence treats absent infrastructure
    as no-blockers). JSONDecodeError OR non-list aggregate emits a stderr
    diagnostic and sys.exit(1) — a silent [] on a corrupt body would
    masquerade as "no blockers" and approve quiescence on a queue we cannot
    actually verify.
    """
    try:
        raw = _rt.wm_read(slot="known_blockers", as_json=True)
    except _rt.RtError as e:
        print(f"[quiescence-gate] wm read failed: {e.body or e}", file=sys.stderr)
        return []
    stripped = (raw or "").lstrip()
    if not stripped:
        return []  # valid empty state — not a corruption signal
    try:
        obj, _consumed = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError as exc:
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            f"quiescence-gate: known_blockers JSONDecodeError ({exc}); body prefix: {body_prefix!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    if obj is None:
        # JSON null literal — canonical "empty slot" serialization from
        # wm-set.sh (e.g., `echo 'null' | wm-set.sh known_blockers`). Match
        # legacy silent-empty-state behavior for this specific shape;
        # corruption is still fatal at the JSONDecodeError + non-list
        # branches above and below.
        return []
    if not isinstance(obj, list):
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            f"quiescence-gate: known_blockers non-list aggregate (type={type(obj).__name__}); "
            f"body prefix: {body_prefix!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    return obj


def _load_aspirations_from(path):
    """Load aspirations.jsonl at given path. Fail-soft."""
    if path is None or not Path(path).exists():
        return []
    out = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return out


def _total_goal_count():
    """Sum of goal counts across world + agent aspirations files."""
    from _paths import WORLD_DIR
    total = 0
    for asp_path in (
        AGENT_DIR / "aspirations.jsonl" if AGENT_DIR else None,
        WORLD_DIR / "aspirations.jsonl" if WORLD_DIR else None,
    ):
        for asp in _load_aspirations_from(asp_path):
            total += len(asp.get("goals", []))
    return total


def _check_newly_arrived_work(snap, cfg, drifted):
    """Append drift entry if N+ executable goals arrived during sleep.

    Mutates `drifted` in place. Returns silently when no baseline was
    captured (snapshot pre-dates Magic Wand 2 or the field was absent)
    because there is nothing to compare against.
    """
    nawc = cfg.get("newly_arrived_work") or {}
    if not nawc.get("enabled", True):
        return
    goal_count_at_entry = snap.get("goal_count_at_entry")
    # No baseline → no comparison. Do NOT default to 0 — that would
    # treat any non-empty queue as drift on the first cycle after
    # deploy and auto-disable quiescence within 3 iterations.
    if goal_count_at_entry is None:
        return

    current_goal_count = _total_goal_count()
    new_goals_arrived = current_goal_count - goal_count_at_entry
    min_delta = int(nawc.get("min_delta", 2))
    if new_goals_arrived < min_delta:
        return

    # Tier 2: run the selector to confirm at least min_executable executable
    # candidates exist (a goal that arrived in blocked/deferred state should
    # NOT trigger early wake).
    try:
        import importlib
        gs = importlib.import_module("goal-selector")
        from _paths import WORLD_DIR

        world_asps = _load_aspirations_from(
            WORLD_DIR / "aspirations.jsonl" if WORLD_DIR else None
        )
        agent_asps = _load_aspirations_from(
            AGENT_DIR / "aspirations.jsonl" if AGENT_DIR else None
        )

        kb = _known_blockers()
        candidates = []
        if world_asps:
            candidates.extend(gs.collect_candidates(
                world_asps, known_blockers=kb, source="world"
            ))
        if agent_asps:
            candidates.extend(gs.collect_candidates(
                agent_asps, known_blockers=kb, source="agent"
            ))

        min_executable = int(nawc.get("min_executable", 2))
        if len(candidates) >= min_executable:
            # external_id MUST stay "__new_work_arrived__" — cmd_verify_wake
            # treats any non-empty `drifted` list as a wake-miss. Renaming
            # this synthetic id silently breaks audit-log correlation.
            drifted.append({
                "external_id": "__new_work_arrived__",
                "type": "newly_executable_goals",
                "count": len(candidates),
                "prior_hash": str(goal_count_at_entry),
                "current_hash": str(current_goal_count),
            })
    except Exception as e:
        # Fail-open: don't block wake-verify on selector errors. The
        # blocker-hash drift check still runs.
        print(f"[quiescence-gate] newly-arrived-work selector check failed: {e}",
              file=sys.stderr)


# --- Session start time -------------------------------------------------------
#
# For the 40% budget check. Falls back to loop_state.session_start_at if set,
# else to handoff.yaml session start, else to quiescence first-entry timestamp
# (which keeps budget_pct meaningful even with partial data).

def _session_start_at(loop_state):
    """Return ISO timestamp of session start, or None if undiscoverable."""
    s = loop_state.get("session_start_at")
    if s:
        return s
    try:
        import yaml
        ho = AGENT_DIR / "session" / "handoff.yaml" if AGENT_DIR else None
        if ho and ho.exists():
            with open(ho, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            cur = data.get("current_session") or {}
            if cur.get("started"):
                return cur["started"]
    except Exception:
        pass
    return None


def _hours_since(iso_str):
    if not iso_str:
        return None
    try:
        dt = parse_naive_iso(iso_str)
        if dt is None:
            return None
        return (datetime.now() - dt).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


# --- Decision logging ---------------------------------------------------------

def _append_log(relative_path, record):
    """Append-only JSONL write to <agent>/session/<relative_path>.

    Uses the same lock-append pattern as blocker-gate-overrides. Fail-silent
    on write error — the decision has already been made.
    """
    if AGENT_DIR is None:
        return None
    from _fileops import locked_append_jsonl
    path = AGENT_DIR / "session" / relative_path
    try:
        locked_append_jsonl(str(path), record)
        return str(path)
    except Exception as e:
        print(f"[quiescence-gate] log append failed ({relative_path}): {e}",
              file=sys.stderr)
        return None


# --- MW#3 helper: fragmentation downgrade ------------------------------------

def evaluate_fragmentation_downgrade(history, baseline_sleep_seconds, cfg):
    """Decide whether to downgrade sleep_seconds based on actual_sleep_history.

    Magic Wand #3 (alpha session-60, 2026-05-07). Exposed as a top-level
    function so tests can exercise it without setting up the full check
    pipeline.

    Args:
        history: list[int] — recent actual_sleep_s values from verify_wake.
        baseline_sleep_seconds: int — the proposed sleep_seconds before
                                       downgrade (typically sleep_seconds_min).
        cfg: dict — quiescence_gate config block, providing
             fragmentation_window, fragmentation_threshold_s,
             fragmentation_min_samples, fragmented_sleep_seconds.

    Returns:
        (sleep_seconds, fragmented_downgrade, reset_counter)
        - sleep_seconds: int — possibly downgraded value
        - fragmented_downgrade: bool — True if we lowered the duration
        - reset_counter: bool — True if the fragmentation streak should reset
                                 (window cleared the threshold)
    """
    history = list(history or [])
    window = int(cfg.get("fragmentation_window", 5))
    threshold = int(cfg.get("fragmentation_threshold_s", 600))
    min_samples = int(cfg.get("fragmentation_min_samples", 3))
    downgraded_to = int(cfg.get("fragmented_sleep_seconds", 600))

    if len(history) < min_samples:
        return baseline_sleep_seconds, False, False

    recent = history[-window:]
    avg_sleep = sum(recent) / len(recent)
    if avg_sleep < threshold:
        # Only downgrade — never upgrade past baseline.
        if downgraded_to < baseline_sleep_seconds:
            return downgraded_to, True, False
        return baseline_sleep_seconds, False, False
    # Window cleared the threshold — reset the counter.
    return baseline_sleep_seconds, False, True


# --- B6.8: drainable-evidence collection () --------------------------
#
# When quiescence APPROVES (queue honestly user-gated), the agent would
# otherwise sleep 30-60min even when drainable framework-hygiene work exists
# that does NOT depend on the user-gated blockers (tree-decompose candidates,
# unreflected resolved hypotheses, actionable findings without a linked goal).
# The B6.8 branch (aspirations-all-blocked SKILL.md) drains ONE such unit per
# cycle before sleeping. This gate supplies the evidence the orchestrator
# branches on: approved_but_drainable + drainable_evidence + drainable_summary.
#
# Each source is INDEPENDENTLY fail-open (returns 0 on any error). The gate is a
# script-gated decision with a fail-open caller wrapper, and a transient daemon
# read hiccup must never crash quiescence or spuriously change its verdict.
# Evidence is gathered ONLY on the approved path (cmd_check), so the denied path
# (which routes to B6.7) pays no extra daemon round-trips.


def _read_decompose_candidates_raw():
    """Raw stdout of `tree.py read --decompose-candidates` (a JSON array).

    Isolated for testability AND because this flag is NOT daemon-served:
    tree-read.sh force-falls-back to `python3 tree.py read
    --decompose-candidates` for the computationally-heavy candidate walk
    (FORCE_FALLBACK=1), so GET /v1/tree/read 400s on this flag. We invoke
    tree.py the SAME way the wrapper does — via sys.executable, NOT _rt (would
    400) and NOT bash (rb-225/rb-247 Windows bash-subprocess hang); mirrors the
    _wm_write_loop_state subprocess pattern. Raises on non-zero rc."""
    py = sys.executable
    script = CORE_ROOT / "scripts" / "tree.py"
    r = subprocess.run(
        [py, str(script), "read", "--decompose-candidates"],
        capture_output=True, text=True, timeout=90,
        encoding="utf-8", errors="replace",
        cwd=str(CORE_ROOT.parent),  # PROJECT_ROOT — the wrapper cd's here
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"tree.py read --decompose-candidates rc={r.returncode}: "
            f"{(r.stderr or '').strip()[:200]}")
    return r.stdout


def _count_tree_decompose_candidates():
    """Count nodes from tree.py read --decompose-candidates (JSON array). 0 on error."""
    try:
        raw = _read_decompose_candidates_raw()
        stripped = (raw or "").strip()
        if not stripped:
            return 0
        obj = json.loads(stripped)
        return len(obj) if isinstance(obj, list) else 0
    except Exception as e:
        print(f"[quiescence-gate] decompose-candidate count failed: {e}", file=sys.stderr)
        return 0


def _count_unreflected_hypotheses():
    """Count REFLECTABLE unreflected hypotheses (JSON array -> outcome filter).

    g-115-6173: the raw --unreflected count is the full never-reflected
    backlog (384 measured), dominated by UNRESOLVABLE/EXPIRED/no-outcome
    records that /reflect-on-outcome can never consume (g-115-4558). Feeding
    that raw number into _DRAINABLE_PRIORITY pinned primary_target to
    'hypothesis' with a 100%-undrainable target — the B6.8 all-blocked drain
    then routed forever at work that cannot drain. Only the reflectable
    subset is drainable, so only it counts here. 0 on error.
    """
    try:
        raw = _rt.rt_call("GET", "/v1/pipeline/read", query="unreflected=1")
        stripped = (raw or "").strip()
        if not stripped:
            return 0
        obj = json.loads(stripped)
        return _reflectable.count_reflectable(obj)
    except Exception as e:
        print(f"[quiescence-gate] unreflected-hypothesis count failed: {e}", file=sys.stderr)
        return 0


def _finding_ids_linked_by_goal_origin():
    """Finding-ids linked to a goal (open OR completed, world+agent) via its
    origin_signal `board_post:{finding-id}` (g-115-3057). A finding whose ONLY
    goal link is a completed goal's origin_signal carries no goal-id tag, so the
    tag check in _count_actionable_findings_without_goal misses it and re-flags
    it drainable on EVERY quiescence cycle — B6.8 then re-investigates a
    fully-handled finding forever (the completed-twin blind spot; concrete:
    msg-4277 -> g-250-262 completed, origin_signal=board_post:msg-4277).
    origin_signal is the strong unique-per-finding dedup key (rb-5058); this
    mirrors the origin_signal_completed strategy g-115-3048 added to
    goal_duplication.py. Fail-soft: empty set on any error."""
    from _paths import WORLD_DIR
    covered = set()
    for asp_path in (
        AGENT_DIR / "aspirations.jsonl" if AGENT_DIR else None,
        WORLD_DIR / "aspirations.jsonl" if WORLD_DIR else None,
    ):
        for asp in _load_aspirations_from(asp_path):
            for goal in asp.get("goals", []):
                if not isinstance(goal, dict):
                    continue
                origin = (goal.get("origin_signal") or "").strip()
                if origin.startswith("board_post:"):
                    fid = origin[len("board_post:"):].strip()
                    if fid:
                        covered.add(fid)
    return covered


def _count_actionable_findings_without_goal():
    """Count findings (board, 7d) tagged 'actionable', NOT goal-linked, and
    not authored by this agent — matching B6.8 Target 3's conversion filter so
    the gate's count agrees with what the orchestrator would actually drain.
    "Goal-linked" = a literal 'goal_id' tag, a 'goal_id:<id>' prefixed tag, a
    bare goal-id-shaped tag (e.g. 'g-115-1766' — rb-3014), OR a goal (open or
    completed) whose origin_signal is `board_post:{this-finding-id}`
    (g-115-3057 completed-twin fix). board-read emits JSONL (one post per line)
    or a JSON array; tolerate both. 0 on any error."""
    try:
        agent = os.environ.get("MIND_AGENT", "") or ""
        raw = _rt.rt_call("GET", "/v1/board/read",
                          query="channel=findings&since=7d&json=1")
        stripped = (raw or "").strip()
        if not stripped:
            return 0
        if stripped.startswith("["):
            posts = json.loads(stripped)
        else:
            posts = []
            for line in stripped.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    posts.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        # Findings covered by a goal's origin_signal (open or completed) carry
        # no goal-id tag — resolve them once so the completed-twin case (a
        # fully-handled finding whose only link is a COMPLETED goal) is not
        # counted drainable ().
        origin_covered = _finding_ids_linked_by_goal_origin()
        count = 0
        for post in posts:
            if not isinstance(post, dict):
                continue
            tags = post.get("tags") or []
            if not isinstance(tags, list):
                continue
            tag_strs = [str(t) for t in tags]
            has_actionable = "actionable" in tag_strs
            has_goal = any(
                t == "goal_id"
                or t.startswith("goal_id:")
                or _GOAL_ID_TAG_RE.match(t)
                for t in tag_strs
            )
            covered_by_origin = str(post.get("id") or "") in origin_covered
            if (has_actionable and not has_goal and not covered_by_origin
                    and post.get("author") != agent):
                count += 1
        return count
    except Exception as e:
        print(f"[quiescence-gate] actionable-finding count failed: {e}", file=sys.stderr)
        return 0


def _collect_drainable_evidence(cfg):
    """Return the 3-field drainable_evidence dict (B6.8)."""
    return {
        "tree_decompose_candidates": _count_tree_decompose_candidates(),
        "unreflected_hypotheses": _count_unreflected_hypotheses(),
        "actionable_findings_without_goal": _count_actionable_findings_without_goal(),
    }


# Priority order MUST match the B6.8 three-target drain sequence
# (aspirations-all-blocked SKILL.md): decompose > hypothesis > finding. drainable_summary.primary_target names the first target with a
# non-zero count so the orchestrator's log line + routing agree with the gate.
_DRAINABLE_PRIORITY = [
    ("decompose", "tree_decompose_candidates"),
    ("hypothesis", "unreflected_hypotheses"),
    ("finding", "actionable_findings_without_goal"),
]


def _drainable_summary(evidence):
    """Reduce drainable_evidence to {primary_target, primary_target_count,
    any_target_available} using the B6.8 priority order."""
    primary_target = None
    primary_target_count = 0
    for name, key in _DRAINABLE_PRIORITY:
        n = int(evidence.get(key, 0) or 0)
        if n >= 1:
            primary_target = name
            primary_target_count = n
            break
    any_available = any(int(evidence.get(k, 0) or 0) >= 1
                        for _, k in _DRAINABLE_PRIORITY)
    return {
        "primary_target": primary_target,
        "primary_target_count": primary_target_count,
        "any_target_available": any_available,
    }


# --- Mode: --check ------------------------------------------------------------

def cmd_check(args, cfg):
    """Evaluate quiescence eligibility. Exit 0 if approved, 1 if denied."""
    now = datetime.now()
    loop_state = _wm_read_loop_state()
    q = (loop_state.get("signals") or {}).get("quiescence") or {}

    # Session-boundary reset. loop_state.signals.quiescence persists across
    # sessions in working-memory, but sleep_total_s (C5 budget accumulator)
    # and misses (C6 cooldown) are strictly per-session concepts — carrying
    # them forward would either falsely deny (C5) or auto-disable (C6) a
    # fresh session based on the prior session's history. current_hash and
    # streak DO survive intentionally: same-blocker-set across sessions is
    # real signal that the external gating is stable.
    #
    # Detection: compare the current loop_state.session_start_at to the one
    # we stored last check. If they differ (or ours is absent), zero the
    # per-session counters and record the new start. No external caller
    # needed — the gate is self-contained.
    current_session_start = _session_start_at(loop_state)
    session_rollover = (
        current_session_start
        and q.get("session_start_at") != current_session_start
    )
    if session_rollover:
        q = dict(q)
        q["sleep_total_s"] = 0
        q["misses"] = 0
        q["session_start_at"] = current_session_start

    # --- Condition evaluation -------------------------------------------------
    reasons = []  # accumulates structured denial reasons

    try:
        blocked_entries = _collect_blocked_entries()
    except _GoalSelectorUnavailable as e:
        # Fail-open (): a transitive goal-selector import failure
        # must NOT crash the gate. DENY via exit 1 so the orchestrator
        # routes to the normal B6.7/B7 backoff. Exit 0 would be wrong here —
        # B7.2 would then run with no sleep_seconds and verify-wake would
        # later find no active_snapshot.
        print(json.dumps({"outcome": "noop",
                           "reason": f"goal-selector import failed: {e}"}))
        sys.exit(1)

    # C1: caller must assert all-blocked (they already saw goal-selector output).
    if not args.all_blocked:
        reasons.append({
            "condition": "C1_all_blocked",
            "detail": "caller did not pass --all-blocked flag",
        })

    # C2/C3: every blocked goal must have a valid, non-expired blocker_ref.
    refs_for_hash = []
    missing_ref = []
    expired_ref = []
    for e in blocked_entries:
        ref = e.get("blocker_ref")
        if not isinstance(ref, dict):
            missing_ref.append(e.get("goal_id"))
            continue
        refs_for_hash.append(ref)
        exp = ref.get("expires_at")
        if not exp:
            # ABSENT is not "not yet expired" (). A missing
            # expires_at is NO liveness evidence at all, so treating it as
            # unexpired lets a ref gate the queue indefinitely with no TTL
            # that can ever break it — precisely the narrative-laundering the
            # blocker_ref requirement exists to prevent, reachable by omitting
            # a field the schema calls automatic. This gate SUPPRESSES loop
            # work (it approves sleep), so guard-487 governs: a suppression
            # gate fails CLOSED when its input cannot establish the fact it
            # needs. That is already this block's posture for an UNPARSEABLE
            # value below — absence was the one case that slipped through, not
            # a deliberate exemption.
            #
            # Auto-population lives in gates/blocker_ref.validate(), which is
            # reached ONLY via the --blocker-ref flag paired with a
            # defer_reason / status=blocked write. A DIRECT `update-goal <id>
            # blocker_ref '<json>'` field write lands verbatim at
            # aspirations.py:1905 with no validation and no TTL. Measured
            # 2026-07-27: 7 of 11 live blocked goals carried a ref with no
            # expires_at. Disqualifying here is the READ-side backstop; the
            # write-side normalization is tracked separately.
            expired_ref.append({
                "goal_id": e.get("goal_id"),
                "external_id": ref.get("external_id"),
                "expired_at": None,
                "missing_expires_at": True,
            })
        else:
            try:
                _exp_dt = parse_naive_iso(exp)
                if _exp_dt is None:
                    raise ValueError("unparseable expires_at")
                if _exp_dt <= now:
                    expired_ref.append({
                        "goal_id": e.get("goal_id"),
                        "external_id": ref.get("external_id"),
                        "expired_at": exp,
                    })
            except (ValueError, TypeError):
                expired_ref.append({
                    "goal_id": e.get("goal_id"),
                    "external_id": ref.get("external_id"),
                    "expired_at": str(exp),
                    "parse_error": True,
                })
    if missing_ref:
        reasons.append({
            "condition": "C2_blocker_ref_required",
            "detail": f"{len(missing_ref)} blocked goal(s) lack blocker_ref",
            "sample_goal_ids": missing_ref[:5],
        })
    if expired_ref:
        # Report the two disqualifying shapes separately — "in the past" would
        # misdescribe a ref that has no expires_at at all, and the reader's next
        # action differs (a past TTL means re-probe the blocker; an absent one
        # means the ref was written through the unvalidated field path and needs
        # normalizing). .
        # THREE disqualifying shapes, not two (, folding in
        # bravo-fec-parse-error-lumped-into-past). n_past was computed by
        # SUBTRACTION — len(expired_ref) - n_missing — which swept every
        # parse_error ref into "expires_at in the past". That detail was false
        # by construction: a value that failed to parse was never compared to
        # anything, so nothing is known about whether it is past. It also sent
        # the reader to the wrong next action, which is the same reason the two
        # shapes above were split in : a past TTL means re-probe the
        # blocker, an absent one means normalize the ref, and an UNPARSEABLE one
        # means fix the value (commonly a tz-aware stamp from the unvalidated
        # direct-field write path). Counted directly now, never by subtraction.
        n_missing = sum(1 for r in expired_ref if r.get("missing_expires_at"))
        n_parse_error = sum(1 for r in expired_ref if r.get("parse_error"))
        n_past = len(expired_ref) - n_missing - n_parse_error
        parts = []
        if n_past:
            parts.append(f"{n_past} with expires_at in the past")
        if n_missing:
            parts.append(f"{n_missing} with NO expires_at (absent != unexpired)")
        if n_parse_error:
            parts.append(f"{n_parse_error} with UNPARSEABLE expires_at "
                         f"(never compared — not known to be past)")
        reasons.append({
            "condition": "C3_blocker_ref_future_expiry",
            "detail": f"{len(expired_ref)} blocker_ref(s) disqualify: " + ", ".join(parts),
            "missing_expires_at_count": n_missing,
            "past_expiry_count": n_past,
            "parse_error_count": n_parse_error,
            "sample": expired_ref[:5],
        })

    # C4: hysteresis — current hash must match prior AND streak >= threshold.
    current_hash = _compute_hash(refs_for_hash)
    prior_hash = q.get("current_hash")
    prior_streak = int(q.get("streak", 0) or 0)
    streak = prior_streak + 1 if prior_hash == current_hash else 1
    # : wall-clock anchor for prolonged-quiescence. Same lifecycle as
    # current_hash/streak — it survives session_rollover (which preserves both,
    # per the reset block above) and resets ONLY when the blocker set drifts.
    # hash_first_seen_at marks when THIS hash was FIRST observed, so
    # _evaluate_prolonged_quiescence can measure how long the user-bound queue
    # has actually persisted (not just this session's slice of it).
    if prior_hash == current_hash and q.get("hash_first_seen_at"):
        hash_first_seen_at = q.get("hash_first_seen_at")
    else:
        hash_first_seen_at = now.isoformat(timespec="seconds")
    hysteresis = int(cfg.get("hysteresis_iters", 2))
    if streak < hysteresis:
        reasons.append({
            "condition": "C4_hysteresis",
            "detail": f"streak={streak} < hysteresis_iters={hysteresis} "
                      f"(prior_hash={'match' if prior_hash == current_hash else 'drift'})",
        })

    # C5: budget — cumulative quiescent sleep vs elapsed session wall-clock.
    # Uses current_session_start captured above. With the session-boundary
    # reset in place, sleep_total_s is guaranteed to be 0 at session start,
    # so no "first-N-minutes" grace is needed — a fresh session naturally
    # has actual_pct=0 until the first quiescent sleep.
    elapsed_h = _hours_since(current_session_start) if current_session_start else None
    sleep_total_s = int(q.get("sleep_total_s", 0) or 0)
    budget_pct = float(cfg.get("budget_pct", 0.40))
    if elapsed_h is not None and elapsed_h > 0:
        elapsed_s = elapsed_h * 3600.0
        actual_pct = sleep_total_s / elapsed_s
        if actual_pct >= budget_pct:
            reasons.append({
                "condition": "C5_budget",
                "detail": f"quiescent_sleep={sleep_total_s}s is "
                          f"{actual_pct:.1%} of session ({elapsed_h:.1f}h elapsed); "
                          f"budget_pct={budget_pct:.1%}",
            })

    # C6: wake-miss cooldown.
    misses = int(q.get("misses", 0) or 0)
    cooldown = int(cfg.get("wake_miss_cooldown", 3))
    if misses >= cooldown:
        reasons.append({
            "condition": "C6_wake_miss_cooldown",
            "detail": f"consecutive_misses={misses} >= cooldown={cooldown}; "
                      f"quiescence auto-disabled for this session",
        })

    approved = not reasons

    # --- Persist hysteresis state regardless of outcome -----------------------
    # (the streak must advance even on deny iterations so the NEXT iter can
    # trip hysteresis once the LLM lands on the right blocker set)
    q_new = dict(q)
    q_new["current_hash"] = current_hash
    q_new["streak"] = streak
    q_new["hash_first_seen_at"] = hash_first_seen_at  #  wall-clock anchor
    q_new["last_check_at"] = now.isoformat(timespec="seconds")
    q_new.setdefault("iters", 0)
    q_new.setdefault("sleep_total_s", sleep_total_s)
    q_new.setdefault("misses", misses)

    # --- Sleep decision -------------------------------------------------------
    sleep_seconds = None
    fragmented_downgrade = False
    # B6.8 (): drainable-evidence fields default to the "no drain"
    # state; populated ONLY on the approved path below. The denied path routes
    # to B6.7 (which gathers its own signal) and pays no extra daemon reads.
    approved_but_drainable = False
    drainable_evidence = None
    drainable_summary = None
    # : default "no escalation" — populated only on the approved path,
    # mirroring the drainable-evidence defaults above. The denied path emits
    # these as False/None, so the orchestrator's notify branch is a no-op there.
    prolonged = {"prolonged_quiescence": False, "should_notify": False}
    if approved:
        sleep_seconds = int(cfg.get("sleep_seconds_min", 1800))
        # Cap respects config.sleep_seconds_max.
        smax = int(cfg.get("sleep_seconds_max", 3600))
        if sleep_seconds > smax:
            sleep_seconds = smax

        # MW#3 (alpha session-60, 2026-05-07): fragmentation downgrade. If the
        # rolling window of actual sleep durations averages below
        # fragmentation_threshold_s, the 30-minute sleep_seconds_min is
        # wishful thinking — bursty external signal will fragment it anyway.
        # See evaluate_fragmentation_downgrade docstring above for the
        # decision rule (extracted to top-level for testability).
        sleep_seconds, fragmented_downgrade, reset_counter = (
            evaluate_fragmentation_downgrade(
                q.get("actual_sleep_history"), sleep_seconds, cfg
            )
        )
        if fragmented_downgrade:
            q_new["fragmented_count"] = int(q.get("fragmented_count", 0) or 0) + 1
        elif reset_counter and int(q.get("fragmented_count", 0) or 0) > 0:
            q_new["fragmented_count"] = 0

        # D1: cap sleep_seconds at the earliest timer horizon so the loop
        # wakes when a deferred/recurring/blocker-expiry/hypothesis-gated
        # goal becomes executable. scan_queue is shared with dry-idle via
        # _wake_timers. Fail-open: scan failure leaves _wt_earliest=None
        # and sleep_seconds stays at the config/fragmentation value.
        _wt_earliest = None
        try:
            from _wake_timers import scan_queue, MIN_FLOOR_S as _TIMER_FLOOR
            _, _wt_earliest = scan_queue(now)
        except Exception:
            pass
        if _wt_earliest is not None:
            try:
                # parse_naive_iso handles BOTH the trailing Z and a numeric
                # +00:00 offset; the old .rstrip("Z") silently missed the latter,
                # so a tz-suffixed timer raised TypeError and this whole floor
                # was skipped by the except below (guard-4372).
                _wt_dt = parse_naive_iso(_wt_earliest)
                if _wt_dt is None:
                    raise ValueError("unparseable wake-timer timestamp")
                _wt_secs = int((_wt_dt - now).total_seconds())
                if _wt_secs < _TIMER_FLOOR:
                    _wt_secs = _TIMER_FLOOR
                if _wt_secs < sleep_seconds:
                    sleep_seconds = _wt_secs
            except (ValueError, TypeError):
                pass

        q_new["iters"] = int(q_new.get("iters", 0)) + 1

        # B6.8 (): gather drainable hygiene evidence (design section
        # 1/3). Only on the approved path. Mirrored into active_snapshot below
        # so post-sleep verify-wake can audit whether the drain happened.
        drainable_evidence = _collect_drainable_evidence(cfg)
        drainable_summary = _drainable_summary(drainable_evidence)
        approved_but_drainable = bool(drainable_summary["any_target_available"])

        # Capture once so active_snapshot.goal_count_at_entry and the 
        # cycle-cache see the SAME value (a second _total_goal_count() call
        # could TOCTOU-skew between the two writes).
        goal_count_at_entry = _total_goal_count()

        # sleep_total_s is incremented by --verify-wake with the ACTUAL
        # time slept (which may be less than sleep_seconds if a wake signal
        # fired). Writing the planned seconds here would over-count.
        q_new["active_snapshot"] = {
            "entered_at": now.isoformat(timespec="seconds"),
            "sleep_seconds": sleep_seconds,
            "earliest_wake_at": _wt_earliest,
            "refs": refs_for_hash,
            # Baseline for Magic Wand 2 newly-arrived-work check. Read by
            # cmd_verify_wake → _check_newly_arrived_work. If the field is
            # absent at wake, the consumer skips the check (no baseline →
            # no comparison) — that is the correct semantic, not a fallback.
            "goal_count_at_entry": goal_count_at_entry,
            # B6.8 (): drainable evidence at entry, so post-sleep
            # verify-wake can audit whether the drain actually reduced it.
            "drainable_evidence": drainable_evidence,
        }

        # : prolonged-quiescence escalating user-ping. Evaluated ONLY on
        # the approved path (we are about to sleep under this blocker set). Pure
        # decision + at most one fail-soft throttle-file write; never raises. The
        # throttle write here (like _write_cycle_cache below) is the single
        # source of truth for "already pinged this window", so the orchestrator
        # only has to act on should_notify, not re-derive the throttle.
        prolonged = _evaluate_prolonged_quiescence(
            blocked_entries, current_hash, hash_first_seen_at, now, cfg)

    # Merge signals.quiescence back into loop_state
    signals = dict(loop_state.get("signals") or {})
    signals["quiescence"] = q_new
    loop_state["signals"] = signals
    _wm_write_loop_state(loop_state)

    # : on approval, write the cross-iteration short-circuit cache so
    # quiescence-cycle-cache.py can collapse identical follow-on cycles WITHOUT
    # reloading the heavy skill chain. Single writer; fail-soft (a write error
    # never voids the approval — the fast path just MISSES next iteration).
    if approved:
        _write_cycle_cache(current_hash, refs_for_hash, sleep_seconds,
                           goal_count_at_entry, now,
                           earliest_wake_at=_wt_earliest)

    # --- Log and return -------------------------------------------------------
    record = {
        "timestamp": now.isoformat(timespec="seconds"),
        "agent": os.environ.get("MIND_AGENT", "") or "unknown",
        "mode": "check",
        "outcome": "approved" if approved else "denied",
        "blocked_count": len(blocked_entries),
        "ref_count": len(refs_for_hash),
        "current_hash": current_hash,
        "streak": streak,
        "reasons": reasons,
        "sleep_seconds": sleep_seconds,
        "fragmented_downgrade": fragmented_downgrade,
        "fragmented_count": int(q_new.get("fragmented_count", 0) or 0),
        # B6.8 (): persist the drain decision in the trail.
        "approved_but_drainable": approved_but_drainable,
        "drainable_summary": drainable_summary,
        # : prolonged-quiescence escalation decision in the trail.
        "prolonged_quiescence": prolonged.get("prolonged_quiescence", False),
        "prolonged_should_notify": prolonged.get("should_notify", False),
        "prolonged_payload": prolonged.get("prolonged_payload"),
    }
    _append_log(cfg.get("log_file", "quiescence-log.jsonl"), record)

    output = {
        "outcome": "approved" if approved else "denied",
        "sleep_seconds": sleep_seconds,
        "blocked_count": len(blocked_entries),
        "current_hash": current_hash,
        "streak": streak,
        "hysteresis_needed": hysteresis,
        "budget_used_pct": (sleep_total_s / (elapsed_h * 3600.0))
                           if (elapsed_h and elapsed_h > 0) else 0.0,
        "misses": misses,
        "reasons": reasons,
        "fragmented_downgrade": fragmented_downgrade,
        "fragmented_count": int(q_new.get("fragmented_count", 0) or 0),
        # B6.8 (): orchestrator branches on approved_but_drainable on
        # the approved path. False/None on the denied path → back-compatible
        # (absent-or-false routes straight to B7.2 sleep as before).
        "approved_but_drainable": approved_but_drainable,
        "drainable_evidence": drainable_evidence,
        "drainable_summary": drainable_summary,
        # : orchestrator (aspirations-all-blocked B6.5) reads these on
        # the approved path and fires /notify-user exactly once per window.
        "prolonged_quiescence": prolonged.get("prolonged_quiescence", False),
        "should_notify": prolonged.get("should_notify", False),
        "prolonged_payload": prolonged.get("prolonged_payload"),
        "throttled_hours_remaining": prolonged.get("throttled_hours_remaining"),
        "earliest_wake_at": _wt_earliest if approved else None,
    }
    print(json.dumps(output, ensure_ascii=False, default=str))

    sys.exit(0 if approved else 1)


# --- Mode: --verify-wake ------------------------------------------------------

def cmd_verify_wake(args, cfg):
    """Compare current external state against the pre-sleep snapshot.

    Emits a quiescence-miss record if ANY external_id's state hash changed
    but the blocker is still listed (i.e., agent saw signal without acting).
    """
    now = datetime.now()
    loop_state = _wm_read_loop_state()
    q = (loop_state.get("signals") or {}).get("quiescence") or {}
    snap = q.get("active_snapshot")
    if not snap:
        # No active quiescence — no-op.
        print(json.dumps({"outcome": "noop",
                          "reason": "no active quiescence snapshot"}))
        sys.exit(0)

    # Re-collect current blocked refs and index them by external_id
    current_refs = {}
    try:
        for e in _collect_blocked_entries():
            ref = e.get("blocker_ref")
            if isinstance(ref, dict) and ref.get("external_id"):
                current_refs[ref["external_id"]] = ref
    except _GoalSelectorUnavailable as e:
        # Fail-open (): cannot re-collect blocked refs to audit
        # drift. Emit a clean noop (exit 0) — verify-wake is advisory and
        # the orchestrator calls it unconditionally on post-sleep re-entry
        # (aspirations Phase -0.5e'); a crash here would break that path.
        print(json.dumps({"outcome": "noop",
                           "reason": f"goal-selector import failed: {e}"}))
        sys.exit(0)

    # Compare each snapshot ref against current
    drifted = []
    for ref in snap.get("refs", []) or []:
        ext_id = ref.get("external_id")
        if not ext_id:
            continue
        cur = current_refs.get(ext_id)
        if cur is None:
            # The blocker is GONE — that's legitimate unblock, not a miss.
            # (The goal is no longer blocked → the selector would pick it up
            # on the next iteration.)
            continue
        prior_hash = ref.get("state_hash")
        current_hash = cur.get("state_hash")
        if prior_hash is not None and current_hash is not None and prior_hash != current_hash:
            drifted.append({
                "external_id": ext_id,
                "type": ref.get("type"),
                "prior_hash": prior_hash,
                "current_hash": current_hash,
            })

    # Magic Wand 2: newly-arrived-work check. Mutates `drifted` in place if
    # N+ executable goals arrived during sleep. Fail-open on any error.
    _check_newly_arrived_work(snap, cfg, drifted)

    # Update sleep_total_s with the actual elapsed time since entry
    entered_at = snap.get("entered_at")
    actual_sleep_s = None
    if entered_at:
        try:
            _entered_dt = parse_naive_iso(entered_at)
            if _entered_dt is None:
                raise ValueError("unparseable entered_at")
            actual_sleep_s = int((now - _entered_dt).total_seconds())
        except (ValueError, TypeError):
            pass

    q_new = dict(q)
    q_new["sleep_total_s"] = int(q.get("sleep_total_s", 0) or 0) + (actual_sleep_s or 0)
    if drifted:
        q_new["misses"] = int(q.get("misses", 0) or 0) + 1
    else:
        q_new["misses"] = 0  # clean wake resets the counter
    q_new["active_snapshot"] = None  # consume snapshot

    # MW#3 (alpha session-60, 2026-05-07): track actual_sleep_s in a rolling
    # window so cmd_check can downgrade approval duration when sleeps are
    # consistently fragmented. Append the most recent actual_sleep_s and cap
    # at fragmented_history_max (default 10). None values (entered_at parse
    # failure) are skipped — they would corrupt the average.
    if actual_sleep_s is not None:
        history = list(q.get("actual_sleep_history") or [])
        history.append(int(actual_sleep_s))
        history_max = int(cfg.get("fragmented_history_max", 10))
        if len(history) > history_max:
            history = history[-history_max:]
        q_new["actual_sleep_history"] = history

    signals = dict(loop_state.get("signals") or {})
    signals["quiescence"] = q_new
    loop_state["signals"] = signals
    _wm_write_loop_state(loop_state)

    record = {
        "timestamp": now.isoformat(timespec="seconds"),
        "agent": os.environ.get("MIND_AGENT", "") or "unknown",
        "mode": "verify-wake",
        "outcome": "miss" if drifted else "clean",
        "drifted_count": len(drifted),
        "drifted": drifted,
        "actual_sleep_s": actual_sleep_s,
        "consecutive_misses": q_new["misses"],
    }
    if drifted:
        _append_log(cfg.get("audit_file", "quiescence-audit.jsonl"), record)

    print(json.dumps({
        "outcome": "miss" if drifted else "clean",
        "drifted": drifted,
        "actual_sleep_s": actual_sleep_s,
        "consecutive_misses": q_new["misses"],
        "auto_disabled": q_new["misses"] >= int(cfg.get("wake_miss_cooldown", 3)),
    }, ensure_ascii=False, default=str))
    sys.exit(2 if drifted else 0)


# --- Main --------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Quiescence gate — script-gated idle.")
    sub = p.add_subparsers(dest="mode", required=True)

    pc = sub.add_parser("check", help="Evaluate eligibility before B7 backoff")
    pc.add_argument("--all-blocked", action="store_true",
                    help="Caller asserts goal-selector returned zero executable goals.")

    pw = sub.add_parser("verify-wake",
                        help="Post-sleep audit of external signals against snapshot")

    args = p.parse_args()

    if AGENT_DIR is None:
        print(json.dumps({"error": "no agent bound (MIND_AGENT not set)",
                          "outcome": "error"}))
        sys.exit(3)

    try:
        cfg = _load_config()
    except Exception as e:
        print(json.dumps({"error": f"config load failed: {e}",
                          "outcome": "error"}))
        sys.exit(3)

    if args.mode == "check":
        cmd_check(args, cfg)
    elif args.mode == "verify-wake":
        cmd_verify_wake(args, cfg)


if __name__ == "__main__":
    main()
