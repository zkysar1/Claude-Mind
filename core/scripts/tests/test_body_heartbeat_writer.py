"""Per-Body heartbeat WRITER in heartbeat-tick.sh — .

The reader half (`_same_box_body_is_live` in the claim endpoint) is pinned by
test_claim_worker_vs_worker.py, which seeds the heartbeat file directly. That
proves the FUNCTION and says nothing about the WIRING (guard-1943): with no
writer the reader returns False on every call and the whole fix is inert in
production, while every reader test stays green. This file pins the writer.

WHY heartbeat-tick.sh IS THE RIGHT WRITER (rb-4589): a liveness heartbeat must
be supervisor-emitted and UNCONDITIONAL, never piggybacked on a discretionary
step. This script is called once per aspirations-loop iteration (Phase -0.5),
once from /start, and every 60s from interruptible-sleep.sh during long waits —
so a Body that is alive but idle still reports fresh.

WHAT THESE TESTS PIN:
  1. a bound Body gets agents/<agent>/sessions/<SID>/body-heartbeat, and the
     mtime ADVANCES on a second tick (it is a heartbeat, not a create-once
     marker — a create-once file would read fresh forever and wedge the goal).
  2. the agent-wide runner-heartbeat still advances in the same tick. The body
     write must not displace or short-circuit it.
  3. no MIND_SID -> no body write, and the rest of the tick still runs. An
     unbound call must not abort the script (`set -euo pipefail` is active).
  4. session dir ABSENT -> no write and NO mkdir. /start owns session-dir
     creation; inventing one here is what path-resolution.md L1 refuses, and
     the absent file is the fail-open direction the reader depends on.

HERMETIC (g-306-206): every test stages a RELOCATED PROJECT_ROOT and drives the
tick from there. The MIND_AGENT_DIR seam alone is NOT enough and never was —
`session-state-get.sh` is IRREDUCIBLY LOCAL and derives PROJECT_ROOT from its
OWN location (`$0/../..`), honoring no seam but MIND_AGENT. So under the seam
alone the IDLE gate reads the REAL box's agent-state: these tests passed on a
live-RUNNING box and went red on any IDLE box — including every cross-box worker
box, which is IDLE by design. Machine state, not code, decided the colour.
Relocating is the only lever, and test 5 already proved it (g-306-208); this
file now applies it uniformly.

Two properties follow, and both are load-bearing:
  - the staged root COPIES scripts, never symlinks them (guard-2534). _paths.sh
    derives PROJECT_ROOT with a logical cd+pwd, which does NOT resolve symlinks,
    while _paths.py uses Path(__file__).resolve(), which DOES — so a symlinked
    core/scripts silently addresses the tmp root from bash and the REAL repo
    from Python. Copying also removes the os.symlink privilege dependency that
    made test 5 SKIP on Windows without developer mode; a skip is not a pass,
    and the worker boxes are exactly where this coverage matters.
  - the shared-store writers are stubbed to no-ops, so NOTHING here reaches the
    real world. guard-2484 permits driving them under the BOUND agent name
    (the side effect equals a normal tick), but not needing that permission is
    strictly better: it also closes the RESIDUAL noted below, since a defaulted
    write key cannot matter when there is no shared write to key.

STORAGE_BACKEND=local (guard-955) keeps any remaining downstream write off a
remote store.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _runtime_bash import bash_cmd  # noqa: E402

# parents: [0]=tests [1]=scripts [2]=core [3]=repo root. Absolute script path
# so the run does not depend on cwd resolution.
REPO = Path(__file__).resolve().parents[3]
SCRIPT = str(REPO / "core" / "scripts" / "heartbeat-tick.sh")
SID = "88888888-9999-aaaa-bbbb-888888888888"

# Deliberately the BOUND agent, not a literal name and not a synthetic one.
# MIND_AGENT_DIR redirects the agent DIR to tmp, but heartbeat-tick's
# team-state write is keyed on the agent NAME and lands in the REAL world
# regardless (MIND_WORLD does not redirect it — measured). So a synthetic name
# creates a permanent phantom agent row in the shared store, and a partner's
# name would forge a heartbeat on their behalf. The bound agent's own row is
# the one this script legitimately advances every iteration, so using it makes
# the side effect identical to a normal tick: no phantom, no forgery.
# NO LITERAL FALLBACK. `os.environ.get("MIND_AGENT") or "<name>"` silently
# reintroduced the very forgery this comment argues against (zeta fresh-eyes-code
# finding zeta-fec-agent-fallback-forges-liveness-202608030740, guard-2484): on an
# UNBOUND run the tick would execute AS that literal agent and stamp
# agent_status.<name>.last_active = now in the SHARED world. That is the one
# direction check-team-state-before-silent.md rule 5 says never to get wrong — a
# FRESH last_active is positive evidence of life, so a forged one makes a DEAD
# agent read as alive (a stale value is merely ambiguous).
# LATENT rather than live: pytest collects this file, so under run-full-suite.sh
# it inherits hook-injected MIND_AGENT.
#
# THE SKIP BELOW IS CURRENTLY UNREACHABLE UNDER PYTEST, and saying so is the
# point — measured 2026-08-04, not assumed. conftest.py selects an agent when
# MIND_AGENT is unset ("honor an externally-set MIND_AGENT if present, else
# pick the first available agent"), so by the time this module is imported the
# var is always populated. `env -u MIND_AGENT python3 -c ...` returns None,
# confirming the re-supply is conftest and not the shell.
# So removing the literal does NOT by itself close the forgery hole; it removes
# the hardcoded name (never default a shared-store write key to a literal agent)
# and installs a loud failure for any path that ever reaches here unbound.
# RESIDUAL — CLOSED for this file by , and the reason generalises. The
# note here used to read: conftest's "first available agent" is a defaulted write
# key one layer up, so on a box where that agent is not the running one, tests 1-4
# still stamp agent_status.<that-agent>.last_active. That is still true of conftest
# and still belongs in conftest — but it can no longer bite HERE, because the
# staged root stubs every shared-store writer to a no-op. A defaulted write key is
# harmless when there is no write to key. AGENT now names only a tmp directory.
AGENT = os.environ.get("MIND_AGENT")
if not AGENT:
    import pytest

    pytest.skip(
        "MIND_AGENT unset — refusing to run unbound. This test drives the real "
        "heartbeat-tick.sh, whose team-state write is keyed on the agent NAME and "
        "lands in the SHARED world regardless of MIND_AGENT_DIR/MIND_WORLD "
        "(guard-2484). Defaulting to a literal name here would forge a partner-"
        "visible liveness heartbeat. Since g-306-206 this is DEFENCE IN DEPTH "
        "rather than the operative guard: _stage_root stubs the shared-store "
        "writers it knows about (team-state-update, live-phase-emit, runner-claim) "
        "to no-ops, so a defaulted name would key no write. The skip stays because "
        "that list is enumerated, not derived — it holds only for the writers "
        "someone remembered, and a forged partner-visible heartbeat is not a "
        "failure mode worth risking on an enumeration.",
        allow_module_level=True,
    )


def _stub(path: Path, body: str) -> None:
    """A tiny executable bash stub at `path`."""
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _stage_root(tmp: Path, *, with_session_dir: bool,
                state: str = "RUNNING") -> tuple[Path, Path]:
    """A relocated PROJECT_ROOT whose agent-state the test actually controls.

    Returns (root, agent_dir).

    `state` defaults to RUNNING (the REDUCER's shape); pass "IDLE" for the
    cross-box WORKER shape — see test 5.

    WHY RELOCATE AT ALL: MIND_AGENT_DIR redirects the agent DIR, but the IDLE
    gate calls `session-state-get.sh`, which derives PROJECT_ROOT from its own
    location and honors no seam but MIND_AGENT. Under the seam alone the gate
    reads the REAL box's state, so these tests were decided by the machine they
    ran on. Relocating the SCRIPT is the only thing that moves the file the gate
    reads. Both seams are still set, because they cover different things: the
    root moves the state read, MIND_AGENT_DIR moves the heartbeat writes.

    COPY, NEVER SYMLINK (guard-2534): _paths.sh resolves PROJECT_ROOT with a
    logical cd+pwd (symlinks NOT resolved) while _paths.py uses
    Path(__file__).resolve() (symlinks resolved), so a symlinked core/scripts
    puts the bash half on the tmp root and the Python half on the REAL repo,
    silently. Copying also drops the os.symlink privilege dependency that made
    this file's IDLE test SKIP on Windows without developer mode.

    Only the two scripts the tick genuinely needs are copied. Every sibling it
    shells out to is STUBBED to a no-op — deliberately, not for speed: those are
    the writes that escape every seam. `team-state-update.sh` writes
    agent_status.<name> into the REAL world regardless of MIND_AGENT_DIR and
    MIND_WORLD (guard-2484), so stubbing it is what makes this file's HERMETIC
    claim literally true rather than merely permitted.
    """
    root = tmp / "root"
    scripts = root / "core" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("heartbeat-tick.sh", "_paths.sh"):
        shutil.copy2(REPO / "core" / "scripts" / name, scripts / name)
    _stub(scripts / "session-state-get.sh", f"echo {state}")
    # No-op the shared-store writers. Each call site in heartbeat-tick.sh is
    # `|| true` guarded, so ABSENT files would also "work" — but then the
    # isolation would rest on those guards staying in place, and a stub says
    # what is meant. runner-claim.sh only fires under own-cloud; stubbed anyway
    # so STORAGE_BACKEND is not the only thing standing between a test and DDB.
    for name in ("team-state-update.sh", "live-phase-emit.sh", "runner-claim.sh"):
        _stub(scripts / name, "exit 0")

    adir = root / "agents" / AGENT
    (adir / "session").mkdir(parents=True, exist_ok=True)
    # Seeded for realism and for the stub above to agree with; the gate reads
    # the stub, so this file is documentation rather than the deciding input.
    (adir / "session" / "agent-state").write_text(state, encoding="utf-8")
    if with_session_dir:
        (adir / "sessions" / SID).mkdir(parents=True, exist_ok=True)
    return root, adir


def _tick(root: Path, agent_dir: Path, *,
          sid: str | None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["STORAGE_BACKEND"] = "local"
    env["MIND_AGENT"] = AGENT
    # MIND_AGENT_DIR is the PUBLIC name of the seam (_paths.sh copies it into
    # the internal _AGENT_DIR_OVERRIDE). Setting the internal name has no
    # effect at all — the tick then resolves the REAL agent dir and writes a
    # live agent's heartbeat, which is how this test first ran.
    env["MIND_AGENT_DIR"] = str(agent_dir)
    # DERIVE the world/meta isolation instead of enumerating it. The staged root
    # has no local-paths.conf and no .mind-data/, so without these two lines
    # _paths.sh falls through to the INHERITED MIND_WORLD/MIND_META -- the
    # launching shell's production world (guard-2337: that env is launch-shape
    # dependent, and a live agent's shell exports it). heartbeat-tick.sh happens
    # to reference neither today (measured: 0 hits for WORLD_PATH/META_PATH), so
    # the stubs above are currently sufficient -- but that makes hermeticity rest
    # on an enumerated stub list staying complete. Pinning the roots makes a
    # future direct $WORLD_PATH write land in tmp instead of production.
    # Rooted off the `root` PARAMETER, never a .parent chain off agent_dir --
    # that chain is the  off-by-one class (CLAUDE.md's third audit
    # grep), and here a miscount would silently place the tmp world one level
    # ABOVE the staged root.
    for _var, _sub in (("MIND_WORLD", "world"), ("MIND_META", "meta")):
        _p = root / _sub
        _p.mkdir(parents=True, exist_ok=True)
        env[_var] = str(_p)
    if sid is None:
        env.pop("MIND_SID", None)
    else:
        env["MIND_SID"] = sid
    return subprocess.run(bash_cmd(str(root / "core" / "scripts" / "heartbeat-tick.sh")),
                          cwd=str(root), env=env,
                          capture_output=True, text=True, timeout=180)


def _body_hb(agent_dir: Path) -> Path:
    return agent_dir / "sessions" / SID / "body-heartbeat"


# --- 1. the writer writes, and the mtime ADVANCES ---------------------------
def test_body_heartbeat_written_and_advances():
    with tempfile.TemporaryDirectory() as tmpd:
        root, adir = _stage_root(Path(tmpd), with_session_dir=True)
        r1 = _tick(root, adir, sid=SID)
        hb = _body_hb(adir)
        assert hb.exists(), (
            "a bound Body must get a per-Body heartbeat; without it the claim "
            f"CAS reads every live worker as dormant. stderr={r1.stderr[-400:]}")
        first = hb.stat().st_mtime
        # Backdate, then tick again: a heartbeat must ADVANCE, where a
        # create-once marker would read fresh forever and wedge the goal.
        old = time.time() - 3600
        os.utime(hb, (old, old))
        _tick(root, adir, sid=SID)
        assert hb.stat().st_mtime > old, (
            "the second tick must ADVANCE the body heartbeat mtime -- a "
            "create-once file would report a crashed Body as alive forever")
        assert first > 0


# --- 2. the agent-wide heartbeat is not displaced ---------------------------
def test_agent_wide_heartbeat_still_advances():
    with tempfile.TemporaryDirectory() as tmpd:
        root, adir = _stage_root(Path(tmpd), with_session_dir=True)
        runner = adir / "session" / "runner-heartbeat"
        runner.write_text("", encoding="utf-8")
        old = time.time() - 3600
        os.utime(runner, (old, old))
        _tick(root, adir, sid=SID)
        assert runner.stat().st_mtime > old, (
            "the per-Body write must not displace or short-circuit the "
            "agent-wide runner-heartbeat touch")


# --- 3. unbound call: no body write, and the tick still completes -----------
def test_no_sid_skips_body_write_without_aborting():
    with tempfile.TemporaryDirectory() as tmpd:
        root, adir = _stage_root(Path(tmpd), with_session_dir=True)
        runner = adir / "session" / "runner-heartbeat"
        runner.write_text("", encoding="utf-8")
        old = time.time() - 3600
        os.utime(runner, (old, old))
        r = _tick(root, adir, sid=None)
        assert not _body_hb(adir).exists(), (
            "no MIND_SID means no Body identity -- nothing to write")
        assert runner.stat().st_mtime > old, (
            "an unbound call must still complete the rest of the tick; under "
            f"`set -euo pipefail` a bad guard would abort. stderr={r.stderr[-400:]}")


# --- 4. absent session dir: no write, and NO mkdir --------------------------
def test_absent_session_dir_is_not_created():
    with tempfile.TemporaryDirectory() as tmpd:
        root, adir = _stage_root(Path(tmpd), with_session_dir=False)
        runner = adir / "session" / "runner-heartbeat"
        runner.write_text("", encoding="utf-8")
        old = time.time() - 3600
        os.utime(runner, (old, old))
        r = _tick(root, adir, sid=SID)
        assert not (adir / "sessions" / SID).exists(), (
            "the tick must NEVER create a session dir -- /start owns that, and "
            "inventing one for an unbound SID is what path-resolution.md L1 "
            "refuses")
        assert not _body_hb(adir).exists()
        assert runner.stat().st_mtime > old, (
            "the rest of the tick must still run when the session dir is "
            f"absent. stderr={r.stderr[-400:]}")


# --- 5. IDLE box: body heartbeat STILL written, agent-wide one still refused -
def test_idle_box_writes_body_heartbeat_but_not_agent_wide():
    """The cross-box WORKER shape ().

    Tests 1-4 all seed agent-state=RUNNING, which is the REDUCER's shape. A
    cross-box worker Body never flips agent-state, so its box is IDLE BY
    DESIGN — and the state gate's `exit 2` used to fire before the per-Body
    write was reached. The per-SID liveness signal was therefore absent on
    exactly the box exposed to a claim pop, while all four tests above stayed
    green. Textbook guard-1479: a write below an early short-circuit passes
    every correctness check and still never runs in production.

    This test pins BOTH halves of the fix, and the second half is what keeps
    the hoist honest:
      - the per-Body heartbeat IS written on an IDLE box, and
      - the agent-WIDE runner-heartbeat is STILL refused there.
    The gate exists to stop `runner-heartbeat` going fresh against IDLE (the
    alpha-2026-05-13 heartbeat_without_running desync, guard-543). Hoisting the
    per-SID write must not weaken that. If a future edit hoists the agent-wide
    touch too, the second assertion fails.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        # The IDLE shape comes from _stage_root, which relocates PROJECT_ROOT
        # and stubs the state reader — the same mechanism this test pioneered
        # under , now shared by all five (). It staged by
        # SYMLINK and skipped where os.symlink needs privilege; both are fixed
        # in the helper (guard-2534, and a skip is not a pass on the very
        # worker boxes this test exists for).
        root, adir = _stage_root(Path(tmpd), with_session_dir=True,
                                 state="IDLE")
        runner = adir / "session" / "runner-heartbeat"
        runner.write_text("", encoding="utf-8")
        old = time.time() - 3600
        os.utime(runner, (old, old))

        r = _tick(root, adir, sid=SID)

        assert (adir / "sessions" / SID / "body-heartbeat").exists(), (
            "a bound Body on an IDLE box MUST still get a per-Body heartbeat -- "
            "a cross-box worker is IDLE by design, so this is the exact box "
            "where the stranded-claim sweep can pop a live claim. "
            f"rc={r.returncode} stderr={r.stderr[-400:]}")

        assert runner.stat().st_mtime == old, (
            "the agent-WIDE runner-heartbeat must STILL be refused on an IDLE "
            "box -- that gate prevents the heartbeat_without_running desync "
            "(guard-543) and the per-Body hoist must not weaken it")

        assert r.returncode == 2, (
            "the state gate must still REFUSE (exit 2) on an IDLE box; the "
            f"hoist only moves the per-SID write above it. rc={r.returncode}")


# --- 6. the carrier carries body_state () --------------------------
#
# THE THIRD WRITER. body-manifest.set_state/park_body/resume_body mirror the
# state on TRANSITION, and their tests cover that. This one covers the writer
# that keeps a LIVE Body's carrier current, which is the larger population: if
# it silently stopped stamping, every running Body would read
# `stale_state_unknown` on the peer side, the between-units stall verdict would
# never fire again, and NOTHING would go red -- the detector would return to the
# blindness  closed, silently. Exercised through the real script in a
# staged root, because that is the only shape that proves the shell quoting and
# the YAML extraction actually work (guard-920).

def _carrier(agent_dir: Path) -> Path:
    return agent_dir / "session" / f"body-heartbeat-{SID}.json"


def _write_manifest(agent_dir: Path, state: str) -> None:
    (agent_dir / "sessions" / SID).mkdir(parents=True, exist_ok=True)
    (agent_dir / "sessions" / SID / "body-manifest.yaml").write_text(
        # Single-quoted, which is how _render_manifest actually emits it --
        # the extraction has to strip those, and a test using bare values
        # would pass against an extractor that cannot.
        f"unit_key: '{SID}'\nbody_state: '{state}'\nrole: 'worker'\n",
        encoding="utf-8")


def test_carrier_carries_the_body_state_from_the_manifest():
    import json
    for state in ("active", "parked", "closed-pending-merge"):
        with tempfile.TemporaryDirectory() as tmpd:
            root, adir = _stage_root(Path(tmpd), with_session_dir=True)
            _write_manifest(adir, state)
            r = _tick(root, adir, sid=SID)
            c = _carrier(adir)
            assert c.exists(), f"no carrier written. stderr={r.stderr[-400:]}"
            doc = json.loads(c.read_text(encoding="utf-8"))
            assert doc["body_state"] == state, (
                f"carrier lost the manifest's body_state ({state!r}); the peer "
                f"stall probe then cannot tell a closed Body from a dead one. "
                f"doc={doc}")
            # The pre-existing contract must survive the added field.
            assert doc["sid"] == SID
            assert doc["ts"], "the timestamp decides staleness and must remain"


def test_carrier_is_valid_json_with_an_absent_manifest():
    """FAIL-OPEN. A Body with no readable manifest still gets a well-formed
    carrier with an EMPTY state -- which the reader renders
    `stale_state_unknown` (never alerts), so a manifest problem can never be
    reported as a stall on its own."""
    import json
    with tempfile.TemporaryDirectory() as tmpd:
        root, adir = _stage_root(Path(tmpd), with_session_dir=True)
        # deliberately no body-manifest.yaml
        r = _tick(root, adir, sid=SID)
        c = _carrier(adir)
        assert c.exists(), f"no carrier written. stderr={r.stderr[-400:]}"
        doc = json.loads(c.read_text(encoding="utf-8"))  # must still parse
        assert doc["body_state"] == ""
        assert doc["sid"] == SID
