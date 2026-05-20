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
   bash world/scripts/efs-ssh.sh "echo ok"
   ```
3. If that succeeds, the skill is not blocked. If it fails, the failure
   output is your real diagnostic signal.
4. Only if the canonical probe is unavailable may you fall back to a
   synthetic probe — and that synthetic probe MUST be documented as such in
   the blocker's `diagnostic_context`.

## Why Synthetic Probes Mislead

Canonical skill wrappers frequently include ceremony that raw commands lack:

| Example | Wrapper behavior | Synthetic probe misses |
|---------|------------------|------------------------|
| `efs-ssh.sh` | `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null` | Plain `ssh host` respects `~/.ssh/known_hosts` and can fail on host-key rotation that the wrapper ignores |
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
