"""GET/POST /v1/history/{list,diff,restore,prune,prune-legacy}.

Two layers:
  1. HTTP round-trip (running_daemon): wiring, missing-param 400s, no-history,
     world-file restore (file restored + changelog appended), agent-file restore
     requiring the X-Mind-Agent header (no header -> agent snapshots invisible
     -> 404), empty prune/prune-legacy.
  2. Byte-compat (direct handler vs the REAL CLI history.py): response body ==
     CLI STDOUT for list/diff; restored file bytes + changelog schema for
     restore; prune/prune-legacy stdout + surviving-file sets.

The CLI subprocess uses MIND_WORLD/MIND_META/MIND_AGENT(_DIR) so it reads/
writes a temp tree. file args are ABSOLUTE (the wrapper-cut contract; world/meta
are external). Subprocess output captured with encoding="utf-8" (universal
newlines). Seeds use legacy raw + gz snapshots AND a real new-store CAS-delta
snapshot via _history_store.save.
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HISTORY_PY = REPO_ROOT / "core" / "scripts" / "history.py"

# Importing the endpoint module also puts core/scripts on sys.path (its module
# top does the insert), so `import _history_store` works afterwards.
from mind_api.src.endpoints import history as history_ep  # noqa: E402
import _history_store  # noqa: E402


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

def _make_legacy_snap(base, rel, ts_str, agent, content, *, gz=False, meta=None):
    """Create <base>/.history/<rel>/<ts>_<agent><ext>[.gz] (+ optional .meta)."""
    d = Path(base) / ".history" / rel
    d.mkdir(parents=True, exist_ok=True)
    ext = Path(rel).suffix
    name = f"{ts_str}_{agent}{ext}"
    if gz:
        p = d / (name + ".gz")
        with gzip.open(p, "wt", encoding="utf-8") as fh:
            fh.write(content)
    else:
        p = d / name
        p.write_text(content, encoding="utf-8")
    if meta is not None:
        (d / (p.name + ".meta")).write_text(meta + "\n", encoding="utf-8")
    return p


def _read_changelog(base):
    p = Path(base) / "changelog.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _run_cli(world, meta, agent_dir, args, allowed_rcs=(0,)):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(agent_dir)
    Path(world).mkdir(parents=True, exist_ok=True)
    Path(meta).mkdir(parents=True, exist_ok=True)
    Path(agent_dir).mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(HISTORY_PY), *args],
        text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    assert proc.returncode in allowed_rcs, (
        f"CLI history.py {args} rc={proc.returncode} not in {allowed_rcs}:\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc.stdout


class _FakePaths:
    def __init__(self, world, meta, agent, project_root):
        self.world = world
        self.meta = meta
        self.agent = agent
        self.project_root = project_root


class _FakeCtx:
    def __init__(self, world, meta, agent, project_root, query, *, agent_name="alpha"):
        self.paths = _FakePaths(world, meta, agent, project_root)
        self.query = query
        self.body = b""
        self.headers = {"x-mind-agent": agent_name} if agent_name else {}


def _http(method, port, path, query=None, *, agent="alpha"):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    req = urllib.request.Request(url, data=b"" if method == "POST" else None, method=method)
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# HTTP round-trip
# ---------------------------------------------------------------------------

def test_list_no_history(running_daemon):
    project_root, port = running_daemon
    f = project_root / "world" / "knowledge" / "nope.md"
    status, body = _http("GET", port, "/v1/history/list", {"file": str(f)})
    assert status == 200
    assert body == f"No history for {f}\n"


def test_list_missing_param(running_daemon):
    _, port = running_daemon
    try:
        _http("GET", port, "/v1/history/list")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_param"
    else:
        raise AssertionError("expected 400 without file")


# ---------------------------------------------------------------------------
#  — an unresolvable path is an ERROR, never an empty store.
#
# `_find_history_snapshots` returns [] both for "no snapshots" and for "under
# no governed root", so `list` rendered the second as "No history" at HTTP 200
# — a false-absent verdict on the recovery layer. `restore` had guarded this
# with unresolved_base since it shipped; list and diff were the outliers, and
# list is the read the recovery path actually depends on.
#
# test_list_no_history above is the deliberate COMPLEMENT of these: its path IS
# under the world root (the fixture sets WORLD_PATH=project_root/"world") and
# simply has no snapshots, so it must stay 200. The discriminator is "under a
# governed root", never "exists on disk" — snapshots outlive their file.
# ---------------------------------------------------------------------------

def test_list_out_of_root_returns_400(running_daemon):
    """A path under no configured base must not read as an empty store."""
    _, port = running_daemon
    try:
        _http("GET", port, "/v1/history/list", {"file": "/etc/hostname"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "unresolved_base"
    else:
        raise AssertionError("expected 400 for a path under no governed root")


def test_diff_out_of_root_returns_400(running_daemon):
    """diff blamed the VERSION when the PATH was the problem."""
    _, port = running_daemon
    try:
        _http("GET", port, "/v1/history/diff",
              {"file": "/etc/hostname", "version": "whatever"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "unresolved_base"
    else:
        raise AssertionError("expected 400 for a path under no governed root")


def test_list_world_prefix_resolves_to_world_root(tmp_path):
    """`world/x` must resolve to the CONFIGURED world root, not project_root.

    The false-NEGATIVE half of g-115-4181.

    DELIBERATELY a direct-handler test, not an HTTP round-trip. The
    `running_daemon` fixture puts world at project_root/"world", so the buggy
    branch (project_root / "world/x") and the correct branch (world_root / "x")
    resolve to the SAME path — an HTTP version of this test passes whether or
    not the fix is present, and mutation testing caught it doing exactly that
    (21/21 green with the prefix branch deleted). Production has world
    EXTERNAL, per local-paths.conf, which is why the bug was live in production
    and structurally invisible under that fixture.

    `_dirs`-style tmp roots reproduce the production shape: world is outside
    project_root, so the two branches diverge and the assertion has teeth.
    """
    world = tmp_path / "external-world"
    meta = tmp_path / "external-meta"
    agent = tmp_path / "external-agent"
    rel = "knowledge/prefixed.md"
    (world / rel).parent.mkdir(parents=True, exist_ok=True)
    (world / rel).write_text("current\n", encoding="utf-8")
    _make_legacy_snap(world, rel, "2026-07-01T10-00-00", "alpha", "older\n")

    ctx = _FakeCtx(world, meta, agent, REPO_ROOT, {"file": f"world/{rel}"})
    body = history_ep.list_versions(ctx).body.decode("utf-8")
    assert "No history" not in body, body
    assert "1 versions" in body, body


def test_list_meta_prefix_resolves_to_meta_root(tmp_path):
    """Same for `meta/` — broken identically, by the same cause."""
    world = tmp_path / "external-world"
    meta = tmp_path / "external-meta"
    agent = tmp_path / "external-agent"
    rel = "strategy.yaml"
    (meta / rel).parent.mkdir(parents=True, exist_ok=True)
    (meta / rel).write_text("current\n", encoding="utf-8")
    _make_legacy_snap(meta, rel, "2026-07-01T10-00-00", "alpha", "older\n")

    ctx = _FakeCtx(world, meta, agent, REPO_ROOT, {"file": f"meta/{rel}"})
    body = history_ep.list_versions(ctx).body.decode("utf-8")
    assert "No history" not in body, body
    assert "1 versions" in body, body


def test_diff_missing_param(running_daemon):
    _, port = running_daemon
    try:
        _http("GET", port, "/v1/history/diff", {"file": "x"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
    else:
        raise AssertionError("expected 400 without version")


def test_restore_version_not_found(running_daemon):
    project_root, port = running_daemon
    f = project_root / "world" / "knowledge" / "ghost.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("body\n", encoding="utf-8")
    try:
        _http("POST", port, "/v1/history/restore", {"file": str(f), "version": "none"})
    except urllib.error.HTTPError as e:
        assert e.code == 404
    else:
        raise AssertionError("expected 404 for missing version")


def test_restore_world_roundtrip(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    rel = "knowledge/rt.md"
    f = world / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("CURRENT\n", encoding="utf-8")
    snap = _make_legacy_snap(world, rel, "2026-05-20T10-00-00", "alpha", "SNAPSHOT\n")
    status, body = _http("POST", port, "/v1/history/restore",
                         {"file": str(f), "version": snap.name})
    assert status == 200, body
    assert f.read_text(encoding="utf-8") == "SNAPSHOT\n"
    cl = _read_changelog(world)
    assert cl and cl[-1]["action"] == "restore"
    assert cl[-1]["agent"] == "alpha"


def test_restore_agent_file_requires_header(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    rel = "agent-note.md"
    f = agent_dir / rel
    f.write_text("CURRENT\n", encoding="utf-8")
    snap = _make_legacy_snap(agent_dir, rel, "2026-05-20T10-00-00", "alpha", "OLD\n")
    # No header -> agent branch skipped in resolve -> snapshot invisible -> 404.
    try:
        _http("POST", port, "/v1/history/restore",
              {"file": str(f), "version": snap.name}, agent=None)
    except urllib.error.HTTPError as e:
        assert e.code == 404
    else:
        raise AssertionError("expected 404 restoring agent file without header")
    # With header -> agent branch included -> restore succeeds.
    status, body = _http("POST", port, "/v1/history/restore",
                         {"file": str(f), "version": snap.name}, agent="alpha")
    assert status == 200, body
    assert f.read_text(encoding="utf-8") == "OLD\n"


def test_prune_empty(running_daemon):
    _, port = running_daemon
    status, body = _http("POST", port, "/v1/history/prune")
    assert status == 200
    assert body == "No .history/ directories found.\n"


def test_prune_legacy_empty(running_daemon):
    _, port = running_daemon
    status, body = _http("POST", port, "/v1/history/prune-legacy")
    assert status == 200
    assert body == "No .history/ directories found.\n"


# ---------------------------------------------------------------------------
# Byte-compat
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HISTORY_PY.exists(), reason="core/scripts/history.py missing")
class TestByteCompat:
    def _dirs(self, tmp_path, name="w"):
        world = tmp_path / f"{name}-world"
        meta = tmp_path / f"{name}-meta"
        agent = tmp_path / f"{name}-agent"
        return world, meta, agent

    # --- list ---------------------------------------------------------------

    def test_list_byte_compat(self, tmp_path):
        world, meta, agent = self._dirs(tmp_path)
        rel = "knowledge/foo.md"
        f = world / rel
        _make_legacy_snap(world, rel, "2026-05-20T10-00-00", "alpha", "v1\n")
        _make_legacy_snap(world, rel, "2026-05-20T11-00-00", "bravo",
                          "v2 longer content here\n", meta="edited twice")
        _make_legacy_snap(world, rel, "2026-05-21T09-00-00", "alpha", "v3\n", gz=True)
        # Real new-store CAS-delta snapshot -> exercises the [new] tag path.
        _history_store.save(f, b"cas store content\n", world, "charlie", summary="cas snap")
        cli = _run_cli(world, meta, agent, ["list", str(f)])
        body = history_ep.list_versions(
            _FakeCtx(world, meta, agent, REPO_ROOT, {"file": str(f)})).body.decode("utf-8")
        assert body == cli

    def test_list_no_history_byte_compat(self, tmp_path):
        world, meta, agent = self._dirs(tmp_path)
        f = world / "knowledge" / "absent.md"
        (world).mkdir(parents=True, exist_ok=True)
        cli = _run_cli(world, meta, agent, ["list", str(f)])
        body = history_ep.list_versions(
            _FakeCtx(world, meta, agent, REPO_ROOT, {"file": str(f)})).body.decode("utf-8")
        assert body == cli

    # --- diff ---------------------------------------------------------------

    def test_diff_byte_compat_raw(self, tmp_path):
        world, meta, agent = self._dirs(tmp_path)
        rel = "knowledge/foo.md"
        f = world / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("line1\nline2-NEW\nline3\n", encoding="utf-8")
        snap = _make_legacy_snap(world, rel, "2026-05-20T10-00-00", "alpha",
                                 "line1\nline2-OLD\nline3\n")
        cli = _run_cli(world, meta, agent, ["diff", str(f), snap.name])
        body = history_ep.diff(_FakeCtx(world, meta, agent, REPO_ROOT,
                                        {"file": str(f), "version": snap.name})).body.decode("utf-8")
        assert body == cli

    def test_diff_byte_compat_gz(self, tmp_path):
        world, meta, agent = self._dirs(tmp_path)
        rel = "knowledge/foo.md"
        f = world / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("alpha\nbeta\n", encoding="utf-8")
        snap = _make_legacy_snap(world, rel, "2026-05-20T10-00-00", "alpha",
                                 "alpha\nGAMMA\n", gz=True)
        cli = _run_cli(world, meta, agent, ["diff", str(f), snap.name])
        body = history_ep.diff(_FakeCtx(world, meta, agent, REPO_ROOT,
                                        {"file": str(f), "version": snap.name})).body.decode("utf-8")
        assert body == cli

    def test_diff_byte_compat_no_difference(self, tmp_path):
        world, meta, agent = self._dirs(tmp_path)
        rel = "knowledge/foo.md"
        f = world / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("same\ncontent\n", encoding="utf-8")
        snap = _make_legacy_snap(world, rel, "2026-05-20T10-00-00", "alpha", "same\ncontent\n")
        cli = _run_cli(world, meta, agent, ["diff", str(f), snap.name])
        body = history_ep.diff(_FakeCtx(world, meta, agent, REPO_ROOT,
                                        {"file": str(f), "version": snap.name})).body.decode("utf-8")
        assert body == cli
        assert body == "No differences.\n"

    # --- restore ------------------------------------------------------------

    def test_restore_byte_compat(self, tmp_path):
        cw, cm, ca = self._dirs(tmp_path, "cli")
        dw, dm, da = self._dirs(tmp_path, "dae")
        rel = "knowledge/foo.md"
        snap_content = "RESTORED CONTENT\nsecond line\n"
        version = "2026-05-20T10-00-00_alpha.md"
        for w in (cw, dw):
            f = w / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("current different\n", encoding="utf-8")
            _make_legacy_snap(w, rel, "2026-05-20T10-00-00", "alpha", snap_content)
        cli_f = cw / rel
        dae_f = dw / rel
        cli_out = _run_cli(cw, cm, ca, ["restore", str(cli_f), version])
        dae_out = history_ep.restore(
            _FakeCtx(dw, dm, da, REPO_ROOT, {"file": str(dae_f), "version": version})).body.decode("utf-8")
        # Restored content byte-identical to the snapshot on both sides.
        assert cli_f.read_text(encoding="utf-8") == snap_content
        assert dae_f.read_text(encoding="utf-8") == snap_content
        # stdout matches each side's own (path-bearing) template.
        assert cli_out == f"Restored {cli_f} from {version}\n"
        assert dae_out == f"Restored {dae_f} from {version}\n"
        # changelog entries match modulo timestamp (same rel file, agent, action).
        cli_e = {k: v for k, v in _read_changelog(cw)[-1].items() if k != "timestamp"}
        dae_e = {k: v for k, v in _read_changelog(dw)[-1].items() if k != "timestamp"}
        assert cli_e == dae_e
        assert cli_e["action"] == "restore"

    # --- prune --------------------------------------------------------------

    def _seed_prune_tree(self, world):
        rel = "knowledge/foo.md"
        today = datetime.now().date()
        old = today - timedelta(days=40)   # weekly tier
        recent = today - timedelta(days=1)  # keep-all tier
        ods = old.strftime("%Y-%m-%d")
        _make_legacy_snap(world, rel, f"{ods}T10-00-00", "alpha", "old-a\n")
        _make_legacy_snap(world, rel, f"{ods}T11-00-00", "alpha", "old-b\n")
        _make_legacy_snap(world, rel, f"{recent.strftime('%Y-%m-%d')}T09-00-00",
                          "alpha", "recent\n")
        return rel

    def test_prune_dry_run_byte_compat(self, tmp_path):
        # Dry-run mutates nothing -> CLI and daemon can share one world, so the
        # absolute paths in the detail line match exactly.
        world, meta, agent = self._dirs(tmp_path)
        self._seed_prune_tree(world)
        cli = _run_cli(world, meta, agent, ["prune", "--dry-run"])
        body = history_ep.prune(
            _FakeCtx(world, meta, agent, REPO_ROOT, {"dry_run": "true"})).body.decode("utf-8")
        assert body == cli

    def test_prune_apply_byte_compat(self, tmp_path):
        cw, cm, ca = self._dirs(tmp_path, "cli")
        dw, dm, da = self._dirs(tmp_path, "dae")
        rel = self._seed_prune_tree(cw)
        self._seed_prune_tree(dw)
        cli = _run_cli(cw, cm, ca, ["prune"])  # real delete
        body = history_ep.prune(
            _FakeCtx(dw, dm, da, REPO_ROOT, {})).body.decode("utf-8")  # dry_run default false
        assert body == cli  # summary only (real prune emits no detail lines)
        cli_survivors = sorted(p.name for p in (cw / ".history" / rel).iterdir())
        dae_survivors = sorted(p.name for p in (dw / ".history" / rel).iterdir())
        assert cli_survivors == dae_survivors

    # --- prune-legacy -------------------------------------------------------

    def test_prune_legacy_dry_run_byte_compat(self, tmp_path):
        world, meta, agent = self._dirs(tmp_path)
        rel = "knowledge/foo.md"
        snap = _make_legacy_snap(world, rel, "2026-01-01T10-00-00", "alpha", "legacy old\n")
        old_mtime = (datetime.now() - timedelta(days=60)).timestamp()
        os.utime(snap, (old_mtime, old_mtime))
        # New-store coverage so the coverage gate passes.
        _history_store.save(world / rel, b"covered now\n", world, "alpha", summary="cov")
        cli = _run_cli(world, meta, agent, ["prune-legacy"])  # default dry-run
        body = history_ep.prune_legacy(
            _FakeCtx(world, meta, agent, REPO_ROOT, {})).body.decode("utf-8")
        assert body == cli

    def test_prune_legacy_skips_no_coverage(self, tmp_path):
        world, meta, agent = self._dirs(tmp_path)
        rel = "knowledge/bar.md"
        snap = _make_legacy_snap(world, rel, "2026-01-01T10-00-00", "alpha", "no cov\n")
        old_mtime = (datetime.now() - timedelta(days=60)).timestamp()
        os.utime(snap, (old_mtime, old_mtime))
        # NO new-store coverage -> should be skipped (no-coverage), not deleted.
        cli = _run_cli(world, meta, agent, ["prune-legacy"])
        body = history_ep.prune_legacy(
            _FakeCtx(world, meta, agent, REPO_ROOT, {})).body.decode("utf-8")
        assert body == cli
        assert "no-coverage" in body
