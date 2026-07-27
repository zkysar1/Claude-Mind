#!/usr/bin/env python3
"""Advance lastAchievedAt on recurring goals whose preconditions fail.

Part 1 of the cargo-cult calibration fix (2026-04-21). Fixes the
"shape-recurring trap": a recurring goal with a structured precondition that
consistently fails (e.g., "at least one unreflected hypothesis exists") never
reaches aspirations-execute, so `lastAchievedAt` never advances. The goal-
selector's urgency formula then inflates `overdue_ratio = (elapsed - interval)
/ interval` unboundedly. When the precondition finally unlocks, the goal
fires with massive urgency on trivially-met evidence, closes routine, and
feeds cargo-cult.

Fix: every iteration, sweep every recurring goal past its time gate.
Evaluate its structured preconditions. If any fail, advance `lastAchievedAt`
to now so overdue_ratio resets. The goal still waits the full interval
before re-evaluation; if the precondition is met by then, it fires normally.

This script is unconditional — no feature flag. It's a data-layer cleanup
that pairs with the cargo_cult auto-extend feature (both land 2026-04-21).

Does NOT increment consecutive_routine: the goal was never actually closed,
only shelved by the precondition filter.

Usage:
    py core/scripts/recurring-precondition-sweep.py [--dry-run]

Exits 0 always (fail-open). Warnings to stderr.
Invoked from aspirations-precheck Phase 0 before goal selection.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import _paths  # noqa: E402
from predicate import evaluate_all  # noqa: E402


def _hours_since(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_ts))
    except (ValueError, TypeError):
        return None
    delta = datetime.now() - dt
    return delta.total_seconds() / 3600.0


def _source_paths():
    """Yield (source_name, path) for world and (if agent bound) agent queues."""
    yield "world", _paths.WORLD_DIR / "aspirations.jsonl"
    agent = os.environ.get("MIND_AGENT", "").strip()
    if agent:
        yield "agent", _paths.agent_dir(agent) / "aspirations.jsonl"


def _iter_recurring_past_gate(src_path: Path):
    """Yield goals that are recurring AND past their time gate."""
    if not src_path.exists():
        return
    with src_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                asp = json.loads(line)
            except json.JSONDecodeError:
                continue
            for g in asp.get("goals", []):
                if not g.get("recurring"):
                    continue
                if g.get("status") in ("completed", "skipped", "expired", "in-progress"):
                    continue
                interval = g.get("interval_hours")
                if interval is None or interval <= 0:
                    continue
                la = g.get("lastAchievedAt")
                # No lastAchievedAt yet → treat as overdue (pending first fire).
                # Time gate passes; let precondition evaluation decide.
                if la is not None:
                    elapsed = _hours_since(la)
                    if elapsed is None or elapsed < float(interval):
                        continue
                yield g


def _advance_last_achieved_at(goal_id: str, source: str, now_iso: str, dry_run: bool) -> bool:
    if dry_run:
        return True
    cmd = [
        sys.executable, str(HERE / "aspirations.py"),
        "--source", source, "update-goal",
        goal_id, "lastAchievedAt", now_iso,
    ]
    # guard-879: aspirations.py's own-cloud write-lock resolves its governed
    # root map from MIND_WORLD/MIND_META (or the *_PATH fallbacks) in the
    # SUBPROCESS env ONLY (OwnCloudBackend._resolve_root_map). This script runs
    # as a direct `py` call (aspirations-precheck Phase 0.5c) with NO _paths.sh
    # preamble, so on env-only own-cloud hosts the inherited env lacks those
    # vars and the update-goal WRITE aborts BEFORE the lock ("cannot map a
    # governed path to a root"; ) — advanced=0 skipped_on_error=1,
    # silently, every iteration a goal actually needs advancing. The READ side
    # above already resolved WORLD_DIR/META_DIR via _paths.py's
    # .mind-data/local-paths.conf fallback, so propagate them under the names
    # from_env reads. Let an already-set MIND_* win (guard-879 / guard-652);
    # skip a None-able root (guard-551). Local backend ignores all four.
    env = os.environ.copy()
    # Fill each alias on FALSY (unset OR empty-string), not merely-missing:
    # from_env's `or` treats "" as unset (owncloud_backend.py:427), and guard-879's
    # idiom is ${MIND_WORLD:-$WORLD_DIR} — so an ambient MIND_WORLD="" must still be
    # filled (setdefault would leave it empty and the child would still abort). An
    # already-set TRUTHY value wins (guard-879/652); a None-able root is skipped
    # (guard-551).
    for key, root in (
        ("MIND_WORLD", _paths.WORLD_DIR), ("WORLD_PATH", _paths.WORLD_DIR),
        ("MIND_META", _paths.META_DIR), ("META_PATH", _paths.META_DIR),
    ):
        if root is not None and not env.get(key):
            # as_posix(), not str(): the value crosses into env consumed by
            # Path() in the child AND by ${MIND_WORLD:-...} shell idioms —
            # forward slashes are the conf/env convention on every platform
            # (local-paths.conf, _transplant_pack.py). On POSIX identical to
            # str(); on Windows avoids backslash-escape hazards in shell.
            env[key] = root.as_posix()
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        sys.stderr.write(
            f"recurring-precondition-sweep: update-goal failed for {goal_id}: {r.stderr}"
        )
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be advanced; do not write.")
    args = ap.parse_args()

    advanced = 0
    skipped = 0
    now_iso = datetime.now().replace(microsecond=0).isoformat()

    for source, path in _source_paths():
        for g in _iter_recurring_past_gate(path):
            # Gather structured gates: verification.preconditions + fire_when
            # (Magic Wand #4, alpha session-60). fire_when is a single
            # structured precondition specific to recurring goals — sugar
            # for "fire only when this upstream signal is present." Same
            # evaluator (predicate.evaluate_all) so any predicate type works.
            pcs = [p for p in (g.get("verification", {}).get("preconditions") or [])
                   if isinstance(p, dict) and "type" in p]
            fire_when = g.get("fire_when")
            if isinstance(fire_when, dict) and "type" in fire_when:
                pcs.append(fire_when)
            if not pcs:
                continue  # no gates; nothing to short-circuit on
            results = evaluate_all(pcs, mode="fail_fast", include_skippable=False)
            failed = [r for r in results if not r.passed]
            if not failed:
                continue  # all gates pass; let the goal fire normally
            goal_id = g.get("id", "<unknown>")
            if _advance_last_achieved_at(goal_id, source, now_iso, args.dry_run):
                advanced += 1
                # Distinguish gate type in the log so the user can tell
                # whether the failure was a precondition or a fire_when.
                # PredicateResult exposes its predicate type as `.type`,
                # not `.ptype` — see core/scripts/predicate.py PredicateResult
                # dataclass. Bug caught by test_recurring_precondition_sweep_fire_when.py.
                gate_kind = (
                    "fire_when" if isinstance(fire_when, dict) and failed[0].type == fire_when.get("type")
                    else "precondition"
                )
                print(
                    f"[precondition-sweep] {('DRY-RUN ' if args.dry_run else '')}"
                    f"advanced {goal_id} ({source}): lastAchievedAt -> {now_iso} "
                    f"(failing {gate_kind}: {failed[0].type})"
                )
            else:
                skipped += 1

    if advanced == 0 and skipped == 0:
        print("[precondition-sweep] no recurring goals with failing preconditions past their time gate")
    else:
        print(f"[precondition-sweep] advanced={advanced} skipped_on_error={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
