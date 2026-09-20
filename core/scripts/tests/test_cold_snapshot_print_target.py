"""`--print-target` must resolve the destination WITHOUT hashing ().

THE DEFECT THIS PINS. Callers that only need to know WHERE the archive would go
— `owncloud-endpoint-flip.sh --status`, `cold-snapshot-target-provision.sh`'s
verification — used `--dry-run`, which prints the `[target]` line and THEN walks
and hashes the entire working set. `build_manifest`'s own comment budgets that
at 10-25s per box; foxtrot measured it at >630s and still running on
LAPTOP-3IOFCNEO, i.e. 25-60x over budget, and the cost scales with the working
set — so it degrades worst on the boxes holding the most data.

`grep -m1` does NOT bound it, which is the part that looks safe and is not:
command substitution waits for the whole pipeline, and a producer that spends
ten minutes hashing never writes again, so it never takes the SIGPIPE that would
end it. The result was that `--status` — advertised as the SAFE READ-ONLY query,
and prescribed by g-372-20 as the way to verify a flip — did not return at all,
leaving an operator who had just repointed the box's live store with no working
readback. Measured after the fix: 56ms for the probe, 86ms for `--status`.

WHY THE LOAD-BEARING TEST ASSERTS A NEGATIVE (a call that must NOT happen).
Asserting only that `--print-target` prints a `[target]` line would pass just as
happily against the OLD code, which also printed it — before hashing for ten
minutes. The property that actually matters is the one the caller pays for, so
the pin is that the expensive walk is never REACHED. A negative assertion is
vacuous on its own (a typo'd flag name would also never reach it), hence the
positive control in the same test: the identical run shape WITHOUT the flag must
still reach it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cold_snapshot as cs  # noqa: E402


def _target(mode="pinned"):
    """A resolved, non-refused target — the shape target_line() formats."""
    return {
        "mode": mode,
        "endpoint": "aws-regional",
        "bucket": "test-cold-bucket",
        "live_endpoint": "http://127.0.0.1:9000",
        "live_bucket": "test-live-bucket",
        "backend": "own-cloud",
    }


def _run(monkeypatch, argv, target=None):
    """Run main() with the destination stubbed; report whether it hashed.

    Returns (rc, calls) where `calls` names every expensive walk that ran.
    """
    calls = []
    monkeypatch.setattr(cs, "resolve_cold_target", lambda env: target or _target())
    monkeypatch.setattr(
        cs, "build_manifest",
        lambda *a, **k: (calls.append("build_manifest"), ([], 0))[1])
    monkeypatch.setattr(
        cs, "build_snapshot",
        lambda *a, **k: (calls.append("build_snapshot"), ([], 0, b""))[1])
    monkeypatch.setattr(sys, "argv", ["cold_snapshot.py", *argv])
    return cs.main(), calls


def test_print_target_never_reaches_the_working_set_hash(monkeypatch, capsys):
    rc, calls = _run(monkeypatch, ["--print-target"])

    assert rc == 0
    assert calls == [], (
        "--print-target reached the expensive walk "
        f"({calls}) -- this is the >630s hang of g-372-35, reintroduced")

    # The line the callers actually grep for must still be on stderr.
    err = capsys.readouterr().err
    assert "[target] mode=pinned" in err, (
        "--print-target skipped the hash but printed no [target] line -- every "
        f"caller greps for it and would read empty. stderr was: {err!r}")

    # POSITIVE CONTROL: the same run shape WITHOUT the flag DOES hash. Without
    # this, the assertion above passes against a main() that never hashes at
    # all, or against a flag name that silently matches nothing.
    rc2, calls2 = _run(monkeypatch, ["--dry-run"])
    assert calls2 == ["build_manifest"], (
        "--dry-run did NOT reach build_manifest, so the negative assertion "
        "above proves nothing about --print-target")
    assert rc2 == 0


def test_print_target_does_not_smuggle_a_refused_target_past_its_refusal():
    """The early return must sit AFTER the refusal, never in front of it.

    A refused target means the archive would land colocated with the live store
    (g-372-16). If --print-target returned 0 before that check, a caller could
    read a clean exit for a destination the tool refuses to write to.
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        refused = _target(mode="refused")
        refused["verdict"] = "refused-colocated"
        refused["reason"] = "cold target is the live store"
        rc, calls = _run(monkeypatch, ["--print-target"], target=refused)
    finally:
        monkeypatch.undo()

    assert rc == 2, (
        f"--print-target returned {rc} for a REFUSED target -- a caller reads "
        "that as a usable destination")
    assert calls == [], "a refused target must not hash either"
