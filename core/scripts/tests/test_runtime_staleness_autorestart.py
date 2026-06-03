"""test_runtime_staleness_autorestart.py —  integration tests.

Verifies the two coordinated changes to core/scripts/_runtime.sh that close
both g-115-735 daemon-staleness failure modes:

  Change 1 — rt_check_staleness arms RT_STALENESS_RESTART_PENDING on SHA
             mismatch; rt_ensure_running consumes it to kill+respawn the
             stale daemon ONCE per shell (RT_STALENESS_RESTARTED), honoring
             RT_NO_AUTOSPAWN.
  Change 2 — rt_curl escalates a routing-layer 404 (body contains
             "no route for ", mind_api/src/server.py Response.error) from
             rc=2 to rc=3 so rt_call takes the auto-spawn/restart path.

Test strategy (first-principles): the verification outcomes ("auto-restart
fires on SHA drift", "routing-404 triggers rc=3") are properties of the
bash control flow, not of OS process / curl mechanics. Spinning a real
daemon, advancing git HEAD, and asserting a PID change is flaky on Windows
(port races, detached-child kill semantics). Instead each case sources the
REAL _runtime.sh and overrides only the leaf primitives (curl, rt_is_up,
rt_spawn, rt_wait_for_ready, rt_daemon_kill, rt_on_disk_sha, rt_base_url)
with recording stubs, then asserts the observable state machine. The code
under test is the real shipped code — only its I/O leaves are stubbed.

Harness mirrors test_iteration_commit_untracked_filter.py (rb-919): resolve
Git-Bash explicitly (default `bash` is WSL on this machine), run with
PROJECT_ROOT pre-exported so _runtime.sh's idempotency guard skips the
_paths.sh agent-binding dependency (pure-function test).

12 cases:
  1. change2-routing-404-escalates       — "no route for " body, 404 -> rc 3
  2. change2-plain-404-unchanged         — other 404 body            -> rc 2
  3. change2-2xx-unchanged               — 200 body                  -> rc 0
  4. change1-autorestart-fires           — PENDING set -> kill+spawn, rc 0,
                                            RESTARTED=1, PENDING cleared
  5. change1-one-shot                    — RESTARTED already 1 -> NO 2nd kill
  6. change1-no-autospawn-suppresses     — RT_NO_AUTOSPAWN=1 -> NO kill
  7. change1-sentinel-armed-on-mismatch  — rt_check_staleness sets PENDING
                                            AND WARNED (outcome 5 preserved)
  8. change1-no-sentinel-when-sha-equal  — SHA equal -> no PENDING, no WARNED
  9. change1d-rc3-check-before-ensure    — rc==3 path: check_staleness runs
                                            BEFORE ensure_running (sq-018 glue)
 10. g115787-success-consumes-sentinel   — rc!=3 + PENDING -> rt_ensure_running
                                            consumes + retry once, rc 0 (the fix)
 11. g115787-fresh-success-skips-ensure  — rc!=3 + no sentinel -> NO ensure
                                            (hot path stays cheap)
 12. g115787-no-autospawn-suppresses     — rc!=3 + PENDING + RT_NO_AUTOSPAWN=1
                                            -> NO recycle (health/test paths)

g-115-787 (cases 9-11): rt_call's SUCCESS path (rc != 3) previously armed the
staleness sentinel but never consumed it — only the rc==3 path called
rt_ensure_running. A stale-but-responsive daemon thus never auto-healed. The
fix makes the success path symmetric (consume in-shell + retry once).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent
RUNTIME_SH = CORE_SCRIPTS / "_runtime.sh"


def _to_bash_path(p) -> str:
    """C:\\a\\b -> /c/a/b for Git-Bash (msys) consumption."""
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


# Resolve bash via shared helper (, 2026-05-16). See
# core/scripts/tests/_bash_helpers.py for the canonical resolution
# priority. GIT_BASH alias kept — _run uses Git-Bash mount-prefix
# pathing (/c/...) and the variable name carries that intent.
sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402


def _run(script_body: str) -> subprocess.CompletedProcess:
    """Write a temp harness that sources the real _runtime.sh and run it."""
    harness = (
        "set -uo pipefail\n"
        f'export PROJECT_ROOT="{_to_bash_path(PROJECT_ROOT)}"\n'
        # PROJECT_ROOT pre-set -> _runtime.sh idempotency guard skips _paths.sh.
        f'source "{_to_bash_path(RUNTIME_SH)}"\n'
        + script_body
    )
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sh", delete=False, newline="\n"
    ) as fh:
        fh.write(harness)
        tmp = fh.name
    try:
        return subprocess.run(
            [GIT_BASH, _to_bash_path(tmp)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(PROJECT_ROOT),
        )
    finally:
        os.unlink(tmp)


# RT_MARKER from _runtime.sh: $'\n\x1f__RT_STATUS__:' then the http_code.
_MARKER = "\\n\\x1f__RT_STATUS__:"


def _curl_stub(body: str, code: str) -> str:
    """A curl() shell function emitting rt_curl's expected marker format."""
    # Body is single-quoted so embedded JSON double-quotes survive the
    # Python->bash string embedding (test bodies are JSON: no single quotes).
    # printf %b interprets the \n and \x1f escapes in _MARKER.
    return (
        "curl() {\n"
        f"  printf '%s' '{body}'\n"
        f"  printf '%b' \"{_MARKER}\"\n"
        f"  printf '%s' \"{code}\"\n"
        "}\n"
    )


# ─────────────────────────── Change 2 (rt_curl) ───────────────────────────

def test_change2_routing_404_escalates_to_rc3():
    """Routing-layer 404 ("no route for ...") -> rc 3, not 2."""
    body = "no route for GET /v1/aspirations/recover-recurring"
    r = _run(
        _curl_stub(body, "404")
        + 'rt_base_url() { echo "http://127.0.0.1:9999"; }\n'
        + "rc=0; rt_curl GET /v1/x >/dev/null 2>&1 || rc=$?\n"
        + 'echo "RC=$rc"\n'
    )
    assert "RC=3" in r.stdout, f"expected rc=3, got:\n{r.stdout}\n{r.stderr}"


def test_change2_plain_404_unchanged():
    """A non-routing 404 body still returns rc 2 (behavior preserved)."""
    body = '{"error":"not_found","detail":"goal g-999 missing"}'
    r = _run(
        _curl_stub(body, "404")
        + 'rt_base_url() { echo "http://127.0.0.1:9999"; }\n'
        + "rc=0; rt_curl GET /v1/x >/dev/null 2>&1 || rc=$?\n"
        + 'echo "RC=$rc"\n'
    )
    assert "RC=2" in r.stdout, f"expected rc=2, got:\n{r.stdout}\n{r.stderr}"


def test_change2_2xx_unchanged():
    """A 200 still returns rc 0 and prints the body (no regression)."""
    r = _run(
        _curl_stub('{"ok":true}', "200")
        + 'rt_base_url() { echo "http://127.0.0.1:9999"; }\n'
        + "rc=0; out=$(rt_curl GET /v1/x 2>/dev/null) || rc=$?\n"
        + 'echo "RC=$rc"; echo "OUT=$out"\n'
    )
    assert "RC=0" in r.stdout, f"expected rc=0, got:\n{r.stdout}\n{r.stderr}"
    assert '{"ok":true}' in r.stdout, f"body not echoed:\n{r.stdout}"


# ────────────────────── Change 1 (rt_ensure_running) ──────────────────────

#  single-chokepoint: rt_spawn owns the clean-stop. The
# rt_ensure_running staleness path no longer calls rt_daemon_kill
# directly — it delegates to rt_spawn, which calls rt_daemon_kill FIRST
# (real _runtime.sh:252). The rt_spawn recorder mirrors that real
# topology so KILL_CALLED reflects production: kill is a CONSEQUENCE of
# rt_spawn, not a separate staleness-path call. Tests asserting
# KILL_CALLED absent (one-shot / no-autospawn) stay green — they skip
# the rt_spawn invocation entirely, so the stubbed kill never fires.
_RESTART_RECORDERS = (
    'rt_is_up() { return 0; }\n'              # daemon IS up (stale, not down)
    # B2 (2026-06-01): the stale-restart path now double-checks rt_daemon_is_fresh
    # after acquiring the spawn lock (skip a redundant restart when another
    # wrapper already refreshed the daemon). Stub it "not fresh" here so the
    # restart-fires cases exercise a GENUINELY-stale daemon — and so the check
    # never falls through to the real primitives / a live daemon (hermetic).
    'rt_daemon_is_fresh() { return 1; }\n'
    'rt_daemon_kill() { echo "KILL_CALLED"; }\n'
    'rt_spawn() { rt_daemon_kill; echo "SPAWN_CALLED"; }\n'
    'rt_wait_for_ready() { return 0; }\n'
)


def test_change1_autorestart_fires_on_pending_sentinel():
    """PENDING set, not yet restarted, autospawn allowed -> kill+spawn,
    rc 0, RESTARTED=1, PENDING cleared."""
    r = _run(
        _RESTART_RECORDERS
        + "export RT_STALENESS_RESTART_PENDING=1\n"
        + "unset RT_STALENESS_RESTARTED 2>/dev/null || true\n"
        + "rc=0; rt_ensure_running || rc=$?\n"
        + 'echo "RC=$rc"\n'
        + 'echo "RESTARTED=${RT_STALENESS_RESTARTED:-unset}"\n'
        + 'echo "PENDING=${RT_STALENESS_RESTART_PENDING:-unset}"\n'
    )
    assert "KILL_CALLED" in r.stdout, f"kill not called:\n{r.stdout}\n{r.stderr}"
    assert "SPAWN_CALLED" in r.stdout, f"spawn not called:\n{r.stdout}"
    assert "RC=0" in r.stdout, f"expected rc=0:\n{r.stdout}"
    assert "RESTARTED=1" in r.stdout, f"RESTARTED not set:\n{r.stdout}"
    assert "PENDING=unset" in r.stdout, f"PENDING not cleared:\n{r.stdout}"


def test_change1_one_shot_no_second_restart():
    """Already RESTARTED=1 -> sentinel block skipped, NO second kill."""
    r = _run(
        _RESTART_RECORDERS
        + "export RT_STALENESS_RESTART_PENDING=1\n"
        + "export RT_STALENESS_RESTARTED=1\n"
        + "rc=0; rt_ensure_running || rc=$?\n"
        + 'echo "RC=$rc"\n'
    )
    assert "KILL_CALLED" not in r.stdout, (
        f"one-shot violated — kill called again:\n{r.stdout}\n{r.stderr}"
    )
    # rt_is_up stub returns up -> rt_ensure_running returns 0 via normal path.
    assert "RC=0" in r.stdout, f"expected rc=0 (up):\n{r.stdout}"


def test_change1_no_autospawn_suppresses_restart():
    """RT_NO_AUTOSPAWN=1 -> stale-restart block skipped (health/test paths)."""
    r = _run(
        _RESTART_RECORDERS
        + "export RT_STALENESS_RESTART_PENDING=1\n"
        + "unset RT_STALENESS_RESTARTED 2>/dev/null || true\n"
        + "export RT_NO_AUTOSPAWN=1\n"
        + "rc=0; rt_ensure_running || rc=$?\n"
        + 'echo "RC=$rc"\n'
    )
    assert "KILL_CALLED" not in r.stdout, (
        f"RT_NO_AUTOSPAWN must suppress restart:\n{r.stdout}\n{r.stderr}"
    )


def test_b2_double_check_skips_redundant_restart_when_fresh():
    """B2 (Lodestar): PENDING set, but by the time we hold the spawn lock the
    daemon is already FRESH (another wrapper restarted it while we waited).
    rt_ensure_running's double-check (rt_daemon_is_fresh) must SKIP the
    redundant restart — no kill, no spawn, rc 0 — while still consuming the
    one-shot sentinel (RESTARTED=1, PENDING cleared). Prevents the churn
    amplifier where N wrappers each detect the same staleness and serially
    kill+respawn a healthy daemon (each restart an orphan window)."""
    r = _run(
        _RESTART_RECORDERS
        + 'rt_daemon_is_fresh() { return 0; }\n'   # already fresh -> skip restart
        + "export RT_STALENESS_RESTART_PENDING=1\n"
        + "unset RT_STALENESS_RESTARTED 2>/dev/null || true\n"
        + "rc=0; rt_ensure_running || rc=$?\n"
        + 'echo "RC=$rc"\n'
        + 'echo "RESTARTED=${RT_STALENESS_RESTARTED:-unset}"\n'
        + 'echo "PENDING=${RT_STALENESS_RESTART_PENDING:-unset}"\n'
    )
    assert "KILL_CALLED" not in r.stdout, (
        f"B2 violated — redundant restart fired despite a fresh daemon:\n{r.stdout}\n{r.stderr}"
    )
    assert "SPAWN_CALLED" not in r.stdout, f"B2 — spawn fired despite fresh:\n{r.stdout}"
    assert "RC=0" in r.stdout, f"expected rc=0 (fresh, skipped):\n{r.stdout}"
    assert "RESTARTED=1" in r.stdout, f"one-shot sentinel not consumed:\n{r.stdout}"
    assert "PENDING=unset" in r.stdout, f"PENDING not cleared:\n{r.stdout}"


# ──────────────── Change 1 (rt_check_staleness arms sentinel) ─────────────

_HEALTH_SHA_RUNNING = '{\\"git_head_sha\\":\\"AAAA1111deadbeef\\",\\"ok\\":true}'


def test_change1_sentinel_armed_on_sha_mismatch():
    """rt_check_staleness on SHA mismatch sets BOTH RESTART_PENDING (Change 1
    escalation) and WARNED (outcome 5 — warning visibility preserved)."""
    r = _run(
        'rt_base_url() { echo "http://127.0.0.1:9999"; }\n'
        + 'rt_on_disk_sha() { echo "BBBB2222feedface"; }\n'
        + f'curl() {{ printf \'%s\' "{_HEALTH_SHA_RUNNING}"; }}\n'
        + "unset RT_STALENESS_WARNED RT_STALENESS_RESTART_PENDING 2>/dev/null || true\n"
        + "rt_check_staleness\n"
        + 'echo "PENDING=${RT_STALENESS_RESTART_PENDING:-unset}"\n'
        + 'echo "WARNED=${RT_STALENESS_WARNED:-unset}"\n'
    )
    assert "PENDING=1" in r.stdout, (
        f"sentinel not armed on mismatch:\n{r.stdout}\n{r.stderr}"
    )
    assert "WARNED=1" in r.stdout, (
        f"warning visibility (outcome 5) not preserved:\n{r.stdout}"
    )


def test_change1_no_sentinel_when_sha_matches():
    """SHA equal -> no escalation, no warning (no false restart)."""
    same = '{\\"git_head_sha\\":\\"CAFEBABE0000\\",\\"ok\\":true}'
    r = _run(
        'rt_base_url() { echo "http://127.0.0.1:9999"; }\n'
        + 'rt_on_disk_sha() { echo "CAFEBABE0000"; }\n'
        + f'curl() {{ printf \'%s\' "{same}"; }}\n'
        + "unset RT_STALENESS_WARNED RT_STALENESS_RESTART_PENDING 2>/dev/null || true\n"
        + "rt_check_staleness\n"
        + 'echo "PENDING=${RT_STALENESS_RESTART_PENDING:-unset}"\n'
        + 'echo "WARNED=${RT_STALENESS_WARNED:-unset}"\n'
    )
    assert "PENDING=unset" in r.stdout, (
        f"false sentinel on matching SHA:\n{r.stdout}\n{r.stderr}"
    )
    assert "WARNED=unset" in r.stdout, f"false warning on match:\n{r.stdout}"


# ──────────── Change 1d (rt_call rc==3 wires staleness → ensure) ──────────
# The coordination glue: Change 2 escalates a routing-404 to rc=3, but that
# only closes Mode 1 if rt_call's rc==3 branch runs rt_check_staleness
# (arming the sentinel) BEFORE rt_ensure_running (consuming it). The cases
# above verify the PIECES in isolation; this asserts rt_call WIRES them in
# the correct ORDER on the rc==3 path. A future _runtime.sh refactor that
# reorders/removes the rc==3 rt_check_staleness call would silently
# re-break Mode-1 closure with every other case still green. (sq-018 gap
# surfaced during ; closed inline rather than as a blocked goal.)

def test_change1d_rc3_calls_check_staleness_before_ensure_running():
    """rt_call rc==3 branch: rt_check_staleness MUST run before
    rt_ensure_running (the g-115-745 Change 1d coordination glue)."""
    body = "no route for GET /v1/aspirations/recover-recurring"
    r = _run(
        _curl_stub(body, "404")                       # Change 2: rt_curl -> rc 3
        + 'rt_base_url() { echo "http://127.0.0.1:9999"; }\n'
        # Ordered recorders shadow the real fns (defined AFTER source):
        # Stubs echo ordering markers to stdout — do NOT redirect rt_call's
        # stdout (that would swallow the markers). rt_warn goes to stderr.
        + 'rt_check_staleness() { echo "ORDER:CHECK_STALENESS"; }\n'
        + 'rt_ensure_running() { echo "ORDER:ENSURE_RUNNING"; return 1; }\n'
        + "rc=0; rt_call GET /v1/aspirations/recover-recurring 2>/dev/null "
        + "|| rc=$?\n"
        + 'echo "RC=$rc"\n'
    )
    out = r.stdout
    i_check = out.find("ORDER:CHECK_STALENESS")
    i_ensure = out.find("ORDER:ENSURE_RUNNING")
    assert i_check != -1, f"rt_check_staleness not called on rc==3:\n{out}\n{r.stderr}"
    assert i_ensure != -1, f"rt_ensure_running not called on rc==3:\n{out}\n{r.stderr}"
    assert i_check < i_ensure, (
        "Change 1d violated — rt_check_staleness must run BEFORE "
        f"rt_ensure_running on the rc==3 path:\n{out}\n{r.stderr}"
    )
    # rt_ensure_running stub returned 1 -> rt_call falls through to return 3.
    assert "RC=3" in out, f"expected terminal rc=3:\n{out}"


# ────────────  (rt_call SUCCESS path consumes the sentinel) ───────
# Sibling to Change 1d (rc==3 path) above. The SUCCESS path (rc != 3) armed
# RT_STALENESS_RESTART_PENDING via rt_check_staleness but returned WITHOUT
# calling rt_ensure_running (the sole consumer). Because `export` scopes the
# sentinel to the current shell, it died when the wrapper subprocess exited
# and the next wrapper (a fresh process) never saw it — a stale-but-RESPONSIVE
# daemon (rc 0/2, never rc 3) never auto-healed (the  JSONDecodeError
# class). The fix makes the success path symmetric with rc==3: consume the
# sentinel in-shell + retry once, gated on rt_ensure_running's own conditions
# (PENDING set, not yet RESTARTED, autospawn allowed).

def test_g115787_success_path_consumes_sentinel_and_retries():
    """rt_call SUCCESS path: when rt_check_staleness arms the sentinel on a
    stale-but-RESPONSIVE daemon, rt_call MUST consume it via rt_ensure_running
    and retry once (g-115-787). Previously armed-but-never-consumed."""
    r = _run(
        _curl_stub('{"ok":true}', "200")                       # rt_curl -> rc 0
        + 'rt_base_url() { echo "http://127.0.0.1:9999"; }\n'
        # Simulate stale-but-responsive: the staleness check arms the sentinel.
        + 'rt_check_staleness() { export RT_STALENESS_RESTART_PENDING=1; echo "ORDER:CHECK_STALENESS"; }\n'
        + 'rt_ensure_running() { echo "ORDER:ENSURE_RUNNING"; unset RT_STALENESS_RESTART_PENDING; return 0; }\n'
        + "unset RT_STALENESS_RESTARTED 2>/dev/null || true\n"
        + "rc=0; rt_call GET /v1/x 2>/dev/null || rc=$?\n"
        + 'echo "RC=$rc"\n'
    )
    out = r.stdout
    i_check = out.find("ORDER:CHECK_STALENESS")
    i_ensure = out.find("ORDER:ENSURE_RUNNING")
    assert i_check != -1, f"rt_check_staleness not called on success path:\n{out}\n{r.stderr}"
    assert i_ensure != -1, (
        "g-115-787 regression: success path armed the sentinel but did NOT "
        f"consume it via rt_ensure_running:\n{out}\n{r.stderr}"
    )
    assert i_check < i_ensure, f"check must precede ensure:\n{out}"
    assert "RC=0" in out, f"expected rc=0 after fresh retry:\n{out}"


def test_g115787_fresh_success_path_skips_ensure_running():
    """rt_call SUCCESS path with a FRESH daemon (sentinel not armed) must NOT
    call rt_ensure_running — the hot path stays cheap (no recycle attempt)."""
    r = _run(
        _curl_stub('{"ok":true}', "200")
        + 'rt_base_url() { echo "http://127.0.0.1:9999"; }\n'
        + 'rt_check_staleness() { :; }\n'                 # fresh daemon: arms nothing
        + 'rt_ensure_running() { echo "ORDER:ENSURE_RUNNING"; return 0; }\n'
        + "unset RT_STALENESS_RESTART_PENDING RT_STALENESS_RESTARTED 2>/dev/null || true\n"
        + "rc=0; rt_call GET /v1/x 2>/dev/null || rc=$?\n"
        + 'echo "RC=$rc"\n'
    )
    out = r.stdout
    assert "ORDER:ENSURE_RUNNING" not in out, (
        "hot-path regression — rt_ensure_running called on a FRESH success "
        f"path (no sentinel):\n{out}\n{r.stderr}"
    )
    assert "RC=0" in out, f"expected rc=0:\n{out}"


def test_g115787_no_autospawn_suppresses_success_recycle():
    """RT_NO_AUTOSPAWN=1 on the success path: sentinel armed but recycle
    suppressed (health-probe / test paths observe true state) — mirrors
    test_change1_no_autospawn_suppresses_restart for the success branch."""
    r = _run(
        _curl_stub('{"ok":true}', "200")
        + 'rt_base_url() { echo "http://127.0.0.1:9999"; }\n'
        + 'rt_check_staleness() { export RT_STALENESS_RESTART_PENDING=1; }\n'
        + 'rt_ensure_running() { echo "ORDER:ENSURE_RUNNING"; return 0; }\n'
        + "unset RT_STALENESS_RESTARTED 2>/dev/null || true\n"
        + "export RT_NO_AUTOSPAWN=1\n"
        + "rc=0; rt_call GET /v1/x 2>/dev/null || rc=$?\n"
        + 'echo "RC=$rc"\n'
    )
    out = r.stdout
    assert "ORDER:ENSURE_RUNNING" not in out, (
        f"RT_NO_AUTOSPAWN must suppress success-path recycle:\n{out}\n{r.stderr}"
    )
    assert "RC=0" in out, f"expected rc=0:\n{out}"


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
