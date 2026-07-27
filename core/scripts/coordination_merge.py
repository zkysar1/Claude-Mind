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
import sys
from typing import Callable, Dict, List, Optional, Tuple

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


def _commutative_key_order(a: dict, b: dict, out: dict) -> dict:
    """Make a field-merged record's KEY ORDER side-independent when the two
    sides' key sequences diverged (g-115-2341). ``out = dict(a)`` + appended
    b-only keys inherits a's insertion order, so when a and b carry DIFFERENT
    key sequences — one side added a new field, or their on-disk orders already
    differ — merge(a, b) and merge(b, a) emit identical VALUES in different
    ORDER. _dump_jsonl serializes insertion order, so the bytes never settle
    and the fenced-PUT loop ping-pongs (guard-907 byte-commutativity violated
    in this corner; no data loss, pure churn). Diverged records are emitted in
    sorted-key order — side-independent AND self-healing: the next round sees
    identical key sequences on both sides and takes the order-preserving path.
    Records whose key sequences already MATCH keep their on-disk order, so
    untouched records never re-order (no blanket-sort churn — the reason
    _dump_jsonl must NOT pass sort_keys)."""
    if list(a.keys()) == list(b.keys()):
        return out
    return {k: out[k] for k in sorted(out)}


def _merge_counters(a: dict, b: dict) -> dict:
    """Merge two utilization-counter dicts: union keys, MAX on numeric values
    (a counter only grows — max never loses an increment), content tiebreak
    otherwise. Commutative (nested key order canonicalized on divergence —
    these dicts serialize inside their parent record, so their insertion
    order reaches the bytes too)."""
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
    return _commutative_key_order(a, b, out)


def _merge_rb_record(a: dict, b: dict) -> dict:
    """Field-merge two records that share BOTH id and ``created`` — i.e. the
    SAME record, edited on both machines. Commutative:
      - status:      a retire is a deliberate, monotonic action -> retired-dominates
      - utilization: per-counter MAX (see _merge_counters)
      - valid_to:    a set retirement bound dominates a null; else newer wins
      - everything else: deterministic content tiebreak (larger canon)
      - key order:   canonicalized when the sides' key sequences diverged
        (_commutative_key_order — byte-commutativity on distinct-key adds)."""
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
    return _commutative_key_order(a, b, out)


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
            prev = g["rec"]
            merged = record_merge_fn(prev, rec)
            # Displacement tombstone is STICKY through the field-merge
            # (, mirrors _merge_goal): the per-store merge fn's LWW
            # base may be the stale pre-displacement copy lacking the field.
            # Union symmetrically (both-set -> lexicographic min) and
            # re-canonicalize key order when the field had to be re-added —
            # the per-store fn already emitted sorted keys for diverged sides
            # (_commutative_key_order), and an append would break that.
            da, db = prev.get("displaced_from"), rec.get("displaced_from")
            tomb = (min(da, db) if isinstance(da, str) and isinstance(db, str)
                    else da if isinstance(da, str) else db)
            if isinstance(tomb, str) and merged.get("displaced_from") != tomb:
                merged = dict(merged)
                merged["displaced_from"] = tomb
                merged = {k: merged[k] for k in sorted(merged)}
            g["rec"] = merged
            g["ids"].add(rec.get("id"))

    # 2. Each logical record's preferred id = the SMALLEST numeric id it was seen
    #    under (a re-id'd record settles back to its lowest id; deterministic) —
    #    UNLESS the record carries a displacement tombstone (, mirrors
    #    _merge_goals): then its LATEST settled displacement slot is preferred,
    #    so a stale replica replaying the pre-collision copy cannot drag it back
    #    into the contested bucket and re-displace it to a pair-dependent id.
    #    Pure function of merged group content -> commutative (guard-907).
    by_pref: Dict[object, List[dict]] = {}
    for ident in order:
        g = groups[ident]
        nums = [i for i in (_int_id(x) for x in g["ids"]) if i is not None]
        home = _int_id(g["rec"].get("displaced_from")) if isinstance(
            g["rec"].get("displaced_from"), str) else None
        non_home = [n for n in nums if n != home]
        if home is not None and non_home:
            pref = max(non_home)
        else:
            pref = min(nums) if nums else None
        by_pref.setdefault(pref, []).append(g["rec"])

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
        # Displacement tombstone (, mirrors _merge_goals): record the
        # contested id this record LOST, once (first home wins on chained
        # displacement), and emit sorted-key at the moment the record gains the
        # key — so a later re-merge against a stale tombstone-less copy
        # canonicalizes to exactly these bytes (fixpoint) instead of leaving
        # stamped-append order the replay's sort can never reproduce.
        if not rec.get("displaced_from") and isinstance(rec.get("id"), str) \
                and rec.get("id"):
            rec["displaced_from"] = rec.get("id")
            rec["id"] = id_format(next_free)
            rec = {k: rec[k] for k in sorted(rec)}
        else:
            # id-less fragment (pid=None lane) or already-tombstoned: no new
            # key gained -> keep on-disk key order (no blanket-sort churn).
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
    return _commutative_key_order(a, b, out)


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


# --- pattern-signatures.jsonl : union by id (id-keyed shape, ) -----
def _sig_identity(rec: dict):
    """Stable cross-machine identity of a pattern-signature record:
    (created, name). ``created`` is DATE-only (coarser than rb's
    second-precision, but two DISTINCT signatures minted the same day with the
    same human-authored name is implausible) and both fields are stable under
    later edits (record-outcome counter bumps, set-status retire). Mirrors
    _rb_identity / _guard_identity."""
    return (str(rec.get("created") or ""), str(rec.get("name") or ""))


def _merge_sig_record(a: dict, b: dict) -> dict:
    """Field-merge two pattern-signature records sharing identity (created +
    name) — the SAME signature edited on both machines. Commutative. Same rules
    as _merge_rb_record (status retired-dominates, utilization per-counter MAX,
    everything else content tiebreak) PLUS:
      - outcome_stats: confirmed/total per-counter MAX, then ``accuracy``
        RECOMPUTED from the merged counters with the writer's exact rounding
        (pattern_signatures_write.record_outcome: round(c/t, 4)). A content
        tiebreak here would keep a stale ratio inconsistent with the merged
        counters.
      - last_matched: monotonic date stamp — newer wins.
      - sample_size: monotonic evidence count — numeric MAX (a bare content
        tiebreak lexically prefers "9" over "26", regressing the count — the
        same hazard _GUARD_MONOTONIC_FIELDS exists for)."""
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
        elif k == "outcome_stats" and isinstance(va, dict) and isinstance(vb, dict):
            merged = _merge_counters(va, vb)
            total = merged.get("total")
            confirmed = merged.get("confirmed")
            if isinstance(total, (int, float)) and not isinstance(total, bool) \
                    and isinstance(confirmed, (int, float)) \
                    and not isinstance(confirmed, bool):
                merged["accuracy"] = (round(confirmed / total, 4)
                                      if total > 0 else 0.0)
            out[k] = merged
        elif k == "last_matched":
            out[k] = va if _newer(va, vb) or (va == vb) else vb
        elif k == "sample_size" \
                and isinstance(va, (int, float)) and isinstance(vb, (int, float)) \
                and not isinstance(va, bool) and not isinstance(vb, bool):
            out[k] = max(va, vb)
        else:
            out[k] = va if _canon(va) >= _canon(vb) else vb
    return _commutative_key_order(a, b, out)


def merge_pattern_signatures(local: bytes, remote: bytes) -> bytes:
    """Union two pattern-signatures.jsonl blobs, keyed by STABLE
    content-identity (created + name) — see ``_merge_id_keyed_jsonl`` for the
    union / collision-reid / convergence algorithm. Same-record edits reconcile
    via ``_merge_sig_record`` (retired-dominates status, counter-MAX
    outcome_stats with accuracy recompute, newer last_matched).

    The store's on-disk ids are MIXED-format — the original allocator 3-padded
    (``sig-001``..``sig-007``) and the current one is unpadded (``sig-8``+) —
    so no pure ``id_format(n)`` reproduces every id byte-for-byte (the generic
    helper's contract). The formatter therefore PRESERVES the form each id was
    OBSERVED under in either input (built symmetrically from both blobs:
    longer/padded form wins a same-int form clash, then lexicographic — a
    deterministic, commutative preference), and falls back to the current
    allocator's unpadded ``sig-{n}`` only for FRESH ids minted by the
    collision-displacement path (always > max existing, so never legacy-padded).
    Re-stamping legacy ids to a uniform width instead would rename records out
    from under external references (guard-575 and skill text cite ``sig-003``;
    weakness-report signal_baseline keys on sig ids).

    Closes the last unadjudicated g-115-2319 store: writers mutate records in
    place (record-outcome counter bumps), so an unregistered both-diverged 412
    froze the file fleet-wide (rb-3150 class) — line-union would instead
    resurrect retired signatures. g-115-2333."""
    observed: Dict[int, str] = {}
    for rec in _parse_jsonl(local) + _parse_jsonl(remote):
        rid = rec.get("id")
        if isinstance(rid, str) and rid.startswith("sig-") and rid[4:].isdigit():
            n = int(rid[4:])
            prev = observed.get(n)
            if prev is None or (len(rid), rid) > (len(prev), prev):
                observed[n] = rid

    def _sig_id_format(n: int) -> str:
        return observed.get(n, f"sig-{n}")

    return _merge_id_keyed_jsonl(
        local, remote, id_prefix="sig-", identity_fn=_sig_identity,
        record_merge_fn=_merge_sig_record, id_format=_sig_id_format)


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
    # Singleton-doc key order side-independent on divergence (: the
    # exact-equal-canon tiebreak returns the FIRST arg, so identical content in
    # different serialization order ping-pongs; same helper as ).
    return _dump_yaml(_commutative_key_order(a, b, out))


def merge_team_state_shard(local: bytes, remote: bytes) -> bytes:
    """Commutative merge of two divergent versions of a SINGLE per-agent
    team-state shard (``world/team-state/agents/<name>.yaml``, the g-328-27
    split).

    Each shard is a FLAT single-agent status document (last_active,
    current_focus, live_phase, session_goals_completed, row_updated, ...) —
    normally only the OWNING agent writes it, so a both-diverged clash
    reconciles by whole-snapshot last-writer-wins on ``last_active`` (mirrors
    ``_merge_agent_status``'s per-agent rule: a partial field-merge could
    stitch an inconsistent in_flight/current_focus/live_phase triple, and the
    per-session ``session_goals_completed`` counter resets so it is NOT a
    monotonic-max field — the newer whole snapshot is authoritative). Winner
    is re-dumped (not returned raw) so both machines emit byte-identical output
    regardless of input YAML formatting.

    This gives per-agent shards the SAME both-diverged self-heal the composite
    ``team-state.yaml`` already had, closing the rb-3150 peer-shard freeze: the
    basename-keyed _HANDLERS never registered the dynamic shard basenames, so
    ``merge_handler_for`` returned None and the backend froze peer shards on the
    both-diverged 412 -> every box saw fresh-SELF + stale-PEERS. Dispatched by
    the path-pattern branch in ``merge_handler_for`` (g-115-2133)."""
    a = yaml.safe_load(local.decode("utf-8")) or {}
    b = yaml.safe_load(remote.decode("utf-8")) or {}
    if not isinstance(a, dict) or not isinstance(b, dict):
        # Non-mapping content is unexpected for a shard; fall back to the
        # content-larger blob so the result stays deterministic + commutative.
        return local if _canon(a) >= _canon(b) else remote
    win, _ = _order_by_ts(a, b, "last_active")
    # Key order side-independent on divergence (; see merge_team_state).
    return _dump_yaml(_commutative_key_order(a, b, dict(win)))


# --- aspirations.jsonl : union aspirations by id, union goals by id ----------
# The hot ~8MB multipart goal queue written by ALL agents every iteration. With
# NO registered handler it froze on the both-diverged 412 — the  route
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
      - claimed_by/claimed_at : live claims by DIFFERENT agents -> first-claim-
        wins on claimed_at (stamped beats timestamp-less; full tie ->
        lexicographic-smaller claimed_by); the pair moves as a UNIT. ONE-side-
        null -> KEEP the non-null claim UNLESS the null side is PROVABLY NEWER
        (its last_modified strictly postdates the claim's claimed_at) (g-115-2547).
        Neither claim() nor release() stamps last_modified, so claimed_at (not the
        claim side's pre-claim last_modified) is the claim's recency signal: a null
        side newer than claimed_at is a genuine release-then-edit and clears the
        claim; a null side older-or-equal (incl. the observed both-lack-
        last_modified tie) is a stale PRE-claim snapshot and the claim is kept.
        Preserving the claim is asymmetric-safe -- a dropped LIVE claim means two
        agents both own the goal (double-claim), while a resurrected pure-RELEASE
        claim (last_modified never advanced) self-heals via the stale-claim
        take-back. Merged NON-recurring terminal status still clears the pair
        (write-path claim-clearing mirror, aspirations.py cmd_update_goal Rule 3)
        -- so a genuine completion clears the claim regardless of this rule.
        Fixes the second-claimer steal: claim() does not stamp last_modified, so
        concurrent claims tie on the LWW base and fell to the _canon content
        tiebreak (g-115-1918 / rb-3043).
      - key order: canonicalized when the sides' key sequences diverged
        (_commutative_key_order, g-115-2355 — the _order_by_ts full-content-tie
        otherwise picks the FIRST ARG between value-identical order-divergent
        copies, and dict(win) key order reaches the bytes).
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
    # Displacement tombstone is STICKY (): the LWW base may be the
    # stale pre-displacement copy (which lacks the field) — a tombstone present
    # on either side survives the field-merge, or _merge_goals' anchor cannot
    # recognize the settled displacement on re-merge. Both-set-and-different
    # (divergent displacement histories) picks the lexicographic min —
    # symmetric in (a, b).
    da, db = a.get("displaced_from"), b.get("displaced_from")
    if isinstance(da, str) and isinstance(db, str):
        out["displaced_from"] = min(da, db)
    elif isinstance(da, str):
        out["displaced_from"] = da
    elif isinstance(db, str):
        out["displaced_from"] = db
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
    # Claim pair: first-claim-wins for LIVE claims by DIFFERENT agents
    # ( / rb-3043 second-claimer steal). Symmetric in (a, b):
    # comparisons are on field VALUES, never argument order.
    ca_by, cb_by = a.get("claimed_by"), b.get("claimed_by")
    if ca_by and cb_by and ca_by != cb_by:
        ca_at, cb_at = a.get("claimed_at"), b.get("claimed_at")
        if ca_at and cb_at:
            if ca_at != cb_at:
                first = a if ca_at < cb_at else b   # older claim stands
            else:
                first = a if ca_by < cb_by else b   # full tie -> lexicographic
        elif ca_at or cb_at:
            first = a if ca_at else b               # stamped beats timestamp-less
        else:
            first = a if ca_by < cb_by else b       # both unstamped -> lexicographic
        out["claimed_by"] = first.get("claimed_by")
        if first.get("claimed_at") is not None:
            out["claimed_at"] = first["claimed_at"]
        else:
            out.pop("claimed_at", None)             # pair moves as a unit
    elif bool(ca_by) != bool(cb_by):
        # Exactly ONE side carries a live claim; the other is null. Keep the
        # claim UNLESS the null side is PROVABLY NEWER than the claim -- i.e. its
        # last_modified strictly postdates the claim's claimed_at, which marks a
        # genuine RELEASE followed by a later edit (or other supersession).
        # Otherwise the null side is a stale PRE-claim snapshot and dropping the
        # claim is the  double-claim hazard (two agents both believe
        # they own the goal).
        #
        # Why claimed_at, not the claim side's last_modified: neither claim() nor
        # release() stamps last_modified (aspirations_write.py claim ~L3454,
        # release ~L3106), so the claim side's last_modified is its PRE-claim
        # value -- useless as a claim-recency signal. claimed_at IS the claim's
        # recency. The observed live bug is exactly the "both sides lack
        # last_modified" tie where the content tiebreak dropped the claim: there
        # null_lm is None and _newer(None, claimed_at) is False -> not provably
        # newer -> the claim is kept (the fix).
        #
        # Asymmetric-safe by design: keeping a stale claim self-heals (the
        # stale-claim take-back clears it after effective_timeout); dropping a
        # live claim does not (it double-claims). A pure release that never got a
        # later edit is resurrected here (its last_modified was never advanced) --
        # accepted, because that too self-heals via timeout. A genuine completion
        # is STILL cleared by the terminal-status block below regardless of this
        # branch. Byte-commutative: claim_side/null_side are chosen by field value
        # (which side is non-null), never argument order; the _newer comparison is
        # on those field values; the pair moves as a unit.
        claim_side = a if ca_by else b
        null_side = b if ca_by else a
        claimed_at = claim_side.get("claimed_at")
        if _newer(null_side.get("last_modified"), claimed_at):
            # Null side provably newer -> genuine release/supersession -> clear.
            out.pop("claimed_by", None)
            out.pop("claimed_at", None)
        else:
            # Stale pre-claim snapshot -> keep the claim (double-claim guard).
            out["claimed_by"] = claim_side.get("claimed_by")
            if claimed_at is not None:
                out["claimed_at"] = claimed_at
            else:
                out.pop("claimed_at", None)         # pair moves as a unit
    # Merged NON-recurring terminal status clears the claim pair (merge-layer
    # mirror of the write-path claim-clearing invariant). Recurring goals
    # cycle completed -> pending, so their claims are left to the LWW base.
    if (not (bool(a.get("recurring")) or bool(b.get("recurring")))
            and out.get("status") in _TERMINAL_STATUSES):
        out.pop("claimed_by", None)
        out.pop("claimed_at", None)
    return _commutative_key_order(a, b, out)


def _goal_identity(g):
    """Stable cross-machine identity of a goal, invariant under re-id — the basis
    of the collision-tolerant goal union (the nested-array analogue of
    ``_rb_identity``). A NEWLY-allocated goal always carries BOTH ``created_at``
    and ``title`` (set together at allocation, aspirations.py), so
    ``(created_at, title)`` identifies it even after a peer re-ids it — the
    property that makes the collision path CONVERGE across the fenced-PUT loop.
    Goals missing either (legacy pre-``created_at`` goals, id-less fragments)
    fall back to id-identity — the pre-fix behaviour — so they field-merge by id
    exactly as before and this change is a no-op for them (they are already
    allocated and never NEWLY collide). A non-dict entry falls back to content so
    it is never collapsed with a distinct entry (zero data loss)."""
    if isinstance(g, dict):
        ct, ti = g.get("created_at"), g.get("title")
        if ct and ti:
            return ("ct", ct, ti)
        gid = g.get("id")
        if gid:
            return ("id", gid)
    return ("_canon", _canon(g))


def _merge_goals(goals_a, goals_b, asp_num: str, evicted_ids=frozenset()) -> list:
    """Collision-tolerant union of two goals arrays for the SAME aspiration — the
    nested-array analogue of ``_merge_id_keyed_jsonl`` (the rb/guardrail keep-both
    algorithm), which aspirations could not use directly because goals live nested
    inside an aspiration record, not as flat top-level jsonl lines.

    - same content-identity (the SAME logical goal edited on both machines)
      -> field-merged via ``_merge_goal``
    - two DISTINCT goals that collided on a PURE-sequential id ``g-{asp}-{seq}``
      (the decentralized max+1 allocation race — aspirations.py ~L1165, two boxes
      pick the same next id in one eventual-consistency window) -> the
      earlier-``created_at`` keeps the id, the rest are re-assigned the next free
      ``g-{asp}-{seq}``. ZERO content loss — this REPLACES the old id-keyed union
      that field-interleaved the two distinct goals into one franken-record and
      dropped a writer's content (g-115-2147; observed live g-335-34 add-loss,
      g-335-32 update-loss).

    Variant ids (``g-{asp}-{seq}-{letter}``), foreign-aspiration ids, and id-less
    fragments are OUTSIDE this aspiration's sequential space, so they never
    collide — grouped by identity and passed through with their exact id.

    ``evicted_ids`` (g-115-2430) is the union of both sides'
    ``archived_census.evicted_ids`` — the eviction TOMBSTONE set. A goal whose id
    is in it was removed by aspirations-evict-completed and is already counted in
    the census; a live copy arriving from a stale replica is a RESURRECTION
    (guard-1072 slow lane of the g-115-2401 phantom producer: union re-adds it,
    the next evict re-bumps the census, counts inflate). Drop such copies at
    entry. Safe against id REUSE because the mint sites (aspirations.py /
    aspirations_write.py) allocate max+1 over live ∪ evicted ids, so a new goal
    can never legitimately carry an evicted id. Evicted seqs are also excluded
    from displacement re-allocation below for the same reason.

    Symmetric in (a, b): the id-assignment tiebreak is a pure function of content
    (earlier created_at, then canonical-JSON order), and the output is id-sorted,
    so both machines converge byte-identically (the fenced-PUT invariant)."""
    def _seq(gid):
        # The PURE sequential seq of THIS aspiration: g-{asp_num}-{digits}, no
        # trailing -letter variant, not a foreign aspiration. Else None.
        if not isinstance(gid, str):
            return None
        parts = gid.split("-")
        if (len(parts) == 3 and parts[0] == "g"
                and parts[1] == asp_num and parts[2].isdigit()):
            return int(parts[2])
        return None

    def _fmt(n):
        return f"g-{asp_num}-{n:02d}"   # byte-exact to aspirations.py allocation

    combined = [g for g in list(goals_a) + list(goals_b)
                if not (isinstance(g, dict) and g.get("id") in evicted_ids)]

    # 1. Collapse by content identity; field-merge same-identity copies; record
    #    every id each logical goal was seen under (old id + any peer re-id).
    groups: Dict[tuple, dict] = {}
    order: List[tuple] = []
    for g in combined:
        ident = _goal_identity(g)
        grp = groups.get(ident)
        if grp is None:
            groups[ident] = {"rec": g,
                             "ids": {g.get("id") if isinstance(g, dict) else None}}
            order.append(ident)
        else:
            cur = grp["rec"]
            if isinstance(cur, dict) and isinstance(g, dict):
                grp["rec"] = _merge_goal(cur, g)
            else:
                grp["rec"] = cur if _canon(cur) >= _canon(g) else g
            grp["ids"].add(g.get("id") if isinstance(g, dict) else None)

    # 2. Sequential-id goals enter collision resolution at their SMALLEST seq;
    #    everything else (variant / foreign / id-less) passes through unchanged.
    by_pref: Dict[int, List[dict]] = {}
    passthrough: List[dict] = []
    for ident in order:
        grp = groups[ident]
        seqs = sorted({s for s in (_seq(i) for i in grp["ids"]) if s is not None})
        if seqs:
            rec = grp["rec"]
            # A goal seen under MULTIPLE sequential ids (its original id + a peer's
            # re-id of the SAME goal) has a field-merged rec whose `id` is LWW-chosen
            # and may be ANY of those ids -- including one HIGHER than its home (min)
            # seq. If that foreign-seq id rides into the min-seq bucket, the keeper
            # carries an id belonging to a DIFFERENT bucket and collides with THAT
            # bucket's keeper -- two distinct goals sharing one id in a single pass
            # ( fresh-eyes finding). Anchor a multi-seq group to its home id
            # so every keeper's id matches its bucket. Single-seq groups are left
            # verbatim -- no churn of legacy non-canonical g-N-N ids.
            if len(seqs) >= 2 and isinstance(rec, dict):
                rec = dict(rec)
                # Anchor choice ( associativity hardening): when the
                # group carries a displacement tombstone whose home seq is one
                # of the seen ids, the OTHER (higher) id is this goal's SETTLED
                # displacement slot — anchor there so a stale replica replaying
                # the pre-collision copy does not drag the goal back into the
                # contested home bucket and re-displace it to a pair-dependent
                # next-free id (merge(merge(a,b),a) now == merge(a,b) for the
                # collision path). Without a tombstone the higher id is drift
                # of unknown provenance (e.g. a peer's LWW edit) — anchor home
                # (min) exactly as before, so a same-goal id drift can never
                # steal a DISTINCT goal's settled slot ( fixture).
                # Pure function of merged group content -> commutative
                # (guard-907).
                home = _seq(rec.get("displaced_from")) if isinstance(
                    rec.get("displaced_from"), str) else None
                non_home = [s for s in seqs if s != home]
                if home is not None and non_home:
                    # Tombstoned: anchor the LATEST settled displacement slot.
                    # Applies whether or not the home seq is still among the
                    # seen ids — a CHAIN-displaced goal (lost its first slot to
                    # an earlier-created contender) has home outside seqs, and
                    # anchoring min would re-fight a bucket it already lost,
                    # relying on vacated-slot dynamics to land back (fresh-eyes
                    # 2026-07-16 second pass: fragile, breaks with two chained
                    # replays or an adjacent allocation).
                    anchor = max(non_home)
                else:
                    anchor = seqs[0]
                rec["id"] = _fmt(anchor)
                by_pref.setdefault(anchor, []).append(rec)
            else:
                by_pref.setdefault(seqs[0], []).append(rec)
        else:
            passthrough.append(grp["rec"])

    # 3. Resolve id collisions: earlier-created_at keeps the id, the rest are
    #    displaced to fresh sequential ids (symmetric => convergent).
    keepers: List[dict] = []
    displaced: List[dict] = []
    for pid in by_pref:
        recs = by_pref[pid]
        recs.sort(key=lambda r: (str(r.get("created_at") or ""), _canon(r)))
        # Winner keeps its EXISTING id verbatim (byte-faithful): for a real
        # canonical id g-{asp}-{seq:02d} this already equals _fmt(pid), and for a
        # legacy/non-canonical id it avoids churning a stable id. Only a
        # DISPLACED goal — which genuinely needs a fresh id — gets canonical _fmt.
        keepers.append(dict(recs[0]))
        displaced.extend(recs[1:])
    taken = {s for s in (_seq(k.get("id")) for k in keepers) if s is not None}
    # Evicted seqs are allocated-forever: a displaced goal re-id'd onto one
    # would be tombstone-dropped by the NEXT merge ().
    taken |= {s for s in (_seq(i) for i in evicted_ids) if s is not None}
    displaced.sort(key=lambda r: (str(r.get("created_at") or ""), _canon(r)))
    next_free = (max(taken) + 1) if taken else 1
    for rec in displaced:
        while next_free in taken:
            next_free += 1
        rec = dict(rec)
        # Displacement tombstone (): record the contested id this goal
        # LOST, once (first home wins — never overwritten on chained
        # displacement). Step 2's anchor reads it to recognize a prior
        # displacement on re-merge, so a stale replica replaying the
        # pre-collision copy cannot re-fight the bucket and shuffle the goal to
        # a different next-free id (collision re-id was commutative but
        # NON-ASSOCIATIVE without it). Also the audit trail for external
        # references (claims, discovered_by, origin_signal dedup) that still
        # point at the lost id.
        if not rec.get("displaced_from"):
            rec["displaced_from"] = rec.get("id")
        rec["id"] = _fmt(next_free)
        # Sorted-key emission at the moment the record gains a new key — the
        # same self-healing convention as _commutative_key_order, so a later
        # re-merge against a stale (tombstone-less) copy canonicalizes to
        # exactly these bytes (fixpoint), instead of leaving the settled bytes
        # in stamped-append order that the replay's sort can never reproduce.
        rec = {k: rec[k] for k in sorted(rec)}
        keepers.append(rec)
        taken.add(next_free)
        next_free += 1

    # 4. Deterministic id-then-content order for the byte-identical result.
    result = keepers + passthrough
    result.sort(key=lambda g: (str(g.get("id") if isinstance(g, dict) else "") or "",
                               _canon(g)))
    return result


def _merge_archived_census(a_census, b_census):
    """Merge two ``archived_census`` dicts with per-field semantics ( —
    the fix for the g-115-2401 census phantom producer, guard-1153: counters in
    record-merge handlers need EXPLICIT merge semantics, never the opaque-LWW
    default; the old LWW-by-``last_selected`` ride-along reverted census repairs
    within ~81min on hot aspirations because ``last_selected`` is bumped by
    SELECTION, uncorrelated with census mutation).

      - ``evicted_ids`` : per-status goal-id SET UNION (commutative, idempotent,
        associative; sorted deduped lists for the byte-identical result). Ids
        are the post-cutover census ground truth AND the resurrection tombstone
        consumed by _merge_goals.
      - ``by_status``   : the FROZEN legacy count baseline. Per-status MIN when
        both sides carry the key (post-cutover only census REPAIRS mutate it,
        and repairs SHRINK — min converges to the most-repaired value, so a
        repair survives a stale peer instead of reverting). A key on one side
        only is kept verbatim (absent = no opinion, NOT zero — else any merge
        against a pre-census copy would zero the baseline). Mixed-fleet
        old-code increments get min'd away: a bounded, conservation-safe
        undercount, accepted by design.
      - other keys (``census_note``...) : deterministic canonical-max pick.

    Returns None when neither side has a census (key stays absent). Output keys
    sorted for byte determinism."""
    a = a_census if isinstance(a_census, dict) else None
    b = b_census if isinstance(b_census, dict) else None
    if a is None and b is None:
        return None
    if a is None or b is None:
        out = dict(a if a is not None else b)
        ids = out.get("evicted_ids")
        if isinstance(ids, dict):   # normalize shape even on the one-sided path
            out["evicted_ids"] = {
                s: sorted({str(x) for x in v})
                for s, v in sorted(ids.items()) if isinstance(v, list) and v}
        return {k: out[k] for k in sorted(out)}
    out = {}
    ids_a = a.get("evicted_ids") if isinstance(a.get("evicted_ids"), dict) else {}
    ids_b = b.get("evicted_ids") if isinstance(b.get("evicted_ids"), dict) else {}
    merged_ids = {}
    for s in set(ids_a) | set(ids_b):
        va = ids_a.get(s) if isinstance(ids_a.get(s), list) else []
        vb = ids_b.get(s) if isinstance(ids_b.get(s), list) else []
        u = sorted({str(x) for x in va} | {str(x) for x in vb})
        if u:
            merged_ids[s] = u
    if merged_ids:
        out["evicted_ids"] = {s: merged_ids[s] for s in sorted(merged_ids)}
    bs_a = a.get("by_status") if isinstance(a.get("by_status"), dict) else {}
    bs_b = b.get("by_status") if isinstance(b.get("by_status"), dict) else {}
    merged_bs = {}
    for s in set(bs_a) | set(bs_b):
        vals = []
        for src in (bs_a, bs_b):
            if s in src:
                try:
                    vals.append(max(0, int(src[s])))
                except (TypeError, ValueError):
                    continue
        if vals:
            merged_bs[s] = min(vals)
    if merged_bs or "by_status" in a or "by_status" in b:
        out["by_status"] = {s: merged_bs[s] for s in sorted(merged_bs)}
    for k in sorted(set(a) | set(b)):
        if k in ("evicted_ids", "by_status"):
            continue
        va, vb = a.get(k), b.get(k)
        if va is None:
            out[k] = vb
        elif vb is None:
            out[k] = va
        else:
            out[k] = va if _canon(va) >= _canon(vb) else vb
    return {k: out[k] for k in sorted(out)}


def _merge_aspiration_record(a: dict, b: dict) -> dict:
    """Merge two records of the SAME aspiration id. Base = newer-``last_selected``
    snapshot (LWW for opaque aspiration-level fields), then:
      - archived_census   : explicit per-field semantics — evicted_ids UNION,
        legacy by_status MIN, never LWW (_merge_archived_census, g-115-2430).
        Also fixes the latent lose-side loss lane: a census present only on the
        LWW-losing side previously vanished entirely.
      - goals             : union by goal id (_merge_goal on same-id clashes),
        minus both sides' evicted_ids (resurrection tombstone)
      - selection_count / sessions_active : numeric MAX (monotonic)
      - key order         : canonicalized when the sides' key sequences
        diverged (_commutative_key_order, g-115-2355 full-tie corner)
    Goals sorted by identity for the byte-identical result commutativity needs."""
    win, _lose = _order_by_ts(a, b, "last_selected")
    out = dict(win)
    census = _merge_archived_census(a.get("archived_census"),
                                    b.get("archived_census"))
    if census is not None:
        out["archived_census"] = census
    # Garbage-tolerant: a one-sided census passes through with its shape only
    # normalized when well-formed, so a hand-corrupted evicted_ids (non-dict,
    # or non-list bucket) must degrade to "no tombstones", never crash the
    # store merge (fresh-eyes-code  finding).
    _ids = (census or {}).get("evicted_ids")
    evicted = frozenset(
        str(gid)
        for vals in (_ids.values() if isinstance(_ids, dict) else ())
        if isinstance(vals, list)
        for gid in vals)
    # Goals unioned with concurrent-allocation collision tolerance: two DISTINCT
    # goals that raced to the same g-{asp}-{seq} id are BOTH kept (loser re-id'd),
    # not field-interleaved into one franken-record (). asp_num scopes
    # the re-id sequence to this aspiration.
    asp_num = str(out.get("id", "")).replace("asp-", "")
    out["goals"] = _merge_goals(a.get("goals") or [], b.get("goals") or [],
                                asp_num, evicted_ids=evicted)
    for f in ("selection_count", "sessions_active"):
        nums = [v for v in (a.get(f), b.get(f))
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if nums:
            out[f] = max(nums)
    return _commutative_key_order(a, b, out)


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
# ( / rb-2849 — BRD P0, the cc-04 fleet wedge.) Hypothesis records
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
      - key order: canonicalized when the sides' key sequences diverged
        (_commutative_key_order, g-115-2355 — the equal-rank canon tiebreak
        otherwise picks the FIRST ARG between value-identical order-divergent
        copies, which a 3-participant merge-order asymmetry can manufacture
        from two concurrent distinct-field adds).
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
    return _commutative_key_order(a, b, out)


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
        everything else: content-tiebreak base + side-only field union;
        key order canonicalized on diverged key sequences
        (_commutative_key_order, g-115-2355 full-tie corner). The wholesale
        early returns need no routing — they emit ONE side's dict chosen by
        CONTENT (type / canon), identical from either arg order."""
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
    return _commutative_key_order(a, b, out)


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


def _parse_jsonl_lossy(data: bytes) -> Tuple[List[dict], List[str]]:
    """Torn-line-tolerant parse for APPEND-ONLY logs ONLY ().

    A torn half-line (a truncated append — the write was interrupted mid-line)
    is unrecoverable half-data for an append-only log: the complete record
    either synced to the other side before the tear (the union keeps it) or
    never fully landed anywhere (nothing complete exists to preserve). The
    strict ``_parse_jsonl`` raises on such a line, which wedges the union lane
    forever (``_merge_reconcile_put`` wraps the handler raise in ConflictError
    and every sweep retries into the same torn bytes — the cc-04 franken-local
    wedge, healed manually under g-115-2297 by pre-filtering parseable lines).
    This variant skips unparseable lines and RETURNS them so the caller can
    surface the drop loudly.

    Scope guard: id-keyed / field-merge handlers MUST keep the strict parse —
    there a parse failure can mean real corruption of an editable record and
    the safe response is the freeze, not a silent skip. Only
    ``merge_append_only_jsonl`` may call this.
    """
    records: List[dict] = []
    torn: List[str] = []
    # errors="replace": a tear can cut mid-UTF-8-sequence; strict decode would
    # raise before line-splitting ever happens. Valid lines decode identically.
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            torn.append(line)
    return records, torn


def _warn_torn_lines(torn: List[str], side: str) -> None:
    """Loud, bounded stderr surface for dropped torn lines ().

    The full torn content (capped) is echoed so the daemon log / sweep output
    preserves the forensic bytes even after the merged write-back replaces the
    on-disk original — the in-band half of preserve-before-drop (the on-disk
    half is the caller-side .history snapshot in owncloud_sync._try_merge_put).
    """
    shown = torn[:5]
    body = " | ".join(t[:200] for t in shown)
    more = f" (+{len(torn) - len(shown)} more)" if len(torn) > len(shown) else ""
    print(f"[coordination-merge] WARN: dropped {len(torn)} torn line(s) from "
          f"{side} side of append-log union{more}: {body[:2000]}",
          file=sys.stderr)


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

    Torn-line tolerance (g-115-2325): parses BOTH sides via _parse_jsonl_lossy —
    symmetric on the two args, so commutativity (guard-907) is preserved: the
    dropped set is a function of content, never of the local-vs-remote role. A
    torn half-line from a truncated append no longer wedges the lane (see
    _parse_jsonl_lossy docstring); it is dropped LOUDLY (stderr carries the
    bytes) and the sync-side caller snapshots the pre-merge local to .history
    first. Append-only ONLY — id-keyed/field-merge handlers keep strict parsing.
    """
    local_recs, local_torn = _parse_jsonl_lossy(local)
    remote_recs, remote_torn = _parse_jsonl_lossy(remote)
    if local_torn:
        _warn_torn_lines(local_torn, "first")
    if remote_torn:
        _warn_torn_lines(remote_torn, "second")
    combined = local_recs + remote_recs
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
        # One-sided modules self-normalize via _merge_module(r, r) (MAX is
        # idempotent; derived success_rate recomputed) so the merged doc is
        # canonical-form regardless of which path a module took — otherwise
        # merge(m, m) rewrites bytes once more when a formerly-one-sided
        # module meets the both-sided normalizer ( idempotency).
        if ra is None:
            merged[mid] = _merge_module(rb, rb) if isinstance(rb, dict) else rb
        elif rb is None:
            merged[mid] = _merge_module(ra, ra) if isinstance(ra, dict) else ra
        elif isinstance(ra, dict) and isinstance(rb, dict):
            merged[mid] = _merge_module(ra, rb)
        else:
            merged[mid] = ra if _canon(ra) >= _canon(rb) else rb
    # Base = content-larger doc so any FUTURE opaque top-level key rides along
    # deterministically (today 'modules' is the only top-level key).
    out = dict(a) if _canon(a) >= _canon(b) else dict(b)
    out["modules"] = merged
    # Key order side-independent on divergence (; see merge_team_state).
    return _dump_yaml_default(_commutative_key_order(a, b, out))


# --- forged-skills.yaml : keyed dict union (skills registry) ----------------
#  — CURE for the stale-base full-file clobber lane (
# check-production-health row +  probe-governed-store /
# reconcile-fleet-fork rows, 07-12/07-13 window). The registry basename had NO
# handler, so concurrent writers fell into the diverged/LWW lanes and a
# stale-base writer deleted peer rows.


def _merge_forged_skill(a: dict, b: dict) -> dict:
    """Reconcile two records of the SAME skill name. A skill record is
    authored as a unit (one forge event writes all fields), so the winner is
    WHOLE-RECORD — field-blending would stitch two authored versions'
    triggers/companion_scripts. Winner: newer ``forged_date`` (ISO string,
    lexicographic; missing sorts oldest) -> more fields (richer side, the
    _merge_module convention) -> ``_canon`` content tiebreak. Symmetric in
    (a, b) -> both machines converge; merge(r, r) == r (idempotent)."""
    # str() coercion: an UNQUOTED YAML date parses as datetime.date (the
    # merge_tree str-vs-date bug class); str(date) is ISO so it compares
    # lexicographically with the quoted-string form.
    da = str(a.get("forged_date")) if a.get("forged_date") is not None else ""
    db = str(b.get("forged_date")) if b.get("forged_date") is not None else ""
    if da != db:
        return a if da > db else b
    if len(a) != len(b):
        return a if len(a) > len(b) else b
    return a if _canon(a) >= _canon(b) else b


def merge_forged_skills(local: bytes, remote: bytes) -> bytes:
    """Keyed union of the forged-skills registry: {skills: {name: record}}.
    Rows are append-mostly (a forge event ADDS a row; retirement is an
    explicit status field, never deletion — /forge-skill has no delete path),
    so key-set UNION is safe: a row present on EITHER side survives, turning
    the concurrent-writer clobber into a union. Same-name divergence resolves
    whole-record via _merge_forged_skill. Non-``skills`` top-level keys (none
    today) ride the content-larger base so a future schema addition cannot
    wedge the handler. Skills emitted SORTED by name — bytes identical
    regardless of arg order (the fenced-PUT commutativity property; same
    discipline as merge_module_health, this handler's template)."""
    a = yaml.safe_load(local.decode("utf-8")) or {}
    b = yaml.safe_load(remote.decode("utf-8")) or {}
    if not isinstance(a, dict) or not isinstance(b, dict):
        return local if _canon(a) >= _canon(b) else remote
    am = a.get("skills") if isinstance(a.get("skills"), dict) else {}
    bm = b.get("skills") if isinstance(b.get("skills"), dict) else {}
    merged: Dict[str, object] = {}
    for name in sorted(set(am) | set(bm)):
        ra, rb = am.get(name), bm.get(name)
        if ra is None:
            merged[name] = rb
        elif rb is None:
            merged[name] = ra
        elif isinstance(ra, dict) and isinstance(rb, dict):
            merged[name] = _merge_forged_skill(ra, rb)
        else:
            merged[name] = ra if _canon(ra) >= _canon(rb) else rb
    out = dict(a) if _canon(a) >= _canon(b) else dict(b)
    out["skills"] = merged
    return _dump_yaml_default(_commutative_key_order(a, b, out))


# --- skill-relations.yaml : list-union relation graph () ----------
# The sibling shared registry to forged-skills.yaml — same clobber lane
# (no handler -> diverged/LWW -> stale-base full-file write deletes peer
# entries). Writer inventory (rb-245, verified 2026-07-17): cmd_add appends
# with a (source,target,type) duplicate check and NEVER deletes;
# skill-coinvocation-discovery --apply is an idempotent RMW appender;
# cmd_co_invoke APPENDS then TAIL-CAPS the log at co_invocation_log_cap —
# the one legitimate delete path, so the merged log must re-apply the same
# cap or a union would resurrect capped-out entries and grow unboundedly.


def _co_invocation_log_cap() -> int:
    """co_invocation_log_cap from core/config/skill-relations.yaml — the same
    SSOT cmd_co_invoke enforces (skill-relations.py:211). Fail-open 200."""
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "config", "skill-relations.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return int((cfg.get("config") or {}).get("co_invocation_log_cap", 200))
    except Exception:
        return 200


def _merge_skill_relation(a: dict, b: dict) -> dict:
    """Reconcile two records of the SAME (source, target, type) edge. No date
    field exists on relation records, so: more fields (richer side — e.g.
    confidence+evidence vs bare edge) -> _canon content tiebreak. Symmetric
    and idempotent (mirrors _merge_forged_skill minus the date leg)."""
    if len(a) != len(b):
        return a if len(a) > len(b) else b
    return a if _canon(a) >= _canon(b) else b


def merge_skill_relations(local: bytes, remote: bytes) -> bytes:
    """Union merge for world/skill-relations.yaml (3 top-level keys):

    - forged_relations: LIST unioned by the (source, target, type) identity
      cmd_add's duplicate check defines; same-edge divergence resolves via
      _merge_skill_relation. Emitted SORTED by identity — bytes identical
      regardless of arg order.
    - co_invocation_log: append-mostly entries unioned by FULL-record content,
      sorted by (date, content) ascending, then TAIL-CAPPED at the writers'
      own co_invocation_log_cap so the merge cannot resurrect capped-out
      entries or outgrow the cap. Unquoted YAML dates (datetime objects)
      are str()-coerced for the sort key (the merge_tree str-vs-date class).
    - last_updated: max non-null (write_yaml never stamps it today — the
      live value is null — but a future stamper must not be rolled back).

    Keys are only emitted when present on at least one side, so a
    non-registry doc that happens to share the basename (core/config/
    skill-relations.yaml — never synced, but defensively) passes through
    without invented keys."""
    a = yaml.safe_load(local.decode("utf-8")) or {}
    b = yaml.safe_load(remote.decode("utf-8")) or {}
    if not isinstance(a, dict) or not isinstance(b, dict):
        return local if _canon(a) >= _canon(b) else remote

    out = dict(a) if _canon(a) >= _canon(b) else dict(b)

    def _list_of(doc, key):
        v = doc.get(key)
        return v if isinstance(v, list) else []

    if "forged_relations" in a or "forged_relations" in b:
        by_id: Dict[tuple, dict] = {}
        raw_rels: List[object] = []
        for r in _list_of(a, "forged_relations") + _list_of(b, "forged_relations"):
            if not isinstance(r, dict):
                raw_rels.append(r)
                continue
            k = (str(r.get("source")), str(r.get("target")), str(r.get("type")))
            if k in by_id:
                by_id[k] = _merge_skill_relation(by_id[k], r)
            else:
                by_id[k] = r
        merged_rels = [by_id[k] for k in sorted(by_id)]
        # Non-dict cruft entries survive dedup'd by content, sorted last —
        # never silently dropped (the union promise), never crash the sort.
        merged_rels += sorted({_canon(r): r for r in raw_rels}.values(),
                              key=_canon)
        out["forged_relations"] = merged_rels

    if "co_invocation_log" in a or "co_invocation_log" in b:
        seen: Dict[str, object] = {}
        for e in _list_of(a, "co_invocation_log") + _list_of(b, "co_invocation_log"):
            seen.setdefault(_canon(e), e)

        def _log_key(e):
            d = e.get("date") if isinstance(e, dict) else None
            # str(datetime) uses a space where ISO strings use 'T' — without
            # normalization a datetime-typed entry sorts BEFORE a string-typed
            # entry of the same second (space < 'T'), skewing cap eviction.
            return (str(d).replace(" ", "T") if d is not None else "",
                    _canon(e))

        merged_log = sorted(seen.values(), key=_log_key)
        cap = _co_invocation_log_cap()
        if len(merged_log) > cap:
            merged_log = merged_log[-cap:]
        out["co_invocation_log"] = merged_log

    lu_a, lu_b = a.get("last_updated"), b.get("last_updated")
    if lu_a is not None or lu_b is not None:
        # Winner by T-normalized comparison (str(datetime) space vs ISO 'T' —
        # fresh-eyes P1: a chronologically NEWER unquoted datetime otherwise
        # loses to an older quoted string). _canon second key: a normalized
        # TIE (same instant, datetime vs string type) must not resolve by arg
        # order or merge(a,b) != merge(b,a) at the byte level.
        candidates = [v for v in (lu_a, lu_b) if v is not None]
        out["last_updated"] = max(
            candidates, key=lambda v: (str(v).replace(" ", "T"), _canon(v)))
    return _dump_yaml_default(_commutative_key_order(a, b, out))


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
    # Key order side-independent on divergence (; see merge_team_state).
    out = _commutative_key_order(a, b, out)
    return (json.dumps(out, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def merge_pipeline_meta(local: bytes, remote: bytes) -> bytes:
    """Field-level reconcile of two pipeline-meta.json documents ( —
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
            elif (isinstance(va, (int, float)) and not isinstance(va, bool)
                    and isinstance(vb, (int, float)) and not isinstance(vb, bool)):
                #  (guard-1153 sweep): confirmed_all_time /
                # corrected_all_time are grow-only counters — numeric MAX.
                # The previous _canon content-compare ordered lexicographically
                # ("10" < "9"), so a counter could go BACKWARD on a same-key
                # clash between two boxes that both batch-processed micros.
                merged_m[k] = max(va, vb)
            else:
                # Non-numeric values (last_session_stats snapshot dict, notes):
                # content-larger stays — a whole-snapshot display blob where
                # either side is a valid session record; deterministic beats
                # date-parsing a cosmetic field.
                merged_m[k] = va if _canon(va) >= _canon(vb) else vb
        # accuracy_all_time is DERIVED from the two counters — recompute after
        # the MAX-merge so one side's ratio is never paired with the other
        # side's counters (same discipline as _merge_module.success_rate and
        # _merge_spark_record.yield_rate). round(3) matches the batch-micro
        # writer's precision.
        conf = merged_m.get("confirmed_all_time")
        corr = merged_m.get("corrected_all_time")
        if (isinstance(conf, (int, float)) and not isinstance(conf, bool)
                and isinstance(corr, (int, float)) and not isinstance(corr, bool)
                and (conf + corr) > 0 and "accuracy_all_time" in merged_m):
            merged_m["accuracy_all_time"] = round(conf / (conf + corr), 3)
        out["micro_hypothesis_stats"] = merged_m
    # Key order side-independent on divergence (; see merge_team_state).
    out = _commutative_key_order(a, b, out)
    return (json.dumps(out, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


# --- _tree.yaml per-node field reconcile (, carved from ) ---
# _tree.yaml is the 4th both-diverged freeze shape: a ~966KB / ~1140-node tree
# where each node dict carries MIXED-semantics fields. This sub-goal (-a)
# supplies the FIELD-CLASSIFICATION map + the three NON-STRUCTURAL merge classes.
# The STRUCTURAL reconcile (children/parent/depth/child_count/node_type) is
# -b, and the top-level merge_tree(bytes->bytes) handler + tree_growth_log
# order-preserving union + _HANDLERS registration is -c. C LANDED
# (cccf0345 + 2292ac9b): merge_tree is LIVE, registered as "_tree.yaml" in
# _HANDLERS (see the registration map below), verified reconciling the frozen
# ~1140-node both-diverged tree post-restart with zero node loss
# (2026-07-09_tree-yaml-freeze-restart-reconciles-without-clobber, CONFIRMED).
#
# Field classes (every per-node field is classified -- see _classify_tree_field):
#   MAX          grow-only utilization counters -> numeric max (never lose a count;
#                identical convention to _merge_counters / _merge_module).
#   NEWER        monotonic timestamps -> strictly-newer ISO wins (only advance).
#   PROGRESSION  confidence / capability_level -> LWW by the node's last_updated
#                (the later edit reflects the node's CURRENT calibration/maturity,
#                so a genuine later downgrade -- e.g. a contradiction lowering
#                confidence -- is PRESERVED, not clobbered), with a NEVER-REGRESS
#                tiebreak ONLY on an equal/both-missing last_updated: the
#                more-progressed value wins (higher confidence; more-mature
#                capability_level per the EXPLORE<CALIBRATE<EXPLOIT<MASTER maturity
#                axis in core/scripts/tree.py). later-wins is the primary rule;
#                never-regress is the ambiguity-only refinement.
#   STRUCTURAL   children/parent/depth/child_count/node_type -> DEFERRED to
#                -b; here they ride the LWW base unchanged (a naive union
#                corrupts parent/child symmetry, so B reconciles them adversarially).
#   BASE         everything else (summary, file, growth_state, derived counts like
#                article_count/utility_ratio/accuracy, opaque/LWW fields) -> rides
#                the newer-last_updated base (self-correcting: the next tree-maintain
#                recomputes derived fields; same precedent as merge_pipeline_meta's
#                derived LWW fields).
# Every rule is a symmetric function of (a, b) -> both machines converge (the
# module-wide commutativity invariant). Verified by the -a unit tests.

# Grow-only utilization counters -> numeric MAX.
_TREE_MAX_FIELDS = ("retrieval_count", "times_helpful", "times_noise",
                    "times_inferred_helpful", "sample_size")
# Monotonic timestamps -> strictly-newer ISO wins. progression_updated_at
# () is the dedicated PROGRESSION-field calibration stamp: writers of
# confidence/capability_level/domain_confidence bump it on edit, and the
# PROGRESSION merge keys on it (see _merge_tree_node) instead of last_updated,
# which  deliberately leaves un-bumped on a field poke. It merges NEWER
# so the winning side's stamp is preserved.
_TREE_NEWER_FIELDS = ("last_retrieved", "last_updated", "last_relevant_at",
                      "progression_updated_at")
# LWW-by-last_updated with never-regress-on-tie.
_TREE_PROGRESSION_FIELDS = ("confidence", "capability_level", "domain_confidence")
# Structural -- reconciled in -b; ride the LWW base here.
_TREE_STRUCTURAL_FIELDS = ("children", "parent", "depth", "child_count", "node_type")
# capability_level maturity axis (core/scripts/tree.py capability-threshold map:
# EXPLORE 0.25 < CALIBRATE 0.50 < EXPLOIT 0.75 < MASTER 1.00). Higher rank = more
# progressed. REFERENCE is orthogonal to the axis (absent -> content tiebreak).
_CAPABILITY_RANK = {"EXPLORE": 0, "CALIBRATE": 1, "EXPLOIT": 2, "MASTER": 3}


def _merge_field_max(va, vb):
    """MAX class: two grow-only counter values -> the larger. A present numeric
    beats a missing/non-numeric side; two non-numerics fall to a content tiebreak.
    Commutative (max and _canon are symmetric)."""
    na = isinstance(va, (int, float)) and not isinstance(va, bool)
    nb = isinstance(vb, (int, float)) and not isinstance(vb, bool)
    if na and nb:
        return max(va, vb)
    if na:
        return va
    if nb:
        return vb
    return va if _canon(va) >= _canon(vb) else vb


def _merge_field_newer(va, vb):
    """NEWER class: two monotonic ISO timestamps -> the strictly-newer (None sorts
    oldest; equal values are identical either way). Commutative -- returns the
    newer value regardless of arg order (via _newer)."""
    return va if (_newer(va, vb) or va == vb) else vb


def _progression_rank(v):
    """Total order for the PROGRESSION never-regress tiebreak. Numbers rank by
    value; capability_level strings rank by the maturity axis; anything else ranks
    by canonical JSON (deterministic + machine-independent). The 3-tuple keeps the
    three kinds separable so a str never numerically-compares against a float."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (2, float(v), "")
    if isinstance(v, str) and v in _CAPABILITY_RANK:
        return (1, float(_CAPABILITY_RANK[v]), "")
    return (0, 0.0, _canon(v))


def _merge_field_progression(va, ta, vb, tb):
    """PROGRESSION class (confidence / capability_level): LWW keyed on the node's
    last_updated (ta / tb) -- the later edit reflects the node's CURRENT
    calibration/maturity, so a genuine later downgrade is kept. On an EQUAL (or
    both-missing) last_updated the winner is ambiguous, so the NEVER-REGRESS
    tiebreak picks the more-progressed value (higher confidence / more-mature
    capability_level), content tiebreak on an equal rank. Commutative: _newer is
    antisymmetric and the rank/_canon tiebreak is symmetric."""
    if _newer(ta, tb):
        return va
    if _newer(tb, ta):
        return vb
    ra, rb = _progression_rank(va), _progression_rank(vb)
    if ra != rb:
        return va if ra > rb else vb
    return va if _canon(va) >= _canon(vb) else vb


def _merge_tree_node(a: dict, b: dict) -> dict:
    """Reconcile two copies of the SAME tree node (same node key) from two
    machines. Base = the newer-last_updated copy (LWW via _order_by_ts) so every
    BASE/derived/opaque field AND the STRUCTURAL fields (deferred to g-001-313-b)
    ride the later edit; then the class fields override:
      MAX          -> _merge_field_max          (larger counter)
      NEWER        -> _merge_field_newer         (strictly-newer ISO)
      PROGRESSION  -> _merge_field_progression   (LWW-by-last_updated, never-regress tie)
    A class field present on only ONE side is kept (an absent field never clobbers
    a present one), and loser-only BASE fields are preserved too (authored fields
    like origin_goal_id / valid_from / domain_class are NOT self-correcting, so a
    loser-only one must not be dropped). Symmetric in (a, b) -> both machines
    converge, INCLUDING the non-dict guard below (proven by the g-001-313-a unit
    tests). NOTE: this returns a merged NODE dict; the byte-exact _tree.yaml
    serialization + tree_growth_log union + dispatcher wiring are g-001-313-c
    (LANDED — reachable via the merge_tree handler registered in _HANDLERS)."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        # Non-dict input (never happens for real tree nodes). The dict side wins;
        # if BOTH are non-dicts fall to the module's content tiebreak so the result
        # is byte-identical regardless of (a, b) order. (-a fresh-eyes fix
        # — the prior `return b if isinstance(b, dict) else a` broke commutativity:
        # merge(3, 5)->3 but merge(5, 3)->5.)
        if isinstance(a, dict):
            return a
        if isinstance(b, dict):
            return b
        return a if _canon(a) >= _canon(b) else b
    base, _lose = _order_by_ts(a, b, "last_updated")
    out = dict(base)
    ta, tb = a.get("last_updated"), b.get("last_updated")
    for f in _TREE_MAX_FIELDS:
        if f in a and f in b:
            out[f] = _merge_field_max(a[f], b[f])
        elif f in a:
            out[f] = a[f]
        elif f in b:
            out[f] = b[f]
    for f in _TREE_NEWER_FIELDS:
        if f in a and f in b:
            out[f] = _merge_field_newer(a[f], b[f])
        elif f in a:
            out[f] = a[f]
        elif f in b:
            out[f] = b[f]
    # PROGRESSION merges key on the DEDICATED progression_updated_at stamp
    # (bumped by writers on any confidence/capability_level/domain_confidence
    # edit), falling back to last_updated for un-migrated nodes. 
    # deliberately does NOT bump last_updated on a field poke (it tracks .md
    # article freshness, SSOT ), so keying PROGRESSION on last_updated
    # made every confidence merge see equal stamps and hit the never-regress
    # tiebreak, silently reverting data-derived DOWNGRADES to the stale-higher
    # value (; rb-3823, guard-1170). The fallback keeps behavior
    # byte-identical for nodes not yet re-written with the stamp (backfill-safe).
    # Commutative: pa/pb are each side's own value passed in (a, b) order and
    # _merge_field_progression is symmetric under (va, pa)<->(vb, pb) swap.
    pa = a.get("progression_updated_at", ta)
    pb = b.get("progression_updated_at", tb)
    for f in _TREE_PROGRESSION_FIELDS:
        if f in a and f in b:
            out[f] = _merge_field_progression(a[f], pa, b[f], pb)
        elif f in a:
            out[f] = a[f]
        elif f in b:
            out[f] = b[f]
    # Preserve loser-only fields the base lacks (-a fresh-eyes fix):
    # authored BASE fields (origin_goal_id / valid_from / domain_class) are NOT
    # self-correcting, so a loser-only one must not be silently dropped. `_lose`
    # is content-fixed (symmetric via _order_by_ts) and every class field is
    # already in `out` from the loops above, so this stays commutative and only
    # adds base-absent keys. Mirrors the id-keyed handlers' loser-key union.
    for k, v in _lose.items():
        if k not in out:
            out[k] = v
    return out


def _classify_tree_field(field: str) -> str:
    """The FIELD-CLASSIFICATION map as a TOTAL function: every per-node field name
    -> its merge class ("MAX" | "NEWER" | "PROGRESSION" | "STRUCTURAL" | "BASE").
    Named classes get a dedicated rule in _merge_tree_node; STRUCTURAL is deferred
    to g-001-313-b; BASE rides the newer-last_updated LWW base. Total by
    construction -- an unrecognized (future) field defaults to BASE, the safe
    self-correcting class -- so the map "covers all per-node fields" by design."""
    if field in _TREE_MAX_FIELDS:
        return "MAX"
    if field in _TREE_NEWER_FIELDS:
        return "NEWER"
    if field in _TREE_PROGRESSION_FIELDS:
        return "PROGRESSION"
    if field in _TREE_STRUCTURAL_FIELDS:
        return "STRUCTURAL"
    return "BASE"


# --- _tree.yaml node-MAP structural merge (-b) ----------------------
# -a reconciles a SINGLE node's non-structural fields. This sub-goal
# (-b) reconciles the whole `nodes:` map's STRUCTURE -- children /
# parent / depth / child_count / node_type -- where a naive per-node children
# UNION corrupts parent/child consistency. Canonical failure: machine A moves
# node Z from parent P to parent N (Z.parent=N, Z in N.children, Z NOT in
# P.children); machine B keeps Z under P. A naive children-union leaves Z in BOTH
# N.children and P.children while Z.parent is only one of them -- a symmetry
# violation that, once wired (-c) onto the live ~1140-node tree, corrupts
# it.
#
# The robust reconcile is PARENT-AUTHORITATIVE: `parent` is the single source of
# truth (reconciled LWW-by-last_updated inside _merge_tree_node -- the later
# edit's parent wins), and children / depth / child_count / node_type are DERIVED
# from the reconciled parents AFTER the per-node merge. Deriving children from
# parent GUARANTEES parent/child symmetry AND no orphaned children by
# construction: a node appears in exactly the children list of the parent it
# actually points to, and that list only ever names keys that exist. Every
# derived list is SORTED, so the result is byte-identical regardless of (a, b)
# order (the module-wide commutativity invariant). -c LANDED: the
# bytes->bytes merge_tree handler (parse YAML -> merge nodes map +
# tree_growth_log union -> serialize, with CRLF handling) wraps these helpers
# and is registered in _HANDLERS ("_tree.yaml": merge_tree).


def _rebuild_tree_structure(merged: Dict[str, dict]) -> None:
    """IN-PLACE structural rebuild of a merged node map: derive children /
    child_count / node_type / depth from each node's reconciled `parent`, so the
    structure is internally consistent (parent/child symmetry + no orphaned
    children) no matter how the two sides diverged. Deterministic (children
    SORTED); depends only on `merged`, so the whole map merge stays commutative.
    Mutates only the freshly-merged node dicts (never the caller's inputs)."""
    # 1. children: derive from parent. children[P] = the keys whose parent == P.
    children_of: Dict[str, list] = {k: [] for k in merged}
    for k in merged:
        p = merged[k].get("parent")
        if p is not None and p in children_of:
            children_of[p].append(k)
    for k, node in merged.items():
        kids = sorted(children_of[k])              # SORTED -> byte-deterministic
        node["children"] = kids
        node["child_count"] = len(kids)
        # node_type is leaf/interior, fully derived from whether the node has
        # children (verified: _tree.yaml node_type is only ever leaf|interior).
        node["node_type"] = "interior" if kids else "leaf"
    # 2. depth: recompute as (parent depth + 1); a root (no parent, or a parent
    # outside the merged map) keeps its merged depth. Memoized + cycle-guarded so
    # a pathological parent cycle can never infinite-loop. A parent-move therefore
    # re-derives the moved subtree's depth to match its NEW parent.
    memo: Dict[str, int] = {}

    def _depth(key: str, visiting: frozenset) -> int:
        if key in memo:
            return memo[key]
        node = merged.get(key) or {}
        p = node.get("parent")
        if p is None or p not in merged or key in visiting:
            d = node.get("depth", 0)               # root / dangling parent / cycle
            d = d if isinstance(d, int) and not isinstance(d, bool) else 0
        else:
            d = _depth(p, visiting | {key}) + 1
        memo[key] = d
        return d

    for k in merged:
        merged[k]["depth"] = _depth(k, frozenset())


def _merge_tree_nodes_map(nodes_a: Dict[str, dict],
                          nodes_b: Dict[str, dict]) -> Dict[str, dict]:
    """Reconcile two `nodes:` maps ({node_key: node_dict}) from two machines.
    Node keys are UNIONED (a node on either side survives -> node count is
    preserved); a key on BOTH sides is field-merged via _merge_tree_node
    (g-001-313-a) and then the whole map's STRUCTURE is rebuilt from the
    reconciled parents (g-001-313-b). Emitted key order is SORTED so the bytes
    are identical regardless of (a, b) order (commutativity invariant). Returns
    a NEW map of NEW node dicts (inputs untouched); the bytes<->bytes merge_tree
    wrapper + tree_growth_log union + _HANDLERS registration are g-001-313-c."""
    if not isinstance(nodes_a, dict) or not isinstance(nodes_b, dict):
        # Degenerate guard (never happens for a real `nodes:` map) — content
        # tiebreak so the result is identical regardless of (a, b) order.
        if isinstance(nodes_a, dict):
            return nodes_a
        if isinstance(nodes_b, dict):
            return nodes_b
        return nodes_a if _canon(nodes_a) >= _canon(nodes_b) else nodes_b
    merged: Dict[str, dict] = {}
    for key in sorted(set(nodes_a) | set(nodes_b)):
        na, nb = nodes_a.get(key), nodes_b.get(key)
        if na is None:
            merged[key] = dict(nb) if isinstance(nb, dict) else nb
        elif nb is None:
            merged[key] = dict(na) if isinstance(na, dict) else na
        else:
            merged[key] = _merge_tree_node(na, nb)
    # Parent-authoritative structural rebuild -> parent/child symmetry + no
    # orphaned children by construction (the whole reason -b is split from -a).
    _rebuild_tree_structure(merged)
    return merged


def _tree_structural_integrity(merged: Dict[str, dict]) -> List[str]:
    """Return the list of structural-integrity VIOLATIONS in a merged node map
    (empty == clean). Checks the invariants g-001-313-b guarantees: (1) no
    orphaned children (every child key exists as a node), (2) parent/child
    symmetry both directions (child in P.children <=> child.parent == P),
    (3) no dangling parent references (every non-null parent key exists as a
    node — added g-001-324 after fresh-eyes caught the `p in keys` false
    negative). Node count is the caller's check (compare len(merged) against
    the input key union). Drives the adversarial test pass; also usable as a
    post-merge assertion. NOTE: parent CYCLES (self-parent, a<->b) are
    semantic validity, out of this checker's structural scope — the depth
    walk in _rebuild_tree_structure cycle-guards them, and a dedicated
    acyclicity check is a separate future concern."""
    issues: List[str] = []
    keys = set(merged)
    for k in sorted(merged):
        node = merged[k]
        for c in node.get("children", []) or []:
            if c not in keys:
                issues.append(f"orphan: {k}.children references missing node {c}")
            elif merged[c].get("parent") != k:
                issues.append(
                    f"asymmetry: {k}.children has {c} but {c}.parent="
                    f"{merged[c].get('parent')!r}")
        p = node.get("parent")
        if p is not None:
            if p not in keys:
                # Dangling parent: the node references a parent slug that is
                # absent from the merged map (e.g. the parent was removed on
                # one machine before the merge). The node is a parentless
                # orphan with a broken reference — no existing node lists it
                # as a child, so the symmetry check below would never see it.
                # The old `p in keys` guard silently passed this as clean
                # (, fresh-eyes review of -b).
                issues.append(f"dangling: {k}.parent={p!r} references missing node")
            elif k not in (merged[p].get("children") or []):
                issues.append(f"asymmetry: {k}.parent={p!r} but {p}.children lacks {k}")
    return issues


# --- _tree.yaml TOP-LEVEL assembly + dispatcher handler (-c) ---------
# The a/b helpers above reconcile the `nodes:` map (per-node field merge +
# parent-authoritative structural rebuild). This sub-goal (C) assembles the full
# document: it merges the SEVEN non-node top-level keys and wires merge_tree into
# _HANDLERS so _tree.yaml is finally a registered both-diverged freeze shape.
# Every top-level rule is a symmetric function of (a, b) -> commutative, same as
# the node reconcile. Symmetry of the RULES is necessary but not sufficient for
# byte-identity: a value can survive a _canon-tie dedup carrying the LOSER's
# dict-key insertion order (or a scalar's int-vs-float / str-vs-date form), which
# sort_keys=False would then serialize arg-order-dependently. merge_tree therefore
# applies a terminal _canonicalize_for_merge pass so the output bytes are a pure
# function of CONTENT -> merge_tree(a, b) is byte-identical to merge_tree(b, a).


def _dump_tree_yaml(data) -> bytes:
    """Byte-exact serializer for _tree.yaml: matches tree.write_tree
    (core/scripts/tree.py) -- yaml.dump with default_flow_style=None (NOT False,
    unlike module-health -- None renders all-scalar leaf lists like `children` in
    flow style and nested structures in block), sort_keys=False (preserve the
    winner's key order), allow_unicode=True, width=200. yaml.dump emits LF on both
    OSes, so a CRLF-written (Windows) input converges to LF output either arg order
    (the CRLF tolerance is automatic: PyYAML normalizes CRLF->LF on PARSE)."""
    return yaml.dump(data, default_flow_style=None, sort_keys=False,
                     allow_unicode=True, width=200).encode("utf-8")


def _canonicalize_for_merge(obj):
    """Deep-canonicalize a merged _tree.yaml value so the serialized bytes are a
    pure function of CONTENT, independent of argument order (guard-907). Three
    normalizations, applied recursively BEFORE _dump_tree_yaml:
      1. dict keys SORTED — closes the sort_keys=False key-order commutativity gap:
         a value whose keys differ only in insertion order serialized differently
         depending on which arg's object survived a _canon-tie dedup (Defect 1 —
         top-level, node-level, entity_index values, cross_references list-dicts,
         tree_growth_log entries, maintenance nested dicts all shared this).
      2. integral float -> int (10.0 -> 10) — a count-like field carries the same
         VALUE but different bytes as int vs float (Defect 2). A real tree.write_tree
         file only ever has int counts (len(nodes)), so this is a no-op on canonical
         input; it defends against a non-standard writer / hand-edit. Non-integral
         floats (0.72 confidence) and bool (not a float) pass through untouched;
         nan/inf raise on int() and are left as-is.
      3. date/datetime -> isoformat str (Defect 3) — an UNQUOTED YAML date parses to
         datetime.date while the quoted form parses to str; both stringify to the
         same ISO text. tree.write_tree always quotes (str), so also a no-op on
         canonical input. `hasattr(.,'isoformat')` uniquely tags date/datetime (str
         lacks it, so an ISO string stays a str and matches the stringified date).
    Applied ONCE at merge_tree's return, this makes the WHOLE output tree canonical,
    so node-level dicts + scalars (b's helpers) are normalized without re-touching
    them. List ORDER is preserved (only dict keys sort) — tree_growth_log stays
    chronological. Idempotent: canonicalize(canonical) == canonical."""
    if isinstance(obj, dict):
        return {k: _canonicalize_for_merge(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_canonicalize_for_merge(v) for v in obj]
    if isinstance(obj, float):
        try:
            if obj == int(obj):
                return int(obj)
        except (ValueError, OverflowError):
            pass  # nan / inf -> leave as-is
        return obj
    if hasattr(obj, "isoformat"):  # datetime.date / datetime.datetime
        return obj.isoformat()
    return obj


def _tree_growth_log_union(a_log, b_log):
    """Order-preserving union of two tree_growth_log lists. Entries are append-only
    chronological events {op, node, children, date, reason}; identity is
    (op, node, date). A naive _canon-sort of the whole entry would scramble history,
    so the union is emitted in CHRONOLOGICAL (date, op, node) order -- a total,
    arg-order-independent order (commutative) that PRESERVES the log's meaning. On an
    identity clash (same op/node/date, differing children/reason) the content-larger
    canonical entry wins (deterministic). Non-dict entries key by their canonical
    form so they dedup with themselves and sort deterministically."""
    a_log = a_log if isinstance(a_log, list) else []
    b_log = b_log if isinstance(b_log, list) else []
    by_id: Dict[tuple, object] = {}
    for entry in list(a_log) + list(b_log):
        if isinstance(entry, dict):
            key = ("d", str(entry.get("op", "")), str(entry.get("node", "")),
                   str(entry.get("date", "")))
        else:
            key = ("x", _canon(entry), "", "")
        prev = by_id.get(key)
        if prev is None or _canon(entry) > _canon(prev):
            by_id[key] = entry

    def _sort_key(entry):
        if isinstance(entry, dict):
            return (str(entry.get("date", "")), str(entry.get("op", "")),
                    str(entry.get("node", "")), _canon(entry))
        return ("", "", "", _canon(entry))

    return sorted(by_id.values(), key=_sort_key)


def _sorted_list_union(la, lb):
    """Set-union of two lists, deduped + sorted by canonical form (deterministic,
    arg-order-independent). Handles unhashable elements (dicts) via _canon. Used for
    unmapped_categories / cross_references (both currently empty but structurally
    lists that only ever grow)."""
    seen: Dict[str, object] = {}
    for item in list(la) + list(lb):
        seen[_canon(item)] = item
    return [seen[k] for k in sorted(seen)]


def _dict_key_union(ea, eb):
    """Per-key union of two dicts (content-larger canonical wins a same-key clash;
    a key on ONE side is kept). Keys emitted SORTED for byte-stability. Used for
    entity_index."""
    merged: Dict[str, object] = {}
    for k in sorted(set(ea) | set(eb)):
        va, vb = ea.get(k), eb.get(k)
        if va is None:
            merged[k] = vb
        elif vb is None:
            merged[k] = va
        else:
            merged[k] = va if _canon(va) >= _canon(vb) else vb
    return merged


def _merge_tree_maintenance(ma, mb):
    """Reconcile the top-level maintenance cadence block. Per-key: numeric -> MAX
    (grow-only cadence counters), else content-larger canonical. For the fixed-width
    ISO timestamps the block carries, content-larger canonical IS the strictly-newer
    value (lexical compare == chronological, per _newer's own contract), so no
    separate ISO detection is needed. Commutative + deterministic; a key on ONE side
    is kept. Keys emitted SORTED for byte-stability."""
    if not isinstance(ma, dict):
        ma = {}
    if not isinstance(mb, dict):
        mb = {}
    merged: Dict[str, object] = {}
    for k in sorted(set(ma) | set(mb)):
        va, vb = ma.get(k), mb.get(k)
        if va is None:
            merged[k] = vb
        elif vb is None:
            merged[k] = va
        elif (isinstance(va, (int, float)) and not isinstance(va, bool)
              and isinstance(vb, (int, float)) and not isinstance(vb, bool)):
            merged[k] = max(va, vb)
        else:
            merged[k] = va if _canon(va) >= _canon(vb) else vb
    return merged


def merge_tree(local: bytes, remote: bytes) -> bytes:
    """Field-level reconcile of two _tree.yaml documents — the 4th and highest-
    blast-radius both-diverged freeze shape (g-001-313; ~966KB / ~1140-node tree).
    Base = the newer top-level `last_updated` snapshot (LWW so any opaque/future
    top-level key rides along), then each field below overrides with its natural
    merge:
      - nodes               : _merge_tree_nodes_map (a per-node field merge + b
                              parent-authoritative structural rebuild)
      - tree_growth_log     : order-preserving union by (op, node, date) identity,
                              emitted in chronological (date, op, node) order
      - last_updated        : strictly-newer wins (monotonic index stamp)
      - total_entities      : numeric MAX (grow-only entity count)
      - unmapped_categories : sorted set-union
      - cross_references    : sorted set-union
      - entity_index        : per-key union (content-larger on a same-key clash)
      - maintenance         : per-key cadence merge (MAX numeric / newer ISO)
    CRLF tolerance is automatic (PyYAML normalizes CRLF->LF on parse; yaml.dump
    emits LF), so a Windows-written CRLF file converges either arg order. Output is
    CANONICAL — _canonicalize_for_merge deep-sorts dict keys, folds integral floats
    to int, and stringifies dates before _dump_tree_yaml — so the bytes are a pure
    function of CONTENT. This is a deliberate divergence from tree.write_tree's
    insertion-order format, REQUIRED for commutativity per guard-907 (a value
    differing only in key order or scalar type must serialize identically regardless
    of arg order). YAML key order is semantic-free: safe_load round-trips the
    canonical form identically and the next tree.write_tree preserves it. Commutative:
    every rule is a symmetric function of (a, b) — merge_tree(a, b) == merge_tree(b, a)
    byte-for-byte."""
    a = yaml.safe_load(local.decode("utf-8")) or {}
    b = yaml.safe_load(remote.decode("utf-8")) or {}
    if not isinstance(a, dict) or not isinstance(b, dict):
        # Degenerate (a valid _tree.yaml is always a dict). Serialize the
        # content-chosen value through the canonical path rather than returning
        # raw input bytes, so two byte-differing-but-content-equal non-dicts
        # still converge either arg order (Defect 4).
        chosen = a if _canon(a) >= _canon(b) else b
        return _dump_tree_yaml(_canonicalize_for_merge(chosen))
    win, _lose = _order_by_ts(a, b, "last_updated")
    out = dict(win)  # LWW base — winner's key order + opaque top-level keys ride along
    # nodes: a/b per-node + structural reconcile
    na = a.get("nodes") if isinstance(a.get("nodes"), dict) else {}
    nb = b.get("nodes") if isinstance(b.get("nodes"), dict) else {}
    out["nodes"] = _merge_tree_nodes_map(na, nb)
    # tree_growth_log: order-preserving chronological union
    out["tree_growth_log"] = _tree_growth_log_union(
        a.get("tree_growth_log"), b.get("tree_growth_log"))
    # last_updated: strictly-newer (win already holds it, but state it explicitly)
    lua, lub = a.get("last_updated"), b.get("last_updated")
    if lua is not None or lub is not None:
        out["last_updated"] = lua if (_newer(lua, lub) or lua == lub) else lub
    # total_entities: grow-only MAX
    te = [v for v in (a.get("total_entities"), b.get("total_entities"))
          if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if te:
        out["total_entities"] = max(te)
    # unmapped_categories / cross_references: sorted set-union
    for f in ("unmapped_categories", "cross_references"):
        la = a.get(f) if isinstance(a.get(f), list) else []
        lb = b.get(f) if isinstance(b.get(f), list) else []
        if la or lb:
            out[f] = _sorted_list_union(la, lb)
    # entity_index: per-key union
    ea = a.get("entity_index") if isinstance(a.get("entity_index"), dict) else {}
    eb = b.get("entity_index") if isinstance(b.get("entity_index"), dict) else {}
    if ea or eb:
        out["entity_index"] = _dict_key_union(ea, eb)
    # maintenance: per-key cadence merge (only when either side has one)
    if isinstance(a.get("maintenance"), dict) or isinstance(b.get("maintenance"), dict):
        out["maintenance"] = _merge_tree_maintenance(
            a.get("maintenance"), b.get("maintenance"))
    # Terminal canonicalization (guard-907): make the serialized bytes a pure
    # function of CONTENT so merge_tree(a,b) == merge_tree(b,a) byte-for-byte even
    # when a value differs only in dict-key insertion order (survived a _canon-tie
    # dedup) or scalar TYPE. One pass normalizes the whole tree incl. node-level
    # dicts + scalars from a/b's helpers, so those need no per-site sort.
    return _dump_tree_yaml(_canonicalize_for_merge(out))


# byte-deterministic without matching the domain writer's dump style.
def merge_outcome_metrics(local: bytes, remote: bytes) -> bytes:
    try:
        a = yaml.safe_load(local.decode("utf-8")) or {}
        b = yaml.safe_load(remote.decode("utf-8")) or {}
    except Exception:  # noqa: BLE001 — unparseable side -> content tiebreak
        a = b = None
    if isinstance(a, dict) and isinstance(b, dict):
        ta, tb = a.get("updated_at"), b.get("updated_at")
        if _newer(ta, tb):
            return local
        if _newer(tb, ta):
            return remote
    # Equal / missing / unparseable timestamps: content tiebreak on the raw
    # bytes (deterministic, arg-order-independent).
    return local if local >= remote else remote


# --- evolution event streams : revision_id-keyed, status-monotonic ----------
# (, from the 2026-07-18 12-file both-diverged repair .)
# The four evolution streams ({self,skill,rule,script}-evolution.jsonl) were
# proposed as plain line-union, but the rb-245 writer read DISPROVED
# append-only: evolution-record.py APPENDS stubs (status=awaiting_completion),
# then evolution-complete.py rewrite_stream() REWRITES the stub line in place
# (status -> final, reasoning filled) and evolution-stub-expiry.py
# locked_modify_jsonl REWRITES it to expired. A line-union would RESURRECT the
# awaiting_completion version beside its finalized/expired twin, false-firing
# the force_evolution_finalize precheck gate (evolution-stub-pending-check
# counts pending stubs) forever. So: union keyed by revision_id, same-id
# copies merged STATUS-MONOTONICALLY — the more terminal status wins
# (awaiting_completion < awaiting_acks < expired < final; a real completion
# beats the honest-fallback expiry). Deterministic output order by (ts,
# revision_id, canon) -> byte-commutative (guard-907).
_EVO_STATUS_RANK = {"awaiting_completion": 0, "awaiting_acks": 1,
                    "expired": 2, "final": 3}


def _merge_evolution_record(a: dict, b: dict) -> dict:
    ra = _EVO_STATUS_RANK.get(str(a.get("status")), 0)
    rb_ = _EVO_STATUS_RANK.get(str(b.get("status")), 0)
    if ra != rb_:
        return a if ra > rb_ else b
    # Same status: newer ts wins; equal/missing -> content tiebreak.
    w, _l = _order_by_ts(a, b, "ts")
    # Canon tie = identical parsed content, but key ORDER may have diverged —
    # _order_by_ts then returns its first arg, which is side-dependent. Emit
    # sorted-key order on divergence ( pattern) so bytes stay
    # side-independent (guard-907).
    if _canon(a) == _canon(b):
        return _commutative_key_order(a, b, w)
    return w


def merge_evolution_stream(local: bytes, remote: bytes) -> bytes:
    """Commutative merge for the evolution event streams. Records keyed by
    revision_id (defensive fallback: whole-record canonical identity for any
    malformed line without one); same-key copies resolved status-monotonically
    via _merge_evolution_record. Output sorted by (ts, revision_id, canon) —
    a pure function of the merged content set (guard-907)."""
    merged: Dict[object, dict] = {}
    for rec in _parse_jsonl(local) + _parse_jsonl(remote):
        rid = rec.get("revision_id")
        key = ("rev", str(rid)) if rid else ("canon", _canon(rec))
        merged[key] = (_merge_evolution_record(merged[key], rec)
                       if key in merged else rec)
    out = sorted(merged.values(),
                 key=lambda r: (str(r.get("ts") or ""),
                                str(r.get("revision_id") or ""), _canon(r)))
    return _dump_jsonl(out)


# --- infra-health.yaml : per-component newest-activity-wins ------------------
# (.) Writer: infra-health.py via locked_modify_yaml — per-component
# in-place mutation of {last_success, last_failure, consecutive_failures,
# streak_started_at, ...}. A component's fields are internally consistent
# (streak math), so the reconcile unit is the WHOLE component record, won by
# the side with the newest activity timestamp (max of last_success /
# last_failure) — never a field-mix. Component keys union (a component probed
# on only one box survives). Top-level non-component keys (none today,
# defensive) union with per-key canon tiebreak.
def _infra_component_ts(c) -> str:
    if not isinstance(c, dict):
        return ""
    return max(str(c.get("last_success") or ""),
               str(c.get("last_failure") or ""))


def merge_infra_health(local: bytes, remote: bytes) -> bytes:
    try:
        a = yaml.safe_load(local.decode("utf-8")) or {}
        b = yaml.safe_load(remote.decode("utf-8")) or {}
    except Exception:  # noqa: BLE001 — unparseable side -> byte tiebreak
        return local if local >= remote else remote
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return local if local >= remote else remote
    ca = a.get("components") if isinstance(a.get("components"), dict) else {}
    cb = b.get("components") if isinstance(b.get("components"), dict) else {}
    comps: Dict[str, object] = {}
    for name in sorted(set(ca) | set(cb)):
        xa, xb = ca.get(name), cb.get(name)
        if xa is None or xb is None:
            comps[name] = xa if xb is None else xb
            continue
        ta, tb = _infra_component_ts(xa), _infra_component_ts(xb)
        if ta != tb:
            comps[name] = xa if ta > tb else xb
        elif _canon(xa) != _canon(xb):
            comps[name] = xa if _canon(xa) > _canon(xb) else xb
        elif isinstance(xa, dict) and isinstance(xb, dict):
            # Canon tie: identical content, possibly diverged key order —
            # side-independent order via  pattern (guard-907).
            comps[name] = _commutative_key_order(xa, xb, xa)
        else:
            comps[name] = xa
    out: Dict[str, object] = {"components": comps}
    for k in sorted((set(a) | set(b)) - {"components"}):
        va, vb = a.get(k), b.get(k)
        if va is None or vb is None:
            out[k] = va if vb is None else vb
        elif _canon(va) != _canon(vb):
            out[k] = va if _canon(va) > _canon(vb) else vb
        elif isinstance(va, dict) and isinstance(vb, dict):
            out[k] = _commutative_key_order(va, vb, va)
        else:
            out[k] = va
    return _dump_yaml(out)


# --- goal-selection-strategy.yaml : version-winner + applications_log union --
# (.) Versioned meta-strategy: writers are whole-file/field RMW
# (meta-yaml.py set, meta-init.py seed, goal-selector.py applications_log
# append via locked_modify_yaml). Reconcile: the side with the HIGHER version
# wins the strategy body (tie -> newer last_updated -> canon); the
# applications_log telemetry list is entry-UNIONED from both sides (dedup by
# full canonical identity, chronological (ts, canon) order) then RE-CAPPED at
# the writer's own FIFO cap of 200 (goal-selector._APPLICATIONS_LOG_CAP —
# the cap is a legitimate delete path a naive union would resurrect; same
# re-cap pattern as merge_skill_relations' co_invocation_log).
_GSS_APPLICATIONS_LOG_CAP = 200


def merge_goal_selection_strategy(local: bytes, remote: bytes) -> bytes:
    try:
        a = yaml.safe_load(local.decode("utf-8")) or {}
        b = yaml.safe_load(remote.decode("utf-8")) or {}
    except Exception:  # noqa: BLE001
        return local if local >= remote else remote
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return local if local >= remote else remote

    def _ver(d):
        v = d.get("version")
        return v if isinstance(v, (int, float)) else -1

    if _ver(a) != _ver(b):
        w = a if _ver(a) > _ver(b) else b
    elif str(a.get("last_updated") or "") != str(b.get("last_updated") or ""):
        w = a if str(a.get("last_updated") or "") > str(b.get("last_updated") or "") else b
    elif _canon(a) != _canon(b):
        w = a if _canon(a) > _canon(b) else b
    else:
        # Canon tie: identical content, possibly diverged key order — a bare
        # "pick a" is side-dependent and breaks byte-commutativity
        # (guard-907; caught by test_serialization_order_commutative).
        # Sorted-key order on divergence per the  pattern.
        w = _commutative_key_order(a, b, a)
    out = dict(w)
    la = a.get("applications_log") if isinstance(a.get("applications_log"), list) else []
    lb = b.get("applications_log") if isinstance(b.get("applications_log"), list) else []
    seen: Dict[str, dict] = {}
    for e in la + lb:
        seen.setdefault(_canon(e), e)
    log = sorted(seen.values(),
                 key=lambda e: (str((e or {}).get("ts") if isinstance(e, dict) else "") or "",
                                _canon(e)))
    if len(log) > _GSS_APPLICATIONS_LOG_CAP:
        log = log[-_GSS_APPLICATIONS_LOG_CAP:]
    out["applications_log"] = log
    return _dump_yaml(out)


# --- hypothesis-category-bindings.json : key-union on a derived map ----------
# (.) Writer: tree-accuracy-sync.py — full-file rewrite of a FLAT
# {category: node-key} string map DERIVED from tree state (rebuildable; the
# next sync run recomputes authoritative values). Keys union; a same-key value
# conflict has no per-key timestamp to arbitrate, so the canonical-larger
# value wins deterministically — safe because the store is derived and
# self-corrects on the next tree-accuracy-sync pass. sort_keys serialization
# matches the writer's own dump style (indent=2, sort_keys=True).
def merge_hypothesis_category_bindings(local: bytes, remote: bytes) -> bytes:
    try:
        a = json.loads(local.decode("utf-8")) or {}
        b = json.loads(remote.decode("utf-8")) or {}
    except Exception:  # noqa: BLE001
        return local if local >= remote else remote
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return local if local >= remote else remote
    out: Dict[str, object] = {}
    for k in sorted(set(a) | set(b)):
        va, vb = a.get(k), b.get(k)
        if va is None or vb is None:
            out[k] = va if vb is None else vb
        else:
            out[k] = va if _canon(va) >= _canon(vb) else vb
    # No trailing newline — byte-matches tree-accuracy-sync.py's own dump so a
    # merge that changes nothing semantically converges instead of re-diverging.
    return json.dumps(out, indent=2, sort_keys=True).encode("utf-8")


# --- registration -----------------------------------------------------------
_HANDLERS: Dict[str, Callable[[bytes, bytes], bytes]] = {
    # id-keyed field-merge (records edited in place -> merge same-id copies)
    "reasoning-bank.jsonl": merge_reasoning_bank,
    "guardrails.jsonl": merge_guardrails,
    # pattern signatures (): in-place-mutating writers (record-outcome
    # counter bumps, set-status retire) -> id-keyed field-merge, NOT line-union
    # (which would resurrect retired signatures). Mixed-format on-disk ids
    # (sig-001 legacy pad / sig-8+ unpadded) are preserved via the observed-form
    # id formatter — see merge_pattern_signatures.
    "pattern-signatures.jsonl": merge_pattern_signatures,
    # field-level YAML reconcile
    "team-state.yaml": merge_team_state,
    # aspiration/goal-id union (records edited in place)
    "aspirations.jsonl": merge_aspirations,
    # hypothesis pipeline: union by content-derived id + stage-monotonic
    # field-merge (records edited in place;  / rb-2849 — the cc-04
    # NON-multipart no_clobber freeze, sibling of bdab36ab's multipart fix).
    # pipeline-archive.jsonl shares the record shape AND the flow (resolve
    # moves live->archive), so it takes the same handler.
    "pipeline.jsonl": merge_pipeline,
    "pipeline-archive.jsonl": merge_pipeline,
    # aspirations-archive shares the record shape AND flow with aspirations
    # (complete/complete_intent/retire APPEND whole-aspiration records;
    # archive_sweep REWRITES normalizing via _normalize_terminal_goals_in) —
    # same handler. Writer inventory read per rb-245 (). Ported from
    # cc-02  registry expansion at the  unwedge merge.
    "aspirations-archive.jsonl": merge_aspirations,
    # outcome metrics: whole-file derived snapshot -> LWW by updated_at
    # (; box-relative counter wart tracked in ).
    "outcome-metrics.yaml": merge_outcome_metrics,
    # forged-skills registry (): keyed dict union under skills: —
    # append-mostly rows (retirement = status field, never deletion), so a
    # row on either side survives; same-name divergence -> whole-record
    # newer-forged_date/richer/_canon winner. CURE for the  +
    #  stale-base row-clobber incidents.
    "forged-skills.yaml": merge_forged_skills,
    # skill-relations graph (): sibling registry to forged-skills —
    # forged_relations list unioned by (source,target,type), co_invocation_log
    # entry-unioned then re-capped at the writers' own cap (cmd_co_invoke's
    # tail-cap is a legitimate delete path a naive union would resurrect).
    "skill-relations.yaml": merge_skill_relations,
    # tree-debt telemetry (): verified append-only by reading BOTH
    # writers (rb-245) — CLI tree.py + daemon tree_write.py, zero rewriters.
    "tree-debt.jsonl": merge_append_only_jsonl,
    # goal-selector anomaly telemetry: append-only.
    "goal-selector-anomalies.jsonl": merge_append_only_jsonl,
    # board read-cursors: append-only per-agent read receipts.
    "coordination-reads.jsonl": merge_append_only_jsonl,
    "decisions-reads.jsonl": merge_append_only_jsonl,
    # store-hygiene archive sinks (): jsonl_hygiene compact/rotate
    # APPENDS moved retired/superseded records archive-FIRST via
    # locked_modify_jsonl and NOTHING deletes from an archive (bounded-by-
    # design, the hygiene glob explicitly never matches *-archive*). Two boxes
    # compacting independently diverge these — line-union matches the writers'
    # documented at-least-once tolerance ("a crash leaves the moved records in
    # BOTH files — recoverable archive dup, never a live loss"). Caveat: the
    # same id archived on two boxes after divergent utilization increments
    # keeps BOTH lines (distinct bytes = distinct events); restore flows read
    # by id and pick the newest — union-over-collapse is deliberate here.
    "guardrails-archive.jsonl": merge_append_only_jsonl,
    "reasoning-bank-archive.jsonl": merge_append_only_jsonl,
    "pattern-signatures-archive.jsonl": merge_append_only_jsonl,
    # Triaged NOT-registered ( audit, dispositions on record):
    #   world/journal.jsonl — 12-byte "placeholder" seed artifact, zero
    #     writers found (agents journal into agents/<name>/journal.jsonl);
    #     freeze is a no-op on a file nothing writes.
    #   world/sources.yaml — reflect-on-outcome LLM read-modify-write
    #     reliability registry (keyed by source id, low write frequency);
    #     freeze acceptable until divergence is ever observed — the fix then
    #     is a keyed union à la merge_forged_skills with counter-max fields.
    #   Remaining world-root telemetry logs (curation-log, precheck-eval-log,
    #     *-sweep-metrics, etc.) — single-writer flows; registration deferred
    #     until a second writer or observed divergence justifies it.
    "findings-reads.jsonl": merge_append_only_jsonl,
    "general-reads.jsonl": merge_append_only_jsonl,
    # 5th board read-cursor ( — froze at streak 5 in the 2026-07-18
    # both-diverged backlog): same board.py mark-read locked_append_jsonl
    # writer as the four registered above.
    "reasoning-reads.jsonl": merge_append_only_jsonl,
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
    # non-default board channels (): same board_write.py append path +
    # same append-only contract as the canonical 4, but absent from
    # DEFAULT_CHANNELS so they were left unregistered and wedged on both-diverged.
    # The merge_append_only_jsonl docstring already claims to cover "the board
    # channels" -- this closes the gap. CORRECTION (): an earlier
    # revision of this comment claimed these non-default channels have "NO
    # archive/prune writer" and are "strictly safer" than the canonical 4 --
    # that was FALSE. store-hygiene.yaml rotates the GLOB `world/board/*.jsonl`
    # (enabled, mode:rotate, max_lines:5000, owner ), which matches ALL
    # eight channels -- reasoning/directives/events/feedback included -- exactly
    # as it matches coordination.jsonl. So all 8 carry the SAME accepted
    # rotate+line-union tradeoff, NOT a safer one: a both-diverged merge unions
    # a rotated-out record back into the live window; the next sweep re-archives
    # it -> at most a DUPLICATE record in <channel>-archive.jsonl. Bounded + no
    # data loss (board.py reads the LIVE file only, never the archive -- see
    # store-hygiene.yaml G11 note), so the registration STANDS on the accepted
    # tradeoff -- the same basis as the canonical 4, which are likewise rotated.
    "reasoning.jsonl": merge_append_only_jsonl,
    "directives.jsonl": merge_append_only_jsonl,
    "events.jsonl": merge_append_only_jsonl,
    "feedback.jsonl": merge_append_only_jsonl,
    # sweep/recheck telemetry metrics (all via _fileops.locked_append_jsonl):
    "defer-recheck-metrics.jsonl": merge_append_only_jsonl,
    "credential-defer-recheck-metrics.jsonl": merge_append_only_jsonl,
    "precondition-defer-recheck-metrics.jsonl": merge_append_only_jsonl,
    "parent-supersession-sweep-metrics.jsonl": merge_append_only_jsonl,
    "unblock-parent-status-sweep-metrics.jsonl": merge_append_only_jsonl,
    "routing-audit-target-status-sweep-metrics.jsonl": merge_append_only_jsonl,
    # Phase 4 bulk-override audit ledger (): multi-agent append-only
    # via _override_helpers.locked_append_jsonl (no rewrite writer). A shared
    # world store where concurrent overrides from two boxes can both-diverge and
    # wedge without a handler; strictly append-only (immutable audit records).
    "override-bypass-ledger.jsonl": merge_append_only_jsonl,
    #  ( remainder): the lower-churn shared append-only
    # stores that could still wedge on both-diverged. EACH verified strictly
    # append-only by reading its writer (rb-245 / rb-3153): merge_append_only_jsonl
    # is a commutative LINE-UNION, so a both-diverged merge RESURRECTS any
    # locally-deleted record -- a store with a rewrite/prune/rebuild writer must
    # NOT be registered. The 9 per-gate override ledgers (all write via
    # _fileops.locked_append_jsonl OR open(...,'a')/'>>' -- no rewrite writer):
    "blocker-gate-overrides.jsonl": merge_append_only_jsonl,
    "goal-duplication-overrides.jsonl": merge_append_only_jsonl,
    "loop-state-merge-overrides.jsonl": merge_append_only_jsonl,
    "origin-signal-overrides.jsonl": merge_append_only_jsonl,
    "output-style-overrides.jsonl": merge_append_only_jsonl,
    "phase-4-26-overrides.jsonl": merge_append_only_jsonl,
    "stale-read-overrides.jsonl": merge_append_only_jsonl,
    "uncommitted-work-overrides.jsonl": merge_append_only_jsonl,
    "missing-artifact-overrides.jsonl": merge_append_only_jsonl,
    # audit/telemetry append-only logs (writer verified: locked_append_jsonl or
    # open(...,'a')): skill_edit_gate (skill-rejected-edits), reflection-cadence-
    # stamp (reflection-history), aspirations.py _log_defer_extraction
    # (defer-date-extractions), retrieve.py _write_trace (retrieval-trace),
    # stop-hook-analyze (loop-death-detections), description-length gate.
    "skill-rejected-edits.jsonl": merge_append_only_jsonl,
    "reflection-history.jsonl": merge_append_only_jsonl,
    "defer-date-extractions.jsonl": merge_append_only_jsonl,
    "retrieval-trace.jsonl": merge_append_only_jsonl,
    "loop-death-detections.jsonl": merge_append_only_jsonl,
    "description-length-telemetry.jsonl": merge_append_only_jsonl,
    # changelog.jsonl + its rotation target changelog-archive.jsonl (,
    # ports cc-02 7b6801e1; SUPERSEDES the  "pruned -> exclude" call).
    # changelog.jsonl IS rotated by store-hygiene.yaml, but the rotation MOVES
    # lines into changelog-archive.jsonl -- it is NOT a rewrite/rebuild that drops
    # records -- so the pair carries the SAME bounded rotate+line-union tradeoff
    # already accepted for the board channels above: a both-diverged merge unions
    # the live file (at most RESURRECTING a just-rotated line as a bounded
    # duplicate, reconciled on the next rotation) and unions the archive (capturing
    # every rotated line). Dispatch is by basename, so these two entries cover ALL
    # six changelog stores at once -- world/, meta/, and agents/<name>/. Leaving
    # them unregistered froze the fleet's ENTIRE write-audit trail out of S3 for 5
    # weeks (2026-06-06 -> 2026-07-14) across all six stores -- the same freeze
    # class as rb-3150 (team-state peer shards), and why partner liveness read
    # days-stale. STEP-3 verification confirms live INTERSECT archive == 0 after
    # the reconcile; a non-zero overlap would mean guard-1005 resurrection bit and
    # the pair wants a paired-archive-aware handler instead of the plain line-union.
    "changelog.jsonl": merge_append_only_jsonl,
    "changelog-archive.jsonl": merge_append_only_jsonl,
    #  (2026-07-18 12-file both-diverged repair,  cure):
    # evolution event streams — NOT line-union (rb-245 read disproved
    # append-only: evolution-complete/stub-expiry REWRITE stub records in
    # place); revision_id-keyed status-monotonic merge instead. Dispatch by
    # basename covers all four streams' world/ paths.
    "self-evolution.jsonl": merge_evolution_stream,
    "skill-evolution.jsonl": merge_evolution_stream,
    "rule-evolution.jsonl": merge_evolution_stream,
    "script-evolution.jsonl": merge_evolution_stream,
    # program-evolution.jsonl: the 5th sibling stream — not in the 2026-07-18
    # freeze backlog but IDENTICAL writer set (evolution-record append /
    # evolution-complete rewrite incl. the program-only awaiting_acks status /
    # stub-expiry rewrite), so it takes the same handler rather than staying
    # the lone freeze-prone sibling.
    "program-evolution.jsonl": merge_evolution_stream,
    # meta/l1-pick-log.jsonl (): NOW writer-verified append-only —
    # _l1_pick.py open('a') + l1-domain-rename.py (self-documented "append");
    # leaves the DEFERRED list below.
    "l1-pick-log.jsonl": merge_append_only_jsonl,
    # meta/meta-log.jsonl (): NOW writer-verified append-only —
    # meta-yaml.py append_log() opens 'a' (the open('r') the old DEFERRED note
    # flagged is only mc-NNN ID ALLOCATION, not a rewrite; daemon twin
    # meta_yaml.py mirrors it). Accepted tradeoff: concurrent cross-box
    # allocation can collide mc-NNN ids and the union keeps both lines —
    # bounded duplicate-ID tolerance (next_meta_change_id takes max, so
    # allocation self-heals), same class as the archive-sink duplicates above.
    "meta-log.jsonl": merge_append_only_jsonl,
    # world/auto-fix-evidence-sweep-metrics.jsonl (): writer RETIRED
    # (zero code references fleet-wide; last record 2026-06-23) — immutable
    # historical run_summary log, so line-union reconciles the residual
    # divergence and nothing can violate append-only going forward.
    "auto-fix-evidence-sweep-metrics.jsonl": merge_append_only_jsonl,
    #  structured trio (per-file decisions on record in each handler):
    "infra-health.yaml": merge_infra_health,
    "goal-selection-strategy.yaml": merge_goal_selection_strategy,
    "hypothesis-category-bindings.json": merge_hypothesis_category_bindings,
    # DELIBERATELY NOT REGISTERED (rb-245 disqualified -- the audit's whole point):
    #   dead-ends.jsonl        -> meta-dead-ends.py write_all() does
    #                             locked_write_jsonl(DE_PATH, records) = full-file
    #                             REWRITE (read-modify-write). Line-union would
    #                             resurrect deleted dead-ends. Stays safe-freeze.
    #   knowledge-graph.jsonl  -> knowledge-graph-build.py REBUILDS the triple
    #                             store via locked_write_jsonl. A rebuild is a
    #                             rewrite; line-union would resurrect stale edges.
    # DEFERRED (writer not yet confirmed strictly append-only -- left unregistered
    # = safe-freeze, the conservative default; needs per-writer read before adding):
    #   scoring-criterion-audit.jsonl, and the
    #   file-contention/gate-d/history-save/history-shadow/write-queue-telemetry
    #   family (variable-path writers not resolved in the  pass).
    #   (pattern-signatures.jsonl left this list 2026-07-16 — registered above
    #   with merge_pattern_signatures, . meta-log.jsonl +
    #   l1-pick-log.jsonl left it 2026-07-18 — writer-verified and registered
    #   above, .)
    #   world/conventions/deploy-secrets.md ( disposition) ->
    #   hand-edited markdown with NO code writer; prose has no commutative
    #   merge unit, so it stays safe-freeze + hand-union on conflict (the
    #   2026-07-18 repair pushed LOCAL, which carried the  correction).
    # field-level YAML/JSON reconcile (records MUTATED IN PLACE -> per-field
    # reconcile; verified per-store by reading each writer, rb-245 / ):
    "module-health.yaml": merge_module_health,
    "aspirations-meta.json": merge_aspirations_meta,
    # pipeline meta: derived counters (LWW, self-correcting via recompute) +
    # micro_hypothesis_stats per-key union — rewritten by every pipeline
    # mutation, so it must reconcile for the  flow to stay unfrozen.
    "pipeline-meta.json": merge_pipeline_meta,
    # _tree.yaml: the 4th, highest-blast-radius freeze shape (~1140-node tree with
    # structural children/parent fields + a chronological growth log + CRLF). Node
    # reconcile a/b + top-level assembly c ->  COMPLETE. A running daemon
    # picks this up on its next normal restart; until then it keeps the prior
    # no-handler safe-freeze behavior. The merge is strictly better than that
    # whole-file default (it never drops a side's tree edits), so a mixed-fleet
    # activation window cannot corrupt worse than the status quo.
    "_tree.yaml": merge_tree,
}


def merge_handler_for(path) -> Optional[Callable[[bytes, bytes], bytes]]:
    """Return the commutative merge handler for ``path``, or None when the
    store is not merge-registered (the backend then keeps its safe-freeze
    behavior for that path).

    Dispatch is by basename EXCEPT for per-agent team-state shards
    (``.../team-state/agents/<name>.yaml``), whose basenames are dynamic
    (alpha.yaml/bravo.yaml/...) and so cannot be enumerated in _HANDLERS. Those
    match by PATH PATTERN (parent dir ``agents`` under ``team-state``) so new
    agents are covered automatically without touching this registry. Without
    the branch, shard basenames returned None and the backend froze peer shards
    on the both-diverged 412 -> fresh-SELF + stale-PEERS on every box (rb-3150;
    fixed g-115-2133). The composite ``team-state.yaml`` is NOT under
    ``team-state/agents/`` so it still routes by basename to merge_team_state."""
    parts = str(path).replace("\\", "/").split("/")
    if (len(parts) >= 3 and parts[-3] == "team-state"
            and parts[-2] == "agents" and parts[-1].endswith(".yaml")):
        return merge_team_state_shard
    return _HANDLERS.get(os.path.basename(str(path)))
