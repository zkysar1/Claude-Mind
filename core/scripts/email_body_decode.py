#!/usr/bin/env python3
"""Decode ONE raw MIME email into BOTH of its readable slices.

Forged from gap-070 (g-115-4763). The capability was hand-rolled three times in
two days, each time differently, and the third encounter revealed the
requirement the first two did not: a caller needs TWO views of the same
message, not one.

  new_text  -- quoted reply history and signature STRIPPED, so a caller can
               isolate what the sender newly wrote.
  full_body -- the COMPLETE decoded body, nothing stripped, so a caller can
               recover a referent that exists ONLY inside the quoted block.

Returning only ``new_text`` is the shape that looks obviously correct and is
the shape that caused a live incident: a top-posted reply ("Sounds great, lets
do it.") carries its referent only in the quoted digest below it, so the
stripped slice alone produced a confident false negative that two user
approvals were unroutable. That was published to the coordination board before
a re-parse recovered both referents immediately. Encoded as guard-2488.

WHY A SHARED PRIMITIVE RATHER THAN A FOURTH DECODER. Three decoders already
existed when this was forged, and every one of them is crippled for this job in
a different way:

  world/scripts/email-read.sh        decodes, then TRUNCATES to [:1500], plain-only
  world/scripts/agent-inbox-fetch.sh full decode + strip, but returns ONLY the
                                     stripped slice, and is agent-inbox-lane scoped
  world/scripts/alert-sweep.sh       has the plain/html fallback the others lack,
                                     but is internal to the sweep and emits no body

So the real gap was never "nothing decodes MIME" -- it was "no path returns the
FULL body of a single message". The walk/quoted-printable/base64/charset and
reply-cut logic here is PROMOTED from agent-inbox-fetch.sh (proven in
production), with three additions: both slices, no truncation, and the
plain/html preference from alert-sweep.sh.

SCOPE: decode only. Fetching the message (S3 keys, buckets, lane cursors) is
domain work and already solved by the three callers above -- pipe their raw
output in. That boundary is what keeps this file domain-free and therefore
git-tracked and portable, per the forge placement fork.

USAGE
    <raw mime on stdin> | py -3 core/scripts/email_body_decode.py
    py -3 core/scripts/email_body_decode.py --file msg.eml
    ... --format text --slice new       # just the new text
    ... --format text --slice full      # just the full body
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re
import sys
from email import message_from_string, policy

# Reply-chain / signature cut markers. Promoted VERBATIM from
# agent-inbox-fetch.sh (the production predicate) rather than re-derived --
# widening it here would silently change behaviour for the agent-inbox lane
# when that script is converted to call this primitive.
_REPLY_HEADER = re.compile(r"^On .+wrote:$")

# HTML handling. Only reached when a message has NO text/plain part at all;
# agent-inbox-fetch.sh returns empty in that case, which is the bug this adds.
_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_BLOCK_BREAK = re.compile(r"(?i)<\s*(br\s*/?|/p|/div|/tr|/li|/h[1-6])\s*>")
_ANY_TAG = re.compile(r"<[^>]+>")
_MANY_BLANKS = re.compile(r"\n{3,}")


def strip_html(raw: str) -> str:
    """Reduce an HTML part to readable text, preserving line structure."""
    if not raw:
        return ""
    text = _SCRIPT_STYLE.sub("", raw)
    text = _BLOCK_BREAK.sub("\n", text)
    text = _ANY_TAG.sub("", text)
    text = _html.unescape(text)
    # Trailing blanks per line, then collapse runs of blank lines.
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return _MANY_BLANKS.sub("\n\n", text).strip()


def extract_body(raw_message: str):
    """Return (body_text, content_type_used, was_html).

    Prefers text/plain and falls back to text/html. ``policy.default`` gives
    ``get_body``, which resolves multipart/alternative correctly; the manual
    walk below is the fallback for messages that defeat it. Quoted-printable
    soft breaks (``=\\n``) are removed by the decoder itself -- an earlier
    encounter reached for ``perl -0pe s/=\\n//g`` only because it was decoding
    in shell without one.
    """
    msg = message_from_string(raw_message, policy=policy.default)

    for want in ("plain", "html"):
        try:
            part = msg.get_body(preferencelist=(want,))
        except Exception:
            part = None
        if part is not None:
            try:
                content = part.get_content()
            except Exception:
                content = None
            if content:
                return (
                    (strip_html(content) if want == "html" else content),
                    f"text/{want}",
                    want == "html",
                )

    # Fallback walk (the agent-inbox-fetch.sh shape) for messages where
    # get_body yields nothing usable.
    if msg.is_multipart():
        for ctype, is_html in (("text/plain", False), ("text/html", True)):
            for part in msg.walk():
                if part.get_content_type() != ctype:
                    continue
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                decoded = payload.decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
                if decoded:
                    return (
                        (strip_html(decoded) if is_html else decoded),
                        ctype,
                        is_html,
                    )
        return "", None, False

    payload = msg.get_payload(decode=True)
    if payload is not None:
        decoded = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    else:
        decoded = msg.get_payload() or ""
    if msg.get_content_type() == "text/html":
        return strip_html(decoded), "text/html", True
    return decoded, msg.get_content_type(), False


def cut_index(lines):
    """Index of the first reply-chain / signature line, or len(lines).

    Predicate promoted verbatim from agent-inbox-fetch.sh. Note ``cut == 0`` is
    a REAL and important outcome, not an error: a pure top-post whose first
    line is already quoted history yields an EMPTY new_text while full_body
    stays populated. That is precisely the case both slices exist for.
    """
    for i, line in enumerate(lines):
        s = line.strip()
        if _REPLY_HEADER.match(s) or s.startswith(">") or s == "--":
            return i
    return len(lines)


def decode(raw_message: str) -> dict:
    body, ctype, was_html = extract_body(raw_message)
    body = body or ""
    # Normalize line endings ONCE, before EITHER slice is derived. Real email is
    # CRLF (RFC 5322), and without this ``full_body`` keeps ``\r\n`` while
    # ``new_text`` is rebuilt by ``"\n".join(...)`` -- so the two slices are not
    # comparable and ``new_text in full_body`` is False on essentially every
    # production message. Found by the mandated fresh-eyes pass on the day this
    # was forged; the suite could not see it because every fixture was written
    # with ``\n`` (guard-920 -- a test must replicate the PRODUCTION shape).
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = body.splitlines()
    cut = cut_index(lines)
    return {
        "new_text": "\n".join(lines[:cut]).strip(),
        "full_body": body.strip(),
        "content_type_used": ctype,
        "was_html": was_html,
        "cut_at_line": cut,
        "total_lines": len(lines),
        "quoted_history_present": cut < len(lines),
        "truncated": False,  # invariant: this primitive never truncates
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Decode one raw MIME email into both its new-text and full-body slices."
    )
    ap.add_argument("--file", help="Read raw MIME from this path (default: stdin)")
    ap.add_argument(
        "--format", choices=("json", "text"), default="json",
        help="json (default, both slices + metadata) or text (one slice)",
    )
    ap.add_argument(
        "--slice", choices=("new", "full", "both"), default="both",
        help="With --format text: which slice to print (default both, labelled)",
    )
    args = ap.parse_args(argv)

    if args.file:
        with open(args.file, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    else:
        raw = sys.stdin.read()

    if not raw.strip():
        print("email_body_decode: empty input -- nothing to decode", file=sys.stderr)
        return 2

    result = decode(raw)

    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.slice == "new":
        print(result["new_text"])
    elif args.slice == "full":
        print(result["full_body"])
    else:
        print("--- NEW TEXT (quoted history stripped) ---")
        print(result["new_text"])
        print()
        print("--- FULL BODY (nothing stripped) ---")
        print(result["full_body"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
