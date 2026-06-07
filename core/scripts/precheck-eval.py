#!/usr/bin/env python3
"""Precheck evaluator — Tier 1a hot-path extraction.

Replaces aspirations-precheck/SKILL.md Phases 0.5.0a → 0.5d with deterministic
Python. The LLM orchestrator calls `precheck-eval.sh run-all`, reads the JSON
summary line, and acts only on the `flags[]` (each flag names a SKILL.md step
that still needs LLM judgment — e.g. routing a zombie to complete-review).

Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 1a #1).

Subcommands (individually testable):
  run-all           — every subcommand below, merged into one report
  zombies           — Phase 0.5.0a zombie-aspiration scan
  pipeline-depth    — Phase 0.5.1  executable-goal low-water-mark
  hypothesis-health — Phase 0.5.2  hypothesis pipeline flow
  accuracy          — Phase 0.5.3  accuracy critical-threshold gate
  consolidation     — Phase 0.5.4  portfolio health + wm-set
  cycles            — Phase 0.5c   unproductive-cycle detection
  user-goals        — Phase 0.5d   user-goal reclassification sweep

Output contract (all subcommands):
  JSON to stdout with at least `{"subcommand","summary","flags":[],...}`.
  Exit 0 = clean. Exit 1 = flags raised (LLM should act). Exit 2 = input error.
  Side effects (aspirations-add-goal.sh, wm-set.sh) only when --apply is
  passed. Without --apply the script reports what it WOULD do — safe to call
  from tests or dry-run probes.

Config:
  Thresholds live in core/config/aspirations.yaml. Fail LOUD on a missing
  key — never hardcode a fallback (plan constraint #4). `run-all` loads the
  YAML once and passes the relevant sub-dict to each subcommand.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta  # noqa: F401 — timedelta used by hypothesis-health
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import AGENT_DIR, PROJECT_ROOT, CORE_ROOT  # type: ignore
from _fileops import log_script_decision  # type: ignore
from _gate_log import log as _gate_log  # type: ignore
from _prefix_registry import PRIMITIVE_PREFIXES  # type: ignore
from _goal_census import effective_counts  # type: ignore  (B9-deep census-augmented counts)

try:
    import yaml  # type: ignore
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr)
    sys.exit(2)


TERMINAL_STATUSES = {"completed", "skipped", "expired", "decomposed", "superseded"}


def _now():
    return datetime.now()


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00").replace("+00:00", ""))
    except (ValueError, TypeError):
        return None


def _load_config():
    """Load core/config/aspirations.yaml. Fail loud on missing file/keys."""
    path = Path(PROJECT_ROOT) / "core" / "config" / "aspirations.yaml"
    if not path.exists():
        raise FileNotFoundError(f"aspirations.yaml not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_compact():
    """Load <agent>/session/aspirations-compact.json. Returns None if missing."""
    if AGENT_DIR is None:
        return None
    path = AGENT_DIR / "session" / "aspirations-compact.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _active_aspirations(compact):
    """Filter compact data to status==active aspirations."""
    if not compact:
        return []
    # compact can be a list of aspirations directly or {"aspirations": [...]}
    items = compact if isinstance(compact, list) else compact.get("aspirations", [])
    return [a for a in items if a.get("status") == "active"]


def _run_script(args, input_text=None, timeout=30):
    """Run a core/scripts/*.sh and return (stdout, stderr, returncode).

    Used for side-effecting scripts (aspirations-add-goal.sh, wm-set.sh,
    pipeline-read.sh). Never shells out a pipe — stdin is passed explicitly
    so values containing quotes / backslashes stay intact on Windows bash.

    timeout default 30s is fine for fast helpers; pass higher for scripts
    that must scan the full knowledge tree (aspiration-trajectory.sh
    rglobs ~900+ .md files for goal-attribution; cold OneDrive cache can
    exceed 30s).
    """
    # POSIX-slash the script path. Git Bash / MSYS on Windows mangles
    # backslashes in argv (e.g. `C:\Zak\...` → `C:Zak...`), resulting in
    # "No such file or directory" for every helper .sh call. as_posix()
    # is cross-platform — native bash also accepts forward slashes.
    cmd = [(Path(CORE_ROOT) / "scripts" / args[0]).as_posix()] + list(args[1:])
    # On Windows we need bash to execute the .sh wrapper.
    from _runtime_bash import BASH as bash  # rb-1472: bin-first, honors MIND_SHELL, clean-PATH-safe
    full = [bash, *cmd]
    proc = subprocess.run(
        full,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.stdout, proc.stderr, proc.returncode


# ─────────────────────────────────────────────────────────────────────────
# Phase 0.5.0a — Zombie aspiration scan
# ─────────────────────────────────────────────────────────────────────────

def cmd_zombies(args, config, compact):
    """Detect aspirations with high completion but blocked-stale trailing goals.

    Mirrors aspirations-precheck/SKILL.md Phase 0.5.0a. Does NOT route to
    complete-review — emits a `zombies[]` list and a `needs_complete_review`
    flag. The orchestrator invokes /aspirations-complete-review per entry.
    """
    intent = config.get("intent_satisfaction") or {}
    zombie_ratio = intent.get("zombie_completion_ratio")
    min_blocked_hours = intent.get("phase_7_4_min_blocked_hours")
    if zombie_ratio is None or min_blocked_hours is None:
        raise KeyError(
            "aspirations.yaml missing intent_satisfaction.{zombie_completion_ratio,"
            "phase_7_4_min_blocked_hours}"
        )

    active = _active_aspirations(compact)
    now = _now()
    zombies = []

    for asp in active:
        goals = asp.get("goals", [])
        if any(g.get("recurring") for g in goals):
            continue
        non_recurring = [g for g in goals if not g.get("recurring")]
        if not non_recurring:
            continue
        unfinished = [g for g in non_recurring if g.get("status") not in TERMINAL_STATUSES]
        if not non_recurring or not unfinished:
            continue
        # Census-augmented (B9-deep): archived completed/abandoned goals still
        # count toward the zombie completion ratio, so eviction can't make a
        # near-done aspiration drop below the zombie threshold and escape review.
        nonrec_total, nonrec_completed = effective_counts(asp, include_recurring=False)
        completion_ratio = nonrec_completed / nonrec_total if nonrec_total else 0.0
        if completion_ratio < zombie_ratio:
            continue
        # All unfinished must be blocked AND past the stale threshold
        if any(g.get("status") != "blocked" for g in unfinished):
            continue
        all_stale = True
        for g in unfinished:
            bs = _parse_iso(g.get("blocked_since"))
            if bs is None:
                all_stale = False
                break
            age_hours = (now - bs).total_seconds() / 3600
            if age_hours < min_blocked_hours:
                all_stale = False
                break
        if not all_stale:
            continue
        zombies.append({
            "aspiration_id": asp.get("id"),
            "title": asp.get("title"),
            "source": asp.get("source", "agent"),
            "completion_ratio": round(completion_ratio, 3),
            "blocked_goal_ids": [g.get("id") for g in unfinished],
        })

    flags = ["needs_complete_review"] if zombies else []
    summary = (
        f"zombies: {len(zombies)} aspiration(s) matching intent-satisfaction profile"
        if zombies else "zombies: clean"
    )
    return {
        "subcommand": "zombies",
        "summary": summary,
        "flags": flags,
        "zombies": zombies,
        "thresholds": {
            "zombie_completion_ratio": zombie_ratio,
            "phase_7_4_min_blocked_hours": min_blocked_hours,
        },
    }


# ─────────────────────────────────────────────────────────────────────────
# Phase 0.5.1 — Pipeline depth (executable goal count)
# ─────────────────────────────────────────────────────────────────────────

def cmd_pipeline_depth(args, config, compact):
    """Count executable goals; flag if below low-water-mark.

    Executable = pending AND (deferred_until null or past) AND all blocked_by
    IDs are in the completed set. The orchestrator acts on `thin_pipeline`
    by invoking /create-aspiration from-self.
    """
    lwm = config.get("pipeline_low_water_mark")
    if lwm is None:
        raise KeyError("aspirations.yaml missing pipeline_low_water_mark")

    active = _active_aspirations(compact)
    now = _now()

    completed_ids = set()
    for asp in active:
        for g in asp.get("goals", []):
            if g.get("status") == "completed":
                completed_ids.add(g.get("id"))

    executable = 0
    for asp in active:
        for g in asp.get("goals", []):
            if g.get("status") != "pending":
                continue
            du = _parse_iso(g.get("deferred_until"))
            if du is not None and du > now:
                continue
            blocked_by = g.get("blocked_by") or []
            if blocked_by and not all(b in completed_ids for b in blocked_by):
                continue
            executable += 1

    flags = ["thin_pipeline"] if executable < lwm else []
    summary = (
        f"pipeline-depth: thin ({executable} executable < {lwm})"
        if flags else f"pipeline-depth: healthy ({executable} executable)"
    )
    return {
        "subcommand": "pipeline-depth",
        "summary": summary,
        "flags": flags,
        "executable_count": executable,
        "threshold": lwm,
    }


# ─────────────────────────────────────────────────────────────────────────
# Phase 0.5.2 — Hypothesis pipeline health
# ─────────────────────────────────────────────────────────────────────────

def _pipeline_query(arg):
    """Call pipeline-read.sh and parse JSON output. Returns None on failure."""
    stdout, _stderr, rc = _run_script(["pipeline-read.sh"] + arg.split())
    if rc != 0 or not stdout.strip():
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


def _days_since(ts_str):
    ts = _parse_iso(ts_str)
    if ts is None:
        return 99999
    return (_now() - ts).total_seconds() / 86400.0


def cmd_hypothesis_health(args, config, compact):
    """Flow check: fresh_discovered (<=7d) + resolvable_active > low-water-mark."""
    lwm = config.get("hypothesis_pipeline_low_water_mark")
    if lwm is None:
        raise KeyError("aspirations.yaml missing hypothesis_pipeline_low_water_mark")

    counts = _pipeline_query("--counts") or {}
    discovered = _pipeline_query("--stage discovered") or []
    active = _pipeline_query("--stage active") or []

    fresh_discovered = [h for h in discovered if _days_since(h.get("formed_date")) <= 7]

    now = _now()
    resolvable_active = []
    time_gated_active = []
    for h in active:
        horizon = h.get("horizon", "")
        formed = _parse_iso(h.get("formed_date"))
        if horizon in ("session", "micro"):
            resolvable_active.append(h)
        elif horizon == "short" and formed and formed + timedelta(hours=12) <= now:
            resolvable_active.append(h)
        elif horizon == "long" and formed and formed + timedelta(hours=24) <= now:
            resolvable_active.append(h)
        else:
            time_gated_active.append(h)

    flowing = len(fresh_discovered) + len(resolvable_active)
    flags = ["stalled_pipeline"] if flowing < lwm else []
    summary = (
        f"hypothesis-health: stalled ({flowing} flowing < {lwm})"
        if flags else f"hypothesis-health: healthy ({flowing} flowing; "
                     f"{len(fresh_discovered)} fresh, {len(resolvable_active)} resolvable)"
    )
    return {
        "subcommand": "hypothesis-health",
        "summary": summary,
        "flags": flags,
        "flowing_count": flowing,
        "fresh_discovered": len(fresh_discovered),
        "resolvable_active": len(resolvable_active),
        "time_gated_active": len(time_gated_active),
        "threshold": lwm,
        "counts": counts,
    }


# ─────────────────────────────────────────────────────────────────────────
# Phase 0.5.3 — Accuracy health gate
# ─────────────────────────────────────────────────────────────────────────

def cmd_accuracy(args, config, compact):
    """Flag when accuracy < critical_threshold with adequate sample."""
    crit = config.get("accuracy_critical_threshold")
    min_sample = config.get("accuracy_min_sample")
    if crit is None or min_sample is None:
        raise KeyError("aspirations.yaml missing accuracy_critical_threshold / accuracy_min_sample")

    data = _pipeline_query("--accuracy") or {}
    total = data.get("total_resolved", 0)
    pct = data.get("accuracy_pct", 0)
    by_strategy = data.get("by_strategy") or {}

    flags = []
    worst = []
    if total >= min_sample and pct < (crit * 100):
        flags.append("accuracy_low")
        worst = [name for name, stats in by_strategy.items()
                 if (stats or {}).get("pct", 100) < 40 and (stats or {}).get("total", 0) >= 3]

    summary = (
        f"accuracy: critical ({pct}% < {crit*100}%, n={total})"
        if flags else f"accuracy: healthy ({pct}% over {total} resolved)"
    )
    return {
        "subcommand": "accuracy",
        "summary": summary,
        "flags": flags,
        "accuracy_pct": pct,
        "total_resolved": total,
        "confirmed": data.get("confirmed", 0),
        "worst_strategies": worst[:3],
        "threshold_pct": round(crit * 100, 1),
        "min_sample": min_sample,
    }


# ─────────────────────────────────────────────────────────────────────────
# Phase 0.5.4 — Consolidation health gate
# ─────────────────────────────────────────────────────────────────────────

def cmd_consolidation(args, config, compact):
    """Portfolio-level completion-ratio rollup. Flags low avg or stalled asps."""
    active = _active_aspirations(compact)
    if not active:
        return {
            "subcommand": "consolidation",
            "summary": "consolidation: no active aspirations",
            "flags": [],
            "active_count": 0,
        }

    ratios = []
    stalled_detail = []
    near_complete = 0
    # "tracked" here EXCLUDES skipped/expired/decomposed but counts superseded —
    # a denominator distinct from the scorer's ABANDONED set, so it needs the
    # per-status census to fold evicted goals back in (B9-deep).
    _TRACKED_EXCL = frozenset({"skipped", "expired", "decomposed"})
    for asp in active:
        tracked_n, completed_n = effective_counts(
            asp, exclude_statuses=_TRACKED_EXCL, include_recurring=True)
        if not tracked_n:
            continue
        ratio = completed_n / tracked_n
        ratios.append(ratio)
        sessions_active = asp.get("sessions_active", 0)
        if ratio < 0.15 and sessions_active > 2:
            stalled_detail.append({
                "asp_id": asp.get("id"),
                "title": asp.get("title"),
                "completion": f"{completed_n}/{tracked_n}",
                "sessions_active": sessions_active,
            })
        if ratio > 0.75:
            near_complete += 1

    avg = sum(ratios) / len(ratios) if ratios else 0.0

    flags = []
    if avg < 0.25 and len(active) >= 3:
        flags.append("shallow_portfolio")
    if stalled_detail:
        flags.append("stalled_aspirations")

    summary_bits = [f"avg={round(avg, 2)}", f"near_complete={near_complete}", f"stalled={len(stalled_detail)}"]
    summary = (
        f"consolidation: warn ({', '.join(summary_bits)})"
        if flags else f"consolidation: healthy ({', '.join(summary_bits)})"
    )
    return {
        "subcommand": "consolidation",
        "summary": summary,
        "flags": flags,
        "avg_completion": round(avg, 3),
        "near_complete": near_complete,
        "stalled_count": len(stalled_detail),
        "stalled": stalled_detail,
        "active_count": len(active),
    }


# ─────────────────────────────────────────────────────────────────────────
# Phase 0.5c — Unproductive cycle detection
# ─────────────────────────────────────────────────────────────────────────

def _has_recent_reports(asp_id, recent_goals, age_days):
    """Whether any agents/<agent>/temp/*.md was filed within
    `age_days` for this aspiration or any of its `recent_goals`. Filename
    substring match against aspiration id OR goal id. Walks every agent's
    temp/ dir under PROJECT_ROOT/agents/ (N-agent safe — not
    hardcoded to alpha/bravo; Phase 2.5.D layout). Briefings moved reports/
    -> temp/ in the file-model normalization; reports/ was abolished 2026-06-02.

    Origin: g-115-188. The cycle detector's zero_learning_velocity branch
    flagged actively-shipping aspirations that produced reports/commits but
    sparse rb/guard/tree encoding (n=2 evidence: g-242-07 + g-248-51 closure
    reports). Reports are substantive learning artifacts; counting them
    suppresses the false positive.
    """
    if age_days <= 0:
        return False
    cutoff = _now() - timedelta(days=age_days)
    recent_ids = {g.get("id", "").lower() for g in recent_goals if g.get("id")}
    asp_lc = (asp_id or "").lower()
    if not asp_lc and not recent_ids:
        return False
    # Phase 2.5.D: agent dirs live under PROJECT_ROOT/agents/. Walk that
    # parent — falling back to a no-op iteration if the parent is missing
    # (fresh repo, test fixture without agents/ seeded).
    agents_parent = Path(PROJECT_ROOT) / "agents"
    if not agents_parent.is_dir():
        return False
    for entry in agents_parent.iterdir():
        if not entry.is_dir():
            continue
        # Briefings activity: scan temp/ (the briefing home — file-model
        # normalization moved briefings reports/ -> temp/; reports/ was
        # abolished 2026-06-02, git history is its archive).
        for sub in ("temp",):
            scan_dir = entry / sub
            if not scan_dir.is_dir():
                continue
            for rpt in scan_dir.glob("*.md"):
                try:
                    mtime = datetime.fromtimestamp(rpt.stat().st_mtime)
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                name_lc = rpt.name.lower()
                if asp_lc and asp_lc in name_lc:
                    return True
                if any(gid and gid in name_lc for gid in recent_ids):
                    return True
    return False


def cmd_cycles(args, config, compact):
    """Detect repeated_failure or zero_learning_velocity cycles per aspiration."""
    cycle_cfg = config.get("cycle_detection") or {}
    lookback = cycle_cfg.get("lookback_window", 3)
    report_age_days = cycle_cfg.get("report_signal_age_days", 7)

    active = _active_aspirations(compact)
    cycles = []

    for asp in active:
        goals = asp.get("goals", [])
        resolved = [g for g in goals if g.get("status") in ("completed", "skipped")]
        #  (session 61,  false-positive): skipped auto-Unblock
        # goals filed by defer-gate's capability-gate keyword false-positives
        # (e.g.,  "Unblock: run for " matched 'processor';
        #  "Unblock: prefix for " matched 'prefix' against an
        # unrelated config-tune row) should not pollute repeated_failure
        # detection. The agent's correct rejection of misrouted auto-Unblocks
        # is not a failure pattern — it's defensive routing working as
        # designed. Genuine repeated failures of non-primitive work still
        # surface (Apply/Maintain/Investigate skips fall through unchanged).
        #
        # 1 (2026-05-27) REMOVED the  synthetic-tag branch
        # (`or "synthetic" in g.get("tags")`). It was DEAD in production: the
        # live compact projection (_COMPACT_GOAL_KEEP in
        # mind_api/src/endpoints/aspirations.py — the daemon duplicate of the
        # now-orphaned CLI COMPACT_GOAL_KEEP) strips `tags`, so cmd_cycles
        # always saw g.get("tags") == None here and the branch never matched on
        # real compact data. The  "confirmed via simulation" used
        # hand-injected tags that bypassed the projection (false-confidence,
        # testSymmetry/ class). No production harm resulted from the
        # dead branch, because repeated_failure is ADVISORY and skip-by-design
        # FPs self-resolve via lookback-window churn (: "pushed migrated
        # trio out of lookback window; cycles clean"; rb-1320: "treat the cycle
        # flag as advisory noise"). Resurrecting the exclusion would require
        # carrying `tags` through the hot-path compact for every goal
        # (regenerated each iteration, consumed by 5+ systems) to protect a
        # near-extinct class (3 historical skipped wire-tests /600/602,
        # 0 active). Subtraction beats that permanent tax. The Unblock: branch
        # below STAYS — `title` survives the projection, so it is genuinely live.
        # Migration skip-by-design FPs () are a separate live concern;
        # if they recur, address with a dedicated mechanism, not tag-resurrection.
        resolved = [
            g for g in resolved
            if not (
                g.get("status") == "skipped"
                and (g.get("title") or "").strip().startswith("Unblock:")
            )
        ]
        if len(resolved) < lookback:
            continue
        recent = resolved[-lookback:]

        skipped = [g for g in recent if g.get("status") == "skipped"]
        cycle_reason = None
        if len(skipped) >= lookback - 1:
            cycle_reason = "repeated_failure"
        else:
            cats = {g.get("category") for g in recent}
            if len(cats) == 1:
                #  (session-58): suppress zero_learning_velocity when all
                # recent goals are Cognitive Primitives (Unblock/Maintain/Idea/
                # Investigate).  (session-59): added Apply: — same class
                # (planner→implementer handoff per aspirations-spark, or self-
                # applied tactical correction).  (session-60): added
                # Batch: — auto-filed by cargo-cult-detector.py for recurring-
                # interval recalibration; same tactical-correction shape as
                # Maintain. Primitives are tactical corrections and meta-
                # diagnostics — they produce tree_nodes/rb/guardrails artifacts
                # only sometimes (Investigate/Idea occasionally, Apply/Unblock/
                # Maintain/Batch rarely by design — they produce commits or
                # framework-state mutations). compute_learning_velocity() counts
                # only those 4 artifact categories, so primitive-heavy windows
                # systematically false-positive as zero-velocity. The signal
                # belongs on goals intended to produce learning, not on
                # corrective primitives.
                all_primitives = all(
                    (g.get("title") or "").strip().startswith(PRIMITIVE_PREFIXES)
                    for g in recent
                )
                if not all_primitives:
                    # Probe trajectory for velocity (fail-open: no trajectory → skip).
                    # 60s timeout — aspiration-trajectory.sh rglobs the full
                    # knowledge tree (~900+ .md files); 30s default tripped on
                    # cold OneDrive cache 2026-05-04 (precheck-eval cycles:error
                    # flag fired post-iter-17 compact resumption).
                    stdout, _e, rc = _run_script(
                        ["aspiration-trajectory.sh", asp.get("id", "")],
                        timeout=60,
                    )
                    if rc == 0:
                        try:
                            traj = json.loads(stdout)
                            if traj.get("current_velocity") == 0:
                                # : trajectory.sh counts only rb/guard/
                                # tree-node encodings, not analytical reports.
                                # Suppress when <agent>/temp/*.md exist for
                                # this aspiration or its recent goals — those
                                # ARE substantive learning artifacts.
                                if not _has_recent_reports(asp.get("id"), recent, report_age_days):
                                    cycle_reason = "zero_learning_velocity"
                        except json.JSONDecodeError:
                            pass

        if cycle_reason:
            cycles.append({
                "aspiration_id": asp.get("id"),
                "title": asp.get("title"),
                "source": asp.get("source", "agent"),
                "reason": cycle_reason,
                "recent_goal_titles": [g.get("title", "")[:60] for g in recent],
                "category": recent[0].get("category"),
            })

    flags = ["cycles_detected"] if cycles else []
    summary = (
        f"cycles: {len(cycles)} detected"
        if cycles else "cycles: clean"
    )
    return {
        "subcommand": "cycles",
        "summary": summary,
        "flags": flags,
        "cycles": cycles,
        "lookback_window": lookback,
    }


# ─────────────────────────────────────────────────────────────────────────
# Phase 0.5d — User-goal reclassification sweep
# ─────────────────────────────────────────────────────────────────────────

# Keyword match for Check 4 in SKILL.md:615. Stays in sync with
# .claude/rules/capability-before-user.md.
AGENT_ACTION_KEYWORDS = [
    "deploy", "commit", "script", "test", "analyze",
    "run", "check", "verify", "monitor",
]


def cmd_user_goals(args, config, compact):
    """Scan active aspirations for [user]-only goals that the agent can handle."""
    active = _active_aspirations(compact)
    candidates = []
    cap = 5

    for asp in active:
        if len(candidates) >= cap:
            break
        for g in asp.get("goals", []):
            if len(candidates) >= cap:
                break
            if g.get("status") != "pending":
                continue
            participants = g.get("participants") or []
            if participants != ["user"]:
                continue  # [agent,user] collaborative goals stay (eligibility filter — not a gate firing)
            title = (g.get("title") or "").lower()
            reason = None
            matched_kw = None
            if g.get("skill"):
                skill_path = Path(PROJECT_ROOT) / ".claude" / "skills" / g["skill"] / "SKILL.md"
                if skill_path.exists():
                    reason = f"skill '{g['skill']}' exists"
                    matched_kw = f"skill:{g['skill']}"
            if reason is None:
                for kw in AGENT_ACTION_KEYWORDS:
                    if kw in title:
                        reason = f"title contains agent-action keyword '{kw}'"
                        matched_kw = f"keyword:{kw}"
                        break
            # gate_id MUST match core/config/gates.yaml id.
            _gate_log(
                "agent-action-keywords",
                "block" if reason else "pass",
                caller=f"precheck-eval.py:cmd_user_goals goal={g.get('id')}",
                trigger_matched=matched_kw,
                payload=title[:200],
                extra={"asp_id": asp.get("id"), "would_block": bool(reason)},
            )
            if reason:
                candidates.append({
                    "goal_id": g.get("id"),
                    "title": g.get("title"),
                    "asp_id": asp.get("id"),
                    "source": asp.get("source", "agent"),
                    "reclassify_reason": reason,
                })

    flags = ["reclassifiable_user_goals"] if candidates else []
    summary = (
        f"user-goals: {len(candidates)} candidate(s) for reclassification"
        if candidates else "user-goals: clean"
    )
    return {
        "subcommand": "user-goals",
        "summary": summary,
        "flags": flags,
        "candidates": candidates,
        "cap": cap,
    }


# ─────────────────────────────────────────────────────────────────────────
# Phase 0.5.x — temp/ accumulation pressure (file-model normalization Phase 5)
# ─────────────────────────────────────────────────────────────────────────

def cmd_temp_pressure(args, config, compact):
    """Count undrained working docs in the bound agent's temp/ store and flag
    accumulation pressure, so temp/ never becomes the new slush directory.

    temp/ is the single staging SSOT for working docs that drain to the
    knowledge tree (core/config/conventions/temp-store.md). Files accumulate
    there until /drain-temp encodes each and moves it to temp/drained/. This
    check counts the UNDRAINED files — files directly under temp/ (NOT the
    drained/ subdir) — and emits:
      - temp_pressure_warn  at >= warn_threshold   (visible nudge, no goal)
      - temp_drain_needed   at >= drain_threshold  (orchestrator files the
                                                    HIGH /drain-temp goal in
                                                    `suggested_goal`)
      - temp_drain_pending  at >= drain_threshold when an open drain goal
                            already exists (deduped — no second goal filed)
    Advisory like the sibling checks: this function never files the goal; the
    aspirations-precheck SKILL acts on `temp_drain_needed` + `suggested_goal`.
    """
    tp = config.get("temp_pressure") or {}
    warn_threshold = tp.get("warn_threshold")
    drain_threshold = tp.get("drain_goal_threshold")
    if warn_threshold is None or drain_threshold is None:
        raise KeyError(
            "aspirations.yaml missing temp_pressure."
            "{warn_threshold,drain_goal_threshold}")

    # Undrained working docs = files directly under temp/, excluding drained/.
    count = 0
    temp_dir = (AGENT_DIR / "temp") if AGENT_DIR is not None else None
    if temp_dir is not None and temp_dir.is_dir():
        for f in temp_dir.iterdir():
            if f.is_file() and f.suffix in (".md", ".json"):
                count += 1

    # Dedup: if a drain-temp goal is already open, do NOT re-suggest filing —
    # else every iteration above threshold would spawn a duplicate HIGH goal.
    existing = None
    for asp in _active_aspirations(compact):
        for g in asp.get("goals", []):
            if g.get("status") in ("pending", "in-progress"):
                t = (g.get("title") or "").lower()
                if "drain" in t and "temp" in t:
                    existing = g.get("id")
                    break
        if existing:
            break

    flags = []
    suggested_goal = None
    if count >= drain_threshold and existing is None:
        flags.append("temp_drain_needed")
        suggested_goal = {
            "title": (f"Maintain: drain {count} accumulated temp/ working docs "
                      f"to the knowledge tree"),
            "priority": "HIGH",
            "participants": ["agent"],
            "description": (
                f"agents/<agent>/temp/ holds {count} undrained working docs "
                f"(>= drain threshold {drain_threshold}). Invoke /drain-temp to "
                f"encode each into the knowledge tree / reasoning bank / "
                f"experience and move it to temp/drained/. temp/ is a staging "
                f"SSOT, not an archive — undrained accumulation is the "
                f"slush-directory failure mode the file-model normalization "
                f"exists to prevent."
            ),
        }
    elif count >= drain_threshold and existing is not None:
        flags.append("temp_drain_pending")
    elif count >= warn_threshold:
        flags.append("temp_pressure_warn")

    summary = (
        f"temp-pressure: {count} undrained doc(s) "
        f"(warn>={warn_threshold}, drain>={drain_threshold}"
        + (f"; open drain goal {existing}" if existing else "") + ")"
        if count else "temp-pressure: clean"
    )
    return {
        "subcommand": "temp-pressure",
        "summary": summary,
        "flags": flags,
        "count": count,
        "existing_drain_goal": existing,
        "thresholds": {"warn_threshold": warn_threshold,
                       "drain_goal_threshold": drain_threshold},
        "suggested_goal": suggested_goal,
    }


# ─────────────────────────────────────────────────────────────────────────
# run-all — aggregate all subcommands into one report
# ─────────────────────────────────────────────────────────────────────────

SUBCMDS = [
    ("zombies", cmd_zombies),
    ("pipeline-depth", cmd_pipeline_depth),
    ("hypothesis-health", cmd_hypothesis_health),
    ("accuracy", cmd_accuracy),
    ("consolidation", cmd_consolidation),
    ("cycles", cmd_cycles),
    ("user-goals", cmd_user_goals),
    ("temp-pressure", cmd_temp_pressure),
]


def cmd_run_all(args, config, compact):
    results = {}
    all_flags = []
    for name, fn in SUBCMDS:
        try:
            r = fn(args, config, compact)
            results[name] = r
            all_flags.extend(f"{name}:{f}" for f in r.get("flags", []))
        except Exception as e:  # fail-open per plan constraint #3
            results[name] = {
                "subcommand": name,
                "summary": f"{name}: ERROR {type(e).__name__}: {e}",
                "flags": ["error"],
                "error": str(e),
            }
            all_flags.append(f"{name}:error")

    summary = (
        f"run-all: {len(all_flags)} flag(s): {', '.join(all_flags)}"
        if all_flags else "run-all: clean"
    )
    return {
        "subcommand": "run-all",
        "summary": summary,
        "flags": all_flags,
        "results": results,
    }


# ─────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────

DISPATCH = {
    "run-all": cmd_run_all,
    "zombies": cmd_zombies,
    "pipeline-depth": cmd_pipeline_depth,
    "hypothesis-health": cmd_hypothesis_health,
    "accuracy": cmd_accuracy,
    "consolidation": cmd_consolidation,
    "cycles": cmd_cycles,
    "user-goals": cmd_user_goals,
    "temp-pressure": cmd_temp_pressure,
}


def main():
    parser = argparse.ArgumentParser(description="Precheck evaluator (Tier 1a)")
    parser.add_argument("subcommand", choices=list(DISPATCH.keys()))
    parser.add_argument("--apply", action="store_true",
                        help="Enable side effects (create goals, wm writes). "
                             "Default is dry-run — emits flags the LLM should act on.")
    parser.add_argument("--compact-path", type=str, default=None,
                        help="Override path to aspirations-compact.json (for tests).")
    args = parser.parse_args()

    try:
        config = _load_config()
    except Exception as e:
        print(json.dumps({
            "subcommand": args.subcommand,
            "summary": f"ERROR loading config: {e}",
            "flags": ["error"],
        }))
        sys.exit(2)

    if args.compact_path:
        with open(args.compact_path, "r", encoding="utf-8") as f:
            compact = json.load(f)
    else:
        compact = _load_compact()
        if compact is None:
            print(json.dumps({
                "subcommand": args.subcommand,
                "summary": "no aspirations-compact.json (run aspirations-compact.sh first)",
                "flags": [],
            }))
            sys.exit(0)

    try:
        result = DISPATCH[args.subcommand](args, config, compact)
    except KeyError as e:
        print(json.dumps({
            "subcommand": args.subcommand,
            "summary": f"ERROR missing config key: {e}",
            "flags": ["error"],
        }))
        sys.exit(2)

    log_script_decision("precheck-eval", {
        "subcommand": args.subcommand,
        "flags": result.get("flags", []),
        "summary": str(result.get("summary", ""))[:200],
    })
    print(json.dumps(result, ensure_ascii=False, default=str))
    sys.exit(1 if result.get("flags") else 0)


if __name__ == "__main__":
    main()
