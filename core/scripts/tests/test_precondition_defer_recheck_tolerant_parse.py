""" / -A3: precondition-defer-recheck.py corruption-tolerant
parse for _read_goals (line 114 in audit; now via _tolerant_decode).

Paired with g-115-797 (bravo audit catalog row 14). Before this fix, the
helper `_read_goals` did `json.loads(out)` then `except json.JSONDecodeError:
return []` — which silently collapsed the goals list to 0 under the
g-115-766 daemon aggregate-corruption mode (valid JSON prefix + trailing
garbage written during a stale-code reload window). Downstream effect:
0 goals to recheck, precondition_unmet defers stay frozen indefinitely
(never re-evaluated).

The fix applies the consolidation-health.py::_tolerant_decode pattern
with the same two adaptations parent-supersession-sweep.py uses:
  1. Accepts BOTH dict-with-aspirations-key AND bare-list aggregates (the
     daemon may emit either; the existing call site already extracts via
     `data.get("aspirations") if isinstance(data, dict) else data`)
  2. Empty body returns None (not []) — caller maps None → [] so the
     dict vs list shape is preserved through the helper boundary

A3 RtError handling — guard-383 fatal symmetry (rb-987):
`_read_goals` is called once for "world" and once for "agent" then
merged at line 179. This is the N>=2-source aggregator pattern
guard-383 mandates be FATAL on per-source error — silent `return []`
would write a complete-looking lie into the merged aggregate. The
exemplar consolidation-health.py corrected this in commit 28a3b7a;
sibling A4 (parent-supersession-sweep.py) followed the corrected
pattern. A3 matches the corrected exemplar / A4 — NOT A1
(defer-recheck.py) which retains the pre-correction silent-return.
A1 is itself a guard-383 violation candidate; sweep is filed as a
follow-up Investigate goal (see /encode-session Lane 4.1 output).

Behavior contract:
  - empty / whitespace body            -> None (caller: [])
  - valid JSON list                    -> the list
  - valid JSON dict with "aspirations" -> the dict
  - valid prefix + trailing garbage    -> the parsed prefix (g-115-766 recovery)
  - unrecoverable body                 -> ONE stderr diagnostic + SystemExit(1)
  - non-dict-and-non-list aggregate    -> ONE stderr diagnostic + SystemExit(1)
  - _read_goals RtError                -> ONE stderr diagnostic + SystemExit(1)
                                          (guard-383 fatal — A4/exemplar symmetry)

Run: py -3 -m pytest core/scripts/tests/test_precondition_defer_recheck_tolerant_parse.py -v
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
    """Import precondition-defer-recheck.py (hyphen in name blocks plain import)."""
    spec = importlib.util.spec_from_file_location(
        "precondition_defer_recheck_module",
        SCRIPT_DIR / "precondition-defer-recheck.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load_module()


def _decode(source, raw):
    """Call _tolerant_decode for non-fatal paths; returns (result, stderr)."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        result = M._tolerant_decode(source, raw)
    return result, buf.getvalue()


def _decode_expect_exit(source, raw):
    """Call _tolerant_decode expecting SystemExit(1); returns stderr text."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            M._tolerant_decode(source, raw)
    except SystemExit as e:
        assert e.code == 1, f"expected SystemExit(1), got SystemExit({e.code!r})"
        return buf.getvalue()
    raise AssertionError("expected SystemExit(1) but _tolerant_decode returned")


# ── Case 1: genuinely empty body — caller maps None → [] (valid state) ────
def test_empty_body_returns_none_no_diagnostic():
    result, err = _decode("world", "")
    assert result is None, f"expected None, got {result!r}"
    assert err == "", f"empty body must not emit a diagnostic, got {err!r}"


# ── Case 2: whitespace-only body (lstrip -> empty) ────────────────────────
def test_whitespace_only_body_returns_none_no_diagnostic():
    result, err = _decode("world", "   \n\t  \n")
    assert result is None, f"expected None, got {result!r}"
    assert err == "", f"whitespace body must not emit a diagnostic, got {err!r}"


# ── Case 3: valid empty list ──────────────────────────────────────────────
def test_valid_empty_list():
    result, err = _decode("world", "[]")
    assert result == [], f"expected [], got {result!r}"
    assert err == "", f"valid empty list must not emit a diagnostic, got {err!r}"


# ── Case 4: valid populated list (no goals — aspirations level only) ──────
def test_valid_populated_list():
    payload = '[{"id": "asp-115", "goals": []}, {"id": "asp-271", "goals": []}]'
    result, err = _decode("world", payload)
    assert result == [
        {"id": "asp-115", "goals": []},
        {"id": "asp-271", "goals": []},
    ], f"unexpected parse: {result!r}"
    assert err == "", f"valid list must not emit a diagnostic, got {err!r}"


# ── Case 5: valid dict with "aspirations" key (daemon dict-wrapped shape) ─
def test_valid_dict_with_aspirations_key():
    # Adaptation from consolidation-health: precondition-defer-recheck handles
    # BOTH dict-with-aspirations and bare-list aggregates.
    payload = '{"aspirations": [{"id": "asp-115", "goals": []}], "meta": {}}'
    result, err = _decode("world", payload)
    assert isinstance(result, dict) and "aspirations" in result, (
        f"dict aggregate must be returned as-is for caller to extract; got {result!r}"
    )
    assert err == "", f"valid dict must not emit a diagnostic, got {err!r}"


# ── Case 6:  recovery — valid prefix + trailing garbage ──────────
def test_valid_list_plus_trailing_garbage_recovers():
    # The canonical  corruption: complete JSON list followed by a
    # stale-code fragment. raw_decode stops at the first valid value.
    payload = '[{"id": "asp-115", "goals": []}]\n<<<STALE RELOAD>>>{"partial": '
    result, err = _decode("world", payload)
    assert result == [{"id": "asp-115", "goals": []}], (
        f"raw_decode must recover the valid prefix; got {result!r}"
    )
    assert err == "", f"successful recovery must not emit a diagnostic, got {err!r}"


# ── Case 7: unrecoverable body — SystemExit(1) + one symmetric diagnostic ─
def test_unrecoverable_body_is_fatal_with_one_diagnostic():
    err = _decode_expect_exit("agent", "not json at all $$$ \x00 garbage")
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly 1 diagnostic line, got {lines!r}"
    assert lines[0].startswith("precondition-defer-recheck: agent "), (
        f"diagnostic must carry the symmetric 'precondition-defer-recheck: "
        f"{{source}}' prefix, got {lines[0]!r}"
    )
    assert "JSONDecodeError" in lines[0], (
        f"diagnostic must name the failure class, got {lines[0]!r}"
    )


# ── Case 7b: diagnostic truncates + escapes newlines in the body prefix ───
def test_diagnostic_truncates_and_escapes_long_body():
    garbage = ("X\n" * 200)  # 400 chars, 200 newlines, never valid JSON
    err = _decode_expect_exit("world", garbage)
    assert "\\n" in err, "newlines in the body prefix must be escaped to \\n"
    msg_line = err.strip()
    assert msg_line.count("\n") == 0, (
        f"diagnostic must be a single line (no raw newlines), got {msg_line!r}"
    )


# ── Case 8: non-dict-non-list aggregate (string) — FATAL ──────────────────
def test_non_dict_non_list_aggregate_is_fatal():
    # A valid JSON scalar (string, int) is not a usable aggregate shape for
    # this script. guard-383 -> fatal+loud.
    err = _decode_expect_exit("world", '"just a string"')
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly 1 diagnostic line, got {lines!r}"
    assert "non-dict-and-non-list" in lines[0] and "type=str" in lines[0], (
        f"diagnostic must name the wrong-aggregate shape, got {lines[0]!r}"
    )


# ── Case 9: integer aggregate is also fatal ──────────────────────────────
def test_integer_aggregate_is_fatal():
    err = _decode_expect_exit("agent", "42")
    assert "type=int" in err, f"diagnostic must name int type, got {err!r}"


# ── Case 10: _read_goals wires _tolerant_decode + flattens goals ──────────
def test_read_goals_flattens_aspirations_into_goals_list():
    """End-to-end: _read_goals stubs _rt to return a valid dict-wrapped
    aggregate with goals and verifies the flattened list shape."""

    class _StubRt:
        class RtError(Exception):
            def __init__(self, body=""):
                self.body = body
                super().__init__(body)

        @staticmethod
        def aspirations_read(source, active):
            return (
                '{"aspirations": ['
                '{"id": "asp-115", "goals": ['
                '{"id": "g-115-1", "status": "pending"},'
                '{"id": "g-115-2", "status": "in-progress"}'
                ']},'
                '{"id": "asp-271", "goals": [{"id": "g-271-1"}]}'
                ']}'
            )

    orig_rt = M._rt
    M._rt = _StubRt
    try:
        goals = M._read_goals("world")
        ids = sorted(g["id"] for g in goals)
        assert ids == ["g-115-1", "g-115-2", "g-271-1"], (
            f"expected flattened goal IDs from both aspirations, got {ids!r}"
        )
        # _source and _aspiration_id are stamped per goal
        sources = sorted(set(g["_source"] for g in goals))
        assert sources == ["world"], f"unexpected _source values: {sources!r}"
        asp_ids = sorted(set(g["_aspiration_id"] for g in goals))
        assert asp_ids == ["asp-115", "asp-271"], (
            f"unexpected _aspiration_id values: {asp_ids!r}"
        )
    finally:
        M._rt = orig_rt


# ── Case 11: _read_goals RtError is FATAL — guard-383 symmetry ────────────
def test_read_goals_rterror_is_fatal_with_loud_diagnostic():
    """Stub _rt to raise RtError; verify guard-383 fatal pathway fires.

    rb-987 lesson: the original consolidation-health.py spec returned []
    on RtError ("symmetric with the spec-prescribed bare return []"). That
    symmetry was WRONG — guard-383 mandates the merged-aggregator pattern
    treat any per-source error as FATAL. consolidation-health.py was
    corrected in 28a3b7a; A4 followed the corrected pattern; A3 (this
    test) pins the same. A1 (defer-recheck.py) still carries the
    pre-correction silent-return and is a candidate for the audit-followup
    sweep (filed via /encode-session Lane 4.1).
    """

    class _StubRt:
        class RtError(Exception):
            def __init__(self, body=""):
                self.body = body
                super().__init__(body)

        @staticmethod
        def aspirations_read(source, active):
            raise _StubRt.RtError("daemon unavailable")

    orig_rt = M._rt
    M._rt = _StubRt
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            M._read_goals("world")
    except SystemExit as e:
        assert e.code == 1, f"expected SystemExit(1), got SystemExit({e.code!r})"
        err = buf.getvalue()
        assert "[precondition-defer-recheck] world read failed" in err, (
            f"diagnostic must carry the bracketed-prefix shape, got {err!r}"
        )
    else:
        raise AssertionError(
            "expected SystemExit(1) but _read_goals returned silently — "
            "guard-383 violation re-introduced (rb-987)"
        )
    finally:
        M._rt = orig_rt


# ── Case 12: _read_goals on JSONDecodeError EXITS (does NOT silent-collapse) ─
def test_read_goals_on_corruption_exits_not_silent():
    """Direct regression for the audit's specific concern: the pre-fix
    code did `except json.JSONDecodeError: return []` which froze
    precondition defers. Post-fix _read_goals must exit (via
    _tolerant_decode's sys.exit(1)), NOT return [] silently.
    """

    class _StubRt:
        class RtError(Exception):
            pass

        @staticmethod
        def aspirations_read(source, active):
            return "<<< not valid json at all >>>"

    orig_rt = M._rt
    M._rt = _StubRt
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            M._read_goals("world")
    except SystemExit as e:
        assert e.code == 1, f"expected SystemExit(1), got SystemExit({e.code!r})"
        err = buf.getvalue()
        assert "JSONDecodeError" in err, (
            f"diagnostic must name JSONDecodeError, got {err!r}"
        )
    else:
        raise AssertionError(
            "expected SystemExit(1) on corrupted body but _read_goals returned silently"
        )
    finally:
        M._rt = orig_rt
