"""Compare-and-swap on the in_flight clear () — store level + wiring.

`test_worker_close_in_flight_clear.py` proves the CALLER behaves (it reports
`raced` and leaves the foreign row alone). This file proves the two things that
sit underneath that and which a caller-level test cannot see:

  1. the guard actually holds when driven through the REAL `locked_modify_yaml`
     cycle against a real file, not a dict in a fixture;
  2. both twins install the SAME modifier, so the daemon — the only live path
     under daemon-only architecture (guard-2323) — cannot drift from the CLI.

Plus the defect-B wiring check: the stop-hook invocation must not send the
helper's stderr to /dev/null, because on a hook path nobody watches, a
permanently-broken invocation is otherwise indistinguishable from a clean
nothing-to-clear run (verify-before-assuming Rule 4).

guard-1165: no module-level os.environ mutation, no sys.modules stubs.
Run: STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_clear_in_flight_cas.py -q
"""

import re
import sys
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import _conflict_fixture as CF  # noqa: E402  (shared conflict seam, )
import _team_state as ts  # noqa: E402
from _fileops import locked_modify_yaml  # noqa: E402

OURS = {"goal_id": "g-1-1", "title": "worker work", "phase": "4"}
THEIRS = {"goal_id": "g-9-9", "title": "reducer work", "phase": "4"}


def _write(path, row):
    path.write_text(yaml.safe_dump(row), encoding="utf-8")


def _read(path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ── the guard, driven through the real read-modify-write cycle ───────────────

def test_cas_declines_when_the_row_moved_under_the_lock(tmp_path):
    """The  interleaving, at the store.

    `locked_modify_yaml` reads INSIDE the lock that guards the write, so what
    the modifier sees is the live row, not the caller's snapshot. A reducer that
    claimed after our ownership read is therefore visible here — and must
    survive.
    """
    p = tmp_path / "alpha.yaml"
    _write(p, {"in_flight": dict(THEIRS), "last_active": "2026-01-01T00:00:00"})

    status = {}
    locked_modify_yaml(
        p, ts.make_clear_in_flight_modifier("tester", if_goal="g-1-1",
                                            status=status))

    assert _read(p)["in_flight"] == THEIRS, "a live foreign row was destroyed"
    assert status["cleared"] is False
    assert status["skipped_goal_id"] == "g-9-9"


def test_cas_clears_when_the_row_is_still_ours(tmp_path):
    """Positive control: the guard must not refuse the ordinary close."""
    p = tmp_path / "alpha.yaml"
    _write(p, {"in_flight": dict(OURS), "last_active": "2026-01-01T00:00:00"})

    status = {}
    locked_modify_yaml(
        p, ts.make_clear_in_flight_modifier("tester", if_goal="g-1-1",
                                            status=status))

    after = _read(p)
    assert "in_flight" not in after
    assert status["cleared"] is True
    assert after["last_active"] != "2026-01-01T00:00:00", "last_active not bumped"


def test_omitting_if_goal_keeps_the_unconditional_clear(tmp_path):
    """Back-compat: recovery/retire/release callers pass no if_goal and must
    keep clearing whatever is present. If this ever starts declining, those
    paths silently stop cleaning up."""
    p = tmp_path / "alpha.yaml"
    _write(p, {"in_flight": dict(THEIRS)})

    status = {}
    locked_modify_yaml(p, ts.make_clear_in_flight_modifier("tester",
                                                           status=status))

    assert "in_flight" not in _read(p)
    assert status["cleared"] is True


def test_absent_row_is_a_noop_and_does_not_stamp(tmp_path):
    p = tmp_path / "alpha.yaml"
    _write(p, {"last_active": "2026-01-01T00:00:00"})

    status = {}
    locked_modify_yaml(
        p, ts.make_clear_in_flight_modifier("tester", if_goal="g-1-1",
                                            status=status))

    after = _read(p)
    assert status["cleared"] is False and status["skipped_goal_id"] is None
    assert after["last_active"] == "2026-01-01T00:00:00", (
        "a no-op clear moved the timestamps")


def test_a_malformed_in_flight_is_never_cas_cleared(tmp_path):
    """A non-dict in_flight (hand-edit, partial write, bare `in_flight:`)
    carries no goal_id to compare against, so a CAS caller must refuse it
    rather than guess — guessing is the failure if_goal exists to prevent.
    It must also not crash the close path."""
    for bad in ("g-1-1", None, ["g-1-1"]):
        p = tmp_path / f"alpha-{type(bad).__name__}.yaml"
        _write(p, {"in_flight": bad})

        status = {}
        locked_modify_yaml(
            p, ts.make_clear_in_flight_modifier("tester", if_goal="g-1-1",
                                                status=status))

        assert "in_flight" in _read(p), f"CAS cleared an unverifiable row: {bad!r}"
        assert status["cleared"] is False
        assert status["skipped_goal_id"] is None


def test_unconditional_clear_still_normalizes_a_malformed_row(tmp_path):
    """Back-compat, and the reason the unconditional branch tests KEY PRESENCE
    rather than well-formedness.

    recovery / retire / release pass no if_goal precisely to clean up whatever
    is there. Narrowing them to well-formed dicts would leave a malformed row
    permanently un-clearable by the exact callers whose job is clearing it —
    a silent regression, since every consumer reads `.get("in_flight") or {}`
    and would report the agent idle while the key sat there forever.
    """
    p = tmp_path / "alpha.yaml"
    _write(p, {"in_flight": None})

    status = {}
    locked_modify_yaml(p, ts.make_clear_in_flight_modifier("tester",
                                                           status=status))

    assert "in_flight" not in _read(p)
    assert status["cleared"] is True


# ── CLI/daemon parity: one implementation, not two (guard-2323) ─────────────

def test_both_twins_use_the_shared_modifier():
    """The daemon is the only live path, so a CLI-only fix is inert the moment
    it lands. Hand-mirrored copies are what drift; this asserts neither twin
    grew a local `_row_modifier` for the clear again.
    """
    cli = (SCRIPTS / "team-state.py").read_text(encoding="utf-8")
    daemon = (SCRIPTS.parent.parent / "mind_api" / "src" / "world"
              / "team_state_write.py").read_text(encoding="utf-8")

    for name, src in (("CLI", cli), ("daemon", daemon)):
        assert "make_clear_in_flight_modifier" in src, (
            f"{name} twin no longer calls the shared clear modifier")

    # The endpoint must also ACCEPT the parameter — a shared modifier the
    # daemon never passes if_goal to is a guard with no way to be invoked.
    assert 'ctx.query.get("if_goal")' in daemon, (
        "daemon endpoint stopped reading the if_goal query parameter")


# ── defect B: the hook invocation must not eat stderr ───────────────────────

def test_stop_hook_does_not_discard_the_helper_stderr():
    """`2>/dev/null` here hides everything upstream of the helper's own handler
    — bad $PY resolution, ImportError on _rt, a syntax error — leaving
    `result=` empty and a broken hook looking exactly like a clean run.
    """
    hook = (SCRIPTS / "stop-hook.sh").read_text(encoding="utf-8")
    calls = [ln for ln in hook.splitlines()
             if "worker_close_in_flight_clear.py" in ln and "$PY" in ln]
    assert calls, "the worker-close in_flight invocation vanished from stop-hook.sh"
    for ln in calls:
        assert "2>/dev/null" not in ln, (
            "stop-hook discards the helper's stderr again (g-306-137 defect B)")
        assert re.search(r'2>>\s*"\$LOG"', ln), (
            "helper stderr is no longer redirected to the hook log")


def test_stop_hook_passes_the_production_arg_shape():
    """guard-920: the helper must receive --agent AND --sid at the real call
    site. Without --sid it resolves the SID from the environment, and
    bash-agent-inject fails open on timeout — so a dropped flag degrades every
    close to `unknown-owner` silently, and no unit test over decide() sees it.
    """
    hook = (SCRIPTS / "stop-hook.sh").read_text(encoding="utf-8")
    call = [ln for ln in hook.splitlines()
            if "worker_close_in_flight_clear.py" in ln and "$PY" in ln]
    assert len(call) == 1, f"expected exactly one invocation, found {len(call)}"
    assert "--agent" in call[0] and "--sid" in call[0]


# ── the conflict-retry re-invocation contract () ────────────────────
#
# WHY THESE DRIVE THE REAL RETRY WRAPPER. The modifier is re-invoked on an
# own-cloud If-Match conflict, and a hand-written `mod(row_a); mod(row_b)` proves
# the fix under strictly EASIER conditions than production supplies — guard-1829:
# whatever sits between the two competing events is a serialization point, and a
# remedy demonstrated across one has not been demonstrated at all. So these tests
# call `_rmw_with_conflict_retry` itself and let IT decide when to re-enter.
#
# WHY THEY MUST INJECT THE CONFLICT. `LocalBackend.conflict_error` is the EMPTY
# TUPLE, so `except ()` catches nothing and the retry wrapper degenerates to a
# single transparent pass — the modifier is invoked exactly ONCE. guard-955
# mandates STORAGE_BACKEND=local for every test run on an own-cloud box, so
# WITHOUT this injection no test in this tree can reach the second invocation,
# and the whole re-invocation defect class is invisible to the suite. Patch the
# seam; never reach for own-cloud to make a test see a retry.

class _FakeConflict(Exception):
    """Stands in for owncloud_backend.ConflictError (a bare Exception subclass)."""


class _FakeBackend:
    conflict_error = _FakeConflict


def _drive_one_conflict(monkeypatch, factory, first_row, second_row):
    """Run the REAL retry wrapper: attempt 1 sees first_row then loses the CAS
    fence, attempt 2 sees second_row and commits. Returns the status dict."""
    import _fileops

    # Routed through the shared seam () so the patch lands on the
    # CURRENT _fileops object rather than a collection-time reference.
    CF.patch_conflict_backend(monkeypatch, _FakeBackend())

    status = {}
    modifier = factory(status)
    rows = [first_row, second_row]
    attempts = []

    def _cycle():
        row = rows[len(attempts)]
        attempts.append(row)
        modifier(row)
        if len(attempts) == 1:
            raise _FakeConflict("If-Match fence lost to a concurrent writer")
        return row

    _fileops._rmw_with_conflict_retry(Path("unused.yaml"), _cycle)
    CF.assert_reinvoked(attempts)
    return status


def _fixed(status):
    return ts.make_clear_in_flight_modifier("tester", if_goal="g-1-1",
                                            status=status)


def test_a_declined_attempt_does_not_leak_its_verdict_into_the_clear(monkeypatch):
    """Attempt 1 finds a foreign row and declines; attempt 2 finds ours and
    clears. The caller must be told `cleared`, with no residue of the decline.

    `worker_close_in_flight_clear.run()` reads `skipped_goal_id` FIRST, so a
    leaked id here makes it report `raced -- left alone` for a row it just
    cleared.
    """
    status = _drive_one_conflict(
        monkeypatch, _fixed,
        {"in_flight": dict(THEIRS)},
        {"in_flight": dict(OURS)},
    )
    assert status["cleared"] is True
    assert status["skipped_goal_id"] is None, (
        "a losing attempt's skipped_goal_id survived into the winning attempt")


def test_a_cleared_attempt_does_not_leak_its_verdict_into_a_decline(monkeypatch):
    """The other direction: attempt 1 clears, attempt 2 re-reads a sibling claim
    and declines. The CLI twin reads `cleared` FIRST, so a leak here makes it
    print `in_flight cleared` for a row that survived.
    """
    status = _drive_one_conflict(
        monkeypatch, _fixed,
        {"in_flight": dict(OURS)},
        {"in_flight": dict(THEIRS)},
    )
    assert status["cleared"] is False, (
        "a losing attempt's cleared=True survived into the winning attempt")
    assert status["skipped_goal_id"] == "g-9-9"


def test_the_seed_once_variant_is_the_permanent_negative_control(monkeypatch):
    """guard-1829: ship the weaker variant AS the control, so the discrimination
    outlives this commit.

    Without a variant that FAILS, the two tests above assert something about
    their own shape rather than about the fix. This reproduces the pre-g-306-163
    modifier exactly — defaults seeded once at factory time, no per-invocation
    reset — and pins that it leaks in BOTH directions. If this ever passes, the
    tests above have stopped discriminating and the fix is no longer being
    tested.
    """
    def _seed_once(status):
        status.setdefault("cleared", False)
        status.setdefault("skipped_goal_id", None)

        def _row_modifier(row):
            current = row.get("in_flight")
            if not isinstance(current, dict) or current.get("goal_id") != "g-1-1":
                status["skipped_goal_id"] = (
                    current.get("goal_id") if isinstance(current, dict) else None)
                return row
            row.pop("in_flight")
            status["cleared"] = True
            return row
        return _row_modifier

    leaked = _drive_one_conflict(
        monkeypatch, _seed_once,
        {"in_flight": dict(THEIRS)},
        {"in_flight": dict(OURS)},
    )
    assert leaked["cleared"] is True and leaked["skipped_goal_id"] == "g-9-9", (
        "the seed-once control did NOT leak — the injection has stopped "
        "reaching the second invocation, so the tests above prove nothing")

    leaked_other = _drive_one_conflict(
        monkeypatch, _seed_once,
        {"in_flight": dict(OURS)},
        {"in_flight": dict(THEIRS)},
    )
    assert leaked_other["cleared"] is True, (
        "the seed-once control did NOT leak in the clear-then-decline direction")


# ── : ABSENT vs BLANK-BUT-SUPPLIED are different requests ───────────
#
# The normalization used to live ABOVE this shared boundary and was written
# twice, differently — the daemon did `(q or "").strip() or None` and the CLI did
# not strip at all. So `--if-goal '  '` PRESERVED a live row via the CLI while
# `?if_goal=%20%20` DESTROYED it via the daemon. `or None` is the whole defect:
# it collapses blank-but-supplied into absent, and absent means "clear
# unconditionally", so a caller asking for a GUARD silently got a WIPE that
# reported ok/cleared=True.

_SENTINEL = object()


def _drive(tmp_path, if_goal=_SENTINEL, row=None):
    """Run the real modifier over a real file. Returns (row_after, status)."""
    p = tmp_path / "alpha.yaml"
    _write(p, {"in_flight": dict(row if row is not None else THEIRS),
               "last_active": "2026-01-01T00:00:00"})
    status = {}
    kwargs = {} if if_goal is _SENTINEL else {"if_goal": if_goal}
    locked_modify_yaml(
        p, ts.make_clear_in_flight_modifier("tester", status=status, **kwargs))
    return _read(p), status


def test_blank_if_goal_raises_instead_of_wiping_a_live_row(tmp_path):
    """The matrix the fix exists for, driven through locked_modify_yaml.

    A blank-but-supplied value must RAISE. Silently treating it as absent is
    what destroyed a live row on the daemon path.
    """
    for blank in ("", "   ", "\t", "\n  "):
        try:
            ts.make_clear_in_flight_modifier("tester", if_goal=blank)
        except ValueError as e:
            assert "blank" in str(e).lower(), (
                f"raised, but the message does not name the cause: {e}")
        else:
            raise AssertionError(
                f"if_goal={blank!r} did NOT raise — a supplied-but-blank CAS "
                "request silently fell through to the unconditional clear, "
                "which is the g-306-170 defect")


def test_only_an_omitted_if_goal_clears_a_non_matching_row(tmp_path):
    """The pin the goal asks for: of the whole matrix, ONLY the absent forms
    clear a row that does not match.

    None and OMITTED are the same request by construction — None is the
    parameter default, and the daemon passes None when the query param is
    absent. Both keep the pre-g-306-137 unconditional behavior that the
    recovery / retire / release callers deliberately rely on.
    """
    # matching id -> clears (positive control: the guard is not just refusing)
    after, status = _drive(tmp_path, if_goal="g-9-9")
    assert "in_flight" not in after and status["cleared"] is True

    # non-matching id -> declines, row survives
    after, status = _drive(tmp_path, if_goal="g-1-1")
    assert after["in_flight"] == THEIRS and status["cleared"] is False
    assert status["skipped_goal_id"] == "g-9-9"

    # whitespace AROUND a valid id is normalized, not rejected — the value is
    # meaningful, only its padding is not.
    after, status = _drive(tmp_path, if_goal="  g-9-9  ")
    assert "in_flight" not in after and status["cleared"] is True, (
        "a padded but valid goal-id must normalize to a match, not decline")

    # absent (both spellings) -> unconditional clear, unchanged behavior
    for label, kwargs in (("explicit None", {"if_goal": None}),
                          ("omitted", {})):
        after, status = _drive(tmp_path, **kwargs)
        assert "in_flight" not in after and status["cleared"] is True, (
            f"{label} must still clear unconditionally — recovery/retire/"
            "release depend on it")


def test_neither_twin_normalizes_if_goal_itself(tmp_path):
    """The re-divergence pin.

    Normalization now happens once, inside the shared modifier. If either twin
    re-grows a local `or None` the two paths can disagree again about what a
    blank means — and that disagreement is invisible until it destroys a row.
    """
    cli = (SCRIPTS / "team-state.py").read_text(encoding="utf-8")
    daemon = (SCRIPTS.parent.parent / "mind_api" / "src" / "world"
              / "team_state_write.py").read_text(encoding="utf-8")

    # The exact collapsing idiom, in either twin, on the if_goal value.
    for name, src in (("CLI", cli), ("daemon", daemon)):
        for line in src.splitlines():
            if "if_goal" not in line or line.lstrip().startswith("#"):
                continue
            assert not re.search(r'if_goal.*\bor\s+None\b', line), (
                f"{name} twin re-grew caller-side if_goal normalization "
                f"({line.strip()!r}) — that is the g-306-170 drift returning")

    # The daemon must forward the RAW value so the modifier can judge it.
    assert 'if_goal = ctx.query.get("if_goal")' in daemon, (
        "daemon no longer forwards the raw if_goal; a caller-side transform "
        "puts normalization back above the shared boundary")


def test_wrapper_gates_on_supplied_not_on_emptiness():
    """`-n "$IF_GOAL"` cannot tell `--if-goal ''` from no flag at all.

    Dropping the query param on a blank makes the daemon read ABSENT, which
    means clear-unconditionally — so the wrapper would silently convert a guard
    request into a wipe before the daemon ever got the chance to refuse it.
    """
    wrapper = (SCRIPTS / "team-state-clear-in-flight.sh").read_text(
        encoding="utf-8")
    assert 'if [ "$IF_GOAL_SET" -eq 1 ]' in wrapper, (
        "wrapper stopped gating the if_goal query param on SUPPLIED-ness")
    assert "IF_GOAL_SET=1" in wrapper, (
        "wrapper no longer records that --if-goal was supplied")
    assert not re.search(r'if \[ -n "\$IF_GOAL" \]', wrapper), (
        "wrapper re-grew the emptiness gate, which collapses "
        "supplied-but-blank into absent (g-306-170)")


def test_blank_query_param_survives_transport_to_the_endpoint():
    """The MISSING LINK in the  chain, found by fresh-eyes on the fix.

    Three links were pinned: the wrapper EMITS `if_goal=`, the daemon FORWARDS
    the raw value, the modifier RAISES on blank. The link between the first two
    was not: HTTP has to deliver `if_goal=` to the endpoint AS AN EMPTY STRING.

    That depends entirely on `keep_blank_values=True` in server.py `_serve`,
    which lives outside this feature and which nothing pinned. Measured both
    branches (2026-08-05, alpha, cc-04, Linux 6.8.0-136-generic):

        keep_blank_values=True   -> {"if_goal": [""]}  -> ""   -> 400 refusal
        keep_blank_values=False  -> key ABSENT         -> None -> UNCONDITIONAL CLEAR

    So deleting one keyword argument in an unrelated file silently restores the
    exact destructive behavior g-306-170 removed — while every other test in
    this file stays green, because they all drive the modifier or the source
    text directly and never cross the transport.
    """
    from urllib.parse import parse_qs

    server_src = (SCRIPTS.parent.parent / "mind_api" / "src"
                  / "server.py").read_text(encoding="utf-8")

    # Source pin: the flag must remain on the _serve query parse. This is the
    # half that goes red on the mutation; the behavioral half below only shows
    # why it matters (it hardcodes the flag, so it cannot catch its removal).
    assert re.search(
        r'parse_qs\(\s*parts\.query\s*,\s*keep_blank_values\s*=\s*True\s*\)',
        server_src), (
        "server.py no longer parses the query string with "
        "keep_blank_values=True — a blank if_goal now VANISHES before the "
        "endpoint sees it, and absent means clear-unconditionally, which is "
        "the g-306-170 defect restored end-to-end (g-306-170 fresh-eyes)")

    # Behavioral: the two branches genuinely differ, and only one is safe.
    kept = parse_qs("agent=alpha&if_goal=", keep_blank_values=True)
    dropped = parse_qs("agent=alpha&if_goal=", keep_blank_values=False)
    assert kept.get("if_goal") == [""], (
        "a blank if_goal must reach the endpoint as an empty string so the "
        "modifier can refuse it")
    assert "if_goal" not in dropped, (
        "control: without the flag the key vanishes entirely — this is the "
        "state the source pin above exists to prevent")

    # And the flatten step must not re-drop it. `if v` filters empty LISTS,
    # not empty strings; [""] is truthy, so the value survives as "".
    assert {k: v[-1] for k, v in kept.items() if v}.get("if_goal") == "", (
        "the _flatten_qs shape no longer preserves a blank value")
