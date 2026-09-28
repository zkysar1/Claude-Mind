#!/usr/bin/env python3
"""Priority-merge lane for LOAD-BEARING worker captures ().

WHY THIS EXISTS — and why the goal's own framing understates it.

The goal says superseding knowledge "waits hours after the worker ships it,"
naming ONE gate: generalize_down runs only at aspirations-consolidate Step -1.
Measured, the latency is the PRODUCT OF TWO gates, and the unnamed one is the
larger:

  Gate A — the Body must CLOSE. body-merge._enumerate_pending filters on
           `manifest.body_state == "closed-pending-merge"`, so an ACTIVE Body's
           captures are never merged at all, no matter how often consolidation
           runs. Measured 2026-08-15 (alpha, cc-08): one Body 21 work units deep
           held 237 capture entries no reducer could see, and would have held
           them under a one-minute consolidation cadence.
  Gate B — the reducer must run consolidation. This is the gate the goal names.

A fast lane that inherited Gate A would be useless to exactly the Bodies that
need it most, so THIS lane enumerates ACTIVE Bodies too. That is its whole
reason for existing as a separate pass rather than a knob on generalize_down.

AND THE WAIT IS NOT THE WORST OF IT. At cap, wm append FIFO-evicts the OLDEST
entry, so on a long-running Body the early findings — the ones that have been
waiting longest, i.e. the ones this lane exists to rescue — are destroyed first.
Same Body, same measurement: 237 entries EVICTED (spark 144, exp 74, hyp 19)
against caps of 50/20/10, ~74% of everything spark_capture was ever handed.
Second instance of the g-306-289 measurement (215 on cc-07), so it is the rule.
That is why `load_bearing` also buys eviction-exemption in wm.py /
wm_write.py::_eviction_sort_key: a priority lane whose entries are gone before
the lane runs is decorative.

WHAT THIS IS NOT. It does not merge whole Bodies, does not mark any Body merged,
does not touch manifests, and deletes nothing. generalize_down remains the sole
owner of Body lifecycle; this pass only COPIES flagged entries forward. Running
it can therefore never lose divergence — the worst case is that it does nothing
and consolidation picks everything up later, exactly as today.

IDEMPOTENCE, and the one trap in it: dedup is by CONTENT HASH
(body-merge._dedup_append), so re-running this pass, and the later full
generalize_down, both skip entries already present. That holds ONLY because
entries are copied VERBATIM. Stamping anything onto an entry (a merged_at, a
source-body id) changes its hash and the full merge would then append a second
copy — which is why all telemetry below lives OUTSIDE the entries.

REDUCER-ONLY. A worker running this would be writing the agent-wide WM, which is
the one thing the worker contract forbids, and would make it an Nth reducer.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import socket
import statistics
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import yaml  # noqa: E402


def _load_hyphen_module(mod_name: str, filename: str):
    """Load a hyphen-named sibling module (not importable by name).

    Same shape as body-merge.py::_load_body_manifest, and cached in sys.modules
    for the same reason.
    """
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# body-merge.py owns the merge primitives. Reuse them rather than reimplement:
# a second content-hash or a second dedup would drift from the one generalize_down
# actually uses, and then this lane's "already merged" and the full merge's
# "already merged" would disagree — producing duplicates precisely when both run.
bmg = _load_hyphen_module("body_merge", "body-merge.py")
bm = bmg.bm  # body-manifest.py: the SOLE manifest reader/writer + path helpers

import wm as wm_mod  # noqa: E402  — CAPTURE_SLOTS lives with the slot registry

# : the session/-rooted carrier. Without it this lane is blind to every
# Body on another box — sessions/ is sync-excluded and machine-local, so the
# store-listing union above can never yield a remote Body's WM.
import body_capture_carrier as bcc  # noqa: E402

CAPTURE_SLOTS = wm_mod.CAPTURE_SLOTS
TELEMETRY_FILENAME = "capture-fast-lane.jsonl"

#  — durable consumed-watermark for the capture lanes. ONE dict slot
# ({slot_name: [content_hash, ...]}) rather than one list slot per lane, so the
# two cleanup predicates (wm.py RESET_SURVIVING_SLOTS + the survive test that
# iterates the lanes BY NAME) each gain exactly one entry instead of four.
# It MUST be a member of wm.RESET_SURVIVING_SLOTS: a watermark wiped by
# wm-reset silently re-opens the very bug it closes, and would be far harder to
# see the second time (guard-2552 — when adding a new WM slot, do not stop at
# making the write work; audit the cleanup predicates).
CONSUMED_HASHES_SLOT = "capture_consumed_hashes"
#  — the ring is bounded by WHAT IS STILL OFFERED, not by a count.
# It was a 2000-hash FIFO, sized as "~12x" the live slot, while the Bodies'
# carriers re-offer every flagged entry they ever captured and never retire one:
# measured 2026-09-25 (alpha reducer, cc-07) 3,655 offered spark hashes against a
# full 2000-hash ring. Every hash the FIFO evicted was re-offered, and a DRAINED
# entry came back at the next close (13 of 13, 17 of 54, 15 of 15 measured on
# 2026-09-24). A hash a source still offers is now never evicted below the
# ceiling; CAP bounds only the hashes NO readable source offers any more, which
# can never be re-delivered and are kept purely as insurance.
CONSUMED_HASHES_CAP = 2000
# Hard bound on one lane's whole ring, offered or not. Reaching it means the
# offered population outgrew it and re-delivery resumes; the pass reports it as
# `overflow` in `consumed_ring` rather than evicting silently.
CONSUMED_HASHES_CEILING = 20000


def _now() -> datetime:
    return datetime.now()


def is_worker_body(agent: str, project_root: Path, sid: str | None = None) -> bool:
    """True when THIS process is a worker Body of `agent`.

    Same predicate the worker loop's Phase -0 uses: a worker has a forked
    per-session working-memory.yaml; the reducer stays on the agent-wide WM.
    """
    sid = sid or os.environ.get("MIND_SID") or ""
    if not sid:
        return False
    adir = bm._agent_dir(project_root, agent)
    return (adir / bm._SESSIONS_DIRNAME / sid / bm._WM_FILENAME).is_file()


def _enumerate_all_bodies(sessions_root: Path, backend) -> list:
    """[(unit_key, manifest_dict_or_empty), ...] for EVERY Body, any body_state.

    Deliberately NOT body-merge._enumerate_pending: that one filters to
    closed-pending-merge, which is Gate A above and the thing this lane exists
    to bypass. The local glob is UNIONed with the authoritative listing for the
    same cross-box reason (g-115-6240) — a Body that shipped from another box
    has its files only in the store.
    """
    unit_keys: set = set()
    if sessions_root.is_dir():
        unit_keys.update(p.name for p in sessions_root.iterdir() if p.is_dir())
    if backend is not None:
        try:
            unit_keys.update(backend.list_dir(sessions_root.resolve()))
        except Exception:  # noqa: BLE001 — store listing is additive, never fatal
            pass
    out = []
    for unit_key in sorted(unit_keys):
        manifest = {}
        raw, _transient = bmg._read_staged_bytes(
            backend, sessions_root / unit_key / bm._MANIFEST_FILENAME)
        if raw is not None:
            try:
                loaded = yaml.safe_load(raw)
                if isinstance(loaded, dict):
                    manifest = loaded
            except yaml.YAMLError:
                manifest = {}
        out.append((unit_key, manifest))
    return out


def _read_body_slots(backend, sessions_root: Path, unit_key: str) -> tuple:
    """(slots_dict_or_None, unreadable) for one Body's WM.

    Shared by fast_lane and census so `sources_unreadable` means the same thing
    in both: a transient read error, a YAML error or a non-dict WM is
    UNREADABLE; an absent WM, or one without a dict `slots`, offers nothing and
    is not a failed read.
    """
    raw, transient = bmg._read_staged_bytes(
        backend, sessions_root / unit_key / bm._WM_FILENAME)
    if transient:
        return None, True
    if raw is None:
        return None, False
    try:
        body_wm = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return None, True
    if not isinstance(body_wm, dict):
        return None, True
    body_slots = body_wm.get("slots")
    return (body_slots if isinstance(body_slots, dict) else None), False


def _flagged(entries) -> list:
    if not isinstance(entries, list):
        return []
    return [e for e in entries
            if isinstance(e, dict) and e.get("load_bearing")]


def _lane_total(entries) -> int:
    """Denominator for the flagged:total ratio — every entry, flagged or not.

    `flagged_seen` alone is UNINTERPRETABLE (guard-4054): 40 flagged cannot
    distinguish 40-of-50 (the flag has stopped discriminating) from 40-of-400
    (healthy). The flag buys eviction-exemption and fast-lane priority, so both
    of those powers decay as the ratio rises — at 80% the exemption forces
    flagged-vs-flagged eviction, which is the plain FIFO it exists to prevent,
    and the priority merge promotes 80% of the lane. Nothing measured this
    ratio, which is why the degradation was invisible (g-306-365).

    Callers pass None instead of this when the denominator is UNMEASURABLE —
    see the carrier note in `_merge_flagged`. None is not 0: reporting an
    unknown denominator as zero would let the instrument express a value it
    cannot measure (the same reasoning `_age_minutes` gives for returning None).
    """
    return len(entries) if isinstance(entries, list) else 0


def _bound_consumed(ring: list, offered) -> tuple:
    """Bound one lane's consumed ring; returns (ring, trimmed, overflow).

    `offered` is the set of hashes some source offered THIS pass, or None when a
    source was unreadable. With None nothing below the ceiling is evicted: an
    unread carrier's hashes would otherwise look un-offered and be trimmed, and
    that carrier would re-deliver them on the next pass.
    """
    kept = list(ring)
    trimmed = 0
    if offered is not None:
        spare = [h for h in kept if h not in offered]
        if len(spare) > CONSUMED_HASHES_CAP:
            drop = set(spare[:len(spare) - CONSUMED_HASHES_CAP])
            kept = [h for h in kept if h not in drop]
            trimmed = len(drop)
    overflow = max(0, len(kept) - CONSUMED_HASHES_CEILING)
    return kept[overflow:], trimmed, overflow


def _age_minutes(entry, now: datetime):
    """Minutes from the entry's _item_ts to now, or None if unparseable.

    None is returned rather than 0 on purpose: a missing or malformed stamp is
    an UNKNOWN latency, and folding it in as zero would drag the median toward
    a healthy-looking number the data never supported (guard-3440 —- never let
    an instrument express a value it cannot measure).
    """
    ts = entry.get("_item_ts") if isinstance(entry, dict) else None
    if not isinstance(ts, str):
        return None
    try:
        return (now - datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")).total_seconds() / 60.0
    except ValueError:
        return None


def fast_lane(agent: str, project_root: Path | None = None,
              dry_run: bool = False, allow_worker: bool = False) -> dict:
    """Copy load-bearing capture entries from every Body into the reducer WM.

    Returns a summary dict; never raises on a single unreadable Body.
    """
    pr = project_root or bmg._project_root()
    adir = bm._agent_dir(pr, agent)  # validates the agent name
    state_dir = adir / bm._STATE_DIRNAME
    sessions_root = adir / bm._SESSIONS_DIRNAME
    reducer_wm_path = state_dir / bm._WM_FILENAME
    now = _now()

    summary = {
        "agent": agent,
        "ts": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "role_refused": False,
        "bodies_scanned": 0,
        "bodies_contributing": 0,
        "flagged_seen": 0,       # flagged entries found across all Bodies
        "merged": 0,             # flagged entries NEW to the reducer WM this run
        "already_present": 0,    # flagged but already merged (the steady state)
        #  — PROVENANCE FOR `already_present`. _read_yaml returns {}
        # for an ABSENT file, so on any box that is not the reducer this field
        # computes a confident 0 from a file that was never there. That zero
        # was read as "not one of the 2611 has ever been merged" and produced a
        # HIGH goal premised on broken delivery. None until the read happens;
        # False means `already_present` is NOT a measurement (guard-3612: an
        # empty field is not a measurement until you read its writer's
        # normalization; guard-346: a wrong-process instrument supports no
        # conclusion in EITHER direction, so this must not be read as evidence
        # that delivery works either).
        "reducer_wm_present": None,
        #  — the DENOMINATOR. `flagged_seen` on its own cannot say
        # whether the flag still discriminates, and that is the whole failure
        # mode: the flag buys eviction-exemption AND fast-lane priority, so as
        # the flagged share rises both powers decay toward the plain FIFO they
        # exist to prevent. Nothing measured the share, so the degradation was
        # structurally invisible — no gate could catch it (the flag is
        # honour-system by design) and no cadence reported it.
        #
        # SCOPED TO THE sessions/ PASS ON PURPOSE. The carrier ships ONLY
        # flagged entries (body_capture_carrier.py:24), so folding it in would
        # report flagged/flagged = 100% for every remote Body — a denominator
        # that is not merely wrong but wrong in the alarming direction. Its
        # flagged entries still count in `flagged_seen`; they are excluded from
        # `flagged_measurable` so the pair below is always like-for-like.
        # `flagged_seen - flagged_measurable` is therefore the flagged
        # population whose share is genuinely UNKNOWN from here, and that is a
        # real limit of this instrument rather than a gap to paper over.
        "entries_seen": 0,        # all entries (flagged or not), sessions/ pass
        "flagged_measurable": 0,  # the numerator that PAIRS with entries_seen
        "by_slot_ratio": {},      # {slot: {"flagged": N, "total": M}}
        "by_slot": {},
        "by_body": {},
        "latency_minutes_median": None,
        "latency_minutes_max": None,
        "latency_unmeasurable": 0,
        # : how much of `merged` arrived via the session/-rooted
        # carrier rather than a sessions/ WM. Reported SEPARATELY because it is
        # the only number that distinguishes "this lane reaches remote Bodies"
        # from "this lane merged something on its own box" — the two were
        # indistinguishable before, which is how the blindness stayed invisible
        # for 1.5 days while the lane reported success.
        "carrier_merged": 0,
        "carrier_bodies": 0,
        "dry_run": bool(dry_run),
    }

    if not allow_worker and is_worker_body(agent, pr):
        # Fail SAFE and LOUD: a worker running this would write the agent-wide
        # WM (forbidden) and act as a second reducer.
        summary["role_refused"] = True
        return summary

    backend = bmg._get_backend()
    summary["reducer_wm_present"] = reducer_wm_path.is_file()
    reducer_wm = bmg._read_yaml(reducer_wm_path)
    if not isinstance(reducer_wm, dict):
        reducer_wm = {}
    slots = reducer_wm.setdefault("slots", {})
    if not isinstance(slots, dict):
        slots = {}
        reducer_wm["slots"] = slots

    latencies: list = []
    changed = False
    seen_bodies: set = set()
    offered: dict = {}       # {slot: hashes of every flagged entry offered this pass}
    unreadable = 0           # sources whose flagged entries this pass could not see

    def _merge_flagged(slot_pairs) -> int:
        """Merge one Body's flagged entries; returns how many were NEW.

        Shared by BOTH sources — the local sessions/ WM and the session/-rooted
        carrier — so there is exactly ONE dedup and ONE latency implementation.
        A second copy for the carrier would be the same drift this module's
        docstring refuses for _content_hash: the two paths' notions of "already
        merged" would diverge and produce duplicates precisely when both run.

        Takes (slot_name, flagged, total) triples. `total` is the lane's full
        entry count for the flagged:total ratio, or None where the source
        cannot supply one (the carrier — see `_lane_total`).
        """
        nonlocal changed
        contributed = 0
        for slot_name, flagged, total in slot_pairs:
            # DENOMINATOR BEFORE THE SKIP BELOW, deliberately. A Body holding
            # 50 entries and 0 flagged is a HEALTHY lane and is exactly the
            # observation the ratio needs; counting it only when it already has
            # a flagged entry would restrict the population to Bodies that pass
            # the very test being measured and bias the share upward. That is
            # the same selection effect the ratio exists to expose, so it must
            # not be baked into the instrument.
            # `total or flagged` — an entirely ABSENT lane contributes no row at
            # all. A {flagged: 0, total: 0} row expresses a share that does not
            # exist and would hand any downstream reader of the telemetry JSONL
            # a division by zero; omitting it is the same reasoning `_age_minutes`
            # applies to an unparseable stamp. Caught by the negative control in
            # test_ratio_absent_when_no_capture_entries_exist.
            if total is not None and (total or flagged):
                row = summary["by_slot_ratio"].setdefault(
                    slot_name, {"flagged": 0, "total": 0})
                row["flagged"] += len(flagged)
                row["total"] += total
                summary["flagged_measurable"] += len(flagged)
                summary["entries_seen"] += total
            if not flagged:
                continue
            summary["flagged_seen"] += len(flagged)
            offered.setdefault(slot_name, set()).update(
                bmg._content_hash(e) for e in flagged)
            existing = slots.get(slot_name)
            if not isinstance(existing, list):
                existing = []
            before = len(existing)
            # CONSUMED-WATERMARK (). The live slot alone is the WRONG
            # dedup basis: the consumer's mandated clear EMPTIES it, and source
            # Bodies retain their flagged entries indefinitely, so every close
            # re-offers the full set (guard-4154). Suppression held only while
            # the entries were still sitting here — clearing is what re-delivers.
            # The watermark records what has EVER been merged, so the clear is
            # irrelevant by construction and the consumer needs no new
            # obligation. Recorded at MERGE time deliberately: a consume-time
            # write would have to be ordered before the clear, and a crash
            # between them would silently drop the batch.
            wm_all = slots.get(CONSUMED_HASHES_SLOT)
            if not isinstance(wm_all, dict):
                wm_all = {}
            prior = wm_all.get(slot_name)
            if not isinstance(prior, list):
                prior = []
            # VERBATIM copy — see the idempotence note in the module docstring.
            merged_list = bmg._dedup_append(existing, flagged, extra_seen=prior)
            added = len(merged_list) - before
            if added:
                # Unbounded HERE on purpose: the bound is applied once, after
                # both passes, when the whole offered set is known ().
                # The old per-merge `[-CONSUMED_HASHES_CAP:]` could evict a hash
                # mid-pass that a LATER Body in the same pass then re-offered.
                fresh = [bmg._content_hash(e) for e in merged_list[before:]]
                wm_all[slot_name] = prior + fresh
                slots[CONSUMED_HASHES_SLOT] = wm_all
            if added:
                slots[slot_name] = merged_list
                changed = True
                contributed += added
                summary["by_slot"][slot_name] = summary["by_slot"].get(slot_name, 0) + added
                summary["merged"] += added
                # Latency is measured only for entries that were ACTUALLY new
                # this run. Counting already-present ones would re-measure the
                # same entry on every pass and inflate the median forever.
                newly = merged_list[before:]
                for e in newly:
                    age = _age_minutes(e, now)
                    if age is None:
                        summary["latency_unmeasurable"] += 1
                    else:
                        latencies.append(age)
            summary["already_present"] += len(flagged) - added
        return contributed

    for unit_key, manifest in _enumerate_all_bodies(sessions_root, backend):
        summary["bodies_scanned"] += 1
        seen_bodies.add(unit_key)
        body_slots, bad = _read_body_slots(backend, sessions_root, unit_key)
        if bad:
            unreadable += 1
            continue
        if body_slots is None:
            continue

        # The sessions/ WM is the FULLER record — it holds every entry, flagged
        # or not (see the carrier note below) — so it is the one source that can
        # supply a denominator.
        contributed = _merge_flagged(
            (s, _flagged(body_slots.get(s)), _lane_total(body_slots.get(s)))
            for s in CAPTURE_SLOTS)

        if contributed:
            summary["bodies_contributing"] += 1
            summary["by_body"][unit_key] = {
                "added": contributed,
                "body_state": manifest.get("body_state"),
                "via": "sessions",
            }

    #  — CARRIER PASS. This is the leg that reaches a Body on another
    # box; everything above can only ever see this box. Runs SECOND on purpose:
    # for a same-box Body the sessions/ WM is the fuller record (it holds every
    # entry, flagged or not), so letting it merge first means the carrier's
    # content-hash pass is a no-op there rather than a competing source. The
    # ordering is an optimisation, not a correctness requirement — dedup is by
    # content hash, so either order converges to the same set.
    carrier_skips: list = []
    carriers = bcc.read_carriers(state_dir, backend, skipped=carrier_skips)
    unreadable += len(carrier_skips)
    for unit_key, by_slot in sorted(carriers.items()):
        if unit_key not in seen_bodies:
            summary["bodies_scanned"] += 1
            seen_bodies.add(unit_key)
        # None, not a count: the carrier ships ONLY flagged entries
        # (body_capture_carrier.py:24), so its "total" would equal its flagged
        # count and report 100% for every remote Body. These entries still
        # count in `flagged_seen`; they are excluded from the ratio pair, and
        # the difference is reported as denominator-unmeasurable rather than
        # folded in silently.
        contributed = _merge_flagged(
            (s, _flagged(by_slot.get(s)), None) for s in CAPTURE_SLOTS)
        if not contributed:
            continue
        summary["carrier_merged"] += contributed
        summary["carrier_bodies"] += 1
        row = summary["by_body"].get(unit_key)
        if row is None:
            summary["bodies_contributing"] += 1
            summary["by_body"][unit_key] = {
                "added": contributed,
                # No manifest: a remote Body's body-manifest.yaml is
                # machine-local too, so its state is genuinely UNKNOWN from
                # here. None, not a guess — the lane never needs it (it does
                # not touch Body lifecycle) and inventing "active" would be a
                # claim nothing measured.
                "body_state": None,
                "via": "carrier",
            }
        else:
            row["added"] += contributed
            row["via"] = "sessions+carrier"

    #  — RECONCILE THE CONSUMED RINGS WITH WHAT IS STILL OFFERED.
    # Two moves per lane, both needing the whole pass's offered set:
    #  1. Record every LIVE entry a source offers. The ring used to learn only
    #     what THIS lane merged, so an entry delivered by generalize_down's
    #     merge_wm had no hash at all, and once drained its carrier re-offered it.
    #     While it sits in the slot the live dedup covers it; recording it now is
    #     what keeps it covered after the drain removes it.
    #  2. Bound the ring by `_bound_consumed`: offered hashes survive, and only
    #     hashes no source offers are trimmed to CAP.
    # Residual, by construction: an entry that enters and leaves the slot
    # between two passes is never seen here, and comes back once.
    summary["sources_unreadable"] = unreadable
    ring_rows = {}
    wm_all = slots.get(CONSUMED_HASHES_SLOT)
    if not isinstance(wm_all, dict):
        wm_all = {}
    for slot_name in CAPTURE_SLOTS:
        ring = wm_all.get(slot_name)
        if not isinstance(ring, list):
            ring = []
        off = offered.get(slot_name, set())
        in_ring = set(ring)
        held = []
        live = slots.get(slot_name)
        for e in (live if isinstance(live, list) else []):
            if not isinstance(e, dict):
                continue
            h = bmg._content_hash(e)
            if h in off and h not in in_ring:
                held.append(h)
                in_ring.add(h)
        if not ring and not held:
            continue
        new_ring, trimmed, overflow = _bound_consumed(
            ring + held, off if not unreadable else None)
        ring_rows[slot_name] = {"size": len(new_ring), "held_recorded": len(held),
                                "trimmed": trimmed, "overflow": overflow}
        if new_ring != ring:
            wm_all[slot_name] = new_ring
            slots[CONSUMED_HASHES_SLOT] = wm_all
            changed = True
    summary["consumed_ring"] = ring_rows

    if latencies:
        summary["latency_minutes_median"] = round(statistics.median(latencies), 1)
        summary["latency_minutes_max"] = round(max(latencies), 1)

    if changed and not dry_run:
        bmg._write_yaml_atomic(reducer_wm_path, reducer_wm)
        _append_telemetry(state_dir, summary)

    return summary


def _append_telemetry(state_dir: Path, summary: dict) -> None:
    """One JSONL row per run that moved something. Best-effort by contract —
    telemetry must never be able to fail the merge that produced it."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / TELEMETRY_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(summary, sort_keys=True) + "\n")
    except OSError:
        pass


def _ratio_fragment(summary: dict) -> str:
    """The flagged:total share per lane, for the reducer's close output.

    Appears on EVERY non-refused branch including the 0-merged one, and that
    is the load-bearing part rather than a formatting nicety (guard-3221). A
    lane sitting at 80% flagged with nothing NEW to merge is precisely the
    state this report exists to surface — everything already merged is the
    steady state, not an absence of signal — so a ratio that printed only when
    `merged` was non-zero would be silent exactly when it matters most.

    Prints flagged AND total, never a bare percentage (guard-4054: a rate is
    uninterpretable without the arrival count beside it).
    """
    # THE REMAINDER IS COMPUTED ABOVE BOTH EARLY RETURNS, deliberately
    # (fresh-eyes F1 on ). Carrier-sourced flagged entries carry no
    # denominator, and when they are the ONLY flagged entries -- the ordinary
    # CROSS-BOX case, which is what this lane exists for -- `by_slot_ratio` is
    # empty and `parts` is empty, so both guards below fire. Computing the
    # remainder after them left the caveat UNREACHABLE in exactly that case:
    # the reducer printed no share information at all, byte-indistinguishable
    # from a lane nobody measured. That is the defect this report was filed to
    # fix, reproduced inside the fix for it.
    unmeasurable = ((summary.get("flagged_seen") or 0)
                    - (summary.get("flagged_measurable") or 0))

    per = summary.get("by_slot_ratio")
    parts = []
    if isinstance(per, dict):
        for slot in sorted(per):
            row = per[slot] or {}
            fl, tot = row.get("flagged", 0), row.get("total", 0)
            if not tot:
                continue
            parts.append(f"{slot} {fl}/{tot}={100.0 * fl / tot:.0f}%")
    if not parts:
        # No measurable lane. Report the unmeasurable population when there is
        # one; stay silent ONLY when there is genuinely nothing to say -- the
        # empty-lane case pinned by test_ratio_absent_when_no_capture_entries_exist.
        if unmeasurable > 0:
            return (" | load-bearing share: none measurable "
                    f"({unmeasurable} flagged carrier-sourced, no denominator)")
        return ""
    frag = " | load-bearing share: " + ", ".join(parts)
    if unmeasurable > 0:
        frag += f" (+{unmeasurable} carrier-sourced, share unmeasurable)"
    return frag


def _ring_fragment(summary: dict) -> str:
    """Names any lane whose consumed ring hit CONSUMED_HASHES_CEILING ().

    An overflow evicts hashes a source still offers, so drained entries start
    coming back again. That must print on the 0-merged branch too.
    """
    over = {s: r.get("overflow") for s, r in (summary.get("consumed_ring") or {}).items()
            if isinstance(r, dict) and r.get("overflow")}
    if not over:
        return ""
    return (" | consumed-ring OVERFLOW (drained captures will be re-delivered): "
            + ", ".join(f"{s}={n}" for s, n in sorted(over.items())))


def format_line(summary: dict) -> str:
    """The one-line form for the reducer's existing iteration-close output."""
    if summary.get("role_refused"):
        return "[capture-fast-lane] SKIPPED — worker Body (reducer-only pass)"
    warn = ""
    if summary.get("reducer_wm_present") is False:
        warn = ("[capture-fast-lane] UNMEASURABLE — no reducer working memory at "
                "the expected path; an absent file reads as empty, so "
                "`already_present` below is NOT a measurement. Run this on the "
                "reducer box before concluding anything about delivery.\n")
    if not summary.get("merged"):
        return (warn + "[capture-fast-lane] 0 load-bearing captures to merge "
                f"({summary.get('bodies_scanned', 0)} Bodies scanned, "
                f"{summary.get('already_present', 0)} already merged)"
                + _ratio_fragment(summary) + _ring_fragment(summary))
    med = summary.get("latency_minutes_median")
    med_s = f"{med}m" if med is not None else "n/a"
    unmeas = summary.get("latency_unmeasurable") or 0
    tail = f", {unmeas} unmeasurable" if unmeas else ""
    # : name the carrier contribution explicitly. It is the only field
    # that shows this lane reached a Body on ANOTHER box, and the goal's own
    # production check is "the reducer's iteration-close prints a non-zero
    # merged count with a worker on a different box" — a bare total cannot
    # answer that, because a same-box merge produces an identical-looking line.
    carried = summary.get("carrier_merged") or 0
    if carried:
        tail += (f", {carried} via carrier from "
                 f"{summary.get('carrier_bodies') or 0} remote Body(s)")
    return (warn + "[capture-fast-lane] merged "
            f"{summary['merged']} load-bearing capture(s) from "
            f"{summary['bodies_contributing']}/{summary['bodies_scanned']} Bodies "
            f"— median flag-to-merge {med_s}, max "
            f"{summary.get('latency_minutes_max')}m{tail} "
            f"[{', '.join(f'{k}={v}' for k, v in sorted(summary['by_slot'].items()))}]"
            + _ratio_fragment(summary) + _ring_fragment(summary))


# --------------------------------------------------------------------------
# READ-ONLY CENSUS of the consumed rings (, gap-222)
# --------------------------------------------------------------------------
# Hand-rolled five times (importlib _content_hash + wm-read dumps + an ad-hoc
# set join) before it lived here. A SEPARATE path, never fast_lane(dry_run=True):
# a report flag on a writer skips the writer's guards (guard-3342), and the dry
# run reports the ring AFTER a hypothetical pass, not the ring that is stored.
# Nothing below writes.
#
# Why not the consumed/never-consumed split alone (guard-7413): since
#  every live entry a readable source offers is recorded on each
# pass, so that split measures SOURCE RETENTION, not arrivals or backlog. What
# can show re-delivery is the ring against the OFFERED population, offered
# hashes the ring lacks, the two bounds, sources_unreadable — and, because ring
# membership means delivered ONCE rather than processed (gap-222's 5th
# encounter), live source goal ids joined against the goals a prior spark
# replay filed from them.
#
# Every input is read STORE-FIRST, the agent-wide WM included. fast_lane reads
# that WM locally because it runs only on the reducer, whose local file is the
# write-through copy; the census runs on any box, and off the reducer's box the
# local file is a stale mirror (guard-980, guard-5930). Measured on cc-09
# 2026-09-28: local 13,490,143 B written 09-24 against a 12,528,343 B store copy.
_RELAY_SOURCE_RE = re.compile(r"spark_capture from (g-\d+(?:-\d+)+)")


def _last_pass(state_dir: Path, backend):
    """The newest fast-lane telemetry row, or None. A pass that changed nothing
    writes no row, so this is the last pass that MOVED something."""
    raw, _ = bmg._read_staged_bytes(backend, state_dir / TELEMETRY_FILENAME)
    if raw is None:
        return None
    lines = raw.decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            return {k: row.get(k) for k in
                    ("ts", "merged", "sources_unreadable", "consumed_ring")}
    return None


def _relay_join(relay_goals: list, live: list, slot: str, as_of=None) -> dict:
    """Join live source goal ids against goals a spark replay filed from them.

    The replay drains EVERY entry of its batch's goal ids after filing, so a
    live entry of a relayed goal arrived after that drain. One whose
    `_item_ts` predates the relay was captured before it: re-delivered (the
    restore signal aspirations-spark names), or a capture that reached the
    reducer late. `_item_ts` is the capture time, not the arrival, so the
    count bounds re-delivery from above. Entries stamped after the relay are
    new captures. The corpus is whatever the caller's query returned; archived
    goals are not in it (guard-7302), so a source with no relay here is
    unproven, not clean.

    `as_of` is when the WM was last written. A relay filed after that drained
    nothing in it, so it is set aside: without this, a stale WM copy reads
    every later relay's batch as re-delivered (measured on a worker box's local
    mirror, 3.9 days old).
    """
    relays: dict = {}
    marked = 0
    later = 0
    for g in relay_goals:
        if not isinstance(g, dict):
            continue
        sources = set(_RELAY_SOURCE_RE.findall(g.get("description") or ""))
        if not sources:
            continue
        marked += 1
        created = g.get("created_at")
        if as_of and isinstance(created, str) and created[:19] >= as_of:
            later += 1
            continue
        for src in sources:
            relays.setdefault(src, []).append(g)
    by_source: dict = {}
    for e in live:
        if e.get("goal_id"):
            by_source.setdefault(e["goal_id"], []).append(e.get("_item_ts"))
    rows = []
    for gid in sorted(by_source):
        rel = relays.get(gid)
        if not rel:
            continue
        stamps = sorted(r["created_at"][:19] for r in rel
                        if isinstance(r.get("created_at"), str))
        first = stamps[0] if stamps else None
        tss = by_source[gid]
        rows.append({
            "source_goal": gid,
            "live_entries": len(tss),
            "predating_relay": sum(1 for t in tss if isinstance(t, str)
                                   and first and t[:19] < first),
            "undated": sum(1 for t in tss if not isinstance(t, str)),
            "first_relay_at": first,
            "relay_goals": sorted({str(r.get("id") or r.get("goal_id"))
                                   for r in rel}),
        })
    return {
        "slot": slot,
        "relay_goals_read": len(relay_goals),
        "relay_marked": marked,
        "relays_after_wm": later,
        "live_sources": len(by_source),
        "sources_with_relay": len(rows),
        "live_entries_predating_relay": sum(r["predating_relay"] for r in rows),
        "rows": rows,
    }


def census(agent: str, project_root: Path | None = None, goal_ids=None,
           relay_goals=None, relay_slot: str = "spark_capture") -> dict:
    """Read-only census of the capture lanes against their consumed rings.

    Reads the sources fast_lane reads, store-first, and writes nothing, so it
    runs on any box. The report names which copy of the agent-wide WM it read
    and when that copy was written.
    """
    pr = project_root or bmg._project_root()
    adir = bm._agent_dir(pr, agent)  # validates the agent name
    state_dir = adir / bm._STATE_DIRNAME
    sessions_root = adir / bm._SESSIONS_DIRNAME
    reducer_wm_path = state_dir / bm._WM_FILENAME
    backend = bmg._get_backend()

    raw, wm_source = None, None
    if backend is not None:
        try:
            raw = backend.read_authoritative_bytes(reducer_wm_path.resolve())
            wm_source = "store"
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 — store unreadable: read local, say so
            wm_source = "local mirror (store unreadable)"
    if raw is None:
        try:
            raw = reducer_wm_path.read_bytes()
            wm_source = wm_source or "local (not in the store)"
        except OSError:
            wm_source = None
    written = None
    try:
        if wm_source == "store" and hasattr(backend, "head_last_modified"):
            written = backend.head_last_modified(reducer_wm_path.resolve())
        elif raw is not None:  # a local backend's store IS the local file
            written = reducer_wm_path.stat().st_mtime
    except Exception:  # noqa: BLE001 — unknown only disables the relay filter
        written = None

    report = {
        "agent": agent,
        "ts": _now().strftime("%Y-%m-%dT%H:%M:%S"),
        "hostname": socket.gethostname(),
        "worker_body": is_worker_body(agent, pr),
        "reducer_wm_present": raw is not None,
        "wm_source": wm_source,
        "wm_as_of": (datetime.fromtimestamp(written).strftime("%Y-%m-%dT%H:%M:%S")
                     if written else None),
        "cap": CONSUMED_HASHES_CAP,
        "ceiling": CONSUMED_HASHES_CEILING,
    }
    reducer_wm = yaml.safe_load(raw) if raw else None
    slots = reducer_wm.get("slots") if isinstance(reducer_wm, dict) else None
    if not isinstance(slots, dict):
        slots = {}
    rings = slots.get(CONSUMED_HASHES_SLOT)
    if not isinstance(rings, dict):
        rings = {}

    offered = {s: {} for s in CAPTURE_SLOTS}  # {slot: {hash: goal_id}}

    def _offer(by_slot) -> None:
        for s in CAPTURE_SLOTS:
            for e in _flagged(by_slot.get(s)):
                offered[s][bmg._content_hash(e)] = e.get("goal_id")

    unreadable = 0
    seen: set = set()
    for unit_key, _manifest in _enumerate_all_bodies(sessions_root, backend):
        seen.add(unit_key)
        body_slots, bad = _read_body_slots(backend, sessions_root, unit_key)
        if bad:
            unreadable += 1
        elif body_slots is not None:
            _offer(body_slots)
    skips: list = []
    carriers = bcc.read_carriers(state_dir, backend, skipped=skips)
    unreadable += len(skips)
    carrier_only = 0
    for unit_key, by_slot in carriers.items():
        if unit_key not in seen:
            carrier_only += 1
            seen.add(unit_key)
        if isinstance(by_slot, dict):
            _offer(by_slot)
    report.update(bodies_scanned=len(seen), carrier_only_bodies=carrier_only,
                  sources_unreadable=unreadable)

    wanted = [g for g in (goal_ids or []) if g]
    located = {g: {} for g in wanted}
    lanes = {}
    live_by_slot = {}
    for s in CAPTURE_SLOTS:
        ring = rings.get(s)
        ring = ring if isinstance(ring, list) else []
        pos: dict = {}
        for i, h in enumerate(ring):
            pos.setdefault(h, i)
        live = slots.get(s)
        live = ([e for e in live if isinstance(e, dict)]
                if isinstance(live, list) else [])
        live_by_slot[s] = live
        live_h = [bmg._content_hash(e) for e in live]
        off = offered[s]
        for g in wanted:
            positions = [pos.get(h) for e, h in zip(live, live_h)
                         if e.get("goal_id") == g]
            hs = [h for h, og in off.items() if og == g]
            if positions or hs:
                located[g][s] = {"live": len(positions),
                                 "live_ring_positions": positions,
                                 "offered": len(hs),
                                 "offered_in_ring": sum(1 for h in hs if h in pos)}
        if not (ring or live or off):
            continue
        live_set = set(live_h)
        missing = [h for h in off if h not in pos]
        tail = sum(1 for h in pos if h not in off)
        saturated = len(ring) >= CONSUMED_HASHES_CEILING
        tail_at_cap = tail >= CONSUMED_HASHES_CAP
        # guard-6824: a ring of EXACTLY CAP is the pre- FIFO shape —
        # a WM written before the fix, or by a box still running pre-fix code
        at_cap = len(ring) == CONSUMED_HASHES_CAP
        consumed = sum(1 for h in live_h if h in pos)
        lanes[s] = {
            "live": len(live),
            "ring": len(ring),
            "offered": len(off),
            "offered_in_ring": len(off) - len(missing),
            # offered, unrecorded, still live: the next pass records these
            "offered_missing_live": sum(1 for h in missing if h in live_set),
            # offered, unrecorded, NOT live: the next pass MERGES these — new
            # arrivals, or drained entries coming back
            "offered_missing_absent": sum(1 for h in missing if h not in live_set),
            "ring_not_offered": tail,
            "saturated": saturated,
            "tail_at_cap": tail_at_cap,
            "at_cap": at_cap,
            "live_consumed": consumed,
            "live_never_consumed": len(live) - consumed,
            # An evicting ring forgets consumed hashes, so never-consumed
            # over-counts (guard-6824): at the ceiling offered hashes go, at
            # CAP the non-offered tail is FIFO-trimmed, and a ring of exactly
            # CAP is the pre-fix FIFO.
            "never_consumed_upper_bound": saturated or tail_at_cap or at_cap,
            # guard-2298: a zero is only evidence beside a proven non-zero
            "positive_control": next(
                ({"goal_id": e.get("goal_id"), "hash": h[:12],
                  "ring_position": pos[h]}
                 for e, h in zip(live, live_h) if h in pos), None),
        }
    report["lanes"] = lanes
    if wanted:
        report["goal_ids"] = located
    if relay_goals is not None:
        report["relay"] = _relay_join(relay_goals, live_by_slot.get(relay_slot, []),
                                      relay_slot, report["wm_as_of"])
    report["last_pass"] = _last_pass(state_dir, backend)
    return report


def format_census(report: dict) -> str:
    """Multi-line form of census(); every count sits beside its population."""
    out = []
    role = "worker Body" if report.get("worker_body") else "not a worker Body"
    out.append(f"[capture-ring-census] agent={report['agent']} "
               f"host={report['hostname']} ({role}) at {report['ts']}; "
               f"agent-wide WM read from {report.get('wm_source')}, written "
               f"{report.get('wm_as_of')}")
    if (report.get("wm_source") or "").startswith("local mirror"):
        out.append("[capture-ring-census] the store copy could not be read, so "
                   "every count below is from this box's local mirror, which "
                   "off the reducer's box can be days stale (guard-980)")
    if not report.get("reducer_wm_present"):
        out.append("[capture-ring-census] UNMEASURABLE — no agent-wide working "
                   "memory at the expected path; every ring and live count "
                   "below is read from an absent file.")
    unread = report.get("sources_unreadable") or 0
    out.append(f"sources: {report.get('bodies_scanned', 0)} Body(s) scanned "
               f"({report.get('carrier_only_bodies', 0)} via carrier only), "
               f"sources_unreadable={unread}"
               + (" — a pass with an unreadable source trims no ring below the "
                  "ceiling" if unread else ""))
    for s, r in sorted((report.get("lanes") or {}).items()):
        flags = []
        if r["saturated"]:
            flags.append(f"SATURATED at ceiling {report['ceiling']}: offered "
                         "hashes are evicted, drained captures come back")
        if r["tail_at_cap"]:
            flags.append(f"SATURATED tail: non-offered hashes at CAP "
                         f"{report['cap']}, FIFO-trimming")
        elif r["at_cap"]:
            flags.append(f"SATURATED at CAP {report['cap']}: the ring is exactly "
                         "CAP, the pre-g-115-10776 FIFO shape (guard-6824)")
        out.append(f"{s}: ring {r['ring']} vs offered {r['offered']} "
                   f"(ceiling {report['ceiling']}) | offered in ring "
                   f"{r['offered_in_ring']}, recorded next pass "
                   f"{r['offered_missing_live']}, NOT in ring and not live "
                   f"{r['offered_missing_absent']} (the next pass merges these: "
                   f"new or re-delivered) | ring-not-offered "
                   f"{r['ring_not_offered']} (cap {report['cap']})"
                   + (" | " + "; ".join(flags) if flags else ""))
        nc = r["live_never_consumed"]
        nc_s = (f"at most {nc} (UPPER BOUND: the ring is evicting)"
                if r["never_consumed_upper_bound"] else str(nc))
        out.append(f"  live split: {r['live_consumed']} consumed / {nc_s} "
                   f"never-consumed of {r['live']} live — source retention, "
                   "not backlog (guard-7413)")
        pc = r.get("positive_control")
        if pc:
            out.append(f"  positive control: live entry {pc['goal_id']} hash "
                       f"{pc['hash']} at ring position {pc['ring_position']} "
                       f"of {r['ring']}")
        elif r["live"] and r["ring"]:
            out.append("  positive control: NONE — no live entry hashes into "
                       "the ring; every live entry is newer than the last "
                       "pass, or the read or hash path is wrong. Treat the split "
                       "as unmeasured.")
    lp = report.get("last_pass")
    if lp:
        su = lp.get("sources_unreadable")
        out.append(f"last fast-lane pass that changed something: {lp.get('ts')} "
                   f"merged={lp.get('merged')} sources_unreadable="
                   f"{'not recorded' if su is None else su} consumed_ring="
                   f"{json.dumps(lp.get('consumed_ring'), sort_keys=True)}")
    else:
        out.append("last fast-lane pass: no telemetry row on this box (a pass "
                   "that changes nothing writes none)")
    for g, per in sorted((report.get("goal_ids") or {}).items()):
        if not per:
            out.append(f"goal {g}: not live and not offered in any capture lane")
        for s, v in sorted(per.items()):
            out.append(f"goal {g} {s}: live {v['live']} (ring positions "
                       f"{v['live_ring_positions'][:10]}), offered "
                       f"{v['offered']} ({v['offered_in_ring']} in ring)")
    rj = report.get("relay")
    if rj:
        out.append(f"relay join ({rj['slot']}): {rj['relay_marked']} "
                   f"relay-marked of {rj['relay_goals_read']} goals read, "
                   f"{rj['relays_after_wm']} filed after the WM was written "
                   "set aside; "
                   f"{rj['live_sources']} live source goal(s), "
                   f"{rj['sources_with_relay']} already relayed, "
                   f"{rj['live_entries_predating_relay']} live entr(ies) "
                   "predate their relay: captured before it, present after its "
                   "drain, so re-delivered or delivered late (at most this "
                   "many re-delivered: _item_ts is capture time, not arrival) "
                   "— archived goals are outside the corpus, so no relay here "
                   "is unproven, not clean (guard-7302)")
        for row in rj["rows"][:15]:
            out.append(f"  {row['source_goal']}: {row['live_entries']} live, "
                       f"{row['predating_relay']} predate its first relay "
                       f"{row['first_relay_at']} "
                       f"({', '.join(row['relay_goals'][:3])})")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT"))
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would merge; write nothing")
    ap.add_argument("--json", action="store_true", help="emit the summary as JSON")
    ap.add_argument("--allow-worker", action="store_true",
                    help=argparse.SUPPRESS)  # tests only; never in production
    ap.add_argument("--census", action="store_true",
                    help="READ-ONLY consumed-ring census (gap-222): ring vs "
                         "offered, bounds, sources_unreadable, live split with "
                         "a positive control; writes nothing, runs on any box. "
                         "rc 3 when there is no agent-wide WM to read")
    ap.add_argument("--goal-ids", default="",
                    help="with --census: comma-separated goal ids to locate in "
                         "the live lanes, the offered sources and the ring")
    ap.add_argument("--relay-goals-file", default=None,
                    help="with --census: JSON list of goal records, '-' for "
                         "stdin, e.g. aspirations-query.sh --description-contains "
                         "'worker Body (spark_capture from' --goal-status "
                         "pending,in-progress,completed,skipped --full; joins "
                         "live source goal ids against relay-marked goals")
    ap.add_argument("--relay-slot", default="spark_capture",
                    help="with --relay-goals-file: the lane to join (default "
                         "spark_capture)")
    args = ap.parse_args(argv)
    if not args.agent:
        print("capture-fast-lane: no agent (set MIND_AGENT or pass --agent)",
              file=sys.stderr)
        return 2
    if args.census:
        relay = None
        if args.relay_goals_file:
            if args.relay_slot not in CAPTURE_SLOTS:
                print(f"capture-fast-lane --census: unknown --relay-slot "
                      f"{args.relay_slot!r}", file=sys.stderr)
                return 2
            try:
                raw = (sys.stdin.read() if args.relay_goals_file == "-" else
                       Path(args.relay_goals_file).read_text(encoding="utf-8"))
                relay = json.loads(raw)
            except (OSError, ValueError) as exc:
                print(f"capture-fast-lane --census: cannot read relay goals: "
                      f"{exc}", file=sys.stderr)
                return 2
            if not isinstance(relay, list):
                print("capture-fast-lane --census: relay goals must be a JSON "
                      "list of goal records", file=sys.stderr)
                return 2
        report = census(args.agent,
                        goal_ids=[g.strip() for g in args.goal_ids.split(",")],
                        relay_goals=relay, relay_slot=args.relay_slot)
        print(json.dumps(report, indent=2, sort_keys=True) if args.json
              else format_census(report))
        return 0 if report.get("reducer_wm_present") else 3
    summary = fast_lane(args.agent, dry_run=args.dry_run,
                        allow_worker=args.allow_worker)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(format_line(summary))
    # Advisory by contract: this pass is a best-effort accelerator sitting in
    # front of a merge that will happen anyway, so it must never fail a caller.
    return 0


if __name__ == "__main__":
    sys.exit(main())
