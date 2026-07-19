"""test_release.py — Wave 1 cross-world versioning rails coverage.

Two layers, both daemon-safe (no daemon, no real S3, no real tag/commit):

  1. Unit tests against _release_lib.py (pure logic): semver, RELEASES.json
     load/chain/duplicate (M1 parse-or-fail), breaking/cross-world (Q3),
     recipe structural contract (H3), version SSOT (CW1).
  2. Black-box subprocess tests of release.sh — ONLY via --dry-run + bad-arg
     combos, which run full validation but write/commit/tag NOTHING. The real
     write path is NEVER invoked from tests (it would cut real tags in this
     repo). RELEASE_SEED_URL is pinned to an instant-fail URL so step 6 never
     waits on a real network fetch and the fail-closed behavior is deterministic.

The synthetic recipe fixture (rename + YAML-field-edit) runs entirely in a
tmp_path tree — it never touches the real world/+meta/.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
# Ad-hoc `py -3 test_release.py` needs these on sys.path (conftest does it under pytest).
for _p in (str(CORE_SCRIPTS), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _release_lib as L  # noqa: E402
from _bash_helpers import BASH  # noqa: E402

RELEASE_SH = CORE_SCRIPTS / "release.sh"
# Instant-fail seed URL: connection refused on a closed port → deterministic,
# fast fail-closed in step 6 (no 30s network wait, no dependence on a real feed).
DEAD_URL = "http://127.0.0.1:9/no-such-feed"


def run_release(*args, extra_env=None):
    """Invoke release.sh with a dead seed URL pinned. Returns CompletedProcess."""
    env = os.environ.copy()
    env["RELEASE_SEED_URL"] = DEAD_URL
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [BASH, str(RELEASE_SH), *args],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
    )


def _release_chain_divergent() -> bool:
    """True when this box's RELEASES.json newest entry != the on-disk
    __version__ — release.sh's chain-anchor validation hard-fails on that
    mismatch BEFORE any behavior the live-repo tests below assert, so every
    such test fails for box-state reasons on an unsynced satellite box
    (549-behind, merge deferred; g-115-1940). Fail-open: any read error
    returns False so the tests still run."""
    try:
        nv = L.newest_version(L.load_releases(str(PROJECT_ROOT / "RELEASES.json")))
        txt = (PROJECT_ROOT / "mind_api" / "src" / "__init__.py").read_text(encoding="utf-8")
        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)', txt, re.MULTILINE)
        return bool(nv and m and nv != m.group(1))
    except Exception:
        return False


# Applied to every test that execs release.sh (or its preflight sibling)
# against the LIVE repo — pure _release_lib unit tests and isolated-repo
# (run_release_in) tests are unaffected and still run everywhere.
live_release_chain_synced = pytest.mark.skipif(
    _release_chain_divergent(),
    reason="release chain anchor divergent on this box (RELEASES.json newest "
           "!= __version__) — live-repo release.sh preflight fails before the "
           "tested behavior; sync/merge the box to re-enable (g-115-1940)",
)


# ===========================================================================
# 1. Semver primitives
# ===========================================================================
def test_bump_major_zeros_minor_patch():
    assert L.bump_version("0.2.5", "major") == "1.0.0"


def test_bump_minor_zeros_patch():
    assert L.bump_version("0.2.5", "minor") == "0.3.0"


def test_bump_patch():
    assert L.bump_version("0.2.5", "patch") == "0.2.6"


@pytest.mark.parametrize("bad", ["v1.2.3", "1.2", "1.2.3-rc1", "1.2.3.4", "x.y.z", ""])
def test_parse_version_rejects_bad(bad):
    with pytest.raises(ValueError):
        L.parse_version(bad)


def test_bump_unknown_kind_raises():
    with pytest.raises(ValueError):
        L.bump_version("1.0.0", "nope")


@pytest.mark.parametrize("a,b,expected", [
    ("0.3.0", "0.2.0", 1), ("0.2.0", "0.3.0", -1), ("1.0.0", "1.0.0", 0),
    ("1.0.0", "0.9.9", 1), ("0.2.10", "0.2.9", 1),  # numeric, not lexical
])
def test_compare_version(a, b, expected):
    assert L.compare_version(a, b) == expected


# ===========================================================================
# 2. RELEASES.json load / chain / duplicate (M1 parse-or-fail)
# ===========================================================================
def test_load_missing_returns_empty(tmp_path):
    assert L.load_releases(str(tmp_path / "nope.json")) == []


def test_load_empty_file_returns_empty(tmp_path):
    p = tmp_path / "RELEASES.json"
    p.write_text("   \n", encoding="utf-8")
    assert L.load_releases(str(p)) == []


def test_load_malformed_raises_not_silent(tmp_path):
    """M1: a present-but-malformed file must raise, never degrade to []."""
    p = tmp_path / "RELEASES.json"
    p.write_text("[ {bad json ", encoding="utf-8")
    with pytest.raises(ValueError):
        L.load_releases(str(p))


def test_load_non_array_raises(tmp_path):
    p = tmp_path / "RELEASES.json"
    p.write_text('{"version": "1.0.0"}', encoding="utf-8")
    with pytest.raises(ValueError):
        L.load_releases(str(p))


def test_validate_chain_ok():
    rel = [
        {"version": "0.3.0", "previous_version": "0.2.0"},
        {"version": "0.2.0", "previous_version": "0.1.0"},
        {"version": "0.1.0", "previous_version": None},
    ]
    ok, errs = L.validate_chain(rel)
    assert ok, errs


def test_validate_chain_detects_break():
    rel = [
        {"version": "0.3.0", "previous_version": "0.1.0"},  # should be 0.2.0
        {"version": "0.2.0", "previous_version": "0.1.0"},
    ]
    ok, errs = L.validate_chain(rel)
    assert not ok and any("chain break" in e for e in errs)


def test_validate_chain_detects_non_descending():
    rel = [
        {"version": "0.2.0", "previous_version": "0.3.0"},
        {"version": "0.3.0", "previous_version": None},
    ]
    ok, errs = L.validate_chain(rel)
    assert not ok


def test_check_duplicate():
    rel = [{"version": "0.2.0"}, {"version": "0.1.0"}]
    assert L.check_duplicate(rel, "0.2.0")
    assert not L.check_duplicate(rel, "0.3.0")


def test_newest_version():
    assert L.newest_version([{"version": "0.9.0"}, {"version": "0.1.0"}]) == "0.9.0"
    assert L.newest_version([]) is None


# ===========================================================================
# 3. Breaking / cross-world (Q3 fail-closed + audited override)
# ===========================================================================
def test_major_is_always_breaking():
    b, cw, errs = L.compute_breaking_cross_world("major", False, False)
    assert b is True and not errs


def test_cross_world_fail_closed_to_breaking():
    """Q3: cross_world non-major with NO override -> breaking=True (fail-closed)."""
    b, cw, errs = L.compute_breaking_cross_world("minor", True, False)
    assert b is True and cw is True and not errs


def test_cross_world_override_allows_non_breaking():
    """Q3: explicit audited override -> breaking stays False."""
    b, cw, errs = L.compute_breaking_cross_world("minor", True, True)
    assert b is False and cw is True and not errs


def test_minor_non_cross_world_not_breaking():
    b, cw, errs = L.compute_breaking_cross_world("patch", False, False)
    assert b is False and cw is False and not errs


def test_major_with_override_is_error():
    b, cw, errs = L.compute_breaking_cross_world("major", False, True)
    assert errs  # override is meaningless/invalid for a major bump


# ===========================================================================
# 4. Recipe structural contract (H3) — gate coverage (M4) at lib level
# ===========================================================================
TEMPLATE = CORE_SCRIPTS.parent / "config" / "upgrade-recipes" / "_template.sh"
TEMPLATE_RB = CORE_SCRIPTS.parent / "config" / "upgrade-recipes" / "_template-rollback.sh"


def test_rollback_path_convention():
    assert L.rollback_path_for("core/config/upgrade-recipes/v0.3.0.sh").endswith("v0.3.0-rollback.sh")


def test_rollback_path_is_posix_separators():
    # RELEASES.json is the cross-world artifact: the recorded rollback path MUST
    # use forward slashes on every platform, or validate_recipe_structure on a
    # POSIX seed/downstream reads a Windows-native backslash path as a single
    # literal filename and fails "rollback file not found". Regression guard for
    # the str(WindowsPath) -> backslash bug found cutting v1.0.0 (the first real
    # cut). The same dir must be preserved, just with portable separators.
    out = L.rollback_path_for("core/config/upgrade-recipes/v1.0.0-n3-mind-prefix.sh")
    assert "\\" not in out, f"rollback path must not contain backslashes: {out!r}"
    assert out == "core/config/upgrade-recipes/v1.0.0-n3-mind-prefix-rollback.sh"


def test_template_recipe_passes_contract():
    ok, missing = L.validate_recipe_structure(str(TEMPLATE), str(TEMPLATE_RB), cross_world=True)
    assert ok, missing


def test_recipe_missing_precheck_refused(tmp_path):
    r = tmp_path / "v.sh"; r.write_text("#!/bin/bash\n# post-check only\n", encoding="utf-8")
    rb = tmp_path / "v-rollback.sh"
    rb.write_text("#!/bin/bash\n# idempotent\n# pre-check\n# post-check\n", encoding="utf-8")
    ok, missing = L.validate_recipe_structure(str(r), str(rb), cross_world=False)
    assert not ok and any("pre-check" in m for m in missing)


def test_cross_world_recipe_missing_snapshot_refused(tmp_path):
    """A cross_world recipe with no world/meta snapshot is refused (H3)."""
    r = tmp_path / "v.sh"
    # No world/meta backup step at all (the word "snapshot" must not appear,
    # else the marker regex would match the comment).
    r.write_text("#!/bin/bash\n# pre-check\n# post-check\n# (migrates in place)\n", encoding="utf-8")
    rb = tmp_path / "v-rollback.sh"
    rb.write_text("#!/bin/bash\n# idempotent\n# pre-check\n# post-check\n", encoding="utf-8")
    ok, missing = L.validate_recipe_structure(str(r), str(rb), cross_world=True)
    assert not ok and any("snapshot" in m for m in missing)


def test_rollback_missing_idempotent_refused(tmp_path):
    r = tmp_path / "v.sh"
    r.write_text("#!/bin/bash\n# pre-check\n# post-check\n", encoding="utf-8")
    rb = tmp_path / "v-rollback.sh"
    rb.write_text("#!/bin/bash\n# pre-check\n# post-check\n", encoding="utf-8")  # no idempotent
    ok, missing = L.validate_recipe_structure(str(r), str(rb), cross_world=False)
    assert not ok and any("idempotent" in m for m in missing)


def test_cross_world_comment_only_markers_refused(tmp_path):
    """Hardening: cross_world markers present ONLY in comments must NOT satisfy
    the H3 snapshot contract — a comment claiming a snapshot is not a snapshot."""
    r = tmp_path / "v.sh"
    r.write_text(
        "#!/bin/bash\n# pre-check\n# post-check\n"
        "# snapshot WORLD_PATH META_PATH (all only in this comment)\necho real_work\n",
        encoding="utf-8",
    )
    rb = tmp_path / "v-rollback.sh"
    rb.write_text("#!/bin/bash\n# idempotent\n# pre-check\n# post-check\n", encoding="utf-8")
    ok, missing = L.validate_recipe_structure(str(r), str(rb), cross_world=True)
    assert not ok
    assert any("snapshot" in m for m in missing)


# omit -> the marker-key fragment that must appear in the missing-marker message.
@pytest.mark.parametrize("omit,key_frag", [
    ("snapshot", "snapshot"), ("WORLD_PATH", "world-path"), ("META_PATH", "meta-path"),
])
def test_cross_world_missing_individual_marker(tmp_path, omit, key_frag):
    """Each cross_world marker is independently required (in executable code)."""
    code = {"snapshot": 'echo snapshot-now', "WORLD_PATH": 'cp "$WORLD_PATH" x', "META_PATH": 'cp "$META_PATH" x'}
    lines = ["#!/bin/bash", "# pre-check", "# post-check"]
    lines += [v for k, v in code.items() if k != omit]
    r = tmp_path / "v.sh"; r.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rb = tmp_path / "v-rollback.sh"
    rb.write_text("#!/bin/bash\n# idempotent\n# pre-check\n# post-check\n", encoding="utf-8")
    ok, missing = L.validate_recipe_structure(str(r), str(rb), cross_world=True)
    assert not ok and any(key_frag in m for m in missing)


# A recipe that fully satisfies the cross_world UPGRADE snapshot contract, so the
# only thing under test in the rollback-restore tests below is the ROLLBACK side.
_GOOD_CW_RECIPE = (
    '#!/bin/bash\n# pre-check\n# post-check\n'
    'echo snapshot-now\ncp "$WORLD_PATH" x\ncp "$META_PATH" x\n'
)


def test_cross_world_rollback_missing_restore_refused(tmp_path):
    """H3b (omni#1): a cross_world UPGRADE that snapshots world/+meta/ but whose
    ROLLBACK cannot restore from that snapshot is refused — otherwise the
    snapshot is a dead artifact and a corrupting migration is irreversible."""
    r = tmp_path / "v.sh"; r.write_text(_GOOD_CW_RECIPE, encoding="utf-8")
    # Rollback has the framework markers but NO restore-from-snapshot code.
    rb = tmp_path / "v-rollback.sh"
    rb.write_text("#!/bin/bash\n# idempotent\n# pre-check\n# post-check\nmv new old\n", encoding="utf-8")
    ok, missing = L.validate_recipe_structure(str(r), str(rb), cross_world=True)
    assert not ok
    assert any("rollback missing restore marker" in m for m in missing)
    # All three restore markers must be independently flagged.
    assert any("snapshot-restore" in m for m in missing)
    assert any("world-path-restore" in m for m in missing)
    assert any("meta-path-restore" in m for m in missing)


def test_cross_world_rollback_comment_only_restore_refused(tmp_path):
    """A rollback whose restore markers live ONLY in a comment does NOT satisfy
    H3b — same comment-stripping discipline as the upgrade snapshot check."""
    r = tmp_path / "v.sh"; r.write_text(_GOOD_CW_RECIPE, encoding="utf-8")
    rb = tmp_path / "v-rollback.sh"
    rb.write_text(
        "#!/bin/bash\n# idempotent\n# pre-check\n# post-check\n"
        "# restore from SNAP_DIR to WORLD_PATH and META_PATH (comment only)\necho real\n",
        encoding="utf-8",
    )
    ok, missing = L.validate_recipe_structure(str(r), str(rb), cross_world=True)
    assert not ok
    assert any("rollback missing restore marker" in m for m in missing)


def test_cross_world_rollback_with_restore_passes(tmp_path):
    """Positive: a rollback that restores world/+meta/ from the snapshot in
    executable code satisfies the full cross_world contract."""
    r = tmp_path / "v.sh"; r.write_text(_GOOD_CW_RECIPE, encoding="utf-8")
    rb = tmp_path / "v-rollback.sh"
    rb.write_text(
        "#!/bin/bash\n# idempotent\n# pre-check\n# post-check\n"
        'cp -r "$SNAP_DIR/world/." "$WORLD_PATH/"\ncp -r "$SNAP_DIR/meta/." "$META_PATH/"\n',
        encoding="utf-8",
    )
    ok, missing = L.validate_recipe_structure(str(r), str(rb), cross_world=True)
    assert ok, missing


def test_framework_only_rollback_needs_no_restore(tmp_path):
    """A framework-only (cross_world:false) rollback is NOT required to restore
    world/+meta/ — the restore markers are only enforced for cross_world."""
    r = tmp_path / "v.sh"
    r.write_text("#!/bin/bash\n# pre-check\n# post-check\n", encoding="utf-8")
    rb = tmp_path / "v-rollback.sh"
    rb.write_text("#!/bin/bash\n# idempotent\n# pre-check\n# post-check\nmv new old\n", encoding="utf-8")
    ok, missing = L.validate_recipe_structure(str(r), str(rb), cross_world=False)
    assert ok, missing


# ===========================================================================
# 5. CW1 version SSOT
# ===========================================================================
def test_real_repo_passes_ssot():
    """Post-Wave-0, the real repo must have a single version SSOT."""
    ok, viol = L.check_version_ssot(str(PROJECT_ROOT))
    assert ok, viol


def test_ssot_detects_stray_pyversion(tmp_path):
    (tmp_path / "mind_api" / "src").mkdir(parents=True)
    (tmp_path / "mind_api" / "src" / "__init__.py").write_text('__version__ = "0.2.0"\n', encoding="utf-8")
    (tmp_path / "mind_api" / "other.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    ok, viol = L.check_version_ssot(str(tmp_path))
    assert not ok and any("stray __version__" in v for v in viol)


def test_ssot_detects_profile_semver(tmp_path):
    cfg = tmp_path / "core" / "config"; cfg.mkdir(parents=True)
    (cfg / "profile.yaml").write_text('system:\n  version: "0.1.0"\n', encoding="utf-8")
    ok, viol = L.check_version_ssot(str(tmp_path))
    assert not ok and any("competing semver" in v for v in viol)


# ===========================================================================
# 6. build_entry / serialize
# ===========================================================================
def test_build_entry_schema():
    e = L.build_entry("0.3.0", "0.2.0", "2026-06-04", True, True, "x",
                      "core/config/upgrade-recipes/v0.3.0.sh",
                      "core/config/upgrade-recipes/v0.3.0-rollback.sh", "0.2.0")
    assert set(e) == {"version", "previous_version", "date", "breaking",
                      "cross_world", "summary", "upgrade_recipe",
                      "rollback_recipe", "min_source"}
    assert e["breaking"] is True


def test_serialize_round_trips():
    rel = [L.build_entry("0.3.0", "0.2.0", "2026-06-04", False, False, "x", None, None, None)]
    txt = L.serialize_releases(rel)
    assert json.loads(txt) == rel and txt.endswith("\n")


def test_build_prepended_via_cli(tmp_path):
    """CLI build-prepended prepends newest-first and parses."""
    p = tmp_path / "RELEASES.json"
    p.write_text(json.dumps([L.build_entry("0.2.0", "0.1.0", "2026-06-01", False, False, "base", None, None, None)]), encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "RELEASES_PATH": str(p), "NEW_VERSION": "0.3.0", "CURRENT_VERSION": "0.2.0",
        "DATE": "2026-06-04", "BREAKING": "0", "CROSS_WORLD": "0", "SUMMARY": "feat",
        "UPGRADE_RECIPE": "", "ROLLBACK_RECIPE": "", "MIN_SOURCE": "",
    })
    r = subprocess.run([sys.executable, str(CORE_SCRIPTS / "_release_lib.py"), "build-prepended"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    arr = json.loads(r.stdout)
    assert arr[0]["version"] == "0.3.0" and arr[1]["version"] == "0.2.0"


# ===========================================================================
# 7. Black-box release.sh — dry-run + bad-arg gate coverage (M4, no writes)
# ===========================================================================
@live_release_chain_synced
def test_dry_run_force_release_ok():
    r = run_release("patch", "--summary", "t", "--force-release", "seed not bootstrapped", "--dry-run")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "dry-run] OK" in r.stdout


def test_major_without_recipe_refused():
    r = run_release("major", "--summary", "t", "--force-release", "x", "--dry-run")
    assert r.returncode == 1
    assert "requires --recipe" in (r.stdout + r.stderr)


def test_non_breaking_with_recipe_refused():
    r = run_release("patch", "--summary", "t", "--recipe", "core/config/upgrade-recipes/_template.sh",
                    "--force-release", "x", "--dry-run")
    assert r.returncode == 1
    assert "must NOT provide --recipe" in (r.stdout + r.stderr)


def test_cross_world_without_override_refused():
    """Q3 fail-closed: cross_world minor with no override -> breaking -> needs recipe -> refuse."""
    r = run_release("minor", "--cross-world", "--summary", "t", "--force-release", "x", "--dry-run")
    assert r.returncode == 1
    assert "requires --recipe" in (r.stdout + r.stderr)


@live_release_chain_synced
def test_cross_world_with_override_ok():
    r = run_release("minor", "--cross-world", "--allow-non-breaking-cross-world",
                    "optional file, back-compat", "--summary", "t", "--force-release", "x", "--dry-run")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "breaking=0 cross_world=1" in r.stdout
    assert "AUDIT override allow-non-breaking-cross-world" in r.stderr


@live_release_chain_synced
def test_invariant_fail_closed_without_force():
    """H1: seed feed unreachable + no --force-release -> hard refuse (exit 1)."""
    r = run_release("patch", "--summary", "t", "--dry-run")
    assert r.returncode == 1
    assert "frontier-invariant" in (r.stdout + r.stderr)


def test_force_release_requires_reason():
    # --force-release with no trailing token -> require_value rejects empty (exit 2).
    r = run_release("patch", "--force-release")
    assert r.returncode == 2
    assert "requires a value" in (r.stdout + r.stderr)


def test_allow_nb_cw_requires_reason():
    r = run_release("minor", "--cross-world", "--allow-non-breaking-cross-world")
    assert r.returncode == 2
    assert "requires a value" in (r.stdout + r.stderr)


def test_flag_swallowing_refused():
    """HIGH regression: `--summary --dry-run` must be REFUSED, not swallow
    --dry-run as the summary value (which would leave DRY=0 and cut a REAL,
    irreversible release when the user asked for a dry run)."""
    r = run_release("patch", "--summary", "--dry-run")
    assert r.returncode == 2
    assert "requires a value" in (r.stdout + r.stderr)


def test_summary_required_for_real_cut():
    """A real cut without --summary is refused at validation (exit 2) — it never
    reaches the write path."""
    r = run_release("patch", "--force-release", "x")  # no --summary, no --dry-run
    assert r.returncode == 2
    assert "summary" in (r.stdout + r.stderr).lower()


# --- seed-latest CLI: M1 parse-or-fail at the subcommand release.sh actually calls
def test_seed_latest_cli_malformed_exits_nonzero(tmp_path):
    p = tmp_path / "RELEASES.json"; p.write_text("[ {bad json ", encoding="utf-8")
    r = subprocess.run([sys.executable, str(CORE_SCRIPTS / "_release_lib.py"), "seed-latest", str(p)],
                       capture_output=True, text=True)
    assert r.returncode != 0


def test_seed_latest_cli_empty_array_exits_nonzero(tmp_path):
    p = tmp_path / "RELEASES.json"; p.write_text("[]", encoding="utf-8")
    r = subprocess.run([sys.executable, str(CORE_SCRIPTS / "_release_lib.py"), "seed-latest", str(p)],
                       capture_output=True, text=True)
    assert r.returncode != 0


def test_seed_latest_cli_valid(tmp_path):
    p = tmp_path / "RELEASES.json"; p.write_text(json.dumps([{"version": "0.9.0"}]), encoding="utf-8")
    r = subprocess.run([sys.executable, str(CORE_SCRIPTS / "_release_lib.py"), "seed-latest", str(p)],
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "0.9.0"


@live_release_chain_synced
def test_invariant_malformed_feed_fail_closed(tmp_path):
    """H1 fail-closed path 2: feed fetchable but malformed -> hard refuse w/o force."""
    bad = tmp_path / "bad.json"; bad.write_text("[ {bad json ", encoding="utf-8")
    r = run_release("patch", "--summary", "t", "--dry-run", extra_env={"RELEASE_SEED_URL": bad.as_uri()})
    assert r.returncode == 1
    assert "frontier-invariant" in (r.stdout + r.stderr)


@live_release_chain_synced
def test_invariant_malformed_feed_with_force_ok(tmp_path):
    bad = tmp_path / "bad.json"; bad.write_text("[ {bad json ", encoding="utf-8")
    r = run_release("patch", "--summary", "t", "--force-release", "override", "--dry-run",
                    extra_env={"RELEASE_SEED_URL": bad.as_uri()})
    assert r.returncode == 0, r.stderr + r.stdout
    assert "AUDIT force-release" in r.stderr


@live_release_chain_synced
def test_check_releases_current_passes_on_real_repo():
    """Seed-preflight check #7 against the real repo (RELEASES.json newest == __version__)."""
    r = subprocess.run([BASH, str(CORE_SCRIPTS / "check-releases-current.sh")],
                       capture_output=True, text=True, cwd=str(PROJECT_ROOT))
    assert r.returncode == 0, r.stderr + r.stdout
    assert "PASS" in r.stdout


def test_unknown_arg_usage_error():
    r = run_release("patch", "--bogus-flag")
    assert r.returncode == 2


def test_missing_bump_kind_usage_error():
    r = run_release("--summary", "t", "--dry-run")
    assert r.returncode == 2


def test_dry_run_writes_nothing():
    """A dry-run must not modify __version__ or RELEASES.json on disk."""
    init_py = PROJECT_ROOT / "mind_api" / "src" / "__init__.py"
    releases = PROJECT_ROOT / "RELEASES.json"
    before_init = init_py.read_text(encoding="utf-8")
    before_rel = releases.read_text(encoding="utf-8")
    run_release("minor", "--summary", "t", "--force-release", "x", "--dry-run")
    assert init_py.read_text(encoding="utf-8") == before_init
    assert releases.read_text(encoding="utf-8") == before_rel


# ===========================================================================
# 8. Synthetic recipe fixture — rename + YAML-field-edit, end-to-end, in tmp
# ===========================================================================
SYNTH_RECIPE = """#!/usr/bin/env bash
# Synthetic upgrade recipe (test fixture): file-rename + YAML-field-edit + H3 snapshot.
set -euo pipefail
TARGET="$1"
# In a real recipe these come from _paths.sh; the fixture points them at tmp dirs
# so the snapshot markers appear in EXECUTABLE code (not just a comment).
WORLD_PATH="$TARGET/world"
META_PATH="$TARGET/meta"
# --- Pre-check ---
[[ -f "$TARGET/field.yaml" ]] || { echo "ERROR: no target" >&2; exit 1; }
# --- World/meta snapshot (cross_world H3) — fail-hard, executable ---
SNAP="$TARGET/.snap"; mkdir -p "$SNAP"
cp -r "$WORLD_PATH" "$SNAP/world" || { echo "ERROR: world snapshot failed" >&2; exit 1; }
cp -r "$META_PATH"  "$SNAP/meta"  || { echo "ERROR: meta snapshot failed" >&2; exit 1; }
echo "snapshot: $SNAP"
# --- Steps ---
[[ -f "$TARGET/old-name.md" ]] && mv "$TARGET/old-name.md" "$TARGET/new-name.md"
sed -i 's/^old_field:/new_field:/' "$TARGET/field.yaml"
# --- Post-check ---
[[ -f "$TARGET/new-name.md" ]] || { echo "ERROR: rename failed" >&2; exit 1; }
echo "upgrade done"
"""

SYNTH_ROLLBACK = """#!/usr/bin/env bash
# Synthetic rollback (test fixture): idempotent reverse of the upgrade, incl.
# the cross_world H3b restore of world/+meta/ from the upgrade snapshot.
set -euo pipefail
TARGET="$1"
WORLD_PATH="$TARGET/world"
META_PATH="$TARGET/meta"
SNAP="$TARGET/.snap"
# --- Pre-check (idempotent) ---
if [[ -f "$TARGET/old-name.md" ]]; then echo "already rolled back (idempotent)"; exit 0; fi
# --- Steps (reverse) ---
[[ -f "$TARGET/new-name.md" ]] && mv "$TARGET/new-name.md" "$TARGET/old-name.md"
sed -i 's/^new_field:/old_field:/' "$TARGET/field.yaml"
# --- Restore world/+meta/ from snapshot (cross_world H3b) ---
if [[ -d "$SNAP/world" && -d "$SNAP/meta" ]]; then
  cp -r "$SNAP/world/." "$WORLD_PATH/" || { echo "ERROR: world restore failed" >&2; exit 1; }
  cp -r "$SNAP/meta/."  "$META_PATH/"  || { echo "ERROR: meta restore failed" >&2; exit 1; }
fi
# --- Post-check ---
echo "rollback done"
"""


def _bash(script: Path, target: Path):
    return subprocess.run([BASH, str(script), str(target)], capture_output=True, text=True)


def test_synthetic_recipe_rename_and_yaml_edit(tmp_path):
    target = tmp_path / "tree"; target.mkdir()
    (target / "old-name.md").write_text("content\n", encoding="utf-8")
    (target / "field.yaml").write_text("old_field: hello\nother: keep\n", encoding="utf-8")
    # world/ + meta/ stand-ins the recipe's H3 snapshot will capture.
    (target / "world").mkdir(); (target / "world" / "node.md").write_text("w\n", encoding="utf-8")
    (target / "meta").mkdir(); (target / "meta" / "strat.yaml").write_text("m: 1\n", encoding="utf-8")
    recipe = tmp_path / "vX.sh"; recipe.write_text(SYNTH_RECIPE, encoding="utf-8")
    rollback = tmp_path / "vX-rollback.sh"; rollback.write_text(SYNTH_ROLLBACK, encoding="utf-8")

    # The synthetic recipe satisfies the structural contract (cross_world) — with
    # WORLD_PATH/META_PATH/snapshot all in executable code, not comments.
    ok, missing = L.validate_recipe_structure(str(recipe), str(rollback), cross_world=True)
    assert ok, missing

    # Forward migration: rename + yaml edit + real world/meta snapshot.
    r = _bash(recipe, target)
    assert r.returncode == 0, r.stderr
    assert (target / "new-name.md").exists()
    assert not (target / "old-name.md").exists()
    assert (target / ".snap" / "world").is_dir()
    assert (target / ".snap" / "meta").is_dir()
    yaml_txt = (target / "field.yaml").read_text(encoding="utf-8")
    assert "new_field: hello" in yaml_txt and "other: keep" in yaml_txt

    # Simulate the migration having mutated world/ — the snapshot is the safety
    # net a git-tag rollback cannot provide (H3b). The rollback must restore it.
    (target / "world" / "node.md").write_text("CORRUPTED\n", encoding="utf-8")

    # Rollback restores the original state (tracked files + world/ from snapshot).
    r = _bash(rollback, target)
    assert r.returncode == 0, r.stderr
    assert (target / "old-name.md").exists()
    assert not (target / "new-name.md").exists()
    assert "old_field: hello" in (target / "field.yaml").read_text(encoding="utf-8")
    # H3b: world/ restored from the upgrade snapshot, not left CORRUPTED.
    assert (target / "world" / "node.md").read_text(encoding="utf-8") == "w\n"

    # Idempotent: a second rollback is a no-op success.
    r = _bash(rollback, target)
    assert r.returncode == 0
    assert "idempotent" in r.stdout
    assert (target / "old-name.md").exists()


# ===========================================================================
# 9. Force-release audit ledger (omni#5) — REAL write path in an ISOLATED repo
# ===========================================================================
# Sections 7-8 only ever --dry-run (they never touch git state), but the ledger
# is written by release.sh Step 9.5 ONLY after a successful commit + tag. To
# exercise that path without cutting real tags in THIS repo, each test builds a
# throwaway git repo in tmp_path holding just the three files release.sh needs:
# itself, _paths.sh, _release_lib.py. _paths.sh anchors PROJECT_ROOT to
# <repo>/core/scripts/../.. (its own BASH_SOURCE, NOT cwd or any env override),
# so the copy operates entirely on the tmp repo — real commits/tags there are
# harmless. The ledger target redirects off-repo via MIND_META. This harness is
# shared with #58 (release.sh atomicity) once it lands.

# Resolve git: PATH first, else derive from the Git-for-Windows bash location.
_GIT = shutil.which("git")
if not _GIT:
    _bp = Path(BASH)
    for _c in (_bp.parents[2] / "cmd" / "git.exe", _bp.parents[1] / "git.exe",
               _bp.parents[2] / "bin" / "git.exe"):
        if _c.exists():
            _GIT = str(_c)
            break
_GIT = _GIT or "git"
try:
    _git_ok = subprocess.run([_GIT, "--version"], capture_output=True, text=True).returncode == 0
except Exception:
    _git_ok = False

requires_git = pytest.mark.skipif(
    not _git_ok, reason="git unavailable for isolated-repo write-path tests")


def _git(repo, *args):
    r = subprocess.run([_GIT, "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r


def _setup_release_repo(tmp_path, version="0.2.0"):
    """Throwaway git repo on `main` carrying the minimal tree release.sh's write
    path needs. Returns the repo Path. Real commits/tags here never touch the
    real repo (PROJECT_ROOT is anchored to this copy's core/scripts/../..)."""
    repo = tmp_path / "repo"
    (repo / "core" / "scripts").mkdir(parents=True)
    (repo / "mind_api" / "src").mkdir(parents=True)
    for name in ("release.sh", "_paths.sh", "_release_lib.py"):
        shutil.copy(CORE_SCRIPTS / name, repo / "core" / "scripts" / name)
    (repo / "mind_api" / "src" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8")
    # Internally consistent 2-entry chain ending in previous_version=None, with
    # the newest version == __version__ (mirrors the real repo's invariants).
    releases = [
        L.build_entry(version, "0.1.0", "2026-06-04", False, False, "base", None, None, None),
        L.build_entry("0.1.0", None, "2026-06-01", False, False, "genesis", None, None, None),
    ]
    (repo / "RELEASES.json").write_text(json.dumps(releases, indent=2) + "\n", encoding="utf-8")
    # Keep the py-shim + bytecode cache from dirtying the tree: release.sh Step 1
    # hard-fails on a dirty tree, and sourcing _paths.sh may create
    # core/scripts/.python-shim/ on a machine whose `python3` is a Store stub.
    (repo / ".gitignore").write_text(
        "core/scripts/.python-shim/\ncore/.pycache/\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "release-test@example.com")
    _git(repo, "config", "user.name", "Release Test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "tag.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "branch", "-M", "main")  # release.sh Step 1 requires branch == main
    return repo


def run_release_in(repo, tmp_path, *args, extra_env=None):
    """Run the COPIED release.sh in the isolated repo. Ledger -> tmp_path/meta
    (off-repo, via MIND_META). Seed URL pinned to the dead URL unless
    extra_env overrides it. Strips session-leaked path/agent env so resolution
    is deterministic against the tmp repo."""
    env = os.environ.copy()
    for k in ("MIND_AGENT", "MIND_WORLD", "MIND_SID", "WORLD_PATH",
              "META_PATH", "MIND_GIT_AVAILABLE"):
        env.pop(k, None)
    env["RELEASE_SEED_URL"] = DEAD_URL
    env["MIND_META"] = str(tmp_path / "meta")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [BASH, str(repo / "core" / "scripts" / "release.sh"), *args],
        capture_output=True, text=True, env=env, cwd=str(repo),
    )


@requires_git
def test_force_release_ledger_written(tmp_path):
    """A successful --force-release cut (invariant UNVERIFIABLE: dead seed URL)
    appends exactly one JSONL record carrying the override metadata."""
    repo = _setup_release_repo(tmp_path)
    r = run_release_in(repo, tmp_path, "patch", "--summary", "fix the thing",
                       "--force-release", "seed not bootstrapped yet")
    assert r.returncode == 0, r.stderr + r.stdout
    ledger = tmp_path / "meta" / "force-release-ledger.jsonl"
    assert ledger.exists(), f"ledger missing.\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    lines = ledger.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1, lines
    rec = json.loads(lines[0])
    assert rec["type"] == "force-release"
    assert rec["version"] == "0.2.1"
    assert rec["previous_version"] == "0.2.0"
    assert rec["reason"] == "seed not bootstrapped yet"
    assert rec["invariant_state"] == "unverifiable"
    assert rec["seed_latest"] is None
    assert "unreachable" in (rec["invariant_detail"] or "")
    assert rec["breaking"] is False
    assert rec["cross_world"] is False
    assert rec["summary"] == "fix the thing"
    # The real annotated tag was created in the throwaway repo.
    assert "v0.2.1" in _git(repo, "tag", "-l").stdout


@requires_git
def test_normal_release_no_ledger_entry(tmp_path):
    """A normal (non-force) cut with a reachable, in-bounds seed feed must NOT
    write the force-release ledger."""
    repo = _setup_release_repo(tmp_path)
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([{"version": "0.2.0"}]), encoding="utf-8")
    r = run_release_in(repo, tmp_path, "patch", "--summary", "ordinary fix",
                       extra_env={"RELEASE_SEED_URL": seed.as_uri()})
    assert r.returncode == 0, r.stderr + r.stdout
    assert "frontier-invariant OK" in r.stdout
    assert not (tmp_path / "meta" / "force-release-ledger.jsonl").exists(), \
        "a normal release must not write the force-release ledger"
    assert "v0.2.1" in _git(repo, "tag", "-l").stdout


@requires_git
def test_force_release_ledger_invariant_violated(tmp_path):
    """When the seed is AHEAD of the new version, --force-release records
    invariant_state=violated and the exact seed_latest it cut behind."""
    repo = _setup_release_repo(tmp_path)
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([{"version": "9.9.9"}]), encoding="utf-8")
    r = run_release_in(repo, tmp_path, "patch", "--summary", "cut behind seed",
                       "--force-release", "intentional frontier override",
                       extra_env={"RELEASE_SEED_URL": seed.as_uri()})
    assert r.returncode == 0, r.stderr + r.stdout
    ledger = tmp_path / "meta" / "force-release-ledger.jsonl"
    assert ledger.exists(), f"ledger missing.\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    rec = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert rec["invariant_state"] == "violated"
    assert rec["seed_latest"] == "9.9.9"
    assert rec["version"] == "0.2.1"
    assert rec["reason"] == "intentional frontier override"


# ===========================================================================
# 10. Pre-push tag gate (M2 enforcement, omni#3)
# ===========================================================================
# Two layers: the pure-logic kernel (check-tag-in-releases.py) via sys.executable
# with a RELEASES_PATH-env fixture, and the fail-closed pre-push hook itself. The
# hook derives RELEASES_PATH from `git rev-parse --show-toplevel` (NO env override
# — no production bypass surface), so the hook tests run it inside an isolated tmp
# repo with the hook + check script + _release_lib copied in, exercising the real
# root-derived path. stdin is fed the git pre-push line format directly.
CHECK_TAG = CORE_SCRIPTS / "check-tag-in-releases.py"
GITHOOKS = CORE_SCRIPTS.parent / "githooks"
PREPUSH_HOOK = GITHOOKS / "pre-push"
_ZERO_OID = "0" * 40   # tag-deletion marker in git pre-push stdin


def _run_check_tag(version, releases_path):
    env = os.environ.copy()
    env["RELEASES_PATH"] = str(releases_path)
    return subprocess.run([sys.executable, str(CHECK_TAG), version],
                          capture_output=True, text=True, env=env)


def _setup_prepush_repo(tmp_path, releases):
    """Tmp git repo carrying the hook + check kernel + _release_lib + a fixture
    RELEASES.json, so the hook's `git rev-parse --show-toplevel`-derived
    RELEASES_PATH points at the fixture. `releases` is a list (serialized) or a
    raw string (written verbatim, for malformed/empty fixtures)."""
    repo = tmp_path / "pp"
    (repo / "core" / "scripts").mkdir(parents=True)
    (repo / "core" / "githooks").mkdir(parents=True)
    shutil.copy(CHECK_TAG, repo / "core" / "scripts" / "check-tag-in-releases.py")
    shutil.copy(CORE_SCRIPTS / "_release_lib.py", repo / "core" / "scripts" / "_release_lib.py")
    shutil.copy(PREPUSH_HOOK, repo / "core" / "githooks" / "pre-push")
    body = releases if isinstance(releases, str) else json.dumps(releases)
    (repo / "RELEASES.json").write_text(body, encoding="utf-8")
    _git(repo, "init", "-q")  # --show-toplevel works post-init, no commit needed
    return repo


def _run_prepush(repo, stdin, extra_env=None):
    env = os.environ.copy()
    env.pop("RELEASE_FORCE_PUSH_TAG", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([BASH, str(repo / "core" / "githooks" / "pre-push")],
                          input=stdin, capture_output=True, text=True,
                          env=env, cwd=str(repo))


# --- kernel (check-tag-in-releases.py) ---
def test_check_tag_found_exits_zero(tmp_path):
    p = tmp_path / "RELEASES.json"
    p.write_text(json.dumps([{"version": "0.3.0"}]), encoding="utf-8")
    assert _run_check_tag("0.3.0", p).returncode == 0


def test_check_tag_missing_exits_nonzero(tmp_path):
    p = tmp_path / "RELEASES.json"
    p.write_text(json.dumps([{"version": "0.3.0"}]), encoding="utf-8")
    r = _run_check_tag("9.9.9", p)
    assert r.returncode == 1 and "no entry" in r.stderr


def test_check_tag_empty_releases_exits_nonzero(tmp_path):
    """Fail-closed: an empty manifest means no release recorded -> no tag pushable
    (distinct from load_releases()'s bootstrap [] accommodation)."""
    p = tmp_path / "RELEASES.json"
    p.write_text("[]", encoding="utf-8")
    r = _run_check_tag("0.1.0", p)
    assert r.returncode == 1 and "empty/missing" in r.stderr


def test_check_tag_malformed_releases_exits_nonzero(tmp_path):
    p = tmp_path / "RELEASES.json"
    p.write_text("[ {bad json ", encoding="utf-8")
    r = _run_check_tag("0.1.0", p)
    assert r.returncode == 1 and "malformed" in r.stderr


def test_check_tag_real_repo_existing_tags_pass():
    """Integration: the real repo's recorded tags must pass against the real
    RELEASES.json (1.0.0 and 0.2.0 are both present)."""
    real = PROJECT_ROOT / "RELEASES.json"
    assert _run_check_tag("1.0.0", real).returncode == 0
    assert _run_check_tag("0.2.0", real).returncode == 0


# --- hook (core/githooks/pre-push) ---
@requires_git
def test_prepush_hook_blocks_unrecorded_tag(tmp_path):
    repo = _setup_prepush_repo(tmp_path, [{"version": "0.3.0"}])
    r = _run_prepush(repo, f"refs/tags/v9.9.9 abc123def {_ZERO_OID} refs/tags/v9.9.9\n")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSED" in r.stderr


@requires_git
def test_prepush_hook_allows_recorded_tag(tmp_path):
    repo = _setup_prepush_repo(tmp_path, [{"version": "0.3.0"}])
    r = _run_prepush(repo, f"refs/tags/v0.3.0 abc123def {_ZERO_OID} refs/tags/v0.3.0\n")
    assert r.returncode == 0, r.stdout + r.stderr


@requires_git
def test_prepush_hook_ignores_non_vtags(tmp_path):
    repo = _setup_prepush_repo(tmp_path, [{"version": "0.3.0"}])
    r = _run_prepush(repo, f"refs/tags/release-2026 abc123def {_ZERO_OID} refs/tags/release-2026\n")
    assert r.returncode == 0, r.stdout + r.stderr


@requires_git
def test_prepush_hook_ignores_branch_pushes(tmp_path):
    repo = _setup_prepush_repo(tmp_path, [{"version": "0.3.0"}])
    r = _run_prepush(repo, f"refs/heads/main abc123def {_ZERO_OID} refs/heads/main\n")
    assert r.returncode == 0, r.stdout + r.stderr


@requires_git
def test_prepush_hook_force_override(tmp_path):
    """RELEASE_FORCE_PUSH_TAG=1 bypasses the gate (logged to stderr)."""
    repo = _setup_prepush_repo(tmp_path, [{"version": "0.3.0"}])
    r = _run_prepush(repo, f"refs/tags/v9.9.9 abc123def {_ZERO_OID} refs/tags/v9.9.9\n",
                     extra_env={"RELEASE_FORCE_PUSH_TAG": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "AUDIT" in r.stderr


@requires_git
def test_prepush_hook_skips_tag_deletion(tmp_path):
    """An all-zero local-oid is a tag deletion — not gated, even if unrecorded."""
    repo = _setup_prepush_repo(tmp_path, [{"version": "0.3.0"}])
    r = _run_prepush(repo, f"refs/tags/v9.9.9 {_ZERO_OID} abc123def refs/tags/v9.9.9\n")
    assert r.returncode == 0, r.stdout + r.stderr


@requires_git
def test_prepush_hook_blocks_multiple_reports_all(tmp_path):
    """`git push --tags` style: multiple v* tags, the hook checks ALL and refuses
    if ANY is unrecorded (doesn't stop at the first)."""
    repo = _setup_prepush_repo(tmp_path, [{"version": "0.3.0"}])
    stdin = (f"refs/tags/v0.3.0 aaa111 {_ZERO_OID} refs/tags/v0.3.0\n"
             f"refs/tags/v9.9.9 bbb222 {_ZERO_OID} refs/tags/v9.9.9\n")
    r = _run_prepush(repo, stdin)
    assert r.returncode == 1, r.stdout + r.stderr


@requires_git
def test_prepush_hook_empty_releases_fail_closed(tmp_path):
    """Bootstrap state ([] manifest) is fail-closed: no recorded release means no
    v* tag should be pushable."""
    repo = _setup_prepush_repo(tmp_path, [])
    r = _run_prepush(repo, f"refs/tags/v0.3.0 abc123def {_ZERO_OID} refs/tags/v0.3.0\n")
    assert r.returncode == 1, r.stdout + r.stderr


# ===========================================================================
# 11. Happy-path frontier-invariant PASS (seed reachable, frontier >= seed) — #58
# ===========================================================================
# Sections 7-8 cover the invariant's fail-closed + force-override paths; these
# cover the PASS path: the seed feed IS reachable and the new version is >= the
# seed latest, so the cut proceeds WITHOUT --force-release. Dry-run against the
# real repo with RELEASE_SEED_URL pointed at a file:// seed fixture.
def _current_repo_version():
    init_py = (PROJECT_ROOT / "mind_api" / "src" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init_py)
    assert m, "could not read the real repo __version__"
    return m.group(1)


@live_release_chain_synced
def test_invariant_pass_without_force_release(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([{"version": "0.1.0"}]), encoding="utf-8")
    r = run_release("patch", "--summary", "t", "--dry-run",
                    extra_env={"RELEASE_SEED_URL": seed.as_uri()})
    assert r.returncode == 0, r.stderr + r.stdout
    assert "frontier-invariant OK" in r.stdout
    assert "dry-run] OK" in r.stdout
    assert "AUDIT force-release" not in r.stderr   # no override needed on the happy path


@live_release_chain_synced
def test_invariant_pass_equal_version(tmp_path):
    """compare()==0 is still >= — an equal seed version passes without force."""
    new = L.bump_version(_current_repo_version(), "patch")
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([{"version": new}]), encoding="utf-8")
    r = run_release("patch", "--summary", "t", "--dry-run",
                    extra_env={"RELEASE_SEED_URL": seed.as_uri()})
    assert r.returncode == 0, r.stderr + r.stdout
    assert "frontier-invariant OK" in r.stdout


@live_release_chain_synced
def test_invariant_pass_frontier_ahead(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([{"version": "0.0.1"}]), encoding="utf-8")
    r = run_release("patch", "--summary", "t", "--dry-run",
                    extra_env={"RELEASE_SEED_URL": seed.as_uri()})
    assert r.returncode == 0, r.stderr + r.stdout
    assert "frontier-invariant OK" in r.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
