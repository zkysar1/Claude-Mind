---
name: scan-runtime-margin
description: "Measures how much headroom a repo has left on its pinned CI runtime line by scanning every lockfile engines constraint — not just the violation count. Use whenever a goal bumps a Node version, adds or upgrades a dependency under a floating CI pin (node-version 20 floats to the newest 20.x), audits runtime compatibility, or asks 'will this still build'. ALWAYS use it before concluding a repo is safe on its current runtime: a zero-violation count is a count, not a margin, and the margin is the number that predicts the next breakage. Fires on 'runtime margin', 'engines check', 'Node version bump', 'is the lockfile compatible', 'how much headroom is left'. MUST use world/scripts/runtime-margin-scan.sh — never a hand-rolled scan; the hand-rolled shape has twice named a platform-gated optional package as the binding constraint and been wrong."
forged: true
forged_by: zeta
forged_date: "2026-07-29"
forged_from: gap-041
user-invocable: false
minimum_mode: assistant
tools_used: [Bash, Read]
companion_scripts:
  - world/scripts/runtime-margin-scan.sh
  - world/scripts/runtime-margin-scan.cjs
  - world/scripts/runtime-margin-scan-selftest.sh
triggers:
  - runtime margin
  - engines check
  - node version bump
  - lockfile compatibility
  - how much headroom
  - runtime compatibility audit
---

# scan-runtime-margin

## What this answers that a violation count does not

A CI pin of `node-version: 20` **floats**. `actions/setup-node@v4` resolves it to
the newest 20.x available on the runner, so a tree whose true floor is 20.19.0
passes every build against a line whose last release is 20.20.2 — while holding
exactly **one non-renewable minor** of headroom. The violation count is 0 and
stays 0 right up until it is suddenly fatal.

So the deliverable is not "does it pass". It is four numbers:

1. **Violations** at the pinned major AND at the concrete version CI resolves to.
2. **A boundary sweep** across the line. This is what converts a bare zero into a
   margin (`guard-1786`, `rb-5635`).
3. **The floor, with every entry sitting on it enumerated**, split optional vs
   non-optional — never a singular "the binding constraint" (`guard-1788`).
4. **A positive control**, so a zero is provably a live predicate rather than a
   dead one.

## Restricted Operations

**MUST use `world/scripts/runtime-margin-scan.sh`. Never a hand-rolled scan, and
never raw `npm view`.** Three reasons, each learned by getting it wrong:

- **The lockfile, not the registry.** `npm ci` installs exactly the lockfile. The
  read is offline, and the binding constraints live in the transitive tree that
  `package.json` never shows (2529 entries vs 26 direct in Vinheim-Web-App).
- **The wrapper owns NODE_PATH.** `semver` resolves from the *target repo's*
  `node_modules`, so the scanner installs nothing and never has to live inside
  the repo it measures.
- **The wrapper proves the repo was untouched.** It diffs `git status --porcelain`
  before and after, every run, and reports `repo_untouched`. The first
  hand-rolled version of this scan wrote its script into the product repo; this
  makes that shape impossible and the claim evidence-backed rather than asserted.

## Procedure

### Step 1 — Pull the target repo first

The scan reads a shared checkout, and a stale checkout produces a confidently
wrong margin. Per `world/conventions/pre-execution.md` Step 2:

```
Bash: git -C <repo> pull --ff-only || true
```

A failed pull is a signal, not noise — state the staleness rather than
suppressing it. Never stash or reset over a dirty tree (it may be a partner's
in-flight work).

### Step 2 — Run the scan

```
Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/runtime-margin-scan.sh" \
        --repo <repo-path> [--major N] [--concrete X.Y.Z] [--line-end X.Y.Z] [--json]
```

The `$WORLD_PATH` resolution is load-bearing, not cosmetic: `world/` is an
EXTERNAL path and the Bash hooks do not rewrite path arguments, so a bare
`bash world/scripts/...` dies rc=127 and reads exactly like a broken tool
(`.claude/rules/probe-with-canonical-code-path.md`).

| Flag | Meaning |
|---|---|
| `--repo` | Repo containing `package-lock.json`. Required. |
| `--major` | The pinned major. **Omit it** — the scanner derives it from `.github/workflows/*.yml` and prints the file:line it read. Pass it only when the workflows disagree (the scanner refuses rather than guessing) or there are none. |
| `--concrete` | The version CI resolves to today. Without it, the at-concrete row reports `not measured` rather than a fabricated number. |
| `--line-end` | The last release the line will ever ship. Headroom is `null` without it — **never guessed and never fetched**, because the scan is offline by design. Get it from `nodejs.org/dist/index.json` when you need the number, and cite it. |
| `--fail-on-violation` | Exit 1 when violations exist at the pin. CI-gate mode. |
| `--json` | Machine-readable. |

### Step 3 — Read the output in this order

**Positive control first.** If it did not pass, the scanner exits 4 and every
number above it is void. Do not quote a floor from a run whose control failed.

**Then the sweep, not the violation count.** The count answers "does it build
today"; the sweep answers "how much of the line is left". A sweep that reads
`20.0.0 → 41 … 20.19.0 → 0` says the tree is pinned to the last two minors of a
line that has ended.

**Then the non-optional floor, not the `all` floor.** They differ, and the
difference is the whole point: optional entries are platform-gated
(`@rolldown/binding-*` ships 15 per-platform variants) and constrain nothing on
any given runner. The `[non-optional] at-floor` list is the operational answer.

**Read the enumeration, never a single name.** An extremum over a set is
frequently achieved by several elements. Vinheim and Lodestar both floor at
20.19.0 with **three** non-optional packages there, from two independent
toolchains (`rolldown` and `vite` via vitest 4, `eslint-visitor-keys` via
typescript-eslint). "Pin the offending package down" is therefore not available
as a cheap remedy in either repo — the floor is over-determined, and removing
any one entry moves nothing.

### Step 4 — Act on the shape you got

| Shape | What it means | Move |
|---|---|---|
| `excludes_line` non-empty | Some entry accepts **no** version in the line. `floor: NONE`. | The line is unusable. Bump the runtime or hold the dep back. This is the g-335-418 shape (jsdom@30, `>=22.22.2`). |
| violations 0, floor near the line end | Passing on borrowed time. | Report headroom in minors. Escalate before the next dep bump, not after. |
| violations 0, floor at `major.0.0` | Genuine headroom across the whole line. | Nothing to do. `at_floor` is empty and that is the correct answer, not a broken scan. |
| `unresolved_floor` non-empty | A range needed more than the bounded scan (60 minors / 40 patches). | Inspect those entries by hand. The tool refuses to guess. |

### Step 5 — Verify the tool before trusting a surprising result

If the scan contradicts a prior measurement, or two repos return suspiciously
identical aggregates, run the discrimination proof before writing the number
down:

```
Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/runtime-margin-scan-selftest.sh"
```

Five synthetic repos with known, **different** correct answers (healthy,
excludes-line, over-determined, gap-range, lockfileVersion-1). It exits 0 only
when all five behave as specified AND at least 3 distinct floors appear across
them. Identical aggregates from two supposedly independent measurements have two
opposite causes — genuine structural identity, or instrument error — and the
numbers cannot tell them apart (`rb-5636`).

## Error handling

| Exit | Meaning | Response |
|---|---|---|
| 0 | Scan completed. | Read the output. |
| 1 | Violations at the pin, with `--fail-on-violation`. | Expected in gate mode. |
| 2 | Usage or input error — missing `--repo`, no lockfile, `lockfileVersion` 1, workflows disagree on the pin, or a parse that yielded zero entries. | The message names the fix. **A zero-entry parse is refused rather than reported as a clean tree** — an empty read and a clean tree are otherwise indistinguishable downstream. |
| 3 | Environment — `semver` unresolvable or `node` absent. | The message names every location searched. Most often the target repo has **no `node_modules`** (never `npm install`ed) — normal for an AUDIT target. The wrapper falls back to its own dir and then a bounded sibling search; if all miss, prefix `NODE_PATH=<any-installed-repo>/node_modules`. The scan installs nothing and uses `semver` for range arithmetic only, so ANY recent copy is equivalent — and when it borrows one, it says so on stderr rather than substituting silently. |
| 4 | **Positive control failed.** | The scan ran but its verdict is untrustworthy. Do not use any number from it. Run the selftest to localize. |
| 5 | The repo's working tree changed during the scan. | The scan is read-only by construction, so this means something wrote concurrently or the scanner regressed. Investigate before trusting the result. |

## Known coverage boundaries

Stated explicitly because a green run is silent about what it never reached
(`guard-1462`). The fixtures inject at the lockfile-file level and run the real
wrapper end to end, so very little is stubbed — but these are not exercised:

- **CI-pin discovery beyond a plain scalar.** Matrix strategies
  (`node-version: [18, 20, 22]`), `node-version-file:`, `.nvmrc`, and
  `${{ matrix.node }}` expressions are not parsed. A matrix repo will hit the
  "workflows disagree" refusal, which is safe but untested.
- **The `UNRESOLVED` bounded-scan exhaustion path.** No fixture drives a range
  past the 60-minor / 40-patch bound. An entry that resolves UNRESOLVED
  contributes NO sweep point, so the reported floor could in principle sit BELOW
  the true floor — a false-SAFETY direction. Tracked as hypothesis
  `2026-07-29_unresolved-floor-silently-understates-the-floor`.
- **The `repo_untouched: FALSE` branch.** All live runs have returned true.

Retired from this list by the g-335-442 validation (2026-07-29):

- **The empty at-floor set.** `floor == major.0.0` with `at_floor: 0` was
  documented but had never been seen on a real repo — every forge-time run
  floored mid-line. Both ZDS repos now produce it after the Node-24 bump. It
  reads exactly like a dead predicate, which is why the positive control is the
  first thing to check: both returned `PASS (probe 24.5.0, two-way ok)`.
- **Exit 3 with no `node_modules`.** Hit on a real audit target
  (`Ayoai-Public-Web-App`). The wrapper had a single hardcoded `NODE_PATH` and
  the error's own remedy said "run via the wrapper" — which is what had just
  failed. Now a 3-step discovered fallback with an announced donor. The lesson
  generalizes past this tool: **an error message whose remedy does not cover the
  case that produced it sends the reader in a circle**, and that is worse than
  no remedy at all.

The exit-4 (control failure) and exit-1 (`--fail-on-violation`) branches WERE
proven by deliberate mutation at forge time, so they were never on this list.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the `Bash` call running `runtime-margin-scan.sh` (or
`runtime-margin-scan-selftest.sh` when Step 5 fires), handing the scan output back
to the invoking phase. Never end with a text summary.
