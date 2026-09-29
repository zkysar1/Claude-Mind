#!/usr/bin/env python3
"""Lane-terminal gate for the /generate-domain-goals supply governor.

ONE question, ONE component: which of the boosted lanes named by
`strategic_focus.primary` still have a LIVE aspiration? The governor's
lane-floor rule (`.claude/skills/generate-domain-goals/SKILL.md` Phase 1
step 2) marks a boosted lane STARVED when its available-goal count is
under the brief's `lane_floor` — and it read ONLY the goal count. A
COMPLETED (or RETIRED) aspiration has zero non-terminal goals, so it reads
as maximally starved and contributes the single largest possible deficit,
aiming fresh supply at a lane that is DONE. Measured 2026-09-19 (g-374-60
filing): two of the six boosted lanes were completed and contributed 12 of
the 25-goal batch target — ~48% of the batch aimed at closed lanes.

WHY A SCRIPT AND NOT A SKILL.md STEP (guard-399): an "LLM must consult the
reader at step N" instruction has no baseline anyone can run. This module
is the bash/pytest baseline; the SKILL.md step is enrichment on top of it,
and `test_lane_terminal_check.py` pins the call site so the wiring cannot
rot invisibly (the outcome-observation rot lesson, domain-hooks.md).

READER: the lane's aspiration status is read through the sanctioned
wrapper `core/scripts/aspirations-read.sh --source <world|agent> --id
<asp-id>` (the same daemon-aware reader goal-selector and
iteration-close use) — never by opening the aspiration store's JSONL
directly. That wrapper's unknown-id behaviour is a loud
`{"error": "not_found", ...}` JSON with exit 1; an unreadable lane is
therefore detectable, and a detectable read failure FAILS OPEN: the lane
stays in the floor calculation with `read_error` set (guard-1084 —
silence is not an all-clear, but refusing the whole run because ONE lane's
status could not be read would turn a read problem into a supply outage).
The degraded set is surfaced in the verdict so the run can say which lanes
it trusted.

FAIL-OPEN BY CONSTRUCTION: absent/unknown lanes, non-JSON reader output,
and unexpected reader exit codes all yield `read_error` + `degraded:
true`, never a crash and never an exclusion. The ONLY path that removes a
lane from the floor calculation is a positively-read terminal status.

Terminal set: `completed`, `retired` (CLAUDE.md aspiration status
vocabulary; `paused` is a live lane with work parked on it — it is NOT
terminal, and a paused lane is exactly where a floor should reach).

CLI
    py -3 core/scripts/lane_terminal_check.py check --lanes asp-011,asp-012 [--source world|agent] [--json]

Exit codes: 0 = verdict emitted (even degraded), 2 = usage error. The
verdict JSON carries `lanes[]` (per-lane status), `in_floor` (the lanes
that still count toward the lane floor — the only set the governor may
sum over), and `excluded_terminal` / `degraded` for the audit trail.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runtime_bash import bash_cmd  # noqa: E402

READER = "core/scripts/aspirations-read.sh"

# CLAUDE.md status vocabulary for aspirations: active, completed, paused,
# retired. Only the first two halves are live lanes; a paused lane still
# holds its pending goals and its floor still means something.
TERMINAL_STATUSES = ("completed", "retired")


def shell_read_status(aspiration_id: str, source: str = "world") -> "tuple[str | None, str | None]":
    """Read one aspiration's status through the sanctioned wrapper.

    Returns `(status, read_error)` — exactly one is None. The wrapper's
    loud not_found (`{"error": ...}`, rc 1), empty output, and non-JSON
    output are all read errors, never statuses.
    """
    argv = bash_cmd(READER, "--source", source, "--id", aspiration_id)
    try:
        proc = subprocess.run(
            argv,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"reader invocation failed: {exc}"
    out = (proc.stdout or "").strip()
    if not out:
        tail = (proc.stderr or "").strip().splitlines()
        return None, f"reader rc={proc.returncode}, no JSON: {tail[-1] if tail else '(no stderr)'}"
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None, f"reader rc={proc.returncode}, non-JSON output: {out[:120]!r}"
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return None, f"reader rc={proc.returncode}, non-object JSON: {out[:120]!r}"
    if "error" in data:
        return None, f"reader error {data['error']}: {data.get('detail', '')}"
    status = data.get("status")
    if not status:
        return None, f"reader rc={proc.returncode}, record carries no status"
    return str(status), None


def classify_lanes(
    lanes,
    source: str = "world",
    reader=shell_read_status,
) -> dict:
    """Classify each boosted lane's aspiration: live, terminal, or unreadable.

    `reader` is injectable (tests monkeypatch it); production passes the
    wrapper reader above. Fail-open: only a positively-read terminal
    status removes a lane from `in_floor`.
    """
    out = []
    in_floor, excluded, degraded = [], [], []
    for lane in lanes:
        status, err = reader(lane, source)
        entry = {"lane": lane, "status": status, "read_error": err,
                 "terminal": bool(status in TERMINAL_STATUSES)}
        out.append(entry)
        if err is not None:
            # Unreadable lanes fail OPEN into the floor set (fail-open; the
            # run degrades, the governor does not).
            in_floor.append(lane)
            degraded.append(lane)
        elif status in TERMINAL_STATUSES:
            excluded.append(lane)
        else:
            in_floor.append(lane)
    return {
        "source": source,
        "lanes": out,
        "in_floor": in_floor,
        "excluded_terminal": excluded,
        "degraded": degraded,
        "degraded_flag": bool(degraded),
    }


def _lane_list(raw: str) -> list:
    lanes = [tok.strip() for tok in raw.split(",") if tok.strip()]
    bad = [tok for tok in lanes if not (tok.startswith("asp-") and tok[4:].isdigit())]
    if bad:
        raise SystemExit(2)  # argparse epilogue below
    return lanes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Report which boosted lanes still have a live aspiration "
                    "(the set the supply governor's lane floor may sum over).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    chk = sub.add_parser("check", help="classify lanes")
    chk.add_argument("--lanes", required=True,
                     help="comma-separated asp-<digits> lanes (the boosted set)")
    chk.add_argument("--source", default="world", choices=("world", "agent"))
    chk.add_argument("--json", action="store_true", help="machine-readable (default on)")
    args = ap.parse_args(argv)

    try:
        lanes = _lane_list(args.lanes)
    except SystemExit:
        ap.error(f"--lanes must be comma-separated asp-<digits> tokens: {args.lanes!r}")

    if not lanes:
        ap.error("--lanes must name at least one asp-<digits> lane")

    # `reader=` is resolved from the module global AT CALL TIME so tests can
    # monkeypatch `shell_read_status` (a default argument would have bound
    # the original at def-time and the patch would silently never apply).
    verdict = classify_lanes(lanes, source=args.source, reader=shell_read_status)
    print(json.dumps(verdict, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
