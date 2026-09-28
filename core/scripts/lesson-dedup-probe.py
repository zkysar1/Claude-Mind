#!/usr/bin/env python3
"""lesson-dedup-probe.py — batch dedup for the LESSON half of a worker spark
replay drain (g-115-10749, gap-237).

Problem: sq013-dedup-probe.py --subjects-file batches the WORK half of a
replay drain (gap-162). The LESSON half had no tool. Every lesson candidate was
checked by hand, in two stages:
  - a provenance join (guard-7379);
  - two read-only retrieves, one phrased as the SUBJECT and one as the
    MECHANISM (guard-6927).
That is 2N retrieve calls per drain. It was hand-looped four times on
2026-09-23 and 2026-09-24 (g-369-416 and g-115-9750 with 21 lessons each,
g-001-10 with 30, g-306-284 with 6; gap-237's encounter log), and every loop
rebuilt the same parser.

This tool runs that loop and REPORTS. It does not decide: strengthen, create,
or skip-as-re-delivery stays with the reader, as the replay block requires.

WHY THIS IS NOT A MODE OF sq013-dedup-probe.py: that script is pure
stdin->stdout by contract (its own docstring; built by g-115-8007). The caller
supplies the queue corpus, so the queue has exactly one reader. This tool must
call retrieve.sh 2N times, which is store I/O, and folding it in would break
the contract the sibling documents.

USAGE
    py -3 core/scripts/lesson-dedup-probe.py --lessons-file <path> [--top 3] [--json]

The lessons file is JSON in either of two shapes:
    {"<key>": ["<subject query>", "<mechanism query>"], ...}
    [{"key": "...", "subject": "...", "mechanism": "...",
      "goal_id": "g-NNN-NN", "item_ts": "<ISO>"}, ...]
goal_id and item_ts are optional (the capture's own `_item_ts` key is accepted
too). Both queries are required: a lesson with one axis is refused, because a
single query is exactly the check guard-6927 measured as insufficient.

PER LESSON, IN THIS ORDER
  1. PROVENANCE (guard-7379). Runs only when the lesson carries a goal_id.
     It lists the active entries derived from that goal, created on/after
     item_ts when one is given:
       - rb entries whose source_goal OR origin_goal_id equals the goal id.
         Both fields: a replay-time encode can carry the REPLAYING goal in
         source_goal and the producing goal in origin_goal_id. Measured
         2026-09-27: the two differ on 418 of 2568 entries that set both.
       - guardrails whose `source` names the goal id as a whole token.
         `source` is free text; it is a bare goal id on 5905 of 6998.
     If one of these entries already states the lesson, the capture is a
     RE-DELIVERY. Do not strengthen that entry, and do not create a sibling.
  2. SUBJECT, and 3. MECHANISM. Each runs
     `retrieve.sh --category <query> --depth shallow --read-only` and reports
     the top-N reasoning-bank and top-N guardrail hits. The two lists are
     reported separately, followed by the ids that ONLY the mechanism query
     found. A full list of plausible near-misses from the subject query is
     the failure guard-6927 measured.

MALFUNCTION, NEVER "NO HITS" (guard-3707, guard-3362, guard-2750)
  A retrieve reply is reported as MALFUNCTION for that query when it:
    - is zero bytes,
    - is not JSON,
    - is not an object carrying both hit lists,
    - exits non-zero,
    - or times out.
  An unreadable store read makes the provenance step MALFUNCTION the same way.
  Only a well-shaped reply with an empty list means "no hits".

EXIT CODES
    0  every query answered (with hits, or genuinely empty)
    2  usage error, or an unreadable lessons file
    3  at least one query or store read MALFUNCTIONED (the report names each)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
from _runtime_bash import bash_cmd  # noqa: E402  (guard-580: never a bare "bash")

RETRIEVE = HERE / "retrieve.sh"
RB_READ = HERE / "reasoning-bank-read.sh"
GUARD_READ = HERE / "guardrails-read.sh"
QUERY_TIMEOUT_S = 180
STORE_TIMEOUT_S = 300
TEXT_CHARS = 160


class UsageError(Exception):
    pass


def run_script(script, *args, timeout):
    """Run one wrapper. Returns (rc, stdout, error); error is set when it could not run."""
    try:
        proc = subprocess.run(bash_cmd(script, *args), capture_output=True, text=True,
                              timeout=timeout, cwd=str(PROJECT_ROOT))
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        return None, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout, None


def load_lessons(path):
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot read lessons file {path}: {exc}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError(f"lessons file is not JSON ({exc})")
    if isinstance(data, dict):
        items = []
        for key, pair in data.items():
            if not (isinstance(pair, list) and len(pair) == 2):
                raise UsageError(f"lesson {key!r}: expected [subject, mechanism]")
            items.append({"key": key, "subject": pair[0], "mechanism": pair[1]})
    elif isinstance(data, list):
        if not all(isinstance(x, dict) for x in data):
            raise UsageError("every array element must be an object")
        items = data
    else:
        raise UsageError("expected {key: [subject, mechanism]} or an array of objects")
    lessons = []
    for i, it in enumerate(items, 1):
        key = str(it.get("key") or f"L{i}")
        subject = " ".join(str(it.get("subject") or "").split())
        mechanism = " ".join(str(it.get("mechanism") or "").split())
        if not subject or not mechanism:
            raise UsageError(f"lesson {key}: both subject and mechanism are required (guard-6927)")
        goal_id = str(it.get("goal_id") or "").strip() or None
        item_ts = str(it.get("item_ts") or it.get("_item_ts") or "").strip() or None
        lessons.append({"key": key, "subject": subject, "mechanism": mechanism,
                        "goal_id": goal_id, "item_ts": item_ts})
    if not lessons:
        raise UsageError("the lessons file holds no lessons")
    return lessons


def _hit(entry, text_key):
    text = " ".join(str(entry.get(text_key) or "").split())
    return {"id": entry.get("id"), "text": text[:TEXT_CHARS]}


def parse_retrieve(rc, out, err, top):
    """Top-N hits from one retrieve reply, or a MALFUNCTION verdict.

    A reply that cannot be read is NEVER returned as an empty hit list."""
    if err is not None:
        return {"status": "MALFUNCTION", "reason": err}
    if rc != 0:
        return {"status": "MALFUNCTION", "reason": f"retrieve.sh exited {rc}"}
    if not out.strip():
        return {"status": "MALFUNCTION", "reason": "zero-byte reply"}
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        return {"status": "MALFUNCTION", "reason": f"non-JSON reply ({len(out)} bytes): {exc}"}
    if not (isinstance(data, dict) and isinstance(data.get("reasoning_bank"), list)
            and isinstance(data.get("guardrails"), list)):
        return {"status": "MALFUNCTION",
                "reason": "reply is not an object carrying reasoning_bank and guardrails lists"}
    return {"status": "ok",
            "rb": [_hit(e, "title") for e in data["reasoning_bank"][:top]],
            "guardrails": [_hit(e, "rule") for e in data["guardrails"][:top]]}


def load_store(script, runner):
    """All ACTIVE entries of one store through its read wrapper: (entries, error)."""
    rc, out, err = runner(script, "--active", timeout=STORE_TIMEOUT_S)
    if err is not None:
        return None, f"{script.name}: {err}"
    if rc != 0:
        return None, f"{script.name} exited {rc}"
    if not out.strip():
        return None, f"{script.name}: zero-byte reply"
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        return None, f"{script.name}: non-JSON reply ({exc})"
    if not isinstance(data, list):
        return None, f"{script.name}: expected a JSON array"
    return data, None


def names_goal(text, goal_id):
    """True when `text` names goal_id as a whole token ( is not )."""
    pattern = r"(?<![\w-])" + re.escape(goal_id) + r"(?![\w-])"
    return re.search(pattern, str(text or "")) is not None


def on_or_after(created, item_ts):
    """created >= item_ts. A date-only side compares on the date, and a missing
    created is kept: provenance lists candidates for a reader, so it errs
    toward showing one rather than hiding it."""
    if not item_ts:
        return True
    created = str(created or "")
    if not created:
        return True
    if len(created) <= 10 or len(item_ts) <= 10:
        return created[:10] >= item_ts[:10]
    return created[:19] >= item_ts[:19]


def provenance_hits(lesson, rb_entries, guard_entries):
    gid, ts = lesson["goal_id"], lesson["item_ts"]
    rb = []
    for e in rb_entries:
        fields = [f for f in ("source_goal", "origin_goal_id") if e.get(f) == gid]
        if fields and on_or_after(e.get("created"), ts):
            rb.append(dict(_hit(e, "title"), created=e.get("created"), matched=",".join(fields)))
    guards = [dict(_hit(e, "rule"), created=e.get("created"), matched="source")
              for e in guard_entries
              if names_goal(e.get("source"), gid) and on_or_after(e.get("created"), ts)]
    return {"status": "ok", "rb": rb, "guardrails": guards}


def probe(lessons, top, runner):
    """Run every lesson. Returns (results, store_sizes, malfunction_count)."""
    rb_entries = guard_entries = None
    store_err = None
    if any(l["goal_id"] for l in lessons):
        rb_entries, err_rb = load_store(RB_READ, runner)
        guard_entries, err_gr = load_store(GUARD_READ, runner)
        store_err = "; ".join(e for e in (err_rb, err_gr) if e) or None
    store_sizes = (None if rb_entries is None or guard_entries is None
                   else (len(rb_entries), len(guard_entries)))
    results, malfunctions = [], 0
    for lesson in lessons:
        row = dict(lesson)
        if not lesson["goal_id"]:
            row["provenance"] = {"status": "skipped", "reason": "no goal_id on this lesson"}
        elif store_err:
            row["provenance"] = {"status": "MALFUNCTION", "reason": store_err}
            malfunctions += 1
        else:
            row["provenance"] = provenance_hits(lesson, rb_entries, guard_entries)
        for axis in ("subject", "mechanism"):
            rc, out, err = runner(RETRIEVE, "--category", lesson[axis], "--depth", "shallow",
                                  "--read-only", timeout=QUERY_TIMEOUT_S)
            row[axis + "_hits"] = parse_retrieve(rc, out, err, top)
            if row[axis + "_hits"]["status"] != "ok":
                malfunctions += 1
        s, m = row["subject_hits"], row["mechanism_hits"]
        if s["status"] == "ok" and m["status"] == "ok":
            seen = {h["id"] for h in s["rb"] + s["guardrails"]}
            row["mechanism_only"] = [h["id"] for h in m["rb"] + m["guardrails"] if h["id"] not in seen]
        results.append(row)
    return results, store_sizes, malfunctions


def _print_hits(label, block):
    if block["status"] != "ok":
        print(f"  {label}MALFUNCTION: {block['reason']} -- this is NOT 'no hits'; "
              f"re-run before calling the lesson novel")
        return
    hits = block["rb"] + block["guardrails"]
    if not hits:
        print(f"  {label}(no hits)")
        return
    print(f"  {label}")
    for h in hits:
        extra = f"  [{h['created']}; {h['matched']}]" if "matched" in h else ""
        print(f"      {h['id']}{extra}  {h['text']}")


def render(results, store_sizes, malfunctions, top):
    queries = 2 * len(results)
    scanned = (f"provenance scanned {store_sizes[0]} rb / {store_sizes[1]} guardrails"
               if store_sizes else "provenance store not read")
    print(f"lesson-dedup-probe: {len(results)} lesson(s), {queries} retrieve(s), "
          f"top {top} per list; {scanned}")
    for r in results:
        print(f"=== {r['key']}  [goal {r['goal_id'] or '-'}, item_ts {r['item_ts'] or '-'}]")
        prov = r["provenance"]
        if prov["status"] == "skipped":
            print(f"  PROVENANCE (guard-7379): skipped -- {prov['reason']}")
        else:
            _print_hits(f"PROVENANCE (guard-7379) from {r['goal_id']}: ", prov)
        _print_hits(f'SUBJECT   "{r["subject"]}": ', r["subject_hits"])
        _print_hits(f'MECHANISM "{r["mechanism"]}": ', r["mechanism_hits"])
        if "mechanism_only" in r:
            only = ", ".join(r["mechanism_only"]) or "none"
            print(f"  mechanism-only (guard-6927): {only}")
    verdict = ("every query answered" if not malfunctions
               else f"{malfunctions} MALFUNCTION(s) -- an empty list above is NOT evidence of novelty")
    print(f"SUMMARY: {len(results)} lesson(s): {verdict}.")


def main(argv=None, runner=None):
    ap = argparse.ArgumentParser(
        description="Batch subject+mechanism dedup for spark-replay lesson candidates "
                    "(g-115-10749); reports, never decides.")
    ap.add_argument("--lessons-file", required=True,
                    help="JSON: {key: [subject, mechanism]} or an array of lesson objects")
    ap.add_argument("--top", type=int, default=3, help="hits per list (default 3)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    if args.top < 1:
        print("lesson-dedup-probe: --top must be >= 1", file=sys.stderr)
        return 2
    try:
        lessons = load_lessons(args.lessons_file)
    except UsageError as exc:
        print(f"lesson-dedup-probe: {exc}", file=sys.stderr)
        return 2
    results, store_sizes, malfunctions = probe(lessons, args.top, runner or run_script)
    if args.json:
        print(json.dumps({"lessons": results, "store_sizes": store_sizes,
                          "malfunctions": malfunctions}, indent=2))
    else:
        render(results, store_sizes, malfunctions, args.top)
    return 3 if malfunctions else 0


if __name__ == "__main__":
    sys.exit(main())
