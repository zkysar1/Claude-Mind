#!/usr/bin/env python3
"""Pins for the uncommitted-work refusal a caller reads ().

THE DEFECT: the daemon returned evaluate()'s whole census as the refusal.
Measured 2026-09-23 on a worker Body closing g-370-61: 55,900 bytes. It held
ONE blocking commit, listed 12 times (its repo plus 11 worktrees sharing the
object store), among 629 non-blocking stale SHAs across 48 repos, and no remedy
line. The Body re-ran the close four times, then tried a bare
--override-uncommitted.

THE CONTRACT PINNED HERE:
  1. That shape yields a refusal of at most 2 KB, as the daemon serializes it.
  2. The blocking commit is named ONCE, with its repo, branch and subject, and
     none of the stale SHAs appear.
  3. The remedy fits the kind: a pushed commit is told to merge its PR, and is
     offered the audited "PR #<n> open" override. A local-only commit is told
     to push and is offered NO override, because it has no PR that could be
     open (guard-5593).
  4. Nothing that decides is cut (guard-3416): every dirty file is listed.
  5. What did not block becomes a count, and the full census stays one
     command away.

Hermetic: git is stubbed, so no repository is read. The one exception is the
subject-decoding test at the end, which needs real git to produce real bytes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from gates.uncommitted_work import refusal_body  # noqa: E402

ROOT = "/opt/GitHub/Ayoai"
MAIN = f"{ROOT}/Ayoai-Environment-Server"
SHA = "a434fa57de3c09e1b2f4d5a6b7c8d9e0f1a2b3c4"
BRANCH = "origin/fix/g-370-61-sidecar-listening-probe-flake"
SUBJECT = "fix(g-370-61): wait for the sidecar port instead of a fixed sleep"


def _stale(repo_i, n):
    return [f"{repo_i:04x}{j:036x}" for j in range(n)]


def _finding(repo, stranded=(), stale=(), ref="origin/dev", dirty_tracked=()):
    return {"repo": repo, "default_ref": ref, "dirty_tracked": list(dirty_tracked),
            "stranded_commits": list(stranded), "stale_stranded_commits": list(stale),
            "unattributed_unmerged": [], "content_free_stranded_commits": [],
            "landed_stranded_commits": [], "landed_via": {}, "refspec_complete": True}


def _g37061_result():
    """48 checkouts: the repo and 11 worktrees carry the blocking commit, and
    629 stale SHAs spread over all 48 (at most 20 per repo, as the gate caps)."""
    repos = [_finding(MAIN, [SHA], _stale(0, 13))]
    repos += [_finding(f"{ROOT}/_wt-envserver-{i}", [SHA], _stale(i, 13))
              for i in range(1, 12)]
    repos += [_finding(f"{ROOT}/repo-{i}", [], _stale(i, 14 if i < 17 else 13))
              for i in range(12, 48)]
    assert sum(len(f["stale_stranded_commits"]) for f in repos) == 629
    return {"would_block": True, "dirty_framework_files": [],
            "repo_path": "/opt/ayoai-mind", "goal_id": "g-370-61",
            "override_applied": None,
            "undelivered_framework_files": ["core/scripts/foo.py"],
            "delivery_would_block": False, "body_role": "worker",
            "stranded_repos": repos, "stranded_would_block": True}


def _describe(remote):
    def describe(repo, sha):
        return {"subject": SUBJECT,
                "remote_branches": [BRANCH] if remote else [],
                "local_branches": ["fix/g-370-61-sidecar-listening-probe-flake"]}
    return describe


def _main(repo):
    return MAIN if "/_wt-envserver-" in repo else repo


def _serialized(body):
    """The bytes the daemon sends (server.Response.json)."""
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def test_the_g37061_shape_fits_in_2kb_and_names_the_commit_once():
    result = _g37061_result()
    raw = json.dumps(result).encode("utf-8")
    body = refusal_body("g-370-61", result, describe=_describe(True),
                        main_checkout=_main)
    out = _serialized(body)
    assert len(raw) > 20_000, "fixture lost its census bulk"
    assert len(out) <= 2048, f"refusal is {len(out)} bytes"
    text = out.decode("utf-8")
    assert text.count(SHA[:10]) == 1, "the blocking commit must be named once"
    assert not any(s in text for f in result["stranded_repos"]
                   for s in f["stale_stranded_commits"]), "stale SHAs leaked"
    (commit,) = body["blocking"]["commits"]
    assert commit == {"sha": SHA[:10], "repo": MAIN, "checkouts_holding_it": 12,
                      "subject": SUBJECT, "branch": BRANCH, "pushed": True,
                      "must_reach": "origin/dev"}


def test_the_refusal_leads_with_the_verdict_and_ends_the_census_in_counts():
    body = refusal_body("g-370-61", _g37061_result(), describe=_describe(True),
                        main_checkout=_main)
    keys = list(body)
    assert keys[:3] == ["error", "gate", "verdict"]
    assert body["error"] == "uncommitted_work_blocked"
    assert body["verdict"].startswith("REFUSED: g-370-61 status was NOT changed.")
    assert body["not_blocking"] == {"stale_stranded_commits": 629,
                                    "unpushed_framework_files": 1,
                                    "checkouts_with_findings": 48}
    assert body["full_output"] == ("python3 core/scripts/uncommitted-work-gate.py "
                                   "--goal-id g-370-61")
    assert "stranded_repos" not in body["gate_output"]
    assert "undelivered_framework_files" not in body["gate_output"]


def test_a_pushed_commit_is_told_to_merge_and_offered_the_audited_override():
    body = refusal_body("g-370-61", _g37061_result(), describe=_describe(True),
                        main_checkout=_main)
    remedy = " ".join(body["remedy"])
    assert "Merge the PR" in remedy and "origin/dev" in remedy
    assert "A branch with no PR needs one opened first" in remedy
    assert '--override-uncommitted "PR #<n> open"' in remedy
    assert "a bare --override-uncommitted is ignored" in remedy


def test_a_local_only_commit_is_told_to_push_and_offered_no_override():
    result = _g37061_result()
    body = refusal_body("g-370-61", result, describe=_describe(False),
                        main_checkout=_main)
    remedy = " ".join(body["remedy"])
    assert "Push the branch holding each LOCAL-only commit" in remedy
    assert "--override-uncommitted" not in remedy, (
        "a local-only commit has no PR that could be open (guard-5593)")
    assert body["blocking"]["commits"][0]["pushed"] is False


def test_every_dirty_file_is_listed_and_the_raw_minimal_payload_survives():
    """The daemon test's fake has only the legacy keys; guard-3416 says the
    decisive list is never cut, however long."""
    dirty = [f"core/scripts/f{i}.py" for i in range(30)]
    result = {"would_block": True, "dirty_framework_files": dirty,
              "repo_path": "/x", "goal_id": "g-001-50", "override_applied": None}
    body = refusal_body("g-001-50", result)
    assert body["blocking"] == {"dirty_framework_files": dirty}
    assert body["gate_output"] == result
    assert body["not_blocking"] == {}
    assert '--override-uncommitted "<whose they are>"' in " ".join(body["remedy"])


def test_a_blocking_delivery_lists_its_files_in_full():
    undelivered = [f"core/scripts/u{i}.sh" for i in range(25)]
    result = {"would_block": True, "dirty_framework_files": [],
              "undelivered_framework_files": undelivered,
              "delivery_would_block": True, "body_role": "reducer",
              "stranded_repos": [], "stranded_would_block": False}
    body = refusal_body("g-1-1", result)
    assert body["blocking"] == {"undelivered_framework_files": undelivered}
    assert body["gate_output"]["undelivered_framework_files"] == undelivered
    assert "Push the committed framework files" in body["remedy"][0]
    assert "--override-uncommitted" not in " ".join(body["remedy"])


def test_modified_tracked_files_are_listed_per_checkout():
    result = _g37061_result()
    result["stranded_repos"][20]["dirty_tracked"] = ["src/main/App.java"]
    body = refusal_body("g-370-61", result, describe=_describe(True),
                        main_checkout=_main)
    assert body["blocking"]["dirty_tracked"] == {f"{ROOT}/repo-20": ["src/main/App.java"]}
    assert "modified tracked files in 1 checkout(s)" in body["verdict"]


def test_a_non_ascii_subject_survives_a_non_utf8_locale(tmp_path):
    """describe_commit reads free text, so it decodes UTF-8 itself. With the
    locale codec, a Windows daemon outside UTF-8 mode (cp1252) raised on U+201D,
    and the endpoint fell back to the raw census (fresh-eyes finding
    msg-20260923-171952-alpha-3824). The child runs with UTF-8 mode OFF. On
    Linux the locale is UTF-8 anyway, so this bites where the defect lived."""
    subject = "fix: the ”quoted” case — dash"
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                    "-c", "user.email=t@example.invalid", "commit", "-q",
                    "--allow-empty", "-m", subject], check=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["PYTHONUTF8"] = "0"
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "from gates.uncommitted_work import describe_commit; "
            "print(ascii(describe_commit(sys.argv[2], sys.argv[3])['subject']))")
    child = subprocess.run([sys.executable, "-c", code, str(SCRIPT_DIR.parent),
                            str(repo), sha], capture_output=True, text=True,
                           env=env, timeout=60)
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == ascii(subject)
