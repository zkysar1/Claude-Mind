""" / -A1: defer-recheck.py corruption-tolerant parse for
_read_goals (line 627 in audit; pattern inlined into _read_goals).

Paired with g-115-797 (bravo audit catalog row 6). Before this fix, the
helper `_read_goals` did `json.loads(out)` then `except json.JSONDecodeError:
return []` — silently collapsing the goals list to 0 under the g-115-766
daemon aggregate-corruption mode (valid JSON prefix + trailing garbage
written during a stale-code reload window). Downstream effect: 0 goals to
recheck, dependency-blocked defers stay frozen ~5 days (canonical incident:
g-115-71 cited g-115-87 dependency which had been completed 3 days earlier;
manual clear unblocked it).

The fix applies the consolidation-health.py::_tolerant_decode pattern with
two A1-specific adaptations:
  1. Accepts BOTH dict-with-aspirations-key AND bare-list aggregates (the
     daemon may emit either; the existing call site extracts via
     `data.get("aspirations") if isinstance(data, dict) else data`).
  2. The tolerant-decode is INLINED inside _read_goals rather than
     extracted to a separate `_tolerant_decode` helper. Sibling A2
     (blocker-recheck) and A3 (precondition-defer-recheck) refactored to
     a helper; A1 kept the inline form. The 5-item decode CONTRACT is
     identical; only the call-shape differs. g-115-949 tracks the
     downstream cleanup to extract a shared `_tolerant_decode` primitive
     to `core/scripts/_rt.py` for all four sites.

A1 RtError handling — corrected to guard-383 fatal-aggregator (g-115-948
resolved via g-115-981): `_read_goals` is called once for "world" and once
for "agent" then merged in main() (`_read_goals("world") + _read_goals(
"agent")`). Per guard-383, the N>=2-source aggregator pattern mandates
per-source error be FATAL on RtError, not silent `return []` — a silent
[] from one source would poison the merged set with a complete-looking
lie (cleared defers based on a half-view). A1 now joins siblings A2
(blocker-recheck), A3 (precondition-defer-recheck), and A4 (parent-
supersession-sweep) in exiting fatally on RtError; the defer-recheck.sh
wrapper swallows non-zero exit so a transient daemon outage leaves the
defers untouched for the next sweep — no regression on daemon-up. Test
case 10 (`test_rterror_is_fatal_guard_383`) pins the corrected behavior.

Behavior contract (g-115-939 goal description, verbatim):
  1. lstrip body before decoding
  2. raw_decode(stripped) — stops at first valid JSON value, recovers
     from prefix + garbage
  3. JSONDecodeError -> LOUD stderr diagnostic + sys.exit(1)
  4. Wrong aggregate type (not dict/not list) -> stderr + exit 1
  5. Empty body = valid empty state (return [])
Plus the A1-inlined post-decode goals-extraction:
  - dict aggregate: walk `data["aspirations"]` then each asp's `goals`
  - list aggregate: walk `data` (already a list of asps) then each asp's `goals`
  - Each yielded goal gets `_source` + `_aspiration_id` stamps

A1 RtError-fatal regression pin (post-g-115-981):
  - _rt.RtError raised -> _read_goals exits 1 with stderr diagnostic.
    Mirrors A2/A3/A4 exemplars; matches the canonical guard-383 pattern
    in parent-supersession-sweep.py.

Run: py -3 -m pytest core/scripts/tests/test_defer_recheck_tolerant_parse.py -v
     OR: py -3 core/scripts/tests/test_defer_recheck_tolerant_parse.py
"""
import contextlib
import importlib.util
import io
import sys
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_module():
    """Import defer-recheck.py (hyphen in name blocks plain import)."""
    spec = importlib.util.spec_from_file_location(
        "defer_recheck_module",
        SCRIPT_DIR / "defer-recheck.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load_module()


# ── Helpers for stubbing _rt.aspirations_read ─────────────────────────────

class _StubRtBase:
    """Base stub for _rt — subclasses override aspirations_read.

    Pure-parse helpers (tolerant_decode_list, tolerant_decode_aggregate)
    delegate to the real _rt module since they don't touch the daemon —
    only aspirations_read needs to be stubbed for daemon isolation.
    Wired here after g-115-949 extracted the decoders to _rt.py.
    """

    class RtError(Exception):
        def __init__(self, body=""):
            self.body = body
            super().__init__(body)

    @staticmethod
    def aspirations_read(source, active):  # pragma: no cover — overridden
        raise NotImplementedError

    @staticmethod
    def tolerant_decode_list(source, raw):
        # Delegate to real _rt — pure parse, no daemon contact ().
        import importlib
        return importlib.import_module("_rt").tolerant_decode_list(source, raw)

    @staticmethod
    def tolerant_decode_aggregate(source, raw):
        # Delegate to real _rt — pure parse, no daemon contact ().
        import importlib
        return importlib.import_module("_rt").tolerant_decode_aggregate(source, raw)


def _call_read_goals(stub_cls, source="world"):
    """Run _read_goals with _rt swapped to stub_cls; return (result, stderr)."""
    orig_rt = M._rt
    M._rt = stub_cls
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            result = M._read_goals(source)
    finally:
        M._rt = orig_rt
    return result, buf.getvalue()


def _call_read_goals_expect_exit(stub_cls, source="world"):
    """Run _read_goals expecting SystemExit(1); return stderr text."""
    orig_rt = M._rt
    M._rt = stub_cls
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            M._read_goals(source)
    except SystemExit as e:
        assert e.code == 1, f"expected SystemExit(1), got SystemExit({e.code!r})"
        return buf.getvalue()
    finally:
        M._rt = orig_rt
    raise AssertionError("expected SystemExit(1) but _read_goals returned")


# ── Case 1: genuinely empty body — valid state, NOT a source error ────────
def test_empty_body_returns_empty_no_diagnostic():
    class _Stub(_StubRtBase):
        @staticmethod
        def aspirations_read(source, active):
            return ""

    result, err = _call_read_goals(_Stub)
    assert result == [], f"expected [], got {result!r}"
    assert err == "", f"empty body must not emit a diagnostic, got {err!r}"


# ── Case 2: whitespace-only body (lstrip -> empty) ────────────────────────
def test_whitespace_only_body_returns_empty_no_diagnostic():
    class _Stub(_StubRtBase):
        @staticmethod
        def aspirations_read(source, active):
            return "   \n\t  \n"

    result, err = _call_read_goals(_Stub)
    assert result == [], f"expected [], got {result!r}"
    assert err == "", f"whitespace body must not emit a diagnostic, got {err!r}"


# ── Case 3: valid empty list aggregate ────────────────────────────────────
def test_valid_empty_list_returns_no_goals():
    class _Stub(_StubRtBase):
        @staticmethod
        def aspirations_read(source, active):
            return "[]"

    result, err = _call_read_goals(_Stub)
    assert result == [], f"expected [], got {result!r}"
    assert err == "", f"valid empty list must not emit a diagnostic, got {err!r}"


# ── Case 4: valid list aggregate with populated aspirations ──────────────
def test_valid_list_aggregate_extracts_goals():
    payload = (
        '[{"id": "asp-115", "goals": ['
        '  {"id": "g-115-001", "status": "pending"},'
        '  {"id": "g-115-002", "status": "blocked"}'
        ']},'
        ' {"id": "asp-001", "goals": [{"id": "g-001-01"}]}]'
    )

    class _Stub(_StubRtBase):
        @staticmethod
        def aspirations_read(source, active):
            return payload

    result, err = _call_read_goals(_Stub, source="world")
    assert len(result) == 3, f"expected 3 goals, got {len(result)}: {result!r}"
    assert result[0]["id"] == "g-115-001"
    assert result[0]["_source"] == "world", "goals must be _source-stamped"
    assert result[0]["_aspiration_id"] == "asp-115"
    assert result[2]["id"] == "g-001-01"
    assert result[2]["_aspiration_id"] == "asp-001"
    assert err == "", f"valid list must not emit a diagnostic, got {err!r}"


# ── Case 5: valid dict aggregate (with "aspirations" key) ────────────────
def test_valid_dict_aggregate_extracts_goals():
    """The daemon may emit a dict envelope {"aspirations": [...]} instead of
    a bare list. _read_goals extracts via
    `data.get("aspirations") if isinstance(data, dict) else data`."""
    payload = (
        '{"version": 1, "aspirations": ['
        '  {"id": "asp-115", "goals": [{"id": "g-115-001"}]}'
        ']}'
    )

    class _Stub(_StubRtBase):
        @staticmethod
        def aspirations_read(source, active):
            return payload

    result, err = _call_read_goals(_Stub, source="agent")
    assert len(result) == 1, f"expected 1 goal, got {len(result)}: {result!r}"
    assert result[0]["id"] == "g-115-001"
    assert result[0]["_source"] == "agent"
    assert result[0]["_aspiration_id"] == "asp-115"
    assert err == "", f"valid dict must not emit a diagnostic, got {err!r}"


# ── Case 6:  recovery — valid prefix + trailing garbage ──────────
def test_valid_list_plus_trailing_garbage_recovers():
    """The canonical  corruption: a complete JSON list followed by
    a stale-code fragment. raw_decode stops at the first valid value."""
    payload = (
        '[{"id": "asp-115", "goals": [{"id": "g-115-001"}]}]'
        '\n<<<STALE RELOAD>>>{"partial": '
    )

    class _Stub(_StubRtBase):
        @staticmethod
        def aspirations_read(source, active):
            return payload

    result, err = _call_read_goals(_Stub)
    assert len(result) == 1 and result[0]["id"] == "g-115-001", (
        f"raw_decode must recover the valid prefix; got {result!r}"
    )
    assert err == "", f"successful recovery must not emit a diagnostic, got {err!r}"


# ── Case 7: unrecoverable body — SystemExit(1) + one symmetric diagnostic ─
def test_unrecoverable_body_is_fatal_with_one_diagnostic():
    class _Stub(_StubRtBase):
        @staticmethod
        def aspirations_read(source, active):
            return "not json at all $$$ \x00 garbage"

    err = _call_read_goals_expect_exit(_Stub, source="agent")
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly 1 diagnostic line, got {lines!r}"
    assert lines[0].startswith("[defer-recheck] agent "), (
        f"diagnostic must carry the symmetric '[defer-recheck] {{source}}' "
        f"prefix, got {lines[0]!r}"
    )
    assert "JSONDecodeError" in lines[0], (
        f"diagnostic must name the failure class, got {lines[0]!r}"
    )


# ── Case 7b: diagnostic truncates + escapes newlines in the body prefix ───
def test_diagnostic_truncates_and_escapes_long_body():
    class _Stub(_StubRtBase):
        @staticmethod
        def aspirations_read(source, active):
            return "X\n" * 200  # 400 chars, 200 newlines, never valid JSON

    err = _call_read_goals_expect_exit(_Stub, source="world")
    assert "\\n" in err, "newlines in the body prefix must be escaped to \\n"
    msg_line = err.strip()
    assert msg_line.count("\n") == 0, (
        f"diagnostic must be a single line (no raw newlines), got {msg_line!r}"
    )


# ── Case 8: non-dict-non-list aggregate (string scalar) — FATAL ──────────
def test_non_aggregate_string_is_fatal():
    """A valid JSON scalar is not a usable aggregate shape. _read_goals
    requires dict or list. Anything else is a corrupt source body."""
    class _Stub(_StubRtBase):
        @staticmethod
        def aspirations_read(source, active):
            return '"just a string"'

    err = _call_read_goals_expect_exit(_Stub, source="world")
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly 1 diagnostic line, got {lines!r}"
    assert "non-dict-and-non-list aggregate" in lines[0] and "type=str" in lines[0], (
        f"diagnostic must name the wrong-aggregate shape, got {lines[0]!r}"
    )


# ── Case 9: integer aggregate is also fatal ──────────────────────────────
def test_integer_aggregate_is_fatal():
    class _Stub(_StubRtBase):
        @staticmethod
        def aspirations_read(source, active):
            return "42"

    err = _call_read_goals_expect_exit(_Stub, source="agent")
    assert "type=int" in err, f"diagnostic must name int type, got {err!r}"


# ── Case 10: RtError -> SystemExit(1) (POST-CORRECTION fatal-aggregator) ──
def test_rterror_is_fatal_guard_383():
    """A1 RtError handling now matches the guard-383 fatal-aggregator
    contract (g-115-948 resolved via g-115-981). _read_goals is one of
    two source reads merged in main() (`_read_goals("world") +
    _read_goals("agent")`); a silent [] from either source would poison
    the merged set with a complete-looking lie.

    Mirrors siblings A2 (blocker-recheck), A3 (precondition-defer-
    recheck), and A4 (parent-supersession-sweep) which all follow the
    canonical pattern from parent-supersession-sweep.py. The defer-
    recheck.sh wrapper swallows non-zero exit so a transient daemon
    outage just leaves defers untouched for the next sweep.
    """
    class _Stub(_StubRtBase):
        @staticmethod
        def aspirations_read(source, active):
            raise _StubRtBase.RtError("daemon unreachable")

    # Use the subclass _Stub (which overrides aspirations_read to raise
    # RtError) — NOT _StubRtBase, whose base aspirations_read raises
    # NotImplementedError. The `except _rt.RtError` clause inside
    # _read_goals catches via _Stub.RtError == _StubRtBase.RtError.
    err = _call_read_goals_expect_exit(_Stub, source="world")
    assert "[defer-recheck] world read failed" in err, (
        f"diagnostic must carry the symmetric '[defer-recheck] {{source}} "
        f"read failed' prefix, got {err!r}"
    )
    assert "daemon unreachable" in err, (
        f"diagnostic must include the RtError body, got {err!r}"
    )


def run_all():
    tests = [
        test_empty_body_returns_empty_no_diagnostic,
        test_whitespace_only_body_returns_empty_no_diagnostic,
        test_valid_empty_list_returns_no_goals,
        test_valid_list_aggregate_extracts_goals,
        test_valid_dict_aggregate_extracts_goals,
        test_valid_list_plus_trailing_garbage_recovers,
        test_unrecoverable_body_is_fatal_with_one_diagnostic,
        test_diagnostic_truncates_and_escapes_long_body,
        test_non_aggregate_string_is_fatal,
        test_integer_aggregate_is_fatal,
        test_rterror_is_fatal_guard_383,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            print(f"  [FAIL] {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print()
    if failed:
        print(f"FAILED: {failed}/{len(tests)}")
        return 1
    print(f"OK: {len(tests)}/{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(run_all())
