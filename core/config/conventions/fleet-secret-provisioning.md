# Fleet Secret Provisioning (Bootstrap-Key → Vault Self-Service)

How a fleet-hosted mind container self-services its entire credential set from a
single pre-seeded bootstrap secret, eliminating manual per-container secret
distribution. This file formalizes the **mechanism** as a domain-free framework
capability; the executable provisioner that instantiates it for a specific
deployment is a `core/scripts/` script that opts out of the domain-token sweep as
sanctioned for functional domain strings (see "Domain routing" below).

## The capability

Standing up a fresh fleet node has three secret-bearing prerequisites: the node
needs (1) its credential set (`.env.local`), (2) the framework code, and (3) the
home repo of the agent it will host. This capability collapses the first from
"push N secrets to the container by hand" to "seed ONE bootstrap key; the
container pulls the rest itself."

Net effect: **one bootstrap secret in → a complete, mode-600 `.env.local` out**,
with every downstream credential fetched from a remote vault the operator already
controls. No secret value is ever printed, logged, or written anywhere except the
final `.env.local`.

## The mechanism (five steps)

1. **Bootstrap secret** — a single credential is pre-seeded on the container at a
   known path (the ONLY manual step). It grants read access to the vault and
   nothing else. Everything below is self-serviced.
2. **Reach the vault** — the provisioner connects to the operator host that fronts
   the credential store, using the deployment's canonical remote-shell wrapper (or
   its exact connection flags). It does NOT hand-roll a raw connection — the
   canonical path carries the host-key and auth ceremony a raw command misses
   (`.claude/rules/probe-with-canonical-code-path.md`).
3. **Read into memory** — the vault file is read into a shell variable, never to an
   intermediate file on disk. It is never echoed to stdout/stderr.
4. **Map + write** — vault entries are transformed to the container's env names via
   an **env-prefix mapping** (see "Vault-file contract"), merged with the
   deployment's non-secret config, and written to `.env.local` at mode `600` in a
   single write.
5. **Verify (values-blind)** — the provisioner prints the KEY NAMES it wrote plus a
   per-credential `OK`/`EMPTY` presence check. It NEVER prints a value. This is the
   only output; a reader can confirm coverage without ever seeing a secret.

## Invariants

1. **Single pre-seeded secret.** Exactly one credential is manual; the bootstrap
   key is the root of trust for everything else. Adding a second manual secret
   defeats the capability.
2. **In-memory only.** Vault contents live in process memory for the duration of
   the run. No plaintext intermediate file. No swap to disk.
3. **Never print values.** No step echoes a secret value — not in logs, not in the
   verify step, not in an error path. Error messages name the KEY, never the value.
4. **Mode 600 on write.** `.env.local` is written owner-read/write only, in one
   operation (not created world-readable then chmod'd).
5. **Verify is values-blind.** The verify/summary output is KEY NAMES + `OK`/`EMPTY`
   only. This is the contract that lets the run be observed safely.
6. **FROM-state guard (idempotent, inert where done).** The provisioner checks its
   starting state and is a safe no-op (or explicit refuse-without-`--force`) when
   `.env.local` is already fully provisioned, or when the bootstrap key is absent.
   This makes it safe to re-run and safe to ship onto a node that does not need it.
   **An ADDITIVE mode belongs inside this invariant, not beside it.** The guard
   exists to stop a TRUNCATING re-run, so a mode that can only append or rotate a
   single key is outside what it defends and must bypass it — otherwise the guard
   blocks the one safe way to change a provisioned file. And the refusal MUST name
   that additive path: a guard whose only stated remedy is the destructive flag
   *teaches* the destructive flag. Measured (gap-054): three agents on three boxes
   inside nine hours each had to read the script, recognise the trap, and disbelieve
   its own error message before hand-building the safe append; a convention file
   documenting the trap did not stop the third. The additive mode preserves every
   invariant above — in-memory only, values-blind, mode 600 — and adds one of its
   own: verify by RE-READING the file and diffing key counts, never by trusting the
   write's own echo.
7. **The env-prefix mapping is the contract.** The vault stores keys under a stable
   prefix scheme; the provisioner's mapping table is the single source of truth for
   which vault key becomes which container env var. Changing the mapping is a
   breaking change to the vault format.

## Vault-file contract

The vault is a flat `KEY=VALUE` file (one entry per line, `#` comments allowed).
Keys fall into two classes:

- **Secret, env-prefixed** — a per-environment prefix namespaces the credential so
  one vault can serve many environments without collision. The provisioner strips
  the prefix to derive the container env name (e.g. a `<ENV>_<SERVICE>_*` vault key
  maps to the container's `<SERVICE>_*` daemon env var). The exact prefix and the
  full mapping table live in the deployment's provisioner script, cross-checked
  against the deployment's `.env.example` (the authoritative key surface).
- **Non-secret config** — deployment wiring that is not sensitive (backend
  selection, environment id, resource names, region, machine id, remote paths).
  These are copied through, not prefix-stripped.
- **Secret, agent-scoped** — a credential that must DIFFER per agent. The
  env-prefix namespaces by ENVIRONMENT, and the environment id is
  per-DEPLOYMENT, not per-agent, so every agent on a deployment resolves the
  same prefix and therefore the same entries. That is correct for a shared
  daemon credential and wrong for a per-agent one, where two agents must not
  receive the same value. Such a key carries a trailing agent scope:
  `<ENV>_<CONTAINER_KEY>__<AGENT>` (agent uppercased), resolving to
  `<CONTAINER_KEY>` on that agent's box only.

### Agent-scope resolution rules

1. A scoped entry for the bound agent **overrides** its generic sibling.
2. A scoped entry for **any other** agent is never written to this box. This is
   the security-critical rule: one shared vault must not place agent A's
   credential on agent B's box.
3. An unscoped entry is generic and applies to every agent — unchanged
   behavior, so a vault with no scoped entries maps exactly as it did before.
   The extension is backward-compatible by construction.
4. With no agent bound (`MIND_AGENT` unset), **no** scoped entry resolves and
   every one is skipped. Fail-safe: never guess whose credential this is.
5. `__` is RESERVED as the scope separator, so a container env name must not
   contain it. No key in the current surface does.

Resolution is two-pass, so precedence between a scoped entry and its generic
sibling never depends on line order in the vault — that ordering is not part of
the contract and must not decide which credential a box receives. The guarantee
is scoped-vs-generic only: two entries at the SAME scope (a base name duplicated
within the vault) are both emitted, so the last one wins on load and the
duplicate is counted twice in the verify summary. Treat a duplicated base name
as an operator error the provisioner does not currently detect.
The verify block tags each resolved key `(agent-scoped: <AGENT>)`,
which stays inside the values-blind contract (invariant 5) because it is a
key-name-level fact, and lets an operator confirm scoping fired rather than
silently falling back to generic.

Because this capability is **formalized, not ported verbatim**, the provisioner
DEFINES this contract — a downstream operator aligns the vault to the contract
rather than the provisioner reverse-engineering an ad-hoc format. This is the
"re-encode per current conventions" discipline (same as heritage porting).

## Companion: clone-home principle (guard-131)

Provisioning credentials is one of two secret-bearing halves of standing up a
fleet node. The other is the **home-repo clone**: when provisioning a machine or
container to HOST an agent, clone THAT agent's home repo onto it (the agent's
private state travels with its repo, per the promotion chain — each agent has a
distinct home). The provisioner (`.env.local`) and the clone-home step (framework
+ agent repo) together stand up a complete node from the single bootstrap secret.
Encoded as `guard-131` (clone-home-repo).

## GitHub write-access lane (`provision-github-from-vault.sh`)

The `.env.local` lane above self-services a container's daemon credentials. A
second secret-bearing prerequisite is **git push access**: a fleet container that
runs an agent must be able to push that agent's state to the fleet repo. Before
this lane, that write access was granted by a human (the prod operator) holding
repo-admin and registering each container's deploy key by hand — a single point
of failure (no new container can get write access while that operator is absent
or de-authed). The GitHub lane makes write access self-serviced from the SAME
bootstrap vault, exactly as `.env.local` is.

Mechanism (per agent, at container bring-up — the same five-step shape as the
`.env.local` lane, retargeted at a GitHub deploy key):

1. **Read one token in-memory** — the provisioner reaches the vault via the same
   bootstrap-key path and reads a single dedicated entry,
   `<PREFIX>_FLEET_GH_DEPLOYKEY_ADMIN_TOKEN` (a fine-grained credential scoped to
   `Administration:read/write` on ONLY the fleet repos — never a broad personal
   token). The token stays in a shell variable, is passed to `curl` via
   `--config` stdin (never argv/proc), and never touches disk.
2. **Generate the agent's own keypair** — an `ed25519` pair at
   `<SSH_KEY_DIR>/<agent>_deploy` (private key mode 600) if absent. Per-agent keys
   preserve attribution + independent revocability.
3. **Self-register as a WRITE deploy key** — `GET /repos/<repo>/keys` then
   `POST` with `read_only:false` via the raw REST API (no `gh` install needed on
   containers). Idempotent: skip when our key MATERIAL is already registered.
   GitHub's `read_only` flag is authoritative for write-status (never a
   key-name heuristic — guard-133); a pre-existing READ-ONLY registration of the
   same material is a clear WARN + non-zero (deleting a deploy key is a
   destructive, operator-gated op left to the operator — guard-1021,
   archive-before-delete), not an auto-delete.
4. **Route ssh** — ensure `<SSH_KEY_DIR>/config` sends `Host github.com` through
   the deploy key (`IdentitiesOnly yes`), appended exactly once.
5. **Verify values-blind** — print the deploy-key title + registration state,
   NEVER the token, NEVER the private key.

**Dormant-but-ready.** The production PAT is minted (GitHub web UI, human-only)
and seeded into the vault ZDS-side by the prod operator + user — out of scope
for the framework. The provisioner ships READY: when the vault has no token entry
it prints a clear skip and exits 0, so bring-up never breaks; it activates the
moment the entry appears. This preserves invariant 1 (single pre-seeded secret):
the bootstrap key remains the only manual per-container secret; the deploy-key
admin token lives once in the shared vault, not per-container.

All seven invariants above apply unchanged (in-memory only, never print values,
mode 600, FROM-state guard via the pubkey-already-registered idempotency check,
values-blind verify). The env-prefix mapping contract (invariant 7) governs the
vault key name.

## Secrets hygiene (non-negotiable)

This capability handles credentials; every rule in
`core/config/conventions/secrets.md` applies, plus:

- Credential VALUES MUST NEVER appear in any file under `world/`, `<agent>/`,
  `meta/`, in the journal/knowledge tree/working memory, or in stdout/stderr
  (`guard-724`). The provisioner's LOCATOR/mapping (which key, from where) is
  documentable; the VALUE is not.
- When reading a value, consume it in the same process/shell invocation — never
  round-trip it through a disk file or a logged variable.
- If a downstream tool echoes a credential (an SDK debug line, a verbose error),
  redact before any write. Prefer wrappers that do not echo.

## Integration with the transplant subsystem

Fleet-node bring-up composes three framework pieces:

| Step | Framework surface | What it delivers |
|------|-------------------|------------------|
| Plant the mind | `core/scripts/seed-transplant.sh` (`/seed plant`) | framework code + seeded state onto the destination |
| Clone the home repo | guard-131 clone-home principle | the hosting agent's private repo |
| **Provision secrets** | **this capability** (`provision-from-vault.sh`) | the mode-600 `.env.local` from one bootstrap key |
| **Provision GitHub write** | **the GitHub lane** (`provision-github-from-vault.sh`) | a per-agent WRITE deploy key so the container can `git push` — no agent holding repo-admin |

The provisioner is the credential-bootstrap step — orthogonal to seed-plant (which
carries no secrets) and to clone-home (which carries code). Run order on a cold
node: seed-transplant → clone-home → **provision-from-vault** (`.env.local`) →
**provision-github-from-vault** (write deploy key) → daemon boots with a complete
env AND push access.

## Domain routing (why this file is domain-free and the script is not)

The **pattern** (bootstrap-key → remote-vault → self-service) is domain-free
framework and lives here, described generically. The **provisioner script** that
instantiates it names concrete resources (the vault path, the operator host, the
per-service env keys) — those are *functional* domain tokens, so the script lives
in `core/scripts/` and opts out of the leak sweep via the shebang-level exemption
that `.claude/rules/domain-free-examples.md` § "Marker Restriction" reserves for
executable code with functional domain strings, and it travels downstream in the
promotion seed. This is the same sanctioned split
`core/config/conventions/domain-recipe-seed-purity.md` establishes for domain
upgrade recipes: the executable script carries functional domain strings under the
exemption; this convention, which documents the mechanism, stays generic and needs
no exemption.

## Promotion

This is framework-capability development: per the promotion cycle
(`guard-97`/`guard-98`), it MUST originate in the dev source and flow DOWN the
promotion chain to the downstream prod operator, never be built in a downstream
prod. A downstream prod operator that validates the pattern routes it UP for
formalization at the dev source — it does not implement it locally. No secret
values travel in such a routing signal; only the pattern and the key-name mapping.

## Reference provenance

The pattern was validated 2026-07-02 on a fleet container (bootstrap master key →
self-serviced `.env.local`), with a companion clone-home step, and routed up for
framework formalization. The validated reference scripts are captured in the prod
operator's home repo (commit `e37209f6`). This convention re-encodes the pattern
per current framework conventions rather than copying the reference verbatim.

## Cross-references

- `core/config/conventions/secrets.md` — credential access + hygiene rules.
- `core/config/conventions/domain-recipe-seed-purity.md` — the domain-code-in-`core/`
  + travels-in-seed doctrine this capability follows.
- `core/config/conventions/agent-spawning.md` — companion bring-up (context
  injection for spawned agents).
- `.claude/rules/probe-with-canonical-code-path.md` — use the canonical
  remote-shell wrapper, not a raw connection.
- `guard-131` — clone-home-repo companion principle.
- `guard-724` — never let a credential value travel.
- `guard-97` / `guard-98` — the dev-originates promotion cycle.
