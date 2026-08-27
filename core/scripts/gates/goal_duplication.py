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

# Bound for the git_log_48h walk, which no longer uses `--since` ( /
# guard-4539 — see the argv comment at that call site for why, twice over).
# Measured 2026-08-20 on the live tree: 919 commits in the 48h window (HEAD,
# merges included), so this is ~3.3x headroom; 0.347s vs 1.705s unbounded, and
# this gate runs on every goal filing. Truncation is REPORTED in the check's
# `reason` rather than absorbed, because the whole defect being fixed here is a
# bound that shrank the window in silence.
_DUP_LOG_WALK_MAX = 3000
from _gate_log import log as _gate_log  # type: ignore
from _paths import agents_root as _agents_root  # type: ignore
from _dt import parse_naive_iso  # type: ignore  (shared tzinfo-stripping naive-ISO parse, /)
from _target_state import (  # type: ignore
    _FILE_PATH_RE,
    _resolve_search_roots,
    extract_and_infer_targets,
    is_add_to_surface_intent,
    is_build_or_test_authoring_intent,
    is_code_target_lead_intent,
    is_modify_intent,
    is_read_intent,
    is_removal_intent,
    is_run_intent,
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
    # : generic structural/framework state-vocabulary that recurs
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
    # : generic English VERBS/adverbs (not structural vocab) that
    # recur across unrelated framework-finding goals and are poor duplicate
    # discriminators. Session-93 ground truth: the gate false-blocked FIVE
    # legitimate goals on these ( vs ,  vs
    # , , , drain-temp — all override-cleared, see
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
    # co-signal (_is_structural_identifier, both checks; ) — turning
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
# Strategy 2 (structural keyword/file overlap). ; canonical incidents
#  vs  and  (each needed --override-duplication).
#
# "parent_aspiration:<asp-id>" (added , 2026-07-26) is the SAME shape
# one level up: every goal auto-filed under an aspiration carries its parent's
# id, so the Nth filing exact-matches all N-1 predecessors under that parent.
# Measured on the live world queue: 19 pending/in-progress goals carry a
# parent_aspiration: origin, 18 of them "parent_aspiration:" alone, and
# they are self-evidently distinct deliverables (poll a mailbox, probe
# remote-storage connectivity, reap stale telemetry rows, re-run a benchmark).
# Goals under DIFFERENT parents carry different strings and never collide; a
# TRUE duplicate under one parent still trips Strategy 2, exactly as for
# decomposition siblings (proven by the P8 control).
_SIBLING_SHARED_ORIGIN_PREFIXES = ("decomposition:", "parent_aspiration:")

# Lane-constant origin_signals: values a SKILL.md template mandates VERBATIM, so
# every goal produced by that lane carries the byte-identical string. Same
# false-positive class as _GENERIC_BARE_ORIGINS (a CATEGORY, not a unique symptom
# key) — but these carry a prefix, so the bare-tag test never catches them and
# Strategy-1 exact-match blocks every Nth filing against all N-1 predecessors.
#
# Canonical incident (, 2026-07-26): filing an encode-session Lane 5
# verify-learning candidate hit 25 pending_queue matches, 23 of them via
# origin_signal / origin_signal_completed — every prior sq-018 candidate,
# colliding by construction rather than by subject. The 25 covered entirely
# unrelated surfaces (runner_capabilities, tree-read projection, CRLF parsing,
# git file modes), so the block carried no duplicate signal at all.
#
# Corroborated from two directions: these are the literals hardcoded in SKILL.md
# JSON templates AND (independently) the highest-frequency non-bare-tag values in
# the live queue — idea:fresh-eyes-followup on 40 goals, maintain:sq-018-verify-
# learning on 26. Real duplicates sharing a lane constant are still caught by
# Strategy 2 (structural keyword/file overlap), exactly as for the bare tags.
#
# Regenerate with:
#   grep -rhoE '"origin_signal"[[:space:]]*:[[:space:]]*"[a-z_]+:[a-z0-9_-]+"' \
#     .claude/skills/ | sed 's/.*: *"//; s/"$//' | sort -u
_LANE_CONSTANT_ORIGINS = frozenset({
    "maintain:sq-018-verify-learning",
    "investigate:approval-request-refused",
    "idea:health-ledger-calibration-complete",
    "idea:fresh-eyes-program-followup",
    "idea:fresh-eyes-followup",
})


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


_FILE_EXT_RE = re.compile(r"\.[a-z0-9]+$")


def _is_structural_identifier(token: str) -> bool:
    """True iff `token` is a real work-target IDENTIFIER — the has_specific
    co-signal for a HARD duplicate block — rather than a word-HYPHEN-word
    English/domain compound.

    g-248-117 (fixes the g-248-115 uncovered path): the old ``[-_0-9]`` shape
    test matched a bare HYPHEN, so a generic compound ('own-cloud', 'end-to-end',
    'env-server', 'phantom-world', 'one-button') that is locally rare (clears
    idf_floor) tripped has_specific and let a keyword-only match HARD-block. A
    token is a real identifier iff it carries an UNDERSCORE or a DIGIT (goal-id,
    hash, snake_case symbol: g-321-08, b9568, board_write, loop_state) OR ends in
    a file-extension suffix (script/file name: scorer-override-audit.py). A pure
    hyphen-joined compound has none of these, so it no longer qualifies.

    The ``[_0-9]`` clause (NOT digit-only) deliberately keeps the bare-underscore
    case: _check_pending_queue's existing co-signal relies on snake_case symbols
    (board_write / append_jsonl_record, L1854), and dropping bare underscore
    would silently weaken the gate with no test to catch it. Only the HYPHEN
    needed removing to fix g-248-115 — this is the minimal shape change.
    """
    return bool(re.search(r"[_0-9]", token)) or bool(_FILE_EXT_RE.search(token))


# : a file-path named ONLY in a NEGATIVE / exclusion context
# ("feature-path-excluded for retrieve.sh", "audit ... other than tree-read.sh")
# asserts the OPPOSITE of aboutness. Counting it as a duplicate co-signal
# false-blocks distinct work — canonical incident : 's
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
# ", "complements ", "unlike "). Scanned by
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

    # : an OUTCOME declares what the goal will CHANGE; a CHECK
    # declares how you would CONFIRM it. Only the first is target-file
    # co-signal. A path reachable ONLY through a check is a probe target — the
    # ledger the check reads, the canonical script it shells — and promoting it
    # to full co-signal blocks legitimate goals against every other goal that
    # genuinely touches that file. Measured twice in one boot on 2026-07-27,
    # both false blocks, both needing an audited --override-duplication.
    #
    # THE PERVERSE INCENTIVE IS THE REASON THIS IS WORTH FIXING RATHER THAN
    # OVERRIDING: the better a goal's verification (canonical scripts, real
    # ledgers), the likelier the false block — so the cheapest way to stop being
    # blocked is to write WEAKER verification, or to reach for the override by
    # habit, which erodes the audit value of every override in the ledger.
    #
    # SCOPE CORRECTED FROM THE FILING (measured 2026-08-11, alpha/cc-08, over
    # the 845  goals carrying a non-empty checks list). The goal proposed
    # excluding paths that appear only inside `verification.checks[].command`.
    # That field barely exists: of 1,669 check elements, 1,572 (94.2%) are plain
    # STRINGS and only 22 carry a `command` key at all. Of the 346 goals whose
    # path co-signal comes only from checks, the proposed rule reaches 15 —
    # 4.3% — and leaves 331 untouched. So the exclusion keys on the checks
    # CONTAINER, not on a field inside it, which is also the shape-independent
    # form: it behaves identically whether a check is a string, a dict with
    # `command`, or a dict shape nobody has written yet.
    #
    # FALL BACK WHEN THERE ARE NO OUTCOMES, or the fix inverts into the worse
    # failure. A goal with checks and no outcomes would otherwise lose ALL
    # file-path co-signal and sail past duplicate detection entirely — trading
    # a false block for a false ADMIT, and a false admit is silent.
    _v = goal.get("verification") or {}
    if source_name == "verification" and (_v.get("outcomes") or []):
        _outcome_paths = set(_FILE_PATH_RE.findall(
            " ".join(str(x) for x in (_v.get("outcomes") or []) if x)))
        # Keep only paths the OUTCOMES vouch for. Paths seen solely in checks
        # stay stripped from the keyword text below (they are still in
        # file_paths_all at that point), so a probe path cannot dodge this by
        # re-entering as a keyword stem — the same trap the strip loop below
        # was written to close for exclusion-context paths.
        _vouched_paths = _outcome_paths & file_paths_all
    else:
        _vouched_paths = set(file_paths_all)

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
    # ", "complements ", "unlike ") disclaims
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
    #  candidate (b): a goal-id the goal DECLARES as its own
    # provenance — `discovered_by`, or one cited inside `origin_signal` — says
    # WHERE THE WORK CAME FROM, not what it is ABOUT. Citing the discovering
    # goal is standard practice, and that goal's own closure record names the
    # same id, so the citation MANUFACTURES the overlap it is then blocked on.
    # (That goal's REFUSAL 2: a block vs a goal closed three hours earlier where
    # file_path_hits was EMPTY and the keyword hits were stopwords — after,
    # author, closed, identifier — plus the discovering goal-id itself.)
    #
    # KEYED ON THE GOAL'S OWN FIELDS, NEVER ON PROSE MARKERS, and that is the
    # load-bearing choice rather than an implementation convenience. A prose
    # marker set cannot separate provenance from shared-work here: the
    # neutral-context cases pinned in test_goal_duplication_contrast_goal_id.py
    # read "the sentinel written by " and "the item tracked under
    # " — provenance-SOUNDING phrasing pinned as GENUINE co-signal to
    # preserve structural cases G3/G9. Marker matching would drop exactly those
    # and repeat the candidate-(c) regression this goal already recorded
    # (its outcome_note: a port broke pinned true positive G11). A declared
    # field is unambiguous and is invisible to every prose-only case.
    #
    # NO LINEAGE SIGNAL IS LOST: genuine goal-id RELATIONSHIPS are handled by
    # the dedicated _lineage_relation + Strategy-1 (origin_signal exact-match)
    # paths, as this module's own  note states. This drop narrows only
    # the FUZZY KEYWORD-OVERLAP path, where a provenance id was never evidence
    # of shared subject matter.
    _provenance_ids = set()
    _disc = str(goal.get("discovered_by") or "").strip().lower()
    if _GOAL_ID_RE.fullmatch(_disc):
        _provenance_ids.add(_disc)
    for _m in _GOAL_ID_RE.finditer((goal.get("origin_signal") or "").lower()):
        _provenance_ids.add(_m.group(0))

    keywords = {
        w for w in words
        if w not in _STOPWORDS
        and w not in _provenance_ids
        and not (_GOAL_ID_RE.fullmatch(w)
                 and _path_in_exclusion_context(text_lower, w,
                                                marker_re=_CONTRAST_MARKER_RE))
    }

    # : drop exclusion-context-only paths from the co-signal set.
    # Iterates _vouched_paths, NOT file_paths_all (): this line used
    # to rebuild from file_paths_all and so would have silently DISCARDED the
    # probe-path filter above — the fix would have been a no-op that reads as
    # applied. The strip loop above still uses file_paths_all deliberately, so a
    # probe path is removed from the keyword text too and cannot re-enter as a
    # stem. Any future filter must narrow _vouched_paths, never re-source from
    # file_paths_all.
    file_paths = {fp for fp in _vouched_paths
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


# : a structured identifier (token shape [-_0-9]) counts toward the
# strong-block co-signal ONLY when df(k) <= this ceiling in the IDF corpus —
# i.e. it is rare (unique to the one compared goal), not cluster-common vocab.
# DF_CEIL=1 is empirically forced (guard-594): the FP identifiers that
# false-blocked  (b9568/ df=2,  df=3,  df=6)
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
    # : directive/handoff-routing goals are EXEMPT from this
    # keyword-overlap-vs-COMPLETED check (see _is_directive_routing_goal — the
    # FP class that dropped  and forced --override on ;
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
    # Completed-Maintain skip () — completes carve-out parity with the
    # other fuzzy-overlap checks (partner_in_flight , git_log
    # /1813, target_state, insight_triggers ). A
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
    # : per-term IDF floor for the token-shape strong path (below).
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
        # a hit keyword that is a structured IDENTIFIER — an underscore/digit
        # token (goal-id, snake_case symbol) or a file-name (rb-335,
        # goal_selector). : a bare word-HYPHEN-word compound
        # (own-cloud, end-to-end, env-server) is NOT an identifier — it used to
        # trip has_specific via the old [-_0-9] shape and false-block a
        # keyword-only overlap; _is_structural_identifier now excludes it.
        # Plain words ("summary", "multiplier") are topical noise, not
        # duplicate-work evidence.
        # DO NOT relax this by raising WEIGHT_THRESHOLD or trusting the IDF
        # sum alone: IDF over the ~50-entry recent_completions corpus cannot
        # separate generic vocab from rare ids (2026-05-16: /
        # vs recurring , weighted 5.3/6.4 on PLAIN words). Token
        # shape is the discriminator. Preserves  IDF intent + the
        # rare-identifier path (gate test CASE 3).
        #  refinement: the structured-token branch ALSO requires
        # per-term IDF >= idf_floor (df(k) <= STRUCT_IDF_DF_CEIL). This is a
        # TIGHTENING, not "trusting the IDF sum" — a cluster-COMMON structured
        # identifier (low per-term IDF; appears across many entries) is shared
        # cluster vocab, not duplicate-work evidence. file-path hits unaffected.
        has_specific = bool(hit_paths) or any(
            _is_structural_identifier(k) and idf.get(k, idf_floor) >= idf_floor
            for k in hit_kws)
        # : a recurring/reflection COMPLETION matched on KEYWORDS ONLY
        # (empty hit_paths) with a genuine structured-identifier co-signal is a
        # keyword vacuum — a recurring sweep re-touching the same symbol is not
        # duplicate work. DEMOTE to advisory (stays visible, never HARD-blocks).
        # Scoped to the vacuum case: a recurring completion that shares a real
        # FILE PATH (hit_paths non-empty) still HARD-blocks — genuine
        # shared-work evidence, not a vacuum.
        #  note: the original  trigger was has_specific
        # tripping on a generic hyphenated compound (env-server, end-to-end).
        # Those compounds no longer reach has_specific at all now (they are not
        # structured identifiers), so this branch fires only for a REAL shared
        # identifier (underscore/digit/file-name) in a recurring completion.
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

def _partner_read_caveat(ts_prov, self_agent, composed_peers=None,
                         core_sourced=None):
    """Qualifier appended to BOTH of this check's negative conclusions.

    g-306-158. This check clears a proposal two different ways — "no partners
    in_flight" (empty peer set) and "no blocking overlap" (peers present, none
    matching) — and BOTH are negatives over the peer set, so both are evidence
    only if that set came from the store of record. The second is the common
    exit whenever any partner is working, so qualifying only the first would
    leave the provenance signal unconsulted almost all the time.

    Returns "" when the read was fully authoritative; otherwise a caveat naming
    what was degraded. Never changes passed/failed — a transient store error
    must not block filing (guard-1753 asks the reader to EXPRESS its blindness,
    not to act on it here).

    g-306-179: `degraded` is keyed over the COMPOSED peer set — the set this
    check's negative conclusion is actually ABOUT — not over the raw shard
    rows, which is what ts_prov["by_agent"] enumerates. The two differ in BOTH
    directions:

      * compose_agent_status DROPS retired rows, so keying over rows lets a
        degraded read of a RETIRED peer's shard qualify a clear over a peer
        partner_inflights never examines (false alarm, reachable today).
      * compose_agent_status ADMITS core-file-sourced peers, which have no
        shard and therefore no by_agent key at all. An absent key read as
        clean, so the caveat stayed empty and the reason claimed the store of
        record over a locally-read row — the guard-1753 false positive
        g-306-158 removed, surviving in the half that fix did not cover
        (silent over-claim, latent).

    `composed_peers`/`core_sourced` default to the pre-g-306-179 behavior when
    a caller does not supply them, so the row-keyed reading is still reachable
    but is no longer what the live call site does.
    """
    if not isinstance(ts_prov, dict):
        return ""
    from _team_state import (PROV_AUTHORITATIVE, PROV_LOCAL_MIRROR, PROV_NONE)
    by_agent = ts_prov.get("by_agent") or {}
    core_sourced = set(core_sourced or ())
    peers = set(by_agent if composed_peers is None else composed_peers)
    peers.discard(self_agent)

    def _prov(agent):
        # A composed value taken from the monolithic core file was read with a
        # plain open() and no force_fresh, so it is a local-mirror read no
        # matter how that agent's shard (if any) was obtained. Pessimistic on
        # purpose: an absent label must not read as clean, and the own-cloud
        # overlay can only make this MORE conservative than reality (the
        # deliberately-out-of-scope note on ).
        if agent in core_sourced:
            return PROV_LOCAL_MIRROR
        return by_agent.get(agent, PROV_NONE)

    degraded = sorted(a for a in peers if _prov(a) != PROV_AUTHORITATIVE)
    roster_ok = ts_prov.get("roster") == PROV_AUTHORITATIVE
    if not degraded and roster_ok:
        return ""
    parts = []
    if not roster_ok:
        parts.append("peer roster came from the local mirror, which drops peer "
                     "shards entirely — an in-flight peer may be invisible")
    if degraded:
        parts.append("non-authoritative rows for " + ", ".join(degraded))
    return " — NOT a verified clear: " + "; ".join(parts) + " (g-306-158)"


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
    # Completed-Maintain skip (, extending  / ).
    # A status=completed Maintain filing records work that ALREADY happened —
    # it cannot race a partner's live in_flight goal, so scope overlap with
    # live work is vocabulary coincidence, not a claim conflict (canonical FPs:
    #  + 's own filing, both blocked on generic tokens vs
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
        #  / guard-980: on the own-cloud backend read each peer shard
        # FRESH from the authoritative store (S3), not the conflict-skipped
        # LOCAL mirror — else partner_in_flight is permanently blind to peers
        # (frozen/absent local shards) and cannot prevent a double-claim.
        # Fail-open to the local read; only this consumer pays the S3 cost.
        # : take the provenance-carrying form. This check's clean
        # result is a NEGATIVE over the peer set, which is evidence only if the
        # set was read from the store of record — see the empty branch below.
        # : take the SOURCES-carrying compose. The caveat below must
        # be keyed over this composed set (and must know which of its values
        # came from the un-refreshed core file), not over the shard rows.
        from _team_state import (compose_agent_status_with_sources, SRC_CORE,
                                 load_rows_authoritative_with_provenance)
        rows, ts_prov = load_rows_authoritative_with_provenance(world_dir)
        agent_status, _compose_src = compose_agent_status_with_sources(
            state.get("agent_status") or {}, rows)
        core_sourced = {n for n, s in _compose_src.items() if s == SRC_CORE}
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
        if isinstance(inflight, dict):
            title = inflight.get("title") or ""
            goal_id = inflight.get("goal_id") or ""
            if title or goal_id:
                partner_inflights.append({
                    "agent": agent_name,
                    "goal_id": goal_id,
                    "title": title,
                    "phase": inflight.get("phase"),
                    "claimed_at": inflight.get("claimed_at"),
                })
        # : Body-keyed rows. `in_flight` is REDUCER-OWNED —
        # team-state-in-flight.sh stamps it only when this box's
        # running-session-id equals MIND_SID, and for every OTHER Body it takes
        # a mutually exclusive branch writing `in_flight_bodies.<sid>` instead.
        # So a partner running as a WORKER Body was invisible here, and this
        # check's peer set silently emptied. Measured 2026-08-16 (zeta, hostname
        # cc-02, uname -r 6.8.0-137-generic): the reducer surface carried 1 live
        # claim fleet-wide while the per-Body surface carried 6 more (alpha 5,
        # bravo 1) — this check saw 1 of 7. Earlier the same day it saw 0 of 7.
        # THE EXPOSURE SCALES WITH BODY-PARALLELISM, NOT TIME: a single-Body
        # agent's only Body IS its reducer, so it stamps the reducer surface
        # normally and was always visible. Anyone measuring this fix before/after
        # must segment by how Body-parallel the agents in the window were.
        #
        # Third of the three readers  identified (after
        # goal-pickup-coordination-check.py and the select partner-claim filter);
        # the pattern below is COPIED from that landed fix, not re-derived.
        # Row keys verified against a live record before writing this filter
        # (guard-2559): a body row carries exactly {claimed_at, goal_id, phase,
        # title} — identical to the reducer row, so it maps 1:1 onto the append.
        #
        # Each body is a SEPARATE candidate, never merged: two Bodies of one Mind
        # hold genuinely concurrent claims on DIFFERENT goals, so collapsing them
        # would drop every claim but one (guard-2325 — a derived enumeration does
        # not save a structural check whose key is non-unique).
        #
        # The goal_id guard is deliberately STRICTER than the reducer branch
        # above (which accepts title-or-goal_id). A cleared body row is DELETED
        # as of , so the common case never reaches here — but pre-fix
        # null residue survives on any box that has not yet run a close, and a
        # hand-edit can still leave one (guard-3443: a bare object listing
        # returns every key, and this store holds more than one population).
        bodies = status.get("in_flight_bodies")
        if isinstance(bodies, dict):
            for _sid, body in bodies.items():
                if not isinstance(body, dict) or not body.get("goal_id"):
                    continue
                partner_inflights.append({
                    "agent": agent_name,
                    "goal_id": body.get("goal_id"),
                    "title": body.get("title") or "",
                    "phase": body.get("phase"),
                    "claimed_at": body.get("claimed_at"),
                })

    prov_caveat = _partner_read_caveat(ts_prov, self_agent,
                                       composed_peers=set(agent_status),
                                       core_sourced=core_sourced)

    if not partner_inflights:
        return {
            "name": "partner_in_flight",
            "passed": True,
            "reason": ("no partners in_flight" + (prov_caveat or
                       " (peer set read from the store of record)")),
            "matches": [],
        }

    MIN_UNIQUE_HITS = 2

    # ⚠ READ THIS BLOCK TO "PORTED:" BEFORE CONCLUDING ANYTHING ABOUT CURRENT
    # BEHAVIOUR. It is HISTORICAL: first the defect, then the fix that closed
    # it. The hardening IS landed here (`has_specific` + the demotion branch
    # below), and the FP counts quoted are PRE-hardening evidence that MOTIVATED
    # the port — not a live risk profile. Measured 2026-08-16 (): a
    # reader stopped two lines short of "PORTED:", filed a goal asserting this
    # check was still unhardened, and cited those counts as a current hazard.
    # rb-2059 is the governing rule — verify a gate's DEPLOYED threshold and
    # coverage before quantifying its behaviour.
    #
    # : this was the ONLY one of the three overlap checks that
    # hard-blocked on RAW unweighted overlap. _check_recent_completions
    # (L619-678) and _check_pending_queue (L2047-2058) both gate their strong
    # path on a structural co-signal; that hardening never landed here, so a
    # two-generic-token coincidence against a partner's title hard-blocked a
    # filing (canonical FP:  vs  on "amplify"+"report",
    # file_path_hits: []; second measured FP: two tokens [lambda, mount],
    # guard-2742's 2026-08-11 qualification).
    #
    # WHY THIS CHECK'S FALSE POSITIVES COST THE MOST: a recent_completions FP
    # blocks against FINISHED work and merely annoys; a partner_in_flight FP
    # blocks against LIVE work, so it reads as a claim conflict and agents
    # defer to it instead of overriding. Measured 2026-08-12 (alpha, hostname
    # cc-04, uname -r 6.8.0-137-generic) over meta/gate-firings.jsonl for
    # ts 2026-08-10..12, attributed by the extra.failing_checks field: 53
    # blocks named partner_in_flight as SOLE cause and only 8 were overridden
    # (15%), against 47 sole-cause recent_completions blocks of which 27 were
    # overridden (57%). The unhardened check was trusted ~4x more readily than
    # its hardened sibling, so its FPs are the ones that silently drop work.
    #
    # PORTED: the token-shape co-signal + the per-term IDF floor.
    # DELIBERATELY NOT PORTED: the sibling's aggregate WEIGHT_THRESHOLD (1.5).
    # That threshold is calibrated to corpora of ~50 (recent_completions) and
    # ~337 (pending_queue) entries. This corpus is the partner in_flight titles
    # — measured 1-5. _compute_idf returns idf=0.0 for every term when df==n,
    # so at n=1 (the common single-partner case) `weighted` is exactly 0.0 and
    # a WEIGHT_THRESHOLD test would be permanently False — i.e. mirroring the
    # sibling "exactly" would silently DISABLE this check in its most common
    # configuration. Verified by direct probe before this edit. The token-shape
    # discriminator is corpus-INDEPENDENT and is what the sibling's own comments
    # (L665-670) identify as the real discriminator; the IDF floor self-disables
    # to 0.0 at n<=2 and filters cluster-common tokens at larger n. This is the
    # rb-4385 "mirror the whole discriminator" principle applied with the one
    # term that does not transfer removed on measured grounds, not forgotten.
    all_terms = set(file_paths) | set(keywords)
    # _compute_idf takes entries with a `key_finding` field; wrap the partner
    # titles in that shape, exactly as _check_pending_queue does (L2050).
    _pseudo_entries = [{"key_finding": pi["title"] or ""}
                       for pi in partner_inflights]
    idf, idf_n = (_compute_idf(_pseudo_entries, all_terms)
                  if all_terms else ({}, 0))
    idf_floor = (math.log(idf_n / (1 + STRUCT_IDF_DF_CEIL))
                 if idf_n > (1 + STRUCT_IDF_DF_CEIL) else 0.0)

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
        # HARD block requires a co-signal beyond bare generic vocabulary: a
        # shared file path, a rare structured identifier, OR a compound token
        # (word-HYPHEN-word). Overlaps consisting ONLY of bare plain words are
        # topical noise (guard-2842: read the DISCRIMINATOR, not the verdict).
        #
        # THE COMPOUND CLAUSE IS DELIBERATE AND IS WHERE THIS CHECK MUST DIVERGE
        # FROM ITS SIBLING. _is_structural_identifier EXCLUDES word-HYPHEN-word
        # compounds () because in _check_recent_completions the corpus
        # is ~50 FINISHED goals, where domain compounds (own-cloud, env-server)
        # recur constantly and are therefore cluster-common vocabulary. This
        # corpus is 1-5 LIVE partner titles, where a shared compound means a
        # partner has that exact scope loaded RIGHT NOW. Measured 2026-08-12
        # (alpha, hostname cc-04, uname -r 6.8.0-137-generic) by extracting real
        # tokens through _extract_signals:
        #   TRUE POSITIVE  (this file's own gate test CASE 1, genuine duplicate
        #     work): hits = ['execution-diary', 'observer-session'] — compounds,
        #     _is_structural_identifier False for BOTH.
        #   FALSE POSITIVE ( vs ; and guard-2742's 2026-08-11
        #     [lambda, mount] case): hits = ['amplify', 'lambda', 'report'] —
        #     bare plain words, _is_structural_identifier False for all.
        # A faithful port of the sibling's predicate scores BOTH as non-specific
        # and so DEMOTES THE TRUE POSITIVE — verified by running this file's gate
        # test against that version: CASE 1 and CASE 6 both regressed. Hyphenation
        # separates the two populations cleanly; structural-identifier shape does
        # not separate them at all. Hence the divergence is measured, not stylistic.
        has_specific = bool(hit_paths) or any(
            (_is_structural_identifier(k) and idf.get(k, idf_floor) >= idf_floor)
            or ("-" in k)
            for k in hit_kws)
        if unique_hits >= MIN_UNIQUE_HITS and not has_specific:
            # Vocabulary-only overlap against LIVE work. DEMOTE to advisory:
            # stays visible to the caller (and to guard-2742's route-to-partner
            # judgment) but never hard-blocks the filing.
            advisories.append({
                "agent": pi["agent"],
                "goal_id": pi["goal_id"],
                "unique_hits": unique_hits,
                "keyword_hits": hit_kws[:5],
                "demoted": ("vocabulary-only — no file-path or rare "
                            "structural-identifier co-signal (g-115-3424)"),
            })
        elif unique_hits >= MIN_UNIQUE_HITS:
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

    # : report the demoted count separately from the sub-threshold
    # count. They are different branches and conflating them makes the gate's
    # own output unable to say which one ran (guard-3478) — a vocabulary-only
    # demotion reported as "sub-threshold" would hide the very behaviour this
    # hardening introduces.
    demoted_n = sum(1 for a in advisories if a.get("demoted"))
    subthreshold_n = len(advisories) - demoted_n

    if matches:
        return {
            "name": "partner_in_flight",
            "passed": False,
            "reason": ("overlap with " + str(len(matches)) +
                       " partner in_flight goal(s) [N>=" +
                       str(MIN_UNIQUE_HITS) + " + structural co-signal" +
                       ", source=" + source_name + "]"),
            "matches": matches,
            "advisories": advisories[:5],
            "demoted_count": demoted_n,
        }
    return {
        "name": "partner_in_flight",
        "passed": True,
        "reason": ("no blocking overlap (source=" + source_name +
                   ", " + str(subthreshold_n) + " sub-threshold advisories, " +
                   str(demoted_n) + " vocabulary-only demoted)" +
                   prov_caveat),
        "matches": [],
        "advisories": advisories[:5],
        "demoted_count": demoted_n,
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


def _same_repo_path(fp, line):
    """True when goal-side pattern `fp` and commit-side path `line` denote the
    SAME repo file (or `fp` is a directory containing `line`).

    SYMMETRIC SPECIFICITY FLOOR (g-115-4775). g-115-1166 added a floor to the
    GOAL side only — `"/" in fp` — leaving `line` unconstrained, so a commit
    touching a file literally named `f` matched any goal path containing the
    letter f. Measured 7 false blocks across 4 agents and 4 boxes from one
    0-byte file; the same commit pair kept firing >32h after the file was
    deleted, because the add AND the remove both sit in the 48h window.

    ANCHORED AT A PATH BOUNDARY, NOT BY LENGTH. A length threshold still
    matches any short-but-real filename and needs re-tuning forever. Anchoring
    also keeps this compatible with alpha's live hypothesis
    2026-08-03_git-log-48h-citations-carry-relevant-prior-art, which scopes its
    claim to FULL MULTI-COMPONENT PATH matches: every match that clause admits
    is boundary-aligned, so none is dropped here.

    THE LEADING-DOT ARM IS LOAD-BEARING, and a separator-only anchor is WRONG
    without it. `_FILE_PATH_RE` starts at a word character, so a goal naming
    `.claude/rules/x.md` in prose yields `claude/rules/x.md` with the dot
    stripped. Measured 2026-08-09 over the live corpus (2307 commit touches ×
    11439 goal-path pairs): a separator-only anchor lost 387 matches and ALL
    387 were this class — every one a genuine same-file hit on the
    `.claude/**` tree the fleet edits most. The count alone did not show it;
    reading the hits did (guard-1790).
    """
    if fp == line:
        return True
    if "/" not in fp:
        # Bare basename: exact match only (, unchanged).
        return False
    a = fp.rstrip("/")
    candidates = [line]
    if line.startswith("."):
        candidates.append(line[1:])
    for cand in candidates:
        # Same file named at different depths, either direction: git reports
        # repo-relative paths while goals often name absolute or shortened ones.
        if cand == a or cand.endswith("/" + a) or a.endswith("/" + cand):
            return True
        # Goal names a DIRECTORY; the commit touched a file inside it.
        if cand.startswith(a + "/"):
            return True
    return False


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
    # Completed-Maintain skip (, extended to git_log by ).
    # A status=completed Maintain goal names framework files touched by its OWN
    # just-shipped commit(s) within 48h — that file-path overlap IS the
    # completion evidence, not duplication. Structural twin of the identical
    # carve-out in _check_target_state (identifiers-present-is-completion, not
    # duplication). Without it, every retroactive Maintain record whose
    # description cites own-session-touched files needs --override-duplication
    # (canonical: /encode-session 2026-07-07 Lane 2 blocked two completed
    # Maintain records; ).
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
            # NO `--since` AT ALL ( / guard-4539). This check has now
            # been silently disabled by that flag TWICE, for two different
            # reasons, and the first one cost 15155 firings:
            #   (1) FORMAT — git approxidate rejects the bare unit-letter form
            #       "48h" and returns 0 commits. Fixed in , which
            #       found the check "dead since inception (0/15155 firings)".
            #   (2) TRAVERSAL CUTOFF — `--since` does not FILTER, it STOPS the
            #       walk at the first commit older than the cutoff, so ONE
            #       old-dated commit at the tip hides every recent commit
            #       behind it. Measured on a fixture 2026-08-20: 7 commits 67
            #       SECONDS old returned EMPTY. Non-monotonic commit dates are
            #       ordinary (rebase, cherry-pick, --amend --date, a merged
            #       long-lived branch, peer clock skew).
            # Both fail SILENTLY and in the permissive direction: no overlap
            # found => no duplicate => the goal gets filed. So the window moves
            # onto %ct, parsed below, and the walk is bounded by COUNT instead.
            # --max-count=3000 measured against 919 commits in the live 48h
            # window (HEAD, merges included) — 3.3x headroom, 0.347s versus
            # 1.705s unbounded, and this gate runs on every goal filing.
            ["git", "log", f"--max-count={_DUP_LOG_WALK_MAX}",
             "--name-only", "--pretty=format:COMMIT %ct %H %s"],
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
    # The 48h window, applied HERE on %ct instead of by `--since` in the argv
    # above. `current is None` now means "this commit is outside the window (or
    # we have not seen a COMMIT header yet)", so its file lines are skipped.
    cutoff = int(dt.datetime.now().timestamp()) - 48 * 3600
    n_commits = 0
    oldest_ct = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("COMMIT "):
            n_commits += 1
            rest = line[len("COMMIT "):]
            ts, _, disp = rest.partition(" ")
            try:
                _ct = int(ts)
                in_window = _ct >= cutoff
                if oldest_ct is None or _ct < oldest_ct:
                    oldest_ct = _ct
            except ValueError:
                # Unparseable timestamp: KEEP the commit. This filter replaced
                # a bound whose failure mode was dropping real commits, and
                # silently reproducing that on a bad stamp would re-disable the
                # check a third time. An extra advisory match costs a reader a
                # second look; a missed one files a duplicate goal.
                in_window = True
                disp = rest
            current = disp[:80] if in_window else None
            continue
        if current is None:
            continue
        # sorted() — set iteration is per-process; first-match
        # `goal_file_pattern` must be deterministic.
        for fp in sorted(file_paths):
            # Specificity floor, BOTH sides ( goal-side, 
            # commit-side). Bare basenames match exactly; qualified paths match
            # only at a path-component boundary. See _same_repo_path — the
            # predicate is shared so the floor cannot drift back to one-sided.
            if _same_repo_path(fp, line):
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
        # Lineage exemption (): a commit whose conventional goal
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
                # : tag maps to a goal the FILING agent completed —
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
    # A clean PASS is what authorizes filing, so if the walk was truncated the
    # reason must say the window was incomplete rather than reading as "nothing
    # was there". This check has been silently narrowed twice already; a bound
    # that reports its own ceiling cannot make that three.
    # Hitting the ceiling is NOT by itself truncation: --max-count always
    # returns its full budget on a repo larger than the budget, so a bare
    # `n_commits >= MAX` test fires on EVERY run here (12k+ commits) and trains
    # the reader to ignore it. Measured on the live gate while adding this.
    # Real truncation is running out of BUDGET before running out of WINDOW --
    # i.e. the oldest commit we managed to walk is still inside the 48h window,
    # so older in-window commits exist that we never looked at.
    reason = "no file-path overlap with recent 48h commits"
    if (n_commits >= _DUP_LOG_WALK_MAX
            and oldest_ct is not None and oldest_ct >= cutoff):
        reason += (" [INCOMPLETE: walk hit the --max-count="
                   + str(_DUP_LOG_WALK_MAX) + " ceiling, so commits beyond it "
                   "were not examined — duplicate detection may under-report]")
    return {
        "name": "git_log_48h",
        "passed": True,
        "reason": reason,
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
    # Completed-Maintain skip () — matches the 4 sibling checks
    # (partner_in_flight , git_log /, target_state,
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
                    rec_time = parse_naive_iso(ts)
                    if rec_time is not None and rec_time < cutoff:
                        continue
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
                    rec_time = parse_naive_iso(ts)
                    if rec_time is not None and rec_time < cutoff:
                        continue
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
        # MODIFY-intent DEMOTE (, echo ). The named
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
        # BUILD- / TEST-AUTHORING-intent DEMOTE () — reached only when
        # the title is NOT modify-intent (that branch returns above, so modify
        # takes precedence for a mixed "Fix: ... gate" title). A goal to BUILD a
        # new gate/check/module or ADD an integration test names the EXISTING file
        # it touches; those symbols are the integration SURFACE (present before the
        # work), not the deliverable — the same ambiguity modify-intent handles, so
        # DEMOTE rather than skip. Two 2026-07 filings hard-blocked on this exact
        # FP ( build "goal-creation gate refusing ...",  test
        # "integration test proving ...") and cleared only after re-wording.
        _bt_class = is_build_or_test_authoring_intent(
            goal.get("title"), _caller="goal-duplication-gate.py")
        if _bt_class:
            return {
                "name": "target_state",
                "passed": True,
                "reason": ("advisory-demoted (" + _bt_class + " goal — named "
                           "identifiers are the integration SURFACE the new "
                           "artifact/test touches, present before the work, not "
                           "duplication evidence; match kept visible but not a "
                           "hard block; "
                           "_target_state.is_build_or_test_authoring_intent / "
                           "g-115-2869): target file(s) contain " +
                           str(pr["total_hits"]) + "/" +
                           str(pr["total_identifiers"]) +
                           " identifiers (hit_ratio=" +
                           str(pr.get("hit_ratio", 0.0)) + ")"),
                "matches": [{
                    "verdict": pr["verdict"],
                    "target_files": ex["target_files"],
                    "identifiers": ex["identifiers"],
                    "hit_ratio": pr.get("hit_ratio"),
                    "demoted": _bt_class,
                    "per_file_hits": [
                        {"file": pf["file"], "hits": pf["hits"][:5],
                         "miss_count": len(pf["misses"])}
                        for pf in pr["per_file"]
                    ],
                }],
            }
        #  DEMOTE carve-outs (target_state FP classes 2-3), reached only
        # when the title is neither modify- nor build/test-intent. All three keep
        # the match visible (passed=True + matches[].demoted) but drop the hard
        # --override requirement — the cited identifier is a precondition/surface/
        # edit-target, present pre-work, not duplication evidence.
        # Class 3a — RUN-intent: the named script's presence is a precondition for
        # running it, not run-completion.
        if is_run_intent(goal.get("title"), _caller="goal-duplication-gate.py"):
            return {
                "name": "target_state",
                "passed": True,
                "reason": ("advisory-demoted (RUN-intent goal — the named script's "
                           "presence is a precondition for running it, not "
                           "run-completion; match kept visible but not a hard "
                           "block; _target_state.RUN_INTENT_VERBS / g-248-119): "
                           "target file(s) contain " + str(pr["total_hits"]) + "/" +
                           str(pr["total_identifiers"]) + " identifiers (hit_ratio=" +
                           str(pr.get("hit_ratio", 0.0)) + ")"),
                "matches": [{
                    "verdict": pr["verdict"],
                    "target_files": ex["target_files"],
                    "identifiers": ex["identifiers"],
                    "hit_ratio": pr.get("hit_ratio"),
                    "demoted": "run-intent",
                    "per_file_hits": [
                        {"file": pf["file"], "hits": pf["hits"][:5],
                         "miss_count": len(pf["misses"])}
                        for pf in pr["per_file"]
                    ],
                }],
            }
        # Class 2 — ADD-TO-SURFACE: "add X to/into <cited surface>" integrates a
        # NEW deliverable into an EXISTING surface (the cited identifiers).
        if is_add_to_surface_intent(goal.get("title"), _caller="goal-duplication-gate.py"):
            return {
                "name": "target_state",
                "passed": True,
                "reason": ("advisory-demoted (ADD-TO-SURFACE goal — named "
                           "identifiers are the integration SURFACE the new "
                           "deliverable is added INTO (integration preposition "
                           "present), present before the work, not duplication "
                           "evidence; match kept visible but not a hard block; "
                           "_target_state.is_add_to_surface_intent / g-248-119): "
                           "target file(s) contain " + str(pr["total_hits"]) + "/" +
                           str(pr["total_identifiers"]) + " identifiers (hit_ratio=" +
                           str(pr.get("hit_ratio", 0.0)) + ")"),
                "matches": [{
                    "verdict": pr["verdict"],
                    "target_files": ex["target_files"],
                    "identifiers": ex["identifiers"],
                    "hit_ratio": pr.get("hit_ratio"),
                    "demoted": "add-to-surface",
                    "per_file_hits": [
                        {"file": pf["file"], "hits": pf["hits"][:5],
                         "miss_count": len(pf["misses"])}
                        for pf in pr["per_file"]
                    ],
                }],
            }
        # Class 3b — CODE-TARGET-LEAD: the title LEADS with a code identifier (the
        # file/symbol it operates on), present pre- and post-edit (the LARGEST
        # uncovered class; corrects rb-4732's smallest-tail estimate).
        if is_code_target_lead_intent(goal.get("title"), _caller="goal-duplication-gate.py"):
            return {
                "name": "target_state",
                "passed": True,
                "reason": ("advisory-demoted (CODE-TARGET-LEAD goal — the leading "
                           "code-identifier names the file/symbol being edited, "
                           "present pre- and post-change, not duplication evidence; "
                           "match kept visible but not a hard block; "
                           "_target_state.is_code_target_lead_intent / g-248-119): "
                           "target file(s) contain " + str(pr["total_hits"]) + "/" +
                           str(pr["total_identifiers"]) + " identifiers (hit_ratio=" +
                           str(pr.get("hit_ratio", 0.0)) + ")"),
                "matches": [{
                    "verdict": pr["verdict"],
                    "target_files": ex["target_files"],
                    "identifiers": ex["identifiers"],
                    "hit_ratio": pr.get("hit_ratio"),
                    "demoted": "code-target-lead",
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

def _iter_pending_goals_from_jsonl(jsonl_path: Path, proposed_id: str,
                                   statuses=("pending", "in-progress")):
    """Yield (asp_id, goal) for goals whose status is in `statuses`
    (default: pending/in-progress) from one aspirations.jsonl file. Skips
    the proposed goal if it shares an id with an existing record (idempotent
    re-file). Fail-open on read errors — missing/unreadable files yield
    nothing.

    g-115-3048: _check_pending_queue passes an extended set that also includes
    "completed" so Strategy-1 origin_signal EXACT-match can scan completed
    twins (a finding already converted+completed under an origin_signal must
    block a re-file). All other callers keep the pending-only default."""
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
                    if g.get("status") not in statuses:
                        continue
                    if proposed_id and g.get("id") == proposed_id:
                        continue
                    yield (asp_id, g)
    except Exception:
        return


# Goal ids carry 2-4 digit suffixes (g-NNN-NN..g-NNN-NNNN), so bare substring
# tests prefix-collide across the live corpus ( matches inside
# "idea:-slug"). Boundary rule: the id must not be followed by
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
    # : completed-Maintain filings restrict this check to EXACT
    # duplicate-record detection (Strategy 1 + exact title) — see below.
    completed_maintain = (goal.get("status") == "completed"
                          and (goal.get("title") or "").startswith("Maintain:"))

    # Collect source paths. Sorted iteration for deterministic ordering.
    source_paths = []
    if world_dir is not None:
        source_paths.append(("world", world_dir / "aspirations.jsonl"))
    if project_root is not None:
        # MIND_AGENTS_ROOT: sibling of the MIND_WORLD redirect — without it
        # tmp-world test runs swept LIVE agent queues (). Default
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

    # Collect candidate goals across all sources.
    # Shape per entry: {source, asp_id, goal_id, title, description,
    # origin_signal, text} where text = (title + description).lower().
    # : also collect COMPLETED goals into a SEPARATE list used ONLY
    # for the Strategy-1 origin_signal exact-match below. `candidates` stays
    # pending/in-progress so Strategy-2 structural overlap, the completed-
    # Maintain title match, and lineage checks all keep their original
    # pending-only corpus (fuzzy dedup must not fire against completed work).
    candidates = []
    completed_candidates = []
    for source_label, jp in source_paths:
        for asp_id, g in _iter_pending_goals_from_jsonl(
                jp, proposed_id,
                statuses=("pending", "in-progress", "completed")):
            title = g.get("title") or ""
            description = g.get("description") or ""
            entry = {
                "source": source_label,
                "asp_id": asp_id,
                "goal_id": g.get("id") or "",
                "title": title,
                "description": description,
                "origin_signal": (g.get("origin_signal") or "").strip(),
                "discovered_by": (g.get("discovered_by") or "").strip(),
                "text": (title + " " + description).lower(),
            }
            if g.get("status") == "completed":
                completed_candidates.append(entry)
            else:
                candidates.append(entry)

    if not candidates and not completed_candidates:
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
    # Lane-constant origins (skill-mandated verbatim strings, e.g.
    # "maintain:sq-018-verify-learning") are excluded for the same reason as the
    # bare tags immediately above: they identify the LANE that filed the goal,
    # not the symptom it addresses, so exact-matching them blocks every Nth
    # filing against its N-1 unrelated predecessors ().
    origin_matches = []
    if (proposed_origin and proposed_origin not in _GENERIC_BARE_ORIGINS
            and proposed_origin not in _LANE_CONSTANT_ORIGINS
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
        # : completed twins carrying the same origin_signal (the
        # exact match is precise + low-FP, so it is safe to extend to the
        # completed corpus where fuzzy overlap would not be). A distinct
        # "origin_signal_completed" label tells the caller the twin is DONE
        # — so the right response is verify-before-assuming (git log --grep
        # the finding id, per rb-5047) rather than re-implementing. Incident:
        #  dup'd completed  (both board_post:msg-4248).
        for c in completed_candidates:
            if c["origin_signal"] and c["origin_signal"] == proposed_origin:
                origin_matches.append({
                    "source": c["source"],
                    "asp_id": c["asp_id"],
                    "goal_id": c["goal_id"],
                    "title": c["title"][:120],
                    "origin_signal": c["origin_signal"],
                    "match_strategy": "origin_signal_completed",
                })

    # Completed-Maintain restriction (, extending  /
    # ). A status=completed Maintain record documents work that
    # already happened; the only real duplicate risk is an exact-duplicate
    # RECORD, not shared vocabulary with pending work (canonical FPs:
    #  + 's own filing + , all blocked by
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
    # : same per-term IDF floor as _check_recent_completions. The
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
        # : the structured-token branch ALSO requires per-term IDF
        # >= idf_floor (df(k) <= STRUCT_IDF_DF_CEIL) so cluster-common ids
        # (/ referenced across many pending goals) no longer
        # false-strong-block legit follow-ups (canonical:  filing).
        # : a PROSE-sourced proposal (no verification block) requires
        # a FILE-PATH co-signal for a HARD structural block — the [_0-9]-keyword
        # branch alone is demoted to advisory. A prose goal that shares a
        # structured keyword-identifier (board_write, git_log_48h, )
        # with a pending goal is DISCUSSING the same topic/incident, not
        # necessarily duplicating the same WORK: the canonical FP is a follow-up
        # that RECAPS its parent's incident (alpha's board-write latency-canary
        # vs its parent , sharing board_write/append_jsonl_record/16ms;
        # file_path_hits EMPTY) or a meta-goal discussing framework vocabulary
        # ( vs  on "git_log_48h"). Prose shares TOPIC
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
        # Bare-basename specificity floor (, mirroring the git_log
        #  floor): a filename with no directory component (SKILL.md,
        # README.md, CLAUDE.md, retrieve.sh) is shared VOCABULARY, not a
        # work-target path — many topically-unrelated goals mention it, so it
        # over-fires as a file-path co-signal (canonical FPs: 6 pending goals
        # matched on "SKILL.md"; 's own filing false-blocked on
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
            # : same structured-identifier shape as recent_completions
            # (_is_structural_identifier). Keeps the snake_case/goal-id co-signal
            # ([_0-9], e.g. board_write) and now also recognizes file-names; a
            # bare hyphen-compound never qualified here (this branch was already
            # [_0-9], not [-_0-9]) so behavior for generic compounds is unchanged.
            has_specific = bool(qualified_hit_paths) or any(
                _is_structural_identifier(k) and idf.get(k, idf_floor) >= idf_floor
                for k in hit_kws)
        if strong and has_specific:
            # : the candidate's own lineage necessarily shares its
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
                       " existing goal(s) across world+agent queues"
                       " [strategies=" + strategy_summary +
                       ", source=" + source_name +
                       ", scanned=" + str(len(candidates)) + " pending/in-progress + "
                       + str(len(completed_candidates)) + " completed]"),
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


# --- Check 7: saturated_frontier ---------------------------------------------
#  / guard-2437. A knowledge-tree node can declare its frontier
# SATURATED — "this has been measured enough; another measurement reproduces an
# encoded number". Before this check the declaration lived only in the node BODY,
# so it was reachable only by an agent already reading the node, i.e. one who no
# longer needed it. Five independent agents re-measured
# multi-env-cognitive-load-baseline.md through that gap, and every prior remedy
# added ANOTHER warning to the same unreachable place, which is why N climbed
# instead of converging. This check moves the consultation to goal FILING, which
# is on the path of every skill and every agent.
#
# The marker is `saturated_topics` on the _tree.yaml node — the same durable
# per-node judgment surface `maintain_exempt` / `decompose_exempt` already use,
# settable via `tree-update.sh --set <key> saturated_topics '[...]'`. No new
# parsed surface, and no semantic matcher (explicit NON-GOAL of ).

_SATURATION_MEASURE_VERB_RE = re.compile(
    r"(?:^|\W)(?:re-)?(?:measur|quantif|audit|baselin|benchmark)\w*", re.I)


def _saturation_topic_tokens(text: str) -> set:
    """Tokenize for saturated-topic matching.

    DELIBERATELY NOT the `[a-zA-Z][\\w-]{4,}` regex the rest of this module
    uses. That one requires 5+ characters, which silently drops `load`, `cost`,
    `add` and `next` — precisely the discriminating words in these topic
    phrases. Measured: "cost add next environment" reduces to {environment}
    under the shared regex, which would fire this check on EVERY goal that
    mentions an environment. Short tokens are load-bearing here, so this
    tokenizer keeps them and relies on ALL-tokens-present for precision.

    DO NOT "harmonize" this back to the shared tokenizer. That edit reddens
    tests/test_goal_duplication_gate_saturated_frontier.py
    ::test_partial_topic_overlap_does_not_fire, which exists to catch exactly
    it. The comment above is the WHY; that test is the enforcement (rb-6475 —
    a divergence defended only by prose loses to any mechanical cleanup pass).
    """
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if t not in _STOPWORDS}


def _load_saturated_nodes(world_dir):
    """Yield (node_key, node_file, [topic, ...]) for nodes declaring saturation.

    Fail-open on a malformed tree: this runs as step 7 of evaluate() on EVERY
    goal filing, so an escaping exception here blocks the filing endpoint for
    the whole fleet.

    The three caught classes are the documented failure modes and NOT a blanket
    `except Exception` (guard-373): OSError = tree absent/unreadable, YAMLError
    = torn or invalid YAML, UnicodeDecodeError = corrupt bytes from a torn sync
    write. UnicodeDecodeError is a ValueError, so the original two-class tuple
    did NOT catch it — measured, not theorised.

    The non-mapping case is a STRUCTURAL isinstance guard rather than a caught
    AttributeError, deliberately: `safe_load(...) or {}` only substitutes on
    None, so valid-YAML-that-is-a-list/scalar reached `.get` and raised. Per
    guard-1946, catching that would make a bug in THIS helper indistinguishable
    from the malformed input it guards against.
    """
    if world_dir is None:
        return []
    tree_path = Path(world_dir) / "knowledge" / "tree" / "_tree.yaml"
    try:
        with open(tree_path, "r", encoding="utf-8") as f:
            tree = yaml.safe_load(f)
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return []
    if not isinstance(tree, dict):
        return []
    nodes = tree.get("nodes")
    if not isinstance(nodes, dict):
        return []
    out = []
    for key, meta in nodes.items():
        if not isinstance(meta, dict):
            continue
        topics = meta.get("saturated_topics")
        if isinstance(topics, str):
            topics = [topics]
        if not isinstance(topics, list):
            continue
        clean = [t for t in topics if isinstance(t, str) and t.strip()]
        if clean:
            out.append((key, meta.get("file") or "", clean))
    return out


def _check_saturated_frontier(goal, world_dir):
    """Refuse a MEASUREMENT/AUDIT goal whose topic a tree node declares saturated.

    Two conjuncts, both required — the conjunction is what keeps precision:
      1. every non-stopword token of some declared topic appears in the goal's
         title+description, and
      2. the goal reads as an origination of measurement work (measure /
         quantify / audit / baseline / benchmark).

    KNOWN AND DELIBERATE LIMITATION (measured against the N=5 population that
    motivated this): it catches the three DIRECT measurement filings
    (g-355-83, g-315-527, g-355-56) and NOT the two "steer/rebalance to PRIMARY"
    Idea filings (g-115-3000, g-115-3012). Those two carry no measurement verb
    and none of the topic tokens at filing time — the re-measurement was an
    execution choice made later. Widening conjunct 2 to catch them would fire on
    every steering Idea that mentions the pattern, including the live-ARC work
    the node itself names as the real lever. 3-of-5 at the filing chokepoint is
    the honest reach of a filing-time check; do not "fix" this by loosening the
    verb gate.
    """
    if world_dir is None:
        return {"name": "saturated_frontier", "passed": True,
                "reason": "skipped (no world_dir)", "matches": []}

    blob = f"{goal.get('title') or ''}\n{goal.get('description') or ''}"
    if not _SATURATION_MEASURE_VERB_RE.search(blob):
        return {"name": "saturated_frontier", "passed": True,
                "reason": "no measurement/audit verb in goal text", "matches": []}

    goal_tokens = _saturation_topic_tokens(blob)
    matches = []
    for key, node_file, topics in _load_saturated_nodes(world_dir):
        for topic in topics:
            topic_tokens = _saturation_topic_tokens(topic)
            if topic_tokens and topic_tokens <= goal_tokens:
                matches.append({"node": key, "file": node_file, "topic": topic})
                break

    if not matches:
        return {"name": "saturated_frontier", "passed": True,
                "reason": "no saturated-topic match", "matches": []}

    first = matches[0]
    return {
        "name": "saturated_frontier",
        "passed": False,
        "reason": (
            f"frontier SATURATED — '{first['topic']}' is declared saturated by "
            f"tree node '{first['node']}' ({first['file']}). READ THAT NODE "
            f"before filing: another measurement here reproduces an "
            f"already-encoded number. If the goal genuinely measures something "
            f"new, re-file with --override-duplication naming what is new. "
            f"(g-115-4703 / guard-2437)"
        ),
        "matches": matches,
    }


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
        _check_saturated_frontier(goal, world_dir),
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
