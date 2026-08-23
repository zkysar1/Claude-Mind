#!/usr/bin/env python3
"""scar-tissue-check — the subtractive gradient's CADENCE ().

Part of the evaluative substrate. This is the periodic CALLER that
``complexity_budget.py`` was written for and never got: its module docstring says
it exists to give "the scar-tissue review cadence ... an objective number to move",
but nothing ever invoked it. Measured 2026-08-01: zero callers in
``core/scripts/*.sh``, ``.claude/skills/*/SKILL.md``, ``core/config/*.yaml`` — the
only references were a docstring line in ``eval_harness.py`` (no import, no
subprocess) and a prose mention in ``.claude/rules/learning-philosophy.md``.

WHY THIS EXISTS
---------------
The framework's self-improvement gradient is ADDITIVE-only: every incident yields a
new gate / rule / guardrail enforced by four layers, while the opposing force
(retire / consolidate) is advisory alone. ``learning-philosophy.md`` rule 5 makes
subtraction a first-class learning artifact and names this script as the measuring
instrument. An instrument with no cadence measures nothing, so the ratchet ran
unopposed and unquantified.

THE TWO HALVES (they are different corpora — do not conflate them)
------------------------------------------------------------------
The originating goal cited guardrail/reasoning-bank retirement ratios and assumed
``complexity_budget.py`` reported them. It does not, and cannot: it counts the
framework's FILE surface (gates, rules, skills, scripts, conventions, two line
counts). Both halves are real and both belong to the same "complexity ratchet"
thesis, so this cadence reports them side by side and keeps them clearly labelled:

  half A — FILE surface   : complexity_budget.measure/append_and_delta (trend + verdict)
  half B — STORE corpus   : guardrails.jsonl + reasoning-bank.jsonl active-vs-retired
                            ratio, never-marked-helpful population, a BOUNDED
                            retirement slate, and a SUBSET-PAIR slate (below)

SUBSET PAIRS — the third half-B measurement (g-115-5053)
--------------------------------------------------------
An entry whose text is a strict byte-PREFIX of a sibling's is a fork, not a
duplicate opinion: the shorter one is pre-amendment text re-inserted under a
fresh id while the amended text kept the original id (``_guard_identity`` keys on
``(created, rule)``, so every in-place ``rule`` amendment is a fork candidate).
Both copies stay ACTIVE and both are served — utilization accrues independently
on each — so a retrieval landing on the short member returns the rule MINUS the
extension block someone paid to learn.

This class had been rediscovered by accident three times (g-115-3331, g-115-4065,
g-335-732) before this detector existed. Three discoveries by three accidents
means a fourth accident was the plan.

PROPOSAL ONLY — STRUCTURAL, NOT A FLAG
--------------------------------------
This script has NO ``--apply`` path and imports no mutation helper. It cannot
retire an entry even if invoked incorrectly. The slate is a PROPOSAL for agent
judgment; ``bulk-retire-dead-entries.py --apply`` remains the only mutation path,
run deliberately by an agent that has read the slate. Retirement is a judgment
call about whether a defense is still earning its keep — automating it would
replace one unopposed ratchet with another pointing the other way.

The slate reuses ``_curation_predicate.is_dead_entry`` — the pure seam that
``bulk-retire-dead-entries.py`` and ``memevo_bench.py`` already share — so the
proposal can never drift from what the production retirement tool would select.

SHAPE: SELF-ACTING, OWN PHASE (NOT a cadence-battery entry)
-----------------------------------------------------------
``_cadence_registry.py`` scopes the battery to cadences "whose fire-action is a
single LLM SKILL INVOCATION", and explicitly excludes ``l1-skew`` because it is
SELF-ACTING (posts to the board inside the script, so it cannot starve via the
skill-skip mode). This cadence is the same shape: it measures and posts, and there
is no ``/scar-tissue-review`` skill to invoke. It therefore mirrors l1-skew — its
own precheck phase, ``--cadence`` gating, ``--post-board`` — rather than becoming a
seventh battery entry.

USAGE
    py -3 core/scripts/scar-tissue-check.py                 # measure + report now
    py -3 core/scripts/scar-tissue-check.py --cadence       # cadence-gated (periodic site)
    py -3 core/scripts/scar-tissue-check.py --post-board    # board post when signal
    py -3 core/scripts/scar-tissue-check.py --json          # machine-readable

Exit 0 always when the measurement ran (this is an observability instrument, not a
gate). Exit 2 only when the measurement could NOT run — a silent zero from a broken
instrument must never read as "no complexity growth".
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _paths import CORE_ROOT, PROJECT_ROOT, WORLD_DIR  # noqa: E402

try:
    from _paths import META_DIR  # noqa: E402
except ImportError:  # pragma: no cover — older _paths without META_DIR
    META_DIR = None

# Half A instrument. Underscore filename -> directly importable (no importlib dance).
import complexity_budget as _cb  # noqa: E402

# Half B predicate — the SHARED active-forgetting seam. Importing it (rather than
# re-deriving the criterion) is what guarantees this proposal matches what
# bulk-retire-dead-entries.py --apply would actually select.
from _curation_predicate import is_dead_entry as _is_dead  # noqa: E402
#  reader seam. Reader-only and a behavioural no-op until the writer
# lands: with no sidecar, `utilization_of` returns the embedded field unchanged.
from _utilization_store import (  # noqa: E402
    KINDS as _UTIL_KINDS,
    load_counters as _load_counters,
    utilization_of as _utilization_of,
)

RB_PATH = WORLD_DIR / "reasoning-bank.jsonl"
GUARD_PATH = WORLD_DIR / "guardrails.jsonl"

DEFAULT_LEDGER_REL = "complexity-ledger.jsonl"
DEFAULT_SLATE_CAP = 25
DEFAULT_MIN_RETRIEVALS = 100
DEFAULT_MIN_AGE_DAYS = 30

# Per-store spec for subset-pair detection. The fields differ PER STORE and the
# difference is MEASURED, not stylistic (guard-1902 — sample the schema, do not
# guess it). Field coverage over the live corpus, 2026-08-11:
#
#   guardrails      source 3121/3121   created 3121/3121   rule    3121/3121
#   reasoning-bank  source   74/7086   created 7086/7086   content 7086/7086
#                   source_goal 7086/7086
#
# So keying reasoning-bank on ``source`` — the obvious symmetry — would silently
# exclude 99% of that corpus and report a clean zero. ``source_goal`` is its
# populated analogue.
SUBSET_SPEC = {
    "guardrails": {"group_fields": ("source", "created"), "text_field": "rule"},
    "reasoning-bank": {"group_fields": ("source_goal", "created"),
                       "text_field": "content"},
}

# Printed in the report, NOT only in this comment. A detector that does not
# publish its own blind spot invites its silence to be read as coverage
# (guard-1760); this one sees a deliberately narrow slice.
SUBSET_BLIND_SPOT = (
    "BLIND SPOT: this probe sees ONLY twins that share every grouping field AND "
    "stand in an exact byte-prefix relation. Semantic near-duplicates with "
    "different wording are invisible to it — the four near-duplicate rails found "
    "by hand in g-115-4065 would most likely NOT appear here. A zero is evidence "
    "about byte-prefix forks, not about corpus duplication in general."
)


# ─────────────────────────── half A: file surface ───────────────────────────

def _ledger_path(explicit: Optional[str]):
    """Resolve the complexity ledger path.

    Lives beside the other ``meta/*.jsonl`` telemetry ledgers (gate-firings,
    evolution-log, depth-calibration, ...) because it is domain-agnostic
    improvement telemetry, not domain state. Returns None when meta is
    unresolvable — half A then reports metrics without a trend rather than
    inventing a path.
    """
    if explicit:
        return Path(explicit)
    if META_DIR is None:
        return None
    return Path(META_DIR) / DEFAULT_LEDGER_REL


def measure_file_surface(ledger, timestamp=None) -> dict:
    """Half A: count the framework's file surface and diff against the ledger."""
    metrics = _cb.measure(PROJECT_ROOT)
    if ledger is None:
        return {"metrics": metrics, "deltas": {}, "transitions": {},
                "verdict": "no-ledger", "had_previous": False,
                "ledger": None}
    out = _cb.append_and_delta(metrics, ledger, timestamp=timestamp)
    # append_and_delta nests the counts under `row`, not at top level. Lift them
    # so BOTH branches of this function return the same shape — without this the
    # ledger branch silently reports an empty metrics line while still printing a
    # verdict, which reads as a successful measurement of nothing.
    out["metrics"] = out.get("row", {}).get("metrics", metrics)
    out["ledger"] = str(ledger)
    return out


# ─────────────────────────── half B: store corpus ───────────────────────────

def _read_jsonl(path) -> List[dict]:
    """Read a JSONL store, skipping unparseable lines.

    Fail-soft per line (a corrupt row must not blind the whole measurement) but
    the CALLER distinguishes 'file missing' from 'file empty' — see corpus_stats.
    """
    recs: List[dict] = []
    p = Path(path)
    if not p.exists():
        return recs
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return recs


def _never_helpful(rec: dict, counters=None) -> bool:
    """True when an entry has NO helpful signal of any kind.

    Counts the same three attestation channels the utilization system uses
    (explicit helpful, citation, inferred-helpful backstop) so this population
    matches the retirement predicate's notion of 'produced no value' rather than
    a narrower explicit-only read that would overstate the problem.

    ``counters`` is the g-358-05 sidecar map (id -> counters). It defaults to
    None so any caller that has not been converted keeps today's exact
    behaviour — with no counters the read falls through to the embedded field.
    Passing it MATTERS here specifically: once the writer lands, the embedded
    field is a frozen pre-split snapshot, so reading it would report entries as
    never-helpful on stale counts and propose live entries for retirement.
    """
    u = _utilization_of(rec, counters)
    return (int(u.get("times_helpful", 0) or 0) == 0
            and int(u.get("times_cited", 0) or 0) == 0
            and int(u.get("times_inferred_helpful", 0) or 0) == 0)


def subset_pairs(active: List[dict], group_fields, text_field, cap,
                 counters=None) -> dict:
    """Find ACTIVE entries whose text is a strict byte-prefix of a sibling's.

    Siblings = same value in every ``group_fields`` entry. Reported separately
    from EXACT duplicates: a prefix pair means one member is missing an extension
    block, while an exact pair means two ids carry the identical rule. The
    remedies differ, so collapsing them would hide which one you have.

    NOT a retirement decision. This returns both ids and their utilisation and
    stops there — which member is stale is a judgment that needs the pair read
    (``amended_fields`` is usually the discriminator, but not by construction).

    ``ungroupable`` is REPORTED, never silently dropped: an entry missing a
    grouping field or its text cannot be compared, and a probe that excludes rows
    without saying so reports a clean number over a corpus it did not read
    (measured: 520 of 7014 active reasoning-bank rows are ungroupable here).
    Grouping on a missing field would be worse — every such row would collide
    into one bucket and manufacture pairs that share nothing.
    """
    groups: Dict[tuple, List[dict]] = {}
    ungroupable = 0
    for r in active:
        key = tuple(r.get(f) for f in group_fields)
        if any(v in (None, "") for v in key) or not (r.get(text_field) or "").strip():
            ungroupable += 1
            continue
        groups.setdefault(key, []).append(r)

    def _u(rec, field):
        return int(_utilization_of(rec, counters).get(field, 0) or 0)

    def _row(sub, sup):
        return {"subset_id": sub.get("id"), "superset_id": sup.get("id"),
                "subset_chars": len(sub.get(text_field) or ""),
                "superset_chars": len(sup.get(text_field) or ""),
                "group": {f: sub.get(f) for f in group_fields},
                "subset_times_active": _u(sub, "times_active"),
                "superset_times_active": _u(sup, "times_active"),
                # The usual discriminator, surfaced so the reader does not have to
                # re-open both records to form a first opinion.
                "subset_amended": bool(sub.get("amended_fields")),
                "superset_amended": bool(sup.get("amended_fields"))}

    pairs: List[dict] = []
    exact: List[dict] = []
    multi = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        multi += 1
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                ta = a.get(text_field) or ""
                tb = b.get(text_field) or ""
                if ta == tb:
                    exact.append({"ids": sorted([a.get("id"), b.get("id")]),
                                  "chars": len(ta),
                                  "group": {f: a.get(f) for f in group_fields}})
                elif tb.startswith(ta):
                    pairs.append(_row(a, b))
                elif ta.startswith(tb):
                    pairs.append(_row(b, a))

    # Widest gap first: the pair whose short member is missing the most text is
    # where a retrieval landing on the wrong copy loses the most.
    pairs.sort(key=lambda p: p["superset_chars"] - p["subset_chars"], reverse=True)
    return {"pairs": pairs[:cap], "pairs_total": len(pairs),
            "pairs_truncated": len(pairs) > len(pairs[:cap]),
            "exact_duplicates": exact[:cap], "exact_total": len(exact),
            "groups": len(groups), "multi_member_groups": multi,
            "ungroupable": ungroupable,
            "group_fields": list(group_fields), "text_field": text_field,
            "blind_spot": SUBSET_BLIND_SPOT}


def corpus_stats(path, label, today, min_retrievals, min_age_days,
                 slate_cap) -> dict:
    """Half B for ONE store: ratio, never-helpful population, bounded slate."""
    p = Path(path)
    if not p.exists():
        # rb-245: a zero-count claim against a store that is not there is not a
        # measurement of zero. Report the gap explicitly.
        return {"store": label, "present": False, "path": str(p),
                "total": None, "active": None, "retired": None,
                "retire_ratio": None, "never_helpful": None,
                "never_helpful_pct": None, "slate": [], "slate_total": 0,
                "slate_truncated": False, "subset_pairs": None}

    recs = _read_jsonl(p)
    # : this store's counter sidecar, read ONCE for every join below.
    # `label` is already exactly a KIND ("guardrails" / "reasoning-bank"), so the
    # precise per-kind loader applies rather than the merged one. Guarded on
    # membership because `_check_kind` RAISES on an unknown kind: a future third
    # store added to SUBSET_SPEC but not to KINDS must degrade to today's
    # embedded-field read, never take down the whole check.
    counters = _load_counters(label) if label in _UTIL_KINDS else {}
    total = len(recs)
    active = [r for r in recs if r.get("status") == "active"]
    retired = [r for r in recs if r.get("status") == "retired"]
    nh = [r for r in active if _never_helpful(r, counters)]

    candidates = []
    for r in active:
        try:
            if _is_dead(r, today=today, min_retrievals=min_retrievals,
                        min_age_days=min_age_days, counters=counters):
                candidates.append(r)
        except Exception:
            # A predicate error on ONE record must not zero the whole slate.
            continue

    # Sort the proposal so the highest-volume/lowest-value entries surface first —
    # a bounded slate should spend its budget where the carrying cost is greatest.
    candidates.sort(
        key=lambda r: int(_utilization_of(r, counters).get("retrieval_count", 0) or 0),
        reverse=True)

    slate_total = len(candidates)
    shown = candidates[:slate_cap]
    slate = [{"id": r.get("id"),
              "title": (r.get("title") or r.get("rule") or "")[:80],
              "created": r.get("created"),
              "retrieval_count": int(_utilization_of(r, counters).get("retrieval_count", 0) or 0)}
             for r in shown]

    spec = SUBSET_SPEC.get(label)
    subsets = (subset_pairs(active, spec["group_fields"], spec["text_field"],
                            slate_cap, counters) if spec else None)

    return {
        "store": label,
        "present": True,
        "path": str(p),
        "subset_pairs": subsets,
        "total": total,
        "active": len(active),
        "retired": len(retired),
        # active-per-retired. None when nothing is retired yet — an infinite
        # ratio is the finding, and printing a fake number would hide it.
        "retire_ratio": (round(len(active) / len(retired), 1) if retired else None),
        "never_helpful": len(nh),
        "never_helpful_pct": (round(100.0 * len(nh) / len(active), 1) if active else None),
        "slate": slate,
        "slate_total": slate_total,
        "slate_truncated": slate_total > len(slate),
    }


# ─────────────────────────── cadence gate (mirrors l1-skew) ───────────────────────────

def _load_cadence_config() -> dict:
    """Load the ``scar_tissue_check`` block from aspirations.yaml (with defaults).

    Reads the dead-entry criterion keys too, not just the cadence keys. A config
    block whose values are silently ignored is worse than no block at all: it
    advertises a tuning knob that does nothing, so an operator who lowers
    ``min_retrievals`` to widen the slate sees no change and concludes the corpus
    is clean.
    """
    cfg = {"goal_cadence": 100, "wm_slot": "last_scar_tissue_check",
           "min_retrievals": DEFAULT_MIN_RETRIEVALS,
           "min_age_days": DEFAULT_MIN_AGE_DAYS,
           "slate_cap": DEFAULT_SLATE_CAP}
    try:
        import yaml
        cfg_path = Path(PROJECT_ROOT) / "core" / "config" / "aspirations.yaml"
        if not cfg_path.exists():
            return cfg
        with open(str(cfg_path), "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        block = loaded.get("scar_tissue_check") or {}
        cfg["goal_cadence"] = int(block.get("goal_cadence", cfg["goal_cadence"]))
        cfg["wm_slot"] = str(block.get("wm_slot", cfg["wm_slot"]))
        for key in ("min_retrievals", "min_age_days", "slate_cap"):
            cfg[key] = int(block.get(key, cfg[key]))
    except Exception as e:
        print("[scar-tissue-check] cadence config read failed: " + str(e),
              file=sys.stderr)
    return cfg


def _count_completed_goals() -> int:
    """Total completed goals. Reuses fresh-eyes-cadence-check's helper so every
    cadence ritual shares one definition of 'completed'.

    Returns 0 on EVERY failure path — see the zero-guard in _cadence_gate, which
    exists precisely because this sentinel is indistinguishable from a real zero.
    """
    try:
        script = str(Path(CORE_ROOT) / "scripts" / "fresh-eyes-cadence-check.py")
        result = subprocess.run(
            [sys.executable, script, "--print-current"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return 0
        return int(result.stdout.strip() or 0)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return 0


def _wm_read(slot):
    try:
        import _rt
        raw = (_rt.wm_read(slot=slot, as_json=True) or "").strip()
        if not raw or raw == "null":
            return None
        return json.loads(raw)
    except Exception:
        return None


def _wm_set(slot, value):
    try:
        wm_script = str(Path(CORE_ROOT) / "scripts" / "wm.py")
        subprocess.run([sys.executable, wm_script, "set", slot],
                       input=json.dumps(value), capture_output=True,
                       text=True, check=True, timeout=10)
    except Exception as e:
        print("[scar-tissue-check] wm-set failed: " + str(e), file=sys.stderr)


def _cadence_gate():
    """Return (fire, current, cfg, last). fire=True when the cadence crossed.

    Carries the two load-bearing guards the sibling cadence scripts learned the
    hard way; the rest of l1-skew's gate (share baselines, dominance) is specific
    to that check and deliberately not copied.

      first-fire normalization (g-001-190) — an unset slot must not fire on the
      full historical goal count; cap the diff at one cadence so the ritual reads
      as 'due now', not 'overdue by thousands'.

      zero-guard (guard-1091) — _count_completed_goals returns 0 as a SILENT
      FAILURE SENTINEL on four distinct error paths. Re-baselining on it would
      persist a transient failure as the new basis and then spuriously fire. Noop
      WITHOUT re-stamping so the next check retries; a real count never falls to
      zero.
    """
    cfg = _load_cadence_config()
    current = _count_completed_goals()
    last = _wm_read(cfg["wm_slot"])

    if not isinstance(last, dict):
        # First fire (or a pre-migration scalar shape): due now, not overdue.
        diff = min(current, cfg["goal_cadence"])
        return diff >= cfg["goal_cadence"], current, cfg, None

    last_count = int(last.get("goals_count_at_last_fire", 0) or 0)
    diff = current - last_count

    if last_count == 0:
        diff = min(diff, cfg["goal_cadence"])

    if diff < 0:
        if current == 0:
            print(f"[scar-tissue-check] negative diff ({diff}) with current=0 vs "
                  f"last={last_count} — FAILED MEASUREMENT, not a real basis; "
                  f"noop WITHOUT re-stamp — retries next check", file=sys.stderr)
            return False, current, cfg, last
        _wm_set(cfg["wm_slot"], {
            "timestamp": last.get("timestamp", "0000-00-00T00:00:00"),
            "goals_count_at_last_fire": current,
            "rebaselined_from": last_count})
        print(f"[scar-tissue-check] negative diff ({diff}) — count basis moved "
              f"backward (last={last_count} > current={current}); re-baselined "
              f"to {current} — noop this iter")
        return False, current, cfg, last

    return diff >= cfg["goal_cadence"], current, cfg, last


def _stamp(cfg, current):
    _wm_set(cfg["wm_slot"], {
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "goals_count_at_last_fire": current})


# ─────────────────────────── reporting ───────────────────────────

def has_signal(result: dict) -> bool:
    """True when the report is worth a board post.

    Signal = the file surface GREW, or either store has a non-empty retirement
    slate. A flat surface with nothing to retire is a clean bill of health and
    should stay quiet — an instrument that posts every fire trains its readers to
    skip it.
    """
    if result["file_surface"].get("verdict") in ("growing", "mixed"):
        return True
    if any(s.get("slate_total", 0) > 0 for s in result["stores"]):
        return True
    # A subset pair is signal on its own: it means a live retrieval can return
    # pre-amendment text, which no other measurement on this page would surface.
    for s in result["stores"]:
        sp = s.get("subset_pairs") or {}
        if sp.get("pairs_total", 0) or sp.get("exact_total", 0):
            return True
    return False


def render(result: dict) -> str:
    fs = result["file_surface"]
    lines = ["═══ SCAR-TISSUE CHECK — the subtractive gradient ═══", ""]

    lines.append("── half A: FILE surface (complexity_budget) ──")
    lines.append(f"verdict: {fs.get('verdict')}")
    m = fs.get("metrics", {})
    lines.append("  " + "  ".join(f"{k}={v}" for k, v in sorted(m.items())))
    deltas = {k: v for k, v in (fs.get("deltas") or {}).items() if v}
    if deltas:
        lines.append("  delta since last fire: "
                     + "  ".join(f"{k}{v:+d}" for k, v in sorted(deltas.items())))
    elif fs.get("had_previous"):
        lines.append("  delta since last fire: none")
    else:
        lines.append("  (baseline row — no previous measurement to diff)")
    if fs.get("transitions"):
        lines.append(f"  ⚠ transitions (file appeared/disappeared): {fs['transitions']}")
    lines.append("")

    lines.append("── half B: STORE corpus (add-to-retire + never-helpful) ──")
    for s in result["stores"]:
        if not s.get("present"):
            lines.append(f"{s['store']}: STORE NOT FOUND at {s['path']} — "
                         f"not a zero measurement, a missing one")
            continue
        ratio = ("no entries retired yet (ratio undefined — that IS the finding)"
                 if s["retire_ratio"] is None else f"{s['retire_ratio']}:1 active:retired")
        lines.append(f"{s['store']}: total={s['total']} active={s['active']} "
                     f"retired={s['retired']} — {ratio}")
        lines.append(f"  never marked helpful: {s['never_helpful']}/{s['active']}"
                     + (f" ({s['never_helpful_pct']}%)" if s["never_helpful_pct"] is not None else ""))
        if s["slate_total"]:
            shown = len(s["slate"])
            trunc = f" (showing {shown} of {s['slate_total']})" if s["slate_truncated"] else ""
            lines.append(f"  PROPOSED retirement slate: {s['slate_total']} entries{trunc}")
            for e in s["slate"]:
                lines.append(f"    {e['id']}  rc={e['retrieval_count']}  {e['title']}")
        else:
            lines.append("  PROPOSED retirement slate: none "
                         "(no active entry meets the dead-entry criterion)")

        sp = s.get("subset_pairs")
        if sp is None:
            lines.append("  subset-pair scan: NOT RUN for this store "
                         "(no entry in SUBSET_SPEC) — not a zero, an absence")
        else:
            # Always print the numbers, including the zeros. "0 pairs over 3032
            # groups" is a measurement; a missing line is indistinguishable from a
            # probe that never ran (the check this goal asked for by name).
            lines.append(
                f"  subset-pair scan: {sp['pairs_total']} prefix pair(s), "
                f"{sp['exact_total']} exact duplicate(s) over "
                f"{sp['multi_member_groups']} multi-member group(s) of "
                f"{sp['groups']}; {sp['ungroupable']} entries ungroupable"
                f" (key={'+'.join(sp['group_fields'])}, text={sp['text_field']})")
            shown = sp["pairs"]
            if shown:
                trunc = (f" (showing {len(shown)} of {sp['pairs_total']})"
                         if sp["pairs_truncated"] else "")
                lines.append(f"    prefix pairs — subset is missing text the "
                             f"superset carries{trunc}:")
                for p in shown:
                    gap = p["superset_chars"] - p["subset_chars"]
                    amend = ("superset amended" if p["superset_amended"]
                             and not p["subset_amended"] else
                             "both amended" if p["superset_amended"]
                             and p["subset_amended"] else
                             "subset amended" if p["subset_amended"] else
                             "neither amended")
                    lines.append(
                        f"      {p['subset_id']} ⊂ {p['superset_id']}  "
                        f"-{gap} chars  active {p['subset_times_active']}"
                        f"/{p['superset_times_active']}  ({amend})")
            if sp["exact_duplicates"]:
                lines.append("    exact duplicates — two ids, byte-identical text:")
                for e in sp["exact_duplicates"]:
                    lines.append(
                        f"      {' == '.join(str(i) for i in e['ids'])}"
                        f"  ({e['chars']} chars)")
            lines.append("    " + sp["blind_spot"])
    lines.append("")
    lines.append("PROPOSAL ONLY — this script cannot retire anything (no --apply path).")
    lines.append("To act on a slate, an agent runs:")
    lines.append("  py -3 core/scripts/bulk-retire-dead-entries.py --store <rb|guard> "
                 "[--apply]")
    lines.append("Retirement is reversible: update-field <id> status active.")
    return "\n".join(lines)


def _post_board(body: str) -> None:
    """Post the report to the findings board.

    Routes argv[0] through ``bash_cmd`` rather than a bare ``"bash"`` literal:
    on Windows a bare ``bash`` resolves via CreateProcess to the System32 WSL
    launcher and can hang forever (guard-580), and ``str(WindowsPath)`` would
    have its backslashes stripped by bash (guard-581). Both are invisible on
    Linux, which is exactly why the pre-commit gate — not testing — caught this.
    """
    try:
        from _runtime_bash import bash_cmd
        script = Path(CORE_ROOT) / "scripts" / "board-post.sh"
        subprocess.run(
            bash_cmd(script, "--channel", "findings", "--type", "finding",
                     "--tags", "scar-tissue,complexity-budget,subtractive-gradient"),
            input=body, cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            timeout=30)
    except Exception as e:
        print("[scar-tissue-check] board post failed: " + str(e), file=sys.stderr)


def run(args) -> dict:
    today = date.today()
    ledger = _ledger_path(args.ledger)
    fs = measure_file_surface(ledger)
    stores = [
        corpus_stats(GUARD_PATH, "guardrails", today, args.min_retrievals,
                     args.min_age_days, args.slate_cap),
        corpus_stats(RB_PATH, "reasoning-bank", today, args.min_retrievals,
                     args.min_age_days, args.slate_cap),
    ]
    return {"file_surface": fs, "stores": stores,
            "criterion": {"min_retrievals": args.min_retrievals,
                          "min_age_days": args.min_age_days,
                          "slate_cap": args.slate_cap}}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Scar-tissue check — measure the complexity ratchet and "
                    "propose (never apply) a bounded retirement slate.")
    ap.add_argument("--cadence", action="store_true",
                    help="cadence-gated mode for periodic callers: run only when "
                         "the goal cadence has crossed, then stamp the WM slot")
    ap.add_argument("--post-board", action="store_true",
                    help="post a findings-channel board message when there is signal")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--ledger", default=None,
                    help="complexity ledger path (default: meta/complexity-ledger.jsonl)")
    # default=None so an unpassed flag is distinguishable from a passed one that
    # happens to equal the default — that distinction is what lets config win
    # over the module default while CLI still wins over config.
    ap.add_argument("--slate-cap", type=int, default=None,
                    help=f"max slate entries to list per store "
                         f"(config scar_tissue_check.slate_cap, else {DEFAULT_SLATE_CAP})")
    ap.add_argument("--min-retrievals", type=int, default=None,
                    help=f"dead-entry criterion "
                         f"(config scar_tissue_check.min_retrievals, else {DEFAULT_MIN_RETRIEVALS})")
    ap.add_argument("--min-age-days", type=int, default=None,
                    help=f"dead-entry criterion "
                         f"(config scar_tissue_check.min_age_days, else {DEFAULT_MIN_AGE_DAYS})")
    args = ap.parse_args(argv)

    cfg = current = None
    if args.cadence:
        fire, current, cfg, _last = _cadence_gate()
        if not fire:
            if args.json:
                print(json.dumps({"fired": False, "current": current,
                                  "goal_cadence": cfg["goal_cadence"]}))
            return 0
    if cfg is None:
        cfg = _load_cadence_config()

    # Precedence: explicit CLI flag > aspirations.yaml block > module default.
    for key in ("min_retrievals", "min_age_days", "slate_cap"):
        if getattr(args, key) is None:
            setattr(args, key, cfg[key])

    try:
        result = run(args)
    except Exception as e:
        # An instrument that fails silently manufactures the confidence it should
        # withhold: "no growth reported" would be indistinguishable from a crash.
        print("[scar-tissue-check] MEASUREMENT FAILED: " + str(e), file=sys.stderr)
        return 2

    if args.cadence and cfg is not None:
        _stamp(cfg, current)

    body = render(result)
    if args.json:
        result["fired"] = True
        result["has_signal"] = has_signal(result)
        print(json.dumps(result, indent=2))
    else:
        print(body)

    if args.post_board and has_signal(result):
        _post_board(body)

    return 0


if __name__ == "__main__":
    sys.exit(main())
