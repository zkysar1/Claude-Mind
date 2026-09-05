"""test_stranded_claim_sweep_empty_corpus.py — a zero-claim corpus is the
DOCUMENTED SAFE CASE, not a sweep failure (g-115-8359).

THE DEFECT. `_query_claimed_goals` re-raised every `RtError` as `SystemExit`.
The daemon derives its `goal_field_name` allowlist FROM THE LIVE CORPUS, so
`claimed_by` is a valid field name exactly while a claim is outstanding and an
INVALID one (400 `unknown_goal_field`) exactly when there is nothing to sweep.
The sweep therefore died rc=1 on every iteration of an idle fleet — precisely
the state its own docstring promises is safe ("exits 0 with {scanned: 0}").
An inverted contract, measured on ZDS-Mind 2026-08-30: the 400 body enumerated
74 valid field names with `claimed_by` absent, while a direct store read showed
245 goals carrying zero `claimed_by` and zero `status=in-progress`.

WHY THE FALSE ALARM MATTERS MORE THAN THE CRASH. On a single-agent or
frequently-all-blocked deployment the zero-claim state is the STEADY state, so
the wrapper printed "stranded claims were NOT released" every iteration. A
permanent false alarm trains readers to ignore the one guard that would report a
genuinely broken sweep.

POSITIVE CONTROLS. The headline assertion is a NON-raise, and a function that
swallowed every error would satisfy it. So the tolerance is pinned as NARROW:
an unrelated RtError must still raise (B), and a healthy corpus must still
return its rows (C). A mutant that catches all RtErrors flips B red; one that
reverts the fix flips A red.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parents[1]


class _FakeRtError(RuntimeError):
    """Mirrors core/scripts/_rt.py::RtError — message, status AND body.

    The status/body attributes are the whole point (g-357-69). This double
    previously carried a message only, so a test could 'pass' against a
    tolerance that read str(e) — which production can never satisfy, because
    _rt.py puts the daemon's error CODE exclusively in the JSON body and the
    message is always "daemon HTTP <code> for <method> <path>". A double that
    is missing the attribute the production code must read cannot fail when
    that read is missing (guard-920 / rb-9476).
    """

    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class _RtStub:
    """Minimal _rt double: rt_call raises or returns per queued behaviour."""

    RtError = _FakeRtError

    def __init__(self, behaviour):
        self._behaviour = behaviour
        self.calls = 0

    def rt_call(self, method, path, query=None, **kw):
        self.calls += 1
        b = self._behaviour
        if isinstance(b, Exception):
            raise b
        return b


def _load_sweep(monkeypatch, behaviour):
    spec = importlib.util.spec_from_file_location(
        "stranded_claim_sweep_empty_corpus_probe",
        CORE_SCRIPTS / "stranded-claim-sweep.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    stub = _RtStub(behaviour)
    monkeypatch.setattr(mod, "_rt", stub)
    return mod, stub


# ── A: the defect — an empty corpus must NOT kill the sweep ────────────────
def test_unknown_goal_field_returns_empty_not_systemexit(monkeypatch):
    """THE ASSERTION UNDER TEST. The daemon's 400 means 'zero claims', which is
    the documented {scanned: 0} case — the sweep must survive it."""
    # THE LITERAL PRODUCTION SHAPE, byte-for-byte from _rt.py:105-107 —
    # message carries NO error code, status=400, body carries the JSON. The
    # previous fixture used "rt_call failed: 400 unknown_goal_field: ..."; that
    # message shape is emitted NOWHERE in the tree, so this test passed for the
    # whole time the tolerance was dead code ().
    err = _FakeRtError(
        "daemon HTTP 400 for GET /v1/aspirations/query",
        status=400,
        body=json.dumps({
            "error": "unknown_goal_field",
            "detail": "no goal record in any queue carries that key",
        }),
    )
    # Guard the fixture itself: if this ever becomes satisfiable by str(e), the
    # test has stopped exercising the defect it was written for.
    assert "unknown_goal_field" not in str(err), (
        "fixture regressed to a message-shaped error — production puts the code "
        "in the body only, so a str(e) tolerance would pass vacuously again"
    )
    mod, stub = _load_sweep(monkeypatch, err)
    out = mod._query_claimed_goals("alpha")
    assert out == []
    # Every strandable status was attempted, not short-circuited on the first.
    assert stub.calls == len(mod._STRANDABLE_STATUSES)


# ── B: positive control — the tolerance is NARROW, not blanket ─────────────
def test_unrelated_rt_error_still_raises(monkeypatch):
    """POSITIVE CONTROL. A daemon that is genuinely broken must still stop the
    sweep loudly; a catch-all here would hide every real transport failure."""
    mod, _ = _load_sweep(monkeypatch, _FakeRtError("connection refused"))
    with pytest.raises(SystemExit):
        mod._query_claimed_goals("alpha")


# ── C: positive control — a healthy corpus still returns its rows ──────────
def test_healthy_corpus_still_returns_rows(monkeypatch):
    """POSITIVE CONTROL for A. The fix must not have made the query inert."""
    rows = [{"goal_id": "g-1-1", "claimed_by": "alpha"}]
    mod, _ = _load_sweep(monkeypatch, json.dumps(rows))
    out = mod._query_claimed_goals("alpha")
    assert [g["goal_id"] for g in out] == ["g-1-1"]  # deduped across statuses


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
