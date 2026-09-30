"""A write that LANDED must not exit 1 because rt_call's stale-daemon lines came
first (g-115-7210).

WHAT IS PINNED. Since ce44c5c2e5 (2026-07-18), rt_call buffers the reply and
hands it to the caller LAST, and g-115-8129 kept that order. So the `[runtime]`
lines it prints for a stale daemon (the staleness WARNING and, since g-115-8129,
"request NOT sent again" for a write) reach a `2>&1` capture BEFORE the body.
aspirations-update-goal.sh and aspirations-claim.sh parsed that capture with
raw_decode at offset 0, which tolerates only TRAILING residue (g-115-769):
- update-goal exited 1 with a JSONDecodeError traceback over a status write that
  had landed;
- claim exited 1 under `set -e` before `_post_claim_effects`, so a claim that had
  landed wrote no checkpoint anchor, no in-flight stamp and no board post.
Both wrappers now pass every line ahead of the first line opening with `{` to
stderr unchanged, and parse the body.

HARNESS. Same shape as test_claim_checkpoint_anchor.py: the REAL wrapper is
copied into a tmp tree beside the real helpers it sources, and `_runtime.sh` is
a stub whose rt_call prints the prefix to stderr FIRST and the body after it, in
rt_call's own order. Only collaborators are stubbed.

COVERAGE (guard-1451 / guard-1660). Sensitivity: each wrapper with its split
excised must go red on the same input (the defect, reproduced). Specificity: a
clean body, a trailing-residue body and an rc=2 refusal behave as before.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _bash_helpers import BASH  # noqa: E402

CORE_SCRIPTS = SCRIPT_DIR.parent
UPDATE = CORE_SCRIPTS / "aspirations-update-goal.sh"
CLAIM = CORE_SCRIPTS / "aspirations-claim.sh"

# The repo's python-invocation convention (see test_claim_checkpoint_anchor.py).
LAUNCHER = "py -3" if sys.platform == "win32" else "python3"

# rt_call's own lines, as _runtime.sh prints them (rt_check_staleness, and the
# non-resend-safe branch of rt_call).
STALE = ("[runtime] WARNING: daemon is running stale code (sha 1111aaaa, on-disk "
         "2222bbbb). Auto-restart will fire on the next rt_ensure_running call "
         "(one-shot).")
NOT_RESENT = ("[runtime] POST /v1/aspirations/update-goal was answered by the stale "
              "daemon (HTTP 200); daemon recycled, request NOT sent again (g-115-8129)")
PREFIX = STALE + "\n" + NOT_RESENT

GOAL = "g-999-01"

# rc=3 on the FIRST call only when STUB_FIRST_RC is set (the wrapper's
# autospawn-retry arm); the count lives in a file because rt_call runs in $().
RUNTIME_STUB = textwrap.dedent("""\
    rt_python_launcher() { printf '%s' "${STUB_LAUNCHER}"; }
    rt_url_encode() { printf '%s' "$1"; }
    rt_try_autospawn() { return 0; }
    rt_no_daemon_error() { echo "no daemon: $1" >&2; exit 1; }
    rt_call() {
      local n=0
      if [ -n "${STUB_FIRST_RC:-}" ]; then
        n="$(cat "${STUB_COUNT}" 2>/dev/null || echo 0)"
        n=$((n + 1)); printf '%s' "$n" > "${STUB_COUNT}"
        if [ "$n" -eq 1 ]; then return "${STUB_FIRST_RC}"; fi
      fi
      if [ -n "${STUB_PREFIX:-}" ]; then printf '%s\\n' "${STUB_PREFIX}" >&2; fi
      if [ "${STUB_RC:-0}" = "2" ]; then
        printf '%s' "${STUB_RESPONSE}" >&2
      else
        printf '%s' "${STUB_RESPONSE}"
      fi
      if [ -n "${STUB_SUFFIX:-}" ]; then printf '\\n%s\\n' "${STUB_SUFFIX}" >&2; fi
      return "${STUB_RC:-0}"
    }
    """)

LOOP_STATE_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    if [ "${1:-}" = "read" ]; then echo null; exit 1; fi
    if [ "${1:-}" = "init" ]; then cat >> "${CP_LOG}"; printf '\\n' >> "${CP_LOG}"; fi
    exit 0
    """)


def _tree(tmp_path: Path, name: str, text: str) -> Path:
    """tmp PROJECT_ROOT holding the wrapper, its real sourced helpers and stubs."""
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / name).write_text(text, encoding="utf-8", newline="")
    for helper in ("_goal-arg-normalize.sh", "_argv_strict.sh"):
        shutil.copy(CORE_SCRIPTS / helper, scripts / helper)
    stubs = {
        "_runtime.sh": RUNTIME_STUB,
        "loop-state-save.sh": LOOP_STATE_STUB,
        "scorer-verdict-gate.py": "import sys\nsys.exit(0)\n",
    }
    for quiet in ("team-state-in-flight.sh", "board-post.sh", "execution-diary.sh",
                  "iteration-push.sh"):
        stubs[quiet] = "exit 0\n"
    for stub, body in stubs.items():
        (scripts / stub).write_text(body, encoding="utf-8", newline="")
    # A stub that does not parse fails every call, which reads exactly like a
    # defect in the code under test (test_claim_checkpoint_anchor.py's lesson).
    for stub in [s for s in stubs if s.endswith(".sh")]:
        chk = subprocess.run([BASH, "-n", str(scripts / stub)],
                             capture_output=True, text=True, timeout=60)
        assert chk.returncode == 0, f"stub {stub} does not parse: {chk.stderr}"
    return scripts / name


def _run(tmp_path: Path, wrapper: Path, args: list[str], response: dict, *,
         text: str | None = None, rc: int = 0, prefix: str = PREFIX,
         suffix: str = "", first_rc: str = ""):
    script = _tree(tmp_path, wrapper.name,
                   text if text is not None else wrapper.read_text(encoding="utf-8"))
    log = tmp_path / "init.log"
    log.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "MIND_AGENT": "alpha",
        "STUB_LAUNCHER": LAUNCHER,
        "STUB_RESPONSE": json.dumps(response),
        "STUB_RC": str(rc),
        "STUB_PREFIX": prefix,
        "STUB_SUFFIX": suffix,
        "STUB_FIRST_RC": first_rc,
        "STUB_COUNT": str(tmp_path / "calls"),
        "CP_LOG": str(log),
    })
    for var in ("MIND_SID", "BODY_WM_PATH"):
        env.pop(var, None)
    proc = subprocess.run([BASH, str(script), *args], capture_output=True, text=True,
                          timeout=120, cwd=str(tmp_path), env=env)
    anchors = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()
               if ln.strip()]
    return proc, anchors


def _update_reply(status: str = "completed") -> dict:
    return {"ok": True, "goal": {"id": GOAL, "status": status}}


def _claim_reply() -> dict:
    return {"ok": True, "goal": {"id": GOAL, "title": "T", "claimed_by": "alpha"}}


def _without_update_split() -> str:
    """update-goal with both `_split_leading_diag` calls removed (the pre-fix parse)."""
    lines = UPDATE.read_text(encoding="utf-8").splitlines(keepends=True)
    calls = [i for i, ln in enumerate(lines) if ln.strip() == "_split_leading_diag"]
    assert len(calls) == 2, (
        f"expected 2 _split_leading_diag call lines, found {len(calls)}: the split "
        "was renamed or moved; update this harness")
    return "".join(ln for i, ln in enumerate(lines) if i not in calls)


def _without_claim_split() -> str:
    """claim with its split block excised (the pre-fix parse)."""
    lines = CLAIM.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith('if [ "${RESPONSE:0:1}" != "{" ]')), None)
    assert start is not None, "claim split block not found; update this harness"
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "fi")
    return "".join(lines[:start] + lines[end + 1:])


# --------------------------------------------------------------------------
# aspirations-update-goal.sh
# --------------------------------------------------------------------------

@pytest.mark.parametrize("first_rc", ["", "3"], ids=["direct", "autospawn-retry"])
def test_landed_write_behind_stale_daemon_lines_exits_0(tmp_path, first_rc):
    proc, _ = _run(tmp_path, UPDATE, [GOAL, "status", "completed"], _update_reply(),
                   first_rc=first_rc)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"id": GOAL, "status": "completed"}
    assert STALE in proc.stderr and NOT_RESENT in proc.stderr, (
        "the runtime's own lines must still reach the operator on stderr")
    assert "Traceback" not in proc.stderr


@pytest.mark.parametrize("first_rc", ["", "3"], ids=["direct", "autospawn-retry"])
def test_update_without_the_split_reproduces_the_false_negative(tmp_path, first_rc):
    """The defect, reproduced: same reply, pre-fix parse, rc=1 over a landed write."""
    proc, _ = _run(tmp_path, UPDATE, [GOAL, "status", "completed"], _update_reply(),
                   text=_without_update_split(), first_rc=first_rc)
    assert proc.returncode == 1, proc.stderr
    assert "JSONDecodeError" in proc.stderr


def test_clean_reply_is_unchanged(tmp_path):
    proc, _ = _run(tmp_path, UPDATE, [GOAL, "status", "completed"], _update_reply(),
                   prefix="")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"id": GOAL, "status": "completed"}
    assert "[runtime]" not in proc.stderr


def test_trailing_residue_still_tolerated(tmp_path):
    """'s case: the runtime line AFTER the body still parses."""
    proc, _ = _run(tmp_path, UPDATE, [GOAL, "status", "completed"], _update_reply(),
                   prefix="", suffix=STALE)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["status"] == "completed"
    assert STALE in proc.stderr


def test_refusal_behind_stale_daemon_lines_still_reads_refused(tmp_path):
    proc, _ = _run(tmp_path, UPDATE, [GOAL, "status", "completed"],
                   {"error": "goal_terminal"}, rc=2)
    assert proc.returncode == 1
    assert "REFUSED (goal_terminal)" in proc.stderr
    assert STALE in proc.stderr


# --------------------------------------------------------------------------
# aspirations-claim.sh
# --------------------------------------------------------------------------

def test_landed_claim_behind_stale_daemon_lines_runs_post_claim_effects(tmp_path):
    proc, anchors = _run(tmp_path, CLAIM, [GOAL], _claim_reply())
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["claimed_by"] == "alpha"
    assert [a["goal_id"] for a in anchors] == [GOAL], (
        "_post_claim_effects did not anchor the checkpoint for a claim that landed")
    assert STALE in proc.stderr


def test_claim_without_the_split_skips_post_claim_effects(tmp_path):
    """The claim-side defect, reproduced: rc=1 and no effects for a landed claim."""
    proc, anchors = _run(tmp_path, CLAIM, [GOAL], _claim_reply(),
                         text=_without_claim_split())
    assert proc.returncode == 1, proc.stderr
    assert "JSONDecodeError" in proc.stderr
    assert anchors == []
