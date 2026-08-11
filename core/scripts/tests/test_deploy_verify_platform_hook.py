"""deploy-verify.sh platform-hook seam ().

WHY THIS EXISTS. guard-119 names deploy-verify.sh as THE canonical post-push
probe, and it read GitHub Actions ONLY. Measured 2026-08-06 on commit c8fdb2e
of a live product repo: the Actions run concluded SUCCESS while the hosting
platform's build of the same commit FAILED, main was blocked, and this script
returned {"status":"ok"} exit 0. Following the guardrail exactly closed the
goal clean on a blocked pipeline.

The fix is a Pattern-B world-script slot, mirroring iteration-close.sh:1838:
core provides the seam and stays domain-free (no cloud-vendor or product name
appears in core/scripts/deploy-verify.sh); the DOMAIN supplies the probe at
$WORLD_DIR/scripts/deploy-verify-platform.sh. A world with no hook behaves
byte-identically to before -- pinned by test_no_hook_verdict_is_unchanged.

The gh stub below drives the GitHub half to a clean "ok" without network, so
every assertion here isolates the platform half.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from _bash_helpers import BASH

REPO_ROOT = Path(__file__).resolve().parents[2].parent
SCRIPT = REPO_ROOT / "core" / "scripts" / "deploy-verify.sh"

# A workflow file whose `on:` is push, base64'd -- the push-capability helper
# parses this to decide the repo can produce a deploy run.
WORKFLOW_YAML_B64 = "b246IHB1c2gKam9iczoge30K"  # "on: push\njobs: {}\n"

GH_STUB = r"""#!/usr/bin/env bash
# Minimal `gh` stand-in: enough of the api surface for deploy-verify.sh to
# reach a clean GitHub-side "ok" with no network.
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
        printf '{"encoding":"base64","content":"%s"}\n' "WORKFLOW_B64" ;;
    *actions/runs*)
        echo '{"workflow_runs":[{"name":"CI","status":"completed","conclusion":"success","html_url":"https://example.invalid/run/1","event":"push"}]}' ;;
    *commits/*)
        echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ;;
    *)
        echo "{}" ;;
esac
"""

HOOK_TEMPLATE = r"""#!/usr/bin/env bash
# Stub platform hook. Emits a fixed verdict so the core seam can be tested
# without any cloud dependency. Always writes a marker to stderr so the
# stderr-surfacing assertion is not vacuous.
echo 'STUBERR-marker: hook diagnostic' >&2
printf '%s\n' 'PAYLOAD'
exit HOOKRC
"""

# A hook that sleeps, to prove the call is bounded.
SLOW_HOOK = r"""#!/usr/bin/env bash
sleep SLEEPSECS
printf '%s\n' '{"state":"ok","detail":"should never be reached"}'
"""


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture()
def rig(tmp_path):
    """Builds a git repo, a gh stub, and a world dir; returns a runner."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin",
                    "https://github.com/acme/widget-service.git"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.invalid", "-c", "user.name=t",
         "commit", "-qm", "init"],
        cwd=repo, check=True,
    )

    gh = _write(tmp_path / "gh-stub.sh", GH_STUB.replace("WORKFLOW_B64", WORKFLOW_YAML_B64))

    world = tmp_path / "world"
    (world / "scripts").mkdir(parents=True)

    def run(hook_payload=None, hook_rc=0):
        if hook_payload is not None:
            _write(
                world / "scripts" / "deploy-verify-platform.sh",
                HOOK_TEMPLATE.replace("PAYLOAD", hook_payload).replace("HOOKRC", str(hook_rc)),
            )
        env = dict(os.environ)
        env.update({
            "GH_BIN": str(gh),
            "WORLD_DIR": str(world),
            "STORAGE_BACKEND": "local",
        })
        proc = subprocess.run(
            [BASH, SCRIPT.as_posix(), "--dir", repo.as_posix(),
             "--timeout-mins", "1", "--poll-secs", "1", "--grace-secs", "1"],
            capture_output=True, text=True, env=env, timeout=180,
        )
        line = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
        payload = json.loads(line[-1]) if line else {}
        return proc.returncode, payload

    return run



@pytest.fixture()
def rig_slow(tmp_path):
    """Like `rig`, but installs a hook that SLEEPS, to prove the call is bounded."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin",
                    "https://github.com/acme/widget-service.git"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@e.invalid", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    gh = _write(tmp_path / "gh-slow.sh", GH_STUB.replace("WORKFLOW_B64", WORKFLOW_YAML_B64))
    world = tmp_path / "world"
    (world / "scripts").mkdir(parents=True)

    def run(timeout_secs, sleep_secs):
        _write(world / "scripts" / "deploy-verify-platform.sh",
               SLOW_HOOK.replace("SLEEPSECS", str(sleep_secs)))
        env = dict(os.environ)
        env.update({
            "GH_BIN": str(gh),
            "WORLD_DIR": str(world),
            "STORAGE_BACKEND": "local",
            "DEPLOY_VERIFY_HOOK_TIMEOUT": str(timeout_secs),
        })
        proc = subprocess.run(
            [BASH, SCRIPT.as_posix(), "--dir", repo.as_posix(),
             "--timeout-mins", "1", "--poll-secs", "1", "--grace-secs", "1"],
            capture_output=True, text=True, env=env, timeout=180,
        )
        line = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
        return proc.returncode, (json.loads(line[-1]) if line else {})

    return run

def test_no_hook_verdict_is_unchanged(rig):
    """REGRESSION PIN: a world with no platform hook must behave exactly as
    before the seam existed. This one passes both pre- and post-fix by design
    -- it is the guarantee that adding the seam changed nothing for the many
    repos and worlds this does not apply to."""
    rc, payload = rig()
    assert rc == 0
    assert payload["status"] == "ok"


def test_platform_failed_overrides_green_ci(rig):
    """THE MEASURED CASE. Actions green, platform build red -> must NOT be ok."""
    rc, payload = rig('{"state":"failed","detail":"job 280 FAILED","platform":"stub"}')
    assert rc == 1, f"expected failed(1), got {rc}: {payload}"
    assert payload["status"] == "failed"
    assert "280" in json.dumps(payload)


def test_platform_pending_is_unverified_never_ok(rig):
    """rb-611: 'cannot tell yet' is exit 2, never collapsed into a pass."""
    rc, payload = rig('{"state":"pending","detail":"job RUNNING","platform":"stub"}')
    assert rc == 2, f"expected unverified(2), got {rc}: {payload}"
    assert payload["status"] == "unverified"


def test_platform_unknown_is_unverified(rig):
    """A probe that cannot see the platform must not report a pass. This is the
    direction that matters: the defect being fixed is a false clean."""
    rc, payload = rig('{"state":"unknown","detail":"cloud CLI unavailable on this box"}')
    assert rc == 2
    assert payload["status"] == "unverified"


def test_platform_absent_keeps_ci_verdict(rig):
    """Repo genuinely has no platform app -> the GitHub verdict stands."""
    rc, payload = rig('{"state":"absent","detail":"no app for this repo"}')
    assert rc == 0
    assert payload["status"] == "ok"


def test_platform_ok_keeps_ci_verdict(rig):
    """Both halves green -> ok."""
    rc, payload = rig('{"state":"ok","detail":"job SUCCEED","platform":"stub"}')
    assert rc == 0
    assert payload["status"] == "ok"


def test_hook_crash_is_unverified_not_ok(rig):
    """A hook that dies is 'cannot tell', not 'clean'. Fail-safe direction:
    the whole defect class here is a false ok, so a broken probe must never
    produce one."""
    rc, payload = rig("not json at all", hook_rc=1)
    assert rc == 2, f"expected unverified(2), got {rc}: {payload}"
    assert payload["status"] == "unverified"


def test_hook_found_without_world_dir_in_env(tmp_path):
    """PRODUCTION SHAPE (guard-920/guard-1943). Every other test here sets
    WORLD_DIR explicitly -- and that is exactly why they ALL passed while the
    seam was inert in production.

    deploy-verify.sh is invoked as guard-119's canonical probe, a bare
    `deploy-verify.sh --dir <repo>`, and never sourced _paths.sh, so WORLD_DIR
    is absent at every real call site. The first implementation read
    `${WORLD_DIR:-}` alone, resolving the hook to "/scripts/..." -- never a
    file, always `absent`. Measured end-to-end against the real red commit with
    the hook installed: {"status":"ok"} exit 0.

    So this test unsets WORLD_DIR and WORLD_PATH and forces resolution to go
    through _paths.sh (via its MIND_WORLD override), which is the only path
    that exercises the wiring rather than the function.
    """
    world = tmp_path / "w"
    (world / "scripts").mkdir(parents=True)
    _write(
        world / "scripts" / "deploy-verify-platform.sh",
        HOOK_TEMPLATE.replace("PAYLOAD", '{"state":"failed","detail":"wired"}').replace("HOOKRC", "0"),
    )

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin",
                    "https://github.com/acme/widget-service.git"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@e.invalid", "-c", "user.name=t",
                    "commit", "-qm", "i"], cwd=repo, check=True)

    gh = _write(tmp_path / "gh2.sh", GH_STUB.replace("WORKFLOW_B64", WORKFLOW_YAML_B64))

    env = dict(os.environ)
    env.pop("WORLD_DIR", None)
    env.pop("WORLD_PATH", None)
    env.pop("DEPLOY_VERIFY_PLATFORM_HOOK", None)
    env["MIND_WORLD"] = str(world)   # _paths.sh honors this; WORLD_DIR stays unset
    env["GH_BIN"] = str(gh)
    env["STORAGE_BACKEND"] = "local"

    proc = subprocess.run(
        [BASH, SCRIPT.as_posix(), "--dir", repo.as_posix(),
         "--timeout-mins", "1", "--poll-secs", "1", "--grace-secs", "1"],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert proc.returncode == 1, (
        "hook was not consulted with WORLD_DIR absent -- the seam is inert in "
        f"the shape it actually runs in. rc={proc.returncode} out={proc.stdout}"
    )


def test_core_script_names_no_vendor(rig):
    """domain-free-examples.md: core/scripts must not name a cloud vendor or
    product. The seam exists precisely so the domain owns those strings."""
    body = SCRIPT.read_text(encoding="utf-8").lower()
    for term in ("amplify", "aws", "vercel", "netlify"):
        assert term not in body, f"core deploy-verify.sh names '{term}'"

def test_nonzero_exit_hook_verdict_is_still_used(rig):
    """A hook that emits a VALID verdict and exits non-zero must have that
    verdict HONORED, not discarded.

    Found by /fresh-eyes-code on this file's own commit. The original
    `raw=$(...) || raw=""` threw stdout away whenever the hook exited non-zero,
    so a hook emitting {"state":"failed"} + exit 1 was reported as
    "no parseable verdict" -> unverified(2). Two defects in one: a definite
    FAILED silently weakened to "cannot tell", and the detail named a cause
    that was not the cause. Non-zero exit is not exotic here -- this seam ITSELF
    maps failed->exit 1, so a hook author copying the convention hits it.
    """
    rc, payload = rig('{"state":"failed","detail":"build 999 FAILED"}', hook_rc=1)
    assert rc == 1, f"verdict discarded on non-zero exit: rc={rc} {payload}"
    assert payload["status"] == "failed"
    assert "999" in json.dumps(payload)


def test_hook_stderr_is_surfaced_in_detail(rig):
    """A failing hook's stderr must reach the verdict.

    Discarding it (`2>/dev/null`) made every hook failure look identical and
    forced a by-hand re-run to learn anything -- verify-before-assuming rule 4,
    a silenced command is zero signals, not one.
    """
    rc, payload = rig('', hook_rc=1)  # no stdout; stub writes to stderr below
    assert rc == 2
    assert payload["status"] == "unverified"
    assert payload["platform_state"] == "unknown"
    assert "STUBERR-marker" in payload["detail"], (
        f"hook stderr was discarded: {payload['detail']}")
    assert "exit" in payload["detail"].lower()  # and the code is named


def test_hanging_hook_times_out_and_is_unverified(rig_slow):
    """An unbounded hook call can hang deploy-verify.sh forever -- and this is
    guard-119's canonical post-push probe invoked from post-execution, so that
    hangs the loop. --timeout-mins covers only the polling loop, never the hook.

    Expiry must map to `unknown` (already the fail-safe state), never to ok.
    """
    rc, payload = rig_slow(timeout_secs=1, sleep_secs=30)
    assert rc == 2, f"hook was not bounded: rc={rc} {payload}"
    assert payload["status"] == "unverified"
    assert payload["platform_state"] == "unknown"
    assert "timed out" in payload["detail"].lower()

