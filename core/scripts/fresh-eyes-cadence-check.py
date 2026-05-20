#!/usr/bin/env python3
"""Cadence gate for /fresh-eyes-review (and its siblings program/tree).

Exits 0 when the ritual should fire (goal-count since last fire has reached
`goal_cadence`). Exits 1 on noop or error (fail-open — must not block the loop).

Invoked from aspirations-precheck Phase 0.5e and from /fresh-eyes-review
Phase 1 (cadence gate).

# Cadence is purely goal-count-based. No pending-question gate.
# (Gate removed 2026-05-19: rituals no longer file pending-questions, and
# stale entries left by the old design permanently noop'd the cadence.)

Flags:
    --verbose         Print counter breakdown and exit with fire/noop code.
    --print-current   Print just the current completed-goals count and exit 0.
                      Used by the skill's Phase 8 "record the tick" step so
                      it never has to parse human-readable output.
    --config-block    Block name in aspirations.yaml to read goal_cadence /
                      wm_slot / first_fire_offset from. Default fresh_eyes_review.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import _paths  # noqa: E402
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

CONFIG_PATH = _paths.CONFIG_DIR / "aspirations.yaml"
# Default config block for backward compat when invoked with no flags. Sibling
# rituals MUST pass --config-block explicitly. The slot name is read from the
# block's `wm_slot` field — no fallback — so (block, slot) are single-source-of-
# truth in YAML and a typo fails loud instead of silently reusing the review
# ritual's slot.
DEFAULT_CONFIG_BLOCK = "fresh_eyes_review"


def _load_yaml(path: Path):
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(
            f"fresh-eyes-cadence-check: WARN: yaml parse failed for {path}: {e}",
            file=sys.stderr,
        )
        return None


def count_completed_goals() -> int:
    """Sum of status=='completed' goals across world+agent aspirations & archive."""
    agent = os.environ.get("MIND_AGENT", "")
    candidates = [
        _paths.WORLD_DIR / "aspirations.jsonl",
        _paths.WORLD_DIR / "aspirations-archive.jsonl",
    ]
    if agent:
        candidates.extend([
            _paths.agent_dir(agent) / "aspirations.jsonl",
            _paths.agent_dir(agent) / "aspirations-archive.jsonl",
        ])
    total = 0
    for p in candidates:
        if not p.exists():
            continue
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        asp = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for g in asp.get("goals", []):
                        if g.get("status") == "completed":
                            total += 1
        except OSError:
            continue
    return total


def wm_slot_value(slot_name: str):
    """Read `slot_name` via daemon. as_json=True preserves --json semantics
    (wm_read returns the same JSON text the deleted wm.py CLI printed).
    Do NOT remove as_json — wm.py's default was YAML, and json.loads on YAML
    silently fails to None (spent an hour debugging this during smoke test)."""
    try:
        raw = _rt.wm_read(slot=slot_name, as_json=True)
    except _rt.RtError:
        return None
    raw = (raw or "").strip()
    if not raw or raw == "null":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument(
        "--print-current",
        action="store_true",
        help="Print current completed-goals count and exit 0 (skill Phase 8 uses this).",
    )
    ap.add_argument(
        "--config-block",
        default=DEFAULT_CONFIG_BLOCK,
        help=(
            "Config block name under aspirations.yaml to read for this ritual's "
            "goal_cadence / wm_slot / first_fire_offset fields. Default: "
            f"{DEFAULT_CONFIG_BLOCK} (fresh-eyes-review). Use `fresh_eyes_program` "
            "or `fresh_eyes_tree` for sibling rituals."
        ),
    )
    args = ap.parse_args()

    # --print-current is a side-channel readout for the skill's tick-recording
    # step. It bypasses the cadence gate and always exits 0 so callers can use
    # `$(... --print-current)` without worrying about the cadence branch.
    if args.print_current:
        print(count_completed_goals())
        return 0

    cfg = _load_yaml(CONFIG_PATH)
    if cfg is None:
        print("fresh-eyes-cadence-check: config read failed — noop", file=sys.stderr)
        return 1  # fail-open — cadence gate MUST NOT block the loop
    # LOAD-BEARING: named block must exist and must declare wm_slot. A missing
    # block (typo in --config-block) or missing wm_slot would otherwise silently
    # read the review ritual's slot on the sibling ritual's cadence, causing
    # cross-ritual drift. Stderr explains the fix; exit 1 still fail-opens the
    # precheck so the loop itself is never blocked.
    if args.config_block not in cfg or not isinstance(cfg[args.config_block], dict):
        print(
            f"fresh-eyes-cadence-check: config block '{args.config_block}' not found "
            f"in aspirations.yaml (typo? new ritual missing block?) — noop",
            file=sys.stderr,
        )
        return 1
    fe = cfg[args.config_block]
    slot_name = fe.get("wm_slot")
    if not slot_name:
        print(
            f"fresh-eyes-cadence-check: config block '{args.config_block}' missing "
            f"required 'wm_slot' field — noop",
            file=sys.stderr,
        )
        return 1
    slot_name = str(slot_name)
    goal_cadence = int(fe.get("goal_cadence", 25))

    # Goal-count cadence (the single source of truth)
    current = count_completed_goals()
    last = wm_slot_value(slot_name) or {}
    last_count = int(last.get("goals_count_at_last_fire", 0) or 0)

    # Seed-stagger fix (): when the WM slot is unset (last_count==0) AND
    # `first_fire_offset` is configured for this ritual, write a staggered seed
    # to the slot in-place and return noop this iteration. Next iteration reads
    # the seeded slot via normal cadence math.
    #
    # Rationale: with three rituals sharing the unseeded-slot first-fire path
    # (review @ 25, felt-sense @ 75, program @ 100), all three would fire on
    # the same iteration in any world where current_goal_count >= 100 (i.e.,
    # essentially every fresh agent dir / migration / slot-wipe scenario in a
    # mature world). The  cap below prevents "infinitely overdue"
    # narration but does NOT prevent simultaneous triple-fire.
    #
    # Per-ritual offsets in aspirations.yaml stagger first-fires across the
    # next ~5/15/30 goals (review/felt-sense/program), so the three rituals
    # land on different iterations after a fresh seed event.
    #
    # Backward-compat: when `first_fire_offset` is missing/0, this branch is
    # skipped and the original  cap behavior is preserved (immediate
    # fire on first detection of an unseeded slot).
    first_fire_offset = int(fe.get("first_fire_offset", 0) or 0)
    if last_count == 0 and first_fire_offset > 0 and current >= goal_cadence:
        seed_count = current - goal_cadence + first_fire_offset
        seed_payload = json.dumps({
            "timestamp": "0000-00-00T00:00:00",  # sentinel: ritual never fired
            "goals_count_at_last_fire": seed_count,
            "seeded_for_stagger": True,
            "seeded_offset": first_fire_offset,
        })
        try:
            subprocess.run(
                [sys.executable, str(HERE / "wm.py"), "set", slot_name],
                input=seed_payload,
                capture_output=True,
                text=True,
                check=True,
            )
            print(
                f"fresh-eyes-cadence-check: seeded {slot_name} for stagger "
                f"(offset={first_fire_offset}, seed_count={seed_count}, "
                f"current={current}, cadence={goal_cadence}) — noop this iter"
            )
            return 1  # noop — seed written, next iter reads it normally
        except (subprocess.CalledProcessError, OSError) as exc:
            # Seed write failed — fall through to legacy cap behavior so the
            # ritual still fires (better than never firing on a transient
            # write failure).
            print(
                f"fresh-eyes-cadence-check: seed write to {slot_name} failed "
                f"({exc!r}) — falling through to legacy first-fire cap",
                file=sys.stderr,
            )

    diff = current - last_count
    # First-fire normalization (): when the WM slot is unset (last_count==0)
    # but the world has accumulated history, raw diff equals the full goal count
    # (e.g., 2132 vs cadence 25), making the ritual present as infinitely-overdue.
    # Cap at cadence so first-fire reads as "exactly due" — fires once
    # deterministically, doesn't trigger emergency narration.
    #
    # Reached when first_fire_offset is unset/0 (legacy behavior) OR when the
    # seed-write above failed.
    if last_count == 0:
        diff = min(diff, goal_cadence)
    if args.verbose:
        print(
            f"fresh-eyes-cadence-check: block={args.config_block} slot={slot_name} "
            f"current={current} last={last_count} diff={diff} cadence={goal_cadence}"
        )
    if diff >= goal_cadence:
        if not args.verbose:
            print(
                f"fresh-eyes-cadence-check: fire (block={args.config_block}, "
                f"current={current}, last={last_count}, diff={diff}, cadence={goal_cadence})"
            )
        return 0
    if not args.verbose:
        print(
            f"fresh-eyes-cadence-check: noop (block={args.config_block}, "
            f"diff={diff} < cadence={goal_cadence})"
        )
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # Fail-open outer guard — ANY unexpected error must exit 1 (noop), not
        # propagate. The cadence gate is fire-and-forget; breaking it would
        # silently disable the loop's precheck phase.
        print(f"fresh-eyes-cadence-check: unexpected error: {exc} — noop", file=sys.stderr)
        sys.exit(1)
