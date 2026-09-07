"""Layer-C detective for the guard-2846 xtrace/credential defense ().

Layer A (xtrace-credential-gate.py) refuses a traced credential command at
PreToolUse. It is fail-open BY CONTRACT -- a hook timeout, an unset WORLD_PATH,
or an unexpected exception all approve rather than block. This sweep is what
notices when that happened.

    py -3 core/scripts/xtrace-credential-audit.py              # human summary
    py -3 core/scripts/xtrace-credential-audit.py --json       # machine-readable
    py -3 core/scripts/xtrace-credential-audit.py --exit-on-hits   # exit 1 on hits

TWO LANES, because the drift arrives by two different doors:

  CORPUS  -- a committed script, SKILL.md or convention that INSTRUCTS a traced
             credential invocation. Nobody is leaking yet; the instruction is
             waiting to be followed. This is the gradle-tests-audit shape.
  RUNTIME -- an offending command that actually appears in a recent transcript
             as an executed Bash call. The gate would have denied it, so its
             presence means the gate did not run: a hook timeout, a missing
             WORLD_PATH, or a bypass. Every hit here is a possible live leak and
             should be treated as one.

The predicate is imported from _xtrace_credential_predicate.py rather than
restated, so the detective and the gate cannot disagree about what "bad" means.
A divergence there is worse than either layer being absent, because the sweep
would report clean over exactly the population the gate stopped covering.

NOTE ON THE OVERRIDE: `offending()` returns empty when the override token is
present, so a deliberate, audited bypass is not reported as drift by either
lane. That is intended -- the token exists to make the bypass visible in the
command itself, and re-reporting it here would train readers to ignore the
sweep.

AUTHORING HITS ARE SEPARATED, NOT SUPPRESSED, and finding out why took running
this sweep against real transcripts (guard-1194). Its first live run flagged two
commands from the session that BUILT this defense: both embedded `bash -x
<credential script>` as test DATA inside a python heredoc, so the offending text
was quoted input to a program, never a traced invocation. That class recurs by
construction -- anyone writing or reviewing tests for this gate produces it --
and a sweep that reports hits from birth is one that gets ignored, which is the
same failure mode as the stale defense it exists to catch. So a command carrying
a heredoc opener or a file-write redirect is classified `authoring`: still
listed, never counted toward --exit-on-hits. The proxy is deliberately coarse
and can hide a genuine traced invocation that happens to sit inside a heredoc;
that is the accepted cost, and it is stated here rather than left for a reader
to discover from a silent count.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _xtrace_credential_predicate import offending  # noqa: E402

# Corpus files worth scanning: anything that can instruct a command.
CORPUS_GLOBS = (
    "core/scripts/*.sh",
    "core/config/**/*.md",
    ".claude/skills/**/*.md",
    ".claude/rules/*.md",
)

# This module and its two siblings QUOTE offending command shapes on purpose --
# in docstrings, deny text and tests. Scanning them would report the defense as
# the defect, every run, forever.
SELF_EXEMPT = (
    "_xtrace_credential_predicate.py",
    "xtrace-credential-gate.py",
    "xtrace-credential-audit.py",
    "xtrace-credential-gate.sh",
)


def _search_dirs(project_root):
    dirs = [str(Path(project_root) / "core" / "scripts")]
    world = os.environ.get("WORLD_PATH") or os.environ.get("MIND_WORLD")
    if world:
        dirs.append(str(Path(world) / "scripts"))
    return dirs


def _default_transcripts_dir(project_root):
    """Claude Code transcripts dir. Same convention as
    aspirations-rejection-audit.py: ~/.claude/projects/<dashified-path>."""
    s = str(Path(project_root).resolve()).replace("\\", "/")
    dashified = s.replace(":", "-").replace("/", "-")
    return Path(os.path.expanduser("~/.claude/projects")) / dashified


def scan_corpus(project_root, search_dirs):
    hits = []
    root = Path(project_root)
    for pattern in CORPUS_GLOBS:
        for path in root.glob(pattern):
            if not path.is_file() or path.name in SELF_EXEMPT:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                scripts = offending(line, project_root, search_dirs)
                if scripts:
                    hits.append({
                        "lane": "corpus",
                        "path": str(path.relative_to(root)),
                        "line": lineno,
                        "command": line.strip()[:200],
                        "scripts": [os.path.basename(s) for s in scripts],
                    })
    return hits


# A command that WRITES or QUOTES text rather than running it. Coarse by design
# -- see the module docstring for what this can hide.
_AUTHORING_MARKERS = ("<<'", '<<"', "<<PY", "<<EOF", "cat >", "cat >>", "tee ")


def _is_authoring(command):
    return any(m in command for m in _AUTHORING_MARKERS)


def _bash_commands(record):
    """Yield the command text of every Bash tool_use in one transcript record."""
    msg = record.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") != "Bash":
            continue
        cmd = (block.get("input") or {}).get("command")
        if isinstance(cmd, str):
            yield cmd


def scan_transcripts(transcripts_dir, since_hours, project_root, search_dirs):
    hits = []
    tdir = Path(transcripts_dir)
    if not tdir.is_dir():
        return hits, "transcripts dir absent: {}".format(tdir)
    cutoff = time.time() - since_hours * 3600
    scanned = 0
    for path in sorted(tdir.glob("*.jsonl")):
        try:
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        scanned += 1
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for lineno, raw in enumerate(fh, 1):
                    raw = raw.strip()
                    if not raw or "Bash" not in raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    for cmd in _bash_commands(record):
                        scripts = offending(cmd, project_root, search_dirs)
                        if scripts:
                            hits.append({
                                "lane": "authoring" if _is_authoring(cmd) else "runtime",
                                "path": path.name,
                                "line": lineno,
                                "command": cmd.strip()[:200],
                                "scripts": [os.path.basename(s) for s in scripts],
                            })
        except OSError:
            continue
    return hits, "scanned {} transcript(s) newer than {}h".format(scanned, since_hours)


def main():
    default_root = Path(__file__).resolve().parents[2]
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--exit-on-hits", action="store_true",
                   help="exit 1 when any hit is found (for a recurring goal / CI)")
    p.add_argument("--since-hours", type=int, default=24,
                   help="runtime lane: transcript age window (default 24)")
    p.add_argument("--project-root", type=Path, default=default_root)
    p.add_argument("--transcripts-dir", type=Path, default=None)
    p.add_argument("--corpus-only", action="store_true",
                   help="skip the runtime lane (no transcript access needed)")
    args = p.parse_args()

    root = str(args.project_root)
    search_dirs = _search_dirs(root)

    corpus_hits = scan_corpus(root, search_dirs)
    runtime_hits, runtime_note = [], "runtime lane skipped (--corpus-only)"
    if not args.corpus_only:
        tdir = args.transcripts_dir or _default_transcripts_dir(root)
        runtime_hits, runtime_note = scan_transcripts(
            tdir, args.since_hours, root, search_dirs)

    hits = corpus_hits + runtime_hits
    # Only real (non-authoring) hits drive the exit code.
    gating_hits = [h for h in hits if h["lane"] != "authoring"]
    if args.json:
        print(json.dumps({
            "hits": hits,
            "count": len(hits),
            "corpus_count": len(corpus_hits),
            "runtime_count": len([h for h in runtime_hits if h["lane"] == "runtime"]),
            "authoring_count": len([h for h in runtime_hits if h["lane"] == "authoring"]),
            "gating_count": len(gating_hits),
            "runtime_note": runtime_note,
        }, indent=2))
    elif not gating_hits and not hits:
        print("xtrace-credential-audit: clean — no traced credential invocations "
              "found.")
        print("  corpus lane : {} pattern(s) scanned, 0 hits".format(
            len(CORPUS_GLOBS)))
        print("  runtime lane: {}, 0 hits".format(runtime_note))
        print("\nSCOPE: this answers 'did an offending command appear', not 'is "
              "the gate wired'. A clean runtime lane is equally consistent with "
              "the gate working and with nobody having tried. Confirm the hook "
              "is registered in .claude/settings.json separately.")
    else:
        print("xtrace-credential-audit: {} hit(s) — commands that would trace a "
              "credential-loading script.\n".format(len(hits)))
        for hit in hits:
            print("  [{}] {}:{}".format(hit["lane"], hit["path"], hit["line"]))
            print("       scripts: {}".format(", ".join(hit["scripts"])))
            print("       {}".format(hit["command"]))
        print("\nRUNTIME-lane hits mean the PreToolUse gate did not run (it is "
              "fail-open): treat each as a possible live credential leak in that "
              "transcript. CORPUS-lane hits are instructions waiting to be "
              "followed. See guard-2846.")

    if hits and not gating_hits and not args.json:
        print("\nAll hits are lane=authoring (a command that QUOTES or WRITES the "
              "shape rather than running it). None counts toward --exit-on-hits.")
    return 1 if (gating_hits and args.exit_on_hits) else 0


if __name__ == "__main__":
    sys.exit(main())
