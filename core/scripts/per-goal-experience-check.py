#!/usr/bin/env python3
"""Phase 4.25 PER-GOAL experience-record check — shared by BOTH close paths.

Extracted from recurring-close.sh (the g-115-547 canary, formerly inline at
~L873-928) by g-115-4661 so the non-recurring close path can use the same
enforcement. recurring-close.sh no longer carries its own copy — per guard-2015
the origin must not keep a fork, or this file's later hardening never reaches it.

WHAT THIS CHECKS, AND WHY IT IS NOT experience-staleness-check.sh
----------------------------------------------------------------
`experience-staleness-check.sh` is STORE-level: it warns when the most-recent
entry of ANY kind exceeds a 12h threshold. It has no goal_id join, so a busy
agent whose store is an hour fresh reads clean while individual deep goals close
with no record at all — structurally invisible, by that check's own contract.

This check is PER-GOAL: it asks whether THIS goal has a record that counts as
coverage for THIS close, and on a miss sets the `force_experience_archival`
WM sentinel naming the goal. aspirations-precheck Phase 0-pre2 consumes the
sentinel next iteration and forces the LLM to retro-compose. The two are
complementary and BOTH should run — the store-level one remains the
long-horizon backstop.

WHAT "COVERAGE" MEANS PER PATH (g-115-5314)
-------------------------------------------
The RECURRING call site keeps a short wall-clock window: the same goal_id
closes many times, so an entry from a prior close must NOT count. The
NON-recurring call site (trigger starts with the shared NONRECURRING_PRODUCER
from spark-fire-dedup.py) matches on goal_id/source_goal ALONE — a
non-recurring goal closes exactly once, so any joined entry is necessarily
this close's, and a window on top of the join could only false-fire as
iteration length grew (measured 2264s..4365s gaps on real closes). The
discriminator is imported, not duplicated; if the import fails, every trigger
keeps the bounded window (fails toward SETTING the sentinel — the fail-closed
asymmetry; see the module-level note for the full rationale).

Measured coverage that motivated the extraction (echo, cc-03, 2026-08-02, joined
against experience.jsonl + experience-archive.jsonl + experience/*.md across 5
agents): non-recurring completed goals with ANY experience record ran 16-32%,
while recurring goals — the one lane where this check was wired — ran 95%.

MATCHING (do not "simplify" this to goal_id alone)
--------------------------------------------------
Matches canonical `goal_id` OR legacy `source_goal` (g-115-2511): a minority of
store entries carry only `source_goal`, because writer templates drifted by
analogy with the rb/guardrail stores where `source_goal` IS canonical. Dropping
the fallback makes the sentinel FALSE-fire on closes whose record exists.
guard-697 / guard-713 are the write-side half of the same seam: a record written
with only `source_goal` is invisible to `experience-read.sh --goal`.

FAIL-OPEN, LOUDLY
-----------------
Always exits 0 — a check failure must never block a goal close. But degradation
is VISIBLE on stderr rather than swallowed: a silent `|| true` makes the check
undetectable in exactly the scenario it exists for (insight trigger
msg-20260801-171952-zeta-5643, same file family). Callers should ALSO guard the
invocation itself (`|| echo "WARN ..." >&2`), because a missing interpreter or
missing file never reaches this code to report anything.
"""

import argparse
import importlib.util
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_WINDOW_SECONDS = 30 * 60
TAIL_LINES = 100


def _warn(msg: str) -> None:
    print(f"[per-goal-experience-check] {msg}", file=sys.stderr)


# : the NON-RECURRING call site (iteration-close.sh do_state_update)
# gets an UNBOUNDED recency bound — matching on goal_id/source_goal alone.
# A non-recurring goal closes exactly ONCE, so any entry joined by goal_id is
# necessarily THIS close's entry; the 30-min window on top of the join only
# subtracts correctness, at a false-positive rate proportional to iteration
# length (measured 2264s..4365s across the record's instances). The RECURRING
# call site keeps the window: the same goal_id closes many times, so an entry
# from a prior close would satisfy a bare join and wrongly suppress the
# sentinel — there the window is load-bearing, and it brackets the sibling
# defect  from the opposite side; a claim-time anchor for that path
# is a separate, not-yet-tested change and deliberately NOT made here.
#
# THE DISCRIMINATOR IS SHARED, NOT DUPLICATED (): spark-fire-dedup.py
# hit the identical problem on the same code path and fixed it in 
# with NONRECURRING_PRODUCER = "nonrecurring-state-update" + UNBOUNDED_LOOKBACK
# (its rationale block: "a non-recurring goal closes exactly once (no prior
# close to mis-match)"). The non-recurring trigger this check receives —
# 'nonrecurring-state-update-deep-no-recent-entry' — names that producer as a
# prefix, so startswith is the producer-awareness test. Load the constant from
# the sibling (importlib: the file is hyphenated, never a package member — the
# repo's standard loader shape, cf. _delivery_gate.py). If the load fails,
# _NONRECURRING_PRODUCER stays None and EVERY trigger falls through to the
# bounded window — today's behavior, which fails toward SETTING the sentinel.
# This check must keep that asymmetry: unlike spark-fire-dedup (which fails
# toward FIRING, because a missed spark loses learning), a missing experience
# record is a real lost artifact and a spurious sentinel costs one probe.
_NONRECURRING_PRODUCER: "str | None" = None
try:
    _spec = importlib.util.spec_from_file_location(
        "spark_fire_dedup", str(SCRIPT_DIR / "spark-fire-dedup.py"))
    if _spec is not None and _spec.loader is not None:
        _sfd = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_sfd)
        _candidate = getattr(_sfd, "NONRECURRING_PRODUCER", None)
        if isinstance(_candidate, str) and _candidate:
            _NONRECURRING_PRODUCER = _candidate
        else:
            _warn("could not read NONRECURRING_PRODUCER from spark-fire-dedup — "
                  "keeping the bounded window for every trigger")
    else:
        _warn("could not load spark-fire-dedup.py — keeping the bounded window "
              "for every trigger")
except Exception as _exc:                     # pragma: no cover - env-specific
    _warn(f"could not import spark-fire-dedup ({_exc}) — keeping the bounded "
          "window for every trigger")


def has_recent_record(exp_path: Path, goal_id: str,
                      window_seconds: "int | None", now: datetime) -> bool:
    """True when exp_path holds a goal_id (or source_goal) entry that counts as
    coverage for this close.

    `window_seconds=None` means UNBOUNDED: any joined entry counts, whatever its
    age — the non-recurring call site's bound (g-115-5314, mirroring
    spark-fire-dedup's UNBOUNDED_LOOKBACK=None, whose rationale is that a
    non-recurring goal closes exactly once, so no prior close can mis-match).
    The RECURRING call site passes the bounded window and needs it: entries
    from prior closes of the same goal_id must NOT count.

    Reads only the tail — recent entries are at the end. (The tail-order
    assumption is a SEPARATE defect, g-115-11555: ledger-merge reorderings can
    park a fresh record outside the tail; the last measured instances excluded
    it as a rival mechanism. Do not 'fix' it here without that goal's bracket.)

    A MISSING file returns False, so the sentinel fires: an agent with no
    experience store has certainly not recorded this goal. A read failure
    RAISES instead, and main() then skips the check entirely (no sentinel) —
    matching the origin block, which exited silently on a store it could not
    read rather than asserting a miss it had not measured. A malformed single
    LINE is skipped, not fatal.
    """
    if not exp_path.exists():
        return False
    lines = exp_path.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines[-TAIL_LINES:]):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if goal_id not in (entry.get("goal_id"), entry.get("source_goal")):
            continue
        if window_seconds is None:
            return True
        try:
            created = datetime.fromisoformat(entry.get("created") or "")
        except (ValueError, TypeError):
            continue
        if (now - created).total_seconds() < window_seconds:
            return True
    return False


def build_payload(goal_id: str, trigger: str, original_outcome: str,
                  now: datetime) -> str:
    """The exact 4-key shape aspirations-precheck Phase 0-pre2 already consumes.

    `trigger` names WHICH close path fired, and `original_outcome` carries the
    caller's pre-flip CLI outcome so a consumer can tell "caller asked for deep"
    from "system flipped routine->deep" (g-115-686 / g-115-688). Do not add keys
    here without checking the consumer.
    """
    return json.dumps({
        "triggered_at": now.isoformat(timespec="seconds"),
        "trigger": trigger,
        "goal_id": goal_id,
        "original_outcome": original_outcome,
    })


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="per-goal-experience-check",
        description="Set force_experience_archival when a goal closed with no recent experience record.",
    )
    ap.add_argument("--goal-id", required=True)
    ap.add_argument("--trigger", required=True,
                    help="label recorded in the sentinel payload, names the calling close path")
    ap.add_argument("--original-outcome", default="",
                    help="caller's pre-flip CLI outcome (recurring path); empty elsewhere")
    ap.add_argument("--window-seconds", type=int, default=DEFAULT_WINDOW_SECONDS)
    ap.add_argument("--dry-run", action="store_true",
                    help="report the verdict and payload; do not write the sentinel")
    args = ap.parse_args()

    goal_id = (args.goal_id or "").strip()
    if not goal_id:
        _warn("empty --goal-id — nothing to check")
        return 0

    # _paths honors MIND_AGENT_DIR / MIND_AGENT, so the agent resolves the
    # same way it does for every other core/scripts consumer (and for the
    # sandboxed tests). Importing by sys.path insert rather than a relative
    # import: this file is run as a script, never imported as a package member.
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import _paths
    except Exception as exc:                        # pragma: no cover - env-specific
        _warn(f"could not import _paths ({exc}) — skipping check for {goal_id}")
        return 0

    agent_dir = getattr(_paths, "AGENT_DIR", None)
    if not agent_dir:
        _warn(f"no AGENT_DIR resolved — skipping check for {goal_id}")
        return 0

    exp_path = Path(agent_dir) / "experience.jsonl"
    now = datetime.now()

    # : producer-awareness. The trigger names the calling close path,
    # and the NON-recurring producer's trigger starts with the shared
    # NONRECURRING_PRODUCER constant (imported from spark-fire-dedup.py, not
    # duplicated here). On that path the recency bound is dropped — match on
    # goal_id/source_goal alone, since a non-recurring goal closes exactly once
    # so any joined entry is necessarily this close's. The RECURRING path keeps
    # the --window-seconds bound, where it is load-bearing (a prior close of the
    # same goal_id must not suppress the sentinel). If the sibling import failed
    # at module load, _NONRECURRING_PRODUCER is None and EVERY trigger falls to
    # the bounded window — today's behavior, failing toward SETTING the
    # sentinel, never toward suppressing it (the fail-closed asymmetry).
    trigger = (args.trigger or "").strip()
    if _NONRECURRING_PRODUCER and trigger.startswith(_NONRECURRING_PRODUCER):
        window = None                                  # unbounded lookback
        window_label = "unbounded (non-recurring, goal_id match only)"
    else:
        window = args.window_seconds
        window_label = f"within {args.window_seconds}s"

    try:
        recent = has_recent_record(exp_path, goal_id, window, now)
    except Exception as exc:
        _warn(f"could not read {exp_path} ({exc}) — skipping check for {goal_id}")
        return 0

    if recent:
        print(f"[per-goal-experience-check] {goal_id}: experience record found "
              f"{window_label} — no sentinel needed", file=sys.stderr)
        return 0

    payload = build_payload(goal_id, args.trigger, args.original_outcome, now)
    if args.dry_run:
        print(payload)
        return 0

    wm_py = SCRIPT_DIR / "wm.py"
    if not wm_py.exists():
        _warn(f"wm.py not found at {wm_py} — sentinel NOT set for {goal_id}")
        return 0
    try:
        proc = subprocess.run(
            [sys.executable, str(wm_py), "set", "force_experience_archival"],
            input=payload, text=True, capture_output=True, timeout=15,
        )
    except Exception as exc:
        _warn(f"wm.py set failed ({exc}) — sentinel NOT set for {goal_id}")
        return 0

    if proc.returncode != 0:
        _warn(f"wm.py set returned rc={proc.returncode} — sentinel NOT set for "
              f"{goal_id}: {(proc.stderr or '').strip()[:300]}")
        return 0

    print(f"[per-goal-experience-check] Phase 4.25 enforcement: deep close on "
          f"{goal_id} with no recent experience entry — set "
          f"force_experience_archival sentinel (trigger={args.trigger})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
