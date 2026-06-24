"""loop-state-merge-gate — refuse subdict-clobber writes to working memory loop_state.signals.

Origin: rb-715 (verify-real cluster Candidate 1, bravo session-61 g-275-02).

Failure mode this gate prevents
================================

The aspirations loop's `loop_state.signals` dict is a multi-writer subdict:

  - Bash gates (`recurring-loop-state-mutate.py`, `tree-encoding-drift-gate.py`,
    `quiescence-gate.py`) write subkeys IN-FLIGHT during an iteration.
  - The LLM also writes `loop_state` at LOOP_CONTINUE prep, after applying
    its own counter mutations.

When the LLM uses the OLD pattern of `merged.signals = session_signals`
(whole-subdict overwrite from a stale snapshot held since the start of
the iteration), it silently destroys concurrent bash writes to
`signals.quiescence`, `signals.goals_since_last_tree_update`,
`signals.routine_streak_global`, etc. The lesson is encoded as rb-715
("Read-merge-write subdict clobber") with no automated guard until now.

This gate fires at write time. When the incoming `loop_state` value is a
dict and contains a `signals` subdict that does NOT include subkeys
present in the on-disk version, the gate refuses the write.

The correct LLM pattern (read-merge-write WITHIN the same iteration-close
flow) preserves all on-disk subkeys and never trips the gate. The gate is
therefore a no-op for compliant callers — it only catches the rb-715
anti-pattern where stale snapshot loses concurrent writes.

Monotonic-counter preservation (g-115-1418)
===========================================

The gate has a second, orthogonal job: floor the three monotonic top-level
loop_state counters — goals_completed, productive_goals (max), and
counted_goals_this_session (union) — to the on-disk committed values on every
write. rb-715 above guards DROPPED `signals` subkeys; this guards LOWERED
top-level counters. A collateral full-slot writer (tree-encoding-drift-gate.py,
stale-sentinel-canary.py, productivity-stop-gate.sh, or the daemon full-slot
set) that captured loop_state BEFORE a peer's counter bump keeps every signals
subkey (so rb-715 passes it through) yet carries stale-lower counters; under
the guard-685 stale-lock-steal race that write reverts the committed bump
(g-115-1411 Mechanism C). _fileops.loop_state_cas_retry never engages for these
writers — they are not the 3 delta-writers. So the gate backstops them:
check() returns `preserved_value` (the incoming value with counters floored),
`counters_preserved`, and `preserved_counters`; callers (wm.py cmd_set, daemon
set_slot) write preserved_value instead of the raw incoming. Skipped on
override (intentional reset) and first-write (on-disk not a dict; the session
boundary clears loop_state to None first, handled by the None pass-through).
Within a session these three counters never legitimately decrease. See tree
node loop-state-integrity.md Mechanism C.

Override
========

`--override-merge-gate "<justification>"` (passed via wm.py CLI) bypasses
the gate. Use ONLY for genuine wholesale signal replacement (session
boundary, explicit reset). Each override is appended to
`world/loop-state-merge-overrides.jsonl` for audit. The LLM should never
need this in normal iteration flow.

Invocation
==========

Usage from wm.py cmd_set:
    from loop_state_merge_gate import check
    result = check(incoming_value, on_disk_loop_state, override="…")
    if result["would_block"]:
        ...

Usage as standalone CLI (manual probe / unit test):
    echo '<incoming-loop_state-json>' | py -3 loop-state-merge-gate.py
    Exit 0: pass.  Exit 1: would block (reason on stderr).

Cross-references
================

- rb-715 (reasoning bank entry — the original incident)
- world/conventions/deploy-state-verification.md (sibling structural guard)
- bravo/reports/verify-real-cluster-catalog-2026-05-07.md Candidate 1
- core/scripts/wm.py cmd_set (the call site)
"""

from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ── 8: monotonic loop_state counters (Mechanism C backstop) ──
# These three top-level loop_state fields only ever advance within a session
# (goals_completed / productive_goals increment; counted_goals_this_session
# appends). A full-slot write carrying stale-lower values must never lower the
# on-disk committed state. Scope is deliberately these three (named in
# guard-746 + the 1 live repro); evolutions / touched are monotonic
# too but out of this goal's scope — add here only if a future incident shows
# them reverted (guard-416: forward-looking ballast is still ballast).
_MONOTONIC_MAX_FIELDS = ("goals_completed", "productive_goals")
_MONOTONIC_UNION_FIELDS = ("counted_goals_this_session",)


def _as_int_or_none(v) -> Optional[int]:
    """Coerce a counter to int for max-comparison; None when not numeric.

    Mirrors loop-state-bump-counters.py::_to_int (a list coerces to its length)
    so the gate and the delta-writer agree on what 'higher' means."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, list):
        return len(v)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            return None
    return None


def _preserve_monotonic_counters(incoming: dict, on_disk: dict):
    """Return (preserved_value, changed_audit).

    preserved_value is a copy of `incoming` with each monotonic counter floored
    to the on-disk value (max) and counted_goals_this_session unioned with the
    on-disk list (committed order first, then incoming-only items). Returns the
    UNCHANGED `incoming` object and an empty audit dict when nothing needed
    preserving — so the caller can cheaply detect the no-op case. Never mutates
    its inputs (lazy deep-copy only when a change is required)."""
    changed: dict = {}
    preserved = None  # lazily deep-copied only when a change is needed

    def _copy() -> dict:
        nonlocal preserved
        if preserved is None:
            preserved = copy.deepcopy(incoming)
        return preserved

    for f in _MONOTONIC_MAX_FIELDS:
        on_disk_v = _as_int_or_none(on_disk.get(f))
        if on_disk_v is None:
            continue
        inc_v = _as_int_or_none(incoming.get(f))
        if inc_v is None or on_disk_v > inc_v:
            _copy()[f] = on_disk_v
            changed[f] = {"incoming": inc_v, "on_disk": on_disk_v, "kept": on_disk_v}

    for f in _MONOTONIC_UNION_FIELDS:
        on_disk_list = on_disk.get(f)
        if not isinstance(on_disk_list, list):
            continue
        inc_list = incoming.get(f) if isinstance(incoming.get(f), list) else []
        # Append on-disk goal_ids missing from incoming (a stale subset). Keep
        # incoming order, append committed-only items — so a mere reorder (same
        # membership) is NOT a change; only genuine drops trigger preservation.
        added = [x for x in on_disk_list if x not in inc_list]
        if added:
            _copy()[f] = inc_list + added
            changed[f] = {
                "incoming_len": len(inc_list),
                "on_disk_len": len(on_disk_list),
                "kept_len": len(inc_list) + len(added),
            }

    return (preserved if preserved is not None else incoming), changed


def _audit_ledger_path() -> Optional[Path]:
    """Locate world/loop-state-merge-overrides.jsonl. Fail-open: returns None if WORLD_DIR cannot resolve.

    Resolution priority:
      1. _paths.WORLD_DIR (canonical — same source wm.py uses)
      2. os.environ['WORLD_DIR'] (fallback for direct invocations)
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _paths import WORLD_DIR  # type: ignore
        if WORLD_DIR:
            return Path(WORLD_DIR) / "loop-state-merge-overrides.jsonl"
    except (ImportError, AttributeError):
        pass
    world_dir = os.environ.get("WORLD_DIR") or os.environ.get("MIND_WORLD_DIR")
    if world_dir:
        return Path(world_dir) / "loop-state-merge-overrides.jsonl"
    return None


def check(
    incoming_value: Any,
    on_disk_loop_state: Any,
    override: Optional[str] = None,
) -> dict:
    """Decide whether to block the loop_state write.

    Returns a dict:
        would_block: bool — True iff the write should be refused
        reason: str — human-readable explanation
        missing_subkeys: list[str] — on-disk signals subkeys absent from incoming
        override_applied: bool — True iff override bypassed a block
        preserved_value: Any — what the caller should WRITE (incoming with
            stale-lower monotonic counters floored to on-disk + counted_goals
            unioned; the unchanged incoming when nothing needed preserving)
        counters_preserved: bool — True iff preserved_value differs from incoming
        preserved_counters: dict — audit of which counters were floored (g-115-1418)

    Pass-through cases (would_block=False):
      - incoming_value is None (clear) or non-dict
      - incoming_value['signals'] is missing or non-dict
      - on_disk_loop_state is None or non-dict
      - on_disk_loop_state['signals'] is missing or non-dict
      - All on-disk signals subkeys are present in incoming.signals

    Block case (would_block=True):
      - On-disk signals has subkeys absent from incoming.signals AND no override
    """
    result = {
        "would_block": False,
        "reason": "",
        "missing_subkeys": [],
        "override_applied": False,
        # 8 monotonic-counter preservation (Mechanism C backstop).
        # preserved_value is what the caller should WRITE (the incoming value
        # with stale-lower top-level counters floored to on-disk + counted_goals
        # unioned). Defaults to the incoming value unchanged; counters_preserved
        # flags whether any flooring happened (preserved_counters audits it).
        "preserved_value": incoming_value,
        "counters_preserved": False,
        "preserved_counters": {},
    }

    # Pass-through: None is a legitimate clear (e.g., consolidation end-of-session).
    if incoming_value is None:
        result["reason"] = "incoming value is None — legitimate clear, pass through"
        return result

    # : refuse type-transition. When on-disk is already a dict and
    # incoming is non-dict, this is never a legitimate write — the slot's
    # structural shape is invariant once established. Catches the residual
    # corruption path if the writer-side guard in wm.py STRUCTURED_DICT_SLOTS
    # is bypassed (direct write via _fileops, broken shim, etc.). Note that
    # `loop_state` is the only caller of this gate today, so on-disk-dict
    # means the slot already has a valid structured value.
    if not isinstance(incoming_value, dict):
        if isinstance(on_disk_loop_state, dict):
            shape = type(incoming_value).__name__
            result["would_block"] = True
            result["reason"] = (
                f"type-transition refused: on-disk loop_state is dict but incoming is {shape} "
                "(structured-shape slot, type-transition is never legitimate)"
            )
            return result
        # On-disk is not a dict either — no structural state to protect, pass through.
        result["reason"] = "incoming value is not a dict and on-disk is not a dict — pass through"
        return result

    # ── Monotonic-counter preservation (8, Mechanism C backstop) ──
    # rb-715 (below) guards DROPPED signals subkeys; it does NOT guard LOWERED
    # top-level counters. A collateral full-slot writer that captured loop_state
    # before a peer's bump carries stale-lower goals_completed/productive_goals
    # and a subset counted_goals_this_session, yet keeps every signals subkey —
    # so rb-715 passes it through and (under the guard-685 stale-lock-steal race)
    # it REVERTS the committed bump (Mechanism C). Floor the counters here, on
    # every write path. The caller writes result["preserved_value"] when
    # counters_preserved is True. Computed BEFORE the signals checks below so it
    # applies even when incoming has no signals dict. Skipped on override
    # (intentional reset) and when on-disk is not a dict (first write / post-clear
    # re-init — the session boundary clears loop_state to None first, handled by
    # the None pass-through above). Within a session these counters never
    # legitimately decrease.
    if isinstance(on_disk_loop_state, dict) and not override:
        preserved_val, changed = _preserve_monotonic_counters(
            incoming_value, on_disk_loop_state
        )
        if changed:
            result["preserved_value"] = preserved_val
            result["counters_preserved"] = True
            result["preserved_counters"] = changed

    incoming_signals = incoming_value.get("signals")
    if not isinstance(incoming_signals, dict):
        result["reason"] = "incoming.signals is not a dict — pass through"
        return result

    # Pass-through: on-disk has nothing to clobber
    if not isinstance(on_disk_loop_state, dict):
        result["reason"] = "on-disk loop_state is not a dict — pass through (first write)"
        return result

    on_disk_signals = on_disk_loop_state.get("signals")
    if not isinstance(on_disk_signals, dict):
        result["reason"] = "on-disk signals is not a dict — pass through"
        return result

    # The actual check: which on-disk subkeys are missing from incoming?
    missing = sorted(k for k in on_disk_signals.keys() if k not in incoming_signals)
    result["missing_subkeys"] = missing

    if not missing:
        result["reason"] = "all on-disk signals subkeys preserved — pass"
        return result

    # Block conditions met. Check override.
    if override:
        result["override_applied"] = True
        result["reason"] = (
            f"BLOCK SUPPRESSED by --override-merge-gate: {override} "
            f"(missing subkeys: {missing})"
        )
        # Audit ledger append
        ledger_path = _audit_ledger_path()
        if ledger_path:
            try:
                ledger_path.parent.mkdir(parents=True, exist_ok=True)
                entry = {
                    "ts": _now_iso(),
                    "missing_subkeys": missing,
                    "justification": override,
                    "incoming_signals_keys": sorted(incoming_signals.keys()),
                    "on_disk_signals_keys": sorted(on_disk_signals.keys()),
                }
                with open(ledger_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            except OSError:
                # Fail-open on ledger write — don't block on logging failure
                pass
        return result

    # Block — no override
    result["would_block"] = True
    result["reason"] = (
        f"rb-715 subdict-clobber: incoming loop_state.signals is missing "
        f"on-disk subkeys {missing}. The on-disk version was likely written "
        f"by a bash gate (recurring-loop-state-mutate.py, "
        f"tree-encoding-drift-gate.py, quiescence-gate.py) AFTER the LLM read "
        f"loop_state at the start of the iteration. The wholesale signals "
        f"overwrite would silently destroy that bash write. Refuse the write "
        f"and demand surgical per-key updates. To intentionally clear these "
        f"subkeys (session boundary, etc.), pass "
        f'--override-merge-gate "<justification>" to wm-set.sh.'
    )
    return result


def _read_current_wm_loop_state() -> Any:
    """Read the current loop_state from working memory. Used by the standalone CLI."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from wm import read_wm  # type: ignore
    except ImportError:
        return None
    data = read_wm() or {}
    slots = data.get("slots") or {}
    return slots.get("loop_state")


def _main() -> int:
    """Standalone CLI: read incoming loop_state JSON from stdin, compare against current WM."""
    import argparse

    parser = argparse.ArgumentParser(description="rb-715 loop_state subdict-clobber gate")
    parser.add_argument(
        "--override-merge-gate",
        default=None,
        help="Justification string. Bypasses block; appends to world/loop-state-merge-overrides.jsonl.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only exit code; suppress reason on stderr.",
    )
    args = parser.parse_args()

    raw = sys.stdin.read().strip()
    if not raw:
        print("ERROR: No input on stdin", file=sys.stderr)
        return 2
    try:
        incoming = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON on stdin: {e}", file=sys.stderr)
        return 2

    on_disk = _read_current_wm_loop_state()
    result = check(incoming, on_disk, override=args.override_merge_gate)
    if not args.quiet:
        print(json.dumps(result), file=sys.stderr)
    return 1 if result["would_block"] else 0


if __name__ == "__main__":
    sys.exit(_main())
