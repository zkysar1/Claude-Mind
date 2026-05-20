""" / -A6: quiescence-gate.py corruption-tolerant
parse for _wm_read_loop_state (line 113) and _known_blockers (line 261).

Paired with g-115-797 (bravo audit catalog row 6). Before this fix:
  - _wm_read_loop_state did `json.loads(raw)` then
    `except json.JSONDecodeError: return {}` — silently collapsing
    loop_state to empty under g-115-766 daemon aggregate-corruption mode.
  - _known_blockers did `json.loads(raw)` inside a broad
    `except Exception: pass` returning [] on any failure.

Downstream effect of the silent collapses:
  - loop_state empty → Phase -0.5 restoration thinks "first iteration"
    when in fact prior session counters existed → routine streak resets,
    cargo-cult-detector loses cross-session signal.
  - known_blockers empty → quiescence-gate believes "no blockers known"
    and may approve quiescence over a queue that actually has stale
    blockers — silent fail-open on the safety-critical sleep decision.

The fix inlines the consolidation-health.py::_tolerant_decode pattern
(landed via g-115-796) in both functions:
  1. lstrip body before decoding
  2. Use json.JSONDecoder().raw_decode(stripped) — recovers JSON prefix
     even when followed by trailing garbage (g-115-766 stale-reload window).
  3. JSONDecodeError → LOUD stderr diagnostic + sys.exit(1)
  4. Wrong aggregate type → stderr diagnostic + sys.exit(1)
  5. Empty body → return {} (loop_state) / [] (known_blockers) — valid
     empty state, not a corruption signal.
  6. RtError remains soft (return {} / []) — the daemon-unreachable
     pathway is qualitatively different from corruption. The script's
     callers (aspirations loop digest Phase -0.5e' verify-wake;
     direct cmd_check invocation) treat absent infrastructure as
     "skip quiescence decision," not "approve quiescence." No external
     shell wrapper exists, so an aggressive fatal-on-RtError would
     crash the orchestrator on transient daemon hiccups.

Behavior contract:
  - empty / whitespace body              -> {} (loop_state) / [] (known_blockers)
  - valid JSON dict (loop_state)         -> the dict
  - valid JSON list (known_blockers)     -> the list
  - valid prefix + trailing garbage      -> the parsed prefix (g-115-766 recovery)
  - unrecoverable body                   -> stderr + SystemExit(1)
  - wrong aggregate type                 -> stderr + SystemExit(1)
  - RtError                              -> stderr line + return {} / []

Run: py -3 -m pytest core/scripts/tests/test_quiescence_gate_tolerant_parse.py -v
"""
import contextlib
import importlib.util
import io
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_module():
    """Import quiescence-gate.py (hyphen in name blocks plain import)."""
    spec = importlib.util.spec_from_file_location(
        "quiescence_gate_module",
        SCRIPT_DIR / "quiescence-gate.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load_module()


# --- Stub helper -----------------------------------------------------------

class _StubRtErr(Exception):
    def __init__(self, body=""):
        self.body = body
        super().__init__(body)


def _stub_rt(*, raw=None, exc=None):
    """Build a stub _rt module the production code can substitute in for."""

    class _Stub:
        RtError = _StubRtErr

        @staticmethod
        def wm_read(slot, as_json):
            if exc is not None:
                raise exc
            return raw

    return _Stub


def _call(fn, *, raw=None, exc=None):
    """Call fn() with stub _rt; return (result, stderr_text). fn raises on exit."""
    orig = M._rt
    M._rt = _stub_rt(raw=raw, exc=exc)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            result = fn()
        return result, buf.getvalue()
    finally:
        M._rt = orig


def _call_expect_exit(fn, *, raw=None, exc=None):
    """Call fn() expecting SystemExit(1); return stderr text."""
    orig = M._rt
    M._rt = _stub_rt(raw=raw, exc=exc)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            fn()
    except SystemExit as e:
        assert e.code == 1, f"expected SystemExit(1), got SystemExit({e.code!r})"
        return buf.getvalue()
    finally:
        M._rt = orig
    raise AssertionError("expected SystemExit(1) but function returned")


# =============================================================================
# _wm_read_loop_state — dict slot
# =============================================================================


def test_loop_state_empty_body_returns_empty_dict_no_diagnostic():
    result, err = _call(M._wm_read_loop_state, raw="")
    assert result == {}, f"expected {{}}, got {result!r}"
    assert err == "", f"empty body must not emit diagnostic, got {err!r}"


def test_loop_state_whitespace_only_returns_empty_dict_no_diagnostic():
    result, err = _call(M._wm_read_loop_state, raw="   \n\t  \n")
    assert result == {}, f"expected {{}}, got {result!r}"
    assert err == "", f"whitespace body must not emit diagnostic, got {err!r}"


def test_loop_state_valid_empty_dict():
    result, err = _call(M._wm_read_loop_state, raw="{}")
    assert result == {}, f"expected {{}}, got {result!r}"
    assert err == "", f"valid empty dict must not emit diagnostic, got {err!r}"


def test_loop_state_null_literal_returns_empty_dict_no_diagnostic():
    """wm-set.sh writes the literal string 'null' when a slot is set to JSON
    null (e.g., `echo 'null' | wm-set.sh loop_state`). The legacy code
    treated this as silent empty-state via `isinstance(None, dict) is False
    -> return {}`. The tolerant-decode rewrite must preserve that shape —
    the initial commit DID regress this case (treating None as wrong-
    aggregate fatal). This test pins the corrected behavior."""
    result, err = _call(M._wm_read_loop_state, raw="null")
    assert result == {}, f"'null' literal must return {{}}, got {result!r}"
    assert err == "", f"'null' literal must not emit diagnostic, got {err!r}"


def test_loop_state_valid_populated_dict():
    payload = (
        '{"goals_completed_this_session": 7, '
        '"signals": {"routine_streak_global": 3}, '
        '"routine_streaks": {"g-115-22": 2}}'
    )
    result, err = _call(M._wm_read_loop_state, raw=payload)
    assert isinstance(result, dict) and result.get("goals_completed_this_session") == 7
    assert result["signals"]["routine_streak_global"] == 3
    assert err == "", f"valid dict must not emit diagnostic, got {err!r}"


def test_loop_state_g_115_766_recovery_prefix_plus_trailing_garbage():
    """Canonical  corruption: valid JSON dict followed by stale-code fragment.
    raw_decode stops at the first valid value."""
    payload = '{"goals_completed_this_session": 5}\n<<<STALE RELOAD>>>{"part'
    result, err = _call(M._wm_read_loop_state, raw=payload)
    assert result == {"goals_completed_this_session": 5}, (
        f"raw_decode must recover the valid prefix; got {result!r}"
    )
    assert err == "", f"successful recovery must not emit diagnostic, got {err!r}"


def test_loop_state_unrecoverable_body_is_fatal():
    """Pre-fix code returned {} silently. Post-fix exits 1 with diagnostic."""
    err = _call_expect_exit(M._wm_read_loop_state, raw="not json $$$ \x00 garbage")
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected 1 diagnostic line, got {lines!r}"
    assert lines[0].startswith("quiescence-gate: loop_state "), (
        f"diagnostic must carry symmetric prefix, got {lines[0]!r}"
    )
    assert "JSONDecodeError" in lines[0], (
        f"diagnostic must name the failure class, got {lines[0]!r}"
    )


def test_loop_state_diagnostic_truncates_and_escapes_long_body():
    garbage = ("X\n" * 200)  # 400 chars, 200 newlines, never valid JSON
    err = _call_expect_exit(M._wm_read_loop_state, raw=garbage)
    assert "\\n" in err, "newlines in body prefix must be escaped to \\n"
    assert err.strip().count("\n") == 0, (
        f"diagnostic must be a single line (no raw newlines), got {err!r}"
    )


def test_loop_state_non_dict_aggregate_list_is_fatal():
    """A valid JSON list is wrong aggregate shape for the dict slot."""
    err = _call_expect_exit(M._wm_read_loop_state, raw="[1, 2, 3]")
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected 1 diagnostic line, got {lines!r}"
    assert "non-dict aggregate" in lines[0] and "type=list" in lines[0], (
        f"diagnostic must name wrong aggregate type, got {lines[0]!r}"
    )


def test_loop_state_non_dict_aggregate_int_is_fatal():
    err = _call_expect_exit(M._wm_read_loop_state, raw="42")
    assert "type=int" in err, f"diagnostic must name int type, got {err!r}"


def test_loop_state_non_dict_aggregate_string_is_fatal():
    err = _call_expect_exit(M._wm_read_loop_state, raw='"just a string"')
    assert "type=str" in err, f"diagnostic must name str type, got {err!r}"


def test_loop_state_rterror_returns_empty_dict_with_log_line():
    """RtError is soft (no fatal exit) — matches original intent + the
    fact that no shell wrapper provides fail-open semantics for this script."""
    result, err = _call(M._wm_read_loop_state, exc=_StubRtErr("daemon unreachable"))
    assert result == {}, f"RtError must return {{}} softly, got {result!r}"
    assert "[quiescence-gate] wm read failed" in err, (
        f"RtError must log a stderr line, got {err!r}"
    )


# =============================================================================
# _known_blockers — list slot
# =============================================================================


def test_known_blockers_empty_body_returns_empty_list_no_diagnostic():
    result, err = _call(M._known_blockers, raw="")
    assert result == [], f"expected [], got {result!r}"
    assert err == "", f"empty body must not emit diagnostic, got {err!r}"


def test_known_blockers_whitespace_only_returns_empty_list_no_diagnostic():
    result, err = _call(M._known_blockers, raw="   \n\t  \n")
    assert result == [], f"expected [], got {result!r}"
    assert err == "", f"whitespace body must not emit diagnostic, got {err!r}"


def test_known_blockers_valid_empty_list():
    result, err = _call(M._known_blockers, raw="[]")
    assert result == [], f"expected [], got {result!r}"
    assert err == "", f"valid empty list must not emit diagnostic, got {err!r}"


def test_known_blockers_null_literal_returns_empty_list_no_diagnostic():
    """Same regression-pin as the loop_state sibling — wm-set.sh emits
    literal 'null' when a slot is null. Legacy code returned [] via the
    bare-except path; tolerant-decode rewrite must preserve that shape."""
    result, err = _call(M._known_blockers, raw="null")
    assert result == [], f"'null' literal must return [], got {result!r}"
    assert err == "", f"'null' literal must not emit diagnostic, got {err!r}"


def test_known_blockers_valid_populated_list():
    payload = (
        '[{"blocker_id": "b-001", "affected_skills": ["efs-ssh"]},'
        ' {"blocker_id": "b-002"}]'
    )
    result, err = _call(M._known_blockers, raw=payload)
    assert isinstance(result, list) and len(result) == 2
    assert result[0]["blocker_id"] == "b-001"
    assert err == "", f"valid list must not emit diagnostic, got {err!r}"


def test_known_blockers_g_115_766_recovery_prefix_plus_trailing_garbage():
    payload = '[{"blocker_id": "b-001"}]\n<<<STALE RELOAD>>>{"partial":'
    result, err = _call(M._known_blockers, raw=payload)
    assert result == [{"blocker_id": "b-001"}], (
        f"raw_decode must recover the valid prefix; got {result!r}"
    )
    assert err == "", f"successful recovery must not emit diagnostic, got {err!r}"


def test_known_blockers_unrecoverable_body_is_fatal():
    """Pre-fix code returned [] silently (bare except Exception).
    Post-fix exits 1 on JSONDecodeError."""
    err = _call_expect_exit(M._known_blockers, raw="not json $$$ \x00 garbage")
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected 1 diagnostic line, got {lines!r}"
    assert lines[0].startswith("quiescence-gate: known_blockers "), (
        f"diagnostic must carry symmetric prefix, got {lines[0]!r}"
    )
    assert "JSONDecodeError" in lines[0], (
        f"diagnostic must name the failure class, got {lines[0]!r}"
    )


def test_known_blockers_non_list_aggregate_dict_is_fatal():
    err = _call_expect_exit(M._known_blockers, raw='{"version": 2}')
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected 1 diagnostic line, got {lines!r}"
    assert "non-list aggregate" in lines[0] and "type=dict" in lines[0], (
        f"diagnostic must name wrong aggregate type, got {lines[0]!r}"
    )


def test_known_blockers_non_list_aggregate_int_is_fatal():
    err = _call_expect_exit(M._known_blockers, raw="42")
    assert "type=int" in err, f"diagnostic must name int type, got {err!r}"


def test_known_blockers_rterror_returns_empty_list_with_log_line():
    """RtError is soft (no fatal exit) — matches the new explicit RtError
    handler that replaces the prior overly-broad `except Exception: pass`.
    The script has no shell wrapper providing fail-open semantics, so
    transient daemon hiccups should not crash quiescence-gate."""
    result, err = _call(M._known_blockers, exc=_StubRtErr("daemon unreachable"))
    assert result == [], f"RtError must return [] softly, got {result!r}"
    assert "[quiescence-gate] wm read failed" in err, (
        f"RtError must log a stderr line, got {err!r}"
    )
