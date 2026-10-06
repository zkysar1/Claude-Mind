""" U15a: the scheduled orphan-collection tick for the composite goal-queue store.

`composite-gc-tick.sh` is the schedule for `composite_gc_runner.py` (U14), which had no
caller. The properties that earn their keep here are the ones whose failure is SILENT:

  * the tick never deletes. It passes no `--apply` and sets no flag of its own, so a
    runner that is ever given a delete flag in the environment still only observes. That
    is pinned two ways: statically (no `--apply` token on a code line, the runner started
    with no argument at all) and by running the real script against a stub runner that
    records its argv and the flags it inherited, with the delete flag exported.
    The static pin is MEANT to go red in the commit that arms deletion: that commit edits
    the `run_pass` function and must edit this file too, which makes the arming a
    reviewed, visible step instead of a one-line change.
  * a broken config reads as "off" and a missing block reads as "off", and neither may
    read as "on". A probe that FAILED must also say so on stderr, because the tick
    would otherwise stay dark forever and look exactly like `enabled: false`.
  * the cadence stamp, the lock and the backgrounding are the three things that keep the
    reducer's iteration close from waiting on, or piling up, GC passes.
  * the call site stays fail-open, argument-free and beside its sibling.
  * (U16) what the runner reports reaches the board once per condition: an anomaly or a delete is
    posted, the same condition is not posted again for a day, a clean completed pass ends the
    episode, a runner that cannot describe itself is itself an anomaly, and a post is remembered
    only when it came back with a message id. Section 8.

NOT pinned here, said plainly: that the tick fires inside a live reducer loop (the call
site is pinned by reading the file, because running `iteration-close.sh` in a test
would run the whole productivity check), and what the real runner does (its own file,
`test_composite_gc_runner_g358202.py`, covers `main([])`, its exit codes and the
`--apply` default). The integration was measured by hand: the tick over the real runner
on a box with both composite flags unset logged `verdict: inactive`, `mode: observe`,
`deleted: 0`, rc 0.

Run: py -3 -m pytest core/scripts/tests/test_composite_gc_tick_g358202.py -v
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from _runtime_bash import bash_cmd  # noqa: E402
import composite_gc_runner as R  # noqa: E402  (the tick consumes this module's final line, so the tests build it with its functions)

TICK = SCRIPTS / "composite-gc-tick.sh"
ITER_CLOSE = SCRIPTS / "iteration-close.sh"
RUNNER = SCRIPTS / "composite_gc_runner.py"
CONFIG = SCRIPTS.parent / "config" / "aspirations.yaml"

ENABLED_30 = "composite_gc_tick:\n  enabled: true\n  interval_minutes: 30\n"
LOCK_RECLAIM_S = 2 * 3600
# What bash says when a non-numeric value reaches `$(( ))` or `[ -ge ]`. Non-fatal in a script file, so only
# the stderr noise distinguishes "caught by the digit guard" from "survived by luck".
_BASH_ARITH_NOISE = ("value too great", "integer expression expected", "invalid arithmetic operator",
                     "invalid integer constant", "syntax error")

# The stand-in runner: records what it was started with, optionally waits for a gate
# file (so the backgrounding test is deterministic, not a race), prints N lines, and
# exits with STUB_RC.
STUB = '''\
import json, os, sys, time
with open(os.environ["STUB_OUT"] + ".argv", "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:],
                        "gc": os.environ.get("OWNCLOUD_COMPOSITE_GC"),
                        "stores": os.environ.get("OWNCLOUD_COMPOSITE_STORES")}) + "\\n")
if os.environ.get("STUB_IGNORE_TERM"):
    import signal
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
gate = os.environ.get("STUB_WAIT_FOR")
if gate:
    end = time.time() + float(os.environ.get("STUB_MAX_WAIT", "12"))
    while not os.path.exists(gate) and time.time() < end:
        time.sleep(0.05)
if os.environ.get("STUB_SELF_KILL"):
    os.kill(os.getpid(), 9)
for i in range(int(os.environ.get("STUB_LINES", "1"))):
    print(json.dumps({"verdict": os.environ.get("STUB_VERDICT", "stub-ok"), "n": i}))
if os.environ.get("STUB_LAST") is not None:
    print(os.environ["STUB_LAST"])
if os.environ.get("STUB_STDERR_AFTER"):
    sys.stdout.flush()
    print(os.environ["STUB_STDERR_AFTER"], file=sys.stderr)
    sys.stderr.flush()
if os.environ.get("STUB_HANG_AFTER"):
    sys.stdout.flush()
    time.sleep(float(os.environ["STUB_HANG_AFTER"]))
sys.exit(int(os.environ.get("STUB_RC", "0")))
'''

# The stand-in for board-post.sh ( U16). It is wired into EVERY run by `Box.run`, not only the
# routing tests: a tick that can post turns any test whose runner reports an anomaly into a production
# writer on an append-only channel unless the seam is on suite-wide (guard-1202). It records argv (one
# argument per line) and stdin per call, then answers like the real script: a message id on stdout.
#   POSTER_RC          exit with this code and print nothing (a failed post)
#   POSTER_PRINT       what to print on success (set empty = rc 0 but no message id, guard-5403)
#   POSTER_PRINT_FIRST print the id before honouring POSTER_RC (an id beside a failing exit is no success)
#   POSTER_WAIT_FOR    hold until this file exists (deterministic "the poster is slow")
POSTER = '''\
#!/usr/bin/env bash
d="${POSTER_DIR:?}"; mkdir -p "$d"
n=$(( $(ls "$d" | grep -c '\\.argv$') + 1 ))
printf '%s\\n' "$@" >"$d/$n.argv"
cat >"$d/$n.stdin"
if [ -n "${POSTER_WAIT_FOR:-}" ]; then
    end=$(( $(date +%s) + ${POSTER_MAX_WAIT:-12} ))
    while [ ! -e "$POSTER_WAIT_FOR" ] && [ "$(date +%s)" -lt "$end" ]; do sleep 0.05; done
fi
[ -z "${POSTER_PRINT_FIRST:-}" ] || echo "${POSTER_PRINT-msg-20261004-000000-stub-0001}"
[ -z "${POSTER_RC:-}" ] || { echo "poster stub: forced failure" >&2; exit "$POSTER_RC"; }
echo "${POSTER_PRINT-msg-20261004-000000-stub-0001}"
'''

_SCRUB = (
    "OWNCLOUD_COMPOSITE_STORES", "OWNCLOUD_COMPOSITE_GC", "STORAGE_S3_ENDPOINT_URL",
    "COMPOSITE_GC_TICK_RUNNER", "COMPOSITE_GC_TICK_CONFIG", "COMPOSITE_GC_TICK_STATE_DIR",
    "COMPOSITE_GC_TICK_SYNC", "MIND_AGENT", "MIND_AGENT_DIR", "MIND_SID",
    "STUB_RC", "STUB_LINES", "STUB_WAIT_FOR", "STUB_MAX_WAIT", "STUB_VERDICT", "STUB_LAST",
    "COMPOSITE_GC_TICK_POSTER", "POSTER_DIR", "POSTER_RC", "POSTER_PRINT", "POSTER_PRINT_FIRST", "POSTER_WAIT_FOR", "POSTER_MAX_WAIT",
    "COMPOSITE_GC_TICK_POST_TIMEOUT_S", "PYTHONIOENCODING",
    "STUB_IGNORE_TERM", "STUB_SELF_KILL", "STUB_HANG_AFTER", "COMPOSITE_GC_TICK_RUNNER_MAX_S", "COMPOSITE_GC_TICK_KILL_GRACE_S",
    "STUB_STDERR_AFTER",
)


class Box:
    """One staged root plus every seam, so a test states only what it varies.

    The script and `_paths.sh` are COPIED, never symlinked (guard-2534). World and meta are
    pinned to tmp dirs so a future direct write lands there (guard-2337), and the backend
    is pinned local (guard-955) even though the stub runner never touches a store.
    """

    def __init__(self, tmp_path, config=ENABLED_30):
        self.root = tmp_path / "root"
        scripts = self.root / "core" / "scripts"
        scripts.mkdir(parents=True)
        for name in ("composite-gc-tick.sh", "_paths.sh", "_platform.sh"):
            shutil.copy2(SCRIPTS / name, scripts / name)
        self.tick = scripts / "composite-gc-tick.sh"
        (self.root / "core" / "config").mkdir()
        shutil.copy2(CONFIG, self.root / "core" / "config" / "aspirations.yaml")
        self.state = tmp_path / "state"
        self.stub = tmp_path / "stub-runner.py"
        self.stub.write_text(STUB, encoding="utf-8")
        self.cfg = tmp_path / "aspirations.yaml"
        if config is not None:
            self.cfg.write_text(config, encoding="utf-8")
        self.out = tmp_path / "stub-out"
        self.poster = tmp_path / "stub-poster.sh"
        self.poster.write_text(POSTER, encoding="utf-8")
        self.poster_dir = tmp_path / "poster-calls"
        self.world = self.root / "world"
        self.meta = self.root / "meta"
        self.world.mkdir()
        self.meta.mkdir()
        self.stamp = self.state / ".composite-gc-tick-last-run"
        self.lock = self.state / ".composite-gc-tick-lock"
        self.log = self.state / "composite-gc-tick.log"
        self.post_state = self.state / ".composite-gc-tick-post"

    def run(self, *, sync=True, timeout=60, defaults=False, **extra):
        """`defaults=True` drops the runner, config and state-dir seams, so the script's own defaults decide."""
        env = os.environ.copy()
        for name in _SCRUB:
            env.pop(name, None)
        env.update({
            "STORAGE_BACKEND": "local",
            "MIND_WORLD": str(self.world),
            "MIND_META": str(self.meta),
            "STUB_OUT": str(self.out),
            "COMPOSITE_GC_TICK_POSTER": str(self.poster),
            "POSTER_DIR": str(self.poster_dir),
        })
        if not defaults:
            env.update({
                "COMPOSITE_GC_TICK_RUNNER": str(self.stub),
                "COMPOSITE_GC_TICK_CONFIG": str(self.cfg),
                "COMPOSITE_GC_TICK_STATE_DIR": str(self.state),
            })
        if sync:
            env["COMPOSITE_GC_TICK_SYNC"] = "1"
        env.update({k: str(v) for k, v in extra.items()})
        return subprocess.run(bash_cmd(self.tick), cwd=str(self.root), env=env,
                              capture_output=True, text=True, timeout=timeout)

    def calls(self):
        f = Path(str(self.out) + ".argv")
        if not f.exists():
            return []
        return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]

    def posts(self):
        """[(argv, stdin)] for every call the poster stub received, in order."""
        found, n = [], 1
        while (self.poster_dir / f"{n}.argv").exists():
            argv = (self.poster_dir / f"{n}.argv").read_text(encoding="utf-8").splitlines()
            found.append((argv, (self.poster_dir / f"{n}.stdin").read_text(encoding="utf-8")))
            n += 1
        return found

    def log_text(self):
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def age(self, path, seconds):
        """Date `path` `seconds` in the past (negative = in the future)."""
        t = time.time() - seconds
        os.utime(path, (t, t))


def _code_lines(path):
    """Non-comment lines of a shell file, as (1-based number, text)."""
    return [(i, ln) for i, ln in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if ln.strip() and not ln.lstrip().startswith("#")]


# ---- 1. the tick never deletes ----------------------------------------------

def test_the_tick_never_names_apply_outside_a_comment():
    hits = [(n, ln) for n, ln in _code_lines(TICK) if "--apply" in ln]
    assert not hits, f"composite-gc-tick.sh must not carry an --apply token: {hits}"


def test_the_runner_is_started_with_no_argument_at_all():
    """Exactly one line starts the runner, under the wall-clock bound, and nothing follows its path but the stderr merge."""
    starts = [(n, ln) for n, ln in _code_lines(TICK) if '"$RUNNER"' in ln]
    assert len(starts) == 1, starts
    assert re.fullmatch(r'\s*out="\$\(timeout -k "\$KILL_GRACE_S" "\$RUNNER_MAX_S" python3 "\$RUNNER" 2>&1\)"; rc=\$\?',
                        starts[0][1]), (
        f"the runner line changed shape: {starts[0][1]!r}. If this is the commit that arms "
        "deletion, update this test to pin the new condition (flip checklist, U15b).")


def test_the_delete_flag_in_the_environment_still_yields_no_apply(tmp_path):
    """The control for the two static pins: the flags ARE inherited, the argv is still empty."""
    box = Box(tmp_path)
    r = box.run(OWNCLOUD_COMPOSITE_GC="test-env", OWNCLOUD_COMPOSITE_STORES="test-env")
    assert r.returncode == 0, r.stderr
    calls = box.calls()
    assert len(calls) == 1
    assert calls[0]["gc"] == "test-env" and calls[0]["stores"] == "test-env", "the stub did not inherit the flags"
    assert calls[0]["argv"] == [], "the tick must start the runner with no argument, flags or not"


def test_the_tick_sets_no_flag_of_its_own(tmp_path):
    box = Box(tmp_path)
    assert box.run().returncode == 0
    calls = box.calls()
    assert len(calls) == 1 and calls[0]["gc"] is None and calls[0]["stores"] is None


# ---- 2. what it logs, and the fail-open exit ----------------------------------

def test_a_pass_logs_the_exit_code_the_agent_and_the_runners_line(tmp_path):
    box = Box(tmp_path)
    r = box.run(MIND_AGENT="zeta", STUB_VERDICT="inactive")
    assert r.returncode == 0, r.stderr
    log = box.log_text()
    assert re.search(r"^--- \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d composite-gc tick rc=0 agent=zeta elapsed=\d+s ---$", log, re.M), log
    assert '"verdict": "inactive"' in log
    assert box.stamp.exists(), "the cadence stamp must advance after a pass"
    assert not box.lock.exists(), "the lock must be released after a pass"


def test_an_unbound_tick_says_so_in_the_header(tmp_path):
    box = Box(tmp_path)
    assert box.run().returncode == 0
    assert "agent=unset" in box.log_text()


@pytest.mark.parametrize("rc", [1, 2])
def test_a_failing_runner_is_logged_and_never_fails_the_tick(tmp_path, rc):
    box = Box(tmp_path)
    r = box.run(STUB_RC=rc)
    assert r.returncode == 0, "the tick is fail-open: its caller must never see a failure"
    assert f"composite-gc tick rc={rc} " in box.log_text()
    assert box.stamp.exists(), "a runner that fails fast must be retried once per interval, not once per iteration"
    assert not box.lock.exists()
    assert box.run().returncode == 0 and len(box.calls()) == 1, "the stamp must gate the retry"


def test_a_missing_runner_is_visible_in_the_log(tmp_path):
    box = Box(tmp_path)
    box.stub.unlink()
    r = box.run()
    assert r.returncode == 0
    log = box.log_text()
    assert "composite-gc tick rc=2 " in log and "stub-runner.py" in log, log


def test_only_the_last_five_lines_of_runner_output_are_kept(tmp_path):
    box = Box(tmp_path)
    assert box.run(STUB_LINES=9).returncode == 0
    log = box.log_text()
    assert '"n": 8' in log and '"n": 4' in log
    assert '"n": 3' not in log, "output beyond the last five lines must be dropped"


# ---- 3. the cadence stamp -------------------------------------------------------

def test_a_second_call_inside_the_interval_is_gated(tmp_path):
    box = Box(tmp_path)
    assert box.run().returncode == 0
    assert box.run().returncode == 0
    assert len(box.calls()) == 1


def test_the_interval_boundary_holds_on_both_sides(tmp_path):
    """29 minutes old with a 30-minute interval is gated; 31 minutes old runs."""
    box = Box(tmp_path)
    box.state.mkdir()
    box.stamp.write_text("")
    box.age(box.stamp, 29 * 60)
    assert box.run().returncode == 0 and box.calls() == []
    box.age(box.stamp, 31 * 60)
    assert box.run().returncode == 0 and len(box.calls()) == 1


def test_a_stamp_dated_in_the_future_is_due(tmp_path):
    """Honouring it would stop the tick until the clock caught up (the runner treats its own the same way)."""
    box = Box(tmp_path)
    box.state.mkdir()
    box.stamp.write_text("")
    box.age(box.stamp, -3600)
    assert box.run().returncode == 0 and len(box.calls()) == 1


def test_the_configured_interval_is_honoured(tmp_path):
    box = Box(tmp_path, config="composite_gc_tick:\n  enabled: true\n  interval_minutes: 5\n")
    box.state.mkdir()
    box.stamp.write_text("")
    box.age(box.stamp, 4 * 60)
    assert box.run().returncode == 0 and box.calls() == []
    box.age(box.stamp, 6 * 60)
    assert box.run().returncode == 0 and len(box.calls()) == 1


def test_a_leading_zero_interval_is_read_as_decimal(tmp_path):
    """`08` is a string to YAML and an arithmetic error to bash unless forced to base 10."""
    box = Box(tmp_path, config="composite_gc_tick:\n  enabled: true\n  interval_minutes: 08\n")
    box.state.mkdir()
    box.stamp.write_text("")
    box.age(box.stamp, 7 * 60)
    r = box.run()
    assert r.returncode == 0 and box.calls() == [], r.stderr
    box.age(box.stamp, 9 * 60)
    r = box.run()
    assert r.returncode == 0 and len(box.calls()) == 1, r.stderr


@pytest.mark.parametrize("bad", ['"abc"', "0", "-5", "2.5", "null", "true", "''"])
def test_a_bad_interval_falls_back_to_thirty_minutes(tmp_path, bad):
    box = Box(tmp_path, config=f"composite_gc_tick:\n  enabled: true\n  interval_minutes: {bad}\n")
    box.state.mkdir()
    box.stamp.write_text("")
    box.age(box.stamp, 29 * 60)
    r = box.run()
    assert r.returncode == 0 and box.calls() == [], f"{bad}: a bad interval must read as 30, got a run ({r.stderr})"
    noise = [m for m in _BASH_ARITH_NOISE if m in r.stderr]
    assert not noise, f"{bad}: a bad interval must be caught BEFORE the arithmetic, not reported by it: {r.stderr!r}"
    box.age(box.stamp, 31 * 60)
    assert box.run().returncode == 0 and len(box.calls()) == 1


# ---- 4. the per-box lock --------------------------------------------------------

def test_a_fresh_lock_blocks_the_run(tmp_path):
    box = Box(tmp_path)
    box.lock.mkdir(parents=True)
    r = box.run()
    assert r.returncode == 0 and box.calls() == []
    assert box.lock.is_dir(), "a lock the tick did not take must be left alone"
    assert not box.stamp.exists()


def test_a_lock_just_inside_the_reclaim_age_still_blocks(tmp_path):
    box = Box(tmp_path)
    box.lock.mkdir(parents=True)
    box.age(box.lock, LOCK_RECLAIM_S - 100)
    assert box.run().returncode == 0 and box.calls() == []
    assert box.lock.is_dir()


def test_a_stale_lock_is_reclaimed_and_the_run_proceeds(tmp_path):
    box = Box(tmp_path)
    box.lock.mkdir(parents=True)
    box.age(box.lock, LOCK_RECLAIM_S + 100)
    r = box.run()
    assert r.returncode == 0 and len(box.calls()) == 1, r.stderr
    assert not box.lock.exists() and box.stamp.exists()


# ---- 5. the config probe is fail-safe and says when it failed ---------------------

QUIET_OFF = [
    pytest.param("composite_gc_tick:\n  enabled: false\n  interval_minutes: 30\n", id="disabled"),
    pytest.param("", id="empty-file"),
    pytest.param("other_block:\n  enabled: true\n", id="block-missing"),
    pytest.param('composite_gc_tick:\n  enabled: "true"\n', id="string-true"),
    pytest.param("composite_gc_tick:\n  enabled: 1\n", id="int-one"),
    pytest.param("composite_gc_tick:\n  interval_minutes: 30\n", id="enabled-key-missing"),
    pytest.param("composite_gc_tick:\n", id="block-null"),
]
LOUD_OFF = [
    pytest.param(None, id="file-missing"),
    pytest.param("a: [unclosed\n", id="yaml-error"),
    pytest.param("- a\n- b\n", id="top-level-list"),
    pytest.param("composite_gc_tick: true\n", id="block-not-a-mapping"),
]


@pytest.mark.parametrize("config", QUIET_OFF)
def test_a_disabled_or_absent_block_spawns_nothing_quietly(tmp_path, config):
    box = Box(tmp_path, config=config)
    r = box.run()
    assert r.returncode == 0
    assert box.calls() == [] and not box.stamp.exists() and not box.lock.exists()
    assert "config probe failed" not in r.stderr, "an intentional off must not read as a failure"


@pytest.mark.parametrize("config", LOUD_OFF)
def test_an_unreadable_config_spawns_nothing_and_says_so_on_stderr(tmp_path, config):
    box = Box(tmp_path, config=config)
    r = box.run()
    assert r.returncode == 0
    assert box.calls() == [] and not box.stamp.exists() and not box.lock.exists()
    assert "composite-gc-tick: config probe failed, tick disabled" in r.stderr, r.stderr


def test_the_script_finds_its_own_runner_config_and_log_dir_by_default(tmp_path):
    """Every other test names its paths through a seam; this one names none.

    Production sets no seam, so the defaults ARE the wiring: the runner beside the script, the
    config at core/config/aspirations.yaml under the root `_paths.sh` resolves, and the log,
    stamp and lock under that root's core/logs.
    """
    box = Box(tmp_path)
    shutil.copy2(box.stub, box.root / "core" / "scripts" / "composite_gc_runner.py")
    r = box.run(defaults=True)
    assert r.returncode == 0, r.stderr
    assert len(box.calls()) == 1, f"the default runner/config did not resolve (stderr: {r.stderr!r})"
    logs = box.root / "core" / "logs"
    assert (logs / "composite-gc-tick.log").is_file() and (logs / ".composite-gc-tick-last-run").is_file()
    assert not (logs / ".composite-gc-tick-lock").exists()


def test_a_python_that_cannot_import_yaml_disables_the_tick_loudly(tmp_path):
    """The failure that happens BEFORE the probe's own try block: an ImportError.

    It must read as disabled (the shell fallback), with the traceback on stderr, never as enabled.
    """
    fake = tmp_path / "fakeyaml"
    fake.mkdir()
    (fake / "yaml.py").write_text('raise ImportError("no yaml in this python")\n', encoding="utf-8")
    box = Box(tmp_path)
    r = box.run(PYTHONPATH=str(fake))
    assert r.returncode == 0
    assert box.calls() == [] and not box.stamp.exists()
    assert "no yaml in this python" in r.stderr, r.stderr


def test_the_shipped_config_enables_the_tick(tmp_path):
    """The positive control for every 'off' case above, and the guard against going dark by accident.

    It runs the tick's OWN probe over the REAL config file. If PyYAML were missing from
    `python3`, or the block were deleted, or `enabled` were no longer a literal true, the
    tick would sit dark exactly like `enabled: false`: nothing else would notice.
    """
    box = Box(tmp_path)
    r = box.run(COMPOSITE_GC_TICK_CONFIG=str(CONFIG))
    assert r.returncode == 0, r.stderr
    assert len(box.calls()) == 1, "the shipped config must enable the tick (stderr: %r)" % r.stderr


def test_the_config_block_carries_only_the_two_documented_keys():
    blk = yaml.safe_load(CONFIG.read_text(encoding="utf-8")).get("composite_gc_tick")
    assert isinstance(blk, dict), blk
    assert set(blk) == {"enabled", "interval_minutes"}, f"a new key here needs a reader and a test: {sorted(blk)}"
    assert blk["enabled"] is True
    assert isinstance(blk["interval_minutes"], int) and not isinstance(blk["interval_minutes"], bool)
    assert blk["interval_minutes"] >= 1


# ---- 6. backgrounded by default ---------------------------------------------------

def test_the_default_mode_returns_before_the_runner_finishes(tmp_path):
    """The runner waits on a gate the test opens AFTER the tick returned, so a blocking tick can't pass."""
    box = Box(tmp_path)
    gate = tmp_path / "release"
    r = box.run(sync=False, timeout=8, STUB_WAIT_FOR=gate, STUB_MAX_WAIT=12)
    assert r.returncode == 0, r.stderr
    assert box.lock.is_dir(), "the lock must be held while the backgrounded run is in flight"
    assert not box.stamp.exists(), "the stamp advances only when the run ends"
    gate.write_text("go")
    deadline = time.time() + 10
    while time.time() < deadline and (box.lock.exists() or not box.stamp.exists()):
        time.sleep(0.1)
    assert box.stamp.exists() and not box.lock.exists(), "the backgrounded run never finished"
    assert "composite-gc tick rc=0 " in box.log_text()
    assert len(box.calls()) == 1


def test_a_reclaimed_lock_is_held_again_while_the_run_is_in_flight(tmp_path):
    """Reclaiming a stale lock must RE-TAKE it: otherwise the next iteration starts a second run."""
    box = Box(tmp_path)
    box.lock.mkdir(parents=True)
    box.age(box.lock, LOCK_RECLAIM_S + 100)
    gate = tmp_path / "release"
    r = box.run(sync=False, timeout=8, STUB_WAIT_FOR=gate, STUB_MAX_WAIT=12)
    assert r.returncode == 0, r.stderr
    assert box.lock.is_dir() and time.time() - box.lock.stat().st_mtime < 60, "the lock must be freshly re-taken"
    gate.write_text("go")
    deadline = time.time() + 10
    while time.time() < deadline and (box.lock.exists() or not box.stamp.exists()):
        time.sleep(0.1)
    assert box.stamp.exists() and not box.lock.exists()


# ---- 7. the call site -------------------------------------------------------------

def _enclosing_function(lines, index):
    for i in range(index, -1, -1):
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{", lines[i])
        if m:
            return m.group(1)
    return None


def test_the_tick_script_and_its_default_runner_exist():
    assert TICK.is_file() and RUNNER.is_file(), "the wrapper's default runner must be the U14 module"


def test_iteration_close_calls_the_tick_fail_open_and_argument_free():
    text = ITER_CLOSE.read_text(encoding="utf-8")
    calls = [(n, ln) for n, ln in _code_lines(ITER_CLOSE) if "composite-gc-tick.sh" in ln]
    assert len(calls) == 1, calls
    n, line = calls[0]
    assert line.strip() == 'bash "$SCRIPT_DIR/composite-gc-tick.sh" \\', f"the call passes an argument or changed shape: {line!r}"
    nxt = text.splitlines()[n]
    assert nxt.strip() == '>>"$CORE_ROOT/logs/iteration-close-stderr.log" 2>&1 || true', (
        f"the call must stay fail-open with the stderr sink: {nxt!r}")
    assert "COMPOSITE_GC_TICK" not in text, "the test seams must not leak into the production call site"


def test_the_call_sits_in_the_same_phase_after_the_eviction_tick():
    """Anti-vacuity twin: deleting the sibling's call or moving ours elsewhere must be noticed."""
    lines = ITER_CLOSE.read_text(encoding="utf-8").splitlines()
    ours = [i for i, ln in enumerate(lines) if 'composite-gc-tick.sh"' in ln and not ln.lstrip().startswith("#")]
    sib = [i for i, ln in enumerate(lines) if 'aspirations-evict-tick.sh"' in ln and not ln.lstrip().startswith("#")]
    assert len(ours) == 1 and len(sib) == 1, (ours, sib)
    assert sib[0] < ours[0], "the composite tick must come after the eviction tick it sits beside"
    assert ours[0] - sib[0] < 20, "the two ticks should stay adjacent"
    assert _enclosing_function(lines, ours[0]) == _enclosing_function(lines, sib[0]) is not None


# ---- 8. anomaly routing (U16) -----------------------------------------------------
#
# The tick posts what the runner reports, once per condition, and nothing else. Every property
# below fails SILENTLY when it breaks: a missing post reads as a quiet pass, a repeated post as
# a busy one, and a post remembered after it failed as one that was delivered.

def runner_line(verdict, anomalies=(), deleted=(), apply=False):
    """The runner's own final line for a result of this shape, built with its functions and not typed by hand."""
    res = R._new_result(apply, 1_760_000_000.0)
    res.update(verdict=verdict, anomalies=list(anomalies), deleted=list(deleted), run_id="run-1" if deleted else None)
    res["post"] = R.build_post(res)
    return json.dumps(R.summary(res), sort_keys=True)


ANOMALY = ["state-write-failed: boom"]


def _again(box, line, rc, **extra):
    """One more pass: age the spawn stamp past the interval, then run the tick over a runner that ends on `line`."""
    if box.stamp.exists():
        box.age(box.stamp, 31 * 60)
    env = {"STUB_LINES": 0, "STUB_LAST": line, "STUB_RC": rc}
    env.update(extra)
    r = box.run(**env)
    assert r.returncode == 0, r.stderr
    return r


def test_an_anomaly_is_posted_once_to_coordination_as_an_escalation(tmp_path):
    box = Box(tmp_path)
    _again(box, runner_line("observed", ANOMALY), 1)
    posts = box.posts()
    assert len(posts) == 1, posts
    argv, body = posts[0]
    assert argv == ["--channel", "coordination", "--type", "escalation", "--tags", "composite-gc,anomaly"], argv
    assert f"composite GC anomaly on {R.STORE_REL}: observed" in body and "- state-write-failed: boom" in body
    assert "Nothing is filed for this post" in body, "the footer must ride with the runner's own line"
    assert re.fullmatch(r"[0-9a-f]{12}", box.post_state.read_text(encoding="utf-8").strip())
    assert re.search(r"composite-gc route: posted anomaly to coordination as msg-\S+ key=[0-9a-f]{12} \(", box.log_text())


def test_a_delete_is_posted_as_a_finding(tmp_path):
    box = Box(tmp_path)
    _again(box, runner_line("applied", deleted=["a", "b"], apply=True), 0)
    posts = box.posts()
    assert len(posts) == 1, posts
    argv, body = posts[0]
    assert argv == ["--channel", "coordination", "--type", "finding", "--tags", "composite-gc,deleted"], argv
    assert "deleted 2 orphan segment object(s) in run run-1" in body


def test_the_types_and_channel_it_posts_with_are_the_documented_ones():
    """The board validates neither, and a typo in either is permanent (board.md)."""
    doc = (SCRIPTS.parent / "config" / "conventions" / "board.md").read_text(encoding="utf-8")
    for kind in ("escalation", "finding"):
        assert f"| `{kind}` |" in doc, kind
    assert "coordination" in doc


@pytest.mark.parametrize("verdict", ["observed", "applied", "inactive", "not-due", "lease-held", "busy",
                                     "not-yet-composite", "not-own-cloud"])
def test_a_pass_with_nothing_to_report_posts_and_logs_nothing(tmp_path, verdict):
    box = Box(tmp_path)
    _again(box, runner_line(verdict), 0)
    assert box.posts() == [] and not box.post_state.exists()
    assert "composite-gc route" not in box.log_text(), box.log_text()


def test_the_same_condition_is_not_posted_twice(tmp_path):
    box = Box(tmp_path)
    line = runner_line("observed", ANOMALY)
    _again(box, line, 1)
    _again(box, line, 1)
    assert len(box.posts()) == 1
    assert "composite-gc route: deduplicated anomaly" in box.log_text()


@pytest.mark.parametrize("second", [
    pytest.param(("observed", ["ledger-write-failed: boom"]), id="other-anomaly"),
    pytest.param(("observed", ANOMALY + ["ledger-write-failed: boom"]), id="one-more-anomaly"),
    pytest.param(("applied", ANOMALY), id="other-verdict"),
])
def test_a_different_condition_is_posted_again(tmp_path, second):
    box = Box(tmp_path)
    _again(box, runner_line("observed", ANOMALY), 1)
    _again(box, runner_line(second[0], second[1]), 1)
    assert len(box.posts()) == 2


def test_a_changing_count_id_or_error_text_is_the_same_condition(tmp_path):
    """rb-2954: a key built from a timestamp, a count or an exception text never matches the next pass."""
    box = Box(tmp_path)
    first = ["kept 3 object(s): gone-since-listing",
             "the cadence stamp is in the future (2026-01-01T00:00:00): treated as due and overwritten"]
    later = ["the cadence stamp is in the future (2027-05-05T10:11:12): treated as due and overwritten",
             "kept 17 object(s): rewritten-since-listing"]
    _again(box, runner_line("error: KeyError", first), 1)
    _again(box, runner_line("error: ValueError", later), 1)
    assert len(box.posts()) == 1, "order, digits, a verdict's detail and trailing text must not split a condition"


def test_a_delete_post_is_deduplicated_like_an_anomaly(tmp_path):
    box = Box(tmp_path)
    line = runner_line("applied", deleted=["a"], apply=True)
    _again(box, line, 0)
    _again(box, line, 0)
    assert len(box.posts()) == 1


@pytest.mark.parametrize("hours,reposts", [(23, False), (25, True)])
def test_the_repost_window_is_a_day_and_holds_on_both_sides(tmp_path, hours, reposts):
    box = Box(tmp_path)
    line = runner_line("observed", ANOMALY)
    _again(box, line, 1)
    box.age(box.post_state, hours * 3600)
    _again(box, line, 1)
    assert len(box.posts()) == (2 if reposts else 1)


def test_a_state_dated_in_the_future_is_due(tmp_path):
    box = Box(tmp_path)
    line = runner_line("observed", ANOMALY)
    _again(box, line, 1)
    box.age(box.post_state, -3600)
    _again(box, line, 1)
    assert len(box.posts()) == 2, "honouring a future-dated state would silence the condition until the clock caught up"


def test_a_clean_completed_pass_ends_the_episode(tmp_path):
    box = Box(tmp_path)
    line = runner_line("observed", ANOMALY)
    _again(box, line, 1)
    _again(box, runner_line("observed"), 0)
    assert not box.post_state.exists()
    assert "composite-gc route: clean pass, the episode is over" in box.log_text()
    _again(box, line, 1)
    assert len(box.posts()) == 2, "a recurrence after a clean pass is a new episode"


@pytest.mark.parametrize("verdict", ["not-due", "lease-held", "inactive", "busy", "not-yet-composite", "not-own-cloud"])
def test_a_pass_that_did_not_run_does_not_end_the_episode(tmp_path, verdict):
    box = Box(tmp_path)
    line = runner_line("observed", ANOMALY)
    _again(box, line, 1)
    _again(box, runner_line(verdict), 0)
    assert box.post_state.exists(), f"a {verdict} pass says nothing about whether the condition went away"
    _again(box, line, 1)
    assert len(box.posts()) == 1


def test_a_nonzero_exit_with_no_post_is_an_anomaly(tmp_path):
    """The runner's setup-failed line carries no `post`; it would otherwise reach only the log."""
    box = Box(tmp_path)
    _again(box, json.dumps({"verdict": "setup-failed", "error": "no backend"}), 2)
    posts = box.posts()
    assert len(posts) == 1, posts
    argv, body = posts[0]
    assert argv[:4] == ["--channel", "coordination", "--type", "escalation"]
    assert "rc=2" in body and "setup-failed" in body and "no backend" in body


@pytest.mark.parametrize("rc", [0, 1])
def test_a_runner_that_prints_no_result_line_is_an_anomaly(tmp_path, rc):
    box = Box(tmp_path)
    _again(box, "ImportError: No module named composite_x", rc)
    posts = box.posts()
    assert len(posts) == 1, posts
    assert f"no result line (rc={rc})" in posts[0][1] and "ImportError" in posts[0][1]


def test_a_runner_that_prints_nothing_is_an_anomaly(tmp_path):
    box = Box(tmp_path)
    assert box.run(STUB_LINES=0, STUB_RC=0).returncode == 0
    posts = box.posts()
    assert len(posts) == 1 and "no output" in posts[0][1], posts


def test_only_the_last_object_line_is_the_result(tmp_path):
    box = Box(tmp_path)
    _again(box, runner_line("observed", ANOMALY), 1, STUB_LINES=3)
    assert len(box.posts()) == 1, "lines before the result (warnings, the stub's own) must not hide it"
    other = Box(tmp_path / "other")
    _again(other, runner_line("observed", ANOMALY) + "\n" + runner_line("observed"), 0)
    assert other.posts() == [], "an anomaly on an EARLIER line is not the result"


def test_a_failed_post_is_not_remembered_and_is_retried(tmp_path):
    box = Box(tmp_path)
    line = runner_line("observed", ANOMALY)
    _again(box, line, 1, POSTER_RC=3)
    assert not box.post_state.exists() and "post FAILED (rc=3" in box.log_text()
    _again(box, line, 1)
    assert len(box.posts()) == 2 and box.post_state.exists()


@pytest.mark.parametrize("printed", ["", "ok", "msg-xyz", "posted"])
def test_an_answer_without_a_message_id_is_not_a_delivery(tmp_path, printed):
    """guard-5403: rc 0 with plausible output is exactly what a dropped post looks like."""
    box = Box(tmp_path)
    _again(box, runner_line("observed", ANOMALY), 1, POSTER_PRINT=printed)
    assert not box.post_state.exists()
    assert "post FAILED (rc=0, message id 'none')" in box.log_text()


def test_an_id_beside_a_failing_exit_is_not_a_delivery(tmp_path):
    box = Box(tmp_path)
    _again(box, runner_line("observed", ANOMALY), 1, POSTER_PRINT_FIRST=1, POSTER_RC=3)
    assert not box.post_state.exists() and "post FAILED (rc=3" in box.log_text()


def test_a_missing_poster_fails_open_and_visibly(tmp_path):
    box = Box(tmp_path)
    _again(box, runner_line("observed", ANOMALY), 1, COMPOSITE_GC_TICK_POSTER=tmp_path / "nope.sh")
    assert "post FAILED" in box.log_text() and not box.post_state.exists()
    assert box.stamp.exists() and not box.lock.exists()


def test_a_hung_poster_is_killed_at_the_timeout(tmp_path):
    """Without the bound a wedged board daemon leaks one waiting process per pass, forever."""
    box = Box(tmp_path)
    started = time.time()
    _again(box, runner_line("observed", ANOMALY), 1, POSTER_WAIT_FOR=tmp_path / "never", POSTER_MAX_WAIT=30,
           COMPOSITE_GC_TICK_POST_TIMEOUT_S=1)
    assert time.time() - started < 15, "the poster ran to its own limit: the timeout did not apply"
    assert "post FAILED (rc=124" in box.log_text() and not box.post_state.exists()


def test_a_slow_poster_holds_neither_the_lock_nor_the_stamp(tmp_path):
    """Routing runs after both are settled, so a poster in flight cannot stop the next tick or the cadence."""
    box = Box(tmp_path)
    gate = tmp_path / "release"
    r = box.run(sync=False, timeout=8, STUB_LINES=0, STUB_LAST=runner_line("observed", ANOMALY), STUB_RC=1,
                POSTER_WAIT_FOR=gate, POSTER_MAX_WAIT=12)
    assert r.returncode == 0, r.stderr
    deadline = time.time() + 10
    while time.time() < deadline and not (box.poster_dir / "1.argv").exists():
        time.sleep(0.05)
    assert (box.poster_dir / "1.argv").exists(), "the poster was never reached"
    assert box.stamp.exists() and not box.lock.exists(), "with the poster in flight the stamp and lock must already be settled"
    gate.write_text("go")
    deadline = time.time() + 10
    while time.time() < deadline and not box.post_state.exists():
        time.sleep(0.05)
    assert box.post_state.exists(), "the post never completed"


def test_a_non_ascii_anomaly_survives_an_ascii_stdout(tmp_path):
    box = Box(tmp_path)
    _again(box, runner_line("observed", ["state-write-failed: café → gone"]), 1, PYTHONIOENCODING="ascii")
    posts = box.posts()
    assert len(posts) == 1 and "café → gone" in posts[0][1], posts


def test_the_default_poster_is_board_post_sh_beside_the_tick():
    code = [ln for _, ln in _code_lines(TICK) if ln.startswith("POSTER=")]
    assert code == ['POSTER="${COMPOSITE_GC_TICK_POSTER:-$SCRIPT_DIR/board-post.sh}"'], code
    assert (SCRIPTS / "board-post.sh").is_file()


def test_the_real_runner_on_a_box_with_no_composite_flag_posts_nothing(tmp_path):
    """The integration the routing rests on: the REAL runner's real last line, both flags unset."""
    box = Box(tmp_path)
    r = box.run(COMPOSITE_GC_TICK_RUNNER=RUNNER)
    assert r.returncode == 0, r.stderr
    assert re.search(r'"verdict": "(inactive|not-own-cloud)"', box.log_text()), box.log_text()
    assert box.posts() == [], box.log_text()


# ---- 9. the key against the runner's real anomaly strings ---------------------------

def _runner_anomaly_formats():
    """Every string the runner appends to res["anomalies"], read from its source so a new site is picked up."""
    import ast
    found = []
    for node in ast.walk(ast.parse(RUNNER.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "append"
                and ast.unparse(node.func.value) == "res['anomalies']"):
            arg = node.args[0]
            fmt = arg.left if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Mod) else arg
            assert isinstance(fmt, ast.Constant) and isinstance(fmt.value, str), ast.unparse(arg)
            found.append(fmt.value)
    return found


# The one format whose lead phrase differs between one entry and several ("entry" / "entries"): a known,
# bounded exception, pinned so that fixing it forces this entry out.
UNSTABLE_LEAD = {"%d ledger entr%s dropped as invalid; the grace of each starts over"}


def _fill(fmt, number, word):
    return re.sub(r"%[ds]", lambda m: str(number if m.group() == "%d" else word), fmt)


def test_the_scan_finds_the_runners_anomaly_sites():
    """Anti-vacuity for the parametrized test below: an empty scan would collect no cases and pass."""
    formats = _runner_anomaly_formats()
    assert len(formats) >= 10, formats
    assert UNSTABLE_LEAD <= set(formats), "the known exception no longer matches a runner site: update UNSTABLE_LEAD"


@pytest.mark.parametrize("fmt", _runner_anomaly_formats(), ids=lambda f: f[:48])
def test_an_anomaly_the_runner_can_emit_keeps_one_key_while_its_details_change(tmp_path, fmt):
    """A new anomaly whose lead phrase carries an id or a count would be posted again at every pass."""
    box = Box(tmp_path)
    if fmt in UNSTABLE_LEAD:
        first, second = _fill(fmt, 1, "y"), _fill(fmt, 2, "ies")
    else:
        first, second = _fill(fmt, 3, "alpha-1"), _fill(fmt, 17, "beta (22): x y")
    _again(box, runner_line("observed", [first]), 1)
    _again(box, runner_line("observed", [second]), 1)
    assert len(box.posts()) == (2 if fmt in UNSTABLE_LEAD else 1), (first, second)


# ---- 10. the wall-clock bound (U17) -----------------------------------------------------
#
# The runner never renews its lease, so a runner older than the lease may be running beside a second
# holder. The tick starts it under `timeout -k` with a bound below the lease. A stopped runner cannot
# print its summary, so what has to hold is that it IS stopped, that the stop is announced as a condition
# of its own and not read as a crash, and that nothing downstream (the lock, the stamp) waits on it.

def _tick_default(name):
    m = re.search(r'^%s="\$\{[A-Z_]+:-(\d+)\}"' % name, TICK.read_text(encoding="utf-8"), re.M)
    assert m, f"no default for {name} found in the tick"
    return int(m.group(1))


def test_the_default_bound_and_its_grace_sit_below_the_runners_lease():
    bound, grace = _tick_default("RUNNER_MAX_S"), _tick_default("KILL_GRACE_S")
    assert bound > 0 and grace > 0, (bound, grace)
    # the runner must be dead well before another caller may take its lease; 120 s is one stalled S3 operation (3 x (10 + 30))
    assert bound + grace <= R.LEASE_TTL_S - 120, (bound, grace, R.LEASE_TTL_S)


def _stopped_run(box, tmp_path, **extra):
    """One pass over a runner held in its gate wait (it never finishes by itself), under a 1 s bound."""
    started = time.time()
    _again(box, runner_line("observed"), 0, STUB_WAIT_FOR=tmp_path / "never", STUB_MAX_WAIT=30,
           COMPOSITE_GC_TICK_RUNNER_MAX_S=1, **extra)
    assert time.time() - started < 15, "the runner ran to its own limit: the bound did not apply"


def test_a_runner_that_outlives_the_bound_is_stopped_and_announced(tmp_path):
    box = Box(tmp_path)
    _stopped_run(box, tmp_path)
    log = box.log_text()
    m = re.search(r"composite-gc tick rc=124 agent=\S+ elapsed=(\d+)s ---", log)
    assert m and int(m.group(1)) >= 1, log   # the seconds are measured, not a constant
    posts = box.posts()
    assert len(posts) == 1, posts
    argv, body = posts[0]
    assert argv == ["--channel", "coordination", "--type", "escalation", "--tags", "composite-gc,anomaly"], argv
    assert "wall-clock bound (1 s" in body and "(rc=124)" in body
    assert "cannot print its summary" in body and "do not re-run a delete pass" in body
    assert box.stamp.exists() and not box.lock.exists(), "a stopped runner must not leave the tick held"


def test_a_runner_that_ignores_the_stop_signal_is_killed_after_the_grace(tmp_path):
    box = Box(tmp_path)
    _stopped_run(box, tmp_path, STUB_IGNORE_TERM=1, COMPOSITE_GC_TICK_KILL_GRACE_S=1)
    assert re.search(r"composite-gc tick rc=137 ", box.log_text()), box.log_text()
    posts = box.posts()
    assert len(posts) == 1, posts
    assert "wall-clock bound (1 s" in posts[0][1] and "(rc=137)" in posts[0][1]


def test_a_kill_that_came_sooner_than_the_bound_is_not_blamed_on_it(tmp_path):
    """The control for the 137 rule: the OOM killer answers 137 too, and a post must not name the bound for it."""
    box = Box(tmp_path)
    _again(box, runner_line("observed"), 0, STUB_SELF_KILL=1)
    posts = box.posts()
    assert len(posts) == 1, posts
    body = posts[0][1]
    assert "(rc=137)" in body and "no result line" in body and "wall-clock bound" not in body, body


def test_a_runner_that_printed_its_result_and_then_hung_is_still_stopped(tmp_path):
    """Its own line is clean but the exit was the bound's: the hang must not read as a clean pass."""
    box = Box(tmp_path)
    started = time.time()
    _again(box, runner_line("observed"), 0, STUB_HANG_AFTER=30, COMPOSITE_GC_TICK_RUNNER_MAX_S=1)
    assert time.time() - started < 15, "the bound did not apply to a runner that hangs after printing"
    posts = box.posts()
    assert len(posts) == 1, posts
    assert "wall-clock bound (1 s" in posts[0][1] and "composite GC anomaly: observed" in posts[0][1]


def test_a_stop_is_its_own_condition_and_is_posted_once(tmp_path):
    box = Box(tmp_path)
    _stopped_run(box, tmp_path)
    _stopped_run(box, tmp_path)
    assert len(box.posts()) == 1, "the same stop inside 24 h is one condition"
    assert "composite-gc route: deduplicated anomaly" in box.log_text()
    _again(box, "ImportError: boom", 1)
    assert len(box.posts()) == 2, "a crash is a different condition from a stop and must not hide behind it"


# ---- 11. the result line among stderr lines (U19) ---------------------------------------
#
# The tick merges stderr into the runner's output (2>&1) because a crash's traceback is what an anomaly post
# carries. The cost of the merge is that a warning printed after the summary (an interpreter-exit message, a
# library's ResourceWarning) is the LAST line of the capture. The router reads the last line that opens a JSON
# object, so that warning neither turns a clean pass into a "no result line" anomaly nor hides a real
# anomaly's own post. The exit code is still read beside the line, so a crash after a summary still posts.

TRAILING_WARNING = "ResourceWarning: unclosed <ssl.SSLSocket fd=7>"
# an end-of-run notice is as likely to open with a bracket as with a word: only an OBJECT is a result
TRAILING_NOTICE = "[notice] A new release of pip is available: 24.0 -> 25.1"


@pytest.mark.parametrize("warning", [TRAILING_WARNING, TRAILING_NOTICE])
def test_a_warning_after_a_clean_result_still_ends_the_episode(tmp_path, warning):
    box = Box(tmp_path)
    _again(box, runner_line("observed", ANOMALY), 1)
    assert len(box.posts()) == 1 and box.post_state.exists()
    _again(box, runner_line("observed"), 0, STUB_STDERR_AFTER=warning)
    assert len(box.posts()) == 1, "a clean pass followed by a stderr warning must not post a second anomaly"
    assert not box.post_state.exists() and "clean pass, the episode is over" in box.log_text()


def test_a_warning_after_an_anomalous_result_does_not_hide_its_post(tmp_path):
    plain = Box(tmp_path / "plain")
    _again(plain, runner_line("observed", ANOMALY), 1)
    box = Box(tmp_path / "warned")
    _again(box, runner_line("observed", ANOMALY), 1, STUB_STDERR_AFTER=TRAILING_WARNING)
    posts = box.posts()
    assert len(posts) == 1 and len(plain.posts()) == 1, (posts, plain.posts())
    assert posts[0] == plain.posts()[0], "the runner's own post must reach the board unchanged"
    assert "no result line" not in posts[0][1]


def test_a_crash_after_a_clean_result_is_still_an_anomaly(tmp_path):
    """The exit code is read beside the line: a traceback after a clean summary is a crash and posts as one."""
    box = Box(tmp_path)
    _again(box, runner_line("observed"), 1, STUB_STDERR_AFTER="Traceback (most recent call last): boom")
    posts = box.posts()
    assert len(posts) == 1, posts
    assert "exited rc=1 with verdict observed and no post" in posts[0][1], posts[0][1]


def test_a_last_object_line_that_does_not_parse_is_no_result(tmp_path):
    """No scan back past it: a runner whose last object line is garbage did not print a summary this pass."""
    box = Box(tmp_path)
    _again(box, runner_line("observed") + "\n{not json", 0)
    posts = box.posts()
    assert len(posts) == 1 and "no result line (rc=0)" in posts[0][1], posts
