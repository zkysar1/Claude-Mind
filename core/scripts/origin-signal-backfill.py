#!/usr/bin/env python3
"""One-shot backfill of origin_signal on every existing goal.

Run ONCE when flipping the origin-signal-gate to strict. Safe to re-run —
already-tagged goals are left alone.

Derivation rules (checked in order; first match wins). Each rule is a
deterministic read of existing goal structure, NOT a fallback:

  1. goal.origin_signal is already set and valid → leave it
  2. goal.title starts with "Unblock:"      → unblock:<asp-id>
  3. goal.title starts with "Investigate:"  → investigate:<asp-id>
  4. goal.title starts with "Idea:"         → idea:<asp-id>
  5. goal.title starts with "Maintain:"     → maintain:<asp-id>
  6. goal has parent_goal_id                → decomposition:<parent_goal_id>
  7. "user" in goal.participants            → user_directive
  8. else                                   → parent_aspiration:<asp-id>

Rule 8 is definitional, not a fallback: every goal lives inside exactly one
aspiration, so parent_aspiration:<asp-id> is always true for any goal.

Usage:
  py -3 core/scripts/origin-signal-backfill.py            # dry run
  py -3 core/scripts/origin-signal-backfill.py --apply    # writes changes
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _paths  # noqa: E402
from _paths import agents_root as _agents_root  # noqa: E402

# Load the gate's is_valid() via source-level exec — the gate's filename has
# hyphens, so normal import won't work. The __name__ override suppresses the
# gate's `if __name__ == "__main__": main()` tail so it doesn't try to read
# stdin on our behalf. This gives us ONE source of truth for what's valid.
_gate_src = (Path(__file__).resolve().parent / "origin-signal-gate.py").read_text(encoding="utf-8")
_gate_ns = {"__name__": "origin_signal_gate_loader"}
exec(compile(_gate_src, "origin-signal-gate.py", "exec"), _gate_ns)
_is_valid = _gate_ns["is_valid"]


def derive(asp_id, goal):
    existing = goal.get("origin_signal")
    if _is_valid(existing):
        return existing, "kept-valid"

    title = (goal.get("title") or "").strip()
    tl = title.lower()
    if tl.startswith("unblock:"):
        return f"unblock:{asp_id}", "title:unblock"
    if tl.startswith("investigate:"):
        return f"investigate:{asp_id}", "title:investigate"
    if tl.startswith("idea:"):
        return f"idea:{asp_id}", "title:idea"
    if tl.startswith("maintain:"):
        return f"maintain:{asp_id}", "title:maintain"

    parent_gid = goal.get("parent_goal_id")
    if parent_gid:
        return f"decomposition:{parent_gid}", "parent_goal_id"

    parts = goal.get("participants") or []
    if "user" in parts:
        return "user_directive", "participants:user"

    return f"parent_aspiration:{asp_id}", "parent_aspiration"


def backfill_file(path: Path, apply: bool):
    if not path.exists():
        print(f"SKIP {path} (not found)")
        return 0, 0
    rewrote = 0
    touched_goals = 0
    records = []
    # READ: surrogatepass tolerates CESU-8 bytes if any slipped in previously.
    # WRITE: ensure_ascii=True is MANDATORY — json.loads can produce Python
    # strings with lone surrogates when the input has \uD8XX/\uDCXX escapes.
    # With ensure_ascii=False, the surrogate would be encoded as CESU-8
    # (invalid UTF-8). With ensure_ascii=True, it stays a JSON \uXXXX escape
    # — round-trip-safe ASCII output, strict UTF-8 valid. Do NOT change this.
    with path.open("r", encoding="utf-8", errors="surrogatepass") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                records.append(line)
                continue
            asp = json.loads(line)
            asp_id = asp.get("id") or "unknown"
            for goal in asp.get("goals", []):
                before = goal.get("origin_signal")
                derived, reason = derive(asp_id, goal)
                if before != derived:
                    goal["origin_signal"] = derived
                    touched_goals += 1
                    print(f"  {asp_id}/{goal.get('id')}: {before!r} -> {derived!r} ({reason})")
            records.append(json.dumps(asp, ensure_ascii=True))
            rewrote += 1
    if apply and touched_goals > 0:
        tmp = path.with_suffix(path.suffix + ".tmp")
        # strict encoding on write — output is pure ASCII (ensure_ascii=True
        # above), so strict is guaranteed safe and will loudly catch any
        # future regression that reintroduces non-ASCII on the write path.
        with tmp.open("w", encoding="utf-8") as f:
            f.write("\n".join(records))
            if records:
                f.write("\n")
        tmp.replace(path)
    # NOTE: skip rewrite when touched_goals == 0. Rewriting a clean file
    # would reserialize every record with ensure_ascii=True, producing a
    # large noisy diff (any non-ASCII char becomes \uXXXX). We only rewrite
    # files that actually needed updates.
    print(f"{'APPLIED' if apply else 'DRY'} {path}: {rewrote} aspirations, {touched_goals} goals updated")
    return rewrote, touched_goals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    args = ap.parse_args()

    targets = []
    world_jsonl = _paths.WORLD_DIR / "aspirations.jsonl"
    targets.append(world_jsonl)
    for agent_dir in sorted(_agents_root().iterdir()):
        if not agent_dir.is_dir():
            continue
        if agent_dir.name in {".git", ".claude", "core", "meta", "world"}:
            continue
        if agent_dir.name.startswith("."):
            continue
        agent_jsonl = agent_dir / "aspirations.jsonl"
        if agent_jsonl.exists():
            targets.append(agent_jsonl)

    total_goals = 0
    for t in targets:
        _, g = backfill_file(t, args.apply)
        total_goals += g
    print(f"\nTotal goals updated: {total_goals}")
    if not args.apply:
        print("Dry run — re-invoke with --apply to write.")


if __name__ == "__main__":
    main()
