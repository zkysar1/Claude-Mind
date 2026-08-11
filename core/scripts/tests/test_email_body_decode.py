"""Dogfood + regression suite for core/scripts/email_body_decode.py (gap-070, ).

Forge Step 3.6 requires a correctness-critical companion script to discriminate
between fixtures that must drive DIFFERENT verdicts -- a script returning the
same answer on the PASS and FAIL fixtures is vacuous (guard-1220, rb-4133).

The three fixtures here are chosen so each fails a DIFFERENT prior decoder:

  multipart+quoted   the goal's own two checks; both slices non-empty and the
                     digest row is in full_body but NOT in new_text
  pure top-post      new_text is EMPTY while full_body is populated -- the
                     guard-2488 incident shape, and the reason both slices exist
  html-only          agent-inbox-fetch.sh returns EMPTY here (it walks for
                     text/plain only); this must return readable text

Anti-vacuity is asserted per-fixture, never via a single aggregate line
(guard-1793): an aggregate summarises one axis and reads green through a defect
on another.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "core" / "scripts"))

from email_body_decode import decode, strip_html, cut_index  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────

# Quoted-printable soft break: the digest row is split mid-line with a trailing
# '=', so a decoder WITHOUT quoted-printable handling cannot read the row at
# all. Encounter 2 recorded that rejoin as mandatory to read a wrapped table.
MULTIPART_QUOTED = """\
From: User <user@example.com>
To: Agent <agent@example.com>
Subject: Re: Escalation digest
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: quoted-printable

Sounds great, lets do it.

On Sun, Aug 3, 2026 at 9:00 AM Agent <agent@example.com> wrote:
> The following item is awaiting your approval:
> g-115-4379 =
| awaiting approval | 2 days | agent-z
> Reply to approve.
--
Sent from a device
"""

# Pure top-post: the very first body line is already quoted history, so the cut
# lands at index 0. new_text MUST come back empty and full_body MUST NOT.
PURE_TOP_POST = """\
From: User <user@example.com>
Subject: Re: Escalation digest
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

> The following item is awaiting your approval:
> g-115-4379 | awaiting approval | 2 days | agent-z
> Reply to approve.
"""

# HTML-only, NON-multipart. Handled by the bare-payload branch, NOT by the
# plain/html preferencelist -- keep both fixtures, because they cover different
# code paths and only the multipart one reaches the preferencelist loop. (The
# mutation proof caught this: with only this fixture, deleting "html" from the
# preferencelist left the suite GREEN -- a vacuous test passing for the wrong
# reason, guard-1462.)
HTML_ONLY = """\
From: Alerts <alerts@example.com>
Subject: Daily summary
MIME-Version: 1.0
Content-Type: text/html; charset="utf-8"

<html><head><style>p{color:red}</style></head><body>
<p>Critical issues detected</p>
<div>g-115-4379 | awaiting approval</div>
<script>var x=1;</script>
</body></html>
"""

# HTML-only, MULTIPART -- the shape that actually breaks the promoted-from
# decoder. agent-inbox-fetch.sh walks a multipart message for text/plain ONLY,
# so it returns empty here. This is the fixture that exercises the
# plain->html preferencelist fallback.
MULTIPART_HTML_ONLY = """\
From: Alerts <alerts@example.com>
Subject: Daily summary
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="BOUND"

--BOUND
Content-Type: text/html; charset="utf-8"

<html><body>
<p>Critical issues detected</p>
<div>g-115-4379 | awaiting approval</div>
</body></html>

--BOUND--
"""

DIGEST_ROW = "g-115-4379 | awaiting approval"


# ── the goal's two verification checks ──────────────────────────────────────

def test_both_slices_nonempty_on_multipart_with_quoted_history():
    """Goal check 1: both slices come back non-empty."""
    r = decode(MULTIPART_QUOTED)
    assert r["new_text"], "new_text slice was empty"
    assert r["full_body"], "full_body slice was empty"
    assert r["new_text"] != r["full_body"], "slices identical -- strip did nothing"


def test_digest_row_in_full_body_but_not_stripped_slice():
    """Goal check 2: the digest row survives in full_body and is absent from new_text.

    This is the guard-2488 requirement stated as an assertion: the referent
    lives ONLY inside the quoted block, so a caller given the stripped slice
    alone cannot recover it.
    """
    r = decode(MULTIPART_QUOTED)
    assert DIGEST_ROW in r["full_body"], (
        "digest row missing from full_body -- quoted-printable soft break was "
        "not rejoined, so the wrapped row is unreadable"
    )
    assert DIGEST_ROW not in r["new_text"], "quoted digest leaked into new_text"
    assert "Sounds great, lets do it." in r["new_text"]


# ── discriminating fixtures (Step 3.6 anti-vacuity) ─────────────────────────

def test_pure_top_post_yields_empty_new_text_and_populated_full_body():
    """The incident shape: cut at line 0. Distinct verdict from fixture 1."""
    r = decode(PURE_TOP_POST)
    assert r["new_text"] == "", "expected empty new_text on a pure top-post"
    assert DIGEST_ROW in r["full_body"], "full_body lost the only copy of the referent"
    assert r["cut_at_line"] == 0
    assert r["quoted_history_present"] is True


def test_multipart_html_only_reaches_the_preferencelist_fallback():
    """The shape that actually breaks agent-inbox-fetch.sh.

    It walks a multipart message for text/plain ONLY, so it returns empty on
    an HTML-only multipart. This asserts the plain->html preferencelist
    fallback fires; the mutation proof pins it (removing "html" from the
    preferencelist must turn this RED).
    """
    r = decode(MULTIPART_HTML_ONLY)
    assert r["was_html"] is True, "preferencelist fallback did not reach text/html"
    assert r["content_type_used"] == "text/html"
    assert "Critical issues detected" in r["full_body"]
    assert DIGEST_ROW in r["full_body"]
    assert "<p>" not in r["full_body"]


def test_html_only_message_still_yields_readable_text():
    """Non-multipart HTML -- the bare-payload branch, a different path."""
    r = decode(HTML_ONLY)
    assert r["was_html"] is True
    assert r["content_type_used"] == "text/html"
    assert "Critical issues detected" in r["full_body"]
    assert DIGEST_ROW in r["full_body"]
    assert "<p>" not in r["full_body"] and "<div>" not in r["full_body"]
    assert "var x=1" not in r["full_body"], "script body leaked into text"
    assert "color:red" not in r["full_body"], "style body leaked into text"


def test_three_fixtures_drive_three_distinct_verdicts():
    """Explicit discrimination proof -- the fixtures are not interchangeable."""
    a, b, c = decode(MULTIPART_QUOTED), decode(PURE_TOP_POST), decode(HTML_ONLY)
    assert (bool(a["new_text"]), a["was_html"]) == (True, False)
    assert (bool(b["new_text"]), b["was_html"]) == (False, False)
    assert (bool(c["new_text"]), c["was_html"]) == (True, True)


# ── invariants ──────────────────────────────────────────────────────────────

def test_never_truncates():
    """The whole point of the primitive: defeat the 1500/500-char caps."""
    long_line = "x" * 5000
    msg = (
        'From: a@b.c\nSubject: s\nMIME-Version: 1.0\n'
        'Content-Type: text/plain; charset="utf-8"\n\n' + long_line + "\n"
    )
    r = decode(msg)
    assert len(r["full_body"]) >= 5000, "body was truncated"
    assert r["truncated"] is False


def test_no_quoted_history_leaves_slices_equal():
    msg = (
        'From: a@b.c\nSubject: s\nMIME-Version: 1.0\n'
        'Content-Type: text/plain; charset="utf-8"\n\nJust one line.\n'
    )
    r = decode(msg)
    assert r["new_text"] == r["full_body"] == "Just one line."
    assert r["quoted_history_present"] is False


# ── CRLF: the PRODUCTION shape (guard-920) ──────────────────────────────────
# Every other fixture in this file is written with \n. Real email is CRLF per
# RFC 5322, and on CRLF input decode() used to build full_body from the original
# bytes while rebuilding new_text with "\n".join(...) -- so the slices were not
# comparable and `new_text in full_body` was False on essentially every real
# message. The LF fixtures above cannot see that class at all: the assertion
# `new_text == full_body` was true of the fixture and false of the input the
# primitive exists to handle. Found by fresh-eyes on forge day.

_CRLF_PLAIN = (
    "From: a@b.c\nSubject: s\nMIME-Version: 1.0\n"
    'Content-Type: text/plain; charset="utf-8"\n\n'
    "Sounds great, lets do it.\nSecond line.\n"
).replace("\n", "\r\n")

_CRLF_QUOTED = MULTIPART_QUOTED.replace("\n", "\r\n")


def test_crlf_slices_are_comparable():
    """Both slices must derive from the SAME normalized text."""
    r = decode(_CRLF_PLAIN)
    assert "\r" not in r["full_body"], "full_body kept CRLF while new_text was rebuilt with LF"
    assert "\r" not in r["new_text"]
    # No quoted history in this fixture, so the two slices must coincide --
    # the same invariant the LF test asserts, now on the production shape.
    assert r["new_text"] == r["full_body"]
    assert r["new_text"] in r["full_body"]
    assert r["quoted_history_present"] is False


def test_crlf_preserves_cut_and_digest_recovery():
    """Normalizing must not disturb the cut or lose the referent."""
    r = decode(_CRLF_QUOTED)
    assert r["new_text"] in r["full_body"], "stripped slice is not a prefix of the full one"
    assert DIGEST_ROW in r["full_body"]
    assert DIGEST_ROW not in r["new_text"]
    assert "Sounds great, lets do it." in r["new_text"]
    assert r["quoted_history_present"] is True


def test_cut_index_matches_promoted_predicate():
    """Predicate promoted verbatim from agent-inbox-fetch.sh -- pin it."""
    assert cut_index(["a", "On x wrote:", "b"]) == 1
    assert cut_index(["a", "> quoted"]) == 1
    assert cut_index(["a", "--", "sig"]) == 1
    assert cut_index(["a", "b"]) == 2


def test_strip_html_preserves_line_structure():
    out = strip_html("<p>one</p><p>two</p>")
    assert "one" in out and "two" in out
    assert out.index("one") < out.index("two")


# ── CLI contract ────────────────────────────────────────────────────────────

def _run(args, stdin_text):
    return subprocess.run(
        [sys.executable, str(REPO / "core" / "scripts" / "email_body_decode.py")] + args,
        input=stdin_text, capture_output=True, text=True,
    )


def test_cli_json_roundtrip():
    p = _run([], MULTIPART_QUOTED)
    assert p.returncode == 0, p.stderr
    r = json.loads(p.stdout)
    assert DIGEST_ROW in r["full_body"] and DIGEST_ROW not in r["new_text"]


def test_cli_text_slice_selection():
    full = _run(["--format", "text", "--slice", "full"], MULTIPART_QUOTED)
    new = _run(["--format", "text", "--slice", "new"], MULTIPART_QUOTED)
    assert full.returncode == 0 and new.returncode == 0
    assert DIGEST_ROW in full.stdout
    assert DIGEST_ROW not in new.stdout


def test_cli_empty_input_exits_2():
    p = _run([], "   \n")
    assert p.returncode == 2
    assert "empty input" in p.stderr


@pytest.mark.parametrize("fixture", [MULTIPART_QUOTED, PURE_TOP_POST, HTML_ONLY])
def test_shell_wrapper_matches_python(fixture):
    """The .sh wrapper must not change the contract."""
    from _runtime_bash import BASH
    p = subprocess.run(
        [BASH, str(REPO / "core" / "scripts" / "email-body-decode.sh")],
        input=fixture, capture_output=True, text=True, cwd=str(REPO),
    )
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout) == decode(fixture)
