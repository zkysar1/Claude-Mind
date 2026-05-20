"""test_origin_signal_gate_layer_d.py — regression test for .

Asserts the Layer-D auto-derive path of origin-signal-gate.py:
  - Title prefixes Investigate/Maintain/Apply/Idea/Unblock + missing
    origin_signal auto-derive to <kind>:<slug> (or decomposition:<slug>
    for Apply, since "apply:" is intentionally NOT a top-level kind).
  - Non-prefixed titles still refuse without override.
  - Existing valid origin_signal path unchanged (no interference).
  - Override path unchanged (when both auto-derive and override are
    available, override takes precedence — caller intent is explicit).
  - Empty/slug-less title body refuses (auto-derive returns None → block).

Pattern: subprocess invocation of origin-signal-gate.py with stdin JSON,
matching the canonical caller shape from aspirations.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
GATE_PY = CORE_SCRIPTS / "origin-signal-gate.py"


def _run_gate(payload: dict, override: str | None = None) -> tuple[int, dict]:
    """Invoke origin-signal-gate.py via subprocess. Returns (rc, parsed_json)."""
    cmd = [sys.executable, str(GATE_PY), "--output", "json"]
    if override:
        cmd += ["--override-signal", override]
    proc = subprocess.run(
        cmd, input=json.dumps(payload), capture_output=True,
        text=True, timeout=15,
    )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out = {"_raw_stdout": proc.stdout, "_raw_stderr": proc.stderr}
    return proc.returncode, out


def main() -> int:
    failures: list[str] = []
    cases_run = 0

    # ── 5 prefix cases — auto-derive should fire ──────────────────────────
    prefix_cases = [
        ("Investigate:", "Investigate: capability-routing layered defense audit",
         "investigate:capability-routing-layered-defense-audit"),
        ("Maintain:", "Maintain: in-flight framework correction for stale checkpoint",
         "maintain:in-flight-framework-correction-for-stale-checkpoint"),
        # Apply title routes via decomposition: per ALLOWED_PREFIXES (apply: is NOT a kind).
        ("Apply:", "Apply: add Layer-D auto-derive to origin-signal-gate",
         "decomposition:add-layer-d-auto-derive-to-origin-signal-gate"),
        ("Idea:", "Idea: cross-pollination across recurring goal outputs",
         "idea:cross-pollination-across-recurring-goal-outputs"),
        ("Unblock:", "Unblock: roblox-studio probe parser regression on multi-env",
         "unblock:roblox-studio-probe-parser-regression-on-multi-env"),
    ]
    for prefix, title, expected_signal in prefix_cases:
        cases_run += 1
        rc, d = _run_gate({"title": title, "origin_signal": None, "source": "agent"})
        label = prefix.rstrip(":")
        if rc != 0:
            failures.append(
                f"{label}: expected rc=0 (auto-derive), got rc={rc} "
                f"reason={d.get('reason')!r}"
            )
        if d.get("would_block") is not False:
            failures.append(
                f"{label}: expected would_block=False, got {d.get('would_block')!r}"
            )
        if d.get("auto_derived") is not True:
            failures.append(
                f"{label}: expected auto_derived=True, got {d.get('auto_derived')!r}"
            )
        if d.get("origin_signal") != expected_signal:
            failures.append(
                f"{label}: expected origin_signal={expected_signal!r}, "
                f"got {d.get('origin_signal')!r}"
            )
        print(f"  [{'PASS' if d.get('origin_signal') == expected_signal else 'FAIL'}] "
              f"{label}: rc={rc} signal={d.get('origin_signal')!r}")

    # ── Negative case: non-prefixed title still refuses ──────────────────
    cases_run += 1
    rc, d = _run_gate({
        "title": "Random title without any cognitive primitive prefix",
        "origin_signal": None,
        "source": "agent",
    })
    if rc != 1:
        failures.append(f"non-prefixed: expected rc=1 (block), got rc={rc}")
    if d.get("would_block") is not True:
        failures.append(
            f"non-prefixed: expected would_block=True, got {d.get('would_block')!r}"
        )
    if d.get("auto_derived"):
        failures.append(
            f"non-prefixed: expected no auto_derived, got auto_derived={d.get('auto_derived')!r}"
        )
    print(f"  [{'PASS' if d.get('would_block') and rc == 1 else 'FAIL'}] "
          f"non-prefixed: rc={rc} would_block={d.get('would_block')!r}")

    # ── Regression: existing valid origin_signal path ────────────────────
    cases_run += 1
    rc, d = _run_gate({
        "title": "Investigate: existing valid signal should not auto-derive",
        "origin_signal": "investigate:already-set",
        "source": "agent",
    })
    if rc != 0:
        failures.append(f"valid-signal: expected rc=0, got rc={rc}")
    if d.get("auto_derived"):
        failures.append(
            "valid-signal: auto_derived should NOT fire when origin_signal is already valid"
        )
    if d.get("origin_signal") != "investigate:already-set":
        failures.append(
            f"valid-signal: signal should be unchanged, got {d.get('origin_signal')!r}"
        )
    print(f"  [{'PASS' if not d.get('auto_derived') and rc == 0 else 'FAIL'}] "
          f"valid-signal: rc={rc} auto_derived={d.get('auto_derived')!r}")

    # ── Regression: override path takes precedence over auto-derive ─────
    cases_run += 1
    rc, d = _run_gate(
        {
            "title": "Investigate: with override should use override not auto-derive",
            "origin_signal": None,
            "source": "agent",
        },
        override="explicit caller justification",
    )
    if rc != 0:
        failures.append(f"override-precedence: expected rc=0, got rc={rc}")
    if d.get("auto_derived"):
        failures.append(
            "override-precedence: auto_derived must NOT fire when --override-signal is supplied"
        )
    if d.get("override_applied") != "explicit caller justification":
        failures.append(
            f"override-precedence: expected override_applied=<justification>, "
            f"got {d.get('override_applied')!r}"
        )
    print(f"  [{'PASS' if not d.get('auto_derived') and d.get('override_applied') else 'FAIL'}] "
          f"override-precedence: rc={rc}")

    # ── Edge case: prefix-only (empty body) refuses ──────────────────────
    cases_run += 1
    rc, d = _run_gate({
        "title": "Investigate:",
        "origin_signal": None,
        "source": "agent",
    })
    if rc != 1:
        failures.append(
            f"empty-body: expected rc=1 (block — slug empty), got rc={rc}"
        )
    if d.get("auto_derived"):
        failures.append(
            "empty-body: auto_derived must NOT fire when slug body is empty"
        )
    print(f"  [{'PASS' if rc == 1 and not d.get('auto_derived') else 'FAIL'}] "
          f"empty-body: rc={rc}")

    # ── Batch mode: mixed prefixes all auto-derive ───────────────────────
    cases_run += 1
    rc, d = _run_gate({
        "source": "agent",
        "batch": [
            {"id": "g-001-01", "title": "Investigate: A", "origin_signal": None},
            {"id": "g-001-02", "title": "Maintain: B", "origin_signal": None},
            {"id": "g-001-03", "title": "Apply: C", "origin_signal": None},
            {"id": "g-001-04", "title": "Idea: D", "origin_signal": None},
            {"id": "g-001-05", "title": "Unblock: E", "origin_signal": None},
        ],
    })
    if rc != 0:
        failures.append(f"batch-all-auto: expected rc=0, got rc={rc}")
    if d.get("any_blocked"):
        failures.append(
            f"batch-all-auto: expected any_blocked=False, got {d.get('any_blocked')!r}"
        )
    expected_signals = [
        "investigate:a", "maintain:b", "decomposition:c", "idea:d", "unblock:e",
    ]
    actual_signals = [r.get("origin_signal") for r in d.get("results", [])]
    if actual_signals != expected_signals:
        failures.append(
            f"batch-all-auto: expected {expected_signals!r}, got {actual_signals!r}"
        )
    print(f"  [{'PASS' if actual_signals == expected_signals else 'FAIL'}] "
          f"batch-all-auto: signals={actual_signals!r}")

    # ── Summary ──────────────────────────────────────────────────────────
    print()
    print(f"Cases run: {cases_run}")
    if failures:
        print(f"FAIL: {len(failures)} failures")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All cases PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
