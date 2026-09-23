""" outcome 3 — the re-push-after-consume resurrection guard.

THE DEFECT THIS PINS, and note it was OPENED by the fix that preceded it.
`body-merge._delete_staged` removes the staged triple's store object AND a local
file, but that unlink runs on the REDUCER's filesystem. The ORIGIN box's legacy
copy is untouched by a remote consume, so its next `push-staged` for that
unitKey found no world-side copy, relocated the legacy one again, re-pushed it,
and the reducer merged the SAME divergence a second time — and a 3-way delta
applied twice double-counts every counter it touches.

Before the destination moved to `world/` (outcome 1) that door was closed BY
ACCIDENT: the push simply failed `NoClaimError`, so nothing ever reached the
store to be resurrected. Making the push work is what opened it, which is why
this guard is part of the same goal rather than a later cleanup.

WHAT THE OUTCOME ACTUALLY DEMANDS, verbatim: "a unitKey already consumed by
_consume_staged is not re-staged by a later push from the origin box." That is a
statement about the PRODUCER's behaviour after the CONSUMER has acted, so the
tests below drive both halves for real and meet in the middle at the tombstone.

WHY THE CLI TEST EXISTS ALONGSIDE THE UNIT ONES (guard-1943 / guard-6374). This
goal's own previous unit recorded that a pre-existing test called the staging
function DIRECTLY and therefore stayed green straight through a live defect in
the bash/CLI wiring. A predicate is not wired until the real call path reads it,
and neither a green suite nor a mutation proof can tell you that. So the
end-to-end test goes through `main(["push-staged", ...])`.

NEGATIVE CONTROLS ARE THE POINT. Every suppression assertion here has a paired
run with NO tombstone that must still push, because "nothing was written" is the
PASS condition for a suppression test and is also what a thoroughly broken
producer looks like (guard-2903: an invariance test is green by default when it
is broken; guard-4166: a fix whose effect is that something stops appearing
needs a positive control that does NOT flip).

Daemon-safe: pure path + file arithmetic against a recording fake backend.

Run:
  STORAGE_BACKEND=local python -m pytest \
      core/scripts/tests/test_body_staged_consumed_tombstone.py -q
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent


def _load(mod_name: str, filename: str):
    """Load a hyphen-named CLI module by path (the house idiom)."""
    spec = importlib.util.spec_from_file_location(mod_name, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


bm = _load("body_manifest", "body-manifest.py")
bmerge = _load("body_merge_tombstone", "body-merge.py")

UNIT = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


class RecordingBackend:
    """Local-filesystem backend that RECORDS every write and delete.

    The recording is what makes "was this re-pushed?" observable. Asserting on
    the filesystem alone cannot answer it: the producer's whole job is to write
    a file that may already be there, so a content check passes whether the push
    happened or was correctly suppressed.
    """

    def __init__(self):
        self.writes: list[str] = []
        self.deletes: list[str] = []

    def write_bytes(self, path, data):
        self.writes.append(Path(path).name)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(data)

    def list_dir(self, path):
        p = Path(path)
        return [c.name for c in p.iterdir()] if p.is_dir() else []

    def delete_object(self, path):
        self.deletes.append(Path(path).name)
        try:
            Path(path).unlink()
        except OSError:
            pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Point the staged-WM root at a TMP world (guard-955 containment).

    Load-bearing, not tidiness: `world_staged_dir` resolves through
    `_paths.WORLD_DIR`, so without this a PRODUCER test writes into the LIVE
    world — the exact leak this goal's previous unit measured twice.
    """
    import _paths
    w = tmp_path / "world"
    w.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_paths, "WORLD_DIR", w, raising=False)
    return w


@pytest.fixture
def agent(tmp_path):
    """agents/alpha/session plus its legacy staging dir."""
    state = tmp_path / "agents" / "alpha" / "session"
    (state / "pending-body-merges").mkdir(parents=True, exist_ok=True)
    return state


def _stage_legacy(state_dir: Path, unit_key: str, body: str = "counter: 3\n"):
    """Write a full legacy triple, the way the bash crash-preserve path does."""
    d = state_dir / "pending-body-merges"
    (d / f"{unit_key}-wm.yaml").write_text(body, encoding="utf-8")
    (d / f"{unit_key}-wm.hash").write_text("notthehash", encoding="utf-8")
    (d / f"{unit_key}-wm-baseline.yaml").write_text("counter: 1\n", encoding="utf-8")
    return d


@pytest.fixture
def ws(world, agent):
    """The world-rooted staged dir for THIS agent, derived the way production
    derives it.

    Deliberately a fixture rather than an inline `world_staged_dir(world)`:
    that function takes an AGENT DIR and reads the agent NAME off it, so handing
    it the world root yields `body-staged-wm/world` — a real directory that
    simply never contains a tombstone, making every suppression check answer
    "not consumed" while looking correct. That is not hypothetical; the first
    run of this file did exactly that and two tests failed for a reason that had
    nothing to do with the code under test. It is the same mis-derivation
    `unit_already_consumed`'s docstring warns about, which is why the parameter
    is passed in rather than re-derived.
    """
    d = bm.world_staged_dir(agent.parent)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------- the CONSUMER half

def test_consume_writes_a_tombstone_for_every_disposition(tmp_path, ws):
    """All four disposing branches tombstone; the DEFERRED one must not.

    The deferred case is the one that would cause real loss if it tombstoned: a
    transport error left the bytes unread, so the unit is deliberately left
    staged for the next drain. Tombstoning it would tell the origin box the
    content was accounted for when nothing ever read it.
    """
    be = RecordingBackend()
    bmerge._mark_consumed(be, ws, UNIT)
    marker = ws / f"{UNIT}-wm.consumed"
    assert marker.is_file(), "the tombstone must exist locally"
    assert marker.name in be.writes, "it must ALSO reach the store — a worker on " \
        "another box can only see it there"
    rec = json.loads(marker.read_text(encoding="utf-8"))
    assert rec["unit_key"] == UNIT
    assert rec["consumed_at"] and rec["consumed_by"], \
        "provenance is what lets a reader tell a real consume from a stray file"

    # NEGATIVE CONTROL: a unit nobody consumed has no tombstone, so the positive
    # assertions above cannot be passing against a directory of marker files.
    assert not (ws / f"{OTHER}-wm.consumed").exists()


def test_mark_consumed_never_raises_when_the_store_rejects_it(tmp_path, ws):
    """A drain that has already merged real content must not abort on a marker."""
    class Hostile(RecordingBackend):
        def write_bytes(self, path, data):
            raise RuntimeError("store refused")

    bmerge._mark_consumed(Hostile(), ws, UNIT)  # must not raise


# ---------------------------------------------------------- the PRODUCER half

def test_relocate_skips_a_consumed_unit_and_copies_one_that_is_not(
        tmp_path, agent, ws):
    """Both directions in ONE test so the suppression cannot pass vacuously."""
    _stage_legacy(agent, UNIT)
    _stage_legacy(agent, OTHER)
    (ws / f"{UNIT}-wm.consumed").write_text("{}", encoding="utf-8")

    suppressed = bm.relocate_legacy_staging(agent, UNIT)
    allowed = bm.relocate_legacy_staging(agent, OTHER)

    assert suppressed == [], "a consumed unit must not be re-staged"
    assert not (ws / f"{UNIT}-wm.yaml").exists(), \
        "and nothing may land in the world dir for it"
    # POSITIVE CONTROL — the same call, same fixture, no tombstone: still works.
    assert sorted(allowed) == [f"{OTHER}-wm-baseline.yaml", f"{OTHER}-wm.hash",
                               f"{OTHER}-wm.yaml"]
    assert (ws / f"{OTHER}-wm.yaml").exists()


def test_push_staged_files_skips_a_consumed_unit_and_reports_success(
        tmp_path, agent, ws, monkeypatch):
    """Suppressed push returns True, not False.

    The bash caller prints "this Body's WM will not reach the reducer" on a
    False. For a consumed unit that warning would be a lie — a reducer already
    merged it — and a false alarm on the one channel that reports real stranding
    is how a real stranding stops being believed.
    """
    be = RecordingBackend()
    import storage_backend
    monkeypatch.setattr(storage_backend, "get_backend", lambda *a, **k: be)

    for uk in (UNIT, OTHER):
        (ws / f"{uk}-wm.yaml").write_text("counter: 3\n", encoding="utf-8")
    (ws / f"{UNIT}-wm.consumed").write_text("{}", encoding="utf-8")

    assert bm.push_staged_files(ws, UNIT) is True
    assert f"{UNIT}-wm.yaml" not in be.writes, "a consumed unit must not re-push"

    # POSITIVE CONTROL: the un-tombstoned sibling pushes through the same call.
    assert bm.push_staged_files(ws, OTHER) is True
    assert f"{OTHER}-wm.yaml" in be.writes


def test_unit_already_consumed_fails_OPEN_when_the_store_is_unreachable(
        tmp_path, ws):
    """The fail direction is the whole safety argument — pin it explicitly.

    A false "not consumed" costs at most ONE double-merge, which the 3-way
    baseline bounds to a counter re-add. A false "consumed" silently DISCARDS a
    Body's only copy of its divergence, which nothing recovers. If someone ever
    "hardens" this to fail-closed, this test is what should stop them.
    """
    class Blind(RecordingBackend):
        def list_dir(self, path):
            raise RuntimeError("store unreachable")

    assert bm.unit_already_consumed(ws, UNIT, backend=Blind()) is False

    # ...and a LOCAL tombstone is still honoured when the store cannot answer,
    # so failing open does not mean ignoring evidence this box already holds.
    (ws / f"{UNIT}-wm.consumed").write_text("{}", encoding="utf-8")
    assert bm.unit_already_consumed(ws, UNIT, backend=Blind()) is True


# ------------------------------------------------- the WIRING (guard-6374)

def test_push_staged_CLI_does_not_resurrect_a_consumed_unit(
        tmp_path, agent, ws, monkeypatch):
    """THE OUTCOME-3 PREDICATE, through the real CLI the bash actually calls.

    Driven via `main(["push-staged", ...])` rather than by calling the two
    functions, because this goal's previous unit measured a test that called
    the function directly and stayed green through a live wiring defect.
    """
    be = RecordingBackend()
    import storage_backend
    monkeypatch.setattr(storage_backend, "get_backend", lambda *a, **k: be)
    monkeypatch.setattr(bm, "_agent_paths",
                        lambda a, s: (None, None, agent), raising=False)

    _stage_legacy(agent, UNIT)
    (ws / f"{UNIT}-wm.consumed").write_text("{}", encoding="utf-8")

    rc = bm.main(["push-staged", "--sid", UNIT, "--agent", "alpha"])

    assert rc == 0, "a suppressed re-push is a satisfied push, not a failure"
    assert not (ws / f"{UNIT}-wm.yaml").exists(), \
        "the legacy triple must NOT have been relocated back into world/"
    assert f"{UNIT}-wm.yaml" not in be.writes, "and must NOT have been re-pushed"

    # POSITIVE CONTROL through the SAME CLI entry: without the tombstone the
    # identical invocation relocates and pushes. Without this, a CLI that had
    # been broken into a no-op would pass every assertion above.
    _stage_legacy(agent, OTHER)
    rc2 = bm.main(["push-staged", "--sid", OTHER, "--agent", "alpha"])
    assert rc2 == 0
    assert (ws / f"{OTHER}-wm.yaml").exists()
    assert f"{OTHER}-wm.yaml" in be.writes


def test_END_TO_END_a_real_consume_then_a_real_push_does_not_resurrect(
        tmp_path, world, monkeypatch):
    """THE CHAIN, not its halves: real `generalize_down` -> real `push-staged`.

    Every other test here proves one side and meets the other at a tombstone
    written by hand. That is two halves, and outcome 3 asks for the CHAIN: "a
    unitKey already consumed by _consume_staged is not re-staged by a later push
    from the origin box." A hand-written marker cannot show that the consumer
    writes the SAME name the producer reads — which is the single most likely
    way this mechanism rots (guard-6374: not wired until the consumer predicate
    reads it, and neither a green suite nor a mutation proof tells you that).

    So this test never constructs a tombstone. It stages a triple, lets the real
    reducer drain it, and then asks the real producer to push it again.
    """
    import yaml
    be = RecordingBackend()
    import storage_backend
    monkeypatch.setattr(storage_backend, "get_backend", lambda *a, **k: be)

    pr = tmp_path
    state = pr / "agents" / "alpha" / "session"
    state.mkdir(parents=True, exist_ok=True)
    (state / "working-memory.yaml").write_text(
        yaml.dump({"slots": {"active_context": {"a": 1}}}), encoding="utf-8")

    staged = bm.world_staged_dir(state.parent)
    staged.mkdir(parents=True, exist_ok=True)
    body = {"slots": {"spark_capture": [{"goal_id": "g-9-01", "note": "payload"}]}}
    (staged / f"{UNIT}-wm.yaml").write_text(yaml.dump(body), encoding="utf-8")
    (staged / f"{UNIT}-wm-baseline.yaml").write_text(
        yaml.dump({"slots": {}}), encoding="utf-8")

    # 1. THE REAL CONSUME.
    summary = bmerge.generalize_down("alpha", project_root=pr)
    assert UNIT in summary["staged_merged"], summary
    reducer = yaml.safe_load((state / "working-memory.yaml").read_text(encoding="utf-8"))
    assert reducer["slots"]["spark_capture"] == body["slots"]["spark_capture"], \
        "precondition: the payload really did reach the reducer"
    assert not (staged / f"{UNIT}-wm.yaml").exists(), "and the triple was consumed"

    # 2. THE TOMBSTONE THE CONSUMER ITSELF WROTE — name never typed by this test.
    assert bm.unit_already_consumed(staged, UNIT, backend=be) is True, \
        "the producer's predicate must read the name the consumer actually wrote"

    # 3. THE REAL PUSH, from the origin box's surviving legacy copy.
    legacy = state / "pending-body-merges"
    legacy.mkdir(parents=True, exist_ok=True)
    for suf in ("-wm.yaml", "-wm-baseline.yaml"):
        (legacy / f"{UNIT}{suf}").write_text(yaml.dump(body), encoding="utf-8")
    monkeypatch.setattr(bm, "_agent_paths",
                        lambda a, s: (None, None, state), raising=False)
    be.writes.clear()

    rc = bm.main(["push-staged", "--sid", UNIT, "--agent", "alpha"])

    assert rc == 0
    assert not (staged / f"{UNIT}-wm.yaml").exists(), \
        "the consumed unit must NOT have been relocated back into world/"
    assert f"{UNIT}-wm.yaml" not in be.writes, "and must NOT have been re-pushed"

    # POSITIVE CONTROL, same CLI, same fixture: an un-consumed unit still flows.
    for suf in ("-wm.yaml", "-wm-baseline.yaml"):
        (legacy / f"{OTHER}{suf}").write_text(yaml.dump(body), encoding="utf-8")
    assert bm.main(["push-staged", "--sid", OTHER, "--agent", "alpha"]) == 0
    assert f"{OTHER}-wm.yaml" in be.writes


def test_the_tombstone_is_invisible_to_both_staged_WM_readers(tmp_path, ws):
    """guard-2616: extending a declared set is not additive until you MEASURE
    that the matcher cannot read the new member.

    A tombstone mis-globbed as a Body WM would be parsed as YAML, fail, and be
    dispositioned as `skipped` — silently consuming the triple it was meant to
    protect. Asserted against the REAL glob and the REAL listing filter, with the
    genuine WM name as the positive control so the assertions cannot pass
    against a matcher that matches nothing at all.
    """
    (ws / f"{UNIT}-wm.yaml").write_text("counter: 1\n", encoding="utf-8")
    (ws / f"{UNIT}-wm.consumed").write_text("{}", encoding="utf-8")

    globbed = {p.name for p in ws.glob("*-wm.yaml")}
    listed = {n for n in (c.name for c in ws.iterdir())
              if n.endswith("-wm.yaml")}

    assert f"{UNIT}-wm.consumed" not in globbed
    assert f"{UNIT}-wm.consumed" not in listed
    # POSITIVE CONTROL: both matchers DO see the real staged WM.
    assert f"{UNIT}-wm.yaml" in globbed and f"{UNIT}-wm.yaml" in listed


# ------------------------------------------- the ORDERING half ()
#
# The tests above prove the tombstone is WRITTEN. These prove WHEN. The
# tombstone is the ACK the origin box reads, and the reducer-WM persist is a
# SEPARATE durable write that can fail while the drain keeps going — so a
# tombstone written at the disposition decision ACKs a delivery that has not
# happened. Measured 2026-09-12T11:19: one cc-07 reducer drain tombstoned 10
# cross-box units and persisted nothing; the triples survived (a later drain
# can still re-merge them) but all 10 PRODUCERS were permanently gated from
# re-pushing. guard-953 is the general form: an ACK follows the durable write.
#
# TWO-SIDED BY CONSTRUCTION, because "no tombstone" is the PASS condition of
# the failure case and is also what a completely inert _consume_staged looks
# like (guard-2903 / guard-4166). Both directions therefore assert
# summary["staged_merged"] == [UNIT] — proof the MERGED branch actually ran —
# and the success case asserts the tombstone IS there, so the failure case
# cannot be passing because tombstoning stopped working altogether.
#
# SCOPE, stated so the pin is not over-read: this pins the ORDERING, i.e. that
# a persist which RAISES leaves no ACK. It does not and cannot pin a persist
# that returns successfully and is later lost (an own-cloud write conflict —
# rb-3636 / guard-3209). That is the other branch of the same outcome and needs
# a durability barrier, not an ordering change.


class _ReadableRecordingBackend(RecordingBackend):
    """RecordingBackend plus the authoritative read `_consume_staged` prefers.

    Without it the drain falls back to the local read, which also works here —
    but that fallback shares its except-branch with the transport-error path
    that DEFERS a unit, and a deferred unit never reaches the merged branch.
    The test would then pass while measuring nothing.
    """

    def read_authoritative_bytes(self, path):
        return Path(path).read_bytes()


class _OrderWitnessBackend(_ReadableRecordingBackend):
    """Records, for every delete, whether the tombstone was already on disk.

    Ordering cannot be read off the final filesystem state — tombstone-then-
    delete and delete-then-tombstone leave byte-identical trees. It has to be
    witnessed AT the delete.
    """

    def __init__(self, marker: Path):
        super().__init__()
        self._marker = marker
        self.tombstone_present_at_delete: list[bool] = []

    def delete_object(self, path):
        self.tombstone_present_at_delete.append(self._marker.is_file())
        super().delete_object(path)


def _stage_world(ws: Path, unit_key: str):
    """A world-staged triple whose hash sidecar deliberately does NOT match, so
    guard 2 (unchanged-from-baseline) misses and the unit takes the MERGED
    branch — the only branch with a deferred persist."""
    (ws / f"{unit_key}-wm.yaml").write_text("counter: 3\n", encoding="utf-8")
    (ws / f"{unit_key}-wm.hash").write_text("notthehash", encoding="utf-8")
    (ws / f"{unit_key}-wm-baseline.yaml").write_text("counter: 1\n", encoding="utf-8")


def _fresh_summary() -> dict:
    return {"scanned": 0, "staged_merged": [], "staged_dedup": [],
            "staged_deferred": [], "noop": [], "skipped": []}


def _drive_consume(monkeypatch, tmp_path, agent, ws, *, persist_raises: bool,
                   backend=None):
    """Run a real _consume_staged over one merged unit. Returns (summary, be)."""
    be = backend if backend is not None else _ReadableRecordingBackend()
    monkeypatch.setattr(bmerge, "_get_backend", lambda: be)

    # Deliberately NOT given the reducer WM's production basename: the function
    # takes this path as a PARAMETER and derives nothing from its name, while
    # that basename trips the framework's direct-store-write gate even inside a
    # tmp fixture. The pin is unaffected and the live store stays untouched.
    reducer_wm = tmp_path / "reducer-wm-fixture.yaml"
    reducer_wm.write_text("counter: 10\n", encoding="utf-8")

    import contextlib
    # The lock is pinned by test_wm_lock_spans_read_write_g115_8667; here it is
    # only noise, and a real lockfile adds a failure mode this pin does not own.
    monkeypatch.setattr(bmerge.wm, "wm_lock_for",
                        lambda p: contextlib.nullcontext(), raising=False)

    if persist_raises:
        def _boom(path, data):
            raise RuntimeError("simulated non-persisting reducer (cc-07 shape)")
        monkeypatch.setattr(bmerge, "_write_yaml_atomic", _boom)

    _stage_world(ws, UNIT)
    summary = _fresh_summary()
    if persist_raises:
        with pytest.raises(RuntimeError):
            bmerge._consume_staged(agent, reducer_wm, summary, set())
    else:
        bmerge._consume_staged(agent, reducer_wm, summary, set())
    return summary, be


def test_a_failed_persist_leaves_NO_tombstone_and_the_triple_intact(
        tmp_path, agent, ws, monkeypatch):
    """THE LOAD-BEARING DIRECTION. No delivery, therefore no ACK."""
    summary, _be = _drive_consume(monkeypatch, tmp_path, agent, ws,
                                  persist_raises=True)

    assert summary["staged_merged"] == [UNIT], (
        "precondition: the unit must have taken the MERGED branch — a deferred "
        "or noop unit never reaches the persist, so the assertions below would "
        "hold vacuously")

    assert not (ws / f"{UNIT}-wm.consumed").exists(), (
        "a persist that never landed must not tell the origin box the unit was "
        "consumed — that is the g-115-9876 defect: 10 producers gated against a "
        "delivery that never happened")
    assert (ws / f"{UNIT}-wm.yaml").is_file(), (
        "and the triple must survive, so a later drain from a box that CAN "
        "persist still delivers it")
    assert (ws / f"{UNIT}-wm.hash").is_file()
    assert (ws / f"{UNIT}-wm-baseline.yaml").is_file()


def test_a_successful_persist_DOES_tombstone_and_deletes_the_triple(
        tmp_path, agent, ws, monkeypatch):
    """THE POSITIVE CONTROL. Without it the test above is satisfied by a
    _consume_staged that never tombstones anything at all."""
    summary, _be = _drive_consume(monkeypatch, tmp_path, agent, ws,
                                  persist_raises=False)

    assert summary["staged_merged"] == [UNIT]
    assert (ws / f"{UNIT}-wm.consumed").is_file(), (
        "a durably-merged unit MUST be tombstoned, or the origin box re-pushes "
        "it and the 3-way delta is applied twice (g-115-9750 outcome 3)")
    assert not (ws / f"{UNIT}-wm.yaml").exists(), (
        "and the triple is consumed exactly once")


def test_the_tombstone_still_precedes_the_delete_after_the_reorder(
        tmp_path, agent, ws, monkeypatch):
    """Moving the tombstone past the PERSIST must not move it past the DELETE.

    Tombstone-then-delete is the invariant `_mark_consumed`'s docstring
    protects: delete-first leaves triple gone / no tombstone, and the origin
    resurrects it. Witnessed at the delete, because the final tree is identical
    under either order.
    """
    be = _OrderWitnessBackend(ws / f"{UNIT}-wm.consumed")
    summary, _ = _drive_consume(monkeypatch, tmp_path, agent, ws,
                                persist_raises=False, backend=be)

    assert summary["staged_merged"] == [UNIT]
    assert be.tombstone_present_at_delete, (
        "precondition: no authoritative delete was witnessed at all, so this "
        "test measured nothing")
    assert all(be.tombstone_present_at_delete), (
        "every delete on the merged path must find the tombstone already "
        "written; a False here is a regression to delete-first")


# ------------------------------------------- the CONSUMER half, THROUGH THE CLI
#
# The three tests above drive `_consume_staged` DIRECTLY. That proves the
# function is correct and says nothing about whether the production entry point
# still reaches it — the guard-1943 / guard-6374 split ("a predicate is not
# wired until the real call path reads it, and neither a green suite nor a
# mutation proof can tell you that") which THIS FILE'S OWN DOCSTRING raises for
# the PRODUCER half and answers there with `main(["push-staged", ...])`. The
# consumer had no equivalent arm; 's progress_note named that as the
# honest residual outstanding against outcome 2. This is it.
#
# Production path, verbatim: main(["generalize-down", ...]) -> generalize_down()
# -> _consume_staged(). With no sessions/ dir and no unit dirs in the store,
# generalize_down takes its early branch (body-merge.py:1110) — drain staged
# orphans, stamp, return — which is the SHORTEST real path from the CLI to the
# consumer and exercises no sessions-pass machinery this pin does not own.


def _drive_generalize_down_cli(monkeypatch, tmp_path, agent, ws, *,
                               persist_raises: bool, backend=None):
    """Stage one world unit and wire the CLI to run against tmp_path."""
    be = backend if backend is not None else _ReadableRecordingBackend()
    monkeypatch.setattr(bmerge, "_get_backend", lambda: be)
    # main() calls generalize_down(args.agent) with no project_root, so the root
    # is resolved, not passed. This is the seam that makes a CLI-level arm
    # possible at all.
    monkeypatch.setattr(bmerge, "_project_root", lambda: tmp_path)

    # generalize_down DERIVES the reducer WM path from bm._WM_FILENAME instead of
    # taking it as a parameter, so the production basename cannot be side-stepped
    # the way _drive_consume side-steps it. Repoint the constant: the framework's
    # direct-store-write gate stays satisfied and the whole tree is under
    # tmp_path, so the live store is untouched either way.
    monkeypatch.setattr(bm, "_WM_FILENAME", "reducer-wm-fixture.yaml", raising=False)
    (agent / "reducer-wm-fixture.yaml").write_text("counter: 10\n", encoding="utf-8")

    import contextlib
    monkeypatch.setattr(bmerge.wm, "wm_lock_for",
                        lambda p: contextlib.nullcontext(), raising=False)

    if persist_raises:
        def _boom(path, data):
            raise RuntimeError("simulated non-persisting reducer (cc-07 shape)")
        monkeypatch.setattr(bmerge, "_write_yaml_atomic", _boom)

    _stage_world(ws, UNIT)
    return be


def test_CLI_generalize_down_actually_reaches_the_consumer(
        tmp_path, agent, ws, monkeypatch, capsys):
    """WIRING. This is the arm that fails if someone unhooks _consume_staged
    from generalize_down, or moves the staged drain behind a branch the early
    return never takes — a change every direct-call test above stays green
    through."""
    _drive_generalize_down_cli(monkeypatch, tmp_path, agent, ws,
                               persist_raises=False)

    rc = bmerge.main(["generalize-down", "--agent", "alpha"])
    assert rc == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["staged_merged"] == [UNIT], (
        "precondition AND the point of this test: the unit must have taken the "
        "MERGED branch having been driven from the CLI. An empty staged_merged "
        "here with the direct-call tests still green is exactly the wiring "
        "regression this arm exists to catch")
    assert (ws / f"{UNIT}-wm.consumed").is_file(), (
        "a durably-merged unit MUST be tombstoned through the production entry "
        "point too, not only when _consume_staged is called by hand")
    assert not (ws / f"{UNIT}-wm.yaml").exists()


def test_CLI_generalize_down_writes_NO_tombstone_when_the_persist_fails(
        tmp_path, agent, ws, monkeypatch):
    """THE LOAD-BEARING DIRECTION, through the CLI: no delivery, no ACK.

    Non-vacuity is carried by `match=` rather than by a summary assertion: the
    RuntimeError is raised INSIDE the persist, so it propagating out of main()
    is itself proof that execution reached the persist inside _consume_staged.
    A run that never reached the consumer would leave the same clean tree and
    raise nothing — which is precisely the indistinguishability this arm closes.
    """
    _drive_generalize_down_cli(monkeypatch, tmp_path, agent, ws,
                               persist_raises=True)

    with pytest.raises(RuntimeError, match="simulated non-persisting reducer"):
        bmerge.main(["generalize-down", "--agent", "alpha"])

    assert not (ws / f"{UNIT}-wm.consumed").exists(), (
        "a persist that never landed must not tell the origin box the unit was "
        "consumed — the g-115-9876 defect, now pinned at the production entry "
        "point and not only at the function")
    assert (ws / f"{UNIT}-wm.yaml").is_file(), (
        "and the triple must survive, so a later drain from a box that CAN "
        "persist still delivers it")
    assert (ws / f"{UNIT}-wm.hash").is_file()
    assert (ws / f"{UNIT}-wm-baseline.yaml").is_file()
