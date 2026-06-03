#!/usr/bin/env python3
# domain-leak-exempt: scanner enumerates timestamp regexes + test-fixture field names by design
"""Time-bomb fixture scanner (guard-566 enforcement, Layer C detective).

A "time-bomb fixture" is a test fixture with a hardcoded absolute ISO
timestamp fed into PRODUCTION code that computes a now()-relative staleness
window (streak/TTL reset, overdue branch, "_since" age check). It passes on
the day it is authored, then silently flips to the overdue/reset path as
wall-clock advances past the production boundary -- a deterministic failure
hours-to-days after authoring, with NO code change. Canonical incident:
g-115-1141 (test_insight_trigger_gate_reprobe.py hardcoded 2026-05-21,
aged out of a 24h scan window, 4 failures). guard-566 mandates the
now-relative idiom; this scanner is the detective half that surfaces drift.

Why advisory, not a fail-loud gate: the pattern is NOT reliably grep-detectable.
Legitimate inert metadata (aspiration `created`), fixed-vs-fixed round-trip
assertions (cmd_reset preservation), and structural field checks all carry
hardcoded ISO literals. A hard PASS/FAIL gate would either force exemption
comments onto dozens of clean fixtures (alarm fatigue) or be ignored. So the
default posture is REPORT (exit 0). Enforcement is opt-in:
  - `--diff` scopes to git-diff-added lines (catches a NEW bomb at authoring
    time without tripping on legacy-safe literals) -- the precise mode.
  - `--exit-on-hits` returns exit 1 when suspects remain (for pre-commit/CI).

guard-566's own exception clause IS the exemption contract here. A hardcoded
literal is exempt when ANY of:
  1. the same line uses the now-relative idiom (timedelta / datetime.now /
     utcnow / .now( / isoformat / time.time / os.utime) -- it is computed,
     not anchored;
  2. the date is far-past (older than --recency-days, default 30) -- guard-566
     explicitly permits a fixed PAST date for the overdue/far-past branch, and
     an old literal that still passes is proven-stable (it would already have
     detonated under any window shorter than its age);
  3. an explicit `# timebomb-safe: <reason>` marker is on the literal's line
     or the line immediately above (author declared it deliberate).

Remediation printed for each suspect: convert to the now-relative idiom
(mirror `_trigger_timestamp` in test_insight_trigger_sweep_reprobe.py:
  (datetime.now() - timedelta(hours=H)).isoformat(timespec="seconds")
), OR add `# timebomb-safe: <why this literal is not a now()-relative input>`.

Usage:
  py -3 core/scripts/timebomb-fixture-scan.py            # full scan, report, exit 0
  py -3 core/scripts/timebomb-fixture-scan.py --diff     # only git-diff-added lines
  py -3 core/scripts/timebomb-fixture-scan.py --exit-on-hits   # exit 1 if suspects
  py -3 core/scripts/timebomb-fixture-scan.py --json     # machine-readable
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCAN_ROOT = REPO_ROOT / "core" / "scripts" / "tests"

# A full ISO datetime literal inside quotes (date-only literals like
# "2026-05-08" without a T are far weaker signals -- many are display dates,
# not clock inputs -- so this scanner targets the date+time shape that the
# canonical incidents used).
ISO_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::\d{2})?')

# Now-relative idiom tokens. Presence on the SAME line means the literal is
# being derived/compared against a computed value, not anchored absolutely.
NOW_IDIOM_RE = re.compile(
    r'timedelta|datetime\.now|datetime\.utcnow|\.now\(|utcnow\(|'
    r'isoformat|time\.time\(|os\.utime'
)

MARKER = "timebomb-safe:"

REMEDIATION = (
    "convert to now-relative idiom "
    "((datetime.now() - timedelta(...)).isoformat(timespec='seconds'); see "
    "test_insight_trigger_sweep_reprobe.py::_trigger_timestamp) "
    "OR add a '# timebomb-safe: <reason>' marker on the literal's line or "
    "the line above (guard-566 far-past/metadata/fixed-vs-fixed exception)."
)


def _parse_iso(m: re.Match) -> _dt.datetime | None:
    try:
        y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
        return _dt.datetime(y, mo, d, h, mi)
    except ValueError:
        return None


def _diff_added_lines() -> dict[Path, set[int]]:
    """Map of file -> set of 1-based line numbers added in the working diff.

    Uses `git diff -U0 HEAD` so only added lines under SCAN_ROOT are scoped.
    Fail-open: any git error returns an empty map (scanner then reports nothing
    in --diff mode rather than crashing the loop).
    """
    out: dict[Path, set[int]] = {}
    try:
        raw = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "diff", "-U0", "HEAD", "--", str(SCAN_ROOT)],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return out
    cur: Path | None = None
    new_ln = 0
    hunk_re = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')
    for line in raw.splitlines():
        if line.startswith("+++ b/"):
            cur = REPO_ROOT / line[6:]
            continue
        hm = hunk_re.match(line)
        if hm:
            new_ln = int(hm.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if cur is not None:
                out.setdefault(cur, set()).add(new_ln)
            new_ln += 1
        elif not line.startswith("-"):
            new_ln += 1
    return out


def scan(recency_days: int, diff_only: bool) -> list[dict]:
    now = _dt.datetime.now()
    cutoff = now - _dt.timedelta(days=recency_days)
    suspects: list[dict] = []
    diff_map = _diff_added_lines() if diff_only else None

    files = sorted(SCAN_ROOT.glob("test_*.py")) + sorted(SCAN_ROOT.glob("test-*.sh"))
    for fp in files:
        if diff_map is not None and fp not in diff_map:
            continue
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for idx, line in enumerate(lines):  # idx is 0-based
            lineno = idx + 1
            if diff_map is not None and lineno not in diff_map.get(fp, set()):
                continue
            m = ISO_RE.search(line)
            if not m:
                continue
            # Exemption 1: same-line now-relative idiom.
            if NOW_IDIOM_RE.search(line):
                continue
            # Exemption 3: explicit marker on this line or the line above.
            prev = lines[idx - 1] if idx > 0 else ""
            if MARKER in line or MARKER in prev:
                continue
            dt = _parse_iso(m)
            if dt is None:
                continue
            # Exemption 2: far-past literal (guard-566 overdue/far-past branch).
            if dt < cutoff:
                continue
            suspects.append({
                "file": str(fp.relative_to(REPO_ROOT)).replace("\\", "/"),
                "line": lineno,
                "literal": m.group(0),
                "age_days": round((now - dt).total_seconds() / 86400.0, 1),
                "snippet": line.strip()[:100],
            })
    return suspects


def main() -> int:
    ap = argparse.ArgumentParser(description="guard-566 time-bomb fixture scanner (advisory).")
    ap.add_argument("--all", action="store_true", help="full scan (default mode; explicit flag for clarity)")
    ap.add_argument("--diff", action="store_true", help="scope to git-diff-added lines only (authoring-time precision)")
    ap.add_argument("--exit-on-hits", action="store_true", help="return exit 1 when suspects remain (pre-commit/CI use)")
    ap.add_argument("--recency-days", type=int, default=30, help="literals older than this are far-past-exempt (default 30)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    suspects = scan(recency_days=args.recency_days, diff_only=args.diff)

    if args.json:
        print(json.dumps({
            "scanned_root": str(SCAN_ROOT.relative_to(REPO_ROOT)).replace("\\", "/"),
            "mode": "diff" if args.diff else "all",
            "recency_days": args.recency_days,
            "suspect_count": len(suspects),
            "suspects": suspects,
        }, indent=2))
    else:
        mode = "diff-added" if args.diff else "full"
        if not suspects:
            print(f"[timebomb-scan] PASS ({mode}) -- 0 unmarked recent hardcoded-timestamp fixtures (guard-566 clean).")
        else:
            print(f"[timebomb-scan] {len(suspects)} suspect(s) ({mode}) -- triage against guard-566:")
            for s in suspects:
                print(f"  {s['file']}:{s['line']}  {s['literal']}  (age {s['age_days']}d)")
                print(f"      {s['snippet']}")
            print(f"  Remediation: {REMEDIATION}")

    if args.exit_on_hits and suspects:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
