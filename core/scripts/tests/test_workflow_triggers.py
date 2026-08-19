"""Regression tests for core/scripts/workflow_triggers.py ().

Each test pins a SPECIFIC failure mode that would silently return the wrong
answer in production, not a happy path:

  * the YAML 1.1 bool-key trap (`on:` loads as True) — without the fallback the
    module reports zero triggers for every real workflow file ever written, which
    reads as a clean negative and makes product-pr-flow merge unverified branches
  * base-branch matching — treating a `branches: [main]` workflow as "a run is
    expected" for a PR based elsewhere would hang the flow for its whole timeout
  * unparseable-is-not-absent — a caller must be able to tell "no trigger" from
    "I could not read the declaration" (guard-1760)
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from workflow_triggers import declares_trigger  # noqa: E402

MODULE = os.path.join(os.path.dirname(__file__), "..", "workflow_triggers.py")


def _wf(tmp_path, name, body):
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    return str(d)


# --------------------------------------------------------------------------
# The YAML 1.1 bool-key trap. This is the test that matters most: delete the
# `doc.get(True)` fallback in _on_block and this one goes red while every other
# test that uses quoted "on" keeps passing.
# --------------------------------------------------------------------------


def test_bare_on_key_parses_as_bool_and_is_still_found(tmp_path):
    d = _wf(
        tmp_path,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n  t:\n    runs-on: ubuntu-latest\n",
    )
    r = declares_trigger(d, "pull_request", "main")
    assert r["declared"] is True, "bare `on:` is YAML 1.1 True — the bool fallback is missing"
    assert r["matches"][0]["file"] == "ci.yml"


def test_quoted_on_key_also_works(tmp_path):
    d = _wf(tmp_path, "ci.yml", '"on":\n  pull_request:\n    branches: [main]\n')
    assert declares_trigger(d, "pull_request", "main")["declared"] is True


# --------------------------------------------------------------------------
# The three shapes `on:` can take.
# --------------------------------------------------------------------------


def test_scalar_form(tmp_path):
    d = _wf(tmp_path, "a.yml", "on: pull_request\n")
    assert declares_trigger(d, "pull_request", "anything")["declared"] is True


def test_list_form(tmp_path):
    d = _wf(tmp_path, "a.yml", "on: [push, pull_request]\n")
    assert declares_trigger(d, "pull_request", "main")["declared"] is True


def test_mapping_form_without_branches_matches_any_ref(tmp_path):
    d = _wf(tmp_path, "a.yml", "on:\n  pull_request:\n")
    assert declares_trigger(d, "pull_request", "some/odd-branch")["declared"] is True


# --------------------------------------------------------------------------
# Base-branch matching ('s stacked-PR finding).
# --------------------------------------------------------------------------


def test_branch_filter_matches_declared_base(tmp_path):
    d = _wf(tmp_path, "a.yml", "on:\n  pull_request:\n    branches: [main]\n")
    assert declares_trigger(d, "pull_request", "main")["declared"] is True


def test_branch_filter_does_not_match_other_base(tmp_path):
    d = _wf(tmp_path, "a.yml", "on:\n  pull_request:\n    branches: [main]\n")
    r = declares_trigger(d, "pull_request", "release/v2")
    assert r["declared"] is False, "a main-only workflow produces NO run for a PR based elsewhere"


def test_glob_branch_pattern(tmp_path):
    d = _wf(tmp_path, "a.yml", "on:\n  pull_request:\n    branches: ['releases/**']\n")
    assert declares_trigger(d, "pull_request", "releases/v3")["declared"] is True
    assert declares_trigger(d, "pull_request", "main")["declared"] is False


def test_branches_ignore_excludes_the_ref(tmp_path):
    d = _wf(tmp_path, "a.yml", "on:\n  pull_request:\n    branches-ignore: [main]\n")
    assert declares_trigger(d, "pull_request", "main")["declared"] is False
    assert declares_trigger(d, "pull_request", "feature")["declared"] is True


# --------------------------------------------------------------------------
# The no_ci path that MUST be preserved — 49 of 56 repos depend on it.
# --------------------------------------------------------------------------


def test_push_only_workflow_is_not_declared(tmp_path):
    d = _wf(tmp_path, "main.yml", "on:\n  push:\n    branches: [main]\n")
    assert declares_trigger(d, "pull_request", "main")["declared"] is False


def test_empty_workflow_dir_is_not_declared(tmp_path):
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    r = declares_trigger(str(d), "pull_request", "main")
    assert r["declared"] is False and r["files_scanned"] == 0


def test_yaml_extension_is_scanned_too(tmp_path):
    d = _wf(tmp_path, "a.yaml", "on:\n  pull_request:\n    branches: [main]\n")
    assert declares_trigger(d, "pull_request", "main")["declared"] is True


# --------------------------------------------------------------------------
# Unparseable is NOT absent (guard-1760).
# --------------------------------------------------------------------------


def test_unparseable_file_is_reported_not_silently_skipped(tmp_path):
    d = _wf(tmp_path, "broken.yml", "on:\n  pull_request:\n   - [unclosed\n")
    r = declares_trigger(d, "pull_request", "main")
    assert r["unparseable"], "an unreadable declaration must be reported, never dropped"
    assert r["declared"] is False


def test_unparseable_does_not_mask_a_real_match(tmp_path):
    _wf(tmp_path, "broken.yml", "on:\n  pull_request:\n   - [unclosed\n")
    d = _wf(tmp_path, "good.yml", "on:\n  pull_request:\n    branches: [main]\n")
    r = declares_trigger(d, "pull_request", "main")
    assert r["declared"] is True and r["unparseable"]


# --------------------------------------------------------------------------
# CLI contract — product-pr-flow.sh branches on these exit codes.
# --------------------------------------------------------------------------


def _cli(root, ref, event="pull_request"):
    return subprocess.run(
        [sys.executable, MODULE, "--repo-root", str(root), "--event", event, "--ref", ref, "--json"],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_exit_zero_when_declared(tmp_path):
    _wf(tmp_path, "ci.yml", "on:\n  pull_request:\n    branches: [main]\n")
    p = _cli(tmp_path, "main")
    assert p.returncode == 0
    assert json.loads(p.stdout)["declared"] is True


def test_cli_exit_one_when_not_declared(tmp_path):
    _wf(tmp_path, "ci.yml", "on:\n  push:\n    branches: [main]\n")
    p = _cli(tmp_path, "main")
    assert p.returncode == 1
    assert json.loads(p.stdout)["declared"] is False


def test_cli_exit_one_when_no_workflows_dir(tmp_path):
    p = _cli(tmp_path, "main")
    assert p.returncode == 1
    assert json.loads(p.stdout)["detail"] == "no workflows directory"


def test_cli_exit_two_when_unknowable(tmp_path):
    _wf(tmp_path, "broken.yml", "on:\n  pull_request:\n   - [unclosed\n")
    p = _cli(tmp_path, "main")
    assert p.returncode == 2, "unparseable-and-no-match must NOT look like a clean not-declared"


# --------------------------------------------------------------------------
# An internal error must not be indistinguishable from a clean negative.
# Python exits 1 on an uncaught exception and 1 is this CLI's "not declared"
# verdict, so without the guard a crash reads to product-pr-flow.sh as "no run
# is expected" and it merges immediately -- silently restoring the very race the
# module closes. Found by the post-close fresh-eyes review, not by the original
# tests, which only ever exercised well-formed inputs.
# --------------------------------------------------------------------------


def test_internal_error_degrades_to_unknown_not_absent(tmp_path, monkeypatch):
    import workflow_triggers as wt

    _wf(tmp_path, "ci.yml", "on:\n  pull_request:\n    branches: [main]\n")

    def boom(*a, **k):
        raise RuntimeError("simulated internal error")

    monkeypatch.setattr(wt, "declares_trigger", boom)
    rc = wt._main(["--repo-root", str(tmp_path), "--event", "pull_request", "--ref", "main", "--json"])
    assert rc == 2, "a crash must degrade to unknown(2); rc=1 would read as a clean not-declared"


def test_the_three_verdicts_are_distinct(tmp_path):
    """0 / 1 / 2 must never collide -- the caller branches on exactly these."""
    import workflow_triggers as wt

    _wf(tmp_path, "ci.yml", "on:\n  pull_request:\n    branches: [main]\n")
    assert wt._main(["--repo-root", str(tmp_path), "--event", "pull_request", "--ref", "main"]) == 0
    assert wt._main(["--repo-root", str(tmp_path), "--event", "pull_request", "--ref", "other"]) == 1
    _wf(tmp_path, "broken.yml", "on:\n  pull_request:\n   - [unclosed\n")
    assert wt._main(["--repo-root", str(tmp_path), "--event", "pull_request", "--ref", "other"]) == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
