#!/usr/bin/env python3
"""exp_capture_drain — reducer-side drain of the exp_capture working-memory slot.

g-306-199 (asp-306, worker-learning Phase C). Worker Bodies write an execution
narrative to `exp_capture` at worker-loop Phase 3.6; the entries reach the
reducer's agent-wide WM via body-merge at generalize-down. Until this script
existed the slot had a writer, four registration sync sites and six green tests
— and NO consumer. Registered, populated, and never drained.

THIS IS A TRANSPORT, NOT AN AUTHOR. The worker already wrote the narrative
(`execution_summary`, `key_decisions`, `verbatim_anchors`) precisely so the
reducer would not have to re-derive it — a reducer cannot recover an error code
it never observed. So every field below is mechanically mapped, never
re-worded, and the encoding itself is a CALL into the existing writer:

    experience-archive-goal.sh  ->  POST /v1/experience/archive-goal

NEVER a reimplementation (no-transcription contract, guard-2676). That contract
is load-bearing rather than stylistic here: the endpoint composes the record id
as `exp-<goal>-<slug>` and then UNIQUIFIES it before deriving the canonical .md
path, which is exactly the collision guard-2939 records as destroying a real
experience file silently. A hand-rolled encoder would re-derive the bare path
and re-open that hole.

Because this runs as a mechanical transform with no LLM judgment, it can live in
a script the flow already runs (iteration-close.sh do_state_update) rather than
as a SKILL.md block. That distinction is the whole point: guard-399's amendment
(2) measured that prose and a `Bash:` line inside a loaded digest are the SAME
enforcement class — both require the model to ELECT to run them — and the
sibling spark_capture drain is LLM-elected for exactly that reason. The
operative test is not "is the invocation a command?" but "WHO executes it".

Fail-open by contract: exits 0 on every path. A drain failure must never block a
goal close.

Usage:
    python3 core/scripts/exp_capture_drain.py [--apply] [--json]
    python3 core/scripts/exp_capture_drain.py --slot-json <file>   # test seam
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from _runtime_bash import bash_cmd  # noqa: E402  (needs the path insert above)

SLOT = "exp_capture"
SKILL_SLUG = "worker-exec"

# Mirrors mind_api/src/endpoints/experience_write.py. Kept as a local floor so a
# too-thin entry is reported here by name instead of surfacing as an opaque
# warning from inside the daemon.
MIN_SUMMARY_CHARS = 20

# The documented element shape, from .claude/skills/worker-loop/SKILL.md
# Phase 3.6. Only the first two are REQUIRED — an entry missing either cannot be
# encoded at all; the rest degrade to empty.
REQUIRED_KEYS = ("goal_id", "execution_summary")


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _read_slot(slot_json: str | None):
    """Return (entries, error). error is None on a clean read.

    An unreadable slot and an empty slot are DIFFERENT outcomes and are reported
    as such (guard-2352): a fail-open guard that renders "no producer on this
    box" and "ran and drained nothing" identically reports healthy while inert.
    """
    if slot_json:
        try:
            raw = json.loads(Path(slot_json).read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            return [], f"slot-json unreadable: {e}"
    else:
        p = _run(bash_cmd(str(SCRIPT_DIR / "wm-read.sh"), SLOT, "--json"))
        if p.returncode != 0:
            return [], f"wm-read rc={p.returncode}: {(p.stderr or '').strip()[:200]}"
        try:
            raw = json.loads(p.stdout or "null")
        except json.JSONDecodeError as e:
            return [], f"wm-read returned non-JSON: {e}"

    # wm-read may hand back the bare value or a {"value": ...} envelope.
    if isinstance(raw, dict) and "value" in raw:
        raw = raw["value"]
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return [], f"slot is {type(raw).__name__}, expected list"
    return raw, None


def conforms(entry) -> bool:
    """Documented-shape test. Deliberately strict, and deliberately NOT a
    normalizer.

    guard-1970: a slot whose readers index fields silently no-ops on an element
    of the wrong shape. The live slot carries both — measured 2026-08-10, 8 of
    12 entries in the Phase 3.6 shape and 4 in two other hand-written shapes
    ({goal_id,summary,outcome,note} and a what_worked/what_failed/lesson form).
    Guessing a mapping for those would silently invent a narrative the worker
    never wrote, so they are RETAINED and REPORTED instead. Normalizing another
    lane's entries under a concurrent-writer read-modify-write is the specific
    thing guard-1970's action hint forbids.
    """
    if not isinstance(entry, dict):
        return False
    for k in REQUIRED_KEYS:
        v = entry.get(k)
        if not isinstance(v, str) or not v.strip():
            return False
    return len(entry["execution_summary"].strip()) >= MIN_SUMMARY_CHARS


def anchor_objects(raw) -> list:
    """Map the worker's anchors onto the experience record's anchor shape.

    THE TWO ENDS OF THIS PIPELINE DISAGREE ON ONE FIELD NAME. worker-loop Phase
    3.6 documents `verbatim_anchors` as a list of BARE STRINGS ("exact error
    codes / paths / hashes / commit shas"); the experience record documents the
    same field as a list of `{key, content}` objects and the daemon validator
    REFUSES bare strings outright. Same name, two shapes, one hop apart in a
    pipeline built to connect them (guard-1970 class, at a cross-store seam).

    Measured 2026-08-10: passing the worker's list through unchanged failed all
    8 encodes with `Each verbatim_anchor must have 'key' and 'content' fields`.
    The refusal is the good outcome — it is loud, it is per-record, and it left
    every entry in the slot. This function is the mapping it demanded.

    `content` stays VERBATIM — that is the entire value of the field, and a
    reworded anchor is a destroyed one. Only `key` is derived, as a slug of the
    content, so the mapping adds an index and never an interpretation.
    """
    out: list = []
    if not isinstance(raw, list):
        return out
    seen: set[str] = set()
    for i, a in enumerate(raw):
        if isinstance(a, dict) and "key" in a and "content" in a:
            out.append(a)  # already in record shape — pass through untouched
            continue
        content = str(a).strip()
        if not content:
            continue
        slug = "".join(c if c.isalnum() else "-" for c in content.lower())
        slug = "-".join(p for p in slug.split("-") if p)[:40].strip("-")
        key = slug or f"anchor-{i}"
        while key in seen:
            key = f"{key}-{i}"
        seen.add(key)
        out.append({"key": key, "content": content})
    return out


def render_trace(entry: dict) -> str:
    """Mechanical render of the worker's own fields. No re-wording."""
    gid = entry["goal_id"].strip()
    lines = [
        "---",
        f"goal_id: {gid}",
        f"category: {entry.get('category') or 'uncategorized'}",
        f"outcome_class: {entry.get('outcome_class') or 'unknown'}",
        f"surprise_level: {entry.get('surprise_level')}",
        f"captured_at: {entry.get('_item_ts') or 'unknown'}",
        f"encoded_at: {datetime.now().isoformat(timespec='seconds')}",
        "source: exp_capture (worker Body, worker-loop Phase 3.6)",
        "---",
        "",
        f"# Worker execution narrative — {gid}",
        "",
        "## Execution summary",
        "",
        entry["execution_summary"].strip(),
        "",
    ]

    decisions = [d for d in (entry.get("key_decisions") or []) if str(d).strip()]
    if decisions:
        lines += ["## Key decisions", ""]
        lines += [f"- {str(d).strip()}" for d in decisions]
        lines += [""]

    anchors = [a for a in (entry.get("verbatim_anchors") or []) if str(a).strip()]
    if anchors:
        # The field that makes this worth more than reconstructing from the goal
        # record: exact strings die with the session that saw them.
        lines += ["## Verbatim anchors", ""]
        lines += [f"- `{str(a).strip()}`" for a in anchors]
        lines += [""]

    lines += [
        "## Provenance",
        "",
        "Captured on a worker Body during execution and carried to the reducer "
        "in the `exp_capture` working-memory slot; encoded here by "
        "`exp_capture_drain.py` via `experience-archive-goal.sh`. Every field "
        "above is the worker's own text, mapped mechanically — the reducer did "
        "not re-author the narrative.",
        "",
    ]
    return "\n".join(lines)


def archive_one(entry: dict, trace_dir: Path, apply: bool) -> dict:
    """Encode ONE entry by calling the existing writer. Returns a result dict."""
    gid = entry["goal_id"].strip()
    summary = entry["execution_summary"].strip()
    category = entry.get("category") or "uncategorized"

    if not apply:
        return {"goal_id": gid, "status": "would-drain"}

    trace_dir.mkdir(parents=True, exist_ok=True)
    # Scratch source only — the endpoint COPIES this to the durable canonical
    # agents/<agent>/experience/<id>.md and deletes the source once the record
    # lands, so no content_path ever points under temp/ (guard-1373).
    trace = trace_dir / f"exp-{gid}-{SKILL_SLUG}.md"
    try:
        trace.write_text(render_trace(entry), encoding="utf-8")
    except OSError as e:
        return {"goal_id": gid, "status": "failed", "error": f"trace write: {e}"}

    # Fields the archive-goal record schema carries. key_decisions maps to
    # reasoning_chain (the record's existing field for it) rather than riding
    # along as an unknown key the endpoint would drop.
    extra = {
        "type": "goal_execution",
        "verbatim_anchors": anchor_objects(entry.get("verbatim_anchors")),
        "reasoning_chain": [str(d) for d in (entry.get("key_decisions") or [])],
    }

    p = _run(
        bash_cmd(
            str(SCRIPT_DIR / "experience-archive-goal.sh"),
            "--goal", gid,
            "--skill-slug", SKILL_SLUG,
            "--category", category,
            "--summary", summary,
            "--trace-file", str(trace),
        ),
        input=json.dumps(extra),
    )
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()[:300]
        return {"goal_id": gid, "status": "failed", "error": f"rc={p.returncode}: {err}"}

    rec_id = None
    content_path = None
    try:
        rec = json.loads(p.stdout or "{}")
        rec_id = rec.get("id")
        content_path = rec.get("content_path")
    except json.JSONDecodeError:
        pass
    return {
        "goal_id": gid,
        "status": "drained",
        "experience_id": rec_id,
        "content_path": content_path,
    }


def write_residue(residue: list) -> str | None:
    """Overwrite the slot with what was NOT drained. Returns an error or None.

    Whole-slot set rather than a clear: the sibling spark_capture drain clears
    unconditionally, which would destroy the non-conforming entries this drain
    deliberately declines to interpret. The daemon's set handler advances
    slot_meta.updated_at + update_count, so slot hygiene holds (guard-540).
    """
    p = _run(
        bash_cmd(str(SCRIPT_DIR / "wm-set.sh"), SLOT),
        input=json.dumps(residue),
    )
    if p.returncode != 0:
        return f"wm-set rc={p.returncode}: {(p.stderr or '').strip()[:200]}"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="encode entries and write the residue back (default: report only)")
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    ap.add_argument("--slot-json", help="read the slot from a file instead of WM (test seam)")
    args = ap.parse_args()

    agent = os.environ.get("MIND_AGENT", "")
    report = {"slot": SLOT, "agent": agent, "applied": bool(args.apply)}

    entries, err = _read_slot(args.slot_json)
    if err:
        # NOT reported as an empty drain — see _read_slot.
        report.update({"verdict": "read-failed", "error": err})
        print(f"[exp-capture-drain] SKIP: could not read {SLOT} — {err}", file=sys.stderr)
        if args.json:
            print(json.dumps(report, indent=2))
        return 0

    conforming = [e for e in entries if conforms(e)]
    nonconforming = [e for e in entries if not conforms(e)]

    if not entries:
        report.update({"verdict": "empty", "scanned": 0})
        print(f"[exp-capture-drain] {SLOT}: empty — nothing to encode")
        if args.json:
            print(json.dumps(report, indent=2))
        return 0

    trace_dir = PROJECT_ROOT / "agents" / (agent or "unknown") / "temp" / "exp-capture-drain"
    results = [archive_one(e, trace_dir, args.apply) for e in conforming]
    # "would-drain" is the dry-run status. Counting it here keeps the report
    # line honest in BOTH modes — a dry run that printed "would encode=0" over 8
    # encodable entries reads as a clean slot, which is the same false all-clear
    # this drain exists to end.
    drained = [r for r in results if r["status"] in ("drained", "would-drain")]
    failed = [r for r in results if r["status"] == "failed"]

    if args.apply:
        # Retain anything not successfully encoded: the non-conforming entries
        # (owner normalizes) plus any that failed (retry next close).
        failed_ids = {r["goal_id"] for r in failed}
        residue = list(nonconforming) + [
            e for e in conforming if e["goal_id"].strip() in failed_ids
        ]
        report["residue_count"] = len(residue)
        if not drained:
            # NOTHING ENCODED -> NOTHING TO WRITE. Skipping the write is not
            # merely an optimization: this drain runs on EVERY goal close, and a
            # slot holding only un-encodable entries (the steady state once the
            # conforming backlog is drained) would otherwise be rewritten every
            # close with identical content. Each such write advances
            # slot_meta.update_count, which wm_write.py:415 uses as the CAS token
            # for the loop_state stale-lock-steal check — so a no-op write is not
            # free, it is churn on a concurrency primitive.
            report["residue_write"] = "skipped-unchanged"
        else:
            set_err = write_residue(residue)
            if set_err:
                report["residue_error"] = set_err
                print(f"[exp-capture-drain] WARN: residue write failed — {set_err} "
                      f"(entries stay in the slot; next close retries)", file=sys.stderr)

    report.update({
        "verdict": "drained" if args.apply else "dry-run",
        "scanned": len(entries),
        "conforming": len(conforming),
        "nonconforming": len(nonconforming),
        "drained": len(drained),
        "failed": len(failed),
        "results": results,
        "nonconforming_goal_ids": [
            (e.get("goal_id") if isinstance(e, dict) else None) for e in nonconforming
        ],
    })

    verb = "encoded" if args.apply else "would encode"
    print(f"[exp-capture-drain] {SLOT}: scanned={len(entries)} {verb}={len(drained)} "
          f"failed={len(failed)} nonconforming={len(nonconforming)}")
    for r in drained:
        if r["status"] == "drained":
            print(f"  + {r['goal_id']} -> {r.get('experience_id')} ({r.get('content_path')})")
        else:
            print(f"  ? {r['goal_id']} (dry-run — pass --apply to encode)")
    for r in failed:
        print(f"  ! {r['goal_id']} FAILED: {r.get('error')}", file=sys.stderr)
    if nonconforming:
        ids = [str(e.get("goal_id")) for e in nonconforming if isinstance(e, dict)]
        print(f"  ~ {len(nonconforming)} entry(s) not in the worker-loop Phase 3.6 shape, "
              f"retained for the owner to normalize (guard-1970): {ids}", file=sys.stderr)

    if args.json:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
