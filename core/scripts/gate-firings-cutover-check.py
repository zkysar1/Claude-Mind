#!/usr/bin/env python3
"""Ordering gate for the gate-firings segmentation cutover ().

g-328-38 landed the store-composition seam (`_gate_log.firings_paths`) and a
segmented writer behind `GATE_FIRINGS_SEGMENTED`, defaulting OFF, and named
ORDERING -- not a fleet-quiet window -- as the real constraint:

    the seam must be deployed FLEET-WIDE before ANY box sets the flag.

The hazard is asymmetric and silent. `meta/gate-firings.jsonl` is a SHARED
own-cloud store. If box A flips the flag and starts writing date segments, a
peer box B that predates the seam still reads only the legacy filename -- so it
sees a few hours of data and reports it as the full 30-day retirement window. A
gate then looks unfired and therefore RETIRABLE. That is a false all-clear,
which is the worst direction this system can fail in, and nothing about it looks
broken from either box.

Until now the stated verification was a MANUAL grep on every box. A manual step
whose omission produces a confident wrong answer is not a control. This makes it
checkable:

    --attest   verify THIS box's consumers really carry the seam, then record
               that fact on this agent's team-state shard.
    (default)  read the live fleet roster and report which agents have NOT
               attested. Exit 0 only when every one of them has.

WHY TEAM-STATE: a box cannot read a peer's filesystem, so local grepping can
never answer a fleet question. Team-state is the existing live roster with a
per-agent shard each agent already writes, which makes it the one surface where
"every box has the seam" is expressible at all.

FAIL-CLOSED BY CONSTRUCTION. Every uncertain path -- unreadable roster, missing
attestation, unparseable timestamp, a consumer that cannot be read -- reports
UNSAFE. This inverts the usual fail-open posture of the sweep family, and
deliberately: the thing being gated is a silent false all-clear, so an error
here must never be mistakable for permission to proceed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import PROJECT_ROOT  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402  (guard-580/581)

# The three consumers  repointed. Each must resolve the store through
# the seam; a hardcoded filename here is exactly the pre-seam state.
CONSUMERS = (
    "gate-stats.py",
    "gate-retirement-eval.py",
    "override-ledger-consume.py",
)
SEAM = "firings_paths"

# An attestation records the box's state at a moment. A box can be rolled back,
# so an old attestation is evidence about a tree that may no longer be deployed.
ATTESTATION_MAX_AGE_DAYS = 30


def _strip_comments(text: str) -> str:
    """Drop `#` comments so a prose mention of the seam cannot pass as a call.

    Deliberately naive: it ignores `#` inside string literals, so a line like
    `msg = "call firings_paths(x) # like this"` keeps its text. That errs
    toward counting a consumer as OK, which is the wrong direction -- but the
    alternative (a real Python parse) buys precision this check does not need,
    and no consumer has ever carried such a literal. If one ever does, the
    honest fix is `ast`, not a cleverer regex.
    """
    out = []
    for line in text.splitlines():
        idx = line.find("#")
        out.append(line if idx == -1 else line[:idx])
    return "\n".join(out)


def _local_seam_report() -> dict:
    """Does THIS box's tree actually route all three consumers through the seam?

    Checks for a CALL in EXECUTABLE code, not merely the import and not a
    mention in prose. Two distinct ways this reports a false all-clear if you
    take the obvious shortcut:

    - an `import firings_paths` that nothing calls leaves the consumer reading
      the legacy filename -- the exact pre-seam state -- while a symbol grep
      succeeds;
    - a COMMENT containing `firings_paths(` does the same, and this is not
      hypothetical: two of the three consumers carry exactly such a comment
      today (gate-stats.py and override-ledger-consume.py both explain the
      seam in prose above the call). Revert the call, leave the comment, and
      an uncommented check still reports the seam present. Same referent trap
      as guard-1685 -- the token survives its own removal.

    So comments are stripped before the call is looked for. Anything the strip
    cannot resolve stays counted as missing, which is the fail-closed side.
    """
    scripts = PROJECT_ROOT / "core" / "scripts"
    missing, unreadable, ok = [], [], []
    for name in CONSUMERS:
        p = scripts / name
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            unreadable.append({"consumer": name, "error": str(exc)})
            continue
        code = _strip_comments(text).replace(f"import {SEAM}", "")
        if f"{SEAM}(" in code:
            ok.append(name)
        else:
            missing.append(name)
    return {
        "seam_present": not missing and not unreadable,
        "ok": ok,
        "missing": missing,
        "unreadable": unreadable,
    }


def _head_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _read_team_state() -> tuple[dict, str | None]:
    try:
        out = subprocess.run(
            bash_cmd(PROJECT_ROOT / "core" / "scripts" / "team-state-read.sh",
                     "--json"),
            capture_output=True, text=True, timeout=90,
        )
        if out.returncode != 0:
            return {}, f"team-state-read exit {out.returncode}: {out.stderr.strip()[:200]}"
        return json.loads(out.stdout), None
    except Exception as exc:
        return {}, f"team-state unreadable: {exc}"


def _parse_ts(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", ""))
    except ValueError:
        return None


def cmd_attest(args) -> int:
    report = _local_seam_report()
    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        print(json.dumps({"verdict": "error",
                          "detail": "MIND_AGENT unset — cannot attest"}, indent=2))
        return 3
    if not report["seam_present"]:
        report.update({"verdict": "refused", "agent": agent,
                       "detail": "this box does not carry the seam in every "
                                 "consumer — attesting would assert the "
                                 "precondition this gate exists to verify"})
        print(json.dumps(report, indent=2))
        return 2

    payload = {
        "attested_at": datetime.now().replace(microsecond=0).isoformat(),
        "commit": _head_commit(),
        "consumers": list(report["ok"]),
    }
    rc = subprocess.run(
        bash_cmd(PROJECT_ROOT / "core" / "scripts" / "team-state-update.sh",
                 "--field", f"agent_status.{agent}.gate_firings_seam",
                 "--value", json.dumps(payload)),
        capture_output=True, text=True, timeout=90,
    )
    if rc.returncode != 0:
        print(json.dumps({"verdict": "error", "agent": agent,
                          "detail": f"team-state write failed: "
                                    f"{rc.stderr.strip()[:300]}"}, indent=2))
        return 3
    print(json.dumps({"verdict": "attested", "agent": agent, **payload}, indent=2))
    return 0


def cmd_check(args) -> int:
    ts, err = _read_team_state()
    if err:
        print(json.dumps({
            "verdict": "UNSAFE",
            "reason": "roster_unreadable",
            "detail": err,
            "note": "fail-closed: an unreadable roster cannot show that every "
                    "box carries the seam (rb-245 — a zero-count from a broken "
                    "read is not a zero)",
        }, indent=2))
        return 2

    roster = ts.get("agent_status") or {}
    now = datetime.now()
    attested, unattested, stale, retired = [], [], [], []

    for name, row in sorted(roster.items()):
        if not isinstance(row, dict):
            unattested.append({"agent": name, "reason": "unreadable shard"})
            continue
        if row.get("retired_at"):
            retired.append(name)
            continue
        seam = row.get("gate_firings_seam")
        if not isinstance(seam, dict):
            unattested.append({"agent": name,
                               "last_active": row.get("last_active")})
            continue
        when = _parse_ts(seam.get("attested_at"))
        if when is None:
            unattested.append({"agent": name,
                               "reason": "unparseable attested_at"})
        elif now - when > timedelta(days=ATTESTATION_MAX_AGE_DAYS):
            stale.append({"agent": name, "attested_at": seam.get("attested_at"),
                          "age_days": round((now - when).total_seconds() / 86400, 1),
                          "commit": seam.get("commit")})
        else:
            attested.append({"agent": name, "commit": seam.get("commit"),
                             "attested_at": seam.get("attested_at")})

    blockers = len(unattested) + len(stale)
    # An empty roster is not unanimity. Zero agents attesting vacuously satisfies
    # "all agents have attested", which is the guard-1665 shape: a predicate that
    # returns clean because it matched nothing.
    if not roster or (not attested and not unattested and not stale):
        verdict, rc = "UNSAFE", 2
        reason = "empty_roster"
    elif blockers:
        verdict, rc = "UNSAFE", 2
        reason = "unattested_or_stale_boxes"
    else:
        verdict, rc = "SAFE", 0
        reason = "every live agent has a current seam attestation"

    print(json.dumps({
        "verdict": verdict,
        "reason": reason,
        "flag": "GATE_FIRINGS_SEGMENTED",
        "attested": attested,
        "unattested": unattested,
        "stale": stale,
        "retired_skipped": retired,
        "local": _local_seam_report(),
        "guidance": (
            "SAFE: the seam is fleet-wide; GATE_FIRINGS_SEGMENTED may be set "
            "per box. UNSAFE: do NOT set the flag — a peer without the seam "
            "reads only the legacy filename, sees hours of data as the full "
            "30-day window, and reports a still-firing gate as retirable. "
            "Each listed box runs: bash core/scripts/gate-firings-cutover-check.sh --attest"
        ),
    }, indent=2))
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--attest", action="store_true",
                    help="verify this box's seam and record it on team-state")
    args = ap.parse_args()
    return cmd_attest(args) if args.attest else cmd_check(args)


if __name__ == "__main__":
    sys.exit(main())
