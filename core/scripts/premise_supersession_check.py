#!/usr/bin/env python3
"""Premise-supersession check: is the REASON for this goal still true?

gap-142. Sibling of the done-but-pending check (gap-100), which asks whether the
DELIVERABLE exists. This asks whether the PREMISE does. They dissociate: a goal
can have live work and a dead premise, and a done-check clears you to proceed on
a false frame.

Emits an ENUMERATION, never a verdict on the claims themselves — re-measuring an
arbitrary cited number needs judgment. The value is making the citations visible
and dated at the moment of claim, which is when they are currently invisible.

LOUD BY CONTRACT: every path that cannot do its job says so and exits non-zero.
A check that declines to run must never report success by default.
"""
import argparse, json, re, subprocess, sys, datetime, pathlib

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from _runtime_bash import bash_cmd  # noqa: E402  # guard-580/581

# A cited measurement: "447 of 491", "37%", "2049 records", "1,212 goals", "58,322 B"
CLAIM_RE = re.compile(
    r'(?P<claim>'
    r'\b\d[\d,]*\s*(?:of|/|out of)\s*\d[\d,]*\b'      # N of M
    r'|\b\d[\d,]*(?:\.\d+)?\s*%'                       # percentages
    r'|\b\d[\d,]*(?:\.\d+)?\s*(?:B|KB|MB|bytes|tokens|records|goals|files|scripts|lines|rows|entries)\b'
    r')', re.I)
# A file path the goal names, so git can be asked whether it moved since filing.
PATH_RE = re.compile(r'\b(?:core|world|meta|agents|mind_api|\.claude)/[\w./-]+\.\w{1,5}\b')


def run(cmd, cwd=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=60)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 127, "", str(e)


def load_goal(goal_id, root):
    # bash_cmd, NOT a bare `.sh` as argv[0] (guard-580/581 — fresh-eyes F-002).
    # A bare shell-script argv[0] works on Linux via the shebang and CANNOT be
    # exec'd by Windows CreateProcess, so this script was permanently rc=2 on
    # every Windows box — and its caller printed nothing on that path (F-001),
    # making the whole advisory silently dead there. Same portability class as
    # the cygpath defect the suite caught in this script's own .sh wrapper.
    rc, out, err = run(bash_cmd(_HERE / "aspirations-query.sh",
                                "--goal-field", "id", goal_id, "--full"),
                       cwd=str(root))
    if rc != 0:
        return None, f"aspirations-query.sh exited {rc}: {(err or out)[:200]}"
    try:
        d = json.loads(out)
    except Exception as e:
        return None, f"goal record did not parse as JSON ({e}); {len(out)} bytes returned"
    g = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) and d else None)
    if not g:
        return None, f"no goal record returned for {goal_id} (query succeeded, 0 rows)"
    return g, None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("goal_id")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--root", default=".", help="project root (default: cwd)")
    a = ap.parse_args()
    root = pathlib.Path(a.root).resolve()

    goal, err = load_goal(a.goal_id, root)
    if err:
        print(f"[premise-supersession] CANNOT CHECK: {err}", file=sys.stderr)
        print("[premise-supersession] This is NOT a clean result — the premise is UNVERIFIED.",
              file=sys.stderr)
        return 2

    title = goal.get("title") or ""
    desc = goal.get("description") or ""
    haystack = f"{title}\n{desc}"

    # (a) the goal's OWN record — the decisive field, per gap-142's amendment
    own = {k: goal.get(k) for k in ("outcome_note", "outcome_notes", "progress_note")
           if goal.get(k)}

    # cited measurements, deduped, order-preserving
    claims, seen = [], set()
    for m in CLAIM_RE.finditer(haystack):
        c = " ".join(m.group("claim").split())
        if c.lower() not in seen:
            seen.add(c.lower()); claims.append(c)

    paths = sorted({p for p in PATH_RE.findall(haystack)})

    # SCHEMA-VERIFIED 2026-08-26: the field is `created_at`. The first draft of this
    # line read ("created", "filed_at", "added_at") — none of which exist on a goal
    # record — so `filed` was always "", age_days always None, and the
    # RE-MEASURE-BEFORE-EXECUTING verdict was STRUCTURALLY UNREACHABLE (guard-3130,
    # rb-245: audit a field only after reading one record to confirm it exists).
    # Caught by the Step 3.6 dogfood, before registration.
    filed = (goal.get("created_at") or goal.get("created")
             or goal.get("filed_at") or goal.get("added_at") or "")[:19]
    age_days = None
    if filed:
        try:
            age_days = (datetime.datetime.now()
                        - datetime.datetime.fromisoformat(filed)).days
        except Exception:
            pass

    # (b) has anything touched the named files since the goal was filed?
    commits, git_note = [], None
    if paths and filed:
        rc, out, errtxt = run(["git", "log", f"--since={filed}", "--format=%h%x09%ad%x09%s",
                               "--date=short", "--", *paths], cwd=str(root))
        if rc != 0:
            git_note = f"git log failed rc={rc}: {(errtxt or '')[:120]}"
        else:
            commits = [l for l in out.splitlines() if l.strip()][:15]
    elif not paths:
        git_note = "no file paths named in title/description — git evidence lane not applicable"
    elif not filed:
        git_note = "goal record carries no creation timestamp — cannot bound git log"

    stale = (age_days is not None and age_days >= 7 and bool(claims))
    result = {
        "goal_id": a.goal_id, "title": title[:120], "filed": filed, "age_days": age_days,
        "own_record_fields_present": sorted(own.keys()),
        "own_record": {k: (v if isinstance(v, str) else str(v))[:600] for k, v in own.items()},
        "cited_measurements": claims, "cited_measurement_count": len(claims),
        "named_paths": paths,
        "commits_touching_named_paths_since_filing": commits,
        "git_note": git_note,
        # LADDER ORDER IS LOAD-BEARING. The first draft tested `own` before `claims`,
        # so a fresh goal carrying 10 cited measurements but no outcome_note reported
        # "NO-CITED-MEASUREMENT" while the same record reported claims=10 — the verdict
        # contradicted its own payload. Claims are the subject of this check; the own-record
        # read is the cheaper prerequisite, not the headline. Caught by the Step 3.6 dogfood.
        "verdict": ("RE-MEASURE-BEFORE-EXECUTING" if stale
                    else "CITED-MEASUREMENT-PRESENT" if claims
                    else "CHECK-OWN-RECORD" if own
                    else "NO-CITED-MEASUREMENT"),
    }

    if a.json:
        print(json.dumps(result, indent=1))
    else:
        print(f"=== premise-supersession: {a.goal_id} ===")
        print(f"filed {filed or '?'} ({age_days if age_days is not None else '?'}d ago)"
              f" | verdict: {result['verdict']}")
        if own:
            print(f"\n(a) THE GOAL'S OWN RECORD carries {', '.join(sorted(own))} — READ IT FIRST.")
            print("    A pending status does not mean unstarted (guard-2803/guard-3761).")
            for k, v in own.items():
                print(f"    {k}: {str(v)[:400]}")
        else:
            print("\n(a) no outcome_note / progress_note on this goal.")
        if claims:
            print(f"\n(b) {len(claims)} CITED MEASUREMENT(S) — each is a premise with an expiry:")
            for c in claims:
                print(f"    - {c}")
            print("    Re-run each WITH A POSITIVE CONTROL and print the population beside")
            print("    the ratio (guard-1866, guard-2298). An empty result may be a broken probe.")
        else:
            print("\n(b) no cited measurement detected in title/description.")
        if commits:
            print(f"\n(c) {len(commits)} COMMIT(S) touched the named paths since filing —")
            print("    the defect may already be remediated (rb-1603):")
            for c in commits:
                print(f"    {c}")
        elif git_note:
            print(f"\n(c) git evidence lane: {git_note}")
        else:
            print("\n(c) no commits touched the named paths since filing.")
        print("\n(d) ASK WHAT THE METRIC EXCLUDES, not only what it counts, then classify")
        print("    the excluded bucket. Then two-token-set dedup over the residual")
        print("    population before filing anything (guard-1204, guard-2228).")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
