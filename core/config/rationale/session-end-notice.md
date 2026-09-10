# Rationale: the session-end completion report

Behind `aspirations-consolidate` SKILL.md Step 9.7 and
`core/config/aspirations.yaml` → `session_end_notice`.

## The owner's directive (2026-09-10, verbatim)

> "as the agent is shutting down, it should send [a] completion report
> naturally, whether that be vinheim or not! let the agent do it.. that way the
> agent can choose not to do it, or the user can alter how it does it"

Prompted by his report the same day: *"when the agent is done, after an hour
run, for example, two different summery emails are sent."*

## Defect 1 — two summaries per wind-down

Two instruments each own a "tell the user how it went" email:

| | skill | category | dedup |
|---|---|---|---|
| daily briefing | `agent-completion-report` Phase 5.5 | `user-digest` | ONE per 20h, fleet-wide |
| shutdown report | `aspirations-consolidate` Step 9.7 | `completion` | prior-outreach, `_default` window |

The two dedup rules are **independent**, so a 20h digest limit can never
suppress a session-end email and vice versa. Each gate works exactly as
written. Nothing owned the question *"has this human already been told how this
run went?"*

The remedy is deliberately **not** a fourth dedup rule — the fleet already has
three and their mutual blindness is what produced the overlap. It is judgement
(Step 9.7 reads the ledger and may decline) plus one owner-facing knob.

## Defect 2 — the shutdown report had no identity

Measured from `world/notifications-sent.jsonl`, not inferred from code:

```
2026-09-09T20:42:02  alpha    "Session ended — 62 goals closed"  rc=0 SENT
2026-09-09T20:58:15  foxtrot  "Session ended — 45 goals closed"  rc=4 REFUSED
                              suppressed_duplicate_of: the alpha row
2026-09-09T20:59:21  foxtrot  same subject                       rc=0 SENT
                              --allow-duplicate: "Different agent, different
                              box, different work..."
```

Nothing is out of order here — one attempt, a correct refusal, and a deliberate
override 66 seconds later with a written reason. (An earlier reading of this
pair as "the first was refused and the second sent, which is backwards" was
wrong: the two rows are one attempt plus its override, not two attempts.)

What is wrong is that the refusal happened at all. Running the gate's own
functions in `notification_outreach.py`:

- `tokens()` keeps only tokens of length > 2, and the goal **count** was the
  only thing that differed between the two subjects.
- Both therefore reduce to `{closed, ended, goals, session}` — **jaccard 1.00**
  against a `SUBJECT_JACCARD` of 0.60.
- `completion` has no `WINDOW_HOURS` entry, so it inherits `_default` = **168
  hours**.

So the first agent in the fleet to stop owned the topic "session ended" for
**seven days**, and every other agent's shutdown report reached the owner only
by overriding the gate. That is a mute, not a dedup. foxtrot's override was
correct and well-argued; it should never have been necessary.

With the agent name and session number in the subject the same probe scores
**0.50** — under threshold, so two agents' shutdowns are two topics. Control: an
unrelated cost-report subject scores 0.00, so the probe discriminates.

### The trap: a bracket prefix is not an identity

`strip_agent_prefix()` deletes leading `[Alpha]`-style tags *before* matching —
deliberately, so that two agents asking the **same question** dedupe. That is
right for a shared question and wrong for a shutdown report, where the agent is
the topic. The identity must be in the subject **text**.

## What pins this

`core/scripts/tests/test_session_end_subject_identity.py` (6 cases). The
load-bearing one is `test_skill_specifies_identified_subject`, which reads the
subject line out of SKILL.md: the gate was never broken, so a test that only
exercised `notification_outreach` would have passed against the defect
(rb-5828 — callee coverage is not caller evidence). `test_old_shape_collides`
is the negative control; if the historical collision stops reproducing, the
gate's matching changed and the whole file needs re-deriving.

## Prose folded out of Step 9.7 to pay for this change

The hot-path size gate refuses growth in `aspirations-consolidate/SKILL.md`,
and rightly — it loads on every loop iteration of every agent. Two existing
WHY-narratives were folded here to make room, so nothing was lost:

**Why mid-loop consolidations do not email.** They recur every few iterations,
so emailing each one floods the inbox; and the subject carries a changing goal
count, which defeats notify-user's 30-minute rate limiter. Mid-session progress
reporting is `/agent-completion-report` Phase 5.5's job.

**Why the body must be built by `notify-build-payload.py`.** On 2026-07-07 a
delta stop email delivered as title + border and an EMPTY body, because a
Title-only payload was hand-built at the transport: the SendInfoAlert renderer
IGNORES `InfoMessage` whenever `Title` is present (structured mode renders
Body/Sections only). `email-send.sh` now refuses bodyless payloads (exit 2,
empty-body guard). On that refusal, take the fallback — never retry with a
thinner payload.
