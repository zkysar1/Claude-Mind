# GRANT Store — Schema and Script API

A **GRANT** is a directed, subtree-scoped edge from one world to another. It is
the framework's single sharing/membership primitive: the same record expresses
"world A may influence world B", "base world A is a member of environment B",
and "reader B may read subtree S of A". One primitive, three readings — there is
deliberately no second store for any of them.

- **Policy SSOT**: `core/scripts/_grants.py` — PURE (no `_fileops` import), so a
  gate evaluating authorization can never bind a caller's storage backend.
- **Script API**: `core/scripts/grants.sh` → `core/scripts/grants.py` — the IO
  half (locking, history, changelog).
- **Guardrail model**: G1-G5, specified in
  `core/config/conventions/world-contract.md`.
- **Architecture**: the `portable-agent-mind-architecture` tree node.

## Store location

```
<WORLD_DIR>/grants.jsonl
```

The **filename** is fleet-canonical (`_grants.STORE_FILENAME`); the
**directory** is not, and must never become a module constant. `world/` is an
external, per-agent configured path (`.claude/rules/path-resolution.md`), so a
hardcoded directory would be wrong on every box but the author's.
`_grants.default_store_path(world_dir)` takes the already-resolved world dir as
an argument, which is what keeps the policy module pure.

**An ABSENT store is not an error.** A world that has correctly never been
granted anything has no file, and that state must read as *zero grants* (G1
default-private), never as *unavailable*. Conflating the two is how
default-private silently becomes default-public.

## Record schema

| Field | Required | Meaning |
|---|---|---|
| `grant_id` | yes | Stable id for the edge. Allocated `grant-N` when absent. |
| `from_env` | yes | Source world's `environment_id`. For a membership, the agent's BASE world. |
| `to_env` | yes | Target world's `environment_id`. For a membership, the ENVIRONMENT joined. |
| `status` | yes | `active` gates the grant; any other value denies. Defaults to `active`. |
| `origin_env` | yes | G5 provenance. Must equal `from_env`. Defaults to `from_env`. |
| `approved_by` | conditional | G3 human approver. An `agent:` / `bot:` / `system:` prefix is refused. |
| `approved_at` | conditional | G3 approval timestamp. |
| `scope` | no | Knowledge-tree node key this grant reaches; covers that node AND its descendants. Absent or `/` means the whole tree. |
| `created` | no | Creation stamp; written when absent. |

**`scope` matches on the separator boundary, never a bare string prefix.** A
prefix match would make the scope `a/agent` also cover the sibling subtree
`a/agent-secrets` — silent over-granting, invisible because the common cases all
look right. A scope is an authorization boundary, not a search filter.

**Absent `scope` WIDENS to root rather than narrowing to nothing.** A grant
written before scoping existed must not silently become unusable. This is the
one place absence is permissive, and it is safe because the authorization
decision (may A reach B at all?) has already been made under G1 default-deny —
scope only narrows an already-granted edge.

## Membership IS a grant

An agent's base **is a world**, so "being in an environment" is holding an
active grant into that environment-world. Membership is therefore a
**projection** of the grant rows, never a stored field and never a second file:

```
memberships(base) == { g.to_env for g in grants if g.from_env == base and g.status == "active" }
```

Consequences worth stating, because each is a question a reader will ask:

- **Multi-environment membership needs no new mechanism.** N active grants out
  of one base ARE membership in N environments.
- **Revoking a grant revokes the membership** — same row, same event. There is
  no separate membership lifecycle to fall out of sync.
- **Membership is a SET, not an edge count.** One base may hold several grants
  into one environment at different scopes; that is one membership with several
  scopes, and `grants.sh memberships` reports both.

## Script API

```bash
bash core/scripts/grants.sh add < record.json
bash core/scripts/grants.sh list [--from-env X] [--to-env Y] [--covering NODE] [--status S|any]
bash core/scripts/grants.sh memberships --base-env X [--status any]
bash core/scripts/grants.sh check --from-env X --to-env Y [--node KEY]
bash core/scripts/grants.sh reach --base-env X [--status any] [--registered-env E ...]
```

`--store PATH` overrides the resolved store (tests, auditing a peer's export).

**`add` reads the record as JSON on STDIN — never as a flag or a positional**
(guard-979 / guard-2037). A `$(cat)`-style reader handed its payload any other
way blocks forever on a stdin that never closes; `grants.py` refuses a TTY
stdin fast and names the correct shape, but redirect from a file at the call
site and the question never arises.

Everything that depends on existing state — id allocation, the G3
first-grant-from-this-source test, the G4 cycle check, the duplicate-id test —
runs INSIDE the write lock (guard-480). Validating before the lock would decide
against a snapshot a concurrent writer can invalidate, letting two callers each
believe they are the first grant from a source so that neither needs the human
approval G3 exists to require.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success / `check` returned ALLOW |
| 2 | the grant was REFUSED — the response carries a `violations` list, and nothing was written |
| 3 | `check` returned DENY — the store read fine and policy says no (G1 working) |
| 4 | `check` returned UNAVAILABLE — the gate's OWN dependency failed; the caller fails OPEN and logs |
| 1 | an IO/plumbing error |

**3 and 4 are different on purpose.** DENY must fail closed (G1); UNAVAILABLE
must fail open (guard-142). Collapsing them breaks the gate in whichever
direction the author happened to pick: a caller that cannot tell them apart must
either ignore a real refusal or block real work on its own plumbing fault.

## Reach: membership joined against the deployment registry

Two sources describe which environments a base relates to, and they are not the
same thing:

| Source | Answers | Lives in |
|---|---|---|
| the grant store | *am I a MEMBER of it?* (authorization) | `<WORLD_DIR>/grants.jsonl` |
| the environment registry | *can I ADDRESS it?* (deployment) | `core/config/environments/*.yaml` |

Neither implies the other, and until they are joined nothing reports where they
disagree. `reach` is that join:

```bash
bash core/scripts/grants.sh reach --base-env X [--status any] [--registered-env E ...]
```

| Field | Meaning |
|---|---|
| `member_of` | registered environments this base holds a grant into — the real multi-environment membership |
| `ungranted` | registered peers with NO grant: **the seed list** |
| `dangling` | grants naming environments the registry does not list |
| `seed_required` | `len(ungranted)` — the number the seed-then-arm order keys on |

`member_of` INTERSECTS the two sets rather than reporting grants, because a
grant naming a world this deployment cannot address is not reach. Overstating
reach is the expensive direction: it is exactly what would make an arming
decision look safe when it is not. `dangling` is not an error — a peer can be
retired from the registry while its grant survives — but it is never reachable.

The base is excluded from all three lists (a world is not its own peer), and
that rule lives in `_grants.reach` rather than in the caller so there is one
place it can be got wrong.

**Why `seed_required` matters.** `world-contract.md` fixes the order as "seed
the grants that describe existing relationships, THEN arm", because G1 starts
every world at zero grants and arming refusal against an empty store would
instantly sever the cross-deployment board channel that has been live since
2026-06-02. While `seed_required` is non-zero, arming severs live
relationships — so this number, not a judgement call, is the gate on that
decision.

**Purity split, same as `memberships`.** The JOIN is policy and lives in the
pure `_grants.reach(grants, base_env, registered_envs)`, which takes the
registry **as data** and never reads it: `core/config/environments/` needs a
YAML parser and a path resolver, and importing either would end the purity that
lets a gate use this module without binding the caller's storage backend. The
READ is IO and lives in `grants.py`. `--registered-env` overrides that read the
way `--store` overrides the store, so a test never depends on one box's
registry.

## Known asymmetry: G3 at creation vs G3 at use

`validate_new_grant` requires a human approver only for the **first** grant out
of a given `from_env`. `check_influence` requires one on **every** grant at use
time. So a second grant can be created legitimately and still be denied
influence forever.

This is recorded rather than quietly patched. It fails **closed**, which is the
safe direction, and the strict use-time rule is the one that actually protects
the property G3 exists for — without it an agent could self-mint unlimited
edges to new targets after a single human-approved grant existed. The permissive
creation rule is the weaker half.

`grants.sh memberships` therefore prints the gate's verdict beside each
membership (`influence`, and an `authorized` list). A resolver that printed only
the edge would let a reader believe access exists that the gate refuses — which
is exactly the drift a single-store design is supposed to make impossible.
Reconciling the two G3 rules is a successor decision, not this API's to make.

## Cross-references

- `core/config/conventions/world-contract.md` — G1-G5, `ENVIRONMENT_ID`,
  `COMMONS_POLICY`, and the BUILT/WIRED status of the grant entity
- `core/scripts/_grants.py` — the pure policy module (docstring carries the
  DENY-vs-UNAVAILABLE rationale in full)
- `core/scripts/tests/test_grants.py` — policy tests;
  `core/scripts/tests/test_grants_cli.py` — store/API tests
- `core/config/conventions/cross-deployment-channel.md` — the peer channel the
  grant edge authorizes writes onto
- `.claude/rules/path-resolution.md` — why the store's directory is a caller
  argument and not a constant
