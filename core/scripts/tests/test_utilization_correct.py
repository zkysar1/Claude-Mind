"""Tests for the utilization-correction path ().

The design decision this file pins: a correction is an INCREMENT of a monotone
`<counter>__corrected` sibling, never a bare negative delta. The proof that the
bare form fails lives beside its positive control in `test_utilization_store.py`
(`test_NEGATIVE_CONTROL_a_bare_decrement_does_not_survive_the_cross_box_merge`).
This file covers the wrapper, its ledger, and the two structural properties the
ledger depends on.

WHY THE STRUCTURAL PROPERTIES ARE TESTED AND NOT JUST ASSERTED IN A COMMENT.
Both were verified by hand when the store was added, and both are invisible when
they break: an unregistered store safe-freezes on the first both-diverged write
(rb-3150), and a machine-local classification keeps each box's corrections to
itself. Neither surfaces as an error, and the ledger keeps looking healthy on
whichever box you happen to read.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))

WRAPPER = PROJECT_ROOT / "core" / "scripts" / "utilization-correct.sh"


def _run(*args):
    """Resolve the shell through `_runtime_bash`, never a bare argv[0].

    On win32 CreateProcess searches System32 before PATH, so an unqualified
    shell name resolves to the WSL launcher and hangs forever on a dead
    LxssManager (guard-580). `shutil.which` searches PATH only and would appear
    to exonerate it; that disagreement is the trap.
    """
    from _runtime_bash import bash_cmd
    return subprocess.run(bash_cmd(str(WRAPPER), *args),
                          cwd=PROJECT_ROOT, capture_output=True, text=True)


# ── the ledger's two structural preconditions ─────────────────────────────

def test_ledger_is_registered_for_append_only_merge():
    """Unregistered, the backend safe-freezes this store on the first
    both-diverged write and corrections silently stop converging."""
    import coordination_merge as cm
    h = cm.merge_handler_for(Path("/x/world/utilization-corrections.jsonl"))
    assert h is cm.merge_append_only_jsonl
    # Control: the lookup is not simply handing a handler to every .jsonl.
    assert cm.merge_handler_for(Path("/x/world/no-such-store.jsonl")) is None


def test_ledger_basename_is_not_classified_machine_local():
    """`world/*-log.jsonl` classifies MACHINE-LOCAL in owncloud_sync, which
    would keep each box's corrections to itself and make the merge registration
    above moot. The name was chosen to avoid that; this pins the choice.

    Asserted against the classification SSOT
    (`owncloud_sync.refresh_would_clobber`) rather than against the name,
    because `_is_machine_local` alone is NOT that SSOT -- the two disagree, and
    a name-only assertion would keep passing while the store went local.
    """
    import owncloud_sync as osync

    w = Path("/x/world").resolve()

    class _FakeRemoteBackend:
        """Stand-in for a remote-backed backend. `_roots` is what the classifier
        actually reads -- a backend without it takes the LocalBackend early
        return, where EVERY path classifies synced. That is precisely why the
        positive control below is not optional: without it this test would pass
        against an inert classifier and say nothing at all."""
        _roots = [(str(w), "world")]

    be = _FakeRemoteBackend()
    # POSITIVE CONTROL first: this classifier can return True here at all.
    assert osync.refresh_would_clobber(be, w / "something-log.jsonl") is True
    assert osync.refresh_would_clobber(be, w / "utilization-corrections.jsonl") is False


# ── the wrapper refuses loudly (the goal's check 2) ───────────────────────

BAD_INVOCATIONS = [
    ([], "the following arguments are required"),
    (["--store", "guardrails", "--id", "guard-1", "--counter", "times_helpful"],
     "the following arguments are required: --reason"),
    (["--store", "guardrails", "--id", "guard-1", "--counter", "times_helpful",
      "--reason", "x", "--resaon", "typo"], "unrecognized arguments: --resaon"),
    (["--store", "gaurdrails", "--id", "guard-1", "--counter", "times_helpful",
      "--reason", "x"], "invalid choice: 'gaurdrails'"),
    (["--store", "guardrails", "--id", "guard-1", "--counter", "times_helpful",
      "--reason", "x", "--by", "-1"], "must be a POSITIVE integer"),
    (["--store", "guardrails", "--id", "guard-1", "--counter", "times_helpful",
      "--reason", "x", "--by", "0"], "must be a POSITIVE integer"),
    (["--store", "guardrails", "--id", "guard-1", "--counter", "times_helpfull",
      "--reason", "x"], "unknown counter: times_helpfull"),
]


@pytest.mark.parametrize("args,needle", BAD_INVOCATIONS)
def test_wrapper_refuses_loudly_and_says_why(args, needle):
    """Needles are the MEASURED strings from the real invocation, not the ones
    the author expected -- argparse says "are required" and "unrecognized", and
    a test written against the imagined wording passes vacuously on nothing."""
    r = _run(*args)
    assert r.returncode == 2, r.stderr
    assert needle in r.stderr, r.stderr


def test_unknown_flag_is_refused_rather_than_silently_discarded():
    """The sibling `guardrails-increment.sh` parses flags with a catch-all that
    DISCARDS them, so a typo'd `--resaon` there would drop the audit reason and
    still exit 0. Unacceptable for a correction, hence the divergence -- and the
    sibling half is asserted too, so if it is ever fixed this test fails and the
    claim gets re-checked instead of quietly going stale."""
    sibling_code = "\n".join(
        ln for ln in (PROJECT_ROOT / "core" / "scripts" / "guardrails-increment.sh"
                      ).read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#"))
    assert "-*) shift" + ";;" in sibling_code, "sibling changed -- re-check this"

    r = _run("--store", "guardrails", "--id", "guard-1", "--counter",
             "times_helpful", "--reason", "real reason", "--resaon", "typo")
    assert r.returncode == 2
    assert "unrecognized arguments" in r.stderr


def test_no_refusal_path_writes_a_ledger_row():
    """Every validation failure precedes the ledger write, so a rejected
    invocation leaves no row claiming a correction was attempted.

    Asserted against the REAL ledger the shim would write to, rather than by
    reading the source for ordering: the ordering claim is what needs proving,
    and a source-order check is one refactor away from proving nothing.
    """
    import _utilization_correct as uc
    ledger = uc.ledger_path()

    def _lines():
        try:
            return len([x for x in ledger.read_text(encoding="utf-8").splitlines()
                        if x.strip()])
        except FileNotFoundError:
            return 0

    before = _lines()
    for args, _needle in BAD_INVOCATIONS:
        assert _run(*args).returncode == 2
    assert _lines() == before


# ── the ledger row itself ─────────────────────────────────────────────────

def test_ledger_row_carries_a_unique_correction_id(tmp_path, monkeypatch):
    """Two identical corrections must not be byte-identical: the append-only
    line-union merge would collapse them into one and under-count."""
    import _utilization_correct as uc
    monkeypatch.setenv("MIND_AGENT", "tester")
    rows = [uc.append_row("guardrails", "guard-1", "times_helpful", 1,
                          "same reason", "applied", 1, world_dir=tmp_path)
            for _ in range(2)]
    assert rows[0]["correction_id"] != rows[1]["correction_id"]
    written = [json.loads(line) for line in
               (tmp_path / uc.LEDGER_REL).read_text(encoding="utf-8").splitlines()
               if line.strip()]
    assert len(written) == 2
    assert {r["correction_id"] for r in written} == {r["correction_id"] for r in rows}


def test_ledger_row_records_who_why_and_what(tmp_path, monkeypatch):
    """The whole reason the audited form was chosen over a bare delta: a counter
    that can be freely decremented is a counter that can be quietly laundered."""
    import _utilization_correct as uc
    monkeypatch.setenv("MIND_AGENT", "tester")
    row = uc.append_row("reasoning-bank", "rb-9", "times_cited", 2,
                        "cited the wrong entry", "applied", 2, world_dir=tmp_path)
    for field in ("agent", "box", "ts", "store", "record_id", "counter",
                  "by", "applied", "outcome", "reason"):
        assert field in row, field
    assert row["agent"] == "tester"
    assert row["reason"] == "cited the wrong entry"
    assert row["by"] == 2 and row["applied"] == 2


def test_valid_counters_come_from_the_ssot_not_a_retyped_list():
    """A hand-copied list would drift from `UTILIZATION_COUNTERS` silently, and
    the wrapper would then refuse a legitimate counter or admit a dead one."""
    import importlib.util
    import _utilization_correct as uc

    spec = importlib.util.spec_from_file_location(
        "_rb_for_counters", PROJECT_ROOT / "core" / "scripts" / "reasoning-bank.py")
    rb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rb)
    assert set(uc.valid_counters()) == rb.UTILIZATION_COUNTERS
