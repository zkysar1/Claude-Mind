#!/usr/bin/env python3
"""Detects drift between verify-learning SKILL.md citations and live records.

For each Bash check line in `.claude/skills/verify-learning/SKILL.md` that
cites a reasoning-bank or guardrail record by id AND asserts a field value
on it, this script reads the live record and confirms the assertion still
holds. Reports any (record_id, asserted_field, asserted_value, line_no)
tuple where the live field has drifted.

Parsed assertion forms:
  grep -q '"<field>": "<value>"'
  grep -E '"<field>": "<value>"'
  d['<field>'] == '<value>'
  d['<field>'] in ('<v1>', '<v2>', ...)

All other check forms (content-substring greps, custom python, tags
membership) are skipped — this script only validates field=value
assertions where the field belongs to the record's top-level schema.

Origin: g-115-1140 (sq-018 spark from g-115-998 — two undetected drifts in
verify-learning's own assertions: rb-435 applies_to drift + guard-006
status drift from active to retired). rb-1191 documents the probe-each-
cited-record discipline this script automates.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SKILL_PATH = PROJECT_ROOT / ".claude" / "skills" / "verify-learning" / "SKILL.md"

sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))
import _paths  # noqa: E402
import _verify_corpus  # noqa: E402

RB_JSONL = Path(_paths.WORLD_DIR) / "reasoning-bank.jsonl"
GUARD_JSONL = Path(_paths.WORLD_DIR) / "guardrails.jsonl"

RECORD_ID_RX = re.compile(r"\b(rb-\d+|guard-\d+)\b")
GREP_FIELD_VALUE_RX = re.compile(
    r"""grep\s+(?:-q|-E)\s+(?:['"])\\?"(?P<field>[a-z_]+)\\?":\s*\\?"(?P<value>[a-zA-Z0-9_\-]+)\\?"(?:['"])"""
)
ASSERT_EQ_RX = re.compile(
    r"""d\[['"](?P<field>[a-z_]+)['"]\]\s*==\s*['"](?P<value>[a-zA-Z0-9_\-]+)['"]"""
)
ASSERT_IN_RX = re.compile(
    r"""d\[['"](?P<field>[a-z_]+)['"]\]\s+in\s+\(\s*(?P<values>(?:['"][a-zA-Z0-9_\-]+['"]\s*,?\s*)+)\s*\)"""
)

RB_INDEX: dict[str, dict] | None = None
GUARD_INDEX: dict[str, dict] | None = None


def _load_jsonl_index(path: Path) -> dict[str, dict]:
    index: dict[str, dict] = {}
    if not path.exists():
        return index
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec_id = rec.get("id")
            if rec_id:
                index[rec_id] = rec
    return index


def read_record(rec_id: str) -> dict | None:
    global RB_INDEX, GUARD_INDEX
    if rec_id.startswith("rb-"):
        if RB_INDEX is None:
            RB_INDEX = _load_jsonl_index(RB_JSONL)
        return RB_INDEX.get(rec_id)
    if rec_id.startswith("guard-"):
        if GUARD_INDEX is None:
            GUARD_INDEX = _load_jsonl_index(GUARD_JSONL)
        return GUARD_INDEX.get(rec_id)
    return None


def extract_assertions(line: str) -> list[tuple[str, list[str]]]:
    """Return list of (field, [allowed_values]) tuples found on this line."""
    assertions: list[tuple[str, list[str]]] = []
    for m in GREP_FIELD_VALUE_RX.finditer(line):
        assertions.append((m.group("field"), [m.group("value")]))
    for m in ASSERT_EQ_RX.finditer(line):
        assertions.append((m.group("field"), [m.group("value")]))
    for m in ASSERT_IN_RX.finditer(line):
        values = re.findall(r"['\"]([a-zA-Z0-9_\-]+)['\"]", m.group("values"))
        assertions.append((m.group("field"), values))
    return assertions


def line_carries_record_lookup(line: str) -> bool:
    return "reasoning-bank-read.sh" in line or "guardrails-read.sh" in line


def main() -> int:
    if not SKILL_PATH.exists():
        print(f"FAIL: SKILL.md not found at {SKILL_PATH}", file=sys.stderr)
        return 1

    drift_cases: list[dict] = []
    missing_records: list[dict] = []
    checked = 0

    # Corpus, not the file. The checks moved to a registry on 2026-08-18
    # (); reading the thin SKILL.md took this gate from correctly
    # FAILING on 5 missing records to vacuously PASSING on 0 checked — a gate
    # that cannot fail is worse than an absent one. The corpus is
    # byte-identical to the pre-cutover file, so line_no still addresses the
    # same line it did before.
    for line_no, line in enumerate(_verify_corpus.corpus_lines(), start=1):
        if not line_carries_record_lookup(line):
            continue
        ids = list({m.group(0) for m in RECORD_ID_RX.finditer(line)})
        if not ids:
            continue
        assertions = extract_assertions(line)
        if not assertions:
            continue
        for rec_id in ids:
            record = read_record(rec_id)
            if record is None:
                missing_records.append(
                    {"line": line_no, "id": rec_id, "snippet": line.strip()[:160]}
                )
                continue
            for field, allowed in assertions:
                if field not in record:
                    drift_cases.append(
                        {
                            "line": line_no,
                            "id": rec_id,
                            "field": field,
                            "expected": allowed,
                            "live": "<field-absent>",
                            "snippet": line.strip()[:160],
                        }
                    )
                    continue
                live_value = record[field]
                if isinstance(live_value, (list, dict)):
                    continue
                live_str = str(live_value)
                if live_str not in allowed:
                    drift_cases.append(
                        {
                            "line": line_no,
                            "id": rec_id,
                            "field": field,
                            "expected": allowed,
                            "live": live_str,
                            "snippet": line.strip()[:160],
                        }
                    )
                checked += 1

    if drift_cases or missing_records:
        print(
            f"FAIL: citation drift detected — {len(drift_cases)} drift, "
            f"{len(missing_records)} missing-record (of {checked} field-assertions checked)"
        )
        for case in drift_cases:
            print(
                f"  DRIFT line {case['line']}: {case['id']} field={case['field']} "
                f"expected={case['expected']} live={case['live']!r}"
            )
            print(f"    snippet: {case['snippet']}")
        for case in missing_records:
            print(f"  MISSING line {case['line']}: {case['id']} (read returned no record)")
            print(f"    snippet: {case['snippet']}")
        return 1

    print(f"PASS: {checked} field-assertion(s) checked across verify-learning SKILL.md; all consistent with live records.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
