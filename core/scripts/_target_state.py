"""Shared library: extract target files + identifiers from a goal
description, then probe whether those identifiers already appear in the
target files.

Consumers:
  - goal-duplication-gate.py (filing-time hard block, 4th check)
  - target-state-probe.py    (execution-time advisory)

Fail-open: missing files, unreadable text, and failed extraction all
produce verdict="unknown" — callers must treat "unknown" as no-op.

Origin: 2026-04-20 g-115-141 incident (fix predated goal filing; grep
would have caught the already-done state before execution).
"""

import os
import re
from pathlib import Path

from _gate_log import log as _gate_log

# File-path regex aligned with goal-duplication-gate's _FILE_PATH_RE:
# word boundary, word-char start, common source extensions.
_FILE_PATH_RE = re.compile(
    r"\b([\w][\w./-]*\.(?:py|sh|md|yaml|yml|json|jsonl|ts|tsx|js|lua|go|java|toml))\b"
)

# file.ext:N line-hint regex (re-uses FILE_PATH + ":" + digits).
_LINE_HINT_RE = re.compile(
    r"\b([\w][\w./-]*\.(?:py|sh|md|yaml|yml|json|jsonl|ts|tsx|js|lua|go|java|toml))"
    r":(\d{1,6})\b"
)

# Backtick-quoted tokens. Non-greedy content up to 120 chars so we don't
# swallow a whole code block.
_BACKTICK_RE = re.compile(r"`([^`\n]{2,120})`")

# Zero-arg call in prose: `frobnicate()` style. Requires leading letter or
# underscore, length >= 4, followed by "()".
_CALL_RE = re.compile(r"\b([A-Za-z_][\w]{3,})\(\)")

# snake_case identifier with >=1 underscore, length >= 5.
_SNAKE_RE = re.compile(r"\b([a-z_][a-z0-9]*(?:_[a-z0-9]+){1,})\b")

# CamelCase identifier, length >= 6 (avoids "HTTP", "AWS", "JSON", etc.).
_CAMEL_RE = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+){1,})\b")

# Stopwords applied to snake_case + CamelCase extraction. Backticked and
# call-shaped tokens are trusted verbatim (the author used quotes for a
# reason).
_STOPWORDS = {
    "this_goal", "this_aspiration", "goal_id", "aspiration_id",
    "knowledge_tree", "reasoning_bank", "reasoning_banks",
    "world_dir", "world_path", "meta_path",
    "working_memory", "session_state",
    "todo_list", "sub_goal", "line_number",
    "SkillSet", "SkillDef", "GoalSet", "GoalDef",
}

# Guardrails: keep extraction bounded.
_MAX_FILES = 6
_MAX_IDENTIFIERS = 12
_MAX_FILE_BYTES = 512 * 1024  # 512 KB — skip bigger files rather than hang.


# Paths that DESCRIBE work rather than IMPLEMENT it. When these appear
# alongside implementation files in target_files, probe_target_state's
# union-across-files aggregation produces false-positive "already_present"
# verdicts: the report describes what to do; the impl file is where the
# work goes. Exclude these from probe targets so the aggregate hit_ratio
# reflects only implementation files.
#
# Discovered 2026-04-25 () when filing  follow-up to
#  audit — bravo/reports/vertx-process-audit-2026-04-25.md had
# all 5 identifiers, Driver.java had 0, aggregate hit_ratio=1.0 blocked
# the impl goal. Per-file breakdown showed the problem clearly but the
# verdict aggregated across files.
_DOCUMENTATION_ONLY_PATTERNS = [
    # temp/ briefings (fresh-eyes, felt-sense) are description-of-work staging
    # files, not implementation targets. (This superseded the legacy reports/
    # pattern: the file-model normalization moved briefings reports/ -> temp/,
    # and reports/ was abolished 2026-06-02 — git history is its archive.)
    re.compile(r"(?:^|/)temp/.*\.md$"),
]


def _is_documentation_only_path(fp):
    """True iff fp is a description-of-work file, not an implementation target.

    Used to filter target_files in extract_targets so audit reports cannot
    contaminate the probe's aggregate hit_ratio (g-001-191).
    """
    return any(p.search(fp) for p in _DOCUMENTATION_ONLY_PATTERNS)


# READ-intent goal title detector (rb-398 follow-through).
#
# Some goal types READ their target files rather than write to them:
# Investigate, Audit, Review, Observe, Research, Analyze — plus compound
# "<agent>'s work review:" titles. For these the target_state check is
# semantically INVERTED: identifiers appearing in target files is a
# PRECONDITION of the work (you need code to audit it), not a "fix already
# shipped" signal. Without this carve-out, a well-formed Investigate goal
# that lists 12 real identifiers in 3 real files trips hit_ratio=1.0 and
# gets blocked — the opposite of the intended semantic.
#
# Match rule: ANY whitespace-separated word in the pre-colon title segment
# matches READ_INTENT_VERBS, case-insensitive, after stripping possessive
# "'s" / "'" suffixes. Catches:
#   "Investigate: X"                 → word "investigate"  → exempt
#   "Review hypothesis: X"           → word "review"       → exempt
#   "Alpha's work review: X"         → word "review"       → exempt
#   "Fix: X"                         → word "fix"          → apply check
#   "Idea: add Y"                    → word "idea"         → apply check
#   "Fix: review the retry logic"    → prefix "Fix", no colon match → apply
#                                       check (review is AFTER colon,
#                                       primary intent is Fix)
#
# Single source of truth — both goal-duplication-gate.py (filing-time
# blocker) and target-state-probe.py (execution-time advisory) call
# is_read_intent() so they cannot diverge on what counts as READ-intent.

READ_INTENT_VERBS = frozenset({
    "investigate", "audit", "review", "observe", "research", "analyze",
    # Added 2026-05-10 () per  weekly classifier-accuracy
    # scan: 28 'probe' + 18 'scan' read-intent goal completions in 7 days
    # were missed (FN). Both verbs consistently used for diagnostic/detection
    # actions in observed sample. 'check' (14 instances) intentionally NOT
    # added — ambiguous between read-intent and 'check and fix' write-intent.
    "probe", "scan",
})


def is_read_intent(title, _caller="unknown"):
    """Return True if the goal's title prefix segment contains a READ-verb.

    Prefix segment is everything before the first ':'. If no colon, the
    whole title. Words are lowercased and possessive suffixes stripped
    before matching READ_INTENT_VERBS.

    `_caller` is a callsite label used only for telemetry (gate firing log).
    Pass a stable string from each consumer so the dashboard can attribute
    matches/misses to goal-duplication-gate vs target-state-probe.
    """
    if not title:
        return False
    prefix = title.split(":", 1)[0]
    # Positional `check` rule (, 2026-05-17): `check` as the FINAL word
    # of the title prefix is read-intent. Prior cycle (, 2026-05-10)
    # excluded `check` from READ_INTENT_VERBS due to ambiguity ('Strategic
    # vision check' = read-intent vs 'Added verify-learning check' =
    # write-intent).  classifier scan 2026-05-17 found 15 FN on
    # `check`; disambiguating pattern: read-intent when LAST word of prefix.
    # Cross-ref: rb-648 (verify named hook target),  (FN measurement).
    words = prefix.split()
    if words:
        last_w = words[-1].lower()
        if last_w.endswith("'s"):
            last_w = last_w[:-2]
        elif last_w.endswith("'"):
            last_w = last_w[:-1]
        if last_w == "check":
            _gate_log("read-intent-verbs", "pass",
                      caller=_caller,
                      trigger_matched="check",
                      payload=title[:200],
                      extra={"position": "final-prefix-word"})
            return True
    for word in prefix.split():
        w = word.lower()
        if w.endswith("'s"):
            w = w[:-2]
        elif w.endswith("'"):
            w = w[:-1]
        if w in READ_INTENT_VERBS:
            # gate_id MUST match core/config/gates.yaml id.
            # "pass" = read-intent detected → caller will exempt downstream dup check.
            _gate_log("read-intent-verbs", "pass",
                      caller=_caller,
                      trigger_matched=w,
                      payload=title[:200])
            return True
    # "noop" = no verb match → caller proceeds with default (non-exempted) behavior.
    _gate_log("read-intent-verbs", "noop",
              caller=_caller,
              trigger_matched=None,
              payload=title[:200])
    return False


def _clean_identifier(raw):
    """Normalize a backtick-extracted token to a bare identifier or None.

    `foo.bar()` -> "foo.bar"; `foo = 1` -> "foo"; `foo bar baz` -> None
    (whitespace-containing phrases are prose, not identifiers).
    """
    s = raw.strip()
    if not s:
        return None
    # Drop trailing () or () {} — keep the name.
    s = re.sub(r"\(\s*\)\s*\{?\s*$", "", s).strip()
    # If it still contains whitespace, it's a phrase, not an identifier.
    if re.search(r"\s", s):
        return None
    # Require at least one letter or underscore as the first char, length >= 3.
    if len(s) < 3 or not re.match(r"^[A-Za-z_]", s):
        return None
    # Permit dotted paths (module.func) and hyphenated names.
    if not re.match(r"^[A-Za-z_][\w\-.]*$", s):
        return None
    return s


# Maintain-CHECK-ABOUT pattern (, design from ).
#
# Goals of the shape "Maintain: add/wire/ensure [<word>] check ..." describe
# the addition of a verification assertion. The target_files extractor
# naturally picks the ASSERTION-TARGET files (the wiring/source the new check
# inspects) because those filenames appear in the goal text — but the
# ASSERTION-HOST file (the SKILL.md where the check itself lives) usually
# doesn't appear as a literal path. Identifiers in assertion-target files are
# tautologically present (that's why they're being asserted on), so
# probe_target_state's union-across-files aggregation produces hit_ratio=1.0
# false-positive "already_present" verdicts.
#
# Canonical incident:  — the probe scanned orphan-root-sweep.sh +
# _orphan_root_helpers.py (where the assertion target lives) but never
# .claude/skills/verify-learning/SKILL.md (where the new check needed to
# land), blocking a legitimate Maintain goal at the duplication gate.
#
# Fix: when the predicate matches, REPLACE target_files with [edit_target]
# (not augment — augmenting keeps hit_ratio=1.0 because total_hits sums
# across all files). Only replacement isolates the edit-target signal.
# NOTE on the optional-word group: the spec from  says "X is
# word-character class" but the canonical incident  has title
# "Maintain: add verify-learning check ..." where "verify-learning" is
# hyphenated. Bare \w+ fails on hyphens, so use [\w-]+ for the optional
# token between the verb and "check". This is the minimum extension
# needed to match canonical Maintain-CHECK-ABOUT goal phrasing in the wild
# ("verify-learning check", "post-state-update check", etc.). \S+ would
# work too but [\w-]+ is more conservative (won't accidentally match dots
# or special chars).
_MAINTAIN_CHECK_ABOUT_RE = re.compile(
    r"^Maintain:.*\b(?:add|wire|ensure)\b\s+(?:[\w-]+\s+)?check\b",
    re.IGNORECASE,
)

# Explicit .claude/skills/<name>/SKILL.md path in goal text. Takes priority
# over phrase-based inference — if the author named the file explicitly,
# trust the author.
_SKILL_MD_PATH_RE = re.compile(
    r"\.claude/skills/([a-z0-9_-]+)/SKILL\.md",
    re.IGNORECASE,
)

# "add/wire/ensure verify-learning check" — the most common shape (rb-917 /
# guard-343 lineage routes encoding work through verify-learning SKILL.md).
# Allows hyphen OR space between verify and learning.
_VERIFY_LEARNING_TRIGGER_RE = re.compile(
    r"\b(?:add|wire|ensure)\s+verify[- ]?learning\s+check\b",
    re.IGNORECASE,
)

# "add/wire/ensure check to/in/into <skill-name>" — resolves to
# .claude/skills/<skill-name>/SKILL.md. Strips optional leading slash
# from the skill name (callers sometimes write "/respond").
_ADD_CHECK_TO_SKILL_RE = re.compile(
    r"\b(?:add|wire|ensure)\s+(?:\w+\s+)?check\s+(?:to|in|into)\s+/?([a-z0-9_-]+)\b",
    re.IGNORECASE,
)


def _is_maintain_check_about_goal(title):
    """True iff title is a Maintain-CHECK-ABOUT goal that needs edit_target replacement.

    Matches: '^Maintain:<anything> (add|wire|ensure) [<word> ]check<anything>'
    case-insensitive. The "<word> " between the verb and "check" is optional
    (covers both "add check" and "add verify-learning check").
    """
    if not title:
        return False
    return bool(_MAINTAIN_CHECK_ABOUT_RE.search(title))


def _extract_edit_target(title, description):
    """Resolve the ASSERTION-HOST SKILL.md path for a Maintain-CHECK-ABOUT goal.

    Priority chain (highest wins — author explicitness > phrase inference > default):
      (a) explicit '.claude/skills/<name>/SKILL.md' literal in title/description
      (b) 'add/wire/ensure verify-learning check' phrase -> verify-learning SKILL.md
      (c) 'add/wire/ensure check to/in/into <skill-name>' -> .claude/skills/<name>/SKILL.md
      (d) generic Maintain-CHECK-ABOUT (no specific signal) -> verify-learning SKILL.md (default)

    Returns the relative path string. The default (d) preserves the behavior
    "if we don't know which SKILL.md hosts the assertion, route through
    verify-learning" — matches the rb-917 / guard-343 convention.
    """
    text = (title or "") + "\n" + (description or "")

    # (a) explicit path wins
    m = _SKILL_MD_PATH_RE.search(text)
    if m:
        return f".claude/skills/{m.group(1).lower()}/SKILL.md"

    # (b) verify-learning trigger phrase
    if _VERIFY_LEARNING_TRIGGER_RE.search(text):
        return ".claude/skills/verify-learning/SKILL.md"

    # (c) 'add/wire/ensure check to <skill-name>'
    m = _ADD_CHECK_TO_SKILL_RE.search(text)
    if m:
        return f".claude/skills/{m.group(1).lower()}/SKILL.md"

    # (d) default — verify-learning hosts the assertion lane
    return ".claude/skills/verify-learning/SKILL.md"


def extract_targets(title, description):
    """Extract target files + identifiers + line hints from goal text.

    Returns a dict:
      {
        "target_files": [str],             # unique, order-preserved
        "identifiers": [str],              # unique, order-preserved
        "line_hints": {path: [int, ...]},  # per-file hinted line numbers
        "confidence": "high|medium|low|none",
        "target_kind": str|None,           # "maintain-check-about" when
                                           #   _is_maintain_check_about_goal
                                           #   fired and target_files was
                                           #   REPLACED with [edit_target];
                                           #   None for all other goals.
      }
    """
    text = (title or "") + "\n" + (description or "")

    # Line hints FIRST — before file paths get deduped.
    line_hints = {}
    for m in _LINE_HINT_RE.finditer(text):
        fp = m.group(1)
        line = int(m.group(2))
        line_hints.setdefault(fp, []).append(line)

    # File paths.
    target_files_seen = []
    seen_files = set()
    for fp in _FILE_PATH_RE.findall(text):
        if fp in seen_files:
            continue
        # : skip audit/report description paths — they describe
        # work, not implement it; including them in target_files lets the
        # probe's aggregate hit_ratio false-positive "already_present" when
        # the audit report cites identifiers that the impl file does not
        # yet contain.
        if _is_documentation_only_path(fp):
            continue
        seen_files.add(fp)
        target_files_seen.append(fp)
        if len(target_files_seen) >= _MAX_FILES:
            break

    # Identifiers. Order:
    #   1. Backticked tokens (highest confidence — author quoted them)
    #   2. Zero-arg calls in prose
    #   3. snake_case with >=1 underscore
    #   4. CamelCase
    identifiers_seen = []
    seen_ids = set()

    def _add(raw):
        ident = _clean_identifier(raw)
        if ident and ident not in seen_ids and ident not in _STOPWORDS:
            # Don't re-add a file path as if it were an identifier.
            if ident in seen_files:
                return
            seen_ids.add(ident)
            identifiers_seen.append(ident)

    # Remove file-path substrings first so "retrieve.py" doesn't become
    # the "retrieve" identifier when searched as CamelCase/snake.
    text_for_ids = text
    for fp in target_files_seen:
        text_for_ids = text_for_ids.replace(fp, " ")

    for m in _BACKTICK_RE.finditer(text_for_ids):
        _add(m.group(1))
    for m in _CALL_RE.finditer(text_for_ids):
        _add(m.group(1))
    for m in _SNAKE_RE.finditer(text_for_ids):
        _add(m.group(1))
    for m in _CAMEL_RE.finditer(text_for_ids):
        _add(m.group(1))

    # Cap identifier list.
    identifiers_seen = identifiers_seen[:_MAX_IDENTIFIERS]

    # : Maintain-CHECK-ABOUT replacement. When the title matches
    # the assertion-addition pattern, REPLACE target_files with the single
    # assertion-HOST SKILL.md so probe_target_state checks for the check's
    # presence in SKILL.md, not in the assertion-target source files where
    # the identifiers are tautologically present. See _is_maintain_check_about_goal
    # docstring +  /  (canonical incident) for the design
    # rationale and the "replace not augment" decision.
    target_kind = None
    if _is_maintain_check_about_goal(title):
        edit_target = _extract_edit_target(title, description)
        if edit_target:
            target_files_seen = [edit_target]
            target_kind = "maintain-check-about"

    # Confidence.
    has_file = bool(target_files_seen)
    n_ids = len(identifiers_seen)
    has_line_hint = bool(line_hints)

    if not has_file:
        confidence = "none"
    elif n_ids >= 2 or (n_ids >= 1 and has_line_hint):
        confidence = "high"
    elif n_ids == 1:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "target_files": target_files_seen,
        "identifiers": identifiers_seen,
        "line_hints": line_hints,
        "confidence": confidence,
        "target_kind": target_kind,
    }


# Directories to skip when basename-searching the repo — VCS, build output,
# vendor dirs, history snapshots, agent private state.
_SEARCH_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".venv", "venv", ".tox", "dist", "build", ".next",
    ".history", ".cache", ".pytest_cache", ".mypy_cache",
}
_SEARCH_MAX_MATCHES = 6  # cap basename-search output to keep probe cheap.

#  inference fallback: when the goal text mentions class-shaped
# identifiers but no explicit file path (the  failure mode), walk
# search_roots looking for files where >=_INFER_MIN_IDENTIFIERS distinct
# identifiers co-occur. Self-reference (class file containing its own
# basename) is excluded to neutralize the trivial "StuckDetector.java
# mentions 'StuckDetector'" case.
_INFER_FILE_EXTS = {".py", ".sh", ".java", ".kt", ".cs", ".ts", ".tsx",
                    ".js", ".lua", ".go", ".rs"}
# Per-root cap on os.walk file inspection. Tuned for the framework repo
# (~500 .py + .sh files = sub-second) AND a single sibling product repo
# (typical 1k-3k source files = a few seconds). When a goal references
# identifiers that ONLY exist in a deep monorepo, the cap will be hit
# before exhaustive coverage — that's an acceptable trade-off given the
# advisory nature of the probe.
_INFER_MAX_FILES_PER_ROOT = 2500
# Skip files larger than this — class definitions are rarely >256KB and
# huge generated files (vendor bundles, autogen schemas) waste budget.
_INFER_MAX_FILE_BYTES = 256 * 1024
_INFER_MIN_IDENTIFIERS = 2  # require >=2 hits per file (excludes self-ref noise)


def _looks_like_class_name(ident):
    """PascalCase / CamelCase, alpha-numeric only, length >= 5.

    Restricts inference candidates to identifiers that look like type or
    class names. Excludes snake_case (likely a function or local), hyphens
    (likely a goal-id like 'rb-308'), and dotted paths (already file-like).
    """
    return bool(re.match(r"^[A-Z][a-zA-Z0-9]{4,}$", ident))


def _resolve_search_roots(agent_name=None):
    """Return search roots for inference: PROJECT_ROOT plus AGENT_WRITE_PATH
    when configured in <agent>/local-paths.conf.

    Sibling product repos under AGENT_WRITE_PATH are where g-250-10-class
    duplications hide — the framework can only catch them by walking outside
    PROJECT_ROOT. Empty list when neither is resolvable; caller treats
    empty as "skip inference."
    """
    try:
        import _paths  # late import — same lazy pattern as elsewhere
    except Exception:
        return []
    roots = [_paths.PROJECT_ROOT]
    agent = agent_name or os.environ.get("MIND_AGENT")
    if agent:
        conf = _paths.agent_dir(agent) / "local-paths.conf"
        if conf.is_file():
            try:
                lines = conf.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines:
                line = line.strip()
                if line.startswith("AGENT_WRITE_PATH=") and "=" in line:
                    # MULTI-ROOT (): ';'-separated, optionally quoted
                    # for bash-source safety. Each part is its own search root.
                    raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                    for part in raw.split(";"):
                        part = part.strip()
                        if part:
                            roots.append(Path(part))
    return roots


def _scan_root_for_co_occurrence(root_resolved, id_patterns, max_files,
                                  max_bytes_per_file):
    """Walk a single resolved root looking for files where >=2 distinct
    identifiers co-occur. Self-reference (file stem == identifier) is
    excluded. Returns list of (abs_path_str, hits_count) for that root.
    Bounded by max_files; aborts early on cap.
    """
    if not root_resolved.is_dir():
        return []
    file_hits = []
    scanned = 0
    for cur, dirs, files in os.walk(root_resolved):
        dirs[:] = [d for d in dirs
                   if d not in _SEARCH_SKIP_DIRS and not d.startswith(".")]
        for f in files:
            _, ext = os.path.splitext(f)
            if ext.lower() not in _INFER_FILE_EXTS:
                continue
            p = os.path.join(cur, f)
            try:
                if os.path.getsize(p) > max_bytes_per_file:
                    continue
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                continue
            scanned += 1
            file_stem = os.path.splitext(f)[0]
            hits = {i for i, pat in id_patterns.items()
                    if i != file_stem and pat.search(content)}
            if len(hits) >= _INFER_MIN_IDENTIFIERS:
                file_hits.append((p, len(hits)))
            if scanned >= max_files:
                return file_hits
        if scanned >= max_files:
            return file_hits
    return file_hits


def _infer_targets_from_identifiers(search_roots, identifiers, max_matches=_MAX_FILES,
                                     max_files_per_root=_INFER_MAX_FILES_PER_ROOT):
    """Walk search_roots looking for files where >=_INFER_MIN_IDENTIFIERS
    distinct class-shaped identifiers co-occur, EXCLUDING trivial
    self-reference (a file named StuckDetector.java mentioning
    "StuckDetector" — that's the class definition, not evidence of feature
    completion).

    Two-pass strategy: scan roots IN ORDER, abort early when matches found.
    The framework repo (PROJECT_ROOT) is fast (~500 source files) so it
    lands first; sibling product repos (AGENT_WRITE_PATH) only get walked
    when the framework yields no hits. Keeps the common framework-goal
    case under ~1s while still catching cross-repo duplicates (g-250-10
    class).

    Returns list of (abs_path_str, hits_count) tuples sorted by hits desc.
    """
    class_idents = [i for i in identifiers if _looks_like_class_name(i)]
    if len(class_idents) < _INFER_MIN_IDENTIFIERS:
        return []

    # Inference uses a more lenient pattern than probe_target_state's
    # verdict regex: allow dotted prefix so `argparse.ArgumentParser`
    # matches a bare `ArgumentParser` from the goal description. The
    # verdict logic in probe_target_state stays strict (it must not
    # false-positive in the duplication call), but inference scanning
    # needs to be permissive — most class-name references in production
    # code use a module-qualified form while goal descriptions don't.
    id_patterns = {i: re.compile(r"\b" + re.escape(i) + r"\b")
                   for i in class_idents}

    aggregate = []
    for root in search_roots:
        try:
            root_resolved = Path(root).resolve()
        except (OSError, ValueError):
            continue
        per_root = _scan_root_for_co_occurrence(
            root_resolved, id_patterns, max_files_per_root, _INFER_MAX_FILE_BYTES,
        )
        aggregate.extend(per_root)
        if aggregate:
            # Found hits in this root — short-circuit. Cheaper roots are
            # walked first by convention (PROJECT_ROOT before AGENT_WRITE_PATH).
            break

    aggregate.sort(key=lambda x: -x[1])
    return aggregate[:max_matches]


def extract_and_infer_targets(title, description, search_roots=None, agent_name=None):
    """extract_targets + class-identifier inference fallback ().

    Bridges the gap when the goal description uses module/class identifiers
    but no explicit file path (g-250-10 class). When extract_targets returns
    no target_files but the description contains >=2 class-shaped
    identifiers, walks search_roots looking for files where those
    identifiers co-occur.

    Pure addition — extract_targets is unchanged. When search_roots is
    None or empty, behavior matches extract_targets exactly (no walk,
    no extra cost).

    Adds these keys to the returned dict:
      target_files_inferred: bool — True iff inference produced new files
      inference_hits: {abs_path_str: hit_count} — only when inferred
    """
    ex = extract_targets(title, description)
    ex["target_files_inferred"] = False

    if ex["target_files"] or not ex["identifiers"]:
        return ex

    if search_roots is None:
        search_roots = _resolve_search_roots(agent_name)
    if not search_roots:
        return ex

    matches = _infer_targets_from_identifiers(search_roots, ex["identifiers"])
    if not matches:
        return ex

    ex["target_files"] = [m[0] for m in matches]
    ex["target_files_inferred"] = True
    ex["inference_hits"] = {m[0]: m[1] for m in matches}
    # Inferred via co-occurrence — bump confidence from "none" to "medium"
    # (or "high" if line hints somehow exist, which is rare for inferred).
    ex["confidence"] = "high" if ex["line_hints"] else "medium"
    return ex


def _resolve_target_paths(project_root, rel_path, allowed_roots=None):
    """Resolve rel_path to one or more real file paths.

    Three modes:
      A) rel_path is absolute (e.g., from extract_and_infer_targets):
         validate it exists AND lives under one of allowed_roots (defaulting
         to [project_root]). Returns single-element list or []. Boundary
         check is preserved — refuses paths outside the allowed set.
      B) rel_path has slashes/backslashes (descriptive path):
         literal-resolve under project_root only.
      C) rel_path is a bare basename:
         literal-resolve, then bounded basename search under project_root.

    Returns list[Path]. Empty list means "not found" or "outside allowed roots".
    """
    project_root_resolved = Path(project_root).resolve()
    roots_resolved = []
    for r in (allowed_roots or [project_root_resolved]):
        try:
            roots_resolved.append(Path(r).resolve())
        except (OSError, ValueError):
            continue
    if not roots_resolved:
        roots_resolved = [project_root_resolved]

    p = Path(rel_path)

    # Mode A: absolute path (inferred from class-name search).
    if p.is_absolute():
        try:
            p_resolved = p.resolve()
        except OSError:
            return []
        if not p_resolved.is_file():
            return []
        for r in roots_resolved:
            try:
                p_resolved.relative_to(r)
                return [p_resolved]
            except ValueError:
                continue
        return []  # absolute path not under any allowed root — refuse.

    # Mode B/C: relative path under project_root.
    literal = (Path(project_root) / rel_path).resolve()
    try:
        literal.relative_to(project_root_resolved)
    except ValueError:
        return []
    if literal.is_file():
        return [literal]

    # Mode C: bare basename — bounded basename search.
    if "/" in rel_path or "\\" in rel_path:
        return []
    basename = Path(rel_path).name
    if not basename:
        return []
    matches = []
    for root, dirs, files in os.walk(project_root_resolved):
        dirs[:] = [d for d in dirs if d not in _SEARCH_SKIP_DIRS and not d.startswith(".")]
        if basename in files:
            matches.append(Path(root) / basename)
            if len(matches) >= _SEARCH_MAX_MATCHES:
                break
    return matches


def _read_target_file(project_root, rel_path):
    """Read target file content if it exists and is within size cap.
    Returns (content_str_or_None, existed_bool).

    This single-file reader is kept for line-hint verification where the
    caller already owns a specific path. For multi-path resolution, use
    _resolve_target_paths + _read_file_content.
    """
    p = (Path(project_root) / rel_path).resolve()
    try:
        project_root_resolved = Path(project_root).resolve()
        p.relative_to(project_root_resolved)
    except ValueError:
        return (None, False)
    if not p.is_file():
        return (None, False)
    return _read_file_content(p)


def _read_file_content(path):
    """Read a resolved Path with size cap. Returns (content, existed)."""
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return (None, True)
        return (path.read_text(encoding="utf-8", errors="replace"), True)
    except OSError:
        return (None, False)


def probe_target_state(project_root, target_files, identifiers, line_hints=None,
                        allowed_roots=None, lenient_match=False):
    """Check each identifier against each target file via substring-in-line match.

    Returns:
      {
        "verdict": "already_present|partially_present|absent|unknown",
        "per_file": [{"file":str, "exists":bool, "readable":bool,
                      "hits":[str], "misses":[str]}],
        "total_hits": int,             # unique identifiers found in ANY target
        "total_identifiers": int,
        "hit_ratio": float,            # total_hits / total_identifiers
        "line_hint_verifications": [
            {"file":str, "line":int, "anchor_found":bool}
        ],
      }

    Verdict thresholds: >=0.75 "already_present", >=0.25 "partially_present",
    >0 "partially_present", 0 "absent". No files/ids -> "unknown".
    """
    line_hints = line_hints or {}
    if not target_files or not identifiers:
        return {
            "verdict": "unknown",
            "per_file": [],
            "total_hits": 0,
            "total_identifiers": len(identifiers or []),
            "hit_ratio": 0.0,
            "line_hint_verifications": [],
            "reason": "no target files or no identifiers extracted",
        }

    # Compile identifier regexes once. Two pattern modes:
    #   strict (default): refuse to match `module.ClassName` when looking
    #     for `ClassName` — preserves the original probe semantics for
    #     explicit target_files (where dotted-prefix could be unrelated).
    #   lenient (caller opt-in): allow dotted prefix — needed when
    #     target_files were chosen by extract_and_infer_targets's
    #     co-occurrence walk, which used a lenient pattern; without this
    #     the probe would say verdict=absent on the very files inference
    #     said were strong matches. Caller passes lenient_match=True
    #     when target_files came from inference (target_files_inferred).
    if lenient_match:
        id_patterns = {i: re.compile(r"\b" + re.escape(i) + r"\b") for i in identifiers}
    else:
        id_patterns = {i: re.compile(r"(?<![\w.])" + re.escape(i) + r"(?![\w])") for i in identifiers}

    per_file = []
    found_anywhere = set()
    any_readable = False

    project_root_resolved = Path(project_root).resolve()
    for fp in target_files:
        resolved_paths = _resolve_target_paths(project_root, fp, allowed_roots=allowed_roots)
        if not resolved_paths:
            # Literal miss + no basename matches. Record as unreadable.
            per_file.append({
                "file": fp,
                "exists": False,
                "readable": False,
                "hits": [],
                "misses": list(identifiers),
            })
            continue
        # Union hits across all resolved copies of this basename (rare but
        # possible — e.g. retrieve.py exists in both core/scripts/ and
        # world/scripts/). We want the OR of hits, not the intersection:
        # if the fix landed in one copy, the goal is effectively already
        # implemented.
        union_hits = set()
        any_copy_readable = False
        any_copy_existed = False
        resolved_rel = []
        for p in resolved_paths:
            any_copy_existed = True
            content, existed = _read_file_content(p)
            try:
                rel = str(p.resolve().relative_to(project_root_resolved))
            except ValueError:
                rel = str(p)
            resolved_rel.append(rel)
            if content is None:
                continue
            any_copy_readable = True
            for i, pat in id_patterns.items():
                if pat.search(content):
                    union_hits.add(i)
        if any_copy_readable:
            any_readable = True
        hits = [i for i in identifiers if i in union_hits]
        misses = [i for i in identifiers if i not in union_hits]
        found_anywhere.update(union_hits)
        per_file.append({
            "file": fp,
            "resolved": resolved_rel,
            "exists": any_copy_existed,
            "readable": any_copy_readable,
            "hits": hits,
            "misses": misses,
        })

    # Line hint verification (anchor check): for each hinted line, confirm
    # that at least one identifier (or its stem) appears within +/- 5 lines
    # of the hint. Cheap sanity check that the line number is still roughly
    # accurate — catches "goal says line 82, but file now has 200 lines and
    # identifier is at line 150".
    line_hint_verifications = []
    for fp, lines in line_hints.items():
        content, existed = _read_target_file(project_root, fp)
        if content is None:
            for ln in lines:
                line_hint_verifications.append({
                    "file": fp, "line": ln, "anchor_found": False,
                    "reason": "file unreadable" if not existed else "file too large",
                })
            continue
        content_lines = content.splitlines()
        total_lines = len(content_lines)
        for ln in lines:
            if ln < 1 or ln > total_lines:
                line_hint_verifications.append({
                    "file": fp, "line": ln, "anchor_found": False,
                    "reason": "line out of range (file has " + str(total_lines) + " lines)",
                })
                continue
            lo = max(0, ln - 6)
            hi = min(total_lines, ln + 5)
            window = "\n".join(content_lines[lo:hi])
            anchor_found = any(pat.search(window) for pat in id_patterns.values())
            line_hint_verifications.append({
                "file": fp, "line": ln, "anchor_found": anchor_found,
            })

    total_ids = len(identifiers)
    total_hits = len(found_anywhere)
    hit_ratio = total_hits / total_ids if total_ids else 0.0

    # ADVISORY VERDICT — NEVER A HARD SKIP.
    # The execution-time caller (Phase 4-pre of aspirations-execute) treats
    # the whole probe as advisory: Phase 5 verification is ground truth and
    # will re-run regardless. The filing-time caller (goal-duplication-gate)
    # blocks ONLY on "already_present" (hit_ratio >= 0.75) AND exposes an
    # --override-duplication escape hatch. Do not add a fifth verdict, do
    # not raise the "absent" floor above 0, and do not lower the 0.75
    # threshold without revisiting both consumers. Fail-open = false
    # "unknown", never a false "already_present".
    if not any_readable:
        verdict = "unknown"
    elif hit_ratio >= 0.75:
        verdict = "already_present"
    elif total_hits > 0:
        verdict = "partially_present"
    else:
        verdict = "absent"

    return {
        "verdict": verdict,
        "per_file": per_file,
        "total_hits": total_hits,
        "total_identifiers": total_ids,
        "hit_ratio": round(hit_ratio, 3),
        "line_hint_verifications": line_hint_verifications,
    }
