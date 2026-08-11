#!/usr/bin/env python3
"""One-shot reconcile sweep for experience .md ⇄ jsonl mismatches.

Created for g-001-256 (rb-443 trade-off confirmation in g-248-32).

Mismatch types and how each is handled:

  orphan_md  — .md exists at <agent>/experience/<id>.md but no jsonl
               record with that id is in EITHER index — experience.jsonl
               or experience-archive.jsonl (see INDEX_FILES).
               Action: parse YAML front matter from the .md, extract
               type/category/summary (with fallbacks: filename gives id;
               first H1 or first non-front-matter paragraph gives summary
               if missing), append a normalized jsonl record. Skip with
               a defer reason if any required field cannot be recovered.

  missing_md — jsonl record references content_path that does not exist.
               Action: regenerate a stub .md containing the YAML front
               matter and the record's `summary` as body. Skip with a
               defer reason if the record's `summary` is empty or the
               `content_path` field itself is missing.

The script writes its decisions to
core/logs/experience-reconcile-defer-{agent}.jsonl per agent for any
record that is genuinely unrecoverable (front matter missing, summary
empty, etc.) — no synthetic content is generated for the unrecoverable
cases. Skip-and-log keeps the audit honest.

Idempotent: skips orphan_md whose id is already in EITHER index — live
or archive (cross-agent or re-run) — and skips missing_md whose .md has
been re-created. Safe to re-run.

ARCHIVE-BLINDNESS (g-115-4570, fixed 2026-08-02). Until this fix the
orphan difference was taken against the LIVE index alone, so every record
that had aged out of experience.jsonl into experience-archive.jsonl was
reported as an orphan and would have been backfilled into the live index
by --apply. The two stores are near-disjoint by construction (measured
fleet-wide: |live ∩ archive| is 0-2 per agent), because archival MOVES a
row rather than copying it — so "absent from live" means "archived", not
"unindexed". Measured effect of scanning both, 2026-08-02 on cc-04:

    agent    orphan (live-only)   orphan (both)   wrongly reported
    alpha            517               136              381
    bravo            548                43              505
    echo             183                42              141
    foxtrot          294                82              212
    zeta             299                72              227

That is 1,841 reported vs 375 real fleet-wide — a 4.9x inflation, and
1,466 index rows --apply would have appended for traces that were already
indexed. Two independent surfaces already treated the archive as part of
the id namespace and were not consulted when this script was written:
experience-orphan-ratchet.py's INDEX_FILES ("the archive is an index, not
a graveyard"), and the daemon write path, whose _check_no_duplicate_id
spans BOTH stores and returns 409 duplicate_id for exactly the appends
this script would have made. Note --apply calls locked_append_jsonl
directly, so that 409 never fires here — the guard exists but this code
path routes around it.

The archived-but-not-live population is NOT silently dropped: it is
reported as `archived_indexed` in the per-agent result, so a run cannot
claim a clean orphan count while declining to say what it excluded.
Symmetrically, `archive_unparsed` counts archive rows the guard could not
read — each is an id missing from the guard set, i.e. the pre-fix hazard
returning one record at a time — and an archive that exists but cannot be
opened raises rather than degrading to an empty guard (see archived_ids).

Usage:
  py -3 core/scripts/experience-reconcile.py                  # dry-run
  py -3 core/scripts/experience-reconcile.py --apply          # write
  py -3 core/scripts/experience-reconcile.py --agent alpha    # one
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _paths import PROJECT_ROOT, agent_dir as _agent_dir, enumerate_agent_confs  # noqa: E402
from _fileops import locked_append_jsonl  # noqa: E402

# SSOT: the experience TYPE enum is OWNED by the write side (experience.py:35).
# This audit used to carry its own copy of the set, and the two drifted the day
# after this file was written: `chat_session` entered experience.py 2026-05-08
# (50ba8a0b1), one day after this file landed (86a6e5950, 2026-05-07), and was
# never mirrored here. The write side therefore accepted /encode-session records
# that this audit reported as unrecoverable orphans — measured :
# 15 chat_session records live in the store, and 9 orphan .md files deferred as
# "type unrecoverable". Importing the owner's set makes that class of divergence
# structurally impossible rather than depending on someone remembering a second
# edit. Safe in every context this script runs in: experience.py's module-level
# work is a stdio reconfigure plus Path constants, and it imports cleanly with
# no agent bound (AGENT_DIR=None is guarded there).
from experience import VALID_TYPES  # noqa: E402

# Front-matter parser is intentionally flat-only: handles `key: value` lines
# but not lists or nested mappings. All currently-consumed fields (type,
# category, goal_id, hypothesis_id, date) are flat strings, so this is
# adequate. If the experience .md schema evolves to include list-typed
# fields (e.g., `tags: [a, b]`), upgrade to a real YAML parser.
FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(\n|$)", re.DOTALL)
GOAL_ID_FROM_STEM_RE = re.compile(r"^exp-(g-\d+-\d+)")

# Both index files, in the same order and with the same name as
# experience-orphan-ratchet.py's constant — deliberately, so a reader who
# finds one finds the other. See the ARCHIVE-BLINDNESS note in the module
# docstring for why the archive must be scanned ().
#
# The live index stays the sole SOURCE for the missing_md / stem_mismatch
# direction (see reconcile_agent) — those regenerate .md stubs, and doing
# that for archived rows would be a different and unrequested behavior.
#
# ARCHIVE_INDEX is named rather than reached as INDEX_FILES[1]: a positional
# read would make a future reorder of this tuple silently point archived_ids
# at the LIVE file, whereupon it returns live ids, the orphan difference
# subtracts live twice, and the fix reverts with every test but the tuple
# assertion still green.
LIVE_INDEX = "experience.jsonl"
ARCHIVE_INDEX = "experience-archive.jsonl"
INDEX_FILES = (LIVE_INDEX, ARCHIVE_INDEX)


def discover_agents() -> list:
    """Glob agent directories from PROJECT_ROOT/*/local-paths.conf.

    Replaces hardcoded ('alpha', 'bravo') tuples. Same pattern as
    recovery-gate.sh (lines 324, 334, 351). Adding a third agent
    (e.g., 'charlie') with its own local-paths.conf works without
    code change.
    """
    return sorted(
        p.parent.name
        for p in enumerate_agent_confs()
        if p.is_file()
    )


def load_goal_index() -> dict:
    """Build {goal_id: (aspiration_id, category)} from world+agent queues.

    Used by reconcile_agent to recover category (and confirm goal_id
    existence) for orphan_md whose front matter is sparse. Reads world
    queue path from any agent's local-paths.conf — paths are identical
    across agents. World aspirations are loaded LAST so they win on
    duplicate goal_ids (the world copy is the source of truth).

    Includes aspirations-archive.jsonl alongside live aspirations.jsonl —
    most leaked orphans reference goals that were since archived (e.g.,
    bravo's 2026-04-10 research run under asp-225). Without archive
    coverage the goal-index lookup misses everything older than the
    current portfolio. Archive files use the same schema as live ones.
    Aspiration-level category (when set) is also recorded as a fallback
    for goals that exist but lack their own `category` field (e.g., some
    asp-001 recurring goals).
    """
    queues = []
    agents = discover_agents()
    for agent in agents:
        p = _agent_dir(agent) / "aspirations.jsonl"
        if p.exists():
            queues.append(p)
        p = _agent_dir(agent) / "aspirations-archive.jsonl"
        if p.exists():
            queues.append(p)
    for agent in agents:
        local_paths = _agent_dir(agent) / "local-paths.conf"
        if not local_paths.exists():
            continue
        for line in local_paths.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("WORLD_PATH="):
                world_root = Path(line.split("=", 1)[1].strip().strip('"'))
                for fname in ("aspirations.jsonl", "aspirations-archive.jsonl"):
                    wp = world_root / fname
                    if wp.exists() and wp not in queues:
                        queues.append(wp)
                break

    index = {}
    for p in queues:
        # Fail-open on unreadable queue files (permission, encoding) — match
        # the rest of this script's defer-rather-than-crash pattern.
        try:
            f = p.open(encoding="utf-8")
        except OSError:
            continue
        with f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "goals" not in rec:
                    continue
                asp_id = rec.get("id", "")
                # Aspiration-level category fallback: derived from tags[0]
                # if no explicit category.  stores ["maintenance",
                # "recurring"] which gives a reasonable category-like tag.
                asp_cat = rec.get("category", "")
                if not asp_cat and rec.get("tags"):
                    asp_cat = rec["tags"][0] if isinstance(rec["tags"], list) and rec["tags"] else ""
                for g in rec["goals"]:
                    gid = g.get("id", "")
                    if not gid:
                        continue
                    cat = g.get("category", "") or asp_cat
                    index[gid] = (asp_id, cat)
    return index


def goal_id_from_stem(stem: str) -> str:
    """Return the embedded g-NNN-NN if the exp-* stem encodes one, else ''."""
    m = GOAL_ID_FROM_STEM_RE.match(stem)
    return m.group(1) if m else ""


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Return (front_matter_dict, body_text). Empty dict on missing/invalid."""
    m = FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    fm_text = m.group(1)
    body = text[m.end():]
    fm = {}
    for line in fm_text.split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        fm[key] = value
    return fm, body


def extract_summary(body: str, fallback: str) -> str:
    """First H1 line, else first non-empty paragraph, else fallback."""
    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    for line in body.split("\n"):
        s = line.strip()
        if s and not s.startswith(("#", "---", "<")):
            return s[:200]
    return fallback


def md_for_record(rec: dict) -> str:
    """Render a stub .md from a jsonl record."""
    fm_lines = ["---"]
    for key in ("id", "type", "category"):
        if rec.get(key):
            fm_lines.append(f"{key}: {rec[key]}")
    if rec.get("goal_id"):
        fm_lines.append(f"goal_id: {rec['goal_id']}")
    if rec.get("hypothesis_id"):
        fm_lines.append(f"hypothesis_id: {rec['hypothesis_id']}")
    if rec.get("created"):
        fm_lines.append(f"date: {str(rec['created'])[:10]}")
    fm_lines.append("regenerated_by: experience-reconcile.py")
    fm_lines.append("regenerated_at: " + datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    fm_lines.append("---")
    fm_lines.append("")
    title = rec.get("summary") or rec.get("id", "(untitled)")
    fm_lines.append(f"# {title[:120]}")
    fm_lines.append("")
    cp = rec.get("content_path", "")
    if cp and (PROJECT_ROOT / cp).exists():
        fm_lines.append(
            f"> Stub regenerated from JSONL record. Trace document lives at "
            f"`{cp}` (cross-reference); this stub exists so the canonical "
            f"`<agent>/experience/<id>.md` location resolves for set-difference audits."
        )
    else:
        fm_lines.append("> Stub regenerated from JSONL record. Original .md was missing.")
    fm_lines.append("")
    fm_lines.append("## Summary")
    fm_lines.append("")
    fm_lines.append(rec.get("summary", "(no summary in JSONL record)"))
    fm_lines.append("")
    if rec.get("verbatim_anchors"):
        fm_lines.append("## Verbatim anchors (from JSONL)")
        fm_lines.append("")
        for a in rec["verbatim_anchors"]:
            if isinstance(a, dict) and "key" in a and "content" in a:
                fm_lines.append(f"- **{a['key']}**: {a['content']}")
        fm_lines.append("")
    return "\n".join(fm_lines) + "\n"


def stamp_now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def archived_ids(agent_dir: Path) -> tuple:
    """Return (ids indexed in the archive, count of unparseable archive lines).

    Read separately from the live index rather than merged into `jsonl_ids`
    because the two are used for different directions: this set only ever
    SUBTRACTS from the orphan candidates, while `jsonl_ids` remains the
    record SOURCE for missing_md / stem_mismatch stub regeneration.

    DELIBERATELY NOT FAIL-OPEN ON AN UNREADABLE ARCHIVE, and this is the one
    non-obvious choice in the function. Everywhere else this script prefers
    defer-rather-than-crash, but that posture is about individual RECORDS it
    cannot recover; here the archive is a SAFETY INPUT. Every id it fails to
    return is an id that becomes an orphan candidate and, under --apply, a
    duplicate append into the live index — the exact hazard this function
    exists to prevent. So an empty set must mean "the archive genuinely has
    no ids", never "the archive could not be read": absence (a fresh agent
    with no archive yet) returns empty and is legitimate; an OSError on a
    file that EXISTS propagates rather than silently disarming the guard.

    A malformed LINE is the residual case and stays a `continue`, matching
    how the live-index reader treats the same shape. It is not silent
    though: the count rides back to the caller and is reported, because a
    dropped id there has the same consequence as an unreadable file, just
    narrower (guard-1760 — never report what ran without reporting what was
    skipped).
    """
    out, unparsed = set(), 0
    p = agent_dir / ARCHIVE_INDEX
    if not p.is_file():
        return out, unparsed
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                unparsed += 1
                continue
            rid = rec.get("id", "")
            if rid:
                out.add(rid)
    return out, unparsed


def reconcile_agent(agent: str, apply: bool) -> dict:
    agent_dir = _agent_dir(agent)
    jsonl_path = agent_dir / "experience.jsonl"
    exp_dir = agent_dir / "experience"
    defer_log = PROJECT_ROOT / "core" / "logs" / f"experience-reconcile-defer-{agent}.jsonl"

    if not jsonl_path.exists():
        return {"agent": agent, "skipped": "no jsonl"}

    jsonl_records = []
    jsonl_ids = {}
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                jsonl_records.append(r)
                jsonl_ids[r.get("id", "")] = r
            except json.JSONDecodeError:
                continue

    md_files = {}
    for p in exp_dir.rglob("exp-*.md") if exp_dir.exists() else []:
        md_files[p.stem] = p

    goal_index = load_goal_index()
    md_stems = set(md_files.keys())
    # Orphan means "referenced by NEITHER index", not "absent from live".
    # Archival MOVES a row out of experience.jsonl, so subtracting the live
    # index alone reports every aged record as an orphan and --apply would
    # append it back into live — resurrecting archived rows and creating the
    # duplicate id the daemon write path already refuses with 409
    # (; see the ARCHIVE-BLINDNESS note in the module docstring).
    arch_ids, arch_unparsed = archived_ids(agent_dir)
    orphan_md = sorted(md_stems - set(jsonl_ids.keys()) - arch_ids)
    # Reported, not discarded: the population this fix REMOVES from the
    # orphan count is exactly the population a reader of the old output was
    # being misled about, so it must stay visible. A run that excluded
    # thousands of records must not print the same shape as a genuinely
    # clean one.
    archived_indexed = sorted((md_stems - set(jsonl_ids.keys())) & arch_ids)
    # Two missing-md classes:
    #  - missing_md_path: content_path field is set but file does not exist.
    #  - stem_mismatch:   record id does not match any .md stem under
    #                     <agent>/experience/, regardless of content_path
    #                     pointing elsewhere. The  calibration used
    #                     this set-difference test; rb-443's "bidirectional
    #                     integrity" promise is stem-keyed.
    missing_md_path = sorted([
        i for i, r in jsonl_ids.items()
        if r.get("content_path") and not (PROJECT_ROOT / r["content_path"]).exists()
    ])
    stem_mismatch = sorted([
        i for i in jsonl_ids.keys()
        if i and i not in md_stems and i not in missing_md_path
    ])
    missing_md = missing_md_path
    missing_no_path = sorted([
        i for i, r in jsonl_ids.items() if not r.get("content_path")
    ])

    backfilled_jsonl = []
    backfilled_md = []
    deferred = []

    for stem in orphan_md:
        md_path = md_files[stem]
        try:
            text = md_path.read_text(encoding="utf-8")
        except Exception as e:
            deferred.append({"id": stem, "kind": "orphan_md",
                             "reason": f"cannot read .md: {e}"})
            continue
        fm, body = parse_front_matter(text)
        # Resolve goal_id: front matter wins; if absent, parse from filename
        # stem. Many legacy orphans have empty front matter but encode their
        # goal_id in the filename (e.g., exp-.md → ).
        goal_id = fm.get("goal_id", "") or goal_id_from_stem(stem)
        rec_type = fm.get("type", "")
        # Type inference for legacy front-matter shapes that pre-date the
        # type field: hypothesis_id → hypothesis_formation; any resolved
        # goal_id (fm or stem) → goal_execution. The legacy bravo shape
        # (id/goal_id/category/outcome/date/session/agent) lacks `type`
        # but is recoverable because `goal_id` identifies the record class.
        if rec_type not in VALID_TYPES:
            if fm.get("hypothesis_id"):
                rec_type = "hypothesis_formation"
            elif goal_id:
                rec_type = "goal_execution"
        if rec_type not in VALID_TYPES:
            deferred.append({"id": stem, "kind": "orphan_md",
                             "reason": f"type unrecoverable (no type/hypothesis_id/goal_id): fm={fm.get('type', '')!r}",
                             "fm_keys": sorted(fm.keys()),
                             "fm_partial": {k: fm[k] for k in fm
                                            if k in ("type", "category",
                                                     "goal_id", "hypothesis_id",
                                                     "date", "id")},
                             "goal_id_resolved": goal_id or None})
            continue
        # Resolve category: front matter wins; otherwise look up goal_id in
        # the goal index (world + agent aspirations.jsonl). Recovers older
        # alpha "Apply:..." orphans whose front matter omitted category but
        # whose goal_id exists in the world queue with a category attached.
        category = fm.get("category", "")
        if not category and goal_id:
            looked_up = goal_index.get(goal_id)
            if looked_up:
                category = looked_up[1] or category
        if not category and rec_type == "chat_session":
            # A chat_session documents a chat session, not a goal — it carries no
            # goal_id by construction, so the goal_index lookup above can never
            # supply its category. Without this fallback the record defers as
            # "category unrecoverable", i.e. the same permanent-orphan outcome the
            # VALID_TYPES fix just removed, one gate later (measured :
            # 8 of the 9 chat_session orphans carry no category in front matter,
            # so the type fix alone would have rescued exactly 1). The record
            # CLASS is the category here; no domain semantics are invented.
            category = "chat-session"
        if not category:
            deferred.append({"id": stem, "kind": "orphan_md",
                             "reason": f"category unrecoverable (fm-empty, goal_id={goal_id or 'none'} not in any queue or category-empty)",
                             "fm_keys": sorted(fm.keys()),
                             "fm_partial": {k: fm[k] for k in fm
                                            if k in ("type", "category",
                                                     "goal_id", "hypothesis_id",
                                                     "date", "id")},
                             "goal_id_resolved": goal_id or None,
                             "type_resolved": rec_type})
            continue
        summary = extract_summary(body, stem.replace("exp-", "").replace("-", " "))
        rel_path = md_path.relative_to(PROJECT_ROOT).as_posix()
        rec = {
            "id": stem,
            "type": rec_type,
            "category": category,
            "summary": summary[:200],
            "content_path": rel_path,
            "created": stamp_now(),
            "tree_nodes_related": [],
            "verbatim_anchors": [],
            "reasoning_chain": [],
            "retrieval_stats": {},
            "archived": False,
            "archived_date": None,
            "reconciled_by": "experience-reconcile.py",
        }
        if goal_id:
            rec["goal_id"] = goal_id
        if fm.get("hypothesis_id"):
            rec["hypothesis_id"] = fm["hypothesis_id"]
        backfilled_jsonl.append(rec)

    for rec_id in missing_md:
        rec = jsonl_ids[rec_id]
        cp = rec.get("content_path")
        if not cp:
            deferred.append({"id": rec_id, "kind": "missing_md",
                             "reason": "content_path missing in record"})
            continue
        summary = rec.get("summary") or ""
        if len(summary.strip()) < 5:
            deferred.append({"id": rec_id, "kind": "missing_md",
                             "reason": "summary empty or too short to regenerate stub"})
            continue
        target = PROJECT_ROOT / cp
        backfilled_md.append({"id": rec_id, "path": str(target),
                              "rel": cp, "summary": summary[:80]})

    for rec_id in stem_mismatch:
        rec = jsonl_ids[rec_id]
        summary = rec.get("summary") or ""
        if len(summary.strip()) < 5:
            deferred.append({"id": rec_id, "kind": "stem_mismatch",
                             "reason": "summary too short to write stub"})
            continue
        # Canonical stem-matched location. Existing content_path (if set)
        # points to a cross-reference doc and is left untouched — the
        # forwarding note in the new stub names it.
        target = _agent_dir(agent) / "experience" / f"{rec_id}.md"
        backfilled_md.append({"id": rec_id, "path": str(target),
                              "rel": target.relative_to(PROJECT_ROOT).as_posix(),
                              "summary": summary[:80],
                              "kind": "stem_mismatch"})

    for rec_id in missing_no_path:
        rec = jsonl_ids[rec_id]
        deferred.append({"id": rec_id, "kind": "missing_no_path",
                         "reason": "no content_path in record",
                         "summary_len": len(rec.get("summary", ""))})

    if apply:
        # Locked appends — _fileops.locked_append_jsonl wraps each write in
        # acquire_lock/save_history/append_changelog/release_lock so a
        # concurrent agent loop appending to the same experience.jsonl
        # cannot race-corrupt the file (Windows O_APPEND emulation is not
        # atomic for concurrent writers).
        for r in backfilled_jsonl:
            locked_append_jsonl(jsonl_path, r)

        for entry in backfilled_md:
            md = md_for_record(jsonl_ids[entry["id"]])
            target = Path(entry["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(md, encoding="utf-8")

        if deferred:
            defer_log.parent.mkdir(parents=True, exist_ok=True)
            # Dedup against existing defer log: skip entries whose
            # (id, kind, reason) tuple is already present. Re-running --apply
            # twice no longer doubles the log. The `logged_at` field is
            # stamped fresh per write but is not part of the dedup key.
            existing_keys = set()
            if defer_log.exists():
                try:
                    with defer_log.open(encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            existing_keys.add((rec.get("id", ""),
                                               rec.get("kind", ""),
                                               rec.get("reason", "")))
                except OSError:
                    pass
            ts = stamp_now()
            for d in deferred:
                key = (d.get("id", ""), d.get("kind", ""),
                       d.get("reason", ""))
                if key in existing_keys:
                    continue
                d2 = dict(d)
                d2["logged_at"] = ts
                locked_append_jsonl(defer_log, d2)
                existing_keys.add(key)

    return {
        "agent": agent,
        "before": {"orphan_md": len(orphan_md),
                   # Not an orphan and not an error: a trace whose index row
                   # has aged into experience-archive.jsonl. Surfaced so the
                   # exclusion is auditable rather than silent ().
                   "archived_indexed": len(archived_indexed),
                   # Rows the archive guard could not parse. Non-zero means
                   # that many ids are missing from the guard set, each one
                   # a potential duplicate append under --apply.
                   "archive_unparsed": arch_unparsed,
                   "missing_md": len(missing_md),
                   "stem_mismatch": len(stem_mismatch),
                   "missing_no_path": len(missing_no_path)},
        "actions": {
            "jsonl_records_backfilled": len(backfilled_jsonl),
            "md_files_backfilled": len(backfilled_md),
            "deferred": len(deferred),
        },
        "applied": apply,
        "defer_log": str(defer_log) if (apply and deferred) else None,
    }


def main():
    discovered = discover_agents()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="Write changes. Default is dry-run.")
    # choices is set dynamically from local-paths.conf glob, so adding a
    # third agent (or running in a single-agent fresh repo) requires no code
    # change. If discovery returns empty, choices is None and any name is
    # accepted — reconcile_agent itself will exit with `skipped: no jsonl`.
    ap.add_argument("--agent", choices=discovered or None,
                    help="Restrict to one agent. Default: all discovered "
                         f"({', '.join(discovered) if discovered else 'none'}).")
    args = ap.parse_args()

    agents = [args.agent] if args.agent else discovered
    results = []
    for a in agents:
        results.append(reconcile_agent(a, args.apply))

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
