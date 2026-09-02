"""WM lost-update reproductions that need NO >10s stall (, step 2).

The Investigate's surviving candidate was a >10s stall letting a peer
stale-break the WM lock (`file_locks.locked`, stale_seconds=10). Two facts
measured on 2026-09-02 (zeta, cc-02, own-cloud) narrow where that can even
happen:

  * `file_locks.locked` takes the per-process `threading.Lock` BEFORE the
    backend lock, so two writers inside ONE daemon can never both be in the
    critical section — a stale-break needs a SECOND PROCESS on the same lock
    key, and on an own-cloud box that key is a DynamoDB conditional put
    (`OwnCloudBackend.acquire_lock`, ttl = now + 10), not a file.
  * The daemon's stderr (mind_api/state/spawn.log) carried ZERO
    `[lock-stale-break]` lines over 26h / 193 WM POSTs after the
    instrumentation restart, with positive controls present.

This file reproduces the two paths that produce the observed symptom —
"the write returned ok:true / rc=0 and later the value is gone" — with no
stall, no second daemon and no lock defect. Both run against the REAL
endpoints on an in-process daemon over a tmp project root (hermetic: never
the live agent WM).

  1. Body-side read → bump → set spanning TWO requests. The lock covers one
     request; a read-modify-write that spans two is unprotected by
     construction, so two sessions bumping `knowledge_debt[].sessions_deferred`
     produce ONE increment, not two — the literal 2026-08-31 symptom.
  2. An UNLOCKED whole-file writer straddling a daemon set: read the whole
     WM, work, rewrite it. A daemon set landing inside that window is
     verified by its own read-back and then reverted with the file's mtime
     fresh — the g-115-7322 signature.

     RE-POINTED 2026-09-02 (g-115-8667). This drives body-merge's PRIMITIVES
     (`_read_yaml` / `_write_yaml_atomic`) by hand, and they are still
     unlocked — correctly so, because the lock belongs at the call site that
     spans both, not on a primitive that cannot know the span. What is no
     longer true is the claim this docstring used to make about the CALL
     SITES: body-merge (`_consume_staged` + the sessions pass),
     compact-restore-slots (both `read_wm()`→`write_wm()` pairs) and
     wm-contamination-check's quarantine swap now each hold the WM lock
     across their read and their write. So this test pins the primitive
     contract ONLY; the call-site guarantee is pinned end-to-end in
     test_wm_lock_spans_read_write_g115_8667.py, and reading a green result
     here as "the writers are still unsafe" would invert its meaning.

These are REPRODUCTIONS of current behaviour. Both remedies landed in
g-115-8667 and neither retires them, because both remedies are ADDITIVE:
the compare-and-set token on POST /v1/wm/set is opt-in, so the default
contract these two pin is unchanged and must stay pinned — a silent switch
to merge-on-write or to mandatory CAS would break every existing caller and
these are what would catch it. The 409 sibling below covers the opt-in path;
the writer-side lock is covered end-to-end in
test_wm_lock_spans_read_write_g115_8667.py.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = TESTS_DIR.parent
for p in (str(TESTS_DIR), str(CORE_SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from _daemon_fixture import DaemonFixture  # noqa: E402

AGENT = "alpha"


def _get(port, slot):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/wm/read?slot={slot}&json=1")
    req.add_header("X-Mind-Agent", AGENT)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _set(port, slot, value):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/wm/set?slot={slot}",
        data=json.dumps(value).encode("utf-8"), method="POST")
    req.add_header("X-Mind-Agent", AGENT)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


@contextlib.contextmanager
def _daemon_with_seeded_wm():
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        world = tmp / "world"
        world.mkdir()
        with DaemonFixture(world, agent=AGENT) as df:
            wm_path = (df.project_root / "agents" / AGENT / "session"
                       / "working-memory.yaml")
            wm_path.write_text(yaml.safe_dump({
                "session_start": "2026-09-02T00:00:00",
                "slots": {"knowledge_debt": [{
                    "node_key": "some-node", "reason": "carried",
                    "priority": "medium", "sessions_deferred": 3}]},
                "slot_meta": {},
            }), encoding="utf-8")
            yield df, wm_path


def test_two_sessions_read_bump_set_lose_one_increment():
    """REPRODUCTION (path 1): the consolidation carry-forward is
    `wm-read.sh knowledge_debt` -> bump sessions_deferred -> `wm-set.sh
    knowledge_debt`. Two sessions doing that concurrently on one agent-wide
    WM: both reads see 3, both sets return 200 ok:true, and the slot ends at
    4 — "incremented once, not twice". No lock is involved in the outcome:
    each set is serialised and correct in isolation; the RMW spans two
    requests, so the lock cannot see it."""
    with _daemon_with_seeded_wm() as (df, _wm_path):
        a = _get(df.port, "knowledge_debt")          # session A reads
        b = _get(df.port, "knowledge_debt")          # session B reads
        assert a[0]["sessions_deferred"] == 3 == b[0]["sessions_deferred"]

        a[0]["sessions_deferred"] += 1               # A: 3 -> 4
        sa, ba = _set(df.port, "knowledge_debt", a)
        b[0]["sessions_deferred"] += 1               # B: 3 -> 4, from its stale read
        sb, bb = _set(df.port, "knowledge_debt", b)
        assert (sa, ba.get("ok")) == (200, True), ba
        # A conditional-by-default set (the  client-lane remedy) would
        # refuse B's stale write HERE, before the final read — so this is the
        # assertion that flips when the remedy lands, and the message says so.
        assert (sb, bb.get("ok")) == (200, True), (
            f"B's stale set was refused ({sb}: {bb}) — the set became conditional "
            "by default; this reproduction no longer describes the default contract")

        final = _get(df.port, "knowledge_debt")
        assert final[0]["sessions_deferred"] == 4, (
            "two increments produced 5 — the daemon merged instead of replacing; "
            "this reproduction no longer describes the default contract")


def _set_cas(port, slot, value, expected_update_count):
    """POST /v1/wm/set carrying the  compare-and-set token."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/wm/set?slot={slot}"
        f"&expected_update_count={expected_update_count}",
        data=json.dumps(value).encode("utf-8"), method="POST")
    req.add_header("X-Mind-Agent", AGENT)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_cas_token_refuses_the_stale_write_instead_of_losing_the_increment():
    """The client-side remedy for path 1: opt in and the loss becomes a 409.

    Same two-session interleaving as the reproduction above — both read at
    sessions_deferred=3, both bump to 4, both write — but each write carries
    the update_count it read. The second is now REFUSED rather than silently
    overwriting the first, and the sanctioned recovery (re-read, re-apply,
    re-send on the fresh token) lands both increments at 5.

    A refusal is only an improvement if it is also NON-DESTRUCTIVE, so the
    value is asserted unchanged after the 409 — a gate that rejects AND writes
    would be strictly worse than the lost update it replaced.
    """
    with _daemon_with_seeded_wm() as (df, wm_path):
        def _uc():
            d = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
            meta = (d.get("slot_meta") or {}).get("knowledge_debt") or {}
            return meta.get("update_count", 0)

        a = _get(df.port, "knowledge_debt")
        tok_a = _uc()
        b = _get(df.port, "knowledge_debt")
        tok_b = _uc()
        assert tok_a == tok_b, "a read must not consume the CAS token"
        assert a[0]["sessions_deferred"] == 3 == b[0]["sessions_deferred"]

        a[0]["sessions_deferred"] += 1
        status, body = _set_cas(df.port, "knowledge_debt", a, tok_a)
        assert (status, body.get("ok")) == (200, True), body

        # B is now stale. Under the default contract (reproduction 1) this
        # write lands and erases A's increment.
        b[0]["sessions_deferred"] += 1
        status, body = _set_cas(df.port, "knowledge_debt", b, tok_b)
        assert status == 409, (status, body)
        assert body.get("error") == "stale_write", body
        assert body.get("expected_update_count") == tok_b, body
        assert body.get("current_update_count") == _uc(), body
        assert _get(df.port, "knowledge_debt")[0]["sessions_deferred"] == 4, (
            "the refused write LANDED — a CAS refusal must not mutate the slot")

        c = _get(df.port, "knowledge_debt")
        c[0]["sessions_deferred"] += 1
        status, body = _set_cas(df.port, "knowledge_debt", c, _uc())
        assert (status, body.get("ok")) == (200, True), body
        assert _get(df.port, "knowledge_debt")[0]["sessions_deferred"] == 5, (
            "both increments must survive once the loser retries on a fresh token")


def _load_body_merge():
    spec = importlib.util.spec_from_file_location(
        "body_merge_under_test", CORE_SCRIPTS / "body-merge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_unlocked_whole_file_rmw_reverts_a_verified_daemon_set():
    """REPRODUCTION (path 2): body-merge.py's real read/write PRIMITIVES,
    driven by hand — read the reducer WM, do work, rewrite the whole file —
    with a daemon set landing in between. The set is verified by its own
    read-back (exactly what verified-wm-set.sh checks) and is then gone, with
    rc=0 at every step.

    SCOPE, re-pointed 2026-09-02 (g-115-8667): this pins the PRIMITIVES, which
    remain unlocked by design. It no longer describes `_consume_staged`, which
    now takes the WM lock across its read and its write — see
    test_wm_lock_spans_read_write_g115_8667.py, where a real concurrent daemon
    set is asserted to SURVIVE that call. Green here means "the primitives are
    still primitives", not "the writers are still unsafe"."""
    # No import-failure skip: a baseline that can SKIP reads green in a chunked
    # suite summary (TOTAL reports only `passed`), which is exactly the silent
    # pass this file exists to prevent. body-merge.py imports `wm`, which needs
    # MIND_AGENT at import time — conftest pins it for the canonical runner and
    # the ten sibling test_body_merge*/test_capture* files import it the same
    # bare way. Outside conftest the RuntimeError is the correct, loud result.
    bm = _load_body_merge()

    with _daemon_with_seeded_wm() as (df, wm_path):
        snapshot = bm._read_yaml(wm_path)            # the unlocked writer reads

        status, body = _set(df.port, "last_strategic_scan", "2026-09-02T13:31:58")
        assert (status, body.get("ok")) == (200, True), body
        assert _get(df.port, "last_strategic_scan") == "2026-09-02T13:31:58"  # verified

        snapshot["slots"]["worker_merge_marker"] = "merged-from-a-staged-body"
        bm._write_yaml_atomic(wm_path, snapshot)     # ... and rewrites the whole file

        assert _get(df.port, "last_strategic_scan") is None, (
            "the daemon set survived the unlocked whole-file rewrite — the writer "
            "now takes the WM lock (or merges onto a fresh read); update this reproduction")
        assert _get(df.port, "worker_merge_marker") == "merged-from-a-staged-body"
