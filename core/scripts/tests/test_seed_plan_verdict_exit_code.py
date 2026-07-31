""" — the seed --plan verdict must reach its caller as an EXIT CODE.

WHY THESE TESTS ARE SUBPROCESS-BASED AND MUST STAY THAT WAY
-----------------------------------------------------------
The defect had two layers, and an in-process test catches NEITHER:

  1. `_seed_engine.py` main() computed the verdict and returned nothing —
     the `plan` branch ended on a comment saying "ALWAYS exit 0".
  2. `if __name__ == "__main__": main()` DISCARDED main()'s return value, so
     even after layer 1 was fixed the process still exited 0.
  3. `seed-transplant.sh` hardcoded `exit 0` after invoking the engine.

A test that imports the module and calls `do_plan()` directly asserts the
verdict STRING and passes against every one of those three bugs, because the
verdict string was never wrong — only its journey to the caller was. The
observable that actually broke is the PROCESS EXIT CODE, so the probe has to
be a process. (guard-920: replicate the production arg shape — production is
`promote-to-upstream.sh` shelling `seed-transplant.sh` shelling the engine.)

Measured consequence of the gap, Hop 2 (Claude-Mind -> ZDS v2.8.4, 2026-07-30):
VERDICT: DO NOT PROMOTE printed over 151 prod-ahead files, promote-to-upstream
planted anyway, 142 files lost 1183 lines, 2 genuine casualties restored by hand.

EXIT VOCABULARY (SSOT: the `plan` dispatch comment in _seed_engine.py)
  0  = SAFE
  20 = REVIEW REQUIRED
  21 = DO NOT PROMOTE
Chosen outside seed-transplant.sh's own 2-8 failure range so a verdict can never
be confused with a usage error or a mutation fault.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _runtime_bash import bash_cmd  # noqa: E402  (guard-580: never a bare "bash")

ENGINE_PATH = CORE_SCRIPTS / "_seed_engine.py"
TRANSPLANT_SH = CORE_SCRIPTS / "seed-transplant.sh"
PROMOTE_SH = CORE_SCRIPTS / "promote-to-upstream.sh"

_spec = importlib.util.spec_from_file_location("_seed_engine_verdict_t", ENGINE_PATH)
_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_engine)

MANIFEST_YAML = """\
include:
  - path: core/base.py
    type: file
transformations: []
"""

SRC_BASE = "BASE = 'x'\n"
# Dest carries a line the seed lacks -> prod-ahead -> DO NOT PROMOTE (guard-119).
DEST_BASE_PROD_AHEAD = "BASE = 'x'\nDOWNSTREAM_ONLY = 'tuned here'\n"
# Dest is a strict subset -> diverged but NOT prod-ahead -> verdict stays SAFE.
DEST_BASE_CLEAN = "BASE = 'x'\n"


def _fixture(tmp_path: Path, dest_base: str):
    """Minimal source+dest+manifest triple. One include file keeps the plan fast
    (the real manifest carries 2008 files) and makes the verdict unambiguous."""
    src = tmp_path / "src"
    (src / "core").mkdir(parents=True)
    (src / "core" / "base.py").write_text(SRC_BASE, encoding="utf-8")

    dest = tmp_path / "dest"
    (dest / "core").mkdir(parents=True)
    (dest / "core" / "base.py").write_text(dest_base, encoding="utf-8")

    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(MANIFEST_YAML, encoding="utf-8")
    return src, dest, manifest


def _run_engine_plan(src: Path, dest: Path, manifest: Path):
    return subprocess.run(
        [sys.executable, ENGINE_PATH.as_posix(), "plan",
         "--manifest", manifest.as_posix(),
         "--source", src.as_posix(),
         "--dest", dest.as_posix()],
        capture_output=True, text=True,
    )


# ── Layer 1+2: the engine CLI ────────────────────────────────────────────────

def test_engine_plan_exits_21_on_do_not_promote(tmp_path):
    """THE load-bearing test. Fails against the original code three separate
    ways: no return, discarded return, and (via the sibling below) hardcoded 0."""
    src, dest, manifest = _fixture(tmp_path, DEST_BASE_PROD_AHEAD)
    r = _run_engine_plan(src, dest, manifest)
    assert "DO NOT PROMOTE" in r.stdout, (
        "fixture did not produce the intended verdict; the exit-code assertion "
        "below would be vacuous. stdout:\n" + r.stdout[:2000])
    assert r.returncode == 21, (
        "plan printed DO NOT PROMOTE but exited %d — the refusal is invisible to "
        "its caller, which is the entire g-115-4136 defect. stdout:\n%s"
        % (r.returncode, r.stdout[:2000]))


def test_engine_plan_exits_0_on_safe(tmp_path):
    """Positive control. Without this, test 1 would still pass if the engine
    exited 21 unconditionally — which would block every promotion forever."""
    src, dest, manifest = _fixture(tmp_path, DEST_BASE_CLEAN)
    r = _run_engine_plan(src, dest, manifest)
    assert "DO NOT PROMOTE" not in r.stdout, (
        "control fixture unexpectedly prod-ahead:\n" + r.stdout[:2000])
    assert r.returncode == 0, (
        "a non-refusing plan must exit 0 or every promote aborts. got %d\n%s"
        % (r.returncode, r.stdout[:2000]))


def test_engine_plan_is_still_read_only(tmp_path):
    """The exit code changed; the read-only contract did not. A plan that
    mutates its destination would be a far worse regression than the one fixed."""
    src, dest, manifest = _fixture(tmp_path, DEST_BASE_PROD_AHEAD)
    before = (dest / "core" / "base.py").read_text(encoding="utf-8")
    _run_engine_plan(src, dest, manifest)
    assert (dest / "core" / "base.py").read_text(encoding="utf-8") == before
    assert not list(dest.glob(".seed-backup-*")), "plan must not create backups"


# ── Layer 3: the shell propagates it (this is where `exit 0` was hardcoded) ──

def test_seed_transplant_sh_propagates_the_verdict_rc(tmp_path):
    """seed-transplant.sh --plan must return the engine's rc, not a constant.
    End-to-end through the real script, with the production arg shape.

    The fixture must use a REAL include file, because seed-transplant.sh always
    passes `--source "$PROJECT_ROOT"` and offers no way to override it. An
    earlier version of this test pointed at a synthetic `core/base.py`: the
    engine then saw a file absent from the real source, scored it as an ORPHAN
    DELETION, and returned REVIEW REQUIRED (20) instead of DO NOT PROMOTE (21).
    The rc was non-zero either way, so a bare `assert rc != 0` would have passed
    while testing a different code path entirely — which is why the
    verdict-in-stdout assertion below runs FIRST and is not decoration.
    """
    real_rel = ".claude/rules/first-principles.md"
    real_src = PROJECT_ROOT / real_rel
    assert real_src.is_file(), "fixture needs a real include file at %s" % real_rel

    dest = tmp_path / "dest"
    (dest / ".claude" / "rules").mkdir(parents=True)
    # Dest = the real file PLUS a downstream-only line -> prod-ahead (guard-119).
    (dest / real_rel).write_text(
        real_src.read_text(encoding="utf-8") + "\nDOWNSTREAM ONLY LINE\n",
        encoding="utf-8")

    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "include:\n  - path: %s\n    type: file\ntransformations: []\n" % real_rel,
        encoding="utf-8")

    r = subprocess.run(
        bash_cmd(TRANSPLANT_SH.as_posix(), dest.as_posix(),
                 "--plan", "--manifest", manifest.as_posix()),
        capture_output=True, text=True, cwd=PROJECT_ROOT.as_posix(),
    )
    combined = r.stdout + r.stderr
    assert "DO NOT PROMOTE" in combined, (
        "shell layer did not reach the verdict; rc assertion would be vacuous. "
        "rc=%d\n%s" % (r.returncode, combined[:2500]))
    assert r.returncode == 21, (
        "seed-transplant.sh swallowed the verdict rc (got %d). Its --plan branch "
        "hardcoded `exit 0` before g-115-4136.\n%s" % (r.returncode, combined[:2500]))


# ── Layer 4: promote-to-upstream consumes it ─────────────────────────────────

def test_promote_force_past_plan_requires_a_justification():
    """A bare boolean override would let the gate be waved through by reflex.
    Behavioral, not structural: the script must REFUSE the valueless form."""
    r = subprocess.run(
        bash_cmd(PROMOTE_SH.as_posix(), "--target", "/nonexistent-xyz",
                 "--force-past-plan"),
        capture_output=True, text=True, cwd=PROJECT_ROOT.as_posix(),
    )
    assert r.returncode == 2, "valueless --force-past-plan must exit 2, got %d" % r.returncode
    assert "requires a justification" in (r.stdout + r.stderr)


def test_promote_treats_21_as_refusal_and_not_as_run_failure():
    """The original code collapsed 'could not assess' and 'assessed: no' into one
    `|| fail`. Distinguishing them IS the fix, so pin that the 21 arm exists and
    is separate from the catch-all. Structural by necessity — reaching this arm
    live needs a real living-prod dest — and NEVER sufficient alone (guard-1451),
    which is why the four behavioral tests above carry the actual proof."""
    src = PROMOTE_SH.read_text(encoding="utf-8")
    assert "PLAN_RC=$?" in src, "plan rc must be captured, not discarded"
    assert "--force-past-plan" in src, "override flag missing"
    # The refusal arm and the could-not-run arm must be distinct branches.
    assert "21)" in src, "no dedicated DO NOT PROMOTE arm"
    assert "failed to run" in src, "catch-all arm must name run-failure distinctly"


# ── The preserve-list half of the goal ───────────────────────────────────────

def test_gitignore_is_deployment_local():
    """.gitignore was overwritten by three consecutive syncs because it was
    absent from the single-source-of-truth preserve set."""
    assert ".gitignore" in _engine._DEPLOYMENT_LOCAL_FILES
    assert _engine._is_preserved_at_dest(".gitignore") is True


def test_gitignore_is_excluded_from_prod_ahead_scan(tmp_path):
    """Consequence of the line above, and the reason it matters: a
    deployment-local file must not also trip the prod-ahead refusal, or every
    living-prod promote would now abort on a legitimately-divergent .gitignore."""
    src = tmp_path / "src"
    src.mkdir()
    (src / ".gitignore").write_text("*.log\n", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / ".gitignore").write_text("*.log\n!.mind-data/\n", encoding="utf-8")
    manifest = tmp_path / "m.yaml"
    manifest.write_text("include:\n  - path: .gitignore\n    type: file\ntransformations: []\n",
                        encoding="utf-8")
    r = _run_engine_plan(src, dest, manifest)
    assert "DO NOT PROMOTE" not in r.stdout, (
        "a divergent deployment-local .gitignore must not trigger the prod-ahead "
        "refusal (§3 skips _DEPLOYMENT_LOCAL_FILES):\n" + r.stdout[:2000])
    assert r.returncode != 21
