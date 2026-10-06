"""Consumer tests for the composite goal-queue layout ( outcome 4, design (b)).

THE QUESTION. Outcome 4 asks that every consumer of the goal queue be exercised against a SEGMENT FIXTURE, not
merely re-run, and that a goal held in a NON-newest segment be returned by the id lookup. Under design (b) the
local file stays one legacy file, so no consumer ever opens a segment. What a consumer can get wrong is
therefore (1) receiving the wrong BYTES (a head, a short join, a stale mirror) or (2) reading a REMOTE attribute
whose meaning the layout changed (the object's version and size, a directory listing). So the fixture is built
with the REAL writer (box A: `OwnCloudBackend.write_bytes`), read by a second box that has never seen the store
through the REAL reader (box B: its own mirror and its own sync manifest), and the named consumers then run on
box B's mirror: the daemon's id lookup and compact read, the zombie sweep of precheck-eval fed by that compact,
and the goal selector's own cache refresh followed by its candidate collection.

EACH VERDICT BELOW DEPENDS ON A NON-NEWEST SEGMENT, which is what makes it a control and not a re-run. asp-358 has
620 goals, so three segments (tokens 0, 1, 2 at span 250) and its pending goal g-358-300 sits in token 1, an OLD
segment; asp-7 has two segments and its ONLY unfinished goal, g-7-5, sits in token 0, the old one, so the zombie
sweep reads asp-7 as live until that goal is completed and as an all-terminal zombie after. A consumer that was
handed a head (empty goals), the newest segment only, or a stale mirror reads a different answer on every one
of these. The completion is written by box A through the real writer, so it also crosses the wire as a
steady-state composite write (one segment and the head), which the tests assert rather than assume.

WHAT IS PINNED ABOUT THE REMOTE ATTRIBUTES. `stat().version` is the head's ETag and moves whenever any segment's
content changes (the head carries the md5 of the joined bytes); `stat().plain_md5` is the md5 of the JOINED
bytes, so the sync layer's `_content_matches` holds for the file consumers read; `stat().size` is the HEAD
OBJECT's size, never the logical file's. A listing of the directory shows the segment directory, which the sync
layer's own exclusion names.

File basename starts with ``test_`` so domain-leak-check.sh skips it.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CORE_SCRIPTS = PROJECT_ROOT / "core" / "scripts"
sys.path.insert(0, str(CORE_SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _owncloud_composite as comp  # noqa: E402
import owncloud_sync as sync  # noqa: E402
import storage_backend  # noqa: E402
from _bash_helpers import BASH  # noqa: E402
from _daemon_fixture import DaemonFixture  # noqa: E402
from test_owncloud_composite_read_g358202 import (  # noqa: E402,F401  (the doubles are shared)
    BUCKET, ENV_ID, REL, _isolate, _legacy, _setup, s3,
)

# goal-selector.py needs MIND_AGENT to import (paths derive AGENT_DIR); capture-restore around it (guard-588).
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")
gs = importlib.import_module("goal-selector")
if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT

_spec = importlib.util.spec_from_file_location("precheck_eval_composite", CORE_SCRIPTS / "precheck-eval.py")
pe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pe)
ZOMBIE_CONFIG = {"intent_satisfaction": {"zombie_completion_ratio": 0.8, "phase_7_4_min_blocked_hours": 24}}

BASENAME = REL.rsplit("/", 1)[1]
OLD_PENDING = "g-358-300"  # token 1 of asp-358, whose newest token is 2
NEWEST_PENDING = "g-358-600"  # token 2, the newest segment of asp-358
LAST_OPEN = "g-7-5"  # the only unfinished goal of asp-7: token 0 of 2
SEGMENTS = ["asp-100/0", "asp-358/0", "asp-358/1", "asp-358/2", "asp-7/0", "asp-7/1"]


@pytest.fixture(autouse=True)
def _composite_on(monkeypatch, _isolate):
    """The writer flag names the test env, and the size floor drops to the fixture's size (the real floor has
    its own test)."""
    monkeypatch.setenv("OWNCLOUD_COMPOSITE_STORES", ENV_ID)
    monkeypatch.setattr(comp, "MIN_RAW_BYTES", 1)


# --- the fixture queue --------------------------------------------------------------------------------
def _goal(asp, n, overrides):
    g = {"id": "g-%s-%d" % (asp, n), "title": "goal %d of asp-%s" % (n, asp), "status": "completed",
         "priority": "MEDIUM", "category": "framework-patterns", "participants": ["agent"],
         "recurring": False, "description": "d" * 160}
    g.update(overrides.get(n, {}))
    return g


def _aspiration(asp, count, overrides):
    goals = sorted((_goal(asp, n, overrides) for n in range(1, count + 1)), key=lambda g: g["id"])
    return {"id": "asp-%s" % asp, "title": "aspiration %s" % asp, "status": "active", "priority": "MEDIUM",
            "goals": goals}


def _queue(finished=()):
    """Three aspirations, six segments. Every goal is completed except the three named open ones; `finished`
    completes some of those."""
    def opened(gid, **fields):
        return {"status": "completed"} if gid in finished else fields
    return [
        _aspiration("100", 40, {}),
        _aspiration("358", 620, {300: opened(OLD_PENDING, status="pending", priority="HIGH"),
                                 600: opened(NEWEST_PENDING, status="pending")}),
        _aspiration("7", 300, {5: opened(LAST_OPEN, status="pending")}),
    ]


# --- two boxes over one object store ------------------------------------------------------------------
class _Fleet:
    """Box A writes and box B reads. Each has its own mirror and its own sync manifest, because a manifest
    shared between two 'machines' would make B's stale mirror look like unpushed local writes and B would
    decline to refresh (the both-diverged verdict), which is a fixture artifact and not the layout."""

    def __init__(self, s3, root, monkeypatch):
        self.s3, self.root, self.mp = s3, root, monkeypatch
        self.a, self.pa, self.key = _setup(root / "a", s3)
        self.b, self.pb, _ = _setup(root / "b", s3)
        s3.put_object(Bucket=BUCKET, Key=self.key, Body=_legacy(_queue()[:1]))  # the pre-composite whole object
        self.on("a")
        self.a.read_bytes(self.pa, force_fresh=True)  # the fence the first flagged write needs

    def on(self, box):
        """Point the sync manifest at this box's own runtime dir (read lazily on every use)."""
        self.mp.setenv("RUNTIME_DIR", str(self.root / box / "_rt"))

    def write(self, records):
        raw = _legacy(records)
        self.on("a")
        self.a.write_bytes(self.pa, raw)
        return raw

    def refresh_b(self):
        self.on("b")
        return self.b.read_bytes(self.pb, force_fresh=True)

    def stored(self):
        return self.s3.get_object(Bucket=BUCKET, Key=self.key)["Body"].read()


@pytest.fixture
def fleet(s3, tmp_path, monkeypatch):
    return _Fleet(s3, tmp_path, monkeypatch)


def _assert_segmented(fl):
    """The fixture stayed composite: a head over six segments, three of them asp-358's. Without this a fixture
    that fell back to a whole-object PUT would make every consumer test below pass vacuously (guard-7238)."""
    stored = fl.stored()
    assert comp.is_head(stored)
    assert sorted(n.split(".")[0] for n in comp.head_object_names(stored)) == SEGMENTS


# --- 1. the remote attributes consumers read ----------------------------------------------------------
def test_stat_names_the_head_and_moves_when_a_non_newest_segment_changes(fleet):
    raw = fleet.write(_queue())
    _assert_segmented(fleet)
    st = fleet.b.stat(fleet.pb)
    assert st.plain_md5 == hashlib.md5(raw).hexdigest()  # the JOINED bytes
    assert sync._content_matches(st, hashlib.md5(raw).hexdigest())  # what the sync layer asks of the mirror
    assert not sync._content_matches(st, hashlib.md5(raw + b" ").hexdigest())  # and it can say no
    assert st.size == len(fleet.stored()) >= comp.HEAD_MIN_BYTES and st.size != len(raw)  # the head's size

    mark = len(fleet.s3.puts)
    new = fleet.write(_queue(finished=(LAST_OPEN,)))  # g-7-5 sits in token 0 of asp-7, an OLD segment
    st2 = fleet.b.stat(fleet.pb)
    assert st2.version != st.version and st2.plain_md5 == hashlib.md5(new).hexdigest()
    puts = fleet.s3.puts[mark:]
    assert len(puts) == 2 and "/%s/" % comp.SEGMENT_DIR in puts[0]["Key"] and puts[1]["Key"] == fleet.key


def test_listings_show_the_segment_directory_and_the_sync_layer_excludes_it(fleet):
    fleet.write(_queue())
    _assert_segmented(fleet)
    assert fleet.b.list_dir(fleet.pb.parent) == sorted([comp.SEGMENT_DIR, BASENAME])
    rows = fleet.b.list_objects(fleet.pb.parent)
    assert len([r for r in rows if r[0].startswith(comp.SEGMENT_DIR + "/")]) == len(SEGMENTS)
    assert [r for r in rows if r[0] == BASENAME]
    assert sync._is_excluded_dir(comp.SEGMENT_DIR)  # the sweep that walks these rows prunes the directory


def test_a_lan_cache_hit_for_the_head_is_still_joined(fleet, monkeypatch):
    """The LAN object cache (the daemon's cache endpoint) serves the DECODED plaintext of the key it is asked
    for, which for a composite store is the HEAD; `_refresh` takes those bytes and joins them afterwards."""
    fleet.write(_queue())
    _assert_segmented(fleet)
    head, calls = fleet.stored(), []

    def served(key, etag):
        calls.append(key)
        return head

    monkeypatch.setattr(fleet.b, "_cache_fetch", served)
    assert fleet.refresh_b() == _legacy(_queue())
    assert calls == [fleet.key]  # the cache path ran, so the join did not come from a plain GET


# --- 2. the named consumers, on box B's mirror --------------------------------------------------------
def _get(port, path):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), headers={"X-Mind-Agent": "alpha"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status, resp.read().decode("utf-8")


@contextlib.contextmanager
def _daemon(fl):
    """An in-process daemon over box B's world directory (STORAGE_BACKEND=local inside it: the daemon reads the
    mirror, which is exactly what it reads on an own-cloud box after `_refresh`)."""
    with DaemonFixture(fl.pb.parent) as df:
        yield df


def _wrapper(df, *argv):
    """The real aspirations-query.sh, pointed at the fixture daemon through its runtime dir."""
    env = dict(os.environ, RT_DIR=str(df.runtime_dir), MIND_AGENT="alpha", STORAGE_BACKEND="local")
    r = subprocess.run([BASH, (CORE_SCRIPTS / "aspirations-query.sh").as_posix(), *argv], capture_output=True,
                       text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr[:300]
    return json.loads(r.stdout)


def _lookup(port, goal_id, full=False):
    q = "goal_field_name=id&goal_field_value=%s%s" % (goal_id, "&full=true" if full else "")
    status, body = _get(port, "/v1/aspirations/query?" + q)
    assert status == 200, body[:200]
    return json.loads(body)


def _zombies(fl):
    with _daemon(fl) as df:
        status, body = _get(df.port, "/v1/aspirations/read?source=world&active_compact=1")
    assert status == 200, body[:200]
    data = json.loads(body)
    out = pe.cmd_zombies(type("Args", (), {})(), ZOMBIE_CONFIG, data if isinstance(data, dict) else {"aspirations": data})
    return sorted(z["aspiration_id"] for z in out["zombies"])


def _candidates(fl):
    """The selector's own cache refresh, then its own read and candidate collection."""
    fl.mp.setattr(storage_backend, "get_backend", lambda: fl.b)
    fl.on("b")
    assert gs.refresh_aspiration_caches([fl.pb]) is True
    asps = gs.read_jsonl(fl.pb)
    goals = [g for a in asps for g in a["goals"]]
    cands = gs.collect_candidates(asps, source="world", global_done_ids={g["id"] for g in goals if g["status"] == "completed"},
                                  global_live_ids={g["id"] for g in goals}, claim_timeout_hours=4.0)
    return {c["goal"]["id"] for c in cands}


def test_the_id_lookup_returns_a_goal_held_in_every_segment(fleet):
    fleet.write(_queue())
    _assert_segmented(fleet)
    assert fleet.refresh_b() == _legacy(_queue())  # box B joined what box A wrote
    with _daemon(fleet) as df:
        port = df.port
        for gid, asp, status in ((OLD_PENDING, "asp-358", "pending"), (NEWEST_PENDING, "asp-358", "pending"),
                                 (LAST_OPEN, "asp-7", "pending"), ("g-358-1", "asp-358", "completed"),
                                 ("g-100-40", "asp-100", "completed")):
            assert [(r["goal_id"], r["asp_id"], r["status"]) for r in _lookup(port, gid)] == [(gid, asp, status)]
        assert _lookup(port, "g-358-9999") == []  # a goal the store lacks is not invented
        full = _lookup(port, OLD_PENDING, full=True)[0]
        assert full["priority"] == "HIGH" and full["description"] == "d" * 160  # the whole record, from the old segment
        # the literal outcome-4 clause: the real wrapper, pointed at this daemon, returns the old-segment goal
        rows = _wrapper(df, "--goal-field", "id", OLD_PENDING)
        assert [(r["goal_id"], r["asp_id"], r["status"]) for r in rows] == [(OLD_PENDING, "asp-358", "pending")]
        assert _wrapper(df, "--goal-field", "id", "g-358-9999") == []


def test_a_completion_in_an_old_segment_reaches_the_selector_and_the_zombie_sweep(fleet):
    fleet.write(_queue())
    _assert_segmented(fleet)
    fleet.refresh_b()
    before = _candidates(fleet)
    assert {OLD_PENDING, NEWEST_PENDING, LAST_OPEN} <= before
    assert not any(g.startswith("g-100-") for g in before)  # every goal of asp-100 is completed
    assert _zombies(fleet) == ["asp-100"]  # asp-7 reads live: its one open goal is in an old segment

    fleet.write(_queue(finished=(LAST_OPEN,)))  # box A completes g-7-5 (one segment and the head)
    after = _candidates(fleet)  # box B is NOT refreshed by hand: the selector's own refresh pulls it
    assert LAST_OPEN not in after and {OLD_PENDING, NEWEST_PENDING} <= after
    assert _zombies(fleet) == ["asp-100", "asp-7"]  # now asp-7 is all-terminal
