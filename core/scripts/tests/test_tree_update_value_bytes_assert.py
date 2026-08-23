""" — the CLIENT half of the tree-write value-integrity check.

tree-update.sh compares the byte length it computed from argv against the length
the daemon reports storing, and fails loud on mismatch. That comparison is the
ONLY one in the chain that is not a string compared to itself: the in-daemon
version (len(req["value"]) vs len(node[field]) after _apply_set) is vacuous
because _apply_set assigns the request value straight onto the node.

It is also the only half that covers the ACTUAL 2026-08-19 incident. That goal's
own analysis places the loss at or before client-side serialization -- a
truncated HTTP body could not have returned 200, because it would be invalid
JSON -- so a daemon-side check, however well built, is blind to it.

WHY THIS TEST EXTRACTS THE REAL SNIPPET instead of restating the logic: the
comparison lives as a python heredoc inside a bash function, which pytest cannot
collect. Re-implementing it here would pin a COPY and stay green while the
shipped snippet drifted -- the exact shape guard-1943 names (pinning the writer
says nothing about the wiring). So the test reads tree-update.sh, lifts the
snippet verbatim, and executes THAT.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TREE_UPDATE = REPO_ROOT / "core" / "scripts" / "tree-update.sh"


def _extract_assert_snippet() -> str:
    """Lift the python body of _assert_value_bytes() out of tree-update.sh."""
    text = TREE_UPDATE.read_text(encoding="utf-8")
    m = re.search(
        r"_assert_value_bytes\(\)\s*\{.*?-c '(?P<py>.*?)'\s*\n\}",
        text,
        re.DOTALL,
    )
    assert m, ("could not locate _assert_value_bytes' python body in "
               "tree-update.sh — the function was renamed or restructured, and "
               "this test must be re-pointed rather than deleted")
    return m.group("py")


def _run(sent, got):
    """Execute the REAL snippet with a synthetic body/response pair."""
    snippet = _extract_assert_snippet()
    body = {"op": "set", "key": "k", "field": "summary", "value": "..."}
    if sent is not None:
        body["value_bytes"] = sent
    resp = {"ok": True, "op": "set", "key": "k"}
    if got is not None:
        resp["value_bytes"] = got
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        input=json.dumps(resp),
        env={"TU_BODY": json.dumps(body), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    return proc


def test_snippet_is_present_and_extractable():
    """Guards the extraction itself — if this fails the others are vacuous."""
    snippet = _extract_assert_snippet()
    assert "value_bytes" in snippet
    assert "sys.exit(1)" in snippet, "the mismatch path must exit non-zero"


def test_matching_lengths_pass_silently():
    proc = _run(sent=17708, got=17708)
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_short_write_fails_loud_and_names_the_loss():
    """The incident's exact numbers: 17,708 sent, 8,186 stored, 9,522 lost."""
    proc = _run(sent=17708, got=8186)
    assert proc.returncode == 1, "a short write must exit non-zero"
    err = proc.stderr
    assert "VALUE INTEGRITY FAILURE" in err
    assert "17708" in err and "8186" in err and "9522" in err, err
    # The operator needs the recovery path in the failure itself, not a lookup.
    assert ".history" in err
    # And must be steered off the destructive recovery.
    assert "history.py restore" in err and "Do NOT" in err


def test_missing_daemon_field_fails_open():
    """An older daemon reports no value_bytes. Fail OPEN: a false alarm here
    would fire on every tree write in the fleet. This is a real coverage gap and
    is pinned so that closing it is a conscious edit."""
    proc = _run(sent=17708, got=None)
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_missing_client_field_fails_open():
    proc = _run(sent=None, got=17708)
    assert proc.returncode == 0


def test_unparseable_response_fails_open():
    snippet = _extract_assert_snippet()
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        input="not json at all",
        env={"TU_BODY": json.dumps({"value_bytes": 10}), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, "a parse failure must never break the write path"


def test_client_declares_utf8_bytes_not_characters():
    """_build_body must count BYTES. If it counted characters, every multibyte
    value would false-alarm against the daemon's byte count."""
    text = TREE_UPDATE.read_text(encoding="utf-8")
    m = re.search(r'body\["value_bytes"\]\s*=\s*(.+)', text)
    assert m, "value_bytes assignment missing from _build_body"
    expr = m.group(1)
    assert "encode" in expr, (
        f"value_bytes must be a UTF-8 BYTE count, got: {expr.strip()}")
