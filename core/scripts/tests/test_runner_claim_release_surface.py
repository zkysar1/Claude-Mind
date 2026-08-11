""" regression: runner-claim.sh must SURFACE a released=False DDB release.

The bug (2026-07-04/05 wedge, g-115-1787): while the daemon was wedged, /stop
D6.8's DDB runner-claim release returned {ok:true, released:false} — an
idempotent no-op that the wrapper framed as "release: ok" and exit 0. D6.8's
`|| WARN` never fired, so the stranded RUNNING self-claim was silent and the
NEXT /start hit rc=4 on it.

The fix surfaces released=False loudly: the wrapper prints an UNCONFIRMED line
and exits 5 ("release unconfirmed", distinct from 2=hard daemon error), so
graceful-stop D6.8 can WARN + drop a handoff note.

This test exercises the wrapper's embedded Python summary block in isolation
(no daemon required) by extracting the heredoc between the PYEOF markers and
feeding it mocked daemon RESPONSE bodies via env — the same contract
runner-claim.sh uses (RESPONSE + OP passed via env per guard-165).
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_WRAPPER = Path(__file__).resolve().parents[1] / "runner-claim.sh"


def _summary_block() -> str:
    """Extract the embedded `<<'PYEOF' ... PYEOF` summary block from the wrapper."""
    src = _WRAPPER.read_text()
    m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", src, re.S)
    assert m, "could not locate the PYEOF summary block in runner-claim.sh"
    return m.group(1)


def _run(op: str, response: str):
    block = _summary_block()
    p = subprocess.run(
        [sys.executable, "-"],
        input=block,
        capture_output=True,
        text=True,
        env={**os.environ, "RESPONSE": response, "OP": op},
    )
    return p.returncode, (p.stdout + p.stderr)


# (op, daemon-response-json, expected-exit-code, expected-substring)
CASES = [
    #  CORE: released=False on a real backend surfaces + exits 5.
    ("release", '{"ok":true,"released":false,"backend":"own-cloud"}', 5, "UNCONFIRMED"),
    # released=True is the clean self-release — unchanged.
    ("release", '{"ok":true,"released":true,"backend":"own-cloud"}', 0, "release: ok"),
    # Non-own-cloud backend no-op is handled earlier (exit 0) — never surfaces.
    ("release", '{"ok":true,"noop":true,"backend":"local","reason":"x"}', 0, "no-op"),
    # A hard daemon error stays exit 2 (wrapper maps to exit 1) — NOT conflated
    # with the new "unconfirmed" code.
    ("release", '{"ok":false,"error":"boom","backend":"own-cloud"}', 2, "FAILED"),
    # acquire held (peer owns a live claim) stays exit 4 — unchanged.
    ("acquire", '{"ok":true,"acquired":false,"held":true,"backend":"own-cloud"}', 4, "HELD"),
    # -c: held WITH holder fields still exits 4, and NAMES the box.
    ("acquire",
     '{"ok":true,"acquired":false,"held":true,"backend":"own-cloud",'
     '"holder_machine_id":"cc-03","holder_heartbeat_age_seconds":520}', 4, "cc-03"),
    # acquire success unchanged.
    ("acquire", '{"ok":true,"acquired":true,"held":false,"backend":"own-cloud"}', 0, "acquire: ok"),
    # heartbeat unchanged.
    ("heartbeat", '{"ok":true,"beat":true,"backend":"own-cloud"}', 0, "heartbeat: ok"),
]


@pytest.mark.parametrize("op,response,exp_rc,exp_txt", CASES)
def test_runner_claim_summary_surface(op, response, exp_rc, exp_txt):
    rc, out = _run(op, response)
    assert rc == exp_rc, f"op={op} response={response} -> rc={rc} (want {exp_rc}); out={out!r}"
    assert exp_txt in out, f"op={op} -> missing {exp_txt!r} in {out!r}"


def test_acquire_held_names_holder_and_falls_back_when_absent():
    """-c: the HELD refusal names the holder when the daemon supplied
    it, and falls back to the original anonymous wording when it did not.

    The fallback half is the load-bearing one. g-306-118-a measured THREE
    acquire response shapes, not two — the daemon OMITS the holder keys when the
    runner_state row is unreadable — so the wrapper must not borrow the
    stale-break branch's `or "unknown-machine"` placeholder here. Printing
    "'unknown-machine' owns a live claim" would assert that a holder had been
    identified when none was, which is worse than the anonymous sentence it
    replaced: it looks like data.
    """
    held = ('{"ok":true,"acquired":false,"held":true,"backend":"own-cloud",'
            '"holder_machine_id":"cc-03","holder_heartbeat_age_seconds":520}')
    rc, out = _run("acquire", held)
    assert rc == 4, f"holder-present held must still exit 4, got {rc}: {out!r}"
    assert "cc-03" in out, f"holder machine_id not surfaced: {out!r}"
    assert "520s" in out, f"heartbeat age not surfaced: {out!r}"
    # The anonymous wording must be GONE when a holder is known.
    assert "another machine" not in out, f"anonymous wording survived: {out!r}"

    bare = '{"ok":true,"acquired":false,"held":true,"backend":"own-cloud"}'
    rc, out = _run("acquire", bare)
    assert rc == 4, f"holder-absent held must still exit 4, got {rc}: {out!r}"
    assert "another machine" in out, f"fallback wording lost: {out!r}"
    assert "unknown-machine" not in out, (
        f"placeholder printed as though a holder were identified: {out!r}")


def test_released_missing_key_does_not_surface():
    """A response without a `released` key must NOT trip exit 5 (backward-compat):
    `r.get('released') is False` is only true on an EXPLICIT False."""
    rc, out = _run("release", '{"ok":true,"backend":"own-cloud"}')
    assert rc == 0, f"missing released key should be a clean exit 0, got rc={rc}: {out!r}"
    assert "UNCONFIRMED" not in out
