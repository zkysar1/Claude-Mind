#!/usr/bin/env python3
"""landing-decompose.py: what each compaction landing in a session transcript is made of, and how long until zone=tight.

Read-only, stdlib only. Prints sizes, token counts and times; never a line of the transcript's text.

A transcript is box-local and holds everything its session saw, so a landing can only be measured on the box that has it
(g-353-118 outcome 5): run this there, over that session's own file.

  py -3 core/scripts/landing-decompose.py <transcript.jsonl> [--limit TOKENS] [--last N] [--json]

Transcripts live at ~/.claude/projects/<project-slug>/<session id>.jsonl.

Definitions
  landing     a system/compact_boundary entry.
  first call  the first main-chain assistant entry after it. Its usage row (input + cache_read + cache_creation tokens) is
              the context the model really started from, system prompt and tool schemas included: the number this tool
              exists for.
  buckets     UTF-8 bytes of the model-visible text of the entries between the boundary and the first call (an
              attachment's `rendered` blocks, the compact summary's message text). instructions = the `instructions`
              attachment; invoked_skills = `invoked_skills`; summary = the compact summary; files = `file` and
              `compact_file_reference`; hook = `hook_*`; other = every other entry. The system prompt and tool schemas
              are not in a transcript, so they are in no bucket. `no_text` counts attachment/user entries in that window
              that yielded 0 bytes: if it jumps, the transcript schema moved and the buckets are not to be trusted.
  to tight    minutes from the first call to the first main-chain assistant usage row whose zone is `tight`, before the
              next landing. '-' = the cycle (or the file) ended first. The zone is context-budget-status.classify_zone,
              the status line's own function, so "tight" here is whatever the banner calls tight.
  limit       the autocompact line in tokens: CLAUDE_CODE_AUTO_COMPACT_WINDOW * CLAUDE_AUTOCOMPACT_PCT_OVERRIDE / 100, or
              --limit. No default: an unset limit refuses (rc 2) instead of measuring against an invented one.

Exit: 0 landings measured | 2 usage or no limit | 3 no landing in the file (nothing measured, not a pass).

State the population with any number taken from this (guard-3696): the header line names the file, its entry count and
its landing count, and a result is scoped to that box until a second box repeats it.
"""
import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BUCKETS = ("instructions", "invoked_skills", "summary", "files", "hook", "other")
ATTACHMENT_BUCKET = {
    "instructions": "instructions",
    "invoked_skills": "invoked_skills",
    "file": "files",
    "compact_file_reference": "files",
}
USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
MAX_INSTRUCTION_RUNS = 12


def load_classify_zone():
    """The sensor's own classify_zone, so 'tight' here is whatever the status line calls tight.

    Importing the sensor arms a module-level os._exit(0) timer (it lives milliseconds as a status-line subprocess). A
    parse of a large transcript outlives it, and the process would die with rc 0 and a partial table. Cancel the timer,
    and refuse loudly if it is gone, so a rename fails here instead of silently re-arming it (guard-2138).
    """
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    spec = importlib.util.spec_from_file_location("context_budget_status", os.path.join(HERE, "context-budget-status.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    timer = getattr(mod, "_timer", None)
    if timer is None:
        raise RuntimeError("context-budget-status.py no longer arms _timer: re-read its import side effects (guard-2138)")
    timer.cancel()
    zone = mod.classify_zone
    if (zone(0), zone(60), zone(100)) != ("fresh", "normal", "tight"):
        raise RuntimeError("classify_zone no longer maps 0/60/100 to fresh/normal/tight: the zone names moved")
    return zone


def autocompact_limit(arg):
    if arg:
        return arg
    window = os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    pct = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
    if window and pct:
        return int(window) * int(pct) // 100
    return None


def text_bytes(x):
    """UTF-8 bytes of the text inside a string, a content-block list or a block dict."""
    if isinstance(x, str):
        return len(x.encode("utf-8"))
    if isinstance(x, list):
        return sum(text_bytes(b) for b in x)
    if isinstance(x, dict):
        t = x.get("text")
        if isinstance(t, str):
            return len(t.encode("utf-8"))
        return text_bytes(x.get("content"))
    return 0


def bucket_of(e):
    if e.get("type") == "attachment":
        kind = (e.get("attachment") or {}).get("type") or ""
        if kind in ATTACHMENT_BUCKET:
            return ATTACHMENT_BUCKET[kind]
        return "hook" if kind.startswith("hook_") else "other"
    if e.get("type") == "user" and e.get("isCompactSummary"):
        return "summary"
    return "other"


def entry_bytes(e):
    if e.get("type") == "attachment":
        return text_bytes(e.get("rendered"))
    if e.get("type") == "user":
        return text_bytes((e.get("message") or {}).get("content"))
    return 0


def call_tokens(e):
    """Context tokens of one assistant entry's usage row; None when it carries no real usage."""
    usage = (e.get("message") or {}).get("usage")
    if not isinstance(usage, dict):
        return None
    return sum(int(usage.get(k) or 0) for k in USAGE_KEYS) or None


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def measure(path, limit, zone):
    """One row per compact_boundary, in file order. Returns (rows, entries, unparsable_lines)."""
    rows, cur = [], None
    entries = bad = 0
    with open(path, "rb") as fh:
        for ln in fh:
            try:
                e = json.loads(ln)
            except ValueError:
                bad += 1
                continue
            entries += 1
            kind = e.get("type")
            if kind == "system" and e.get("subtype") == "compact_boundary":
                meta = e.get("compactMetadata") or {}
                cur = {
                    "ts": e.get("timestamp"), "version": e.get("version"), "trigger": meta.get("trigger"),
                    "pre_tokens": meta.get("preTokens"), "post_tokens": meta.get("postTokens"),
                    "buckets": {b: 0 for b in BUCKETS}, "no_text": 0,
                    "first_tokens": None, "first_ts": None, "tight_ts": None, "peak_tokens": 0,
                }
                rows.append(cur)
                continue
            if cur is None or e.get("isSidechain"):
                continue
            if kind == "assistant":
                tokens = call_tokens(e)
                if tokens is None:
                    continue
                if cur["first_tokens"] is None:
                    cur["first_tokens"], cur["first_ts"] = tokens, e.get("timestamp")
                cur["peak_tokens"] = max(cur["peak_tokens"], tokens)
                if cur["tight_ts"] is None and zone(min(100.0, tokens / limit * 100)) == "tight":
                    cur["tight_ts"] = e.get("timestamp")
            elif cur["first_tokens"] is None:
                size = entry_bytes(e)
                cur["buckets"][bucket_of(e)] += size
                if size == 0 and kind in ("attachment", "user"):
                    cur["no_text"] += 1
    for r in rows:
        r["total_bytes"] = sum(r["buckets"].values())
        r["of_autocompact_pct"] = round(r["first_tokens"] / limit * 100, 1) if r["first_tokens"] else None
        r["to_tight_min"] = None
        if r["first_ts"] and r["tight_ts"]:
            r["to_tight_min"] = round((parse_ts(r["tight_ts"]) - parse_ts(r["first_ts"])).total_seconds() / 60, 1)
    return rows, entries, bad


def pctl(vals, f):
    v = sorted(vals)
    return v[min(len(v) - 1, int(f * len(v)))] if v else None


def instruction_runs(rows):
    """Consecutive landings with the same instructions bytes, as (bytes, first #, last #)."""
    runs = []
    for i, r in enumerate(rows, 1):
        b = r["buckets"]["instructions"]
        if runs and runs[-1][0] == b:
            runs[-1][2] = i
        else:
            runs.append([b, i, i])
    return runs


def summarize(rows):
    firsts = [r["first_tokens"] for r in rows if r["first_tokens"]]
    tights = [r["to_tight_min"] for r in rows if r["to_tight_min"] is not None]
    return {
        "landings": len(rows),
        "with_first_call": len(firsts),
        "first_call_tokens": {k: pctl(firsts, f) for k, f in (("min", 0), ("p25", .25), ("median", .5), ("p75", .75), ("max", .999999))},
        "reached_tight": len(tights),
        "to_tight_min": {k: pctl(tights, f) for k, f in (("min", 0), ("median", .5), ("max", .999999))},
        "no_text_entries": sum(r["no_text"] for r in rows),
        "instruction_runs": instruction_runs(rows),
    }


def render(rows, entries, bad, limit, path, last):
    out = ["population: %s | %d entries, %d unparsable | %d landings | first %s, last %s | limit %d tokens" % (
        os.path.basename(path), entries, bad, len(rows), (rows[0]["ts"] or "-")[:19], (rows[-1]["ts"] or "-")[:19], limit)]
    out.append("%4s %-19s %-8s %8s %6s %8s %8s %8s %7s %6s %7s %8s %8s %8s %7s" % (
        "#", "first call (UTC)", "version", "call_tok", "of_ac%", "instr_B", "skills_B", "summ_B", "files_B", "hook_B",
        "other_B", "total_B", "meta_post", "to_tight", "peak_tok"))
    shown = rows if not last else rows[-last:]
    start = len(rows) - len(shown) + 1
    for i, r in enumerate(shown, start):
        b = r["buckets"]
        out.append("%4d %-19s %-8s %8s %6s %8d %8d %8d %7d %6d %7d %8d %8s %8s %8d" % (
            i, (r["first_ts"] or r["ts"] or "-")[:19], str(r["version"])[:8], r["first_tokens"] or "-",
            r["of_autocompact_pct"] if r["of_autocompact_pct"] is not None else "-", b["instructions"],
            b["invoked_skills"], b["summary"], b["files"], b["hook"], b["other"], r["total_bytes"],
            r["post_tokens"] if r["post_tokens"] is not None else "-",
            r["to_tight_min"] if r["to_tight_min"] is not None else "-", r["peak_tokens"]))
    s = summarize(rows)
    f, t = s["first_call_tokens"], s["to_tight_min"]
    out.append("summary: %d landings, %d with a first call; call_tok min/p25/median/p75/max = %s/%s/%s/%s/%s" % (
        s["landings"], s["with_first_call"], f["min"], f["p25"], f["median"], f["p75"], f["max"]))
    out.append("         reached tight before the next landing: %d of %d; to_tight min/median/max = %s/%s/%s min" % (
        s["reached_tight"], s["landings"], t["min"], t["median"], t["max"]))
    out.append("         no_text entries in the landing windows: %d" % s["no_text_entries"])
    runs = s["instruction_runs"]
    out.append("         instructions bytes by run (bytes x landings #first-#last): " + "; ".join(
        "%d x%d #%d-#%d" % (b, hi - lo + 1, lo, hi) for b, lo, hi in runs[:MAX_INSTRUCTION_RUNS])
        + ("; +%d more runs" % (len(runs) - MAX_INSTRUCTION_RUNS) if len(runs) > MAX_INSTRUCTION_RUNS else ""))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="size and time-to-tight of every compaction landing in a session transcript")
    ap.add_argument("transcript")
    ap.add_argument("--limit", type=int, help="autocompact line in tokens (default: window x pct from the environment)")
    ap.add_argument("--last", type=int, default=0, help="print only the last N landing rows (the summary covers all)")
    ap.add_argument("--json", action="store_true", help="print one JSON document instead of the table")
    args = ap.parse_args()
    limit = autocompact_limit(args.limit)
    if not limit or limit <= 0:
        print("no autocompact limit: set CLAUDE_CODE_AUTO_COMPACT_WINDOW and CLAUDE_AUTOCOMPACT_PCT_OVERRIDE or pass --limit", file=sys.stderr)
        return 2
    if not os.path.isfile(args.transcript):
        print("not a file: %s" % args.transcript, file=sys.stderr)
        return 2
    rows, entries, bad = measure(args.transcript, limit, load_classify_zone())
    if not rows:
        print("no compact_boundary in %s (%d entries): nothing measured" % (os.path.basename(args.transcript), entries), file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps({"file": os.path.basename(args.transcript), "entries": entries, "unparsable": bad, "limit": limit,
                          "landings": rows, "summary": summarize(rows)}))
    else:
        print(render(rows, entries, bad, limit, args.transcript, args.last))
    return 0


if __name__ == "__main__":
    sys.exit(main())
