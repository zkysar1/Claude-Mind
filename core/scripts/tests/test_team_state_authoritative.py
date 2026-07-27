"""test_team_state_authoritative.py — .

Unit tests for `_team_state.load_rows_authoritative`: the surgical per-consumer
authoritative shard read that fixes partner_in_flight blindness on the own-cloud
backend. The local shard mirror is conflict-skipped/frozen for PEER shards
(guard-980 / g-115-2163), so the double-claim guard must read peer in_flight
FRESH from S3 — but ONLY that opt-in consumer, never the hot load_rows path.

Cases:
  1. local backend            -> delegates to load_rows EXACTLY (the no-S3
     guarantee that keeps every existing gate test unchanged).
  2. own-cloud (fake backend) -> each shard read FRESH via read_text(force_fresh),
     UNIONS the S3 roster (discovers peers ABSENT from the local mirror), prefers
     the fresh content over the frozen local row.
  3. own-cloud, one shard errors -> per-shard fail-open keeps that agent's LOCAL
     row; other shards still refresh.
  4. own-cloud, backend init raises -> whole-call fail-open to load_rows.

No real S3 — a FakeBackend stands in for OwnCloudBackend.from_env. Hermetic;
mutated globals (STORAGE_BACKEND, OwnCloudBackend.from_env) restored per case.

Run: py -3 core/scripts/tests/test_team_state_authoritative.py
Looks for "PASS (4/4 cases)". Also pytest-collectable via test_load_rows_authoritative().
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

import _team_state as ts  # noqa: E402


def _seed_local_rows(world: Path, rows: dict):
    d = world / "team-state" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    for agent, doc in rows.items():
        with open(d / f"{agent}.yaml", "w", encoding="utf-8") as f:
            yaml.dump(doc, f, sort_keys=False)


class _FakeBackend:
    """Stand-in for OwnCloudBackend: an S3 roster + fresh shard content by agent.
    `errors` names agents whose read_text raises (per-shard fail-open probe)."""

    def __init__(self, s3_rows: dict, errors=None):
        self._s3_rows = s3_rows
        self._errors = set(errors or ())

    def list_dir(self, path):  # noqa: ARG002 — path unused; roster is the fixture
        return [f"{a}.yaml" for a in self._s3_rows]

    def read_text(self, path, *, force_fresh=False):  # noqa: ARG002
        agent = Path(path).stem
        if agent in self._errors:
            raise RuntimeError(f"simulated S3 read error for {agent}")
        if agent not in self._s3_rows:
            raise FileNotFoundError(path)
        return yaml.dump(self._s3_rows[agent], sort_keys=False)


def _in_flight_id(row):
    if not isinstance(row, dict):
        return None
    inf = row.get("in_flight")
    return inf.get("goal_id") if isinstance(inf, dict) else None


def _install_backend(fake_or_boom):
    """Point STORAGE_BACKEND at own-cloud and OwnCloudBackend.from_env at the
    fixture (a _FakeBackend, or a callable that raises)."""
    os.environ["STORAGE_BACKEND"] = "own-cloud"
    import owncloud_backend
    if callable(fake_or_boom) and not isinstance(fake_or_boom, _FakeBackend):
        owncloud_backend.OwnCloudBackend.from_env = classmethod(
            lambda cls: fake_or_boom())
    else:
        owncloud_backend.OwnCloudBackend.from_env = classmethod(
            lambda cls: fake_or_boom)


# ── cases ────────────────────────────────────────────────────────────────────

def _case_local_delegates(tmp, fail):
    os.environ["STORAGE_BACKEND"] = "local"
    _seed_local_rows(tmp, {
        "alpha": {"last_active": "2026-07-14T10:00:00", "in_flight": None},
        "bravo": {"last_active": "2026-07-10T10:00:00",
                  "in_flight": {"goal_id": "g-x", "title": "t"}},
    })
    got = ts.load_rows_authoritative(tmp)
    want = ts.load_rows(tmp)
    if got != want:
        fail(f"local backend must delegate to load_rows: {got} != {want}")
    if _in_flight_id(got.get("bravo")) != "g-x":
        fail(f"local bravo in_flight lost: {got.get('bravo')}")


def _case_owncloud_fresh_and_roster(tmp, fail):
    # Local mirror: bravo STALE (in_flight None); zeta ABSENT locally.
    _seed_local_rows(tmp, {"bravo": {"last_active": "2026-07-07T00:00:00",
                                     "in_flight": None}})
    s3 = {
        "bravo": {"last_active": "2026-07-14T21:00:00",
                  "in_flight": {"goal_id": "g-fresh", "title": "fresh work"}},
        "zeta": {"last_active": "2026-07-14T21:05:00",
                 "in_flight": {"goal_id": "g-zeta", "title": "zeta work"}},
    }
    _install_backend(_FakeBackend(s3))
    got = ts.load_rows_authoritative(tmp)
    if _in_flight_id(got.get("bravo")) != "g-fresh":
        fail(f"bravo must refresh to FRESH in_flight, not frozen local None: "
             f"{got.get('bravo')}")
    if "zeta" not in got:
        fail(f"zeta (S3-only, absent locally) must appear via roster union: "
             f"{sorted(got)}")
    elif _in_flight_id(got.get("zeta")) != "g-zeta":
        fail(f"zeta fresh in_flight lost: {got.get('zeta')}")


def _case_owncloud_per_shard_failopen(tmp, fail):
    _seed_local_rows(tmp, {
        "bravo": {"last_active": "2026-07-07T00:00:00",
                  "in_flight": {"goal_id": "g-local-bravo"}},
        "foxtrot": {"last_active": "2026-07-07T00:00:00", "in_flight": None},
    })
    s3 = {
        "bravo": {"last_active": "2026-07-14T21:00:00",
                  "in_flight": {"goal_id": "g-fresh-bravo"}},
        "foxtrot": {"last_active": "2026-07-14T21:00:00",
                    "in_flight": {"goal_id": "g-fresh-fox"}},
    }
    _install_backend(_FakeBackend(s3, errors={"bravo"}))  # bravo's S3 read raises
    got = ts.load_rows_authoritative(tmp)
    if _in_flight_id(got.get("bravo")) != "g-local-bravo":
        fail(f"bravo S3-error must keep LOCAL row (per-shard fail-open): "
             f"{got.get('bravo')}")
    if _in_flight_id(got.get("foxtrot")) != "g-fresh-fox":
        fail(f"foxtrot must still refresh when a sibling shard errors: "
             f"{got.get('foxtrot')}")


def _case_owncloud_backend_init_failopen(tmp, fail):
    _seed_local_rows(tmp, {"bravo": {"last_active": "2026-07-07T00:00:00",
                                     "in_flight": {"goal_id": "g-local-only"}}})

    def _boom():
        raise RuntimeError("no creds")

    _install_backend(_boom)
    got = ts.load_rows_authoritative(tmp)
    want = ts.load_rows(tmp)
    if got != want:
        fail(f"backend-init failure must fail-open to load_rows: {got} != {want}")
    if _in_flight_id(got.get("bravo")) != "g-local-only":
        fail(f"local bravo in_flight lost on backend-init failure: {got.get('bravo')}")


_CASES = [
    ("local_delegates", _case_local_delegates),
    ("owncloud_fresh_and_roster", _case_owncloud_fresh_and_roster),
    ("owncloud_per_shard_failopen", _case_owncloud_per_shard_failopen),
    ("owncloud_backend_init_failopen", _case_owncloud_backend_init_failopen),
]


def main() -> int:
    failures = []
    import owncloud_backend
    for name, fn in _CASES:
        saved_backend = os.environ.get("STORAGE_BACKEND")
        saved_from_env = owncloud_backend.OwnCloudBackend.from_env
        tmp = Path(tempfile.mkdtemp(prefix="tsa-test-"))

        def _fail(msg, _n=name):
            failures.append(f"{_n}: {msg}")

        try:
            fn(tmp, _fail)
        except Exception as e:  # noqa: BLE001 — a raised case is a failure
            failures.append(f"{name}: unexpected {type(e).__name__}: {e}")
        finally:
            if saved_backend is None:
                os.environ.pop("STORAGE_BACKEND", None)
            else:
                os.environ["STORAGE_BACKEND"] = saved_backend
            owncloud_backend.OwnCloudBackend.from_env = saved_from_env
            shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"FAIL ({len(failures)} problems)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"PASS ({len(_CASES)}/{len(_CASES)} cases)")
    return 0


def test_load_rows_authoritative():
    """Pytest entry point — runs the 4-case suite (tmp-world isolated)."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
