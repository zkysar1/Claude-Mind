"""stale-sentinel-canary — defense-in-depth for Cat C sentinels ().

Cat C sentinels have BASH WRITERS + SKILL-only consumers. If the consumer
SKILL is skipped mid-iteration (graceful-stop, compact recovery, LLM
omission), the sentinel accumulates a set value and goes unread. This
canary detects that state by counting consecutive canary runs in which
each tracked sentinel remains set.

Tracked sentinels (force_* counter-gate family; set/clear semantics):

    force_tree_encoding              — writer: RETIRED (g-115-1521; was
                                       tree-encoding-drift-gate.py). Its only
                                       consumer (aspirations-state-update Step 8)
                                       was on the COLD path the loop bypasses, so
                                       the hot-path set was never cleared and
                                       accumulated "true". The set was removed at
                                       source; force_tree_maintain carries the
                                       encoding-drift signal now. Kept in the
                                       tracked list as a defensive tripwire — if
                                       any future path re-introduces a set without
                                       a hot-path consumer, the canary still
                                       catches it.
    force_tree_maintain              — writer: tree-encoding-drift-gate.py +
                                                iteration-close learning-gate
                                       consumer: aspirations-precheck Phase 0-pre
    fresh_eyes_dispatch_pending      — writer: post-state-update-gate.sh
                                       consumer: aspirations-precheck Phase 0-pre3
    force_metric_encoding_pending    — writer: post-state-update-metric-gate.sh
                                       consumer: aspirations-precheck Phase 0-pre4

Not tracked: consolidation_health (refresh-snapshot, no set/clear) and
known_blockers (list-shaped, handled by blocker-recheck.sh).

Mechanism:
  - Own state slot `slots.stale_sentinel_canary` in working memory holds a
    dict {sentinel_name: stuck_count}.
  - Each run, for every tracked sentinel:
      if set:     stuck_count += 1
      else:       stuck_count = 0
      if stuck_count >= threshold: file Investigate, reset to 0.
  - Threshold from config: stale_sentinel.threshold_iterations (default 3).

Direct YAML I/O on working-memory.yaml — same pattern as
tree-encoding-drift-gate.py (g-248-75) because subprocess-spawned bash on
Windows mangles Python-form paths.

Invocation: iteration-close.sh do_productivity_check() (fail-open).

Verification: tests/test_stale_sentinel_canary.py simulates 4 iterations
with force_tree_maintain set and asserts the Investigate goal is filed.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import yaml
    from _paths import AGENT_DIR, CORE_ROOT, PROJECT_ROOT
    from _fileops import acquire_lock, release_lock
    from _runtime_bash import bash_cmd  # : Windows-safe bash resolution
except Exception:
    sys.exit(0)

TRACKED_SENTINELS = [
    "force_tree_encoding",
    "force_tree_maintain",
    "fresh_eyes_dispatch_pending",
    "force_metric_encoding_pending",
]

CANARY_SLOT = "stale_sentinel_canary"
ASP_ID = ""  # framework-architecture aspiration
DEFAULT_THRESHOLD = 3

# Consumption-aware sentinels (3). Bare presence-count (_is_set)
# false-fires for a sentinel whose WRITER re-arms it every iteration while
# the CONSUMER keeps up: the count then measures consecutive-writer-arms
# (e.g. consecutive substantive deep closes), NOT consumer-bypass. The
# canonical case is fresh_eyes_dispatch_pending — iteration-close.sh
# do_state_update re-arms it on every deep close with material core changes
# (Phase 8), and the canary samples AFTER that arming (Phase 12,
# do_productivity_check), so the sentinel is "set" at sample time on every
# deep-close iteration even though precheck Phase 0-pre3 dispatched and
# cleared it each time. The literal "fire when last_dispatch < set_at" form
# ALSO false-fires here: within one iteration the consumer's last dispatch
# (precheck, early) always precedes the writer's arming (state-update, late),
# so last_dispatch < set_at holds in the healthy keeping-up flow. The
# timing-correct discriminator is dispatch ADVANCEMENT: fire only when the
# consumer's dispatch timestamp has NOT advanced across `threshold`
# consecutive samples. The consumer (aspirations-precheck Phase 0-pre3 + the
# in-iteration iteration-close-digest item-7 path) stamps the dispatch slot
# on ANY handling — dispatch OR a justified no-dispatch clear (e.g. files
# were partner-attributed). Maps sentinel -> consumer dispatch slot.
CONSUMPTION_AWARE = {
    "fresh_eyes_dispatch_pending": "fresh_eyes_last_dispatch",
}
# Canary-state key prefix for the last-observed consumer dispatch timestamp.
# Stored alongside the per-sentinel stuck counts in CANARY_SLOT; the prefix
# keeps it from ever colliding with a TRACKED_SENTINELS name (the run loop
# iterates TRACKED_SENTINELS, so this extra key is inert there).
LAST_SEEN_PREFIX = "_last_dispatch_seen__"


def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _is_set(value) -> bool:
    """A sentinel is 'set' when it carries a meaningful non-empty value.

    JSON null / boolean false / empty / 'null'-strings are NOT set.
    Dict-shaped sentinels (post-state-update-gate.sh JSON payloads) are
    set when their top-level 'fired' key is truthy, or — if 'fired' is
    absent — when the dict has any keys at all (defensive default).
    """
    if value is None or value is False:
        return False
    if isinstance(value, str):
        s = value.strip().lower()
        return s not in ("", "null", "false")
    if isinstance(value, dict):
        if "fired" in value:
            return bool(value["fired"])
        return bool(value)
    if isinstance(value, (list, tuple)):
        return bool(value)
    return True


def _read_threshold(override: int | None) -> int:
    if override is not None:
        return max(1, int(override))
    try:
        cfg_path = Path(CORE_ROOT) / "config" / "aspirations.yaml"
        if not cfg_path.exists():
            return DEFAULT_THRESHOLD
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return DEFAULT_THRESHOLD
    section = cfg.get("stale_sentinel") or {}
    try:
        return max(1, int(section.get("threshold_iterations", DEFAULT_THRESHOLD)))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


def _file_investigate(sentinel: str, stuck: int, dry_run: bool) -> dict:
    """File an Investigate goal under  via aspirations-add-goal.sh.

    Uses POSIX-form path (forward slashes) so Git-Bash-for-Windows can
    resolve the script — passing Windows-form ``C:\\...`` paths through
    subprocess gets backslashes interpreted as escapes by bash.
    """
    title = (
        f"Investigate: stale sentinel {sentinel} set for {stuck} iterations "
        "— consumer SKILL likely bypassed"
    )
    description = (
        f"The {sentinel} sentinel has been set in working memory for {stuck} "
        f"consecutive stale-sentinel-canary runs without being cleared. Its "
        f"consumer SKILL (aspirations-precheck Phase 0-pre/0-pre2/0-pre3/"
        f"0-pre4 or aspirations-state-update Step 8) likely failed to fire "
        f"mid-iteration (graceful-stop interruption, compact recovery, LLM "
        f"omission). Investigate: (1) read the sentinel via "
        f"`wm-read.sh {sentinel} --json` to capture the payload; (2) identify "
        f"the consumer SKILL phase responsible for clearing it (see canary "
        f"source for the writer/consumer pairs); (3) determine why the "
        f"consumer hasn't fired in this run; (4) either clear the sentinel "
        f"manually (`echo 'null' | wm-set.sh {sentinel}`) if the consumer's "
        f"action has already been taken, or trigger the consumer phase "
        f"explicitly. Filed by stale-sentinel-canary; threshold "
        f"`stale_sentinel.threshold_iterations` controls sensitivity."
    )
    payload = {
        "title": title,
        "description": description,
        "priority": "MEDIUM",
        "participants": ["agent"],
        "category": "framework-architecture",
        "origin_signal": f"investigate:stale-sentinel-canary:{sentinel}",
        "work_class": "framework",
        "intended_agent": "either",
        "tags": [
            "sentinel-lifecycle",
            "stale-sentinel-canary",
            "defense-in-depth",
            f"sentinel:{sentinel}",
        ],
    }
    if dry_run:
        return {"dry_run": True, "payload_title": title}

    script_path = (Path(PROJECT_ROOT) / "core" / "scripts" / "aspirations-add-goal.sh").as_posix()
    try:
        result = subprocess.run(
            bash_cmd(script_path, "--source", "world", ASP_ID),
            input=json.dumps(payload),
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        return {"error": "subprocess_failed", "detail": str(exc)}
    if result.returncode != 0:
        return {
            "error": "add_goal_rc_nonzero",
            "rc": result.returncode,
            "stderr": result.stderr.strip()[:300],
        }
    try:
        return {"ok": True, "goal": json.loads(result.stdout.strip())}
    except json.JSONDecodeError:
        return {"ok": True, "stdout": result.stdout.strip()[:300]}


def run(threshold: int, dry_run: bool) -> dict:
    """Read WM under lock, evaluate sentinels, optionally write back."""
    report: dict = {
        "checked_at": _now_iso(),
        "threshold_iterations": threshold,
        "dry_run": dry_run,
        "sentinels": {},
        "investigate_goals_filed": [],
    }

    if AGENT_DIR is None:
        report["skipped"] = "no_agent_bound"
        return report

    from wm import wm_path as _resolve_wm_path  # Phase 1A per-Body WM routing ()
    wm_path = _resolve_wm_path()
    if not wm_path.exists():
        report["skipped"] = "no_working_memory_file"
        return report

    lock_path = wm_path.with_suffix(".lock")
    try:
        # stale_seconds=10 mirrors wm.py wm_lock — same WM file, same RMW cadence.
        acquire_lock(lock_path, stale_seconds=10)
    except Exception as exc:
        report["skipped"] = f"lock_acquire_failed: {exc}"
        return report

    fired_records: list[tuple[str, int]] = []
    try:
        try:
            data = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            report["skipped"] = f"yaml_load_failed: {exc}"
            return report

        slots = data.setdefault("slots", {})
        counters = slots.get(CANARY_SLOT)
        if not isinstance(counters, dict):
            counters = {}

        for sentinel in TRACKED_SENTINELS:
            value = slots.get(sentinel)
            is_set = _is_set(value)
            prev = int(counters.get(sentinel, 0) or 0)

            entry: dict = {
                "is_set": is_set,
                "prev_stuck_count": prev,
                "new_stuck_count": 0,
                "fired": False,
            }

            if sentinel in CONSUMPTION_AWARE and is_set:
                # Consumption-aware (3): count toward stuck only
                # while the consumer's dispatch timestamp stays FROZEN. A
                # writer re-arm with the consumer keeping up advances the
                # dispatch slot, which resets the count — distinguishing
                # "kept up" from "genuinely bypassed", which bare presence-
                # count cannot. None-vs-None (consumer never stamped) counts;
                # any change (incl. None -> first stamp) resets.
                dispatch_slot = CONSUMPTION_AWARE[sentinel]
                seen_key = LAST_SEEN_PREFIX + sentinel
                current_dispatch = slots.get(dispatch_slot)
                last_seen = counters.get(seen_key)
                dispatch_advanced = current_dispatch != last_seen
                new_count = 0 if dispatch_advanced else prev + 1
                counters[seen_key] = current_dispatch
                entry["consumption_aware"] = True
                entry["current_dispatch"] = current_dispatch
                entry["last_seen_dispatch"] = last_seen
                entry["dispatch_advanced"] = dispatch_advanced
                if isinstance(value, dict):
                    entry["sentinel_set_at"] = value.get("set_at")
            else:
                # Presence-count (default): set -> +1, cleared -> reset to 0.
                new_count = prev + 1 if is_set else 0

            entry["new_stuck_count"] = new_count

            if new_count >= threshold:
                entry["fired"] = True
                fired_records.append((sentinel, new_count))
                new_count = 0  # reset post-fire so the next set re-starts counting

            counters[sentinel] = new_count
            report["sentinels"][sentinel] = entry

        if not dry_run:
            slots[CANARY_SLOT] = counters
            # Atomic write via tempfile rename (matches wm.py write_wm).
            tmp = wm_path.with_suffix(".yaml.tmp")
            tmp.write_text(
                yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            tmp.replace(wm_path)
    finally:
        try:
            release_lock(lock_path)
        except Exception:
            pass

    # File Investigate goals AFTER releasing the WM lock so the
    # aspirations-add-goal subprocess can't deadlock against any nested
    # WM access during validation. Counters are already persisted with the
    # post-fire reset; if filing fails the next iteration starts fresh.
    for sentinel, stuck in fired_records:
        filing = _file_investigate(sentinel, stuck, dry_run)
        report["sentinels"][sentinel]["filing_result"] = filing
        report["investigate_goals_filed"].append({
            "sentinel": sentinel,
            "stuck_count": stuck,
            "result": filing,
        })

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stale-sentinel canary (g-115-717)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Do not file Investigate goals and do not persist counters.",
    )
    parser.add_argument(
        "--threshold", type=int, default=None,
        help="Override stale_sentinel.threshold_iterations from aspirations.yaml.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress JSON output on stdout when no Investigate goals fired.",
    )
    args = parser.parse_args(argv)

    threshold = _read_threshold(args.threshold)
    report = run(threshold=threshold, dry_run=args.dry_run)

    if args.quiet and not report.get("investigate_goals_filed"):
        return 0

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
