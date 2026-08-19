#!/usr/bin/env python3
"""doc-pin-map — which documentation lines are load-bearing for a test?

WHY THIS EXISTS (g-115-6471). The context-window-diet family (g-115-6466 /
6468 / 6469 / 6473) shrinks hot-path documentation: `CLAUDE.md` and
`.claude/rules/*.md` (the fixed per-turn preamble) and the loop-spec
`SKILL.md` files. The family's stated blocker was that "the test suite has
become the guardian of the bloat" — delete a paragraph, go red.

Before this script, the ONLY way to learn which prose a suite pins was to
delete it and watch what broke. That is a discovery mechanism with a red
suite as its output. This script makes the same map DERIVABLE up front:

    py -3 core/scripts/doc-pin-map.py --file .claude/rules/some-rule.md

reports every pin that reads that document, and classifies each by whether
its literal is hosted in a STATEMENT (a Bash line, an assignment, a heading,
a numbered step — which a prose diet preserves) or in NARRATIVE prose (which
a diet deletes). Only the NARRATIVE rows constrain a shrink.

MEASURED ON FIRST RUN (cc-08, 2026-08-17, Linux 6.8.0-137-generic): of 797
literal-bearing `Check:` pins in verify-learning/SKILL.md, 456 are hosted in
statements, 199 name a literal absent from the target (38 of those are
negative-polarity checks where absence IS the pass), 119 carry no literal at
all (LLM-judgement pins) — and **21 sit on narrative prose**. Exactly one of
those 21 is in `.claude/rules/`. The four largest rule files
(run-full-suite-after-deep-code.md, retrieve-before-deciding.md,
code-review-protocol.md, learning-philosophy.md — 116 KB combined, 38% of the
rules budget) have ZERO content pins from any source.

CLASSIFIER HONESTY (guard-3938). `_is_statement` applies ONE structural
convention to ~40 SKILL.md files written by many different agents, and those
files do NOT share a convention. The first version of this heuristic reported
96 narrative-hosted pins; refining it (numbered steps, ELIF, hyphenated YAML
keys, bold step headers, inline `Bash:`) cut that to 21 with no change to the
corpus. So treat NARRATIVE as "inspect this line", never as a verdict — every
row prints its host line verbatim so a reader can judge it in one glance.

POSITIVE CONTROLS (guard-2421, guard-1641). A scan that finds nothing and a
scan that never ran print the same silence. This one refuses to: it asserts
floors on checks parsed and targets resolved, prints them on every run, and
under --strict exits 2 when the scan has collapsed. Unresolved targets are
NAMED, never silently dropped (guard-3970 — a resolver fallback chain is an
enumeration claim).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VERIFY_LEARNING = PROJECT_ROOT / ".claude" / "skills" / "verify-learning" / "SKILL.md"

sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))
import _verify_corpus  # noqa: E402

# Floors below which the scan has collapsed and its zero means nothing.
# Set well under the measured values (797 literal pins / 40 targets on cc-08)
# so ordinary corpus churn never trips them, but a broken resolver does.
MIN_CHECKS = 200
MIN_TARGETS = 15

TEST_GLOBS = (
    "core/scripts/tests/*.py",
    "core/scripts/tests/*.sh",
    "mind_api/tests/*.py",
    "core/tests/gates/*.py",
)

# A doc target named inside a Check: line.
_TARGET_RE = re.compile(
    r"`?((?:\.claude/skills/)?[a-z0-9-]+/SKILL\.md"
    r"|CLAUDE\.md"
    r"|\.claude/rules/[a-z0-9-]+\.md)`?"
)
# Backticked or double-quoted literals long enough to be a real needle.
_LIT_RE = re.compile(r"`([^`]{4,90})`|\"([^\"]{4,90})\"")

# `echo "..."` / `echo '...'` payloads inside a check's Bash half. Human
# messages, never grep needles — see the narrowing block in
# scan_verify_learning for why leaving them in defeats that narrowing.
_ECHO_PAYLOAD_RE = re.compile(r"""echo\s+(["'])(?:(?!\1).)*\1""")

# Absence-is-pass phrasings. A check that asserts a literal is GONE reports
# "absent" for the same reason it passes, so its polarity has to be read
# before its result means anything.
#
# `\(not ` MUST sit OUTSIDE the \b group. `\b` before a literal `(` can never
# match — the char before it is a space, and space-then-paren is not a word
# boundary — so the whole "(not X)" family silently never fired. That family
# is how most of these checks phrase themselves ('table says "5-phase" (not
# "4-phase")'), and every miss lands in ABSENT, i.e. reads as a broken check
# when it is a passing one.
#
# `WITHOUT` was missing until 2026-08-17 and cost a phantom NARRATIVE on the
# only file this goal's own outcome list still showed as coupled: "invokes
# `from-self` WITHOUT `--plan`" read as a POSITIVE check, so the map went
# looking for `--plan`, found it in an unrelated phase, and reported prose
# coupling that does not exist. Absence phrasings are an open set — every one
# missed manufactures a warning, never suppresses one.
_NEG_RE = re.compile(
    r"(?:\b(?:does NOT|do NOT|MUST NOT|has ZERO|not the old|no longer"
    r"|NOT a\b|NOT v|never|must not|without|absent|no trace of)|\(not )",
    re.IGNORECASE,
)

# A host line that a prose diet preserves. Deliberately generous: a false
# STATEMENT costs a missed warning, a false NARRATIVE costs a wasted look,
# and over-warning is what makes a detector ignored.
_STMT_RE = re.compile(
    r"^("
    r"#{1,6} "                                   # markdown heading
    r"|<!--"                                     # html comment / pin marker
    r"|[-*] "                                    # bullet
    r"|>"                                        # blockquote
    r"|\d+(\.\d+)*[a-z]?[.)] "                   # numbered step: "3.5. " / "0.7) "
    r"|[a-z](\.\d+)*[.)] "                       # lettered step: "a. " "b) " "c.5. "
    r"|\*\*"                                     # bold step header
    r"|(IF|ELIF|ELSE|FOR|WHILE|RETURN|SET|THEN|DONE|SKIP|Bash|Check|Read"
    r"|Write|Edit|Skill|Output|Log|Invoke|Append|Parse|Run|Combine|Generate"
    r"|Record|Do NOT|NEVER|MUST)\b"
    r"|[A-Za-z_][A-Za-z_0-9./-]*\s*(=|\+=|:)"    # assignment / yaml key (hyphens!)
    r"|echo |bash |py |python|source |\$\(|`"    # shell
    r"|\)"                                       # continuation of a call
    r")"
)


def _host_class(line: str) -> str:
    """STATEMENT | TABLE | NARRATIVE for one host line.

    TABLE is split out of STATEMENT deliberately. A markdown table row is
    neither a rule the executor follows nor narrative it can lose — it is
    DATA, and data is precisely what the diet relocates to config (the brief
    calls the largest rule file "mostly dated baseline ROWS ... data, not a
    rule"). Folding table rows into STATEMENT told a reader "a diet preserves
    this" about the rows a diet is most likely to move. Measured on CLAUDE.md:
    25 of its 26 literal pins are hosted in Convention-Index / Core-Systems /
    User-Control table rows, and every one of them read STATEMENT before this
    split — a clean bill of health on the exact surface most at risk.
    """
    s = line.strip()
    if not s:
        return "NARRATIVE"
    if s.startswith("|"):
        return "TABLE"
    if _STMT_RE.match(s):
        return "STATEMENT"
    if re.search(r"\bBash:\s", s):  # "8. Bash: ..." — prefix is a step number
        return "STATEMENT"
    if re.match(r"^\s*[a-z_]+\(\)", s):
        return "STATEMENT"
    return "NARRATIVE"


def _strip_trailing_comment(line: str) -> str:
    """Drop a trailing ` # ...` annotation, respecting backtick spans.

    Backticks are tracked because Check: lines routinely embed shell in
    backticks, and a `#` there is a literal the assertion depends on — cutting
    at it would silently truncate the needle rather than the annotation.
    """
    in_tick = False
    for i, ch in enumerate(line):
        if ch == "`":
            in_tick = not in_tick
        elif ch == "#" and not in_tick and i > 0 and line[i - 1].isspace():
            return line[:i].rstrip()
    return line


def _resolve_target(token: str) -> Path | None:
    """Map a Check:-line token to a repo path. Returns None when unresolvable;
    callers must REPORT the None rather than skip it (guard-3970)."""
    tok = token.strip('`" ')
    if tok == "CLAUDE.md":
        return PROJECT_ROOT / "CLAUDE.md"
    if tok.startswith(".claude/rules/"):
        return PROJECT_ROOT / tok
    m = re.match(r"^(?:\.claude/skills/)?([a-z0-9-]+)/SKILL\.md$", tok)
    if m:
        return PROJECT_ROOT / ".claude" / "skills" / m.group(1) / "SKILL.md"
    return None


def scan_verify_learning() -> dict:
    """Every `Check:` line in verify-learning that names a doc target."""
    rows: list[dict] = []
    unresolved: list[dict] = []
    if not VERIFY_LEARNING.is_file():
        return {"rows": rows, "unresolved": unresolved,
                "error": f"verify-learning SKILL.md not found at {VERIFY_LEARNING}"}

    body_cache: dict[str, list[str]] = {}
    # Corpus, not the file: the checks moved to a registry on 2026-08-18
    # () and the thin SKILL.md yields 0 pins against MIN_CHECKS=200.
    # The corpus is byte-identical to the pre-cutover file, so the source_line
    # numbers emitted below still address the same lines they always did.
    lines = _verify_corpus.corpus_lines()

    for idx, raw in enumerate(lines, start=1):
        s = raw.strip()
        if not s.startswith("Check:"):
            continue
        # A trailing ` # ...` comment is ANNOTATION, not assertion. Without this
        # strip, a note like "# re-anchored off the CLAUDE.md index row" makes the
        # tool attribute the check to CLAUDE.md and then report its literal ABSENT
        # there — so the very act of documenting a re-anchor manufactures a phantom
        # pin on the file being decoupled. Measured while landing 's own
        # re-anchors: CLAUDE.md ABSENT went 1 -> 3 purely from the comments.
        # `#` inside a backticked span is left alone (a shell literal, not a comment).
        s = _strip_trailing_comment(s)
        tm = _TARGET_RE.search(s)
        if not tm:
            continue
        target = _resolve_target(tm.group(1))
        if target is None or not target.is_file():
            unresolved.append({"source_line": idx, "token": tm.group(1),
                               "check": s[:160]})
            continue
        rel = str(target.relative_to(PROJECT_ROOT))
        if rel not in body_cache:
            body_cache[rel] = target.read_text(
                encoding="utf-8", errors="replace").splitlines()
        body = body_cache[rel]

        literals = [a or b for a, b in _LIT_RE.findall(s)]
        literals = [x for x in literals
                    if x and x != tm.group(1) and not x.endswith("SKILL.md")]
        negative = bool(_NEG_RE.search(s))

        # PREFER THE EXECUTABLE NEEDLE. A Check line has two halves — a prose
        # claim and the `Bash:` command that tests it — and only the second
        # half's literals are what actually gets grepped. The prose half
        # PARAPHRASES: it writes `meter start` where the command greps
        # `aspirations-precheck-budget-meter.sh start`. Treating both as pins
        # searches the target for a string no check depends on, and a
        # paraphrase is short and generic, so it lands in a prose sentence and
        # is reported NARRATIVE — a phantom "the diet would break this" on a
        # pin whose real host is a `Bash:` line. All three of the last
        # remaining NARRATIVE pins in this goal's outcome list were this.
        #
        # Fallback is deliberate and must stay: when the Bash half quotes
        # nothing (an `assert x in y` needle, a py -3 one-liner), narrowing
        # would empty the set and silently drop a real pin, which is the
        # opposite failure and the worse one (guard-3970 — a filter that can
        # empty an enumeration must say so, not vanish).
        # Message payloads are not needles. Every check in this corpus ends
        # `... && echo "PASS: <human sentence>" || echo "FAIL: <sentence>"`,
        # and those sentences RESTATE the prose claim — so the paraphrase the
        # narrowing below exists to drop reappears verbatim inside the echo
        # and survives the membership test. Measured: `meter start` is absent
        # from the real needle (`...budget-meter.sh start`) yet present in
        # `echo "PASS: meter start+end wired"`, which kept the last phantom
        # NARRATIVE alive after the narrowing was already correct.
        literals = [x for x in literals
                    if not re.match(r"^(PASS|FAIL|WARN|OK|SKIP)\b\s*:", x)]

        needle_source = "whole-line"
        bm = re.search(r"\bBash:\s", s)
        if bm and literals:
            exec_half = _ECHO_PAYLOAD_RE.sub(" ", s[bm.end():])
            in_exec = [x for x in literals if x in exec_half]
            if in_exec:
                literals, needle_source = in_exec, "bash-half"

        if not literals:
            rows.append({"source": "verify-learning", "source_line": idx,
                         "target": rel, "literal": None, "verdict": "JUDGEMENT",
                         "negative": negative, "host_line": None, "host": None,
                         "check": s[:200], "needle_source": needle_source})
            continue

        for lit in literals:
            hosts = [(j, bl) for j, bl in enumerate(body, start=1) if lit in bl]
            if not hosts:
                rows.append({"source": "verify-learning", "source_line": idx,
                             "target": rel, "literal": lit,
                             "verdict": "ABSENT-PASS" if negative else "ABSENT",
                             "negative": negative, "host_line": None,
                             "host": None, "check": s[:200], "needle_source": needle_source})
                continue
            # Best-case host wins: one statement host is enough to say a
            # prose diet leaves the literal reachable. Ranked so a literal
            # that appears in BOTH a table row and a statement reports the
            # statement, and only a literal with no better host reports TABLE.
            kinds = {_host_class(bl) for _, bl in hosts}
            verdict = ("STATEMENT" if "STATEMENT" in kinds
                       else "TABLE" if "TABLE" in kinds
                       else "NARRATIVE")
            # POLARITY OUTRANKS HOSTING. A negative check asserts the literal
            # is GONE, and deleting text can only make that MORE true — so no
            # diet can break it, whatever kind of line currently happens to
            # host the string. Reporting it NARRATIVE tells a shrinker to
            # preserve prose in order to protect a check that wants the prose
            # removed, which is backwards.
            # This says nothing about whether the check currently PASSES: a
            # present host may mean the check is failing, or may mean the
            # literal sits outside the phase the check scopes itself to — and
            # the map does not resolve phase scope, so it must not imply one.
            if negative:
                verdict = "NEGATIVE"
            hl, htext = next(((j, bl) for j, bl in hosts
                              if _host_class(bl) == verdict), hosts[0])
            rows.append({"source": "verify-learning", "source_line": idx,
                         "target": rel, "literal": lit, "verdict": verdict,
                         "negative": negative, "host_line": hl,
                         "host": htext.strip()[:160], "check": s[:200], "needle_source": needle_source})
    return {"rows": rows, "unresolved": unresolved, "error": None}


# A test file that READS a live repo doc. `tmp_path`-rooted fixtures are not
# pins — only a path anchored at the real repo root couples to real content.
_LIVE_DOC_RE = re.compile(
    r"(?:REPO|REPO_ROOT|ROOT|PROJECT_ROOT|SCRIPTS|SCRIPT_DIR|CORE_SCRIPTS"
    r"|Path\(__file__\)[^\n]*?)"
    r"\s*(?:\.parents\[\d\]|\.parent)*\s*/\s*[\"']\.claude[\"']\s*/\s*[\"'](?:skills|rules)[\"']"
    r"[^\n]*?[\"']([a-z0-9-]+)[\"']"
)
_LIVE_INLINE_RE = re.compile(
    r"(?:REPO|REPO_ROOT|ROOT|PROJECT_ROOT|SCRIPTS|SCRIPT_DIR|CORE_SCRIPTS"
    r"|Path\(__file__\)[^\n]*?)"
    r"\s*(?:\.parents\[\d\]|\.parent)*\s*/\s*[\"']\.claude/(skills|rules)/([a-z0-9-]+)"
)
_READS_CONTENT_RE = re.compile(r"\.read_text\(|\bopen\(")


def scan_tests() -> list[dict]:
    """Test files that read a live repo doc's CONTENT, with their needles."""
    rows: list[dict] = []
    for pat in TEST_GLOBS:
        for f in sorted(PROJECT_ROOT.glob(pat)):
            text = f.read_text(encoding="utf-8", errors="replace")
            targets: set[str] = set()
            for m in _LIVE_DOC_RE.finditer(text):
                name = m.group(1)
                p = PROJECT_ROOT / ".claude" / "skills" / name / "SKILL.md"
                if p.is_file():
                    targets.add(str(p.relative_to(PROJECT_ROOT)))
            for m in _LIVE_INLINE_RE.finditer(text):
                kind, name = m.group(1), m.group(2)
                p = (PROJECT_ROOT / ".claude" / "skills" / name / "SKILL.md"
                     if kind == "skills" else
                     PROJECT_ROOT / ".claude" / "rules" / f"{name}.md")
                if p.is_file():
                    targets.add(str(p.relative_to(PROJECT_ROOT)))
            if not targets:
                continue
            reads = bool(_READS_CONTENT_RE.search(text))
            needles = []
            for ln in text.splitlines():
                s = ln.strip()
                if not re.match(r"(assert |self\.assert|pytest\.fail)", s):
                    continue
                if not re.search(r"\bin (text|src|skill|md|body|raw|content)\b", s):
                    continue
                for a, b in _LIT_RE.findall(s):
                    v = a or b
                    if v:
                        needles.append(v)
            for t in sorted(targets):
                rows.append({"source": str(f.relative_to(PROJECT_ROOT)),
                             "target": t,
                             "reads_content": reads,
                             "verdict": "CONTENT-PIN" if reads else "PATH-FIXTURE",
                             "needles": needles[:25]})
    return rows


def build(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", help="report only pins reading this doc "
                                   "(repo-relative or absolute)")
    ap.add_argument("--narrative-only", action="store_true",
                    help="show only NARRATIVE-hosted rows (the shrink blockers)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="exit 2 when a positive control fails")
    args = ap.parse_args(argv)

    vl = scan_verify_learning()
    tests = scan_tests()
    rows = vl["rows"]

    targets = {r["target"] for r in rows} | {r["target"] for r in tests}
    controls = {
        "checks_parsed": len(rows),
        "checks_floor": MIN_CHECKS,
        "targets_resolved": len(targets),
        "targets_floor": MIN_TARGETS,
        "test_files_coupled": len({r["source"] for r in tests}),
        "unresolved_targets": len(vl["unresolved"]),
    }
    collapsed = (controls["checks_parsed"] < MIN_CHECKS
                 or controls["targets_resolved"] < MIN_TARGETS
                 or vl["error"] is not None)
    controls["collapsed"] = collapsed

    unresolved = vl["unresolved"]
    if args.file:
        want = args.file
        if Path(want).is_absolute():
            want = str(Path(want).resolve().relative_to(PROJECT_ROOT))
        want = want.replace("\\", "/")
        rows = [r for r in rows if r["target"] == want]
        tests = [r for r in tests if r["target"] == want]
        # An unresolved token belongs to no file, so it cannot be attributed
        # to THIS one. Reporting the global list under a --file scope reads as
        # "your file has 2 broken pins" and sent one reader chasing a
        # placeholder token that was never about their file.
        unresolved = []
    if args.narrative_only:
        rows = [r for r in rows if r["verdict"] == "NARRATIVE"]

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    if args.json:
        print(json.dumps({"controls": controls, "counts": counts,
                          "verify_learning_pins": rows, "test_pins": tests,
                          "unresolved": vl["unresolved"]},
                         indent=2, ensure_ascii=True))
        return 2 if (collapsed and args.strict) else 0

    scope = args.file or "ALL documentation targets"
    print(f"[doc-pin-map] scope: {scope}")
    print(f"[doc-pin-map] POSITIVE CONTROL: {controls['checks_parsed']} "
          f"verify-learning pins parsed (floor {MIN_CHECKS}); "
          f"{controls['targets_resolved']} targets resolved "
          f"(floor {MIN_TARGETS}); "
          f"{controls['test_files_coupled']} test files coupled")
    if collapsed:
        print("[doc-pin-map] DEGRADED: a positive control failed — this scan's "
              "zeros mean NOTHING (guard-2421). Do not read an empty result "
              "below as 'no pins'.")
    if unresolved:
        # UNRESOLVED IS NOT A DEFECT REPORT. Both live cases are correct:
        # `recover/SKILL.md does NOT exist (skill deleted)` asserts absence,
        # so unresolved IS its pass; and `skill-dir/SKILL.md:line:type` is a
        # placeholder inside an error-format description, never a target.
        # They are printed because a resolver that drops what it cannot map
        # is making a silent enumeration claim (guard-3970) — read the token,
        # then decide.
        print(f"[doc-pin-map] {len(unresolved)} target token(s) named by a "
              f"Check: did not resolve to a file — NAMED, not dropped "
              f"(absence-asserting checks and prose placeholders land here "
              f"legitimately; read the token before filing anything):")
        for u in unresolved:
            print(f"    VL:L{u['source_line']}  token={u['token']!r}")

    print("\nverdict counts (verify-learning Check: pins in scope)")
    for k in ("NARRATIVE", "TABLE", "STATEMENT", "ABSENT", "ABSENT-PASS",
              "JUDGEMENT"):
        if k in counts:
            print(f"  {counts[k]:5d}  {k}")
    if not counts:
        print("     0  (no verify-learning pin reads this target)")

    for kind, blurb in (
        ("NARRATIVE", "a prose diet WOULD break these"),
        ("TABLE", "hosted in a markdown table ROW — safe from a PROSE diet, "
                  "but broken by relocating the table to config, which is "
                  "exactly what the diet does to data"),
    ):
        sel = [r for r in rows if r["verdict"] == kind]
        if not sel and args.narrative_only and kind != "NARRATIVE":
            continue
        print(f"\n{kind}-HOSTED ({len(sel)}) — {blurb}. Read each host line; "
              f"the classifier is a heuristic over files that share no "
              f"structural convention (guard-3938), not a verdict.")
        for r in sel:
            print(f"  VL:L{r['source_line']} -> {r['target']}:L{r['host_line']}")
            print(f"      literal: {r['literal']!r}")
            print(f"      host:    {r['host']}")

    if tests:
        print(f"\nTEST FILES reading this target ({len(tests)}):")
        for r in tests:
            print(f"  [{r['verdict']}] {r['source']} -> {r['target']}")
    elif args.file:
        print("\nTEST FILES reading this target: none")

    return 2 if (collapsed and args.strict) else 0


if __name__ == "__main__":
    sys.exit(build())
