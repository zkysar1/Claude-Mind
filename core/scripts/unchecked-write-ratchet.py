#!/usr/bin/env python3
"""unchecked-write-ratchet.py — advisory drift ratchet over the unchecked-write audit.

Wired into /verify-learning as a post-test check (g-115-3882). Runs
`unchecked-write-audit.py`, compares its STRICT `unverified` count against the
last-recorded baseline in meta/audit-baselines.yaml, and reports:

  - unverified > baseline: WARN (regressed) — new unchecked write sites
  - unverified < baseline: OK (ratcheted)   — lower baseline persisted
  - unverified == baseline: OK (stable)
  - audit verdict "skipped": SKIPPED        — baseline UNTOUCHED (see below)

WHY `skipped` IS LOAD-BEARING, NOT AN EDGE CASE. The audit reports
verdict="skipped" when its wrapper-discovery or skill glob comes back empty
(a moved directory, a renamed layout, a broken checkout). In that state
`unverified` is 0 — indistinguishable from a codebase with zero unchecked
writes. A ratchet only ever SHRINKS, so seeding or ratcheting on a vacuous 0
would lock the baseline at 0 permanently and every future real regression
would read as "regressed from 0", or worse, the seed would silently declare
the drift solved. unchecked-write-audit.py names this goal explicitly in its
own source comment for exactly this reason (rb-245: an empty population is
`skipped`, never a confident verdict). So: on `skipped` this script reads the
baseline but writes NOTHING and returns 0.

WHICH MATCHER IS TRACKED. The audit emits BOTH a strict count and a
deliberately over-generous band (`verified_generous`, which also counts
re-read HINTS). This ratchet tracks the STRICT `unverified` so the tracked
number has exactly one definition over time. Do not switch it to the generous
band without re-seeding — the two are not comparable, and a silent switch
would render the whole history meaningless.

Exit codes:
  0  any outcome (advisory — never hard-fails /verify-learning)
  2  script error (audit failed to run or emit parseable JSON)

The exit-always-0 choice matches the sibling ratchets: existing drift is
historical and should be visible without blocking routine runs. Opt into
hard-gating with VERIFY_LEARNING_DRIFT_HARD_GATE=1.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from _paths import META_DIR  # type: ignore
from _fileops import locked_modify_yaml  # type: ignore

try:
    import yaml  # type: ignore  # noqa: F401  (locked_modify_yaml needs it present)
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr)
    sys.exit(2)

BASELINES_PATH = META_DIR / "audit-baselines.yaml"
KEY = "unchecked_writes"
AUDIT_PY = _HERE / "unchecked-write-audit.py"


def _run_audit():
    """Invoke the SHIPPED audit predicate and return its parsed summary.

    Deliberately shells out to the audit rather than re-implementing its
    classification: this goal's own scope says to seed from the number the
    shipped wrapper reports. A second in-process copy of the predicate is how
    two counts diverge and a baseline stops meaning anything.

    Uses sys.executable (never a bare "bash" argv[0] — guard-580) and runs the
    .py directly; the .sh wrapper only sources _paths/_platform and execs this
    same file, and the audit derives PROJECT_ROOT from __file__, so cwd is
    irrelevant to the result.
    """
    proc = subprocess.run(
        [sys.executable, str(AUDIT_PY)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "unchecked-write-audit.py exited %d: %s"
            % (proc.returncode, (proc.stderr or "").strip()[-400:])
        )
    out = proc.stdout or ""
    start = out.find("{")
    if start < 0:
        raise RuntimeError("audit emitted no JSON object")
    return json.loads(out[start:])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    args = ap.parse_args()

    try:
        audit = _run_audit()
    except Exception as e:
        print(f"ERROR: audit failed: {e}", file=sys.stderr)
        return 2

    current = int(audit.get("unverified") or 0)
    verified = int(audit.get("verified") or 0)
    # `population` is a DICT of sub-counts
    # ({write_wrappers, read_wrappers, skill_files, call_sites}), NOT an int —
    # int() on it raises. Carry the sub-counts into the breakdown so a future
    # regression can be attributed (did call_sites grow, or did wrapper
    # discovery shrink?) instead of leaving a bare delta to interpret.
    pop = audit.get("population")
    pop = pop if isinstance(pop, dict) else {}
    call_sites = int(pop.get("call_sites") or (current + verified))
    audit_verdict = str(audit.get("verdict") or "")

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # VACUOUS-RUN GUARD (see module docstring). Read the baseline for the
    # report, but do not seed, ratchet, or record history.
    if audit_verdict == "skipped":
        try:
            existing = yaml.safe_load(
                BASELINES_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            existing = {}
        prior = (existing.get(KEY) or {}).get("baseline")
        message = (
            "SKIPPED: the audit reported an empty population (verdict=skipped), "
            "so unverified=%d is vacuous. Baseline left UNTOUCHED at %s. "
            "Investigate wrapper discovery / the SKILL.md glob before trusting "
            "any number from this run." % (current, prior)
        )
        result = {"verdict": "skipped", "baseline": prior,
                  "current": {"unverified": current, "verified": verified,
                              "call_sites": call_sites, "population": pop},
                  "message": message}
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"[unchecked-write-ratchet] SKIPPED: {message}")
        return 0

    captured: dict = {}

    def _modify(baselines):
        # Read the baseline INSIDE the lock so the verdict + new_baseline
        # decision sees the same prior_baseline the write commits. Sibling
        # ratchets share this one file via the same lock () — without
        # the locked RMW, two writers each ratchet against an already-stale
        # baseline and the second reverts the first's foreign-key changes.
        if not isinstance(baselines, dict):
            baselines = {}
        entry = baselines.get(KEY) or {}
        prior_baseline = entry.get("baseline")

        if prior_baseline is None:
            verdict = "seeded"
            new_baseline = current
            message = (
                f"Seeded baseline at unverified={new_baseline} "
                f"(verified={verified} of {call_sites} call sites). Future runs "
                f"warn if it grows, ratchet if it shrinks."
            )
        elif current > prior_baseline:
            verdict = "regressed"
            new_baseline = prior_baseline  # never raise the baseline
            message = (
                f"WARN: unchecked write sites grew from baseline "
                f"{prior_baseline} to {current} (+{current - prior_baseline}). "
                f"Run `bash core/scripts/unchecked-write-audit.sh "
                f"--list-unverified 20` to inspect the new sites."
            )
        elif current < prior_baseline:
            verdict = "ratcheted"
            new_baseline = current
            message = (
                f"OK: unchecked write sites shrank from baseline "
                f"{prior_baseline} to {current} (-{prior_baseline - current}). "
                f"Baseline ratcheted down."
            )
        else:
            verdict = "stable"
            new_baseline = prior_baseline
            message = f"OK: unchecked write sites stable at baseline {current}."

        history = entry.get("history") or []
        history.append({
            "recorded_at": now_iso,
            "drift_total": current,
            "verdict": verdict,
            "breakdown": {
                "unverified": current,
                "verified": verified,
                "call_sites": call_sites,
                "write_wrappers": pop.get("write_wrappers"),
                "skill_files": pop.get("skill_files"),
            },
        })
        history = history[-50:]
        baselines[KEY] = {
            "baseline": new_baseline,
            # WHICH MATCHER THIS NUMBER COUNTS. A 5th key beyond the four in
            # audit-baselines.md, added deliberately: the audit emits BOTH a
            # strict count and an over-generous band, so `baseline: 464` alone
            # does not say what was measured. A future reader who switches the
            # ratchet to the generous band would silently render the whole
            # history incomparable, and nothing in the entry would show it.
            # Additive and safe — every consumer of this file reads named keys
            # via .get(), none validates the key set (verified across all 8
            # readers before adding this).
            "matcher": "strict_unverified",
            "last_recorded": now_iso,
            "last_verdict": verdict,
            "history": history,
        }
        captured["verdict"] = verdict
        captured["new_baseline"] = new_baseline
        captured["message"] = message
        return baselines

    try:
        locked_modify_yaml(BASELINES_PATH, _modify, initial={})
    except Exception as e:
        print(f"WARN: could not persist baseline to {BASELINES_PATH}: {e}",
              file=sys.stderr)
        # OVERWRITE, never setdefault. _modify runs INSIDE locked_modify_yaml
        # and populates `captured` before the write; if the write then fails
        # (disk full, conflict-retry exhausted, validation), setdefault is a
        # no-op and this would report the COMPUTED verdict as though it had
        # persisted. stderr is the only contradicting signal and no JSON
        # consumer reads it. A tool must not claim a write it did not make.
        computed = captured.get("verdict")
        captured["verdict"] = "error"
        captured["new_baseline"] = None
        captured["message"] = (
            f"baseline operation FAILED and nothing was persisted: {e}"
            + (f" (the computed verdict was '{computed}' — it did NOT "
               f"take effect)" if computed else ""))

    result = {
        "verdict": captured["verdict"],
        "baseline": captured["new_baseline"],
        "current": {"unverified": current, "verified": verified,
                    "call_sites": call_sites, "population": pop},
        "message": captured["message"],
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"[unchecked-write-ratchet] "
              f"{captured['verdict'].upper()}: {captured['message']}")

    if os.environ.get("VERIFY_LEARNING_DRIFT_HARD_GATE") == "1":
        return 1 if captured["verdict"] == "regressed" else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
