"""test_check_upstream.py — Wave 2 downstream-listener coverage (daemon-safe).

Two layers, mirroring test_release.py:

  1. Pure-lib unit tests for the Wave-2 classification/chain functions in
     _release_lib.py (releases_above, classify_update chain-walk, upstream_role)
     + the classify-chain CLI (env-driven, parse-or-fail exit 2).
  2. Black-box subprocess tests of check-upstream.sh in SAFE modes only:
       - frontier short-circuit against the REAL overlay (read-only, no network),
       - MIND_WORLD-redirected downstream overlays + a file:// feed (curl reads a
         local tmp RELEASES.json) exercising every exit code (0/2/4/5) and the
         config-error paths.
     The tests NEVER --auto-apply (no snapshot tag, no IDLE gate), NEVER hit the
     network, and NEVER mutate the real repo. MIND_WORLD redirects only the
     overlay; CHECK_UPSTREAM_URL pins the feed to a local file:// URL.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
for _p in (str(CORE_SCRIPTS), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _release_lib as L  # noqa: E402
from _bash_helpers import BASH  # noqa: E402

CHECK_SH = CORE_SCRIPTS / "check-upstream.sh"
LIB = CORE_SCRIPTS / "_release_lib.py"
INIT_PY = PROJECT_ROOT / "mind_api" / "src" / "__init__.py"
# Connection-refused on a closed port → deterministic, fast fail-closed.
DEAD_URL = "http://127.0.0.1:9/no-such-feed"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _local_version() -> str:
    for line in INIT_PY.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split('"')[1]
    raise AssertionError("no __version__ in real __init__.py")


def _below(version: str):
    """A strictly-lower semver than `version`, or None if none exists (0.0.0)."""
    maj, mn, pa = L.parse_version(version)
    if pa > 0:
        return f"{maj}.{mn}.{pa - 1}"
    if mn > 0:
        return f"{maj}.{mn - 1}.0"
    if maj > 0:
        return f"{maj - 1}.0.0"
    return None


def _feed(entries):
    """Serialize a RELEASES.json array (newest-first) to JSON text."""
    import json
    return json.dumps(entries, indent=2)


def _entry(version, prev, breaking=False, cross_world=False, recipe=None):
    return {
        "version": version, "previous_version": prev, "breaking": breaking,
        "cross_world": cross_world, "summary": "t",
        "upgrade_recipe": recipe, "rollback_recipe": None, "min_source": prev,
    }


def _write_overlay(world: Path, *, self_role=None, with_sources_url=None):
    (world / "config").mkdir(parents=True, exist_ok=True)
    lines = []
    if self_role is not None:
        lines.append(f"self_role: {self_role}")
    if with_sources_url is not None:
        lines += ["sources:", "  frontier:", f'    releases_url: "{with_sources_url}"']
    (world / "config" / "compatibility.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_check(*args, world=None, url=None, extra_env=None):
    env = os.environ.copy()
    if world is not None:
        env["MIND_WORLD"] = str(world)
    if url is not None:
        env["CHECK_UPSTREAM_URL"] = url
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [BASH, str(CHECK_SH), *args],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
    )


def lib_classify(upstream_path: Path, local: str):
    env = os.environ.copy()
    env["UPSTREAM_RELEASES_PATH"] = str(upstream_path)
    env["LOCAL_VERSION"] = local
    return subprocess.run(
        [sys.executable, str(LIB), "classify-chain"],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
    )


# ===========================================================================
# 1. Pure-lib: releases_above
# ===========================================================================
def test_releases_above_sorts_oldest_first():
    rel = [_entry("0.5.0", "0.4.0"), _entry("0.4.0", "0.3.0"), _entry("0.3.0", None)]
    above = L.releases_above(rel, "0.3.0")
    assert [e["version"] for e in above] == ["0.4.0", "0.5.0"]  # apply order


def test_releases_above_excludes_local_and_below():
    rel = [_entry("0.3.0", "0.2.0"), _entry("0.2.0", "0.1.0"), _entry("0.1.0", None)]
    assert [e["version"] for e in L.releases_above(rel, "0.2.0")] == ["0.3.0"]


def test_releases_above_raises_on_missing_version():
    with pytest.raises(ValueError):
        L.releases_above([{"breaking": False}], "0.1.0")


def test_releases_above_raises_on_bad_version():
    with pytest.raises(ValueError):
        L.releases_above([_entry("not-semver", None)], "0.1.0")


# ===========================================================================
# 2. Pure-lib: classify_update (chain-walk)
# ===========================================================================
def test_classify_no_updates():
    rel = [_entry("0.2.0", "0.1.0"), _entry("0.1.0", None)]
    c = L.classify_update(rel, "0.2.0")
    assert c["has_updates"] is False and c["count"] == 0 and c["latest"] is None


def test_classify_non_breaking():
    rel = [_entry("0.3.0", "0.2.0"), _entry("0.2.0", None)]
    c = L.classify_update(rel, "0.2.0")
    assert c["has_updates"] and c["breaking"] is False and c["latest"] == "0.3.0"


def test_classify_chain_walk_any_breaking_is_breaking():
    """A breaking release in the MIDDLE makes the WHOLE update breaking — no
    leapfrogging a breaking change even to a non-breaking latest."""
    rel = [
        _entry("0.5.0", "0.4.0", breaking=False),
        _entry("0.4.0", "0.3.0", breaking=True, recipe="core/config/upgrade-recipes/v0.4.0.sh"),
        _entry("0.3.0", None),
    ]
    c = L.classify_update(rel, "0.3.0")
    assert c["breaking"] is True and c["count"] == 2 and c["latest"] == "0.5.0"


def test_classify_upgrade_recipes_only_breaking_in_apply_order():
    rel = [
        _entry("0.6.0", "0.5.0", breaking=True, recipe="r6"),
        _entry("0.5.0", "0.4.0", breaking=False),
        _entry("0.4.0", "0.3.0", breaking=True, recipe="r4"),
        _entry("0.3.0", None),
    ]
    c = L.classify_update(rel, "0.3.0")
    # apply order is oldest-first: r4 (0.4.0) before r6 (0.6.0); 0.5.0 has no recipe.
    assert c["upgrade_recipes"] == ["r4", "r6"]


# ===========================================================================
# 3. Pure-lib: upstream_role
# ===========================================================================
CHAIN = ["frontier", "seed", "downstream"]


@pytest.mark.parametrize("self_role,expected", [
    ("frontier", None), ("seed", "frontier"), ("downstream", "seed"), ("bogus", None),
])
def test_upstream_role(self_role, expected):
    assert L.upstream_role(CHAIN, self_role) == expected


# ===========================================================================
# 4. classify-chain CLI (env-driven; parse-or-fail exit 2)
# ===========================================================================
def test_cli_classify_no_updates(tmp_path):
    f = tmp_path / "u.json"
    f.write_text(_feed([_entry("0.2.0", "0.1.0"), _entry("0.1.0", None)]), encoding="utf-8")
    r = lib_classify(f, "0.2.0")
    assert r.returncode == 0, r.stderr
    assert "HAS_UPDATES=0" in r.stdout and "UPSTREAM_NEWEST=0.2.0" in r.stdout


def test_cli_classify_non_breaking(tmp_path):
    f = tmp_path / "u.json"
    f.write_text(_feed([_entry("0.3.0", "0.2.0"), _entry("0.2.0", None)]), encoding="utf-8")
    r = lib_classify(f, "0.2.0")
    assert r.returncode == 0, r.stderr
    assert "HAS_UPDATES=1" in r.stdout and "BREAKING=0" in r.stdout
    assert "COUNT=1" in r.stdout and "LATEST=0.3.0" in r.stdout and "VERSIONS=0.3.0" in r.stdout


def test_cli_classify_breaking(tmp_path):
    f = tmp_path / "u.json"
    f.write_text(_feed([
        _entry("1.0.0", "0.2.0", breaking=True, recipe="core/config/upgrade-recipes/v1.0.0.sh"),
        _entry("0.2.0", None),
    ]), encoding="utf-8")
    r = lib_classify(f, "0.2.0")
    assert r.returncode == 0, r.stderr
    assert "BREAKING=1" in r.stdout
    assert "UPGRADE_RECIPES=core/config/upgrade-recipes/v1.0.0.sh" in r.stdout


def test_cli_classify_empty_feed_exit2(tmp_path):
    f = tmp_path / "u.json"
    f.write_text("[]", encoding="utf-8")
    r = lib_classify(f, "0.2.0")
    assert r.returncode == 2  # fail-closed: cannot classify => potential breaking


def test_cli_classify_malformed_feed_exit2(tmp_path):
    f = tmp_path / "u.json"
    f.write_text("[ {bad json", encoding="utf-8")
    r = lib_classify(f, "0.2.0")
    assert r.returncode == 2


def test_cli_classify_missing_file_exit2(tmp_path):
    r = lib_classify(tmp_path / "nope.json", "0.2.0")
    assert r.returncode == 2


# ===========================================================================
# 5. check-upstream.sh — frontier short-circuit + arg handling (REAL overlay)
# ===========================================================================
def test_shell_frontier_short_circuit():
    """The real deployment overlay is self_role: frontier — no upstream, exit 0,
    and NO network fetch is attempted (the short-circuit precedes the curl)."""
    r = run_check()
    assert r.returncode == 0, r.stderr
    assert "frontier" in (r.stdout + r.stderr).lower()


def test_shell_diagnose_frontier():
    r = run_check("--diagnose")
    assert r.returncode == 0, r.stderr


def test_shell_unknown_arg_exit1():
    r = run_check("--bogus")
    assert r.returncode == 1


def test_shell_help_exit0():
    r = run_check("--help")
    assert r.returncode == 0


# ===========================================================================
# 6. check-upstream.sh — downstream exit-code mapping (MIND_WORLD + file:// feed)
# ===========================================================================
def _file_url(p: Path) -> str:
    # Path.as_uri() yields a curl-readable file:///C:/... (Windows) or file:///... (POSIX).
    return p.resolve().as_uri()


def test_shell_downstream_dead_url_exit2(tmp_path):
    """Fetch failure is FAIL-CLOSED (exit 2 — treat as potential breaking)."""
    _write_overlay(tmp_path, self_role="seed")
    r = run_check(world=tmp_path, url=DEAD_URL)
    assert r.returncode == 2
    assert "FAIL-CLOSED" in r.stderr


def test_shell_downstream_malformed_feed_exit2(tmp_path):
    _write_overlay(tmp_path, self_role="seed")
    feed = tmp_path / "u.json"; feed.write_text("[ {bad", encoding="utf-8")
    r = run_check(world=tmp_path, url=_file_url(feed))
    assert r.returncode == 2
    # Pin the PARSE-failure path (the feed WAS fetched then failed to parse) —
    # so a silent curl/file:// fetch failure can't make this pass vacuously.
    assert "malformed" in r.stderr.lower()


def test_shell_downstream_up_to_date_exit0(tmp_path):
    local = _local_version()
    _write_overlay(tmp_path, self_role="seed")
    feed = tmp_path / "u.json"
    feed.write_text(_feed([_entry(local, _below(local))]), encoding="utf-8")
    r = run_check(world=tmp_path, url=_file_url(feed))
    assert r.returncode == 0, r.stderr
    assert "up to date" in r.stdout


def test_shell_downstream_non_breaking_exit0(tmp_path):
    local = _local_version()
    newer = L.bump_version(local, "minor")
    _write_overlay(tmp_path, self_role="seed")
    feed = tmp_path / "u.json"
    feed.write_text(_feed([_entry(newer, local), _entry(local, None)]), encoding="utf-8")
    r = run_check(world=tmp_path, url=_file_url(feed))
    assert r.returncode == 0, r.stderr
    assert "non-breaking" in r.stdout


def test_shell_downstream_breaking_exit4(tmp_path):
    local = _local_version()
    major = L.bump_version(local, "major")
    _write_overlay(tmp_path, self_role="seed")
    feed = tmp_path / "u.json"
    feed.write_text(_feed([
        _entry(major, local, breaking=True, recipe="core/config/upgrade-recipes/v-x.sh"),
        _entry(local, None),
    ]), encoding="utf-8")
    r = run_check(world=tmp_path, url=_file_url(feed))
    assert r.returncode == 4
    assert "BREAKING" in r.stdout


def test_shell_invariant_violation_exit5(tmp_path):
    """CW2: local exceeds upstream latest => exit 5 (the downstream is somehow
    AHEAD of its upstream — a chain inversion that must be surfaced loudly)."""
    local = _local_version()
    below = _below(local)
    if below is None:
        pytest.skip("local version is 0.0.0 — no strictly-lower version to test with")
    _write_overlay(tmp_path, self_role="seed")
    feed = tmp_path / "u.json"
    feed.write_text(_feed([_entry(below, None)]), encoding="utf-8")
    r = run_check(world=tmp_path, url=_file_url(feed))
    assert r.returncode == 5
    assert "INVARIANT VIOLATION" in r.stderr


def test_shell_downstream_diagnose_exit0(tmp_path):
    """--diagnose on a DOWNSTREAM role exercises the real diagnose-report path
    (check-upstream.sh lines ~122-128) — not the frontier short-circuit that
    test_shell_diagnose_frontier hits. Reports state, exits 0, mutates nothing."""
    local = _local_version()
    newer = L.bump_version(local, "minor")
    _write_overlay(tmp_path, self_role="seed")
    feed = tmp_path / "u.json"
    feed.write_text(_feed([_entry(newer, local), _entry(local, None)]), encoding="utf-8")
    r = run_check("--diagnose", world=tmp_path, url=_file_url(feed))
    assert r.returncode == 0, r.stderr
    assert "DIAGNOSE" in r.stdout          # the real diagnose handler ran
    assert local in r.stdout               # local version reported in the diagnostic


# ===========================================================================
# 7. check-upstream.sh — config-error paths (exit 1)
# ===========================================================================
def test_shell_no_self_role_exit1(tmp_path):
    _write_overlay(tmp_path)  # empty overlay — no self_role
    r = run_check(world=tmp_path, url=DEAD_URL)
    assert r.returncode == 1


def test_shell_bad_role_not_in_chain_exit1(tmp_path):
    _write_overlay(tmp_path, self_role="nonsense-role")
    r = run_check(world=tmp_path, url=DEAD_URL)
    assert r.returncode == 1


def test_shell_no_url_configured_exit1(tmp_path):
    """self_role=seed but neither CHECK_UPSTREAM_URL nor sources.frontier.releases_url
    is set => no upstream feed to check => config error exit 1."""
    _write_overlay(tmp_path, self_role="seed")  # no sources block
    r = run_check(world=tmp_path)  # no url override
    assert r.returncode == 1
