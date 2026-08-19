"""Regression test for  — the stale-skip path must SAY what it discards.

WHAT WAS BROKEN. `precompact-checkpoint.py` counts what it saves (it logs
`{eq_count} encoding, {non_null}/{total} slots`). The discard side counted
nothing: `main()`'s freshness gate printed only the mtime delta and then
unlinked the file. So a real `encoding_queue` loss on that path was
unmeasurable AFTER THE FACT BY CONSTRUCTION — the checkpoint is gone, and
precompact's count only ever went to stderr. "No loss reported" read
identically to "loss never looked for" (guard-1760, rb-245 family).

WHAT THIS PINS, and what it deliberately does not. Only the OBSERVABILITY half.
Whether the stale-skip should restore `encoding_queue` at all is a
merge-semantics question the `SKIP_SLOTS` comment already names as open; this
suite asserts the report exists and is honest, and asserts the stale-skip STILL
skips and STILL deletes. If a future change makes the gate restore something,
these tests should fail — the behavior-unchanged case below exists for that.

WHY MOST CASES ARE PURE. `_describe_discarded_checkpoint` takes the two
already-loaded documents and touches no filesystem and no module globals, which
is the same testability contract `_is_checkpoint_stale` and
`_delete_checkpoint_safely` state for themselves. That lets the exec-slice
idiom (below) reach it with no agent dir, no module import, and no `sys.modules`
pollution — the last of which matters because two of this file's three siblings
mutate global module state and are, separately, invisible to pytest (0 collected
tests each). Every case here is `def test_`, so `pytest -k compact_restore`
actually runs it.

THE E2E CASE IS THE ONE THAT COULD HAVE GONE WRONG. It runs in a SUBPROCESS,
with the working memory redirected through `BODY_WM_PATH` (a real, supported
seam read by `wm.py`) and the checkpoint redirected by assigning
`CHECKPOINT_PATH` (the guard-1415-sanctioned module redirect). Both are needed:
measured while writing this, `MIND_AGENT_DIR` is honored by `_paths.AGENT_DIR`
but NOT by `body_state_path()`, so an override alone resolves the checkpoint to
`agents/<name>/session/...` under the REAL repo — which for a made-up agent name
silently creates a stray agent dir, and for a real one would drive main() at a
live agent's checkpoint. That asymmetry is tracked separately as g-115-3592; it
is named here because it is the trap this file had to route around.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = PROJECT_ROOT / "core" / "scripts"
TARGET = SCRIPT_DIR / "compact-restore-slots.py"

# Text-slice + exec, matching test_compact_restore_freshness_gate.py. Anchored at
# `_is_checkpoint_stale` rather than at the helper itself so the slice also
# carries SKIP_SLOTS, which the function's default argument resolves against.
_source = TARGET.read_text(encoding="utf-8")
_start = _source.find("def _is_checkpoint_stale")
_end = _source.find("\ndef main(")
assert _start >= 0 and _end > _start, (
    "slice anchors not found in compact-restore-slots.py — the helpers were "
    "renamed or main() moved; every case below would be vacuous"
)
_ns: dict = {"sys": sys}
exec(_source[_start:_end], _ns)  # noqa: S102 — same idiom as the sibling suites
_describe = _ns["_describe_discarded_checkpoint"]
SKIP_SLOTS = _ns["SKIP_SLOTS"]


def _ck(encoding_queue=None, slots=None, *, omit_eq: bool = False) -> dict:
    doc: dict = {"all_slots": dict(slots or {}), "slot_meta": {}}
    if not omit_eq:
        doc["encoding_queue"] = list(encoding_queue or [])
    return doc


def _wm(slots=None) -> dict:
    return {"version": 1, "slots": dict(slots or {}), "slot_meta": {}}


# ── the count, and the ambiguity guard-1641 warns about ──────────────────────

def test_reports_the_discarded_encoding_queue_count() -> None:
    """The primary ask: the count that precompact logged and this side did not."""
    out = _describe(_ck(encoding_queue=[{"a": 1}, {"b": 2}, {"c": 3}]), _wm())
    assert "3 encoding_queue item(s)" in out


def test_an_empty_queue_is_reported_as_a_measured_zero() -> None:
    out = _describe(_ck(encoding_queue=[]), _wm())
    assert "0 encoding_queue item(s)" in out
    assert "ABSENT" not in out


def test_an_absent_key_is_distinguished_from_an_empty_queue() -> None:
    """guard-1641 — a count-like 0 conflates "counted zero" with "never produced".

    precompact writes `encoding_queue` unconditionally, so its ABSENCE means a
    format or version mismatch, not an empty queue. Collapsing the two would
    hide exactly the case worth seeing, and in the flattering direction.
    """
    out = _describe(_ck(omit_eq=True), _wm())
    assert "ABSENT" in out
    assert "NOT an empty queue" in out
    assert "0 encoding_queue item(s)" not in out


# ── the at-risk slot list ────────────────────────────────────────────────────

def test_names_a_slot_the_checkpoint_holds_and_live_wm_does_not() -> None:
    out = _describe(
        _ck(slots={"sensory_buffer": ["x"], "known_blockers": [{"id": "b1"}]}),
        _wm(slots={"sensory_buffer": ["x"]}),
    )
    assert "known_blockers" in out
    # Present in BOTH -> not at risk, so it must not be listed as such.
    assert "sensory_buffer" not in out.split("empty/null in live wm:")[1]


def test_a_slot_present_in_live_wm_is_not_reported_at_risk() -> None:
    out = _describe(
        _ck(slots={"known_blockers": [{"id": "b1"}]}),
        _wm(slots={"known_blockers": [{"id": "b1"}]}),
    )
    assert "none (every non-empty slot is also populated in live wm)" in out


def test_empty_checkpoint_slots_are_not_reported_as_loss() -> None:
    """A slot the checkpoint itself held empty cannot be lost by discarding it."""
    out = _describe(
        _ck(slots={"known_blockers": [], "sensory_buffer": None, "notes": ""}),
        _wm(),
    )
    assert "none (every non-empty slot is also populated in live wm)" in out


def test_a_checkpoint_with_no_slots_says_so_rather_than_all_clear() -> None:
    """Zero slots and all-slots-covered are different facts (guard-1641 again).

    "every non-empty slot is also populated in live wm" is vacuously true of an
    empty checkpoint and reads as a clean bill of health, which is the same
    conflation this function refuses for encoding_queue two branches earlier.
    """
    out = _describe(_ck(encoding_queue=[{"a": 1}], slots={}), _wm())
    assert "n/a — the checkpoint carried NO slots at all" in out
    assert "every non-empty slot is also populated" not in out
    assert "1 encoding_queue item(s)" in out


def test_skip_slots_are_excluded_and_the_exclusion_is_stated() -> None:
    """They are never restored by design, so naming them would invent a loss.

    Saying so is the other half: a report that silently drops a category
    overstates its own coverage, which is the failure this whole change is
    about (guard-1760).
    """
    victim = sorted(SKIP_SLOTS)[0]
    out = _describe(
        _ck(slots={victim: {"stamp": "2026-08-10T00:00:00"}, "known_blockers": [{"id": "b"}]}),
        _wm(),
    )
    assert victim not in out
    assert "1 SKIP_SLOTS slot(s) excluded" in out
    assert "known_blockers" in out


def test_skip_slots_argument_overrides_the_default() -> None:
    """The parameter exists so the helper is reachable from a slice that does not
    carry SKIP_SLOTS — without it the default would NameError there."""
    out = _describe(_ck(slots={"known_blockers": [{"id": "b"}]}), _wm(),
                    skip_slots={"known_blockers"})
    assert "0 SKIP_SLOTS" not in out
    assert "1 SKIP_SLOTS slot(s) excluded" in out


# ── unreadable inputs must say UNKNOWN, never a reassuring zero ──────────────

def test_a_non_mapping_checkpoint_reports_unknown_not_empty() -> None:
    for bad in (None, "", [], "just a string", 7):
        out = _describe(bad, _wm())
        assert "UNKNOWN" in out, f"{bad!r} produced: {out}"
        assert "not evidence it was empty" in out


def test_unreadable_live_wm_degrades_to_unclassified_not_safe() -> None:
    """The count still lands; the comparison honestly reports that it could not run.

    Reporting "no slots at risk" here would be a false all-clear built on a
    failed read — the rb-245 shape this change exists to remove.
    """
    out = _describe(_ck(encoding_queue=[{"a": 1}], slots={"known_blockers": [{"id": "b"}]}), None)
    assert "1 encoding_queue item(s)" in out
    assert "UNAVAILABLE" in out
    assert "NOT known-safe" in out


# ── the wiring: the report must actually be reached before the unlink ────────

def test_main_calls_the_reporter_before_deleting_on_the_stale_path() -> None:
    """A pure helper nothing calls is indistinguishable from one that works.

    Anchored on the EXECUTABLE lines and their ORDER, not on a bare mention
    anywhere in the file (guard-2368) — a docstring reference would otherwise
    satisfy a naive grep forever.
    """
    body = _source[_source.find("\ndef main("):]
    gate = body[body.find("if _wm_fresher_than_checkpoint:"):]
    gate = gate[: gate.find("\n    checkpoint = ")]
    call = gate.find("_describe_discarded_checkpoint(")
    unlink = gate.find('_delete_checkpoint_safely(CHECKPOINT_PATH, "stale-skip")')
    assert call > 0, "stale-skip path does not invoke _describe_discarded_checkpoint"
    assert unlink > 0, "stale-skip path no longer deletes the checkpoint"
    assert call < unlink, "the report must run BEFORE the only copy is unlinked"


# ── end to end, in a subprocess, against a real stale checkpoint ─────────────

_E2E_DRIVER = textwrap.dedent(
    """
    import importlib.util, sys, pathlib
    tmp = pathlib.Path(sys.argv[1])
    sys.path.insert(0, sys.argv[2])
    spec = importlib.util.spec_from_file_location("crs", sys.argv[3])
    crs = importlib.util.module_from_spec(spec)
    sys.modules["crs"] = crs
    spec.loader.exec_module(crs)
    # guard-1415 sanctioned redirect; BODY_WM_PATH (set by the parent) already
    # points wm.py at the temp working memory.
    crs.CHECKPOINT_PATH = tmp / "compact-checkpoint.yaml"
    crs.main()
    """
).strip()


def test_end_to_end_stale_skip_reports_then_still_skips_and_deletes() -> None:
    """The goal's own check: force a stale checkpoint, confirm the line names it.

    Runs OUT OF PROCESS with both paths redirected to a temp dir, so it cannot
    touch this box's live checkpoint or working memory.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        wm_file = tmp / "working-memory.yaml"
        ck_file = tmp / "compact-checkpoint.yaml"

        ck_file.write_text(
            yaml.safe_dump(
                {
                    "encoding_queue": [{"kind": "rb"}, {"kind": "guardrail"}],
                    "all_slots": {"known_blockers": [{"id": "b1"}], "loop_state": {"n": 1}},
                    "slot_meta": {},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        wm_file.write_text(
            yaml.safe_dump(_wm(slots={"known_blockers": None}), sort_keys=False),
            encoding="utf-8",
        )
        # wm.yaml strictly newer than the checkpoint == the stale-skip premise.
        ck_stat = ck_file.stat()
        os.utime(wm_file, (ck_stat.st_atime + 120, ck_stat.st_mtime + 120))

        env = dict(os.environ)
        env["BODY_WM_PATH"] = str(wm_file)
        env.setdefault("MIND_AGENT", "alpha")
        proc = subprocess.run(
            [sys.executable, "-c", _E2E_DRIVER, str(tmp), str(SCRIPT_DIR), str(TARGET)],
            capture_output=True, text=True, timeout=120, env=env,
        )
        out = proc.stdout + proc.stderr
        assert proc.returncode == 0, out
        assert "restore SKIPPED" in out, out
        assert "2 encoding_queue item(s)" in out, out
        assert "known_blockers" in out, out
        assert "loop_state" not in out.split("empty/null in live wm:")[1], out
        # Behavior otherwise unchanged: still skipped, still deleted.
        assert not ck_file.exists(), "stale checkpoint was not deleted"
        restored = yaml.safe_load(wm_file.read_text(encoding="utf-8")) or {}
        assert restored.get("slots", {}).get("known_blockers") is None, (
            "the stale-skip RESTORED a slot — this change was supposed to add "
            "reporting only"
        )


def test_end_to_end_unreadable_checkpoint_still_deletes_and_says_unknown() -> None:
    """A diagnostic must never break the operation it describes (guard-1562).

    Before this change the stale-skip path never parsed the checkpoint, so a
    malformed one was still deleted cleanly. That has to stay true.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        wm_file = tmp / "working-memory.yaml"
        ck_file = tmp / "compact-checkpoint.yaml"
        ck_file.write_text("{{ this is not: valid yaml ][\n", encoding="utf-8")
        wm_file.write_text(yaml.safe_dump(_wm(), sort_keys=False), encoding="utf-8")
        ck_stat = ck_file.stat()
        os.utime(wm_file, (ck_stat.st_atime + 120, ck_stat.st_mtime + 120))

        env = dict(os.environ)
        env["BODY_WM_PATH"] = str(wm_file)
        env.setdefault("MIND_AGENT", "alpha")
        proc = subprocess.run(
            [sys.executable, "-c", _E2E_DRIVER, str(tmp), str(SCRIPT_DIR), str(TARGET)],
            capture_output=True, text=True, timeout=120, env=env,
        )
        out = proc.stdout + proc.stderr
        assert proc.returncode == 0, out
        assert "restore SKIPPED" in out, out
        assert "UNKNOWN" in out, out
        assert not ck_file.exists(), "a malformed checkpoint was left behind"


if __name__ == "__main__":  # pragma: no cover — pytest is the primary runner
    import traceback

    failures = 0
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"  PASS  {_name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"  FAIL  {_name}")
                traceback.print_exc()
    print(f"g-115-4882 discard report: {failures} failure(s)")
    sys.exit(1 if failures else 0)
