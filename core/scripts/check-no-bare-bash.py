#!/usr/bin/env python3
"""Layer B gate: refuse a bare-``"bash"`` argv[0] in Python subprocess calls.

guard-580 forbids ``subprocess.run(["bash", ...])`` from Python. On win32,
CreateProcess with ``lpApplicationName=NULL`` searches ``System32`` BEFORE
``PATH``, so a bare ``"bash"`` resolves to ``C:/Windows/System32/bash.exe`` —
the WSL launcher — even when Git Bash sits earlier in PATH. On a box whose WSL
is broken the launcher BLOCKS FOREVER on a dead LxssManager: 0-CPU processes
accumulate, the parent hangs in ``communicate()``, and pytest's faulthandler
bound aborts the whole run. The fix is to resolve the binary explicitly via
``core/scripts/_runtime_bash.py`` ``BASH`` / ``bash_cmd()`` (production) or
``core/scripts/tests/_bash_helpers.py`` ``BASH`` (tests).

WHY A GATE AND NOT JUST THE GUARDRAIL (rb-5255): the honour-system layer was
tested under the most favourable conditions available and still failed. Having
just swept this bug class out of 12 production sites and rescoped guard-580 to
name the hang mechanism, the same author wrote a one-off script using a bare
argv[0] within the hour, and it hung exactly as predicted. Correctly-scoped
rule + freshly read + author actively fixing that class = still reintroduced.
Only Layer B closes it.

WHY ``ast`` AND NOT ``grep`` (the deviation from g-115-3171's stated shape):
the goal specified a grep-based checker that "MUST NOT flag docstring/comment
references", naming ``dependent-unblock.py`` as a legitimate prose mention.
That mention lives inside a multi-line triple-quoted docstring, not a ``#``
comment — so the comment-stripping grep used by the sibling gate
``check-no-python-cli-fallback.sh`` would flag it. Matching prose exclusion
with grep needs multi-line docstring state tracking; ``ast`` gives it for
free and exactly: comments never enter an AST at all, and a docstring is an
``Expr(Constant(str))`` statement, never a call argument. Both false-positive
classes vanish structurally rather than by heuristic. The trade-off accepted:
``ast`` requires syntactically-valid Python, so an unparseable file is
reported as a skip rather than silently passing.

DETECTION — the three syntactic forms g-115-3171 requires:

    (a) call-site literal      subprocess.run(["bash", script])
    (b) concatenation          cmd = ["bash"] + cmd
    (c) shell=True string      subprocess.run("bash x.sh", shell=True)

Forms (a) and (b) are ONE rule here: any list literal whose FIRST element is
the constant ``"bash"`` is an argv being built, wherever it appears. That
subsumes the call-site form, the assignment form, and the concatenation form
without dataflow analysis — which is what the first g-115-3085 sweep missed
(form (b) in ``monitor-tick.py`` was found only by a second, broader grep).

OVERRIDE — for a genuinely POSIX-only path:

    subprocess.run(["bash", x])   # allow-bare-bash: linux-only CI shim
    # allow-bare-bash: reason                 (preceding line also works)
    # allow-bare-bash-file: reason            (whole file, anywhere in it)

Lineage: goal g-115-3171 (this gate); guard-580 (the rule); rb-5143 (the
7-site sweep); rb-5255 (the same-hour reintroduction that promoted this to
HIGH); g-115-3085 (the Windows hang investigation); ``_runtime_bash.py`` (the
sanctioned fix). Sibling Layer-B gates: ``check-no-python-cli-fallback.sh``
(the wiring precedent), ``check-no-ownership-flag.sh``.
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path, PurePosixPath

# Scope: the surfaces  names, as an explicit predicate rather than
# glob strings.
#
# WHY NOT ``Path.match("mind_api/src/**/*.py")``: ``PurePath.match`` does NOT
# treat ``**`` as a recursive wildcard (that is ``full_match`` in 3.13+); it
# degrades to a single ``*``, so the pattern requires exactly one intermediate
# directory and silently MISSES files sitting directly in ``mind_api/src/`` —
# ``agent_paths.py``, ``lifecycle.py``, and every other top-level daemon module.
# A scope that under-covers is worse than no gate, because it reads as covered.
# Caught by test_scope_globs; the depth-1 glob-drift class CLAUDE.md documents.
FLAT_SCOPE_DIRS = frozenset(
    {
        "core/scripts",
        "core/scripts/gates",
        "core/scripts/tests",
        "world/scripts",
    }
)
RECURSIVE_SCOPE_DIRS = ("mind_api/src",)

# The detector may come to hold the pattern as its own detection material —
# never flag it, or it could never be committed (precedent:
# check-no-python-cli-fallback.sh line 70).
#
# Deliberately NOT exempting the test file: because detection is AST-based, a
# fixture embedded in a Python string literal is a Constant str, never a List
# whose elts[0] is "bash", so fixtures are safe WITHOUT an exemption. Exempting
# the test file would instead hide a real violation written into it. A test that
# genuinely needs a bare argv[0] uses the documented marker, which self-documents.
SELF_EXEMPT = frozenset({"core/scripts/check-no-bare-bash.py"})

LINE_MARKER = "allow-bare-bash:"
FILE_MARKER = "allow-bare-bash-file:"

# argv[0] spellings that re-trigger the System32 search. A path containing a
# separator ("/bin/bash", "C:/.../bash.exe") is an EXPLICIT resolution and is
# out of scope for this gate — it names a binary rather than delegating the
# search to CreateProcess.
BARE_NAMES = frozenset({"bash", "bash.exe"})

DISPATCH_FUNCS = frozenset(
    {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
)


class _Visitor(ast.NodeVisitor):
    """Collect (lineno, form, snippet) for each bare-bash argv construction."""

    def __init__(self) -> None:
        self.hits: list[tuple[int, str, str]] = []

    # -- forms (a) + (b): any list literal whose first element is "bash" ----
    def visit_List(self, node: ast.List) -> None:
        if node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if first.value in BARE_NAMES:
                    self.hits.append(
                        (
                            node.lineno,
                            "list-argv",
                            f'["{first.value}", ...] argv literal',
                        )
                    )
        self.generic_visit(node)

    # -- form (c): shell=True whose command string starts with "bash" -------
    def visit_Call(self, node: ast.Call) -> None:
        if self._is_dispatch(node.func) and self._has_shell_true(node):
            if node.args:
                lead = self._leading_literal(node.args[0])
                if lead is not None and self._starts_with_bare_bash(lead):
                    self.hits.append(
                        (
                            node.lineno,
                            "shell-string",
                            f'shell=True command string "{lead.strip()[:40]}"',
                        )
                    )
        self.generic_visit(node)

    @staticmethod
    def _is_dispatch(func: ast.expr) -> bool:
        if isinstance(func, ast.Attribute):
            return func.attr in DISPATCH_FUNCS
        if isinstance(func, ast.Name):
            return func.id in DISPATCH_FUNCS
        return False

    @staticmethod
    def _has_shell_true(node: ast.Call) -> bool:
        for kw in node.keywords:
            if kw.arg == "shell":
                v = kw.value
                if isinstance(v, ast.Constant) and v.value is True:
                    return True
        return False

    @staticmethod
    def _leading_literal(arg: ast.expr) -> str | None:
        """Leading literal text of a str constant or an f-string."""
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.JoinedStr) and arg.values:
            head = arg.values[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                return head.value
        return None

    @staticmethod
    def _starts_with_bare_bash(text: str) -> bool:
        stripped = text.lstrip()
        for name in BARE_NAMES:
            if stripped == name:
                return True
            if stripped.startswith(name) and stripped[len(name)] in " \t":
                return True
        return False


def _is_in_scope(rel: str) -> bool:
    p = PurePosixPath(rel)
    if p.suffix != ".py":
        return False
    parent = p.parent.as_posix()
    if parent in FLAT_SCOPE_DIRS:
        return True
    return any(
        parent == d or parent.startswith(d + "/") for d in RECURSIVE_SCOPE_DIRS
    )


def scan_source(src: str, rel: str = "<stdin>") -> list[tuple[int, str, str]]:
    """Return violations in ``src``, honoring override markers.

    Raises SyntaxError when ``src`` is not parseable — callers decide whether
    that is a skip (audit/pre-commit over real files) or a hard error.
    """
    if rel in SELF_EXEMPT:
        return []
    lines = src.splitlines()
    if any(FILE_MARKER in ln for ln in lines):
        return []
    tree = ast.parse(src)
    visitor = _Visitor()
    visitor.visit(tree)

    kept: list[tuple[int, str, str]] = []
    for lineno, form, detail in visitor.hits:
        if _line_override(lines, lineno):
            continue
        kept.append((lineno, form, detail))
    return sorted(set(kept))


def _line_override(lines: list[str], lineno: int) -> bool:
    """True when the violating line, or the one above it, carries the marker."""
    for idx in (lineno - 1, lineno - 2):
        if 0 <= idx < len(lines) and LINE_MARKER in lines[idx]:
            return True
    return False


def _git(args: list[str], cwd: Path) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60
    )
    return out.stdout if out.returncode == 0 else ""


def _added_lines(rel: str, repo: Path) -> set[int]:
    """Line numbers added to ``rel`` in the staged diff (new-file numbering)."""
    diff = _git(["diff", "--cached", "-U0", "--", rel], repo)
    added: set[int] = set()
    cur = 0
    for line in diff.splitlines():
        if line.startswith("@@"):
            # @@ -a,b +c,d @@
            try:
                plus = line.split("+", 1)[1].split("@@", 1)[0].strip()
                start = int(plus.split(",", 1)[0])
            except (IndexError, ValueError):
                continue
            cur = start
        elif line.startswith("+") and not line.startswith("+++"):
            added.add(cur)
            cur += 1
    return added


def _report(rel: str, hits: list[tuple[int, str, str]], label: str) -> None:
    for lineno, form, detail in hits:
        print(f"{label}: {rel}:{lineno} [{form}] {detail}", file=sys.stderr)


def _fix_hint() -> None:
    # NEVER print a `[BASH, str(path)]` remedy here. This hint is read at the one
    # moment an author is looking for a shape to copy, so a remedy that satisfies
    # guard-580 while violating guard-581 propagates the second defect under the
    # authority of the first gate. It did: the tests: line used to read
    # `subprocess.run([BASH, str(SCRIPT)], ...)`, and a 2026-08-01 sweep found 8
    # live `[BASH, str(` sites across 7 files. str(WindowsPath) reaches bash with
    # backslashes, which it treats as escape introducers and strips -- invisible
    # on Linux, where str() and .as_posix() are identical by definition. Both
    # lines below now pass the path through a helper that enforces .as_posix().
    print(
        "  A bare 'bash' argv[0] resolves to System32 WSL on win32 and can hang\n"
        "  forever (guard-580). Resolve it explicitly instead:\n"
        "    production:  from _runtime_bash import bash_cmd\n"
        "                 subprocess.run(bash_cmd('core/scripts/x.sh', arg), ...)\n"
        "    tests:       from _bash_helpers import BASH\n"
        "                 subprocess.run([BASH, Path(SCRIPT).as_posix()], ...)\n"
        "  Pass script paths via bash_cmd() or .as_posix(), never str(Path):\n"
        "  bash silently strips the backslashes of a str(WindowsPath) (guard-581).\n"
        "  Genuinely POSIX-only? Add '# allow-bare-bash: <reason>' on the line\n"
        "  (or '# allow-bare-bash-file: <reason>' for the whole file).\n"
        "  Do not --no-verify; fix the code.",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--audit",
        action="store_true",
        help="scan the whole tracked tree instead of the staged diff",
    )
    mode.add_argument(
        "--snippet",
        action="store_true",
        help="scan Python source on stdin (authoring-time layer)",
    )
    ap.add_argument(
        "--paths", nargs="*", default=None, help="explicit files to scan (ignores scope globs)"
    )
    args = ap.parse_args(argv)

    repo_out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=60
    )
    if repo_out.returncode != 0:
        # Not a git work tree — nothing to gate. Fail open, never block.
        return 0
    repo = Path(repo_out.stdout.strip())

    # ---- snippet mode: stdin is Python source (ad-hoc / one-off code) ----
    if args.snippet:
        src = sys.stdin.read()
        try:
            hits = scan_source(src, "<snippet>")
        except SyntaxError:
            # Not parseable Python — out of this gate's reach. Fail open.
            return 0
        if hits:
            _report("<snippet>", hits, "BLOCKED")
            _fix_hint()
            return 1
        return 0

    # ---- explicit paths (fixture testing) ----
    if args.paths is not None:
        rc = 0
        for raw in args.paths:
            p = Path(raw)
            try:
                src = p.read_text(encoding="utf-8")
            except OSError as exc:
                print(f"SKIP: {raw} unreadable ({exc})", file=sys.stderr)
                continue
            try:
                hits = scan_source(src, p.as_posix())
            except SyntaxError as exc:
                print(f"SKIP: {raw} unparseable ({exc})", file=sys.stderr)
                continue
            if hits:
                _report(raw, hits, "HIT")
                rc = 1
        return rc

    # ---- audit mode: whole tracked tree ----
    if args.audit:
        rc = 0
        total = 0
        tracked = _git(["ls-files", "*.py"], repo).splitlines()
        for rel in tracked:
            if not rel or not _is_in_scope(rel):
                continue
            f = repo / rel
            if not f.is_file():
                continue
            try:
                hits = scan_source(f.read_text(encoding="utf-8"), rel)
            except (SyntaxError, OSError):
                continue
            if hits:
                _report(rel, hits, "AUDIT HIT")
                total += len(hits)
                rc = 1
        if rc == 0:
            print("audit clean: no bare-bash argv[0] in scoped Python")
        else:
            print(f"audit: {total} bare-bash argv[0] site(s)", file=sys.stderr)
        return rc

    # ---- pre-commit mode (default): staged files, ADDED lines only ----
    # Added-lines scoping matches check-no-python-cli-fallback.sh: pre-existing
    # sites never block an unrelated commit, but any NEW introduction does.
    rc = 0
    staged = _git(["diff", "--cached", "--name-only"], repo).splitlines()
    for rel in staged:
        if not rel or not _is_in_scope(rel):
            continue
        f = repo / rel
        if not f.is_file():
            continue
        try:
            hits = scan_source(f.read_text(encoding="utf-8"), rel)
        except (SyntaxError, OSError):
            continue
        if not hits:
            continue
        added = _added_lines(rel, repo)
        fresh = [h for h in hits if h[0] in added]
        if fresh:
            _report(rel, fresh, "BLOCKED")
            rc = 1
    if rc:
        _fix_hint()
    return rc


if __name__ == "__main__":
    sys.exit(main())
