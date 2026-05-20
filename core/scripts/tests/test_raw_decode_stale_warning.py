"""test_raw_decode_stale_warning.py —  regression test.

Verifies the raw_decode fix in aspirations-update-goal.sh and
aspirations-claim.sh handles stale-daemon stderr leakage appended to
the JSON body. Per zeta investigation (g-115-769): rt_call uses
2>&1 to merge stderr into stdout; when the daemon emits a stale
warning like "[runtime] WARNING: daemon is running stale code...",
json.loads(COMBINED) chokes with "Extra data: line N column M".

The fix replaces json.loads(...) with json.JSONDecoder().raw_decode(...)
which consumes only the first valid JSON document and exposes any
trailing residual (re-emitted to stderr for ops visibility).

Test strategy: mirror the heredoc Python in isolation (text it against
synthetic COMBINED inputs that match the production failure shape).
This is the same pattern as test_post_state_update_gate_cooldown.py —
mirror the embedded Python in pure Python so we can drive contract
verification without standing up the full daemon + bash wrapper chain.

Cross-references:
  - g-115-769 (Investigate) — zeta confirmed mechanism end-to-end.
  - g-115-878 (this Apply) — alpha applied raw_decode + residual stderr.
  - core/scripts/aspirations-update-goal.sh:145, :180 (fix sites)
  - core/scripts/aspirations-claim.sh:89, :116 (fix sites)
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr


def parse_response(src: str) -> tuple[dict, str]:
    """Mirror of the heredoc Python in fixed wrappers.

    Returns (parsed_json_dict, residual_string). Re-raises the same
    json.JSONDecodeError the wrapper would on malformed input.
    """
    resp, idx = json.JSONDecoder().raw_decode(src)
    residual = src[idx:].strip()
    return resp, residual


class TestRawDecodeStaleWarning(unittest.TestCase):
    """Six-case contract for the raw_decode fix."""

    def test_clean_json_no_residual(self):
        """JSON only, no warning appended — no residual emitted."""
        src = '{"goal": {"id": "g-001-01", "status": "completed"}}'
        resp, residual = parse_response(src)
        self.assertEqual(resp["goal"]["id"], "g-001-01")
        self.assertEqual(residual, "")

    def test_json_then_stale_warning_no_throw(self):
        """JSON followed by stale-daemon stderr-leakage — parses cleanly."""
        warning = "[runtime] WARNING: daemon is running stale code (heartbeat 120s behind)"
        src = '{"goal": {"id": "g-001-01", "status": "completed"}}\n' + warning
        resp, residual = parse_response(src)
        self.assertEqual(resp["goal"]["id"], "g-001-01")
        self.assertEqual(residual, warning)

    def test_json_then_multiline_warning(self):
        """Multi-line residual after JSON — preserved fully (modulo strip)."""
        warning = "[runtime] WARNING: daemon stale\n[runtime] retry in 5s"
        src = '{"goal": null, "warnings": []}\n\n' + warning
        resp, residual = parse_response(src)
        self.assertIsNone(resp["goal"])
        self.assertEqual(residual, warning)

    def test_json_with_warnings_field_passes_through(self):
        """JSON's own 'warnings' field is independent of stderr residual."""
        warning_text = "[runtime] WARNING: stale"
        src = (
            '{"goal": {"id": "g-x"}, "warnings": ["server-internal warning A"]}\n'
            + warning_text
        )
        resp, residual = parse_response(src)
        self.assertEqual(resp["warnings"], ["server-internal warning A"])
        self.assertEqual(residual, warning_text)

    def test_pre_fix_json_loads_would_throw(self):
        """Sanity check: confirm json.loads DOES throw on the same input.

        Pins the regression: if json.loads stops throwing here (e.g., Python
        changes its strict mode), the raw_decode fix becomes unnecessary --
        the test would still pass but the regression has shifted shape.
        """
        warning = "[runtime] WARNING: daemon is running stale code"
        src = '{"goal": {"id": "g-001-01"}}\n' + warning
        with self.assertRaises(json.JSONDecodeError):
            json.loads(src)

    def test_malformed_json_still_throws(self):
        """raw_decode does NOT silently swallow malformed JSON."""
        with self.assertRaises(json.JSONDecodeError):
            parse_response('{"goal": invalid_token}')
        with self.assertRaises(json.JSONDecodeError):
            parse_response("not even json at all")


if __name__ == "__main__":
    unittest.main()
