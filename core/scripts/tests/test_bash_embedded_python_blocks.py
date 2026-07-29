"""Structural guard for python source embedded in bash via `python3 -c '...'`.

Origin: g-115-3565. `iteration-close.sh` built its corruption-canary Investigate
payload from f-strings whose EXPRESSIONS contained backslash-escaped double
quotes (`f"...{os.environ[\\"CANARY_BASENAME\\"]}..."`). A backslash is not
permitted inside an f-string expression, so the whole block was a SyntaxError.
The payload build returned empty, the empty string was piped onward, and the
call was guarded by `|| echo WARN ... (non-fatal)` -- so the alarm path for
aspirations.jsonl CORRUPTION filed nothing, and the only trace was one stderr
line nobody reads. `bash -n` passed the entire time.

TWO surfaces, so TWO assertions (guard-504):

  1. The extracted source must COMPILE. Catches the backslash case above.

  2. The source must contain NO apostrophe. Catches the strictly nastier case:
     the block is a single-quoted bash string, so an apostrophe TERMINATES it.
     An ODD count leaves it unterminated and `bash -n` fails loudly -- - safe,
     because it is noisy. An EVEN count closes and REOPENS, handing the text
     between the two apostrophes to bash for expansion while `bash -n` still
     passes. Correctness then depends on that text happening to be inert.
     Critically, assertion 1 CANNOT catch this: the raw extracted text still
     ast.parses fine, because the corruption happens at the bash layer, not the
     python layer. `iteration-close.sh` carried exactly this at ~L863, where a
     comment reading `IFS=$'\\t' read` reached python as `IFS=$t read`.

The enumeration is DERIVED from the filesystem rather than hand-listed
(guard-1648): a block added to any core/scripts/*.sh tomorrow is covered
without editing this test. Hand-listing the two known sites would have pinned
the two bugs already fixed and nothing else.
"""

import ast
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = PROJECT_ROOT / "core" / "scripts"

# A block opener is `python3 -c '` (or `py -3 -c '`) with the quote LAST on the
# line, i.e. the python source starts on the following line. The closing
# delimiter is an apostrophe at column 0.
_OPENER = re.compile(r"(?:python3|py -3)\s+-c\s+'\s*$")
_CLOSER = re.compile(r"^'")


def _scan_roots():
    """Directories whose *.sh get scanned.

    `core/scripts` is always in scope. The world script dir is added WHEN
    RESOLVABLE, because the canonical incidents of this very class
    (g-115-3430, g-115-3498) happened there, not in core -- a guard that skips
    the surface that motivated it is the guard-1291 failure mode. The world dir
    is an EXTERNAL, gitignored path, so it is genuinely absent on a fresh clone
    or in CI; that is a supported configuration, not a breakage, and the
    coverage simply narrows to core (which is why this is not the guard-139
    masking anti-pattern).
    """
    roots = [SCRIPTS_DIR]
    # Restore sys.path afterwards. A bare insert would leave core/scripts on the
    # path for the REST of the pytest process, where it can shadow modules for
    # unrelated tests sharing the chunk -- a cross-test side effect that would be
    # attributed to whatever test failed next, not to this one.
    saved = list(sys.path)
    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        from _paths import WORLD_DIR  # noqa: PLC0415

        if WORLD_DIR:
            world_scripts = Path(WORLD_DIR) / "scripts"
            if world_scripts.is_dir():
                roots.append(world_scripts)
    except Exception:
        pass
    finally:
        sys.path[:] = saved
    return roots


def _iter_blocks():
    """Yield (path, opener_lineno, source) for every embedded python block.

    Derived from the filesystem so new call sites are covered automatically.
    """
    for path in sorted(p for root in _scan_roots() for p in root.glob("*.sh")):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        i = 0
        while i < len(lines):
            if _OPENER.search(lines[i]):
                j = i + 1
                while j < len(lines) and not _CLOSER.match(lines[j]):
                    j += 1
                if j < len(lines):
                    yield path, i + 1, "\n".join(lines[i + 1 : j])
                    i = j
            i += 1


def _label(path):
    """Repo-relative when possible. The world dir is externally configured and
    may sit outside PROJECT_ROOT entirely, where relative_to raises."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def test_blocks_are_discovered():
    """The deriver must actually find blocks.

    Without this, a regex that silently matches nothing would make both real
    assertions below vacuously true -- the failure mode that lets a structural
    test pass forever while guarding nothing.
    """
    blocks = list(_iter_blocks())
    assert len(blocks) >= 20, (
        "expected the deriver to find many embedded python blocks in "
        f"{SCRIPTS_DIR}, found {len(blocks)}. If the invocation style changed, "
        "update _OPENER/_CLOSER -- do NOT delete this assertion, it is what "
        "keeps the other two from going vacuous."
    )


def test_embedded_python_blocks_compile():
    """Surface 1: every embedded block must be valid python (g-115-3565)."""
    failures = []
    for path, lineno, src in _iter_blocks():
        try:
            ast.parse(src)
        except SyntaxError as exc:
            rel = _label(path)
            failures.append(f"{rel}:{lineno} -> {exc.msg} (block line {exc.lineno})")
    assert not failures, (
        "embedded `python3 -c` block(s) do not compile:\n  "
        + "\n  ".join(failures)
        + "\n\nA backslash is not permitted inside an f-string expression. "
        "Inside a single-quoted bash string, double quotes need no escaping at "
        "all -- so drop the backslashes, or hoist the subscript into a local "
        "above the f-string (guard-504 remedy a). Note `bash -n` passes on this "
        "defect, which is why it must be caught by compiling the block."
    )


def test_embedded_python_blocks_have_no_apostrophe():
    """Surface 2: an apostrophe closes the single-quoted bash string (guard-504).

    Deliberately separate from the compile test: an even number of apostrophes
    still compiles as python, so the assertion above cannot catch it.
    """
    failures = []
    for path, lineno, src in _iter_blocks():
        if "'" not in src:
            continue
        rel = _label(path)
        offenders = [
            f"      block line {n}: {line.strip()[:90]}"
            for n, line in enumerate(src.splitlines(), 1)
            if "'" in line
        ]
        parity = "EVEN -- bash -n PASSES, string silently reopens" if src.count("'") % 2 == 0 else "odd -- string left unterminated"
        failures.append(
            f"{rel}:{lineno} ({src.count(chr(39))} apostrophes, {parity})\n"
            + "\n".join(offenders)
        )
    assert not failures, (
        "apostrophe(s) found inside single-quoted `python3 -c` block(s):\n  "
        + "\n  ".join(failures)
        + "\n\nThe block IS a single-quoted bash string, so an apostrophe ends "
        "it -- including one in a pure COMMENT, which is the likelier vector "
        "precisely because comments feel inert. Rewrite without the apostrophe "
        "(\"do not\" for \"don't\"; name the tab instead of writing $'\\t')."
    )
