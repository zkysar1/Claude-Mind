# Domain-Free Examples in Core Files

## Principle

Example and illustration text in `core/` and base skills must use generic terms.
Never use cloud-provider names, product names, or project-specific terms in examples.

## Approved Generic Terms

Use these instead of domain-specific equivalents:
- "service", "endpoint", "external service" (not Lambda, API Gateway, DynamoDB)
- "firewall", "network rule" (not security group)
- "remote storage", "shared filesystem" (not EFS, S3)
- "integration test", "smoke test" (not game session, active session)
- "data pipeline", "processor" (not NPC processor, Roblox bridge)
- "credentials", "API key" (not AYOAI_API_KEY, AWS_ACCESS_KEY_ID)

If realistic examples help clarity, use fictional placeholders ("Acme API", "widget-service").

## Scope

Applies to: `core/config/`, `core/scripts/`, `.claude/rules/`, `mind_api/src/`, `mind_api/tests/`, and base (non-forged) skills in `.claude/skills/`.
Does NOT apply to: `world/`, `meta/`, `<agent>/`, or forged skills (registered in `world/forged-skills.yaml`).

## Where Domain Content Goes

Domain-specific content is not "wrong" — it just lives in a different place
than the framework core. Use this decision sub-tree to route correctly:

| Content shape | Goes in | Why |
|---|---|---|
| **Domain-agnostic behavioral imperative** ("agent MUST do X before Y") | `.claude/rules/<kebab-case>.md` | Universal cognitive discipline |
| **Domain-specific operational rule** (specific service endpoint, branded workflow, product-specific protocol) | `world/conventions/<kebab-case>.md` | Domain overlay loaded by `load-conventions.sh` after core conventions, per `core/config/conventions/domain-overlay-pattern.md` |
| **Framework structural protocol/schema/API** (JSONL field definitions, script CLI signatures, integration catalogs) | `core/config/conventions/<kebab-case>.md` | Declarative, domain-agnostic, catalog-style |
| **Domain reference docs / lookup tables** (resource locators, endpoint catalogs, agent-name list) | `world/conventions/*.md` per `encode-stable-facts.md` | Lookup data, not learned knowledge |

The full decision tree (with all 11 routing rules) lives in
`core/config/conventions/learning-routing.md` § "Decision Tree". The
"Rules vs Conventions" sub-tree is rule 11 there.

### Marker Restriction (per Phase 5)

The `domain-leak-exempt:` marker (see "Marker Placement" below) is
reserved for **executable code files** where domain strings are functional
(regex patterns matching domain identifiers, literal directory names in
path operations, test fixtures with real-world payloads). It is NOT a
license to inline domain examples in rule or convention files.

- **`.claude/rules/*.md`** — Markers are case-by-case. Prefer
  generic placeholders or move domain examples to `world/conventions/`.
- **`core/config/conventions/*.md`** — Markers SHOULD NOT appear.
  Conventions are portable structural docs; concrete domain examples
  defeat that purpose. Use generic synthetic examples ("agent-a coordinates
  with agent-b on service-x") that preserve teaching value.
- **`.claude/skills/*/SKILL.md`** — Markers SHOULD NOT appear in
  applies_to enum help-text. Genericize the help instead. The marker IS
  legitimate when the SKILL.md's pseudocode includes verification-grep
  examples or sentinel patterns that must NOT be transformed (see
  `.claude/skills/seed/SKILL.md`).

A Layer-B gate `core/scripts/marker-placement-gate.{py,sh}` (Phase 5.7,
landed 2026-05-20) refuses new `domain-leak-exempt:` introductions in
these over-applied locations. Override per edit via
`MARKER_PLACEMENT_OVERRIDE="<justification>"` in the edit content.
Permanent allowlist entries (files whose primary purpose is to DOCUMENT
the marker, e.g. `verify-learning/SKILL.md`, `learning-routing.md`) live
in `core/scripts/marker-placement-gate.py` ALLOWLIST — keep
`core/scripts/domain-leak-check.sh` ALLOWLIST array in sync.

A Layer-C scanner (`core/scripts/domain-leak-check.sh`) runs the
audit half: it reports `MARKER MISPLACED: <path>` for any in-scope
file carrying the marker and not in the allowlist. The detective layer
catches markers that slip in via override or that pre-date the gate.

## Verification

Run `core/scripts/domain-leak-check.sh` after editing core files. The
scanner respects the `domain-leak-exempt:` marker AND covers `mind_api/src`
+ `mind_api/tests` (Phase 3.2 extension, 2026-05-20).

## Marker Placement (Opting a File Out)

When a file in scope legitimately needs domain terms (pedagogical examples, regex
patterns enumerating domain strings, intentionally-named config constants), add
the marker `domain-leak-exempt: <rationale>` to opt it out of the leak check.
Placement depends on the file format — **wrong placement breaks strict parsers
that anchor on the first line**:

| File shape | Placement | Marker syntax |
|------------|-----------|---------------|
| YAML front matter present (SKILL.md, knowledge-tree nodes, any `.md` with `---` on line 1) | **INSIDE** the front-matter block, as a YAML `#`-comment | `# domain-leak-exempt: <rationale>` |
| Plain markdown, no front matter (`.claude/rules/*.md` that don't open with `---`) | Top of file as HTML comment | `<!-- domain-leak-exempt: <rationale> -->` |
| Python (`.py`) | Top of file, line 1 or 2 | `# domain-leak-exempt: <rationale>` |
| Shell (`.sh`) | After the shebang | `# domain-leak-exempt: <rationale>` |

The leak-check's marker scan (`domain-leak-check.sh:90`) uses `grep -q
"domain-leak-exempt:"` against the whole file — so any line containing the
marker token works. The placement rules above exist not for the leak check
but for downstream parsers that read the file (`core/scripts/_skill_md.py`
`parse_front_matter` requires `---` on line 1; an HTML comment above it
silently breaks every consumer).

**Why this matters**: on 2026-05-11, an LLM session running the leak check
pattern-matched the marker placement from `.claude/rules/probe-before-defer.md`
(legitimately HTML-comment-at-top because rule files don't have front matter)
to 7 SKILL.md files (which DO have front matter). All 7 silently lost their
front matter from every parser's view. The rb-840 / guard-518 / Section SFI
in `/verify-learning` triple-defend the regression class, but documenting
placement here closes the source-of-the-mistake.
