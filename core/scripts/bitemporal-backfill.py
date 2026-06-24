#!/usr/bin/env python3
"""Bi-temporal validity-interval backfill (, BRD Gap 5 sub-goal C).

Backfills valid_from=created (valid_to left null) on EXISTING records that
predate the g-306-35 bi-temporal writer path, so their validity history is
complete retroactively. New records keep the writer default valid_from=null
(implicitly-valid-from-now); the falsification path (close-old-insert-new)
sets explicit timestamps going forward. This is a ONE-TIME retroactive
completion, not an ongoing writer change.

Scope (schema-bearing stores only — see store_registry.py / reasoning-bank.py):
  - reasoning-bank  (world/reasoning-bank.jsonl)   valid_from <- created
  - guardrails      (world/guardrails.jsonl)        valid_from <- created
Out of scope (documented in the goal closure):
  - beliefs         knowledge/beliefs.yaml is empty (0 records); team-state ToM
                    beliefs are ephemeral session-state, not persistent records.

Tree front matter (g-306-38): now handled by the --tree mode below. 443 of 1109
tree-node .md files carry `created` and gain a `valid_from: <created>` line INSIDE
the YAML front matter (guard-518); the created-absent rest are left untouched (no
valid_from is fabricated from last_updated). valid_to is left ABSENT (== null ==
still-current for the reader), matching the JSONL path which leaves valid_to null.
The migration is a TEXTUAL single-line insertion (NOT a YAML round-trip) so every
untouched byte -- body and all other front-matter lines -- is preserved exactly;
node front matter is not always round-trip-safe through a YAML dumper (inline flow
maps, colons inside unquoted scalars). Writes are binary (no newline translation:
60 of 1109 nodes are CRLF, the rest LF, none mixed).

R2 risk mitigation (medium risk), per the goal contract:
  - lock-safe: writes via _fileops.locked_modify_jsonl, whose file lock is
    cross-process (coordinates with the running daemon + sibling agents), so a
    concurrent append is INSIDE the locked items list, never clobbered.
  - .history snapshot BEFORE mutating: locked_modify_jsonl calls save_history
    per write attempt (and append_changelog "edit").
  - --dry-run verifier (default): projects the migration in memory and asserts
    NO corruption WITHOUT writing.
  - no-corruption invariant (enforced in both dry-run projection and post-apply
    re-read): every pre-existing record id survives; every non-temporal field's
    content-hash is unchanged; the ONLY mutation is valid_from null->created.
  - NEVER deletes a record (training-data preservation).
  - mtime-gated daemon cache (jsonl_cache.py) auto-reloads after the write.

Usage:
  py -3 core/scripts/bitemporal-backfill.py [--apply] [--store STORE ...]
  py -3 core/scripts/bitemporal-backfill.py --tree [--apply]
  (default is dry-run; no flags => reasoning-bank + guardrails JSONL stores;
   --tree => tree-node .md front matter)

Exit codes: 0 = success (dry-run clean OR apply verified), 1 = verifier failure
(corruption detected — apply aborts before/at write; dry-run reports and exits 1).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

# core/scripts is on sys.path for sibling imports when run as a script.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _fileops import (  # noqa: E402
    acquire_lock,
    append_changelog,
    locked_modify_jsonl,
    read_jsonl_with_recovery,
    release_lock,
    save_history,
)
from _paths import WORLD_DIR  # noqa: E402

# Temporal fields the migration is allowed to touch. Everything else must be
# byte-identical pre/post (the no-corruption invariant).
TEMPORAL_FIELDS = ("valid_from", "valid_to")

STORE_PATHS = {
    "reasoning-bank": Path(WORLD_DIR) / "reasoning-bank.jsonl",
    "guardrails": Path(WORLD_DIR) / "guardrails.jsonl",
}


def _nontemporal_hash(rec: dict) -> str:
    """Content-hash of a record EXCLUDING the temporal fields. Two records with
    identical non-temporal content hash the same regardless of valid_from/valid_to.
    Sort keys so field-order changes never register as content changes."""
    stripped = {k: v for k, v in rec.items() if k not in TEMPORAL_FIELDS}
    payload = json.dumps(stripped, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return read_jsonl_with_recovery(path)


def _project(records: list[dict]) -> tuple[list[dict], int, list[str]]:
    """Pure projection of the migration over a records list. Returns
    (new_records, changed_count, skipped_no_created_ids). Does NOT mutate the
    input (deep-ish copy per record so the verifier can diff pre vs post)."""
    out = []
    changed = 0
    skipped_no_created = []
    for rec in records:
        new = dict(rec)
        vf = new.get("valid_from")
        created = new.get("created")
        if vf is None and created:
            new["valid_from"] = created
            # valid_to deliberately untouched (stays null/default).
            changed += 1
        elif vf is None and not created:
            skipped_no_created.append(new.get("id", "<no-id>"))
        out.append(new)
    return out, changed, skipped_no_created


def _verify(pre: list[dict], post: list[dict]) -> tuple[bool, list[str]]:
    """No-corruption invariant. Returns (ok, problems).

    Concurrency-aware: POST may contain MORE ids than PRE (a concurrent append
    that landed before our lock) — that is NOT corruption. The invariants are:
      1. No PRE id is missing from POST (no deletion).
      2. Every PRE id's non-temporal content-hash is unchanged in POST.
      3. valid_to is never mutated by this migration.
      4. The only valid_from mutation allowed is null -> (its own `created`).
    """
    problems: list[str] = []
    post_by_id = {}
    for r in post:
        rid = r.get("id")
        if rid is not None:
            post_by_id[rid] = r

    pre_by_id = {}
    for r in pre:
        rid = r.get("id")
        if rid is not None:
            pre_by_id[rid] = r

    # 1 + 2 + 3 + 4
    for rid, pre_rec in pre_by_id.items():
        post_rec = post_by_id.get(rid)
        if post_rec is None:
            problems.append(f"DELETED: record {rid} present pre, missing post")
            continue
        if _nontemporal_hash(pre_rec) != _nontemporal_hash(post_rec):
            problems.append(
                f"CONTENT-CHANGED: non-temporal fields differ for {rid}")
        # valid_to must not change
        if pre_rec.get("valid_to") != post_rec.get("valid_to"):
            problems.append(
                f"VALID_TO-MUTATED: {rid} valid_to "
                f"{pre_rec.get('valid_to')!r} -> {post_rec.get('valid_to')!r}")
        # valid_from change must be null -> own created (or unchanged)
        pre_vf = pre_rec.get("valid_from")
        post_vf = post_rec.get("valid_from")
        if pre_vf != post_vf:
            if pre_vf is not None:
                problems.append(
                    f"VALID_FROM-OVERWRITE: {rid} already had valid_from "
                    f"{pre_vf!r}, changed to {post_vf!r}")
            elif post_vf != post_rec.get("created"):
                problems.append(
                    f"VALID_FROM-WRONG: {rid} valid_from set to {post_vf!r}, "
                    f"expected its created {post_rec.get('created')!r}")
    return (len(problems) == 0), problems


def run_store(store: str, apply: bool) -> dict:
    path = STORE_PATHS[store]
    pre = _read_records(path)
    projected, changed, skipped = _project(pre)
    ok, problems = _verify(pre, projected)

    result = {
        "store": store,
        "path": str(path),
        "pre_count": len(pre),
        "would_change": changed,
        "skipped_no_created": len(skipped),
        "verify_ok": ok,
        "problems": problems[:20],
        "applied": False,
    }
    if not ok:
        return result  # never write on a failed projection

    if apply and changed > 0:
        def _modifier(items: list[dict]) -> list[dict]:
            new_items, _c, _s = _project(items)
            return new_items
        locked_modify_jsonl(path, _modifier)
        # Re-read from disk and re-verify against the ORIGINAL pre snapshot.
        post = _read_records(path)
        ok2, problems2 = _verify(pre, post)
        # New ids that appeared post-write (concurrent appends) are informational.
        pre_ids = {r.get("id") for r in pre}
        new_ids = [r.get("id") for r in post if r.get("id") not in pre_ids]
        backfilled = sum(
            1 for r in post
            if r.get("id") in pre_ids and r.get("valid_from") is not None
            and r.get("valid_from") == r.get("created"))
        result.update({
            "applied": True,
            "post_count": len(post),
            "post_verify_ok": ok2,
            "post_problems": problems2[:20],
            "concurrent_appends": [i for i in new_ids if i],
            "backfilled_now_set": backfilled,
        })
        result["verify_ok"] = ok2
    return result


# ---------------------------------------------------------------------------
# Tree-front-matter mode (). Structurally distinct from the JSONL stores
# above (per-file .md walk vs whole-file JSONL RMW), so it carries its own pure
# projector + verifier + writer. The pure pair mirrors _project / _verify so the
# same no-corruption invariant style holds and is unit-testable without disk.
# ---------------------------------------------------------------------------

_TREE_SUBDIR = ("knowledge", "tree")


def _agent_name() -> str:
    return os.environ.get("MIND_AGENT", "system") or "system"


def _frontmatter_close_idx(lines: list) -> "int | None":
    """Index of the closing '---' delimiter in splitlines(keepends=True) output,
    or None when there is no well-formed front matter. The opening '---' must be
    line 0. Lines are compared with trailing CR/LF stripped so both LF and CRLF
    delimiters match."""
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            return i
    return None


def _body_after_frontmatter(raw: bytes) -> "bytes | None":
    """Bytes from the closing '---' line (inclusive) to EOF, or None if the front
    matter is malformed. Decoded/encoded with surrogateescape so any byte
    round-trips losslessly. Feeds the content-hash body verifier."""
    lines = raw.decode("utf-8", "surrogateescape").splitlines(keepends=True)
    close_idx = _frontmatter_close_idx(lines)
    if close_idx is None:
        return None
    return "".join(lines[close_idx:]).encode("utf-8", "surrogateescape")


def _project_tree_md(raw: bytes) -> "tuple[bytes, bool, str]":
    """Pure projection of the tree migration over ONE .md file's bytes. Returns
    (new_bytes, changed, reason). Inserts a single `valid_from: <created-value>`
    line immediately after the top-level `created:` line inside the front matter,
    copying the value (and the line's exact ending) verbatim so valid_from ==
    created in both value and format. Does NOT touch disk; does NOT mutate input."""
    lines = raw.decode("utf-8", "surrogateescape").splitlines(keepends=True)
    close_idx = _frontmatter_close_idx(lines)
    if close_idx is None:
        return raw, False, "no-front-matter"
    fm = lines[1:close_idx]  # between delimiters; top-level keys at column 0
    if any(re.match(r"^valid_from:", ln) for ln in fm):
        return raw, False, "already-has-valid_from"
    created_rel = next((j for j, ln in enumerate(fm)
                        if re.match(r"^created:", ln)), None)
    if created_rel is None:
        return raw, False, "no-created"
    created_line = fm[created_rel]
    # Substitute ONLY the key token; the separator, value, and line ending carry
    # over byte-for-byte (handles quoted "2026-05-07" and bare 2026-05-07 alike).
    valid_from_line = re.sub(r"^created:", "valid_from:", created_line, count=1)
    abs_idx = 1 + created_rel  # created line index in the absolute list
    new_lines = lines[:abs_idx + 1] + [valid_from_line] + lines[abs_idx + 1:]
    return ("".join(new_lines).encode("utf-8", "surrogateescape"), True,
            "inserted")


def _verify_tree_md(pre: bytes, post: bytes) -> "tuple[bool, list]":
    """No-corruption invariant for ONE .md file. Returns (ok, problems). Two
    independent checks: (1) the body (closing '---' onward) is content-hash
    identical pre/post; (2) post is pre with EXACTLY one inserted valid_from
    line -- removing it reproduces pre byte-for-byte."""
    problems: list = []
    pre_body = _body_after_frontmatter(pre)
    post_body = _body_after_frontmatter(post)
    if pre_body is None or post_body is None:
        problems.append("FRONT-MATTER-MALFORMED")
    elif (hashlib.sha256(pre_body).hexdigest()
          != hashlib.sha256(post_body).hexdigest()):
        problems.append("BODY-HASH-CHANGED: body content-hash differs pre/post")
    pre_lines = pre.decode("utf-8", "surrogateescape").splitlines(keepends=True)
    post_lines = post.decode("utf-8", "surrogateescape").splitlines(keepends=True)
    if len(post_lines) != len(pre_lines) + 1:
        problems.append(
            f"LINE-COUNT: post={len(post_lines)} expected={len(pre_lines) + 1}")
    else:
        div = next((i for i in range(len(pre_lines))
                    if pre_lines[i] != post_lines[i]), len(pre_lines))
        inserted = post_lines[div] if div < len(post_lines) else ""
        if not re.match(r"^valid_from:", inserted):
            problems.append(f"INSERT-NOT-VALID_FROM: {inserted!r}")
        if "".join(post_lines[:div] + post_lines[div + 1:]) != "".join(pre_lines):
            problems.append("RECONSTRUCT-MISMATCH: post minus inserted != pre")
    return (len(problems) == 0), problems


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomic byte-level overwrite: sibling temp file -> fsync -> os.replace with
    bounded retry for transient Windows/own-cloud PermissionError. BINARY so no
    newline translation can corrupt a CRLF node."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".bitemp-",
                                    suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                tmp = None
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(min(5.0, 0.1 * (2 ** attempt)))
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()


_BACKFILL_SUMMARY = "bitemporal valid_from backfill, tree front matter (g-306-38)"


def _write_tree_md(path: Path, data: bytes) -> None:
    """Lock -> per-file .history snapshot -> atomic byte write. The .history
    snapshot (the AC requirement, giving per-node restorability) is taken here;
    the changelog entry is written ONCE per run by run_tree as a single aggregate
    record, NOT 443 per-file lines on the own-cloud-synced changelog.jsonl -- the
    JSONL path likewise logs one changelog entry per store, not per record."""
    lock_path = path.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        save_history(path, WORLD_DIR, _agent_name(), summary=_BACKFILL_SUMMARY)
        _atomic_write_bytes(path, data)
    finally:
        release_lock(lock_path)


def run_tree(apply: bool) -> dict:
    """Walk every tree-node .md under WORLD/knowledge/tree, project the
    valid_from insertion, verify, and (when apply) write. Dry-run projects +
    verifies in memory with NO disk writes (default). A file whose projection
    fails verify is NEVER written."""
    tree_root = Path(WORLD_DIR).joinpath(*_TREE_SUBDIR)
    md_files = sorted(tree_root.rglob("*.md"))
    skipped: dict = {}
    changed_files: list = []
    problems: list = []
    applied = 0
    for p in md_files:
        raw = p.read_bytes()
        post, changed, reason = _project_tree_md(raw)
        if not changed:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        ok, probs = _verify_tree_md(raw, post)
        if not ok:
            problems.append({"file": str(p.relative_to(tree_root)),
                             "problems": probs})
            continue  # never write a file whose projection fails verify
        changed_files.append(str(p.relative_to(tree_root)))
        if apply:
            _write_tree_md(p, post)
            # Re-read from disk and re-verify against the ORIGINAL pre bytes.
            ok2, probs2 = _verify_tree_md(raw, p.read_bytes())
            if ok2:
                applied += 1
            else:
                problems.append({"file": str(p.relative_to(tree_root)),
                                 "post_problems": probs2})
    # One aggregate changelog edit record for the whole bulk migration (the
    # per-file audit trail lives in .history). Only when something was actually
    # written.
    if apply and applied > 0:
        append_changelog(
            WORLD_DIR, _agent_name(), tree_root, "edit",
            summary=f"{_BACKFILL_SUMMARY}: {applied} node .md front matters "
                    f"(valid_from<-created)",
            lines_changed=applied)
    return {
        "store": "tree-front-matter",
        "tree_root": str(tree_root),
        "total_md": len(md_files),
        "would_change": len(changed_files),
        "applied": applied if apply else 0,
        "skipped": skipped,
        "changed_files_sample": changed_files[:20],
        "verify_ok": len(problems) == 0,
        "problems": problems[:20],
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Bi-temporal valid_from backfill "
                    "(g-306-37 JSONL stores; g-306-38 tree front matter)")
    ap.add_argument("--apply", action="store_true",
                    help="perform the write (default: dry-run projection only)")
    ap.add_argument("--store", action="append", choices=sorted(STORE_PATHS),
                    help="JSONL store(s) to migrate (repeatable; "
                         "default: all schema-bearing)")
    ap.add_argument("--tree", action="store_true",
                    help="backfill tree-node .md front matter (valid_from <- "
                         "created); runs INSTEAD of the JSONL stores unless "
                         "--store is also given")
    args = ap.parse_args()

    overall_ok = True
    results = []

    # Preserve the historical default exactly: with no flags (or --store), run
    # the JSONL stores. --tree alone runs ONLY the tree mode.
    run_jsonl = bool(args.store) or not args.tree
    if run_jsonl:
        stores = args.store or sorted(STORE_PATHS)
        for s in stores:
            r = run_store(s, args.apply)
            results.append(r)
            overall_ok = overall_ok and r["verify_ok"]

    if args.tree:
        rt = run_tree(args.apply)
        results.append(rt)
        overall_ok = overall_ok and rt["verify_ok"]

    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "overall_ok": overall_ok,
        "stores": results,
    }, indent=2, ensure_ascii=False))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
