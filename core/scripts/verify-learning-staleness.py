#!/usr/bin/env python3
"""verify-learning-staleness — detect stale Check:/Bash:/Parse: assertions.

Scans a SKILL.md (default verify-learning, or any path via --skill-md, or
the entire .claude/skills/*/SKILL.md surface via --all-skills) for
assertions that reference files, phases, grep patterns, CLI flags, or
response fields that no longer exist in the codebase. Catches the
"refactor moved the target but nobody updated the SKILL pseudocode"
failure mode.

Five detection lanes (each fails open on parse error):

  L1 — Path references:
    Any `core/scripts/foo.{py,sh}`, `.claude/skills/<n>/SKILL.md`,
    `core/config/<n>.{md,yaml}`, `world/conventions/<n>.md`,
    `meta/<n>.{yaml,jsonl}` mentioned inside a `Check:` or `Bash:` line
    must resolve to an existing file.

  L2 — Phase/Step references:
    A line that names a SKILL.md path AND a phrase like "Phase 8.5",
    "Step 8.77", "Phase 0.5.0a", or "Step 2.55" must be matchable by
    grep inside that file. Stale = the phase/step header doesn't appear.

  L3 — Grep-target references:
    A line of the form `Grep <FILE> for <PATTERN>` or `grep ... <FILE>`
    where PATTERN is in backticks must produce ≥1 match in FILE.

  L4 — Argparse-flag references (g-001-207):
    A `Bash: <script>.{sh,py} --flag <args>` invocation where `--flag`
    is a literal flag (not a `<placeholder>`). Stale = flag string is
    absent from the script's source AND from any python script the
    shell wrapper forwards via `exec python3 ... "$@"`. Catches the
    SKILL-pseudocode-vs-actual-script-API drift class (rb-412, rb-485,
    guard-359 cover the proactive author side; this is the post-write
    detection layer). Surfaced from felt-sense 2026-04-26 hitting
    `aspirations-query.sh --status` (actual flag is `--goal-status`).

  L5 — Response-field references (g-115-3607):
    A `Parse <var>: <names>` line whose <var> was assigned from a script
    in a nearby `Bash: <var>=$(... script ...)` line. Each name must
    appear as a quoted key in that script's source, or in anything it
    forwards to — including the daemon endpoint that registers the
    route a daemon-only wrapper `rt_call`s (such a wrapper emits nothing
    itself, so checking only the wrapper reports every field stale).
    Stale = the procedure reads a key the emitter never writes, so the
    read yields None and the branch silently takes the wrong path.

    SCOPE — this lane detects STATIC name drift only. It cannot see
    RUNTIME shape variance: an emitter with several return shapes writes
    every name somewhere in its source, so a name that is real but
    BRANCH-LOCAL passes this check while still reading None at runtime.
    That class (the one the originating goal actually hit — see
    curriculum.py's terminal-stage early return vs. its full return, and
    the shape tests in tests/test_curriculum.py) needs a per-shape
    contract test, not a name scan. Do not read a clean L5 as proof the
    parsed fields are populated on the path that actually runs.

    COVERAGE — measured 2026-07-28 over all 84 SKILL.md files: 46 lines
    begin with `Parse`, but only 4 name a variable bound by a
    `Bash: <var>=$(<script>)` line, and only 1 of those also names fields
    inline. So this lane's live ceiling is ~1-4 lines, and the binding
    constraint is the var->script LINKAGE, not the line-shape regex —
    widening the regex does not raise it. The lane is therefore mostly
    PROSPECTIVE (it catches the pattern as authors write it) rather than
    a way to drain existing drift. A 0-finding L5 result is the expected
    steady state, not evidence the corpus was audited.

Output: JSON to stdout. Exit 0 if 0 stale, exit 1 if any stale found.
Use --text for human summary on stdout.

Invocation:
  py -3 core/scripts/verify-learning-staleness.py
  py -3 core/scripts/verify-learning-staleness.py --text
  py -3 core/scripts/verify-learning-staleness.py --skill-md <path>
  py -3 core/scripts/verify-learning-staleness.py --all-skills
  py -3 core/scripts/verify-learning-staleness.py --all-skills --text
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_SKILL_MD = REPO_ROOT / ".claude" / "skills" / "verify-learning" / "SKILL.md"

sys.path.insert(0, str(SCRIPT_DIR))
import _verify_corpus  # noqa: E402

# Regexes anchored to the framework's path conventions. Deliberately
# narrow — we only flag references with known framework prefixes to avoid
# false positives on prose like "the script foo.py" without context.
#
# CRITICAL: `meta/` and `world/` are EXTERNAL paths configured per-agent
# in <agent>/local-paths.conf and not committed to the local repo. They
# are deliberately excluded from L1 — checking their existence against
# REPO_ROOT would always fail. (See CLAUDE.md "External paths" section.)
_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"((?:core/scripts|core/config|core/config/conventions|core/config/rationale|"
    r"core/config/modes|"
    r"\.claude/skills/[a-z0-9-]+|\.claude/rules)"
    r"/[A-Za-z0-9_.-]+\.[a-z]+)"
)

# `Grep Phase X for \`pattern\` must match` / `Grep Step X for ...` —
# the Check: line will also mention a SKILL.md path, which we resolve
# via _PATH_RE; the Phase/Step is then the section anchor inside that
# SKILL.md. Stale detection: pattern absent from the WHOLE skill file
# (don't try to bound by section — too brittle, and a moved-out string
# usually moved to a different file entirely).
_GREP_PHASE_RE = re.compile(
    r"[Gg]rep\s+(?:Phase|Step)\s+[\d.]+[a-zA-Z]*\s+for\s+`([^`]+)`"
)

# Phase/Step phrases. Matches "Phase 0.5.0a", "Step 8.77", "Step 2.55",
# "Phase 9.5d". Captures the leading verb (Phase|Step) and the identifier.
_PHASE_RE = re.compile(r"\b(Phase|Step)\s+([\d.]+[a-zA-Z]*)\b")

# A `Check:` or `Bash:` line is what we evaluate. Markdown bullet `-` or
# leading whitespace allowed.
_CHECK_LINE_RE = re.compile(r"^\s*-?\s*(Check|Bash):\s*(.+)$")

# L5: `Parse <var>: <field>, <field>, ...` — the field-name lane ().
# DELIBERATELY a SEPARATE regex rather than a third alternation inside
# _CHECK_LINE_RE. Adding `Parse` there would route these lines into all four
# existing lanes (paths/phases/grep-target/grep-phase) and inflate
# `assertions_scanned`, changing the meaning of an already-published metric to
# fix an unrelated blind spot. A separate matcher keeps the blast radius at
# zero: the existing lanes see exactly what they saw before.
#
# The identifier must be IMMEDIATELY followed by the colon, which is what keeps
# prose out: "Parse the output: it contains foo" fails (after `the` comes a
# space then `output`, not `:`), while "Parse eval_json: configured, ..." matches.
_PARSE_LINE_RE = re.compile(r"^\s*-?\s*Parse\s+([A-Za-z_]\w*)\s*:\s*(.+)$")

# L5: capture the VARIABLE NAME from a command-substitution assignment so a
# later `Parse <var>:` can be linked back to the script that produced it.
# _BASH_SCRIPT_RE's own `var=$(` prefix is NON-capturing, and its comment
# (rb-3437 / guard-1081) warns that widening that prefix must not weaken its
# tail guard — so this is a separate expression rather than an edit to it.
_BASH_ASSIGN_RE = re.compile(
    r"^\s*-?\s*Bash:\s+"
    r"([A-Za-z_]\w*)=\$\(\s*"                  # var name (capture 1)
    r"(?:bash\s+|py\s+-3\s+|python\d?\s+)?"    # optional runner prefix
    r"(\S+\.(?:sh|py))\b"                      # script path (capture 2)
)

# L5: a field token inside a `Parse` body. Bare identifiers only — prose words
# are filtered by the emitted-key check, not here.
_FIELD_TOKEN_RE = re.compile(r"\b([a-z_][a-z0-9_]{2,})\b")

# L5: field names that are English prose in practice, never JSON keys. Without
# this the lane reports the connective tissue of the sentence.
_FIELD_STOPWORDS = frozenset({
    "and", "the", "for", "from", "with", "into", "then", "else", "when",
    "each", "any", "all", "not", "via", "per", "its", "are", "was", "has",
    "plus", "also", "only", "must", "may", "can", "see", "run", "use",
    "json", "output", "stdout", "field", "fields", "value", "values", "key",
    "keys", "parse", "read", "true", "false", "null", "none", "etc",
})

# `Grep <FILE> for <PATTERN>` — PATTERN is backticked.
# Variant form: `grep ... <PATTERN> ... <FILE>` — harder to parse
# reliably; we only handle the explicit "Grep <FILE> for `pattern`" form
# plus the common "Grep <PATH> for `<phrase>` must match" idiom used in
# the existing checklist.
_GREP_TARGET_RE = re.compile(
    r"[Gg]rep\s+([A-Za-z0-9_./-]+\.[a-z]+|[A-Za-z0-9_-]+/SKILL\.md|"
    r"`[^`]+`)"
    r"\s+for\s+`([^`]+)`"
)

# L4: Bash: <script>.sh|<script>.py invocation extraction.
# Matches `Bash: <var=$(>? <runner>? <full-script-path>.{sh,py} <args-until-shell-meta-or-paren>`.
# Captures the full path and the args-tail. An OPTIONAL command-substitution
# assignment prefix (`var=$(`) is consumed BEFORE the runner so the very common
# dedup-guard form `Bash: existing=$(bash <script> --flag ...)` is scanned —
# without it the whole line was skipped and stale flags on the highest-stakes
# call sites (dedup guards whose false-empty result fails open into filing a
# DUPLICATE goal) were never checked (). The args capture STILL STOPS
# at the first shell metacharacter (`|`, `&`, `;`, `>`, `<`) OR opening paren `(`
# so flags belonging to (a) a downstream piped command (e.g., `grep -q`
# after `script.sh ... |`) or (b) parenthesized prose comments (e.g.,
# `wm-read.sh encoding_queue --json  (if --selective mode)`) are NOT
# mis-attributed to the script. Widening the PREFIX must never weaken this TAIL
# guard (rb-3437 / guard-1081 — a raise/guard is an interface; grep for what
# depends on current behavior before loosening). Quoted scripts (`'foo.sh'`)
# and backtick command substitution are not matched — pseudocode rarely uses
# them; a known limitation. Quote-aware arg parsing is also out of scope (a
# regex pattern with `(` inside `"..."` would be truncated; rare enough to accept).
_BASH_SCRIPT_RE = re.compile(
    r"^\s*-?\s*Bash:\s+"
    r"(?:[A-Za-z_]\w*=\$\(\s*)?"               # optional command-sub prefix: var=$(
    r"(?:bash\s+|py\s+-3\s+|python\d?\s+)?"    # optional runner prefix
    r"(\S+\.(?:sh|py))\b"                      # full script path (capture 1)
    r"(\s[^|&;<>(\n]*)?"                        # args tail, stops at shell meta or `(`
)

# L4: literal flag tokens. Matches `--name` or `-x` not preceded by `<`,
# `{`, alphanumeric, or `_` (i.e., not inside a placeholder or token).
# Excludes uppercase-led flags (rare in CLI conventions; reduces false
# positives on prose like "X--Y" or `<--marker>`).
_LITERAL_FLAG_RE = re.compile(
    r"(?<![<{a-zA-Z0-9_])(-{1,2}[a-z][a-z0-9-]*)\b"
)

# L4: detect `exec python3 <path>.py` or `python3 "$VAR/path/foo.py"`
# patterns inside shell wrappers. The path may be quoted ("$CORE_ROOT/..."),
# variable-prefixed ($CORE_ROOT/...), or bare. We match through any
# non-whitespace prefix to capture the .py basename. When a shell wrapper
# has no flag in its own body, check whether it forwards to a python
# script — if so, follow the forward and check that script too.
_FORWARD_PY_RE = re.compile(
    r"(?:python3?|py)\s+(?:-3\s+)?"
    r"\S*?([a-z][a-z0-9_-]*\.py)\b"
)

# L4: detect `exec "$..."/<basename>.sh "$@"` shell-wrapper-to-shell
# delegations. Without this, wrappers like agent-aspirations-read.sh
# (which `exec`s aspirations-read.sh) produce false-positive stale
# findings — the flag lives in the underlying sibling, not the wrapper.
#  (2026-05-22).
_FORWARD_SH_RE = re.compile(
    r"^\s*exec\s+.*?/?([a-z][a-z0-9_-]*\.sh)\b",
    re.MULTILINE,
)

# L5: detect `rt_call <METHOD> /v1/<route>` daemon delegation ().
# 35+ wrappers are DAEMON-ONLY (.claude/rules/no-python-cli-fallback.md): they
# emit NOTHING themselves and forward to an HTTP endpoint under
# mind_api/src/endpoints/. Without this leg the response-field lane reports
# every field of every daemon-backed wrapper as missing — measured on
# curriculum-evaluate.sh, where the live output demonstrably contains
# "configured" and "all_passed" while the wrapper's own source contains
# neither. That is a false positive on the single largest wrapper class in the
# repo, and a lane that cries wolf on 35 wrappers gets muted and then deleted.
_FORWARD_RT_RE = re.compile(
    r"rt_call\s+(?:GET|POST|PUT|PATCH|DELETE)\s+(/v1/[A-Za-z0-9_/-]+)"
)

# L5: root the rt_call leg searches for a route registration. This is
# `mind_api/src` RECURSIVELY, not `mind_api/src/endpoints` — the daemon source
# is split into subpackages and routes register in several of them (measured:
# /v1/curriculum/evaluate lives in endpoints/curriculum.py, but
# /v1/team-state/read lives in world/team_state.py). Globbing only endpoints/
# resolved the first and silently missed the second, reporting live
# team-state fields as stale. A directory walk rather than a route->file table
# so a new endpoint module needs no edit here — a hand-maintained table is
# exactly the stale-reference class this scanner exists to catch.
_DAEMON_SRC_DIR = "mind_api/src"

# Max continuation lines absorbed into one wrapped `Parse <var>:` field list.
# A runaway guard, not a real limit — the longest corpus instance wraps once.
_MAX_PARSE_CONTINUATION = 5

# Route -> registering modules. The walk is recursive over the daemon source,
# so cache it: without this the scan re-reads every module for every field of
# every Parse line.
_ROUTE_CACHE: dict[str, list[Path]] = {}

# L5: follow a python module's imports one more hop. An endpoint frequently
# does not build the response dict itself — it delegates to a shared compose
# module (canonical: /v1/team-state/read -> endpoints/*.py -> core/scripts/
# _team_state.py, which CLAUDE.md names the routing/compose SSOT and which is
# where "agent_status" actually lives). Without this leg the lane reports a
# real field as missing purely because the key is one module deeper than the
# route handler.
_FORWARD_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import\b|import\s+([A-Za-z_][\w.]*))",
    re.MULTILINE,
)

# L5: extra search roots for a module name resolved out of an import.
_MODULE_SEARCH_DIRS = ("core/scripts", "mind_api/src", "mind_api/src/endpoints")


def load_skill_md(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, raw_line)] from the verify-learning SKILL.md."""
    # The verify-learning check corpus moved to a registry on 2026-08-18
    # (); reading the now-175-line file drops assertions_scanned
    # from 2,517 to 6. The corpus is byte-identical to the pre-cutover file,
    # so the line numbers this function hands out stay exactly as they were.
    # Every OTHER skill still reads from disk — this scanner globs all of them.
    if path.resolve() == DEFAULT_SKILL_MD.resolve():
        return list(enumerate(_verify_corpus.corpus_lines(), start=1))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"[verify-learning-staleness] cannot read {path}: {e}",
              file=sys.stderr)
        return []
    return list(enumerate(text.splitlines(), start=1))


def extract_assertions(lines: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
    """Return [(lineno, kind, body)] for every Check:/Bash: line."""
    out = []
    for lineno, raw in lines:
        m = _CHECK_LINE_RE.match(raw)
        if m:
            out.append((lineno, m.group(1), m.group(2)))
    return out


# Negative-assertion phrases. When the Check: body contains any of these,
# the missing artifact IS the expected state — flagging it as stale would
# be a false positive. Match case-sensitively because "must not" and
# "MUST NOT" carry different verification weights elsewhere in the
# codebase, but for guard purposes either is sufficient evidence of a
# negative assertion.
_NEGATIVE_PHRASES = (
    "does NOT exist",
    "does not exist",
    "DOES NOT exist",
    "must not exist",
    "MUST NOT exist",
    "MUST NOT contain",
    "must not contain",
    "is absent",
    "absent.",  # "Phase X absent."
    "absent (",
    "deleted deliberately",
    "deleted)",
    "has been deleted",
    "have been deleted",
    "removed)",
    "has been removed",
    "have been removed",
    "test ! -f",
    "test ! -d",
    "echo FAIL || echo PASS",  # the inverted-success idiom
    "&& echo FAIL ||",          # ditto
    "must not",
    "MUST NOT",
    "do NOT",
    "DO NOT",
    "does NOT",
    "DOES NOT",
    "no longer exist",
    "should not exist",
)


def is_negative_assertion(body: str) -> bool:
    """Return True if the assertion EXPECTS the artifact to be missing."""
    return any(phrase in body for phrase in _NEGATIVE_PHRASES)


def check_paths(lineno: int, body: str) -> list[dict]:
    """Lane 1: every framework path mentioned must resolve.
    Skips bodies that assert non-existence (negative assertions)."""
    if is_negative_assertion(body):
        return []
    findings = []
    for m in _PATH_RE.finditer(body):
        rel_path = m.group(1)
        abs_path = REPO_ROOT / rel_path
        if not abs_path.exists():
            findings.append({
                "line": lineno,
                "lane": "L1_path",
                "stale_ref": rel_path,
                "detail": f"path {rel_path!r} does not exist",
                "body": body[:160],
            })
    return findings


def check_phases(lineno: int, body: str) -> list[dict]:
    """Lane 2: every Phase/Step reference next to a SKILL.md path must
    be matchable inside that file. Skips negative-assertion bodies."""
    if is_negative_assertion(body):
        return []
    findings = []
    paths = [m.group(1) for m in _PATH_RE.finditer(body)
             if m.group(1).endswith("SKILL.md")]
    if not paths:
        return findings
    phases = list(_PHASE_RE.finditer(body))
    if not phases:
        return findings
    for skill_rel in paths:
        skill_abs = REPO_ROOT / skill_rel
        if not skill_abs.exists():
            continue  # Lane 1 will already flag this
        try:
            skill_text = skill_abs.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pm in phases:
            verb, ident = pm.group(1), pm.group(2)
            patterns = [
                rf"\b{verb}\s+{re.escape(ident)}\b",
                rf"##+\s*{verb}\s+{re.escape(ident)}\b",
            ]
            if not any(re.search(p, skill_text) for p in patterns):
                findings.append({
                    "line": lineno,
                    "lane": "L2_phase",
                    "stale_ref": f"{verb} {ident} in {skill_rel}",
                    "detail": (
                        f"{verb} {ident!r} not found in {skill_rel} — "
                        f"phase/step was renumbered, removed, or extracted"
                    ),
                    "body": body[:160],
                })
    return findings


def check_grep_phase(lineno: int, body: str) -> list[dict]:
    r"""Lane 3b: `Grep Phase X for \`pattern\` must match` — the SKILL.md
    target is mentioned earlier in the same Check: line. Stale = pattern
    absent from that file. Skips negative-assertion bodies."""
    if is_negative_assertion(body):
        return []
    findings = []
    skill_paths = [m.group(1) for m in _PATH_RE.finditer(body)
                   if m.group(1).endswith("SKILL.md")]
    if not skill_paths:
        return findings
    for gm in _GREP_PHASE_RE.finditer(body):
        pattern = gm.group(1)
        for skill_rel in skill_paths:
            skill_abs = REPO_ROOT / skill_rel
            if not skill_abs.exists():
                continue
            try:
                text = skill_abs.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                hit = bool(re.search(pattern, text))
            except re.error:
                hit = pattern in text
            if not hit:
                findings.append({
                    "line": lineno,
                    "lane": "L3b_grep_phase",
                    "stale_ref": f"`{pattern}` in {skill_rel}",
                    "detail": (
                        f"`{pattern}` not found anywhere in {skill_rel} — "
                        f"likely extracted to a script or removed"
                    ),
                    "body": body[:160],
                })
    return findings


def check_grep_targets(lineno: int, body: str) -> list[dict]:
    """Lane 3: `Grep <FILE> for <PATTERN>` must produce ≥1 match.
    Skips negative-assertion bodies."""
    if is_negative_assertion(body):
        return []
    findings = []
    for m in _GREP_TARGET_RE.finditer(body):
        target_raw = m.group(1).strip("`")
        pattern = m.group(2)
        # Resolve target — accept relative path or basename
        candidates = []
        if "/" in target_raw or "\\" in target_raw:
            candidates.append(REPO_ROOT / target_raw)
        else:
            for sub in (".claude/skills", "core/scripts", "core/config",
                        "world/conventions"):
                p = REPO_ROOT / sub
                if p.exists():
                    candidates.extend(p.rglob(target_raw))
        target_path = None
        for cand in candidates:
            if cand.exists():
                target_path = cand
                break
        if target_path is None:
            findings.append({
                "line": lineno,
                "lane": "L3_grep",
                "stale_ref": f"target {target_raw!r}",
                "detail": f"grep target {target_raw!r} could not be resolved",
                "body": body[:160],
            })
            continue
        try:
            text = target_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            if not re.search(pattern, text):
                findings.append({
                    "line": lineno,
                    "lane": "L3_grep",
                    "stale_ref": f"`{pattern}` in {target_raw}",
                    "detail": (
                        f"grep pattern {pattern!r} returns 0 matches in "
                        f"{target_path.relative_to(REPO_ROOT)}"
                    ),
                    "body": body[:160],
                })
        except re.error:
            # Treat as literal string when not a valid regex.
            if pattern not in text:
                findings.append({
                    "line": lineno,
                    "lane": "L3_grep",
                    "stale_ref": f"`{pattern}` in {target_raw}",
                    "detail": (
                        f"literal {pattern!r} not found in "
                        f"{target_path.relative_to(REPO_ROOT)}"
                    ),
                    "body": body[:160],
                })
    return findings


_SCRIPT_SEARCH_DIRS = (
    "core/scripts",
    "world/scripts",
)


def _resolve_script(basename: str) -> Path | None:
    """Return repo-relative Path to the script, or None if not found.
    Searches core/scripts/ and world/scripts/ in order."""
    for sub in _SCRIPT_SEARCH_DIRS:
        cand = REPO_ROOT / sub / basename
        if cand.is_file():
            return cand
    return None


def _script_contains_any(script_path: Path, needles: tuple[str, ...],
                         max_hops: int = 3) -> bool:
    """Return True if ANY needle appears literally in script_path, OR in any
    script it forwards to via `exec python3 ... <forwarded>.py` / the shell
    equivalent. Follows up to 2 hops to keep runtime bounded; in practice
    wrappers forward at most once.

    Extracted from _script_accepts_flag (g-115-3607) so the L4 flag lane and
    the L5 response-field lane share ONE traversal. Two live call sites, so
    this is not a single-use abstraction — and a divergent second copy of the
    forwarding walk is exactly how one lane silently stops following wrappers
    while the other keeps working."""
    visited: set[Path] = set()
    queue: list[Path] = [script_path]
    hops = 0
    while queue and hops < max_hops:
        path = queue.pop()
        if path in visited:
            continue
        visited.add(path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(n in text for n in needles):
            return True
        if path.suffix == ".sh":
            for m in _FORWARD_PY_RE.finditer(text):
                forwarded = _resolve_script(m.group(1))
                if forwarded and forwarded not in visited:
                    queue.append(forwarded)
            for m in _FORWARD_SH_RE.finditer(text):
                forwarded = _resolve_script(m.group(1))
                if forwarded and forwarded not in visited:
                    queue.append(forwarded)
            for m in _FORWARD_RT_RE.finditer(text):
                for ep in _resolve_endpoints(m.group(1)):
                    if ep not in visited:
                        queue.append(ep)
        elif path.suffix == ".py":
            for m in _FORWARD_IMPORT_RE.finditer(text):
                mod = (m.group(1) or m.group(2) or "").split(".")[-1]
                if not mod:
                    continue
                resolved = _resolve_module(mod)
                if resolved and resolved not in visited:
                    queue.append(resolved)
        hops += 1
    return False


def _resolve_module(mod: str) -> Path | None:
    """Resolve a bare python module name to a repo file, or None.

    Searched only for the L5 walk's import leg; stdlib and third-party names
    simply fail to resolve and are skipped, which is the intended behaviour —
    the goal is to reach in-repo compose modules, not to model the import
    system."""
    for sub in _MODULE_SEARCH_DIRS:
        cand = REPO_ROOT / sub / f"{mod}.py"
        if cand.is_file():
            return cand
    return None


def _resolve_endpoints(route: str) -> list[Path]:
    """Return endpoint modules that register `route`.

    A daemon-only wrapper's only self-description is its `rt_call <M> <route>`;
    the keys it appears to emit are actually written by the endpoint that
    registers that route. Rather than maintain a route->file table (which would
    itself go stale — the exact failure class this scanner exists to detect),
    resolve by searching the endpoints directory for the route literal. The
    registration line `routes[("POST", "/v1/curriculum/evaluate")] = evaluate`
    contains it verbatim, so the match is exact, not heuristic.

    The search is RECURSIVE over the whole daemon source, not just
    `endpoints/`: measured, `/v1/curriculum/evaluate` registers in
    `endpoints/curriculum.py` but `/v1/team-state/read` registers in
    `world/team_state.py` (wired in via `_team_state.register(routes)`). A
    non-recursive glob resolved the first and returned [] for the second,
    which the caller could not distinguish from "no endpoint emits this
    field" — reporting live team-state fields as stale.

    Returns [] when the source tree is absent so a deployment without the
    daemon source degrades to the pre-existing behaviour instead of erroring."""
    if route in _ROUTE_CACHE:
        return _ROUTE_CACHE[route]
    src_dir = REPO_ROOT / _DAEMON_SRC_DIR
    if not src_dir.is_dir():
        _ROUTE_CACHE[route] = []
        return []
    # Both quote styles: registration tables are written either way.
    needles = (f'"{route}"', f"'{route}'")
    out = []
    for cand in sorted(src_dir.rglob("*.py")):
        try:
            body = cand.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(n in body for n in needles):
            out.append(cand)
    _ROUTE_CACHE[route] = out
    return out


def _script_accepts_flag(script_path: Path, flag: str) -> bool:
    """Return True if the literal flag string appears in script_path,
    OR in any python script that script_path forwards to via
    `exec python3 ... <forwarded>.py`. Follows up to 2 hops to keep
    runtime bounded; in practice wrappers forward at most once.

    Match policy: literal substring search. Argparse `add_argument`
    calls and shell `case` clauses both contain the literal flag
    string, and false-positive risk (mention in a comment that doesn't
    correspond to actual support) is acceptable for this lane —
    findings are advisory, not blocking."""
    return _script_contains_any(script_path, (flag,))


def _script_emits_field(script_path: Path, field: str) -> bool:
    """Return True if `field` plausibly appears as a JSON KEY emitted by
    script_path (or anything it forwards to).

    Match policy is deliberately TIGHTER than the flag lane's bare-substring
    test: the needle is the QUOTED form (`"field"` / `'field'`), because a bare
    identifier like `gates` occurs in prose, comments, and local variable names
    all over a script, which would make the lane report PASS on names the
    script never emits — the exact false-negative that lets the drift through.
    Quoting is what makes the match evidence of a key rather than of a mention.

    Both quote styles are accepted: the emitter may be a python dict literal
    (either style), a json.dumps of a literal, or a shell heredoc. `f"{field}"`
    dynamic key construction is NOT detected — an accepted limitation, and the
    reason this lane is advisory."""
    # max_hops=6, NOT the default 3. The L5 chain is one level deeper than
    # L4's: wrapper.sh -> endpoint.py -> compose-module.py is already 3 pops,
    # and the DFS queue can hold several endpoints when a wrapper carries more
    # than one rt_call, so a budget of exactly 3 runs out before the module is
    # reached and reports a live field as missing. Raised HERE rather than on
    # the shared default so the L4 flag lane keeps its measured behaviour
    # byte-for-byte — a bound is an interface (rb-3437 / guard-1081), and
    # loosening it for L4 would silently turn real flag findings into passes.
    return _script_contains_any(script_path, (f'"{field}"', f"'{field}'"),
                                max_hops=6)


def check_argparse_flags(lineno: int, body: str) -> list[dict]:
    """Lane 4: every `--flag` mentioned in a `Bash: <script>.sh` line
    must appear in the script's source (or in any python script the
    shell wrapper forwards to). Skips negative-assertion bodies.

    The body of the Bash: line was already split off by
    extract_assertions; reconstruct the full line for matching."""
    if is_negative_assertion(body):
        return []
    full_line = f"Bash: {body}"
    m = _BASH_SCRIPT_RE.match(full_line)
    if not m:
        return []
    script_full = m.group(1)
    # Strip trailing shell-meta (rare — regex anchors via `\b` already)
    # and extract basename from the full path. Handle both POSIX `/`
    # and Windows `\` separators since SKILL pseudocode is path-agnostic.
    script_basename = script_full.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    rest = (m.group(2) or "").strip()
    if not rest:
        return []
    script_path = _resolve_script(script_basename)
    if script_path is None:
        # L1 already flags missing scripts via the path lane; don't
        # double-report under L4.
        return []
    findings = []
    seen_flags: set[str] = set()
    for fm in _LITERAL_FLAG_RE.finditer(rest):
        flag = fm.group(1)
        if flag in seen_flags:
            continue
        seen_flags.add(flag)
        if not _script_accepts_flag(script_path, flag):
            findings.append({
                "line": lineno,
                "lane": "L4_argparse_flag",
                "stale_ref": f"{flag} on {script_basename}",
                "detail": (
                    f"flag {flag!r} not found in {script_basename} "
                    f"or any forwarded python script — "
                    f"argparse drift or wrapper-API mismatch"
                ),
                "body": body[:160],
            })
    return findings


def extract_var_script_map(lines: list[tuple[int, str]]) -> dict[str, str]:
    """Return {var_name: script_basename} for every `Bash: var=$(... script)`
    assignment. L5 uses it to link a later `Parse <var>:` back to the script
    whose output is being parsed.

    Last assignment wins: pseudocode reassigns a scratch name like `existing`
    in many phases, and the nearest preceding definition is the relevant one
    for lines that follow it. A whole-file map is an approximation of that —
    accepted, because the alternative (scoping by phase) needs a phase parser
    this lane does not have, and a wrong-script link degrades to a finding the
    reader can dismiss rather than to a silent miss."""
    out: dict[str, str] = {}
    for _lineno, raw in lines:
        m = _BASH_ASSIGN_RE.match(raw)
        if m:
            script_full = m.group(2)
            basename = script_full.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            out[m.group(1)] = basename
    return out


def extract_parse_lines(lines: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
    """Return [(lineno, var, body)] for every `Parse <var>: ...` line, joining
    WRAPPED field lists.

    A field list long enough to be worth checking is long enough to wrap, and
    both real instances in the corpus do:

        Parse eval_json: configured, error, all_passed, gates,
                         current_stage, terminal_stage, stage_name, next_stage

    Matching per-line captured only through the trailing comma and silently
    dropped the rest — so 4 of those 8 fields were never checked, in the lane
    built to check exactly that line (found by fresh-eyes on this file,
    g-115-3607). Under-coverage here is invisible: fewer fields checked simply
    means fewer findings, which reads identically to clean.

    Continuation rule: while the accumulated body ends in a comma, absorb the
    next line. The trailing comma is the author's own explicit "this list is
    not finished" marker — far safer than an indentation heuristic, which
    would swallow the following `# comment` or `Bash:` line whenever a list
    happened to end at a line boundary. Bounded at _MAX_PARSE_CONTINUATION
    lines so a stray comma cannot consume the rest of the file."""
    out = []
    pending = None  # (lineno, var, [body parts]) awaiting continuation
    for lineno, raw in lines:
        if pending is not None:
            start_no, var, parts = pending
            stripped = raw.strip()
            if (stripped
                    and len(parts) <= _MAX_PARSE_CONTINUATION
                    and not _PARSE_LINE_RE.match(raw)
                    and not _CHECK_LINE_RE.match(raw)
                    and not stripped.startswith("#")):
                parts.append(stripped)
                if stripped.endswith(","):
                    continue
            out.append((start_no, var, " ".join(parts)))
            pending = None
        m = _PARSE_LINE_RE.match(raw)
        if m:
            body = m.group(2).strip()
            if body.endswith(","):
                pending = (lineno, m.group(1), [body])
            else:
                out.append((lineno, m.group(1), body))
    if pending is not None:
        start_no, var, parts = pending
        out.append((start_no, var, " ".join(parts)))
    return out


def check_response_fields(lineno: int, var: str, body: str,
                          var_script_map: dict[str, str]) -> list[dict]:
    """Lane 5 (): every field name a `Parse <var>:` line claims to
    read out of a script's JSON output must actually appear as a key in that
    script's source.

    This closes a blind spot that made a whole drift class structurally
    undetectable: the scanner verified paths, phase headers, grep patterns and
    argparse flags, but never the RESPONSE SHAPE a procedure depends on. A
    SKILL.md step that parses four field names the script stopped emitting runs
    green forever — every read yields None, the branch silently takes the wrong
    path, and the scanner's clean result reads as coverage of the very thing it
    never looked at.

    Skips (each a deliberate precision choice, since a noisy advisory lane gets
    ignored and then deleted):
      - negative assertions, same as every other lane
      - unknown vars: no `Bash: var=$(script)` assignment was seen, so there is
        no output to check against. Unverifiable is NOT stale.
      - unresolvable scripts: L1's path lane already reports those; reporting
        again here would double-count one defect.
      - stopwords and <3-char tokens: prose connectives, not keys."""
    if is_negative_assertion(body):
        return []
    script_basename = var_script_map.get(var)
    if script_basename is None:
        return []
    script_path = _resolve_script(script_basename)
    if script_path is None:
        return []
    findings = []
    seen: set[str] = set()
    for fm in _FIELD_TOKEN_RE.finditer(body):
        field = fm.group(1)
        if field in seen or field in _FIELD_STOPWORDS:
            continue
        seen.add(field)
        if not _script_emits_field(script_path, field):
            findings.append({
                "line": lineno,
                "lane": "L5_response_field",
                "stale_ref": f"{field} from {script_basename}",
                "detail": (
                    f"field {field!r} is parsed out of ${var} but never appears "
                    f"as a quoted key in {script_basename} (or anything it "
                    f"forwards to) — response-shape drift: the read yields "
                    f"None and the branch silently takes the wrong path"
                ),
                "body": body[:160],
            })
    return findings


def scan(skill_md: Path) -> dict:
    """Run all five lanes and return a result dict."""
    lines = load_skill_md(skill_md)
    assertions = extract_assertions(lines)
    var_script_map = extract_var_script_map(lines)
    parse_lines = extract_parse_lines(lines)
    findings = []
    for lineno, var, body in parse_lines:
        findings.extend(check_response_fields(lineno, var, body, var_script_map))
    for lineno, kind, body in assertions:
        findings.extend(check_paths(lineno, body))
        findings.extend(check_phases(lineno, body))
        findings.extend(check_grep_targets(lineno, body))
        findings.extend(check_grep_phase(lineno, body))
        # L4 only fires on Bash: lines — Check: lines that mention an
        # inner Bash snippet (e.g., `Check: foo.py works. Bash: \`grep -c ...\``)
        # would otherwise mis-attribute the inner command's flags
        # (here `-c` belongs to grep, not foo.py).
        if kind == "Bash":
            findings.extend(check_argparse_flags(lineno, body))
    return {
        "skill_md": str(skill_md),
        "assertions_scanned": len(assertions),
        # Reported SEPARATELY, not folded into assertions_scanned: that field
        # has an established meaning (Check:/Bash: lines) and silently changing
        # what it counts would corrupt any trend read across the change.
        "parse_lines_scanned": len(parse_lines),
        "stale_count": len(findings),
        "findings": findings,
    }


def scan_all_skills() -> dict:
    """Iterate every .claude/skills/<name>/SKILL.md and aggregate
    findings. Each finding carries an extra `skill` field naming the
    skill directory it came from. Per-skill counts are also included
    in `per_skill` for at-a-glance triage."""
    skills_dir = REPO_ROOT / ".claude" / "skills"
    per_skill = {}
    aggregate_findings = []
    total_assertions = 0
    total_parse_lines = 0
    if not skills_dir.is_dir():
        return {
            "mode": "all-skills",
            "skills_scanned": 0,
            "assertions_scanned_total": 0,
            "parse_lines_scanned_total": 0,
            "stale_count_total": 0,
            "per_skill": {},
            "findings": [],
            "error": f"{skills_dir} not found",
        }
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        skill_name = skill_md.parent.name
        result = scan(skill_md)
        per_skill[skill_name] = {
            "skill_md": result["skill_md"],
            "assertions_scanned": result["assertions_scanned"],
            # Carried into the aggregate so L5's coverage is visible in the
            # mode the recurring audit actually runs (--all-skills). Without
            # it the lane can scan zero lines corpus-wide and report the same
            # clean result as a lane that scanned every one.
            "parse_lines_scanned": result["parse_lines_scanned"],
            "stale_count": result["stale_count"],
        }
        total_assertions += result["assertions_scanned"]
        total_parse_lines += result["parse_lines_scanned"]
        for f in result["findings"]:
            f_with_skill = dict(f)
            f_with_skill["skill"] = skill_name
            aggregate_findings.append(f_with_skill)
    return {
        "mode": "all-skills",
        "skills_scanned": len(per_skill),
        "assertions_scanned_total": total_assertions,
        "parse_lines_scanned_total": total_parse_lines,
        "stale_count_total": len(aggregate_findings),
        "per_skill": per_skill,
        "findings": aggregate_findings,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skill-md", type=Path, default=DEFAULT_SKILL_MD,
                    help="Path to a single SKILL.md (default: verify-learning/SKILL.md). "
                         "Ignored when --all-skills is set.")
    ap.add_argument("--all-skills", action="store_true",
                    help="Scan every .claude/skills/<name>/SKILL.md and aggregate "
                         "findings. Each finding gains a `skill` field. Use this to "
                         "catch SKILL-pseudocode-vs-script-API drift across the whole "
                         "skill surface, not just verify-learning.")
    ap.add_argument("--text", action="store_true",
                    help="Human-readable summary on stdout (JSON on stderr).")
    args = ap.parse_args()

    if args.all_skills:
        result = scan_all_skills()
        stale_total = result["stale_count_total"]
        if args.text:
            print(json.dumps(result, indent=2), file=sys.stderr)
            # Parse-lines reported alongside assertions on BOTH summaries: a
            # lane absent from the coverage line is a lane nobody notices
            # scanned nothing (the human-facing half of the same gap fixed in
            # the JSON aggregate —  fresh-eyes F-2).
            print(f"verify-learning-staleness (all-skills): scanned "
                  f"{result['skills_scanned']} skills / "
                  f"{result['assertions_scanned_total']} assertions / "
                  f"{result['parse_lines_scanned_total']} parse-lines, "
                  f"found {stale_total} stale")
            for f in result["findings"]:
                print(f"  [{f['lane']}] {f['skill']}/SKILL.md "
                      f"line {f['line']}: {f['stale_ref']}")
                print(f"    {f['detail']}")
        else:
            print(json.dumps(result))
        return 0 if stale_total == 0 else 1

    result = scan(args.skill_md)

    if args.text:
        print(json.dumps(result, indent=2), file=sys.stderr)
        print(f"verify-learning-staleness: scanned "
              f"{result['assertions_scanned']} assertions / "
              f"{result['parse_lines_scanned']} parse-lines, "
              f"found {result['stale_count']} stale")
        for f in result["findings"]:
            print(f"  [{f['lane']}] line {f['line']}: {f['stale_ref']}")
            print(f"    {f['detail']}")
    else:
        print(json.dumps(result))

    return 0 if result["stale_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
