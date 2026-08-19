#!/usr/bin/env python3
"""guardrail_retire.py - D1 LLM-reviewed guardrail cluster retirement engine ().

Implements the D1 design (relevance-aware cluster-resurgence refresh, rb-2338), the
guardrail instantiation of the shared cluster-refresh pattern whose reference
instantiation is tree_archive.py (D2, g-303-29). D1 mirrors D2 Sections 2/3/4 with
a guardrail-specific Section 1.

  - compute_cluster(guard_id): cluster of ACTIVE guardrails sharing ANY of signal
    A (shared category, with a CATEGORY_COHESION_FLOOR so the coarse `category`
    field does not collapse a 100+-member category into one cluster) / B (shared
    source-incident token parsed from the free-text `source`) / C (tags Jaccard >=
    floor). MAX_CLUSTER_SIZE guard: if a candidate's cluster blows past the cap,
    the signals are too loose for it - fall back to B-only (source-incident, the
    tightest signal).
  - scan: emit stale ACTIVE candidates (effective-relevance staleness > threshold)
    + each candidate's cluster + refresh-eligibility (a member retrieved/fired
    within the lookback window => refresh the whole cluster, skip retire) + the
    guard-707 doc-reference pre-gate result.
  - apply <id> <verdict>: keep | refresh | retire | revise. Returns a MUTATION
    PLAN (list of {id, field, value}); the guardrail-retire.sh wrapper executes
    the plan via guardrails-update-field.sh (world/guardrails.jsonl is own-cloud
    append-JSONL per guard-832 - the engine never writes it directly).
      * retire is HARD-refused when the guard is doc-referenced (guard-707).
      * retire is natural-gated dormant (retires_per_pass: 0) unless --force.
      * reversibility: retire is a status flip active->retired, NOT a delete.
  - restore <id>: undo a retire (status retired->active).

INVARIANTS (design Section 5 / verify-before-assuming-applied-to-deletion):
  - ACTIVE ONLY. status != active guardrails are NEVER candidates and NEVER
    cluster members.
  - DEFAULT-TO-KEEP. No relevance signal => not eligible (not retired). The LLM
    review prompt (assembled by the caller) defaults to keep under uncertainty -
    a wrongly-retired guardrail removes a SAFETY check.
  - guard-707 HARD keep. A guard cited by ID in a LIVE framework file
    (CLAUDE.md / .claude/skills / .claude/rules / core/config, EXCLUDING
    .history/) is load-bearing even at evidence=0 and is never retired.
  - SINGLE-WRITER for last_relevant_at + status=retired (guard-155/rb-254): only
    this engine's plan (executed by guardrail-retire.sh via guardrails-update-
    field.sh) writes them.
  - NATURAL-GATED DORMANT (guard-348): `retires_per_pass: 0` in the config block
    means the autonomous lane scans + reviews + applies keep/refresh but applies
    ZERO retire verdicts. Raise it above 0 to activate destructive retirement.
    No `enabled:` boolean - the cap-of-0 IS the gate.

Pure helpers (_jaccard, _parse_date, _parse_source_incident_token, _guard_signals,
effective_relevance, staleness_days, _is_active, _compute_cluster_pure,
_verdict_mutations) import NOTHING from _paths, so the unit tests import this
module without a configured WORLD_DIR. Disk-backed functions (scan/apply/restore/
compute_cluster/doc_referenced) import _paths lazily.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import yaml

# Defaults mirror the d1-cluster-retirement-design.md Section 6 values. The live
# values come from core/config/aspirations.yaml -> guardrail_retirement (loaded by
# _load_config); these are the fallback when the block is absent.
_DEFAULTS = {
    "retire_threshold_days": 180,      # RETIRE_THRESHOLD_DAYS (user-set, conservative)
    "refresh_lookback_days": 60,       # REFRESH_LOOKBACK_DAYS
    "tag_jaccard_floor": 0.5,          # TAG_JACCARD_FLOOR
    "category_cohesion_floor": 40,     # CATEGORY_COHESION_FLOOR
    "max_cluster_size": 30,            # MAX_CLUSTER_SIZE (fallback to B-only)
    "retires_per_pass": 0,             # natural gate: 0 == dormant
    "allowlist": [],                   # class-(b1)+(b3) guard ids: never retire
}

# Framework files/dirs grep'd for the guard-707 doc-reference HARD-keep pre-gate.
# .history/ is EXCLUDED (copy-on-write snapshots inflate match counts ~78x -
# the exact inflation guard-707 names). Paths are repo-root-relative.
_DOC_REF_TARGETS = ("CLAUDE.md", ".claude/skills", ".claude/rules", "core/config")
_HISTORY_EXCLUDE = ".history"


# ---------------------------------------------------------------------------
# Pure helpers - NO _paths import (unit-testable without WORLD_DIR).
# ---------------------------------------------------------------------------

def _jaccard(a, b):
    """Jaccard similarity of two sets. 0.0 when both empty (no shared meaning)."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


def _parse_date(value):
    """ISO date/datetime string -> date, or None. Tolerant of trailing time."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        return None


# Signal-B incident-token patterns (design Section 0 fact 2: `source` is free-text,
# so a STABLE incident token is parsed out - NOT whole-string equality). Order
# matters: the session-<agent>-<date> prefix is checked first, then a leading
# g-NNN-NN / g-xw-<ts>-NN / rb-NNN / asp-NNN id. Free-text source with no parseable
# token contributes NO signal-B edge (returns None - not a wildcard match).
_TOKEN_PATTERNS = (
    re.compile(r"^(session-[a-z0-9]+-\d{4}-\d{2}-\d{2})", re.IGNORECASE),
    # : g- branch admits both g-NNN-NN and the cross-world g-xw-<ts>-NN
    # form so an xw-goal-sourced guardrail still yields a signal-B clustering token.
    re.compile(r"^(g-(?:\d+-\d+|xw-\d{8}T\d{6}-\d{2})|rb-\d+|asp-\d+)", re.IGNORECASE),
)


def _parse_source_incident_token(source):
    """Parse a stable incident token from a guardrail's free-text `source`.

    Returns the `session-<agent>-<date>` prefix OR a leading `g-NNN-NN` /
    `rb-NNN` / `asp-NNN` id (lower-cased), or None when no token is parseable.
    None means "no signal-B edge" (NOT a wildcard) - two guards both with
    unparseable sources do NOT cluster on B."""
    if not source or not isinstance(source, str):
        return None
    s = source.strip()
    for pat in _TOKEN_PATTERNS:
        m = pat.match(s)
        if m:
            return m.group(1).lower()
    return None


def _guard_signals(record):
    """Extract the {category, token, tags} cluster signals from a guardrail record.

    category : top-level `category` (coarse - signal A is cohesion-floored).
    token    : parsed incident token from `source` (signal B), or None.
    tags     : set of `tags` (signal C, Jaccard-floored).
    """
    tags = record.get("tags") or []
    if not isinstance(tags, (list, tuple, set)):
        tags = []
    return {
        "category": record.get("category"),
        "token": _parse_source_incident_token(record.get("source")),
        "tags": set(tags),
    }


def _utilization(record, counters=None):
    """The record's utilization counters — sidecar-aware ().

    `counters` is the caller-supplied sidecar map (id -> counters). It is a
    PARAMETER rather than a load so this module keeps depending on stdlib +
    yaml ALONE at import time; the seam pulls in `_paths`, which resolves
    WORLD_DIR at import. Same reasoning (and same lazy-import shape) as the
    `from _paths import PROJECT_ROOT` already inside `scan`. Default None =>
    read the embedded field, byte-identical to pre-seam behaviour.

    THIS IS THE RETIREMENT ENGINE'S CLOCK, and it fails in the destructive
    direction. Once the writer lands, the embedded counters are a frozen
    pre-split snapshot, so a guardrail retrieved or fired yesterday would read
    as never-touched. That feeds BOTH paths that decide destruction: staleness
    (makes it a retire candidate) and refresh-eligibility (removes the
    cluster's protection). Both fail toward retiring, so a stale read here
    inverts this engine's own DEFAULT-TO-KEEP invariant on exactly the
    guardrails that are most in use.
    """
    if counters:
        # Deferred, and deliberately inside the `counters` branch — see above.
        # Reusing `utilization_of` rather than re-typing its sidecar-wins
        # precedence keeps one implementation of that rule (guard-2676).
        from _utilization_store import utilization_of as _uo
        return _uo(record, counters)
    return record.get("utilization") or {}


def _last_active(record, counters=None):
    """last_active_at lives under `utilization` (stamped when times_active
    increments, once the daemon stamp ships - design Section 2). Absent today;
    read gracefully so the engine works pre-stamp."""
    return _utilization(record, counters).get("last_active_at")


def _last_retrieved(record, counters=None):
    return _utilization(record, counters).get("last_retrieved")


def effective_relevance(record, counters=None):
    """max(last_retrieved, last_active_at, last_relevant_at-or-created).

    last_relevant_at defaults (semantically) to `created` - guardrails have no
    `last_updated`, so `created` is the always-present back-fill (design Section
    2). Returns None only when the record carries NO date signal at all
    (default-to-keep: a guard with no relevance clock is never eligible).
    """
    lr = _parse_date(_last_retrieved(record, counters))
    la = _parse_date(_last_active(record, counters))
    lra = _parse_date(record.get("last_relevant_at")) or _parse_date(record.get("created"))
    candidates = [d for d in (lr, la, lra) if d is not None]
    if not candidates:
        return None
    return max(candidates)


def staleness_days(record, today, counters=None):
    """Days since effective_relevance, or None when no relevance signal."""
    er = effective_relevance(record, counters)
    if er is None:
        return None
    return (today - er).days


def _is_active(record):
    """Only status==active guardrails are candidates / cluster members."""
    return record.get("status") == "active"


def _compute_cluster_pure(guard_id, records,
                          category_cohesion_floor=40,
                          tag_jaccard_floor=0.5,
                          max_cluster_size=30):
    """Cluster(g) = { h active : A(g,h) OR B(g,h) OR C(g,h) }, g in Cluster(g).

    records : dict guard_id -> record (ACTIVE guardrails ONLY; callers pre-filter
              so non-active guards are excluded from membership entirely - Section
              1 edge iv). Returns a set of guard ids. Reflexive.

    Signal A (shared category) is COHESION-FLOORED: it is admitted as a STANDALONE
    edge only when the candidate's category has < category_cohesion_floor active
    members. In a large category, a same-category pair needs ALSO B or C - the
    guardrail analog of D2's MAX_CLUSTER_SIZE guard, applied at the signal level
    because the dominant signal (category) is the coarse one.

    Signal B (source-incident token) and C (tags Jaccard >= floor) are always
    admitted. MAX_CLUSTER_SIZE scope guard: if the A/B/C cluster still exceeds the
    cap, fall back to B-ONLY (source-incident, the tightest signal).
    """
    if guard_id not in records:
        return {guard_id}

    g_sig = _guard_signals(records[guard_id])
    g_cat, g_token, g_tags = g_sig["category"], g_sig["token"], g_sig["tags"]

    # Category cohesion: is signal A admissible as a standalone edge for g?
    cat_sizes = Counter(
        r.get("category") for r in records.values() if r.get("category")
    )
    a_standalone = g_cat is not None and cat_sizes.get(g_cat, 0) < category_cohesion_floor

    def _members(b_only=False):
        out = {guard_id}
        for m_id, m in records.items():
            if m_id == guard_id:
                continue
            m_sig = _guard_signals(m)
            b = g_token is not None and m_sig["token"] == g_token
            if b_only:
                if b:
                    out.add(m_id)
                continue
            a = g_cat is not None and m_sig["category"] == g_cat
            c = bool(g_tags) and bool(m_sig["tags"]) and \
                _jaccard(g_tags, m_sig["tags"]) >= tag_jaccard_floor
            # A standalone only in small (cohesive) categories; in large
            # categories A needs a co-occurring B or C for the same pair.
            edge = b or c or (a and a_standalone)
            if edge:
                out.add(m_id)
        return out

    cluster = _members(b_only=False)
    if len(cluster) > max_cluster_size:
        # Signals too loose for this candidate - fall back to B-only (Section 1).
        cluster = _members(b_only=True)
    return cluster


_VALID_VERDICTS = ("keep", "refresh", "retire", "revise")


def _verdict_mutations(guard_id, verdict, cluster, today_iso, reason=""):
    """PURE map verdict -> {mutations, actions}. No I/O, no gating (the disk-backed
    apply() does the doc-707 + dormancy gating before calling this).

    mutations : list of {id, field, value} the .sh executes via
                guardrails-update-field.sh.
    actions   : non-mutation follow-ups (e.g. revise files an Apply goal) the .sh
                surfaces for the caller to act on.
    """
    if verdict == "keep":
        muts = [{"id": guard_id, "field": "last_relevant_at", "value": today_iso}]
        return muts, []
    if verdict == "refresh":
        # Refresh the WHOLE cluster: last_relevant_at := today for every member.
        muts = [{"id": m, "field": "last_relevant_at", "value": today_iso}
                for m in sorted(cluster)]
        return muts, []
    if verdict == "revise":
        # Still relevant, just stale-worded: stamp relevant + file an Apply goal.
        muts = [{"id": guard_id, "field": "last_relevant_at", "value": today_iso}]
        actions = [{"type": "file_revise_goal", "guard_id": guard_id,
                    "note": reason or "guardrail rule text drifted - update it"}]
        return muts, actions
    # verdict == "retire": status flip active -> retired (reversible, NOT delete).
    muts = [{"id": guard_id, "field": "status", "value": "retired"}]
    return muts, []


# ---------------------------------------------------------------------------
# Config (natural-gated). Lazy _paths import.
# ---------------------------------------------------------------------------

def _load_config():
    """Read core/config/aspirations.yaml -> guardrail_retirement, merged over
    defaults. Fail-open to defaults (dormant) on any error."""
    cfg = dict(_DEFAULTS)
    try:
        from _paths import CONFIG_DIR
        path = Path(CONFIG_DIR) / "aspirations.yaml"
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            block = data.get("guardrail_retirement")
            if isinstance(block, dict):
                cfg.update({k: block[k] for k in block if k in _DEFAULTS})
    except Exception:
        pass
    return cfg


def is_dormant(cfg=None):
    """Natural gate (guard-348): retires_per_pass <= 0 => dormant (no retire
    verdicts auto-applied). Scan/keep/refresh remain available."""
    cfg = cfg or _load_config()
    try:
        return int(cfg.get("retires_per_pass", 0)) <= 0
    except (TypeError, ValueError):
        return True


# ---------------------------------------------------------------------------
# guard-707 doc-reference pre-gate (HARD keep). Disk-backed.
# ---------------------------------------------------------------------------

def _read_doc_corpus(repo_root):
    """Read all live framework files (EXCLUDING .history/) once into
    [(relpath, text)]. Built once per scan so the doc-reference check is
    O(files) not O(files * candidates)."""
    root = Path(repo_root)
    corpus = []
    for target in _DOC_REF_TARGETS:
        p = root / target
        if not p.exists():
            continue
        files = [p] if p.is_file() else [
            f for f in p.rglob("*")
            if f.is_file() and _HISTORY_EXCLUDE not in f.parts
        ]
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except (OSError, UnicodeDecodeError):
                continue
            corpus.append((str(f.relative_to(root)).replace("\\", "/"), text))
    return corpus


def doc_referenced(guard_id, repo_root=None, corpus=None):
    """True if `guard_id` is cited by ID in a LIVE framework file, EXCLUDING
    .history/ (guard-707: copy-on-write snapshots inflate match counts ~78x).

    Returns (referenced: bool, files: list[str]). A doc-referenced guard is
    load-bearing even at evidence=0 (the doc reference IS active use) - HARD keep.
    Stdlib only (no grep subprocess, no daemon) so it is testable against a tmp
    repo. Pass `corpus` (from _read_doc_corpus) to amortize the file reads across
    many candidates; otherwise it builds the corpus for this single check.
    """
    if corpus is None:
        if repo_root is None:
            try:
                from _paths import PROJECT_ROOT
                repo_root = PROJECT_ROOT
            except Exception:
                return (False, [])
        corpus = _read_doc_corpus(repo_root)
    needle = str(guard_id)
    # Word-boundary match (): a raw substring containment check (the
    # `in`-operator on each corpus text) lets a short guard ID (e.g. guard-14)
    # collide with any longer cited ID sharing
    # its prefix (guard-147), permanently shielding low-ID guards (guard-1..99)
    # from retirement even when genuinely dormant. The over-keep direction is
    # SAFE (doc_referenced is a HARD-KEEP gate, so the error never wrong-retires),
    # but unlike the Layer-D coarse-match case (coarse-match-resilience tree node),
    # there is NO downstream forced-analytical-pass to correct it — the over-keep
    # is silent and permanent, so precision matters here. The trailing `(?![0-9])`
    # blocks the guard-14-vs-guard-147 digit collision; the leading
    # `(?<![A-Za-z0-9])` blocks partial-word matches (e.g. xguard-14). `re` is
    # imported at module level; compile once, search each corpus entry.
    pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(needle) + r"(?![0-9])")
    hits = [rel for rel, text in corpus if pat.search(text)]
    return (bool(hits), hits)


# ---------------------------------------------------------------------------
# Disk-backed operations. Lazy _paths import.
# ---------------------------------------------------------------------------

def _guardrails_path():
    from _paths import WORLD_DIR
    return Path(WORLD_DIR) / "guardrails.jsonl"


def _read_guardrails(path=None):
    """Read world/guardrails.jsonl into {id -> record}. READ-ONLY (the engine
    never writes the store directly - guard-832). Returns ALL records keyed by id;
    callers filter to status==active for candidacy/membership."""
    path = path or _guardrails_path()
    records = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = rec.get("id")
                if rid:
                    records[rid] = rec   # last-writer-wins on duplicate ids
    except OSError:
        return {}
    return records


def _active_records(records):
    return {k: v for k, v in records.items() if _is_active(v)}


def compute_cluster(guard_id, records=None, cfg=None):
    """Disk-backed cluster for a live guardrail id (reads world/guardrails.jsonl)."""
    cfg = cfg or _load_config()
    if records is None:
        records = _read_guardrails()
    active = _active_records(records)
    return _compute_cluster_pure(
        guard_id, active,
        category_cohesion_floor=cfg["category_cohesion_floor"],
        tag_jaccard_floor=cfg["tag_jaccard_floor"],
        max_cluster_size=cfg["max_cluster_size"],
    )


def scan(today=None, scope_category=None, cfg=None, records=None, repo_root=None,
         counters=None):
    """Emit stale ACTIVE candidates + clusters + refresh-eligibility + the
    guard-707 doc-grep result. READ-ONLY (writes nothing). Returns a
    JSON-serialisable dict.

    `counters` (g-358-05): the utilization sidecar map. Loaded ONCE here and
    threaded to every relevance read below, so a scan cannot mix sidecar and
    embedded reads across candidates. Pass {} to force embedded reads (tests);
    pass a dict to supply one.
    """
    cfg = cfg or _load_config()
    today = today or date.today()
    if records is None:
        records = _read_guardrails()
    active = _active_records(records)

    # Load the sidecar ONCE per scan. Fail-open to {} => embedded reads, i.e.
    # exactly today's behaviour — same shape as the repo_root resolution below.
    # Fail-open is the correct direction here and is worth stating: an empty
    # map degrades to the pre-split read, whereas raising would take down a
    # READ-ONLY scan over a counter store that is advisory by design.
    if counters is None:
        try:
            from _utilization_store import load_counters as _load_counters
            counters = _load_counters("guardrails")
        except Exception:
            counters = {}

    threshold = cfg["retire_threshold_days"]
    lookback = cfg["refresh_lookback_days"]
    allowlist = set(cfg.get("allowlist") or [])

    # Build the guard-707 doc-reference corpus ONCE so the per-candidate check is
    # O(files) not O(files * candidates). Resolve repo_root lazily; on failure the
    # corpus is empty and doc_referenced returns (False, []) for every candidate -
    # the same fail-open behavior as the per-candidate path it replaces.
    if repo_root is None:
        try:
            from _paths import PROJECT_ROOT
            repo_root = PROJECT_ROOT
        except Exception:
            repo_root = None
    doc_corpus = _read_doc_corpus(repo_root) if repo_root is not None else []

    candidates = []
    for gid, rec in active.items():
        if scope_category and rec.get("category") != scope_category:
            continue
        sd = staleness_days(rec, today, counters)
        if sd is None or sd <= threshold:
            continue
        cluster = _compute_cluster_pure(
            gid, active,
            category_cohesion_floor=cfg["category_cohesion_floor"],
            tag_jaccard_floor=cfg["tag_jaccard_floor"],
            max_cluster_size=cfg["max_cluster_size"],
        )
        # Refresh-eligible: any cluster member retrieved OR fired within lookback.
        refresh_eligible = False
        for m in cluster:
            mr = active.get(m, {})
            for d in (_parse_date(_last_retrieved(mr, counters)),
                      _parse_date(_last_active(mr, counters))):
                if d is not None and (today - d).days <= lookback:
                    refresh_eligible = True
                    break
            if refresh_eligible:
                break
        ref, ref_files = doc_referenced(gid, corpus=doc_corpus)
        candidates.append({
            "id": gid,
            "rule": str(rec.get("rule", ""))[:160],
            "category": rec.get("category"),
            "source": rec.get("source"),
            "created": rec.get("created"),
            "last_retrieved": _last_retrieved(rec, counters),
            "last_active_at": _last_active(rec, counters),
            "last_relevant_at": rec.get("last_relevant_at"),
            "times_active": _utilization(rec, counters).get("times_active", 0),
            "staleness_days": sd,
            "cluster": sorted(cluster),
            "cluster_size": len(cluster),
            "refresh_eligible": refresh_eligible,
            "doc_referenced": ref,           # guard-707 HARD-keep pre-gate result
            "doc_reference_files": ref_files,
            "allowlisted": gid in allowlist,  # class-(b1)/(b3) HARD keep
        })

    candidates.sort(key=lambda c: c["staleness_days"], reverse=True)
    return {
        "today": today.isoformat(),
        "config": cfg,
        "dormant": is_dormant(cfg),
        "active_count": len(active),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def apply(guard_id, verdict, today=None, cfg=None, force=False, reason="",
          records=None, repo_root=None):
    """Compute the per-candidate verdict MUTATION PLAN. Returns a dict; the
    guardrail-retire.sh wrapper executes `mutations` via guardrails-update-field.sh
    (the engine never writes world/guardrails.jsonl directly - guard-832).

    Gating performed here (disk-backed) BEFORE emitting a retire plan:
      - guard-707 doc-reference HARD keep: a doc-referenced guard is NEVER retired.
      - natural-gate dormancy (retires_per_pass: 0) unless --force.
      - allowlist (class-(b1)/(b3)): NEVER retired.
    """
    if verdict not in _VALID_VERDICTS:
        raise ValueError("invalid verdict: %r (expected one of %s)"
                         % (verdict, ", ".join(_VALID_VERDICTS)))
    cfg = cfg or _load_config()
    today = today or date.today()
    today_iso = today.isoformat()
    if records is None:
        records = _read_guardrails()

    rec = records.get(guard_id)
    if rec is None:
        return {"ok": False, "id": guard_id, "verdict": verdict,
                "error": "guard_not_found"}

    if verdict == "retire":
        if rec.get("status") == "retired":
            return {"ok": False, "id": guard_id, "verdict": verdict,
                    "error": "already_retired"}
        # guard-707 HARD keep: doc-referenced guards are never retired.
        ref, ref_files = doc_referenced(guard_id, repo_root=repo_root)
        if ref:
            return {"ok": False, "id": guard_id, "verdict": verdict,
                    "error": "doc_referenced_hard_keep",
                    "detail": "cited in live framework file(s): %s (guard-707)"
                              % ", ".join(ref_files[:5]),
                    "doc_reference_files": ref_files}
        # allowlist (class-(b1) rare-but-critical / (b3) constitutional).
        if guard_id in set(cfg.get("allowlist") or []):
            return {"ok": False, "id": guard_id, "verdict": verdict,
                    "error": "allowlisted",
                    "detail": "class-(b1)/(b3) allowlist - never auto-retire"}
        # natural gate: dormant unless forced (manual/test) or cap raised.
        if is_dormant(cfg) and not force:
            return {"ok": False, "id": guard_id, "verdict": verdict,
                    "error": "dormant",
                    "detail": "guardrail_retirement.retires_per_pass=0 "
                              "(natural-gated dormant); pass --force or raise the "
                              "cap to retire"}

    cluster = sorted(compute_cluster(guard_id, records=records, cfg=cfg)) \
        if verdict == "refresh" else [guard_id]
    mutations, actions = _verdict_mutations(
        guard_id, verdict, cluster, today_iso, reason=reason)
    return {"ok": True, "id": guard_id, "verdict": verdict,
            "mutations": mutations, "actions": actions,
            "cluster": cluster if verdict == "refresh" else None}


def restore(guard_id, records=None):
    """Compute the mutation plan to undo a retire (status retired -> active)."""
    if records is None:
        records = _read_guardrails()
    rec = records.get(guard_id)
    if rec is None:
        return {"ok": False, "id": guard_id, "error": "guard_not_found"}
    if rec.get("status") != "retired":
        return {"ok": False, "id": guard_id, "error": "not_retired",
                "detail": "status=%s (restore only un-retires)" % rec.get("status")}
    return {"ok": True, "id": guard_id,
            "mutations": [{"id": guard_id, "field": "status", "value": "active"}]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="D1 guardrail cluster retirement engine (g-303-31)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="emit stale active candidates + clusters")
    p_scan.add_argument("--scope-category", help="limit the scan to one category")
    p_scan.add_argument("--today", help="override today (ISO date) for testing")

    p_apply = sub.add_parser("apply", help="emit a per-candidate verdict mutation plan")
    p_apply.add_argument("id")
    p_apply.add_argument("verdict", choices=_VALID_VERDICTS)
    p_apply.add_argument("--reason", default="")
    p_apply.add_argument("--force", action="store_true",
                         help="bypass the natural-gate dormancy (manual/test retire)")
    p_apply.add_argument("--today", help="override today (ISO date) for testing")

    p_restore = sub.add_parser("restore", help="emit the un-retire mutation plan")
    p_restore.add_argument("id")

    p_cluster = sub.add_parser("cluster", help="print the cluster of a guard id")
    p_cluster.add_argument("id")

    args = parser.parse_args(argv)
    today = _parse_date(getattr(args, "today", None))

    if args.cmd == "scan":
        out = scan(today=today, scope_category=args.scope_category)
        print(json.dumps(out, indent=2))
        return 0
    if args.cmd == "apply":
        out = apply(args.id, args.verdict, today=today,
                    force=args.force, reason=args.reason)
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1
    if args.cmd == "restore":
        out = restore(args.id)
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1
    if args.cmd == "cluster":
        out = {"id": args.id, "cluster": sorted(compute_cluster(args.id))}
        print(json.dumps(out, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    try:
        from _stdio import reconfigure_stdio
        reconfigure_stdio()
    except Exception:
        pass
    sys.exit(main())
