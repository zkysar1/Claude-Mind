""": consolidation-health.py corruption-intolerant parse fix
(guard-383-compliant revision after code-review-protocol.md step-4
pre-apply consultation).

Paired with g-115-775. Before this fix, `_load_active` did
`json.loads(raw or "[]")` and on ANY JSONDecodeError returned [] — which
silently collapsed active_count to 0 under the g-115-766 daemon
aggregate-corruption mode (valid JSON prefix + trailing garbage written
during a stale-code reload window). The three downstream consumers
(aspirations-evolve gap gate, aspirations-select near-complete bias,
create-aspiration consolidation-gate) then read "portfolio is empty" and
fired wrong-direction decisions with NO diagnostic.

guard-383 (retroactively consulted via retrieve.sh — the rb-774/guard-165
class of "fix that passed tests but violated a guardrail"): a script
merging N>=2 independent sources into a single aggregate MUST fail the
WHOLE output on a per-source error (`sys.exit(1)`), never return partial
data. The single fail-open boundary is the caller's shell wrapper
`|| echo WARN` (rb-347), never inside the aggregator. So the corrupt /
non-list / RtError branches are now FATAL, not `return []` — a `[]`
return would still poison the consolidation_health slot with a
complete-looking lie the 3 gates cannot see past.

Behavior contract:
  - empty / whitespace body            -> [] (genuinely empty queue is a
                                          VALID state, NOT a source error;
                                          guard-383 does not apply)
  - valid JSON list                    -> the list
  - valid JSON list + trailing garbage -> the parsed list (g-115-766
                                          recovery via raw_decode; a
                                          recoverable body is not an error)
  - unrecoverable body                 -> ONE stderr diagnostic + SystemExit(1)
  - non-list aggregate (dict etc.)     -> ONE stderr diagnostic + SystemExit(1)
                                          (resolves fresh-eyes finding
                                          msg-20260516-070416-alpha-1156:
                                          loud-fatal > silent-[])
  - _load_active RtError               -> ONE stderr diagnostic + SystemExit(1)
                                          (guard-383 action_hint #2; symmetric)

Run: py -3 core/scripts/tests/test_consolidation_health_tolerant_parse.py
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
    """Import consolidation-health.py (hyphen in name blocks plain import)."""
    spec = importlib.util.spec_from_file_location(
        "consolidation_health_module",
        SCRIPT_DIR / "consolidation-health.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load_module()


def _decode(source, raw):
    """Call _tolerant_decode for the NON-fatal paths; returns (result, stderr)."""
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


# ── Case 1: genuinely empty body — valid state, NOT a source error ────────
def test_empty_body_returns_empty_no_diagnostic():
    result, err = _decode("world", "")
    assert result == [], f"expected [], got {result!r}"
    assert err == "", f"empty body must not emit a diagnostic, got {err!r}"


# ── Case 2: whitespace-only body (lstrip -> empty) ────────────────────────
def test_whitespace_only_body_returns_empty_no_diagnostic():
    result, err = _decode("world", "   \n\t  \n")
    assert result == [], f"expected [], got {result!r}"
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


# ── Case 5:  recovery — valid prefix + trailing garbage ──────────
def test_valid_list_plus_trailing_garbage_recovers():
    # The canonical  corruption: a complete JSON list followed by
    # a stale-code fragment. A recoverable body is NOT a source error, so
    # guard-383 does not make it fatal — raw_decode returns the prefix.
    payload = '[{"id": "asp-115"}]\n<<<STALE RELOAD>>>{"partial": '
    result, err = _decode("world", payload)
    assert result == [{"id": "asp-115"}], (
        f"raw_decode must recover the valid prefix; got {result!r}"
    )
    assert err == "", f"successful recovery must not emit a diagnostic, got {err!r}"


# ── Case 6: unrecoverable body — SystemExit(1) + one symmetric diagnostic ─
def test_unrecoverable_body_is_fatal_with_one_diagnostic():
    err = _decode_expect_exit("agent", "not json at all $$$ \x00 garbage")
    # guard-383: source error is FATAL, not a silent []. Exactly ONE
    # diagnostic, symmetric prefix with the RtError branch.
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly 1 diagnostic line, got {lines!r}"
    assert lines[0].startswith("consolidation-health: agent "), (
        f"diagnostic must carry the symmetric 'consolidation-health: {{source}}' "
        f"prefix, got {lines[0]!r}"
    )
    assert "JSONDecodeError" in lines[0], (
        f"diagnostic must name the failure class, got {lines[0]!r}"
    )


# ── Case 6b: diagnostic truncates + escapes newlines in the body prefix ───
def test_diagnostic_truncates_and_escapes_long_body():
    garbage = ("X\n" * 200)  # 400 chars, 200 newlines, never valid JSON
    err = _decode_expect_exit("world", garbage)
    assert "\\n" in err, "newlines in the body prefix must be escaped to \\n"
    msg_line = err.strip()
    assert msg_line.count("\n") == 0, (
        f"diagnostic must be a single line (no raw newlines), got {msg_line!r}"
    )


# ── Case 7: non-list aggregate (dict) — now FATAL (was the silent-loss) ───
def test_non_list_object_is_fatal_with_one_diagnostic():
    # Resolves fresh-eyes finding msg-20260516-070416-alpha-1156: a
    # daemon error-envelope {...} recovered from a  object-prefix
    # is a source error, NOT an empty portfolio. guard-383 -> fatal+loud.
    err = _decode_expect_exit("world", '{"error": "stale", "active_count": 5}')
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly 1 diagnostic line, got {lines!r}"
    assert lines[0].startswith("consolidation-health: world "), (
        f"diagnostic must carry the symmetric prefix, got {lines[0]!r}"
    )
    assert "non-list aggregate" in lines[0] and "type=dict" in lines[0], (
        f"diagnostic must name the non-list shape, got {lines[0]!r}"
    )


# ── Wiring: _load_active recovery delegates to _tolerant_decode ──────────
def test_load_active_delegates_to_tolerant_decode():
    # Outcome #1: _load_active routes through _tolerant_decode. Stub _rt so
    # no daemon is required; assert the RECOVERABLE corrupted body flows
    # through the recovery path (not the old json.loads-then-[] collapse).
    real_read = M._rt.aspirations_read
    try:
        M._rt.aspirations_read = lambda source, active: (
            '[{"id": "asp-1"}]TRAILING-STALE-GARBAGE'
        )
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            out = M._load_active("world")
        assert out == [{"id": "asp-1"}], (
            f"_load_active must recover via _tolerant_decode; got {out!r}"
        )
        assert buf.getvalue() == "", "recovered read must be silent"
    finally:
        M._rt.aspirations_read = real_read


# ── Wiring: _load_active RtError branch is FATAL (guard-383 #2, symmetry) ─
def test_load_active_rterror_is_fatal():
    real_read = M._rt.aspirations_read
    RtError = M._rt.RtError

    def _boom(source, active):
        raise RtError("daemon unreachable")

    try:
        M._rt.aspirations_read = _boom
        buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                M._load_active("agent")
        except SystemExit as e:
            assert e.code == 1, f"expected SystemExit(1), got {e.code!r}"
        else:
            raise AssertionError("RtError branch must be fatal (guard-383 #2)")
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 1, f"expected 1 diagnostic, got {lines!r}"
        assert lines[0].startswith("consolidation-health: agent read failed"), (
            f"RtError diagnostic must carry the symmetric prefix, got {lines[0]!r}"
        )
    finally:
        M._rt.aspirations_read = real_read


def run_all():
    tests = [
        test_empty_body_returns_empty_no_diagnostic,
        test_whitespace_only_body_returns_empty_no_diagnostic,
        test_valid_empty_list,
        test_valid_populated_list,
        test_valid_list_plus_trailing_garbage_recovers,
        test_unrecoverable_body_is_fatal_with_one_diagnostic,
        test_diagnostic_truncates_and_escapes_long_body,
        test_non_list_object_is_fatal_with_one_diagnostic,
        test_load_active_delegates_to_tolerant_decode,
        test_load_active_rterror_is_fatal,
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
