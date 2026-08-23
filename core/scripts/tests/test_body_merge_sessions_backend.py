""" — the sessions-pass must drain a CROSS-BOX worker Body.

Sibling of test_body_merge_staged_backend.py, which pins the ORPHAN path
(session/pending-body-merges/). This file pins the NORMAL ship path
(sessions/<unitKey>/), whose enumerate + read + state-stamp were all
local-only: a reducer receiving a Body that shipped from another box holds
NO local sessions/ dir, so `generalize_down` returned at its
`sessions_root.is_dir()` gate and merged nothing, forever, silently.

The store/local divergence is injected through the `merge._get_backend`
seam — the same seam and the same reason as the staged sibling: it is the
exact state own-cloud read-through caching produces, and it cannot be
reproduced with real files.

WHICH TESTS ARE DEFECT PINS, MEASURED — not asserted. Reverting the gate to
its pre-fix `if not sessions_root.is_dir():` form and re-running gives
`FFF...`: the first THREE fail (they are the pins) and the last three pass.
That is correct and worth stating rather than rounding up to "all six pin the
fix": `test_local_only_body_still_drains_when_store_is_empty` and
`test_active_body_in_the_store_is_not_merged` are REGRESSION guards — they
must pass both before and after, because their job is to prove the union did
not REPLACE the local glob and that liveness filtering still holds.
A test that passes against the broken code is not a weak pin; it is a
different instrument, and mislabelling it hides which of the two it is
(guard-1943).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import wm  # noqa: E402  — for CAPTURE_SLOTS; see the parameterized lane test below


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


merge = _load("body_merge_sessions_tests", "body-merge.py")

# UUID-shaped: body-manifest's _valid_sid_shape rejects anything else, so a
# prettier key here would fail inside set_state rather than in the code under test.
UNIT = "44e0aaaa-1111-4222-8333-444455556666"
AGENT = "alpha"


def _y(d: dict) -> bytes:
    return yaml.dump(d, default_flow_style=False, sort_keys=False).encode("utf-8")


class SessionsStore:
    """Authoritative store keyed by '<unitKey>/<filename>'.

    Deliberately NOT basename-keyed like the staged sibling's FakeStore: the
    sessions layout nests one manifest per unit dir, so basenames collide
    across units and a basename key would make a two-Body test silently read
    one Body's bytes for the other.
    """

    def __init__(self, files: dict[str, bytes] | None = None, fail_reads: bool = False):
        self.files = dict(files or {})
        self.fail_reads = fail_reads
        self.listed: list[str] = []

    @staticmethod
    def _key(path) -> str:
        p = Path(path)
        assert p.is_absolute(), (
            f"backend called with non-absolute path {p} — the production "
            ".resolve() is load-bearing (a relative path makes _s3_key raise "
            "and the listing degrades silently)")
        return f"{p.parent.name}/{p.name}"

    def list_dir(self, path):
        p = Path(path)
        assert p.is_absolute(), f"list_dir called with relative path {p}"
        self.listed.append(str(p))
        return sorted({k.split("/", 1)[0] for k in self.files})

    def read_authoritative_bytes(self, path) -> bytes:
        if self.fail_reads:
            raise RuntimeError("simulated transport error")
        key = self._key(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]


def _mk_reducer(tmp_path: Path, reducer_wm: dict) -> Path:
    state = tmp_path / "agents" / AGENT / "session"
    state.mkdir(parents=True, exist_ok=True)
    (state / "working-memory.yaml").write_bytes(_y(reducer_wm))
    return tmp_path


def _reducer_wm(pr: Path) -> dict:
    return yaml.safe_load(
        (pr / "agents" / AGENT / "session" / "working-memory.yaml").read_text(
            encoding="utf-8")) or {}


def _store_body(unit: str = UNIT, spark=None, state: str = "closed-pending-merge",
                slot: str = "spark_capture") -> dict:
    """A shipped Body present ONLY in the store — no local sessions/ dir at all.

    `slot` defaults to spark_capture so every test written before 2026-08-22 keeps
    its exact payload; only the parameterized lane test below varies it.
    """
    manifest = {
        "unitKey": unit, "mindKey": AGENT, "role": "worker",
        "body_state": state, "remote_body": True, "machine_id": "cc-99",
    }
    body_wm = {"slots": {slot: spark if spark is not None else [
        {"goal_id": "g-1", "observation": "cross-box learning"}]}}
    return {f"{unit}/body-manifest.yaml": _y(manifest),
            f"{unit}/working-memory.yaml": _y(body_wm)}


# ── THE DEFECT PIN ──────────────────────────────────────────────────────────

def test_cross_box_body_merges_with_no_local_sessions_dir(tmp_path, monkeypatch):
    """The live-specimen shape: both sessions files exist ONLY in the store and
    the local sessions/ dir does not exist. Pre-fix, generalize_down returned at
    its local is_dir() gate and merged nothing — spark_capture never reached the
    reducer. This is acceptance outcome 4 end-to-end."""
    pr = _mk_reducer(tmp_path, {"slots": {"spark_capture": []}})
    store = SessionsStore(_store_body())
    monkeypatch.setattr(merge, "_get_backend", lambda: store)

    assert not (pr / "agents" / AGENT / "sessions").is_dir(), "precondition: no local dir"

    summary = merge.generalize_down(AGENT, pr)

    assert summary["merged"] == [UNIT], summary
    sparks = _reducer_wm(pr)["slots"]["spark_capture"]
    assert any(s.get("observation") == "cross-box learning" for s in sparks), sparks


def test_gate_consults_the_store_not_only_the_local_dir(tmp_path, monkeypatch):
    """Pins the gate specifically: the sessions_root listing must be requested
    even though the local dir is absent. Without the _store_has_unit_dirs half,
    backend-routing _enumerate_pending is unreachable and this list is empty."""
    pr = _mk_reducer(tmp_path, {"slots": {}})
    store = SessionsStore(_store_body())
    monkeypatch.setattr(merge, "_get_backend", lambda: store)

    merge.generalize_down(AGENT, pr)

    assert any(p.endswith("sessions") for p in store.listed), store.listed


def test_state_stamp_survives_a_store_only_manifest(tmp_path, monkeypatch):
    """set_state reads the LOCAL manifest, so a store-only Body raised
    FileNotFoundError and aborted the whole merge on exactly the case this fix
    serves. The merge must complete AND the local mirror must record merged."""
    pr = _mk_reducer(tmp_path, {"slots": {"spark_capture": []}})
    store = SessionsStore(_store_body())
    monkeypatch.setattr(merge, "_get_backend", lambda: store)

    summary = merge.generalize_down(AGENT, pr)  # must not raise

    assert summary["merged"] == [UNIT], summary
    local = pr / "agents" / AGENT / "sessions" / UNIT / "body-manifest.yaml"
    assert local.is_file(), "merged Body's manifest was not materialized locally"
    assert (yaml.safe_load(local.read_text(encoding="utf-8")) or {}
            )["body_state"] == "merged"


def test_transient_store_error_leaves_the_body_pending(tmp_path, monkeypatch):
    """A transport hiccup must NOT consume the Body: marking it merged on bytes
    we never saw destroys the worker's divergence irrecoverably. It must be
    deferred, and must NOT be marked merged."""
    pr = _mk_reducer(tmp_path, {"slots": {"spark_capture": []}})
    store = SessionsStore(_store_body(), fail_reads=True)
    monkeypatch.setattr(merge, "_get_backend", lambda: store)

    summary = merge.generalize_down(AGENT, pr)

    assert summary["merged"] == [], summary
    assert UNIT not in summary["skipped"], summary
    local = pr / "agents" / AGENT / "sessions" / UNIT / "body-manifest.yaml"
    assert not local.is_file(), "a deferred Body must not be stamped merged"


def test_local_only_body_still_drains_when_store_is_empty(tmp_path, monkeypatch):
    """Regression guard: the union must never REPLACE the local glob. A Body
    forked on THIS box with nothing in the store has to merge exactly as before."""
    pr = _mk_reducer(tmp_path, {"slots": {"spark_capture": []}})
    sdir = pr / "agents" / AGENT / "sessions" / UNIT
    sdir.mkdir(parents=True)
    (sdir / "body-manifest.yaml").write_bytes(_y({
        "unitKey": UNIT, "mindKey": AGENT, "role": "worker",
        "body_state": "closed-pending-merge",
    }))
    (sdir / "working-memory.yaml").write_bytes(
        _y({"slots": {"spark_capture": [{"goal_id": "g-2", "observation": "local"}]}}))
    monkeypatch.setattr(merge, "_get_backend", lambda: SessionsStore({}))

    summary = merge.generalize_down(AGENT, pr)

    assert summary["merged"] == [UNIT], summary
    sparks = _reducer_wm(pr)["slots"]["spark_capture"]
    assert any(s.get("observation") == "local" for s in sparks), sparks


def test_active_body_in_the_store_is_not_merged(tmp_path, monkeypatch):
    """Only closed-pending-merge Bodies drain. A still-ACTIVE cross-box Body
    must be enumerated and then left alone — merging a live Body's WM would
    take a snapshot it is still diverging from."""
    pr = _mk_reducer(tmp_path, {"slots": {"spark_capture": []}})
    store = SessionsStore(_store_body(state="active"))
    monkeypatch.setattr(merge, "_get_backend", lambda: store)

    summary = merge.generalize_down(AGENT, pr)

    assert summary["merged"] == [], summary
    assert summary["scanned"] == 0, summary


# ── EVERY LANE, NOT JUST THE REPRESENTATIVE ONE () ─────────────────

@pytest.mark.parametrize("slot", sorted(wm.CAPTURE_SLOTS))
def test_every_capture_lane_survives_the_cross_box_path(tmp_path, monkeypatch, slot):
    """All six tests above seed `spark_capture` and only spark_capture, so until
    2026-08-22 the cross-box path was proven for ONE of the four capture lanes.

    That is the g-115-6054 lesson — a representative member is not coverage of a
    family — and it is the THIRD time it has surfaced inside g-306-204 alone.
    The residual risk was small but not zero: the lanes share `merge_wm`'s slot
    loop (pinned in test_capture_lane_chain.py), but the array_limits
    enforcement that runs on the MERGED slots is keyed PER SLOT, so a
    lane-specific failure on this path was possible and nothing would have
    caught it.

    Parameterized over `wm.CAPTURE_SLOTS` rather than a written-out list of four,
    so a fifth lane is covered by being REGISTERED — the same discipline
    test_capture_lane_chain.py and test_wm_reset_cadence.py already apply.

    Deliberately additive: the six tests above are left byte-for-byte alone.
    Their docstrings record which are DEFECT PINS and which are REGRESSION
    GUARDS (measured, not asserted — see the module docstring), and rewriting
    them to loop would blur a distinction guard-1943 exists to keep sharp.
    """
    pr = _mk_reducer(tmp_path, {"slots": {slot: []}})
    store = SessionsStore(_store_body(slot=slot))
    monkeypatch.setattr(merge, "_get_backend", lambda: store)

    assert not (pr / "agents" / AGENT / "sessions").is_dir(), "precondition: no local dir"

    summary = merge.generalize_down(AGENT, pr)

    assert summary["merged"] == [UNIT], (
        f"{slot}: a cross-box Body carrying this lane did not merge at all: {summary}"
    )
    landed = (_reducer_wm(pr)["slots"] or {}).get(slot) or []
    assert any(e.get("observation") == "cross-box learning" for e in landed), (
        f"{slot}: the Body merged but this lane's payload did not reach the "
        f"reducer (lane holds {landed!r}) — a worker's learning in this lane "
        f"would be silently lost on every cross-box ship"
    )
