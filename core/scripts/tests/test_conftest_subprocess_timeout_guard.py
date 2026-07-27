"""Tests for the conftest default-subprocess-timeout guard ().

WHY THE GUARD EXISTS: on this box, spawning bash from Windows Python
intermittently hangs AT BASH STARTUP (measured 2026-07-25 — `bash -x` emits
zero trace, so the hang precedes the first command). Hung bashes sit at 0 CPU
and accumulate. The parent then blocks forever in communicate(), and pytest's
faulthandler bound (600s, pytest.ini) ABORTS THE WHOLE RUN — one unlucky spawn
destroys a ~90-minute suite and blocks run-full-suite-after-deep-code.md for
everyone on the box.

149 of 333 subprocess.run call sites passed no timeout. The guard patches the
shared surface once rather than editing 149 sites, and covers future tests too.

These tests pin the properties that make it safe: the injection decision, the
two orderings the default must satisfy, fail-safe env parsing, and that the
wrapper is genuinely installed during a test.
"""
import subprocess
import sys

import pytest

import conftest as cf


# ── the injection decision (pure, no spawning) ───────────────────────────────

def test_injects_when_timeout_absent():
    assert cf._inject_timeout({}, 300.0) == {"timeout": 300.0}


def test_injects_when_timeout_is_explicitly_none():
    # `timeout=None` is the stdlib's "unbounded" — the exact hang case. It must
    # be treated as absent, not as a deliberate caller choice.
    assert cf._inject_timeout({"timeout": None}, 300.0)["timeout"] == 300.0


def test_never_overrides_an_explicit_caller_timeout():
    # The guard fills a gap; it does not impose policy on a caller who chose.
    assert cf._inject_timeout({"timeout": 5}, 300.0)["timeout"] == 5


def test_preserves_other_kwargs():
    out = cf._inject_timeout({"capture_output": True, "text": True}, 300.0)
    assert out["capture_output"] is True and out["text"] is True
    assert out["timeout"] == 300.0


# ── the two orderings the default MUST satisfy ───────────────────────────────

def test_default_fires_before_the_faulthandler_abort():
    # THE load-bearing ordering. If this inverts, the guard is useless: pytest
    # would abort the process at 600s before our timeout ever raised.
    assert cf._default_subprocess_timeout() < 600


def test_default_exceeds_the_slowest_known_legitimate_test():
    # Slowest legit test on record is 139.61s (run-full-suite-after-deep-code.md).
    # A default below that would start failing healthy tests.
    assert cf._default_subprocess_timeout() > 139.61


# ── env handling ─────────────────────────────────────────────────────────────

def test_env_override_tunes_the_value(monkeypatch):
    monkeypatch.setenv(cf._SUBPROC_TIMEOUT_ENV, "42")
    assert cf._default_subprocess_timeout() == 42.0


def test_env_zero_disables_the_guard(monkeypatch):
    monkeypatch.setenv(cf._SUBPROC_TIMEOUT_ENV, "0")
    assert cf._default_subprocess_timeout() is None


def test_malformed_env_falls_back_to_default(monkeypatch):
    # FAIL SAFE: a typo must not silently disable suite-abort protection.
    monkeypatch.setenv(cf._SUBPROC_TIMEOUT_ENV, "not-a-number")
    assert cf._default_subprocess_timeout() == 300.0


# ── the wrapper is actually live ─────────────────────────────────────────────

def test_wrapper_is_installed_during_a_test():
    # If subprocess.run ever reads as the stdlib function here, the autouse
    # fixture is not running and every unbounded call is exposed again.
    assert subprocess.run.__name__ == "_run"
    assert subprocess.call.__name__ == "_call"


def test_a_bounded_call_still_raises_on_a_slow_child():
    # End-to-end: the mechanism that converts a hang into an attributable
    # failure works (explicit short timeout stands in for the injected one, so
    # the test stays fast).
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run([sys.executable, "-c", "import time; time.sleep(20)"],
                       capture_output=True, timeout=1)


def test_a_normal_call_is_unaffected():
    # The guard must not change behaviour of healthy calls.
    p = subprocess.run([sys.executable, "-c", "print('ok')"],
                       capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip() == "ok"
