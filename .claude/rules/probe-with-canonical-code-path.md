---
description: "Probe with the skill's companion script in its production call shape (same env, args), never a synthetic probe: wrong shape proves nothing."
---

<!-- domain-leak-exempt: deliberate pedagogical examples — efs-ssh.sh, aws-exec.sh, operator.ayoai.com appear in the wrapper-vs-synthetic-probe comparison table where the framework's failure mode is demonstrated by NAMING the actual wrappers that hide the StrictHostKeyChecking flag / .env loading / auth header. Genericizing to `<wrapper>.sh` would lose the "look at WHY plain-ssh diverges" specificity that makes the rule learnable. -->
# Probe With the Canonical Code Path

## Principle

When diagnosing whether a skill is blocked, or whether an external service is
available, probe using the skill's **own companion script** — not a synthetic
equivalent you construct on the fly. Different code paths set different option
flags, wrappers, and environment. A probe that diverges from the skill's real
code path measures something the skill will never encounter, and produces
false-positive blockers.

## The Canonical Probe Rule

1. Open the skill's SKILL.md front matter and read `companion_scripts:`.
2. The first script listed (or the one most directly exercising the failure
   mode) is the canonical probe. Invoke it with a trivial success argument:
   ```
   source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/efs-ssh.sh" "echo ok"
   ```
   The `$WORLD_PATH` resolution is load-bearing, not cosmetic: `world/` is an
   EXTERNAL path and the Bash hooks do not rewrite path arguments, so a bare
   `bash world/scripts/...` dies rc=127 "No such file or directory" — which
   reads exactly like a dead connection and produces the very false-positive
   blocker this rule exists to prevent.
3. If that succeeds, the skill is not blocked. If it fails, the failure
   output is your real diagnostic signal.
4. Only if the canonical probe is unavailable may you fall back to a
   synthetic probe — and that synthetic probe MUST be documented as such in
   the blocker's `diagnostic_context`.

## Canonical BINARY Is Not Canonical INVOCATION

Running the right script is half the rule. The other half is running it with the
**call shape the production caller uses** — same env vars, same args. A canonical
script invoked with a non-production arg shape exercises a branch production never
takes, and because the output is genuine output from the genuine script, it reads
as authoritative. This is the letter/intent split that makes the class
self-concealing: the "did I use the canonical script?" reflex fires and reports
all-clear, so nothing prompts a second look.

Before treating a hand-run script's output as evidence:

1. **Grep the production call site** and diff its env/args against yours. One grep.
   (`grep -n '<script-name>' core/scripts/*.sh .claude/skills/*/SKILL.md`)
2. **Read the emitter for CONDITIONAL fields.** A key that appears only on one
   branch is a fingerprint of which branch ran. Its *absence* is signal, not noise.
3. **Run wrong-shape and right-shape side by side in one turn.** The diff is the
   positive control, and it is seconds of work.
4. **Check whether your SHELL is the wrong shape.** Your interactive Bash is not the
   script environment. The user profile can define shell FUNCTIONS and aliases that
   shadow standard commands, and those are NOT exported — so `bash script.sh` and
   every child process resolve the real binary instead. `type -t <cmd>` reports
   `function` in your shell and `file` inside a script; confirm with
   `bash -c 'type -t <cmd>'` before trusting any hand-run predicate.

   This axis fails in BOTH directions, which is why it is worth a separate check:
   it can hand-test GREEN on something broken (guard-1742 — the hook-wrapper env-var
   case) *or* hand-test RED on something healthy. Measured g-115-3794: `grep -qv` on
   empty stdin returns 0 under a profile-defined `grep` function wrapping ugrep, and
   1 under the GNU grep that every script actually gets. The false alarm reached a
   committed rationale comment before re-measurement in script context caught it —
   the code was fine; only the stated reason was wrong.

Canonical incident (g-115-3260, 2026-07-26): `post-state-update-gate.sh` was
hand-run with no `GOAL_ID` and returned `{"fired": false, "core_count": 0}`. That
became a HIGH Unblock declaring the fresh-eyes gate "structurally dead." But
`iteration-close.sh:1212` *always* passes `GOAL_ID`, and the gate emits
`commits_scanned` only when committed scope resolves — its absence from the quoted
verdict was proof the measurement came from a branch the loop never reaches. Re-run
with `GOAL_ID` set: `fired:true, core_count=3, commits_scanned=2`, on three real
commits, under the exact condition the goal claimed was uncovered. A speculative fix
was written and reverted before measurement corrected the premise. Encoded as
`rb-5235`; structurally identical to `guard-920` (regression tests must replicate
the literal production arg shape, not the contract-ideal one) — same defect moved
from tests to diagnostics.

## Why Synthetic Probes Mislead

Canonical skill wrappers frequently include ceremony that raw commands lack:

| Example | Wrapper behavior | Synthetic probe misses |
|---------|------------------|------------------------|
| `efs-ssh.sh` | Owns the operator transport — and does not tell its callers what that transport is. Currently AWS SSM via `ssm-run.sh`, plus the user/HOME/cwd/exit-code/stdin shims SSM does not give for free | Plain `ssh host` cannot reach the operator **at all**: the world-open port-22 ingress on `ayoai-operator-sg` was removed 2026-08-06 (g-335-852). A raw probe now fails with a connection error that reads exactly like an outage — the strongest form of this rule's thesis |
| `aws-exec.sh` | Loads `.env.local` credentials via `_env.sh` | Plain `aws` may use wrong profile or missing credentials |
| `operator-api.sh` | Adds `AYOAI-API-KEY` header | Plain `curl` returns 401 unauthenticated |

In each case the synthetic probe can produce a failure that **the real code
path would never see**. Filing a blocker based on the synthetic failure
blocks goals that were never blocked.

## Session-47 Incident

A synthetic `ssh operator.ayoai.com` probe saw a host-key mismatch at
`~/.ssh/known_hosts:311` and produced `pq-ssh-host-key-operator`, claiming
to block six skills. All six skills use `efs-ssh.sh` or HTTPS — none touch
`~/.ssh/known_hosts`. The real block did not exist. The agent slept eight
backoff cycles (~4h wall-clock) on a non-problem. Encoded as `guard-147` and
`rb-246`.

## Scope

Applies to any diagnostic action that could produce a blocker or mark
infrastructure unavailable:
- `aspirations-execute` CREATE_BLOCKER Phase 4.1e
- `aspirations-precheck` Phase 0.5a pre-selection guardrail check
- Phase 0.5b blocker resolution re-probe
- `infra-health.sh check <component>` paths
- Any inline diagnostic from a failing goal execution

Also applies symmetrically when clearing a blocker: re-probe with the
canonical path before declaring resolution.

## Enforcement

- `guard-147` (action hint: before filing a blocker, confirm the probe used
  is the skill's canonical companion script).
- `rb-246` (reasoning bank entry cross-linking to `rb-225` — both are
  instances of "wrong code path ≠ right failure mode").
- `core/scripts/blocker-create-gate.py` (canonical_probe check) — runs as
  Step 2.55 of CREATE_BLOCKER. Reads each affected skill's SKILL.md
  `companion_scripts:` front matter (via `core/scripts/_skill_md.py`) and
  verifies `--probe-command` invokes at least one listed script. Exits 1
  with a specific reason on mismatch; `--override-blocker-gate
  "<justification>"` bypasses and logs to
  `world/blocker-gate-overrides.jsonl`. Skipped for HUMAN_ONLY_BLOCKER_TYPES
  (security-trust, credentials-required, physical-hardware, user_action) —
  those have no companion_script path.
