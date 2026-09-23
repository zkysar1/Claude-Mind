""" — the spawn guard must REFUSE rather than orphan a live daemon.

THE DEFECT. `__main__.py`'s spawn guard gates on `lifecycle.is_daemon_alive`,
which is PIDFILE-scoped: "Return True iff both PID + port files exist AND the PID
is alive." It therefore answers "is the REGISTERED daemon alive?", never "is ANY
daemon alive?". Whenever daemon.pid is missing, stale or unparsable BESIDE a
genuinely live daemon it returns False, the guard falls through, and
`clear_runtime_files()` -- the next statement -- erases the only pointer to that
process. The daemon keeps listening and keeps burning CPU, reachable by nothing.

Measured signatures this pins against:
  * a 17.8h orphan at 24% sustained CPU (4h17m46s CPU time), contending on
    world/aspirations.lock as lock-stale-break;
  * an orphan at 3.7 GB RSS that drove the box into memory pressure and got a
    close OOM-killed MID-VERIFY -- which then re-ran and inflated
    loop_state.goals_completed 65 -> 66 -> 67 for ONE real completion;
  * guard-6980: ten orphans over 18h holding ~4.25 GB against WSL's 9.9 GB.

WHY REFUSE AND NOT REAP. outcome 2 of the goal permits either. Refusing is the
branch the guardrails require -- guard-7162 (killing a live process is
destructive, unrevertable, and destroys the evidence that would have settled
whether it needed killing) and guard-1144 (never kill the live daemon while a
suite is running on the box). The deliberate reap already exists and is the
documented recovery in guard-6154 and guard-6980: `daemon-orphan-sweep.sh`, whose
keep-set spans every sibling deployment's published pair.

SELF-MATCH DISCIPLINE. The goal's own addendum (foxtrot-pgrep-selfmatch-20260919)
warns that a process census in THIS fix's test can match itself and report a
phantom duplicate -- "which in THIS goal's subject matter is the very condition
under investigation". Hence `test_census_ignores_a_line_that_merely_names_the_module`
and `test_census_excludes_self`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from mind_api.src import lifecycle  # noqa: E402


# ───────────────────────── the matching rule (pure) ─────────────────────────
#
# Driven with FIXED `ps` text. A live process table cannot produce a `py -3`
# launch or a self-match line on demand, and an always-empty matcher would pass a
# live-only assertion identically to a working one (guard-1715 / rb-245).

PS_FIXTURE = "\n".join([
    "   1 /sbin/init",
    " 100 python3 -m mind_api.src",              # a real daemon
    " 200 py -3 -m mind_api.src",                # real, and MISSED by the shipped
                                                 # pgrep -f 'python.* -m mind_api\\.src'
    " 300 grep -rn mind_api.src core/scripts",   # merely NAMES the module
    " 400 ps -eo pid=,args=",                    # the census command itself
    " 500 vim mind_api/src/__main__.py",         # an editor holding the file
    " 600 python3 -m mind_api.src --foreground",  # real, with trailing args
    " 700 python3 -m mind_api.srcfoo --x",       # a DIFFERENT module, prefix-sharing
])


def test_census_does_not_match_a_prefix_sharing_module():
    """`-m mind_api.srcfoo` must NOT match -- severity is inverted here.

    Found by the fresh-eyes pass on this very change and confirmed by probe: a
    bare `" -m mind_api.src" in args` substring test returned pid 700. Because
    this census REFUSES a start, ONE false positive refuses EVERY start on the
    box -- over-matching is an outage, while under-matching is only the orphan
    leak that already existed. The token must therefore end at end-of-string or
    whitespace.
    """
    assert 700 not in lifecycle.parse_ps_for_daemon_pids(PS_FIXTURE, keep=())


def test_census_finds_real_module_launches():
    """The positive control: the matcher must actually match."""
    assert lifecycle.parse_ps_for_daemon_pids(PS_FIXTURE, keep=()) == [100, 200, 600]


def test_census_catches_the_py_dash_3_launch_the_shipped_pgrep_misses():
    """`pgrep -f 'python.* -m mind_api\\.src'` requires argv to start with `python`.

    A `py -3 -m mind_api.src` daemon does not match it, so both shipped POSIX
    sweeps (_runtime.sh rt_sweep_orphan_daemons, mind-api-start.sh
    _sweep_orphan_daemons) are blind to it. Matching on the module token has no
    such gap; this pins that difference so a future "simplification" back to the
    pgrep pattern fails loudly.
    """
    assert 200 in lifecycle.parse_ps_for_daemon_pids(PS_FIXTURE, keep=())


def test_census_ignores_a_line_that_merely_names_the_module():
    """guard-1238 PHANTOM-ALIVE: a grep/editor/ps naming the module is not a daemon.

    If these matched, the guard would refuse EVERY legitimate start -- turning an
    orphan-prevention fix into a total outage.
    """
    found = lifecycle.parse_ps_for_daemon_pids(PS_FIXTURE, keep=())
    for phantom in (300, 400, 500):
        assert phantom not in found


def test_census_excludes_the_published_pair_and_self():
    """The live daemon and its parent are KEPT -- they are not orphans."""
    assert lifecycle.parse_ps_for_daemon_pids(PS_FIXTURE, keep={100, 600}) == [200]
    assert lifecycle.parse_ps_for_daemon_pids(PS_FIXTURE, keep={100, 200, 600}) == []


def test_census_empty_on_empty_input():
    assert lifecycle.parse_ps_for_daemon_pids("", keep=()) == []


# ───────────────────────── the live wiring (end-to-end) ─────────────────────
#
# guard-1943: a green suite certifies the FUNCTION, never the WIRING. These two
# exercise the real `ps` call against this box's actual process table.


def test_live_census_excludes_self():
    """This pytest process must never appear in its own census (self-match)."""
    with tempfile.TemporaryDirectory() as td:
        assert os.getpid() not in lifecycle.find_unreferenced_daemon_pids(Path(td))


def test_live_census_keeps_the_published_daemon():
    """Against the REAL project root, a correctly-published daemon is not an orphan.

    The load-bearing direction: a false positive here refuses every legitimate
    restart. Skips when no daemon is published, rather than asserting a zero that
    an empty population would satisfy anyway.
    """
    if not lifecycle.is_daemon_alive(PROJECT_ROOT):
        pytest.skip("no published live daemon on this box")
    published = lifecycle.read_pid(PROJECT_ROOT)
    assert published not in lifecycle.find_unreferenced_daemon_pids(PROJECT_ROOT)


# ───────────────────── the daemon.log decision record ───────────────────────


def test_log_event_writes_one_json_line_to_daemon_log():
    """outcome 2: the refusal decision must be recorded in daemon.log.

    Before this change every spawn-time decision NOT to start went to stderr --
    i.e. into the SPAWN log -- so an auditor reading daemon.log saw `started`
    events with unexplained gaps and no record of the decisions between them.
    """
    import json

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        line = lifecycle.log_event(
            root, "start-refused", "9.9.9",
            reason="unreferenced-daemon-alive", pids=[123, 456],
        )
        rec = json.loads(line)
        assert rec["event"] == "start-refused"
        assert rec["reason"] == "unreferenced-daemon-alive"
        assert rec["pids"] == [123, 456]
        assert rec["version"] == "9.9.9"
        assert "ts" in rec

        on_disk = lifecycle.daemon_log(root).read_text(encoding="utf-8").splitlines()
        assert on_disk == [line], "the returned line must be exactly what landed"


def test_log_event_never_raises_on_an_unwritable_log():
    """A lifecycle record must never be able to kill a daemon start."""
    unwritable = Path("/proc/self/mem-does-not-exist") / "nope"
    line = lifecycle.log_event(unwritable, "started", "9.9.9")
    assert '"event": "started"' in line


def test_started_and_refused_share_one_record_shape():
    """server.py delegates to lifecycle.log_event, so the two writers cannot drift.

    g-115-10336 introduced the second writer (__main__.py's refusal records).
    Pinning the shared key set is what stops a future edit to one of them from
    silently producing a daemon.log an auditor cannot parse uniformly.
    """
    import json

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        started = json.loads(lifecycle.log_event(root, "started", "1.0", port=1, pid=2))
        refused = json.loads(lifecycle.log_event(root, "start-refused", "1.0", reason="x"))
        for common in ("ts", "event", "version"):
            assert common in started and common in refused


def test_server_log_lifecycle_delegates_to_lifecycle_log_event():
    """The delegation is the SSOT claim -- pin it rather than trusting the comment."""
    src = (PROJECT_ROOT / "mind_api" / "src" / "server.py").read_text(encoding="utf-8")
    body = src.split("def _log_lifecycle", 1)[1].split("\n    def ", 1)[0]
    assert "lifecycle.log_event(" in body, "server.py must not re-implement the shape"
    assert "json.dumps" not in body, "a second record shape has reappeared"
