# Rationale: Probe With the Canonical Code Path — incidents and measured cases

Referenced from `.claude/rules/probe-with-canonical-code-path.md`. That rule
keeps the imperatives; this file holds the incident narratives, measured cases,
and the wrapper-vs-synthetic-probe comparison table.

## The g-115-3794 shell-function measurement (rule 4 — "Check whether your SHELL is the wrong shape")

This axis fails in BOTH directions, which is why it is worth a separate check:
it can hand-test GREEN on something broken (guard-1742 — the hook-wrapper env-var
case) *or* hand-test RED on something healthy. Measured g-115-3794: `grep -qv` on
empty stdin returns 0 under a profile-defined `grep` function wrapping ugrep, and
1 under the GNU grep that every script actually gets. The false alarm reached a
committed rationale comment before re-measurement in script context caught it —
the code was fine; only the stated reason was wrong.

## Canonical incident: canonical BINARY is not canonical INVOCATION (g-115-3260, 2026-07-26)

`post-state-update-gate.sh` was hand-run with no `GOAL_ID` and returned
`{"fired": false, "core_count": 0}`. That became a HIGH Unblock declaring the
fresh-eyes gate "structurally dead." But `iteration-close.sh:1212` *always*
passes `GOAL_ID`, and the gate emits `commits_scanned` only when committed scope
resolves — its absence from the quoted verdict was proof the measurement came
from a branch the loop never reaches. Re-run with `GOAL_ID` set: `fired:true,
core_count=3, commits_scanned=2`, on three real commits, under the exact
condition the goal claimed was uncovered. A speculative fix was written and
reverted before measurement corrected the premise. Encoded as `rb-5235`;
structurally identical to `guard-920` (regression tests must replicate the
literal production arg shape, not the contract-ideal one) — same defect moved
from tests to diagnostics.

## Why Synthetic Probes Mislead — wrapper comparison table

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

A synthetic `ssh operator.example.com` probe saw a host-key mismatch at
`~/.ssh/known_hosts:311` and produced `pq-ssh-host-key-operator`, claiming
to block six skills. All six skills use `efs-ssh.sh` or HTTPS — none touch
`~/.ssh/known_hosts`. The real block did not exist. The agent slept eight
backoff cycles (~4h wall-clock) on a non-problem. Encoded as `guard-147` and
`rb-246`.

## Cross-references

- `.claude/rules/probe-with-canonical-code-path.md` — the imperatives this file explains
- rb-5235 — the g-115-3260 encoding (BINARY ≠ INVOCATION)
- guard-920 — regression tests must replicate literal production arg shape
- guard-147, rb-246 — Session-47 incident (synthetic SSH probe)
- guard-1742 — hook-wrapper env-var case (hand-test GREEN on something broken)
- g-353-151 — the context-floor diet that moved this text out of the always-loaded rule
