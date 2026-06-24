"""Stale-read gate logic — daemon-safe extraction (PR 7c/1).

Blocks new goals filed by agents on stale parent context. Fires when the
new goal cites `parent_goal`, the agent HAS a tracked read receipt for that
parent, AND the parent was modified after that read (Block 2 below). The
"agent never read it" branch (formerly Block 1) is RETIRED (g-115-1572):
it passes unconditionally. The read-receipt mechanism is structurally
false-positive-prone -- parent context reaches an agent via compact data, the
goal selector, or a prior session, not only daemon reads -- so an absent
goal-reads.jsonl receipt is a zero signal (all 29 historical overrides were
this branch, every one a false positive where the agent HAD read the parent).
The gate now evaluates ONLY Block 2 (a tracked receipt that is stale).

Public API:
    evaluate(payload, *, override, agent_name, world_dir, agent_dir) -> dict

Payload shape:
    {"parent_goal": "g-NNN-NN", "agent": "alpha"}

Return shape (matches the legacy CLI's stdout JSON byte-for-byte):
    {
      "would_block": bool,
      "reason": str,
      "parent_goal": str | None,
      "parent_last_modified": str | None,
      "agent_last_read": str | None,
      "override_applied": str | None,
      # Daemon-only field — added so the CLI shim can re-derive exit code:
      "_fail_open": bool,
    }

Decision telemetry (`_gate_log`) is emitted INSIDE evaluate() so daemon
callers get parity with the CLI. CLI shim must NOT emit telemetry again.

Daemon safety:
  - Reads no env directly. agent_name / world_dir / agent_dir are explicit.
  - `_log_override` audit-ledger writes are best-effort and degrade
    gracefully when world_dir is None (the legacy CLI's WORLD_DIR-is-None
    early-out is preserved).
  - File reads (_find_goal, _agent_last_read) take Path args; no globals.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Optional

from _gate_log import log as _gate_log  # type: ignore


# ---------------------------------------------------------------------------
# File-scanning helpers (pure — take Path args, no globals)
# ---------------------------------------------------------------------------

def _find_goal(goal_id: str, world_dir: Optional[Path],
               agent_dir: Optional[Path]) -> Optional[dict]:
    """Search live aspirations.jsonl in world+agent for `goal_id`. None if
    missing in both. Archive paths are intentionally NOT searched — archived
    goals are terminal, not a stale-context concern.
    """
    for d in (world_dir, agent_dir):
        if d is None:
            continue
        path = d / "aspirations.jsonl"
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        asp = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for g in asp.get("goals", []) or []:
                        if g.get("id") == goal_id:
                            return g
        except OSError:
            continue
    return None


def _agent_last_read(parent_goal_id: str, agent: str,
                     agent_dir: Optional[Path]) -> Optional[str]:
    """Return the most-recent read_at ISO for parent_goal_id by agent. None
    if file missing, no matching entry, or every line unparseable.
    """
    if not agent or agent_dir is None:
        return None
    log = agent_dir / "session" / "goal-reads.jsonl"
    if not log.exists():
        return None
    latest = None
    try:
        with open(log, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("goal_id") != parent_goal_id:
                    continue
                if rec.get("agent") != agent:
                    continue
                ts = rec.get("read_at")
                if not ts:
                    continue
                if latest is None or ts > latest:
                    latest = ts
    except OSError:
        return None
    return latest


def _log_override(world_dir: Optional[Path], parent_goal: str, agent: str,
                  justification: str, parent_last_modified: Optional[str],
                  agent_last_read: Optional[str]) -> None:
    """Append override audit record. Best-effort: write failures are silent
    (override decision already made; ledger is observability, not gate state).
    """
    if world_dir is None:
        return
    rec = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "agent": agent,
        "parent_goal": parent_goal,
        "parent_last_modified": parent_last_modified,
        "agent_last_read": agent_last_read,
        "justification": justification,
    }
    try:
        from _fileops import locked_append_jsonl
        locked_append_jsonl(world_dir / "stale-read-overrides.jsonl", rec)
    except Exception:
        # Match legacy: stderr warning only when caller wants it. The daemon
        # doesn't have a stderr to surface; the CLI shim handles its own
        # error messaging if needed.
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate(payload: dict, *, override: Optional[str] = None,
             agent_name: str = "",
             world_dir: Optional[Path] = None,
             agent_dir: Optional[Path] = None) -> dict:
    """Run the gate. Returns the legacy CLI's JSON payload + a `_fail_open`
    flag so the CLI shim can re-derive exit code.

    Args:
        payload: {"parent_goal": str, "agent": str}.
        override: justification string; when non-empty AND the gate would
            have blocked, writes audit record + returns would_block=False.
        agent_name: filing agent's name. When payload.agent is absent, this
            is used. The legacy CLI also fell back to $MIND_AGENT — that
            env read is now the CLI shim's responsibility, not this module's.
        world_dir: WORLD_DIR for parent-goal scan + override audit ledger.
        agent_dir: AGENT_DIR for parent-goal scan + goal-reads scan.

    Side effects:
        - Override audit write (when override fires AND would have blocked).
        - Single _gate_log() record per call.
    """
    parent_goal = (payload or {}).get("parent_goal")
    agent = (payload or {}).get("agent") or agent_name or ""

    def _telemetry(decision: str, out: dict) -> None:
        """Emit ONE _gate_log per evaluate() call. Daemon and CLI both go
        through evaluate(), so telemetry must live here (CLI shim no longer
        emits separately). Best-effort — never raises."""
        try:
            _gate_log(
                "stale-read-gate",
                decision,
                payload={
                    "parent_goal": out.get("parent_goal"),
                    "parent_last_modified": out.get("parent_last_modified"),
                    "agent_last_read": out.get("agent_last_read"),
                    "override_applied": out.get("override_applied"),
                },
            )
        except Exception:
            pass

    # Skip 1: no parent_goal → gate not applicable.
    if not parent_goal:
        out = {
            "would_block": False,
            "reason": "no parent_goal field — gate does not apply",
            "parent_goal": None,
            "parent_last_modified": None,
            "agent_last_read": None,
            "override_applied": None,
            "_fail_open": False,
        }
        _telemetry("pass", out)
        return out

    # Skip 2: parent not found → fail-open. Parent may be archived (terminal)
    # or missing entirely. Either way, stale-context isn't a concern.
    parent = _find_goal(parent_goal, world_dir, agent_dir)
    if parent is None:
        out = {
            "would_block": False,
            "reason": f"parent_goal {parent_goal} not found in live queues — fail-open",
            "parent_goal": parent_goal,
            "parent_last_modified": None,
            "agent_last_read": None,
            "override_applied": None,
            "_fail_open": True,
        }
        _telemetry("fail_open", out)
        return out

    parent_lm = parent.get("last_modified")

    # Skip 3: legacy parent without last_modified → fail-open (pass).
    # Backward compat: absent field is "unknown", not stale.
    if not parent_lm:
        out = {
            "would_block": False,
            "reason": "parent has no last_modified field (legacy goal) — fail-open",
            "parent_goal": parent_goal,
            "parent_last_modified": None,
            "agent_last_read": None,
            "override_applied": None,
            "_fail_open": False,
        }
        _telemetry("pass", out)
        return out

    last_read = _agent_last_read(parent_goal, agent, agent_dir)

    # No read receipt for this parent: the never-read check is RETIRED
    # (2, decision B; was a temporary fail-open under 9).
    # The read-receipt mechanism is structurally false-positive-prone: an agent
    # acquires parent context through many paths -- compact aspiration data, the
    # goal selector's output, a prior session, a direct read -- but only a daemon
    # goal-read would ever write goal-reads.jsonl, and the daemon read path does
    # NOT write it (documented Phase-A/B scope-cut in
    # mind_api/src/endpoints/aspirations.py + aspirations_query.py). So an absent
    # receipt is a ZERO signal, not evidence of an unread parent
    # (verify-before-assuming.md: a silently-absent signal is zero signals).
    # Evidence: all 29 ledger overrides were this branch, every one a false
    # positive where the agent HAD read the parent (world/stale-read-overrides
    # .jsonl); zero genuine Block-2 stale-reads ever fired. Restoring a
    # daemon-safe goal-read writer (option A) was REJECTED -- it would re-create
    # that 100%-override friction (the FP cause is multi-path context
    # acquisition, which a write-on-read receipt cannot capture) at the cost of
    # write-on-every-goal-read amplification. The genuine check (Block 2 below)
    # still fires when a tracked receipt EXISTS and is stale.
    if last_read is None:
        out = {
            "would_block": False,
            "reason": (
                f"no tracked read receipt for parent_goal {parent_goal}; "
                f"never-read check retired (g-115-1572). The gate evaluates only "
                f"a tracked-receipt-that-is-stale (Block 2) -- pass."
            ),
            "parent_goal": parent_goal,
            "parent_last_modified": parent_lm,
            "agent_last_read": None,
            "override_applied": None,
            "_fail_open": True,
        }
        # Telemetry decision is "pass", NOT "fail_open" (8). This
        # branch is a retired-PASS: the never-read check no longer exists, it
        # permits unconditionally -- exactly like Skip 3's legacy-goal pass --
        # so the analytics bucket is "pass", not a gate-couldn't-evaluate
        # fail-open. Logging it "fail_open" made gate-retirement-eval.py
        # mislabel a by-design pass as "the gate raised an exception" and file
        # a false Investigate goal every cycle. `_fail_open` STAYS True so the
        # CLI shim keeps the deliberate 9/1572 exit-2 operational
        # signal -- `_fail_open` is read ONLY by the CLI shim (stale-read-gate
        # .py); the sole production consumer (daemon aspirations_write.py)
        # branches on `would_block`, which is unchanged. Post-fix,
        # decision="fail_open" uniquely identifies Skip-2 parent-not-found (a
        # genuine fail-open) -- improving, not reducing, telemetry diagnosability
        # (guard-502).
        _telemetry("pass", out)
        return out

    # Block 2: parent modified after agent's most-recent read.
    # ISO 8601 string comparison is valid because both timestamps use the
    # same shape (datetime.isoformat(timespec="seconds")) and any-zone
    # homogeneity holds within this codebase (local system time per CLAUDE.md).
    if parent_lm > last_read:
        if override:
            _log_override(world_dir, parent_goal, agent, override, parent_lm, last_read)
            out = {
                "would_block": False,
                "reason": "override applied (stale read)",
                "parent_goal": parent_goal,
                "parent_last_modified": parent_lm,
                "agent_last_read": last_read,
                "override_applied": override,
                "_fail_open": False,
            }
            _telemetry("override", out)
            return out
        out = {
            "would_block": True,
            "reason": (
                f"stale read: parent_goal {parent_goal} last_modified at {parent_lm} "
                f"but agent {agent!r} last read at {last_read}. Re-read the parent "
                f"(cmd_read --id <asp_id> or cmd_query) before filing the child. "
                f"Pass --override-stale-read \"<justification>\" if the modification "
                f"is irrelevant to the child being filed."
            ),
            "parent_goal": parent_goal,
            "parent_last_modified": parent_lm,
            "agent_last_read": last_read,
            "override_applied": None,
            "_fail_open": False,
        }
        _telemetry("block", out)
        return out

    # Pass: read is fresh (last_read >= parent_lm).
    out = {
        "would_block": False,
        "reason": "fresh read",
        "parent_goal": parent_goal,
        "parent_last_modified": parent_lm,
        "agent_last_read": last_read,
        "override_applied": None,
        "_fail_open": False,
    }
    _telemetry("pass", out)
    return out
