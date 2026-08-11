#!/usr/bin/env python3
"""Cargo-cult detector: auto-extend recurring-goal intervals, else file Idea.

Invoked from recurring-close.sh when a recurring goal's `consecutive_routine`
counter reaches cargo_cult_threshold (default 3, core/config/aspirations.yaml
§ recurring.cargo_cult_threshold). Two modes:

Per-goal (default): py cargo-cult-detector.py <goal-id> [--source world|agent]
    1. Locate the goal in <source>/aspirations.jsonl.
    2. If artifact-producing (keyword match: report/sweep/refresh/...), skip
       filing and reset the counter — routine output is expected, not signal.
    3. Auto-extend: interval_hours * cargo_cult.multiplier (default 1.5),
       capped at cap_ratio * original_interval_hours (default 3.0). Write the
       new interval via aspirations.py update-goal; reset consecutive_routine.
    4. Past the cap: fall through to the Idea path so a human decides whether
       the goal itself should be retired.
    5. Idea path dedupes against "Idea: Extend interval for <goal-id>".

Batch (--audit-all): sweep every recurring goal across world + agent queues,
score by consecutive_routine + signal_ratio, and file ONE ranked Idea
("Batch: Calibrate recurring intervals") on asp-001. Caller (recurring-close.sh)
dedupes against cargo_cult.batch_audit_dedupe_hours before invoking this mode.

Exits 0 on success (extended / filed / dedup hit / dry-run), 1 on failure.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml  # read cargo_cult config block from aspirations.yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import _paths  # noqa: E402
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)
from _owner_qualified_signal import qualified_signal  # noqa: E402  (guard-2107, )

from _gate_log import log as _gate_log

DEFAULT_PRIORITY = "MEDIUM"
DEFAULT_CATEGORY = "framework-maintenance"

# Single source of truth for the batch-Idea title. Used both by the
# emission path (cmd_audit_all) and the self-dedup scan
# (_recent_audit_all_batch). recurring-close.sh has a sibling literal at
# its line ~256; keep them in lockstep until that script imports the
# constant or both move to a shared config key (bravo-355 SSOT finding).
BATCH_AUDIT_TITLE = "Batch: Calibrate recurring intervals"


class SourceUnavailable(Exception):
    """source_path('agent') called without MIND_AGENT bound.

    Replaces a prior `raise SystemExit(...)`. SystemExit was catchable by the
    `cmd_audit_all` try/except but also swallowed any unrelated sys.exit(1)
    from downstream code — exactly the fail-open-hides-bugs anti-pattern.
    Keep this exception narrow: raise ONLY for the MIND_AGENT-unbound case.
    """

# Artifact-producing goal signatures ( fix): recurring goals whose
# purpose is to generate a durable artifact (report, sweep, refresh, inventory
# snapshot) — NOT to detect pattern changes. A "routine" outcome on these
# goals means "artifact generated normally," NOT "no actionable signal."
# Cargo-cult semantics conflate the two and file false-positive extend-interval
# Ideas. Suppress filing for these goals; still reset consecutive_routine so
# the detector does not re-fire every cycle.
# Distinct from  (filing override) — this is the CLASSIFICATION gap
# upstream of that fix. Added 2026-04-21 via  (bravo session 55).
ARTIFACT_PRODUCING_KEYWORDS = (
    "report",
    "sweep",
    "refresh",
    "snapshot",
    "inventory",
    "flush",
    "archive",
    #  (classifier-staleness review , 2026-07-08): the 5 below
    # are recurring goal shapes that produce a durable artifact EVERY fire yet
    # missed all 7 original keywords — a ~15-25% FN that wrongly flagged
    # legitimate artifact-producers as cargo-cult (a routine close on them is
    # normal, not going-through-motions). Each verb is UNAMBIGUOUS — it never
    # conflates produce-with-detect — so it is safe in the keyword list.
    "reflect",   # "Reflect and journal" -> journal + reasoning-bank entries
    "replay",    # "Hippocampal replay" -> reconsolidation / rb records
    "drain",     # "Drained temp" -> temp-store cleanup artifacts
    "evict",     # "evict-aged-terminal" -> aspirations state mutation
    "reap",      # "session-telemetry-reap" -> telemetry records
    #  (classifier-accuracy scan , 2026-07-15): the hypothesis
    # NOUN reliably signals a durable pipeline-record producer. Empirical blast-
    # radius scan of every recurring goal across all queues found 9 net-new
    # True-flips — 8 clear producers ("Review and resolve hypotheses" x5 agents,
    # "Learn from resolved hypotheses", "Generate hypotheses from recent work")
    # + 1 audit-trail producer ( Tree contradiction audit, which emits
    # an inspection trail every fire); 0 harmful FP. Matches the NOUN, not the
    # ambiguous "resolve"/"review" verbs. Prior sibling fix:  (the 5
    # verbs above). Low FP: a recurring goal naming hypotheses reliably touches
    # the pipeline store; a rare pure-monitor exception opts out via the flag.
    "hypothes",  # "Generate/Review/Resolve hypotheses" -> pipeline records
    # "analyze"/"analysis" intentionally NOT here — they conflate
    # artifact-producing ("analyze + emit report") with detection ("analyze
    # health"). Genuinely artifact-producing analyze-titled goals must opt in
    # via the explicit `artifact_producing: true` flag (see L91-93). The SAME
    # exclusion (conflates produce-vs-check) applies to the VERBS "resolve"/
    # "detect"/"monitor": recurring goals under those verbs that genuinely
    # produce artifacts should set the explicit flag rather than widen this list
    # with an ambiguous verb. ("hypothesis" was formerly excluded here too, but
    #  promoted it to a keyword above — as a NOUN it reliably signals a
    # pipeline producer, unlike those verbs; see the dated note.)
)


def is_artifact_producing(goal: dict) -> tuple[bool, str | None]:
    """Return (True, matched_keyword) if goal title/description signals artifact generation.

    Heuristic — errs toward suppression. If the flag `artifact_producing`
    is explicitly set on the goal, honor it. Otherwise keyword-match the
    title (primary) and description (secondary) against ARTIFACT_PRODUCING_KEYWORDS.
    The explicit flag takes precedence; this lets future goals opt-out of
    heuristic suppression when the keyword match would be wrong.
    """
    explicit = goal.get("artifact_producing")
    if explicit is True:
        return True, "artifact_producing:true"
    if explicit is False:
        return False, None
    haystack = f"{goal.get('title','')} {goal.get('description','')}".lower()
    for kw in ARTIFACT_PRODUCING_KEYWORDS:
        if kw in haystack:
            return True, kw
    return False, None


def source_path(source: str, agent_override: str | None = None) -> Path:
    # `source` is constrained to {"world", "agent"} by argparse — no else branch needed.
    if source == "world":
        return _paths.WORLD_DIR / "aspirations.jsonl"
    agent = agent_override or os.environ.get("MIND_AGENT", "")
    if not agent:
        raise SourceUnavailable("cargo-cult-detector: --source agent requires MIND_AGENT")
    return _paths.agent_dir(agent) / "aspirations.jsonl"


def discover_agents() -> list:
    """Glob agent directories from PROJECT_ROOT/*/local-paths.conf.

    Mirrors experience-reconcile.discover_agents and recovery-gate.sh
    pattern. Used by --audit-all so agent-side rows can be scanned across
    every agent's queue (not just MIND_AGENT's). Sorted for stable output.

    g-115-569: pre-fix the audit only saw MIND_AGENT's agent-queue, so
    a calibrator running zeta would miss alpha-owned routine goals
    entirely. With cross-agent scan + owning-agent tag in each row,
    calibrators can route to the actual owner via --source agent +
    MIND_AGENT=<owner>.
    """
    return sorted(
        p.parent.name
        for p in _paths.enumerate_agent_confs()
        if p.is_file()
    )


def find_goal(src: Path, goal_id: str) -> tuple[dict, dict] | None:
    """Return (asp, goal) for the goal-id, or None if not found."""
    with src.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            asp = json.loads(line)
            for g in asp.get("goals", []):
                if g.get("id") == goal_id:
                    return asp, g
    return None


def already_filed(asp: dict, dedup_title: str) -> str | None:
    """Return the goal_id if an unresolved Idea with this title already exists."""
    for g in asp.get("goals", []):
        if g.get("title") != dedup_title:
            continue
        # Skip if already terminal (completed/skipped/expired) — stale Ideas
        # don't block new ones; only pending/in-progress/blocked still gate.
        if g.get("status") in ("completed", "skipped", "expired"):
            continue
        return g.get("id")
    return None


def build_idea(goal_id: str, title: str, interval_hours: int, consecutive: int,
               now: datetime, source: str = "world") -> dict:
    description = (
        f"Auto-filed by cargo-cult-detector after recurring goal {goal_id} "
        f"({title}) returned routine outcome {consecutive} times in a row.\n\n"
        f"Current interval_hours: {interval_hours}h\n"
        f"Consecutive routine count: {consecutive}\n\n"
        f"Proposal: evaluate whether to\n"
        f"  (a) extend interval_hours so the goal fires less often,\n"
        f"  (b) change the skill/args because the current one finds nothing actionable,\n"
        f"  (c) retire the goal as cargo-cult — it's not producing learning signal.\n\n"
        f"This Idea was filed because the loop is spending tokens running a "
        f"recurring cycle that consistently returns no actionable work."
    )
    return {
        "title": f"Idea: Extend interval for {goal_id}",
        "status": "pending",
        "priority": DEFAULT_PRIORITY,
        "skill": None,
        "participants": ["agent"],
        "category": DEFAULT_CATEGORY,
        "description": description,
        "verification": {
            "outcomes": [
                "Interval evaluated — extended, skill changed, or goal retired",
            ],
            "checks": [],
        },
        "blocked_by": [],
        # origin-signal-gate: cargo-cult detection cites the source goal
        # (the one that returned too many routine outcomes in a row).
        # guard-2107 / : `idea:` is the BROADEST of this family — no
        # discriminating middle segment at all — so a bare agent-queue
        # `g-001-NN` here is ambiguous to any cross-queue reader
        # (`aspirations-query.sh --goal-field origin_signal` spans world + the
        # bound agent). This detector's OWN dedup is title-based, so qualifying
        # changes no dedup behaviour here; it removes the ambiguity for external
        # readers. The calibrator invokes this path as
        # `MIND_AGENT=<owning_agent> --source agent`, so the env owner is the
        # goal's real owner, not merely whoever is running.
        "origin_signal": qualified_signal("idea:", goal_id, source),
        "tags": ["cargo-cult", "auto-filed", f"source-goal:{goal_id}"],
        "created_at": now.replace(microsecond=0).isoformat(),
    }


def file_idea(asp_id: str, source: str, idea: dict) -> str | None:
    """File a goal via the daemon; returns the new goal id (or None on failure).

    Auto-injects --override-duplication: the detector's prose necessarily
    overlaps the source goal's own recent routine-close summaries, so
    goal-duplication-gate rejects every filing unless the override fires.
    The override justification is the finding itself — pattern repetition
    IS what the detector surfaces. See g-248-24 for the 4-instance pattern.
    """
    override = "cargo-cult-alert: pattern repetition IS the finding"
    try:
        record = _rt.aspirations_add_goal(
            asp_id, idea, source=source,
            overrides={"Duplication": override})
    except _rt.RtError as e:
        sys.stderr.write(
            f"cargo-cult-detector: add-goal failed: "
            f"{(e.body or str(e)).strip()[:400]}\n")
        return None
    # Daemon response shape: top-level has "goal_id"; legacy CLI returned the
    # goal dict with "id". Read "goal_id" first, fall back to "id" — same
    # defensive shape as blocker-recheck.py (rb-1041 / ).
    return record.get("goal_id") or record.get("id")


def _load_detector_config() -> dict:
    """Read cargo_cult block from core/config/aspirations.yaml.

    Returns {multiplier, cap_ratio} with defaults (1.5, 3.0) if the block or
    file is missing. The detector never needs to refuse to run — it either
    auto-extends within cap, or escalates to the Idea path when the cap hits.
    """
    defaults = {"multiplier": 1.5, "cap_ratio": 3.0}
    cfg_path = _paths.CONFIG_DIR / "aspirations.yaml"
    if not cfg_path.exists():
        return defaults
    try:
        with cfg_path.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        sys.stderr.write(f"cargo-cult-detector: config read failed ({e}); using defaults\n")
        return defaults
    block = cfg.get("cargo_cult") or {}
    return {**defaults, **block}


def _load_contract_config() -> dict:
    """Read contract block from recurring section of aspirations.yaml.

    Origin: LifingPolls plan item 4 (2026-05-08). Symmetric to
    _load_detector_config but reads from `recurring:` block (where the
    contract knobs live alongside cargo_cult_threshold).
    """
    defaults = {
        "deep_streak_contract_threshold": 3,
        "deep_streak_contract_divisor": 1.5,
        "contract_floor_ratio": 0.33,
        "contract_suppress_window": 5,
        "contract_suppress_min_samples": 3,
    }
    cfg_path = _paths.CONFIG_DIR / "aspirations.yaml"
    if not cfg_path.exists():
        return defaults
    try:
        with cfg_path.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return defaults
    block = cfg.get("recurring") or {}
    return {
        "deep_streak_contract_threshold": int(block.get(
            "deep_streak_contract_threshold",
            defaults["deep_streak_contract_threshold"])),
        "deep_streak_contract_divisor": float(block.get(
            "deep_streak_contract_divisor",
            defaults["deep_streak_contract_divisor"])),
        "contract_floor_ratio": float(block.get(
            "contract_floor_ratio",
            defaults["contract_floor_ratio"])),
        "contract_suppress_window": int(block.get(
            "contract_suppress_window",
            defaults["contract_suppress_window"])),
        "contract_suppress_min_samples": int(block.get(
            "contract_suppress_min_samples",
            defaults["contract_suppress_min_samples"])),
    }


def _load_streak_mult() -> float:
    """Load recurring.streak_mult from core/config/aspirations.yaml.

    Same shared knob as streak-break-reflector.py::_load_streak_mult and
    aspirations_write.py cmd_complete_by (g-115-929 consolidation): the
    multiplier that DEFINES a streak-break also defines "actual cadence far
    exceeds interval" for the contract-suppression predicate below. Fail-open:
    any read/parse error falls back to 2.0 (the historical literal).
    """
    cfg_path = _paths.CONFIG_DIR / "aspirations.yaml"
    try:
        with cfg_path.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        v = (cfg.get("recurring") or {}).get("streak_mult")
        if v is None:
            return 2.0
        return float(v)
    except Exception:
        return 2.0


def _recent_actual_cadence(goal_id: str, window: int, min_samples: int,
                           log_path: Path | None = None
                           ) -> tuple[float | None, str, int]:
    """Median of the last `window` actual_elapsed_hours for goal_id from
    <agent>/session/streak-breaks.jsonl (written by aspirations_write
    cmd_complete_by on every late fire).

    Returns (median, status, samples):
      status "ok"           — median computed over `samples` entries
      status "insufficient" — fewer than min_samples entries (median None).
                              Absence of break records is a MEANINGFUL
                              negative: the goal fires on time, so
                              contraction is legitimate. NOT an error.
      status "no-agent"     — MIND_AGENT unbound (median None; same as
                              insufficient for the caller)
      status "unreadable"   — the file exists but could not be READ at all
                              (median None). guard-487: a suppression gate
                              whose input cannot be parsed fails CLOSED —
                              the caller must treat this as suppress, not
                              as "no lateness evidence".

    Per-line corruption in the append-only JSONL is skipped with a loud
    stderr WARN (standard JSONL posture) — only whole-file read failure
    escalates to "unreadable". The log_path param is a test-only override;
    production always derives from _paths.AGENT_DIR.
    """
    import statistics

    if log_path is None:
        if _paths.AGENT_DIR is None:
            return None, "no-agent", 0
        log_path = _paths.AGENT_DIR / "session" / "streak-breaks.jsonl"
    if not log_path.exists():
        return None, "insufficient", 0
    values: list[float] = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    sys.stderr.write(
                        "cargo-cult-detector: WARN skipping corrupt "
                        "streak-breaks.jsonl line\n")
                    continue
                if rec.get("goal_id") != goal_id:
                    continue
                v = rec.get("actual_elapsed_hours")
                if isinstance(v, (int, float)) and v > 0:
                    values.append(float(v))
    except OSError as e:
        sys.stderr.write(
            f"cargo-cult-detector: WARN streak-breaks.jsonl unreadable "
            f"({e}) — treating contract-suppression input as unknown "
            f"(guard-487 fail-CLOSED)\n")
        return None, "unreadable", 0
    recent = values[-window:]
    if len(recent) < min_samples:
        return None, "insufficient", len(recent)
    return statistics.median(recent), "ok", len(recent)


# Floor-hit Idea title (pre-existing escalation) — the rebase-UP dedup must
# also honor a pending one of these so the two escalation shapes never stack
# on the same goal.
FLOOR_HIT_TITLE_FMT = "Idea: Rebase original interval for {goal_id}"
REBASE_UP_TITLE_FMT = "Idea: Rebase interval UP for {goal_id} (selection-gated)"


def _file_rebase_up_idea(asp: dict, asp_id: str, source: str, goal_id: str,
                         title: str, interval_hours: float,
                         actual_median: float, samples: int,
                         consecutive_deep: int, streak_mult: float,
                         dry_run: bool) -> int:
    """File/refresh ONE deduped rebase-UP Idea for a suppression-suppressed
    contraction (regime-c selection-gated goal). Dedups against BOTH the
    rebase-UP title and the pre-existing floor-hit title.
    """
    dedup_title = REBASE_UP_TITLE_FMT.format(goal_id=goal_id)
    floor_title = FLOOR_HIT_TITLE_FMT.format(goal_id=goal_id)
    existing = already_filed(asp, dedup_title) or already_filed(asp, floor_title)
    if existing:
        print(
            f"[cargo-cult-contract] dedup hit — rebase Idea {existing} already "
            f"pending for {goal_id} on {asp_id}; skipping"
        )
        return 0

    idea = {
        "title": dedup_title,
        "status": "pending",
        "priority": DEFAULT_PRIORITY,
        "skill": None,
        "participants": ["agent"],
        "category": DEFAULT_CATEGORY,
        "description": (
            f"Auto-filed by cargo-cult-detector contract-suppression: recurring "
            f"goal {goal_id} ({title}) reached the deep-streak contract "
            f"threshold (consecutive_deep={consecutive_deep}), but its recent "
            f"ACTUAL firing cadence (median {actual_median:.2f}h over the last "
            f"{samples} recorded streak-breaks) far exceeds interval_hours "
            f"({interval_hours}h x streak_mult {streak_mult:g} = "
            f"{streak_mult * float(interval_hours):.2f}h boundary). This is the "
            f"regime-c selection-gated shape (rb-1558/rb-2475): the goal fires "
            f"when the selector serves it, not when the timer elapses, so "
            f"CONTRACTING the interval cannot raise the firing rate — it only "
            f"manufactures streak-break noise and floor-hit escalations.\n\n"
            f"Proposal: rebase interval_hours (and original_interval_hours) UP "
            f"toward the actual cadence (~{actual_median:.1f}h), then reset the "
            f"streak state. Per guard-1015, any interval_hours write outside "
            f"cargo-cult-detector's update_interval_hours() MUST persist "
            f"original_interval_hours in the same write when absent. "
            f"Discriminator context: recurring-cadence-mechanics tree node "
            f"(three floor-hit regimes; this is regime c: actual>>interval + "
            f"genuine deeps -> rebase UP)."
        ),
        "verification": {
            "outcomes": [
                "interval_hours (+ original_interval_hours anchor) rebased "
                "toward actual cadence, or a decision recorded not to",
            ],
            "checks": [], "preconditions": [],
        },
        "blocked_by": [],
        # guard-2107 /  — same reasoning as build_idea's `idea:` key.
        # `source` is already a parameter of this function, so the owner comes
        # from the caller's routing rather than from ambient state.
        "origin_signal": qualified_signal(
            "investigate:contract-suppressed:", goal_id, source),
        "tags": ["cargo-cult", "auto-filed", "regime-c",
                 f"source-goal:{goal_id}"],
        "created_at": datetime.now().replace(microsecond=0).isoformat(),
    }

    if dry_run:
        print(f"[cargo-cult-contract] DRY-RUN — would file rebase-UP Idea on {asp_id}:")
        print(json.dumps(idea, indent=2))
        return 0

    new_id = file_idea(asp_id, source, idea)
    if not new_id:
        return 1
    print(
        f"[cargo-cult-contract] filed {new_id} on {asp_id}: '{dedup_title}' "
        f"(actual median {actual_median:.2f}h vs interval {interval_hours}h)"
    )
    return 0


def update_interval_hours(goal_id: str, source: str,
                          new_interval: float, original_interval: float,
                          had_original: bool) -> bool:
    """Set interval_hours=new_interval on the goal, and original_interval_hours=original
    if the goal didn't already carry one.

    CRITICAL ORDERING — do not reorder: original_interval_hours is the provenance
    anchor for cap_ratio. If interval_hours were written first and the original
    write then failed, the next auto-extension would read orig_stored=None and
    treat the already-extended value as "original" — ratcheting the cap upward
    each retry. Writing original first means a failure leaves the goal in its
    pre-extension state, safely recoverable on the next trip.
    """
    if not had_original:
        cmd_original = [
            sys.executable, str(HERE / "aspirations.py"),
            "--source", source, "update-goal",
            goal_id, "original_interval_hours", str(original_interval),
        ]
        r1 = subprocess.run(cmd_original, capture_output=True, text=True, encoding="utf-8")
        if r1.returncode != 0:
            sys.stderr.write(
                f"cargo-cult-detector: original_interval_hours write failed: {r1.stderr}"
            )
            return False
    cmd_interval = [
        sys.executable, str(HERE / "aspirations.py"),
        "--source", source, "update-goal",
        goal_id, "interval_hours", str(new_interval),
    ]
    r2 = subprocess.run(cmd_interval, capture_output=True, text=True, encoding="utf-8")
    if r2.returncode != 0:
        sys.stderr.write(f"cargo-cult-detector: interval_hours update failed: {r2.stderr}")
        return False
    return True


def reset_consecutive_deep(goal_id: str, source: str) -> bool:
    """Reset consecutive_deep to 0 after a contract fires.

    Origin: LifingPolls plan item 4 (2026-05-08). Symmetric to
    reset_consecutive_routine — same write semantics, different field.
    """
    cmd = [
        sys.executable, str(HERE / "aspirations.py"),
        "--source", source, "update-goal",
        goal_id, "consecutive_deep", "0",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        sys.stderr.write(
            f"cargo-cult-detector: failed to reset consecutive_deep: {result.stderr}"
        )
        return False
    return True


def reset_consecutive_routine(goal_id: str, source: str) -> bool:
    """Reset consecutive_routine to 0 so the detector won't re-fire next cycle.

    Invokes aspirations.py update-goal directly via sys.executable — bypasses
    `bash` to avoid Windows shell-resolution picking the wrong bash binary
    (observed: subprocess.run(['bash', ...]) launched a non-Git-Bash shell
    that rejected `set -o pipefail` in the wrapper script).
    """
    cmd = [
        sys.executable, str(HERE / "aspirations.py"),
        "--source", source, "update-goal",
        goal_id, "consecutive_routine", "0",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        sys.stderr.write(
            f"cargo-cult-detector: failed to reset consecutive_routine: {result.stderr}"
        )
        return False
    return True


def _iter_recurring_goals(source: str, agent_override: str | None = None):
    """Yield (asp, goal) tuples for every recurring goal in the given source.

    Matches find_goal() iteration but returns ALL recurring entries instead of
    stopping at the first match. Used by --audit-all for batch calibration.
    `agent_override` lets the caller scan a specific agent's queue when
    source="agent" (g-115-569 — cross-agent audit-all).
    """
    src = source_path(source, agent_override=agent_override)
    if not src.exists():
        return
    with src.open("r", encoding="utf-8") as f:
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
                if g.get("status") in ("completed", "skipped", "expired"):
                    # Completed recurring goals DO exist in the live store
                    # (they reset to pending via aspirations-recover-recurring),
                    # but the audit only cares about active-class goals. Skip
                    # terminal to avoid double-counting stale history.
                    continue
                yield asp, g


def _score_recurring(goal: dict) -> dict:
    """Compute a cargo-cult-lite score for a recurring goal.

    Signal-per-fire ratio is approximated by:
      signal_ratio ≈ 1 - min(consecutive_routine, achievedCount) / achievedCount
    i.e. "fraction of recent fires that produced deep outcomes."

    Score formula (higher = more suspect):
      score = consecutive_routine_weight * (consecutive_routine / threshold)
            + routine_ratio_weight      * (1 - signal_ratio)
            + streak_weight             * (current_streak of routines)

    Where the current_streak approximation falls back to consecutive_routine
    when a dedicated streak counter is absent on the goal.
    """
    ach = int(goal.get("achievedCount") or 0)
    cons = int(goal.get("consecutive_routine") or 0)
    interval_h = float(goal.get("interval_hours") or 0.0)
    if ach <= 0:
        signal_ratio = 1.0     # no data → assume productive (avoid false bumps)
    else:
        signal_ratio = max(0.0, 1.0 - min(cons, ach) / ach)

    # : lifetime substantive-hit rate — the SECOND, independent signal
    # the consecutive_routine streak misses. substantive_runs is the tracked
    # denominator (counted from field-introduction by recurring-close.sh, NOT
    # achievedCount, so legacy run history never poisons the rate); substantive_hits
    # is the genuine-deep numerator. No tracked data → rate 1.0 (assume productive,
    # mirroring signal_ratio's no-data default; cmd_audit_all's chronic gate also
    # requires substantive_runs >= chronic_min_runs, so a 0-run goal never flags).
    sub_hits = int(goal.get("substantive_hits") or 0)
    sub_runs = int(goal.get("substantive_runs") or 0)
    if sub_runs <= 0:
        lifetime_hit_rate = 1.0
    else:
        lifetime_hit_rate = max(0.0, min(1.0, sub_hits / sub_runs))

    # Weights chosen so consecutive_routine dominates when it's near threshold,
    # but low signal_ratio amplifies the suspicion. Interval enters via rank
    # tie-breaking only (shorter interval = bumped higher, all else equal).
    # The chronic-low boost (a function of lifetime_hit_rate) is applied by the
    # caller (cmd_audit_all) where the chronic thresholds are read — keeping this
    # scorer config-free and the chronic policy single-sourced.
    score = cons * 2.0 + (1.0 - signal_ratio) * 1.5
    return {
        "goal_id": goal.get("id"),
        "title": goal.get("title", "")[:80],
        "interval_hours": interval_h,
        "achievedCount": ach,
        "consecutive_routine": cons,
        "signal_ratio": round(signal_ratio, 3),
        "substantive_hits": sub_hits,
        "substantive_runs": sub_runs,
        "lifetime_hit_rate": round(lifetime_hit_rate, 3),
        "last_substantive_at": goal.get("last_substantive_at"),
        "score": round(score, 2),
    }


def _propose_new_interval(goal: dict, cfg: dict) -> float | None:
    """Suggest a new interval_hours for a suspect recurring goal.

    Reads cargo_cult.multiplier and cargo_cult.cap_ratio from cfg so the
    batch-audit recommendation matches what the per-goal auto-extend path
    would have written. Single source of truth — DO NOT hardcode numbers
    here. Returns None if we lack the data to propose.
    """
    # : review-ritual recurring goals (fresh-eyes / felt-sense / l1-skew /
    # curriculum cadences, strategic-scan) fire at a DELIBERATE cadence — a routine
    # "nothing to report" close IS their value, not a signal to stretch the interval.
    # An explicit calibration_exempt flag opts a goal out of auto-extension entirely,
    # so a review ritual is never batch-extended into irrelevance.
    if goal.get("calibration_exempt"):
        return None
    interval_h = goal.get("interval_hours")
    if interval_h is None or interval_h <= 0:
        return None
    multiplier = float(cfg.get("multiplier", 1.5))
    cap_ratio = float(cfg.get("cap_ratio", 3.0))
    # original_interval_hours is now persisted at the aspirations.py update-goal
    # chokepoint on EVERY interval_hours write — per-goal, BATCH-CALIBRATE, and
    # MANUAL apply paths alike ( anchor-write fix). So the `or interval_h`
    # fallback below is now only a legacy-goal safety net (goals last extended before
    # the chokepoint fix), NOT the unbounded-ratchet hole it used to be: fresh
    # extensions always read a real anchor, so cap = cap_ratio x TRUE original and
    # can no longer ratchet upward on each batch apply.
    original = float(goal.get("original_interval_hours") or interval_h)
    proposed = min(float(interval_h) * multiplier, original * cap_ratio)
    return proposed if proposed > interval_h else None


def _recent_audit_all_batch(hours: float) -> bool:
    """Self-side temporal dedup gate ().

    Scan world + agent queues for ANY goal titled BATCH_AUDIT_TITLE created
    within the last `hours`, REGARDLESS of status. Defense-in-depth against
    the recurring-close.sh caller-side dedup gap: that gate filters by
    status in (completed, skipped, expired) — once a prior batch closes,
    the next routine cycle re-fires --audit-all with the same 15
    candidates. Bravo session 58 saw three duplicate batches in 4
    iterations (g-248-64 → g-001-187 → g-001-188); the second and third
    landed on signal the first had already covered.

    Returns True iff a batch goal exists with created_at >= (now - hours).
    """
    if hours <= 0:
        return False
    cutoff = datetime.now() - timedelta(hours=hours)
    # : scan world + EVERY agent queue (not just the current agent). The
    # batch goal files to world () by default, so the world scan already dedups
    # cross-agent for the common path; enumerating all agent queues via discover_agents()
    # closes the rare fallback where  is absent and the batch lands in one agent's
    # queue — mirrors cmd_audit_all's scan_passes so dedup coverage == emission coverage.
    scan_targets: list[tuple[str, str | None]] = [("world", None)]
    for ag in discover_agents():
        scan_targets.append(("agent", ag))
    for source, agent_override in scan_targets:
        try:
            p = source_path(source, agent_override=agent_override)
        except SourceUnavailable:
            # MIND_AGENT not bound (cmd-line/tests) — agent queue unreachable.
            continue
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    asp = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for g in asp.get("goals", []):
                    if g.get("title") != BATCH_AUDIT_TITLE:
                        continue
                    ca = g.get("created_at")
                    if not ca:
                        continue
                    try:
                        if datetime.fromisoformat(str(ca)) >= cutoff:
                            return True
                    except (ValueError, TypeError):
                        continue
        except OSError:
            # Read failure on one source should not crash the detector;
            # let the other source decide. Fail-open: treat as no-match.
            continue
    return False


def cmd_audit_all(args, cfg) -> int:
    """Sweep every recurring goal across both sources and emit ONE batch Idea.

    ADVISORY ONLY — this function builds a description table; it does NOT
    write interval_hours. Interval writes happen through the per-goal path
    (main()'s auto-extend branch) so there is exactly one write site.
    Do not add update_interval_hours calls here without reconciling with
    the per-goal path's cap accounting.

    Caller-dedupe: recurring-close.sh checks cargo_cult.batch_audit_dedupe_hours
    against an existing pending "Batch: Calibrate recurring intervals" Idea
    before invoking --audit-all. That gate filters by status — completed
    batches don't suppress the next firing. This function adds a self-side
    gate (audit_all_batch_dedup_hours) that checks ANY status within the
    window, closing the duplicate-batch hole observed in bravo session 58
    (g-248-74).
    """
    # cfg is the cargo_cult sub-dict (see _load_detector_config), not the
    # parent aspirations.yaml — read keys directly without re-indexing.
    self_dedup_hours = float(cfg.get("audit_all_batch_dedup_hours", 0))
    #  chronic-low thresholds (single-sourced here so _score_recurring
    # stays config-free).
    chronic_threshold = float(cfg.get("chronic_hit_rate_threshold", 0.15))
    chronic_min_runs = int(cfg.get("chronic_min_runs", 8))
    chronic_weight = float(cfg.get("chronic_score_weight", 2.0))
    if self_dedup_hours > 0 and _recent_audit_all_batch(self_dedup_hours):
        print(
            f"[cargo-cult audit-all] self-dedup HIT — batch goal landed "
            f"within {self_dedup_hours:g}h; skipping emission"
        )
        # gate_id MUST match core/config/gates.yaml id.
        _gate_log(
            "cargo-cult-detector",
            "block",
            trigger_matched="audit_all_self_dedup",
            extra={
                "would_block": True,
                "decision_path": "audit-all-self-dedup",
                "self_dedup_hours": self_dedup_hours,
            },
        )
        return 0

    now = datetime.now()
    candidates = []
    # World pass: single queue, no owning-agent (the world queue is shared).
    # Agent pass: iterate every agent dir ( — pre-fix the audit only
    # saw MIND_AGENT's queue, so a calibrator running on one agent missed
    # the routine-goal data in the other agents' queues).
    scan_passes: list[tuple[str, str | None]] = [("world", None)]
    for ag in discover_agents():
        scan_passes.append(("agent", ag))

    for source, agent_override in scan_passes:
        try:
            items = list(_iter_recurring_goals(source, agent_override=agent_override))
        except SourceUnavailable:
            # agent source requires either MIND_AGENT or an explicit override;
            # skip silently if missing. Narrow catch — any OTHER exception
            # (sys.exit from downstream, I/O errors, etc.) MUST propagate so
            # fail-open doesn't hide bugs.
            items = []
        for asp, goal in items:
            # Respect the artifact-producing suppression from the per-goal path.
            is_artifact, _ = is_artifact_producing(goal)
            if is_artifact:
                continue
            scored = _score_recurring(goal)
            # : chronic-low lifetime-rate flag + rank boost. A goal with
            # enough TRACKED closes but a low lifetime substantive rate is suspect
            # even when its consecutive_routine streak is short — the streak-based
            # score alone ranks it near zero and the >=1 filter below would drop
            # it. Flag it and boost its score so it surfaces in the batch.
            chronic = (scored["substantive_runs"] >= chronic_min_runs
                       and scored["lifetime_hit_rate"] < chronic_threshold)
            scored["chronic"] = chronic
            if chronic:
                scored["score"] = round(
                    scored["score"] + chronic_weight * (1.0 - scored["lifetime_hit_rate"]),
                    2,
                )
            proposed = _propose_new_interval(goal, cfg)
            if proposed is None:
                continue
            scored["proposed_interval_hours"] = proposed
            scored["source"] = source
            scored["asp_id"] = asp.get("id")
            # owning_agent is None for world rows (shared queue) and the
            # agent name for agent-side rows. Calibrators read this to
            # route via --source agent + MIND_AGENT=<owning_agent>.
            scored["owning_agent"] = agent_override
            candidates.append(scored)

    # Rank by score desc, take top slice. Empty → no-op.
    candidates.sort(key=lambda c: c["score"], reverse=True)
    # : include chronic-low goals even when consecutive_routine == 0 —
    # the lifetime-rate signal is the whole point of the extension (a goal that
    # never strings 3 routines together but is chronically low-signal would
    # otherwise never be surfaced by the streak-only filter).
    top = [c for c in candidates
           if c["consecutive_routine"] >= 1 or c.get("chronic")][:15]
    if not top:
        print("[cargo-cult audit-all] no suspect recurring goals with consecutive_routine >= 1")
        _gate_log(
            "cargo-cult-detector",
            "noop",
            extra={
                "would_block": False,
                "decision_path": "audit-all-no-candidates",
                "candidates_total": len(candidates),
            },
        )
        return 0

    # Build the batch table as a markdown-rendered description.
    lines = [
        "Auto-filed by cargo-cult-detector --audit-all. Batch review of "
        "recurring-goal intervals across world + agent queues.\n",
        "",
        "| Goal | Scope | Interval → Proposed | achievedCount | cons_routine | signal_ratio | lifetime_rate | Score |",
        "|------|-------|---------------------|---------------|--------------|--------------|---------------|-------|",
    ]
    for c in top:
        # Tag agent-side rows with owning agent so calibrators know which
        # agent's queue holds the data (). World rows stay
        # unchanged: shared queue, no owning agent.
        # Scope column ( / guard-762): make the apply policy explicit
        # so the claiming agent does not have to infer ownership from the label.
        # world queue -> "shared" (any claimer may direct-apply); agent queue ->
        # "owned:<agent>" (the owner direct-applies; another claimer must
        # surface-to-owner via the board, never unilaterally rewrite a partner's
        # recurring cadence).
        if c.get("owning_agent"):
            src_label = f"agent: {c['owning_agent']}"
            scope_label = f"owned:{c['owning_agent']}"
        else:
            src_label = c["source"]
            scope_label = "shared"
        # : surface the lifetime rate; tag chronic rows so the reviewer
        # sees WHY a low-cons_routine goal was pulled into the batch.
        lifetime_cell = f"{c['lifetime_hit_rate']}"
        if c.get("chronic"):
            lifetime_cell += f" [chronic {c['substantive_hits']}/{c['substantive_runs']}]"
        lines.append(
            f"| {c['goal_id']} ({src_label}) — {c['title']} "
            f"| {scope_label} "
            f"| {c['interval_hours']:g}h → {c['proposed_interval_hours']:g}h "
            f"| {c['achievedCount']} "
            f"| {c['consecutive_routine']} "
            f"| {c['signal_ratio']} "
            f"| {lifetime_cell} "
            f"| {c['score']} |"
        )
    lines += [
        "",
        "Scope legend (g-115-1448 / guard-762): **shared** rows (world queue) "
        "and **owned:<you>** rows are DIRECT-APPLY. An **owned:<other-agent>** "
        "row is that agent's recurring cadence (part of their operating rhythm, "
        "not a shared-resource parameter) -- SURFACE it to the owner via the "
        "coordination/findings board; do NOT unilaterally rewrite another "
        "agent's recurring interval.",
        "",
        "For each row: consider (a) extending interval to proposed, (b) "
        "changing skill/args if the goal is finding nothing actionable, or "
        "(c) retiring if it produces no learning signal.",
        "",
        "A **[chronic hits/runs]** tag (g-317-02) marks a goal flagged on its "
        "LIFETIME substantive rate (genuine-deep closes / tracked closes), not a "
        "consecutive-routine streak. Chronic rows are the strongest retirement/"
        "recalibration candidates: little real signal over many runs, yet they "
        "never tripped the consecutive-routine trigger.",
        "",
        "This batch replaces the per-goal Idea filing to collapse "
        "symptom-chasing into one calibration pass. See "
        "world/conventions/goal-schemas.md (recurring) and "
        "core/config/aspirations.yaml § cargo_cult.",
    ]
    description = "\n".join(lines)

    dedup_title = BATCH_AUDIT_TITLE
    idea = {
        "title": dedup_title,
        "status": "pending",
        "priority": DEFAULT_PRIORITY,
        "skill": None,
        "participants": ["agent"],
        "category": DEFAULT_CATEGORY,
        "description": description,
        "verification": {
            "outcomes": [
                f"All {len(top)} candidate recurring goals reviewed; "
                f"interval/skill/retirement decisions applied per row"
            ],
            "checks": [],
        },
        "blocked_by": [],
        "origin_signal": "idea:cargo-cult-audit-all",
        "tags": ["cargo-cult", "batch-audit", "auto-filed"],
        "created_at": now.replace(microsecond=0).isoformat(),
    }

    # File on the framework-maintenance aspiration ( by convention).
    # When it doesn't exist, fall back to the highest-cons_routine goal's
    # parent — at least the batch lands somewhere readable.
    target_asp = "asp-001"
    target_source = "world"
    # Verify  exists in the world queue before using it.
    world_src = source_path("world")
    if world_src.exists():
        found_asp = False
        with world_src.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    a = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if a.get("id") == target_asp:
                    found_asp = True
                    break
        if not found_asp:
            target_asp = top[0]["asp_id"]
            target_source = top[0]["source"]

    if args.dry_run:
        print("[cargo-cult audit-all] DRY-RUN — would file:")
        print(f"  target: {target_asp} ({target_source})")
        print(f"  title:  {dedup_title}")
        print(f"  candidates ranked: {len(top)}")
        for c in top[:5]:
            print(f"    - {c['goal_id']}: {c['interval_hours']:g}h → "
                  f"{c['proposed_interval_hours']:g}h (score {c['score']})")
        _gate_log(
            "cargo-cult-detector",
            "noop",
            extra={
                "would_block": False,
                "decision_path": "audit-all-dry-run",
                "candidates_count": len(top),
                "dry_run": True,
            },
        )
        return 0

    new_id = file_idea(target_asp, target_source, idea)
    if not new_id:
        _gate_log(
            "cargo-cult-detector",
            "noop",
            extra={
                "would_block": False,
                "decision_path": "audit-all-file-failed",
                "candidates_count": len(top),
            },
        )
        return 1

    print(f"[cargo-cult audit-all] filed {new_id} on {target_asp} "
          f"({target_source}) with {len(top)} candidates")
    _gate_log(
        "cargo-cult-detector",
        "pass",
        extra={
            "would_block": False,
            "decision_path": "audit-all-batch-filed",
            "filed_idea_id": new_id,
            "asp_id": target_asp,
            "candidates_count": len(top),
        },
    )
    return 0


def cmd_contract_per_goal(args, cfg: dict, contract_cfg: dict) -> int:
    """Auto-contract a recurring goal whose consecutive_deep ≥ threshold.

    Mirror of the auto-extend per-goal path:
      1. Read consecutive_deep on the goal.
      2. If interval_hours / divisor ≥ floor_ratio × original: contract +
         reset counter.
      3. Else: file an Idea proposing a deeper config-level rebase
         (parallel to the auto-extend cap-hit branch).

    Origin: LifingPolls plan item 4 (2026-05-08).
    """
    try:
        src = source_path(args.source)
    except SourceUnavailable as e:
        print(str(e), file=sys.stderr)
        return 1
    if not src.exists():
        sys.stderr.write(f"cargo-cult-detector: source file not found: {src}\n")
        return 1

    found = find_goal(src, args.goal_id)
    if found is None:
        sys.stderr.write(f"cargo-cult-detector: goal {args.goal_id} not found in {src}\n")
        return 1
    asp, goal = found
    if not goal.get("recurring"):
        sys.stderr.write(f"cargo-cult-detector: {args.goal_id} is not recurring\n")
        return 1

    interval_hours = goal.get("interval_hours")
    consecutive_deep = int(goal.get("consecutive_deep", 0))
    asp_id = asp.get("id")
    title = goal.get("title", args.goal_id)

    if interval_hours is None or interval_hours <= 0:
        sys.stderr.write(f"cargo-cult-detector: {args.goal_id} has no valid "
                         f"interval_hours; cannot contract\n")
        return 1

    divisor = float(contract_cfg["deep_streak_contract_divisor"])
    floor_ratio = float(contract_cfg["contract_floor_ratio"])
    orig_stored = goal.get("original_interval_hours")
    original = (float(orig_stored) if orig_stored is not None
                else float(interval_hours))
    proposed = round(float(interval_hours) / divisor, 2)
    floor = original * floor_ratio
    above_floor = proposed >= floor

    # Cadence-aware contract suppression (, regime-c detector fix).
    # For selection-gated goals on a busy loop, the ACTUAL firing cadence is
    # set by selector competition, not the timer — contracting interval_hours
    # cannot raise the firing rate (inert) and only manufactures streak-break
    # noise + floor-hit "rebase original" treadmills (: 25/25 fires
    # "broke"; 15+ rebase Ideas fleet-wide). Before contracting, compare the
    # recent actual cadence against streak_mult x interval; when actual far
    # exceeds interval, SKIP contraction and file/refresh ONE deduped
    # rebase-UP Idea instead. Runs BEFORE the floor branch on purpose: a
    # floor-pinned selection-gated goal must get the rebase-UP framing, not
    # the floor-hit "rebase original DOWN / retire" framing.
    streak_mult = _load_streak_mult()
    actual_median, cadence_status, cadence_samples = _recent_actual_cadence(
        args.goal_id,
        window=int(contract_cfg["contract_suppress_window"]),
        min_samples=int(contract_cfg["contract_suppress_min_samples"]),
    )
    if cadence_status == "unreadable":
        # guard-487: suppression-gate input unparseable -> fail CLOSED
        # (treat as suppressed). Do NOT reset consecutive_deep — a transient
        # IO error retries on the next close via the persistent counter.
        print(
            f"[cargo-cult-contract] SUPPRESSED (input unreadable): "
            f"streak-breaks.jsonl could not be read — treating as suppressed "
            f"per guard-487; will retry next close"
        )
        return 0
    if actual_median is not None and actual_median > streak_mult * float(interval_hours):
        print(
            f"[cargo-cult-contract] SUPPRESSED: actual cadence "
            f"{actual_median:.2f}h >> interval {interval_hours}h "
            f"(> {streak_mult:g}x streak_mult, {cadence_samples} recent "
            f"breaks) — contraction inert, regime-c selection-gated"
        )
        if args.dry_run:
            print(
                f"[cargo-cult-contract] DRY-RUN — would file rebase-UP Idea "
                f"for {args.goal_id} and reset consecutive_deep"
            )
            return 0
        rc = _file_rebase_up_idea(
            asp, asp_id, args.source, args.goal_id, title,
            float(interval_hours), actual_median, cadence_samples,
            consecutive_deep, streak_mult, args.dry_run,
        )
        # Reset regardless of filing outcome mirrors the floor-hit dedup
        # path: leaving the counter >= threshold would re-trigger this
        # (deduped) path on every subsequent deep close — the exact
        # treadmill this predicate removes.
        if not reset_consecutive_deep(args.goal_id, args.source):
            sys.stderr.write(
                f"cargo-cult-detector: suppression fired but "
                f"consecutive_deep reset failed for {args.goal_id}\n"
            )
        return rc

    if above_floor:
        if args.dry_run:
            print(
                f"[cargo-cult-contract] DRY-RUN — auto-contract "
                f"{args.goal_id}: {interval_hours}h -> {proposed}h "
                f"(original={original}h, floor={floor:.2f}h "
                f"= {floor_ratio:g}x)"
            )
            return 0
        if update_interval_hours(
            args.goal_id, args.source, proposed, original,
            had_original=(orig_stored is not None),
        ):
            reset_consecutive_deep(args.goal_id, args.source)
            print(
                f"[cargo-cult-contract] auto-contracted {args.goal_id}: "
                f"interval_hours {interval_hours}h -> {proposed}h "
                f"(original={original}h, floor={floor:.2f}h, "
                f"consecutive_deep was {consecutive_deep})"
            )
            return 0
        sys.stderr.write(
            "cargo-cult-detector: auto-contract failed; falling back to Idea path\n"
        )
    else:
        print(
            f"[cargo-cult-contract] floor HIT for {args.goal_id}: "
            f"proposed {proposed}h < {floor:.2f}h "
            f"({floor_ratio:g}x original {original}h) — escalating "
            f"to Idea path for human review"
        )

    # Floor hit (or contract write failed) — file an Idea proposing rebase.
    dedup_title = f"Idea: Rebase original interval for {args.goal_id}"
    existing = already_filed(asp, dedup_title)
    if existing:
        print(
            f"[cargo-cult-contract] dedup hit — Idea {existing} already "
            f"pending for {args.goal_id} on {asp_id}; skipping"
        )
        if not args.dry_run:
            reset_consecutive_deep(args.goal_id, args.source)
        return 0

    # The goal's OWN substantive rate, when the sensor tracks it. consecutive_deep
    # is a STREAK, not a rate — reporting only the streak invited readers to infer
    # "issues found on every fire", which is false whenever the streak is shorter
    # than the run history (: streak 3 against 22/32 = 68.8% substantive).
    # guard-3060 / guard-2406 both turn on this distinction, so the Idea has to
    # carry the rate or its reader cannot apply them.
    _hits = goal.get("substantive_hits")
    _runs = goal.get("substantive_runs")
    if isinstance(_hits, int) and isinstance(_runs, int) and _runs > 0:
        rate_line = (
            f"Its own substantive rate across tracked runs is {_hits}/{_runs} "
            f"({100.0 * _hits / _runs:.1f}%) — NOT every fire. Judge the deep "
            f"label on this goal's own reading, never on the iteration's total "
            f"learning (guard-3060, guard-2406).\n\n"
        )
    else:
        rate_line = (
            "This goal does not track substantive_hits/substantive_runs, so its "
            "own hit RATE is unknown here — a streak is not a rate. Establish the "
            "rate before treating the deep signal as continuous (guard-3060).\n\n"
        )

    idea = {
        "title": dedup_title,
        "description": (
            f"Recurring goal {args.goal_id} ({title}) closed deep "
            f"{consecutive_deep} times consecutively, which reached the "
            f"contract threshold. Auto-contract then DECLINED to act: its "
            f"interval is {interval_hours}h and the next contraction would be "
            f"{proposed}h, which is below the floor of {floor:.2f}h "
            f"({floor_ratio:g}x original {original}h). The interval has NOT "
            f"reached the floor — the floor is what stopped it, and it is "
            f"doing its job unless the evidence below says otherwise.\n\n"
            f"{rate_line}"
            f"Either:\n"
            f"  1. Rebase original_interval_hours to a smaller value if the "
            f"natural cadence is genuinely tighter than the original\n"
            f"  2. Investigate WHY the deep streak keeps rebuilding "
            f"(real signal vs. false positive in the routine/deep classifier)\n"
            f"  3. Retire this goal if the persistent deep signal indicates "
            f"the work it represents is no longer useful"
        ),
        "priority": "MEDIUM",
        "category": "framework-maintenance",
        "participants": ["agent"],
        "verification": {
            "outcomes": [
                "Decision recorded: rebase / investigate / retire"
            ],
            "checks": [], "preconditions": [],
        },
        "origin_signal": f"investigate:contract-floor:{args.goal_id}",
    }

    if args.dry_run:
        print(f"[cargo-cult-contract] DRY-RUN — would file on {asp_id}:")
        print(json.dumps(idea, indent=2))
        return 0

    new_id = file_idea(asp_id, args.source, idea)
    if not new_id:
        return 1
    if not reset_consecutive_deep(args.goal_id, args.source):
        sys.stderr.write(
            f"cargo-cult-detector: filed {new_id} but consecutive_deep "
            f"reset failed\n"
        )
    print(
        f"[cargo-cult-contract] filed {new_id} on {asp_id}: '{dedup_title}' "
        f"(consecutive_deep was {consecutive_deep})"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("goal_id", nargs="?", default=None,
                    help="Recurring goal-id to file an extend-interval Idea for "
                         "(omit when using --audit-all)")
    # WORLD_AGENT_ONLY: cross-agent routes via MIND_AGENT env override ()
    ap.add_argument("--source", choices=["world", "agent"], default="world")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be filed; do not write")
    ap.add_argument("--audit-all", action="store_true",
                    help="Batch mode: sweep every recurring goal across world "
                         "+ agent queues and emit ONE ranked Idea instead of "
                         "N per-goal Ideas. Triggered from aspirations-precheck "
                         "Phase 0.5 when any single goal hits threshold.")
    ap.add_argument("--contract-mode", action="store_true",
                    help="Inverse of auto-extend: contract interval_hours when "
                         "consecutive_deep >= threshold. Mirrors the per-goal "
                         "extend path with a floor at contract_floor_ratio × "
                         "original. LifingPolls plan item 4.")
    args = ap.parse_args()

    cfg = _load_detector_config()
    contract_cfg = _load_contract_config()

    # Contract mode short-circuits BOTH auto-extend and audit-all paths —
    # different counter, different math, separate code path.
    if args.contract_mode:
        if not args.goal_id:
            ap.error("goal_id is required for --contract-mode")
        return cmd_contract_per_goal(args, cfg, contract_cfg)

    # Batch mode short-circuits the per-goal path. --source and goal_id are
    # ignored (the audit reads both sources directly and doesn't need an ID).
    if args.audit_all:
        return cmd_audit_all(args, cfg)

    if not args.goal_id:
        ap.error("goal_id is required (or use --audit-all for batch mode)")

    try:
        src = source_path(args.source)
    except SourceUnavailable as e:
        # Preserves pre-rb-442 behavior (exit 1 with the same message) while
        # keeping the exception type narrow for cmd_audit_all's silent-skip path.
        print(str(e), file=sys.stderr)
        return 1
    if not src.exists():
        sys.stderr.write(f"cargo-cult-detector: source file not found: {src}\n")
        return 1

    found = find_goal(src, args.goal_id)
    if found is None:
        sys.stderr.write(f"cargo-cult-detector: goal {args.goal_id} not found in {src}\n")
        return 1
    asp, goal = found
    if not goal.get("recurring"):
        sys.stderr.write(f"cargo-cult-detector: {args.goal_id} is not recurring\n")
        return 1

    title = goal.get("title", args.goal_id)
    interval_hours = goal.get("interval_hours")
    consecutive = int(goal.get("consecutive_routine", 0))
    asp_id = asp.get("id")
    if not asp_id:
        sys.stderr.write(f"cargo-cult-detector: parent aspiration missing id\n")
        return 1

    # : artifact-producing goals produce durable output every fire; a
    # "routine" outcome on them is normal (report generated), not a cargo-cult
    # signal. Skip filing; still reset counter so detector doesn't re-fire.
    is_artifact, kw = is_artifact_producing(goal)
    if is_artifact:
        print(
            f"[cargo-cult] SKIP — {args.goal_id} is artifact-producing "
            f"(matched '{kw}'); routine outcome is expected, not cargo-cult signal"
        )
        if not args.dry_run:
            reset_consecutive_routine(args.goal_id, args.source)
        # gate_id MUST match core/config/gates.yaml id.
        _gate_log(
            "cargo-cult-detector",
            "block",
            trigger_matched=kw,
            payload=f"{args.goal_id}::{title}",
            extra={
                "would_block": True,
                "goal_id": args.goal_id,
                "consecutive_routine": consecutive,
                "matched_keyword": kw,
                "decision_path": "artifact-producing-skip",
                "dry_run": args.dry_run,
            },
        )
        return 0

    # Auto-extend branch: when the detector fires on a non-artifact-producing
    # recurring goal with a valid interval, multiply by cargo_cult.multiplier
    # and write the new value. Cap at cap_ratio × original_interval_hours;
    # past the cap, fall through to the Idea path so a human decides whether
    # the goal itself should be retired.
    if interval_hours is not None:
        multiplier = float(cfg.get("multiplier", 1.5))
        cap_ratio = float(cfg.get("cap_ratio", 3.0))
        orig_stored = goal.get("original_interval_hours")
        original = float(orig_stored) if orig_stored is not None else float(interval_hours)
        proposed = round(float(interval_hours) * multiplier, 2)
        within_cap = proposed <= original * cap_ratio
        if within_cap:
            if args.dry_run:
                print(
                    f"[cargo-cult] DRY-RUN — auto-extend {args.goal_id}: "
                    f"{interval_hours}h -> {proposed}h "
                    f"(original={original}h, cap={cap_ratio}x)"
                )
                # gate_id MUST match core/config/gates.yaml id.
                _gate_log(
                    "cargo-cult-detector",
                    "noop",
                    payload=f"{args.goal_id}::{title}",
                    extra={
                        "would_block": False,
                        "goal_id": args.goal_id,
                        "consecutive_routine": consecutive,
                        "decision_path": "auto-extend-dry-run",
                        "interval_hours": interval_hours,
                        "proposed": proposed,
                        "original": original,
                        "cap_ratio": cap_ratio,
                        "dry_run": True,
                    },
                )
                return 0
            if update_interval_hours(
                args.goal_id, args.source, proposed, original,
                had_original=(orig_stored is not None),
            ):
                reset_consecutive_routine(args.goal_id, args.source)
                print(
                    f"[cargo-cult] auto-extended {args.goal_id}: "
                    f"interval_hours {interval_hours}h -> {proposed}h "
                    f"(original={original}h, cap={cap_ratio}x, "
                    f"consecutive_routine was {consecutive})"
                )
                # gate_id MUST match core/config/gates.yaml id.
                _gate_log(
                    "cargo-cult-detector",
                    "pass",
                    payload=f"{args.goal_id}::{title}",
                    extra={
                        "would_block": False,
                        "goal_id": args.goal_id,
                        "consecutive_routine": consecutive,
                        "decision_path": "auto-extend-success",
                        "interval_hours_old": interval_hours,
                        "interval_hours_new": proposed,
                        "original": original,
                        "cap_ratio": cap_ratio,
                        "dry_run": False,
                    },
                )
                return 0
            # update_interval_hours failed — fall through to Idea path so the
            # signal still reaches the agent.
            sys.stderr.write(
                "cargo-cult-detector: auto-extend failed; falling back to Idea path\n"
            )
        else:
            print(
                f"[cargo-cult] auto-extend CAP HIT for {args.goal_id}: "
                f"proposed {proposed}h > {cap_ratio}x original {original}h — "
                f"escalating to Idea path for human review"
            )
            # fall through to Idea path

    dedup_title = f"Idea: Extend interval for {args.goal_id}"
    existing = already_filed(asp, dedup_title)
    if existing:
        print(
            f"[cargo-cult] dedup hit — Idea {existing} already pending for {args.goal_id} "
            f"on {asp_id}; skipping"
        )
        # Still reset the counter — the user has been notified, no need to re-fire.
        if not args.dry_run:
            reset_consecutive_routine(args.goal_id, args.source)
        # gate_id MUST match core/config/gates.yaml id.
        _gate_log(
            "cargo-cult-detector",
            "noop",
            payload=f"{args.goal_id}::{title}",
            extra={
                "would_block": False,
                "goal_id": args.goal_id,
                "consecutive_routine": consecutive,
                "decision_path": "dedup-hit",
                "existing_idea_id": existing,
                "asp_id": asp_id,
                "dry_run": args.dry_run,
            },
        )
        return 0

    idea = build_idea(args.goal_id, title, interval_hours, consecutive,
                      datetime.now(), source=args.source)

    if args.dry_run:
        print(f"[cargo-cult] DRY-RUN — would file on {asp_id}:")
        print(json.dumps(idea, indent=2))
        return 0

    new_id = file_idea(asp_id, args.source, idea)
    if not new_id:
        return 1

    if not reset_consecutive_routine(args.goal_id, args.source):
        # Idea was filed but counter reset failed — surface, but exit 0 since
        # the primary action succeeded. Counter will tick to threshold+1 next
        # cycle and dedup will catch it.
        sys.stderr.write(
            f"cargo-cult-detector: filed {new_id} but consecutive_routine reset failed\n"
        )

    print(
        f"[cargo-cult] filed {new_id} on {asp_id}: "
        f"'{dedup_title}' (consecutive_routine was {consecutive})"
    )
    # gate_id MUST match core/config/gates.yaml id.
    _gate_log(
        "cargo-cult-detector",
        "pass",
        payload=f"{args.goal_id}::{title}",
        extra={
            "would_block": False,
            "goal_id": args.goal_id,
            "consecutive_routine": consecutive,
            "decision_path": "idea-filed",
            "filed_idea_id": new_id,
            "asp_id": asp_id,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
