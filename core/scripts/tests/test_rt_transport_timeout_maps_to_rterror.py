""": a READ-PHASE transport timeout must reach callers as RtError.

WHY THIS EXISTS
---------------
`_rt.rt_call` is the canonical Python -> daemon client. Every caller that wants
to fail soft on a daemon problem writes `except _rt.RtError`. That contract was
incomplete: `urlopen` was guarded by `urllib.error.HTTPError` and
`urllib.error.URLError`, which cover the CONNECT phase. Once the request has
been sent and accepted, a socket timeout surfaces as a BARE `TimeoutError` out
of `http.client.getresponse -> socket.recv_into` and matched no clause — so it
escaped `rt_call` as an unhandled exception.

MEASURED (foxtrot, LAPTOP-3IOFCNEO, 2026-08-21): `cargo-cult-detector.py:309`
wraps its only daemon call in exactly that handler. It printed "falling back to
Idea path" and then died on an unhandled TimeoutError anyway — the Idea was
never filed, the counter never reset, and `recurring-close.sh`'s auto-contract
path relayed the traceback while still exiting 0.

WHAT IS PINNED
--------------
1. A bare `TimeoutError` raised at the urlopen boundary becomes `RtError`.
2. `HTTPError` still maps to `RtError` carrying status + body, NOT to the new
   broad arm. `HTTPError` and `URLError` are both `OSError` subclasses, so the
   new `except (TimeoutError, OSError)` arm MUST stay BELOW them; if a future
   edit reorders the clauses, the richer diagnostics are silently swallowed and
   this test fails.
3. `URLError` still maps to `RtError` carrying its `reason`.

Test 2 is the load-bearing one: test 1 alone would still pass if someone
"simplified" the handlers into a single broad `except OSError`, which would be a
real regression.
"""

from __future__ import annotations

import io
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import _rt  # noqa: E402


def _force_urlopen(monkeypatch, exc):
    """Make the urlopen inside rt_call raise `exc`."""

    def _boom(*_a, **_kw):
        raise exc

    monkeypatch.setattr(urllib.request, "urlopen", _boom)


def test_bare_timeout_error_becomes_rterror(monkeypatch):
    """The measured  case: read-phase TimeoutError must not escape."""
    _force_urlopen(monkeypatch, TimeoutError("timed out"))

    with pytest.raises(_rt.RtError) as ei:
        _rt.rt_call("GET", "/v1/anything")

    # The message must name the failure so a stderr line is actionable.
    assert "transport failure" in str(ei.value)
    assert "/v1/anything" in str(ei.value)


def test_bare_timeout_is_not_reraised_as_timeouterror(monkeypatch):
    """Explicitly pin the NEGATIVE: callers' `except RtError` must suffice.

    Before the fix this raised TimeoutError, which is exactly what
    cargo-cult-detector.py:309 could not catch.
    """
    _force_urlopen(monkeypatch, TimeoutError("timed out"))

    try:
        _rt.rt_call("GET", "/v1/anything")
    except _rt.RtError:
        pass  # correct
    except TimeoutError:  # pragma: no cover - regression path
        pytest.fail(
            "rt_call leaked a bare TimeoutError; callers doing "
            "`except _rt.RtError` cannot fail soft (g-115-7136)"
        )
    else:  # pragma: no cover - the monkeypatch always raises
        pytest.fail("rt_call did not raise at all")


def test_httperror_still_maps_with_status_and_body(monkeypatch):
    """Clause ORDER guard: HTTPError is an OSError subclass and must win.

    If the new broad arm is ever moved above the HTTPError clause, status/body
    are lost and this fails.
    """
    err = urllib.error.HTTPError(
        url="http://x/v1/thing",
        code=418,
        msg="teapot",
        hdrs=None,
        fp=io.BytesIO(b"brewing"),
    )
    _force_urlopen(monkeypatch, err)

    with pytest.raises(_rt.RtError) as ei:
        _rt.rt_call("POST", "/v1/thing")

    assert getattr(ei.value, "status", None) == 418, (
        "HTTPError lost its status — the broad transport arm is catching it, "
        "i.e. the except-clause order regressed (g-115-7136)"
    )
    assert "brewing" in (getattr(ei.value, "body", "") or "")


def test_urlerror_still_maps_with_reason(monkeypatch):
    """Clause ORDER guard, connect-phase twin of the HTTPError case."""
    _force_urlopen(monkeypatch, urllib.error.URLError("connection refused"))

    with pytest.raises(_rt.RtError) as ei:
        _rt.rt_call("GET", "/v1/thing")

    msg = str(ei.value)
    assert "unreachable" in msg, (
        "URLError no longer takes the connect-phase branch — the broad "
        "transport arm is catching it (g-115-7136)"
    )
    assert "connection refused" in msg
