#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# test-capability-gate.sh — Regression test for capability-gate.py STOPWORDS + matching logic.
#
# Locks in session-47 B2 fixes so a future editor removing a "generic-looking" stopword
# (new/old/create/update/delete/exit) reverts the keyword-collision behavior (rb-246).
#
# Each case asserts expected matches + forbidden non-matches + would_block value.
#
# Exit codes:
#   0 — all pass
#   1 — one or more failures
#   2 — infra error
#
# Source:  (session-47 B2 review).

set -eu
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GATE="$SCRIPT_DIR/capability-gate.py"

[[ -f "$GATE" ]] || { echo "ERROR: $GATE missing" >&2; exit 2; }

# Source _paths.sh UNCONDITIONALLY so `python3` resolves cross-platform (native
# on Linux; the Microsoft-Store-stub-avoiding shim / py launcher on Windows —
# see _paths.sh python3 detection) AND $PROJECT_ROOT/$AGENTS_PARENT_DIR are set
# for the auto-bind glob below. (: previously sourced only inside the
# MIND_AGENT-unset branch, so the bound case — the normal path — ran without
# the python3 PATH setup and `command -v python` failed on cc-04.)
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh"

# Test-fixture: capability-gate reads world/conventions/capability-routing.md
# resolved through MIND_AGENT → local-paths.conf → WORLD_DIR. With no agent
# bound, the gate fail-opens by design (rb-347: one fail-open boundary), so
# every "matches expected" assertion silently passes as matches=[] and every
# "should block" assertion fails as would_block=False. This caused the false
# /verify-learning 12/14 regression report on 2026-04-22.
#
# Fix: auto-bind to the first agent in the repo that has a readable
# local-paths.conf. If none exist, skip with a clear message rather than
# silently running under the fail-open path and producing misleading PASS/
# FAIL output.
if [[ -z "${MIND_AGENT:-}" ]]; then
    # Resolve PROJECT_ROOT + AGENTS_PARENT_DIR from the canonical helper rather
    # than hardcoding the agent-parent layout (2). The prior bare
    # "$PROJECT_ROOT"/*/ walk looked for local-paths.conf as a DIRECT child of
    # PROJECT_ROOT, but under AGENTS_PARENT_DIR=agents the confs live one level
    # deeper at agents/<name>/local-paths.conf — so the walk matched none,
    # AUTO_BOUND stayed empty, and the gate SKIPped (exit 0) instead of binding,
    # silently defeating the auto-bind this block exists for. Sourcing _paths.sh
    # is safe with MIND_AGENT unset (it falls through to the first agent, never
    # hard-fails) and mirrors session-save-id.sh's $PROJECT_ROOT/$AGENTS_PARENT_DIR glob.
    # _paths.sh already sourced unconditionally at the top of this script, so
    # $PROJECT_ROOT/$AGENTS_PARENT_DIR are in scope here ().
    AUTO_BOUND=""
    for conf in "$PROJECT_ROOT/$AGENTS_PARENT_DIR"/*/local-paths.conf; do
        [[ -f "$conf" ]] || continue
        AUTO_BOUND="$(basename "$(dirname "$conf")")"; break
    done
    if [[ -z "$AUTO_BOUND" ]]; then
        echo "SKIP: no agent with local-paths.conf found under $PROJECT_ROOT/$AGENTS_PARENT_DIR — capability-routing.md unreachable without an agent binding. Bind an agent via /start <name> first." >&2
        exit 0
    fi
    export MIND_AGENT="$AUTO_BOUND"
    echo "test-capability-gate: auto-bound MIND_AGENT=$AUTO_BOUND for capability-routing.md resolution" >&2
fi

# _paths.sh (sourced above) guarantees `python3` resolves cross-platform:
# native on Linux, or the Microsoft-Store-stub-avoiding shim / py launcher on
# Windows. Use python3, never bare `python` (absent on Linux; a Store stub on
# Windows) — per CLAUDE.md "Use python3 only inside .sh scripts that source
# _paths.sh". One resolver, one behavior — no fallback chain (rb-347).
command -v python3 >/dev/null 2>&1 || { echo "ERROR: no python3 on PATH — _paths.sh shim not active" >&2; exit 2; }

# All assertion logic in Python for reliability.
python3 -u - "$GATE" <<'PYEOF'
import json, subprocess, sys

gate = sys.argv[1]
cases = [
    # (name, failure_reason, required_matches, forbidden_matches, expected_would_block)
    (
        "a_ssh_host_key_no_add_npc_task",
        "SSH host key mismatch for operator.example.com - new ED25519 fingerprint needs user trust confirmation before known_hosts rewrite",
        {"access-efs-data"},
        {"add-npc-task"},
        True,
    ),
    (
        "b_processor_run_failure",
        "processor-run.sh launch failed: llama-server-8082 not responding on GPU node",
        {"run-processor"},
        set(),
        True,
    ),
    (
        "c_user_trust_confirmation",
        "need user trust confirmation for credential rotation",
        set(),  # no specific skill required, just policy expectations
        {"add-npc-task"},  # but stopwords shouldn't trigger false matches
        # Flipped True→False (3 triage, 2026-07-16): the comment
        # "capability-routing.md row on credentials triggers block" was never
        # true — the only credentials row is Genuinely-Human-Only ("Must be
        # created in AWS IAM by user"). The block actually rode a sole
        # generic-token match ('rotation' → the SSH-to- row's "host-key
        # rotation" prose), which the  precision rule (2026-07-13)
        # correctly suppresses. Trust-confirmation for credential rotation is
        # a genuine security-trust user-leg — NOT blocking user-routing here
        # is the correct policy.
        False,
    ),
    (
        "d_stopwords_dont_match_alone",
        "exit code 7 failed new attempt create update delete old",
        set(),
        {"add-npc-task", "run-processor", "access-efs-data"},
        False,
    ),
    # Session-48 C1 dry-run additions (): defer narratives where
    # generic English words previously produced FPs. These lock in the
    # stopword expansion so removing any of access/fresh/live/cannot/still/
    # present/pending/awaiting/requires/control/deployment/evaluation
    # reverts the C1 sweep to its 17/0 FP state.
    (
        "e_fresh_session_not_fresh_eyes",
        "Multi-hour test-implementation work (500-2700 line files) needs fresh session context",
        set(),
        {"fresh-eyes-review"},  # "fresh" must not match fresh-eyes-review
        False,
    ),
    (
        "f_player_presence_not_a_skill",
        "Requires live game session with player present agent cannot control player presence",
        set(),
        {"access-efs-data", "access-operator-api"},  # cannot/still/live are not signals
        False,
    ),
    (
        "g_time_wait_not_analysis",
        "Need ~4 weeks of post-change telemetry accumulated before evaluation is meaningful",
        set(),
        {"analyze-npc-behavior"},  # "evaluation" must not match analyze-npc-behavior
        False,
    ),
    (
        "h_dependency_chain_narrative",
        "Only 1 session with Strategy 3.5 active (need 3+). Awaiting asp-238 deployment for next",
        set(),
        {"access-email"},  # "deployment" must not match access-email
        False,
    ),
    # Guard the OTHER direction: legit technical tokens must still match.
    (
        "i_legit_processor_still_matches",
        "awaiting CI/Processor pipeline after iter-20 commits",
        {"run-processor"},
        set(),
        True,
    ),
    (
        "j_legit_efs_still_matches",
        "EFS logs pending on bastion — need to pull via efs-ssh",
        {"access-efs-data"},
        set(),
        True,
    ),
    # Session-48 context-aware disqualification rules ( follow-up).
    # Negation-by-means, target-of-future-work, target-of-accumulation.
    # Removing any rule from _keyword_is_invocation_signal breaks these.
    (
        "k_negation_by_means_via",
        # `api` is the means being ruled out ("cannot be triggered via API") —
        # must NOT match access-operator-api. The defer still legitimately
        # mentions Roblox Studio (which IS agent-capable via access-roblox-studio),
        # so would_block may still be True on that independent match. The rule
        # being tested is narrow: negation-by-means removes the `api` keyword,
        # not whether the overall defer gets blocked on other grounds.
        "Requires Roblox Studio (local desktop app) — cannot be triggered via API. Needs human-operated assistant mode",
        set(),
        {"access-operator-api"},
        True,  # roblox/studio legitimately match; only the api FP is filtered
    ),
    (
        "l_target_of_future_work",
        "Let predicate.py bake on the precondition path for ~4 weeks before refactoring curriculum.py",
        set(),
        {"curriculum-gates"},
        False,
    ),
    (
        "m_target_of_accumulation",
        "observation window requires 3-5 new /create-aspiration invocations first",
        set(),
        {"create-aspiration"},
        False,
    ),
    # Critical anti-regression: "cannot access X" must STILL match (means vs
    # ends distinction). An agent writing "cannot access " is exactly
    # the kind of defer the gate exists to catch — dropping this test means
    # future edits could accidentally block the gate's primary purpose.
    (
        "n_cannot_access_is_still_a_match",
        "cannot access EFS, need user to check bastion",
        {"access-efs-data"},
        set(),
        True,
    ),
]

passed = failed = 0
results = []

for name, reason, required, forbidden, expected_block in cases:
    # sys.executable is the running interpreter — guaranteed to work for
    # invoking capability-gate.py. No fallback chain: if this fails, the
    # test harness has a real infra problem that fallbacks would hide.
    proc = subprocess.run(
        [sys.executable, gate,
         "--failure-reason", reason,
         "--intended-participants", "user",
         "--output", "json"],
        capture_output=True, text=True,
    )

    if not proc.stdout.strip():
        results.append(f"FAIL: {name} — no stdout (stderr: {proc.stderr[:200]})")
        failed += 1
        continue

    try:
        d = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        results.append(f"FAIL: {name} — JSON parse error: {e}")
        failed += 1
        continue

    matched = {m.get("skill") for m in d.get("matches", []) if m.get("skill")}
    would_block = d.get("would_block", False)
    missing = required - matched
    unexpected = forbidden & matched
    ok = not missing and not unexpected and would_block == expected_block

    if ok:
        results.append(f"PASS: {name} — matched={sorted(matched)} would_block={would_block}")
        passed += 1
    else:
        issues = []
        if missing:   issues.append(f"missing={sorted(missing)}")
        if unexpected: issues.append(f"forbidden_matched={sorted(unexpected)}")
        if would_block != expected_block:
            issues.append(f"would_block={would_block}!={expected_block}")
        results.append(f"FAIL: {name} — matched={sorted(matched)} — {'; '.join(issues)}")
        failed += 1

print("\n=== capability-gate regression test ===")
for r in results:
    print(r)
print(f"\nPASS: {passed}   FAIL: {failed}")
sys.exit(0 if failed == 0 else 1)
PYEOF
