#!/usr/bin/env python3
"""user-signal-refresh.py — the missing WRITER for user-signal-snapshot.yaml.

WHY THIS EXISTS (g-001-269)
---------------------------
`goal-selector.py` has a complete CONSUMER for the `user_signal_boost` criterion —
`load_user_signal_snapshot()`, `load_user_signal_boost_config()`, scoring path 7d,
and a tuned config block in `aspirations.yaml`. It reads
`agents/<agent>/session/user-signal-snapshot.yaml`. That file never existed, and
the token appeared ONLY in the consumer: the "signal-refresh hook" its docstring
names as the producer was never built.

The consequence is the sharp part. The criterion's job is to surface a goal whose
pending question has gone unanswered — the loop's only scoring feedback from the
principal. With no writer it contributed nothing, so an outstanding ask could sit
indefinitely without ever gaining rank.

THE FRAMEWORK ALREADY DIAGNOSED THIS CLASS AND THEN REPEATED IT. goal-selector
line 2922 records that Path A (per-goal `user_signal_kind` / `user_thread_id`) was
retired 2026-04-24 because "fields stayed 0/798 for 6+ days — writer never landed",
and states the lesson outright: "reader-without-writer is the failure mode this
retire removed." Path B — the `silent_48h_goal_ids` path this script feeds — was
then left in exactly that state. Retiring Path A fixed the instance and not the
class. (Cf. the `system/inert-mechanism-class` tree node.)

SCOPE IS DELIBERATELY NARROW — one field, because one field is all that is read.
`silence_48h_boost` is the ONLY live consumption. `reply_boost`, `directive_boost`,
`override_penalty` and `thread_active_boost` remain in the config block but their
read paths were retired with Path A, so populating anything for them would be
writing into a reader that no longer exists — the same mistake mirrored. Do NOT
"complete" the snapshot shape without first restoring the reads, and per that same
comment, ship reader and writer in ONE change if you ever do.

EMITS "RAN AND FOUND NOTHING" DISTINGUISHABLY FROM "NEVER RAN". The consumer
fail-opens on a missing file, so an absent snapshot and an empty one score
identically — which is precisely how this defect stayed invisible. So the snapshot
is ALWAYS written, carrying `refreshed_at` plus counters, and an empty
`silent_48h_goal_ids` is a positive statement that nothing qualified rather than an
absence of evidence.

KNOWN COMPROMISE: goal ids are extracted from pending-question PROSE — from
`context` and `question` ONLY — because the pending-questions schema carries no
structured goal field (its keys are id/date/status/context/question/
default_action/notes). `notes` and `default_action` are deliberately EXCLUDED:
they carry retrospective references (a since-resolved blocker, a filed follow-up,
what was done instead), and boosting those raises goals the question does not
gate. Measured on live data 2026-07-30: all four fields yielded 8 ids of which 3
were mentions-only; context+question yields exactly the 5 gated goals.

Every current entry names its goal in prose and the pattern is exact-format
(`g-NNN-NN` .. `g-NNN-NNNN`), but a question naming no goal contributes nothing
and one naming several boosts all of them. The durable fix is a structured
`goal_ids:` field on the pending-question schema; filed alongside this script. A
stale id (e.g. a since-skipped goal) is harmless — the scorer consults the list
only for goals it is already scoring, so an id matching no candidate never fires.

USAGE
    py -3 core/scripts/user-signal-refresh.py            # write the snapshot
    py -3 core/scripts/user-signal-refresh.py --dry-run  # print, write nothing
    py -3 core/scripts/user-signal-refresh.py --selftest

EXIT 0 always on the write path (fail-open — this runs on the precheck path and
must never block the loop). Exit 1 only on --selftest failure.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Matches the framework's goal-id format (2-4 digit tail per CLAUDE.md "ID Formats").
GOAL_ID_RE = re.compile(r"\bg-\d{3}-\d{2,4}\b")
SILENCE_HOURS = 48


def _now() -> _dt.datetime:
    return _dt.datetime.now()


def _parse_date(val) -> _dt.datetime | None:
    """Pending-question `date` is a plain YYYY-MM-DD (sometimes full ISO)."""
    if val is None:
        return None
    s = str(val).strip().strip('"').strip("'")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s[:len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", ""))
    except Exception:
        return None


def extract_goal_ids(entry: dict) -> list:
    """Goal ids named anywhere in a pending question's prose. Order-preserving."""
    # ONLY context + question — the fields that state what the question is ABOUT.
    # `notes` and `default_action` are excluded deliberately: they carry
    # RETROSPECTIVE references (resolved blockers, follow-up goal ids, what was
    # done instead), and boosting those would raise goals the question does not
    # gate. Measured on live data 2026-07-30: all four fields yielded 8 ids, of
    # which 3 were mentions-only (a since-resolved blocker and a filed follow-up);
    # context alone yields exactly the 5 gated goals. A boost is a scoring
    # intervention, so precision here matters more than recall.
    blob = " ".join(str(entry.get(k, "") or "")
                    for k in ("context", "question"))
    seen, out = set(), []
    for m in GOAL_ID_RE.findall(blob):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def compute_silent(entries, now=None, silence_hours=SILENCE_HOURS) -> tuple:
    """(silent_goal_ids, diagnostics). Pure — unit-testable without I/O.

    A question counts only when status is pending AND its age >= silence_hours.
    An unparseable date is SKIPPED rather than treated as ancient: guessing old
    would boost goals on a formatting error, and the fail-safe direction here is
    to under-boost, never to over-boost.
    """
    now = now or _now()
    silent, diag = [], {"considered": 0, "pending": 0, "aged": 0,
                        "no_goal_id": 0, "bad_date": 0}
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        diag["considered"] += 1
        if str(e.get("status", "")).strip().lower() != "pending":
            continue
        diag["pending"] += 1
        d = _parse_date(e.get("date"))
        if d is None:
            diag["bad_date"] += 1
            continue
        if (now - d).total_seconds() < silence_hours * 3600:
            continue
        diag["aged"] += 1
        gids = extract_goal_ids(e)
        if not gids:
            diag["no_goal_id"] += 1
            continue
        for g in gids:
            if g not in silent:
                silent.append(g)
    return silent, diag


def build_snapshot(entries, now=None) -> dict:
    silent, diag = compute_silent(entries, now=now)
    now = now or _now()
    return {
        # Shape per aspirations.yaml `user_signal_boost` block. ONLY
        # pending_questions.silent_48h_goal_ids is read today (Path B); the other
        # documented sub-keys had their reads retired with Path A on 2026-04-24
        # and are deliberately NOT populated here.
        "refreshed_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "produced_by": "core/scripts/user-signal-refresh.py",
        "sources": {
            "pending_questions": {
                "silent_48h_goal_ids": silent,
                "silence_hours": SILENCE_HOURS,
            },
        },
        # Counters exist so an EMPTY list is legible as "ran, nothing qualified"
        # rather than "never ran" — the distinction whose absence hid this defect.
        "diagnostics": diag,
    }


def selftest() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok = ok and bool(cond)

    now = _dt.datetime(2026, 7, 30, 12, 0, 0)
    old = "2026-07-27"      # 3 days -> aged
    fresh = "2026-07-30"    # today  -> not aged

    aged = [{"id": "pq-a", "status": "pending", "date": old,
             "context": "asp-008 / g-008-35 — the blocker"}]
    s, d = compute_silent(aged, now=now)
    check("aged pending question yields its goal id", s == ["g-008-35"] and d["aged"] == 1)

    s, _ = compute_silent([{"id": "pq-b", "status": "pending", "date": fresh,
                            "context": "g-016-67 fresh"}], now=now)
    check("question younger than 48h yields nothing", s == [])

    s, _ = compute_silent([{"id": "pq-c", "status": "answered", "date": old,
                            "context": "g-016-84 answered"}], now=now)
    check("non-pending status is ignored regardless of age", s == [])

    s, d = compute_silent([{"id": "pq-d", "status": "pending", "date": old,
                            "context": "no goal named here"}], now=now)
    check("aged question naming no goal contributes nothing", s == [] and d["no_goal_id"] == 1)

    s, d = compute_silent([{"id": "pq-e", "status": "pending", "date": "not-a-date",
                            "context": "g-029-64"}], now=now)
    check("unparseable date SKIPS (fail-safe: under-boost, never over-boost)",
          s == [] and d["bad_date"] == 1)

    s, _ = compute_silent([{"id": "pq-f", "status": "pending", "date": old,
                            "context": "g-029-54 and g-029-64 both"}], now=now)
    check("multi-goal question boosts every named goal", s == ["g-029-54", "g-029-64"])

    s, _ = compute_silent([{"id": "pq-g", "status": "pending", "date": old,
                            "context": "g-001-01"},
                           {"id": "pq-h", "status": "pending", "date": old,
                            "context": "also g-001-01"}], now=now)
    check("same goal named twice is not duplicated", s == ["g-001-01"])

    snap = build_snapshot(aged, now=now)
    check("snapshot carries refreshed_at + diagnostics so empty != never-ran",
          snap.get("refreshed_at") and "diagnostics" in snap)
    check("snapshot shape matches the consumer's read path",
          snap["sources"]["pending_questions"]["silent_48h_goal_ids"] == ["g-008-35"])

    check("garbage input does not raise", compute_silent([None, 42, {}], now=now)[0] == [])

    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print the snapshot, write nothing")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    try:
        import yaml
        from _paths import AGENT_DIR  # type: ignore
        pq_path = Path(AGENT_DIR) / "session" / "pending-questions.yaml"
        entries = []
        if pq_path.is_file():
            entries = yaml.safe_load(pq_path.read_text(encoding="utf-8")) or []
        snap = build_snapshot(entries)
        out = Path(AGENT_DIR) / "session" / "user-signal-snapshot.yaml"
        if a.dry_run:
            print(json.dumps(snap, indent=1))
            return 0
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(yaml.safe_dump(snap, sort_keys=False, allow_unicode=True),
                       encoding="utf-8")
        n = len(snap["sources"]["pending_questions"]["silent_48h_goal_ids"])
        print(f"user-signal-refresh: wrote {out.name} "
              f"silent_48h={n} diagnostics={snap['diagnostics']}")
        return 0
    except Exception as exc:
        # Fail-open: this runs on the precheck path. A producer that crashes the
        # loop is worse than one that skips a cycle — but say so on stderr rather
        # than exiting silently, or this becomes the very defect it fixes.
        print(f"user-signal-refresh: SKIPPED ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
