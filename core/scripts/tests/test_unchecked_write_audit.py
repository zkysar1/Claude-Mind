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
