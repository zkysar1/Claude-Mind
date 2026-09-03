""" — the session/-rooted carrier that lets capture_fast_lane reach a
Body on ANOTHER box.

WHAT THESE PIN, and why the central one needs a diverging fixture backend.

capture_fast_lane reads `agents/<agent>/sessions/<unitKey>/working-memory.yaml`.
`sessions` is in owncloud_sync._EXCLUDE_DIRS and OwnCloudBackend._machine_local,
so for a Body on another box that file exists in NEITHER the local tree NOR the
store — the lane's store-listing union can never produce it. Measured
2026-08-16 (alpha worker d1aec55b on cc-07, reducer cc-04): the store's whole
sessions/ listing for alpha held ONE unit key, not this Body's, while the Body
held 107 flagged entries.

A test that writes the Body's WM into tmp_path and reads it back CANNOT catch
that: on one filesystem, local-and-remote are the same place, so the blindness
is invisible by construction. The load-bearing case below therefore builds a
backend whose STORE diverges from the local dir — the carrier exists only in
the store, and the Body has no local sessions/ dir at all — which is the shape
the real cross-box case has.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _load(mod_name: str, filename: str):
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


import body_capture_carrier as bcc  # noqa: E402
import capture_fast_lane as cfl  # noqa: E402

bmg = _load("body_merge", "body-merge.py")

AGENT = "testagent"
CARRIER_DIRNAME = "pending-body-merges"   # the PRE- location
WORLD_CARRIER_DIRNAME = "body-carriers"   # where the carrier lives now


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

class DivergingBackend:
    """A store that does NOT mirror the local filesystem.

    Keyed by BASENAME WITHIN A DIRECTORY, because carriers live flat in one
    directory (unlike the sessions/ layout, where basenames collide across unit
    dirs and the sibling fixture must key by parent/name).

    DIRECTORY-AWARE ON PURPOSE. `read_carriers` consults TWO directories now —
    the world root and the pre-g-306-420 legacy location — so a fake that
    returned the same listing for ANY path would hand the SAME object to both
    legs and fabricate a duplicate that no real store can produce. Measured
    while writing this: three unrelated tests turned red on that artifact alone
    (`flagged_seen` 2 -> 4), which would have been "fixed" by relaxing their
    assertions and would have deleted the very counts they exist to pin.
    `files` is the WORLD carrier dir; `legacy` is
    `agents/<agent>/session/pending-body-merges`.

    `write_bytes` records into `files`, so a producer test can assert the push
    actually reached the store rather than only the local file — and a write
    landing in `legacy` would be a bug, since nothing produces there any more.
    """

    def __init__(self, files: dict[str, bytes] | None = None,
                 fail_reads: bool = False, fail_writes: bool = False,
                 legacy: dict[str, bytes] | None = None):
        self.files = dict(files or {})
        self.legacy = dict(legacy or {})
        self.fail_reads = fail_reads
        self.fail_writes = fail_writes
        self.listed: list[str] = []
        self.written: list[str] = []

    def _bucket(self, path) -> dict:
        parent = Path(path).parent.name if Path(path).suffix else Path(path).name
        return self.legacy if parent == CARRIER_DIRNAME else self.files

    def list_dir(self, path):
        p = Path(path)
        assert p.is_absolute(), (
            f"list_dir called with relative path {p} — the production "
            ".resolve() is load-bearing (a relative path makes _s3_key raise "
            "and the listing degrades silently)")
        self.listed.append(str(p))
        return sorted(self._bucket(p))

    def read_authoritative_bytes(self, path) -> bytes:
        if self.fail_reads:
            raise RuntimeError("simulated transport error")
        bucket = self._bucket(path)
        name = Path(path).name
        if name not in bucket:
            raise FileNotFoundError(name)
        return bucket[name]

    def write_bytes(self, path, data):
        if self.fail_writes:
            raise RuntimeError("simulated write failure")
        self.written.append(Path(path).name)
        self.files[Path(path).name] = data


@pytest.fixture(autouse=True)
def _hermetic_world(tmp_path, monkeypatch):
    """Point the carrier root at a TMP world for every test in this file.

    Load-bearing, not tidiness (g-306-420). The carrier now resolves through
    `_paths.WORLD_DIR`, so without this fixture a producer test would write its
    carrier into the LIVE `world/` — and on an own-cloud box that is the
    guard-955 production-key collision class, from a test that looks hermetic
    because every path it constructs itself is under tmp_path.

    Patching the module ATTRIBUTE works because `_world_carrier_dir` does its
    `from _paths import WORLD_DIR` inside the function body; a module-level
    import there would have frozen the real path at collection time and this
    fixture would silently do nothing.
    """
    import _paths
    w = tmp_path / "world"
    w.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_paths, "WORLD_DIR", w, raising=False)
    return w


def _mk_root(tmp_path: Path) -> Path:
    (tmp_path / "agents" / AGENT / "session").mkdir(parents=True)
    (tmp_path / "agents" / AGENT / "sessions").mkdir(parents=True)
    (tmp_path / "agents" / AGENT / "session" / "working-memory.yaml").write_text(
        yaml.safe_dump({"slots": {}}), encoding="utf-8")
    return tmp_path


def _entry(gid: str, *, load_bearing: bool = True,
           ts: str = "2026-08-16T10:00:00", fact: str = "a fact") -> dict:
    e = {"goal_id": gid, "fact": fact, "_item_ts": ts}
    if load_bearing:
        e["load_bearing"] = True
    return e


def _carrier_bytes(unit_key: str, pairs) -> bytes:
    return ("\n".join(
        json.dumps({"unit_key": unit_key, "slot": slot, "entry": entry},
                   sort_keys=True)
        for slot, entry in pairs) + "\n").encode("utf-8")


def _reducer_slots(root: Path) -> dict:
    p = root / "agents" / AGENT / "session" / "working-memory.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("slots") or {}


@pytest.fixture(autouse=True)
def _no_ambient_sid(monkeypatch):
    """A worker Body's own SID must not leak in from the environment.

    fast_lane refuses to run on a worker (is_worker_body), and these tests run
    INSIDE a live worker session, so an inherited MIND_SID would make the
    refusal depend on the runner rather than the fixture.
    """
    monkeypatch.delenv("MIND_SID", raising=False)


# --------------------------------------------------------------------------
# THE CASE THE LANE WAS BUILT FOR AND COULD NOT SERVE
# --------------------------------------------------------------------------

def test_remote_body_carrier_merges_with_no_local_sessions_dir(tmp_path, monkeypatch):
    """VERIFY (1): a flagged entry from a Body whose sessions/ dir exists ONLY on
    another box reaches the reducer WM.

    The Body has NO local sessions/<unit> dir and NO store entry under
    sessions/ — exactly the production shape. Before the carrier this merged
    nothing, and the lane reported a cheerful '0 to merge'.
    """
    root = _mk_root(tmp_path)
    remote = "remote-body-sid"
    be = DivergingBackend({
        f"{remote}-fastlane.jsonl": _carrier_bytes(
            remote, [("spark_capture", _entry("g-1"))]),
    })
    monkeypatch.setattr(bmg, "_get_backend", lambda: be)

    summary = cfl.fast_lane(AGENT, project_root=root)

    assert summary["merged"] == 1, summary
    assert summary["carrier_merged"] == 1, summary
    assert summary["carrier_bodies"] == 1, summary
    slots = _reducer_slots(root)
    assert [e["goal_id"] for e in slots["spark_capture"]] == ["g-1"]
    # the Body genuinely had no local presence
    assert not (root / "agents" / AGENT / "sessions" / remote).exists()


def test_line_names_the_carrier_so_the_production_check_is_answerable(tmp_path, monkeypatch):
    """The goal's production check is 'iteration-close prints a non-zero merged
    count WITH A WORKER ON A DIFFERENT BOX'. A bare total cannot answer that —
    a same-box merge prints an identical line — so the carrier count must be
    named in the one-liner the reducer actually emits."""
    root = _mk_root(tmp_path)
    be = DivergingBackend({
        "rb-fastlane.jsonl": _carrier_bytes("rb", [("exp_capture", _entry("g-2"))]),
    })
    monkeypatch.setattr(bmg, "_get_backend", lambda: be)

    line = cfl.format_line(cfl.fast_lane(AGENT, project_root=root))
    assert "via carrier" in line, line
    assert "1 via carrier from 1 remote Body(s)" in line, line


# --------------------------------------------------------------------------
# idempotence — the trap the fast-lane docstring names
# --------------------------------------------------------------------------

def test_carrier_entries_are_copied_verbatim(tmp_path, monkeypatch):
    """No key may be added to a merged entry. Dedup is by CONTENT HASH, so a
    stamped `source_body` or `carried_at` would change the hash and make the
    later full generalize_down append a SECOND copy."""
    root = _mk_root(tmp_path)
    original = _entry("g-3", fact="verbatim matters")
    be = DivergingBackend({
        "rb-fastlane.jsonl": _carrier_bytes("rb", [("spark_capture", original)]),
    })
    monkeypatch.setattr(bmg, "_get_backend", lambda: be)

    cfl.fast_lane(AGENT, project_root=root)

    merged = _reducer_slots(root)["spark_capture"][0]
    assert merged == original, (
        f"entry was mutated in transit: {set(merged) ^ set(original)}")


def test_rerunning_the_lane_does_not_double_append(tmp_path, monkeypatch):
    """Same carrier, two passes, one copy."""
    root = _mk_root(tmp_path)
    be = DivergingBackend({
        "rb-fastlane.jsonl": _carrier_bytes("rb", [("spark_capture", _entry("g-4"))]),
    })
    monkeypatch.setattr(bmg, "_get_backend", lambda: be)

    cfl.fast_lane(AGENT, project_root=root)
    second = cfl.fast_lane(AGENT, project_root=root)

    assert len(_reducer_slots(root)["spark_capture"]) == 1
    assert second["merged"] == 0
    assert second["already_present"] == 1


def test_same_entry_in_both_carrier_and_sessions_wm_merges_once(tmp_path, monkeypatch):
    """VERIFY (3), in the shape it actually occurs: on the reducer's OWN box a
    Body is visible through BOTH sources. Content-hash dedup must collapse them,
    or every same-box Body would be counted twice the moment the carrier shipped.
    """
    root = _mk_root(tmp_path)
    unit = "local-body"
    entry = _entry("g-5")
    d = root / "agents" / AGENT / "sessions" / unit
    d.mkdir(parents=True)
    (d / "working-memory.yaml").write_text(
        yaml.safe_dump({"slots": {"spark_capture": [entry]}}), encoding="utf-8")
    (d / "body-manifest.yaml").write_text(
        yaml.safe_dump({"body_state": "active", "unit_key": unit}), encoding="utf-8")
    be = DivergingBackend({
        f"{unit}-fastlane.jsonl": _carrier_bytes(unit, [("spark_capture", entry)]),
    })
    monkeypatch.setattr(bmg, "_get_backend", lambda: be)

    summary = cfl.fast_lane(AGENT, project_root=root)

    assert len(_reducer_slots(root)["spark_capture"]) == 1
    assert summary["merged"] == 1
    # counted ONCE as a contributing Body, not twice
    assert summary["bodies_contributing"] == 1
    assert summary["by_body"][unit]["via"] == "sessions"
    # REACHABILITY CONTROL. Every assertion above is satisfiable by the
    # sessions/ leg ALONE, so without this the test passes unchanged when the
    # carrier pass is deleted — measured: it was the one survivor of the
    # consumer mutation. Proving the carrier dir was actually consulted is what
    # makes this a dedup test rather than a sessions/ test wearing its name.
    assert any(WORLD_CARRIER_DIRNAME in p for p in be.listed), (
        f"carrier dir was never listed; backend saw {be.listed}")


def test_full_generalize_down_after_the_carrier_does_not_double_append(tmp_path, monkeypatch):
    """VERIFY (3) as the goal states it: the fast lane runs, THEN the real
    close-time merge runs, and the entry exists once.

    Composed rather than inferred, and it uses the REAL producer
    (`record_local`) rather than a hand-built carrier — that detail is the
    whole test. Both passes call the same body-merge._dedup_append, so "cannot
    double-append" is derivable; but the derivation holds only while entries
    travel VERBATIM, which is a property of the PRODUCER, not of the dedup.
    Measured while writing this: with a hand-built carrier, stamping a
    `source_body` onto the entry left this test GREEN — producer corruption
    cannot reach a fixture that bypasses the producer. Driving record_local
    here is what makes that mutation fail as a genuine double-append.
    """
    root = _mk_root(tmp_path)
    unit = "closing-body"
    entry = _entry("g-10")
    d = root / "agents" / AGENT / "sessions" / unit
    d.mkdir(parents=True)
    (d / "working-memory.yaml").write_text(
        yaml.safe_dump({"slots": {"spark_capture": [entry]}}), encoding="utf-8")
    (d / "body-manifest.yaml").write_text(yaml.safe_dump({
        "unitKey": unit, "mindKey": AGENT, "env_id": "local", "role": "worker",
        # closed-pending-merge: the ONLY state generalize_down enumerates.
        "body_state": "closed-pending-merge",
        "started_at": "2026-08-16T00:00:00",
        "forked_wm_hash": "deadbeef",
    }), encoding="utf-8")
    # THE REAL PRODUCER builds the carrier — see the docstring. A hand-built
    # one would make this test blind to exactly the corruption it guards.
    assert bcc.record_local(d / "working-memory.yaml", "spark_capture",
                            entry) is not None
    be = DivergingBackend()
    monkeypatch.setattr(bmg, "_get_backend", lambda: be)

    cfl.fast_lane(AGENT, project_root=root)
    assert len(_reducer_slots(root)["spark_capture"]) == 1, "fast lane pre-condition"

    bmg.generalize_down(AGENT, project_root=root)

    after = _reducer_slots(root)["spark_capture"]
    assert len(after) == 1, (
        f"generalize_down re-appended a fast-laned entry: {after}")
    assert after[0] == entry


# --------------------------------------------------------------------------
# robustness
# --------------------------------------------------------------------------

def test_unflagged_carrier_entries_are_ignored(tmp_path, monkeypatch):
    """The producer only writes flagged entries, but the consumer re-checks:
    a hand-edited or future-format carrier must not smuggle unflagged noise
    into the priority lane."""
    root = _mk_root(tmp_path)
    be = DivergingBackend({
        "rb-fastlane.jsonl": _carrier_bytes("rb", [
            ("spark_capture", _entry("keep")),
            ("spark_capture", _entry("drop", load_bearing=False)),
        ]),
    })
    monkeypatch.setattr(bmg, "_get_backend", lambda: be)

    cfl.fast_lane(AGENT, project_root=root)

    assert [e["goal_id"] for e in _reducer_slots(root)["spark_capture"]] == ["keep"]


def test_malformed_line_does_not_strand_the_rest_of_the_body(tmp_path, monkeypatch):
    """One truncated append must not cost a Body every other flagged entry —
    the carrier is append-only and a partial write is the realistic failure."""
    root = _mk_root(tmp_path)
    good = _carrier_bytes("rb", [("spark_capture", _entry("g-good"))])
    be = DivergingBackend({
        "rb-fastlane.jsonl": b'{"unit_key": "rb", "slot": "spark_ca\n' + good,
    })
    monkeypatch.setattr(bmg, "_get_backend", lambda: be)

    cfl.fast_lane(AGENT, project_root=root)

    assert [e["goal_id"] for e in _reducer_slots(root)["spark_capture"]] == ["g-good"]


def test_transport_failure_merges_nothing_rather_than_reporting_empty(tmp_path, monkeypatch):
    """A store read that RAISES must not look like 'this Body has nothing
    flagged'. That false negative is the entire defect this carrier exists to
    remove, so it must not be reintroduced as an error path."""
    root = _mk_root(tmp_path)
    be = DivergingBackend(
        {"rb-fastlane.jsonl": _carrier_bytes("rb", [("spark_capture", _entry("g-6"))])},
        fail_reads=True)
    monkeypatch.setattr(bmg, "_get_backend", lambda: be)

    summary = cfl.fast_lane(AGENT, project_root=root)

    assert summary["merged"] == 0
    assert _reducer_slots(root).get("spark_capture") in (None, [])

    # PAIRED POSITIVE — without it this test passes vacuously. "merged == 0" is
    # also what a DELETED carrier pass produces, so an assert-absence alone
    # cannot tell "the read failed" from "the feature is gone" — which is
    # precisely the defect under test. Flipping only fail_reads must merge.
    be.fail_reads = False
    recovered = cfl.fast_lane(AGENT, project_root=root)
    assert recovered["carrier_merged"] == 1, (
        "the zero above must come from the transport failure, not from the "
        "carrier pass being unreachable")


def test_all_four_capture_lanes_travel_by_carrier(tmp_path, monkeypatch):
    """encoding_capture is UNCAPPED and the other three are not; a lane omitted
    from the carrier would be invisible rather than merely slow."""
    root = _mk_root(tmp_path)
    slots = ("spark_capture", "exp_capture", "hyp_capture", "encoding_capture")
    be = DivergingBackend({
        "rb-fastlane.jsonl": _carrier_bytes(
            "rb", [(s, _entry(f"g-{s}")) for s in slots]),
    })
    monkeypatch.setattr(bmg, "_get_backend", lambda: be)

    summary = cfl.fast_lane(AGENT, project_root=root)

    assert summary["merged"] == 4, summary
    got = _reducer_slots(root)
    for s in slots:
        assert [e["goal_id"] for e in got[s]] == [f"g-{s}"], s


# --------------------------------------------------------------------------
# producer side
# --------------------------------------------------------------------------

def test_record_local_writes_a_carrier_for_a_body_wm(tmp_path):
    root = _mk_root(tmp_path)
    wm = root / "agents" / AGENT / "sessions" / "sid-1" / "working-memory.yaml"
    wm.parent.mkdir(parents=True)
    wm.write_text("slots: {}\n", encoding="utf-8")

    entry = _entry("g-7")
    path = bcc.record_local(wm, "spark_capture", entry)

    assert path is not None
    assert path.name == "sid-1-fastlane.jsonl"
    # world/body-carriers/<agent>/ (). The old assertion here pinned
    # `session/` (singular) on the reasoning that it is the SYNCABLE dir. That
    # reasoning was true and insufficient: syncable is not the same as
    # writable, and every write under agents/<agent>/ is refused by the
    # own-cloud claim fence on any box that does not hold the runner claim —
    # i.e. on every worker Body, which is the only kind of Body that produces
    # a carrier at all.
    assert path.parent.name == AGENT
    assert path.parent.parent.name == WORLD_CARRIER_DIRNAME
    rec = json.loads(path.read_text(encoding="utf-8").strip())
    assert rec == {"unit_key": "sid-1", "slot": "spark_capture", "entry": entry}


def test_record_local_refuses_the_agent_wide_wm(tmp_path):
    """The REDUCER writes the agent-wide WM. If a carrier were emitted for it,
    the reducer would re-merge its own entries from a second source on every
    pass — dedup makes that harmless but the file is pure noise, and it would
    make `carrier_merged` (the cross-box signal) fire on a single-box fleet."""
    root = _mk_root(tmp_path)
    agent_wm = root / "agents" / AGENT / "session" / "working-memory.yaml"

    assert bcc.record_local(agent_wm, "spark_capture", _entry("g-8")) is None
    assert not (root / "world" / WORLD_CARRIER_DIRNAME / AGENT).exists()

    # PAIRED POSITIVE — assert-absence alone would also pass if record_local
    # were broken for EVERY path, so pin the discriminator in both directions
    # within the same test rather than trusting a sibling to cover it.
    assert bcc.split_body_wm_path(agent_wm) == (None, None)
    body_wm = root / "agents" / AGENT / "sessions" / "sid-x" / "working-memory.yaml"
    body_wm.parent.mkdir(parents=True)
    assert bcc.split_body_wm_path(body_wm)[1] == "sid-x"
    assert bcc.record_local(body_wm, "spark_capture", _entry("g-8b")) is not None


def test_record_local_does_not_mutate_the_entry(tmp_path):
    """Envelope metadata lives on the LINE. A key added to `entry` changes its
    content hash and breaks dedup against the sessions/ WM copy."""
    root = _mk_root(tmp_path)
    wm = root / "agents" / AGENT / "sessions" / "sid-2" / "working-memory.yaml"
    wm.parent.mkdir(parents=True)
    wm.write_text("slots: {}\n", encoding="utf-8")

    entry = _entry("g-9")
    before = dict(entry)
    path = bcc.record_local(wm, "spark_capture", entry)

    assert entry == before
    # PAIRED POSITIVE — "was not mutated" is also true when NOTHING HAPPENED,
    # so this passed unchanged with record_local neutered (measured). Assert the
    # write occurred AND that the entry travelled whole, or the no-mutation
    # claim is untestable by construction.
    assert path is not None and path.is_file()
    assert json.loads(path.read_text(encoding="utf-8").strip())["entry"] == before


def test_both_wm_writers_are_wired_to_the_carrier():
    """The DAEMON copy is the live one — pin BOTH, by source.

    wm-append.sh is daemon-routed, so a carrier call added only to
    core/scripts/wm.py is inert at runtime (guard-742, the g-115-1992 class);
    and a call added only to the daemon leaves the CLI twin silently divergent.
    Neither is catchable by importing: mind_api's package-relative imports make
    a standalone load of wm_write.py fail, which is exactly why the existing
    shared-constants parity test AST-reads that file instead. Same technique
    here, for the wiring rather than the constants.

    This is a STRUCTURAL pin, and it is honest about what it cannot show: that
    a live daemon has RELOADED the new code. Measured 2026-08-16 — after this
    change landed, a real load-bearing append through wm-append.sh produced NO
    carrier on this box, because the running daemon still held its
    launch-time module. Source presence is necessary, not sufficient.
    """
    repo_root = SCRIPT_DIR.parent.parent   # SCRIPT_DIR is core/scripts
    for rel in ("core/scripts/wm.py",
                "mind_api/src/endpoints/wm_write.py"):
        path = repo_root / rel
        assert path.is_file(), (
            f"{rel} not found at {path} — this test's own path arithmetic is "
            "wrong; a missing file must not read as a missing wiring")
        src = path.read_text(encoding="utf-8")
        assert "body_capture_carrier" in src, f"{rel} is not wired to the carrier"
        assert "record_local" in src, f"{rel} never records a carrier entry"
        assert ".push(" in src, f"{rel} records but never pushes — local-only"
        # The gate must be BOTH capture-slot AND flagged: dropping either would
        # ship every append to the store (cost) or every flagged non-capture
        # entry (wrong lane).
        assert "CAPTURE_SLOTS" in src and 'get("load_bearing")' in src, (
            f"{rel} lost the capture-slot/load_bearing gate on the carrier write")


def test_push_sends_the_whole_file_so_a_failed_push_self_heals(tmp_path, monkeypatch):
    """Whole-file, not delta: append A (push fails), append B (push succeeds)
    -> the store must hold BOTH. That property is what removes the need for a
    retry queue, so it is pinned rather than left to the docstring."""
    root = _mk_root(tmp_path)
    wm = root / "agents" / AGENT / "sessions" / "sid-3" / "working-memory.yaml"
    wm.parent.mkdir(parents=True)
    wm.write_text("slots: {}\n", encoding="utf-8")

    be = DivergingBackend(fail_writes=True)
    import storage_backend
    monkeypatch.setattr(storage_backend, "get_backend", lambda: be)

    p1 = bcc.record_local(wm, "spark_capture", _entry("g-A"))
    assert bcc.push(p1) is False          # transport down; never raises
    assert be.files == {}

    be.fail_writes = False
    p2 = bcc.record_local(wm, "spark_capture", _entry("g-B"))
    assert bcc.push(p2) is True

    shipped = be.files["sid-3-fastlane.jsonl"].decode("utf-8")
    assert "g-A" in shipped and "g-B" in shipped, (
        "the recovered push must carry the entry whose own push failed")


def test_push_failure_is_reported_once_and_still_never_raises(tmp_path, monkeypatch, capsys):
    """A failed push must NAME its cause on stderr, exactly once per process.

    g-306-420: this except discarded the cause entirely, so a transport that
    could not work AT ALL looked identical to one with nothing to send —
    measured on cc-08, a worker Body's carrier held 101 undelivered rows behind
    a quiet False, because the carrier's destination was then inside the
    claim-protected agent tree and a worker never holds the runner claim. (The
    same change moved it to world/body-carriers/<agent>/, so that particular
    always-fails condition is gone; the reporting requirement is not, because a
    push can still fail for transport reasons and the caller sees only a bool.)

    ONCE, not per-call, is the half worth pinning: a systematically dark carrier
    fails on EVERY append, so a per-call report would be noise that the first
    reader filters out — which is how a loud failure becomes a dark one again.

    The module flag is process-global and pytest shares a process, so it is
    reset explicitly here. Without the reset this test passes or fails on
    whichever sibling happened to push first, which is a cross-test dependency
    that would be blamed on whatever failed next rather than on this file.
    """
    # raising=False so a source that lost the FLAG fails on the missing REPORT
    # below rather than here — the assertion should name the behaviour that
    # regressed, not a private attribute.
    monkeypatch.setattr(bcc, "_PUSH_FAILURE_REPORTED", False, raising=False)
    root = _mk_root(tmp_path)
    wm = root / "agents" / AGENT / "sessions" / "sid-rep" / "working-memory.yaml"
    wm.parent.mkdir(parents=True)
    wm.write_text("slots: {}\n", encoding="utf-8")

    be = DivergingBackend(fail_writes=True)
    import storage_backend
    monkeypatch.setattr(storage_backend, "get_backend", lambda: be)

    p = bcc.record_local(wm, "spark_capture", _entry("g-REP"))

    assert bcc.push(p) is False, "a failed push still returns False"
    first = capsys.readouterr().err
    assert "push FAILED" in first, (
        "a failed push must name itself on stderr; a bare False is the dark "
        "failure g-306-420 was filed about")
    assert "NOT reaching the reducer" in first, (
        "the report must say what the failure COSTS, not merely that it happened")

    assert bcc.push(p) is False, "still False on the second failure"
    second = capsys.readouterr().err
    assert "push FAILED" not in second, (
        "the report is once-per-process; a per-call report is noise on a "
        "non-reducer box where every push fails")

    # The never-raises contract is the reason this swallows at all: a raise here
    # would fail the WM append the transport exists to back.
    assert bcc.push(None) is False


# --------------------------------------------------------------------------
#  — the carrier must not inflate the flagged:total denominator
# --------------------------------------------------------------------------

def test_carrier_entries_are_excluded_from_the_ratio_denominator(tmp_path, monkeypatch):
    """The carrier ships ONLY flagged entries, so counting it in the
    denominator reports flagged/flagged = 100% for every remote Body — a
    maximally-degraded reading manufactured from a source that simply has no
    denominator to give.

    Its flagged entries still count in `flagged_seen`; the gap between that and
    `flagged_measurable` is reported as unmeasurable rather than folded in.
    """
    root = _mk_root(tmp_path)
    # Local Body: 1 flagged of 4 — the only measurable share.
    d = root / "agents" / AGENT / "sessions" / "local-body"
    d.mkdir(parents=True, exist_ok=True)
    (d / "working-memory.yaml").write_text(yaml.safe_dump({"slots": {"spark_capture": [
        _entry("g-1"),
        _entry("g-2", load_bearing=False),
        _entry("g-3", load_bearing=False),
        _entry("g-4", load_bearing=False),
    ]}}), encoding="utf-8")
    (d / "body-manifest.yaml").write_text(
        yaml.safe_dump({"body_state": "active", "unit_key": "local-body"}),
        encoding="utf-8")
    # Remote Body reachable only by carrier: 2 flagged, no denominator.
    be = DivergingBackend({
        "remote-fastlane.jsonl": _carrier_bytes(
            "remote", [("spark_capture", _entry("g-9")),
                       ("spark_capture", _entry("g-10"))]),
    })
    monkeypatch.setattr(bmg, "_get_backend", lambda: be)

    s = cfl.fast_lane(AGENT, project_root=root)

    # The share is the LOCAL one, undisturbed by the two carrier entries.
    assert s["by_slot_ratio"]["spark_capture"] == {"flagged": 1, "total": 4}, s
    assert s["entries_seen"] == 4, s
    assert s["flagged_measurable"] == 1, s
    # ...while the carrier's flagged entries are still SEEN.
    assert s["flagged_seen"] == 3, s
    line = cfl.format_line(s)
    assert "spark_capture 1/4=25%" in line, line
    assert "+2 carrier-sourced, share unmeasurable" in line, line
    # Negative control: with no carrier the caveat must be ABSENT, or the
    # assertion above would pass against any run that merged anything.
    monkeypatch.setattr(bmg, "_get_backend", lambda: DivergingBackend({}))
    root2 = _mk_root(tmp_path / "second")
    d2 = root2 / "agents" / AGENT / "sessions" / "local-body"
    d2.mkdir(parents=True, exist_ok=True)
    (d2 / "working-memory.yaml").write_text(yaml.safe_dump({"slots": {"spark_capture": [
        _entry("g-1"), _entry("g-2", load_bearing=False),
    ]}}), encoding="utf-8")
    (d2 / "body-manifest.yaml").write_text(
        yaml.safe_dump({"body_state": "active", "unit_key": "local-body"}),
        encoding="utf-8")
    s2 = cfl.fast_lane(AGENT, project_root=root2)
    assert s2["flagged_seen"] == s2["flagged_measurable"] == 1, s2
    assert "unmeasurable" not in cfl.format_line(s2), cfl.format_line(s2)


def test_carrier_only_lane_still_reports_that_the_share_is_unmeasurable(
        tmp_path, monkeypatch):
    """The CROSS-BOX case: every flagged entry arrived by carrier, so there is
    no measurable lane at all.

    This sits between the two tests that bracket it — the mixed case above
    (local measurable + carrier) and the empty-lane negative control in
    test_capture_fast_lane.py — and neither reaches it. Found by fresh-eyes
    review of g-306-365's own commit (4798cb2c): `_ratio_fragment` computed the
    unmeasurable remainder AFTER two early returns, both of which fire when
    `by_slot_ratio` is empty, so the reducer printed NO share information
    whatsoever in exactly the remote case this lane exists for — byte-identical
    to a lane nobody measured. That is the defect g-306-365 was filed to fix,
    reproduced inside the fix for it.
    """
    root = _mk_root(tmp_path)
    # A live local Body holding NO capture entries — the lane is genuinely
    # empty locally, which is what makes `by_slot_ratio` empty.
    d = root / "agents" / AGENT / "sessions" / "local-body"
    d.mkdir(parents=True, exist_ok=True)
    (d / "working-memory.yaml").write_text(
        yaml.safe_dump({"slots": {}}), encoding="utf-8")
    (d / "body-manifest.yaml").write_text(
        yaml.safe_dump({"body_state": "active", "unit_key": "local-body"}),
        encoding="utf-8")
    be = DivergingBackend({
        "remote-fastlane.jsonl": _carrier_bytes(
            "remote", [("spark_capture", _entry("g-20")),
                       ("spark_capture", _entry("g-21"))]),
    })
    monkeypatch.setattr(bmg, "_get_backend", lambda: be)

    s = cfl.fast_lane(AGENT, project_root=root)

    assert s["by_slot_ratio"] == {}, s          # nothing measurable
    assert s["flagged_measurable"] == 0, s
    assert s["flagged_seen"] == 2, s            # ...but the entries were seen
    line = cfl.format_line(s)
    assert "none measurable" in line, line
    assert "2 flagged carrier-sourced, no denominator" in line, line
    # The pre-fix behaviour was total silence about the share. Pin that it
    # cannot return: a line with no share clause at all is the regression.
    assert "load-bearing share" in line, line


# --------------------------------------------------------------------------
#  — the carrier lives under world/, NOT under the agent tree
# --------------------------------------------------------------------------

def test_carrier_is_not_under_the_agent_tree(tmp_path, _hermetic_world):
    """THE DEFECT PIN.

    Every other location assertion in this file would pass against the pre-fix
    source too, because they only ever check that producer and consumer AGREE —
    and they agreed perfectly while both pointed at a path no worker could write.
    This one does not agree-check: pre-fix, `carrier_dir` returned
    `<agent_dir>/session/pending-body-merges`, inside the own-cloud claim fence,
    so `push()` raised NoClaimError on every non-reducer box, forever. This
    assertion is the one that FAILS against the pre-fix source, which is what
    makes the rest of this file's location assertions more than a restatement of
    whatever the code currently does (rb-9217).
    """
    agent_dir = tmp_path / "agents" / AGENT
    cdir = bcc.carrier_dir(agent_dir)

    assert agent_dir not in cdir.parents, (
        f"carrier resolved INSIDE the claim-fenced agent tree: {cdir}")
    assert CARRIER_DIRNAME not in cdir.parts, (
        f"carrier still at the pre-g-306-420 location: {cdir}")
    assert cdir == _hermetic_world / WORLD_CARRIER_DIRNAME / AGENT


def test_producer_and_consumer_resolve_the_same_carrier_dir(tmp_path):
    """guard-3408, both ends: the producer emits a path the consumer must find.

    Asserted as a ROUND TRIP, not by comparing two path expressions. The
    consumer derives its directory inline from `state_dir`, so a path-equality
    check would only restate the code; a write-then-read proves DELIVERY, which
    is the property that was actually broken.
    """
    agent_dir = tmp_path / "agents" / AGENT
    unit = "unit-abc"
    wm_path = agent_dir / "sessions" / unit / "working-memory.yaml"

    written = bcc.record_local(wm_path, "spark_capture", {"observation": "x"})
    assert written is not None, "producer refused to write a carrier line"
    assert written == bcc.carrier_path(agent_dir, unit)

    got = bcc.read_carriers(agent_dir / "session", backend=None)
    assert got == {unit: {"spark_capture": [{"observation": "x"}]}}


def test_world_dir_is_injectable_so_a_carrier_never_escapes_to_the_live_world(
        tmp_path, _hermetic_world):
    """`world_dir` is a correctness fence, not a test convenience.

    Without it every caller falls through to the REAL world root, so a hermetic
    test building a tmp agent tree would still write its carrier into the LIVE
    `world/` — under own-cloud that is the guard-955 / rb-2983 production-key
    collision class, which has truncated a live store before.
    """
    agent_dir = tmp_path / "agents" / AGENT
    elsewhere = tmp_path / "another-world"
    unit = "unit-xyz"
    wm_path = agent_dir / "sessions" / unit / "working-memory.yaml"

    written = bcc.record_local(wm_path, "spark_capture", {"observation": "y"},
                               world_dir=elsewhere)
    assert written == (elsewhere / WORLD_CARRIER_DIRNAME / AGENT
                       / f"{unit}{bcc.CARRIER_SUFFIX}")
    assert written.is_file()
    # the INJECTED root is the only one touched — the ambient world stays clean
    assert not (_hermetic_world / WORLD_CARRIER_DIRNAME).exists()

    got = bcc.read_carriers(agent_dir / "session", backend=None,
                            world_dir=elsewhere)
    assert got == {unit: {"spark_capture": [{"observation": "y"}]}}


# --------------------------------------------------------------------------
# THE TRANSITION READ ( /  follow-up)
# --------------------------------------------------------------------------

def test_a_carrier_pushed_before_the_move_is_still_read(tmp_path, monkeypatch):
    """THE DEFECT PIN. Moving the carrier to `world/` must not strand carriers
    that were already pushed to the OLD location.

    Not hypothetical. Measured against the authoritative store from cc-09 on
    2026-09-03: `agents/alpha/session/pending-body-merges/` holds FOUR carriers
    (3,718,867 B, 1,632 flagged entries), newest store write
    2026-08-27T16:59:39Z, and ZERO staged `<unit>-wm.yaml` beside them — so the
    close-time `generalize_down` merge, the fallback the omission relied on, has
    nothing to merge for those Bodies. The carrier is their only surviving copy.

    Store leg, not the local one: a remote Body's carrier exists ONLY in the
    store, which is the case this whole module was built for.
    """
    root = _mk_root(tmp_path)
    be = DivergingBackend(legacy={
        "old-body-fastlane.jsonl": _carrier_bytes(
            "old-body", [("spark_capture", _entry("g-legacy"))]),
    })
    monkeypatch.setattr(bmg, "_get_backend", lambda: be)

    summary = cfl.fast_lane(AGENT, project_root=root)

    assert summary["carrier_merged"] == 1, summary
    assert [e["goal_id"] for e in _reducer_slots(root)["spark_capture"]] == ["g-legacy"]
    # REACHABILITY CONTROL — prove the legacy DIRECTORY was the one consulted,
    # so this cannot pass by the world leg happening to hold the same object.
    assert any(Path(p).name == CARRIER_DIRNAME for p in be.listed), (
        f"legacy carrier dir was never listed; backend saw {be.listed}")


def test_both_carrier_locations_union_for_one_body(tmp_path, monkeypatch):
    """A Body that pushed BEFORE the move and appended AFTER it has entries in
    both places, and BOTH must arrive.

    The consumer accumulates into `out[unit][slot]`, so a second directory that
    REPLACED rather than extended that list would silently drop whichever leg
    ran first — invisible in the single-directory tests, and the reason this
    asserts on one unit key present in both dirs rather than two separate ones.
    """
    root = _mk_root(tmp_path)
    unit = "straddling-body"
    be = DivergingBackend(
        files={f"{unit}-fastlane.jsonl": _carrier_bytes(
            unit, [("spark_capture", _entry("g-after"))])},
        legacy={f"{unit}-fastlane.jsonl": _carrier_bytes(
            unit, [("spark_capture", _entry("g-before"))])},
    )
    monkeypatch.setattr(bmg, "_get_backend", lambda: be)

    got = bcc.read_carriers(root / "agents" / AGENT / "session", backend=be)

    assert sorted(e["goal_id"] for e in got[unit]["spark_capture"]) == \
        ["g-after", "g-before"], got


def test_the_same_entry_in_both_locations_merges_once(tmp_path, monkeypatch):
    """The hazard the omission cited — "a real double-append when one Body has
    entries in both" — does not exist, and this is the control that says so.

    `body-merge._dedup_append` does `seen.add(h)` INSIDE its own loop, so the
    same entry arriving twice within ONE call is dropped on the second sighting;
    the g-306-311 `capture_consumed_hashes` watermark covers the across-call
    case. Pinned rather than argued, because the argument was the thing that
    turned out to be wrong.
    """
    root = _mk_root(tmp_path)
    unit = "duplicated-body"
    entry = _entry("g-dup")
    payload = _carrier_bytes(unit, [("spark_capture", entry)])
    be = DivergingBackend(files={f"{unit}-fastlane.jsonl": payload},
                          legacy={f"{unit}-fastlane.jsonl": payload})
    monkeypatch.setattr(bmg, "_get_backend", lambda: be)

    summary = cfl.fast_lane(AGENT, project_root=root)

    assert len(_reducer_slots(root)["spark_capture"]) == 1, _reducer_slots(root)
    assert summary["carrier_bodies"] == 1, summary


def test_nothing_is_ever_written_to_the_legacy_location(tmp_path):
    """The transition read is ONE-DIRECTIONAL, so the legacy directory drains
    and never refills.

    The producer must keep resolving to `world/`: a write here would be refused
    by the own-cloud claim fence on every worker box anyway (`NoClaimError`,
    which is what stranded those 3.7 MB in the first place), and would re-arm
    the exact defect g-306-420 removed.
    """
    agent_dir = tmp_path / "agents" / AGENT
    unit = "unit-legacy-write"
    wm_path = agent_dir / "sessions" / unit / "working-memory.yaml"

    written = bcc.record_local(wm_path, "spark_capture", {"observation": "z"})

    assert written is not None
    assert CARRIER_DIRNAME not in written.parts, written
    assert not (agent_dir / "session" / CARRIER_DIRNAME).exists()
