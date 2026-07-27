"""Blocker-create gate logic — daemon-safe extraction (PR 7a/3).

Hard checks BEFORE writing a new blocker. Catches the five canonical
false-positive failure modes (non-canonical probe / single-signal / unverified
statistical claim / infra blocker without infra-health probe / credentials-required
without per-source identity enumeration). See the CLI
wrapper docstring for the full failure-mode catalog and the rb-NNN crosslinks.

Public API:
    evaluate(blocker, *, probe_command, override_blocker_gate,
             world_dir, agent_name) -> dict

Return shape (matches the legacy CLI's `result` dict byte-for-byte):
    {
      "would_block": bool,
      "checks": [{"name":..., "passed":..., "reason":...}, ...],
      "failing_count": int,
      "override_applied": str|None,
      "reason": str,
      "override_logged_to"?: str,   # only present when override fired + logged
    }

Side effects (both happen inside evaluate()):
    1. When override_blocker_gate is set AND any checks failed: append to
       <world_dir>/blocker-gate-overrides.jsonl via locked_append_jsonl.
       Fail-silent — log failure surfaces on stderr but never propagates.
    2. Always: emit one _gate_log() telemetry record.

Daemon safety:
    - Reads no env directly. world_dir / agent_name are explicit args.
    - SKILLS_DIR / PROJECT_ROOT are constants derived from __file__ at
      module import — safe to cache once at daemon startup.
    - get_companion_scripts (from _skill_md) reads SKILL.md files at call
      time (no module-level cache). Safe under concurrent calls.
    - blocker-recheck.HUMAN_ONLY_BLOCKER_TYPES imported via importlib at
      module load time (filename has hyphen). blocker-recheck.py has an
      `if __name__ == "__main__"` guard so import side effects are nil.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import re
import sys
from pathlib import Path
from typing import Optional

from _fileops import locked_append_jsonl  # type: ignore
from _gate_log import log as _gate_log  # type: ignore
from _skill_md import get_companion_scripts  # type: ignore

try:  # normal package import (core/scripts on sys.path)
    from gates.credential_enum import check as _credential_enum_check
except ImportError:  # loaded with gates/ itself on sys.path
    from credential_enum import check as _credential_enum_check  # type: ignore


SCRIPT_DIR = Path(__file__).resolve().parent.parent  # core/scripts/
PROJECT_ROOT = SCRIPT_DIR.parent.parent              # repo root
SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"


def _import_hyphen_script(filename: str):
    """Load core/scripts/<filename>.py whose name is not a valid Python
    identifier (e.g. blocker-recheck.py). Returns the loaded module."""
    path = SCRIPT_DIR / filename
    spec = importlib.util.spec_from_file_location(
        filename.replace("-", "_").replace(".py", ""), path,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_BR = _import_hyphen_script("blocker-recheck.py")
HUMAN_ONLY_BLOCKER_TYPES = _BR.HUMAN_ONLY_BLOCKER_TYPES


# Statistical-negation patterns. Trip the schema-probe requirement (check 3).
# Must cover all the rb-245 / rb-258 / rb-259 canonical forms. The last
# pattern is broad: "N% [of] records (have|are) X" where X is either a
# zero-ish terminator (0|zero|missing|none) OR a field=value assignment
# whose value is zero-ish. Without the field=value arm the regex misses
# "98% of records have active_brain=0" — the exact rb-245 text.
_ZEROISH = r"(?:0|zero|missing|none)"
_STAT_NEG_PATTERNS = [
    re.compile(r"\b0 records\b", re.I),
    re.compile(r"\bmissing field\b", re.I),
    re.compile(r"\ball \d+ have\b", re.I),
    re.compile(r"\bnone have\b", re.I),
    re.compile(r"\bevery \w+ has \w+=" + _ZEROISH + r"\b", re.I),
    re.compile(
        r"\b\d+(?:%|\s+of\s+\d+)?\s+(?:of\s+)?records\s+(?:have|are)\s+"
        r"(?:" + _ZEROISH + r"|\w+=" + _ZEROISH + r")\b",
        re.I,
    ),
]

# Silent-failure flag patterns — presence of any in a command string makes
# that evidence entry count as ZERO signals. See verify-before-assuming.md
# rule 4. Narrow on purpose: `-q` alone is ambiguous (curl -q disables
# .curlrc; mysqldump -q means "quick"; only a subset of tools use -q for
# silent). Long forms (--silent / --quiet) are caught explicitly.
_SILENT_FAILURE_PATTERNS = [
    re.compile(r"(?<!\w)-sf\b"),
    re.compile(r"(?<!\w)-s\s+-f\b"),
    re.compile(r"\bssh\s+(?:\S+\s+)*-q\b"),
    re.compile(r"2>/dev/null\b"),
    re.compile(r"--silent\b"),
    re.compile(r"--quiet\b"),
]


# --- Per-check helpers -------------------------------------------------------

def _is_human_only(blocker: dict) -> bool:
    return (blocker.get("type") or "") in HUMAN_ONLY_BLOCKER_TYPES


def _check_canonical_probe(blocker: dict, probe_command: Optional[str]) -> dict:
    """Check 1: probe_command must invoke a companion_script of each affected skill.

    Skipped entirely when blocker.type is human-only (no companion_script
    exists for credentials/hardware/user_action flows). Fails open on
    SKILL.md parse errors — a skill with no companion_scripts declared has
    nothing to enforce.
    """
    if _is_human_only(blocker):
        return {"name": "canonical_probe", "passed": True,
                "reason": "skipped: human-only blocker type"}

    affected = blocker.get("affected_skills") or []
    if not affected:
        return {"name": "canonical_probe", "passed": True,
                "reason": "no affected_skills to check"}

    if not probe_command:
        return {
            "name": "canonical_probe",
            "passed": False,
            "reason": ("no --probe-command provided; cannot verify that the "
                       "probe used the skill's canonical companion_script. "
                       "Re-run with --probe-command \"<exact command>\"."),
        }

    probe_text = probe_command.lower()
    missing_canonical = []
    for skill in affected:
        clean = skill.lstrip("/")
        md = SKILLS_DIR / clean / "SKILL.md"
        scripts = get_companion_scripts(md)
        if not scripts:
            continue
        hit = False
        for script in scripts:
            base = str(script).rsplit("/", 1)[-1].lower()
            if base and base in probe_text:
                hit = True
                break
        if not hit:
            missing_canonical.append({
                "skill": clean,
                "companion_scripts": scripts,
            })

    if missing_canonical:
        first = missing_canonical[0]
        return {
            "name": "canonical_probe",
            "passed": False,
            "reason": (
                f"non-canonical probe for {first['skill']}; "
                f"use {first['companion_scripts'][0]} instead. "
                f"Probing via a synthetic command (ssh/curl) can fail where "
                f"the skill's wrapper would succeed — see rb-226 / guard-147."
            ),
            "detail": missing_canonical,
        }
    return {"name": "canonical_probe", "passed": True,
            "reason": f"probe invokes canonical script for all {len(affected)} affected skill(s)"}


def _evidence_is_silent(entry: dict) -> bool:
    cmd = entry.get("command") or ""
    for pat in _SILENT_FAILURE_PATTERNS:
        if pat.search(cmd):
            return True
    return False


def _evidence_key(entry: dict) -> str:
    """Signature for independence: distinct (tool|endpoint|evidence_type)."""
    return (
        str(entry.get("tool") or "")
        + "|" + str(entry.get("endpoint") or "")
        + "|" + str(entry.get("evidence_type") or "")
    )


def _check_multi_signal(blocker: dict) -> dict:
    """Check 2: blocker.evidence must have ≥2 independent non-silent signals."""
    evidence = blocker.get("evidence") or []
    if not isinstance(evidence, list) or not evidence:
        return {
            "name": "multi_signal",
            "passed": False,
            "reason": ("blocker.evidence[] missing or empty; need at least 2 "
                       "independent verification signals before creating a "
                       "blocker (verify-before-assuming rule 1)."),
        }

    valid_entries = [e for e in evidence if isinstance(e, dict) and not _evidence_is_silent(e)]
    silent_count = len(evidence) - len(valid_entries)
    keys = {_evidence_key(e) for e in valid_entries}
    distinct_signals = len([k for k in keys if k != "||"])

    if distinct_signals < 2:
        return {
            "name": "multi_signal",
            "passed": False,
            "reason": (
                f"single-signal negative conclusion: {distinct_signals} "
                f"distinct signal(s) found ({silent_count} silent-failure "
                f"entries do not count). Need ≥2 independent signals "
                f"(different tool, endpoint, or evidence_type) — "
                f"verify-before-assuming.md rule 1."
            ),
        }
    return {
        "name": "multi_signal",
        "passed": True,
        "reason": f"{distinct_signals} distinct signal(s), {silent_count} silent-failure ignored",
    }


def _check_schema_probe(blocker: dict) -> dict:
    """Check 3: statistical negations require schema_probe_evidence."""
    reason_text = blocker.get("failure_reason") or ""
    statistical = any(p.search(reason_text) for p in _STAT_NEG_PATTERNS)
    if not statistical:
        return {"name": "schema_probe", "passed": True,
                "reason": "not a statistical negation; check skipped"}

    probe = blocker.get("schema_probe_evidence")
    if not probe:
        return {
            "name": "schema_probe",
            "passed": False,
            "reason": (
                "statistical negation without schema verification: failure_reason "
                "claims a count/fraction over records but blocker.schema_probe_evidence "
                "is missing. Read ONE live record and verify the claimed field exists "
                "before concluding — rb-245 / rb-258 / rb-259."
            ),
        }
    return {"name": "schema_probe", "passed": True,
            "reason": "schema_probe_evidence provided for statistical claim"}


def _check_infra_health(blocker: dict) -> dict:
    """Check 4: infrastructure blockers must have infra-health probe evidence."""
    if (blocker.get("type") or "") != "infrastructure":
        return {"name": "infra_health", "passed": True,
                "reason": "not an infrastructure blocker; check skipped"}
    probe = blocker.get("infra_health_check")
    if not probe:
        return {
            "name": "infra_health",
            "passed": False,
            "reason": (
                "infrastructure blocker without infra-health probe: "
                "blocker.infra_health_check must show `infra-health.sh check <component>` "
                "was run. Don't declare infrastructure unavailable without it — "
                "verify-before-assuming.md rule 3."
            ),
        }
    return {"name": "infra_health", "passed": True,
            "reason": "infra_health_check present"}


def _check_credential_enumeration(blocker: dict) -> dict:
    """Check 5: credentials-required blockers must enumerate per-source identities.

    Motivated by pq-s3-deleteobject (86h human-gated for a self-serviceable
    grant — the root credential was already in the default CLI chain, but no
    enumeration was ever required, so the untested source was never checked).
    Check 1 (_check_canonical_probe) fails OPEN for human-only types, so
    credentials-required blockers previously bypassed every self-service
    verification. This check restores one for exactly that class.

    THE PREDICATE ITSELF LIVES IN `gates.credential_enum` (g-115-3158) because
    the SAME question is asked at a second door — the blocker_ref payload on
    `aspirations-update-goal.sh` defer_reason / status=blocked writes, which
    previously ran only the 5-key envelope validator and never checked
    credentials at all. One implementation, both doors; a copy here would
    drift. Behavior at this call site is unchanged.
    """
    return _credential_enum_check(blocker)


def _log_override(world_dir: Optional[Path], agent_name: str,
                  blocker: dict, justification: str,
                  failing_checks: list) -> Optional[str]:
    """Append to <world_dir>/blocker-gate-overrides.jsonl. Returns the
    written path on success, None on missing world_dir or write failure.
    Fail-silent on write errors (stderr message; gate proceeds)."""
    if world_dir is None:
        print("[blocker-create-gate] WARN: override granted but not logged "
              "(no WORLD_PATH resolved).", file=sys.stderr)
        return None
    log_path = world_dir / "blocker-gate-overrides.jsonl"
    try:
        record = {
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "agent": agent_name or "unknown",
            "blocker_type": blocker.get("type"),
            "affected_skills": blocker.get("affected_skills") or [],
            "failure_reason": blocker.get("failure_reason", "")[:200],
            "justification": justification,
            "which_checks_bypassed": [c["name"] for c in failing_checks],
        }
        locked_append_jsonl(str(log_path), record)
        return str(log_path)
    except Exception as e:
        print("[blocker-create-gate] WARN: override-log append failed: " + str(e),
              file=sys.stderr)
        return None


def evaluate(blocker: dict, *, probe_command: Optional[str] = None,
             override_blocker_gate: Optional[str] = None,
             world_dir: Optional[Path] = None,
             agent_name: str = "") -> dict:
    """Run all five checks. See module docstring for return shape + side effects.

    Args:
        blocker: parsed blocker JSON dict (type, affected_skills,
            failure_reason, evidence, schema_probe_evidence, infra_health_check).
        probe_command: the exact probe command that was run; required for
            check 1 unless blocker.type is human-only.
        override_blocker_gate: justification string. When non-empty AND any
            check failed, audit-log is written and would_block flips False.
        world_dir: WORLD_DIR for the audit log. None disables audit-log
            writes (matches legacy CLI when MIND_AGENT is unset).
        agent_name: MIND_AGENT value for the audit-log "agent" field.
    """
    checks = [
        _check_canonical_probe(blocker, probe_command),
        _check_multi_signal(blocker),
        _check_schema_probe(blocker),
        _check_infra_health(blocker),
        _check_credential_enumeration(blocker),
    ]
    failing = [c for c in checks if not c.get("passed")]
    would_block = bool(failing) and not override_blocker_gate

    result = {
        "would_block": would_block,
        "checks": checks,
        "failing_count": len(failing),
        "override_applied": override_blocker_gate,
    }
    result["reason"] = failing[0]["reason"] if failing else "all checks passed"

    if override_blocker_gate:
        log_path = _log_override(world_dir, agent_name, blocker,
                                  override_blocker_gate, failing)
        result["override_logged_to"] = log_path

    # Decision derivation.
    if not failing:
        decision = "noop"
        trigger = None
    elif override_blocker_gate:
        decision = "override"
        trigger = failing[0].get("name")
    else:
        decision = "block"
        trigger = failing[0].get("name")
    _gate_log(
        "blocker-create-gate",
        decision,
        trigger_matched=trigger,
        payload=str(blocker)[:500],
        override_reason=override_blocker_gate,
        extra={
            "would_block": would_block,
            "failing_count": len(failing),
            "failing_checks": [c.get("name") for c in failing],
            "all_check_names": [c.get("name") for c in checks],
        },
    )

    return result
