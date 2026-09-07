#!/usr/bin/env python3
"""Derive the cross-agent glob consumer set from CODE and diff it against the
hand-maintained table in ``core/config/conventions/agent-dir-resolution.md``.

WHY THIS EXISTS (g-115-3862). CLAUDE.md's "Agent-dir Resolution" documents three
audit greps for an ``AGENTS_PARENT_DIR`` rename, then adds a table of cross-agent
glob consumers with this caveat: they "are invisible to all THREE greps above ...
This table is their only audit surface; check it on rename." A hand-maintained
list standing in for an invariant the language cannot see drifts silently, and
this one has: the goal measured 5 omissions on 2026-07-29 and this script found
more on 2026-09-06. Nothing fails until someone renames the constant and checks a
table that covers a fraction of the consumers — the exact depth-1 redrift the
caveat warns about, which has already zeroed skill-discovery's invocation sources
once.

WHY AST AND NOT GREP (guard-1099). A grep-based check matches the explanatory
comment as readily as the code, so a file that only *warns* about
``PROJECT_ROOT.glob("*/...")`` in a docstring reads as a live drift. Measured
while building this: a regex prototype reported three such docstrings
(``skill-discovery.py:217``, ``skill-latency-report.py:106``,
``endpoints/skill_discovery.py:156``) as drifted call sites. An AST cannot see a
comment and treats a docstring as a statement rather than a call, so the whole
false-positive class is excluded by construction rather than by an anchor pattern
someone must keep correct.

WHY THE RECEIVER IS RESOLVED THROUGH LOCALS. The canonical form is
``agents_root().glob("*/x")``, but two documented consumers take the root as an
ARGUMENT (``_frontier.py``, ``skill-latency-report.py``) and glob a local name.
Matching on the receiver TEXT alone therefore under-counts exactly the files the
convention holds up as the reference pattern. This walks assignments and
parameters first, so ``root = agents_root()`` then ``root.glob("*/x")`` resolves.

WHY THE PATTERN ALONE IS NOT ENOUGH EITHER. ``.glob("*/...")`` is common and
mostly NOT cross-agent — ``.claude/skills/*/SKILL.md``, ``*/session`` under a
sessions root, ``*/temp``. A pattern-only enumeration returned 59 sites where 21
were cross-agent. So a site counts only when the RECEIVER resolves to an agents
root, or when it resolves to a PROJECT root while the pattern names a per-agent
artifact — which is the drift signature.

WHY IT DIFFS RATHER THAN GENERATES. The goal asked to prefer generating the table
over diffing it. Read the table first: its rows are not a list of paths, they are
incident records — which drift zeroed which source, for how long, and what guards
it now. Generating would replace that with bare paths and delete the knowledge
that makes the table worth having. Subtraction is only elegance when what goes is
carrying cost; here the prose IS the value, and the drift is in the FILE SET
alone. So the file set is derived and checked, and the prose is left to people.

EXIT CODES
  0  clean: no drift, no undocumented consumer
  1  findings: a depth-1 drift, an undocumented consumer, or an unresolved receiver
  2  usage / could-not-scan (never a silent pass)

There is ONE mode and it is the audit (guard-4194: a dual-mode script runs its
default, and a default that scans nothing exits 0). ``--json`` changes only the
rendering.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent

# Roots scanned for consumers. Tests are excluded: a test may legitimately build
# a depth-1 glob over a tmp tree as a fixture, and flagging those would train
# readers to ignore the output (guard-1836's over-match failure, one level up).
SCAN_ROOTS = ("core/scripts", "mind_api/src")

CONVENTION = "core/config/conventions/agent-dir-resolution.md"
TABLE_ANCHOR = "**Plus cross-agent glob consumers**"

# The generated block. The PROSE table above it stays hand-written because its
# rows are incident records; this block carries the machine half — the file set
# — so that half stops drifting. Regenerate with --write.
GEN_BEGIN = "<!-- BEGIN GENERATED cross-agent-glob-consumers -->"
GEN_END = "<!-- END GENERATED cross-agent-glob-consumers -->"

# A receiver expression that resolves to the AGENTS root. Matched against
# `ast.unparse` output, so it covers agents_root(), _agents_root(),
# self._agents_root(), resolver._agents_root(), ctx.paths.agents_root and
# Path(agents_root()). The trailing \w* is load-bearing: the two documented
# take-the-root-as-an-argument consumers name their parameter `agents_root_path`
# / `agents_root_fn`, and a \b there fails on the underscore — which silently
# demoted the convention's own reference consumer (_frontier.py) to UNRESOLVED.
AGENTS_ROOT_RE = re.compile(r"\b_?agents_root\w*|\bagents_dir\b")

# A receiver built from the agent-dir CONSTANTS rather than the helper —
# `project_root / AGENTS_PARENT_DIR / agent / SESSIONS_DIRNAME`. These are NOT
# this table's concern: the constant name is right there in the source, so
# CLAUDE.md's three audit greps already see them. That is the whole reason the
# glob table exists separately — it covers the sites the greps CANNOT see.
CONSTANT_ROUTED_RE = re.compile(
    r"\bAGENTS_PARENT_DIR\b|\bSESSIONS_DIRNAME\b|\bSESSION_DIRNAME\b"
    r"|\bagent_dir\b|\bagent_sessions_root\b|\bagent_state_dir\b")

# A receiver expression that resolves to the PROJECT root. A glob from here at
# depth 1 is the redrift: post-relocation it matches NOTHING.
PROJECT_ROOT_RE = re.compile(r"\bPROJECT_ROOT\b|\bproject_root\b")

# Path fragments that only ever appear INSIDE an agent directory. A depth-1 glob
# from the project root naming one of these is drifted; a depth-1 glob naming
# SKILL.md is not a cross-agent glob at all.
PER_AGENT_ARTIFACTS = (
    "local-paths.conf",
    "aspirations.jsonl",
    "aspirations-archive.jsonl",
    "experience.jsonl",
    "experience-archive.jsonl",
    "skill-invocations.jsonl",
    "journal.jsonl",
    "self.md",
    "curriculum.yaml",
    "session/",
    "sessions/",
    "body-manifest.yaml",
    "body-heartbeat",
    "working-memory.yaml",
    "pending-questions.yaml",
    "execution-diary.jsonl",
    "running-session-id",
    "streak-breaks.jsonl",
    "temp/",
)


def _names_per_agent_artifact(pattern: str) -> bool:
    """True when a glob pattern reaches a file that lives inside an agent dir."""
    p = pattern.rstrip("/") + "/"
    return any(a in p for a in PER_AGENT_ARTIFACTS)


def _scope_bindings(scope: ast.AST) -> set:
    """Names bound, in ONE function scope, to an agents-root expression and to a
    constant-built agent path -- returned as the pair (agents_root, constant).

    PER-FUNCTION, not per-module, and that is not fastidiousness — a module-wide
    version of this produced a live false positive on the first run. In
    `skill-freshness-report.py` two sibling functions each bind a local named
    `base`: one to `skills_dir` (line 115) and one to `agents_root()` (line 145).
    Merged module-wide, the skills-dir binding inherited the agents-root
    classification and `base.glob("*/SKILL.md")` — a glob over `.claude/skills/`
    — was reported as an undocumented cross-agent consumer. A false positive here
    is the expensive direction: it lands in a table people trust and trains the
    next reader to skim the output.

    Nested defs are NOT descended into (they get their own scope on their own
    pass), which is why the walk is hand-rolled rather than `ast.walk`.
    """
    bound, const_bound = set(), set()
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # A parameter NAMED for the agents root is the documented
        # take-the-root-as-an-argument pattern (_frontier.py, skill-latency-
        # report.py). A parameter cannot be resolved further without
        # whole-program analysis, so this is a binding by NAME.
        for a in list(scope.args.args) + list(scope.args.kwonlyargs):
            if AGENTS_ROOT_RE.search(a.arg):
                bound.add(a.arg)

    body = getattr(scope, "body", [])
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue  # its own scope; an anonymous-function body closes over THIS one, so descend
        if isinstance(node, ast.Assign):
            try:
                src = ast.unparse(node.value)
            except Exception:  # noqa: BLE001 — unparse is best-effort
                src = ""
            target = (bound if AGENTS_ROOT_RE.search(src)
                      else const_bound if CONSTANT_ROUTED_RE.search(src)
                      else None)
            if target is not None:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        target.add(t.id)
        for child in ast.iter_child_nodes(node):
            stack.append(child)
    return bound, const_bound


def _receiver_names(expr: ast.AST) -> set:
    """Every bare NAME appearing in a receiver expression.

    Splitting on `.` and `(` — the obvious approach — reads `Path(ar())` as the
    name `Path` and never looks at `ar`, so `housekeeping-tick.py:304`
    (`Path(ar()).glob("*/experience.jsonl")`, with `ar` bound to `agents_root`)
    came back UNRESOLVED on the first run. Collect every Name instead and let any
    one of them match.
    """
    return {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}


def scan_file(path: Path, rel: str) -> list:
    """Every depth-1 glob call site in one module, classified. Never raises."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:  # noqa: BLE001
        return [{"file": rel, "line": 0, "kind": "unreadable", "detail": str(e)}]
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        # COULD-NOT-SCAN, never a clean file (rb-245 / guard-1091). A parse
        # failure that returned [] would read as "no consumers here" forever.
        return [{"file": rel, "line": e.lineno or 0, "kind": "unparseable",
                 "detail": str(e)}]

    out = []
    # One pass per SCOPE (module, then every function/lambda), so a local name
    # bound in one function never leaks its classification into a sibling.
    scopes = [tree] + [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for scope in scopes:
        bound, const_bound = _scope_bindings(scope)
        for node in _calls_in_scope(scope):
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "glob"):
                continue
            if not node.args:
                continue
            arg = node.args[0]
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                continue
            pattern = arg.value
            if not pattern.startswith("*/"):
                continue
            try:
                recv = ast.unparse(fn.value)
            except Exception:  # noqa: BLE001
                recv = "<unparseable>"

            names = _receiver_names(fn.value)
            if AGENTS_ROOT_RE.search(recv) or (names & bound):
                kind = "routed"
            elif CONSTANT_ROUTED_RE.search(recv) or (names & const_bound):
                kind = "constant-routed"
            elif PROJECT_ROOT_RE.search(recv):
                kind = ("drift" if _names_per_agent_artifact(pattern)
                        else "not-cross-agent")
            elif _names_per_agent_artifact(pattern):
                # The receiver could not be tied to any root, but the pattern
                # reaches into an agent dir. Reported, never counted as covered.
                kind = "unresolved"
            else:
                kind = "not-cross-agent"

            out.append({"file": rel, "line": node.lineno, "kind": kind,
                        "receiver": recv, "pattern": pattern})
    return out


def _calls_in_scope(scope: ast.AST):
    """Call nodes directly inside one scope, not descending into nested defs."""
    stack = list(getattr(scope, "body", []) or [])
    while stack:
        node = stack.pop()
        if node is not scope and isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.Call):
            yield node
        for child in ast.iter_child_nodes(node):
            stack.append(child)


def documented_files(root: Path) -> tuple:
    """File paths named in the convention's glob-consumer table.

    Returns (files, error). Scoped to the table that follows TABLE_ANCHOR — the
    document holds several tables and the constant-name table lists unrelated
    files (session-state-get.sh, iteration-commit.sh, ...) that would otherwise
    read as documented consumers.
    """
    conv = root / CONVENTION
    try:
        lines = conv.read_text(encoding="utf-8").splitlines()
    except OSError as e:  # noqa: BLE001
        return set(), f"cannot read {CONVENTION}: {e}"
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith(TABLE_ANCHOR))
    except StopIteration:
        return set(), (f"anchor {TABLE_ANCHOR!r} not found in {CONVENTION} — the "
                       "table moved or was renamed; this audit cannot diff "
                       "against a table it cannot locate")
    files, seen_row = set(), False
    # Files named in the GENERATED block count as documented: that block IS the
    # derived record, and requiring a prose row for each would re-create the
    # hand-maintenance this check exists to end.
    inside_gen = False
    for line in lines:
        if line.startswith(GEN_BEGIN):
            inside_gen = True
            continue
        if line.startswith(GEN_END):
            inside_gen = False
            continue
        if inside_gen:
            for m in re.finditer(r"`([^`]+):\d+`", line):
                files.add(m.group(1).strip())
    for line in lines[start:]:
        if line.startswith("| `"):
            seen_row = True
            for m in re.finditer(r"`([^`]+)`", line.split("|")[1]):
                v = m.group(1).strip()
                if "/" in v and (v.endswith(".py") or v.endswith(".sh")):
                    files.add(v)
        elif seen_row and not line.startswith("|"):
            break
    if not files:
        return set(), f"table under {TABLE_ANCHOR!r} yielded zero files"
    return files, None


def render_generated_block(res: dict) -> str:
    """The generated block's body: every derived cross-agent glob site."""
    rows = ["", GEN_BEGIN,
            "<!-- Derived from code by core/scripts/cross-agent-glob-audit.py.",
            "     DO NOT HAND-EDIT: regenerate with",
            "       py -3 core/scripts/cross-agent-glob-audit.py --write",
            "     The PROSE table above carries the incident history and IS",
            "     hand-maintained; this block carries only the file SET, which",
            "     is what silently drifted (19 consumers missing, 2026-09-06). -->",
            "",
            f"Derived cross-agent glob consumers ({len(res['routed'])} sites, "
            f"{len(res['code_files'])} files):",
            ""]
    for site in sorted(res["routed"], key=lambda s: (s["file"], s["line"])):
        rows.append(f"- `{site['file']}:{site['line']}` — "
                    f"`{site['receiver']}.glob({site['pattern']!r})`")
    rows += ["", GEN_END, ""]
    return "\n".join(rows)


def write_generated_block(root: Path, res: dict) -> str:
    """Replace (or append) the generated block in the convention. Returns a note."""
    conv = root / CONVENTION
    text = conv.read_text(encoding="utf-8")
    block = render_generated_block(res).strip("\n")
    if GEN_BEGIN in text and GEN_END in text:
        pre = text.split(GEN_BEGIN)[0].rstrip("\n")
        post = text.split(GEN_END, 1)[1].lstrip("\n")
        new = f"{pre}\n\n{block}\n\n{post}"
        note = "replaced"
    else:
        new = text.rstrip("\n") + "\n\n" + block + "\n"
        note = "appended"
    conv.write_text(new, encoding="utf-8")
    return f"{note} generated block in {CONVENTION} ({len(res['code_files'])} files)"


def audit(root: Path) -> dict:
    sites, scan_errors = [], []
    for r in SCAN_ROOTS:
        base = root / r
        if not base.exists():
            scan_errors.append(f"scan root missing: {r}")
            continue
        for p in sorted(base.rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            if "/tests/" in rel or p.name.startswith("test_"):
                continue
            for s in scan_file(p, rel):
                if s["kind"] in ("unreadable", "unparseable"):
                    # Normalised to a STRING here: scan_errors is rendered and
                    # JSON-emitted as a flat list of messages, and a mixed
                    # dict/str list makes every consumer of it type-check.
                    scan_errors.append(
                        f"{s['kind']} {s['file']}:{s['line']}: {s['detail']}")
                else:
                    sites.append(s)

    routed = [s for s in sites if s["kind"] == "routed"]
    drift = [s for s in sites if s["kind"] == "drift"]
    unresolved = [s for s in sites if s["kind"] == "unresolved"]
    constant_routed = [s for s in sites if s["kind"] == "constant-routed"]

    doc, doc_err = documented_files(root)
    if doc_err:
        scan_errors.append(doc_err)

    code_files = sorted({s["file"] for s in routed})
    undocumented = sorted(f for f in code_files if f not in doc)
    # A documented file with no live glob is NOT a failure: several rows describe
    # non-glob consumers (git pathspecs, bash enumerations) that this Python
    # scan cannot see by design. Advisory only.
    doc_without_glob = sorted(f for f in doc if f not in set(code_files))

    return {
        "scanned_roots": list(SCAN_ROOTS),
        "glob_sites_total": len(sites),
        "routed": routed,
        "drift": drift,
        "unresolved": unresolved,
        "constant_routed": constant_routed,
        "documented_files": sorted(doc),
        "code_files": code_files,
        "undocumented": undocumented,
        "documented_without_python_glob": doc_without_glob,
        "scan_errors": scan_errors,
    }


def render(res: dict) -> int:
    print(f"[cross-agent-glob-audit] scanned {', '.join(res['scanned_roots'])}: "
          f"{res['glob_sites_total']} depth-1 glob site(s), "
          f"{len(res['routed'])} cross-agent (routed), "
          f"{len(res['drift'])} DRIFTED, {len(res['unresolved'])} unresolved, "
          f"{len(res['constant_routed'])} constant-routed (covered by the "
          f"CLAUDE.md audit greps, not by this table)")
    print(f"  table documents {len(res['documented_files'])} file(s); "
          f"code has {len(res['code_files'])} routed consumer file(s)")

    if res["drift"]:
        print("\n  *** DEPTH-1 DRIFT — these glob a PROJECT root and match NOTHING "
              "post-relocation ***")
        for s in res["drift"]:
            print(f"    {s['file']}:{s['line']}  {s['receiver']}.glob({s['pattern']!r})")
    if res["unresolved"]:
        print("\n  UNRESOLVED receiver (reaches an agent-dir artifact; NOT counted "
              "as covered):")
        for s in res["unresolved"]:
            print(f"    {s['file']}:{s['line']}  {s['receiver']}.glob({s['pattern']!r})")
    if res["undocumented"]:
        print(f"\n  UNDOCUMENTED — routed cross-agent consumers absent from the "
              f"table ({len(res['undocumented'])}):")
        for f in res["undocumented"]:
            for s in res["routed"]:
                if s["file"] == f:
                    print(f"    {f}:{s['line']}  {s['receiver']}.glob({s['pattern']!r})")
    if res["documented_without_python_glob"]:
        print(f"\n  advisory — documented rows with no Python depth-1 glob "
              f"(bash / git-pathspec / non-glob consumers are expected here): "
              f"{', '.join(res['documented_without_python_glob'])}")
    if res["scan_errors"]:
        print("\n  SCAN ERRORS (this result is INCOMPLETE):")
        for e in res["scan_errors"]:
            print(f"    {e}")

    if res["scan_errors"]:
        return 2
    if res["drift"] or res["undocumented"] or res["unresolved"]:
        return 1
    print("\n  CLEAN: every routed cross-agent consumer is documented, no drift.")
    print("  SCOPE: this answers ENUMERATION only — whether the code's consumer set "
          "matches\n         the documented audit surface. It reads the CALLING CODE "
          "by AST and never\n         opens a globbed file, so it says NOTHING about "
          "whether a correctly-routed\n         read returns FRESH bytes. Routed-but-"
          "stale is a measured, separate defect\n         (g-115-9173): under own-cloud "
          "the local tree is a read-through cache and the\n         rows a glob misses "
          "are always the NEWEST ones. Do not read CLEAN as\n         'these consumers "
          "return current data' (guard-1760, guard-2302).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Derive cross-agent glob consumers from code and diff "
                    "against the agent-dir-resolution table.")
    ap.add_argument("--json", action="store_true", help="emit the raw result")
    ap.add_argument("--root", default=str(PROJECT_ROOT),
                    help="project root to scan (test seam)")
    ap.add_argument("--write", action="store_true",
                    help="regenerate the derived block in the convention file")
    args = ap.parse_args()

    root = Path(args.root)
    res = audit(root)
    if args.write:
        if res["scan_errors"]:
            # Never write a derived record from an INCOMPLETE scan — that would
            # bless a parse failure as "these are all the consumers" (rb-245).
            print("REFUSED --write: scan incomplete", file=sys.stderr)
            for e in res["scan_errors"]:
                print(f"  {e}", file=sys.stderr)
            return 2
        print(write_generated_block(root, res))
        res = audit(root)  # re-audit so the printed diff reflects the write
    if args.json:
        print(json.dumps(res, indent=2))
        if res["scan_errors"]:
            return 2
        return 1 if (res["drift"] or res["undocumented"] or res["unresolved"]) else 0
    return render(res)


if __name__ == "__main__":
    sys.exit(main())
