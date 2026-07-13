"""Regression for cp1252 em-dash UTF-8 byte bug (rb-1954, ).

cp1252 em-dash is a single byte 0x97 — not the valid UTF-8 sequence e2 80 94
(U+2014). When this byte reaches a Python consumer via env var or sys.stdin in
UTF-8 text mode, it triggers UnicodeDecodeError or json.dumps ValueError.

Three consumer locations fixed:
  L362  iteration-close.sh     SUM env var → execution-diary breadcrumb
  L122  post-state-update-metric-gate.sh  OUTCOME_NOTE env var
  L161  journal-append.sh      sys.stdin (SUMMARY piped via printf)

On POSIX, Python decodes env-var bytes with surrogateescape (PEP 383),
turning 0x97 into lone surrogate U+DC97. json.dumps then raises ValueError.
The fix: .encode("utf-8", errors="replace").decode("utf-8") at each ingress.
"""
import io
import json
import re
import sys

import pytest


def _posix_environ(raw: bytes) -> str:
    """Simulate POSIX os.environ surrogate-escape of non-UTF-8 bytes (PEP 383)."""
    return raw.decode("utf-8", errors="surrogateescape")


# ── Fixed behaviour (must pass) ────────────────────────────────────────────

def test_l362_env_var_em_dash():
    """L362 fix: SUM env var cp1252 em-dash produces valid JSON."""
    content = _posix_environ(b"summary\x97note").encode("utf-8", errors="replace").decode("utf-8")
    out = json.dumps({"entry_type": "finding", "goal_id": "g-t", "content": "verified: " + content})
    assert json.loads(out)["entry_type"] == "finding"


def test_l122_metric_gate_em_dash():
    """L122 fix: OUTCOME_NOTE env var cp1252 em-dash supports regex scan."""
    text = _posix_environ(b"found 42\x97items").encode("utf-8", errors="replace").decode("utf-8")
    assert re.findall(r"\d+", text) == ["42"]


def test_l161_stdin_buffer_em_dash():
    """L161 fix: sys.stdin.buffer.read() with cp1252 em-dash decodes cleanly."""
    summary = io.BytesIO(b"text\x97here").read().decode("utf-8", errors="replace")
    assert isinstance(summary, str) and "text" in summary


# ── Unfixed behaviour (demonstrates the bug exists without the fix) ────────

@pytest.mark.skipif(sys.platform == "win32", reason="Windows handles surrogates; bug manifests only on POSIX agents")
def test_l362_without_fix_raises():
    """Without fix: the cp1252 em-dash lone surrogate cannot be UTF-8 encoded —
    the failure the L362 ingress fix (.encode('utf-8', errors='replace')) prevents.

    NOTE (g-115-1836): the original demonstration asserted json.dumps() raises,
    but on CPython 3.12 json.dumps(ensure_ascii=True) escapes the lone surrogate
    to an ASCII \\u escape and does NOT raise. The bug surfaces one step later,
    at the UTF-8 encode of the diary write. raw.encode('utf-8') is the minimal,
    version-robust reproduction of that fundamental failure. POSIX agents (Linux)
    are the deployment target; Windows CI skips this test.
    """
    raw = _posix_environ(b"summary\x97note")
    if "\uDC97" not in raw:
        pytest.skip("no surrogate present on this platform — cannot demonstrate bug")
    with pytest.raises(UnicodeEncodeError):
        raw.encode("utf-8")


def test_l161_without_fix_raises():
    """Without fix: text-mode read of 0x97 byte raises UnicodeDecodeError."""
    with pytest.raises(UnicodeDecodeError):
        io.TextIOWrapper(io.BytesIO(b"text\x97here"), encoding="utf-8").read()
