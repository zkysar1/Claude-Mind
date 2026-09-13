#!/usr/bin/env python3
"""GS-1's revert-ownership decision, as a pure function the stop path calls.

`aspirations-graceful-stop` Phase GS-1 reverts orphaned in-progress goals to pending.
Deciding WHICH goals it may revert is a destructive judgement that used to live only as
SKILL.md prose, so nothing could test it and nothing could stop it drifting (g-115-9717).
This module is that judgement, extracted so it is script-gated rather than
LLM-discretionary — the same shape as `reducer_self_fence.decide` and
`loop_exhaustion_fence.decide`.

THE RULE IS POSITIVE EVIDENCE, AND IT IS THE WHOLE DESIGN (g-115-9717). A goal is
revertible ONLY when it carries `claimed_by_sid` equal to this session's SID. Every other
shape — key absent, value null, a different SID, an unreadable row — is SKIPPED. There is
no fallback arm, which is what makes a degraded projection safe automatically: no key
means no positive evidence means no revert, with no separate detector to keep in sync.

WHY NOT THE OLD THREE-ARM CASCADE (introduced by g-115-5719, removed by g-115-9717). It
fell back to `claimed_by == <agent>` and then to an `in_flight`-only test. Both are
unsound for the Mind/Body split:

  * `claimed_by` is the AGENT name and every Body of one agent writes the same value, so
    the comparison is `alpha == alpha` for a partner Body's goal — it does not identify
    ownership at all, it just says "same agent", and reverting on it takes work away from
    a live sibling (guard-1460: key claims on claimed_by_sid, NEVER on claimed_by).
  * `in_flight` holds AT MOST ONE goal per agent, so as a protective filter it shields one
    goal and exposes every other one the partner holds — guard-1802's narrow-predicate
    class, where the blast radius grows the harder partners are working.

Collapsing to one arm is not a new restriction; it is the skill's own stated intent
("GS-1 reverts what it owns and leaves the rest") finally matching its code. A genuinely
stranded claim from a dead Body is deliberately left alone here: reclaiming it needs a
liveness/age judgement, which is stranded-claim-sweep's job, not a stop path's.

KEY-ABSENT AND VALUE-NULL ARE REPORTED SEPARATELY, and the distinction is not cosmetic.
The canonical claim writers POP the claim triple on release, so an unclaimed goal has the
key ABSENT — that is the healthy shape. A key PRESENT WITH NULL is the guard-4920 damage
signature (a `--field null` clear that null-fills instead of popping), which
claim-integrity flags forever and no agent-available writer can undo. Both mean "no
positive evidence" and both skip, but only one of them is a defect worth surfacing.

`degraded` answers a different question from any single row: did this BATCH come back
through a projection that carries ownership at all? The default `aspirations-query.sh`
projection returns six keys and none of them is a claim field (guard-1424), so a caller
that forgets `--full` gets rows where every ownership test is vacuously false. Because
sparse JSON also omits the key on genuinely unclaimed goals, key-absence on ONE row is
ambiguous — but a non-empty batch of IN-PROGRESS goals in which NOT ONE row carries the
key is not: an in-progress goal reached that status by being claimed. That is the signal,
and it is advisory here precisely because the positive-evidence rule already made the
outcome safe; it tells an operator the projection is wrong rather than guarding against it.
"""

from __future__ import annotations

from typing import Any

#: The field that identifies the BODY that holds a goal. The only ownership signal this
#: module will act on.
SID_FIELD = "claimed_by_sid"

#: The agent-name field. Read ONLY to report the guard-4920 damage shape and to make the
#: skip reasons legible — never to decide ownership. See the module docstring.
AGENT_FIELD = "claimed_by"


def _row_id(row: dict[str, Any]) -> str | None:
    """The goal id, accepting either key the query surfaces (`id` or `goal_id`)."""
    for key in ("id", "goal_id"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def decide(goals: Any, my_sid: Any) -> dict[str, Any]:
    """Split in-progress goals into what this session may revert and what it must not.

    Pure: no I/O, no clock, no environment. Every ambiguous input resolves toward NOT
    reverting, because a wrong revert takes work from a live sibling Body and a wrong skip
    costs one stale row that stranded-claim-sweep already owns.
    """
    revert: list[str] = []
    skip: list[dict[str, str]] = []
    # Each revertible goal's queue, carried alongside rather than left for the caller to
    # re-join against the query output: `aspirations-update-goal.sh` needs `--source`, and
    # a hand-join between two structures is exactly the step that drifts.
    sources: dict[str, str] = {}

    # A non-list is not an empty batch — it is an unreadable one, and treating it as empty
    # would report a confident "nothing to do" over a failed read.
    if not isinstance(goals, list):
        return {
            "revert": [],
            "skip": [],
            "sources": {},
            "degraded": True,
            "degraded_reason": "goals payload is not a list — projection unreadable",
            "counts": {"total": 0, "revert": 0, "skip": 0, "key_absent": 0, "key_null": 0},
        }

    # Without our own SID there is no positive evidence available for ANY row, so the
    # honest answer is to revert nothing rather than to guess with the agent name.
    sid_known = isinstance(my_sid, str) and bool(my_sid.strip())

    key_absent = 0
    key_null = 0

    for index, row in enumerate(goals):
        if not isinstance(row, dict):
            skip.append({"goal_id": f"<row {index}>", "reason": "row is not an object"})
            continue

        goal_id = _row_id(row) or f"<row {index}>"

        if SID_FIELD not in row:
            key_absent += 1
            skip.append({"goal_id": goal_id, "reason": "no claim recorded (no positive evidence of ownership)"})
            continue

        holder = row.get(SID_FIELD)
        if holder is None:
            key_null += 1
            agent = row.get(AGENT_FIELD)
            damage = " — claim field null-filled, see guard-4920" if agent is not None else ""
            skip.append({"goal_id": goal_id, "reason": f"claim field present but null{damage}"})
            continue

        if not isinstance(holder, str) or not holder.strip():
            skip.append({"goal_id": goal_id, "reason": f"unreadable {SID_FIELD} ({holder!r})"})
            continue

        if not sid_known:
            skip.append({"goal_id": goal_id, "reason": "this session's SID is unknown — cannot prove ownership"})
            continue

        if holder == my_sid:
            revert.append(goal_id)
            queue = row.get("source")
            sources[goal_id] = queue if isinstance(queue, str) and queue else "world"
        else:
            skip.append({"goal_id": goal_id, "reason": f"claimed by another Body {holder}"})

    # Advisory only — the positive-evidence rule above already made the outcome safe.
    degraded = bool(goals) and key_absent == len(goals)
    degraded_reason = ""
    if degraded:
        degraded_reason = (
            f"{key_absent} of {len(goals)} in-progress rows carry no '{SID_FIELD}' key at all; "
            "an in-progress goal reached that status by being claimed, so the projection is "
            "probably missing --full (guard-1424). Nothing was reverted."
        )
    if not sid_known and goals:
        degraded = True
        degraded_reason = (degraded_reason + " " if degraded_reason else "") + (
            "This session's SID was not supplied, so no row could be proven ours."
        )

    return {
        "revert": revert,
        "skip": skip,
        "sources": sources,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
        "counts": {
            "total": len(goals),
            "revert": len(revert),
            "skip": len(skip),
            "key_absent": key_absent,
            "key_null": key_null,
        },
    }


def _main(argv: list[str]) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description="Decide which in-progress goals GS-1 may revert (positive evidence only)."
    )
    parser.add_argument("--sid", default="", help="this session's SID ($MIND_SID)")
    parser.add_argument(
        "--goals-file",
        default="-",
        help="path to the aspirations-query.sh --full JSON (default: - for stdin)",
    )
    args = parser.parse_args(argv)

    raw = sys.stdin.read() if args.goals_file == "-" else open(args.goals_file, encoding="utf-8").read()
    try:
        goals = json.loads(raw)
    except (ValueError, TypeError) as exc:
        # A parse failure is the most degraded projection there is: say so in the same
        # shape callers already branch on, rather than raising into the stop path.
        print(
            json.dumps(
                {
                    "revert": [],
                    "skip": [],
                    "sources": {},
                    "degraded": True,
                    "degraded_reason": f"could not parse the goals payload: {exc}",
                    "counts": {"total": 0, "revert": 0, "skip": 0, "key_absent": 0, "key_null": 0},
                }
            )
        )
        return 0

    print(json.dumps(decide(goals, args.sid), indent=2))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
