"""POST /v1/aspirations/update-goal-fields ().

The recurring-close counter block wrote its fields as up to eight separate
`aspirations.py update-goal` calls; on a whole-object-PUT store every call is a
full rewrite of aspirations.jsonl. The route lands them in ONE locked rewrite
and reads each field back. What is pinned here:

  * ONE write -- a single history snapshot, store write and changelog row for
    all seven fields (the count the per-field path could not reach).
  * the SAME END STATE as the per-field path, including the whole-store terminal
    normalizer that path ran at every write (g-115-10145): a batch must keep
    healing other goals exactly as the CLI did, so its normalizer is the CLI's and
    not the daemon twin's (which nulls blocked_by).
  * REFUSALS WRITE NOTHING and list every field in `unwritten`.
  * A field the authoritative store does not carry fails the call and is NAMED:
    exactly the unconfirmed fields, never all of them, never none.

Hermetic: a tmp world, STORAGE_BACKEND=local (mind_api/tests/conftest.py pins it).
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ASP_PY = REPO_ROOT / "core" / "scripts" / "aspirations.py"
GID = "g-100-01"

# Every field the close writes, with the value the close would compute.
FIELDS = {
    "consecutive_routine": 3,
    "consecutive_deep": 2,
    "last_outcome_origin": "genuine",
    "substantive_runs": 6,
    "substantive_hits": 3,
    "last_substantive_at": "2026-10-03T12:00:00",
    "pull_signal": None,
}


class _Paths:
    def __init__(self, world: Path, agent: Path):
        self.world, self.agent = world, agent
        self.meta = world.parent / "meta"
        self.project_root = REPO_ROOT
        self.agent_name = "alpha"
        self.agents_root = agent.parent


class _Ctx:
    def __init__(self, world, agent, *, query=None, body=None, headers=None):
        self.paths = _Paths(world, agent)
        self.query = query if query is not None else {"id": GID, "source": "world"}
        self.body = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
        self.headers = {"x-mind-agent": "alpha"} if headers is None else headers


def _goals():
    return [
        {"id": GID, "title": "recurring goal", "status": "pending", "priority": "MEDIUM",
         "recurring": True, "interval_hours": 4.0, "consecutive_routine": 2,
         "consecutive_deep": 1, "substantive_runs": 5, "substantive_hits": 2,
         "pull_signal": {"reason": "pulled"}, "blocked_by": [],
         "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
         "participants": ["agent"]},
        # A TERMINAL goal carrying residue the CLI writer heals at every write.
        {"id": "g-100-02", "title": "closed goal", "status": "completed",
         "priority": "LOW", "completed_at": None, "completed_date": "2026-09-01",
         "defer_reason": "stale", "defer_reason_set_at": "2026-09-01T00:00:00",
         "deferred_until": "2026-09-02", "blocker_ref": {"x": 1},
         "blocked_since": "2026-09-01T00:00:00", "blocked_by": [GID],
         "participants": ["agent"]},
    ]


def _seed(tmp: Path) -> tuple:
    world = tmp / "world"
    agent = tmp / "agents" / "alpha"
    world.mkdir(parents=True)
    agent.mkdir(parents=True)
    (tmp / "meta").mkdir()
    asp = {"id": "asp-100", "title": "Test", "motivation": "t", "scope": "project",
           "priority": "MEDIUM", "status": "active", "created": "2026-09-25T00:00:00",
           "goals": _goals()}
    (world / "aspirations.jsonl").write_text(json.dumps(asp) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world, agent


def _store(world: Path) -> list:
    return [json.loads(l) for l in
            (world / "aspirations.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


def _goal(world: Path, gid: str = GID) -> dict:
    return next(g for a in _store(world) for g in a["goals"] if g["id"] == gid)


def _call(world, agent, fields, **kw):
    from mind_api.src.endpoints import aspirations_write as aw
    resp = aw.update_goal_fields(_Ctx(world, agent, body={"fields": fields}, **kw))
    return resp.status, json.loads(resp.body)


def test_all_seven_fields_land_in_one_rewrite(tmp_path, monkeypatch):
    from mind_api.src.endpoints import aspirations_write as aw
    world, agent = _seed(tmp_path)
    writes, snapshots, rows = [], [], []
    real_write = aw._atomic_write_jsonl
    monkeypatch.setattr(aw, "_atomic_write_jsonl",
                        lambda p, items: (writes.append(p), real_write(p, items))[1])
    real_snap, real_append = aw.history.snapshot, aw.changelog.append
    monkeypatch.setattr(aw.history, "snapshot",
                        lambda *a, **k: (snapshots.append(k.get("summary")), real_snap(*a, **k))[1])
    monkeypatch.setattr(aw.changelog, "append",
                        lambda *a, **k: (rows.append(k.get("summary")), real_append(*a, **k))[1])

    status, body = _call(world, agent, FIELDS)

    assert status == 200, body
    assert body["fields"] == list(FIELDS) and body["goal_id"] == GID
    assert (len(writes), len(snapshots), len(rows)) == (1, 1, 1), (
        "one close must be ONE store rewrite, one snapshot, one changelog row; the "
        f"per-field path made one of each per field. got {len(writes)}/{len(snapshots)}/{len(rows)}")
    assert rows[0].startswith(f"update-goal-fields {GID} consecutive_routine,")
    g = _goal(world)
    for name, want in FIELDS.items():
        assert g[name] == want, (name, g.get(name))
    assert "pull_signal" in g and g["pull_signal"] is None  # cleared to null, key kept
    assert g["title"] == "recurring goal" and g["last_modified"]


def test_end_state_matches_one_cli_call_per_field(tmp_path):
    """The route replaces seven CLI writes, so its end state must be THEIRS,
    including what the CLI writer's whole-store normalizer did to ANOTHER goal."""
    cli_world, cli_agent = _seed(tmp_path / "cli")
    api_world, api_agent = _seed(tmp_path / "api")
    env = dict(os.environ, MIND_WORLD=str(cli_world), MIND_META=str(tmp_path / "cli" / "meta"),
               MIND_AGENT="alpha", MIND_AGENT_DIR=str(cli_agent), STORAGE_BACKEND="local")
    for name, value in FIELDS.items():
        proc = subprocess.run(
            [sys.executable, str(ASP_PY), "--source", "world", "update-goal", GID, name,
             "null" if value is None else str(value)],
            env=env, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, (name, proc.stderr)

    status, body = _call(api_world, api_agent, FIELDS)
    assert status == 200, body

    def comparable(world):
        store = _store(world)
        for a in store:
            for g in a["goals"]:
                g.pop("last_modified", None)  # stamped per call, so it differs by seconds
        return store

    assert comparable(api_world) == comparable(cli_world)
    # Positive control: the normalizer really did something to the OTHER goal, so
    # equality above is not two untouched stores agreeing.
    healed = _goal(api_world, "g-100-02")
    assert healed["completed_at"] == "2026-09-01T00:00:00"
    assert healed["defer_reason"] is None and healed["blocker_ref"] is None
    assert healed["blocked_by"] == [GID], "blocked_by is lineage; the CLI normalizer keeps it"


def _load_cli():
    sys.path.insert(0, str(REPO_ROOT / "core" / "scripts"))
    spec = importlib.util.spec_from_file_location("aspirations_cli_for_parity", ASP_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("status", ["completed", "skipped", "expired", "decomposed", "superseded",
                                    "pending", "in-progress", "blocked"])
@pytest.mark.parametrize("variant", [
    {},
    {"completed_at": None, "completed_date": "2026-09-01"},
    {"completed_at": None, "completed_date": "2026-09-01T08:30:00"},
    {"completed_at": "2026-09-02T00:00:00"},
    {"defer_reason": "r", "defer_reason_set_at": "t", "deferred_until": "d",
     "blocker_ref": {"a": 1}, "blocked_since": "t", "blocked_by": ["g-1-1"]},
])
def test_normalizer_is_the_cli_normalizer(status, variant):
    """Parity against the CLI function ITSELF, per status and residue shape."""
    from mind_api.src.endpoints import aspirations_write as aw
    cli = _load_cli()
    assert cli.TERMINAL_GOAL_STATUSES == aw._TERMINAL_GOAL_STATUSES
    goal = {"id": "g-1-1", "status": status, **variant}
    mine, theirs = copy.deepcopy([{"goals": [goal]}]), copy.deepcopy([{"goals": [goal]}])
    aw._normalize_terminal_goals_cli_parity(mine)
    cli._normalize_terminal_goals_in(theirs)
    for side in (mine, theirs):  # the now() fallback differs by a second between calls
        g = side[0]["goals"][0]
        if g.get("completed_at") and "completed_date" not in variant and "completed_at" not in variant:
            g["completed_at"] = "<now>"
    assert mine == theirs


@pytest.mark.parametrize("fields, error, needle", [
    ({"status": "completed"}, "invalid_fields", "not writable"),
    ({"consecutive_routine": -1}, "invalid_fields", "non-negative integer"),
    ({"consecutive_routine": "3"}, "invalid_fields", "non-negative integer"),
    ({"substantive_runs": True}, "invalid_fields", "non-negative integer"),
    ({"last_outcome_origin": ""}, "invalid_fields", "non-empty string"),
    ({"pull_signal": {"reason": "set"}}, "invalid_fields", "only be cleared"),
    ({"consecutive_routine": 1, "defer_reason": "x"}, "invalid_fields", "defer_reason"),
])
def test_refusal_writes_nothing_and_lists_every_field(tmp_path, fields, error, needle):
    world, agent = _seed(tmp_path)
    before = (world / "aspirations.jsonl").read_bytes()
    status, body = _call(world, agent, fields)
    assert (status, body["error"]) == (400, error)
    assert needle in body["detail"]
    assert body["unwritten"] == list(fields), "a refused batch applied none of it"
    assert (world / "aspirations.jsonl").read_bytes() == before


def test_unknown_goal_and_bad_requests_write_nothing(tmp_path):
    from mind_api.src.endpoints import aspirations_write as aw
    world, agent = _seed(tmp_path)
    before = (world / "aspirations.jsonl").read_bytes()
    status, body = _call(world, agent, {"consecutive_routine": 1},
                         query={"id": "g-100-99", "source": "world"})
    assert (status, body["error"], body["unwritten"]) == (404, "goal_not_found", ["consecutive_routine"])
    for query, body_in, want in [
        ({"source": "world"}, {"fields": {"consecutive_routine": 1}}, "missing_param"),
        ({"id": "nope", "source": "world"}, {"fields": {"consecutive_routine": 1}}, "invalid_goal_id"),
        ({"id": GID, "source": "bogus"}, {"fields": {"consecutive_routine": 1}}, "invalid_source"),
        ({"id": GID, "source": "world"}, {"fields": {}}, "invalid_body"),
        ({"id": GID, "source": "world"}, {"nope": 1}, "invalid_body"),
        ({"id": GID, "source": "agent"}, {"fields": {"consecutive_routine": 1}}, "missing_agent_header"),
    ]:
        headers = {} if want == "missing_agent_header" else None
        resp = aw.update_goal_fields(_Ctx(world, agent, query=query, body=body_in, headers=headers))
        assert (resp.status, json.loads(resp.body)["error"]) == (400, want), want
    assert (world / "aspirations.jsonl").read_bytes() == before


def test_a_field_the_authoritative_store_lacks_is_named_and_only_it(tmp_path, monkeypatch):
    """Failure injection: the read-back finds ONE stale field. The call fails and
    names exactly that field -- not all seven, and not none (a 200 over it)."""
    from mind_api.src.endpoints import aspirations_write as aw
    world, agent = _seed(tmp_path)

    def stale_lookup(live_path, asp_id, goal_id):
        g = copy.deepcopy(_goal(world))
        g["substantive_runs"] = 5  # the pre-write value: this one PUT "did not land"
        return "found", g

    monkeypatch.setattr(aw, "_authoritative_goal_lookup", stale_lookup)
    status, body = _call(world, agent, FIELDS)
    assert (status, body["error"]) == (500, "update_not_persisted")
    assert body["unwritten"] == ["substantive_runs"]
    assert "substantive_runs" in body["detail"] and "consecutive_routine" not in body["detail"]


def test_a_goal_absent_from_the_authoritative_store_names_every_field(tmp_path, monkeypatch):
    from mind_api.src.endpoints import aspirations_write as aw
    world, agent = _seed(tmp_path)
    monkeypatch.setattr(aw, "_authoritative_goal_lookup", lambda *a: ("goal-absent", None))
    status, body = _call(world, agent, FIELDS)
    assert (status, body["error"]) == (500, "update_not_persisted")
    assert body["unwritten"] == list(FIELDS)


def test_a_confirming_read_back_is_a_200(tmp_path, monkeypatch):
    """Positive control for the two failure tests: the SAME hook, returning the
    written goal, must succeed -- so those failures come from the missing field,
    not from the hook being unusable."""
    from mind_api.src.endpoints import aspirations_write as aw
    world, agent = _seed(tmp_path)
    monkeypatch.setattr(aw, "_authoritative_goal_lookup",
                        lambda *a: ("found", copy.deepcopy(_goal(world))))
    status, body = _call(world, agent, FIELDS)
    assert status == 200, body


def test_allowlist_is_made_of_known_goal_fields():
    from mind_api.src.endpoints import aspirations_write as aw
    assert aw._BATCH_ALLOWED_FIELDS == set(FIELDS)
    assert all(aw._is_known_goal_field(n) for n in aw._BATCH_ALLOWED_FIELDS)
    assert not aw._is_known_goal_field("not_a_goal_field")  # the predicate can say no
