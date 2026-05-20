"""test_audit_baselines_race.py — concurrency smoke test for .

Verifies that concurrent writers targeting DIFFERENT keys of
meta/audit-baselines.yaml (session-manifest-orphan-ratchet.py and
learning-routing-ratchet.py both write distinct keys) do not
sibling-stomp each other after the locked_modify_yaml refactor.

Before g-115-287, both ratchet scripts performed unlocked
read-modify-write: each loaded the file independently, mutated its own
key, and wrote the mutated copy back. Two concurrent runs would both
read the same baseline, each modify their own key, and the second
writer would clobber the first writer's foreign-key changes.

The test seeds an empty audit-baselines.yaml, spawns N=8 concurrent
subprocesses (4 each of orphan-ratchet and routing-ratchet), and
asserts that BOTH keys survive in the final file.

The test backs up the live audit-baselines.yaml, seeds an empty one,
runs the race, asserts, then restores the backup.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

from _paths import META_DIR  # type: ignore

BASELINES_PATH = META_DIR / "audit-baselines.yaml"
BACKUP_PATH = BASELINES_PATH.with_suffix(f".yaml.race-test-backup.{os.getpid()}")
N_RACERS_PER_SCRIPT = 4
ORPHAN_PY = CORE_SCRIPTS / "session-manifest-orphan-ratchet.py"
ROUTING_PY = CORE_SCRIPTS / "learning-routing-ratchet.py"


def _run_script(spec: tuple[str, str]) -> subprocess.CompletedProcess:
    """spec is (kind, label). kind ∈ {"orphan", "routing"}."""
    kind, label = spec
    script = ORPHAN_PY if kind == "orphan" else ROUTING_PY
    env = os.environ.copy()
    env["MIND_AGENT"] = "alpha"  # any valid agent — paths come from local-paths.conf
    return subprocess.run(
        [sys.executable, str(script), "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def main() -> int:
    if BASELINES_PATH.exists():
        BACKUP_PATH.write_bytes(BASELINES_PATH.read_bytes())
        print(f"backed up live audit-baselines.yaml to {BACKUP_PATH.name}")

    # Seed an empty baseline file so both racers see the same starting state.
    BASELINES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BASELINES_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump({}, f)

    try:
        specs = []
        for i in range(N_RACERS_PER_SCRIPT):
            specs.append(("orphan", f"orphan-{i}"))
            specs.append(("routing", f"routing-{i}"))

        start = time.time()
        with ThreadPoolExecutor(max_workers=len(specs)) as ex:
            results = list(ex.map(_run_script, specs))
        elapsed = time.time() - start
        print(f"N={len(specs)} racers completed in {elapsed:.2f}s")

        # Print stderr from each — if any subprocess errored, surface it.
        for spec, r in zip(specs, results):
            err = (r.stderr or "").strip()
            if r.returncode != 0 or err:
                print(f"  {spec[1]}: rc={r.returncode} stderr={err!r}")

        failed = [(s, r) for s, r in zip(specs, results) if r.returncode != 0]
        if failed:
            print(f"TEST FAIL: {len(failed)} subprocess(es) errored:", file=sys.stderr)
            return 1

        with open(BASELINES_PATH, "r", encoding="utf-8") as f:
            final = yaml.safe_load(f) or {}

        missing = []
        if "session_manifest_orphans" not in final:
            missing.append("session_manifest_orphans")
        if "learning_routing_drift" not in final:
            missing.append("learning_routing_drift")

        if missing:
            print(f"TEST FAIL: {len(missing)}/2 sibling key(s) missing:", file=sys.stderr)
            for k in missing:
                print(f"  {k}", file=sys.stderr)
            print(f"  final keys: {list(final.keys())}", file=sys.stderr)
            return 1

        # Both keys present — race fix works. Each entry should also have a
        # populated history list (every racer adds an entry under its key).
        orphan_hist = (final.get("session_manifest_orphans") or {}).get("history") or []
        routing_hist = (final.get("learning_routing_drift") or {}).get("history") or []

        # Each script ran 4 times; the LAST writer's history wins for ITS key.
        # Without the lock, sibling-stomp meant the WHOLE file was overwritten,
        # erasing the OTHER key's history entirely. With the lock, each key's
        # history accumulates independently (4 entries per key).
        if len(orphan_hist) < 1 or len(routing_hist) < 1:
            print(
                f"TEST FAIL: history lists too short — orphan={len(orphan_hist)} "
                f"routing={len(routing_hist)} (expected ≥1 each)",
                file=sys.stderr,
            )
            return 1

        print(
            f"TEST PASS: both keys survived {N_RACERS_PER_SCRIPT * 2} concurrent "
            f"writers (orphan history={len(orphan_hist)}, "
            f"routing history={len(routing_hist)})"
        )
        return 0
    finally:
        if BACKUP_PATH.exists():
            BASELINES_PATH.write_bytes(BACKUP_PATH.read_bytes())
            BACKUP_PATH.unlink()
            print("restored live audit-baselines.yaml")


if __name__ == "__main__":
    sys.exit(main())
