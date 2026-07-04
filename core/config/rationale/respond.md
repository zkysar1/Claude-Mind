# Rationale: respond

Referenced from `.claude/skills/respond/SKILL.md`. WHY reasoning for four
structural choices across Step 4b Reader-Mode Observation Surfacing, Step 6.5
Post-Edit Tree Reconciliation, and Step 7.6 Mid-Session Cadence Nudge.

## Why reader mode surfaces observations in response text rather than writing (Step 4b)

Reader mode has no write capability — observations the agent notices during
retrieval (a node looks stale, two conventions contradict, a fact in the
tree disagrees with a fact from the workspace codebase) would otherwise
vanish at session end.

Approach A (this implementation): the agent surfaces the observation in
its response text. The user can either ignore it OR mode-switch to
assistant and run `/encode-session` (or a targeted /respond directive)
to capture it. Zero infrastructure — no carve-out from reader's no-write
contract.

## Why Step 6.5 exists separately from Step 6 (Step 6.5 intro)

Step 6 fires on user CORRECTIONS of belief ("X is actually Y"). Step 6.5
fires on user-DIRECTED edits ("fix the bug in script X", "update convention
Y to say Z") — world-changing actions that don't surface as belief
corrections but still invalidate tree knowledge that describes the edited
file. Without this step, the assistant-mode equivalent of autonomous Phase
4.5 is missing: knowledge stays stale until the user happens to ask about
the file and notices the tree is out of date.

Sister mechanism to Step 6's broad re-retrieve (G12/R15), but driven by the
agent's own writes rather than the user's narrative correction.

## Why Step 6.5 follows Step 6 and is not part of Step 5 (Step 6.5 ordering)

**Order vs Step 6**: Step 6 runs FIRST (covers user-correction-driven
reconciliation, which is more specific). Step 6.5 then handles the residual
case where edits happened without belief correction.

**Why not part of Step 5**: Step 5 routes directives into writes; Step 6/6.5
react to writes already-done. Separation keeps Step 5's directive table
clean (one row per directive type) and concentrates encoding-reaction in
6.x.

## Why Step 7.6 uses a cadence nudge rather than forced encoding (Step 7.6)

In assistant mode the autonomous loop isn't running, so the only encoding
trigger that fires automatically is the PostToolUse mechanical hook (T21).
All substantive encoding lanes (Lane 1.x of `/encode-session`) require
explicit user invocation. Long assistant sessions that end abruptly
(window closed, network drop, user moves on) lose every learning that
hasn't been encoded yet — including the `knowledge_debt` entries Steps 4.5
and 6.5 just filed.

This step is a cheap mitigation: count substantive turns, and at a
configured cadence, surface a one-line nudge in the response inviting the
user to run `/encode-session`. No forced writes. No blocking. The user can
ignore the nudge; the counter resets on invocation.

## Cross-references

- `core/config/conventions/encoding-triggers.md` E5 row — Reader-Mode Observation Surfacing trigger catalog entry (Step 4b)
- `core/config/conventions/encoding-triggers.md` E2 row — Post-Edit Tree Reconciliation trigger catalog entry (Step 6.5)
- `core/config/conventions/encoding-triggers.md` E4 row — Mid-Session Cadence Nudge trigger catalog entry (Step 7.6)
- G12/R15 — Step 6 broad re-retrieve (sister mechanism to Step 6.5)
- T21 — PostToolUse mechanical hook (only auto-encoding trigger in assistant mode)
- `.claude/skills/respond/SKILL.md` — consumer of this rationale
