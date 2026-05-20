#!/usr/bin/env python3
"""aspirations-increment-sessions-active.py — atomic per-aspiration sessions_active
counter increment. Implements the Phase 8.1 promise from
core/config/aspirations-loop-digest.md:154 ("asp.sessions_active += 1") that was
previously LLM-residue pseudocode never bashified.

Filed by g-001-217 (HIGH Unblock) after g-001-216 investigation found:
- All 17 active aspirations had sessions_active=0 (counter never advances)
- Read in 3 places (aspirations.py:2031 maturity gate, precheck-eval.py:399
  stalled detection, aspirations-complete-review:346 Phase 7.6 deepening)
- Written nowhere
- Maturity gate would falsely deepen near-complete aspirations on closure
- Stalled-detection branch was effectively dead code

Caller: iteration-close.sh do_state_update (per-goal-close), gated by a
session-scoped tracking file so first-time-per-session semantics match the
LLM-side loop_state.touched but bash-owned for SSOT consistency with the
rb-686 / rb-428 LLM-residue → bash promotion pattern.

Inputs (env vars):
    ASP_ID:  aspiration ID to increment (e.g., asp-115)
    SOURCE:  "world" or "agent" (selects which JSONL to update)

Output:
    stdout: "sessions_active incremented for <id> -> <new_value>" on success
    stderr: warning on failure
    exit 0 always (fail-open — never block iteration-close)

Locks: uses _fileops.acquire_lock on aspirations.jsonl.lock — same primitive
the existing cmd_update / cmd_add_goal paths use.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR, AGENT_DIR  # type: ignore
from _fileops import acquire_lock, release_lock  # type: ignore


def main():
    asp_id = os.environ.get("ASP_ID", "").strip()
    src = os.environ.get("SOURCE", "world").strip()

    if not asp_id:
        print("aspirations-increment-sessions-active: ASP_ID empty", file=sys.stderr)
        sys.exit(0)

    target = Path(AGENT_DIR if src == "agent" else WORLD_DIR) / "aspirations.jsonl"
    if not target.exists():
        print(
            f"aspirations-increment-sessions-active: {target} missing",
            file=sys.stderr,
        )
        sys.exit(0)

    lock = target.with_suffix(".lock")
    try:
        acquire_lock(lock)
        # surrogatepass preserves any lone surrogates already in the file
        # (Windows-origin jsonl content occasionally carries them); without
        # it, the round-trip read+write fails on a "can't encode character"
        # error. ensure_ascii=False on dump keeps Unicode untouched —
        # combined with surrogatepass on write the existing data is preserved.
        with target.open("r", encoding="utf-8", errors="surrogatepass") as f:
            lines = f.read().splitlines()
        out = []
        found = False
        new_val = None
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                asp = json.loads(stripped)
            except Exception:
                out.append(stripped)
                continue
            if asp.get("id") == asp_id:
                asp["sessions_active"] = int(asp.get("sessions_active", 0)) + 1
                new_val = asp["sessions_active"]
                found = True
            # CRITICAL: ensure_ascii=False is load-bearing — paired with
            # errors="surrogatepass" on read (L66) and write (L93) to round-trip
            # Unicode untouched. ensure_ascii=True would silently re-encode every
            # non-ASCII char in pass-through aspirations on every increment call.
            out.append(json.dumps(asp, ensure_ascii=False))
        if found:
            # Atomic write: write to .tmp first, fsync, then os.replace.
            # If we wrote directly to target with mode='w' and the encode
            # failed mid-write, the file would be truncated — exactly the
            # failure mode that destroyed bravo/aspirations.jsonl in the
            # initial run of this script before surrogatepass was added.
            # Always use atomic rename for jsonl writes.
            tmp = target.with_suffix(target.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8", errors="surrogatepass", newline="") as f:
                f.write("\n".join(out) + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, target)
            print(f"sessions_active incremented for {asp_id} -> {new_val}")
        else:
            print(
                f"aspirations-increment-sessions-active: {asp_id} not found in {target}",
                file=sys.stderr,
            )
    except Exception as e:
        print(
            f"aspirations-increment-sessions-active: error for {asp_id}: {e}",
            file=sys.stderr,
        )
    finally:
        try:
            release_lock(lock)
        except Exception:
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
