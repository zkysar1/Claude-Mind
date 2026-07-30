#!/usr/bin/env python3
"""Classify governed-store write call sites in skill pseudocode as VERIFIED or
UNVERIFIED, per hypothesis 2026-07-26_unchecked-writes-are-the-norm.

A write call site is VERIFIED when, within the following 6 lines, the pseudocode
either branches on the wrapper's exit code / rc, or re-reads the same store to
confirm the value landed. Everything else is UNVERIFIED.

WHY THIS IS SCRIPTED, NOT HAND-AUDITED: the population is in the hundreds, and a
hand audit would not be reproducible. Both properties come straight from the
hypothesis's resolution_method.

POPULATION DERIVATION (the part most worth checking before believing any
fraction -- rb-245). A "governed-store write wrapper" is NOT a hand-maintained
name list, which would silently rot as wrappers are added. It is derived: a
wrapper under core/scripts/ is a WRITE wrapper iff its source issues a mutating
daemon call (`rt_call POST|PUT|PATCH|DELETE`). Read wrappers issue `rt_call GET`.
That discriminator is a property of the wrapper contract, so a newly-added
wrapper joins the population automatically.

Call-site detection excludes two things the raw grep counts and the method does
not want: pure-comment lines, and prose mentions ("see wm-set.sh for the API").
A mention is not a call site; counting it would inflate the denominator with
lines that could not possibly check an exit code.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "core" / "scripts"
SKILLS_GLOB = ".claude/skills/*/SKILL.md"

LOOKAHEAD = 6  # lines, per resolution_method

MUTATING = re.compile(r"rt_call\s+(POST|PUT|PATCH|DELETE)\b")
READING = re.compile(r"rt_call\s+GET\b")

# An invocation, as opposed to a prose mention. Skill pseudocode invokes a
# wrapper in a small number of recognisable shapes; anything else that merely
# contains the name is a reference.
INVOKE_SHAPES = (
    # "Bash: x.sh" / "Bash (name): x.sh" -- and critically the piped form
    # `echo 'null' | Bash: wm-set.sh loop_state`, which is THE canonical wm-set /
    # wm-append idiom. An earlier version anchored this to line start and missed
    # every piped write, a systematic false negative concentrated in exactly the
    # store whose writes are most often one-liners.
    re.compile(r"Bash\s*(?:\([^)]*\))?\s*[:(].*%(name)s"),
    re.compile(r"\bbash\s+\S*%(name)s"),          # "bash core/scripts/x.sh"
    re.compile(r"\bpy\s+-3\s+\S*%(name)s"),       # "py -3 core/scripts/x.py"
    re.compile(r"\bpython3?\s+\S*%(name)s"),      # "python3 core/scripts/x.py"
    re.compile(r"\|\s*(?:bash\s+\S*)?%(name)s"),  # "... | wm-set.sh slot"
    re.compile(r"\$\(\s*(?:bash\s+\S*)?%(name)s"),  # "$(x.sh ...)"
    re.compile(r"^\s*%(name)s\s"),                # bare leading invocation
)

# Evidence that the pseudocode checked the write. Deliberately GENEROUS: this
# audit's claim is that verification is RARE, so every borderline call is scored
# in the direction that would falsify the hypothesis. An over-generous matcher
# that still yields a low fraction is strong evidence; a stingy one proves
# nothing (guard-1470 -- an assertion tuned to pass its own thesis is hollow).
RC_PATTERNS = [
    re.compile(r"exit\s*code", re.I),
    re.compile(r"\bexit_code\b"),
    re.compile(r"\brc\s*(?:=|!=|==|>|<|\bin\b)"),
    re.compile(r"\$\?"),
    re.compile(r"\breturncode\b"),
    re.compile(r"\bnon-?zero\b", re.I),
    re.compile(r"\bexits?\s+(?:0|1|non)", re.I),
    re.compile(r"\|\|\s*\S"),          # "cmd || fallback"
    re.compile(r"&&\s*\S"),            # "cmd && next"
    re.compile(r"\bIF\s+.*(?:fail|error|refus)", re.I),
    re.compile(r"\bset\s+-e\b"),
]
REREAD_HINTS = [
    re.compile(r"\bread[- ]back\b", re.I),
    re.compile(r"\bre-?read\b", re.I),
    re.compile(r"\bconfirm\b", re.I),
    re.compile(r"\bverify\b", re.I),
    re.compile(r"\bassert\b", re.I),
]


def discover_wrappers():
    """-> (write_names, read_names, store_prefixes)."""
    write, read = set(), set()
    for p in sorted(SCRIPTS_DIR.glob("*.sh")):
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if MUTATING.search(src):
            write.add(p.name)
        elif READING.search(src):
            read.add(p.name)
    return write, read


def store_prefix(name: str) -> str:
    """Map a wrapper basename to its store key.

    'aspirations-update-goal.sh' -> 'aspirations';  'team-state-update.sh' ->
    'team-state'.  Two-token stores are recognised explicitly because a bare
    tokens[0] split would map team-state and team-* to the same key.
    """
    stem = name.rsplit(".", 1)[0]
    for two in ("team-state", "skill-quality", "pending-questions", "background-jobs"):
        if stem.startswith(two):
            return two
    return stem.split("-", 1)[0]


def is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


# A markdown table row that merely NAMES a wrapper in a cell is documentation,
# not a call site -- counting it inflates the denominator with a line that could
# not check an exit code even in principle. Keyed on 3+ cell delimiters AND the
# absence of an invocation token, because a LEADING single pipe is usually a
# genuine continuation of a piped command:
#     echo '{...}' \
#       | bash core/scripts/evolution-log-append.sh
TABLE_ROW = re.compile(r"^\s*\|.*\|.*\|")


def is_table_row(line: str) -> bool:
    return bool(TABLE_ROW.match(line)) and not re.search(r"\b(?:bash|py|python3?)\s", line)


def invokes(line: str, name: str) -> bool:
    esc = re.escape(name)
    for shape in INVOKE_SHAPES:
        pat = shape.pattern
        if "%(name)s" in pat:
            if re.search(pat % {"name": esc}, line):
                return True
        elif shape.search(line) and name in line:
            return True
    return False


def classify(path: Path, lines, write_names, read_names):
    """Yield one record per write call site in `path`."""
    read_by_prefix = {}
    for r in read_names:
        read_by_prefix.setdefault(store_prefix(r), set()).add(r)

    for i, line in enumerate(lines):
        if is_comment(line) or is_table_row(line):
            continue
        # sorted(), not the raw set: `break` below keeps only the FIRST match,
        # and Python randomises string hashing per process, so set order would
        # make the attributed wrapper vary run to run. Currently inert (exactly
        # one line in the corpus matches two distinct wrappers, and both map to
        # the same store, so no count moves) -- pinned anyway, because a ratchet
        # consuming this output turns a cosmetic flip into phantom drift.
        for name in sorted(write_names):
            if name not in line or not invokes(line, name):
                continue
            # The call LINE itself counts for rc evidence, not just the lines
            # after it: `wm-set.sh slot && echo done` and `x.sh || fallback`
            # branch on the write's exit status inline. Scanning only the
            # lookahead missed every one-line chain -- a false negative that
            # biases the measurement toward the hypothesis it is testing, which
            # is the one direction an audit must not lean.
            look = "\n".join(lines[i + 1 : i + 1 + LOOKAHEAD])
            rc_blob = line + "\n" + look   # rc chains can sit on the call line
            reason = None
            for pat in RC_PATTERNS:
                if pat.search(rc_blob):
                    reason = f"rc:{pat.pattern[:28]}"
                    break
            if reason is None:
                pref = store_prefix(name)
                siblings = read_by_prefix.get(pref, set())
                # Sibling READ wrapper of the SAME store, in the lookahead only.
                # This is the method's "re-reads the same store" clause, and it
                # is structural -- it names a script, not a word.
                if any(s in look for s in siblings):
                    reason = f"reread:{pref}"
            # Prose hints ("confirm", "verify", "assert") are tracked SEPARATELY
            # and never counted in the primary figure. They match the word, not
            # the act: measured, 30 sites were credited by them, and the sample
            # was dominated by the literal string "Verify:" inside goal TITLES
            # and by the skill name "verify-learning". A matcher that reads a
            # goal's title as evidence about a write is measuring nothing.
            # Kept as a deliberately over-generous upper band, because the one
            # direction this audit must not lean is toward its own thesis.
            hint = reason is None and any(h.search(look) for h in REREAD_HINTS)
            yield {
                "file": str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "line": i + 1,
                "wrapper": name,
                "store": store_prefix(name),
                "verified": reason is not None,
                "verified_generous": reason is not None or hint,
                "evidence": reason or ("hint" if hint else None),
                "text": line.strip()[:120],
            }
            break  # one site per line -- do not double-count a piped pair


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit the full record set")
    ap.add_argument("--list-unverified", type=int, default=0,
                    help="print N unverified sites for eyeballing")
    ap.add_argument("--threshold", type=float, default=0.15,
                    help="CONFIRMED if verified_fraction is below this")
    args = ap.parse_args()

    write_names, read_names = discover_wrappers()
    records = []
    for path in sorted(PROJECT_ROOT.glob(SKILLS_GLOB)):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        records.extend(classify(path, lines, write_names, read_names))

    total = len(records)
    verified = sum(1 for r in records if r["verified"])
    verified_gen = sum(1 for r in records if r["verified_generous"])
    frac = (verified / total) if total else 0.0
    frac_gen = (verified_gen / total) if total else 0.0
    verdict = "CONFIRMED" if frac < args.threshold else "CORRECTED"
    verdict_gen = "CONFIRMED" if frac_gen < args.threshold else "CORRECTED"

    # EMPTY POPULATION IS "skipped", NEVER "CONFIRMED" (rb-245).
    # If the wrapper discovery or the skill glob comes back empty -- a moved
    # directory, a renamed layout, a broken checkout -- then total==0, frac
    # computes to 0.0, and 0.0 < threshold, so the audit would report a
    # CONFIDENT CONFIRMED with unverified=0. That is indistinguishable from a
    # codebase with zero unchecked writes, and it is the WORSE direction: a
    # ratchet consuming `unverified` would read 0 as perfect drift-free state
    # and lock the baseline there permanently, since a ratchet only ever
    # shrinks. The sibling experience-orphan ratchet reports `skipped` for
    # exactly this reason; this audit must too, and especially before
    #  wires it as a ratchet.
    if not write_names or total == 0:
        verdict = verdict_gen = "skipped"
    # A result near the threshold is only as trustworthy as its margin. Report
    # how many sites would have to flip, so a reader never has to reverse-engineer
    # the robustness of the verdict from the fraction alone.
    import math
    need = math.ceil(args.threshold * total) if total else 0

    by_store = {}
    for r in records:
        s = by_store.setdefault(r["store"], {"n": 0, "v": 0})
        s["n"] += 1
        s["v"] += int(r["verified"])

    out = {
        "population": {
            "write_wrappers": len(write_names),
            "read_wrappers": len(read_names),
            "skill_files": len(list(PROJECT_ROOT.glob(SKILLS_GLOB))),
            "call_sites": total,
        },
        "verified": verified,
        "unverified": total - verified,
        "verified_fraction": round(frac, 4),
        "threshold": args.threshold,
        "verdict": verdict,
        "margin_sites_to_flip": need - verified,
        "generous_band": {
            "note": "adds prose hints (confirm/verify/assert) as re-read evidence; "
                    "deliberately over-generous upper bound, not the primary figure",
            "verified": verified_gen,
            "verified_fraction": round(frac_gen, 4),
            "verdict": verdict_gen,
        },
        "by_store": {k: {**v, "frac": round(v["v"] / v["n"], 3)}
                     for k, v in sorted(by_store.items(), key=lambda kv: -kv[1]["n"])},
    }
    if args.json:
        out["records"] = records
    print(json.dumps(out, indent=2))

    if args.list_unverified:
        print("\n--- sample UNVERIFIED sites ---", file=sys.stderr)
        for r in [x for x in records if not x["verified"]][: args.list_unverified]:
            print(f"  {r['file']}:{r['line']}  {r['wrapper']}  {r['text']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
