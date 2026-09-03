"""Tests for the load-bearing capture priority-merge lane ().

Every test builds a hermetic tmp project root. The lane writes the AGENT-WIDE
working memory, so it must never be exercised against the live tree.

A tmp project root stopped being SUFFICIENT for that in g-306-420: the capture
carrier moved from `agents/<agent>/session/pending-body-merges/` (derived from
the project root, hermetic for free) to `world/body-carriers/<agent>/`, and
`world` is an EXTERNAL path that the project root does not determine. So the
`_hermetic_world` fixture below is now load-bearing, not tidiness -- without it
these tests write real carriers into the LIVE world and then read them back
(measured: `bodies_scanned: 6` against a one-body fixture, 15 failures).

The load-bearing assertions, and why each one is here rather than being obvious:

  * ACTIVE Bodies contribute. This is the whole point of the lane —
    body-merge._enumerate_pending filters to closed-pending-merge, so an active
    Body's captures are invisible to generalize_down no matter how often
    consolidation runs. A version of this lane that inherited that filter would
    pass every other test in this file and be useless in production.
  * Entries merge VERBATIM. Dedup is by content hash, so stamping anything onto
    a merged entry would make the later full generalize_down append a second
    copy. The duplicate would appear hours later, in a different process, from
    a mutation that looked harmless here.
  * Unflagged entries are NOT merged. Without this the "lane" is just an early
    full merge, which is the thing the goal explicitly rules out.
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


import capture_fast_lane as cfl  # noqa: E402
import wm as wm_mod  # noqa: E402

bmg = _load("body_merge", "body-merge.py")


# --------------------------------------------------------------------------
# fixture helpers
# --------------------------------------------------------------------------

def _mk_root(tmp_path: Path, agent: str = "testagent") -> Path:
    (tmp_path / "agents" / agent / "session").mkdir(parents=True)
    (tmp_path / "agents" / agent / "sessions").mkdir(parents=True)
    return tmp_path


@pytest.fixture(autouse=True)
def _hermetic_world(tmp_path, monkeypatch):
    """Point the capture carrier's world root at tmp_path ().

    AUTOUSE because the escape is silent: a test that forgot it would still
    pass in isolation and only corrupt the run once a sibling test had left a
    carrier in the live world.
    """
    import _paths
    w = tmp_path / "world"
    w.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_paths, "WORLD_DIR", w, raising=False)
    return w


def _write_reducer_wm(root: Path, agent: str, slots: dict | None = None) -> Path:
    p = root / "agents" / agent / "session" / "working-memory.yaml"
    p.write_text(yaml.safe_dump({"slots": slots or {}}), encoding="utf-8")
    return p


def _write_body(root: Path, agent: str, unit_key: str, slots: dict,
                body_state: str = "active") -> Path:
    d = root / "agents" / agent / "sessions" / unit_key
    d.mkdir(parents=True, exist_ok=True)
    (d / "working-memory.yaml").write_text(
        yaml.safe_dump({"slots": slots}), encoding="utf-8")
    (d / "body-manifest.yaml").write_text(
        yaml.safe_dump({"body_state": body_state, "unit_key": unit_key}),
        encoding="utf-8")
    return d


def _entry(gid: str, *, load_bearing: bool = False, ts: str = "2026-08-15T10:00:00",
           fact: str = "a fact") -> dict:
    e = {"goal_id": gid, "fact": fact, "_item_ts": ts}
    if load_bearing:
        e["load_bearing"] = True
    return e


def _reducer_slots(root: Path, agent: str = "testagent") -> dict:
    p = root / "agents" / agent / "session" / "working-memory.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("slots") or {}


# --------------------------------------------------------------------------
# the Gate A bypass — the lane's reason to exist
# --------------------------------------------------------------------------

def test_active_body_contributes(tmp_path):
    """An ACTIVE Body's flagged capture merges. generalize_down cannot do this:
    _enumerate_pending filters on body_state == closed-pending-merge."""
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    _write_body(root, "testagent", "unit-active",
                {"encoding_capture": [_entry("g-1", load_bearing=True)]},
                body_state="active")

    s = cfl.fast_lane("testagent", project_root=root)

    assert s["merged"] == 1, s
    assert s["bodies_contributing"] == 1
    got = _reducer_slots(root)["encoding_capture"]
    assert [e["goal_id"] for e in got] == ["g-1"]


def test_generalize_down_would_not_see_the_active_body(tmp_path):
    """Positive control for the test above: prove the claim about the OTHER
    code path rather than asserting it in a comment. Without this, 'the lane
    bypasses Gate A' rests on a reading of body-merge rather than a measurement."""
    root = _mk_root(tmp_path)
    _write_body(root, "testagent", "unit-active", {"encoding_capture": []},
                body_state="active")
    sessions_root = root / "agents" / "testagent" / "sessions"

    pending = bmg._enumerate_pending(sessions_root, set(), None)
    assert pending == [], "an ACTIVE body must be invisible to generalize_down"

    # ...while this lane sees it.
    seen = cfl._enumerate_all_bodies(sessions_root, None)
    assert [uk for uk, _ in seen] == ["unit-active"]


def test_closed_pending_merge_body_also_contributes(tmp_path):
    """The lane must not FLIP the filter — it widens it. A closed-pending-merge
    Body is still eligible (it is simply also eligible for the full merge)."""
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    _write_body(root, "testagent", "unit-closed",
                {"spark_capture": [_entry("g-2", load_bearing=True)]},
                body_state="closed-pending-merge")

    s = cfl.fast_lane("testagent", project_root=root)
    assert s["merged"] == 1
    assert _reducer_slots(root)["spark_capture"][0]["goal_id"] == "g-2"


# --------------------------------------------------------------------------
# selectivity
# --------------------------------------------------------------------------

def test_unflagged_entries_are_not_merged(tmp_path):
    """A lane that carried everything would be an early full merge, which the
    goal explicitly rules out (consolidation batching is deliberate)."""
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    _write_body(root, "testagent", "u1", {
        "spark_capture": [_entry("keep", load_bearing=True),
                          _entry("drop-me"),
                          _entry("drop-me-too")],
    })

    s = cfl.fast_lane("testagent", project_root=root)

    assert s["merged"] == 1
    assert [e["goal_id"] for e in _reducer_slots(root)["spark_capture"]] == ["keep"]


def test_all_four_capture_lanes_are_covered(tmp_path):
    """A lane list that silently omitted one slot would look healthy forever.
    Drives off wm.CAPTURE_SLOTS so a fifth lane joins by being registered."""
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    _write_body(root, "testagent", "u1", {
        slot: [_entry(f"g-{slot}", load_bearing=True)]
        for slot in wm_mod.CAPTURE_SLOTS
    })

    s = cfl.fast_lane("testagent", project_root=root)

    assert s["merged"] == len(wm_mod.CAPTURE_SLOTS)
    assert set(s["by_slot"]) == set(wm_mod.CAPTURE_SLOTS)


# --------------------------------------------------------------------------
# idempotence + hash stability
# --------------------------------------------------------------------------

def test_second_run_merges_nothing(tmp_path):
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    _write_body(root, "testagent", "u1",
                {"hyp_capture": [_entry("g-3", load_bearing=True)]})

    first = cfl.fast_lane("testagent", project_root=root)
    second = cfl.fast_lane("testagent", project_root=root)

    assert first["merged"] == 1
    assert second["merged"] == 0
    assert second["already_present"] == 1
    assert len(_reducer_slots(root)["hyp_capture"]) == 1


def test_entry_is_copied_verbatim_so_the_full_merge_dedups_it(tmp_path):
    """The trap this pins: any stamp added to a merged entry changes its content
    hash, and generalize_down would then append a SECOND copy hours later, in a
    different process. Assert byte-equality AND that _dedup_append agrees."""
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    original = _entry("g-4", load_bearing=True, fact="do not mutate me")
    _write_body(root, "testagent", "u1", {"exp_capture": [dict(original)]})

    cfl.fast_lane("testagent", project_root=root)
    merged = _reducer_slots(root)["exp_capture"][0]

    assert merged == original, "entry must merge verbatim"
    assert bmg._content_hash(merged) == bmg._content_hash(original)
    # The real consequence: a subsequent full merge adds nothing.
    assert bmg._dedup_append([merged], [original]) == [merged]


# --------------------------------------------------------------------------
# role gate
# --------------------------------------------------------------------------

def test_worker_body_is_refused(tmp_path, monkeypatch):
    """A worker running this would write the agent-wide WM (forbidden) and act
    as a second reducer. Refusal must be the DEFAULT, not an opt-in."""
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    _write_body(root, "testagent", "my-sid",
                {"spark_capture": [_entry("g-5", load_bearing=True)]})
    monkeypatch.setenv("MIND_SID", "my-sid")

    s = cfl.fast_lane("testagent", project_root=root)

    assert s["role_refused"] is True
    assert s["merged"] == 0
    assert _reducer_slots(root) == {}, "a refused run must write nothing"


def test_reducer_without_a_fork_is_allowed(tmp_path, monkeypatch):
    """Negative control for the gate: the refusal must key on THIS session
    having a fork, not merely on some Body existing."""
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    _write_body(root, "testagent", "someone-elses-sid",
                {"spark_capture": [_entry("g-6", load_bearing=True)]})
    monkeypatch.setenv("MIND_SID", "reducer-sid-with-no-fork")

    s = cfl.fast_lane("testagent", project_root=root)

    assert s["role_refused"] is False
    assert s["merged"] == 1


# --------------------------------------------------------------------------
# telemetry
# --------------------------------------------------------------------------

def test_latency_is_measured_only_for_newly_merged_entries(tmp_path):
    """Re-measuring already-present entries on every pass would inflate the
    median forever — a metric that only ever gets worse teaches nothing."""
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    _write_body(root, "testagent", "u1",
                {"spark_capture": [_entry("g-7", load_bearing=True,
                                          ts="2026-08-15T10:00:00")]})

    first = cfl.fast_lane("testagent", project_root=root)
    second = cfl.fast_lane("testagent", project_root=root)

    assert first["latency_minutes_median"] is not None
    assert second["latency_minutes_median"] is None, \
        "no NEW entries -> no latency sample, not a re-measurement"


def test_unparseable_timestamp_is_counted_not_folded_in_as_zero(tmp_path):
    """guard-3440: an instrument must not express a value it cannot measure.
    A missing stamp is UNKNOWN latency, not zero latency."""
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    bad = {"goal_id": "g-8", "fact": "no stamp", "load_bearing": True}
    _write_body(root, "testagent", "u1", {"spark_capture": [bad]})

    s = cfl.fast_lane("testagent", project_root=root)

    assert s["merged"] == 1
    assert s["latency_unmeasurable"] == 1
    assert s["latency_minutes_median"] is None


def test_telemetry_row_is_written_on_a_merge(tmp_path):
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    _write_body(root, "testagent", "u1",
                {"spark_capture": [_entry("g-9", load_bearing=True)]})

    cfl.fast_lane("testagent", project_root=root)

    p = root / "agents" / "testagent" / "session" / cfl.TELEMETRY_FILENAME
    assert p.is_file()
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1 and rows[0]["merged"] == 1


def test_dry_run_writes_nothing(tmp_path):
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    _write_body(root, "testagent", "u1",
                {"spark_capture": [_entry("g-10", load_bearing=True)]})

    s = cfl.fast_lane("testagent", project_root=root, dry_run=True)

    assert s["merged"] == 1, "dry-run still REPORTS what would merge"
    assert _reducer_slots(root) == {}, "dry-run must not write the WM"
    assert not (root / "agents" / "testagent" / "session" / cfl.TELEMETRY_FILENAME).exists()


# --------------------------------------------------------------------------
# robustness — one bad Body must not sink the pass
# --------------------------------------------------------------------------

@pytest.mark.parametrize("payload", ["{{{ not yaml", "- a\n- b\n", ""])
def test_unreadable_body_is_skipped_not_fatal(tmp_path, payload):
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    bad = root / "agents" / "testagent" / "sessions" / "bad"
    bad.mkdir(parents=True)
    (bad / "working-memory.yaml").write_text(payload, encoding="utf-8")
    (bad / "body-manifest.yaml").write_text(
        yaml.safe_dump({"body_state": "active"}), encoding="utf-8")
    _write_body(root, "testagent", "good",
                {"spark_capture": [_entry("g-11", load_bearing=True)]})

    s = cfl.fast_lane("testagent", project_root=root)

    assert s["merged"] == 1, "the healthy Body must still contribute"


def test_no_bodies_is_a_clean_noop(tmp_path):
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    s = cfl.fast_lane("testagent", project_root=root)
    assert s["merged"] == 0 and s["bodies_scanned"] == 0
    assert "0 load-bearing captures" in cfl.format_line(s)


# --------------------------------------------------------------------------
# eviction exemption (wm.py / wm_write.py) — the precondition for the lane
# --------------------------------------------------------------------------

def test_eviction_prefers_unflagged_entries():
    k = wm_mod._eviction_sort_key
    arr = [_entry("old-unflagged", ts="2026-01-01T00:00:00"),
           _entry("old-flagged", load_bearing=True, ts="2026-01-02T00:00:00"),
           _entry("new-unflagged", ts="2026-08-15T00:00:00")]
    arr.sort(key=k)
    while len(arr) > 1:
        arr.pop(0)
    assert arr[0]["goal_id"] == "old-flagged", \
        "the OLDEST entry survived because it was flagged — FIFO alone would drop it"


def test_cap_still_holds_when_everything_is_flagged():
    """A cap defeatable by a field the writer controls is not a cap."""
    k = wm_mod._eviction_sort_key
    arr = [_entry(f"g{i}", load_bearing=True, ts=f"2026-08-{i + 1:02d}T00:00:00")
           for i in range(5)]
    arr.sort(key=k)
    while len(arr) > 2:
        arr.pop(0)
    assert len(arr) == 2
    assert [e["goal_id"] for e in arr] == ["g3", "g4"], "oldest flagged go first"


def test_non_dict_entries_do_not_crash_the_sort_key():
    k = wm_mod._eviction_sort_key
    arr = ["a string", 42, None, _entry("real", load_bearing=True)]
    arr.sort(key=k)
    assert arr[-1]["goal_id"] == "real"


def _daemon_src() -> str:
    # SCRIPT_DIR is core/scripts, so the project root is two levels up —
    # mind_api/ is a sibling of core/, not of scripts/.
    return (SCRIPT_DIR.parent.parent / "mind_api" / "src" / "endpoints"
            / "wm_write.py").read_text(encoding="utf-8")


def test_daemon_eviction_key_is_defined_and_actually_called():
    """The daemon copy is the LIVE path (wrappers are daemon-only), so the
    wm.py edit alone is inert at runtime — the g-115-1992 bug class.

    Two assertions, and the SECOND is the one that matters: a mirrored helper
    that is defined but never CALLED passes every definition check while
    changing nothing, which is precisely how that bug class re-enters. The
    daemon module cannot be imported standalone (package-relative imports), so
    this AST-parses instead — the same approach test_wm_reset_cadence.py uses.
    """
    import ast

    src = _daemon_src()
    tree = ast.parse(src)

    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_eviction_sort_key" in defined, \
        "daemon wm_write.py lost its _eviction_sort_key mirror (g-306-293)"

    # USE-SITE: it must be the sort key inside append_slot, not merely defined.
    body = src.split("def append_slot(", 1)[1].split("\ndef ", 1)[0]
    assert "key=_eviction_sort_key" in body, (
        "daemon append_slot no longer sorts with _eviction_sort_key — "
        "load-bearing captures would be FIFO-evicted at runtime while every "
        "CLI-side test stayed green (g-306-293 / g-115-1992)"
    )
    # And the OLD inline lambda must be gone, or the mirror is dead code.
    assert 'key=lambda x: x.get("_item_ts"' not in body, \
        "daemon append_slot still carries the pre-g-306-293 inline sort lambda"


def test_daemon_capture_slots_mirror_matches():
    """Behavioural equality of the constant, AST-extracted (see above for why
    the module cannot simply be imported)."""
    import ast

    for node in ast.walk(ast.parse(_daemon_src())):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "CAPTURE_SLOTS" for t in node.targets)):
            assert tuple(ast.literal_eval(node.value)) == tuple(wm_mod.CAPTURE_SLOTS)
            return
    pytest.fail("daemon wm_write.py has no CAPTURE_SLOTS mirror (g-306-293)")


# --------------------------------------------------------------------------
#  — the flagged:total ratio (the DENOMINATOR)
#
# `flagged_seen` alone cannot distinguish a healthy lane from one where the
# flag has stopped discriminating, and both of the flag's powers (eviction
# exemption, fast-lane priority) decay as the share rises. Every test below
# drives the REAL fast_lane rather than asserting on a hand-built summary —
# a ratio computed in the test is not evidence the production path emits one.
# --------------------------------------------------------------------------

def test_ratio_reports_flagged_and_total_not_a_bare_count(tmp_path):
    """1 flagged of 4 must report BOTH numbers (guard-4054: a rate is
    uninterpretable without the arrival count beside it)."""
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    _write_body(root, "testagent", "unit-a", {"spark_capture": [
        _entry("g-1", load_bearing=True),
        _entry("g-2"), _entry("g-3"), _entry("g-4"),
    ]})

    s = cfl.fast_lane("testagent", project_root=root)

    assert s["by_slot_ratio"]["spark_capture"] == {"flagged": 1, "total": 4}, s
    assert s["entries_seen"] == 4 and s["flagged_measurable"] == 1, s
    line = cfl.format_line(s)
    assert "spark_capture 1/4=25%" in line, line


def test_ratio_absent_when_no_capture_entries_exist(tmp_path):
    """NEGATIVE CONTROL (guard-3221). Without this the assertion above passes
    against any non-empty string — it must be shown that the fragment is
    absent when the upstream value is absent, not merely present when it is."""
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    _write_body(root, "testagent", "unit-empty", {})

    s = cfl.fast_lane("testagent", project_root=root)

    assert s["entries_seen"] == 0, s
    assert s["by_slot_ratio"] == {}, s
    assert "load-bearing share" not in cfl.format_line(s)


def test_denominator_counts_a_body_with_zero_flagged(tmp_path):
    """A Body holding entries but flagging NONE is a healthy lane and belongs
    in the denominator. Counting only Bodies that already have a flagged entry
    would restrict the population to those passing the very test being
    measured — the selection effect this ratio exists to expose."""
    root = _mk_root(tmp_path)
    _write_reducer_wm(root, "testagent")
    _write_body(root, "testagent", "unit-flagged",
                {"spark_capture": [_entry("g-1", load_bearing=True)]})
    _write_body(root, "testagent", "unit-clean",
                {"spark_capture": [_entry("g-2"), _entry("g-3"), _entry("g-4")]})

    s = cfl.fast_lane("testagent", project_root=root)

    # 1 of 4 across both Bodies. Dropping the all-clean Body would read 1/1=100%
    # — a maximally-degraded lane — from data that is 25%.
    assert s["by_slot_ratio"]["spark_capture"] == {"flagged": 1, "total": 4}, s
    assert "spark_capture 1/4=25%" in cfl.format_line(s)


def test_ratio_prints_on_the_zero_merged_branch(tmp_path):
    """The state most worth reporting — a degraded share with nothing NEW to
    merge — takes format_line's early return. A ratio gated on `merged` would
    be silent exactly when it matters (guard-3221)."""
    root = _mk_root(tmp_path)
    flagged = _entry("g-1", load_bearing=True)
    # Already in the reducer WM, so this run merges nothing.
    _write_reducer_wm(root, "testagent", {"spark_capture": [flagged]})
    _write_body(root, "testagent", "unit-a",
                {"spark_capture": [flagged, _entry("g-2")]})

    s = cfl.fast_lane("testagent", project_root=root)

    assert s["merged"] == 0, s
    line = cfl.format_line(s)
    assert "0 load-bearing captures to merge" in line, line
    assert "spark_capture 1/2=50%" in line, line
