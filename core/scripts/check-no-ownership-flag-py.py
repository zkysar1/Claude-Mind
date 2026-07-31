#!/usr/bin/env python3
"""AST half of the OWNERSHIP_MODE elimination gate (, ).

`check-no-ownership-flag.sh` owns `.sh` and `SKILL.md`, where a leading-`#`
comment strip is a complete comment model. It is NOT complete for Python:
triple-quoted docstrings carry no `#`, so a line of PROSE documenting the
removed flag matched the shell gate's env-read regex and BLOCKED the commit.
Measured reproduction (g-115-3323, 2026-07-26): a docstring reading
"this used to call os.environ.get(...)" was refused rc=1, with the only
escapes being `--no-verify` (which the gate's own message forbids) or
deleting accurate documentation. That is the false-positive-blocker class of
rb-246 / guard-147 — the gate refusing legitimate work.

The fix is structural, not a better regex: an env read is a CALL or a
SUBSCRIPT node, and a docstring is `Expr(Constant(str))` — a bare string
statement that is neither. Prose therefore cannot reach the detector at all,
rather than being filtered out of it by a pattern that has to anticipate
every way prose can be written (rb-5261). Comments never enter an AST.

Modes mirror `check-no-bare-bash.py`, the sibling that already splits this
way for the same reason:
  (default)  pre-commit — staged in-scope files, whole-file AST intersected
             with the ADDED line numbers, so a pre-existing site never blocks
             an unrelated commit but any NEW introduction does.
  --audit    whole tracked tree.
  --paths    explicit files (tests, ad-hoc repro).

Detection stays at least as strict as the regex it replaces on real reads:
`environ.get` / `getenv` / `environ[...]` / `environ.setdefault` / `.pop`,
with the flag named either as a string constant or as a bare identifier.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path, PurePosixPath

# The removed names. A live env READ of either is the regression this refuses.
_FLAGS = ("OWNERSHIP_MODE", "MACHINE_OWNED_AGENTS")

# Attribute/function names that denote an environment read.
_ENV_CALLS = ("get", "getenv", "setdefault", "pop")
_ENV_ROOTS = ("environ", "getenv")

# This detector names the flags as detection constants, never reads them.
_SELF_EXEMPT = ("core/scripts/check-no-ownership-flag-py.py",)


def _dotted(node: ast.expr) -> str:
    """Resolve an Attribute/Name chain to a dotted string ('' when neither)."""
    parts: list[str] = []
    cur: ast.expr | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    elif parts == []:
        return ""
    return ".".join(reversed(parts))


def _names_a_flag(node: ast.expr) -> str | None:
    """The flag named by ``node`` as a string constant or bare identifier."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if node.value in _FLAGS else None
    if isinstance(node, ast.Name):
        return node.id if node.id in _FLAGS else None
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        dotted = _dotted(node.func)
        tail = dotted.rsplit(".", 1)
        leaf = tail[-1]
        root_ok = any(r in dotted.split(".") for r in _ENV_ROOTS)
        if leaf in _ENV_CALLS and root_ok or leaf == "getenv":
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                flag = _names_a_flag(arg)
                if flag:
                    self.hits.append((node.lineno, f"{dotted}(...{flag}...)"))
                    break
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        dotted = _dotted(node.value)
        if "environ" in dotted.split("."):
            flag = _names_a_flag(node.slice)
            if flag:
                self.hits.append((node.lineno, f"{dotted}[{flag!r}]"))
        self.generic_visit(node)


def scan_source(src: str, rel: str = "<stdin>") -> list[tuple[int, str]]:
    """Env reads of a removed flag in ``src``.

    Raises SyntaxError when ``src`` is not parseable — callers decide whether
    that is a skip (real files, fail-open) or a hard error.
    """
    if rel in _SELF_EXEMPT:
        return []
    visitor = _Visitor()
    visitor.visit(ast.parse(src))
    return sorted(set(visitor.hits))


def is_in_scope(rel: str) -> bool:
    """Mirror of check-no-ownership-flag.sh in_scope(), Python half only."""
    p = PurePosixPath(rel)
    if p.suffix != ".py":
        return False
    if rel in _SELF_EXEMPT:
        return False
    parts = p.parts
    if "tests" in parts or "__pycache__" in parts:
        return False
    if rel.startswith("core/config/upgrade-recipes/"):
        return False
    if rel.startswith("mind_api/docs/"):
        return False
    # PREFIX, not exact-parent. The shell's in_scope() matches with bash `case`
    # globs, where `*` SPANS `/` — so `core/scripts/*.py` was ALREADY covering
    # core/scripts/gates/*.py and core/scripts/audit_helpers/*.py. Porting that
    # as an exact-parent match dropped 21 tracked files (measured 2026-07-31)
    # off a surface the grep path had been watching. That would be a coverage
    # REGRESSION wearing a fix's clothes: the `*.py) return 1` early return in
    # the shell takes those files OFF the grep path, so this predicate is the
    # only thing that can put them back. Match the glob's reach, not its
    # literal text (guard-2094 — sweep the condition, not the token).
    return any(rel.startswith(root)
               for root in ("core/scripts/", "mind_api/src/", "mind_api/scripts/"))


class _GitError(RuntimeError):
    """git could not answer — the population is UNKNOWN, not empty."""


def _git(args: list[str], repo: Path, strict: bool = False) -> str:
    """Stdout of a git call. Non-strict returns '' when git fails.

    `strict` exists because '' is ambiguous and the ambiguity is dangerous in
    exactly one direction: a failed ENUMERATION and an empty tree produce the
    same value, so a caller that treats '' as the population reports `clean`
    for a surface it never read (the rb-245 zero-count class). Enumeration
    callers pass strict=True and let the error surface; `rev-parse`
    deliberately stays non-strict, because "not a work tree" genuinely means
    "nothing to gate" and failing open there is correct.

    TimeoutExpired/OSError are caught rather than propagated: uncaught they
    exit non-zero, which the pre-commit hook reads as BLOCKED — a gate that
    could not run must never refuse a commit (rb-246/guard-147).
    """
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=60
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        if strict:
            raise _GitError(f"git {' '.join(args)}: {exc}") from exc
        return ""
    if out.returncode != 0:
        if strict:
            raise _GitError(
                f"git {' '.join(args)} exited {out.returncode}: {out.stderr.strip()[:200]}"
            )
        return ""
    return out.stdout


def _added_lines(rel: str, repo: Path) -> set[int]:
    """Line numbers added to ``rel`` in the staged diff (new-file numbering).

    strict=True for the same reason the two enumeration callers use it: this
    derives a POPULATION from git, and on failure the non-strict '' collapses
    to an empty set, which the caller reads as "none of this file's hits are
    fresh" and passes the file CLEAN. That is the rb-245 zero-count class the
    other two sites were converted away from, and it is worse here — it fires
    only for a file already known to CONTAIN candidate hits, and it failed
    SILENTLY (rc=0) where the enumeration sites fail loudly (rc=3).
    """
    diff = _git(["diff", "--cached", "-U0", "--", rel], repo, strict=True)
    added: set[int] = set()
    cur = 0
    for line in diff.splitlines():
        if line.startswith("@@"):
            try:
                plus = line.split("+", 1)[1].split("@@", 1)[0].strip()
                cur = int(plus.split(",", 1)[0])
            except (IndexError, ValueError):
                continue
        elif line.startswith("+") and not line.startswith("+++"):
            added.add(cur)
            cur += 1
    return added


def _report(rel: str, hits: list[tuple[int, str]], label: str) -> None:
    for lineno, detail in hits:
        print(f"{label}: {rel}:{lineno} reads a removed flag — {detail}", file=sys.stderr)


def _fix_hint() -> None:
    print(
        "  OWNERSHIP_MODE and MACHINE_OWNED_AGENTS were removed 2026-07-02\n"
        "  (g-115-1737). Single-runner ownership is unconditional, keyed on\n"
        "  STORAGE_BACKEND. Fix the code, do not --no-verify.\n"
        "  See mind_api/docs/lodestar-dynamic-ownership-design.md (SUPERSEDED).\n"
        "  Mentioning the names in a docstring or comment is fine and is NOT\n"
        "  what this reports — only an executable env read is.",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audit", action="store_true",
                    help="scan the whole tracked tree instead of the staged diff")
    ap.add_argument("--paths", nargs="*", default=None,
                    help="scan these files explicitly (tests / ad-hoc repro)")
    args = ap.parse_args(argv)

    repo_out = _git(["rev-parse", "--show-toplevel"], Path.cwd())
    if not repo_out.strip():
        return 0  # not a work tree — fail open, never block
    repo = Path(repo_out.strip())

    def _scan_file(rel: str) -> list[tuple[int, str]] | None:
        f = repo / rel
        if not f.is_file():
            return None
        try:
            return scan_source(f.read_text(encoding="utf-8"), rel)
        except (SyntaxError, OSError, UnicodeDecodeError):
            return None  # unparseable/unreadable — fail open

    if args.paths is not None:
        rc = 0
        for rel in args.paths:
            hits = _scan_file(rel)
            if hits:
                _report(rel, hits, "HIT")
                rc = 1
        return rc

    if args.audit:
        rc = 0
        total = 0
        try:
            tracked = _git(["ls-files", "*.py"], repo, strict=True).splitlines()
        except _GitError as exc:
            print(f"ERROR: cannot enumerate tracked .py files — {exc}", file=sys.stderr)
            print("  The .py surface was NOT audited. This is not a clean result.",
                  file=sys.stderr)
            return 3  # distinct from 0/1: population unknown, caller must not report clean
        for rel in tracked:
            if not rel or not is_in_scope(rel):
                continue
            hits = _scan_file(rel)
            if hits:
                _report(rel, hits, "AUDIT HIT")
                total += len(hits)
                rc = 1
        if rc:
            print(f"audit: {total} OWNERSHIP_MODE / MACHINE_OWNED_AGENTS read(s) in Python",
                  file=sys.stderr)
        return rc

    # pre-commit: staged in-scope files, ADDED lines only.
    rc = 0
    try:
        staged = _git(["diff", "--cached", "--name-only"], repo, strict=True).splitlines()
    except _GitError as exc:
        print(f"ERROR: cannot enumerate staged files — {exc}", file=sys.stderr)
        print("  The .py surface was NOT checked. Commit is NOT blocked, but it was",
              file=sys.stderr)
        print("  also not cleared — re-run the gate once git is reachable.", file=sys.stderr)
        return 3
    unknown: list[str] = []
    for rel in staged:
        if not rel or not is_in_scope(rel):
            continue
        hits = _scan_file(rel)
        if not hits:
            continue
        try:
            added = _added_lines(rel, repo)
        except _GitError as exc:
            # This file HAS candidate hits and we could not learn which lines
            # are new. Do not fall through: an empty added-set would clear it.
            print(f"ERROR: cannot read the staged diff for {rel} — {exc}", file=sys.stderr)
            unknown.append(rel)
            continue
        fresh = [h for h in hits if h[0] in added]
        if fresh:
            _report(rel, fresh, "BLOCKED")
            rc = 1
    if rc:
        _fix_hint()
        return rc  # a real violation outranks an unreadable one
    if unknown:
        print(f"  {len(unknown)} file(s) with candidate hits were NOT checked: "
              f"{', '.join(unknown)}", file=sys.stderr)
        print("  Commit is NOT blocked, but these were also not cleared —", file=sys.stderr)
        print("  re-run the gate once git is reachable.", file=sys.stderr)
        return 3
    return rc


if __name__ == "__main__":
    sys.exit(main())
