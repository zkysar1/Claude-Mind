#!/usr/bin/env python3
"""Scoring-Criterion Field Audit — Phase 1 of the scoring health plan.

Reads `core/config/scoring-criteria.yaml` + the goal corpus (world and/or
agent aspiration stores) and produces per-field recommendations:
  dead_field | degenerate_field | sparse_below_floor | sparse_by_design |
  source_skew | healthy | insufficient_data | unmapped

Recommender, not judge. Never edits goal-selector.py or weights — output is
JSON / human table only on stdout. Persistence is the caller's job: pipe
`> meta/scoring-criterion-audit.jsonl` if you want a diffable history. The
script intentionally does NOT write a log itself, because concurrent
`--all-agents` runs would race on a multi-KB single-line append (POSIX
atomic-write guarantee is only < PIPE_BUF=4096 bytes; Windows has none).

Sister to `gate-retirement-eval.py`: gates evaluator scores Boolean decision
points against runtime firings; this scores numeric scoring criteria against
input-field coverage in the source corpus. Same recommender pattern, same
self-test discipline, different signal source.

Usage:
  py -3 core/scripts/scoring-criterion-audit.py
        [--goals-source world|agent|both]      (default: both)
        [--agent-name NAME]                    (default: $MIND_AGENT)
        [--all-agents]                         (scan every <agent>/aspirations.jsonl)
        [--min-goals K]                        (insufficient_data threshold; default 50)
        [--min-aspirations K]                  (asp-level threshold; default 5)
        [--criterion ID]                       (restrict to one criterion)
        [--include-sample-values | --no-sample-values]   (default include)
        [--output json|human]                  (default json)
        [--self-test]                          (synthetic regression tests)

Recommendation rules
--------------------
For a (criterion, field, source) row:

  unmapped            criterion exists in goal-selection-strategy.yaml weights
                      but is not in scoring-criteria.yaml criteria[] OR
                      pending_audit[]. Drift detector — emitted at the
                      criterion level, not per-field.

  insufficient_data   total scanned for this source < min threshold
                      (min_goals for goal-level, min_aspirations for asp-level).

  dead_field          non_null_count == 0 AND total >= min
                      AND criterion is NOT sparse_by_design.
                      → No record in the corpus has this field. Reader path
                        is unreachable.

  degenerate_field    distinct_count < expected_distinct_min
                      AND role == discriminator AND total >= min
                      AND non_null_count > 0.
                      → Field exists but takes too few distinct values to
                        differentiate. (e.g. handoff_to=alpha on every goal.)

  sparse_below_floor  non_null_count / total < discriminability_floor
                      AND criterion is NOT sparse_by_design
                      AND non_null_count > 0
                      AND total >= min.
                      → Field present but coverage is below the configured
                        signal floor.

  sparse_by_design    criterion.sparse_by_design == true.
                      → Reported for visibility, not flagged as broken.

  healthy             None of above; field coverage and distinct values
                      meet floors.

For source_skew (only when scanning >= 2 sources):

  source_skew         (max non_null_pct − min non_null_pct) > SKEW_THRESHOLD
                      AND max(totals) >= min
                      AND at least one source has non_null_count > 0.
                      → Likely a missing writer in the lower-coverage source.
                      → CAVEAT: pairwise on (min,max) only; with N >= 3
                        sources an intermediate skew can be mislabeled.
                        Ecosystem max today is world+alpha+bravo, so this is
                        a documented approximation, not fixed.

Sample values
-------------
When --include-sample-values is set (the default), every (criterion, field,
source) row carries a `sample_values` array: top-3 most-frequent non-null
values by count, ties broken by string sort. Uses the same canonical JSON
form as the cardinality counter — lists/dicts render as `["a","b"]` /
`{"k":"v"}` (NOT placeholders), so the reader can see what's actually in the
field. Surrounding double-quotes are stripped from string values at display
time only. Each value is truncated to MAX_VALUE_CHARS_PER (30) chars; the
list stops once cumulative length crosses MAX_VALUE_CHARS_TOTAL (80).

Contract
--------
Bad goal records skip with stderr WARN. Missing `scoring-criteria.yaml` is
fatal (exit 2) — the manifest IS the input. Missing aspiration store for a
source is fail-open (zero records from that source, row → insufficient_data).

Exit codes:
  0  recommendations emitted (regardless of severity)
  1  --self-test failed at least one case
  2  fatal config / IO error before any recommendation
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

from _paths import (  # noqa: E402
    META_DIR, CONFIG_DIR, WORLD_DIR, PROJECT_ROOT,
    agent_dir as _agent_dir, agents_root as _agents_root,
)

CRITERIA_YAML = CONFIG_DIR / "scoring-criteria.yaml"
WEIGHTS_YAML = META_DIR / "goal-selection-strategy.yaml"

SKEW_THRESHOLD = 0.50          # |high_pct - low_pct| above this → source_skew
DEFAULT_MIN_GOALS = 50
DEFAULT_MIN_ASPIRATIONS = 5    # aspiration-level rows are inherently coarser
MAX_DISTINCT_TRACK = 100       # cap distinct-value set growth per field
MAX_VALUE_CHARS_PER = 30
MAX_VALUE_CHARS_TOTAL = 80
SAMPLE_VALUES_TOP_N = 3
MAX_DISTINCT_KEY_LEN = 200     # cap canonical key string length to bound memory


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------

def _load_criteria_yaml():
    """Load scoring-criteria.yaml. Fail CLOSED — the manifest is the input.

    Returns (criteria_by_id, pending_audit_ids).
    """
    if not CRITERIA_YAML.is_file():
        print(f"[scoring-criterion-audit] FATAL: {CRITERIA_YAML} not found.",
              file=sys.stderr)
        sys.exit(2)
    try:
        data = yaml.safe_load(CRITERIA_YAML.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"[scoring-criterion-audit] FATAL: {CRITERIA_YAML} parse "
              f"error: {e}", file=sys.stderr)
        sys.exit(2)
    criteria = {c["id"]: c for c in (data.get("criteria") or []) if c.get("id")}
    pending = {p["id"] for p in (data.get("pending_audit") or []) if p.get("id")}
    return criteria, pending


def _load_weights():
    """Load goal-selection-strategy.yaml weights. Fail OPEN (returns {}).

    Weights are a CROSS-CHECK input for the unmapped detector, not the
    primary input. Missing weights → no unmapped check fires; the rest of
    the audit still produces useful output. (Different signal class than
    _load_criteria_yaml, which fails closed because it's the input.)
    """
    if not WEIGHTS_YAML.is_file():
        return {}
    try:
        data = yaml.safe_load(WEIGHTS_YAML.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data.get("weights", {}) or {}


def _iter_jsonl_records(path):
    """Yield parsed records from a JSONL file. Skip unparseable lines (WARN).

    Returns nothing if the file is missing — caller treats absence as zero
    records (fail-open, same shape as gate-retirement-eval._load_firings).
    """
    if not path.is_file():
        return
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            skipped += 1
    if skipped:
        print(f"[scoring-criterion-audit] WARN: skipped {skipped} unparseable "
              f"line(s) in {path}", file=sys.stderr)


def _resolve_sources(args):
    """Resolve --goals-source / --agent-name / --all-agents into a list of
    (source_label, jsonl_path) tuples.

    Two sources of truth, no fallback chain: explicit `--agent-name` flag
    OR `$MIND_AGENT` env (set by the PreToolUse[Bash] hook). If neither
    resolves AND --goals-source includes 'agent', the agent slot drops
    silently — caller sees world-only output, which is the correct fail-
    open. With --goals-source=agent AND no resolution, main() exits FATAL
    (the user explicitly asked for the agent source and provided no name).
    """
    sources = []
    want_world = args.goals_source in ("world", "both")
    want_agent = args.goals_source in ("agent", "both")

    if want_world:
        sources.append(("world", WORLD_DIR / "aspirations.jsonl"))

    if want_agent:
        if args.all_agents:
            # An agent dir has two markers: aspirations.jsonl AND
            # local-paths.conf. The conf filter excludes any spurious dirs.
            for entry in sorted(_agents_root().iterdir()):
                if not entry.is_dir():
                    continue
                asp = entry / "aspirations.jsonl"
                conf = entry / "local-paths.conf"
                if asp.is_file() and conf.is_file():
                    sources.append((entry.name, asp))
        else:
            agent_name = args.agent_name or os.environ.get("MIND_AGENT")
            if agent_name:
                sources.append(
                    (agent_name, _agent_dir(agent_name) / "aspirations.jsonl")
                )
            # else: agent slot drops silently. World-only output is the
            # correct fail-open — see docstring.
    return sources


# --------------------------------------------------------------------------
# Field probing
# --------------------------------------------------------------------------

def _get_dotted(record, dotted):
    """Walk dotted path through dicts. Present-but-null returns missing.

    Mirrors `jsonl-field-probe._get_dotted` AND `audit-schema-gate._get_dotted`
    semantics — a field whose terminal value is None counts as ABSENT
    (rb-245: "key present but never written" is operationally identical
    to "key not in schema"). DO NOT change this — three call sites depend
    on it.
    """
    cur = record
    for seg in dotted.split("."):
        if not isinstance(cur, dict):
            return False, None
        if seg not in cur:
            return False, None
        cur = cur[seg]
    if cur is None:
        return False, None
    return True, cur


# CRITICAL: this function is the cardinality keyer. DO NOT also use it for
# display formatting — keep the two concerns separate. If you collapse
# lists/dicts to placeholders here for display, you destroy cardinality (every
# list value collides into one bucket). The single-canonical-key approach
# below correctly handles both jobs because the truncated JSON form is also
# human-readable; the small string-quote-strip in `_display_value` is the
# only display-side affordance and explicitly does NOT alter cardinality.
def _canonical_key(value):
    """Stable string for cardinality counting. Preserves distinct values."""
    try:
        s = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        s = repr(value)
    if len(s) > MAX_DISTINCT_KEY_LEN:
        s = s[:MAX_DISTINCT_KEY_LEN] + "…"
    return s


def _display_value(canonical):
    """Convert canonical key to a short human label. Truncate; strip outer
    double-quotes for string values so `"alpha"` reads as `alpha`."""
    s = canonical
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    if len(s) > MAX_VALUE_CHARS_PER:
        s = s[:MAX_VALUE_CHARS_PER - 1] + "…"
    return s


def _scan_source(jsonl_path, criteria_by_id):
    """Scan one aspirations JSONL store; return per-field counters.

    CRITICAL: two passes per aspiration — DO NOT merge them. Asp-level
    fields probed once per asp; goal-level once per goal. Merging would
    count asp.priority once per child goal but the denominator is
    total_aspirations → percentages > 100% (Bug B from session-56 build).

    Returns:
        {
          "total_goals": int,
          "total_aspirations": int,
          "fields": {
            (criterion_id, field_path, source): {
              "non_null_count": int,
              "distinct": Counter[canonical_json_key → int],
            }
          }
        }

    Distinct tracking caps at MAX_DISTINCT_TRACK new keys per field; after
    the cap, hits to existing keys still increment, but new values are
    silently ignored. `distinct_capped: true` surfaces this in evidence so
    the reader knows distinct_count is a lower bound.
    """
    # Pre-bin fields by source kind so each pass only touches its own.
    goal_fields = []   # list of (criterion, field_meta)
    asp_fields = []
    for crit in criteria_by_id.values():
        for field in (crit.get("fields_read") or []):
            if not field.get("path"):
                continue
            src = field.get("source", "goal")
            (asp_fields if src == "aspiration" else goal_fields).append(
                (crit, field)
            )

    fields_state = defaultdict(
        lambda: {"non_null_count": 0, "distinct": Counter()}
    )

    def _record_value(state, value):
        state["non_null_count"] += 1
        key = _canonical_key(value)
        if key in state["distinct"]:
            state["distinct"][key] += 1
        elif len(state["distinct"]) < MAX_DISTINCT_TRACK:
            state["distinct"][key] = 1
        # else: cap reached, new value ignored (distinct_capped surfaces it)

    total_goals = 0
    total_aspirations = 0

    for asp in _iter_jsonl_records(jsonl_path):
        total_aspirations += 1

        # Pass 1: aspiration-level fields, once per asp.
        for crit, field in asp_fields:
            state = fields_state[(crit["id"], field["path"], "aspiration")]
            present, value = _get_dotted(asp, field["path"])
            if present:
                _record_value(state, value)

        # Pass 2: goal-level fields, once per goal.
        for goal in (asp.get("goals") or []):
            total_goals += 1
            for crit, field in goal_fields:
                state = fields_state[(crit["id"], field["path"], "goal")]
                present, value = _get_dotted(goal, field["path"])
                if present:
                    _record_value(state, value)

    return {
        "total_goals": total_goals,
        "total_aspirations": total_aspirations,
        "fields": dict(fields_state),
    }


def _top_sample_values(state, include):
    """Top-N display values by count, with cumulative-length cap.

    Reads canonical-JSON keys from `state["distinct"]` and renders each
    through `_display_value`. Returns list of {"value": str, "count": int}.
    """
    if not include:
        return []
    distinct = state.get("distinct") or {}
    if not distinct:
        return []
    # Sort by (count desc, key asc) for stable output across runs.
    ranked = sorted(distinct.items(), key=lambda kv: (-kv[1], kv[0]))
    out = []
    cum_chars = 0
    for canonical_key, count in ranked:
        if len(out) >= SAMPLE_VALUES_TOP_N:
            break
        display = _display_value(canonical_key)
        if cum_chars + len(display) > MAX_VALUE_CHARS_TOTAL and out:
            break
        out.append({"value": display, "count": count})
        cum_chars += len(display)
    return out


# --------------------------------------------------------------------------
# Recommendation engine
# --------------------------------------------------------------------------

# CRITICAL: `min_total` is threaded by the caller per source kind
# (min_goals for source=goal, min_aspirations for source=aspiration).
# DO NOT collapse to a single threshold — aspirations are an order of
# magnitude rarer than goals, and a shared threshold would either flood
# false-insufficient_data on asp rows or under-protect goal rows.
def _score_field(criterion, field_meta, state, total, min_total, unit_label):
    """Apply field-coverage rules to one (criterion, field, source) row.

    `total` is the appropriate denominator for this row (total_goals for
    source=goal, total_aspirations for source=aspiration). `min_total` is
    the matching insufficient_data threshold. `unit_label` ("goals" or
    "aspirations") drives both the reason strings AND the evidence-key
    naming (`total_goals` vs `total_aspirations`) the skew detector reads.
    """
    role = field_meta.get("role", "discriminator")
    sparse_by_design = bool(criterion.get("sparse_by_design", False))
    floor = float(criterion.get("discriminability_floor", 0.0) or 0.0)
    expected_distinct_min = criterion.get("expected_distinct_min")

    non_null = state.get("non_null_count", 0)
    distinct_map = state.get("distinct") or Counter()
    distinct_observed = len(distinct_map)
    distinct_capped = distinct_observed >= MAX_DISTINCT_TRACK

    pct = (non_null / total) if total > 0 else 0.0

    evidence = {
        f"total_{unit_label}": total,
        "non_null_count": non_null,
        "non_null_pct": round(pct, 4),
        "distinct_count": distinct_observed,
        "distinct_capped": distinct_capped,
        "role": role,
        "sparse_by_design": sparse_by_design,
        "discriminability_floor": floor,
        "expected_distinct_min": expected_distinct_min,
    }

    if total < min_total:
        return {
            "recommendation": "insufficient_data",
            "reason": (f"Only {total} {unit_label} scanned in this source "
                       f"(< min={min_total}). Need more data."),
            "evidence": evidence,
        }

    if non_null == 0 and not sparse_by_design:
        return {
            "recommendation": "dead_field",
            "reason": (f"0 of {total} {unit_label} have non-null "
                       f"'{field_meta['path']}'. Reader path in "
                       f"goal-selector.py is unreachable."),
            "evidence": evidence,
        }

    if non_null == 0 and sparse_by_design:
        return {
            "recommendation": "sparse_by_design",
            "reason": (f"0 of {total} {unit_label} have '{field_meta['path']}', "
                       f"but criterion is marked sparse_by_design. No action."),
            "evidence": evidence,
        }

    # Degenerate field: discriminator with too few distinct values.
    if (role == "discriminator"
            and expected_distinct_min is not None
            and distinct_observed < int(expected_distinct_min)
            and non_null > 0):
        sole = ""
        if distinct_observed == 1:
            (only_key, only_cnt), = list(distinct_map.items())
            display = _display_value(only_key)
            sole = f" only value: {display!r} ({only_cnt}x)."
        return {
            "recommendation": "degenerate_field",
            "reason": (f"distinct_count={distinct_observed} on "
                       f"'{field_meta['path']}' across {non_null} non-null "
                       f"{unit_label}; expected_distinct_min="
                       f"{expected_distinct_min}.{sole} "
                       f"Criterion can't differentiate."),
            "evidence": evidence,
        }

    # Sparse-by-design fields skip the floor check (their floor is 0.0 anyway).
    if not sparse_by_design and pct < floor:
        return {
            "recommendation": "sparse_below_floor",
            "reason": (f"non_null coverage {pct:.1%} < discriminability_floor "
                       f"{floor:.1%} on '{field_meta['path']}' across "
                       f"{total} {unit_label}. Criterion fires less often "
                       f"than configured floor."),
            "evidence": evidence,
        }

    if sparse_by_design:
        return {
            "recommendation": "sparse_by_design",
            "reason": (f"non_null coverage {pct:.1%} ({non_null}/{total}); "
                       f"sparse-by-design — no action."),
            "evidence": evidence,
        }

    return {
        "recommendation": "healthy",
        "reason": (f"non_null coverage {pct:.1%} meets floor {floor:.1%}; "
                   f"distinct_count={distinct_observed}."),
        "evidence": evidence,
    }


def _score_skew(criterion, field_meta, source_rows, min_total, unit_label):
    """Detect source_skew across two or more sources.

    Pairwise on (min_pct, max_pct). With N >= 3 sources an intermediate
    skew can be mislabeled (which source is the outlier). Documented in
    the module docstring; not fixed today.

    Reads `total_<unit_label>` from evidence so the same code works for
    goal-level and aspiration-level skews.
    """
    if len(source_rows) < 2:
        return None
    total_key = f"total_{unit_label}"
    pcts = [(label,
             row["evidence"]["non_null_pct"],
             row["evidence"].get(total_key, 0),
             row["evidence"]["non_null_count"])
            for label, row in source_rows]
    pcts.sort(key=lambda x: x[1])
    low_label, low_pct, low_total, low_nn = pcts[0]
    high_label, high_pct, high_total, high_nn = pcts[-1]
    diff = high_pct - low_pct
    max_total = max(low_total, high_total)

    if max_total < min_total:
        return None
    if high_nn == 0:  # both dead — dead_field already covers
        return None
    if diff <= SKEW_THRESHOLD:
        return None
    return {
        "recommendation": "source_skew",
        "reason": (f"'{field_meta['path']}' present {high_pct:.1%} in "
                   f"{high_label} ({high_nn}/{high_total}) vs {low_pct:.1%} "
                   f"in {low_label} ({low_nn}/{low_total}); diff "
                   f"{diff:.1%} > {SKEW_THRESHOLD:.0%}. Likely a writer "
                   f"asymmetry — check whether {low_label} should populate "
                   f"this field too."),
        "evidence": {
            "sources": [
                {"label": label, "non_null_pct": round(pct, 4),
                 "non_null_count": nn, total_key: tot}
                for label, pct, tot, nn in pcts
            ],
            "skew_diff": round(diff, 4),
            "skew_threshold": SKEW_THRESHOLD,
        },
    }


# --------------------------------------------------------------------------
# Output formatting
# --------------------------------------------------------------------------

REC_ORDER = {
    "dead_field": 0,
    "degenerate_field": 1,
    "sparse_below_floor": 2,
    "source_skew": 3,
    "unmapped": 4,
    "sparse_by_design": 5,
    "healthy": 6,
    "insufficient_data": 7,
}


def _human_table(batch):
    rows = batch["rows"]
    lines = []
    lines.append("=== Scoring Criterion Field Audit ===")
    lines.append(f"Evaluated at: {batch['evaluated_at']}")
    lines.append(f"Sources: {', '.join(s['label'] for s in batch['sources'])}")
    lines.append(f"Criteria evaluated: {batch['criteria_evaluated']}")
    by_rec = Counter(r["recommendation"] for r in rows)
    lines.append("Distribution:")
    for rec in REC_ORDER:
        if by_rec.get(rec):
            lines.append(f"  {by_rec[rec]:3d}  {rec}")
    lines.append("")

    actionable = [r for r in rows if r["recommendation"] in
                  ("dead_field", "degenerate_field", "sparse_below_floor",
                   "source_skew", "unmapped")]
    lines.append(f"Action items ({len(actionable)}):")
    if not actionable:
        lines.append("  (none — all evaluable rows healthy or sparse-by-design)")
    for r in actionable:
        loc = f"[{r['source']}]" if r.get("source") else ""
        field = r.get("field", "")
        cid = r.get("criterion_id", "")
        lines.append(f"  - {cid}{('.' + field) if field else ''} {loc} → {r['recommendation']}")
        lines.append(f"      {r['reason']}")
        sv = r.get("sample_values") or []
        if sv:
            sv_str = ", ".join(f"{v['value']!r}({v['count']}x)" for v in sv)
            lines.append(f"      sample_values: {sv_str}")

    lines.append("")
    quiet = [r for r in rows if r["recommendation"] in
             ("sparse_by_design", "healthy", "insufficient_data")]
    if quiet:
        lines.append(f"Quiet rows ({len(quiet)}):")
        for r in quiet:
            cid = r.get("criterion_id", "")
            field = r.get("field", "")
            src = r.get("source", "")
            ev = r.get("evidence", {})
            pct = ev.get("non_null_pct")
            pct_s = f"{pct:.1%}" if isinstance(pct, (int, float)) else "n/a"
            # Surface the denominator unit so the reader can sanity-check
            # unusual percentages (100% on 18 asps and 100% on 600 goals
            # are very different signals).
            tot_g = ev.get("total_goals")
            tot_a = ev.get("total_aspirations")
            denom = ""
            if tot_g is not None:
                denom = f"n={tot_g}g"
            elif tot_a is not None:
                denom = f"n={tot_a}a"
            lines.append(
                f"  {cid}.{field} [{src}] {r['recommendation']:<18s} "
                f"pct={pct_s} {denom}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def _mk_state(non_null=0, distinct=None):
    """Build a state dict in the production schema for self-test fixtures.

    `distinct` is a {canonical_key: count} dict. Defaults handle the
    common "no values" case.
    """
    distinct = distinct or {}
    return {
        "non_null_count": non_null,
        "distinct": Counter(distinct),
    }


def _self_test():
    """Synthetic-input regression test for the recommendation rules.

    Asserts every recommendation path on hand-crafted criterion/field/state
    tuples. Mirrors gate-retirement-eval._self_test discipline. Includes:
      - Dead/degenerate/sparse paths on goal-level fields.
      - Source-skew detector across two synthetic source rows.
      - Bug-A regression: list-valued fields with N distinct concrete
        values must NOT collapse to distinct=1.
      - Bug-B regression: source=aspiration uses min_aspirations as the
        threshold and reads `total_aspirations` from evidence.
    """
    min_goals = 10
    min_asps = 3

    # (label, crit, field, state, total, min_total, unit_label, expected)
    cases = [
        ("dead_field-discriminator-zero-presence",
         {"id": "_t", "discriminability_floor": 0.05, "sparse_by_design": False},
         {"path": "user_signal_kind", "source": "goal", "role": "discriminator"},
         _mk_state(non_null=0),
         50, min_goals, "goals", "dead_field"),

        ("sparse_by_design-zero-presence",
         {"id": "_t", "discriminability_floor": 0.0, "sparse_by_design": True},
         {"path": "deferred_until", "source": "goal", "role": "gating"},
         _mk_state(non_null=0),
         50, min_goals, "goals", "sparse_by_design"),

        ("degenerate_field-distinct-1",
         {"id": "_t", "discriminability_floor": 0.0, "sparse_by_design": True,
          "expected_distinct_min": 2},
         {"path": "handoff_to", "source": "goal", "role": "discriminator"},
         _mk_state(non_null=16, distinct={'"alpha"': 16}),
         600, min_goals, "goals", "degenerate_field"),

        # Bug-A regression: list-valued fields must score as healthy when
        # they have N distinct concrete values, NOT degenerate. The pre-fix
        # scanner collapsed every list to "[...]" before counting,
        # producing distinct=1 and a false degenerate flag.
        ("list-valued-field-preserves-cardinality",
         {"id": "_t", "discriminability_floor": 0.50, "sparse_by_design": False,
          "expected_distinct_min": 2},
         {"path": "participants", "source": "goal", "role": "discriminator"},
         _mk_state(non_null=580,
                   distinct={'["agent"]': 400,
                             '["agent","user"]': 150,
                             '["alpha","bravo"]': 30}),
         600, min_goals, "goals", "healthy"),

        ("sparse_below_floor",
         {"id": "_t", "discriminability_floor": 0.50, "sparse_by_design": False},
         {"path": "category", "source": "goal", "role": "discriminator"},
         _mk_state(non_null=30,
                   distinct={f'"v{i}"': 1 for i in range(30)}),
         200, min_goals, "goals", "sparse_below_floor"),

        ("healthy-discriminator",
         {"id": "_t", "discriminability_floor": 0.50, "sparse_by_design": False,
          "expected_distinct_min": 3},
         {"path": "work_class", "source": "goal", "role": "discriminator"},
         _mk_state(non_null=580,
                   distinct={'"code"': 300, '"research"': 200, '"review"': 80}),
         600, min_goals, "goals", "healthy"),

        ("insufficient_data-goal",
         {"id": "_t", "discriminability_floor": 0.50, "sparse_by_design": False},
         {"path": "work_class", "source": "goal", "role": "discriminator"},
         _mk_state(non_null=5, distinct={'"a"': 5}),
         5, min_goals, "goals", "insufficient_data"),

        ("sparse_by_design-with-presence",
         {"id": "_t", "discriminability_floor": 0.0, "sparse_by_design": True},
         {"path": "deferred_until", "source": "goal", "role": "gating"},
         _mk_state(non_null=14,
                   distinct={'"2026-04-19T12:00:00"': 1}),
         600, min_goals, "goals", "sparse_by_design"),

        # Bug-B regression: source=aspiration uses min_aspirations and the
        # `total_aspirations` evidence key. With total=18 and min_asps=3
        # this should land healthy, not insufficient_data.
        ("asp-source-uses-min-aspirations",
         {"id": "_t", "discriminability_floor": 0.50, "sparse_by_design": False,
          "expected_distinct_min": 2},
         {"path": "priority", "source": "aspiration", "role": "discriminator"},
         _mk_state(non_null=18,
                   distinct={'"HIGH"': 5, '"MEDIUM"': 10, '"LOW"': 3}),
         18, min_asps, "aspirations", "healthy"),
    ]
    failures = 0
    for label, crit, field, state, total, min_total, unit, expected in cases:
        result = _score_field(crit, field, state, total, min_total, unit)
        actual = result["recommendation"]
        ok = actual == expected
        if not ok:
            failures += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: "
              f"expected={expected!r} actual={actual!r}")

    skew_cases = [
        ("source_skew-fires-goal-level",
         [("world", {"evidence": {"non_null_pct": 0.027, "total_goals": 597,
                                   "non_null_count": 16}}),
          ("alpha", {"evidence": {"non_null_pct": 0.855, "total_goals": 200,
                                   "non_null_count": 171}})],
         min_goals, "goals", "source_skew"),

        ("source_skew-suppressed-by-aligned",
         [("world", {"evidence": {"non_null_pct": 0.985, "total_goals": 597,
                                   "non_null_count": 588}}),
          ("alpha", {"evidence": {"non_null_pct": 1.000, "total_goals": 200,
                                   "non_null_count": 200}})],
         min_goals, "goals", None),

        ("source_skew-suppressed-by-both-dead",
         [("world", {"evidence": {"non_null_pct": 0.0, "total_goals": 597,
                                   "non_null_count": 0}}),
          ("alpha", {"evidence": {"non_null_pct": 0.0, "total_goals": 200,
                                   "non_null_count": 0}})],
         min_goals, "goals", None),

        ("source_skew-suppressed-by-low-volume",
         [("world", {"evidence": {"non_null_pct": 0.027, "total_goals": 5,
                                   "non_null_count": 0}}),
          ("alpha", {"evidence": {"non_null_pct": 0.855, "total_goals": 8,
                                   "non_null_count": 7}})],
         min_goals, "goals", None),

        # Bug-B regression: skew detector reads total_aspirations key when
        # unit is aspirations.
        ("source_skew-fires-aspiration-level",
         [("world", {"evidence": {"non_null_pct": 0.10, "total_aspirations": 18,
                                   "non_null_count": 2}}),
          ("alpha", {"evidence": {"non_null_pct": 0.95, "total_aspirations": 8,
                                   "non_null_count": 8}})],
         min_asps, "aspirations", "source_skew"),

        # Display-side regression: _display_value strips outer quotes from
        # string canonicals so degenerate_field reasons read 'alpha' not '"alpha"'.
        ("display-value-strips-string-quotes",
         None, None, None, None, None),
    ]
    for case in skew_cases:
        if len(case) == 6 and case[1] is None:
            # Display test sentinel.
            label = case[0]
            actual = _display_value('"alpha"')
            ok = actual == "alpha"
            if not ok:
                failures += 1
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}: "
                  f"expected='alpha' actual={actual!r}")
            continue
        label, source_rows, min_total, unit, expected = case
        result = _score_skew({}, {"path": "_x"}, source_rows, min_total, unit)
        actual = result["recommendation"] if result else None
        ok = actual == expected
        if not ok:
            failures += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: "
              f"expected={expected!r} actual={actual!r}")

    print()
    if failures:
        print(f"FAILED: {failures} self-test case(s) failed.")
        return 1
    print(f"OK: all {len(cases) + len(skew_cases)} self-test case(s) passed.")
    return 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Audit scoring-criterion field coverage in the goal "
                    "corpus and recommend dead/degenerate/skewed fields.")
    ap.add_argument("--goals-source", default="both",
                    choices=["world", "agent", "both"],
                    help="Which aspiration store(s) to scan (default: both).")
    ap.add_argument("--agent-name", default=None,
                    help="Agent dir to scan (default: $MIND_AGENT).")
    ap.add_argument("--all-agents", action="store_true",
                    help="Scan every <agent>/aspirations.jsonl found under "
                         "the project root. Overrides --agent-name.")
    ap.add_argument("--min-goals", type=int, default=DEFAULT_MIN_GOALS,
                    help=f"Insufficient_data threshold per source for "
                         f"goal-level fields (default {DEFAULT_MIN_GOALS}).")
    ap.add_argument("--min-aspirations", type=int, default=DEFAULT_MIN_ASPIRATIONS,
                    help=f"Insufficient_data threshold per source for "
                         f"aspiration-level fields (default "
                         f"{DEFAULT_MIN_ASPIRATIONS}).")
    ap.add_argument("--criterion", default=None,
                    help="Restrict output to one criterion id.")
    ap.add_argument("--include-sample-values", dest="include_sv",
                    action="store_true", default=True,
                    help="Include sample_values in output (default).")
    ap.add_argument("--no-sample-values", dest="include_sv",
                    action="store_false",
                    help="Suppress sample_values for slimmer output.")
    ap.add_argument("--output", default="json", choices=["json", "human"],
                    help="Output format (default json).")
    ap.add_argument("--self-test", action="store_true",
                    help="Run synthetic regression tests and exit.")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    criteria_by_id, pending_audit_ids = _load_criteria_yaml()
    weights = _load_weights()

    if args.criterion:
        if args.criterion not in criteria_by_id:
            print(f"[scoring-criterion-audit] no criterion '{args.criterion}' "
                  f"in {CRITERIA_YAML}", file=sys.stderr)
            return 2
        criteria_by_id = {args.criterion: criteria_by_id[args.criterion]}

    sources = _resolve_sources(args)
    if not sources:
        print("[scoring-criterion-audit] FATAL: no sources resolved (check "
              "--goals-source / --agent-name / --all-agents flags).",
              file=sys.stderr)
        return 2

    source_scans = {label: _scan_source(path, criteria_by_id)
                    for label, path in sources}

    rows = []

    # --- Per (criterion, field, source) recommendations --------------------
    for cid, crit in criteria_by_id.items():
        for field in (crit.get("fields_read") or []):
            field_path = field.get("path")
            field_src = field.get("source", "goal")
            if not field_path:
                continue

            # CRITICAL single decision site: source kind → unit_label →
            # min_total. Changing this routing means re-thinking the skew
            # detector's `total_<unit>` evidence-key contract too.
            if field_src == "aspiration":
                unit_label = "aspirations"
                min_total = args.min_aspirations
            else:
                unit_label = "goals"
                min_total = args.min_goals

            per_source_rows = []
            for label, scan in source_scans.items():
                key = (cid, field_path, field_src)
                state = scan["fields"].get(
                    key, {"non_null_count": 0, "distinct": Counter()}
                )
                total = (scan["total_aspirations"]
                         if field_src == "aspiration" else scan["total_goals"])
                rec = _score_field(crit, field, state, total, min_total, unit_label)
                rec["criterion_id"] = cid
                rec["field"] = field_path
                rec["field_source"] = field_src
                rec["source"] = label
                rec["sample_values"] = _top_sample_values(state, args.include_sv)
                rows.append(rec)
                per_source_rows.append((label, rec))

            skew = _score_skew(crit, field, per_source_rows, min_total,
                               unit_label)
            if skew is not None:
                skew["criterion_id"] = cid
                skew["field"] = field_path
                skew["field_source"] = field_src
                skew["source"] = "cross-source"
                skew["sample_values"] = []
                rows.append(skew)

    # --- Criterion-level unmapped check ------------------------------------
    # Any weight key not in criteria_by_id and not in pending_audit_ids is
    # drift — a criterion was added/renamed without updating the manifest.
    if not args.criterion:
        for weight_key in weights:
            if weight_key in criteria_by_id:
                continue
            if weight_key in pending_audit_ids:
                continue
            rows.append({
                "criterion_id": weight_key,
                "field": None,
                "field_source": None,
                "source": "manifest-check",
                "recommendation": "unmapped",
                "reason": (f"Weight '{weight_key}' exists in "
                           f"{WEIGHTS_YAML.name} but is NOT in "
                           f"{CRITERIA_YAML.name} criteria[] OR "
                           f"pending_audit[]. Drift — update the manifest."),
                "evidence": {"weight": weights[weight_key]},
                "sample_values": [],
            })

    rows.sort(key=lambda r: (
        REC_ORDER.get(r["recommendation"], 99),
        r.get("criterion_id", ""),
        r.get("field") or "",
        r.get("source", ""),
    ))

    batch = {
        "schema_version": 1,
        "evaluated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "min_goals": args.min_goals,
        "min_aspirations": args.min_aspirations,
        "skew_threshold": SKEW_THRESHOLD,
        "include_sample_values": args.include_sv,
        "sources": [
            {"label": label,
             "total_goals": scan["total_goals"],
             "total_aspirations": scan["total_aspirations"]}
            for label, scan in source_scans.items()
        ],
        "criteria_evaluated": len(criteria_by_id),
        "rows": rows,
    }

    if args.output == "human":
        print(_human_table(batch))
    else:
        print(json.dumps(batch, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
