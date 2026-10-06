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

SECOND MODE, `--new-since REV`: WHICH sites joined the unverified set. The ratchet
records a count, so a regression arrives as "+N" with nothing naming the sites, and no
member set is kept for it to name them from. Git already holds every corpus the count
was ever taken over, so the old one is rebuilt from the revision and BOTH corpora go
through the CURRENT matcher, which leaves the corpus as the only variable. REV is a
commit, or `baseline` for the head the ratchet recorded on the newest history row that
read the recorded baseline (the last commit at or before that row when it recorded
none); the other side is the working tree, or `--until REV`. The census JSON carries
`provenance` ({head, dirty}) so the ratchet can record the tree beside each reading.
Design and the alternatives it rejected:
core/config/rationale/unchecked-write-delta-localisation.md
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "core" / "scripts"
SKILLS_GLOB = ".claude/skills/*/SKILL.md"

LOOKAHEAD = 6  # lines, per resolution_method

# The ratchet's entry in meta/audit-baselines.yaml, which `--new-since baseline` reads.
BASELINE_KEY = "unchecked_writes"

# The audit's two inputs: as git pathspecs (where `*` crosses `/`), and as the exact
# shapes the live walk reads. A revision is rebuilt from these, so they must select
# what discover_wrappers() and SKILLS_GLOB read; a test pins that a rebuilt tree
# classifies the same as the checkout it came from.
INPUT_PATHSPECS = ("core/scripts/*.sh", SKILLS_GLOB)
TREE_INPUT = re.compile(r"^(?:core/scripts/[^/]+\.sh|\.claude/skills/[^/]+/SKILL\.md)$")

# A head read back from the baselines file must be a FULL object name before it reaches git:
# a symbolic name (`main`, `HEAD`) moves, and every box writes that file.
FULL_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

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
    # The (?<![A-Za-z0-9_.-]) lookbehind on every %(name)s is LOAD-BEARING, not
    # tidiness (). The \S* and (?:bash\s+\S*)? prefixes below are greedy
    # and unanchored on the LEFT, so without it `bash core/scripts/verified-wm-set.sh`
    # matches name="wm-set.sh" and every wrapper whose name merely ENDS with another
    # script's name is counted as an unchecked call to the wrapped one. Measured over
    # the full 460-site population: 13 sites (2.8%) are this false positive, and
    # wm-set.sh alone is 11 of 49 (22.4%). Two-direction corpus measurement before
    # shipping (guard-6850): 774 files, 120 lines naming wm-set.sh, current detects
    # 81 / lookbehind detects 70, 11 flipped, and ZERO of the 11 are true
    # regressions — every one is a verified-wm-set.sh call. Line 7 ("^\s*") already
    # pins its own left edge and deliberately does NOT carry the lookbehind.
    re.compile(r"Bash\s*(?:\([^)]*\))?\s*[:(].*(?<![A-Za-z0-9_.-])%(name)s"),
    re.compile(r"\bbash\s+\S*(?<![A-Za-z0-9_.-])%(name)s"),          # "bash core/scripts/x.sh"
    re.compile(r"\bpy\s+-3\s+\S*(?<![A-Za-z0-9_.-])%(name)s"),       # "py -3 core/scripts/x.py"
    re.compile(r"\bpython3?\s+\S*(?<![A-Za-z0-9_.-])%(name)s"),      # "python3 core/scripts/x.py"
    re.compile(r"\|\s*(?:bash\s+\S*)?(?<![A-Za-z0-9_.-])%(name)s"),  # "... | wm-set.sh slot"
    re.compile(r"\$\(\s*(?:bash\s+\S*)?(?<![A-Za-z0-9_.-])%(name)s"),  # "$(x.sh ...)"
    re.compile(r"^\s*%(name)s\s"),                # bare leading invocation (left edge already pinned)
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


def discover_wrappers(scripts_dir=None):
    """-> (write_names, read_names, store_prefixes)."""
    write, read = set(), set()
    for p in sorted((SCRIPTS_DIR if scripts_dir is None else scripts_dir).glob("*.sh")):
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


def classify(path: Path, lines, write_names, read_names, root=None):
    """Yield one record per write call site in `path`, filed relative to `root`."""
    root = PROJECT_ROOT if root is None else root
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
                "file": str(path.relative_to(root)).replace("\\", "/"),
                "line": i + 1,
                "wrapper": name,
                "store": store_prefix(name),
                "verified": reason is not None,
                "verified_generous": reason is not None or hint,
                "evidence": reason or ("hint" if hint else None),
                "text": line.strip()[:120],
            }
            break  # one site per line -- do not double-count a piped pair


def collect(root=None):
    """Classify every write call site of the tree at `root` (default: this checkout).

    -> (records, write_names, read_names, skill_files)
    """
    if root is None:
        root, scripts_dir = PROJECT_ROOT, SCRIPTS_DIR
    else:
        scripts_dir = root / "core" / "scripts"
    write_names, read_names = discover_wrappers(scripts_dir)
    skills = sorted(root.glob(SKILLS_GLOB))
    records = []
    for path in skills:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        records.extend(classify(path, lines, write_names, read_names, root))
    return records, write_names, read_names, len(skills)


# --- `--new-since`: which sites joined the unverified set -------------------
# Rationale (WHY a revision diff, with nothing persisted beside the count):
# core/config/rationale/unchecked-write-delta-localisation.md

def _git(*args):
    """Run git in this checkout -> stdout bytes; the RuntimeError carries git's own stderr."""
    proc = subprocess.run(["git", "-C", str(PROJECT_ROOT), *args],
                          capture_output=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError("git %s failed (rc %d): %s" % (
            args[0], proc.returncode,
            proc.stderr.decode("utf-8", "replace").strip()[-300:]))
    return proc.stdout


def _commit(spec):
    """-> the full sha of the commit `spec` names in this checkout."""
    try:
        return _git("rev-parse", "--verify", "--quiet",
                    spec + "^{commit}").decode().strip()
    except RuntimeError:
        raise RuntimeError("%r does not name a commit in this checkout" % spec) from None


def _describe(sha):
    return _git("log", "-1", "--format=%h %cI %s", sha).decode().strip()[:120]


def provenance():
    """-> {"head", "dirty"}: the tree this reading was taken over, for the ratchet to record.

    `head` is HEAD's full sha. `dirty` counts the audited inputs whose working-tree state
    differs from it: modified, staged, deleted AND untracked, because the census walks the
    filesystem, so an untracked wrapper moves the count exactly as an edited one does. A
    reader treats dirty > 0 as "the head only approximates the corpus". Both are None when
    git cannot answer, so a reading is never lost to a checkout that is not a git tree.
    """
    try:
        head = _git("rev-parse", "HEAD").decode().strip()
        # --no-optional-locks: this runs beside live commits, and a status refresh must
        # not hold the index against them. --untracked-files=all: a checkout configured
        # with status.showUntrackedFiles=no would otherwise hide an untracked input, which
        # the census still counts.
        status = _git("--no-optional-locks", "status", "--porcelain", "-z", "--no-renames",
                      "--untracked-files=all", "--", *INPUT_PATHSPECS)
    except (RuntimeError, OSError):
        return {"head": None, "dirty": None}
    names = (e[3:] for e in status.decode("utf-8", "replace").split("\0") if len(e) > 3)
    return {"head": head, "dirty": sum(1 for n in names if TREE_INPUT.match(n))}


def collect_at(rev):
    """collect() over the audit's inputs as they were at `rev`; the checkout is untouched.

    Archive members are matched against TREE_INPUT before anything is written, so an
    entry cannot land outside the scratch directory.
    """
    raw = _git("archive", "--format=tar", rev, "--", *INPUT_PATHSPECS)
    with tempfile.TemporaryDirectory(prefix="unchecked-write-audit-") as tmp:
        root = Path(tmp)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            for member in tar:
                if member.isfile() and TREE_INPUT.match(member.name):
                    target = root / member.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(tar.extractfile(member).read())
        return collect(root)


def baseline_reading(path=None):
    """-> (baseline, recorded_at, seen): the newest retained history row that read AT the baseline.

    `seen` is the {"head", "dirty"} the ratchet recorded in that row's breakdown, None for
    each on a row that predates them. The ratchet keeps the baseline and its history and
    nothing else, in a file that is merge-protected across boxes. Parsed with yaml, never a
    line scan.
    """
    import yaml
    if path is None:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        from _paths import META_DIR
        path = META_DIR / "audit-baselines.yaml"
    entry = (yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}).get(BASELINE_KEY) or {}
    baseline, history = entry.get("baseline"), entry.get("history") or []
    if not isinstance(baseline, int):
        raise RuntimeError("no %s baseline is recorded in %s" % (BASELINE_KEY, path))
    rows = [r for r in history
            if isinstance(r, dict) and r.get("drift_total") == baseline
            and r.get("recorded_at")]
    if not rows:
        raise RuntimeError(
            "none of the %d retained history rows reads the recorded baseline (%d), so its "
            "reading has left the window; name a revision with --new-since REV"
            % (len(history), baseline))
    row = max(rows, key=lambda r: str(r["recorded_at"]))
    breakdown = row.get("breakdown") if isinstance(row.get("breakdown"), dict) else {}
    return (baseline, str(row["recorded_at"]),
            {"head": breakdown.get("head"), "dirty": breakdown.get("dirty")})


def resolve_since(spec, until):
    """-> {"sha", "how", "baseline"}: the commit `--new-since` names.

    `baseline` is the head the ratchet recorded on the newest history row that read the
    recorded baseline, when that row has one and this checkout holds the commit; `until`
    bounds only the date walk below, since a recorded head is an exact commit. Otherwise it
    is the last commit at or before that row. Row stamps are naive UTC (the framework's
    timestamp posture) and are handed to git as UTC. The match by commit date is
    approximate: the caller prints the commit it landed on and how, and
    `--new-since <sha>` overrides it.
    """
    if spec != "baseline":
        return {"sha": _commit(spec), "how": "named", "baseline": None}
    baseline, stamp, seen = baseline_reading()
    head, dirty = seen.get("head"), seen.get("dirty")
    if isinstance(head, str) and FULL_SHA.match(head):
        try:
            sha = _commit(head)
        except RuntimeError:
            sha = None  # recorded by a box whose commit this checkout does not hold
        if sha:
            approx = ("; %d audited file(s) differed from it, so the corpus is approximate"
                      % dirty) if isinstance(dirty, int) and dirty > 0 else ""
            return {"sha": sha, "baseline": baseline,
                    "how": "head recorded with the baseline reading of %s%s" % (stamp, approx)}
    unusable = " (the head recorded on that row is not a commit in this checkout)" if head else ""
    sha = _git("rev-list", "-1", "--before=%s+0000" % stamp,
               _commit(until) if until else "HEAD").decode().strip()
    if not sha:
        raise RuntimeError("no commit at or before the baseline reading %s" % stamp)
    return {"sha": sha, "baseline": baseline,
            "how": "last commit at or before the baseline reading of %s%s" % (stamp, unusable)}


def _populated(label, collected):
    """Refuse a tree that read as empty: an empty `before` makes every site look new and an
    empty `after` makes every site look fixed, so a blind run would read as a delta."""
    records, write_names, _read, skill_files = collected
    if not (records and write_names and skill_files):
        raise RuntimeError(
            "the %s tree read an empty population (write wrappers %d, skill files %d, call "
            "sites %d); its inputs could not be rebuilt, so no delta is reported"
            % (label, len(write_names), skill_files, len(records)))


def _moves(src, dst):
    """Unverified sites in `dst` beyond those in `src`, counted per (file, wrapper, text).

    Counting per key and not per position is what makes a line shift invisible, and
    keeps repeated identical lines from collapsing into one.
    """
    def by_key(records):
        out = {}
        for r in records:
            out.setdefault((r["file"], r["wrapper"], r["text"]), []).append(r)
        return out

    s, d = by_key(src), by_key(dst)
    rows = []
    for key, there in d.items():
        here = s.get(key, [])
        n = (sum(1 for r in there if not r["verified"])
             - sum(1 for r in here if not r["verified"]))
        if n <= 0:
            continue
        # Identical lines cannot be told apart by their text, so the i-th of one tree is
        # matched to the i-th of the other (twins keep their order unless one is inserted
        # among them) and the instances that changed state are named. If that disagrees
        # with the count, every unverified twin is named instead: wide, never wrong.
        moved = [r for i, r in enumerate(there)
                 if not r["verified"] and (i >= len(here) or here[i]["verified"])]
        shown = moved if len(moved) == n else [r for r in there if not r["verified"]]
        absent = min(n, max(0, len(there) - len(here)))
        rows.append({
            "file": key[0], "wrapper": key[1], "text": key[2],
            "lines": [r["line"] for r in shown], "identical": len(there),
            "count": n, "absent": absent, "verified_other": n - absent,
            "other_evidence": sorted({r["evidence"] for r in here if r["verified"]}),
        })
    return sorted(rows, key=lambda r: (r["file"], r["lines"][0]))


def delta_report(before, after):
    """Compare two collect() results -> dict. `joined` entered the unverified set between
    them and `left` departed it, so joined minus left is the change in `unverified`."""
    def pop(collected):
        records, write_names, _read, skill_files = collected
        verified = sum(1 for r in records if r["verified"])
        return {"unverified": len(records) - verified, "verified": verified,
                "call_sites": len(records), "write_wrappers": len(write_names),
                "skill_files": skill_files}

    return {
        "before": pop(before), "after": pop(after),
        "joined": _moves(before[0], after[0]), "left": _moves(after[0], before[0]),
        "wrappers_joined": sorted(after[1] - before[1]),
        "wrappers_dropped": sorted(before[1] - after[1]),
    }


def render_delta(rep):
    b, a = rep["before"], rep["after"]
    out = ["unchecked-write delta",
           "  before      %s" % rep["before_label"],
           "  after       %s" % rep["after_label"],
           "  unverified  %d -> %d (%+d)   verified  %d -> %d" % (
               b["unverified"], a["unverified"], a["unverified"] - b["unverified"],
               b["verified"], a["verified"]),
           "  population  write wrappers %d -> %d | skill files %d -> %d | call sites %d -> %d" % (
               b["write_wrappers"], a["write_wrappers"], b["skill_files"], a["skill_files"],
               b["call_sites"], a["call_sites"])]
    if rep["baseline"] is not None:
        out.append("  baseline    recorded %d; the before tree reads %d under the current matcher"
                   % (rep["baseline"], b["unverified"]))
        if rep["baseline"] != b["unverified"]:
            out.append("  NOTE        they differ, so the matcher or the commit match has moved "
                       "since that reading: the lists below explain the change from %d, not from %d"
                       % (b["unverified"], rep["baseline"]))
    for title, rows, tree, words in (
            ("JOINED", rep["joined"], "AFTER", ("new site", "credit lost")),
            ("LEFT", rep["left"], "BEFORE", ("site removed", "now verified"))):
        out += ["", "%s the unverified set (%d) -- line numbers are in the %s tree"
                % (title, sum(r["count"] for r in rows), tree)]
        for r in rows:
            why = []
            if r["absent"]:
                why.append(words[0] + (" (wrapper newly a write wrapper)"
                                       if title == "JOINED" and r["wrapper"] in rep["wrappers_joined"]
                                       else ""))
            if r["verified_other"]:
                why.append("%s (%s %s)" % (words[1], "was" if title == "JOINED" else "now",
                                           ", ".join(r["other_evidence"])))
            lines = ",".join(str(n) for n in r["lines"])
            if r["identical"] > 1:
                if len(r["lines"]) == r["count"]:
                    lines += " (%d of %d identical lines, matched by order)" % (
                        r["count"], r["identical"])
                else:
                    lines += " (%d of these %d unverified identical lines)" % (
                        r["count"], len(r["lines"]))
            out += ["  %s:%s  %s  %s" % (r["file"], lines, r["wrapper"], "; ".join(why)),
                    "      %s" % r["text"]]
    out += ["", "write wrappers that joined the population: %s"
            % (", ".join(rep["wrappers_joined"]) or "none"),
           "write wrappers that left the population: %s"
            % (", ".join(rep["wrappers_dropped"]) or "none")]
    return "\n".join(out)


def delta_main(since, until, as_json):
    """The `--new-since` mode -> 0 when a delta was reported, 2 when it could not be."""
    try:
        ref = resolve_since(since, until)
        before = collect_at(ref["sha"])
        _populated("before", before)
        if until:
            sha = _commit(until)
            after, after_label = collect_at(sha), _describe(sha)
        else:
            after = collect()
            after_label = "working tree (HEAD %s)" % _git(
                "rev-parse", "--short", "HEAD").decode().strip()
        _populated("after", after)
        rep = delta_report(before, after)
        rep.update(before_label="%s  [%s]" % (_describe(ref["sha"]), ref["how"]),
                   after_label=after_label, baseline=ref["baseline"])
    except Exception as e:  # a failure is reported, never read as "no change"
        print("ERROR: %s" % e, file=sys.stderr)
        return 2
    if hasattr(sys.stdout, "reconfigure"):
        # Skill lines carry arrows and dashes: a console encoding that cannot hold them must
        # not cost the operator a finished report (the ratchet guards its stdout the same way).
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(rep, indent=2) if as_json else render_delta(rep))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit the full record set")
    ap.add_argument("--list-unverified", type=int, default=0,
                    help="print N unverified sites for eyeballing")
    ap.add_argument("--threshold", type=float, default=0.15,
                    help="CONFIRMED if verified_fraction is below this")
    ap.add_argument("--new-since", metavar="REV",
                    help="instead of the census, name the sites that joined the unverified "
                         "set since REV: a commit, or `baseline` for the commit at the "
                         "recorded baseline reading (with --json, emit the delta as JSON)")
    ap.add_argument("--until", metavar="REV",
                    help="with --new-since: compare against REV instead of the working tree")
    args = ap.parse_args()
    if args.until and not args.new_since:
        ap.error("--until needs --new-since")
    if args.new_since:
        return delta_main(args.new_since, args.until, args.json)

    records, write_names, read_names, skill_files = collect()

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
            "skill_files": skill_files,
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
        # The tree this count was taken over; the ratchet records it beside the reading.
        "provenance": provenance(),
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
