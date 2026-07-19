"""test_evil_merge_detector.py — evil-merge detector (4).

Golden replay of the rb-3692 incident SHAPE: mainline adds a gate-paired
feature line (the XPayloadProvenance stamp) to a framework builder; a side
branch holds the stale pre-stamp version; the merge resolves by taking the
stale side — the stamp line vanishes from the result although NO branch
deleted it relative to the merge-base. Pickaxe (`git log -S`) skips merges,
so only result-vs-parents algebra sees it.

NOTE on the live incident: the actual 2026-07-16 strip commit (886814794)
lives in the DOWNSTREAM transplant repo (audited via GitHub API in
g-115-2386) and was a wholesale-overwrite transplant commit — this repo's
history holds no replayable copy, so the golden input is synthesized here
with the exact merge-resolution shape the goal targets.

Cases:
  A  stale-side merge drops mainline's stamp addition -> flagged
     (class p1-addition-dropped, CamelCase token hit)
  B  healthy merge of the same topology (union resolution) -> zero flags
  C  legit branch deletion honored by the merge -> zero flags
     (line in base+P1, deleted by P2's branch — the everyday shape)
  D  both-parents drop (resolution deletes a line both sides kept) -> flagged
  E  non-framework file drops are ignored (path scope)
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
_spec = importlib.util.spec_from_file_location(
    "evil_merge_detector", str(CORE_SCRIPTS / "evil-merge-detector.py"))
emd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emd)

STAMP = 'payload["XPayloadProvenance"] = "notify-build-payload g-115-2186"'


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), check=True,
                          capture_output=True, text=True).stdout


def _init(repo: Path) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "emd-test")


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _commit_all(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


BUILDER = "core/scripts/notify-build-payload.py"
BASE_BODY = "def build_payload(agent):\n    payload = {}\n    return payload\n"
STAMPED_BODY = ("def build_payload(agent):\n    payload = {}\n"
                f"    {STAMP}\n    return payload\n")


def _build_incident_repo(tmp: Path, *, resolution: str) -> Path:
    """Base -> side branch (stale) -> mainline adds STAMP -> merge side.

    resolution='stale'  : merge takes the side branch's file (drops the stamp)
    resolution='union'  : merge keeps the stamped mainline file (healthy)
    """
    repo = tmp / f"repo-{resolution}"
    _init(repo)
    _write(repo, BUILDER, BASE_BODY)
    _commit_all(repo, "base builder")
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo, "core/scripts/other.py", "# side work\n")
    _commit_all(repo, "side: unrelated work")
    _git(repo, "checkout", "-q", "main")
    _write(repo, BUILDER, STAMPED_BODY)
    _commit_all(repo, "feat(g-115-2186): provenance stamp")
    # merge side INTO main with a manual resolution
    _git(repo, "merge", "--no-ff", "--no-commit", "-q", "side")
    if resolution == "stale":
        _write(repo, BUILDER, BASE_BODY)  # take the stale shape — the strip
        _git(repo, "add", BUILDER)
    _git(repo, "commit", "-q", "-m", "merge side")
    return repo


def test_stale_side_merge_flags_dropped_stamp(tmp_path):
    repo = _build_incident_repo(tmp_path, resolution="stale")
    report = emd.scan(repo, ["HEAD"])
    assert report["merges_scanned"] == 1
    assert report["merges_flagged"] == 1, report
    drops = report["flagged"][0]["flags"][0]["dropped"]
    stamp_hits = [d for d in drops if STAMP.strip() == d["line"]]
    assert stamp_hits, f"stamp line not flagged: {drops}"
    assert stamp_hits[0]["class"] == "p1-addition-dropped"
    # CamelCase matcher anchors at the first [A-Z][a-z]+ hump, so the captured
    # token is "PayloadProvenance" (the leading single-cap X sits outside).
    assert any("PayloadProvenance" in t for t in stamp_hits[0]["tokens"])


def test_healthy_merge_zero_flags(tmp_path):
    repo = _build_incident_repo(tmp_path, resolution="union")
    report = emd.scan(repo, ["HEAD"])
    assert report["merges_scanned"] == 1
    assert report["merges_flagged"] == 0, report


def test_branch_deletion_honored_not_flagged(tmp_path):
    """P2's branch deliberately deletes a token line; the merge honors it."""
    repo = tmp_path / "repo-del"
    _init(repo)
    _write(repo, BUILDER, STAMPED_BODY)
    _commit_all(repo, "base with stamp")
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo, BUILDER, BASE_BODY)  # side deliberately removes the stamp
    _commit_all(repo, "side: retire stamp deliberately")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "core/scripts/other.py", "# mainline work\n")
    _commit_all(repo, "mainline: unrelated")
    _git(repo, "merge", "--no-ff", "-q", "-m", "merge side", "side")
    report = emd.scan(repo, ["HEAD"])
    assert report["merges_scanned"] == 1
    assert report["merges_flagged"] == 0, report


def test_both_parents_drop_flagged(tmp_path):
    """Resolution deletes a line BOTH sides still carried."""
    repo = tmp_path / "repo-both"
    _init(repo)
    _write(repo, BUILDER, STAMPED_BODY)
    _commit_all(repo, "base with stamp")
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo, "core/scripts/other.py", "# side\n")
    _commit_all(repo, "side work")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "core/config/x.yaml", "k: v\n")
    _commit_all(repo, "main work")
    _git(repo, "merge", "--no-ff", "--no-commit", "-q", "side")
    _write(repo, BUILDER, BASE_BODY)  # resolution strips a both-sides line
    _git(repo, "add", BUILDER)
    _git(repo, "commit", "-q", "-m", "merge side")
    report = emd.scan(repo, ["HEAD"])
    assert report["merges_flagged"] == 1, report
    drops = report["flagged"][0]["flags"][0]["dropped"]
    assert any(d["class"] == "both-parents-drop" and STAMP.strip() == d["line"]
               for d in drops), drops


def test_non_framework_paths_ignored(tmp_path):
    repo = tmp_path / "repo-scope"
    _init(repo)
    _write(repo, "app/module.py", f"x = 1\n{STAMP}\n")
    _commit_all(repo, "base app file")
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo, "app/other.py", "# side\n")
    _commit_all(repo, "side work")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "app/module.py", f"x = 2\n{STAMP}\n")
    _commit_all(repo, "main work")
    _git(repo, "merge", "--no-ff", "--no-commit", "-q", "side")
    _write(repo, "app/module.py", "x = 1\n")  # drop outside framework scope
    _git(repo, "add", "app/module.py")
    _git(repo, "commit", "-q", "-m", "merge side")
    report = emd.scan(repo, ["HEAD"])
    assert report["merges_flagged"] == 0, report


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
