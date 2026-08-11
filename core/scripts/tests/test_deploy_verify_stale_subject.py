"""Does deploy-verify.sh verify the commit anyone asked about? EXECUTED ().

THE DEFECT. `deploy-verify.sh --dir <repo>` with no `--sha` takes its subject
from `git rev-parse HEAD` on the LOCAL checkout. When that checkout is behind,
the script answers a question nobody asked. Measured on Ayoai-Environment-Server
right after a PR merge: HEAD d87b915 vs origin/main 377fa15, 14 commits behind,
and the bare form emitted `{"status":"ok"}` for d87b915. Every field in that JSON
-- repo, sha, status -- was factually CORRECT; only the subject was wrong, so the
usual false-green tell (an implausible value) is absent by construction. A merge
performed through the GitHub API is exactly the case that leaves a checkout
stale, which is the fleet's highest-volume caller (guard-119 times_active 2184).

WHY THE GATE REFUSES ONLY ON *PROVEN* DIVERGENCE, and this is the whole design.
`git ls-remote` has three outcomes and they are not equivalent:

    rc != 0          remote unreadable          UNKNOWN -> warn, proceed
    rc == 0, empty   branch has no remote head  UNKNOWN -> warn, proceed
    rc == 0, differs POSITIVE evidence          -> refuse, naming both shas

A first draft refused on all three. It broke 8 of the 12 platform-hook tests,
and -- the part worth recording -- 3 of the remaining 4 went VACUOUSLY green:
they assert `unverified`, which the over-eager gate also returned, so the suite
sat 4/12 green while exercising none of the hook it exists to test. The gate
preempted `probe_platform`, which is consulted LATER and is the entire
verification path for deploys that are not GitHub Actions. An unreadable remote
is a statement about the network, not about this checkout.

`test_unreachable_remote_warns_and_still_reaches_the_hook` is the pin for that
regression and is the load-bearing test in this file. Without it, someone
tightening the gate to "refuse whenever we cannot confirm" reintroduces the
outage silently -- the platform-hook suite would go green-but-vacuous exactly as
it did the first time, and only the count would move.

WHY ls-remote AND NOT `@{u}`. The cheaper option is to compare against the local
remote-tracking ref. Measured on 56 repos: 16 looked current by `@{u}`, but only
7 genuinely were -- 2 of them had a tracking ref that was ITSELF stale, and 7
were on unpushed branches. The tracking ref is a local cache with the same
staleness bug the gate is fixing, so using it would have reproduced the defect
one level down.
ls-remote queries the remote directly and mutates nothing (unlike a fetch).

POPULATION AT THE TIME OF THE FIX (2026-08-08, hostname cc-07, uname -r
6.8.0-136-generic, 56 repos under /opt/GitHub):
    42 of 56 (75%)  would have been verified at the WRONG sha
     7              genuinely current -- ZERO of them newly rejected
     7              on unpushed feature branches -> warn, proceed
     0              detached HEAD, 0 without an origin remote
The goal asked what fail-closed would NEWLY reject; two of the four concerns it
named have no population here, and the rejected set is the defective set.

Detached HEAD is deliberately NOT gated -- it is the CI-runner shape, where the
checkout IS the pushed sha by construction and there is no branch to compare
against.

FIXTURE NOTE: these use a REAL local bare repo as `origin` (file:// path), so
`ls-remote` genuinely succeeds offline and the proven-divergence branch is
reached for real rather than simulated. The sibling platform-hook file points
origin at an unreachable https URL, which is why it exercises the UNKNOWN path
-- the two files cover different arms of the same switch, on purpose.

guard-1165: no module-level os.environ mutation, no sys.modules stubs.
Run: STORAGE_BACKEND=local python -m pytest \
     core/scripts/tests/test_deploy_verify_stale_subject.py -q
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402  (guard-580: explicit bash binary)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "core" / "scripts" / "deploy-verify.sh"

GH_STUB = r"""#!/usr/bin/env bash
path=""
q=""
prev=""
for a in "$@"; do
    case "$prev" in -q) q="$a" ;; esac
    case "$a" in repos/*) [ -z "$path" ] && path="$a" ;; esac
    prev="$a"
done
case "$path" in
    *actions/workflows*)
        case "$q" in
            *length*) echo 1 ;;
            *) echo ".github/workflows/ci.yml" ;;
        esac ;;
    *contents/*)
        printf '{"encoding":"base64","content":"b246CiAgcHVzaDoKICAgIGJyYW5jaGVzOiBbbWFpbl0Kam9iczoKICBidWlsZDoKICAgIHJ1bnMtb246IHVidW50dS1sYXRlc3QKICAgIHN0ZXBzOgogICAgICAtIHJ1bjogZWNobyBoaQo="}\n' ;;
    *actions/runs*)
        echo '{"workflow_runs":[{"name":"CI","status":"completed","conclusion":"success","html_url":"https://example.invalid/run/1","event":"push"}]}' ;;
    *commits/*)
        echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ;;
    *)
        echo "{}" ;;
esac
"""


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "user.email=t@e.invalid", "-c", "user.name=t", *args],
        cwd=cwd, check=check, capture_output=True, text=True,
    )


@pytest.fixture()
def rig(tmp_path):
    """A working clone whose `origin` is a REAL local bare repo.

    Returns (run, repo, bare). `run` invokes the script; callers mutate the repo
    or the bare between calls to select which arm of the gate is exercised.
    """
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    _git(repo, "remote", "add", "origin", bare.as_uri())
    (repo / "f.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "-q", "origin", "main")

    gh = tmp_path / "gh-stub.sh"
    gh.write_text(GH_STUB, encoding="utf-8")
    gh.chmod(0o755)

    world = tmp_path / "world"
    (world / "scripts").mkdir(parents=True)

    def run(*extra):
        env = dict(os.environ)
        env.update({"GH_BIN": str(gh), "WORLD_DIR": str(world),
                    "STORAGE_BACKEND": "local"})
        proc = subprocess.run(
            [BASH, SCRIPT.as_posix(), "--dir", repo.as_posix(),
             "--timeout-mins", "1", "--poll-secs", "1", "--grace-secs", "1",
             *extra],
            capture_output=True, text=True, env=env, timeout=180,
        )
        lines = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
        return proc.returncode, (json.loads(lines[-1]) if lines else {}), proc.stderr

    return run, repo, bare


def _advance_remote(repo, bare):
    """Push a second commit, then reset the working clone back one — leaving the
    clone genuinely behind its own origin, which is the measured defect shape."""
    (repo / "f.txt").write_text("y", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "second")
    _git(repo, "push", "-q", "origin", "main")
    remote_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "reset", "-q", "--hard", "HEAD~1")
    local_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert local_sha != remote_sha
    return local_sha, remote_sha


# ── 1. positive control: a current checkout still verifies ──────────────────

def test_current_checkout_is_not_blocked(rig):
    """The gate must be invisible when the checkout matches origin.

    Positive control for every refusal below: each asserts the script STOPPED,
    and would pass for free if the script stopped for any unrelated reason
    (guard-1829).
    """
    run, repo, bare = rig
    rc, payload, _ = run()
    assert payload.get("status") == "ok", (
        f"a current checkout was not verified: rc={rc} {payload}")


# ── 2. THE DEFECT: proven divergence is refused, naming both shas ───────────

def test_stale_checkout_is_refused_and_names_both_shas(rig):
    """A checkout behind its own origin must NOT be verified silently.

    Before this gate the same call returned status=ok for the local sha, with
    every field factually correct and only the SUBJECT wrong.
    """
    run, repo, bare = rig
    local_sha, remote_sha = _advance_remote(repo, bare)

    rc, payload, _ = run()

    assert payload.get("status") == "unverified", (
        f"a stale checkout was verified anyway: {payload}")
    assert "STALE SUBJECT" in payload.get("detail", ""), (
        f"the refusal did not name the defect: {payload}")
    # Both shas must appear, so the reader can act without re-deriving them.
    assert payload.get("local_sha") == local_sha, f"wrong local_sha: {payload}"
    assert payload.get("remote_sha") == remote_sha, f"wrong remote_sha: {payload}"
    assert remote_sha in payload.get("detail", ""), (
        "the detail must carry the re-run command with the correct sha")


def test_the_refusal_is_reachable_both_ways(rig):
    """Discriminator (guard-1220): the SAME invocation passes before the remote
    advances and refuses after. Without this pairing, test 2 passes for any
    reason that makes the script stop."""
    run, repo, bare = rig
    _, before, _ = run()
    _advance_remote(repo, bare)
    _, after, _ = run()
    assert before.get("status") == "ok", f"pre-divergence run did not verify: {before}"
    assert after.get("status") == "unverified", f"post-divergence run verified: {after}"


# ── 3. THE REGRESSION PIN — unknown must NOT be treated as stale ────────────

def test_unreachable_remote_warns_and_still_reaches_the_hook(rig, tmp_path):
    """THE LOAD-BEARING TEST. An unreadable remote is UNKNOWN, not stale.

    A first draft refused here too. That preempted probe_platform — consulted
    later, and the entire verification path for non-Actions deploys — breaking 8
    of 12 platform-hook tests while 3 of the remaining 4 passed VACUOUSLY on the
    `unverified` the over-eager gate happened to emit.

    The assertion is not merely "did not refuse": it proves the PLATFORM HOOK
    RAN, by installing a hook whose verdict only it can produce. A weaker
    assertion would go green again the moment the gate starts short-circuiting.
    """
    run, repo, bare = rig
    # Point origin at a path that cannot be read -> ls-remote rc != 0.
    _git(repo, "remote", "set-url", "origin",
         (tmp_path / "does-not-exist.git").as_uri())

    world = tmp_path / "world"
    hook = world / "scripts" / "deploy-verify-platform.sh"
    hook.write_text(
        '#!/usr/bin/env bash\n'
        'printf \'%s\\n\' \'{"state":"failed","detail":"HOOKMARKER-9134"}\'\n',
        encoding="utf-8")
    hook.chmod(0o755)

    rc, payload, stderr = run()

    assert "HOOKMARKER-9134" in json.dumps(payload), (
        "the platform hook did NOT run — the gate short-circuited an UNKNOWN "
        f"remote as though it were proven-stale. payload={payload}")
    assert "STALE SUBJECT" not in payload.get("detail", ""), (
        f"an unreadable remote was reported as staleness: {payload}")
    assert "WARNING" in stderr, (
        f"the unknown subject was not surfaced at all: {stderr!r}")


def test_unpushed_branch_warns_and_does_not_refuse(rig):
    """A branch with no remote head is UNKNOWN too, not proven-stale.

    Measured 7 of 56 repos in this state. The warning names it; the script still
    runs, and the CI path reaches its own (correct) conclusion that no run
    exists.
    """
    run, repo, bare = rig
    _git(repo, "checkout", "-q", "-b", "feature/never-pushed")

    rc, payload, stderr = run()

    assert "STALE SUBJECT" not in payload.get("detail", ""), (
        f"an unpushed branch was reported as staleness: {payload}")
    assert "WARNING" in stderr and "no remote head" in stderr, (
        f"the unpushed branch was not surfaced: {stderr!r}")


# ── 4. the explicit form bypasses the gate entirely ─────────────────────────

def test_explicit_sha_bypasses_the_gate(rig):
    """--sha names the subject, so the local checkout is irrelevant.

    This is the invocation guard-119 now prescribes, and it must keep working on
    a stale checkout — otherwise the gate would block the very fix it advises.
    """
    run, repo, bare = rig
    _, remote_sha = _advance_remote(repo, bare)

    rc, payload, _ = run("--sha", remote_sha)

    assert "STALE SUBJECT" not in payload.get("detail", ""), (
        f"--sha did not bypass the stale-subject gate: {payload}")
    assert payload.get("sha") == remote_sha, (
        f"the explicit sha was not used as the subject: {payload}")


def test_gate_does_not_run_a_pipe_for_rc(rig):
    """SOURCE PIN for the rc-capture shape (guard-1150).

    `ls-remote ... | cut` reports CUT's status, so an unreachable remote would
    return 0 with empty output — silently collapsing UNKNOWN into "empty" and
    undoing the three-way distinction this whole gate rests on. The rc must be
    captured from the bare command.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "_dv_ls=$(git -C \"$DIR\" ls-remote origin" in src, (
        "the ls-remote call was renamed or reshaped; re-verify the rc capture")
    assert "_dv_rc=$?" in src, (
        "the ls-remote rc is no longer captured on its own line — a pipe would "
        "mask an unreachable remote as an empty result (guard-1150)")
