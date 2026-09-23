"""test_runtime_write_resend_safety.py — : rt_call must never send a
write twice.

rt_call (core/scripts/_runtime.sh) re-sent the identical request on two paths,
and neither looked at the method:

  (a) the SUCCESS path (g-115-787): after any reply, a stale daemon is
      recycled and the request re-sent so the caller gets fresh-code output.
      For a write the stale daemon had already applied, that is a second write
      (or a spurious 409 over a store that holds the value);
  (b) the rc==3 path: rt_curl returns 3 for EVERY curl failure without a
      reply — connection refused, but also a timeout, a reset or an empty
      reply, where the daemon may have applied the request after the client
      gave up.

Every wrapper then answers rc=3 with rt_try_autospawn and a second rt_call,
so the two layers compound. Measured in production: ONE
aspirations-add-goal.sh call wrote three goals (alpha, 2026-09-23) and
another wrote four, spaced one RT_CURL_TIMEOUT apart (guard-7167).

The rule the fix enforces: send a request again only when the first send
provably did not apply it —
  * a read (GET/HEAD/OPTIONS) the caller did not mark --mutates: running it
    twice writes nothing. board-read.sh --mark-read is a GET that WRITES read
    receipts, and with --unread-only a second send returns an empty set, so it
    passes --mutates and is treated as the write it is;
  * a 4xx refusal, including the routing-404 of a route a stale daemon lacks:
    the daemon decides those before it writes anything;
  * a send that never reached a daemon: no port file, DNS failure,
    connection refused.
Anything else — a 2xx the stale daemon already applied, a 5xx, a timeout, a
reset, an empty reply — is not re-sent. On the no-reply paths rt_call returns
2 instead of 3, because 2 is the code no wrapper retries.

Harness: sources the REAL _runtime.sh and stubs only the I/O leaves (curl,
rt_base_url, rt_check_staleness, rt_ensure_running, rt_try_autospawn), like
test_runtime_staleness_autorestart.py. The curl stub counts sends in a FILE:
rt_curl runs curl inside a command substitution with stderr discarded, so a
variable or an echo to stderr would never reach the test.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1].parent
RUNTIME_SH = PROJECT_ROOT / "core" / "scripts" / "_runtime.sh"

sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402


def _bash_path(p) -> str:
    """C:\\a\\b -> /c/a/b for Git-Bash (msys) consumption."""
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


# Behaviours the curl stub can play, one word per send in STUB_SEQ (the last
# word repeats). \037 is the 0x1f byte of rt_curl's status marker.
_STUBS = r'''
curl() {
    local method=GET prev="" a n behavior
    for a in "$@"; do
        [ "$prev" = "-X" ] && method="$a"
        prev="$a"
    done
    # A body arrives on stdin (--data-binary @-). Drain it so the writer never
    # takes SIGPIPE, which pipefail would report in place of curl's own code.
    case " $* " in *" --data-binary "*) cat >/dev/null;; esac
    n=$(( $(wc -l < "$SENDS") + 1 ))
    echo "$method" >> "$SENDS"
    behavior=$(printf '%s\n' $STUB_SEQ | sed -n "${n}p")
    [ -n "$behavior" ] || behavior=$(printf '%s\n' $STUB_SEQ | tail -n 1)
    case "$behavior" in
        ok200)     printf '{"ok":true,"send":%s}' "$n"; printf '\n\037__RT_STATUS__:200';;
        refuse409) printf '{"error":"refused","send":%s}' "$n"; printf '\n\037__RT_STATUS__:409';;
        err500)    printf '{"error":"internal_error","send":%s}' "$n"; printf '\n\037__RT_STATUS__:500';;
        noroute)   printf 'no route for %s /v1/x' "$method"; printf '\n\037__RT_STATUS__:404';;
        timeout)   return 28;;
        refused)   return 7;;
        empty)     return 52;;
    esac
    return 0
}
rt_base_url() {
    if [ "${STUB_NO_PORT:-0}" = 1 ] && [ ! -e "$SPAWNED" ]; then
        echo ""
    else
        echo "http://127.0.0.1:9"
    fi
}
rt_check_staleness() {
    echo CHECK >> "$TRACE"
    if [ "${STUB_STALE:-0}" = 1 ] && [ "${RT_STALENESS_RESTARTED:-0}" != 1 ]; then
        export RT_STALENESS_RESTART_PENDING=1
    fi
}
rt_ensure_running() {
    echo ENSURE >> "$TRACE"
    touch "$SPAWNED"
    if [ "${RT_STALENESS_RESTART_PENDING:-0}" = 1 ]; then
        export RT_STALENESS_RESTARTED=1
        unset RT_STALENESS_RESTART_PENDING
    fi
    return 0
}
rt_try_autospawn() { echo AUTOSPAWN >> "$TRACE"; return 0; }
'''

# One call the way a wrapper makes it: capture stdout, keep the rc.
_CALL = '''
rc=0; out=$(rt_call {method} /v1/x {body}) || rc=$?
printf 'RC=%s\\n' "$rc"; printf 'OUT=%s\\n' "$out"
'''

# The shape every wrapper shares (aspirations-add-goal.sh, board-post.sh,
# guardrails-add.sh, ...): rc=3 -> rt_try_autospawn -> rt_call again.
_WRAPPER = '''
rc=0; out=$(rt_call {method} /v1/x {body}) || rc=$?
if [ "$rc" = 3 ] && rt_try_autospawn; then
    rc=0; out=$(rt_call {method} /v1/x {body}) || rc=$?
fi
printf 'RC=%s\\n' "$rc"; printf 'OUT=%s\\n' "$out"
'''


class Result:
    def __init__(self, proc, sends, trace):
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.sends = sends
        self.trace = trace

    def value(self, key):
        for line in self.stdout.splitlines():
            if line.startswith(key + "="):
                return line[len(key) + 1:]
        raise AssertionError(f"{key}= missing from stdout:\n{self.stdout}\n{self.stderr}")

    def dump(self):
        return f"sends={self.sends} trace={self.trace}\nstdout:\n{self.stdout}\nstderr:\n{self.stderr}"


def _run(tmp_path, template, method, seq, stale=False, no_port=False, extra=""):
    body = "--body-string '{\"a\":1}'" if method != "GET" else ""
    body = f"{body} {extra}".strip()
    sends = tmp_path / "sends"
    trace = tmp_path / "trace"
    sends.write_text("")
    trace.write_text("")
    harness = (
        "set -uo pipefail\n"
        f'export PROJECT_ROOT="{_bash_path(PROJECT_ROOT)}"\n'
        # PROJECT_ROOT pre-set -> _runtime.sh skips _paths.sh. RT_DIR keeps the
        # elapsed-time record rt_curl writes on a failed send out of the live
        # mind_api/state.
        f'export RT_DIR="{_bash_path(tmp_path / "rt")}"\n'
        f'source "{_bash_path(RUNTIME_SH)}"\n'
        f'SENDS="{_bash_path(sends)}"\n'
        f'TRACE="{_bash_path(trace)}"\n'
        f'SPAWNED="{_bash_path(tmp_path / "spawned")}"\n'
        f"STUB_SEQ='{seq}'\n"
        f"STUB_STALE={1 if stale else 0}\n"
        f"STUB_NO_PORT={1 if no_port else 0}\n"
        "unset RT_STALENESS_RESTART_PENDING RT_STALENESS_RESTARTED RT_NO_AUTOSPAWN 2>/dev/null || true\n"
        + _STUBS
        + template.format(method=method, body=body)
    )
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n") as fh:
        fh.write(harness)
        script = fh.name
    try:
        proc = subprocess.run(
            [BASH, _bash_path(script)],
            capture_output=True, text=True, timeout=60, cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
        )
    finally:
        os.unlink(script)
    return Result(proc, sends.read_text().split(), trace.read_text().split())


# ─────────── success path: a stale daemon answered the first send ───────────

def test_stale_2xx_write_is_not_resent(tmp_path):
    """The  shape: the stale daemon APPLIED the write (2xx). Sending
    it again after the recycle wrote a second copy, or drew a 409 over a store
    that already held the value (the d7c3e2ada2 expect-sha256 case)."""
    r = _run(tmp_path, _CALL, "POST", "ok200 refuse409", stale=True)
    assert r.sends == ["POST"], r.dump()
    assert r.value("RC") == "0", r.dump()
    assert '"send":1' in r.value("OUT"), r.dump()
    # The daemon is still recycled, so the NEXT call runs fresh code.
    assert "ENSURE" in r.trace, r.dump()


def test_stale_2xx_read_is_still_resent(tmp_path):
    """'s heal survives for reads: the caller gets fresh-code output."""
    r = _run(tmp_path, _CALL, "GET", "ok200 ok200", stale=True)
    assert r.sends == ["GET", "GET"], r.dump()
    assert r.value("RC") == "0", r.dump()
    assert '"send":2' in r.value("OUT"), r.dump()


def test_stale_4xx_write_is_resent_and_the_stale_body_is_dropped(tmp_path):
    """A stale daemon REFUSED the write (e.g. an allowlist that predates the
    field), so nothing was written and the fresh daemon must judge it. Only the
    fresh reply may reach the caller: rt_curl used to print the stale error body
    to stderr before the re-send, so a 2>&1 caller saw two verdicts while rc
    said 0 (zeta 2026-09-23, g-115-10664)."""
    r = _run(tmp_path, _CALL, "POST", "refuse409 ok200", stale=True)
    assert r.sends == ["POST", "POST"], r.dump()
    assert r.value("RC") == "0", r.dump()
    assert '"send":2' in r.value("OUT"), r.dump()
    assert '"send":1' not in r.stderr, r.dump()


def test_stale_5xx_write_is_not_resent(tmp_path):
    """A 5xx says nothing about whether the write landed (a serialize failure
    comes AFTER the write), so it is reported, not re-sent."""
    r = _run(tmp_path, _CALL, "POST", "err500 ok200", stale=True)
    assert r.sends == ["POST"], r.dump()
    assert r.value("RC") == "2", r.dump()
    assert '"send":1' in r.stderr, r.dump()


# ──────────────── no reply: the rc==3 path of rt_call ────────────────

def test_timed_out_write_is_not_resent_and_returns_2(tmp_path):
    """A timeout means the CLIENT gave up; the daemon may still apply the write.
    rc 2 (not 3) keeps every wrapper from retrying it."""
    r = _run(tmp_path, _CALL, "POST", "timeout ok200")
    assert r.sends == ["POST"], r.dump()
    assert r.value("RC") == "2", r.dump()
    assert "ENSURE" not in r.trace, r.dump()
    assert "g-115-8129" in r.stderr, r.dump()


def test_empty_reply_write_is_not_resent(tmp_path):
    """The daemon closed the connection with no reply — the shape of a daemon
    recycled mid-request — after it may have applied the write."""
    r = _run(tmp_path, _CALL, "POST", "empty ok200")
    assert r.sends == ["POST"], r.dump()
    assert r.value("RC") == "2", r.dump()


def test_refused_write_is_resent(tmp_path):
    """Connection refused: the request never reached a daemon, so spawning one
    and sending is the only way it happens at all."""
    r = _run(tmp_path, _CALL, "POST", "refused ok200")
    assert r.sends == ["POST", "POST"], r.dump()
    assert r.value("RC") == "0", r.dump()
    assert "ENSURE" in r.trace, r.dump()


def test_refused_then_timed_out_write_returns_2(tmp_path):
    """The re-send itself timed out, so IT may have landed: no rc 3 either."""
    r = _run(tmp_path, _CALL, "POST", "refused timeout")
    assert r.sends == ["POST", "POST"], r.dump()
    assert r.value("RC") == "2", r.dump()


def test_routing_404_write_is_resent(tmp_path):
    """A stale daemon that lacks the route executed nothing."""
    r = _run(tmp_path, _CALL, "POST", "noroute ok200", stale=True)
    assert r.sends == ["POST", "POST"], r.dump()
    assert r.value("RC") == "0", r.dump()


def test_no_port_write_spawns_then_sends_once(tmp_path):
    """No port file: nothing was sent before the spawn."""
    r = _run(tmp_path, _CALL, "POST", "ok200", no_port=True)
    assert r.sends == ["POST"], r.dump()
    assert r.value("RC") == "0", r.dump()
    assert "ENSURE" in r.trace, r.dump()


def test_timed_out_read_is_still_resent(tmp_path):
    """Reads keep the retry: running a read twice writes nothing."""
    r = _run(tmp_path, _CALL, "GET", "timeout ok200")
    assert r.sends == ["GET", "GET"], r.dump()
    assert r.value("RC") == "0", r.dump()


# ──────────── a GET that writes: board-read.sh --mark-read ────────────

def test_stale_2xx_mutating_read_is_not_resent(tmp_path):
    """--mark-read appends read receipts, so the stale daemon's 2xx CONSUMED
    the unread set; a re-send would hand the caller the fresh daemon's EMPTY
    reply instead. Same input as test_stale_2xx_read_is_still_resent — the
    flag alone flips the outcome."""
    r = _run(tmp_path, _CALL, "GET", "ok200 ok200", stale=True, extra="--mutates")
    assert r.sends == ["GET"], r.dump()
    assert r.value("RC") == "0", r.dump()
    assert '"send":1' in r.value("OUT"), r.dump()
    assert "ENSURE" in r.trace, r.dump()


def test_timed_out_mutating_read_returns_2(tmp_path):
    """Same input as test_timed_out_read_is_still_resent, with --mutates."""
    r = _run(tmp_path, _CALL, "GET", "timeout ok200", extra="--mutates")
    assert r.sends == ["GET"], r.dump()
    assert r.value("RC") == "2", r.dump()
    assert "WRITE OUTCOME UNKNOWN" in r.stderr, r.dump()


# ──────────────── the wrapper layer: the retry ladder ────────────────

def test_wrapper_sends_a_timed_out_write_once(tmp_path):
    """The measured ladder: rt_call re-sent once, then the wrapper's rc=3 branch
    ran rt_try_autospawn and rt_call again, which re-sent again — four sends
    from one call (guard-7167). One send, and the wrapper never retries."""
    r = _run(tmp_path, _WRAPPER, "POST", "timeout timeout timeout timeout")
    assert r.sends == ["POST"], r.dump()
    assert "AUTOSPAWN" not in r.trace, r.dump()
    assert r.value("RC") == "2", r.dump()


_BOARD_READ = PROJECT_ROOT / "core" / "scripts" / "board-read.sh"

# A curl that answers the daemon health probe and plays STUB_SEQ for every
# other request. Exported with `export -f`, so the REAL board-read.sh — its own
# bash process — resolves `curl` to it.
_EXPORTED_CURL = r'''
curl() {
    local a url="" n behavior
    for a in "$@"; do case "$a" in http://*) url="$a";; esac; done
    case "$url" in */v1/admin/health) printf '{"ok":true}'; return 0;; esac
    n=$(( $(wc -l < "$STUB_SENDS") + 1 ))
    echo GET >> "$STUB_SENDS"
    behavior=$(printf '%s\n' $STUB_SEQ | sed -n "${n}p")
    [ -n "$behavior" ] || behavior=$(printf '%s\n' $STUB_SEQ | tail -n 1)
    case "$behavior" in
        ok200)   printf '{"id":"msg-%s"}' "$n"; printf '\n\037__RT_STATUS__:200';;
        timeout) return 28;;
    esac
    return 0
}
export -f curl
'''


def _board_read(tmp_path, flags, seq):
    tmp_path.mkdir(parents=True, exist_ok=True)
    rt = tmp_path / "rt"
    rt.mkdir()
    (rt / "daemon.port").write_text("9")
    sends = tmp_path / "sends"
    sends.write_text("")
    harness = (
        "set -uo pipefail\n"
        # RT_NO_AUTOSPAWN: no path in this test may start a real daemon.
        f'export RT_DIR="{_bash_path(rt)}" RT_NO_AUTOSPAWN=1 STORAGE_BACKEND=local\n'
        f'export STUB_SENDS="{_bash_path(sends)}" STUB_SEQ=\'{seq}\'\n'
        + _EXPORTED_CURL
        + f'rc=0; out=$(bash "{_bash_path(_BOARD_READ)}" --channel coordination {flags}) || rc=$?\n'
        + "printf 'RC=%s\\n' \"$rc\"; printf 'OUT=%s\\n' \"$out\"\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n") as fh:
        fh.write(harness)
        script = fh.name
    try:
        proc = subprocess.run(
            [BASH, _bash_path(script)],
            capture_output=True, text=True, timeout=60, cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
        )
    finally:
        os.unlink(script)
    return Result(proc, sends.read_text().split(), [])


def test_board_read_sends_a_timed_out_mark_read_once(tmp_path):
    """The call site, on an input where the two paths MUST disagree
    (guard-3292): the same timed-out board read is re-sent without
    --mark-read, and sent ONCE with it."""
    plain = _board_read(tmp_path / "plain", "", "timeout ok200")
    assert plain.sends == ["GET", "GET"], plain.dump()
    assert plain.value("RC") == "0", plain.dump()
    assert "msg-2" in plain.value("OUT"), plain.dump()

    marked = _board_read(tmp_path / "marked", "--unread-only --mark-read", "timeout ok200")
    assert marked.sends == ["GET"], marked.dump()
    assert marked.value("RC") == "1", marked.dump()
    assert "WRITE OUTCOME UNKNOWN" in marked.stderr, marked.dump()


# ──────────────── rt_curl's own contract is unchanged ────────────────

def test_rt_curl_contract_unchanged(tmp_path):
    """rt_curl still prints a 2xx body on stdout, a non-2xx body on stderr,
    and nothing for rc 3 — direct callers and the existing tests rely on it."""
    tpl = '''
rc=0; out=$(rt_curl {method} /v1/x {body}) || rc=$?
printf 'RC=%s\\n' "$rc"; printf 'OUT=%s\\n' "$out"
'''
    ok = _run(tmp_path, tpl, "POST", "ok200")
    assert ok.value("RC") == "0" and '"send":1' in ok.value("OUT"), ok.dump()

    tmp2 = tmp_path / "b"
    tmp2.mkdir()
    bad = _run(tmp2, tpl, "POST", "refuse409")
    assert bad.value("RC") == "2" and bad.value("OUT") == "", bad.dump()
    assert '"send":1' in bad.stderr, bad.dump()

    tmp3 = tmp_path / "c"
    tmp3.mkdir()
    gone = _run(tmp3, tpl, "POST", "timeout")
    assert gone.value("RC") == "3" and gone.value("OUT") == "", gone.dump()
    assert gone.sends == ["POST"], gone.dump()


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
