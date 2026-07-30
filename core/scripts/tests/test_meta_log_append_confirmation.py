""": the meta-log append endpoint must return a VERIFIABLE confirmation.

THE DEFECT (mind_api/src/meta/meta_yaml.py log_record, pre-fix):

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(raw + "\\n")
    return Response.text(json.dumps({"status": "logged"}) + "\\n", ...)

and meta-log-append.sh discarded even that with `> /dev/null`, exiting 0
silently. `core/config/conventions/meta-strategies.md` documented the wrapper's
output as "Confirmation", but measured stdout+stderr length on success was
ZERO. A caller could not distinguish "written" from "swallowed" — which is how a
~2400-char correction came to return exit 0 and never appear in the store, with
no signal at the call site and no way to reproduce it afterwards.

WHY path/offset/bytes AND NOT just a status string: the two failure modes that
actually occur here are both invisible to a bare status.

  1. A daemon bound to a DIFFERENT meta root writes somewhere the caller does
     not expect and reports success. This is a live hazard on this repo — an
     unmarked test that spawns a daemon can repoint the shared runtime out from
     under the fleet (see CLAUDE.md "Live-Daemon Exception", g-115-3329).
     `path` makes that a one-glance mismatch.
  2. An append that does not extend the store. `offset + bytes == new size` is
     arithmetic the caller can check against its own stat() without a re-read,
     and without depending on file ORDER — which is not reliable here (of 554
     records measured 2026-07-27, 287 carry a date-only `date`, 239 a full ISO
     timestamp, and 28 no date at all; the own-cloud union merge handler may
     also reorder). That is why the convention now says grep, not tail.

NOT the mechanism (measured, so the fix does not cargo-cult a lock): concurrent
appender interleaving. 40 concurrent processes writing records of 2,400 /
60,000 / 500,000 / 2,000,000 bytes through the raw `open(path,"a")` shape lost
and corrupted NOTHING — Linux O_APPEND on a regular file is atomic at every
size tested, matching _fileops.locked_append_jsonl exactly. The store is also
already registered in coordination_merge._HANDLERS, so the guard-1055
unregistered-store write-freeze class does not apply either.

Run: py -3 -m pytest core/scripts/tests/test_meta_log_append_confirmation.py -v
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
WRAPPER = SCRIPT_DIR / "meta-log-append.sh"
ENDPOINT = PROJECT_ROOT / "mind_api" / "src" / "meta" / "meta_yaml.py"


def _endpoint_src():
    return ENDPOINT.read_text(encoding="utf-8")


def _log_record_body():
    """The source text of log_record() only, so assertions cannot match a
    sibling function that happens to use the same call."""
    src = _endpoint_src()
    start = src.index("def log_record(")
    end = src.index("\ndef ", start + 1)
    return src[start:end]


def test_endpoint_returns_path_offset_and_bytes():
    """The confirmation must carry the three fields callers verify against."""
    body = _log_record_body()
    for field in ('"path"', '"offset"', '"bytes"', '"status": "logged"'):
        assert field in body, (
            f"log_record no longer returns {field} — the g-115-3534 "
            "confirmation contract regressed to a bare status"
        )


def test_endpoint_materializes_local_file_before_appending():
    """ensure_local() must run before the append.

    meta-log.jsonl is NOT machine-local (_machine_local -> False, s3_key
    ayoai-mind/meta/meta-log.jsonl), so on a box whose mirror is absent or
    stale the append would otherwise extend the wrong base and report an
    offset measured against it. The sibling _append_log path gets this for
    free via _next_meta_change_id; this endpoint had no equivalent.
    """
    body = _log_record_body()
    assert "ensure_local" in body, (
        "log_record lost its ensure_local() call — an S3-only meta-log would "
        "be appended to a stale/absent local mirror"
    )
    assert body.index("ensure_local") < body.index('open(log_path, "a"'), (
        "ensure_local() must run BEFORE the append, not after"
    )


def test_wrapper_does_not_discard_the_response():
    """The wrapper must print the confirmation, not redirect it away.

    guard-989: never redirect a governed-store write wrapper's output to
    /dev/null. This wrapper did exactly that on both its call sites.
    """
    src = WRAPPER.read_text(encoding="utf-8")
    call_lines = [
        ln for ln in src.splitlines()
        if "/v1/meta/yaml/log" in ln or ("rt_call" in ln and "meta/yaml/log" in ln)
    ]
    assert call_lines, "no rt_call to /v1/meta/yaml/log found in the wrapper"
    for ln in call_lines:
        assert "/dev/null" not in ln, (
            f"wrapper discards the daemon response again: {ln.strip()!r}"
        )
    assert 'printf' in src and 'RESP' in src, (
        "wrapper no longer prints the captured response"
    )


def test_convention_documents_the_actual_confirmation_shape():
    """meta-strategies.md must describe what the wrapper really emits.

    The row said "Confirmation" while the measured output was empty. A doc
    that overstates a contract is what sent four goals across three days into
    probing this by hand.
    """
    doc = (PROJECT_ROOT / "core" / "config" / "conventions"
           / "meta-strategies.md").read_text(encoding="utf-8")
    row = [ln for ln in doc.splitlines() if "`meta-log-append.sh`" in ln and "|" in ln]
    assert row, "meta-log-append.sh row missing from the script table"
    assert "offset" in row[0], (
        "the script table no longer documents the offset the wrapper returns"
    )
    assert "grep" in doc and "tail" in doc, (
        "the verification guidance (grep, not tail) was removed"
    )


def test_convention_corrects_the_meta_change_id_expectation():
    """meta-log-append.sh appends VERBATIM — it assigns no mc-NNN.

    Only _append_log (the meta-set.sh path) calls _next_meta_change_id;
    log_record json.loads()-validates and writes the body unchanged. The
    convention previously implied every logged change receives one.
    """
    body = _log_record_body()
    # the paren makes this a CALL, not the prose mention in the ensure_local comment
    assert "_next_meta_change_id(" not in body, (
        "log_record now assigns a meta_change_id — update the convention, "
        "which documents that it does not"
    )
    doc = (PROJECT_ROOT / "core" / "config" / "conventions"
           / "meta-strategies.md").read_text(encoding="utf-8")
    assert "NOT by `meta-log-append.sh`" in doc, (
        "the meta_change_id caveat was removed from meta-strategies.md"
    )


def test_byte_count_is_immune_to_newline_translation():
    """`bytes` must count the bytes that actually land, on every platform.

    In text mode with newline=None, Python translates each "\\n" to os.linesep
    on write. On a box where os.linesep is "\\r\\n" the file grows one byte MORE
    per record than len(payload.encode()) counts, so both `bytes` and `offset`
    are wrong by one per record. Their SUM still equals the file size — which is
    precisely why the error hides, and why this test pins the two fields
    independently rather than only their sum. log_record therefore opens with an
    explicit newline="\\n".

    Measured: with newline forced to "\\r\\n", an 8-byte payload reported
    offset=1 into an EMPTY file (should be 0) while offset+bytes still equalled
    the 9-byte size.
    """
    body = _log_record_body()
    assert 'newline="\\n"' in body, (
        "log_record's append no longer pins newline='\\n' — on a CRLF platform "
        "`bytes` undercounts and `offset` drifts one byte per record"
    )


def test_ensure_local_failure_does_not_lose_the_record():
    """An S3 error must degrade to a local append, never 500 away the record.

    OwnCloudBackend._refresh bare-`raise`s any non-404 ClientError, so an
    unguarded ensure_local() turns a throttle, outage, or expired credential
    into a lost audit record — strictly worse than the pre-fix behavior, which
    always landed locally and let the sync sweep carry it up. The degraded case
    must still be VISIBLE, or the endpoint silently reports an offset measured
    against a stale mirror.
    """
    body = _log_record_body()
    idx_try = body.find("try:")
    idx_ensure = body.find("ensure_local")
    idx_open = body.find('open(log_path, "a"')
    assert idx_try != -1 and idx_try < idx_ensure < idx_open, (
        "ensure_local() is no longer inside a try: that precedes the append — "
        "an S3 error will now lose the record instead of degrading to a local "
        "append"
    )
    assert "stale_base" in body, (
        "the degraded path no longer reports stale_base — a caller cannot tell "
        "the offset was measured against a possibly-stale mirror"
    )


@pytest.mark.skipif(
    not (PROJECT_ROOT / "mind_api" / "state" / "daemon.port").exists(),
    reason="no live daemon on this box",
)
@pytest.mark.skipif(
    os.environ.get("META_LOG_LIVE_E2E") != "1",
    reason="writes a real record to the live meta-log; opt in with "
           "META_LOG_LIVE_E2E=1 (the structural tests above pin the contract "
           "on every run)",
)
def test_live_append_offset_matches_measured_growth(tmp_path):
    """END-TO-END: offset+bytes must equal the store's new size.

    This is the property the whole fix exists to provide — a caller can prove
    its record landed from the confirmation alone.

    OPT-IN, because it appends a real record to the live audit store and the
    daemon (not this process) resolves the meta root, so the write cannot be
    redirected to a tmp dir from here. Running it on every full-suite pass would
    manufacture exactly the probe-artifact pollution that made the original
    defect so expensive to diagnose — 14 such records across four goals and
    three days. The structural tests above run unconditionally and pin every
    field of the contract; this one proves the wiring end to end when asked.
    """
    import os
    sys.path.insert(0, str(SCRIPT_DIR))
    import _paths  # noqa: E402
    # guard-580: a bare "bash" argv[0] resolves via CreateProcess to the
    # System32 WSL launcher on win32 and can hang forever.
    from _bash_helpers import BASH  # noqa: E402

    log = Path(_paths.META_DIR) / "meta-log.jsonl"
    before = log.stat().st_size if log.exists() else 0

    record = json.dumps({
        "date": "2026-07-27T00:00:00",
        "strategy_file": "core/scripts/tests/test_meta_log_append_confirmation.py",
        "field": "confirmation_contract",
        "reason": "g-115-3534 regression test: verify offset+bytes == new size",
    })
    proc = subprocess.run(
        [BASH, str(WRAPPER)], input=record, capture_output=True,
        text=True, cwd=str(PROJECT_ROOT), env={**os.environ},
    )
    assert proc.returncode == 0, f"wrapper failed: {proc.stderr}"
    assert proc.stdout.strip(), (
        "wrapper printed NOTHING on success — the original g-115-3534 defect"
    )
    conf = json.loads(proc.stdout.strip().splitlines()[-1])

    assert conf["status"] == "logged"
    assert Path(conf["path"]) == log, (
        f"daemon wrote to {conf['path']}, caller expected {log} — the daemon "
        "is bound to a different meta root"
    )
    assert conf["offset"] + conf["bytes"] == log.stat().st_size, (
        "offset+bytes does not equal the store's new size — the confirmation "
        "cannot be used to verify the append"
    )
    # offset >= the caller's pre-write view, never less: the store is
    # append-only and ensure_local() may pull records this box had not seen.
    # A STRICTLY GREATER offset is not a failure, it is the staleness readout —
    # on the first run of this test the daemon appended at 1,307,327 while the
    # local mirror measured 1,306,073, i.e. the mirror was 1,254 bytes behind
    # S3. Pre-fix, with no ensure_local(), that append would have landed at the
    # stale 1,306,073 and written across a span S3 already had — which is the
    # divergence the "silently dropped write" report was chasing. The
    # confirmation surfaced it on its first live run.
    assert conf["offset"] >= before, (
        f"offset {conf['offset']} is BEHIND the caller's pre-write size "
        f"{before} — the append-only store went backwards"
    )
