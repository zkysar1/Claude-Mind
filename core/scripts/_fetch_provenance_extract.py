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

    # BASH-DERIVED FETCH (). The hook this module serves bound only to
    # WebFetch/WebSearch, so a page pulled with `curl` from a Bash call left NO
    # record — while the session-level instruction MANDATES preferring Bash. So
    # obeying the instruction GUARANTEED a Q4 `decorative-citation` on a URL the
    # session genuinely fetched. Measured ( occurrence 10): three URLs
    # fetched live with curl, HTTP 200 and a 49,276-byte body, all three reported
    # "cited but NOT retrieved this session" — a live network GET is the most
    # literal act of retrieval available, and the manifest scored it decorative.
    # guard-4407 already recorded the scope limit honestly; this CLOSES it rather
    # than teaching the consumer to ignore its own answer, which keeps the alarm
    # (a fabricated URL still has no record) instead of demoting the check.
    #
    # A FETCH VERB IS REQUIRED, and that requirement is the whole safety of this
    # branch. Recording every URL that merely APPEARS in a command would let
    # `echo "https://fabricated.example"` register as a retrieval — turning this
    # recorder into a laundering channel for exactly the fabricated citation Q4
    # exists to catch (guard-1901: alarm suppression is the one direction this
    # must never fail in). Requiring curl/wget bounds the claim to "this session
    # ran a fetch command naming this URL".
    #
    # RESIDUAL LIMIT, stated because an unstated limit gets read as total
    # (guard-1760): this records the ATTEMPT, not a 200. A curl that resolved
    # nothing still records. That is the same bar the WebFetch branch above
    # meets, and it is weaker than "the content supports the claim" — which no
    # provenance manifest has ever answered.
    #
    # QUERY AND FRAGMENT ARE STRIPPED — guard-2426, not tidiness. A command line
    # is precisely where credentials ride along as `?token=...`, and this value
    # lands in a durable manifest. It costs nothing: `retrieved_predicate`
    # substring-matches in BOTH directions, so a recorded `https://h/p` still
    # matches a citation of `https://h/p?token=secret`.
    if tool.startswith("bash"):
        command = ti.get("command") or ""
        if isinstance(command, str) and re.search(r"\b(?:curl|wget)\b", command):
            try:
                cap = int(os.environ.get("RESULT_CAP", "10"))
            except ValueError:
                cap = 10
            seen_cmd = set()
            for m in _URL_RE.finditer(command):
                candidate = m.group(0).rstrip(_TRAILING)
                candidate = candidate.split("#", 1)[0].split("?", 1)[0]
                if not candidate or candidate in seen_cmd:
                    continue
                seen_cmd.add(candidate)
                out.append(("url", candidate))
                if len(seen_cmd) >= cap:
                    break

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
