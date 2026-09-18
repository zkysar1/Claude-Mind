"""Split the unreflected reflection queue by resolved_by ownership ().

THE ONE IMPLEMENTATION BEHIND THREE CALLERS (guard-2676). The iteration-close
nudge, review-hypotheses Mode 2, and g-001-08's string precondition all ask the
same question -- "is there reflection work THIS agent may do?" -- and before this
script they each answered it with an ownerless count. guard-5623 says the owner
is `resolved_by` and a live owner's records must be left alone, so the count and
the guardrail disagreed: the nudge fired every close on records the agent was
forbidden to touch (guard-1984, a guardrail cannot outvote the instrument it
guards). Routing every caller through here means the next change to the rule
moves all three together instead of one at a time.

THIS SCRIPT OWNS THE I/O; `_reflectable` OWNS THE RULE. Agent identity and
liveness verdicts are resolved HERE, at the process boundary, and passed into
the pure predicate as explicit arguments -- guard-2601: a predicate that reaches
for MIND_AGENT itself silently answers a different question than its caller
asked, and an always-"held" is indistinguishable from a legitimate abstention.
That split is also what makes the rule unit-testable without a live fleet
(core/scripts/tests/test_reflection_ownership.py).

LIVENESS IS PROBED ONCE PER OWNER, NOT ONCE PER RECORD. The queue is dominated
by a single owner in practice (measured 2026-09-15: 23 of 39 one agent, and a
13-record close nudge where all 13 shared one owner), so this is ~1 subprocess,
not ~13.

guard-3604 (a cross-agent in_flight clear bumps a dormant peer's last_active, so
it reads `alive` for up to 6h) biases this script toward ABSTAINING on a record
it could have reclaimed. That is the safe direction and is why no corroboration
branch is built here: the failure it would prevent is a delayed reflection,
while the failure it could introduce is two conflicting ABC chains on one record
where the loser is silent by construction.

Usage:
  py -3 core/scripts/reflection-ownership-split.py            # JSON
  py -3 core/scripts/reflection-ownership-split.py --nudge    # one stderr line
  ... --agent <name>   override self (default $MIND_AGENT)
  ... --stdin          read the --unreflected array from stdin instead of
                       shelling out to pipeline-read.sh
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import _reflectable as R  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402  (guard-580/581)

MAX_IDS_IN_NUDGE = 5


def _read_queue(use_stdin: bool):
    if use_stdin:
        raw = sys.stdin.read()
    else:
        proc = subprocess.run(
            bash_cmd(SCRIPTS / "pipeline-read.sh", "--unreflected"),
            capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            return None, f"pipeline-read --unreflected rc={proc.returncode}"
        raw = proc.stdout
    if not raw.strip():
        # A zero-byte read is a malfunction, not an empty queue (guard-3707).
        return None, "pipeline-read --unreflected returned 0 bytes"
    try:
        d = json.loads(raw)
    except ValueError as e:
        return None, f"unparseable --unreflected payload: {e}"
    return (d if isinstance(d, list) else []), None


def _liveness(owners, self_agent):
    """agent -> verdict, probed once per DISTINCT owner. Self is never probed
    (guard-6259: never judge your own liveness); the predicate short-circuits on
    self before consulting this map at all."""
    out = {}
    for owner in owners:
        if owner == self_agent:
            continue
        try:
            proc = subprocess.run(
                bash_cmd(SCRIPTS / "liveness-check.sh",
                         "--agent", owner, "--json"),
                capture_output=True, text=True, timeout=120)
            out[owner] = str(json.loads(proc.stdout).get("verdict") or "").lower()
        except Exception:
            # Unreadable liveness classifies `held` in the predicate -- abstain.
            out[owner] = "unknown"
    return out


def main(argv):
    use_stdin = "--stdin" in argv
    nudge = "--nudge" in argv
    self_agent = os.environ.get("MIND_AGENT", "")
    if "--agent" in argv:
        self_agent = argv[argv.index("--agent") + 1]

    records, err = _read_queue(use_stdin)
    if err is not None:
        # Fail OPEN and say so. A close must never wedge on this, but a silent
        # 0 would read as "no reflection work" -- the one answer that is never
        # safe to invent (guard-2298: a zero with two explanations is not a
        # measurement).
        print(json.dumps({"error": err, "actionable": 0, "degraded": True}))
        return 0

    owners = R.owners_of(records)
    live = _liveness(owners, self_agent)
    out = R.split_by_owner(records, self_agent, live)
    out["self_agent"] = self_agent
    out["liveness"] = live
    out["degraded"] = False

    if nudge:
        if out["actionable"] > 0:
            ids = out["mine"] + out["unowned"] + out["reclaimable"]
            shown = ", ".join(str(i) for i in ids[:MAX_IDS_IN_NUDGE])
            if len(ids) > MAX_IDS_IN_NUDGE:
                shown += f", +{len(ids) - MAX_IDS_IN_NUDGE} more"
            line = (f"LLM-ACTION: {out['actionable']} reflectable unreflected "
                    f"hypothesis/es YOU may reflect (mine: {len(out['mine'])}, "
                    f"unowned: {len(out['unowned'])}, "
                    f"reclaimable: {len(out['reclaimable'])}) — {shown}")
            if out["held"]:
                held_by = ", ".join(f"{k}:{v}" for k, v in
                                    sorted(out["held_by"].items()))
                line += (f" | abstaining on {len(out['held'])} held by a live "
                         f"owner ({held_by}) — guard-5623")
            print(line)
        elif out["held"]:
            # NOT SILENT, and deliberately so. A pure filter that printed
            # nothing here would be indistinguishable from an empty queue, and
            # the whole backlog would go invisible to every agent at once.
            # INFO, not LLM-ACTION: there is genuinely nothing this agent may
            # do, so asking it to act would recreate the defect in reverse.
            held_by = ", ".join(f"{k}:{v}" for k, v in
                                sorted(out["held_by"].items()))
            print(f"INFO: 0 reflectable hypotheses are yours — "
                  f"{len(out['held'])} held by a live owner ({held_by}); "
                  f"abstaining per guard-5623")
        return 0

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
