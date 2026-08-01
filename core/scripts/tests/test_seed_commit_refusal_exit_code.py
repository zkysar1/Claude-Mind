""" — a REFUSED plant commit must not report success.

WHY THESE TESTS ARE SUBPROCESS-BASED AND MUST STAY THAT WAY
-----------------------------------------------------------
The defect was a swallowed exit code, and nothing else. Every value in the
system was CORRECT: git returned non-zero, the pre-commit hook printed its
refusal, seed-verify printed the dirty tree. Only the journey of those signals
to the caller was broken, in three places that each independently sufficed:

  1. `seed-transplant.sh` ended its commit block with
     `git commit ... || echo "  (nothing to commit)"`. The `||` both discarded
     the rc AND relabelled a hook refusal as an empty diff — and because it was
     the last statement in the `if`, the script exited 0.
  2. `seed-transplant.sh` then ran seed-verify with
     `|| echo "  (verification reported issues — see above)"`, so even a verify
     that FAILED could not make the plant fail.
  3. `seed-verify.sh` Check 4 printed "N changed files (expected after plant)"
     unconditionally and never incremented FAILS — so post-plant, the one state
     that PROVES the plant failed was hardcoded as the expected state.

Consequence: `promote-to-upstream.sh` carries two gates —
`|| fail "seed plant failed"` (L287) and `|| fail "post-promotion verify FAILED"`
(L291) — and layers 1-2 killed the first while layer 3 killed the second. A
promotion that committed NOTHING pushed a branch, opened a PR, and printed
"═══ PROMOTED ═══". Fixing either gate alone still leaves the other dead, which
is why the tests below assert BOTH the transplant rc and the verify rc.

An in-process test catches none of this: it would assert the message strings,
which were never wrong. The observable that broke is the PROCESS EXIT CODE, so
the probe has to be a process. (guard-920: replicate the production arg shape —
production is `promote-to-upstream.sh` shelling `seed-transplant.sh`.)

This is the --commit-path twin of g-115-4136 (the --plan verdict that computed
DO NOT PROMOTE and exited 0). Same class, same script, opposite branch.

EXIT VOCABULARY (SSOT: the argument parser + failure exits in seed-transplant.sh)
  2-8 = usage / pre-flight / mutation faults (pre-existing)
  9   = commit REFUSED at the destination (this fix)
  10  = post-plant verification FAILED (this fix)
Chosen above the pre-existing 2-8 range and clear of the --plan verdict codes
(0/20/21) so a refusal can never be confused with a usage error or a verdict.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _runtime_bash import bash_cmd  # noqa: E402  (guard-580: never a bare "bash")

TRANSPLANT_SH = CORE_SCRIPTS / "seed-transplant.sh"
VERIFY_SH = CORE_SCRIPTS / "seed-verify.sh"
PROMOTE_SH = CORE_SCRIPTS / "promote-to-upstream.sh"

# seed-transplant.sh hardcodes `--source "$PROJECT_ROOT"` with no override, so
# every include must be a REAL file in this repo or the engine scores it as an
# orphan and the plant exercises a different path than production does.
# check-prerequisites.sh is not decorative: seed-verify Check 6 (bootability)
# looks for it by hardcoded path, independent of the manifest, so a fixture that
# omits it can never reach Status: PASS and the positive control below would be
# unable to distinguish "my fix broke the plant" from "the fixture was unbootable".
MANIFEST_YAML = """\
include:
  - path: .claude/rules/first-principles.md
    type: file
  - path: core/scripts/check-prerequisites.sh
    type: file
transformations: []
"""

REFUSAL_MARKER = "REFUSED-BY-POLICY-GATE"
PRE_COMMIT_HOOK = (
    "#!/bin/sh\n"
    f"echo '{REFUSAL_MARKER}: this destination declines the commit' >&2\n"
    "exit 1\n"
)

# The source publishability gate inspects PROJECT_ROOT, which is orthogonal to the
# commit-swallow defect and would make these tests depend on the live tree's
# cleanliness. Skipping it is the documented, audited escape hatch.
SKIP_PREFLIGHT = (
    "test fixture: source publishability is orthogonal to the "
    "destination-commit-refusal path under test"
)


def _git(dest: Path, *args, check=True):
    return subprocess.run(["git", "-C", dest.as_posix(), *args],
                          capture_output=True, text=True, check=check)


def _fixture(tmp_path: Path, *, refusing_hook: bool):
    """A destination repo with a PRE-EXISTING head, optionally guarded by a
    pre-commit hook that refuses everything.

    The pre-existing commit is load-bearing, not scenery: it is what
    seed-verify's Check 4 printed as `last_commit=` during the incident, which
    is how an empty promotion could look like it had committed something.
    """
    dest = tmp_path / "dest"
    dest.mkdir(parents=True)
    _git(dest, "init", "-q")
    _git(dest, "config", "user.email", "seed-test@example.invalid")
    _git(dest, "config", "user.name", "seed-test")
    (dest / "README.md").write_text("pre-existing\n", encoding="utf-8")
    _git(dest, "add", "README.md")
    _git(dest, "commit", "-q", "-m", "pre-existing head")

    if refusing_hook:
        # Installed AFTER the seed commit, or the fixture could not build itself.
        hook = dest / ".git" / "hooks" / "pre-commit"
        hook.write_text(PRE_COMMIT_HOOK, encoding="utf-8")
        hook.chmod(0o755)

    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(MANIFEST_YAML, encoding="utf-8")
    return dest, manifest


def _assert_hook_actually_refuses(dest: Path):
    """PRECONDITION. A pre-commit hook can be silently bypassed by an ambient
    `core.hooksPath` (global or system), and this fixture deliberately does NOT
    set that config — writing it is a hook-redirection that guard-901's gate
    refuses, and asserting the behavior is a stronger guarantee than configuring
    it anyway. If the hook does not fire, the plant below commits cleanly and
    every assertion in the refusal test passes for the wrong reason.
    """
    probe = dest / ".seed-hook-probe"
    probe.write_text("probe\n", encoding="utf-8")
    _git(dest, "add", probe.name)
    r = _git(dest, "commit", "-q", "-m", "hook probe", check=False)
    _git(dest, "reset", "-q")
    probe.unlink()
    assert r.returncode != 0, (
        "fixture hook did not refuse a commit — an ambient core.hooksPath is "
        "shadowing .git/hooks, so the refusal tests below would be VACUOUS")


def _run_plant(dest: Path, manifest: Path, *extra):
    return subprocess.run(
        bash_cmd(TRANSPLANT_SH, dest.as_posix(), "--force", "--commit",
                 "--no-backup", "--manifest", manifest.as_posix(),
                 "--skip-preflight", SKIP_PREFLIGHT, *extra),
        capture_output=True, text=True,
    )


def _dest_log(dest: Path) -> str:
    return _git(dest, "log", "--oneline", check=False).stdout


# ── Layer 1: the swallowed commit rc ─────────────────────────────────────────

def test_refused_commit_makes_the_plant_exit_nonzero(tmp_path):
    """THE load-bearing test. Against the original code this fails on rc alone:
    the plant printed "(nothing to commit)" and exited 0 with nothing planted."""
    dest, manifest = _fixture(tmp_path, refusing_hook=True)
    _assert_hook_actually_refuses(dest)

    r = _run_plant(dest, manifest)
    out = r.stdout + r.stderr

    assert "Committing planted files" in out, (
        "the plant never reached its commit step, so the rc assertion below "
        "would be vacuous — it failed earlier for an unrelated reason:\n"
        + out[-3000:])
    assert r.returncode == 9, (
        "commit was refused but the plant exited %d. An exit of 0 here is the "
        "entire g-115-3481 defect: promote-to-upstream's `|| fail \"seed plant "
        "failed\"` cannot fire, so an empty promotion opens a PR.\n%s"
        % (r.returncode, out[-3000:]))


def test_refused_commit_surfaces_the_hook_stderr(tmp_path):
    """The operator must be able to see WHY. The old `|| echo` replaced the
    hook's own words with a message asserting the opposite cause."""
    dest, manifest = _fixture(tmp_path, refusing_hook=True)
    _assert_hook_actually_refuses(dest)

    r = _run_plant(dest, manifest)
    out = r.stdout + r.stderr

    assert REFUSAL_MARKER in out, (
        "the destination hook's refusal text was swallowed; the operator cannot "
        "diagnose the failure:\n" + out[-3000:])
    assert "nothing to commit" not in out, (
        "a REFUSED commit was still described as an empty diff — that is the "
        "mislabel, independent of the exit code:\n" + out[-3000:])


def test_refused_commit_does_not_leave_a_plant_commit(tmp_path):
    """The state claim, not just the message claim: nothing was committed."""
    dest, manifest = _fixture(tmp_path, refusing_hook=True)
    _assert_hook_actually_refuses(dest)
    _run_plant(dest, manifest)

    log = _dest_log(dest)
    assert "chore: sync framework" not in log, (
        "a refused plant left a sync commit behind:\n" + log)
    assert "pre-existing head" in log, (
        "the destination's pre-existing head vanished:\n" + log)


# ── The discrimination the old single message could not make ─────────────────

def test_empty_index_is_reported_differently_from_a_refusal(tmp_path):
    """`git commit` exits non-zero for two OPPOSITE reasons. The old code
    collapsed them into one message, which is what made a refusal invisible.
    Replanting an unchanged tree leaves a genuinely empty index."""
    dest, manifest = _fixture(tmp_path, refusing_hook=False)
    first = _run_plant(dest, manifest)
    assert first.returncode == 0, (
        "the first plant must succeed or the no-op replant below is not a no-op:"
        "\n" + (first.stdout + first.stderr)[-3000:])

    second = _run_plant(dest, manifest)
    out = second.stdout + second.stderr

    assert second.returncode == 0, (
        "a genuine no-op replant must stay benign — treating an empty index as "
        "a refusal would break every idempotent re-plant. got %d\n%s"
        % (second.returncode, out[-3000:]))
    assert "staging area empty" in out, (
        "the benign path must SAY it was an empty index, not reuse the "
        "refusal wording:\n" + out[-3000:])
    assert REFUSAL_MARKER not in out


def test_clean_plant_still_succeeds(tmp_path):
    """Positive control. Without it, the refusal tests would still pass if the
    plant exited 9 unconditionally — which would block every promotion forever.
    Also the only test that proves the propagated seed-verify rc (layer 2) does
    not fail an honest plant."""
    dest, manifest = _fixture(tmp_path, refusing_hook=False)
    r = _run_plant(dest, manifest)
    out = r.stdout + r.stderr

    assert r.returncode == 0, (
        "an unobstructed plant must exit 0. got %d\n%s"
        % (r.returncode, out[-3000:]))
    assert "chore: sync framework" in _dest_log(dest), (
        "the plant exited 0 but committed nothing — exactly the state this "
        "whole fix exists to make impossible:\n" + _dest_log(dest))
    assert "Status: PASS" in out, (
        "post-plant verify did not pass on a clean plant:\n" + out[-3000:])


# ── Layer 3: seed-verify must be ABLE to call a dirty post-plant tree a FAIL ──

def _run_verify(dest: Path, manifest: Path, *extra):
    return subprocess.run(
        bash_cmd(VERIFY_SH, dest.as_posix(), "--manifest", manifest.as_posix(),
                 *extra),
        capture_output=True, text=True,
    )


def test_verify_fails_on_a_dirty_tree_when_a_commit_was_expected(tmp_path):
    """Post-plant, a dirty tree IS the failure. Check 4 called it "expected
    after plant" unconditionally, which is why promote's second gate was dead."""
    dest, manifest = _fixture(tmp_path, refusing_hook=False)
    assert _run_plant(dest, manifest).returncode == 0
    (dest / "uncommitted-drift.txt").write_text("drift\n", encoding="utf-8")

    r = _run_verify(dest, manifest, "--expect-commit")
    out = r.stdout + r.stderr

    assert "uncommitted change" in out, (
        "Check 4 did not report the dirty tree at all:\n" + out[-3000:])
    assert r.returncode != 0, (
        "seed-verify printed the evidence of a failed plant and exited 0 — the "
        "verifier that cannot fail is the third layer of the defect:\n"
        + out[-3000:])


def test_verify_stays_lenient_without_the_flag(tmp_path):
    """--expect-commit is opt-in for a reason: standalone `/seed verify <dest>`
    inspects whatever state a destination happens to be in, where a dirty tree
    is informational. Making strictness the default would break that caller."""
    dest, manifest = _fixture(tmp_path, refusing_hook=False)
    assert _run_plant(dest, manifest).returncode == 0
    (dest / "uncommitted-drift.txt").write_text("drift\n", encoding="utf-8")

    r = _run_verify(dest, manifest)
    out = r.stdout + r.stderr

    assert "FAILS: 0" in out, (
        "the standalone verify contract regressed — a dirty destination must "
        "not be a FAIL without --expect-commit:\n" + out[-3000:])
    assert r.returncode == 0


# ── The wiring that makes promote's second gate live ─────────────────────────

def test_promote_passes_expect_commit_to_its_verify(tmp_path):
    """Structural, and NEVER sufficient alone (guard-1451) — the behavioral
    proof is the test pair above. This one exists because promote's verify call
    is the single line that decides whether that behavior is reachable in
    production, and a silent revert of just this argument would restore the
    incident while every other test here still passed.

    The predicate deliberately covers the `[dry-run] would:` preview line as
    well as the real invocation, and that breadth is not an accident: on first
    run it caught the preview still advertising the pre-fix call shape. A
    dry-run that misdescribes the real path is its own defect — an operator
    reads the preview precisely to decide whether to run the real thing.
    """
    src = PROMOTE_SH.read_text(encoding="utf-8")
    verify_lines = [ln for ln in src.splitlines()
                    if "seed-verify.sh" in ln and not ln.lstrip().startswith("#")]
    assert verify_lines, "promote-to-upstream.sh no longer calls seed-verify.sh"
    assert all("--expect-commit" in ln for ln in verify_lines), (
        "a post-promotion seed-verify call (or its dry-run preview) omits "
        "--expect-commit, so a dirty destination reads as 'expected after "
        "plant' and the `|| fail` gate is dead again:\n" + "\n".join(verify_lines))
