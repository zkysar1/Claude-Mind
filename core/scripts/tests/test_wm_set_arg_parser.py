"""wm-set.sh must take EXACTLY ONE positional and a value on stdin — or refuse loudly.

THE DEFECT (the wm-append.sh twin, g-115-6767 / guard-4437): the wrapper's arg loop was
a bare catch-all, so `wm-set.sh <slot> '<json>'` — the shape a model reaches for when it
has a value in hand — silently made the VALUE the slot name and sent an EMPTY body. The
daemon then replied `{"error": "empty_body", "detail": "value required in request body"}`,
which names neither mistake. Measured 2026-08-29 (coach, zc-03): nine identical retries in
one session, each a model turn.

Two refusals, both at the call site where the offending token can be quoted verbatim and
no round trip is spent: a second positional, and an empty stdin. Each prints the exact
corrected command (`printf '%s' '<value>' | bash core/scripts/wm-set.sh <slot>`).

HERMETIC BY CONSTRUCTION — no daemon, no network, no working-memory write. Each case runs
a COPY of the production wrapper inside a tmp project root whose `_runtime.sh` is a stub
that RECORDS the query string instead of sending it (the wm-append harness, verbatim).

Run:
  STORAGE_BACKEND=local python3 -m pytest core/scripts/tests/test_wm_set_arg_parser.py -q
"""
from __future__ import annotations

import shutil
import sys
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _bash_helpers import BASH  # noqa: E402

CORE_ROOT = SCRIPT_DIR.parent.parent
WRAPPER = CORE_ROOT / "scripts" / "wm-set.sh"

_RUNTIME_STUB = """\
rt_url_encode() { printf '%s' "$1"; }
rt_call() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --query) printf '%s' "$2" > "$RT_STUB_LOG"; shift 2;;
            --body-string) printf '%s' "$2" > "$RT_STUB_LOG.body"; shift 2;;
            *) shift;;
        esac
    done
    printf '%s' '{"ok": true}'
    return 0
}
rt_try_autospawn() { return 1; }
rt_no_daemon_error() { echo "stub: rt_no_daemon_error reached" >&2; exit 1; }
"""


def _sandbox(tmp_path: Path, mutate: bool = False) -> Path:
    """A tmp project root holding a copy of the wrapper plus the stub runtime.

    `mutate=True` restores the pre-fix shape in the copy (bare catch-all, no stdin
    check): the positive control that proves the harness runs the wrapper at all
    (guard-2421).
    """
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WRAPPER, scripts / "wm-set.sh")
    (scripts / "_runtime.sh").write_text(_RUNTIME_STUB, encoding="utf-8")
    if mutate:
        src = (scripts / "wm-set.sh").read_text(encoding="utf-8")
        start = src.index("        *)\n            if [ -n \"$SLOT\" ]; then")
        end = src.index("            SLOT=\"$1\"; shift;;", start)
        assert start < end, "could not locate the second-positional refusal to mutate"
        src = src[:start] + "        *)\n" + src[end:]
        src = src.replace(
            "if [ -z \"$BODY\" ]; then\n"
            "    echo \"Error: no value on stdin for slot '$SLOT'. Run: printf '%s' "
            "'<json-or-scalar>' | bash core/scripts/wm-set.sh $SLOT\" >&2\n"
            "    exit 1\n"
            "fi\n",
            "",
        )
        assert "no value on stdin" not in src, "the stdin check must be removed by the mutation"
        (scripts / "wm-set.sh").write_text(src, encoding="utf-8")
    return scripts


def _run(scripts: Path, args, stdin: str = ""):
    log = scripts.parent.parent / "rt-stub.log"
    for p in (log, Path(str(log) + ".body")):
        if p.exists():
            p.unlink()
    proc = subprocess.run(
        [BASH, (scripts / "wm-set.sh").as_posix(), *args],
        input=stdin, text=True, capture_output=True, timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(scripts.parent.parent),
             "RT_STUB_LOG": str(log)},
    )
    sent = log.read_text(encoding="utf-8") if log.exists() else None
    return proc, sent


def test_value_as_second_positional_is_refused_with_the_corrected_command(tmp_path):
    scripts = _sandbox(tmp_path)
    proc, sent = _run(scripts, ["goals_completed_this_session", '[{"goal_id": "g-1"}]'])
    assert proc.returncode != 0
    assert "ONE positional" in proc.stderr, proc.stderr
    assert "printf '%s' '[{\"goal_id\": \"g-1\"}]' | bash core/scripts/wm-set.sh goals_completed_this_session" in proc.stderr, proc.stderr
    assert sent is None, "the refusal must happen before the transport is reached"


def test_empty_stdin_is_refused_before_the_daemon(tmp_path):
    scripts = _sandbox(tmp_path)
    proc, sent = _run(scripts, ["goals_completed_this_session"], stdin="")
    assert proc.returncode != 0
    assert "no value on stdin" in proc.stderr and "wm-set.sh goals_completed_this_session" in proc.stderr
    assert sent is None


def test_the_documented_shape_reaches_the_transport_with_the_right_slot(tmp_path):
    scripts = _sandbox(tmp_path)
    proc, sent = _run(scripts, ["goals_completed_this_session"], stdin='["g-1"]')
    assert proc.returncode == 0, proc.stderr
    assert sent == "slot=goals_completed_this_session"
    assert Path(str(scripts.parent.parent / "rt-stub.log") + ".body").read_text(encoding="utf-8") == '["g-1"]'


def test_positive_control_pre_fix_wrapper_sends_the_value_as_the_slot(tmp_path):
    """Without the fix the second positional silently becomes the slot and an empty body
    goes out — the harness must SEE that, or the refusals above prove nothing."""
    scripts = _sandbox(tmp_path, mutate=True)
    proc, sent = _run(scripts, ["goals_completed_this_session", '[{"goal_id": "g-1"}]'])
    assert proc.returncode == 0
    assert sent == 'slot=[{"goal_id": "g-1"}]'
    assert Path(str(scripts.parent.parent / "rt-stub.log") + ".body").read_text(encoding="utf-8") == ""
