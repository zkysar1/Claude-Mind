# World Contract

## Overview

A "world" is a bounded execution environment where agents perceive state, decide,
act, receive feedback, and accumulate knowledge. The world contract defines the
**seven elements** every world must provide, regardless of substrate (3D game,
2D interface, desktop environment, business workflow, or any other bounded
environment with agents).

The contract is substrate-independent by construction -- it names capabilities,
not implementations. The substrate determines how perception, physics, rendering,
and input work; the contract sits above that line and governs what every world
exposes to the cognitive engine running inside it.

## The Seven Elements

| Element | What it is | Framework realization (claude-mind) |
|---------|-----------|-------------------------------------|
| **Agents** | Entities that perceive, decide, and act within the world | `agents/<name>/` directory (self.md, aspirations.jsonl, experience.jsonl, journal.jsonl, session/) |
| **Tasks** | Units of work agents select, execute, and verify | `world/aspirations.jsonl` (world-level) + `agents/<name>/aspirations.jsonl` (agent-level) + goals within aspirations |
| **State / data objects** | The world's current condition -- knowledge, configuration, conventions | `world/knowledge/tree/` (knowledge tree) + YAML/JSONL stores under `world/` (pipeline, guardrails, reasoning bank, pattern signatures, team state) |
| **Feedback loops** | Mechanisms by which outcomes inform future decisions | Hypothesis pipeline (`world/pipeline.jsonl`) + reflection engine (`/reflect`) + spark questions (`meta/spark-questions.jsonl`) |
| **Signals / events** | Communication channels within and between agents | Board channels (`world/board/*.jsonl`) + session signals (`agents/<name>/session/`) + working memory (`agents/<name>/session/working-memory.yaml`) |
| **Memory / context** | Persistent knowledge that survives across sessions | Experience archive (`agents/<name>/experience.jsonl`) + reasoning bank (`world/reasoning-bank.jsonl`) + working memory + knowledge tree |
| **Execution history** | Record of what happened and why | Journal (`agents/<name>/journal.jsonl` + `agents/<name>/journal/`) + execution diary (`agents/<name>/session/execution-diary.jsonl`) + changelog (`world/changelog.jsonl`) |

### What the contract does NOT prescribe

Perception modality, physics model, spatial representation, rendering, and input
schema. These are substrate concerns that live below the contract line. A real-time game
world and a text-based workflow world both satisfy the same seven-element contract;
they differ only in how substrate-level perception feeds state into the contract
layer.

For the perception interface design (how substrate-level signals cross into the
contract layer), see `core/config/conventions/perception-module.md` (when
available).

## World Identity: `ENVIRONMENT_ID`

Every world instance has a stable identifier: `ENVIRONMENT_ID`.

```
ENVIRONMENT_ID=zds-mind
```

> **Naming alignment (2026-06-07, principal directive).** This var was
> renamed from `MIND_ENV_ID` to `ENVIRONMENT_ID`, and the commons-policy
> value vocabulary was converged to the official `private|selective|public`
> (from this world's earlier nothing/shared/open vocabulary; mapping
> nothing=private, shared=selective, open=public). Both the `ENVIRONMENT_ID`
> and `COMMONS_POLICY` keys AND their values now match ayoai-mind across both
> environments, so shared code (e.g. Zak-Code) reads one name and one
> vocabulary everywhere. Semantics are unchanged.

### Purpose

- **Hosted-store partition prefix**: when multi-world store isolation ships (build
  roadmap Step 3.1), `ENVIRONMENT_ID` prefixes partition keys so multiple worlds
  share infrastructure without data leakage.
- **Cross-world provenance**: when knowledge flows between worlds (via the
  cross-world influence mechanism or the generalization engine), `ENVIRONMENT_ID`
  is the `originWorldId` in provenance metadata -- it traces where a piece of
  knowledge came from.
- **Human-readable stable name**: unlike UUIDs, `ENVIRONMENT_ID` values are
  short, readable, and stable (e.g., `zds-mind`, `ayoai-mind`,
  `vinheim-demo-01`).

### Current status (honest)

`ENVIRONMENT_ID` is defined as an env var in `.env.example` and loaded by
`env-read.sh` at runtime. As of 2026-06-02, no code file reads or branches
on this variable. It is a declared design primitive, not a built primitive.
The first consumer will be the multi-world DDB isolation work (build roadmap
Step 3.1) and the cross-world provenance chain (guardrail G5).

### Rules

1. **One `ENVIRONMENT_ID` per world.** A world's env-id is set once at creation
   time and never changes. Renaming requires a migration.
2. **Format**: lowercase kebab-case, max 64 characters. Must be unique across
   all worlds that share infrastructure (DDB tables, S3 buckets).
3. **Not a secret.** `ENVIRONMENT_ID` appears in `.env.example` with its real
   value. It is an identifier, not a credential.

## Commons Crossing Policy: `COMMONS_POLICY`

Every world declares how its knowledge may flow outward to the commons
(lodestar.wiki). This is the per-world privacy dial.

```
COMMONS_POLICY=private
```

### The three values

| Value | Privacy tier | What crosses the world boundary | Generalization engine |
|-------|-------------|--------------------------------|----------------------|
| `private` | **Private** | Nothing. No egress except platform control plane. | Architecturally absent -- no code path exists that could extract patterns from this world's data. |
| `selective` | **Selective** | Only generalized patterns emitted by the engine (role sequences, step counts, tool names, tags -- never raw text). Raw knowledge stays in-world. | Runs on outbound data. Six-layer privacy mechanics apply (exact-secret redaction, typed-placeholder lifting, structural inducer, k-aggregation, leakage test, DP budget). |
| `public` | **Public** | Full knowledge tree exposed via read API. The raw content IS the product. | Does NOT run. Public-tier worlds share the real thing, not a generalized abstraction of it. |

### How `COMMONS_POLICY` gates the generalization engine

The build roadmap's value spine (Steps 2.1-2.3) defines three transitions:

1. **T1 (World data to engine input)**: experience.jsonl -> RawTrace[] for
   the generalize() pipeline. Fires only when `COMMONS_POLICY=selective`.
2. **T2 (Engine output to commons storage)**: EmittedRecord[] -> commons
   storage (S3/DDB). Fires only when `COMMONS_POLICY=selective`.
3. **T3 (Dial gate)**: `COMMONS_POLICY` is the single variable that
   controls whether T1 and T2 fire. When `private`, neither fires. When
   `public`, T1/T2 are bypassed entirely -- the raw tree is exposed directly
   via the read API.

### Current status (honest)

`COMMONS_POLICY` is defined in `.env.example` with value `private`.
As of 2026-06-02, no code file reads or branches on this variable. The
generalization engine exists (11 modules, 77 tests in Lodestar-Web-App)
but has zero production callers. The T1/T2/T3 transitions above are design
targets, not built features. Every world today is effectively private -- not
because the private tier is enforced in code, but because the selective and
public tiers do not yet exist.

### Rules

1. **Default is `private`.** New worlds start with the tightest
   policy. This is not a safe default -- it is the ONLY default.
2. **Tightening is always allowed.** `public` -> `selective` -> `private` may
   happen at any time.
3. **Loosening applies to future records only.** `private` -> `selective`
   does NOT retroactively expose existing knowledge. The loosened policy
   applies only to data created after the change. An explicit user
   confirmation is required before loosening takes effect.
4. **Valid values are exactly three.** `private`, `selective`, `public`. Any
   other value must be treated as `private` (fail-closed).
5. **Not a secret.** The policy value appears in `.env.example`. It is a
   configuration dial, not a credential.

## Cross-World Guardrails (G1-G5)

The world contract interacts with the five cross-world guardrails that
govern how worlds influence each other. These guardrails are defined in
the product strategy (`agents/omni/reports/company-strategy.md`,
"Cross-World Influence" section) and will be hardened in build roadmap
Step 3.3. Summary:

| Guardrail | What it enforces | Contract interaction |
|-----------|-----------------|---------------------|
| **G1: Default-private dual-write GRANT** | New worlds start with zero GRANTs (no cross-world access). Private-tier worlds have no inbound influence endpoint. | `COMMONS_POLICY=private` means no inbound GRANT can be created for influence. |
| **G2: Cross-world goal sandboxing** | Injected goals execute in the TARGET world's sandbox with target agents and permissions. | The world contract's Agents and Tasks elements scope execution -- an external world never gains access to another world's agents. |
| **G3: Human approval gate** | First INFLUENCE grant from World A to World B requires explicit human approval from B's owner. | No agent can consent to cross-world influence on behalf of its human owner. |
| **G4: Rate limit + depth cap + cycle detection** | Per-grant rate limiting, no transitive influence (A->B->C requires A->C GRANT), cycle detection at GRANT creation. | `ENVIRONMENT_ID` is the key in rate-limit and cycle-detection lookups. |
| **G5: Cross-world provenance** | Emitted patterns carry originWorldId, sourceTraceIds, contributorIds, influence_chain. | `ENVIRONMENT_ID` is the `originWorldId` value in provenance metadata. |

### Current status (honest)

The GRANT entity does not exist in any hosted-store schema. The guardrails are
design artifacts documented in the pearl and roadmap, not built enforcement
mechanisms. The first concrete cross-world influence instance (this world
feeding aspirations to a sibling Mind world via board cross-post) uses lightweight
guardrails only (G2 sandbox metadata + G3 human approval + G4 rate limit),
with full RT2 hardening (G1-G5) planned for build roadmap Step 3.3.

## Environment Variables in `.env.example`

Both world-contract vars live in `.env.example` under the "World contract
vars" section. They are loaded at runtime via `env-read.sh` (see
`core/config/conventions/secrets.md`).

```bash
# World contract vars
ENVIRONMENT_ID=zds-mind                # this world's stable id
COMMONS_POLICY=private         # commons crossing policy: private|selective|public
```

These are NOT secrets. They carry their real values in `.env.example`
(unlike AWS keys or API tokens, which are placeholders). `.env.local`
may override them if a deployment needs different values, but the
example file shows the canonical values for this world.

## Relationship to Other Conventions

| Convention | Relationship |
|-----------|-------------|
| `external-paths.md` | World contract elements map to files under `WORLD_PATH` (resolved per external-paths). The contract names capabilities; external-paths names filesystem locations. |
| `perception-module.md` | (Planned) Defines how substrate-level perception crosses into the contract layer. The contract sits above perception; the perception module sits below it. |
| `coordination.md` | Multi-agent coordination within a single world uses board channels and team state -- both are contract elements (Signals/events). Cross-world coordination uses the GRANT mechanism governed by G1-G5. |
| `session-state.md` | Session state files under `agents/<name>/session/` are the framework realization of the contract's Signals/events and Memory/context elements. |
| `aspirations.md` | Aspirations and goals are the framework realization of the contract's Tasks element. |
| `experience.md` | Experience archive is the framework realization of the contract's Execution history element. |
| `learning-routing.md` | The ten stores described in learning-routing are the framework realization of the contract's Memory/context element, spread across world/ and agents/<name>/. |

## Anti-patterns

- Treating the world contract as a storage schema. The contract names
  capabilities, not storage. A hosted key-value store is one possible
  realization; filesystem YAML/JSONL is another.
- Assuming `ENVIRONMENT_ID` or `COMMONS_POLICY` are consumed by any
  code today. They are declared primitives awaiting their first consumer.
  Do not write code that reads them without also building the feature
  that acts on the value.
- Hardcoding a world's identity or privacy policy anywhere other than
  `.env.local` / `.env.example`. `ENVIRONMENT_ID` and `COMMONS_POLICY`
  are the single source of truth for those values.
- Conflating the world contract with the perception interface. The contract
  is substrate-independent; perception is substrate-specific. A world
  satisfies the contract even if its perception module is unbuilt (the
  contract elements are populated by the cognitive engine, not by
  perception).
