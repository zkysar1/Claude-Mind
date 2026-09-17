""" outcome 3 — the worker loop reads the capture fast lane BACK from
the store, so a wedge is caught by the unit that caused it.

WHAT FAILED. A worker Body's load-bearing captures are appended locally and then
pushed whole to `world/body-carriers/<agent>/<sid>-fastlane.jsonl`. On cc-07
(2026-09-12) that push failed for a full work unit while every `wm-append`
returned rc=0. The local file and the store copy were identical and both stale,
so no local reading could have shown it.

WHAT THESE PIN.
  - Presence is judged on the STORE copy, by the consumer's own identity
    (`body-merge._content_hash`, keyed per slot). Not by mtime, and not by bytes.
  - Only captures THIS Body flagged since its fork are expected. The Body WM
    starts as a copy of the agent-wide WM, and measured on cc-09, 1,245 of one
    Body's 1,395 flagged entries were inherited. A check without the baseline
    subtraction reports all of them missing on a healthy carrier.
  - An unrunnable check is "unverified", never "delivered" (guard-1760).
  - The CLI's exit codes, and the worker-loop wiring that runs it after the
    capture lanes and before the closure evidence.

The carrier rows below come from the REAL producer (`record_local`). A
hand-written row would pin this check against itself, not against what the
producer writes (guard-3221).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import body_capture_carrier as bcc  # noqa: E402
import storage_backend  # noqa: E402

REPO = SCRIPT_DIR.parent.parent
WORKER_LOOP = REPO / ".claude" / "skills" / "worker-loop" / "SKILL.md"
RATIONALE = REPO / "core" / "config" / "rationale" / "capture-carrier-delivery-check.md"
SID = "5ca1ab1e-0000-4000-8000-00000000c0de"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

class Store:
    """The store side only. `data=None` means the key is absent."""

    def __init__(self, data: bytes | None = None, exc: Exception | None = None):
        self.data = data
        self.exc = exc
        self.paths: list[Path] = []

    def read_authoritative_bytes(self, path):
        p = Path(path)
        assert p.is_absolute(), (
            f"read_authoritative_bytes got a relative path {p}; own-cloud raises "
            "on it, so the production .resolve() is load-bearing")
        self.paths.append(p)
        if self.exc is not None:
            raise self.exc
        if self.data is None:
            raise FileNotFoundError(p)
        return self.data


def _dump(path: Path, data: dict) -> None:
    """The daemon's own WM dump call (wm_write._write_wm), so the round trip the
    check depends on is the production one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


class Body:
    """A tmp worker Body: forked WM, fork baseline, and a world root for its carrier."""

    def __init__(self, root: Path, inherited: dict | None = None):
        self.world = root / "world"
        self.world.mkdir(parents=True)
        self.wm = root / "agents" / "alpha" / "sessions" / SID / "working-memory.yaml"
        self.slots = {k: list(v) for k, v in (inherited or {}).items()}
        _dump(self.wm, {"slots": self.slots})
        _dump(self.wm.parent / "forked-wm-baseline.yaml", {"slots": self.slots})

    @property
    def carrier(self) -> Path:
        return bcc.carrier_path(self.wm.parent.parent.parent, SID, self.world)

    def append(self, slot: str, entry: dict, *, record: bool = True) -> None:
        """What wm_write.append_slot does: WM append, then the carrier line for a
        flagged capture. `record=False` is a WM append whose carrier line never
        landed."""
        self.slots.setdefault(slot, []).append(entry)
        _dump(self.wm, {"slots": self.slots})
        if record and entry.get("load_bearing"):
            assert bcc.record_local(self.wm, slot, entry, world_dir=self.world)

    def local_store(self) -> Store:
        return Store(self.carrier.read_bytes() if self.carrier.is_file() else None)

    def verify(self, backend) -> tuple:
        return bcc.verify_delivery(self.wm, backend=backend, world_dir=self.world)


def _spark(n: int, **extra) -> dict:
    return {"goal_id": f"g-000-{n:02d}", "category": "test",
            "observation": f"observation number {n}", "sq_trigger": None, **extra}


# --------------------------------------------------------------------------
# verdicts
# --------------------------------------------------------------------------

def test_delivered_when_every_capture_flagged_since_the_fork_is_in_the_store(tmp_path):
    b = Body(tmp_path)
    b.append("spark_capture", _spark(1, load_bearing=True))
    b.append("encoding_capture", {"goal_id": "g-000-02", "fact": "x", "load_bearing": True})
    b.append("spark_capture", _spark(3))  # unflagged: never carried
    verdict, detail = b.verify(b.local_store())
    assert verdict == "delivered", detail
    assert "all 2 capture(s)" in detail


def test_unflagged_captures_are_not_expected_in_the_carrier(tmp_path):
    """guard-6181: the carrier holds flagged entries only, so a Body that
    flagged nothing has nothing to deliver. An empty carrier is correct here."""
    b = Body(tmp_path)
    b.append("spark_capture", _spark(1))
    b.append("exp_capture", {"goal_id": "g-000-02", "execution_summary": "s"})
    assert b.verify(Store(None))[0] == "n/a"


def test_inherited_flagged_entries_are_not_this_bodys_to_deliver(tmp_path):
    """The measured cc-09 shape: the WM was forked holding flagged entries that
    this Body never appended, so none of them are in its carrier."""
    inherited = {"encoding_capture": [
        {"goal_id": f"g-999-{i}", "fact": f"inherited {i}", "load_bearing": True}
        for i in range(3)]}
    b = Body(tmp_path, inherited=inherited)
    assert b.verify(Store(None))[0] == "n/a"      # nothing of its own yet
    b.append("spark_capture", _spark(1, load_bearing=True))
    verdict, detail = b.verify(b.local_store())
    assert verdict == "delivered", detail
    assert "all 1 capture(s)" in detail


def test_push_failure_the_local_carrier_holds_it_and_the_store_does_not(tmp_path):
    b = Body(tmp_path)
    b.append("spark_capture", _spark(1, load_bearing=True))
    pushed = b.carrier.read_bytes()                # the store saw the first push only
    b.append("spark_capture", _spark(2, load_bearing=True))
    verdict, detail = b.verify(Store(pushed))
    assert verdict == "undelivered", detail
    assert "1 of 2 capture(s)" in detail
    assert "spark_capture=1" in detail
    assert "the local carrier holds 1 of them" in detail


def test_the_cc07_shape_where_the_local_copy_is_as_stale_as_the_store(tmp_path):
    """Local and store identical, both missing the entry. A local reading,
    whether mtime, size or md5 against the store, would pass this."""
    b = Body(tmp_path)
    b.append("spark_capture", _spark(1, load_bearing=True))
    b.append("hyp_capture", {"goal_id": "g-000-02", "hypothesis_id": "h",
                             "load_bearing": True}, record=False)
    assert b.local_store().data == b.carrier.read_bytes()
    verdict, detail = b.verify(b.local_store())
    assert verdict == "undelivered", detail
    assert "hyp_capture=1" in detail
    assert "the local carrier holds 0 of them" in detail


def test_an_absent_store_key_is_undelivered_even_when_the_local_file_exists(tmp_path):
    """The  measurement: 151,390 local bytes, store key ABSENT. The
    consumer on another box has no local copy to fall back to."""
    b = Body(tmp_path)
    b.append("spark_capture", _spark(1, load_bearing=True))
    assert b.carrier.is_file()
    verdict, detail = b.verify(Store(None))
    assert verdict == "undelivered", detail
    assert "the store holds NO carrier" in detail


def test_a_store_read_error_is_unverified_never_delivered(tmp_path):
    b = Body(tmp_path)
    b.append("spark_capture", _spark(1, load_bearing=True))
    verdict, detail = b.verify(Store(exc=TimeoutError("transport hiccup")))
    assert verdict == "unverified", detail
    assert "TimeoutError" in detail


def test_a_missing_fork_baseline_is_unverified_not_a_guess(tmp_path):
    b = Body(tmp_path)
    b.append("spark_capture", _spark(1, load_bearing=True))
    (b.wm.parent / "forked-wm-baseline.yaml").unlink()
    verdict, detail = b.verify(b.local_store())
    assert verdict == "unverified", detail
    assert "baseline" in detail


def test_a_missing_body_wm_is_unverified(tmp_path):
    b = Body(tmp_path)
    b.wm.unlink()
    assert b.verify(Store(None))[0] == "unverified"


def test_the_agent_wide_wm_has_no_carrier(tmp_path):
    reducer_wm = tmp_path / "agents" / "alpha" / "session" / "working-memory.yaml"
    _dump(reducer_wm, {"slots": {}})
    verdict, _ = bcc.verify_delivery(reducer_wm, backend=Store(None), world_dir=tmp_path)
    assert verdict == "n/a"


def test_another_units_row_with_the_same_entry_does_not_count(tmp_path):
    b = Body(tmp_path)
    entry = _spark(1, load_bearing=True)
    b.append("spark_capture", entry, record=False)
    foreign = json.dumps({"unit_key": "some-other-sid", "slot": "spark_capture",
                          "entry": entry}, sort_keys=True) + "\n"
    assert b.verify(Store(foreign.encode()))[0] == "undelivered"


def test_the_same_content_under_another_slot_does_not_count(tmp_path):
    """The consumer merges per slot, so a row under the wrong slot delivers the
    entry to the wrong lane, which is not delivery."""
    b = Body(tmp_path)
    entry = _spark(1, load_bearing=True)
    b.append("spark_capture", entry, record=False)
    wrong = json.dumps({"unit_key": SID, "slot": "exp_capture", "entry": entry},
                       sort_keys=True) + "\n"
    assert b.verify(Store(wrong.encode()))[0] == "undelivered"


def test_the_wm_yaml_round_trip_keeps_the_consumer_hash(tmp_path):
    """The premise the whole check rests on: an entry read back out of the WM
    YAML hashes the same as the JSON row the producer wrote. Timestamp-shaped
    strings are the risk (a YAML loader can turn one into a datetime), so the
    entry carries several, plus unicode and nesting."""
    b = Body(tmp_path)
    b.append("exp_capture", {
        "goal_id": "g-000-01", "execution_summary": "ran — ✓ done",
        "started": "2026-09-14T15:10:25", "date": "2026-09-14", "at": "15:10",
        "surprise_level": 7, "verbatim_anchors": ["c9e00bb38e", "rc=0", "yes", "null"],
        "key_decisions": [{"why": "because", "n": 1.5}], "load_bearing": True})
    verdict, detail = b.verify(b.local_store())
    assert verdict == "delivered", detail


def test_malformed_rows_and_older_rows_do_not_break_a_delivered_verdict(tmp_path):
    b = Body(tmp_path)
    b.append("spark_capture", _spark(1, load_bearing=True))
    older = json.dumps({"unit_key": SID, "slot": "spark_capture",
                        "entry": _spark(0, load_bearing=True)}, sort_keys=True)
    data = b"not json at all\n" + older.encode() + b"\n\n" + b.carrier.read_bytes()
    verdict, detail = b.verify(Store(data))
    assert verdict == "delivered", detail


def test_the_real_local_backend_reads_the_store_as_the_file(tmp_path):
    """No fixture store at all: under STORAGE_BACKEND=local the file IS the store."""
    b = Body(tmp_path)
    b.append("spark_capture", _spark(1, load_bearing=True))
    verdict, detail = b.verify(storage_backend.LocalBackend())
    assert verdict == "delivered", detail


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cli(*args, env_over: dict | None = None, drop=("BODY_WM_PATH",)):
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["STORAGE_BACKEND"] = "local"                # guard-955
    env.update(env_over or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "worker_execute.py"), *args],
        capture_output=True, text=True, env=env, timeout=120)


def test_cli_exit_codes_follow_the_verdict(tmp_path):
    b = Body(tmp_path)
    b.append("spark_capture", _spark(1, load_bearing=True))
    world = {"MIND_WORLD": str(b.world)}

    r = _cli("check-capture-carrier", "--wm-path", str(b.wm), env_over=world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("delivered:")

    b.append("spark_capture", _spark(2, load_bearing=True), record=False)
    r = _cli("check-capture-carrier", "--wm-path", str(b.wm), env_over=world)
    assert r.returncode == 1, r.stdout + r.stderr
    assert r.stdout.startswith("undelivered:")
    assert "Do NOT hold the goal open" in r.stderr

    (b.wm.parent / "forked-wm-baseline.yaml").unlink()
    r = _cli("check-capture-carrier", "--wm-path", str(b.wm), env_over=world)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "NOT an all-clear" in r.stderr


def test_cli_reducer_wm_is_not_applicable(tmp_path):
    reducer_wm = tmp_path / "agents" / "alpha" / "session" / "working-memory.yaml"
    _dump(reducer_wm, {"slots": {}})
    r = _cli("check-capture-carrier", "--wm-path", str(reducer_wm),
             env_over={"MIND_WORLD": str(tmp_path)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("n/a:")


def test_cli_cannot_name_the_body_is_unverified_not_clean():
    r = _cli("check-capture-carrier", drop=("BODY_WM_PATH", "MIND_SID"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert r.stdout.startswith("unverified:")


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------

def test_worker_loop_runs_the_check_after_the_capture_lanes_and_before_closure():
    """The check must follow every capture append, or it reads a store the
    unit has not written to yet. It must precede the closure evidence, so the
    unit can name a wedge in its own narrative."""
    src = WORKER_LOOP.read_text(encoding="utf-8")
    check = src.find("worker_execute.py check-capture-carrier")
    last_capture = src.find("wm-append.sh encoding_capture")
    evidence = src.find("closure-evidence-write.sh")
    assert check != -1, "worker-loop no longer runs check-capture-carrier"
    assert last_capture != -1 and evidence != -1, "worker-loop anchors missing"
    assert last_capture < check < evidence
    assert "core/config/rationale/capture-carrier-delivery-check.md" in src
    assert RATIONALE.is_file()
