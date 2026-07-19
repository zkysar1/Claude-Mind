"""Pytest visibility wrapper for the bash suite test-infra-streak-dedup.sh.

infra-streak-notify.sh's episode-keyed dedup + re_escalation cadence + SA_RC
crash discrimination are covered by the hermetic bash suite
core/scripts/tests/test-infra-streak-dedup.sh (7 dedup cases + 2 SA_RC cases,
g-249-28/29/31). Bash tests are invisible to BOTH aggregators — `pytest
core/scripts/tests` collects only test_*.py, and run-invisible-suites.sh
enumerates only main()-style test_*.py files — so without this wrapper the
suite's redness is silent (g-115-2637; same silent-red class as g-115-2349).
"""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BASH_SUITE = REPO_ROOT / "core" / "scripts" / "tests" / "test-infra-streak-dedup.sh"


def test_infra_streak_dedup_bash_suite():
    assert BASH_SUITE.is_file(), f"bash suite missing: {BASH_SUITE}"
    env = os.environ.copy()
    # guard-955: no test may reach the own-cloud store. The bash suite is
    # hermetic (tmpdir sent-file + --alert-file seam + python3 shim), but the
    # pin costs nothing and fences any future drift.
    env["STORAGE_BACKEND"] = "local"
    env.setdefault("MIND_AGENT", "testagent")
    proc = subprocess.run(
        ["bash", str(BASH_SUITE)],
        capture_output=True,
        text=True,
        timeout=240,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, (
        f"test-infra-streak-dedup.sh failed rc={proc.returncode}\n"
        f"stdout tail:\n{proc.stdout[-2000:]}\n"
        f"stderr tail:\n{proc.stderr[-2000:]}"
    )
    assert "TEST PASS" in proc.stdout, f"no TEST PASS marker in output:\n{proc.stdout[-500:]}"
