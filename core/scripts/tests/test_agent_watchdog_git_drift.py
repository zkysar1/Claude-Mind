"""test_agent_watchdog_git_drift.py — GitDriftProbe (agent-watchdog.py, ).

THE GAP THIS PROBE CLOSES. Until 2026-08-14 nothing monitored per-box git
divergence, carrier-ref depth, or host disk. Every divergence measured on
2026-08-13 was found the same way — an operator asked a question: cc-07 forked
at 342 ahead since 2026-08-08; cc-08 accumulated 223 main-bound commits in 3
days; cc-03 sat DORMANT 163 behind with 0 ahead and 0 dirty, so no launch and no
loop ever fired a heal path. The companion disk metric is here for the identical
reason: zakbox1 reached 90% of 905G, equally invisible.

THE POSITIVE CONTROL IS THE POINT OF THIS FILE. A detector with no positive
control is indistinguishable from one that never fires (the g-115-4236 lesson,
restated in this goal's own verification criteria). So the ahead/behind tests do
NOT stub `_git` — they build a REAL git repository with a REAL local remote and
REAL commits, run the REAL probe against it, and assert the alert path fires.
Three guardrails shape how:

  - guard-1276: the fixture repo lives in pytest's `tmp_path`, NEVER under
    `agents/<agent>/temp/`. That directory is gitignored INSIDE the main repo, so
    `git init` there gives git no repo to find and it walks UP to the real one —
    a throwaway fixture would then operate on the live checkout.
  - guard-1833: the fixture is built from fresh commits, never from git HISTORY
    (`git show <ref>:<path>`, `git log`). CI checkouts are shallow by default, so
    a history-sourced fixture is a test that passes locally and cannot run in CI.
  - guard-1094: nothing here writes production state. `_file_drift_goal`,
    `_post_board_alert` and `_close_drift_goal` are monkeypatched in every test
    that reaches them; a positive control that writes real state must use
    synthetic identifiers, and the cleanest synthetic identifier is no write.

Tests:
  1.  Config — defaults come from aspirations.yaml, env overrides win, malformed
      overrides fall back rather than disabling the probe with an unreachable
      threshold.
  2.  POSITIVE CONTROL (ahead) — a real repo seeded past threshold fires critical.
  3.  DISCRIMINATION — the SAME repo under a raised threshold stays silent. A
      detector that fires on everything has not detected anything.
  4.  POSITIVE CONTROL (behind) — the cc-03 dormant-box shape.
  5.  Disk — fires above threshold, quiet below, and quiet when unreadable.
  6.  Carrier axis SKIPS (never counts) when liveness is unreadable, and records
      WHY in the payload — counting a closed Body's frozen ref is the false
      positive this goal's first draft fell into.
  7.  Carrier axis counts LIVE sids only.
  8.  ticks_to_file — one event per episode, not one per tick.
  9.  Clear path emits `git_drift_cleared` and releases the lease (guard-3419: a
      dedup keyed on open-goal existence with no release path disables the
      detector permanently).
  10. State round-trips through to_dict/from_dict (tick mode persists across
      separate process invocations).
  11. Registered in build_probes() AND in WORKER_SAFE_PROBES — the boxes that
      forked (cc-07, cc-08) were not reducers.
  12. Fail-open on a non-git directory.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load_watchdog():
    # agent-watchdog.py is hyphenated — load via importlib for its symbols.
    spec = importlib.util.spec_from_file_location(
        "agent_watchdog_gitdrift", CORE_SCRIPTS / "agent-watchdog.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WD = _load_watchdog()

_GIT_ID = ("-c", "user.email=probe@example.invalid", "-c", "user.name=probe")


class _Ctx:
    """Minimal stand-in for WatchdogContext."""

    def __init__(self, root: Path, agent_dir: Path | None = None) -> None:
        self.agent_name = "testagent"
        self.project_root_path = root
        self.agent_dir = agent_dir or root


def _run(cwd: Path, *args: str) -> None:
    proc = subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"


def _seed_repo(tmp_path: Path, *, ahead: int = 0, behind: int = 0) -> Path:
    """A real repo with a real local remote, `ahead` local-only commits and
    `behind` remote-only commits. Built from fresh commits only (guard-1833)."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    other = tmp_path / "other"
    remote.mkdir()
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                   capture_output=True, text=True, timeout=60, check=True)
    subprocess.run(["git", "init", "-b", "main", str(work)],
                   capture_output=True, text=True, timeout=60, check=True)
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run(work, "add", "seed.txt")
    _run(work, *_GIT_ID, "commit", "-m", "seed")
    _run(work, "remote", "add", "origin", str(remote))
    _run(work, "push", "-q", "origin", "main")

    if behind:
        # A second clone pushes commits `work` has not seen — the dormant-box shape.
        subprocess.run(["git", "clone", "-q", str(remote), str(other)],
                       capture_output=True, text=True, timeout=60, check=True)
        for i in range(behind):
            (other / f"r{i}.txt").write_text(f"{i}\n", encoding="utf-8")
            _run(other, "add", f"r{i}.txt")
            _run(other, *_GIT_ID, "commit", "-m", f"remote {i}")
        _run(other, "push", "-q", "origin", "main")

    for i in range(ahead):
        (work / f"l{i}.txt").write_text(f"{i}\n", encoding="utf-8")
        _run(work, "add", f"l{i}.txt")
        _run(work, *_GIT_ID, "commit", "-m", f"local {i}")

    _run(work, "fetch", "-q", "origin", "main")
    return work


def _probe(monkeypatch, root: Path, *, live_sids=frozenset(), no_escalate=True):
    """A probe on `root` with escalation stubbed out (guard-1094 — no test may
    write production queue or board state)."""
    monkeypatch.setattr(WD, "_live_body_sids", lambda _w: set(live_sids))
    p = WD.GitDriftProbe(_Ctx(root))
    if no_escalate:
        monkeypatch.setattr(p, "_file_drift_goal",
                            lambda payload: {"filed": True, "goal_id": "g-test-01",
                                             "error": None})
        monkeypatch.setattr(p, "_post_board_alert",
                            lambda payload, goal: {"posted": True, "msg_id": "msg-test"})
        monkeypatch.setattr(p, "_close_drift_goal",
                            lambda: {"attempted": False, "detail": None})
    return p


def _clear_env(monkeypatch) -> None:
    for name in WD._GIT_DRIFT_ENV.values():
        monkeypatch.delenv(name, raising=False)


# ── 1. config ────────────────────────────────────────────────────────────────

def test_config_defaults_come_from_aspirations_yaml(monkeypatch):
    """guard-308: the probe must READ the config, not restate it. If this drifts
    from core/config/aspirations.yaml the probe is running on stale policy."""
    _clear_env(monkeypatch)
    import yaml
    cfg_path = CORE_SCRIPTS.parent / "config" / "aspirations.yaml"
    with cfg_path.open(encoding="utf-8") as f:
        declared = (yaml.safe_load(f) or {}).get("git_drift") or {}
    assert declared, "aspirations.yaml must declare a git_drift block"
    got = WD._git_drift_config()
    for key, value in declared.items():
        assert got[key] == value, f"{key}: probe read {got[key]}, config declares {value}"


def test_env_override_wins(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_AHEAD", "3")
    monkeypatch.setenv("GIT_DRIFT_DISK_PCT", "95")
    cfg = WD._git_drift_config()
    assert cfg["ahead_threshold"] == 3
    assert cfg["disk_used_pct_threshold"] == 95


@pytest.mark.parametrize("raw", ["", "   ", "abc", "25%", "1e999x", "--4"])
def test_malformed_override_falls_back_to_config(monkeypatch, raw):
    """A bad value must not crash and must not silently disable the probe by
    yielding a threshold nothing can reach."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_AHEAD", raw)
    assert WD._git_drift_config()["ahead_threshold"] == 25


@pytest.mark.parametrize("raw", ["-1", "-5", "-0.5"])
def test_negative_override_is_rejected(monkeypatch, raw):
    """A sign error is the one malformed value that does NOT look malformed.

    `-5` parses cleanly through int(), so without an explicit guard it becomes a
    live threshold and every metric breaches on every tick, forever. The config
    path has always carried a `>= 0` check; the env path did not until this test
    forced it (found by re-reading the parser, not by a failing run)."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_AHEAD", raw)
    assert WD._git_drift_config()["ahead_threshold"] == 25


def test_zero_override_is_allowed(monkeypatch):
    """0 is a legitimate value — 'alert on ANY divergence' — and must survive the
    negative guard. A `> 0` check instead of `>= 0` would silently discard it."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_AHEAD", "0")
    assert WD._git_drift_config()["ahead_threshold"] == 0


# ── 2-3. POSITIVE CONTROL: ahead, and the discrimination proof ───────────────

def test_positive_control_seeded_ahead_fires(monkeypatch, tmp_path):
    """A REAL repo seeded past the ahead threshold fires the alert path.

    This is the goal's explicit verification criterion: 'a seeded ahead/behind
    condition past threshold fires the alert path'."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_AHEAD", "3")
    monkeypatch.setenv("GIT_DRIFT_TICKS_TO_FILE", "1")
    monkeypatch.setenv("GIT_DRIFT_DISK_PCT", "100")  # isolate the git axis
    repo = _seed_repo(tmp_path, ahead=5)
    p = _probe(monkeypatch, repo)
    events = p.check()
    assert len(events) == 1, f"expected one event, got {events}"
    ev = events[0]
    assert ev.probe == "git-drift"
    assert ev.event == "git_drift"
    assert ev.severity == "critical"
    assert ev.payload["ahead"] == 5
    assert ev.payload["behind"] == 0
    assert any("ahead=5" in b for b in ev.payload["breaches"])
    assert ev.payload["goal"]["goal_id"] == "g-test-01"
    assert ev.payload["board"]["posted"] is True


def test_discrimination_same_repo_under_threshold_is_silent(monkeypatch, tmp_path):
    """The SAME seeded repo, threshold raised above the seeded value: no event.

    Without this, test 2 proves only that the probe emits — not that it
    discriminates. A detector that fires on every input has detected nothing."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_AHEAD", "50")
    monkeypatch.setenv("GIT_DRIFT_TICKS_TO_FILE", "1")
    monkeypatch.setenv("GIT_DRIFT_DISK_PCT", "100")
    repo = _seed_repo(tmp_path, ahead=5)
    p = _probe(monkeypatch, repo)
    assert p.check() == []


# ── 4. POSITIVE CONTROL: behind (the cc-03 dormant-box shape) ────────────────

def test_positive_control_seeded_behind_fires(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_BEHIND", "2")
    monkeypatch.setenv("GIT_DRIFT_TICKS_TO_FILE", "1")
    monkeypatch.setenv("GIT_DRIFT_DISK_PCT", "100")
    repo = _seed_repo(tmp_path, behind=4)
    p = _probe(monkeypatch, repo)
    events = p.check()
    assert len(events) == 1
    assert events[0].payload["behind"] == 4
    assert events[0].payload["ahead"] == 0
    assert any("behind=4" in b for b in events[0].payload["breaches"])


# ── 5. disk ──────────────────────────────────────────────────────────────────

def test_disk_breach_fires(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_DISK_PCT", "80")
    monkeypatch.setenv("GIT_DRIFT_TICKS_TO_FILE", "1")
    monkeypatch.setattr(WD, "_disk_used_pct", lambda _p: 92.0)
    # Pinned LOW so this is a genuine breach under the both-conditions rule
    # (2026-08-22). Before that rule this test passed on the percentage alone,
    # and the unpinned real free space of the box running the suite silently
    # decided the outcome.
    monkeypatch.setattr(WD, "_disk_free_gib", lambda _p: 3.0)
    repo = _seed_repo(tmp_path)
    p = _probe(monkeypatch, repo)
    events = p.check()
    assert len(events) == 1
    assert events[0].payload["disk_used_pct"] == 92.0
    assert events[0].payload["disk_free_gib"] == 3.0
    # PIN THE `host-` PREFIX, not a bare `disk=` substring. The prior assertion
    # was `any("disk=92.0%" in b ...)`, which "host-disk=92.0%" ALSO satisfies —
    # so it survived the rename without noticing it, and would equally survive
    # the prefix being REMOVED again. That prefix is the whole point of the
    # change: shutil.disk_usage measures the filesystem CONTAINING the path,
    # which on a shared-fs container is the HOST volume, and a breach string
    # that does not say so becomes a goal title nobody can act on (guard-846;
    # guard-602 — assert the specific attribute you just wrote).
    # Back-ported from downstream prod 2026-08-23, where the assertion was
    # authored alongside the rename it pins.
    breach = next(b for b in events[0].payload["breaches"] if "disk=92.0%" in b)
    assert breach.startswith("host-disk=92.0%"), breach
    assert "HOST volume" in breach and "not reclaimable from inside" in breach


def test_disk_unreadable_is_not_a_breach(monkeypatch, tmp_path):
    """None means 'could not measure'. Treating it as a breach would alarm every
    platform where the call fails; treating it as 0% would be a false all-clear.
    It is neither — the metric is simply absent from the breach list."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_TICKS_TO_FILE", "1")
    monkeypatch.setattr(WD, "_disk_used_pct", lambda _p: None)
    repo = _seed_repo(tmp_path)
    p = _probe(monkeypatch, repo)
    assert p.check() == []


def test_disk_used_pct_is_df_style(tmp_path):
    """df's Use% is used/(used+avail), which excludes root-reserved blocks and is
    both what a human sees and the more conservative reading."""
    pct = WD._disk_used_pct(tmp_path)
    assert pct is None or 0.0 <= pct <= 100.0


# ── 6-7. carrier axis ────────────────────────────────────────────────────────

def test_carrier_axis_skips_when_liveness_unreadable(monkeypatch, tmp_path):
    """An unreadable liveness source must SKIP, never count.

    Counting a CLOSED Body's ref is the false positive g-115-6128's first draft
    fell into: that history is frozen by design, not unconsumed work. The skip
    must also be VISIBLE in the payload — a probe that declines to run and says
    nothing reports success by default."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_TICKS_TO_FILE", "1")
    monkeypatch.setenv("GIT_DRIFT_DISK_PCT", "100")
    monkeypatch.setattr(WD, "_live_body_sids", lambda _w: None)
    repo = _seed_repo(tmp_path)
    p = WD.GitDriftProbe(_Ctx(repo))
    result = p._carrier_depths(repo)
    assert result["skipped"] is True
    assert result["max_unconsumed"] is None
    assert result["reason"], "a skip with no recorded reason is an invisible silence"


def test_carrier_axis_counts_live_sids_only(monkeypatch, tmp_path):
    """A live Body's ref is counted; a closed Body's identically-shaped ref is not."""
    _clear_env(monkeypatch)
    repo = _seed_repo(tmp_path, ahead=3)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=60).stdout.strip()
    live_sid = "11111111-aaaa-4bbb-8ccc-000000000001"
    dead_sid = "22222222-aaaa-4bbb-8ccc-000000000002"
    for sid in (live_sid, dead_sid):
        _run(repo, "update-ref", f"refs/workers/testagent/{sid}", head)

    monkeypatch.setattr(WD, "_live_body_sids", lambda _w: {live_sid})
    p = WD.GitDriftProbe(_Ctx(repo))
    result = p._carrier_depths(repo)
    assert result["skipped"] is False
    sids = [r["sid"] for r in result["refs"]]
    assert sids == [live_sid], f"closed-body ref must be skipped, got {sids}"
    assert result["max_unconsumed"] == 3


# ── 8. episode dedup ─────────────────────────────────────────────────────────

def test_ticks_to_file_dedups_the_episode(monkeypatch, tmp_path):
    """The probe runs every iteration-close. A sustained breach must emit ONCE."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_AHEAD", "2")
    monkeypatch.setenv("GIT_DRIFT_TICKS_TO_FILE", "2")
    monkeypatch.setenv("GIT_DRIFT_DISK_PCT", "100")
    repo = _seed_repo(tmp_path, ahead=5)
    p = _probe(monkeypatch, repo)
    assert p.check() == [], "tick 1 is below ticks_to_file — must not fire yet"
    fired = p.check()
    assert len(fired) == 1, "tick 2 reaches ticks_to_file — must fire once"
    assert p.check() == [], "tick 3 is the same episode — must stay quiet"
    assert p.consecutive_breach == 3


# ── 9. the release path (guard-3419) ─────────────────────────────────────────

def test_clear_emits_cleared_and_releases_the_lease(monkeypatch, tmp_path):
    """A dedup keyed on open-goal existence is a LEASE. Without a release path it
    disables the detector permanently — MirrorWedgeProbe learned this with three
    goals open at 7, 14 and 17 days."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_AHEAD", "2")
    monkeypatch.setenv("GIT_DRIFT_TICKS_TO_FILE", "1")
    monkeypatch.setenv("GIT_DRIFT_DISK_PCT", "100")
    repo = _seed_repo(tmp_path, ahead=5)
    p = _probe(monkeypatch, repo)
    assert len(p.check()) == 1 and p.fired is True

    closes: list[bool] = []
    monkeypatch.setattr(p, "_close_drift_goal",
                        lambda: (closes.append(True) or {"attempted": True,
                                                         "closed": ["g-test-01"],
                                                         "held": [],
                                                         "detail": "closed g-test-01"}))
    monkeypatch.setenv("GIT_DRIFT_AHEAD", "500")  # condition resolves
    events = p.check()
    assert closes == [True], "the release path must run when the drift clears"
    assert len(events) == 1
    assert events[0].event == "git_drift_cleared"
    assert events[0].severity == "info"
    assert p.fired is False and p.consecutive_breach == 0


def test_close_path_is_not_gated_on_fired(monkeypatch, tmp_path):
    """`fired` lives in a box-local, ephemeral state file. A reset would make a
    goal filed by a previous episode unclosable forever, which is the same
    filed-never-closed defect one level up."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_DISK_PCT", "100")
    monkeypatch.setenv("GIT_DRIFT_AHEAD", "500")
    repo = _seed_repo(tmp_path, ahead=1)
    p = _probe(monkeypatch, repo, no_escalate=False)
    monkeypatch.setattr(p, "_close_drift_goal",
                        lambda: {"attempted": True, "closed": ["g-test-02"],
                                 "held": [], "detail": "closed g-test-02"})
    assert p.fired is False  # never fired in this process
    events = p.check()
    assert len(events) == 1 and events[0].event == "git_drift_cleared"


# ── 10. tick-mode state ──────────────────────────────────────────────────────

def test_state_round_trips(tmp_path):
    p = WD.GitDriftProbe(_Ctx(tmp_path))
    p.consecutive_breach, p.fired, p.last_fetch_ts = 4, True, 1234.5
    q = WD.GitDriftProbe(_Ctx(tmp_path))
    q.from_dict(p.to_dict())
    assert (q.consecutive_breach, q.fired, q.last_fetch_ts) == (4, True, 1234.5)


def test_from_dict_tolerates_garbage(tmp_path):
    p = WD.GitDriftProbe(_Ctx(tmp_path))
    p.from_dict({"consecutive_breach": None, "fired": None, "last_fetch_ts": "nope"})
    assert (p.consecutive_breach, p.fired, p.last_fetch_ts) == (0, False, 0.0)


# ── 11. registration ─────────────────────────────────────────────────────────

def test_registered_in_build_probes(tmp_path):
    ctx = WD.WatchdogContext(agent_name="testagent", agent_dir=tmp_path,
                             project_root_path=tmp_path)
    assert "git-drift" in {p.name for p in WD.build_probes(ctx)}


def test_registered_for_worker_bodies(tmp_path):
    """cc-07 (342 ahead) and cc-08 (223 unpushed) were not reducers. A worker has
    the same checkout, the same host disk, and pushes its own carrier ref."""
    assert "git-drift" in WD.WORKER_SAFE_PROBES
    ctx = WD.WatchdogContext(agent_name="testagent", agent_dir=tmp_path,
                             project_root_path=tmp_path, body_role="worker")
    assert "git-drift" in {p.name for p in WD.build_probes(ctx)}


def test_worker_safe_names_all_resolve(tmp_path):
    """A typo'd filter is indistinguishable from a working one at the call site —
    the hyphen-vs-underscore lesson that silently registered 1 probe of 5."""
    ctx = WD.WatchdogContext(agent_name="testagent", agent_dir=tmp_path,
                             project_root_path=tmp_path)
    real = {p.name for p in WD.build_probes(ctx)}
    assert WD.WORKER_SAFE_PROBES <= real, WD.WORKER_SAFE_PROBES - real


# ── 12. fail-open ────────────────────────────────────────────────────────────

def test_non_git_directory_is_silent(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_TICKS_TO_FILE", "1")
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    p = _probe(monkeypatch, plain)
    assert p.check() == []


# ---------------------------------------------------------------------------
# Disk breach requires BOTH a percentage over threshold AND an absolute free
# floor (2026-08-22). A bare percentage filed five HIGH goals in one hour for a
# 905 GiB volume with 169 GiB free.
# ---------------------------------------------------------------------------

def test_disk_free_floor_is_configured():
    cfg = WD._git_drift_config()
    assert cfg["disk_free_floor_gib"] == 15


def test_large_volume_over_pct_but_plenty_free_does_not_breach(monkeypatch, tmp_path):
    """THE 2026-08-22 FALSE ALARM, verbatim: 905 GiB at 81% = 169 GiB free.
    Five HIGH goals in one hour across four containers, every one correctly
    ignored. Drives the real probe -- an earlier draft of this test recomputed
    the threshold expression by hand, which asserts nothing about the code."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_DISK_PCT", "80")
    monkeypatch.setenv("GIT_DRIFT_TICKS_TO_FILE", "1")
    monkeypatch.setattr(WD, "_disk_used_pct", lambda _p: 81.0)
    monkeypatch.setattr(WD, "_disk_free_gib", lambda _p: 169.0)
    repo = _seed_repo(tmp_path)
    p = _probe(monkeypatch, repo)
    assert p.check() == []


def test_small_volume_over_pct_and_low_free_still_breaches(monkeypatch, tmp_path):
    """The case the percentage exists for: a 40 GiB root at 97% = 1.2 GiB free.
    The absolute floor must not cost us this."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_DISK_PCT", "80")
    monkeypatch.setenv("GIT_DRIFT_TICKS_TO_FILE", "1")
    monkeypatch.setattr(WD, "_disk_used_pct", lambda _p: 97.0)
    monkeypatch.setattr(WD, "_disk_free_gib", lambda _p: 1.2)
    repo = _seed_repo(tmp_path)
    events = _probe(monkeypatch, repo).check()
    assert len(events) == 1
    assert any("1.2GiB free" in b for b in events[0].payload["breaches"])


def test_unreadable_free_lets_percentage_speak_alone(monkeypatch, tmp_path):
    """None free must not SUPPRESS -- it cannot confirm headroom, so the
    percentage is allowed to fire on its own."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("GIT_DRIFT_DISK_PCT", "80")
    monkeypatch.setenv("GIT_DRIFT_TICKS_TO_FILE", "1")
    monkeypatch.setattr(WD, "_disk_used_pct", lambda _p: 95.0)
    monkeypatch.setattr(WD, "_disk_free_gib", lambda _p: None)
    repo = _seed_repo(tmp_path)
    assert len(_probe(monkeypatch, repo).check()) == 1


def test_disk_free_gib_reads_real_filesystem(tmp_path):
    got = WD._disk_free_gib(tmp_path)
    assert got is not None and got > 0


def test_disk_free_gib_returns_none_on_error(monkeypatch):
    monkeypatch.setattr(WD.shutil, "disk_usage",
                        lambda p: (_ for _ in ()).throw(OSError("gone")))
    assert WD._disk_free_gib(Path(".")) is None
