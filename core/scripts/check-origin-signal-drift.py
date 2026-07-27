#!/usr/bin/env python3
"""check-origin-signal-drift.py — Layer B/D guard for the prescribed-vs-allowed
origin_signal drift class (g-115-3096).

THE DRIFT CLASS. Skill pseudocode prescribes a literal origin_signal for goals
it tells the agent to file (`"origin_signal": "some-prefix:<slug>"`). The gate
that admits those goals reads a separate tuple — `ALLOWED_PREFIXES` in
`gates/origin_signal.py`. Nothing kept the two in sync, so a prefix could be
prescribed by a skill and rejected by the gate at the same time. THREE failure shapes result, and only ONE of them is loud — which is why the
class accrues silently and why a standing checker is needed rather than
trusting the gate to surface it:

  GOAL-level, auto-derivable title — SILENT (and the COMMON case). When the
              title starts with a cognitive-primitive prefix
              (`Investigate:` / `Maintain:` / `Idea:` / `Unblock:` / `Apply:`),
              Layer-D auto-derive (gates/origin_signal.py `try_auto_derive`)
              REWRITES the invalid signal from the title slug and returns a
              NON-BLOCKING pass (decision_path="auto_derive"). Nothing refuses,
              nothing warns. Since cognitive-primitive goals are titled exactly
              that way, most goal-level violations take this path. Observed:
              a goal filed with `sq-013:<slug>` was stored as
              `investigate:<title-slug>` — the prescribed provenance key
              silently replaced.

  GOAL-level, non-derivable title — LOUD. No auto-derive prefix matches, so
              origin-signal-gate REFUSES the add. The caller improvises or
              reaches for --override-signal, inflating the gate's override rate
              (the metric gate-retirement-eval reads). This is the ONLY shape
              that announces itself, and it is how `skill-discovery-audit:`
              was noticed.

  ASPIRATION-level — SILENT. NOT gate-checked at add time, so the non-allowed
              value is simply written. create-aspiration/SKILL.md names the
              rule exactly ("Aspiration-level values aren't gate-checked at add
              time ... do not invent new prefixes without extending
              ALLOWED_PREFIXES first") but nothing enforced it.
              `blocker_pattern:` survived here, unnoticed.

Common cost across all three: the PRESCRIBED key never lands, so any later
exact-match dedup on that key is vacuous and the emitting ritual re-files a
duplicate every cadence (the g-115-2196 class).

Prior instances (both fixed by widening the gate, not by rewriting callers):
g-115-1100 (4 automated-filer prefixes), g-115-1439 (7 infer-parity prefixes).
This script is the detective layer that makes a fourth instance impossible to
accrue silently.

RELATIONSHIP TO test_goal_source_infer_parity.py — the two are complementary
halves and neither subsumes the other:

    infer()  ──parity test──▶  ALLOWED_PREFIXES  ◀──this script──  SKILL.md
    (code says it's a source)                        (docs say to emit it)

The parity test pins code→gate. This pins docs→gate. A prefix prescribed only
in prose is invisible to the parity test, which is precisely how
`skill-discovery-audit:` and `blocker_pattern:` both survived.

USAGE
    python3 core/scripts/check-origin-signal-drift.py [--json] [--root DIR]

    exit 0  no drift
    exit 1  drift — every mismatch printed as file:line -> value on stderr

Placeholders (`<origin_signal>`, `{new_goal.type}:{goal.id}`) are templated at
runtime, carry no literal prefix to check, and are reported separately for
visibility without failing the run.

KNOWN COVERAGE LIMIT (measured, not assumed). Only QUOTED values are matched —
`origin_signal: idea:foo` without quotes is invisible to this scan. Measured
2026-07-25: the corpus contains exactly 2 unquoted occurrences, both prose
inside backticks in coordination.md naming the already-registered
`alert-email:` prefix, so the live false-negative count is ZERO. The regex was
deliberately NOT widened: every unquoted form found is prose, so matching it
would add false positives without removing a real miss. If a future prescribed
literal is written unquoted in an executable block, this scan will miss it —
widen the regex THEN, with a backtick-parity exclusion like the one in
check-no-bare-agent-prefix.sh, and not before.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gates.origin_signal import ALLOWED_PREFIXES  # noqa: E402

# Matches   origin_signal": "value"   /   origin_signal = "value"
# /   origin_signal: "value"   — the three shapes the corpus actually uses.
_LITERAL = re.compile(r'origin_signal["\']?\s*[:=]\s*["\']([^"\']+)["\']')

# Scanned surfaces: every skill's pseudocode plus the config-side digests and
# conventions that also prescribe goal JSON. `core/config/**/*.md` matches both
# top-level files and nested conventions/ + rationale/ dirs.
_GLOBS = (".claude/skills/*/SKILL.md", "core/config/**/*.md")


def is_placeholder(value: str) -> bool:
    """Templated value — the real prefix is substituted at runtime."""
    return value.startswith("<") or value.startswith("{")


def is_allowed(value: str) -> bool:
    return any(value.startswith(p) for p in ALLOWED_PREFIXES)


def scan(root: Path):
    """Return (mismatches, placeholders, allowed_count).

    Each mismatch/placeholder is a (relative_path, line_number, value) tuple.
    """
    mismatches, placeholders, allowed = [], [], 0
    seen = set()
    for pattern in _GLOBS:
        for path in sorted(root.glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue  # unreadable file is not drift evidence
            rel = path.relative_to(root).as_posix()
            for lineno, line in enumerate(text.splitlines(), 1):
                for m in _LITERAL.finditer(line):
                    value = m.group(1)
                    if is_placeholder(value):
                        placeholders.append((rel, lineno, value))
                    elif is_allowed(value):
                        allowed += 1
                    else:
                        mismatches.append((rel, lineno, value))
    return mismatches, placeholders, allowed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--root", default=None,
                    help="repo root to scan (default: this script's repo)")
    args = ap.parse_args()

    root = Path(args.root).resolve() if args.root \
        else Path(__file__).resolve().parents[2]

    mismatches, placeholders, allowed = scan(root)
    scanned = allowed + len(mismatches) + len(placeholders)

    if args.json:
        print(json.dumps({
            "root": str(root),
            "allowlist_entries": len(ALLOWED_PREFIXES),
            "literals_scanned": scanned,
            "allowed": allowed,
            "mismatches": [
                {"file": f, "line": n, "value": v} for f, n, v in mismatches],
            "placeholders": [
                {"file": f, "line": n, "value": v} for f, n, v in placeholders],
            "verdict": "drift" if mismatches else "clean",
        }, indent=2))
        return 1 if mismatches else 0

    print(f"[origin-signal-drift] scanned {scanned} prescribed literals "
          f"against {len(ALLOWED_PREFIXES)} allowlist entries "
          f"({allowed} allowed, {len(placeholders)} templated)")

    if placeholders:
        print("[origin-signal-drift] templated (not checkable, informational):")
        for f, n, v in placeholders:
            print(f"    {f}:{n} -> {v}")

    if not mismatches:
        print("[origin-signal-drift] CLEAN — no prescribed prefix is missing "
              "from ALLOWED_PREFIXES")
        return 0

    print(f"\nORIGIN_SIGNAL_DRIFT: {len(mismatches)} prescribed literal(s) name "
          f"a prefix absent from ALLOWED_PREFIXES", file=sys.stderr)
    for f, n, v in mismatches:
        print(f"  {f}:{n} -> {v!r}", file=sys.stderr)
    print("""
A skill prescribes this origin_signal but the gate does not admit it. At
GOAL level origin-signal-gate refuses the add (override-rate inflation, and
the prescribed key never lands so later exact-match dedup on it is vacuous);
at ASPIRATION level the value is written silently as forward-compat debt.

Fix ONE of:
  (a) Register the prefix in core/scripts/gates/origin_signal.py
      ALLOWED_PREFIXES, AND add it to the matching branch of
      _goal_source.infer() (core/scripts/_goal_source.py) so goal_source does
      not land null, AND add a sample to
      core/scripts/tests/test_goal_source_infer_parity.py.
      Prefer this when the prefix is a real discriminator its own reader
      matches on — rewriting it to a generic prefix would erase that.
  (b) Correct the pseudocode to emit an already-sanctioned prefix.
      Prefer this when a sanctioned prefix already expresses the intent.

Precedent: g-115-1100, g-115-1439, g-115-3096 all chose (a).
""", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
