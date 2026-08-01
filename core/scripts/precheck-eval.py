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
import time
from datetime import datetime, timedelta  # noqa: F401 — timedelta used by hypothesis-health
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import AGENT_DIR, PROJECT_ROOT, CORE_ROOT, META_DIR, WORLD_DIR  # type: ignore
from _fileops import log_script_decision  # type: ignore
from _gate_log import log as _gate_log  # type: ignore
from _prefix_registry import PRIMITIVE_PREFIXES  # type: ignore
from _goal_census import effective_counts  # type: ignore  (B9-deep census-augmented counts)
from _drain_title import (  # type: ignore  ( owner-scope drain SSOT)
    _DRAIN_GOAL_TITLE_PREFIX, _DRAIN_GOAL_TITLE_INFIX, is_drain_action_title)

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


def _load_cognitive_horizons():
    """Load meta/cognitive-horizons.yaml (SSOT, BRD Gap 19 / ).

    Fail loud on missing file (guard-424 + precheck-eval constraint #4 — no
    hardcoded fallback; the yaml is the single source for horizon windows so
    callers consume it rather than re-hardcoding literals, rb-335).
    """
    if META_DIR is None:
        raise RuntimeError("META_DIR unresolved — cannot load cognitive-horizons.yaml")
    path = Path(META_DIR) / "cognitive-horizons.yaml"
    if not path.exists():
        raise FileNotFoundError(f"cognitive-horizons.yaml not found at {path}")
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


def _compact_staleness(compact_path=None):
    """ — is the compact OLDER than the stores it summarizes?

    Every one of the 8 detectors takes `compact` as a parameter and none
    re-reads live state, so a stale compact makes them return a WRONG ANSWER
    rather than an error. The refresh (`load-aspirations-compact.sh`) lives in
    a SEPARATE, LLM-invoked precheck step — so detector correctness rides on
    the LLM having remembered a different line first. LLM-gated steps drift;
    this makes the drift VISIBLE instead of silent.

    Paired with `_refresh_compact()`: main() refreshes when this reports stale,
    then re-runs this probe on the result. So this serves two roles — the
    TRIGGER for the auto-refresh, and the VERIFICATION that the refresh
    actually worked. A staleness flag surviving the refresh means the rebuild
    failed, which is a genuinely loud condition rather than routine drift.

    The staleness DEFINITION is not invented here — it is the same test
    load-aspirations-compact.sh already uses to decide whether to regenerate
    (`[ "$WORLD_JSONL" -nt "$COMPACT" ]` / `[ "$AGENT_JSONL" -nt "$COMPACT" ]`,
    i.e. newest-source-mtime > compact-mtime). Mirroring the refresher's own
    predicate keeps ONE definition of "stale" (communication-clarity rule 5):
    whenever this reports stale, the canonical refresher would regenerate, so
    the reported condition is always clearable by the documented action.

    Returns a dict always; never raises (a staleness probe must not be able to
    break the detectors it is reporting on).
    """
    info = {"checked": False, "stale": False}
    try:
        path = Path(compact_path) if compact_path else (
            (AGENT_DIR / "session" / "aspirations-compact.json")
            if AGENT_DIR is not None else None)
        if path is None or not path.exists():
            return info
        c_mtime = path.stat().st_mtime
        # Both queues are literally named `aspirations.jsonl`, so a basename
        # cannot tell them apart — carry an explicit kind so the report can say
        # WHICH queue moved without string-matching a path.
        sources = []
        if WORLD_DIR:
            sources.append(("world", Path(WORLD_DIR) / "aspirations.jsonl"))
        if AGENT_DIR is not None:
            sources.append(("agent", AGENT_DIR / "aspirations.jsonl"))
        newest, newest_src, newest_kind = 0.0, None, None
        for kind, s in sources:
            try:
                if s.exists() and s.stat().st_mtime > newest:
                    newest, newest_src, newest_kind = s.stat().st_mtime, str(s), kind
            except OSError:
                continue
        if newest_src is None:
            return info
        info["checked"] = True
        info["compact_mtime"] = time.strftime("%Y-%m-%dT%H:%M:%S",
                                              time.localtime(c_mtime))
        info["age_minutes"] = round((time.time() - c_mtime) / 60.0, 1)
        if newest > c_mtime:
            info["stale"] = True
            info["newer_source"] = newest_src
            info["newer_source_kind"] = newest_kind
            info["source_ahead_minutes"] = round((newest - c_mtime) / 60.0, 1)
    except Exception as e:  # noqa: BLE001 — never break the detectors
        # An errored probe does not know the answer. Say so rather than letting
        # the default `stale: False` read as a verified-fresh verdict — an
        # unverified negative is exactly what this whole fix exists to remove.
        info["checked"] = False
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def _refresh_compact(timeout=90):
    """ — rebuild the compact via its canonical builder. Fail-OPEN.

    Delegates to load-aspirations-compact.sh, the ONE thing that knows how to
    build a compact, rather than rebuilding inline — an inline rebuild would
    fork a second copy of the build logic that drifts from the real one
    (communication-clarity rule 5). The builder is itself a no-op when the
    compact is already fresh, but the caller still gates on staleness so the
    common fresh path costs zero subprocesses.

    Never raises. On timeout, spawn failure, or a non-zero builder exit, the
    caller proceeds with whatever compact is on disk AND the post-refresh
    staleness probe flags it loudly — so a failed refresh degrades to
    visible-stale, never to a stall or a silent wrong answer.
    """
    try:
        _out, err, rc = _run_script(["load-aspirations-compact.sh"], timeout=timeout)
        info = {"attempted": True, "rc": rc}
        if rc != 0:
            info["stderr"] = (err or "").strip()[-300:]
        return info
    except Exception as e:  # noqa: BLE001 — subprocess timeout / spawn failure
        return {"attempted": True, "rc": None, "error": f"{type(e).__name__}: {e}"}


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
    """Detect zombie aspirations: blocked-stale tails AND all-terminal never-closed.

    Three kinds (discriminated by entry["kind"]): `blocked_stale` — high
    completion with only blocked-and-stale goals remaining;
    `blocked_stale_no_motivation` — the same profile on an aspiration whose
    `motivation` yields no usable tokens (g-115-4164); `all_terminal` — every
    non-recurring goal terminal but the aspiration never closed (g-115-2584).
    Mirrors aspirations-precheck/SKILL.md Phase 0.5.0a.

    Routes nothing itself — emits `zombies[]` plus the flag(s) naming the
    handler each class can actually be discharged by:
      `needs_complete_review`         -> /aspirations-complete-review Phase 7.4
      `needs_retire_or_normal_close`  -> retire, or close by the normal path
    A run raises both when the scan matches a mix. The split exists because
    Phase 7.4's closure script validates the supplied rationale against the
    aspiration's `motivation`; without one it cannot discharge the flag at all,
    so a single flag made that class re-fire forever. The orchestrator invokes
    the handler named by the flag, per entry.
    """
    intent = config.get("intent_satisfaction") or {}
    zombie_ratio = intent.get("zombie_completion_ratio")
    min_blocked_hours = intent.get("phase_7_4_min_blocked_hours")
    if zombie_ratio is None or min_blocked_hours is None:
        raise KeyError(
            "aspirations.yaml missing intent_satisfaction.{zombie_completion_ratio,"
            "phase_7_4_min_blocked_hours}"
        )

    # Import the HANDLER's own refusal predicate rather than mirroring it
    # (). aspirations._validate_intent_satisfaction refuses when
    # _motivation_tokens(asp["motivation"]) is EMPTY — which a bare
    # `if not asp.get("motivation")` does NOT reproduce: a motivation of only
    # short words ("a b cd") is present-but-unusable and would still be routed
    # to a handler that cannot discharge it. Mirroring the predicate here would
    # re-introduce exactly the detector/handler divergence this fix exists to
    # close, so this is a deliberate SSOT import (lazy — ~60ms, paid only by
    # the zombie scan, not by the other seven subcommands).
    from aspirations import _motivation_tokens

    active = _active_aspirations(compact)
    now = _now()
    zombies = []

    for asp in active:
        goals = asp.get("goals", [])
        has_recurring = any(g.get("recurring") for g in goals)
        non_recurring = [g for g in goals if not g.get("recurring")]
        if not non_recurring:
            continue
        unfinished = [g for g in non_recurring if g.get("status") not in TERMINAL_STATUSES]
        if not unfinished:
            # All-terminal class (): every non-recurring goal is
            # terminal yet the aspiration is still active. The in-loop closer
            # (complete-review at the completing goal's iteration close) is a
            # moment-in-time trigger — sweep-completions, cross-box closes, and
            # autocompact at the closing moment all miss it, and nothing
            # re-visited (census 2026-07-18: 10 such aspirations, oldest ~2mo).
            # complete-review discriminates the sub-shapes itself: no recurring
            # → fully-complete close; recurring riders → functionally-complete
            # stamp. A present stamp is the sanctioned documented-hold — skip.
            #
            # The `has_recurring and` conjunct was REMOVED (): it gave
            # the documented-hold escape to the recurring shape ONLY, so a
            # fully-complete aspiration with no recurring riders had no exit at
            # all. When its close was independently blocked (the unsatisfiable
            # evidence arithmetic fixed in aspirations.py in the same goal), the
            # flag became PERMANENT rather than suppressible — measured on ZDS
            # , which fired on 3+ consecutive passes with no way to stand
            # it down. 's identical flag WAS killable precisely because it
            # had recurring riders. Honoring the stamp for both shapes means any
            # future unsatisfiable-gate bug degrades to a suppressible flag
            # instead of one that trains the reader to ignore the signal class.
            if asp.get("functionally_complete_at"):
                continue
            nonrec_total, nonrec_completed = effective_counts(asp, include_recurring=False)
            zombies.append({
                "aspiration_id": asp.get("id"),
                "title": asp.get("title"),
                "source": asp.get("source", "agent"),
                "kind": "all_terminal",
                "completion_ratio": round(nonrec_completed / nonrec_total, 3) if nonrec_total else 0.0,
                "blocked_goal_ids": [],
                "has_recurring": has_recurring,
            })
            continue
        if has_recurring:
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
        # RE-ROUTE, do not skip ( / rb-792). The blocked_stale kind
        # routes to complete-review Phase 7.4 -> aspirations-complete-intent.sh,
        # which validates that the rationale quotes a token from the
        # aspiration's `motivation`. An aspiration with no usable motivation
        # therefore CANNOT be discharged by that handler, so the flag re-fires
        # forever and trains the reader to ignore the whole signal class
        # (measured: 3+ consecutive passes on ZDS ).
        #
        # Two fixes were measured and REJECTED before this one — do not
        # re-propose either. (1) Skipping motivation-less aspirations in the
        # DETECTOR: half the ZDS portfolio lacks the field (7 of 14 active there;
        # measured 4 of 31 active here 2026-07-31), so skipping would trade a
        # visible annoyance for an invisible detection gap.
        #
        # That 13%/87% split is also what makes `has_motivation` a real
        # discriminator rather than a flag that is true for everything and
        # therefore separates nothing (guard-1654 — tabulate the boolean against
        # the live population BEFORE branching on it, especially in a path that
        # files goals on a recurring schedule).
        # (2) Backfilling the motivation: self-defeating, because that field IS
        # the reference text the closure gate validates the rationale against —
        # when a gate checks A against B you cannot supply B on the gate's
        # behalf. Detection coverage is preserved here; only the ROUTE changes,
        # to the destination aspirations.py:2387's own comment already names
        # ("retire or complete normally").
        has_motivation = bool(_motivation_tokens(asp.get("motivation") or ""))
        zombies.append({
            "aspiration_id": asp.get("id"),
            "title": asp.get("title"),
            "source": asp.get("source", "agent"),
            "kind": "blocked_stale" if has_motivation else "blocked_stale_no_motivation",
            "completion_ratio": round(completion_ratio, 3),
            "blocked_goal_ids": [g.get("id") for g in unfinished],
        })

    # Two DISTINCT flags so each lands on a handler that can actually discharge
    # it (). Emitting one flag for both classes is what made the
    # motivation-less case permanent: complete-review is the right destination
    # for a motivation-bearing zombie and a structural dead end for one without.
    # A run can raise both when the scan matches a mix.
    dischargeable = [z for z in zombies if z["kind"] != "blocked_stale_no_motivation"]
    no_motivation = [z for z in zombies if z["kind"] == "blocked_stale_no_motivation"]
    flags = []
    if dischargeable:
        flags.append("needs_complete_review")
    if no_motivation:
        flags.append("needs_retire_or_normal_close")
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

    # Cognitive-horizon windows — single source: meta/cognitive-horizons.yaml
    # (BRD Gap 19 / ; rb-335 reader-consumes). Fail loud, no fallback.
    ch = _load_cognitive_horizons()
    fresh_days = ch["pipeline_windows"]["fresh_discovered_window_days"]
    short_win = ch["horizons"]["short"]["re_probe_window_hours"]
    long_win = ch["horizons"]["long"]["re_probe_window_hours"]

    fresh_discovered = [h for h in discovered if _days_since(h.get("formed_date")) <= fresh_days]

    now = _now()
    resolvable_active = []
    time_gated_active = []
    for h in active:
        horizon = h.get("horizon", "")
        formed = _parse_iso(h.get("formed_date"))
        if horizon in ("session", "micro"):
            resolvable_active.append(h)
        elif horizon == "short" and formed and formed + timedelta(hours=short_win) <= now:
            resolvable_active.append(h)
        elif horizon == "long" and formed and formed + timedelta(hours=long_win) <= now:
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
    """Flag when accuracy < critical_threshold with adequate sample.

    Type-segmented (g-001-96 / rb-268): when the daemon-computed meta carries
    `by_type`, the accuracy_low flag is based on the CALIBRATION-RELEVANT subset
    — all commitment types, i.e. everything EXCEPT `exploration` (designed-
    uncertain probes whose low hit-rate must not drag the gate; canonical
    incident g-001-84: aggregate 37.5% fired while high-conviction was 100% and
    every miss was an exploration/calibration designed-uncertain probe). When
    `by_type` is absent/empty (legacy pipeline meta), the flag falls back to the
    aggregate accuracy_pct (flag_basis="aggregate"). `by_confidence_band`
    (g-001-122 / rb-323) is passed through for surfacing WHERE overconfidence
    concentrates, independent of the flag basis.
    """
    crit = config.get("accuracy_critical_threshold")
    min_sample = config.get("accuracy_min_sample")
    if crit is None or min_sample is None:
        raise KeyError("aspirations.yaml missing accuracy_critical_threshold / accuracy_min_sample")

    data = _pipeline_query("--accuracy") or {}
    total = data.get("total_resolved", 0)
    pct = data.get("accuracy_pct", 0)
    by_strategy = data.get("by_strategy") or {}
    by_type = data.get("by_type") or {}
    by_confidence_band = data.get("by_confidence_band") or {}

    # Calibration-relevant basis: when by_type is present, the gate flags on the
    # commitment types only (exclude `exploration`). Otherwise fall back to the
    # aggregate (legacy meta lacking by_type).
    if by_type:
        cr_confirmed = sum(s.get("confirmed", 0) for t, s in by_type.items()
                           if t != "exploration" and isinstance(s, dict))
        cr_total = sum(s.get("total", 0) for t, s in by_type.items()
                       if t != "exploration" and isinstance(s, dict))
        cr_pct = round(cr_confirmed / cr_total * 100, 1) if cr_total > 0 else 0.0
        flag_basis = "calibration-relevant"
        flag_total, flag_pct = cr_total, cr_pct
    else:
        cr_total, cr_pct = 0, 0.0
        flag_basis = "aggregate"
        flag_total, flag_pct = total, pct

    flags = []
    worst = []
    if flag_total >= min_sample and flag_pct < (crit * 100):
        flags.append("accuracy_low")
        worst = [name for name, stats in by_strategy.items()
                 if (stats or {}).get("pct", 100) < 40 and (stats or {}).get("total", 0) >= 3]

    summary = (
        f"accuracy: critical ({flag_pct}% < {crit*100}%, n={flag_total}, basis={flag_basis})"
        if flags else f"accuracy: healthy ({pct}% over {total} resolved, basis={flag_basis})"
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
        "flag_basis": flag_basis,
        "calibration_relevant_total": cr_total,
        "calibration_relevant_pct": cr_pct,
        "by_confidence_band": by_confidence_band,
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
        #  (2026-05-27) REMOVED the  synthetic-tag branch
        # (`or "synthetic" in g.get("tags")`). It was DEAD in production: the
        # live compact projection (_COMPACT_GOAL_KEEP in
        # mind_api/src/endpoints/aspirations.py — the SSOT; the orphaned CLI
        # COMPACT_GOAL_KEEP copy was retired ) strips `tags`, so cmd_cycles
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
        # : also exclude NEVER-ATTEMPTED skips. A failure requires an
        # attempt — 96.5% of live skipped goals (109/113) carry no attempt marker
        # (they are WITHDRAWN: duplicate/superseded/obsolete/misrouted, skipped
        # straight from pending by a dedup sweep, never claimed). Only a skip that
        # WAS attempted (carries `started`, stamped at goal-CLAIM time by
        # aspirations_write.py) is genuine failure evidence. Without this a dedup
        # sweep in ANY active aspiration trips a phantom repeated_failure (it did
        # on ). `started` survives the compact projection (it is in
        # _COMPACT_GOAL_KEEP), so cmd_cycles sees it here. A naive filter on
        # `started` ALONE was reverted under  because `started` had no
        # writer then — genuine failures also lacked it; making it truthful at
        # claim time () is the prerequisite that makes this exclusion
        # safe. test_never_attempted_skips_no_cycle + the (now `started`-carrying)
        # genuine tests pin both directions.
        resolved = [
            g for g in resolved
            if not (
                g.get("status") == "skipped"
                and (
                    (g.get("title") or "").strip().startswith("Unblock:")
                    or not g.get("started")
                )
            )
        ]
        #  (2026-07-16, zeta): exclude recurring goals from the window.
        # Recurring cadence goals (e.g. "Reflect and journal", "Review and resolve
        # hypotheses") re-resolve perpetually by design, share one category within
        # their aspiration, are NOT primitive-prefixed (so the all_primitives
        # suppression below never catches them), and their routine closes produce
        # rb/tree encodings only sometimes — so a recurring-heavy stretch
        # systematically false-positives as zero_learning_velocity (fired 07-15 on
        # a g-001-* routine-close run while the session's overall deep ratio was
        # 69%). Recurring-goal pathology (staleness, cargo-cult closes) has its own
        # dedicated detector (cargo-cult-detector.py via recurring-close.sh) — this
        # cycle detector owns non-recurring learning-intent goals. `recurring` is
        # in _COMPACT_GOAL_KEEP (verified live, not a  dead-branch field);
        # the completion_ratio block below already relies on it the same way.
        resolved = [g for g in resolved if not g.get("recurring")]
        #  (2026-07-14, zeta) — WHY THERE IS NO never-attempted FILTER HERE,
        # and what a future agent must build FIRST before adding one.
        #
        # This detector treats `skipped` as failure evidence. It is not: a skip can
        # be WITHDRAWN work (byte-identical duplicate, superseded, obsolete,
        # misrouted) which was never attempted at all. A failure requires an attempt.
        # Canonical incident: alpha overrode the (correct) duplication gate 3x with
        # one boilerplate justification, landing 3 byte-identical Pearl P3-4 goals
        # (/46/47); the dupes were correctly dedup-skipped 4 min later,
        # never claimed. Those 3 skips filled this lookback window and tripped
        # repeated_failure on  — the team's PRIMARY strategic aspiration —
        # auto-filing a phantom Investigate that pulled an agent off strategic work.
        #
        # CORRECTION (-t, same day): the paragraph that stood here was
        # WRONG on its central claim, and the error is instructive enough to keep.
        # It asserted that `started` "has NO WRITER on the goal path — nothing sets
        # it when a goal is claimed or executed," and inferred that from a grep that
        # came back empty. The grep was empty because it searched for a HARDCODED
        # writer. The real writer was the LLM: aspirations-loop-digest.md Phase 4
        # instructed "aspirations-update-goal.sh status in-progress; started today",
        # which reaches the field through the GENERIC setter and so matches no
        # field-specific grep. Measured across 1695 live goals: 527 (31.1%) carried
        # `started`, with 122 written that very month — not the "~4 legacy records"
        # the old comment claimed. (The 4-of-119 figure was true of SKIPPED goals
        # only, and generalizing it to the whole corpus is what produced the false
        # conclusion.) An empty confirming grep is not proof of absence.
        #
        # So `started` was never a field without a writer; it was a field with an
        # HONOR-SYSTEM writer that drifted to 31% — and honor-system writes are
        # exactly what this framework keeps having to move into bash (loop_state:
        # /05/06, , ).
        #
        # AS OF -t the daemon claim endpoint writes it:
        # aspirations_write.py claim() does `goal.setdefault("started", _claim_ts)`
        # beside claimed_by/claimed_at. It is now script-enforced at the one place a
        # goal is actually attempted, costs zero hot-path tax (`started` was already
        # in _COMPACT_GOAL_KEEP), and setdefault preserves first-attempt semantics
        # across release/re-claim and stale-claim take-back.
        #
        # THE never-attempted FILTER STILL CANNOT LAND HERE — but for a different
        # reason than the old comment gave. It is no longer "there is no marker";
        # it is COVERAGE: 1168 legacy goals pre-date the writer and carry no
        # `started`. A naive `exclude skips lacking started` would classify every
        # one of them as never-attempted and DELETE genuine repeated-failure
        # detection — precisely the trap that caught the  author, stopped
        # only by test_genuine_repeated_failure_still_detected + the 
        # synthetic-skip test (both of which model real goals as having no
        # `started`, which was correct for pre-fix data and is now the thing to
        # migrate). The filter therefore needs a CUTOVER: apply it only to goals
        # created after the claim-writer landed. That is its own goal; file it once
        # coverage has accumulated.
        #
        # (`claimed_by` remains the other candidate marker and remains projected
        # AWAY by _COMPACT_GOAL_KEEP — carrying it would cost the same hot-path tax
        # that correctly killed tag-resurrection above. `started` wins precisely
        # because it is already on the hot path.)
        #
        # This is also WHY the two exclusions that DO exist here (the Unblock:
        # title-prefix above, the removed synthetic-tag branch) are both hacks:
        # every author hit the same missing datum and reached for a proxy.
        # Until the cutover lands, repeated_failure stays ADVISORY (rb-1320) —
        # believe it only after checking whether the window's skips were ever
        # actually attempted.
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
                    #  (rb-3820, diagnosed by ): all-product
                    # windows are velocity-blind by construction — their
                    # deliverables are sibling product-repo commits, which none
                    # of compute_learning_velocity's five counters can see
                    # (rb + guard + sig + tree + framework scripts/conventions
                    # per rb-803). A stretch of product Fix-closes therefore
                    # false-positives zero_learning_velocity until a
                    # batch-encoding goal lands framework-side artifacts
                    # (canonical:  flagged during Vinheim/Lodestar
                    # closes, velocity 0->1.0 only after  encoded).
                    # Suppress when EVERY window goal carries
                    # work_class == "product": strict all() mirrors
                    # all_primitives, keeps the detector fully live for mixed
                    # and framework windows, and legacy goals missing
                    # work_class fail the equality so their behavior is
                    # unchanged (safe-cutover semantics per -t).
                    # work_class is in _COMPACT_GOAL_KEEP (added ,
                    # both copies) — without that this check would be a dead
                    # branch ( class). Placed BEFORE the trajectory
                    # probe: also saves the 60s subprocess on product windows.
                    # Attribution option (a) — scanning sibling product repos
                    # by goal-id — was rejected as box-DEPENDENT: repo
                    # presence varies per box, so the flag would still fire on
                    # boxes lacking the repo (worse than suppression, which
                    # reads only the shared store and is box-consistent).
                    all_product = all(
                        g.get("work_class") == "product" for g in recent
                    )
                    if all_product:
                        continue

                    # : completion-ratio gate. A near-complete
                    # aspiration consolidating its final goals will
                    # naturally have all recent goals in the same
                    # category — that's the convergence shape of healthy
                    # consolidate-before-expand, not unproductive cycling.
                    # Suppress the velocity probe when completion_ratio >=
                    # completion_ratio_suppress (default 0.8).
                    non_recurring = [g for g in goals if not g.get("recurring")]
                    completed = [g for g in non_recurring if g.get("status") == "completed"]
                    completion_ratio = (
                        len(completed) / len(non_recurring) if non_recurring else 0.0
                    )
                    suppress_threshold = cycle_cfg.get("completion_ratio_suppress", 0.8)
                    if completion_ratio >= suppress_threshold:
                        continue

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
# Matched as a substring against the lowercased goal title (see cmd_user_goals
# below): a [user]-only goal whose title contains any of these is flagged as
# agent-provisionable (the agent can self-service it — routing violation).
# Extended  (2026-07-08): the original 9-verb list was ~53% false-
# negative vs the 10% threshold (bravo  weekly classifier review) —
# common fleet close-verbs (sweep/resolve/review/reflect/encode/audit/...) were
# missed. "verify" -> "verif" so the stem also substring-matches
# verified/verification/verifying (the single most-common fleet close-verb,
# previously unmatched because "verify" is not a substring of "verified").
# NOTE: "form" was in the proposed set but is DELIBERATELY OMITTED — as a bare
# substring it matches platform/information/performance/format/transform/formal
# (high false-positive rate); safely adding it requires word-prefix (stem-token)
# matching instead of substring — deferred as a follow-up (the pending queue
# already carries matching-related work). The verbs kept below are all
# distinctive substrings (low FP); "fix" carries a minor prefix/suffix FP
# accepted as low-harm (this sweep only surfaces candidates).
AGENT_ACTION_KEYWORDS = [
    "deploy", "commit", "script", "test", "analyze",
    "run", "check", "verif", "monitor",
    "sweep", "fix", "resolve", "review", "reflect", "generate",
    "encode", "replay", "drain", "evict", "audit", "reconcil",
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

# Drain-action goal title template markers + positive-signature matcher now live
# in the shared SSOT module core/scripts/_drain_title.py (imported at top), so BOTH
# this file's suggested_goal template/dedup AND goal-selector.py's
# _is_owner_scoped_goal key off the same constants and cannot drift
# (;  / rb-3452 "assert the mechanism, not the case").


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

    # temp/ holds THREE file classes (core/config/conventions/temp-store.md).
    # The first two are counted below; the third is the COMPLEMENT of both
    # allowlists and is handled by `unclassified_count` (see  note):
    #   - drainable working docs (.md/.json) -> /drain-temp encodes to the tree
    #     then archives to drained/. Counted as `count`.
    #   - pure ephemera (.log/.txt/.py/.sh/.err: test-suite output, tool dumps
    #     like leak-check.txt, and one-shot scratch scripts like build-*.py /
    #     orphan-*.py / restart-poller.sh / gs.err) -> carry NO knowledge;
    #     /drain-temp Phase 1.5 PURGES them (deletes — gitignored + unencodable,
    #     120-min age guard protects in-flight writes). Counted as
    #     `ephemera_count`.  added the scratch-script class (.py/.sh/
    #     .err);  added .log/.txt.
    # Both classes accumulate in temp/ root, so BOTH must feed the pressure
    # signal — else the ephemera slush stays invisible to the drain trigger and
    # grows unbounded (: 7 .log/.txt survived a full drain because the
    # glob AND this metric both saw only .md/.json). Threshold flags fire on the
    # COMBINED pressure; the two counts stay distinct so the drain goal can name
    # what it drains vs purges.
    EPHEMERA_SUFFIXES = (".log", ".txt", ".py", ".sh", ".err")
    count = 0
    ephemera_count = 0
    # : THIRD class. The two classes above are extension ALLOWLISTS, so
    # every other suffix in temp/ root was invisible to this metric — not counted,
    # not reported, no hint it existed. Measured on a downstream deployment
    # 2026-07-30: temp/ root held 26 files and this function returned 7 (a 3.7x
    # undercount); the missing 19 were 6 .pdf, 4 .yaml, 4 .docx, 2 .jsonl, 1 .ps1,
    # 1 extensionless. Reported separately and deliberately NOT folded into
    # pressure_count: these files are not drain-drainable and not purgeable, so
    # counting them toward the drain threshold would fire drain goals that cannot
    # drain them. Visibility is the fix; changing threshold semantics is not.
    unclassified_count = 0
    # WHY NOT rglob (): the originating goal's verification proposed
    # reconciling against "an independent rglob enumeration". That is the WRONG
    # denominator and would be a regression — rglob picks up drained/ (195 files
    # at measure time), and a drained file is by definition NOT undrained
    # pressure. iterdir over temp/ ROOT is correct; the undercount was never a
    # traversal-depth bug, it was the extension allowlist above.
    temp_dir = (AGENT_DIR / "temp") if AGENT_DIR is not None else None
    ephemera_files = []
    if temp_dir is not None and temp_dir.is_dir():
        for f in temp_dir.iterdir():
            if not f.is_file():
                continue
            if f.suffix in (".md", ".json"):
                count += 1
            elif f.suffix in EPHEMERA_SUFFIXES:
                ephemera_count += 1
                ephemera_files.append(f)
            else:
                unclassified_count += 1

    #  / guard-273: a purge suggestion must never count a git-TRACKED
    # file. Extension alone cannot tell a business record from scratch, and the
    # ephemera classes include .txt/.py/.sh — all plausible tracked deliverables.
    #
    # SCOPED HONESTLY: at measure time ZERO ephemera-classified files were
    # tracked, so this defect was LATENT, not realised. The originating goal
    # claimed the emitted text "proposes destroying deliverables" and cited two
    # .pdf files as evidence; .pdf is not in EPHEMERA_SUFFIXES, so those could
    # never have entered a purge count. That specific claim does not hold. The
    # general risk does: `.gitignore` now ignores ALL of agents/*/temp/*
    # (), but it does not UNTRACK what was tracked before that rule,
    # and legacy-tracked files remained under temp/. So tracked files DO live
    # here, and one rename away from a .txt is a purge that names a deliverable.
    # This check makes that structurally impossible.
    #
    # FAILS OPEN: any git error leaves the counts untouched. A precheck advisory
    # must never break the loop over an unavailable git.
    ephemera_tracked_excluded = 0
    if ephemera_files:
        try:
            import subprocess as _sp
            _r = _sp.run(["git", "ls-files", "-z", "--", str(temp_dir)],
                         capture_output=True, text=True, timeout=15,
                         cwd=str(PROJECT_ROOT))
            if _r.returncode == 0:
                _tracked = {p.replace("\\", "/") for p in _r.stdout.split("\0") if p}
                for f in ephemera_files:
                    try:
                        _rel = f.relative_to(PROJECT_ROOT).as_posix()
                    except ValueError:
                        continue
                    if _rel in _tracked:
                        ephemera_count -= 1
                        unclassified_count += 1
                        ephemera_tracked_excluded += 1
        except Exception:
            pass  # fail open — see comment above

    pressure_count = count + ephemera_count

    # Dedup: if a drain-temp ACTION goal is already open, do NOT re-suggest filing —
    # else every iteration above threshold would spawn a duplicate HIGH goal.
    # Only ACTION goals drain temp/; a goal whose title merely MENTIONS temp-drain must
    # NOT satisfy the dedup. The match below is a POSITIVE drain-action signature
    # () firing only on the real "Maintain: drain N accumulated temp/ working
    # docs" template this check files — so BOTH an Investigate:/Idea: analysis goal (the
    # original  case) AND a "Maintain:" goal merely ABOUT the drain ()
    # are excluded by CONSTRUCTION. Otherwise such a goal falsely counts as the open drain
    # goal, permanently suppressing the real drain goal from filing and letting temp/ grow
    # unbounded (the confirmed  root cause: 134 undrained files, 0 auto-filed).
    # Agent-scope the dedup (): the undrained-doc COUNT above targets
    # AGENT_DIR/temp (the BOUND agent's store), so this dedup must match. World-
    # queue drain goals appear in EVERY agent's compact, so without scoping, one
    # agent's open drain goal set existing != None for ALL agents -> suggested_goal
    # stays null (temp_drain_pending) fleet-wide and every OTHER agent's temp/ grew
    # unbounded. Skip a drain goal that is identifiably ANOTHER agent's; scope by
    # filed_by_agent (stamped at add time, ) / handoff_to. A drain goal
    # with no owner stamp still dedups fleet-wide (conservative — real auto-filed
    # drain goals always carry filed_by_agent; preserves the legacy contract).
    agent_name = AGENT_DIR.name if AGENT_DIR is not None else None
    existing = None
    for asp in _active_aspirations(compact):
        for g in asp.get("goals", []):
            if g.get("status") in ("pending", "in-progress"):
                # Positive drain-action signature (/, shared
                # SSOT is_drain_action_title): match ONLY the real drain template
                # this check files (prefix + count + infix). A goal whose title
                # merely MENTIONS temp-drain — an Investigate:/Idea: analysis goal
                # () OR a "Maintain:" goal ABOUT the drain () —
                # never starts with the template prefix, so it is excluded by
                # CONSTRUCTION and can no longer falsely count as the open drain goal.
                if not is_drain_action_title(g.get("title")):
                    continue
                owner = g.get("filed_by_agent") or g.get("handoff_to")
                if owner is not None and owner != agent_name:
                    continue  # another agent's drain goal () — not ours
                existing = g.get("id")
                break
        if existing:
            break

    flags = []
    suggested_goal = None
    if pressure_count >= drain_threshold and existing is None:
        flags.append("temp_drain_needed")
        _purge_clause = (f" + purge {ephemera_count} stale ephemera file(s)"
                         if ephemera_count else "")
        suggested_goal = {
            "title": (f"{_DRAIN_GOAL_TITLE_PREFIX}{count} {_DRAIN_GOAL_TITLE_INFIX} "
                      f"to the knowledge tree" + _purge_clause),
            "priority": "HIGH",
            "participants": ["agent"],
            "description": (
                f"agents/<agent>/temp/ holds {count} undrained working docs"
                + (f" and {ephemera_count} pure-ephemera scratch file(s)"
                   if ephemera_count else "")
                + f" (combined >= drain threshold {drain_threshold}). Invoke "
                f"/drain-temp to encode each working doc into the knowledge "
                f"tree / reasoning bank / experience and move it to "
                f"temp/drained/"
                + (", and PURGE the stale ephemera (Phase 1.5 — pure ephemera "
                   "carries no knowledge, so it is deleted, not archived)"
                   if ephemera_count else "")
                + ". temp/ is a staging SSOT, not an archive — undrained/"
                f"unpurged accumulation is the slush-directory failure mode the "
                f"file-model normalization exists to prevent."
            ),
        }
        # Route to the temp OWNER, not the content classifier (, rb-3876).
        # /drain-temp is bound-agent-scoped (drains agents/<BOUND-AGENT>/temp/), so this
        # goal is satisfiable ONLY by the agent whose store triggered it (agent_name).
        # Without an explicit intended_agent, capability_route's Tier-3 "knowledge tree"
        # ->bravo heuristic (matching the drain DESTINATION named in title/description,
        # NOT the owner) misroutes to bravo; the drain then no-ops on bravo's temp while
        # the owner's grows unbounded (observed: alpha temp 14->83). The daemon add-goal
        # Phase D honors an explicit intended_agent (skips the classifier). agent_name is
        # None only when AGENT_DIR is unresolved (no live agent) — omit then so the
        # pre-fix classifier path still applies (no regression).
        if agent_name:
            suggested_goal["intended_agent"] = agent_name
    elif pressure_count >= drain_threshold and existing is not None:
        flags.append("temp_drain_pending")
    elif pressure_count >= warn_threshold:
        flags.append("temp_pressure_warn")

    # : surface unclassified in the SUMMARY, not just the JSON body. The
    # summary is what the precheck prints every iteration and therefore the only
    # part a reader reliably sees; a count that exists only in the JSON is the
    # same invisibility this fix exists to remove. Named "not-drainable" rather
    # than a bare number so it cannot be misread as additional drain pressure.
    if pressure_count or unclassified_count:
        _breakdown = f"{count} undrained doc(s)"
        if ephemera_count:
            _breakdown += f" + {ephemera_count} ephemera(.log/.txt/.py/.sh/.err)"
        if unclassified_count:
            _breakdown += (f" + {unclassified_count} not-drainable"
                           "(other suffixes, excluded from thresholds)")
        if ephemera_tracked_excluded:
            _breakdown += (f" [{ephemera_tracked_excluded} git-tracked file(s) "
                           "reclassified out of purge scope]")
        summary = (
            f"temp-pressure: {_breakdown} "
            f"(warn>={warn_threshold}, drain>={drain_threshold}"
            + (f"; open drain goal {existing}" if existing else "") + ")"
        )
    else:
        summary = "temp-pressure: clean"
    return {
        "subcommand": "temp-pressure",
        "summary": summary,
        "flags": flags,
        "count": count,
        "ephemera_count": ephemera_count,
        "pressure_count": pressure_count,
        "unclassified_count": unclassified_count,
        "ephemera_tracked_excluded": ephemera_tracked_excluded,
        "temp_root_total": count + ephemera_count + unclassified_count,
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

    refresh_info = None
    if args.compact_path:
        # Caller supplied its own snapshot and owns its freshness — never
        # rebuild the agent's compact behind a caller that asked for a
        # specific file.
        with open(args.compact_path, "r", encoding="utf-8") as f:
            compact = json.load(f)
    else:
        #  — own our own input. The refresh used to live ONLY in a
        # separate, LLM-invoked precheck step, so every detector's correctness
        # rode on the LLM having remembered a different line first. It drifted
        # twice in one day, each time producing a confident WRONG answer rather
        # than an error. Refreshing here removes the cross-step dependency
        # instead of documenting it again: per rb-428 / , the durable
        # pattern is to move the obligation into the script, never into another
        # instruction telling the LLM to remember.
        #
        # Gated on staleness so the common fresh case costs zero subprocesses.
        # `not checked` covers the missing-compact case, which used to make
        # this whole script a no-op on a fresh agent.
        pre = _compact_staleness()
        if pre.get("stale") or not pre.get("checked"):
            refresh_info = _refresh_compact()
        compact = _load_compact()
        if compact is None:
            print(json.dumps({
                "subcommand": args.subcommand,
                "summary": "no aspirations-compact.json (run aspirations-compact.sh first)",
                "flags": [],
                "compact_refresh": refresh_info,
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

    #  — staleness is reported at the SINGLE output chokepoint, so
    # all 9 subcommands are covered without touching any of the 8 detectors
    # (they take `compact` as a parameter and none re-reads live state, so the
    # defect is uniform across them and the fix should be too).
    freshness = _compact_staleness(args.compact_path)
    result["compact_freshness"] = freshness
    if refresh_info is not None:
        result["compact_refresh"] = refresh_info
    if freshness.get("stale"):
        # Reaching here means the auto-refresh above did NOT resolve the
        # staleness (it errored, timed out, or the builder exited non-zero) —
        # or a --compact-path caller supplied a stale snapshot. Either way this
        # is no longer routine drift, it is a rebuild that failed, so fail LOUD.
        #
        # A stale run's verdict is not wrong-SHAPED, it is confidently-wrong-
        # VALUED — the summary reads exactly like a healthy one. Marking the
        # summary itself (not just a nested field) is what removes the silence,
        # because the summary is what a consumer reads first.
        # Headline the STALENESS MAGNITUDE (how far behind the source), not the
        # snapshot's wall-clock age — a 2m-old compact written 1.9m behind its
        # source is barely stale, while a 101m-old one 95m behind is the
        # originating incident. Both numbers ship; the actionable one leads.
        # Attribute the surviving staleness correctly. "rebuild failed" and
        # "rebuild worked but the shared world queue was written again in the
        # interval" are DIFFERENT problems pointing at different fixes, and in a
        # multi-agent fleet the second is routine — any partner filing a goal
        # writes that queue. Reporting the race as a builder failure sends the
        # reader to debug a builder that is working fine.
        if refresh_info is None:
            rebuilt = "not auto-rebuilt (explicit --compact-path)"
        elif refresh_info.get("rc") == 0:
            rebuilt = "rebuild SUCCEEDED — source written again since; likely a concurrent write, not a builder fault"
        else:
            rebuilt = f"auto-rebuild FAILED (rc={refresh_info.get('rc')})"
        # .get() with fallbacks, NOT bracket indexing: these three keys are set
        # together inside one branch of the probe, so any future edit that sets
        # `stale` without them would raise KeyError HERE — after all 8 detectors
        # already ran, discarding their entire output. The probe promises it
        # cannot break the detectors; that promise only holds if its consumer
        # honours it too (fresh-eyes finding F-001 on this file).
        ahead = freshness.get("source_ahead_minutes", "?")
        kind = freshness.get("newer_source_kind", "source")
        age = freshness.get("age_minutes", "?")
        result["summary"] = (
            f"[COMPACT STALE — {ahead}m behind the {kind} queue "
            f"(snapshot {age}m old; {rebuilt}); this verdict "
            f"is computed from that snapshot and may be wrong] "
            f"{result.get('summary', '')}"
        )
        result.setdefault("flags", []).append("compact_stale")

    log_script_decision("precheck-eval", {
        "subcommand": args.subcommand,
        "flags": result.get("flags", []),
        "summary": str(result.get("summary", ""))[:200],
    })
    print(json.dumps(result, ensure_ascii=False, default=str))
    sys.exit(1 if result.get("flags") else 0)


if __name__ == "__main__":
    main()
