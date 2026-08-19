"""Tests for core/scripts/stop-reason-record.py ().

COMPLETION BAR (rb-7741) for the originating goal: "each path notification
proven by forcing the condition once (or a unit-level equivalent with the
transport mocked)". `test_notify_fires_for_every_stop_path` is that proof — it
drives all four deliberate-stop path names through record() with the transport
mocked and asserts a notification was actually attempted for each.

The transport is mocked rather than live for one reason worth stating: the real
consumer sends the user an email, and a test suite that mails a human on every
run gets its notifications muted, which would defeat the mechanism this goal
exists to build.
"""
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent


def _load():
    """Load the hyphenated CLI module (same pattern as its siblings)."""
    spec = importlib.util.spec_from_file_location(
        "stop_reason_record", SCRIPTS / "stop-reason-record.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["stop_reason_record"] = mod
    spec.loader.exec_module(mod)
    return mod


srr = _load()

# The four deliberate-stop paths named by the goal, plus the user-stop carve-out.
STOP_PATHS = (
    "productivity-stop-gate",
    "recovery-gate-zombie",
    "recovery-failed-permanent",
    "reducer-self-fence",
)


@pytest.fixture()
def agent_home(tmp_path, monkeypatch):
    """Hermetic agent dir — never touches the live agent's session state."""
    adir = tmp_path / "agents" / "testagent"
    (adir / "session").mkdir(parents=True)
    monkeypatch.setattr(srr._paths, "agent_dir", lambda name: tmp_path / "agents" / name)
    return adir


class _Sender:
    """Stand-in for the email transport. Records calls; never sends."""

    def __init__(self, status="sent", detail="ok"):
        self.calls = []
        self.status = status
        self.detail = detail

    def __call__(self, agent, subject, body, log):
        self.calls.append({"agent": agent, "subject": subject, "body": body})
        return self.status, self.detail


def _read(adir):
    return srr.parse_reason_file(
        (adir / "session" / srr.REASON_FILENAME).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- reason file

def test_reason_file_written_with_all_machine_readable_fields(agent_home):
    srr.record("reducer-self-fence", "lease held by another machine",
               "testagent", sender=_Sender())
    got = _read(agent_home)
    assert got["path"] == "reducer-self-fence"
    assert got["agent"] == "testagent"
    assert got["reason"] == "lease held by another machine"
    assert got["user_initiated"] == "0"
    assert got["notified"] == "sent"
    assert got["box"]
    datetime.strptime(got["stopped_at"], srr.TS_FMT)  # parseable or raises


def test_no_tmp_file_survives_the_atomic_write(agent_home):
    """guard-320: the write is .tmp + os.replace, so no partial file lingers."""
    srr.record("productivity-stop-gate", "score below floor", "testagent",
               sender=_Sender())
    leftovers = list((agent_home / "session").glob("*.tmp"))
    assert leftovers == [], f"non-atomic write left {leftovers}"


def test_reason_is_collapsed_to_one_line(agent_home):
    """A multi-line reason would corrupt the key=value format for the sweeper."""
    srr.record("recovery-gate-zombie", "line one\nline two\n\tline three",
               "testagent", sender=_Sender())
    body = (agent_home / "session" / srr.REASON_FILENAME).read_text(encoding="utf-8")
    reason_lines = [ln for ln in body.splitlines() if ln.startswith("reason=")]
    assert len(reason_lines) == 1
    assert reason_lines[0] == "reason=line one line two line three"


# ------------------------------------------------- the completion-bar proof

@pytest.mark.parametrize("path", STOP_PATHS)
def test_notify_fires_for_every_stop_path(agent_home, path):
    """COMPLETION BAR: every deliberate-stop path attempts a notification."""
    sender = _Sender()
    fields = srr.record(path, f"forced condition for {path}", "testagent",
                        sender=sender)
    assert len(sender.calls) == 1, f"{path} did not attempt a notification"
    assert fields["notified"] == "sent"
    assert _read(agent_home)["path"] == path


@pytest.mark.parametrize("path", STOP_PATHS)
def test_notification_names_the_agent_box_and_restart_command(agent_home, path):
    """The email must be actionable on its own — /start is the only way back."""
    sender = _Sender()
    srr.record(path, "some reason", "testagent", sender=sender)
    call = sender.calls[0]
    assert "testagent" in call["subject"]
    assert path in call["subject"]
    assert "/start testagent" in call["body"]
    assert "some reason" in call["body"]


def test_every_goal_named_path_is_in_the_closed_enum():
    """A caller typo must be refused, not silently written as an unbucketable value."""
    for path in STOP_PATHS:
        assert path in srr.VALID_PATHS


# ------------------------------------------------------- user-stop carve-out

def test_user_initiated_stop_records_but_never_emails(agent_home):
    """The user issued /stop — they already know. Reason file still written so
    the fleet sweeper reads EXPECTED-IDLE instead of alerting."""
    sender = _Sender()
    fields = srr.record("user-stop", "user ran /stop", "testagent",
                        user_initiated=True, sender=sender)
    assert sender.calls == []
    assert fields["notified"] == "skipped-user-initiated"
    assert _read(agent_home)["user_initiated"] == "1"


def test_no_notify_flag_still_records(agent_home):
    sender = _Sender()
    fields = srr.record("productivity-stop-gate", "r", "testagent",
                        notify=False, sender=sender)
    assert sender.calls == []
    assert fields["notified"] == "skipped-disabled"
    assert _read(agent_home)["path"] == "productivity-stop-gate"


# ------------------------------------------------------------------ throttle

def test_throttle_suppresses_a_repeat_of_the_same_path(agent_home):
    t0 = datetime(2026, 8, 15, 12, 0, 0)
    s1, s2 = _Sender(), _Sender()
    srr.record("productivity-stop-gate", "first", "testagent", now=t0, sender=s1)
    srr.record("productivity-stop-gate", "second", "testagent",
               now=t0 + timedelta(minutes=5), sender=s2)
    assert len(s1.calls) == 1
    assert s2.calls == [], "repeat inside the window should not re-email"
    assert _read(agent_home)["notified"] == "throttled"


def test_throttle_expires_after_the_window(agent_home):
    t0 = datetime(2026, 8, 15, 12, 0, 0)
    srr.record("productivity-stop-gate", "first", "testagent", now=t0,
               sender=_Sender())
    s2 = _Sender()
    srr.record("productivity-stop-gate", "later", "testagent",
               now=t0 + timedelta(minutes=61), sender=s2)
    assert len(s2.calls) == 1


def test_a_different_path_is_never_throttled(agent_home):
    """Two different stop causes are two different things the user must hear."""
    t0 = datetime(2026, 8, 15, 12, 0, 0)
    srr.record("productivity-stop-gate", "first", "testagent", now=t0,
               sender=_Sender())
    s2 = _Sender()
    srr.record("reducer-self-fence", "different cause", "testagent",
               now=t0 + timedelta(minutes=1), sender=s2)
    assert len(s2.calls) == 1


def test_a_previous_FAILURE_does_not_throttle_the_next_attempt(agent_home):
    """The load-bearing asymmetry: only a SENT notification throttles.

    If a failed send throttled, a single transport blip would silence the path
    for the whole window — which is precisely the silent-quiet-mode failure this
    script exists to prevent.
    """
    t0 = datetime(2026, 8, 15, 12, 0, 0)
    srr.record("productivity-stop-gate", "first", "testagent", now=t0,
               sender=_Sender(status="failed", detail="transport rc=1"))
    s2 = _Sender()
    srr.record("productivity-stop-gate", "second", "testagent",
               now=t0 + timedelta(minutes=1), sender=s2)
    assert len(s2.calls) == 1, "a failed send must not suppress the retry"


def test_unparseable_previous_timestamp_does_not_throttle(agent_home):
    """Cannot prove we are inside the window -> fail toward telling the user."""
    prev = {"path": "productivity-stop-gate", "notified": "sent",
            "stopped_at": "not-a-timestamp"}
    assert srr.should_throttle(prev, "productivity-stop-gate",
                               datetime(2026, 8, 15, 12, 0, 0), 60) is False


def test_missing_previous_file_does_not_throttle():
    assert srr.should_throttle({}, "productivity-stop-gate",
                               datetime(2026, 8, 15, 12, 0, 0), 60) is False


# -------------------------------------------------------- failure visibility

def test_transport_failure_is_recorded_not_swallowed(agent_home):
    """guard-1673: the consumer's actual error must survive into the record."""
    sender = _Sender(status="failed", detail="transport rc=2: REFUSED provenance")
    fields = srr.record("recovery-gate-zombie", "zombie recovered", "testagent",
                        sender=sender)
    assert fields["notified"] == "failed"
    assert "REFUSED provenance" in fields["notify_detail"]
    assert "REFUSED provenance" in _read(agent_home)["notify_detail"]


def test_transport_failure_is_announced_loudly_on_stderr(agent_home, capsys):
    """guard-3737: the failure branch is the one that makes an inert
    notification look like a working one, so it must log loudly."""
    srr.record("reducer-self-fence", "stepdown", "testagent",
               sender=_Sender(status="failed", detail="boom"))
    err = capsys.readouterr().err
    assert "CRITICAL" in err
    assert "nobody has been told" in err


def test_a_raising_transport_never_propagates(agent_home):
    """A stop must complete even if its announcement is broken end to end."""
    def explode(agent, subject, body, log):
        raise RuntimeError("transport exploded")

    with pytest.raises(RuntimeError):
        explode("a", "b", "c", lambda m: None)  # sanity: it really does raise

    # record() delegates to the sender; the CLI wrapper is what must absorb it.
    # Prove the CLI contract instead of pretending record() swallows everything.
    import subprocess
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "stop-reason-record.py"),
         "--path", "productivity-stop-gate", "--reason", "x", "--agent", ""],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, "the recorder must never block a stop"


def test_cli_with_no_agent_exits_zero(agent_home):
    import subprocess
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "stop-reason-record.py"),
         "--path", "reducer-self-fence", "--reason", "r", "--agent", ""],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0
    assert "no agent" in out.stderr


# ------------------------------------------------- caller wiring (guard-3737)
#
# Everything above mocks the transport, which proves the recorder WORKS but not
# that anything CALLS it. guard-3737 is explicit that "the code looks right" is
# not acceptable confirmation for fail-open wiring — so assert the call sites.

CALLERS = {
    "productivity-stop-gate.sh": ["productivity-stop-gate"],
    "recovery-gate.sh": ["recovery-failed-permanent", "recovery-gate-zombie"],
    "reducer-self-fence.sh": ["reducer-self-fence"],
}


@pytest.mark.parametrize("fname,paths", sorted(CALLERS.items()))
def test_each_stop_path_actually_invokes_the_recorder(fname, paths):
    src = (SCRIPTS / fname).read_text(encoding="utf-8")
    assert "stop-reason-record.py" in src, f"{fname} lost its recorder wiring"
    for p in paths:
        assert (f"--path {p}" in src) or (f'"--path", "{p}"' in src), (
            f"{fname} no longer records the '{p}' stop path")


@pytest.mark.parametrize("fname,paths", sorted(CALLERS.items()))
def test_caller_path_values_are_all_in_the_closed_enum(fname, paths):
    """A caller and the enum drifting apart would make argparse refuse at the
    exact moment of a real stop — the worst possible time to discover it."""
    for p in paths:
        assert p in srr.VALID_PATHS, f"{fname} uses '{p}', absent from VALID_PATHS"


@pytest.mark.parametrize("fname", sorted(CALLERS))
def test_recorder_stderr_is_not_discarded_at_any_call_site(fname):
    """guard-1673: a 2>/dev/null here throws away the consumer's actual reason,
    making a refused send look identical to a successful one."""
    src = (SCRIPTS / fname).read_text(encoding="utf-8")
    for line in src.splitlines():
        if "stop-reason-record.py" in line and "2>/dev/null" in line:
            pytest.fail(f"{fname} discards the recorder's stderr: {line.strip()}")


def test_all_four_goal_named_paths_have_a_caller():
    """The goal names four deliberate-stop paths. If one loses its caller, the
    box goes quiet again on exactly that path — the original defect."""
    wired = {p for paths in CALLERS.values() for p in paths}
    assert set(STOP_PATHS) == wired, f"unwired stop paths: {set(STOP_PATHS) - wired}"


def test_cli_refuses_an_unknown_path():
    """argparse choices — a typo fails loudly at the caller, not silently."""
    import subprocess
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "stop-reason-record.py"),
         "--path", "typo-path", "--reason", "r", "--agent", "testagent"],
        capture_output=True, text=True, timeout=60)
    assert out.returncode != 0
    assert "invalid choice" in out.stderr


# ------------------------------------------- worker park paths ()
#
# The park is the FIFTH deliberate-quiet path, and the first that RESUMES
# ITSELF. That makes it the first one whose reason file must be REMOVED again,
# and the first that must never email. Both properties are pinned here because
# both fail silently: a park that emails trains the user to mute the transport,
# and a latch that never clears suppresses the alert for a LATER real death.
#
# NOT added to STOP_PATHS / CALLERS deliberately, and the bound is stated rather
# than left implied (guard-1936). Those two structures assert that a `.sh` under
# core/scripts/ greps for the path name. `worker-body-parked`'s only call site is
# LLM-executed prose in .claude/skills/worker-loop/SKILL.md, which is not a shell
# caller — the same reason `user-stop` sits outside them. The SKILL.md wiring is
# asserted separately below so "no shell caller" does not become "no caller".

def test_park_is_recorded_so_the_sweeper_reads_expected_idle(agent_home):
    """MEASURED, not stylistic: the sweeper's classify() short-circuits to
    EXPECTED_IDLE on this file BEFORE its heartbeat-age branch, and its
    --stale-min default is 45 while a park re-polls at 3600s (the ScheduleWakeup
    clamp). 60 > 45, so with no reason file a healthy parked Body is classified
    DEAD_LOOP for ~15 min of every hour and the user is emailed that a
    deliberately-parked box is dead.
    """
    sender = _Sender()
    fields = srr.record("worker-body-parked", "reducer LIVE check failed",
                        "testagent", sender=sender)
    assert _read(agent_home)["path"] == "worker-body-parked"
    assert fields["reason"] == "reducer LIVE check failed"


def test_park_never_emails_even_when_the_caller_forgets_no_notify(agent_home):
    """STRUCTURAL enforcement, not caller-supplied.

    `notify=True` is passed explicitly here — the production default and exactly
    what an LLM call site that forgets `--no-notify` produces. A park is quiet
    on purpose and self-resuming, so there is no human action to request.
    """
    sender = _Sender()
    fields = srr.record("worker-body-parked", "no reducer", "testagent",
                        notify=True, sender=sender)
    assert sender.calls == [], "a park must never email — nobody needs to act"
    assert fields["notified"] == "skipped-path-never-notifies"


def test_park_expiry_DOES_email_because_a_human_start_is_then_required(agent_home):
    """The opposite side of the same split. At PARK_MAX_HOURS the Body takes the
    genuine close path, and /start is user-only — so this one is a deliberate
    stop in the original sense and must reach a human."""
    sender = _Sender()
    fields = srr.record("worker-park-expired", "parked 60h, no reducer returned",
                        "testagent", sender=sender)
    assert len(sender.calls) == 1
    assert fields["notified"] == "sent"
    assert "/start testagent" in sender.calls[0]["body"]


def test_clear_removes_the_latch_so_a_resumed_body_is_not_forever_idle(agent_home):
    """The inverse operation. Every OTHER writer here stops the loop for good and
    hands recovery to /start, which clears the file via session-manifest-clear.
    A park->resume never goes through /start, so without this the Body resumes,
    works normally for days, and keeps reporting EXPECTED_IDLE."""
    srr.record("worker-body-parked", "no reducer", "testagent", sender=_Sender())
    target = agent_home / "session" / srr.REASON_FILENAME
    assert target.exists()
    assert srr.clear("testagent") is True
    assert not target.exists()


def test_clear_is_idempotent_and_never_raises(agent_home):
    """It runs on every resume, including resumes that never parked through this
    recorder. Raising there would break the resume path to protect a file."""
    assert srr.clear("testagent") is False
    assert srr.clear("testagent") is False


def test_cli_clear_needs_no_path_or_reason(agent_home, monkeypatch):
    """`--reason` was required=True before this. Demanding a reason for
    UN-stopping is nonsense, so the requirement moved into main()."""
    import subprocess
    srr.record("worker-body-parked", "no reducer", "testagent", sender=_Sender())
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "stop-reason-record.py"),
         "--clear", "--agent", "testagent"],
        capture_output=True, text=True, timeout=60,
        env={**__import__("os").environ,
             "MIND_AGENTS_ROOT": str(agent_home.parent)})
    assert out.returncode == 0
    assert "cleared" in out.stderr


def test_cli_without_path_or_reason_is_loud_but_never_blocks(agent_home):
    """A malformed invocation must not become the reason a box fails to stop."""
    import subprocess
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "stop-reason-record.py"),
         "--agent", "testagent"],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, "this helper must never block a stop"
    assert "required unless --clear" in out.stderr


def test_worker_loop_skill_actually_wires_both_park_paths():
    """The caller-wiring assertion for the LLM-executed call site.

    CALLERS above only greps `.sh` files, so without this the park paths would
    be the one family in this module with no wiring proof at all — and an
    unwired park is the DEAD_LOOP false alarm the paths exist to prevent.
    """
    skill = (SCRIPTS.parent.parent / ".claude" / "skills" / "worker-loop"
             / "SKILL.md").read_text(encoding="utf-8")
    assert "stop-reason-record.py" in skill, "worker-loop lost its recorder wiring"
    for p in ("worker-body-parked", "worker-park-expired"):
        assert f"--path {p}" in skill, f"worker-loop no longer records '{p}'"
    # Assert the INVOCATION, not the token. `--clear` alone was satisfied by this
    # file's own prose explaining the flag, so mutating the real call to
    # `--NOPE` killed zero mutants — a presence check that passes on
    # documentation while the wiring is gone (guard-302 class). Caught by
    # mutation, not by reading.
    assert "stop-reason-record.py --clear" in skill, (
        "the resume path must clear the latch or EXPECTED_IDLE never lifts")


def test_park_paths_are_in_the_closed_enum():
    for p in ("worker-body-parked", "worker-park-expired"):
        assert p in srr.VALID_PATHS
    assert "worker-body-parked" in srr.NO_NOTIFY_PATHS
    assert "worker-park-expired" not in srr.NO_NOTIFY_PATHS, (
        "expiry hands recovery to a human and MUST reach them")
