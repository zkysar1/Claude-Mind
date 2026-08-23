"""test_field_shrink_guard.py — .

A goal's long-lived prose can be destroyed through TWO doors, both of which
reported success on 2026-08-21 while a 36,904-char description was replaced
with 3,467 chars belonging to an UNRELATED goal:

  Door A  daemon  mind_api/src/endpoints/aspirations_write.py::update_goal
  Door B  CLI     core/scripts/aspirations.py::cmd_update_goal

The fix is ONE shared predicate (`gates.field_shrink.evaluate`) consumed at
both `goal[field] = value` sites. These tests pin the properties that make
that true and would fail if either door re-grew its own copy or drifted.

THE PLACEMENT IS THE WHOLE GATE. The predicate needs the PRE-mutation value,
so it must run before `goal[field] = value`. A guard that ran after the
assignment would compare the new value against itself, compute ratio 1.0, and
pass unconditionally — green tests, zero protection. `test_guard_precedes_the_
assignment_in_both_doors` is the test that catches that, and it is
mutation-proven below rather than assumed.

ANTI-VACUITY IS LOAD-BEARING HERE. A predicate that refused everything would
"catch" the incident perfectly while making the loop unwritable, so the pass
branches are tested as hard as the block branch — including the closest
LEGITIMATE shrink ever measured in the live corpus (g-115-2571, ratio 0.55),
which must still pass.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
REPO = CORE_SCRIPTS.parent.parent
DAEMON_WRITE = REPO / "mind_api" / "src" / "endpoints" / "aspirations_write.py"
CLI_WRITE = CORE_SCRIPTS / "aspirations.py"
WRAPPER = CORE_SCRIPTS / "aspirations-update-goal.sh"

sys.path.insert(0, str(CORE_SCRIPTS))

from gates.field_shrink import (  # noqa: E402
    evaluate, GUARDED_FIELDS, MIN_OLD_CHARS, MAX_SHRINK_RATIO,
)


def _s(n: int) -> str:
    return "x" * n


# ── the predicate: every branch reachable, each with its own decision_path ────

def test_the_incident_is_refused():
    """The literal  numbers: 36,904 chars -> 3,467 (ratio 0.09)."""
    r = evaluate("description", _s(36904), _s(3467))
    assert r["blocked"] is True
    assert r["decision_path"] == "shrink-refused"
    assert r["old_len"] == 36904 and r["new_len"] == 3467
    assert r["ratio"] == pytest.approx(3467 / 36904, abs=1e-9)


def test_growth_passes():
    """Anti-vacuity. Every description update in the measured corpus except the
    incident GREW; refusing growth would refuse essentially all real traffic."""
    r = evaluate("description", _s(5000), _s(9000))
    assert r["blocked"] is False
    assert r["decision_path"] == "ratio-within-tolerance"


def test_closest_legitimate_shrink_still_passes():
    """ outcome_note, 7784 -> 4250 (ratio 0.55) — the smallest
    legitimate shrink in the live corpus. If a future threshold change makes
    this red, the threshold has crossed into refusing real work."""
    r = evaluate("outcome_note", _s(7784), _s(4250))
    assert r["blocked"] is False, "threshold now refuses a known-legitimate shrink"


@pytest.mark.parametrize("field", ["title", "defer_reason", "status",
                                   "outcome_class", "participants"])
def test_unguarded_fields_are_untouched(field):
    """Byte-identical behavior for every field that is MEANT to be replaced
    wholesale. A title rewrite is normal work, not data loss."""
    r = evaluate(field, _s(50000), _s(3))
    assert r["blocked"] is False
    assert r["decision_path"] == "field-not-guarded"


@pytest.mark.parametrize("old,new", [
    (None, _s(100)),          # field absent / previously cleared
    (_s(50000), None),        # a deliberate clear (the literal `null` write)
    ({"a": 1}, _s(10)),       # structured value
    (_s(50000), 42),          # numeric
], ids=["absent", "clear", "structured", "numeric"])
def test_non_string_operands_are_out_of_scope(old, new):
    """This gate has an opinion about PROSE. A clear-to-null is a distinct,
    already-supported operation (guard-2080/4258) and must not be caught here."""
    r = evaluate("description", old, new)
    assert r["blocked"] is False
    assert r["decision_path"] == "non-string-operand"


def test_short_fields_are_out_of_scope():
    """Below the floor, wholesale rewrites are normal — early drafts, one-line
    notes, title-as-spec recurring goals."""
    r = evaluate("description", _s(MIN_OLD_CHARS - 1), _s(1))
    assert r["blocked"] is False
    assert r["decision_path"] == "old-below-floor"


def test_floor_boundary_is_inclusive():
    """Exactly at the floor the gate engages; one char below it does not."""
    assert evaluate("description", _s(MIN_OLD_CHARS), _s(1))["blocked"] is True
    assert evaluate("description", _s(MIN_OLD_CHARS - 1), _s(1))["blocked"] is False


def test_ratio_boundary_is_inclusive_on_the_pass_side():
    """ratio == MAX_SHRINK_RATIO passes; a hair under it blocks. Pins the
    comparison direction so a `>` / `>=` flip cannot slip through."""
    old = 10000
    at = int(old * MAX_SHRINK_RATIO)
    assert evaluate("description", _s(old), _s(at))["blocked"] is False
    assert evaluate("description", _s(old), _s(at - 1))["blocked"] is True


def test_empty_new_value_on_a_long_field_is_refused():
    """The worst case — total erasure — must not divide-by-zero or slip past."""
    r = evaluate("description", _s(30000), "")
    assert r["blocked"] is True
    assert r["ratio"] == 0.0


def test_every_decision_path_is_distinct():
    """guard-502: every branch needs a unique label, or the firing log cannot
    tell which branch produced a verdict."""
    paths = {
        evaluate("title", _s(9000), _s(1))["decision_path"],
        evaluate("description", None, None)["decision_path"],
        evaluate("description", _s(10), _s(1))["decision_path"],
        evaluate("description", _s(9000), _s(9000))["decision_path"],
        evaluate("description", _s(9000), _s(1))["decision_path"],
    }
    assert len(paths) == 5, f"decision_path collision: {paths}"


def test_refusal_message_is_educational_and_names_the_escape_hatch():
    msg = evaluate("description", _s(36904), _s(3467))["message"]
    assert "--override-shrink" in msg
    assert "36904" in msg and "3467" in msg
    assert "append" in msg          # names the mechanism that causes this
    assert msg is not None


def test_pass_branches_carry_no_message():
    """A message on a pass branch would be logged as a refusal reason that
    never fired."""
    for r in (evaluate("title", _s(9000), _s(1)),
              evaluate("description", _s(10), _s(1)),
              evaluate("description", _s(9000), _s(9000))):
        assert r["message"] is None


# ── purity: the daemon imports this module into its request path ─────────────

def test_predicate_is_pure():
    """Runs inside the daemon request path on EVERY update-goal call. A file
    read, env lookup or clock call here would be paid by every write."""
    src = (CORE_SCRIPTS / "gates" / "field_shrink.py").read_text(encoding="utf-8")
    body = src.split('"""', 2)[2]
    for forbidden in ("os.environ", "open(", "subprocess", "Path(",
                      "datetime", "requests", "urllib", "import os"):
        assert forbidden not in body, f"field_shrink is not pure: found {forbidden!r}"


def test_predicate_has_no_fail_open_handler():
    """guard-3803: a fail-open handler also covers refusal-message construction,
    so a compose-time bug silently becomes an approval. This predicate is pure
    arithmetic behind isinstance checks — it must have no except at all."""
    body = (CORE_SCRIPTS / "gates" / "field_shrink.py").read_text(
        encoding="utf-8").split('"""', 2)[2]
    assert "except" not in body, \
        "a fail-open handler here can convert a refusal into a silent pass"


# ── both doors share ONE implementation ──────────────────────────────────────

@pytest.mark.parametrize("path", [DAEMON_WRITE, CLI_WRITE])
def test_both_doors_import_the_shared_predicate(path):
    text = path.read_text(encoding="utf-8")
    assert "from gates.field_shrink import" in text, \
        f"{path.name} does not import the shared predicate — its door is open"
    assert "_field_shrink_eval(field," in text, \
        f"{path.name} imports the predicate but never calls it"


def test_thresholds_live_in_exactly_one_file():
    """A second copy of either constant is drift waiting to happen — the two
    writers would then disagree about what a legal write is."""
    owners = {}
    for path in list((CORE_SCRIPTS / "gates").glob("*.py")) + [DAEMON_WRITE, CLI_WRITE]:
        text = path.read_text(encoding="utf-8")
        for needle in ("MIN_OLD_CHARS", "MAX_SHRINK_RATIO"):
            if re.search(rf"^{needle}\s*=", text, re.M):
                owners.setdefault(needle, []).append(path.name)
    for needle in ("MIN_OLD_CHARS", "MAX_SHRINK_RATIO"):
        assert owners.get(needle) == ["field_shrink.py"], \
            f"{needle} defined in {owners.get(needle)} — the threshold was copied"


def test_guard_precedes_the_assignment_in_both_doors():
    """THE test. The predicate needs the PRE-mutation value, so the guard must
    run BEFORE `goal[field] = value`. Placed after, it would compare the new
    value against itself (ratio 1.0) and pass unconditionally — a gate that is
    present, imported, logged, and completely inert.

    Mutation-proven: moving either call below its assignment turns this red."""
    for path in (DAEMON_WRITE, CLI_WRITE):
        lines = path.read_text(encoding="utf-8").splitlines()
        guard = [i for i, ln in enumerate(lines) if "_field_shrink_eval(field," in ln]
        assign = [i for i, ln in enumerate(lines)
                  if re.match(r"\s*goal\[field\] = value\s*$", ln)]
        assert len(guard) == 1, f"{path.name}: expected 1 guard call, found {len(guard)}"
        assert len(assign) == 1, f"{path.name}: expected 1 assignment, found {len(assign)}"
        assert guard[0] < assign[0], (
            f"{path.name}: guard at line {guard[0]+1} runs AFTER the assignment at "
            f"line {assign[0]+1} — it would compare the new value against itself")


def test_neither_door_wraps_the_guard_in_a_bare_except():
    """A try/except around the call re-introduces exactly the fail-open surface
    the predicate was written to avoid (guard-3803)."""
    for path in (DAEMON_WRITE, CLI_WRITE):
        lines = path.read_text(encoding="utf-8").splitlines()
        idx = next(i for i, ln in enumerate(lines) if "_field_shrink_eval(field," in ln)
        window = "\n".join(lines[max(0, idx - 3):idx + 1])
        assert "try:" not in window, \
            f"{path.name}: the shrink guard sits inside a try block"


# ── the override must be reachable from the documented invocation ────────────

def test_wrapper_accepts_and_plumbs_the_override():
    """aspirations-update-goal.sh is daemon-only, so an argparse flag with no
    matching header is inert on the hot path. Worse, guard-2525: an
    UNRECOGNIZED flag is silently DROPPED and its value REPLACES the field — so
    an unregistered --override-shrink would itself cause the exact data loss
    this gate exists to prevent."""
    sh = WRAPPER.read_text(encoding="utf-8")
    assert "--override-shrink" in sh.split("_ACCEPTED_FLAGS=")[1].split("\n")[0], \
        "--override-shrink missing from _ACCEPTED_FLAGS — it would be DROPPED " \
        "and its value would overwrite the field (guard-2525)"
    assert "--override-shrink)" in sh, "wrapper has no parse branch for the flag"
    assert 'X-Mind-Override-Shrink: $OVERRIDE_SHRINK' in sh, \
        "flag is parsed but never becomes a header — unreachable on the daemon path"
    assert re.search(r"^OVERRIDE_SHRINK=\"\"", sh, re.M), \
        "OVERRIDE_SHRINK is unset; the wrapper runs under `set -u`"


def test_daemon_reads_the_override_header():
    text = DAEMON_WRITE.read_text(encoding="utf-8")
    assert '_header_override(\n                    ctx, "X-Mind-Override-Shrink")' in text \
        or 'X-Mind-Override-Shrink' in text


def test_cli_exposes_the_override_flag():
    p = subprocess.run(
        [sys.executable, str(CLI_WRITE), "update-goal", "--help"],
        capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin", "STORAGE_BACKEND": "local"},
    )
    assert p.returncode == 0, p.stderr
    assert "--override-shrink" in p.stdout


# ── telemetry registration ───────────────────────────────────────────────────

def test_gate_is_registered_in_the_registry():
    """_gate_log requires gate_id to match an `id` in gates.yaml or the
    retirement evaluator never sees this gate's firings."""
    import yaml
    reg = yaml.safe_load((REPO / "core" / "config" / "gates.yaml").read_text(
        encoding="utf-8"))
    entry = next((g for g in reg["gates"] if g["id"] == "field-shrink-guard"), None)
    assert entry is not None, "field-shrink-guard is not in the gate registry"
    assert entry["override_flag"] == "--override-shrink"
    assert entry["instrumented"] is True
    assert "RETIREMENT CRITERION" in entry["tuning_notes"], \
        "guard-769: a new defense records its retirement criterion at birth"


def test_both_doors_log_every_branch():
    """guard-502: block, override and noop must each emit a firing, or the
    retirement evaluator sees a partial distribution."""
    for path in (DAEMON_WRITE, CLI_WRITE):
        lines = path.read_text(encoding="utf-8").splitlines()
        idx = next(i for i, ln in enumerate(lines) if "_field_shrink_eval(field," in ln)
        block = "\n".join(lines[idx:idx + 55])
        assert '"field-shrink-guard"' in block
        assert '"override" if' in block, f"{path.name}: override branch not logged"
        assert '"noop"' in block, f"{path.name}: noop branch not logged"


# ── BEHAVIORAL: drive the real CLI against a tmp world ──────────────────────
# Structural tests read source text; none of them RUN the refusal. That exact
# gap shipped a live NameError once in this package (see
# test_credential_enum_both_doors.test_daemon_guard_executes_and_refuses), so
# the guard is also exercised end-to-end here.
#
# guard-1006: never probe a write-path gate with the PRODUCTION write command —
# if the gate does not block, the throwaway payload LANDS as live state. Hence
# a tmp world. guard-955: STORAGE_BACKEND=local is MANDATORY, not hygiene —
# under own-cloud the S3 key ignores the MIND_WORLD override and a fixture
# write collides on the PRODUCTION key (that truncated the real
# world/aspirations.jsonl on 2026-07-09).

import json  # noqa: E402
import os  # noqa: E402

# Sliced to EXACT lengths rather than trusting repeat-count arithmetic: the
# first draft used `"ORIGINAL " * 4613` believing that was 36,904 chars. It is
# 41,517 (the unit is 9 chars, not 8), and the size assertion below is what
# caught it. Keep both the slice and the assertion.
_LONG = ("ORIGINAL " * 4200)[:36904]     # the incident's exact old length
_SHORT = ("unrelated goal prose " * 200)[:3467]   # its exact new length, ratio 0.09


@pytest.fixture()
def shrink_world(tmp_path):
    wd = tmp_path / "world"
    wd.mkdir()
    asp = {
        "id": "asp-999", "title": "shrink fixtures", "status": "active",
        "goals": [
            {"id": "g-999-01", "title": "long prose", "status": "pending",
             "priority": "MEDIUM", "participants": ["agent"],
             "description": _LONG},
            {"id": "g-999-02", "title": "short prose", "status": "pending",
             "priority": "MEDIUM", "participants": ["agent"],
             "description": "tiny"},
        ],
    }
    (wd / "aspirations.jsonl").write_text(json.dumps(asp) + "\n", encoding="utf-8")
    return wd


def _update(world, goal_id, field, value, *, override=None):
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = "alpha"
    cmd = [sys.executable, str(CLI_WRITE), "update-goal", goal_id, field, value]
    if override:
        cmd += ["--override-shrink", override]
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          cwd=str(REPO), timeout=120)


def _desc(world, goal_id):
    for line in (world / "aspirations.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for g in json.loads(line).get("goals", []):
            if g["id"] == goal_id:
                return g.get("description")
    raise AssertionError(f"{goal_id} not found")


def test_end_to_end_refusal_leaves_the_bytes_on_disk_untouched(shrink_world):
    """The whole point of the gate, proven at the only layer that matters: after
    the refusal the original prose is still there, byte for byte."""
    before = _desc(shrink_world, "g-999-01")
    assert len(before) == 36904, "fixture drifted from the incident's size"

    res = _update(shrink_world, "g-999-01", "description", _SHORT)

    assert res.returncode != 0, "the catastrophic shrink was ALLOWED through"
    assert "field_shrink_blocked" in (res.stderr or ""), res.stderr
    assert _desc(shrink_world, "g-999-01") == before, \
        "refusal returned non-zero but the write landed anyway"


def test_end_to_end_override_lets_the_write_land(shrink_world):
    """The escape hatch must actually work from the documented invocation —
    a refusal with an unreachable override is a wedge, not a guard."""
    res = _update(shrink_world, "g-999-01", "description", _SHORT,
                  override="deliberate condense, verified against .history")
    assert res.returncode == 0, res.stderr
    assert _desc(shrink_world, "g-999-01") == _SHORT


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows CreateProcess caps the whole command line at 32767 chars; this "
        "case passes _LONG (36904 -- deliberately the incident's exact old length) "
        "as an argv element, so the spawn itself dies with WinError 206 before the "
        "SUT runs. Not a defect in the guard: the same code path is covered on "
        "Windows by the shorter-value cases in this file, and in full on Linux. "
        "Shortening _LONG would silently weaken the incident fidelity this case "
        "exists to preserve, so the case is skipped rather than trimmed."))
def test_end_to_end_ordinary_writes_are_unaffected(shrink_world):
    """Anti-vacuity at the integration layer. A short field replaced wholesale,
    and a long field GROWING, are both ordinary traffic and must pass."""
    res = _update(shrink_world, "g-999-02", "description", "a fuller spec " * 40)
    assert res.returncode == 0, res.stderr

    res = _update(shrink_world, "g-999-01", "description", _LONG + " appended")
    assert res.returncode == 0, res.stderr
    assert _desc(shrink_world, "g-999-01").endswith(" appended")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
