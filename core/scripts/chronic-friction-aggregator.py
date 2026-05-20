#!/usr/bin/env python3
"""Aggregate chronic friction themes per aspiration.

For each active aspiration, scan:
  - All goals' historical defer_reason values (current + archive)
  - All reasoning-bank entries linked to its goals (source_goal match
    or aspiration-id tag)
  - critical_blockers history from team-state.yaml

Cluster source phrases by 3+ word phrase frequency, surface top N
themes, and write them back to aspiration.chronic_friction[]. Origin:
LifingPolls plan item 8 (2026-05-08). Read-only on rb / defer history;
ONLY writes the chronic_friction field on aspirations.

Usage:
  py -3 core/scripts/chronic-friction-aggregator.py [--dry-run]
                                                   [--asp-id ID]
                                                   [--top N]

Default: scan all active aspirations across world + agent queues, write
top 3 themes per aspiration. Idempotent — re-running overwrites with
fresh aggregation.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _paths import WORLD_DIR, AGENT_DIR  # noqa: E402

# Stopwords filtered from theme phrases — too generic to be signal.
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "is", "was", "are", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "this", "that", "these", "those",
    "from", "as", "if", "then", "than", "when", "where", "what", "which",
    "who", "how", "why", "not", "no", "yes", "so", "just", "still", "yet",
    "very", "only", "also", "more", "less", "most", "least", "all", "any",
    "some", "each", "every", "until", "before", "after", "during",
    "while", "since", "because", "due", "i", "me", "my", "we", "our",
    "you", "your", "he", "she", "it", "they", "them", "their",
    "goal", "goals", "asp", "aspirations",  # framework-noise
}

PHRASE_LEN = 3       # bigram→trigram window
MIN_PHRASE_COUNT = 2  # phrases appearing fewer times are noise
TOP_N = 3            # default themes per aspiration


# ---- Source readers --------------------------------------------------------


def _iter_aspiration_files():
    """Yield (path, queue_name) for each aspiration file we care about.

    Returns live + archive for both world and agent queues. The aggregator
    reads all four to capture deferred_reason history that may have been
    archived with completed goals.
    """
    if WORLD_DIR is not None:
        yield (WORLD_DIR / "aspirations.jsonl", "world-live")
        yield (WORLD_DIR / "aspirations-archive.jsonl", "world-archive")
    if AGENT_DIR is not None:
        yield (AGENT_DIR / "aspirations.jsonl", "agent-live")
        yield (AGENT_DIR / "aspirations-archive.jsonl", "agent-archive")


def _read_aspirations(path: Path):
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _collect_defer_phrases(asp_id: str) -> list[tuple[str, str]]:
    """Return [(phrase_text, source_ref), ...] for all defer_reasons
    on goals belonging to asp_id (live + archive, both queues)."""
    out = []
    for path, queue in _iter_aspiration_files():
        for asp in _read_aspirations(path):
            if asp.get("id") != asp_id:
                continue
            for g in asp.get("goals", []):
                dr = g.get("defer_reason")
                if dr and isinstance(dr, str) and dr.strip():
                    out.append((dr, f"defer:{queue}:{g.get('id')}"))
    return out


def _collect_rb_phrases(asp_id: str, goal_ids: set[str]) -> list[tuple[str, str]]:
    """Return [(phrase_text, source_ref), ...] for all rb entries
    matching this aspiration."""
    if WORLD_DIR is None:
        return []
    rb_path = WORLD_DIR / "reasoning-bank.jsonl"
    if not rb_path.exists():
        return []
    out = []
    with open(rb_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") not in ("active", "weakened"):
                continue
            tags = rec.get("tags") or []
            tags_set = {t.lower() for t in tags if isinstance(t, str)}
            source_goal = rec.get("source_goal", "") or ""
            matches = (
                source_goal in goal_ids
                or asp_id.lower() in tags_set
            )
            if not matches:
                continue
            text_parts = []
            if rec.get("title"):
                text_parts.append(rec["title"])
            if rec.get("content"):
                text_parts.append(rec["content"])
            if rec.get("failure_lesson"):
                text_parts.append(rec["failure_lesson"])
            text = ". ".join(text_parts)
            if text.strip():
                out.append((text, f"rb:{rec.get('id')}"))
    return out


def _collect_critical_blocker_phrases(asp_id: str, goal_ids: set[str]) -> list[tuple[str, str]]:
    """Pull phrases from team-state critical_blockers when goal_id matches."""
    if WORLD_DIR is None:
        return []
    ts_path = WORLD_DIR / "team-state.yaml"
    if not ts_path.exists():
        return []
    try:
        import yaml
        with open(ts_path, "r", encoding="utf-8") as f:
            ts = yaml.safe_load(f) or {}
    except Exception:
        return []
    out = []
    for cb in ts.get("critical_blockers", []) or []:
        if cb.get("goal_id") in goal_ids:
            cause = cb.get("cause") or ""
            title = cb.get("title") or ""
            text = f"{title}. {cause}".strip(". ").strip()
            if text:
                out.append((text, f"critical_blocker:{cb.get('goal_id')}"))
    return out


# ---- Phrase extraction -----------------------------------------------------


_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z\-]{2,}")


def _tokenize(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text)
            if w.lower() not in STOPWORDS and len(w) >= 3]


def _ngrams(tokens: list[str], n: int = PHRASE_LEN) -> list[str]:
    """Yield n-gram phrases (joined by space)."""
    if len(tokens) < n:
        return []
    return [" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def _has_consecutive_2gram_overlap(phrase_a: str, phrase_b: str) -> bool:
    """Return True iff phrase_a and phrase_b share at least one consecutive
    2-token subsequence. Tighter than set-intersection 2-of-3 — avoids
    false-positive merges of phrases sharing 2 common tokens at non-adjacent
    positions (e.g. "lock path resolution" vs "shared lock path" share
    {lock, path} as a set but only "lock path" as adjacent pair, which IS
    a real overlap; conversely "build retry timeout" and "retry network
    build" share {retry, build} as a set but no adjacent pair, so they
    are correctly NOT merged).
    """
    tokens_a = phrase_a.split()
    tokens_b = phrase_b.split()
    if len(tokens_a) < 2 or len(tokens_b) < 2:
        return False
    bigrams_b = {(tokens_b[i], tokens_b[i + 1])
                 for i in range(len(tokens_b) - 1)}
    return any((tokens_a[i], tokens_a[i + 1]) in bigrams_b
               for i in range(len(tokens_a) - 1))


def _aggregate_themes(sources: list[tuple[str, str]],
                       top_n: int = TOP_N) -> list[dict]:
    """Cluster source phrases into top-N theme entries.

    Each theme: {theme: str, count: int, last_seen: ISO,
                 sources: [{type, ref}, ...]}
    """
    phrase_counter = Counter()
    phrase_sources = defaultdict(list)
    for text, ref in sources:
        tokens = _tokenize(text)
        for ngram in _ngrams(tokens):
            phrase_counter[ngram] += 1
            phrase_sources[ngram].append(ref)
    if not phrase_counter:
        return []
    # MIN_PHRASE_COUNT is the signal floor: a count=1 phrase is noise, not a
    # theme. Empty themes list when nothing repeats is the correct answer —
    # callers (priority-review, complete-review) interpret empty as "no
    # recurring friction yet."
    candidates = [(p, c) for p, c in phrase_counter.most_common()
                  if c >= MIN_PHRASE_COUNT]
    if not candidates:
        return []
    now = datetime.now().isoformat(timespec="seconds")
    # Sliding-window n-grams over the same source text produce phrases that
    # share an adjacent token pair (e.g. "lock path resolution" + "shared
    # lock path" share "lock path"). Merge each candidate into a survivor
    # iff they share a consecutive 2-gram, accumulating count and refs, so
    # top-N reflects distinct themes rather than sliding-window restatements.
    # Cap at top_n: once we have top_n distinct survivors, later candidates
    # can still merge into existing ones but cannot start new themes.
    selected: list[tuple[str, int, list[str]]] = []
    for phrase, count in candidates:
        merged = False
        for i, (s_phrase, s_count, s_refs) in enumerate(selected):
            if _has_consecutive_2gram_overlap(phrase, s_phrase):
                selected[i] = (s_phrase, s_count + count,
                               s_refs + phrase_sources[phrase])
                merged = True
                break
        if not merged:
            if len(selected) >= top_n:
                # top_n distinct themes already locked in. Skip appending
                # this candidate; later candidates can still merge into
                # the existing survivors.
                continue
            selected.append((phrase, count,
                             list(phrase_sources[phrase])))
    themes = []
    for s_phrase, s_count, s_refs in selected[:top_n]:
        sources_for_theme = []
        for ref in s_refs[:5]:
            kind, _, ident = ref.partition(":")
            sources_for_theme.append({"type": kind, "ref": ref})
        themes.append({
            "theme": s_phrase,
            "count": s_count,
            "last_seen": now,
            "sources": sources_for_theme,
        })
    return themes


# ---- Writeback -------------------------------------------------------------


def _update_aspiration_friction(asp_id: str, themes: list[dict],
                                 dry_run: bool, source: str) -> bool:
    """Write chronic_friction back to the aspiration via aspirations.py."""
    if dry_run:
        print(f"[dry-run] would write chronic_friction on {asp_id}: "
              f"{len(themes)} themes")
        return True
    import subprocess
    cmd = [sys.executable, str(HERE / "aspirations.py"),
           "--source", source, "update-asp-field", asp_id,
           "chronic_friction", json.dumps(themes)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print(f"[chronic-friction] WARN: writeback for {asp_id} failed: "
              f"{proc.stderr}", file=sys.stderr)
        return False
    return True


# ---- Main ------------------------------------------------------------------


def _list_active_aspirations() -> list[tuple[str, str]]:
    """Return [(asp_id, source_queue), ...] for active aspirations.

    Walks world + agent live files only (archive entries are terminal).
    """
    out = []
    if WORLD_DIR is not None:
        for asp in _read_aspirations(WORLD_DIR / "aspirations.jsonl"):
            if asp.get("status") == "active":
                out.append((asp["id"], "world"))
    if AGENT_DIR is not None:
        for asp in _read_aspirations(AGENT_DIR / "aspirations.jsonl"):
            if asp.get("status") == "active":
                out.append((asp["id"], "agent"))
    return out


def _aspiration_goal_ids(asp_id: str) -> set[str]:
    """All goal IDs ever associated with this aspiration (live + archive)."""
    out = set()
    for path, _ in _iter_aspiration_files():
        for asp in _read_aspirations(path):
            if asp.get("id") != asp_id:
                continue
            for g in asp.get("goals", []):
                gid = g.get("id")
                if gid:
                    out.add(gid)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="Print themes per aspiration; don't write")
    p.add_argument("--asp-id", help="Aggregate one specific aspiration")
    p.add_argument("--top", type=int, default=TOP_N,
                   help=f"Top-N themes per aspiration (default {TOP_N})")
    args = p.parse_args()

    if args.asp_id:
        # Find the source queue for this asp_id
        targets = []
        for asp_id, source in _list_active_aspirations():
            if asp_id == args.asp_id:
                targets.append((asp_id, source))
                break
        if not targets:
            print(f"ERROR: aspiration {args.asp_id} not found in active queues",
                  file=sys.stderr)
            return 1
    else:
        targets = _list_active_aspirations()

    if not targets:
        print("[chronic-friction] no active aspirations to aggregate")
        return 0

    total_written = 0
    for asp_id, source in targets:
        goal_ids = _aspiration_goal_ids(asp_id)
        sources: list[tuple[str, str]] = []
        sources += _collect_defer_phrases(asp_id)
        sources += _collect_rb_phrases(asp_id, goal_ids)
        sources += _collect_critical_blocker_phrases(asp_id, goal_ids)

        if not sources:
            # No friction data → write empty list to clear stale themes from
            # prior runs (idempotent semantics).
            themes = []
        else:
            themes = _aggregate_themes(sources, top_n=args.top)

        if args.dry_run or themes:
            print(f"[chronic-friction] {asp_id} ({source}): "
                  f"{len(sources)} source phrases, "
                  f"{len(themes)} themes")
            for t in themes:
                print(f"  - '{t['theme']}' (count={t['count']})")

        if _update_aspiration_friction(asp_id, themes, args.dry_run, source):
            total_written += 1

    print(f"[chronic-friction] processed {total_written}/{len(targets)} aspirations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
