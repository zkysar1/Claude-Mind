---
name: perception-verticle-scaffolding
description: "Scaffolds a new perception verticle in the AyoAI Environment Server (Java/Vert.x) end-to-end: writes the {Name}PerceptionVerticle class with the 3-consumer lifecycle, registers all 3 Driver.java sites (isCompleted boolean, deploy method, readiness conjunction) and all 3 CharacterDriverVerticle sites (perceptionKeys entry, count comment, publishPerception call), wires both _done publish channels, adds integration tests, and runs the full gradle suite. Use whenever a goal or user asks to 'create a new perception', 'add a perception verticle', 'scaffold a perception', or to extend NPC perception with a new modality (threat, sound, resource, terrain, etc.). MUST use this skill for new perception verticles — ad-hoc scaffolding misses registration sites and the verticle silently never publishes or wedges character streaming."
forged: true
forged_by: alpha
forged_date: "2026-07-16"
forged_from: gap-002
user-invocable: false
parent-skill: aspirations
triggers:
  - "create a new perception"
  - "add a perception verticle"
  - "scaffold a perception"
  - "new perception modality"
  - "perception verticle scaffolding"
tools_used: [Bash, Read, Write, Edit, Grep, Glob]
companion_scripts: []
conventions: [perception-verticle-scaffolding]
minimum_mode: autonomous
revision_id: "forge-perception-verticle-scaffolding-20260716"
previous_revision_id: null
---

# perception-verticle-scaffolding — New Perception Verticle, End to End

Creates a new `{Name}PerceptionVerticle` in the AyoAI Environment Server and
registers it at every required site. The failure mode this skill prevents:
a verticle that compiles but was registered at only some of the 6 registration
sites — the character streaming cycle then waits forever on a `_done` key that
never arrives, or the verticle deploys but is never asked to publish.

## Step 0: Load Conventions

`Bash: load-conventions.sh perception-verticle-scaffolding` — read the returned
path if not already in context. The convention carries repo roots, grep anchors,
the canonical template file, and domain gotchas (rb-3314, rb-2857, guard-729).

## Step 1: Ground in live code (NEVER scaffold from memory)

```
repo = first existing of the convention's Known roots
Bash: grep -n "ToolPerception" {repo}/src/main/java/AyoServer/Driver.java
Bash: grep -n "perceptionKeys" -A 18 {repo}/src/main/java/AyoServer/Characters/CharacterDriverVerticle.java
Read {repo}/src/main/java/AyoServer/Characters/Perceptions/ToolPerceptionVerticle.java
```

ToolPerceptionVerticle is the live template. Line numbers in the convention are
provenance, not gospel — the grep output is the current truth.

## Step 2: Write the verticle class

`Write {repo}/src/main/java/AyoServer/Characters/Perceptions/{Name}PerceptionVerticle.java`

Mirror the template's shape exactly (see convention §1): 3 consumers
(`completed.CharactersListService`, `startPopulatingPrivateSelf`,
`publishPerception.{Name}PerceptionVerticle`), a private
`gather{Name}Perception(character, unitKey)` method, and a
`"{Name}PerceptionVerticle_done"` publish on BOTH the first-time and streaming
paths. Domain gotchas: use `high()` not `critical()` in catch blocks (rb-3314);
setup must not activate characters (rb-2857).

## Step 3: Register in Driver.java (3 edits)

Per convention §2: completion boolean + `set{Name}PerceptionVerticle()` deploy
call in the startup sequence + deploy method + `&& isCompleted_{Name}PerceptionVerticle`
in the readiness conjunction. Grep for the Tool sites and mirror each one.

## Step 4: Register in CharacterDriverVerticle.java (3 edits)

Per convention §3: `"{Name}PerceptionVerticle_done"` in `perceptionKeys`,
increment the active-verticle count comment above the list, and add the
`publishPerception.{Name}PerceptionVerticle` streaming publish next to its
siblings.

## Step 5: Tests

Copy the shape of `TestToolPerceptionIntegration.java` for the new modality.
Check `TestPerceptionVerticles.java` for a deployed-verticle enumeration that
must include the new class. Run the targeted tests first
(`./gradlew test --tests 'AyoServer.Characters.Test{Name}*' --no-daemon` —
wildcard form per guard-639), then the FULL suite
(`./gradlew test --no-daemon`) before any commit — see
`.claude/rules/run-full-suite-after-deep-code.md`.

## Step 6: Verify registration completeness (the whole point)

```
Bash: grep -c "{Name}PerceptionVerticle" {repo}/src/main/java/AyoServer/Driver.java          # expect >= 4
Bash: grep -c "{Name}PerceptionVerticle" {repo}/src/main/java/AyoServer/Characters/CharacterDriverVerticle.java  # expect >= 2
Bash: grep -c "_done" {repo}/src/main/java/AyoServer/Characters/Perceptions/{Name}PerceptionVerticle.java        # expect >= 2
```

Any count below the floor = a missed registration site. Fix before proceeding.

## Step 7: Commit, push, verify deploy

Per `world/conventions/post-execution.md` Step 2: commit ALL modified files
(Guard-014), push to main, verify CI via `gh run view` + the success email
(Guard-013; rb-3542 — push-to-main auto-deploys to DEV).

## Error handling

- Gradle "No tests found" on a single class → use the wildcard `--tests` form (guard-639).
- Build green but streaming wedged in a live session → a `_done` key missing
  from `perceptionKeys` or a publish channel not wired; re-run Step 6 greps.
- Repo root absent on this box → check the convention's Known roots; product
  estate location differs per machine.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the final verification Bash call (Step 6 greps or the
gradle suite) or the post-execution commit ceremony. Never end with a text summary.
