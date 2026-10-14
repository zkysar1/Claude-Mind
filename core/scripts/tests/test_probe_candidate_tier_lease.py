""" -- a watchdog probe's dedup lease must see the candidate tier.

A probe that files a goal HOLDS A LEASE on the goal's origin_signal (guard-3419):
the dedup read is the TAKE and the close-on-clear read is the RELEASE. Both reads
used a status vocabulary that stopped at (pending, in-progress, blocked), while
the intake tier (aspirations.yaml candidate_tier) files every `investigate:`
origin as `candidate`. So a probe never saw an earlier filing of its signal, its
own or another agent's, and filed again, and when the condition cleared its close
path ignored the candidate too: a lease nobody could take twice and nobody could
release.

Measured on the live queue 2026-10-13 (zeta, machine_id cc-02, uname -r
6.8.0-142-generic): five open `candidate` leases for ONE stalled release tag
(g-115-12254, 12255, 12256, 12258, 12259, filed by five different agents within
50 minutes) and four for the next (g-115-12361, 12362, 12363, 12367, four
different agents), none ever retired.

The fix is one vocabulary in pointer_freshness read by every site:

  OPEN_STATUSES       what the TAKE side treats as a live lease
  RETIRABLE_STATUSES  what the RELEASE side may close (open AND unstarted)
  is_retirable(goal)  the single close predicate the four probes share

These tests pin the three halves separately, because each can regress alone:
the sets themselves (and their drift from the lifecycle), the reads, and the
four close paths driven through the real reads against a real tmp store. The
last group runs the whole lease -- take, hold, release, retake -- per probe.
"""

import ast
import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _release_train as rt  # noqa: E402
import _session_telemetry as st  # noqa: E402
import pointer_freshness as pf  # noqa: E402
from gates import candidate_transition as ct  # noqa: E402


def _load_watchdog():
    spec = importlib.util.spec_from_file_location(
        "watchdog_candidate_lease_under_test", SCRIPTS / "agent-watchdog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def wd():
    return _load_watchdog()


def _status_literal(name: str) -> set:
    """A module-level status literal of aspirations.py, read WITHOUT importing it
    (the import pulls _paths and five sibling modules), the idiom
    test_valid_goal_statuses_mirror_parity.py uses for the same constants."""
    tree = ast.parse((SCRIPTS / "aspirations.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return set(ast.literal_eval(node.value))
    raise AssertionError(f"module-level assignment {name!r} not found in aspirations.py")


# The lifecycle, spelled out. test_the_two_lists_cover_the_lifecycle keeps these
# from falling behind VALID_GOAL_STATUSES, so a new status cannot slip past the
# parametrized cases below without a test going red.
OPEN = ["pending", "in-progress", "blocked", "candidate"]
TERMINAL = ["completed", "skipped", "expired", "decomposed", "superseded"]


# ---------------------------------------------------------------------------
# the vocabulary
# ---------------------------------------------------------------------------

def test_the_two_lists_cover_the_lifecycle():
    assert set(OPEN) | set(TERMINAL) == _status_literal("VALID_GOAL_STATUSES")
    assert set(TERMINAL) == _status_literal("TERMINAL_GOAL_STATUSES")


def test_open_statuses_are_the_non_terminal_lifecycle():
    """The drift pin. OPEN_STATUSES is a literal (pointer_freshness is stdlib-only
    and the watchdog imports it inside a fail-open tick), so nothing derives it
    from the lifecycle. This is the derivation, run as a test: a status added to
    VALID_GOAL_STATUSES must be classified here as open or terminal before the
    suite passes, and an unclassified one files instead of suppressing."""
    expected = (_status_literal("VALID_GOAL_STATUSES")
                - _status_literal("TERMINAL_GOAL_STATUSES"))
    assert pf.OPEN_STATUSES == expected, (
        f"pointer_freshness.OPEN_STATUSES {sorted(pf.OPEN_STATUSES)} no longer equals "
        f"VALID_GOAL_STATUSES minus TERMINAL_GOAL_STATUSES {sorted(expected)}: a "
        f"probe's dedup lease reads a different lifecycle than the store writes")


def test_retirable_statuses_are_open_and_unstarted():
    assert pf.RETIRABLE_STATUSES == {"pending", "candidate"}
    assert pf.RETIRABLE_STATUSES <= pf.OPEN_STATUSES, (
        "a status the probe may retire must be one its dedup can see, or it "
        "would retire leases it never takes")
    # in-progress is live work; blocked carries somebody's stated reason (guard-1007)
    assert not {"in-progress", "blocked"} & pf.RETIRABLE_STATUSES


@pytest.mark.parametrize("status,claimed_by,retirable", [
    ("pending", None, True),
    ("candidate", None, True),
    ("pending", "zeta", False),
    ("candidate", "zeta", False),
    ("in-progress", None, False),
    ("blocked", None, False),
    ("completed", None, False),
    ("skipped", None, False),
    ("superseded", None, False),
    ("not-a-status", None, False),
    (None, None, False),
])
def test_is_retirable_truth_table(status, claimed_by, retirable):
    goal = {"id": "g-1"}
    if status is not None:
        goal["status"] = status
    if claimed_by:
        goal["claimed_by"] = claimed_by
    assert pf.is_retirable(goal) is retirable


def test_the_intake_gate_admits_the_write_the_retire_makes():
    """A candidate lease is only releasable if the status write the probes make,
    `skipped`, is a row of the candidate transition table (a refused write would
    leave the lease held forever and read as `close-failed`). The gate also
    ledgers it, which is why the probe needs no ledger argument."""
    for status in sorted(pf.RETIRABLE_STATUSES):
        verdict = ct.evaluate(status, "skipped")
        assert verdict["allowed"], verdict["message"]
    row = ct.evaluate("candidate", "skipped")
    assert row["ledger"] is True and row["verdict"] == "close-moot"


# ---------------------------------------------------------------------------
# the take side: the reads
# ---------------------------------------------------------------------------

SIGNAL = "investigate:probe-lease-under-test"


def _write_aspirations(root: Path, goals: list) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / "aspirations.jsonl"
    p.write_text(json.dumps({"id": "asp-115", "goals": goals}) + "\n",
                 encoding="utf-8")
    return p


def _goal(gid, status, signal=SIGNAL, **extra):
    return dict({"id": gid, "origin_signal": signal, "status": status}, **extra)


@pytest.mark.parametrize("status", OPEN)
def test_every_open_status_holds_the_lease(tmp_path, status):
    _write_aspirations(tmp_path / "world", [_goal("g-1", status)])
    got = pf.open_goal_records(SIGNAL, tmp_path / "world", None)
    assert [g["id"] for g in got] == ["g-1"]
    assert pf.open_goal_exists(SIGNAL, tmp_path / "world", None) is True


@pytest.mark.parametrize("status", TERMINAL)
def test_every_terminal_status_frees_the_lease(tmp_path, status):
    _write_aspirations(tmp_path / "world", [_goal("g-1", status)])
    assert pf.open_goal_records(SIGNAL, tmp_path / "world", None) == []
    assert pf.open_goal_exists(SIGNAL, tmp_path / "world", None) is False


def test_a_flat_goal_shaped_line_in_the_candidate_tier_holds_the_lease(tmp_path):
    """The defensive branch: open_goal_records also honors a goal-shaped line at
    the top level, and it reads the same set."""
    root = tmp_path / "world"
    root.mkdir()
    (root / "aspirations.jsonl").write_text(
        json.dumps(_goal("g-flat", "candidate")) + "\n", encoding="utf-8")
    assert [g["id"] for g in pf.open_goal_records(SIGNAL, root, None)] == ["g-flat"]


def test_the_candidate_lease_is_found_in_the_agent_queue_too(tmp_path):
    _write_aspirations(tmp_path / "agent", [_goal("g-a", "candidate")])
    got = pf.open_goal_records(SIGNAL, tmp_path / "world", tmp_path / "agent")
    assert [(g["id"], g["_source"]) for g in got] == [("g-a", "agent")]


def test_the_release_train_prefix_read_sees_a_candidate_lease(tmp_path):
    """open_release_goals widens the exact-signal read to the PREFIX, so the
    retire path can find a lease filed for any older tag. It reads the same
    OPEN_STATUSES; a candidate lease for an old tag must be in its result."""
    old, new = rt.signal_for("v2.12.95"), rt.signal_for("v2.12.99")
    _write_aspirations(tmp_path / "world", [
        _goal("g-old", "candidate", signal=old),
        _goal("g-new", "pending", signal=new),
        _goal("g-done", "skipped", signal=old),
    ])
    got = rt.open_release_goals(tmp_path / "world", None)
    assert sorted(g["id"] for g in got) == ["g-new", "g-old"]


def test_no_probe_module_keeps_a_private_pending_only_predicate():
    """The static half of "every probe reads the shared set": a goal filter
    spelled `status == "pending"` in the watchdog or the release-train helpers
    is the shape that went blind to the candidate tier. The funnel's retire path
    in _frontier.py names both statuses and is pinned by its own tests."""
    pattern = re.compile(r"""get\(\s*["']status["']\s*\)\s*(?:!=|==)\s*["']pending["']""")
    for name in ("agent-watchdog.py", "_release_train.py"):
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        assert not pattern.search(src), (
            f"{name} compares a goal's status to the literal 'pending'; read "
            f"pointer_freshness.OPEN_STATUSES / is_retirable instead (g-115-12092)")


# ---------------------------------------------------------------------------
# the probes, driven through the real reads against a real tmp store
# ---------------------------------------------------------------------------

class _Ctx:
    def __init__(self, tmp_path, agent_name="echo"):
        self.agent_name = agent_name
        self.agent_dir = tmp_path / "agent"
        self.project_root_path = tmp_path


class _Runner:
    """Stands in for subprocess.run: records argv and the JSON body piped to
    aspirations-add-goal.sh, and answers every call with success."""

    def __init__(self):
        self.calls, self.bodies = [], []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if kw.get("input"):
            self.bodies.append(json.loads(kw["input"]))
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"id": "g-new"}),
                                     stderr="")

    def reset(self):
        self.calls.clear()
        self.bodies.clear()


def _wire(wd, tmp_path, monkeypatch, runner):
    """Point every probe at a tmp world and capture its subprocess calls. The box
    id is pinned (the wedge and drift keys carry it) and the overcap probe's
    pytest write-refusal is switched off, as its own comment says a test that
    stubs the boundary must."""
    monkeypatch.setitem(sys.modules, "_paths",
                        types.SimpleNamespace(WORLD_DIR=str(tmp_path / "world")))
    monkeypatch.setitem(sys.modules, "_runtime_bash", types.SimpleNamespace(BASH="bash"))
    monkeypatch.setattr(wd.subprocess, "run", runner)
    monkeypatch.setenv("MACHINE_ID", "cc-lease")
    monkeypatch.setattr(st, "_MACHINE_ID", None)
    monkeypatch.setattr(wd.StoreOvercapProbe, "REFUSE_WRITES_UNDER_PYTEST", False)
    return tmp_path / "world"


WEDGE = {"files": {"a.md": 9}, "wedged_count": 1}
DRIFT = {"files": {}, "wedged_count": 1, "breaches": ["ahead 12 over 10"],
         "branch": "main", "ahead": 12, "behind": 0, "disk_used_pct": 40,
         "carrier_refs": [], "consecutive_breach": 5, "fetched_this_tick": True}
TRAIN = {"newest_tag": "v2.12.99", "tag_age_hours": 30.5, "commits_past": 4,
         "commit_sample": ["abc1234 a framework change"],
         "tag_created": "2026-10-12T00:00:00", "fetch_age_minutes": 3,
         "tag_basis": "origin"}
TRAIN_CFG = {"stale_hours": 24, "skip_hold_hours": 24}
OVERCAP = {"over_now": {"a.jsonl": {"ratio": 1.3, "total": 130, "bound": 100,
                                    "mode": "line", "action": "none",
                                    "owner_goal": None, "machine_local": False}},
           "threshold": 1.0, "line_bounded": 3}


class _Lease:
    """One probe's file and close entry points behind a common shape, so the
    cases below read the same for all four."""

    def __init__(self, wd, kind, tmp_path):
        self.kind = kind
        self.world = tmp_path / "world"
        ctx = _Ctx(tmp_path)
        if kind == "wedge":
            p = wd.MirrorWedgeProbe(ctx)
            p.consecutive_wedged = 5
            self.file = lambda: p._file_wedge_goal(WEDGE)
            self.close = lambda: p._close_wedge_goal()
        elif kind == "drift":
            p = wd.GitDriftProbe(ctx)
            p.consecutive_breach = 5
            self.file = lambda: p._file_drift_goal(DRIFT)
            self.close = lambda: p._close_drift_goal()
        elif kind == "release":
            p = wd.ReleaseTrainProbe(ctx)
            self.file = lambda: p._file_release_goal(
                TRAIN, TRAIN_CFG, rt.open_release_goals(self.world, ctx.agent_dir))
            self.close = lambda: p._retire_release_goals(
                rt.open_release_goals(self.world, ctx.agent_dir),
                "a newer tag was cut")
        else:
            p = wd.StoreOvercapProbe(ctx)
            self.file = lambda: p._file_overcap_goal(["a.jsonl"], OVERCAP)
            self.close = lambda: p._close_overcap_goal(OVERCAP)


KINDS = ["wedge", "drift", "release", "overcap"]


@pytest.mark.parametrize("kind", KINDS)
def test_a_probe_dedups_against_a_candidate_lease_instead_of_filing_again(
        wd, tmp_path, monkeypatch, kind):
    """THE outcome. Tick one files; the goal lands in the candidate tier; tick
    two finds it and files nothing. Before g-115-12092 tick two filed again."""
    runner = _Runner()
    _wire(wd, tmp_path, monkeypatch, runner)
    lease = _Lease(wd, kind, tmp_path)

    first = lease.file()
    assert first["filed"] is True, first
    signal = runner.bodies[-1]["origin_signal"]
    _write_aspirations(lease.world, [_goal("g-new", "candidate", signal=signal)])

    runner.reset()
    second = lease.file()
    assert second["filed"] is False and second.get("dedup") is True, second
    assert runner.calls == [], "a deduped tick must make no call at all"


@pytest.mark.parametrize("kind", KINDS)
def test_the_control_a_terminal_goal_does_not_suppress_the_next_filing(
        wd, tmp_path, monkeypatch, kind):
    """Without this the case above could pass on a probe that dedups against
    everything. The same goal, closed, must let the probe file again."""
    runner = _Runner()
    _wire(wd, tmp_path, monkeypatch, runner)
    lease = _Lease(wd, kind, tmp_path)
    lease.file()
    signal = runner.bodies[-1]["origin_signal"]
    _write_aspirations(lease.world, [_goal("g-old", "skipped", signal=signal)])

    runner.reset()
    assert lease.file()["filed"] is True
    assert len(runner.bodies) == 1


def _status_writes(calls):
    return [(c[2], c[3], c[4]) for c in calls]


@pytest.mark.parametrize("kind", KINDS)
def test_a_candidate_lease_is_released_when_the_condition_clears(
        wd, tmp_path, monkeypatch, kind):
    runner = _Runner()
    _wire(wd, tmp_path, monkeypatch, runner)
    lease = _Lease(wd, kind, tmp_path)
    lease.file()
    signal = runner.bodies[-1]["origin_signal"]
    _write_aspirations(lease.world, [_goal("g-new", "candidate", signal=signal)])

    runner.reset()
    out = lease.close()

    assert out["closed"] == ["g-new"], out
    writes = _status_writes(runner.calls)
    assert [(gid, field) for gid, field, _ in writes] == [
        ("g-new", "outcome_note"), ("g-new", "status")], (
        "outcome_note MUST land before status: a failed second write then leaves "
        "an open goal WITH an explanation, never a closed one without")
    assert writes[-1][2] == "skipped", "never `completed`: nothing was investigated"
    assert all(c[-2:] == ["--source", "world"] for c in runner.calls)


@pytest.mark.parametrize("held_status,claimed_by", [
    ("candidate", "zeta"),
    ("pending", "zeta"),
    ("in-progress", None),
    ("blocked", None),
])
@pytest.mark.parametrize("kind", KINDS)
def test_started_or_claimed_work_is_left_to_its_owner(
        wd, tmp_path, monkeypatch, kind, held_status, claimed_by):
    """guard-1007. Widening the release to the candidate tier must not widen it
    to work somebody has started, claimed, or blocked with a reason."""
    runner = _Runner()
    _wire(wd, tmp_path, monkeypatch, runner)
    lease = _Lease(wd, kind, tmp_path)
    lease.file()
    signal = runner.bodies[-1]["origin_signal"]
    extra = {"claimed_by": claimed_by} if claimed_by else {}
    _write_aspirations(lease.world,
                       [_goal("g-held", held_status, signal=signal, **extra)])

    runner.reset()
    out = lease.close()

    assert out["closed"] == [], out
    assert runner.calls == [], "a held goal must not be touched at all"
    assert "g-held" in out["detail"]


@pytest.mark.parametrize("kind", KINDS)
def test_the_whole_lease_take_hold_release_retake(wd, tmp_path, monkeypatch, kind):
    """The lifecycle guard-3419 describes, end to end with the goal in the
    candidate tier throughout: file it, hold it across a second tick, release it
    when the condition clears, and take it again on the next episode. A lease
    that cannot be taken twice or cannot be released fails one of the four."""
    runner = _Runner()
    _wire(wd, tmp_path, monkeypatch, runner)
    lease = _Lease(wd, kind, tmp_path)

    # TAKE
    assert lease.file()["filed"] is True
    signal = runner.bodies[-1]["origin_signal"]
    _write_aspirations(lease.world, [_goal("g-1", "candidate", signal=signal)])

    # HOLD
    runner.reset()
    assert lease.file().get("dedup") is True
    assert runner.calls == []

    # RELEASE
    assert lease.close()["closed"] == ["g-1"]
    _write_aspirations(lease.world, [_goal("g-1", "skipped", signal=signal)])

    # RETAKE
    runner.reset()
    assert lease.file()["filed"] is True
    assert runner.bodies[-1]["origin_signal"] == signal, (
        "the second episode must file under the key the close path releases")
