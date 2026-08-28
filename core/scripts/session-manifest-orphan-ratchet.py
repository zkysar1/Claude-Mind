#!/usr/bin/env python3
"""session-manifest-orphan-ratchet.py — advisory orphan-count check with baseline ratchet.

Wired into /verify-learning as a post-test check. Runs session-desync-check.sh,
counts files with id == "orphan_file" in the warnings list, compares against the
last-recorded baseline in meta/audit-baselines.yaml under key
"session_manifest_orphans", and reports:

  - count > baseline: WARN (regressed) — new orphan drift since baseline
  - count < baseline: OK (ratcheted) — lower baseline persisted
  - count == baseline: OK (stable)

The ratchet ensures orphan count can only shrink over time. The baseline is
auto-seeded on first run.

Exit codes:
  0  any outcome (advisory — never hard-fails /verify-learning)
  1  ONLY when verdict == "regressed" AND VERIFY_LEARNING_ORPHAN_HARD_GATE=1
  2  script error (subprocess failed, unwriteable baseline file, etc.)

The exit-always-0 default mirrors learning-routing-ratchet.py: visibility, not
gating. Operators opt into hard-gating via env var when they want regression
to fail the verify-learning suite.
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
    import yaml  # type: ignore
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr)
    sys.exit(2)


BASELINES_PATH = META_DIR / "audit-baselines.yaml"
KEY = "session_manifest_orphans"
SNAPSHOT_PY = _HERE / "session_snapshot.py"


def _count_orphans():
    """Subprocess session_snapshot.py via sys.executable (matches the pattern
    in session_desync_check.py) and read orphans[] directly from the snapshot.

    Returns a dict: {"total": int, "files": [str, ...]} on success, or raises
    on subprocess / parse failure. The snapshot enumerates files in
    <agent>/session/ that are not registered in core/config/session-manifest.yaml.
    """
    proc = subprocess.run(
        [sys.executable, str(SNAPSHOT_PY), "--output", "json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"session_snapshot.py exited {proc.returncode}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    snapshot = json.loads(proc.stdout)
    orphans = snapshot.get("orphans") or []
    files = [o for o in orphans if isinstance(o, str)]
    return {"total": len(files), "files": files}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    args = ap.parse_args()

    try:
        current = _count_orphans()
    except Exception as e:
        print(f"ERROR: orphan count failed: {e}", file=sys.stderr)
        return 2

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    captured: dict = {}

    def _modify(baselines):
        # : read baseline INSIDE the lock so verdict + ratchet decision
        # use the same prior_baseline that the write commits. Without this, two
        # concurrent ratchet runs could each compute "ratcheted" against an
        # already-stale baseline and the second writer would clobber the first.
        # Sibling writers (learning-routing-ratchet) share this lock via the
        # same file path — see core/config/conventions/audit-baselines.md.
        if not isinstance(baselines, dict):
            baselines = {}
        entry = baselines.get(KEY) or {}
        prior_baseline = entry.get("baseline")

        if prior_baseline is None:
            verdict = "seeded"
            new_baseline = current["total"]
            message = (
                f"Seeded baseline at orphans={new_baseline}. Future runs will "
                f"warn if count exceeds this, ratchet if it shrinks."
            )
        elif current["total"] > prior_baseline:
            verdict = "regressed"
            new_baseline = prior_baseline
            delta = current["total"] - prior_baseline
            message = (
                f"WARN: orphan count increased from baseline {prior_baseline} to "
                f"{current['total']} (+{delta}). New orphans: "
                f"{', '.join(current['files'][-delta:]) if delta <= 5 else f'{delta} new files'}. "
                f"Either register the file in core/config/session-manifest.yaml or "
                f"file a Maintain goal to remove it."
            )
        elif current["total"] < prior_baseline:
            verdict = "ratcheted"
            new_baseline = current["total"]
            delta = prior_baseline - current["total"]
            message = (
                f"OK: orphan count shrank from baseline {prior_baseline} to "
                f"{current['total']} (-{delta}). Baseline ratcheted down."
            )
        else:
            verdict = "stable"
            new_baseline = prior_baseline
            message = f"OK: orphan count stable at baseline {current['total']}."

        history = entry.get("history") or []
        history.append({
            "recorded_at": now_iso,
            "drift_total": current["total"],
            "verdict": verdict,
            "breakdown": {
                "orphan_files": current["total"],
            },
        })
        history = history[-50:]
        baselines[KEY] = {
            "baseline": new_baseline,
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

    verdict = captured["verdict"]
    new_baseline = captured["new_baseline"]
    message = captured["message"]

    result = {
        "verdict": verdict,
        "baseline": new_baseline,
        "current": current["total"],
        "files": current["files"],
        "message": message,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"[session-manifest-orphan-ratchet] {verdict.upper()}: {message}")

    if os.environ.get("VERIFY_LEARNING_ORPHAN_HARD_GATE") == "1":
        return 1 if verdict == "regressed" else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
