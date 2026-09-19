"""Pins the GitHub owner/name derivation shared by deploy-detect-hook.sh and
deploy-verify.sh, plus the write-time qualification predicate (g-115-10133).

THE DEFECT. Both scripts carried the same inline sed —
`s#^(git@|https://)([^/:]+)[:/]##; s#\\.git$##` — which is a PREFIX STRIP, not a
GitHub test: it removes git@host: or https://host/ and returns whatever is left.
On a clone whose origin is a non-GitHub URL that yields a value `gh api
repos/<value>` can never resolve, and `_repo_is_qualified` waved it through
because it only tested for a "/". Measured on cc-04: 90 live obligations, 0 ever
cleared, `not_clean` set on every framework-push close fleet-wide.

WHAT THIS FILE PINS, AND THE ASYMMETRY THAT MAKES IT EVIDENCE (guard-4166).
The fix's effect is that something STOPS APPEARING — a malformed repo value is
no longer produced. An empty string is ALSO what a completely dead derivation
returns, so "the bad value is absent" is not on its own evidence of anything.
So the tests come in two deliberate halves, and the mutation proof must show
them behaving DIFFERENTLY:

  FIX PINS (must go RED when the old sed is restored):
    test_non_github_origin_yields_no_slug
    test_a_deeper_github_path_is_not_truncated_into_owner_name
    test_repo_is_qualified_rejects_every_measured_malformation

  POSITIVE CONTROL (must stay GREEN under that same mutant):
    test_github_url_forms_all_derive_owner_name
    test_for_dir_uses_origin_when_origin_is_already_github

If the control goes red alongside the fix pins it is not a control, it is a
third copy of the same assertion, and the run proves nothing.

  WIRING PIN (a third category, and the one this file was missing):
    test_deploy_verify_reaches_a_verdict_with_world_dir_set
  The two halves above both exercise the helper DIRECTLY. Neither says anything
  about whether the production scripts can still RUN after sourcing it -- and
  the first cut of this change could not (guard-1943). See that test's docstring.

The names used here are synthetic on purpose. Pinning against this estate's real
owner/repo would couple the file to deployment identity for no test value.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _bash_helpers import BASH

_SCRIPTS = Path(__file__).resolve().parent.parent
HELPER = _SCRIPTS / "_repo_slug.sh"
PENDING = _SCRIPTS / "pending-deploys.py"


def _slug(url):
    """Call gh_slug_from_url exactly as the two production scripts do."""
    r = subprocess.run(
        [BASH, "-c", '. "$1" && gh_slug_from_url "$2"', "_", str(HELPER), url],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _slug_for_dir(d):
    r = subprocess.run(
        [BASH, "-c", '. "$1" && gh_slug_for_dir "$2"', "_", str(HELPER), str(d)],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture(scope="module")
def pd():
    spec = importlib.util.spec_from_file_location("pending_deploys", PENDING)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─── POSITIVE CONTROL — must stay GREEN under the old-sed mutant ─────────────

def test_github_url_forms_all_derive_owner_name():
    """Every GitHub form the estate actually uses still resolves. This is the
    control: the old sed handled git@ and https:// correctly too, so restoring
    it must NOT break these."""
    for url in ("git@github.com:acme/widget-service.git",
                "https://github.com/acme/widget-service.git",
                "https://github.com/acme/widget-service",
                "ssh://git@github.com/acme/widget-service.git",
                "git://github.com/acme/widget-service.git"):
        assert _slug(url) == "acme/widget-service", url


# ─── FIX PINS — must go RED under the old-sed mutant ─────────────────────────

def test_non_github_origin_yields_no_slug():
    """THE DEFECT, pinned. The old sed returned 'rack:/srv/bulk/widget-service'
    here — a value with two slashes that passed the old '/'-in-repo check and
    that gh can never resolve, so the obligation could never clear."""
    for url in ("rack:/srv/bulk/widget-service.git",
                "git@gitlab.com:acme/widget-service.git",
                "https://example.invalid/acme/widget-service.git",
                "/srv/bulk/widget-service.git",
                ""):
        assert _slug(url) == "", f"{url!r} produced a slug"


def test_a_deeper_github_path_is_not_truncated_into_owner_name():
    """A GitHub URL with more than two path segments is not an owner/name.
    Silently reshaping it into one would be the same class of bug as the sed."""
    assert _slug("https://github.com/acme/group/widget-service.git") == ""


def test_repo_is_qualified_rejects_every_measured_malformation(pd):
    assert pd._repo_is_qualified("acme/widget-service")
    assert pd._repo_is_qualified("a/b")
    # the  bare name (already caught by the old check)
    assert not pd._repo_is_qualified("widget-service")
    # the  forms the old '/'-in-repo check ADMITTED
    assert not pd._repo_is_qualified("rack:/srv/bulk/widget-service")
    assert not pd._repo_is_qualified("ssh://git@github.com/acme/widget-service")
    assert not pd._repo_is_qualified("https://github.com/acme/widget-service")
    assert not pd._repo_is_qualified("acme/group/widget-service")
    assert not pd._repo_is_qualified("")
    assert not pd._repo_is_qualified(None)


# ─── for_dir: remote preference ──────────────────────────────────────────────

def _mkrepo(tmp_path, remotes):
    d = tmp_path / "clone"
    d.mkdir()
    env = dict(os.environ)
    env.update({"GIT_CONFIG_GLOBAL": str(tmp_path / "gc"), "GIT_CONFIG_NOSYSTEM": "1"})
    (tmp_path / "gc").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(d), "init", "-q"], check=True, env=env)
    for name, url in remotes:
        subprocess.run(["git", "-C", str(d), "remote", "add", name, url], check=True, env=env)
    return d


def test_for_dir_uses_origin_when_origin_is_already_github(tmp_path):
    """Control half two: the common clone shape pays no behaviour change."""
    d = _mkrepo(tmp_path, [("origin", "git@github.com:acme/widget-service.git")])
    assert _slug_for_dir(d) == "acme/widget-service"


def test_for_dir_falls_back_to_a_github_mirror_when_origin_is_not_github(tmp_path):
    """THE ESTATE SHAPE (guard-6711): a rack bare repo is origin and GitHub is a
    push mirror. The correct answer is the MIRROR's slug, not origin's path and
    not empty."""
    d = _mkrepo(tmp_path, [("origin", "rack:/srv/bulk/widget-service.git"),
                           ("github", "https://github.com/acme/widget-service.git")])
    assert _slug_for_dir(d) == "acme/widget-service"


def test_for_dir_is_empty_when_no_remote_is_github(tmp_path):
    """Empty is the correct answer, and the callers turn it into
    skip-registration / unverified-with-a-reason — never a guess."""
    d = _mkrepo(tmp_path, [("origin", "rack:/srv/bulk/widget-service.git")])
    assert _slug_for_dir(d) == ""


def test_both_production_scripts_use_the_shared_helper(tmp_path):
    """The two scripts drifted precisely because each carried its own copy of the
    rule. Pin that neither re-inlines a prefix-strip sed."""
    for name in ("deploy-detect-hook.sh", "deploy-verify.sh"):
        text = (_SCRIPTS / name).read_text(encoding="utf-8", errors="replace")
        assert "_repo_slug.sh" in text, f"{name} no longer sources the shared helper"
        assert "gh_slug_for_dir" in text, f"{name} no longer calls the shared derivation"
        assert "s#^(git@|https://)" not in text, (
            f"{name} re-inlined the prefix-strip sed the helper replaced")


def test_deploy_verify_reaches_a_verdict_with_world_dir_set(tmp_path):
    """WIRING, not function (guard-1943).

    test_both_production_scripts_use_the_shared_helper above is a TEXT grep. It
    stayed green while deploy-verify.sh died rc=1 on its commonest call shape,
    because `. "$_dv_dir/_repo_slug.sh"` was added at a point where _dv_dir was in
    scope ONLY when WORLD_DIR was unset. Every test rig and every caller that had
    already sourced _paths.sh sets it, so `set -u` killed the script before any
    verdict -- 14 tests red across the two deploy-verify suites, caught by the
    regression set and not by this file.

    So: run the real script the real way, with WORLD_DIR set, and assert it
    reaches a JSON verdict carrying the derived slug. gh is stubbed to fail, so
    the verdict is `unverified`; that is not what is pinned here. What is pinned
    is that the source line cannot abort the script before it answers.
    """
    d = _mkrepo(tmp_path, [("origin", "https://github.com/acme/widget-service.git")])
    subprocess.run(["git", "-C", str(d), "-c", "user.email=t@e.invalid",
                    "-c", "user.name=t", "commit", "-qm", "init", "--allow-empty"],
                   check=True)
    world = tmp_path / "world"
    (world / "scripts").mkdir(parents=True)
    ghstub = tmp_path / "gh-fail.sh"
    ghstub.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    ghstub.chmod(0o755)

    env = dict(os.environ)
    env.update({"WORLD_DIR": str(world), "GH_BIN": str(ghstub), "STORAGE_BACKEND": "local"})
    r = subprocess.run([BASH, (_SCRIPTS / "deploy-verify.sh").as_posix(), "--dir", str(d),
                        "--timeout-mins", "1", "--poll-secs", "1", "--grace-secs", "1"],
                       capture_output=True, text=True, env=env, timeout=180)

    assert "unbound variable" not in r.stderr, r.stderr
    lines = [l for l in r.stdout.splitlines() if l.strip().startswith("{")]
    assert lines, f"no JSON verdict; rc={r.returncode} stderr={r.stderr}"
    assert json.loads(lines[-1]).get("repo") == "acme/widget-service"
