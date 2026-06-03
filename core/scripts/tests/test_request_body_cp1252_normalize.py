"""test_request_body_cp1252_normalize.py — B5 (Lodestar soak).

The daemon's POST handler used to hand the raw request bytes straight to each
endpoint's strict ``body.decode("utf-8")``. A client on a Windows cp1252 shell
sends an em-dash as the lone byte 0x97 (not the UTF-8 sequence E2 80 94), which
raises UnicodeDecodeError -> 500 -> the agent's write (board post, rb lesson,
working-memory, experience) is silently DROPPED. Observed 8x / 2500 reqs in the
6-agent soak across 5 endpoints, plus mojibake baked into stored goal titles.

server._normalize_request_body() now normalizes the body to valid UTF-8 ONCE at
the single door, RECOVERING the intended character (cp1252 0x97 -> U+2014) rather
than blanking it. This pins that contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mind_api.src.server import _normalize_request_body  # noqa: E402


def test_cp1252_emdash_recovered_to_utf8():
    """Byte 0x97 (cp1252 em-dash) -> valid UTF-8 U+2014, losslessly."""
    raw = b'{"text": "before\x97after"}'
    out = _normalize_request_body(raw)
    # Must now be valid UTF-8 (no exception) and parse cleanly.
    decoded = out.decode("utf-8")
    obj = json.loads(decoded)
    assert obj["text"] == "before—after", \
        f"em-dash not recovered: {obj['text']!r}"
    # The recovered byte sequence IS the canonical UTF-8 em-dash.
    assert b"\xe2\x80\x94" in out, "expected UTF-8 em-dash bytes E2 80 94 in output"


def test_valid_utf8_passthrough_unchanged():
    """A body that is already valid UTF-8 is returned byte-identical."""
    raw = '{"text": "clean em—dash"}'.encode("utf-8")
    out = _normalize_request_body(raw)
    assert out == raw, "valid UTF-8 body must pass through unchanged"
    assert json.loads(out.decode("utf-8"))["text"] == "clean em—dash"


def test_empty_body_unchanged():
    assert _normalize_request_body(b"") == b""


def test_cp1252_undefined_byte_does_not_crash():
    """0x81 is undefined in cp1252; errors='replace' covers it (no exception)."""
    raw = b'{"x": "\x81"}'  # invalid UTF-8 AND undefined cp1252
    out = _normalize_request_body(raw)
    # Must not raise; output is valid UTF-8 (the undefined byte -> U+FFFD).
    decoded = out.decode("utf-8")
    assert json.loads(decoded)  # parses (value is the replacement char)


def test_multiple_cp1252_punctuation():
    """En-dash 0x96, em-dash 0x97, curly quotes 0x93/0x94 all recover."""
    raw = b'{"a": "\x96", "b": "\x97", "c": "\x93x\x94"}'
    obj = json.loads(_normalize_request_body(raw).decode("utf-8"))
    assert obj["a"] == "–"  # en-dash
    assert obj["b"] == "—"  # em-dash
    assert obj["c"] == "“x”"  # curly double quotes


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = []
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failures.append((t.__name__, f"{type(e).__name__}: {e}"))
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(_run_all())
