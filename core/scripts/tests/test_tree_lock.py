"""Advisory working-tree lock ().

The subject sits in front of iteration-push.sh, which EVERY Body runs EVERY
cycle. That asymmetry drives the whole test file: a lock that wrongly ALLOWS
costs one bad merge, while a lock that wrongly REFUSES silently freezes
framework sync for a box and turns "resume on local code" into permanent
staleness. So most of these tests pin the PROCEED branches, and exactly one
pins the refusal.

The bug this file exists to keep dead: `acquire` originally recorded
`os.getpid()` as the holder. It runs as a short-lived CLI invocation, so that
pid was always gone by the time anyone called `check` — every lock read as
`dead-holder`, the single blocking branch was unreachable, and the gate was
inert while looking entirely correct. It was caught by an end-to-end smoke run,
not by any unit test, which is why the pid-provenance tests below are here.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tree_lock as tl  # noqa: E402


MINE = "sid-mine-0001"
THEIRS = "sid-theirs-0002"


def _rec(**over):
    base = {"holder_sid": THEIRS, "holder_agent": "alpha", "holder_pid": os.getpid(),
            "reason": "suite run", "acquired_at": time.time(), "ttl_seconds": 600}
    base.update(over)
    return base


# ── the ONE refusal ─────────────────────────────────────────────────────────


def test_fresh_lock_from_a_live_foreign_holder_BLOCKS():
    # The only condition that may ever block. os.getpid() is this test process,
    # which is by definition alive.
    v = tl.evaluate(_rec(), MINE)
    assert v["blocked"] is True
    assert v["state"] == "held"


def test_the_blocking_message_names_the_holder_and_the_reason():
    # A gate that refuses without saying who holds the tree sends the reader to
    # debug their own box. Both facts must be in the one line they will see.
    v = tl.evaluate(_rec(reason="full-suite run"), MINE)
    assert THEIRS[:8] in v["reason"]
    assert "full-suite run" in v["reason"]


# ── everything else PROCEEDS (the fail-safe direction) ──────────────────────


class TestFailsOpen:
    def test_absent_lock_proceeds(self):
        assert tl.evaluate(None, MINE)["blocked"] is False

    def test_my_own_lock_proceeds(self):
        # A Body must never deadlock against itself — this is the branch that
        # makes the lock re-entrant across the many scripts one unit runs.
        assert tl.evaluate(_rec(holder_sid=MINE), MINE)["blocked"] is False

    def test_expired_lock_proceeds(self):
        v = tl.evaluate(_rec(acquired_at=time.time() - 5000, ttl_seconds=600), MINE)
        assert v["blocked"] is False
        assert v["state"] == "expired"

    def test_dead_holder_proceeds_before_the_ttl_expires(self):
        # Self-healing: a killed suite frees the tree at once rather than after
        # 90 minutes. 999999 is not a live pid on any box this runs on.
        v = tl.evaluate(_rec(holder_pid=999999), MINE)
        assert v["blocked"] is False
        assert v["state"] == "dead-holder"

    def test_missing_holder_sid_proceeds(self):
        assert tl.evaluate({"acquired_at": time.time(), "ttl_seconds": 600}, MINE)["blocked"] is False

    def test_missing_timestamps_proceed(self):
        assert tl.evaluate({"holder_sid": THEIRS}, MINE)["blocked"] is False

    def test_garbage_timestamps_proceed(self):
        v = tl.evaluate(_rec(acquired_at="not-a-number", ttl_seconds=None), MINE)
        assert v["blocked"] is False

    def test_unreadable_file_reads_as_absent(self, tmp_path):
        d = tmp_path / "mind_api" / "state"
        d.mkdir(parents=True)
        (d / tl.LOCK_FILENAME).write_text("{not json", encoding="utf-8")
        assert tl.read_lock(tmp_path) is None
        assert tl.evaluate(tl.read_lock(tmp_path), MINE)["blocked"] is False

    def test_a_json_list_instead_of_an_object_reads_as_absent(self, tmp_path):
        d = tmp_path / "mind_api" / "state"
        d.mkdir(parents=True)
        (d / tl.LOCK_FILENAME).write_text("[]", encoding="utf-8")
        assert tl.read_lock(tmp_path) is None


# ── pid provenance: the inert-gate bug ──────────────────────────────────────


class TestHolderPidProvenance:
    def test_acquire_does_NOT_record_its_own_pid(self, tmp_path):
        # THE REGRESSION GUARD. Recording os.getpid() here is what made the gate
        # inert: the CLI process is gone microseconds later, so every subsequent
        # check saw a dead holder and proceeded. If this assertion ever fails,
        # the blocking branch has become unreachable again.
        rc, _ = tl.acquire(tmp_path, MINE, "alpha", "unit work")
        assert rc == tl.RC_OK
        rec = tl.read_lock(tmp_path)
        assert "holder_pid" not in rec, (
            "acquire must not stamp its own short-lived pid — with it, every "
            f"lock reads dead-holder and nothing is ever blocked: {rec}")

    def test_no_pid_still_blocks_a_foreign_body(self, tmp_path):
        # The consequence that matters: absent liveness info must NOT be read as
        # "free". Unknown falls back to the TTL, which is unexpired here.
        tl.acquire(tmp_path, THEIRS, "alpha", "suite run")
        assert tl.evaluate(tl.read_lock(tmp_path), MINE)["blocked"] is True

    def test_an_explicit_live_pid_is_recorded_and_blocks(self, tmp_path):
        tl.acquire(tmp_path, THEIRS, "alpha", "suite", holder_pid=os.getpid())
        rec = tl.read_lock(tmp_path)
        assert rec["holder_pid"] == os.getpid()
        assert tl.evaluate(rec, MINE)["blocked"] is True

    @pytest.mark.parametrize("bad", [0, -1, "1234", None])
    def test_a_nonsense_pid_is_not_recorded_rather_than_stored_wrong(self, tmp_path, bad):
        tl.acquire(tmp_path, THEIRS, "alpha", "suite", holder_pid=bad)
        assert "holder_pid" not in tl.read_lock(tmp_path)

    def test_pid_alive_returns_None_not_False_for_a_missing_pid(self):
        # None and False are NOT interchangeable here: False means "provably
        # gone, steal the lock", None means "cannot tell, respect the TTL".
        # Collapsing them would let any pid-less lock be stolen instantly.
        assert tl._pid_alive(None) is None
        assert tl._pid_alive("abc") is None
        assert tl._pid_alive(os.getpid()) is True


# ── acquire / release ───────────────────────────────────────────────────────


class TestAcquireRelease:
    def test_acquire_refuses_when_a_live_foreign_holder_exists(self, tmp_path):
        tl.acquire(tmp_path, THEIRS, "alpha", "theirs", holder_pid=os.getpid())
        rc, info = tl.acquire(tmp_path, MINE, "alpha", "mine")
        assert rc == tl.RC_REFUSED
        assert info["blocked"] is True

    def test_force_takes_the_lock_and_records_what_it_displaced(self, tmp_path):
        # An override with no audit trail is indistinguishable from a bug the
        # next time someone asks why a suite was invalidated.
        tl.acquire(tmp_path, THEIRS, "alpha", "theirs", holder_pid=os.getpid())
        rc, _ = tl.acquire(tmp_path, MINE, "alpha", "mine", force="operator override")
        assert rc == tl.RC_OK
        rec = tl.read_lock(tmp_path)
        assert rec["holder_sid"] == MINE
        assert rec["forced_over"]["holder_sid"] == THEIRS
        assert rec["forced_over"]["why"] == "operator override"

    def test_reacquire_by_the_same_body_is_allowed(self, tmp_path):
        tl.acquire(tmp_path, MINE, "alpha", "first", holder_pid=os.getpid())
        rc, _ = tl.acquire(tmp_path, MINE, "alpha", "second", holder_pid=os.getpid())
        assert rc == tl.RC_OK
        assert tl.read_lock(tmp_path)["reason"] == "second"

    def test_release_only_removes_MY_lock(self, tmp_path):
        # Releasing a sibling's lock is the exact failure this module prevents;
        # it must be a reported no-op, not an error and not a removal.
        tl.acquire(tmp_path, THEIRS, "alpha", "theirs", holder_pid=os.getpid())
        rc, info = tl.release(tmp_path, MINE)
        assert rc == tl.RC_OK
        assert info["state"] == "not-mine"
        assert tl.read_lock(tmp_path) is not None

    def test_release_is_idempotent_on_an_absent_lock(self, tmp_path):
        rc, info = tl.release(tmp_path, MINE)
        assert rc == tl.RC_OK
        assert info["state"] == "absent"

    def test_acquire_refuses_an_empty_sid_rather_than_writing_an_inert_lock(self, tmp_path):
        # `holder_sid: ""` reads back as MALFORMED and is treated as free, so the
        # acquirer would be unprotected while believing otherwise -- the same
        # silent-inertness shape as the holder_pid bug. Refuse instead, and write
        # NOTHING: a half-written lock file would be worse than none.
        rc, info = tl.acquire(tmp_path, "", "alpha", "suite", holder_pid=os.getpid())
        assert rc == tl.RC_PLUMBING
        assert info["state"] == "no-sid"
        assert tl.read_lock(tmp_path) is None

    def test_acquire_reports_plumbing_failure_rather_than_a_false_success(self, tmp_path):
        # A caller that believes it holds a lock it does not is worse off than
        # one told the write failed. Simulated by making state/ a FILE.
        (tmp_path / "mind_api").mkdir()
        (tmp_path / "mind_api" / "state").write_text("blocker", encoding="utf-8")
        rc, info = tl.acquire(tmp_path, MINE, "alpha", "x")
        assert rc == tl.RC_PLUMBING
        assert info["state"] == "write-failed"


# ── the CLI contract iteration-push depends on ──────────────────────────────


class TestCliContract:
    def _run(self, capsys, argv, sid):
        os.environ["MIND_SID"] = sid
        rc = tl.main(argv)
        capsys.readouterr()
        return rc

    def test_check_returns_1_when_blocked_but_status_returns_0(self, tmp_path, capsys):
        # `status` is a diagnostic read and must never be mistakable for a
        # decision — a human running it should not get a refusal exit code.
        tl.acquire(tmp_path, THEIRS, "alpha", "suite", holder_pid=os.getpid())
        root = ["--project-root", str(tmp_path)]
        assert self._run(capsys, ["check", *root], MINE) == tl.RC_REFUSED
        assert self._run(capsys, ["status", *root], MINE) == tl.RC_OK

    def test_check_never_returns_2(self, tmp_path, capsys):
        # For the GATE specifically, an indeterminate state must still mean
        # proceed. rc=2 from check would be read by iteration-push as neither
        # go nor refuse, and its fail-soft contract would have to guess.
        (tmp_path / "mind_api" / "state").mkdir(parents=True)
        (tmp_path / "mind_api" / "state" / tl.LOCK_FILENAME).write_text("~~", encoding="utf-8")
        assert self._run(capsys, ["check", "--project-root", str(tmp_path)], MINE) == tl.RC_OK

    def test_check_on_a_free_tree_returns_0(self, tmp_path, capsys):
        assert self._run(capsys, ["check", "--project-root", str(tmp_path)], MINE) == tl.RC_OK

    def test_acquire_writes_valid_json_a_second_process_can_read(self, tmp_path, capsys):
        self._run(capsys, ["acquire", "--reason", "x", "--project-root", str(tmp_path)], MINE)
        raw = (tmp_path / "mind_api" / "state" / tl.LOCK_FILENAME).read_text(encoding="utf-8")
        assert json.loads(raw)["holder_sid"] == MINE

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlink support")
    def test_two_spellings_of_one_root_reach_the_SAME_lock(self, tmp_path, capsys):
        # The lock's two halves arrive by different routes: the suite runner takes
        # the __file__ default (symlink-resolved), iteration-push passes its own
        # $REPO (a logical `cd .. && pwd`, which is NOT). Without the .resolve() in
        # main() those are two different lock FILES -- the writer locks one, the
        # reader finds the other absent, and the gate is inert while every
        # hand-test passes. Same shape as the holder-pid bug, different door.
        real = tmp_path / "real"
        real.mkdir()
        alias = tmp_path / "alias"
        try:
            os.symlink(real, alias, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - platform gate
            pytest.skip("symlink creation not permitted here")
        assert self._run(capsys, ["acquire", "--reason", "s",
                                  "--project-root", str(real)], THEIRS) == tl.RC_OK
        # Read it back through the OTHER spelling; the foreign sid must be refused,
        # which can only happen if both spellings resolved to one file.
        assert self._run(capsys, ["check", "--project-root", str(alias)], MINE) == tl.RC_REFUSED
        assert not (alias / "mind_api").is_symlink()  # sanity: alias is the dir link
        assert len(list(real.glob("mind_api/state/*.json"))) == 1


# ── the gate wiring in iteration-push.sh ────────────────────────────────────


class TestIterationPushWiring:
    """Static pins on the two properties of the call site that are invisible here.

    A behavioural test would need a live co-resident Body, so these pin the shape
    instead. Both pinned properties are ones a later 'tidy-up' would plausibly
    undo, and both fail in the expensive direction: a wrongly-REFUSING gate
    freezes a box's framework sync indefinitely, where a wrongly-allowing one
    costs a single bad merge.
    """

    @property
    def _src(self):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "iteration-push.sh")
        with open(p, encoding="utf-8") as fh:
            return fh.read()

    def test_only_rc_1_skips_never_bare_if_not(self):
        # `if ! tree-lock.sh check` treats EVERY non-zero as "held", including a
        # missing interpreter (127) or a broken _paths.sh -- plumbing faults that
        # would then freeze this box's sync forever (guard-1562).
        src = self._src
        assert 'tree-lock.sh" check' in src, "the gate call site is gone"
        assert 'if ! _TL_OUT="$(bash "$SCRIPT_DIR/tree-lock.sh" check' not in src, (
            "the gate reverted to a bare `if !`, which reads a plumbing fault as a "
            "held tree and fails CLOSED")
        assert '_TL_RC" -eq 1 ' in src or '_TL_RC" -eq 1;' in src, (
            "the skip branch must test rc == 1 explicitly")

    def test_the_gate_is_scoped_to_the_repo_being_merged(self):
        # Without --project-root "$REPO", a hermetic --repo run consults THIS
        # machine's real lock, so test_iteration_push.py would pass or fail on
        # whether a co-resident Body happened to be running a suite.
        assert 'check --project-root "$REPO"' in self._src
