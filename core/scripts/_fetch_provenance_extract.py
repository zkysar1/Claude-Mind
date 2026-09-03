r"""Extract provenance records from a PostToolUse[WebFetch|WebSearch] payload.

Reads the hook JSON on stdin, writes the session id on line 1 and then one
`kind<TAB>value` record per line.

WHY THIS IS A FILE AND NOT AN INLINE `python3 -c` IN THE HOOK. The URL-scanning
regex needs a character class containing both quote characters and backslashes.
Embedded in a double-quoted bash string that is unwritable: bash rewrites `\"`
to `"` and leaves `\s` alone, so the program python receives is not the program
that was authored — and because a PostToolUse hook swallows stderr, the result
is a hook that exits 0 and silently records nothing (measured, g-357-43). A
separate file passes the bytes through untouched and is directly testable.

RESULT_CAP bounds the work one tool call can cause.
"""
import json
import os
import re
import sys

# Deliberately permissive on the left, trimmed on the right: match to the first
# whitespace, then peel the delimiters and punctuation that commonly abut a URL
# inside serialized JSON or prose.
_URL_RE = re.compile(r"https?://\S+")
_TRAILING = "\"'<>)]},.;:\\"


def extract(payload):
    """(session_id, [(kind, value), ...]) — pure, so it can be unit-tested."""
    ti = payload.get("tool_input") or {}
    tool = (payload.get("tool_name") or "").lower()
    sid = payload.get("session_id") or ""
    out = []

    url = (ti.get("url") or "").strip()
    if url:
        out.append(("url", url))

    query = (ti.get("query") or "").strip()
    if query:
        out.append(("search", query))

    # Result URLs live in the RESPONSE, not the input — that is the half a
    # citation would actually quote. The response shape varies by tool version,
    # so scan the serialized blob rather than assuming a key path.
    resp = payload.get("tool_response")
    if resp is not None and tool.startswith("websearch"):
        try:
            blob = resp if isinstance(resp, str) else json.dumps(resp)
        except (TypeError, ValueError):
            blob = ""
        try:
            cap = int(os.environ.get("RESULT_CAP", "10"))
        except ValueError:
            cap = 10
        seen = set()
        for m in _URL_RE.finditer(blob):
            candidate = m.group(0).rstrip(_TRAILING)
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            out.append(("url", candidate))
            if len(seen) >= cap:
                break

    cleaned = []
    for kind, value in out:
        value = str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()
        if value:
            cleaned.append((kind, value))
    return sid, cleaned


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    sid, records = extract(payload)
    print(sid)
    for kind, value in records:
        print(f"{kind}\t{value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
