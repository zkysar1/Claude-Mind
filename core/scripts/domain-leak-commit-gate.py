#!/usr/bin/env python3
"""domain-leak-commit-gate.py — refuse a commit that ADDS a domain term to a framework file.

Gate M3 of core/githooks/commit-msg. The BLOCKING half of the domain border
wall; core/scripts/domain-leak-check.sh stays the report-only whole-tree audit.

WHY THIS LIVES IN commit-msg AND NOT IN pre-commit's Gate 6 (g-115-10048).
The goal's outcome 1 names "pre-commit Gate 6", and its own outcome 2 requires
the bypass to be a commit-message trailer. Those cannot both be satisfied:
pre-commit runs BEFORE the message exists, so a blocking gate there has no
reachable override and the first legitimate domain term would wedge the repo
for every agent on the shared tree. commit-msg is the earliest hook that sees
BOTH the staged content and the message — git exports GIT_INDEX_FILE for a
pathspec commit, so `git diff --cached` here is exactly what the commit will
contain (pinned by test_hot_path_size_gate.py::test_pathspec_commit_is_seen_as_committed).
So the BLOCK moved to M3 and pre-commit's Gate 6 is deliberately left advisory.

ADDED LINES ONLY (guard-1426). The tree already violates the blocklist at scale
-- the measured backlog is 238 files for one term alone -- so a whole-file check
would fail every subsequent commit for every agent. Only lines this commit ADDS
are gated; an untouched pre-existing line can never block. The whole-tree
backlog stays with domain-leak-check.sh and its tracking goals.

THE MARKER PREDICATE IS ANCHORED HERE, AND THAT IS A DELIBERATE DIVERGENCE
(guard-6989 / g-115-10246). domain-leak-check.sh:193 honours the exemption with
an unanchored substring test, so a file that merely MENTIONS the token in prose
exempts itself from the entire wall. Inherited into a BLOCKING gate that is not
a quirk, it is the bypass: any commit could clear this gate by adding one line
of prose naming the marker. This gate therefore requires the token to OPEN A
COMMENT, which is the construct that actually claims the exemption per
.claude/rules/domain-free-examples.md § Marker Placement.

The divergence is MEASURED to be a no-op on the current tree, not assumed
(guard-6989 action_hint 1). Census over the real scan scope 2026-09-18 (alpha,
cc-07): 3,394 files walked, 118 token-carrying, 110 anchored/deliberate, 8
unanchored-only. Of those 8, three are already suppressed by other filters
(domain-leak-check.sh and domain-free-examples.md by the self-reference filter,
test_session_manifest_write_gate.py by the test-file filter); the remaining five
-- learning-routing.md, domain-recipe-seed-purity.md, retrieve.py,
rule-vs-convention-gate.py, marker-placement-gate.py -- carry ZERO blocklist
terms between them, verified with a planted positive control proving the matcher
discriminates (guard-2421). So no file's verdict changes today. When g-115-10246
anchors the scanner's own predicate the two converge and this note can shrink.

FAIL-OPEN BY CONTRACT. An unreadable blocklist, an unparseable diff, a missing
world dir or any unexpected exception ALLOWS the commit with a WARN. A commit
hook that refuses on its own plumbing fault wedges the shared tree, which is
strictly worse than the leak it is trying to stop (guard-1562).

Usage:
  (hook)  --commit-msg-file <path>   exit 1 = refuse, 0 = allow
  (cli)   --check                    report added-line violations, exit 1 if any
  (cli)   --staged-report            same as --check, always exit 0
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

GATE_ID = "domain-leak-commit-gate"
TAG = "[domain-leak-commit-gate]"
DEFAULT_TRAILER = "domain-leak-override:"
# A leak override should name the term and why it must stay. 8 (the hot-path
# gate's floor) admits "because"; 12 does not.
MIN_JUSTIFICATION = 12

# --- scope: MIRRORS core/scripts/domain-leak-check.sh SCAN_DIRS + extensions ---
# Kept honest by test_domain_leak_commit_gate.py::test_scope_matches_scanner,
# which parses the bash rather than trusting this copy (the ALLOWLIST <->
# marker-placement-gate.py pairing in that script is the same pattern).
SCAN_DIRS = (
    "core/config", "core/scripts", ".claude/rules",
    ".claude/skills", "mind_api/src", "mind_api/tests",
)
EXTS = (".md", ".yaml", ".yml", ".sh", ".py", ".txt")

# Self-referential files: they name the terms because they define or document
# the wall. domain-leak-check.sh filters these by substring; same set here.
SELF_REF = (
    "core/config/domain-term-blocklist.txt",
    "core/scripts/domain-leak-check.sh",
    ".claude/rules/domain-free-examples.md",
    ".claude/skills/verify-learning/SKILL.md",
)
TEST_FILE_RX = re.compile(r"(^|/)test[-_][A-Za-z0-9_-]+\.(sh|py)$")
MARKER_ANCHORED_RX = re.compile(r"^[ \t]*(#|<!--|//|--|\*)[ \t]*domain-leak-exempt:")
FORGED_FM_RX = re.compile(r"^forged:[ \t]*true[ \t]*$", re.M)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def in_merge(repo: Path) -> bool:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                       capture_output=True, text=True)
    return r.returncode == 0


def load_terms(repo: Path):
    """Terms from the SHARED blocklist file — the one source of truth this gate
    and the scanner agree on. Case-sensitive, exactly as the scanner's default;
    --ignore-case is report scope and must not reach a blocking path."""
    p = repo / "core" / "config" / "domain-term-blocklist.txt"
    terms = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        t = line.strip()
        if t and not t.startswith("#"):
            terms.append(t)
    return terms


def forged_skill_names(repo: Path):
    """Forged skills are exempt per domain-free-examples.md § Scope. Absent or
    unreadable registry => empty set (scan everything), matching the scanner."""
    try:
        from _paths import WORLD_DIR  # type: ignore
        if not WORLD_DIR:
            return set()
        f = Path(WORLD_DIR) / "forged-skills.yaml"
        if not f.is_file():
            return set()
        names, inside = set(), False
        for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.rstrip("\r")
            if line.startswith("skills:"):
                inside = True
                continue
            if inside:
                if line and not line.startswith(" "):
                    break
                m = re.match(r"^  ([a-z][A-Za-z0-9_-]*):[ \t]*$", line)
                if m:
                    names.add(m.group(1))
        return names
    except Exception:
        return set()


def in_scope(rel: str) -> bool:
    return rel.endswith(EXTS) and any(rel == d or rel.startswith(d + "/") for d in SCAN_DIRS)


def is_exempt(repo: Path, rel: str, forged: set) -> bool:
    if rel in SELF_REF:
        return True
    if TEST_FILE_RX.search(rel):
        return True
    for name in forged:
        if rel.startswith(".claude/skills/" + name + "/"):
            return True
    try:
        txt = (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if FORGED_FM_RX.search(txt):
        return True
    return any(MARKER_ANCHORED_RX.match(l) for l in txt.splitlines())


def added_lines(repo: Path):
    """{relpath: [(new_line_no, text), ...]} for lines this commit ADDS.

    -U0 so no context lines are emitted. `+++ b/<path>` sets the file, `@@`
    resets the new-side counter, `+` lines are additions; `-` lines never
    advance the new-side counter, which is why deletions cannot shift the
    reported line numbers."""
    out = _git(repo, "diff", "--cached", "-U0", "--diff-filter=ACM")
    res, path, newno = {}, None, 0
    for raw in out.splitlines():
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            path = None if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
            continue
        if raw.startswith("@@"):
            m = re.match(r"@@ -\S+ \+(\d+)(?:,\d+)? @@", raw)
            newno = int(m.group(1)) if m else 0
            continue
        if path and raw.startswith("+") and not raw.startswith("+++"):
            res.setdefault(path, []).append((newno, raw[1:]))
            newno += 1
    return res


def evaluate(repo: Path):
    """[{path, line, term, text}] — one entry per (added line, matched term)."""
    terms = load_terms(repo)
    if not terms:
        return []
    forged = forged_skill_names(repo)
    rx = {t: re.compile(r"\b" + re.escape(t) + r"\b") for t in terms}
    violations = []
    for rel, lines in sorted(added_lines(repo).items()):
        if not in_scope(rel) or is_exempt(repo, rel, forged):
            continue
        for no, text in lines:
            for t in terms:
                if rx[t].search(text):
                    violations.append({"path": rel, "line": no, "term": t,
                                       "text": text.strip()[:160]})
    return violations


def parse_override(message: str, trailer: str = DEFAULT_TRAILER):
    """(justification | None, note). Comment lines are ignored — the hook reads
    the raw message file, before git's cleanup strips them. The match is
    single-line by construction, which is also what the audit record stores
    (g-115-8218): write a ONE-LINE reason or the rest is silently dropped."""
    rx = re.compile(r"^\s*" + re.escape(trailer) + r"\s*(.*?)\s*$", re.IGNORECASE)
    for line in message.splitlines():
        if line.startswith("#"):
            continue
        m = rx.match(line)
        if not m:
            continue
        just = m.group(1)
        if len(just) < MIN_JUSTIFICATION:
            return None, ("trailer found but the justification is too short "
                          "(%d chars; need >= %d)" % (len(just), MIN_JUSTIFICATION))
        return just, ""
    return None, ""


def _ledger_path():
    from _paths import WORLD_DIR  # type: ignore
    if not WORLD_DIR:
        raise RuntimeError("WORLD_DIR unresolved")
    return Path(WORLD_DIR) / "override-bypass-ledger.jsonl"


def write_ledger(repo: Path, violations, justification: str, message: str) -> str:
    """Append the override record through the shared locked appender. Returns ""
    on success, else a WARN reason. Never raises: an audit-write failure must
    not wedge the commit."""
    try:
        from _fileops import locked_append_jsonl  # type: ignore
        subject = next((ln.strip() for ln in message.splitlines()
                        if ln.strip() and not ln.startswith("#")), "")
        try:
            head = _git(repo, "rev-parse", "--short", "HEAD").strip()
        except Exception:
            head = None
        record = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "gate": GATE_ID,
            "override_token": hashlib.sha1(
                justification.encode("utf-8", errors="replace")).hexdigest()[:12],
            "justification": justification[:1000],
            "agent": os.environ.get("MIND_AGENT") or None,
            "session_id": os.environ.get("MIND_SID") or None,
            "context": {
                "caller": "core/githooks/commit-msg",
                "head_before": head,
                "commit_subject": subject[:200],
                "terms": sorted({v["term"] for v in violations}),
                "sites": [f"{v['path']}:{v['line']}:{v['term']}" for v in violations[:50]],
                "site_count": len(violations),
            },
        }
        locked_append_jsonl(_ledger_path(), record)
        return ""
    except Exception as e:
        return "audit append failed: %s" % e


def refusal_text(violations, trailer: str, note: str) -> str:
    lines = [
        TAG + " REFUSED — this commit ADDS domain term(s) to framework files.",
        "",
        "  Added lines carrying a blocklisted term (file:line:term):",
    ]
    for v in violations[:25]:
        lines.append("    %s:%d:%s" % (v["path"], v["line"], v["term"]))
        lines.append("        + " + v["text"])
    if len(violations) > 25:
        lines.append("    ... and %d more" % (len(violations) - 25))
    lines += [
        "",
        "  Only lines THIS COMMIT ADDS are gated; pre-existing lines never block",
        "  (guard-1426). The whole-tree backlog is domain-leak-check.sh's report.",
        "",
        "  Fix — pick one:",
        "    1. Genericize the term ('service', 'remote storage', 'the framework')",
        "       per .claude/rules/domain-free-examples.md.",
        "    2. Move the content to world/conventions/ (the sanctioned home for",
        "       domain-specific operational rules).",
        "    3. If the term is FUNCTIONAL here (a regex, a sentinel, a fixture),",
        "       opt the file out with the marker OPENING A COMMENT, e.g.",
        "         # domain-leak-exempt: <why this term is functional>",
        "       A bare prose mention of the marker does NOT exempt this gate.",
        "",
        "  Override (audited) — add a ONE-LINE trailer to the commit message:",
        "    " + trailer + " <why this term must live in a framework file>",
        "  (>= %d chars; only the first line is stored)" % MIN_JUSTIFICATION,
    ]
    if note:
        lines += ["", "  NOTE: " + note]
    return "\n".join(lines)


def run_gate(repo: Path, msg_file, out=sys.stdout) -> int:
    if in_merge(repo):
        print(TAG + " merge commit — not gated (merges combine already-gated commits)", file=out)
        return 0
    try:
        violations = evaluate(repo)
    except Exception as e:
        print(TAG + " WARN: could not evaluate staged additions (%s) — allowing commit" % e, file=out)
        return 0
    if not violations:
        return 0
    message = ""
    if msg_file:
        try:
            message = Path(msg_file).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(TAG + " WARN: cannot read commit message (%s)" % e, file=out)
    justification, note = parse_override(message)
    if justification:
        warn = write_ledger(repo, violations, justification, message)
        terms = ", ".join(sorted({v["term"] for v in violations}))
        print(TAG + " OVERRIDE accepted — %d added-line site(s) [%s] — recorded to the "
                    "override-bypass audit%s"
              % (len(violations), terms, "" if not warn else " (WARN: " + warn + ")"), file=out)
        return 0
    print(refusal_text(violations, DEFAULT_TRAILER, note), file=out)
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", help="repo root (default: git toplevel of cwd)")
    ap.add_argument("--commit-msg-file", help="commit-msg hook shape: path to the message file")
    ap.add_argument("--check", action="store_true", help="report added-line violations; exit 1 if any")
    ap.add_argument("--staged-report", action="store_true", help="like --check but always exit 0")
    args = ap.parse_args(argv)

    if args.repo:
        repo = Path(args.repo).resolve()
    else:
        try:
            repo = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        except Exception as e:
            print(TAG + " WARN: not in a git work tree (%s) — nothing to gate" % e)
            return 0

    if args.check or args.staged_report:
        try:
            violations = evaluate(repo)
        except Exception as e:
            print(TAG + " WARN: could not evaluate (%s)" % e)
            return 0
        if not violations:
            print(TAG + " CLEAN: no blocklisted term on any added line.")
            return 0
        print(refusal_text(violations, DEFAULT_TRAILER, ""))
        return 0 if args.staged_report else 1

    return run_gate(repo, args.commit_msg_file)


if __name__ == "__main__":
    sys.exit(main())
