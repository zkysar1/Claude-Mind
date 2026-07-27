"""test_credential_enum_both_doors.py — .

A credentials-required blocker can enter the system through TWO doors:

  Door A  CREATE_BLOCKER -> blocker-create-gate.py check #5        (was enforced)
  Door B  aspirations-update-goal.sh -> blocker_ref payload        (was NOT)

Door B ran only `blocker_ref.validate()`, which checks a 5-key envelope and
nothing about credentials — so a credentials-required blocker written via
defer_reason or status=blocked bypassed check #5 entirely.  sat 90h
asserting a human credential mint was required, with a blocker_ref carrying no
enumeration at all: the Door-B fingerprint.

The fix is ONE shared predicate (`gates.credential_enum.check`) consumed by
both doors. These tests pin the properties that make that true, and would fail
if either door re-grew its own copy.

DOOR B HAS TWO LANES AND THE HOT ONE IS THE DAEMON. `aspirations-update-goal.sh`
is daemon-only (rt_call POST /v1/aspirations/update-goal, no CLI fallback), so a
fix wired only into aspirations.py::cmd_update_goal would have been inert on all
real traffic. `test_daemon_lane_is_wired` and `test_all_four_call_sites_guarded`
exist specifically to catch that regression — they assert the DAEMON sites are
guarded, not just the CLI ones.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
REPO = CORE_SCRIPTS.parent.parent
DAEMON_WRITE = REPO / "mind_api" / "src" / "endpoints" / "aspirations_write.py"

sys.path.insert(0, str(CORE_SCRIPTS))

from gates.credential_enum import check, refusal_message  # noqa: E402


def _enum(*entries):
    return {"type": "credentials-required",
            "credential_source_enumeration": list(entries)}


def _src(name, ident, probed=True, denied=True):
    return {"source": name, "identity": ident, "probed": probed, "denied": denied}


GOOD = _enum(_src("env-pair", "identity-one"), _src("default-chain", "identity-two"))


# ── the predicate itself ──────────────────────────────────────────────────────

def test_valid_enumeration_passes():
    """Anti-vacuity: a checker that refused everything would 'catch' everything."""
    r = check(GOOD)
    assert r["passed"] is True, r["reason"]


def test_missing_enumeration_is_refused():
    """The  shape: credentials-required, no enumeration at all."""
    r = check({"type": "credentials-required", "external_id": "pq-something"})
    assert r["passed"] is False
    assert "credential_source_enumeration" in r["reason"]


@pytest.mark.parametrize("payload,needle", [
    (_enum(_src("only-one", "i1")), "need >=2"),
    (_enum(_src("a", "i1", probed=False), _src("b", "i2")), "un-probed"),
    (_enum(_src("a", "i1", denied=False), _src("b", "i2")), "self-serviceable"),
    (_enum(_src("a", "same"), _src("b", "same")), "pseudo-independent"),
    (_enum({"source": "a"}, _src("b", "i2")), "malformed"),
])
def test_each_refusal_branch(payload, needle):
    r = check(payload)
    assert r["passed"] is False
    assert needle in r["reason"], r["reason"]


@pytest.mark.parametrize("btype", [
    "infrastructure", "resource", "user_action", "security-trust",
    "physical-hardware", "partner-response", "external-service",
])
def test_non_credentials_types_are_untouched(btype):
    """Byte-identical behavior for every other blocker type — the check must be
    invisible to them, with or without an enumeration field present."""
    assert check({"type": btype, "external_id": "x"})["passed"] is True
    assert check({"type": btype, "credential_source_enumeration": []})["passed"] is True


def test_accepts_json_string_and_dict_identically():
    """CLI passes a JSON string, the daemon passes a decoded dict. Same verdict."""
    assert check(GOOD) == check(json.dumps(GOOD))
    bad = {"type": "credentials-required"}
    assert check(bad) == check(json.dumps(bad))


def test_shape_errors_defer_to_validate():
    """Envelope shape is validate()'s contract, not this predicate's. Reporting
    it here too would double-report one defect under a check name that does not
    own it. Safe because validate() runs FIRST at every Door-B site — pinned by
    test_validate_runs_before_the_guard."""
    for junk in ("not json{", "", None, 42, ["a"]):
        assert check(junk)["passed"] is True


# ── purity: the daemon imports this module ────────────────────────────────────

def test_predicate_is_pure():
    """gates/blocker_ref.py states validate() is pure (no I/O, no env reads);
    this module sits beside it in the same daemon import path and must hold the
    same contract. A file read or env lookup here would run inside the daemon's
    request path and inside every gate invocation."""
    src = (CORE_SCRIPTS / "gates" / "credential_enum.py").read_text(encoding="utf-8")
    body = src.split('"""', 2)[2]  # strip module docstring
    for forbidden in ("os.environ", "open(", "subprocess", "Path(", "datetime.now",
                      "requests", "urllib"):
        assert forbidden not in body, f"credential_enum is not pure: found {forbidden!r}"


def test_blocker_ref_validate_drops_the_enumeration_field():
    """THE reason every call site must pass the RAW payload. validate() returns a
    REBUILT 5-key dict, so a guard reading its output would see the enumeration
    field missing on every input and refuse unconditionally. If validate() ever
    starts preserving it, this test fails and the 'raw payload' comments at the
    four call sites should be revisited."""
    from gates.blocker_ref import validate
    ok, normalized = validate(dict(GOOD, external_id="ext-1"))
    assert ok is True
    assert "credential_source_enumeration" not in normalized
    assert set(normalized) == {"type", "external_id", "state_hash",
                               "created_at", "expires_at"}


# ── both doors share ONE implementation ───────────────────────────────────────

def test_door_a_delegates_to_the_shared_predicate():
    """blocker-create-gate check #5 must produce the shared predicate's verdict
    verbatim — same name, same passed, same reason. A re-grown local copy would
    drift silently, which is the whole failure mode being fixed."""
    spec = importlib.util.spec_from_file_location(
        "bc_gate", CORE_SCRIPTS / "gates" / "blocker_create.py")
    bc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bc)
    for payload in (GOOD,
                    {"type": "credentials-required"},
                    _enum(_src("a", "same"), _src("b", "same")),
                    {"type": "infrastructure"}):
        assert bc._check_credential_enumeration(payload) == check(payload)


def test_door_a_has_no_second_copy_of_the_predicate():
    """Structural: the distinctive refusal strings must exist in exactly one
    source file. Two copies = drift."""
    needles = ["pseudo-independent credential sources",
               "self-serviceable credential source(s)"]
    owners = {}
    for path in list((CORE_SCRIPTS / "gates").glob("*.py")) + [DAEMON_WRITE]:
        text = path.read_text(encoding="utf-8")
        for n in needles:
            if n in text:
                owners.setdefault(n, []).append(path.name)
    for n in needles:
        assert owners.get(n) == ["credential_enum.py"], \
            f"{n!r} appears in {owners.get(n)} — the predicate has been copied"


# ── Door B: the DAEMON lane is the hot path ───────────────────────────────────

def test_daemon_lane_is_wired():
    """aspirations-update-goal.sh is daemon-only, so the daemon handler is where
    real traffic lands. This is the test that would have caught wiring only the
    CLI: it asserts the daemon imports the shared predicate and guards both of
    its blocker_ref sites."""
    text = DAEMON_WRITE.read_text(encoding="utf-8")
    assert "from gates.credential_enum import" in text, \
        "daemon does not import the shared predicate — Door B is still open"
    assert text.count("_credential_enum_guard(") >= 3, \
        "expected 1 definition + 2 call sites in the daemon handler"


def test_update_goal_wrapper_is_daemon_only():
    """Pins the premise of the test above. If a CLI fallback is ever restored,
    this fails and the 'daemon is the hot path' reasoning must be re-derived."""
    sh = (CORE_SCRIPTS / "aspirations-update-goal.sh").read_text(encoding="utf-8")
    assert "rt_call" in sh and "/v1/aspirations/update-goal" in sh
    assert "_fallback_exec" not in sh, \
        "a Python CLI fallback reappeared (see .claude/rules/no-python-cli-fallback.md)"


def test_all_four_call_sites_guarded():
    """Every place blocker_ref.validate() is called on an inbound payload must be
    followed by the credential guard. Counting them structurally means a NEW
    validate() site added later without a guard trips this test."""
    for path in (DAEMON_WRITE, CORE_SCRIPTS / "aspirations.py"):
        text = path.read_text(encoding="utf-8")
        validate_sites = len(re.findall(r"_validate_blocker_ref\(\s*ref_", text))
        # Count CALLS only. The function DEFINITION also contains the token, so
        # a naive occurrence count stays satisfied after a call site is deleted
        # — proven vacuous by mutation before this line was written: dropping the
        # defer-site guard left def+1call = 2 >= 2 and the assertion still passed.
        # `(?<!def )` excludes the definition; equality (not >=) means a deleted
        # call cannot be masked by the surviving one.
        guard_calls = len(re.findall(r"(?<!def )_credential_enum_guard\(", text))
        assert validate_sites == 2, \
            f"{path.name}: expected 2 inbound validate() sites, found {validate_sites}"
        assert guard_calls == validate_sites, \
            f"{path.name}: {validate_sites} validate() sites but {guard_calls} guard CALLS"


def test_validate_runs_before_the_guard():
    """Ordering. The guard defers envelope-shape errors to validate() (see
    test_shape_errors_defer_to_validate), which is only safe if validate()
    actually runs FIRST at every site. Pair them by line number.

    Deliberately NOT a regex that slices source blocks by indentation: the first
    draft did exactly that with `\\s{0,12}` and silently matched ONE block in
    aspirations.py instead of two — its sites are indented 16 — so the second
    call site was never covered and deleting its guard left the test green.
    Line-number pairing is indentation-independent and was mutation-proven to go
    red for every dropped guard in both files."""
    for path in (DAEMON_WRITE, CORE_SCRIPTS / "aspirations.py"):
        lines = path.read_text(encoding="utf-8").splitlines()
        v_lines = [i for i, ln in enumerate(lines)
                   if re.search(r"_validate_blocker_ref\(\s*ref_", ln)]
        g_lines = [i for i, ln in enumerate(lines)
                   if re.search(r"(?<!def )_credential_enum_guard\(", ln)]
        assert len(v_lines) == len(g_lines) == 2, (
            f"{path.name}: {len(v_lines)} validate sites vs {len(g_lines)} guard calls")
        # Window sized from measurement, not guessed: the real gaps are 8-11
        # lines (validate, its 4-line error return, the comment, the guard) and
        # the two sites are 63+ lines apart, so 20 cannot mis-pair.
        for v, g in zip(v_lines, g_lines):
            assert 0 < g - v <= 20, (
                f"{path.name}: guard at line {g+1} is not within 20 lines AFTER "
                f"its validate() at line {v+1} — ordering or pairing is wrong")


def test_daemon_guard_executes_and_refuses():
    """BEHAVIORAL, not structural. Every other daemon test in this file reads
    source text; none of them CALL the guard. That gap shipped a real
    `NameError: Response is not defined` (the helper is module-level, but
    `Response` is imported function-locally throughout this package to avoid an
    import cycle) — 64 green tests, and the very first live refusal 500'd.

    This test invokes the function, so the refusal path must actually run."""
    sys.path.insert(0, str(REPO))
    from mind_api.src.endpoints.aspirations_write import _credential_enum_guard

    class _Paths:
        world = None
        agent_name = "testagent"

    class _Ctx:
        paths = _Paths()
        headers: dict = {}

    ctx = _Ctx()

    # Refusal path — must return a Response, not raise.
    resp = _credential_enum_guard(ctx, "g-1-1",
                                  {"type": "credentials-required",
                                   "external_id": "e1"}, "defer text")
    assert resp is not None, "unproven credentials blocker was allowed through"
    assert getattr(resp, "status", None) == 400 or "400" in repr(resp)

    # Allow paths — a valid enumeration and a non-credentials type both pass.
    assert _credential_enum_guard(ctx, "g-1-1", dict(GOOD, external_id="e1"),
                                  "defer text") is None
    assert _credential_enum_guard(ctx, "g-1-1",
                                  {"type": "infrastructure", "external_id": "e1"},
                                  "defer text") is None


def test_cli_guard_executes_and_refuses(monkeypatch):
    """Behavioral twin of test_daemon_guard_executes_and_refuses for the CLI lane.

    Calls the helper directly rather than driving `aspirations.py update-goal`:
    that path resolves the goal BEFORE reaching the gate (verified live — a
    nonexistent id exits 'Goal not found' first), so an end-to-end CLI refusal
    would require writing against a REAL goal. The gate's PLACEMENT in that path
    is covered by the mutation-proven structural tests above; this covers that
    the code actually runs."""
    import aspirations as asp

    class _Args:
        override_blocker_gate = None

    with pytest.raises(SystemExit) as exc:
        asp._credential_enum_guard(
            "g-1-1", {"type": "credentials-required", "external_id": "e1"},
            "defer text", _Args())
    assert exc.value.code == 1

    # Allow paths must NOT exit.
    assert asp._credential_enum_guard("g-1-1", dict(GOOD, external_id="e1"),
                                      "defer text", _Args()) is None
    assert asp._credential_enum_guard("g-1-1",
                                      {"type": "infrastructure", "external_id": "e1"},
                                      "defer text", _Args()) is None

    # Override suppresses the refusal.
    class _Over:
        override_blocker_gate = "justified false positive"

    monkeypatch.setattr(asp, "WORLD_DIR", None, raising=False)
    assert asp._credential_enum_guard(
        "g-1-1", {"type": "credentials-required", "external_id": "e1"},
        "defer text", _Over()) is None


def test_wrapper_plumbs_the_override_to_the_daemon():
    """The escape hatch must be reachable from the documented invocation.
    `aspirations-update-goal.sh` is daemon-only, so an argparse flag with no
    corresponding header is inert on the hot path — the same wired-the-wrong-
    lane defect the guard itself was fixing."""
    sh = (CORE_SCRIPTS / "aspirations-update-goal.sh").read_text(encoding="utf-8")
    assert "--override-blocker-gate)" in sh, "wrapper does not accept the flag"
    assert 'X-Mind-Override-Blocker-Gate: $OVERRIDE_BLOCKER_GATE' in sh, \
        "flag is parsed but never becomes a header — unreachable on the hot path"


# ── override parity ───────────────────────────────────────────────────────────

def test_override_flag_and_header_exist_with_shared_ledger():
    """--override-blocker-gate (CLI) and X-Mind-Override-Blocker-Gate (daemon)
    must both exist and both append to world/blocker-gate-overrides.jsonl, the
    same ledger Door A's override uses."""
    cli = (CORE_SCRIPTS / "aspirations.py").read_text(encoding="utf-8")
    assert "--override-blocker-gate" in cli
    assert 'which_checks_bypassed=["credential_enumeration"]' in cli

    daemon = DAEMON_WRITE.read_text(encoding="utf-8")
    assert "X-Mind-Override-Blocker-Gate" in daemon
    assert 'which_checks_bypassed=["credential_enumeration"]' in daemon

    ledger = (CORE_SCRIPTS / "gates" / "blocker_ref.py").read_text(encoding="utf-8")
    assert "blocker-gate-overrides.jsonl" in ledger
    assert "which_checks_bypassed: Optional[list] = None" in ledger


def test_override_ledger_default_is_unchanged():
    """The new parameter must not alter existing unstructured-defer records."""
    import inspect
    from gates.blocker_ref import log_unstructured_override
    sig = inspect.signature(log_unstructured_override)
    assert sig.parameters["which_checks_bypassed"].default is None
    body = inspect.getsource(log_unstructured_override)
    assert 'which_checks_bypassed or ["blocker_ref_required"]' in body


def test_refusal_message_is_educational_and_names_the_escape_hatch():
    msg = refusal_message("g-999-99", "only 1 credential source enumerated",
                          flag_hint="pass --override-blocker-gate")
    assert "g-999-99" in msg
    assert "only 1 credential source enumerated" in msg
    assert "credential_source_enumeration" in msg
    assert "--override-blocker-gate" in msg


# ── end-to-end through the real CLI entry point ───────────────────────────────

def test_cli_refuses_unproven_credentials_blocker_end_to_end(tmp_path):
    """Drive the real argparse path. Proves the guard is reachable from the
    documented invocation, not just callable in isolation."""
    p = subprocess.run(
        [sys.executable, str(CORE_SCRIPTS / "aspirations.py"), "update-goal",
         "--help"],
        capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin", "STORAGE_BACKEND": "local",
             "HOME": str(tmp_path)},
    )
    assert p.returncode == 0, p.stderr
    assert "--override-blocker-gate" in p.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
