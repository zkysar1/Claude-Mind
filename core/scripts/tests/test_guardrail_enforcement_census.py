"""test_guardrail_enforcement_census.py -- .

The census lists every guardrail that carries an owner/user directive with the gate its
`enforced_by` field names, or `honor-system`. A census that miscounts is worse than none:
an inflated population hides the gap in noise and a deflated one hides it outright. So
every rule below is pinned from BOTH sides -- what must count and what must not:

  * population: each user-origin tag counts; a tag naming a rule ABOUT directives does
    not; a speech-act phrase in `source` counts; the bare word "user" in prose does not
    (and is COUNTED as outside the population, so the boundary stays visible);
  * the head arm needs "STANDING" at the very start plus an owner/user/grant/directive
    word in the same sentence's first 40 characters -- plain prose use of the word is out;
  * `enforced_by` shapes: cleared shapes are honor-system, the census's own word is never
    a gate, a wrong type is `malformed` and never crashes;
  * a gate is only `present` for an in-repo relative path (absolute paths, `../` escapes
    and symlinks out of the tree are `unverifiable`; a bare name is never resolved);
  * the cited-by hint ignores tests (a test that cites a rule enforces nothing);
  * a printed census never copies an address- or account-id-shaped value;
  * the wrapper refuses a failed or empty read rather than printing a clean-looking
    census, and does not merge the reader's stderr into the payload (guard-1963).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
for _p in (CORE_SCRIPTS, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import guardrail_enforcement_census as c  # noqa: E402
from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare "bash" argv[0])


def _guard(gid="guard-900", **extra):
    rec = {"id": gid, "rule": "Never do X without first doing Y.", "category": "framework",
           "source": "g-000-00", "status": "active", "tags": []}
    rec.update(extra)
    return rec


@pytest.fixture
def repo(tmp_path):
    """A throwaway repo: one gate-named script citing guard-900, one plain module citing
    guard-903, a TEST citing guard-901, a doc citing guard-902, and an outside file."""
    root = tmp_path / "repo"
    (root / "core/scripts/tests").mkdir(parents=True)
    (root / "core/scripts/__pycache__").mkdir()
    (root / "mind_api/src").mkdir(parents=True)
    (root / "core/scripts/real-gate.py").write_text("# enforces guard-900\n")
    (root / "core/scripts/tests/test_real.py").write_text("# covers guard-901\n")
    (root / "core/scripts/__pycache__/cached.py").write_text("# stale guard-904\n")
    (root / "core/scripts/notes.md").write_text("guard-902 is only prose here\n")
    (root / "mind_api/src/mod.py").write_text("# guard-903 and guard-007\n")
    (root / "core/scripts/other.sh").write_text("# guard-7 again\n")
    (tmp_path / "outside.py").write_text("print('outside the repo')\n")
    os.symlink(tmp_path / "outside.py", root / "core/scripts/link.py")
    return root


# --- population ---------------------------------------------------------------

@pytest.mark.parametrize("tag", sorted(c.USER_TAGS))
def test_each_user_tag_is_a_tag_signal(tag):
    assert c.population_signals(_guard(tags=["unrelated", tag])) == ["tag"]


@pytest.mark.parametrize("tag", [
    "directive-lane", "directive", "directives", "standing-grants", "standing-grant",
    "standing-directive", "directive-lane-compliance", "ownership", "user-facing",
    "user-routing", "notify-user", "capability-before-user", "self-correction"])
def test_tags_about_directives_or_users_are_not_signals(tag):
    # NEGATIVE CONTROL for the tag arm: each of these tags is on live records and names a
    # rule ABOUT directives, grants or users, not an owner's directive itself.
    assert c.population_signals(_guard(tags=[tag])) == []


@pytest.mark.parametrize("source", [
    "g-115-1; user directive 2026-08-23", "USER-DIRECTIVE", "user_correction",
    "owner ruling 2026-09-25", "user-flagged: alpha already", "user granted the agent",
    "user directives", "owner-directed", "user reply 2026-07-01", "user explicitly asked"])
def test_speech_act_in_source_is_a_source_signal(source):
    assert c.population_signals(_guard(source=source)) == ["source"]


@pytest.mark.parametrize("source", [
    "user-interaction", "user-prompt-skill-record.sh", "user ran the new code",
    "user/skill.md l220 confirms", "superuser directive", "g-115-1 fresh-eyes", ""])
def test_bare_user_word_in_source_is_not_a_signal(source):
    assert c.population_signals(_guard(source=source)) == []


@pytest.mark.parametrize("rule", [
    "STANDING OWNER DIRECTIVE: never do X", "STANDING GRANT (grant-007): you may do Y",
    "STANDING USER DIRECTIVE 2026-08-11 (verbatim): do Z"])
def test_standing_head_naming_an_owner_is_a_head_signal(rule):
    assert c.population_signals(_guard(rule=rule)) == ["head"]


@pytest.mark.parametrize("rule", [
    "STANDING no-op: a cadence review that finds nothing is fine",
    "Evidentiary standing requires a measured value",
    "A STANDING OWNER DIRECTIVE that does not open the rule",
    "STANDING " + "x" * 50 + " OWNER",           # keyword beyond the 40-char window
    "STANDING no-op. The OWNER decided otherwise"])  # keyword past a sentence end
def test_standing_as_plain_prose_is_not_a_head_signal(rule):
    assert c.population_signals(_guard(rule=rule)) == []


def test_signals_combine_in_a_fixed_order():
    rec = _guard(tags=["user-directive"], source="user correction", rule="STANDING OWNER DIRECTIVE: x")
    assert c.population_signals(rec) == ["tag", "source", "head"]


def test_non_string_fields_never_crash_the_census():
    odd = _guard(source=123, rule=None, tags="user-directive", id=7, enforced_by=None)
    assert c.population_signals(odd) == []
    row = c.classify(_guard(source=["user directive"], rule=["x"], id=7, tags=["user-correction"]), "/", {})
    assert row["id"] == "" and row["rule_head"] == "" and row["verdict"] == "honor-system"


# --- enforced_by parsing ------------------------------------------------------

@pytest.mark.parametrize("value,gates", [
    (None, []), ("", []), ("   ", []), ([], []),
    ("core/scripts/a-gate.py", ["core/scripts/a-gate.py"]),
    (["core/a.py", "  hook-x  "], ["core/a.py", "hook-x"]),
    ("honor-system", []), (["none", "honour-system", "HONOR-SYSTEM"], [])])
def test_parse_enforced_by_valid_shapes(value, gates):
    assert c.parse_enforced_by(value) == (gates, None)


@pytest.mark.parametrize("value,kind", [
    (5, "int"), (True, "bool"), ({"gate": "x"}, "dict"), (["a", 3], "list"), ([["a"]], "list")])
def test_parse_enforced_by_wrong_type_is_a_problem_not_a_crash(value, kind):
    gates, problem = c.parse_enforced_by(value)
    assert gates == [] and problem == "enforced_by has type " + kind


# --- gate presence ------------------------------------------------------------

@pytest.mark.parametrize("gate", [
    "core/scripts/real-gate.py", "core/scripts/real-gate.py:12",
    "core/scripts/real-gate.py::check_x", "core/scripts/real-gate.py#L10",
    "core/scripts/real-gate.py (Layer B)"])
def test_repo_relative_path_that_exists_is_present(repo, gate):
    assert c.gate_presence(gate, repo) == "present"


def test_repo_relative_path_that_does_not_exist_is_missing(repo):
    assert c.gate_presence("core/scripts/nope.py", repo) == "missing"


@pytest.mark.parametrize("gate", ["real-gate.py", "hook:before-write", "pre-commit"])
def test_bare_names_are_never_resolved(repo, gate):
    # "real-gate.py" EXISTS under core/scripts, and still reads unverifiable: the census
    # does not guess where a bare name lives.
    assert c.gate_presence(gate, repo) == "unverifiable"


def test_absolute_path_is_unverifiable_even_when_it_exists(repo):
    inside = str(repo / "core/scripts/real-gate.py")
    assert os.path.exists(inside)
    assert c.gate_presence(inside, repo) == "unverifiable"


def test_path_climbing_out_of_the_repo_is_unverifiable(repo):
    assert os.path.exists(repo.parent / "outside.py")
    assert c.gate_presence("../outside.py", repo) == "unverifiable"


def test_symlink_pointing_out_of_the_repo_is_unverifiable(repo):
    assert os.path.exists(repo / "core/scripts/link.py")   # the link itself resolves
    assert c.gate_presence("core/scripts/link.py", repo) == "unverifiable"


# --- verdicts -----------------------------------------------------------------

@pytest.mark.parametrize("enforced_by,verdict", [
    (None, "honor-system"), ("", "honor-system"), ([], "honor-system"),
    ("honor-system", "honor-system"),
    ("core/scripts/real-gate.py", "gated"),
    ("hook:before-write", "gated-unverified"),
    ("core/scripts/nope.py", "stale-gate"),
    (["core/scripts/nope.py", "core/scripts/real-gate.py"], "gated"),
    (["core/scripts/nope.py", "hook:before-write"], "gated-unverified"),
    (["core/scripts/nope.py", "core/scripts/also-nope.py"], "stale-gate"),
    (5, "malformed"), (["a", 3], "malformed")])
def test_verdict_for_each_enforced_by_shape(repo, enforced_by, verdict):
    assert c.classify(_guard(enforced_by=enforced_by), repo, {})["verdict"] == verdict


def test_never_set_field_is_honor_system(repo):
    rec = _guard()
    assert "enforced_by" not in rec
    assert c.classify(rec, repo, {})["verdict"] == "honor-system"


def test_not_enforced_is_exactly_the_three_unguarded_verdicts():
    assert set(c.NOT_ENFORCED) == {"honor-system", "stale-gate", "malformed"}


# --- the cited-by hint --------------------------------------------------------

def test_citations_exclude_tests_caches_and_non_code_files(repo):
    cites = c.collect_citations(str(repo))
    assert cites[900] == ["core/scripts/real-gate.py"]
    assert 901 not in cites     # cited only by a test
    assert 902 not in cites     # cited only by a .md file
    assert 904 not in cites     # cited only under __pycache__


def test_citation_keys_ignore_leading_zeros(repo):
    # guard-007 (mod.py) and guard-7 (other.sh) are the same guardrail
    assert c.collect_citations(str(repo))[7] == ["core/scripts/other.sh", "mind_api/src/mod.py"]


@pytest.mark.parametrize("path,expected", [
    ("core/scripts/gates/x.py", True), ("core/scripts/foo-gate.py", True),
    ("core/scripts/pre-commit-hook.sh", True), ("core/scripts/domain-leak-check.sh", True),
    ("core/scripts/foo.py", False), ("mind_api/src/mod.py", False)])
def test_gate_like_by_name(path, expected):
    assert c.is_gate_like(path) is expected


def test_row_carries_the_cited_by_hint(repo):
    cites = c.collect_citations(str(repo))
    gate_cited = c.classify(_guard("guard-900"), repo, cites)
    assert gate_cited["cited_count"] == 1 and gate_cited["cited_by_gate_like"] is True
    plain = c.classify(_guard("guard-903"), repo, cites)
    assert plain["cited_count"] == 1 and plain["cited_by_gate_like"] is False
    test_only = c.classify(_guard("guard-901"), repo, cites)
    assert test_only["cited_count"] == 0 and test_only["cited_by_gate_like"] is False
    # the hint never changes the verdict: cited is still honor-system
    assert gate_cited["verdict"] == plain["verdict"] == "honor-system"


# --- the census ---------------------------------------------------------------

def _population(repo):
    return [
        _guard("guard-900", tags=["user-directive"]),                                  # honor-system, gate-cited
        _guard("guard-903", source="owner ruling 2026-09-25"),                         # honor-system, plain-cited
        _guard("guard-905", rule="STANDING OWNER DIRECTIVE: x"),                       # honor-system, uncited
        _guard("guard-906", tags=["user-correction"], enforced_by="core/scripts/real-gate.py"),   # gated
        _guard("guard-907", tags=["user-correction"], enforced_by="core/scripts/nope.py"),        # stale
        _guard("guard-908", tags=["user-correction"], enforced_by=5),                             # malformed
        _guard("guard-909", source="user-interaction"),                                # outside, counted as such
        _guard("guard-910", tags=["directive-lane"]),                                  # outside, not counted
        "not a dict", None, 5,
    ]


def test_census_counts_the_population_and_each_verdict(repo):
    rows, s = c.census(_population(repo), str(repo))
    assert s["active_guardrails_read"] == 11
    assert s["population"] == len(rows) == 6
    assert s["by_signal"] == {"tag": 4, "source": 1, "head": 1}
    assert s["by_verdict"] == {"gated": 1, "gated-unverified": 0, "stale-gate": 1,
                               "malformed": 1, "honor-system": 3}
    assert s["not_enforced"] == 5
    assert s["not_enforced_cited_by_gate_like"] == 1      # guard-900 only
    assert s["not_enforced_uncited"] == 3                 # 905, 907, 908
    assert s["source_mentions_user_unmatched"] == 1       # guard-909, and not guard-910


def test_a_gate_moves_a_row_out_of_not_enforced(repo):
    before = c.census([_guard("guard-905", tags=["user-directive"])], str(repo))[1]
    after = c.census([_guard("guard-905", tags=["user-directive"],
                             enforced_by="core/scripts/real-gate.py")], str(repo))[1]
    assert (before["not_enforced"], after["not_enforced"]) == (1, 0)
    assert after["by_verdict"]["gated"] == 1


# --- printing -----------------------------------------------------------------

def _hazard_rule():
    # built at runtime so no address- or account-id-shaped literal sits in the source
    return "Never send to " + "someone" + "@" + "example.invalid" + " in account " + "1" * 12 + " ever"


def test_printed_excerpts_mask_addresses_and_account_ids(repo, tmp_path, capsys):
    rec = _guard("guard-911", tags=["user-directive"], rule=_hazard_rule())
    # positive control: the raw record DOES carry both shapes
    assert "@" in rec["rule"] and "1" * 12 in rec["rule"]
    f = tmp_path / "g.json"
    f.write_text(json.dumps([rec]))
    assert c.main(["--guardrails-json-file", str(f), "--repo-root", str(repo)]) == 0
    text = capsys.readouterr().out
    assert "<addr>" in text and "<12d>" in text
    assert "@" not in text and "1" * 12 not in text
    assert c.main(["--guardrails-json-file", str(f), "--repo-root", str(repo), "--json"]) == 0
    blob = capsys.readouterr().out
    assert "@" not in blob and "1" * 12 not in blob


def test_a_gate_name_that_looks_like_an_address_is_masked_too(repo):
    row = c.classify(_guard(enforced_by="someone" + "@" + "example.invalid"), repo, {})
    assert row["gates"][0]["name"] == "<addr>"


def test_mask_runs_before_the_cut_so_no_fragment_of_an_address_prints():
    # The excerpt is cut at 90 characters. Cutting first would leave the front of an
    # address that straddles the cut ("some") with no "@" left for the mask to find.
    rule = "x" * 85 + " " + "someone" + "@" + "example.invalid"   # the address starts at 86
    head = c.classify(_guard(rule=rule), "/", {})["rule_head"]
    assert head.startswith("x" * 85 + " ") and "some" not in head


def test_text_report_shows_gate_and_honor_system(repo, tmp_path, capsys):
    f = tmp_path / "g.json"
    f.write_text(json.dumps(_population(repo)[:6]))
    assert c.main(["--guardrails-json-file", str(f), "--repo-root", str(repo)]) == 0
    text = capsys.readouterr().out
    assert "core/scripts/real-gate.py [present]" in text
    assert "core/scripts/nope.py [missing]" in text
    assert "(enforced_by has type int)" in text
    assert "honor-system (cited in 1 file, incl. a gate-like one)" in text
    assert re.search(r"NOT ENFORCED\s*:\s*5\b", text)


def test_honor_system_only_lists_the_gap_but_summarises_everyone(repo, tmp_path, capsys):
    f = tmp_path / "g.json"
    f.write_text(json.dumps(_population(repo)[:6]))
    assert c.main(["--guardrails-json-file", str(f), "--repo-root", str(repo),
                   "--json", "--honor-system-only"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["population"] == 6                       # the whole population
    assert {r["id"] for r in out["rows"]} == {"guard-900", "guard-903", "guard-905",
                                              "guard-907", "guard-908"}
    assert "guard-906" not in {r["id"] for r in out["rows"]}       # the gated row is not listed


# --- refusing a read that cannot be trusted -------------------------------------

def test_load_records_returns_a_list(tmp_path):
    f = tmp_path / "g.json"
    f.write_text(json.dumps([_guard()]))
    assert c.load_records(str(f)) == [_guard()]


@pytest.mark.parametrize("payload,needle", [
    ("{\"guardrails\": []}", "top-level keys: ['guardrails']"),
    ("[]", "0 active guardrails"),
    ("not json at all", "is not JSON"),
    ("", "is not JSON")])
def test_load_records_refuses_a_payload_that_would_read_as_a_clean_census(tmp_path, payload, needle):
    f = tmp_path / "g.json"
    f.write_text(payload)
    with pytest.raises(SystemExit) as exc:
        c.load_records(str(f))
    assert needle in str(exc.value) and "bytes" in str(exc.value)


# --- the wrapper -----------------------------------------------------------------

_STUB_READ = """#!/usr/bin/env bash
case "$STUB_MODE" in
  fail) echo "stub: daemon unreachable" >&2; exit 1 ;;
  empty) exit 0 ;;
  warn) echo "[runtime] WARNING: daemon is running stale code" >&2; cat "$STUB_PAYLOAD" ;;
  list) cat "$STUB_PAYLOAD" ;;
esac
"""


@pytest.fixture
def wrapper(tmp_path):
    if shutil.which(BASH) is None or shutil.which("python3") is None:
        pytest.skip("needs bash and python3 on PATH")
    scripts = tmp_path / "wrap/core/scripts"
    scripts.mkdir(parents=True)
    shutil.copy(CORE_SCRIPTS / "guardrail-enforcement-census.sh", scripts)
    shutil.copy(CORE_SCRIPTS / "guardrail_enforcement_census.py", scripts)
    (scripts / "_paths.sh").write_text("# stub: the wrapper only sources it\n")
    (scripts / "guardrails-read.sh").write_text(_STUB_READ)
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps([_guard("guard-900", tags=["user-directive"]), _guard("guard-901")]))
    tmpdir = tmp_path / "mktemp-dir"
    tmpdir.mkdir()

    def run(mode):
        env = dict(os.environ, STUB_MODE=mode, STUB_PAYLOAD=str(payload), TMPDIR=str(tmpdir))
        p = subprocess.run([BASH, (scripts / "guardrail-enforcement-census.sh").as_posix()],
                           env=env, capture_output=True, text=True, timeout=60)
        return p, tmpdir
    return run


def test_wrapper_prints_a_census_from_a_good_read(wrapper):
    p, tmpdir = wrapper("list")                     # positive control for the three below
    assert p.returncode == 0, p.stderr
    assert re.search(r"population\s*:\s*1\b", p.stdout)
    assert os.listdir(tmpdir) == []                 # the capture file is removed


def test_wrapper_refuses_a_failed_read(wrapper):
    p, tmpdir = wrapper("fail")
    assert p.returncode == 1 and p.stdout == ""
    assert "FAILED" in p.stderr and "stub: daemon unreachable" in p.stderr   # diagnostics unmerged
    assert os.listdir(tmpdir) == []


def test_wrapper_refuses_an_empty_read(wrapper):
    p, tmpdir = wrapper("empty")
    assert p.returncode == 1 and p.stdout == ""
    assert "EMPTY" in p.stderr
    assert os.listdir(tmpdir) == []


def test_reader_stderr_does_not_poison_the_payload(wrapper):
    # A stale-code warning on stderr beside rc=0 is a normal state after a framework
    # commit (guard-1963). Merging it into the payload would make the census refuse.
    p, _ = wrapper("warn")
    assert p.returncode == 0, p.stderr
    assert re.search(r"population\s*:\s*1\b", p.stdout)
    assert "WARNING: daemon is running stale code" in p.stderr
