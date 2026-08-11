"""test_owncloud_pull_timeout_no_retry.py — regression for .

owncloud-pull.sh's `_pull_one_agent` retried on rc=3. That looks harmless until
you know what rc=3 MEANS: _runtime.sh:855 returns 3 for connection-refused, DNS
failure AND request-exceeded-RT_CURL_TIMEOUT alike, with curl's own stderr
discarded by `2>/dev/null` (guard-114). So a TIMEOUT was being retried as if the
daemon were absent — re-issuing the same slow request for a second full ceiling,
and autospawning against a perfectly healthy daemon (the orphan hazard
_runtime.sh:52-55 names).

The cost is not the wasted request, it is the SILENCE. Nothing is printed until
the whole thing gives up, because stdout is captured into RESPONSE and the only
message naming RT_CURL_TIMEOUT lives in the caller's rt_no_daemon_error, past
the retry. Measured on cc-03 (2026-08-02): RT_CURL_TIMEOUT=5 cost 20.2s wall,
4x the ceiling; at the 90s default that is ~190s of zero output. An observer who
kills the command at 120s therefore sees ZERO BYTES and reports a silent hang
rather than a timeout — which is exactly what was reported, on three boxes and
two OSes, before anyone found the retry.

These tests drive the SHIPPED function (extracted off disk at test time), not a
copy, so they cannot drift from what actually runs. Extraction is anchored on
stable content markers rather than line numbers, matching the sibling suite
test_owncloud_pull_fleet.py.

The load-bearing assertion is `test_timeout_does_not_retry`: it fails if anyone
reinstates the retry on a reachable daemon. `test_absent_daemon_still_retries`
is its guard-rail — the fix must NOT disable autospawn for the case autospawn
actually exists for, and a fix that made both paths fail fast would pass the
first test alone.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from _bash_helpers import BASH  # never a bare "bash" argv[0] (guard-580)

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PULL_SCRIPT = CORE_SCRIPTS / "owncloud-pull.sh"


def extract_pull_one_agent() -> str:
    """Pull `_pull_one_agent` out of the shipped script by content markers.

    Anchored on the function header and the first line-initial `}` after it, so
    ordinary edits inside the body (comments, an added probe) do not break
    extraction, while a rename or deletion does — which is the correct failure.
    """
    src = PULL_SCRIPT.read_text(encoding="utf-8")
    start = src.index("_pull_one_agent() {")
    end = src.index("\n}\n", start) + len("\n}\n")
    body = src[start:end]
    assert "rt_try_autospawn" in body, "extraction lost the autospawn branch"
    return body


def run_harness(*, daemon_up: bool) -> dict:
    """Drive the extracted function against stubbed collaborators.

    Stubs stand in for the three things the function calls out to: `_do_call`
    (the daemon request), `rt_is_up` (the health probe), `rt_try_autospawn`
    (the respawn). `_do_call` ALWAYS returns 3, i.e. the request never succeeds
    — so the only thing under test is how many times it is attempted and
    whether a respawn was triggered.
    """
    fn = extract_pull_one_agent()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        calls = tmp / "calls"
        spawns = tmp / "spawns"
        calls.write_text("", encoding="utf-8")
        spawns.write_text("", encoding="utf-8")

        # rt_is_up's exit code is the whole discriminator: 0 == reachable
        # (so an rc=3 must have been a TIMEOUT), 1 == genuinely absent.
        up_rc = 0 if daemon_up else 1
        harness = f"""
set -uo pipefail
_do_call() {{ echo "call" >> "{calls.as_posix()}"; return 3; }}
rt_is_up() {{ return {up_rc}; }}
rt_try_autospawn() {{ echo "spawn" >> "{spawns.as_posix()}"; return 0; }}

{fn}

_pull_one_agent "testagent"
echo "RC=$?"
"""
        proc = subprocess.run([BASH, "-c", harness],
                              capture_output=True, text=True)
        rc_match = re.search(r"RC=(\d+)", proc.stdout)
        return {
            "rc": int(rc_match.group(1)) if rc_match else None,
            "calls": len([l for l in calls.read_text(encoding="utf-8").splitlines() if l]),
            "spawns": len([l for l in spawns.read_text(encoding="utf-8").splitlines() if l]),
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }


def test_timeout_does_not_retry():
    """Reachable daemon + rc=3 == timeout: attempt ONCE, never respawn.

    This is the regression. Before the fix both counters were 2 and 1, which is
    what doubled the silent window from ~95s to ~190s and put the diagnostic
    out of reach of a 120s observation kill.
    """
    r = run_harness(daemon_up=True)
    assert r["calls"] == 1, (
        "a timeout was retried: _do_call ran %d times. The daemon is REACHABLE, "
        "so rc=3 meant the request was slow, not lost — retrying it buys nothing "
        "and doubles the silent window (g-115-3822)." % r["calls"]
    )
    assert r["spawns"] == 0, (
        "autospawn ran against a healthy daemon (%d times) — the orphan hazard "
        "_runtime.sh:52-55 names." % r["spawns"]
    )
    # Tolerant, per guard-695: assert the CONTRACT (a non-zero rc reaches the
    # caller, distinct from the absent-daemon code so fleet mode can isolate it)
    # rather than re-pinning a literal. This assertion previously read
    # `== 3` and went stale the moment  split the codes — a failure
    # that surfaces only on a full-suite run, never on the targeted test.
    assert r["rc"] not in (0, None), (
        "a timeout must propagate a non-zero rc so the caller can react; got %r"
        % (r["rc"],)
    )
    assert r["rc"] != run_harness(daemon_up=False)["rc"], (
        "the timeout rc must be DISTINCT from the absent-daemon rc — collapsing "
        "them is the g-115-4580 defect: fleet mode then cannot tell a slow agent "
        "(isolate) from a dead daemon (abort), and one slow peer kills the sweep"
    )


def test_absent_daemon_still_retries():
    """Genuinely-absent daemon: respawn and retry, exactly as before.

    Without this, a 'fix' that simply deleted the autospawn branch would pass
    the regression above while breaking the recovery path autospawn exists for.
    """
    r = run_harness(daemon_up=False)
    assert r["spawns"] == 1, (
        "autospawn did not run for an ABSENT daemon — the fix must narrow the "
        "retry to the no-daemon case, not remove it"
    )
    assert r["calls"] == 2, (
        "expected the post-spawn retry (2 calls), got %d" % r["calls"]
    )


def test_probe_precedes_autospawn_in_shipped_source():
    """Order matters: the health probe must gate the respawn, not follow it.

    A probe placed after rt_try_autospawn would still 'have a probe' while the
    respawn — the expensive, orphan-risking half — had already fired.
    """
    body = extract_pull_one_agent()
    assert "rt_is_up" in body, "the rt_is_up guard is gone (guard-597)"
    assert body.index("rt_is_up") < body.index("rt_try_autospawn"), (
        "rt_is_up must be probed BEFORE rt_try_autospawn, or the respawn fires "
        "on a healthy daemon anyway"
    )
