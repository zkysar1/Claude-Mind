"""Boundary of the no-daemon tolerance in _wm_cooldown.neutralize_cooldown (-a).

The helper returns quietly for exactly one failure, the shared "no daemon" text that
rt_no_daemon_error prints, and asserts on everything else. A tolerance that swallowed
more would turn the 12 gate tests green in any runtime while testing nothing, so the
boundary is pinned here against fake wm-set.sh scripts. No daemon is involved.

The last two tests run the REAL wm-set.sh in a runtime with no daemon. The marker is a
copy of text that core/scripts/_runtime.sh owns and the fakes cannot see it drift; a box
with a live daemon never reaches the tolerance either (g-374-628-a).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _wm_cooldown  # noqa: E402


def _fake_wm_set(tmp_path, rc, stderr):
    script = tmp_path / "wm-set.sh"
    # Bytes, not write_text: text mode writes CRLF on Windows and bash then rejects
    # `exit 0\r` (rc 2), failing two of these tests and passing the other three for
    # the wrong reason (guard-1688, -a fresh-eyes).
    text = f"#!/usr/bin/env bash\ncat > /dev/null\necho {shlex.quote(stderr)} >&2\nexit {rc}\n"
    script.write_bytes(text.encode("utf-8"))
    return script


def test_a_successful_write_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(_wm_cooldown, "WM_SET_SH", _fake_wm_set(tmp_path, 0, ""))
    _wm_cooldown.neutralize_cooldown("testagent")


def test_the_shared_no_daemon_failure_is_tolerated(tmp_path, monkeypatch):
    stderr = "ERROR: " + _wm_cooldown.NO_DAEMON_TEXT + "."
    monkeypatch.setattr(_wm_cooldown, "WM_SET_SH", _fake_wm_set(tmp_path, 1, stderr))
    _wm_cooldown.neutralize_cooldown("testagent")


@pytest.mark.parametrize("stderr", [
    "ERROR: write refused",
    # The other branch of rt_no_daemon_error (a slow or contended daemon): it names the
    # daemon, so a marker loosened to the bare word would tolerate it.
    "Retry - warmup is paid once per daemon idle period; contention is transient",
    "",
])
def test_any_other_failure_still_asserts(tmp_path, monkeypatch, stderr):
    monkeypatch.setattr(_wm_cooldown, "WM_SET_SH", _fake_wm_set(tmp_path, 1, stderr))
    with pytest.raises(AssertionError, match="could not neutralize testagent"):
        _wm_cooldown.neutralize_cooldown("testagent")


@pytest.fixture
def no_daemon_runtime(tmp_path, monkeypatch):
    """A runtime in which no daemon can be reached, whatever this box is running.

    The shell wrappers read their daemon dir from RT_DIR and never from RUNTIME_DIR, which
    is the launcher's variable: a scratch RUNTIME_DIR alone left wm-read.sh answering from
    the live daemon (measured in g-374-628-a, the split guard-1547 documents). So RT_DIR is
    the load-bearing pin, RUNTIME_DIR rides with it, and RT_NO_AUTOSPAWN=1 keeps the wrapper
    from starting a daemon of its own.
    """
    rt = tmp_path / "rt"
    rt.mkdir()
    for name in ("RT_DIR", "RUNTIME_DIR"):
        monkeypatch.setenv(name, _wm_cooldown._to_bash_path(rt))
    monkeypatch.setenv("RT_NO_AUTOSPAWN", "1")


def test_the_marker_is_what_the_real_wrapper_prints_without_a_daemon(no_daemon_runtime):
    """The marker copies text core/scripts/_runtime.sh owns; this runs the real producer (-a).

    The fakes above cannot see that copy drift, and a box with a live daemon never reaches
    the tolerance, so a reword of the real text would stay green everywhere except a
    deployment-shaped run, where the 12 gate tests would read as setup errors that name
    nothing. Running the real wm-set.sh with no daemon is what turns it red here (g-374-628-a).
    """
    r = subprocess.run(
        [_wm_cooldown.GIT_BASH, _wm_cooldown._to_bash_path(_wm_cooldown.WM_SET_SH),
         "fresh_eyes_last_fire"],
        input="null", capture_output=True, text=True, timeout=60,
        env={**os.environ, "MIND_AGENT": "testagent"},
    )
    # Positive control: a daemon that answered would make everything below say nothing
    # about the marker.
    assert r.returncode != 0, (
        f"wm-set.sh succeeded, so this runtime was not daemonless (rc={r.returncode}): "
        f"{r.stderr!r}"
    )
    assert _wm_cooldown.NO_DAEMON_TEXT in r.stderr, (
        "the real no-daemon error no longer carries the marker; update NO_DAEMON_TEXT in "
        "core/scripts/tests/_wm_cooldown.py to match core/scripts/_runtime.sh. "
        f"stderr={r.stderr!r}"
    )
    _wm_cooldown.neutralize_cooldown("testagent")


def test_a_reworded_marker_is_not_tolerated_by_the_real_wrapper(no_daemon_runtime, monkeypatch):
    """A drifted marker must fail loudly against the real wrapper, never be swallowed (-a).

    It is also the second positive control for the runtime: were a daemon reachable, the
    helper's write would succeed, nothing would raise, and this test would read red. The
    wrong marker is a phrase no producer would adopt, so a real reword of the source can
    never coincide with it and turn this test red for the wrong reason (g-374-628-a).
    """
    monkeypatch.setattr(_wm_cooldown, "NO_DAEMON_TEXT", "a phrase no wrapper ever prints")
    with pytest.raises(AssertionError, match="could not neutralize testagent"):
        _wm_cooldown.neutralize_cooldown("testagent")
