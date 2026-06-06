"""Goal-duplication gate logic — daemon-safe extraction (PR 7a/4).

Hard checks BEFORE filing a new goal. Catches the g-115-141 class: a new
goal whose scope overlaps with peer work that is (a) in team-state
recent_completions, (b) the subject of a partner's in_flight claim, (c)
visible in 48h git commits, (d) the subject of an active insight_trigger,
(e) already implemented in the target file, or (f) already pending /
in-progress in the world or any agent queue (g-115-783; closes the
missing-CORPUS gap surfaced by the 4-way l1-skew dup cluster
g-115-743/776/778/779). See the CLI wrapper docstring for the full
failure-mode catalog and rb-NNN crosslinks.

Public API:
    evaluate(goal, *, override_duplication, agent_name, world_dir,
             project_root) -> dict

Return shape (matches the legacy CLI's `result` dict byte-for-byte):
    {
      "would_block": bool,
      "checks": [{"name":..., "passed":..., "reason":..., "matches":[...]}, ...],
      "failing_count": int,
      "self_agent": str|None,
      "file_paths_detected": [...],
      "expected_coverage_paths": [...],
      "override_applied": str|None,
      "reason": str,
      "description_quality_warning"?: True,    # optional informational tag
      "description_quality_reason"?: str,
      "override_logged_to"?: str,              # only when override + log path
    }

Side effects (both happen inside evaluate()):
    1. When override_duplication is set AND any checks failed: append to
       <world_dir>/goal-duplication-overrides.jsonl via locked_append_jsonl.
       Fail-silent — log failure surfaces on stderr but never propagates.
    2. Always: emit one _gate_log() telemetry record.

Daemon safety:
    - Reads no env directly. world_dir / agent_name / project_root are
      explicit args.
    - `git log` subprocess invoked with cwd=project_root (no env reads).
    - `_resolve_search_roots(agent_name=...)` called explicitly so the
      legacy fallback to `os.environ.get("MIND_AGENT")` is bypassed.
    - PROJECT_ROOT computed once at import as a fallback for the rare
      caller that passes project_root=None (matches legacy default).

N-agent invariant — DO NOT enumerate peers anywhere in this module. The
single identity needed is `agent_name`; every check filters non-self
sources via `!= agent_name`. Adding partner enumeration would break the
N>=1 world contract (see g-115-633 / rb-846).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

import yaml  # type: ignore

from _fileops import locked_append_jsonl  # type: ignore
from _gate_log import log as _gate_log  # type: ignore
from _target_state import (  # type: ignore
    _FILE_PATH_RE,
    _resolve_search_roots,
    extract_and_infer_targets,
    is_read_intent,
    probe_target_state,
)
from gates.origin_signal import ALLOWED_PREFIXES  # type: ignore


# Default PROJECT_ROOT for callers that pass project_root=None.
# __file__ = core/scripts/gates/goal_duplication.py → 4 .parents up = repo root.
_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[3]


# Stopwords for keyword extraction — common English + generic dev vocabulary.
# Kept narrow; too aggressive pruning creates false negatives on legitimate
# technical overlap (e.g. "cache" is a real content keyword, not noise).
_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "onto", "this", "that", "then",
    "make", "build", "wire", "pass", "fail", "test", "code", "file", "script",
    "when", "where", "what", "should", "would", "could", "goal", "goals",
    "aspiration", "goal-id", "source", "status", "participant", "participants",
}


# Response-prefix set. A goal whose origin_signal starts with one of these IS
# a response to a board signal, not de-novo work. The expected-coverage helper
# only inspects findings when the prefix matches — guards against ALL goals
# getting expected_paths populated, which would weaken the file_path overlap
# check globally.
_RESPONSE_ORIGIN_PREFIXES = (
    "investigate:",
    "idea:",
    "maintain:",
    "unblock:",
)


# Bare-tag origin_signals (the no-colon entries in
# origin_signal.ALLOWED_PREFIXES, currently "user_directive" and
# "idle_fallback") are generic standalone CATEGORIES, not unique symptom
# keys — see _check_pending_queue Strategy 1 for why an exact-match block on
# them is a false positive. Derived from the SSOT so the two gates never drift.
_GENERIC_BARE_ORIGINS = frozenset(
    t for t in ALLOWED_PREFIXES if not t.endswith(":"))


# --- Signal extraction -------------------------------------------------------

def _verification_text(goal: dict) -> str:
    """Gather verification.outcomes + verification.checks into one text blob.
    g-248-12: verification is the authoritative declaration of what the goal
    will touch; description prose may DISCUSS files without MODIFYING them,
    producing false overlaps.
    """
    v = goal.get("verification") or {}
    chunks = []
    for key in ("outcomes", "checks"):
        val = v.get(key)
        if isinstance(val, list):
            chunks.extend(str(x) for x in val if x)
    return " ".join(chunks)


def _extract_signals(goal: dict):
    """Extract file-paths and keyword-stems from the goal's most authoritative
    text source. Returns (file_paths, keywords, source_name).

    g-248-12 identifier-source ordering:
      1. verification.outcomes + verification.checks — structured, authoritative
      2. title + description — prose fallback when verification is absent

    File paths found first, then removed from the text before keyword
    extraction so a path like retrieve.py doesn't double-count as keyword
    "retrieve".
    """
    ver_text = _verification_text(goal)
    if ver_text.strip():
        text = ver_text
        source_name = "verification"
    else:
        text = (goal.get("title") or "") + " " + (goal.get("description") or "")
        source_name = "prose"
    file_paths = set(_FILE_PATH_RE.findall(text))

    cleaned = text
    for fp in file_paths:
        cleaned = cleaned.replace(fp, " ")
    words = re.findall(r"[a-zA-Z][\w-]{4,}", cleaned.lower())
    keywords = {w for w in words if w not in _STOPWORDS}
    return file_paths, keywords, source_name


def _count_non_stopword_tokens(text: str) -> int:
    """Count non-stopword tokens. : shared with the
    description_quality_warning probe so the "description short" signal
    aligns with the gate's existing keyword semantics."""
    if not text:
        return 0
    words = re.findall(r"[a-zA-Z][\w-]{4,}", text.lower())
    return sum(1 for w in words if w not in _STOPWORDS)


# --- Check 1: recent_completions --------------------------------------------

def _compute_idf(entries, terms):
    """Return {term: idf_weight} using recent_completions key_findings as the
    corpus. g-248-12: rare identifiers contribute high weight; common ones
    contribute near-zero. Returns {t: 1.0} fail-open when corpus is empty.
    """
    findings = []
    for e in entries:
        if isinstance(e, dict):
            kf = (e.get("key_finding") or "").lower()
            if kf:
                findings.append(kf)
    n = len(findings)
    if n == 0:
        return {t: 1.0 for t in terms}
    out = {}
    for t in terms:
        tl = t.lower()
        df = sum(1 for kf in findings if tl in kf)
        out[t] = math.log(n / (1 + df)) if df < n else 0.0
    return out


def _is_expected_path(fp: str, expected_paths) -> bool:
    """: fuzzy match between proposed file_path and expected-coverage
    paths. Mirrors the substring fuzziness in _check_insight_triggers so a
    basename like `iteration-close.sh` matches a fully-qualified expected path
    `core/scripts/iteration-close.sh`.
    """
    if not expected_paths:
        return False
    fp_l = fp.lower()
    if fp_l in expected_paths:
        return True
    for ep in expected_paths:
        if fp_l == ep or fp_l in ep or ep in fp_l:
            return True
    return False


def _check_recent_completions(goal, file_paths, keywords, self_agent,
                              source_name, world_dir, expected_paths=None):
    """N-agent correct: filters `completed_by != self_agent`. Scales to any
    N>=1 without config change. DO NOT add a `partner` param or peer-list
    lookup."""
    if world_dir is None:
        return {
            "name": "recent_completions",
            "passed": True,
            "reason": "skipped (no WORLD_PATH — cannot resolve team-state.yaml)",
            "matches": [],
        }
    ts_path = world_dir / "team-state.yaml"
    try:
        with open(ts_path, "r", encoding="utf-8") as f:
            state = yaml.safe_load(f) or {}
        entries = state.get("recent_completions") or []
    except Exception as e:
        return {
            "name": "recent_completions",
            "passed": True,
            "reason": "skipped (read error: " + str(e) + ")",
            "matches": [],
        }

    if not isinstance(entries, list):
        entries = []

    all_terms = set(file_paths) | set(keywords)
    idf = _compute_idf(entries, all_terms) if all_terms else {}

    WEIGHT_THRESHOLD = 1.5
    MIN_UNIQUE_HITS = 2

    expected = expected_paths or set()

    matches = []
    advisories = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        completed_by = entry.get("completed_by")
        if not completed_by or completed_by == self_agent:
            continue
        key_finding = (entry.get("key_finding") or "").lower()
        goal_id = entry.get("goal_id") or ""
        # sorted() — file_paths / keywords are sets; set iteration order
        # changes between processes (Python hash randomization), so CLI ↔
        # module equivalence depends on stable ordering here.
        hit_paths = sorted(fp for fp in file_paths
                           if fp.lower() in key_finding and not _is_expected_path(fp, expected))
        hit_kws = sorted(kw for kw in keywords if kw in key_finding)
        unique_hits = len(hit_paths) + len(hit_kws)
        weighted = sum(idf.get(fp, 1.0) for fp in hit_paths) + \
                   sum(idf.get(kw, 1.0) for kw in hit_kws)
        strong = unique_hits >= MIN_UNIQUE_HITS and weighted >= WEIGHT_THRESHOLD
        # HARD block requires a STRUCTURAL co-signal: a shared file-path, or
        # a hit keyword carrying a hyphen/underscore/digit (a structured
        # identifier - rb-335, goal_selector). Plain words ("summary",
        # "multiplier") are topical noise, not duplicate-work evidence.
        # DO NOT relax this by raising WEIGHT_THRESHOLD or trusting the IDF
        # sum alone: IDF over the ~50-entry recent_completions corpus cannot
        # separate generic vocab from rare ids (2026-05-16: /
        # vs recurring , weighted 5.3/6.4 on PLAIN words). Token
        # shape is the discriminator. Preserves  IDF intent + the
        # rare-identifier path (gate test CASE 3).
        has_specific = bool(hit_paths) or any(
            re.search(r"[-_0-9]", k) for k in hit_kws)
        if strong and has_specific:
            matches.append({
                "goal_id": goal_id,
                "completed_by": completed_by,
                "completed_at": entry.get("completed_at"),
                "file_path_hits": hit_paths,
                "keyword_hits": hit_kws[:5],
                "weighted_score": round(weighted, 2),
                "unique_hits": unique_hits,
                "key_finding_excerpt": (entry.get("key_finding") or "")[:150],
            })
        elif strong:
            # strong overlap on plain words only -> advisory, never a block
            # (git_log_48h / target_state / partner_in_flight still catch
            # real file/identifier dups independently).
            advisories.append({
                "goal_id": goal_id,
                "completed_by": completed_by,
                "unique_hits": unique_hits,
                "weighted_score": round(weighted, 2),
                "keyword_hits": hit_kws[:5],
                "strong_keyword_only": True,
            })
        elif unique_hits >= 1:
            advisories.append({
                "goal_id": goal_id,
                "completed_by": completed_by,
                "unique_hits": unique_hits,
                "weighted_score": round(weighted, 2),
            })

    # Surface demoted strong-keyword-only overlaps first so the [:5] slice
    # never hides them behind weak advisories (observability for review).
    advisories.sort(key=lambda a: (not a.get("strong_keyword_only"),
                                    -a["weighted_score"]))
    strong_only = sum(1 for a in advisories if a.get("strong_keyword_only"))
    if matches:
        return {
            "name": "recent_completions",
            "passed": False,
            "reason": "overlap with " + str(len(matches)) +
                      " recent non-self completion(s) [weighted>=" +
                      str(WEIGHT_THRESHOLD) + ", N>=" + str(MIN_UNIQUE_HITS) +
                      ", structural_co_signal_required, source=" + source_name + "]",
            "matches": matches,
            "advisories": advisories[:5],
        }
    return {
        "name": "recent_completions",
        "passed": True,
        "reason": ("no blocking overlap (source=" + source_name +
                   ", " + str(len(advisories)) + " sub-threshold advisories"
                   + (", " + str(strong_only) +
                      " strong keyword-only demoted (no file-path/identifier co-signal)"
                      if strong_only else "") + ")"),
        "matches": [],
        "advisories": advisories[:5],
    }


# --- Check 2: partner_in_flight ---------------------------------------------

def _check_partner_in_flight(goal, file_paths, keywords, self_agent,
                             source_name, world_dir):
    """Detect when a non-self agent is currently in_flight on a goal whose
    scope overlaps the proposed goal's scope (rb-846 / g-248-85). N-agent
    correct: iterates every agent_status entry where key != self_agent."""
    if world_dir is None:
        return {
            "name": "partner_in_flight",
            "passed": True,
            "reason": "skipped (no WORLD_PATH — cannot resolve team-state.yaml)",
            "matches": [],
        }
    ts_path = world_dir / "team-state.yaml"
    try:
        with open(ts_path, "r", encoding="utf-8") as f:
            state = yaml.safe_load(f) or {}
        agent_status = state.get("agent_status") or {}
    except Exception as e:
        return {
            "name": "partner_in_flight",
            "passed": True,
            "reason": "skipped (read error: " + str(e) + ")",
            "matches": [],
        }

    if not isinstance(agent_status, dict):
        agent_status = {}

    partner_inflights = []
    for agent_name, status in agent_status.items():
        if not isinstance(status, dict):
            continue
        if agent_name == self_agent:
            continue
        inflight = status.get("in_flight")
        if not isinstance(inflight, dict):
            continue
        title = inflight.get("title") or ""
        goal_id = inflight.get("goal_id") or ""
        if not title and not goal_id:
            continue
        partner_inflights.append({
            "agent": agent_name,
            "goal_id": goal_id,
            "title": title,
            "phase": inflight.get("phase"),
            "claimed_at": inflight.get("claimed_at"),
        })

    if not partner_inflights:
        return {
            "name": "partner_in_flight",
            "passed": True,
            "reason": "no partners in_flight",
            "matches": [],
        }

    MIN_UNIQUE_HITS = 2

    proposed_id = goal.get("id") or ""
    matches = []
    advisories = []
    for pi in partner_inflights:
        if pi["goal_id"] and pi["goal_id"] == proposed_id:
            continue
        text = (pi["title"] or "").lower()
        # sorted() — see _check_recent_completions for rationale.
        hit_paths = sorted(fp for fp in file_paths if fp.lower() in text)
        hit_kws = sorted(kw for kw in keywords if kw in text)
        unique_hits = len(hit_paths) + len(hit_kws)
        if unique_hits >= MIN_UNIQUE_HITS:
            matches.append({
                "agent": pi["agent"],
                "goal_id": pi["goal_id"],
                "title": pi["title"][:120],
                "phase": pi["phase"],
                "claimed_at": pi["claimed_at"],
                "file_path_hits": hit_paths,
                "keyword_hits": hit_kws[:5],
                "unique_hits": unique_hits,
            })
        elif unique_hits >= 1:
            advisories.append({
                "agent": pi["agent"],
                "goal_id": pi["goal_id"],
                "unique_hits": unique_hits,
            })

    if matches:
        return {
            "name": "partner_in_flight",
            "passed": False,
            "reason": ("overlap with " + str(len(matches)) +
                       " partner in_flight goal(s) [N>=" +
                       str(MIN_UNIQUE_HITS) + ", source=" + source_name + "]"),
            "matches": matches,
            "advisories": advisories[:5],
        }
    return {
        "name": "partner_in_flight",
        "passed": True,
        "reason": ("no blocking overlap (source=" + source_name +
                   ", " + str(len(advisories)) + " sub-threshold advisories)"),
        "matches": [],
        "advisories": advisories[:5],
    }


# --- Check 3: git_log_48h ----------------------------------------------------

def _check_git_log(goal, file_paths, project_root):
    """Intersect proposed file-paths against all 48h git commits (any author).

    N-AGENT INVARIANT — DO NOT ADD AUTHOR FILTERING. Scanning all commits
    is correct: it catches overlap with every concurrent contributor AND
    catches self-recent-work.
    """
    if not file_paths:
        return {
            "name": "git_log_48h",
            "passed": True,
            "reason": "skipped (no file paths in goal text)",
            "matches": [],
        }
    try:
        out = subprocess.run(
            # git approxidate rejects the bare unit-letter form "48h" (returns
            # 0 commits, silently disabling this check); use "N.units.ago".
            # 6 — this check was dead since inception (0/15155 firings).
            ["git", "log", "--since=48.hours.ago",
             "--name-only", "--pretty=format:COMMIT %H %s"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            cwd=str(project_root),
        )
    except Exception as e:
        return {
            "name": "git_log_48h",
            "passed": True,
            "reason": "skipped (git error: " + str(e) + ")",
            "matches": [],
        }
    if out.returncode != 0:
        return {
            "name": "git_log_48h",
            "passed": True,
            "reason": "skipped (git exited " + str(out.returncode) + ")",
            "matches": [],
        }

    lines = (out.stdout or "").splitlines()
    raw_matches = []
    current = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("COMMIT "):
            current = line[len("COMMIT "):][:80]
            continue
        # sorted() — set iteration is per-process; first-match
        # `goal_file_pattern` must be deterministic.
        for fp in sorted(file_paths):
            # Specificity floor (6): a bare basename (no "/") is too
            # generic to confirm same-file against the large 48h commit corpus
            # (dominated by frequently-churned state files), so it would
            # over-fire once the date filter above was un-broken. Require an
            # exact match for bare names; only qualified paths may
            # substring-match either direction.
            if fp == line or ("/" in fp and (fp in line or line in fp)):
                raw_matches.append({
                    "commit": current,
                    "file": line,
                    "goal_file_pattern": fp,
                })

    seen = set()
    uniq = []
    for m in raw_matches:
        key = (m["commit"], m["file"])
        if key not in seen:
            seen.add(key)
            uniq.append(m)

    if uniq:
        return {
            "name": "git_log_48h",
            "passed": False,
            "reason": "file-path overlap with " + str(len(uniq)) + " recent commit touch(es) in 48h",
            "matches": uniq[:10],
        }
    return {
        "name": "git_log_48h",
        "passed": True,
        "reason": "no file-path overlap with recent 48h commits",
        "matches": [],
    }


# --- Check 4: insight_triggers + expected_coverage helpers ------------------

def _check_insight_triggers(goal, file_paths, self_agent, world_dir,
                            expected_paths=None):
    """Flag when proposed files are the subject of an active insight_trigger
    posted by a non-self agent. g-115-289: expected_paths exempts response-
    coverage subset."""
    if not file_paths:
        return {
            "name": "insight_triggers",
            "passed": True,
            "reason": "skipped (no file paths)",
            "matches": [],
        }
    if world_dir is None:
        return {
            "name": "insight_triggers",
            "passed": True,
            "reason": "skipped (no WORLD_PATH)",
            "matches": [],
        }
    findings_path = world_dir / "board" / "findings.jsonl"
    if not findings_path.exists():
        return {
            "name": "insight_triggers",
            "passed": True,
            "reason": "skipped (no findings.jsonl)",
            "matches": [],
        }
    cutoff = dt.datetime.now() - dt.timedelta(hours=48)
    entries = []
    try:
        with open(findings_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("timestamp") or rec.get("posted_at") or rec.get("created")
                if ts:
                    try:
                        rec_time = dt.datetime.fromisoformat(ts.replace("Z", ""))
                        if rec_time < cutoff:
                            continue
                    except Exception:
                        pass
                entries.append(rec)
    except Exception as e:
        return {
            "name": "insight_triggers",
            "passed": True,
            "reason": "skipped (read error: " + str(e) + ")",
            "matches": [],
        }

    expected = expected_paths or set()

    matches = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("author") == self_agent:
            continue
        tags = entry.get("tags") or []
        if "insight_trigger" not in tags:
            continue
        affected = []
        severity = "unknown"
        for t in tags:
            if not isinstance(t, str):
                continue
            if t.startswith("affects:"):
                affected.append(t.split(":", 1)[1])
            elif t.startswith("severity:"):
                severity = t.split(":", 1)[1]
        # sorted(file_paths) — set iteration is per-process; stable order
        # matters for CLI ↔ module equivalence.
        hits = []
        for fp in sorted(file_paths):
            if _is_expected_path(fp, expected):
                continue
            for af in affected:
                if fp == af or fp in af or af in fp:
                    hits.append(fp)
                    break
        if hits:
            matches.append({
                "finding_id": entry.get("id"),
                "severity": severity,
                "affects": affected[:5],
                "overlap_files": hits,
            })

    if matches:
        return {
            "name": "insight_triggers",
            "passed": False,
            "reason": str(len(matches)) + " active insight_trigger(s) affecting proposed files",
            "matches": matches,
        }
    return {
        "name": "insight_triggers",
        "passed": True,
        "reason": "no active insight_triggers affecting proposed files",
        "matches": [],
    }


def _scan_affects_tags(jsonl_path: Path, type_filter, required_tag,
                       self_agent: str, cutoff):
    """Scan a board JSONL file for non-self posts within `cutoff` whose tags
    contain `affects:<path>`. Returns lowercased set of paths. Fail-open
    (any error → empty contribution)."""
    if not jsonl_path.exists():
        return set()
    expected = set()
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if type_filter and rec.get("type") != type_filter:
                    continue
                ts = rec.get("timestamp") or rec.get("posted_at") or rec.get("created")
                if ts:
                    try:
                        rec_time = dt.datetime.fromisoformat(ts.replace("Z", ""))
                        if rec_time < cutoff:
                            continue
                    except Exception:
                        pass
                if rec.get("author") == self_agent:
                    continue
                tags = rec.get("tags") or []
                if required_tag and required_tag not in tags:
                    continue
                for t in tags:
                    if isinstance(t, str) and t.startswith("affects:"):
                        path = t.split(":", 1)[1]
                        if path:
                            expected.add(path.lower())
    except Exception:
        return set()
    return expected


def _expected_coverage_paths(goal: dict, self_agent: str,
                             world_dir: Optional[Path]):
    """rb-591: when a goal IS the response to a cross-agent signal, the
    file_paths in its description ARE the cited paths. Treating that as
    duplication is structural false-positive. Skipping the file_path overlap
    for those specific paths preserves the keyword-based overlap signal."""
    origin = goal.get("origin_signal") or ""
    if not isinstance(origin, str):
        return set()
    if not any(origin.startswith(p) for p in _RESPONSE_ORIGIN_PREFIXES):
        return set()
    if world_dir is None:
        return set()

    cutoff = dt.datetime.now() - dt.timedelta(hours=24)
    expected = set()

    expected |= _scan_affects_tags(
        world_dir / "board" / "findings.jsonl",
        type_filter=None,
        required_tag="insight_trigger",
        self_agent=self_agent,
        cutoff=cutoff,
    )

    expected |= _scan_affects_tags(
        world_dir / "board" / "coordination.jsonl",
        type_filter="review-request",
        required_tag=None,
        self_agent=self_agent,
        cutoff=cutoff,
    )

    return expected


# --- Check 5: target_state ---------------------------------------------------

def _check_target_state(goal: dict, agent_name: str, project_root: Path):
    """Filing-time grep-before-file: block when the goal's target file
    already contains the identifiers the description says to implement.
    g-115-141 / rb-382."""
    if is_read_intent(goal.get("title"), _caller="goal-duplication-gate.py"):
        return {
            "name": "target_state",
            "passed": True,
            "reason": (
                "skipped (READ-intent goal title — identifiers in target "
                "files are precondition not completion signal; see "
                "_target_state.READ_INTENT_VERBS / rb-398)"
            ),
            "matches": [],
        }
    # Completed-Maintain skip (). When a status=completed Maintain
    # goal records just-shipped framework code, all identifiers from the
    # description are already present in the target file — that IS the
    # completion signal, not duplication. Without this skip, every
    # retroactive Maintain record needs --override-duplication
    # (canonical incident:  / encode-session 2026-05-16).
    # Structural twin of the is_read_intent skip above — same semantic
    # inversion class (identifiers-present is precondition/completion,
    # not duplication evidence).
    if (goal.get("status") == "completed"
            and (goal.get("title") or "").startswith("Maintain:")):
        return {
            "name": "target_state",
            "passed": True,
            "reason": (
                "skipped (status=completed Maintain goal — identifiers in "
                "target files are the completion signal, not duplication; "
                "g-115-836 / encode-session 2026-05-16)"
            ),
            "matches": [],
        }
    # NOTE: _target_state._resolve_search_roots reads MIND_AGENT env when
    # agent_name is empty/None. CLI sets env so this matches legacy. Daemon
    # callers MUST pass a non-empty agent_name explicitly; empty/None will
    # leak the daemon's startup env into per-request results.
    search_roots = _resolve_search_roots(agent_name=agent_name)
    try:
        ex = extract_and_infer_targets(
            goal.get("title"), goal.get("description"),
            search_roots=search_roots,
        )
    except Exception as e:
        return {
            "name": "target_state",
            "passed": True,
            "reason": "skipped (extractor error: " + str(e) + ")",
            "matches": [],
        }
    if ex["confidence"] == "none" or not ex["identifiers"]:
        return {
            "name": "target_state",
            "passed": True,
            "reason": "skipped (no target file+identifier pair extracted)",
            "matches": [],
        }
    try:
        pr = probe_target_state(
            project_root,
            ex["target_files"],
            ex["identifiers"],
            ex["line_hints"],
            allowed_roots=search_roots,
            lenient_match=ex.get("target_files_inferred", False),
        )
    except Exception as e:
        return {
            "name": "target_state",
            "passed": True,
            "reason": "skipped (probe error: " + str(e) + ")",
            "matches": [],
        }

    if pr["verdict"] == "already_present":
        return {
            "name": "target_state",
            "passed": False,
            "reason": ("target file(s) already contain " +
                       str(pr["total_hits"]) + "/" + str(pr["total_identifiers"]) +
                       " identifiers from goal description (hit_ratio=" +
                       str(pr.get("hit_ratio", 0.0)) + ")"),
            "matches": [{
                "verdict": pr["verdict"],
                "target_files": ex["target_files"],
                "identifiers": ex["identifiers"],
                "hit_ratio": pr.get("hit_ratio"),
                "per_file_hits": [
                    {"file": pf["file"], "hits": pf["hits"][:5],
                     "miss_count": len(pf["misses"])}
                    for pf in pr["per_file"]
                ],
            }],
        }
    return {
        "name": "target_state",
        "passed": True,
        "reason": ("no hard overlap — verdict=" + pr["verdict"] +
                   " (" + str(pr.get("total_hits", 0)) + "/" +
                   str(pr.get("total_identifiers", 0)) + " identifiers present)"),
        "matches": [],
    }


# --- Check 6: pending_queue --------------------------------------------------

def _iter_pending_goals_from_jsonl(jsonl_path: Path, proposed_id: str):
    """Yield (asp_id, goal) for goals with status in (pending, in-progress)
    from one aspirations.jsonl file. Skips the proposed goal if it shares
    an id with an existing record (idempotent re-file). Fail-open on read
    errors — missing/unreadable files yield nothing."""
    if not jsonl_path.exists():
        return
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    asp = json.loads(line)
                except Exception:
                    continue
                asp_id = asp.get("id") or ""
                for g in asp.get("goals", []) or []:
                    if not isinstance(g, dict):
                        continue
                    if g.get("status") not in ("pending", "in-progress"):
                        continue
                    if proposed_id and g.get("id") == proposed_id:
                        continue
                    yield (asp_id, g)
    except Exception:
        return


def _check_pending_queue(goal, file_paths, keywords, source_name,
                        world_dir, project_root):
    """Scan world + per-agent aspirations.jsonl for pending/in-progress
    goals overlapping the proposed goal. Closes the missing-CORPUS gap
    flagged by g-115-783: the other five checks NEVER read the pending
    queue, so a new proposal whose semantic twin was already pending
    (canonical incident: 4-way l1-skew dup cluster
    g-115-743/776/778/779 over 22h, consolidated manually) slipped
    through cleanly.

    Match strategies (any one blocks):
      1. origin_signal exact match — STRONG; symptom-keyed identity
         (the goal description: "keyed on symptom/origin-signal not
         title prose"). Only fires when both proposed and existing
         origin_signal are non-empty.
      2. Structural overlap with co-signal — mirrors
         _check_recent_completions: weighted >= 1.5, unique_hits >= 2,
         structural co-signal (file-path hit OR keyword with -_0-9).

    Sources scanned:
      - world_dir/aspirations.jsonl (world queue)
      - project_root/agents/*/aspirations.jsonl (per-agent queues — ALL
        agents; pending dups are equally bad in any queue).

    Skip-paths:
      - world_dir and project_root both None → skip (no sources)
      - Empty proposed signals AND empty origin_signal → skip
      - Read errors → skip the affected file silently
    """
    if world_dir is None and project_root is None:
        return {
            "name": "pending_queue",
            "passed": True,
            "reason": "skipped (no world_dir or project_root)",
            "matches": [],
        }

    proposed_origin = (goal.get("origin_signal") or "").strip()
    proposed_id = goal.get("id") or ""

    # Collect source paths. Sorted iteration for deterministic ordering.
    source_paths = []
    if world_dir is not None:
        source_paths.append(("world", world_dir / "aspirations.jsonl"))
    if project_root is not None:
        agents_root = project_root / "agents"
        if agents_root.is_dir():
            for agent_dir in sorted(agents_root.iterdir()):
                if not agent_dir.is_dir():
                    continue
                source_paths.append((f"agent:{agent_dir.name}",
                                     agent_dir / "aspirations.jsonl"))

    # Collect candidate pending goals across all sources.
    # Shape per entry: {source, asp_id, goal_id, title, description,
    # origin_signal, text} where text = (title + description).lower().
    candidates = []
    for source_label, jp in source_paths:
        for asp_id, g in _iter_pending_goals_from_jsonl(jp, proposed_id):
            title = g.get("title") or ""
            description = g.get("description") or ""
            candidates.append({
                "source": source_label,
                "asp_id": asp_id,
                "goal_id": g.get("id") or "",
                "title": title,
                "description": description,
                "origin_signal": (g.get("origin_signal") or "").strip(),
                "text": (title + " " + description).lower(),
            })

    if not candidates:
        return {
            "name": "pending_queue",
            "passed": True,
            "reason": "no pending/in-progress goals across world+agent queues",
            "matches": [],
        }

    # Strategy 1: origin_signal exact match.
    # Symptom-keyed origin_signals are the strongest non-id duplicate
    # signal — e.g. "idea:dup-gate-pending-corpus-gap" or
    # "alert-email:s3-key/foo". Exact match blocks immediately.
    # Bare-tag origins (user_directive, idle_fallback) are generic standalone
    # categories shared by many legitimate distinct goals (a live queue carries
    # 8+ pending user_directive goals), NOT unique symptom keys. Exact-matching
    # them here false-positives every second user-directed goal (canonical: the
    # update-goal cascade tests + the concurrent-add hammer, 2026-06-03). Real
    # dups that share a bare-tag origin are still caught by Strategy 2 below.
    origin_matches = []
    if proposed_origin and proposed_origin not in _GENERIC_BARE_ORIGINS:
        for c in candidates:
            if c["origin_signal"] and c["origin_signal"] == proposed_origin:
                origin_matches.append({
                    "source": c["source"],
                    "asp_id": c["asp_id"],
                    "goal_id": c["goal_id"],
                    "title": c["title"][:120],
                    "origin_signal": c["origin_signal"],
                    "match_strategy": "origin_signal",
                })

    # Strategy 2: structural overlap mirroring _check_recent_completions.
    # IDF computed over the candidate text corpus so common queue vocab
    # doesn't inflate matches (matches the rare-identifier discipline in
    # the recent_completions check).
    all_terms = set(file_paths) | set(keywords)
    # _compute_idf takes entries with `key_finding` field. Wrap candidate
    # text in that shape so the helper applies symmetrically.
    pseudo_entries = [{"key_finding": c["text"]} for c in candidates]
    idf = _compute_idf(pseudo_entries, all_terms) if all_terms else {}

    WEIGHT_THRESHOLD = 1.5
    MIN_UNIQUE_HITS = 2

    structural_matches = []
    advisories = []
    for c in candidates:
        text = c["text"]
        # sorted() — file_paths/keywords are sets; stable order needed for
        # determinism across processes (Python hash randomization).
        hit_paths = sorted(fp for fp in file_paths if fp.lower() in text)
        hit_kws = sorted(kw for kw in keywords if kw in text)
        unique_hits = len(hit_paths) + len(hit_kws)
        weighted = sum(idf.get(fp, 1.0) for fp in hit_paths) + \
                   sum(idf.get(kw, 1.0) for kw in hit_kws)
        strong = unique_hits >= MIN_UNIQUE_HITS and weighted >= WEIGHT_THRESHOLD
        # Structural co-signal required — stricter than the recent_completions
        # variant. Pending+in-progress corpus is ~5-10x larger than
        # recent_completions (377 vs ~50), so generic compound vocabulary
        # ("cross-agent", "fresh-eyes", "post-execution" — hyphen-only)
        # produces too many false-positive structural matches. Require a
        # file-path hit OR a keyword with [_0-9] (true identifier — goal
        # IDs like , rb-IDs, script names with digit suffixes).
        # Hyphen-alone does NOT qualify. This aligns with the goal's
        # "keyed on symptom/origin-signal not title prose" intent — the
        # PRIMARY duplicate signal is origin_signal exact match
        # (Strategy 1 above); structural overlap is the safety net for
        # cases where the duplicate was filed with a distinct
        # origin_signal but shares structural fingerprints.
        has_specific = bool(hit_paths) or any(
            re.search(r"[_0-9]", k) for k in hit_kws)
        if strong and has_specific:
            structural_matches.append({
                "source": c["source"],
                "asp_id": c["asp_id"],
                "goal_id": c["goal_id"],
                "title": c["title"][:120],
                "origin_signal": c["origin_signal"],
                "file_path_hits": hit_paths,
                "keyword_hits": hit_kws[:5],
                "weighted_score": round(weighted, 2),
                "unique_hits": unique_hits,
                "match_strategy": "structural_overlap",
            })
        elif strong:
            advisories.append({
                "source": c["source"],
                "goal_id": c["goal_id"],
                "unique_hits": unique_hits,
                "weighted_score": round(weighted, 2),
                "keyword_hits": hit_kws[:5],
                "strong_keyword_only": True,
            })
        elif unique_hits >= 1:
            advisories.append({
                "source": c["source"],
                "goal_id": c["goal_id"],
                "unique_hits": unique_hits,
                "weighted_score": round(weighted, 2),
            })

    matches = origin_matches + structural_matches
    advisories.sort(key=lambda a: (not a.get("strong_keyword_only"),
                                    -a.get("weighted_score", 0.0)))
    strong_only = sum(1 for a in advisories if a.get("strong_keyword_only"))

    if matches:
        strategy_summary = ", ".join(sorted(set(m["match_strategy"]
                                               for m in matches)))
        return {
            "name": "pending_queue",
            "passed": False,
            "reason": ("overlap with " + str(len(matches)) +
                       " pending/in-progress goal(s) across world+agent queues"
                       " [strategies=" + strategy_summary +
                       ", source=" + source_name +
                       ", scanned=" + str(len(candidates)) + "]"),
            "matches": matches,
            "advisories": advisories[:5],
        }
    return {
        "name": "pending_queue",
        "passed": True,
        "reason": ("no blocking overlap (scanned=" + str(len(candidates)) +
                   " pending/in-progress, source=" + source_name +
                   ", " + str(len(advisories)) +
                   " sub-threshold advisories"
                   + (", " + str(strong_only) +
                      " strong keyword-only demoted (no file-path/identifier co-signal)"
                      if strong_only else "") + ")"),
        "matches": [],
        "advisories": advisories[:5],
    }


# --- Override audit ----------------------------------------------------------

def _log_override(world_dir: Optional[Path], agent_name: str, goal: dict,
                  justification: str, failing_checks: list) -> Optional[str]:
    """Append to <world_dir>/goal-duplication-overrides.jsonl. Returns the
    written path on success, None on missing world_dir or write failure.
    Fail-silent on write errors."""
    if world_dir is None:
        print("[goal-duplication-gate] WARN: override granted but not logged "
              "(no WORLD_PATH resolved).", file=sys.stderr)
        return None
    log_path = world_dir / "goal-duplication-overrides.jsonl"
    try:
        record = {
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "agent": agent_name or "unknown",
            "title": (goal.get("title") or "")[:200],
            "justification": justification,
            "which_checks_bypassed": [c["name"] for c in failing_checks],
            "match_summary": {c["name"]: len(c.get("matches") or []) for c in failing_checks},
        }
        locked_append_jsonl(str(log_path), record)
        return str(log_path)
    except Exception as e:
        print("[goal-duplication-gate] WARN: override-log append failed: " + str(e),
              file=sys.stderr)
        return None


# --- Main entry point --------------------------------------------------------

def evaluate(goal: dict, *, override_duplication: Optional[str] = None,
             agent_name: str = "", world_dir: Optional[Path] = None,
             project_root: Optional[Path] = None) -> dict:
    """Run all five checks. See module docstring for return shape + side effects.

    Args:
        goal: parsed goal JSON dict (title, description, verification,
            origin_signal, participants, source, id, recurring).
        override_duplication: justification string. When non-empty AND any
            check failed, audit-log is written and would_block flips False.
        agent_name: MIND_AGENT value. Filters non-self overlap sources.
            Empty string allowed — every completion treated as non-self
            (conservative default flags everything).
        world_dir: WORLD_DIR for team-state.yaml + findings.jsonl reads
            and the override audit log. None disables all world-backed
            checks (they fail-open with "skipped" reasons).
        project_root: framework repo root for git subprocess + target_state
            probe. Defaults to repo root computed from __file__.
    """
    if project_root is None:
        project_root = _DEFAULT_PROJECT_ROOT

    self_agent = agent_name or ""
    file_paths, keywords, source_name = _extract_signals(goal)

    expected_paths = _expected_coverage_paths(goal, self_agent, world_dir)

    checks = [
        _check_recent_completions(goal, file_paths, keywords, self_agent,
                                  source_name, world_dir, expected_paths),
        _check_partner_in_flight(goal, file_paths, keywords, self_agent,
                                 source_name, world_dir),
        _check_git_log(goal, file_paths, project_root),
        _check_insight_triggers(goal, file_paths, self_agent, world_dir,
                                expected_paths),
        _check_target_state(goal, self_agent, project_root),
        _check_pending_queue(goal, file_paths, keywords, source_name,
                             world_dir, project_root),
    ]
    failing = [c for c in checks if not c.get("passed")]
    would_block = bool(failing) and not override_duplication

    result = {
        "would_block": would_block,
        "checks": checks,
        "failing_count": len(failing),
        "self_agent": self_agent or None,
        "file_paths_detected": sorted(file_paths),
        "expected_coverage_paths": sorted(expected_paths),
        "override_applied": override_duplication,
    }
    result["reason"] = failing[0]["reason"] if failing else "all checks passed"

    # description_quality_warning — informational only, never flips
    # would_block. Recurring goals exempt (title-as-spec by design).
    if not goal.get("recurring"):
        title_tokens = _count_non_stopword_tokens(goal.get("title", ""))
        desc_tokens = _count_non_stopword_tokens(goal.get("description", ""))
        if desc_tokens < title_tokens:
            result["description_quality_warning"] = True
            result["description_quality_reason"] = (
                f"description has {desc_tokens} non-stopword tokens vs "
                f"{title_tokens} in title — consider expanding the description"
            )

    if override_duplication:
        log_path = _log_override(world_dir, self_agent, goal,
                                 override_duplication, failing)
        result["override_logged_to"] = log_path

    # Decision derivation.
    if not failing:
        decision = "noop"
        trigger = None
    elif override_duplication:
        decision = "override"
        trigger = failing[0].get("name")
    else:
        decision = "block"
        trigger = failing[0].get("name")
    _gate_log(
        "goal-duplication-gate",
        decision,
        trigger_matched=trigger,
        payload=(goal.get("title") or "")[:500],
        override_reason=override_duplication,
        extra={
            "would_block": would_block,
            "failing_count": len(failing),
            "failing_checks": [c.get("name") for c in failing],
            "all_check_names": [c.get("name") for c in checks],
            "self_agent": self_agent or None,
            "file_paths_count": len(file_paths),
        },
    )

    return result
