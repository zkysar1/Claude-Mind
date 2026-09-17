"""GET /v1/board/read — parity with `board.py read`.

Required:
    channel=<name>

Optional filters (all combinable):
    since=<duration|timestamp>
                         duration  e.g. 1h, 30m, 2d
                         timestamp e.g. 2026-07-29T00:45:45
                         an unparseable value is a 400, never a silent no-op
                         (g-115-3775)
    author=<name>
    tag=<tag>
    type=<message-type>
    last=<N>             show only last N messages after filters
    json=1               output as JSONL (one message per line)
                          default: human-readable [ts] author (type): text ...
    unread_only=1        filter out messages already seen by the requesting agent
    mark_read=1          after returning messages, append their IDs to the
                          per-channel reads sidecar so subsequent reads skip them

Each channel is its own JSONL file under <world>/board/<channel>.jsonl.
Empty/missing channel produces a plain-text "Channel '<name>' is empty
or does not exist." line, matching the CLI.

Equivalence target: stdout of `python3 core/scripts/board.py read --channel ... [...]`.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from ..jsonl_cache import cache
from ..agent_paths import assert_not_cruft  # also puts core/scripts on sys.path

# READER-SIDE PATH-LIST SEAM (). Imported AFTER agent_paths, which is
# what adds core/scripts/ to sys.path at module load. One definition of "which
# files make up a channel", shared by every reader, so a future segmented writer
# cannot silently starve one of them.
import _board_paths  # noqa: E402


def _channel_path(ctx, channel: str):
    return ctx.paths.world / "board" / f"{channel}.jsonl"


# --- Archive reach () ----------------------------------------------
#
# Rotation MOVES posts to <channel>-archive.jsonl and deletes nothing, but this
# endpoint read only the live file — so any `since` window older than the live
# file's earliest retained post was cut SILENTLY. Measured 2026-09-16 (bravo,
# cc-05): fresh-eyes-program asks coordination for 60 days and was getting ~8.2;
# findings' live window is 21.4 days against 30d/60d readers. guard-6252 (a
# msg-id search returning zero is a window artifact) is the same defect from the
# search side.
#
# THE ARCHIVE IS READ FROM ITS TAIL, AND THAT IS NOT AN OPTIMISATION — it is the
# only shape whose cost scales with the REQUEST instead of with archive size
# (rb-3803). coordination-archive.jsonl is 34 MB today and only grows. Rotation
# APPENDS, so the tail holds the NEWEST archived posts, which is exactly the
# window adjacent to the live file's earliest post — the direction a widened
# `since` reaches into first.
#
# THE TAIL EXTENDS WITH THE REQUEST (). A fixed tail made the reachable
# window "live bytes + 4 MiB", so lowering a channel's rotate cap still shortened
# every long-window reader (findings at 2800 lines: 21.2d against a 30d reader).
# The read now steps backward one budget at a time until the record at the read
# boundary predates the requested cutoff — so a 30d request costs what 30 days
# of archive weighs — under a HARD ceiling of budget x _ARCHIVE_TAIL_MAX_STEPS.
_ARCHIVE_TAIL_BYTES_DEFAULT = 4 * 1024 * 1024   # 4 MiB
_ARCHIVE_TAIL_MAX_STEPS = 8                      # ceiling 32 MiB at the default


def _archive_tail_budget() -> int:
    """Byte budget for one archive tail read. Env-overridable for tests."""
    raw = os.environ.get("BOARD_ARCHIVE_TAIL_BYTES")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return _ARCHIVE_TAIL_BYTES_DEFAULT


def _archive_path(ctx, channel: str):
    return ctx.paths.world / "board" / f"{channel}-archive.jsonl"


# --- Segment freshness and discovery ( U3) --------------------------
#
# The base file is refreshed on every read (jsonl_cache.get -> ensure_local), but
# channel_paths() enumerates segments off LOCAL disk. On own-cloud, a segment a
# PEER minted reaches this box only when pull_sweep's LIST materialises it, and
# pull_sweep runs every OWNCLOUD_PULL_EVERY_N push ticks (default 5 x 120s, about
# 10 min). A segment already on disk is refreshed on that same cadence and no
# faster. So without this step a reader could miss up to ~10 min of a peer's
# posts, while the base file beside them is at most one cache TTL old.
#
# ONLY TODAY'S AND YESTERDAY'S NAMES ARE REFRESHED, and that bound is deliberate.
# Those are the only segments that still take appends: today's, plus yesterday's
# for posts written or merged around the UTC day boundary. An older segment is
# closed by its date, and pull_sweep still covers it. Refreshing every enumerated
# segment would make the cost grow with retention, not with the request.
#
# A NAME ABSENT FROM THE STORE IS RE-PROBED AT MOST ONCE PER CACHE TTL. The
# backend's TTL shortcut needs a local file, so without this throttle every read
# before the first segment is minted would pay two HEADs that find nothing.
_SEGMENT_ABSENT_PROBED_AT: dict = {}


def _segment_probe_ttl() -> float:
    """How long a store-absent segment name stays un-re-probed: the backend's own
    cache TTL. A backend without one (local) makes the refresh free, so nothing
    needs throttling there. On error, return 0: probing every read is correct,
    only more expensive."""
    try:
        from storage_backend import get_backend
        return float(getattr(get_backend(), "cache_ttl", 0) or 0)
    except Exception:
        return 0.0


def _refresh_recent_segments(board_dir, channel: str, today=None):
    """Refresh today's and yesterday's date segments of `channel` from the store,
    materialising either one that exists remotely but not yet locally.

    MUST run BEFORE channel_paths(), which lists local disk only. Returns one
    "<name>: <error>" string per refresh that failed. Fail-open like the archive
    refresh: the read still serves the local copies, but the caller must not
    claim the window is covered."""
    import time
    from storage_backend import ensure_local_before_append

    today = today or datetime.now().date()
    ttl = _segment_probe_ttl()
    errors = []
    for day in (today - timedelta(days=1), today):
        path = Path(board_dir) / _board_paths.segment_name(channel, day)
        key = str(path)
        if not path.exists():
            probed = _SEGMENT_ABSENT_PROBED_AT.get(key)
            if probed is not None and time.monotonic() - probed < ttl:
                continue
        err = ensure_local_before_append(path, op="board_segment_refresh")
        if err:
            errors.append(f"{path.name}: {err}")
        elif path.exists():
            _SEGMENT_ABSENT_PROBED_AT.pop(key, None)
        else:
            _SEGMENT_ABSENT_PROBED_AT[key] = time.monotonic()
    return errors


def _read_archive_tail(path, budget: int, cutoff=None):
    """Parse the tail of an archive file: the last `budget` bytes, or with
    `cutoff`, as many `budget`-sized steps back as it takes for the record at
    the read boundary to be at or before `cutoff` — never more than
    budget x _ARCHIVE_TAIL_MAX_STEPS bytes (g-358-118).

    Returns (records, truncated, bytes_read). `truncated` is True when the file
    was larger than what was read — i.e. there are OLDER archived posts this
    read did not look at. The caller MUST surface that: a partial window
    presented as a whole one is the very defect this function exists to fix.

    Deliberately does NOT use jsonl_cache: caching a 34 MB archive to serve a
    tail would hand the memory cost straight back.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return [], False, 0
    ceiling = budget * (_ARCHIVE_TAIL_MAX_STEPS if cutoff is not None else 1)
    want = budget
    try:
        with open(path, "rb") as fh:
            while want < size and want < ceiling:
                fh.seek(size - want)
                fh.readline()   # the partial record at the seek point
                try:
                    ts = _parse_ts(json.loads(fh.readline()).get("timestamp"))
                except (ValueError, AttributeError):
                    ts = None
                if ts is not None and ts <= cutoff:
                    break
                want = min(want + budget, ceiling)
            if size <= want:
                start, truncated = 0, False
            else:
                start, truncated = size - want, True
            fh.seek(start)
            blob = fh.read()
    except OSError:
        return [], False, 0
    if truncated:
        # The seek landed mid-record; drop the partial first line.
        nl = blob.find(b"\n")
        blob = b"" if nl < 0 else blob[nl + 1:]
    recs = []
    for line in blob.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            recs.append(obj)
    return recs, truncated, len(blob)


def _reads_sidecar_path(ctx, channel: str):
    """Per-channel read-tracking sidecar: world/board/<channel>-reads.jsonl.

    Mirrors board.py:reads_sidecar_path (line 318-320).
    """
    return ctx.paths.world / "board" / f"{channel}-reads.jsonl"


def _load_seen_set(ctx, channel: str, agent: str) -> set:
    """Load msg_ids already read by `agent` from the sidecar.

    Mirrors board.py:_load_read_msg_ids (lines 375-400). Fail-open: returns
    empty set on any error so unread_only never blocks the read path.
    """
    sidecar = _reads_sidecar_path(ctx, channel)
    if not sidecar.exists():
        return set()
    seen = set()
    try:
        with open(sidecar, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("reader_agent") == agent:
                    mid = row.get("msg_id")
                    if mid:
                        seen.add(mid)
    except OSError:
        return set()
    return seen


def _mark_read_append(ctx, channel: str, agent: str, messages: list, seen: set):
    """Append unseen message IDs to the reads sidecar.

    Mirrors board.py cmd_read mark_read block (lines 276-296). Fail-open:
    errors writing the sidecar are logged to stderr but do NOT block the read.
    """
    sidecar = _reads_sidecar_path(ctx, channel)
    try:
        assert_not_cruft(sidecar.parent, "mkdir (board reads sidecar)")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        # <channel>-reads.jsonl is an S3-backed governed store (registered in
        # coordination_merge._HANDLERS), and an append never reads the file — so
        # without this nothing on the mark-read path ever pulls it and the append
        # extends a stale mirror (). Fail-open by return value, never
        # by raise; this whole block is already best-effort by design.
        from storage_backend import ensure_local_before_append
        ensure_local_before_append(sidecar)
        read_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with open(sidecar, "a", encoding="utf-8") as f:
            for m in messages:
                mid = m.get("id")
                if not mid or mid in seen:
                    continue
                row = json.dumps({
                    "msg_id": mid,
                    "reader_agent": agent,
                    "reader_sid": "",
                    "read_at": read_at,
                }, ensure_ascii=False)
                f.write(row + "\n")
                seen.add(mid)
    except Exception as e:
        import sys
        print(f"[board.read] WARN: mark_read append failed: {e}", file=sys.stderr)


def _parse_duration(s: str):
    """e.g. '1h' -> timedelta(hours=1). Returns None on parse failure."""
    if not s:
        return None
    unit = s[-1].lower()
    try:
        value = int(s[:-1])
    except ValueError:
        return None
    if unit == "m":
        return timedelta(minutes=value)
    if unit == "h":
        return timedelta(hours=value)
    if unit == "d":
        return timedelta(days=value)
    return None


def _parse_since(s: str, now=None):
    """Resolve a `since` query value to an absolute cutoff datetime.

    Two accepted shapes:
      - relative duration  ``<int><m|h|d>``            e.g. ``24h``, ``30m``, ``7d``
      - absolute timestamp ``YYYY-MM-DDTHH:MM:SS``     the naive form every
        framework caller mints via ``date +%Y-%m-%dT%H:%M:%S``

    Returns the cutoff datetime, or None when the value matches NEITHER shape.

    Callers MUST treat None as a client error. Do NOT restore the old
    ``if delta:`` gate, which skipped the whole filter block on an unparseable
    value and returned the entire channel at HTTP 200 — a zero-signal read
    presented as a scoped one (verify-before-assuming.md rule 4). Measured on
    the live daemon 2026-07-29 (alpha, cc-04), same channel seconds apart:
    ``--since 2026-01-01T00:00:00`` returned 5069 lines, ``--since 1h``
    returned 13.

    The timestamp shape is added here rather than removed from the caller
    because ``world/conventions/post-execution.md`` Step 1.75a mints an absolute
    instant on purpose — "findings posted since I started this goal" is not
    expressible as a whole-unit duration without rounding and a race.

    Note the ``is not None`` below is load-bearing and NOT a style choice: the
    old truthiness test ALSO swallowed a legitimately-parsed ``0h``
    (``timedelta(0)`` is falsy), silently widening the window to unbounded.
    That second silent drop is not hypothetical — ``infra-streak-notify.sh:393``
    carries a ceil() specifically because an accidental ``0h`` board query
    "re-permitted the cross-claimant double email" (g-249-33). Truthiness on a
    parse result conflates "no value" with "a valid zero". (g-115-3775)
    """
    if not s:
        return None
    now = now or datetime.now()
    delta = _parse_duration(s)
    if delta is not None:
        return now - delta
    return _parse_ts(s)


def read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    channel = (q.get("channel") or "").strip()
    if not channel:
        return Response.error(400, "missing_param",
                              "query parameter 'channel' is required")

    ch_path = _channel_path(ctx, channel)
    # Refresh the two segments that still take appends BEFORE enumerating, so a
    # peer's segment that exists only in the store is on disk when
    # channel_paths() lists it ( U3; see _refresh_recent_segments).
    segment_note = None
    _seg_errors = _refresh_recent_segments(ch_path.parent, channel)
    if _seg_errors:
        segment_note = (f"segment refresh FAILED ({'; '.join(_seg_errors)}) — local "
                        f"segment(s) may be stale, window NOT verified covered")
        print(f"[board/read] WARN channel={channel}: {segment_note}", file=sys.stderr)

    # PATH-LIST SEAM (). The "live half" of a channel is its base file
    # PLUS any date segments — a set that CHANGES OVER TIME once a segmented
    # writer lands. Enumerating it here, fresh on every read, is what makes this
    # endpoint safe BEFORE that writer exists: today `channel_paths` returns
    # exactly `[<channel>.jsonl]`, so this is byte-identical to the previous
    # behaviour, and it stays correct the day a segment appears. The archive is
    # excluded here and handled below on its own tail-read budget — a window that
    # lies inside the live half must still never open the 46 MB archive.
    live_paths = _board_paths.channel_paths(ch_path.parent, channel,
                                            include_archive=False)
    if not live_paths:
        # Match the CLI: prints a single human-readable line. JSON mode still
        # gets the same text — the CLI doesn't branch on json_output here.
        return Response.text(
            f"Channel '{channel}' is empty or does not exist.",
            content_type="text/plain",
        )

    live = []
    seam_missing = []
    for _lp in live_paths:
        if _lp == ch_path:
            # Hot path unchanged: the base file keeps its mtime-keyed cache.
            live.extend(cache().get(_lp))
        else:
            # Segments go through read_paths, NOT the cache, because
            # jsonl_cache.get() returns [] for a missing file. Under a rolling
            # window that silence is the false all-clear this seam exists to
            # prevent: an evicted segment would read as an empty one, and the
            # footer would assert a window it did not cover.
            _recs, _miss = _board_paths.read_paths([_lp])
            live.extend(_recs)
            seam_missing.extend(_miss)
    messages = live
    archive_note = None
    archive_unverified = False

    since = q.get("since")
    if since:
        cutoff = _parse_since(since)
        if cutoff is None:
            return Response.error(
                400, "invalid_param",
                "query parameter 'since' must be a duration (<int><m|h|d>, "
                "e.g. '24h') or a timestamp (YYYY-MM-DDTHH:MM:SS); "
                f"got {since!r}")

        # ARCHIVE REACH (). Consult the archive ONLY when the requested
        # cutoff predates the live file's earliest retained post. A window that
        # lies inside the live file must not stat, open or read the archive at
        # all — that is what keeps the common read exactly as cheap as before.
        _live_stamps = [t for t in (_parse_ts(m.get("timestamp")) for m in live) if t]
        _earliest_live = min(_live_stamps) if _live_stamps else None
        if _earliest_live is None or cutoff < _earliest_live:
            arch_path = _archive_path(ctx, channel)
            # REFRESH BEFORE THE TAIL READ (). *-archive.jsonl is excluded
            # from the own-cloud eager pull (), which relies on reads going
            # through ensure_local; this read opened the local file directly, so a
            # box that did not run the channel's last rotation served a FROZEN
            # archive (cc-02 findings: 8,075 local ids against 10,218 in the store).
            # Cheap by construction: board/* is plaintext and range-tail eligible,
            # so a stale mirror pulls only the appended bytes, and a current one
            # costs at most a HEAD per cache TTL. It stays inside this branch, so an
            # in-window read still never touches the archive. Fail-open by return
            # value: a failed refresh still serves the local copy, but the reply must
            # not claim the window is covered.
            from storage_backend import ensure_local_before_append
            arch_refresh_err = ensure_local_before_append(arch_path, op="board_archive_refresh")
            if arch_refresh_err:
                archive_unverified = True
                archive_note = (f"archive refresh FAILED ({arch_refresh_err}) — local "
                                f"archive may be stale, window NOT verified covered")
                print(f"[board/read] WARN channel={channel} since={since}: "
                      f"{archive_note}", file=sys.stderr)
            if arch_path.exists():
                budget = _archive_tail_budget()
                arch_recs, arch_truncated, arch_bytes = _read_archive_tail(arch_path, budget, cutoff)
                # DEDUP BY id, LIVE WINS (guard-3523). The archive holds
                # re-archived duplicates of its own —  measured 4,728 on
                # coordination — so the archive side is deduped against itself
                # too, keeping the FIRST (oldest-positioned) copy.
                live_ids = {m.get("id") for m in live if m.get("id")}
                seen_arch = set()
                extra = []
                for m in arch_recs:
                    mid = m.get("id")
                    if mid and (mid in live_ids or mid in seen_arch):
                        continue
                    if mid:
                        seen_arch.add(mid)
                    extra.append(m)
                extra.sort(key=lambda m: m.get("timestamp") or "")
                # Prepending keeps the merged list chronological AND leaves
                # the live half in its original file order, because rotation
                # moves the OLDEST posts out. Measured 2026-09-16 on both
                # rotated channels: coordination archive ends 2026-08-29 against
                # a live file starting 2026-09-07; findings ends 2026-08-14
                # against 2026-08-26. Should that ever stop holding, the cost is
                # display ORDER only — the `>= cutoff` filter below still admits
                # exactly the requested window.
                messages = extra + live
                _arch_stamps = sorted(m.get("timestamp", "") for m in extra if m.get("timestamp"))
                _oldest_arch = _arch_stamps[0] if _arch_stamps else None
                # A truncated tail whose OLDEST record is still newer than the
                # requested cutoff means the window is NOT fully covered. Say so
                # — silently returning the covered part is the same class of
                # wrong answer as the bug being fixed.
                _short = bool(arch_truncated and _oldest_arch
                              and _parse_ts(_oldest_arch)
                              and _parse_ts(_oldest_arch) > cutoff)
                archive_note = (
                    (archive_note + "; " if archive_note else "")
                    + f"archive={len(extra)} record(s) merged, tail {arch_bytes}B"
                    f" of budget {budget * _ARCHIVE_TAIL_MAX_STEPS}B"
                    + (f", oldest {_oldest_arch}" if _oldest_arch else "")
                    + (", TRUNCATED AT BUDGET — window NOT fully covered" if _short
                       else (", tail truncated (older posts unread, but the "
                             "requested window is covered)" if arch_truncated else ""))
                )
                if _short:
                    print(f"[board/read] WARN channel={channel} since={since}: "
                          f"{archive_note}. Raise BOARD_ARCHIVE_TAIL_BYTES or "
                          f"narrow --since.", file=sys.stderr)

        messages = [
            m for m in messages
            if _parse_ts(m.get("timestamp")) and _parse_ts(m.get("timestamp")) >= cutoff
        ]

    author = q.get("author")
    if author:
        messages = [m for m in messages if m.get("author") == author]

    msg_type = q.get("type")
    if msg_type:
        messages = [m for m in messages if m.get("type") == msg_type]

    tag = q.get("tag")
    if tag:
        messages = [m for m in messages if tag in (m.get("tags") or [])]

    last_raw = q.get("last")
    if last_raw:
        try:
            last_n = int(last_raw)
        except ValueError:
            return Response.error(400, "invalid_param", "last must be integer")
        messages = messages[-last_n:]

    # T1.7: --unread-only / --mark-read parity (board.py lines 245-296).
    from ._jsonl_common import flag as _flag
    unread_only = _flag(q, "unread_only")
    mark_read = _flag(q, "mark_read")
    current_agent = ctx.paths.agent_name or "unknown"
    seen = (_load_seen_set(ctx, channel, current_agent)
            if (unread_only or mark_read) else set())
    if unread_only:
        messages = [m for m in messages if m.get("id") not in seen]

    as_json = (q.get("json") or "").lower() not in ("", "0", "false", "no")

    if as_json:
        # CLI prints one JSON object per line.
        if mark_read and messages:
            _mark_read_append(ctx, channel, current_agent, messages, seen)
        lines = [json.dumps(m, ensure_ascii=False) for m in messages]
        return Response.text("\n".join(lines), content_type="application/json")

    # Human-readable. Mirror the CLI exactly: two lines per message + blank line.
    out = []
    for msg in messages:
        tags = (msg.get("tags") or [])
        tags_str = f" [{', '.join(tags)}]" if tags else ""
        reply = f" (reply to {msg['reply_to']})" if msg.get("reply_to") else ""
        mtype = msg.get("type", "status")
        type_label = f" ({mtype})" if mtype and mtype != "status" else ""
        out.append(
            f"[{msg.get('timestamp', '?')}] {msg.get('author', '?')}{type_label}: "
            f"{msg.get('text', '')}{tags_str}{reply}"
        )
        out.append(f"  id: {msg.get('id', '')}")
        out.append("")

    # Filter-provenance footer ( follow-up). A filtered EMPTY result is
    # indistinguishable from "the record is not there" unless the reply states the
    # window it actually covered: a query for an 05:07 post against a --last slice
    # that begins 17:52 returns a clean, wrong zero. Printing the realized extent
    # beside the count makes a wrong-WINDOW zero self-refuting the same way a byte
    # count makes a wrong-PARSER zero self-refuting (guard-2298). The generalisation
    # is that a positive control sharing the TOOL but not the FILTER cannot detect a
    # filter miss, so the filter has to report itself.
    # HUMAN OUTPUT ONLY, deliberately: the JSON branch above returns one object per
    # line and returns early, so a non-JSON footer can never reach a parser (the
    # receipt-inside-a-JSONL-store defect, archive-before-delete.md step 6).
    _filters = ["channel=" + str(channel)]
    for _k in ("since", "author", "tag", "type", "last"):
        _v = q.get(_k)
        if _v:
            _filters.append(f"{_k}={_v}")
    if unread_only:
        _filters.append("unread_only=1")
    if archive_note:
        _filters.append(archive_note)
    if segment_note:
        _filters.append(segment_note)
    # DISCONTINUITY, not merely path enumeration (). "window covered
    # X .. Y" is computed from oldest/newest below, so it asserts coverage ACROSS
    # a hole. Enumerating paths cannot detect a MISSING segment — that is the
    # eviction case seen from the other side — and one day-bucket pass is the
    # only check that can tell a clean hand-off from two stores that have
    # diverged past each other (guard-6871). Empty string on a healthy read, so
    # this adds nothing to the common footer.
    _seam_note = _board_paths.coverage_note(messages, seam_missing)
    if _seam_note:
        _filters.append(_seam_note)
    _fstr = " ".join(_filters)
    if messages:
        _stamps = sorted(m.get("timestamp", "") for m in messages if m.get("timestamp"))
        _extent = f"{_stamps[0]} .. {_stamps[-1]}" if _stamps else "unknown"
        _failed = [name for name, bad in (("archive refresh failed", archive_unverified),
                                          ("segment refresh failed", bool(segment_note)))
                   if bad]
        if _failed:
            out.append(f"-- {len(messages)} message(s); extent {_extent} NOT verified "
                       f"covered ({', '.join(_failed)}); filters: {_fstr}")
        else:
            out.append(f"-- {len(messages)} message(s); window covered {_extent}; filters: {_fstr}")
    else:
        out.append(f"-- 0 messages matched; filters: {_fstr}")
        out.append("   (empty is scoped to these filters - not proof the record is absent)")

    # T1.7: mark_read AFTER building output (mirrors CLI: display then mark).
    if mark_read and messages:
        _mark_read_append(ctx, channel, current_agent, messages, seen)

    return Response.text("\n".join(out), content_type="text/plain")


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None


def register(routes) -> None:
    routes[("GET", "/v1/board/read")] = read
