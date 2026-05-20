#!/usr/bin/env python3
"""Obligation Abbreviation Audit — Phase 9.5d of aspirations-learning-gate.

Ported from the skill's pseudocode (lines 200-289) into a standalone script so
iteration-close.sh's hot path can call it cheaply. The skill body itself is
currently dead code on the hot path because iteration-close.sh does not Skill()
back into aspirations-learning-gate — it inlines the bookkeeping. This script
closes that gap.

What it does:
  1. Reads core/config/obligation-schema.yaml for the authoritative allow-list.
  2. Reads <agent>/session/context-budget.json → .zone
  3. Reads <agent>/session/iteration-checkpoint.json → .outcome_class, .goal_id
  4. Reads today's journal file; scopes to the last iteration section
     (header "## {ts} — Goal:" or "## {ts} — Routine:"); extracts
     `OBLIGATION ABBREVIATED: {phase} — {condition}` lines.
  5. For each line: validates whether the claimed condition was true at
     iteration time against the runtime state. Appends one record per claim
     to <agent>/session/obligation-audit.jsonl.
  6. If session-cumulative false-claim count >= threshold (from schema), files
     ONE Investigate goal (deduped via aspirations-query.sh).

Non-blocking. Always exits 0. Errors go to stderr.

Usage: obligation-audit.py (no required args — reads MIND_AGENT env)
       Optional --goal-id to override checkpoint.goal_id in records.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

sys.path.insert(0, str(SCRIPT_DIR))
from _paths import AGENT_DIR  # type: ignore

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _warn(msg: str) -> None:
    print(f"[obligation-audit] {msg}", file=sys.stderr)


def _load_schema() -> dict:
    path = CORE_ROOT / "config" / "obligation-schema.yaml"
    try:
        import yaml  # type: ignore
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        _warn(f"schema load failed: {e}")
        return {}


def _read_json_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        _warn(f"{path.name} parse failed: {e}")
        return None


def _journal_path(agent_dir: Path) -> Path:
    now = datetime.now()
    return agent_dir / "journal" / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.strftime('%Y-%m-%d')}.md"


def _extract_current_section(text: str) -> str:
    """Slice from the LAST '## {ts} — Goal:' or '## {ts} — Routine:' header to EOF."""
    # Header regex: flexible on the em-dash vs hyphen and the leading spaces.
    pattern = re.compile(r"^## .* — (?:Goal|Routine):", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        # Some encodings produce "-" instead of "—"; fall back to broader match
        alt = re.compile(r"^##\s+.*(?:Goal|Routine)\s*:", re.MULTILINE)
        matches = list(alt.finditer(text))
        if not matches:
            return ""
    last = matches[-1]
    return text[last.start():]


def _parse_abbreviations(section: str) -> list[tuple[str, str]]:
    """Return list of (phase, condition) from lines starting with the marker."""
    out = []
    for line in section.splitlines():
        s = line.strip()
        if not s.startswith("OBLIGATION ABBREVIATED:"):
            continue
        body = s[len("OBLIGATION ABBREVIATED:"):].strip()
        # Accept either em-dash or hyphen separator
        if "—" in body:
            phase, _, cond = body.partition("—")
        elif " - " in body:
            phase, _, cond = body.partition(" - ")
        else:
            phase, _, cond = body.partition("-")
        phase = phase.strip()
        cond = cond.strip()
        if phase:
            out.append((phase, cond))
    return out


def _validate(phase: str, condition: str, budget_zone: str, outcome_class: str | None, schema: dict) -> bool:
    obligations = (schema or {}).get("obligations") or {}
    spec = obligations.get(phase)
    if not spec:
        return False
    allowed = spec.get("abbreviated_allowed_when") or []
    if condition not in allowed:
        return False
    # Verify the two conditions we know how to validate against runtime state.
    if condition == "outcome_class == routine":
        return outcome_class == "routine"
    if condition == "context_budget.zone == tight":
        return budget_zone == "tight"
    # Schema allows it but we have no runtime check for it — mark invalid so it
    # surfaces rather than rubber-stamping unknown conditions.
    return False


def _append_jsonl(path: Path, record: dict) -> None:
    try:
        # Use _fileops for session/ files — it history-skips these paths.
        from _fileops import locked_append_jsonl  # type: ignore
        locked_append_jsonl(str(path), record)
    except Exception as e:
        _warn(f"locked_append failed, falling back to plain append: {e}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _existing_investigate_goal() -> bool:
    try:
        r = subprocess.run(
            ["bash", (SCRIPT_DIR / "aspirations-query.sh").as_posix(),
             "--goal-status", "pending", "--goal-status", "in-progress"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return False
        data = json.loads(r.stdout)
        entries = data if isinstance(data, list) else data.get("goals", [])
        for e in entries:
            title = (e.get("title") or "") if isinstance(e, dict) else ""
            if title.startswith("Investigate: false-abbreviation-claims"):
                return True
        return False
    except Exception as e:
        _warn(f"aspirations-query failed: {e}")
        return False


def _file_investigate_goal(session_false_claims: int, audit_log_path: Path) -> None:
    # Find a target aspiration (first active framework-maintenance, else first active).
    try:
        r = subprocess.run(
            ["bash", (SCRIPT_DIR / "world-cat.sh").as_posix(), "aspirations-compact.json"],
            capture_output=True, text=True, timeout=15,
        )
        target_id = None
        target_source = "world"
        if r.returncode == 0 and r.stdout.strip():
            try:
                compact = json.loads(r.stdout)
            except Exception:
                compact = []
            active = [a for a in (compact if isinstance(compact, list) else [])
                      if a.get("status") == "active"]
            fm = [a for a in active if (a.get("category") or "") == "framework-maintenance"]
            pick = (fm or active or [None])[0]
            if pick:
                target_id = pick.get("id")
                target_source = pick.get("source", "world")
        if not target_id:
            _warn("no target aspiration found — investigate goal not filed")
            return

        goal = {
            "title": "Investigate: false-abbreviation-claims",
            "description": (
                f"Phase 9.5d logged {session_false_claims} abbreviation claim(s) "
                f"whose conditions were not true at iteration time. Investigate: "
                f"which obligations are being abbreviated incorrectly, and why "
                f"the claimed condition didn't hold. Consult {audit_log_path} for per-claim detail."
            ),
            "priority": "HIGH",
            "participants": ["agent"],
            "category": "framework-maintenance",
            # origin-signal-gate: the obligation-audit log IS the signal.
            # Cite it so the audit trail connects goal → triggering record.
            "origin_signal": f"investigate:obligation-audit-{session_false_claims}-false-claims",
        }
        subprocess.run(
            ["bash", (SCRIPT_DIR / "aspirations-add-goal.sh").as_posix(),
             "--source", target_source, target_id],
            input=json.dumps(goal), text=True, capture_output=True, timeout=15,
        )
        print(f"[obligation-audit] filed investigate goal in {target_id} "
              f"after {session_false_claims} false claim(s)")
    except Exception as e:
        _warn(f"investigate goal filing failed: {e}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Audit OBLIGATION ABBREVIATED journal claims.")
    ap.add_argument("--goal-id", default=None, help="Override checkpoint.goal_id for record tagging.")
    args = ap.parse_args(argv)

    if AGENT_DIR is None:
        _warn("AGENT_DIR unset — no-agent mode, skipping")
        return 0

    agent_dir = Path(AGENT_DIR)
    schema = _load_schema()
    budget = _read_json_file(agent_dir / "session" / "context-budget.json") or {}
    budget_zone = budget.get("zone") or "normal"
    checkpoint = _read_json_file(agent_dir / "session" / "iteration-checkpoint.json") or {}
    outcome_class = checkpoint.get("outcome_class")
    checkpoint_goal = checkpoint.get("goal_id")
    goal_id = args.goal_id or checkpoint_goal

    journal = _journal_path(agent_dir)
    if not journal.exists():
        return 0
    try:
        text = journal.read_text(encoding="utf-8")
    except Exception as e:
        _warn(f"journal read failed: {e}")
        return 0

    section = _extract_current_section(text)
    if not section:
        return 0
    claims = _parse_abbreviations(section)
    if not claims:
        return 0

    audit_log = agent_dir / "session" / "obligation-audit.jsonl"
    now_iso = datetime.now().isoformat(timespec="seconds")

    # Count pre-existing false claims in this session's audit log so we don't
    # double-file the investigate goal. One audit log lives for the whole
    # session; this audit is scoped to the current iteration only.
    session_false_claims = 0
    if audit_log.exists():
        try:
            for ln in audit_log.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                rec = json.loads(ln)
                if rec.get("valid") is False:
                    session_false_claims += 1
        except Exception as e:
            _warn(f"audit log re-read failed: {e}")

    new_false_this_iter = 0
    for phase, cond in claims:
        valid = _validate(phase, cond, budget_zone, outcome_class, schema)
        record = {
            "timestamp": now_iso,
            "goal_id": goal_id,
            "phase": phase,
            "condition_claimed": cond,
            "runtime_zone": budget_zone,
            "runtime_outcome_class": outcome_class,
            "valid": valid,
        }
        _append_jsonl(audit_log, record)
        if not valid:
            new_false_this_iter += 1
            try:
                subprocess.run(
                    ["bash", (SCRIPT_DIR / "wm-append.sh").as_posix(), "sensory_buffer"],
                    input="false_abbreviation", text=True,
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass

    session_false_claims += new_false_this_iter
    threshold = (
        (schema.get("abbreviation_audit") or {}).get("false_claim_threshold")
        or 3
    )
    if new_false_this_iter > 0 and session_false_claims >= threshold:
        if not _existing_investigate_goal():
            _file_investigate_goal(session_false_claims, audit_log)

    if new_false_this_iter:
        print(f"[obligation-audit] iter: {new_false_this_iter} false / {len(claims)} claim(s); "
              f"session cumulative false: {session_false_claims}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
