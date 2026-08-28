"""Canonical goal-record field names — the ONE source of truth for the
update-goal allowlist (g-115-6573).

# domain-leak-exempt: enumerates literal field names observed in the live goal
# store; the strings are functional data, not pedagogical examples.

WHY THIS MODULE EXISTS. `aspirations-update-goal.sh <goal> <field> <value>`
accepted ANY field name and silently created it on the shared goal record, so
one keystroke slip became a permanent schema mutation on a store the whole
fleet reads. The damage is SELF-CONCEALING: every consumer that reads a goal by
field name (goal-selector scoring, the reclaim lanes, the sweeps, the daemon
compose) ignores the stray twin, so the write LOOKS accepted and has no effect.
Measured cost already on the record: a `precondition_unmet` FIELD (that string
is a defer_reason PREFIX, not a field) on a goal whose author believed it had
been deferred — the goal was never deferred, and nothing said so.

IMPORTED BY BOTH WRITE PATHS ON PURPOSE. `core/scripts/aspirations.py`
(cmd_update_goal) and `mind_api/src/endpoints/aspirations_write.py`
(update_goal) both import from here. The daemon is the LIVE path — wrappers are
daemon-only — so a copy hand-typed into one of them would drift silently and
the CLI-side list would look correct while changing nothing at runtime
(guard-742/547 class). One list, two importers, no twin.

HOW THE SET WAS DERIVED (2026-08-18, alpha, cc-08). Census over every live goal
in all statuses via `aspirations-query.sh --goal-status <s> --full`:
2,791 goals, 147 distinct top-level keys. The allowlist is
EVERY OBSERVED KEY MINUS the 27 positively-identified strays below — a
deliberately generous boundary, because a false refusal on a shared write path
breaks live work for the whole fleet, while a missing refusal costs one stray
that the migration pass cleans up. Rarity alone was NOT used to classify: some
rare fields are legitimate and documented (`deliverable_file` is on 2 goals).
Each stray earned its place by evidence — zero quoted-key references anywhere
in core/, mind_api/ or .claude/, AND zero mentions in
core/config/conventions/goal-schemas.md (predicate positive-controlled against
known-documented fields before it was trusted).

RULE OF THUMB FOR EXTENDING (mirrors GUARD_KNOWN_FIELDS in reasoning-bank.py):
a field earns a spot ONLY if (a) an active writer sets it, or (b) it is a core
schema element documented in goal-schemas.md. Add it here with a citation, in
the same change that ships the writer — never as a drive-by.
"""
from __future__ import annotations

# Every field name legitimately present on a goal record. Sorted; the trailing
# comment on each line is the number of live goals carrying it at derivation
# time, which is evidence of use rather than a constraint.
GOAL_KNOWN_FIELDS = frozenset({
    'abstained_at',                      # 17
    'abstained_by',                      # 18
    'achievedCount',                     # 92
    'alert_repeat_count',                # 17
    'alloc_nonce',                       # 2361
    'args',                              # 19
    'artifact_producing',                # 1
    'asp_id',                            # 2791
    'aspiration_id',                     # 7
    'blocked_by',                        # 154
    'blocked_since',                     # 98
    'blocker_ref',                       # 14
    'cadence_signal',                    # 2
    'category',                          # 2782
    'claimed_at',                        # 22
    'claimed_by',                        # 24
    'claimed_by_sid',                    # 22
    'closes_knowledge_debt',             # 1
    'commit_sha',                        # 1
    'completed_at',                      # 821
    'completed_by',                      # 808
    # g-306-204: writer is iteration-close.sh do_verify, stamped beside
    # outcome_class when BODY_ROLE is set (only ever "worker" — see below).
    # 0 at introduction BY CONSTRUCTION: it is a going-forward provenance
    # stamp, so the census that derived every other count here cannot produce
    # one for it. PRESENT+"worker" positively identifies a worker-completed
    # goal; ABSENT means reducer-or-unknown and must never be read as
    # "reducer" (bash-agent-inject.py exports BODY_ROLE ONLY on the worker
    # fork path, so the reducer leaves it unset). That asymmetry is the same
    # "an absent value beats a wrong one" rule the completed_by_sid stamp
    # already follows.
    'completed_by_role',                 # 0 (new)
    'completed_by_sid',                  # 792
    'completed_date',                    # 762
    'consecutive_deep',                  # 79
    'consecutive_routine',               # 79
    'coordination_note',                 # 4
    'created_at',                        # 2767
    'created_at_backfilled',             # 16
    'cross_world_audit_ref',             # 6
    'cross_world_origin',                # 17
    'cross_world_reason',                # 17
    'cross_world_timestamp',             # 17
    'currentStreak',                     # 82
    'deadline',                          # 3
    'defer_reason',                      # 234
    'defer_reason_set_at',               # 232
    'deferred_until',                    # 28
    'deliverable_file',                  # 2
    'depends_on',                        # 6
    'depth',                             # 1
    'description',                       # 2790
    'diagnostic_context',                # 2
    'discovered_by',                     # 1080
    'discovery_type',                    # 1082
    'displaced_from',                    # 14
    'estimated_depth',                   # 38
    'estimated_seconds',                 # 34
    'evidence_note',                     # 12
    'executed_by',                       # 886
    'executed_by_sid',                   # 886
    'expected_coverage_paths',           # 2
    'experience_ref',                    # 4
    'filed_by_agent',                    # 2683
    'findings',                          # 1
    'goal_id',                           # 2791
    'goal_source',                       # 2741
    'handoff_created_at',                # 25
    'handoff_from',                      # 40
    'handoff_to',                        # 49
    'horizon',                           # 206
    'hypothesis_id',                     # 216
    'id',                                # 2791
    'injected_by',                       # 17
    'intended_agent',                    # 2753
    'interval_hours',                    # 86
    'key_finding',                       # 275
    'lastAchievedAt',                    # 81
    'last_completed',                    # 6
    'last_modified',                     # 1649
    'last_outcome_origin',               # 79
    'last_run',                          # 1
    'last_shelve_reason',                # 1
    'last_shelved_at',                   # 1
    'last_substantive_at',               # 65
    'longestStreak',                     # 82
    'longestWindowStreak',               # 81
    'notes',                             # 4
    'offload_decision',                  # 30
    'ohs_axis',                          # 10
    'origin_goal',                       # 1
    'origin_signal',                     # 2791
    'original_interval_hours',           # 58
    'outcome',                           # 1
    'outcome_class',                     # 700
    'outcome_note',                      # 966
    'outcome_notes',                     # 25
    'outcome_signal_source',             # 4
    'parent_goal',                       # 6
    'participants',                      # 2774
    'preconditions',                     # 3
    'priority',                          # 2791
    'progress_note',                     # 262
    'pull_signal',                       # 0 — NEW 2026-08-23 (g-115-6590).
    #   Zero observations is CORRECT, and is exactly why it was missing: this
    #   allowlist was derived 2026-08-18 from a census of keys OBSERVED on live
    #   goals, while `pull_signal`'s CONSUMER (goal-selector apply_pull_boost)
    #   had shipped 2026-08-17 with no producer — so no goal carried the key and
    #   the census could not see it. A census-derived allowlist is structurally
    #   blind to any read-only field whose writer has not shipped yet. Registering
    #   it here is the refusal message's own prescribed remedy ('register it in
    #   _goal_fields.py in the same change that ships its writer'), and this
    #   change ships that writer: core/scripts/pull-signal-set.sh.
    'recurring',                         # 160
    'recurring_interval_hours',          # 2
    'references',                        # 1
    'release_negatives',                 # 0  (g-115-8163 — see release() in
                                         #     mind_api/src/endpoints/aspirations_write.py)
    'requires_capability',               # 8
    'resolves_by',                       # 196
    'resolves_no_earlier_than',          # 194
    'revenue_link',                      # 29
    'sandbox',                           # 17
    'sessions_active',                   # 1
    'skill',                             # 378
    'skip_reason',                       # 1
    'source',                            # 2791
    'source_s3_key',                     # 6
    'started',                           # 969
    'started_at',                        # 14
    'status',                            # 2791
    'subclass',                          # 72
    'substantive_hits',                  # 65
    'substantive_runs',                  # 79
    'superseded_by',                     # 0 at add time — g-115-7893; core
                                         # schema element, documented in
                                         # goal-schemas.md 'Supersession
                                         # Pointer Field'. Qualifies under
                                         # rule (b) above; this change ships
                                         # its writer by allowlisting it, and
                                         # goal-schemas.md 'Writing it' asks
                                         # for exactly this write.
    'tags',                              # 360
    'title',                             # 2791
    'type',                              # 52
    'user_leg_scope',                    # 53
    'verification',                      # 1308
    'verification_notes',                # 1
    'verify_summary',                    # 3
    'windowStreak',                      # 81
    'work_class',                        # 2758
})

# Fields observed on live records that are NOT legitimate. Kept as data rather
# than deleted from history so the one-shot migration (g-115-6573 item 2) has a
# machine-readable target, and so a future reader can see WHY each is refused
# instead of rediscovering it. A name here is REFUSED by the gate even though it
# exists in the store — existing occurrences are the migration's problem, not
# the writer's licence.
GOAL_STRAY_FIELDS = {
    '__noop': 'no writer, no reader, no schema entry — one-off invention',
    '__probe__': 'no writer, no reader, no schema entry — one-off invention',
    '_probe': 'field-name probe artifact',
    'box_local_demonstrated': 'no writer, no reader, no schema entry — one-off invention',
    'checks': 'verification.checks written at goal top level, one goal '
              '(g-335-1330, completed); no consumer reads a top-level checks '
              '(selection-stack review census 2026-08-21)',
    'complete-by': 'hyphenated; matches the SCRIPT aspirations-complete-by.sh, not a field',
    'created': 'twin of created_at (1935 goals)',
    'defer_reason_correction': 'no writer, no reader, no schema entry — one-off invention',
    'defer_until': 'no writer, no reader, no schema entry — one-off invention',
    'depends on': 'space-typo twin of depends_on, one goal (g-115-7095); its '
                  'referent g-115-7093 is completed so the pointer is inert; '
                  'fold on migration (selection-stack review 2026-08-21)',
    'description_append': 'invented to append to description (1958 goals)',
    'desiredEndState': 'camelCase drift',
    'evaluator_waste_note': 'no writer, no reader, no schema entry — one-off invention',
    'evidence_refs': 'no writer, no reader, no schema entry — one-off invention',
    'evidence_source': 'one goal (g-326-476, completed); evidence citation '
                       'that belongs in outcome_note/key_finding '
                       '(selection-stack review census 2026-08-21)',
    'execution_note': 'no writer, no reader, no schema entry — one-off invention',
    'lastAchieved': 'no writer, no reader, no schema entry — one-off invention',
    'measurement_baseline': 'no writer, no reader, no schema entry — one-off invention',
    'not_before': 'one goal (g-248-44, skipped); hand-invented precursor of '
                  'resolves_no_earlier_than / deferred_until '
                  '(selection-stack review census 2026-08-21)',
    'outcome_note_addendum': 'no writer, no reader, no schema entry — one-off invention',
    'precondition_unmet': 'a defer_reason PREFIX typed into the field slot',
    'prior_instances': 'no writer, no reader, no schema entry — one-off invention',
    'processor_gpu_triage_note': 'no writer, no reader, no schema entry — one-off invention',
    'related_reasoning': 'no writer, no reader, no schema entry — one-off invention',
    'retraction_stale_checkout': 'no writer, no reader, no schema entry — one-off invention',
    'route_to': 'no writer, no reader, no schema entry — one-off invention',
    'scheduleType': 'no writer, no reader, no schema entry — one-off invention',
    'site2_processor_note': 'no writer, no reader, no schema entry — one-off invention',
    'site_enumeration': 'no writer, no reader, no schema entry — one-off invention',
    'skip_justification': 'no writer, no reader, no schema entry — one-off invention',
    'verification_goal_ref': 'no writer, no reader, no schema entry — one-off invention',
}


def is_known(field: str) -> bool:
    """True when `field` may be written to a goal record without an override."""
    return field in GOAL_KNOWN_FIELDS


def unknown_field_error(field: str) -> str:
    """The refusal message. Names the field, says why, and gives the escape hatch.

    Echoes a CLOSE MATCH when one exists: every stray measured in the derivation
    census was either a probe artifact or a near-miss of a real field
    (`created` for `created_at`, `complete-by` for a script name), so the
    single most useful thing the message can carry is the name the author meant.
    """
    hint = ""
    if field in GOAL_STRAY_FIELDS:
        hint = (f" It is a KNOWN STRAY: {GOAL_STRAY_FIELDS[field]}."
                f" It exists on some records; that is drift, not precedent.")
    else:
        near = sorted(
            k for k in GOAL_KNOWN_FIELDS
            if k.replace("_", "").replace("-", "").lower()
            == field.replace("_", "").replace("-", "").lower()
        )
        if near:
            hint = f" Did you mean {near[0]!r}?"
    return (
        f"unknown goal field {field!r} — refused.{hint} "
        f"aspirations-update-goal accepts only the {len(GOAL_KNOWN_FIELDS)} names in "
        f"core/scripts/_goal_fields.py::GOAL_KNOWN_FIELDS. A silently-created field is "
        f"invisible to every consumer that reads goals by name, so the write would look "
        f"accepted and do nothing. To add a genuinely new field, pass "
        f"--allow-new-field \"<justification>\" (audited), and register it in "
        f"_goal_fields.py in the same change that ships its writer."
    )
