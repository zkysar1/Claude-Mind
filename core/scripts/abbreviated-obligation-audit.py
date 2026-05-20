#!/usr/bin/env python3
"""Abbreviated-obligation audit — Tier 1a hot-path extraction.

Replaces aspirations-learning-gate/SKILL.md Phase 9.5d (lines 207-330).

Validates claims like "OBLIGATION ABBREVIATED: <phase> — <condition>" in the
current iteration's journal entry. Each claim is audited against:
  - obligation-schema.yaml (is <phase> declared, is <condition> allowed?)
  - iteration-checkpoint.json (outcome_class claim verification)
  - the banner line in the journal entry (context_budget.zone claim)

Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 1a #6).

Appends each claim's verdict to <agent>/session/obligation-audit.jsonl.
If cumulative false-claim count hits the schema's threshold, --apply will
file ONE `Investigate: false-abbreviation-claims` goal per session
(dedup-guarded).

Exit codes: 0=all claims valid, 1=false claims found, 2=input error.
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import AGENT_DIR, PROJECT_ROOT, CORE_ROOT  # type: ignore

try:
    import yaml  # type: ignore
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr)
    sys.exit(2)

try:
    from _fileops import locked_append_jsonl  # type: ignore
except ImportError as e:
    print(f"ERROR: _fileops not importable: {e}", file=sys.stderr)
    sys.exit(2)


# Banner shape — contract with core/scripts/context-budget-banner.sh.
# Keep in sync with context-citation-audit.sh banner_re.
BANNER_RE = re.compile(
    r"^CTX:\s*raw\s*\d+%\s*\|\s*of-autocompact\s*\d+%\s*\|\s*zone\s+(fresh|normal|tight)\s*\|"
)
CLAIM_RE = re.compile(r"^OBLIGATION ABBREVIATED:\s*(\S+)\s*(?:—|-|--)\s*(.+?)\s*$")
HEADER_RE = re.compile(r"^## .*—\s*(?:Goal|Routine):")


def _now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _load_schema():
    path = Path(PROJECT_ROOT) / "core" / "config" / "obligation-schema.yaml"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:  # noqa: BLE001
        print(f"WARN: obligation-schema.yaml parse failed: {e}", file=sys.stderr)
        return None


def _load_checkpoint():
    if AGENT_DIR is None:
        return None
    cp = AGENT_DIR / "session" / "iteration-checkpoint.json"
    if not cp.exists():
        return None
    try:
        with open(cp, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _today_journal():
    if AGENT_DIR is None:
        return None
    now = datetime.now()
    return (AGENT_DIR / "journal" / f"{now.year:04d}" / f"{now.month:02d}"
            / f"{now.strftime('%Y-%m-%d')}.md")


def _last_iteration_section(lines):
    """Slice from the last ## header matching HEADER_RE to EOF."""
    last_idx = -1
    for i, line in enumerate(lines):
        if HEADER_RE.match(line):
            last_idx = i
    return lines[last_idx:] if last_idx >= 0 else []


def _iteration_banner_zone(section_lines):
    """Return the zone from the banner line in this iteration, or None.

    context-budget-banner.sh stamps ONE banner per iteration at iteration
    start (Phase -0.5). Any OBLIGATION ABBREVIATED claim later in the same
    iteration cites the same banner. So we scan the whole section for the
    first banner match — no per-claim walk needed.

    A previous implementation walked forward/backward from each claim and
    stopped at CLAIM_RE, which missed the banner when two claims shared it
    (false-positive audit for tight-zone claims in multi-abbreviation
    iterations). Do NOT reintroduce per-claim bounding.
    """
    for ln in section_lines:
        m = BANNER_RE.match(ln)
        if m:
            return m.group(1)
    return None


def _validate_claim(phase, condition, schema, iter_outcome_class, claim_zone):
    """Returns (valid: bool, failure_reason: str|None)."""
    obligations = (schema or {}).get("obligations") or {}
    spec = obligations.get(phase)
    if spec is None:
        return False, "unknown obligation phase"
    allowed = spec.get("abbreviated_allowed_when") or []
    if condition not in allowed:
        return False, "schema disallows this condition"
    if condition == "outcome_class == routine":
        if iter_outcome_class == "routine":
            return True, None
        return False, f"claim says routine but checkpoint says {iter_outcome_class}"
    if condition == "context_budget.zone == tight":
        if claim_zone is None:
            return False, "banner line missing — citation absent"
        if claim_zone != "tight":
            return False, f"banner says zone={claim_zone} but claim says tight"
        return True, None
    return False, "schema allows it but no verifier"


def _count_false_claims(audit_path):
    if not audit_path.exists():
        return 0
    n = 0
    with open(audit_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("valid") is False:
                n += 1
    return n


def _existing_investigate_goal_present():
    """Check if Investigate: false-abbreviation-claims already exists
    pending/in-progress. Uses aspirations-query via subprocess."""
    bash = "bash"
    try:
        p = subprocess.run(
            [bash, (Path(CORE_ROOT) / "scripts" / "aspirations-query.sh").as_posix(),
             "--goal-status", "pending"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15,
        )
        if p.returncode == 0 and p.stdout.strip():
            if "false-abbreviation-claims" in p.stdout:
                return True
        # Also in-progress
        p2 = subprocess.run(
            [bash, (Path(CORE_ROOT) / "scripts" / "aspirations-query.sh").as_posix(),
             "--goal-status", "in-progress"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15,
        )
        if p2.returncode == 0 and "false-abbreviation-claims" in (p2.stdout or ""):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def main():
    parser = argparse.ArgumentParser(description="Abbreviated-obligation audit (Tier 1a)")
    parser.add_argument("--apply", action="store_true",
                        help="Enable side effects (append audit, create goal on threshold).")
    args = parser.parse_args()

    if AGENT_DIR is None:
        print(json.dumps({"summary": "no agent bound", "flags": ["no_agent"]}))
        sys.exit(2)

    schema = _load_schema()
    if schema is None:
        print(json.dumps({"summary": "obligation-schema.yaml missing/unparseable",
                          "flags": ["schema_missing"]}))
        sys.exit(0)  # fail-open

    checkpoint = _load_checkpoint()
    iter_outcome_class = (checkpoint or {}).get("outcome_class")
    goal_id = (checkpoint or {}).get("goal_id")

    journal = _today_journal()
    if journal is None or not journal.exists():
        print(json.dumps({"summary": "no journal file today",
                          "flags": []}))
        sys.exit(0)

    with open(journal, "r", encoding="utf-8") as f:
        all_lines = f.read().splitlines()
    section = _last_iteration_section(all_lines)
    if not section:
        print(json.dumps({"summary": "no iteration header in today's journal",
                          "flags": []}))
        sys.exit(0)

    # Banner is stamped once per iteration at Phase -0.5; all claims in the
    # section share it. Resolve once outside the loop.
    iter_zone = _iteration_banner_zone(section)

    records = []
    false_count = 0
    for line in section:
        m = CLAIM_RE.match(line)
        if not m:
            continue
        phase = m.group(1).strip()
        condition = m.group(2).strip()
        valid, reason = _validate_claim(phase, condition, schema, iter_outcome_class, iter_zone)
        rec = {
            "timestamp": _now_iso(),
            "goal_id": goal_id,
            "phase": phase,
            "condition_claimed": condition,
            "claim_banner_zone": iter_zone,
            "runtime_outcome_class": iter_outcome_class,
            "valid": valid,
            "failure_reason": reason,
        }
        records.append(rec)
        if not valid:
            false_count += 1

    audit_path = AGENT_DIR / "session" / "obligation-audit.jsonl"
    if args.apply:
        for rec in records:
            try:
                locked_append_jsonl(audit_path, rec)
            except Exception:  # noqa: BLE001
                pass

    cumulative_false = _count_false_claims(audit_path) + (
        false_count if not args.apply else 0  # in --apply we already wrote them
    )

    threshold = (schema.get("abbreviation_audit") or {}).get("false_claim_threshold", 3)
    threshold_breached = cumulative_false >= threshold
    # Filing the Investigate goal is the caller's responsibility (the script has
    # no target aspiration id). We surface threshold_breached in flags; the
    # SKILL.md orchestrator reads it and calls aspirations-add-goal with the
    # correct asp-id. Do NOT add a subprocess here — see plan Tier 1a #6.

    flags = []
    if false_count > 0:
        flags.append("false_claims")
    if threshold_breached:
        flags.append("threshold_breached")

    summary = (
        f"obligation-audit: {len(records)} claim(s), "
        f"{false_count} false this iter, {cumulative_false} cumulative "
        f"(threshold={threshold})"
    )

    print(json.dumps({
        "summary": summary,
        "flags": flags,
        "records_this_iteration": records,
        "false_count_this_iteration": false_count,
        "cumulative_false": cumulative_false,
        "threshold": threshold,
        "threshold_breached": threshold_breached,
        "investigate_goal_already_exists": (
            _existing_investigate_goal_present() if threshold_breached else False
        ),
        "apply_mode": args.apply,
    }, ensure_ascii=False, default=str))
    sys.exit(1 if flags else 0)


if __name__ == "__main__":
    main()
