#!/usr/bin/env python3
# pyright: strict
r"""Routing-Audit Target-Status Sweep (g-115-1353, rb-1478, incident g-115-1329).

`post-decompose-routing-audit.py` files `Investigate: routing-mismatch <target>`
and `Investigate: routing-either-resolve <target>` goals into asp-115 when a
freshly-stamped goal's `intended_agent` disagrees with the best Self.md
domain-token Jaccard match. The audit goal's primary action is to re-stamp the
TARGET goal's `intended_agent` (aspirations-update-goal.sh <target> intended_agent
<best_agent>). When the target subsequently lands in a terminal status
(completed / archived / skipped / superseded), the re-stamp is MOOT — you cannot
meaningfully re-route a goal that already executed — and the audit goal survives
as actionable work whose premise has dissolved.

Canonical incident (rb-1478 / exp-g-115-1329):
    g-115-1329 resolved that routing-either-resolve fired a re-stamp (either ->
    delta) on g-115-1328 which was ALREADY completed 2026-06-03 (re-stamp moot)
    AND content-contradicted (g-1328 is framework-hygiene, NOT delta's lane;
    +0.0192 Jaccard = token contamination). This is the "terminal-target"
    sub-mode: the audit goal OUTLIVED its target's completion. Distinct from the
    content-FP-on-a-PENDING-target sub-mode (g-115-1346) and from the metric-bias
    root (rb-1249 / g-115-1200).

This sweep is the EXACT mirror of unblock-parent-status-sweep.py (Phase 0.5b.7),
adapted from the Layer-D `Unblock: <verb> for <parent-id>` goal class to the
routing-audit `Investigate: routing-(mismatch|either-resolve) <target-id>` goal
class. It joins the rb-428 sweep family:
    - blocker-recheck.py            — Layer C for participants:[user] blockers
    - defer-recheck.py              — explicit dep-chain narrative defers
    - precondition-defer-recheck.py — structured precondition_unmet
    - monitor-stale-check.py        — Monitor proc-NNN goals past current run_dir
    - pending-questions-sweep.py    — source_goal-completed lifecycle
    - parent-supersession-sweep.py  — sibling decomposition supersession
    - unblock-parent-status-sweep.py— Layer D Unblock parent terminal
    - routing-audit-target-status-sweep.py — THIS sweep (audit target terminal)

Class-membership signal (most reliable first):
    1. discovered_by == "post-decompose-routing-audit"  (constant set on BOTH
       audit goal shapes by _build_investigate_spec / _build_either_resolve_spec)
    2. origin_signal "routing-mismatch:<g-id>" / "routing-either-resolve:<g-id>"
    3. title  "Investigate: routing-(mismatch|either-resolve) ..."

Target-id extraction (priority order):
    1. origin_signal "routing-(mismatch|either-resolve):<g-id>"  (machine-emitted,
       most reliable — _build_*_spec sets it verbatim)
    2. title  "routing-(mismatch|either-resolve) <g-id> ..."     (fallback)
    NOTE: discovered_by is the CONSTANT "post-decompose-routing-audit", NOT a
    goal-id, so it is NOT a target-extraction source (contrast the unblock sweep,
    where discovered_by carries the parent id for legacy/manual unblocks).

Target terminal-state set:
    {'completed', 'archived', 'skipped', 'superseded'}

Second close reason — intended_agent corrected while pending (g-115-1529):
    post-decompose-routing-audit fires the routing-mismatch Investigate at
    decompose time, but the owning agent's SAME decompose flow often self-corrects
    target.intended_agent to the audit's recommended agent moments later (canonical
    g-315-219: stamped alpha 03:52:09, audit filed 03:52:10, echo corrected
    alpha->echo at 03:52:56 — 46s). The Investigate then outlives the mismatch as a
    moot-but-live goal even though the target is still PENDING (terminal-only
    auto-close never reaches it). FIX: also auto-close when the target's CURRENT
    intended_agent == the audit's recommended agent (parsed from the description
    clause "aspirations-update-goal.sh <target-id> intended_agent <best_agent>" via
    RECOMMENDED_AGENT_PATTERN). Safe by definition: if intended_agent now equals the
    recommendation, the re-stamp the audit asked for already happened, so the flagged
    mismatch is resolved. Conservative — matches ONLY the audit's specific
    recommendation; a correction to some OTHER agent leaves the goal open. Cross-ref
    rb-1478 (82pct FP terminal-target sub-mode), rb-1249 / g-115-1200 (Jaccard
    metric-bias root).

Re-stamp vs table-extension nuance (why auto-skip is still correct): a
routing-mismatch goal carries TWO candidate actions — (a) re-stamp the target's
intended_agent (moot once terminal), and (b) extend capability_route's tables
(a systemic fix). Auto-skipping on terminal target retires (a). For (b): per
rb-1478 the mismatch path is 82% false-positive (spurious competing-Jaccard,
not a real routing error needing a table fix), and a genuine systemic table-gap
RE-FIRES on the next decompose that hits it (the audit runs on every decompose).
The sibling unblock-parent-status-sweep already accepts this same tradeoff for
Unblock goals that could also encode systemic fixes. Net: the dominant case
(FP audit, target done) is correctly auto-closed; the rare systemic-gap signal
is preserved by re-firing, not by leaving a stale moot goal pending.

Action modes:
    --report (default): print JSON, no mutation
    --apply: mark each candidate status=skipped with outcome_note
        "routing-audit target resolved without action needed <detail>" where
        <detail> is "(target_id=<X>, target.status=<Y>)" for the terminal reason
        OR "(target_id=<X>, intended_agent corrected to <Z>)" for the corrected
        reason. The OUTCOME_NOTE_PREFIX is constant across both reasons, so the
        idempotency check holds: if outcome_note already starts with the phrase,
        skip rewrite.

Single-writer / fail-quiet (rb-428 family):
    - One Python pass over active queues per call
    - Metric writes use locked_append_jsonl — fail-open on metric error
    - Update failures log but do not abort the sweep
    - Always exits 0 (reporting tool)

Exit: 0 always. JSON result:
    {
      "scanned": N,        # total routing-audit goals examined
      "eligible": N,       # passed age + target_id parse filters
      "candidates": [...], # target in terminal state (recommend skip)
      "applied": N,        # audit goals marked skipped (apply mode only)
      "details": [...],    # per-goal trace incl. target status
    }

CLI:
    routing-audit-target-status-sweep.py [--max-age-hours N] [--apply]
                                         [--output json|human]
                                         [--metrics-log PATH | --metrics-log ""]
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

from _paths import WORLD_DIR  # noqa: E402
from _fileops import locked_append_jsonl  # noqa: E402

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _dt import parse_naive_iso  # noqa: E402  (shared tzinfo-stripping naive-ISO parse, )
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

#  lost-update guard. This sweep has the SAME scan-then-write shape as
# unblock-parent-status-sweep, where the race was measured: `_mark_skipped`
# rewrites outcome_note and flips status on a candidate chosen at scan time,
# with nothing re-checking that the goal is still open at WRITE time. Same
# blast radius too — a destroyed completion record on a goal another box just
# finished. The refusal POLICY and the authoritative READ are both shared; only
# the two thin seams below are local, so this file's `_read_aspirations` and
# `_is_owncloud_backend` stay monkeypatchable (guard-2385).
from _team_state import _is_owncloud_backend  # noqa: E402
from _sweep_write_guard import (  # noqa: E402
    reread_goal_authoritative as _shared_reread_goal_authoritative,
    stale_candidate_reason as _shared_stale_candidate_reason,
)


# discovered_by constant set by post-decompose-routing-audit.py on both shapes
ROUTING_AUDIT_DISCOVERER = "post-decompose-routing-audit"
# origin_signal: exact forms emitted by _build_investigate_spec /
# _build_either_resolve_spec — captures the TARGET goal-id in group 2.
# `(?:-[a-z])?` matches the SSOT (aspirations.py GOAL_ID_RE). Both patterns in
# this file need it, and they fail DIFFERENTLY, which is why one fix is not
# enough (, measured cc-07 2026-08-10 on live goal  whose
# signal is `routing-either-resolve:-a`):
#   - THIS one is anchored on \s*$, so a suffixed id does not match AT ALL and
#     the record falls through to the title fallback below. A silent miss.
#   - The title fallback is UNANCHORED, so it then captures `` — a
#     goal that DOES NOT EXIST — and the sweep decides a status question about
#     a nonexistent record. That is the truncation class guard-2414 names, and
#     it is reached precisely BECAUSE the anchored pattern above declined.
# A fallback that is laxer than its primary converts a miss into a wrong
# answer; fixing only the anchored one would have left that intact.
ORIGIN_SIGNAL_PATTERN = re.compile(
    r"^routing-(?:mismatch|either-resolve):(g-\d+-\d+(?:-[a-z])?)\s*$")
# Title prefix: "Investigate: routing-(mismatch|either-resolve) ..."
TITLE_CLASS_PATTERN = re.compile(
    r"^\s*Investigate\s*:\s*routing-(?:mismatch|either-resolve)\b", re.IGNORECASE)
# Title target fallback: "routing-(mismatch|either-resolve) <g-id> ..."
TITLE_TARGET_PATTERN = re.compile(
    r"routing-(?:mismatch|either-resolve)\s+(g-\d+-\d+(?:-[a-z])?)\b")

# : the audit's RECOMMENDED agent, parsed from the description clause
# both spec builders (post-decompose-routing-audit._build_investigate_spec /
# _build_either_resolve_spec) emit verbatim:
#   "aspirations-update-goal.sh <target-id> intended_agent <best_agent>"
# (investigate form closes the clause with ')', either-resolve with '.'). The
# whitespace BEFORE the agent name is load-bearing: the STAMPED agent is written
# "intended_agent=<x>" (equals, no space), so this pattern can never capture the
# stamped value — only the re-stamp recommendation.
RECOMMENDED_AGENT_PATTERN = re.compile(
    r"aspirations-update-goal\.sh\s+g-\d+-\d+\s+intended_agent\s+([a-z]+)")

# Target states that imply "no routing-audit action is needed"
TERMINAL_STATES = {"completed", "archived", "skipped", "superseded"}

# Idempotency / outcome-note phrase (kept short so the prefix check is stable)
OUTCOME_NOTE_PREFIX = "routing-audit target resolved without action needed"


def _resolve_metrics_log(cli_path):
    """Resolve metrics log path. Mirrors unblock-parent-status-sweep convention."""
    if cli_path == "":
        return None
    if cli_path is not None:
        return Path(cli_path)
    return Path(WORLD_DIR) / "routing-audit-target-status-sweep-metrics.jsonl"


def _append_metric(path, record):
    """Append one metric record. Fail-open by design."""
    if path is None:
        return
    try:
        locked_append_jsonl(str(path), record)
    except Exception as e:
        print(f"[routing-audit-target-status-sweep] WARN: metrics append failed: {e}",
              file=sys.stderr)


def _run(argv, input_text=None):
    result = subprocess.run(argv, input=input_text, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    return result.returncode, result.stdout, result.stderr


def _py(args, input_text=None):
    return _run([sys.executable] + args, input_text=input_text)


def _tolerant_decode(source, raw):
    """-tolerant decode for daemon aspirations_read body.

    Mirrors unblock-parent-status-sweep::_tolerant_decode verbatim (contract per
    g-115-797-A5 / guard-383 / rb-774): empty body -> None; valid JSON (list OR
    dict with "aspirations" key) -> as-is; valid prefix + trailing garbage
    (g-115-766 shape) -> recovered prefix; JSONDecodeError or non-dict/non-list
    aggregate -> ONE stderr diagnostic + sys.exit(1) (source errors are FATAL for
    the N>=2 aggregator pattern — _read_aspirations is called for "world" then
    "agent" and merged; a silent [] would poison the merged aggregate).
    """
    stripped = (raw or "").lstrip()
    if not stripped:
        return None  # genuinely empty queue — valid state, not source error
    try:
        obj, _consumed = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError as exc:
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            f"routing-audit-target-status-sweep: {source} JSONDecodeError ({exc}); "
            f"body prefix: {body_prefix!r}",
            file=sys.stderr,
        )
        sys.exit(1)  # guard-383: source error is fatal
    if not isinstance(obj, (dict, list)):
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            f"routing-audit-target-status-sweep: {source} non-dict-and-non-list "
            f"aggregate (type={type(obj).__name__}); body prefix: {body_prefix!r}",
            file=sys.stderr,
        )
        sys.exit(1)  # guard-383: corrupt aggregate shape
    return obj


def _read_aspirations(source):
    """Return list of (aspiration_dict, source_str) tuples.

    Uses _rt.aspirations_read (daemon client) — the aspirations.py read CLI was
    deleted in the 2026-05-14 cutover. Parse path is g-115-766-tolerant via
    _tolerant_decode — see that helper for the contract.
    """
    try:
        raw = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        # guard-383: source error is FATAL for the N>=2-source aggregator
        # pattern (world + agent merged below). A silent [] would poison the
        # merged aggregate with a complete-looking lie.
        print(f"routing-audit-target-status-sweep: {source} read failed: {e.body or e}",
              file=sys.stderr)
        sys.exit(1)
    data = _tolerant_decode(source, raw)
    if data is None:
        return []
    aspirations = data.get("aspirations") if isinstance(data, dict) else data
    return [(asp, source) for asp in (aspirations or [])]


def _age_hours(ts):
    if not ts:
        return None
    try:
        t = parse_naive_iso(ts)
        return (dt.datetime.now() - t).total_seconds() / 3600
    except Exception:
        return None


def _is_routing_audit_goal(g):
    """Class membership: discovered_by constant OR origin_signal OR title.

    discovered_by is the single most reliable signal (set by
    post-decompose-routing-audit.py on both _build_*_spec shapes). origin_signal
    and title are fallbacks for goals predating the discovered_by field or whose
    discovered_by was stripped.
    """
    if (g.get("discovered_by") or "").strip() == ROUTING_AUDIT_DISCOVERER:
        return True
    if ORIGIN_SIGNAL_PATTERN.match((g.get("origin_signal") or "").strip()):
        return True
    if TITLE_CLASS_PATTERN.match(g.get("title", "") or ""):
        return True
    return False


def _parse_target_id(g):
    """Extract the audited TARGET goal-id. Returns goal-id or None.

    Priority order:
      1. origin_signal "routing-(mismatch|either-resolve):<g-id>" — machine-emitted
      2. title "routing-(mismatch|either-resolve) <g-id> ..." — fallback
    discovered_by is the CONSTANT "post-decompose-routing-audit", never a target
    id, so it is deliberately NOT a parse source here.
    """
    os_ = (g.get("origin_signal") or "").strip()
    m = ORIGIN_SIGNAL_PATTERN.match(os_)
    if m:
        return m.group(1)
    title = g.get("title") or ""
    mm = TITLE_TARGET_PATTERN.search(title)
    if mm:
        return mm.group(1)
    return None


def _parse_recommended_agent(g):
    """Extract the audit's RECOMMENDED agent (). Returns agent or None.

    Both _build_investigate_spec and _build_either_resolve_spec embed the exact
    clause 'aspirations-update-goal.sh <target-id> intended_agent <best_agent>'
    in the description — best_agent IS the agent the audit recommends re-stamping
    the target to. The STAMPED agent appears separately as 'intended_agent=<x>'
    (equals); RECOMMENDED_AGENT_PATTERN requires whitespace after 'intended_agent',
    so it captures ONLY the re-stamp recommendation, never the stamped value.
    """
    desc = g.get("description") or ""
    m = RECOMMENDED_AGENT_PATTERN.search(desc)
    if m:
        return m.group(1)
    return None


def _build_status_index(all_aspirations):
    """Return {goal_id: status} across world + agent active queues."""
    idx = {}
    for asp, _src in all_aspirations:
        for g in (asp.get("goals") or []):
            gid = g.get("id")
            if gid:
                idx[gid] = g.get("status")
    return idx


def _build_intended_agent_index(all_aspirations):
    """Return {goal_id: intended_agent} across world + agent active queues.

    Sibling of _build_status_index — supports the g-115-1529 corrected-while-
    pending check (does the target's CURRENT intended_agent match the audit's
    recommendation?).
    """
    idx = {}
    for asp, _src in all_aspirations:
        for g in (asp.get("goals") or []):
            gid = g.get("id")
            if gid:
                idx[gid] = g.get("intended_agent")
    return idx


def _recommended_matches_current(g, target_id, intended_agent_idx):
    """: has the target already been re-stamped to the recommendation?

    Returns (matched, recommended, current). `matched` is True when the audit's
    recommended agent (parsed from the description clause) equals the target's
    CURRENT intended_agent — i.e. the re-stamp the audit asked for already
    happened, so the flagged mismatch is resolved by definition even while the
    target is still pending. Conservative: matches ONLY the audit's specific
    recommendation (a correction to some OTHER agent does not auto-close — the
    routing changed in a way the audit did not vouch for).
    """
    recommended = _parse_recommended_agent(g)
    current = intended_agent_idx.get(target_id)
    matched = (recommended is not None and current is not None
               and current == recommended)
    return matched, recommended, current


def _reread_goal_authoritative(source, goal_id):
    """``(goal, provenance)`` from the STORE OF RECORD — seam over the shared
    reader. Collaborators are passed explicitly and resolved as module globals
    at call time so they stay monkeypatchable here (guard-2385)."""
    return _shared_reread_goal_authoritative(
        source, goal_id,
        read_aspirations=_read_aspirations,
        is_owncloud=_is_owncloud_backend,
        label="routing-audit-target-status-sweep",
    )


def _stale_candidate_reason(source, goal_id):
    """``None`` when the write may proceed, else the refusal reason."""
    goal, prov = _reread_goal_authoritative(source, goal_id)
    return _shared_stale_candidate_reason(goal, prov)


def _mark_skipped(source, goal_id, note_detail, metrics_path=None,
                  aspiration_id=None):
    """Mark routing-audit goal as skipped with target-resolved outcome_note.

    `note_detail` is the parenthesised reason suffix — terminal-status
    (g-115-1353) OR intended_agent-corrected (g-115-1529). OUTCOME_NOTE_PREFIX
    stays constant in front of it so `_is_already_swept` idempotency catches
    BOTH close reasons with a single prefix check.

    INVARIANT: uses sys.executable directly (same rationale as
    unblock-parent-status-sweep._mark_skipped — bash on Windows can resolve to
    WSL bash.exe with surprising PATH semantics; aspirations-update-goal.sh just
    shells aspirations.py with the same args).

    g-115-6332: re-asserts the candidate predicate against the STORE OF RECORD
    immediately before writing, and refuses when it no longer holds. The scan
    that produced this candidate ran over the whole eligible set, so the
    scan->apply gap is unbounded in principle.
    """
    stale = _stale_candidate_reason(source, goal_id)
    if stale is not None:
        print(f"[routing-audit-target-status-sweep] REFUSED {goal_id}: {stale}",
              file=sys.stderr)
        # COUNT the refusal. A silent no-op is indistinguishable from never
        # having raced, which would make this guard's own effectiveness
        # unmeasurable — and an unmeasurable guard is the one that gets
        # "simplified" away later.
        _append_metric(metrics_path, {
            "type": "routing_audit_refused_stale_candidate",
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "goal_id": goal_id,
            "source": source,
            "aspiration_id": aspiration_id,
            "note_detail": note_detail,
            "reason": stale,
            "agent": os.environ.get("MIND_AGENT", "") or None,
        })
        return False

    note = f"{OUTCOME_NOTE_PREFIX} {note_detail}"
    rc1, _, err1 = _py([str(SCRIPT_DIR / "aspirations.py"),
                        "--source", source, "update-goal",
                        goal_id, "outcome_note", note])
    if rc1 != 0:
        print(f"[routing-audit-target-status-sweep] update outcome_note rc={rc1}: {err1.strip()}",
              file=sys.stderr)
        return False
    rc2, _, err2 = _py([str(SCRIPT_DIR / "aspirations.py"),
                        "--source", source, "update-goal",
                        goal_id, "status", "skipped"])
    if rc2 != 0:
        print(f"[routing-audit-target-status-sweep] update status rc={rc2}: {err2.strip()}",
              file=sys.stderr)
        return False
    return True


def _is_already_swept(g):
    """Idempotency: the note AND a terminal status — never the note alone.

    g-115-5097, and the EXACT mirror of unblock-parent-status-sweep's guard —
    this file documents itself as that sweep's mirror, and it mirrored the
    defect too. _mark_skipped writes note then status as two non-atomic daemon
    calls; keying dedup on the note alone makes the FIRST write the key, so a
    partial success leaves the goal note-bearing and still pending, and every
    later run skips it forever. Requiring a terminal status lets that state
    re-qualify and self-heal on the next run.

    Live instance this repairs without a migration: g-115-4016 carried
    'routing-audit target resolved without action needed (target_id=g-115-4015,
    target.status=completed)' with status=pending. It was left broken ON PURPOSE
    by g-115-5097's author as the verification specimen.

    As in the sibling sweep, main() pre-filters to pending/in-progress before
    calling this, so the conjunct reads as always-false there — which is the
    finding, not an oversight: the pre-fix guard could only ever fire on the
    stranded goals, never on the fully-swept ones it claimed to protect. Kept
    rather than deleted so the function remains correct if that filter changes.
    """
    note = (g.get("outcome_note") or "")
    if not note.startswith(OUTCOME_NOTE_PREFIX):
        return False
    return (g.get("status") or "") in TERMINAL_STATES


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-hours", type=float, default=0.0,
                    help=("Minimum audit-goal age before consideration "
                          "(default 0 — fire immediately when target state "
                          "becomes terminal)."))
    ap.add_argument("--apply", action="store_true",
                    help=("Mark candidates as skipped with target-resolved "
                          "outcome_note (default: report only)."))
    ap.add_argument("--output", choices=["json", "human"], default="json")
    ap.add_argument("--metrics-log", default=None,
                    help=("Path to JSONL metrics log. Default: "
                          "<WORLD_PATH>/routing-audit-target-status-sweep-metrics.jsonl. "
                          "Pass empty string to disable."))
    args = ap.parse_args()

    metrics_path = _resolve_metrics_log(args.metrics_log)

    all_aspirations = (_read_aspirations("world")
                       + _read_aspirations("agent"))
    status_idx = _build_status_index(all_aspirations)
    intended_agent_idx = _build_intended_agent_index(all_aspirations)

    scanned = 0
    eligible = 0
    candidates = []
    applied = 0
    details = []

    for asp, source in all_aspirations:
        if asp.get("status") and asp["status"] != "active":
            continue
        for g in (asp.get("goals") or []):
            if not _is_routing_audit_goal(g):
                continue
            scanned += 1
            if g.get("status") not in ("pending", "in-progress"):
                continue
            if _is_already_swept(g):
                # Idempotent: already swept once, leave alone
                continue
            target_id = _parse_target_id(g)
            if target_id is None:
                details.append({
                    "goal_id": g.get("id"),
                    "aspiration_id": asp.get("id"),
                    "action": "skipped",
                    "reason": "target_id not parseable from origin_signal/title",
                })
                continue
            ref_ts = (g.get("created_at") or g.get("defer_reason_set_at")
                      or g.get("started"))
            age_h = _age_hours(ref_ts)
            if age_h is None:
                # An UNDEFINED age is not a small one. Fusing it into the
                # below-threshold branch rendered the literal string
                # "age None below threshold 24h" — a sentence that is not even
                # well-formed, let alone true — and a reader scanning `reason`
                # sees a threshold word and moves on. This goal carries NO
                # parseable created_at / defer_reason_set_at / started, so it can
                # never age into eligibility: the skip is permanent, not pending.
                # Skipping stays correct (guard-420: no arithmetic on a null);
                # only the name was wrong. See guard-2024, and  which
                # fixed the identical fusion in user-blocker-escalation-check.py.
                details.append({
                    "goal_id": g.get("id"),
                    "aspiration_id": asp.get("id"),
                    "target_id": target_id,
                    "age_hours": None,
                    "action": "skipped",
                    "reason": ("age_uncomputable: no parseable created_at / "
                               "defer_reason_set_at / started — this goal can "
                               "never age into eligibility"),
                })
                continue
            if age_h < args.max_age_hours:
                details.append({
                    "goal_id": g.get("id"),
                    "aspiration_id": asp.get("id"),
                    "target_id": target_id,
                    "age_hours": round(age_h, 1),
                    "action": "skipped",
                    "reason": f"age {age_h:.1f}h below threshold {args.max_age_hours}h",
                })
                continue
            eligible += 1
            # Target missing from active scan -> archived (it WAS active before
            # being archived; absence is the terminal signal). Mirrors the
            # unblock sweep's parent-absence handling.
            target_status = status_idx.get(target_id, "archived")
            # Two independent close reasons:
            #   terminal  () — target reached a terminal status; the
            #             re-stamp the audit asked for is moot.
            #   corrected () — target still pending/in-progress, but its
            #             CURRENT intended_agent already equals the audit's
            #             recommendation, so the flagged mismatch is resolved by
            #             definition (the re-stamp already happened).
            # Terminal takes precedence (stronger signal; original note format).
            close_reason = None
            recommended = None
            note_detail = None
            if target_status in TERMINAL_STATES:
                close_reason = "terminal"
                note_detail = (f"(target_id={target_id}, "
                               f"target.status={target_status})")
            else:
                matched, recommended, _current_ia = _recommended_matches_current(
                    g, target_id, intended_agent_idx)
                if matched:
                    close_reason = "corrected"
                    note_detail = (f"(target_id={target_id}, "
                                   f"intended_agent corrected to {recommended})")
            if close_reason is None:
                details.append({
                    "goal_id": g.get("id"),
                    "aspiration_id": asp.get("id"),
                    "target_id": target_id,
                    "target_status": target_status,
                    "age_hours": round(age_h, 1),
                    "action": "skipped",
                    "reason": (f"target not terminal and intended_agent not "
                               f"corrected to recommendation "
                               f"(target.status={target_status}, "
                               f"recommended={recommended})"),
                })
                continue
            entry = {
                "goal_id": g.get("id"),
                "aspiration_id": asp.get("id"),
                "source": source,
                "target_id": target_id,
                "target_status": target_status,
                "close_reason": close_reason,
                "age_hours": round(age_h, 1),
                "title": g.get("title", ""),
                "action": "would_mark",
            }
            candidates.append({
                "goal_id": g.get("id"),
                "aspiration_id": asp.get("id"),
                "target_id": target_id,
                "target_status": target_status,
                "close_reason": close_reason,
            })
            if args.apply:
                ok = _mark_skipped(source, g.get("id"), note_detail,
                                   metrics_path=metrics_path,
                                   aspiration_id=asp.get("id"))
                entry["action"] = "marked" if ok else "mark_failed"
                if ok:
                    applied += 1
                    _append_metric(metrics_path, {
                        "type": "routing_audit_target_resolved",
                        "timestamp": dt.datetime.now().isoformat(
                            timespec="seconds"),
                        "goal_id": g.get("id"),
                        "source": source,
                        "aspiration_id": asp.get("id"),
                        "target_id": target_id,
                        "target_status": target_status,
                        "close_reason": close_reason,
                        "age_hours_at_mark": round(age_h, 2),
                        "agent": os.environ.get("MIND_AGENT", "") or None,
                    })
            details.append(entry)

    _append_metric(metrics_path, {
        "type": "run_summary",
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "scanned": scanned,
        "eligible": eligible,
        "candidates": len(candidates),
        "applied": applied,
        "mode": "apply" if args.apply else "report",
        "agent": os.environ.get("MIND_AGENT", "") or None,
    })

    result = {
        "scanned": scanned,
        "eligible": eligible,
        "candidates": candidates,
        "applied": applied,
        "details": details,
    }

    if args.output == "human":
        print(f"routing-audit-target-status-sweep: scanned={scanned} "
              f"eligible={eligible} candidates={len(candidates)} "
              f"applied={applied} mode={'apply' if args.apply else 'report'}")
        for c in candidates:
            print(f"  {c['goal_id']} -> target {c['target_id']} "
                  f"(status={c['target_status']})")
    else:
        print(json.dumps(result, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
