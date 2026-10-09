"""board-read.sh --unhandled-only sends the query the daemon's HANDLED filter reads ().

The daemon tests drive /v1/board/read directly, so they cannot see the wrapper. A wrapper
that sent only unread_only would pass every daemon test and silently put the directive ACK
read back on the SHOWN key, which is the 2026-08-11 failure. This pins the query the real
wrapper sends. --unread-only alone is the control: it must keep the SHOWN filter and must
NOT carry unhandled_only, so every other caller keeps its behaviour.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[2]
BOARD_READ = PROJECT_ROOT / "core" / "scripts" / "board-read.sh"

sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402


def _bash_path(p) -> str:
    """C:\\a\\b -> /c/a/b for Git-Bash (msys) consumption."""
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


# A curl that answers the daemon health probe and records the URL of every other
# request. Exported with `export -f`, so the REAL board-read.sh resolves `curl` to it.
_RECORDING_CURL = r'''
curl() {
    local a url=""
    for a in "$@"; do case "$a" in http://*) url="$a";; esac; done
    case "$url" in */v1/admin/health) printf '{"ok":true}'; return 0;; esac
    echo "$url" >> "$STUB_URLS"
    printf '{"id":"msg-1"}'; printf '\n\037__RT_STATUS__:200'
    return 0
}
export -f curl
'''


def _query_sent(tmp_path, flags):
    rt = tmp_path / "rt"
    rt.mkdir(parents=True)
    (rt / "daemon.port").write_text("9")
    urls = tmp_path / "urls"
    urls.write_text("")
    harness = (
        "set -uo pipefail\n"
        # RT_NO_AUTOSPAWN: no path in this test may start a real daemon.
        f'export RT_DIR="{_bash_path(rt)}" RT_NO_AUTOSPAWN=1 STORAGE_BACKEND=local\n'
        f'export STUB_URLS="{_bash_path(urls)}"\n'
        + _RECORDING_CURL
        + f'bash "{_bash_path(BOARD_READ)}" --channel coordination --type directive {flags} >/dev/null\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n") as fh:
        fh.write(harness)
        script = fh.name
    try:
        proc = subprocess.run([BASH, _bash_path(script)], capture_output=True, text=True,
                              timeout=60, cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL)
    finally:
        os.unlink(script)
    sent = [u for u in urls.read_text().split() if u]
    assert len(sent) == 1, (sent, proc.stdout, proc.stderr)
    return sent[0].split("?", 1)[1].split("&")


def test_unhandled_only_sends_the_handled_filter_with_the_shown_filter_beside_it(tmp_path):
    q = _query_sent(tmp_path, "--since 24h --unhandled-only --mark-read --json")
    # unhandled_only is the HANDLED filter; unread_only rides beside it so a daemon that
    # predates the flag degrades to the old SHOWN dedup instead of to no filter at all.
    assert "unhandled_only=1" in q and "unread_only=1" in q and "mark_read=1" in q, q


def test_unread_only_alone_keeps_the_shown_filter_and_gains_no_handled_filter(tmp_path):
    q = _query_sent(tmp_path, "--since 24h --unread-only --mark-read --json")
    assert "unread_only=1" in q and "mark_read=1" in q, q
    assert "unhandled_only=1" not in q, q
