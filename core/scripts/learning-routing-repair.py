#!/usr/bin/env python3
"""learning-routing-repair.py — one-shot cleanup of dangling cross-references.

Runs the audit's cross-reference check, then (with --apply) sets every
dangling field to null in its source JSONL. Old values are preserved in an
append-only journal at world/.history/learning-routing-repair-YYYY-MM-DD.jsonl
so the prior author's intent is not destroyed.

After --apply, the audit returns 0 dangling cross-references, and the invariant
'dangling == 0' becomes permanent (enforced by the advisory + ratchet baseline
wired into /verify-learning).

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
    # find which agent's experience file contains this record ID
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


def repair_file(path, repairs):
    """Rewrite one JSONL file, applying all repairs matching its records.

    A record may have multiple findings (e.g., two bad list-elements in the
    same field, or dangling refs in different fields). All are applied — the
    grouping is (record_id → list-of-findings), not record_id → single-finding.
    Atomic: writes to .tmp then renames.
    """
    if not path.exists():
        return []
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
    return applied


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
        print(f"  {store} ({path}): {len(refs)} refs")
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
    for (store, path_str), refs in grouped.items():
        applied = repair_file(Path(path_str), refs)
        for a in applied:
            a["store"] = store
            a["file"] = path_str
            a["repaired_at"] = datetime.now().isoformat(timespec="seconds")
            all_applied.append(a)

    # Append to history journal (create if missing)
    with history_path.open("a", encoding="utf-8") as f:
        for a in all_applied:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")

    print()
    print(f"APPLIED — {len(all_applied)} fields nulled across {len(grouped)} files.")
    print(f"History journal: {history_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
