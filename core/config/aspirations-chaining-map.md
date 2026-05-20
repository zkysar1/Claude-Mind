# /aspirations Chaining Map (Full)

Referenced from `.claude/skills/aspirations/SKILL.md` — the orchestrator keeps a
compact summary inline; the full per-phase table lives here. Consult this file
during framework maintenance, skill-graph analysis, or when debugging which
sub-skill should fire at which phase.

| Skill | Called When | Returns |
|---|---|---|
| `/aspirations-precheck` | Every iteration (Phases 0-1) | Updated blockers, auto-completions |
| `/aspirations-select` | Every iteration (Phases 2-2.9) | goal, effort_level, batch |
| `/aspirations-execute` | Phase 4: via digest (load-execute-protocol.sh), full SKILL.md only for edge cases | result, outcome_class, infrastructure_failure |
| `/aspirations-verify` | Phase 5: verification | goal_completed, aspiration_complete |
| `/aspirations-spark` | Phase 6: deep outcomes (all sparks) | New goals, guardrails |
| `/aspirations-complete-review` | Phase 7: aspiration completion | goals_added, should_archive |
| `/aspirations-state-update` | Phase 8: every iteration | Tree encoding, journal |
| `/aspirations-evolve` | Phase 9: triggered evolution | New aspirations, parameter tuning |
| `/aspirations-learning-gate` | Phase 9.5-9.8: every iteration | Learning verified |
| `/aspirations-consolidate` | Session-end | Handoff, encoding, restart |
| `/aspirations-strategic-scan` | Phase 1.5: scan cadence fired | New aspirations from external signal |
| `/aspirations-all-blocked` | Phase 2-2.9: selector returned no executable goals | Backoff sleep, constraint-aware aspirations, yield turn |
| `/aspirations-graceful-stop` | Phase -1.4: stop-requested signal present | Completes in-flight verify/state-update, runs D1-D7, exits loop |
| `/research-topic` | Execute research goals | Tree node updates |
| `/review-hypotheses` | Execute review goals | Accuracy data |
| `/reflect` | Via spark, full-cycle | Patterns, strategies |
| `/decompose` | Compound goal detected | Sub-goals |
| `/boot` | Session start, consolidation restart | Status, handoff |
| `/create-aspiration` | Health, alignment, completion | New aspirations |
| `/forge-skill` | Evolve forge check, spark Q6 | Forged skills |
| `/tree maintain` | Consolidation step 6 | Tree structure changes |
| `/curriculum-gates` | Consolidation step 8.6, evolve post-forge | Stage promotion |
| `/reflect-on-outcome` (Batch Micro mode) | Consolidation step 0 | Batch stats, promotions |
