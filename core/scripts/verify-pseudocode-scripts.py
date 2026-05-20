#!/usr/bin/env python3
# Validates `bash|py -3|python3 core/scripts/<name>.<ext>` references in
# SKILL.md against actual script existence and --help output. Catches
# phantom scripts and phantom flags before they ship.
#
# Subcommand-aware: handles `py -3 core/scripts/aspirations.py read --active`
# by probing `aspirations.py --help` (top-level flags + subcommand list)
# and then ONLY the subcommands an invocation actually references.
#
# Parallel: probes run in a ThreadPoolExecutor (6 workers, 15s timeout).
# Full run on Windows ~3 min for ~170 unique scripts. The --scripts-only
# mode skips help probes entirely (~1.5s) for inline /verify-learning use.
#
# Exit: 0 clean, 1 phantom(s) found, 2 script error.

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_THIS = Path(__file__).resolve()
PROJECT_ROOT = _THIS.parent.parent.parent
SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"
SCRIPTS_DIR = PROJECT_ROOT / "core" / "scripts"


def _resolve_bash():
    # On Windows, `subprocess.run(['bash', ...])` can pick up WSL bash at
    # C:\Windows\System32\bash.exe, which sees the repo under /mnt/c/ and
    # cannot exec python3 against Windows-side scripts in finite time.
    # The framework's MIND_SHELL convention pins the right bash; honor it
    # first, then fall through to a Git Bash probe.
    env_shell = os.environ.get("MIND_SHELL")
    if env_shell and Path(env_shell).exists():
        return env_shell
    if sys.platform == "win32":
        for candidate in (
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
        ):
            if Path(candidate).exists():
                return candidate
    return shutil.which("bash") or "bash"


BASH = _resolve_bash()

INVOKE_RE = re.compile(
    r'\b(?:bash|py\s+-3|python3?)\s+core/scripts/([A-Za-z0-9_-]+)\.(sh|py)\b'
)

FLAG_RE = re.compile(r'(--[a-zA-Z][a-zA-Z0-9_-]*)')

# Boundary that ends one shell command. A second core/scripts invocation on
# the same line also bounds the prior one's flag scope. We deliberately omit
# `<` and `>` here even though they're real shell redirects: SKILL.md uses
# `<placeholder>` syntax far more often than literal `>file` redirects, and
# truncating on `<` drops flags after the placeholder (e.g.,
# `--source <agent> --active` would lose `--active`). Including a redirect's
# target path in the flag-scan segment is harmless — paths don't contain
# `--xxx` tokens.
CMD_END_RE = re.compile(
    r'(\||&&|;|`|(?:bash|py\s+-3|python3?)\s+core/scripts/)'
)

WORD_RE = re.compile(r'[a-zA-Z_][a-zA-Z0-9_-]*')

# Argparse subcommand line: 4-space indent + lowercase word + 2+ space gap.
SUBCMD_RE = re.compile(r'^    ([a-z][a-z0-9_-]*)\s{2,}\S')

# Any --flag literal that appears in a wrapper script's source. Bash
# wrappers commonly handle flags in case/while statements before
# delegating (e.g., `--text` → `--find`, `--source` consumed pre-subcmd).
# Those translations aren't visible in the wrapped script's --help, so we
# also extract every literal `--foo` token from the wrapper file itself
# and accept it as valid.
WRAPPER_FLAG_RE = re.compile(r'--[a-z][a-zA-Z0-9_-]*')

HELP_MARKERS = ("usage:", "options:", "optional arguments:")
HELP_TIMEOUT_SEC = 15
PROBE_WORKERS = 6


def _launcher_for(script_path):
    ext = script_path.suffix
    if ext == ".sh":
        return [BASH]
    if ext == ".py":
        return ["py", "-3"] if sys.platform == "win32" else ["python3"]
    return None


def _run_help(script_path, *extra_args):
    launcher = _launcher_for(script_path)
    if launcher is None:
        return None
    cmd = launcher + [str(script_path)] + list(extra_args) + ["--help"]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            timeout=HELP_TIMEOUT_SEC,
            text=True,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    blob = (r.stdout or "") + "\n" + (r.stderr or "")
    if not any(m in blob.lower() for m in HELP_MARKERS):
        return None
    return blob


def _parse_flags(text):
    return set(FLAG_RE.findall(text))


def _parse_subcommands(text):
    return {m.group(1) for m in (SUBCMD_RE.match(ln) for ln in text.splitlines()) if m}


def _segment_after_match(line, match_end_idx):
    rest = line[match_end_idx:]
    cut = CMD_END_RE.search(rest)
    if cut:
        rest = rest[:cut.start()]
    return rest


def _find_subcommand(segment, known_subs):
    if not known_subs:
        return None
    for tok in WORD_RE.findall(segment):
        if tok in known_subs:
            return tok
    return None


def _extract_wrapper_flags(script_path):
    """For .sh wrappers, scan source for case-handled --flag literals.
    For .py, return empty set. The wrapper file itself is the source of
    truth for flags consumed before delegation."""
    if script_path.suffix != ".sh":
        return set()
    try:
        text = script_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    return set(WRAPPER_FLAG_RE.findall(text))


def _probe_top(filename):
    """Returns ('missing', None) | ('no-help', None) | ('ok', {top_flags, subcommands})."""
    script_path = SCRIPTS_DIR / filename
    if not script_path.exists():
        return ("missing", None)
    blob = _run_help(script_path)
    wrapper_flags = _extract_wrapper_flags(script_path)
    if blob is None:
        # --help failed (script lacks argparse, or timed out under load).
        # Fail open: wrapper_flags alone is NOT enough — a wrapper that
        # case-handles --read-only but forwards everything else to a
        # python script would falsely flag valid forwarded flags as
        # phantom. Validation requires confident help output.
        return ("no-help", None)
    return ("ok", {
        "top_flags": _parse_flags(blob) | wrapper_flags,
        "subcommands": _parse_subcommands(blob),
    })


def _probe_sub(filename, subcmd):
    script_path = SCRIPTS_DIR / filename
    blob = _run_help(script_path, subcmd)
    return _parse_flags(blob) if blob is not None else None


def scan_files(skill_files):
    """Pass 1: collect every invocation site without probing anything."""
    invocations = []
    unique_scripts = set()
    for path in skill_files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"WARN: skipped {path}: {e}", file=sys.stderr)
            continue
        try:
            rel = str(path.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
        except ValueError:
            rel = str(path).replace("\\", "/")
        for line_no, line in enumerate(text.splitlines(), start=1):
            for m in INVOKE_RE.finditer(line):
                script_name = m.group(1)
                ext = m.group(2)
                if not any(c.islower() for c in script_name):
                    continue  # placeholder like X.py
                filename = f"{script_name}.{ext}"
                segment = _segment_after_match(line, m.end())
                invocations.append({
                    "file": rel, "line": line_no, "script": filename,
                    "segment": segment,
                    "snippet": line.strip()[:140],
                })
                unique_scripts.add(filename)
    return invocations, unique_scripts


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Validate `bash|py -3|python3 core/scripts/<name>.<ext>` "
                    "references in SKILL.md against script existence and "
                    "--help output (subcommand-aware, parallel probe).",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON results")
    p.add_argument("--dir", default=str(SKILLS_DIR),
                   help="Directory to recurse for SKILL.md "
                        "(default: .claude/skills/)")
    p.add_argument("--workers", type=int, default=PROBE_WORKERS,
                   help=f"Parallel --help probes (default: {PROBE_WORKERS})")
    p.add_argument("--scripts-only", action="store_true",
                   help="Existence check only — skip --help probes and flag "
                        "validation. ~10x faster (seconds vs minutes); use "
                        "in /verify-learning where the 3-minute full scan "
                        "would inflate evaluation time.")
    args = p.parse_args(argv)

    target = Path(args.dir).resolve()
    if not target.exists():
        print(f"ERROR: dir not found: {target}", file=sys.stderr)
        return 2

    skill_files = sorted(target.rglob("SKILL.md"))
    invocations, unique_scripts = scan_files(skill_files)

    # Pass 2: probe top-level --help for every unique script in parallel.
    # In --scripts-only mode we skip the subprocess probe and only record
    # whether each script exists on disk. This is the only check that
    # /verify-learning runs inline; full flag validation is too slow to
    # block its evaluation.
    top_info = {}
    if args.scripts_only:
        for fn in unique_scripts:
            top_info[fn] = ("missing", None) if not (SCRIPTS_DIR / fn).exists() else ("no-help", None)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(_probe_top, fn): fn for fn in unique_scripts}
            for fut in futures:
                fn = futures[fut]
                top_info[fn] = fut.result()

    # Pass 3: collect needed (script, subcommand) pairs from invocations.
    needed_subs = set()
    for inv in invocations:
        status, info = top_info.get(inv["script"], ("missing", None))
        if status != "ok":
            continue
        if not info["subcommands"]:
            continue
        sub = _find_subcommand(inv["segment"], info["subcommands"])
        if sub:
            needed_subs.add((inv["script"], sub))

    # Pass 4: probe needed subcommand --help in parallel.
    sub_flags = {}
    if not args.scripts_only and needed_subs:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(_probe_sub, fn, sc): (fn, sc) for fn, sc in needed_subs}
            for fut in futures:
                key = futures[fut]
                sub_flags[key] = fut.result()

    # Pass 5: validate every invocation against cached help.
    results = {"phantom_scripts": [], "phantom_flags": []}
    for inv in invocations:
        fn = inv["script"]
        status, info = top_info.get(fn, ("missing", None))
        if status == "missing":
            results["phantom_scripts"].append({
                "file": inv["file"], "line": inv["line"], "script": fn,
                "snippet": inv["snippet"],
            })
            continue
        if status != "ok":
            continue  # no argparse help; fail open
        valid = set(info["top_flags"])
        sub = _find_subcommand(inv["segment"], info["subcommands"])
        if sub:
            sf = sub_flags.get((fn, sub))
            if sf is None:
                continue  # sub --help failed; fail open
            valid |= sf
        elif info["subcommands"]:
            continue  # subcommand-style script but invocation has no match; fail open
        for f in FLAG_RE.findall(inv["segment"]):
            if f not in valid:
                results["phantom_flags"].append({
                    "file": inv["file"], "line": inv["line"], "script": fn,
                    "flag": f, "snippet": inv["snippet"],
                })

    found = bool(results["phantom_scripts"] or results["phantom_flags"])

    if args.json:
        results["scanned_files"] = len(skill_files)
        results["unique_scripts"] = len(unique_scripts)
        print(json.dumps(results, indent=2))
    else:
        if results["phantom_scripts"]:
            print(f"\n=== Phantom scripts ({len(results['phantom_scripts'])}) ===")
            for r in results["phantom_scripts"]:
                print(f"  {r['file']}:{r['line']}  {r['script']}")
                print(f"    {r['snippet']}")
        if results["phantom_flags"]:
            print(f"\n=== Phantom flags ({len(results['phantom_flags'])}) ===")
            for r in results["phantom_flags"]:
                print(f"  {r['file']}:{r['line']}  {r['script']}  {r['flag']}")
                print(f"    {r['snippet']}")
        if not found:
            print(f"clean — scanned {len(skill_files)} SKILL.md files, "
                  f"{len(unique_scripts)} unique scripts referenced")

    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
