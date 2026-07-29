---
name: probe-aws-capability
forged: true
forged_by: foxtrot
forged_date: "2026-07-27"
forged_from: gap-032
description: "Enumerates every AWS credential source on the executing box — each key pair in .env.local, each shared-credentials profile, and the ambient default chain — resolves each via sts get-caller-identity, ATTEMPTS a named AWS action with each, then emits a routing-ready verdict with per-identity ARN evidence and an explicit unprobed-boxes scope caveat. Use whenever the agent needs to know whether an AWS action is actually permitted: 'can I delete this', 'do we have permission to', 'check whether that action is denied', 'probe the AWS capability', 'which identity can do this', 'is this AccessDenied', or before writing or updating a capability-routing convention row. MUST use this skill instead of inferring permission from an ARN — identity equality predicts a denial, it does not prove one. Fires before any capability-absence claim about AWS."
user-invocable: false
triggers: [aws-capability, capability-probe, access-denied, accessdenied, permission-probe, iam-probe, can-i, credential-enumeration, sts-get-caller-identity, capability-routing, which-identity, is-it-denied]
tools_used: [Bash]
companion_scripts: [world/scripts/aws-capability-probe.sh]
conventions: [secrets, infrastructure, capability-routing]
minimum_mode: autonomous
revision_id: "skill-forge-probe-aws-capability-gap032"
previous_revision_id: null
---

# /probe-aws-capability — AWS Capability Probe

Answer "can this box actually perform AWS action X?" with **attempted evidence**,
never with an inference from an identity ARN.

Forged from `gap-032` after the same procedure was hand-rolled four times across
three agents (zeta `g-335-288`; alpha `g-335-290`, `g-335-292`, `g-115-3438`).

## The failure this exists to prevent

The hand-rolled form **under-enumerates, and does so invisibly**. On 2026-07-27
an agent probed the two credential sources it remembered and discovered a third
(the ambient default chain) only because a disproof gate demanded a falsifier.
All three happened to agree, so the shortfall left **no trace in the verdict** —
a wrong answer and a right answer look identical from the outside.

Enumeration is therefore structural in the companion script, not a thing the
author must remember. Two further properties follow from the same principle:

- **Never infer permission from an ARN.** Identity equality predicts a denial;
  it does not prove one. Only an attempted action is evidence.
- **The scope caveat is emitted automatically.** A per-box measurement silently
  promoted into a fleet-universal claim is the exact overclaim that had to be
  withdrawn by hand in `g-115-3438`.

## Restricted Operations

MUST use `world/scripts/aws-capability-probe.sh` — never raw `aws` calls with
hand-picked credentials, and never a hand-rolled enumeration loop. The script is
the access boundary: it reads credentials through the project's `.env.local`
loader, consumes each secret inside a per-source subshell, and never writes a
credential value to disk or stdout.

```
bash "$WORLD_DIR/scripts/aws-capability-probe.sh" \
    --action <iam-style-label> [--json] [--allow-effective] [--region <r>] \
    -- <aws-cli-args...>
```

Resolve `$WORLD_DIR` first: `source core/scripts/_paths.sh`. A bare
`bash world/scripts/...` fails rc=127 because `world/` is an external path and
the Bash hooks do not rewrite path arguments (`.claude/rules/path-resolution.md`).

## Procedure

1. **Name the action.** Pick the IAM-style label the capability-routing row will
   carry (`s3:DeleteObjectVersion`, `iam:SimulatePrincipalPolicy`, ...). This is
   `--action`; it is documentation, not what gets executed.

2. **Choose a SAFE probe target.** See "Safety" below. For a mutating verb this
   is the whole design decision — get it right before running anything.

3. **Run the companion script.** It enumerates all three source classes, resolves
   each identity, attempts the action with each, and classifies every result.

4. **Read the verdict, not the vibe.** `INCONCLUSIVE_INVALID_PROBE` means the
   action was never authorized-checked — treat it as "measured nothing", never as
   a denial. Re-target and re-run.

5. **Carry the evidence forward verbatim** into the capability-routing row,
   guardrail, or finding — including the scope caveat.

## Safety — mutating verbs

A capability probe **attempts the real action**, so on a mutating verb a success
is a real mutation. The script refuses any non-read-only verb (exit 2) unless
`--allow-effective` is passed.

**Preferred pattern: probe a NON-EXISTENT resource.** A denial still returns
`AccessDenied`; permission returns `NoSuchKey`/`404`/`ResourceNotFound`, reported
as the distinct verdict `PERMITTED_NOT_FOUND`. You learn exactly the same thing
and mutate nothing.

```bash
source core/scripts/_paths.sh
bash "$WORLD_DIR/scripts/aws-capability-probe.sh" \
    --action s3:DeleteObjectVersion --allow-effective --json \
    -- s3api delete-object --bucket "$BKT" \
       --key "__probe_nonexistent_$(date +%s)__" --version-id "null"
```

The target must still be **well-formed**. S3 validates argument shape *before*
authorization, so a malformed version-id returns `InvalidArgument` and the
permission question is never reached — the script reports that honestly as
`INCONCLUSIVE_INVALID_PROBE` rather than letting a malformed probe masquerade as
a finding.

This is `archive-before-delete.md` applied to probing: authorization sets the
goal, it does not set the method.

## Output contract

`--json` emits:

| field | meaning |
|---|---|
| `action` | the `--action` label |
| `box` | hostname the measurement is valid for |
| `overall` | `DENIED_ALL` · `PERMITTED_SOME` · `INCONCLUSIVE_INVALID_PROBE` · `INCONCLUSIVE_NO_USABLE_CREDENTIALS` · `INCONCLUSIVE` |
| `sources_enumerated` | count of credential sources found |
| `results[]` | per source: `source`, `source_kind`, `identity_arn`, `verdict`, `evidence` |
| `effective_probe` | whether `--allow-effective` was used |
| `scope_caveat` | the automatic unprobed-boxes disclaimer |

Per-source verdicts: `OK`, `AccessDenied`, `PERMITTED_NOT_FOUND`,
`NoCredentials`, `BadCredentials`, `InvalidProbe`, `OtherError`.

Without `--json` the same payload is followed by a markdown table in the shape
capability-routing rows consume.

### Verdict subtleties that carry real meaning

- **`NoCredentials` is not a denial.** A source that enumerates but carries no
  usable credential was never probed, so it can neither support nor refute a
  permission claim. It is excluded from the `DENIED_ALL` tally rather than
  padding it. A container with no instance role legitimately reports this for
  the ambient chain.
- **`DENIED_ALL` means every *usable* source was denied** — read
  `sources_enumerated` alongside it to see how much was actually exercised.

## Error handling

- **`aws` CLI absent** → exit 1 after reporting how many sources were enumerated;
  enumeration is still useful signal.
- **Identity unresolvable** → `identity_arn: UNRESOLVED`, and the action is still
  attempted. An unresolvable identity does not excuse skipping the attempt.
- **A single source failing** never aborts the run; every source is probed and
  reported (loud-failure-per-lane contract, g-115-1910).
- **Exit 2** is a refusal, not a failure: re-target at a non-existent resource,
  or pass `--allow-effective` deliberately.

## Chaining

- **Called by**: any goal establishing whether an AWS action is permitted;
  capability-routing convention edits; `archive-before-delete` recovery-layer
  verification; blocker creation that would claim an AWS permission wall.
- **Composes with**: `access-aws-services` (that skill *executes* AWS operations;
  this one *adjudicates* whether they are permitted).
- **Feeds**: `world/conventions/capability-routing.md` rows, guardrail evidence
  blocks, `capability-gate.py` routing decisions.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the `Bash` call running `world/scripts/aws-capability-probe.sh`
(or the follow-on Edit that writes its evidence into a capability-routing row).
Never end with a text summary.
