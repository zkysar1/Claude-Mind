"""Cross-box worker manifest tests (-a, asp-306 two-bodies G2a).

Covers the `--reducer-sid remote` override on body-manifest write: the path a
worker Body takes when the reducer holds the DDB runner-claim from a DIFFERENT
machine, activated by a bare `/start <agent>` after a cross-box rc=4 (role
derivation, 2026-08-03; the interim `--body worker` flag was removed same-day).

The load-bearing test here is `test_without_override_a_worker_box_does_not_fork`
— the NEGATIVE CONTROL. The default fork decision reads the LOCAL
`running-session-id`, and a worker box never writes one (staying IDLE is the
whole point of the CW branch), so the default silently yields fork_needed=False.
Without a fork the worker mutates the agent-wide WM, which is `sync_tier:
continuity` (LWW) — the live reducer's writes and this box's would silently
destroy each other. That test fails if the override is ever removed, which is
what makes the positive tests below mean something rather than merely pass.

Daemon-safe (pure path + file arithmetic; no daemon, no network).

Run:
  STORAGE_BACKEND=local python -m pytest \
      core/scripts/tests/test_body_manifest_remote_worker.py -q
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

CORE_SCRIPTS = Path(__file__).resolve().parent.parent  # core/scripts/


def _load_body_manifest():
    """Load the hyphen-named module via importlib (not importable by name)."""
    spec = importlib.util.spec_from_file_location(
        "body_manifest", CORE_SCRIPTS / "body-manifest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bm = _load_body_manifest()

SID_WORKER = "33333333-3333-4333-8333-333333333333"
SID_OTHER = "44444444-4444-4444-8444-444444444444"
WM_TEXT = "goals_completed: 7\nactive_context:\n  summary: from-the-reducer\n"


def _mk_worker_box(tmp_path: Path, name: str = "alpha",
                   running_sid: str | None = None) -> Path:
    """A worker box: agent dir + a WM, and (by default) NO running-session-id.

    `running_sid=None` is the realistic cross-box shape and the default here —
    box B never writes that file.
    """
    adir = tmp_path / "agents" / name
    state = adir / "session"
    state.mkdir(parents=True, exist_ok=True)
    if running_sid is not None:
        (state / "running-session-id").write_text(running_sid, encoding="utf-8")
    (state / "working-memory.yaml").write_bytes(WM_TEXT.encode("utf-8"))
    return tmp_path


def _read_yaml(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8"))


# ── The negative control ────────────────────────────────────────────────────

def test_without_override_a_worker_box_does_not_fork(tmp_path):
    """MUTATION GUARD: delete the `if remote: fork_needed = True` line and this
    test still passes while every positive test below fails — that asymmetry is
    the point. It pins the DEFECT, proving the override is reachable and needed.
    """
    root = _mk_worker_box(tmp_path)  # no running-session-id, as on a real box B
    path = bm.write_manifest(SID_WORKER, "alpha", "local", "worker",
                             project_root=root)
    data = _read_yaml(path)
    assert data["forked_wm_hash"] is None, (
        "default path forked on a worker box; if this changed deliberately, the "
        "override may be redundant — re-derive before deleting either")
    body_wm = root / "agents" / "alpha" / "sessions" / SID_WORKER / "working-memory.yaml"
    assert not body_wm.exists()
    assert data["remote_body"] is False


# ── The fix ─────────────────────────────────────────────────────────────────

def test_remote_override_forces_the_fork(tmp_path):
    root = _mk_worker_box(tmp_path)
    path = bm.write_manifest(SID_WORKER, "alpha", "local", "worker",
                             project_root=root,
                             reducer_sid=bm.REMOTE_REDUCER_SENTINEL)
    data = _read_yaml(path)
    assert data["forked_wm_hash"] is not None
    body_wm = root / "agents" / "alpha" / "sessions" / SID_WORKER / "working-memory.yaml"
    assert body_wm.read_bytes() == WM_TEXT.encode("utf-8"), (
        "the fork must be a byte-exact copy — forked_wm_hash is only a valid "
        "merge baseline if it hashes the bytes actually written")


def test_remote_override_writes_the_merge_baseline(tmp_path):
    """The 3-way-delta common ancestor. Without it the reducer-side merge falls
    back to 2-way union+SUM, which double-counts counters."""
    root = _mk_worker_box(tmp_path)
    bm.write_manifest(SID_WORKER, "alpha", "local", "worker",
                      project_root=root,
                      reducer_sid=bm.REMOTE_REDUCER_SENTINEL)
    sess = root / "agents" / "alpha" / "sessions" / SID_WORKER
    baseline = sess / "forked-wm-baseline.yaml"
    assert baseline.read_bytes() == WM_TEXT.encode("utf-8")
    assert baseline.read_bytes() == (sess / "working-memory.yaml").read_bytes()


def test_remote_override_records_reducer_sid_sentinel(tmp_path):
    root = _mk_worker_box(tmp_path)
    data = _read_yaml(bm.write_manifest(
        SID_WORKER, "alpha", "local", "worker", project_root=root,
        reducer_sid=bm.REMOTE_REDUCER_SENTINEL))
    assert data["reducer_sid"] == "remote"


def test_remote_body_and_machine_id_are_recorded(tmp_path):
    root = _mk_worker_box(tmp_path)
    data = _read_yaml(bm.write_manifest(
        SID_WORKER, "alpha", "local", "worker", project_root=root,
        reducer_sid=bm.REMOTE_REDUCER_SENTINEL))
    assert data["remote_body"] is True
    # NOT merely "a non-empty string" — that assertion passes even when the
    # resolver's import fails and it returns its "unknown" fallback, so it could
    # not distinguish a working resolver from a silently broken one. Pin it to
    # the resolver's ACTUAL output, and separately reject the fallback: together
    # these fail if the import breaks under any invocation shape.
    assert data["machine_id"] == bm._resolve_machine_id()
    assert data["machine_id"] != "unknown", (
        "machine_id fell back to 'unknown' — the _session_telemetry import "
        "broke; attribution of a cross-box merge conflict depends on this")


def test_bool_renders_lowercase_for_non_pyyaml_parsers(tmp_path):
    """the framework-ES also reads this manifest; YAML 1.2 parsers reject bare `True`.
    Asserted on the RAW TEXT because yaml.safe_load would accept either form and
    so cannot distinguish them."""
    root = _mk_worker_box(tmp_path)
    path = bm.write_manifest(SID_WORKER, "alpha", "local", "worker",
                             project_root=root,
                             reducer_sid=bm.REMOTE_REDUCER_SENTINEL)
    raw = path.read_text(encoding="utf-8")
    assert "remote_body: true" in raw
    assert "remote_body: True" not in raw


# ── Rejections ──────────────────────────────────────────────────────────────

def test_an_invented_reducer_sid_is_rejected(tmp_path):
    """A cross-box reducer SID cannot be read from this machine. Accepting one
    would let a caller mis-address the reducer-side merge with a value that
    looks entirely plausible."""
    root = _mk_worker_box(tmp_path)
    with pytest.raises(ValueError, match="remote"):
        bm.write_manifest(SID_WORKER, "alpha", "local", "worker",
                          project_root=root, reducer_sid=SID_OTHER)


def test_remote_is_rejected_for_non_worker_roles(tmp_path):
    root = _mk_worker_box(tmp_path)
    for role in ("reducer", "observer"):
        with pytest.raises(ValueError, match="role"):
            bm.write_manifest(SID_WORKER, "alpha", "local", role,
                              project_root=root,
                              reducer_sid=bm.REMOTE_REDUCER_SENTINEL)


# ── Backward compatibility ──────────────────────────────────────────────────

def test_reducer_path_is_unchanged_apart_from_the_new_fields(tmp_path):
    root = _mk_worker_box(tmp_path, running_sid=SID_WORKER)
    data = _read_yaml(bm.write_manifest(SID_WORKER, "alpha", "local", "reducer",
                                        project_root=root))
    assert data["reducer_sid"] is None
    assert data["forked_wm_hash"] is None
    assert data["remote_body"] is False


def test_same_box_worker_fork_still_keys_on_running_session_id(tmp_path):
    """The pre-existing same-box case must keep working: a DIFFERENT live body
    holds running-session-id, so the local read still decides."""
    root = _mk_worker_box(tmp_path, running_sid=SID_OTHER)
    data = _read_yaml(bm.write_manifest(SID_WORKER, "alpha", "local", "worker",
                                        project_root=root))
    assert data["forked_wm_hash"] is not None
    assert data["reducer_sid"] == SID_OTHER
    assert data["remote_body"] is False, (
        "a same-box worker is not a remote body — conflating them would tell "
        "the reducer to expect an explicitly-pushed staged WM that never comes")


# ── The post-CW box invariant ───────────────────────────────────────────────

def test_worker_box_is_not_reducer_shaped_after_the_fork(tmp_path):
    """Simulates /start's CW-pre + CW1b: the triple-write happened, CW-pre
    removed all three files, then the manifest was written. Asserts the invariant
    the whole cross-box design rests on — with no running-session-id, stop-hook
    Gate 0 always mismatches, is_reducer() is false, and recovery-gate is
    indifferent. If this ever fails, box B looks like a second reducer and the
    shared stores get two writers.
    """
    root = _mk_worker_box(tmp_path)
    state = root / "agents" / "alpha" / "session"
    # The triple-write that ran before the acquire refused.
    for fname in ("running-session-id", "latest-session-id", "runner-token"):
        (state / fname).write_text("placeholder", encoding="utf-8")
    # CW-pre.
    for fname in ("running-session-id", "latest-session-id", "runner-token"):
        (state / fname).unlink()
    # CW1b.
    bm.write_manifest(SID_WORKER, "alpha", "local", "worker",
                      project_root=root,
                      reducer_sid=bm.REMOTE_REDUCER_SENTINEL)

    for fname in ("running-session-id", "latest-session-id", "runner-token"):
        assert not (state / fname).exists(), f"{fname} must not exist on a worker box"
    assert bm.is_reducer(SID_WORKER, "alpha", root) is False
    # ...and the fork still happened despite running-session-id being absent.
    assert (root / "agents" / "alpha" / "sessions" / SID_WORKER
            / "working-memory.yaml").is_file()


# ── CLI boundary ────────────────────────────────────────────────────────────

def test_cli_rejects_an_invented_sid_at_parse_time(tmp_path, capsys):
    """argparse choices= makes this a parse error, so /start's CW1b cannot pass
    a bad value through even if the caller is wrong."""
    root = _mk_worker_box(tmp_path)
    with pytest.raises(SystemExit):
        bm.main(["write", "--sid", SID_WORKER, "--agent", "alpha",
                 "--reducer-sid", SID_OTHER])


def test_cli_accepts_remote(tmp_path, monkeypatch):
    root = _mk_worker_box(tmp_path)
    monkeypatch.setattr(bm, "_project_root", lambda: root)
    rc = bm.main(["write", "--sid", SID_WORKER, "--agent", "alpha",
                  "--role", "worker", "--reducer-sid", "remote"])
    assert rc == 0
    data = _read_yaml(root / "agents" / "alpha" / "sessions" / SID_WORKER
                      / "body-manifest.yaml")
    assert data["remote_body"] is True
    assert data["reducer_sid"] == "remote"
