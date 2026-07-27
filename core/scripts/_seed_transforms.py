# domain-leak-exempt: regex pattern strings for transformation engine include literal AyoAI/Ayoai/Zachary tokens by design
"""Transformation engine for /seed plant.

Applies four transformation types to file contents at copy time:
  1. file_replace      — full content swap from seed-templates/
  2. inline_edit       — per-file pattern/replacement
  3. global_regex      — repo-wide regex
  4. word_list_strip   — strip specific words when in comments/example contexts

Priority (highest first): file_replace > inline_edit > global_regex > word_list_strip.
file_replace short-circuits other rules for that file; inline_edit chains with G/W.

Context guards (`when_in_context`):
  - "comment"        — line is a comment (# in py/sh; <!-- --> in md; YAML # comment)
  - "example_value"  — inside a YAML 'example:' value or 'e.g.' prose marker
  - "example_marker" — line contains 'example:', 'e.g.', 'for example' nearby

`applies_to` is a list of glob patterns matched via fnmatch on the repo-relative
path with forward-slash normalization. `excludes_paths` likewise.

The engine is content-only: it takes file content + path and returns transformed
content. The caller handles file I/O and staging.
"""
import fnmatch
import re
from pathlib import Path, PurePosixPath


# ---------- path matching ----------

def _norm_rel(rel_path: str) -> str:
    """Normalize a repo-relative path to forward-slash form for glob matching."""
    return str(PurePosixPath(rel_path.replace("\\", "/")))


def _glob_to_regex(pattern: str) -> str:
    """Convert a gitignore-flavor glob to a regex (no anchors).

    Semantics:
      - `**`     → any sequence of characters including `/`
      - `**/`    → zero or more directory components (i.e., `(?:.*/)?`)
      - `*`      → any sequence of NON-slash characters
      - `?`      → any single non-slash character
      - trailing `/` → "anything under this directory" (the caller normally
        appends `**` before calling)
      - other regex specials are escaped
    """
    # Tokenize: walk char-by-char, emit regex fragments.
    i = 0
    n = len(pattern)
    out = []
    while i < n:
        c = pattern[i]
        # Check for ** with optional trailing /
        if c == "*" and i + 1 < n and pattern[i + 1] == "*":
            # Lookahead: is the next char after ** a /?
            if i + 2 < n and pattern[i + 2] == "/":
                out.append(r"(?:.*/)?")
                i += 3
            else:
                out.append(r".*")
                i += 2
        elif c == "*":
            out.append(r"[^/]*")
            i += 1
        elif c == "?":
            out.append(r"[^/]")
            i += 1
        elif c in r".+(){}|^$\\":
            # Regex-special chars — escape
            out.append("\\" + c)
            i += 1
        elif c == "[":
            # Character class — copy through to the closing ]
            j = i + 1
            while j < n and pattern[j] != "]":
                j += 1
            if j < n:
                out.append(pattern[i:j + 1])
                i = j + 1
            else:
                out.append("\\[")
                i += 1
        else:
            out.append(re.escape(c) if not c.isalnum() and c not in "/-_" else c)
            i += 1
    return "".join(out)


def _glob_match(rel_path: str, pattern: str) -> bool:
    """Match a repo-relative path against a gitignore-flavor glob pattern.

    Supports `**` (matches across `/`), `*` (no `/`), `?`, `[seq]`, and
    trailing `/` (matches any path under the directory). `**/X` matches
    `X` at root AND at any depth.
    """
    rel = _norm_rel(rel_path)
    pat = pattern

    # Trailing slash means "anything under this directory"
    if pat.endswith("/"):
        pat = pat + "**"

    pattern_re = "^" + _glob_to_regex(pat) + r"\Z"
    try:
        return re.match(pattern_re, rel, re.DOTALL) is not None
    except re.error:
        # Conservative fallback
        return fnmatch.fnmatch(rel, pat)


def _matches_any(rel_path: str, patterns):
    if not patterns:
        return False
    return any(_glob_match(rel_path, p) for p in patterns)


def _applies_to(rel_path: str, rule: dict) -> bool:
    applies = rule.get("applies_to", ["**/*"])
    excludes = rule.get("excludes_paths", [])
    if not _matches_any(rel_path, applies):
        return False
    if _matches_any(rel_path, excludes):
        return False
    return True


# ---------- context guards ----------

# A line is a "comment" if it matches any of these per-extension styles.
_COMMENT_PATTERNS = {
    ".py":   [re.compile(r"^\s*#"), re.compile(r'^\s*"""'), re.compile(r"^\s*'''")],
    ".sh":   [re.compile(r"^\s*#")],
    ".yaml": [re.compile(r"^\s*#")],
    ".yml":  [re.compile(r"^\s*#")],
    ".md":   [re.compile(r"^\s*<!--")],
    ".json": [],  # JSON has no comments
}


# Suffixes whose comment marker is a bare `#` running to end-of-line.
_HASH_COMMENT_SUFFIXES = (".py", ".sh", ".yaml", ".yml")


def _split_comment_span(line: str, suffix: str):
    """Split `line` into (code, marker, comment) at the first REAL `#`.

    Returns ``("", "", line)`` when the language has no `#`-to-EOL comment or
    no comment marker is present — callers treat an empty marker as "no code
    span to protect" and fall back to whole-line behavior.

    A `#` inside a string literal is NOT a comment marker, so quote state is
    tracked (single/double, with backslash escapes honored). This is the
    guard that keeps `word_list_strip` off executable code: see the CODE-SPAN
    GUARD note in ``apply_word_list_strip`` for the incident.
    """
    if suffix not in _HASH_COMMENT_SUFFIXES or "#" not in line:
        return "", "", line

    in_single = in_double = False
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:idx], "#", line[idx + 1:]
    return "", "", line


def _is_comment_line(line: str, suffix: str) -> bool:
    """Heuristic: does this line look like a comment in the file's language?

    For .py we also catch inline # comments via a relaxed scan (the # must be
    at column 0 or preceded only by whitespace OR be a clear inline # comment
    not inside a string). Full lexical analysis is out of scope — we err on
    safe matching: a line counted as comment is also OK to transform inside
    code if the transformation is conservative.
    """
    patterns = _COMMENT_PATTERNS.get(suffix, [])
    for p in patterns:
        if p.match(line):
            return True
    # For .py and .sh, also catch inline # comments (after some code)
    if suffix in (".py", ".sh"):
        # crude: if there's a # after non-whitespace, treat as inline comment line
        stripped = line.lstrip()
        if "#" in stripped and not stripped.startswith("#"):
            # heuristic — only count as comment-context if # comes after a
            # complete-looking statement. We approximate by saying "# is
            # comment if there's whitespace before it AND it's not inside
            # quotes". Quote-tracking is brittle; we just require " # " or
            # the # is preceded by ) or whitespace.
            if re.search(r'\s#', line):
                return True
    return False


_EXAMPLE_MARKERS = re.compile(
    r"(?i)\b(?:example|e\.g\.|for\s+example|sample|illustrat\w*|placeholder)\b"
)


def _is_example_context(line: str, prev_line: str = "") -> bool:
    """Line contains or follows an example/e.g./placeholder marker."""
    if _EXAMPLE_MARKERS.search(line):
        return True
    if _EXAMPLE_MARKERS.search(prev_line):
        return True
    # YAML: `example:` or `- example:` followed by a value on the same line
    if re.search(r"(^|\s)example\s*:", line):
        return True
    return False


def _check_context(line: str, context: str, suffix: str, prev_line: str = "") -> bool:
    """Returns True if `line` satisfies the given `when_in_context` guard."""
    if not context:
        return True
    if context == "comment":
        return _is_comment_line(line, suffix)
    if context in ("example_value", "example_marker"):
        return _is_example_context(line, prev_line)
    # Unknown context — fail safe (don't apply)
    return False


# ---------- per-rule transforms ----------

def apply_file_replace(target_path: str, template_path: str, source_root: Path) -> str:
    """Read the template at template_path (relative to source_root) and return its content.

    Raises FileNotFoundError if the template doesn't exist; the caller can
    decide to skip the file with WARN (pending_template handling).
    """
    tpath = source_root / template_path
    return tpath.read_text(encoding="utf-8")


def apply_inline_edit(content: str, rule: dict) -> str:
    """Apply one inline_edit rule to content.

    Pattern is a Python regex. Replacement is a literal (regex backrefs allowed).
    Honors `multiline`, `when_in_context`, suffix-based context detection.
    line_hint is purely advisory (not enforced) — the pattern itself is the
    authoritative match anchor.
    """
    pattern = rule["pattern"]
    replacement = rule.get("replacement", "")
    multiline = rule.get("multiline", False)
    context = rule.get("when_in_context", "")

    flags = re.DOTALL if multiline else 0

    if not context:
        return re.sub(pattern, replacement, content, flags=flags)

    # Line-by-line apply with context guard
    target = rule.get("target", "")
    suffix = Path(target).suffix
    out_lines = []
    lines = content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        prev = lines[i - 1] if i > 0 else ""
        if _check_context(line, context, suffix, prev):
            line = re.sub(pattern, replacement, line, flags=flags)
        out_lines.append(line)
    return "".join(out_lines)


def apply_global_regex(content: str, rule: dict, rel_path: str) -> str:
    """Apply one global_regex rule to content (if rel_path matches applies_to)."""
    if not _applies_to(rel_path, rule):
        return content

    pattern = rule["pattern"]
    replacement = rule.get("replacement", "")
    context = rule.get("when_in_context", "")
    multiline = rule.get("multiline", False)
    flags = re.DOTALL if multiline else 0

    if not context:
        return re.sub(pattern, replacement, content, flags=flags)

    suffix = Path(rel_path).suffix
    out_lines = []
    lines = content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        prev = lines[i - 1] if i > 0 else ""
        if _check_context(line, context, suffix, prev):
            line = re.sub(pattern, replacement, line, flags=flags)
        out_lines.append(line)
    return "".join(out_lines)


def apply_word_list_strip(content: str, rule: dict, rel_path: str) -> str:
    """Apply one word_list_strip rule to content.

    Strips each word (with word-boundary `\\b`) when in `when_in_context`.
    Replacement defaults to empty string; can override via `replacement`.
    """
    if not _applies_to(rel_path, rule):
        return content

    words = rule.get("words", [])
    if not words:
        return content
    context = rule.get("when_in_context", "comment")
    replacement = rule.get("replacement", "")

    # Build one regex: \b(word1|word2|...)\b
    # Escape each word.
    escaped = [re.escape(w) for w in words]
    word_re = re.compile(r"\b(?:" + "|".join(escaped) + r")\b")

    suffix = Path(rel_path).suffix
    out_lines = []
    lines = content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        prev = lines[i - 1] if i > 0 else ""
        if _check_context(line, context, suffix, prev):
            # CODE-SPAN GUARD (g-115-3445). `_is_comment_line` returns True for a
            # CODE line that merely carries a TRAILING comment — its docstring
            # excuses this as safe "if the transformation is conservative", but
            # word_list_strip DELETES tokens, so it is not. Substituting across
            # the whole line rewrote `import boto3  # noqa: E402` to
            # `import   # noqa: E402` in the planted seed — a SyntaxError shipped
            # to the public repo, and one that recurred on every promotion
            # because restoring the file could not outlive the next plant.
            # `# noqa` lives on import lines by construction, so the stripped
            # words and this idiom collide by design, not by accident.
            # Confine the substitution to the comment span; leave code untouched.
            if context == "comment":
                head, sep, tail = _split_comment_span(line, suffix)
                line = head + sep + word_re.sub(replacement, tail) if sep else line
            else:
                line = word_re.sub(replacement, line)
        out_lines.append(line)
    return "".join(out_lines)


# ---------- top-level orchestrator ----------

def select_rules_for_file(rel_path: str, transformations: list) -> dict:
    """Group transformations by type for a single file.

    Returns:
      {
        "file_replace": <rule or None>,
        "inline_edit":  [rules],
        "global_regex": [rules],
        "word_list_strip": [rules],
        "pending_template_skip": bool,
      }

    Resolution rules:
      - file_replace matches by exact target == rel_path. At most one match.
      - inline_edit matches by exact target == rel_path.
      - global_regex / word_list_strip match by applies_to glob.
      - If a file_replace rule has pending_template: true, we DO NOT skip the
        file outright — we instead drop the file_replace and let the chain
        process the file via inline_edit + global_regex + word_list_strip,
        flagging pending_template_skip so the caller can WARN.

    The pending_template fallback behavior was introduced 2026-05-19 so the
    initial plant can proceed with partial template coverage; F3-F7 are
    intentionally `pending_template: true` until proper SKILL.md/convention
    templates are authored.
    """
    norm = _norm_rel(rel_path)
    result = {
        "file_replace": None,
        "inline_edit": [],
        "global_regex": [],
        "word_list_strip": [],
        "pending_template_skip": False,
    }
    for rule in transformations:
        ttype = rule.get("type")
        if ttype == "file_replace":
            target = rule.get("target", "")
            if _norm_rel(target) == norm:
                if rule.get("pending_template"):
                    result["pending_template_skip"] = True
                else:
                    result["file_replace"] = rule
        elif ttype == "inline_edit":
            target = rule.get("target", "")
            if _norm_rel(target) == norm:
                result["inline_edit"].append(rule)
        elif ttype == "global_regex":
            if _applies_to(norm, rule):
                result["global_regex"].append(rule)
        elif ttype == "word_list_strip":
            if _applies_to(norm, rule):
                result["word_list_strip"].append(rule)
    return result


def _has_domain_leak_exempt_marker(content: str) -> bool:
    """Check if file content carries the `domain-leak-exempt:` marker.

    Files marked this way have intentional domain references (regex patterns,
    sentinel strings, test fixtures, audit-trail documentation) that MUST NOT
    be rewritten by sweeping global_regex / word_list_strip transformations.
    Explicit file_replace / inline_edit are still honored.

    This mirrors the convention used by core/scripts/domain-leak-check.sh
    (PostToolUse leak scanner) so a single marker exempts a file from BOTH
    the leak scanner AND the seed transformation chain.
    """
    return "domain-leak-exempt:" in content


def transform_file(
    rel_path: str,
    content: str,
    transformations: list,
    source_root: Path,
) -> tuple:
    """Apply all relevant transformations to one file.

    Returns (new_content, applied_rule_ids, pending_template_skipped).
      applied_rule_ids is a list of rule IDs in application order.
      pending_template_skipped is True iff a pending_template file_replace
        was matched and demoted to chain mode (caller may want to WARN).

    Files carrying the `domain-leak-exempt:` marker skip global_regex and
    word_list_strip rules (sweeping transforms with no per-file intent).
    file_replace and inline_edit ARE still applied — those carry explicit
    per-file authorial intent that overrides the blanket exemption.
    """
    rules = select_rules_for_file(rel_path, transformations)
    applied = []
    is_exempt = _has_domain_leak_exempt_marker(content)

    if rules["file_replace"]:
        new = apply_file_replace(
            rel_path,
            rules["file_replace"]["source"],
            source_root,
        )
        applied.append(rules["file_replace"]["id"])
        return new, applied, False

    pending_skip = rules["pending_template_skip"]
    new = content

    for r in rules["inline_edit"]:
        new = apply_inline_edit(new, r)
        applied.append(r["id"])

    # global_regex: per-rule exemption gating. A rule flagged
    # apply_even_if_exempt:true (e.g. G2 MIND_->MIND_, a FUNCTIONAL env-var
    # rename that MUST fire for a deployment to resolve its agent name) pierces
    # the domain-leak-exempt marker. All OTHER global_regex rules (G3/G4 repo +
    # product-name rewrites, etc.) stay skipped for exempt files — those rewrite
    # the cosmetic domain strings the marker exists to protect. Seed machinery
    # whose MIND_ is DATA (the G2 rule pattern itself, the self-reference
    # scanner's detection literal, seed sentinels) is held out via the rule's
    # own excludes_paths, which apply_global_regex -> _applies_to honors.
    for r in rules["global_regex"]:
        if is_exempt and not r.get("apply_even_if_exempt", False):
            continue
        new = apply_global_regex(new, r, rel_path)
        applied.append(r["id"])

    if not is_exempt:
        for r in rules["word_list_strip"]:
            new = apply_word_list_strip(new, r, rel_path)
            applied.append(r["id"])

    return new, applied, pending_skip


# ---------- binary detection (skip binary files) ----------

_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".7z",
    ".exe", ".dll", ".so", ".dylib", ".bin",
    ".pyc", ".pyo", ".pyd",
    ".db", ".sqlite", ".sqlite3",
    ".woff", ".woff2", ".ttf", ".eot",
    ".mp3", ".mp4", ".webm", ".wav", ".ogg",
}


def is_binary_path(rel_path: str) -> bool:
    """True if path's suffix is a known binary type — skip transformation."""
    return Path(rel_path).suffix.lower() in _BINARY_SUFFIXES


def is_likely_binary_content(content_bytes: bytes) -> bool:
    """Heuristic: contains a NUL byte in the first 8KB."""
    return b"\x00" in content_bytes[:8192]
