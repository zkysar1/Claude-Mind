"""The four  liveness fixtures for runner-dead-check.sh (recovery-gate
Path A's 6-condition gate) and runner-liveness-evidence.sh, inside a
physical-copy sandbox root.

  no-heartbeat        an ABSENT heartbeat file is INERT — it cannot contribute
                      to a kill on its own (rc=1 alive), and it counts only
                      beside a positive death signal (rc=0 dead).
  rate-limited-alive  every "dead" absence condition holds (stale heartbeat,
                      stale diary, no block, no stop, no jobs) but a life
                      signal exists — the multi-hour provider backoff shape
                      that killed live loops on 2026-09-01. Must SURVIVE.
  genuine-zombie      the positive control: the same absences with no life
                      evidence still recovers (rc=0), so the veto did not
                      turn the gate off.
  verdict-record      recovery-gate's `_recovery_log_entry` emits one durable
                      JSON row with every field the incident lacked; the
                      empty-log shape is pinned.

Also: heartbeat-stale.sh's three-way vocabulary (fresh / stale / absent).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
CORE = SCRIPTS.parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
from _bash_helpers import BASH  # noqa: E402

AGENT = "alpha"
SID = "reducer-sid-0001"


def _copy_core(dest_root: Path) -> None:
    (dest_root / "core" / "logs").mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "tests")
    for name in ("scripts", "config"):
        shutil.copytree(CORE / name, dest_root / "core" / name, ignore=ignore, symlinks=False)


def _set_mtime(p: Path, minutes_ago: float) -> None:
    if not p.exists():
        p.write_text("", encoding="utf-8")
    t = (datetime.now() - timedelta(minutes=minutes_ago)).timestamp()
    os.utime(p, (t, t))


def _sandbox(tmp_path: Path, *, heartbeat_minutes=None, diary_minutes=60.0,
             state="RUNNING", running_sid=SID) -> tuple[Path, Path]:
    """A RUNNING reducer whose diary is stale; heartbeat per `heartbeat_minutes`
    (None = absent). Every other absence condition holds by construction:
    no stop-hook log, no stop-requested, no background jobs."""
    root = tmp_path / "root"
    root.mkdir()
    _copy_core(root)
    world, meta = tmp_path / "world", tmp_path / "meta"
    world.mkdir()
    meta.mkdir()
    adir = root / "agents" / AGENT
    sess = adir / "session"
    sess.mkdir(parents=True)
    (adir / "local-paths.conf").write_text(
        f"WORLD_PATH={world.as_posix()}\nMETA_PATH={meta.as_posix()}\n", encoding="utf-8")
    (sess / "agent-state").write_text(state, encoding="utf-8")
    (sess / "agent-mode").write_text("autonomous", encoding="utf-8")
    if running_sid:
        (sess / "running-session-id").write_text(running_sid + "\n", encoding="utf-8")
    (sess / "execution-diary.jsonl").write_text('{"phase": "phase_start"}\n', encoding="utf-8")
    _set_mtime(sess / "execution-diary.jsonl", diary_minutes)
    if heartbeat_minutes is not None:
        _set_mtime(sess / "runner-heartbeat", heartbeat_minutes)
    return root, adir


def _env(root: Path, extra=None):
    env = dict(os.environ)
    env.update({"MIND_AGENT": AGENT, "STORAGE_BACKEND": "local", "RT_NO_AUTOSPAWN": "1",
                "RUNTIME_DIR": str(root / "rt"),
                # no Claude Code transcripts / zakcode docs / sidecar unless a test plants them
                "RUNNER_TRANSCRIPTS_DIR": str(root / "transcripts"),
                "ZAKCODE_HOME": str(root / "zakcode-home"),
                "SIDECAR_MARKER_FILE": str(root / "no-such-marker")})
    env.pop("MIND_SID", None)
    env.pop("PROVIDER_RETRY_LOG", None)
    env.pop("SIDECAR_HEALTH_URL", None)
    if extra:
        env.update(extra)
    return env


def _dead_check(root: Path, extra=None):
    r = subprocess.run([BASH, str(root / "core" / "scripts" / "runner-dead-check.sh")],
                       capture_output=True, text=True, env=_env(root, extra), timeout=240)
    # stdout is one (pretty-printed, multi-line) JSON object; parse it whole
    # and fall back to the first `{` so a stray prefix line cannot hide it.
    raw = r.stdout.strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = json.loads(raw[raw.index("{"):]) if "{" in raw else {}
    return r.returncode, data, r.stderr


def _plant_transcript(root: Path, minutes_ago: float) -> None:
    tdir = root / "transcripts"
    tdir.mkdir(exist_ok=True)
    ts = (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S")
    (tdir / f"{SID}.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": ts}) + "\n"
        + json.dumps({"type": "assistant", "timestamp": ts}) + "\n", encoding="utf-8")


# ───────────────────────── heartbeat-stale.sh vocabulary ─────────────────────────

def _hb(root: Path) -> str:
    r = subprocess.run([BASH, str(root / "core" / "scripts" / "heartbeat-stale.sh")],
                       capture_output=True, text=True, env=_env(root), timeout=120)
    return r.stdout.strip()


def test_heartbeat_stale_is_three_way(tmp_path):
    root, adir = _sandbox(tmp_path, heartbeat_minutes=None)
    assert _hb(root) == "absent"
    _set_mtime(adir / "session" / "runner-heartbeat", 0)
    assert _hb(root) == "fresh"
    _set_mtime(adir / "session" / "runner-heartbeat", 180)
    assert _hb(root) == "stale"


# ───────────────────────── no-heartbeat: inert ─────────────────────────

def test_no_heartbeat_cannot_contribute_to_a_kill_on_its_own(tmp_path):
    root, _ = _sandbox(tmp_path, heartbeat_minutes=None)
    rc, d, err = _dead_check(root)
    assert rc == 1, (d, err)
    assert d["heartbeat"] == "absent"
    assert d["conditions"]["heartbeat_stale"] is False
    assert "INERT" in d["messages"]["heartbeat_stale"]
    assert d["dead"] is False
    # every other absence condition DID hold — the absence alone is what saved it
    assert d["conditions"]["state_running"] and d["conditions"]["diary_stale"]
    assert d["conditions"]["no_recent_block"] and d["conditions"]["no_stop_requested"]
    assert d["conditions"]["no_background_jobs"]


def test_no_heartbeat_counts_beside_a_positive_death_signal(tmp_path):
    root, _ = _sandbox(tmp_path, heartbeat_minutes=None)
    _plant_transcript(root, minutes_ago=180)  # last assistant turn 3h ago
    rc, d, err = _dead_check(root)
    assert rc == 0, (d, err)
    assert d["heartbeat"] == "absent"
    assert d["conditions"]["heartbeat_stale"] is True
    assert "positive death signal" in d["messages"]["heartbeat_stale"]
    assert d["life_evidence"]["verdict"] == "dead"
    assert "assistant_turn_stale" in d["life_evidence"]["death"]


# ───────────────────────── rate-limited-alive: survives ─────────────────────────

def test_rate_limited_alive_survives_on_provider_retry_activity(tmp_path):
    root, _ = _sandbox(tmp_path, heartbeat_minutes=180)
    plog = root / "provider.log"
    plog.write_text("2026-09-01 06:58:10 WARNING rate limit hit, retrying in 1800s\n", encoding="utf-8")
    _set_mtime(plog, 5)
    rc, d, err = _dead_check(root, {"PROVIDER_RETRY_LOG": str(plog)})
    assert rc == 1, (d, err)
    assert d["dead"] is False
    assert d["conditions"]["heartbeat_stale"] is True, "the stale heartbeat still counts — it is the veto that saves the loop"
    assert d["conditions"]["no_life_evidence"] is False
    assert "vetoed" in d["messages"]["no_life_evidence"]
    assert d["life_evidence"]["verdict"] == "alive"
    assert "provider_retry_activity" in d["life_evidence"]["life"]


def test_rate_limited_alive_survives_on_a_recent_assistant_turn(tmp_path):
    root, _ = _sandbox(tmp_path, heartbeat_minutes=180)
    _plant_transcript(root, minutes_ago=2)
    rc, d, err = _dead_check(root)
    assert rc == 1, (d, err)
    assert d["conditions"]["no_life_evidence"] is False
    assert "recent_assistant_turn" in d["life_evidence"]["life"]


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="runner-proc liveness needs /proc")
def test_rate_limited_alive_survives_on_a_live_runner_proc_stamp(tmp_path):
    root, adir = _sandbox(tmp_path, heartbeat_minutes=180)
    pid = os.getpid()
    start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    (adir / "session" / "runner-proc").write_text(f"{pid}:{start}\n", encoding="utf-8")
    rc, d, err = _dead_check(root)
    assert rc == 1, (d, err)
    assert "runner_proc_alive" in d["life_evidence"]["life"]


# ───────────────────────── genuine zombie: positive control ─────────────────────────

def test_genuine_zombie_still_recovers(tmp_path):
    root, _ = _sandbox(tmp_path, heartbeat_minutes=180)
    rc, d, err = _dead_check(root)
    assert rc == 0, (d, err)
    assert d["dead"] is True
    assert d["conditions"]["no_life_evidence"] is True
    assert d["life_evidence"]["verdict"] == "unknown"
    assert "[5]" in err


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="runner-proc liveness needs /proc")
def test_genuine_zombie_with_a_dead_runner_proc_stamp(tmp_path):
    root, adir = _sandbox(tmp_path, heartbeat_minutes=None)
    (adir / "session" / "runner-proc").write_text("999999:1\n", encoding="utf-8")
    rc, d, err = _dead_check(root)
    assert rc == 0, (d, err)
    assert "runner_proc_dead" in d["life_evidence"]["death"]


def test_the_re_check_only_runs_when_every_absence_condition_holds(tmp_path):
    root, _ = _sandbox(tmp_path, heartbeat_minutes=0)  # fresh heartbeat -> alive at condition 2
    rc, d, err = _dead_check(root)
    assert rc == 1
    assert d["life_evidence"] is None
    assert d["conditions"]["no_life_evidence"] is False


# ───────────────────────── durable verdict record ─────────────────────────

def _function_source(name: str) -> str:
    """The bash function `name() {` ... first line that is exactly `}` — extracted
    in Python, not with a sed range: the function's inline python block ends
    with a `}))'` line at column 0, which a `/^}/` range terminator would
    mistake for the function's closing brace."""
    lines = (SCRIPTS / "recovery-gate.sh").read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _log_entry(*args, env_extra=None, tmp_dir: Path | None = None):
    fn = _function_source("_recovery_log_entry")
    fpath = (tmp_dir or Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or ".")) / "_rle_fn.sh"
    fpath.write_text(fn, encoding="utf-8")
    # recovery-gate.sh sources _paths.sh before defining the function; that is
    # what puts a working python3 on PATH on a box whose bare `python3` is the
    # Store stub (CLAUDE.md "Python Invocation"). Mirror the production shape.
    script = 'source "$1"; source "$2"; shift 2; _recovery_log_entry "$@"'
    env = dict(os.environ)
    env.update(env_extra or {})
    r = subprocess.run([BASH, "-c", script, "bash", (SCRIPTS / "_paths.sh").as_posix(), fpath.as_posix(), *args],
                       capture_output=True, text=True, env=env, timeout=120)
    return r.returncode, r.stdout.strip()


def test_recovery_log_entry_carries_every_field_the_incident_lacked(tmp_path):
    rc, out = _log_entry("recover", "2026-09-01T07:01:33", AGENT, "crashed runner: six absences",
                         "sid-demoted", "A", json.dumps({"conditions": {"state_running": True}}),
                         env_extra={"SESSION_ID": "hook-sid-77", "SOURCE": "startup"}, tmp_dir=tmp_path)
    assert rc == 0 and out, out
    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert len(rows) == 1, "one firing = exactly one JSON row (the empty-log shape)"
    row = rows[0]
    assert set(row) == {"ts", "agent", "action", "path", "cause", "sid_recorded",
                        "acting_sid", "source", "evidence"}
    assert row["action"] == "recover" and row["path"] == "A"
    assert row["sid_recorded"] == "sid-demoted" and row["acting_sid"] == "hook-sid-77"
    assert row["evidence"]["conditions"]["state_running"] is True


def test_recovery_log_entry_keeps_malformed_evidence_as_text_and_empty_as_null(tmp_path):
    rc, out = _log_entry("suppressed", "2026-09-01T07:01:33", AGENT, "life evidence", "", "A", "{not json",
                         tmp_dir=tmp_path)
    row = json.loads(out)
    assert row["evidence"] == "{not json" and row["sid_recorded"] is None
    rc, out = _log_entry("self_heal", "2026-09-01T07:01:33", AGENT, "path B", "", "B", "", tmp_dir=tmp_path)
    assert json.loads(out)["evidence"] is None


def test_recovery_gate_logs_suppressed_firings_and_mirrors_the_marker():
    src = (SCRIPTS / "recovery-gate.sh").read_text(encoding="utf-8")
    assert "_recovery_log_entry suppressed" in src
    assert "Path A SUPPRESSED" in src
    assert 'agent_status.$agent.last_recovery' in src
    assert '_perform_recovery "$agent" "$cause" A "$rdc_json"' in src
    # Path C still requires the exact `stale` reading; `absent` must not satisfy it
    assert 'local path="${3:-?}"' in src and 'local evidence="${4:-}"' in src


def test_runner_dead_check_declares_the_re_check_and_absent_semantics():
    src = (SCRIPTS / "runner-dead-check.sh").read_text(encoding="utf-8")
    assert "runner-liveness-evidence.sh" in src
    assert '"no_life_evidence"' in src
    assert 'elif [[ "$hb" == "absent" ]]' in src
    # the Linux diary probe must not depend on the Windows launcher (the silent 999-min stale)
    assert "py -3 -c" not in src
    assert "py -3 -c" not in (SCRIPTS / "runner-recent-block.sh").read_text(encoding="utf-8")


def test_session_save_id_breadcrumb_guard_treats_absent_like_stale():
    src = (SCRIPTS / "session-save-id.sh").read_text(encoding="utf-8")
    assert '!= "fresh"' in src
