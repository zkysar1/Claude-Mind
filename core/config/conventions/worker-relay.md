# Worker relay: where a worker Body's encode-session outputs go

Loaded by `/encode-session --relay` (`.claude/skills/encode-session/SKILL.md`
§ Relay Mode). `/stop`'s worker branch calls that mode as its step 3, and a worker
Body may also run it on its own.

A worker must not write the shared stores. Its learning reaches them when the
reducer replays the Body's working-memory capture lanes after a merge, and a
worker that encodes directly is an Nth reducer (worker-loop § The phase split).
Relay mode keeps every lane's judgment and changes only where each output is
written. Every write goes to this Body's WM: `wm-append.sh` routes there through
the injected `BODY_WM_PATH`.

## Lane mapping

| Lane output | Relay write |
|---|---|
| 1.2 lesson, 1.3 guardrail candidate, 1.4 pattern | `spark_capture` with `sq_trigger` null. Write a guardrail candidate as trigger + rule. |
| 4.1 new work: a fix, follow-up, dependency or capability gap | `spark_capture` with `sq_trigger` `"sq-013"`, shaped as the filing 4.1 would have made: what is wrong or needed; where (path:line, script, store); the evidence measured; the classification; a one-line title. The reducer's spark replay dedups the relay and files it. |
| 1.1 tree fact | ONE `spark_capture` with `sq_trigger` `"sq-013"`, titled "Encode into the knowledge tree". It carries every fact with its evidence, its suggested node and what it supersedes. **Exception:** a fact from a goal this Body COMPLETED goes in `encoding_capture` keyed to that goal (worker-loop Phase 3.66 shape). |
| 1.5 experience | `exp_capture`, only for a goal this Body completed whose unit wrote none (worker-loop Phase 3.6 shape). Otherwise skip it; the session narrative is the stop's session summary. |

## Entry shape

```
Bash: echo '{"goal_id":"<id>","category":"<category>","observation":"<...>","sq_trigger":null}' | bash core/scripts/wm-append.sh spark_capture
```

The shape's single source of truth is worker-loop Phase 3.5. Two fields need care:

- **`goal_id`.** Use the goal the observation came from. For a session-level
  observation, use the literal `worker-stop`.
  - Never leave it null. The replay drains entries by goal_id, so an entry with no
    goal_id is never removed.
  - Never put a host name or SID in it.
- **`load_bearing: true`.** Set it only when the entry supersedes or contradicts an
  encoded conclusion, or unblocks a queued decision (worker-loop § `load_bearing`).
  Do not flag everything.

## Summary line

The Phase Final block prints one line in place of the lane lines:

```
RELAYED spark:<n> (sq-013:<n>) encoding:<n> exp:<n>
```

## Why spark_capture carries almost everything

Its consumer, the reducer's Worker Spark Replay, reads every entry. The
`exp_capture` and `encoding_capture` consumers read entries only for goals a Body
completed, so an entry keyed to anything else is never read. The measurements are
in `core/config/rationale/worker-stop-close.md`.
