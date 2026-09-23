# The Deterministic Aspirations-Loop Driver

A deterministic state-machine driver for the aspirations loop. The loop's **orchestration** runs in
code (the Stop hook); the model is called only at the irreducible **cognitive gaps**. A *weak* model
then runs the full loop cleanly and produces accurate, gated, complete research.

> Built + validated 2026-06 against the Zak Code harness (a clean-room, Claude-Code-compatible engine)
> driving this Mind on `gpt-4o-mini`. Runs unchanged on real Claude Code (standard hook/tool contract).

## Thesis: determinism > model

The aspirations loop used to be **model-navigated markdown** — the model read `aspirations/SKILL.md`
and walked its phases. That *orchestration*, not the cognition, was the failure point: `gpt-4o-mini`
overflowed its context re-invoking the orchestrator; even `gpt-4o` burned 22 `use_skill` calls
navigating and never reached the research. Moving orchestration into a deterministic state machine
fixed it — the intelligence was never the bottleneck.

## Architecture

The Stop hook (`stop-hook.sh`, `driver-mode` branch, opt-in via `session/driver-mode`) runs
`aspirations-driver.py` — a state machine whose entire state lives **on disk** (`driver-state.json`).
The **parent is a pure orchestrator**: the driver writes each step's full prompt to a **file**, and
the parent only emits a tiny *"spawn a sub-agent that reads `<file>`"* instruction. So **all cognition
runs in isolated sub-agents**, and the parent's per-turn context stays minimal and **constant
regardless of goal count** (an earlier design inlined the prompts/findings in the parent and drifted
as that context grew). Per goal:

1. **execute** (best-of-N): the driver writes `prompt_1..N.txt` (primed, angle-diverse); the parent
   spawns **N concurrent sub-agents** that each read their prompt file, `web_search`, and write
   `findings_k.json`. Each sub-agent's web research stays in its own isolated context.
2. **synthesize** (isolated): the driver writes `critic-prompt.txt`; the parent spawns **1 critic
   sub-agent** that reads `findings_1..N.json` and merges the best facts → `findings.json`.
3. **verify** (isolated + UNBIASED): the driver writes `judge-prompt.txt`; the parent spawns **1 judge
   sub-agent** that reads `findings.json` and scores it 1–5 → `verdict.json`. Because the judge never
   sees *how* the findings were produced, it reviews them on their merits — a stricter, unbiased gate.
   A **deterministic structural gate** (valid JSON, ≥3 distinct facts) + the judge score → encode or
   bounded redo (`MAX_RETRIES`).
4. **encode** (deterministic): the verified winner → the knowledge tree + an audit journal (+ verdict).

Every parent turn is a single `task` call ending in a **concrete word** (`spawned` / `synthesized` /
`judged`) — the pattern that keeps the weak model reliably on-rails. The driver re-injects each step
from on-disk state, so the parent conversation never accumulates cognitive content → **the loop
scales**, and compaction is rarely even needed.

## The boundary (claude-code ↔ the Mind framework)

- **Substrate (the harness):** emits the hook/tool/lifecycle **contracts** (the seams). It records and
  exposes; it does not orchestrate or decide. The driver runs *on* these seams.
- **Cognition (this driver):** loop orchestration, best-of-N, priming, synthesis, verification,
  encoding. Uses only the standard Claude-Code contract, so it runs unchanged on Claude Code.

### Priming (the determinism thesis, one level down)
A bare sub-agent is not a Mind agent. The driver assembles each sub-agent's Mind context —
identity + program/mission (from `world/program.md`) + research stance — **deterministically** from
world/agent state (NOT the accumulated conversation), so each isolated worker acts AS the Mind agent
for one bounded step.

## Results (gpt-4o-mini)

| Dimension | Result |
| --- | --- |
| Markdown loop | overflow / flail, 0 research reached |
| Driver | clean autonomous loop, accurate on-program research |
| Per-call token curve | flat ~21–28K (was 21K→**111K** derail without sub-agent isolation) |
| Verify-gate judge | discriminates: good findings 4–5/5 (pass), deliberately-bad 1/5 (fail → redo) |
| Parallelization | 60 min → 11 min (5.4×) after the per-sub-agent SessionStart-boot fix |
| Sub-agent hook isolation | a further ~27% faster (2-goal: 664s → 483s) by dropping the parent's 8 PreToolUse[Write] gates per sub-agent write |
| **File-based pure-orchestrator** | parent context **FLAT 21.1K→22.3K regardless of goal count** (parent makes `task` calls only — zero inlined cognition); 2-goal **289s, ~40% faster**; the now-isolated judge is **stricter** (4/5 with substantive issues vs blanket 5/5) |
| Reliability | repeatedly clean 2/2 at small scale; verify-gate gives deserved scores |

## CC-compatibility + the supporting substrate fixes

The driver is `.claude` hooks + scripts only. The harness needed five fixes to emit the Claude-Code
contract faithfully (all merged): the hook contract (cwd/payload/Git-Bash), the tool-name matcher,
`$CLAUDE_PROJECT_DIR` expansion, sub-agents not re-firing `SessionStart`, and sub-agents running their
own (empty) hook set rather than the parent's gates. None of these are cognition — they make the
substrate behave like Claude Code.

Notably, the **file-based redesign** that fixed the scale limit and made the judge unbiased required
**zero new substrate** — it is pure cognition (the existing `task` tool + workspace file I/O). Those
five fixes were the *only* harness work; every quality gain since has lived entirely in the driver.
That is the boundary working as intended: a sufficient substrate, with cognition iterating on top.

## Resolved + remaining

- **Scale stall: RESOLVED.** The earlier inline design drifted at goal 2 of a long run (the parent
  accumulated each goal's prompts/findings). The file-based pure-orchestrator redesign keeps the parent
  context **flat (21.1K→24.6K across a full 6-goal run)** and the loop completes **all 6 goals** cleanly
  (scores 4/5/4/4/5/5), ~110s/goal linear. The parent never accumulates cognitive content.
- **Isolated judge: DONE.** The judge runs as an isolated sub-agent (see *verify* above) — stricter and
  unbiased because it never sees how the findings were produced.
- **Model-call timeout: ADDED** (harness, PR #91, merged): a per-call wall-clock ceiling so a hung
  model call can never block the loop forever (litellm set none) — defense-in-depth beyond the bounded
  context.
- **Remaining (minor):** the judge's self-reported `pass` boolean can disagree with its score (the
  driver gates on the *score*, so this is cosmetic); a redo retries once (`MAX_RETRIES=1`); per-goal
  latency is ~110s, `web_search`-bound (the N attempts already run in parallel).

## Files

- `core/scripts/aspirations-driver.py` — the state machine.
- `core/scripts/stop-hook.sh` — the `driver-mode` branch (fail-safe to the default payload).
- `core/scripts/bash-agent-inject.py` — `core/scripts` on PATH (deterministic bare-name resolution).
- Gated by `session/driver-mode`; state in `agents/<agent>/session/driver-state.json`.
- Per-goal scratch at the workspace root (driver writes the prompts, sub-agents read them and write
  the outputs, driver cleans each goal): `prompt_1..N.txt` / `critic-prompt.txt` / `judge-prompt.txt`
  (file-based prompts) and `findings_k.json` / `findings.json` / `verdict.json` (sub-agent outputs).
