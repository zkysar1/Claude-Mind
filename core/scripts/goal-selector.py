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

  recurring_urgency: base + log2(1 + overdue_ratio) * log_scale, THEN CLAMPED to
    urgency_max (g-115-1090). "no cap" stood here until 2026-07-30 and was false
    from the day the clamp landed; it is the sentence that keeps sending readers
    looking elsewhere for why heavily-overdue goals do not rise. MEASURED at the
    shipped defaults (base 1.5, log_scale 1.5, urgency_max 4.0): the clamp binds at
    overdue_ratio 2.175, i.e. age = 3.17x interval. The starvation detector's own
    threshold is 2.0x, so the clamp binds 0.175 ABOVE the ratio that DEFINES a goal
    as starved — past that point this term carries ZERO information about how
    overdue a goal is. Live on 527 candidates (zeta, cc-02, 2026-07-30): 11 of 12
    recurring rows with ratio >= 2.0 sat at exactly 4.0, spanning 2.12x .. 97.85x
    (a 46x spread) for one identical urgency. See g-115-4103 / g-115-4047.
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
                    ENVIRONMENT_ID, agents_root as _agents_root, read_agent_conf)
from _fileops import locked_modify_yaml  # noqa: E402  ( applications_log)
from wm import read_wm  # noqa: E402
# : the board-routing-tag rule lives in peer_surface (the same module
# that owns split_author) so directive-honor here, insight-trigger-gate, and the
# sweep cannot drift apart about what `agent@env-id` means.
from peer_surface import (parse_routing_tag,  # noqa: E402
                          routing_tag_targets_agent)
from cadence_signals import evaluate_cadence_signal  # noqa: E402  ( signal-gated cadence)
# Single source of truth for terminal goal statuses — see aspirations.py.
# Derived sets below (SKIP_STATUSES, ABANDONED_STATUSES) stay consistent if a new
# status is added to TERMINAL_GOAL_STATUSES.
from aspirations import (TERMINAL_GOAL_STATUSES, STRUCTURED_DEFER_PREFIXES,  # noqa: E402
                         routes_away_from)
from _goal_census import effective_counts  # noqa: E402  (B9-deep census-augmented counts)
from _iaus_scorer import iaus_score  # noqa: E402  ( flagged utility scorer)
from _runner_capabilities import (  # noqa: E402  ( per-runner capability filter)
    derive_runner_capabilities, box_config_from_conf, merge_capability_config,
    goal_is_locally_executable, goal_required_capabilities)
from _drain_title import is_drain_action_title  # noqa: E402  ( owner-scope drain SSOT)
from _dependency_graph import supersession_satisfied_ids  # noqa: E402  ( SSOT, guard-547)
SKIP_STATUSES = TERMINAL_GOAL_STATUSES | {"in-progress"}              # not selectable
ABANDONED_STATUSES = TERMINAL_GOAL_STATUSES - {"completed"}            # terminal but not "done"


def goal_record_id(asp, g):
    """The id of goal ``g`` of aspiration ``asp``, or None for a record that has none —
    warned once on stderr, never raised. A goal may be a bare string ref (a legacy shape
    aspirations-read --active-compact expands) or a malformed dict: measured 2026-08-29,
    a Body rewrote asp-002's ``goals`` as ``{"goal_id", "status"}`` stubs and every
    ``select`` on the fleet died on ``KeyError: 'id'`` until the records were restored.
    One bad record must cost one warning, not the whole fleet's selection."""
    if isinstance(g, dict):
        gid = g.get("id")
        if isinstance(gid, str) and gid:
            return gid
    key = (asp.get("id") if isinstance(asp, dict) else None, id(g))
    if key not in _MALFORMED_GOALS_WARNED:
        _MALFORMED_GOALS_WARNED.add(key)
        shape = ("string ref" if isinstance(g, str)
                 else f"dict without id (keys: {sorted(g.keys())[:6]})" if isinstance(g, dict)
                 else type(g).__name__)
        print(f"[goal-selector] WARN: {asp.get('id') if isinstance(asp, dict) else '?'} "
              f"has a goal record with no id ({shape}) — skipped; the record needs "
              f"repair (aspirations-update-goal.sh cannot address it)", file=sys.stderr)
    return None


_MALFORMED_GOALS_WARNED = set()


def global_goal_id_sets(aspirations):
    """``(done_ids, live_ids)`` across every ACTIVE aspiration in ``aspirations`` — the
    cross-aspiration dependency sets (g-115-1344). The one builder for the selection
    pass, its all-blocked retry, and the ``blocked`` diagnostic, so the three cannot
    disagree; goal records without an id are skipped with a warning
    (:func:`goal_record_id`)."""
    done_ids = set()
    live_ids = set()  # non-terminal goals — dependency-liveness check ()
    for asp in aspirations:
        if asp.get("status") != "active":
            continue
        for g in asp.get("goals", []) or []:
            gid = goal_record_id(asp, g)
            if gid is None:
                continue
            st = g.get("status")
            if st in ("completed", "decomposed"):
                done_ids.add(gid)
            if st not in TERMINAL_GOAL_STATUSES:
                live_ids.add(gid)
    return done_ids, live_ids


def expand_done_ids_via_supersession(aspirations, done_ids):
    """Return `done_ids` plus every id whose supersession chain ends completed.

    `done_ids` is a flat SET and every dependency check in this module is set
    membership (`bid not in done_ids`), which cannot follow a pointer. So a
    duplicate closed `skipped` with its work MOVED to another goal reads as
    NOT-done forever, and every dependent stays frozen. Measured 2026-08-26:
    that exact read re-deferred two just-unblocked goals and left zero goals
    selectable across 1,400 ranked for ~4h.

    STRICTLY ADDITIVE — a union, never a difference. Nothing that was
    selectable before can become blocked by this call, so the failure
    direction is "still frozen", which is the pre-existing behaviour, never
    "wrongly unfrozen by removing a check". The resolver behind it satisfies
    ONLY on a chain that reaches `completed`; `open` / `unknown` / `cycle` all
    decline (see `_dependency_graph.supersession_satisfied_ids`).

    Called at each `global_done_ids` build site rather than at the two
    `blocked_by` read sites, deliberately: collect_candidates and
    collect_blocked MUST agree on done-ness or a goal appears unblocked in
    selection and blocked in diagnostics, and both already read whatever this
    one set contains. One expansion point keeps that symmetry structural
    instead of remembered.

    Live effect when wired (2026-08-27, 3,182 goals): 10 ids added, 0 live
    goals changed their unmet-dep set — a latent fix, not a queue reshuffle.
    """
    index = {}
    for asp in aspirations:
        if asp.get("status") != "active":
            continue
        for g in asp.get("goals", []) or []:
            gid = g.get("id")
            if gid:
                index[gid] = g
    return done_ids | supersession_satisfied_ids(index, already_done=done_ids)

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


def _is_handoff_gated_defer(goal):
    """A STRUCTURED defer on a goal that is ROUTED ELSEWHERE ().

    The 120h fail-open below is a re-probe window for defers whose premise is a
    WORLD CONDITION — "is the service up yet?" — and Phase 0.5b re-probes those
    independently, so expiring the window is harmless. It is WRONG for a defer
    whose premise is about WHO MAY ACT: a re-probe of a routing gate returns
    "still true" forever, so the goal can only ever leave the deferred state by
    the TIMER, and the timer hands it to whichever Body ranks it next — which is
    precisely the Body the routing said must not act.

    Measured (g-326-184, cc-07 2026-08-25T17:37): defer_reason
    "precondition_unmet:studio_session_required", handoff_to=foxtrot,
    intended_agent=alpha; it fail-opened at TTL to rank #1 of 1399 for
    alpha-on-cc-07. Both halves — fail-open AND misroute — are visible in that
    one record, and `handoff_to` is what distinguishes it.

    Deliberately NARROW. It keys on handoff_to, so it covers routing-gated
    defers ONLY. A sibling class exists whose premise is the BOX or the tool
    policy rather than the lane (g-115-6259: "git cherry-pick is refused by
    tool-permission policy on this box") and carries no handoff_to; that is NOT
    covered here, on purpose — one measured instance supports one predicate, and
    widening to "execution-context defers never fail open" would price the claim
    off a conjunct nothing has measured (rb-2572).

    Reinforces guard-2983: a re-route that lives only in prose is invisible to
    the selector and the goal boomerangs back. This makes the routing structural.
    """
    defer = (goal.get("defer_reason") or "")
    if not defer.lower().startswith(_STRUCTURED_DEFER_PREFIXES_LOWER):
        return False
    return bool(str(goal.get("handoff_to") or "").strip())



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

# Body identity ( part 1). AGENT_NAME names the MIND; under the
# Mind/Body split several Bodies (separate sessions, possibly separate boxes)
# share one mind key, so a name-only claim comparison cannot tell "mine" from
# "my sibling's". The session id is the Body discriminator. Empty string when
# MIND_SID is unset, which makes every Body-aware check below fail OPEN to the
# pre-split behavior rather than guessing.
BODY_SID = os.environ.get("MIND_SID", "") or ""

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


def _agent_is_resident():
    """True when MIND_AGENT names an agent CONFIGURED ON THIS BOX.

    g-115-5850, direction (d). `goal-selector.sh select` is not read-only: it
    writes `session/drain-lane-state.json` (the drain-lane cadence counter),
    `session/scorer-verdict.json` (the input the claim chokepoint gates on), and
    an `applications_log` entry attributed to MIND_AGENT. Every one of those
    lands in whatever agent MIND_AGENT names — so the fleet-vantage recipe the
    standing directive prescribes for its own exit condition
    (`MIND_AGENT=<name> goal-selector.sh` once per live agent) FABRICATES
    selector state for each partner as a side effect of measuring them. Measured
    on cc-02 (`uname -r` 6.8.0-137-generic) 2026-08-13: zeta is the ONLY agent
    with a `local-paths.conf` here, yet alpha, bravo, echo and foxtrot each
    carried both files, all stamped 2026-08-11T20:23-20:25 — the exact timestamp
    of the probe this goal's own description records as its second instance.

    RESIDENCE, not liveness, is the right predicate: an agent is resident when
    its dir carries a `local-paths.conf`, the same signal `_paths.
    enumerate_agent_confs` uses to identify a configured agent on this machine
    (and `inbound-reference-census._resident_agents` for the same purpose —
    though that helper unconditionally adds the bound MIND_AGENT, which makes
    the bound agent resident BY CONSTRUCTION and is exactly the clause that
    cannot be reused here).

    Who NEWLY gets refused, enumerated against live state per guard-1562: on a
    box hosting N agents all N have a conf, so every agent's own loop writes
    exactly as before, and the ONLY caller that changes behaviour is an explicit
    `MIND_AGENT=<other>` cross-agent probe. No-regression by construction.

    Fail-OPEN in both degenerate directions — `AGENT_DIR is None` (the writes
    already no-op there) and any stat error return True, so a plumbing fault
    restores the pre-fix behaviour rather than silently disabling a real agent's
    scorer verdict. Suppressing a write nobody asked for is cheap; suppressing a
    resident agent's claim-gate input would wedge its next claim.
    """
    if AGENT_DIR is None:
        return True
    try:
        return (AGENT_DIR / "local-paths.conf").exists()
    except OSError:
        return True


def _suppress_cross_agent_write(what):
    """Emit the one-line stderr reason for a suppressed cross-agent-probe write.

    Loud on purpose (guard-1977): a check that declines to act and says nothing
    reports success by default, and its only observable state is silence — so a
    reader cannot tell "suppressed correctly" from "never ran".
    """
    print(f"[goal-selector] {what} write SUPPRESSED — MIND_AGENT={AGENT_NAME!r} "
          f"is not resident on this box (no agents/{AGENT_NAME}/local-paths.conf); "
          f"cross-agent probe must not fabricate a partner's selector state "
          f"(g-115-5850)", file=sys.stderr)


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
    # (d): the entry is stamped with `agent` from the environment, so
    # under a cross-agent probe it asserts that the PROBED agent consulted this
    # strategy — a falsely-attributed telemetry row in a SHARED meta store, one
    # per partner per fleet-vantage run. Guarded inside the helper rather than at
    # the call site precisely because the docstring above invites other
    # meta-strategy consumers to reuse it; they inherit the protection.
    if not _agent_is_resident():
        _suppress_cross_agent_write("applications_log")
        return
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
        # : interval-scoped exemption for monitor-class recurring goals.
        # These MUST be listed here — the loop below iterates `defaults` as an
        # ALLOWLIST, so a key present in aspirations.yaml but absent from this dict
        # is silently discarded with no parse error and no warning. Values mirror
        # the shipped aspirations.yaml so behavior is identical whether or not a
        # given deployment's YAML carries the keys.
        "substantive_demotion_short_interval_hours": 6.0,
        "substantive_demotion_short_interval_exempt_ratio": 1.0,
        #  drain lane. Listed here for the SAME allowlist reason as the
        # two keys above: the loop below iterates `defaults`, so a key present in
        # aspirations.yaml but absent from this dict is silently discarded — the
        # K knob would read as its default forever with no parse error.
        "drain_lane_enabled": True,
        "drain_lane_interval_iterations": 5,
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


def load_starvation_boost_config():
    """Load anti-starvation boost params from core/config/aspirations.yaml.

    g-115-5426 (2026-08-10). Params for apply_starvation_boost, the post-scoring
    pass that rescues aged, unclaimed, HIGH-priority NON-recurring goals from
    indefinite selection starvation. Recurring goals already have recurring_urgency
    (overdue_ratio); one-shot goals had NO age-based term, so a one-shot goal's
    score is essentially fixed at filing time and a lone HIGH goal in a sprawling,
    low-completion aspiration never rises. DEFAULT ON, but no-regression by
    construction: a goal younger than min_age_hours gets ZERO boost, so normal
    selection is byte-identical for all but the genuine starvation population.
    Same overlay/type-coerce shape as the sibling config loaders; priority_multipliers
    (a nested dict) is merged key-by-key rather than type-coerced whole.
    """
    defaults = {
        "enabled": True,
        "min_age_hours": 12.0,
        "full_boost_age_hours": 36.0,
        "max_boost": 4.0,
    }
    priority_multipliers = {"HIGH": 1.0, "MEDIUM": 0.0, "LOW": 0.0}
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", Path(__file__).parent / "_config_overlay.py"
        )
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        asp_config = overlay.merged_config("aspirations.yaml")
        sb = asp_config.get("starvation_boost", {})
        if isinstance(sb, dict):
            for k, default in defaults.items():
                v = sb.get(k)
                if v is not None:
                    defaults[k] = type(default)(v)
            pm = sb.get("priority_multipliers")
            if isinstance(pm, dict):
                for pk, pv in pm.items():
                    if pv is not None:
                        priority_multipliers[str(pk)] = float(pv)
    except Exception:
        pass
    defaults["priority_multipliers"] = priority_multipliers
    return defaults


STARVATION_CONFIG = load_starvation_boost_config()


# Hoisted to module level so the FALLBACK values are directly pinnable. They are
# not merely a formality: the loader swallows every overlay failure, and a world
# whose aspirations.yaml carries no pull_boost block runs on exactly these numbers.
# A mutation control that corrupted the in-function literal came back GREEN,
# because the test read load_pull_boost_config() and the shipped YAML overrode the
# damage — the effective config was pinned while the default it falls back to was
# not (guard-3534: a test is only protection if it can fail).
_PULL_BOOST_DEFAULTS = {
    "enabled": True,
    "boost": 4.0,
    "max_age_hours": 24.0,
}


def load_pull_boost_config():
    """Load dependency-pull boost params from core/config/aspirations.yaml.

    g-115-6590 (2026-08-17). Every existing anti-starvation term in this file is
    TIME-keyed (recurring_urgency, starvation_boost, the drain lane, the monitor
    interval arm); none is EVENT-keyed. A consumer goal that exists to drain a
    dependency the moment it MATERIALIZES therefore cannot be lifted by anything
    here — its urgency lives in the PRODUCER's event, which the scorer never sees.
    A producer sets ``pull_signal`` on the consumer goal when the dependency lands;
    this pass converts that event into rank.

    THE DEFAULT BOOST IS MEASURED, NOT PICKED. guard-1895 (2): sizing a scorer fix
    against the deterministic deficit rather than the NOISE WIDTH is what makes an
    intervention look like a fix while changing almost nothing. Measured live on
    this queue 2026-08-17 (cc-07, 1163 candidates): exploration_noise ~ U(0, 1.210)
    applied to 99.6% of candidates, with 44 candidates inside one noise width of
    the deterministic top. The goal's own acceptance bar is a pulled goal beating a
    top substantive 2 points above it, so the boost must clear 2 + 1.210 = 3.21.
    4.0 clears that and stays BELOW directive_boost's 4.5 raw ceiling, so a fresh
    USER directive still outranks a machine-generated pull — the same ordering
    argument load_starvation_boost_config makes for its own 4.0.

    max_age_hours is a SAFETY VALVE, not decoration, and it is why this pass does
    not depend on the clear working. Measured against coordination_merge._merge_goal
    the same day: a one-sided SET survives cross-box merge even when the other side
    is NEWER (good — the producer's write reaches the consumer), but CLEAR-BY-KEY-
    REMOVAL is RESURRECTED by the one-sided-key loop even when the clearer is
    strictly newer, and CLEAR-BY-NULL loses whenever the clearing write is not
    strictly newer than the set (last_modified is seconds-resolution, so a same-
    second tie is reachable). A lost clear would otherwise pin the consumer goal at
    rank 1 forever — strictly worse than the starvation being fixed. Ageing the
    signal out makes that failure self-healing.

    Same overlay/type-coerce shape as the sibling config loaders.
    """
    defaults = dict(_PULL_BOOST_DEFAULTS)
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", Path(__file__).parent / "_config_overlay.py"
        )
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        asp_config = overlay.merged_config("aspirations.yaml")
        pb = asp_config.get("pull_boost", {})
        if isinstance(pb, dict):
            for k, default in defaults.items():
                v = pb.get(k)
                if v is not None:
                    defaults[k] = type(default)(v)
    except Exception:
        pass
    return defaults


PULL_CONFIG = load_pull_boost_config()


_FAN_IN_DEFAULTS = {
    "enabled": True,
    "per_dependent": 1.2,
    "cap": 4.0,
    "priority_weights": {"HIGH": 1.0, "MEDIUM": 0.5, "LOW": 0.25},
}


def load_fan_in_config():
    """Load fan-in (dependency-inversion) boost params from aspirations.yaml.

    g-115-6590 item (2), 2026-08-28. Same overlay/type-coerce shape as the
    sibling config loaders. See ``apply_fan_in_boost`` for why the defaults are
    the values they are.
    """
    defaults = dict(_FAN_IN_DEFAULTS)
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", Path(__file__).parent / "_config_overlay.py"
        )
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        asp_config = overlay.merged_config("aspirations.yaml")
        fb = asp_config.get("fan_in_boost", {})
        if isinstance(fb, dict):
            for k, default in defaults.items():
                v = fb.get(k)
                if v is not None:
                    defaults[k] = type(default)(v)
    except Exception:
        pass
    return defaults


FAN_IN_CONFIG = load_fan_in_config()


_PULL_SKEW_TOLERANCE_H = 1.0


def pull_signal_live_age_hours(sig, config, now=None):
    """SINGLE SOURCE OF TRUTH for "is this ``pull_signal`` live?".

    Returns the signal's age in hours (clamped at 0.0) when it is LIVE, or
    ``None`` when it is absent, malformed, aged out, or implausibly far in the
    future. Two callers consume it and they must never disagree:

      * ``apply_pull_boost`` — converts a live signal into RANK.
      * the recurring hour gate — converts a live signal into ELIGIBILITY.

    WHY THIS IS ONE FUNCTION AND NOT TWO COPIES (g-115-6590, 2026-08-28). The
    boost shipped 2026-08-17 reading this predicate inline, and the eligibility
    half did not exist at all -- so a not-yet-due recurring consumer carrying a
    live signal was dropped by the hour gate BEFORE ``apply_pull_boost`` ran over
    the already-scored list, and the boost could never see it. Measured on this
    queue 2026-08-28: g-306-284 held a live signal (set 14:14:34, 22 min old)
    while ZERO of 1375 returned candidates carried the field, i.e. the boost was
    operating on an empty set. Re-implementing the liveness test at the second
    call site would let the two halves drift into disagreeing about which signals
    are live -- a goal admitted by one and ignored by the other -- so both call
    this.

    THE SKEW TOLERANCE IS LOAD-BEARING, NOT DEFENSIVE PADDING, and it is why this
    does not use ``hours_since``: that helper folds ANY future timestamp into
    "corrupt" and returns None. Correct for its own callers, wrong here -- the
    signal is written on the PRODUCER's box and read on the CONSUMER's, so a
    producer even seconds ahead stamps a ``set_at`` in the reader's future and
    inheriting ``hours_since`` would silently drop the pull (guard-3221, the exact
    consumer-does-not-receive-what-the-producer-sent failure this mechanism lives
    inside). So: a SIGNED age with a bounded skew tolerance treated as live, and
    anything further ahead treated as bogus rather than clamped -- clamping would
    let a far-future stamp hold the lift for as long as the skew, the unbounded
    case ``max_age_hours`` exists to prevent.

    Respects ``enabled``: a disabled pull_boost config yields no lift on EITHER
    axis, so one flag still turns the whole mechanism off.
    """
    if not config.get("enabled"):
        return None
    if not isinstance(sig, dict):
        return None
    raw_set_at = sig.get("set_at")
    if not raw_set_at or not isinstance(raw_set_at, str):
        return None
    try:
        set_at = datetime.fromisoformat(raw_set_at)
    except (ValueError, TypeError):
        return None
    max_age = float(config.get("max_age_hours", 24.0))
    age_h = ((now or datetime.now()) - set_at).total_seconds() / 3600.0
    if age_h > max_age or age_h < -_PULL_SKEW_TOLERANCE_H:
        return None
    return max(0.0, age_h)


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


_SKILL_QUALITY_CACHE = {}


def _load_skill_quality_cached():
    """Read meta/skill-quality.yaml once and cache for the selector run.

    skill_affinity (criterion 12) previously called read_yaml_file per
    CANDIDATE — the same ~18KB YAML parsed once per scored goal. Measured
    2026-08-21 (alpha, cc-09, 1,335 candidates): 27.9ms of the 29.9ms
    per-goal scoring cost — ~37s of a ~40s selector invocation spent
    re-parsing one unchanged file for the second-lowest-weighted criterion.
    One snapshot per process is also the CORRECT read, not merely the cheap
    one: a mid-invocation edit to skill-quality.yaml must not let two
    candidates score against different quality tables in the same ranking.
    Keyed by str(SKILL_QUALITY_PATH) rather than a bare singleton so tests
    and perf probes that repoint the module attribute get a fresh parse
    instead of a stale hit. Mirrors _load_team_state_cached above. Callers
    treat the returned dict as READ-ONLY (guard-1663: never mutate a
    shared-cache record).
    """
    key = str(SKILL_QUALITY_PATH)
    if key not in _SKILL_QUALITY_CACHE:
        _SKILL_QUALITY_CACHE[key] = read_yaml_file(SKILL_QUALITY_PATH)
    return _SKILL_QUALITY_CACHE[key]


PRIORITY_MAP = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
# Inverse of PRIORITY_MAP: numeric raw["priority"] -> name, for apply_starvation_boost
# (), which gates the anti-starvation lift on the named-priority multipliers.
_PRIORITY_NUM_TO_NAME = {v: k for k, v in PRIORITY_MAP.items()}


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


def _anomalies_path():
    """Destination for goal-selector anomaly telemetry ( STEP 1).

    A FUNCTION rather than an inline `Path(WORLD_DIR) / ...` build, so the
    emitter's OUTPUT is patchable by exactly the seam tests already use for its
    INPUTS. `test_goal_selector_allblocked_reread.py` patches eight module
    attributes (read_jsonl, read_wm, score_goal, AGENT_DIR, ...) and could not
    patch the destination, because there was nothing to patch — the path was
    built inline from a module global at write time. That asymmetry IS the bug:
    inputs were injectable, the output was not.

    `GOAL_SELECTOR_ANOMALIES_PATH` lets a test that WANTS to assert on the
    emitted record point it at tmp_path instead of suppressing it.
    """
    override = os.environ.get("GOAL_SELECTOR_ANOMALIES_PATH")
    if override:
        return Path(override)
    return Path(WORLD_DIR) / "goal-selector-anomalies.jsonl"


def _anomalies_write_refused():
    """True when writing would append FIXTURE output to real deployment evidence.

    Measured on cc-02 2026-08-01: goal-selector-anomalies.jsonl held 1014
    records and ALL 1014 were fixture output (first/retry aspiration+goal counts
    of 1/1/1/1, reproducing _world_with_blocked / _world_with_pending), spanning
    2026-05-31 to a run that same morning. Zero real anomalies. The evidence file
    for a live investigation was 100% noise, and it grew on every suite run.

    Chokepoint rather than per-test discipline, deliberately: the same shape as
    g-115-3329, where the spawn paths began REFUSING under PYTEST_CURRENT_TEST
    with no explicit runtime dir. A fix that requires each test to remember to
    patch WORLD_DIR only fixes the tests that exist today; this one holds for
    every future test that reaches this emitter, which is the class the goal
    asks for. Suppression is announced on stderr — a silent skip would trade one
    invisible behavior for another.
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) and not os.environ.get(
        "GOAL_SELECTOR_ANOMALIES_PATH")


def _log_transient_allblocked_recovery(first_world, retry_world, retry_count,
                                       event="transient_all_blocked_recovered"):
    """Record a transient all_blocked recovery for root-cause evidence ().

    `event` is parameterized (g-115-4010 STEP 3) so the FAILURE branch — retry
    also returned zero — emits a sibling record through this same counting and
    fail-open path. Default preserves the original single-event behavior, so the
    existing call site and signature are unchanged.

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
        if event == "transient_all_blocked_recovered":
            sys.stderr.write(
                "[goal-selector] WARN transient all_blocked recovered: first pass 0 "
                "candidates, retry found %d. world_content_changed_between_reads=%s "
                "(first %d asps/%d goals, retry %d asps/%d goals). See g-115-1295.\n"
                % (retry_count, content_changed, fa, fg, ra, rg))
        else:
            sys.stderr.write(
                "[goal-selector] all_blocked confirmed: retry ALSO returned 0 "
                "candidates. world_content_changed_between_reads=%s "
                "(first %d asps/%d goals, retry %d asps/%d goals). See g-115-4010.\n"
                % (content_changed, fa, fg, ra, rg))
        try:
            rec = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "event": event,
                "retry_candidates": retry_count,
                "first_world_aspirations": fa, "retry_world_aspirations": ra,
                "first_world_goals": fg, "retry_world_goals": rg,
                "world_content_changed_between_reads": content_changed,
            }
            if _anomalies_write_refused():
                sys.stderr.write(
                    "[goal-selector] anomaly telemetry SUPPRESSED under pytest — "
                    "refusing to append fixture output to real deployment evidence. "
                    "Set GOAL_SELECTOR_ANOMALIES_PATH to capture it. (g-115-4010)\n")
            else:
                path = _anomalies_path()
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
        except Exception:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cross-session class completions ( /  drift fix)
# ---------------------------------------------------------------------------

# Max age of the NEWEST contributing journal entry before the recent-completions
# window is treated as fossil rather than recent (). 7 days is
# deliberately loose: the window is a scoring input, not a correctness gate, and
# a quiet weekend must not trip it. The measured failures were 50 and 82 days —
# an order of magnitude past this — so the threshold does not need to be tight
# to catch the real defect, and a loose one keeps false positives near zero.
# Override for tests / tuning via env.
RECENT_WINDOW_MAX_AGE_DAYS = float(
    os.environ.get("RECENT_WINDOW_MAX_AGE_DAYS", "7") or "7"
)


def _window_age_days(entry_date):
    """Age in days of a journal entry's `date`, or None if unparseable.

    Returns None (never raises, never guesses) on a missing or malformed date so
    the caller's guard fails OPEN — an unparseable date must not be treated as
    stale, or a journal-format change would silently disable the real window.
    """
    if not entry_date or not isinstance(entry_date, str):
        return None
    raw = entry_date.strip()
    # Journal entries carry a bare `YYYY-MM-DD`; tolerate a full ISO timestamp
    # so this keeps working if the writer is ever restored with more precision.
    #
    # MOST-SPECIFIC FIRST, and slice to each format's OWN width. Ordering the
    # date-only format first makes the ISO branch unreachable: `raw[:10]` of an
    # ISO stamp parses cleanly as a date, so the time is silently dropped and
    # every ISO input reads up to a day older than it is. Caught by
    # test_full_iso_timestamp_is_accepted, which measured 3.7 days for a
    # 3-day-old stamp.
    for fmt, width in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10)):
        candidate = raw[:width]
        if len(candidate) < width:
            continue
        try:
            parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        return (datetime.now() - parsed).total_seconds() / 86400.0
    return None


def load_recent_class_completions(window_size=20):
    """Cross-session sampling window for goal-selector criteria.

    Replaces in-session-only `wm.goals_completed_this_session` (which resets
    every /stop and was structurally blind to cross-session drift) with a
    rolling window of THIS agent's recent completions, drawn from world+agent
    aspirations.jsonl (`completed_at` / `completed_date` / `lastAchievedAt`,
    filtered to `completed_by == AGENT_NAME`). <agent>/journal.jsonl is a
    fallback for worlds carrying no completion markers — it was the primary
    source until g-115-4293, when its `goals_completed` field was found to have
    no writer anywhere in the codebase and the window had silently fossilised.

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

    # NOTE: journal existence is checked at the JOURNAL branch below, not here.
    # It used to gate this whole function, which was correct while the journal
    # was the only source — but once the aspirations store became primary that
    # early return made the store path unreachable for any agent lacking a
    # journal.jsonl (a fresh agent, or a transplanted one), silently pinning it
    # to the in-session list. Caught on pre-completion re-read, .

    # Build goal_id → {aspiration_id, recurring, work_class} index from
    # world + agent aspirations. Empty work_class is preserved so callers
    # can distinguish "no entry" from "no work_class tag" (the existing
    # class_balance check filters missing work_class out of the denominator).
    index = {}
    dated = []  # (completion_ts, info, completed_by) for goals carrying a marker
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
                    info = {
                        "goal_id": gid,
                        "aspiration_id": asp_id,
                        "recurring": bool(g.get("recurring", False)),
                        "work_class": g.get("work_class") or "",
                    }
                    index[gid] = info
                    # : collect completion markers while we are already
                    # walking these records, so the primary window below costs no
                    # additional I/O. `completed_at` first — measured highest
                    # coverage (77% of 4,628 goals vs 66% for completed_date);
                    # lastAchievedAt (2%) is the recurring-goal marker, which is
                    # the ONLY marker a recurring goal ever gets (it returns to
                    # status=pending on close and never carries completed_date).
                    ts = (g.get("completed_at") or g.get("completed_date")
                          or g.get("lastAchievedAt"))
                    if ts and info["work_class"]:
                        # DEDUP BY CONSTRUCTION ( F-2): a goal id can
                        # contribute at most ONE row here, because the store keeps a
                        # single completion marker per goal — measured 2026-08-27
                        # (zeta, cc-02): 948 dated rows, 948 distinct ids, 0 repeats,
                        # unfiltered population 3130. The journal this replaced
                        # () carried one entry PER FIRING, so
                        # per_goal_saturation.consecutive_threshold > 1 is now
                        # unreachable from this path. See core/config/aspirations.yaml
                        # per_goal_saturation.
                        dated.append((str(ts), info, g.get("completed_by") or ""))
    except Exception:
        return _in_session_fallback()

    def _guarded(window, newest_ts, source_label):
        """Return `window`, or fall back when it is empty or FOSSIL.

        Applied to BOTH source paths deliberately. An earlier draft of this fix
        returned the aspirations window directly and left the guard sitting only
        on the journal path — which `dated` being non-empty made structurally
        unreachable, so the guard would have been dead on arrival in the same
        change that introduced it. Whatever builds the window, the freshness
        assertion is the last thing between it and the scorer.
        """
        if not window:
            return _in_session_fallback()
        age_days = _window_age_days(newest_ts) if newest_ts else None
        if age_days is not None and age_days > RECENT_WINDOW_MAX_AGE_DAYS:
            print(
                "[goal-selector] WARN: recent-completions window is STALE — "
                f"newest contributing record in {source_label} is {age_days:.1f} "
                f"days old (max {RECENT_WINDOW_MAX_AGE_DAYS}). Falling back to "
                "the in-session list. per_goal_saturation / class_balance_bonus "
                "/ context_coherence would otherwise score against fossil data "
                "(g-115-4293).",
                file=sys.stderr,
            )
            return _in_session_fallback()
        return window

    # ── Primary window: the aspirations store () ────────────────
    # The journal path below is retained as a FALLBACK, not the primary source.
    # Rationale: journal.jsonl `goals_completed` was a DUPLICATE of completion
    # data the aspirations store already holds, and nothing has written it since
    # the writer was lost — so it drifted precisely because it was a duplicate
    # (communication-clarity rule 5: one piece of data, one home). The store is
    # also the evidence-grade source guard-138 names (an explicit completion
    # marker, not a journal/experience count).
    #
    # Measured 2026-07-31 (alpha, cc-04): 3,077 goals carry both a completion
    # marker and a work_class, newest stamped minutes earlier — against a journal
    # window whose newest contributor was 15.7 days old and oldest 50.7.
    # SCOPE: this agent's completions, NOT the fleet's. The window's whole
    # purpose () was catching THIS agent's 53% framework dominance, and
    # the journal it originally read was per-agent. Sourcing the shared store
    # without this filter silently widens it to every agent: measured on the
    # first draft, 7 of 8 window entries belonged to partners. That is harmless
    # for per_goal_saturation (arguably better — it suppresses a recurring goal a
    # partner just finished) but wrong for the other two consumers, which ask
    # about SELF: class_balance_bonus would score the fleet's class mix instead
    # of mine, and context_coherence would reward following the fleet's working
    # context instead of my own.
    # Fall back to unfiltered when nothing is self-attributed (a fresh agent, or
    # a deployment that does not populate completed_by) — a slightly-too-wide
    # window still beats no cross-session window at all, which is the defect
    # being fixed.
    if dated:
        mine = [d for d in dated if AGENT_NAME and d[2] == AGENT_NAME]
        scoped = mine or dated
        label = ("the aspirations store" if mine
                 else "the aspirations store (unattributed — fleet-wide)")
        scoped.sort(key=lambda t: t[0])               # ascending == chronological
        recent = scoped[-window_size:]
        return _guarded([info for _ts, info, _by in recent], recent[-1][0], label)

    # Tail-read journal: collect goals_completed entries from latest entries
    # backwards until we have window_size with non-empty work_class.
    try:
        with open(journal_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return _in_session_fallback()

    completions = []
    newest_contrib_date = None  # date of the NEWEST entry that contributed
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
            if newest_contrib_date is None:
                # We walk newest-first, so the FIRST contributor is the newest.
                newest_contrib_date = entry.get("date")
            completions.append(info)
            if len(completions) >= window_size:
                break
        if len(completions) >= window_size:
            break

    # ── Journal fallback + staleness guard () ───────────────────
    # Reached only when the aspirations store yielded no dated completion at all
    # (a fresh world, or one predating work_class tagging). Every fallback ABOVE
    # keys on the window being UNREADABLE (no AGENT_DIR, missing journal, index
    # error, read error) or EMPTY — none keyed on it being OLD, so a window
    # filled entirely from months ago was indistinguishable from a fresh one and
    # was returned as "recent".
    #
    # CORRECTED IN PLACE 2026-08-28 (, alpha/cc-08). This comment used
    # to read "`goals_completed` has NO writer anywhere in core/scripts or
    # mind_api". That is FALSE and has been since 2026-04-25 (commit c9b2248d4):
    # `core/scripts/journal-append.sh:257` unions the closing goal id into a
    # journal record via journal-merge.sh, and both live callers reach it
    # (iteration-close.sh:2133, worker_retrospective.py:790). The original grep
    # covered .py only, where every match really does target a different store
    # (session telemetry, handoff.yaml, the loop_state int counter) — which is
    # exactly why the wrong conclusion looked well-evidenced.
    #
    # The field is written CONSTANTLY, to ONE WRONG RECORD. journal-append.sh
    # derives its session number from `active_context.session_id`, which fails
    # two INDEPENDENT ways that both land on the literal fallback "1":
    #   (A) format — `wm-read.sh active_context` emits YAML and the inline
    #       parser calls json.load, so it raises JSONDecodeError and takes
    #       `except: print("1")`. wm-read.sh HAS a `--json` flag; no caller
    #       passes it.
    #   (B) schema — `active_context` has no `session_id` key. wm.py:427 defines
    #       the slot as {summary, experience_refs, retrieval_manifest} and
    #       nothing in production writes one; only test fixtures construct it.
    #       So even with --json, sid == "" and the same "1" is returned.
    # Fixing (A) alone is a NO-OP. Measured 2026-08-28 across all five agents:
    # 90.1%-99.5% of every goal id ever recorded sits in 3-19 records claiming
    # session 1, dated 2026-03-27..2026-05-16; alpha's largest holds 2062 ids and
    # its newest entries are same-day closes.
    #
    # So this window does not fossilise because nothing writes — it fossilises
    # because the writes land in an ancient record that this newest-first walk
    # reaches LAST. The staleness guard below is unchanged and still needed; only
    # its stated cause was wrong. Measured 2026-07-31: alpha walked back 194 of
    # 384 journal entries to fill 20, whose newest contributor was 15.7 days old
    # and oldest 50.7; zeta measured 82 days on a second box. Three scorer
    # criteria consume this window — per_goal_saturation (a RAPID-REPEAT
    # suppressor charging -5.0 for a months-old completion), class_balance_bonus,
    # and context_coherence.
    #
    # guard-138 governs the SHAPE: a clock-only staleness heuristic must not take
    # a destructive action. This one is deliberately non-destructive — it falls
    # back to the in-session list (current, if short) and WARNS on stderr. It
    # never deletes, reverts, or rewrites stored state, so the guard-138
    # evidence-gate requirement (which protects destructive reversion) is not
    # engaged. Being loud is the actual fix: the defect was silence, not the
    # staleness itself.
    #
    # Reverse to chronological (oldest first) so [-N:] slicing semantics match.
    return _guarded(list(reversed(completions)), newest_contrib_date,
                    "journal.jsonl")


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
    reallocation_hours (g-115-1766 gap #4 — intended_agent idle-reallocation).

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
        backend = os.environ.get("STORAGE_BACKEND", "local")
        fresh_iso = _lc.fetch_fresh_signal(name, str(WORLD_DIR), backend)
        # The shard OBJECT's write time is BODY activity; the last_active VALUE
        # inside the authoritative shard is MIND liveness (-e). Supply
        # both so this routing decision cannot disagree with the CLI verdict —
        # object-time alone made a worker Body's write look like a live reducer.
        # Provenance travels WITH the value (). read_shard_authoritative
        # fails open to the local mirror, and a mirror value promoted to
        # verdict=alive is a false ALIVE — here that would keep goals routed to a
        # dead agent. Passing the provenance lets decide_liveness degrade to
        # "unknown", which this function does not treat as dormant either way, so
        # the failure stays in the goals-stay-routed direction.
        auth_la_iso, auth_la_prov = _lc.fetch_authoritative_last_active_with_provenance(
            name, str(WORLD_DIR))
        # A row stamped by ANOTHER agent cannot certify its subject alive
        # (, guard-3604): clearing a dormant peer's stranded in_flight
        # bumps the CLEARED row's last_active, so a peer this fleet just policed
        # reads fresh for a full window. This call site must pass the stamp too
        # or the reallocation gate keeps the defect the CLI just lost — the
        # verdict is what decides whether a dormant agent's routed goals are
        # reclaimed, so a false "alive" here strands them indefinitely.
        row_stamp = _lc.fetch_row_stamp(name, str(WORLD_DIR))
        verdict = _lc.decide_liveness(
            last_active_iso, fresh_iso, threshold_hours=threshold_hours,
            now=datetime.now(),
            authoritative_last_active_iso=auth_la_iso,
            authoritative_provenance=auth_la_prov,
            row_updated_by=row_stamp, row_agent=name)["verdict"]
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

        # Cooldown check. Reads last_selected, NOT last_worked ():
        # `last_worked` has ZERO writers in core/scripts or mind_api and is absent
        # from every live record (measured 2026-08-10: 0/28 world aspirations carry
        # the key). It survives only as a seeded null in the three bootstrap
        # templates, which is what made it look real. last_selected is the live
        # field with these semantics, stamped by aspirations.py /
        # aspirations_write.py when a goal is selected from the aspiration.
        #
        # hours_since, NOT days_since — load-bearing, not a style choice.
        # last_selected is written as datetime.now().isoformat(timespec="seconds"),
        # and days_since() calls date.fromisoformat(), which RAISES on any string
        # carrying a time component (verified py3.12.3: date.fromisoformat(
        # "2026-08-10T05:51:36") -> ValueError). days_since swallows it and returns
        # None, so the obvious same-shape repoint — days_since(last_selected) —
        # would leave this branch permanently dead and look fixed. That is the very
        # defect being removed here, one level up. hours_since parses BOTH forms.
        cooldown = asp.get("cooldown_days", 0)
        if cooldown > 0:
            hs = hours_since(asp.get("last_selected"))
            if hs is not None and hs < cooldown * 24:
                continue

        # Use global done_ids if provided (cross-aspiration dependency enforcement),
        # otherwise fall back to per-aspiration scope (legacy behavior).
        if global_done_ids is not None:
            done_ids = global_done_ids
        else:
            done_ids = {g["id"] for g in asp.get("goals", [])
                        if isinstance(g, dict) and g.get("id")
                        and g.get("status") in ("completed", "decomposed")}

        # live_ids: goal IDs that could still complete (non-terminal status).
        # Re-validates dependency liveness before honoring the dependency_timeout
        # fail-open (). Mirrors done_ids: global set when supplied,
        # else per-aspiration scope.
        if global_live_ids is not None:
            live_ids = global_live_ids
        else:
            live_ids = {g["id"] for g in asp.get("goals", [])
                        if isinstance(g, dict) and g.get("id")
                        and g.get("status") not in TERMINAL_GOAL_STATUSES}

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

            # Claim check (ALL sources): skip goals claimed by someone else.
            # Expiry makes stale claims (older than claim_timeout_hours) fall through
            # so other agents can pick up abandoned work. The actual re-claim is still
            # atomic via aspirations-claim.sh — this only controls VISIBILITY.
            # For recurring goals, claim timeout is capped at 2x interval_hours so that
            # short-interval goals (e.g. 1h email check) don't stay claimed for 4h.
            #
            # "Someone else" has TWO forms, and they share one expiry ladder:
            #   (a) another MIND — a different agent name. The original case.
            #   (b) another BODY of THIS mind ( part 1) — same agent
            #       name, different session. A name-only comparison waves this
            #       straight through, so two Bodies of one mind re-select each
            #       other's live claim every cycle and livelock. The claim does
            #       NOT set status (status lands later, in aspirations-execute),
            #       so the goal stays `pending` and keeps re-qualifying here.
            # The expiry ladder is deliberately the liveness test for BOTH: a
            # sibling Body's abandoned claim must age out exactly like a foreign
            # agent's, and reusing the ladder keeps a network liveness probe off
            # the per-goal selection hot path. stranded-claim-sweep.py remains
            # the authority for actually releasing a dead Body's claim.
            # Fail-open by construction: when either SID is absent (legacy record
            # with no claimed_by_sid, or MIND_SID unset) `sibling_body` is False
            # and this filter behaves exactly as it did before .
            #
            # UNCONDITIONAL ACROSS SOURCES since the 2026-08-21 selection-stack
            # review ( part 2). This block was `if source == "world"`
            # from birth, but agent-queue goals DO carry claims: cross-agent
            # pull writes the claim back to the OWNING sibling's queue
            # (), and under Mind/Body a sibling Body's claim lands on
            # this agent's own queue — both were invisible to this filter, so a
            # claimed agent-queue goal kept re-qualifying for its owner and for
            # every puller (the world-queue livelock, reproduced one store
            # over). Where the claim fields are absent — the overwhelming
            # agent-queue majority — `other_mind` and `sibling_body` are both
            # False and the block is a structural no-op, so behavior changes
            # ONLY for records carrying a live foreign/sibling claim.
            #   (c) an ORPHANED SID claim () — `claimed_by` is null but
            #       `claimed_by_sid` still names a live session. BOTH branches
            #       above are gated on `bool(claimed)`, so this record is
            #       structurally invisible to them and gets offered as UNCLAIMED
            #       while a live Body holds it. guard-4434 is the reading rule:
            #       a null claimed_by beside a non-null claimed_by_sid IS a
            #       claim, and a foreign sid fails closed regardless of the
            #       missing name. This is the enforcement half.
            #       The shape is not hypothetical and not a legacy artifact: it
            #       is produced by own-cloud fenced-PUT reconcile damage
            #       (rb-3636 sub-mechanism B / class ), which nulls
            #       claimed_by while siblings survive. Measured 2026-08-22
            #       (zeta, cc-02): 7 damaged records live, and the ONE carrying
            #       a non-null sid — , holding alpha's then-active body
            #       SID — was being offered in the candidate list at that
            #       moment. Detector: core/scripts/claim-integrity-check.py.
            #       Fails CLOSED when BODY_SID is unset (every sid then reads
            #       foreign), which is the safe direction for a claim check and
            #       costs nothing: the population is records with a null name
            #       AND a live sid — 1 of 2239 non-terminal goals when measured.
            #       Routed through the SAME expiry ladder below, so a dead
            #       Body's orphaned claim still ages out and is reclaimable
            #       rather than freezing the goal forever.
            claimed = goal.get("claimed_by")
            claim_sid = goal.get("claimed_by_sid")
            other_mind = bool(claimed) and claimed != AGENT_NAME
            sibling_body = (
                bool(claimed)
                and claimed == AGENT_NAME
                and isinstance(claim_sid, str) and bool(claim_sid)
                and bool(BODY_SID)
                and claim_sid != BODY_SID
            )
            orphan_sid_claim = (
                not claimed
                and isinstance(claim_sid, str) and bool(claim_sid)
                and claim_sid != BODY_SID
            )
            if other_mind or sibling_body or orphan_sid_claim:
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
                        # EVENT-keyed bypass (), mirroring the
                        # cadence_signal bypass directly above. apply_pull_boost
                        # runs on `scored`, and this `continue` drops the goal
                        # BEFORE scoring — so the boost could only ever lift a
                        # consumer the time gate had ALREADY admitted, which made
                        # the flag inert for exactly the case it exists for
                        # ("fire WHEN the carrier arrives", not on the interval).
                        # MEASURED 2026-08-28:  carried a live
                        # pull_signal for 1h50m (producer healthy — set by
                        # alpha/cc-08 with the carrier ref) and was ABSENT from
                        # BOTH bravo's and alpha's candidate sets, because
                        # la 2.1h < interval 4.45h. Absence, not a low rank:
                        # nothing score-side could have reached it.
                        #
                        # Liveness comes from pull_signal_producer.is_live, whose
                        # docstring is literally "True when apply_pull_boost would
                        # currently honour this signal" — ONE predicate, TWO
                        # consumers, so eligibility and lift cannot drift apart.
                        # That is the same argument overdue_exemption_level makes
                        # for its own two consumers (); a second inline
                        # copy of the skew/age arithmetic is what would rot.
                        #
                        # NO-REGRESSION BY CONSTRUCTION: is_live returns False for
                        # a goal with no pull_signal dict, so every unpulled goal
                        # takes the `continue` exactly as before. Gated on
                        # PULL_CONFIG["enabled"] so disabling the mechanism
                        # disables the bypass too, matching apply_pull_boost's
                        # own early return.
                        #
                        # PREDICATE CHOICE, settled at the cc-05/cc-07 merge
                        # (2026-08-28): this calls goal-selector's own
                        # pull_signal_live_age_hours, NOT pull_signal_producer's
                        # is_live. Both were written for this gate and they agree
                        # across the whole age range (verified 0.5/12/23.9/25/100h
                        # plus the absent-signal case) — but is_live re-implements
                        # the skew/age arithmetic in the producer module, and
                        # apply_pull_boost needs the AGE (it records
                        # pull_signal_age_hours), so it cannot use a bool. Reading
                        # is_live here would therefore leave the gate and the boost
                        # on two SEPARATE copies of one rule, which is precisely
                        # the drift the paragraph above argues against. One
                        # predicate, two consumers — literally, not by intent.
                        if not (
                            PULL_CONFIG.get("enabled")
                            and pull_signal_live_age_hours(
                                goal.get("pull_signal"), PULL_CONFIG) is not None
                        ):
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
                    # Handoff-gated defer (): same never-self-clears
                    # property as human_blocked:, different cause — routing, not a
                    # human gate. SYMMETRY: collect_blocked has the twin. Change both.
                    if _is_handoff_gated_defer(goal):
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
            # : routes_away_from() also returns False for a value
            # OUTSIDE the live vocabulary (a retired agent, or an unrecognized
            # sentinel like the cycle-detector's "any"), so such a goal falls
            # through and becomes visible instead of vanishing from BOTH this
            # output and collect_blocked (which never references
            # intended_agent). Conservative on an unreadable roster -- see the
            # helper's docstring.
            intended_agent = goal.get("intended_agent")
            if routes_away_from(intended_agent, AGENT_NAME):
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
                    reallocation_hours=None,
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
    # Same accessor collect_candidates uses (memoized per process, so this second
    # call costs no extra authoritative-store probes) -- required by the
    # routed_to_agent branch below to stay the exact complement of the candidate
    # side's idle-reallocation escape. Empty set when reallocation is disabled.
    idle_agents = _get_idle_agents(reallocation_hours)

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
                        if isinstance(g, dict) and g.get("id")
                        and g.get("status") in ("completed", "decomposed")}

        # live_ids: mirror of collect_candidates — goal IDs still able to complete
        # (non-terminal status). Re-validates dependency liveness so the timeout
        # fail-open stays the logical complement across both functions ().
        if global_live_ids is not None:
            live_ids = global_live_ids
        else:
            live_ids = {g["id"] for g in asp.get("goals", [])
                        if isinstance(g, dict) and g.get("id")
                        and g.get("status") not in TERMINAL_GOAL_STATUSES}

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
                    # Handoff-gated twin of the guard above (). Keep it in
                    # blocked[] so all_blocked stays assertable and quiescence fires,
                    # instead of falling through to the candidate pool of a Body the
                    # goal is not routed to.
                    if _is_handoff_gated_defer(goal):
                        entry["block_reason"] = "deferred"
                        entry["block_detail"] = (
                            "Handoff-gated: routed to {to}; {reason}".format(
                                to=goal.get("handoff_to"),
                                reason=goal.get("defer_reason", "")))
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

            # 6.5 Routed-to-another-agent: intended_agent names a peer, so
            #    collect_candidates DROPPED this goal at its intended_agent
            #    filter. Without this inverse the goal vanishes from the ranked
            #    list AND the blocked list -- invisible in both directions and
            #    therefore immortal (guard-1698, which names this exact filter;
            #    census  identified intended_agent as the ONLY
            #    PERMANENT select-time drop lacking an inverse).
            #    SYMMETRY: must be the logical complement of the intended_agent
            #    check in collect_candidates. If you change one, change the
            #    other. That side ESCAPES (surfaces the goal as selectable) only
            #    when ALL THREE hold -- owner idle, goal unclaimed, goal not
            #    owner-scoped -- so this side blocks unless all three hold, which
            #    keeps candidate XOR blocked exact.
            #    guard-3644: the detail names EVERY unmet escape conjunct, not
            #    just the first. A message reporting one conjunct of an AND
            #    describes the cheapest check, never the decisive one -- and an
            #    "owner is idle, this self-clears" forecast read off a partial
            #    reason is how a decided block gets re-read as a transient wait.
            #    Placed BEFORE not_my_lane to mirror capability being the LAST
            #    filter on the candidate side.
            intended_agent = goal.get("intended_agent")
            if routes_away_from(intended_agent, AGENT_NAME):
                unmet_escape = []
                if intended_agent not in idle_agents:
                    unmet_escape.append("owner not idle")
                if goal.get("claimed_by"):
                    unmet_escape.append(
                        "claimed by {c}".format(c=goal.get("claimed_by")))
                if _is_owner_scoped_goal(goal):
                    unmet_escape.append("owner-scoped work")
                if unmet_escape:
                    entry["block_reason"] = "routed_to_agent"
                    entry["block_detail"] = (
                        "Routed to {a}; idle-reallocation escape unavailable "
                        "({w})".format(a=intended_agent,
                                       w="; ".join(unmet_escape)))
                    # guard-1362: routing/ownership fields must reach the
                    # consuming LLM, not only the scoring/diagnostic ones.
                    entry["intended_agent"] = intended_agent
                    entry["unmet_escape_conditions"] = unmet_escape
                    if not isinstance(entry.get("blocker_ref"), dict):
                        entry["blocker_ref"] = _synth_block_ref(
                            "routed-to-agent", str(intended_agent))
                    blocked.append(entry)
                    continue
                # All three escape conditions hold -> collect_candidates
                # surfaces this goal as selectable, so it is NOT blocked here.
                # Fall through (never classify a live candidate as blocked).

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


BLOCKED_PRESET_REASONS = [
    "infrastructure", "dependency", "deferred", "hypothesis_gate", "explicit_status",
]


def _blocked_reason_counts(blocked):
    """Canonical reason -> count map over collect_blocked output.

    SINGLE SOURCE for the by_reason reason SET, shared by cmd_select's
    all-blocked summary and cmd_blocked's tally so the two surfaces cannot
    drift again. They drifted once: cmd_blocked hard-coded the 5
    BLOCKED_PRESET_REASONS while collect_blocked had grown to 7
    (precondition_unmet, not_my_lane). Measured 2026-08-21 (alpha, cc-09):
    9 of 250 live blocked rows were present in blocked_goals[] and counted
    in summary.total_blocked but absent from by_reason — sum(by_reason) !=
    total_blocked, and a reader tallying by_reason concluded those classes
    were empty. Preset keys are always present (zero-count) because
    consumers iterate them (verify-learning q3386 asserts key presence;
    aspirations-all-blocked reads them); observed extras are added
    dynamically so a future predicate class is visible the day it ships.
    """
    counts = {r: 0 for r in BLOCKED_PRESET_REASONS}
    for e in blocked:
        r = e.get("block_reason") or "unknown"
        counts[r] = counts.get(r, 0) + 1
    return counts


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

def _suggest_category(text):
    """Category suggestion for free text. Returns a category key or None.

    Calls gates.category_suggest.evaluate IN-PROCESS rather than spawning
    category-suggest.py. The CLI is a thin shim over this same function, so the
    result is identical; what is removed is one interpreter startup per lookup.

    MEASURED (g-115-6972, echo, cc-03, 2026-08-21) — and the first two fixes
    tried were both wrong, so the reasoning is recorded here:
      * a MEMO on (title, description) buys NOTHING: instrumented over a real
        select, category lookups were hits=0 / misses=5 — five uncategorized
        goals, five distinct texts, each resolved exactly once. There is
        nothing to memoize.
      * BATCHING the subprocess was the other candidate, and it addresses the
        smaller half: of the 3.8s per spawn, only ~1.7s is process startup.
      * The dominant 2.1s was INSIDE evaluate, which re-parsed a 1.53 MB
        _tree.yaml and rebuilt the concept index on every call. That is fixed
        in gates/category_suggest.py::_load_tree_cached.
    In-process is what makes that cache reachable at all — a fresh subprocess
    per lookup can never hit an in-process cache, so the two changes only work
    together.

    Import is lazy and inside the try: it costs 0.03s, is needed only on the
    uncategorized path, and a failure here must degrade to the tags fallback
    exactly as the subprocess failure did — never raise into scoring.
    """
    try:
        from gates.category_suggest import evaluate as _cat_eval
        matches = _cat_eval(text, top_n=1, world_dir=WORLD_DIR)
        if matches and matches[0].get("score", 0) > 0:
            return matches[0]["key"]
    except Exception:
        pass
    return None


def _resolve_category(goal, asp):
    """Resolve goal category: direct field > suggest from text > aspiration tag.

    Falls back through three strategies:
    1. goal.category if set and not "uncategorized"
    2. gates.category_suggest.evaluate on title+description — called IN-PROCESS,
       NOT memoized here. A per-(title,description) memo was tried and measured
       useless (hits=0/misses=5: every lookup is a distinct goal text); the win
       came from the parsed-tree cache INSIDE category_suggest. See
       _suggest_category.
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
    suggested = _suggest_category(text)
    if suggested:
        return suggested

    tags = _ensure_list(asp.get("tags"))
    return tags[0] if tags else "uncategorized"


# ---------------------------------------------------------------------------
# Directive Boost (cross-agent priority influence)
# ---------------------------------------------------------------------------

BOARD_COORD_PATH = WORLD_DIR / "board" / "coordination.jsonl"


# Raw weight applied to a targeted directive that carries no explicit `weight:`
# tag. Raw, i.e. BEFORE WEIGHTS["directive_boost"] = 1.5, so the default lands at
# +1.5 final -- deliberately the same magnitude strategic_focus_boost already
# contributes in-lane, and BELOW what an author gets by stating a weight
# explicitly. Stating a weight remains the way to ask for stronger influence.
DIRECTIVE_DEFAULT_WEIGHT = 1.0


def parse_directive_admission(msg, now=None):
    """THE shared admission predicate for board directives ().

    Returns None when `msg` is not an admitted directive, else a dict:
    {id, tags, text, target_goals, target_categories, weight, weight_explicit}.

    WHY THIS EXISTS. Two call sites used to decide admission independently and
    disagreed on exactly one clause, with no shared code to keep them honest:

      load_active_directives (SCORING)  -- required an explicit `weight:` tag,
        `if weight == 0.0: continue`, so a directive with `target:` tags and no
        `weight:` tag was dropped from scoring ENTIRELY.
      emit_directive_honor_banner (guard-1310 MUST-SELECT banner) -- required
        only `target:` tags + directed-at-this-agent. It never looked at weight.

    Net effect: the banner fired a MUST-SELECT imperative for directives the
    scorer had scored at ZERO. Measured over the whole coordination board (32
    directive messages): 2 carried `weight:` (6.2%), 18 carried `target:`/
    `category:` (56.2%), exactly ONE carried both and could therefore score
    (3.1%). Of the 2 carrying `weight:`, one was `weight:high` -- float() raised,
    the bare except swallowed it, weight stayed 0.0 -- so the number of
    directives that have EVER influenced scoring is ONE. Authors of the 17
    targeted-but-weightless: echo 7, alpha 4, bravo 3, zeta 2, foxtrot 1 --
    fleet-wide, not one agent's habit. Sharpest single case: a USER ENDORSEMENT
    relayed from the alert inbox (msg-20260801-141730-alpha, tags
    [user-directive, endorsement, target:g-115-4005, ...]) contributed exactly
    0.00 to the ranking. A user-endorsed goal was invisible to the scorer.

    THE DECISION (resolution (a) of the two the filing goal named, chosen
    deliberately -- see the goal's own "Do NOT fix only one side"):
    scoring ADMITS a targeted directive with DIRECTIVE_DEFAULT_WEIGHT when
    `weight:` is absent, rather than the banner being tightened to require
    `weight:` (resolution (b)). Reasons, in order:

      1. (b) would make a weightless USER ENDORSEMENT inert on BOTH paths --
         no boost and no banner. That is strictly worse than today, where at
         least the banner fires. The user-endorsement case is the one this
         subsystem most needs to get right.
      2. (a) makes the two predicates agree BY CONSTRUCTION rather than by
         hand: after this change there is no such thing as an admitted
         zero-weight directive, so the banner can no longer compel a selection
         the scorer scored at zero. The defect closes at its root.
      3. The 17 live weightless directives become effective immediately, which
         is what their authors plainly intended by writing `target:` at all.

    GUARD-1310 CALIBRATION CONSEQUENCE, stated explicitly because outcome 2 of
    the filing goal requires it and because it is the real risk of choosing (a):
    guard-1310 was calibrated in a world where the boost had ALREADY lifted the
    target near the top -- its own text says "directive_boost pushed it to
    #1/#2 every time and the LLM skipped it anyway". Under (a) a directive no
    longer compels selection with zero numeric support, so the banner's
    MUST-SELECT is better justified than before, NOT worse. But the boost is
    bounded (+1.5 final, a nudge and never a veto -- Scorer Sovereignty
    g-115-2812), so a directive-targeted goal can still rank below a strong
    candidate and still raise the banner. That residual gap is guard-1310's
    to close via the ack / justified-deferral path, exactly as today; this
    change narrows it rather than eliminating it. Re-read guard-1310 before
    raising DIRECTIVE_DEFAULT_WEIGHT -- raising it trades Scorer Sovereignty
    for banner agreement, which is not this goal's call to make.

    A non-numeric `weight:` value now fails LOUD (stderr, naming the directive
    id) instead of silently zeroing. Silent zeroing is the worse failure: the
    tag is PRESENT and LOOKS correct, so the author has no way to learn it did
    nothing. The directive is still admitted at the default weight -- a typo in
    one tag should not silently discard the whole directive.
    """
    if msg.get("type") != "directive":
        return None
    now = now or datetime.now()
    tags = _ensure_list(msg.get("tags"))

    # Expiry -- identical semantics on both paths (unchanged).
    for tag in tags:
        if tag.startswith("expires:"):
            try:
                if now > datetime.fromisoformat(tag[8:]):
                    return None
            except (ValueError, TypeError):
                pass

    # Weight: explicit when parseable, else the default. Never zero-admits.
    weight = DIRECTIVE_DEFAULT_WEIGHT
    weight_explicit = False
    for tag in tags:
        if not tag.startswith("weight:"):
            continue
        try:
            weight = float(tag[7:])
            weight_explicit = True
        except (ValueError, TypeError):
            print(f"[goal-selector] directive {msg.get('id')}: non-numeric "
                  f"weight tag {tag!r} — falling back to default weight "
                  f"{DIRECTIVE_DEFAULT_WEIGHT}", file=sys.stderr)

    # An EXPLICIT `weight:0` is the author saying "this directive should have no
    # effect" -- honour it by dropping the directive from BOTH paths. Found by
    # the  fresh-eyes pass on this very function: without this clause
    # the explicit zero was admitted, scored 0.00, and STILL fired the banner --
    # i.e. it reproduced the exact defect  closed, in the one case the
    # original `if weight == 0.0: continue` had covered by accident. Removing
    # that line fixed the implicit-zero path and silently opened the explicit
    # one. Note the asymmetry with a non-numeric weight, which falls back to the
    # default rather than dropping: `weight:high` is a TYPO (intent unknown, so
    # preserve the directive) while `weight:0` is an INSTRUCTION (intent stated,
    # so obey it).
    if weight_explicit and weight == 0.0:
        return None

    target_goals = [t[7:] for t in tags if t.startswith("target:")]
    target_categories = [t[9:] for t in tags if t.startswith("category:")]
    if not target_goals and not target_categories:
        return None  # No targets = no effect (unchanged on both paths)

    return {
        "id": msg.get("id"),
        "tags": tags,
        "text": str(msg.get("text", "") or ""),
        "target_goals": target_goals,
        "target_categories": target_categories,
        "weight": weight,
        "weight_explicit": weight_explicit,
        # Whether this directive may compel a MUST-SELECT. A NEGATIVE weight is
        # a deprioritisation, so a banner ordering the agent to select it now is
        # self-contradictory -- it stays admitted (scoring still applies the
        # negative bias) but must not compel. Computed HERE, and read as a field
        # by the banner, so the "one predicate" property survives: the banner
        # does not re-derive a weight rule, it consumes one.
        "compels_selection": weight > 0.0,
    }


def load_active_directives():
    """Load active (non-expired) directive messages from the coordination board.

    Returns a list of dicts: [{target_goals: [...], target_categories: [...], weight: float}]
    Admission is delegated to parse_directive_admission -- the SAME predicate
    emit_directive_honor_banner uses, so scoring and the banner cannot diverge.
    """
    if not BOARD_COORD_PATH.exists():
        return []
    directives = []
    now = datetime.now()
    for msg in read_jsonl(BOARD_COORD_PATH):
        d = parse_directive_admission(msg, now)
        if d is None:
            continue
        directives.append({
            "target_goals": d["target_goals"],
            "target_categories": d["target_categories"],
            "weight": d["weight"],
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


def emit_strategic_focus_banner(scored, agent_name):
    """Emit a LOUD stderr STRATEGIC-FOCUS banner when a routine sweep outranks the
    standing directive's own lane (g-115-3251).

    The directive is a PAIRWISE claim -- "product goals outrank routine infra
    sweeps at selection time" -- but strategic_focus_boost above is a PER-GOAL
    SCALAR. A scalar cannot express a pairwise preference: it biases the lane
    against EVERYTHING equally. That is why the weight was deliberately NOT
    raised here. Any value large enough to clear a routine sweep (measured
    shortfall 1.29, zeta 2026-07-28) also overrides the verified-defect work
    that aspirations.yaml's own calibration comment excludes -- so the magnitude
    debate is unresolvable AT the scalar, and the pairwise half belongs here.

    A banner, not a veto: Scorer Sovereignty (g-115-2812) keeps the ranking with
    the scorer. Mirrors emit_directive_honor_banner's compaction-proof posture --
    goal-selector.py runs every iteration, so a bash-emitted line cannot be
    summarized away, whereas the meta-strategy heuristic sh-004 that states the
    same rule is LLM-honor-system only (zero code readers) AND had been
    structurally unfireable: it was written 2026-05-22 for STARVED lanes
    (completion_ratio < 0.40) and asp-335 sits at 0.88.

    "Routine infra sweep" keys on `recurring` alone -- the directive's own noun.
    Deliberately NOT a category enum: sh-004's hardcoded framework/product lists
    are precisely what broke it (on live data BOTH its top-pick category test and
    its challenger category test missed). Returns the emitted warnings for tests.
    Fail-open throughout: never raises, never blocks selection.

    CHALLENGED AND UPHELD 2026-08-02 (foxtrot, g-115-4046; LAPTOP-3IOFCNEO,
    Linux 6.6.87.2-microsoft-standard-WSL2). The recurring-only test was filed as
    a suspected gap: banner silent while ranks 1-8 were all non-recurring asp-115.
    It is not a gap, for two independently sufficient measured reasons.
    (1) The lane was DRAINED, not outranked. goal-selector returned 552 candidates
    of which exactly ONE was asp-335 (g-335-09); the other 28 non-terminal goals
    were legitimately excluded (hypothesis_gate 16 / deferred 9 / dependency 3, per
    `goal-selector.sh blocked`), and asp-335 stood at 611/673 complete. guard-2110:
    an ordering test against a drained lane restates the directive's exit condition
    and is never a compliance signal.
    (2) That sole lane candidate is ITSELF recurring -- a monitoring cadence. A
    widened predicate would have fired the banner recommending one routine sweep
    over another, and over verified-defect work (5 of ranks 1-7 were verified
    defects), realizing exactly the risk the scalar paragraph above declines to
    take. Even had the top pick been recurring, gap was 0.0 and the `gap <= 0`
    guard below returns [] regardless.
    Do not re-litigate without first re-measuring the lane's ELIGIBLE set from
    goal-selector's own candidate list (guard-2110: never hand-roll that predicate).
    """
    if not agent_name or not scored:
        return []
    try:
        lanes = load_strategic_focus()["aspirations"]
    except Exception as e:  # pragma: no cover - fail-open guard
        print(f"[goal-selector] strategic-focus banner skipped "
              f"({type(e).__name__}: {e})", file=sys.stderr)
        return []
    if not lanes:
        return []  # no standing directive, or its prose names no asp-NNN

    def _eligible(s):
        # A goal with no intended_agent is open to anyone; "either" likewise.
        ia = s.get("intended_agent")
        return bool(s.get("routed_to_me")) or ia in (None, "", "either", agent_name)

    mine = [s for s in scored if _eligible(s)]
    if not mine:
        return []
    top = mine[0]
    if not top.get("recurring"):
        return []  # top eligible pick is not a sweep -- directive says nothing
    if top.get("aspiration_id") in lanes:
        return []  # the sweep IS lane work -- already honoring the directive
    # Clause (ii) of the directive: recurring lane goals are NOT eligible
    # nominees (). The directive excludes them from what counts as
    # lane work remaining -- "both re-supply continuously from the lane's own
    # cadence and neither is unbuilt product work" -- so nominating one swaps
    # a routine sweep for a routine sweep, and the "product outranks sweeps"
    # premise cannot discriminate between them. The top-pick side above already
    # tests `recurring`; this is the same field test on the other side of the
    # comparison, which is what the directive calls clause (ii) (a FIELD test,
    # recurring:true, not a title heuristic -- so unlike clause (i) it carries
    # no under-count). Left unfiltered, an agent that complied literally would
    # file a meta-tiebreaker deviation for work the directive excludes, which
    # is what pollutes the Layer-C deviation audit.
    lane = next((s for s in mine
                 if s.get("aspiration_id") in lanes and not s.get("recurring")),
                None)
    if lane is None:
        return []  # no NON-RECURRING lane candidate available to this agent now
    try:
        gap = round(float(top.get("score") or 0.0) - float(lane.get("score") or 0.0), 2)
    except (TypeError, ValueError):
        return []  # non-numeric score -- the docstring promises no raise, so honor it
    if gap <= 0:
        return []  # lane already outranks the sweep -- nothing to correct
    warn = (
        f"[goal-selector] ⚠ STRATEGIC-FOCUS: top pick eligible to {agent_name} is a "
        f"ROUTINE SWEEP ({top.get('goal_id')} '{str(top.get('title'))[:48]}', "
        f"recurring, score {top.get('score')}), outranking lane goal "
        f"{lane.get('goal_id')} '{str(lane.get('title'))[:48]}' "
        f"({lane.get('aspiration_id')}, score {lane.get('score')}) by {gap}. The "
        f"standing user directive says product goals OUTRANK routine infra sweeps "
        f"at selection time. Prefer {lane.get('goal_id')} and claim it with "
        f"--deviation meta-tiebreaker, OR state why the sweep genuinely cannot "
        f"wait. This is bias, not veto -- a real blocker still wins (sh-004)."
    )
    print(warn, file=sys.stderr)
    return [warn]


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
    # _paths.ENVIRONMENT_ID is the canonical world identity (env var, then
    # .env.local) -- do NOT re-derive it here.  first shipped a local
    # _self_env_id() copying insight-trigger-sweep._self_env; both duplicate a
    # constant this module already imports, and the local copy referenced an
    # unimported PROJECT_ROOT that py_compile cannot catch (a NameError, not a
    # syntax error) inside a function whose only guard was `except OSError`.
    # None is a legitimate value and fails OPEN toward visibility -- see
    # peer_surface.routing_tag_targets_agent for why this consumer diverges.
    self_env = ENVIRONMENT_ID
    warnings = []
    for msg in rows:
        # SHARED admission predicate () -- type, expiry, weight and
        # target parsing all come from parse_directive_admission, the same
        # function load_active_directives uses. These two call sites previously
        # decided admission independently and disagreed on the weight clause, so
        # this banner could fire a MUST-SELECT for a directive the scorer had
        # scored at 0.00. They cannot diverge again without changing that one
        # function, which is the point.
        d = parse_directive_admission(msg, now)
        if d is None:
            continue
        if not d["compels_selection"]:
            # Negative weight = a deprioritisation. Scoring still applies it;
            # a MUST-SELECT banner for it would contradict the author. Read as
            # a FIELD, not re-derived here -- see parse_directive_admission.
            continue
        tags = d["tags"]
        text = d["text"]
        # : an explicit routing tag takes PRECEDENCE over a loose
        # prose mention. A directive routed to agent X (requires_action_by:X or
        # a bare agent-name tag) but naming agent Y in an exclusionary prose
        # clause ("X please claim; Y cannot do it") must NOT flag Y — the
        # prose-mention fallback fires ONLY when the directive carries no
        # explicit routing tag. Live incident msg-20260721-211141-bravo-5456
        # (routed requires_action_by:alpha, prose "bravo cannot deploy it well")
        # false-flagged bravo on every selection; self-authored directives
        # (author names self in prose) hit the identical trap.
        # : BOTH predicates route through peer_surface's tag rule so
        # the @env-qualified form the cross-deployment convention recommends is
        # seen here. The third clause below is not redundant with the second: a
        # bare QUALIFIED tag (`zeta@ayoai-mind`, no requires_action_by prefix)
        # fails the plain known_agents membership, so without the agent-part
        # check has_routing_tag would stay False and the prose fallback would
        # fire -- re-opening the  false-flag the fallback gate closed.
        has_routing_tag = (
            any(t.startswith("requires_action_by:") for t in tags)
            or any(t in known_agents for t in tags)
            or any(parse_routing_tag(t)[0] in known_agents
                   for t in tags if "@" in str(t)))
        explicitly_directed = any(
            routing_tag_targets_agent(t, agent_name, self_env) for t in tags)
        directed = explicitly_directed or (
            not has_routing_tag and agent_name.lower() in text.lower())
        if not directed:
            continue
        did = d["id"]
        if did in acked:
            continue  # already honored (ack or justified-deferral)
        target_goals = d["target_goals"]
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
    # Logarithmic scaling preserves differentiation among overdue goals ONLY BELOW the
    # clamp — above it, every goal ties. This sentence claimed "a 72x-overdue goal scores
    # higher than a 4x-overdue one" until 2026-08-11; that was true of the bare log curve
    # and false from the day urgency_max landed, and it is the call-site copy the
    # 2026-07-30 module-docstring correction missed (a docstring fix does not reach its
    # call-site comments — guard-2333). MEASURED at the shipped defaults: the clamp binds
    # at overdue_ratio 2.175 = 3.17x interval, ABOVE the starvation detector's own 2.0x
    # threshold, so this term carries zero ordering information across exactly the
    # population that is starved. Reproduced independently on two boxes: 11 of 12 rows
    # >=2.0x tied at 4.0 spanning 2.12x..97.85x (zeta, cc-02, 527 candidates, 2026-07-30)
    # and 11 of 41 recurring rows tied at the same ceiling (bravo, cc-05, 827 candidates,
    # 2026-08-11). Full derivation in the module docstring; owned by  /
    # , and see the  note at 7b for why the cancellation is exact.
    # DO NOT "fix" the tie by raising or removing urgency_max: the cap is load-bearing
    # (see next paragraph) and raising it only relocates the cancellation point — measured,
    # prior tuning passes did not move the tally ( -> ).
    # urgency_max (, zeta-1477 fix) caps raw at a ceiling so heavily-overdue
    # recurring goals can no longer systematically out-score capped role_affinity
    # (1.5x ceiling × weight 1.0 = 1.5 max contribution) — bounds asymmetry while
    # preserving relative ordering up to the cap point (~3x overdue at default 4.0).
    rec = 0
    overdue_ratio = 0.0  # hoisted (FW-1): exposed in the result dict so the
                         # post-scoring substantive-demotion exemption can read it.
    interval = 0.0       # hoisted (): same reason, one field further. The
                         # exemption needs the INTERVAL as well as the ratio, because
                         # a pure ratio test carries no absolute-time bound — see
                         # apply_substantive_demotion for why that starves monitors.
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
        # INDIVIDUAL-STARVATION RELIEF (). The comment on 7b claims
        # "truly overdue recurring goals overcome this via high recurring_urgency."
        # That is arithmetically impossible at the shipped defaults, and the
        # cancellation is exact: urgency_max and saturation_max_penalty are BOTH
        # 4.0 and carry the SAME 0.8 weight, so at full class saturation the
        # largest urgency a goal can earn (+3.20) is cancelled to the decimal by
        # the largest penalty it can pay (-3.20) — a net of 0.00 no matter how
        # overdue it is. Measured live 2026-07-30 (cc-04): every capped row in the
        # starved population showed exactly +3.20 - 2.40 = +0.80 at saturation
        # 0.75. Raising urgency_max cannot fix this; it just relocates the
        # cancellation point, which is why prior tuning passes did not move the
        # tally (14 -> 25 over the day,  -> ).
        #
        # The category error is that this is a CLASS penalty aimed at recurring
        # goals CROWDING OUT substantive work. A goal that has not fired in 20
        # days is not crowding anything — it is what got crowded out. So scale
        # the penalty down by the goal's OWN staleness, using the exemption bar
        # FW-1 already applies to the very same population (shared predicate, so
        # the two mechanisms cannot drift): before this, FW-1 exempted a 20x-
        # overdue production health probe from demotion while this penalty went
        # on charging it in full.
        rec_sat *= (1.0 - overdue_exemption_level(
            overdue_ratio, interval, RECURRING_CONFIG))
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
    skill_quality_data = _load_skill_quality_cached()
    sq_skills = skill_quality_data.get("skills", {})
    sq_entry = sq_skills.get(skill_name, {})
    sq_aggregate = sq_entry.get("aggregate", {})
    sq_overall = sq_aggregate.get("overall", 0.5)  # default neutral
    raw["skill_affinity"] = (sq_overall - 0.5) * 2  # maps [0,1] to [-1, +1]

    # 13b. directive_boost (cross-agent priority influence from board directives
    # PLUS the standing user directive in team-state strategic_focus, ).
    # Both are "someone with authority said this matters more"; they share the
    # criterion and its 1.5 weight rather than splitting into two knobs.
    # The strategic-focus addend is bound to a NAMED local, not left inline, because
    # 13b-i below keys on IT and not on the composite. guard-2412: a composite
    # criterion hides its addends from the breakdown, so `raw["directive_boost"] > 0`
    # cannot tell a standing user directive apart from a board directive — and only
    # the former carries the precedence claim that justifies waiving a sibling term.
    _sf_boost = strategic_focus_boost(asp.get("id", ""), completion_ratio)
    raw["directive_boost"] = (
        directive_boost_score(goal.get("id", ""), category) + _sf_boost)

    # 13b-i. FLOOR class_balance_bonus AT ZERO INSIDE A LIVE strategic_focus LANE
    # (). The balancer may still BOOST a directive lane; it may never
    # PENALIZE one.
    #
    # MEASURED (foxtrot, LAPTOP-3IOFCNEO / WSL2, 2026-07-30T01:39, 413 candidates):
    # every directive-aligned term favored the product goal (+1.500 directive_boost,
    # +0.500 role_affinity, +0.450 variety_bonus, +0.240 completion_pressure) and one
    # session-local term erased all of them — class_balance_bonus +0.640 on the
    # framework goal vs -1.600 on the product goal, a -2.240 delta that flipped a
    # +1.470 product win into a -0.770 loss. The asymmetry is structural, not an
    # unlucky draw: class_balance_bonus spans raw [-2.0, +2.0] at weight 0.8 = a
    # weighted swing of 3.2, while strategic_focus's entire authority is raw 1.0 at
    # weight 1.5 = +1.5. A term with 2.1x the swing of the directive it is supposed
    # to yield to will outvote it whenever the session is lane-heavy — which is
    # exactly the state OBEYING the directive produces. Obedience fed the term that
    # punished it.
    #
    # THOSE ARE THE CONFIGURED CAPS, AND ONLY THE PENALTY END IS REACHABLE — which
    # makes this clamp SUFFICIENT rather than merely a mitigation. Do not size a
    # future change off the 3.2 figure (fresh-eyes on this very commit, 8d1caf91,
    # first concluded the clamp was insufficient by doing exactly that). Measured
    # 2026-08-11 (bravo, cc-05) against the live targets product 0.40 / framework
    # 0.30 / hygiene 0.15 / research 0.15: cbb = min(max_boost, deficit*max_boost*2)
    # with deficit = target - observed, so on the BOOST side observed >= 0 bounds
    # deficit <= target, and the x2 saturation needs deficit >= 0.5 — which NO
    # configured target reaches. On the PENALTY side observed <= 1 lets deficit reach
    # -(1-target) >= 0.6, which saturates for every class. Reachable weighted extremes:
    #   framework +0.960 / -1.600   hygiene  +0.480 / -1.600
    #   product   +1.280 / -1.600   research +0.480 / -1.600
    # So the worst a NON-product competitor can gain from this term is +0.960, which
    # cannot outvote the directive's +1.5 alone. The term could only ever ATTACK the
    # lane, never defend it hard enough to matter — that asymmetry IS the mechanism,
    # and flooring the penalty is therefore the whole fix, not half of it. Widening
    # the clamp to also floor a competitor's positive bonus would be unnecessary.
    #
    # WHY NOT A WEIGHT CHANGE (the goal's check (b), and the lever to reach for
    # first). Neither weight is the lever. Raising strategic_focus's is already
    # refused, with reasons, by emit_strategic_focus_banner above: the directive is a
    # PAIRWISE claim and the boost is a PER-GOAL SCALAR, so any value large enough to
    # clear a routine sweep also overrides the verified-defect work aspirations.yaml's
    # calibration comment deliberately excludes. Lowering class_balance_bonus's 0.8
    # trades away work-mix balance EVERYWHERE, including the majority of sessions
    # where no directive is active — paying globally to fix a scoped interaction.
    # What is actually wrong is neither magnitude but the ORDERING: two terms with no
    # declared precedence. So bound the interaction where it occurs and leave both
    # weights alone. Same shape as apply_substantive_demotion (FW-1) — a targeted
    # bound expressing a precedence the weighted sum cannot.
    #
    # SELF-RETIRING FOR FREE, which is why the predicate is the function and not a
    # re-derived "is the lane live" test. strategic_focus_boost already returns 0.0
    # when the directive names no such aspiration, when the prose parses to nothing,
    # and when the lane has DRAINED (completion_ratio >= 1.0). Keying on its return
    # inherits all three: no second copy to drift, and stale prose costs nothing here
    # for the same reason it costs nothing there.
    #
    # Fleet-vantage re-measured 2026-08-11T13:32 (bravo, cc-05, one instant, per the
    # directive's own exit rule):  executable excluding recurring + hypothesis
    # goals reads alpha 0, bravo 0, echo 0, foxtrot 2, zeta 1. Not every agent reads
    # zero, so the directive is LIVE and this clamp has a live subject. It is NOT
    # conditioned on that measurement — the predicate above re-derives liveness on
    # every call.
    # The waived amount is recorded because a silently-zeroed term is the same
    # invisibility this goal exists to fix — a reader must be able to see the
    # waiver fired and what it cost. It rides OUT as a top-level candidate field
    # (see `class_balance_penalty_waived` in the return dict), NOT as a raw key:
    # KNOWN_CRITERIA is a manifest of things that GET WEIGHTS, and
    # test_goal_selector_weights_contract asserts raw-keys == manifest. Putting
    # telemetry in `raw` would force a manifest entry, which would in turn tell
    # load_weights that a weight named class_balance_penalty_waived is legitimate
    # — and a deployment adding one would silently start scoring telemetry. The
    # passthrough precedent is `created_at` in the same return dict.
    _cb_penalty_waived = None
    if _sf_boost > 0 and raw.get("class_balance_bonus", 0.0) < 0:
        _cb_penalty_waived = raw["class_balance_bonus"]
        raw["class_balance_bonus"] = 0.0

    # 13b-ii. ALSO FLOOR THE BONUS ON GOALS OUTSIDE A LIVE LANE ().
    # The block above says "the balancer may still BOOST a directive lane; it may
    # never PENALIZE one" — but the balancer decides the ORDERING either way: a
    # bonus on the lane's COMPETITOR moves the comparison exactly as far as a
    # penalty on the lane. 13b-i's own sufficiency argument ("the worst a
    # non-product competitor can gain is +0.960, which cannot outvote the
    # directive's +1.5 ALONE") evaluated the pair in isolation; the composed
    # total also carries role_affinity, which legitimately OPPOSES the directive
    # for some agents. Measured (zeta, cc-02 2026-08-12; reproduced cc-07
    # 2026-08-13 via MIND_AGENT=zeta): framework cbb +1.0×0.8 = 0.800 plus
    # role_affinity delta 0.700 consumed the directive's 1.500 EXACTLY —
    # 9.32 == 9.32, a tie, so the standing user directive decided nothing and
    # the pick fell to tiebreak. Same mechanism as 13b-i, opposite sign:
    # obeying the product directive makes framework under-represented, which
    # feeds the bonus that outranks the directive.
    #
    # SELF-RETIREMENT IS ASYMMETRIC HERE, stated rather than hidden: 13b-i keys
    # on _sf_boost, inheriting all three retiring conditions (no directive /
    # unparseable prose / lane drained) because it fires while scoring a LANE
    # goal, whose own completion_ratio is in scope. This block fires while
    # scoring a NON-lane goal, where the named lane's ratio is NOT in scope —
    # so it inherits only the prose conditions (load_strategic_focus returns an
    # empty aspiration set for absent/unparseable prose) and NOT the drain
    # condition. Cost while drained-but-uncleared prose stands: non-lane goals
    # forgo a positive cbb (reachable weighted max +1.28), a work-mix
    # misallocation bounded by the fleet-vantage directive-hygiene cadence that
    # clears stale prose. Accepted: a stale-prose window costing bounded
    # rebalancing beats a live directive that cannot open a margin.
    _cb_bonus_waived = None
    if _cb_penalty_waived is None and _sf_boost == 0.0 \
            and raw.get("class_balance_bonus", 0.0) > 0:
        _sf = load_strategic_focus()
        if _sf["aspirations"] and _sf["weight"] > 0:
            _cb_bonus_waived = raw["class_balance_bonus"]
            raw["class_balance_bonus"] = 0.0

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
        # : age source for apply_starvation_boost (the anti-starvation
        # post-pass). A one-shot goal's score is otherwise fixed at filing time,
        # so an aged unclaimed HIGH goal never rises. Passthrough only; not a
        # WEIGHTS/scoring field, so guard-760 does not apply.
        "created_at": goal.get("created_at") or goal.get("created"),
        # : producer-set dependency-pull signal, read by apply_pull_boost
        # (the post-scoring pass). Passthrough only, exactly like created_at above —
        # not a WEIGHTS/scoring field, so guard-760 and the KNOWN_CRITERIA contract
        # do not apply. Absent on ~every goal; None is the overwhelming case.
        "pull_signal": goal.get("pull_signal"),
        # : the class_balance penalty waived by 13b-i for a goal inside a
        # LIVE strategic_focus lane (None when no waiver fired — the common case).
        # Telemetry, same posture as created_at above: passthrough only, not a
        # WEIGHTS/scoring field, so guard-760 and the KNOWN_CRITERIA contract do
        # not apply. Present so a waiver is auditable rather than a term that
        # silently went to zero — the invisibility class the goal was filed over.
        "class_balance_penalty_waived": _cb_penalty_waived,
        # : the class_balance BONUS waived by 13b-ii for a goal
        # OUTSIDE a live strategic_focus lane (None when no waiver fired).
        # Same telemetry posture as the penalty field above.
        "class_balance_bonus_waived": _cb_bonus_waived,
        "recurring": bool(goal.get("recurring")),
        "recurring_overdue_ratio": round(overdue_ratio, 3),
        "recurring_interval_hours": round(interval, 3),
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


def apply_starvation_boost(scored, config):
    """Anti-starvation lift for aged, unclaimed, HIGH-priority NON-recurring goals.

    g-115-5426 (2026-08-10). The 21-term score_goal formula has an age-based
    anti-starvation term for RECURRING goals (recurring_urgency / overdue_ratio)
    but NONE for one-shot goals: a one-shot goal's score is essentially fixed at
    filing time, so a lone HIGH goal in a sprawling, low-completion, heterogeneous
    aspiration (canonical: an alert-sweep Unblock in asp-115) never rises and can
    sit unclaimed indefinitely while consolidate-before-expand momentum
    (completion_pressure + tail_bonus + context_coherence) keeps winning. This pass
    gives such a goal a bounded lift that ramps with how long it has been unclaimed,
    so a starved HIGH goal eventually wins selection. This is the missing counterpart
    to guard-1337/guard-1498, which cover starvation of a goal that ranks TOP and is
    deviated-from forever; this covers a goal that never ranks top at all.

    NO-REGRESSION BY CONSTRUCTION: a goal younger than min_age_hours receives ZERO
    boost, so selection is byte-identical for all but the genuine starvation
    population. Recurring goals are skipped (recurring_urgency already covers them).
    The lift ramps linearly from 0 at min_age_hours to max_boost at
    full_boost_age_hours, is scaled by the goal's priority_multiplier (HIGH-only by
    default), and is clamped at max_boost. max_boost defaults BELOW directive_boost's
    4.5 raw ceiling so a fresh user directive still outranks a starved goal.

    Mutates and returns ``scored`` in place; records the lift in each boosted
    candidate's breakdown + raw for telemetry. Same in-place + no-op-when-disabled
    contract as apply_cell_return_boost. created_at is read from the scored entry
    (emitted by score_goal); a missing/unparseable timestamp -> no boost (fail-open).
    """
    if not config.get("enabled"):
        return scored
    min_age = float(config.get("min_age_hours", 12.0))
    full_age = float(config.get("full_boost_age_hours", 36.0))
    max_boost = float(config.get("max_boost", 4.0))
    pmults = config.get("priority_multipliers") or {}
    span = full_age - min_age
    for s in scored:
        if s.get("recurring"):
            continue
        prio_name = _PRIORITY_NUM_TO_NAME.get((s.get("raw") or {}).get("priority"))
        mult = float(pmults.get(prio_name, 0.0)) if prio_name else 0.0
        if mult <= 0:
            continue
        age_h = hours_since(s.get("created_at"))
        if age_h is None or age_h < min_age:
            continue
        ramp = 1.0 if span <= 0 else min(1.0, (age_h - min_age) / span)
        boost = round(mult * max_boost * ramp, 2)
        if boost <= 0:
            continue
        s["score"] = round(s["score"] + boost, 2)
        s.setdefault("breakdown", {})["starvation_boost"] = boost
        s.setdefault("raw", {})["starvation_age_hours"] = round(age_h, 2)
    return scored


def apply_pull_boost(scored, config):
    """EVENT-keyed lift for a consumer goal whose dependency has just materialized.

    g-115-6590 (2026-08-17). Sibling of apply_starvation_boost and deliberately its
    opposite: that pass is TIME-keyed (a goal rises because it has waited), this one
    is EVENT-keyed (a goal rises because the thing it exists to consume has ARRIVED).
    Every other anti-starvation term in this file is time-keyed, so a drain goal
    could only ever fire on its interval, never WHEN a carrier landed.

    NO-REGRESSION BY CONSTRUCTION, and more strongly than the sibling passes: the
    boost requires a ``pull_signal`` dict on the goal, which ~no goal carries, so
    selection is byte-identical for every candidate except the handful a producer
    has explicitly pulled.

    NOT RESTRICTED TO RECURRING GOALS, though the first consumer (g-306-284) is one.
    Adding that condition would buy nothing the mechanism needs and would silently
    no-op for a non-recurring consumer — a surprise with no failure signal. The
    absent condition is the simpler code and the safer behaviour.

    AGE IS A SAFETY VALVE (see load_pull_boost_config for the measurement): a lost
    CLEAR must not pin a goal at rank 1 forever, so a signal older than
    max_age_hours stops lifting on its own.

    THE AGE IS PARSED HERE RATHER THAN VIA hours_since, and that is deliberate.
    hours_since returns None for ANY future timestamp — it folds "stamped ahead of
    me" into "corrupt" (goal-selector.py: "Negative = corrupt timestamp"). That is
    right for its own callers, who ask "how long has this been waiting", but wrong
    here: this signal is written on the PRODUCER's box and read on the CONSUMER's,
    so a producer even seconds ahead of the reader stamps a set_at in the reader's
    future, and inheriting hours_since would silently drop the pull — the exact
    consumer-does-not-receive-what-the-producer-sent failure (guard-3221) this
    mechanism exists inside. Caught by test_small_clock_skew_is_tolerated, which
    failed on the first implementation. So: a SIGNED age, with a bounded skew
    tolerance treated as live, and anything further ahead treated as bogus rather
    than clamped — clamping would let a far-future stamp hold the boost for as long
    as the skew, the unbounded case the valve exists to prevent. hours_since itself
    is left alone; it is a shared helper with many callers and its semantics are
    correct for them.

    Mutates and returns ``scored`` in place; records the lift in breakdown + raw for
    telemetry. Same in-place + no-op-when-disabled contract as the sibling passes.
    A missing/unparseable set_at yields no boost (fail-open, like created_at above).
    """
    if not config.get("enabled"):
        return scored
    boost = float(config.get("boost", 4.0))
    if boost <= 0:
        return scored
    now = datetime.now()
    for s in scored:
        # Liveness (parse + max_age + clock-skew) lives in ONE predicate shared
        # with the recurring hour gate, so rank and eligibility cannot disagree
        # about which signals are live. See pull_signal_live_age_hours.
        age_h = pull_signal_live_age_hours(s.get("pull_signal"), config, now)
        if age_h is None:
            continue
        s["score"] = round(s["score"] + boost, 2)
        s.setdefault("breakdown", {})["pull_boost"] = boost
        s.setdefault("raw", {})["pull_signal_age_hours"] = round(age_h, 2)
    return scored


def apply_fan_in_boost(scored, all_aspirations, config):
    """GRAPH-keyed lift for a goal that other LIVE goals are waiting on.

    g-115-6590 item (2), 2026-08-28. Third sibling of apply_starvation_boost
    (TIME-keyed: a goal rises because it has waited) and apply_pull_boost
    (EVENT-keyed: a goal rises because what it consumes has ARRIVED). This one is
    GRAPH-keyed: a goal rises because other goals cannot start until it lands.

    Nothing in scoring read that before. ``blocked_by`` was consulted only to
    SUPPRESS the dependent (collect_candidates / collect_blocked), never to LIFT
    the blocker, so a READY root holding up five downstream goals competed on its
    own unaided merit and lost. The inverse map already existed but in the WRONG
    COMMAND -- cmd_blocked builds root_groups[...]["downstream_ids"] from
    trace_root_bottleneck, and cmd_select never sees it. That split is the whole
    defect, not a missing computation.

    BLOCKED_BY ONLY, NEVER depends_on (guard-4554). blocked_by is the SEQUENCING
    field and a list of id STRINGS; depends_on is the {goal_id, expects}
    output-passing annotation whose values are DICTS. Teaching any part of this
    selector to read depends_on is explicitly forbidden there: done_ids is a set,
    so a dict union raises TypeError and crashes the fleet's mandatory selection
    entry point. A goal sequenced with depends_on alone LOOKS sequenced and is
    not -- it will not be suppressed and it will not lift its blocker here.

    DECAY IS STRUCTURAL, NOT SCHEDULED. Only NON-TERMINAL dependents are counted,
    so the lift falls as dependents close and is gone when the last one does.
    There is no signal to clear and therefore no lost-clear failure mode -- the
    property apply_pull_boost needs its max_age_hours valve to simulate, obtained
    here for free because the graph IS the state.

    THE DEFAULT IS MEASURED, NOT PICKED (guard-1895 (2): sizing a scorer fix
    against the deterministic deficit rather than the NOISE WIDTH is what makes
    an intervention look like a fix while changing almost nothing). Measured live
    on this queue 2026-08-28 (cc-07, 1368 candidates): weighted exploration_noise
    is ~U(0, 1.220) on 100% of candidates. The acceptance bar is a goal with 3
    HIGH dependents outranking a SAME-PRIORITY lane goal, whose deterministic
    deficit is ~0, so the boost need only clear the noise width -- 3 x 1.2 = 3.6
    clears 1.220 about threefold. The cap holds the total BELOW directive_boost's
    4.5 raw ceiling, so a fresh USER directive still outranks a machine-derived
    fan-in: the same ordering argument both sibling passes make for their own 4.0.

    NO-REGRESSION BY CONSTRUCTION: a goal nothing depends on gets no map entry
    and is scored byte-identically, which on this queue is the overwhelming
    majority of candidates.

    Mutates and returns ``scored`` in place, recording the lift in breakdown and
    the dependent census in raw. Same in-place + no-op-when-disabled contract as
    the sibling passes.
    """
    if not config.get("enabled"):
        return scored
    per_dependent = float(config.get("per_dependent", 1.2))
    cap = float(config.get("cap", 4.0))
    if per_dependent <= 0 or cap <= 0:
        return scored
    pri_w = config.get("priority_weights") or {}

    # Weighted census of LIVE dependents, keyed by the id each one waits on.
    # Active aspirations only, mirroring cmd_select's global_live_ids loop.
    weight_by_blocker = {}
    count_by_blocker = {}
    for asp in all_aspirations or []:
        if asp.get("status") != "active":
            continue
        for g in asp.get("goals", []) or []:
            if g.get("status") in TERMINAL_GOAL_STATUSES:
                continue
            gid = g.get("id")
            w = float(pri_w.get(str(g.get("priority") or "").upper(), 0.0) or 0.0)
            if w <= 0:
                continue
            for bid in _ensure_list(g.get("blocked_by")):
                bid = str(bid)
                if not bid or bid == gid:
                    continue
                weight_by_blocker[bid] = weight_by_blocker.get(bid, 0.0) + w
                count_by_blocker[bid] = count_by_blocker.get(bid, 0) + 1

    if not weight_by_blocker:
        return scored
    for s in scored:
        gid = s.get("goal_id")
        w = weight_by_blocker.get(gid)
        if not w:
            continue
        boost = min(cap, round(per_dependent * w, 2))
        s["score"] = round(s["score"] + boost, 2)
        s.setdefault("breakdown", {})["fan_in_boost"] = boost
        s.setdefault("raw", {})["fan_in_dependents"] = count_by_blocker.get(gid, 0)
        s.setdefault("raw", {})["fan_in_weight"] = round(w, 2)
    return scored


def overdue_exemption_level(ratio, interval_hours, config):
    """How exempt a stale recurring goal is from suppression, on [0.0, 1.0].

    ONE predicate, TWO consumers (g-115-4018): apply_substantive_demotion's
    binary exemption below, and the recurring_saturation relief in score_goal.
    Both answer the same question — "is this goal stale enough that suppressing
    it further would let monitoring rot?" — so they must not be able to drift
    apart. Before they were joined, FW-1 exempted a 20x-overdue PRODUCTION health
    probe from demotion while recurring_saturation went on charging that same
    goal the full class penalty. Two mechanisms, same population, opposite
    verdicts, neither wrong on its own terms.

    Returns 1.0 when fully exempt — either the pure-ratio bar
    (substantive_demotion_overdue_exempt_ratio) or the interval-scoped
    monitor-class bar (g-115-3922) is met. Below that, the larger of the two
    normalized fractions, so relief phases in rather than switching on. Callers
    wanting the original binary test use `>= 1.0`, which is exactly how
    apply_substantive_demotion consumes it — behavior there is unchanged.

    WHY THE SECOND ARM IS INTERVAL-SCOPED AND NOT FLAT ABSOLUTE HOURS
    (carried from the g-115-3922 site this predicate absorbed). The pure ratio
    test has no absolute-time bound, so exempt_ratio 5.0 means elapsed == 6x the
    interval: a 6h monitor stays suppressed until 36h stale, a 24h goal until 6
    days. A monitor's whole value is timeliness, so scale the bar to the interval
    instead of relaxing it globally. Measured 2026-07-29 over 38 live recurring
    candidates: a flat ">=12h overdue" OR exempts 24/38 (baseline 6/38) and
    ">=48h" still exempts 19/38 — a 3-4x relaxation that re-opens the exact
    recurring domination FW-1 was built to prevent (6 of 7 agents reporting it).
    The interval-scoped form exempts 8/38 and by construction cannot touch the
    55 of 61 corpus recurring goals whose interval exceeds the threshold.
    short_interval_hours 0.0 makes the guard `0 < iv <= 0.0` unsatisfiable, so
    behavior is identical to pre-g-115-3922.
    """
    exempt_ratio = float(config["substantive_demotion_overdue_exempt_ratio"])
    short_iv_h = float(config.get("substantive_demotion_short_interval_hours") or 0.0)
    short_ratio = float(config.get("substantive_demotion_short_interval_exempt_ratio") or 0.0)
    frac = 0.0
    if exempt_ratio > 0:
        frac = ratio / exempt_ratio
    if 0 < interval_hours <= short_iv_h:
        # short_ratio <= 0 reproduces today's `ratio >= 0` guard, which is
        # unconditionally true for a monitor-class interval.
        if short_ratio <= 0:
            return 1.0
        frac = max(frac, ratio / short_ratio)
    return min(max(frac, 0.0), 1.0)


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
    # The three exemption knobs are read by overdue_exemption_level, not here
    # () — keeping local copies would be a second place to update.
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
        if not s.get("recurring") or s["score"] <= cap:
            continue
        ratio = float(s.get("recurring_overdue_ratio", 0.0))
        iv = float(s.get("recurring_interval_hours", 0.0))
        # Both exemption arms now come from the shared predicate () so
        # this and the recurring_saturation relief cannot diverge. `>= 1.0` is the
        # binary form and is behavior-identical to the two inline tests it
        # replaced; see overdue_exemption_level for why they were joined.
        if overdue_exemption_level(ratio, iv, config) >= 1.0:
            continue  # genuinely-stale monitoring must surface
        s.setdefault("breakdown", {})["substantive_demotion"] = round(cap - s["score"], 2)
        s.setdefault("raw", {})["substantive_demotion_pre_score"] = s["score"]
        s["raw"]["substantive_demotion_applied"] = True
        s["score"] = cap
    return scored


def candidate_sort_key(x):
    """Sort key for the final candidate ranking: highest score first, then MOST
    OVERDUE first among equal scores, then lower aspiration id, then lower goal
    id. Non-recurring rows carry recurring_overdue_ratio 0.0, so their relative
    order is byte-identical to the old (-score, aspiration_id, goal_id) key;
    only a tie that INCLUDES a recurring row moves, and it moves toward the
    stalest row — the same ordering apply_drain_lane already uses.

    The overdue term exists because apply_substantive_demotion writes
    `score = cap` onto EVERY non-exempt recurring row above the cap, so the tie
    it produces is by construction all-recurring and the rows' ordering
    information is gone by the time the sort runs (apply_drain_lane's docstring
    says the same of the urgency cap). Under the old key that cluster was
    ordered by aspiration-id STRING — unrelated to staleness, and a fixed
    penalty on high-numbered aspirations. Measured 2026-08-17 (alpha, cc-09,
    the g-306-284 stall): the reducer-only worker-ref drain lane (interval 8h)
    carried the HIGHEST pre-demotion score of all ~1150 candidates in four
    consecutive selector runs (15.45-16.02 vs a top substantive of
    13.36-14.16), was demoted to the cap every time, and then sorted LAST of
    the tied cluster in every run because "asp-306" > "asp-115" — behind rows
    0.8x overdue while it stood at 1.7x. It went 22h unpicked while its
    dependents (g-115-6466, g-115-6471, g-115-6472) waited on the merge.
    """
    return (
        -x["score"],
        -float(x.get("recurring_overdue_ratio") or 0.0),
        x["aspiration_id"],
        x["goal_id"],
    )


def _drain_lane_state_path(agent_dir):
    return None if agent_dir is None else agent_dir / "session" / "drain-lane-state.json"


def read_drain_lane_state(agent_dir):
    """Read the persisted lane cadence counter. Fail-open to a fresh counter."""
    p = _drain_lane_state_path(agent_dir)
    if p is None:
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def write_drain_lane_state(agent_dir, state):
    """Atomically persist the lane cadence counter (tempfile + os.replace, the
    same durability pattern as write_scorer_verdict). FAIL-OPEN: a write failure
    is swallowed to stderr. Worst case the counter does not advance, which makes
    the lane fire LESS often — the safe direction for an anti-flood guard."""
    p = _drain_lane_state_path(agent_dir)
    if p is None:
        return
    # (d): a cross-agent probe must not advance a partner's cadence
    # counter — that CONSUMES the partner's next real drain-lane slot. The
    # READ above is deliberately left alone: suppressing only the write keeps
    # the probe's returned ordering identical to what the agent itself would
    # see, so the measurement stays faithful while the side effect stops.
    if not _agent_is_resident():
        _suppress_cross_agent_write("drain-lane state")
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".drain-lane-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception as e:  # pragma: no cover - defensive
        print(f"[goal-selector] drain-lane state write failed "
              f"({type(e).__name__}: {e})", file=sys.stderr)


def apply_drain_lane(scored, config, agent_dir):
    """Bounded drain lane (; decision (b)).

    REORDERS the already-sorted list so ONE genuinely-starved recurring goal
    takes the top slot, at most once per K invocations. Returns the promoted
    row, or None.

    WHY REORDER AND NOT RESCORE. Two clamps downstream of recurring_urgency each
    flatten distinct overdue ratios onto a single value — the urgency cap
    (g-115-4047: 16/21 starved rows spanning 5.1x-78.7x all land on +3.20) and
    apply_substantive_demotion's `score = cap` above (g-115-4045). So no
    score-side change can restore drain ORDERING; the information is already
    gone by the time the sort runs. Keying on overdue ratio here, after the
    sort, bypasses both clamps. It also makes acceptance bucket 3 true by
    construction: this function never writes `score`, so every non-lane pick is
    byte-identical to pre-lane behavior.

    ELIGIBILITY reuses overdue_exemption_level — the SAME predicate FW-1
    demotion-exemption and the g-115-4018 saturation relief consume — so the
    three mechanisms cannot drift apart. Mind the scale: that predicate compares
    the SELECTOR's overdue_ratio = (age - interval)/interval, which is exactly
    1.0 LOWER than the ratio recurring-starvation-check headlines (age/basis).
    Authoring an eligibility expectation from the detector's number is
    guard-2004's third trap, and it already bit this goal's own acceptance
    criteria: two of the five rows originally listed as past-exempt measured
    4.57 and 4.76 on this scale and are NOT eligible.

    CADENCE counts selector INVOCATIONS, not wall-clock iterations. An ad-hoc
    diagnostic run therefore advances the counter. That is deliberate and
    stated rather than engineered around: the guarantee is "at most one lane
    pick per K invocations", which still bounds the flood, and the alternative
    (an iteration id the selector does not own) would couple this to loop state
    it cannot see.

    Existing filters are untouched — blocked/deferred/claimed/intended_agent
    routing all still apply upstream, so the lane can only promote a row that
    was already a legitimate candidate for THIS agent.
    """
    if not scored or not config.get("drain_lane_enabled", True):
        return None
    k = int(config.get("drain_lane_interval_iterations") or 0)
    if k <= 0:
        return None  # non-positive K disables the lane rather than dividing by it

    state = read_drain_lane_state(agent_dir)
    try:
        since = int(state.get("invocations_since_pick") or 0)
    except Exception:
        since = 0
    since += 1

    #  outcome (a): a live pull_signal is an INDEPENDENT admission to
    # this lane, ORed with the time-keyed exemption above it. This is the
    # design's FIRST option ("treat a recurring goal with a live pull_signal as
    # fully overdue-exempt"), and it belongs HERE rather than in
    # overdue_exemption_level: that helper takes (ratio, interval_hours, config)
    # and never sees the goal, which is why the exemption was previously read as
    # unshippable. The CALL SITE has the row, so no signature change is needed.
    #
    # WHY NOT A BIGGER / RELATIVE SCORE BOOST, the spec's other option. Measured
    # on this queue 2026-08-28 (1372 candidates): exploration noise spans
    # 0.010-1.220, so score-side lift is a max-of-N lottery (guard-1895), and
    # sizing a boost against `top_sub` inherits ONE noise draw into a bound that
    # is then ASSIGNED to a whole cohort — the same shape guard-1895 measured as
    # strictly worse than leaving it noisy, plus the  collapse where
    # every lifted row lands on one value and intra-cohort order degenerates to
    # alphabetical. This lane REORDERS and never writes `score`, so it bypasses
    # the lottery and both clamps entirely, which is guard-1895's own prescribed
    # remedy: remove the item from the competition rather than try to win it.
    def _pull_live(s):
        return pull_signal_live_age_hours(
            s.get("pull_signal"), PULL_CONFIG) is not None

    eligible = [
        s for s in scored
        if s.get("recurring")
        and (
            overdue_exemption_level(
                float(s.get("recurring_overdue_ratio") or 0.0),
                float(s.get("recurring_interval_hours") or 0.0),
                config,
            ) >= 1.0
            or _pull_live(s)
        )
    ]

    picked = None
    if eligible and since >= k:
        # MOST overdue first; deterministic tiebreak mirrors the main sort so a
        # tie can never reorder run-to-run.
        # PULLED FIRST, then most-overdue. A pull says the dependency this goal
        # exists to consume has ARRIVED; an overdue ratio says only that time has
        # passed. A pulled goal is typically NOT overdue (ratio 0.0), so without
        # this key it would sort LAST among eligible rows and never take the slot
        # it was just admitted to — admission without ordering is a no-op.
        # The cadence bound (`since >= k`) is deliberately NOT bypassed: it is
        # the anti-flood guarantee, and one lane pick per K invocations still
        # bounds a producer that sets many signals.
        eligible.sort(key=lambda s: (
            0 if _pull_live(s) else 1,
            -float(s.get("recurring_overdue_ratio") or 0.0),
            str(s.get("aspiration_id") or ""),
            str(s.get("goal_id") or ""),
        ))
        picked = eligible[0]
        if scored[0] is not picked:
            scored.remove(picked)
            scored.insert(0, picked)
        picked["drain_lane_pick"] = True
        since = 0

    write_drain_lane_state(agent_dir, {
        "invocations_since_pick": since,
        "k": k,
        "eligible_count": len(eligible),
        "last_pick_goal_id": (picked or {}).get("goal_id") or state.get("last_pick_goal_id"),
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    })
    return picked


def emit_drain_lane_banner(picked, eligible_count, since, k):
    """stderr-only (stdout JSON is what the orchestrator parses). Says WHY the
    top pick is not the scorer's, so the LLM does not read a lane pick as a
    scoring anomaly. Silence when the lane did not fire is deliberate: a banner
    on every quiet iteration would train the reader to skip it."""
    if picked is None:
        return
    print(
        "[goal-selector] DRAIN-LANE: promoted {gid} to top — recurring goal "
        "{r:.2f}x overdue on the SELECTOR scale ((age-interval)/interval), past the "
        "exemption bar, and {n} eligible row(s) were starved. Scores are UNCHANGED; "
        "only ordering moved, for this one slot, at most once per {k} invocations. "
        "This IS the sanctioned top pick — claim it without a deviation code.".format(
            gid=picked.get("goal_id"), r=float(picked.get("recurring_overdue_ratio") or 0.0),
            n=eligible_count, k=k),
        file=sys.stderr,
    )


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
    # (d): the highest-consequence of the three writes. This file is
    # the claim chokepoint's input, so a cross-agent probe overwriting it aims
    # the partner's NEXT claim at a verdict computed from THIS box's vantage —
    # a scorer-sovereignty refusal on the partner's own legitimate top pick.
    if not _agent_is_resident():
        _suppress_cross_agent_write("scorer-verdict")
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


def write_scorer_verdict_banners(banners, agent_dir):
    """Additively record the banner emitters' RETURNS onto the verdict sidecar
    so the banners survive loss of stderr (g-115-4296).

    WHY A SECOND WRITE instead of folding this into write_scorer_verdict: that
    writer runs BEFORE both emitters deliberately ("so it can never disturb the
    pinned emit_directive_honor_banner call site", g-115-2807), and the banners
    do not exist yet at that point. Reordering would move the pinned call site;
    an additive second write does not. The sidecar is already fail-open and its
    only consumer reads just top_goal_id + ts, so an extra key is tolerated and
    a failure here cannot affect selection or the claim gate.

    THE TWO EMITTERS RETURN DIFFERENT SHAPES, AND THAT IS PRESERVED RATHER THAN
    NORMALISED. emit_directive_honor_banner returns structured records
    ({directive_id, goal_id, rank}) and PRINTS its prose without ever returning
    it; emit_strategic_focus_banner returns its full banner TEXT. Reshaping the
    former is not available: test_goal_selector_directive_honor_banner.py
    asserts on warns[0]["directive_id"]/["goal_id"]/["rank"] and on `== []`, so
    changing that return breaks the very pin this placement exists to protect.
    Nothing actionable is lost — the prose is a rendering of those three fields
    plus static boilerplate.

    THE KEY IS WRITTEN EVEN WHEN BOTH LISTS ARE EMPTY, and that is load-bearing.
    An ABSENT `banners` key means the sidecar predates this feature or the second
    write failed; a PRESENT key holding empty lists means the emitters ran and
    had nothing to say. Collapsing those two states would leave the sidecar
    unable to answer the one question it was added to answer.

    `errors` is recorded for the same reason: an emitter that RAISED reports only
    to stderr, which is exactly the channel this function exists to backstop, so
    an exception would otherwise be indistinguishable from "returned nothing".

    ONLY EVER CALLED FROM cmd_select. `goal-selector.sh blocked` must keep
    touching neither the drain-lane state nor this sidecar — guard-2545 measured
    that byte- and mtime-identical, and a prescribed cross-check ritual depends
    on it — so do not call this from any other subcommand.
    """
    if agent_dir is None or not banners:
        return
    if not _agent_is_resident():
        _suppress_cross_agent_write("scorer-verdict-banners")
        return
    try:
        session_dir = agent_dir / "session"
        target = session_dir / "scorer-verdict.json"
        if not target.exists():
            return  # primary write no-op'd (no candidates) — nothing to append to
        with open(target, encoding="utf-8") as fh:
            verdict = json.load(fh)
        if not isinstance(verdict, dict):
            return  # never overwrite a sidecar whose shape we do not recognise
        verdict["banners"] = banners
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
        print(f"[goal-selector] scorer-verdict banner write error "
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
    global_done_ids, global_live_ids = global_goal_id_sets(all_aspirations)
    # Supersession-aware done-ness (). Additive union; see the
    # function docstring for why this is the ONE expansion point.
    global_done_ids = expand_done_ids_via_supersession(all_aspirations, global_done_ids)

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
        global_done_ids_retry, global_live_ids_retry = global_goal_id_sets(
            all_aspirations_retry)
        # Supersession-aware done-ness () — mirrors the primary
        # build above; the retry path must not resolve dependencies
        # differently from the pass it is retrying.
        global_done_ids_retry = expand_done_ids_via_supersession(
            all_aspirations_retry, global_done_ids_retry)
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
            #  STEP 3: emit the SIBLING record. Before this, only the
            # success branch above logged, so "retry also empty" had no event and
            # no counter — its count was zero BY CONSTRUCTION, not by measurement,
            # and the failure mode under investigation was structurally
            # unobservable. Any backoff tuned against that metric would have been
            # tuned against a number that could not move (which is why STEP 4 is
            # explicitly ordered after this one).
            _log_transient_allblocked_recovery(
                world_aspirations, world_retry, 0,
                event="transient_all_blocked_retry_also_empty")
            # (Report blocked goals from the fresh retry read for consistency.)
            blocked = collect_blocked(all_aspirations_retry, known_blockers=known_blockers,
                                      global_done_ids=global_done_ids_retry,
                                      defer_reason_timeout_hours=defer_reason_timeout_hours,
                                      dependency_timeout_hours=dependency_timeout_hours,
                                      reallocation_hours=reallocation_hours,
                                      global_live_ids=global_live_ids_retry)
            if blocked:
                print(json.dumps({
                    "candidates": [],
                    "all_blocked": True,
                    "blocked_count": len(blocked),
                    "by_reason": _blocked_reason_counts(blocked),
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

    # Anti-starvation lift (): rescue aged, unclaimed HIGH-priority
    # NON-recurring goals that never rise under the fixed one-shot score (recurring
    # goals already have recurring_urgency). Runs AFTER the other boosts and BEFORE
    # the sort so the lift drives ranking; a goal younger than min_age_hours gets
    # zero boost, so normal selection is byte-identical (no-regression by
    # construction). See apply_starvation_boost.
    apply_starvation_boost(scored, STARVATION_CONFIG)

    # Dependency-pull lift (): the EVENT-keyed counterpart to the
    # time-keyed passes above — a consumer goal rises the moment a producer says the
    # dependency it exists to drain has ARRIVED. Runs LAST among the boosts and
    # BEFORE the sort so the lift drives ranking; a goal with no pull_signal (which
    # is ~all of them) gets zero boost, so selection is byte-identical outside the
    # pulled set. See apply_pull_boost.
    apply_pull_boost(scored, PULL_CONFIG)
    apply_fan_in_boost(scored, all_aspirations, FAN_IN_CONFIG)

    # Sort: highest score first, then MOST OVERDUE first among equal scores, then
    # lower aspiration id, then lower goal id. See candidate_sort_key for why
    # the overdue tiebreak exists (the  stall, 2026-08-17).
    scored.sort(key=candidate_sort_key)

    # Bounded drain lane (, decision (b)). Runs AFTER the sort
    # because it REORDERS rather than rescores, and BEFORE write_scorer_verdict
    # below so the sidecar records the lane pick as the sanctioned top — the claim
    # gate then accepts it without a deviation code, which is the intended
    # semantics: when the lane fires, its pick IS the top pick. Wrapped defensively
    # for the same reason as the banners — a lane bug must never suppress the
    # ranked-candidate output that every agent depends on each iteration.
    try:
        _lane_k = int(RECURRING_CONFIG.get("drain_lane_interval_iterations") or 0)
        _lane_pick = apply_drain_lane(scored, RECURRING_CONFIG, AGENT_DIR)
        if _lane_pick is not None:
            _lane_elig = sum(
                1 for s in scored
                if s.get("recurring")
                and overdue_exemption_level(
                    float(s.get("recurring_overdue_ratio") or 0.0),
                    float(s.get("recurring_interval_hours") or 0.0),
                    RECURRING_CONFIG) >= 1.0
            )
            emit_drain_lane_banner(_lane_pick, _lane_elig, 0, _lane_k)
    except Exception as e:  # pragma: no cover - defensive; lane must never block
        print(f"[goal-selector] drain-lane error "
              f"({type(e).__name__}: {e})", file=sys.stderr)

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
    # Return values captured () for write_scorer_verdict_banners below.
    # Both emitters report ONLY to stderr, which an ad-hoc invocation carrying a
    # hand-typed redirect discards silently — that is the failure mode the sidecar
    # backstops. `errors` is carried for the same reason: an emitter that RAISED
    # also reports only to stderr, so without it an exception would be
    # indistinguishable from "ran and had nothing to say".
    banners = {"directive_honor": [], "strategic_focus": [], "errors": []}

    try:
        banners["directive_honor"] = emit_directive_honor_banner(scored, AGENT_NAME) or []
    except Exception as e:  # pragma: no cover - defensive; banner must never block
        banners["errors"].append(f"directive_honor: {type(e).__name__}: {e}")
        print(f"[goal-selector] directive-honor banner error "
              f"({type(e).__name__}: {e})", file=sys.stderr)

    # STRATEGIC-FOCUS banner (): the pairwise half of the standing user
    # directive, which the per-goal strategic_focus_boost scalar structurally
    # cannot express. Separately wrapped so it can never disturb the pinned
    # emit_directive_honor_banner call site above ().
    try:
        banners["strategic_focus"] = emit_strategic_focus_banner(scored, AGENT_NAME) or []
    except Exception as e:  # pragma: no cover - defensive; banner must never block
        banners["errors"].append(f"strategic_focus: {type(e).__name__}: {e}")
        print(f"[goal-selector] strategic-focus banner error "
              f"({type(e).__name__}: {e})", file=sys.stderr)

    # Second, ADDITIVE sidecar write (). It must stay AFTER both
    # emitters — that ordering is the entire reason it is a separate writer from
    # write_scorer_verdict above, whose placement BEFORE them is itself pinned
    # (). Fail-open; never reached by cmd_blocked (guard-2545).
    write_scorer_verdict_banners(banners, AGENT_DIR)

    print(json.dumps(scored, indent=2, ensure_ascii=False))


def cmd_blocked(args):
    """List all blocked goals with reasons. Output: JSON with blocked_goals and by_reason."""
    empty_reasons = {r: {"count": 0, "goal_ids": []} for r in BLOCKED_PRESET_REASONS}
    empty_reasons["dependency"]["head_count"] = 0
    empty_reasons["dependency"]["downstream_count"] = 0

    # Load expiry config (same source as cmd_select)
    defer_reason_timeout_hours = None
    dependency_timeout_hours = None
    # reallocation_hours feeds collect_blocked's routed_to_agent branch, whose
    # idle-owner escape must match the one cmd_select applies in
    # collect_candidates. Loading it here (rather than leaving the kwarg at its
    # None default) is what keeps the `blocked` CLI view consistent with the
    # `select` view: with None the idle set is empty, so a goal whose owner IS
    # idle would be a candidate under select AND blocked under this command --
    # the exact candidate-XOR-blocked violation the branch exists to prevent.
    reallocation_hours = None
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
            rh = ma.get("reallocation_hours")
            if rh is not None:
                reallocation_hours = float(rh)
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
    global_done_ids, global_live_ids = global_goal_id_sets(aspirations)
    # Supersession-aware done-ness () — same expansion the selection
    # path uses, so `blocked` diagnostics never disagree with selection.
    global_done_ids = expand_done_ids_via_supersession(aspirations, global_done_ids)

    blocked = collect_blocked(aspirations, known_blockers=known_blockers,
                              global_done_ids=global_done_ids,
                              defer_reason_timeout_hours=defer_reason_timeout_hours,
                              dependency_timeout_hours=dependency_timeout_hours,
                              reallocation_hours=reallocation_hours,
                              global_live_ids=global_live_ids)

    # Count total non-terminal goals across active aspirations
    total_active = 0
    for asp in aspirations:
        if asp.get("status") != "active":
            continue
        for g in asp.get("goals", []):
            if g.get("status") not in ("completed", "skipped", "expired", "decomposed"):
                total_active += 1

    # Group by reason — reason set from the shared canonical counter (preset 5
    # always present, observed extras added dynamically) so this tally can never
    # again omit a class collect_blocked emits. See _blocked_reason_counts for
    # the 2026-08-21 drift this closes.
    by_reason = {}
    for reason in _blocked_reason_counts(blocked):
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
