"""test_owncloud_silent_counter_surfaced.py — .

Sibling of `test_owncloud_error_identity_surfaced.py` (g-115-7255), one counter
class over, and the reason it needed its own file is that the earlier fix cured
the SYMPTOM it was filed on (`errors: N` naming nothing) while leaving the
WHITELIST that caused it in place.

`sync_file` hands `_sync_one` a stats dict with twelve keys. The admin payload
hand-enumerated SIX. Seven of the remaining ones are real outcomes
(`stale_skipped`, `stale_pulled`, `stale_would_pull`, `nobaseline_skipped`,
`nobaseline_reconciled`, `multipart_deferred`, `multipart_merged`), and TWO of
those — `stale_pulled` and `nobaseline_reconciled` — are branches that
**overwrite the local file from S3** via `be.refresh()`
(owncloud_sync.py:1167 and :1342). Both had their per-file stderr print
deliberately removed as a flood fix (g-328-14). So a call that silently reverted
a just-written local file returned

    {ok: true, pushed: 0, would_push: 0, in_sync: 0, conflicts: 0,
     diverged_skipped: 0, errors: 0}

and reported nothing about the overwrite it had just performed.

Measured consequence (bravo, cc-05, 2026-09-11 05:35-05:40): a one-line
size-neutral front-matter repair to a world tree node was written twice — once
by `sed -i`, once by the Edit tool so the PostToolUse push hook fired — and both
times the file read its OLD value again within ~2 minutes with no error at any
point. That all-zero payload was the sharpest diagnostic available and carried
none of the information that existed at the time.

A `_skip()` sets `stats_out["reason"]`, which the payload DOES surface, so
all-zeros-with-no-reason narrowed the cause to exactly these seven.

These tests pin the RENDER generically rather than as a longer list, so the
NEXT counter added to `_sync_one` cannot go silent the same way — the defect
class rb-4868 names (a hand-maintained whitelist silently drops new fields).
"""

from __future__ import annotations

import sys
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import owncloud_sync  # noqa: E402


def _admin():
    sys.path.insert(0, str(CORE_SCRIPTS.parent.parent))
    from mind_api.src.endpoints import admin
    return admin


class _Ctx:
    class _P:
        project_root = Path(__file__).resolve().parents[3]

    def __init__(self, query=None):
        self.query = query or {}
        self.paths = self._P()


def _body(resp):
    import json
    raw = getattr(resp, "body", None)
    if raw is None:
        raw = resp.data if hasattr(resp, "data") else resp
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    return json.loads(raw) if isinstance(raw, str) else raw


def _call(admin, path="/w/world/x.md"):
    return _body(admin.owncloud_sync_file(_Ctx({"path": path})))


def _stub(monkeypatch, stats_payload, rc=0):
    def _fake_sync_file(be, target, *, dry_run, stats_out=None):
        if stats_out is not None:
            stats_out.update(stats_payload)
        return rc
    monkeypatch.setattr(owncloud_sync, "sync_file", _fake_sync_file)
    import storage_backend
    monkeypatch.setattr(storage_backend, "get_backend", lambda: object())


# ------------------------------------------------------- the reverting pair

def test_nobaseline_reconciled_reaches_the_caller(monkeypatch):
    """The branch that GETs S3 over the local file must not report all-zeros.

    This is the exact payload shape from the 2026-09-11 incident. Before the
    fix the assertion below failed and `body` was six zeros.
    """
    admin = _admin()
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    _stub(monkeypatch, {"scanned": 1, "pushed": 0, "would_push": 0,
                        "in_sync": 0, "conflicts": 0, "errors": 0,
                        "diverged_skipped": 0, "nobaseline_reconciled": 1})
    body = _call(admin)
    assert body["ok"] is True
    assert all(body[k] == 0 for k in ("pushed", "would_push", "in_sync",
                                      "conflicts", "diverged_skipped",
                                      "errors")), body
    assert body.get("nobaseline_reconciled") == 1, (
        "the endpoint reported six zeros for a call that OVERWROTE the local "
        "file from S3 — this is the g-115-9689 defect")


def test_stale_pulled_reaches_the_caller(monkeypatch):
    """The other be.refresh() branch, same requirement."""
    admin = _admin()
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    _stub(monkeypatch, {"scanned": 1, "pushed": 0, "in_sync": 0,
                        "conflicts": 0, "errors": 0, "diverged_skipped": 0,
                        "stale_pulled": 1})
    body = _call(admin)
    assert body.get("stale_pulled") == 1, body


# ------------------------------------------------------- backward compatible

def test_clean_push_payload_is_unchanged(monkeypatch):
    """A clean push must carry no extra keys at all.

    The six whitelisted keys stay present-and-zero (existing consumers index
    them unconditionally); everything else is absent, not zero, so a reader
    never has to distinguish 'did not happen' from 'happened and was dropped'.
    """
    admin = _admin()
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    _stub(monkeypatch, {"scanned": 1, "pushed": 1, "would_push": 0,
                        "in_sync": 0, "conflicts": 0, "errors": 0,
                        "diverged_skipped": 0, "stale_pulled": 0,
                        "nobaseline_reconciled": 0, "push_paths": ["/w/x"]})
    body = _call(admin)
    assert body["pushed"] == 1
    for k in ("stale_pulled", "nobaseline_reconciled", "scanned", "push_paths"):
        assert k not in body, f"{k!r} leaked into a clean-push payload: {body!r}"


def test_skip_reason_still_surfaces(monkeypatch):
    """A _skip() path is how all-zeros is ALLOWED to be uninformative — it
    carries a reason. That distinction is what narrowed the incident, so it is
    pinned rather than assumed."""
    admin = _admin()
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    _stub(monkeypatch, {"reason": "machine_local"})
    body = _call(admin)
    assert body.get("reason") == "machine_local", body


# ------------------------------------------- producer -> render seam (the one
#                                              that survives a rename)

def _producer_counter_names() -> set[str]:
    """Every counter name the PRODUCER writes, read off the producer itself.

    Two sources, because the producer has two and only one of them is visible
    at runtime:

    * `sync_file`'s stats initialiser, captured by RUNNING it (below) — ten
      keys, and NOT the interesting ten: `stale_pulled` and
      `nobaseline_reconciled` are absent from it.
    * `_sync_one`'s lazy `stats["X"] = stats.get("X", 0) + 1` sites, which
      CREATE counters the initialiser never mentions. Those are exactly the
      seven the endpoint's whitelist omitted, so a test that read only the
      initialiser would miss the entire defect it is here to pin.

    Static extraction is deliberate and is the only way to reach the second
    set without executing every branch of a function that talks to S3.
    """
    src = (CORE_SCRIPTS / "owncloud_sync.py").read_text(encoding="utf-8")
    # THREE increment forms, and all three are needed. The `\\?` is not
    # decoration: the producer LINE-WRAPS its longest counter names, so
    # `stats["nobaseline_reconciled"] = \\` + newline + `stats.get(...)` does
    # not match a pattern that only allows whitespace after the `=` — and
    # nobaseline_reconciled is one of the two counters this whole file exists
    # for. The positive control below is what caught that.
    names = set(re.findall(
        r'stats\["([a-z_]+)"\]\s*=\s*\\?\s*stats\.get', src))
    names |= set(re.findall(r'stats\["([a-z_]+)"\]\s*\+=', src))
    # `_try_merge_put(..., counter="X")` sets stats[X] through a parameter, so
    # the name never appears beside `stats[` at all. Four live call sites.
    names |= set(re.findall(r'counter="([a-z_]+)"', src))
    return names


def test_every_producer_counter_is_renderable(monkeypatch, tmp_path):
    """No hand-written counter names: read them off the REAL producer.

    Every other test here names its counter in a literal, so all of them stay
    green if `_sync_one` renames one or adds an eighth — the render would
    faithfully carry nothing and the suite would agree. That is guard-3871 (a
    dict literal between producer and consumer IS a schema) and guard-920 /
    rb-5235 (replicate the PRODUCTION shape, not the contract-ideal one).
    """
    admin = _admin()
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")

    # --- half 1: what sync_file HANDS OVER, captured by running it ----------
    captured = {}

    def _capture_sync_one(be, full, *, dry_run, stats, **kw):
        captured.update(stats)
        return None

    monkeypatch.setattr(owncloud_sync, "_sync_one", _capture_sync_one)
    monkeypatch.setattr(owncloud_sync, "_is_machine_local", lambda *a, **k: False)
    monkeypatch.setattr(owncloud_sync, "_owned_agents", lambda **k: None)
    monkeypatch.setattr(owncloud_sync, "_log_sweep_stats", lambda *a, **k: None)

    root = tmp_path / "world"
    target = root / "node.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x\n", encoding="utf-8")

    class _Be:
        _roots = [(str(root), "world")]

    rc = owncloud_sync.sync_file(_Be(), target, dry_run=False, stats_out={})
    assert rc == 0, f"sync_file skipped instead of reaching _sync_one: {captured!r}"
    initialised = {k for k, v in captured.items() if isinstance(v, int)}
    assert len(initialised) >= 8, (
        f"sync_file handed over too few counters: {sorted(initialised)!r} — "
        "the capture did not reach the real initialiser")

    # --- half 2: what _sync_one CREATES lazily, read off the source ---------
    lazy = _producer_counter_names()
    # POSITIVE CONTROL. A regex over source that silently matches nothing
    # would make the loop below vacuous and this whole test would pass while
    # pinning nothing — the exact shape guard-2298 is about. These two names
    # are the defect's own counters; if the extraction stops finding them the
    # test fails here rather than reporting a clean sweep.
    for must in ("stale_pulled", "nobaseline_reconciled"):
        assert must in lazy, (
            f"counter-name extraction found {sorted(lazy)!r} and missed "
            f"{must!r} — the regex no longer matches the producer's "
            "increment form, so this test would pass vacuously")

    # --- the assertion -------------------------------------------------------
    import storage_backend
    monkeypatch.setattr(storage_backend, "get_backend", lambda: object())
    for name in sorted(initialised | lazy):
        if name in ("scanned", "push_paths", "error_paths"):
            continue  # scanned is always 1 here and deliberately not reported
        _stub(monkeypatch, {name: 1})
        body = _call(admin)
        assert body.get(name) == 1, (
            f"counter {name!r} is written by the producer but never reaches "
            f"the caller — a counter went silent the way "
            f"nobaseline_reconciled did. Payload: {body!r}")
