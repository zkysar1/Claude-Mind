"""Pipeline-store known-field allowlist + the WARN arm for unknown keys ().

WHY WARN AND NOT REFUSE — the choice is measured, not a fallback.
Census 2026-09-01 (echo, cc-03) over the full live corpus, read through
`pipeline-read.sh --stage <each>`, 1,844 deduped records:

    distinct keys                                            363
    writer's known set (REQUIRED_FIELDS | DEFAULT_FIELDS)      15
    unknown / caller-supplied pass-through                    348

The 15 writer fields are present on 100.0% of records and the set of
100%-present keys is EXACTLY the writer set — that equality is the control
proving the census read what it claims to.

The 348 unknown keys were banded by EVIDENCE, not judgement:

    A  written by framework code (quoted literal in core/scripts | mind_api/src)
       101 keys, 19,802 record-key occurrences — includes `resolves_by` (87.4%
       of records), `claim` (86.3%), `measurement_channel` (72.8%),
       `outcome_date` (69.3%), `archived_date` (59.7%), `reflected_date`
       (56.8%), `replay_metadata` (41.6%).
    B  prescribed by a SKILL.md / rule / config but written by no code
       35 keys, 1,889 occurrences — `source_validation` (19.1%),
       `context_consulted` (15.3%), `context_quality` (15.0%).
    C  neither code nor doc references it — the genuine caller-drift set
       212 keys, 1,323 occurrences; 111 of them (52%) appear on exactly ONE
       record.

A HARD REFUSAL WOULD BREAK THE STORE ON DAY ONE. Bands A and B are 136 keys
across 21,691 record-key occurrences that the framework's own code and its own
pseudocode actively write — refusing them would fail the archive, resolve,
reflect and replay paths immediately. The goal that commissioned this census
said so in advance ("DO NOT SHIP A HARD REFUSAL FIRST ... a warn-and-log arm is
a legitimate outcome if the population is large or live callers depend on
pass-through"); both conditions hold, so warn is the CHOICE the measurement
supports, not a retreat from refusing.

WHAT THE WARN ARM IS ACTUALLY FOR, and it is narrower and more useful than
"unknown keys exist": band C is dominated by SPELLING DRIFT of concepts the
store already has. Measured in the same pass —

    premortem 304 (C) / pre_mortem 50 (B) / adversarial_premortem 13 (C)
      / adversarial_pre_mortem 4 (C) / notes_premortem 1 (C)
    reflected_date 1048 (A) / reflected_at 13 (C) / reflected_on 13 (C)
    falsifier 57 (B) / falsifiers 5 / falsification_criteria 32 (C)
      / falsification_criterion 1 / falsifiable_criteria 1

One concept, five spellings, none of them queryable together. That is the cost
being paid today, and it is what a warning surfaces at write time — before the
sixth spelling lands — where a corpus audit only finds it years later.

THE ALLOWLIST AGES, DELIBERATELY (guard-1969). It is a snapshot of what was
legitimate on 2026-09-01. A NEW legitimate field will warn once; that warning is
the prompt to add it here with a source-writer citation, exactly as
GUARD_KNOWN_FIELDS in mind_api/src/store_registry.py works — which is the
sibling mechanism g-115-5965 said to LOCATE AND COPY rather than invent. Do not
silence a warning by widening this set without checking which band the key
belongs to; the banding recipe is in core/config/pipeline-known-fields.json,
which also records the band-C population at generation time so drift is
trendable.

SINGLE SOURCE OF TRUTH: this module is imported by BOTH writers —
mind_api/src/world/pipeline_write.py and core/scripts/pipeline.py. guard-2323
requires a core/scripts fix to be ported to its mind_api twin in the same
change; sharing the constant makes that parity structural instead of a
discipline anyone has to remember.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, Optional

PIPELINE_KNOWN_FIELDS = frozenset((
    "abc_chain",
    "actual",
    "actual_outcome",
    "agent",
    "amendment",
    "applies_to",
    "archived_date",
    "archived_reason",
    "author",
    "baseline",
    "basis",
    "box",
    "calibration",
    "category",
    "caveat",
    "claim",
    "confidence",
    "confounder",
    "consumer",
    "context",
    "context_consulted",
    "context_gaps_identified",
    "context_manifest",
    "context_quality",
    "correction",
    "created_at",
    "created_by",
    "depends_on",
    "depth",
    "derived_from",
    "description",
    "direction",
    "discovered_at",
    "discovered_by",
    "discriminating",
    "domain_class",
    "dual_classification",
    "encoding_score",
    "evidence",
    "evidence_for",
    "evidence_note",
    "evidence_override",
    "evidence_summary",
    "expected_outcome",
    "experience_ref",
    "falsification",
    "falsifier",
    "filed_by",
    "formed",
    "formed_at",
    "formed_date",
    "goal_id",
    "high_surprise",
    "horizon",
    "hypothesis",
    "hypothesis_id",
    "id",
    "last_reviewed",
    "learning",
    "lesson",
    "links",
    "measurement",
    "measurement_baseline",
    "measurement_channel",
    "measurement_gap_detail",
    "measurement_gap_subtype",
    "measurement_pending",
    "measurement_pending_set_at",
    "mechanism",
    "mitigations",
    "note",
    "notes",
    "origin",
    "origin_goal",
    "origin_signal",
    "our_hypothesis",
    "outcome",
    "outcome_date",
    "outcome_detail",
    "outcome_lesson",
    "outcome_note",
    "outcome_summary",
    "position",
    "pre_mortem",
    "preconditions",
    "prediction",
    "predicts",
    "process_quality",
    "process_score",
    "progress_note",
    "question",
    "rationale",
    "reasoning",
    "reconciliation_candidates",
    "reflected",
    "reflected_by",
    "reflected_date",
    "reflected_on",
    "reflection",
    "reflection_id",
    "reflection_note",
    "related",
    "related_goals",
    "replay_metadata",
    "resolution",
    "resolution_criteria",
    "resolution_date_actual",
    "resolution_evidence",
    "resolution_goal",
    "resolution_method",
    "resolution_note",
    "resolution_signal",
    "resolution_source",
    "resolution_summary",
    "resolved",
    "resolved_at",
    "resolved_by",
    "resolved_by_goal",
    "resolved_date",
    "resolved_goal",
    "resolved_in_goal",
    "resolved_via",
    "resolver",
    "resolves_by",
    "resolves_no_earlier_than",
    "resolves_when",
    "result",
    "settling_signal",
    "sibling",
    "slug",
    "source",
    "source_agent",
    "source_data",
    "source_goal",
    "source_goals",
    "source_hypothesis",
    "source_reflection_id",
    "source_step",
    "source_validation",
    "spark_question",
    "stage",
    "statement",
    "status",
    "strategy",
    "surprise",
    "surprise_level",
    "tags",
    "test",
    "title",
    "type",
    "verification",
))

def warn_unknown_fields(rec: Dict[str, Any], *, source: str = "pipeline") -> Optional[set]:
    """WARN (never raise) on keys outside the measured allowlist.

    Returns the unknown set (or None when clean) so a caller or a test can
    assert on it without parsing stderr. Emits at most ONE line per record.
    Deliberately does no file I/O: this runs on every pipeline write, and a
    validator that can fail on a full disk is worse than the drift it reports.
    """
    unknown = set(rec.keys()) - PIPELINE_KNOWN_FIELDS
    if not unknown:
        return None
    print(
        f"[pipeline-unknown-field] {source}: record {rec.get('id')!r} carries "
        f"{len(unknown)} field(s) outside the measured allowlist: {sorted(unknown)}. "
        f"NOT refused — the record is stored verbatim (g-115-5965: 136 of 348 "
        f"caller-supplied keys are legitimately written by framework code or "
        f"pseudocode, so refusing is not available). Check for a SPELLING DRIFT "
        f"of an existing field before adding it to PIPELINE_KNOWN_FIELDS in "
        f"core/scripts/_pipeline_fields.py with a source-writer citation.",
        file=sys.stderr,
    )
    return unknown
