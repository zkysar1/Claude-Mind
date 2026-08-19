#!/usr/bin/env python3
"""learning-routing-repair.py — one-shot cleanup of dangling cross-references.

Runs the audit's cross-reference check, then (with --apply) sets every
dangling field to null in its source JSONL. Old values are preserved in an
append-only journal at world/.history/learning-routing-repair-YYYY-MM-DD.jsonl
so the prior author's intent is not destroyed.

--apply drives the audit toward 0 dangling cross-references for every
MERGE-REGISTERED store. It does NOT reach zero overall, and that is deliberate
since g-115-5659: a store with no commutative merge handler (write-class (b) —
today the per-agent experience files) is REFUSED rather than raw-written, so
its refs stay reported by the audit. Do not read a non-zero audit total as this
tool having failed, and do not "fix" it by removing the refusal — the refused
population is dominated by refs that are not dangling at all, only unresolvable
by THIS tool's resolver (measured: 651 of 772 resolve by leaf tree-key name or
in pipeline-archive.jsonl). See repair_file's WRITE-CLASS GATE for the incident.

Default is --dry-run: prints what would be repaired. Use --apply to write.

Exit codes:
  0  clean (nothing to repair) OR repair succeeded
  1  dangling refs found but not applied (dry-run)
  2  input or write error
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import WORLD_DIR, agents_root  # type: ignore

# Reuse the audit's classifier and store loaders
import importlib.util
_audit_spec = importlib.util.spec_from_file_location(
    "learning_routing_audit",
    Path(__file__).parent / "learning-routing-audit.py",
)
audit = importlib.util.module_from_spec(_audit_spec)
_audit_spec.loader.exec_module(audit)


# Each repair target: (store_name, jsonl_path, field_path, value_transform)
# value_transform takes the current field value + the dangling ref and returns
# the new field value (usually None, but supports list-element removal for
# tree_nodes_related).

STORE_PATHS = {
    "reasoning_bank": WORLD_DIR / "reasoning-bank.jsonl",
    "guardrails": WORLD_DIR / "guardrails.jsonl",
    "pipeline": WORLD_DIR / "pipeline.jsonl",
    "experience": None,  # per-agent glob, resolved at write time
}


def _resolve_store_path(store, record_id):
    """experience records live in per-agent files; all others in world/.

    Routed through agents_root() (guard-1318, CLAUDE.md "cross-agent glob
    consumers"). This read was PROJECT_ROOT.glob("*/experience.jsonl") until
    2026-08-10 (g-115-5646) — depth-1, matching nothing post-relocation, so
    every experience-store repair silently resolved to None. Its twin in
    learning-routing-audit.load_all_experiences() was the destructive half:
    this module imports that function, so a zero-record corpus there made the
    audit call EVERY experience_ref dangling and this script nulled them.
    """
    if store != "experience":
        return STORE_PATHS[store]
    # find which agent's experience file contains this record ID.
    #
    # The ARCHIVE is unioned in for a reason specific to THIS file rather than
    # its audit twin: a repair is a WRITE, and resolving a record to None here
    # means "not found", which is indistinguishable from "not mine". Live file
    # first — an id present in both should resolve to the live one, which a
    # rotation would have written most recently.
    for exp_path in sorted(agents_root().glob("*/experience.jsonl")) + \
            sorted(agents_root().glob("*/experience-archive.jsonl")):
        for line in exp_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("id") == record_id:
                return exp_path
    return None


def _apply_null_to_field(record, field, ref):
    """Return (new_record, old_value) with field set to null, removing ref from
    list-valued fields instead of nulling the whole list."""
    old_value = record.get(field)
    if isinstance(old_value, list):
        new_list = [v for v in old_value if v != ref]
        record[field] = new_list if new_list else None
    else:
        record[field] = None
    return record, old_value


def is_merge_protected(path):
    """True when `path` has a commutative merge handler (write-class (a)).

    Resolved through `coordination_merge.merge_handler_for`, which is the ONE
    authoritative classifier — never a basename grep. Its own docstring is
    explicit that three path-pattern branches run before the dict lookup, so a
    store can be protected with no basename entry and unprotected despite one.

    Fails CLOSED: if the classifier cannot be imported or raises, the answer is
    "not protected", which refuses the write. An unreadable classifier must not
    silently re-enable the destructive path this guard exists to close.
    """
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from coordination_merge import merge_handler_for  # type: ignore
        return merge_handler_for(path) is not None
    except Exception as exc:  # noqa: BLE001 — see fail-closed note above
        print(f"[learning-routing-repair] merge_handler_for unavailable "
              f"({exc.__class__.__name__}: {exc}) — treating {path} as "
              f"UNPROTECTED and refusing the write", file=sys.stderr)
        return False


def repair_file(path, repairs):
    """Rewrite one JSONL file, applying all repairs matching its records.

    A record may have multiple findings (e.g., two bad list-elements in the
    same field, or dangling refs in different fields). All are applied — the
    grouping is (record_id → list-of-findings), not record_id → single-finding.
    Atomic: writes to .tmp then renames.

    Returns ``(applied, refused)``. ``refused`` is non-empty only for a store
    with no commutative merge handler — see the write-class gate below.

    WRITE-CLASS GATE (g-115-5659). This function raw-writes: read, mutate,
    ``.tmp``, ``replace``. That is safe for a merge-registered store — a
    concurrent write that loses the race is reconciled by the store's handler
    on the next sync — and UNSAFE for one without a handler, where the last
    writer wins outright and the loser's record is gone with no reconciler
    below it to restore it. So the gate is per-PATH at the write, resolved
    through ``merge_handler_for``.

    The measured incident: the day this file's experience-store lookup was
    repaired (g-115-5646) its experience branch went live for the first time,
    and the very next automatic run nulled 772 experience-side fields across
    12 files and 6 agents. 651 of those 772 were VALID — 460 tree refs resolve
    by LEAF name and 191 hypothesis refs resolve in ``pipeline-archive.jsonl``,
    neither of which this tool's resolver consults. The 8 runs before the fix
    had been nulling ~315 ``pipeline.experience_ref`` each, and the count never
    moved, because ``pipeline.jsonl`` IS merge-registered and its handler put
    them back every time — a destructive loop that ran for days and was
    invisible precisely because the self-heal worked. Experience stores have no
    such handler, so nothing puts them back.

    Refusing is the honest stop-gap and not merely the cheap one: a "dangling"
    ref that resolves under a resolver this tool does not implement was never
    dangling, and nulling it to drive a counter to zero destroys the author's
    intent to satisfy the audit. The refused refs stay reported by
    ``learning-routing-audit`` every run, which is the correct standing signal
    — so nothing here manufactures a false all-clear (guard-1760); the caller
    that captures and discards this output loses a NON-action, never data.
    """
    if not is_merge_protected(path):
        print(f"[learning-routing-repair] REFUSED {len(repairs)} repair(s) in "
              f"{path}: no commutative merge handler (write-class (b)) — a raw "
              f"write here cannot be reconciled if it loses a race, and these "
              f"refs may resolve under a resolver this tool does not implement. "
              f"Left intact; the audit continues to report them.",
              file=sys.stderr)
        return [], list(repairs)
    if not path.exists():
        return [], []
    lines_out = []
    applied = []
    by_id = {}
    for r in repairs:
        by_id.setdefault(r["record_id"], []).append(r)
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            lines_out.append(line)
            continue
        try:
            rec = json.loads(stripped)
        except json.JSONDecodeError:
            lines_out.append(line)
            continue
        rec_id = rec.get("id")
        if rec_id in by_id:
            for r in by_id[rec_id]:
                rec, old = _apply_null_to_field(rec, r["field"], r["ref"])
                applied.append({
                    "record_id": rec_id,
                    "field": r["field"],
                    "old_value": old,
                    "removed_ref": r["ref"],
                    "new_value": rec.get(r["field"]),
                })
        lines_out.append(json.dumps(rec, ensure_ascii=False))
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    tmp_path.replace(path)
    return applied, []


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="Write repairs to disk. Default is dry-run.")
    args = ap.parse_args()

    stores = {
        "reasoning_bank": audit.load_reasoning_bank(),
        "guardrails": audit.load_guardrails(),
        "pipeline": audit.load_pipeline(),
        "pattern_signatures": audit.load_pattern_signatures(),
        "experience": audit.load_all_experiences(),
    }
    tree_keys = audit.load_tree_node_keys()
    ids = audit.build_id_sets(stores)
    dangling, _prose = audit.audit_cross_refs(stores, ids, tree_keys)

    if not dangling:
        print("CLEAN — no dangling refs to repair.")
        return 0

    # Group repairs by (store, file_path)
    grouped = {}
    for d in dangling:
        store = d["store"]
        path = _resolve_store_path(store, d["record_id"])
        if path is None:
            print(f"WARN: could not resolve file for {store}/{d['record_id']}",
                  file=sys.stderr)
            continue
        grouped.setdefault((store, str(path)), []).append(d)

    print(f"Found {len(dangling)} dangling refs across {len(grouped)} files.")
    for (store, path), refs in grouped.items():
        # The write-class marker is printed on BOTH paths, not just --apply.
        # A dry run that lists a ref it will silently decline to touch is the
        # same misleading-report defect this gate exists to end.
        mark = "" if is_merge_protected(Path(path)) else "  [WILL REFUSE — write-class (b)]"
        print(f"  {store} ({path}): {len(refs)} refs{mark}")
        for r in refs[:3]:
            print(f"    {r['record_id']}.{r['field']} = {r['ref']!r}")
        if len(refs) > 3:
            print(f"    ... ({len(refs) - 3} more)")

    if not args.apply:
        print()
        print("DRY RUN — no changes written. Re-run with --apply to repair.")
        return 1

    # Prepare the history journal BEFORE writing repairs
    history_dir = WORLD_DIR / ".history"
    history_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    history_path = history_dir / f"learning-routing-repair-{today}.jsonl"

    all_applied = []
    all_refused = []
    for (store, path_str), refs in grouped.items():
        applied, refused = repair_file(Path(path_str), refs)
        for a in applied:
            a["store"] = store
            a["file"] = path_str
            a["repaired_at"] = datetime.now().isoformat(timespec="seconds")
            all_applied.append(a)
        for r in refused:
            all_refused.append((store, path_str, r))

    # Append to history journal (create if missing). ONLY applied changes go
    # here: this journal is undo-data, and a refusal changed nothing, so giving
    # it a row would put entries with no `old_value` in front of every reader
    # that treats a row as "something to restore".
    with history_path.open("a", encoding="utf-8") as f:
        for a in all_applied:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")

    print()
    print(f"APPLIED — {len(all_applied)} fields nulled across "
          f"{len(grouped) - len({p for _s, p, _r in all_refused})} files.")
    print(f"History journal: {history_path}")
    if all_refused:
        refused_files = sorted({p for _s, p, _r in all_refused})
        print()
        print(f"REFUSED — {len(all_refused)} ref(s) across {len(refused_files)} "
              f"file(s) left INTACT (write-class (b): no merge handler).")
        for p in refused_files:
            n = sum(1 for _s, pp, _r in all_refused if pp == p)
            print(f"  {p}: {n} ref(s)")
        print("  These stay reported by learning-routing-audit until either the")
        print("  audit's resolver is widened (leaf-name tree keys, pipeline-archive")
        print("  hypothesis ids) or the refs are repaired through a fenced writer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
