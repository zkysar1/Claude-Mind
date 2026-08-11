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

# Sentinel `id` stamped onto the fire_when predicate so the failing gate can be
# resolved by IDENTITY rather than by predicate TYPE ().
#
# The prior test — `failed[0].type == fire_when.get("type")` — mislabels a
# failing PRECONDITION as `fire_when` whenever the two happen to share a
# predicate type (two `file_check`s, say). That was cosmetic while it only fed
# one stdout line, but  promoted it into the DURABLE
# `last_shelve_reason` field that guard-2197 sends readers to.
#
# Index-by-position is NOT a safe alternative here: `evaluate_all(...,
# include_skippable=False)` `continue`s past any predicate carrying
# `selector_skip`, so `results[i]` does not correspond to `pcs[i]`. Identity is
# the only resolution that survives that. All 8 predicate handlers populate
# `PredicateResult.predicate_id` from `p.get("id")` (verified), including the
# unknown-type and evaluator-error paths in `predicate.evaluate`, so the
# sentinel propagates on every branch.
FIRE_WHEN_PID = "__fire_when__"


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


def _update_goal_field(goal_id: str, source: str, field: str, value: str, dry_run: bool) -> bool:
    """Write one field on a goal record via aspirations.py update-goal.

    Generalized from `_advance_last_achieved_at` (g-005-28, 2026-07-31) because
    a shelve now writes THREE fields, not one — see the shelve-trace comment at
    the call site for why. The env plumbing below (guard-879) is the reason this
    is parameterized rather than duplicated per field.
    """
    if dry_run:
        return True
    cmd = [
        sys.executable, str(HERE / "aspirations.py"),
        "--source", source, "update-goal",
        goal_id, field, value,
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
                # Shallow COPY + sentinel id. The copy matters: `pcs` holds
                # references into the live goal record, and stamping an `id`
                # onto the original would mutate the goal's own fire_when.
                fw = dict(fire_when)
                fw["id"] = FIRE_WHEN_PID
                pcs.append(fw)
            if not pcs:
                continue  # no gates; nothing to short-circuit on
            results = evaluate_all(pcs, mode="fail_fast", include_skippable=False)
            failed = [r for r in results if not r.passed]
            if not failed:
                continue  # all gates pass; let the goal fire normally
            goal_id = g.get("id", "<unknown>")
            # Distinguish gate type in the log AND in the durable shelve trace
            # below. Resolved by IDENTITY — the FIRE_WHEN_PID sentinel stamped
            # onto the fire_when copy above — NOT by predicate type. The prior
            # `failed[0].type == fire_when.get("type")` test mislabels a failing
            # PRECONDITION as `fire_when` whenever the two merely SHARE a type
            # (two `file_check`s, say), and  promoted that label from a
            # transient stdout line into the durable `last_shelve_reason` field.
            # PredicateResult exposes `.predicate_id` and `.type` — not `.ptype`;
            # see core/scripts/predicate.py. (`.ptype` bug caught by
            # test_recurring_precondition_sweep_fire_when.py.)
            gate_kind = "fire_when" if failed[0].predicate_id == FIRE_WHEN_PID else "precondition"

            # ── SHELVE TRACE (, 2026-07-31) ────────────────────────────
            # Advancing lastAchievedAt is NOT an achievement — this sweep never
            # touches achievedCount. But the goal record afterwards is
            # INDISTINGUISHABLE from one that genuinely closed: lastAchievedAt is
            # fresh and `last_outcome_origin` still reads whatever the last REAL
            # close wrote (recurring-close.sh is its only writer), so a stale
            # "genuine" sits beside a fresh timestamp and reads as a recent genuine
            # close. Discriminating without these fields needs TWO readings of
            # achievedCount across time; a single read cannot do it. Measured on
            # : lastAchievedAt advanced ~40h (>=5 intervals) while
            # achievedCount sat at 91, and a goal addendum written off that record
            # inferred "roughly 11 genuine deep closes with zero evidence" from what
            # was actually zero closes. Same class as rb-245 / the frozen-numerator
            # trap — a field that MOVES read as proof of an event a different field
            # says did not happen.
            # Single-read discriminator this enables:
            #     lastAchievedAt == last_shelved_at  =>  shelved, not achieved
            #
            # WRITE ORDER IS LOAD-BEARING (). The trace is written FIRST
            # and lastAchievedAt LAST. These are three separate subprocess writes
            # with no transaction around them, so a crash — or a daemon failure on
            # write 2 of 3 — can land any PREFIX of them:
            #   trace-LAST (the original order): lastAchievedAt lands fresh while
            #     last_shelved_at still holds a PRIOR value, so the discriminator
            #     reads NOT-EQUAL => "genuine close". That is the exact false
            #     reading this trace exists to prevent, manufactured by the trace's
            #     own write order. Fail-DANGEROUS.
            #   trace-FIRST (this order): last_shelved_at lands fresh while
            #     lastAchievedAt still holds its OLD value, so the discriminator
            #     reads NOT-EQUAL => "not shelved this cycle" — which is TRUE, the
            #     advance never landed. Fail-SAFE.
            # Trace-write failure stays SOFT (logged, never counted as skipped):
            # resetting overdue_ratio is this sweep's actual contract, and a goal
            # left un-advanced because its trace could not be written would
            # re-inflate urgency every cycle — the very trap this script exists for.
            for _f, _v in (
                ("last_shelved_at", now_iso),
                ("last_shelve_reason", f"{gate_kind}:{failed[0].type}"),
            ):
                if not _update_goal_field(goal_id, source, _f, _v, args.dry_run):
                    sys.stderr.write(
                        f"recurring-precondition-sweep: shelve-trace {_f} "
                        f"failed for {goal_id} (advance still attempted)\n"
                    )
            if _update_goal_field(goal_id, source, "lastAchievedAt", now_iso, args.dry_run):
                advanced += 1
                print(
                    f"[precondition-sweep] {('DRY-RUN ' if args.dry_run else '')}"
                    f"advanced {goal_id} ({source}): lastAchievedAt -> {now_iso} "
                    f"(failing {gate_kind}: {failed[0].type}) [shelved, not achieved]"
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
