#!/usr/bin/env python3
"""Surface directive-vs-actual goal mix in the precheck header.

WHY (g-115-3279): `strategic_focus` in team state names the aspirations the
owner wants worked NOW, and `goal-selector.py` already implements it as a
scoring boost (`strategic_focus_boost`, shipped g-115-3136). What never
existed is a per-iteration SURFACE: nothing showed whether the realised close
mix matched the directive, so a 69/11 inversion sat unnoticed for 22 days.

SCOPE -- read before extending. This lane is OBSERVABILITY ONLY. It does not
boost, veto, re-rank or gate anything, and it must not: the selector already
carries the enforcement term, and re-applying the directive as a per-pick
override is forbidden by consolidate-before-expand rule 2 and refused at write
time by the scorer-sovereignty gate. rb-5003 is the standing caution that
surfacing a signal is NOT eliciting the behaviour -- so treat a persistent
skew as input to a WEIGHT-TUNING decision backed by multi-iteration evidence,
never as licence to hand-pick goals.

COUNTING. One-offs are stamped `completed_date`; recurring goals are stamped
`lastAchievedAt` and never reach status=completed. A status=completed-only
scan therefore drops the entire recurring lane, and always in the direction
that flatters compliance -- hence two queries, unioned by goal id.

STORE ACCESS. Goals come through `aspirations-query.sh`, never a hand parser
over the raw store (CLAUDE.md: the LLM never reads JSONL stores directly; the
wrapper owns the schema, the external path and the daemon cache).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_bash import bash_cmd  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

ASP_RE = re.compile(r"\basp-(\d+)\b")
_QUERY_TIMEOUT_S = 120


def _parse_ts(v):
    if not v or not isinstance(v, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(v[:19] if "T" in v else v[:10], fmt)
        except ValueError:
            continue
    return None


def _decode_list(raw):
    i = raw.find("[")
    if i < 0:
        return []
    try:
        got = json.JSONDecoder().raw_decode(raw[i:])[0]
    except json.JSONDecodeError:
        return []
    return got if isinstance(got, list) else []


def focus_aspirations(team_state):
    """Extract asp-NNN ids from the strategic_focus prose.

    Deliberately the same regex-over-prose approach goal-selector.py uses: if
    the two disagreed about which aspirations the directive names, this
    surface would report on a different set than the boost acts on, which is
    worse than no surface at all.
    """
    sf = (team_state or {}).get("strategic_focus")
    if not sf:
        return [], ""
    text = sf if isinstance(sf, str) else json.dumps(sf)
    return sorted({"asp-" + m for m in ASP_RE.findall(text)}), text


def mix(goals, window_hours, now):
    cutoff = now - timedelta(hours=window_hours)
    per = {}
    total = 0
    no_stamp = 0
    for g in goals:
        recurring = bool(g.get("recurring"))
        if recurring:
            stamp = g.get("lastAchievedAt")
        else:
            if g.get("status") != "completed":
                continue
            stamp = g.get("completed_date")
        ts = _parse_ts(stamp)
        if ts is None:
            # COUNTED, never silently dropped. Measured 2026-08-31:
            # 56 of 621 loaded goals carried no completion stamp -- 9%
            # of the population vanishing from the denominator with no
            # trace, making `closes_total` a floor and the percentage
            # unauditable. Same posture as basis_suppressed in
            # recurring-starvation-check.py: count the exclusion so it
            # is visible, not invisible (guard-963).
            no_stamp += 1
            continue
        if ts < cutoff:
            continue
        # `asp_id` is what aspirations-query.sh --full actually returns.
        # The id-derived fallback is deliberate: an unrecognised goal must not
        # collapse into an "unknown" bucket that reads as 0% on-directive --
        # that exact shape shipped once here and the finding it produced was
        # entirely spurious (a false zero of the reader's own making).
        asp = g.get("asp_id") or g.get("aspiration_id")
        if not asp:
            m = re.match(r"^g-(\d+)-", g.get("id") or "")
            asp = ("asp-" + m.group(1)) if m else "unknown"
        per[asp] = per.get(asp, 0) + 1
        total += 1
    return per, total, no_stamp


_TERMINAL = {"completed", "skipped", "expired", "decomposed", "superseded"}


def focus_eligibility(goals, focus):
    """How many focus-lane goals were ELIGIBLE to be closed, and why the rest
    were not.

    THE DENOMINATOR THE SHARE WAS MISSING (g-115-8430). `on_directive_pct`
    divides focus-lane closes by TOTAL closes and never asks whether any
    focus-lane goal could have been closed at all. So a lane whose entire
    population is externally gated and a lane the fleet is ignoring emit the
    IDENTICAL low percentage -- the finding is unfalsifiable from its own
    output. Measured 2026-08-31 (zeta, cc-02): pct=14.7 against floor 25.0 with
    finding=true, while goal-selector emitted STRATEGIC-FOCUS INERT the same
    minute -- ZERO of 1514 ranked goals came from the four focus lanes, and all
    21 deferred focus goals carried structured prefixes on genuinely external
    gates (Stripe-side review, an owner-only IAM grant, a promotion owned by
    another goal, an absent provisioning path).

    Returns (eligible, blocked, reasons). ELIGIBLE = non-terminal, not blocked,
    no defer_reason -- i.e. a goal the fleet could actually have picked up.
    Emitting the count is the point (guard-3489): a check that reports a clean
    or dirty verdict must also report the coverage behind it.
    """
    eligible = 0
    blocked = 0
    reasons = {}
    for g in goals:
        asp = g.get("asp_id") or g.get("aspiration_id")
        if not asp:
            m = re.match(r"^g-(\d+)-", g.get("id") or "")
            asp = ("asp-" + m.group(1)) if m else None
        if asp not in focus:
            continue
        if (g.get("status") or "") in _TERMINAL:
            continue
        if g.get("status") == "blocked":
            blocked += 1
            reasons["blocked_status"] = reasons.get("blocked_status", 0) + 1
        elif g.get("defer_reason"):
            blocked += 1
            # Bucket by the structured prefix when there is one, so a reader can
            # tell a re-probing precondition from a human gate without opening
            # 21 records by hand.
            pref = str(g.get("defer_reason")).split(":", 1)[0][:32]
            key = "deferred:" + (pref if len(pref) < 32 else "unstructured")
            reasons[key] = reasons.get(key, 0) + 1
        else:
            eligible += 1
    return eligible, blocked, reasons


def build(team_state, goals, window_hours, now, floor_pct=25.0):
    focus, text = focus_aspirations(team_state)
    per, total, no_stamp = mix(goals, window_hours, now)
    focus_eligible, focus_blocked, focus_block_reasons = focus_eligibility(
        goals, focus
    )
    on = sum(n for a, n in per.items() if a in focus)
    pct = round(100.0 * on / total, 1) if total else None
    top = sorted(per.items(), key=lambda kv: -kv[1])[:5]
    return {
        "window_hours": window_hours,
        "focus_aspirations": focus,
        "directive_present": bool(focus),
        "closes_total": total,
        "excluded_no_stamp": no_stamp,
        "closes_on_directive": on,
        "on_directive_pct": pct,
        "top_aspirations": [
            {"aspiration_id": a, "closes": n, "on_directive": a in focus}
            for a, n in top
        ],
        # THE ELIGIBILITY DENOMINATOR (). Reported ALWAYS, not only
        # when it changes the verdict: the share alone cannot distinguish a
        # gated lane from an ignored one, so these three fields are what make
        # the number falsifiable from its own output.
        "focus_eligible": focus_eligible,
        "focus_blocked": focus_blocked,
        "focus_block_reasons": focus_block_reasons,
        # No directive, or no closes in the window, is an ABSENCE OF EVIDENCE,
        # not a finding -- firing on it would make this lane noise on every
        # fresh session and train the reader to skip the line.
        #
        # `focus_eligible > 0` is the same posture applied one level deeper. A
        # low share with ZERO eligible focus goals is not an allocation choice
        # the fleet made -- there was nothing in the lanes to pick up, so the
        # finding would be a permanent false alarm, and a permanently-true
        # finding trains the fleet to ignore an always-run lane. This SUPPRESSES
        # the verdict; it never narrows the measurement (guard-3909): pct,
        # closes, and the block breakdown are all still emitted.
        "finding": (
            bool(focus)
            and total > 0
            and pct is not None
            and pct < floor_pct
            and focus_eligible > 0
        ),
        # Set only when the finding was suppressed by the clause above, so the
        # suppression is auditable rather than silent (a suppressed alarm and a
        # healthy lane must not read identically).
        "finding_suppressed_reason": (
            "focus lanes externally gated: %d eligible, %d blocked"
            % (focus_eligible, focus_blocked)
            if (
                bool(focus)
                and total > 0
                and pct is not None
                and pct < floor_pct
                and focus_eligible == 0
            )
            else None
        ),
        # Positive-flag mirror of `finding`, consumed by the always-run
        # battery's `false` handler. True (healthy) whenever there is no
        # directive or no closes yet -- absence of evidence must not read as a
        # skew, or the lane becomes noise on every fresh session.
        "on_directive_ok": not (
            bool(focus)
            and total > 0
            and pct is not None
            and pct < floor_pct
            and focus_eligible > 0
        ),
        "floor_pct": floor_pct,
        "strategic_focus_excerpt": text[:180],
    }


def load_goals():
    seen = set()
    goals = []
    # The third query is REQUIRED by focus_eligibility () and must not
    # be dropped as redundant: the first two load only COMPLETED and RECURRING
    # goals, so a blocked or deferred focus-lane goal is structurally absent
    # from this population. Computing an eligibility denominator over it
    # returned `4 eligible / 0 blocked` on a fleet that actually held 35 blocked
    # focus goals -- the predicate was narrower than the question (guard-1802),
    # and the wrong number looked entirely plausible. mix() is unaffected: it
    # skips non-recurring goals whose status is not `completed`.
    for argv in (["--goal-status", "completed", "--full"],
                 ["--goal-field", "recurring", "true", "--full"],
                 ["--goal-status", "pending,in-progress,blocked", "--full"]):
        try:
            r = subprocess.run(
                bash_cmd(str(SCRIPT_DIR / "aspirations-query.sh"), *argv),
                capture_output=True, text=True, timeout=_QUERY_TIMEOUT_S)
            rows = _decode_list(r.stdout)
        except Exception:
            rows = []
        for g in rows:
            gid = g.get("id")
            if gid and gid in seen:
                continue
            if gid:
                seen.add(gid)
            goals.append(g)
    return goals


def load_team_state():
    try:
        r = subprocess.run(
            bash_cmd(str(SCRIPT_DIR / "team-state-read.sh"), "--json"),
            capture_output=True, text=True, timeout=30)
        i = r.stdout.find("{")
        if i < 0:
            return None
        return json.JSONDecoder().raw_decode(r.stdout[i:])[0]
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="directive-vs-actual mix surface")
    ap.add_argument("--window-hours", type=float, default=168.0)
    ap.add_argument("--floor-pct", type=float, default=25.0)
    ap.add_argument("--apply", action="store_true",
                    help="accepted for battery uniformity; this lane is read-only")
    ap.add_argument("--output", default="json")
    args = ap.parse_args(argv)

    team_state = load_team_state()
    if team_state is None:
        # Fail-open and SAY SO. An unreadable input must never render as a
        # clean zero -- that is the shape this whole session kept catching.
        print(json.dumps({"error": "team-state unreadable", "finding": False,
                          "directive_present": None}, indent=2))
        return 0

    goals = load_goals()
    if not goals:
        print(json.dumps({"error": "aspirations-query.sh returned no goals",
                          "finding": False, "directive_present": None}, indent=2))
        return 0

    print(json.dumps(
        build(team_state, goals, args.window_hours, datetime.now(), args.floor_pct),
        indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
