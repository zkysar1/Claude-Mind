"""test_conftest_mind_api_env_scrub.py —  regression tests.

The IN-PROCESS twin of core/scripts/tests/test_runtime_spawn_env_scrub.py and
test_daemon_start_env_scrub.py. Those two pin the environment scrub on the
SUBPROCESS spawn chokepoints (`rt_spawn`, `mind-api-start.sh`), where the
daemon is a child process and the launcher can shape what it inherits. This
file pins the one path neither can reach.

INCIDENT. `running_daemon` (conftest.py) does not spawn anything: it binds a
ThreadingHTTPServer on a thread of the TEST process itself, and `_Handler`
re-reads MIND_API_TOKEN from `os.environ` on EVERY request (server.py), so the
fixture daemon and the test client share one environment that no spawn-time
scrub ever touches. On a box whose shell exports MIND_API_TOKEN, the daemon
therefore demands a bearer token the test client never sends, and every
fixture-backed request is refused upstream of the code under test. Measured on
cc-03 2026-09-15: 432 failures carrying 1130 `missing or invalid bearer token`
lines, against 8 failures and ZERO bearer lines for the same selection run
under `env -u`. The suite was not broken — it was unauthenticated, and the
difference is invisible from a failure count.

STRATEGY. The scrub runs BEFORE each test body, so a test cannot pollute its
own environment ahead of it. The two things that can are an import-time write
(collection runs before any fixture) and a previous test — so this file uses
both, which are also the two shapes the real defect takes: a value inherited
from the shell, and a value leaked forward by a careless sibling test.

test_a_deliberate_setenv_still_stands is the load-bearing NEGATIVE control
(guard-1220 — a predicate must reject as well as accept). The fixture pops
unconditionally, so without this assertion a scrub that also clobbered a test's
OWN setenv would pass everything else here while silently breaking every
bearer-auth test in test_runtime_auth.py, whose whole subject is the variable
being set.
"""
from __future__ import annotations

import os

import pytest

_SENTINEL = "inherited-from-the-box-shell-g-115-10006"

# Import-time pollution — stands in for the shell that exported the variable
# before pytest started. This runs at COLLECTION, before any fixture, which is
# the only way to place a value ahead of an autouse fixture. It is self-
# containing: the scrub under test removes it before the first test body, so it
# cannot leak into the rest of the suite (and if the scrub is ever removed, the
# leak it causes is the defect, reported by the assertions below).
os.environ["MIND_API_TOKEN"] = _SENTINEL
os.environ["MIND_API_BIND"] = "127.0.0.1"   # loopback: harmless if some module reads it at import
os.environ["MIND_API_PORT"] = "59999"

# Set by the leak test, read by the containment test. Without it the
# containment test would pass VACUOUSLY if the two ever ran out of order —
# it asserts an ABSENCE, which is exactly what a test that never leaked also
# sees. Definition order is pytest's default and no ordering plugin is
# installed here, so the pairing holds; this pins that it held.
_LEAK_RAN = False


def test_inherited_values_are_gone_before_the_first_test_body() -> None:
    """The import-time values above are absent by the time a test runs."""
    assert "MIND_API_TOKEN" not in os.environ
    assert "MIND_API_BIND" not in os.environ
    assert "MIND_API_PORT" not in os.environ


def test_leak_forward_the_way_a_careless_test_would() -> None:
    """Positive control: a raw os.environ write really does persist in-process.

    Without monkeypatch there is no teardown, so this value would reach every
    later test in this process. That is the mechanism the scrub contains, and
    asserting it here keeps the next test from pinning a containment that the
    interpreter was providing for free.
    """
    global _LEAK_RAN
    os.environ["MIND_API_TOKEN"] = _SENTINEL
    os.environ["MIND_API_PORT"] = "59999"
    _LEAK_RAN = True
    assert os.environ["MIND_API_TOKEN"] == _SENTINEL


def test_a_prior_tests_leak_does_not_reach_the_next_test() -> None:
    """The leak above is gone — the scrub fires between tests, not just once."""
    assert _LEAK_RAN, (
        "precondition: test_leak_forward_the_way_a_careless_test_would must run "
        "before this test, or the absence asserted below proves nothing"
    )
    assert "MIND_API_TOKEN" not in os.environ
    assert "MIND_API_PORT" not in os.environ


def test_a_deliberate_setenv_still_stands(monkeypatch: pytest.MonkeyPatch) -> None:
    """NEGATIVE CONTROL: the scrub must not fight a test that sets it itself.

    The scrub runs before the test body; a value set INSIDE the body is set
    after it and must survive. This is the property every bearer-auth test in
    test_runtime_auth.py depends on.
    """
    monkeypatch.setenv("MIND_API_TOKEN", "s3cr3t-set-by-this-test")
    assert os.environ["MIND_API_TOKEN"] == "s3cr3t-set-by-this-test"
