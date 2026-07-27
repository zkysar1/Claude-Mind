"""test_check_stderr_json_merge.py —  / guard-659 detector regression test.

Pins the behavior of core/scripts/check-stderr-json-merge.py, the detective-layer
scanner for the guard-659 regression class: a shell command captured with `2>&1`
whose output is fed to python json.loads/json.load silently zeroes out on ANY
stderr line (parse fails -> except -> false-empty result; canonical
infra-streak-notify.sh:58, alert_count -> 0, detected g-249-16, fixed g-249-18).

The detector must:
  - FLAG the bug shape (2>&1 capture piped to json.loads, no hardening).
  - EXEMPT the three valid mitigations:
      (b) raw_decode + residual-split — g-115-769 (aspirations-update-goal.sh).
      (c) per-line extraction (`for line in` + a `startswith` prefix filter)
          — guard-559 (iteration-close.sh).
  - leave mitigation (a) — remove `2>&1` — neither flagged nor exempt-listed,
    because the var never enters the 2>&1-capture set in the first place.
  - stay clean on a file with no captures at all.

A live-tree assertion mirrors the verify-learning Section SMJ check: the real
core/scripts/*.sh must currently scan clean (exit 0).

Refs: guard-659 (regression class), g-249-16 (detect) / g-249-18 (fix instance 2),
g-115-769 (raw_decode mitigation), guard-559 (per-line mitigation), g-115-1265
(this detector + verify-learning Section SMJ check).
"""
from __future__ import annotations

import glob
import importlib.util
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
DETECTOR_PATH = CORE_SCRIPTS / "check-stderr-json-merge.py"

# The detector filename is hyphenated (not a valid module name) — load by path.
# __name__ becomes "check_stderr_json_merge", so the script's
# `if __name__ == "__main__"` block does NOT execute on import (no side effects).
_spec = importlib.util.spec_from_file_location("check_stderr_json_merge", DETECTOR_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _scan_text(tmp_path, name, text):
    """Write `text` to a temp .sh file and return (flagged, exempt, scanned)."""
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return _mod.scan([str(p)])


# --- the bug shape: 2>&1 capture piped straight into json.loads, no hardening ---
BUG = """#!/usr/bin/env bash
RAW=$(some-cmd emit-json 2>&1 || true)
RESULT=$(echo "$RAW" | python3 -c "import sys, json; print(json.loads(sys.stdin.read()))")
echo "$RESULT"
"""

# --- mitigation (b): raw_decode + residual-split () — exempt ---
# Realistic shape: raw_decode is the primary parse; the residual segment is
# parsed with json.loads, so the window contains BOTH raw_decode and json.loads.
RAW_DECODE = """#!/usr/bin/env bash
COMBINED="$(rt_call something 2>&1)"
echo "$COMBINED" | python3 -c "
import sys, json
raw = sys.stdin.read()
dec = json.JSONDecoder()
obj, idx = dec.raw_decode(raw)
residual = raw[idx:].strip()
if residual:
    extra = json.loads(residual)
print(obj)
"
"""

# --- mitigation (c): per-line extraction with startswith filter (guard-559) — exempt ---
LINE_EXTRACT = """#!/usr/bin/env bash
OUT="$(commit_thing 2>&1)"
echo "$OUT" | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line.startswith('{'):
        continue
    d = json.loads(line)
    print(d)
"
"""

# --- mitigation (a): no 2>&1 in the capture — var never enters the flag set ---
NO_MERGE = """#!/usr/bin/env bash
CLEAN=$(some-cmd emit-json || true)
echo "$CLEAN" | python3 -c "import sys, json; print(json.loads(sys.stdin.read()))"
"""

# --- clean: no command substitution captures at all ---
CLEAN = """#!/usr/bin/env bash
echo hello
ls -la
"""


def test_bug_shape_flagged(tmp_path) -> None:
    flagged, exempt, scanned = _scan_text(tmp_path, "bug.sh", BUG)
    assert scanned == 1
    assert len(flagged) == 1, f"expected the bug flagged; flagged={flagged} exempt={exempt}"
    assert flagged[0]["var"] == "RAW"
    assert exempt == [], f"bug shape must not be exempt-listed: {exempt}"


def test_raw_decode_exempt(tmp_path) -> None:
    flagged, exempt, scanned = _scan_text(tmp_path, "rawdecode.sh", RAW_DECODE)
    assert flagged == [], f"raw_decode site wrongly flagged: {flagged}"
    assert len(exempt) == 1, f"expected one exempt entry; exempt={exempt}"
    assert "raw_decode" in exempt[0]["reason"]


def test_line_extraction_exempt(tmp_path) -> None:
    flagged, exempt, scanned = _scan_text(tmp_path, "lineextract.sh", LINE_EXTRACT)
    assert flagged == [], f"per-line extraction site wrongly flagged: {flagged}"
    assert len(exempt) == 1, f"expected one exempt entry; exempt={exempt}"
    assert "per-line" in exempt[0]["reason"]


def test_no_merge_not_flagged(tmp_path) -> None:
    """No `2>&1` in the capture -> the var never enters the 2>&1 set, so the
    json.loads consumer is never examined. Neither flagged nor exempt."""
    flagged, exempt, scanned = _scan_text(tmp_path, "nomerge.sh", NO_MERGE)
    assert flagged == [], f"no-2>&1 capture wrongly flagged: {flagged}"
    assert exempt == [], f"no-2>&1 capture should not be exempt-listed either: {exempt}"


def test_clean_file(tmp_path) -> None:
    flagged, exempt, scanned = _scan_text(tmp_path, "clean.sh", CLEAN)
    assert flagged == []
    assert exempt == []
    assert scanned == 1


def test_repo_core_scripts_clean() -> None:
    """The live core/scripts/*.sh tree must currently scan clean (exit 0) —
    the same invariant the verify-learning Section SMJ check enforces."""
    paths = sorted(glob.glob(str(CORE_SCRIPTS / "*.sh")))
    flagged, exempt, scanned = _mod.scan(paths)
    assert flagged == [], (
        "live core/scripts has unhardened 2>&1->json.loads site(s): "
        f"{flagged} — fix via remove-2>&1 / raw_decode (g-115-769) / "
        "per-line extraction (guard-559)"
    )
    assert scanned > 0, "expected to scan at least one core/scripts/*.sh"


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tp = Path(td)
        test_bug_shape_flagged(tp)
        test_raw_decode_exempt(tp)
        test_line_extraction_exempt(tp)
        test_no_merge_not_flagged(tp)
        test_clean_file(tp)
    test_repo_core_scripts_clean()
    print("PASS: check-stderr-json-merge detector behavior pinned "
          "(5 synthetic paths + live-tree clean)")
