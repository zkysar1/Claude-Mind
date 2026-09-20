"""Single source of truth: "would this command trace a credential-loading script?"

Imported by:
  - xtrace-credential-gate.py   (Layer A -- enforce at PreToolUse[Bash])
  - xtrace-credential-audit.py  (Layer C -- observe drift when the hook fails open)

CRITICAL: do not duplicate these predicates inline anywhere else. If a third
caller needs the same check, import it from here. The layers MUST agree on what
"bad" means or the detective's signal diverges from the gate's enforcement.

THE HAZARD (guard-2846). Shell xtrace echoes every expanded command to stderr.
A script that sources the credential loader has every secret in its environment,
so tracing it prints them in plaintext into a transcript that persists. The
guardrail has been correct and complete since it was written; it sat at
times_active 17 / times_helpful 0, because its trigger fires only for someone
who ALREADY suspects the hazard. The pull toward xtrace is strongest when a
script fails SILENTLY -- no output, no error, nothing to search on -- which is
exactly when nothing prompts a retrieval. This module moves the defence to the
moment of use.

WHY THE PREDICATE RESOLVES THROUGH THE SOURCE EDGE, WHICH IS THE WHOLE DESIGN.
The leaking script does not read the credential file; it sources a loader that
does. MEASURED on this deployment 2026-09-06 (alpha worker, cc-08): 30 scripts
source the loader, and a naive "does this file mention the credential file"
grep finds 5 of them. The other 25 -- including every canonical credential
wrapper the framework documents -- are invisible to it. A direct-read predicate
would therefore miss ~83% of the population, which is why the hop is mandatory
rather than a refinement.

AND WHY THE SOURCE LINE IS MATCHED BY LOADER NAME, NOT BY PARSING ITS PATH.
The dominant idiom is `source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"`. A
path-parsing extractor that stops at the first whitespace sees `"$(dirname` and
silently drops it. Measured the same day: parsing the path found 17 sourcers,
matching the NAME found 30 -- a 43% undercount that omitted exactly the
canonical wrappers. Never reintroduce a structural path parse here.

EVERY NUMBER IN THIS FILE IS POINT-IN-TIME. RE-DERIVE, DO NOT TRUST.
The first draft of this docstring said 27 / 3 / 24 / ~89% / 14 / 48%, and the
blast-radius comment below said 33 (3.5%) and 587 (63%). All of them were wrong
within THREE HOURS -- not because anything regressed, but because the tree grew:
nine peer commits merged mid-session, plus this module's own new files. The
drift is not even in a consistent direction (the sourcer counts rose while the
no-exception blast radius FELL, 587 -> 554), so a reader cannot correct for it
by intuition. A count is the fastest-decaying part of a document and the part
that reads as most authoritative. Re-measure with `script_reaches_loader` over
`core/scripts/**/*.sh` plus `$WORLD_PATH/scripts/**/*.sh` before relying on any
figure here.
"""

import os
import re

# ---------------------------------------------------------------- xtrace side

# Shell invocations that can carry a trace flag. Anchored on word boundaries so
# `pushd`/`flash`/`bashful` cannot match.
_SHELL_WORD = r"(?:ba|z|k|a|da)?sh"

# `bash -x`, `sh -ex`, `bash -eux`, `/bin/bash -x`. The flag cluster must be a
# single dash followed by short flags including x. Deliberately NOT a bare `-x`
# anywhere: `tar -x`, `grep -x`, `chmod -x`, `test -x` and `unzip -x` are all
# common, harmless, and would make this gate a nuisance that gets switched off.
_SHELL_DASH_X = re.compile(
    r"(?:^|[\s;&|(])(?:[\w/.-]*/)?" + _SHELL_WORD + r"\b[^\n;&|]*?\s-[a-wyzA-WYZ]*x[a-wyzA-WYZ]*\b"
)

# `bash -o xtrace` / `sh --debug`-style long forms.
_SHELL_LONG_X = re.compile(
    r"(?:^|[\s;&|(])(?:[\w/.-]*/)?" + _SHELL_WORD + r"\b[^\n;&|]*?\s-o\s+xtrace\b"
)

# `set -x`, `set -ex`, `set -o xtrace`. `set +x` DISABLES tracing and must never
# match -- the +/- distinction is the whole difference between the hazard and
# the remedy, and several credential scripts carry `set +x` defensively.
_SET_X = re.compile(r"(?:^|[\s;&|(])set\s+-[a-wyzA-WYZ]*x[a-wyzA-WYZ]*\b")
_SET_O_X = re.compile(r"(?:^|[\s;&|(])set\s+-o\s+xtrace\b")

# Environment-level enablers. SHELLOPTS is read-only in bash but is honoured when
# exported into a child shell, and BASH_XTRACEFD redirects the trace stream --
# both reach the same leak by another door.
_ENV_ANCHOR = r"(?:^|[\s;&|(])(?:export\s+)?"
# ANCHORED LIKE ITS FOUR SIBLINGS, and it was not until 2026-09-06.
# Every other pattern here requires command position; this one required only a
# word boundary, so it matched the assignment inside ANY quoted string. Measured
# the moment the gate went live: it refused the command writing this module's own
# test file, whose only xtrace token was `SHELLOPTS=xtrace` inside a Python string
# literal. Anchoring loses no real invocation -- `VAR=x cmd`, `export VAR=x` and
# a line-leading assignment all sit at command position by construction.
_ENV_X = re.compile(
    _ENV_ANCHOR + r"SHELLOPTS=[^\s;&|]*xtrace"
    r"|" + _ENV_ANCHOR + r"BASH_XTRACEFD=")

_XTRACE_MATCHERS = (_SHELL_DASH_X, _SHELL_LONG_X, _SET_X, _SET_O_X, _ENV_X)


def has_xtrace(command):
    """True when `command` enables shell tracing by any of the known doors."""
    if not command or not isinstance(command, str):
        return False
    return any(m.search(command) for m in _XTRACE_MATCHERS)


# ------------------------------------------------------------ credential side

# Scripts whose whole job is to put secrets into the environment, or which read
# the credential file directly. A script REACHES this set if it sources one of
# them, directly or through a chain.
#
# `_env.sh` is the measured loader on this deployment: it exports every variable
# from the credential file into the environment, so a trace of anything that
# sourced it prints all of them. The credential FILE is included so a script
# that reads it without a loader is caught too.
LOADER_NAMES = ("_env.sh",)
CREDENTIAL_FILES = (".env.local", ".env.secret")

# A script that sources a credential FILE directly, rather than through the
# loader, is the same hazard by another door -- measured 2026-09-06: two do
# (one of them with `set -a`, which auto-exports every variable it reads).
#
# PATH-CONFIG EXCEPTION, and it is the single most consequential line in this
# module. `_paths.sh` sources `"$_MD_DIR/.env.local"` -- a PATH-CONFIG file in
# the .mind-data tier holding WORLD_PATH / META_PATH, not credentials -- and 497
# of 930 scripts in the two trees source `_paths.sh`. Counting it as a loader
# takes the blocked population from 36 (3.9%) to 554 (60%): a gate that refuses
# xtrace on essentially every script in the repo, which is a gate that gets
# switched off within a day (guard-1426: separate blocking scope from reporting
# scope on a codebase that already contains the pattern).
#
# DO NOT "FIX" THIS BY READING THE REDIRECT. The exception is NOT justified by
# `_paths.sh` sourcing inside `$( ... >/dev/null 2>&1 )`. That looks protective
# and is not: measured directly on 2026-09-06 with a sentinel value, a plain
# `. file` under `bash -x` and the redirected-subshell form BOTH leaked the
# value, 2 occurrences each. The redirect suppresses nothing. The exception
# rests only on WHAT that file holds (paths, not secrets) and on the blast
# radius above -- so if the .mind-data tier ever carries a credential, this
# entry must go, and the 63% becomes the price of correctness.
PATH_CONFIG_EXCEPTIONS = ("_paths.sh",)

# Explicit escape hatch: the CONSTRUCT `XTRACE_CREDENTIAL_GATE_OVERRIDE="<reason>"`
# (quoted, non-empty) at a shell-word boundary. NOT a bare token anywhere in the
# command -- that read a MENTION as an invocation and disarmed the gate
# (). Deliberate and auditable, never a silent fail-open.
OVERRIDE_TOKEN = "XTRACE_CREDENTIAL_GATE_OVERRIDE"

# : an override must be a CONSTRUCT the author had to build, never a
# NAME they could type in passing. `OVERRIDE_TOKEN in command` matched a trailing
# comment, a quoted string, or a heredoc of documentation, so the artifact most
# likely to name the bypass -- the runbook explaining it -- silently disarmed the
# gate. Shape adopted from the sibling that already does it right
# (marker-placement-gate.py:92): token + `=` + a NON-EMPTY quoted justification,
# which is also what makes the override auditable.
#
# The prefix is a shell-word boundary, NOT `^` and NOT a line anchor. rb-9764
# measured the cost of anchoring a command predicate at a command START: the
# fleet's dominant shape is `cd ... && VAR=v bash core/scripts/x.sh`, so a
# start-anchored test silently refuses every real invocation -- trading a silent
# bypass for a silently-unusable escape hatch, which is harder to notice because
# the gate then looks MORE protective than it is. `re.M` is likewise absent on
# purpose (guard-5706): with re.M, `^` matches EVERY line start, including lines
# inside a documentation heredoc, which would re-open the exact hole.
# The boundary also stops `FOO_XTRACE_CREDENTIAL_GATE_OVERRIDE="x"` from matching, which the sibling
# (reading FILE content, not a command line) does not need.
_OVERRIDE_RE = re.compile(
    r'(?:^|[\s;&|(])' + re.escape(OVERRIDE_TOKEN) + r'=(["\'])([^"\']+)\1'
)


def override_invoked(command) -> bool:
    """True only when the command INVOKES the override, not when it names it.

    Requires `XTRACE_CREDENTIAL_GATE_OVERRIDE="<non-empty reason>"` (single or double quotes) preceded by
    a shell-word boundary. A mention -- `# do not reach for XTRACE_CREDENTIAL_GATE_OVERRIDE here` -- has
    no `=` and is correctly refused. Measured residual, recorded rather than
    papered over: a line that writes the whole construct as an EXAMPLE
    (`# use XTRACE_CREDENTIAL_GATE_OVERRIDE="reason" to bypass`) still suppresses. Narrowing that
    further needs comment-state parsing of an arbitrary shell command, which is
    more fragile than the hole it would close.
    """
    if not isinstance(command, str):
        return False
    return bool(_OVERRIDE_RE.search(command))


# A `source`/`.` line that names a loader ANYWHERE on it. See the module
# docstring: matching the name rather than parsing the path is load-bearing.
_SOURCE_LINE = re.compile(r"^[^\n#]*?(?:^|[\s;&|(])(?:source|\.)\s+[^\n]*", re.M)

# Script tokens in a command: anything ending in .sh, quoted or not.
_SCRIPT_TOKEN = re.compile(r"[\w./$~{}\"'-]*?[\w.-]+\.sh")

# Depth cap for the source walk. Measured depth on this deployment is 1; the cap
# exists so a cycle or a pathological chain cannot stall a PreToolUse hook.
MAX_SOURCE_DEPTH = 4


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _names_loader(text, filename=""):
    """True when a source/. line in `text` names a loader or a credential file.

    Both checks are confined to SOURCE LINES, never the whole file. A bare
    substring scan over the text flags every script that merely MENTIONS the
    credential path -- an existence check, a scrub, a gitignore entry -- and
    measured 2026-09-06 that is 26 scripts against 2 real ones. It also matches
    the explanatory comment as readily as the code (guard-1099), which added 4
    more. `_SOURCE_LINE` already excludes comment lines.
    """
    if os.path.basename(filename) in PATH_CONFIG_EXCEPTIONS:
        return False
    for line in _SOURCE_LINE.findall(text):
        if any(name in line for name in LOADER_NAMES):
            return True
        if any(cf in line for cf in CREDENTIAL_FILES):
            return True
    return False


def _sourced_paths(text, base_dir):
    """Resolve the source targets of `text` that we can actually locate on disk.

    Best-effort BY DESIGN: an unresolvable `$(...)`-built path yields nothing
    here, and that is safe because the loader NAME check above already fired for
    the common idiom. This walk only has to catch a chain one hop longer.
    """
    out = []
    for line in _SOURCE_LINE.findall(text):
        for tok in _SCRIPT_TOKEN.findall(line):
            cand = tok.strip("\"'")
            if "$" in cand or "{" in cand:
                cand = os.path.basename(cand)
            if not cand:
                continue
            for probe in (
                os.path.join(base_dir, cand),
                os.path.join(base_dir, os.path.basename(cand)),
            ):
                if os.path.isfile(probe):
                    out.append(os.path.realpath(probe))
                    break
    return out


def script_reaches_loader(path, _depth=0, _seen=None):
    """True when `path` sources a credential loader, directly or transitively."""
    if _depth > MAX_SOURCE_DEPTH:
        return False
    real = os.path.realpath(path)
    _seen = _seen if _seen is not None else set()
    if real in _seen:
        return False
    _seen.add(real)
    text = _read(real)
    if text is None:
        return False
    if os.path.basename(real) in LOADER_NAMES:
        return True
    if _names_loader(text, real):
        return True
    base_dir = os.path.dirname(real)
    for child in _sourced_paths(text, base_dir):
        if script_reaches_loader(child, _depth + 1, _seen):
            return True
    return False


def _candidate_paths(token, project_root, search_dirs):
    cand = token.strip("\"'")
    if "$" in cand or "{" in cand:
        cand = os.path.basename(cand)
    if not cand:
        return []
    probes = [cand]
    if not os.path.isabs(cand):
        probes.append(os.path.join(project_root, cand))
        for d in search_dirs:
            probes.append(os.path.join(d, os.path.basename(cand)))
    return probes


def credential_scripts_in(command, project_root=".", search_dirs=()):
    """Return the scripts named in `command` that reach a credential loader.

    Order-preserving and de-duplicated, so the deny message can name them in the
    order the author wrote them.
    """
    if not command or not isinstance(command, str):
        return []
    hits, seen = [], set()
    for token in _SCRIPT_TOKEN.findall(command):
        for probe in _candidate_paths(token, project_root, search_dirs):
            if not os.path.isfile(probe):
                continue
            real = os.path.realpath(probe)
            if real in seen:
                break
            if script_reaches_loader(real):
                seen.add(real)
                hits.append(real)
            break
    return hits


_HEREDOC_OPEN = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_INTERPRETER_ON_LINE = re.compile(r"(?:^|[\s;&|(])(?:[\w/.-]*/)?" + _SHELL_WORD + r"\b")


def strip_heredoc_bodies(command):
    """Drop heredoc bodies that are being WRITTEN OUT rather than executed.

    A heredoc body headed for a file is DATA. Writing a test, a guardrail or a
    convention that quotes an offending shape as an EXAMPLE is not running it,
    and a gate that refuses its own documentation cannot coexist with its own
    maintenance -- it gets overridden as a reflex, and a reflex override is
    indistinguishable from no gate at all. Measured here: the command that
    authored this module's test file was refused.

    The body is KEPT whenever the opening line names a shell (`bash <<EOF`,
    `cat <<EOF | bash`), because there the body IS the command. That closes the
    obvious evasion structurally, so the exemption never rests on an argument
    about who would bother to use it.
    """
    out, lines, i = [], command.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = _HEREDOC_OPEN.search(line)
        i += 1
        if not m:
            continue
        delim, executed = m.group(2), bool(_INTERPRETER_ON_LINE.search(line))
        while i < len(lines) and lines[i].strip() != delim:
            if executed:
                out.append(lines[i])
            i += 1
        if i < len(lines):
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def offending(command, project_root=".", search_dirs=()):
    """The gate's whole question, in one call.

    Returns the list of credential-reaching scripts this command would trace --
    empty when the command is safe, when tracing is absent, or when the override
    is INVOKED as `XTRACE_CREDENTIAL_GATE_OVERRIDE="<reason>"` (a bare mention of
    the token does NOT suppress -- g-115-10247). Both conditions are required: xtrace alone is fine, and
    running a credential script without tracing is the normal case.
    """
    if not command or not isinstance(command, str):
        return []
    # Strip heredoc BODIES before the override test, not after (fresh-eyes,
    # ). A heredoc body is DATA the command writes, not command text:
    # a runbook documenting the correct invocation form would otherwise disarm
    # the gate for the very command that writes it -- the self-inflicted-by-
    # documentation shape this goal was filed on, narrowed by the construct
    # requirement but not closed by it. Measured: `cat > runbook.md <<'EOF' ...
    # TOKEN="reason" ... EOF` + a real traced credential script returned [].
    scanned = strip_heredoc_bodies(command)
    if override_invoked(scanned):
        return []
    if not has_xtrace(scanned):
        return []
    return credential_scripts_in(scanned, project_root, search_dirs)
