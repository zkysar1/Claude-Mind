# Rules Loading and `paths:` Scoping

How `.claude/rules/*.md` reaches the model, and how to stop a rule from
reaching it on every turn.

**Status: VERIFIED from the shipped implementation.** Claude Code **2.1.226**
(`claude --version`), measured 2026-08-17 on `hostname` **cc-08**, `uname -r`
**6.8.0-137-generic**. This is not a docs claim and not an inference from
behavior — the loader was read out of the binary. Re-derivation procedure is at
the bottom so a future version can be re-checked rather than trusted.

## The verdict

`paths:` YAML front matter on a rule file **works**, and does exactly what a
preamble diet needs:

| rule has `paths:` | in the always-on preamble? | injected when? |
|---|---|---|
| no | **yes, every turn** | always |
| yes | **no** | only when a touched file matches one of its globs |

So converting a rule to path-scoped is a **metadata change**, not a rewrite. A
rule does not lose its content; it loses its unconditional seat.

## The mechanism

One directory walker, `szt({rulesDir, type, conditionalRule, …})`, is called
twice with opposite values of `conditionalRule`, and each call keeps exactly
the rules the other discards:

```js
// inside szt(), per .md file found under .claude/rules/
c.push(...S.filter(b => o ? b.globs : !b.globs))   // o === conditionalRule
```

- **Always-on pass** — `conditionalRule: false`, called from the main memory
  load for every ancestor directory's `.claude/rules`. Keeps only rules with
  **no** globs. This is the pass that builds the per-turn preamble.
- **Conditional pass** — `conditionalRule: true`, reached through a wrapper
  that takes a FILE PATH. Keeps only rules **with** globs, then filters them
  against that path.

Front matter is parsed before either pass:

```js
function li_(e){
  let {frontmatter:t, content:r} = qp(e);
  if (!t.paths) return {content:r};
  let n = gvo(t.paths).map(o => o.endsWith("/**") ? o.slice(0,-3) : o)
                      .filter(o => o.length > 0);
  if (n.length === 0 || n.every(o => o === "**")) return {content:r};
  return {content:r, paths:n};
}
```

and the wrapper does the matching:

```js
let s = type === "Project" ? dirname(dirname(rulesDir)) : cwd();   // = project root
let a = isAbsolute(file) ? relative(s, file) : file;               // repo-relative
return i.filter(l => {
  if (!l.globs || l.globs.length === 0) return false;
  if (!a || a.startsWith("..") || isAbsolute(a)) return false;     // outside the root
  return ignore().add(l.globs).ignores(a);
});
```

## Semantics that will bite you

1. **Globs are matched with gitignore semantics, not picomatch.** The matcher is
   the `ignore` package (`ignore().add(globs).ignores(path)`). A pattern with no
   slash matches at any depth. This differs from `claudeMdExcludes`, which the
   settings help text says uses picomatch — two glob dialects in one product, so
   do not carry intuition from one to the other.

2. **Paths are relative to the PROJECT ROOT** (the rules dir's grandparent for
   project rules). Write `core/scripts/**`, not `/opt/…/core/scripts/**`.

3. **A file outside the project root can never match.** The `a.startsWith("..")`
   guard returns false before any glob is consulted. So path-scoping a rule
   whose subject files live in a SIBLING repository does not defer that rule —
   it retires it from context entirely. That is sometimes the right trade (see
   the worked example below), but it is never the trade you think you are
   making, so check where the trigger files actually live first.

4. **A trailing `/**` is stripped**: `src/**` becomes `src`, which under
   gitignore semantics still matches the directory and everything beneath it.
   Harmless, but it means `src/**` and `src` are the same pattern here.

5. **`paths: ["**"]` is an escape hatch, not a wildcard scope.** If every entry
   is `**`, the loader discards the globs and the rule stays always-on. Same for
   an empty list. Use it to mark "deliberately unconditional" without deleting
   the key.

6. **A glob the matcher rejects is dropped silently** (`cyt` probes each pattern
   and filters the bad ones, emitting telemetry rather than an error). A
   malformed glob therefore does not fail loudly — it just narrows the rule to
   nothing, or, if all globs are dropped, makes the rule unreachable while the
   file still looks scoped. Positive-control a newly scoped rule; do not assume
   the pattern parsed.

7. **The same key exists for skills** (`skill_paths` appears alongside
   `claudemd_rule_globs` in the glob-validation table). Not investigated here;
   do not assume identical semantics without reading it.

## Measured baseline (before any scoping)

On cc-08, 2026-08-17, immediately after integrating the first prose-cut commit:

| | bytes |
|---|---|
| `CLAUDE.md` | 59,560 |
| 33 files in `.claude/rules/` | 258,749 |
| **always-on total** | **318,309** |

**0 of 33** rule files carried `paths:`. Largest four:
`run-full-suite-after-deep-code.md` 37,319 · `check-team-state-before-silent.md`
15,572 · `retrieve-before-deciding.md` 13,864 · `verify-before-assuming.md`
13,540.

Consumer-side confirmation that the whole set really is unconditional: all 33
appeared as `Contents of …/.claude/rules/<name>.md` headers in the `claudeMd`
system-reminder delivered to a live session on this box.

## How to observe the preamble (and how not to)

**Do** count the headers in the `claudeMd` block a session actually receives.
The loader emits one `Contents of <path> (project instructions, checked into
the codebase):` header per loaded file, so the header count IS the loaded set.
A fresh session or a post-compaction re-injection both carry the full set.

**Do not** try to recover the preamble from the session transcript
(`~/.claude/projects/<slug>/<sid>.jsonl`). Measured on this box with a positive
control: it is not persisted there. Both apparent hits in a 40k-line transcript
were the probing Bash command echoed back — a `guard-1238` self-match. Rule out
self-matches before believing any transcript grep for preamble text.

## Worked example: the first scoped rule

`.claude/rules/gradle-tests-pattern.md` was scoped first, deliberately, because
its enforcement does not depend on the rule text:
`core/scripts/gradle-tests-gate.sh` is a PreToolUse[Bash] hook wired in
`.claude/settings.json`, and `gradle-tests-audit.py` is the detective. Both were
re-run after the edit and are unaffected — the gate still refuses a
package-qualified pattern and prints the full mechanism in its deny message, and
the audit is clean.

Note this rule hits semantic (3): its trigger files live in sibling product
repos, outside this project root, so scoping it removes it from context rather
than deferring it. That is acceptable **only** because the gate carries the
mechanism to the moment of use. Do not generalize the choice — generalize the
question: *if this rule never loads again, what still enforces it?*

## Choosing what to scope

Ranked by how safe the trade is:

1. **Rule has an automated gate that carries its own explanation** — safest.
   The rule text is documentation; the gate is the enforcement.
2. **Rule's trigger files are inside the project root and easy to name**
   (`core/scripts/**`, `mind_api/**`, `.claude/skills/**`) — the intended case.
   The rule appears exactly when someone touches that surface.
3. **Rule is behavioral and fires at a moment with no file trigger** — e.g. a
   rule about how to word a conclusion, or one that governs a decision rather
   than an edit. **Do not scope these.** There is no file to match, so scoping
   them is deletion with extra steps.

Category 3 is the one to be careful about, and it is not rare: a rule that
governs *reasoning* has no glob. Sizing alone will point you at the biggest
files, and the biggest file is not automatically the most scopeable one.

## Re-deriving this on a future version

The CLI ships as a single compiled binary. To re-check after an upgrade:

```bash
B=$(readlink -f "$(command -v claude)")
strings -n 6 "$B" > /tmp/cc-strings.txt        # ~2 min, ~550k lines
grep -n 'conditionalRule' /tmp/cc-strings.txt  # the two-pass loader
grep -n 'claudemd_rule_globs' /tmp/cc-strings.txt
```

Avoid `grep -o -E '.{300}<pat>.{300}'` directly against the binary — it does not
finish in a reasonable time on a ~300 MB file. Extract strings once to a file,
then grep the file.

**One recorded false lead.** The binary contains the string `alwaysApply: false`,
which is the front-matter key Cursor uses for exactly this feature. It is **not**
Claude Code's. It comes from a `.cursor/rules/use-bun-instead-of-node…mdc`
template embedded by Bun, which is what the CLI is compiled with. Chasing it
leads to a Bun scaffolding blob, not to a rules loader. The real keys are
`paths` (rules front matter) and `globs` (the parsed result on the loaded entry).

## `description:` and `alwaysApply:` — what a NON-Claude-Code runtime sees (2026-08-29)

Claude Code folds every unscoped rule's full body into the preamble. A Zak-Code Body
does not: its full render is capped at 32 KB total / 8 KB per file (only the first
seven rules by name fit; 27 are dropped with a note), so the coach fleet runs the
**lean rules index** — one line per rule (`- name: summary [path]`, summary ≤ 140
chars) plus a `read_rule` tool. Measured 2026-08-29 over one hour of eight sessions on
a 35B model: 252 turns, **zero** `read_rule` calls, and the summary line was the rule's
TITLE because no rule carried a `description:`. The index line is therefore the only
part of a rule a small-model Body ever sees. Two front-matter keys fix that; Claude
Code's loader ignores both (its only rules key is `paths`, verified above):

| key | who reads it | contract |
|---|---|---|
| `description: "<imperative>"` | Zak-Code lean index (and any Cursor-style loader) | ≤ 140 chars, one sentence, the rule's imperative — not its title. Every rule carries one; `core/scripts/tests/test_rules_frontmatter_index.py` pins length and presence. |
| `alwaysApply: true` | Zak-Code (ADR-0105) | the FULL body rides in the prompt ahead of the index (8 KB per-file cap — the test pins that a pinned body fits) and is folded first under the full render. Budget ≈ 25 KB across pins: `return-protocol`, `verify-before-assuming`, `read-before-edit`, `no-scratchpad` today. |

A rule that had no front matter gained a block; a rule that already had `paths:` gained
the keys inside its block. The HTML-comment `domain-leak-exempt` marker on rules without
front matter still sits below the block (the scanner greps the whole file). Byte cost:
+125 lines / ~5 KB across 34 files, paid on every Claude Code turn — accepted with a
`size-budget-override` because the alternative was 34 rules a Body never applies.

## Cross-references

- `.claude/rules/gradle-tests-pattern.md` — the first scoped rule
- `core/config/conventions/retrieval-triggers.md` — what loads on demand vs
  always; this file is the always-on half of that picture
- `.claude/rules/verify-before-assuming.md` — the positive-control discipline
  applied to the transcript negative above
