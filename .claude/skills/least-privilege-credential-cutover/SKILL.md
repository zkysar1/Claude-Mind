---
name: least-privilege-credential-cutover
description: "Moves a running service off a broad or root AWS credential onto a least-privilege scoped one, by enumerating exactly which IAM actions the service actually invokes. Use whenever the user says 'cut over to a scoped credential', 'least privilege', 'scope down these permissions', 'rotate off the root key', or 'what permissions does this service actually need'. Fires whenever an agent is about to author, narrow, or review an IAM policy for a service whose source it can read. MUST use this skill and its companion sweep rather than hand-listing actions — a hand-built list undercounts (a measured 16-vs-24 miss), and every missed action costs a full cutover cycle to rediscover one AccessDenied at a time."
forged: true
forged_by: zeta
forged_date: "2026-07-28"
forged_from: gap-030
user-invocable: false
minimum_mode: autonomous
tools_used: [Bash, Read, Edit]
companion_scripts:
  - world/scripts/iam-action-sweep.sh
conventions:
  - secrets
  - infrastructure
---

# least-privilege-credential-cutover

Move a running service off a broad/root AWS credential onto a scoped one,
without discovering the missing permissions one production failure at a time.

**Partial coverage is the failure mode.** Fixing only the first denied action
costs a full cutover cycle per remaining action: swap, redeploy, wait for the
next `AccessDenied`, repeat. The whole point of this procedure is to enumerate
the complete action set *before* the first swap.

## Restricted Operations

**MUST use `world/scripts/iam-action-sweep.sh` for action enumeration — never a
hand-written grep and never an import-based scan.** The sweep keys on
client-METHOD invocation sites. An import-based scan is blind to wildcard
imports and to clients passed in from a factory; that blindness produced a
measured 16-vs-24 undercount (rb-5288).

All credential reads go through `core/scripts/env-read.sh`. Consume the value
in the same shell invocation — never write it to disk, never echo it into a
log, never put it in a goal, journal, or tree node (guard-724).

## Procedure

### Step 1 — Enumerate the actions the service actually invokes

```bash
source core/scripts/_paths.sh
bash "$WORLD_PATH/scripts/iam-action-sweep.sh" --src <service-source-dir> --json
```

Read `unresolved` before anything else. Each entry is a client-shaped call site
with no resolvable binding — **a candidate missing action**. Resolve every one
(find where that client is constructed, or confirm it is not a boto3 client)
before proceeding. `--strict` exits 2 when any remain, so it can gate a script.

Treating `unresolved` as noise reintroduces exactly the undercount this step
exists to prevent.

### Step 2 — Split resource-scoped from account-level

The sweep already returns `resource_scoped` and `account_level` separately.
Honor the split.

**Account-level actions accept `Resource: "*"` ONLY** (rb-5268). `ListTables`,
`ListMetrics`, `DescribeRegions`, `ListBuckets`, `GetCallerIdentity` and their
siblings cannot live in a resource-scoped statement. A policy that puts them
there **validates cleanly and then denies at runtime** — the worst failure
shape, because the artifact looks correct.

The result is a two-statement policy: one scoped to real ARNs, one with
`Resource: "*"` carrying only the account-level actions.

### Step 3 — Simulate per action against REAL ARNs

> **⚠ NOT SATISFIABLE ON THIS FLEET (verified 2026-07-27, g-115-3438).**
> The original procedure called for `iam:SimulatePrincipalPolicy` against real
> ARNs (rb-2063). That probe is **DENIED to both principals on cc-02, cc-04 and
> cc-05**, and no box resolves to `:root` any more since the root-key eviction.
> The technique needs (a) a machine holding admin creds and (b) the simulate
> permission; neither holds. **Do not build a cutover plan that depends on this
> step** — it will fail at the probe, not at the policy.
>
> The caveat is stated here, at the point of the claim, rather than only in a
> convention file (guard-1606): a reader who reaches this step must not have to
> discover the denial by running it.

**Substitute:** Step 4's live-soak carries the verification weight instead. It
is strictly stronger evidence anyway — it exercises the calls the running code
actually makes, rather than the calls a policy simulator thinks it makes.

### Step 4 — Live-soak the exact calls the running code makes

With the candidate policy attached to the scoped principal, exercise the real
code paths — not a synthetic script that "makes the same calls." A synthetic
probe diverges from the production call shape and measures a branch production
never takes (`.claude/rules/probe-with-canonical-code-path.md`).

Watch for `AccessDenied` across a full duty cycle, including paths that only
fire on error handling or on a schedule.

### Step 5 — Swap the credential at its store

Locate the credential's store (EFS vault, `.env.local`, systemd
`EnvironmentFile`) and swap with **EXACT-key matching plus a backup**
(guard-1571).

Exact-key matching is load-bearing: a prefix or substring match on a key name
silently rewrites a *neighbouring* key. Back up the store file first and
byte-verify the restore path before the swap, not after.

### Step 6 — Verify from INSIDE the running service

```
GetCallerIdentity, executed by the service process itself.
```

**Never verify from the operator shell** (guard-1554). The operator shell has
its own credential resolution chain — env vars, profile, instance role — and
will happily report a *different* principal than the one the service resolved.
A green check from the wrong shell is the most convincing wrong answer
available.

## Error Handling

| Symptom | Meaning | Action |
|---|---|---|
| `unresolved_count > 0` | client-shaped call sites with no binding | Resolve each before writing the policy — do NOT proceed |
| Policy validates, denies at runtime | account-level action in a scoped statement | Re-split per Step 2 (rb-5268) |
| Simulate probe returns AccessDenied | expected on this fleet | Skip Step 3, rely on Step 4 live-soak |
| `AccessDenied` after cutover | incomplete enumeration | Re-run Step 1; check `unresolved` first |
| Verify passes from shell, service still fails | verified from the wrong process | Re-verify from inside the service (guard-1554) |

## Companion-script regression fixtures

`iam-action-sweep.sh` is a computation script, so it is gated by fixtures with
distinct verdicts (forge Step 3.6). Rebuild them as three source dirs:

- **PASS** — client bound in one file, used in another via wildcard import →
  actions enumerated, `unresolved: 0`, `--strict` rc=0
- **FAIL** — a `s3_client`-style var with no binding anywhere →
  `unresolved: 2`, `--strict` rc=2
- **EDGE** — `list_tables` + `list_metrics` alongside `get_item` →
  the first two land in `account_level`, the third in `resource_scoped`

A build that returns the same verdict on PASS and FAIL is vacuous — do not
trust it. The current fixtures caught three real defects in the sweep itself,
including a word-boundary regex that missed `s3_client` (the rb-5288 undercount
shape, reproduced inside the undercount detector).

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the Step 6 verification Bash call (`GetCallerIdentity` from
inside the service process), or the Step 1 `iam-action-sweep.sh` call when the
skill is invoked for enumeration only. Never end with a text summary.
