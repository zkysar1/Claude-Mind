#!/usr/bin/env python3
"""Print ONE wrapper's accepted invocation surface, at the moment of use.

WHY THIS EXISTS (gap-066, g-115-4632)
-------------------------------------
`guard-136` / `guard-2172` / `guard-2350` already say: derive a wrapper's accepted
flags from its own parsing block, never by running `--help`, never by analogy. The
rules are correct and the trap still fires. Measured encounters: 5-6 misses in one
session (g-115-4175), 2 more in g-115-1955 (one reached a published board finding),
3 in ONE goal in g-115-4590, and 9 in one session in g-115-4632 -- the goal that
built this. That is the signature of a rule that needs a TOOL rather than more
emphasis (sig-48: a correct, retrievable rule that nothing READS at the moment of
the action; its prescription is to build a reader at the action point).

WHY IT IS AN EXTENSION, NOT A NEW PARSER
----------------------------------------
`skillmd-flag-audit.py` already carries the extraction engine -- `sh_flags`,
`py_flags`, `delegate_targets`, and a transitive `wrapper_surface` -- built and
dogfooded for a different consumer (auditing SKILL.md `Bash:` call sites). Nothing
exposed it as a QUERY. This module imports those functions rather than re-deriving
them, so the two consumers can never disagree about what a wrapper accepts
(communication-clarity rule 5: one source of truth). The hyphen in the engine's
filename makes it un-importable by name; it is loaded by file path via importlib,
the same indirection `test_infra_health_retire.py` uses.

WHAT THIS ADDS OVER THE ENGINE (the re-scope foxtrot's forge-hold required)
--------------------------------------------------------------------------
gap-066 was FORGE-READY on count and deliberately HELD (foxtrot, g-115-1955) on the
grounds that the procedure AS SCOPED -- "print accepted flags" -- would have caught
NEITHER encounter it had counted, and that forging the narrow version would set
status=forged and permanently suppress the gap. That hold was correct about the
blind spots and is answered here rather than routed around. Re-measured on the 9
misses of g-115-4632: 7 of 9 ARE the narrow flag/subcommand class, so the narrow
engine was worth exposing -- but on its own it would still have missed the other 2.
Four layers, because the failure lives at whichever one VALIDATES:

  1. FLAGS       -- the engine's transitive union (own parsing block + delegates).
  2. SUBCOMMANDS -- argparse `add_parser` / positional `choices`, and bare-word
     shell case arms. The engine collects only tokens starting with "-", so a
     subcommand dispatch is invisible to it. This was the single largest class in
     the g-115-4632 sample (4 of 9): `goal-selector.sh --top 8` (ate --top as its
     subcommand), `infra-health.sh read`, `tree-update.sh` (needs --set/--add-child),
     `aspirations-read.sh` (needs one of --id/--active/...).
  3. STDIN       -- whether the wrapper reads its payload from stdin instead of
     flags. 2 of 9: `board-post.sh` (no --body; message text is stdin) and
     `guardrails-add.sh` (stdin JSON, not flags). A flag list alone cannot say
     "there is no flag for this because it is not passed as a flag."
  4. DAEMON      -- guard-2374, and foxtrot's second blind spot: for a daemon-only
     wrapper the argparse/case block is the CLIENT's view; the ENFORCING party is
     the endpoint under mind_api/src/endpoints/. The wrapper forwards without
     validating, so a flag can exist here and its VALUE or its combination be
     rejected there. This layer never claims to know the endpoint's rules -- it
     names the endpoint and sends the reader to it, which is the honest limit.

Layer 4 is a POINTER, not an answer. Resolving a daemon field schema requires
reading the handler, which is judgment; everything this script prints is
mechanically derived and deterministic.

KNOWN LIMITS -- stated because an unstated limit is how a narrow tool passes its
own suite while the live trap keeps firing (guard-1760: report what you declined
to look for).

  1. ORCHESTRATOR WRAPPERS ARE NOISY. The .sh delegation union is CORRECT --
     a one-line `exec sibling.sh "$@"` wrapper genuinely accepts everything its
     delegate accepts, and reporting an empty surface for it is the silent-skip
     failure the engine's own delegate_targets docstring records fixing. But an
     ORCHESTRATOR defeats it at scale: `iteration-close.sh` reaches **84 .sh
     delegates and this prints 212 flags / 71 subcommands** (measured 2026-08-02,
     bravo, hostname cc-05, uname -r 6.8.0-136-generic), which is a wall of text
     rather than an answer. Those three numbers were first written here as
     "~80 / ~400 / ~90" from memory rather than from a run -- in a KNOWN LIMITS
     block added specifically to be accurate -- and two of the three were wrong
     by ~40%. guard-2189: enumerate by querying the source, never from memory;
     re-run the tool before quoting any of them again.
     Useful and low-noise on LEAF
     wrappers (board-post.sh: 5 flags + STDIN:YES; goal-selector.sh: 0 flags +
     2 subcommands; aspirations-query.sh: 6 flags), noisy above roughly a dozen
     delegates. Use it on leaves; on an orchestrator, read its own case block.
     aspirations-query.sh read 4 until 2026-08-09 and was re-measured, not
     re-derived, per the guard-2189 line just above: adopting the `_argv_strict`
     refusal (g-115-5214) added an `-h|--help)` arm, so `-h` and `--help` now
     appear. The `-*)` refusal arm itself does NOT — the engine collects only
     tokens starting with `-`, and `-*` is a glob, not a flag. So a wrapper's
     count RISES by exactly 2 on adoption; if you see +3 here, something else
     changed too.
     This line said "measured PRECISE on leaf wrappers" until limit 3 below was
     found by a live miss on a leaf. Low-noise and precise are different claims;
     only the first was measured.
     Candidate treatments, NOT prescribed (guard-2260 -- a remedy is a separate
     claim needing its own measurement): report the wrapper's OWN surface apart
     from the inherited union; announce-and-cap past N delegates rather than
     truncating silently; or detect the dispatcher shape and switch presentation.
     Whatever ships must keep test_py_delegates_are_terminal_not_expanded green.

  2. PER-SUBCOMMAND ARITY IS NOT MODELLED. `infra-health.sh status` is a real
     subcommand; that it takes no positional is one level deeper than this reads.
     Asserted as a KNOWN LIMIT in test_wrapper_surface.py rather than dropped.

  3. [FIXED 2026-08-10, g-115-3122 FIX 5 -- retained because the WORKAROUND it
     taught is now WRONG, and a reader who remembers the rule but not the fix
     would keep applying it.] SINGLE-LINE `case` VALIDATION IDIOMS YIELDED ZERO
     SUBCOMMANDS -- a FALSE NEGATIVE, on a LEAF, reported as the
     indistinguishable `(none)`. `_SH_WORD_ARM` is used with `.match()` and its
     pattern anchors at the line start, so it only saw an arm that STARTS a
     line. The compact one-liner
     `case "$SUB" in cat|list|head) ;; *) usage ;; esac` puts every arm after
     `case "$X" in` on the same line, and none was found. Alternation itself was
     handled fine; the LINE ANCHOR was the miss. Measured 2026-08-02 (bravo,
     hostname cc-05, uname -r 6.8.0-136-generic) on `backend-cat.sh` (line 34):
     it printed `SUBCOMMANDS: (none)`; the script has three.
     Found the expensive way, which is why it was limit 3 and not a test case:
     mid-goal I invoked `backend-cat.sh <path>` without its `cat` subcommand,
     got a usage error and an EMPTY stdout, compared that empty string against
     the real file, and concluded the store had DIVERGED -- a false alarm about
     a governed store, produced by exactly the trap this tool exists to prevent,
     on the day it shipped. I had not run the tool first; when I did, it would
     not have saved me. Both halves matter: the reflex to consult it, and its
     answer being right when consulted.
     FIX: `sh_subcommands` now splits the `case ... in` header line on `;;` and
     tests each chunk as its own arm. Verified 2026-08-10 (bravo, hostname
     cc-05, uname -r 6.8.0-136-generic): `backend-cat.sh` reports
     `SUBCOMMANDS: cat head list`; multi-line `case` wrappers are byte-unchanged;
     across all of `core/scripts/*.sh` EXACTLY ONE wrapper gained subcommands --
     the reproducer -- so the change has no collateral.
     THE OLD INTERIM RULE IS RETIRED: `(none)` no longer needs to be read as
     UNKNOWN *for this reason*. It is still not proof of "no subcommands" --
     limit 4 below is a separate and UNFIXED cause of a false `(none)`, so the
     grep confirmation remains worthwhile whenever the answer matters.

  4. DYNAMICALLY-REGISTERED SUBCOMMANDS ARE INVISIBLE, and this is a DIFFERENT
     limit from 3 -- do not fold them together, because 3 is a fixable regex bug
     and this is a bound on static reading. `py_subcommands` keys on the
     `add_parser` CALL SITE, so it sees `sub.add_parser("select")` and misses
     `sub.add_parser(name)` inside a loop, even when the names are plain
     literals a few lines above. Measured 2026-08-02 (bravo, hostname cc-05,
     uname -r 6.8.0-136-generic): `execution-diary.sh` reports 4 subcommands
     (append read summary trim); argparse actually offers 6 -- `phase-start` and
     `phase-end` are registered by a `for name, helptxt in (...)` loop at
     execution-diary.py:431-435 and never appear as a literal argument to
     `add_parser`. I hit this live in the same session as limit 3, by calling
     `execution-diary.sh append --goal ... --phase ...` when the shape is the
     `phase-end` SUBCOMMAND. Following simple loop-over-literals would recover
     this case; an f-string or a computed name never will, so the layer should
     eventually say WHICH kind it could not resolve rather than printing a
     shorter list. Same interim rule as limit 3: a subcommand list is a floor.

Usage:
    py -3 core/scripts/wrapper-surface.py <wrapper.sh|wrapper.py> [--json]
    bash core/scripts/wrapper-surface.sh <wrapper.sh|wrapper.py> [--json]
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PROJECT_ROOT = _HERE.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "core" / "scripts"
ENDPOINTS_DIR = PROJECT_ROOT / "mind_api" / "src" / "endpoints"


def _load_engine():
    """Import skillmd-flag-audit.py by path (its hyphen blocks a normal import).

    Fail LOUD rather than silently re-implementing: a second copy of the flag
    parsers is exactly the drift this module exists to avoid.
    """
    engine_path = SCRIPTS_DIR / "skillmd-flag-audit.py"
    spec = importlib.util.spec_from_file_location("_skillmd_flag_audit", engine_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load flag-surface engine at {engine_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- Layer 2: subcommands -------------------------------------------------

# A bare-word case arm: `select)` / `check|status)` / `add-child)`. Deliberately
# excludes arms starting with "-" (the engine owns those) and the `*)` catch-all.
_SH_WORD_ARM = re.compile(
    r"^\s*\(?\s*((?:[a-z][a-z0-9_-]*\s*\|\s*)*[a-z][a-z0-9_-]*)\s*\)")
# The `case <word> in` header, so the compact single-line form can be split into
# its arms (see sh_subcommands). Non-greedy between `case` and `in` so a subject
# containing the letters "in" does not over-consume.
_SH_CASE_HEADER = re.compile(r"\bcase\b.*?\bin\b")


def py_subcommands(path: Path) -> set[str]:
    """argparse subparser names + positional `choices` for one .py file.

    Two forms, both real here: `sub.add_parser("select")` (goal-selector.py) and
    `add_argument("command", choices=[...])`. A subcommand is not a flag, so
    py_flags() cannot see either.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute):
            continue
        if fn.attr == "add_parser":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.add(arg.value)
                    break
        elif fn.attr == "add_argument":
            # Positional with a choices= list is a subcommand dispatch in
            # everything but name.
            positional = bool(node.args) and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str) \
                and not node.args[0].value.startswith("-")
            if not positional:
                continue
            for kw in node.keywords:
                if kw.arg == "choices" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    for elt in kw.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            found.add(elt.value)
    return found


def sh_subcommands(path: Path) -> set[str]:
    """Bare-word case arms for one .sh file (comment lines excluded).

    TWO layouts, and the compact one was invisible until g-115-3122 FIX 5.
    `_SH_WORD_ARM` anchors at the line start and is used with `.match()`, so it
    only ever saw an arm that BEGINS a line. The compact single-line validation
    idiom puts every arm after the `case ... in` header on that same line:

        case "$SUB" in cat|list|head) ;; *) usage ;; esac

    and yielded ZERO. Measured on `backend-cat.sh:34`, which declares three
    subcommands and reported `SUBCOMMANDS: (none)`. The defect is the LINE
    ANCHOR, not the alternation handling.

    That false "(none)" is not cosmetic: it reads identically to "this wrapper
    has no subcommands", so a caller invokes the wrapper without one, gets a
    usage error plus EMPTY stdout, and can conclude a governed store has
    diverged — a false alarm produced by the very tool meant to prevent it.
    Hence the KNOWN LIMITS rule "treat (none) as UNKNOWN, not as none".

    Fix: when a line carries the `case ... in` header, split what follows on
    `;;` and test each chunk as its own arm. A multi-line `case` is untouched —
    its header line contributes no arms and its arms still match as before.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    found: set[str] = set()
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        header = _SH_CASE_HEADER.search(line)
        # Only the compact form yields extra segments; the multi-line form's
        # header has nothing after `in` and falls through harmlessly.
        segments = line[header.end():].split(";;") if header else [line]
        for seg in segments:
            m = _SH_WORD_ARM.match(seg)
            if not m:
                continue
            for alt in m.group(1).split("|"):
                alt = alt.strip()
                if alt and not alt.startswith("-"):
                    found.add(alt)
    return found


# --- Layer 3: stdin -------------------------------------------------------

_PY_STDIN = re.compile(r"\bsys\.stdin\b")
_SH_STDIN = re.compile(
    r"/dev/stdin|\bcat\s*(?:-|<&0)|\$\(\s*cat\s*\)|\bread\s+-r|"
    r"\bxargs\b|<&0|\bIFS=.*\bread\b")


def reads_stdin(path: Path) -> bool:
    """Whether the wrapper consumes a stdin payload (comment lines excluded)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    pat = _PY_STDIN if path.name.endswith(".py") else _SH_STDIN
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        if pat.search(line):
            return True
    return False


# --- Layer 4: daemon endpoint (guard-2374) --------------------------------

_RT_CALL = re.compile(r"\brt_call\b|_runtime\.sh|\bRT_DIR\b")
_ENDPOINT = re.compile(r"[\"']?(/v1/[a-z0-9/_-]+)")


def daemon_route(path: Path) -> dict:
    """Detect daemon routing and name the endpoint(s) the wrapper POSTs to.

    Returns {"routed": bool, "endpoints": [...], "note": str}. Deliberately does
    NOT try to read the endpoint's validation rules -- that is a judgment read of
    the handler, and claiming to have done it mechanically would be the exact
    over-reach guard-2374 warns about.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"routed": False, "endpoints": [], "note": ""}
    routed = False
    endpoints: set[str] = set()
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        if _RT_CALL.search(line):
            routed = True
        for m in _ENDPOINT.finditer(line):
            endpoints.add(m.group(1))
    note = ""
    if routed:
        note = (
            "DAEMON-ROUTED: the parsing block above is the CLIENT's view. The "
            "enforcing party is the endpoint handler under mind_api/src/endpoints/ "
            "-- a flag can exist here and its VALUE, FORMAT, or COMBINATION be "
            "rejected there (guard-2374). Grep the handler before trusting a "
            "help= string about what values a flag accepts."
        )
    return {"routed": routed, "endpoints": sorted(endpoints), "note": note}


# --- composition ----------------------------------------------------------

def describe(name: str) -> dict:
    """Full four-layer surface for one wrapper basename or path."""
    engine = _load_engine()

    path = Path(name)
    if not path.exists():
        path = SCRIPTS_DIR / Path(name).name
    if not path.exists():
        return {"wrapper": name, "error": "not_found",
                "detail": f"no such script under {SCRIPTS_DIR} (or as a path)"}

    base = path.name
    cache: dict = {}
    flags = engine.wrapper_surface(base, cache)

    # Subcommands + stdin are unioned across the delegation chain for the same
    # reason flags are: a one-line `exec sibling.sh "$@"` wrapper accepts
    # everything its delegate accepts, and reporting an empty surface for it is
    # the silent-skip failure the engine's own docstring records fixing.
    # MIRROR THE ENGINE'S RECURSION RULE EXACTLY: expand delegates only from .sh
    # files; a .py is TERMINAL. Getting this wrong is not cosmetic — the first
    # cut expanded .py delegates too, and `goal-selector.sh` transitively reached
    # ~250 scripts, so its subcommand list became every case arm in the codebase
    # and its "daemon endpoints" became all 62. That is precisely the
    # over-permissive union the engine's own delegate_targets docstring records
    # fixing, reintroduced one layer up: a surface that contains the right answer
    # among 200 wrong ones is not an answer. A .sh forwards "$@" to its
    # implementation; a .py IS the implementation and merely IMPORTS or SPAWNS
    # its neighbours, which does not widen what it accepts.
    chain = [path]
    seen = {base}
    frontier = [path] if base.endswith(".sh") else []
    while frontier:
        cur = frontier.pop()
        for tgt in engine.delegate_targets(cur):
            if tgt in seen:
                continue
            tgt_path = SCRIPTS_DIR / tgt
            if not tgt_path.exists():
                continue
            seen.add(tgt)
            chain.append(tgt_path)
            if tgt.endswith(".sh"):        # .py is terminal — do NOT expand it
                frontier.append(tgt_path)

    subs: set[str] = set()
    stdin_from: list[str] = []
    daemon = {"routed": False, "endpoints": [], "note": ""}
    for p in chain:
        subs |= py_subcommands(p) if p.name.endswith(".py") else sh_subcommands(p)
        if reads_stdin(p):
            stdin_from.append(p.name)
        d = daemon_route(p)
        if d["routed"]:
            daemon["routed"] = True
            daemon["note"] = d["note"]
        daemon["endpoints"] = sorted(set(daemon["endpoints"]) | set(d["endpoints"]))

    return {
        "wrapper": base,
        "path": str(path.relative_to(PROJECT_ROOT)) if str(path).startswith(str(PROJECT_ROOT)) else str(path),
        "flags": sorted(flags) if flags is not None else None,
        "flags_unparseable": flags is None,
        "subcommands": sorted(subs),
        "reads_stdin": bool(stdin_from),
        "stdin_sources": stdin_from,
        "delegates_to": [p.name for p in chain[1:]],
        "daemon": daemon,
    }


def render(d: dict) -> str:
    if d.get("error"):
        return f"WRAPPER: {d['wrapper']}\nERROR: {d['error']} -- {d.get('detail','')}"
    out = [f"WRAPPER: {d['wrapper']}  ({d['path']})"]
    if d["flags_unparseable"]:
        out.append("FLAGS: UNPARSEABLE -- surface unknown, do NOT infer it is empty.")
    else:
        out.append("FLAGS: " + (" ".join(d["flags"]) if d["flags"] else "(none)"))
    out.append("SUBCOMMANDS: " + (" ".join(d["subcommands"]) if d["subcommands"] else "(none)"))
    if d["reads_stdin"]:
        out.append("STDIN: YES -- payload is read from stdin, not a flag "
                   f"(in: {', '.join(d['stdin_sources'])})")
    else:
        out.append("STDIN: no")
    if d["delegates_to"]:
        out.append("DELEGATES TO: " + " ".join(d["delegates_to"])
                   + "  (surface above is the UNION across this chain)")
    if d["daemon"]["routed"]:
        eps = ", ".join(d["daemon"]["endpoints"]) or "(endpoint not literal in source)"
        out.append(f"DAEMON: {eps}")
        out.append("  " + d["daemon"]["note"])
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wrapper", help="wrapper basename (goal-selector.sh) or a path")
    ap.add_argument("--json", action="store_true", help="emit one JSON object")
    args = ap.parse_args()
    d = describe(args.wrapper)
    print(json.dumps(d, ensure_ascii=False) if args.json else render(d))
    return 1 if d.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
