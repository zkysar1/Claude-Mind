# domain-leak-exempt: algorithm comments cite literal test strings
# ("cannot access EFS", "insufficient_session_data") that anchor the
# rb-389 means-vs-ends distinction and the iter-15..17 misroute series.
# Those strings ARE the regression fixtures — genericizing them would
# lose the reasoning anchors future maintainers need.
"""Capability gate logic — daemon-safe extraction (PR 7a/5).

Automated guard against routing infrastructure blockers to the user.
Called by CREATE_BLOCKER before writing `participants: [user]` for an
infrastructure blocker. Scans three canonical sources for agent-
provisionable capabilities and refuses the route when any keyword
matches.

Public API:
    evaluate(failure_reason, *, diagnostic_text, intended_participants,
             override_agent_match, evidence_raw, suggest_unblock,
             for_goal_id, agent_name, world_dir, skills_dir) -> dict

Return shape (matches the legacy CLI's `result` dict verbatim):
    {
      "matches": [...],
      "match_count": int,
      "suggested_routing": "agent"|"unknown",
      "intended_participants": str,
      "override_applied": str|None,
      "evidence_applied": list|None,
      "evidence_logged_to": str|None,
      "approval_kind": "evidence"|"override-agent-match"|None,
      "keywords_extracted": [...],
      "sources_scanned": int,
      "would_block": bool,
      "narrative_framing_detected": bool,
      "narrative_patterns": [...],
      "user_only_preconditions_detected": bool,
      "user_only_precondition_substrings": [...],
      "session_requirement_detected": bool,
      "session_requirement_phrases": [...],
      "session_requirement_classification": str|None,
      "cure_action": str|None,
      "cure_overrides_exemption": bool,
      "reason": str,
      # When suggest_unblock=True and would_block=True:
      "unblock_suggested": True,
      "unblock_title": str,
      "unblock_description": str,
      "matched_capability": {...},
      # When suggest_unblock=True and would_block=False:
      "unblock_suggested": False,
    }

Evidence-error short-circuit (matches legacy CLI verbatim):
    When evidence_raw is provided AND malformed, evaluate() returns the
    3-key shape {would_block: True, evidence_error: str, reason: str}
    AND emits a fail_open _gate_log record. Callers must check for
    `evidence_error` in the result before treating it as a normal verdict.

Side effects (both happen inside evaluate()):
    1. When approval_kind == "evidence" AND bool(matches) AND
       intended_participants == "user": append evidence-approval to
       <world_dir>/blocker-gate-overrides.jsonl via locked_append_jsonl.
       Fail-silent — log failure surfaces on stderr but never propagates.
    2. Always: emit one _gate_log() telemetry record (fail_open on
       evidence error; noop/block/override/pass otherwise).

Daemon safety:
    - Reads no env directly. world_dir / agent_name / skills_dir are
      explicit args.
    - PROJECT_ROOT / SKILLS_DIR defaults computed from __file__ for
      the rare caller that passes None.
    - No argparse, no sys.argv, no sys.exit. Pure function call.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Optional

import yaml  # type: ignore

from _fileops import locked_append_jsonl  # type: ignore
from _gate_log import log as _gate_log  # type: ignore
from _skill_md import parse_front_matter  # type: ignore


# --- Default path constants (caller may override) ----------------------------
# __file__ = core/scripts/gates/capability.py → 4 .parents up = repo root.
_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_SKILLS_DIR = _DEFAULT_PROJECT_ROOT / ".claude" / "skills"


# --- Narrative-framing patterns (g-255-02 / capability-routing.md) -----------
NARRATIVE_PATTERNS = [
    "user approves",
    "user approved",
    "user authorizes",
    "user authorized",
    "waiting for user decision",
    "user-leg scope: approval",
    "user-leg: approval",
    "user must",
    "user needs to",
    "user should",
    "pending user sign-off",
    "pending user review",
    "blocked on user-initiated",
    "blocked on user action",
]


def _match_narrative_patterns(text: str) -> list:
    if not text:
        return []
    lo = text.lower()
    return [p for p in NARRATIVE_PATTERNS if p in lo]


# --- User-only-precondition exemption (g-115-372) ---------------------------
USER_ONLY_PRECONDITION_SUBSTRINGS = [
    "roblox_studio_session_required",
    "studio_session_required",
    "insufficient_session_data",
    "active_sessions=0",
    "player_character_required",
    "player_movement_required",
    "player_keypress_required",
]


def _match_user_only_preconditions(text: str) -> list:
    if not text:
        return []
    lo = text.lower()
    return [s for s in USER_ONLY_PRECONDITION_SUBSTRINGS if s in lo]


# --- User-only-precondition cure registry (g-248-79, 2026-05-06) -------------
USER_ONLY_PRECONDITION_CURES = {
    "insufficient_session_data": (
        "start RUN-mode session via roblox-studio.sh start-session --mode RUN"
    ),
    "active_sessions=0": (
        "start session via roblox-studio.sh start-session"
    ),
    "roblox_studio_session_required": None,
    "studio_session_required":         None,
    "player_character_required":       None,
    "player_movement_required":        None,
    "player_keypress_required":        None,
}


def _resolve_cure_action(user_only_matches: list) -> Optional[str]:
    for precon in user_only_matches:
        cure = USER_ONLY_PRECONDITION_CURES.get(precon)
        if cure:
            return cure
    return None


# --- Session-requirement narrative patterns (g-248-79) -----------------------
SESSION_REQUIREMENT_PATTERN = re.compile(
    r"\b(?:requires?|needs?|blocked\s+on|verification\s+requires?)\s+"
    r"([^.!?\n]{0,80}?)\s+session\b",
    re.IGNORECASE,
)

SESSION_REQUIREMENT_KEYSTROKE_MARKERS = [
    "e-press",
    "epress",
    " f5",
    "f5-play",
    "f5-click",
    "keystroke",
    "keypress",
    "manual equip",
    "manual drop",
    "manual movement",
    "manual ui",
    "contextactionservice",
    "user input",
    "user-input",
    "player input",
    "player-input",
    "player movement",
    "player-movement",
    "player keypress",
    "player-keypress",
    "player character",
    "player-character",
    "player_keypress",
    "player_movement",
    "player_character",
    "character spawn",
    "character-spawn",
    "proximity-trigger",
    "approach-trajectory",
    "player present",
    "player presence",
    "control player",
    "player-presence",
]

SESSION_REQUIREMENT_GAME_INDICATORS = [
    "run-mode",
    "run mode",
    "play-mode",
    "play mode",
    "playmode",
    " run ",
    " play ",
    "game",
    "roblox",
    "studio",
    "npc",
    "bridge",
    "cell",
    "multi-npc",
    "server-only",
    "bridge-driven",
    "smoke-test",
    "smoke test",
    "playtest",
    "datamodel",
    "ohs",
    "behavior",
    "state-replay",
]


def _has_game_indicator(captured_x: str, full_text: str) -> bool:
    text_lower = ((captured_x or "") + " " + (full_text or "")).lower()
    return any(ind in text_lower for ind in SESSION_REQUIREMENT_GAME_INDICATORS)


def _match_session_requirement_patterns(text: str) -> list:
    if not text:
        return []
    out = []
    for m in SESSION_REQUIREMENT_PATTERN.finditer(text):
        captured_x = (m.group(1) or "").strip()
        if not _has_game_indicator(captured_x, text):
            continue
        out.append((m.group(0), captured_x))
    return out


def _classify_session_requirement(captured_x: str, full_text: str) -> str:
    text_lower = ((captured_x or "") + " " + (full_text or "")).lower()
    for marker in SESSION_REQUIREMENT_KEYSTROKE_MARKERS:
        if marker in text_lower:
            return "user_keystroke_required"
    return "agent_provisionable"


# --- Source parsers ---------------------------------------------------------

def _load_forged_skills(world_dir) -> list:
    if world_dir is None:
        return []
    path = world_dir / "forged-skills.yaml"
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        skills_map = data.get("skills") or {}
        return [{
            "source": "forged-skills.yaml",
            "skill": name,
            "triggers": list((meta or {}).get("triggers") or []),
            "scripts": list((meta or {}).get("companion_scripts") or []),
        } for name, meta in skills_map.items()]
    except Exception:
        return []


def _load_skill_md_triggers(skills_dir) -> list:
    if skills_dir is None or not skills_dir.is_dir():
        return []
    result = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        md = skill_dir / "SKILL.md"
        if not md.is_file():
            continue
        fm = parse_front_matter(md)
        if not fm:
            continue
        result.append({
            "source": ".claude/skills",
            "skill": skill_dir.name,
            "triggers": list(fm.get("triggers") or []),
            "scripts": [],
        })
    return result


def _load_capability_routing(world_dir) -> list:
    if world_dir is None:
        return []
    path = world_dir / "conventions" / "capability-routing.md"
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return []
    rows = []
    in_section = False
    seen_divider = False
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped.lower().startswith("## agent-provisionable")
            seen_divider = False
            continue
        if not in_section:
            continue
        if not stripped.startswith("|"):
            continue
        if set(stripped.replace("|", "").replace("-", "").replace(":", "").strip()) <= set(" "):
            seen_divider = True
            continue
        if not seen_divider:
            continue
        rows.append({
            "source": "capability-routing.md",
            "row": stripped,
        })
    return rows


# --- Keyword extraction + matching ------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*")
_PORT_RE = re.compile(r"\b\d{4,5}\b")
# g-001-254: ISO dates/timestamps in failure_reasons (origin_signal carries
# "2026-05-31T..." etc.) would otherwise have their 4-digit year matched by
# _PORT_RE below as a spurious "port" keyword, producing false capability
# matches and false-positive HIGH Unblock goals. Strip them first. A real port
# (4-5 bare digits) never matches this dd-dd-dd-shaped pattern, so legit
# port-keyword extraction is unaffected.
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2})?\b")

# Stopwords — generic tokens too general to discriminate. DO NOT add
# discriminative infra terms here; that silently breaks detection.
_STOPWORDS = {
    "the", "and", "for", "but", "not", "you", "are", "with", "this", "that",
    "was", "has", "can", "all", "out", "any", "see", "via", "one", "two",
    "our", "their", "them", "when", "from", "have", "been", "does", "than",
    "what", "will", "should", "would", "could", "there", "these", "those",
    "into", "upon", "its", "his", "her", "who", "why", "how",
    "run", "get", "set", "log", "use", "need", "make", "open", "start",
    "stop", "close", "show", "tell", "find", "call", "read", "write",
    "check", "test", "try", "fix", "build", "pass", "file", "data", "path",
    "code", "line", "rule", "step", "time", "work", "case", "name", "part",
    "query", "item", "list", "load", "save", "side", "role",
    "port", "server", "service", "system", "health", "status", "host",
    "url", "endpoint", "process", "script", "application", "binary", "disk",
    "user", "agent", "session", "game",
    "api",
    "error", "issue", "problem", "fails", "failed", "broken", "down", "dead",
    "stuck", "blocked", "hang", "timeout",
    "exit", "exited", "fail", "failure", "errno", "exception", "exceptions",
    "traceback", "stderr", "stdout", "return", "returned", "returns",
    "raise", "raised", "throw", "thrown", "caught", "abort", "aborted",
    "crash", "crashed", "panic", "panicked", "fatal", "warning", "warn",
    "new", "old", "create", "created", "update", "updated", "delete", "deleted",
    "add", "added", "remove", "removed", "change", "changed", "modify", "modified",
    "reset", "rotate", "rotated", "refresh", "refreshed", "restart", "restarted",
    "replace", "replaced", "keep", "kept", "same", "different", "current",
    "previous", "next", "prior", "latest", "initial", "final",
    "pre", "post", "sub", "non",
    "exe", "sh", "py", "js", "ts", "md",
    "access", "fresh", "live", "cannot", "still", "present", "presence",
    "pending", "awaiting", "required", "requires", "needs", "active",
    "control", "deployment", "evaluation", "accumulated", "meaningful",
    # g-001-255: generic analysis verbs are non-discriminative; without these,
    # "evaluate" leaks into keyword matching (it is in _IMPERATIVE_VERBS for
    # title-gen, NOT here) and false-matched analyze-npc-behavior. The noun
    # "evaluation" was already a stopword; the verb forms were not.
    "evaluate", "evaluated", "evaluating",
    "before", "after", "only", "first", "last", "then", "stale",
    "must",
    # g-115-1791: generic infra terms that leak into keyword matching from
    # own-cloud write-fence defer_reasons ("own-cloud pipeline writes fenced;
    # reads work, writes fail; write_conflict") and false-match domain
    # capability rows. "writes"/"reads" are the plural-leak of the already-
    # present singular "write"/"read" above; each is a non-discriminative verb
    # in domain prose (game-session row "NPC memory writes"; PLAY-mode row
    # "reads Player presence") whose row retains other identifying tokens
    # (npc/memory/session; play-mode/bridge/player/datamodel). "pipeline" is a
    # generic infra noun in the same class as the already-present "process"/
    # "script"/"system" — it collides across the behavioral-analysis row
    # ("OHS scoring pipeline", which still matches on ohs/scoring/analyze),
    # the framework hypothesis pipeline, and pipeline-*.sh companion scripts,
    # i.e. "too general to discriminate". Evidence: g-001-317 spurious Unblock
    # auto-filed 2026-07-05 when g-001-02 was deferred on the own-cloud fence.
    "writes", "reads", "pipeline",
    # g-115-2070: "own-cloud" is the same fence-leak class as g-115-1791's
    # writes/reads/pipeline — a backend-NAME noun that leaks from an own-cloud
    # write-fence defer ("precondition_unmet: own-cloud ... fenced;
    # write_conflict"). It survived extraction as a NON-matching token until the
    # forged skill 'probe-governed-store' (zeta, 2026-07-11) registered trigger
    # "verify write propagated to own-cloud" — then a legitimately-FENCED
    # own-cloud defer false-matched that skill on the SOLE token 'own-cloud'
    # (would_block=True; test_fence_defer_does_not_falsely_block regressed).
    # Safe per the line-348 rule: 'own-cloud' is non-actionable prose (a backend
    # identifier, not a verb), and probe-governed-store keeps its discriminating
    # tokens (governed/store/s3-authoritative/backend-cat/s3/probe/propagated/
    # sharded/mirror/drift) — recall proven by the adjacent-to-stopword control
    # in test_capability_gate_fence_stopwords.py (guard-958).
    "own-cloud",
    # g-115-1882: tokens that appear ONLY as incidental PROSE or as the
    # section/category descriptor in capability-routing.md rows -- never as a
    # capability identifier -- so extracting them only yields prose-collision
    # false matches (the g-115-1848 defer -> spurious g-115-1881). Safe per the
    # line-348 rule: none discriminates a real capability. rb-2993.
    #   - "goal"/"idle": bounded-config row "named in the goal"; game-session
    #     row "idle Player object".
    #   - "provisionable"/"agent-provisionable": the "## Agent-Provisionable"
    #     section descriptor, repeated in row prose ("Agent-provisionable for
    #     X"). Matching it INVERTS a negated defer_reason -- "the fleet is NOT
    #     agent-provisionable" was flagged AS matching an agent-provisionable
    #     capability (the gate ignores the "not"). It never names an action.
    "goal", "idle", "provisionable", "agent-provisionable",
    # g-115-1885: boolean literals appear as config-VALUE fragments in row prose
    # (game-session row "plugin_connected: true") and in defer_reasons naming a
    # flag ("cross_agent_surfacing.enabled=true" -> the tokenizer splits off
    # "true"). A boolean literal NEVER names a capability -- it is pure prose/
    # value. Without this the g-115-1848 fleet defer's "=true" false-matched
    # "plugin_connected: true" in the RUN-mode game-session row -> spurious
    # Unblock g-115-1884. Boolean-literal class (closed): true + false. rb-2996.
    "true", "false",
    # g-115-1987: "own-cloud" is the STORAGE-LAYER name — an infrastructure
    # noun in the same class as the already-present "process"/"system"/
    # "pipeline", never an action. It appears in nearly every CAS-fence /
    # write_conflict / sync defer_reason (the g-115-1791 fence narrative
    # itself contains it) AND, since the probe-governed-store forge
    # (g-115-1982, 2026-07-11), in that skill's trigger "verify write
    # propagated to own-cloud" — so the token alone false-matched a READ
    # probe skill as the "cure" for a WRITE fence (a probe cannot cure a
    # fence). The skill's rows retain discriminating tokens (backend-cat,
    # governed, s3, mirror, drift, sharded, authoritative), so legitimate
    # detection is preserved.
    "own-cloud",
    # g-115-2336: markdown-table STRUCTURAL vocabulary + prose function words
    # that appear in capability-routing.md row TEXT only incidentally — never
    # as a capability identifier — yet co-occur in defer_reasons often enough
    # to form 2+-token overlaps that BYPASS the g-248-105 sole-token
    # distinctiveness rule (multi-token matches always survive by design; the
    # rule assumed each shared token carries signal, which structural/function
    # words do not). Two junk auto-Unblocks in one day (2026-07-16): echo's
    # g-115-2269 defer ("registry entry must exist...") matched 'row'/'entry'
    # against the PLAY-mode and bounded-config-tune rows -> skipped g-115-2329;
    # foxtrot's g-350-21 same shape. Live repro: 'because'+'row'+'play-mode'
    # survived as a 3-token match on text referencing no capability.
    #   - "row"/"entry"/"table": table-structure words ("see Genuinely
    #     Human-Only row", "agent-provisionable table above", "registry entry").
    #   - "because": conjunction, quoted inside the PLAY-mode row's log message.
    #   - "verified"/"evidence": verification-note prose ("Verified g-273-02
    #     spike", "explicit empirical evidence"). Distinct tokens from the
    #     imperative "verify" (_IMPERATIVE_VERBS), which stays matchable —
    #     inflected narration is not a capability request (rb-2996).
    # Safe per the line-348 rule: every colliding row retains its true
    # discriminators (roblox-studio.sh/start-session/play-mode/player;
    # bounded-config/tune compounds) — recall proven by the adjacent-to-
    # stopword control in test_capability_gate_table_token_noise.py (guard-958).
    "row", "entry", "table", "because", "verified", "evidence",
}


_IMPERATIVE_VERBS = {
    "commit", "push", "deploy", "build", "release", "publish", "rollback",
    "merge", "rebase", "tag",
    "restart", "start", "stop", "kill", "spawn", "reconnect", "reload",
    "resume", "pause",
    "configure", "install", "uninstall", "upgrade", "downgrade", "provision",
    "register", "deregister", "enable", "disable",
    "run", "execute", "trigger", "invoke", "launch", "dispatch", "fire",
    "sync", "fetch", "pull", "upload", "download", "transfer", "copy",
    "import", "export", "migrate",
    "monitor", "probe", "validate", "verify", "audit", "investigate",
    "diagnose",
    "compose", "generate", "produce", "emit", "extract", "parse",
    "process", "analyze", "scan", "review", "evaluate",
    "clean", "purge", "archive", "rotate",
    "delete", "remove", "save", "edit", "modify", "load", "restore",
    "create", "add", "set", "unset",
}


# g-115-2583 / rb-3955: a small curated set of tokens that are BOTH in
# _IMPERATIVE_VERBS AND commonly used as an ADJECTIVE modifying a following
# noun ("clean session" = a fresh/idle session, NOT "clean the session"). When
# such a token is used adjectivally it is not the requested action verb, so the
# action-verb extractor in evaluate() must skip it. Kept intentionally MINIMAL
# and evidence-driven (only what a documented incident showed): a general
# "verb directly followed by a noun is adjectival" rule is WRONG -- it would
# reject genuine verb+object requests like "deploy production" / "restart
# service" / "push origin". Add a member only when a real incident shows that
# token mis-parsing. Origin: g-250-192 filed a spurious "Unblock: clean for
# g-X" from "clean session".
_ADJECTIVE_VERBS = {"clean"}

# Function words that, when they immediately FOLLOW an _ADJECTIVE_VERBS token,
# signal genuine VERB use ("clean the cache", "clean up the logs") rather than
# adjectival use ("clean session"). A following bare noun (not in this set)
# signals adjectival use.
_ADJ_VERB_FOLLOW_FUNCWORDS = {
    "the", "a", "an", "this", "that", "these", "those", "it", "them", "us",
    "up", "out", "off", "away", "all", "and", "or", "then", "before", "after",
    "until", "via", "again", "now", "everything",
}


def _is_adjectival_use(text: str, match) -> bool:
    """True if the _ADJECTIVE_VERBS token at `match` is used adjectivally
    (directly modifying a following noun) rather than as an imperative verb.

    Heuristic: look at the word immediately following the token. If it is a
    function word (article/particle/conjunction) the token reads as a VERB
    ("clean the session" / "clean up") -> return False (keep as action verb).
    If it is a bare noun the token reads as an ADJECTIVE ("clean session")
    -> return True (reject as action verb). Fails toward KEEPING the verb
    (returns False) when nothing follows -- conservative, matching the gate's
    fail-open bias (guard-958). g-115-2583 / rb-3955.
    """
    tail = text[match.end():]
    nxt = _TOKEN_RE.search(tail)
    if not nxt:
        return False  # nothing after -> treat as verb
    return nxt.group(0).lower() not in _ADJ_VERB_FOLLOW_FUNCWORDS


# --- Context-aware keyword disqualification ---------------------------------

_VIA_END = re.compile(r"\b(via|through|by|using|with)\b\s*\S*\s*$")
_NEGATION_ANYWHERE_IN_PRE = re.compile(
    r"\b(cannot|can['\u2019]?t|won['\u2019]?t|unable|no\s+way|not)\b"
)
_BEFORE_GERUND_END = re.compile(r"\bbefore\s+\w+ing\b[^.!?\n]*$")
_COUNT_BEFORE_END = re.compile(
    r"\b(?:requires?|needs?|only|few|several|minimum|maximum|max|min|"
    r"at\s+least|up\s+to)\s+\d+[\-+]?\d*\b[^.!?\n]{0,30}$"
)
_UNIT_AFTER_START = re.compile(
    r"^\s*(invocations?|runs?|executions?|triggers?|attempts?|events?|"
    r"cycles?|iterations?|calls?)\s+"
    r"(first|required|needed|before|until|must)\b"
)

# g-115-1882: a word that is BOTH a common noun AND an imperative verb
# (probe, monitor, audit, review, scan) used as a NOUN whose evidence is being
# REPORTED ("fresh probe 2026-07-09 shows X", "the audit found Y") describes a
# past observation, not a requested action -- so it must NOT count as a
# capability-invocation signal. Keyed on a clear past-evidence/reporting verb
# within 2 words AFTER the keyword. Safe against action requests: "deploy the
# build" / "needs a fresh deploy" have no following evidence verb, so they still
# match. Ambiguous verbs (report/return) are deliberately EXCLUDED -- "probe the
# service and report status" is a genuine action pair, not reported evidence.
# This extends the g-115-1872 guard (which only SUPPRESSED the Unblock for
# verbLESS matches; a verb-noun used as a noun kept matching + filing). rb-2993.
# g-115-1883: INFLECTED report forms ONLY (shows/showed/showing, finds/found,
# confirms/confirmed, ...) -- NEVER the bare base forms (show/find/confirm/
# reveal/indicate/demonstrate/suggest), which are IMPERATIVES in compound action
# requests ("deploy the service. Confirm health", "push and show the team").
# Matching the bare form there wrongly stripped a genuine provisionable keyword
# -> false-negative in a safety gate (fresh-eyes review a6e3fd81 confirmed).
# Evidence-reporting English never writes bare "show" (it writes "shows"/
# "showed"/"showing"), so the original "probe ... shows ..." incident still
# matches the inflected "shows". rb-2996, guard-958.
_EVIDENCE_VERB_AFTER = re.compile(
    r"^\W*(?:\S+\s+){0,2}"
    r"(shows|showed|showing|finds|found|reveals|revealed|"
    r"indicates|indicated|confirms|confirmed|demonstrates|demonstrated|"
    r"suggests|suggested)\b"
)

# g-115-2583 / rb-3955: causal-relevance disqualifiers. An INCIDENTAL keyword
# whose surrounding narrative asserts the referent is AVAILABLE / not-the-block
# (the defer says the capability was probed-fine and is NOT the actual blocker),
# or merely names it as the LOCATION where some OTHER thing is absent, is not a
# capability-invocation signal. These are false-negative-SAFE by construction:
# a genuine block asserts the OPPOSITE state (unreachable / down / cannot-access
# / "is not available"), which these patterns do NOT match -- so they can never
# strip a real "X is the blocker" match (the guard-958 concern). Both are scoped
# to the immediate window around the keyword and only skip THAT occurrence
# (fail-open across occurrences, like the checks above).
#
# _AVAILABILITY_AFTER: keyword FOLLOWED by an availability assertion. The
# leading (is|was|probed|...)? optional group deliberately does NOT include
# "not", so "efs is not available" / "efs is not reachable" fall THROUGH (the
# alternation never matches "not ...") and remain genuine matches.
_AVAILABILITY_AFTER = re.compile(
    r"^\W*(?:is|was|are|were|been|remains?|stays?|still|now|probed|tested|"
    r"verified|confirmed)?\s*(?:as\s+|been\s+)?"
    r"(available|reachable|healthy|online|accessible|"
    r"operational|responsive|up\s+and\s+running|"
    r"not\s+the\s+(?:block|blocker|issue|problem|cause|bottleneck))\b"
    # g-115-2583 fresh-eyes (self-review): 'mounted'/'connected' deliberately
    # EXCLUDED — they are ambiguous ("efs mounted read-only, cannot write" /
    # "efs connected to the wrong endpoint" describe a limited/misconfigured
    # state that CAN be the genuine blocker). guard-958: when a disqualifier
    # member is ambiguous, fail toward matching. The unambiguous members above
    # fully cover the rb-3955 "probed available" case.
)
# _ABSENCE_LOCATION_BEFORE: an ABSENCE word + a location preposition immediately
# PRECEDE the keyword ("config absent on efs", "file deleted from efs") -- the
# keyword is the healthy container, the absent thing is elsewhere. Requiring
# BOTH an absence word AND a trailing location preposition keeps genuine
# requests safe: "cannot access efs" / "unable to mount efs" / "fetch from efs"
# have no absence word before a location prep, so they still match.
_ABSENCE_LOCATION_BEFORE = re.compile(
    r"\b(absent|missing|not\s+found|gone|removed|deleted|empty|"
    r"no\s+longer\s+(?:present|there|available))\b"
    r"[^.!?\n]{0,20}\b(?:on|in|at|under|from|inside)\s*$"
)


def _keyword_is_invocation_signal(text_lower: str, keyword: str) -> bool:
    """True if `keyword` has at least one valid-context occurrence.
    Fail-open — returns True when no occurrence found (defensive)."""
    kw = keyword.lower()
    pattern = re.compile(r'\b' + re.escape(kw) + r'\b')
    occurrences = list(pattern.finditer(text_lower))
    if not occurrences:
        return True

    for m in occurrences:
        start, end = m.span()
        pre = text_lower[max(0, start - 60):start]
        post = text_lower[end:end + 40]

        # CRITICAL: keep AND condition. "cannot access EFS" (ends) must
        # still match access-efs-data — only "cannot X via Y" (means) is
        # disqualified. See rb-389 means-vs-ends distinction.
        if (_VIA_END.search(pre)
                and _NEGATION_ANYWHERE_IN_PRE.search(pre)):
            continue
        if _BEFORE_GERUND_END.search(pre):
            continue
        if _COUNT_BEFORE_END.search(pre):
            continue
        if _UNIT_AFTER_START.match(post):
            continue
        # g-115-1882: verb-noun used as a reported-evidence subject, not an
        # action request ("fresh probe 2026-07-09 shows X", "the audit found Y").
        if kw in _IMPERATIVE_VERBS and _EVIDENCE_VERB_AFTER.search(post):
            continue
        # g-115-2583: incidental keyword whose narrative asserts the referent is
        # available / not-the-blocker, or names it as the location of some OTHER
        # absent thing. False-negative-safe (genuine blocks assert the opposite
        # state; see the pattern comments). rb-3955.
        if _AVAILABILITY_AFTER.match(post):
            continue
        if _ABSENCE_LOCATION_BEFORE.search(pre):
            continue

        return True

    return False


def _filter_context_disqualified(text: str, keywords: set) -> set:
    if not text or not keywords:
        return keywords
    text_lower = text.lower()
    return {kw for kw in keywords
            if _keyword_is_invocation_signal(text_lower, kw)}


def _load_noise_phrases(world_dir) -> list:
    """Load <world>/conventions/capability-gate-noise-phrases.yaml.

    CRITICAL: sort longest-first. _strip_noise_phrases applies in list
    order; without this, "BussedIn" processed before "BussedIn-PPE"
    fragments the longer phrase to "-PPE" which tokenizes as "PPE".
    """
    if world_dir is None:
        return []
    path = world_dir / "conventions" / "capability-gate-noise-phrases.yaml"
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    raw = data.get("phrases") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        s = entry.strip()
        if s:
            out.append(s)
    out.sort(key=len, reverse=True)
    return out


def _strip_noise_phrases(text: str, phrases: list) -> str:
    if not text or not phrases:
        return text
    out = text
    for phrase in phrases:
        out = re.sub(re.escape(phrase), " ", out, flags=re.IGNORECASE)
    return out


def _extract_keywords(text: str, noise_phrases: list = None) -> set:
    if not text:
        return set()
    if noise_phrases:
        text = _strip_noise_phrases(text, noise_phrases)
    # g-001-254: drop ISO dates/timestamps before port extraction (see _ISO_DATE_RE).
    text = _ISO_DATE_RE.sub(" ", text)
    keywords = set()
    for m in _PORT_RE.finditer(text):
        keywords.add(m.group(0))
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0).lower()
        if len(tok) < 3:
            continue
        if tok in _STOPWORDS:
            continue
        keywords.add(tok)
    return keywords


def _entry_tokens(entry: dict) -> set:
    parts = []
    if "skill" in entry and entry["skill"]:
        parts.append(str(entry["skill"]))
    for t in entry.get("triggers", []) or []:
        parts.append(str(t))
    for s in entry.get("scripts", []) or []:
        parts.append(str(s))
    if "row" in entry:
        parts.append(str(entry["row"]))
    blob = " ".join(parts).lower()
    toks = set()
    for m in _PORT_RE.finditer(blob):
        toks.add(m.group(0))
    for m in _TOKEN_RE.finditer(blob):
        toks.add(m.group(0).lower())
    return toks


# --- Evidence-based approval -------------------------------------------------

_VALID_EVIDENCE_TYPES = {"rb", "pipeline", "metric", "goal", "guardrail",
                         "tree", "experience"}


def _parse_evidence(raw: str) -> tuple:
    """Parse evidence JSON. Returns (valid_entries, error_message)."""
    if not raw:
        return [], "empty evidence argument"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return [], f"evidence is not valid JSON: {e}"
    if not isinstance(data, list):
        return [], "evidence must be a JSON array"
    valid = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            return [], f"evidence[{i}] is not an object"
        etype = str(entry.get("type", "")).strip().lower()
        eid = str(entry.get("id", "")).strip()
        eclaim = str(entry.get("claim", "")).strip()
        if etype not in _VALID_EVIDENCE_TYPES:
            return [], (f"evidence[{i}].type='{etype}' not in allowed set "
                        f"{{{','.join(sorted(_VALID_EVIDENCE_TYPES))}}}")
        if not eid:
            return [], f"evidence[{i}].id is missing or empty"
        if not eclaim:
            return [], f"evidence[{i}].claim is missing or empty"
        valid.append({"type": etype, "id": eid, "claim": eclaim})
    if not valid:
        return [], "evidence array is empty"
    return valid, None


def _log_evidence_approval(world_dir, agent_name: str, failure_reason: str,
                            evidence: list, intended_participants: str,
                            top_matches: list) -> Optional[str]:
    """Append evidence-approval to world/blocker-gate-overrides.jsonl.
    Fail-silent on write errors (stderr message; gate proceeds).

    SHARED LEDGER: same path as `gates/blocker_create.py:_log_override`.
    Distinguish via the `gate` field — this writes "capability-gate";
    blocker-create's records omit `gate` entirely. Consumers must filter
    by presence/value of `gate` to separate the two."""
    if world_dir is None:
        print("[capability-gate] WARN: evidence approval granted but not logged "
              "(no WORLD_PATH resolved).", file=sys.stderr)
        return None
    log_path = world_dir / "blocker-gate-overrides.jsonl"
    try:
        record = {
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "agent": agent_name or "unknown",
            "gate": "capability-gate",
            "action": "evidence-approval",
            "intended_participants": intended_participants,
            "failure_reason": (failure_reason or "")[:200],
            "evidence": evidence,
            "matched_capabilities": [
                (m.get("skill") or (m.get("row") or "")[:80])
                for m in (top_matches or [])[:3]
            ],
        }
        locked_append_jsonl(str(log_path), record)
        return str(log_path)
    except Exception as e:
        print("[capability-gate] WARN: evidence-approval log append failed: "
              + str(e), file=sys.stderr)
        return None


def _identifier_parts(entry: dict) -> set:
    """Hyphen/underscore parts of the entry's SKILL NAME — and only the name.
    "access-efs-data" -> {access, efs, data}. The name is what the skill's
    author chose as its identity, so a prose keyword equal to a name part is a
    deliberate reference, not incidental vocabulary (g-248-105). Companion
    SCRIPT names are deliberately EXCLUDED: roblox-bridge.py would make
    "bridge" an identifier part of access-roblox-studio, reintroducing the
    exact observed FP (prose "bridge" -> that skill) this fix closes. Trigger/
    row prose is likewise excluded (it IS the vocabulary being demoted)."""
    parts = set()
    if entry.get("skill"):
        for frag in re.split(r"[-_./\s]+", str(entry["skill"]).lower()):
            if frag:
                parts.add(frag)
    return parts


def _single_token_qualifies(tok: str, entry: dict) -> bool:
    """g-248-105: is a SOLE shared token discriminative enough to match?

    True when the token is structurally compound (hyphen/underscore/digit —
    "backend-cat", "s3", "zeta_deploy": such tokens exist only where someone
    named a real thing) OR it is an imperative capability VERB
    (_IMPERATIVE_VERBS — "commit the hotfix" / "push and show the team" name
    a deliberate provisionable ACTION; dropping those re-opened the
    g-115-1883 recall regressions) OR it is an identifier part of THIS
    entry's skill NAME ("efs" in access-efs-data, "roblox" in
    access-roblox-studio; companion scripts excluded — see _identifier_parts).
    Plain common-prose tokens shared only via trigger/row VOCABULARY ("bridge",
    "analysis", "reachable" — none imperative verbs) fail — the sig-30
    hardcoded-list-under-coverage FPs this closes (each forced an --override
    3x in one session). NOT more stopwords by design: extraction is
    untouched, so these tokens still count inside multi-token overlaps —
    zero recall loss for any 2+-token match."""
    if "-" in tok or "_" in tok or any(c.isdigit() for c in tok):
        return True
    if tok in _IMPERATIVE_VERBS:
        return True
    return tok in _identifier_parts(entry)


def _find_matches(keywords: set, entries: list) -> list:
    """INVARIANT: whole-token set intersection, NOT substring matching.
    Substring matching would let "port" match "report" and "exe" match
    "execute". Synthetic test suite locks this in.

    Single-token precision (g-248-105): a match carried by ONE shared token
    survives only when that token is distinctive (_single_token_qualifies).
    Multi-token overlaps (>=2) always survive. SAFETY DIRECTION: dropping a
    match LOOSENS the gate (fewer refusals of user-routing), which risks the
    g-115-792 anti-pattern — hence the narrow predicate + the both-ways test
    matrix (FPs stop; genuine single-token matches like "cannot access EFS"
    -> efs -> access-efs-data still fire via identifier parts)."""
    matches = []
    for entry in entries:
        entry_toks = _entry_tokens(entry)
        hits = sorted(keywords & entry_toks)
        if not hits:
            continue
        if len(hits) == 1 and not _single_token_qualifies(hits[0], entry):
            continue  # sole generic-prose token — vocabulary, not a reference
        m = dict(entry)
        m["matched_keyword"] = hits[0]
        m["all_matched_keywords"] = hits
        matches.append(m)
    return matches


# --- Main entry point --------------------------------------------------------

def evaluate(failure_reason: str, *,
             diagnostic_text: str = "",
             intended_participants: str = "user",
             override_agent_match: Optional[str] = None,
             evidence_raw: Optional[str] = None,
             suggest_unblock: bool = False,
             for_goal_id: Optional[str] = None,
             agent_name: str = "",
             world_dir: Optional[Path] = None,
             skills_dir: Optional[Path] = None) -> dict:
    """Run the capability gate. See module docstring for return shape +
    side effects.

    Evidence-error short-circuit: when evidence_raw is provided AND
    malformed, returns 3-key shape and emits fail_open telemetry.
    Callers must check for `evidence_error` key before processing.

    Args:
        failure_reason: the failure-reason text from CREATE_BLOCKER.
        diagnostic_text: optional concatenated diagnostic content.
        intended_participants: "agent" | "user" | "hybrid".
        override_agent_match: free-text override justification, or None.
        evidence_raw: structured evidence JSON string, or None.
        suggest_unblock: when True AND would_block, emit unblock_payload.
        for_goal_id: goal-id to interpolate into unblock_title.
        agent_name: MIND_AGENT value for audit-log "agent" field.
        world_dir: WORLD_DIR for source-loaders + audit log. None
            disables all world-backed checks.
        skills_dir: SKILLS_DIR for SKILL.md scan. Defaults to repo's
            .claude/skills when None.
    """
    if skills_dir is None:
        skills_dir = _DEFAULT_SKILLS_DIR

    text_blob = failure_reason or ""
    if diagnostic_text:
        text_blob += "\n" + diagnostic_text

    # CRITICAL ORDER: _load_noise_phrases(world_dir) must run BEFORE
    # _extract_keywords — keyword extraction depends on the WORLD_DIR-scoped
    # noise-phrase list; reordering silently disables noise disqualification
    # (g-115-460).
    noise_phrases = _load_noise_phrases(world_dir)
    keywords = _extract_keywords(text_blob, noise_phrases=noise_phrases)
    keywords = _filter_context_disqualified(text_blob, keywords)

    all_entries = []
    all_entries.extend(_load_forged_skills(world_dir))
    all_entries.extend(_load_skill_md_triggers(skills_dir))
    all_entries.extend(_load_capability_routing(world_dir))

    matches = _find_matches(keywords, all_entries)

    narrative_matches = _match_narrative_patterns(failure_reason)
    session_req_matches = _match_session_requirement_patterns(failure_reason)
    session_req_classification = None
    if session_req_matches:
        classifications = [
            _classify_session_requirement(cx, failure_reason)
            for _, cx in session_req_matches
        ]
        if "user_keystroke_required" in classifications:
            session_req_classification = "user_keystroke_required"
        else:
            session_req_classification = "agent_provisionable"

    # Evidence parsing — short-circuit on malformed input.
    evidence_entries = []
    evidence_error = None
    evidence_logged_to = None
    if evidence_raw is not None:
        evidence_entries, evidence_error = _parse_evidence(evidence_raw)
        if evidence_error:
            err = {
                "would_block": True,
                "evidence_error": evidence_error,
                "reason": ("--evidence provided but invalid: " + evidence_error
                           + ". Fix the evidence JSON or fall back to "
                             "--override-agent-match \"<justification>\"."),
            }
            _gate_log(
                "capability-gate",
                "fail_open",
                payload=failure_reason or "",
                gate_error=evidence_error,
                extra={
                    "would_block": True,
                    "intended_participants": intended_participants,
                    "decision_path": "evidence-error",
                },
            )
            return err

    # Approval precedence: valid evidence > override.
    approval_kind = None
    if evidence_entries:
        approval_kind = "evidence"
    elif override_agent_match:
        approval_kind = "override-agent-match"

    user_only_matches = _match_user_only_preconditions(failure_reason)
    cure_action = _resolve_cure_action(user_only_matches)

    suggested_routing = "agent" if matches else "unknown"

    keyword_block = (
        bool(matches)
        and intended_participants == "user"
        and approval_kind is None
        and not (user_only_matches and cure_action is None)
        and session_req_classification != "user_keystroke_required"
    )

    session_req_block = (
        session_req_classification == "agent_provisionable"
        and intended_participants == "user"
        and approval_kind is None
    )

    cure_block = (
        cure_action is not None
        and intended_participants == "user"
        and approval_kind is None
        and session_req_classification != "user_keystroke_required"
    )

    would_block = keyword_block or session_req_block or cure_block

    # Log evidence approval ONLY when the gate would have blocked without
    # it — preserves the ledger from no-signal entries.
    if (approval_kind == "evidence" and bool(matches)
            and intended_participants == "user"):
        evidence_logged_to = _log_evidence_approval(
            world_dir, agent_name, failure_reason, evidence_entries,
            intended_participants, matches,
        )

    # Unblock-suggestion payload (g-257-02).
    unblock_payload = {}
    if suggest_unblock:
        if would_block:
            title_suffix = f" for {for_goal_id}" if for_goal_id else ""
            action_verb = None
            for m in _TOKEN_RE.finditer(failure_reason or ""):
                tok = m.group(0).lower()
                if tok in _IMPERATIVE_VERBS:
                    # g-115-2583 / rb-3955: an ambiguous verb-adjective used
                    # ADJECTIVALLY ("clean" in "clean session") is not the
                    # requested action -- skip it and keep scanning for a real
                    # verb. When none follows, action_verb stays None and the
                    # g-115-1872 verbless-suppression below correctly withholds
                    # the Unblock (rather than filing "Unblock: clean for g-X").
                    if tok in _ADJECTIVE_VERBS and _is_adjectival_use(
                            failure_reason or "", m):
                        continue
                    action_verb = tok
                    break
            if cure_action:
                # cap_label intentionally omitted — description f-string in
                # this branch references first_precon + cure_action directly,
                # not the matched-capability label (unlike keyword_block /
                # session_req branches below where cap_label IS used).
                first_precon = user_only_matches[0]
                if matches:
                    top = matches[0]
                    top_kw = top["matched_keyword"]
                    cap_source = top.get("source")
                    cap_skill = top.get("skill")
                    cap_row = top.get("row")
                else:
                    top_kw = "cure-action"
                    cap_source = "user_only_precondition_cure_registry"
                    cap_skill = None
                    cap_row = None
                title_action = cure_action
                unblock_payload = {
                    "unblock_suggested": True,
                    "unblock_title": f"Unblock: {title_action}{title_suffix}",
                    "unblock_description": (
                        f"Failure reason: {(failure_reason or '')[:160]}. "
                        f"Capability gate detected user-only precondition "
                        f"'{first_precon}' which has a registered agent-"
                        f"provisionable cure: '{cure_action}'. Cure registry "
                        f"overrides the blanket exemption for this substring. "
                        f"Invoke the cure rather than waiting on user."
                    ),
                    "matched_capability": {
                        "source": cap_source,
                        "skill": cap_skill,
                        "row": cap_row,
                        "matched_keyword": top_kw,
                        "cure_action": cure_action,
                        "cure_for_precondition": first_precon,
                    },
                }
            elif keyword_block and not (action_verb is None
                                        and session_req_matches):
                # (Verbless keyword match WITH session-requirement phrasing
                # falls through to the session-req branch below — its 'start'
                # default verb is the right action. The g-115-1872 suppression
                # inside this branch targets spurious noun-matches only.)
                top = matches[0]
                top_kw = top["matched_keyword"]
                # g-115-1872: the Unblock suggestion must name a genuine ACTION
                # the agent performs instead of deferring. When failure_reason
                # carries no imperative verb (action_verb is None) the prior
                # `action_verb or top_kw` fallback used the matched KEYWORD --
                # often a bare domain noun (e.g. 'npc' from a tree-restructuring
                # description token-matched to access-efs-data) -- yielding a
                # meaningless "Unblock: npc" / "perform npc" HIGH goal that
                # topped the selector queue (g-115-1868). A verbless match
                # against a domain-noun token is the spurious-match signature:
                # suppress the Unblock (would_block still stands, so the improper
                # defer/user-route is still refused) rather than file a
                # noun-as-action. rb-574 fixed title=verb-not-keyword; this
                # closes the deeper no-verb-at-all case.
                if action_verb is None:
                    unblock_payload = {
                        "unblock_suggested": False,
                        "unblock_suppressed_reason": (
                            f"no imperative verb in failure_reason; matched "
                            f"keyword '{top_kw}' is a bare token, not an action "
                            f"(g-115-1872 noun-as-verb guard)"
                        ),
                    }
                else:
                    cap_label = top.get("skill") or (top.get("row", "")[:120])
                    cap_source = top.get("source")
                    cap_skill = top.get("skill")
                    cap_row = top.get("row")
                    title_action = action_verb
                    unblock_payload = {
                        "unblock_suggested": True,
                        "unblock_title": f"Unblock: {title_action}{title_suffix}",
                        "unblock_description": (
                            f"Failure reason: {(failure_reason or '')[:160]}. "
                            f"Capability gate matched '{top_kw}' against "
                            f"{cap_source}: {cap_label}. "
                            f"Action required: '{title_action}'. "
                            f"Invoke the matched capability (or a peer) to perform "
                            f"'{title_action}' rather than routing to user."
                        ),
                        "matched_capability": {
                            "source": cap_source,
                            "skill": cap_skill,
                            "row": cap_row,
                            "matched_keyword": top_kw,
                        },
                    }
            else:
                session_phrase, captured_x = session_req_matches[0]
                top_kw = "start-session"
                phrase_excerpt = (session_phrase or "")[:60]
                cap_label = (
                    f"Game session — RUN-mode (matched phrasing: '{phrase_excerpt}')"
                )
                cap_source = "capability-routing.md (Agent-Provisionable Services)"
                cap_skill = None
                cap_row = None
                if action_verb is None:
                    action_verb = "start"
                title_action = action_verb
                unblock_payload = {
                    "unblock_suggested": True,
                    "unblock_title": f"Unblock: {title_action}{title_suffix}",
                    "unblock_description": (
                        f"Failure reason: {(failure_reason or '')[:160]}. "
                        f"Capability gate matched '{top_kw}' against "
                        f"{cap_source}: {cap_label}. "
                        f"Action required: '{title_action}'. "
                        f"Invoke the matched capability (or a peer) to perform "
                        f"'{title_action}' rather than routing to user."
                    ),
                    "matched_capability": {
                        "source": cap_source,
                        "skill": cap_skill,
                        "row": cap_row,
                        "matched_keyword": top_kw,
                    },
                }
        else:
            unblock_payload = {"unblock_suggested": False}

    result = {
        "matches": [
            {k: v for k, v in m.items() if k != "scripts" or v}
            for m in matches[:20]
        ],
        "match_count": len(matches),
        "suggested_routing": suggested_routing,
        "intended_participants": intended_participants,
        "override_applied": override_agent_match,
        "evidence_applied": evidence_entries if evidence_entries else None,
        "evidence_logged_to": evidence_logged_to,
        "approval_kind": approval_kind,
        "keywords_extracted": sorted(keywords),
        "sources_scanned": len(all_entries),
        "would_block": would_block,
        **unblock_payload,
        "narrative_framing_detected": bool(narrative_matches),
        "narrative_patterns": narrative_matches,
        "user_only_preconditions_detected": bool(user_only_matches),
        "user_only_precondition_substrings": user_only_matches,
        "session_requirement_detected": bool(session_req_matches),
        "session_requirement_phrases": [phrase for phrase, _ in session_req_matches],
        "session_requirement_classification": session_req_classification,
        "cure_action": cure_action,
        "cure_overrides_exemption": cure_action is not None,
    }

    # Reason composition — precedence: cure > keyword > session-req >
    # exemption > user-keystroke > evidence > plain match > no match.
    if would_block and cure_action:
        first_precon = user_only_matches[0]
        if matches:
            top = matches[0]
            kw_clause = (
                f" Keyword scan also matched {top['source']}: "
                f"{top.get('skill') or top.get('row', '')[:80]} "
                f"(keyword: {top['matched_keyword']})."
            )
        else:
            kw_clause = " Keyword scan returned no matches."
        result["reason"] = (
            f"User-only precondition '{first_precon}' detected with registered "
            f"agent cure: '{cure_action}'. Cure registry overrides the blanket "
            f"exemption — defer refused, invoke the cure capability instead."
            f"{kw_clause} If this match is a false positive, re-call with "
            '--override-agent-match "<justification>".'
        )
    elif would_block and keyword_block:
        top = matches[0]
        canonical_hint = (
            " If your failure mode is genuinely user-only (the matched "
            "capability has user-only subsets the agent cannot address), "
            "prefix defer_reason with one of these canonical substrings "
            f"to exempt: {', '.join(USER_ONLY_PRECONDITION_SUBSTRINGS)}."
        )
        result["reason"] = (
            f"Matched agent-provisionable capability in {top['source']}"
            f": {top.get('skill') or top.get('row', '')[:120]}"
            f" (keyword: {top['matched_keyword']}). "
            "Invoke that capability instead of routing to user. "
            "If this match is a false positive, re-call with "
            '--override-agent-match "<justification>" OR '
            '--evidence \'[{"type":"rb","id":"rb-NNN","claim":"..."}]\' '
            "for structured agent self-approval."
            f"{canonical_hint}"
        )
    elif would_block and session_req_block:
        session_phrase = session_req_matches[0][0]
        canonical_hint = (
            " If your failure mode is genuinely user-only (E-press, manual "
            "character spawn, or keystroke-required UI), prefix defer_reason "
            "with one of these canonical substrings to exempt: "
            f"{', '.join(USER_ONLY_PRECONDITION_SUBSTRINGS)}."
        )
        result["reason"] = (
            f"Matched session-requirement narrative pattern "
            f"'{session_phrase[:80]}'. Agent-provisionable: "
            "'Game session — RUN-mode' is startable via "
            "roblox-studio.sh start-session --mode RUN whenever "
            "plugin_connected=true (capability-routing.md "
            "Agent-Provisionable Services). Invoke that capability "
            "instead of routing to user. If this match is a false positive, "
            're-call with --override-agent-match "<justification>".'
            f"{canonical_hint}"
        )
    elif matches and user_only_matches:
        result["reason"] = (
            f"{len(matches)} capability keyword match(es) exempted: "
            f"failure_reason names user-only precondition(s) "
            f"{user_only_matches}. The matched capability "
            f"({matches[0].get('skill') or matches[0].get('row', '')[:80]}) "
            "splits into agent-provisionable and user-only subsets; the "
            "structured precondition prefix identified the user-only subset, "
            "so the keyword match is not actionable. Defer permitted."
        )
    elif session_req_classification == "user_keystroke_required":
        session_phrase = session_req_matches[0][0]
        result["reason"] = (
            f"Session-requirement matched ('{session_phrase[:80]}') but "
            "session type requires user keystroke (E-press, F5-Play "
            "character spawn, manual UI, or related platform-constrained "
            "input). Defer permitted; "
            "narrative_framing_detected: session_requirement_user_keystroke."
        )
    elif matches and approval_kind == "evidence":
        logged_suffix = (
            f" (logged to {evidence_logged_to})" if evidence_logged_to
            else " (not logged — intended_participants != user, no block averted)"
        )
        result["reason"] = (
            f"{len(matches)} capability match(es) found; "
            f"agent self-approved via {len(evidence_entries)} evidence entries"
            f"{logged_suffix}."
        )
    elif matches:
        result["reason"] = (
            f"{len(matches)} capability match(es) found; user-routing not attempted"
            " or override provided."
        )
    else:
        result["reason"] = "No agent-provisionable capability matched; routing decision left to caller."

    # Telemetry — _resolve_trigger mirrors unblock_payload precedence so
    # the gate-firings ledger and the routing message agree on attribution.
    has_any_match = (
        bool(matches) or bool(session_req_matches) or cure_action is not None
    )

    def _resolve_trigger():
        if cure_action:
            return f"cure:{user_only_matches[0]}"
        if matches:
            return matches[0].get("matched_keyword")
        if session_req_matches:
            return "session-requirement"
        return None

    if not has_any_match:
        decision = "noop"
        trigger = None
    elif would_block:
        decision = "block"
        trigger = _resolve_trigger()
    elif override_agent_match:
        decision = "override"
        trigger = _resolve_trigger()
    else:
        decision = "pass"
        trigger = _resolve_trigger()

    gate_extra = {
        "would_block": would_block,
        "match_count": len(matches),
        "intended_participants": intended_participants,
        "approval_kind": approval_kind,
        "evidence_logged_to": evidence_logged_to,
        "keywords_count": len(keywords),
        "sources_scanned": len(all_entries),
        "top_match_source": (matches[0].get("source") if matches else None),
    }
    if narrative_matches:
        gate_extra["narrative_framing_detected"] = True
        gate_extra["narrative_patterns"] = narrative_matches
    if session_req_matches:
        gate_extra["session_requirement_detected"] = True
        gate_extra["session_requirement_classification"] = (
            session_req_classification
        )
    if cure_action:
        gate_extra["cure_action_registered"] = cure_action
        gate_extra["cure_overrides_exemption"] = True

    _gate_log(
        "capability-gate",
        decision,
        trigger_matched=trigger,
        payload=failure_reason or "",
        override_reason=override_agent_match,
        extra=gate_extra,
    )

    return result
