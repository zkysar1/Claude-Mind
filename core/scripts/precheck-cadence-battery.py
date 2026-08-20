"""precheck-cadence-battery — one call running every deferrable skill-invocation
cadence gate check and reporting which FIRE (g-115-2984, fix for g-115-2982).

Kills the cadence-starvation-by-abbreviation class: the seven skill-invocation
cadence rituals (fresh-eyes-review/program/tree, felt-sense, curriculum,
evolution) were SIX separate LLM-orchestrated precheck phases (0.5e / 0.5e.5 /
0.5e.7 / 0.5f / 0.5i / 0.5j), each abbreviate-able under context pressure. When
the LLM skipped a phase, its cadence-check gate was never run, so the ritual's
skill was never invoked and the ritual starved silently. felt-sense (0.5f)
starved 3 days / 581 goals exactly this way — foxtrot looped continuously, the
budget meter was NOT dropping it (g-115-2982 refuted that mechanism), and a
non-fire at diff 581>>cadence 75 proved the gate was never run. This battery is
the g-115-2303 sentinel-battery / g-115-2550 orchestrator-entry-battery pattern
applied to cadences: post-autocompact, "run the cadence battery" is the ONE line
that must survive summarization; the output re-derives the full cadence set
(which FIRE + the skill to invoke) from _cadence_registry, so a phase can never
silently fall out of the protocol.

CHECKS ARE READ-ONLY, THE BATTERY IS NOT (g-115-6564): the seven cadence-check
scripts "only read state" (goal-count vs last-fire), so running them is
side-effect-free even under pressure, and the SKILL.md dispatch loop keeps
ownership of the actual SKILL INVOCATION on FIRE. But the battery ALSO maintains a
small STATEFUL per-cadence consecutive-FIRE counter (box-local, lock-guarded,
`agents/<agent>/session/cadence-fire-counters.json`) to escalate SUSTAINED
starvation. A single FIRE line cannot distinguish "due once" from "due, dispatch
skipped, 15 prechecks running" — which is exactly how felt-sense starved 3 days /
581 goals (g-115-2982) BEFORE the battery, and how a cadence still starves AFTER
it if the meter-drop / abbreviation keeps skipping the dispatch. The counter
INCREMENTS on FIRE (rc==0 = due) and RESETs to 0 on noop (rc!=0 = dispatched OR
not-due, both non-starved); at count >= ESCALATION_THRESHOLD the battery prints a
LOUD line naming the oldest-starved cadence and directing dispatch of EXACTLY ONE
this iteration (drains a starved backlog one-per-iteration without re-spending the
budget that caused the skips). The counter write is LIGHTWEIGHT (plain locked
atomic JSON, NOT the governed history/changelog path — it updates every precheck)
and FAIL-OPEN: any counter error degrades escalation to a no-op and never blocks
the loop.

The battery deliberately does NOT read the budget meter: the checks are cheap and
read-only, so running them costs almost nothing even under pressure; the meter's
tight-zone `deferrable` drop gates the EXPENSIVE skill invocation and is applied
at DISPATCH time in the SKILL.md (each FIRE line carries its meter_name for that
gate). The escalation OVERRIDES that drop for the single oldest-starved cadence —
repeated drops are the starvation cause, so one forced dispatch per iteration is
the bounded price of breaking it.

Scope (principled — see _cadence_registry docstring): the seven skill-invocation
cadences. l1-skew (0.5g, self-acting board post) and health-regression (0.5h,
DORMANT + multi-step verify/revert) keep their own phases — both sit outside the
skill-invocation-skip starvation class.

Output (guard-424 fail-loud-with-stderr; guard-614 structured on EVERY exit path):
  default — one human line per FIRING cadence + a summary line:
      ▸ CADENCE FIRE: <name> (phase <phase>) meter=<meter_name> → <dispatch>
      [cadence-battery] N fire / M checked
  --json  — {checked_at, registered, fired:[{name,phase,meter_name,dispatch}],
            escalation: null | {threshold, starved:[{name,count}], dispatch_one},
            error?}

Fail-open: any error prints the structured report with an `error` field (also to
stderr, guard-424) and exits 0 — the LLM falls back to the per-phase cadence
checks. The battery must never block the loop.

Invocation (aspirations-precheck Phase 0.5-cadence-battery): the thin wrapper
`bash core/scripts/precheck-cadence-battery.sh` or direct
`py -3 core/scripts/precheck-cadence-battery.py`.
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

_CHECK_TIMEOUT_S = 30

# : consecutive-FIRE count at which a cadence is declared SUSTAINED-
# starved and the battery escalates. 5 = "due and dispatch-skipped five prechecks
# running" — well clear of the normal 1-3 cadences co-firing in a single iteration
# (each of which resets to 0 the moment its ritual IS dispatched, so momentary
# co-firing never reaches the threshold). Env override CADENCE_ESCALATION_THRESHOLD
# for ops tuning; run(threshold=) for tests.
ESCALATION_THRESHOLD = 5
_COUNTER_FILENAME = "cadence-fire-counters.json"


def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _escalation_threshold() -> int:
    """Effective threshold: env override CADENCE_ESCALATION_THRESHOLD (>=1) else
    the module default. Fail-open to the default on any parse error."""
    import os
    raw = os.environ.get("CADENCE_ESCALATION_THRESHOLD", "").strip()
    if raw:
        try:
            n = int(raw)
            if n >= 1:
                return n
        except ValueError:
            pass
    return ESCALATION_THRESHOLD


class _FileCounterStore:
    """Box-local persistent per-cadence consecutive-FIRE counter ().

    Lightweight BY DESIGN: a plain lock-guarded atomic JSON write, NOT the
    governed locked_write_json (history + changelog) path — this file updates on
    every precheck iteration and audit-logging each bump would spam .history.
    Box-local semantics are correct: the ONE reducer that runs this battery lives
    on one box; if it migrates the counter resets and starvation simply re-accrues.
    Every method is FAIL-OPEN — a resolution/read/write error yields an empty map
    or a skipped save, degrading escalation to a no-op, never blocking the loop.
    The lock (guard-multi-Body) is defensive: the runner lease already guarantees
    a single reducer-writer, so contention is structurally near-impossible.
    """

    def __init__(self, path=None):
        self._path = path  # explicit path (tests) else resolved lazily from env

    def _resolve(self):
        if self._path is not None:
            return Path(self._path)
        import os
        agent = os.environ.get("MIND_AGENT", "").strip()
        if not agent:
            return None  # no bound agent (hooks/bg) -> no persistence, fail-open
        from _paths import agent_state_dir
        return agent_state_dir(agent) / _COUNTER_FILENAME

    @staticmethod
    def _read(p) -> dict:
        try:
            if p and p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    # coerce to int, drop anything non-integer (corruption guard)
                    return {k: int(v) for k, v in data.items()
                            if isinstance(v, int) and not isinstance(v, bool)}
        except Exception:
            pass
        return {}

    @staticmethod
    def _write(p, counters: dict) -> None:
        import os
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(counters, handle, indent=2)
            os.replace(tmp, str(p))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def modify(self, mutate_fn) -> dict:
        """Hold the lock ACROSS read+mutate+write (rb: read-then-locked-write is a
        lost-update bug class; the lock must span the whole RMW, not just the
        write, or a concurrent Body loses an increment). `mutate_fn(counters)`
        mutates the map in place. Fully FAIL-OPEN: any error returns {} and the
        escalation no-ops, never blocking the loop. When no agent is bound (hooks /
        bg) the mutate still runs on an ephemeral map so this run's logic works;
        it simply is not persisted (so a hook invocation never accumulates)."""
        try:
            p = self._resolve()
            if p is None:
                counters: dict = {}
                mutate_fn(counters)
                return counters
            p.parent.mkdir(parents=True, exist_ok=True)
            from _fileops import acquire_lock, release_lock
            lock = p.with_suffix(".lock")
            acquire_lock(lock, stale_seconds=10)
            try:
                counters = self._read(p)
                mutate_fn(counters)
                self._write(p, counters)
                return counters
            finally:
                release_lock(lock)
        except Exception:
            return {}  # fail-open: never block the loop on a counter error


def _update_counters(counters: dict, fired_names, noop_names) -> dict:
    """Increment FIRED cadences, RESET NOOP'd ones to 0. Errored checks (in
    NEITHER set this run) keep their prior count — an unreadable check is
    indeterminate, not a non-starved noop. Mutates and returns `counters`."""
    for name in fired_names:
        counters[name] = counters.get(name, 0) + 1
    for name in noop_names:
        counters[name] = 0
    return counters


def _pick_oldest_starved(counters: dict, threshold: int, registry_order):
    """Among cadences at count >= threshold, return the single oldest-starved
    NAME to dispatch: highest count first, felt-sense on a tie, then registry
    order. Returns None when nothing is starved. Pure."""
    starved = [n for n, c in counters.items() if c >= threshold]
    if not starved:
        return None
    index = {name: i for i, name in enumerate(registry_order)}

    def _key(name):
        return (-counters[name],
                0 if name == "felt-sense" else 1,
                index.get(name, len(registry_order)))

    return sorted(starved, key=_key)[0]


def _run_bash(argv, timeout):
    """Run a core/scripts bash script. Return (returncode:int|None, err:str|None).

    returncode None + err set == the check could not run (timeout / missing
    script) — the caller treats it as a noop and surfaces the error so the LLM
    can fall back to the per-phase check. Exit 0 == FIRE, any non-zero == noop.
    """
    script = argv[0]
    from _runtime_bash import bash_cmd  # guard-580 + guard-581
    full = bash_cmd(SCRIPT_DIR / script, *argv[1:])
    try:
        r = subprocess.run(
            full, cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, None
    except Exception as exc:  # timeout, missing script, spawn failure
        return None, f"{script}: {exc}"


def _emit(report: dict, as_json: bool) -> None:
    if report.get("error"):
        # guard-424: cadence/precheck scripts fail LOUD with stderr, never silent.
        print(f"[cadence-battery] {report['error']}", file=sys.stderr)
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
        return
    for e in report.get("fired", []):
        print(
            f"▸ CADENCE FIRE: {e['name']} (phase {e['phase']}) "
            f"meter={e['meter_name']} → {e['dispatch']}"
        )
    esc = report.get("escalation")
    if esc:
        d = esc["dispatch_one"]
        starved_s = ", ".join(f"{s['name']}={s['count']}" for s in esc["starved"])
        print(
            f"⚠⚠ CADENCE STARVATION (g-115-6564): {starved_s} — FIRED >= "
            f"{esc['threshold']}x consecutively without dispatch. Dispatch EXACTLY "
            f"ONE this iteration (oldest-starved): {d['name']} (phase {d['phase']}) "
            f"meter={d['meter_name']} → {d['dispatch']} — UNCONDITIONALLY (override "
            f"the meter-drop; it is the starvation cause). Leave the other FIRE "
            f"lines; they drain one per iteration."
        )
    n_fire = len(report.get("fired", []))
    n_reg = report.get("registered", 0)
    err = f" error={report['error']}" if report.get("error") else ""
    if n_fire == 0 and not report.get("error"):
        print(f"[cadence-battery] all {n_reg} cadence gates noop — nothing to fire")
    else:
        print(f"[cadence-battery] {n_fire} fire / {n_reg} checked{err}")


def run(as_json: bool, check_runner=None, counter_store=None,
        threshold=None) -> int:
    """Run every registered cadence gate check, report the FIRE set, and escalate
    SUSTAINED starvation (g-115-6564).

    check_runner:  injectable (argv, timeout) -> (returncode|None, err|None) for
                   tests. Defaults to the real bash subprocess runner.
    counter_store: injectable object with .load()->dict / .save(dict) for the
                   persistent consecutive-FIRE counter. Defaults to the box-local
                   _FileCounterStore; tests inject an in-memory store so the
                   suite never touches disk.
    threshold:     escalation trigger; defaults to _escalation_threshold().
    """
    runner = check_runner or _run_bash
    report: dict = {"checked_at": _now_iso(), "registered": 0, "fired": [],
                    "escalation": None}
    try:
        from _cadence_registry import cadences
    except Exception as exc:  # fail-open (import env broken)
        report["error"] = f"import_failed: {exc}"
        _emit(report, as_json)
        return 0

    cads = cadences()
    report["registered"] = len(cads)

    check_errors: list[str] = []
    fired_names: list[str] = []
    noop_names: list[str] = []
    for c in cads:
        rc, err = runner(c["check_cmd"], _CHECK_TIMEOUT_S)
        if err is not None:
            check_errors.append(err)
            continue  # broken/timed-out check -> errored (counter untouched)
        if rc == 0:  # FIRE — cadence crossed
            fired_names.append(c["name"])
            report["fired"].append(
                {
                    "name": c["name"],
                    "phase": c["phase"],
                    "meter_name": c["meter_name"],
                    "dispatch": c["fire_dispatch"],
                }
            )
        else:  # any non-zero rc -> clean noop (dispatched OR not-due; non-starved)
            noop_names.append(c["name"])

    if check_errors:
        report["error"] = "check_errors: " + "; ".join(check_errors)

    # SUSTAINED-starvation escalation (). Fully FAIL-OPEN: any counter
    # error is caught and surfaced for telemetry but never blocks the loop.
    try:
        store = counter_store if counter_store is not None else _FileCounterStore()
        n = threshold if threshold is not None else _escalation_threshold()
        registry_names = [c["name"] for c in cads]
        reg_set = set(registry_names)

        def _mutate(counters):
            _update_counters(counters, fired_names, noop_names)
            # Prune stale keys IN THE SAME LOCKED CYCLE: a cadence REMOVED from the
            # registry would otherwise keep a >= N counter forever (it never FIREs
            # or noops again, so it is never reset) and escalate a phantom with "?"
            # fields. Pruning also keeps the counter file self-cleaning.
            for k in [k for k in counters if k not in reg_set]:
                del counters[k]

        counters = store.modify(_mutate)
        oldest = _pick_oldest_starved(counters, n, registry_names)
        if oldest is not None:
            fired_by_name = {e["name"]: e for e in report["fired"]}
            dispatch_one = fired_by_name.get(oldest) or {
                "name": oldest,
                "phase": next((c["phase"] for c in cads
                               if c["name"] == oldest), "?"),
                "meter_name": next((c["meter_name"] for c in cads
                                    if c["name"] == oldest), "?"),
                "dispatch": next((c["fire_dispatch"] for c in cads
                                  if c["name"] == oldest), "?"),
            }
            report["escalation"] = {
                "threshold": n,
                "starved": sorted(
                    [{"name": k, "count": counters[k]}
                     for k in counters if counters[k] >= n],
                    key=lambda d: -d["count"],
                ),
                "dispatch_one": dispatch_one,
            }
    except Exception as exc:  # fail-open — escalation is advisory, never a gate
        report["escalation_error"] = f"{exc}"

    _emit(report, as_json)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit one JSON object")
    args = parser.parse_args()
    return run(args.json)


if __name__ == "__main__":
    sys.exit(main())
