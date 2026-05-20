# Negative Conclusions Convention

A negative conclusion is any claim that something CAN'T be done, IS broken,
DOESN'T work, or ISN'T available. These are uniquely dangerous because they
prevent work — often silently blocking multiple goals for extended periods.

## What Counts as a Negative Conclusion

- "Service X is not running"
- "Infrastructure Y is unavailable"
- "This approach won't work"
- "The data doesn't exist"
- "This endpoint/API/path is wrong"
- "The test failed" (when the test harness itself might be broken)

What is NOT a negative conclusion (no special treatment needed):
- "Goal completed successfully" (positive conclusion — Phase 5 handles this)
- "Value X equals Y" (factual observation, not a negation)
- "This code has a bug" (assertion about presence, not absence)

## Independent Verification Signals

Two signals are "independent" when they use different evidence paths:

| Signal 1 | Independent Signal 2 | NOT Independent |
|----------|---------------------|-----------------|
| curl to endpoint A | curl to endpoint B | Same curl retried |
| HTTP health check | Process/PID check | Same HTTP check with different flag |
| SSH command fails | `infra-health.sh check` | Same SSH retried |
| File not found at path A | Search for file by name | Same path checked twice |
| API returns error | Check API logs/status | Same API call retried |

## Cost-Proportional Verification Tiers

| Downstream Cost | Required Signals | Additional |
|-----------------|-----------------|------------|
| Blocks 0 goals | 1 signal sufficient | — |
| Blocks 1-2 goals | 2 independent signals | — |
| Blocks 3+ goals or creates a blocker | 2+ signals | Must try at least one alternative approach |

"Blocks" means the conclusion would prevent execution of those goals — either via
a formal blocker or by the agent choosing to skip/defer them.

## Silent Failure Catalog

These commands/flags produce ZERO-information results. Empty output from
these means "I don't know," not "it's down":

| Pattern | Why It's Zero-Information |
|---------|-------------------------|
| `curl -sf` | `-f` fails silently on HTTP errors; `-s` suppresses all output. A 404 looks identical to connection refused. |
| `curl -s ... 2>/dev/null` | Swallows both the response and error output |
| `command 2>/dev/null` | Hides the error that would explain the failure |
| `command \|\| true` | Masks the exit code |
| `grep -q` | Returns exit code only — no way to distinguish "not found" from "file error" |
| `test -f` on remote paths | SSH wrapper failures look like "file doesn't exist" |

When a silent-failure command returns empty: re-run WITHOUT the silent flag to see
what actually happened. Then that verbose output counts as signal 1.

## Enforcement Points

1. **Phase 4.0** (fast-path SKIP): Before CREATE_BLOCKER, verify the failure with
   a second independent signal. If the initial failure came from a silent-failure
   command, it counts as zero signals.

2. **Phase 2.5** (metacognitive assessment): When infrastructure probing concludes
   a component is down, record the conclusion in working memory with evidence count.

3. **Phase 0.5b** (blocker resolution): Active reprobing already happens every
   iteration. The convention adds: if a re-probe contradicts the blocker's original
   conclusion, clear the blocker immediately.

4. **Any inline decision**: When the agent decides mid-execution that something
   doesn't work and plans to skip or defer, apply the multi-signal requirement
   before accepting the conclusion.

5. **Phase 5 Q2 (aspirations-verify)**: Automated gate wire-up — a negation
   claim in the result text or Q2 failure-mode answer is routed to the
   appropriate gate script. See `aspirations-verify/SKILL.md` Q2 NEGATIVE CHECK.

## Statistical / Audit Negations (rb-245)

A class of negative conclusion distinct from operational and knowledge
negations: claims about *data values across many records*. Examples:

- "98% of records have times_triggered=0" <!-- DRIFT-EXEMPT: anti-pattern illustration -->
- "0 goals have priority=HIGH"
- "all experience entries are missing the verified_values field"
- "N/M pipeline records lack resolution_criteria"

These look authoritative but fail silently when the field name drifted. The
2026-04-17 guardrail-utilization audit concluded "98% zero-utilization" by
aggregating `times_triggered`; the real counter was `utilization.times_active`.  <!-- DRIFT-EXEMPT: anti-pattern illustration -->
The conclusion had to be retracted the next iteration.

### Protocol

Before accepting a statistical/audit negation, BOTH must hold:

1. **Schema probe**: A `core/scripts/jsonl-field-probe.py` run confirmed the
   field exists in at least one sampled record. The probe is signal 2 — the
   statistical claim itself is signal 1.
2. **Gate pass**: `core/scripts/zero-count-gate.py` exits 0 with
   `--file-probed`, `--field-probed`, and `--probe-result="found"`.

A probe result of `missing` means the field does not exist in the sample —
the statistical claim is almost certainly a schema-drift artifact and must
not be accepted.

### Enforcement

- `aspirations-verify/SKILL.md` Q2 NEGATIVE CHECK invokes the gate for any
  matching claim. A gate exit of 1 maps to Q2 FAIL and the goal goes back
  to pending, same as the infrastructure and knowledge gates.
- The gate is fail-open on internal errors: it prefers letting a legitimate
  claim through to silently suppressing output. Integrity of the enforcement
  surface comes from repeated use across verification cycles, not from the
  gate being a hard wall.

### Two gates, one principle (g-115-58)

Two scripts enforce rb-245 at different points in the audit lifecycle —
they are **complementary, not redundant**:

| Gate | When it fires | Input | What it checks |
|------|---------------|-------|----------------|
| `audit-schema-gate.py` | BEFORE an audit runs | Caller passes file path + field names | Samples live records; blocks if any claimed field is absent or always-null across the sample |
| `zero-count-gate.py` | AFTER a claim is made | Triggered by aspirations-verify Q2 on phrases like "98% have X=0" | Scans the claim text for statistical-negation patterns; requires a separate `jsonl-field-probe.py` run as signal 2 |

**Call order for a new audit/analysis script**:
1. Call `audit-schema-gate.py` with the fields you plan to aggregate over.
   If the gate exits non-zero, the audit is misconfigured — STOP, do not
   proceed to aggregate. This prevents producing the false-authoritative
   claim in the first place.
2. If schema-gate passes, run the aggregation.
3. When the resulting claim reaches aspirations-verify Q2, zero-count-gate
   catches any claim that bypassed step 1 or that used unscoped field
   inference. This is the belt-and-suspenders layer.

The pre-audit gate (step 1) is load-bearing for new code; the post-audit
gate (step 3) is the safety net for code paths that don't yet call
step 1.

## Integration with infra-health.sh

`infra-health.sh check <component>` always counts as 1 real signal (it runs
actual probe scripts, not silent commands). When verifying an infrastructure
conclusion:
- Signal 1: the original failure (unless silent → 0)
- Signal 2: `infra-health.sh check <component>`
- If both agree (down): conclusion accepted
- If they disagree: try a third method before accepting either

## Recording Conclusions

When a negative conclusion is accepted (sufficient signals), record it in the
`conclusions` working memory slot (see working-memory convention). Include:
- The conclusion text
- Evidence signals with weights (0 = silent/zero-info, 1 = real)
- Which goals it blocks
- A re-verify timestamp (30 min for blocking conclusions)
