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
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

import yaml  # type: ignore

from _fileops import locked_append_jsonl  # type: ignore
from _gate_log import log as _gate_log  # type: ignore
from _paths import agents_root as _agents_root  # type: ignore
from _target_state import (  # type: ignore
    _FILE_PATH_RE,
    _resolve_search_roots,
    extract_and_infer_targets,
    is_modify_intent,
    is_read_intent,
    is_removal_intent,
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
    # 5: generic structural/framework state-vocabulary that recurs
    # across many framework-finding goals (loop_state checks, iteration-close
    # audits, etc.). Each is a poor duplicate discriminator: shared alongside a
    # single structural topic token (e.g. loop_state) they inflated unique_hits
    # to >=2 and weighted to >=1.5, false-blocking file-path-DISTINCT goals
    # (alpha session 92: 4 FPs; bravo: 2 FPs, each verified-distinct then
    # override-cleared). Demoting them keeps a lone shared structural token at
    # N=1 (advisory, not a block) while genuine multi-identifier duplicates
    # still block. Token shape ([-_0-9]) remains the discriminator, not generic
    # prose vocab. Expand this set as new generic-token FP classes surface.
    "exists", "global", "populated", "recurring", "class", "close",
    # 6: generic English VERBS/adverbs (not structural vocab) that
    # recur across unrelated framework-finding goals and are poor duplicate
    # discriminators. Session-93 ground truth: the gate false-blocked FIVE
    # legitimate goals on these (5 vs , 7 vs
    # , 6, , drain-temp — all override-cleared, see
    # world/goal-duplication-overrides.jsonl). "re-run" is doubly bad: its
    # hyphen matched the has_specific co-signal (re.search(r"[-_0-9]") in
    # _check_recent_completions), turning a ZERO-file-path generic-verb overlap
    # into a HARD block. Demoting all of them removes the inflation; a genuine
    # duplicate still needs a structural co-signal ([-_0-9] identifier or
    # file-path), which these plain words never carry.
    "cause", "caused", "causes", "confirm", "confirmed", "confirms",
    "every", "harden", "hardened", "hardening", "rerun", "re-run",
    "finding", "findings",
    # : verification-SCHEMA vocabulary. _extract_signals pulls the
    # proposed goal's keywords from its verification block (outcomes + checks)
    # when present — so a check dict's `type` VALUE (command_check, file_check,
    # …) and its `condition`/`command` KEY names become "keywords". These recur
    # in EVERY goal's verification block, making them zero-discriminant duplicate
    # signals. Worse: the `*_check` / `*_suite` type-values and `python3` carry
    # an underscore/digit, so they satisfy the has_specific structured-identifier
    # co-signal ([_0-9] in pending_queue, [-_0-9] in recent_completions) — turning
    # a schema-token-ONLY overlap into a HARD block instead of a demoted advisory.
    # The IDF floor does NOT catch them: the IDF corpus is candidate
    # title+description PROSE (not verification blocks), where schema tokens are
    # rare → high IDF → they look like rare identifiers. Evidence 2026-07-10 (echo
    # session 101): FOUR unrelated adds blocked in one day on keyword_hits
    # {check, command, command_check, condition, python3, scripts, block},
    # file_path_hits empty every time (recurring goals with rich verification
    # blocks structurally match every other such goal; the 4th block fired on the
    # very Idea goal describing this bug —  ×3,  ×1). Demoting
    # this vocabulary removes both the inflation AND the false co-signal in BOTH
    # checks (recent_completions + pending_queue) — a genuine duplicate still needs
    # a real content token or a file-path / non-schema [-_0-9] identifier co-signal.
    # The check-`type` VALUES are enumerated from the live queue (file_check ×41,
    # command_check ×14, code_check ×7, test_suite ×6, command_succeeds ×3,
    # manual_review ×2, manual_check/grep_check/doc_check/eval_check/board_post ×1).
    # DECISION (, 2026-07-13): manual expansion STAYS — there is no
    # registry to derive from. Verified: verification check-`type` values are
    # free-form strings validated NOWHERE (the VALID_TYPES enums in pipeline.py,
    # reasoning-bank.py, experience.py cover RECORD types — high-conviction,
    # success/failure, etc. — NOT verification checks). Deriving this list from a
    # registry would require FIRST building an enum + imposing type-validation on
    # every goal author — a larger constraint than this small, slowly-changing
    # list. The superior lighter fix — a suffix pattern (*_check|*_suite|
    # *_succeeds|*_review) that auto-demotes NEW schema-token types WITHOUT manual
    # expansion — is filed as . Until that lands, add new check-type
    # values to the list below by hand.
    "check", "command", "command_check", "condition", "python3", "scripts",
    "block", "file_check", "code_check", "test_suite", "command_succeeds",
    "manual_review", "manual_check", "grep_check", "doc_check", "eval_check",
    "board_post",
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

# Decomposition siblings legitimately share the parent's origin_signal
# ("decomposition:<parent-id>"): filing the 2nd+ child of one parent
# exact-matches the 1st by origin_signal even though each child is a DISTINCT
# deliverable. Same false-positive class as the bare-tag origins above, so the
# prefix is exempt from Strategy-1 exact-match. Children of DIFFERENT parents
# carry different strings (never collide); a TRUE duplicate child still trips
# Strategy 2 (structural keyword/file overlap). 6; canonical incidents
#  vs  and 2 (each needed --override-duplication).
_SIBLING_SHARED_ORIGIN_PREFIXES = ("decomposition:",)


def _is_directive_routing_goal(goal: dict) -> bool:
    """A directive/handoff goal that ROUTES work to a target agent. Its
    description necessarily RECAPS that agent's domain work (keyword-dense), so
    it structurally matches the target's COMPLETED goals in recent_completions
    — a false-positive duplicate signal, NOT real duplicate work. The FP
    DROPPED a Bravo handoff from the durable queue (g-115-23, directive
    substance lost) and forced --override-duplication on an echo ARC directive
    (g-115-1538). See rb-2462 / g-115-1674.

    Two signals (per the goal spec): a user directive — the bare-tag
    origin_signal "user_directive" (the only standalone directive origin in
    gates.origin_signal.ALLOWED_PREFIXES) — OR a cross-agent handoff
    (handoff_to set). ONLY _check_recent_completions consults this: the other
    five checks (pending_queue, partner_in_flight, git_log, target_state,
    insight_triggers) still run, so a TRUE duplicate directive (already
    pending / in-flight / implemented) is still caught.
    """
    origin = (goal.get("origin_signal") or "").strip()
    if origin == "user_directive":
        return True
    handoff_to = goal.get("handoff_to")
    if handoff_to and str(handoff_to).strip():
        return True
    return False


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


# 7: a file-path named ONLY in a NEGATIVE / exclusion context
# ("feature-path-excluded for retrieve.sh", "audit ... other than tree-read.sh")
# asserts the OPPOSITE of aboutness. Counting it as a duplicate co-signal
# false-blocks distinct work — canonical incident 6: 's
# "feature-path-excluded for tree-read.sh/retrieve.sh" HARD-blocked an unrelated
# Maintain goal via file_path_hits=['retrieve.sh'] (weighted 7.53). The fix is a
# surgical context-disqualifier at the single extraction point (guard-958:
# prefer context-disqualifiers over a matcher rewrite; verify RECALL with a
# genuine-positive control). CONSERVATIVE marker set — only unambiguous
# exclusion vocabulary, to avoid dropping a genuinely-about path that merely
# sits near code-discussion words like "skip"/"ignore"/"except:" (recall loss
# is the failure mode guard-958 warns against).
_EXCLUSION_MARKER_RE = re.compile(
    r"\b(?:exclud\w*|except\s+for|other\s+than|aside\s+from|apart\s+from)\b",
    re.IGNORECASE,
)
# : contrast markers. A goal-id preceded (within its clause) by one of
# these disclaims duplication rather than asserting it ("distinct from
# ", "complements ", "unlike 5"). Scanned by
# _path_in_exclusion_context(marker_re=...) so a cited-in-contrast goal-id is
# dropped from the co-signal keyword set while a neutral / shared-work goal-id
# is KEPT (preserves the structural-co-signal tests G3/G9). Look-BACK only,
# matching the exclusion-context precedent; "g-XXX ... is a distinct sweep"
# (marker-AFTER) is a known gap deliberately left for a follow-up.
_CONTRAST_MARKER_RE = re.compile(
    r"\b(?:distinct(?:\s+from|\s+work)?|complements?|complementary(?:\s+to)?|"
    r"unlike|not\s+a\s+dup\w*|separate\s+from|as\s+opposed\s+to|rather\s+than|"
    r"in\s+contrast(?:\s+to)?|differs?\s+from|different\s+from|vs\.?|versus)\b",
    re.IGNORECASE,
)
# Clause boundary for scoping the exclusion look-back: a sentence end (". ") or
# ";" / newline. A BARE "." is deliberately NOT a boundary — file extensions
# (.sh/.py/.md) and paths (a/b.sh) carry non-boundary dots that would otherwise
# truncate the exclusion context and mask the marker.
_CLAUSE_DELIM_RE = re.compile(r"(?:\.\s)|[;\n]")


def _path_in_exclusion_context(text_lower: str, path_lower: str,
                               window: int = 80, marker_re=None) -> bool:
    """True IFF `path_lower` occurs in `text_lower` AND every occurrence is
    preceded — within its own clause, capped at `window` chars back — by a
    context marker. Any positive/aboutness occurrence returns False so the
    token is KEPT (recall preservation, guard-958). No occurrence at all → False
    (nothing to disqualify).

    marker_re defaults to _EXCLUSION_MARKER_RE (path exclusion — "except for
    X"). g-248-113 passes _CONTRAST_MARKER_RE to reuse this clause-scoped
    look-back for goal-id contrast citations ("distinct from g-XXX")."""
    marker = marker_re if marker_re is not None else _EXCLUSION_MARKER_RE
    start = 0
    saw_any = False
    while True:
        idx = text_lower.find(path_lower, start)
        if idx == -1:
            break
        saw_any = True
        ctx = text_lower[max(0, idx - window):idx]
        # Trim to the current clause: keep only text after the last boundary,
        # so an exclusion marker from a PRIOR sentence never taints this path.
        last = None
        for m in _CLAUSE_DELIM_RE.finditer(ctx):
            last = m
        if last is not None:
            ctx = ctx[last.end():]
        if not marker.search(ctx):
            return False  # positive-context occurrence → keep the token
        start = idx + len(path_lower)
    return saw_any


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
    file_paths_all = set(_FILE_PATH_RE.findall(text))

    # Strip ALL detected paths (incl. exclusion-context ones) from the text
    # before keyword extraction so a path like retrieve.sh never leaks its stem
    # ("retrieve") into keywords — the exclusion filter below must remove the
    # path's aboutness signal ENTIRELY, not shift it from file-path to keyword.
    cleaned = text
    for fp in file_paths_all:
        cleaned = cleaned.replace(fp, " ")
    words = re.findall(r"[a-zA-Z][\w-]{4,}", cleaned.lower())
    text_lower = text.lower()
    # : a goal-id cited in a CONTRAST clause ("distinct from
    # ", "complements ", "unlike 5") disclaims
    # duplication, yet its [-_0-9] shape otherwise makes it a false
    # "structural co-signal" that inflates overlap — a recurring slice of the
    # 3252 override-ledger entries. Drop a goal-id from keywords ONLY when
    # every occurrence sits in such a contrast context; a goal-id in NEUTRAL or
    # shared-work context is KEPT (it can be genuine duplicate evidence when
    # paired with a topical token — tests G3/G9). Genuine goal-id RELATIONSHIPS
    # stay handled by _lineage_relation + Strategy 1. Mirrors the
    # exclusion-context path drop below; _GOAL_ID_RE is defined with the
    # pending-queue lineage block. fullmatch keeps it precise (only an exact
    # goal-id token qualifies).
    keywords = {
        w for w in words
        if w not in _STOPWORDS
        and not (_GOAL_ID_RE.fullmatch(w)
                 and _path_in_exclusion_context(text_lower, w,
                                                marker_re=_CONTRAST_MARKER_RE))
    }

    # 7: drop exclusion-context-only paths from the co-signal set.
    file_paths = {fp for fp in file_paths_all
                  if not _path_in_exclusion_context(text_lower, fp.lower())}
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
    """Return (idf_map, n) using recent_completions key_findings as the
    corpus. g-248-12: rare identifiers contribute high weight; common ones
    contribute near-zero. idf_map is {t: 1.0} and n is 0 (fail-open) when the
    corpus is empty. g-115-1325: the corpus size n is returned so callers can
    derive a df-equivalent IDF floor log(n/(1+CEIL)) from the LIVE corpus size
    (a fixed IDF floor cannot span the two corpora — see STRUCT_IDF_DF_CEIL).
    """
    findings = []
    for e in entries:
        if isinstance(e, dict):
            kf = (e.get("key_finding") or "").lower()
            if kf:
                findings.append(kf)
    n = len(findings)
    if n == 0:
        return {t: 1.0 for t in terms}, 0
    out = {}
    for t in terms:
        tl = t.lower()
        df = sum(1 for kf in findings if tl in kf)
        out[t] = math.log(n / (1 + df)) if df < n else 0.0
    return out, n


# 5: a structured identifier (token shape [-_0-9]) counts toward the
# strong-block co-signal ONLY when df(k) <= this ceiling in the IDF corpus —
# i.e. it is rare (unique to the one compared goal), not cluster-common vocab.
# DF_CEIL=1 is empirically forced (guard-594): the FP identifiers that
# false-blocked 4 (b9568/ df=2,  df=3,  df=6)
# are all df>=2; df=1 identifiers (the legit duplicate signal, structural-
# co-signal gate test CASE G3) are preserved. Implemented as an IDF floor
# log(n/(1+CEIL)) derived from the LIVE corpus size because a fixed floor
# cannot span recent_completions (n~50, df=1 idf=3.22) and pending_queue
# (n~337, df=1 idf=5.13). idf(k) >= log(n/(1+CEIL))  <=>  df(k) <= CEIL.
STRUCT_IDF_DF_CEIL = 1


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


def _recurring_goal_ids(world_dir):
    """Goal ids of RECURRING goals across the world + every per-agent queue.

    A recurring/reflection goal's COMPLETION in recent_completions is a keyword
    vacuum: a generic sweep/reflection key_finding with EMPTY file_path_hits that
    trips `has_specific` via hyphenated compounds (env-server, end-to-end) — a
    structural false positive, NOT duplicate-work evidence (g-248-114; the
    g-335-103/104/105 new-capability goals were HARD-blocked by g-115-23
    "Recurring: Product world model refresh sweep" + g-001-09 learn-pass
    reflection, file_path_hits empty every time — omni msg-20260717-213111).

    Recurring goals never archive (they re-fire on cadence and persist in the
    live queue at any status), so an active-queue scan across world + agents is
    complete for the class — no writer-side marker migration needed. Both false-
    block classes ("recurring" sweeps and "reflection"/learn-pass goals) carry
    `recurring: true`, so this single predicate covers both. Fail-open: any read
    error yields an empty set (the exemption simply does not fire, preserving the
    pre-fix hard-block behavior). Mirrors the world+agents path enumeration in
    _check_pending_queue (MIND_AGENTS_ROOT override → _agents_root())."""
    ids = set()
    paths = []
    if world_dir is not None:
        paths.append(world_dir / "aspirations.jsonl")
    try:
        env_root = os.environ.get("MIND_AGENTS_ROOT", "").strip()
        agents_root = Path(env_root) if env_root else _agents_root()
        if agents_root.is_dir():
            for agent_dir in sorted(agents_root.iterdir()):
                if agent_dir.is_dir():
                    paths.append(agent_dir / "aspirations.jsonl")
    except Exception:
        pass
    for jp in paths:
        try:
            if not jp.exists():
                continue
            with open(jp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        asp = json.loads(line)
                    except Exception:
                        continue
                    for g in asp.get("goals", []) or []:
                        if isinstance(g, dict) and g.get("recurring") is True:
                            gid = g.get("id")
                            if gid:
                                ids.add(gid)
        except Exception:
            continue
    return ids


def _check_recent_completions(goal, file_paths, keywords, self_agent,
                              source_name, world_dir, expected_paths=None):
    """N-agent correct: filters `completed_by != self_agent`. Scales to any
    N>=1 without config change. DO NOT add a `partner` param or peer-list
    lookup."""
    # 4: directive/handoff-routing goals are EXEMPT from this
    # keyword-overlap-vs-COMPLETED check (see _is_directive_routing_goal — the
    # FP class that dropped  and forced --override on 8;
    # rb-2462). Scoped to THIS check only; the other five still run, so a TRUE
    # duplicate directive (already pending / in-flight / implemented) is still
    # caught. Placed before the world_dir read: a directive is exempt regardless.
    if _is_directive_routing_goal(goal):
        return {
            "name": "recent_completions",
            "passed": True,
            "reason": ("skipped (directive/handoff-routing goal — description "
                       "recaps target-agent domain work; completed-overlap is a "
                       "structural false positive, g-115-1674)"),
            "matches": [],
        }
    # Completed-Maintain skip (6) — completes carve-out parity with the
    # other fuzzy-overlap checks (partner_in_flight 7, git_log
    # /1813, target_state, insight_triggers 5). A
    # status=completed Maintain filing RECORDS work that already happened;
    # keyword/path overlap with a partner's recent completion is a completion
    # coincidence, not a NEW duplicate — blocking only prevents the record (and
    # its encoding) from landing. Placed before the team-state read so the skip
    # also saves it. Exact-duplicate RECORDS are still caught by pending_queue
    # Strategy 1 (which RESTRICTS rather than fully skips).
    if (goal.get("status") == "completed"
            and (goal.get("title") or "").startswith("Maintain:")):
        return {
            "name": "recent_completions",
            "passed": True,
            "reason": ("skipped (status=completed Maintain goal — recent-completion "
                       "overlap is completion coincidence, not duplication; "
                       "g-115-2686 / g-115-2477 family)"),
            "matches": [],
        }
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
    idf, idf_n = _compute_idf(entries, all_terms) if all_terms else ({}, 0)
    # 5: per-term IDF floor for the token-shape strong path (below).
    # idf(k) >= idf_floor  <=>  df(k) <= STRUCT_IDF_DF_CEIL — keeps only rare
    # (unique) structured identifiers as discriminating co-signals. Derived
    # from the LIVE corpus size; inert (0.0) on a corpus too small to discriminate.
    idf_floor = (math.log(idf_n / (1 + STRUCT_IDF_DF_CEIL))
                 if idf_n > (1 + STRUCT_IDF_DF_CEIL) else 0.0)

    WEIGHT_THRESHOLD = 1.5
    MIN_UNIQUE_HITS = 2

    expected = expected_paths or set()

    # : recurring/reflection goal ids — their COMPLETIONS are keyword
    # vacuums (empty file_path_hits, generic hyphenated compounds) and must not
    # HARD-block a new-capability goal. Computed once; empty on any read error.
    recurring_ids = _recurring_goal_ids(world_dir)

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
        # 5 refinement: the structured-token branch ALSO requires
        # per-term IDF >= idf_floor (df(k) <= STRUCT_IDF_DF_CEIL). This is a
        # TIGHTENING, not "trusting the IDF sum" — a cluster-COMMON structured
        # identifier (low per-term IDF; appears across many entries) is shared
        # cluster vocab, not duplicate-work evidence. file-path hits unaffected.
        has_specific = bool(hit_paths) or any(
            re.search(r"[-_0-9]", k) and idf.get(k, idf_floor) >= idf_floor
            for k in hit_kws)
        # : a recurring/reflection COMPLETION matched on KEYWORDS ONLY
        # (empty hit_paths) is a keyword vacuum — has_specific tripped on a
        # generic hyphenated compound (env-server, end-to-end), not duplicate
        # work. DEMOTE to advisory (stays visible, never HARD-blocks). Scoped to
        # the vacuum case: a recurring completion that shares a real FILE PATH
        # (hit_paths non-empty) still HARD-blocks — that is genuine shared-work
        # evidence, not a vacuum.
        recurring_vacuum = (goal_id in recurring_ids) and not hit_paths
        if strong and has_specific and not recurring_vacuum:
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
        elif strong and has_specific:
            # recurring/reflection keyword-vacuum completion — demoted, not blocked.
            advisories.append({
                "goal_id": goal_id,
                "completed_by": completed_by,
                "unique_hits": unique_hits,
                "weighted_score": round(weighted, 2),
                "keyword_hits": hit_kws[:5],
                "recurring_vacuum_exempt": True,
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
    # Completed-Maintain skip (7, extending  / 3).
    # A status=completed Maintain filing records work that ALREADY happened —
    # it cannot race a partner's live in_flight goal, so scope overlap with
    # live work is vocabulary coincidence, not a claim conflict (canonical FPs:
    # 6 + 7's own filing, both blocked on generic tokens vs
    # foxtrot's stranded-claim scan within one precheck). Placed before the
    # team-state read so the skip also saves the authoritative S3 shard reads.
    # Exact-duplicate RECORDS are still caught by pending_queue Strategy 1.
    if (goal.get("status") == "completed"
            and (goal.get("title") or "").startswith("Maintain:")):
        return {
            "name": "partner_in_flight",
            "passed": True,
            "reason": ("skipped (status=completed Maintain goal — completed "
                       "records cannot race live partner work; g-115-2477 / "
                       "g-115-836 family)"),
            "matches": [],
        }
    ts_path = world_dir / "team-state.yaml"
    try:
        state = {}
        if ts_path.exists():
            with open(ts_path, "r", encoding="utf-8") as f:
                state = yaml.safe_load(f) or {}
        #  sharding: overlay per-agent row files (rows win
        # newest-wins) so partner in_flight reads the sharded truth.
        # 8 / guard-980: on the own-cloud backend read each peer shard
        # FRESH from the authoritative store (S3), not the conflict-skipped
        # LOCAL mirror — else partner_in_flight is permanently blind to peers
        # (frozen/absent local shards) and cannot prevent a double-claim.
        # Fail-open to the local read; only this consumer pays the S3 cost.
        from _team_state import compose_agent_status, load_rows_authoritative
        agent_status = compose_agent_status(
            state.get("agent_status") or {}, load_rows_authoritative(world_dir))
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

# Conventional commit tag: `type(g-NNN-NN): subject` (the shape
# iteration-commit.sh writes). Strict `):` anchor — only the commit's OWN
# goal tag exempts, never an incidental mid-subject id mention.
_COMMIT_TAG_RE = re.compile(r"\((g-\d+-\d+)\):")


def _lineage_parent_ids(goal):
    """Goal ids that are the proposal's lineage parents: a goal-id-shaped
    ``discovered_by`` plus any ids embedded in ``origin_signal``. Uses the
    greedy _GOAL_ID_RE (defined with the pending-queue lineage block below),
    so a shorter id can never be extracted from inside a longer one (the
    g-315-39-in-g-315-390 prefix class). Used by the git_log_48h lineage
    exemption (g-115-2462 — 5th observed lineage false-positive shape: a
    follow-up filed minutes after its parent closed matches the parent's
    OWN commit, since sq-013/sq-018 follow-ups routinely name files the
    parent just committed)."""
    ids = set()
    disc = (goal.get("discovered_by") or "").strip()
    if disc and _GOAL_ID_RE.fullmatch(disc):
        ids.add(disc)
    for m in _GOAL_ID_RE.finditer(goal.get("origin_signal") or ""):
        ids.add(m.group(0))
    return ids


def _self_completed_ids(world_dir, self_agent):
    """Goal ids from team-state recent_completions completed by the FILING
    agent itself. Consumed by the git_log_48h self-completion demotion
    (g-115-2555): a commit tagged with a goal the filer THEMSELVES completed
    carries no duplicate-information the filer lacks, so the match demotes to
    a visible advisory naming the commit. Partner-completed tags and untagged
    commits are NOT included — they keep hard-blocking, preserving the
    N-agent invariant's cross-agent dup-of-done-work detection. Fail-open to
    empty set (= no demotion) on any read error."""
    if world_dir is None or not self_agent:
        return set()
    try:
        with open(world_dir / "team-state.yaml", "r", encoding="utf-8") as f:
            state = yaml.safe_load(f) or {}
        entries = state.get("recent_completions") or []
    except Exception:
        return set()
    if not isinstance(entries, list):
        return set()
    return {e.get("goal_id") for e in entries
            if isinstance(e, dict) and e.get("completed_by") == self_agent
            and e.get("goal_id")}


def _check_git_log(goal, file_paths, project_root, self_agent="", world_dir=None):
    """Intersect proposed file-paths against all 48h git commits (any author).

    N-AGENT INVARIANT — DO NOT ADD AUTHOR FILTERING to the git SCAN. Scanning
    all commits is correct: it catches overlap with every concurrent
    contributor AND catches self-recent-work (all agents share one git
    identity, so --author filtering would be a no-op anyway). The g-115-2555
    self-completion demotion below does NOT violate this: the scan stays
    author-blind; only match CLASSIFICATION consults completion attribution
    (team-state recent_completions via _self_completed_ids), demoting a match
    to a visible advisory when its commit tag maps to a goal the FILING agent
    itself completed — the filer already knows what they just did, so the
    match carries no duplicate-information (basis: 14-day telemetry, 19 solo
    git_log filing attempts, 9 overridden ALL verified-FP, 0 demonstrated
    TPs; g-115-2554 board msg-20260718-012141). Partner-completed tags and
    untagged commits still hard-block.
    """
    if not file_paths:
        return {
            "name": "git_log_48h",
            "passed": True,
            "reason": "skipped (no file paths in goal text)",
            "matches": [],
        }
    # Completed-Maintain skip (, extended to git_log by 3).
    # A status=completed Maintain goal names framework files touched by its OWN
    # just-shipped commit(s) within 48h — that file-path overlap IS the
    # completion evidence, not duplication. Structural twin of the identical
    # carve-out in _check_target_state (identifiers-present-is-completion, not
    # duplication). Without it, every retroactive Maintain record whose
    # description cites own-session-touched files needs --override-duplication
    # (canonical: /encode-session 2026-07-07 Lane 2 blocked two completed
    # Maintain records; 3).
    if (goal.get("status") == "completed"
            and (goal.get("title") or "").startswith("Maintain:")):
        return {
            "name": "git_log_48h",
            "passed": True,
            "reason": ("skipped (status=completed Maintain goal — 48h commit "
                       "file-path overlap is the completion signal, not "
                       "duplication; g-115-836 / g-115-1813)"),
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
        # Lineage exemption (2): a commit whose conventional goal
        # tag IS one of the proposal's lineage parents (discovered_by /
        # origin_signal-embedded id) is expected file-path overlap — a
        # follow-up filed from a just-closed goal names files that goal just
        # committed. Demote those matches to visible advisories; every other
        # commit match still blocks.
        parent_ids = _lineage_parent_ids(goal)
        self_ids = _self_completed_ids(world_dir, self_agent)
        blocking, lineage, self_done = [], [], []
        for m in uniq:
            tag = _COMMIT_TAG_RE.search(m.get("commit") or "")
            if parent_ids and tag and tag.group(1) in parent_ids:
                lineage.append(dict(m, lineage_exempt=True,
                                    lineage="commit-of-lineage-parent"))
            elif self_ids and tag and tag.group(1) in self_ids:
                # 5: tag maps to a goal the FILING agent completed —
                # demote (see docstring). Checked AFTER lineage so attribution
                # stays most-specific-first.
                self_done.append(dict(m, self_completion_exempt=True,
                                      lineage="commit-of-self-completed-goal"))
            else:
                blocking.append(m)
        demoted = lineage + self_done
        if blocking:
            reason = ("file-path overlap with " + str(len(blocking))
                      + " recent commit touch(es) in 48h")
            if lineage:
                reason += (" (+" + str(len(lineage))
                           + " lineage-parent commit match(es) demoted)")
            if self_done:
                reason += (" (+" + str(len(self_done))
                           + " self-completed-goal commit match(es) demoted)")
            return {
                "name": "git_log_48h",
                "passed": False,
                "reason": reason,
                "matches": blocking[:10],
                "advisories": demoted[:5],
            }
        parts = []
        if lineage:
            parts.append(str(len(lineage)) + " lineage-exempt (own lineage "
                         "parent's commits; g-115-2462)")
        if self_done:
            parts.append(str(len(self_done)) + " self-completion-exempt "
                         "(commits of goals this agent completed; g-115-2555)")
        return {
            "name": "git_log_48h",
            "passed": True,
            "reason": ("passed with all commit match(es) demoted: "
                       + " + ".join(parts)),
            "matches": [],
            "advisories": demoted[:5],
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
    # Completed-Maintain skip (5) — matches the 4 sibling checks
    # (partner_in_flight 7, git_log /3, target_state,
    # pending_queue). A status=completed Maintain filing RECORDS work that
    # already happened; file overlap with an active insight_trigger is a
    # completion/vocabulary coincidence, not a NEW duplicate. Placed before the
    # findings.jsonl read so the skip also saves it. Exact-duplicate RECORDS are
    # still caught by pending_queue Strategy 1.
    if (goal.get("status") == "completed"
            and (goal.get("title") or "").startswith("Maintain:")):
        return {
            "name": "insight_triggers",
            "passed": True,
            "reason": ("skipped (status=completed Maintain goal — active "
                       "insight_trigger file overlap is completion coincidence, "
                       "not duplication; g-115-2685 / g-115-2477 family)"),
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
    # REMOVAL-intent skip () — the mirror inversion of READ-intent:
    # for a retire/remove/delete goal, identifiers present in target files
    # means the removal has NOT happened yet (they ARE the removal target),
    # so presence must not read as duplication. Same shared-classifier
    # contract as is_read_intent (single source of truth in _target_state).
    if is_removal_intent(goal.get("title"), _caller="goal-duplication-gate.py"):
        return {
            "name": "target_state",
            "passed": True,
            "reason": (
                "skipped (REMOVAL-intent goal title — identifiers present "
                "in target files ARE the removal target, not completion "
                "evidence; see _target_state.REMOVAL_INTENT_VERBS / g-248-101)"
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
        # MODIFY-intent DEMOTE (5, echo 6). The named
        # identifiers are the modification SUBJECT — present in the target file
        # both BEFORE and AFTER the change — so a solo target_state block on a
        # modify-verb goal is a subject-not-deliverable FP (71% / 37 of 52 solo
        # overrides since 2026-07-04). Unlike is_read_intent / is_removal_intent
        # (which SKIP because identifier presence INVERTS the completion
        # semantic), modify-presence is AMBIGUOUS, so we DEMOTE rather than skip:
        # keep the match visible (passed=True + matches[].demoted marker) but drop
        # the hard --override-duplication requirement. Placed at the block branch
        # (not an early skip) precisely so the probe runs and the match stays
        # visible as an advisory.
        if is_modify_intent(goal.get("title"), _caller="goal-duplication-gate.py"):
            return {
                "name": "target_state",
                "passed": True,
                "reason": ("advisory-demoted (MODIFY-intent goal — named "
                           "identifiers are the modification SUBJECT, present "
                           "pre- and post-change, not duplication evidence; "
                           "match kept visible but not a hard block; "
                           "_target_state.MODIFY_INTENT_VERBS / g-115-2565): "
                           "target file(s) contain " + str(pr["total_hits"]) +
                           "/" + str(pr["total_identifiers"]) +
                           " identifiers (hit_ratio=" +
                           str(pr.get("hit_ratio", 0.0)) + ")"),
                "matches": [{
                    "verdict": pr["verdict"],
                    "target_files": ex["target_files"],
                    "identifiers": ex["identifiers"],
                    "hit_ratio": pr.get("hit_ratio"),
                    "demoted": "modify-intent",
                    "per_file_hits": [
                        {"file": pf["file"], "hits": pf["hits"][:5],
                         "miss_count": len(pf["misses"])}
                        for pf in pr["per_file"]
                    ],
                }],
            }
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


# Goal ids carry 2-4 digit suffixes (g-NNN-NN..g-NNN-NNNN), so bare substring
# tests prefix-collide across the live corpus ( matches inside
# "idea:1-slug"). Boundary rule: the id must not be followed by
# another digit.
_GOAL_ID_RE = re.compile(r"g-\d+-\d+")


def _id_boundary_match(goal_id, text):
    if not goal_id or not text:
        return False
    return re.search(re.escape(goal_id) + r"(?!\d)", text) is not None


def _lineage_relation(goal, candidate):
    """Return a short label when the pending-queue match `candidate` is the
    proposed goal's own LINEAGE — its discoverer, a declared precondition
    dependency, a same-discoverer sibling, or the goal named inside the
    proposal's origin_signal — else None.

    Lineage goals NECESSARILY share identifiers/keywords with the proposal
    (the description cites the parent by id; the precondition names the
    dependency by design), so a structural overlap against them is expected
    self-reference, not duplicate evidence. Observed false-blocks
    (g-115-2456): three in one echo session — g-315-389 vs its own
    discoverer g-315-388, g-315-391 vs its declared prerequisite g-315-390,
    g-115-2452 vs same-discoverer lane siblings — plus the zeta shape
    g-335-85 vs g-335-73 (the in-progress goal named in its origin_signal).
    Exempt matches are DEMOTED to advisories (still visible), never hidden.
    Strategy 1 (origin_signal EXACT equality) is deliberately unaffected:
    two goals carrying the same symptom key are true duplicates even inside
    a lineage chain.
    """
    cand_id = candidate.get("goal_id") or ""
    if not cand_id:
        return None
    if (goal.get("discovered_by") or "") == cand_id:
        return "discovered_by-parent"
    # Declared dependency: candidate named in verification preconditions
    # (goal_completed_after / goal_id keys, or a bare string mention).
    ver = goal.get("verification")
    if isinstance(ver, dict):
        pres = ver.get("preconditions")
        if isinstance(pres, list):
            for p in pres:
                if isinstance(p, dict):
                    vals = [str(v) for k, v in p.items()
                            if k in ("goal_id", "goal_completed_after")]
                    if any(cand_id == v or _id_boundary_match(cand_id, v)
                           for v in vals):
                        return "precondition-dependency"
                elif isinstance(p, str) and _id_boundary_match(cand_id, p):
                    return "precondition-dependency"
    # Same-discoverer siblings carrying DISTINCT symptom keys: both spawned
    # by one discovery pass, necessarily sharing its vocabulary. Guarded to
    # goal-id-shaped discoverers — free-form values ("user", a pipeline name)
    # would exempt unrelated filings that merely share a producer label.
    disc = goal.get("discovered_by") or ""
    if (disc and _GOAL_ID_RE.fullmatch(disc)
            and disc == candidate.get("discovered_by")):
        po = (goal.get("origin_signal") or "").strip()
        co = (candidate.get("origin_signal") or "").strip()
        if po and co and po != co:
            return "same-discoverer-sibling"
    # Origin-signal-embedded parent: the proposal's origin_signal names the
    # candidate goal id (convention embeds the source goal id, e.g.
    # "idea:-posix-identity-alignment" filed FROM ).
    if _id_boundary_match(cand_id, goal.get("origin_signal") or ""):
        return "origin-signal-lineage"
    return None


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
      - <agents-root>/*/aspirations.jsonl (per-agent queues — ALL agents;
        pending dups are equally bad in any queue). Agents root =
        MIND_AGENTS_ROOT env override (test hermeticity, g-115-2461) else
        _paths.agents_root() (tracks AGENTS_PARENT_DIR — CLAUDE.md
        "Agent-dir Resolution").

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
    # 7: completed-Maintain filings restrict this check to EXACT
    # duplicate-record detection (Strategy 1 + exact title) — see below.
    completed_maintain = (goal.get("status") == "completed"
                          and (goal.get("title") or "").startswith("Maintain:"))

    # Collect source paths. Sorted iteration for deterministic ordering.
    source_paths = []
    if world_dir is not None:
        source_paths.append(("world", world_dir / "aspirations.jsonl"))
    if project_root is not None:
        # MIND_AGENTS_ROOT: sibling of the MIND_WORLD redirect — without it
        # tmp-world test runs swept LIVE agent queues (1). Default
        # routes through _paths.agents_root() instead of hardcoding the
        # literal "agents" segment.
        env_root = os.environ.get("MIND_AGENTS_ROOT", "").strip()
        agents_root = Path(env_root) if env_root else _agents_root()
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
                "discovered_by": (g.get("discovered_by") or "").strip(),
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
    if (proposed_origin and proposed_origin not in _GENERIC_BARE_ORIGINS
            and not proposed_origin.startswith(_SIBLING_SHARED_ORIGIN_PREFIXES)):
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

    # Completed-Maintain restriction (7, extending  /
    # 3). A status=completed Maintain record documents work that
    # already happened; the only real duplicate risk is an exact-duplicate
    # RECORD, not shared vocabulary with pending work (canonical FPs:
    # 6 + 7's own filing + 3, all blocked by
    # structural_overlap on generic tokens / boilerplate / citation
    # identifiers within 24h). Restrict to exact origin_signal (Strategy 1
    # above) + exact-normalized-title match; skip Strategy 2 structural
    # scanning entirely for these filings.
    if completed_maintain:
        norm_title = " ".join((goal.get("title") or "").split()).lower()
        title_matches = []
        for c in candidates:
            if " ".join(c["title"].split()).lower() == norm_title:
                title_matches.append({
                    "source": c["source"],
                    "asp_id": c["asp_id"],
                    "goal_id": c["goal_id"],
                    "title": c["title"][:120],
                    "origin_signal": c["origin_signal"],
                    "match_strategy": "title_exact",
                })
        exact_matches = origin_matches + title_matches
        if exact_matches:
            strategy_summary = ", ".join(sorted(set(
                m["match_strategy"] for m in exact_matches)))
            return {
                "name": "pending_queue",
                "passed": False,
                "reason": ("exact-duplicate completed-Maintain record: " +
                           str(len(exact_matches)) + " match(es) [strategies=" +
                           strategy_summary + ", scanned=" +
                           str(len(candidates)) + "; g-115-2477 carve-out "
                           "restricts completed-Maintain to exact matching]"),
                "matches": exact_matches,
                "advisories": [],
            }
        return {
            "name": "pending_queue",
            "passed": True,
            "reason": ("no exact duplicate (status=completed Maintain goal — "
                       "restricted to exact origin_signal/title match; "
                       "structural strategies skipped: completed records "
                       "cannot race pending work; g-115-2477 / g-115-836 "
                       "family, scanned=" + str(len(candidates)) + ")"),
            "matches": [],
            "advisories": [],
        }

    # Strategy 2: structural overlap mirroring _check_recent_completions.
    # IDF computed over the candidate text corpus so common queue vocab
    # doesn't inflate matches (matches the rare-identifier discipline in
    # the recent_completions check).
    all_terms = set(file_paths) | set(keywords)
    # _compute_idf takes entries with `key_finding` field. Wrap candidate
    # text in that shape so the helper applies symmetrically.
    pseudo_entries = [{"key_finding": c["text"]} for c in candidates]
    idf, idf_n = _compute_idf(pseudo_entries, all_terms) if all_terms else ({}, 0)
    # 5: same per-term IDF floor as _check_recent_completions. The
    # pending corpus is larger (~337) so cluster-common identifiers (,
    # , b9568 — measured df 2-6) carry HIGHER absolute IDF here than in
    # recent_completions; the live-n floor adapts. idf(k) >= floor <=> df<=CEIL.
    idf_floor = (math.log(idf_n / (1 + STRUCT_IDF_DF_CEIL))
                 if idf_n > (1 + STRUCT_IDF_DF_CEIL) else 0.0)

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
        # 5: the structured-token branch ALSO requires per-term IDF
        # >= idf_floor (df(k) <= STRUCT_IDF_DF_CEIL) so cluster-common ids
        # (/ referenced across many pending goals) no longer
        # false-strong-block legit follow-ups (canonical: 4 filing).
        # 1: a PROSE-sourced proposal (no verification block) requires
        # a FILE-PATH co-signal for a HARD structural block — the [_0-9]-keyword
        # branch alone is demoted to advisory. A prose goal that shares a
        # structured keyword-identifier (board_write, git_log_48h, )
        # with a pending goal is DISCUSSING the same topic/incident, not
        # necessarily duplicating the same WORK: the canonical FP is a follow-up
        # that RECAPS its parent's incident (alpha's board-write latency-canary
        # vs its parent , sharing board_write/append_jsonl_record/16ms;
        # file_path_hits EMPTY) or a meta-goal discussing framework vocabulary
        # (1 vs 3 on "git_log_48h"). Prose shares TOPIC
        # identifiers, not work-target files. The [_0-9] co-signal STAYS for
        # VERIFICATION-sourced proposals: verification.outcomes/checks naming an
        # identifier IS an authoritative work-target declaration (), so a
        # shared structured id there is real duplicate evidence. Scoped to
        # pending_queue only (the already-strictest, larger-corpus check where
        # every evidenced FP landed); recent_completions is unchanged (G3). A
        # true prose duplicate that names the same FILE still blocks (P2/P8);
        # one that shares only a topic identifier is demoted to advisory, still
        # surfaced, and still caught by Strategy 1 (origin_signal) when it is a
        # real dup filed under the same symptom key.
        # Bare-basename specificity floor (3, mirroring the git_log
        # 6 floor): a filename with no directory component (SKILL.md,
        # README.md, CLAUDE.md, retrieve.sh) is shared VOCABULARY, not a
        # work-target path — many topically-unrelated goals mention it, so it
        # over-fires as a file-path co-signal (canonical FPs: 6 pending goals
        # matched on "SKILL.md"; 3's own filing false-blocked on
        # "retrieve.sh"). Only a directory-QUALIFIED path (contains "/") is
        # specific enough to be the has_specific co-signal for a HARD block.
        # Bare-filename-only overlap demotes to a visible advisory (still
        # surfaced; a real dup is still caught by Strategy 1 origin_signal + by
        # qualified-path / [_0-9]-identifier co-signals). Bare names still count
        # toward strong (unique_hits/weighted) — only their HARD-BLOCK power is
        # removed.
        qualified_hit_paths = [fp for fp in hit_paths if "/" in fp]
        if source_name == "prose":
            has_specific = bool(qualified_hit_paths)
        else:
            has_specific = bool(qualified_hit_paths) or any(
                re.search(r"[_0-9]", k) and idf.get(k, idf_floor) >= idf_floor
                for k in hit_kws)
        if strong and has_specific:
            # 6: the candidate's own lineage necessarily shares its
            # vocabulary — demote to a visible advisory instead of blocking.
            lineage = _lineage_relation(goal, c)
            if lineage:
                advisories.append({
                    "source": c["source"],
                    "goal_id": c["goal_id"],
                    "unique_hits": unique_hits,
                    "weighted_score": round(weighted, 2),
                    "keyword_hits": hit_kws[:5],
                    "lineage_exempt": lineage,
                })
                continue
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
    advisories.sort(key=lambda a: (not a.get("lineage_exempt"),
                                    not a.get("strong_keyword_only"),
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
        _check_git_log(goal, file_paths, project_root, self_agent, world_dir),
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
