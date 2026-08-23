"""Body-keyed WM isolation UNDER CONCURRENT WRITES ().

THE GAP THIS FILLS, stated against what already exists so the overlap is not
re-litigated. Two files already cover neighbouring halves:

  test_wm_body_routing.py   — 14 tests, pure PATH RESOLUTION: which file does a
                              given Body resolve to, on the CLI side and the
                              daemon side. It never writes anything.
  test_wm_advisory_lock.py  — a main()-style stress test (pytest-INVISIBLE; it
                              runs via run-invisible-suites.sh) that spawns N
                              concurrent writers against ONE file — the
                              AGENT-WIDE WM — each on a DISJOINT slot, and
                              proves the advisory lock stops them clobbering
                              each other.

So "the path is right" is pinned, and "one file survives concurrent writers" is
pinned. What was NOT pinned is the property g-306-204 actually names —
*body-keyed store isolation under concurrent writes*: several Bodies writing the
SAME slot AT THE SAME TIME, each to its OWN file, without leaking into each
other's. That is a different claim from either neighbour, and note it is the
case the lock CANNOT help with: `wm_lock_path()` is body-aware, so two Bodies
hold DIFFERENT locks and never serialise against one another. Isolation here is
a property of the path routing holding up under concurrency, not of mutual
exclusion — which is exactly why it needs its own behavioural test.

WHY THIS WRITES VIA THE CLI AND READS THE FILES DIRECTLY. There is a known
writer/reader asymmetry on this surface (guard-862 / guard-3375, measured on
cc-07 2026-08-17 and recorded in test_wm_advisory_lock.py's own `_env`): the
`wm.py` WRITER honours `BODY_WM_PATH`, while the daemon READER resolves the WM
from the agent+unit_key instead — so on a worker Body a daemon read-back of a
body write returns null. Routing the read-back through the daemon would
therefore measure that asymmetry rather than isolation. Reading the YAML files
directly removes the daemon from the loop entirely and asserts the one thing in
question: where did each Body's bytes actually land.

Hermetic: every Body writes to a tmp file. Nothing touches the real agent-wide
WM, and no daemon is required.

Run:
  py -3 -m pytest core/scripts/tests/test_body_wm_concurrent_isolation.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

CORE_ROOT = Path(__file__).resolve().parent.parent.parent
CORE_SCRIPTS = CORE_ROOT / "scripts"
WM_PY = CORE_SCRIPTS / "wm.py"
sys.path.insert(0, str(CORE_SCRIPTS))

import wm  # noqa: E402

# encoding_capture deliberately: it is a registered ARRAY slot (so `append` is
# legal) and it is the one capture lane with NO entry in `array_limits`, so a
# cap eviction cannot silently remove an entry mid-test and make an isolation
# failure look like a cap. Assert that rather than trusting it.
SLOT = "encoding_capture"

BODIES = 3          # concurrent Bodies
PER_BODY = 4        # appends each


def test_the_slot_under_test_is_uncapped_so_eviction_cannot_confound_the_result():
    """Guard on the fixture itself. If encoding_capture ever gains a cap below
    PER_BODY, the isolation assertions below would start failing for a reason
    that has nothing to do with isolation — and the failure message would point
    at the wrong thing. Fail HERE instead, with the real reason.
    """
    assert SLOT in wm.ARRAY_SLOTS, f"{SLOT} must be an array slot for append"
    limits = getattr(wm, "ARRAY_LIMITS", None)
    if limits is None:                       # name drift — read it off the config
        limits = (wm.load_config().get("array_limits") or {}
                  if hasattr(wm, "load_config") else {})
    cap = (limits or {}).get(SLOT)
    assert cap is None or cap >= BODIES * PER_BODY, (
        f"{SLOT} now has a cap of {cap}; this test appends up to "
        f"{BODIES * PER_BODY} entries and eviction would confound the isolation "
        f"assertions. Pick an uncapped array slot or lower PER_BODY."
    )


def _seed(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump({"slots": {}}, default_flow_style=False),
                    encoding="utf-8")


def _entry(body: int, n: int) -> dict:
    return {"goal_id": f"g-iso-{body}-{n:02d}",
            "fact": f"body{body} entry{n}",
            "evidence": f"marker-body-{body}"}


def _spawn(body_wm: Path | None, body: int, n: int) -> subprocess.Popen:
    """One `wm.py append` subprocess. body_wm=None -> no BODY_WM_PATH set."""
    env = os.environ.copy()
    env.pop("BODY_WM_PATH", None)
    if body_wm is not None:
        env["BODY_WM_PATH"] = str(body_wm)
    return subprocess.Popen(
        [sys.executable, str(WM_PY), "append", SLOT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )


def _run_all(procs, payloads):
    """Feed EVERY process's stdin and close it, THEN wait for all of them.

    THE SHAPE IS THE TEST. The obvious version — `proc.communicate(...)` in a
    loop — looks concurrent because every process was Popen'd up front, but
    communicate() blocks until that process EXITS, and each child blocks on
    stdin until it is fed. So the run is fully SERIAL and this file would assert
    isolation under no concurrency at all, i.e. pass vacuously.

    Measured on cc-07 (uname -r 6.8.0-137-generic) while writing this: 6 writers
    took 2.41s under the communicate-loop and 0.62s under the shape below —
    **3.89x**, which is the serialisation, not noise. Do not "simplify" this
    back into a communicate() loop.

    Returns the count of processes still RUNNING at the instant the feed loop
    finished — a deterministic witness that they overlapped, used by the caller.
    A Python interpreter start is ~100ms+ while the feed loop is sub-millisecond,
    so this cannot be flaky-low on a machine that runs processes at all.
    """
    for proc, payload in zip(procs, payloads):
        proc.stdin.write(json.dumps(payload))
        proc.stdin.close()
    still_running = sum(1 for p in procs if p.poll() is None)
    errors = []
    for proc in procs:
        proc.wait(timeout=60)
        err = proc.stderr.read()
        proc.stdout.close()
        proc.stderr.close()
        if proc.returncode != 0:
            errors.append((proc.returncode, err[:300]))
    return still_running, errors


def _slot_of(path: Path) -> list:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return (data.get("slots") or {}).get(SLOT) or []


def test_concurrent_bodies_writing_the_same_slot_do_not_cross_contaminate(tmp_path):
    """THE PROPERTY. N Bodies append to the SAME slot at the same time, each
    with its own BODY_WM_PATH. Every entry must land in exactly one file — its
    author's — and no file may hold a foreign entry.

    A leak here is not a cosmetic defect: capture-lane entries are keyed by
    goal_id and merged into the reducer by content hash, so one Body's payload
    appearing in another Body's staged WM would be attributed to the wrong goal
    and merged twice.
    """
    paths = {b: tmp_path / f"body{b}" / "working-memory.yaml" for b in range(BODIES)}
    for p in paths.values():
        _seed(p)

    procs, payloads = [], []
    for n in range(PER_BODY):                 # interleave bodies, not blocks
        for b in range(BODIES):
            procs.append(_spawn(paths[b], b, n))
            payloads.append(_entry(b, n))
    overlapped, errors = _run_all(procs, payloads)
    assert not errors, f"{len(errors)} append subprocess(es) failed: {errors[:3]}"

    # The concurrency WITNESS. Without it a future refactor could serialise the
    # writers (see _run_all) and this file would keep passing while testing
    # nothing about concurrency at all.
    assert overlapped >= 2, (
        f"only {overlapped} of {len(procs)} writers were still running when the "
        f"feed loop finished — the writes did NOT overlap, so the isolation "
        f"assertions below say nothing about CONCURRENT access"
    )

    for b, path in paths.items():
        got = _slot_of(path)
        assert len(got) == PER_BODY, (
            f"body{b} holds {len(got)} entries, expected {PER_BODY} — a "
            f"concurrent write was LOST (contents: {got!r})"
        )
        foreign = [e for e in got
                   if e.get("evidence") != f"marker-body-{b}"]
        assert not foreign, (
            f"body{b}'s WM holds {len(foreign)} entry(ies) authored by another "
            f"Body: {foreign!r} — body-keyed routing did NOT hold under "
            f"concurrent writes, and the reducer would attribute this payload "
            f"to the wrong goal"
        )


def test_positive_control_the_same_writers_all_land_when_pointed_at_one_file(tmp_path):
    """WITHOUT THIS, THE TEST ABOVE PASSES VACUOUSLY.

    "No file holds a foreign entry" is satisfied trivially by writes that never
    happen, by a slot name the writer rejects, or by a harness whose processes
    do not actually overlap. Point the IDENTICAL harness at ONE file: all
    BODIES*PER_BODY entries must land there. That proves the writers run, that
    `append` reaches this slot, and that the concurrency is real contention on a
    single file rather than serialised no-ops.
    """
    shared = tmp_path / "shared" / "working-memory.yaml"
    _seed(shared)

    procs, payloads = [], []
    for n in range(PER_BODY):
        for b in range(BODIES):
            procs.append(_spawn(shared, b, n))
            payloads.append(_entry(b, n))
    overlapped, _ = _run_all(procs, payloads)
    assert overlapped >= 2, (
        f"only {overlapped} writers overlapped; this control cannot speak to "
        f"contention on a shared file"
    )

    got = _slot_of(shared)
    assert len(got) == BODIES * PER_BODY, (
        f"positive control landed {len(got)} of {BODIES * PER_BODY} entries in "
        f"one file. The isolation assertions above are therefore NOT evidence — "
        f"either the writers are not running, `append` is rejecting {SLOT}, or "
        f"the advisory lock is dropping concurrent writes to a shared file."
    )


def test_the_lock_moves_with_the_wm_path_so_one_file_never_has_two_locks(monkeypatch,
                                                                        tmp_path):
    """The invariant that makes BOTH failure directions impossible.

    Isolation here does not come from mutual exclusion — two Bodies hold
    different locks by design. What must never happen is the two halves drifting
    apart, and each direction has a distinct consequence:

      lock body-keyed + path agent-wide -> two writers, DIFFERENT locks, SAME
        file: silent clobbering, which is precisely what the advisory lock was
        added to prevent.
      lock agent-wide + path body-keyed -> unrelated Bodies serialise on one
        lock: correct but needlessly slow, and it hides the first defect.

    Pinning them as siblings for an arbitrary BODY_WM_PATH forbids both.
    """
    body = tmp_path / "bodyX" / "working-memory.yaml"
    monkeypatch.setenv("BODY_WM_PATH", str(body))
    assert wm.wm_path() == body
    assert wm.wm_lock_path() == body.with_suffix(".lock"), (
        "the lock is no longer a sibling of the effective WM path — two Bodies "
        "could now share a file while holding different locks"
    )

    monkeypatch.delenv("BODY_WM_PATH", raising=False)
    agent_wide, agent_lock = wm.wm_path(), wm.wm_lock_path()
    assert agent_wide != body, "body routing collapsed onto the agent-wide path"
    assert agent_lock == agent_wide.with_suffix(".lock")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
