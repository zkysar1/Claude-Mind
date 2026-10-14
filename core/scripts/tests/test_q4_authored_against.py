"""--authored-against: the non-git twin of --authored-since ().

A fold, or any edit of a file git does not track (a knowledge node), carries text
the editor deliberately did not re-derive. --authored-since cannot scope such an
artifact: `git diff` lists no untracked path, so it reads zero authored lines
(guard-6919). --authored-against diffs the artifact against a baseline copy, the
byte-verified pre-fold copy the archive step already makes.

DELIBERATELY A SEPARATE FILE from test_q4_authored_scope.py and
test_q4_provenance_sample.py, for the reason the former states at its top.
"""
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "q4_cli_authored_against", SCRIPTS / "q4-provenance-sample.py")
q4_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(q4_cli)

# The two-cluster shape test_q4_authored_scope.py documents: a fact line needs an
# ENTITY and an assertion verb, and the claims sit far apart so they form
# SEPARATE clusters. OLD stands in for carried text, NEW for the editor's own line.
OLD = "The OLD claim: LogCollector was reported at 4210 sessions in 2024.\n"
NEW = "The NEW claim: Router was reported at 99 percent uptime in 2026.\n"
BLANKS = "\n\n\n"
OLD_LINE, NEW_LINE = 1, 5


def _run(tmp_path, capsys, baseline_text, artifact_text, extra=()):
    art = tmp_path / "artifact.md"
    art.write_text(artifact_text, encoding="utf-8")
    argv = ["--goal", "g-115-9664", "--artifact", str(art), "-n", "10", "--json",
            "--session-id", "no-such-session"]
    if baseline_text is not None:
        base = tmp_path / "baseline.md"
        base.write_text(baseline_text, encoding="utf-8")
        argv += ["--authored-against", str(base)]
    rc = q4_cli.main(argv + list(extra))
    out = capsys.readouterr()
    res = json.loads(out.out) if out.out.lstrip().startswith("{") else None
    return rc, res, out.err


def _lines(res):
    return {f["start_line"] for f in res["findings"]}


def test_POSITIVE_CONTROL_unscoped_both_lines_are_graded(tmp_path, capsys):
    """Non-vacuity: if the unscoped run graded only one line, the scoping tests
    below would pass while proving nothing (guard-2421)."""
    rc, res, _ = _run(tmp_path, capsys, None, OLD + BLANKS + NEW)
    assert rc == 1
    assert {OLD_LINE, NEW_LINE} <= _lines(res)


def test_carried_line_is_not_graded_and_the_authored_line_still_is(tmp_path, capsys):
    rc, res, _ = _run(tmp_path, capsys, OLD, OLD + BLANKS + NEW)
    assert OLD_LINE not in _lines(res), "text the baseline already holds is carried"
    assert NEW_LINE in _lines(res), "scoping, not alarm suppression (guard-1901)"
    assert rc == 1
    assert res["authored_scoped"][0]["authored_lines"] == 4


def test_an_EDITED_carried_line_counts_as_authored(tmp_path, capsys):
    """Editing another author's line to pass the check must not launder it."""
    edited = OLD.replace("4210", "4211")
    _rc, res, _ = _run(tmp_path, capsys, OLD, edited + BLANKS + NEW)
    assert OLD_LINE in _lines(res)


def test_nothing_authored_is_skipped_and_is_not_reported_as_a_pass(tmp_path, capsys):
    text = OLD + BLANKS + NEW
    rc, res, _ = _run(tmp_path, capsys, text, text)
    assert rc == 0
    assert res["verdict"] == "skipped"
    assert "AUTHORED lines" in (res["skip_reason"] or "")


def test_refuses_to_combine_with_authored_since(tmp_path, capsys):
    rc, res, err = _run(tmp_path, capsys, OLD, OLD + BLANKS + NEW,
                        extra=["--authored-since", "HEAD"])
    assert rc == 2 and res is None
    assert "exactly one" in err


def test_refuses_more_than_one_artifact(tmp_path, capsys):
    a, b, base = (tmp_path / n for n in ("a.md", "b.md", "base.md"))
    for p in (a, b, base):
        p.write_text(OLD, encoding="utf-8")
    rc = q4_cli.main(["--goal", "g-115-9664", "--artifact", str(a),
                      "--artifact", str(b), "--authored-against", str(base)])
    assert rc == 2
    assert "exactly one" in capsys.readouterr().err


def test_an_unreadable_baseline_fails_loud_rather_than_grading_the_whole_file(
        tmp_path, capsys):
    art = tmp_path / "artifact.md"
    art.write_text(OLD, encoding="utf-8")
    rc = q4_cli.main(["--goal", "g-115-9664", "--artifact", str(art),
                      "--authored-against", str(tmp_path / "no-such-baseline.md")])
    assert rc == 2
    assert "cannot read" in capsys.readouterr().err
