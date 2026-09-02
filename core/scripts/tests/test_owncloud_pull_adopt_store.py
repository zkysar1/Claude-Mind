"""test_owncloud_pull_adopt_store.py — regression for .

THE DEFECT. Under own-cloud the local tree is a read-through cache and S3 is
authoritative (guard-980). Every pull overwrite is gated on a PERSISTENT
per-file baseline md5 in `mind_api/state/owncloud-sync-manifest.json`. When a
file reaches THREE-WAY DIVERGENCE — local md5 != baseline md5 != S3 md5 — the
no-clobber gate reads it as "local has unpushed writes", skips it as
`local_ahead_skipped`, and does so FOREVER: nothing below the pull ever
re-derives a baseline, so a poisoned baseline is a PERMANENT wedge rather than a
transient skip. Class (b) governed mirror — no reconciler below the write
(governed-store-write-classes.md) — and hand-writing the file is refused by
guard-996, so before this lane there was no sanctioned remedy at all.

Measured 2026-09-02 (alpha, cc-09, own-cloud): the poisoned baseline md5 was
`08ae6a83...`, byte-identical to the value cc-13 independently reported, so the
defect is not box-specific. 40 continuity files across five agents were
pull-skipped on one box.

WHAT `--adopt-store` DOES, and the one thing these tests exist to pin: it drops
the manifest baseline for explicitly-named continuity files so the file takes
each gate's EXISTING no-baseline branch (S3-authoritative first pull, which
snapshots local to .history first). It adds no new overwrite path.

TWO FENCES, NOT ONE — the bug inside the fix, and the reason for
`test_adopt_persists_the_baseline_drop_not_only_the_argument`. The baseline is
read by two gates that deliberately mirror each other:

  1. `owncloud_sync._pull_one`                      -- takes baseline_md5 as an ARGUMENT
  2. `owncloud_backend._overwrite_decision`         -- RE-READS _load_manifest()
     (reached via be.refresh -> _refresh)

The first implementation dropped only the argument. Fence 2 re-read the poisoned
baseline from disk, returned "no_clobber", and kept local — while `_pull_one`,
already committed to pulling, counted `pulled += 1` and re-stamped the baseline
to LOCAL's md5. Live result: `pulled=1 adopted=1/1 errors=0 rc=0` over a file
that kept its 10-day-old bytes and mtime, with the DRIFT check still failing.
A caller reading rc=0 would have stopped while the wedge stood. Persisting the
drop before the pull makes both fences see an ABSENT baseline, which is exactly
the S3-authoritative branch each already implements
(`test_refresh_no_baseline_pulls_s3_authoritative` pins fence 2's half).

RENDERER HALF. The wrapper cannot merely be ABLE to send the flag: the ENDPOINT
is the contract (guard-2374). A daemon running older code accepts the query
param, ignores it, and returns a normal no-clobber pull — no `adopted` key, no
adopt line, rc=0, wedge intact. That was observed live here against a
not-yet-recycled daemon, which is why the renderer keys off what the WRAPPER
SENT (`ADOPT_REQ`) rather than off what the response happens to carry
(guard-2018: an absent field can BE the zero).

The renderer tests drive the SHIPPED printer, extracted off disk at test time
and fed on STDIN exactly as `$PYLAUNCH -` feeds it in production (guard-920:
replicate the production invocation shape, not the contract-ideal one).

WHAT THIS FILE DOES NOT COVER, stated rather than left implicit (guard-1462):
the HTTP round trip and the endpoint's `**stats` spread are structurally
unfalsifiable from here. Those were verified live on cc-09 — a single
`--adopt-store` pass took foxtrot's working memory from
`[DRIFT — local mirror differs from store]` to `[match]`, with a 66,930-byte
`.history` snapshot written first, and the same for zeta.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PULL_SCRIPT = SCRIPT_DIR.parent / "owncloud-pull.sh"
SYNC_PY = SCRIPT_DIR.parent / "owncloud_sync.py"

FLEET_PRINTER, SINGLE_PRINTER = 0, 1


def _printer(which: int) -> str:
    src = PULL_SCRIPT.read_text(encoding="utf-8")
    blocks = re.findall(r"<<'PYEOF'\n(.*?)\nPYEOF", src, re.S)
    assert len(blocks) == 2, (
        f"expected exactly 2 PYEOF heredocs in owncloud-pull.sh, found {len(blocks)}"
    )
    return blocks[which]


def _render(response: str, adopt_req: str = ""):
    """Run the shipped single-agent printer the way bash runs it."""
    return subprocess.run(
        [sys.executable, "-"], input=_printer(SINGLE_PRINTER),
        env={"RESPONSE": response, "ADOPT_REQ": adopt_req, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True,
    )


# Live response shapes, transcribed from cc-09 2026-09-02 rather than invented.
ADOPTED_OK = (
    '{"backend":"own-cloud","ok":true,"agent":"foxtrot","scanned":1,"pulled":1,'
    '"in_sync":0,"would_pull":0,"s3_absent":0,"local_ahead_skipped":0,'
    '"multipart_deferred":0,"errors":0,"pulled_files":["working-memory.yaml"],'
    '"only":["working-memory.yaml"],"adopt_store":["working-memory.yaml"],'
    '"adopted":["working-memory.yaml"]}'
)
ADOPTED_NOTHING = (
    '{"backend":"own-cloud","ok":true,"agent":"alpha","scanned":1,"pulled":0,'
    '"in_sync":1,"would_pull":0,"s3_absent":0,"local_ahead_skipped":0,'
    '"multipart_deferred":0,"errors":0,"pulled_files":[],"only":["handoff.yaml"],'
    '"adopt_store":["no-such-file.yaml"],'
    '"adopt_requested_missing":["no-such-file.yaml"],"adopted":[]}'
)
# The stale-daemon shape: the query param was sent and SILENTLY DROPPED, so the
# response is an ordinary no-clobber pull with no adopt keys at all.
ENDPOINT_IGNORED_IT = (
    '{"backend":"own-cloud","ok":true,"agent":"alpha","scanned":1,"pulled":0,'
    '"in_sync":0,"would_pull":0,"s3_absent":0,"local_ahead_skipped":1,'
    '"multipart_deferred":0,"errors":0,"pulled_files":[],'
    '"only":["working-memory.yaml"]}'
)


# --- the ENDPOINT is the contract, not the wrapper's ability to send ---------

def test_a_dropped_adopt_param_is_not_reported_as_success():
    """guard-2374 + guard-2018, observed live against a stale daemon.

    This is the assertion that would have caught the real incident: the same
    invocation exited 0 with no adopt line at all while the wedge stood.
    """
    r = _render(ENDPOINT_IGNORED_IT, adopt_req="working-memory.yaml")
    assert r.returncode == 6, (
        "a sent --adopt-store with no 'adopted' echo means nothing honoured the "
        f"flag; that must not exit 0. got rc={r.returncode}: {r.stdout!r}"
    )
    assert "does not implement it" in r.stdout, (
        f"the message must name the CAUSE (stale daemon), not just fail: {r.stdout!r}"
    )


def test_the_stale_daemon_message_names_the_remedy():
    """A refusal that does not say what to do next just relocates the stall.

    Asserted against the WRAPPER SOURCE, not the renderer's stdout, because the
    two halves print at different seams: the python printer names the CAUSE and
    exits 6, and bash's `6)` case arm names the REMEDY on stderr. Driving the
    printer alone can never see the second half — a distinction worth pinning
    rather than papering over, since the first draft of this test asserted the
    remedy at the renderer seam and failed for exactly that reason.
    """
    src = PULL_SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"\n    6\) echo \"\$SUMMARY\"\n(.*?);;\n", src, re.S)
    assert m, "the rc=6 (stale daemon) case arm is missing from owncloud-pull.sh"
    assert "mind-api-start.sh --restart" in m.group(1), (
        f"the remedy (recycle the daemon) must be printed: {m.group(1)!r}"
    )


def test_no_adopt_request_means_no_adopt_output_at_all():
    """The guard keys off what WE SENT, so an ordinary pull must be untouched.

    Without this, every plain `owncloud-pull.sh` run on a daemon that predates
    the lane would start failing — the fix would be worse than the defect.
    """
    r = _render(ENDPOINT_IGNORED_IT, adopt_req="")
    assert r.returncode == 0, (
        f"a pull that never asked to adopt must be unaffected. got rc={r.returncode}"
    )
    assert "adopt-store" not in r.stdout, (
        f"no adopt line may appear when none was requested: {r.stdout!r}"
    )


# --- coverage reporting, mirroring the --only discipline --------------------

def test_a_successful_adopt_reports_which_names_it_dropped():
    r = _render(ADOPTED_OK, adopt_req="working-memory.yaml")
    assert r.returncode == 0, f"a successful adopt exits 0. got {r.returncode}"
    assert "1/1" in r.stdout, f"coverage must be matched/requested: {r.stdout!r}"
    assert "working-memory.yaml" in r.stdout


def test_an_adopt_that_matched_nothing_refuses_to_exit_zero():
    """guard-3489: refuse to exit 0 when the coverage count is zero.

    Distinct from rc=6 above: here the endpoint DID implement the lane and
    honestly reported adopting nothing, because the name was outside the
    scanned continuity set.
    """
    r = _render(ADOPTED_NOTHING, adopt_req="no-such-file.yaml")
    assert r.returncode == 5, (
        f"a vacuous adopt must not report success. got rc={r.returncode}: {r.stdout!r}"
    )
    assert "no-such-file.yaml" in r.stdout


def test_the_two_zero_cases_do_not_collapse_to_one_answer():
    """A discriminator that takes the same value on both branches is decoration.

    "endpoint ignored the flag" (6) and "endpoint honoured it and matched
    nothing" (5) demand DIFFERENT operator actions — recycle the daemon vs fix
    the filename — so they must never share an exit code. (guard-5163.)
    """
    ignored = _render(ENDPOINT_IGNORED_IT, adopt_req="working-memory.yaml")
    vacuous = _render(ADOPTED_NOTHING, adopt_req="no-such-file.yaml")
    assert ignored.returncode != vacuous.returncode, (
        "the stale-daemon and empty-match cases must stay distinguishable; both "
        f"returned {ignored.returncode}"
    )


# --- the engine half: BOTH fences, not only the argument --------------------

def test_adopt_persists_the_baseline_drop_not_only_the_argument():
    """The two-fence regression, pinned as SOURCE STRUCTURE.

    `be.refresh` -> `_overwrite_decision` re-reads the manifest FROM DISK, so
    dropping the baseline only in `_pull_one`'s argument leaves the second fence
    reading the poisoned value — it returns "no_clobber", local is kept, and the
    pull is counted anyway. Pinning the persisted drop is what keeps the lane
    single-pass; without it the live behaviour was `pulled=1` over an unchanged
    file.

    A source-level assertion rather than a behavioural one because exercising
    the real seam needs a live S3 backend; the behavioural proof is the live
    cc-09 run recorded in this module's docstring.
    """
    src = SYNC_PY.read_text(encoding="utf-8")
    m = re.search(r"\n    if adopt_names and not dry_run:\n(.*?)\n    for name in ",
                  src, re.S)
    assert m, (
        "pull_continuity must PERSIST the baseline drop before its pull loop; "
        "the `if adopt_names and not dry_run:` block is gone or was moved after "
        "the loop, which silently restores the two-fence no-op"
    )
    block = m.group(1)
    assert "_save_manifest" in block, (
        "the drop must be SAVED to the manifest, not only removed in memory — "
        "the backend's second fence re-reads the file from disk"
    )
    assert ".pop(" in block, "the drop removes the poisoned key from the manifest"


def test_adopt_never_widens_the_pull_beyond_the_continuity_set():
    """`only` may never widen the pull; adopt inherits that invariant exactly.

    Adopt names are intersected with `continuity_names` AFTER the `only`
    narrowing, so a caller cannot reach a non-continuity path through this flag.
    """
    src = SYNC_PY.read_text(encoding="utf-8")
    assert "adopt_names = {n for n in continuity_names if n in wanted_adopt}" in src, (
        "adopt names must be intersected with the (already narrowed) continuity "
        "set; anything else lets adopt reach a path `only` could not"
    )


def test_a_dry_run_adopt_does_not_persist_the_drop():
    """`dry_run` must write nothing — including the manifest.

    _pull_one already returns before both the .history snapshot and the refresh
    on a dry run; the persisted drop has to respect the same contract or a dry
    run would leave the manifest mutated.
    """
    src = SYNC_PY.read_text(encoding="utf-8")
    assert "if adopt_names and not dry_run:" in src, (
        "the persisted baseline drop must be gated on `not dry_run`"
    )


def test_the_fleet_form_is_refused():
    """Adopting is a per-path judgement; a fleet form would apply one such
    judgement to every agent at once, which is the sweep the design forbids."""
    src = PULL_SCRIPT.read_text(encoding="utf-8")
    assert '[ -n "$ALL_AGENTS" ] && [ -n "$ADOPT_STORE" ]' in src, (
        "--adopt-store must be refused with --all-agents"
    )
