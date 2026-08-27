# Chat-Goal Protocol Digest

Referenced from `.claude/skills/respond/SKILL.md` Step 5.0b. Loaded on demand —
never in the hot path.

The autonomous loop runs every unit of work through
retrieve → execute → verify → encode, anchored on a **goal record**. Assistant
mode runs the same kind of work with none of that scaffolding, so chat work is
invisible to the learning loop unless someone remembers to run
`/encode-session`. This digest is how a substantive chat request gets the loop's
disciplines **without entering the loop** — the mode boundary is deliberate user
control and is not crossed here.

Owner ask that motivated it (2026-08-21, verbatim intent): *"is there a way to
force an agent to get into one aspiration loop, and take that end to end for
each user goal?"*

## The classifier — conservative by design

Fire the lane ONLY when the request is a **substantive work request**: it is
multi-step AND it produces durable artifacts (code, config, framework files, a
knowledge-tree change, a deployment).

Do NOT fire on:

- Questions, explanations, status checks, "what does X do", "why did Y happen"
- Single mechanical edits (a typo, one config value, a rename)
- Anything already routed by a Step 5 directive row (self update, new
  aspiration, priority review, remember-fact, …) — those have their own lanes
  and a second goal record would double-count
- Work the user frames as exploratory ("just poke at", "have a look")

**Over-firing is the failure mode to fear, not under-firing.** The owner likes
chat fast; turning a two-minute answer into goal ceremony is a regression even
when the bookkeeping is correct. When genuinely uncertain, DO NOT fire — the
existing `/encode-session` and Maintain lanes still catch the learning, just
later. One skipped goal record costs a little attribution; one unnecessary
ceremony costs the user's patience on every turn afterwards.

## The four legs

**1 — FILE the goal record, before executing.**

```
Bash: cat <<'JSON' | bash core/scripts/aspirations-add-goal.sh <asp-id> --source <world|agent>
{
  "title": "<the work, as the user framed it>",
  "priority": "MEDIUM",
  "participants": ["agent"],
  "goal_source": "user",
  "origin_signal": "chat-goal:<short-slug-of-the-request>",
  "status": "in-progress",
  "description": "<the user's request, verbatim>. Filed by the chat-goal lane (respond Step 5.0b).",
  "verification": {"outcomes": ["<stated UP FRONT, before any work>"]}
}
JSON
```

**The two fields answer different questions, and getting them backwards is the
defect this lane shipped with (g-306-342, corrected 2026-08-26).** `goal_source`
is a CLOSED vocabulary answering WHO INITIATED — and a chat request is initiated
by the user, so it is `user`, exactly like every other user-pushed goal. It is
NOT `chat-originated`: that value is not in `_goal_source.VALID_GOAL_SOURCES`
and never was, so a record carrying it would have been unrecognised by every
consumer that filters on user-sourced work (drift denominators, US-06
attribution). The daemon write path does not validate `goal_source`, so it would
have landed silently rather than failing — the flattering direction.

WHICH LANE filed it is `origin_signal`'s job, and `chat-goal:` is a **sanctioned
prefix** in `gates/origin_signal.py ALLOWED_PREFIXES`, kept locked with the
`user` branch of `_goal_source.infer()` (which maps it back to `user`).
Registration is load-bearing, not tidiness: per guard-2329 an unsanctioned
prefix does NOT reliably fail — the write returns rc=0 and a goal id while the
signal is silently rewritten to a title-derived one, which would leave the
adoption census below querying a key that was never stored, vacuous forever.

Scope-route the goal the same way any other goal is routed: framework/product
work to `world`, agent-private work to `agent`.

**Verification criteria are stated BEFORE execution, not after.** This is the
whole difference between a goal record and a receipt. Criteria written after the
work are a description of what happened; criteria written first can fail.

**2 — EXECUTE under the disciplines that already exist.** No new machinery: the
pre-apply consultation (two queries, subject and mechanism), Tier 2.5 retrieval
escalation, and the full-suite recommender all apply exactly as they do in the
loop. Chat mode has never been exempt from them; it has only lacked the record
that makes skipping them visible.

**3 — CLOSE with evidence.** Write the `outcome_note` with what was actually
measured, then set the status. A chat goal that ends without an `outcome_note`
has produced a record with no evidence in it, which is worse than no record —
it reads as verified work to every later consumer.

**4 — ENCODING FOLLOWS AUTOMATICALLY.** Nothing further to invoke. Once a closed
goal record exists, the standard pipeline, completion reports, and utilization
feedback all see the work, because every one of them is keyed on goal records
rather than on transcripts.

## Prior art — this is a generalization, not a new idea

The **Maintain** primitive already does exactly this for inline fixes: file the
goal with `status: completed` so the standard encoding pipeline fires over work
that was already done. The chat-goal lane is the same move extended from
after-the-fact to in-progress, which is what makes leg 1's up-front criteria
possible.

The companion **retrieve** leg shipped separately on 2026-08-21 as the
`UserPromptSubmit` auto-retrieval pre-pass (`user-prompt-retrieval-inject.sh`),
which covers retrieval per turn. This digest adds execute / verify / encode.

## Measuring adoption

**`origin_signal` prefix `chat-goal:` is the census key** — NOT `goal_source`,
which reads `user` for this lane and cannot discriminate it (122 of 173
user-sourced goals already carry a bare `user_directive`, so that field answers
a different question and answers it the same way for every one of them).

**`aspirations-query.sh --goal-field` CANNOT run this census — it matches
EXACTLY, not by prefix, and its refusal is silent.** Measured 2026-08-26:
`--goal-field origin_signal "unblock:"` returns 0 records while the store holds
175 distinct `unblock:<slug>` signals. A prefix census written that way is
vacuous forever and fails in the flattering direction — the same defect this
section is being rewritten to remove, one layer down. Scan directly:

```
source core/scripts/_paths.sh && py -3 - "$WORLD_PATH/aspirations.jsonl" <<'EOF'
import json, sys
tot = hit = ctl = 0
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line: continue
    for g in json.loads(line).get("goals", []):
        tot += 1
        s = g.get("origin_signal") or ""
        if s.startswith("chat-goal:"): hit += 1
        if s.startswith("unblock:"):   ctl += 1     # positive control
print("goals=%d  chat-goal:=%d  (control unblock:=%d)" % (tot, hit, ctl))
EOF
```

The control must share the query's SHAPE. A bare tag like `user_directive`
carries no suffix, so it succeeds under exact matching and proves nothing about
prefix behaviour — that is exactly the control that let the broken query above
look correct.

Its SHAPE matters more than its level: a count that climbs steadily on a
chat-heavy day is the lane working, while a count that fires on nearly every
turn is the classifier over-firing and should be tightened, not celebrated.

**Positive-control the zero before reading it as under-firing** (guard-2298): run
the same query shape against a prefix known to be populated. Measured 2026-08-26
at correction time the count was 0 across 3,060 goals against 2,266 for
`agent-self` — a real zero, and the reason this section was rewritten: the census
had been keyed on a `goal_source` value that is not in the enum, so it could
never have counted anything even after the lane fired. Re-measured with the
prefix scan above: `chat-goal:=0` against a same-shape control of
`unblock:=175`. The lane has still never fired; that is now a MEASUREMENT
rather than an artifact of a census that could not count.

## Cross-references

- `.claude/skills/respond/SKILL.md` Step 5.0b — the pointer that reaches this file
- `.claude/skills/encode-session/SKILL.md` — the manual lane this reduces reliance on
- `core/config/conventions/hot-path-size-budget.md` — why this content lives here
  and not in `respond/SKILL.md`
- `core/config/conventions/learning-routing.md` — where each kind of learning goes
- CLAUDE.md § Cognitive Primitives — the Maintain primitive this generalizes
