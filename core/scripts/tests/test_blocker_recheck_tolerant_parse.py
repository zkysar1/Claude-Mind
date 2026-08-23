""" / -A2: blocker-recheck.py corruption-tolerant
parse for _wm_read_blockers (line 87 in audit; now via _tolerant_decode).

Paired with g-115-797 (bravo audit catalog row 3) and A3 sibling
(precondition-defer-recheck.py, g-115-941, commit bd71431c). Before this
fix, the helper `_wm_read_blockers` did `json.loads(out)` then `except
json.JSONDecodeError: return []` — silently collapsing the blocker
list to 0 under the g-115-766 daemon aggregate-corruption mode (valid
JSON prefix + trailing garbage written during a stale-code reload
window). Downstream effect: 0 blockers to recheck, blocker-recovery
loop reports zero rechecks, every aged blocker stays frozen
indefinitely (never re-evaluated against capability gate).

The fix applies the consolidation-health.py::_tolerant_decode pattern
adapted for single-source wm slot reads:
  1. Accepts BOTH dict and bare-list aggregates (daemon shape variance
     tolerance — mirrors A3/A4/exemplar; A2 slot schema is list-only but
     the decode tolerates aggregate variance and the caller's
     `isinstance(data, list) else []` guards downstream consumption).
  2. Empty body OR literal "null" returns None (caller maps None → [])

A2 RtError handling — guard-383 fatal symmetry (rb-987):
`_wm_read_blockers` feeds the blocker-recheck loop at line 184. This is
the single-source-feeds-consumer pattern guard-383 mandates be FATAL on
per-source error — silent `return []` would write a complete-looking
lie to consumers (zero rechecks instead of "wm unreachable"). The
exemplar consolidation-health.py corrected this in commit 28a3b7a; A3
sibling (precondition-defer-recheck.py) followed the corrected pattern.
A2 matches the corrected exemplar / A3 / A4 — NOT A1 (defer-recheck.py)
which retains the pre-correction silent-return.

Behavior contract:
  - empty / whitespace body            -> None (caller: [])
  - literal "null" string              -> None (caller: [])
  - valid JSON list                    -> the list
  - valid JSON dict                    -> the dict (caller filters via
                                          isinstance check)
  - valid prefix + trailing garbage    -> the parsed prefix (g-115-766 recovery)
  - unrecoverable body                 -> ONE stderr diagnostic + SystemExit(1)
  - non-dict-and-non-list aggregate    -> ONE stderr diagnostic + SystemExit(1)
  - _wm_read_blockers RtError          -> ONE stderr diagnostic + SystemExit(1)
                                          (guard-383 fatal — A4/exemplar symmetry)

Run: py -3 -m pytest core/scripts/tests/test_blocker_recheck_tolerant_parse.py -v
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
    """Import blocker-recheck.py (hyphen in name blocks plain import)."""
    spec = importlib.util.spec_from_file_location(
        "blocker_recheck_module",
        SCRIPT_DIR / "blocker-recheck.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load_module()


def _decode(slot, raw):
    """Call _tolerant_decode for non-fatal paths; returns (result, stderr)."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        result = M._tolerant_decode(slot, raw)
    return result, buf.getvalue()


def _decode_expect_exit(slot, raw):
    """Call _tolerant_decode expecting SystemExit(1); returns stderr text."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            M._tolerant_decode(slot, raw)
    except SystemExit as e:
        assert e.code == 1, f"expected SystemExit(1), got SystemExit({e.code!r})"
        return buf.getvalue()
    raise AssertionError("expected SystemExit(1) but _tolerant_decode returned")


# ── Case 1: genuinely empty body — caller maps None → [] (valid state) ────
def test_empty_body_returns_none_no_diagnostic():
    result, err = _decode("known_blockers", "")
    assert result is None, f"expected None, got {result!r}"
    assert err == "", f"empty body must not emit a diagnostic, got {err!r}"


# ── Case 2: whitespace-only body (lstrip -> empty) ────────────────────────
def test_whitespace_only_body_returns_none_no_diagnostic():
    result, err = _decode("known_blockers", "   \n\t  \n")
    assert result is None, f"expected None, got {result!r}"
    assert err == "", f"whitespace body must not emit a diagnostic, got {err!r}"


# ── Case 3: literal "null" (canonical empty-slot serialization) ───────────
def test_null_literal_returns_none_no_diagnostic():
    """wm_read returns the literal string 'null' when a slot was set to
    JSON null. Treating this as empty-state (None → caller maps to [])
    preserves the legacy behavior at line 83 of the pre-fix code."""
    result, err = _decode("known_blockers", "null")
    assert result is None, f"expected None for 'null' literal, got {result!r}"
    assert err == "", f"'null' literal must not emit a diagnostic, got {err!r}"


# ── Case 3b: the PRODUCTION body shape — 'null' WITH a trailing newline ───
def test_null_literal_with_trailing_newline_returns_none():
    """guard-920 / self.md 7th state: Case 3 fixtures a BARE 'null', but the
    daemon does not emit one. Measured on cc-02 2026-08-21 via
    `_rt.wm_read(slot='known_blockers', as_json=True)`: the body is
    `'null\\n'`. The early-return guard used `.lstrip()`, which strips only
    LEADING whitespace, so `'null\\n'.lstrip() != 'null'` and the empty slot
    fell through to `tolerant_decode_aggregate`, which correctly classifies
    Python None as a non-dict-and-non-list aggregate -> SystemExit(1).

    Effect: blocker-recheck died rc=1 with ZERO stdout on every agent whose
    known_blockers slot is null -- the common case (g-115-4328 measured all
    five fleet agents reading null). The goal-record population that same
    fix moved INTO the script (_read_goal_blocker_refs) never ran, because
    the WM read killed the process first.

    Case 3 passed throughout: it reached the defect, sat at the right layer,
    discriminated correctly, and asserted against a fixture production never
    produces. Sibling create-blocker.py:109 uses `.strip()` on the same slot.
    """
    result, err = _decode("known_blockers", "null\n")
    assert result is None, (
        f"the daemon's real empty-slot body is 'null\\n' -- got {result!r}"
    )
    assert err == "", f"'null\\n' must not emit a diagnostic, got {err!r}"


# ── Case 3c: CRLF variant (Windows daemon) ────────────────────────────────
def test_null_literal_with_crlf_returns_none():
    result, err = _decode("known_blockers", "null\r\n")
    assert result is None, f"expected None for 'null\\r\\n', got {result!r}"
    assert err == "", f"'null\\r\\n' must not emit a diagnostic, got {err!r}"


# ── Case 4: valid empty list ──────────────────────────────────────────────
def test_valid_empty_list():
    result, err = _decode("known_blockers", "[]")
    assert result == [], f"expected [], got {result!r}"
    assert err == "", f"valid empty list must not emit a diagnostic, got {err!r}"


# ── Case 5: valid populated list ──────────────────────────────────────────
def test_valid_populated_list():
    payload = (
        '[{"blocker_id": "b-001", "reason": "SSH host key", '
        '"affected_skills": ["efs-ssh"]},'
        ' {"blocker_id": "b-002", "reason": "stale daemon"}]'
    )
    result, err = _decode("known_blockers", payload)
    assert isinstance(result, list) and len(result) == 2, (
        f"expected 2-element list, got {result!r}"
    )
    assert result[0]["blocker_id"] == "b-001"
    assert result[1]["blocker_id"] == "b-002"
    assert err == "", f"valid list must not emit a diagnostic, got {err!r}"


# ── Case 6: valid dict aggregate (caller filters) ─────────────────────────
def test_valid_dict_returned_for_caller_filter():
    """A2's _tolerant_decode tolerates aggregate-shape variance — the
    daemon may emit a dict envelope under future schema evolution.
    The decode returns it as-is; the caller's
    `return data if isinstance(data, list) else []` line guards
    downstream consumption against unexpected dict shape (turns
    dict into [] safely). Symmetric with A3's exemplar."""
    payload = '{"version": 2, "blockers": []}'
    result, err = _decode("known_blockers", payload)
    assert isinstance(result, dict), f"dict aggregate must be returned as-is; got {result!r}"
    assert result.get("version") == 2
    assert err == "", f"valid dict must not emit a diagnostic, got {err!r}"


# ── Case 7:  recovery — valid prefix + trailing garbage ──────────
def test_valid_list_plus_trailing_garbage_recovers():
    """The canonical  corruption: complete JSON list followed
    by a stale-code fragment. raw_decode stops at the first valid value."""
    payload = '[{"blocker_id": "b-001"}]\n<<<STALE RELOAD>>>{"partial": '
    result, err = _decode("known_blockers", payload)
    assert result == [{"blocker_id": "b-001"}], (
        f"raw_decode must recover the valid prefix; got {result!r}"
    )
    assert err == "", f"successful recovery must not emit a diagnostic, got {err!r}"


# ── Case 8: unrecoverable body — SystemExit(1) + one symmetric diagnostic ─
def test_unrecoverable_body_is_fatal_with_one_diagnostic():
    err = _decode_expect_exit("known_blockers", "not json at all $$$ \x00 garbage")
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly 1 diagnostic line, got {lines!r}"
    assert lines[0].startswith("blocker-recheck: known_blockers "), (
        f"diagnostic must carry the symmetric 'blocker-recheck: "
        f"{{slot}}' prefix, got {lines[0]!r}"
    )
    assert "JSONDecodeError" in lines[0], (
        f"diagnostic must name the failure class, got {lines[0]!r}"
    )


# ── Case 8b: diagnostic truncates + escapes newlines in the body prefix ───
def test_diagnostic_truncates_and_escapes_long_body():
    garbage = ("X\n" * 200)  # 400 chars, 200 newlines, never valid JSON
    err = _decode_expect_exit("known_blockers", garbage)
    assert "\\n" in err, "newlines in the body prefix must be escaped to \\n"
    msg_line = err.strip()
    assert msg_line.count("\n") == 0, (
        f"diagnostic must be a single line (no raw newlines), got {msg_line!r}"
    )


# ── Case 9: non-dict-non-list aggregate (string) — FATAL ──────────────────
def test_non_dict_non_list_aggregate_is_fatal():
    """A valid JSON scalar (string, int) is not a usable aggregate shape
    for this slot. guard-383 -> fatal+loud."""
    err = _decode_expect_exit("known_blockers", '"just a string"')
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly 1 diagnostic line, got {lines!r}"
    assert "non-dict-and-non-list" in lines[0] and "type=str" in lines[0], (
        f"diagnostic must name the wrong-aggregate shape, got {lines[0]!r}"
    )


# ── Case 10: integer aggregate is also fatal ─────────────────────────────
def test_integer_aggregate_is_fatal():
    err = _decode_expect_exit("known_blockers", "42")
    assert "type=int" in err, f"diagnostic must name int type, got {err!r}"


# ── Case 11: _wm_read_blockers maps None → [] and dict → [] via caller filter ─
def test_read_blockers_empty_slot_returns_empty_list():
    """End-to-end: _wm_read_blockers wires _tolerant_decode + caller filter.
    Empty slot ('null' literal) -> _tolerant_decode None -> caller []."""

    class _StubRt:
        class RtError(Exception):
            def __init__(self, body=""):
                self.body = body
                super().__init__(body)

        @staticmethod
        def wm_read(slot, as_json):
            return "null"  # canonical empty-slot serialization

        @staticmethod
        def tolerant_decode_aggregate(source, raw):
            import importlib
            return importlib.import_module("_rt").tolerant_decode_aggregate(source, raw)

    orig_rt = M._rt
    M._rt = _StubRt
    try:
        result = M._wm_read_blockers()
        assert result == [], f"empty slot must map to [], got {result!r}"
    finally:
        M._rt = orig_rt


# ── Case 11b: the END-TO-END path against the shape production ACTUALLY emits ─
def test_read_blockers_empty_slot_with_trailing_newline_returns_empty_list():
    """Case 11's twin, and the one with mutation sensitivity.

    Case 11 stubs `wm_read` returning a BARE "null". The live daemon returns
    `'null\\n'` (measured on cc-02 2026-08-21). That difference is not
    cosmetic: it is the whole defect. Case 11 passed continuously while
    `_tolerant_decode` used `.lstrip()`, so the end-to-end test asserting the
    empty-slot contract could not observe the empty-slot contract being
    broken -- for 64 days, on the lane that runs every precheck iteration.

    Revert `_tolerant_decode` to `.lstrip()` and THIS test fails
    (SystemExit(1) out of tolerant_decode_aggregate) while Case 11 still
    passes. That asymmetry is the point: a fixture is only a regression guard
    for the shape it actually carries, so the stub must carry the producer's
    bytes, not the contract's idealized spelling.

    Same defect class as rb-4832 (an invented fixture body that fingerprints
    to None and green-passes a dedup test it never exercised) -- framework
    surface rather than domain, reached from the opposite direction.
    """

    class _StubRt:
        class RtError(Exception):
            def __init__(self, body=""):
                self.body = body
                super().__init__(body)

        @staticmethod
        def wm_read(slot, as_json):
            return "null\n"  # the daemon's REAL empty-slot body

        @staticmethod
        def tolerant_decode_aggregate(source, raw):
            import importlib
            return importlib.import_module("_rt").tolerant_decode_aggregate(source, raw)

    orig_rt = M._rt
    M._rt = _StubRt
    try:
        result = M._wm_read_blockers()
        assert result == [], (
            "the daemon's real empty-slot body 'null\\n' must map to [], "
            f"got {result!r}"
        )
    finally:
        M._rt = orig_rt


def test_read_blockers_dict_slot_filtered_to_empty_list():
    """Defense-in-depth: if the wm slot somehow contains a dict (schema
    drift), the caller's `isinstance(data, list) else []` guard turns it
    into []. _tolerant_decode does NOT exit on dict (tolerant of
    aggregate-shape variance), but the caller filters to preserve
    blocker-recheck's downstream list contract."""

    class _StubRt:
        class RtError(Exception):
            pass

        @staticmethod
        def wm_read(slot, as_json):
            return '{"version": 2}'  # dict slot value

        @staticmethod
        def tolerant_decode_aggregate(source, raw):
            import importlib
            return importlib.import_module("_rt").tolerant_decode_aggregate(source, raw)

    orig_rt = M._rt
    M._rt = _StubRt
    try:
        result = M._wm_read_blockers()
        assert result == [], f"dict slot must filter to [], got {result!r}"
    finally:
        M._rt = orig_rt


# ── Case 12: _wm_read_blockers RtError is FATAL — guard-383 symmetry ─────
def test_read_blockers_rterror_is_fatal_with_loud_diagnostic():
    """Stub _rt to raise RtError; verify guard-383 fatal pathway fires.

    rb-987 lesson: the original consolidation-health.py spec returned []
    on RtError ("symmetric with the spec-prescribed bare return []"). That
    symmetry was WRONG — guard-383 mandates the merged-aggregator pattern
    treat any per-source error as FATAL. consolidation-health.py was
    corrected in 28a3b7a; A3 followed the corrected pattern; A2 (this
    test) pins the same. A1 (defer-recheck.py) still carries the
    pre-correction silent-return.
    """

    class _StubRt:
        class RtError(Exception):
            def __init__(self, body=""):
                self.body = body
                super().__init__(body)

        @staticmethod
        def wm_read(slot, as_json):
            raise _StubRt.RtError("daemon unreachable")

    orig_rt = M._rt
    M._rt = _StubRt
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            M._wm_read_blockers()
    except SystemExit as e:
        assert e.code == 1, f"expected SystemExit(1), got SystemExit({e.code!r})"
        err = buf.getvalue()
        assert "[blocker-recheck] known_blockers wm_read failed" in err, (
            f"diagnostic must carry the bracketed-prefix shape, got {err!r}"
        )
    else:
        raise AssertionError(
            "expected SystemExit(1) but _wm_read_blockers returned silently — "
            "guard-383 violation re-introduced (rb-987)"
        )
    finally:
        M._rt = orig_rt


# ── Case 13: _wm_read_blockers on JSONDecodeError EXITS (does NOT silent-collapse) ─
def test_read_blockers_on_corruption_exits_not_silent():
    """Direct regression for the audit's specific concern: the pre-fix
    code did `except json.JSONDecodeError: return []` which froze
    every aged blocker. Post-fix _wm_read_blockers must exit (via
    _tolerant_decode's sys.exit(1)), NOT return [] silently.
    """

    class _StubRt:
        class RtError(Exception):
            pass

        @staticmethod
        def wm_read(slot, as_json):
            return "<<< not valid json at all >>>"

        @staticmethod
        def tolerant_decode_aggregate(source, raw):
            import importlib
            return importlib.import_module("_rt").tolerant_decode_aggregate(source, raw)

    orig_rt = M._rt
    M._rt = _StubRt
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            M._wm_read_blockers()
    except SystemExit as e:
        assert e.code == 1, f"expected SystemExit(1), got SystemExit({e.code!r})"
        err = buf.getvalue()
        assert "JSONDecodeError" in err, (
            f"diagnostic must name JSONDecodeError, got {err!r}"
        )
    else:
        raise AssertionError(
            "expected SystemExit(1) on corrupted body but _wm_read_blockers returned silently"
        )
    finally:
        M._rt = orig_rt
