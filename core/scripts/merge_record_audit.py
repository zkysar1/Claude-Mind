#!/usr/bin/env python3
"""merge_record_audit — diff governed stores BY RECORD ID against a pre-merge state.

guard-424 mandates this audit after any raw `git merge` that touches a store with a
handler in ``coordination_merge.py``; it had been hand-rolled four times
(2026-08-04, 09-06, 09-10, 09-14) before gap-042 forged it.

THREE GUARDRAILS SHAPE EVERY DESIGN DECISION HERE. They are not decoration — each
one names a way this audit produces a confident wrong answer:

  guard-424  A raw merge resolves aspirations.jsonl LINE BY LINE, and that file is
             ONE LINE PER ASPIRATION with goals nested inside. Taking origin's side
             of a single line silently reverts EVERY goal on it — statuses,
             completion metadata, defer_reasons — with no conflict marker. So the
             audit must compare per-id STATUS and per-id PRESENCE of
             completed_date / completed_by / defer_reason, not just id sets.
  guard-598  Compare unique-ROW SETS, never line counts. A correct deduplicating
             merge is indistinguishable from data loss by count, and the alarm it
             raises is loud, plausible and wrong (measured: a clean merge took
             changelog.jsonl BELOW BOTH PARENTS). A result EXCEEDING the pre-state
             is expected — those are this session's own newer writes.
  guard-1017 Parse the JSON and match the id FIELD. `grep -c` counts LINES
             CONTAINING a match; ids appear as REFERENCES inside other records'
             prose, so it answers wrongly in the reassuring direction. A grep count
             once authorized a `git restore` that destroyed 15 goal records.

EXIT CODES — non-zero means GENUINE LOSS, so this is safe to wire into a post-merge
path (which is the real prize: it turns a rule someone must remember into a step
that always runs):
    0  clean, or only gains/re-serializations
    1  GENUINE LOSS — records or terminal fields absent from the post-state
    2  usage / the pre-state could not be resolved at all
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

RC_OK = 0
RC_LOSS = 1
RC_USAGE = 2
# A CRASH MUST NOT LOOK LIKE LOSS (fresh-eyes F1). An uncaught exception exits 1 in
# CPython, which is exactly RC_LOSS — so a registry import failure, a missing git, or
# an undecodable store would report "GENUINE LOSS" to anything reading the exit code.
# For a script whose whole purpose is to gate a post-merge path that is the fail-CLOSED
# direction the wiring goal () explicitly must not have: iteration-push.sh's
# own comments say a wrongly-refusing gate there "silently freezes framework sync for
# the box". Internal faults get their OWN code, and main() catches everything.
RC_INTERNAL = 4

SCRIPTS = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS.parent.parent

# Terminal statuses. A move OUT of this set is a backward lifecycle move — the
# exact damage guard-424 measured (completed -> pending with completed_date and
# completed_by stripped, on records that had genuinely closed the day before).
TERMINAL = {"completed", "skipped", "expired", "retired", "archived", "resolved"}

# The fields whose DISAPPEARANCE is loss even when the record survives. guard-424
# names these three explicitly; they are the ones a line-wise merge drops silently.
TERMINAL_FIELDS = ("completed_date", "completed_by", "defer_reason")

# THE LIFECYCLE FIELD IS NOT ALWAYS `status` (fresh-eyes F2). pipeline records key
# on `stage` and carry NO `status` at all, so a hardcoded `.get("status")` made the
# backward-lifecycle check silently INERT on the pipeline store — a store guard-424
# names explicitly, and the one the 2026-09-14 encounter was about. Measured: an
# archived -> active pipeline revert returned [] while a completed -> pending goal
# revert returned ["backward_lifecycle"], same function, same call.
LIFECYCLE_FIELDS = ("status", "stage")


def _lifecycle(rec: dict) -> "tuple[str | None, str | None]":
    """(field_name, value) for whichever lifecycle field this record carries."""
    for f in LIFECYCLE_FIELDS:
        if f in rec:
            return f, rec.get(f)
    return None, None


def _load_handlers() -> dict:
    """Import the live handler registry rather than hardcoding a store list.

    Hardcoding is the drift this audit exists to catch: a store gains a handler,
    nobody updates the audit's list, and the one store most likely to need
    checking is the one silently skipped. `_HANDLERS` is the same dict the
    own-cloud sync path routes through, so the audit's scope tracks the merge
    layer's scope by construction.
    """
    spec = importlib.util.spec_from_file_location(
        "_cm_for_audit", SCRIPTS / "coordination_merge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return dict(getattr(mod, "_HANDLERS"))


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=str(cwd or PROJECT_ROOT))


def _tracked_paths_for(basenames: set) -> "dict[str, list[str]]":
    """Map each handler basename to every TRACKED repo path carrying it.

    A basename can legitimately appear more than once (world and meta both hold a
    changelog.jsonl), and auditing only the first would silently drop the other.
    """
    res = _git("ls-files")
    out: "dict[str, list[str]]" = {}
    if res.returncode != 0:
        return out
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        base = line.rsplit("/", 1)[-1]
        if base in basenames:
            out.setdefault(base, []).append(line)
    return out


def _read_rev(sha: str, path: str) -> "tuple[str | None, str]":
    """Return (text, verdict). verdict == 'absent' means the path did not exist at
    the pre-state — which is SKIPPED, never total loss. Conflating the two would
    make every newly-added store report as catastrophic on its first audit."""
    res = _git("show", f"{sha}:{path}")
    if res.returncode != 0:
        return None, "absent"
    return res.stdout, "ok"


def _jsonl_records(text: str) -> "tuple[list, int]":
    """Parse JSONL leniently — an unparseable line is reported, never silently
    counted as zero (a try/except that swallows a shape mismatch converts a
    measurement failure into a confident zero)."""
    recs, bad = [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except Exception:
            bad += 1
    return recs, bad


def _canon_row(line: str) -> str:
    """Content-identity for one JSONL row: parsed and re-emitted with sorted keys
    and no incidental whitespace. An unparseable row falls back to its raw text,
    which is the conservative direction (it can only over-report, never hide)."""
    line = line.strip()
    try:
        return json.dumps(json.loads(line), sort_keys=True, separators=(",", ":"))
    except Exception:
        return line


def _goal_index(records: list) -> dict:
    """aspirations.jsonl is ONE LINE PER ASPIRATION with goals nested (guard-1017).
    The unit that gets silently reverted is the GOAL, so index goals, not lines."""
    idx = {}
    for asp in records:
        if not isinstance(asp, dict):
            continue
        for g in (asp.get("goals") or []):
            if isinstance(g, dict) and g.get("id"):
                idx[g["id"]] = g
    return idx


def _id_index(records: list) -> dict:
    return {r["id"]: r for r in records
            if isinstance(r, dict) and isinstance(r.get("id"), str)}


# A record that left a LIVE store for its companion ARCHIVE store has moved, not
# vanished. Measured on this script's own first run: 24 of 24 "lost" pipeline ids
# were sitting in pipeline-archive.jsonl. An audit that calls an archive sweep
# "loss" fires on every correct sweep, which is the guard-598 shape moved from
# counts to namespaces — a true alarm nobody can act on is a false alarm.
ARCHIVE_COMPANION = {
    "aspirations.jsonl": "aspirations-archive.jsonl",
    "pipeline.jsonl": "pipeline-archive.jsonl",
    "guardrails.jsonl": "guardrails-archive.jsonl",
    "reasoning-bank.jsonl": "reasoning-bank-archive.jsonl",
    "pattern-signatures.jsonl": "pattern-signatures-archive.jsonl",
}


def _companion_ids(path: str, extractor, post_sha: "str | None" = None) -> "tuple[set, set]":
    """Ids reachable in the companion archive, plus displaced_from aliases there.

    `displaced_from` is the merge handler's own record that it RENUMBERED a
    colliding id. A renumbered record is present under a new id, so treating its
    old id as lost reports the collision-resolution mechanism as data loss.
    """
    base = path.rsplit("/", 1)[-1]
    comp = ARCHIVE_COMPANION.get(base)
    if not comp:
        return set(), set()
    crel = f"{path.rsplit('/', 1)[0]}/{comp}"
    if post_sha:
        text, v = _read_rev(post_sha, crel)
        if v == "absent":
            return set(), set()
    else:
        cpath = PROJECT_ROOT / crel
        if not cpath.exists():
            return set(), set()
        text = cpath.read_text(encoding="utf-8", errors="replace")
    recs, _ = _jsonl_records(text)
    idx = extractor(recs)
    return set(idx), _displaced_aliases(idx) | _superseded_ids(idx)


def _superseded_ids(idx: dict) -> set:
    """Ids that a LIVE record names as its predecessor via `origin_signal`.

    A goal can be retired by being SUCCEEDED: the successor carries
    `origin_signal: "<prefix>:<old-id>"` (e.g. `residual:g-014-84`,
    `unblock:g-115-2888`) and the original is removed. That is a designed
    transition, and an audit blind to it reports the succession as data loss.

    Measured 2026-09-15, and it is the reason this filter exists: after six other
    filters the audit's ONE surviving finding was `g-014-84` — HIGH, pending,
    absent from all four aspiration stores. It looked real enough that an Unblock
    goal was drafted to restore it. The goal-duplication gate refused the filing
    and named `g-014-97` ('Re-measure the CISA poll cadence …',
    `origin_signal: residual:g-014-84`, created 2026-09-13, live and pending).
    The obligation had been carried forward two days earlier; nothing was lost.
    The gate caught what six filters and a deliberate investigation did not.

    MATCHED ON THE STRUCTURED FIELD ONLY, never on prose. guard-1017: ids appear
    as references inside descriptions and outcome_notes, so a substring scan over
    record text would mark almost anything as superseded. `origin_signal` is a
    controlled `<prefix>:<tag>` field, so an exact suffix match is narrow.
    """
    out = set()
    for rec in idx.values():
        if not isinstance(rec, dict):
            continue
        sig = rec.get("origin_signal")
        if isinstance(sig, str) and ":" in sig:
            tail = sig.rsplit(":", 1)[-1].strip()
            if tail:
                out.add(tail)
    return out


def _displaced_aliases(idx: dict) -> set:
    out = set()
    for rec in idx.values():
        df = rec.get("displaced_from") if isinstance(rec, dict) else None
        for d in ([df] if isinstance(df, str) else (df or [])):
            if isinstance(d, str):
                out.add(d)
    return out


def _classify_absent(pre_idx: dict, absent_ids: list) -> "tuple[list, list]":
    """Split absent ids into EXPECTED (terminal at the pre-state) and ALARM.

    THIS IS THE SPLIT THAT MAKES THE EXIT CODE USABLE, and it was missing from
    the first draft. Measured on the replay that produced it: 72 goal ids present
    at the pre-state were absent from every live and archive store — which reads
    as catastrophic. Broken down by their status AT THE PRE-STATE: 64 completed,
    6 skipped, 1 expired, 1 pending. Seventy-one had reached a terminal state and
    left through the designed archival/pruning path; exactly ONE had not, and that
    one was a real HIGH-priority goal that had genuinely vanished.
    An aggregate that collapses to one item when split by a second field was never
    a measurement of loss — it was a measurement of archival, wearing loss's face.
    """
    expected, alarm = [], []
    for rid in absent_ids:
        rec = pre_idx.get(rid) or {}
        _, st = _lifecycle(rec)
        (expected if st in TERMINAL else alarm).append(
            {"id": rid, "status_at_pre": st,
             "title": str(rec.get("title") or "")[:90],
             "priority": rec.get("priority")})
    return expected, alarm


def _regressions(pre_idx: dict, post_idx: dict) -> list:
    """Per-id damage that leaves the record PRESENT — the half an id-set diff
    cannot see, and the half guard-424 actually measured."""
    out = []
    for rid, pre in pre_idx.items():
        post = post_idx.get(rid)
        if post is None:
            continue  # counted as a lost id, not as a regression
        pre_field, pre_st = _lifecycle(pre)
        _, post_st = _lifecycle(post)
        if pre_st in TERMINAL and post_st not in TERMINAL:
            out.append({"id": rid, "kind": "backward_lifecycle",
                        "field": pre_field, "from": pre_st, "to": post_st})
        # A record that has CLOSED since the pre-state is expected to have shed
        # its defer_reason — closing clears the defer. Measured: 
        # reported a lost `human_blocked:` defer and was completed 2026-09-15,
        # same title and same created_at, i.e. the same record closing normally.
        # Without this the audit flags every goal that closed after the merge.
        if post_st in TERMINAL and pre_st not in TERMINAL:
            continue
        for f in TERMINAL_FIELDS:
            if pre.get(f) not in (None, "") and post.get(f) in (None, ""):
                # A CLEARED defer is not a DROPPED defer, and the framework
                # already draws that line: probe-before-defer rule 4 re-probes
                # `precondition_unmet:` defers every iteration and clears them on
                # success, while `human_blocked:` is documented as the one
                # structured prefix that NEVER auto-clears. So a vanished
                # precondition defer is the designed path and a vanished
                # human_blocked defer is the guard-424 damage. Measured on the
                # replay that produced this rule: 8 dropped defer_reasons, 7 of
                # them `precondition_unmet:`, 1 `human_blocked:`. Flagging all 8
                # buries the one that matters under seven that do not.
                if f == "defer_reason" and str(pre.get(f)).startswith("precondition_unmet:"):
                    continue
                out.append({"id": rid, "kind": "dropped_field", "field": f,
                            "was": str(pre.get(f))[:80]})
    return out


def audit_store(sha: str, path: str, handler_name: str,
                post_sha: "str | None" = None) -> dict:
    pre_text, verdict = _read_rev(sha, path)
    if verdict == "absent":
        return {"path": path, "handler": handler_name, "verdict": "SKIPPED",
                "reason": f"path absent at {sha[:12]} — a store that did not exist "
                          "in the pre-state cannot have lost anything"}
    if post_sha:
        post_text, pverdict = _read_rev(post_sha, path)
        if pverdict == "absent":
            return {"path": path, "handler": handler_name, "verdict": "LOSS",
                    "reason": f"present at {sha[:12]} and ABSENT at {post_sha[:12]} "
                              "— whole-file loss"}
    else:
        live = PROJECT_ROOT / path
        if not live.exists():
            return {"path": path, "handler": handler_name, "verdict": "LOSS",
                    "reason": "present in the pre-state and ABSENT now — whole-file loss"}
        post_text = live.read_text(encoding="utf-8", errors="replace")

    if not path.endswith(".jsonl"):
        return {"path": path, "handler": handler_name, "verdict": "NOT-AUDITED",
                "reason": "keyed-YAML stores are not diffed by id by this audit; "
                          "its absence from the verdict is ignorance, not a pass"}

    pre_recs, pre_bad = _jsonl_records(pre_text)
    post_recs, post_bad = _jsonl_records(post_text)

    # APPEND-ONLY stores carry no stable id contract, so compare unique ROW SETS
    # (guard-598). (pre | post) - post == pre - post: rows present before and gone
    # after. Rows ABOVE the pre-state are this session's own writes and are fine.
    if handler_name == "merge_append_only_jsonl":
        # CANONICALIZE BEFORE COMPARING. guard-598 says compare row SETS rather
        # than line counts — but a raw-byte row set is still comparing
        # REPRESENTATION, not content, so a re-serialization (key reorder,
        # separator change) reports every rewritten row as lost. Measured on this
        # script's own first run: meta-log.jsonl reported 26 lost rows; canonical
        # re-serialization put the figure at ZERO. Same defect class as the
        # guardrail's own incident (bytes fell while record count rose), one level
        # further in — which is why the guardrail alone was not enough to avoid it.
        pre_rows = {_canon_row(l) for l in pre_text.splitlines() if l.strip()}
        post_rows = {_canon_row(l) for l in post_text.splitlines() if l.strip()}
        lost = pre_rows - post_rows
        return {"path": path, "handler": handler_name,
                "verdict": "LOSS" if lost else "CLEAN",
                "mode": "unique-row-set (guard-598)",
                "pre_rows": len(pre_rows), "post_rows": len(post_rows),
                "lost_rows": len(lost),
                "sample_lost": [r[:120] for r in list(lost)[:3]]}

    if handler_name == "merge_aspirations":
        pre_idx, post_idx = _goal_index(pre_recs), _goal_index(post_recs)
        unit = "goal"
    else:
        pre_idx, post_idx = _id_index(pre_recs), _id_index(post_recs)
        unit = "record"

    extractor = _goal_index if handler_name == "merge_aspirations" else _id_index
    arch_ids, arch_aliases = _companion_ids(path, extractor, post_sha)
    live_aliases = _displaced_aliases(post_idx) | _superseded_ids(post_idx)

    # A record is REACHABLE if it is in the live store, in its companion archive,
    # or is the old id of something the merge renumbered. Only the unreachable
    # are candidates for loss.
    # REACHABLE = still present, moved to the companion archive, renumbered by the
    # merge, or SUCCEEDED by a record that names it in origin_signal. Only what is
    # unreachable by all four is a candidate for loss.
    reachable = set(post_idx) | arch_ids | arch_aliases | live_aliases
    absent = sorted(set(pre_idx) - reachable)
    expected, alarm = _classify_absent(pre_idx, absent)
    gained = sorted(set(post_idx) - set(pre_idx))
    regressions = _regressions(pre_idx, post_idx)

    return {"path": path, "handler": handler_name,
            "verdict": "LOSS" if (alarm or regressions) else "CLEAN",
            "mode": f"union-by-id ({unit})",
            "pre": len(pre_idx), "post": len(post_idx),
            "archived": len(arch_ids),
            "alias_and_successor_ids": len(arch_aliases | live_aliases),
            "absent_total": len(absent),
            "absent_expected_terminal": len(expected),
            "alarm_ids": alarm[:20], "alarm_count": len(alarm),
            "gained_count": len(gained),
            "regressions": regressions[:20],
            "regression_count": len(regressions),
            "unparseable_lines": {"pre": pre_bad, "post": post_bad}}


def main(argv=None) -> int:
    try:
        return _main(argv)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(json.dumps({"ok": False, "rc": RC_INTERNAL,
                          "error": f"internal fault, NOT a loss verdict: "
                                   f"{type(exc).__name__}: {exc}",
                          "traceback": traceback.format_exc()[-1200:]}), file=sys.stderr)
        return RC_INTERNAL


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="merge-record-audit")
    ap.add_argument("pre_sha", nargs="?", default="ORIG_HEAD",
                    help="the pre-merge state (default: ORIG_HEAD)")
    ap.add_argument("--post", default=None,
                    help="the post-state to compare against. DEFAULT IS THE WORKING "
                         "TREE, which is what a just-ran merge leaves behind and is "
                         "the shape this audit is wired for. Pass a SHA only to "
                         "REPLAY an old merge — comparing an old pre-state against "
                         "today answers 'what changed since?', not 'did that merge "
                         "lose anything?', and every legitimate close, archive and "
                         "defer-clear in between reads as loss.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    probe = _git("rev-parse", "--verify", f"{args.pre_sha}^{{commit}}")
    if probe.returncode != 0:
        print(json.dumps({"ok": False, "rc": RC_USAGE,
                          "error": f"cannot resolve pre-state {args.pre_sha!r}: "
                                   f"{probe.stderr.strip()[:200]}"}), file=sys.stderr)
        return RC_USAGE
    sha = probe.stdout.strip()

    post_sha = None
    if args.post:
        pp = _git("rev-parse", "--verify", f"{args.post}^{{commit}}")
        if pp.returncode != 0:
            print(json.dumps({"ok": False, "rc": RC_USAGE,
                              "error": f"cannot resolve post-state {args.post!r}"}),
                  file=sys.stderr)
            return RC_USAGE
        post_sha = pp.stdout.strip()

    handlers = _load_handlers()
    by_base = _tracked_paths_for(set(handlers))
    results = []
    for base, paths in sorted(by_base.items()):
        hname = getattr(handlers[base], "__name__", str(handlers[base]))
        for p in paths:
            results.append(audit_store(sha, p, hname, post_sha))

    losses = [r for r in results if r["verdict"] == "LOSS"]
    # SCOPE, reported unconditionally: a runner reports what it RAN, never what it
    # declined to look for, and a registry entry with no tracked file on this box
    # is exactly the kind of absence that reads as coverage (guard-1760).
    scope = {
        "handlers_registered": len(handlers),
        "basenames_tracked_here": len(by_base),
        "not_present_on_this_box": sorted(set(handlers) - set(by_base)),
        "audited": len(results),
        "clean": sum(1 for r in results if r["verdict"] == "CLEAN"),
        "skipped_absent_at_pre": sum(1 for r in results if r["verdict"] == "SKIPPED"),
        "not_audited_non_jsonl": sum(1 for r in results if r["verdict"] == "NOT-AUDITED"),
        "loss": len(losses),
    }
    payload = {"pre_sha": sha, "post_sha": post_sha or "<working tree>",
               "scope": scope, "stores": results}

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"═══ MERGE RECORD AUDIT — {sha[:12]} -> "
              f"{(post_sha[:12] if post_sha else 'working tree')} ═══")
        print(f"scope: {scope['audited']} store file(s) from {scope['handlers_registered']} "
              f"registered handlers ({scope['basenames_tracked_here']} basenames tracked here); "
              f"{scope['clean']} clean, {scope['skipped_absent_at_pre']} skipped (absent at pre), "
              f"{scope['not_audited_non_jsonl']} not audited (non-JSONL), {scope['loss']} LOSS")
        for r in results:
            if r["verdict"] == "CLEAN":
                continue
            print(f"  [{r['verdict']}] {r['path']} ({r['handler']})")
            if r.get("reason"):
                print(f"      {r['reason']}")
            if r.get("alarm_count"):
                print(f"      {r['alarm_count']} NON-TERMINAL record(s) unreachable "
                      f"({r.get('absent_expected_terminal',0)} more were terminal at the "
                      f"pre-state and left via archival — expected, not loss):")
                for a in r.get("alarm_ids") or []:
                    print(f"        {a['id']} [{a.get('priority')}] status_at_pre="
                          f"{a.get('status_at_pre')} :: {a.get('title')}")
            if r.get("lost_rows"):
                print(f"      lost {r['lost_rows']} unique row(s); sample: {r.get('sample_lost')}")
            for g in (r.get("regressions") or []):
                print(f"      REGRESSION {g}")
        if not losses:
            print("VERDICT: NOTHING LOST IN ANY STORE. Counts may move in either "
                  "direction — a dedup or re-serialization is not loss (guard-598).")
        else:
            print(f"VERDICT: GENUINE LOSS in {len(losses)} store(s) — restore the "
                  "dropped records/fields and READ THEM BACK (guard-424).")
    return RC_LOSS if losses else RC_OK


if __name__ == "__main__":
    sys.exit(main())
