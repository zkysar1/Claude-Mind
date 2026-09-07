#!/usr/bin/env python3
"""Bash-derived fetch provenance + framework rule-ids as source tokens ().

Two halves of one defect class: the Q4 provenance gate reported FAIL on citations
the session had genuinely made, because the manifest could not see how they were
made. Measured on g-115-6286 occurrence 10 -- three URLs pulled live with curl
(HTTP 200, 49,276-byte body) all reported "cited but NOT retrieved this session",
and a line whose only citation was `guard-2024` reported "missing-citation".

TWO KINDS OF TEST BELOW, following test_q4_provenance_sample.py's split. The
POSITIVES pin the new behaviour. The CONTROLS pin what must NOT have moved, and
they carry the weight here: this change's effect is that findings STOP appearing,
and a fix like that is indistinguishable from breaking the check unless a control
that should NOT flip is asserted alongside (guard-4166). Hence
test_bare_url_without_fetch_verb_is_not_recorded and
test_uncited_line_still_reports_missing_citation -- if either ever goes green-by-
silence, this change has become alarm suppression (guard-1901).
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from _fetch_provenance_extract import extract  # noqa: E402
from ground_truth_citation import analyze, source_tokens  # noqa: E402


def _bash(command):
    return {"tool_name": "Bash", "session_id": "sid-test",
            "tool_input": {"command": command}}


# ─── Bash-derived fetch: positives ───────────────────────────────────────────

def test_curl_url_is_recorded():
    _sid, recs = extract(_bash("curl -s https://lodestar.wiki/ -o /dev/null"))
    assert ("url", "https://lodestar.wiki/") in recs


def test_wget_url_is_recorded():
    _sid, recs = extract(_bash("wget https://example.com/a/b -O out.html"))
    assert ("url", "https://example.com/a/b") in recs


def test_query_and_fragment_are_stripped_from_recorded_url():
    """guard-2426: a command line is where credentials ride along as ?token=.

    The stripped form still MATCHES a citation of the full URL, because
    retrieved_predicate substring-matches in both directions -- so this costs
    no verification power.
    """
    _sid, recs = extract(_bash("curl https://api.example.com/v1/x?token=SECRET#frag"))
    assert recs == [("url", "https://api.example.com/v1/x")]
    assert all("SECRET" not in v for _k, v in recs)


def test_multiple_urls_in_one_command_are_all_recorded():
    _sid, recs = extract(_bash(
        "curl https://a.example/one && curl https://b.example/two"))
    values = [v for k, v in recs if k == "url"]
    assert "https://a.example/one" in values
    assert "https://b.example/two" in values


# ─── Bash-derived fetch: controls that must NOT move ─────────────────────────

def test_bare_url_without_fetch_verb_is_not_recorded():
    """THE load-bearing control. Without it this recorder launders fabrication.

    If a URL merely APPEARING in a command registered as a retrieval, then
    `echo "https://fabricated"` would satisfy the very check Q4 exists to run,
    and the gate would be worse than useless -- it would certify invention
    (guard-1901: alarm suppression is the one direction this must never fail).
    """
    _sid, recs = extract(_bash('echo "https://fabricated.example/page"'))
    assert recs == []


def test_command_with_no_url_records_nothing():
    _sid, recs = extract(_bash("ls -la core/scripts && wc -c CLAUDE.md"))
    assert recs == []


def test_webfetch_path_is_unchanged():
    """Regression control: the working tool-bound path must be byte-identical."""
    _sid, recs = extract({"tool_name": "WebFetch", "session_id": "s",
                          "tool_input": {"url": "https://real.example/x"}})
    assert recs == [("url", "https://real.example/x")]


def test_websearch_path_is_unchanged():
    _sid, recs = extract({"tool_name": "WebSearch", "session_id": "s",
                          "tool_input": {"query": "lodestar block status"}})
    assert ("search", "lodestar block status") in recs


# ─── Framework rule-ids as source tokens ─────────────────────────────────────

def test_guard_id_is_a_source_token():
    assert ("rule-id", "guard-2024") in source_tokens(
        "The value sb.ssr=1 is unassessed and never folded into CLEAN (guard-2024).")


def test_rb_id_is_a_source_token():
    assert ("rule-id", "rb-10303") in source_tokens(
        "A synced write can arrive split, which rb-10303 records.")


def test_rule_id_does_not_swallow_goal_ids():
    """Control: the two id shapes stay distinct, and neither masks the other."""
    toks = source_tokens("The Close on g-115-6286 is governed by guard-1604.")
    assert ("goal-id", "g-115-6286") in toks
    assert ("rule-id", "guard-1604") in toks


def test_rule_id_only_line_no_longer_reports_missing_citation():
    text = ("The Safe Browsing value sb.ssr=1 is unassessed and never folded "
            "into CLEAN (guard-2024).")
    kinds = [f.kind for f in analyze(text, retrieved=lambda k, v: False)]
    assert "missing-citation" not in kinds


def test_uncited_line_still_reports_missing_citation():
    """The other half of guard-4166: the check must still FIRE where it should.

    Without this, `test_rule_id_only_line_no_longer_reports_missing_citation`
    would pass just as happily if the citation check had been disabled outright.
    """
    text = "The Xfinity oracle reported that lodestar.wiki is no longer blocked."
    kinds = [f.kind for f in analyze(text, retrieved=lambda k, v: False)]
    assert "missing-citation" in kinds
