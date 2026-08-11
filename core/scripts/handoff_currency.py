#!/usr/bin/env python3
"""Handoff currency gate — decide whether boot Step 0.5's ABBREVIATED
auto-continuation path may run, or must fall through to a full boot.

WHY (g-115-4671, measured fleet-wide 2026-08-08 by zeta on cc-02): boot Step
0.5 sub-step 5 "Delete handoff.yaml (consumed)" is a LOCAL unlink with no
backend delete. Under own-cloud the S3 object survives the consume, and
handoff.yaml is in the continuity pull-set, so the NEXT /start re-materializes
it. On an agent whose sessions end by CRASH rather than /stop, no new handoff is
ever written, so S3 pins the last GRACEFUL stop's copy indefinitely while local
sessions keep advancing. guard-1493 states the general form: "a local-only
unlink is silently RE-MATERIALIZED by read-through on the next read."

Measured at filing time (embedded handoff timestamp vs that agent's
journal-read --meta last_updated; all five journals last_updated 2026-08-07):

    alpha    2026-08-05T18:56   ~2d
    bravo    2026-07-26T17:41  ~13d
    echo     2026-07-28T11:30  ~11d
    foxtrot  2026-07-26T20:06  ~13d
    zeta     2026-07-26T20:25  ~13d

exists=true for ALL FIVE — this is fleet-wide, not one agent's quirk. The blast
radius is goal SELECTION: a resurrected `first_action` is handed to the loop as
a pre-scored top pick and bypasses fresh scoring.

WHY TIMESTAMP, NOT session_number. The originating goal proposed comparing
handoff `session_number` against the journal's session count. Those are
DIFFERENT COUNTERS and comparing them would be an unmeasured mechanism claim
(guard-1476) — measured: bravo's handoff says 62 while its journal reports 426
total_sessions, a gap that says nothing on its own. The embedded `timestamp`
and the journal's `last_updated` are directly comparable, and that comparison
is what the evidence above actually supports.

TWO INDEPENDENT ARMS (g-115-5313, each probed). Staleness is measured BOTH as
journal-minus-handoff AND as wall-clock-minus-handoff, and either one breaching
the threshold refuses. The journal arm alone cannot fire on a DORMANT agent —
its journal is stale too — which is exactly the crash-heavy population this gate
exists for. Probed before the fix: a handoff of 2026-01-01 with a journal of
2026-01-02 read `current` on 2026-08-08, seven months old, because `now` was
accepted, documented, threaded through all 10 tests, and never read.

WHAT NEWLY FIRES (guard-1562): any handoff older than the threshold in absolute
time now refuses, where before only a handoff behind its own journal did. That
is a strict widening, and it is the fail-SAFE direction — exit 2 does not block
work, it routes boot to the FULL path instead of the abbreviated one.

FAIL-OPEN BY CONSTRUCTION (guard-142): a gate that blocks work because of its
own bugs is worse than the problem it catches. Every error path — unreadable
handoff, unparseable timestamp, missing journal, unusable clock, bad threshold —
leaves the corresponding arm silent, and a gate with no arm firing returns
CURRENT so boot proceeds exactly as it does today. The gate can only ever REFUSE
on a positively-measured staleness, never on an absence of evidence.

Exit codes: 0 = current (proceed with auto-continuation) · 2 = STALE (fall
through to full boot). Never exits non-zero for any other reason.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

CURRENT, STALE = 0, 2
DEFAULT_MAX_AGE_DAYS = 3.0

# Anchored at COLUMN 0, not `^\s*` (, probed): with re.M + .search the
# leading-whitespace form takes the EARLIEST match, so a NESTED `timestamp:` key
# above the top-level one shadows it — a nested fresh value made a 12d-stale
# handoff read current.
# The character class carries `.`, `+` and `Z` because 3 of 4 realistic ISO
# shapes (fractional seconds, Z suffix, numeric offset) missed the old class
# entirely and went permissive with no log line.
_TS = re.compile(r"""^timestamp\s*:\s*['"]?([0-9T:.+\-Z ]+?)['"]?\s*$""", re.M)
_SN = re.compile(r"""^session_number\s*:\s*['"]?(\d+)['"]?\s*$""", re.M)


def _parse_dt(raw: str):
    raw = (raw or "").strip().replace(" ", "T")
    # Offset/Z-aware shapes first, normalized to naive UTC. The prefix-truncating
    # loop below silently DROPS an offset, which the widened _TS class now lets
    # through — and the fleet compares naive stamps on a shared UTC wall clock
    # (CLAUDE.md Naming Rules; rb-3741 is the cost of getting this wrong).
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
    return None


def _max_age_days() -> float:
    raw = os.environ.get("HANDOFF_CURRENCY_MAX_DAYS")
    if not raw:
        return DEFAULT_MAX_AGE_DAYS
    try:
        v = float(raw)
        return v if v > 0 else DEFAULT_MAX_AGE_DAYS
    except ValueError:
        return DEFAULT_MAX_AGE_DAYS


def decide(handoff_text: str, journal_last_updated: str, now: datetime,
           max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> dict:
    """Pure decision. Returns {verdict, reason, age_days, ...}.

    verdict is "current" or "stale". Anything the gate cannot positively
    establish yields "current" (fail-open)."""
    out = {"verdict": "current", "reason": "", "age_days": None,
           "wall_age_days": None, "stale_arms": [],
           "handoff_timestamp": None, "journal_last_updated": journal_last_updated,
           "session_number": None, "max_age_days": max_age_days}

    if not handoff_text:
        out["reason"] = "no handoff content — nothing to gate"
        return out

    m = _TS.search(handoff_text)
    if not m:
        out["reason"] = "handoff carries no parseable timestamp — failing open (guard-142)"
        return out
    out["handoff_timestamp"] = m.group(1).strip()

    sn = _SN.search(handoff_text)
    if sn:
        out["session_number"] = int(sn.group(1))

    h_dt = _parse_dt(out["handoff_timestamp"])
    if h_dt is None:
        out["reason"] = "handoff timestamp unparseable — failing open (guard-142)"
        return out

    # ARM 2 — WALL-CLOCK age. Evaluated FIRST and independently of the journal:
    # gating it behind the journal arm would re-create the exact blind spot
    #  probed, because the dormant agents this arm exists for are the
    # ones whose journal is also stale. An unusable clock leaves the arm silent.
    wall_age = None
    if isinstance(now, datetime):
        try:
            wall_age = (now - h_dt).total_seconds() / 86400.0
        except Exception:  # noqa: BLE001 — fail-open is the whole contract
            wall_age = None
    if wall_age is not None:
        out["wall_age_days"] = round(wall_age, 2)
        if wall_age > max_age_days:
            out["stale_arms"].append("wall-clock")

    # ARM 1 — JOURNAL lag. The journal is the relative freshness reference.
    # Absent/unparseable -> this arm stays silent (not a refusal): "cannot
    # establish" must never become "refuse". Arm 2 may still have fired.
    j_dt = _parse_dt(journal_last_updated) if journal_last_updated else None
    if j_dt is not None:
        age = (j_dt - h_dt).total_seconds() / 86400.0
        out["age_days"] = round(age, 2)
        if age > max_age_days:
            out["stale_arms"].append("journal")

    if out["stale_arms"]:
        out["verdict"] = "stale"
        out["reason"] = (
            "handoff is stale on the %s arm(s) [journal lag %s, wall-clock age %s, "
            "threshold %.1fd]: under own-cloud a handoff this far behind was almost "
            "certainly resurrected from the backend after a local-only consume "
            "(g-115-4671, guard-1493), and the wall-clock arm additionally covers a "
            "DORMANT agent whose journal is stale too (g-115-5313). Falling through "
            "to a full boot rather than resuming an arbitrarily stale first_action."
            % (" + ".join(out["stale_arms"]),
               "n/a" if out["age_days"] is None else "%.1fd" % out["age_days"],
               "n/a" if out["wall_age_days"] is None else "%.1fd" % out["wall_age_days"],
               max_age_days))
        return out

    if j_dt is None:
        out["reason"] = (
            "journal last_updated unreadable — journal arm failing open (guard-142); "
            "wall-clock arm %s" % (
                "unavailable (no usable clock)" if wall_age is None
                else "did not fire (%.1fd <= %.1fd)" % (wall_age, max_age_days)))
        return out

    out["reason"] = (
        "handoff is %.1fd behind the journal and %s old (both <=%.1fd) — current" % (
            out["age_days"],
            "n/a" if out["wall_age_days"] is None else "%.1fd" % out["wall_age_days"],
            max_age_days))
    return out


def main(argv) -> int:
    agent = os.environ.get("MIND_AGENT", "")
    as_json = "--json" in argv
    for i, a in enumerate(argv):
        if a == "--agent" and i + 1 < len(argv):
            agent = argv[i + 1]

    result = {"verdict": "current", "reason": "gate did not run", "agent": agent}
    try:
        handoff_path = os.environ.get("HANDOFF_PATH") or (
            os.path.join("agents", agent, "session", "handoff.yaml"))
        text = ""
        if os.path.exists(handoff_path):
            with open(handoff_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        journal_lu = os.environ.get("JOURNAL_LAST_UPDATED", "")
        result = decide(text, journal_lu, datetime.now(), _max_age_days())
        result["agent"] = agent
    except Exception as exc:  # noqa: BLE001 — fail-open is the whole contract
        result = {"verdict": "current", "agent": agent,
                  "reason": "gate error (%s) — failing open (guard-142)" % exc}

    if as_json:
        print(json.dumps(result))
    else:
        print("handoff-currency: %s — %s" % (result["verdict"], result["reason"]))
    return STALE if result.get("verdict") == "stale" else CURRENT


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
