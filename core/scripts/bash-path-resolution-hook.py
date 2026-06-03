#!/usr/bin/env python3
"""PreToolUse[Bash] hook — L1 path-resolution enforcement (Bash sibling).

Closes the gap where Write/Edit/MultiEdit are checked by
path-resolution-hook.py but Bash mkdir/heredoc/redirect/cp/mv/touch/tee
operations creating NEW top-level entries under governed roots are not.

Origin: 2026-05-21. The rotate-lambda-common-pat.sh script landed at
`agents/alpha/scripts/` (new top-level under bound agent dir) via a Bash
heredoc, which path-resolution-hook.py doesn't see (it only fires on
Write/Edit/MultiEdit).

Scope (narrow on purpose to minimize false positives):

  1. Absolute paths anywhere in the bash command, hitting is_new_toplevel
     under WORLD_PATH / META_PATH / bound-agent-dir.
  2. Relative paths of the shape `agents/<bound-agent>/<NEW_SEGMENT>/...`
     (treated as relative-to-PROJECT_ROOT — the dominant Bash-cwd case).

OUT of scope (intentional — too false-positive-prone):

  - `cd /other-path && mkdir foo` — cwd changes that move the relative
    base outside PROJECT_ROOT. Caught only via absolute-path detection.
  - Random relative paths that don't match the `agents/...`/`world/...`/
    `meta/...` prefix shape.
  - Process substitution (`>(...)`) and `<(...)` constructs.
  - Path components inside variable expansions (`$DIR/foo`).

SAFETY: fail open on ANY error. Empty stdout + exit 0 = approve. Same
contract as path-resolution-hook.py.

# domain-leak-exempt: this hook inspects bash command text — the regex
# tokens it uses (mkdir/touch/cp/mv/tee/heredoc patterns) are the hook's
# detection contract, not accidental domain bleed.
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_helpers import (  # noqa: E402
    approve_no_mutation,
    emit_deny,
    stdin_json_or_approve,
)

# Sync with path-resolution-hook.py / _paths.py.
AGENTS_PARENT_DIR = "agents"
SESSIONS_DIRNAME = "sessions"


def _resolve_agent_dir(project_root, agent):
    if AGENTS_PARENT_DIR:
        return os.path.join(project_root, AGENTS_PARENT_DIR, agent)
    return os.path.join(project_root, agent)


def norm_path(p):
    """Same algorithm as path-resolution-hook.py:norm_path."""
    if not p:
        return ""
    try:
        p = p.replace("\\", "/")
        if len(p) >= 3 and p[0] == "/" and p[2] == "/" and p[1].isalpha():
            p = p[1].lower() + ":" + p[2:]
        if p.startswith("//") and len(p) >= 5 and p[3] == ":":
            p = p[2:].lower()[0] + p[3:]
        if len(p) >= 2 and p[1] == ":":
            p = p[0].lower() + p[1:]
        while "//" in p:
            p = p.replace("//", "/")
        if len(p) > 1 and p.endswith("/"):
            p = p[:-1]
        return p
    except Exception:
        return ""


def is_under(child, root):
    if not child or not root:
        return False
    if child == root:
        return True
    return child.startswith(root + "/")


def is_new_toplevel(target, root):
    """Same algorithm as path-resolution-hook.py:is_new_toplevel."""
    if not is_under(target, root) or target == root:
        return False
    rel = target[len(root) + 1:]
    if not rel:
        return False
    first_segment = rel.split("/", 1)[0]
    if not first_segment:
        return False
    toplevel_path = root + "/" + first_segment
    return not os.path.exists(toplevel_path)


def read_paths_conf(conf_path):
    """Read WORLD_PATH, META_PATH from a local-paths.conf file."""
    result = {"WORLD_PATH": None, "META_PATH": None, "AGENT_WRITE_PATH": None}
    try:
        with open(conf_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip().replace("\r", "")
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key in result:
                    result[key] = value
    except Exception:
        pass
    return result


# Regex catalog of write-creating bash constructs. Each pattern extracts
# the destination path as group 1.
#
# Notes:
#   - `mkdir -p? <path>` — first non-flag arg is the dir to create.
#   - `touch <path>` — first arg.
#   - `cp [-flags] <src> <dst>` — last arg is the destination.
#   - `mv [-flags] <src> <dst>` — last arg is the destination.
#   - `> <path>` or `>> <path>` — file redirect. Excluded: `2>`, `&>`,
#     `>(...)` (process substitution), `2>&1`. Patterns require whitespace
#     before > to avoid matching `1>`/`2>`.
#   - `tee [-a] <path>` — destination.
#
# Path-token character class. Includes:
#   . / - _ ~ alphanumeric    — normal POSIX path components
#   :                         — Windows drive prefix (C:/…) and rare colon-bearing names
# Excludes:
#   $   — variable expansion is out of scope (would over-capture)
#   <   — would catch heredoc delimiters inadvertently
#   \\  — backslash-quoted spaces; rare and Windows-translation lossy
PATH_CHARS = r'[A-Za-z0-9_.:/~\-]'

# Flag-token pattern. Handles three shapes:
#   1. Short flags: -p, -pv, -laR
#   2. Long flags: --parents, --no-clobber, --preserve=mode
#   3. End-of-flags marker: bare --
#
# Earlier versions matched only `-[a-zA-Z]+` which let `mkdir --parents <path>`
# slip past the hook entirely — the engine would greedily consume `--parents`
# as the path token via PATH_CHARS (which includes `-`), then fail the
# governed-root check on the non-path string and approve the write. Likewise
# `mkdir -- <path>` (end-of-flags) bypassed because `--` was consumed as the
# path token. Both confirmed during fresh-eyes review 2026-05-21.
FLAG_TOKENS = r'(?:--?[a-zA-Z][a-zA-Z0-9=._-]*\s+|--\s+)*'

# cp/mv use the LAST arg (destination); redirect uses the only arg after >.
# mkdir/touch/tee take MULTIPLE positional args, each a target — handled
# separately in extract_targets so we don't miss `mkdir EXISTING NEW`
# (H12, fresh-eyes review 2026-05-21). The previous single-arg regex captured
# only the FIRST positional arg, leaving downstream args unchecked: cruft
# created via the second arg satisfied is_new_toplevel=False on later writes
# (parent already existed) and the Write hook approved follow-on file
# operations into the bypass-created dir.
PATTERNS_LAST_ARG = [
    # cp [-flags] <src> ... <dst>  — destination is the LAST arg
    # Use a non-greedy match between cp and the final path before EOL/operator.
    # This is approximate; for richer parsing we'd need a real shell tokenizer.
    (r'\bcp\s+' + FLAG_TOKENS + r'(?:' + PATH_CHARS + r'+\s+)+(["\']?)(' + PATH_CHARS + r'+)\1(?=\s|[;&|]|$)', 2),
    # mv [-flags] <src> <dst>
    (r'\bmv\s+' + FLAG_TOKENS + r'(?:' + PATH_CHARS + r'+\s+)+(["\']?)(' + PATH_CHARS + r'+)\1(?=\s|[;&|]|$)', 2),
    # >  <path> or >> <path>   (file redirect, leading whitespace required so 1>/2>/&> don't match)
    (r'(?<=\s)>>?\s*(["\']?)(' + PATH_CHARS + r'+)\1',                          2),
]

# Multi-arg verbs — every positional arg after the flag block is a target.
# Match each verb instance and let the caller iterate over the trailing
# token stream (split on whitespace) so `mkdir A B C` checks A, B, and C.
MULTI_ARG_VERBS = ('mkdir', 'touch', 'tee')
_MULTI_ARG_TAIL_RE = {
    verb: re.compile(r'\b' + verb + r'\s+' + FLAG_TOKENS + r'(.+?)(?=$|[;&|\n])')
    for verb in MULTI_ARG_VERBS
}
_PATH_TOKEN_RE = re.compile(r'^' + PATH_CHARS + r'+$')


def extract_targets(cmd):
    """Return a list of (verb, path) tuples extracted from the bash command."""
    targets = []
    # Multi-arg verbs (mkdir / touch / tee): scan every positional token after the flags.
    for verb in MULTI_ARG_VERBS:
        for m in _MULTI_ARG_TAIL_RE[verb].finditer(cmd):
            tail = m.group(1)
            for tok in tail.split():
                tok = tok.strip('"\'')
                if not tok or tok.startswith('-'):
                    continue
                if not _PATH_TOKEN_RE.match(tok):
                    continue
                targets.append((verb, tok))
    # Single-target patterns (cp/mv last-arg, > redirect destination).
    for pattern, target_group in PATTERNS_LAST_ARG:
        for m in re.finditer(pattern, cmd):
            verb_match = re.search(r'\b(cp|mv)\b', m.group(0))
            verb = verb_match.group(1) if verb_match else "redirect"
            path = m.group(target_group)
            if path:
                targets.append((verb, path))
    return targets


def main():
    data = stdin_json_or_approve()
    tool_name = data.get("tool_name", "") or ""
    if tool_name != "Bash":
        approve_no_mutation()

    tool_input = data.get("tool_input") or {}
    cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not cmd or not isinstance(cmd, str):
        approve_no_mutation()

    # Fast filter: skip commands that can't possibly create files.
    if not re.search(r'(mkdir|touch|tee|cp|mv|\s>)', cmd):
        approve_no_mutation()

    project_root = os.environ.get("PROJECT_ROOT", "")
    if not project_root:
        approve_no_mutation()
    pr_norm = norm_path(project_root)
    if not pr_norm:
        approve_no_mutation()

    # Resolve bound agent
    sid = data.get("session_id", "") or ""
    agent = os.environ.get("AYOAI_AGENT", "") or ""
    if not agent and sid:
        try:
            from _resolve_agent_from_sid import resolve as _resolve_agent
            agent = _resolve_agent(sid, Path(project_root)) or ""
        except Exception:
            agent = ""
    if not agent:
        approve_no_mutation()

    # Read paths conf for WORLD/META
    conf_path = os.path.join(_resolve_agent_dir(project_root, agent),
                              "local-paths.conf")
    paths = read_paths_conf(conf_path) if os.path.isfile(conf_path) else {}

    agent_dir_norm = norm_path(_resolve_agent_dir(project_root, agent))
    wp_norm = norm_path(paths.get("WORLD_PATH") or "")
    mp_norm = norm_path(paths.get("META_PATH") or "")

    # Build governed-root list (label, normalized_path)
    governed = []
    if agent_dir_norm:
        governed.append(("bound agent dir", agent_dir_norm))
    if wp_norm:
        governed.append(("WORLD_PATH", wp_norm))
    if mp_norm:
        governed.append(("META_PATH", mp_norm))
    if not governed:
        approve_no_mutation()

    # Extract candidate targets
    targets = extract_targets(cmd)
    if not targets:
        approve_no_mutation()

    # Check each target
    for verb, raw_path in targets:
        # Resolve to absolute candidate path
        if os.path.isabs(raw_path) or (len(raw_path) >= 2 and raw_path[1] == ":"):
            abs_path = raw_path
        elif raw_path.startswith("/"):
            abs_path = raw_path
        else:
            # Relative — treat as relative to PROJECT_ROOT (the dominant Bash cwd).
            # Only consider relative paths that START with `agents/`, `world/`,
            # or `meta/` — these are the shapes that could land cruft in
            # governed roots. Everything else: skip (false-positive risk).
            lead = raw_path.split("/", 1)[0] if "/" in raw_path else raw_path
            if lead not in ("agents", "world", "meta"):
                continue
            abs_path = os.path.join(project_root, raw_path)

        target_norm = norm_path(abs_path)
        if not target_norm:
            continue

        for label, root in governed:
            if is_under(target_norm, root) and is_new_toplevel(target_norm, root):
                # Compute the offending new top-level for the deny message
                rel = target_norm[len(root) + 1:]
                new_seg = rel.split("/", 1)[0] if "/" in rel else rel
                reason = (
                    f"Path-resolution hook (L1-Bash) blocked Bash command:\n"
                    f"  verb: {verb}\n"
                    f"  target: {raw_path}\n"
                    f"This would create a new top-level entry under "
                    f"{label} ({root}):\n"
                    f"  new top-level: {new_seg}/\n\n"
                    f"New top-level entries under governed roots require "
                    f"explicit approval. The Write/Edit hook (path-resolution-"
                    f"hook.py) blocks this for Write tool calls; this is the "
                    f"Bash sibling closing the same gap.\n\n"
                    f"Options:\n"
                    f"  (a) Place under an EXISTING top-level dir within the "
                    f"governed root.\n"
                    f"  (b) Ask the user where the new entry should live; once "
                    f"the dir exists this command will succeed on retry.\n"
                    f"  (c) For ephemeral files, use "
                    f"{agent}/session/ (cross-session) or "
                    f"agents/{agent}/sessions/<bound-SID>/ (per-session "
                    f"scratch — L1-sanctioned).\n"
                    f"See: .claude/rules/path-resolution.md "
                    f"\"L1 Cruft Prevention\" section."
                )
                emit_deny(reason)

    approve_no_mutation()


try:
    main()
except Exception:
    # Catch-all fail-open
    pass

sys.exit(0)
