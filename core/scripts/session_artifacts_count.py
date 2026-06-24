#!/usr/bin/env python3
"""Count encoding artifacts created/updated since session_start.

Consumed by `productivity-stop-gate.sh` to compute a true session-total
`encoding_ratio` (artifacts / goals_completed) instead of the stale
gap-counter that masked 102-goal / 1-encoding sessions.

# DO NOT TOUCH — single source of truth for session_start
#
# session_start is read ONLY from working-memory.yaml. It is seeded by
# /start on every session entry (reader / assistant / autonomous paths).
# /boot preserves it across autocompact restart. If this file cannot
# resolve session_start, it exits 2 — the stop-gate then treats total
# artifacts as 0 (starvation-direction default), which is correct for a
# stop-gate (a broken seed should not mask starvation by inventing a
# count-all-history number). Do not add a handoff.yaml or epoch
# fallback here — that was the bug the original implementation shipped
# with; a plausible-looking fabricated cutoff produced encoding_ratio=1.0
# and silently disabled the gate.

Artifact sources (schemas verified 2026-04-21; structural_progress added 2026-05-08):
  - world/reasoning-bank.jsonl      → `created`
  - world/guardrails.jsonl          → `created`
  - world/pattern-signatures.jsonl  → `created`
  - world/pipeline.jsonl            → `formed_date` (hyp_created),
                                      `reflected_date` on stage==resolved
                                      (hyp_resolved)  [double-count by
                                      design: form + resolve are two
                                      distinct learning events]
  - world/knowledge/tree/**/*.md    → `last_updated` front matter
  - <agent>/experience.jsonl        → `created` (raw traces, ~1 per goal;
                                      weighted low in productivity-stop-gate
                                      to buffer good sessions without
                                      masking padded ones — see
                                      aspirations.yaml artifact_weights)
  - world/forged-skills.yaml        → `forged_date` per skill entry (g-251-09)
  - world/tree-maintenance-log.jsonl → llm_reported.<op>.actioned summed across
                                       records with ended_at >= cutoff
                                       (Lane D Magic Wand #1 — credits
                                       structural reorganization that doesn't
                                       write new files)
  - <agent>/working-memory.yaml goals_completed_this_session[] →
                                       count entries with work_class=="framework"
                                       (Lane D Magic Wand #1 — credits
                                       framework goals whose output isn't
                                       captured in any other store)

Output on stdout: one-line JSON. Exit 0 on success, 2 if session_start
is unresolved.
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path

# Unicode stdout on Windows cp1252
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from _paths import AGENT_DIR, WORLD_DIR  # noqa: E402
from _long_path import open_long_path  # noqa: E402  —  / rb-450

import yaml  # required — fail loud if missing


def _iso(val):
    """Normalize a YAML/JSON scalar to a T-separated ISO string for
    lexicographic comparison against session_start.

    Critical: PyYAML parses unquoted `2026-04-21T10:30:00` into a
    datetime object whose `str()` uses a SPACE separator
    ('2026-04-21 10:30:00'), which lex-compares BEFORE
    '2026-04-21T00:00:00'. Forcing isoformat() restores the T.
    """
    if val is None:
        return ""
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    return str(val).strip()


def read_session_start():
    """Return session_start ISO string from WM, or None if unset."""
    from wm import wm_path as _resolve_wm_path  # Phase 1A per-Body WM routing ()
    wm_path = _resolve_wm_path()
    if not wm_path.exists():
        return None
    with open(wm_path, encoding="utf-8") as f:
        wm = yaml.safe_load(f) or {}
    ss = _iso(wm.get("session_start"))
    return ss or None


def count_jsonl(path, field, cutoff, predicate=None):
    """Count JSONL entries where entry[field] >= cutoff.

    Errors (missing file → 0; per-line JSON decode error → skip that line
    and continue; open/IO error → raise). Broad exception swallowing
    would bias the signal downward — let real failures surface.
    """
    if not path.exists():
        return 0
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # Per-line corruption is a real data issue; log but
                # don't count. Surfacing as stderr lets operators see it.
                print(f"[session-artifacts] malformed JSONL line in {path.name}",
                      file=sys.stderr)
                continue
            if predicate and not predicate(rec):
                continue
            val = _iso(rec.get(field))
            if val and val >= cutoff:
                n += 1
    return n


# Cosmetic/maintenance triggers that do not represent an encoding event.
# A `last_update_trigger.type` (or scalar) in this set is skipped even when
# `last_updated >= cutoff` — otherwise a typo fix inflates encoding_ratio and
# silently weakens the stop-gate's starvation signal. Missing/unknown triggers
# still count (backward-compat — today most edits omit the field). Extend
# cautiously: expanding this list in the count-less direction makes the gate
# stricter; never add a trigger here that represents real learning.
_MAINTENANCE_TRIGGERS = frozenset({
    "typo-fix", "typo",
    "formatting", "format", "whitespace",
    "link-update", "link-fix", "broken-link",
    "cosmetic", "cleanup",
    "backfill",
})


def _trigger_type(val):
    """Extract a normalized trigger type from the 98%-dict / 2%-scalar schema.

    Observed shapes in world/knowledge/tree (2026-04-22 sampling):
      dict:   {type: decompose, source: ..., session: 38}   (dominant form)
      scalar: "initial_creation (g-115-106 buildout)"       (early nodes)
      scalar: "asp-245-completion"
      missing
    Normalize to lowercase first token so scalar parentheticals don't mask
    an otherwise matching trigger.
    """
    if isinstance(val, dict):
        t = val.get("type")
        return str(t).strip().lower() if t else ""
    if val is None:
        return ""
    s = str(val).strip().lower()
    # Take token before the first space/paren so 'initial_creation (g-...)' → 'initial_creation'
    for sep in (" ", "("):
        if sep in s:
            s = s.split(sep, 1)[0]
    return s


def count_yaml_entries(path, field, cutoff):
    """Count top-level dict entries under `skills:` (or root) where entry[field] >= cutoff.

    Schema mirrors world/forged-skills.yaml: `skills` maps skill-name → metadata
    dict containing forged_date. Entries land at the same nesting depth as
    JSONL records — one entry per line semantically — so this helper mirrors
    count_jsonl's contract: cutoff filter on the named timestamp field, missing
    file → 0, parse error → log+skip, no broad swallow. (g-251-09)

    Cosmetic note: forged-skills.yaml uses `forged_date` (date-only YYYY-MM-DD)
    not a full timestamp — _iso() handles both date and datetime, so a date-only
    forged_date lex-compares correctly against the session_start ISO string
    (date strings are a strict prefix of datetime strings in ISO-8601 ordering).
    """
    if not path.exists():
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[session-artifacts] cannot parse {path.name}: {e}", file=sys.stderr)
        return 0
    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, dict):
        return 0
    n = 0
    for _name, meta in skills.items():
        if not isinstance(meta, dict):
            continue
        val = _iso(meta.get(field))
        if val and val >= cutoff:
            n += 1
    return n


def count_tree_writes(tree_root, cutoff):
    """Count tree node .md files whose `last_updated` front matter >= cutoff,
    excluding edits whose `last_update_trigger` identifies a maintenance-only
    change (typo-fix, formatting, etc.).

    Skips index files (_-prefix) and files without front matter — the
    convention requires front matter, and a malformed node is a real
    issue worth seeing rather than silently miscounting.
    """
    if not tree_root.exists():
        return 0
    n = 0
    for md in tree_root.rglob("*.md"):
        if md.name.startswith("_"):
            continue
        try:
            #  / rb-450: open_long_path retries with the Win32
            # extended-prefix API (\\\\?\\) on OSError, bypassing the
            # 260-char MAX_PATH limit that silently dropped legitimately-
            # readable .md files at deeply-nested category paths. POSIX
            # is unaffected (the helper is a no-op there).
            f = open_long_path(md)
        except OSError as e:
            # Genuine read failures (orphan tree entries, broken
            # OneDrive placeholders that bash can't read either,
            # permission errors) — log and skip so the gate stays
            # usable; the operator sees the path in stderr and fixes
            # the tree state. Do NOT broaden to `except Exception` —
            # data-format errors (YAML/JSON parse) should still
            # propagate.
            print(f"[session-artifacts] cannot open {md.name}: {e}",
                  file=sys.stderr)
            continue
        with f:
            if f.readline().strip() != "---":
                continue  # no front matter — not a tree node
            fm_lines = []
            for line in f:
                if line.strip() == "---":
                    break
                fm_lines.append(line)
        fm = yaml.safe_load("".join(fm_lines)) or {}
        val = _iso(fm.get("last_updated"))
        if not (val and val >= cutoff):
            continue
        if _trigger_type(fm.get("last_update_trigger")) in _MAINTENANCE_TRIGGERS:
            continue  # cosmetic edit, not an encoding event
        n += 1
    return n


def count_structural_progress(cutoff):
    """Count tree-maintenance ops + framework-class completed goals since cutoff.

    Lane D Magic Wand #1 — see core/config/modes/autonomous.md "Stop-mode does
    work" for the meta-principle. The existing 8 axes (tw/gd/rb/pt/hc/hr/ex/fg)
    measure WRITTEN artifacts. They miss STRUCTURAL work — decomposes, redistributes,
    distills don't write new files; they reorganize existing ones. A session that
    drains 5 decompose candidates moves the codebase forward but scores 0 on every
    write-based axis. The structural axis fixes that blind spot.

    Two signal sources:

    1. world/tree-maintenance-log.jsonl — sum llm_reported.<op>.actioned across
       every record where ended_at >= cutoff. All ops count equally (decompose,
       redistribute, distill, split, sprout, merge, prune, retire). Records
       written by `tree-update.sh --record-maintenance --with-run-record`,
       which fires on every /tree maintain invocation (standard / backlog / stop).

    2. <agent>/working-memory.yaml goals_completed_this_session[] entries with
       work_class == "framework". Captures sessions whose framework work
       didn't trigger a tree-maintenance run (e.g., editing a SKILL.md, fixing
       a script, adding a guardrail through a goal rather than direct write).

    Fail-open at every layer: missing files / unparseable records → contribute 0.
    A broken signal must never block the productivity gate from firing on a
    legitimately-idle session.

    Returns int. Sum without weighting — productivity-stop-gate's artifact_weights
    map applies the per-axis weight (see aspirations.yaml `productivity_gate.
    artifact_weights.structural_progress`).
    """
    total = 0

    # Source 1: tree-maintenance-log.jsonl
    tml = WORLD_DIR / "tree-maintenance-log.jsonl"
    if tml.exists():
        with open(tml, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    print(f"[session-artifacts] malformed JSONL line in {tml.name}",
                          file=sys.stderr)
                    continue
                # Only count records that ended within this session.
                # Use ended_at (when the maintain run completed); fall back to
                # started_at for older records that predate the field.
                ts = _iso(rec.get("ended_at") or rec.get("started_at"))
                if not ts or ts < cutoff:
                    continue
                llm = rec.get("llm_reported") or {}
                if not isinstance(llm, dict):
                    continue
                for op_name, op_data in llm.items():
                    if not isinstance(op_data, dict):
                        continue
                    actioned = op_data.get("actioned")
                    if isinstance(actioned, int):
                        total += actioned

    # Source 2: framework-class goals from working memory.
    # NOTE — no cutoff filter: `goals_completed_this_session` is reset to []
    # by wm.py _do_reset() on consolidation (see SESSION_IDENTITY_FIELDS in
    # wm.py — only `session_start` is preserved across reset). Every entry
    # in the list is therefore implicitly within the current session by
    # construction. Do NOT add a cutoff filter here — there is no per-entry
    # timestamp to filter on, and the wm-reset invariant makes it unnecessary.
    from wm import wm_path as _resolve_wm_path  # Phase 1A per-Body WM routing ()
    wm_path = _resolve_wm_path()
    if wm_path.exists():
        try:
            with open(wm_path, encoding="utf-8") as f:
                wm = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            print(f"[session-artifacts] cannot parse {wm_path.name}: {e}",
                  file=sys.stderr)
            wm = {}
        goals = wm.get("goals_completed_this_session") or []
        if isinstance(goals, list):
            for g in goals:
                if isinstance(g, dict) and g.get("work_class") == "framework":
                    total += 1

    return total


def main():
    cutoff = read_session_start()
    if not cutoff:
        print("[session-artifacts] session_start unset in WM — /start must "
              "seed it. Exiting 2 (stop-gate treats this as 0 artifacts).",
              file=sys.stderr)
        sys.exit(2)

    rb = count_jsonl(WORLD_DIR / "reasoning-bank.jsonl", "created", cutoff)
    guards = count_jsonl(WORLD_DIR / "guardrails.jsonl", "created", cutoff)
    patsigs = count_jsonl(WORLD_DIR / "pattern-signatures.jsonl", "created", cutoff)

    pipe_path = WORLD_DIR / "pipeline.jsonl"
    hyp_created = count_jsonl(pipe_path, "formed_date", cutoff)
    hyp_resolved = count_jsonl(
        pipe_path, "reflected_date", cutoff,
        predicate=lambda r: r.get("stage") == "resolved",
    )

    tree_writes = count_tree_writes(WORLD_DIR / "knowledge" / "tree", cutoff)

    # Experience records live in the agent's private archive, not world/.
    # Counted as baseline learning signal (not double-counting — experience
    # records are the RAW interaction trace, distinct from distilled rb/tree
    # entries they may later spawn).
    experience = count_jsonl(AGENT_DIR / "experience.jsonl", "created", cutoff)

    # : count forged skills (cross-session durable artifact). Counted
    # by `forged_date` field at YAML schema. Weight in
    # productivity-stop-gate.sh artifact_weights (see aspirations.yaml).
    forges = count_yaml_entries(WORLD_DIR / "forged-skills.yaml", "forged_date", cutoff)

    # Lane D Magic Wand #1: structural progress (tree maintain ops + framework goals).
    # See count_structural_progress() docstring above for rationale.
    structural_progress = count_structural_progress(cutoff)

    total = (rb + guards + patsigs + hyp_created + hyp_resolved
             + tree_writes + experience + forges + structural_progress)

    print(json.dumps({
        "rb": rb,
        "hyp_created": hyp_created,
        "hyp_resolved": hyp_resolved,
        "guards": guards,
        "patsigs": patsigs,
        "tree_writes": tree_writes,
        "experience": experience,
        "forges": forges,
        "structural_progress": structural_progress,
        "total": total,
        "session_start": cutoff,
    }))


if __name__ == "__main__":
    main()
