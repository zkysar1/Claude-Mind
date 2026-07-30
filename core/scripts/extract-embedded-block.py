#!/usr/bin/env python3
"""Extract an embedded executable block from a host file and run it VERBATIM.

Companion script for the `extract-and-run-embedded-block` forged skill
(gap-042). The capability is narrow and deliberate: pull code that lives
INSIDE another file out of it and execute the exact bytes, never a paraphrase.
Paraphrasing is the failure this exists to prevent -- a hand-retyped check body
tests the retyping, not the check (probe-with-canonical-code-path.md: canonical
BINARY is not canonical INVOCATION).

TWO HOST GRAMMARS.

  check  -- `   Bash (<name>): <body>` lines in a SKILL.md (the /verify-learning
            check corpus is the reference instance). Bodies may span many lines.
  shell  -- a python block embedded in a .sh, delimited by a caller-supplied
            open marker (`python3 -c '`, `py -3 -c '`, a `PY` heredoc, ...) and
            a close line.

THE CAPTURE RULE FOR `check`, AND WHY IT IS INDENT-BASED (measured, not assumed).

gap-042 records the rule as "capture until the next line matching
`^   (Bash \\(|#|->)`". Measured against the live corpus on 2026-07-30
(318 checks): that rule OVER-CAPTURES on 25 of them (7.9%), because `   Check:`
prose lines follow one-line checks and match none of those three tokens. The
swallowed prose then dies as a shell syntax error that is indistinguishable
from a failing check -- footgun 2 from the gap, on a token the gap does not name.

The robust discriminator is INDENTATION, not the token:

  * every structural line in the host grammar sits at indent EXACTLY 3
    (`   Bash (`, `   Check:`, `   #`, `   ->`)
  * a genuine continuation line of an embedded block is column-0-anchored,
    so its indent is 0, or 4/8/12/16/20 from the embedded language's own
    nesting -- never 3.

So: continue while the line is non-blank AND its indent is not exactly 3.
This is token-agnostic, which means a NEW structural token added to the host
file cannot silently reopen the over-capture hole. Under this rule the same
corpus yields 16 genuinely multi-line checks, the largest +27 lines with
balanced quotes.

VERDICT CLASSIFICATION -- four states, not two.

A bare `PASS` prefix test is wrong in both directions and both were hit live:

  * ratchet-style checks legitimately emit `[name] STABLE:` / `RATCHETED:` /
    `SEEDED:` / `REGRESSED:` and would be scored as failures (gap-042 footgun 3)
  * a body that CRASHES emits neither vocabulary, and scoring that as a failure
    manufactured "5 of 6 apparent reds" that were harness artifacts (g-115-3280)

Hence ERROR (the harness broke -- rc != 0 or an empty capture) is reported
separately from FAIL (the assertion fired), and INDETERMINATE (rc == 0 but no
verdict vocabulary) is reported separately from PASS. A silent rc=0 is the
emptiest signal there is: a check that DECLINES to run reports success by
default, so green is its only observable state (rb-5871, guard-1977). This tool
refuses to render that as PASS.

AUTHORING-TIME READS. `--from` reads the host file from the working tree
(default), the git index, or any ref. The working-tree default is load-bearing:
the motivating consumer (g-115-3960) needs to run a check body BEFORE it is
committed, and a design that only reads HEAD satisfies the two post-hoc audit
consumers while being unusable for that one.
"""
import argparse
import json
import os
import re
import subprocess
import sys

# The test suite loads this file via spec_from_file_location, which does NOT put
# core/scripts on sys.path the way running it as a script does. Without this the
# lazy `from _runtime_bash import BASH` in run_body() would import fine in
# production and ImportError only under test -- the wrong way round.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_CHECK_FILE = ".claude/skills/verify-learning/SKILL.md"

# Host-grammar structural indent. Every `   Bash (`, `   Check:`, `   #`,
# `   ->` line sits here; embedded continuation lines never do.
STRUCTURAL_INDENT = 3

CHECK_START = re.compile(r"^ {3}Bash \(([^)]+)\):[ ]?(.*)$")

# SAME-LINE trailing prose. gap-042 names only the FOLLOWING-line `-> expect`
# form; measured 2026-07-30, 12 of 318 live checks put the prose on the Bash
# line itself. The delimiter is a U+2192 arrow in 10 of them.
#
# ASCII ` -> ` is deliberately NOT a delimiter here, and that is a measured
# decision rather than caution: BOTH live instances of it sit INSIDE the quoted
# PASS/FAIL message text (`...released=False -> exit 5 (...)`), so treating it
# as a delimiter truncates those two bodies mid-string. The naive reading
# ("arrows introduce prose") corrupts exactly the cases it claims to fix.
PROSE_ARROW = "→"

# Verdict vocabulary. Matched at line start OR after a `[prefix] ` tag, so
# `[orphan-ratchet] STABLE: ...` classifies the same as a bare `STABLE: ...`.
PASS_TOKENS = ("PASS", "STABLE", "RATCHETED", "SEEDED")
FAIL_TOKENS = ("FAIL", "REGRESSED")
_VOCAB = re.compile(
    r"^(?:\[[^\]]*\]\s*)?(" + "|".join(PASS_TOKENS + FAIL_TOKENS) + r")\b",
    re.MULTILINE,
)


def die(msg):
    """Extraction failed -- exit ERROR (2), never FAIL (1).

    `raise SystemExit("text")` prints the text but exits 1, which is THIS
    tool's FAIL code. Every failure routed here is the harness breaking (a
    name that does not resolve, an absent marker, a bad git ref) rather than
    an assertion firing, so reporting them as FAIL is exactly the ERROR/FAIL
    merge this tool exists to prevent (g-115-3280). Found by Phase 5 verify
    of the forging goal -- in the tool itself, against its own contract.
    """
    print(msg, file=sys.stderr)
    raise SystemExit(2)


def read_host(path, source):
    """Return host-file text from the worktree, the index, or a git ref.

    `staged` and a ref both go through `git show`, which needs a
    repo-relative path; the worktree read does not.
    """
    if source in (None, "worktree"):
        # die(), not a bare open(): an unreadable host file is the CALLER being
        # wrong, so it must report ERROR(2). Letting OSError escape gives a raw
        # traceback and exit 1 -- this tool's FAIL code -- which is the same
        # ERROR/FAIL collapse guard-1993 covers, in the one path that never used
        # SystemExit and so survived that sweep. Found by the fresh-eyes pass.
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            die("cannot read host file %s: %s" % (path, exc))
    spec = ":" + path if source == "staged" else source + ":" + path
    # argv list, never a shell string: a bare "bash"/shell hop here would
    # reintroduce the argv[0] resolution hazard (guard-580).
    proc = subprocess.run(["git", "show", spec], capture_output=True, text=True)
    if proc.returncode != 0:
        die("git show %s failed: %s" % (spec, proc.stderr.strip()[:300]))
    return proc.stdout


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def ends_inside_quote(text):
    """True when `text` ends with an unterminated shell quote.

    A naive `count('"') % 2` is wrong in both common directions and measurably
    so: `grep -qF '_commit_sha="$(printf'` has an ODD double-quote count with
    no quote open at all (they sit inside single quotes), and an escaped `\\"`
    inside a double-quoted string flips the parity the other way. Substituting
    the counter for this scanner made 5 corpus checks over-capture.

    Shell rules, minimally: outside quotes a backslash escapes the next
    character; single quotes suppress all escaping until the next single quote;
    inside double quotes a backslash escapes again.
    """
    in_single = in_double = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_double = False
        else:
            if ch == "\\":
                i += 1
            elif ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
        i += 1
    return in_single or in_double


def strip_trailing_prose(body):
    """Split a captured body into (code, prose) at a QUOTE-BALANCED arrow.

    Quote-awareness is the whole rule, not a refinement of it. Measured on the
    live corpus: 9 of the 10 U+2192 arrows sit outside any quote and genuinely
    introduce prose, but `transplant-no-copytree` carries one INSIDE a
    double-quoted message string. A position-only strip silently truncates that
    body mid-string and the result dies as a shell syntax error -- the same
    signal a failing check gives, which is the confusion this tool exists to
    remove. So an arrow only delimits when the text before it has balanced
    double AND single quotes.
    """
    idx = 0
    while True:
        k = body.find(PROSE_ARROW, idx)
        if k < 0:
            return body, ""
        head = body[:k]
        if head.count('"') % 2 == 0 and head.count("'") % 2 == 0:
            return head.rstrip(), body[k:].strip()
        idx = k + 1


def extract_check(text, name):
    """Extract one named check body. Returns (body, start_line, n_continuation)."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = CHECK_START.match(line)
        if not m or m.group(1) != name:
            continue
        body = [m.group(2)]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if nxt.strip() == "":
                break
            # A structural token can never appear inside an OPEN quote, so an
            # indent-3 line only terminates when the body so far is
            # quote-balanced. Without this, a multi-line block whose
            # continuation lines happen to be indented 3 (rather than
            # column-0) truncates to its opening `py -3 -c "` and dies with
            # `unexpected EOF while looking for matching quote` -- gap-042's
            # footgun 1, which the indent rule alone does NOT fix.
            # Measured 2026-07-30: this recovers 6 silently-truncating checks
            # (+1 to +15 lines each) and changes no other capture in the corpus.
            if indent_of(nxt) == STRUCTURAL_INDENT and not ends_inside_quote("\n".join(body)):
                break
            body.append(nxt)
            j += 1
        code, prose = strip_trailing_prose("\n".join(body))
        return code, i + 1, j - i - 1, prose
    die("check not found: %s" % name)


def list_checks(text):
    out = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = CHECK_START.match(line)
        if m:
            out.append({"name": m.group(1), "line": i + 1})
    return out


def extract_shell(text, open_marker, close_line):
    """Extract a block delimited by an open marker and a close line.

    The open marker is matched as a SUBSTRING because call sites differ
    (`python3 -c '` vs `py -3 -c '` vs a heredoc tag); the close line is
    matched as the whole rstripped line so a `'` inside the body cannot
    terminate the capture early.
    """
    lines = text.split("\n")
    start = None
    for i, line in enumerate(lines):
        if open_marker in line:
            start = i + 1
            break
    if start is None:
        die("open marker not found: %r" % open_marker)
    for j in range(start, len(lines)):
        if lines[j].rstrip() == close_line:
            return "\n".join(lines[start:j]), start, j - start
    die("close line %r not found after open marker %r" % (close_line, open_marker))


def apostrophe_offenders(src):
    """1-indexed line numbers containing an apostrophe.

    A single quote anywhere inside a bash-single-quoted `python3 -c '...'`
    block TERMINATES the block, so its absence is a real invariant of the
    host script -- asserting it here doubles as a regression check on the
    host, not merely a precondition for this extraction.
    """
    return [k + 1 for k, line in enumerate(src.split("\n")) if "'" in line]


def classify(rc, stdout, stderr):
    """Map an execution result onto {PASS, FAIL, ERROR, INDETERMINATE}."""
    if rc != 0:
        return "ERROR", "body exited %d -- harness artifact, NOT an assertion failure" % rc
    hits = _VOCAB.findall(stdout)
    has_fail = any(h in FAIL_TOKENS for h in hits)
    has_pass = any(h in PASS_TOKENS for h in hits)
    if has_fail:
        return "FAIL", "emitted %s" % ",".join(h for h in hits if h in FAIL_TOKENS)
    if has_pass:
        return "PASS", "emitted %s" % ",".join(h for h in hits if h in PASS_TOKENS)
    return ("INDETERMINATE",
            "rc=0 but no verdict vocabulary in stdout -- a check that DECLINES "
            "to run reports success by default; this is NOT a pass")


def run_body(src, interpreter, stdin_text, env_extra, timeout):
    from _runtime_bash import BASH  # rb-1472: bin-first, honors MIND_SHELL
    env = dict(os.environ)
    for pair in env_extra or []:
        if "=" not in pair:
            die("--env expects KEY=VALUE, got %r" % pair)
        k, v = pair.split("=", 1)
        env[k] = v
    # BASH, never a bare "bash" argv[0]: on win32 that resolves via CreateProcess,
    # which searches System32 BEFORE PATH and reaches the WSL launcher, where it
    # can hang forever on a wedged LxssManager (guard-580).
    argv = [BASH, "-c", src] if interpreter == "bash" else [sys.executable, "-c", src]
    proc = subprocess.run(argv, input=stdin_text, capture_output=True,
                          text=True, env=env, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def main():
    ap = argparse.ArgumentParser(
        description="Extract an embedded executable block from a host file and run it verbatim.")
    ap.add_argument("--grammar", choices=["check", "shell"], default="check")
    ap.add_argument("--file", help="host file (default: %s for check grammar)" % DEFAULT_CHECK_FILE)
    ap.add_argument("--from", dest="source", default="worktree",
                    help="worktree (default) | staged | any git ref")
    ap.add_argument("--name", help="check name (check grammar)")
    ap.add_argument("--list", action="store_true", help="enumerate check names and exit")
    ap.add_argument("--open-marker", help="substring identifying the opening line (shell grammar)")
    ap.add_argument("--close-line", default="'",
                    help="exact rstripped closing line (shell grammar, default: a single quote)")
    ap.add_argument("--assert-no-apostrophe", action="store_true",
                    help="fail if the extracted body contains an apostrophe")
    ap.add_argument("--run", action="store_true", help="execute the body (default: print it)")
    # allow-bare-bash: argparse choices list, not a subprocess argv -- no argv[0] resolution happens here
    ap.add_argument("--interpreter", choices=["bash", "python"], default=None,
                    help="default: bash for check grammar, python for shell grammar")
    ap.add_argument("--stdin-json", help="JSON string piped to the body on stdin when --run")
    ap.add_argument("--env", action="append", help="KEY=VALUE passed to the body (repeatable)")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    path = args.file or (DEFAULT_CHECK_FILE if args.grammar == "check" else None)
    if not path:
        die("--file is required for the shell grammar")
    text = read_host(path, args.source)

    if args.list:
        names = list_checks(text)
        if args.json:
            print(json.dumps({"file": path, "source": args.source, "checks": names}))
        else:
            for c in names:
                print("%6d  %s" % (c["line"], c["name"]))
        return 0

    if args.grammar == "check":
        if not args.name:
            die("--name is required for the check grammar (or use --list)")
        src, line, cont, prose = extract_check(text, args.name)
        label = args.name
    else:
        prose = ""
        if not args.open_marker:
            die("--open-marker is required for the shell grammar")
        src, line, cont = extract_shell(text, args.open_marker, args.close_line)
        label = args.open_marker

    interpreter = args.interpreter or ("bash" if args.grammar == "check" else "python")

    result = {"file": path, "source": args.source, "grammar": args.grammar,
              "name": label, "start_line": line, "continuation_lines": cont,
              "chars": len(src), "interpreter": interpreter,
              # Never silent: what was stripped is reported, so a wrong strip is
              # visible in the payload rather than showing up as a syntax error.
              "trailing_prose": prose}

    if not src.strip():
        result.update(verdict="ERROR", reason="empty capture -- extraction produced no body")
        print(json.dumps(result) if args.json else "ERROR: empty capture for %s" % label)
        return 2

    if args.assert_no_apostrophe:
        offenders = apostrophe_offenders(src)
        result["apostrophe_offenders"] = offenders
        if offenders:
            result.update(verdict="FAIL",
                          reason=("apostrophe on line(s) %s of the extracted block -- inside a "
                                  "bash-single-quoted block this terminates the quote"
                                  % offenders[:6]))
            print(json.dumps(result) if args.json else "FAIL: %s" % result["reason"])
            return 1

    if not args.run:
        if args.json:
            result["body"] = src
            print(json.dumps(result))
        else:
            sys.stdout.write(src if src.endswith("\n") else src + "\n")
        return 0

    try:
        rc, out, err = run_body(src, interpreter, args.stdin_json, args.env, args.timeout)
    except subprocess.TimeoutExpired:
        result.update(verdict="ERROR", reason="body timed out after %ds" % args.timeout)
        print(json.dumps(result) if args.json else "ERROR: %s" % result["reason"])
        return 2

    verdict, reason = classify(rc, out, err)
    result.update(verdict=verdict, reason=reason, rc=rc,
                  stdout=out.strip()[:4000], stderr=err.strip()[:2000])
    if args.json:
        print(json.dumps(result))
    else:
        print("%s [%s] %s" % (verdict, label, reason))
        if out.strip():
            print(out.rstrip())
        if verdict == "ERROR" and err.strip():
            print("--- stderr ---", file=sys.stderr)
            print(err.rstrip()[:2000], file=sys.stderr)
    return {"PASS": 0, "FAIL": 1, "ERROR": 2, "INDETERMINATE": 3}[verdict]


if __name__ == "__main__":
    sys.exit(main())
