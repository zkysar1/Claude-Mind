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
CARRIER_DIRNAME = "pending-body-merges"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

class DivergingBackend:
    """A store that does NOT mirror the local filesystem.

    Keyed by BASENAME because carriers live flat in one directory (unlike the
    sessions/ layout, where basenames collide across unit dirs and the sibling
    fixture must key by parent/name).

    `write_bytes` records into the same dict, so a producer test can assert the
    push actually reached the store rather than only the local file.
    """

    def __init__(self, files: dict[str, bytes] | None = None,
                 fail_reads: bool = False, fail_writes: bool = False):
        self.files = dict(files or {})
        self.fail_reads = fail_reads
        self.fail_writes = fail_writes
        self.listed: list[str] = []
        self.written: list[str] = []

    def list_dir(self, path):
        p = Path(path)
        assert p.is_absolute(), (
            f"list_dir called with relative path {p} — the production "
            ".resolve() is load-bearing (a relative path makes _s3_key raise "
            "and the listing degrades silently)")
        self.listed.append(str(p))
        return sorted(self.files)

    def read_authoritative_bytes(self, path) -> bytes:
        if self.fail_reads:
            raise RuntimeError("simulated transport error")
        name = Path(path).name
        if name not in self.files:
            raise FileNotFoundError(name)
        return self.files[name]

    def write_bytes(self, path, data):
        if self.fail_writes:
            raise RuntimeError("simulated write failure")
        self.written.append(Path(path).name)
        self.files[Path(path).name] = data


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
    assert any(CARRIER_DIRNAME in p for p in be.listed), (
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
    assert path.parent.name == CARRIER_DIRNAME
    # session/ (singular) — the syncable dir. Under sessions/ it would be
    # machine-local and the whole carrier would be as invisible as the WM.
    assert path.parent.parent.name == "session"
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
    assert not (root / "agents" / AGENT / "session" / CARRIER_DIRNAME).exists()

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
