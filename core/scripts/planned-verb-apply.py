#!/usr/bin/env python3
"""Apply one addressed Planned-board verb to one goal (, PEARL B2b).

The round-trip's last leg: ``handle + verb + value`` -> resolved goal -> planned field
writes -> the canonical goal writer. Three existing pieces do the real work and this
script is deliberately thin glue between them, so none of their contracts is re-stated
here in a second place that could drift:

* ``knowledge-export.resolve_handle`` turns an opaque published handle back into exactly
  one goal id, box-side, with no stored mapping (g-369-119);
* ``planned_verbs.plan_verb`` decides WHICH fields a verb writes, enforces the positive
  opt-in gate, and refuses any write that would touch a field the projection reads;
* ``aspirations-update-goal.sh`` performs the write, so this path inherits the daemon
  contract, the changelog and the merge handling rather than reimplementing them.

REPORT-ONLY BY DEFAULT. ``--apply`` is explicit, for the same reason
``worker-ref-consume.sh`` defaults to a readable diff: this is a write path against LIVE
MEMBER DATA, and a near-miss mutates a real member's goal. The default output is the
plan you can read before authorising it.

EXIT CODES, and they are the signal:
  0  plan produced (``--apply``: writes landed)
  2  usage error — the caller erred. NEVER read as "refused"
  3  refused — unknown verb, unresolvable handle, goal not opted in, or invalid value

An unresolvable handle and a goal that has not opted in are refused IDENTICALLY, and
print nothing on stdout. Distinguishing them would tell an unauthenticated caller which
handles exist — the same reason ``knowledge-export --resolve-handle`` prints nothing on
a miss.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _runtime_bash import BASH  # noqa: E402
from planned_verbs import plan_verb  # noqa: E402

_UPDATE_WRAPPER = SCRIPT_DIR / "aspirations-update-goal.sh"


def _load_export_mod():
    """``knowledge-export.py`` is hyphenated, so it cannot be a plain import.

    Same loader shape ``test_knowledge_export.py`` uses; keeping it identical means the
    module under test and the module in production are loaded the same way.
    """
    path = SCRIPT_DIR / "knowledge-export.py"
    spec = importlib.util.spec_from_file_location("knowledge_export_for_verbs", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import plumbing
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_field(goal_id: str, field: str, value: object, source: str) -> int:
    """One field write through the canonical wrapper.

    ``None`` clears the field — that is what makes every verb reversible, so it is passed
    through as the literal the wrapper understands rather than skipped.
    """
    payload = "" if value is None else str(value)
    # guard-580: never a bare "bash" argv[0]; BASH is resolved once at import.
    proc = subprocess.run(
        [BASH, _UPDATE_WRAPPER.as_posix(), "--source", source, goal_id, field, payload],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"planned-verb-apply: write failed for {goal_id}.{field}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:400]}\n"
        )
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Apply one Planned-board member verb.")
    ap.add_argument("--handle", required=True, help="opaque published goal handle")
    ap.add_argument("--verb", required=True, help="prioritize | pause | not-this | comment")
    ap.add_argument("--value", default="", help="verb value (priority, on/off, or text)")
    ap.add_argument("--source", default="world", choices=("world", "agent"))
    ap.add_argument("--apply", action="store_true", help="perform the writes")
    ap.add_argument("--json", action="store_true", help="machine-readable plan")
    args = ap.parse_args(argv)

    export = _load_export_mod()
    world = export._resolve_world()
    root = SCRIPT_DIR.parent.parent

    goal_id = export.resolve_handle(world, root, args.handle)
    goal = None
    if goal_id:
        for g in export._read_goals(world):
            if str(g.get("id") or g.get("goal_id") or "").strip() == goal_id:
                goal = g
                break

    # goal is None for BOTH an unresolvable handle and a resolved-but-vanished record;
    # plan_verb refuses it identically to a non-opted-in goal. Do not split these.
    plan = plan_verb(goal, args.verb, args.value)
    if not plan.ok:
        sys.stderr.write(f"planned-verb-apply: refused ({plan.refusal})\n")
        return 3

    out = {"goal_id": goal_id, "verb": args.verb, "writes": plan.writes, "applied": False}
    if args.apply:
        rc_total = 0
        for field, value in plan.writes.items():
            rc_total |= _write_field(goal_id, field, value, args.source)
        if rc_total != 0:
            return 1
        # Stamp the defer clock only when a defer was actually written, so the selector's
        # age arithmetic has a basis. Ordering matters: the reason is on the record before
        # the timestamp, never a timestamp pointing at nothing.
        if plan.writes.get("defer_reason"):
            _write_field(
                goal_id, "defer_reason_set_at",
                __import__("datetime").datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                args.source,
            )
        out["applied"] = True

    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        verb_writes = ", ".join(f"{k}={v!r}" for k, v in sorted(plan.writes.items()))
        print(f"{goal_id}: {args.verb} -> {verb_writes}"
              + ("" if args.apply else "   (dry run — pass --apply)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
