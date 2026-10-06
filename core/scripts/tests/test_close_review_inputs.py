"""close-review-inputs.py builds a close review's inputs from the goal itself ().

Reviewers built --source-file and --artifact-file by hand for every review, and one hand
build came out WRONG: the commit search found nothing and the fallback showed an unrelated
merge. These tests pin the parts a hand build gets wrong.

Design notes:

* The fixture is a real bare origin with three clones, as in
  test_fresh_eyes_close_delivery.py: a seed that owns main, a worker that pushes only to
  refs/workers/**, and the reviewer's clone the script runs in. Nothing below git is
  stubbed; only the goal lookup is, because the record lives in the world store.
* main() runs in-process with the argument shape a reviewer types (guard-920).
* Each exclusion has a control that shows the pin can fail: the digit guard, the
  sub-goal guard, refs/stash, a body-only mention, a diff's index lines, the fetch, a
  merge's cherry lines, a separator byte inside a message.
* The fidelity and q4 tests read their expected values from the components that consume
  these files, close-review-verdict.py and q4_provenance_sample.py (guard-1220).
"""
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cri = _load("close_review_inputs_under_test", "close-review-inputs.py")

GOAL = "g-9-10"
SID = "9c5cc235-0000-4000-8000-000000000002"


def _git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 0, (args, p.stderr)
    return p.stdout.strip()


def _identity(repo):
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")


def _clone(origin, path):
    subprocess.run(["git", "clone", "-q", str(origin), str(path)], check=True)
    _identity(path)
    return path


def _commit(repo, name, body, message):
    (Path(repo) / name).write_text(body)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def box(tmp_path):
    """A fresh origin per test, holding one commit of every kind the script must sort."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    _identity(seed)
    _git(seed, "remote", "add", "origin", str(origin))
    s = {"base": _commit(seed, "app.txt", "one\n", "base")}
    s["landed"] = _commit(seed, "app.txt", "one\nlanded line\n",
                          f"feat({GOAL}): the landed change")
    s["longer"] = _commit(seed, "longer.txt", "x\n", "feat(g-9-100): a longer goal id")
    s["subgoal"] = _commit(seed, "sub.txt", "x\n", f"feat({GOAL}-a): a lettered sub-goal")
    s["cites"] = _commit(seed, "other.txt", "x\n",
                         f"chore(g-9-11): other work\n\nFollows the approach of {GOAL}.")
    # A change carried onto main by cherry-pick: the original stays on a pushed branch.
    _git(seed, "checkout", "-q", "-b", "carry", s["base"])
    s["carried"] = _commit(seed, "carry.txt", "carried\n", f"fix({GOAL}): the carried change")
    _git(seed, "push", "-q", "origin", "carry")
    _git(seed, "checkout", "-q", "main")
    _git(seed, "cherry-pick", "-x", s["carried"])
    s["carry_copy"] = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "-q", "origin", "main")

    worker = _clone(origin, tmp_path / "worker")
    s["worker"] = _commit(worker, "app.txt", "one\nlanded line\nworker line\n",
                          f"fix({GOAL}): the worker change")
    _git(worker, "push", "-q", "origin", f"HEAD:refs/workers/alpha/{SID}")

    reviewer = _clone(origin, tmp_path / "reviewer")
    _git(reviewer, "checkout", "-q", "-b", "side")
    s["local"] = _commit(reviewer, "local.txt", "x\n", f"test({GOAL}): only in this clone")
    _git(reviewer, "checkout", "-q", "main")
    (reviewer / "other.txt").write_text("churn\n")
    _git(reviewer, "stash", "push", "-q", "-m", f"churn while on {GOAL}")
    s["stash"] = _git(reviewer, "rev-parse", "refs/stash")
    return {"reviewer": reviewer, "shas": s, "out": tmp_path / "out"}


def _goal(**extra):
    g = {"id": GOAL, "title": "Fix the widget", "description": "Make the widget stop at "
         "rb-9-1 and keep guard-9-2.", "verification": {"outcomes": ["the widget stops"],
         "checks": ["test -f app.txt"]}, "outcome_note": "OUTCOME 1: MET. Follows rb-9-1 "
         "and guard-9-2.", "completed_by": "bravo"}
    g.update(extra)
    return g


def _run(box, monkeypatch, capsys, goal=None, args=(), gid=GOAL):
    monkeypatch.setattr(cri, "load_goal", lambda _gid: dict(goal) if goal is not None else {})
    rc = cri.main(["--goal", gid, "--out-dir", str(box["out"]),
                   "--repo", str(box["reviewer"]), *args])
    out = capsys.readouterr().out
    src = box["out"] / f"{gid}-source.txt"
    art = box["out"] / f"{gid}-artifact.txt"
    return (rc, out, src.read_text() if src.exists() else None,
            art.read_text() if art.exists() else None)


def _commit_lines(out):
    """{sha12: rest of line} for every COMMIT line."""
    return {m.group(1): m.group(2) for m in re.finditer(r"^COMMIT (\w{12}) (.*)$", out, re.M)}


def test_own_commits_are_carried_and_their_neighbours_are_not(box, monkeypatch, capsys):
    rc, out, _src, art = _run(box, monkeypatch, capsys, _goal())
    assert rc == cri.EXIT_OK
    s = box["shas"]
    for key in ("landed", "worker", "carried", "carry_copy", "local"):
        assert s[key] in art, key
    for key in ("longer", "subgoal", "cites", "stash", "base"):
        assert s[key] not in art, key
    assert "landed line" in art and "worker line" in art
    # control: the neighbours do match a looser search, so the guards above are doing work
    loose = _git(box["reviewer"], "log", "--all", "--format=%H", f"--grep={GOAL}")
    for key in ("longer", "subgoal", "cites", "stash"):
        assert s[key] in loose, key


def test_each_own_commit_prints_where_it_stands_against_main(box, monkeypatch, capsys):
    rc, out, _src, _art = _run(box, monkeypatch, capsys, _goal())
    lines, s = _commit_lines(out), box["shas"]
    assert lines[s["landed"][:12]].startswith("LANDED |")
    assert lines[s["carry_copy"][:12]].startswith("LANDED |")
    assert lines[s["worker"][:12]].startswith(
        "STRANDED_WORKER_REF patch-equivalent-on-target=no")
    assert lines[s["local"][:12]].startswith(
        "STRANDED_LOCAL_ONLY patch-equivalent-on-target=no")
    # a carry lands the same change under a new sha: only the patch check shows it
    assert lines[s["carried"][:12]].startswith(
        "STRANDED_REMOTE_BRANCH patch-equivalent-on-target=yes")
    assert out.splitlines()[0] == "FETCH ok"


def test_a_merge_reads_unknown_rather_than_its_second_parents_cherry_line(
        box, monkeypatch, capsys):
    """Over `M^..M` git cherry lists the commits a merge's second parent brought in that the
    target lacks, and skips the merge itself, so its first line answers for another commit."""
    repo, s = box["reviewer"], box["shas"]
    # a feature commit with main's "landed" patch under a new sha and a subject of its own
    _git(repo, "checkout", "-q", "-b", "feature", s["base"])
    _git(repo, "cherry-pick", "-n", s["landed"])
    _git(repo, "commit", "-q", "-m", "feat: the same change on a feature branch")
    _git(repo, "checkout", "-q", "-b", "mside", s["base"])
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", f"merge({GOAL}): bring the feature in")
    merge = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    # control: cherry's only line is the feature commit, '-' because main has its patch, so
    # reading the first line would call this local-only merge patch-equivalent on the target
    lines = _git(repo, "cherry", "origin/main", merge, f"{merge}^").splitlines()
    assert len(lines) == 1 and lines[0].startswith("- ") and merge not in lines[0], lines
    _rc, out, _src, _art = _run(box, monkeypatch, capsys, _goal(), args=("--no-fetch",))
    assert "patch-equivalent-on-target=unknown |" in _commit_lines(out)[merge[:12]]


def test_a_separator_byte_in_a_message_neither_drops_nor_demotes_a_commit(
        box, monkeypatch, capsys):
    """Git refuses NUL in a message and allows every other control byte, so the log is read
    NUL-terminated: a 0x1f or 0x1e delimiter would split both of these records."""
    repo = box["reviewer"]
    _git(repo, "checkout", "-q", "-b", "odd")
    us = _commit(repo, "us.txt", "x\n", f"fix: first half\x1f({GOAL}) second half")
    rs = _commit(repo, "rs.txt", "x\n", f"chore(g-9-13): other\n\nintro\x1e then cites {GOAL}")
    _git(repo, "checkout", "-q", "main")
    # control: the bytes really are inside the messages
    assert "\x1f" in _git(repo, "log", "-1", "--format=%s", us)
    assert "\x1e" in _git(repo, "log", "-1", "--format=%b", rs)
    _rc, out, _src, art = _run(box, monkeypatch, capsys, _goal(), args=("--no-fetch",))
    assert us[:12] in _commit_lines(out) and us in art
    cites = [ln for ln in out.splitlines() if ln.startswith("CITES ")]
    assert any(f"newest {rs[:12]}" in ln for ln in cites), cites
    assert rs not in art


def test_without_the_fetch_a_worker_commit_is_not_seen(box, monkeypatch, capsys):
    # The control for the fetch: a default clone carries no worker refs.
    _rc, out, _src, art = _run(box, monkeypatch, capsys, _goal(), args=("--no-fetch",))
    assert box["shas"]["worker"][:12] not in _commit_lines(out)
    assert box["shas"]["worker"] not in art
    assert out.splitlines()[0].startswith("FETCH skipped")


def test_a_body_only_mention_is_listed_as_cites_and_left_out(box, monkeypatch, capsys):
    _rc, out, _src, art = _run(box, monkeypatch, capsys, _goal())
    cites = [ln for ln in out.splitlines() if ln.startswith("CITES ")]
    assert len(cites) == 1
    assert f"newest {box['shas']['cites'][:12]}" in cites[0]
    assert "chore(g-9-11): other work" in cites[0]
    assert box["shas"]["cites"][:12] not in _commit_lines(out)
    assert "Follows the approach" not in art


def test_the_record_commit_sha_is_a_breadcrumb_not_a_goal_commit(box, monkeypatch, capsys):
    crumb = box["shas"]["cites"]
    _rc, out, _src, _art = _run(box, monkeypatch, capsys, _goal(commit_sha=crumb))
    line = next(ln for ln in out.splitlines() if ln.startswith("BREADCRUMB "))
    assert line.startswith(f"BREADCRUMB commit_sha={crumb[:12]} LANDED subject-names-goal=False")
    assert crumb[:12] not in _commit_lines(out)
    # positive control: a breadcrumb that is the goal's own commit says so
    own = box["shas"]["landed"]
    _rc, out, _src, _art = _run(box, monkeypatch, capsys, _goal(commit_sha=own))
    assert f"BREADCRUMB commit_sha={own[:12]} LANDED subject-names-goal=True" in out


def test_no_commit_exits_5_with_the_note_alone_and_an_include_releases_it(
        box, monkeypatch, capsys, tmp_path):
    gid = "g-9-12"
    rc, out, _src, art = _run(box, monkeypatch, capsys, _goal(id=gid), gid=gid)
    assert rc == cri.EXIT_NO_COMMIT
    assert f"NO COMMIT names {gid}" in out
    assert f"COMMITS WHOSE SUBJECT NAMES {gid}: 0" in art and "OUTCOME 1: MET." in art
    assert box["shas"]["landed"] not in art  # never another goal's commit as a stand-in
    node = tmp_path / "node.md"
    node.write_text("measured 42 widgets, per rb-9-3\n")
    rc, out, _src, art = _run(box, monkeypatch, capsys, _goal(id=gid), gid=gid,
                              args=("--include", str(node)))
    assert rc == cri.EXIT_OK and "NO COMMIT" not in out
    assert f"INCLUDED FILE {node}:" in art and "measured 42 widgets" in art


def test_a_world_include_resolves_through_paths(box, monkeypatch, capsys, tmp_path):
    node = tmp_path / "world-root" / "knowledge" / "n.md"
    node.parent.mkdir(parents=True)
    node.write_text("tree node text\n")
    stub = type(sys)("_paths")
    stub.resolve_file_path = lambda p: tmp_path / "world-root" / p.split("/", 1)[1]
    monkeypatch.setitem(sys.modules, "_paths", stub)
    rc, out, _src, art = _run(box, monkeypatch, capsys, _goal(),
                              args=("--include", "world/knowledge/n.md"))
    assert rc == cri.EXIT_OK and "tree node text" in art
    # q4 opens the file itself, so its command carries the resolved path, not world/...
    assert f"--artifact {node} " in next(ln for ln in out.splitlines()
                                        if ln.startswith("NEXT bash "))


def test_the_source_is_the_goal_text_without_its_id(box, monkeypatch, capsys):
    _rc, _out, src, _art = _run(box, monkeypatch, capsys, _goal())
    assert "TITLE: Fix the widget" in src and "rb-9-1" in src
    assert "- the widget stops" in src and "- test -f app.txt" in src
    assert GOAL not in src


def test_diff_index_lines_are_dropped_and_the_change_is_kept(box, monkeypatch, capsys):
    _rc, _out, _src, art = _run(box, monkeypatch, capsys, _goal())
    assert not re.search(r"^index [0-9a-f]{7,}\.\.[0-9a-f]{7,}", art, re.M)
    assert "+landed line" in art
    # control: the raw diff does carry an index line
    raw = _git(box["reviewer"], "show", "--no-color", box["shas"]["landed"])
    assert re.search(r"^index [0-9a-f]{7,}\.\.[0-9a-f]{7,}", raw, re.M)


def test_the_fidelity_diff_matches_a_hand_built_pair(box, monkeypatch, capsys):
    """The consumer decides: close-review-verdict.py gives the same diff on the built files
    as on the same material assembled by hand."""
    crv = _load("crv_for_inputs_test", "close-review-verdict.py")
    _rc, _out, src, art = _run(box, monkeypatch, capsys, _goal())
    g = _goal()
    hand_src = f"{g['title']}\n{g['description']}\nthe widget stops\ntest -f app.txt\n"
    hand_art = g["outcome_note"] + "\n" + "\n".join(
        _git(box["reviewer"], "show", "--no-color", "--format=%H%n%B", box["shas"][k])
        for k in ("landed", "worker", "carried", "carry_copy", "local"))
    built = crv.source_fidelity(src, art, goal_id=GOAL)
    hand = crv.source_fidelity(hand_src, hand_art, goal_id=GOAL)
    assert built["missing"] == hand["missing"] == []
    assert built["passed"] is hand["passed"] is True
    # The one intended difference: the hand build keeps the diff index lines, whose blob
    # hashes read as invented ids. The built pair invents nothing the hand pair does not.
    hand_only = set(hand["invented"]) - set(built["invented"])
    index_text = "\n".join(re.findall(r"^index \S+", hand_art, re.M))
    assert set(built["invented"]) <= set(hand["invented"])
    assert hand_only and all(i in index_text for i in hand_only), hand_only


def test_q4_reads_the_outcome_note_alone_and_never_the_commits(box, monkeypatch, capsys,
                                                               tmp_path):
    """q4 grades each claim by its citations, and a commit message is prose that cites
    nothing, so the artifact file fails q4 whatever the closure says. The consumer decides:
    q4's own run() flags the commit prose in the artifact file (the control that shows q4
    fires on this fixture) and finds nothing in the note file it is pointed at."""
    repo = box["reviewer"]
    _git(repo, "checkout", "-q", "-b", "prose")
    prose = _commit(repo, "limit.txt", "x\n", f"docs({GOAL}): note the limit\n\n"
                    "The Widget Service has capped every batch since 2024.")
    _git(repo, "checkout", "-q", "main")
    node = tmp_path / "node.md"
    node.write_text("measured 42 widgets, per rb-9-3\n")
    note_text = ("OUTCOME 1: MET. The Widget Service was measured at 42 percent, per "
                 "docs/widget-limits.md.")
    rc, out, _src, art = _run(box, monkeypatch, capsys, _goal(outcome_note=note_text),
                              args=("--no-fetch", "--include", str(node)))
    note = box["out"] / f"{GOAL}-note.txt"
    assert rc == cri.EXIT_OK and note.read_text() == note_text + "\n"
    assert prose in art and "capped every batch" in art
    q4_line = next(ln for ln in out.splitlines() if ln.startswith("NEXT bash "))
    assert q4_line == (f"NEXT bash core/scripts/q4-provenance-sample.sh --goal {GOAL} "
                       f"--artifact {note} --artifact {node} "
                       f"--source-file {box['out'] / f'{GOAL}-source.txt'} --json")
    q4 = _load("q4_for_inputs_test", "q4_provenance_sample.py")
    # No session manifest here, so q4 skips its decorative test; missing-citation still runs.
    monkeypatch.setattr(q4, "retrieved_predicate", lambda _sid: None)
    monkeypatch.setattr(q4, "expressible_predicate", lambda _sid=None: None)
    on_art = q4.run(GOAL, [str(box["out"] / f"{GOAL}-artifact.txt")], n=50)
    on_note = q4.run(GOAL, [str(note)], n=50)
    assert [f["sample"] for f in on_art["findings"] if f["kind"] == "missing-citation"] == [
        "The Widget Service has capped every batch since 2024."]
    assert on_note["sampled_count"] == 1 and on_note["findings"] == []


def test_refusals_name_their_cause(box, monkeypatch, capsys, tmp_path):
    assert _run(box, monkeypatch, capsys, None)[0] == cri.EXIT_NO_GOAL
    assert _run(box, monkeypatch, capsys, _goal(), gid="G-9-10")[0] == cri.EXIT_USAGE
    rc = _run(box, monkeypatch, capsys, _goal(), args=("--include", str(tmp_path / "nope")))[0]
    assert rc == cri.EXIT_USAGE
    monkeypatch.setattr(cri, "load_goal", lambda _gid: _goal())
    assert cri.main(["--goal", GOAL, "--out-dir", str(box["out"]),
                     "--repo", str(tmp_path)]) == cri.EXIT_NO_REPO


def test_cli_help_runs_from_a_subprocess():
    res = subprocess.run([sys.executable, str(SCRIPTS / "close-review-inputs.py"), "--help"],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0 and "--include" in res.stdout


# --- : a loop commit's agent state, state-only commits, and the size caps ---------
# The agent-state directory comes from the framework constant, as the script takes it
# (agent-dir-resolution.md); conftest puts core/scripts on sys.path.
from _paths import AGENTS_PARENT_DIR  # noqa: E402

STATE = f"{AGENTS_PARENT_DIR}/alpha/experience.jsonl"


def _commit_files(repo, files, message):
    for name, body in files.items():
        p = Path(repo) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _state_diff_bytes(repo, sha):
    """The byte size of one commit's agent-state diff as git prints it: the truth the
    left-out count is held to."""
    p = subprocess.run(["git", "-C", str(repo), "show", "--no-color", "--format=", "--patch",
                        sha, "--", f":(top){AGENTS_PARENT_DIR}/"], capture_output=True,
                       timeout=60)
    assert p.returncode == 0 and p.stdout
    return len(p.stdout)


def test_a_loop_commit_keeps_its_deliverable_and_leaves_its_agent_state_out(
        box, monkeypatch, capsys):
    """rb-12095: a loop commit carries the closer's agent-state churn beside its deliverable,
    and the ids in that churn swamp check 2's fidelity diff in both directions. The goal
    cites g-9-71, which the deliverable never mentions and the churn does."""
    repo = box["reviewer"]
    _git(repo, "checkout", "-q", "-b", "loop")
    # rb ids in the shape check 2's id regex reads (rb-<number>), so rb-951 is one id
    churn = "".join(f'{{"id": "exp-{i}", "goal": "g-9-{70 + i}", "rb": "rb-{950 + i}"}}\n'
                    for i in range(40))
    loop = _commit_files(repo, {STATE: churn, "widget.py": "deliverable line\n"},
                         f"chore({GOAL}): iteration 7 commit")
    _git(repo, "checkout", "-q", "main")
    # positive control (guard-4166): the commit really carries an agent-state diff
    raw = _git(repo, "show", "--no-color", loop)
    assert f"diff --git a/{STATE} b/{STATE}" in raw and "g-9-71" in raw
    goal = _goal(description="Make the widget stop at rb-9-1 and keep guard-9-2, as g-9-71 "
                             "asked.")
    rc, out, src, art = _run(box, monkeypatch, capsys, goal, args=("--no-fetch",))
    assert rc == cri.EXIT_OK
    assert "+deliverable line" in art and loop in art
    assert f"diff --git a/{AGENTS_PARENT_DIR}/" not in art and "rb-951" not in art
    left = [ln for ln in out.splitlines() if ln.startswith(f"LEFT OUT {loop[:12]} ")]
    assert left == [f"LEFT OUT {loop[:12]} 1 file(s), {_state_diff_bytes(repo, loop)} bytes "
                    f"under {AGENTS_PARENT_DIR}/"], left
    # The consumer decides (guard-1220). On the built artifact the cited g-9-71 reads as
    # missing and no churn id reads as invented. With the churn diff put back, the churn
    # hides the miss and invents ids, which is the defect.
    crv = _load("crv_for_state_test", "close-review-verdict.py")
    built = crv.source_fidelity(src, art, goal_id=GOAL)
    assert "g-9-71" in built["missing"] and "rb-951" not in built["invented"]
    churned = crv.source_fidelity(src, art + "\n" + raw, goal_id=GOAL)
    assert "g-9-71" not in churned["missing"] and "rb-951" in churned["invented"]


def test_a_goal_whose_only_own_commit_changes_agent_state_exits_5(box, monkeypatch, capsys):
    """A state-only loop commit is not a deliverable: 's one such commit read exit 0."""
    gid, repo = "g-9-14", box["reviewer"]
    _git(repo, "checkout", "-q", "-b", "state-only")
    sha = _commit_files(repo, {STATE: "a\n"}, f"chore({gid}): iteration 8 commit")
    _git(repo, "checkout", "-q", "main")
    # control: the search does find the commit, so the exit comes from what it changes
    assert sha in _git(repo, "log", "--all", "--format=%H", f"--grep={gid}")
    rc, out, _src, art = _run(box, monkeypatch, capsys, _goal(id=gid), gid=gid,
                              args=("--no-fetch",))
    assert rc == cri.EXIT_NO_COMMIT and f"NO COMMIT names {gid}" in out
    line = next(ln for ln in out.splitlines() if ln.startswith(f"COMMIT {sha[:12]} "))
    assert "state-only" in line
    assert f"COMMITS WHOSE SUBJECT NAMES {gid}: 0" in art and sha not in art


def test_a_goal_whose_only_own_commit_changes_no_file_exits_5(box, monkeypatch, capsys):
    """OUTCOME 2 reads "no path outside" the agent-state directory, and a commit that
    changes no file at all has none either."""
    gid, repo = "g-9-16", box["reviewer"]
    _git(repo, "checkout", "-q", "-b", "empty")
    _git(repo, "commit", "-q", "--allow-empty", "-m", f"chore({gid}): a commit with no change")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    rc, out, _src, art = _run(box, monkeypatch, capsys, _goal(id=gid), gid=gid,
                              args=("--no-fetch",))
    assert rc == cri.EXIT_NO_COMMIT and sha not in art
    line = next(ln for ln in out.splitlines() if ln.startswith(f"COMMIT {sha[:12]} "))
    assert "changes no file: left out of the artifact" in line


def test_a_recurring_goals_hundreds_of_commits_end_in_a_capped_artifact(
        box, monkeypatch, capsys):
    """'s 499 own commits built a 305 MB artifact in memory. The caps are lowered
    here so a fixture can reach them; raising=False lets the unfixed script fail on the size
    assertion rather than on a missing name."""
    gid, repo = "g-9-15", box["reviewer"]
    monkeypatch.setattr(cri, "COMMIT_CAP", 4_000, raising=False)
    monkeypatch.setattr(cri, "ARTIFACT_CAP", 60_000, raising=False)
    _git(repo, "checkout", "-q", "-b", "recurring")
    shas = [_commit_files(repo, {"big.txt": "x" * 30_000 + "\n"}, f"chore({gid}): a large cycle")]
    for i in range(199):
        (repo / "cycle").mkdir(exist_ok=True)
        (repo / "cycle" / f"{i}.txt").write_text(f"cycle {i} line\n" * 120)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"chore({gid}): cycle {i}")
    shas += _git(repo, "log", "--format=%H", "-199", "--reverse").splitlines()
    _git(repo, "checkout", "-q", "main")
    rc, out, _src, art = _run(box, monkeypatch, capsys, _goal(id=gid), gid=gid,
                              args=("--no-fetch",))
    assert rc == cri.EXIT_OK
    assert len(art.encode("utf-8")) <= 60_000
    assert "[close-review-inputs: diff cut at 4000 bytes" in art and shas[0] in art
    m = re.search(r"reached its 60000-byte cap; (\d+) more commit\(s\)", art)
    assert m, art[-400:]
    shown = sum(1 for s in shas if s in art)
    assert 0 < shown < 200 and shown + int(m.group(1)) == 200
    # every own commit still gets its delivery line
    assert len([s for s in shas if f"COMMIT {s[:12]} " in out]) == 200


def test_with_the_legacy_empty_agent_directory_nothing_is_left_out(box, monkeypatch, capsys):
    """An empty AGENTS_PARENT_DIR is the legacy layout, with agent dirs among everything else
    at the root: no prefix separates them, so nothing may be excluded. The control is the
    same commit under the real constant, where its agent-state diff is left out."""
    repo = box["reviewer"]
    _git(repo, "checkout", "-q", "-b", "legacy")
    loop = _commit_files(repo, {STATE: "b\n", "widget.py": "w\n"}, f"chore({GOAL}): cycle")
    _git(repo, "checkout", "-q", "main")
    rc, _out, _src, art = _run(box, monkeypatch, capsys, _goal(), args=("--no-fetch",))
    assert rc == cri.EXIT_OK and loop in art and f"diff --git a/{STATE}" not in art
    monkeypatch.setattr(cri, "AGENTS_PARENT_DIR", "", raising=False)
    rc, out, _src, art = _run(box, monkeypatch, capsys, _goal(), args=("--no-fetch",))
    assert rc == cri.EXIT_OK and loop in art and f"diff --git a/{STATE}" in art
    assert not [ln for ln in out.splitlines() if ln.startswith(f"LEFT OUT {loop[:12]} ")]


def test_a_timeout_timer_firing_after_git_exits_does_not_fail_the_read(box, monkeypatch):
    """The timeout timer can fire in the instant after git exits and before it is
    cancelled. git finished, so its output stands. This stand-in timer fires at cancel
    time, after the wait, which makes that instant certain."""
    class LateTimer:
        def __init__(self, _seconds, fn):
            self.fn = fn

        def start(self):
            pass

        def cancel(self):
            self.fn()

    monkeypatch.setattr(cri.threading, "Timer", LateTimer)
    rc, out, past = cri._git_capped(str(box["reviewer"]), 100, "rev-parse", "HEAD")
    assert (rc, len(out.strip()), past) == (0, 40, 0)
