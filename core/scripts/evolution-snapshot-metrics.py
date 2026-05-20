#!/usr/bin/env python3
"""Evolution snapshot-metrics — INTENTIONAL UNIMPLEMENTED PLACEHOLDER.

Finding F3 (2026-05-15): this script is invoked by two consumers —
`evolution-complete.py` (baseline vector at edit-completion time) and
`meta-backpressure.py::_sample_vector` (current vector for regression
comparison) — but it was NEVER created. Consequence: every material
self/program/skill/rule evolution edit since inception tried to register a
backpressure monitor, the subprocess failed with a raw `can't open file`
error, and monitor registration was silently skipped. The entire Phase-4
self/program/skill/rule auto-rollback safety net has therefore been DEAD
since day one.

Why this is a placeholder and not a real implementation:

`core/config/meta.yaml::self_program_backpressure` specifies a COUNT of
signals per monitor kind (self=15, program=21, skill=10, rule=9) and a
count-sensitive combinator (`majority_count_self: 8 of 15`, etc.). The
signal *names and computations* are specified NOWHERE — not in meta.yaml,
not in code, not in the spec (`world/conventions/self-program-evolution.md`
§"Backpressure Auto-Rollback" lists counts, not definitions). Emitting a
fabricated or partial signal vector into a count-sensitive auto-rollback
combinator is strictly MORE dangerous than a cleanly-disabled monitor: a
wrong vector would auto-`history.py restore` good self/program edits. So
this placeholder fails LOUDLY-BUT-CLEANLY (distinct sentinel exit code +
one structured line) instead of fabricating a contract that was never
specified.

Proper implementation is filed as a scoped design goal (see the Investigate
goal referenced in the F2/F3 commit and the coordination-board findings
post). Until then, callers detect SENTINEL_RC and treat the monitor as
intentionally disabled (one INFO line), not as a runtime error.

Contract when implemented: read `--file-kind {agent_self|program|skill_edit|
rule_edit}` (+ optional `--file-path`), print a JSON dict of
`{signal_name: float in 0..1}` to stdout with EXACTLY the meta.yaml
`signal_count` for that kind, deterministic, regression-meaningful, exit 0.
"""
import argparse
import sys

# Distinct, documented sentinel. Chosen out of the way of the callers'
# meaningful codes (0 ok / 1 generic / 2 usage / 3 not-found). Callers
# special-case this to emit ONE clean "monitor intentionally disabled"
# line instead of a scary WARN stack.
SENTINEL_RC = 64


def main():
    ap = argparse.ArgumentParser(
        description="Evolution snapshot-metrics (UNIMPLEMENTED placeholder — "
                    "see module docstring / F3 design goal)."
    )
    ap.add_argument("--file-kind", default="")
    ap.add_argument("--file-path", default="")
    ap.parse_args()  # accept caller args so this fails intentionally, not on argparse

    sys.stderr.write(
        "evolution-snapshot-metrics: INTENTIONALLY UNIMPLEMENTED "
        f"(sentinel rc={SENTINEL_RC}). The backpressure signal registry "
        "(meta.yaml self_program_backpressure: 15/21/10/9 signals) was never "
        "specified; fabricating it would feed a count-sensitive auto-rollback "
        "combinator and could revert good edits. Evolution backpressure "
        "monitoring is DISABLED by design until the signal registry is "
        "specced + built. This is a known state, not a runtime error.\n"
    )
    return SENTINEL_RC


if __name__ == "__main__":
    sys.exit(main())
