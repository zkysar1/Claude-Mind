"""Tests for call_shape_census.py (gap-048).

The two failure modes gap-048 names are both MEASURED defects, so each gets a
test that fails if the defense is removed:

  (a) false POSITIVE — usage strings / declarations / docs counted as callers
  (b) false NEGATIVE — a git rc=128 pathspec-magic fatal rendered as a clean
      zero because stderr was swallowed (guard-1926)

Plus the defect this tool shipped with and was caught by its own acceptance
check: the primary input is a CLI FLAG, so a dash-leading identifier must
survive BOTH git's option parser and argparse's.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import call_shape_census as csc  # noqa: E402


# ── (a) classification: the false-POSITIVE direction ────────────────────────

@pytest.mark.parametrize(
    "path,line,expected",
    [
        ("README.md", "run with --since 24h", "doc"),
        ("core/scripts/tests/test_x.py", "cmd = ['--since', '1h']", "test"),
        ("core/scripts/x.py", "# --since is optional", "comment"),
        ("core/scripts/x.sh", "  # pass --since here", "comment"),
        ("core/scripts/x.py", 'ap.add_argument("--since", default=None)', "declaration"),
        ("core/scripts/x.sh", 'echo "Usage: x.sh --since <dur>"', "usage_string"),
        ("core/scripts/x.sh", "  --since <duration>", "usage_string"),
        ("core/scripts/x.sh", "  --since DURATION", "usage_string"),
        ("core/scripts/x.sh", "  --since {dur}", "usage_string"),
        # the live shape: an actual value passed at a call site
        ("core/scripts/x.sh", "bash board-read.sh --since 720h", "live"),
        ("core/scripts/x.py", 'subprocess.run([p, "--since", val])', "live"),
    ],
)
def test_classify_separates_live_calls_from_mentions(path, line, expected):
    assert csc.classify(path, line, "--since") == expected


def test_file_class_is_decided_before_line_shape():
    """A usage banner inside a doc is a doc hit — the ordering is load-bearing
    and documented, so pin it rather than leave it to the next reader."""
    assert csc.classify("docs/x.md", "Usage: x --since <dur>", "--since") == "doc"


def test_test_paths_are_recognised_by_all_three_conventions():
    for p in ("core/scripts/tests/x.py", "tests/x.py", "pkg/x_test.py", "pkg/test_x.py"):
        assert csc._is_test_path(p), p
    assert not csc._is_test_path("core/scripts/contest_helper.py")


# ── (b) the false-NEGATIVE direction: a fatal must never become a zero ──────

def test_pathspec_magic_fatal_raises_and_is_never_a_zero(tmp_path):
    """guard-1926: `git grep -- :!<path>` with unimplemented magic exits 128.
    Under `2>/dev/null` that reads exactly like 'nothing consumes this symbol'."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("x = 1  # --since\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    with pytest.raises(csc.GitGrepFatal) as exc:
        csc.census("--since", tmp_path, [":(weirdmagic)core"])
    assert "128" in str(exc.value)
    assert "pathspec magic" in str(exc.value).lower()


def test_cli_returns_rc_2_and_verdict_fatal_on_a_git_failure(tmp_path, capsys):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("x = 1  # --since\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    rc = csc.main(["-e", "--since", "--repo", str(tmp_path), "--exclude", ":(weirdmagic)core"])
    assert rc == 2
    err = capsys.readouterr().err
    assert json.loads(err)["verdict"] == "FATAL"


def test_stderr_is_captured_not_discarded():
    """The whole mechanism behind failure mode (b) is the /dev/null redirect.
    Assert the source never grows one back."""
    src = Path(csc.__file__).read_text()
    # Check the CODE, not the prose: the module docstring names the hazard
    # verbatim, so a bare substring test matches its own warning. In Python the
    # discard is `subprocess.DEVNULL`, and that is what must never appear.
    assert "DEVNULL" not in src
    assert "capture_output=True" in src
    assert "stderr" in src


# ── the positive control: LATENT vs ABSENT vs LIVE ──────────────────────────

def _repo(tmp_path, files):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def test_latent_when_present_but_no_live_call(tmp_path):
    repo = _repo(tmp_path, {"README.md": "pass --since <dur>\n"})
    r = csc.census("--since", repo)
    assert r["verdict"] == "LATENT"
    assert r["live_calls"] == 0
    # the control MUST have run, and must be non-zero — that is what makes
    # LATENT distinguishable from ABSENT
    assert r["positive_control_total"] == 1


def test_absent_when_identifier_appears_nowhere(tmp_path):
    repo = _repo(tmp_path, {"README.md": "nothing here\n"})
    r = csc.census("--nope", repo)
    assert r["verdict"] == "ABSENT"
    assert r["positive_control_total"] == 0


def test_live_skips_the_control_entirely(tmp_path):
    repo = _repo(tmp_path, {"run.sh": "board-read.sh --since 720h\n"})
    r = csc.census("--since", repo)
    assert r["verdict"] == "LIVE"
    assert r["live_calls"] == 1
    # No zero is being reported, so no control is needed; null records that.
    assert r["positive_control_total"] is None


def test_a_naive_count_would_overstate_the_live_population(tmp_path):
    """The value of the whole tool in one assertion."""
    repo = _repo(
        tmp_path,
        {
            "README.md": "use --since <dur>\n",
            "cli.py": 'ap.add_argument("--since")\n',
            "help.sh": 'echo "Usage: x --since <dur>"\n',
            "run.sh": "x.sh --since 24h\n",
        },
    )
    r = csc.census("--since", repo)
    assert r["total_occurrences"] == 4
    assert r["live_calls"] == 1  # a naive `git grep -c` would say 4


# ── the shipped defect: a dash-leading identifier must survive both parsers ──

@pytest.mark.parametrize(
    "argv",
    [["-e", "--since"], ["-e--since"], ["--identifier=--since"]],
)
def test_all_three_dash_leading_arg_forms_work(tmp_path, capsys, argv):
    repo = _repo(tmp_path, {"run.sh": "x.sh --since 24h\n"})
    rc = csc.main(argv + ["--repo", str(repo), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["identifier"] == "--since"
    assert out["verdict"] == "LIVE"


def test_git_receives_the_pattern_behind_dash_e(tmp_path):
    """Without `-e`, git parses `--since` as its own option and exits 129."""
    src = Path(csc.__file__).read_text()
    assert '"-e", identifier' in src


def test_plain_symbol_still_works_as_a_positional(tmp_path, capsys):
    repo = _repo(tmp_path, {"run.js": "listEnvironmentsVerified()\n"})
    rc = csc.main(["listEnvironmentsVerified", "--repo", str(repo), "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "LIVE"


def test_exported_symbol_split_reproduces_encounter_two(tmp_path, capsys):
    """Encounter 2: listEnvironmentsVerified vs listEnvironments — the narrower
    symbol is a substring of nothing, the broader one matches both, so the
    census must report them at different live counts."""
    repo = _repo(
        tmp_path,
        {
            "a.ts": "import { listEnvironmentsVerified } from './x';\nlistEnvironmentsVerified();\n",
            "b.ts": "listEnvironments();\n",
            "docs.md": "listEnvironments is deprecated\n",
        },
    )
    verified = csc.census("listEnvironmentsVerified", repo)
    plain = csc.census("listEnvironments", repo)
    assert verified["live_calls"] == 2
    # the plain name matches the Verified occurrences too — that overlap is the
    # thing the encounter had to reason about, so pin it rather than hide it
    assert plain["live_calls"] > verified["live_calls"]
    assert plain["counts_by_shape"].get("doc") == 1
