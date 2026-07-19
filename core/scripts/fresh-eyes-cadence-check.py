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
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import _paths  # noqa: E402
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)
from _goal_census import census_completed  # noqa: E402  (B9-deep evicted-goal counts)

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


def count_completed_goals(world_only: bool = False) -> int:
    """Sum of status=='completed' goals across world+agent aspirations & archive.

    world_only=True skips the agent-queue files. Used by the team-aware gate
    (g-115-1388): agent-inclusive counts differ per agent (each agent's own
    queue history is private), so cross-agent comparisons against the shared
    team stamp MUST use world-only counts on both sides."""
    agent = os.environ.get("MIND_AGENT", "")
    candidates = [
        _paths.WORLD_DIR / "aspirations.jsonl",
        _paths.WORLD_DIR / "aspirations-archive.jsonl",
    ]
    if agent and not world_only:
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
                    # B9-deep: evicted completed goals live only in the per-status
                    # census now (removed from the goals list) — fold them back so
                    # the cadence count is eviction-invariant.
                    total += census_completed(asp)
        except OSError:
            continue
    return total


def team_stamp_value(slot_name: str):
    """Read the shared team-fire stamp for `slot_name` from
    world/team-state.yaml → shared_cadences.<slot_name> (g-115-1388).

    Returns the stamp dict or None. Direct YAML file read (not the daemon):
    the gate runs inside the precheck hot-ish path and must stay fail-open
    under daemon flakiness — any read/parse error returns None, which the
    caller treats as 'no team stamp; per-agent behavior'."""
    doc = _load_yaml(_paths.WORLD_DIR / "team-state.yaml")
    if not isinstance(doc, dict):
        return None
    stamps = doc.get("shared_cadences")
    if not isinstance(stamps, dict):
        return None
    stamp = stamps.get(slot_name)
    return stamp if isinstance(stamp, dict) else None


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


# Report-timestamp filename pattern shared by all three fresh-eyes rituals:
# `<prefix>-YYYY-MM-DDThh-mm-ss.md` (the skill writes the time component with
# hyphens for Windows-filesystem safety). We read the timestamp from the
# FILENAME rather than the file mtime: the filename is written once and never
# mutated, so it is immune to the background-writer / git-checkout mtime-refresh
# failure mode rb-190 warns about ("any monitoring system that uses file mtime
# as a liveness signal is vulnerable to background writers — check content, not
# timestamp"). The trailing-timestamp shape is identical across review / program
# / tree reports, so one regex covers all three.
_REPORT_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})\.md$")


def _parse_iso_epoch(iso) -> float | None:
    """Parse an ISO 'YYYY-MM-DDThh:mm:ss' string to a local-time epoch float.
    Returns None for a missing/empty/sentinel/unparseable value (e.g. the
    seed-stagger '0000-00-00T00:00:00' marker) so callers treat 'no real prior
    stamp' as 'do not reconcile — fire normally'."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _report_filename_epoch(filename: str) -> float | None:
    """Extract the embedded timestamp from a fresh-eyes report filename and
    return it as a local-time epoch float, or None when the name does not carry
    the expected trailing `...THH-MM-SS.md` shape."""
    m = _REPORT_TS_RE.search(filename)
    if not m:
        return None
    date_part, hh, mm, ss = m.groups()
    return _parse_iso_epoch(f"{date_part}T{hh}:{mm}:{ss}")


def newest_reconcilable_report(reports_dir: Path, report_glob: str, slot_epoch: float):
    """8 auto-reconcile detector. Return (Path, iso_ts) for the newest
    report under `reports_dir` matching `report_glob` whose FILENAME timestamp is
    strictly newer than `slot_epoch` (the last cadence stamp). Return None when no
    such report exists — i.e. the most recent review is already accounted for by
    the slot, so the cadence should fire normally.

    The 'newer-than-stamp report exists' condition means a fresh-eyes ritual wrote
    its Phase-4 archive but its Phase-8 stamp was lost (autocompact between the two
    steps). Reconciling re-stamps instead of wastefully re-running the whole ritual
    whose briefing already exists on disk."""
    newest_epoch = slot_epoch  # only files strictly newer than the stamp qualify
    newest = None
    try:
        for p in reports_dir.glob(report_glob):
            fe_epoch = _report_filename_epoch(p.name)
            if fe_epoch is None:
                continue  # unrecognized name shape — skip (do not reconcile on it)
            if fe_epoch > newest_epoch:
                newest_epoch = fe_epoch
                newest = p
    except OSError:
        return None
    if newest is None:
        return None
    iso_ts = datetime.fromtimestamp(newest_epoch).strftime("%Y-%m-%dT%H:%M:%S")
    return newest, iso_ts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument(
        "--print-current",
        action="store_true",
        help="Print current completed-goals count and exit 0 (skill Phase 8 uses this).",
    )
    ap.add_argument(
        "--world-only",
        action="store_true",
        help=(
            "With --print-current: print the WORLD-ONLY completed count (skip "
            "agent queues). record-tick uses this for the team-aware shared "
            "stamp — cross-agent comparable units (g-115-1388)."
        ),
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
        print(count_completed_goals(world_only=args.world_only))
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
    # Defensive type-guard (2, mirror of l1-skew-check.py:307): a
    # legacy/restored WM slot may hold a bare timestamp string (the
    # pre-dict-migration shape) instead of the {goals_count_at_last_fire: N}
    # dict the recorder writes. Without this, last.get() raises AttributeError
    # at read time -- and because the read crash precedes the dict-write, the
    # slot can never self-heal (self-heal deadlock, rb-2482). Coercing a
    # non-dict to {} routes through the first-fire seed path, which rewrites
    # the correct dict shape.
    if not isinstance(last, dict):
        last = {}
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
    # Negative-diff self-heal (6): a DOWNWARD count-basis correction
    # (census double-count repair, store surgery, count-basis change) leaves
    # the stamped slot ABOVE the live count. Without this branch, diff stays
    # negative and the ritual silently starves until the count regrows past
    # the stale stamp (~months for a large repair — e.g. the  census
    # carries +872 phantom completions; repairing it drops every agent's
    # count basis at once). Re-stamp the slot to the current count (preserving
    # the last real fire timestamp) and noop — cadence resumes from the
    # corrected basis. Fires at most once per correction; upward jumps and
    # the last_count==0 first-fire path are unaffected (diff >= 0 there).
    if diff < 0:
        # ZERO-GUARD (guard-1091; fresh-eyes-code F-001, 2026-07-14). A FAILED
        # measurement is not a measurement of ZERO. count_completed_goals()
        # returns 0 as a SILENT FAILURE SENTINEL: every candidate file missing
        # (`if not p.exists(): continue`) or unreadable (`except OSError:
        # continue`) leaves total=0. The world store is S3-backed on an
        # own-cloud deployment, so a mid-sync read miss is routine.
        #
        # Re-baselining on that 0 would PERSIST the transient error as the new
        # basis and SPURIOUSLY FIRE next iteration (last_count==0 first-fire
        # path). BEFORE this heal existed a transient 0 was HARMLESS — diff<0
        # => noop => NO WRITE => self-recovering. The heal must not convert a
        # self-recovering error into permanent state corruption. Noop WITHOUT
        # re-stamping; the next check retries.
        #
        # `current == 0` inside `diff < 0` already implies `last_count > 0`, so
        # this cannot mask a legitimate basis: a real count never falls to zero
        # (folds in archives + census_completed; eviction-invariant). A
        # genuinely empty store self-heals the moment ONE goal completes.
        if current == 0:
            print(
                f"fresh-eyes-cadence-check: negative diff ({diff}) with current=0 "
                f"vs last={last_count} — FAILED MEASUREMENT, not a real basis "
                f"(count_completed_goals returns 0 on read failure); noop WITHOUT "
                f"re-stamp — retries next check",
                file=sys.stderr,
            )
            return 1
        rebase_payload = json.dumps({
            "timestamp": last.get("timestamp", "0000-00-00T00:00:00"),
            "goals_count_at_last_fire": current,
            "rebaselined_from": last_count,
        })
        try:
            subprocess.run(
                [sys.executable, str(HERE / "wm.py"), "set", slot_name],
                input=rebase_payload,
                capture_output=True,
                text=True,
                check=True,
            )
            print(
                f"fresh-eyes-cadence-check: negative diff ({diff}) — count basis "
                f"moved backward (last={last_count} > current={current}); "
                f"re-baselined {slot_name} to {current} — noop this iter"
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            print(
                f"fresh-eyes-cadence-check: negative-diff re-baseline write "
                f"failed ({exc!r}) — noop without re-stamp; retries next check",
                file=sys.stderr,
            )
        return 1
    if diff >= goal_cadence:
        # 8 team-aware gate: shared-resource rituals (tree, program)
        # have ONE time series, but the per-agent WM slot above never sees a
        # TEAMMATE's fire — so an agent whose own slot is stale re-runs the
        # ritual days after the team already reviewed (canonical incident:
        # delta re-fired the tree review 24h after the 2026-06-09 team
        # review). When the config block declares `team_aware: true`, consult
        # world/team-state.yaml shared_cadences.<slot> (stamped by
        # fresh-eyes-record-tick.sh on every fire) and noop while the team's
        # last fire is within cadence. Units are WORLD-ONLY counts on both
        # sides — agent-inclusive counts are not cross-agent comparable (each
        # agent's private queue history differs by hundreds of goals).
        # No slot sync: this gate re-noops each precheck (cheap — only
        # reached when the per-agent cadence already crossed) until the world
        # count elapses past the team stamp. Fail-open: missing file, missing
        # stamp, missing field, or parse error → per-agent behavior.
        # KNOWN RESIDUAL (9, rb-2876): the world-only diff STILL
        # crosses when a single fleet S3 sync imports > cadence completed
        # goals in one window (a transient fresh-box bring-up artifact --
        # steady-state syncs are incremental < cadence). Harm is
        # efficiency-only (a wasted ritual re-run, caught manually via subject
        # mtime + re-stamp). Deliberately NOT closed with a 3rd count-scoping
        # gate: bounded + transient does not warrant new hot-path complexity
        # (implementation-discipline + elegance-is-subtraction). Revisit only
        # on evidenced steady-state harm.
        if fe.get("team_aware"):
            team = team_stamp_value(slot_name)
            team_world = (team or {}).get("world_goals_count_at_last_fire")
            if team_world is not None:
                try:
                    team_world = int(team_world)
                except (TypeError, ValueError):
                    team_world = None
            if team_world is not None:
                current_world = count_completed_goals(world_only=True)
                team_diff = current_world - team_world
                # Team-layer negative-diff self-heal (1, sibling of
                # the 6 per-agent guard above): a DOWNWARD world-count
                # correction (census repair) leaves the shared stamp ABOVE the
                # live world count, and only a FIRE re-stamps
                # shared_cadences.<slot> (fresh-eyes-record-tick.sh) — which
                # this gate's noop prevents, so team_aware rituals starve until
                # the world count regrows past the stale stamp. Re-stamp the
                # shared cadence to the current world count (preserve the last
                # fire's timestamp/fired_by; mark rebaselined_from) and noop
                # once. Fail-open: a write error noops without re-stamp and
                # retries on the next check. In-process daemon call (_rt) —
                # no bash subprocess from Python (rb-225/rb-247).
                if team_diff < 0 and current_world == 0:
                    # ZERO-GUARD (guard-1091; fresh-eyes-code F-001, 2026-07-14)
                    # — the SHARED-STATE case, and the worst of the four. This
                    # branch writes world/team-state.yaml shared_cadences, so a
                    # re-baseline on a failure sentinel corrupts the cadence
                    # basis for the WHOLE FLEET, not just this agent.
                    # count_completed_goals(world_only=True) is the SAME function
                    # as the per-agent path above and returns 0 on the same silent
                    # read failures. Noop WITHOUT re-stamping; retries next check.
                    # (team_diff < 0 with current_world == 0 already implies
                    # team_world > 0 — a real world count never falls to zero.)
                    print(
                        f"fresh-eyes-cadence-check: team negative diff ({team_diff}) "
                        f"with current_world=0 vs team stamp {team_world} — FAILED "
                        f"MEASUREMENT, not a real basis; refusing to re-stamp the "
                        f"SHARED cadence (would corrupt every agent's basis) — "
                        f"retries next check",
                        file=sys.stderr,
                    )
                    # Explicit noop. Falling through would ALSO noop (team_diff<0
                    # is always < goal_cadence), but only emergently — and it would
                    # print the "healthy team-aware gate" message below, disguising
                    # a failed measurement as a normal not-yet-due skip. That
                    # disguise is the exact class of bug this guard exists to kill.
                    return 1
                elif team_diff < 0:
                    rebase_stamp = json.dumps({
                        "timestamp": (team or {}).get(
                            "timestamp", "0000-00-00T00:00:00"),
                        "world_goals_count_at_last_fire": current_world,
                        "fired_by": (team or {}).get("fired_by", "unknown"),
                        "rebaselined_from": team_world,
                    })
                    try:
                        _rt.rt_call(
                            "POST", "/v1/team-state/update",
                            query={
                                "field": f"shared_cadences.{slot_name}",
                                "value": rebase_stamp,
                                "operation": "set",
                            },
                        )
                        print(
                            f"fresh-eyes-cadence-check: negative TEAM diff "
                            f"({team_diff}) — world count basis moved backward "
                            f"(team stamp {team_world} > current_world "
                            f"{current_world}); re-baselined shared_cadences."
                            f"{slot_name} to {current_world} — noop this iter"
                        )
                    except _rt.RtError as exc:
                        print(
                            f"fresh-eyes-cadence-check: team-stamp re-baseline "
                            f"write failed ({exc}) — noop without re-stamp; "
                            f"retries next check",
                            file=sys.stderr,
                        )
                    return 1
                if team_diff < goal_cadence:
                    if not args.verbose:
                        print(
                            f"fresh-eyes-cadence-check: noop (block={args.config_block}, "
                            f"per-agent diff={diff}>=cadence={goal_cadence} but team-aware "
                            f"gate: world diff={team_diff} since team fire by "
                            f"{(team or {}).get('fired_by', 'unknown')} at "
                            f"{(team or {}).get('timestamp', '?')} < cadence) [g-115-1388]"
                        )
                    return 1
        # 4 min_session_goals gate: world-goal completions tick every
        # agent's cadence counter, but per-agent rituals (Self briefing, felt-
        # sense lanes 1-6) need session-scoped data to do real work. When the
        # firing-agent has <min_session_goals completed THIS session, return
        # noop instead of fire — the ritual's lanes would no-op anyway, but
        # the unfired tick still increments the slot so cadence stays sane.
        # Fail-open: missing loop_state slot or read error → no-gate behavior.
        min_session_goals = int(fe.get("min_session_goals", 0) or 0)
        if min_session_goals > 0:
            loop_state = wm_slot_value("loop_state") or {}
            session_done = int(loop_state.get("goals_completed_this_session", 0) or 0)
            if session_done < min_session_goals:
                if not args.verbose:
                    print(
                        f"fresh-eyes-cadence-check: noop (block={args.config_block}, "
                        f"diff={diff}>=cadence={goal_cadence} but min_session_goals gate: "
                        f"session_done={session_done} < min_session_goals={min_session_goals})"
                    )
                return 1
        # 8 auto-reconcile: the fresh-eyes rituals write their archive
        # report (Phase 4) BEFORE stamping the cadence slot (Phase 8). Autocompact
        # between those two steps leaves a report on disk with the slot un-stamped,
        # so on the next iteration the cadence "fires" again even though the review
        # already ran. Before firing, look for a report whose embedded filename
        # timestamp is newer than the slot stamp; if one exists, the ritual already
        # ran (just lost its stamp) — re-stamp the slot and noop instead of
        # re-running it. Config-gated by `report_glob`: rituals without that field
        # (felt_sense, l1_skew — which write no fresh-eyes-*.md report) skip this.
        # The slot write mirrors the seed-stagger path above (wm.py set); on write
        # failure we fall through to fire (re-running the review is safe and
        # self-heals via Phase 8 record-tick — better than silently suppressing).
        report_glob = fe.get("report_glob")
        slot_epoch = _parse_iso_epoch(last.get("timestamp")) if report_glob else None
        if report_glob and slot_epoch is not None:
            agent = os.environ.get("MIND_AGENT", "")
            # Briefings now live under temp/ (file-model normalization moved
            # fresh-eyes briefings reports/ -> temp/; reports/ is abolished).
            briefing_dir = _paths.agent_dir(agent) / "temp" if agent else None
            if briefing_dir is not None and briefing_dir.is_dir():
                hit = newest_reconcilable_report(briefing_dir, report_glob, slot_epoch)
                if hit is not None:
                    report_path, report_ts_iso = hit
                    reconcile_payload = json.dumps({
                        "timestamp": report_ts_iso,
                        "goals_count_at_last_fire": current,
                        "auto_reconciled": True,
                        "reconciled_from_report": report_path.name,
                    })
                    try:
                        subprocess.run(
                            [sys.executable, str(HERE / "wm.py"), "set", slot_name],
                            input=reconcile_payload,
                            capture_output=True,
                            text=True,
                            check=True,
                        )
                        print(
                            f"fresh-eyes-cadence-check: auto-reconciled "
                            f"(block={args.config_block}, report {report_path.name} "
                            f"newer than slot stamp — review already ran, re-stamped "
                            f"{slot_name}, noop instead of re-fire) [g-115-1308]"
                        )
                        return 1  # noop — slot re-stamped; ritual already ran
                    except (subprocess.CalledProcessError, OSError) as exc:
                        print(
                            f"fresh-eyes-cadence-check: auto-reconcile write to "
                            f"{slot_name} failed ({exc!r}) — firing normally",
                            file=sys.stderr,
                        )
                        # fall through to fire
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
