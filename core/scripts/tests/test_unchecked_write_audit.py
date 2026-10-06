"""Tests for unchecked-write-audit.py ().

Three of these pin defects found by hand-checking the classifier's own output
mid-run, each of which moved the verdict. They are regression pins, not
decoration: the audit's whole value is that its number can be trusted, and every
one of these defects produced a plausible, confident, wrong number.

The non-vacuity test is the load-bearing one. An audit whose verdict is
structurally reachable in only one direction proves nothing by reporting that
direction (guard-1470) -- and this audit reports CONFIRMED, so "it can also say
CORRECTED" is exactly the claim a reader needs checked.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "unchecked-write-audit.py"


def _load():
    spec = importlib.util.spec_from_file_location("uwa", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def uwa():
    return _load()


def test_write_and_read_wrappers_are_discovered_not_hardcoded(uwa):
    """Population comes from the rt_call verb, so new wrappers join it for free."""
    write, read = uwa.discover_wrappers()
    assert "aspirations-add-goal.sh" in write
    assert "wm-set.sh" in write
    assert "aspirations-read.sh" in read
    assert "wm-read.sh" in read
    # A wrapper must not be classed both ways.
    assert not (write & read)


def test_store_prefix_handles_two_token_stores(uwa):
    assert uwa.store_prefix("aspirations-update-goal.sh") == "aspirations"
    assert uwa.store_prefix("team-state-update.sh") == "team-state"
    assert uwa.store_prefix("wm-append.sh") == "wm"


def test_piped_bash_form_is_an_invocation(uwa):
    """REGRESSION: the canonical wm-set idiom is `echo X | Bash: wm-set.sh slot`.

    An earlier shape anchored `Bash:` to line start and missed every piped write.
    That is a systematic false negative concentrated in one store, which moved
    the population by 78 sites.
    """
    line = """echo 'null' | Bash: wm-set.sh loop_state"""
    assert uwa.invokes(line, "wm-set.sh")


def test_prose_mention_is_not_an_invocation(uwa):
    """A reference in a Calls: list names a wrapper but cannot check an rc."""
    assert not uwa.invokes("- **Calls**: `env-read.sh`, `aspirations-add-goal.sh`",
                           "aspirations-add-goal.sh")
    assert not uwa.invokes("fall back to a goal via aspirations-add-goal.sh.",
                           "aspirations-add-goal.sh")


def test_markdown_table_row_excluded_but_piped_continuation_kept(uwa):
    """Both start with `|`; only one is documentation."""
    table = "   | A behavioral rule the agent must obey | Guardrails | guardrails-add.sh |"
    cont = "  | bash core/scripts/evolution-log-append.sh"
    assert uwa.is_table_row(table)
    assert not uwa.is_table_row(cont)


def _classify_corpus(uwa, tmp_path, body):
    """Run classify() over a synthetic SKILL.md and return the records."""
    p = tmp_path / "SKILL.md"
    p.write_text(body, encoding="utf-8")
    write, read = uwa.discover_wrappers()
    # classify() reports paths relative to PROJECT_ROOT; a tmp path is outside
    # it, so give it a path it can relativise.
    monkey = uwa.PROJECT_ROOT
    try:
        uwa.PROJECT_ROOT = tmp_path
        return list(uwa.classify(p, body.splitlines(), write, read))
    finally:
        uwa.PROJECT_ROOT = monkey


def test_rc_chain_on_the_call_line_counts(uwa, tmp_path):
    """REGRESSION: `x.sh && next` branches on the write's own exit status.

    Scanning only the LOOKAHEAD missed every one-line chain -- a false negative
    that biases toward the hypothesis under test, the one direction an audit
    must not lean.
    """
    recs = _classify_corpus(uwa, tmp_path,
                            'Bash: bash core/scripts/wm-set.sh slot && echo done\n')
    assert len(recs) == 1
    assert recs[0]["verified"] is True
    assert recs[0]["evidence"].startswith("rc:")


def test_prose_hint_is_excluded_from_strict_but_kept_as_band(uwa, tmp_path):
    """REGRESSION: the word "Verify:" in a goal TITLE is not write verification.

    Crediting it moved 30 sites and flipped the verdict. Strict must ignore the
    word; the generous band may keep it, clearly labelled.
    """
    body = (
        "Bash: bash core/scripts/aspirations-add-goal.sh --source world asp-1\n"
        '  title "Verify: the task works end-to-end"\n'
    )
    recs = _classify_corpus(uwa, tmp_path, body)
    assert len(recs) == 1
    assert recs[0]["verified"] is False, "prose word must not count as strict evidence"
    assert recs[0]["verified_generous"] is True, "band should still capture it"


def test_sibling_reread_of_same_store_counts(uwa, tmp_path):
    body = (
        "Bash: bash core/scripts/meta-set.sh skill-gaps.yaml gaps[0].type utility\n"
        "Bash: bash core/scripts/meta-read.sh skill-gaps.yaml\n"
    )
    recs = _classify_corpus(uwa, tmp_path, body)
    write_rec = [r for r in recs if r["wrapper"] == "meta-set.sh"]
    assert write_rec and write_rec[0]["verified"] is True
    assert write_rec[0]["evidence"] == "reread:meta"


def test_comment_lines_are_not_call_sites(uwa, tmp_path):
    recs = _classify_corpus(uwa, tmp_path,
                            "# Bash: bash core/scripts/wm-set.sh slot\n")
    assert recs == []


def test_classifier_is_not_vacuous_both_verdicts_reachable(uwa, tmp_path):
    """The audit reports CONFIRMED. Prove it is CAPABLE of reporting CORRECTED.

    A checker that can only emit one verdict emits no information. Here the same
    classifier run over an all-verified corpus must produce a fraction ABOVE the
    threshold, and over an all-unverified corpus one BELOW it.
    """
    unverified = "".join(
        f"Bash: bash core/scripts/wm-set.sh slot{i}\n" for i in range(20))
    verified = "".join(
        f"Bash: bash core/scripts/wm-set.sh slot{i} || handle_failure\n"
        for i in range(20))

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    lo = _classify_corpus(uwa, tmp_path / "a", unverified)
    hi = _classify_corpus(uwa, tmp_path / "b", verified)

    lo_frac = sum(r["verified"] for r in lo) / len(lo)
    hi_frac = sum(r["verified"] for r in hi) / len(hi)
    assert lo_frac == 0.0, "all-unverified corpus must score 0"
    assert hi_frac == 1.0, "all-verified corpus must score 1"
    assert lo_frac < 0.15 <= hi_frac, "both verdicts must straddle the threshold"


def test_empty_population_reports_skipped_not_confirmed(uwa, tmp_path, monkeypatch,
                                                        capsys):
    """An empty population must NEVER read as a confident CONFIRMED (rb-245).

    Found by the fresh-eyes pass on this file's own first version, which
    returned verdict=CONFIRMED / unverified=0 when wrapper discovery came back
    empty. That is the WORSE direction: g-115-3882 wires this output into a
    ratchet, and a ratchet only shrinks -- so a single broken-environment run
    reporting 0 drift would lock the baseline at 0 permanently, and every later
    real regression would sit under a baseline that can never grow back.
    """
    import json
    import sys
    monkeypatch.setattr(uwa, "SCRIPTS_DIR", tmp_path / "nonexistent")
    monkeypatch.setattr(sys, "argv", ["uwa"])
    uwa.main()
    d = json.loads(capsys.readouterr().out)
    assert d["population"]["write_wrappers"] == 0
    assert d["population"]["call_sites"] == 0
    assert d["verdict"] == "skipped", "empty population must not report CONFIRMED"
    assert d["generous_band"]["verdict"] == "skipped"


def test_wrapper_attribution_is_deterministic(uwa, tmp_path):
    """`break` keeps the FIRST match, so iteration order must not be a set.

    Python randomises string hashing per process; a raw set would make the
    attributed wrapper (and, on a line whose wrappers span two stores, the
    verdict) vary between runs. Inert today, but a ratchet turns a cosmetic
    flip into phantom drift.
    """
    body = "Bash: echo x | bash core/scripts/wm-set.sh slot && bash core/scripts/wm-reset.sh\n"
    seen = set()
    for _ in range(6):
        recs = _classify_corpus(uwa, tmp_path, body)
        seen.add(tuple((r["wrapper"], r["store"], r["verified"]) for r in recs))
    assert len(seen) == 1, f"attribution varied across runs: {seen}"


def test_live_run_reports_a_margin(uwa):
    """The verdict sits near its threshold, so the margin must be reported.

    A fraction alone forces every reader to reverse-engineer robustness. This
    audit's margin was 10 sites at measurement time -- small enough that the
    number is the point.
    """
    import json
    import subprocess
    import sys
    out = subprocess.run([sys.executable, str(SCRIPT)],
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr
    d = json.loads(out.stdout)
    assert "margin_sites_to_flip" in d
    assert "generous_band" in d
    assert d["population"]["call_sites"] > 0
    assert d["verdict"] in ("CONFIRMED", "CORRECTED")


def test_longer_wrapper_name_is_not_a_shorter_one(uwa):
    """REGRESSION: `verified-wm-set.sh` is NOT a `wm-set.sh` call site ().

    Six of the seven INVOKE_PATTERNS carry greedy, left-UNANCHORED prefixes
    (`\\S*`, `(?:bash\\s+\\S*)?`), so before the `(?<![A-Za-z0-9_.-])` lookbehind
    a wrapper name that was merely the SUFFIX of a longer name matched. Measured
    over the live corpus: 12 false sites, 11 of them `verified-wm-set.sh` scored
    as `wm-set.sh` -- which pinned a fleet-shared ratchet at REGRESSED against a
    baseline it could never reach, and the correction was mistaken for real drift
    for days. The seventh pattern (`^\\s*%(name)s\\s`) deliberately has no
    lookbehind because `^\\s*` already pins its left edge.

    The positive control at the end is load-bearing, not decoration: without it
    this test passes just as happily against an `invokes` that never matches
    anything, which is the failure direction a purely-negative assertion cannot
    see.
    """
    for line, shorter in (
        ("Bash: bash core/scripts/verified-wm-set.sh force_tree_maintain", "wm-set.sh"),
        ("echo 'null' | Bash: verified-wm-set.sh force_evolution_finalize", "wm-set.sh"),
        ("Bash: `echo 'null' | verified-wm-set.sh pending_phase_6_spark`", "wm-set.sh"),
        ("echo '{}' | Bash: agent-aspirations-add-goal.sh asp-001",
         "aspirations-add-goal.sh"),
    ):
        assert not uwa.invokes(line, shorter), f"prefix-extension matched: {line}"

    # Positive control -- the genuine invocations must STILL be call sites.
    assert uwa.invokes("Bash: bash core/scripts/wm-set.sh loop_state", "wm-set.sh")
    assert uwa.invokes("echo 'null' | Bash: wm-set.sh loop_state", "wm-set.sh")
    assert uwa.invokes("echo '{}' | Bash: aspirations-add-goal.sh asp-001",
                       "aspirations-add-goal.sh")


# --- `--new-since`: naming the sites that joined the unverified set -----------
#
# The ratchet records a count, so a regression used to arrive as "+N" beside a
# remedy that printed an arbitrary 20 of the unverified sites. These tests build a
# throwaway git repo holding the audit's two input shapes and check that the sites
# which moved are NAMED, that nothing is named when nothing moved, and that a tree
# the audit could not rebuild is refused rather than read as "no change".

WRITER = "#!/usr/bin/env bash\nrt_call POST /store\n"   # a write wrapper by the audit's own rule
READER = "#!/usr/bin/env bash\nrt_call GET /store\n"
_PROSE = "\n".join("Plain prose line %d." % i for i in range(8))   # no exit-code evidence
_CHECK = "IF that call exits non-zero, stop here."                 # matches the non-zero pattern
SKILL = ".claude/skills/demo/SKILL.md"
NO_TREE = {"head": None, "dirty": None}                            # a baseline row that recorded no tree


def _site(name):
    return "Bash: bash core/scripts/wm-set.sh " + name


def _skill(*sites):
    """A SKILL.md with one block per site: the site (it may span lines, e.g. a call plus a
    check line), then prose that carries no exit-code evidence."""
    return "# Demo skill\n\n" + "".join("%s\n%s\n\n" % (s, _PROSE) for s in sites)


def _line_of(body, needle):
    return [i for i, ln in enumerate(body.splitlines(), 1) if ln == needle]


def _git(repo, *args, env=None):
    proc = subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false", *args],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _write(repo, rel, body):
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")


def _commit(repo, msg, date=None):
    _git(repo, "add", "-A")
    env = dict(os.environ)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    _git(repo, "commit", "-q", "--no-verify", "-m", msg, env=env)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def delta_repo(tmp_path, uwa, monkeypatch):
    """An empty git checkout the audit is pointed at (its working tree is the 'after')."""
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    monkeypatch.setattr(uwa, "PROJECT_ROOT", root)
    monkeypatch.setattr(uwa, "SCRIPTS_DIR", root / "core" / "scripts")
    return root


def _seed(repo, *sites):
    _write(repo, "core/scripts/wm-set.sh", WRITER)
    _write(repo, "core/scripts/wm-read.sh", READER)
    _write(repo, SKILL, _skill(*sites))


def _net(rep):
    """joined minus left must equal the change in `unverified`, whatever moved."""
    return (sum(r["count"] for r in rep["joined"]) - sum(r["count"] for r in rep["left"]))


def _rec(text, line, verified, evidence=None, f="a/SKILL.md", wrapper="wm-set.sh"):
    return {"file": f, "wrapper": wrapper, "text": text, "line": line,
            "verified": verified, "evidence": evidence}


def test_delta_a_new_site_is_named_with_its_line(uwa, delta_repo):
    """The synthetic +1 the ratchet's remedy has to be able to answer."""
    _seed(delta_repo, _site("slot_a"), _site("slot_b"))
    first = _commit(delta_repo, "base")
    body = _skill(_site("slot_a"), _site("slot_b"), _site("slot_c"))
    _write(delta_repo, SKILL, body)
    rep = uwa.delta_report(uwa.collect_at(first), uwa.collect())
    assert rep["before"]["unverified"] == 2 and rep["after"]["unverified"] == 3
    assert [(r["file"], r["text"], r["lines"], r["absent"], r["verified_other"])
            for r in rep["joined"]] == [
        (SKILL, _site("slot_c"), _line_of(body, _site("slot_c")), 1, 0)]
    assert rep["left"] == []
    assert _net(rep) == 1


def test_delta_cli_reports_the_new_site_as_text_and_json(uwa, delta_repo, monkeypatch, capsys):
    _seed(delta_repo, _site("slot_a"))
    first = _commit(delta_repo, "base")
    body = _skill(_site("slot_a"), _site("slot_b"))
    _write(delta_repo, SKILL, body)

    monkeypatch.setattr(sys, "argv", ["uwa", "--new-since", first])
    assert uwa.main() == 0
    out = capsys.readouterr().out
    assert "JOINED the unverified set (1)" in out
    assert "%s:%d" % (SKILL, _line_of(body, _site("slot_b"))[0]) in out
    assert "new site" in out and "LEFT the unverified set (0)" in out
    assert "line numbers are in the AFTER tree" in out

    monkeypatch.setattr(sys, "argv", ["uwa", "--new-since", first, "--json"])
    assert uwa.main() == 0
    rep = json.loads(capsys.readouterr().out)
    assert [r["text"] for r in rep["joined"]] == [_site("slot_b")]
    assert rep["baseline"] is None and rep["left"] == []


def test_delta_a_line_shift_or_line_ending_is_not_a_change(uwa, delta_repo):
    """Records are keyed by (file, wrapper, text): inserting lines above a site, or
    rewriting the file with CRLF endings, moves every line number and changes nothing."""
    _seed(delta_repo, _site("slot_a"), _site("slot_b"))
    first = _commit(delta_repo, "base")
    shifted = "Added line 1.\nAdded line 2.\nAdded line 3.\n" + _skill(_site("slot_a"), _site("slot_b"))
    (delta_repo / SKILL).write_bytes(shifted.replace("\n", "\r\n").encode("utf-8"))
    rep = uwa.delta_report(uwa.collect_at(first), uwa.collect())
    assert rep["joined"] == [] and rep["left"] == []
    assert rep["before"]["unverified"] == rep["after"]["unverified"] == 2


def test_delta_a_site_that_loses_its_check_is_named_as_credit_lost(uwa, delta_repo, capsys):
    """The real regression class: no call line changed, a neighbouring line was edited
    and the site stopped being credited. The row says what it was credited by."""
    _seed(delta_repo, _site("slot_a") + "\n" + _CHECK, _site("slot_b"))
    first = _commit(delta_repo, "base")
    _write(delta_repo, SKILL, _skill(_site("slot_a"), _site("slot_b")))
    rep = uwa.delta_report(uwa.collect_at(first), uwa.collect())
    assert rep["before"]["unverified"] == 1 and rep["after"]["unverified"] == 2
    [row] = rep["joined"]
    assert row["text"] == _site("slot_a")
    assert (row["absent"], row["verified_other"]) == (0, 1)
    assert row["other_evidence"] and row["other_evidence"][0].startswith("rc:")
    assert _net(rep) == 1
    assert "credit lost (was rc:" in uwa.render_delta(dict(rep, before_label="b", after_label="a", baseline=None))


def test_delta_a_callee_that_became_a_write_wrapper_names_its_callers(uwa, delta_repo):
    """Reclassification: the call sites were always there and not one skill line changed.
    A diff of changed call-site lines finds nothing; a diff of the audit's own records
    names the callers and the wrapper that joined the population."""
    _write(delta_repo, "core/scripts/relay-set.sh", "#!/usr/bin/env bash\necho hello\n")
    _write(delta_repo, "core/scripts/wm-set.sh", WRITER)
    _write(delta_repo, "core/scripts/wm-read.sh", READER)
    skill = _skill("Bash: bash core/scripts/relay-set.sh one", "Bash: bash core/scripts/relay-set.sh two",
                   _site("slot_a"))
    _write(delta_repo, SKILL, skill)
    first = _commit(delta_repo, "base")
    _write(delta_repo, "core/scripts/relay-set.sh", "#!/usr/bin/env bash\nrt_call POST /store\n")
    second = _commit(delta_repo, "relay-set.sh gains a mutating call")
    assert _git(delta_repo, "diff", "--name-only", first, second) == "core/scripts/relay-set.sh"
    rep = uwa.delta_report(uwa.collect_at(first), uwa.collect_at(second))
    assert rep["wrappers_joined"] == ["relay-set.sh"] and rep["wrappers_dropped"] == []
    assert sorted(r["text"] for r in rep["joined"]) == [
        "Bash: bash core/scripts/relay-set.sh one", "Bash: bash core/scripts/relay-set.sh two"]
    assert _net(rep) == 2
    assert "wrapper newly a write wrapper" in uwa.render_delta(
        dict(rep, before_label="b", after_label="a", baseline=None))
    # The mirror: the wrapper loses the call and its callers leave the unverified set.
    back = uwa.delta_report(uwa.collect_at(second), uwa.collect_at(first))
    assert back["wrappers_dropped"] == ["relay-set.sh"] and back["wrappers_joined"] == []
    assert back["joined"] == [] and sorted(r["text"] for r in back["left"]) == sorted(
        r["text"] for r in rep["joined"])


def test_delta_departures_are_reported_and_the_two_lists_reconcile(uwa, delta_repo):
    _seed(delta_repo, _site("slot_a"), _site("slot_b"), _site("slot_c"))
    first = _commit(delta_repo, "base")
    _write(delta_repo, SKILL, _skill(_site("slot_a") + "\n" + _CHECK, _site("slot_b")))
    rep = uwa.delta_report(uwa.collect_at(first), uwa.collect())
    by_text = {r["text"]: r for r in rep["left"]}
    assert (by_text[_site("slot_a")]["absent"], by_text[_site("slot_a")]["verified_other"]) == (0, 1)
    assert (by_text[_site("slot_c")]["absent"], by_text[_site("slot_c")]["verified_other"]) == (1, 0)
    assert rep["joined"] == []
    assert _net(rep) == rep["after"]["unverified"] - rep["before"]["unverified"] == -2
    text = uwa.render_delta(dict(rep, before_label="b", after_label="a", baseline=None))
    assert "LEFT the unverified set (2) -- line numbers are in the BEFORE tree" in text


def test_delta_identical_lines_are_counted_not_collapsed(uwa):
    """Twins have the same key. A set-based diff would report 0 when a third appears."""
    before = [_rec("call", 10, False), _rec("call", 20, False)]
    after = [_rec("call", 10, False), _rec("call", 20, False), _rec("call", 30, False)]
    [row] = uwa._moves(before, after)
    assert (row["count"], row["identical"], row["lines"]) == (1, 3, [30])
    assert uwa._moves(after, before) == []


def test_delta_twins_are_matched_by_order_so_the_flipped_one_is_named(uwa):
    before = [_rec("call", 10, True, "rc:x"), _rec("call", 20, False), _rec("call", 30, False)]
    after = [_rec("call", 11, False), _rec("call", 21, False), _rec("call", 31, False)]
    [row] = uwa._moves(before, after)
    assert row["lines"] == [11] and row["count"] == 1
    assert (row["absent"], row["verified_other"], row["other_evidence"]) == (0, 1, ["rc:x"])


def test_delta_twins_fall_back_to_naming_all_when_order_matching_disagrees(uwa):
    """If matching by order does not reproduce the count, name every unverified twin:
    wide is acceptable, a wrong line is not."""
    before = [_rec("call", 10, False), _rec("call", 20, False), _rec("call", 30, True, "rc:x")]
    after = [_rec("call", 11, True, "rc:x"), _rec("call", 21, False), _rec("call", 31, False),
             _rec("call", 41, False)]
    [row] = uwa._moves(before, after)
    # Matching by order would name 31 and 41 (two lines for a count of 1), which is not
    # the count, so every unverified twin is named instead.
    assert row["count"] == 1 and row["lines"] == [21, 31, 41]
    text = uwa.render_delta({
        "before": dict(unverified=2, verified=1, call_sites=3, write_wrappers=1, skill_files=1),
        "after": dict(unverified=3, verified=1, call_sites=4, write_wrappers=1, skill_files=1),
        "joined": [row], "left": [], "wrappers_joined": [], "wrappers_dropped": [],
        "before_label": "b", "after_label": "a", "baseline": None})
    assert "(1 of these 3 unverified identical lines)" in text


@pytest.mark.parametrize("before,after", [
    ([_rec("a", 1, False)], [_rec("a", 1, False), _rec("b", 2, False)]),
    ([_rec("a", 1, True, "rc:x"), _rec("b", 2, False)], [_rec("a", 1, False), _rec("b", 2, True, "rc:x")]),
    ([_rec("a", 1, False), _rec("a", 2, True, "rc:x")], [_rec("a", 1, True, "rc:x"), _rec("a", 2, False), _rec("a", 3, False)]),
    ([_rec("a", 1, False), _rec("b", 2, False)], []),
])
def test_delta_joined_minus_left_equals_the_change_in_unverified(uwa, before, after):
    joined, left = uwa._moves(before, after), uwa._moves(after, before)
    unv = lambda rs: sum(1 for r in rs if not r["verified"])
    assert sum(r["count"] for r in joined) - sum(r["count"] for r in left) == unv(after) - unv(before)


def test_delta_an_unknown_revision_fails_visibly(uwa, delta_repo, capsys):
    _seed(delta_repo, _site("slot_a"))
    _commit(delta_repo, "base")
    assert uwa.delta_main("no-such-revision", None, False) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "does not name a commit" in captured.err


def test_delta_a_tree_that_could_not_be_rebuilt_is_refused_not_read_as_no_change(uwa, delta_repo, capsys):
    """An empty `before` makes every site look new; an empty one on the other side makes
    every site look fixed. Both are a blind run, so neither may print a delta."""
    _write(delta_repo, "README", "nothing the audit reads\n")
    bare = _commit(delta_repo, "bare")
    _write(delta_repo, "core/scripts/wm-read.sh", READER)      # a reader only: no write wrapper
    _write(delta_repo, SKILL, _skill(_site("slot_a")))
    readers_only = _commit(delta_repo, "readers only")
    _seed(delta_repo, _site("slot_a"))
    full = _commit(delta_repo, "full")

    assert uwa.delta_main(bare, None, False) == 2
    err = capsys.readouterr()
    assert err.out == "" and "git archive failed" in err.err

    assert uwa.delta_main(readers_only, None, False) == 2
    err = capsys.readouterr()
    assert err.out == "" and "the before tree read an empty population" in err.err

    assert uwa.delta_main(full, readers_only, False) == 2
    err = capsys.readouterr()
    assert err.out == "" and "the after tree read an empty population" in err.err


def test_delta_archive_members_cannot_land_outside_the_scratch_dir(uwa, tmp_path, monkeypatch):
    """The tar comes from our own history, but the filter is what the docstring promises."""
    probe = Path(tempfile.gettempdir()) / "unchecked-write-escape-probe.sh"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, body in (("../unchecked-write-escape-probe.sh", b"rt_call POST /x\n"),
                           ("core/scripts/wm-set.sh", WRITER.encode()),
                           (SKILL, _skill(_site("slot_a")).encode())):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    monkeypatch.setattr(uwa, "_git", lambda *a: buf.getvalue())
    try:
        records, write_names, _read, skill_files = uwa.collect_at("anything")
        assert not probe.exists()
        assert write_names == {"wm-set.sh"} and skill_files == 1 and len(records) == 1
    finally:
        if probe.exists():
            probe.unlink()


def test_delta_a_rebuilt_tree_classifies_like_the_checkout_it_came_from(uwa, delta_repo):
    """The invariant the whole mode rests on: the corpus is the only variable. Nested files
    that neither the live walk nor the archive filter selects are present to prove both
    leave them out."""
    _seed(delta_repo, _site("slot_a"), _site("slot_b") + "\n" + _CHECK)
    _write(delta_repo, "core/scripts/tests/nested-set.sh", WRITER)
    _write(delta_repo, ".claude/skills/demo/extra/SKILL.md", _skill(_site("slot_z")))
    head = _commit(delta_repo, "base")
    live, rebuilt = uwa.collect(), uwa.collect_at(head)
    assert live[0] == rebuilt[0] and live[1] == rebuilt[1] and live[3] == rebuilt[3]
    assert len(live[0]) == 2 and live[3] == 1


def test_delta_baseline_reading_is_the_newest_row_at_the_baseline(uwa, tmp_path):
    yaml = pytest.importorskip("yaml")
    def history(*rows):
        p = tmp_path / "audit-baselines.yaml"
        p.write_text(yaml.safe_dump({"unchecked_writes": {"baseline": 444, "history": [
            {"recorded_at": t, "drift_total": n, "verdict": "x"} for t, n in rows]}}), encoding="utf-8")
        return p

    # Not in chronological order, and the last row is a higher reading: newest AT the baseline.
    p = history(("2026-01-03T00:00:00", 444), ("2026-01-01T00:00:00", 444),
                ("2026-01-04T00:00:00", 446), ("2026-01-02T00:00:00", 446))
    assert uwa.baseline_reading(p) == (444, "2026-01-03T00:00:00", NO_TREE)

    # A row that recorded the tree it read hands it back, and an older row at the baseline
    # does not leak its own tree into the newest one's.
    sha = "ab" * 20
    p = tmp_path / "audit-baselines.yaml"
    p.write_text(yaml.safe_dump({"unchecked_writes": {"baseline": 444, "history": [
        {"recorded_at": "2026-01-01T00:00:00", "drift_total": 444, "verdict": "x",
         "breakdown": {"head": "cd" * 20, "dirty": 9}},
        {"recorded_at": "2026-01-02T00:00:00", "drift_total": 444, "verdict": "x",
         "breakdown": {"unverified": 444, "head": sha, "dirty": 2}}]}}), encoding="utf-8")
    assert uwa.baseline_reading(p) == (444, "2026-01-02T00:00:00", {"head": sha, "dirty": 2})

    gone = history(("2026-01-04T00:00:00", 446), ("2026-01-05T00:00:00", 446))
    with pytest.raises(RuntimeError, match="left the window"):
        uwa.baseline_reading(gone)

    (tmp_path / "empty.yaml").write_text("other_metric: {baseline: 3}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="no unchecked_writes baseline"):
        uwa.baseline_reading(tmp_path / "empty.yaml")


def test_delta_baseline_mode_resolves_the_commit_at_or_before_the_reading(uwa, delta_repo, monkeypatch):
    _seed(delta_repo, _site("slot_a"))
    c1 = _commit(delta_repo, "c1", "2026-01-01T10:00:00+0000")
    _write(delta_repo, "NOTES", "two\n")
    c2 = _commit(delta_repo, "c2", "2026-01-02T10:00:00+0000")
    _write(delta_repo, "NOTES", "three\n")
    c3 = _commit(delta_repo, "c3", "2026-01-03T10:00:00+0000")
    # A zone east of UTC: a stamp handed to git WITHOUT an explicit UTC offset would be read
    # in local time, 13 hours early, and land on c1 instead of c2.
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    monkeypatch.setattr(uwa, "baseline_reading", lambda: (444, "2026-01-02T12:00:00", NO_TREE))

    ref = uwa.resolve_since("baseline", None)
    assert ref["sha"] == c2 and ref["baseline"] == 444
    assert uwa.resolve_since("baseline", c1)["sha"] == c1          # the walk starts at --until
    assert uwa.resolve_since(c3, None) == {"sha": c3, "how": "named", "baseline": None}

    monkeypatch.setattr(uwa, "baseline_reading", lambda: (444, "2025-12-31T00:00:00", NO_TREE))
    with pytest.raises(RuntimeError, match="no commit at or before"):
        uwa.resolve_since("baseline", None)


def test_delta_report_prints_on_a_stdout_that_cannot_encode_the_skill_text(uwa, delta_repo, monkeypatch):
    """Skill lines carry arrows and dashes. A console encoding that cannot hold them must
    not cost the operator the report after the work is done."""
    _seed(delta_repo, _site("slot_a"))
    first = _commit(delta_repo, "base")
    _write(delta_repo, SKILL, _skill(_site("slot_a"), _site("slot_b") + "  # step \u2192 next"))
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="ascii", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stdout", stdout)
    assert uwa.delta_main(first, None, False) == 0
    assert "\u2192".encode("utf-8") in raw.getvalue()


def test_delta_until_without_new_since_is_a_usage_error(uwa, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["uwa", "--until", "HEAD"])
    with pytest.raises(SystemExit) as exc:
        uwa.main()
    assert exc.value.code == 2


# --- provenance: which tree a reading was taken over () -------------------
#
# The ratchet's floor is a count, and a count from another box's tree reads like drift. Each
# reading now carries the HEAD it was taken at and how many audited inputs differed from it,
# and `--new-since baseline` rebuilds that head instead of guessing a commit from the clock.

def test_provenance_names_head_and_counts_the_audited_inputs_that_differ_from_it(uwa, delta_repo):
    """The census walks the filesystem, so an untracked wrapper moves the count exactly as an
    edited one does: `dirty` counts both, plus staged and deleted inputs."""
    _seed(delta_repo, _site("slot_a"))
    head = _commit(delta_repo, "base")
    assert uwa.provenance() == {"head": head, "dirty": 0}

    _write(delta_repo, SKILL, _skill(_site("slot_a"), _site("slot_b")))          # edited input
    assert uwa.provenance()["dirty"] == 1
    _git(delta_repo, "add", SKILL)                                               # staged still differs
    assert uwa.provenance()["dirty"] == 1
    _write(delta_repo, "core/scripts/new-set.sh", WRITER)                        # untracked input
    _write(delta_repo, ".claude/skills/fresh/SKILL.md", _skill(_site("slot_z")))  # untracked, new directory
    (delta_repo / "core/scripts/wm-read.sh").unlink()                            # deleted input
    assert uwa.provenance() == {"head": head, "dirty": 4}

    # What neither the live walk nor the archive filter selects is not counted.
    _write(delta_repo, "core/scripts/tests/nested-set.sh", WRITER)
    _write(delta_repo, ".claude/skills/demo/extra/SKILL.md", _skill(_site("slot_y")))
    _write(delta_repo, "NOTES", "not an input\n")
    assert uwa.provenance() == {"head": head, "dirty": 4}


def test_provenance_counts_an_untracked_input_even_when_the_checkout_hides_untracked_files(uwa, delta_repo):
    """`--untracked-files=all` looks redundant under git's defaults and is not: a checkout
    configured with status.showUntrackedFiles=no lists nothing untracked without it."""
    _seed(delta_repo, _site("slot_a"))
    head = _commit(delta_repo, "base")
    _git(delta_repo, "config", "status.showUntrackedFiles", "no")
    _write(delta_repo, "core/scripts/new-set.sh", WRITER)
    assert _git(delta_repo, "status", "--porcelain", "--", "core/scripts/*.sh") == ""   # the hazard
    assert uwa.provenance() == {"head": head, "dirty": 1}


def test_provenance_is_none_when_git_cannot_answer(uwa, delta_repo, monkeypatch):
    """A reading is never lost to a checkout git cannot read: both fields come back None."""
    assert uwa.provenance() == {"head": None, "dirty": None}      # no commit yet, so HEAD names nothing

    def no_git(*args):
        raise OSError("git is not installed")
    monkeypatch.setattr(uwa, "_git", no_git)
    assert uwa.provenance() == {"head": None, "dirty": None}


def test_census_json_carries_the_tree_it_read(uwa, delta_repo, monkeypatch, capsys):
    _seed(delta_repo, _site("slot_a"))
    head = _commit(delta_repo, "base")
    monkeypatch.setattr(sys, "argv", ["uwa"])
    assert uwa.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["provenance"] == {"head": head, "dirty": 0}
    assert out["unverified"] == 1          # the new key moves no count


def _three_commits(repo):
    _seed(repo, _site("slot_a"))
    c1 = _commit(repo, "c1", "2026-01-01T10:00:00+0000")
    _write(repo, "NOTES", "two\n")
    c2 = _commit(repo, "c2", "2026-01-02T10:00:00+0000")
    _write(repo, "NOTES", "three\n")
    c3 = _commit(repo, "c3", "2026-01-03T10:00:00+0000")
    return c1, c2, c3


def test_delta_baseline_mode_uses_the_head_recorded_on_the_row(uwa, delta_repo, monkeypatch, capsys):
    """The row names the tree it read, so the clock is not consulted: the stamp alone lands on c2."""
    c1, _c2, c3 = _three_commits(delta_repo)
    monkeypatch.setattr(uwa, "baseline_reading",
                        lambda: (444, "2026-01-02T12:00:00", {"head": c1, "dirty": 0}))
    ref = uwa.resolve_since("baseline", None)
    assert (ref["sha"], ref["baseline"]) == (c1, 444)
    assert "head recorded" in ref["how"] and "approximate" not in ref["how"]
    assert uwa.resolve_since("baseline", c3)["sha"] == c1       # `until` bounds the date walk only

    monkeypatch.setattr(uwa, "baseline_reading",
                        lambda: (444, "2026-01-02T12:00:00", {"head": c1, "dirty": 2}))
    assert "2 audited file(s) differed" in uwa.resolve_since("baseline", None)["how"]

    # and the operator sees which way the revision was found
    _write(delta_repo, SKILL, _skill(_site("slot_a"), _site("slot_b")))
    assert uwa.delta_main("baseline", None, False) == 0
    assert "head recorded with the baseline reading of 2026-01-02T12:00:00" in capsys.readouterr().out


def test_delta_baseline_mode_falls_back_to_the_clock_when_the_row_has_no_usable_head(uwa, delta_repo, monkeypatch):
    c1, c2, c3 = _three_commits(delta_repo)
    cases = (
        (None, False),           # a row written before the field existed
        ("ef" * 20, True),       # a full sha this checkout does not hold
        ("main", True),          # symbolic names move, so they are not heads: this one would
        ("HEAD", True),          # resolve to c3 if it reached git
        ("--output=x", True),    # and nothing read from the file may reach git as an option
    )
    for head, says_so in cases:
        monkeypatch.setattr(uwa, "baseline_reading",
                            lambda head=head: (444, "2026-01-02T12:00:00", {"head": head, "dirty": 0}))
        ref = uwa.resolve_since("baseline", None)
        assert ref["sha"] == c2, head
        assert ("not a commit in this checkout" in ref["how"]) is says_so, (head, ref["how"])
