"""test_verified_wm_set.py — 6 regression test.

verified-wm-set.sh hardens the WM-write mechanism for cadence stamps: it
writes a value, reads it back, asserts JSON-canonical equality, and retries
ONCE before failing loud (exit 1). It exists because the strategic-scan S5
`last_strategic_scan` stamp used a bare `echo | wm-set.sh` that silently
dropped a write 2026-06-13 (wm-set returned exit-0 but the value did not
persist — the load-bearing g-240-60 / g-001-189 silent-drop class).

The behavioral tests stub wm-set.sh / wm-read.sh inside a sandbox so the
write → read-back → assert → retry control flow is exercised WITHOUT the
daemon, including the two cases that motivated the goal:
  - persistent silent drop  → exit 1, loud diagnostic (never silent)
  - transient drop then OK   → retry persists, exit 0

The structural test pins the routing regression: strategic-scan S5 must
route `last_strategic_scan` through verified-wm-set.sh (exactly one writer),
so a revert to the bare form is caught.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
VERIFIED_WM_SET = CORE_SCRIPTS / "verified-wm-set.sh"
SHIM_DIR = CORE_SCRIPTS / ".python-shim"
STRATEGIC_SCAN_SKILL = (
    PROJECT_ROOT / ".claude" / "skills" / "aspirations-strategic-scan" / "SKILL.md"
)
VERIFY_LEARNING_SKILL = (
    PROJECT_ROOT / ".claude" / "skills" / "verify-learning" / "SKILL.md"
)


def _bash() -> str:
    return shutil.which("bash") or "bash"


class TestVerifiedWmSetBehavior(unittest.TestCase):
    """Stub-backed behavioral tests of the write→readback→assert→retry flow."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vwmset-test-")
        self.d = Path(self.tmp.name)
        # Copy the real script in so its SCRIPT_DIR resolves to our stubs.
        shutil.copy(VERIFIED_WM_SET, self.d / "verified-wm-set.sh")
        self.state_prefix = (self.d / "wm_state").as_posix()
        self.drop_marker = (self.d / "drop_count").as_posix()
        # Stub _paths.sh: no-op except guaranteeing python3 resolves on Windows
        # (the real _paths.sh sets up the shim PATH; here we mimic only that).
        (self.d / "_paths.sh").write_text(
            'command -v python3 >/dev/null 2>&1 || '
            'export PATH="%s:$PATH"\n' % SHIM_DIR.as_posix()
        )
        # Stub wm-set.sh: persist stdin under the slot UNLESS a drop is pending
        # (drop = exit-0 without persisting, i.e. the silent-drop failure mode).
        (self.d / "wm-set.sh").write_text(textwrap.dedent(f'''\
            #!/usr/bin/env bash
            slot="$1"; val="$(cat)"
            n=0; [ -f "{self.drop_marker}" ] && n=$(cat "{self.drop_marker}")
            if [ "$n" -gt 0 ]; then
                echo $((n-1)) > "{self.drop_marker}"   # consume one drop, do NOT persist
                exit 0
            fi
            printf '%s' "$val" > "{self.state_prefix}.$slot"
            exit 0
        '''))
        # Stub wm-read.sh: print stored value or literal "null".
        (self.d / "wm-read.sh").write_text(textwrap.dedent(f'''\
            #!/usr/bin/env bash
            slot="$1"
            f="{self.state_prefix}.$slot"
            if [ -f "$f" ]; then cat "$f"; else echo "null"; fi
        '''))

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, slot, value, drop=0):
        if drop:
            Path(self.drop_marker).write_text(str(drop))
        return subprocess.run(
            [_bash(), str(self.d / "verified-wm-set.sh"), slot],
            input=value, capture_output=True, text=True,
        )

    def test_success_string(self):
        p = self._run("last_strategic_scan", '"2026-06-13T16:37:51"')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("landed (verified)", p.stdout)

    def test_success_object_canonical_compare(self):
        # Object value round-trips; canonical compare (not byte compare) passes.
        p = self._run("s", '{"b":2,"a":1}')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_persistent_silent_drop_fails_loud(self):
        # Drop BOTH the write and the retry write → never persists → exit 1.
        p = self._run("s", '"v"', drop=2)
        self.assertEqual(p.returncode, 1)
        self.assertIn("did not persist", p.stderr)

    def test_transient_drop_retry_succeeds(self):
        # Drop only the FIRST write; the retry persists → exit 0 on retry.
        p = self._run("s", '"v"', drop=1)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("on retry", p.stdout)

    def test_missing_slot_arg_exits_1(self):
        p = subprocess.run(
            [_bash(), str(self.d / "verified-wm-set.sh")],
            input='"v"', capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 1)
        self.assertIn("slot name required", p.stderr)

    def test_empty_value_exits_1(self):
        p = self._run("s", "")
        self.assertEqual(p.returncode, 1)
        self.assertIn("empty value", p.stderr)


class TestCanonicalCompareContract(unittest.TestCase):
    """Pin the JSON-canonical equality contract the wrapper relies on."""

    @staticmethod
    def _match(a: str, b: str) -> str:
        # Mirror of verified-wm-set.sh _values_match (json-canonical w/ fallback).
        try:
            return "1" if json.loads(a) == json.loads(b) else "0"
        except Exception:
            return "1" if a.strip() == b.strip() else "0"

    def test_equal_string(self):
        self.assertEqual(self._match('"2026-06-13T16:37:51"',
                                     '"2026-06-13T16:37:51"'), "1")

    def test_equal_object_whitespace_and_keyorder(self):
        self.assertEqual(self._match('{"b":2,"a":1}', '{"a": 1, "b": 2}'), "1")

    def test_mismatch_value(self):
        self.assertEqual(self._match('"new"', '"old"'), "0")

    def test_null_readback_is_mismatch(self):
        # A silent drop leaves the slot null; that must NOT match a real value.
        self.assertEqual(self._match('"2026-06-13T16:37:51"', 'null'), "0")


class TestStrategicScanRoutingRegression(unittest.TestCase):
    """Pin the goal's structural invariant: S5 routes through the wrapper."""

    def test_wrapper_exists(self):
        self.assertTrue(VERIFIED_WM_SET.is_file())

    def test_wrapper_has_load_bearing_elements(self):
        body = VERIFIED_WM_SET.read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", body)
        self.assertIn("wm-read.sh", body)         # read-back
        self.assertIn("_values_match", body)       # assert
        self.assertIn("retry", body.lower())       # retry-once
        self.assertIn("exit 1", body)              # fail loud

    def test_s5_routes_last_strategic_scan_through_verified_wrapper(self):
        body = STRATEGIC_SCAN_SKILL.read_text(encoding="utf-8")
        verified = body.count("verified-wm-set.sh last_strategic_scan")
        self.assertEqual(verified, 1,
                         "S5 must route last_strategic_scan through "
                         "verified-wm-set.sh exactly once")
        # No bare `wm-set.sh last_strategic_scan` call survives as a SECOND
        # writer: the only occurrence of that substring is inside the verified
        # call itself (single-writer rule, guard-155).
        bare = body.count("wm-set.sh last_strategic_scan")
        self.assertEqual(bare, 1, "single-writer: only the verified call line "
                                  "may contain the slot-write substring")

    def test_verify_learning_check_updated(self):
        body = VERIFY_LEARNING_SKILL.read_text(encoding="utf-8")
        self.assertIn("verified-wm-set.sh last_strategic_scan", body,
                      "verify-learning SS2 must assert the verified routing")


if __name__ == "__main__":
    unittest.main()
