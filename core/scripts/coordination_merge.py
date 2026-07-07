#!/usr/bin/env python3
"""Commutative merge handlers for coordination stores under own-cloud.

Purpose
-------
When two machines both hold UNPUSHED local writes to the same coordination
store AND the remote (S3) has also moved — the "both-diverged" state — the
own-cloud backend can neither pull remote over local (rb-2096: that clobbers
the unpushed local write) nor push local over remote (that clobbers the peer's
write). Left unreconciled, the compare-and-swap fence either FREEZES the file
forever (a stale If-Match 412s on every retry) or, post-restart with an empty
fence, SILENTLY CLOBBERS the peer (an unconditional PUT). Both outcomes are
wrong for append-friendly coordination stores where the two machines' writes
should simply COMBINE.

This module supplies the third option: MERGE. ``OwnCloudBackend._put``, on
detecting the both-diverged state for a REGISTERED store, calls that store's
handler with ``(local_outgoing_bytes, remote_bytes)`` and PUTs the merged
result fenced on the remote ETag. See ``owncloud_backend._merge_reconcile_put``.

The commutativity invariant (why the fleet converges)
-----------------------------------------------------
Every handler here MUST be COMMUTATIVE at the record/field level:
``merge(a, b)`` and ``merge(b, a)`` produce BYTE-IDENTICAL output. That is the
whole reason the two machines converge — each computes the merge from its own
(local, remote) vantage, and byte-level commutativity makes both reach the same
result, so the ETag-fenced retry loop TERMINATES instead of ping-ponging (A
lands X, B re-merges to the same X, B's next PUT matches or adopts it). Every
tiebreak here is therefore a function of CONTENT (lexicographic order on
canonical JSON), NEVER of the local-vs-remote ROLE — the role differs per
machine and a role-based tiebreak would diverge. Every ordering that reaches
the output (dict-key order, list order) is explicitly SORTED so the bytes do
not depend on set-iteration / hash-seed nondeterminism.

Byte-exact output
-----------------
Handlers serialize in the SAME on-disk format the store's normal writer uses,
so a merge does not flip the file's style (which would churn the sync manifest
and yield spurious diffs):
  - reasoning-bank.jsonl: ``json.dumps(rec, ensure_ascii=True) + "\\n"`` per
    record (matches mind_api/src/endpoints/store.py::_atomic_write_jsonl).
  - team-state.yaml: ``yaml.dump(..., Dumper=CSafeDumper,
    default_flow_style=False, allow_unicode=True, sort_keys=False)`` (matches
    _fileops.locked_modify_yaml).

Registration is by store BASENAME (see ``merge_handler_for``). Adding a new
coordination store = write a commutative handler + add one line to _HANDLERS.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Dict, List, Optional

import yaml

# Ring-buffer ceiling for team-state.recent_completions. Kept in sync with
# core/scripts/team-state.py MAX_RECENT_COMPLETIONS (not imported — that module
# pulls in _paths.WORLD_DIR at import, which this pure/side-effect-free helper
# must stay free of so it is safe to lazily import from the backend hot path).
_MAX_RECENT_COMPLETIONS = 50


# --- content-based helpers (symmetric; the basis of every tiebreak) ---------
def _canon(v) -> str:
    """Canonical JSON for a value — a total, machine-independent order used for
    every content tiebreak. sort_keys makes it independent of dict insertion
    order; ensure_ascii makes it independent of encoding."""
    return json.dumps(v, sort_keys=True, ensure_ascii=True, default=str)


def _newer(x, y) -> bool:
    """True iff ISO-8601 timestamp ``x`` is STRICTLY newer than ``y``. None
    (missing timestamp) sorts oldest. ISO 8601 local timestamps are fixed-width
    ``YYYY-MM-DDTHH:MM:SS``, so a lexical string compare is a chronological
    compare."""
    if x == y:
        return False
    if x is None:
        return False
    if y is None:
        return True
    return str(x) > str(y)


def _order_by_ts(a: dict, b: dict, field: str):
    """Return (winner, loser) ordered by ``field`` timestamp, breaking an equal
    timestamp with a CONTENT tiebreak (larger canonical JSON wins). Symmetric:
    both machines pick the same winner from the same two contents."""
    ta, tb = (a or {}).get(field), (b or {}).get(field)
    if _newer(ta, tb):
        return a, b
    if _newer(tb, ta):
        return b, a
    return (a, b) if _canon(a) >= _canon(b) else (b, a)


# --- reasoning-bank.jsonl : union by record id ------------------------------
def _parse_jsonl(data: bytes) -> List[dict]:
    out: List[dict] = []
    for line in data.decode("utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _dump_jsonl(records: List[dict]) -> bytes:
    return ("".join(json.dumps(r, ensure_ascii=True) + "\n"
                    for r in records)).encode("utf-8")


def _merge_counters(a: dict, b: dict) -> dict:
    """Merge two utilization-counter dicts: union keys, MAX on numeric values
    (a counter only grows — max never loses an increment), content tiebreak
    otherwise. Commutative."""
    out = dict(a)
    for k, vb in b.items():
        if k not in out:
            out[k] = vb
        else:
            va = out[k]
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)) \
                    and not isinstance(va, bool) and not isinstance(vb, bool):
                out[k] = max(va, vb)
            elif va != vb:
                out[k] = va if _canon(va) >= _canon(vb) else vb
    return out


def _merge_rb_record(a: dict, b: dict) -> dict:
    """Field-merge two records that share BOTH id and ``created`` — i.e. the
    SAME record, edited on both machines. Commutative:
      - status:      a retire is a deliberate, monotonic action -> retired-dominates
      - utilization: per-counter MAX (see _merge_counters)
      - valid_to:    a set retirement bound dominates a null; else newer wins
      - everything else: deterministic content tiebreak (larger canon)."""
    out = dict(a)
    for k, vb in b.items():
        if k not in out:
            out[k] = vb
            continue
        va = out[k]
        if va == vb:
            continue
        if k == "status":
            out[k] = "retired" if "retired" in (va, vb) \
                else (va if _canon(va) >= _canon(vb) else vb)
        elif k == "utilization" and isinstance(va, dict) and isinstance(vb, dict):
            out[k] = _merge_counters(va, vb)
        elif k == "valid_to":
            if va is None:
                out[k] = vb
            elif vb is None:
                out[k] = va
            else:
                out[k] = va if _newer(va, vb) or (va == vb) else vb
        else:
            out[k] = va if _canon(va) >= _canon(vb) else vb
    return out


def _merge_id_keyed_jsonl(local: bytes, remote: bytes, *, id_prefix: str,
                          identity_fn, record_merge_fn, id_format) -> bytes:
    """Union two JSONL blobs whose records carry a sequential ``<id_prefix>N`` id
    allocated under a cross-machine lock (reasoning-bank.jsonl, guardrails.jsonl —
    both share this shape). Generic over the id prefix, the stable-identity
    function, the same-record field-merge, and the fresh-id formatter, so a new
    id-keyed coordination store is one thin wrapper + one registry line.

    - a record on ONE side only        -> kept
    - the SAME logical record on both   -> field-merged via ``record_merge_fn``,
      settled at the SMALLEST id it was ever seen under
    - two DISTINCT records that collide on an id (concurrent allocation under a
      cross-machine lock stale-break) -> the earlier-``created`` keeps the id,
      each other is re-assigned the next free ``id_format(N)`` (zero data loss)

    Keying on a STABLE identity (``identity_fn`` — e.g. created+title, created+rule)
    rather than the volatile id is what makes the collision path CONVERGE across
    the multi-round cross-machine fenced-PUT loop: a record one machine re-id'd is
    recognized by the peer as the same record when it returns under its old id, so
    it is NOT re-duplicated each round. ``id_format(N)`` MUST reproduce the store's
    on-disk id byte-for-byte for every N a kept record can settle on (rb: unpadded
    ``rb-N``; guard: 3-pad ``guard-{N:03d}``) — otherwise the re-stamp churns ids
    and the merge never byte-converges. Output is sorted by numeric id for the
    byte-identical result the fenced PUT / commutativity relies on.
    """
    def _int_id(rec_or_id):
        rid = rec_or_id.get("id") if isinstance(rec_or_id, dict) else rec_or_id
        if isinstance(rid, str) and rid.startswith(id_prefix):
            tail = rid[len(id_prefix):]
            if tail.isdigit():
                return int(tail)
        return None

    combined = _parse_jsonl(local) + _parse_jsonl(remote)

    # 1. Collapse by stable identity; field-merge same-identity copies and record
    #    EVERY id each logical record was seen under (its old id + any re-id'd one).
    groups: Dict[tuple, dict] = {}   # identity -> {"rec": merged, "ids": set}
    order: List[tuple] = []          # first-seen identity order (determinism)
    for rec in combined:
        ident = identity_fn(rec)
        g = groups.get(ident)
        if g is None:
            groups[ident] = {"rec": dict(rec), "ids": {rec.get("id")}}
            order.append(ident)
        else:
            g["rec"] = record_merge_fn(g["rec"], rec)
            g["ids"].add(rec.get("id"))

    # 2. Each logical record's preferred id = the SMALLEST numeric id it was seen
    #    under (a re-id'd record settles back to its lowest id; deterministic).
    by_pref: Dict[object, List[dict]] = {}
    for ident in order:
        g = groups[ident]
        nums = [i for i in (_int_id(x) for x in g["ids"]) if i is not None]
        by_pref.setdefault(min(nums) if nums else None, []).append(g["rec"])

    # 3. Assign final ids. Where two DISTINCT records prefer the same id (a true
    #    concurrent-allocation collision), the earlier-``created`` keeps it and
    #    the rest are displaced to fresh ids. Symmetric => both machines converge.
    keepers: List[tuple] = []   # (final_int_id, rec)
    displaced: List[dict] = []
    for pid in by_pref:
        recs = by_pref[pid]
        if pid is None:
            displaced.extend(recs)
            continue
        recs.sort(key=lambda r: (str(r.get("created") or ""), _canon(r)))
        kept = dict(recs[0])
        kept["id"] = id_format(pid)
        keepers.append((pid, kept))
        displaced.extend(recs[1:])

    taken = {pid for pid, _ in keepers}
    displaced.sort(key=lambda r: (str(r.get("created") or ""), _canon(r)))
    next_free = (max(taken) + 1) if taken else 1
    for rec in displaced:
        while next_free in taken:
            next_free += 1
        rec = dict(rec)
        rec["id"] = id_format(next_free)
        keepers.append((next_free, rec))
        taken.add(next_free)
        next_free += 1

    keepers.sort(key=lambda t: t[0])
    return _dump_jsonl([rec for _, rec in keepers])


def _rb_identity(rec: dict):
    """Stable cross-machine identity of a reasoning-bank record: (created, title).
    Both are set at allocation and stable under later edits (utilization
    increments, status retire), so a record keeps this identity even after being
    RE-ID'd on a peer machine. That is what lets the merge recognize a re-id'd
    copy — returning under its OLD id on the other machine — as the SAME logical
    record instead of duplicating it every round. `created` is script-owned
    (second precision) and `title` is the human-authored summary; two DISTINCT
    records sharing BOTH is implausible, so keying on identity (not the volatile
    id) never false-splits and never false-merges in practice."""
    return (str(rec.get("created") or ""), str(rec.get("title") or ""))


def merge_reasoning_bank(local: bytes, remote: bytes) -> bytes:
    """Union two reasoning-bank.jsonl blobs, keyed by STABLE content-identity
    (created + title), NOT by the volatile ``id`` — see ``_merge_id_keyed_jsonl``
    for the union / collision-reid / convergence algorithm. Same-record edits
    reconcile via ``_merge_rb_record`` (retired-dominates status, MAX utilization
    counters). ``rb-N`` is unpadded, so a re-id'd record settles at the next free
    ``rb-N`` (zero data loss). Convergence regression pinned by
    test_rb_multiround_collision_converges.
    """
    return _merge_id_keyed_jsonl(
        local, remote, id_prefix="rb-", identity_fn=_rb_identity,
        record_merge_fn=_merge_rb_record, id_format=lambda n: f"rb-{n}")


# --- guardrails.jsonl : union by record id (same id-keyed shape as rb) -------
def _guard_identity(rec: dict):
    """Stable cross-machine identity of a guardrail record: (created, rule).
    ``created`` is script-owned at allocation; ``rule`` is the human-authored
    imperative text — both stable under later edits (utilization increments,
    status retire, valid_to set). Two DISTINCT guardrails sharing BOTH is
    implausible, so keying on identity (not the volatile ``guard-N`` id) never
    false-splits and recognizes a re-id'd copy as the SAME logical record — the
    property ``_merge_id_keyed_jsonl`` needs to CONVERGE. Mirrors _rb_identity."""
    return (str(rec.get("created") or ""), str(rec.get("rule") or ""))


# Top-level MONOTONIC counters on a guardrail (they only grow, so MAX never loses
# an increment). Unlike reasoning-bank — whose counters ALL live inside the
# ``utilization`` dict (merged by _merge_counters) — a guardrail also carries
# ``times_triggered`` at the TOP level; a bare content tiebreak on it would pick
# the lexically-larger canonical JSON ("9" > "10"), REGRESSING the count.
_GUARD_MONOTONIC_FIELDS = ("times_triggered",)


def _merge_guard_record(a: dict, b: dict) -> dict:
    """Field-merge two guardrail records sharing identity (created + rule) — the
    SAME guardrail edited on both machines. Commutative. Same rules as
    _merge_rb_record (status retired-dominates, utilization per-counter MAX,
    valid_to set-dominates-else-newer, everything else content tiebreak) PLUS
    top-level ``times_triggered`` MAX (a monotonic trigger counter reasoning-bank
    records do not carry)."""
    out = dict(a)
    for k, vb in b.items():
        if k not in out:
            out[k] = vb
            continue
        va = out[k]
        if va == vb:
            continue
        if k == "status":
            out[k] = "retired" if "retired" in (va, vb) \
                else (va if _canon(va) >= _canon(vb) else vb)
        elif k == "utilization" and isinstance(va, dict) and isinstance(vb, dict):
            out[k] = _merge_counters(va, vb)
        elif k == "valid_to":
            if va is None:
                out[k] = vb
            elif vb is None:
                out[k] = va
            else:
                out[k] = va if _newer(va, vb) or (va == vb) else vb
        elif k in _GUARD_MONOTONIC_FIELDS \
                and isinstance(va, (int, float)) and isinstance(vb, (int, float)) \
                and not isinstance(va, bool) and not isinstance(vb, bool):
            out[k] = max(va, vb)
        else:
            out[k] = va if _canon(va) >= _canon(vb) else vb
    return out


def merge_guardrails(local: bytes, remote: bytes) -> bytes:
    """Union two guardrails.jsonl blobs, keyed by STABLE content-identity
    (created + rule), NOT the volatile ``guard-N`` id — see ``_merge_id_keyed_jsonl``
    for the union / collision-reid / convergence algorithm. Same-record edits
    reconcile via ``_merge_guard_record`` (retired-dominates status, MAX
    utilization counters + times_triggered). ``guard-N`` is lock-allocated exactly
    like ``rb-N``, so the same concurrent-allocation collision re-id (zero data
    loss) applies; its on-disk id is 3-pad (``guard-001``), so id_format 3-pads —
    which reproduces every existing id AND matches all future daemon mints
    (n>=913 is always >=3 digits, where pad-0 == pad-3).

    Cures the both-diverged write-freeze on guardrails.jsonl — the 2nd hot
    multi-writer store verified frozen (62 CONFLICT-skips in spawn.log; delta
    2026-07-03 08:42), after aspirations.jsonl (74d227cd). g-001-309.
    """
    return _merge_id_keyed_jsonl(
        local, remote, id_prefix="guard-", identity_fn=_guard_identity,
        record_merge_fn=_merge_guard_record, id_format=lambda n: f"guard-{n:03d}")


# --- team-state.yaml : per-field last-writer-wins + list union --------------
def _dump_yaml(data) -> bytes:
    return yaml.dump(data, Dumper=yaml.CSafeDumper, default_flow_style=False,
                     allow_unicode=True, sort_keys=False).encode("utf-8")


def _union_scalar_list(a: list, b: list) -> list:
    """Union two scalar lists (e.g. acknowledged_by), sorted for determinism."""
    seen: Dict[str, object] = {}
    for x in list(a) + list(b):
        seen.setdefault(_canon(x), x)
    return [seen[k] for k in sorted(seen)]


def _union_dict_list(a: list, b: list, key_fields=()) -> list:
    """Union two lists of dicts, deduped by the first present key field (falling
    back to full canonical content), keeping the larger-canon item on a key
    clash. Output sorted by canonical content. Commutative."""
    seen: Dict[object, object] = {}
    for item in list(a) + list(b):
        k = None
        if isinstance(item, dict):
            for kf in key_fields:
                if kf in item:
                    k = (kf, _canon(item[kf]))
                    break
        if k is None:
            k = ("_canon", _canon(item))
        if k not in seen or _canon(item) > _canon(seen[k]):
            seen[k] = item
    return sorted(seen.values(), key=_canon)


def _completion_ts(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    for f in ("completed_at", "at", "timestamp", "ts"):
        v = item.get(f)
        if v:
            return str(v)
    return ""


def _merge_recent_completions(a: list, b: list) -> list:
    """Union the recent-completions ring buffer by ``goal_id`` (newer completion
    wins a dup), sorted newest-first, trimmed to the buffer ceiling."""
    by_key: Dict[object, dict] = {}
    for item in list(a) + list(b):
        k = item.get("goal_id") if isinstance(item, dict) else None
        if k is None:
            k = _canon(item)
        cur = by_key.get(k)
        if cur is None:
            by_key[k] = item
            continue
        tn, tc = _completion_ts(item), _completion_ts(cur)
        if _newer(tn, tc) or (tn == tc and _canon(item) > _canon(cur)):
            by_key[k] = item
    items = sorted(by_key.values(),
                   key=lambda r: (_completion_ts(r), _canon(r)), reverse=True)
    return items[:_MAX_RECENT_COMPLETIONS]


def _merge_strategic_focus(a: dict, b: dict) -> dict:
    """Newer ``set_at`` wins the scalar focus fields; ``acknowledged_by`` is
    unioned so no acknowledgement from either machine is lost."""
    a, b = a or {}, b or {}
    win, _ = _order_by_ts(a, b, "set_at")
    out = dict(win)
    out["acknowledged_by"] = _union_scalar_list(
        a.get("acknowledged_by") or [], b.get("acknowledged_by") or [])
    return out


def _merge_agent_status(a: dict, b: dict) -> dict:
    """Per-agent last-writer-wins keyed on ``last_active``: each agent normally
    writes only its OWN status, so the union of agent keys preserves every
    agent, and the newer whole-snapshot wins on a same-agent clash (a partial
    field-merge could stitch an inconsistent in_flight/current_focus pair).
    Keys sorted for deterministic YAML."""
    a, b = a or {}, b or {}
    out: Dict[str, object] = {}
    for name in sorted(set(a) | set(b)):
        ra, rb = a.get(name), b.get(name)
        if ra is None:
            out[name] = rb
        elif rb is None:
            out[name] = ra
        else:
            win, _ = _order_by_ts(ra, rb, "last_active")
            out[name] = win
    return out


def merge_team_state(local: bytes, remote: bytes) -> bytes:
    """Field-level reconcile of two team-state.yaml documents.

    The document with the newer top-level ``last_updated`` is the base (so
    opaque / future keys default to last-writer-wins), then the fields with a
    natural merge are overridden:
      - strategic_focus   -> newer set_at wins; acknowledged_by unioned
      - agent_status      -> per-agent, newer last_active wins
      - active/critical_blockers -> unioned (dedup by id/goal_id)
      - recent_completions -> unioned, newest-first, trimmed to the ceiling
    """
    a = yaml.safe_load(local.decode("utf-8")) or {}
    b = yaml.safe_load(remote.decode("utf-8")) or {}
    if not isinstance(a, dict) or not isinstance(b, dict):
        # Non-mapping content is unexpected for team-state; fall back to the
        # content-larger blob so the result is still deterministic.
        return local if _canon(a) >= _canon(b) else remote

    win, _ = _order_by_ts(a, b, "last_updated")
    out = dict(win)  # opaque + future keys ride along from the winner (LWW)
    out["strategic_focus"] = _merge_strategic_focus(
        a.get("strategic_focus") or {}, b.get("strategic_focus") or {})
    out["agent_status"] = _merge_agent_status(
        a.get("agent_status") or {}, b.get("agent_status") or {})
    out["active_blockers"] = _union_dict_list(
        a.get("active_blockers") or [], b.get("active_blockers") or [],
        key_fields=("id", "goal_id"))
    out["critical_blockers"] = _union_dict_list(
        a.get("critical_blockers") or [], b.get("critical_blockers") or [],
        key_fields=("id", "goal_id"))
    out["recent_completions"] = _merge_recent_completions(
        a.get("recent_completions") or [], b.get("recent_completions") or [])
    return _dump_yaml(out)


# --- aspirations.jsonl : union aspirations by id, union goals by id ----------
# The hot ~8MB multipart goal queue written by ALL agents every iteration. With
# NO registered handler it froze on the both-diverged 412 — the 1 route
# (_put's 412 handler -> _merge_reconcile_put) fires ONLY for a registered store,
# so aspirations.jsonl fell through to the doomed RMW retry -> fleet-wide
# write-freeze / silent revert (confirmed 2026-07-03: complete-by returns 200 but
# lastAchievedAt never advances; recurring closes re-select forever). Union is
# the correct reconcile: two agents almost always touch DIFFERENT goals, so a
# goal-id union loses nothing; the rare same-goal clash reconciles per-field.

# Truly-monotonic goal totals — only ever grow -> MAX never loses a bump. NOTE:
# currentStreak / windowStreak / consecutive_routine / consecutive_deep are
# deliberately EXCLUDED — they RESET on a break/outcome-flip, so they are not
# monotonic; they ride along from the last_modified-winner base (latest snapshot).
_GOAL_MAX_FIELDS = (
    "achievedCount", "longestStreak", "longestWindowStreak",
    "substantive_hits", "substantive_runs",
)
# Monotonic timestamps — only advance -> strictly-newer wins (independent of base
# so a stale-base LWW write can never roll back a real achievement).
_GOAL_NEWER_FIELDS = ("lastAchievedAt", "last_substantive_at")
_TERMINAL_STATUSES = ("completed", "skipped", "expired")


def _merge_goal(a: dict, b: dict) -> dict:
    """Commutative merge of two records of the SAME goal id, edited on both
    machines. Base = the newer-``last_modified`` snapshot (LWW for the many
    resettable/opaque fields: currentStreak, consecutive_*, outcome_class,
    defer_*, verification, ...), then the MONOTONIC fields are overridden so a
    stale-base write can never roll back real progress:
      - lastAchievedAt / last_substantive_at : strictly-newer wins (only advance)
      - achievedCount / longest* / substantive_* : numeric MAX (only grow)
      - created_at                            : OLDER wins (stable allocation ts)
      - status: recurring goals CYCLE (completed -> recover-recurring -> pending),
        so status rides the LWW base; NON-recurring terminal statuses
        (completed/skipped/expired) DOMINATE a non-terminal (fixes the both-
        diverged 'completed reverted to in-progress' clobber, delta 2026-07-03).
    Every rule is a symmetric function of (a, b) -> both machines converge."""
    win, _lose = _order_by_ts(a, b, "last_modified")
    out = dict(win)
    # Monotonic timestamps: strictly-newer across BOTH (None sorts oldest).
    for f in _GOAL_NEWER_FIELDS:
        va, vb = a.get(f), b.get(f)
        if va is None and vb is None:
            continue
        out[f] = va if (_newer(va, vb) or va == vb) else vb
    # Monotonic counters: MAX (ignore non-numeric / bool).
    for f in _GOAL_MAX_FIELDS:
        nums = [v for v in (a.get(f), b.get(f))
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if nums:
            out[f] = max(nums)
    # Allocation timestamp: OLDER is canonical (stable across re-writes).
    ca, cb = a.get("created_at"), b.get("created_at")
    if ca is not None and cb is not None:
        out["created_at"] = cb if _newer(ca, cb) else ca
    elif ca is not None:
        out["created_at"] = ca
    elif cb is not None:
        out["created_at"] = cb
    # Status: recurring cycles (base LWW status stands); non-recurring
    # terminal-dominates so a completed goal is never reverted to in-progress.
    if not (bool(a.get("recurring")) or bool(b.get("recurring"))):
        sa, sb = a.get("status"), b.get("status")
        ta, tb = sa in _TERMINAL_STATUSES, sb in _TERMINAL_STATUSES
        if ta and not tb:
            out["status"] = sa
        elif tb and not ta:
            out["status"] = sb
        elif ta and tb and sa != sb:
            out["status"] = sa if _canon(sa) >= _canon(sb) else sb
        # else: both terminal-equal OR both non-terminal -> base LWW status stands
    return out


def _goal_key(g):
    """Stable identity of a goal record: its ``id`` (goals always carry one).
    A pathological id-less/non-dict entry falls back to a content key so it is
    NEVER collapsed with a distinct entry (zero data loss)."""
    if isinstance(g, dict) and g.get("id"):
        return ("id", g["id"])
    return ("_canon", _canon(g))


def _merge_aspiration_record(a: dict, b: dict) -> dict:
    """Merge two records of the SAME aspiration id. Base = newer-``last_selected``
    snapshot (LWW for opaque aspiration-level fields), then:
      - goals             : union by goal id (_merge_goal on same-id clashes)
      - selection_count / sessions_active : numeric MAX (monotonic)
    Goals sorted by identity for the byte-identical result commutativity needs."""
    win, _lose = _order_by_ts(a, b, "last_selected")
    out = dict(win)
    merged: Dict[object, object] = {}
    for g in list(a.get("goals") or []) + list(b.get("goals") or []):
        k = _goal_key(g)
        cur = merged.get(k)
        if cur is None:
            merged[k] = g
        elif isinstance(cur, dict) and isinstance(g, dict) and k[0] == "id":
            merged[k] = _merge_goal(cur, g)
        else:
            merged[k] = cur if _canon(cur) >= _canon(g) else g
    out["goals"] = [merged[k] for k in sorted(merged, key=lambda t: (t[0], _canon(t[1])))]
    for f in ("selection_count", "sessions_active"):
        nums = [v for v in (a.get(f), b.get(f))
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if nums:
            out[f] = max(nums)
    return out


def merge_aspirations(local: bytes, remote: bytes) -> bytes:
    """Union two aspirations.jsonl blobs by aspiration id (goals unioned by goal
    id within — see _merge_aspiration_record / _merge_goal). An aspiration on ONE
    side is kept; the SAME id on both is field-merged. Output sorted by aspiration
    id for the byte-identical result the fenced PUT / commutativity relies on."""
    by_key: Dict[object, object] = {}
    for rec in _parse_jsonl(local) + _parse_jsonl(remote):
        k = (("id", rec["id"]) if isinstance(rec, dict) and rec.get("id")
             else ("_canon", _canon(rec)))
        cur = by_key.get(k)
        if cur is None:
            by_key[k] = rec
        elif isinstance(cur, dict) and isinstance(rec, dict) and k[0] == "id":
            by_key[k] = _merge_aspiration_record(cur, rec)
        else:
            by_key[k] = cur if _canon(cur) >= _canon(rec) else rec
    ordered = [by_key[k] for k in sorted(by_key, key=lambda t: (t[0], _canon(t[1])))]
    return _dump_jsonl(ordered)


# --- pipeline.jsonl / pipeline-archive.jsonl : union by content-derived id ---
# (7 / rb-2849 — BRD P0, the cc-04 fleet wedge.) Hypothesis records
# are EDITED IN PLACE (stage moves via pipeline-move, outcome/reflected/
# surprise set by review-hypotheses), so the append-only LINE-UNION would
# resurrect superseded versions as duplicate ids; the correct reconcile is
# union-by-id + field-merge of same-id copies. Ids are CONTENT-DERIVED
# (YYYY-MM-DD_slug — pipeline.py ID_RE), never allocated from a sequence, so
# the rb/guard concurrent-allocation collision-reid does NOT apply here: two
# machines minting the SAME id authored the same-day/same-slug hypothesis and
# field-merging them is semantically right. Byte-exact: both writers —
# pipeline.py via _fileops.locked_*_jsonl and mind_api pipeline_write.py
# _atomic_write_jsonl — emit json.dumps(rec, ensure_ascii=True) + "\n"
# (== _dump_jsonl). Verified per-store by reading each writer (rb-245).

# Monotonic lifecycle rank: a record only moves FORWARD (pipeline-move
# transitions: discovered->active, active->measurement-pending,
# measurement-pending->resolved|archived, resolved->archived). The
# further-along side is the merge base so a peer's concurrent metadata bump
# can never revert a resolution. Unknown/missing stage ranks lowest.
_PIPELINE_STAGE_RANK = {
    "discovered": 0, "active": 1, "measurement-pending": 2,
    "resolved": 3, "archived": 4,
}
# Monotonic timestamps — only advance; strictly-newer wins independent of base.
_PIPELINE_NEWER_FIELDS = ("last_reviewed", "outcome_date", "reflected_date")
# Resolution facts — written once at resolve time; a set value dominates null.
_PIPELINE_SET_DOMINATES_FIELDS = (
    "outcome", "surprise", "experience_ref", "outcome_detail")


def _merge_pipeline_record(a: dict, b: dict) -> dict:
    """Commutative merge of two records of the SAME hypothesis id, edited on
    both machines. Base = the further-along side by stage rank (the lifecycle
    is monotonic — a resolution/archival must never be reverted by a peer's
    concurrent metadata bump), content tiebreak on equal rank. Then:
      - side-only fields: unioned (a field present on one side is never lost)
      - outcome / surprise / experience_ref / outcome_detail:
        set-dominates-null (both set -> the base's stands, deterministically)
      - reflected: True dominates (the reflect flag is monotonic)
      - last_reviewed / outcome_date / reflected_date: strictly-newer wins
      - formed_date: OLDER wins (stable formation timestamp)
    Every rule is a symmetric function of (a, b) -> both machines converge."""
    ra = _PIPELINE_STAGE_RANK.get(a.get("stage"), -1)
    rb = _PIPELINE_STAGE_RANK.get(b.get("stage"), -1)
    if ra != rb:
        win, lose = (a, b) if ra > rb else (b, a)
    else:
        win, lose = (a, b) if _canon(a) >= _canon(b) else (b, a)
    out = dict(win)
    for k, v in lose.items():
        if k not in out:
            out[k] = v
    for f in _PIPELINE_SET_DOMINATES_FIELDS:
        if out.get(f) is None and lose.get(f) is not None:
            out[f] = lose[f]
    if bool(a.get("reflected")) or bool(b.get("reflected")):
        out["reflected"] = True
    for f in _PIPELINE_NEWER_FIELDS:
        va, vb = a.get(f), b.get(f)
        if va is None and vb is None:
            continue
        out[f] = va if (_newer(va, vb) or va == vb) else vb
    fa, fb = a.get("formed_date"), b.get("formed_date")
    if fa is not None and fb is not None:
        out["formed_date"] = fb if _newer(fa, fb) else fa
    return out


def merge_pipeline(local: bytes, remote: bytes) -> bytes:
    """Union two pipeline JSONL blobs (pipeline.jsonl and pipeline-archive.jsonl
    share the record shape) by hypothesis id — a record on ONE side is kept;
    the SAME id on both is field-merged (_merge_pipeline_record). Output sorted
    by id (YYYY-MM-DD_slug sorts chronologically) for the byte-identical result
    the fenced PUT / commutativity relies on.

    Cures the 2026-07-04..06 cc-04 fleet wedge (g-115-1787 / rb-2849): the
    ~530KB NON-multipart pipeline.jsonl hit _overwrite_decision's "no_clobber"
    with no registered handler, so the stale IfMatch fence 412'd every
    hypothesis add/resolve/reflected write DETERMINISTICALLY until daemon
    restart — bdab36ab cured only the MULTIPART branch of the same freeze.

    CONTENT-SORTED FOLD (not encounter-ordered): the live pipeline-archive.jsonl
    already carries duplicate-id lines WITHIN one file (5 byte-identical groups
    from historical re-appends — 2026-07-07 probe), so a single id can
    contribute 3+ copies across the two sides. A pairwise fold in encounter
    order would make the result depend on the (local, remote) argument order
    unless _merge_pipeline_record were associative — instead each id's copies
    are folded in canonical-content order, making the output a function of the
    record SET alone (commutative + idempotent by construction; identical
    duplicate lines collapse for free)."""
    by_key: Dict[object, List[dict]] = {}
    for rec in _parse_jsonl(local) + _parse_jsonl(remote):
        k = (("id", rec["id"]) if isinstance(rec, dict) and rec.get("id")
             else ("_canon", _canon(rec)))
        by_key.setdefault(k, []).append(rec)
    merged: Dict[object, object] = {}
    for k, copies in by_key.items():
        copies = sorted(copies, key=_canon)
        if k[0] == "id" and all(isinstance(c, dict) for c in copies):
            out = copies[0]
            for c in copies[1:]:
                out = _merge_pipeline_record(out, c)
            merged[k] = out
        else:
            merged[k] = copies[-1]  # largest-canon (content tiebreak)
    ordered = [merged[k] for k in sorted(merged, key=lambda t: (t[0], _canon(t[1])))]
    return _dump_jsonl(ordered)


# --- spark-questions.jsonl : union by stable text identity -------------------
# (rb-2849 — frozen alongside pipeline.jsonl on cc-04.) Records are EDITED IN
# PLACE (times_asked/sparks_generated counter bumps, status retire, candidate
# -> question promotion), so line-union would duplicate. The union is keyed on
# the question TEXT — the stable human-authored identity that survives promote
# (spark_questions_write.promote REWRITES id sq-cNN -> sq-NNN and type
# candidate -> question but keeps text), so a promoted copy and its stale
# candidate twin collapse to ONE record instead of resurrecting the candidate.
# Byte-exact: all three writers (CLI spark-questions.py via
# _fileops.locked_modify_jsonl, the generic daemon store endpoint, and the
# bespoke spark_questions_write increment/promote) emit
# json.dumps(rec, ensure_ascii=True) + "\n" (== _dump_jsonl). rb-245-verified.

_SPARK_COUNTER_FIELDS = ("times_asked", "sparks_generated")


def _spark_identity(rec: dict) -> str:
    """Stable cross-machine identity of a spark record: its question text.
    Set at creation, stable under every edit (counter bumps, retire) AND under
    promotion (which rewrites id + type but never text). Two DISTINCT records
    sharing the exact text is implausible — a duplicate question IS the same
    question — so keying on text never false-splits in practice."""
    return str(rec.get("text") or "")


def _merge_spark_record(a: dict, b: dict) -> dict:
    """Field-merge two records sharing text identity. Commutative.
      - candidate vs question: the QUESTION side wins wholesale — promotion is
        monotonic, and promote deliberately RESET the counters, so the stale
        candidate twin's fields must not bleed back in (it carries none anyway).
      - same type: times_asked / sparks_generated -> per-counter MAX
        (grow-only); yield_rate is DERIVED -> recomputed from the merged
        counters (matches cmd_increment — a blind MAX would overstate it);
        status: retired dominates (a retire is deliberate + monotonic);
        everything else: content-tiebreak base + side-only field union."""
    ta, tb = a.get("type"), b.get("type")
    if ta != tb:
        if ta == "question":
            return dict(a)
        if tb == "question":
            return dict(b)
        return dict(a) if _canon(a) >= _canon(b) else dict(b)
    win, lose = (a, b) if _canon(a) >= _canon(b) else (b, a)
    out = dict(win)
    for k, v in lose.items():
        if k not in out:
            out[k] = v
    for f in _SPARK_COUNTER_FIELDS:
        nums = [v for v in (a.get(f), b.get(f))
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if nums:
            out[f] = max(nums)
    if "retired" in (a.get("status"), b.get("status")):
        out["status"] = "retired"
    if out.get("type") == "question" and "yield_rate" in out:
        out["yield_rate"] = round(
            out.get("sparks_generated", 0) / max(out.get("times_asked", 0), 1), 4)
    return out


def _spark_family(rec: dict):
    """(family_tag, id_format) for a spark record — candidates and questions
    allocate ids from SEPARATE sequences (next_id_for_prefix: sq-001.. 3-pad
    for questions, sq-c01.. 2-pad for candidates), so a displaced record must
    be re-id'd within its OWN family to stay byte-shaped like a minted id."""
    if rec.get("type") == "candidate" or str(rec.get("id") or "").startswith("sq-c"):
        return "candidate", lambda n: f"sq-c{n:02d}"
    return "question", lambda n: f"sq-{n:03d}"


def _spark_int_id(rec: dict):
    """Numeric tail of a spark id within its family (sq-7 / sq-007 -> 7,
    sq-c12 -> 12), or None for a malformed/missing id."""
    rid = str(rec.get("id") or "")
    family, _fmt = _spark_family(rec)
    prefix = "sq-c" if family == "candidate" else "sq-"
    if rid.startswith(prefix):
        tail = rid[len(prefix):]
        if tail.isdigit():
            return int(tail)
    return None


def merge_spark_questions(local: bytes, remote: bytes) -> bytes:
    """Union two spark-questions.jsonl blobs keyed by STABLE text identity
    (survives candidate->question promotion — the promoted copy and its stale
    candidate twin collapse to one record), field-merged via
    _merge_spark_record. A true id collision (two DISTINCT texts under one id —
    concurrent allocation under a cross-machine lock stale-break) is re-id'd:
    the smaller-canon record keeps the contested id, the displaced one gets the
    next free id in ITS OWN family (questions sq-{n:03d}, candidates
    sq-c{n:02d} — matching next_id_for_prefix's zero-pad, which reproduces
    every on-disk id byte-for-byte). Zero data loss. Output sorted questions-
    then-candidates, numeric within family, for the byte-identical result the
    fenced PUT / commutativity relies on."""
    combined = _parse_jsonl(local) + _parse_jsonl(remote)

    # 1. Collapse by text identity; field-merge same-identity copies. Folded
    #    in canonical-content order (NOT encounter order) so the result is a
    #    function of the record SET alone — commutative regardless of the
    #    (local, remote) argument order even when one side carries duplicate
    #    copies (same hardening as merge_pipeline's content-sorted fold).
    raw_groups: Dict[str, List[dict]] = {}
    for rec in combined:
        raw_groups.setdefault(_spark_identity(rec), []).append(rec)
    groups: Dict[str, dict] = {}
    for ident, copies in raw_groups.items():
        copies = sorted(copies, key=_canon)
        out = dict(copies[0])
        for c in copies[1:]:
            out = _merge_spark_record(out, c)
        groups[ident] = out

    # 2. Bucket merged records by (family, final id) to detect true collisions.
    by_id: Dict[tuple, List[dict]] = {}
    for rec in groups.values():
        fam, _fmt = _spark_family(rec)
        by_id.setdefault((fam, _spark_int_id(rec)), []).append(rec)

    # 3. Resolve collisions per family: smallest-canon keeps the contested id;
    #    the rest (and any id-less record) get the next free numeric id in
    #    their family. Symmetric => both machines converge.
    keepers: List[tuple] = []      # (family, int_id, rec)
    displaced: List[tuple] = []    # (family, rec)
    taken: Dict[str, set] = {"question": set(), "candidate": set()}
    for (fam, iid) in by_id:
        recs = sorted(by_id[(fam, iid)], key=_canon)
        if iid is None:
            displaced.extend((fam, r) for r in recs)
            continue
        keepers.append((fam, iid, recs[0]))
        taken[fam].add(iid)
        displaced.extend((fam, r) for r in recs[1:])
    displaced.sort(key=lambda t: (t[0], _canon(t[1])))
    next_free = {"question": 1, "candidate": 1}
    for fam, rec in displaced:
        _same_fam, fmt = _spark_family(rec)
        n = max(next_free[fam], (max(taken[fam]) + 1) if taken[fam] else 1)
        while n in taken[fam]:
            n += 1
        rec = dict(rec)
        rec["id"] = fmt(n)
        keepers.append((fam, n, rec))
        taken[fam].add(n)
        next_free[fam] = n + 1

    keepers.sort(key=lambda t: (0 if t[0] == "question" else 1, t[1]))
    return _dump_jsonl([rec for _fam, _n, rec in keepers])


# --- append-only logs : commutative LINE-UNION (NO field-merge) --------------
# The LARGEST both-diverged freeze class by CONFLICT-skip volume (spawn.log
# 2026-07-03: evolution-log 362, productivity-snapshots 341, gate-firings 157,
# coordination 61, trigger-firings 30, sweep-metrics 32-48 each). Every one of
# these stores is APPEND-ONLY: writers only ever APPEND a record (via
# _fileops.locked_append_jsonl / locked_append_jsonl_with_allocator, or board.py
# whose header states "append-only -- never edited or deleted"), and the ONLY
# rewrites (jsonl_hygiene cap/rotate/archive, trigger_firings _enforce_cap) DROP
# or ARCHIVE whole OLD lines -- they never MUTATE a record in place. So a record
# is immutable once written and two non-identical lines are always DISTINCT
# events, never two edits of one logical record. THAT is why the id-keyed
# field-merge (reasoning-bank/guardrails) would be WRONG here (it would collapse
# two distinct events sharing an id, and these logs have no stable id anyway) and
# a plain line-union is right: the both-diverged reconcile is the UNION of both
# sides' lines with the already-synced baseline prefix collapsed. Verified
# append-only PER-STORE by reading each writer (rb-245 -- never assumed from the
# name). The field-yaml stores named in the same  audit
# (module-health.yaml, _tree.yaml, aspirations-meta.json) are a DISTINCT shape
# and are deliberately NOT handled here (separate follow-up). .
_LOG_TS_FIELDS = ("ts", "timestamp", "date", "at", "created_at", "created", "set_at")


def _log_ts(rec) -> str:
    """First-present timestamp field of an append-only log record, as a string
    (empty when none -> sorts first). Used ONLY to keep the merged log in
    CHRONOLOGICAL order -- the order writers append in, and the order
    jsonl_hygiene's cap/rotate 'keep newest' trim depends on -- NEVER as a record
    identity. Field coverage spans the stamps these logs actually use: ``ts``
    (gate-firings, productivity-snapshots, trigger-firings), ``timestamp`` (board
    channels), ``date`` (evolution-log). Mirrors jsonl_hygiene._ts field intent."""
    if isinstance(rec, dict):
        for f in _LOG_TS_FIELDS:
            v = rec.get(f)
            if v:
                return str(v)
    return ""


def merge_append_only_jsonl(local: bytes, remote: bytes) -> bytes:
    """Commutative LINE-UNION merge for append-only JSONL logs (evolution-log,
    productivity-snapshots, gate-firings, the board channels, trigger-firings,
    the sweep-metrics family). Records are IMMUTABLE once written, so the
    both-diverged reconcile is simply the union of both sides' lines:
      - the SAME record on BOTH sides (the already-synced baseline prefix, in
        local AND remote because it synced before the divergence) -> ONE copy
      - a record on ONE side only (each machine's new appends)     -> kept

    Dedup is by the record's SERIALIZED output line (``json.dumps(rec,
    ensure_ascii=True)`` -- the exact per-record bytes _dump_jsonl emits and the
    writers append), so a byte-identical baseline record collapses to one and the
    dict stored for a given key is always the same (commutative regardless of the
    (local, remote) arg order -- every key maps to identical bytes). Output is
    sorted by (timestamp, canonical JSON) so the merged log stays CHRONOLOGICAL,
    matching the writers' append order and keeping jsonl_hygiene's 'keep newest'
    cap/rotate valid, with the canonical tiebreak making equal / absent timestamps
    deterministic. There is NO field-merge: an append-only record is never edited,
    so two non-identical lines are distinct events (contrast _merge_id_keyed_jsonl,
    where two records CAN be two edits of one logical record). Byte-exact:
    _dump_jsonl matches every writer's ``json.dumps(rec, ensure_ascii=True) + "\\n"``.
    """
    combined = _parse_jsonl(local) + _parse_jsonl(remote)
    by_line: Dict[str, dict] = {}   # serialized line -> record (identical collapse)
    for rec in combined:
        by_line[json.dumps(rec, ensure_ascii=True)] = rec
    ordered = sorted(by_line.values(), key=lambda r: (_log_ts(r), _canon(r)))
    return _dump_jsonl(ordered)


# --- field-level YAML/JSON reconcile (module-health.yaml, aspirations-meta.json) ---
# The 3rd both-diverged freeze shape (), distinct from id-keyed jsonl
# (reasoning-bank/guardrails) AND append-only jsonl (logs): these stores are
# MUTATED IN PLACE -- module_health.record_invocation increments a module's
# counters; aspirations meta_update sets top-level fields -- so neither a
# line-union (records are not immutable events) nor an id-keyed collapse (no
# record id) fits. The reconcile is FIELD-level: a symmetric base + monotonic
# overrides, mirroring merge_team_state / _merge_goal. Verified per-store by
# reading each writer (rb-245). _tree.yaml is named in the same  audit
# but is a 4th, structurally-distinct shape (966KB / 1134-node tree with
# STRUCTURAL children/parent fields + a chronological growth log + CRLF) with a
# far higher blast radius, so it is deliberately carved to a dedicated follow-up
# (). .


def _dump_yaml_default(data) -> bytes:
    """Byte-exact serializer for module-health.yaml: matches
    module_health.save_module_health -- the DEFAULT (pure-python) Dumper with
    width=200 (empirically byte-identical to CSafeDumper on module-health data,
    but the exact writer call is matched so a future long module-id cannot drift
    the wrap width)."""
    return yaml.dump(data, default_flow_style=False, sort_keys=False,
                     allow_unicode=True, width=200).encode("utf-8")


def _merge_module(a: dict, b: dict) -> dict:
    """Reconcile two records of the SAME module id in module-health.yaml.
    Grow-only counters (total_invocations/successful/failed/null_returns) -> MAX
    (never loses an invocation; identical convention to _merge_counters).
    success_rate is DERIVED, so it is RECOMPUTED from the merged counters (matches
    record_invocation) rather than merged directly -- a blind MAX on the ratio
    would overstate it (e.g. 5/13 vs 10/12 -> max(0.385,0.833)=0.833 but the true
    merged rate is 10/13=0.769). avg_latency_ms is a running mean we cannot
    reconstruct without per-side sample counts, so it rides the RICHER side
    (higher total_invocations; content tiebreak on a tie -- 'more data wins').
    Every rule is a symmetric function of (a, b) -> both machines converge."""
    ta = a.get("total_invocations") if isinstance(a.get("total_invocations"), (int, float)) else 0
    tb = b.get("total_invocations") if isinstance(b.get("total_invocations"), (int, float)) else 0
    base = a if (ta > tb or (ta == tb and _canon(a) >= _canon(b))) else b
    out = dict(base)  # richer side's key order + avg_latency_ms + any opaque field
    for f in ("total_invocations", "successful", "failed", "null_returns"):
        nums = [v for v in (a.get(f), b.get(f))
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if nums:
            out[f] = max(nums)
    total = out.get("total_invocations", 0)
    succ = out.get("successful", 0)
    if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0 \
            and isinstance(succ, (int, float)) and not isinstance(succ, bool):
        out["success_rate"] = round(succ / total, 4)
    else:
        out["success_rate"] = 0.0
    return out


def merge_module_health(local: bytes, remote: bytes) -> bytes:
    """Field-level reconcile of two module-health.yaml documents. The structure is
    {modules: {id: {counters + derived}}} with NO top-level timestamp, so the
    reconcile is a UNION of the modules maps: a module on ONE side is kept; the
    SAME module on BOTH is field-merged (_merge_module). Modules are emitted
    SORTED by id so the bytes are identical regardless of (local, remote) arg
    order -- the property the fenced PUT / commutativity relies on (same discipline
    as the id-keyed / append-only handlers, which also sort). Byte-exact to
    module_health.save_module_health via _dump_yaml_default."""
    a = yaml.safe_load(local.decode("utf-8")) or {}
    b = yaml.safe_load(remote.decode("utf-8")) or {}
    if not isinstance(a, dict) or not isinstance(b, dict):
        return local if _canon(a) >= _canon(b) else remote
    am = a.get("modules") if isinstance(a.get("modules"), dict) else {}
    bm = b.get("modules") if isinstance(b.get("modules"), dict) else {}
    merged: Dict[str, object] = {}
    for mid in sorted(set(am) | set(bm)):
        ra, rb = am.get(mid), bm.get(mid)
        if ra is None:
            merged[mid] = rb
        elif rb is None:
            merged[mid] = ra
        elif isinstance(ra, dict) and isinstance(rb, dict):
            merged[mid] = _merge_module(ra, rb)
        else:
            merged[mid] = ra if _canon(ra) >= _canon(rb) else rb
    # Base = content-larger doc so any FUTURE opaque top-level key rides along
    # deterministically (today 'modules' is the only top-level key).
    out = dict(a) if _canon(a) >= _canon(b) else dict(b)
    out["modules"] = merged
    return _dump_yaml_default(out)


_META_MAX_FIELDS = ("session_count", "annecs_solved")
# Monotonic timestamps in aspirations-meta.json -- only advance -> strictly-newer
# wins (independent of the LWW base so a stale-base write can never roll one back).
_META_NEWER_FIELDS = ("last_updated", "last_evolution", "tree.last_maintain_at",
                      "last_calibration_check")


def merge_aspirations_meta(local: bytes, remote: bytes) -> bytes:
    """Field-level reconcile of two aspirations-meta.json documents. Base = the
    newer top-level 'last_updated' snapshot (LWW for the opaque string fields --
    calibration_finding, confidence_calibration_bias, ...), then the fields with a
    natural merge override it:
      - session_count / annecs_solved : numeric MAX (only ever grow)
      - last_updated / last_evolution / tree.last_maintain_at /
        last_calibration_check : strictly-newer wins (monotonic -- only advance)
      - readiness_gates : per-key union (content-larger on a same-key clash)
    Byte-exact to aspirations_write meta_update: json.dumps(indent=2,
    ensure_ascii=True) + '\\n' (the writer adds the trailing newline)."""
    a = json.loads(local.decode("utf-8")) if local.strip() else {}
    b = json.loads(remote.decode("utf-8")) if remote.strip() else {}
    if not isinstance(a, dict) or not isinstance(b, dict):
        return local if _canon(a) >= _canon(b) else remote
    win, _lose = _order_by_ts(a, b, "last_updated")
    out = dict(win)  # winner's key order preserved; opaque fields ride along (LWW)
    for f in _META_MAX_FIELDS:
        nums = [v for v in (a.get(f), b.get(f))
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if nums:
            out[f] = max(nums)
    for f in _META_NEWER_FIELDS:
        va, vb = a.get(f), b.get(f)
        if va is None and vb is None:
            continue
        out[f] = va if (_newer(va, vb) or va == vb) else vb
    ga = a.get("readiness_gates") if isinstance(a.get("readiness_gates"), dict) else {}
    gb = b.get("readiness_gates") if isinstance(b.get("readiness_gates"), dict) else {}
    if ga or gb:
        merged_g: Dict[str, object] = {}
        for k in sorted(set(ga) | set(gb)):
            va2, vb2 = ga.get(k), gb.get(k)
            if va2 is None:
                merged_g[k] = vb2
            elif vb2 is None:
                merged_g[k] = va2
            else:
                merged_g[k] = va2 if _canon(va2) >= _canon(vb2) else vb2
        out["readiness_gates"] = merged_g
    return (json.dumps(out, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def merge_pipeline_meta(local: bytes, remote: bytes) -> bytes:
    """Field-level reconcile of two pipeline-meta.json documents (7 —
    frozen in the same cc-04 flow: every pipeline add/move/update recomputes
    and rewrites this file, so an unmergeable meta would re-freeze the flow the
    pipeline.jsonl handler just unfroze). Everything except
    micro_hypothesis_stats is DERIVED (stage_counts + accuracy are recomputed
    from live+archive records by _update_meta / recompute-meta on every
    mutation), so the LWW base by top-level 'last_updated' (day-precision —
    content tiebreak settles the common same-day case) is self-correcting:
    the next recompute overwrites any residual drift. micro_hypothesis_stats
    is the ONE preserved-not-recomputed section (pipeline_write._update_meta
    carries it across recomputes), so it is unioned per key (content-larger on
    a same-key clash) — a key present on only one machine is never dropped.
    Byte-exact to BOTH writers (_fileops.locked_write_json and
    pipeline_write._update_meta): json.dumps(indent=2, ensure_ascii=True)+'\\n'."""
    a = json.loads(local.decode("utf-8")) if local.strip() else {}
    b = json.loads(remote.decode("utf-8")) if remote.strip() else {}
    if not isinstance(a, dict) or not isinstance(b, dict):
        return local if _canon(a) >= _canon(b) else remote
    win, _lose = _order_by_ts(a, b, "last_updated")
    out = dict(win)  # winner's key order preserved; derived fields ride along (LWW)
    ma = a.get("micro_hypothesis_stats") if isinstance(a.get("micro_hypothesis_stats"), dict) else {}
    mb = b.get("micro_hypothesis_stats") if isinstance(b.get("micro_hypothesis_stats"), dict) else {}
    if ma or mb:
        merged_m: Dict[str, object] = {}
        for k in sorted(set(ma) | set(mb)):
            va, vb = ma.get(k), mb.get(k)
            if va is None:
                merged_m[k] = vb
            elif vb is None:
                merged_m[k] = va
            else:
                merged_m[k] = va if _canon(va) >= _canon(vb) else vb
        out["micro_hypothesis_stats"] = merged_m
    return (json.dumps(out, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


# --- registration -----------------------------------------------------------
_HANDLERS: Dict[str, Callable[[bytes, bytes], bytes]] = {
    # id-keyed field-merge (records edited in place -> merge same-id copies)
    "reasoning-bank.jsonl": merge_reasoning_bank,
    "guardrails.jsonl": merge_guardrails,
    # field-level YAML reconcile
    "team-state.yaml": merge_team_state,
    # aspiration/goal-id union (records edited in place)
    "aspirations.jsonl": merge_aspirations,
    # hypothesis pipeline: union by content-derived id + stage-monotonic
    # field-merge (records edited in place; 7 / rb-2849 — the cc-04
    # NON-multipart no_clobber freeze, sibling of bdab36ab's multipart fix).
    # pipeline-archive.jsonl shares the record shape AND the flow (resolve
    # moves live->archive), so it takes the same handler.
    "pipeline.jsonl": merge_pipeline,
    "pipeline-archive.jsonl": merge_pipeline,
    # spark questions: union by stable text identity (survives candidate ->
    # question promotion), counter-MAX + derived yield_rate recompute
    # (rb-2849 — frozen alongside pipeline.jsonl).
    "spark-questions.jsonl": merge_spark_questions,
    # append-only logs -> LINE-UNION (records immutable; verified append-only
    # per-store by reading each writer, rb-245 / ):
    "evolution-log.jsonl": merge_append_only_jsonl,
    "productivity-snapshots.jsonl": merge_append_only_jsonl,
    "gate-firings.jsonl": merge_append_only_jsonl,
    "trigger-firings.jsonl": merge_append_only_jsonl,
    # board channels (board.py: "append-only -- never edited or deleted"; the
    # canonical 4 documented in CLAUDE.md -- all share the one append-only writer):
    "coordination.jsonl": merge_append_only_jsonl,
    "general.jsonl": merge_append_only_jsonl,
    "findings.jsonl": merge_append_only_jsonl,
    "decisions.jsonl": merge_append_only_jsonl,
    # sweep/recheck telemetry metrics (all via _fileops.locked_append_jsonl):
    "defer-recheck-metrics.jsonl": merge_append_only_jsonl,
    "credential-defer-recheck-metrics.jsonl": merge_append_only_jsonl,
    "precondition-defer-recheck-metrics.jsonl": merge_append_only_jsonl,
    "parent-supersession-sweep-metrics.jsonl": merge_append_only_jsonl,
    "unblock-parent-status-sweep-metrics.jsonl": merge_append_only_jsonl,
    "routing-audit-target-status-sweep-metrics.jsonl": merge_append_only_jsonl,
    # field-level YAML/JSON reconcile (records MUTATED IN PLACE -> per-field
    # reconcile; verified per-store by reading each writer, rb-245 / .
    # _tree.yaml is a 4th, higher-risk shape carved to ):
    "module-health.yaml": merge_module_health,
    "aspirations-meta.json": merge_aspirations_meta,
    # pipeline meta: derived counters (LWW, self-correcting via recompute) +
    # micro_hypothesis_stats per-key union — rewritten by every pipeline
    # mutation, so it must reconcile for the 7 flow to stay unfrozen.
    "pipeline-meta.json": merge_pipeline_meta,
}


def merge_handler_for(path) -> Optional[Callable[[bytes, bytes], bytes]]:
    """Return the commutative merge handler for ``path`` by its basename, or
    None when the store is not merge-registered (the backend then keeps its
    safe-freeze behavior for that path)."""
    return _HANDLERS.get(os.path.basename(str(path)))
