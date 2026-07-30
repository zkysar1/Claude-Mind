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
                                       (CONSUMPTION-AWARE since g-115-1649 —
                                       keyed on force_tree_maintain_last_dispatch;
                                       same arm-then-sample-before-clear false
                                       fire that g-115-1553 fixed for the sibling)
    fresh_eyes_dispatch_pending      — writer: post-state-update-gate.sh
                                       consumer: aspirations-precheck Phase 0-pre3
    force_metric_encoding_pending    — writer: post-state-update-metric-gate.sh
                                       consumer: aspirations-precheck Phase 0-pre4
    force_evolution_finalize         — writer: evolution-stub-pending-check.sh
                                       consumer: aspirations-precheck Phase 0-pre2.5
                                       (CONSUMPTION-AWARE from registration,
                                       g-115-3678 — the producer re-arms every
                                       iteration while any stub is pending, and
                                       a stub whose rationale genuinely cannot
                                       be reconstructed is SUPPOSED to sit set
                                       until the 24h expiry, so presence-count
                                       would fire on correct behavior)

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
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import yaml
    from _paths import AGENT_DIR, CORE_ROOT, WORLD_DIR
    from _fileops import acquire_lock, release_lock
    from _runtime_bash import bash_cmd  # : Windows-safe bash resolution
    from _sentinel_registry import canary_tracked_slots, consumption_aware_map
    from _sentinel_registry import is_set as _registry_is_set
except Exception:
    sys.exit(0)

# Registry-derived (): _sentinel_registry.py is the single source of
# truth shared with precheck-sentinel-battery.py. canary_tracked_slots()
# yields exactly the four slots previously hardcoded here (force_tree_maintain,
# fresh_eyes_dispatch_pending, force_metric_encoding_pending + the
# force_tree_encoding legacy tripwire); parity is pinned by
# tests/test_precheck_sentinel_battery.py.
TRACKED_SENTINELS = canary_tracked_slots()

CANARY_SLOT = "stale_sentinel_canary"
# Target aspiration for filed Investigate goals.  is the framework-
# evolution world aspiration (the g-115-* queue) — the same framework-work
# target used by other canary scripts (inbox-alert-age-check.py,
# insight-trigger-sweep.py, blocker-recheck.py). It was empty ("") before
# , so even when the path resolved the add-goal call filed to no
# aspiration (daemon aspiration_not_found) — the canary could never actually
# surface a stuck sentinel. Paired with the SCRIPT_DIR path fix in
# _file_investigate (the rc-127 fix), the canary now reaches the daemon AND
# names a real aspiration.
ASP_ID = "asp-115"
DEFAULT_THRESHOLD = 3
# Re-file suppression window (). The stuck condition this canary
# detects is INTERMITTENT (consumer-skip across a compact/stop boundary), so a
# genuinely-stuck sentinel re-trips the count every few iterations. Without
# dedup the canary filed a byte-identical Investigate on EACH fire — three
# identical `investigate:stale-sentinel-canary:force_tree_maintain` goals
# ( 06-24,  06-25,  06-29) polluted the queue,
# the last ranking #1 in the selector at score 11.13 and displacing real work.
# `_recent_investigate_exists` suppresses a re-file when an OPEN (pending/
# in-progress) OR recently-filed (< DEDUP_HOURS) identical-origin_signal
# Investigate already exists — the rb-428 sweep-family idempotency posture every
# OTHER filer in this family already has, mirroring the sibling
# streak-break-reflector._recent_investigate_exists. 168h (7d) because the
# observed re-file gap was ~96h ( -> ); 48h (the
# streak-break value) would not have caught it. The post-fire counter reset is
# unchanged, so a still-stuck sentinel re-trips and re-checks dedup every
# `threshold` runs (self-correcting: a persisting problem re-alerts once the
# cooldown lapses).
DEDUP_HOURS = 168

# Consumption-aware sentinels (). Bare presence-count (_is_set)
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
# Registry-derived (): {slot: dispatch_slot} for every canary-tracked
# slot carrying a dispatch stamp — currently fresh_eyes_dispatch_pending
# (), force_tree_maintain (), force_metric_encoding_pending
# (). All three share the identical false-fire shape those goals
# fixed: the writer re-arms the sentinel in iteration-close do_state_update
# (Phase 8) and the canary samples in do_productivity_check (Phase 12, SAME
# close, AFTER the arm), BEFORE the next iteration's precheck consumer clears
# it — so a bare presence-count reads "set" on every arming close even when the
# consumer keeps up. The dispatch-ADVANCEMENT discriminator in the run loop
# below (count climbs only while the consumer's dispatch stamp stays frozen)
# is what makes these consumption-aware. New dispatch-stamped sentinels get
# this behavior by setting dispatch_slot in _sentinel_registry.py.
CONSUMPTION_AWARE = consumption_aware_map()
# Canary-state key prefix for the last-observed consumer dispatch timestamp.
# Stored alongside the per-sentinel stuck counts in CANARY_SLOT; the prefix
# keeps it from ever colliding with a TRACKED_SENTINELS name (the run loop
# iterates TRACKED_SENTINELS, so this extra key is inert there).
LAST_SEEN_PREFIX = "_last_dispatch_seen__"


def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _is_set(value) -> bool:
    """Registry-shared set-semantics () — see _sentinel_registry.is_set.

    Moved verbatim to the registry so the battery and this canary can never
    diverge on what counts as "set". This alias keeps every internal call
    site and the test suite's import surface unchanged.
    """
    return _registry_is_set(value)


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


def _recent_investigate_exists(sentinel: str, since_hours: int = DEDUP_HOURS) -> bool:
    """Suppress a duplicate canary Investigate ().

    Returns True (suppress the file) when the world+agent queues already hold an
    Investigate with origin_signal ``investigate:stale-sentinel-canary:<sentinel>``
    that is EITHER open (pending/in-progress — a live duplicate) OR was created
    within ``since_hours`` (recently filed/resolved — the intermittent re-trip
    of a known-characterised condition). Matches on the precise origin_signal the
    canary stamps, not a title regex (the sibling streak-break-reflector matches
    title because its goals carry no stable origin token; this canary does).

    guard-487 (suppression gates fail CLOSED): on any queue READ error, return
    True (suppress). A swallowed error mapping to "no duplicate found" would
    re-enable the spam this gate exists to stop. The post-fire counter reset
    makes a genuinely-stuck sentinel re-trip and re-alert once the read recovers
    (self-correcting), so erring toward suppression loses no durable signal.
    """
    origin = f"investigate:stale-sentinel-canary:{sentinel}"
    cutoff = _dt.datetime.now() - _dt.timedelta(hours=since_hours)
    candidates = []
    if WORLD_DIR is not None:
        candidates.append(Path(WORLD_DIR) / "aspirations.jsonl")
    if AGENT_DIR is not None:
        candidates.append(Path(AGENT_DIR) / "aspirations.jsonl")
    for path in candidates:
        try:
            if not path.exists():
                continue
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        asp = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # one bad line — skip, not a gate-disabling error
                    for g in asp.get("goals", []):
                        if (g.get("origin_signal") or "") != origin:
                            continue
                        # OPEN duplicate — always suppress, no time window.
                        if g.get("status") in ("pending", "in-progress"):
                            return True
                        # Recently filed/resolved — suppress within cooldown.
                        created = (g.get("created_at") or g.get("created_date")
                                   or g.get("created"))
                        if not created:
                            return True  # origin match without date — assume recent
                        try:
                            c_dt = _dt.datetime.fromisoformat(str(created))
                        except (ValueError, TypeError):
                            continue
                        if c_dt > cutoff:
                            return True
        except Exception:
            # guard-487: fail CLOSED on a read error — suppress rather than risk
            # re-enabling duplicate spam. Self-correcting via the post-fire re-trip.
            return True
    return False


def _file_investigate(sentinel: str, stuck: int, dry_run: bool) -> dict:
    """File an Investigate goal under  via aspirations-add-goal.sh.

    Uses POSIX-form path (forward slashes) so Git-Bash-for-Windows can
    resolve the script — passing Windows-form ``C:\\...`` paths through
    subprocess gets backslashes interpreted as escapes by bash.
    """
    filer = os.environ.get("MIND_AGENT") or "<see filed_by_agent>"
    title = (
        f"Investigate: stale sentinel {sentinel} (filed by {filer}) set for "
        f"{stuck} iterations — consumer SKILL likely bypassed"
    )
    description = (
        f"The {sentinel} sentinel has been set in working memory for {stuck} "
        f"consecutive stale-sentinel-canary runs without being cleared. Its "
        f"consumer SKILL (aspirations-precheck Phase 0-pre/0-pre2/0-pre3/"
        f"0-pre4 or aspirations-state-update Step 8) likely failed to fire "
        f"mid-iteration (graceful-stop interruption, compact recovery, LLM "
        f"omission). CROSS-AGENT TRIAGE (rb-5069): this canary fires "
        f"PER-AGENT but files into the SHARED world queue, so triage the "
        f"FILING agent's WM (filed_by_agent={filer}), NOT your own — a "
        f"stale value in a different agent's WM is not this goal's subject. "
        f"Investigate: (1) read the sentinel in {filer}'s WM via "
        f"`MIND_AGENT={filer} wm-read.sh {sentinel} --json` to capture the "
        f"payload; (2) check whether the sentinel's dispatch_slot (from "
        f"_sentinel_registry.py, for consumption-aware sentinels) advanced "
        f"since this goal was filed — if it advanced, the consumer DID fire "
        f"and the episode self-resolved (close benign); only a STILL-set "
        f"sentinel with a frozen dispatch is a genuine stuck condition; "
        f"(3) identify the consumer SKILL phase responsible for clearing it "
        f"(see canary source for the writer/consumer pairs) and why it "
        f"hasn't fired; (4) either clear the sentinel manually (`echo 'null' "
        f"| MIND_AGENT={filer} wm-set.sh {sentinel}`) if the consumer's "
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

    # SCRIPT_DIR (Path(__file__).resolve().parent) is ALWAYS absolute and
    # independent of cwd/env — unlike PROJECT_ROOT, which can resolve to an
    # empty/relative value inside a nested subprocess (the canary runs nested:
    # iteration-close do_productivity_check -> here), yielding a relative
    # "core/scripts/aspirations-add-goal.sh" that bash cannot find (rc 127).
    # Mirrors the proven obligation-audit.py pattern.
    # aspirations-add-goal.sh lives in SCRIPT_DIR (core/scripts/).
    script_path = (SCRIPT_DIR / "aspirations-add-goal.sh").as_posix()
    def _run_add(extra):
        return subprocess.run(
            bash_cmd(script_path, "--source", "world", ASP_ID, *extra),
            input=json.dumps(payload),
            capture_output=True, text=True, timeout=30,
        )

    try:
        result = _run_add([])
    except Exception as exc:
        return {"error": "subprocess_failed", "detail": str(exc)}
    # : the goal-duplication gate's prose-overlap heuristic
    # false-positives on canary Investigates BY CONSTRUCTION — a stale-sentinel
    # Investigate shares vocabulary ("stale sentinel", the sentinel name, the
    # consumer-phase names) with any recently-completed goal about the same
    # sentinel. This canary runs UNATTENDED (iteration-close productivity-check),
    # so a bare add_goal_rc_nonzero return silently drops the alert (nobody reads
    # this return value — the  silent-death shape). Machine-key dedup
    # already ran upstream (_recent_investigate_exists on the exact origin_signal,
    # fail-closed), so reaching here means the sentinel is genuinely NEW and any
    # dup-block is a structural-overlap-only false-positive — the override is
    # justified. Mirrors alert-sweep.sh / ohs-husk-cluster
    # (automated-filer-gate-interaction tree node, rb-3835).
    if result.returncode != 0 and "goal_duplication_blocked" in (
        (result.stdout or "") + (result.stderr or "")
    ):
        reason = (
            f"stale-sentinel-canary: origin_signal "
            f"investigate:stale-sentinel-canary:{sentinel} already dedup'd upstream "
            f"(_recent_investigate_exists, fail-closed); dup-gate prose-overlap on "
            f"canary vocabulary is a structural false-positive (g-115-2504)"
        )
        try:
            result = _run_add(["--override-duplication", reason])
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
        "investigate_goals_suppressed": [],
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
                # Consumption-aware (): count toward stuck only
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
        # Dedup (): on the LIVE filing path, suppress a re-file when an
        # OPEN or recently-filed identical-origin_signal Investigate already
        # exists. The counter was already reset post-fire, so a still-stuck
        # sentinel re-trips and re-checks dedup every `threshold` runs
        # (self-correcting). Gated on `not dry_run`: dedup is a FILING concern
        # and dry-run files nothing — dry-run is the detection-preview/test path
        # (the regression tests assert raw fire DETECTION via --dry-run), so it
        # must report the fire unsuppressed. Production (iteration-close.sh) runs
        # non-dry-run and always applies dedup.
        if not dry_run and _recent_investigate_exists(sentinel):
            report["sentinels"][sentinel]["filing_result"] = {"suppressed_dedup": True}
            report["investigate_goals_suppressed"].append({
                "sentinel": sentinel,
                "stuck_count": stuck,
                "reason": "open_or_recent_duplicate",
            })
            continue
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
