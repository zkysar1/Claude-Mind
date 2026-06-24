#!/usr/bin/env python3
# domain-leak-exempt: Gate D analysis tool — references gate-d telemetry paths and
# experiment fields by design (analysis-stage pilot validator, run by omni).
"""gate-d-validate-telemetry.py — pilot/interim telemetry integrity checks.

ANALYSIS-STAGE (omni-run, repeated through the pilot window). Never touches
assignment or flag logic. Checks every record in the per-agent telemetry files
against the blessed contracts:

ASSIGNMENT records
  A1. arm + assignment_hash match a fresh assign_arm(goal_id) (golden algorithm)
  A2. corpus_size == 338 and experiment_version == "gate-d-v1" (frozen constants)
  A3. injection_status in {injected, no_patterns, control, error}
  A4. arm A  => patterns_injected == 0 (SEAM-1 no-op)
      arm B + injected => 1..5 patterns and injection_tokens <= 2000
  AX. excluded=true assignments (post-compaction resume): A1 verified, A2/A3/A4
      skipped — the goal received de-facto no-injection; excluded from A/B analysis.
OUTCOME records
  O1. amendment 6: blocker_created == true  =>  verify_first_pass == false
  O2. duplicate OUTCOME per (agent, goal_id): identical-sans-timestamp => warning
      (known benign double-write shape); conflicting field values => violation
      Four supersede patterns recognized (not violations):
        (a) pre-assignment boundary OUTCOME (null measurement fields) superseded
            by post-assignment re-run (anchored to assignment timestamp)
        (b) null-fill boundary OUTCOME (all three measurement fields null) superseded
            by completed re-run (not assignment-anchored; unambiguous from null shape)
        (c) identical-sans-timestamp (benign double-write)
        (d) routine→deep re-classification: outcome_class is the sole differing
            field, transitioning routine→deep; the deeper re-run supersedes
QUARANTINE
  S1 violations for agents in _S1_QUARANTINE_AGENTS are downgraded to warnings.
  Those agents' assignments are excluded from all verdict math (Addendum D).
JOIN
  J1. orphan OUTCOMEs (no matching ASSIGNMENT on (agent, goal_id)) — report
  J2. orphan ASSIGNMENTs (no OUTCOME yet) — informational (goal may be in flight)
SINGLE-BLIND
  S1. each agent's execution-diary.jsonl carries no experiment-identifying tokens;
      allowed markers (step-5e completion + seam-resume announcement) are skipped.

Exit 0 = no violations (orphans/in-flight are warnings, not violations).
Exit 1 = at least one A*/O*/S* violation.

Usage:
  py -3 core/scripts/gate-d-validate-telemetry.py            # all agents
  py -3 core/scripts/gate-d-validate-telemetry.py --agent alpha
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _gate_d import EXPERIMENT_VERSION, TOP_K, TOKEN_CAP, assign_arm  # noqa: E402

#: Frozen corpus file name and size (Addendum A1, sha anchored in methodology doc).
#: Used to synthesize corpus_size for DEGRADED-shape records that carry corpus_source
#: but omit corpus_size (the wrapper field absent from raw seam stdout).
_FROZEN_CORPUS_FILE = "gate-d-corpus-zds-2026-06-10.jsonl"
_FROZEN_CORPUS_SIZE = 338

#: Tokens that identify THE experiment (generic words like "experiment"/"treatment"
#: false-positive on ordinary goal content — alpha runs its own experiments).
_BLIND_TOKENS = ["gate-d", "gate_d", "commons pattern", "injected pattern",
                 "supplementary reference pattern", "control arm", "arm assignment"]
#: Diary lines matching any of these markers are exempt from S1 scan.
#: "step-5e context preparation complete" — the canonical seam completion marker.
#: "gate-d step-5e skipped on resume" — seam resume announcement (post-compaction
#:   case: bravo/g-318-08, 2026-06-13; the seam ran after execution was already
#:   complete; goal excluded from A/B analysis; see campaign log 2026-06-13).
#: "gate-d-5e-outside-skip" — verify-learning check identifier for Step 5e seam
#:   structural integrity (asserts Step 5e ALWAYS RUNS, NEVER gated by trivial_mode;
#:   check names: gate-d-5e-outside-skip-digest, gate-d-5e-outside-skip-skill).
#:   Framework maintenance work; no arm-assignment or injection data revealed.
#:   Canonical case: bravo/g-115-1414, 2026-06-13T14:10:05.
_ALLOWED_MARKERS = [
    "step-5e context preparation complete",
    "gate-d step-5e skipped on resume",
    "gate-d-5e-outside-skip",
]
#: Only diary entries written after the pilot flip are in scope for S1.
_FLIP_TS = "2026-06-11T07:55:00"
#: Per-agent S1 scope: an agent's diary is in scope only AFTER that agent was
#: enrolled (stage-1 cutover enrolled echo+zeta 2026-06-12T00:47). Mentions
#: before enrollment are sanctioned non-blind roles — e.g. zeta diagnosing the
#: test_gate_d fixture regression on 06-11 as the designated non-blind party
#: (campaign log 2026-06-12 01:14 incident). Agents not listed default to
#: _FLIP_TS.
_S1_SCOPE = {"alpha": _FLIP_TS,
             "echo": "2026-06-12T00:47:00",
             "zeta": "2026-06-12T00:47:00",
             "bravo": "2026-06-12T13:20:00",
             "delta": "2026-06-12T13:20:00",
             "charlie": "2026-06-14T16:07:00"}
#: Agents quarantined from A/B analysis due to a confirmed S1 single-blind
#: violation. S1 violations for quarantined agents are downgraded to warnings —
#: the violation is documented; these agents' assignments are excluded from all
#: verdict math (see Addendum D in the methodology doc).
#: 2026-06-13: echo — diary line 4135 written at 2026-06-13T21:07:02 contains
#: "applied Gate-D-injected rb-102", revealing awareness of the gate-d experiment
#: identifier. Root cause: _gate_d.py JSON output includes
#: experiment_version:"gate-d-v1" which the LLM sees during Step 5e JSON parse.
#: Echo's 4 B-arm executions occurred before the violation (last at 16:09, diary
#: at 21:07), but the integrity concern is sufficient to quarantine the agent.
#: 2026-06-14: bravo — diary line 26227 written at 2026-06-14T02:25:47 contains
#: "gate-d-inject not run for this execution (lost to autocompact between select
#: 02:05 and close); no ASSIGNMENT record so this Gate D outcome is incomplete
#: and drops at analysis join." Agent explicitly aware of gate-d injection mechanism
#: AND analysis join mechanics. All 4 valid bravo B-arm outcomes pre-violation
#: (last: g-001-270 at 2026-06-13T22:14:21). J2 g-001-07 (assigned 00:15 06-14)
#: will complete post-quarantine; excluded regardless. Addendum E.
#: 2026-06-14: delta — diary line 3775 written at 2026-06-14T09:49:15 for goal
#: g-001-01 contains "Gate D arm-B treatment restored by reconstructing the 5 logged
#: patterns (rb-13,rb-129,rb-230,guard-35,guard-54) directly from gate-d-corpus by
#: signature (post-compaction gate-d-inject re-run was non-deterministic; corpus
#: lookup is fidelity-exact)." Full unblinding: arm assignment, injection content,
#: and corpus source all revealed. Assignment at 09:38:44, violation at 09:49:15,
#: OUTCOME at 10:02:19 — verify executed after unblinding. 11 pre-violation delta
#: pairs valid (A=6, B=5) but full-quarantine protocol applied per echo/bravo
#: precedent. Remaining enrolled agents: alpha, zeta. Addendum G.
_S1_QUARANTINE_AGENTS: set = {"echo", "bravo", "delta"}
#: Known pre-amendment artifacts (append-only file keeps them forever): the first
#: two pilot goals ran before amendment 5 (no ASSIGNMENT) and amendment 6
#: (first_pass defaulted true on blocked) — documented in the campaign log
#: 2026-06-11; excluded so the validator alerts only on NEW violations.
_KNOWN_ARTIFACTS = {("alpha", "g-250-120"), ("alpha", "g-250-127")}
#: Known seam-format anomalies: records written by a non-canonical Step-5e seam
#: variant that uses legacy field names (status/ts instead of injection_status/
#: timestamp) and computes a hash that doesn't match the golden algorithm.
#: For each entry: the ARM is verified correct by checking expected parity; the
#: treatment was correctly applied (arm A = no injection); the OUTCOME is eligible
#: for A/B analysis. A1/A3 violations are suppressed; a warning is emitted instead.
#: 2026-06-13: alpha/g-315-178 — seam format v0 (ts/status field names); recorded
#: hash 0xD8BB32B8 vs expected 0xE6502EF8; arm A correct; no injection applied;
#: no step-5e diary marker; outcome vfp=True is a genuine control observation.
#: 2026-06-13: alpha/g-115-1428 — same legacy seam code path (hash 0xD8BB32B8).
#: CRITICAL difference from g-315-178: recorded arm=A but golden algorithm says arm=B.
#: Goal received CONTROL treatment when it should have been INJECTED. No outcome yet
#: at time of discovery (J2 in-flight). Entry is in _ARM_WRONG_EXCLUSIONS — must be
#: excluded from A/B tally regardless of what arm field says. A1 suppressed to warning.
#: 2026-06-14: delta/g-318-02 — same legacy seam sentinel (hash 0xD8BB32B8, iter-110).
#: injection_status=control, patterns_injected=0 — consistent with A-arm control treatment.
#: Golden algorithm says arm=B. Recorded arm=A with legacy hash. Same exclusion class as
#: alpha/g-115-1428. Delta session running legacy gate-d-inject.sh code. A1 suppressed.
#: 2026-06-14: bravo/g-115-1448 — legacy seam sentinel (hash 0xD8BB32B8). Bravo already
#: quarantined (Addendum E); this A1 suppressed to warning for audit completeness.
#: Recorded arm=A, golden algorithm says arm=B. Excluded from A/B tally.
#: 2026-06-14: bravo/g-305-10 — legacy seam sentinel (hash 0xD8BB32B8, iter-116).
#: Recorded arm=A, golden algorithm also says arm=A — arm CORRECT, no injection misapplied.
#: Same legacy seam code path; treatment applied correctly. Bravo quarantined; tally
#: unaffected regardless. A1 suppressed to warning. NOT in _ARM_WRONG_EXCLUSIONS.
_KNOWN_SEAM_FORMAT_ANOMALIES = {("alpha", "g-315-178"), ("alpha", "g-115-1428"),
                                  ("delta", "g-318-02"), ("bravo", "g-115-1448"),
                                  ("bravo", "g-305-10")}
#: Records where the legacy seam not only used the wrong hash but recorded the WRONG
#: ARM — the goal received different treatment than the golden algorithm dictated.
#: These records must be excluded from A/B tally (not just have A1 downgraded to warning).
#: The tally script must check this set and skip any matching (agent, goal_id) pair.
_ARM_WRONG_EXCLUSIONS: set = {("alpha", "g-115-1428"), ("delta", "g-318-02"),
                              ("bravo", "g-115-1448")}
#: Measurement fields used to detect null-fill boundary OUTCOMEs (O2 pattern b).
_NULL_MEASUREMENT_FIELDS = ("verify_first_pass", "verify_escalation_depth", "retry_count")


def _read_jsonl(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        out.append({"_malformed": line[:80]})
    except OSError:
        pass
    return out


def main():
    ap = argparse.ArgumentParser(description="Gate D telemetry integrity validator")
    ap.add_argument("--agent", default="*", help="agent name (default: all)")
    args = ap.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(script_dir))
    violations, warnings, n_assign, n_outcome = [], [], 0, 0
    assigns, outcomes = {}, {}

    for path in sorted(glob.glob(os.path.join(
            root, "agents", args.agent, "session", "gate-d-telemetry.jsonl"))):
        agent = os.path.basename(os.path.dirname(os.path.dirname(path)))
        for r in _read_jsonl(path):
            if "_malformed" in r:
                violations.append(f"[{agent}] malformed line: {r['_malformed']}")
                continue
            key = (agent, r.get("goal_id"))
            if "record_type" not in r and "assignment_hash" in r and "arm" in r:
                # Degraded-shape assignment: raw seam stdout written without the
                # Step-5e telemetry wrapper (no record_type/agent/timestamp).
                # Canonical case: zeta/g-316-03, 2026-06-12 — arm+hash verified
                # exact vs golden, so the assignment is REAL; normalize so it
                # joins and A1 verifies it. experiment_version and corpus_size are
                # absent in the raw shape — synthesized here, with the deviation
                # flagged. corpus_size is derived from corpus_source: if it names
                # the frozen file, synthesize 338; otherwise leave None so A2 fires.
                _cs = r.get("corpus_source", "")
                _synth_size = _FROZEN_CORPUS_SIZE if _cs == _FROZEN_CORPUS_FILE else None
                r = dict(r, record_type="assignment",
                         injection_status=r.get("status"),
                         timestamp=r.get("ts", ""),
                         experiment_version=EXPERIMENT_VERSION,
                         corpus_size=r.get("corpus_size") or _synth_size)
                warnings.append(
                    f"DEGRADED-shape assignment (raw seam output, normalized): "
                    f"{agent}/{r.get('goal_id')}")
            if r.get("record_type") == "assignment":
                n_assign += 1
                excluded = r.get("excluded", False)
                if excluded:
                    # AX: post-hoc exclusion — seam ran after execution was already
                    # complete (compaction resume or Step 5e sequencing slip); arm-B
                    # patterns were NOT applied to execution. Goal is censored from A/B
                    # analysis. Verify A1 (hash integrity) but skip A2/A3/A4.
                    # Reason field: records use either 'excluded_reason' or 'exclude_reason'
                    # (without 'd') — accept both.
                    _excl_reason = (r.get("excluded_reason") or r.get("exclude_reason") or "")
                    warnings.append(
                        f"AX EXCLUDED assignment (post-hoc, censored from A/B): "
                        f"{agent}/{r.get('goal_id')} arm={r.get('arm')} "
                        f"reason={_excl_reason[:80]}")
                    arm, h = assign_arm(r["goal_id"])
                    if r.get("arm") != arm or r.get("assignment_hash") != "0x%08X" % h:
                        violations.append(
                            f"A1 [{agent}/{r['goal_id']}] arm/hash mismatch on excluded "
                            f"assignment: recorded {r.get('arm')}/{r.get('assignment_hash')} "
                            f"expected {arm}/0x{h:08X}")
                    assigns[key] = r  # still record so J2 doesn't fire for in-flight
                    continue
                assigns[key] = r
                arm, h = assign_arm(r["goal_id"])
                if r.get("arm") != arm or r.get("assignment_hash") != "0x%08X" % h:
                    if key in _KNOWN_SEAM_FORMAT_ANOMALIES:
                        # Seam format anomaly: legacy field names (ts/status) + wrong hash.
                        # For most entries arm is correct; but _ARM_WRONG_EXCLUSIONS entries
                        # received the WRONG treatment — see that set for per-entry notes.
                        if key in _ARM_WRONG_EXCLUSIONS:
                            warnings.append(
                                f"A1 SEAM-FORMAT-ANOMALY [{agent}/{r.get('goal_id')}] "
                                f"hash mismatch (legacy seam fmt, ARM WRONG — excluded from tally): "
                                f"recorded {r.get('arm')}/{r.get('assignment_hash')} expected {arm}/0x{h:08X}")
                        else:
                            warnings.append(
                                f"A1 SEAM-FORMAT-ANOMALY [{agent}/{r.get('goal_id')}] "
                                f"hash mismatch (legacy seam fmt, arm correct, no injection): "
                                f"recorded {r.get('arm')}/{r.get('assignment_hash')} expected {arm}/0x{h:08X}")
                    else:
                        violations.append(
                            f"A1 [{agent}/{r['goal_id']}] arm/hash mismatch: recorded "
                            f"{r.get('arm')}/{r.get('assignment_hash')} expected {arm}/0x{h:08X}")
                if r.get("corpus_size") != 338 and r.get("injection_status") in ("injected", "no_patterns"):
                    violations.append(
                        f"A2 [{agent}/{r['goal_id']}] corpus_size={r.get('corpus_size')} (expected 338)")
                if r.get("experiment_version") != EXPERIMENT_VERSION:
                    violations.append(
                        f"A2 [{agent}/{r['goal_id']}] experiment_version={r.get('experiment_version')}")
                _inj_status = r.get("injection_status") or r.get("status")
                if _inj_status not in ("injected", "no_patterns", "control", "error"):
                    if key in _KNOWN_SEAM_FORMAT_ANOMALIES:
                        warnings.append(
                            f"A3 SEAM-FORMAT-ANOMALY [{agent}/{r.get('goal_id')}] "
                            f"injection_status field absent (legacy seam fmt: status={r.get('status')!r})")
                    elif agent in _S1_QUARANTINE_AGENTS:
                        warnings.append(
                            f"A3-QUARANTINE [{agent}/{r['goal_id']}] injection_status={r.get('injection_status')}"
                            f" (agent quarantined from A/B — excluded from verdict math)")
                    else:
                        violations.append(
                            f"A3 [{agent}/{r['goal_id']}] injection_status={r.get('injection_status')}")
                if r.get("arm") == "A" and r.get("patterns_injected", 0) != 0:
                    violations.append(f"A4 [{agent}/{r['goal_id']}] arm A with injections (SEAM-1)")
                if r.get("arm") == "B" and r.get("injection_status") == "injected":
                    pi = r.get("patterns_injected") or 0
                    tok = r.get("injection_tokens") or 0
                    if not (1 <= pi <= TOP_K) or tok > TOKEN_CAP:
                        violations.append(
                            f"A4 [{agent}/{r['goal_id']}] B-arm bounds: patterns={pi} tokens={tok}")
            elif r.get("record_type") == "outcome":
                n_outcome += 1
                if key in outcomes:
                    prev = {k: v for k, v in outcomes[key].items() if k != "timestamp"}
                    cur = {k: v for k, v in r.items() if k != "timestamp"}
                    if prev == cur:
                        warnings.append(
                            f"O2 duplicate OUTCOME (identical sans timestamp): {agent}/{r.get('goal_id')}")
                    else:
                        # Re-execution boundary: a goal can carry a pre-assignment
                        # OUTCOME (e.g. graceful-stop boundary record with null
                        # fields) and later be re-executed WITH an assignment.
                        # prev < assignment <= current means the earlier record is
                        # superseded, not conflicting — the join uses the
                        # post-assignment pair (canonical case: echo/g-315-154,
                        # campaign log 2026-06-12).
                        a_ts = assigns.get(key, {}).get("timestamp", "")
                        prev_ts = outcomes[key].get("timestamp", "")
                        if a_ts and prev_ts < a_ts <= r.get("timestamp", ""):
                            warnings.append(
                                f"O2 pre-assignment boundary OUTCOME superseded by "
                                f"post-assignment re-run: {agent}/{r.get('goal_id')}")
                        else:
                            # Null-fill boundary: first OUTCOME had all measurement
                            # fields null (graceful-stop written before values were
                            # computed). Second OUTCOME with actual values supersedes
                            # it — unambiguous even without an assignment timestamp.
                            # Canonical case: bravo/g-001-01, 2026-06-13.
                            prev_is_null = all(
                                outcomes[key].get(f) is None
                                for f in _NULL_MEASUREMENT_FIELDS)
                            cur_has_values = any(
                                r.get(f) is not None
                                for f in _NULL_MEASUREMENT_FIELDS)
                            if prev_is_null and cur_has_values:
                                warnings.append(
                                    f"O2 null-fill boundary OUTCOME superseded by "
                                    f"completed re-run: {agent}/{r.get('goal_id')}")
                            else:
                                diff = sorted(
                                    k for k in set(prev) | set(cur)
                                    if prev.get(k) != cur.get(k))
                                # Pattern (d): outcome_class-only re-classification.
                                # The sole differing field is outcome_class (any direction:
                                # routine→deep, deep→routine, etc.). All verdict-relevant
                                # measurements (vfp, retry_count, blocker_created) agree —
                                # only the depth label changed. Last record supersedes.
                                # 2026-06-12→13: alpha/g-001-10 routine→deep (original)
                                # 2026-06-14: alpha/g-001-10 deep→routine (third re-run,
                                # still vfp=True/retry=0 — same outcome, lighter label)
                                if diff == ["outcome_class"]:
                                    old_oc = outcomes[key].get("outcome_class")
                                    new_oc = r.get("outcome_class")
                                    warnings.append(
                                        f"O2 outcome_class re-classification ({old_oc}→{new_oc}, "
                                        f"last supersedes): {agent}/{r.get('goal_id')}")
                                elif agent in _S1_QUARANTINE_AGENTS:
                                    warnings.append(
                                        f"O2-QUARANTINE [{agent}/{r.get('goal_id')}] conflicting "
                                        f"duplicate OUTCOME (fields differ: {', '.join(diff)}) "
                                        f"— agent quarantined, excluded from verdict math (Addendum D)")
                                else:
                                    violations.append(
                                        f"O2 [{agent}/{r.get('goal_id')}] conflicting duplicate OUTCOME "
                                        f"(fields differ: {', '.join(diff)})")
                outcomes[key] = r
                if key in _KNOWN_ARTIFACTS:
                    continue
                # O3: verify_first_pass must be a boolean (True/False) — not a string.
                # String "t"/"f"/"true"/"false" are telemetry writer bugs. Emit a warning
                # (not a violation) because the intended value is unambiguous, but note
                # for tally scripts that normalize before counting.
                vfp_raw = r.get("verify_first_pass")
                if vfp_raw is not None and not isinstance(vfp_raw, bool):
                    warnings.append(
                        f"O3 [{agent}/{r['goal_id']}] verify_first_pass is non-boolean: {vfp_raw!r} "
                        f"(telemetry writer bug — normalize before counting)")
                if r.get("blocker_created") is True and r.get("verify_first_pass") is True:
                    violations.append(
                        f"O1 [{agent}/{r['goal_id']}] blocked goal with verify_first_pass=true (amendment 6)")

        diary = os.path.join(os.path.dirname(path), "execution-diary.jsonl")
        if os.path.isfile(diary):
            with open(diary, "r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    low = line.lower()
                    if any(m in low for m in _ALLOWED_MARKERS):
                        continue
                    try:
                        ts = json.loads(line).get("timestamp", "")
                    except (json.JSONDecodeError, ValueError):
                        ts = ""
                    if ts < _S1_SCOPE.get(agent, _FLIP_TS):
                        continue  # pre-enrollment entry — out of S1 scope
                    for tok in _BLIND_TOKENS:
                        if tok in low:
                            if agent in _S1_QUARANTINE_AGENTS:
                                warnings.append(
                                    f"S1-QUARANTINE [{agent}] diary line {i} contains '{tok}' "
                                    f"(agent quarantined from A/B — excluded from verdict math, Addendum D)")
                            else:
                                violations.append(f"S1 [{agent}] diary line {i} contains '{tok}'")
                            break

    for key in outcomes:
        if key not in assigns and key not in _KNOWN_ARTIFACTS:
            if outcomes[key].get("outcome_class") == "routine":
                # Every assignment ever recorded carries estimated_depth=deep and
                # routine goals appear only as orphan OUTCOMEs — consistent with
                # the seam assigning deep goals only. Label separately so the
                # censoring count (deep orphans) stays readable.
                warnings.append(
                    f"J1 routine-class OUTCOME without ASSIGNMENT (consistent with "
                    f"deep-only assignment): {key[0]}/{key[1]}")
            else:
                warnings.append(f"J1 orphan OUTCOME (no ASSIGNMENT): {key[0]}/{key[1]}")
    for key in assigns:
        if key not in outcomes:
            warnings.append(f"J2 ASSIGNMENT awaiting OUTCOME (in flight?): {key[0]}/{key[1]}")

    print(f"records: {n_assign} assignment / {n_outcome} outcome | "
          f"violations: {len(violations)} | warnings: {len(warnings)}")
    for v in violations:
        print("VIOLATION", v)
    for w in warnings:
        print("warning  ", w)
    sys.exit(1 if violations else 0)


if __name__ == "__main__":
    main()
