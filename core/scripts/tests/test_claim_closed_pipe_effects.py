"""A claim that landed finishes its post-claim effects when its reader closes the
pipe early (g-375-137).

WHAT WAS MEASURED. 2026-10-05, on the worker Bodies: 2 of 38 claims in 24 h
committed with no Body row. Both were run as `aspirations-claim.sh ... 2>&1 | head`,
and the sync tick then integrated 19 times under one of those claims, because
tick_claim_probe.py read the missing row as no claim.

WHAT IS PINNED. aspirations-claim.sh runs under set -euo pipefail. It printed the
goal JSON and only then ran _post_claim_effects, so once the reader had gone, the
next write ended the script before the checkpoint anchor, the Body row and the
claim-time diary breadcrumb were written. Now nothing reaches the caller until
those have run. The worker pull's output is held too, so a closed pipe cannot
kill the pull either.

HARNESS. test_rt_call_leading_diag_split.py's shape: the REAL wrapper in a tmp
tree beside the real helpers it sources, a stub _runtime.sh whose rt_call answers
the claim, and stub effect scripts that each log the one write the test looks for.
The team-state-in-flight.sh stub prints to stderr before its write, as the real
one prints "birth carrier written" before the row.

DETERMINISM. The pipe is `head -c 100`, and the output in front of it is padded
past a pipe's default capacity (64 KiB on Linux). The writer cannot finish into
the pipe buffer, so it is still writing when head exits, on every run: the
pre-fix order fails every time, not on a race.

CONTROLS. Unpiped, the same claims print the JSON whole, keep the old output
order and write all three effects (positive control). The pre-fix order, rebuilt
from the shipped text, writes none of them behind the same pipe (sensitivity),
and all of them unpiped, which shows the rebuild itself still works.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare "bash" argv[0])

CORE_SCRIPTS = SCRIPT_DIR.parent
CLAIM = CORE_SCRIPTS / "aspirations-claim.sh"

# The repo's python-invocation convention (see test_claim_checkpoint_anchor.py).
LAUNCHER = "py -3" if sys.platform == "win32" else "python3"

GOAL = "g-999-01"
# Bytes of padding: past any default pipe capacity (64 KiB on Linux), so the
# writer in front of `head -c 100` is still writing when head exits.
PAD = 200_000
HEAD_BYTES = 100
PULL_MARKER = "[iteration-push] PULL-MARKER-LINE (stub)"
ROW_MESSAGE = "body row written (stub)"
EFFECTS = ("checkpoint", "row", "diary")

# rc=3 on the FIRST call only when STUB_FIRST_RC is set (the wrapper's
# autospawn-retry arm). The count lives in a file because rt_call runs in $().
# The reply comes from a file: a padded reply is past the per-string limit of
# an environment variable.
RUNTIME_STUB = textwrap.dedent("""\
    rt_python_launcher() { printf '%s' "${STUB_LAUNCHER}"; }
    rt_url_encode() { printf '%s' "$1"; }
    rt_try_autospawn() { return 0; }
    rt_no_daemon_error() { echo "no daemon: $1" >&2; exit 1; }
    rt_call() {
      local n
      n="$(cat "${STUB_COUNT}" 2>/dev/null || echo 0)"
      n=$((n + 1)); printf '%s' "$n" > "${STUB_COUNT}"
      if [ -n "${STUB_FIRST_RC:-}" ] && [ "$n" -eq 1 ]; then return "${STUB_FIRST_RC}"; fi
      cat "${STUB_REPLY}"
    }
    """)

LOOP_STATE_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    if [ "${1:-}" = "read" ]; then echo null; exit 1; fi
    if [ "${1:-}" = "init" ]; then cat >> "${LOG_DIR}/checkpoint"; printf '\\n' >> "${LOG_DIR}/checkpoint"; fi
    exit 0
    """)

# Prints before and after its write, as the real helper does.
IN_FLIGHT_STUB = textwrap.dedent(f"""\
    #!/usr/bin/env bash
    echo "[team-state-in-flight] birth carrier written (stub)" >&2
    goal=""
    while [ $# -gt 0 ]; do
      case "$1" in --goal-id) goal="$2"; shift 2;; *) shift;; esac
    done
    printf '%s\\n' "$goal" >> "${{LOG_DIR}}/row"
    echo "[team-state-in-flight] {ROW_MESSAGE}: $goal" >&2
    exit 0
    """)

DIARY_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    if [ "${1:-}" = "append" ]; then cat >> "${LOG_DIR}/diary"; printf '\\n' >> "${LOG_DIR}/diary"; fi
    exit 0
    """)

# The pull: its output, padded by PULL_PAD bytes, then a record written only when
# nothing killed it while it printed.
PULL_STUB = textwrap.dedent(f"""\
    #!/usr/bin/env bash
    printf '%s\\n' "{PULL_MARKER}"
    head -c "${{PULL_PAD:-0}}" /dev/zero | tr '\\0' 'p'
    printf '\\n'
    printf '%s\\n' "$*" >> "${{LOG_DIR}}/pull-finished"
    exit 0
    """)


def _tree(tmp_path: Path, text: str) -> Path:
    """tmp PROJECT_ROOT holding the wrapper, its real sourced helpers and stubs."""
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    wrapper = scripts / CLAIM.name
    wrapper.write_text(text, encoding="utf-8", newline="")
    for helper in ("_goal-arg-normalize.sh", "_argv_strict.sh"):
        shutil.copy(CORE_SCRIPTS / helper, scripts / helper)
    stubs = {
        "_runtime.sh": RUNTIME_STUB,
        "loop-state-save.sh": LOOP_STATE_STUB,
        "team-state-in-flight.sh": IN_FLIGHT_STUB,
        "execution-diary.sh": DIARY_STUB,
        "iteration-push.sh": PULL_STUB,
        "board-post.sh": "cat >/dev/null\nexit 0\n",
        "scorer-verdict-gate.py": "import sys\nsys.exit(0)\n",
        "worker_execute.py": "import sys\nsys.stdin.read()\n",
    }
    for stub, body in stubs.items():
        (scripts / stub).write_text(body, encoding="utf-8", newline="")
    # A stub that does not parse fails every call, which reads exactly like a
    # defect in the code under test (test_claim_checkpoint_anchor.py's lesson).
    for stub in [s for s in stubs if s.endswith(".sh")]:
        chk = subprocess.run([BASH, "-n", str(scripts / stub)],
                             capture_output=True, text=True, timeout=60)
        assert chk.returncode == 0, f"stub {stub} does not parse: {chk.stderr}"
    return wrapper


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _run(tmp_path: Path, *, mode: str, text: str | None = None, worker: bool = False,
         first_rc: str = "", pad: int = PAD, pull_pad: int = 0):
    """Run one claim. mode: 'piped' (2>&1 | head -c 100), 'merged' (2>&1, whole)
    or 'plain' (stdout and stderr apart). -> (proc, {log name: goal ids})."""
    wrapper = _tree(tmp_path, text if text is not None else CLAIM.read_text(encoding="utf-8"))
    logs = tmp_path / "logs"
    logs.mkdir()
    reply = tmp_path / "reply.json"
    reply.write_text(json.dumps({"ok": True, "goal": {
        "id": GOAL, "title": "T", "claimed_by": "alpha", "description": "d" * pad}}),
        encoding="utf-8")
    env = dict(os.environ)
    for var in ("MIND_SID", "BODY_WM_PATH", "MIND_GOAL_ID"):
        env.pop(var, None)
    env.update({
        "MIND_AGENT": "alpha",
        "STUB_LAUNCHER": LAUNCHER,
        "STUB_REPLY": str(reply),
        "STUB_FIRST_RC": first_rc,
        "STUB_COUNT": str(tmp_path / "calls"),
        "LOG_DIR": str(logs),
        "PULL_PAD": str(pull_pad),
    })
    if worker:
        env["BODY_WM_PATH"] = str(tmp_path / "body-wm.yaml")
    if mode == "piped":
        cmd = [BASH, "-c", f'"$1" "$2" "$3" 2>&1 | head -c {HEAD_BYTES}',
               "_", BASH, str(wrapper), GOAL]
    elif mode == "merged":
        cmd = [BASH, "-c", '"$1" "$2" "$3" 2>&1', "_", BASH, str(wrapper), GOAL]
    else:
        cmd = [BASH, str(wrapper), GOAL]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                          cwd=str(tmp_path), env=env)
    found = {name: _lines(logs / name) for name in (*EFFECTS, "pull-finished")}
    for name in ("checkpoint", "diary"):
        found[name] = [json.loads(ln)["goal_id"] for ln in found[name]]
    return proc, found


def _pre_fix_order(text: str) -> str:
    """The shipped wrapper with both success paths put back in the pre-fix order:
    print the goal JSON, end the script when that print fails (set -e did), and
    only then run the effects."""
    pat = re.compile(
        r'^(?P<ind>[ \t]*)_effects_out="\$\(_post_claim_effects "\$GOAL_ID" "\$AGENT" '
        r'"\$RESPONSE" 2>&1\)" \|\| true\n'
        r'(?P=ind)_claim_emit "\$RESPONSE" "\$_effects_out" \|\| rc=\$\?\n',
        re.M)
    out, n = pat.subn(
        lambda m: (f'{m["ind"]}_claim_emit "$RESPONSE" "" || exit $?\n'
                   f'{m["ind"]}_post_claim_effects "$GOAL_ID" "$AGENT" "$RESPONSE"\n'),
        text)
    assert n == 2, f"expected both success paths, rebuilt {n}: update this harness"
    return out


# --------------------------------------------------------------------------
# The fix: effects land behind a closed pipe
# --------------------------------------------------------------------------

@pytest.mark.parametrize("first_rc", ["", "3"], ids=["direct", "autospawn-retry"])
def test_effects_land_when_the_reader_closes_the_pipe_early(tmp_path, first_rc):
    proc, found = _run(tmp_path, mode="piped", first_rc=first_rc)
    assert len(proc.stdout) == HEAD_BYTES, "head must cut the output short, or this proves nothing"
    assert proc.stdout.startswith("{"), "the goal JSON still comes first on this path"
    for name in EFFECTS:
        assert found[name] == [GOAL], f"{name} was not written behind a closed pipe"


def test_worker_pull_is_held_and_finishes_behind_a_closed_pipe(tmp_path):
    proc, found = _run(tmp_path, mode="piped", worker=True, pad=0, pull_pad=PAD)
    assert len(proc.stdout) == HEAD_BYTES
    assert proc.stdout.startswith(PULL_MARKER), "the held pull output still prints first"
    assert found["pull-finished"] == ["--no-push"], "the caller's closed pipe killed the pull"
    for name in EFFECTS:
        assert found[name] == [GOAL], f"{name} was not written behind a closed pipe"


# --------------------------------------------------------------------------
# Positive controls: unpiped, nothing changes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("first_rc", ["", "3"], ids=["direct", "autospawn-retry"])
def test_unpiped_claim_prints_the_json_and_writes_every_effect(tmp_path, first_rc):
    proc, found = _run(tmp_path, mode="plain", first_rc=first_rc, pad=10)
    assert proc.returncode == 0, proc.stderr
    goal = json.loads(proc.stdout)
    assert (goal["id"], goal["claimed_by"]) == (GOAL, "alpha")
    for name in EFFECTS:
        assert found[name] == [GOAL], name
    assert ROW_MESSAGE in proc.stderr, "the effects' own output must still reach stderr"


def test_worker_output_keeps_its_order_and_stays_off_stdout(tmp_path):
    merged, found = _run(tmp_path, mode="merged", worker=True, pad=10, pull_pad=1000)
    assert merged.returncode == 0, merged.stdout
    out = merged.stdout
    assert out.index(PULL_MARKER) < out.index('"claimed_by"') < out.index(ROW_MESSAGE), (
        "the pull's output, the goal JSON and the effects' output must print in that order")
    assert found["pull-finished"] == ["--no-push"]

    plain, _ = _run(tmp_path / "plain", mode="plain", worker=True, pad=10, pull_pad=1000)
    assert plain.returncode == 0, plain.stderr
    assert json.loads(plain.stdout)["id"] == GOAL, "stdout must carry the goal JSON alone"
    assert PULL_MARKER in plain.stderr


# --------------------------------------------------------------------------
# Sensitivity: the pre-fix order, behind the same pipe
# --------------------------------------------------------------------------

def test_the_pre_fix_order_loses_every_effect_behind_the_same_pipe(tmp_path):
    proc, found = _run(tmp_path, mode="piped", text=_pre_fix_order(CLAIM.read_text(encoding="utf-8")))
    assert len(proc.stdout) == HEAD_BYTES
    for name in EFFECTS:
        assert found[name] == [], f"{name} was written: the pipe did not reproduce the defect"


def test_the_pre_fix_order_rebuild_is_whole_when_unpiped(tmp_path):
    """The rebuild still claims: the sensitivity test above is not a broken script."""
    proc, found = _run(tmp_path, mode="plain", text=_pre_fix_order(CLAIM.read_text(encoding="utf-8")),
                       pad=10)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["id"] == GOAL
    for name in EFFECTS:
        assert found[name] == [GOAL], name
