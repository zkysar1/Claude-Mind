#!/usr/bin/env python3
"""Signal-Lifecycle Gate — enforce init/increment/reset completeness.

Motivated by session-49 finding F1 (`consecutive_blocked_sleeps` + F2
`consecutive_goal_failures`): both counters had init + increment sites but
ZERO reset sites anywhere in the codebase. The backoff schedule inherited
stale elevated values across unrelated blocked episodes. The
verification-checklist check for this was a prose line that nobody could
actually verify, so it rotted into a false-PASS.

This gate parses the authoritative signal inventory from
`core/config/aspirations-loop-digest.md` and, for each discovered signal,
asserts that the codebase contains the three lifecycle sites the signal
needs (init + increment + reset). Signals that are structurally monotonic
(overwrite-latest strings, session-total accumulators) may opt out via the
MONOTONIC_WHITELIST below, which must include an inline rationale.

Also enforces the cadence-timestamp single-writer rule (guard-155): every
`last_*_at` / `last_*_time` / `last_strategic_scan` slot discovered in the
codebase must have exactly ONE `wm-set.sh <slot>` writer. >1 writer is a
duplication-of-authority bug; 0 writers is a phantom read (F3 class — caught
retroactively at session-49).

Contract
  --digest <path>    override the default digest path
  --check <names>    comma-separated subset. Options:
                        session_signals_lifecycle
                        cadence_single_writer
                        phantom_reads
                        wm_set_positional_value
                     Default: all four.
  --json (default) | --text

Exit codes
  0 pass, 1 block. Fail-open on missing digest or unparseable files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from _paths import CORE_ROOT, PROJECT_ROOT

DIGEST = CORE_ROOT / "config" / "aspirations-loop-digest.md"
SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"
SCRIPTS_DIR = CORE_ROOT / "scripts"

ALL_CHECKS = ("session_signals_lifecycle", "cadence_single_writer",
              "phantom_reads", "wm_set_positional_value")

# CRITICAL: the verifier skill itself CONTAINS grep patterns that look like
# code (e.g., `grep -rn "wm-set.sh last_strategic_scan"` assertion). Scanning
# it inflates writer counts for every cadence slot we check. Exclude it.
# Same logic for this gate's own source — we reference the slots we check by
# name in docstrings and whitelist keys.
CORPUS_EXCLUSIONS = {
    "verify-learning/SKILL.md",
    "signal-lifecycle-gate.py",
    "skill-structure-gate.py",
    # _prose_filter.py contains the triple-quote regex itself
    # (`r'"""|\'\'\''`) and a comment line citing `"""` and `'''` as the
    # tokens it matches. Both confuse our own parity tracker — exclude the
    # file (a regex literal is not a real docstring delimiter).
    "_prose_filter.py",
}

# Signals whose LIFECYCLE CONTRACT differs from init-increment-reset.
# KEY: signal name (without session_signals prefix).
# VALUE: (exempt_sites, rationale) — exempt_sites is a set of the three
#   lifecycle stages that don't apply to this signal. Rationale MUST be
#   non-empty; empty rationale is rejected at load time to force thought.
#   A fully-exempt signal (all three sites) is effectively an opt-out of
#   the lifecycle check — reserve for dict/set container slots whose
#   lifecycle is per-key, not per-slot.
MONOTONIC_WHITELIST = {
    "routine_count_total": (
        {"reset"},
        "session-total accumulator, explicitly documented in digest as "
        "'never resets mid-session'",
    ),
    "last_failed_goal_id": (
        {"increment", "reset"},
        "string slot — overwrite-by-latest-value semantics; neither "
        "'increment' nor 'reset to zero' apply to a string",
    ),
    "last_evolution_goal_count": (
        {"increment", "reset"},
        "snapshot counter reassigned to current goal count at evolution "
        "time; not an accumulator",
    ),
    "goals_completed_this_session": (
        {"reset"},
        "session-total, monotonic by design (counts ALL goals, not streaks)",
    ),
    "productive_goals_this_session": (
        {"reset"},
        "session-total, monotonic by design (counts DEEP outcomes)",
    ),
    "evolutions_this_session": (
        {"reset"},
        "session-total, monotonic by design",
    ),
    "goals_since_last_alignment_check": (
        {"increment", "reset"},
        "paired with alignment-check reset site, which is too variable to "
        "detect by regex without false negatives",
    ),
    "aspirations_touched_this_session": (
        {"reset"},
        "set accumulator; reset is session-scoped, not mid-session",
    ),
    "routine_streaks": (
        {"init", "increment", "reset"},
        "dict-of-counters — lifecycle is per-key (routine_streaks[goal.id]), "
        "not per-slot. Init is implicit empty dict; per-key add/reset pattern "
        "is already visible in digest 4.1 Block A.",
    ),
    "goals_since_last_tree_update": (
        {"increment"},
        "increment is performed by tree-encoding-drift-gate.py via atomic "
        "read-modify-write on working-memory.yaml (lines 100-127), NOT by an "
        "in-line `signals['goals_since_last_tree_update'] += 1` pattern in a "
        "skill or digest. The regex scanner cannot detect direct-file-I/O "
        "increments. iteration-close.sh do_state_update invokes the gate, "
        "which is the SINGLE writer per the gate's own docstring. Init and "
        "reset sites are still detected and validated.",
    ),
}


def _validate_whitelist():
    """Sanity-check MONOTONIC_WHITELIST at load time."""
    valid_sites = {"init", "increment", "reset"}
    for sig, entry in MONOTONIC_WHITELIST.items():
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise RuntimeError(
                f"MONOTONIC_WHITELIST[{sig!r}] must be a (set, str) tuple"
            )
        sites, reason = entry
        if not isinstance(sites, set) or not sites.issubset(valid_sites):
            raise RuntimeError(
                f"MONOTONIC_WHITELIST[{sig!r}] sites must be a subset of "
                f"{valid_sites}"
            )
        if not reason or not reason.strip():
            raise RuntimeError(
                f"MONOTONIC_WHITELIST[{sig!r}] rationale cannot be empty"
            )


_validate_whitelist()

# Search roots for lifecycle patterns.
_SEARCH_ROOTS = [
    SKILLS_DIR,
    SCRIPTS_DIR,
    CORE_ROOT / "config",
]

# --- Digest parsing ----------------------------------------------------------

_ITERATION_VAR_RE = re.compile(
    r"^([a-z][a-z0-9_]+)\s+\([^)]+\)", re.MULTILINE
)

# Sub-keys sentence looks like:
#   `session_signals` sub-keys: `foo`, `bar`, `baz`.
#   continuing across multiple lines until a period.
_SUBKEYS_RE = re.compile(
    r"`session_signals`\s+sub-keys:\s*([^.]+?)\.",
    re.MULTILINE | re.DOTALL,
)
_BACKTICK_NAME_RE = re.compile(r"`([a-z][a-z0-9_]+)`")


def discover_signals_from_digest(digest_path: Path) -> tuple:
    """Return (iteration_vars, session_signal_subkeys).

    Both are lists of strings. Fail-open: unreadable digest yields ([], [])."""
    try:
        text = digest_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []
    # Iteration State Variables code fence
    in_code = False
    iter_vars = []
    for line in text.splitlines():
        if line.startswith("## ") and "Iteration State Variables" in line:
            # Next ``` begins the block
            in_code = "pending"
            continue
        if in_code == "pending":
            if line.strip().startswith("```"):
                in_code = True
            continue
        if in_code is True:
            if line.strip().startswith("```"):
                break
            m = _ITERATION_VAR_RE.match(line)
            if m:
                iter_vars.append(m.group(1))
    # session_signals sub-keys sentence
    subkeys = []
    m = _SUBKEYS_RE.search(text)
    if m:
        for tm in _BACKTICK_NAME_RE.finditer(m.group(1)):
            subkeys.append(tm.group(1))
    return iter_vars, subkeys


# --- Lifecycle scanning ------------------------------------------------------

def _gather_text(roots: list) -> list:
    """Yield (path, text) tuples for every .md, .sh, .py under the roots.

    Skips binary files, dotfiles, and any path whose tail matches
    CORPUS_EXCLUSIONS. The verifier skill is intentionally excluded because
    it contains grep patterns that look like writer/reader code."""
    out = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix not in (".md", ".sh", ".py", ".yaml"):
                continue
            if "/.history/" in p.as_posix() or "\\.history\\" in str(p):
                continue
            # Skip files whose trailing-path matches an exclusion token.
            posix = p.as_posix()
            if any(posix.endswith("/" + excl) or posix.endswith(excl)
                   for excl in CORPUS_EXCLUSIONS):
                continue
            try:
                out.append((p, p.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
    return out


def _find_lifecycle_sites(signal: str, corpus: list) -> dict:
    """Return {'init':[...], 'increment':[...], 'reset':[...]} for a signal.

    Each list contains (path, line_number) tuples for matches."""
    # Init pattern — YAML-style dict-member init: `<signal>: 0` / `: null` /
    # `: None` / `: false` / `: {}` / `: []` / `: set()` / `: dict[...]` /
    # `: loop_state.X` (migration from previous iteration's state).
    # Also `<signal> = loop_state.X` (python-style pull from prior state
    # is a form of init on a fresh session).
    init_re = re.compile(
        rf"\b{re.escape(signal)}\s*[:=]\s*"
        rf"(0|null|None|false|\[\]|\{{\}}|set\(\)|dict\[|loop_state\.)"
    )
    # Strict increment (direct adjacency): `<signal> += N` / `<signal>++` /
    # `<signal>[x] +=` / `<signal>.add(` / `<signal>.append(`.
    inc_strict_re = re.compile(
        rf"\b{re.escape(signal)}(?:\[[^\]]*\])?\s*"
        rf"(\+=|\+\+|\.add\(|\.append\()"
    )
    # Loose increment (same-line proximity) — used when strict misses. This
    # catches pseudocode like:
    #   `IF cond: goals_since_last_tree_update = 0 ELSE +=1.`
    # where the slot name and the `+=` appear on the same line but are
    # separated by other tokens. Only applied when the strict regex fails,
    # so it can't mask a direct-adjacency match.
    sig_re = re.compile(rf"\b{re.escape(signal)}\b")
    op_re = re.compile(r"(\+=|\+\+|\.add\(|\.append\()")
    # Reset: `<signal> = 0` / `= null` / `= None` (with space around =),
    # OR `<signal>[key] = 0` for dict-of-counters reset-in-place. Exclude
    # the YAML-init form `<signal>: 0` via the explicit `=` requirement.
    reset_re = re.compile(
        rf"\b{re.escape(signal)}(?:\[[^\]]*\])?\s*=\s*(0|null|None|false)\b"
    )
    sites = {"init": [], "increment": [], "reset": []}
    for path, text in corpus:
        for lineno, line in enumerate(text.splitlines(), start=1):
            if init_re.search(line):
                sites["init"].append((str(path), lineno))
            # Skip loose-increment detection if the line is obviously prose
            # about the slot rather than code (no operator at all).
            if inc_strict_re.search(line):
                sites["increment"].append((str(path), lineno))
            elif sig_re.search(line) and op_re.search(line):
                # Proximity match — both the slot name and an increment
                # operator are on this line. Treat as increment.
                sites["increment"].append((str(path), lineno))
            if reset_re.search(line):
                sites["reset"].append((str(path), lineno))
    return sites


# --- WM slot scanning (cadence + phantom reads) ------------------------------

_WM_SET_RE = re.compile(r"wm-set\.sh\s+([a-z_][a-z0-9_]*)")
_WM_READ_RE = re.compile(r"wm-read\.sh\s+([a-z_][a-z0-9_]*)")
# Broken positional-value syntax: `wm-set.sh SLOT VALUE` — argparse rejects
# the extra positional with "unrecognized arguments". Canonical form is
# `echo '<value>' | wm-set.sh SLOT` (stdin-piped). This regex anchors on
# the SLOT ident, then requires whitespace + a non-terminator char to flag
# broken callsites. Terminators that indicate a legitimate trailing form:
#   |  &  ;  \  #  )  ]  }  ,  `  "  '  >  <
# (`>` / `<` rule out rare redirection; trailing quote/backtick rule out
# code-fence closers in .md; comma / bracket chars rule out inline-JSON docs.)
_WM_SET_POSITIONAL_RE = re.compile(
    r"wm-set\.sh\s+([a-z_][a-z0-9_]*)\s+([^\s#|&;\\)\]},`\"'<>]+)"
)
# wm-append.sh is also a writer — appending creates a slot if absent. For the
# phantom-reads check, a slot touched only by append is still "written".
_WM_APPEND_RE = re.compile(r"wm-append\.sh\s+([a-z_][a-z0-9_]*)")
# wm-set-if-unset.sh is rare but behaves like a write.
_WM_SET_IF_UNSET_RE = re.compile(r"wm-set-if-unset\.sh\s+([a-z_][a-z0-9_]*)")
# Python scripts that use sys.executable / subprocess call wm.py directly.
# The two real patterns (both single-line — see LINE-SCOPED note below):
#   A. Nested list:  `_run_py("wm.py", ["read", "slot"])`            (create-blocker.py, backfill-closes-knowledge-debt.py)
#   B. Flat outer-list with path-concat:
#                    `[str(CORE_ROOT / "scripts" / "wm.py"), "read", "slot"]`  (blocker-recheck.py, conclusion-record.py)
# The VERB ("read"/"set"/"append"/"set-if-unset") MUST be a quoted Python
# string literal — i.e., immediately wrapped in `'` or `"`. A bare word like
# `f"wm.py read returned non-JSON: ..."` (create-blocker.py:107 class) is
# prose inside an f-string, NOT a subprocess call, and must not match.
# The slot name must also be quote-prefixed for the same reason.
#
# LINE-SCOPED: the scanner iterates text.splitlines(), so a subprocess call
# broken across physical lines will NOT match and the slot becomes a phantom
# read/write. The codebase discipline enforcing "keep on ONE physical line"
# is documented at conclusion-record.py:133 ("INVARIANT" comment). Do not
# re-flow real writers to multi-line without also teaching this scanner
# multiline mode.
#
# PROSE IN .py COMMENTS: Layer 1 (_prose_filter.is_prose_line) deliberately
# does NOT skip `.py` comment lines (per rb-349 — admitting .py would hide
# real writers). A `.py` comment that quotes verb+slot WILL match here
# (e.g., conclusion-record.py:133 registers `conclusions` as an extra
# writer). Harmless when the slot also has a real writer and is not a
# cadence slot (cadence_single_writer would otherwise flag >1 writers).
# Do NOT "fix" this by extending Layer 1 to .py — re-read rb-349 first.
#
# Do NOT add `/` to the separator class — `/` never appears after `wm.py`
# in real code and admitting it widens the match for no detection benefit.
_WM_PY_WRITE_RE = re.compile(
    r"""wm\.py["'()\[\],\s]*?['"](?:set|append|set-if-unset)['"][\s,\]]+['"]([a-z_][a-z0-9_]*)"""
)
_WM_PY_READ_RE = re.compile(
    r"""wm\.py["'()\[\],\s]*?['"]read['"][\s,\]]+['"]([a-z_][a-z0-9_]*)"""
)

# Two-layer prose filter shared with skill-structure-gate.py via _prose_filter.py
# (rb-349, guard-319). `_is_prose_line` is Layer 1 (comment-line skip);
# `_strip_prose_refs` is Layer 2 (inline-backtick strip in .md).
from _prose_filter import is_prose_line as _is_prose_line
from _prose_filter import strip_prose_refs as _strip_prose_refs
from _prose_filter import pyfile_docstring_lines as _pyfile_docstring_lines


def _gather_wm_ops(corpus: list) -> tuple:
    """Return (writers_by_slot, readers_by_slot) dict[str -> list[(path,lineno)]].

    Writers include wm-set.sh, wm-append.sh, wm-set-if-unset.sh. For the
    cadence-single-writer check, callers filter to wm-set.sh only via
    the `_gather_strict_set_writers` helper below."""
    writers = {}
    readers = {}
    for path, text in corpus:
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _is_prose_line(line, path):
                continue
            effective = _strip_prose_refs(line, path)
            for rx in (_WM_SET_RE, _WM_APPEND_RE, _WM_SET_IF_UNSET_RE,
                      _WM_PY_WRITE_RE):
                for m in rx.finditer(effective):
                    writers.setdefault(m.group(1), []).append((str(path), lineno))
            for rx in (_WM_READ_RE, _WM_PY_READ_RE):
                for m in rx.finditer(effective):
                    readers.setdefault(m.group(1), []).append((str(path), lineno))
    return writers, readers


def _gather_strict_set_writers(corpus: list) -> dict:
    """Return writers_by_slot restricted to `wm-set.sh` calls only.

    Cadence timestamps must be overwritten in-place by wm-set.sh per
    guard-155; wm-append semantics would never converge to a single value,
    so an append-only slot matching a `last_*` name is itself a bug.
    """
    writers = {}
    for path, text in corpus:
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _is_prose_line(line, path):
                continue
            effective = _strip_prose_refs(line, path)
            for m in _WM_SET_RE.finditer(effective):
                writers.setdefault(m.group(1), []).append((str(path), lineno))
    return writers


# --- Check runners -----------------------------------------------------------

# Signals that are the CONTAINER itself (a dict grouping other signals), not
# mutable entries. The digest lists `session_signals` as a slot and `routine_streaks`
# as a dict-type slot — both are operated on by key, not as whole-object counters.
CONTAINER_SLOTS = {"session_signals"}


def check_session_signals_lifecycle(signals: list, corpus: list) -> list:
    """For each signal, require init + increment + reset unless whitelisted."""
    violations = []
    for sig in signals:
        if sig in CONTAINER_SLOTS:
            continue  # container dicts are scanned by their sub-keys, not directly
        sites = _find_lifecycle_sites(sig, corpus)
        exempt_sites, reason = MONOTONIC_WHITELIST.get(sig, (set(), ""))
        missing = []
        for stage in ("init", "increment", "reset"):
            if stage in exempt_sites:
                continue
            if not sites[stage]:
                missing.append(stage)
        if missing:
            violations.append({
                "check": "session_signals_lifecycle",
                "signal": sig,
                "missing": missing,
                "whitelist_exempt_sites": sorted(exempt_sites),
                "whitelist_reason": reason,
                "found_sites": {
                    k: len(v) for k, v in sites.items()
                },
            })
    return violations


def check_cadence_single_writer(writers: dict, readers: dict) -> list:
    """Slots matching cadence-timestamp naming must have exactly 1 writer."""
    violations = []
    cadence_slots = {
        slot for slot in set(list(writers.keys()) + list(readers.keys()))
        if slot.startswith("last_") and (
            slot.endswith("_at") or slot.endswith("_time")
            or slot == "last_strategic_scan"
        )
    }
    for slot in sorted(cadence_slots):
        n = len(writers.get(slot, []))
        if n == 0:
            violations.append({
                "check": "cadence_single_writer",
                "slot": slot,
                "writer_count": 0,
                "reader_count": len(readers.get(slot, [])),
                "detail": (
                    f"cadence slot {slot!r} is read but NEVER written — "
                    "time-based triggers depending on it silently never "
                    "fire (phantom read, see F3 in session-49 plan)"
                ),
            })
        elif n > 1:
            violations.append({
                "check": "cadence_single_writer",
                "slot": slot,
                "writer_count": n,
                "writers": writers.get(slot, []),
                "detail": (
                    f"cadence slot {slot!r} has {n} writers — violates "
                    "guard-155 single-writer rule. The skill that performs "
                    "the action owns the timestamp."
                ),
            })
    return violations


def check_wm_set_positional_value(corpus: list) -> list:
    """Flag `wm-set.sh SLOT VALUE` callsites. wm-set.sh reads the value from
    stdin; any extra positional argument is rejected by argparse with
    "unrecognized arguments". The canonical form is
    `echo '<value>' | wm-set.sh SLOT`. Static scan catches the broken form
    before it ships — the prior 4-callsite regression in aspirations-select
    (program_misalignment_streak / boost_generative_sparks) silently failed
    every iteration because the error surfaced on stderr, not in the loop's
    control flow."""
    violations = []
    for path, text in corpus:
        # Python docstrings can contain `wm-set.sh SLOT VALUE` as prose
        # (e.g., infra-health.py docstring: "Replaces the prior wm-read.sh +
        # wm-set.sh subprocess pair"). Real Python callsites go through
        # subprocess.run with arg-lists, not space-separated bash commands,
        # so the positional pattern cannot appear in executable .py code —
        # the docstring-line skip is safe for THIS check. Compute once per
        # file. rb-349 still applies to OTHER checks where a real writer
        # could follow a `#`-comment line; do not generalize this skip.
        docstring_lines = _pyfile_docstring_lines(text, path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _is_prose_line(line, path):
                continue
            # .py `#`-comment lines AND triple-quoted docstring lines are
            # both prose for this scanner (see comment above).
            if path.suffix == ".py" and (
                line.lstrip().startswith("#") or lineno in docstring_lines
            ):
                continue
            effective = _strip_prose_refs(line, path)
            for m in _WM_SET_POSITIONAL_RE.finditer(effective):
                slot = m.group(1)
                extra = m.group(2)
                violations.append({
                    "check": "wm_set_positional_value",
                    "slot": slot,
                    "site": (str(path), lineno),
                    "extra_arg_snippet": extra[:40],
                    "detail": (
                        f"wm-set.sh {slot!r} called with extra positional "
                        f"arg {extra[:30]!r} — argparse rejects this. "
                        f"Use `echo '<value>' | wm-set.sh {slot}` instead."
                    ),
                })
    return violations


def check_phantom_reads(writers: dict, readers: dict) -> list:
    """Any WM slot that's read but never written is a phantom read."""
    violations = []
    # Known intentionally-read-before-written slots: none yet. If one is
    # genuinely expected (e.g. compact restore slot written by a hook, not
    # a skill), add it here with a rationale.
    whitelist = {
        # Two categories of legitimate phantom-read exemptions:
        #
        # CATEGORY 1 — Script-direct writers that bypass the wm-set.sh
        # wrapper for valid reasons (Windows-MSYS subprocess filesystem-view
        # mismatch). The phantom-reads writer-detection regex scans for
        # `wm-set.sh KEY value` bash invocations and does NOT inspect
        # `wm.py set KEY` subprocess calls or direct working-memory.yaml
        # file I/O. Each entry below is cross-checked: the named script
        # is the single writer for the slot.
        #
        # CATEGORY 2 — User-only kill switches. The slot has NO programmatic
        # writer by design — the user invokes `wm-set.sh KEY value`
        # manually to flip the switch. Exempt because the alternative
        # (auto-clearing or auto-setting) would defeat the point of the
        # switch. Comment value documents the user invocation.
        "consolidation_health":          # written by consolidation-health.py
                                         # via `sys.executable wm.py set
                                         # consolidation_health` (line ~173).
                                         # Direct python invocation chosen
                                         # because MSYS Git Bash on Windows
                                         # cannot resolve Windows-style
                                         # python paths through bash wrappers
                                         # in some hook contexts.
            "consolidation-health.py",
        "goals_since_last_tree_update":  # written by tree-encoding-drift-
                                         # gate.py via atomic read-modify-
                                         # write on working-memory.yaml
                                         # (lines 100-127). Direct file I/O
                                         # chosen because subprocess-spawned
                                         # bash has a different MSYS
                                         # filesystem view than the parent.
                                         # Single writer; iteration-close.sh
                                         # do_state_update invokes the gate.
            "tree-encoding-drift-gate.py",
        # CATEGORY 2 entries:
        "suppress_user_push":            # User-only kill switch for
                                         # aspirations-state-update Step
                                         # 8.77 (user-notable event push
                                         # classifier). User invokes
                                         # `bash core/scripts/wm-set.sh
                                         # suppress_user_push true` to
                                         # mute pushes during on-call /
                                         # focus periods; clear with
                                         # `... wm-set.sh suppress_user_push
                                         # false`. Intentionally has no
                                         # programmatic writer — the agent
                                         # must never auto-mute itself,
                                         # and an automated writer would
                                         # defeat the user-control intent.
            "user-only-kill-switch",
    }
    for slot in sorted(readers.keys()):
        if slot.startswith("last_") and (slot.endswith("_at")
                                          or slot.endswith("_time")
                                          or slot == "last_strategic_scan"):
            # Handled by cadence_single_writer to avoid double-reporting.
            continue
        if slot in whitelist:
            continue
        if slot not in writers:
            violations.append({
                "check": "phantom_reads",
                "slot": slot,
                "reader_count": len(readers[slot]),
                "sample_reader": readers[slot][0],
                "detail": (
                    f"slot {slot!r} is read but has no wm-set.sh writer "
                    "anywhere in the codebase"
                ),
            })
    return violations


# --- Entry point -------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Signal-Lifecycle Gate: verify init/increment/reset "
                    "completeness + cadence single-writer.",
    )
    ap.add_argument("--digest", default=str(DIGEST),
                    help="Path to aspirations-loop-digest.md")
    ap.add_argument("--check", default=",".join(ALL_CHECKS),
                    help=f"Comma-separated checks. Default: all. Options: {ALL_CHECKS}")
    ap.add_argument("--text", action="store_true",
                    help="Human summary on stdout (JSON on stderr).")
    args = ap.parse_args()

    checks = {c.strip() for c in args.check.split(",") if c.strip()}
    unknown = checks - set(ALL_CHECKS)
    if unknown:
        print(f"[signal-lifecycle-gate] error: unknown check(s): {sorted(unknown)}",
              file=sys.stderr)
        sys.exit(2)

    iter_vars, subkeys = discover_signals_from_digest(Path(args.digest))
    all_signals = sorted(set(iter_vars) | set(subkeys))

    corpus = _gather_text(_SEARCH_ROOTS)
    writers, readers = _gather_wm_ops(corpus)
    strict_writers = _gather_strict_set_writers(corpus)

    violations = []
    if "session_signals_lifecycle" in checks:
        violations.extend(check_session_signals_lifecycle(all_signals, corpus))
    if "cadence_single_writer" in checks:
        # Cadence slots must use wm-set.sh (overwrite) — not wm-append.
        violations.extend(check_cadence_single_writer(strict_writers, readers))
    if "phantom_reads" in checks:
        # Phantom-reads: any WM write (set/append/set-if-unset) counts.
        violations.extend(check_phantom_reads(writers, readers))
    if "wm_set_positional_value" in checks:
        violations.extend(check_wm_set_positional_value(corpus))

    result = {
        "digest": str(args.digest),
        "checks_run": sorted(checks),
        "signals_discovered": all_signals,
        "iteration_vars": iter_vars,
        "session_signal_subkeys": subkeys,
        "wm_slot_writers": len(writers),
        "wm_slot_readers": len(readers),
        "violation_count": len(violations),
        "violations": violations,
        "would_block": bool(violations),
    }

    if args.text:
        print(f"signal-lifecycle-gate: {len(all_signals)} signals discovered, "
              f"{len(writers)} WM writers, {len(readers)} WM readers, "
              f"{len(violations)} violations")
        for v in violations:
            if v["check"] == "session_signals_lifecycle":
                print(f"  [{v['check']}] {v['signal']}: missing {v['missing']} "
                      f"(sites found: {v['found_sites']})")
            elif v["check"] == "wm_set_positional_value":
                p, ln = v["site"]
                print(f"  [{v['check']}] {v['slot']} @ {p}:{ln}: {v['detail']}")
            else:
                print(f"  [{v['check']}] {v['slot']}: {v['detail']}")
        print(json.dumps(result), file=sys.stderr)
    else:
        print(json.dumps(result))

    sys.exit(1 if violations else 0)


if __name__ == "__main__":
    main()
