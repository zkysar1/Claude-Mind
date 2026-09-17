"""test_conftest_mind_api_env_scrub.py —  regression tests.

The core-tree twin of mind_api/tests/test_conftest_mind_api_env_scrub.py, and
the in-process counterpart to test_runtime_spawn_env_scrub.py /
test_daemon_start_env_scrub.py in this same directory. Those two pin the scrub
on the SUBPROCESS spawn chokepoints (`rt_spawn`, `mind-api-start.sh`), where
the daemon is a child and the launcher shapes what it inherits. Neither can
reach the path pinned here.

WHY THIS TREE IS EXPOSED. `_daemon_fixture.py` — used by 77 test files in this
directory — binds a ThreadingHTTPServer on a thread of the TEST process, not a
child, so no spawn-time scrub applies to it; and `_Handler` re-reads
MIND_API_TOKEN from `os.environ` on every request (mind_api/src/server.py). On
a box whose shell exports MIND_API_TOKEN the fixture daemon then demands a
bearer token its own test client never sends, and the request is refused
upstream of the code under test. The fixture also hands its environment to
wrapper subprocesses, so the same ambient value reaches both halves of every
CLI/daemon parity test. Measured on cc-03 2026-09-15: 432 failures carrying
1130 `missing or invalid bearer token` lines, against 8 failures and ZERO
bearer lines for the same selection under `env -u`. The suite was not broken —
it was unauthenticated, and that difference is invisible from a failure count.

STRATEGY and the negative control: see the mind_api twin's docstring. The two
conftests hold the same three lines because the test packages load
independently and neither imports the other's conftest; these files are what
keep the copies honest, so they are deliberately parallel.
"""
from __future__ import annotations

import os

import pytest

_SENTINEL = "inherited-from-the-box-shell-g-115-10006"

# Import-time pollution — stands in for the shell that exported the variable
# before pytest started. Collection runs before any fixture, which is the only
# way to place a value ahead of an autouse one. Self-containing: the scrub
# under test removes it before the first test body, so it cannot leak into the
# rest of the suite.
os.environ["MIND_API_TOKEN"] = _SENTINEL
os.environ["MIND_API_BIND"] = "127.0.0.1"   # loopback: harmless if some module reads it at import
os.environ["MIND_API_PORT"] = "59999"

# Set by the leak test, read by the containment test — without it the
# containment test passes VACUOUSLY if the two ever run out of order, since a
# test that never leaked sees the same absence it asserts.
_LEAK_RAN = False


def test_inherited_values_are_gone_before_the_first_test_body() -> None:
    """The import-time values above are absent by the time a test runs."""
    assert "MIND_API_TOKEN" not in os.environ
    assert "MIND_API_BIND" not in os.environ
    assert "MIND_API_PORT" not in os.environ


def test_leak_forward_the_way_a_careless_test_would() -> None:
    """Positive control: a raw os.environ write really does persist in-process.

    Without monkeypatch there is no teardown, so this value would reach every
    later test in this process. Asserting it here keeps the next test from
    pinning a containment the interpreter was providing for free.
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
    after it and must survive. Every bearer-auth test depends on this property.
    """
    monkeypatch.setenv("MIND_API_TOKEN", "s3cr3t-set-by-this-test")
    assert os.environ["MIND_API_TOKEN"] == "s3cr3t-set-by-this-test"
