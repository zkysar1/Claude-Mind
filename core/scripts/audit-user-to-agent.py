#!/usr/bin/env python3
"""Audit pass — drain accumulated participants:[user]-only goals.

For every pending or blocked goal with participants == ["user"] in
world/aspirations.jsonl and in each agent's <agent>/aspirations.jsonl, run
capability-gate.py against the goal's title+description. If the gate finds
an agent-provisionable match the goal should have been tagged [agent, user]
at creation. In --apply mode, promote participants to ["agent", "user"] via
aspirations.py update-goal and append a reclassification record to
world/audit-user-to-agent.jsonl.

Dry-run by default — prints proposed reclassifications without mutating.

Part (1) of g-243-02 (plan: curious-sparking-simon.md). Pairs with the
--evidence flag in capability-gate.py (same goal, Part 2, already shipped).

Usage:
  py -3 core/scripts/audit-user-to-agent.py            # dry-run, prints plan
  py -3 core/scripts/audit-user-to-agent.py --apply    # apply reclassifications
  py -3 core/scripts/audit-user-to-agent.py --limit 5  # test on first 5
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent
GATE_PY = SCRIPT_DIR / "capability-gate.py"
ASP_PY = SCRIPT_DIR / "aspirations.py"

sys.path.insert(0, str(SCRIPT_DIR))
from _fileops import locked_append_jsonl  # noqa: E402
from _paths import agent_dir as _agent_dir, enumerate_agent_confs  # noqa: E402


def _world_dir_for(agent: str) -> Path:
    """Resolve WORLD_PATH from <agent>/local-paths.conf.

    Routes the value through `_path_helpers.absolutize` so a drive-letter
    path is absolutized correctly on POSIX-flavored Python (g-115-733).
    Plain Path(value) returns a relative PosixPath under that flavor.
    """
    conf = _agent_dir(agent) / "local-paths.conf"
    if not conf.is_file():
        return None
    for line in conf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == "WORLD_PATH":
            from _path_helpers import absolutize
            return absolutize(v.strip().strip('"').strip("'"), PROJECT_ROOT)
    return None


def _discover_agents() -> list:
    """Every directory that owns a local-paths.conf is an agent."""
    out = []
    for conf in enumerate_agent_confs():
        out.append(conf.parent.name)
    return out


def _load_jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    recs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return recs


def _find_user_only_goals(source_label: str, asp_path: Path) -> list:
    """Return candidates: goals where participants == ['user'] exactly.

    Each candidate: {source, aspiration_id, goal, file_path}.
    """
    out = []
    for asp in _load_jsonl(asp_path):
        if asp.get("status") in ("archived", "retired", "completed"):
            continue
        for g in (asp.get("goals") or []):
            parts = g.get("participants")
            if not isinstance(parts, list):
                continue
            if [str(p).strip().lower() for p in parts] != ["user"]:
                continue
            if g.get("status") not in ("pending", "blocked"):
                continue
            # Skip deliberate user routing: the audit targets accidental
            # agent-side drift ([user] wrongly set AT CREATION), not goals the
            # user explicitly directed. origin_signal == "user_directive" marks
            # a deliberate [user] choice (e.g. a participants:[user] park signal
            # —  : "DO-NOT-TOUCH: park is participants:[user]
            # ONLY ... Reversal = the user edits participants"). Auto-promoting
            # it to [agent, user] would let the agent act on the goal, violating
            # the directive. ( audit run, 2026-06-09: caught the
            # felt-sense-checkin "lane" keyword false-positive on .)
            if (g.get("origin_signal") or "").strip().lower() == "user_directive":
                continue
            out.append({
                "source": source_label,
                "aspiration_id": asp.get("id"),
                "goal": g,
                "file_path": str(asp_path),
            })
    return out


def _run_gate(failure_reason: str, agent_env: str) -> dict:
    """Invoke capability-gate.py with JSON output. Returns parsed dict or {}.

    Uses --intended-participants user so the gate evaluates as if the goal
    were being routed to [user] right now — matches the original decision
    point we're auditing after the fact.
    """
    env = dict(os.environ)
    env["MIND_AGENT"] = agent_env
    try:
        r = subprocess.run(
            [sys.executable, str(GATE_PY),
             "--failure-reason", failure_reason,
             "--intended-participants", "user",
             "--output", "json"],
            capture_output=True, text=True, env=env,
            encoding="utf-8", errors="replace",
        )
    except Exception as e:
        return {"error": f"gate invocation failed: {e}"}
    if not r.stdout.strip():
        return {"error": "gate produced no output",
                "stderr": (r.stderr or "")[:200]}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        return {"error": f"gate json parse failed: {e}"}


def _update_goal_participants(source: str, goal_id: str,
                              new_participants: list) -> tuple:
    """Call aspirations.py update-goal to set participants. Returns (ok, msg)."""
    env = dict(os.environ)
    # update-goal resolves the queue by --source argument
    try:
        r = subprocess.run(
            [sys.executable, str(ASP_PY),
             "--source", source,
             "update-goal", goal_id, "participants",
             json.dumps(new_participants)],
            capture_output=True, text=True, env=env,
            encoding="utf-8", errors="replace",
        )
    except Exception as e:
        return False, f"update-goal invocation failed: {e}"
    if r.returncode != 0:
        return False, f"rc={r.returncode} stderr={(r.stderr or '').strip()[:200]}"
    return True, (r.stdout or "").strip() or "ok"


def _log_reclassification(world_dir: Path, record: dict) -> None:
    """Append to world/audit-user-to-agent.jsonl. Fail-silent on write errors."""
    if world_dir is None:
        print("[audit-user-to-agent] WARN: no WORLD_PATH -> cannot log",
              file=sys.stderr)
        return
    path = world_dir / "audit-user-to-agent.jsonl"
    try:
        locked_append_jsonl(str(path), record)
    except Exception as e:
        print(f"[audit-user-to-agent] WARN: log append failed: {e}",
              file=sys.stderr)


def _summarize(cands: list) -> str:
    """Short goal summary: title truncated + aspiration + source."""
    lines = []
    for c in cands:
        g = c["goal"]
        title = (g.get("title") or "")[:70]
        lines.append(f"  [{c['source']}/{c['aspiration_id']}] {g['id']}: {title}")
    return "\n".join(lines) if lines else "  (none)"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Audit [user]-only goals and reclassify matches to [agent, user].")
    ap.add_argument("--apply", action="store_true",
                    help="Actually mutate goals. Default is dry-run.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process only the first N candidates (for testing).")
    ap.add_argument("--agent-for-gate", default=None,
                    help="MIND_AGENT env for gate invocations. "
                         "Defaults to current session's agent or the first "
                         "discovered agent dir.")
    args = ap.parse_args(argv)

    _discovered = _discover_agents()
    agent_for_gate = (args.agent_for_gate
                      or os.environ.get("MIND_AGENT", "").strip()
                      or (_discovered[0] if _discovered else ""))
    if not agent_for_gate:
        print("[audit-user-to-agent] No agent found — set MIND_AGENT or create "
              "an agent dir with local-paths.conf.", file=sys.stderr)
        return 2

    # Resolve world dir from the gate-agent's local-paths.conf.
    # ASSUMPTION: all agents in this repo share the same WORLD_PATH. The audit
    # ledger lives in that shared world dir. If agents ever use separate worlds,
    # this script must grow per-agent world resolution — today it does not.
    world_dir = _world_dir_for(agent_for_gate)

    # Build the candidate list from world/aspirations.jsonl + every agent's file.
    candidates = []
    if world_dir is not None:
        candidates += _find_user_only_goals(
            "world", world_dir / "aspirations.jsonl")
    for a in _discover_agents():
        candidates += _find_user_only_goals(
            a, _agent_dir(a) / "aspirations.jsonl")

    if args.limit and args.limit > 0:
        candidates = candidates[:args.limit]

    print(f"[audit-user-to-agent] {len(candidates)} candidate goal(s) with "
          f"participants=['user']")
    if not candidates:
        return 0

    reclassified = []
    unchanged = []
    errors = []

    for c in candidates:
        g = c["goal"]
        failure_reason = (g.get("title") or "").strip()
        desc = (g.get("description") or "").strip()
        if desc:
            failure_reason += "\n" + desc[:500]
        if not failure_reason:
            errors.append({"goal_id": g["id"], "reason": "no title/description"})
            continue

        gate = _run_gate(failure_reason, agent_for_gate)
        if gate.get("error"):
            errors.append({"goal_id": g["id"], "reason": gate["error"]})
            continue

        matches = gate.get("matches") or []
        narrative_patterns = gate.get("narrative_patterns") or []

        # : include narrative-pattern matches as a reclassification
        # signal. Today the gate's would_block only flips on capability-keyword
        # matches; narrative-only matches are pure telemetry at CREATE_BLOCKER
        # time (insufficient signal alone for a hard refusal). At AUDIT time,
        # however, a [user]-only goal whose title/description carries narrative
        # phrasings like "user approves X" or "pending user sign-off" is a
        # routing-drift candidate even without a capability-keyword hit — the
        # narrative phrasing IS the drift signature. Flag for reclassification
        # so a follow-up can verify whether the user-routing was intentional.
        if not matches and not narrative_patterns:
            unchanged.append(c)
            continue

        # Match found (capability OR narrative) — propose promotion to [agent, user].
        if matches:
            top = matches[0]
            matched = top.get("skill") or (top.get("row") or "")[:80] or "(unknown)"
            matched_kw = top.get("matched_keyword") or ""
        else:
            matched = f"narrative-only: {narrative_patterns[0]}"
            matched_kw = narrative_patterns[0]

        action = {
            "goal_id": g["id"],
            "aspiration_id": c["aspiration_id"],
            "source": c["source"],
            "title": g.get("title", ""),
            "old_participants": ["user"],
            "new_participants": ["agent", "user"],
            "matched_capability": matched,
            "matched_keyword": matched_kw,
            "match_count": len(matches),
            "narrative_patterns": narrative_patterns,
        }

        if args.apply:
            # Determine source for aspirations.py: 'world' or 'agent'. For agent
            # sources, MIND_AGENT must point at the owning agent so the script
            # reads the right <agent>/aspirations.jsonl.
            if c["source"] == "world":
                update_source = "world"
                env_agent = agent_for_gate
            else:
                update_source = "agent"
                env_agent = c["source"]
            os.environ["MIND_AGENT"] = env_agent
            ok, msg = _update_goal_participants(
                update_source, g["id"], ["agent", "user"])
            os.environ["MIND_AGENT"] = agent_for_gate  # restore
            action["applied"] = bool(ok)
            if not ok:
                action["apply_error"] = msg[:200]
                errors.append({"goal_id": g["id"], "reason": f"update failed: {msg[:200]}"})
                continue
            _log_reclassification(world_dir, {
                "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
                **action,
            })

        reclassified.append(action)

    # --- Report ---
    print("")
    print(f"Would reclassify: {len(reclassified)}")
    for a in reclassified:
        flag = " [APPLIED]" if a.get("applied") else " [DRY-RUN]"
        print(f"  {a['goal_id']}: '{a['title'][:50]}'{flag}")
        print(f"    [{a['source']}/{a['aspiration_id']}] match: "
              f"{a['matched_capability']} (kw: {a['matched_keyword']})")
    print(f"\nUnchanged (no match): {len(unchanged)}")
    if unchanged:
        print(_summarize(unchanged))
    if errors:
        print(f"\nErrors: {len(errors)}")
        for e in errors:
            print(f"  {e['goal_id']}: {e['reason']}")

    if not args.apply:
        print("\n(dry-run — pass --apply to mutate goals)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
