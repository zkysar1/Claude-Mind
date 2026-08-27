"""test_owncloud_error_identity_surfaced.py — .

`_record_error` has stored {path, phase, exc, msg} in `stats["error_paths"]`
since 2026-08-11, bounded at 40 with an overflow counter, and its producer side
is pinned by test_owncloud_sweep_stats_log.py. NOTHING rendered it: the two
hand-enumerated admin payloads whitelisted counters and dropped the list, and
the CLI summaries printed the identities of the SUCCESSES (push_paths /
pulled_files) while printing failures as a bare count.

Measured consequence (zeta, cc-02, 2026-08-22): a world tree-node body would
not push; three push paths each returned `{errors: 1, conflicts: 0,
diverged_skipped: 0, pushed: 0}` with no reason exposed, and the reason existed
the whole time. One completed ritual pass was silently lost.

These tests pin the RENDER, which is the half that was missing. The sibling
`owncloud_pull` endpoint splats `**stats` and has always carried the field —
that asymmetry is why a whitelist is the thing worth testing.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import owncloud_sync  # noqa: E402


_ERR = [
    {"path": "/w/world/knowledge/tree/system/program-alignment-health.md",
     "phase": "union-merge-push", "exc": "ValueError", "msg": "same-heading divergence"},
    {"path": "/w/world/board/findings.jsonl",
     "phase": "put", "exc": "ClientError", "msg": "PreconditionFailed"},
]


# --------------------------------------------------------------- CLI render

def test_print_error_paths_names_path_phase_and_reason(capsys):
    owncloud_sync._print_error_paths({"errors": 2, "error_paths": _ERR})
    err = capsys.readouterr().err
    # All four recorded fields must reach the operator. A render that prints
    # only the path reintroduces "errors: N naming no cause" one level down.
    for token in ("program-alignment-health.md", "union-merge-push",
                  "ValueError", "same-heading divergence",
                  "findings.jsonl", "put", "ClientError", "PreconditionFailed"):
        assert token in err, f"{token!r} absent from render: {err!r}"


def test_print_error_paths_is_quiet_on_a_clean_run(capsys):
    """A clean run must be byte-identical to before this change."""
    owncloud_sync._print_error_paths({"errors": 0, "error_paths": []})
    owncloud_sync._print_error_paths({"errors": 0})
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err == "", f"noise on a clean run: {cap!r}"


def test_print_error_paths_reports_overflow_from_both_sources(capsys):
    """`_record_error` caps its list at 40 and counts the overflow; this render
    caps again at its own limit. BOTH shortfalls must be reported or the tail
    is silently dropped — the exact ambiguity _record_error's cap avoids."""
    stats = {"errors": 55, "error_paths": [dict(_ERR[0]) for _ in range(40)],
             "error_paths_truncated": 15}
    owncloud_sync._print_error_paths(stats, limit=10)
    err = capsys.readouterr().err
    # 30 not shown by this render + 15 never recorded = 45
    assert "45 more error(s) not shown" in err, err


# ----------------------------------------------------------- endpoint render

def _admin():
    sys.path.insert(0, str(CORE_SCRIPTS.parent.parent))
    from mind_api.src.endpoints import admin
    return admin


def test_sync_file_payload_carries_error_identity(monkeypatch):
    admin = _admin()
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")

    def _fake_sync_file(be, target, *, dry_run, stats_out=None):
        stats_out.update({"scanned": 1, "pushed": 0, "errors": 1,
                          "conflicts": 0, "diverged_skipped": 0,
                          "error_paths": _ERR[:1], "error_paths_truncated": 3})
        return 1

    monkeypatch.setattr(owncloud_sync, "sync_file", _fake_sync_file)
    import storage_backend
    monkeypatch.setattr(storage_backend, "get_backend", lambda: object())

    body = _call_sync_file(admin, "/w/world/x.md")
    assert body["errors"] == 1 and body["ok"] is False
    assert body.get("error_paths") == _ERR[:1], (
        "the endpoint dropped the identity list the producer recorded — this is "
        "the g-115-7255 defect")
    assert body.get("error_paths_truncated") == 3


def test_sync_file_payload_omits_the_key_on_a_clean_push(monkeypatch):
    """Absent, not an empty list: a caller must not have to distinguish
    'no errors' from 'errors whose names were lost'."""
    admin = _admin()
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")

    def _fake_sync_file(be, target, *, dry_run, stats_out=None):
        stats_out.update({"scanned": 1, "pushed": 1, "errors": 0,
                          "conflicts": 0, "diverged_skipped": 0,
                          "error_paths": []})
        return 0

    monkeypatch.setattr(owncloud_sync, "sync_file", _fake_sync_file)
    import storage_backend
    monkeypatch.setattr(storage_backend, "get_backend", lambda: object())

    body = _call_sync_file(admin, "/w/world/x.md")
    assert body["ok"] is True and body["pushed"] == 1
    assert "error_paths" not in body and "error_paths_truncated" not in body


def test_flush_payload_carries_error_identity(monkeypatch):
    """guard-1579 put `pruned_agent_names` in this payload for exactly this
    reason; `errors` sat four lines below it naming nothing."""
    admin = _admin()
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setattr(owncloud_sync, "sweep", lambda *a, **k: {
        "scanned": 9, "pushed": 1, "in_sync": 0, "skipped_unchanged": 0,
        "conflicts": 0, "errors": 2, "pruned_agents": 0,
        "pruned_agent_names": [], "error_paths": _ERR})
    import storage_backend
    monkeypatch.setattr(storage_backend, "get_backend", lambda: object())

    body = _call_flush(admin)
    assert body["errors"] == 2
    assert body.get("error_paths") == _ERR, (
        "flush reported a count with no identities — the same defect the "
        "adjacent pruned_agent_names field already fixed for prunes")


def test_flush_payload_omits_the_key_when_clean(monkeypatch):
    admin = _admin()
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setattr(owncloud_sync, "sweep", lambda *a, **k: {
        "scanned": 9, "pushed": 9, "in_sync": 0, "skipped_unchanged": 0,
        "conflicts": 0, "errors": 0, "pruned_agents": 0,
        "pruned_agent_names": [], "error_paths": []})
    import storage_backend
    monkeypatch.setattr(storage_backend, "get_backend", lambda: object())

    body = _call_flush(admin)
    assert body["errors"] == 0 and "error_paths" not in body


# ------------------------------------------------------------------ harness

class _Ctx:
    """Minimal ctx: the two endpoints read only .query and .paths.project_root."""

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


def _call_sync_file(admin, path):
    return _body(admin.owncloud_sync_file(_Ctx({"path": path})))


def _call_flush(admin):
    return _body(admin.owncloud_flush(_Ctx()))
# ------------------------------------------------- producer -> render seam

def test_producer_to_render_seam_no_hand_built_stats(capsys):
    """The one test that routes a REAL error through the producer.

    Every other test in this file hands `_print_error_paths` a stats dict it
    built itself, so all of them stay green if `_record_error` renames the key
    or changes the entry shape -- the render would faithfully print nothing and
    the suite would agree. That is guard-3871 exactly (a hand-written dict
    literal between a producer and a consumer IS a schema) and guard-920 /
    rb-5235 (a regression test must replicate the PRODUCTION arg shape, not the
    contract-ideal one) -- the same class this file exists to fix, inverted:
    there the producer recorded and the render dropped; here the render works
    and only a producer change would break it.

    Found by /fresh-eyes-code on g-115-7255, in the fix's own tests.
    """
    stats = {}
    owncloud_sync._record_error(
        stats, "/w/world/board/findings.jsonl",
        ValueError("same-heading divergence"), phase="union-merge-push")

    # Producer's own output, never a literal -- if the key moves, this is the
    # test that goes red.
    assert stats.get("errors") == 1
    owncloud_sync._print_error_paths(stats)
    err = capsys.readouterr().err

    for token in ("/w/world/board/findings.jsonl", "union-merge-push",
                  "ValueError", "same-heading divergence"):
        assert token in err, (
            f"{token!r} was recorded by _record_error but did not reach the "
            f"operator through _print_error_paths. Got: {err!r}")


def test_producer_to_render_seam_survives_overflow(capsys):
    """Same seam at the cap boundary, again with no hand-built entries."""
    stats = {}
    for i in range(owncloud_sync._ERROR_PATHS_CAP + 3):
        owncloud_sync._record_error(
            stats, f"/w/f{i}.jsonl", OSError("disk"), phase="put")
    assert stats["errors"] == owncloud_sync._ERROR_PATHS_CAP + 3
    assert stats["error_paths_truncated"] == 3

    owncloud_sync._print_error_paths(stats)
    err = capsys.readouterr().err
    # The overflow the producer counted must be reported, not silently dropped.
    assert "3 more error(s) not shown" in err, err
