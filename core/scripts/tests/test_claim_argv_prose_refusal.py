""" / guard-4418 — the claim wrapper must refuse a prose agent-name.

THE DEFECT (measured live 2026-08-19, six timed-out claims in one morning)
`aspirations-claim.sh <goal> --deviation <code> "<justification>"` binds the
justification sentence to the FREE agent-name positional — `--deviation`
consumes only its enum code, so the trailing string is just another positional.
The sentence then reached the daemon query as `agent=<prose with spaces>`,
curl exited 3 (URL MALFORMED) without ever sending the request, the generic
failure branch mapped that to rc=3 ("unreachable"), rt_call respawned and
retried a deterministic local failure, and the wrapper burned the full
RT_CURL_TIMEOUT window before blaming daemon warmup/contention — the wrong
tree. Same misdiagnosis class as the `--data-raw @`-expansion incident
(g-001-318) documented inside rt_curl itself.

TWO FIXES, TESTED SEPARATELY HERE
  (a) aspirations-claim.sh validates the agent-name positional against
      ^[a-z0-9_-]+$ and refuses extra positionals. The refusal fires in the
      argv loop BEFORE `_runtime.sh` is even sourced, so no daemon client
      exists and nothing can be written — same ordering argument as
      test_unknown_flag_refusal.py (its docstring point 1).
  (b) rt_curl maps curl_rc=3 to return 2 (hard error, no retry) with a
      diagnostic naming the real cause, instead of return 3 (unreachable,
      which triggers respawn+retry against a deterministic failure).

WHY rc==1 IS SAFE TO PIN FOR (a) DESPITE THE SIBLING FILE'S rc==2 WARNING
test_unknown_flag_refusal.py pins rc==2 because its wrappers' daemon path can
also exit 1, so `!= 0` stays green on revert. aspirations-claim.sh's own
refusals (unknown flag, missing goal_id) exit 1 by file convention, and the
daemon path can ALSO exit 1 — so the rc alone cannot discriminate. Every case
below therefore pins rc AND the refusal message AND the absence of daemon-path
markers. On revert, the prose agent proceeds to the daemon path: stderr then
carries daemon/transport text and never the refusal text, so the message
assertions go RED regardless of which rc the transport happens to return.

HERMETIC IN BOTH DIRECTIONS
RT_PORT_FILE points at a nonexistent path and RT_NO_AUTOSPAWN=1 is set
(honored at both spawn sites — _runtime.sh:727,766), so even a REVERTED
wrapper never contacts a live daemon nor spawns one: rt_base_url resolves
empty, rt_call returns 3, autospawn is suppressed. The goal id is bogus
(g-99999-99999, sibling-file convention) as a second net.
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2].parent
SCRIPTS = PROJECT_ROOT / "core" / "scripts"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _runtime_bash import BASH, bash_cmd  # noqa: E402

BOGUS_GOAL = "g-99999-99999"
CLAIM = SCRIPTS / "aspirations-claim.sh"

# The incident argv, verbatim shape: quoted justification after --deviation.
PROSE = "Reclaiming after diagnosing the argv trap"

# Markers that only appear when the wrapper got PAST the parser into the
# daemon/transport path. The refusal text contains none of these.
DAEMON_MARKERS = ("daemon", "rt_call", "rt_curl", "http://", "unreachable")


def _env(port_file):
    env = dict(os.environ)
    # guard-955 / rb-2983: own-cloud box derives S3 keys from env id, not tmp
    # overrides — pin local for ANY test runner. Nothing here should reach a
    # backend at all; the pin is the belt to the bogus-id braces.
    env["STORAGE_BACKEND"] = "local"
    env["RT_NO_AUTOSPAWN"] = "1"
    env["RT_PORT_FILE"] = str(port_file)
    env["MIND_AGENT"] = "alpha"
    return env


def _run_claim(argv, port_file):
    return subprocess.run(
        bash_cmd(CLAIM, *argv),
        capture_output=True,
        text=True,
        input="",
        env=_env(port_file),
        cwd=str(PROJECT_ROOT),
        timeout=120,
    )


def test_prose_justification_after_deviation_refused(tmp_path):
    """The incident shape: rc=1, the swallowed text named, no daemon contact."""
    r = _run_claim([BOGUS_GOAL, "--deviation", "force-override", PROSE],
                   tmp_path / "no-such-port-file")
    assert r.returncode == 1, (
        f"expected parse-time refusal rc=1, got {r.returncode}.\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    assert "looks like prose" in r.stderr
    assert PROSE in r.stderr, (
        "the refusal must echo the swallowed text so the caller sees exactly "
        "which argument mis-bound"
    )
    assert "--deviation takes ONLY the enum code" in r.stderr, (
        "the refusal must name the likely cause, not just reject"
    )
    lowered = (r.stdout + r.stderr).lower()
    for marker in DAEMON_MARKERS:
        assert marker not in lowered, (
            f"found daemon-path marker {marker!r} — the wrapper got past the "
            f"refusal.\nstderr={r.stderr!r}"
        )
    assert r.stdout == ""


def test_unquoted_justification_extra_positional_refused(tmp_path):
    """The unquoted variant: `--deviation <code> reclaiming after diagnosis`.

    'reclaiming' is lowercase and MATCHES ^[a-z0-9_-]+$, so the regex alone
    would bind it as a phantom agent and (pre-fix) silently drop the rest —
    the g-115-3686 phantom-agent class with a team-state row release can never
    clear. The extra-positional refusal is what catches this variant.
    """
    r = _run_claim(
        [BOGUS_GOAL, "--deviation", "force-override",
         "reclaiming", "after", "diagnosis"],
        tmp_path / "no-such-port-file",
    )
    assert r.returncode == 1, (
        f"expected extra-positional refusal rc=1, got {r.returncode}.\n"
        f"stderr={r.stderr!r}"
    )
    assert "unexpected extra positional" in r.stderr
    assert "'after'" in r.stderr, (
        "the refusal must name the first extra token"
    )
    lowered = (r.stdout + r.stderr).lower()
    for marker in DAEMON_MARKERS:
        assert marker not in lowered


def test_third_positional_after_valid_agent_refused(tmp_path):
    """A plain extra positional after goal-id + agent-name is refused loudly
    (previously dropped silently)."""
    r = _run_claim([BOGUS_GOAL, "alpha", "extra-word"],
                   tmp_path / "no-such-port-file")
    assert r.returncode == 1
    assert "unexpected extra positional" in r.stderr
    assert "'extra-word'" in r.stderr


def test_valid_agent_name_passes_the_parser(tmp_path):
    """Control: a legal agent name gets PAST the parser to the daemon path.

    rc is deliberately not pinned (it is the transport's business); the pin is
    that NEITHER refusal fired and the failure that does occur is the daemon
    path's (proving the parser handed off). RT_NO_AUTOSPAWN + nonexistent port
    file keep this hermetic: rt_base_url is empty, autospawn suppressed.
    """
    r = _run_claim([BOGUS_GOAL, "alpha", "--deviation", "force-override"],
                   tmp_path / "no-such-port-file")
    assert "looks like prose" not in r.stderr
    assert "unexpected extra positional" not in r.stderr
    lowered = (r.stdout + r.stderr).lower()
    assert any(m in lowered for m in DAEMON_MARKERS), (
        f"expected the daemon path's own failure text (parser handed off), "
        f"got:\nstdout={r.stdout!r}\nstderr={r.stderr!r}"
    )


def _run_rt_curl(query, port_file):
    """Invoke rt_curl directly in a bash subshell against RT_PORT_FILE."""
    script = (
        "source core/scripts/_paths.sh >/dev/null 2>&1; "
        "source core/scripts/_runtime.sh; "
        f'rt_curl GET "/v1/claim-probe" --query "{query}"'
    )
    return subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        env=_env(port_file),
        cwd=str(PROJECT_ROOT),
        timeout=60,
    )


def test_rt_curl_maps_curl_rc3_to_hard_error_rc2(tmp_path):
    """Fix (b): a malformed URL (spaces in query) is rc=2 + diagnostic, not
    rc=3 — so rt_call never respawns/retries a deterministic local failure.

    Hermetic: the port file names port 1, and curl detects URL-malformed at
    parse time, BEFORE any connection attempt — no listener is ever touched.
    """
    port_file = tmp_path / "daemon.port"
    port_file.write_text("1", encoding="utf-8")
    r = _run_rt_curl("agent=has spaces here", port_file)
    assert r.returncode == 2, (
        f"expected rc=2 (hard error, no retry), got {r.returncode}.\n"
        f"rc=3 is the REVERTED mapping — it sends rt_call into respawn+retry "
        f"against a failure that cannot change.\nstderr={r.stderr!r}"
    )
    assert "URL malformed" in r.stderr
    assert "NEVER SENT" in r.stderr, (
        "the diagnostic must state the request never left the box — that is "
        "what distinguishes this from every 'daemon unreachable' reading"
    )


def test_rt_curl_connection_refused_still_rc3(tmp_path):
    """No widening: a clean query against a dead port is still rc=3
    (curl rc=7, connection refused) — only curl rc=3 got the new mapping."""
    port_file = tmp_path / "daemon.port"
    port_file.write_text("1", encoding="utf-8")
    r = _run_rt_curl("agent=alpha", port_file)
    assert r.returncode == 3, (
        f"expected rc=3 (unreachable), got {r.returncode}.\n"
        f"stderr={r.stderr!r}"
    )
    assert "URL malformed" not in r.stderr
