"""GET /v1/changelog/read and /v1/changelog/stats — parity with changelog.py.

The changelog APPEND primitive is already daemonized as a write side-effect
(mind_api/src/changelog.py:append, called by every write endpoint). These two
endpoints daemonize the READ side — the query/stats tools the CLI exposes via
`changelog.py read` / `changelog.py stats`.

Both are WORLD-SCOPED: they read only world/changelog.jsonl (changelog.py:27),
mirroring the CLI's limitation. _fileops.append_changelog writes to multiple
bases (world/meta/agent/.claude per resolve_base_dir) but the CLI query tools
only surface the world changelog — the daemon mirrors that for byte-compat.
No X-Mind-Agent header is needed (the --agent filter on read is a post-read
CONTENT filter, not a path-resolution mechanism).

Byte-compat target is the CLI's STDOUT (not a file): the eventual wrapper cut
prints the response body verbatim, so the body must equal what
`python3 changelog.py read|stats` prints today. That means:
  - read --json:  json.loads(line) -> json.dumps(e, ensure_ascii=False)+"\n"
    per entry (changelog.py:87). NOT a raw echo of the stored ensure_ascii=True
    line — the round-trip converts \\uXXXX escapes to literal UTF-8.
  - read text:    the f-string at changelog.py:90-92 + "\n" per entry.
  - stats:        the exact multi-line format at changelog.py:115-131, with
    collections.Counter.most_common() tie-ordering (insertion order, CPython
    3.7+) and the most_common(10) cap on Top files.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta

from ._jsonl_common import flag


def _path(ctx):
    return ctx.paths.world / "changelog.jsonl"


def _read_entries(path):
    """Mirror changelog.py:read_entries (lines 30-40).

    Read top-to-bottom WITHOUT a lock (matching the CLI) and with NO try/except
    around json.loads: a malformed line raises (propagates to the server's 500
    handler), exactly as the CLI crashes loudly with a traceback + non-zero exit.
    """
    if not path.exists():
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                entries.append(json.loads(stripped))
    return entries


def _parse_duration(duration_str):
    """Mirror changelog.py:parse_duration (lines 43-58): only m/h/d suffixes;
    invalid strings return None and the caller silently skips the filter.

    DELIBERATELY still silent, unlike the sibling in endpoints/board.py, which
    was changed to a 400 by g-115-3775. Do not "fix" this one to match without
    reading the two reasons they diverge (measured 2026-07-29):

      1. PARITY IS PINNED HERE AND ABSENT THERE. core/scripts/changelog.py is a
         LIVE CLI (argparse + __main__, `--since` on both read and stats), this
         endpoint is byte-compat-pinned to it, and
         mind_api/tests/test_runtime_changelog.py::test_stats_invalid_since_no_filter
         asserts the silent skip ON PURPOSE. Changing only this side breaks the
         pin; changing both sides also changes a live CLI's behavior.
         core/scripts/board.py, by contrast, has no `--since` flag at all — its
         parse_duration is dead code — so the board endpoint had no parity
         constraint to honor.
      2. NO LIVE CALLER PASSES A NON-DURATION HERE. A repo-wide census of
         changelog `--since` call shapes found only two usage strings
         ("<duration>]", "<dur>]") and zero real invocations, so the hazard is
         latent. The board hazard was live: post-execution.md Step 1.75d passes
         an ISO timestamp, and Step 1.75e is a fail-closed gate reading the
         result.

    The silent-skip is still the wrong shape and is tracked separately; this
    docstring exists so the divergence reads as a decision, not an oversight.
    """
    if not duration_str:
        return None
    unit = duration_str[-1].lower()
    try:
        value = int(duration_str[:-1])
    except ValueError:
        return None
    if unit == "m":
        return timedelta(minutes=value)
    elif unit == "h":
        return timedelta(hours=value)
    elif unit == "d":
        return timedelta(days=value)
    return None


def read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    entries = _read_entries(_path(ctx))

    # --since (silent-skip on unparseable, matching changelog.py:66-71).
    since = q.get("since")
    if since:
        delta = _parse_duration(since)
        if delta:
            cutoff = datetime.now() - delta
            entries = [e for e in entries
                       if datetime.strptime(e["timestamp"], "%Y-%m-%dT%H:%M:%S") >= cutoff]

    # --agent (exact-match content filter, changelog.py:74-75).
    agent = q.get("agent")
    if agent:
        entries = [e for e in entries if e.get("agent") == agent]

    # --file (substring content filter, changelog.py:78-79).
    file_filter = q.get("file")
    if file_filter:
        entries = [e for e in entries if file_filter in e.get("file", "")]

    # --last (changelog.py:82-83). argparse type=int then `if args.last:`, so a
    # value of 0 is falsy → no slice. Replicate that exact truthiness gate.
    last = q.get("last")
    if last is not None and last != "":
        try:
            n = int(last)
        except ValueError:
            return Response.error(400, "invalid_param", "last must be integer")
        if n:
            entries = entries[-n:]

    out = []
    if flag(q, "json"):
        for e in entries:
            out.append(json.dumps(e, ensure_ascii=False) + "\n")
    else:
        for e in entries:
            summary = f" — {e['summary']}" if e.get("summary") else ""
            lines = f" ({e['lines_changed']} lines)" if e.get("lines_changed") else ""
            out.append(
                f"[{e['timestamp']}] {e.get('agent', '?')} {e.get('action', '?')} "
                f"{e.get('file', '?')}{lines}{summary}\n"
            )
    return Response.text("".join(out), content_type="text/plain")


def stats(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    entries = _read_entries(_path(ctx))

    out = []

    def emit(s: str = "") -> None:
        # Replicate print(): content + "\n" (print() with no args -> just "\n").
        out.append(s + "\n")

    # Empty-FILE early return (changelog.py:99-101) fires BEFORE the --since
    # filter. A file with entries that --since filters to empty does NOT take
    # this path — it proceeds to the full stats format with 0 entries.
    if not entries:
        emit("No changelog entries.")
        return Response.text("".join(out), content_type="text/plain")

    since = q.get("since")
    if since:
        delta = _parse_duration(since)
        if delta:
            cutoff = datetime.now() - delta
            entries = [e for e in entries
                       if datetime.strptime(e["timestamp"], "%Y-%m-%dT%H:%M:%S") >= cutoff]

    agent_counts = Counter(e.get("agent", "unknown") for e in entries)
    file_counts = Counter(e.get("file", "unknown") for e in entries)
    action_counts = Counter(e.get("action", "unknown") for e in entries)

    # period uses the raw `since` directly (changelog.py:115): an unparseable
    # value like "5x" shows "(last 5x)" with NO filtering applied.
    period = f" (last {since})" if since else " (all time)"
    emit(f"Changelog stats{period}: {len(entries)} entries")
    emit()
    emit("By agent:")
    for agent, count in agent_counts.most_common():
        emit(f"  {agent}: {count}")
    emit()
    emit("By action:")
    for action, count in action_counts.most_common():
        emit(f"  {action}: {count}")
    emit()
    emit("Top files:")
    for file, count in file_counts.most_common(10):
        emit(f"  {file}: {count}")
    return Response.text("".join(out), content_type="text/plain")


def register(routes) -> None:
    routes[("GET", "/v1/changelog/read")] = read
    routes[("GET", "/v1/changelog/stats")] = stats
