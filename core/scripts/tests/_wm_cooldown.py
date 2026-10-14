"""The one body of the `_isolate_wm_cooldown` autouse fixture (-a).

Three test files carried a COPY of this fixture -- the origin,
test_post_state_update_gate_committed_files_only.py, and the two files that copied
it from there (..._mode_only, ..._daemon_dispatch). Each keeps a thin autouse
wrapper so the fixture stays visible where it applies, and calls
`neutralize_cooldown(AGENT)` here, so a change to what neutralizing means is made
once instead of three times (the copying is how g-115-4887 found the hazard in the
first place).

WHAT IT NEUTRALIZES. post-state-update-gate.sh reads its cooldown from the bound
agent's own working memory (`fresh_eyes_last_fire`) and WRITES it when the gate
fires, so a firing case would otherwise seed a cooldown for the next test. The
bound agent is an OFF-ROSTER test agent, so the suite never touches a live fleet
member's working memory (g-115-4887). The old fixture saved, null-wrote, then
restored the slot for the LIVE agent `zeta` under `except Exception: pass` with no
`finally`: a crash between neutralize and restore destroyed a real list of
fresh-eyes review records permanently and silently. A dedicated test agent became
possible when `_paths.sh` started falling through to the first available conf for
WORLD/META while AGENT_DIR still points at the NAMED agent (g-115-960 /
g-115-6417): `MIND_AGENT=testagent wm-read.sh fresh_eyes_last_fire --json`
returns `{}` on a real, off-roster agent dir. No save/restore remains -- the
dangerous half was deleted, not hardened, because a test agent's scratch slot has
nothing worth preserving.

WHEN THERE IS NO DAEMON THERE IS NOTHING TO NEUTRALIZE (g-374-628). wm-set.sh is
daemon-only (no Python CLI fallback, no-python-cli-fallback.md), so in a runtime
with no reachable daemon it exits 1 with the shared `rt_no_daemon_error` text. That
is the shape of a deployment whose pytest run has a scratch RUNTIME_DIR and refuses
to spawn a daemon from inside a test (g-115-3329) -- and of
deployment-shaped-run.py's runtime. The unconditional assert below failed all 12
tests of the three files at SETUP there. Skipping the neutralize is safe because
the gate's own use of the slot is fail-open on both sides
(`wm-read.sh ... || echo "null"` and `wm-set.sh ... || true`), so with no daemon the
cooldown reads null on every case: deterministic with no neutralize at all.

ONLY THAT ONE FAILURE IS TOLERATED (g-374-628-a). A daemon that is up and failing, a contended or
slow one (rt_no_daemon_error prints different text for those), a missing script, a
timeout -- each still fails loudly, because then the firing cases below WOULD be
non-deterministic.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from _bash_helpers import BASH as GIT_BASH

WM_SET_SH = Path(__file__).resolve().parent.parent / "wm-set.sh"

# First line rt_no_daemon_error() (core/scripts/_runtime.sh) prints when the daemon
# cannot be reached and could not be spawned. Its other branches (a daemon that is
# slow or contended) print different text, so this phrase is what separates "no
# daemon in this runtime" from a daemon that is present and failing.
NO_DAEMON_TEXT = "daemon is unreachable and could not be auto-spawned"


def _to_bash_path(p) -> str:
    """Convert a Windows path to Git-Bash mount-prefix form (/c/...); no-op on POSIX."""
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def neutralize_cooldown(agent: str) -> None:
    """Null `agent`'s fresh_eyes_last_fire slot; return quietly when there is no daemon."""
    r = subprocess.run(
        [GIT_BASH, _to_bash_path(WM_SET_SH), "fresh_eyes_last_fire"],
        input="null", capture_output=True, text=True, timeout=30,
        env={**os.environ, "MIND_AGENT": agent},
    )
    if r.returncode != 0 and NO_DAEMON_TEXT in r.stderr:
        return
    assert r.returncode == 0, (
        f"could not neutralize {agent} fresh_eyes_last_fire "
        f"(rc={r.returncode} stderr={r.stderr!r}) -- the firing cases below would "
        f"be non-deterministic, so fail loudly rather than flake"
    )
