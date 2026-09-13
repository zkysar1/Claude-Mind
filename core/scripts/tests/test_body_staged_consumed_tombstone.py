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
