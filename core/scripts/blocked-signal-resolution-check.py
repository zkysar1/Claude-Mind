#!/usr/bin/env python3
"""Blocked-Signal Resolution Check — flag `status=blocked` goals whose block
signals have ALL resolved, so a goal whose dependency finished stops sitting
blocked until somebody reads the queue by hand.

THE GAP THIS FILLS. The precheck sweep family keys on `defer_reason`
(precondition-defer-recheck, defer-recheck, credential-defer-recheck,
defer-drift-check, ...). None of them looks at the OTHER block shape — the
`blocked_by` / `blocker_ref` pair of the Blocker Reference Schema. A goal
blocked on that pair stays blocked after its dependency completes, invisible
to every automated sweep. Canonical cost: g-335-144 sat blocked 7 days after
its dependency completed; bravo's felt-sense Lane 3 found it by hand on
2026-07-26 (msg-20260726-051926-bravo-4441). Measured again by THIS checker at
first run: g-350-36 had sat blocked 7 days while its only block signal
(g-350-59) completed ~1.5h after the block was set.

Nearest sibling is `reason-less-blocked-check.py` (precheck Phase 0.5b.11),
which is the exact COMPLEMENT: it finds blocked goals carrying NO block signal
at all. This one finds blocked goals whose signals are all present and all
satisfied. A goal is in exactly one of the two populations, never both.

WHY THE OBVIOUS PREDICATE IS WRONG — and wrong in BOTH directions. A naive
"every blocked_by id is terminal -> unblock" fails twice over, measured against
the live fleet on 2026-07-26 (9 blocked goals carrying a block signal):

  * It OVER-unblocks. g-250-03-c has blocked_by=[g-250-127] (completed) but a
    SEPARATE still-live blocker_ref (type resource-contention, expires_at
    2026-07-28). Naive says unblock; that is a false positive.
  * It UNDER-detects, which is the larger defect and was not anticipated. Both
    goals that were genuinely unblock-eligible (g-350-36, g-350-95) carry NO
    `blocked_by` field at all — their only block signal is `blocker_ref`. A
    blocked_by-only predicate misses them entirely.

  Naive score on that population: 0 of 2 recall, 0 of 1 precision.

THE INPUTS ARE POLYMORPHIC — this checker's real work. Both block-signal fields
vary in TYPE and in REFERENT KIND, so a checker that assumes
(list-of-goal-ids, dict-with-expires_at) mishandles 8 of the 9 live cases. This
is the `checker-input-assumption-defects` family (tree node
system/system-constraints-loop/checker-input-assumption-defects): the checker's
input does not mean what the checker assumes.

  blocked_by  : list[str] | bare str | absent.
                A bare str iterated as a list yields one phantom id PER
                CHARACTER — 'g-335-260' becomes 7 ids, none of which resolve,
                so the goal silently reads "not resolved" forever.
  blocker_ref : dict | bare str | absent.
  referents   : a goal id, a `pq-*` pending-question id, a
                `coordination:msg-*` board reference, or an opaque external id.

Normalizing those is `_norm_blocked_by` / `_norm_blocker_ref` / `_classify_ref`
below, and it is the reason this is a script rather than a one-line predicate.

DETECTIVE, NOT CORRECTIVE — deliberately no `--apply`. Three reasons, in order
of weight: (1) the population is tiny (2 eligible fleet-wide), so automation
buys little while a wrong auto-unblock is expensive; (2) most hits are
lane-owned by another agent, and unblocking another agent's goal appropriates
their queue; (3) a passed `expires_at` means the block record FAIL-OPENED per
the Blocker Reference Schema TTL — it does NOT prove the underlying premise
cleared, so every TTL hit needs a human/owner re-probe before action. The
report is the deliverable. Escalate to --apply only if the population grows.

`--post-routing` (g-115-3414) does NOT breach that posture, and is named apart
from `--apply` so it cannot be mistaken for one: it drops a coordination-board
routing breadcrumb and mutates NO goal. It exists because the routing this sweep
hands to the LLM was being duplicated once per agent per iteration — measured at
7 posts from 3 agents over ~29h on goals that never changed, with g-250-03-c
alone accumulating 35 board mentions. Every one of those posts was individually
CORRECT under the lane rule; what was missing was a SHARED, DURABLE cooldown, so
the breadcrumb doubles as both the routing notice and the cooldown record —
exactly as siblings inbox-alert-age-check (g-115-1533) and handoff-aging-check
(g-115-1531) already do.

Verdicts (only the first four are reported; still_blocked is the quiet case):
  all_resolved  — every block signal present on the goal has resolved.
                  Unblock-eligible. Check `resolution_basis` before acting:
                  `referent_terminal` is strong evidence; `ttl_expired` only
                  means the record fail-opened.
  disagreement  — one signal resolved, another did not. Do NOT unblock: the
                  disagreement IS the signal, and the stale half likely needs
                  reconciling instead.
  dangling_ref  — a signal references an id that does not exist in any store.
                  It can NEVER auto-clear, so it will sit blocked forever
                  unless someone repoints or removes the reference. Emitted for
                  a `pq-` referent ONLY when `pq_corpus_complete` is true —
                  otherwise absence is ignorance, not evidence, and the verdict
                  degrades to `undecidable` (see `_load_pq_index`).
  undecidable   — the reference is opaque (board message / external id), or a
                  `pq-` referent could not be resolved against a complete
                  corpus.
  still_blocked — at least one signal genuinely unresolved. Not reported.

Exit always 0 except the guard-383 fatal on a source read error (a silent empty
aggregate would hide real hits behind a "0 found" lie — same fatal-source-read
contract as defer-drift-check.py / precondition-defer-recheck.py).

JSON output:
  {
    "scanned": N,
    "blocked_with_signal": N,
    "all_resolved_count": N,
    "all_resolved":  [entry, ...],
    "disagreement":  [entry, ...],
    "dangling_ref":  [entry, ...],
    "undecidable":   [entry, ...],
    "naive_would_unblock": [goal_id, ...],   # the blocked_by-only predicate,
                                             # reported for contrast
    "pq_index_size": N,                      # fleet pq ids resolved
    "pq_corpus_complete": bool,              # false => dangling withheld
    "pq_unreadable_agents": [name, ...],     # never silent (guard-383 spirit)
    # Routing cooldown (). `read_ok` / `degraded` are reported
    # ALONGSIDE the counts because an empty cooldown set has two causes — a
    # quiet board and a failed read — and the counts alone cannot separate them.
    "routing_cooldown_hours": float,
    "routing_cooldown_read_ok": bool,
    "routing_cooldown_degraded": bool,       # board read failed => fails OPEN
    "routing_breadcrumbs_seen": N,
    "routing_suppressed_count": N,           # already routed inside the window
    "routing_eligible_count": N,
    "routing_posted": [{goal_id, detail}],   # only with --post-routing
    "routing_post_failed": [{goal_id, detail}],
    "now": iso
  }
  entry = {goal_id, source, aspiration_id, intended_agent, title, verdict,
           blocked_since, days_blocked, blocked_by, blocked_by_status,
           blocked_by_resolved, blocker_ref_kind, blocker_ref_resolved,
           blocker_ref_why, resolution_basis, blocked_by_raw_type,
           already_routed_age_hours, routing_suppressed}

Sibling pattern (rb-428 bash-consolidation family): defer-drift-check.py,
reason-less-blocked-check.py, unblock-parent-status-sweep.py. Guards honored:
guard-420 (tolerant datetime parse), guard-645 (every field read via .get with
a default), guard-614 (structured JSON output), guard-365 (bash wrapper),
guard-383 (fatal source read), guard-980 (read the store of record, never the
local read-through cache — see `_load_pq_index`). Cross-agent enumeration routed
through agents_root() per the CLAUDE.md cross-agent-glob-consumer contract.
Reference: g-115-3241.
"""

import argparse
import datetime as dt
import json
import math
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _dt import parse_naive_iso  # noqa: E402  (shared tzinfo-stripping naive-ISO parse)
from _dependency_graph import (  # noqa: E402  SSOT (guard-547)
    norm_blocked_by,
    resolve_dependency,
    supersession_target,
)
from _runtime_bash import bash_cmd  # noqa: E402   Windows-safe bash resolution
import _rt  # noqa: E402  canonical Python -> daemon client

# : this reader resolves TWO KINDS of referent -- goals and pending
# questions -- and they have DIFFERENT terminal vocabularies. Until now both
# branches shared the one set below, so an `answered` pending question (the state
# pending-questions-close.sh actually writes) read as NOT resolved forever, and a
# blocked signal citing it could never discharge. The two sets are deliberately
# NOT merged: a pending question is never "completed" and a goal is never
# "answered" (guard-1127 -- decouple at the consumer, do not widen a shared value).
# The name carries GOAL_ so the next reader cannot reach for the ambient one by
# accident, which is exactly how the pq branch acquired the wrong set.
GOAL_TERMINAL_STATUSES = ("completed", "archived", "skipped", "expired", "resolved")

# discharges_a_blocker, and NEITHER of its neighbours — the choice is measured,
# not stylistic:
#   is_settled  would exclude `answered`, re-creating the very defect this fixes
#               (the closer WRITES `answered`, so the signal never discharges).
#   is_closed   would include `retired`, which means the question was WITHDRAWN —
#               that kills a blocker's clearing path rather than satisfying it, so
#               counting it as a discharge silently unblocks a goal nobody answered
#               (human-blocked-defer-join.py:83-87 keeps the same distinction).
from _pending_question_status import discharges_a_blocker as pq_discharges  # noqa: E402

# ── Routing cooldown () ────────────────────────────────────────────
# The tag written by `_post_routing_breadcrumb`, and the primary marker read
# back by `_read_recent_routings`. The two LEGACY markers are the tags the
# hand-written LLM routing posts actually carried during the measured
# duplication (see this goal's record: "msg tags blocked-signal,foxtrot,..." and
# "blocked-signal-sweep,foxtrot,..."). Recognising them costs nothing and lets
# the cooldown honour a pre-existing manual post instead of re-routing over it.
#
# A legacy post that never tagged its goal_id simply does not register, because
# `_read_recent_routings` keys on the `g-` tag — that is FAIL-OPEN (it does not
# suppress), which is the safe direction for a cooldown.
ROUTING_TAG = "blocked-signal-routed"
_ROUTING_MARKERS = (ROUTING_TAG, "blocked-signal", "blocked-signal-sweep")

# Tolerance for a breadcrumb stamped AHEAD of `now` (). A post whose
# age is negative beyond this band came from a box whose clock is skewed, and it
# must not suppress: a negative age is unconditionally `< cooldown_hours`, so the
# caller would read it as maximally fresh and the suppression would last ~the
# skew rather than ~the cooldown. Inside the band it is ordinary inter-box
# jitter and IS honoured — dropping every negative age would weaken the cooldown
# into re-routing duplicates on routine jitter.
# NOT HYPOTHETICAL HERE (rb-3741): fleet peers are TZ-split by up to 4h, and
# CLAUDE.md notes a long-lived process keeps the TZ it started with.
ROUTING_CLOCK_SKEW_TOLERANCE_HOURS = 0.5

# Default cooldown window. The measured duplication spanned ~29h with 7 posts
# from 3 agents on UNCHANGED goals, so a window shorter than a day would not
# have suppressed it. Deliberately a CLI arg with a documented default rather
# than a new `core/config/aspirations.yaml` block: this goal did not ask for
# per-deployment tuning, and guard-348 asks for a natural gate before adding
# config surface. Promote it to config only when a second consumer needs it.
DEFAULT_ROUTING_COOLDOWN_HOURS = 24.0

# Board references are recognised but deliberately NOT resolved: a coordination
# post is prose, and "was it answered" is a judgment the checker must not fake.
_BOARD_PREFIXES = ("coordination:", "findings:", "general:", "decisions:", "msg-")


def _tolerant_decode(source, raw):
    """guard-383 contract: empty -> None, raw_decode recovery, fatal on a
    JSONDecodeError or a non dict-or-list body."""
    return _rt.tolerant_decode_aggregate(
        f"blocked-signal-resolution-check: {source}", raw)


def _read_goals(source):
    """Read all active goals from a queue via the daemon.

    guard-383 fatal symmetry (rb-987): a per-source read error in an N>=2
    source aggregator MUST be fatal — a silent `return []` writes a
    complete-looking lie into the merged aggregate. The single fail-open
    boundary is the shell wrapper's `|| echo WARN`, never inside here.
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        print(f"[blocked-signal-resolution-check] {source} read failed: "
              f"{e.body or e}", file=sys.stderr)
        sys.exit(1)  # guard-383: source error fatal
    data = _tolerant_decode(source, out)
    if data is None:
        return []
    goals = []
    for asp in (data.get("aspirations") if isinstance(data, dict) else data) or []:
        for g in asp.get("goals", []) or []:
            g["_source"] = source
            g["_aspiration_id"] = asp.get("id")
            goals.append(g)
    return goals


def _read_archived_goals(source):
    """Return goals nested inside ARCHIVED aspirations for one source.

    `_read_goals` above reads active=True, so it sees only aspirations-*.jsonl.
    When an aspiration COMPLETES it moves to aspirations-archive.jsonl and every
    one of its goals leaves that view — so a blocker_ref naming a goal that
    completed inside a since-archived aspiration is absent from the live index
    and falls through `_classify_ref` to the `rid.startswith("g-")` branch,
    which reports it `dangling`.

    That verdict is not merely missing, it is INVERTED, and this sweep's own
    docstring calls dangling "a real defect to surface": the STRONGEST possible
    resolution (the referent completed AND its whole initiative closed, per
    guard-1555) is reported as a broken reference, so a reader acting on it goes
    and repairs a reference that is perfectly satisfied. That is strictly worse
    than the silent skip the sibling defer-recheck.py had (g-115-3916), because
    a wrong verdict is acted on while a missing one is merely unhelpful.

    Same family as guard-1715: an enumerator's all-clear is bounded by the
    population IT declares, and this index declared only live goals.

    FAIL-SOFT, deliberately asymmetric to `_read_goals`. That function exits(1)
    on RtError per guard-383 because a silent [] there poisons the merged
    aggregate. Here the failure mode is the opposite: losing the archive degrades
    to exactly the pre-fix behavior (an archived referent reads as dangling
    again), so it must not take the sweep down. The degradation is REPORTED, never
    silent — see `archive_read_failed` / `archive_degraded` in the JSON output.
    """
    try:
        out = _rt.aspirations_read(source=source, archive=True)
    except _rt.RtError as e:
        print(f"[blocked-signal-resolution-check] {source} archive read failed "
              f"(degrading to live-only for this source): {e.body or e}",
              file=sys.stderr)
        return None
    data = _tolerant_decode(f"{source} archive", out)
    if data is None:
        # EMPTY BODY IS A VALID STATE, NOT A FAILURE. A source whose archive is
        # simply empty (fresh world, nothing archived yet) must NOT be reported as
        # archive_read_failed — that would flip archive_degraded true everywhere
        # and make the sweep disown its own correct verdicts. Only the RtError
        # branch above is a real read failure.
        return []
    # ?archive=1 returns a BARE list of aspirations, not the
    # {"aspirations": [...]} envelope the active reads use. Handle both so a
    # future endpoint change cannot silently empty this index.
    asps = data.get("aspirations") if isinstance(data, dict) else data
    goals = []
    for asp in asps or []:
        for g in asp.get("goals", []) or []:
            g["_source"] = source
            g["_aspiration_id"] = asp.get("id")
            g["_archived"] = True
            goals.append(g)
    return goals


def _load_pq_index():
    """Fleet-wide pending-question id -> status. Returns (index, missing_agents).

    MUST READ THE STORE OF RECORD, NEVER THE LOCAL TREE (guard-980). Under
    own-cloud the local tree is a READ-THROUGH CACHE: a file nobody has opened
    on this box never materializes locally, so a local glob sees only the
    RESIDENT agent. Measured on cc-02 (2026-07-26): a local
    `*/session/pending-questions.yaml` glob found 1 file (zeta's) while all FIVE
    agents' files were present in the authoritative store (alpha 65841B, bravo
    46233B, echo 28169B, foxtrot 12094B, zeta 21670B).

    That is not a cosmetic under-read — it manufactured TWO false
    `dangling_ref` verdicts on this sweep's own first run, and they were
    reported to the owning agent as "repoint or remove the reference" before
    being caught. `pq-fox-vinheim-chardef-authoring` and
    `pq-fox-roblox-clone-stale-reconcile` are both LIVE in foxtrot's store (the
    first `status: pending`); locally neither is visible, so both read as
    nonexistent. Advising an agent to delete a valid blocker is worse than
    saying nothing.

    Why not just call `pending-questions-read.sh --all-agents` instead: that
    reader is a LOCAL glob by DESIGN, and it is correct — the fleet pq view is a
    two-leg design (g-115-3074) where a freshness leg (`owncloud-pull.sh
    --all-agents`) warms the peer files first and the glob then reads them. The
    reader is not broken; it is CONDITIONAL. This sweep runs inside the precheck,
    which has no freshness leg, so depending on that condition would make the
    sweep's verdicts a function of whether someone recently ran /open-questions.
    Reading the store of record directly makes it freshness-INDEPENDENT, which is
    the property a sweep needs. (The missing-freshness-leg defect at the OTHER
    consumer, aspirations-evolve Step 0.5b, is tracked as its own goal.)

    `missing_agents` is the fail-safe half, and it is what makes the bug
    unrepeatable rather than merely fixed: any agent whose pq file could not be
    read is named, and `_classify_ref` then refuses to call ANY unresolved `pq-`
    referent `dangling` — it degrades to `undecidable`. Silence about an
    incomplete corpus is exactly the guard-383 "complete-looking lie", so
    incompleteness is surfaced in the JSON rather than swallowed. Kept
    non-fatal (unlike `_read_goals`) because the goal-id lane stays fully valid
    without it; the invariant, not a crash, is the protection.

    Agent enumeration is over agent DIRECTORIES via `agents_root()` — never
    over the pq files themselves. Globbing the files would make an unreadable
    file indistinguishable from an agent that has none, which is the whole
    defect.
    """
    index, missing = {}, []
    try:
        import yaml  # local import: only this lane needs it
        from _paths import agents_root
    except Exception:
        return index, ["<pq lane unavailable: yaml/_paths import failed>"]
    try:
        agent_dirs = sorted(d for d in agents_root().iterdir() if d.is_dir())
    except Exception as e:
        return index, [f"<agents_root unreadable: {e}>"]

    backend = None
    try:
        from storage_backend import get_backend
        backend = get_backend()
    except Exception:
        backend = None  # local-only box: the local read below IS the store

    for d in agent_dirs:
        p = d / "session" / "pending-questions.yaml"
        raw = None
        # Store of record FIRST; the local file is only a fallback for boxes
        # with no backend (or a backend that cannot serve this key).
        if backend is not None:
            try:
                raw = backend.read_text(p)
            except Exception:
                raw = None
        if raw is None:
            try:
                raw = p.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue  # agent legitimately has no pending questions
            except Exception:
                missing.append(d.name)
                continue
        try:
            data = yaml.safe_load(raw)
        except Exception:
            missing.append(d.name)
            continue
        entries = data if isinstance(data, list) else []
        if isinstance(data, dict):
            for key in ("questions", "pending_questions", "entries"):
                v = data.get(key)
                if isinstance(v, list):
                    entries = v
                    break
            else:
                entries = [v for v in data.values() if isinstance(v, dict)]
        for e in entries:
            if isinstance(e, dict) and e.get("id"):
                index[str(e["id"])] = e.get("status") or "unknown"
    return index, missing


def _parse_iso(ts):
    """Tolerant ISO parse (guard-420). Returns datetime or None — never raises."""
    if not ts:
        return None
    try:
        return parse_naive_iso(ts)
    except Exception:
        return None


# MOVED to `_dependency_graph.norm_blocked_by` () — SSOT for the
# polymorphic-`blocked_by` normalization, now shared with
# dependency-cycle-check.py. The body is unchanged; only its home moved, so
# guard-547's measured harm (a hand-mirrored copy drifting fixes behind the
# original) cannot apply to it. This module-level alias preserves the private
# name for the file's existing call sites and for tests that import it.
_norm_blocked_by = norm_blocked_by


def _norm_blocker_ref(v):
    """Normalize the polymorphic `blocker_ref` field.

    Returns (kind, as_dict_or_None, raw) where kind is one of
    'none' | 'dict' | 'str' | 'other'. Same defect class as `_norm_blocked_by`:
    the field is a structured dict on some goals (g-250-03-c, g-354-21) and a
    bare id string on others (g-335-228 -> 'pq-fox-vinheim-chardef-authoring',
    g-350-36 -> 'g-350-59').
    """
    if v is None:
        return ("none", None, v)
    if isinstance(v, dict):
        return ("dict", v, v)
    if isinstance(v, str):
        return ("str", None, v) if v.strip() else ("none", None, None)
    return ("other", None, v)


def _classify_ref(ref_id, goal_index, pq_index, pq_complete=True):
    """Resolve ONE reference id to (resolved, why, referent_kind).

    resolved is True / False / None, where None means "cannot be decided here".
    referent_kind is 'goal' | 'pending_question' | 'board' | 'dangling' | 'opaque'.

    Referent kind is decided by LOOKUP FIRST, prefix second: an id that is
    present in a store is that kind regardless of how it is spelled. Only when
    no store has it does the spelling decide between `dangling` (it looks like
    a goal or pq id, so the reference is broken) and `opaque` (an external id
    this checker was never able to resolve). That ordering matters — a
    dangling reference can never auto-clear and is a real defect to surface,
    while an opaque one is merely outside scope.
    """
    rid = (ref_id or "").strip()
    if not rid:
        return (None, "empty reference", "opaque")
    if rid in goal_index:
        _g = goal_index[rid][1]
        status = (_g.get("status") or "unknown")
        # "found in the archive" is kept DISTINCT from "found live" and from
        # "found nowhere" (guard-1555): a referent that completed AND had its
        # whole initiative archived is a STRONGER fact than one merely completed
        # in the live queue, and collapsing the three into one message is what
        # hid the sibling defect for 37 days. The archived case reaches this
        # branch at all only because main() folds the archive into goal_index.
        _where = ("an ARCHIVED aspiration" if _g.get("_archived")
                  else "the live queue")
        # SUPERSESSION-AWARE (). A goal closed `skipped`/`superseded`
        # may have had its work MOVED to another goal rather than abandoned, and
        # a status-equality check cannot see that. Measured 2026-08-26: a
        # duplicate chain closed `skipped` with supersession notes was read as
        # NOT-done by a re-probe, which re-deferred two just-unblocked goals on a
        # premise that was already false — zero vinheim goals selectable across
        # 1,400 ranked for ~4h.
        #
        # Only followed when a pointer actually exists, so the flat-view rebuild
        # below costs nothing on the overwhelmingly common path. `satisfied`
        # requires the SUPERSEDING goal to be completed: supersession moves the
        # obligation, it does not discharge it, so a chain ending in another
        # skip stays unresolved rather than silently satisfying the dependency.
        if supersession_target(_g):
            _flat = {k: v[1] for k, v in goal_index.items()}
            _verdict, _resolved_id, _chain = resolve_dependency(rid, _flat)
            if _verdict == "satisfied":
                return (True,
                        f"goal {rid} is {status} but was superseded by "
                        f"{_resolved_id}, which is completed "
                        f"(chain {' -> '.join(_chain)})", "goal")
            if _verdict == "cycle":
                # A looping superseded_by chain is a data defect, not a
                # resolution. None (not False) — it cannot be decided here.
                return (None,
                        f"goal {rid} has a CYCLIC supersession chain "
                        f"({' -> '.join(_chain)}) — repoint it; the dependency "
                        f"cannot be resolved while it loops", "goal")
            if _verdict == "open":
                return (False,
                        f"goal {rid} is {status}, superseded by {_resolved_id} "
                        f"which is NOT completed — the obligation moved, it was "
                        f"not discharged", "goal")
            # "unknown" — the superseding goal is absent from live AND archive.
            # guard-1890: absence is ignorance, never evidence. Fall through to
            # the plain status read rather than guessing.
        return (status in GOAL_TERMINAL_STATUSES,
                f"goal {rid} is {status} (found in {_where})", "goal")
    if rid in pq_index:
        status = pq_index[rid]
        # PENDING-QUESTION vocabulary, not the goal one directly above ().
        return (pq_discharges(status),
                f"pending-question {rid} is {status}", "pending_question")
    if rid.startswith(_BOARD_PREFIXES):
        return (None, f"board reference {rid} — not resolvable here", "board")
    if rid.startswith("pq-"):
        # FAIL-SAFE (see _load_pq_index): "not in the index" only means DANGLING
        # when the index is provably COMPLETE. With any agent's pq store
        # unreadable, absence is ignorance, not evidence — and the wrong call
        # here tells the owning agent to delete a live blocker.
        if not pq_complete:
            return (None,
                    f"pending-question {rid} not found, but the fleet pq corpus "
                    f"is INCOMPLETE — cannot distinguish dangling from "
                    f"unreadable, so NOT reported as dangling", "opaque")
        return (None,
                f"pending-question {rid} does not exist in any agent's store — "
                f"DANGLING, can never auto-clear", "dangling")
    if rid.startswith("g-"):
        return (None,
                f"goal {rid} not found in the scanned queues — DANGLING or "
                f"out of scan scope", "dangling")
    return (None, f"opaque external id {rid!r}", "opaque")


def _resolve_blocker_ref(kind, as_dict, raw, goal_index, pq_index, now,
                         pq_complete=True, external_resolver=None):
    """Resolve the blocker_ref half. Returns (resolved, why, basis).

    basis is 'no_blocker_ref' | 'ttl_expired' | 'referent_terminal' |
    'referent_terminal_external' | 'external_unresolvable' | 'unresolved' |
    'dangling' | 'opaque'.

    `external_resolver` is an optional callable taking a cross-world referent
    id ('<world>:<goal-id>') and returning that referent's status string, or
    None when it cannot say. It is injected rather than imported because this
    checker runs in worlds that have no cross-world reader at all — see the
    external_id block below for why absence gets its own basis instead of
    being folded into 'opaque'.

    TTL SEMANTICS — stated explicitly because it is easy to over-read: a passed
    `expires_at` means the block record FAIL-OPENED by design (the schema's TTL
    exists so work is never frozen forever). It is NOT evidence the underlying
    premise cleared. Hence basis is surfaced separately from `resolved`, so a
    reader can weight `referent_terminal` above `ttl_expired` and re-probe the
    latter before acting. This is a first-class reason the checker stays
    detective-only.
    """
    if kind == "none":
        return (True, "no blocker_ref", "no_blocker_ref")
    if kind == "other":
        return (None, f"blocker_ref has unexpected type "
                      f"{type(raw).__name__}", "opaque")
    if kind == "str":
        resolved, why, referent = _classify_ref(raw, goal_index, pq_index,
                                                 pq_complete)
        basis = ("referent_terminal" if resolved else
                 "dangling" if referent == "dangling" else
                 "opaque" if resolved is None else "unresolved")
        return (resolved, f"str-ref: {why}", basis)

    # dict form
    whys = []
    expired = False
    # exp_usable: a TTL this reader could actually READ. Distinct from
    # `bool(exp_raw)` — a truthy-but-unparseable expires_at is PRESENT but
    # carries no signal, and conflating the two made the fallthrough below
    # assert a definite verdict from a field the reader had just failed to
    # parse ( / rb-245: a definite conclusion drawn against an
    # unverifiable field).
    exp_usable = False
    exp_raw = as_dict.get("expires_at")
    if exp_raw:
        exp_dt = _parse_iso(exp_raw)
        if exp_dt is not None:
            exp_usable = True
            expired = exp_dt < now
            whys.append(f"expires_at={exp_raw} "
                        f"{'PASSED' if expired else 'future'}")
        else:
            whys.append(f"expires_at={exp_raw!r} unparseable — contributes NO "
                        f"signal, not a live TTL")
    # Both spellings observed in the wild.
    ug = as_dict.get("unblock_goal") or as_dict.get("unblocking_goal")
    ug_resolved, ug_referent = None, None
    if ug:
        ug_resolved, ug_why, ug_referent = _classify_ref(
            ug, goal_index, pq_index, pq_complete)
        whys.append(f"unblock_goal: {ug_why}")

    # external_id (). A blocker_ref of type partner-response names its
    # referent HERE and nowhere else, so until this branch existed that entire
    # blocker class was undetectable by the one checker built to find blocked
    # goals whose signals have all cleared. Measured cost before the fix (two
    # live goals, cleared by hand 2026-07-28): one blocked 5 days past its
    # referent's completion, one 10 days.
    #
    # This is the READ-side of guard-367 ("a field with a canonical resolver
    # that some code path declines to resolve is SILENT BIAS, not missing
    # data"): _classify_ref was right there, already resolving unblock_goal.
    ext = as_dict.get("external_id")
    ext_resolved, ext_referent = None, None
    if ext:
        ext_s = str(ext).strip()
        # Board prefixes legitimately contain ':' ("coordination:", "findings:",
        # ...), so a bare colon test would misroute them into the cross-world
        # branch and strip them of the board classification _classify_ref gives
        # them. Check the prefixes FIRST.
        if ":" in ext_s and not ext_s.startswith(_BOARD_PREFIXES):
            # Cross-world referent. The other world emits no completion
            # callback, which is the whole reason a defer against it decays
            # silently from accurate to false. Only an injected resolver can
            # settle it.
            if external_resolver is not None:
                # FAIL-OPEN (guard-142 / Invariant 2). The resolver is injected
                # foreign code reaching another world — the single likeliest
                # dependency here to be down, slow, or throwing. A detective
                # sweep that dies on its own optional dependency is worse than
                # one that reports less: this function is called per-goal, so an
                # unhandled raise takes out the WHOLE scan, not just this ref.
                # The error is recorded in `whys` rather than swallowed, so a
                # broken resolver is diagnosable instead of merely quiet.
                try:
                    status = external_resolver(ext_s)
                except Exception as exc:  # noqa: BLE001 — deliberate catch-all
                    status = None
                    whys.append(f"external_id: cross-world {ext_s} — resolver "
                                f"raised {type(exc).__name__}: {exc}")
                if status is not None:
                    ext_resolved = status in GOAL_TERMINAL_STATUSES
                    ext_referent = "external"
                    whys.append(f"external_id: cross-world {ext_s} is {status}")
            if ext_referent is None:
                # NOT 'opaque'. Opaque means "a shape this reader does not
                # understand"; this shape IS understood and merely unreachable
                # from here. Folding the two together makes a real operational
                # population — cross-world refs nothing can currently resolve —
                # uncountable, and a population you cannot count is one nobody
                # fixes.
                ext_referent = "external_unresolvable"
                whys.append(f"external_id: cross-world {ext_s} — no resolver "
                            f"configured, referent state undeterminable here")
        else:
            ext_resolved, ext_why, ext_referent = _classify_ref(
                ext_s, goal_index, pq_index, pq_complete)
            whys.append(f"external_id: {ext_why}")

    if not whys:
        # : the old message here read "no resolvable signal", which is
        # mechanically true of THIS READER but substantively wrong about the ref
        # — every live instance measured 2026-07-27 was content-rich (`ref`,
        # `why`, `blocker_type`, `blocking_goal`, `denied_action`, `principal`,
        # `probe`...). A goal filed off that summary alone would claim these
        # blocks are unresolvable, which is false (rb-245: an undecidable count
        # against a field-name assumption). Name the keys so a reader can tell
        # "genuinely opaque" from "schema variant this reader does not parse".
        #
        # Deliberately NOT fixed by adding `blocking_goal` as a third accepted
        # spelling beside unblock_goal / unblocking_goal above: absorbing
        # variants one at a time is how a vocabulary reaches five spellings.
        # Canonicalization belongs at the WRITE path (gates/blocker_ref.validate
        # already returns exactly type/external_id/state_hash/created_at/
        # expires_at); it is bypassed by direct `update-goal <id> blocker_ref`
        # field writes, which is the actual defect. Tracked separately.
        present = sorted(k for k in as_dict if as_dict.get(k) is not None)
        recognized = {"expires_at", "unblock_goal", "unblocking_goal",
                      "external_id"}
        if set(present) - recognized:
            whys.append(
                "blocker_ref dict carries no key this reader resolves "
                f"(needs expires_at or unblock_goal); present keys: {present} "
                "— SCHEMA VARIANT, not an empty ref: read the payload before "
                "concluding the block is unresolvable")
        else:
            # NOT necessarily empty (). A recognized key can be
            # PRESENT with a falsy value — expires_at: "" is the observed case —
            # and contribute nothing. Calling that dict "empty" sends the next
            # reader hunting for a missing field that is sitting right there,
            # which is the same overclaim  fixed one branch above.
            # Name what is actually present instead.
            falsy = sorted(k for k in as_dict if not as_dict.get(k))
            if falsy:
                whys.append("blocker_ref dict carries no resolvable signal — "
                            f"recognized key(s) present but EMPTY: {falsy}")
            else:
                whys.append("blocker_ref dict is empty — no resolvable signal")

    if ug_resolved:
        return (True, "; ".join(whys), "referent_terminal")
    if ext_resolved:
        # ORDER IS THE FIX for shape (b): a TERMINAL referent must outrank the
        # clock. Before this line sat above `expired`, a ref carrying both an
        # external_id and a future expires_at returned 'unresolved' and waited
        # out its TTL, even though the referent had completed days earlier —
        # completion was never the trigger, only the clock was. Distinct basis
        # from `referent_terminal` so a reader can tell WHICH field settled it.
        return (True, "; ".join(whys), "referent_terminal_external")
    if expired:
        return (True, "; ".join(whys), "ttl_expired")
    if ug_referent == "dangling" or ext_referent == "dangling":
        return (None, "; ".join(whys), "dangling")
    if ext_referent == "external_unresolvable":
        return (None, "; ".join(whys), "external_unresolvable")
    # `ext_resolved is None` is load-bearing, not defensive: a PENDING external
    # referent yields False here, and False must fall through to 'unresolved'
    # rather than be swallowed as 'opaque'. Testing truthiness instead would
    # re-hide exactly the goals this fix exists to surface.
    # `not exp_usable`, NOT `not exp_raw` (). With exp_raw, a garbage
    # TTL string counted as a present signal and pushed control past this guard
    # to the definite `unresolved` below — reporting "this block is confirmed
    # still live" on the strength of a field the reader could not read. Every
    # other undecidable shape in this function returns opaque; an unreadable TTL
    # is one of those, and the precheck 0.5b.12 sweep consumes the difference.
    if ug_resolved is None and ext_resolved is None and not exp_usable:
        return (None, "; ".join(whys), "opaque")
    return (False, "; ".join(whys), "unresolved")


def _classify(goal, goal_index, pq_index, now, pq_complete=True):
    """Pure eligibility test for ONE goal. Returns an entry dict or None.

    Pure (no I/O, no daemon) so the whole verdict ladder is unit-testable with
    synthetic goals — the daemon reads in main() are the only impure part.
    """
    if goal.get("status") != "blocked":
        return None

    bb = _norm_blocked_by(goal.get("blocked_by"))
    kind, as_dict, raw = _norm_blocker_ref(goal.get("blocker_ref"))
    if not bb and kind == "none":
        # No block signal at all — that is reason-less-blocked-check.py's
        # population (precheck Phase 0.5b.11), not ours. Never double-report.
        return None

    bb_status = {}
    for b in bb:
        entry = goal_index.get(b)
        bb_status[b] = (entry[1].get("status") or "unknown") if entry else "NOT-FOUND"
    bb_dangling = any(s == "NOT-FOUND" for s in bb_status.values())
    # VACUOUS TRUTH IS DELIBERATE HERE, AND DANGEROUS ONE STEP LATER. An absent
    # `blocked_by` must read as resolved=True so the `all_resolved` conjunction
    # can fire on `blocker_ref` alone — that is what catches the two genuinely
    # eligible goals, which carry no `blocked_by` at all. But the same vacuous
    # True must NOT feed the `disagreement` verdict: "the signals disagree"
    # requires two signals to actually BE there. Hence bb_present/br_present
    # below. Measured cost of getting this wrong: 4 of 6 first-run
    # "disagreements" were goals with ONE live signal — i.e. plain blocked
    # goals, working exactly as intended, reported as findings. A detective
    # sweep whose report is majority-noise trains its reader to skip it.
    bb_resolved = (not bb) or all(s in GOAL_TERMINAL_STATUSES for s in bb_status.values())
    bb_present = bool(bb)

    br_resolved, br_why, basis = _resolve_blocker_ref(
        kind, as_dict, raw, goal_index, pq_index, now, pq_complete)
    br_present = kind != "none"

    if bb_dangling or basis == "dangling":
        verdict = "dangling_ref"
    elif br_resolved is None:
        verdict = "undecidable"
    elif bb_resolved and br_resolved:
        verdict = "all_resolved"
    elif bb_present and br_present and bb_resolved != br_resolved:
        verdict = "disagreement"
    else:
        verdict = "still_blocked"

    blocked_since = goal.get("blocked_since")
    bs_dt = _parse_iso(blocked_since)
    days_blocked = round((now - bs_dt).total_seconds() / 86400, 1) if bs_dt else None

    return {
        "goal_id": goal.get("id"),
        "source": goal.get("_source"),
        "aspiration_id": goal.get("_aspiration_id"),
        "intended_agent": goal.get("intended_agent"),
        "title": (goal.get("title") or "")[:80],
        "verdict": verdict,
        "blocked_since": blocked_since,
        "days_blocked": days_blocked,
        "blocked_by": bb,
        "blocked_by_raw_type": type(goal.get("blocked_by")).__name__,
        "blocked_by_status": bb_status,
        "blocked_by_resolved": bb_resolved,
        "blocker_ref_kind": kind,
        "blocker_ref_resolved": br_resolved,
        "blocker_ref_why": br_why,
        "resolution_basis": basis,
    }


def _routing_window_str(cooldown_hours):
    """board-read --since needs an int+unit duration. Round up + 1h margin so
    the read window safely covers the whole cooldown window (mirrors
    inbox-alert-age-check._escalate_window_str)."""
    return "%dh" % (int(math.ceil(cooldown_hours)) + 1)


def _read_recent_routings(now, cooldown_hours, board_log_path=None):
    """Return ({goal_id: most_recent_age_hours}, read_ok) for routing breadcrumbs
    on the coordination board within the scan window — from ANY agent.

    `read_ok` is returned SEPARATELY from the dict, and that is the point: an
    empty dict has two causes that are otherwise indistinguishable — nothing was
    routed recently (clean), or the read failed and fail-open emptied it
    (degraded). Deriving the flag from the dict's truthiness cannot tell them
    apart and would report a healthy quiet board as a failure. Same
    discrimination `archive_degraded` provides for the archive read (rb-245:
    a zero whose provenance is unknown is not a measurement).

    THE SHARED, DURABLE COOLDOWN (g-115-3414), mirroring the pattern
    inbox-alert-age-check (g-115-1533) and handoff-aging-check (g-115-1531)
    already use, rather than inventing a second one. The board breadcrumb IS the
    cooldown record because it is the only store with BOTH properties this needs:

      - SHARED  — every agent reads the same coordination board, so agent B sees
                  that agent A already routed this hit. A per-agent WM slot
                  cannot do this, and that is exactly why 0.5b.2 abandoned
                  `proactive_escalation_log` (g-115-1531).
      - DURABLE — board posts persist in world/board/; a WM reset between
                  iterations would re-arm a WM-based cooldown and re-fire.

    Returns the SMALLEST age (most-recent post) per goal_id so the caller
    compares against the freshest evidence, not the oldest.

    `board_log_path` (tests only): read a JSON list of post dicts directly,
    bypassing the daemon/subprocess board scan.

    FAIL-OPEN: any read failure yields an empty dict -> no cooldown -> every hit
    is eligible to route. That direction is deliberate. A cooldown that
    fail-CLOSED would silence routing whenever the board is unreachable, which
    converts a transient board fault into invisible blocked work — the exact
    failure class 0.5b.12 exists to surface. A duplicate post is cheap; a
    suppressed-and-forgotten hit is not.
    """
    posts = []
    read_ok = True
    if board_log_path is not None:
        try:
            with open(board_log_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            posts = data if isinstance(data, list) else []
        except Exception:
            posts = []
            read_ok = False
    else:
        try:
            proc = subprocess.run(
                bash_cmd(SCRIPT_DIR / "board-read.sh",
                         "--channel", "coordination",
                         "--type", "status",
                         "--since", _routing_window_str(cooldown_hours),
                         "--json"),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if proc.returncode == 0:
                for line in (proc.stdout or "").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        posts.append(json.loads(line))
                    except Exception:
                        continue
            else:
                read_ok = False
                sys.stderr.write(
                    "blocked-signal-resolution-check: board-read.sh exit=%d "
                    "stderr=%s — fail-open (no cooldown this sweep)\n"
                    % (proc.returncode, (proc.stderr or "").strip()[:200]))
        except Exception as exc:
            read_ok = False
            sys.stderr.write(
                "blocked-signal-resolution-check: board-read.sh exception (%s) "
                "— fail-open (no cooldown this sweep)\n" % exc)

    recent = {}
    for p in posts:
        if not isinstance(p, dict):
            continue
        tags = p.get("tags") or []
        if not any(m in tags for m in _ROUTING_MARKERS):
            continue
        age = _age_hours_from(p.get("timestamp") or p.get("ts"), now)
        if age is None:
            continue
        if age < -ROUTING_CLOCK_SKEW_TOLERANCE_HOURS:
            # Future-stamped beyond clock skew: DROP, never clamp to 0 —
            # clamping still yields `age < cooldown_hours` and still suppresses.
            # Dropping fails OPEN, the direction this function's docstring
            # already commits to ("A duplicate post is cheap; a
            # suppressed-and-forgotten hit is not"). Silent by construction
            # otherwise: read_ok stays True because the READ succeeded, so no
            # field reported the suppression (guard-1760 shape).
            continue
        for t in tags:
            # The breadcrumb tags the goal_id (`g-*`); lane/agent tags never do.
            if isinstance(t, str) and t.startswith("g-"):
                if t not in recent or age < recent[t]:
                    recent[t] = age
    return recent, read_ok


def _age_hours_from(ts, now):
    """Hours between `ts` and `now`, or None when unparseable."""
    parsed = _parse_iso(ts)
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / 3600.0


def _post_routing_breadcrumb(entry, no_board=False):
    """Post the shared+durable routing breadcrumb. Returns (ok, detail).

    Body goes on STDIN via `input=` — board-post.sh reads it with `BODY="$(cat)"`
    and has NO --body flag (guard-1531 / guard-1036). Passing it as an argument
    silently posts an empty body.
    """
    if no_board:
        return True, "no_board"
    gid = entry.get("goal_id") or "?"
    lane = entry.get("intended_agent") or "unknown"
    try:
        tags = "%s,%s,lane:%s" % (ROUTING_TAG, gid, lane)
        msg = ("Blocked-signal sweep (0.5b.12): [%s] verdict=%s basis=%s "
               "blocked %s — routing to lane %s. Detective-only: no goal status "
               "was mutated. %s"
               % (gid, entry.get("verdict"), entry.get("resolution_basis"),
                  ("%sd" % entry["days_blocked"]) if entry.get("days_blocked") is not None else "?d",
                  lane, entry.get("title") or ""))
        proc = subprocess.run(
            bash_cmd(SCRIPT_DIR / "board-post.sh",
                     "--channel", "coordination",
                     "--type", "status",
                     "--tags", tags),
            input=msg,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if proc.returncode == 0:
            return True, "posted"
        sys.stderr.write(
            "blocked-signal-resolution-check: board-post.sh exit=%d stderr=%s\n"
            % (proc.returncode, (proc.stderr or "").strip()[:300]))
        return False, "board_post_nonzero:%d" % proc.returncode
    except Exception as exc:
        sys.stderr.write(
            "blocked-signal-resolution-check: board-post.sh exception (%s) — "
            "skipping post\n" % exc)
        return False, "board_post_exception:%s" % exc.__class__.__name__


def main():
    ap = argparse.ArgumentParser(
        description=("Flag status=blocked goals whose block signals "
                     "(blocked_by / blocker_ref) have ALL resolved. Detective "
                     "only — never mutates. Complement of "
                     "reason-less-blocked-check.py."),
    )
    ap.add_argument("--output", choices=["json", "human"], default="json")
    # --post-routing, deliberately NOT named --apply (). Every sibling
    # sweep in this family uses --apply to mean "MUTATE GOALS", and this sweep's
    # detective-only posture is load-bearing and documented at its phase header.
    # Reusing the family's mutation verb for a board post would invite exactly
    # the misreading the header warns against. The flag posts a breadcrumb and
    # touches no goal.
    ap.add_argument("--post-routing", action="store_true",
                    help=("Post the shared coordination-board routing breadcrumb "
                          "for each non-suppressed hit. Never mutates a goal."))
    ap.add_argument("--cooldown-hours", type=float,
                    default=DEFAULT_ROUTING_COOLDOWN_HOURS,
                    help="Routing-breadcrumb cooldown window (default: %(default)s).")
    ap.add_argument("--board-routing-log", default=None,
                    help=argparse.SUPPRESS)  # tests only: read posts from a JSON file
    ap.add_argument("--no-board", action="store_true",
                    help=argparse.SUPPRESS)  # tests only: skip the post subprocess
    args = ap.parse_args()

    now = dt.datetime.now()
    all_goals = _read_goals("world") + _read_goals("agent")
    goal_index = {g.get("id"): (g.get("_source"), g) for g in all_goals if g.get("id")}

    # ARCHIVE INDEX (, sibling audit of ). Reference
    # resolution must span BOTH stores: a referent that completed inside a
    # since-archived aspiration is absent from every live read above, so
    # `_classify_ref` fell through to the `g-` branch and reported the strongest
    # possible resolution as `dangling` — an INVERTED verdict a reader acts on.
    #
    # Folded into the SAME `goal_index` (keeping its `(source, goal)` tuple
    # shape) so ALL existing lookup sites — `_classify_ref`, `_resolve_blocker_ref`,
    # and the `_classify` blocked_by walk — resolve it without threading a second
    # index through four signatures.
    #
    # LIVE WINS on collision, and that direction is load-bearing: an id present in
    # both stores means the live record is the current one (a re-opened goal, or a
    # mid-archive race), so the archive copy is a stale snapshot that must never
    # shadow it. The `not in goal_index` guard is what enforces that — do NOT
    # "simplify" it to an unconditional assignment.
    #
    # `_archived` on the goal dict carries the origin to `_classify_ref`, which
    # keeps "found in archive" distinct from "found live" and from "found in
    # neither store" (that last one is still `dangling`, correctly).
    archive_read_failed = []
    for _src in ("world", "agent"):
        _arch = _read_archived_goals(_src)
        if _arch is None:
            archive_read_failed.append(_src)
            continue
        for _g in _arch:
            _gid = _g.get("id")
            if _gid and _gid not in goal_index:
                goal_index[_gid] = (_g.get("_source"), _g)

    pq_index, pq_missing = _load_pq_index()
    pq_complete = not pq_missing

    buckets = {"all_resolved": [], "disagreement": [],
               "dangling_ref": [], "undecidable": []}
    blocked_with_signal = 0
    naive_would_unblock = []

    for g in all_goals:
        entry = _classify(g, goal_index, pq_index, now, pq_complete)
        if entry is None:
            continue
        blocked_with_signal += 1
        # The blocked_by-only predicate, computed for contrast so the report can
        # show what the obvious implementation would have done.
        if entry["blocked_by"] and entry["blocked_by_resolved"]:
            naive_would_unblock.append(entry["goal_id"])
        if entry["verdict"] == "still_blocked":
            continue
        buckets[entry["verdict"]].append(entry)

    for v in buckets.values():
        v.sort(key=lambda e: (e["days_blocked"] is None, -(e["days_blocked"] or 0)))

    # ── Routing cooldown () ────────────────────────────────────────
    # Annotate EVERY surfaced hit, in all four buckets, because the phase routes
    # from all four. The annotation gates the OUTBOUND BOARD POST only — the
    # stdout surfacing below is untouched by design (this goal's CAVEAT: the
    # stdout line is how the RUNNING agent learns the state, and suppressing it
    # would hide the finding from the one agent positioned to act on it).
    recent_routings, routing_read_ok = _read_recent_routings(
        now, args.cooldown_hours,
        Path(args.board_routing_log) if args.board_routing_log else None)
    routing_suppressed = 0
    routing_eligible = 0
    routing_posted = []
    routing_post_failed = []
    for name in ("all_resolved", "disagreement", "dangling_ref", "undecidable"):
        for e in buckets[name]:
            age = recent_routings.get(e["goal_id"])
            suppressed = age is not None and age < args.cooldown_hours
            e["already_routed_age_hours"] = round(age, 2) if age is not None else None
            e["routing_suppressed"] = suppressed
            if suppressed:
                routing_suppressed += 1
                continue
            routing_eligible += 1
            if args.post_routing:
                ok, detail = _post_routing_breadcrumb(e, no_board=args.no_board)
                (routing_posted if ok else routing_post_failed).append(
                    {"goal_id": e["goal_id"], "detail": detail})

    result = {
        "scanned": len(all_goals),
        "blocked_with_signal": blocked_with_signal,
        "all_resolved_count": len(buckets["all_resolved"]),
        "all_resolved": buckets["all_resolved"],
        "disagreement": buckets["disagreement"],
        "dangling_ref": buckets["dangling_ref"],
        "undecidable": buckets["undecidable"],
        "naive_would_unblock": naive_would_unblock,
        "pq_index_size": len(pq_index),
        "pq_corpus_complete": pq_complete,
        "pq_unreadable_agents": pq_missing,
        # : a lost archive read degrades this sweep to its pre-fix
        # behavior (archived referents read as `dangling` again). Surfaced so the
        # degradation is never silent — a silent one reinstates the original
        # invisibility, which is the defect, not a smaller version of it. Note an
        # EMPTY archive is NOT a failure and must leave these two untouched.
        "archive_read_failed": archive_read_failed,
        "archive_degraded": bool(archive_read_failed),
        #  routing cooldown. `routing_cooldown_read_ok` is reported
        # SEPARATELY from the suppression count because an empty cooldown set has
        # two causes that are otherwise indistinguishable: nothing was routed
        # recently (clean), or the board read failed and fail-open emptied it
        # (degraded). A reader must be able to tell those apart — the same
        # discrimination `archive_degraded` above provides for the archive read.
        "routing_cooldown_hours": args.cooldown_hours,
        "routing_cooldown_read_ok": routing_read_ok,
        "routing_cooldown_degraded": not routing_read_ok,
        "routing_breadcrumbs_seen": len(recent_routings),
        "routing_suppressed_count": routing_suppressed,
        "routing_eligible_count": routing_eligible,
        "routing_posted": routing_posted,
        "routing_post_failed": routing_post_failed,
        "now": now.isoformat(timespec="seconds"),
    }

    if args.output == "human":
        print(f"scanned={result['scanned']} "
              f"blocked_with_signal={blocked_with_signal} "
              f"all_resolved={len(buckets['all_resolved'])} "
              f"disagreement={len(buckets['disagreement'])} "
              f"dangling={len(buckets['dangling_ref'])} "
              f"undecidable={len(buckets['undecidable'])}")
        for name in ("all_resolved", "disagreement", "dangling_ref", "undecidable"):
            for e in buckets[name]:
                days = f"{e['days_blocked']}d" if e["days_blocked"] is not None else "?d"
                print(f"  [{name}] {e['goal_id']} ({e['source']}, "
                      f"{e['intended_agent']}) blocked {days} | "
                      f"basis={e['resolution_basis']} | {e['title']}")
                print(f"      bb={e['blocked_by']} ({e['blocked_by_raw_type']}) "
                      f"-> {e['blocked_by_status']} resolved={e['blocked_by_resolved']}")
                print(f"      br[{e['blocker_ref_kind']}]: {e['blocker_ref_why']}")
        if naive_would_unblock:
            print(f"  naive blocked_by-only predicate would unblock: "
                  f"{naive_would_unblock}")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
