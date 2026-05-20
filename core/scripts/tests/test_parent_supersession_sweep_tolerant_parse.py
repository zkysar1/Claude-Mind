""" / -A4: parent-supersession-sweep.py corruption-tolerant
parse for _read_aspirations (line 141 in audit; now via _tolerant_decode).

Paired with g-115-797 (bravo audit catalog). Before this fix, the helper
`_read_aspirations` did `json.loads(out)` then `except json.JSONDecodeError:
return []` — which silently collapsed the aspiration list to 0 under the
g-115-766 daemon aggregate-corruption mode (valid JSON prefix + trailing
garbage written during a stale-code reload window). Downstream effect:
0 parents seen, no supersession matches fire — silently breaking the
parent-supersession-sweep guard.

The fix applies the consolidation-health.py::_tolerant_decode pattern with
two adaptations:
  1. Accepts BOTH dict-with-aspirations-key AND bare-list aggregates (the
     daemon may emit either; the existing call site at line 195 already
     extracts via `data.get("aspirations") if isinstance(data, dict) else data`)
  2. Empty body returns None (not []) — the caller maps None → [] so it can
     distinguish "empty queue" (valid) from "decoded as a dict" (extract via
     "aspirations" key)

guard-383 (N>=2-source aggregator pattern): `_read_aspirations` is called
for BOTH world and agent sources at line 279 and merged. A per-source error
must be FATAL (sys.exit(1)) — never silent [] — to avoid poisoning the
merged aggregate with a complete-looking lie.

Behavior contract:
  - empty / whitespace body            -> None (caller: [])
  - valid JSON list                    -> the list
  - valid JSON dict with "aspirations" -> the dict
  - valid prefix + trailing garbage    -> the parsed prefix (g-115-766 recovery)
  - unrecoverable body                 -> ONE stderr diagnostic + SystemExit(1)
  - non-dict-and-non-list aggregate    -> ONE stderr diagnostic + SystemExit(1)
  - _read_aspirations RtError          -> ONE stderr diagnostic + SystemExit(1)
                                          (guard-383 symmetry)

Run: py -3 -m pytest core/scripts/tests/test_parent_supersession_sweep_tolerant_parse.py -v
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
    """Import parent-supersession-sweep.py (hyphen in name blocks plain import)."""
    spec = importlib.util.spec_from_file_location(
        "parent_supersession_sweep_module",
        SCRIPT_DIR / "parent-supersession-sweep.py",
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


# ── Case 4: valid populated list ──────────────────────────────────────────
def test_valid_populated_list():
    payload = '[{"id": "asp-115", "status": "active"}, {"id": "asp-271"}]'
    result, err = _decode("world", payload)
    assert result == [
        {"id": "asp-115", "status": "active"},
        {"id": "asp-271"},
    ], f"unexpected parse: {result!r}"
    assert err == "", f"valid list must not emit a diagnostic, got {err!r}"


# ── Case 5: valid dict with "aspirations" key (daemon dict-wrapped shape) ─
def test_valid_dict_with_aspirations_key():
    # Adaptation from consolidation-health: parent-supersession-sweep handles
    # BOTH dict-with-aspirations and bare-list aggregates.
    payload = '{"aspirations": [{"id": "asp-115"}, {"id": "asp-271"}], "meta": {}}'
    result, err = _decode("world", payload)
    assert isinstance(result, dict) and "aspirations" in result, (
        f"dict aggregate must be returned as-is for caller to extract; got {result!r}"
    )
    assert err == "", f"valid dict must not emit a diagnostic, got {err!r}"


# ── Case 6:  recovery — valid prefix + trailing garbage ──────────
def test_valid_list_plus_trailing_garbage_recovers():
    # The canonical  corruption: complete JSON list followed by a
    # stale-code fragment. raw_decode stops at the first valid value.
    payload = '[{"id": "asp-115"}]\n<<<STALE RELOAD>>>{"partial": '
    result, err = _decode("world", payload)
    assert result == [{"id": "asp-115"}], (
        f"raw_decode must recover the valid prefix; got {result!r}"
    )
    assert err == "", f"successful recovery must not emit a diagnostic, got {err!r}"


# ── Case 7: unrecoverable body — SystemExit(1) + one symmetric diagnostic ─
def test_unrecoverable_body_is_fatal_with_one_diagnostic():
    err = _decode_expect_exit("agent", "not json at all $$$ \x00 garbage")
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly 1 diagnostic line, got {lines!r}"
    assert lines[0].startswith("parent-supersession-sweep: agent "), (
        f"diagnostic must carry the symmetric 'parent-supersession-sweep: "
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


# ── Case 10: _read_aspirations wires _tolerant_decode (RtError -> fatal) ──
def test_read_aspirations_rterror_is_fatal_with_loud_diagnostic():
    # Stub _rt to raise RtError; verify guard-383 fatal pathway fires with
    # the symmetric diagnostic prefix (previously this silently returned []).
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
            M._read_aspirations("world")
    except SystemExit as e:
        assert e.code == 1, f"expected SystemExit(1), got SystemExit({e.code!r})"
        err = buf.getvalue()
        assert "parent-supersession-sweep: world read failed" in err, (
            f"diagnostic must carry the symmetric prefix, got {err!r}"
        )
    else:
        raise AssertionError(
            "expected SystemExit(1) but _read_aspirations returned silently"
        )
    finally:
        M._rt = orig_rt
