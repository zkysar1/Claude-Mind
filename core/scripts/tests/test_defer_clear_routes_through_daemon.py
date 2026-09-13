""" + : pin that ALL THREE defer sweeps clear via the typed
daemon client, and that a FAILED clear is loud rather than a silent no-op.

THE HEADLINE SAID "BOTH" UNTIL 2026-09-12 AND THAT WAS THE BUG IN THIS FILE.
It loaded precondition-defer-recheck and defer-recheck; the third sweep,
dependency-timeout-check, was never loaded. So this pin was NARROWER than the
population it claimed to cover and passed green for two weeks over a
`_clear_defer` that had never once cleared a defer (g-115-8314) — the exact
shape reclaim-routed-work.md rule 7 and guard-1802 name: a predicate narrower
than the gate that creates its population reports clean forever. When a fourth
sweep appears, ADD IT HERE; a count word in a docstring is not coverage.

Incident (measured 2026-09-10 cc-13, bravo): precondition-defer-recheck.sh
--apply reported would_clear [g-115-9055, g-368-13] and cleared 0, every
detail action=clear_failed, at rc 0. Both `_clear_defer` helpers shelled to
`aspirations.py update-goal` via sys.executable, which on that box printed
`Error: 'NoneType' object has no attribute 'get'` and exited 0 — so the
sweep's `rc == 0` test read a failure as success, and the Layer-D auto-clear
of precondition_unmet defers silently did nothing. It held the asp-368
product lane until a hand clear.

WHY THIS IS A UNIT TEST AND NOT A LIVE ONE — read before "improving" it into
an end-to-end run. The defect is BOX-DEPENDENT: re-measured 2026-09-11 on
cc-02, the identical CLI argv SUCCEEDED and the write landed, on the same
STORAGE_BACKEND=own-cloud. So a live --apply run passes on a healthy box
whether or not the fix is present, which makes it worthless as a regression
pin — it would have passed on cc-02 the day cc-13 was broken. What is
box-INDEPENDENT is the call shape and the failure contract, and that is what
these tests hold.

Coverage contract:
  1. All three sweeps' _clear_defer route through _rt.aspirations_update_goal
     — NOT a subprocess. A regression to any sys.executable / bash hop fails
     here because the stub is never called.
  2. precondition-defer-recheck and dependency-timeout-check clear BOTH
     defer_reason and defer_reason_set_at, each with value None (guard-5211:
     None is what json-encodes to the `null` body that clears a field). Note
     the value must be None and NOT the string "null": the shell wrapper needs
     a non-empty argv token so it passes the literal text, but a Python caller
     handing "null" to _rt would STORE that text as the defer reason.
  3. A daemon refusal (RtError) yields ok=False AND propagates the daemon's
     response body — never a bare boolean (rb-10397: aggregating per-record
     failures into a count hides the body that says why).
  4. The forbidden repair stays forbidden: neither module may regain a
     sys.executable hop into a migrated CLI (guard-1322 / guard-555).

Hermetic: _rt.aspirations_update_goal is monkeypatched. No daemon, no store
write, no world/meta touch.

Run: STORAGE_BACKEND=local py -3 -m pytest \
    core/scripts/tests/test_defer_clear_routes_through_daemon.py -v
"""
import ast
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _rt  # noqa: E402  (the module both sweeps must route through)


def _load(stem, filename):
    """Import a hyphenated core/scripts module (plain import cannot)."""
    spec = importlib.util.spec_from_file_location(
        stem, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PRECOND = _load("precondition_defer_recheck_g9621", "precondition-defer-recheck.py")
DEFER = _load("defer_recheck_g9621", "defer-recheck.py")
# : the THIRD sweep. It was absent from this file until 2026-09-12
# while the docstring above said "BOTH defer sweeps" — so the pin was narrower
# than its own population and reported clean for two weeks over a
# dependency-timeout-check._clear_defer that had never cleared a defer at all.
# NOTE the argument order is (root_id, source) here, the REVERSE of the other
# two sweeps' (source, goal_id). Do not "harmonise" it in a test — assert the
# real signature, or the test stops exercising the shipped call.
DEPTIMEOUT = _load("dependency_timeout_check_g8314", "dependency-timeout-check.py")


class _Recorder:
    """Stands in for _rt.aspirations_update_goal; records every call."""

    def __init__(self, raise_on=None):
        self.calls = []
        self.raise_on = raise_on

    def __call__(self, goal_id, field, value, source="world"):
        self.calls.append((goal_id, field, value, source))
        if self.raise_on is not None and field == self.raise_on:
            raise _rt.RtError(
                "daemon refused", status=409,
                body='{"error":"gate_refused","detail":"blocker_ref_required"}')
        return {"ok": True, "goal_id": goal_id, "field": field,
                "goal": {"id": goal_id, field: value}}


# --- 1 + 2: routes through the typed client, with the clearing value --------

def test_precondition_sweep_clears_both_fields_via_daemon_client(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(_rt, "aspirations_update_goal", rec)

    ok, detail = PRECOND._clear_defer("world", "g-test-9621")

    assert ok is True
    assert detail is None
    assert [(c[1], c[2]) for c in rec.calls] == [
        ("defer_reason", None),
        ("defer_reason_set_at", None),
    ], "must clear BOTH fields, each with None (-> `null` body, guard-5211)"
    assert all(c[0] == "g-test-9621" and c[3] == "world" for c in rec.calls)


def test_defer_sweep_clears_defer_reason_via_daemon_client(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(_rt, "aspirations_update_goal", rec)

    ok, detail = DEFER._clear_defer("agent", "g-test-9621b")

    assert ok is True
    assert detail is None
    assert rec.calls == [("g-test-9621b", "defer_reason", None, "agent")]


def test_dependency_timeout_sweep_clears_both_fields_via_daemon_client(
        monkeypatch):
    """. Before 2026-09-12 this helper called
    `bash_cmd(aspirations-update-goal.sh, root_id, 'defer_reason', '')`, and
    the wrapper refuses an EMPTY value (`[ -z "$VALUE" ]`) before the engine is
    reached — so it returned (False, 'clear rc=1 ...') every single time and no
    falsified defer was ever cleared. Two independent assertions below would
    have caught it: the stub is never called on the subprocess path, and the
    old shape cleared only ONE field even in principle."""
    rec = _Recorder()
    monkeypatch.setattr(_rt, "aspirations_update_goal", rec)

    # Signature is (root_id, source) — reversed vs the other two sweeps.
    ok, detail = DEPTIMEOUT._clear_defer("g-test-8314", "world")

    assert ok is True, detail
    assert [(c[1], c[2]) for c in rec.calls] == [
        ("defer_reason", None),
        ("defer_reason_set_at", None),
    ], ("must clear BOTH fields with None; the pre-fix shape passed the empty "
        "string to a wrapper that refuses it, and omitted the _set_at twin "
        "entirely (g-115-8314)")
    assert all(c[0] == "g-test-8314" and c[3] == "world" for c in rec.calls)


def test_dependency_timeout_refusal_is_loud_and_carries_body(monkeypatch):
    rec = _Recorder(raise_on="defer_reason")
    monkeypatch.setattr(_rt, "aspirations_update_goal", rec)

    ok, detail = DEPTIMEOUT._clear_defer("g-test-8314b", "world")

    assert ok is False, "a refused clear must never report success"
    assert "gate_refused" in (detail or ""), (
        "the daemon's own body must reach the caller (rb-10397)")
    assert [c[1] for c in rec.calls] == ["defer_reason"], (
        "must stop at the first failure rather than clearing _set_at beside a "
        "live defer_reason")


# --- 3: a refusal is loud, and carries the daemon body ----------------------

@pytest.mark.parametrize("mod,raise_on", [
    (PRECOND, "defer_reason"),
    (PRECOND, "defer_reason_set_at"),   # failure on the SECOND call still fails
    (DEFER, "defer_reason"),
])
def test_daemon_refusal_returns_false_and_surfaces_body(mod, raise_on,
                                                        monkeypatch):
    rec = _Recorder(raise_on=raise_on)
    monkeypatch.setattr(_rt, "aspirations_update_goal", rec)

    ok, detail = mod._clear_defer("world", "g-test-9621c")

    assert ok is False, "a refused clear must never report success"
    assert detail, "the failure detail must not be empty"
    assert "gate_refused" in detail, (
        "the daemon's own body must reach the caller, not just a boolean "
        "(rb-10397)")


def test_precondition_sweep_stops_at_first_failure(monkeypatch):
    """A failed defer_reason clear must not silently proceed to the second
    field — that would leave defer_reason_set_at cleared beside a live
    defer_reason, a shape no reader expects."""
    rec = _Recorder(raise_on="defer_reason")
    monkeypatch.setattr(_rt, "aspirations_update_goal", rec)

    ok, _ = PRECOND._clear_defer("world", "g-test-9621d")

    assert ok is False
    assert [c[1] for c in rec.calls] == ["defer_reason"]


# --- 4: the forbidden repair cannot come back -------------------------------

@pytest.mark.parametrize("filename", [
    "precondition-defer-recheck.py",
    "defer-recheck.py",
    "dependency-timeout-check.py",
])
def test_no_sys_executable_hop_into_a_migrated_cli(filename):
    """guard-1322 / guard-555: neither sweep may invoke a migrated core/scripts
    CLI or wrapper through a subprocess. Asserted on the AST rather than by
    grepping source text, so a comment mentioning the old pattern (both files
    deliberately keep one, explaining why it was removed) cannot fail this."""
    tree = ast.parse((SCRIPT_DIR / filename).read_text(encoding="utf-8"))

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Any argv list containing sys.executable, or a literal "bash".
        for arg in node.args:
            if not isinstance(arg, (ast.List, ast.Tuple)):
                continue
            for elt in arg.elts:
                if (isinstance(elt, ast.Attribute)
                        and elt.attr == "executable"):
                    offenders.append(f"{filename}:{node.lineno} sys.executable")
                if isinstance(elt, ast.Constant) and elt.value == "bash":
                    offenders.append(f"{filename}:{node.lineno} bare bash")

    assert not offenders, (
        "forbidden subprocess hop reintroduced: %s — route through a typed "
        "_rt client instead (guard-1322/guard-555, g-115-9621)" % offenders)
