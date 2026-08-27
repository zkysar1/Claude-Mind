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
CONSUMED_HASHES_CAP = 2000


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
                # Bounded: keep the most recent CONSUMED_HASHES_CAP hashes per
                # slot. The bound must exceed the largest plausible re-offer —
                # 162 entries measured 2026-08-15 (guard-3897) — because eviction
                # here is oldest-first and an evicted hash re-opens this bug for
                # that entry. 2000 is ~12x the observed worst case.
                fresh = [bmg._content_hash(e) for e in merged_list[before:]]
                wm_all[slot_name] = (prior + fresh)[-CONSUMED_HASHES_CAP:]
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
        body_wm_bytes, transient = bmg._read_staged_bytes(
            backend, sessions_root / unit_key / bm._WM_FILENAME)
        if transient or body_wm_bytes is None:
            continue
        try:
            body_wm = yaml.safe_load(body_wm_bytes) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(body_wm, dict):
            continue
        body_slots = body_wm.get("slots")
        if not isinstance(body_slots, dict):
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
    for unit_key, by_slot in sorted(bcc.read_carriers(state_dir, backend).items()):
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


def format_line(summary: dict) -> str:
    """The one-line form for the reducer's existing iteration-close output."""
    if summary.get("role_refused"):
        return "[capture-fast-lane] SKIPPED — worker Body (reducer-only pass)"
    if not summary.get("merged"):
        return ("[capture-fast-lane] 0 load-bearing captures to merge "
                f"({summary.get('bodies_scanned', 0)} Bodies scanned, "
                f"{summary.get('already_present', 0)} already merged)"
                + _ratio_fragment(summary))
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
    return ("[capture-fast-lane] merged "
            f"{summary['merged']} load-bearing capture(s) from "
            f"{summary['bodies_contributing']}/{summary['bodies_scanned']} Bodies "
            f"— median flag-to-merge {med_s}, max "
            f"{summary.get('latency_minutes_max')}m{tail} "
            f"[{', '.join(f'{k}={v}' for k, v in sorted(summary['by_slot'].items()))}]"
            + _ratio_fragment(summary))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT"))
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would merge; write nothing")
    ap.add_argument("--json", action="store_true", help="emit the summary as JSON")
    ap.add_argument("--allow-worker", action="store_true",
                    help=argparse.SUPPRESS)  # tests only; never in production
    args = ap.parse_args(argv)
    if not args.agent:
        print("capture-fast-lane: no agent (set MIND_AGENT or pass --agent)",
              file=sys.stderr)
        return 2
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
