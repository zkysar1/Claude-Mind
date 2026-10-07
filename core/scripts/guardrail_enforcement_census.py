#!/usr/bin/env python3
"""Census of standing owner/user directives held as guardrails: which name an
enforcing gate, which are honor-system. (g-306-572)

WHY THIS EXISTS. A guardrail that says "never do X" is RETRIEVED, not enforced:
whether the next action honours it depends on a reader noticing it. When the owner
corrects the agent, the correction should become something the action is CHECKED
against. This makes the gap countable: for every guardrail that carries a user or
owner directive it prints the gate named in the optional `enforced_by` field (see
reasoning-guardrails.md) or the word `honor-system`.

This is a pure transformer: the wrapper (guardrail-enforcement-census.sh) reads the
store through guardrails-read.sh and hands this script the JSON, so it runs and is
tested without the daemon. It never writes.

POPULATION -- three signals, ORed; every row prints which of them matched:
  tag     the record carries a tag that names an owner or user act (USER_TAGS)
  source  `source` pairs user/owner with a speech-act word ("user directive",
          "owner ruling", "user-flagged", ...), not the bare word "user" in prose
  head    the rule OPENS "STANDING" and names an owner, user, grant or directive
          within its first 40 characters
"STANDING in the first 80 characters" alone is NOT used: measured 2026-10-07 it
matched 25 active records and only 4 of them open a standing directive or grant;
the rest use the word as prose ("evidentiary standing", "a standing no-op").
Tags that name a rule ABOUT directives or grants (directive, standing-grants,
standing-directive, directive-lane) are NOT signals: sampled, none of those records
was itself an owner's directive. The boundary is countable: the summary reports how
many records mention user/owner in `source` and matched no signal.

VERDICTS (what the `enforced_by` field says; a gate is only ever COUNTED, never run):
  gated             a named gate exists as a path in this repo
  gated-unverified  a name that is not a repo path (a hook name) -- cannot be checked
  stale-gate        every named gate is a repo path that does not exist -- NOT enforced
  malformed         the field has the wrong type (the validators refuse it, so this
                    is a legacy or hand-edited record) -- NOT enforced
  honor-system      no gate named: absent, null, "", [], or the census's own word
NOT ENFORCED = honor-system + stale-gate + malformed.

CITED-BY HINT. A gate that enforces a rule often cites its id in a comment. Each row
also lists the non-test files under SCAN_ROOTS that cite the guardrail id, and
whether one of them is gate-like by name. That is a HINT for backfilling
`enforced_by`, never a verdict: a citation proves a file mentions the rule, not that
it checks it. It exists so the headline number does not overstate the gap.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

USER_TAGS = frozenset({
    "user-directive", "owner-directive", "user-correction",
    "user-ruling", "user-grant", "user-policy", "owner-decision"})
# Every word below occurs after user/owner in at least one active `source`
# (measured 2026-10-07); words with no occurrence (instruction, request, said, ...)
# are left out until a record uses them.
_SPEECH_ACTS = (
    "directive", "correction", "feedback", "ruling", "grant(?:ed)?", "asked",
    "report(?:ed)?", "flagged", "caught", "directed", "supplied", "observed",
    "reply", "explicitly")
_SOURCE_RE = re.compile(
    r"\b(?:user|owner)[ _-](?:%s)s?\b" % "|".join(_SPEECH_ACTS), re.I)
_USER_WORD_RE = re.compile(r"\b(?:user|owner)\b", re.I)
_HEAD_RE = re.compile(r"^STANDING\b[^.]{0,40}\b(?:OWNER|USER|GRANT|DIRECTIVE)\b")
# The census prints rule excerpts, so anything address- or account-id-shaped is masked
# in the excerpt: a printed census must never become a copy of a value that is
# masked everywhere else.
_ADDR_RE = re.compile(r"[^\s<>\"']+@[^\s<>\"']+")
_ACCT_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
# The census prints this word for an unguarded rule, so an author may write it into
# the field; it must never count as a gate.
_NO_GATE_TOKENS = frozenset({"honor-system", "honour-system", "none"})
_GATE_LIKE_RE = re.compile(r"gate|hook|check|enforce|validat", re.I)
_GUARD_ID_RE = re.compile(r"^guard-(\d+)$")
_CITE_RE = re.compile(r"\bguard-(\d+)\b")

SCAN_ROOTS = ("core/scripts", "mind_api/src")
_SCAN_SUFFIXES = (".py", ".sh", ".json", ".yaml", ".yml")
_SKIP_DIRS = frozenset({"tests", "__pycache__"})

NOT_ENFORCED = ("honor-system", "stale-gate", "malformed")


def _text(rec, key):
    """A record field as text; a legacy record with a non-string value reads as empty
    rather than crashing the whole census."""
    value = rec.get(key)
    return value if isinstance(value, str) else ""


def population_signals(rec):
    """Which of the three population signals this record matches (possibly none)."""
    signals = []
    tags = rec.get("tags")
    if isinstance(tags, list) and USER_TAGS & {t for t in tags if isinstance(t, str)}:
        signals.append("tag")
    if _SOURCE_RE.search(_text(rec, "source")):
        signals.append("source")
    if _HEAD_RE.search(_text(rec, "rule")[:80]):
        signals.append("head")
    return signals


def parse_enforced_by(value):
    """-> (gates, problem). `gates` are the cleaned names; `problem` is None or why
    the value is unusable. Cleared shapes (None, "", []) are not a problem."""
    if value is None:
        return [], None
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list) and all(isinstance(x, str) for x in value):
        items = value
    else:
        return [], "enforced_by has type %s" % type(value).__name__
    gates = []
    for item in items:
        name = item.strip()
        if name and name.lower() not in _NO_GATE_TOKENS:
            gates.append(name)
    return gates, None


def gate_presence(gate, repo_root):
    """'present' | 'missing' | 'unverifiable' for one named gate.

    Only a repo-relative path (a token containing '/', with any :line, ::name or
    #anchor suffix dropped) can be checked. Anything else -- a hook name, a bare file
    name, an absolute path, a path that climbs out of the repo -- is 'unverifiable',
    never 'present' and never 'missing': the census must not probe outside the repo
    or guess where a bare name lives."""
    token = re.split(r"[:#]", gate.split()[0], 1)[0]
    if token.startswith("/") or "/" not in token:
        return "unverifiable"
    root = os.path.realpath(repo_root)
    full = os.path.realpath(os.path.join(root, token))
    if os.path.commonpath([root, full]) != root:
        return "unverifiable"
    return "present" if os.path.exists(full) else "missing"


def is_gate_like(relpath):
    return "/gates/" in relpath or bool(_GATE_LIKE_RE.search(os.path.basename(relpath)))


def collect_citations(repo_root, roots=SCAN_ROOTS):
    """{guard number: sorted repo-relative paths citing it}, tests excluded (a test
    that cites a rule enforces nothing). Keyed by int so guard-007 and guard-7 meet."""
    cites = defaultdict(set)
    for root in roots:
        top = os.path.join(repo_root, root)
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                if not fn.endswith(_SCAN_SUFFIXES):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
                for m in _CITE_RE.finditer(text):
                    cites[int(m.group(1))].add(rel)
    return {k: sorted(v) for k, v in cites.items()}


def _mask(text):
    return _ACCT_RE.sub("<12d>", _ADDR_RE.sub("<addr>", text))


def classify(rec, repo_root, citations):
    gates, problem = parse_enforced_by(rec.get("enforced_by"))
    named = [{"name": _mask(g), "presence": gate_presence(g, repo_root)} for g in gates]
    if problem:
        verdict = "malformed"
    elif not named:
        verdict = "honor-system"
    elif any(g["presence"] == "present" for g in named):
        verdict = "gated"
    elif any(g["presence"] == "unverifiable" for g in named):
        verdict = "gated-unverified"
    else:
        verdict = "stale-gate"
    m = _GUARD_ID_RE.match(_text(rec, "id"))
    cited_in = citations.get(int(m.group(1)), []) if m else []
    return {
        "id": _text(rec, "id"),
        "verdict": verdict,
        "signals": population_signals(rec),
        "gates": named,
        "problem": problem,
        "cited_in": cited_in[:5],
        "cited_count": len(cited_in),
        "cited_by_gate_like": any(is_gate_like(p) for p in cited_in),
        "category": rec.get("category"),
        "rule_head": _mask(_text(rec, "rule").replace("\n", " "))[:90],
    }


def census(records, repo_root, citations=None):
    """-> (rows for the population, summary). Pure: no I/O when `citations` is given."""
    if citations is None:
        citations = collect_citations(repo_root)
    rows = []
    unmatched = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if population_signals(rec):
            rows.append(classify(rec, repo_root, citations))
        elif _USER_WORD_RE.search(_text(rec, "source")):
            unmatched += 1
    verdicts = Counter(r["verdict"] for r in rows)
    not_enforced = [r for r in rows if r["verdict"] in NOT_ENFORCED]
    summary = {
        "active_guardrails_read": len(records),
        "population": len(rows),
        "source_mentions_user_unmatched": unmatched,
        "by_signal": {s: sum(1 for r in rows if s in r["signals"]) for s in ("tag", "source", "head")},
        "by_verdict": {v: verdicts.get(v, 0) for v in
                       ("gated", "gated-unverified", "stale-gate", "malformed", "honor-system")},
        "not_enforced": len(not_enforced),
        "not_enforced_cited_by_gate_like": sum(1 for r in not_enforced if r["cited_by_gate_like"]),
        "not_enforced_uncited": sum(1 for r in not_enforced if r["cited_count"] == 0),
    }
    return rows, summary


def _id_key(row):
    m = _GUARD_ID_RE.match(row["id"])
    return int(m.group(1)) if m else 0


def render_text(rows, summary):
    s = summary
    lines = [
        "guardrail enforcement census (g-306-572)",
        "  active guardrails read : %d" % s["active_guardrails_read"],
        "  population             : %d  (tag %d, source %d, head %d; a row may match several)" % (
            s["population"], s["by_signal"]["tag"], s["by_signal"]["source"], s["by_signal"]["head"]),
        "  outside the population : %d more mention user/owner in `source` with no speech-act phrase (not counted)" %
        s["source_mentions_user_unmatched"],
        "  verdicts               : " + ", ".join("%s %d" % (k, v) for k, v in s["by_verdict"].items()),
        "  NOT ENFORCED           : %d  (honor-system + stale-gate + malformed)" % s["not_enforced"],
        "    of which cited by a gate-like file (candidates to backfill enforced_by): %d" %
        s["not_enforced_cited_by_gate_like"],
        "    of which cited by no non-test file under %s: %d" % (", ".join(SCAN_ROOTS), s["not_enforced_uncited"]),
        "",
        "%-11s %-16s %-17s %s" % ("id", "verdict", "signals", "enforcing gate | cited-by hint | rule head"),
    ]
    for r in sorted(rows, key=lambda r: (r["verdict"], _id_key(r))):
        if r["gates"]:
            gate = ", ".join("%s [%s]" % (g["name"], g["presence"]) for g in r["gates"])
        elif r["problem"]:
            gate = "(%s)" % r["problem"]
        else:
            gate = "honor-system"
            if r["cited_count"]:
                gate += " (cited in %d file%s%s)" % (
                    r["cited_count"], "" if r["cited_count"] == 1 else "s",
                    ", incl. a gate-like one" if r["cited_by_gate_like"] else "")
        lines.append("%-11s %-16s %-17s %s | %s" % (
            r["id"], r["verdict"], "+".join(r["signals"]), gate, r["rule_head"]))
    return "\n".join(lines)


def load_records(path):
    """Read the guardrails-read.sh --active JSON, refusing any shape that would let an
    empty or mis-shaped read pass as a clean census (guard-2298: print the shape)."""
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise SystemExit("[census] %s is not JSON (%d bytes): %s" % (path, len(raw), e))
    if not isinstance(data, list):
        keys = sorted(data) if isinstance(data, dict) else "-"
        raise SystemExit("[census] expected a bare JSON list of guardrails, got %s (top-level keys: %s, %d bytes)" % (
            type(data).__name__, keys, len(raw)))
    if not data:
        raise SystemExit("[census] 0 active guardrails read (%d bytes): refusing -- an empty read is not a clean census" % len(raw))
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--guardrails-json-file", required=True,
                    help="output of guardrails-read.sh --active")
    ap.add_argument("--repo-root", default=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    ap.add_argument("--json", action="store_true", help="emit {summary, rows} as JSON")
    ap.add_argument("--honor-system-only", action="store_true",
                    help="list only NOT ENFORCED rows (the summary still covers the whole population)")
    args = ap.parse_args(argv)

    records = load_records(args.guardrails_json_file)
    rows, summary = census(records, args.repo_root)
    shown = [r for r in rows if r["verdict"] in NOT_ENFORCED] if args.honor_system_only else rows
    if args.json:
        print(json.dumps({"summary": summary, "rows": shown}, indent=2, ensure_ascii=False))
    else:
        print(render_text(shown, summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
