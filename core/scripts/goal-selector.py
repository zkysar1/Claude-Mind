#!/usr/bin/env python3
"""Goal scoring and selection with exploration noise.

Implements the scoring formula from aspirations/SKILL.md Goal Selection Algorithm.
The LLM no longer computes scores — this script handles the arithmetic.
The LLM still handles Phase 2.5 (metacognitive assessment) but MUST NOT override the
ranking except via a sanctioned deviation code at claim time (Scorer Sovereignty Layer B;
see scorer-verdict-gate.py + the system-constraints-loop/scorer-sovereignty tree node).

Scoring criteria (21 deterministic + 1 stochastic weighted factors):
  priority × 1.0 + deadline_urgency × 1.0 + agent_executable × 0.8
  + variety_bonus × 0.5 + streak_momentum × 0.5 + novelty_bonus × 0.6
  + recurring_urgency × 0.8 + recurring_saturation × 0.8 + per_goal_saturation × 0.8
  + user_signal_boost × 1.2 + class_balance_bonus × 0.8
  + reward_history × 0.5 + completion_pressure × 0.8 + depth_bonus × 0.6
  + tail_bonus × 0.8
  + evidence_backing × 0.7 + deferred_readiness × 0.6
  + context_coherence × 1.0 + skill_affinity × 0.4 + directive_boost × 1.5
  + co_invest_alignment × 0.5
  + exploration_noise × (epsilon × noise_scale)  [dynamic weight]

  co_invest_alignment: +1.0 raw bonus when this candidate's co_parent_id
    matches a partner's live team-state in_flight.co_parent_id — pair-
    iteration bias. Schema + protocol in core/config/conventions/coordination.md
    Co-Investigation Protocol section. g-115-563.

  context_coherence: +2.0 if same category as last goal, 0 otherwise.
    Context-pressure agnostic — same-category reuse saves tokens regardless of zone.

  recurring_urgency: base + log2(1 + overdue_ratio) * log_scale (logarithmic, no cap)
  recurring_saturation: -(ratio * max_penalty) penalty when recurring goals dominate recent selections (CLASS level)
  per_goal_saturation: flat penalty when the SAME goal_id fires repeatedly in the recent window (GOAL level).
    Config: aspirations.yaml → per_goal_saturation. Tranche B (rb-390).
  user_signal_boost: reads <agent>/session/user-signal-snapshot.yaml (refreshed
    by the signal-refresh hook) and boosts goals listed in the
    pending_questions.silent_48h_goal_ids snapshot field — i.e., goals whose
    user-facing question has gone unanswered for 48h+. Path A (per-goal
    user_signal_kind/user_thread_id) retired 2026-04-24 (g-252-03) after
    fields stayed 0/798 for 6+ days; only the snapshot-level Path B contributes.
    Fail-open: missing snapshot OR empty silent_48h list → zero contribution.
    Config: aspirations.yaml → user_signal_boost. Tranche C (rb-390).
  class_balance_bonus: pulls under-represented work_class values up when the
    last-N session completions distribution drifts from configured targets.
    Goals with no work_class default to "unclassified" and are excluded from
    the balance computation.
    Fail-open: empty distribution or missing targets → zero contribution.
    Config: aspirations.yaml → class_balance (nested: targets, window_size,
    max_boost, max_penalty). Tranche C (rb-390).
  deferred_readiness: +1.5 when a deferred goal's time has arrived
  exploration_noise: random(0,1) scaled by developmental epsilon.
    At exploring stage (~0.85 epsilon): noise can reorder rankings.
    At mastering stage (~0.19 epsilon): noise mostly breaks ties.
"""

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
import hashlib
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml  # Required — tree.py already depends on PyYAML

from _paths import (WORLD_DIR, AGENT_DIR, META_DIR, CONFIG_DIR, CORE_ROOT,
                    agents_root as _agents_root, read_agent_conf)
from _fileops import locked_modify_yaml  # noqa: E402  ( applications_log)
from wm import read_wm  # noqa: E402
from cadence_signals import evaluate_cadence_signal  # noqa: E402  ( signal-gated cadence)
# Single source of truth for terminal goal statuses — see aspirations.py.
# Derived sets below (SKIP_STATUSES, ABANDONED_STATUSES) stay consistent if a new
# status is added to TERMINAL_GOAL_STATUSES.
from aspirations import TERMINAL_GOAL_STATUSES, STRUCTURED_DEFER_PREFIXES  # noqa: E402
from _goal_census import effective_counts  # noqa: E402  (B9-deep census-augmented counts)
from _iaus_scorer import iaus_score  # noqa: E402  ( flagged utility scorer)
from _runner_capabilities import (  # noqa: E402  ( per-runner capability filter)
    derive_runner_capabilities, box_config_from_conf, merge_capability_config,
    goal_is_locally_executable, goal_required_capabilities)
from _drain_title import is_drain_action_title  # noqa: E402  ( owner-scope drain SSOT)
SKIP_STATUSES = TERMINAL_GOAL_STATUSES | {"in-progress"}              # not selectable
ABANDONED_STATUSES = TERMINAL_GOAL_STATUSES - {"completed"}            # terminal but not "done"

# Populated by main() before scoring loop fires; consumed by score_goal's
# cross_aspiration_support criterion (LifingPolls item 2). Empty fallback
# means the criterion contributes 0 — never errors when supports[] is unset.
_ASP_COMPLETION_RATIOS: dict = {}
_STRUCTURED_DEFER_PREFIXES_LOWER = tuple(p.lower() for p in STRUCTURED_DEFER_PREFIXES)

#  per-runner capability filter. Lazily-computed, process-cached set of
# capability tokens THIS runner provides (config override > cheap probes). Cached
# because the probes (shutil.which / import checks) should run at most once per
# process, and cmd_select reads it once per selection.
_RUNNER_CAPABILITIES = None


def _get_runner_capabilities():
    """Return (process-cached) the capability set this runner provides.

    Two config layers merge before probing (g-115-3079):
      1. `runner_capabilities` in aspirations.yaml -- git-shared, so it applies
         FLEET-WIDE. Correct for "no box in this fleet has a GPU"; WRONG for any
         claim true of one box only.
      2. `RUNNER_CAPABILITIES_{PROVIDES,LACKS,PROBE}` in the bound agent's
         `local-paths.conf` -- the only genuinely per-box surface (gitignored AND
         in owncloud_sync._EXCLUDE_NAMES, so it never reaches git or S3). This is
         where a Studio host asserts `studio-session`, which by design is never
         probed (a transient runtime resource on one host, not a box property).
    merge_capability_config unions provides/lacks (lacks stays authoritative) and
    lets the box win on `probe`.

    Fail-open at every layer: a config error degrades to probe-only, and a probe
    error to the empty set. An empty set never filters a goal that lacks
    requires_capability, so the conservative default is a no-op filter."""
    global _RUNNER_CAPABILITIES
    if _RUNNER_CAPABILITIES is not None:
        return _RUNNER_CAPABILITIES
    fleet_cfg = {}
    try:
        asp_config = read_yaml_file(CONFIG_DIR / "aspirations.yaml")
        rc = asp_config.get("runner_capabilities")
        if isinstance(rc, dict):
            fleet_cfg = rc
    except Exception:
        fleet_cfg = {}
    box_cfg = {}
    try:
        box_cfg = box_config_from_conf(read_agent_conf())
    except Exception:
        box_cfg = {}
    try:
        _RUNNER_CAPABILITIES = derive_runner_capabilities(
            merge_capability_config(fleet_cfg, box_cfg))
    except Exception:
        _RUNNER_CAPABILITIES = set()
    return _RUNNER_CAPABILITIES
# : the one structured prefix that NEVER auto-clears. collect_eligible
# and collect_blocked exempt it from the 120h defer fall-through so a genuinely
# human-gated goal stays suppressed-from-selector + counted-in-blocked[] (enabling
# quiescence) instead of re-surfacing as a candidate every iteration. The other
# structured prefixes keep the fail-open expiry (their sweeps auto-clear them).
_HUMAN_BLOCKED_PREFIX = "human_blocked:"


def _has_future_deferred_until(goal):
    """True only when deferred_until is present AND still in the future.

    The single predicate both collect_candidates and collect_blocked use to
    decide whether a goal's structural time gate SUPERSEDES its defer_reason.
    It must be one helper, not two inline checks: the two call sites are
    required to stay logical complements, and the defect this closes
    (g-115-3150) was precisely the two of them agreeing on a WRONG shared
    precedence — `if not goal.get("deferred_until")`, which treats a PAST date
    the same as a FUTURE one and hands the goal to a time gate that clears
    past dates unconditionally. A live defer_reason was therefore never
    evaluated, and the goal fell out of BOTH lists.

    Corrupt/unparseable values return False (no structural gate) so the
    defer_reason arm — which carries its own fail-open expiry — decides,
    rather than a garbage timestamp silently releasing the goal.
    """
    raw = goal.get("deferred_until")
    if not raw:
        return False
    try:
        return datetime.fromisoformat(str(raw)) > datetime.now()
    except (ValueError, TypeError):
        return False


def _parse_rne_dt(rne):
    """Parse resolves_no_earlier_than into a datetime, or None on failure.

    The field arrives in TWO shapes: date-only ("2026-07-20") from older
    records, and full datetime ("2026-07-20T00:00:00") from the sq-009
    hypothesis template. date.fromisoformat RAISES on the datetime shape
    (verified Python 3.12.3), so the prior per-site try/except silently
    no-opped the hypothesis time gate for every template-filed goal —
    g-115-2507 incident: a 3-days-future hypothesis goal top-ranked at 8.57.
    Same inert-gate class as rb-3830/rb-3834 (swallowed-parse gates).
    """
    if not rne:
        return None
    s = str(rne)
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        pass
    try:
        return datetime.fromisoformat(f"{s}T00:00:00")
    except (ValueError, TypeError):
        return None


def _synth_blocker_ref_from_structured_defer(goal):
    """For quiescence-gate compatibility (, extended ): when a
    goal is structurally deferred/blocked but lacks a valid typed blocker_ref,
    synthesize one so quiescence-gate.py C2 (blocker_ref_required) + C3
    (future-expiry) treat the structural marker as structured-enough. Evaluation
    order (first match wins):
      (f) BARE-STRING blocker_ref -> coerce to a typed resource ref, preserving
          the original string in original_ref. Checked right after the dict
          short-circuit so a legacy string ref is never DISCARDED (the L1659
          call site would otherwise return None for it). (g-115-1794)
      (b) deferred_until is a FUTURE ISO timestamp -> time gate is the structural
          marker; expires_at=deferred_until. Evaluated BEFORE (a) — g-115-1751:
          an explicit future deferred_until is the authoritative expiry and wins
          over a's 120h fail-open.
      (a) defer_reason has STRUCTURED_DEFER_PREFIXES (precondition_unmet:,
          blocked_on_dependency:, Circuit breaker:). Write-side accepts these
          without --blocker-ref; without this synth quiescence sees None forever.
          Expiry = defer_reason_set_at + 120h, ROLLED FORWARD to now+120h when
          that window has already lapsed (g-115-1794 C3 fix — a long-lived
          structured defer previously synthesized an already-expired ref,
          tripping C3 into false denial + B7 churn).
      (c) resolves_no_earlier_than FUTURE date -> hypothesis time gate.
      (d) bare blocked_by (non-empty) with no structured defer -> dependency wait.
          UNCONDITIONAL synth (safe: quiescence only evaluates in all-blocked,
          where every dependency head is provably a non-candidate). (g-115-1794)
      (e) any other non-empty defer_reason (narrative defer, no structured prefix,
          no blocked_by) -> external gate that already passed the defer-time
          capability gate (probe-before-defer.md). (g-115-1794)
    All synthesized refs use type=resource (catch-all for structural waits — see
    BLOCKER_REF_TYPES in aspirations.py) + md5(stable-key)[:12] external_id so C4
    hysteresis hash stays stable across iters. Returns the existing dict ref
    unchanged if present, a synthesized dict for (f/b/a/c/d/e), else None."""
    existing = goal.get("blocker_ref")
    if isinstance(existing, dict):
        # C3 roll-forward for a STORED typed dict ref whose expires_at has
        # lapsed ( sibling): a long-lived user_action / infrastructure
        # ref on a user-gated recurring monitor (e.g.  game-session,
        #  npc-deploy) keeps its stored typed ref for months, but its
        # original short TTL (created_at + N days) expired long ago — tripping
        # quiescence C3 (future-expiry) into false denial + B7 churn while the
        # underlying blocker is still genuinely active (Phase 0.5b re-probes it
        # independently — blocker-recheck cleared 0, confirming still-active).
        # expires_at is a fail-open RE-CHECK window, NOT a hard deadline, so roll
        # a lapsed window forward to now+120h, preserving type/external_id/
        # created_at. Mirrors path (a) roll-forward (below) +  /
        #  rolling-expiry idiom. A future expires_at is returned
        # unchanged (no behavior change for already-fresh stored refs).
        try:
            _exp = existing.get("expires_at")
            if _exp and datetime.fromisoformat(str(_exp)) <= datetime.now():
                _rolled = dict(existing)
                _rolled["expires_at"] = (
                    datetime.now() + timedelta(hours=120)
                ).isoformat(timespec="seconds")
                _rolled["expiry_rolled_forward"] = True
                return _rolled
        except (ValueError, TypeError):
            pass
        return existing
    # Path (f): legacy BARE-STRING blocker_ref (). A plain-string
    # blocker_ref (e.g. "worldbuilders-apikey-missing" from an older write path)
    # is NOT a dict, so quiescence C2 counts it missing — AND the L1659 call site
    # would DISCARD the string by returning None here. Coerce to a typed resource
    # ref, preserving the original string so the human-readable signal survives.
    if existing:
        _now_f = datetime.now()
        _h_f = hashlib.md5(str(existing).encode("utf-8", errors="replace")).hexdigest()[:12]
        return {
            "type": "resource",
            "external_id": f"legacy-ref:{_h_f}",
            "state_hash": None,
            "created_at": _now_f.isoformat(timespec="seconds"),
            "expires_at": (_now_f + timedelta(hours=120)).isoformat(timespec="seconds"),
            "synthesized": True,
            "original_ref": str(existing),
        }
    defer = goal.get("defer_reason") or ""
    deferred_until = goal.get("deferred_until")

    # Path (b): deferred_until time gate. CHECKED BEFORE path (a) — :
    # an explicit future deferred_until is the AUTHORITATIVE structural expiry and
    # must win over the 120h structured-prefix fail-open. A goal carrying BOTH a
    # structured-prefix defer_reason AND a future deferred_until previously matched
    # path (a) first and got the shorter set_at+120h expiry — already PAST for
    # long-deferred goals — tripping quiescence-gate C3 (expires_at must be future)
    # into false quiescence denial + B7 backoff churn. When deferred_until is absent
    # or already past, this block falls through to path (a) unchanged.
    if deferred_until:
        try:
            expires_dt = datetime.fromisoformat(str(deferred_until))
            if expires_dt > datetime.now():
                set_at = goal.get("defer_reason_set_at")
                try:
                    created = datetime.fromisoformat(str(set_at)) if set_at else datetime.now()
                except (ValueError, TypeError):
                    created = datetime.now()
                key = f"{deferred_until}:{defer[:80]}"
                h = hashlib.md5(key.encode("utf-8", errors="replace")).hexdigest()[:12]
                return {
                    "type": "resource",
                    "external_id": f"time-gate:{h}",
                    "state_hash": None,
                    "created_at": created.isoformat(timespec="seconds"),
                    "expires_at": expires_dt.isoformat(timespec="seconds"),
                    "synthesized": True,
                }
        except (ValueError, TypeError):
            pass

    # Path (a): structured-prefix defer_reason (120h fail-open expiry). Reached only
    # when there is no future deferred_until (path (b) above returns first for those).
    if defer and defer.lower().startswith(_STRUCTURED_DEFER_PREFIXES_LOWER):
        set_at = goal.get("defer_reason_set_at")
        try:
            created = datetime.fromisoformat(str(set_at)) if set_at else datetime.now()
        except (ValueError, TypeError):
            created = datetime.now()
        expires = created + timedelta(hours=120)
        # C3 roll-forward (): a long-lived structured defer whose
        # defer_reason_set_at is >120h in the past previously synthesized an
        # ALREADY-EXPIRED ref (created+120h < now), tripping quiescence C3 into
        # false denial + B7 churn (observed : set_at 2026-05-23 ->
        # expires 2026-05-28). The 120h is a fail-open RE-CHECK window, not a real
        # deadline — Phase 0.5b re-probes the defer independently — so roll the
        # window to now+120h when it has lapsed (self-healing; created_at stays
        # set_at, mirrors the not-my-lane /  rolling expiry).
        _now_a = datetime.now()
        if expires <= _now_a:
            expires = _now_a + timedelta(hours=120)
        h = hashlib.md5(defer.encode("utf-8", errors="replace")).hexdigest()[:12]
        return {
            "type": "resource",
            "external_id": f"structured-defer:{h}",
            "state_hash": None,
            "created_at": created.isoformat(timespec="seconds"),
            "expires_at": expires.isoformat(timespec="seconds"),
            "synthesized": True,
        }

    # Path (c): hypothesis time gate via resolves_no_earlier_than (date string).
    # Hypothesis-tracked goals carry an ISO date; treat it as the structural expiry.
    rne = goal.get("resolves_no_earlier_than")
    if rne:
        try:
            # Handles BOTH date-only and datetime forms ( — the old
            # f"{rne}T00:00:00" promotion raised on datetime-form values).
            expires_dt = _parse_rne_dt(rne)
            if expires_dt is not None and expires_dt > datetime.now():
                created_str = goal.get("created_at") or goal.get("started")
                try:
                    created = datetime.fromisoformat(str(created_str)) if created_str else datetime.now()
                except (ValueError, TypeError):
                    created = datetime.now()
                key = f"rne:{rne}:{goal.get('id','')}"
                h = hashlib.md5(key.encode("utf-8", errors="replace")).hexdigest()[:12]
                return {
                    "type": "resource",
                    "external_id": f"hypothesis-gate:{h}",
                    "state_hash": None,
                    "created_at": created.isoformat(timespec="seconds"),
                    "expires_at": expires_dt.isoformat(timespec="seconds"),
                    "synthesized": True,
                }
        except (ValueError, TypeError):
            pass

    # Path (d): bare blocked_by dependency (). A goal blocked on a
    # machine-readable predecessor (blocked_by non-empty) but carrying NO
    # structured-prefix defer_reason previously synthesized nothing -> quiescence
    # C2 rejected it forever, forcing B7 backoff churn instead of honest quiescent
    # sleep (observed /126, , , /05/07).
    # UNCONDITIONAL synth is safe: quiescence only evaluates in the all-blocked
    # state, where every dependency head is provably a non-candidate (an
    # agent-executable head would be a candidate and break all-blocked BEFORE
    # quiescence is reached), so no head-executability tracing is needed. Keyed on
    # raw blocked_by (not `unmet`) because this fn lacks done_ids AND because
    # explicit_status goals with blocked_by () never reach the dependency
    # branch — they rely on this L1659 synth. type=resource + now+120h rolling
    # expiry = self-healing (when the dep completes the goal leaves the blocked
    # set and the synth vanishes). NOTE: a not_my_lane goal (branch 7) with a
    # fully-COMPLETED blocked_by would get a "dependency:" external_id here rather
    # than "not-my-lane:" — cosmetic only (both type=resource, identical C2/C3/C4;
    # 0 current collisions verified ).
    blocked_by = goal.get("blocked_by") or []
    if isinstance(blocked_by, list) and blocked_by:
        _now_d = datetime.now()
        _key_d = "dependency:" + ",".join(str(x) for x in sorted(blocked_by))
        _h_d = hashlib.md5(_key_d.encode("utf-8", errors="replace")).hexdigest()[:12]
        return {
            "type": "resource",
            "external_id": f"dependency:{_h_d}",
            "state_hash": None,
            "created_at": _now_d.isoformat(timespec="seconds"),
            "expires_at": (_now_d + timedelta(hours=120)).isoformat(timespec="seconds"),
            "synthesized": True,
        }

    # Path (e): narrative defer_reason with no structured prefix (). A
    # goal deferred with free-text (e.g. "Requires solver-v0 baseline ...",
    # observed ) that did NOT match a STRUCTURED_DEFER_PREFIX and has no
    # blocked_by. By the time a defer_reason is persisted it has passed the
    # defer-time capability gate (probe-before-defer.md) -> it names a genuine
    # external gate -> synth so quiescence can account for it. Same self-healing
    # rolling expiry. (Reached only after paths a/b/c and path d all skip.)
    if defer:
        _now_e = datetime.now()
        _h_e = hashlib.md5(defer.encode("utf-8", errors="replace")).hexdigest()[:12]
        return {
            "type": "resource",
            "external_id": f"narrative-defer:{_h_e}",
            "state_hash": None,
            "created_at": _now_e.isoformat(timespec="seconds"),
            "expires_at": (_now_e + timedelta(hours=120)).isoformat(timespec="seconds"),
            "synthesized": True,
        }

    return None


def _synth_block_ref(kind, key):
    """Synthesize a type=resource blocker_ref for a blocked goal whose branch
    classified it (kind = infrastructure / precondition / explicit-status / ...) but
    which reached collect_blocked with blocker_ref=None -- the known_blocker (if any)
    carried no blocker_ref of its own AND _synth_blocker_ref_from_structured_defer
    returned None (goal has no defer_reason/deferred_until/rne/blocked_by/string-ref).
    quiescence-gate C2 (blocker_ref_required) requires a DICT; a None ref makes an
    all-blocked queue fail C2 on a capability-limited runner -> perpetual B7 backoff
    churn (300->1800 escalation + idle-tick spin) instead of clean quiescent sleep.
    Mirrors the not_my_lane synth (collect_blocked branch 7): type=resource (catch-all
    structural wait, NOT user-only -> normal short quiescent sleep re-checked each
    wake) + md5(kind:key)[:12] external_id (C4 hysteresis hash stays stable across
    iters when `key` is stable; `kind` is the external_id prefix -> per-branch hashes
    stay disjoint + aid debugging) + now+120h rolling expiry (self-healing -- when the
    block clears the goal leaves the blocked set and the synth vanishes). Coverage
    lineage: g-115-1792/1794 (structured-defer paths, via the sibling
    _synth_blocker_ref_from_structured_defer), g-115-1887 (infrastructure branches),
    g-115-1888 (precondition_unmet + explicit_status branches)."""
    _now = datetime.now()
    _h = hashlib.md5(
        (str(kind) + ":" + str(key)).encode("utf-8", errors="replace")
    ).hexdigest()[:12]
    return {
        "type": "resource",
        "external_id": f"{kind}:{_h}",
        "state_hash": None,
        "created_at": _now.isoformat(timespec="seconds"),
        "expires_at": (_now + timedelta(hours=120)).isoformat(timespec="seconds"),
        "synthesized": True,
    }

# Collective domain stores (world/)
WORLD_ASP_PATH = WORLD_DIR / "aspirations.jsonl"
PIPELINE_PATH = WORLD_DIR / "pipeline.jsonl"
PIPELINE_ARCHIVE_PATH = WORLD_DIR / "pipeline-archive.jsonl"

# Per-agent aspiration queue
AGENT_ASP_PATH = AGENT_DIR / "aspirations.jsonl" if AGENT_DIR else None

# Agent identity (used for claim checking AND participant-based goal routing)
AGENT_NAME = AGENT_DIR.name if AGENT_DIR else ""

# Meta-strategies (meta/)
SKILL_QUALITY_PATH = META_DIR / "skill-quality.yaml"

# Per-agent state
DEV_STAGE_PATH = AGENT_DIR / "developmental-stage.yaml" if AGENT_DIR else None
DEV_STAGE_CONFIG_PATH = CONFIG_DIR / "developmental-stage.yaml"

# Single source of truth for goal scoring weights: meta/goal-selection-strategy.yaml
# Seeded by init-meta.sh, editable by the agent during evolution Step 0.7.
# NOTE: exploration_noise is NOT here — its weight is dynamic (epsilon × noise_scale),
# computed at runtime in score_goal(). Do not add it to this dict.
META_GOAL_SELECTION = META_DIR / "goal-selection-strategy.yaml"

# : cap on applications_log entries to prevent unbounded growth.
# Proof-of-concept lane for meta-strategy "when was this applied" telemetry.
_APPLICATIONS_LOG_CAP = 200


def _record_strategy_application(strategy_path, summary):
    """Append a {ts, agent, sid, summary} entry to a meta-strategy's applications_log.

    g-304-09: Surfaces "when this meta-strategy was applied" — a flat strategy
    YAML alone tells you the parameters but not whether anything ever consumed
    them. Log is FIFO-capped at _APPLICATIONS_LOG_CAP to prevent unbounded
    growth. Fail-open: any error logs to stderr but does NOT crash the caller.
    """
    agent = os.environ.get("MIND_AGENT", "unknown")
    sid = os.environ.get("MIND_SID", "unknown")
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "agent": agent,
        "sid": (sid[:8] if isinstance(sid, str) and sid else "unknown"),
        "summary": summary,
    }
    def _mut(data):
        if not isinstance(data, dict):
            data = {}
        log = data.get("applications_log")
        if not isinstance(log, list):
            log = []
        log.append(entry)
        if len(log) > _APPLICATIONS_LOG_CAP:
            log = log[-_APPLICATIONS_LOG_CAP:]
        data["applications_log"] = log
        return data
    try:
        locked_modify_yaml(strategy_path, _mut)
    except Exception as e:
        print(f"[goal-selector] applications_log append failed: {e}",
              file=sys.stderr)


# Code-side contract manifest: every criterion key score_goal() computes into
# `raw`, EXCLUDING exploration_noise (dynamic weight = epsilon * noise_scale,
# never in the meta weights dict — see META_GOAL_SELECTION note above).
# : load_weights() filters meta keys against this set so an orphaned
# weight (meta names a criterion the code no longer computes — the rb-498-era
# promotion-clobber class that killed selection fleet-wide in prod via KeyError
# at the weighted sum) degrades to a loud stderr warning + opt-out instead.
# promote-preflight cross-checks seed weights against this same set (single
# contract, parsed via AST). When ADDING a criterion to score_goal, add its
# key here in the same change — test_goal_selector_weights_contract.py pins
# the two lists equal.
KNOWN_CRITERIA = frozenset({
    "priority", "deadline_urgency", "agent_executable", "variety_bonus",
    "streak_momentum", "novelty_bonus", "recurring_urgency",
    "recurring_saturation", "per_goal_saturation", "user_signal_boost",
    "class_balance_bonus", "role_affinity", "reward_history",
    "completion_pressure", "tail_bonus", "depth_bonus",
    "cross_aspiration_support", "evidence_backing", "deferred_readiness",
    "context_coherence", "skill_affinity", "directive_boost",
    "handoff_bonus", "co_invest_alignment", "critical_blocker_surface",
    "opportunity_boost",
})


def load_weights():
    """Load goal selection weights from meta/goal-selection-strategy.yaml.

    g-115-2525 hardening: weight keys with no matching criterion in
    KNOWN_CRITERIA are DROPPED with a loud stderr warning instead of
    reaching the weighted sum, where they raise KeyError and kill selection
    for every agent on the box (the orphaned-weight class: a promotion
    replaces selector code while the external meta/ file keeps a weight the
    new code never computes). No value fallback in the other direction —
    a criterion missing from meta simply opts out of scoring (rb-215).
    """
    with open(META_GOAL_SELECTION, encoding="utf-8") as f:
        meta = yaml.safe_load(f)
    raw = meta["weights"]
    unknown = sorted(set(raw) - KNOWN_CRITERIA)
    if unknown:
        print(
            f"[goal-selector] WARNING: meta weights name {len(unknown)} "
            f"criteria this selector does not compute: {', '.join(unknown)} "
            f"— ignoring them (meta/code contract drift, g-115-2525). "
            f"Fix meta/goal-selection-strategy.yaml or restore the criteria.",
            file=sys.stderr,
        )
    # Non-numeric values (YAML null from an add-key backpressure rollback, or
    # a stray string like "None" — mc-081 2026-07-18 crashed selection for
    # every agent on the box) opt the criterion OUT of scoring, same as an
    # absent key (rb-215). Loud skip, never a crash: a corrupt meta value must
    # degrade one criterion, not kill fleet-wide goal selection.
    non_numeric = sorted(
        k for k, v in raw.items()
        if k in KNOWN_CRITERIA and not isinstance(v, (int, float))
    )
    if non_numeric:
        print(
            f"[goal-selector] WARNING: meta weights carry non-numeric values "
            f"for: {', '.join(non_numeric)} — treating as opted-out (rb-215). "
            f"Fix meta/goal-selection-strategy.yaml.",
            file=sys.stderr,
        )
    return {
        k: max(0.0, min(3.0, float(v)))
        for k, v in raw.items()
        if k in KNOWN_CRITERIA and isinstance(v, (int, float))
    }


WEIGHTS = load_weights()
# handoff_bonus: raw value IS the bonus (not scaled). Configured in
# meta/goal-selection-strategy.yaml like every other weight — no setdefault
# fallback (, rb-215 single-source-of-truth).

# Override-key prefix for aspirations.yaml — see world/conventions/capability-routing.md
# "Currently wired readers" list. Keep in sync with _OVERRIDE_FILE_PREFIX in tree.py.
_OVERRIDE_FILE_PREFIX = "aspirations."


def load_recurring_config():
    """Load recurring goal scoring params from core/config/aspirations.yaml.

    Routes through _config_overlay.merged_config so meta/config-overrides.yaml
    entries keyed `aspirations.recurring.*` take effect. Wires 7 params:
    urgency_base, urgency_log_scale, saturation_window, saturation_max_penalty,
    debt_threshold, debt_bonus, streak_mult (g-115-123, rb-335 — streak_mult
    renamed from streak_reset_multiplier 2026-05-18 / g-115-929 to consolidate
    with the literal variable name used at the two readers in
    aspirations_write.py cmd_complete_by and streak-break-reflector.py).
    Plus urgency_max (g-115-1090) and the FW-1 substantive_demotion_* family
    (2026-05-25, 7-agent feedback distillation).
    """
    defaults = {
        "urgency_base": 1.5, "urgency_log_scale": 1.5,
        "urgency_max": 4.0,
        "saturation_window": 4, "saturation_max_penalty": 4.0,
        "debt_threshold": 0.80, "debt_bonus": 3.0,
        "streak_mult": 2.0,
        # FW-1 (2026-05-25): substantive-availability demotion (see aspirations.yaml).
        "substantive_demotion_enabled": True,
        "substantive_demotion_margin": 0.5,
        "substantive_demotion_floor": 5.0,
        "substantive_demotion_overdue_exempt_ratio": 5.0,
    }
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", Path(__file__).parent / "_config_overlay.py"
        )
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        asp_config = overlay.merged_config("aspirations.yaml")
        rc = asp_config.get("recurring", {})
        if isinstance(rc, dict):
            for k, default in defaults.items():
                v = rc.get(k)
                if v is not None:
                    defaults[k] = type(default)(v)
    except Exception:
        pass
    return defaults


RECURRING_CONFIG = load_recurring_config()


def load_cross_agent_surfacing_enabled():
    """Whether the selector surfaces sibling-queue goals routed via intended_agent.

    ENABLED 2026-07-15 (g-115-1848, part iii of g-115-1844). History: g-115-946
    built cross-agent SURFACING (collect_cross_agent_candidates); it was gated OFF
    2026-07-04 (g-115-1764 follow-up) because the EXECUTION path was unwired — the
    claim endpoint (aspirations_write.py claim()) resolves only world + the
    caller's own-agent queue, so a surfaced sibling-queue goal 404'd at claim ->
    hard selector livelock (g-115-1766; empirical: selector surfaced g-001-282
    intended_agent=alpha in bravo's queue, then aspirations-claim.sh -> 404).
    The execution path is now wired: collect_cross_agent_candidates stamps
    source='cross-agent:<owner>'; aspirations-select Phase 2.95 splits it into
    (effective_source='agent', cross_agent_owner); aspirations-execute Phase 4
    env-prefixes MIND_AGENT=<owner> on the claim so it resolves the OWNER's queue
    under the owner's identity — no 404. Prereq g-115-1847 (cross-agent-write.sh
    write-back helper) landed; wiring proven by test_goal_selector_cross_agent_pull.py
    + test_loop_state_save_cross_agent_owner.py + test_cross_agent_write.py.
    Reversible: set aspirations.yaml cross_agent_surfacing.enabled=false to gate off.
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", Path(__file__).parent / "_config_overlay.py")
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        asp_config = overlay.merged_config("aspirations.yaml")
        block = asp_config.get("cross_agent_surfacing", {})
        if isinstance(block, dict) and block.get("enabled") is not None:
            return bool(block.get("enabled"))
    except Exception:
        pass
    return False


CROSS_AGENT_SURFACING_ENABLED = load_cross_agent_surfacing_enabled()


def load_iaus_config():
    """Load the utility-scorer flag + params from core/config/aspirations.yaml.

    g-306-32 (BRD Gap 8): the utility scorer is a second, flag-gated code path in
    score_goal(); the additive scorer stays the default. Routes through
    _config_overlay.merged_config so meta/config-overrides.yaml entries keyed
    `aspirations.iaus_selector.*` take effect. Default use_iaus=False keeps the
    additive scorer the live default (R1 hot-path mitigation) — zero behavior
    change until the sibling A/B (g-306-33) shows parity-or-improvement.
    urgency_max mirrors RECURRING_CONFIG so recurring_urgency scales to [0,1]
    against the same ceiling the additive path caps it at.
    """
    defaults = {
        "use_iaus": False,
        "primary_floor": 0.1,
        "watermark": 0.0,
        "bonus_scale": 4.0,
        "urgency_max": float(RECURRING_CONFIG.get("urgency_max", 4.0)),
    }
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", Path(__file__).parent / "_config_overlay.py"
        )
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        asp_config = overlay.merged_config("aspirations.yaml")
        ic = asp_config.get("iaus_selector", {})
        if isinstance(ic, dict):
            for k, default in defaults.items():
                v = ic.get(k)
                if v is None:
                    continue
                if isinstance(default, bool):
                    # YAML parses `false` as bool already; the string branch
                    # defends config-overrides that store the flag as text.
                    defaults[k] = (
                        v.strip().lower() in ("true", "1", "yes", "on")
                        if isinstance(v, str) else bool(v)
                    )
                else:
                    defaults[k] = type(default)(v)
    except Exception:
        pass
    return defaults


IAUS_CONFIG = load_iaus_config()


def load_per_goal_saturation_config():
    """Load per-goal rapid-repeat suppression config from aspirations.yaml.

    Tranche B 2026-04-20 (rb-390): suppresses the SAME goal_id firing
    multiple times within the recent session-completions window.
    Distinct from RECURRING_CONFIG['saturation_*'] which is class-level.
    """
    defaults = {
        "window_size": 8,
        "consecutive_threshold": 1,
        "suppress_penalty": -5.0,
    }
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", Path(__file__).parent / "_config_overlay.py"
        )
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        asp_config = overlay.merged_config("aspirations.yaml")
        pgs = asp_config.get("per_goal_saturation", {})
        if isinstance(pgs, dict):
            for k, default in defaults.items():
                v = pgs.get(k)
                if v is not None:
                    defaults[k] = type(default)(v)
    except Exception:
        pass
    return defaults


PER_GOAL_SATURATION_CONFIG = load_per_goal_saturation_config()


def load_cell_return_config():
    """Load the Go-Explore cell-return boost params from core/config/aspirations.yaml.

    BRD Gap 17 child C (g-306-49). Flag-gated, boost-only selection adjustment that
    mirrors the g-306-44 retrieve.py PPR blend. DEFAULT OFF: when ``enabled`` is
    false, apply_cell_return_boost() is a byte-identical no-op (no-regression by
    construction). When on, seeds PPR from the top-N highest-value archived cells and
    adds a bounded graph-proximity bonus to each candidate (reusing the g-306-47 store
    + g-306-48 matcher). Same overlay/type-coerce shape as the sibling config loaders.
    """
    defaults = {
        "enabled": False,
        "seed_top_n": 5,
        "bonus_scale": 3.0,
        "bonus_max": 1.5,
        "enrich_signature": False,
    }
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", Path(__file__).parent / "_config_overlay.py"
        )
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        asp_config = overlay.merged_config("aspirations.yaml")
        cr = asp_config.get("cell_return", {})
        if isinstance(cr, dict):
            for k, default in defaults.items():
                v = cr.get(k)
                if v is not None:
                    defaults[k] = type(default)(v)
    except Exception:
        pass
    return defaults


CELL_RETURN_CONFIG = load_cell_return_config()


def load_user_signal_boost_config():
    """Load user-signal boost params from core/config/aspirations.yaml.

    Tranche C 2026-04-20 (rb-390): reweights goals based on fresh user-signal
    evidence collected by the signal-refresh hook
    (<agent>/session/user-signal-snapshot.yaml).

    Natural gate: snapshot existence. No separate `enabled` flag — if the
    snapshot is missing or empty, every boost path evaluates to zero.
    """
    defaults = {
        "reply_boost": 2.5,           # user_signal_kind == "reply" on this thread
        "directive_boost": 3.0,       # user_signal_kind == "directive"
        "silence_48h_boost": 1.5,     # pending-question silent ≥48h — surface the ask
        "override_penalty": -2.0,     # user override superseded this thread
        "thread_active_boost": 1.0,   # user_thread_id appears in any recent-activity list
    }
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", Path(__file__).parent / "_config_overlay.py"
        )
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        asp_config = overlay.merged_config("aspirations.yaml")
        # Block name matches the scorer criterion (user_signal_boost) —
        # keep these two in sync. DO NOT rename without matching score_goal().
        usb = asp_config.get("user_signal_boost", {})
        if isinstance(usb, dict):
            for k, default in defaults.items():
                v = usb.get(k)
                if v is not None:
                    defaults[k] = type(default)(v)
    except Exception:
        pass
    return defaults


USER_SIGNAL_BOOST_CONFIG = load_user_signal_boost_config()


def load_user_signal_snapshot():
    """Read <agent>/session/user-signal-snapshot.yaml if present.

    Fail-open: missing file, parse error, or wrong shape → empty dict.
    Called once per goal-selector invocation (one-shot script; the file is
    refreshed per iteration by the signal-refresh hook in aspirations-precheck).
    """
    try:
        snap_path = AGENT_DIR / "session" / "user-signal-snapshot.yaml"
        if not snap_path.exists():
            return {}
        with open(snap_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


USER_SIGNAL_SNAPSHOT = load_user_signal_snapshot()


def load_class_balance_config():
    """Load work-class balance params from core/config/aspirations.yaml.

    Tranche C 2026-04-20 (rb-390): pulls under-represented work_class values
    up when the last-N session completions distribution drifts from the
    configured targets. Addresses the work-mix skew failure mode (framework
    maintenance accumulating gravity unopposed).

    Natural gate: `targets` non-empty. No separate `enabled` flag — an empty
    targets map means the criterion contributes zero.
    """
    defaults = {
        "window_size": 20,
        "max_boost": 2.0,             # cap for under-represented class
        "max_penalty": -2.0,          # cap for over-represented class
        "targets": {},                # {work_class: fraction}; empty → disabled
    }
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", Path(__file__).parent / "_config_overlay.py"
        )
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        asp_config = overlay.merged_config("aspirations.yaml")
        cb = asp_config.get("class_balance", {})
        if isinstance(cb, dict):
            for k, default in defaults.items():
                v = cb.get(k)
                if v is not None:
                    if isinstance(default, dict):
                        defaults[k] = v if isinstance(v, dict) else {}
                    else:
                        defaults[k] = type(default)(v)
    except Exception:
        pass
    return defaults


CLASS_BALANCE_CONFIG = load_class_balance_config()


def load_agent_role_multipliers():
    """Load per-agent work_class multipliers from meta/goal-selection-strategy.yaml.

    Magic Wand 4 (bravo session-61, 2026-05-07): encodes agent role into goal
    scoring without per-iteration LLM metacognition. Format:
    {agent_name: {work_class: multiplier_float}}. Empty dict on missing key,
    parse error, or wrong shape — fail-open (criterion contributes zero,
    identical to today's scoring).

    Multiplier interpretation: positive = score boost, zero = no effect.
    Values are NOT clamped to [0, 3] like the weights table — they are
    per-class scaling factors meant to live in the [0.0, 2.0] range
    (e.g., 0.3 dampens by 70%, 1.5 boosts by 50%).
    """
    try:
        with open(META_GOAL_SELECTION, encoding="utf-8") as f:
            meta = yaml.safe_load(f) or {}
        v = meta.get("agent_role_multipliers", {})
        if not isinstance(v, dict):
            return {}
        return v
    except Exception:
        return {}


AGENT_ROLE_MULTIPLIERS = load_agent_role_multipliers()


def compute_role_affinity(agent_name, goal_class, multipliers):
    """Return the role_affinity raw value for (agent, goal_class).

    Pure function — no module state. Extracted to top level so tests can
    exercise the decision rule without setting up a full score_goal call.

    Returns 0.0 (zero contribution) when:
    - agent_name is empty/None
    - goal_class is None or "unclassified"
    - agent has no entry in multipliers
    - agent's entry is not a dict (corrupt config)
    - goal_class has no entry under that agent
    - the looked-up value cannot be coerced to float
    """
    # "unclassified" is excluded by design — same precedent as criterion
    # 7e (class_balance_bonus, line ~1473): goals with no work_class tag
    # don't participate in class-based scoring at all. Removing this
    # exclusion would attribute role_affinity to backfill-pending goals.
    if not agent_name or not goal_class or goal_class == "unclassified":
        return 0.0
    if not isinstance(multipliers, dict):
        return 0.0
    agent_mults = multipliers.get(agent_name, {})
    if not isinstance(agent_mults, dict):
        return 0.0
    try:
        return float(agent_mults.get(goal_class, 0.0))
    except (TypeError, ValueError):
        return 0.0


def load_handoff_config():
    """Load cross-agent handoff scoring params from core/config/aspirations.yaml.

    handoff_bonus: scoring bonus when goal.handoff_to == current MIND_AGENT.
    handoff_sender_penalty: penalty applied to goals.handoff_to == OTHER agent
      (prevents the sender from taking back their own routed work).
    handoff_aging:
      warn_hours: receiver-side escalating-bonus onset
      escalate_hours: aspirations-precheck board-post + notify age
      sender_decay_hours: sender-side silence-gated decay window
      partner_active_threshold_min: below this (minutes of silence),
        penalty stays full regardless of handoff age
    """
    defaults = {
        "handoff_bonus": 0.30,
        "handoff_sender_penalty": -2.5,
        "warn_hours": 48,
        "escalate_hours": 72,
        "sender_decay_hours": 4,
        "partner_active_threshold_min": 30,
    }
    try:
        with open(CONFIG_DIR / "aspirations.yaml", encoding="utf-8") as f:
            asp_config = yaml.safe_load(f)
        scoring = asp_config.get("scoring", {}) or {}
        if "handoff_bonus" in scoring:
            defaults["handoff_bonus"] = float(scoring["handoff_bonus"])
        if "handoff_sender_penalty" in scoring:
            defaults["handoff_sender_penalty"] = float(scoring["handoff_sender_penalty"])
        aging = asp_config.get("handoff_aging", {}) or {}
        for k in ("warn_hours", "escalate_hours", "sender_decay_hours"):
            if k in aging:
                defaults[k] = int(aging[k])
        if "partner_active_threshold_min" in aging:
            defaults["partner_active_threshold_min"] = int(aging["partner_active_threshold_min"])
    except Exception:
        pass
    return defaults


HANDOFF_CONFIG = load_handoff_config()


def load_critical_blocker_surface_config():
    """Load critical-blocker-surface scoring params from core/config/aspirations.yaml.

    g-305-07 (bravo US-07): surface long-blocked-but-EXECUTABLE bottleneck goals
    recorded in world/team-state.yaml critical_blockers[] (written by
    aspirations-consolidate Step 8.87, purged by team-state-sync-blockers.py once
    resolved) so a high-downstream-unlock goal doesn't get out-ranked
    indefinitely -- "break one, unlock five."

    enabled:        master switch (default False -- opt-in, like co_invest).
    min_downstream: ignore entries below this downstream_count (noise floor).
    downstream_cap: normalize the boost -- downstream_count >= cap scores 1.0.
    """
    defaults = {"enabled": False, "min_downstream": 3, "downstream_cap": 10}
    try:
        with open(CONFIG_DIR / "aspirations.yaml", encoding="utf-8") as f:
            asp_config = yaml.safe_load(f)
        block = asp_config.get("critical_blocker_surface", {}) or {}
        if "enabled" in block:
            defaults["enabled"] = bool(block["enabled"])
        if "min_downstream" in block:
            defaults["min_downstream"] = int(block["min_downstream"])
        if "downstream_cap" in block:
            defaults["downstream_cap"] = int(block["downstream_cap"])
    except Exception:
        pass
    return defaults


def compute_critical_blocker_surface(goal_id, critical_blockers, min_downstream, downstream_cap):
    """Pure boost computation for the critical_blocker_surface criterion ().

    Returns min(downstream_count, cap) / cap in [0, 1] when goal_id matches a
    team-state.critical_blockers[] entry whose downstream_count >= min_downstream;
    0.0 otherwise. Fully fail-open: bad/missing inputs -> 0.0, never raises.
    Extracted (mirrors compute_role_affinity) so the decision rule is unit-
    testable without subprocess or file I/O. critical_blockers entries are
    unique by goal_id (top-3 bottlenecks), so the first id match is terminal.
    """
    if not goal_id or not isinstance(critical_blockers, list):
        return 0.0
    try:
        cap = float(downstream_cap)
    except (TypeError, ValueError):
        return 0.0
    if cap <= 0:
        return 0.0
    for cb in critical_blockers:
        if not isinstance(cb, dict) or cb.get("goal_id") != goal_id:
            continue
        ds = cb.get("downstream_count")
        if isinstance(ds, (int, float)) and not isinstance(ds, bool) and ds >= min_downstream:
            return min(float(ds), cap) / cap
        return 0.0  # matched the goal but ds missing/below-floor/non-numeric
    return 0.0  # no matching entry


CRITICAL_BLOCKER_SURFACE_CONFIG = load_critical_blocker_surface_config()


# Sentinel for the team-state read cache. None = not yet read this run.
# Any dict (including {}) means the read has already happened — a repeat
# call returns the cached value. Declared BEFORE the function that uses it
# so the module's read order matches its runtime order.
_TEAM_STATE_CACHE = None

# Parse-or-restore retry budget for the team-state read (rb-2429). team-state.yaml
# is written by every agent via _atomic_write_with_fallback (core/scripts/_fileops.py),
# which under sustained multi-agent sync contention can fall back to an in-place
# truncate-rewrite (~25.7% of bursts, world/conventions/file-system-resilience.md).
# That rewrite completes in ms, so a few short retries clear a transient partial-YAML
# read before the reader fails open. Worst-case added latency = RETRIES * RETRY_SLEEP.
_TEAM_STATE_READ_RETRIES = 3
_TEAM_STATE_READ_RETRY_SLEEP = 0.05  # seconds between retries


def _load_team_state_cached():
    """Read world/team-state.yaml once and cache for the selector run.

    The sender-side handoff penalty decay is gated by partner liveness
    (agent_status.<partner>.last_active). We read the file exactly once per
    selector invocation — not once per scored goal — to avoid per-goal I/O.
    Missing file → {} (legitimate pre-coordination state).

    Parse-or-restore recovery (rb-2429): team-state.yaml is written by every
    agent via _atomic_write_with_fallback (core/scripts/_fileops.py), which
    under sustained multi-agent sync contention exhausts its os.replace retry
    budget and falls back to an in-place truncate-rewrite (~25.7% of bursts per
    world/conventions/file-system-resilience.md). An unlocked reader caught
    mid-fallback sees partial YAML. team-state is ADVISORY scoring input
    (handoff liveness, critical_blockers) — never correctness-critical for
    selection — so a partial/unreadable read must NOT crash the whole selector.
    Retry a few times (the in-place write completes in ms), then fail open to {}.
    The empty dict is cached for the rest of the run, consistent with the
    read-once contract, so a persistently-broken file does not re-spin per goal.
    """
    global _TEAM_STATE_CACHE
    if _TEAM_STATE_CACHE is not None:
        return _TEAM_STATE_CACHE
    path = WORLD_DIR / "team-state.yaml"
    if not path.exists():
        _TEAM_STATE_CACHE = _compose_rows({})
        return _TEAM_STATE_CACHE
    last_err = None
    for attempt in range(_TEAM_STATE_READ_RETRIES):
        try:
            with open(path, "r", encoding="utf-8") as f:
                _TEAM_STATE_CACHE = _compose_rows(yaml.safe_load(f) or {})
            return _TEAM_STATE_CACHE
        except (OSError, yaml.YAMLError) as e:
            last_err = e
            if attempt < _TEAM_STATE_READ_RETRIES - 1:
                time.sleep(_TEAM_STATE_READ_RETRY_SLEEP)
    # All retries saw a partial/unreadable file — fail open to {} (advisory
    # input). Crashing here would block goal selection every iteration until
    # the contending writer finishes (rb-2429). Warn on stderr for visibility;
    # stdout (the selector's JSON) is untouched.
    print(
        f"[goal-selector] WARN: team-state.yaml unreadable after "
        f"{_TEAM_STATE_READ_RETRIES} attempts "
        f"({type(last_err).__name__}: {last_err}); failing open to {{}} — "
        f"handoff-liveness + critical-blocker scoring disengaged this run (rb-2429)",
        file=sys.stderr,
    )
    _TEAM_STATE_CACHE = _compose_rows({})
    return _TEAM_STATE_CACHE


def _compose_rows(core_doc):
    """Overlay per-agent row files onto the core team-state doc (
    sharding — agent_status rows live in world/team-state/agents/*.yaml;
    rows win newest-wins over core residuals). Fail-open: composition
    errors return the core doc unchanged (advisory input, rb-2429 spirit)."""
    try:
        from _team_state import compose_state
        return compose_state(core_doc, WORLD_DIR)
    except Exception as e:  # noqa: BLE001 — advisory input never crashes selection
        print(f"[goal-selector] WARN: team-state row compose failed "
              f"({type(e).__name__}: {e}); using core file only", file=sys.stderr)
        return core_doc


PRIORITY_MAP = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


def _ensure_list(val, default=None):
    """Normalize a JSONL field that should be a list. Strings become [string].

    Use this for every list-typed JSONL field (blocked_by, participants, tags).
    Raw .get() on these fields will silently iterate characters if the data is
    a string, producing wrong results without any error.
    """
    if val is None:
        return default if default is not None else []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        return [val]
    return default if default is not None else []


def _is_agent_eligible(participants, agent_name):
    """Check if current agent can execute a goal based on participants.

    - ["agent"]: any agent (backward compatible wildcard)
    - ["user"]: not eligible
    - ["alpha"]: only alpha
    - ["alpha", "user"]: alpha + user collaborative
    - Empty/default: treated as ["agent"]
    """
    if not participants:
        return True
    if participants == ["user"]:
        return False
    if "agent" in participants:
        return True
    if agent_name and agent_name in participants:
        return True
    # Only specific agent names remain, and we're not one of them
    non_user = [p for p in participants if p != "user"]
    return not non_user  # True only if nothing but "user" entries


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def read_jsonl(path):
    """Read a JSONL file and return a list of dicts."""
    p = Path(path)
    if not p.exists():
        return []
    items = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s:
                items.append(json.loads(s))
    return items


def read_yaml_file(path):
    """Read a YAML file via PyYAML."""
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _log_transient_allblocked_recovery(first_world, retry_world, retry_count):
    """Record a transient all_blocked recovery for root-cause evidence ().

    Emitted by cmd_select when the FIRST collection pass returns zero candidates
    but a fresh re-read + re-collect finds work. The WORLD aspirations file lives
    on a synced network drive (OneDrive); the leading hypothesis is a transient
    stale/partial snapshot presented during sync. Recording first-vs-retry
    aspiration/goal counts lets a later analysis distinguish "file content changed
    between the two reads" (world_content_changed_between_reads=True -> stale-read
    confirmed) from "identical content, different collection result" (=False ->
    a deeper non-determinism worth a separate investigation). Fail-open at every
    layer: telemetry must never break selection.
    """
    try:
        def _counts(asps):
            ng = sum(len(a.get("goals", []) or [])
                     for a in (asps or []) if a.get("status") == "active")
            return len(asps or []), ng
        fa, fg = _counts(first_world)
        ra, rg = _counts(retry_world)
        content_changed = (fa != ra) or (fg != rg)
        sys.stderr.write(
            "[goal-selector] WARN transient all_blocked recovered: first pass 0 "
            "candidates, retry found %d. world_content_changed_between_reads=%s "
            "(first %d asps/%d goals, retry %d asps/%d goals). See g-115-1295.\n"
            % (retry_count, content_changed, fa, fg, ra, rg))
        try:
            rec = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "event": "transient_all_blocked_recovered",
                "retry_candidates": retry_count,
                "first_world_aspirations": fa, "retry_world_aspirations": ra,
                "first_world_goals": fg, "retry_world_goals": rg,
                "world_content_changed_between_reads": content_changed,
            }
            path = Path(WORLD_DIR) / "goal-selector-anomalies.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cross-session class completions ( /  drift fix)
# ---------------------------------------------------------------------------

def load_recent_class_completions(window_size=20):
    """Cross-session sampling window for goal-selector criteria.

    Replaces in-session-only `wm.goals_completed_this_session` (which resets
    every /stop and was structurally blind to cross-session drift) with a
    rolling window drawn from <agent>/journal.jsonl recent completions, cross-
    referenced against world+agent aspirations.jsonl for `work_class` lookup.

    Returns a list shaped like `goals_completed_this_session` entries:
        [{goal_id, aspiration_id, recurring, work_class}, ...]
    in chronological order (oldest first), so consumers can keep using
    `recent[-window:]` slicing semantics unchanged.

    Falls back to the in-session list when:
    - No AGENT_DIR (fresh install)
    - journal.jsonl missing
    - journal yielded zero entries with mappable work_class (fresh agent)

    See alpha/reports/framework-vs-product-drift-2026-05-09.md for motivation
    (g-115-508 finding: 53% framework dominance over 60 sessions vs 25%
    target was invisible to class_balance_bonus because the window reset
    every session).
    """
    if AGENT_DIR is None:
        return []

    journal_path = AGENT_DIR / "journal.jsonl"

    def _in_session_fallback():
        try:
            wm = read_wm()
            sc = wm.get("goals_completed_this_session", [])
            return sc if isinstance(sc, list) else []
        except Exception:
            return []

    if not journal_path.exists():
        return _in_session_fallback()

    # Build goal_id → {aspiration_id, recurring, work_class} index from
    # world + agent aspirations. Empty work_class is preserved so callers
    # can distinguish "no entry" from "no work_class tag" (the existing
    # class_balance check filters missing work_class out of the denominator).
    index = {}
    try:
        for src_path in (WORLD_ASP_PATH, AGENT_ASP_PATH):
            if not src_path:
                continue
            for asp in read_jsonl(src_path):
                asp_id = asp.get("id")
                for g in asp.get("goals", []):
                    gid = g.get("id")
                    if not gid:
                        continue
                    index[gid] = {
                        "goal_id": gid,
                        "aspiration_id": asp_id,
                        "recurring": bool(g.get("recurring", False)),
                        "work_class": g.get("work_class") or "",
                    }
    except Exception:
        return _in_session_fallback()

    # Tail-read journal: collect goals_completed entries from latest entries
    # backwards until we have window_size with non-empty work_class.
    try:
        with open(journal_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return _in_session_fallback()

    completions = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        gids = entry.get("goals_completed") or []
        if not isinstance(gids, list):
            continue
        # Process in reverse (last-completed-first within this entry)
        for gid in reversed(gids):
            if not isinstance(gid, str):
                continue
            info = index.get(gid)
            if not info or not info.get("work_class"):
                # Skip goals without a current work_class lookup —
                # archived/orphaned IDs would dilute the denominator
                continue
            completions.append(info)
            if len(completions) >= window_size:
                break
        if len(completions) >= window_size:
            break

    if not completions:
        return _in_session_fallback()

    # Reverse to chronological (oldest first) so [-N:] slicing semantics match
    return list(reversed(completions))


# ---------------------------------------------------------------------------
# Exploration params
# ---------------------------------------------------------------------------

def load_exploration_params():
    """Load epsilon and noise_scale from developmental stage + config.

    Returns (epsilon, noise_scale) tuple.
    Epsilon from <agent>/developmental-stage.yaml -> exploration.epsilon
    noise_scale from core/config/developmental-stage.yaml -> exploration.noise_scale
    """
    # Read epsilon from mutable state
    dev_state = read_yaml_file(DEV_STAGE_PATH)
    epsilon = 0.85  # default for uninitialized (exploring stage)
    exploration = dev_state.get("exploration", {})
    if isinstance(exploration, dict):
        epsilon = exploration.get("epsilon", 0.85)

    # Read noise_scale from framework config
    dev_config = read_yaml_file(DEV_STAGE_CONFIG_PATH)
    noise_scale = 3.0  # default
    config_exploration = dev_config.get("exploration", {})
    if isinstance(config_exploration, dict):
        noise_scale = config_exploration.get("noise_scale", 3.0)

    return (float(epsilon), float(noise_scale))


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def days_until(date_str):
    """Days until a future date. Negative if past."""
    if not date_str:
        return None
    try:
        return (date.fromisoformat(str(date_str)) - date.today()).days
    except (ValueError, TypeError):
        return None


def days_since(date_str):
    """Days since a past date. Negative if future."""
    if not date_str:
        return None
    try:
        return (date.today() - date.fromisoformat(str(date_str))).days
    except (ValueError, TypeError):
        return None


def hours_since(timestamp_str):
    """Hours since a past timestamp. Handles both YYYY-MM-DD and YYYY-MM-DDTHH:MM:SS.

    For date-only strings (legacy), assumes start of day (00:00:00).
    Returns float hours, or None if unparseable/corrupt.
    Timestamps must be local system time — see core/config/conventions/goal-schemas.md.
    """
    if not timestamp_str:
        return None
    s = str(timestamp_str)
    try:
        if "T" in s:
            past = datetime.fromisoformat(s)
        else:
            past = datetime.combine(date.fromisoformat(s), datetime.min.time())
        delta = datetime.now() - past
        hours = delta.total_seconds() / 3600.0
        # Negative = corrupt timestamp. Return None so callers treat goal as due (fail open).
        if hours < 0:
            return None
        return hours
    except (ValueError, TypeError):
        return None


def get_interval_hours(goal):
    """Get the recurring interval in hours for a goal.

    Reads interval_hours first, falls back to remind_days * 24, defaults to 24.
    """
    if "interval_hours" in goal:
        return goal["interval_hours"]
    if "remind_days" in goal:
        return goal["remind_days"] * 24
    return 24


# ---------------------------------------------------------------------------
# FILTER + COLLECT
# ---------------------------------------------------------------------------

def _is_owner_scoped_goal(goal):
    """True when a goal operates ONLY on the bound agent's own dir tree and so
    CANNOT be executed by a cross-agent reallocatee (rb-4792, g-115-2945).

    /drain-temp is the canonical case: its SKILL.md Phase 1 sets
    TEMP_DIR=$AGENT_DIR/temp and operates on the bound agent ONLY. Surfacing
    such a goal to another agent via idle-reallocation puts it in the
    reallocatee's candidate list where it is UNEXECUTABLE -- running it drains
    the WRONG agent's temp, and running it as the owner collides with the
    owner's live session. Owner-scoped goals must therefore never be
    cross-agent reallocated, even when their owner looks idle.

    Detected three independent ways so a rename of any one signal still catches
    it: the skill id, the origin_signal, or the title. The `maintain:temp-drain`
    Maintain goal (skill=None) is caught by origin_signal/title; the
    orchestrator-filed HIGH `/drain-temp` action goal is caught by skill.
    """
    if (goal.get("skill") or "") == "/drain-temp":
        return True
    origin = (goal.get("origin_signal") or "").lower()
    if "temp" in origin and "drain" in origin:
        return True
    # Positive drain-action signature () — the SAME SSOT matcher
    # precheck-eval.py dedup uses, so a title-template edit cannot desync the
    # two. The prior '"drain" in title and "temp" in title' fallback false-
    # positived on any goal that merely MENTIONS temp-drain and stranded it
    # with a dormant owner (rb-3452 "assert the mechanism, not the case").
    if is_drain_action_title(goal.get("title")):
        return True
    return False


def _get_idle_agents(reallocation_hours):
    """Set of agent names whose team-state last_active is older than
    reallocation_hours ( gap #4 — intended_agent idle-reallocation).

    An intended_agent-routed goal is normally hidden from every other agent
    (collect_candidates intended_agent filter). When the routed-to agent has
    gone idle for longer than reallocation_hours, that routing STRANDS the
    goal — invisible to the running agent AND absent from collect_blocked (a
    select-time drop, not a block), so it vanishes from selection entirely
    (verified 2026-07-08: 15 framework goals routed to a 5.75-day-idle agent
    vanished from both selectable and blocked outputs -> running agent falsely
    concluded all-blocked). This set lets the intended_agent filter fall
    through for an idle target, mirroring the reallocatable+reallocation_hours
    mechanism but keyed on intended-agent idleness rather than the explicit
    reallocatable flag.

    Conservative + fail-open (rb-1028 posture): returns an empty set when
    reallocation is disabled (reallocation_hours is None) or team-state is
    unavailable, and an agent with a missing/unparseable last_active is NOT
    treated as idle — the goal stays routed (status quo) rather than being
    surfaced on absent evidence.

    g-115-2315: a stale last_active alone is NOT sufficient evidence of
    idleness — it is the LOCAL MIRROR of the peer's pushed snapshot, and a
    lagged pull path freezes it for every peer while they actively push
    (the g-115-2149 read-side lie; observed 2026-07-16: foxtrot last_active
    27.5h stale on this box while its shard hit the authoritative store 4
    minutes earlier, so its alert goal leaked to echo). Before declaring an
    agent idle, cross-check the inherently-fresh signal (the agent's
    team-state shard write time read from the AUTHORITATIVE store) via
    liveness_check: idle ONLY on a "dormant" verdict (both signals stale);
    "alive" and "unknown" (fresh signal unreadable) keep the agent NOT idle,
    honoring the never-on-absent-evidence promise above. Probe results are
    memoized per process — one authoritative-store HEAD per stale agent per
    selector run, not per goal.
    """
    if reallocation_hours is None:
        return set()
    ts = _load_team_state_cached() or {}
    agent_status = ts.get("agent_status") or {}
    idle = set()
    for name, row in agent_status.items():
        if not isinstance(row, dict):
            continue
        age = hours_since(row.get("last_active"))
        if age is not None and age > reallocation_hours:
            if _liveness_confirms_dormant(name, row.get("last_active"),
                                          reallocation_hours):
                idle.add(name)
    return idle


_LIVENESS_DORMANT_CACHE = {}


def _liveness_confirms_dormant(name, last_active_iso, threshold_hours):
    """True only when liveness_check upholds the dormant conclusion for
    ``name`` (g-115-2315 — see _get_idle_agents docstring).

    Dispatches to liveness_check.fetch_fresh_signal (authoritative-store
    shard write time: S3 LastModified on own-cloud, shard mtime on local)
    and the pure decide_liveness verdict. Memoized per process so repeated
    collect_candidates calls (world + agent queues) probe each stale agent
    once. Fail-safe: any import/probe error returns False (NOT idle) — the
    same direction as decide_liveness's "unknown" verdict, degrading toward
    goals-stay-routed (slow but never wrongly leaked) rather than reviving
    the false-idle defect this check exists to fix.
    """
    if name in _LIVENESS_DORMANT_CACHE:
        return _LIVENESS_DORMANT_CACHE[name]
    try:
        import liveness_check as _lc
        fresh_iso = _lc.fetch_fresh_signal(
            name, str(WORLD_DIR), os.environ.get("STORAGE_BACKEND", "local"))
        verdict = _lc.decide_liveness(
            last_active_iso, fresh_iso, threshold_hours=threshold_hours,
            now=datetime.now())["verdict"]
        dormant = (verdict == "dormant")
    except Exception:  # noqa: BLE001 — fail-safe toward NOT idle
        dormant = False
    _LIVENESS_DORMANT_CACHE[name] = dormant
    return dormant


def collect_candidates(aspirations, known_blockers=None, source="world",
                       global_done_ids=None, claim_timeout_hours=None,
                       reallocation_hours=None,
                       abstention_timeout_hours=None,
                       defer_reason_timeout_hours=None,
                       dependency_timeout_hours=None,
                       global_live_ids=None):
    """Return unblocked pending goals from active aspirations (Phase 2 FILTER + COLLECT).

    Args:
        aspirations: list of aspiration dicts
        known_blockers: list of blocker dicts from working memory
        source: "world" or "agent" — tags each candidate with its origin queue
        global_done_ids: set of completed/decomposed goal IDs across ALL aspirations
            (both world and agent). Enables cross-aspiration blocked_by enforcement.
            If None, falls back to per-aspiration done_ids (legacy behavior).
        global_live_ids: set of goal IDs whose status is non-terminal (still able to
            complete) across ALL aspirations. Used to re-validate dependency liveness
            before honoring the dependency_timeout fail-open (g-115-1344) — a still-live
            unmet dep keeps the goal blocked regardless of blocked_since age.
            If None, falls back to per-aspiration live_ids (legacy behavior).
        claim_timeout_hours: hours after which a stale claim is treated as expired.
            If None, claims persist indefinitely (legacy behavior).
        reallocation_hours: hours after which an unclaimed goal with reallocatable=true
            targeted at another agent becomes eligible for any agent.
            If None, reallocation is disabled (legacy behavior).
    """
    today = date.today()
    results = []
    # Per-runner capability set ( Slice 2) — derived once, cached
    # module-wide so cmd_select AND quiescence-gate's candidate check skip
    # locally-unexecutable goals consistently (no call-site threading).
    runner_caps = _get_runner_capabilities()
    # Agents idle beyond reallocation_hours ( gap #4) — used below to
    # reallocate intended_agent-routed goals stranded on an idle target. Derived
    # once per run (mirrors runner_caps); empty set when reallocation is disabled
    # (reallocation_hours is None) or team-state is unavailable.
    idle_agents = _get_idle_agents(reallocation_hours)

    # Build set of skills blocked by infrastructure blockers
    blocked_skills = set()
    blocked_categories = set()
    if known_blockers:
        for b in known_blockers:
            if b.get("resolution") is None:
                for skill in b.get("affected_skills", []):
                    blocked_skills.add(skill)
                for cat in b.get("affected_categories", []):
                    blocked_categories.add(cat)

    for asp in aspirations:
        if asp.get("status") != "active":
            continue

        # Cooldown check
        cooldown = asp.get("cooldown_days", 0)
        if cooldown > 0:
            lw = days_since(asp.get("last_worked"))
            if lw is not None and lw < cooldown:
                continue

        # Use global done_ids if provided (cross-aspiration dependency enforcement),
        # otherwise fall back to per-aspiration scope (legacy behavior).
        if global_done_ids is not None:
            done_ids = global_done_ids
        else:
            done_ids = {g["id"] for g in asp.get("goals", [])
                        if g.get("status") in ("completed", "decomposed")}

        # live_ids: goal IDs that could still complete (non-terminal status).
        # Re-validates dependency liveness before honoring the dependency_timeout
        # fail-open (). Mirrors done_ids: global set when supplied,
        # else per-aspiration scope.
        if global_live_ids is not None:
            live_ids = global_live_ids
        else:
            live_ids = {g["id"] for g in asp.get("goals", [])
                        if g.get("status") not in TERMINAL_GOAL_STATUSES}

        # verification.preconditions come in two forms:
        #   - strings → LLM-evaluated in aspirations-select Phase 2.2
        #   - dicts with "type" → structured predicates, filtered below via
        #     predicate.evaluate_all (fail-fast; selector_skip=True is honored)
        for goal in asp.get("goals", []):
            if goal.get("status") != "pending":
                continue

            # Self-abstention check: skip goals this agent previously abstained from.
            # The other agent sees them normally. (arXiv 2603.28990: voluntary abstention)
            # Expiry: abstentions older than abstention_timeout_hours are ignored (fail-open).
            # If no abstained_at timestamp exists (legacy), abstention expires immediately.
            if goal.get("abstained_by") == AGENT_NAME:
                if abstention_timeout_hours is not None:
                    abstain_age = hours_since(goal.get("abstained_at"))
                    if abstain_age is not None and abstain_age <= abstention_timeout_hours:
                        continue  # Valid abstention — skip
                    # else: expired or no timestamp — fall through (fail-open)
                else:
                    continue  # No expiry configured — legacy behavior

            # Claim check (world goals only): skip goals claimed by another agent.
            # Expiry makes stale claims (older than claim_timeout_hours) fall through
            # so other agents can pick up abandoned work. The actual re-claim is still
            # atomic via aspirations-claim.sh — this only controls VISIBILITY.
            # For recurring goals, claim timeout is capped at 2x interval_hours so that
            # short-interval goals (e.g. 1h email check) don't stay claimed for 4h.
            if source == "world":
                claimed = goal.get("claimed_by")
                if claimed and claimed != AGENT_NAME:
                    if claim_timeout_hours is not None:
                        effective_timeout = claim_timeout_hours
                        if goal.get("recurring"):
                            interval = get_interval_hours(goal)
                            effective_timeout = min(claim_timeout_hours, 2 * interval)
                        claim_age = hours_since(goal.get("claimed_at"))
                        if claim_age is not None and claim_age <= effective_timeout:
                            continue  # Valid claim — skip
                        # else: claim expired or no claimed_at — fall through to include
                    else:
                        continue  # No expiry configured — legacy behavior

            # blocked_by check (dependency timeout — fail-CLOSED hardening, ).
            # Keep the goal blocked UNLESS the block is genuinely stale: blocked_since
            # is set AND aged past the timeout AND every unmet dep is terminal-
            # unresolvable (abandoned status or orphan ref — none still live).
            #   (a) null/unparseable blocked_since => recently-blocked, not expired.
            #   (b) a still-LIVE unmet dep => could still complete; keep blocked
            #       regardless of age (re-validate completion before honoring timeout).
            unmet_deps = [b for b in _ensure_list(goal.get("blocked_by"))
                          if b not in done_ids]
            if unmet_deps:
                if dependency_timeout_hours is not None:
                    dep_age = hours_since(goal.get("blocked_since"))
                    live_unmet = [b for b in unmet_deps if b in live_ids]
                    if dep_age is None or dep_age <= dependency_timeout_hours or live_unmet:
                        continue  # Recent/valid block, or a live dep remains — skip
                    # else: stale (aged) AND all unmet deps dead/orphan — fall through (fail-open)
                else:
                    continue  # No expiry configured — legacy behavior

            # Infrastructure blocker check (skill-based, primary)
            goal_skill = goal.get("skill", "")
            if goal_skill and blocked_skills and goal_skill in blocked_skills:
                continue
            # Category fallback: when skill is null/empty, check goal.category
            if not goal_skill and blocked_categories:
                goal_cat = goal.get("category", "")
                if goal_cat and goal_cat in blocked_categories:
                    continue

            # Recurring time gate (hour-level precision)
            if goal.get("recurring"):
                # Signal-gated cadence ( / design ): when a
                # recurring goal carries a `cadence_signal`, fire IFF that signal
                # is PRESENT. Pure signal-gate (no `cadence_fallback_days`) skips
                # entirely while the signal is absent; HYBRID (with fallback_days)
                # falls back to a day-floor. Goals WITHOUT `cadence_signal` keep
                # the legacy hour-interval gate below (backwards-compat by
                # construction). Fail-open: an unknown/erroring signal returns
                # True -> "fire" (cadence_signals.evaluate_cadence_signal).
                cadence_signal = goal.get("cadence_signal")
                if cadence_signal:
                    if not evaluate_cadence_signal(cadence_signal, goal):
                        fallback_days = goal.get("cadence_fallback_days")
                        if fallback_days is None:
                            continue  # pure signal-gate, signal absent -> skip
                        la = hours_since(goal.get("lastAchievedAt"))
                        if la is not None and la < float(fallback_days) * 24.0:
                            continue  # hybrid: within fallback window -> skip
                        # else: hybrid fallback floor elapsed -> fire
                    # signal present -> fire (bypass the hour gate entirely)
                else:
                    interval = get_interval_hours(goal)
                    la = hours_since(goal.get("lastAchievedAt"))
                    if la is not None and la < interval:
                        continue

            # Hypothesis time gate (datetime-form safe — , _parse_rne_dt)
            rne_dt = _parse_rne_dt(goal.get("resolves_no_earlier_than"))
            if rne_dt is not None and datetime.now() < rne_dt:
                continue

            # Defer reason: textual deferral blocks the goal.
            # Expiry: defer_reason without deferred_until expires after defer_reason_timeout_hours.
            # Goals WITH deferred_until are governed by the time gate below, not this expiry —
            # the structured field is authoritative when present, and falls through here
            # to the time gate which checks the date and clears past-dated entries naturally.
            # If no defer_reason_set_at timestamp (legacy), deferral expires immediately (fail-open).
            # (: prior `else: continue` unconditionally blocked goals with both fields,
            # leaving 5 goals with deferred_until in the past permanently blocked because they
            # never reached the time gate at L678-685.)
            if goal.get("defer_reason"):
                # Only a FUTURE deferred_until defers to the time gate below. A
                # PAST (or corrupt) one must NOT bypass the defer_reason
                # evaluation — the time gate clears past dates unconditionally,
                # so bypassing here RELEASED goals whose defer_reason was
                # simultaneously fresh and structured. Measured :
                #  (a DESTRUCTIVE own-cloud S3 prune whose defer_reason
                # "precondition_unmet:fleet_quiesced_window" had been re-stamped
                # 2h earlier and was still true) was released by a deferred_until
                # 3.6h in the past to scorer rank #1 of 59 — and deferred_readiness
                # (L3005) then ADDED +0.9, so the stale field did not merely fail
                # to block, it BOOSTED the goal to the top of the shared queue.
                # 's fix (a past date must not block forever) is preserved:
                # this arm has its own defer_reason_timeout_hours fail-open, so a
                # genuinely stale defer still ages out — it is just no longer
                # INSTANTLY cleared by a past date. Also restores parity with
                # _synthesize_blocker_ref (L239-246), which already falls through
                # a past deferred_until to its path (a).
                # SYMMETRY: collect_blocked has the identical guard. Change both.
                if not _has_future_deferred_until(goal):
                    # human_blocked: never expires (). A genuinely
                    # human-gated block can never auto-clear, so the 120h
                    # fall-through below would wrongly re-surface it as a live
                    # candidate every iteration. Always skip it (excluded from
                    # candidates); collect_blocked routes it to blocked[] with a
                    # synthesized blocker_ref so quiescence can fire.
                    if (goal.get("defer_reason") or "").lower().startswith(_HUMAN_BLOCKED_PREFIX):
                        continue
                    # No structural gate — apply expiry logic
                    if defer_reason_timeout_hours is not None:
                        defer_age = hours_since(goal.get("defer_reason_set_at"))
                        if defer_age is not None and defer_age <= defer_reason_timeout_hours:
                            continue  # Valid deferral — skip
                        # else: expired or no timestamp — fall through (fail-open)
                    else:
                        continue  # No expiry configured — defer indefinitely
                # else: has deferred_until — let the time gate below handle it

            # Deferred time gate
            deferred = goal.get("deferred_until")
            if deferred:
                try:
                    dt = datetime.fromisoformat(str(deferred))
                    if datetime.now() < dt:
                        continue  # Not yet time
                except (ValueError, TypeError):
                    pass  # Corrupt value — fail open

            # Structured preconditions (cheap filter; strings stay on the LLM path).
            # SYMMETRY: must be the logical complement of the struct_pc check in
            # collect_blocked. If you change one, change the other.
            # No try/except on the import — predicate is a required sibling module;
            # a broken import is a real bug we want to surface, not silence.
            # NOTE: `goal.get("verification") or {}` (not `goal.get("verification", {})`)
            # because some goals carry an explicit `verification: null` from filings
            # that didn't structure verification — dict.get returns the stored None
            # in that case, not the default. AttributeError on the chained `.get`
            # was the crash observed during selector runs.
            struct_pcs = [p for p in (goal.get("verification") or {}).get("preconditions") or []
                          if isinstance(p, dict) and "type" in p]
            # Magic Wand #4 (alpha session-60, 2026-05-07): fire_when sugar.
            # Recurring goals can carry a single structured fire_when gate
            # that's evaluated alongside preconditions. Same predicate
            # registry — any predicate type works (command_succeeds for
            # infrastructure probes, file_check for data-pending gates,
            # metric_threshold for traffic-driven recurring fires, etc).
            # When fire_when fails, recurring-precondition-sweep.py advances
            # lastAchievedAt so overdue_ratio doesn't run away while the
            # upstream signal is absent.
            fw = goal.get("fire_when")
            if isinstance(fw, dict) and "type" in fw:
                struct_pcs.append(fw)
            if struct_pcs:
                from predicate import evaluate_all as _eval_preconditions
                # NOTE: do NOT name this `results` — outer `results = []` (L644)
                # is the candidate accumulator and L820 appends to it. Function
                # scope means a `results = ...` assignment here would clobber
                # the accumulator and L822 would return the wrong object type.
                # Filed as fresh-eyes-code F-001 (alpha-fec, msg-715), fixed
                # in  — the regression came in with commit 0f42275.
                pc_results = _eval_preconditions(struct_pcs, mode="fail_fast",
                                                 include_skippable=False)
                if any(not r.passed for r in pc_results):
                    continue

            # Agent eligibility check (filters user-only AND other-agent goals)
            participants = _ensure_list(goal.get("participants"), ["agent"])
            if not _is_agent_eligible(participants, AGENT_NAME):
                # Straggler-aware reallocation: if the goal is marked reallocatable
                # and hasn't been claimed by the targeted agent within reallocation_hours,
                # any agent can pick it up. (Distributed Systems Finding 5: dynamic realloc.)
                if (reallocation_hours is not None
                        and goal.get("reallocatable")
                        and not goal.get("claimed_by")):
                    # Check if enough time has passed since goal creation/last status change
                    created = goal.get("created") or asp.get("created")
                    age = hours_since(created)
                    if age is not None and age >= reallocation_hours:
                        pass  # Fall through — goal is reallocatable and overdue
                    else:
                        continue  # Not yet eligible for reallocation
                else:
                    continue  # Not eligible and not reallocatable

            # Intended-agent routing filter (): drop goals routed to a
            # different agent. Routing is a HINT, not a hard refusal — the
            # `intended_agent` field can be null/unset (no preference),
            # "either" (no preference), this agent's own name (route to me),
            # or another agent's name (route away from me). Only the last
            # case drops; the first three pass through. Mirror of the
            # partner-claim filter pattern in aspirations-select.
            #
            # Pairs with the creation-side wire-up in aspirations.py
            # cmd_add_goal () that stamps intended_agent via
            # capability-route-gate.py. The gate suggests "either" on
            # uncertainty, so any genuinely cross-lane goal stays visible
            # to both agents — only goals confidently routed to a specific
            # peer get filtered out here.
            intended_agent = goal.get("intended_agent")
            if (intended_agent
                    and intended_agent != AGENT_NAME
                    and intended_agent != "either"):
                # Idle-agent reallocation ( gap #4): when the routed-to
                # agent has been idle beyond reallocation_hours AND the goal is
                # unclaimed, fall through so a running capable agent can pick up
                # otherwise-stranded work. Mirrors the reallocatable path above
                # but keyed on intended-agent idleness rather than the explicit
                # reallocatable flag. Conservative: fires only when team-state
                # POSITIVELY shows the target idle (missing last_active => not
                # idle => keep routing, status quo). Unclaimed-only so a goal an
                # active peer already owns is never yanked away.
                # Owner-scoped exclusion (, rb-4792): NEVER reallocate
                # a goal that operates only on its owner's own dir tree (e.g.
                # /drain-temp) -- the reallocatee cannot execute it (wrong temp),
                # so surfacing it here just strands the reallocatee's top-of-queue
                # on unexecutable work. Owner-idle is NOT a reason to reallocate
                # owner-scoped work; it waits for the owner to revive.
                if (intended_agent in idle_agents
                        and not goal.get("claimed_by")
                        and not _is_owner_scoped_goal(goal)):
                    pass  # reallocate — surface to this running agent
                else:
                    continue

            # Per-runner capability skip ( Slice 2): drop goals whose
            # EXPLICIT requires_capability this runner cannot satisfy. A per-RUNNER
            # gap (distinct from a global block) — executable by OTHER agents, just
            # not on this box. Skipping keeps them out of ranking (mixed queue) AND
            # lets a fully-constrained box reach all_blocked -> not_my_lane
            # quiescence (collect_blocked classifies the same goals — the inverse).
            # Conservative (rb-1028): only EXPLICIT requires_capability gates; an
            # empty runner_caps (derivation failure) skips nothing.
            if runner_caps and not goal_is_locally_executable(goal, runner_caps):
                continue

            results.append({"goal": goal, "aspiration": asp, "source": source})

    return results


def collect_cross_agent_candidates(project_root, agent_dir, agent_name,
                                   known_blockers=None,
                                   global_done_ids=None,
                                   claim_timeout_hours=None,
                                   reallocation_hours=None,
                                   abstention_timeout_hours=None,
                                   defer_reason_timeout_hours=None,
                                   dependency_timeout_hours=None,
                                   global_live_ids=None):
    """Pull pending goals from sibling agent queues where intended_agent == agent_name.

    Closes the cross-agent stranding gap (g-115-946): the capability-route gate
    stamps intended_agent on newly-filed goals; when that lands in the FILER's
    private queue, the routed-to TARGET never sees the goal because the
    selector previously read only world + own-agent queues. This helper adds
    a third read pass over sibling agent dirs at selection time. Symmetric
    with the cross-agent awareness already present in directive_boost and
    handoff_bonus scoring criteria — preserves "each goal lives in one queue"
    invariant (no migration / copy / push).

    Returns list of candidate dicts shaped exactly like collect_candidates,
    with source="cross-agent:<owner_dir_name>" so downstream completion
    attribution can write back to the owning sibling's queue.

    Strict-match contract: ONLY goals where goal["intended_agent"] == agent_name
    are returned. Goals with intended_agent unset, "either", or another agent's
    name are NOT included (they're either visible via the agent's own queue
    or correctly routed elsewhere).

    Fail-open per sibling: an unreadable aspirations.jsonl, missing dir, or
    permission error on a single sibling skips THAT sibling without aborting
    the sweep. iterdir errors at the project_root level return empty list.
    """
    if project_root is None or agent_dir is None or not agent_name:
        return []
    results = []
    # : derive the agents-parent from agent_dir.parent, NOT
    # `project_root / "agents"`. Both wired call sites pass AGENT_DIR.parent
    # (which is ALREADY the agents parent = PROJECT_ROOT/agents) as project_root,
    # so `project_root / "agents"` computed PROJECT_ROOT/agents/agents
    # (nonexistent) -> the is_dir() guard below returned [] on EVERY call ->
    # 's cross-agent stranding fix was INERT from the Phase 2.5.D
    # relocation (agent dirs moved under agents/) until now. Empirically: alpha's
    # wired call returned 0 while a corrected call surfaced  (a live
    # bravo->alpha routed goal alpha had never seen). agent_dir.parent is
    # unambiguously the agents parent for BOTH the real call
    # (PROJECT_ROOT/agents/<name> -> .parent) and any test caller (custom
    # agent_dir -> its own parent), so it survives an AGENTS_PARENT_DIR rename
    # and any project_root drift. (project_root is retained in the signature for
    # backward compat + the None-guard; it is no longer used to locate siblings.)
    agents_parent = agent_dir.parent
    if not agents_parent.is_dir():
        return []
    try:
        siblings = list(agents_parent.iterdir())
    except Exception:
        return []  # fail-open at parent
    for sib_dir in siblings:
        if not sib_dir.is_dir() or sib_dir == agent_dir:
            continue
        sib_q = sib_dir / "aspirations.jsonl"
        if not sib_q.exists():
            continue  # non-agent dir (no aspirations.jsonl)
        try:
            sib_aspirations = read_jsonl(sib_q)
        except Exception:
            continue  # fail-open per sibling
        # Reuse the full eligibility filter from collect_candidates (status,
        # claims, defers, blocked_by, structured preconditions, participants,
        # intended_agent routing). The intended_agent filter at the end of
        # collect_candidates already drops goals routed to OTHER agents; we
        # post-filter to require exact match (drop "either" and None too,
        # those are not stranded — they're visible via world/own queue or
        # explicitly meant to stay in the filer's hands).
        sib_candidates = collect_candidates(
            sib_aspirations, known_blockers=known_blockers,
            source=f"cross-agent:{sib_dir.name}",
            global_done_ids=global_done_ids,
            claim_timeout_hours=claim_timeout_hours,
            reallocation_hours=reallocation_hours,
            abstention_timeout_hours=abstention_timeout_hours,
            defer_reason_timeout_hours=defer_reason_timeout_hours,
            dependency_timeout_hours=dependency_timeout_hours,
            global_live_ids=global_live_ids)
        for c in sib_candidates:
            if c.get("goal", {}).get("intended_agent") == agent_name:
                results.append(c)
    return results


# ---------------------------------------------------------------------------
# BLOCKED GOAL DIAGNOSTICS
# ---------------------------------------------------------------------------

def collect_blocked(aspirations, known_blockers=None, global_done_ids=None,
                    defer_reason_timeout_hours=None,
                    dependency_timeout_hours=None,
                    global_live_ids=None):
    """Return blocked goals with reasons (inverse of collect_candidates).

    Checks blocking conditions in priority order (first match = primary reason):
      explicit_status  — goal.status == "blocked"
      infrastructure   — goal.skill in known_blockers affected_skills
      dependency       — blocked_by contains unmet prerequisite IDs
      deferred         — deferred_until is in the future
      hypothesis_gate  — resolves_no_earlier_than is in the future

    Excludes: recurring cooldown (not a real block), user-only goals,
    completed/skipped/expired/decomposed/in-progress goals.
    """
    today = date.today()
    blocked = []
    # Per-runner capability set ( Slice 2) — derived once, cached
    # module-wide (same accessor as collect_candidates) so the not_my_lane
    # classification below is the exact inverse of the candidate skip.
    runner_caps = _get_runner_capabilities()

    # Map skill -> blocker info for infrastructure blocks
    blocker_by_skill = {}
    blocker_by_category = {}
    if known_blockers:
        for b in known_blockers:
            if b.get("resolution") is None:
                for skill in b.get("affected_skills", []):
                    blocker_by_skill[skill] = b
                for cat in b.get("affected_categories", []):
                    blocker_by_category[cat] = b

    for asp in aspirations:
        if asp.get("status") != "active":
            continue

        asp_id = asp.get("id", "")
        # Use global done_ids for cross-aspiration dependency resolution (must match
        # collect_candidates — otherwise a goal can appear "unblocked" in selection
        # but "blocked" in diagnostics for the same cross-aspiration dependency).
        if global_done_ids is not None:
            done_ids = global_done_ids
        else:
            done_ids = {g["id"] for g in asp.get("goals", [])
                        if g.get("status") in ("completed", "decomposed")}

        # live_ids: mirror of collect_candidates — goal IDs still able to complete
        # (non-terminal status). Re-validates dependency liveness so the timeout
        # fail-open stays the logical complement across both functions ().
        if global_live_ids is not None:
            live_ids = global_live_ids
        else:
            live_ids = {g["id"] for g in asp.get("goals", [])
                        if g.get("status") not in TERMINAL_GOAL_STATUSES}

        for goal in asp.get("goals", []):
            status = goal.get("status", "")
            goal_id = goal.get("id", "")

            # Skip terminal and in-progress statuses
            if status in SKIP_STATUSES:
                continue

            # Skip ineligible goals (user-only or other-agent)
            if not _is_agent_eligible(_ensure_list(goal.get("participants"), ["agent"]), AGENT_NAME):
                continue

            entry = {
                "goal_id": goal_id,
                "aspiration_id": asp_id,
                "title": goal.get("title", ""),
                "skill": goal.get("skill"),
                "priority": goal.get("priority", asp.get("priority", "MEDIUM")),
                "chain_position": None,
                # blocker_ref (Change 1: typed blocker reference). Present
                # uniformly so the quiescence gate (Change 2) has one read
                # path regardless of block_reason. For deferred blocks this
                # comes from goal.blocker_ref; for infrastructure blocks the
                # known_blockers augmentation below overwrites it with the
                # infra blocker's own blocker_ref. Absence (None) is the
                # signal that disqualifies quiescence.
                # : synth blocker_ref from structured-prefix defer
                # so quiescence accepts what the write-side already accepts.
                "blocker_ref": _synth_blocker_ref_from_structured_defer(goal),
            }

            # Checks 1-5: first match wins. Order matters — higher-level blocks
            # (infrastructure) must precede lower-level (dependency) so chain
            # compression classifies downstream goals correctly.
            # 1. Explicit "blocked" status
            if status == "blocked":
                entry["block_reason"] = "explicit_status"
                entry["block_detail"] = goal.get("block_reason", "No reason given")
                # C2 coverage (): an explicitly-blocked goal with no defer
                # fields keeps blocker_ref=None (the L1817 synth found nothing) ->
                # quiescence C2 fails -> B7 churn. Synth a type=resource ref keyed on
                # the goal id (mirrors branch 7). Defensive: 's
                # all-gap-shapes test only exercised explicit_status WITH a
                # defer/blocked_by/string-ref (which the L1817 synth already covers).
                if not isinstance(entry.get("blocker_ref"), dict):
                    entry["blocker_ref"] = _synth_block_ref("explicit-status", goal_id)
                blocked.append(entry)
                continue

            # Only pending goals from here
            if status != "pending":
                continue

            # 2. Infrastructure blocker (skill-based, primary)
            goal_skill = goal.get("skill", "")
            if goal_skill and goal_skill in blocker_by_skill:
                b = blocker_by_skill[goal_skill]
                entry["block_reason"] = "infrastructure"
                entry["block_detail"] = "{skill} blocked: {reason}".format(
                    skill=goal_skill, reason=b.get("reason", "unknown"))
                entry["blocker_id"] = b.get("blocker_id", "")
                # Prefer the known_blockers blocker_ref over the goal's own
                # — infrastructure blocks are owned by the blocker record,
                # not the individual goal. create-blocker.py writes this.
                if b.get("blocker_ref"):
                    entry["blocker_ref"] = b["blocker_ref"]
                # Residual gap (, bravo msg-2949): the known_blocker may
                # carry NO blocker_ref of its own, and the L1817 structured-defer
                # synth returns None for an infra-blocked goal with no defer fields,
                # leaving entry.blocker_ref=None -> quiescence C2 never passes ->
                # perpetual B7 churn. Synth a type=resource ref (mirrors branch 7).
                if not isinstance(entry.get("blocker_ref"), dict):
                    entry["blocker_ref"] = _synth_block_ref(
                        "infrastructure", b.get("blocker_id") or goal_skill)
                blocked.append(entry)
                continue

            # 2b. Infrastructure blocker (category fallback for skill=null goals)
            if not goal_skill:
                goal_cat = goal.get("category", "")
                if goal_cat and goal_cat in blocker_by_category:
                    b = blocker_by_category[goal_cat]
                    entry["block_reason"] = "infrastructure"
                    entry["block_detail"] = "{cat} category blocked: {reason}".format(
                        cat=goal_cat, reason=b.get("reason", "unknown"))
                    entry["blocker_id"] = b.get("blocker_id", "")
                    if b.get("blocker_ref"):
                        entry["blocker_ref"] = b["blocker_ref"]
                    # Same residual gap as the skill-based branch above
                    # (): synth a type=resource ref when neither the
                    # known_blocker nor the structured-defer synth supplied one.
                    if not isinstance(entry.get("blocker_ref"), dict):
                        entry["blocker_ref"] = _synth_block_ref(
                            "infrastructure", b.get("blocker_id") or goal_cat)
                    blocked.append(entry)
                    continue

            # 3. Dependency (blocked_by with unmet prerequisites, timeout-aware).
            # SYMMETRY: must be the logical complement of the blocked_by check in
            # collect_candidates. If you change one, change the other. (
            # fail-CLOSED hardening — same two branches: (a) null blocked_since and
            # (b) live unmet dep both KEEP the goal blocked; fail-open only when the
            # block is stale AND every unmet dep is dead/orphan.)
            unmet = [bid for bid in _ensure_list(goal.get("blocked_by")) if bid not in done_ids]
            if unmet:
                if dependency_timeout_hours is not None:
                    dep_age = hours_since(goal.get("blocked_since"))
                    live_unmet = [bid for bid in unmet if bid in live_ids]
                    if dep_age is not None and dep_age > dependency_timeout_hours and not live_unmet:
                        pass  # Genuinely stale (aged AND all unmet deps dead/orphan) — fail-open
                    else:
                        # null blocked_since (fail-closed), within timeout, or a live dep remains
                        entry["block_reason"] = "dependency"
                        entry["block_detail"] = "Waiting on: {deps}".format(deps=", ".join(unmet))
                        entry["unmet_deps"] = unmet
                        blocked.append(entry)
                        continue
                else:
                    entry["block_reason"] = "dependency"
                    entry["block_detail"] = "Waiting on: {deps}".format(deps=", ".join(unmet))
                    entry["unmet_deps"] = unmet
                    blocked.append(entry)
                    continue

            # 4. Deferred time gate
            deferred = goal.get("deferred_until")
            if deferred:
                try:
                    dt = datetime.fromisoformat(str(deferred))
                    if datetime.now() < dt:
                        entry["block_reason"] = "deferred"
                        entry["block_detail"] = "Deferred until {until}: {reason}".format(
                            until=deferred,
                            reason=goal.get("defer_reason", ""))
                        entry["deferred_until"] = str(deferred)
                        blocked.append(entry)
                        continue
                except (ValueError, TypeError):
                    pass

            # 4b. Defer reason (textual — blocks unless expired)
            # Symmetric to collect_eligible's defer_reason check (L664-680).
            # Goals WITH deferred_until are governed by the time gate at L864-877
            # above; this block only handles goals WITHOUT a structural gate.
            # (: prior `else: append blocked + continue` marked goals
            # with both fields as "deferred" even when deferred_until had passed.)
            if goal.get("defer_reason"):
                # SYMMETRY (): identical guard to collect_candidates —
                # only a FUTURE deferred_until defers to the time gate. A past one
                # must not bypass the defer_reason evaluation, or the goal lands in
                # NEITHER list (not blocked here, and a live candidate there) —
                # which is exactly how  escaped both. See the full
                # rationale at the collect_candidates site.
                if not _has_future_deferred_until(goal):
                    # human_blocked: never expires (). Keep it in
                    # blocked[] (synth blocker_ref already set on `entry` above by
                    # _synth_blocker_ref_from_structured_defer) so all_blocked can
                    # be asserted and quiescence fires, instead of falling through
                    # to the candidate pool every iteration.
                    if (goal.get("defer_reason") or "").lower().startswith(_HUMAN_BLOCKED_PREFIX):
                        entry["block_reason"] = "deferred"
                        entry["block_detail"] = "Human-blocked: {reason}".format(
                            reason=goal.get("defer_reason", ""))
                        blocked.append(entry)
                        continue
                    # No structural gate — apply expiry logic
                    if defer_reason_timeout_hours is not None:
                        defer_age = hours_since(goal.get("defer_reason_set_at"))
                        if defer_age is None or defer_age > defer_reason_timeout_hours:
                            pass  # Expired — fall through to candidate pool
                        else:
                            entry["block_reason"] = "deferred"
                            entry["block_detail"] = "Deferred: {reason}".format(
                                reason=goal.get("defer_reason", ""))
                            blocked.append(entry)
                            continue
                    else:
                        entry["block_reason"] = "deferred"
                        entry["block_detail"] = "Deferred: {reason}".format(
                            reason=goal.get("defer_reason", ""))
                        blocked.append(entry)
                        continue
                # else: has deferred_until — handled by time gate at L864-877

            # 5. Hypothesis time gate (datetime-form safe — , _parse_rne_dt)
            rne = goal.get("resolves_no_earlier_than")
            rne_dt = _parse_rne_dt(rne)
            if rne_dt is not None and datetime.now() < rne_dt:
                entry["block_reason"] = "hypothesis_gate"
                entry["block_detail"] = "Not before {date}".format(date=rne)
                blocked.append(entry)
                continue

            # 6. Structured preconditions unmet (SYMMETRY with collect_candidates).
            # NOTE: `goal.get("verification") or {}` defends against goals with
            # explicit `verification: null` — same fix as in collect_candidates.
            struct_pcs = [p for p in (goal.get("verification") or {}).get("preconditions") or []
                          if isinstance(p, dict) and "type" in p]
            # Magic Wand #4 (alpha session-60): fire_when sugar in collect_blocked
            # too — must mirror collect_candidates for the SYMMETRY invariant.
            fw = goal.get("fire_when")
            if isinstance(fw, dict) and "type" in fw:
                struct_pcs.append(fw)
            if struct_pcs:
                from predicate import evaluate_all as _eval_preconditions
                results = _eval_preconditions(struct_pcs, mode="fail_fast",
                                              include_skippable=False)
                failed = [r for r in results if not r.passed]
                if failed:
                    failed_ids = [r.predicate_id or r.type for r in failed]
                    entry["block_reason"] = "precondition_unmet"
                    entry["block_detail"] = "Preconditions unmet: " + ", ".join(failed_ids)
                    entry["precondition_unmet"] = failed_ids
                    # C2 coverage (, bravo msg-2949): a goal whose
                    # preconditions fail LIVE here but which carries no defer fields
                    # keeps blocker_ref=None (the L1817 synth found nothing) ->
                    # quiescence C2 fails -> B7 churn (//).
                    # Synth a type=resource ref keyed on the failing predicate set
                    # (stable while the same preconditions fail; mirrors branch 7).
                    if not isinstance(entry.get("blocker_ref"), dict):
                        entry["blocker_ref"] = _synth_block_ref(
                            "precondition", ",".join(sorted(failed_ids)))
                    blocked.append(entry)
                    continue

            # 7. Not-my-lane: this runner lacks a capability the goal EXPLICITLY
            #    requires ( Slice 2). A per-RUNNER gap, distinct from a
            #    global block — the goal is executable by OTHER agents, just not on
            #    this box. Classifying it blocked (the INVERSE of collect_candidates'
            #    capability skip — same runner_caps + goal_is_locally_executable, so
            #    a goal is a candidate XOR not_my_lane-blocked, never both) lets a
            #    fully capability-constrained runner reach all_blocked -> quiescence
            #    sleep instead of hot-looping on unexecutable goals. LAST check
            #    (mirrors capability being the last collect_candidates filter): an
            #    earlier real block (dependency/deferred/precondition) wins. Synth a
            #    blocker_ref so quiescence C2/C3 accept it; type "resource" (NOT
            #    user-only) -> only normal-quiescence short sleep, re-checked each
            #    wake (self-healing as caps/queue change). Conservative (rb-1028):
            #    only EXPLICIT requires_capability gates; an empty runner_caps
            #    (derivation failure) classifies nothing (matches the skip guard).
            if runner_caps and not goal_is_locally_executable(goal, runner_caps):
                missing = sorted(goal_required_capabilities(goal) - set(runner_caps))
                entry["block_reason"] = "not_my_lane"
                entry["block_detail"] = (
                    "Requires capability not on this runner: {m} (runner has: {r})".format(
                        m=",".join(missing), r=",".join(sorted(runner_caps)) or "none"))
                entry["missing_capabilities"] = missing
                if not isinstance(entry.get("blocker_ref"), dict):
                    _nml_now = datetime.now()
                    _nml_key = "not-my-lane:" + ",".join(missing)
                    entry["blocker_ref"] = {
                        "type": "resource",
                        "external_id": "not-my-lane:" + hashlib.md5(
                            _nml_key.encode("utf-8")).hexdigest()[:12],
                        "state_hash": None,
                        "created_at": _nml_now.isoformat(timespec="seconds"),
                        "expires_at": (_nml_now + timedelta(hours=120)).isoformat(
                            timespec="seconds"),
                        "synthesized": True,
                    }
                blocked.append(entry)
                continue

            # Recurring cooldown is NOT a block — goal is just "not yet due"

    # Dependency chain compression: mark head vs downstream
    dep_blocked_ids = {e["goal_id"] for e in blocked if e["block_reason"] == "dependency"}
    for entry in blocked:
        if entry["block_reason"] == "dependency":
            # Head = none of its unmet deps are themselves dependency-blocked
            unmet = entry.get("unmet_deps", [])
            if any(u in dep_blocked_ids for u in unmet):
                entry["chain_position"] = "downstream"
            else:
                entry["chain_position"] = "head"

    return blocked


def trace_root_bottleneck(goal_id, goal_map, done_ids, blocker_by_skill, blocker_by_category=None, visited=None):
    """Walk dependency chains to find the ultimate root blocker.

    Returns (root_goal_id, cause_label) tuple.
    Follows blocked_by references recursively until hitting a terminal condition.
    """
    if visited is None:
        visited = set()
    if goal_id in visited:
        return (goal_id, "CYCLE")
    visited.add(goal_id)

    goal = goal_map.get(goal_id)
    if not goal:
        return (goal_id, "UNKNOWN (missing goal)")

    status = goal.get("status", "")

    # Terminal statuses
    if status == "in-progress":
        return (goal_id, "IN PROGRESS")
    if status == "blocked":
        return (goal_id, "BLOCKED (status)")
    if status in ("skipped", "expired"):
        return (goal_id, "DEAD END: prereq {id} {status}".format(id=goal_id, status=status))

    # For pending goals: check unsatisfied deps
    if status == "pending":
        unsatisfied = [b for b in _ensure_list(goal.get("blocked_by")) if b not in done_ids]
        if unsatisfied:
            # Follow first dep only — preserves 1:1 goal→bottleneck invariant
            return trace_root_bottleneck(unsatisfied[0], goal_map, done_ids,
                                         blocker_by_skill, blocker_by_category, visited)

        # No unsatisfied deps — this IS the root. Classify it.
        deferred = goal.get("deferred_until")
        if deferred:
            try:
                dt = datetime.fromisoformat(str(deferred))
                if datetime.now() < dt:
                    return (goal_id, "DEFERRED until {t}".format(t=deferred))
            except (ValueError, TypeError):
                pass

        goal_skill = goal.get("skill", "")
        if goal_skill and goal_skill in blocker_by_skill:
            reason = blocker_by_skill[goal_skill].get("reason", "unknown")
            return (goal_id, "INFRA: {r}".format(r=reason))
        if not goal_skill and blocker_by_category:
            goal_cat = goal.get("category", "")
            if goal_cat and goal_cat in blocker_by_category:
                reason = blocker_by_category[goal_cat].get("reason", "unknown")
                return (goal_id, "INFRA: {cat} — {r}".format(cat=goal_cat, r=reason))

        participants = _ensure_list(goal.get("participants"), ["agent"])
        if not _is_agent_eligible(participants, AGENT_NAME):
            if participants == ["user"]:
                return (goal_id, "NEEDS USER")
            return (goal_id, "OTHER AGENT ({})".format(", ".join(p for p in participants if p != "user")))

        return (goal_id, "READY")

    # Completed/decomposed shouldn't reach here (in done_ids), but handle gracefully
    return (goal_id, "READY")


# ---------------------------------------------------------------------------
# Evidence backing
# ---------------------------------------------------------------------------

def evidence_score(asp, resolved):
    """Compute evidence_backing for an aspiration from resolved hypotheses.

    For each resolved hypothesis relevant to this goal's aspiration:
      earned_confirmed: +2.0, unlucky_corrected: +1.0
      lucky_confirmed: +0.5, deserved_corrected: -1.0
    Normalize by count. 0 if no relevant hypotheses.
    """
    tags = set(_ensure_list(asp.get("tags")))
    hyp_ids = {g.get("hypothesis_id") for g in asp.get("goals", [])
               if g.get("hypothesis_id")}

    relevant = [h for h in resolved
                if h.get("category") in tags or h.get("id") in hyp_ids]
    if not relevant:
        return 0.0

    dual_scores = {
        "earned_confirmed": 2.0, "unlucky_corrected": 1.0,
        "lucky_confirmed": 0.5, "deserved_corrected": -1.0,
    }
    total = 0.0
    for h in relevant:
        ps = h.get("process_score") or {}
        dc = ps.get("dual_classification") if isinstance(ps, dict) else None
        if dc and dc in dual_scores:
            total += dual_scores[dc]
        else:
            # Fallback: use outcome directly
            outcome = h.get("outcome")
            total += 1.0 if outcome == "CONFIRMED" else (-0.5 if outcome == "CORRECTED" else 0)

    return total / len(relevant)


# ---------------------------------------------------------------------------
# Category resolution
# ---------------------------------------------------------------------------

def _resolve_category(goal, asp):
    """Resolve goal category: direct field > suggest from text > aspiration tag.

    Falls back through three strategies:
    1. goal.category if set and not "uncategorized"
    2. category-suggest.py on title+description
    3. First aspiration tag, then "uncategorized"
    """
    cat = goal.get("category")
    if cat and cat != "uncategorized":
        return cat

    # Derive from title+description via category-suggest
    text = "{title}. {desc}".format(
        title=goal.get("title", ""),
        desc=goal.get("description", ""),
    )
    try:
        result = subprocess.run(
            [sys.executable, str(CORE_ROOT / "scripts" / "category-suggest.py"),
             "--text", text, "--top", "1"],
            capture_output=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            matches = json.loads(result.stdout)
            if matches and matches[0].get("score", 0) > 0:
                return matches[0]["key"]
    except Exception:
        pass

    tags = _ensure_list(asp.get("tags"))
    return tags[0] if tags else "uncategorized"


# ---------------------------------------------------------------------------
# Directive Boost (cross-agent priority influence)
# ---------------------------------------------------------------------------

BOARD_COORD_PATH = WORLD_DIR / "board" / "coordination.jsonl"


def load_active_directives():
    """Load active (non-expired) directive messages from the coordination board.

    Returns a list of dicts: [{target_goals: [...], target_categories: [...], weight: float}]
    Parses structured tags from directive messages (see board.md Directive Payload Schema).
    """
    if not BOARD_COORD_PATH.exists():
        return []
    directives = []
    now = datetime.now()
    for msg in read_jsonl(BOARD_COORD_PATH):
        if msg.get("type") != "directive":
            continue
        tags = _ensure_list(msg.get("tags"))
        # Parse expiry
        expires = None
        for tag in tags:
            if tag.startswith("expires:"):
                try:
                    expires = datetime.fromisoformat(tag[8:])
                except (ValueError, TypeError):
                    pass
        if expires and now > expires:
            continue  # Expired
        # Parse weight modifier
        weight = 0.0
        for tag in tags:
            if tag.startswith("weight:"):
                try:
                    weight = float(tag[7:])
                except (ValueError, TypeError):
                    pass
        if weight == 0.0:
            continue  # No weight = no effect
        # Parse targets
        target_goals = []
        target_categories = []
        for tag in tags:
            if tag.startswith("target:"):
                target_goals.append(tag[7:])
            elif tag.startswith("category:"):
                target_categories.append(tag[9:])
        if not target_goals and not target_categories:
            continue  # No targets = no effect
        directives.append({
            "target_goals": target_goals,
            "target_categories": target_categories,
            "weight": weight,
        })
    return directives


# Cache directives for the duration of a single selector run.
# Safe without cleanup: each goal-selector.py invocation is a separate process.
_ACTIVE_DIRECTIVES = None


def _get_directives():
    global _ACTIVE_DIRECTIVES
    if _ACTIVE_DIRECTIVES is None:
        _ACTIVE_DIRECTIVES = load_active_directives()
    return _ACTIVE_DIRECTIVES


# --- strategic_focus: the standing user directive () --------------
# world/team-state.yaml `strategic_focus` is set by the USER and acknowledged by
# every agent. The live one reads: "Product goals outrank routine infra sweeps
# AT SELECTION TIME until  drains." Until now it was consumed by exactly
# two readers — boot/SKILL.md and create-aspiration/SKILL.md — and NOT by this
# file. A directive whose own text names selection time had no path into
# selection, so five agents acknowledged it and the ranking never changed.
#
# It rides the EXISTING directive_boost criterion instead of adding a new one:
# that term already means "user / cross-agent priority influence", already
# carries WEIGHTS["directive_boost"] = 1.5, and reusing it adds no breakdown key
# for downstream consumers to break on. Bounded bias, never a veto — the scorer
# still owns the ranking (Scorer Sovereignty, ).
#
# Only the aspiration ids in the prose are machine-usable; the rest is rationale
# for humans. Parsing is therefore deliberately narrow (an `asp-NNN` regex), and
# every failure path yields no boost — the selector must never depend on this.
_STRATEGIC_FOCUS_ASP_RE = re.compile(r"\basp-\d+\b")
_STRATEGIC_FOCUS = None


def load_strategic_focus():
    """-> {"aspirations": set[str], "weight": float}.

    Reads team-state via the run-scoped cache (no extra I/O — handoff liveness
    already loads it). Missing/malformed strategic_focus, or prose naming no
    aspiration, yields an empty aspiration set = no boost anywhere.

    Prose that EXISTS but names no `asp-NNN` warns on stderr. Silence there
    would be the vacuous-pass shape (`checker-input-assumption-defects` tree
    node): the user writes "asp 335" or renames a lane, the regex matches
    nothing, the boost quietly does nothing, and the directive looks honored
    because no error was raised. A live directive that parses to zero targets
    is always worth one loud line.
    """
    global _STRATEGIC_FOCUS
    if _STRATEGIC_FOCUS is not None:
        return _STRATEGIC_FOCUS
    weight = 1.0
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", Path(__file__).parent / "_config_overlay.py")
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        sfb = overlay.merged_config("aspirations.yaml").get(
            "strategic_focus_boost", {})
        if isinstance(sfb, dict) and sfb.get("weight") is not None:
            weight = float(sfb["weight"])
    except Exception:  # noqa: BLE001 — advisory input, keep the default
        pass
    asps = set()
    try:
        sf = (_load_team_state_cached() or {}).get("strategic_focus")
        if isinstance(sf, dict):
            # `primary` is the directive proper; `secondary` is optional and
            # only present in some deployments.
            text = " ".join(
                str(sf.get(k) or "") for k in ("primary", "secondary")).strip()
            asps = set(_STRATEGIC_FOCUS_ASP_RE.findall(text))
            if text and not asps:
                print(f"[goal-selector] WARN: strategic_focus is set but names "
                      f"no asp-NNN, so it boosts nothing — check the wording "
                      f"(set_by={sf.get('set_by')!r}): {text[:160]!r}",
                      file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — never block selection on this
        print(f"[goal-selector] WARN: strategic_focus unreadable "
              f"({type(e).__name__}: {e}); directive boost disengaged",
              file=sys.stderr)
    _STRATEGIC_FOCUS = {"aspirations": asps, "weight": weight}
    return _STRATEGIC_FOCUS


def strategic_focus_boost(asp_id, completion_ratio):
    """Bounded boost for goals under an aspiration the user's directive names.

    Self-retiring: the live directive says "until asp-335 drains", so a named
    aspiration at completion_ratio >= 1.0 stops being boosted without anyone
    having to edit team-state. Stale prose then costs nothing.
    """
    if not asp_id:
        return 0.0
    sf = load_strategic_focus()
    if asp_id not in sf["aspirations"]:
        return 0.0
    if completion_ratio is not None and completion_ratio >= 1.0:
        return 0.0  # drained — the directive has satisfied itself here
    return sf["weight"]


def directive_boost_score(goal_id, category):
    """Compute directive boost for a goal based on active directives."""
    boost = 0.0
    for d in _get_directives():
        if goal_id in d["target_goals"]:
            boost += d["weight"]
        elif category in d["target_categories"]:
            boost += d["weight"]
    return boost


def emit_directive_honor_banner(scored, agent_name, board_path=None):
    """Emit a LOUD stderr DIRECTIVE-HONOR banner (guard-1310, ) for each
    active directive DIRECTED AT agent_name that targets a goal PRESENT in the
    scored candidate list AND that agent_name has NOT yet acked.

    Compaction-proof companion to aspirations-select Phase 2.07's LLM-executed
    DIRECTIVE-HONOR hard rule. That LLM path is skippable after autocompact (the
    exact 2026-07-20 miss: a user directive targeting zeta was lane-skipped 5+
    times over 8h with 0 acks / 0 read-receipts). goal-selector.py runs EVERY
    iteration ("goal-selector.sh MUST run every iteration, no exceptions"), so a
    bash-emitted banner here cannot be summarized away by compaction. Reuses the
    same board parse + expiry/target tag semantics as load_active_directives, but
    keys on the directive id + agent-target (which load_active_directives drops)
    so it can filter "directed at THIS agent" and check ack existence.

    Fire condition mirrors the SKILL.md hard rule exactly ("any target goal-id is
    in ranked_goals"): the target must be an EXECUTABLE candidate (present in
    scored). A blocked / precondition-gated directive target is absent from
    scored and is handled by the justified-deferral path instead -- and that
    ack then suppresses this banner (a plain ack OR a justified-deferral ack both
    reply_to the directive). Returns the list of warnings emitted (for testing);
    the side effect is the stderr banner. Fail-open: any error prints a skip note
    to stderr and returns []; the banner must never block goal selection.
    """
    if not agent_name or not scored:
        return []
    bp = board_path if board_path is not None else BOARD_COORD_PATH
    try:
        if not bp.exists():
            return []
        rows = list(read_jsonl(bp))
    except Exception as e:  # pragma: no cover - fail-open guard
        print(f"[goal-selector] directive-honor banner skipped "
              f"({type(e).__name__}: {e})", file=sys.stderr)
        return []
    # Directive-ids this agent has already replied to (a plain ack OR a
    # justified-deferral ack both reply_to the directive -> honored -> no nag).
    acked = {r.get("reply_to") for r in rows
             if r.get("author") == agent_name and r.get("reply_to")}
    rank_by_id = {s.get("goal_id"): i for i, s in enumerate(scored)}
    now = datetime.now()
    # Roster for bare-agent-name routing-tag detection (). Fetched
    # ONCE per run; fail-open to empty (then only requires_action_by:* tags —
    # which need no roster — count as explicit routing, still fixing the
    # canonical incident).
    try:
        from _agents import get_active_agents
        known_agents = set(get_active_agents())
    except Exception:  # pragma: no cover - fail-open guard
        known_agents = set()
    warnings = []
    for msg in rows:
        if msg.get("type") != "directive":
            continue
        tags = _ensure_list(msg.get("tags"))
        expires = None
        for tag in tags:
            if tag.startswith("expires:"):
                try:
                    expires = datetime.fromisoformat(tag[8:])
                except (ValueError, TypeError):
                    pass
        if expires and now > expires:
            continue  # expired -- same semantics as load_active_directives
        text = str(msg.get("text", "") or "")
        # : an explicit routing tag takes PRECEDENCE over a loose
        # prose mention. A directive routed to agent X (requires_action_by:X or
        # a bare agent-name tag) but naming agent Y in an exclusionary prose
        # clause ("X please claim; Y cannot do it") must NOT flag Y — the
        # prose-mention fallback fires ONLY when the directive carries no
        # explicit routing tag. Live incident msg-20260721-211141-bravo-5456
        # (routed requires_action_by:alpha, prose "bravo cannot deploy it well")
        # false-flagged bravo on every selection; self-authored directives
        # (author names self in prose) hit the identical trap.
        has_routing_tag = (
            any(t.startswith("requires_action_by:") for t in tags)
            or any(t in known_agents for t in tags))
        explicitly_directed = (agent_name in tags
                               or f"requires_action_by:{agent_name}" in tags)
        directed = explicitly_directed or (
            not has_routing_tag and agent_name.lower() in text.lower())
        if not directed:
            continue
        did = msg.get("id")
        if did in acked:
            continue  # already honored (ack or justified-deferral)
        target_goals = [t[7:] for t in tags if t.startswith("target:")]
        for gid in target_goals:
            if gid not in rank_by_id:
                continue  # blocked/gated -> not an executable candidate
            idx = rank_by_id[gid]
            rank = idx + 1
            score = scored[idx].get("score")
            warnings.append({"directive_id": did, "goal_id": gid, "rank": rank})
            print(
                f"\n[goal-selector] ========== DIRECTIVE-HONOR REQUIRED "
                f"(guard-1310) ==========\n"
                f"[goal-selector]   Directive {did} (directed at {agent_name}, "
                f"UNACKED) targets {gid}\n"
                f"[goal-selector]   -> candidate #{rank}/{len(scored)} "
                f"(score {score}).\n"
                f"[goal-selector]   SELECT it now, OR post a justified-deferral "
                f"ack (--reply-to {did})\n"
                f"[goal-selector]   naming a HARD blocker (infra / capability gap "
                f"-- NOT lane / focus).\n"
                f"[goal-selector]   A silent lane/focus/consolidate skip is "
                f"FORBIDDEN (guard-1310).\n"
                f"[goal-selector] ================================================"
                f"============",
                file=sys.stderr)
    return warnings


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_goal(cand, wm, resolved, session_completions, epsilon=0.85, noise_scale=3.0):
    """Score a single goal using the 15-criteria weighted formula."""
    goal, asp, source = cand["goal"], cand["aspiration"], cand.get("source", "world")
    raw = {}

    # 1. priority (HIGH=3, MEDIUM=2, LOW=1)
    raw["priority"] = PRIORITY_MAP.get(
        goal.get("priority", asp.get("priority", "MEDIUM")), 2)

    # 2. deadline_urgency (+3 <=1d, +2 <=3d, +1 <=7d; long-horizon ramp +0.5 <=30d, +0.25 <=90d)
    # Inheritance (): a goal with no own deadline inherits its aspiration's
    # `deadline`, so a fixed external deadline (e.g. the ARC clock, 2026-11-02) creates
    # prioritization pull on every goal under that aspiration -- not only goals that
    # individually carry resolves_by. The long-horizon ramp (0.5 at <=30d, 0.25 at <=90d)
    # gives months-out pull without overriding near-term urgency or priority weight.
    deadline = goal.get("resolves_by") or goal.get("deadline") or asp.get("deadline")
    remaining = days_until(deadline)
    raw["deadline_urgency"] = (
        3 if remaining is not None and remaining <= 1 else
        2 if remaining is not None and remaining <= 3 else
        1 if remaining is not None and remaining <= 7 else
        0.5 if remaining is not None and remaining <= 30 else
        0.25 if remaining is not None and remaining <= 90 else 0)

    # 3. agent_executable (+2 if current agent is eligible)
    participants = _ensure_list(goal.get("participants"), ["agent"])
    raw["agent_executable"] = 2 if _is_agent_eligible(participants, AGENT_NAME) else 0

    # 4. variety_bonus (+1.5 if different aspiration than last touched)
    touched = wm.get("aspiration_touched_last", "")
    raw["variety_bonus"] = 1.5 if asp.get("id") != touched else 0

    # 5. streak_momentum (+0.5 if same aspiration had a goal completed this session)
    # Each entry written by aspirations-state-update Step 3: {"goal_id", "aspiration_id", "recurring", "_item_ts"}
    asp_id = asp.get("id", "")
    raw["streak_momentum"] = (
        0.5 if any(s.get("aspiration_id") == asp_id for s in session_completions) else 0)

    # 6. novelty_bonus (+1.0 if never done before)
    raw["novelty_bonus"] = 1.0 if goal.get("achievedCount", 0) == 0 else 0

    # 7. recurring_urgency (log-scaled: base + log2(1 + overdue_ratio) * scale, capped at urgency_max)
    # Logarithmic scaling preserves differentiation among overdue goals — a 72x-overdue
    # goal scores higher than a 4x-overdue one, unlike the old linear cap at 5.0.
    # urgency_max (, zeta-1477 fix) caps raw at a ceiling so heavily-overdue
    # recurring goals can no longer systematically out-score capped role_affinity
    # (1.5x ceiling × weight 1.0 = 1.5 max contribution) — bounds asymmetry while
    # preserving relative ordering up to the cap point (~3x overdue at default 4.0).
    rec = 0
    overdue_ratio = 0.0  # hoisted (FW-1): exposed in the result dict so the
                         # post-scoring substantive-demotion exemption can read it.
    if goal.get("recurring"):
        interval = get_interval_hours(goal)
        #  (cross-machine clock-skew fix): capture the RAW lastAchievedAt
        # field separately from the computed elapsed. hours_since() returns None for
        # BOTH an ABSENT lastAchievedAt AND a PRESENT-but-FUTURE one (an off-machine
        # ahead-clock stamps lastAchievedAt in this box's future; the hours<0 clamp
        # in hours_since() turns that into None). never_fired MUST key on the FIELD's
        # absence (la_raw is None), NOT on la being None — otherwise a just-achieved
        # goal whose stamp reads as future is misclassified as never-fired-since-
        # creation and the  fallback below inflates it to urgency_max (probe:
        # rec=4.0 for a not-due goal vs the correct 0). Keying on la_raw lets a
        # future-stamp fall through to the not-due path (la stays None → the
        # `la is not None and la >= interval` guard is False → rec stays 0), while a
        # genuinely-absent field still triggers the  never-fired escalation.
        la_raw = goal.get("lastAchievedAt")
        la = hours_since(la_raw)
        # NEVER-FIRED FALLBACK (): a recurring goal with no lastAchievedAt
        # has never fired. Deriving overdue_ratio from lastAchievedAt alone pins
        # such a goal at urgency_base forever — a 41-day-old 24h-interval goal
        # (the tree-decompose-drain  case: 0 fires since 2026-05-12)
        # scored identically to one that fired moments ago, so the class-level
        # recurring_saturation penalty (7b below) buried it under pickable rank
        # indefinitely and its function silently never ran. The "truly overdue
        # recurring goals overcome saturation via high recurring_urgency" design
        # is defeated for never-fired goals because they cannot accrue urgency
        # off a null baseline. Treat "never fired" as "overdue since
        # created_at + interval" so the most-neglected recurring goal earns the
        # urgency the escalator intends. Mirrors the created-age precedent at the
        # reallocation filter (hours_since(goal.get("created")…)). created_at is
        # the live field name (goal-schemas.md); `created` is the legacy alias.
        never_fired = la_raw is None  # : FIELD absence, not `la is None`
        if never_fired:
            la = hours_since(goal.get("created_at") or goal.get("created"))
        if never_fired or (la is not None and la >= interval and interval > 0):
            if la is not None and interval > 0:
                overdue_ratio = max((la - interval) / interval, 0.0)
            rec = RECURRING_CONFIG["urgency_base"] + math.log2(1 + overdue_ratio) * RECURRING_CONFIG["urgency_log_scale"]
            rec = min(rec, RECURRING_CONFIG["urgency_max"])
    raw["recurring_urgency"] = rec

    # 7b. recurring_saturation (penalty when recurring goals dominate recent selections)
    # Uses goals_completed_this_session from working memory. Each entry has an optional
    # "recurring" flag (defaults to False for backward compat with older entries).
    # Truly overdue recurring goals overcome this via high recurring_urgency.
    rec_sat = 0.0
    if goal.get("recurring") and session_completions:
        window = int(RECURRING_CONFIG["saturation_window"])
        recent = session_completions[-window:]
        recurring_count = sum(1 for s in recent if s.get("recurring", False))
        ratio = recurring_count / len(recent)
        rec_sat = -(ratio * RECURRING_CONFIG["saturation_max_penalty"])
    raw["recurring_saturation"] = rec_sat

    # 7c. per_goal_saturation (penalty when the SAME goal_id fires rapidly)
    # Tranche B 2026-04-20 (rb-390). Addresses the " fired 4× in 2 min"
    # failure mode the recurring.saturation_* block above cannot see — that one
    # measures the CLASS ratio (any recurring goal in window), not the specific
    # goal_id. This criterion suppresses rapid re-selection of the exact same
    # goal by counting matches in the recent completions window and applying
    # a flat penalty once the threshold is reached. Count-based rather than
    # wall-clock because session_completions entries carry no timestamp; in
    # practice 4 back-to-back fires == 4 consecutive window entries regardless
    # of elapsed seconds, which is exactly the signal we want.
    pgs = 0.0
    if session_completions:
        window = int(PER_GOAL_SATURATION_CONFIG["window_size"])
        threshold = int(PER_GOAL_SATURATION_CONFIG["consecutive_threshold"])
        recent = session_completions[-window:]
        same_id = sum(1 for s in recent if s.get("goal_id") == goal.get("id"))
        if same_id >= threshold:
            pgs = float(PER_GOAL_SATURATION_CONFIG["suppress_penalty"])
    raw["per_goal_saturation"] = pgs

    # 7d. user_signal_boost (Path B: snapshot-level silent_48h detection only)
    # Path A (per-goal user_signal_kind/user_thread_id) retired 2026-04-24
    # after : fields stayed 0/798 for 6+ days — writer never landed.
    # Re-audit on 2026-05-07 will confirm Path B still contributes. To revert:
    # restore signal_kind/thread_id reads AND ship a scanner that populates
    # them in the same change — reader-without-writer is the failure mode
    # this retire removed. See bravo/reports/-user-signal-boost-decision.md.
    usb_total = 0.0
    if USER_SIGNAL_SNAPSHOT:
        sources = USER_SIGNAL_SNAPSHOT.get("sources", {}) or {}
        goal_id = goal.get("id")
        pq = sources.get("pending_questions", {}) or {}
        silent_ids = pq.get("silent_48h_goal_ids", []) or []
        if goal_id and isinstance(silent_ids, list) and goal_id in silent_ids:
            usb_total += float(USER_SIGNAL_BOOST_CONFIG["silence_48h_boost"])
    raw["user_signal_boost"] = usb_total

    # 7e. class_balance_bonus (pull under-represented work_class up)
    # Tranche C 2026-04-20 (rb-390). Computes the last-N distribution of
    # work_class among session completions; if this goal's class is below
    # its configured target fraction, boost proportionally (capped by
    # max_boost); if above, penalize (capped by max_penalty). Goals with
    # no work_class tag default to "unclassified" and are excluded from
    # both the distribution and the bonus computation.
    # Fail-open: empty distribution, no targets, missing work_class,
    # or disabled config → 0.0.
    cbb = 0.0
    targets = CLASS_BALANCE_CONFIG["targets"] or {}
    goal_class = goal.get("work_class")
    if (
        targets
        and goal_class
        and goal_class != "unclassified"
        and goal_class in targets
        and session_completions
    ):
        window = int(CLASS_BALANCE_CONFIG["window_size"])
        recent = session_completions[-window:]
        # work_class in session_completions entries is optional; missing →
        # excluded from the denominator so classes that aren't being tracked
        # don't dilute the fractions.
        classed = [s.get("work_class") for s in recent if s.get("work_class")]
        if classed:
            count = sum(1 for c in classed if c == goal_class)
            observed_fraction = count / len(classed)
            target_fraction = float(targets.get(goal_class, 0.0))
            # deficit > 0 = under-represented; < 0 = over-represented
            deficit = target_fraction - observed_fraction
            max_boost = float(CLASS_BALANCE_CONFIG["max_boost"])
            max_penalty = float(CLASS_BALANCE_CONFIG["max_penalty"])
            # Linear proportional to deficit, clamped to [max_penalty, max_boost].
            # Deficit range is [-1, +1]; scale = cap at boundary.
            if deficit >= 0:
                cbb = min(max_boost, deficit * max_boost * 2)  # ×2 so deficit=0.5 saturates
            else:
                cbb = max(max_penalty, deficit * abs(max_penalty) * 2)
    raw["class_balance_bonus"] = cbb

    # 7f. role_affinity (per-agent work_class preference)
    # Magic Wand 4 (bravo session-61, 2026-05-07): encodes the agent's role
    # into scoring so PM-shaped goals naturally rank higher for PM agents
    # (bravo) and code-shaped goals naturally rank higher for code agents
    # (alpha). Replaces reliance on per-iteration LLM metacognition to
    # enforce role. Stacks additively with handoff_bonus — a bravo-created
    # goal with handoff_to=alpha gets BOTH bonuses on alpha's selector,
    # reinforcing routing intent. Backward compat: missing AGENT_NAME,
    # missing multipliers config, or missing/unclassified work_class all
    # resolve to multiplier 0.0 → zero contribution → identical to today.
    # Decision rule lives in compute_role_affinity (top-level, testable).
    raw["role_affinity"] = compute_role_affinity(
        AGENT_NAME, goal_class, AGENT_ROLE_MULTIPLIERS
    )

    # 8. reward_history (+1.0 if previous goals in this aspiration had high success)
    # Census-augmented (B9-deep): evicted completed goals still count toward the
    # reward signal, so an aspiration whose done goals were archived is not
    # mistaken for one that never succeeded.
    _, completed = effective_counts(asp, exclude_statuses=ABANDONED_STATUSES)
    raw["reward_history"] = 1.0 if completed > 0 else 0

    # 8b. completion_pressure (nonlinear boost for near-complete aspirations)
    # Quadratic: negligible for early aspirations, dominant for near-complete ones
    #   1/15 = 0.01, 7/15 = 0.54, 10/15 = 1.11, 14/15 = 2.18
    # "active" = status not in ABANDONED_STATUSES (recurring kept). effective_counts
    # folds archived completed back in so eviction leaves the ratio byte-identical.
    total_goals, done_goals = effective_counts(
        asp, exclude_statuses=ABANDONED_STATUSES)
    completion_ratio = done_goals / total_goals if total_goals > 0 else 0
    # Completability factor (; zeta rb-2384 / exp-). Bare
    # completion_ratio² radiated near-max pressure FOREVER from never-completable
    # aspirations — recurring catch-alls (: recurring goals refill `total`
    # indefinitely so the ratio is pinned ~0.96) and blocked tails (:
    # gap-to-1.0 all blocked) — structurally starving achievable product lanes
    # ( Vinheim, 8-day stall). `completability` = the share of the remaining
    # gap that is genuine achievable terminal progress (pending/in-progress AND
    # non-recurring; blocked + recurring goals never close the gap). Folding it
    # into the ratio BEFORE squaring keeps the existing quadratic shape:
    # completion_pressure becomes (achievable_completion_ratio)². A genuine all-
    # pending tail (completability == 1.0) is BYTE-IDENTICAL to the prior formula;
    # only recurring/blocked-dominated aspirations are discounted (by the square of
    # their unachievable share). This is a completability FACTOR, NOT a denominator
    # swap (done/achievable_total RAISES the blocked-tail ratio — wrong direction,
    # rb-2384). Live goals are never evicted (eviction removes only NON-RECURRING
    # TERMINAL goals, _goal_census.py:91) so the completable count is complete.
    # Due hygiene is unaffected: it rides recurring_urgency + cadence-gating.
    _remaining = total_goals - done_goals
    if _remaining > 0:
        _completable = sum(
            1 for _g in (asp.get("goals") or [])
            if _g.get("status") in ("pending", "in-progress")
            and not _g.get("recurring"))
        _completability = min(1.0, _completable / _remaining)
    else:
        _completability = 1.0
    raw["completion_pressure"] = ((completion_ratio * _completability) ** 2) * 2.5

    # 8c. tail_bonus (surface frontier goals as an aspiration nears completion)
    # Zero below 50% completion. As the tail shrinks, each remaining goal gets
    # a larger share of the pull — final straggler in 14/15 scores ~1.30 raw
    # under the 0.50 threshold. Natural ceiling is ~1.50 (asymptote at
    # remaining=1, ratio→1); do not add a cap, it would mask deliberate future
    # tuning of the 3.0 factor. Threshold lowered from 0.70 to 0.50 to extend
    # consolidation pressure into the mid-tail range (50-70% aspirations).
    if completion_ratio >= 0.50 and total_goals > done_goals:
        remaining = total_goals - done_goals  # guaranteed >= 1 by the guard above
        raw["tail_bonus"] = (completion_ratio - 0.50) / remaining * 3.0
    else:
        raw["tail_bonus"] = 0.0

    # 8d. depth_bonus (reward continuing in same aspiration — counterbalances variety_bonus)
    raw["depth_bonus"] = 1.0 if asp.get("id") == touched else 0

    # 8e. cross_aspiration_support (LifingPolls plan item 2 — 2026-05-08).
    # When a goal declares supports: [asp-id, ...], it advances the named
    # aspirations in addition to its own parent. Boost = sum of supported
    # aspirations' completion_pressure × per_support_weight, capped at +2.0.
    # Soft attribution only — completion still ticks one parent.
    supports = goal.get("supports") or []
    if isinstance(supports, list) and supports:
        per_weight = 0.3
        cap = 2.0
        bonus = 0.0
        for sup_id in supports:
            sup_ratio = _ASP_COMPLETION_RATIOS.get(sup_id, 0.0)
            # Mirror completion_pressure shape: quadratic × 2.5 weight.
            bonus += (sup_ratio ** 2) * 2.5 * per_weight
        raw["cross_aspiration_support"] = round(min(bonus, cap), 3)
    else:
        raw["cross_aspiration_support"] = 0.0

    # 9. evidence_backing (resolved hypothesis support score)
    raw["evidence_backing"] = evidence_score(asp, resolved)

    # 10. deferred_readiness (+1.5 when a deferred goal becomes due)
    dr = 0
    deferred = goal.get("deferred_until")
    if deferred:
        try:
            dt = datetime.fromisoformat(str(deferred))
            if datetime.now() >= dt:
                dr = 1.5
        except (ValueError, TypeError):
            pass
    raw["deferred_readiness"] = dr

    # 11. context_coherence — same-category bonus. Context-pressure agnostic:
    # reusing already-primed category knowledge is always cheaper, regardless
    # of zone. Do not re-introduce zone modulation without profiling evidence.
    last_cat = wm.get("last_goal_category", "")
    category = _resolve_category(goal, asp)
    raw["context_coherence"] = 2.0 if category and last_cat and category == last_cat else 0

    # 12. skill_affinity (quality-weighted skill preference)
    # Reads meta/skill-quality.yaml for aggregate quality of the goal's linked skill.
    # High-quality skills get a boost; low-quality skills get a penalty.
    # Goals with no skill or unevaluated skills get neutral 0.
    skill = goal.get("skill", "")
    skill_name = skill.strip("/").split()[0] if skill else ""
    skill_quality_data = read_yaml_file(SKILL_QUALITY_PATH)
    sq_skills = skill_quality_data.get("skills", {})
    sq_entry = sq_skills.get(skill_name, {})
    sq_aggregate = sq_entry.get("aggregate", {})
    sq_overall = sq_aggregate.get("overall", 0.5)  # default neutral
    raw["skill_affinity"] = (sq_overall - 0.5) * 2  # maps [0,1] to [-1, +1]

    # 13b. directive_boost (cross-agent priority influence from board directives
    # PLUS the standing user directive in team-state strategic_focus, ).
    # Both are "someone with authority said this matters more"; they share the
    # criterion and its 1.5 weight rather than splitting into two knobs.
    raw["directive_boost"] = (
        directive_boost_score(goal.get("id", ""), category)
        + strategic_focus_boost(asp.get("id", ""), completion_ratio))

    # 13c. handoff_bonus (cross-agent handoff routing).
    # A planning/reviewer agent files implementer-targeted goals via
    # handoff_to="<target-agent>". The target's selector applies a positive
    # bonus; the sender's selector applies a penalty so those goals don't
    # loop back. Aging boost fires after warn_hours so stale handoffs
    # surface naturally.
    # NOTE: handoff_to is a ROUTING hint, not a visibility gate. Participants
    # still controls who CAN see the goal (see goal-schemas.md). raw value IS
    # the bonus (WEIGHTS["handoff_bonus"] = 1.0 — no scaling).
    raw["handoff_bonus"] = 0.0
    ht = goal.get("handoff_to")
    if ht:
        if ht == AGENT_NAME:
            raw["handoff_bonus"] = HANDOFF_CONFIG["handoff_bonus"]
            ch = goal.get("handoff_created_at")
            age = hours_since(ch) if ch else None
            if age is not None and age > HANDOFF_CONFIG["warn_hours"]:
                # Escalating bonus: +0.10 per 48h of age, capped at 2x (0.20 total addition)
                raw["handoff_bonus"] += min(age / 48, 2.0) * 0.10
        else:
            # Other agent's handoff — partner-liveness-gated penalty.
            # Heavy (handoff_sender_penalty, default -2.5) when the routed
            # partner is demonstrably alive. Decays linearly to 0 over
            # sender_decay_hours (default 4h) of measured PARTNER SILENCE —
            # NOT of elapsed wall-clock handoff age.
            #
            # Why the decay clock is silence, not handoff-age: a 3-day-old
            # handoff where bravo is still actively running means bravo is
            # working on it. The old pure-age decay let alpha take back
            # bravo's fresh work after 72h just because time passed. This
            # rebase makes the penalty derivable from observable state —
            # rb-324 (brittle tuned weight → derivable state, 2026-04-19).
            #
            # History: see rb-284 +  for why fresh penalty had to
            # be ≤-2.5 in the first place (lower values let priority +
            # recurring_urgency still rank other-agent handoffs #1).
            base = HANDOFF_CONFIG["handoff_sender_penalty"]
            decay_h = HANDOFF_CONFIG["sender_decay_hours"]
            active_floor_min = HANDOFF_CONFIG["partner_active_threshold_min"]

            team = _load_team_state_cached()
            partner_status = (team.get("agent_status") or {}).get(ht) or {}
            silence_h = hours_since(partner_status.get("last_active"))
            handoff_age_h = hours_since(goal.get("handoff_created_at"))

            # FAIL-OPEN on missing inputs: the sender penalty is a behavioral
            # control that requires BOTH partner silence AND handoff age to
            # compute. If either input is missing/corrupt, we can't derive
            # the decision from observable state — so the control does not
            # engage (penalty = 0). No fake-protection, no protective guess.
            # The active-partner branch below is NOT a fallback — it is the
            # feature working correctly when both inputs are valid.
            if silence_h is None or silence_h < 0 or handoff_age_h is None or handoff_age_h < 0:
                # INVARIANT — DO NOT REPLACE WITH `base`.
                # Fail-open is the spec: without both inputs we cannot
                # derive the penalty from observed state, so the control
                # disengages. Restoring the full penalty here would bring
                # back the brittle tuned-weight behavior rb-324 retired.
                raw["handoff_bonus"] = 0.0
            elif silence_h * 60 < active_floor_min:
                # Partner wrote to team-state within the active floor — alive.
                # Full base penalty protects them from take-back.
                raw["handoff_bonus"] = base
            else:
                # INVARIANT — DO NOT REMOVE THE min() CLAMP.
                # Decay cannot exceed handoff age. Without this clamp, a
                # long-silent partner sending a fresh handoff would let the
                # sender immediately take it back (silence_h >> age_h).
                # The clamp encodes "decay only counts silence that overlaps
                # with the handoff's own lifetime."
                effective_h = min(silence_h, handoff_age_h)

                if decay_h <= 0 or effective_h >= decay_h:
                    raw["handoff_bonus"] = 0.0
                else:
                    raw["handoff_bonus"] = base * (1.0 - effective_h / decay_h)

    # 13d. co_invest_alignment (): pair-iteration bias.
    # Bonus when this candidate's co_parent_id matches a partner's live
    # team-state in_flight.co_parent_id — biases the selector toward "pair
    # on the same parent right now." Schema: see core/config/conventions/
    # coordination.md → Co-Investigation Protocol. Reads use the cached
    # team-state; missing/empty fields produce 0.0 (no bonus).
    raw["co_invest_alignment"] = 0.0
    candidate_cpi = goal.get("co_parent_id")
    if candidate_cpi:
        team = _load_team_state_cached()
        agent_status = team.get("agent_status", {}) or {}
        for other_name, other_state in agent_status.items():
            if other_name == AGENT_NAME:
                continue
            in_flight = (other_state or {}).get("in_flight") or {}
            if in_flight.get("co_parent_id") == candidate_cpi:
                raw["co_invest_alignment"] = 1.0
                break

    # 13e. critical_blocker_surface (): boost candidates that ARE a
    # high-downstream-unlock bottleneck recorded in team-state.critical_blockers[]
    # (written by aspirations-consolidate Step 8.87, purged by
    # team-state-sync-blockers when resolved). Those entries are typically
    # "ready-unclaimed" EXECUTABLE goals that other work out-ranks; surfacing
    # THIS candidate when its id matches lets break-one-unlock-five fire instead
    # of the bottleneck coasting. Boost is proportional to downstream_count
    # normalized by downstream_cap (>= cap -> 1.0). DESIGN NOTE (logged for
    # bravo, the US-07 author): the read-only selector has no monotonic
    # iteration counter for a literal "every 25 iterations" cadence, and
    # critical_blockers[].updated_at tracks the consolidation WRITE, not the
    # block's age -- so the US-07 every-N-iterations cadence is implemented as a
    # PERSISTENT bounded boost that self-clears the moment team-state-sync-
    # blockers purges the resolved entry. Simpler, same intent (the bottleneck
    # surfaces until it resolves), one fewer piece of mutable state. Reads
    # cached team-state; missing/empty/non-list/malformed -> 0.0 (fail-open,
    # never blocks selection).
    raw["critical_blocker_surface"] = 0.0
    cbs_cfg = CRITICAL_BLOCKER_SURFACE_CONFIG
    if cbs_cfg.get("enabled"):
        raw["critical_blocker_surface"] = compute_critical_blocker_surface(
            goal.get("id"),
            _load_team_state_cached().get("critical_blockers"),
            cbs_cfg["min_downstream"],
            cbs_cfg["downstream_cap"],
        )

    # 13f. opportunity_boost ( restore): scoring teeth for the
    # standing pursue-opportunities user directive (ZDS meta-log 2026-06-18,
    #  signal). The original criterion was implemented prod-side in
    # ZDS-Mind and clobbered by the rb-498-era promotion — framework code
    # flows dev→prod, so prod-only code loses on every promote (guard-97/98
    # now forbid prod-side framework dev; this restores the criterion at the
    # dev origin). Opportunity-shaped goals: explicit sq-013
    # `discovery_type: opportunity` gets the full boost; Idea-primitive goals
    # (origin_signal `idea:` / title `Idea:` — CLAUDE.md defines Idea as
    # "creative insight, improvement opportunity") get half. Weight lives in
    # meta like every criterion; deployments tune it (ZDS ran 3.0, the
    # largest in its file; the dev seed default is deliberately modest).
    raw["opportunity_boost"] = 0.0
    if goal.get("discovery_type") == "opportunity":
        raw["opportunity_boost"] = 1.0
    elif (str(goal.get("origin_signal") or "").startswith("idea:")
          or str(goal.get("title") or "").startswith("Idea:")):
        raw["opportunity_boost"] = 0.5

    # 14. exploration_noise (random value scaled by developmental epsilon)
    raw["exploration_noise"] = random.random()

    # Weighted total — static criteria + dynamic exploration noise.
    #  (BRD Gap 8): utility-shaped scoring is a flag-gated SECOND path
    # (iaus_selector.use_iaus, default False). The additive sum stays the live
    # default; veto-by-zero is the utility path's primary behavioral win. The
    # exploration_noise term is added additively in BOTH paths so epsilon-greedy
    # exploration is unchanged (design section 2c). Reversible by a single flag
    # flip — no data migration, no schema change. A/B is the sibling .
    noise_weight = epsilon * noise_scale
    if IAUS_CONFIG["use_iaus"]:
        total = iaus_score(raw, WEIGHTS, IAUS_CONFIG)["score"]
        total += raw["exploration_noise"] * noise_weight
    else:
        # raw.get backstop (): load_weights() already filters
        # unknown keys, but a KNOWN criterion skipped by a future early-exit
        # path must degrade to 0-contribution, never KeyError selection dead.
        total = sum(raw.get(k, 0.0) * WEIGHTS[k] for k in WEIGHTS)
        total += raw["exploration_noise"] * noise_weight

    return {
        "goal_id": goal.get("id"),
        "aspiration_id": asp_id,
        "source": source,
        "title": goal.get("title", ""),
        # : expose cross-world provenance so consumers (aspirations-select
        # display, boot status) can badge foreign-injected goals as [foreign: <origin>]
        # instead of rendering them indistinguishably from native goals. None for
        # native goals -> no false badge.
        "cross_world_origin": goal.get("cross_world_origin"),
        # : preserve intended_agent + derive routed_to_me so the target
        # agent's LLM sees a cross-agent candidate is routed TO IT, not "someone
        # else's goal". By collect_cross_agent_candidates' strict-match contract
        # (intended_agent==agent_name), EVERY source='cross-agent:<owner>' candidate
        # is routed to the selecting agent — so a 'cross-agent' not-my-lane abstention
        # on it is ALWAYS wrong. Dropping the field made bravo abstain 13x from its
        # own HIGH-routed .
        "intended_agent": goal.get("intended_agent"),
        "routed_to_me": bool((source or "").startswith("cross-agent:")),
        "skill": goal.get("skill"),
        "category": category,
        "tags": _ensure_list(goal.get("tags")),
        "recurring": bool(goal.get("recurring")),
        "recurring_overdue_ratio": round(overdue_ratio, 3),
        "score": round(total, 2),
        "breakdown": {
            **{k: round(raw.get(k, 0.0) * WEIGHTS[k], 2) for k in WEIGHTS},
            "exploration_noise": round(raw["exploration_noise"] * noise_weight, 2),
        },
        "raw": {k: round(v, 2) if isinstance(v, float) else v for k, v in raw.items()},
        "exploration_params": {
            "epsilon": epsilon,
            "noise_scale": noise_scale,
            "noise_weight": round(noise_weight, 2),
        },
    }


_CELL_SIM_MODULE_CACHE = None


def _load_cell_sim_module():
    """importlib-load the hyphen-named cell-similarity.py once per process.

    Returns the module, or None if it cannot be loaded (fail-open: a missing or broken
    matcher just removes the cell-return boost, it never breaks selection). The False
    sentinel records a prior failed attempt so we do not re-pay the import cost on every
    call when the module is genuinely absent. Mirrors retrieve.py::_load_ppr_module
    (g-306-44). The matcher in turn loads the cell store (via its own _cells_module) and
    the KG+PPR substrate, so this one loader reaches the whole g-306-42/43/47/48 stack.
    """
    global _CELL_SIM_MODULE_CACHE
    if _CELL_SIM_MODULE_CACHE is not None:
        return _CELL_SIM_MODULE_CACHE or None
    try:
        import importlib.util
        sim_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "cell-similarity.py")
        spec = importlib.util.spec_from_file_location("cell_similarity", sim_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CELL_SIM_MODULE_CACHE = mod
        return mod
    except Exception:
        _CELL_SIM_MODULE_CACHE = False  # tried + failed; do not retry this process
        return None


def _cell_score_of(rec):
    """Best-trajectory score of a cell record, float-safe (-> 0.0 on any bad value)."""
    try:
        return float(rec.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _high_value_cell_seeds(sim, seed_top_n, *, cells_dir=None, agent=None):
    """Record-id entities of the top-N highest-value archived Go-Explore cells.

    These are the cells worth RETURNING to (Go-Explore return policy). Loads every
    category from the cell archive (through the matcher's own _cells_module loader so
    store access stays single-sourced), ranks records by descending score (tie-break by
    state_signature for determinism), takes the top seed_top_n, and unions their
    signature entities into the PPR seed set. Returns a SORTED unique list (sorted for
    determinism). Fail-open to [] when the archive is empty/unavailable (-> no boost,
    selection unchanged). cells_dir/agent default to None (the live per-agent archive);
    tests inject a tmp dir for hermetic, daemon-free runs.
    """
    cells = sim._cells_module()
    if cells is None:
        return []
    records = []
    try:
        for cat in cells.list_categories(agent=agent, cells_dir=cells_dir):
            for rec in cells.load_category(cat, agent=agent, cells_dir=cells_dir).values():
                records.append(rec)
    except Exception:
        return []
    if not records:
        return []
    records.sort(key=lambda r: (-_cell_score_of(r), str(r.get("state_signature", ""))))
    seeds = []
    for rec in records[:max(1, int(seed_top_n))]:
        seeds.extend(sim.signature_entities(rec.get("state_signature")))
    return sorted(dict.fromkeys(seeds))


def apply_cell_return_boost(scored, config, *, cells_dir=None, agent=None, graph_path=None):
    """Go-Explore cell-return boost: promote candidates near the highest-value cells.

    BRD Gap 17 child C (g-306-49). Flag-gated + boost-only, mirroring the g-306-44
    retrieve.py PPR blend:
      * config["enabled"] false -> EARLY RETURN, ``scored`` untouched. Selection is
        byte-identical to pre-g-306-49 by construction (the no-regression criterion).
      * enabled true -> seed Personalized PageRank from the top-N highest-value archived
        cells (the cells worth RETURNING to), then for each candidate add a bounded
        bonus = min(bonus_max, summed-PPR-mass * bonus_scale) over the candidate's
        record-id entities (HippoRAG passage score, 2405.14831). The bonus is always
        >= 0 (a sum of non-negative PPR masses) so the blend can only PROMOTE, never
        demote -- the same no-regression-by-construction property the PPR weight relies
        on.

    Deterministic: the matcher's score is a pure function of (candidate, archive,
    graph). Fail-open at every layer (missing matcher / empty archive / no graph signal
    -> no boost). Mutates and returns ``scored`` in place; records the bonus in each
    boosted candidate's breakdown + raw for telemetry. Same in-place + no-op-when-
    disabled contract as apply_substantive_demotion; candidate signature is read from
    the scored entry (goal_id + title + category), so it needs no extra inputs.
    cells_dir/agent/graph_path default to None (the live archive + default knowledge
    graph); tests inject tmp paths for hermetic, daemon-free runs.
    """
    if not config.get("enabled"):
        return scored
    sim = _load_cell_sim_module()
    if sim is None:
        return scored
    seeds = _high_value_cell_seeds(sim, config.get("seed_top_n", 5),
                                   cells_dir=cells_dir, agent=agent)
    if not seeds:
        return scored
    ppr_scores, _personalized = sim._ppr_scores_for(seeds, graph_path)
    if not ppr_scores:
        return scored
    bonus_scale = float(config.get("bonus_scale", 3.0))
    bonus_max = float(config.get("bonus_max", 1.5))
    enrich = bool(config.get("enrich_signature"))
    for s in scored:
        # Candidate signature: the goal's own id + title + category. The id is always a
        # record entity; titles routinely reference g-/rb-/guard- ids. score_cell sums
        # PPR mass over those entities -- the candidate's graph proximity to high-value
        # cells (the deterministic "matching" that drives the return).
        cand_sig = "{} {} {}".format(
            s.get("goal_id", ""), s.get("title", ""), s.get("category", ""))
        if enrich:
            # : the _extract_refs regex (knowledge-graph-build) only emits
            # rb-/guard-/g- record ids, so a bare category/tag never becomes an
            # entity -- a candidate's reliable graph footprint is just its leaf
            # goal-id, which is often absent from the periodically-rebuilt graph.
            # Inject the cat:/tag: pseudo-node entities DIRECTLY (the graph already
            # carries has_category/has_tag edges to them), so PPR mass from a
            # same-category high-value cell lands on cat:<category> and boosts
            # same-category candidates. Default-off until an A/B proves the
            # enriched signal helps (cell-return-ab-harness --enrich-signature).
            ents = list(sim.signature_entities(cand_sig))
            cat = s.get("category")
            if cat:
                ents.append("cat:" + str(cat))
            for t in (s.get("tags") or []):
                if t:
                    ents.append("tag:" + str(t))
            cell_score, overlap = sim.score_entities(ents, ppr_scores)
        else:
            cell_score, overlap = sim.score_cell({"state_signature": cand_sig}, ppr_scores)
        if cell_score <= 0:
            continue
        bonus = min(bonus_max, cell_score * bonus_scale)
        if bonus <= 0:
            continue
        s["score"] = round(s["score"] + bonus, 2)
        s.setdefault("breakdown", {})["cell_return_bonus"] = round(bonus, 2)
        s.setdefault("raw", {})["cell_return_ppr_mass"] = round(cell_score, 4)
        s["raw"]["cell_return_overlap"] = overlap
    return scored


def apply_substantive_demotion(scored, config):
    """FW-1 (2026-05-25, 7-agent feedback): bound recurring scores below substantive work.

    Six of seven agents reported recurring sweeps perpetually out-ranking rare
    substantive work (e.g. g-001-01: 175 runs, 98.8% routine, score 13.87, still
    #1). The existing knobs (urgency_max cap, recurring_saturation,
    per_goal_saturation, recurring_debt_bonus) each address a piece, but a healthy
    MIX of candidates still lets one recurring goal top the list because it also
    wins on priority / agent_executable / role_affinity.

    This caps any recurring goal's effective score to `substantive_demotion_margin`
    BELOW the best-scoring non-recurring, agent-executable candidate, UNLESS that
    recurring goal is overdue beyond `substantive_demotion_overdue_exempt_ratio`
    (genuinely-stale monitoring must still surface — monitoring must not rot).

    Pure w.r.t. `config`; mutates and returns `scored` in place. Records the
    adjustment in each demoted goal's breakdown + raw for telemetry. No-ops when
    disabled, when fewer than 2 candidates exist, when no agent-executable
    substantive candidate exists, or when the best substantive score is below
    `substantive_demotion_floor` (don't suppress maintenance for low-value
    stragglers). MUST run AFTER the recurring_debt_bonus block so the substantive
    floor already reflects catch-up boosts.
    """
    if not config.get("substantive_demotion_enabled"):
        return scored
    if len(scored) < 2:
        return scored
    margin = float(config["substantive_demotion_margin"])
    floor = float(config["substantive_demotion_floor"])
    exempt_ratio = float(config["substantive_demotion_overdue_exempt_ratio"])
    # "Substantive" = non-recurring AND executable by THIS agent (agent_executable
    # raw is 2 when eligible, 0 otherwise). Only protect work the agent can pick up.
    substantive = [
        s for s in scored
        if not s.get("recurring")
        and (s.get("raw") or {}).get("agent_executable", 0) > 0
    ]
    if not substantive:
        return scored
    top_sub = max(s["score"] for s in substantive)
    if top_sub < floor:
        return scored
    cap = round(top_sub - margin, 2)
    for s in scored:
        if (s.get("recurring")
                and s["score"] > cap
                and float(s.get("recurring_overdue_ratio", 0.0)) < exempt_ratio):
            s.setdefault("breakdown", {})["substantive_demotion"] = round(cap - s["score"], 2)
            s.setdefault("raw", {})["substantive_demotion_pre_score"] = s["score"]
            s["raw"]["substantive_demotion_applied"] = True
            s["score"] = cap
    return scored


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def write_scorer_verdict(scored, agent_dir):
    """Atomically record the scorer's top pick + top-5 to the per-agent session
    verdict sidecar (Scorer Sovereignty Layer B, g-115-2812).

    The claim chokepoint (aspirations-claim.sh -> scorer_verdict_gate.py) reads
    this file to refuse unsanctioned divergence from the scorer's top pick. This
    is a DETERMINISTIC-code writer (never the LLM). tempfile + os.replace gives
    an atomic swap so a concurrent reader never sees a half-written verdict
    (same durability pattern as the loop-state save).

    FAIL-OPEN: any error is swallowed to stderr — a verdict-write failure must
    never block selection output, and the claim gate independently fail-opens on
    a missing/stale verdict, so the worst case of a write failure is that the
    gate simply does not run this iteration.
    """
    if agent_dir is None or not scored:
        return
    try:
        session_dir = agent_dir / "session"
        session_dir.mkdir(parents=True, exist_ok=True)
        top = scored[0]
        verdict = {
            "top_goal_id": top.get("goal_id"),
            "top_score": round(float(top.get("score") or 0.0), 4),
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "top_5": [
                {"goal_id": s.get("goal_id"), "score": round(float(s.get("score") or 0.0), 4)}
                for s in scored[:5]
            ],
        }
        target = session_dir / "scorer-verdict.json"
        fd, tmp = tempfile.mkstemp(
            dir=str(session_dir), prefix=".scorer-verdict-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(verdict, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:  # pragma: no cover - defensive; write must never block
        print(f"[goal-selector] scorer-verdict write error "
              f"({type(e).__name__}: {e})", file=sys.stderr)


def cmd_select(args):
    """Score and rank all unblocked goals from both world and agent queues.

    Output: JSON array sorted by score desc, each entry tagged with source.
    """
    # Read from both aspiration queues
    world_aspirations = read_jsonl(WORLD_ASP_PATH)
    agent_aspirations = read_jsonl(AGENT_ASP_PATH) if AGENT_ASP_PATH else []

    if not world_aspirations and not agent_aspirations:
        print("[]")
        return

    # Load resolved hypotheses for evidence_backing
    pipeline = read_jsonl(PIPELINE_PATH)
    archive = read_jsonl(PIPELINE_ARCHIVE_PATH)
    resolved = [r for r in pipeline + archive
                if r.get("outcome") in ("CONFIRMED", "CORRECTED")]

    # Load working memory for variety/streak context
    wm = read_wm()
    # Cross-session sampling window ( /  drift fix).
    # Replaces in-session-only `wm.goals_completed_this_session` (which reset
    # every /stop) with a rolling window from journal + aspirations.
    # See alpha/reports/framework-vs-product-drift-2026-05-09.md.
    cb_window = int(CLASS_BALANCE_CONFIG.get("window_size", 20)) if CLASS_BALANCE_CONFIG else 20
    sc = load_recent_class_completions(window_size=cb_window)
    if not isinstance(sc, list):
        sc = []

    known_blockers = wm.get("slots", {}).get("known_blockers", [])
    if not isinstance(known_blockers, list):
        known_blockers = []

    # Build global done_ids across ALL aspirations for cross-aspiration dependency enforcement.
    # Without this, blocked_by references to goals in other aspirations are silently ignored.
    # (Mirrors the global goal_map approach already used by collect_blocked/trace_root_bottleneck.)
    all_aspirations = world_aspirations + agent_aspirations
    global_done_ids = set()
    global_live_ids = set()  # non-terminal goals — dependency-liveness check ()
    for asp in all_aspirations:
        if asp.get("status") != "active":
            continue
        for g in asp.get("goals", []):
            st = g.get("status")
            if st in ("completed", "decomposed"):
                global_done_ids.add(g["id"])
            if st not in TERMINAL_GOAL_STATUSES:
                global_live_ids.add(g["id"])

    # Load multi-agent coordination config from aspirations.yaml
    claim_timeout_hours = None
    reallocation_hours = None
    abstention_timeout_hours = None
    defer_reason_timeout_hours = None
    dependency_timeout_hours = None
    try:
        asp_config = read_yaml_file(CONFIG_DIR / "aspirations.yaml")
        ma = asp_config.get("multi_agent", {})
        if isinstance(ma, dict):
            cth = ma.get("claim_timeout_hours")
            if cth is not None:
                claim_timeout_hours = float(cth)
            rh = ma.get("reallocation_hours")
            if rh is not None:
                reallocation_hours = float(rh)
            ath = ma.get("abstention_timeout_hours")
            if ath is not None:
                abstention_timeout_hours = float(ath)
            drth = ma.get("defer_reason_timeout_hours")
            if drth is not None:
                defer_reason_timeout_hours = float(drth)
            dth = ma.get("dependency_timeout_hours")
            if dth is not None:
                dependency_timeout_hours = float(dth)
    except Exception:
        pass

    # Collect candidates from both queues
    candidates = collect_candidates(
        world_aspirations, known_blockers=known_blockers, source="world",
        global_done_ids=global_done_ids, claim_timeout_hours=claim_timeout_hours,
        reallocation_hours=reallocation_hours,
        abstention_timeout_hours=abstention_timeout_hours,
        defer_reason_timeout_hours=defer_reason_timeout_hours,
        dependency_timeout_hours=dependency_timeout_hours,
        global_live_ids=global_live_ids)
    candidates += collect_candidates(
        agent_aspirations, known_blockers=known_blockers, source="agent",
        global_done_ids=global_done_ids, reallocation_hours=reallocation_hours,
        abstention_timeout_hours=abstention_timeout_hours,
        defer_reason_timeout_hours=defer_reason_timeout_hours,
        dependency_timeout_hours=dependency_timeout_hours,
        global_live_ids=global_live_ids)
    #  — cross-agent stranding fix. Pull goals from sibling agent
    # queues where intended_agent matches AGENT_NAME. Catches goals filed by
    # another agent (capability-route gate stamps intended_agent on add) that
    # landed in the FILER's private queue and were invisible to the TARGET.
    # Strict-match contract: only intended_agent == AGENT_NAME pulls; "either"
    # and unset stay in their owner's queue. Fail-open per sibling.
    # Gated by cross_agent_surfacing.enabled (ENABLED 2026-07-15, ).
    # See load_cross_agent_surfacing_enabled() — the execution path is wired
    # (select Phase 2.95 split + execute Phase 4 owner env-prefix), so a surfaced
    # sibling-queue goal claims under the owner's identity, no 404 ( resolved).
    if AGENT_DIR is not None and CROSS_AGENT_SURFACING_ENABLED:
        candidates += collect_cross_agent_candidates(
            AGENT_DIR.parent, AGENT_DIR, AGENT_NAME,
            known_blockers=known_blockers,
            global_done_ids=global_done_ids,
            claim_timeout_hours=claim_timeout_hours,
            reallocation_hours=reallocation_hours,
            abstention_timeout_hours=abstention_timeout_hours,
            defer_reason_timeout_hours=defer_reason_timeout_hours,
            dependency_timeout_hours=dependency_timeout_hours,
            global_live_ids=global_live_ids)
    if not candidates:
        # VERIFY-BEFORE-ASSUMING (): all_blocked is a NEGATIVE,
        # work-gating conclusion ("no executable goals exist"). The WORLD
        # aspirations file is on a synced network drive (OneDrive); a transient
        # stale/partial snapshot during sync can produce a valid-but-empty FIRST
        # collection while real candidates exist (observed alpha session-77: 124
        # candidates present, selector intermittently emitted all_blocked; load +
        # collect proven deterministic 8/8 + 12/12 on the settled file). Per
        # .claude/rules/verify-before-assuming.md a negative work-gating
        # conclusion requires 2+ independent signals -- re-read fresh and
        # re-collect ONCE before declaring all-blocked. If the retry finds
        # candidates the first pass was a transient anomaly: proceed with the
        # retry results (recomputing all_aspirations + global_done_ids from the
        # fresh read) and log the discrepancy for root-cause evidence. The three
        # collect_* calls below intentionally MIRROR the initial collection above
        # -- keep them in sync; test_goal_selector_allblocked_reread guards this.
        world_retry = read_jsonl(WORLD_ASP_PATH)
        agent_retry = read_jsonl(AGENT_ASP_PATH) if AGENT_ASP_PATH else []
        all_aspirations_retry = world_retry + agent_retry
        global_done_ids_retry = set()
        global_live_ids_retry = set()  # non-terminal goals ()
        for asp in all_aspirations_retry:
            if asp.get("status") != "active":
                continue
            for g in asp.get("goals", []):
                st = g.get("status")
                if st in ("completed", "decomposed"):
                    global_done_ids_retry.add(g["id"])
                if st not in TERMINAL_GOAL_STATUSES:
                    global_live_ids_retry.add(g["id"])
        retry_candidates = collect_candidates(
            world_retry, known_blockers=known_blockers, source="world",
            global_done_ids=global_done_ids_retry, claim_timeout_hours=claim_timeout_hours,
            reallocation_hours=reallocation_hours,
            abstention_timeout_hours=abstention_timeout_hours,
            defer_reason_timeout_hours=defer_reason_timeout_hours,
            dependency_timeout_hours=dependency_timeout_hours,
            global_live_ids=global_live_ids_retry)
        retry_candidates += collect_candidates(
            agent_retry, known_blockers=known_blockers, source="agent",
            global_done_ids=global_done_ids_retry, reallocation_hours=reallocation_hours,
            abstention_timeout_hours=abstention_timeout_hours,
            defer_reason_timeout_hours=defer_reason_timeout_hours,
            dependency_timeout_hours=dependency_timeout_hours,
            global_live_ids=global_live_ids_retry)
        # Gated (see first call site + load_cross_agent_surfacing_enabled()).
        if AGENT_DIR is not None and CROSS_AGENT_SURFACING_ENABLED:
            retry_candidates += collect_cross_agent_candidates(
                AGENT_DIR.parent, AGENT_DIR, AGENT_NAME,
                known_blockers=known_blockers,
                global_done_ids=global_done_ids_retry,
                claim_timeout_hours=claim_timeout_hours,
                reallocation_hours=reallocation_hours,
                abstention_timeout_hours=abstention_timeout_hours,
                defer_reason_timeout_hours=defer_reason_timeout_hours,
                dependency_timeout_hours=dependency_timeout_hours,
                global_live_ids=global_live_ids_retry)
        if retry_candidates:
            _log_transient_allblocked_recovery(
                world_aspirations, world_retry, len(retry_candidates))
            candidates = retry_candidates
            all_aspirations = all_aspirations_retry
            global_done_ids = global_done_ids_retry
            global_live_ids = global_live_ids_retry
        else:
            # Two independent signals agree: genuinely all-blocked.
            # (Report blocked goals from the fresh retry read for consistency.)
            blocked = collect_blocked(all_aspirations_retry, known_blockers=known_blockers,
                                      global_done_ids=global_done_ids_retry,
                                      defer_reason_timeout_hours=defer_reason_timeout_hours,
                                      dependency_timeout_hours=dependency_timeout_hours,
                                      global_live_ids=global_live_ids_retry)
            if blocked:
                summary = {}
                for b in blocked:
                    reason = b["block_reason"]
                    summary[reason] = summary.get(reason, 0) + 1
                print(json.dumps({
                    "candidates": [],
                    "all_blocked": True,
                    "blocked_count": len(blocked),
                    "by_reason": summary,
                    "blocked_goals": [
                        {"goal_id": b["goal_id"], "title": b["title"],
                         "reason": b["block_reason"],
                         "detail": b.get("block_detail", "")}
                        for b in blocked[:10]
                    ]
                }, indent=2))
            else:
                print("[]")
            return

    # Per-runner capability filtering happens at COLLECTION time (
    # Slice 2): collect_candidates skips locally-unexecutable goals and
    # collect_blocked classifies them not_my_lane (same cached runner_caps, so
    # the two stay exact inverses). A fully capability-constrained box therefore
    # returns 0 candidates -> the re-read/all_blocked path ABOVE emits the
    # not_my_lane blocks -> aspirations-select routes to quiescence sleep instead
    # of hot-looping. No post-collection candidate filter is needed here; the
    # mixed-queue case already has the unexecutable goals skipped from `candidates`.
    epsilon, noise_scale = load_exploration_params()
    # Precompute completion ratios for cross_aspiration_support criterion.
    # Origin: LifingPolls plan item 2 (2026-05-08). Builds a dict of
    # {asp_id: completion_ratio} so score_goal can read it without
    # re-walking aspirations per candidate.
    global _ASP_COMPLETION_RATIOS
    _ASP_COMPLETION_RATIOS = {}
    for asp in all_aspirations:
        # Census-augmented (B9-deep): same "active" semantics as completion_pressure.
        total, done = effective_counts(asp, exclude_statuses=ABANDONED_STATUSES)
        _ASP_COMPLETION_RATIOS[asp.get("id", "")] = (
            done / total if total > 0 else 0.0)
    scored = [score_goal(c, wm, resolved, sc, epsilon=epsilon, noise_scale=noise_scale)
              for c in candidates]

    # Recurring debt recovery: when most candidates are recurring (agent recovering from
    # a gap), boost non-recurring goals so real work gets oxygen during catch-up.
    recurring_count = sum(1 for s in scored if s.get("recurring"))
    debt_threshold = RECURRING_CONFIG["debt_threshold"]
    if len(scored) >= 5 and recurring_count / len(scored) > debt_threshold:
        bonus = RECURRING_CONFIG["debt_bonus"]
        for s in scored:
            if not s.get("recurring"):
                s["score"] += bonus
                s["breakdown"]["recurring_debt_bonus"] = bonus
                s["raw"]["recurring_debt_bonus"] = bonus

    # FW-1 (2026-05-25): bound recurring goals below the best substantive
    # candidate when real work is available. Runs AFTER recurring_debt_bonus so
    # the substantive floor already reflects catch-up boosts; BEFORE the sort so
    # the demoted scores drive the ranking. See apply_substantive_demotion.
    apply_substantive_demotion(scored, RECURRING_CONFIG)

    # Go-Explore cell-return boost (): flag-gated, boost-only, DEFAULT OFF.
    # No-op + byte-identical selection when cell_return.enabled is false. Runs AFTER
    # substantive_demotion so the bonus reflects the post-demotion baseline, and BEFORE
    # the sort so the boost drives ranking (same placement rationale as the
    # recurring_debt_bonus / substantive_demotion passes above).
    apply_cell_return_boost(scored, CELL_RETURN_CONFIG)

    # Sort: highest score first, then lower aspiration number, then lower goal number
    scored.sort(key=lambda x: (-x["score"], x["aspiration_id"], x["goal_id"]))

    # : log meta-strategy application. Proof-of-concept that the
    # goal-selection-strategy.yaml WAS consulted during this iteration —
    # surfaces "which meta-strategies actually fire" telemetry that's
    # invisible from the strategy file alone. Other meta-strategy consumers
    # (reflection-strategy, encoding-strategy, etc.) can follow the same
    # pattern: import _record_strategy_application + call near decision point.
    _record_strategy_application(META_GOAL_SELECTION, {
        "skill": "goal-selector.cmd_select",
        "candidates_scored": len(scored),
        "top_score": round(float(scored[0]["score"]), 2) if scored else None,
        "top_goal_id": scored[0]["goal_id"] if scored else None,
    })

    # Scorer-verdict sidecar (Scorer Sovereignty Layer B, ): record the
    # top pick + top-5 so the claim chokepoint can refuse unsanctioned divergence
    # from the scorer's top pick. Deterministic writer, fail-open. Placed BEFORE
    # the banner so it can never disturb the pinned emit_directive_honor_banner
    # call site ( / test_goal_selector_directive_honor_banner.py).
    write_scorer_verdict(scored, AGENT_DIR)

    # DIRECTIVE-HONOR banner (guard-1310, ): stderr-only so the stdout
    # JSON the orchestrator parses is untouched. Fires when an unacked directive
    # directed at THIS agent targets a goal present in `scored`. Runs every
    # iteration -> compaction cannot summarize it away (the Phase 2.07 LLM path
    # can). Fail-open inside the helper; wrap defensively so a banner bug can
    # never suppress the ranked-candidate output.
    try:
        emit_directive_honor_banner(scored, AGENT_NAME)
    except Exception as e:  # pragma: no cover - defensive; banner must never block
        print(f"[goal-selector] directive-honor banner error "
              f"({type(e).__name__}: {e})", file=sys.stderr)

    print(json.dumps(scored, indent=2, ensure_ascii=False))


def cmd_blocked(args):
    """List all blocked goals with reasons. Output: JSON with blocked_goals and by_reason."""
    empty_reasons = {r: {"count": 0, "goal_ids": []} for r in
                      ["infrastructure", "dependency", "deferred", "hypothesis_gate", "explicit_status"]}
    empty_reasons["dependency"]["head_count"] = 0
    empty_reasons["dependency"]["downstream_count"] = 0

    # Load expiry config (same source as cmd_select)
    defer_reason_timeout_hours = None
    dependency_timeout_hours = None
    try:
        asp_config = read_yaml_file(CONFIG_DIR / "aspirations.yaml")
        ma = asp_config.get("multi_agent", {})
        if isinstance(ma, dict):
            drth = ma.get("defer_reason_timeout_hours")
            if drth is not None:
                defer_reason_timeout_hours = float(drth)
            dth = ma.get("dependency_timeout_hours")
            if dth is not None:
                dependency_timeout_hours = float(dth)
    except Exception:
        pass

    # Read from both aspiration queues
    world_aspirations = read_jsonl(WORLD_ASP_PATH)
    agent_aspirations = read_jsonl(AGENT_ASP_PATH) if AGENT_ASP_PATH else []
    aspirations = world_aspirations + agent_aspirations
    if not aspirations:
        print(json.dumps({"blocked_goals": [], "by_reason": empty_reasons,
            "bottlenecks": [], "summary": {
            "total_blocked": 0, "total_active_goals": 0,
            "bottleneck_count": 0}}, indent=2))
        return

    wm = read_wm()
    known_blockers = wm.get("slots", {}).get("known_blockers", [])
    if not isinstance(known_blockers, list):
        known_blockers = []

    # Build global done_ids + live_ids for cross-aspiration dependency resolution
    global_done_ids = set()
    global_live_ids = set()  # non-terminal goals — dependency-liveness check ()
    for asp in aspirations:
        if asp.get("status") != "active":
            continue
        for g in asp.get("goals", []):
            st = g.get("status")
            if st in ("completed", "decomposed"):
                global_done_ids.add(g["id"])
            if st not in TERMINAL_GOAL_STATUSES:
                global_live_ids.add(g["id"])

    blocked = collect_blocked(aspirations, known_blockers=known_blockers,
                              global_done_ids=global_done_ids,
                              defer_reason_timeout_hours=defer_reason_timeout_hours,
                              dependency_timeout_hours=dependency_timeout_hours,
                              global_live_ids=global_live_ids)

    # Count total non-terminal goals across active aspirations
    total_active = 0
    for asp in aspirations:
        if asp.get("status") != "active":
            continue
        for g in asp.get("goals", []):
            if g.get("status") not in ("completed", "skipped", "expired", "decomposed"):
                total_active += 1

    # Group by reason
    reasons = ["infrastructure", "dependency", "deferred", "hypothesis_gate", "explicit_status"]
    by_reason = {}
    for reason in reasons:
        matches = [e for e in blocked if e["block_reason"] == reason]
        entry = {"count": len(matches), "goal_ids": [e["goal_id"] for e in matches]}
        if reason == "dependency":
            entry["head_count"] = sum(1 for e in matches if e.get("chain_position") == "head")
            entry["downstream_count"] = sum(1 for e in matches if e.get("chain_position") == "downstream")
        by_reason[reason] = entry

    # --- Root bottleneck tracing ---
    # Global goal map + done_ids (NOT per-aspiration) — chains cross aspirations
    goal_map = {}
    all_done_ids = set()
    for asp in aspirations:
        if asp.get("status") != "active":
            continue
        for g in asp.get("goals", []):
            gid = g.get("id", "")
            goal_map[gid] = {
                "status": g.get("status", ""),
                "blocked_by": g.get("blocked_by", []),
                "skill": g.get("skill"),
                "deferred_until": g.get("deferred_until"),
                "participants": g.get("participants"),
                "title": g.get("title", ""),
                "aspiration_id": asp.get("id", ""),
            }
            if g.get("status") in ("completed", "decomposed"):
                all_done_ids.add(gid)

    # Build blocker_by_skill and blocker_by_category for INFRA classification
    blocker_by_skill = {}
    blocker_by_category = {}
    if known_blockers:
        for b in known_blockers:
            if b.get("resolution") is None:
                for skill in b.get("affected_skills", []):
                    blocker_by_skill[skill] = b
                for cat in b.get("affected_categories", []):
                    blocker_by_category[cat] = b

    # Trace root bottleneck for each blocked goal
    for entry in blocked:
        gid = entry["goal_id"]
        if entry["block_reason"] == "dependency":
            # Follow the chain to its root
            root_id, cause = trace_root_bottleneck(
                gid, goal_map, all_done_ids, blocker_by_skill, blocker_by_category)
            entry["root_bottleneck"] = {"goal_id": root_id, "cause": cause}
        else:
            # Non-dependency blocks: root is self, cause from block_detail
            cause_map = {
                "infrastructure": entry.get("block_detail", "INFRA"),
                "deferred": "DEFERRED until {t}".format(
                    t=entry.get("deferred_until", "?")),
                "hypothesis_gate": entry.get("block_detail", "hypothesis gate"),
                "explicit_status": entry.get("block_detail", "explicit block"),
            }
            entry["root_bottleneck"] = {
                "goal_id": gid,
                "cause": cause_map.get(entry["block_reason"], entry["block_reason"]),
            }

    # Group by root bottleneck → build bottlenecks array
    root_groups = {}
    for entry in blocked:
        root_id = entry["root_bottleneck"]["goal_id"]
        if root_id not in root_groups:
            root_info = goal_map.get(root_id, {})
            root_groups[root_id] = {
                "goal_id": root_id,
                "title": root_info.get("title", entry.get("title", "")),
                "aspiration_id": root_info.get("aspiration_id",
                                               entry.get("aspiration_id", "")),
                "cause": entry["root_bottleneck"]["cause"],
                "downstream_ids": [],
                "affected_aspirations": set(),
            }
        group = root_groups[root_id]
        if entry["goal_id"] != root_id:
            group["downstream_ids"].append(entry["goal_id"])
        group["affected_aspirations"].add(entry["aspiration_id"])

    bottlenecks = []
    for root_id, group in root_groups.items():
        bottlenecks.append({
            "goal_id": group["goal_id"],
            "title": group["title"],
            "aspiration_id": group["aspiration_id"],
            "cause": group["cause"],
            "downstream_count": len(group["downstream_ids"]),
            "downstream_ids": group["downstream_ids"],
            "affected_aspirations": sorted(group["affected_aspirations"]),
        })
    bottlenecks.sort(key=lambda b: -b["downstream_count"])

    result = {
        "blocked_goals": blocked,
        "by_reason": by_reason,
        "bottlenecks": bottlenecks,
        "summary": {
            "total_blocked": len(blocked),
            "total_active_goals": total_active,
            "bottleneck_count": len(bottlenecks),
        },
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Goal scoring with exploration noise")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("select", help="Score and rank all unblocked goals")
    sub.add_parser("blocked", help="List all blocked goals with reasons")
    args = parser.parse_args()
    {"select": cmd_select, "blocked": cmd_blocked}[args.command](args)


if __name__ == "__main__":
    main()
