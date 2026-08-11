"""DDB claim-heartbeat failure must be DURABLE and LOUD — .

WHAT BROKE (measured 2026-08-05, alpha/cc-04): heartbeat-tick.sh calls two
independent liveness legs in one tick — the local `runner-heartbeat` mtime touch
plus the team-state update, and the cross-machine DDB claim heartbeat. The DDB
leg was wired `|| true`, discarding its rc. It stopped succeeding ~105 minutes
before the agent stopped working, while the other legs kept succeeding, so
NOTHING surfaced it. The claim aged past OWNERSHIP_STALE_SECONDS under a healthy,
goal-executing reducer; two later `/start`s each read a "free" claim and came up
as REDUCERS instead of workers. Role derivation was correct — its input was a lie.

WHAT THESE TESTS PIN:
  1. a failing DDB leg writes the durable marker with count=1 (guard-772: a
     stderr-only warning is invisible inside a backgrounded Bash call, which is
     the normal case for this tick — so the FILE is the primary signal).
  2. consecutive failures ACCUMULATE (count=2) and keep the ORIGINAL
     first_failed_at, because the age of the outage — not the retry count — is
     what decides whether a peer may legally take the claim.
  3. a later success CLEARS the marker, so `count` always means CONSECUTIVE
     (rb-4842: a stability signal that never resets reads as a permanent outage
     after one blip).
  4. once the outage exceeds half of OWNERSHIP_STALE_SECONDS the warning
     ESCALATES to the loud banner naming the second-reducer consequence — the
     window where there is still runway to act before takeover becomes legal.
  5. the tick still EXITS 0 and still writes runner-heartbeat while the DDB leg
     is failing. This is the load-bearing one: the whole point is visibility
     WITHOUT converting a DDB hiccup into a blocked iteration
     (guard-1562 — flipping fail-open to fail-closed is its own hazard).

HERMETIC, and for the reason test_body_heartbeat_writer.py documents: the
MIND_AGENT_DIR seam alone is NOT enough, because `session-state-get.sh` is
IRREDUCIBLY LOCAL and derives PROJECT_ROOT from its own location, so under the
seam alone the IDLE gate reads the REAL box's agent-state and the colour of the
test is decided by machine state rather than code. Every test here stages a
RELOCATED PROJECT_ROOT, COPIES scripts (never symlinks — guard-2534: _paths.sh
resolves logically while _paths.py resolves symlinks, so a symlinked tree
addresses two different roots), and stubs runner-claim.sh so nothing reaches a
real daemon or a real DDB table.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MARKER = "claim-heartbeat-failure"


def _bash():
    """Never a bare 'bash' argv[0] on win32 (guard-580 / rb CreateProcess class):
    CreateProcess searches System32 first and can reach the WSL launcher, which
    blocks forever on a wedged LxssManager."""
    sys.path.insert(0, str(REPO / "core" / "scripts"))
    from _runtime_bash import bash_cmd  # noqa: E402

    return bash_cmd


def _stage(tmp_path, *, claim_rc, agent="alpha"):
    """Relocated PROJECT_ROOT with a stubbed runner-claim.sh exiting `claim_rc`."""
    root = tmp_path / "repo"
    (root / "core" / "scripts").mkdir(parents=True)
    (root / "core" / "config").mkdir(parents=True)
    sess = root / "agents" / agent / "session"
    sess.mkdir(parents=True)

    src = REPO / "core" / "scripts"
    # COPY, never symlink (guard-2534).
    for name in ("heartbeat-tick.sh", "_paths.sh", "_platform.sh"):
        if (src / name).exists():
            shutil.copy2(src / name, root / "core" / "scripts" / name)

    # Stub every sibling the tick shells out to, so the unit under test is the
    # ONLY live code path and nothing reaches a shared store (guard-2484).
    for name in (
        "session-state-get.sh",
        "team-state-update.sh",
        "live-phase-emit.sh",
        "body-manifest.sh",
        "session-signal-exists.sh",
    ):
        p = root / "core" / "scripts" / name
        p.write_text("#!/usr/bin/env bash\necho RUNNING\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)

    claim = root / "core" / "scripts" / "runner-claim.sh"
    claim.write_text(
        "#!/usr/bin/env bash\n"
        f"[ {claim_rc} -ne 0 ] && echo 'daemon returned an error (stubbed)' >&2\n"
        f"exit {claim_rc}\n",
        encoding="utf-8",
    )
    claim.chmod(0o755)

    (root / ".env.local").write_text("STORAGE_BACKEND=own-cloud\n", encoding="utf-8")
    (sess / "agent-state").write_text("RUNNING", encoding="utf-8")
    return root, sess


def _tick(root, agent="alpha", **extra_env):
    env = dict(os.environ)
    env.update(
        {
            "MIND_AGENT": agent,
            "MIND_AGENT_DIR": str(root / "agents" / agent),
            "PROJECT_ROOT": str(root),
            "STORAGE_BACKEND": "own-cloud",
            "MIND_SID": "",
        }
    )
    env.update({k: str(v) for k, v in extra_env.items()})
    bash_cmd = _bash()
    return subprocess.run(
        bash_cmd(str(root / "core" / "scripts" / "heartbeat-tick.sh")),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(root),
    )


def _parse(marker_path):
    out = {}
    for line in marker_path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def test_failing_ddb_leg_writes_durable_marker(tmp_path):
    """1. The FILE is the primary signal — stderr alone is invisible (guard-772)."""
    root, sess = _stage(tmp_path, claim_rc=1)
    r = _tick(root)
    marker = sess / MARKER
    assert marker.exists(), (
        "a failing DDB claim heartbeat left NO durable trace; stderr alone is "
        f"invisible in a backgrounded call. stdout={r.stdout!r} stderr={r.stderr!r}"
    )
    fields = _parse(marker)
    assert fields.get("count") == "1"
    assert fields.get("last_rc") == "1"
    assert fields.get("first_failed_at", "").isdigit()


def test_consecutive_failures_accumulate_and_keep_first_timestamp(tmp_path):
    """2. Age of the outage decides takeover legality — it must not reset per tick."""
    root, sess = _stage(tmp_path, claim_rc=1)
    _tick(root)
    first = _parse(sess / MARKER)["first_failed_at"]
    _tick(root)
    second = _parse(sess / MARKER)
    assert second["count"] == "2"
    assert second["first_failed_at"] == first, (
        "first_failed_at was rewritten on the second failure — the outage would "
        "look 0s old forever and the escalation could never fire"
    )


def test_success_clears_the_marker(tmp_path):
    """3. count must mean CONSECUTIVE (rb-4842)."""
    root, sess = _stage(tmp_path, claim_rc=1)
    _tick(root)
    assert (sess / MARKER).exists()

    claim = root / "core" / "scripts" / "runner-claim.sh"
    claim.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    claim.chmod(0o755)
    _tick(root)
    assert not (sess / MARKER).exists(), (
        "a recovered heartbeat left the failure marker behind; one old blip "
        "would then read as an ongoing outage forever"
    )


def test_escalates_loudly_past_half_the_stale_window(tmp_path):
    """4. Escalation must name the SECOND-REDUCER consequence, not just 'failed'."""
    root, sess = _stage(tmp_path, claim_rc=1)
    _tick(root)
    # Backdate the outage past half of a small stale window.
    marker = sess / MARKER
    fields = _parse(marker)
    fields["first_failed_at"] = str(int(fields["first_failed_at"]) - 400)
    marker.write_text(
        "".join(f"{k}={v}\n" for k, v in fields.items()), encoding="utf-8"
    )
    r = _tick(root, OWNERSHIP_STALE_SECONDS=600)
    assert "CLAIM HEARTBEAT FAILING" in r.stderr, (
        f"no loud banner past half the stale window. stderr={r.stderr!r}"
    )
    assert "SECOND REDUCER" in r.stderr, (
        "the banner must name the consequence (a peer /start becoming a second "
        f"reducer), not merely report a failed call. stderr={r.stderr!r}"
    )


def test_tick_stays_fail_open_while_the_ddb_leg_fails(tmp_path):
    """5. Visibility must NOT become a blocked iteration (guard-1562)."""
    root, sess = _stage(tmp_path, claim_rc=1)
    r = _tick(root)
    assert r.returncode == 0, (
        "a failing DDB claim heartbeat blocked the tick; a hiccup in the "
        f"cross-machine leg must never stop the loop. stderr={r.stderr!r}"
    )
    assert (sess / "runner-heartbeat").exists(), (
        "the LOCAL runner-heartbeat was not written while the DDB leg failed — "
        "the two legs must stay independent in both directions"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
