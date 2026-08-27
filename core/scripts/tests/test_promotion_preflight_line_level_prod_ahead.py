""" — preflight must see LINE-level prod-ahead, not just FILE-level.

WHY THESE TESTS ARE SUBPROCESS-BASED AND MUST STAY THAT WAY
-----------------------------------------------------------
Same reasoning as test_seed_plan_verdict_exit_code.py: the observable that
broke is the PROCESS EXIT CODE. A test that imports promotion-preflight and
calls classify_direction() directly asserts the direction STRING -- which was
never wrong. `source_ahead` is the CORRECT answer to "which side is newer".
The defect is that main() then treats that answer as "safe to overwrite", so
only the exit code of the whole process exposes it.

THE DEFECT (quoted from g-115-4136's measured description, via g-115-4155):
  "promotion-preflight.sh returned exit 0 CLEAN on the SAME pair, because it
  compares at FILE level (reporting 153 source-ahead, safe to overwrite) while
  the plan compares at LINE level."

classify_direction returns on Signal 1 (git recency) BEFORE reading content, so
a source committed more recently is labelled source_ahead and never joins
`blocking` -- even when the target half carries lines a mirror would DELETE.

EXIT VOCABULARY (SSOT: the module docstring of promotion-preflight.py)
  0 = CLEAN   2 = DRIFT   1 = ERROR

guard-2066: every assertion below pins the SPECIFIC code, and each fixture is
built so that code has exactly ONE possible source -- the orphan-risk and
target-ahead buckets are asserted EMPTY, so a `2` cannot arrive by another
route and quietly pass this test for the wrong reason.

guard-1479: no test here passes --strict. promotion-cycle.md prescribes the
bare invocation as the safety check, so a fix reachable only under --strict is
a fix that never runs in production.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "promotion-preflight.py"


def _run(src: Path, tgt: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(src), "--target", str(tgt), *extra],
        capture_output=True, text=True,
    )


def _json(src: Path, tgt: Path, *extra: str) -> dict:
    r = _run(src, tgt, "--json", *extra)
    return json.loads(r.stdout)


def _commit(repo: Path, rel: str, content: str, when: str) -> None:
    """Write + commit with a controlled committer date (drives Signal 1)."""
    env = {**os.environ, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when}
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo),
                       capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo),
                       capture_output=True, check=True)
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    subprocess.run(["git", "add", rel], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", f"add {rel}"], cwd=str(repo),
                   capture_output=True, check=True, env=env)


REL = "core/scripts/widget.sh"
OLD = "2025-01-01T00:00:00"
NEW = "2025-06-01T00:00:00"


def _source_ahead_pair(tmp_path, tgt_body: str, src_body: str = "echo hi\n"):
    """Target committed EARLY, source committed LATE -> Signal 1 = source_ahead."""
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _commit(tgt, REL, tgt_body, OLD)
    _commit(src, REL, src_body, NEW)
    return src, tgt


def test_source_ahead_file_with_target_only_functional_line_blocks_exit_2(tmp_path):
    """THE REGRESSION. Remove the fix and this returns 0, not 2."""
    src, tgt = _source_ahead_pair(tmp_path, "echo hi\necho DOWNSTREAM_ONLY\n")
    d = _json(src, tgt)
    # The direction heuristic still says source_ahead -- we did NOT change it.
    assert REL in d["source_ahead_core"], d["source_ahead_core"]
    # ...and the FILE-level blocking buckets are empty, so a 2 can only come
    # from the new line-level path (guard-2066).
    assert d["orphan_risk_core"] == [], d["orphan_risk_core"]
    assert d["target_ahead_core"] == [], d["target_ahead_core"]
    # The new signal names the file and quotes the line a mirror would delete.
    assert REL in d["line_level_prod_ahead"], d["line_level_prod_ahead"]
    assert any("DOWNSTREAM_ONLY" in ln for ln in d["line_level_prod_ahead"][REL])
    assert d["verdict"] == "DRIFT", d["verdict"]
    assert d["exit"] == 2, d["exit"]
    assert _run(src, tgt).returncode == 2


def test_comment_only_target_line_does_not_block_exit_0(tmp_path):
    """Anti-false-block. A downstream comment/provenance line must NOT block --
    that would regress the g-115-2885 comment-drift excusal. This is the
    two-way proof: without it the test above passes on a gate that blocks on
    ANY difference, which is not a working discriminator (guard-1220)."""
    src, tgt = _source_ahead_pair(tmp_path, "echo hi\n# downstream note, not code\n")
    d = _json(src, tgt)
    assert REL in d["source_ahead_core"], d["source_ahead_core"]
    assert d["line_level_prod_ahead"] == {}, d["line_level_prod_ahead"]
    assert d["verdict"] == "CLEAN", d["verdict"]
    assert _run(src, tgt).returncode == 0


def test_identical_content_stays_clean_exit_0(tmp_path):
    """Positive control: the new check must not fire when nothing differs."""
    src, tgt = _source_ahead_pair(tmp_path, "echo hi\n")
    assert _run(src, tgt).returncode == 0


def test_insert_surrounded_by_a_delete_still_blocks(tmp_path):
    """Both sides carry unique functional lines AND the target ADDS one the
    source never had -- so the file diff is delete+insert, not a clean append.

    MEASURED, not assumed: this fixture lands in source_ahead_core, NOT
    ambiguous_core, because Signal 1 (git recency) fires before the content
    heuristic can call it ambiguous. An earlier docstring here claimed the
    ambiguous lane; probing the live gate refuted it. The ambiguous lane is
    covered by the no-git test below."""
    src, tgt = _source_ahead_pair(
        tmp_path,
        tgt_body="echo shared\necho tail\necho DOWNSTREAM_ADDED\n",
        src_body="echo shared\necho SOURCE_ONLY\necho tail\n",
    )
    d = _json(src, tgt)          # NOTE: no --strict
    assert REL in d["line_level_prod_ahead"], d["line_level_prod_ahead"]
    assert any("DOWNSTREAM_ADDED" in ln for ln in d["line_level_prod_ahead"][REL])
    assert _run(src, tgt).returncode == 2


def test_ambiguous_lane_no_git_blocks_without_strict(tmp_path):
    """The genuinely ambiguous lane: no git on either side, mtimes within the
    60s noise floor, and neither side a strict subset -> classify_direction
    returns 'ambiguous', which blocked ONLY under --strict. promotion-cycle.md
    prescribes the BARE invocation as the safety check, so a downstream addition
    sitting in an ambiguous file was invisible in production (guard-1479)."""
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    for base, body in ((src, "echo shared\necho SOURCE_ONLY\necho tail\n"),
                       (tgt, "echo shared\necho tail\necho DOWNSTREAM_ADDED\n")):
        p = base / REL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    d = _json(src, tgt)          # NOTE: no --strict
    assert REL in d["ambiguous_core"], d["ambiguous_core"]
    assert REL in d["line_level_prod_ahead"], d["line_level_prod_ahead"]
    assert _run(src, tgt).returncode == 2


def test_modified_line_is_not_prod_ahead_exit_0(tmp_path):
    """THE FALSE-POSITIVE THIS DESIGN EXISTS TO AVOID, and the reason the check
    is difflib-insert-only rather than a set difference.

    A version bump is a REPLACE: the target's `0.0.1` line is one the source
    overwrites, not one it deletes. A set-difference implementation flags it,
    and since every promotion bumps a version that makes the gate fire on every
    promotion -- measured here on 2026-08-26, an earlier set-difference build of
    this check turned 18 test_promote.py cases red with
    `mind_api/src/__init__.py  target-only: __version__ = "0.0.1"`. A gate that
    always fires is one people route around with PROMOTE_ALLOW_DRIFT=1, which is
    a worse outcome than the hole it closed."""
    src, tgt = _source_ahead_pair(
        tmp_path,
        tgt_body='__version__ = "0.0.1"\n',
        src_body='__version__ = "1.0.0"\n',
    )
    d = _json(src, tgt)
    assert REL in d["source_ahead_core"], d["source_ahead_core"]
    assert d["line_level_prod_ahead"] == {}, d["line_level_prod_ahead"]
    assert _run(src, tgt).returncode == 0


def test_remedy_is_named_in_text_output(tmp_path):
    """guard-1532: a gate that blocks must name a remediation reachable from the
    state it observed. These files are absent from every FILE-level bucket the
    generic message points at, so without this the reader concludes the verdict
    is spurious and forces past it."""
    src, tgt = _source_ahead_pair(tmp_path, "echo hi\necho DOWNSTREAM_ONLY\n")
    out = _run(src, tgt).stdout
    assert "LINE-level clobber risk" in out, out[-1500:]
    assert "REMEDY: back-port" in out, out[-1500:]
    assert "DOWNSTREAM_ONLY" in out, out[-1500:]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
