#!/usr/bin/env python3
"""Compile every embedded python block in the shell-script corpus.

WHY THIS EXISTS (g-115-3566, generalizing g-115-3498).

`python3 -c '<source>'` hands the source to bash as a SINGLE-QUOTED string, and
inside a bash single-quoted string nothing escapes -- the very next `'`
terminates it. So one apostrophe anywhere in the block (an f-string key
`r['env']`, or ordinary prose in a COMMENT: "the sweep's output", "don't")
silently truncates the python source at that character and hands the remainder
to bash.

`bash -n` does NOT reliably catch this, which is the whole trap (guard-504):
an ODD apostrophe count leaves the string unterminated and fails loudly, but an
EVEN count CLOSES and REOPENS it -- bash -n passes, and correctness then depends
on the text between the two apostrophes happening to be inert. Accidental, not
designed.

guard-504's action_hint names the mechanical remedy this script implements:
extract the block and COMPILE it (ast.parse). That single check covers both the
odd and the even case, because either way the python that actually reaches the
interpreter is not the python that was written.

WHAT IS REUSED, AND WHY THAT MATTERS (guard-2222).

The quote-state scanner is IMPORTED from `extract-embedded-block.py`, never
re-implemented. That helper's `ends_inside_quote` walks real shell quoting rules
(backslash escapes outside quotes and inside double quotes; single quotes
suppress escaping) because the naive `count("'") % 2` parity test is wrong in
BOTH directions -- guard-1989 forbids it, and substituting it once made 5 corpus
checks over-capture. A second copy of that scanner here would be a second thing
to get wrong.

WHAT THIS ADDS THAT THE HELPER CANNOT DO.

`extract_shell()` returns the FIRST line matching an open marker and stops. It is
built for "run THIS one block", which is the right shape for its forged skill.
Measured on this corpus: 277 blocks across 124 files, of which 153 (55%) sit in
multi-block files and are therefore unreachable by any first-match extractor. So
enumeration -- every block in every file -- is genuinely new capability, not a
re-implementation of an existing one.

CLOSE-LINE RULE, measured rather than inherited.

The helper closes a block on a line whose rstrip() equals `'` exactly. On the
live corpus that shape is rare: real closers are `' 2>/dev/null)"`, `')`,
`' | bash ...`, `' || true`. Matching whole lines would silently miss most
blocks, so this script closes on the next `'` CHARACTER instead, which is what
bash itself does.

VERDICTS
  0  clean (or only quarantined findings)
  1  at least one un-quarantined block fails to compile
  2  operational error
"""
import argparse
import ast
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Single source of truth for shell quote-state (guard-2222 / guard-1989).
# Loaded under an explicit guard that exits 2, NOT 1. The `try/except` in
# __main__ cannot cover this: a module-level import runs BEFORE it, so an
# unguarded failure here exits 1 -- which this tool defines as "a block failed to
# compile". An operational failure would then be indistinguishable from real
# findings, and a CI caller would act on phantom offenders. That ERROR/FAIL merge
# is the precise thing extract-embedded-block.py guards against for itself
# ("ERROR (2), never FAIL (1)"); inheriting its scanner without inheriting that
# discipline was a fresh-eyes finding on this file's first review.
try:
    _spec = importlib.util.spec_from_file_location(
        "_eeb", str(PROJECT_ROOT / "core" / "scripts" / "extract-embedded-block.py")
    )
    _eeb = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_eeb)
    ends_inside_quote = _eeb.ends_inside_quote
except Exception as _e:  # noqa: BLE001 - any import failure is operational
    print("[embedded-python-audit] ERROR: cannot load the shared quote-state "
          "scanner from extract-embedded-block.py: %s" % _e, file=sys.stderr)
    sys.exit(2)

# `python3 -c '` / `py -3 -c '` / `python -c '` -- the single-quoted form only.
PAT_C = re.compile(r"(?:python3|py\s+-3|python)\s+-c\s+'")
# Heredoc opener; group(1) is the quote (if any) around the tag, group(2) the tag.
PAT_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
PY_TAGS = {"PY", "PYTHON", "PYEOF", "EOFPY", "PYCODE", "PYSRC"}

# A block may opt out with this marker on the opener line or the line above.
QUARANTINE = "embedded-python-audit: skip"


def _is_shell_trailer(rest):
    """True when text following a `'` looks like the block's closing delimiter.

    This is the discriminator between a real closer and a stray apostrophe, and
    it works because the two are followed by categorically different things.
    Measured on the live corpus: every real closer is followed by shell syntax
    (` 2>/dev/null)"`, `)`, `)"`, ` | bash ...`, ` || true`, or end-of-line),
    while a prose apostrophe is followed by a LETTER -- the possessive in
    "the sweep's output", the contraction in "don't". So the character right
    after the quote decides it, with no parsing required.
    """
    return rest == "" or rest[0] in " \t)\"|;&>"


def _is_quarantined(lines, i):
    """True when the opener at index `i` is opted out.

    The marker may sit on the opener itself or anywhere in the CONTIGUOUS
    comment block directly above it. Checking only `lines[i-1]` was too narrow
    to be usable: a quarantine worth having carries a rationale, a rationale
    runs to several lines, and the marker then lands on the FIRST of them while
    the opener sees only the last. Measured on the first real quarantine
    (test_check_daemon_endpoint_registry.sh) -- the opt-out silently did
    nothing and the finding persisted, which is the worst failure shape for an
    escape hatch, since the author believes it applied.
    """
    if QUARANTINE in lines[i]:
        return True
    j = i - 1
    while j >= 0 and lines[j].lstrip().startswith("#"):
        if QUARANTINE in lines[j]:
            return True
        j -= 1
    return False


def find_blocks(path, text):
    """Yield every embedded python block in one file.

    Returns dicts with kind, opener line (1-indexed), body, and quarantine flag.
    """
    lines = text.split("\n")
    out = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        # A COMMENT line is never a block opener. Skipping it is not a nicety:
        # this corpus documents its own embedded-python hazards IN comments, so
        # prose like "locates that writer by regex-matching its `python3 -c '`
        # prefix" matches the opener pattern and ends inside a quote. Without
        # this guard the scanner extracts English as python and reports a
        # confident SyntaxError against a file that is perfectly correct
        # (measured: 2 of 6 baseline findings, iteration-close.sh:414 and
        # orphan-root-sweep.sh:186, were both this artifact).
        if line.lstrip().startswith("#"):
            i += 1
            continue

        quarantined = _is_quarantined(lines, i)

        m = PAT_C.search(line)
        if m and ends_inside_quote(line):
            # Capture the INTENDED body -- to the structural closing delimiter --
            # NOT to the next apostrophe.
            #
            # Cutting at the next apostrophe is what bash does, but it makes the
            # audit blind to the very defect it exists to find. Measured on the
            # positive control: for `# do not trust the sweep's output` the cut
            # leaves `import json` plus a truncated comment, which is PERFECTLY
            # VALID python, so ast.parse returns clean and the block passes. The
            # compile check alone therefore does NOT cover the comment-apostrophe
            # case -- truncating a comment usually still yields a valid comment.
            #
            # So find the delimiter structurally and assert the invariant
            # directly (guard-504/guard-2035: NO apostrophe may appear anywhere
            # in a single-quoted -c block). The closer is the next line whose
            # stripped form starts with `'`; measured against the live corpus,
            # real closers are `' 2>/dev/null)"`, `')`, `' | bash ...`,
            # `' || true` -- all of that shape, and a whole-line `'` match (the
            # extract-embedded-block default) would have missed most of them.
            # The body starts on the OPENER line (`-c 'import sys,json`) and the
            # closer is usually MID-LINE (`except: print(0)' 2>/dev/null`), so
            # neither "body begins next line" nor "closer is a line starting with
            # a quote" survives contact with this corpus.
            region = [line[m.end():]] + lines[i + 1:]
            close_li = close_ci = None
            for k, rl in enumerate(region):
                for pos, ch in enumerate(rl):
                    if ch == "'" and _is_shell_trailer(rl[pos + 1:]):
                        close_li, close_ci = k, pos
                        break
                if close_li is not None:
                    break
            if close_li is None:
                out.append(dict(kind="c_single_quoted", line=i + 1,
                                body="\n".join(region),
                                quarantined=quarantined, unterminated=True))
                i += 1
                continue
            body_lines = region[:close_li] + [region[close_li][:close_ci]]
            out.append(dict(kind="c_single_quoted", line=i + 1,
                            body="\n".join(body_lines),
                            quarantined=quarantined, unterminated=False))
            i = i + close_li + 1
            continue

        hm = PAT_HEREDOC.search(line)
        if hm and hm.group(2) in PY_TAGS:
            tag = hm.group(2)
            body_lines = []
            # The body starts after the COMPLETE command, not after the opener's
            # physical line. A `\`-continued opener
            # (`python3 - "$V" <<'PY' 2>/dev/null || \` / `  printf '<fallback>'`)
            # carries its continuation lines BEFORE the heredoc body, and bash
            # begins the body only at the first UNESCAPED newline. Starting at
            # i+1 swallows the continuation line as python line 1 and reports a
            # phantom "SyntaxError ... unexpected indent" on a block bash runs
            # correctly — verified by running the shape (2026-08-10, cc-05).
            # A trailing backslash continues the line only when itself UNESCAPED,
            # i.e. an ODD run of backslashes; `\\` at EOL is a literal backslash.
            # Blast radius measured before the change (guard-1807): exactly 1 of
            # 133 python-tagged heredoc openers across both scanned roots is
            # `\`-continued, and it is the one this fix stops mis-reporting.
            j = i + 1
            while j < n and (len(lines[j - 1]) - len(lines[j - 1].rstrip("\\"))) % 2 == 1:
                j += 1
            while j < n and lines[j].strip() != tag:
                body_lines.append(lines[j])
                j += 1
            out.append(dict(
                kind="heredoc_" + ("quoted" if hm.group(1) else "unquoted"),
                line=i + 1, body="\n".join(body_lines),
                quarantined=quarantined, unterminated=(j >= n)))
            i = j + 1
            continue

        i += 1
    return out


def _expand_unquoted_heredoc(src):
    r"""Apply bash's backslash processing to an UNQUOTED heredoc body.

    An unquoted `<<PY` body is not literal text: bash rewrites it before python
    ever sees it, so compiling the RAW bytes audits a string that never reaches
    the interpreter. MEASURED on this box (g-115-7302), a backslash is special
    ONLY before `\`, `$` and a backtick. Every other `\x` -- `\d`, `\n`, `\"` --
    passes through untouched:

        RAW  s = "a \\"q\\" b"    ->  PYTHON GETS  s = "a \"q\" b"
        RAW  t = "back\\\\slash"  ->  PYTHON GETS  t = "back\\slash"
        RAW  u = "dollar \$H"     ->  PYTHON GETS  u = "dollar $H"
        RAW  w = "other \d \n"    ->  PYTHON GETS  w = "other \d \n"  (unchanged)

    THE FILING GOAL'S SUGGESTED REMEDY WAS HALF WRONG AND MEASUREMENT FALSIFIED
    IT (guard-1719 -- a diagnosis and its prescribed remedy carry different
    evidentiary weight). g-115-7302 proposed a two-substitution shape including
    `\"` -> `"`. Bash has no such rule: the flagged block's `\\"` becomes `\"`
    purely by the `\\` -> `\` rule, with the quote untouched. Adding a `\"` ->
    `"` rule would silently corrupt every escaped quote in an unquoted heredoc
    -- turning a false RED into a false GREEN, which is strictly worse.

    ONE LEFT-TO-RIGHT PASS, never sequential str.replace: replacing `\\`->`\`
    and then `\$`->`$` would rewrite `\\$` (a literal backslash followed by an
    expansion) all the way down to `$`, losing the backslash. A single scan
    cannot double-apply.

    `\<newline>` (bash line continuation) is deliberately NOT collapsed. Doing
    so shifts every later line number, and this function's only consumer
    reports `SyntaxError at block-relative line N`. It is also unnecessary:
    python reads `\<newline>` as its own line continuation, so both arms parse
    equivalently.
    """
    out = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c == "\\" and i + 1 < n and src[i + 1] in ("\\", "$", "`"):
            out.append(src[i + 1])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def check_block(b):
    """Check one block. Returns None when clean, else a reason string.

    TWO checks, because neither subsumes the other:

      apostrophe -- only for single-quoted `-c` blocks, where it is the direct
        invariant (guard-504/2035). Heredocs are exempt: a `<<'PY'` body is not
        a bash single-quoted string and apostrophes there are harmless.
      compile -- for every kind, catching breakage an apostrophe scan cannot see.

    The apostrophe check must run FIRST and independently: a block truncated at
    an apostrophe often still COMPILES (see find_blocks), so relying on compile
    alone silently passes the commonest real defect.
    """
    if b["unterminated"]:
        return "block never closes (unterminated quote/heredoc)"
    src = b["body"]
    if not src.strip():
        return None
    if b["kind"] == "c_single_quoted":
        # Imported, not re-implemented (guard-2222).
        offenders = _eeb.apostrophe_offenders(src)
        if offenders:
            return ("apostrophe inside a single-quoted -c block at "
                    "block-relative line(s) %s -- terminates the bash string, so "
                    "the python reaching the interpreter is truncated (guard-504)"
                    % ", ".join(str(x) for x in offenders))
    # Compile what the INTERPRETER receives, not the raw file bytes. bash
    # rewrites an unquoted heredoc body before python ever sees it, so auditing
    # the raw form reports a SyntaxError in a string that never existed
    # (). A QUOTED heredoc (`<<'PY'`) is literal by definition and is
    # deliberately NOT expanded -- that is precisely what quoting the delimiter
    # means, and expanding it would re-introduce the same class of false verdict
    # in the opposite direction.
    parse_src = src
    if b["kind"] == "heredoc_unquoted":
        parse_src = _expand_unquoted_heredoc(src)
    try:
        ast.parse(parse_src)
    except SyntaxError as e:
        return "SyntaxError at block-relative line %s: %s" % (e.lineno, e.msg)
    except ValueError as e:
        return "ValueError: %s" % e
    return None


def _world_root_from_authority():
    """Resolve world/ from the AUTHORITY that defines it, not from ambient env.

    `WORLD_PATH` is exported by `_paths.sh`, so reading ONLY the environment
    made this tool's scope a property of HOW IT WAS LAUNCHED. Sourced wrapper ->
    2 roots; bare `py -3 embedded-python-audit.py` -> 1 root, a "clean" verdict,
    and rc=0 over half the corpus. Measured 2026-08-10 (zeta, hostname cc-02,
    uname -r 6.8.0-136-generic): 137 blocks / 1 root vs 276 blocks / 2 roots --
    139 of 276 (50.4%) unscanned, with nothing in the output saying so.

    That is illusion #7 in `test-coverage-illusions` (green conditional on the
    runner) reaching the SCANNER rather than a test: the pytest standing guards
    below invoke this file as a bare subprocess with no env of their own, so
    their verdict was a property of the shell that launched pytest.

    `_paths.py` is the single source of truth every other framework consumer
    resolves through, and it already carries the MIND_WORLD override, the
    conf lookup, and the .mind-data convention. Deriving from it here rather
    than re-implementing a third chain is guard-308 (never duplicate a resolved
    constant) and guard-3097 (derive the denominator from the authority that
    DEFINES the population, never from what this box happens to expose).

    Returns (path_or_None, state):
      "scanned"              resolved, world/scripts present -- it IS in scope
      "configured-absent"    resolved, but no scripts/ dir under it
      "unresolved"           the authority could not be read

    An unresolved authority NEVER refuses (guard-3097 clause 1): a failed
    enumeration means "we learned nothing", not "the set is empty". A fresh
    clone with no world configured must still audit core/scripts and exit on
    its findings alone.
    """
    # An explicit WORLD_PATH in the environment stays authoritative -- the
    # wrapper sets it, and honoring it keeps a deliberate override working.
    # The import below is the FALLBACK that makes the bare shape correct.
    candidates = []
    wp = os.environ.get("WORLD_PATH")
    if wp:
        candidates.append(Path(wp))
    else:
        try:
            _pspec = importlib.util.spec_from_file_location(
                "_eeb_paths", str(PROJECT_ROOT / "core" / "scripts" / "_paths.py")
            )
            _pmod = importlib.util.module_from_spec(_pspec)
            _pspec.loader.exec_module(_pmod)
            if getattr(_pmod, "WORLD_DIR", None):
                candidates.append(Path(_pmod.WORLD_DIR))
        except Exception:  # noqa: BLE001 - unreadable authority is "unknown"
            return None, "unresolved"
    if not candidates:
        return None, "unresolved"
    world = candidates[0]
    scripts = world / "scripts"
    if scripts.exists():
        return scripts, "scanned"
    return scripts, "configured-absent"


def iter_roots(explicit):
    """Resolve the roots to scan. Returns (roots, world_state).

    An explicit --root REPLACES the defaults rather than adding to them. Adding
    made the flag unusable for the only caller that needs it: a test pointing at
    a fixture dir also swept the live corpus, so counts were polluted and a
    hermetic case could never assert on its own findings. That replacement is
    load-bearing and is pinned by a negative control in the suite -- appending
    the world root unconditionally would silently un-hermeticize every fixture
    test (guard-1836: a coverage assertion has no power against over-matching,
    so it must be paired with a control naming what must NOT be scanned).
    """
    if explicit:
        return [Path(e) for e in explicit if Path(e).exists()], "explicit-root"
    roots = [PROJECT_ROOT / "core" / "scripts"]
    world_scripts, state = _world_root_from_authority()
    if state == "scanned":
        roots.append(world_scripts)
    return roots, state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", default=[],
                    help="root to scan (repeatable); REPLACES the default "
                         "core/scripts + world/scripts pair when given")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true", help="list blocks, run no checks")
    args = ap.parse_args()

    # Resolve roots ONCE and REPORT them. A verdict of "clean" is only as wide as
    # the scope it was computed over, and this tool can silently narrow: with
    # WORLD_PATH unset (or _paths.sh failing in the wrapper) it scans core/scripts
    # alone and still prints "clean" -- 128 blocks instead of 230, with nothing in
    # the output saying so. A checker that reports what it RAN but never what it
    # declined to look for hands the reader a false all-clear (guard-1760).
    roots, world_state = iter_roots(args.root)
    findings, total, quarantined = [], 0, 0
    for root in roots:
        for f in sorted(root.rglob("*.sh")):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for b in find_blocks(f, text):
                total += 1
                if b["quarantined"]:
                    quarantined += 1
                    continue
                reason = check_block(b)
                if reason:
                    findings.append(dict(file=str(f), line=b["line"],
                                         kind=b["kind"], reason=reason))

    roots_str = [str(r) for r in roots]
    if args.json or args.list:
        print(json.dumps(dict(total_blocks=total, quarantined=quarantined,
                              roots_scanned=roots_str, world_root_state=world_state,
                              findings=findings), indent=2))
    else:
        print("[embedded-python-audit] scanned %d blocks (%d quarantined) across %d root(s): %s"
              % (total, quarantined, len(roots_str), ", ".join(roots_str)))
        # Print the SCOPE on every run, clean and dirty alike (guard-3097). A
        # verdict is only as wide as the roots it was computed over, and a tool
        # that reports what it READ but never what it declined to look for hands
        # the reader a false all-clear (guard-1760). Deliberately NOT an exit
        # code: an unreadable authority means "we learned nothing", not "the set
        # is empty", so a fresh clone with no world configured still audits
        # core/scripts and exits on its own findings (guard-3097 clause 1).
        if world_state not in ("scanned", "explicit-root"):
            print("[embedded-python-audit] SCOPE: world root %s -- this run did "
                  "NOT cover world/scripts, so 'clean' below is only as wide as "
                  "the root(s) listed above" % world_state)
        for x in findings:
            print("  FAIL %s:%s [%s] %s" % (x["file"], x["line"], x["kind"], x["reason"]))
        if not findings:
            print("[embedded-python-audit] clean")
    return 1 if findings else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # operational failure must not read as "clean"
        print("[embedded-python-audit] ERROR: %s" % e, file=sys.stderr)
        sys.exit(2)
