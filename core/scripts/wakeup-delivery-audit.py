#!/usr/bin/env python3
"""Deadman wakeup DELIVERY audit ().

Question: does a scheduled wakeup whose fire-time lands while the session is
still BUSY get delivered later (or lost) versus one that lands during silence?

Rebuild of the instrument the goal named at agents/zeta/temp/wakeup_gaps.py,
which no longer exists. It is placed in core/scripts/ ON PURPOSE: temp/ is a
QUEUE that gets drained (core/config/conventions/temp-store.md), so an
instrument cited from there is guaranteed to vanish, which is what happened.

THE CONFOUND THIS FIXES (named in the goal, not discovered here): the retracted
first pass measured BUSY latency from gap_start and IDLE latency from sched, so
the two classes had different clocks and the p50/p90 gap was structural. Here
BOTH classes are measured from a COMMON ORIGIN (sched), and gap length is
reported alongside so the reader can stratify rather than trust a pooled number.

Emits counts for every exclusion. A filter applied to one consumer and not
another is the exact error that produced the retracted headline.
"""
import json, sys, os, glob
from collections import defaultdict

GAP_MIN_S = 60.0          # a silence shorter than this is not a gap
SENTINEL  = "<<autonomous-loop-dynamic>>"

def ts_ms(iso):
    # Transcript stamps carry an explicit trailing Z (UTC). strptime returns a
    # NAIVE datetime and .timestamp() would read it as LOCAL time, while
    # scheduledFor is a true UTC epoch — mixing the two offsets EVERY latency by
    # this box's UTC offset. Measured: +4h under America/New_York, -9h under
    # Asia/Tokyo, exact under UTC. That is not hypothetical here (rb-3741: the
    # fleet is TZ-split, a WSL2 box stamps EDT), and it would manufacture
    # multi-hour "delivery latency" — a false positive for the very question
    # this script exists to measure. timegm reads the tuple as UTC. Slicing to
    # [:19] discards the Z, which is guard-1398's pattern, so the UTC reading
    # must be supplied explicitly rather than inferred.
    import calendar
    from datetime import datetime
    return float(calendar.timegm(
        datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").timetuple()))

def scan(path):
    """One pass: every record time, plus arms carrying scheduledFor."""
    times, arms, human, compact, bad = [], [], set(), set(), []
    dropped = {"unparsable_json": 0, "unparsable_timestamp": 0}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"timestamp"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                dropped["unparsable_json"] += 1
                continue
            t = d.get("timestamp")
            if not t:
                continue
            try:
                sec = ts_ms(t)
            except Exception:
                dropped["unparsable_timestamp"] += 1
                continue
            times.append(sec)
            tur = d.get("toolUseResult")
            if isinstance(tur, dict) and "scheduledFor" in tur:
                # SANITY-GATE the field rather than trusting it (guard-2298).
                # A 0/None/second-valued scheduledFor silently yields a latency
                # equal to the raw epoch (~1.8e9 s == 56 years), which reads as a
                # catastrophic delivery failure instead of as a bad input. The
                # first full-corpus run of this script printed max_h 496525.55 for
                # exactly that reason; the gate turns it into a counted reject.
                raw = tur.get("scheduledFor")
                if isinstance(raw, (int, float)) and 1.0e12 < raw < 4.0e12:
                    arms.append((sec, raw / 1000.0))
                else:
                    bad.append(raw)
            # classify how a gap ENDED: a real human turn vs a machine resume.
            if d.get("type") == "user":
                blob = json.dumps(d.get("message", {}))
                if "tool_result" not in blob:
                    if SENTINEL in blob or "Skill /aspirations is already loaded" in blob:
                        pass                      # machine re-entry: eligible
                    elif "session is being continued" in blob.lower() or "compact" in blob.lower():
                        compact.add(round(sec, 3))
                    else:
                        human.add(round(sec, 3))
    times.sort()
    return times, arms, human, compact, bad, dropped

def main(paths):
    tot = defaultdict(int)
    rows = []
    for p in paths:
        times, arms, human, compact, bad, dropped = scan(p)
        for k, v in dropped.items():
            tot[k] += v
        tot["rejected_bad_scheduledFor"] += len(bad)
        if len(times) < 2:
            continue
        gaps = [(times[i], times[i+1]) for i in range(len(times)-1)
                if times[i+1] - times[i] >= GAP_MIN_S]
        tot["records"] += len(times); tot["arms"] += len(arms); tot["gaps"] += len(gaps)
        arms.sort()
        for idx, (arm_t, sched) in enumerate(arms):
            # REPLACE-SLOT: a later arm before this one's fire time supersedes it.
            if idx + 1 < len(arms) and arms[idx+1][0] < sched:
                tot["replaced_before_fire"] += 1
                continue
            g = next(((s, e) for (s, e) in gaps if e > sched), None)
            if g is None:
                tot["no_following_gap"] += 1
                continue
            gs, ge = g
            if round(ge, 3) in human:
                tot["excl_human_typed"] += 1;  continue
            if round(ge, 3) in compact:
                tot["excl_compact_resume"] += 1; continue
            rows.append({
                "busy": sched < gs,                 # fire landed mid-turn
                "lat_common": ge - sched,           # COMMON ORIGIN, both classes
                "gap_len": ge - gs,
            })
    def stats(rs, key="lat_common"):
        v = sorted(r[key] for r in rs)
        if not v: return None
        q = lambda p: v[min(len(v)-1, int(len(v)*p))]
        return {"n": len(v), "p50": round(q(.50),1), "p90": round(q(.90),1),
                "p99": round(q(.99),1), "max_h": round(v[-1]/3600,2),
                "ge_1h": sum(1 for x in v if x >= 3600),
                "ge_1h_pct": round(100*sum(1 for x in v if x >= 3600)/len(v),2)}
    busy = [r for r in rows if r["busy"]]
    idle = [r for r in rows if not r["busy"]]
    out = {"totals": dict(tot), "eligible": len(rows),
           "BUSY_common_origin": stats(busy), "IDLE_common_origin": stats(idle),
           "strata": {}}
    # STRATIFY by gap length -- the goal's own suggestion for a non-confounded test.
    for lo, hi, name in [(0,600,"gap<10m"), (600,3600,"gap10m-1h"), (3600,10**9,"gap>1h")]:
        b = [r for r in busy if lo <= r["gap_len"] < hi]
        i = [r for r in idle if lo <= r["gap_len"] < hi]
        out["strata"][name] = {"BUSY": stats(b), "IDLE": stats(i)}
    # A silent zero is the failure this script is about. An empty/!=-matching
    # input set renders IDENTICALLY to "measured, nothing eligible" — so say so
    # on stderr rather than emitting confident nulls (guard-1587/guard-1665).
    if not paths:
        print("wakeup-delivery-audit: NO INPUT FILES — the default glob matched "
              "nothing (it is box-specific). This output is not a measurement.",
              file=sys.stderr)
    elif not rows:
        print("wakeup-delivery-audit: %d file(s) read but ZERO eligible rows. "
              "Check the exclusion counts in totals before reading this as "
              "'no latency'." % len(paths), file=sys.stderr)
    print(json.dumps(out, indent=1))

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        args = sorted(glob.glob("/root/.claude/projects/-opt-ayoai-mind/*.jsonl"))
    main(args)
