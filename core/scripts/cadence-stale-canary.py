"""cadence-stale-canary — defense-in-depth for the  cadence battery
(g-115-2986). The sibling of stale-sentinel-canary.py, one level down.

The g-115-2984 cadence battery makes the SIX skill-invocation cadence CHECKS
un-skippable (one read-only battery call re-derives the full set from
_cadence_registry). But the DISPATCH — invoking the fresh-eyes / felt-sense /
evolution / curriculum skill on a printed FIRE line — stays LLM-orchestrated. A
printed "▸ CADENCE FIRE" line is far more visible than a silently-skipped phase
(the class g-115-2984 closed), so it is a real improvement but not airtight: the
LLM can still see the FIRE line and not invoke the ritual. This canary catches
that residual skip.

Mechanism (fire/noop — the cadence analogue of the sentinel canary's set/clear):
  - A cadence-check returns exit 0 (FIRE) while its dispatch stamp is stale, and
    exit non-zero (noop) once a successful dispatch advances the stamp. So a
    cadence that keeps FIRING across consecutive canary runs means its dispatch
    keeps being skipped (a successful dispatch would stamp the slot and flip the
    check to noop). This is EXACTLY the set-for-N-runs shape the sentinel canary
    counts, read through the check EXIT CODE instead of a WM slot value.
  - Own state slot `slots.cadence_stale_canary` holds {cadence_name: stuck_count}.
  - Each run, for every cadence in _cadence_registry.cadences():
      run its check_cmd (subprocess; exit 0 == firing)
      if firing: stuck_count += 1
      else:      stuck_count = 0
      if stuck_count >= threshold: file Investigate, reset to 0.
  - Threshold from config: stale_cadence.threshold_iterations (default 3).

Why threshold 3 (not 1): there is a built-in 1-iteration lag. A cadence crosses
its goal-count threshold at iteration N's close (state-update bumps
goals_completed), so the canary at N's close sees FIRE (count=1); then N+1's
precheck Phase 0.5e dispatches it (stamp advances) and N+1's close sees noop
(reset). A HEALTHY loop therefore oscillates 0->1->0 and never reaches 3; only a
persistently-skipped dispatch climbs 1->2->3. Matches stale_sentinel's threshold.

Starvation class this catches: felt-sense (0.5f) starved 3 days / 581 goals
(g-115-2982) because its phase was abbreviated under context pressure and the
gate was never run. g-115-2984 made the CHECK un-skippable; this canary makes a
skipped DISPATCH un-silent — an Investigate fires after `threshold` consecutive
un-dispatched fires.

Scope: the six skill-invocation cadences in _cadence_registry (the same SSOT the
battery reads). l1-skew (0.5g) and health-regression (0.5h) are NOT
cadence-battery members (they self-act / are dormant) and are outside this canary
too, exactly as they are outside the battery.

CADENCE-CHECKS ARE RUN LOCK-FREE, BEFORE the WM lock is acquired: each check
reads working memory (goal count / elapsed vs the last-fire stamp), so running it
while HOLDING the WM lock would deadlock against its own wm-read. Fire states are
collected first (no lock held), then a short locked RMW updates the counters. The
checks are read-only (they never stamp — the dispatch loop owns the stamp), so
running each 6x per close mutates nothing.

Direct YAML I/O on working-memory.yaml + atomic tempfile rename — same pattern as
stale-sentinel-canary.py (subprocess-spawned bash on Windows mangles Python-form
paths, so filing uses SCRIPT_DIR POSIX paths).

Invocation: iteration-close.sh do_productivity_check() (fail-open), beside
stale-sentinel-canary.py.

Verification: tests/test_cadence_stale_canary.py drives run() with an injected
check-runner and asserts the Investigate fires at threshold, resets after, and
never fires while a cadence is noop or its check errors.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import yaml
    from _paths import AGENT_DIR, CORE_ROOT, WORLD_DIR
    from _fileops import acquire_lock, release_lock
    from _runtime_bash import bash_cmd  # Windows-safe bash resolution ()
    from _cadence_registry import cadences
except Exception:
    sys.exit(0)

CANARY_SLOT = "cadence_stale_canary"
# Target aspiration for filed Investigate goals.  is the framework-
# evolution world aspiration (the g-115-* queue) — the same target the sibling
# stale-sentinel-canary and the other framework canaries file into.
ASP_ID = "asp-115"
DEFAULT_THRESHOLD = 3
# Re-file suppression window (mirrors stale-sentinel-canary DEDUP_HOURS). The
# stuck condition is INTERMITTENT (a persistently-skipped dispatch re-trips the
# count every `threshold` runs after each post-fire reset), so without dedup the
# canary would file a byte-identical Investigate every few iterations. 168h (7d)
# matches the sibling; `_recent_investigate_exists` suppresses a re-file when an
# OPEN or recently-filed identical-origin_signal Investigate already exists.
DEDUP_HOURS = 168
_CHECK_TIMEOUT_S = 30


def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


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
    section = cfg.get("stale_cadence") or {}
    try:
        return max(1, int(section.get("threshold_iterations", DEFAULT_THRESHOLD)))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


def _run_check(check_cmd: list[str]) -> tuple[bool | None, str | None]:
    """Run a cadence check_cmd from _cadence_registry.

    Returns (firing:bool|None, err:str|None). firing None + err set == the check
    could not run (timeout / missing script) — the caller treats it as NOT firing
    (noop, fail-open) and surfaces the error. Exit 0 == FIRE, any non-zero == noop.
    The check is READ-ONLY (it never stamps), so running it here mutates nothing.
    """
    script = check_cmd[0]
    full = bash_cmd((SCRIPT_DIR / script).as_posix(), *check_cmd[1:])
    try:
        r = subprocess.run(
            full, cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=_CHECK_TIMEOUT_S,
        )
        return (r.returncode == 0), None
    except Exception as exc:  # timeout, missing script, spawn failure
        return None, f"{script}: {exc}"


def _recent_investigate_exists(cadence: str, since_hours: int = DEDUP_HOURS) -> bool:
    """Suppress a duplicate canary Investigate (mirrors stale-sentinel-canary).

    Returns True (suppress the file) when the world+agent queues already hold an
    Investigate with origin_signal ``investigate:cadence-stale-canary:<cadence>``
    that is EITHER open (pending/in-progress — a live duplicate) OR was created
    within ``since_hours`` (recently filed/resolved — the intermittent re-trip of a
    known-characterised condition). Matches on the precise origin_signal the canary
    stamps, not a title regex.

    guard-487 (suppression gates fail CLOSED): on any queue READ error, return True
    (suppress). A swallowed error mapping to "no duplicate found" would re-enable
    the spam this gate exists to stop. The post-fire counter reset makes a
    genuinely-stuck cadence re-trip and re-alert once the read recovers
    (self-correcting), so erring toward suppression loses no durable signal.
    """
    origin = f"investigate:cadence-stale-canary:{cadence}"
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


def _file_investigate(cadence: str, stuck: int, cadence_dict: dict, dry_run: bool) -> dict:
    """File an Investigate goal under  via aspirations-add-goal.sh.

    Uses POSIX-form path (forward slashes) so Git-Bash-for-Windows can resolve the
    script — passing Windows-form ``C:\\...`` paths through subprocess gets
    backslashes interpreted as escapes by bash.
    """
    dispatch = cadence_dict.get("fire_dispatch", "the cadence ritual")
    check_cmd_str = " ".join(cadence_dict.get("check_cmd", []) or [])
    title = (
        f"Investigate: cadence {cadence} FIRING for {stuck} iterations without "
        "dispatch — ritual skill likely bypassed"
    )
    description = (
        f"The {cadence} cadence-check has returned FIRE for {stuck} consecutive "
        f"cadence-stale-canary runs without its dispatch stamp advancing (a "
        f"successful dispatch would stamp the slot and flip the check to noop). "
        f"Its skill dispatch ({dispatch}) is LLM-orchestrated at "
        f"aspirations-precheck Phase 0.5e on the printed '▸ CADENCE FIRE' line "
        f"— the residual abbreviation risk one level below g-115-2984 (which made "
        f"the CHECK un-skippable; the DISPATCH stays LLM-driven). The likely cause "
        f"is an LLM omission under context pressure, OR a persistent tight-zone "
        f"meter-drop of the ritual. This is the felt-sense-starvation class "
        f"(g-115-2982: felt-sense fired for 3 days / 581 goals un-dispatched). "
        f"Investigate: (1) confirm the cadence still fires — run its check_cmd "
        f"`{check_cmd_str}` (exit 0 == fire); (2) if firing, {dispatch} now to "
        f"advance the stamp; (3) if a persistent meter-drop is the cause "
        f"(tight zone), investigate why the loop stays in tight zone. Filed by "
        f"cadence-stale-canary; threshold `stale_cadence.threshold_iterations` "
        f"controls sensitivity."
    )
    payload = {
        "title": title,
        "description": description,
        "priority": "MEDIUM",
        "participants": ["agent"],
        "category": "framework-architecture",
        "origin_signal": f"investigate:cadence-stale-canary:{cadence}",
        "work_class": "framework",
        "intended_agent": "either",
        "tags": [
            "cadence-lifecycle",
            "cadence-stale-canary",
            "defense-in-depth",
            f"cadence:{cadence}",
        ],
    }
    if dry_run:
        return {"dry_run": True, "payload_title": title}

    # SCRIPT_DIR (Path(__file__).resolve().parent) is ALWAYS absolute and
    # independent of cwd/env — unlike PROJECT_ROOT, which can resolve to an
    # empty/relative value inside a nested subprocess (the canary runs nested:
    # iteration-close do_productivity_check -> here), yielding a relative
    # "core/scripts/aspirations-add-goal.sh" that bash cannot find (rc 127).
    # Mirrors stale-sentinel-canary._file_investigate.
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
    # The goal-duplication gate's prose-overlap heuristic false-positives on
    # canary Investigates BY CONSTRUCTION — a cadence-stale Investigate shares
    # vocabulary (the cadence name, "cadence fire", the dispatch phrase) with any
    # recently-completed goal about the same cadence. This canary runs UNATTENDED
    # (iteration-close productivity-check), so a bare add_goal_rc_nonzero return
    # silently drops the alert. Machine-key dedup already ran upstream
    # (_recent_investigate_exists on the exact origin_signal, fail-closed), so
    # reaching here means the cadence is genuinely NEW and any dup-block is a
    # structural-overlap-only false-positive — the override is justified. Mirrors
    # stale-sentinel-canary () / alert-sweep.sh.
    if result.returncode != 0 and "goal_duplication_blocked" in (
        (result.stdout or "") + (result.stderr or "")
    ):
        reason = (
            f"cadence-stale-canary: origin_signal "
            f"investigate:cadence-stale-canary:{cadence} already dedup'd upstream "
            f"(_recent_investigate_exists, fail-closed); dup-gate prose-overlap on "
            f"canary vocabulary is a structural false-positive (mirrors g-115-2504)"
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


def run(threshold: int, dry_run: bool, check_runner=None) -> dict:
    """Read WM under lock, evaluate cadences, optionally write back + file.

    check_runner: injectable (check_cmd) -> (firing|None, err|None) for tests.
    Defaults to the real bash subprocess runner (_run_check). Fire states are
    computed with the runner BEFORE the WM lock is taken (the checks read WM and
    would deadlock against a held lock), then the counters are updated under lock.
    """
    runner = check_runner or _run_check
    report: dict = {
        "checked_at": _now_iso(),
        "threshold_iterations": threshold,
        "dry_run": dry_run,
        "cadences": {},
        "investigate_goals_filed": [],
        "investigate_goals_suppressed": [],
    }

    if AGENT_DIR is None:
        report["skipped"] = "no_agent_bound"
        return report

    try:
        cads = cadences()
    except Exception as exc:
        report["skipped"] = f"cadence_registry_failed: {exc}"
        return report

    # LOCK-FREE fire-state collection. Each cadence-check reads working memory,
    # so it MUST run before the WM lock below is acquired (else its own wm-read
    # deadlocks against our held lock). The checks never stamp, so this mutates
    # nothing.
    fire_states: list[tuple[str, bool | None, str | None, dict]] = []
    for c in cads:
        firing, err = runner(c["check_cmd"])
        fire_states.append((c["name"], firing, err, c))

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

    fired_records: list[tuple[str, int, dict]] = []
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

        for name, firing, err, cadence_dict in fire_states:
            prev = int(counters.get(name, 0) or 0)
            entry: dict = {
                "firing": firing,
                "check_error": err,
                "prev_stuck_count": prev,
                "new_stuck_count": 0,
                "fired": False,
            }
            if err is not None:
                # Check could not run — fail-open: treat as noop, reset, surface
                # the error. A broken check must never manufacture a stuck count.
                new_count = 0
            elif firing:
                new_count = prev + 1
            else:
                new_count = 0

            entry["new_stuck_count"] = new_count

            if new_count >= threshold:
                entry["fired"] = True
                fired_records.append((name, new_count, cadence_dict))
                new_count = 0  # reset post-fire so the next fire re-starts counting

            counters[name] = new_count
            report["cadences"][name] = entry

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
    # aspirations-add-goal subprocess can't deadlock against any nested WM access
    # during validation. Counters are already persisted with the post-fire reset;
    # if filing fails the next iteration starts fresh.
    for name, stuck, cadence_dict in fired_records:
        # Dedup on the LIVE filing path only (dry-run files nothing and is the
        # detection-preview/test path — it must report the fire unsuppressed).
        if not dry_run and _recent_investigate_exists(name):
            report["cadences"][name]["filing_result"] = {"suppressed_dedup": True}
            report["investigate_goals_suppressed"].append({
                "cadence": name,
                "stuck_count": stuck,
                "reason": "open_or_recent_duplicate",
            })
            continue
        filing = _file_investigate(name, stuck, cadence_dict, dry_run)
        report["cadences"][name]["filing_result"] = filing
        report["investigate_goals_filed"].append({
            "cadence": name,
            "stuck_count": stuck,
            "result": filing,
        })

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cadence-stale canary (g-115-2986)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Do not file Investigate goals and do not persist counters.",
    )
    parser.add_argument(
        "--threshold", type=int, default=None,
        help="Override stale_cadence.threshold_iterations from aspirations.yaml.",
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
