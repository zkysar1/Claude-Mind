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
from collections import Counter
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

sys.path.insert(0, str(SCRIPT_DIR))
from _paths import AGENT_DIR  # type: ignore
from _runtime_bash import bash_cmd  # noqa: E402  # : Windows-safe bash resolution

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


# Two vocabularies name the same four obligations, and nothing mapped between them
# (, measured 2026-09-17). The journal/loop label the phases
# `verify` / `state-update` / `learning-gate` / `spark`; obligation-schema.yaml keys them
# `verify` / `state` / `learn` / `spark`. `_parse_abbreviations` passes the journal label
# straight through, so `obligations.get("state-update")` was always None and the `if not
# spec` arm below returned False for EVERY claim on two of the four phases — regardless of
# the condition text or the runtime state. Positive control on the same call, same schema,
# same runtime: `state` and `learn` return True on `context_budget.zone == tight` with
# budget_zone == "tight", while `state-update` and `learning-gate` return False. So the
# "false abbreviation claims" this module files Investigate goals about were its own false
# positives — both live rows on cc-05 claimed zone==tight with runtime_zone=="tight", which
# both `state` and `learn` explicitly allow.
# Per rb-1915, the normalizer is a TOTAL function onto the schema vocabulary: a label with
# no alias passes through unchanged and still fails at `if not spec`, so an unrecognised
# phase surfaces rather than being rubber-stamped — the safe direction is preserved.
# Fallback only. The map is declared in obligation-schema.yaml `phase_aliases:` so that
# this module and `abbreviated-obligation-audit.py` — which validate the same claims and
# had this defect INDEPENDENTLY — cannot drift apart again. This literal keeps the
# normalizer working if the schema is unreadable at audit time.
_PHASE_ALIASES = {
    "state-update": "state",
    "learning-gate": "learn",
}


def _normalize_phase(phase: str, schema: dict | None = None) -> str:
    phase = (phase or "").strip()
    aliases = ((schema or {}).get("phase_aliases") or {}) or _PHASE_ALIASES
    return aliases.get(phase, phase)


def _normalize_condition(condition: str) -> str:
    """Strip a trailing explanation from a claimed condition.

    The journal line is `OBLIGATION ABBREVIATED: <phase> — <condition>`, and an agent
    that also records WHAT it did inline writes the canonical token followed by the
    explanation. That explanation arrives in TWO punctuations, not one: parenthesised
    (`context_budget.zone == tight (tree node updated; findings routed)`) or as a
    FOLLOWING SENTENCE (`context_budget.zone == tight. Steps 8, 8.5 and 8.55 ran in
    full.`). `condition not in allowed` is exact membership, so the more informative
    claim scored invalid while a bare token passed.

    Until 2026-09-21 only the parenthesised form was stripped, and the SENTENCE form
    was the live shape. Measured that day over the whole fleet corpus (14 records,
    5 agents, read out of the remote store because this log is push-only telemetry):
    of 13 false verdicts, 2 were caused by nothing but this punctuation — `state` and
    `learn` claims at zone tight whose own `claim_banner_zone` corroborated the
    condition was TRUE. Widening admitted exactly those 2 and nothing else (guard-1828:
    sweep a matcher over the real corpus before claiming it is narrow).

    Cutting at ". " (period + SPACE) is safe: no canonical token contains that pair —
    `context_budget.zone` has a period with no space after it — so a claim naming a
    genuinely different condition still fails. Keep this body identical to its twin in
    `abbreviated-obligation-audit.py`; `test_obligation_audit_phase_vocabulary.py` pins
    the two together (tree node `two-parsers-one-invariant`).
    """
    cond = (condition or "").strip()
    cut = len(cond)
    for sep in (" (", ". "):
        i = cond.find(sep)
        if i != -1:
            cut = min(cut, i)
    return cond[:cut].strip()


def _validate_claim(phase: str, condition: str, budget_zone: str,
                    outcome_class: str | None, schema: dict) -> tuple[bool, str | None]:
    """Returns (valid, failure_reason) — reason vocabulary shared with the twin.

    WHY A REASON AND NOT JUST A BOOLEAN (g-115-10407, measured 2026-09-21). A bare
    `False` collapses three different things into one word, and the goal this module
    FILES then reports all three as "conditions were not true at iteration time".
    Measured over the whole fleet corpus that day (14 records, 5 agents): 5 of the 13
    false verdicts were `unknown obligation phase` — the agent named a phase this
    schema does not govern at all, so no condition was ever evaluated — and 2 more were
    TRUE conditions rejected on punctuation. NOT ONE was an obligation abbreviated
    while its condition was false. A reader who takes the filed goal's wording at face
    value investigates a defect that is not there; the reason is what makes the count
    decomposable. `abbreviated-obligation-audit.py::_validate_claim` already returned
    one — this module, the one that files the goal, did not.
    """
    obligations = (schema or {}).get("obligations") or {}
    phase = _normalize_phase(phase, schema)
    condition = _normalize_condition(condition)
    spec = obligations.get(phase)
    if not spec:
        return False, "unknown obligation phase"
    allowed = spec.get("abbreviated_allowed_when") or []
    if condition not in allowed:
        return False, "schema disallows this condition"
    # Verify the two conditions we know how to validate against runtime state.
    if condition == "outcome_class == routine":
        if outcome_class == "routine":
            return True, None
        return False, f"claim says routine but checkpoint says {outcome_class}"
    if condition == "context_budget.zone == tight":
        # SOURCE-NEUTRAL WORDING IS LOAD-BEARING, not a style choice (fresh-eyes
        # 2026-09-21, zeta/cc-02). This module tallies `failure_reason` into ONE
        # Counter and renders it as the `BY REASON:` breakdown of the goal it
        # files, so the reason is a CATEGORY KEY. The twin reads its zone from the
        # claim banner and this one from the runtime budget; wording the key by
        # source ("banner says…" vs "runtime zone=…") split one category in two
        # and would fragment any cross-auditor tally. Provenance belongs in the
        # record's own `claim_banner_zone` / zone fields, never in the key.
        if budget_zone is None:
            # Absent zone is NOT a mismatch. Without this the f-string below
            # rendered the Python literal `None` into a filed goal's description
            # and blamed a zone disagreement for what is a missing citation.
            return False, "zone citation absent"
        if budget_zone != "tight":
            return False, f"observed zone={budget_zone} but claim says tight"
        return True, None
    # Schema allows it but we have no runtime check for it — mark invalid so it
    # surfaces rather than rubber-stamping unknown conditions.
    return False, "schema allows it but no verifier"


def _validate(phase: str, condition: str, budget_zone: str, outcome_class: str | None, schema: dict) -> bool:
    """Boolean face of :func:`_validate_claim` — kept so existing callers and the
    cross-auditor parity test keep a single comparable verdict."""
    valid, _reason = _validate_claim(phase, condition, budget_zone, outcome_class, schema)
    return valid


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
            bash_cmd(SCRIPT_DIR / "aspirations-query.sh",
                     "--goal-status", "pending", "--goal-status", "in-progress"),
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


def _file_investigate_goal(session_false_claims: int, audit_log_path: Path,
                           reasons: "Counter[str] | None" = None,
                           phases: "Counter[str] | None" = None) -> None:
    # Find a target aspiration (first active framework-maintenance, else first active).
    try:
        # : read the compact from the AGENT-SESSION path, not $WORLD_DIR.
        # The previous `world-cat.sh aspirations-compact.json` resolved to
        # $WORLD_DIR/aspirations-compact.json, which DOES NOT EXIST — the compact is
        # written by load-aspirations-compact.sh to AGENT_DIR/session/. world-cat.sh
        # is a bare `cat`, so the miss returned rc=1 + empty stdout, the guard below
        # failed, target_id stayed None, and this function NEVER filed. Since it is
        # the enforcement arm for false abbreviation claims (Phase 9.5d), the whole
        # escalation path was structurally dead.
        # Mirrors findings-gate.py:192-205 — refresh best-effort FIRST so the target
        # is picked from a FRESH snapshot; a bare path swap would merely trade
        # not-found for the  stale-read defect.
        target_id = None
        target_source = "world"
        compact_path = (Path(AGENT_DIR) / "session" / "aspirations-compact.json"
                        if AGENT_DIR else None)
        loader = SCRIPT_DIR / "load-aspirations-compact.sh"
        if compact_path is not None and loader.exists():
            try:
                subprocess.run(
                    bash_cmd(loader),
                    env={**os.environ,
                         "MIND_AGENT": os.environ.get("MIND_AGENT", "")},
                    capture_output=True, timeout=10, check=False,
                )
            except Exception:
                pass
        if compact_path is not None and compact_path.exists():
            try:
                compact = json.loads(compact_path.read_text(encoding="utf-8"))
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

        reason_line = ", ".join(
            f"{n} {r}" for r, n in sorted((reasons or {}).items(), key=lambda kv: (-kv[1], kv[0]))
        ) or "not recorded (log predates failure_reason)"
        phase_line = ", ".join(
            f"{p or '(blank)'} x{n}" for p, n in sorted((phases or {}).items(), key=lambda kv: (-kv[1], kv[0]))
        ) or "not recorded"
        goal = {
            "title": "Investigate: false-abbreviation-claims",
            "description": (
                f"Phase 9.5d scored {session_false_claims} abbreviation claim(s) INVALID "
                f"this session. READ THE BREAKDOWN BEFORE ASSUMING AN OBLIGATION WAS "
                f"ABBREVIATED IMPROPERLY — 'invalid' covers three different things and "
                f"only one of them is that (g-115-10407, measured 2026-09-21 over the "
                f"whole fleet corpus: of 13 false verdicts, 5 were phases the schema does "
                f"not govern at all, 2 were TRUE conditions rejected on punctuation, and "
                f"ZERO were an obligation abbreviated while its condition was false).\n\n"
                f"BY REASON: {reason_line}.\n"
                f"BY PHASE (schema-normalized): {phase_line}.\n\n"
                f"'unknown obligation phase' means the agent named a phase outside "
                f"obligation-schema.yaml's `obligations:` keys, so no condition was ever "
                f"evaluated — the fix is vocabulary (either the claim or the schema), not "
                f"discipline. 'schema disallows this condition' means the phase is governed "
                f"but the condition text did not match an `abbreviated_allowed_when` token. "
                f"Only a reason naming the runtime state ('runtime zone=...', 'claim says "
                f"routine but checkpoint says ...') is an obligation abbreviated on a "
                f"condition that did not hold.\n\n"
                f"Per-claim detail: {audit_log_path}. That log is push-only telemetry "
                f"(session-manifest sync_tier: ephemeral, never pulled, recovery_action "
                f"clear), so on any box but the filer's it does not exist locally and a "
                f"missing file is NOT evidence of a missing record — read it out of the "
                f"store with `backend-cat.sh cat` against that same relative path."
            ),
            "priority": "HIGH",
            "participants": ["agent"],
            "category": "framework-maintenance",
            # origin-signal-gate: the obligation-audit log IS the signal.
            # Cite it so the audit trail connects goal → triggering record.
            "origin_signal": f"investigate:obligation-audit-{session_false_claims}-false-claims",
        }
        subprocess.run(
            bash_cmd(SCRIPT_DIR / "aspirations-add-goal.sh",
                     "--source", target_source, target_id),
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
    # Tally WHY, not just how many — the breakdown is what the filed goal carries
    # so a reader on another box can act without the log (). Records
    # written before failure_reason existed tally as "unrecorded" rather than
    # silently as zero.
    false_reasons: Counter[str] = Counter()
    false_phases: Counter[str] = Counter()
    if audit_log.exists():
        try:
            for ln in audit_log.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                rec = json.loads(ln)
                if rec.get("valid") is False:
                    session_false_claims += 1
                    false_reasons[rec.get("failure_reason") or "unrecorded"] += 1
                    false_phases[_normalize_phase(rec.get("phase") or "", schema)] += 1
        except Exception as e:
            _warn(f"audit log re-read failed: {e}")

    new_false_this_iter = 0
    for phase, cond in claims:
        valid, failure_reason = _validate_claim(phase, cond, budget_zone, outcome_class, schema)
        if not valid:
            false_reasons[failure_reason or "unrecorded"] += 1
            false_phases[_normalize_phase(phase, schema)] += 1
        record = {
            "timestamp": now_iso,
            "goal_id": goal_id,
            "phase": phase,
            "condition_claimed": cond,
            "runtime_zone": budget_zone,
            "runtime_outcome_class": outcome_class,
            "valid": valid,
            "failure_reason": failure_reason,
        }
        _append_jsonl(audit_log, record)
        if not valid:
            new_false_this_iter += 1
            try:
                subprocess.run(
                    bash_cmd(SCRIPT_DIR / "wm-append.sh", "sensory_buffer"),
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
            _file_investigate_goal(session_false_claims, audit_log,
                                   false_reasons, false_phases)

    if new_false_this_iter:
        print(f"[obligation-audit] iter: {new_false_this_iter} false / {len(claims)} claim(s); "
              f"session cumulative false: {session_false_claims}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
