#!/usr/bin/env python3
"""lock-symmetry-lint — surface acquire/release scope asymmetry in bash locks.

Origin: rb-356 (PreCompact serialization gate) + rb-369 (adversarial review).
The release predicate of .autocompact-serialize-lock was a strict subset of
the acquire predicate, stranding the lock for 5 min on assistant-mode
autocompacts. Bug passed 15/15 regression tests. guard-328 documents the rule
at LLM-review time; this script enforces it at edit-time.

Structural surfacing only: emits predicate chains for each acquire and
release site, per distinct lock token. Semantic "is release predicate a
superset" decision is undecidable on arbitrary bash, so the tool lists the
evidence and lets the LLM (or /verify-learning) judge.

Scope: core/scripts/*.sh, world/scripts/*.sh. Other paths intentionally
skipped — framework-layer locks are the only in-scope class for this tool.

Stdlib only. Python 3.8+.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable, NamedTuple

# --- Patterns -----------------------------------------------------------

# Acquire: `mkdir <arg>` or `mkdir -p <arg>` where <arg> contains "lock"
# (case-insensitive) as substring. Captures the raw <arg> token.
ACQUIRE_RE = re.compile(
    r"""
    ^\s*
    (?:if\s+)?                 # may be wrapped in `if mkdir ...`
    mkdir\s+                   # the verb
    (?:-p\s+)?                 # optional -p
    (?P<arg>[^\s;]+)           # the target path/var
    """,
    re.VERBOSE,
)

# Release: `rm -rf <arg>` or `rmdir <arg>` where <arg> contains "lock".
RELEASE_RE = re.compile(
    r"""
    ^\s*
    rm\s+-rf?\s+               # rm -r or rm -rf
    (?P<arg>[^\s;]+)
    """,
    re.VERBOSE,
)
RMDIR_RE = re.compile(r"^\s*rmdir\s+(?P<arg>[^\s;]+)")

# Tokenize a path argument down to its "lock identity":
# - strip surrounding quotes
# - strip leading variables (${PROJECT_ROOT}/, $PROJECT_ROOT/, $(...)/)
# - strip $PROJECT_ROOT/ variants
# - strip .tmp, .claimed-$$ suffixes
# - case-fold
VAR_PREFIX_RE = re.compile(r"""^[\"']?\$\{?[A-Za-z_][A-Za-z0-9_]*\}?/""")
CAPTURE_PREFIX_RE = re.compile(r"""^[\"']?\$\([^)]*\)/""")


def is_lock_arg(arg: str) -> bool:
    """Heuristic: does the argument name something that looks like a lock?"""
    lower = arg.lower()
    return "lock" in lower


def normalize_lock_token(arg: str) -> str:
    """Reduce an acquire/release argument to a comparable identity.

    `$LOCK_DIR`                                 -> $lock_dir
    `"$PROJECT_ROOT/.autocompact-serialize-lock"` -> .autocompact-serialize-lock
    `"$LOCK_DIR.tmp"`                           -> $lock_dir.tmp
    """
    s = arg.strip()
    # Strip matching surrounding quotes
    while s and s[0] in ('"', "'") and s[-1] == s[0]:
        s = s[1:-1]
    # Strip variable/command-substitution prefixes that are just "where"
    while True:
        m = VAR_PREFIX_RE.match(s) or CAPTURE_PREFIX_RE.match(s)
        if not m:
            break
        s = s[m.end():]
    return s.lower()


# --- Predicate-chain extraction -----------------------------------------

# Walk upward from a matched line, collecting enclosing `if`, `while`, `for`
# conditions until we hit top-level (no indent or closing `fi`/`done`).
CONDITIONAL_START_RE = re.compile(r"^\s*(if|while|for|elif)\b(?P<rest>.*)")
CONDITIONAL_END_RE = re.compile(r"^\s*(fi|done|esac)\b")


class Site(NamedTuple):
    script: str
    line_no: int
    raw_line: str
    token: str
    role: str  # "ACQUIRE" | "RELEASE"
    predicates: tuple[str, ...]
    expanded_tokens: tuple[str, ...]  # intra-script variable resolution


def extract_predicates(lines: list[str], hit_idx: int) -> tuple[str, ...]:
    """Walk backwards from hit_idx, stacking open conditionals and popping
    closed ones. Return the still-open conditional headers at hit_idx."""
    stack: list[str] = []
    open_stack: list[str] = []  # conditionals still open at hit_idx
    # Forward pass from start to hit_idx, maintaining open block depth
    i = 0
    while i < hit_idx:
        line = lines[i]
        m_start = CONDITIONAL_START_RE.match(line)
        m_end = CONDITIONAL_END_RE.match(line)
        if m_start:
            kw = m_start.group(1)
            # elif does not open a new block; it swaps the top `if` predicate
            if kw == "elif" and stack:
                stack[-1] = f"elif{m_start.group('rest')}"
            else:
                # Capture just the condition, not the whole multi-line mess
                stack.append(line.strip())
        elif m_end:
            if stack:
                stack.pop()
        i += 1
    open_stack = list(stack)
    return tuple(open_stack)


# --- Scanning -----------------------------------------------------------

VAR_ASSIGN_RE = re.compile(r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<val>.+)$")


def build_var_map(lines: list[str]) -> dict[str, str]:
    """Scan assignments `VAR=...` and build name→value map.
    Only keeps vars whose value contains "lock" — those are identity-carrying.
    No shell-level evaluation; this is a best-effort source-level match."""
    vmap: dict[str, str] = {}
    for line in lines:
        if line.lstrip().startswith("#"):
            continue
        m = VAR_ASSIGN_RE.match(line)
        if not m:
            continue
        val = m.group("val").split("#", 1)[0].strip()
        if "lock" in val.lower():
            vmap[m.group("name")] = val
    return vmap


def expand_token(token: str, vmap: dict[str, str]) -> set[str]:
    """Given a token like `$lock_dir` or `${lock_dir}.tmp`, return the
    expanded forms (literal path after var substitution). Always includes
    the original token so exact-match fallback still works."""
    out = {token}
    # Try direct name resolution: token might be `$VAR` or `${VAR}` (post-normalize
    # tokens are lowercased, so case-fold the var map lookup)
    for name, val in vmap.items():
        lname = name.lower()
        for marker in (f"${lname}", f"${{{lname}}}"):
            if marker in token:
                expanded = token.replace(marker, normalize_lock_token(val))
                out.add(expanded)
    return out


def scan_file(path: Path) -> list[Site]:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = content.splitlines()
    vmap = build_var_map(lines)
    sites: list[Site] = []
    for idx, line in enumerate(lines):
        # Skip comments
        if line.lstrip().startswith("#"):
            continue
        m = ACQUIRE_RE.match(line)
        if m and is_lock_arg(m.group("arg")):
            tok = normalize_lock_token(m.group("arg"))
            sites.append(Site(
                script=str(path),
                line_no=idx + 1,
                raw_line=line.rstrip(),
                token=tok,
                role="ACQUIRE",
                predicates=extract_predicates(lines, idx),
                expanded_tokens=tuple(expand_token(tok, vmap)),
            ))
            continue
        m = RELEASE_RE.match(line)
        if m and is_lock_arg(m.group("arg")):
            tok = normalize_lock_token(m.group("arg"))
            sites.append(Site(
                script=str(path),
                line_no=idx + 1,
                raw_line=line.rstrip(),
                token=tok,
                role="RELEASE",
                predicates=extract_predicates(lines, idx),
                expanded_tokens=tuple(expand_token(tok, vmap)),
            ))
            continue
        m = RMDIR_RE.match(line)
        if m and is_lock_arg(m.group("arg")):
            tok = normalize_lock_token(m.group("arg"))
            sites.append(Site(
                script=str(path),
                line_no=idx + 1,
                raw_line=line.rstrip(),
                token=tok,
                role="RELEASE",
                predicates=extract_predicates(lines, idx),
                expanded_tokens=tuple(expand_token(tok, vmap)),
            ))
    return sites


def collect_targets(root: Path) -> Iterable[Path]:
    for sub in ("core/scripts", "world/scripts"):
        base = root / sub
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*.sh")):
            yield p


# --- Token equivalence --------------------------------------------------
# Two tokens in different scripts may refer to the same lock if their
# normalized forms match OR if one is a variable name and the other is
# the literal basename the variable resolves to. We accept any of:
#   - exact normalized equality
#   - either token contains the other as substring after stripping $/{/}
#   - both tokens share a common "stem" (e.g., `autocompact-serialize-lock`)
STEM_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)+")


def token_stems(tok: str) -> set[str]:
    # Normalize variable syntax away
    cleaned = tok.replace("${", "").replace("}", "").replace("$", "")
    stems = set(STEM_RE.findall(cleaned))
    # Keep only stems that contain "lock" — these are the identity-carrying
    # parts. E.g., token `autocompact-serialize-lock.tmp` → stems include
    # `autocompact-serialize-lock` and `tmp`; we keep only the former.
    return {s for s in stems if "lock" in s}


def same_lock(a: str, b: str) -> bool:
    if a == b:
        return True
    sa, sb = token_stems(a), token_stems(b)
    if sa & sb:
        return True
    # Fallback: substring containment on the bare token
    if a and b and (a in b or b in a):
        return True
    return False


def sites_match(a: Site, b: Site) -> bool:
    """Two sites refer to the same lock if any of their expanded tokens
    satisfy same_lock() — this handles cross-script comparisons where one
    side uses a variable name and the other uses the literal path."""
    for ta in a.expanded_tokens:
        for tb in b.expanded_tokens:
            if same_lock(ta, tb):
                return True
    return False


# --- Reporting ----------------------------------------------------------

def format_site(site: Site) -> str:
    pred = " & ".join(site.predicates) if site.predicates else "<top-level>"
    return (
        f"{site.script}:{site.line_no}: {site.role} "
        f"token={site.token!r} predicates=[{pred}]\n"
        f"    {site.raw_line.strip()}"
    )


def run_scan(root: Path) -> tuple[list[str], bool]:
    """Return (messages, any_orphan_acquire)."""
    all_sites: list[Site] = []
    for p in collect_targets(root):
        all_sites.extend(scan_file(p))
    # Partition by role
    acquires = [s for s in all_sites if s.role == "ACQUIRE"]
    releases = [s for s in all_sites if s.role == "RELEASE"]
    messages: list[str] = []
    any_orphan = False
    for aq in acquires:
        matches = [rel for rel in releases if sites_match(aq, rel)]
        messages.append(format_site(aq))
        if not matches:
            messages.append(
                f"{aq.script}:{aq.line_no}: WARN no RELEASE found for "
                f"ACQUIRE site (token={aq.token!r})"
            )
            any_orphan = True
        else:
            for rel in matches:
                messages.append(format_site(rel))
    # Also surface orphan releases (release-only scripts — unusual)
    for rel in releases:
        if not any(sites_match(aq, rel) for aq in acquires):
            messages.append(
                f"{rel.script}:{rel.line_no}: INFO orphan RELEASE "
                f"(no matching ACQUIRE) token={rel.token!r}"
            )
            messages.append(format_site(rel))
    return messages, any_orphan


def run_check_pair(
    root: Path, acquire_script: str, release_script: str
) -> tuple[list[str], bool]:
    """Narrow check: verify the acquire in <aq-script> has a matching release
    in <rel-script>. Returns (messages, failed)."""
    aq_path = None
    rel_path = None
    for p in collect_targets(root):
        if p.name == acquire_script or p.stem == acquire_script:
            aq_path = p
        if p.name == release_script or p.stem == release_script:
            rel_path = p
    messages: list[str] = []
    if aq_path is None:
        return [f"--check-pair: acquire script not found: {acquire_script}"], True
    if rel_path is None:
        return [f"--check-pair: release script not found: {release_script}"], True
    aq_sites = [s for s in scan_file(aq_path) if s.role == "ACQUIRE"]
    rel_sites = [s for s in scan_file(rel_path) if s.role == "RELEASE"]
    if not aq_sites:
        return [f"--check-pair: no ACQUIRE sites in {aq_path}"], True
    if not rel_sites:
        return [f"--check-pair: no RELEASE sites in {rel_path}"], True
    failed = False
    for aq in aq_sites:
        pair = next((r for r in rel_sites if sites_match(aq, r)), None)
        messages.append(format_site(aq))
        if pair is None:
            messages.append(
                f"{aq.script}:{aq.line_no}: FAIL no RELEASE in "
                f"{rel_path.name} for ACQUIRE token={aq.token!r}"
            )
            failed = True
        else:
            messages.append(format_site(pair))
    return messages, failed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Surface bash acquire/release scope asymmetry."
    )
    parser.add_argument("--force", action="store_true",
                        help="Always exit 0 (WIP bypass).")
    parser.add_argument("--check-pair", nargs=2,
                        metavar=("ACQUIRE_SCRIPT", "RELEASE_SCRIPT"),
                        help="Verify one specific acquire/release pair.")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress output when no orphans are found.")
    args = parser.parse_args()

    root = Path(os.environ.get("PROJECT_ROOT", os.getcwd()))

    if args.check_pair:
        messages, failed = run_check_pair(root, args.check_pair[0], args.check_pair[1])
        for m in messages:
            print(m)
        if args.force:
            return 0
        return 1 if failed else 0

    messages, orphan = run_scan(root)
    if orphan or not args.quiet:
        for m in messages:
            print(m)
    if args.force:
        return 0
    return 1 if orphan else 0


if __name__ == "__main__":
    sys.exit(main())
