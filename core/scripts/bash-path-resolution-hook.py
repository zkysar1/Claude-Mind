#!/usr/bin/env python3
"""PreToolUse[Bash] hook — L1 path-resolution enforcement (Bash sibling).

Closes the gap where Write/Edit/MultiEdit are checked by
path-resolution-hook.py but Bash mkdir/heredoc/redirect/cp/mv/touch/tee
operations creating NEW top-level entries under governed roots are not.

Origin: 2026-05-21. The rotate-lambda-common-pat.sh script landed at
`agents/alpha/scripts/` (new top-level under bound agent dir) via a Bash
heredoc, which path-resolution-hook.py doesn't see (it only fires on
Write/Edit/MultiEdit).

Scope (narrow on purpose to minimize false positives):

  1. Absolute paths anywhere in the bash command, hitting is_new_toplevel
     under WORLD_PATH / META_PATH / bound-agent-dir.
  2. Relative paths of the shape `agents/<bound-agent>/<NEW_SEGMENT>/...`
     (treated as relative-to-PROJECT_ROOT — the dominant Bash-cwd case).
  3. Absolute write targets OUTSIDE every allowed root (g-115-3338) — the
     tool-surface parity branch, without which the same write was refused on
     the Edit surface and approved here minutes later. Both branches DENY.

Both refusals key on WRITE INTENT, never on the mere presence of a path: an
Edit is a write by definition, while a shell command's write intent is
INFERRED, so it can be inferred wrongly. Three mechanisms bound that
inference — heredoc bodies and quoted payload spans are stripped as DATA
before extraction, and verbs/redirect operators are matched only OUTSIDE
quoted spans, with a bounded `bash -c "..."` allowlist that descends into a
quoted command that really is executed locally (g-115-3349). The branch-3
deny was demoted to a stderr advisory for one iteration while those FP classes
were open; it was restored only after a full-corpus replay measured BOTH
directions (see the comment at the branch itself, and
core/scripts/bash-hook-corpus-replay.py).

OUT of scope (intentional — too false-positive-prone):

  - `cd /other-path && mkdir foo` — cwd changes that move the relative
    base outside PROJECT_ROOT. Caught only via absolute-path detection.
  - Random relative paths that don't match the `agents/...`/`world/...`/
    `meta/...` prefix shape.
  - Process substitution (`>(...)`) and `<(...)` constructs.
  - Path components inside variable expansions (`$DIR/foo`).

SAFETY: fail open on ANY error. Empty stdout + exit 0 = approve. Same
contract as path-resolution-hook.py.

# domain-leak-exempt: this hook inspects bash command text — the regex
# tokens it uses (mkdir/touch/cp/mv/tee/heredoc patterns) are the hook's
# detection contract, not accidental domain bleed.
"""

import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_helpers import (  # noqa: E402
    approve_no_mutation,
    emit_advisory,
    emit_deny,
    stdin_json_or_approve,
)
# Root resolution is SHARED with path-resolution-hook.py (g-115-3338) so the
# two surfaces cannot drift. These five were duplicated verbatim in both hooks
# — verified semantically identical before consolidating.
from _path_roots import (  # noqa: E402
    compute_allowed_roots,
    is_new_toplevel,
    is_under,
    is_write_exempt_sink,
    norm_path,
    read_paths_conf,
)

# Sync with path-resolution-hook.py / _paths.py.
AGENTS_PARENT_DIR = "agents"
SESSIONS_DIRNAME = "sessions"


def _resolve_agent_dir(project_root, agent):
    if AGENTS_PARENT_DIR:
        return os.path.join(project_root, AGENTS_PARENT_DIR, agent)
    return os.path.join(project_root, agent)


# Regex catalog of write-creating bash constructs. Each pattern extracts
# the destination path as group 1.
#
# Notes:
#   - `mkdir -p? <path>` — first non-flag arg is the dir to create.
#   - `touch <path>` — first arg.
#   - `cp [-flags] <src> <dst>` — last arg is the destination.
#   - `mv [-flags] <src> <dst>` — last arg is the destination.
#   - `> <path>` or `>> <path>` — file redirect. Excluded: `2>`, `&>`,
#     `>(...)` (process substitution), `2>&1`. Patterns require whitespace
#     before > to avoid matching `1>`/`2>`.
#   - `tee [-a] <path>` — destination.
#
# Path-token character class. Includes:
#   . / - _ ~ alphanumeric    — normal POSIX path components
#   :                         — Windows drive prefix (C:/…) and rare colon-bearing names
# Excludes:
#   $   — variable expansion is out of scope (would over-capture)
#   <   — would catch heredoc delimiters inadvertently
#   \\  — backslash-quoted spaces; rare and Windows-translation lossy
PATH_CHARS = r'[A-Za-z0-9_.:/~\-]'

# Flag-token pattern. Handles three shapes:
#   1. Short flags: -p, -pv, -laR
#   2. Long flags: --parents, --no-clobber, --preserve=mode
#   3. End-of-flags marker: bare --
#
# Earlier versions matched only `-[a-zA-Z]+` which let `mkdir --parents <path>`
# slip past the hook entirely — the engine would greedily consume `--parents`
# as the path token via PATH_CHARS (which includes `-`), then fail the
# governed-root check on the non-path string and approve the write. Likewise
# `mkdir -- <path>` (end-of-flags) bypassed because `--` was consumed as the
# path token. Both confirmed during fresh-eyes review 2026-05-21.
FLAG_TOKENS = r'(?:--?[a-zA-Z][a-zA-Z0-9=._-]*\s+|--\s+)*'

# cp/mv use the LAST arg (destination); redirect uses the only arg after >.
# mkdir/touch/tee take MULTIPLE positional args, each a target — handled
# separately in extract_targets so we don't miss `mkdir EXISTING NEW`
# (H12, fresh-eyes review 2026-05-21). The previous single-arg regex captured
# only the FIRST positional arg, leaving downstream args unchecked: cruft
# created via the second arg satisfied is_new_toplevel=False on later writes
# (parent already existed) and the Write hook approved follow-on file
# operations into the bypass-created dir.
PATTERNS_LAST_ARG = [
    # cp [-flags] <src> ... <dst>  — destination is the LAST arg
    # Use a non-greedy match between cp and the final path before EOL/operator.
    # This is approximate; for richer parsing we'd need a real shell tokenizer.
    (r'\bcp\s+' + FLAG_TOKENS + r'(?:' + PATH_CHARS + r'+\s+)+(["\']?)(' + PATH_CHARS + r'+)\1(?=\s|[;&|]|$)', 2),
    # mv [-flags] <src> <dst>
    (r'\bmv\s+' + FLAG_TOKENS + r'(?:' + PATH_CHARS + r'+\s+)+(["\']?)(' + PATH_CHARS + r'+)\1(?=\s|[;&|]|$)', 2),
    # install [-flags] <src> ... <dst> — same last-arg destination shape as cp/mv
    # (g-115-3345). `install -m 644 src dst` parses because FLAG_TOKENS eats
    # `-m ` and the mode `644` is then absorbed by the src repetition group.
    (r'\binstall\s+' + FLAG_TOKENS + r'(?:' + PATH_CHARS + r'+\s+)+(["\']?)(' + PATH_CHARS + r'+)\1(?=\s|[;&|]|$)', 2),
    # dd ... of=<path> — the destination is NOT positional, so no last-arg
    # pattern can reach it (g-115-3345). Bounded to one command segment so a
    # later `of=` on the far side of a `;` cannot be attributed to this dd.
    (r'\bdd\s+[^;&|\n]*?\bof=(["\']?)(' + PATH_CHARS + r'+)\1',                 2),
    # >  <path> or >> <path> or >| <path>   (file redirect, leading whitespace
    # required so 1>/2>/&> don't match). `>|` is bash's force-clobber form and
    # is a plain write — the `\|?` is the whole fix for it (g-115-3345).
    (r'(?<=\s)>>?\|?\s*(["\']?)(' + PATH_CHARS + r'+)\1',                       2),
]

# Multi-arg verbs — every positional arg after the flag block is a target.
# Match each verb instance and let the caller iterate over the trailing
# token stream (split on whitespace) so `mkdir A B C` checks A, B, and C.
MULTI_ARG_VERBS = ('mkdir', 'touch', 'tee')
_MULTI_ARG_TAIL_RE = {
    verb: re.compile(r'\b' + verb + r'\s+' + FLAG_TOKENS + r'(.+?)(?=$|[;&|\n])')
    for verb in MULTI_ARG_VERBS
}
_PATH_TOKEN_RE = re.compile(r'^' + PATH_CHARS + r'+$')

# Heredoc opener: `<<DELIM`, `<<-DELIM`, `<<'DELIM'`, `<<"DELIM"`. Group 1 is
# the delimiter with quotes stripped by the alternation.
# `(?<!<)` + `(?!<)` exclude the HERESTRING `<<<`. Without them the regex still
# matches at the SECOND `<` of `<<<` (consuming chars 2-3), so `cat <<< "hello"`
# read as a heredoc opened by the delimiter `hello`. Found by fresh-eyes probe
# F-2 (g-115-3338); see the terminator-exists guard in strip_heredoc_bodies for
# the other half of the same defect class.
_HEREDOC_OPEN_RE = re.compile(
    r'(?<!<)<<-?(?!<)\s*(?:"([^"]+)"|\'([^\']+)\'|([A-Za-z_][A-Za-z0-9_]*))')


def strip_heredoc_bodies(cmd):
    """Return `cmd` with every heredoc BODY removed, opener lines preserved.

    The extractor's regexes are written for COMMAND text. A heredoc body is
    DATA — JSON payloads piped into `*-add.sh`, inline Python source, prose in
    a reasoning-bank entry. Scanning it produces phantom targets: measured
    2026-07-26 over 11,554 real Bash calls (g-115-3338), EVERY out-of-root
    candidate that survived the allowlist came from a heredoc body, e.g. an
    rb-entry's prose yielding ('touch', '/') — a refusal keyed on the
    filesystem root.

    The opener LINE is kept, because the real sink lives there
    (`cat > /path/f <<'EOF'`); only the lines after it, up to the terminator,
    are dropped. This is the shell-command instance of the two-layer prose
    filter in the `prose-filter-pattern` tree node (rb-349: fix at source,
    not by loosening the regex).

    Fail-open: any error returns the original command unchanged.
    """
    try:
        if '<<' not in cmd:
            return cmd
        lines = cmd.split('\n')
        # TERMINATOR-EXISTS GUARD (fresh-eyes probe F-1, g-115-3338). `<<` is
        # also bash/python/C LEFT-SHIFT, and `1 << n` matches the opener regex
        # with delimiter `n`. Before this guard, `py -3 -c 'x = 1 << n'` on line
        # 1 silently swallowed EVERY later line — so a following
        # `mkdir -p /opt/not-a-root/x` extracted no target and sailed past the
        # refusal (measured: targets [] vs [('mkdir', ...)] for the identical
        # command without the shift). A real heredoc ALWAYS has its delimiter on
        # a line of its own; a left-shift operand essentially never does. So a
        # candidate delimiter is honored only when such a line exists. This is
        # the false-NEGATIVE direction rb-401 mandates re-checking when a
        # static-scan regex is tightened — the same bypass class as the four
        # 2026-05-21 fresh-eyes findings this hook already guards.
        standalone = {ln.strip() for ln in lines}
        out = []
        pending = []          # delimiters opened on the current line
        terminator = None
        for line in lines:
            if terminator is not None:
                if line.strip() == terminator:
                    terminator = pending.pop(0) if pending else None
                continue
            out.append(line)
            found = [m.group(1) or m.group(2) or m.group(3)
                     for m in _HEREDOC_OPEN_RE.finditer(line)
                     if (m.group(1) or m.group(2) or m.group(3)) in standalone]
            if found:
                terminator = found[0]
                pending = found[1:]
        return '\n'.join(out)
    except Exception:
        return cmd


# A quoted span that is an argument to a payload-emitting verb. `echo`/`printf`
# never write a file themselves — their redirect target sits OUTSIDE the quotes
# — so the span's CONTENTS are always data, never a write target.
_PAYLOAD_SPAN_RE = re.compile(
    r"\b(?:echo|printf)\s+(?:-[a-zA-Z]+\s+)*(?:'([^']*)'|\"([^\"]*)\")")


def strip_payload_spans(cmd):
    """Blank the CONTENTS of quoted payloads passed to `echo`/`printf`.

    Sibling of strip_heredoc_bodies for the single-line case. The dominant
    framework idiom is `echo '<json>' | some-script.sh` — board posts,
    goal records, reasoning-bank entries. When that JSON happens to quote a
    path (`"... write to /opt/elsewhere/x ..."`) the extractor reads it as a
    redirect target and the out-of-root branch refuses an ordinary write of a
    DATA payload.

    Not hypothetical and not caught by the corpus: across 16,830 historical
    Bash calls this fired zero times, because none of them were commands ABOUT
    out-of-root paths — then it fired on the FIRST live wrapper probe of this
    very feature (g-115-3338). A corpus can only bound the false-positive
    classes it happens to contain; a live probe found the one it did not.

    Narrow on purpose: only `echo`/`printf` arguments are blanked, so a quoted
    redirect target (`echo hi > '/opt/GitHub/Ayoai/f.txt'`) is untouched — it
    sits outside the span. Fail-open: any error returns the command unchanged.
    """
    try:
        if "'" not in cmd and '"' not in cmd:
            return cmd

        def _blank(m):
            # Blank by SPAN OFFSET, not str.replace: the payload's first
            # occurrence inside the matched text can be in the VERB, not the
            # quotes. `echo 'o'` blanked the 'o' of "echo" and left the payload
            # intact ("ech  'o'"); `printf 'p'` corrupted "printf". Harmless
            # today (a 1-2 char payload is never a path token) but wrong, and
            # the offsets are exact. Fresh-eyes probe F-3, g-115-3338.
            grp = 1 if m.group(1) is not None else 2
            start, end = m.span(grp)
            if start < 0 or end <= start:
                return m.group(0)
            off = m.start()
            text = m.group(0)
            return text[:start - off] + " " * (end - start) + text[end - off:]

        return _PAYLOAD_SPAN_RE.sub(_blank, cmd)
    except Exception:
        return cmd


_QUOTED_SPAN_RE = re.compile(
    r"""'[^']*'"""   # single-quoted span
    r'|"[^"]*"'      # double-quoted span
)

# Verbs that EXECUTE their quoted argument on THIS machine. Their argument is
# command text, so it must be rescanned rather than skipped. Everything else
# receiving a quoted argument (remote-exec wrappers like efs-ssh.sh/ssh, and
# ordinary tools taking prose like `git commit -m` or `--summary "..."`) is
# either not-local or not-executed, so its quoted span is DATA. (g-115-3349)
# BOTH halves of this pattern are load-bearing, and a looser first draft
# re-enabled the very FP class this allowlist exists beside (found by the
# same-iteration fresh-eyes review of g-115-3349):
#   * the shell name must be its OWN token, not a filename suffix. `\bsh\b`
#     matches the `sh` in `efs-ssh.sh` (the `.` before it IS a word boundary),
#     so a remote-exec wrapper re-entered the allowlist.
#   * the flag must be a genuine SHORT option cluster containing `c` (-c, -lc,
#     -ec). `-[a-z]*c[a-z]*` alone also matches LONG flags — `--exec`,
#     `--check`, `--cached`, `--category` — so `wrapper.sh --exec "..."` and
#     even `retrieve.sh --category "..."` were rescanned as local command text.
# Measured before the fix: 1,544 of 48,731 corpus calls (3.2%) matched without
# being a bare shell -c, and `retrieve.sh --category "...mkdir /opt/x..."` —
# the pre-apply consultation shape code-review-protocol.md step 4 MANDATES —
# was denied outright.
_LOCAL_EXEC_RE = re.compile(
    r"""(?<![\w.\-])(?:bash|sh|zsh|dash)"""   # shell as its own token, not `.sh`/`-ssh`
    r"""(?:\s+-[a-z]+)*?"""                   # optional preceding short-flag clusters
    r"""\s+-[a-z]*c[a-z]*"""                  # the -c cluster (single dash ONLY)
    r"""\s+("(?:[^"]*)"|'(?:[^']*)')"""       # its quoted argument
)


def quoted_spans(cmd):
    """Offsets of every quoted region: [(start, end), ...]. Never raises."""
    try:
        return [m.span() for m in _QUOTED_SPAN_RE.finditer(cmd)]
    except Exception:
        return []


def _in_span(pos, spans):
    return any(s <= pos < e for s, e in spans)


# `sed -i` target extraction (g-115-3345). Deliberately NOT a regex. The target
# is a positional arg that FOLLOWS an expression which routinely contains
# slashes, spaces, quotes and even `;`, so every regex candidate measured
# against the 60,507-call corpus leaked expression fragments as paths
# ('role-ish', 'timer/On', 'is/carries', and a bare '/'). shlex already knows
# where the quoting ends, which is the entire difficulty of this form.
_SED_SEP = {';', '&&', '||', '|', '&', ';;'}
_SED_IN_PLACE_RE = re.compile(r'^-(?=[a-zA-Z]*i)[a-zA-Z]*(?:\..*)?$|^--in-place')


def _sed_inplace_targets(cmd, spans):
    """Return [('sed-i', path), ...] for every `sed -i` write in cmd.

    Fails OPEN (returns []) on anything unparseable: a missed write is merely
    the pre-existing state, whereas an invented target is a false DENIAL of a
    command that was never doing anything wrong.
    """
    # A newline separates commands in bash, but only OUTSIDE a quoted span (an
    # expression may legally contain one). shlex treats '\n' as ordinary
    # whitespace, so without this substitution the walker runs past the sed
    # invocation into the following line and captures later commands as
    # "files" — measured on the corpus, that denied /usr/bin/grep.
    flat = ''.join(' ; ' if (ch == '\n' and not _in_span(i, spans)) else ch
                   for i, ch in enumerate(cmd))
    try:
        toks = shlex.split(flat, posix=True)
    except ValueError:
        return []
    out = []
    i = 0
    while i < len(toks):
        if toks[i] != 'sed':
            i += 1
            continue
        i += 1
        inplace = False
        expr_seen = False
        files = []
        while i < len(toks) and toks[i] not in _SED_SEP:
            tok = toks[i]
            if tok == '--':
                i += 1
                while i < len(toks) and toks[i] not in _SED_SEP:
                    files.append(toks[i])
                    i += 1
                break
            if tok.startswith('-') and len(tok) > 1:
                if _SED_IN_PLACE_RE.match(tok):
                    inplace = True
                if tok in ('-e', '-f', '--expression', '--file'):
                    expr_seen = True   # script supplied by flag; no bare script
                    i += 2
                    continue
                if tok.startswith('-e') or tok.startswith('-f'):
                    expr_seen = True
                    i += 1
                    continue
                i += 1
                continue
            if not expr_seen:
                expr_seen = True       # first bare arg IS the script, not a file
                i += 1
                continue
            files.append(tok)
            i += 1
        if inplace:
            for f in files:
                if f and f not in ('/', '//') and _PATH_TOKEN_RE.match(f):
                    out.append(('sed-i', f))
    return out


def extract_targets(cmd, _depth=0):
    """Return a list of (verb, path) tuples extracted from the bash command.

    SPAN RULE (g-115-3349). A redirect OPERATOR or command VERB only counts
    when its offset lies OUTSIDE every quoted span. This kills two measured
    false-positive classes that shared one shape — `<verb> "<text with a
    path>"` — where the quoted text is DATA, not command text:
      * remote-exec wrapper args  (`efs-ssh.sh "mkdir -p /home/ec2-user/..."`)
      * quoted prose on any verb  (`--summary "...>>/tee -a..."`,
                                    `git commit -m "...>>/tee -a..."`)
    The redirect TARGET may still be quoted (`echo hi > '/opt/x'`) because the
    OPERATOR sits outside the span — that case keeps working and is pinned.

    The rule alone would be a false-NEGATIVE bypass, because `bash -c "mkdir
    /x"` has the identical shape yet DOES run locally (rb-401: tightening a
    static scan demands re-checking the other direction). Measured: 5 such
    forms. So local-exec verbs are allowlisted and their quoted argument is
    RESCANNED. A remote-exec denylist was rejected by measurement — it cannot
    reach the prose class at all, since neither iteration-close.sh nor git is
    an exec wrapper.
    """
    cmd = strip_payload_spans(strip_heredoc_bodies(cmd))
    targets = []
    spans = quoted_spans(cmd)

    # Local-exec allowlist: rescan the quoted argument as command text. Bounded
    # recursion — a nested `bash -c "bash -c ..."` is legitimate but must not
    # loop; depth 2 covers observed shapes and terminates unconditionally.
    if _depth < 2:
        for m in _LOCAL_EXEC_RE.finditer(cmd):
            inner = m.group(1)[1:-1]
            if inner.strip():
                targets.extend(extract_targets(inner, _depth + 1))

    # Multi-arg verbs (mkdir / touch / tee): scan every positional token after the flags.
    for verb in MULTI_ARG_VERBS:
        for m in _MULTI_ARG_TAIL_RE[verb].finditer(cmd):
            if _in_span(m.start(), spans):
                continue
            tail = m.group(1)
            for tok in tail.split():
                tok = tok.strip('"\'')
                if not tok or tok.startswith('-'):
                    continue
                if not _PATH_TOKEN_RE.match(tok):
                    continue
                targets.append((verb, tok))
    # Single-target patterns (cp/mv last-arg, > redirect destination).
    for pattern, target_group in PATTERNS_LAST_ARG:
        for m in re.finditer(pattern, cmd):
            # Span rule: the OPERATOR/VERB position decides, not the target's.
            # `echo hi > '/opt/x'` keeps working (the `>` is outside the quote);
            # `--summary "...>>/tee..."` does not (the `>>` is inside one).
            if _in_span(m.start(), spans):
                continue
            verb_match = re.search(r'\b(cp|mv|install|dd)\b', m.group(0))
            verb = verb_match.group(1) if verb_match else "redirect"
            path = m.group(target_group)
            if path:
                targets.append((verb, path))

    # sed -i writes — a tokenizer walk, not a pattern; see _sed_inplace_targets.
    targets.extend(_sed_inplace_targets(cmd, spans))
    return targets


def main():
    data = stdin_json_or_approve()
    tool_name = data.get("tool_name", "") or ""
    if tool_name != "Bash":
        approve_no_mutation()

    tool_input = data.get("tool_input") or {}
    cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not cmd or not isinstance(cmd, str):
        approve_no_mutation()

    # Fast filter: skip commands that can't possibly create files.
    # MUST list every verb extract_targets can extract, or that verb's support
    # is unreachable in production while its unit-level extraction tests pass
    # (g-115-3345; guard-3448 — a gate is only as broad as its entry points).
    # `\s>` already admits the `>|` clobber form, so it needs no entry here.
    if not re.search(r'(mkdir|touch|tee|cp|mv|sed|install|\bdd\b|\s>)', cmd):
        approve_no_mutation()

    project_root = os.environ.get("PROJECT_ROOT", "")
    if not project_root:
        approve_no_mutation()
    pr_norm = norm_path(project_root)
    if not pr_norm:
        approve_no_mutation()

    # Resolve bound agent
    sid = data.get("session_id", "") or ""
    agent = os.environ.get("MIND_AGENT", "") or ""
    if not agent and sid:
        try:
            from _resolve_agent_from_sid import resolve as _resolve_agent
            agent = _resolve_agent(sid, Path(project_root)) or ""
        except Exception:
            agent = ""
    if not agent:
        approve_no_mutation()

    # Read paths conf for WORLD/META
    conf_path = os.path.join(_resolve_agent_dir(project_root, agent),
                              "local-paths.conf")
    conf_present = os.path.isfile(conf_path)
    paths = read_paths_conf(conf_path) if conf_present else {}

    # --- Stray repo-root world/|meta/ advisory (2026-08-28 incident) ---
    # A literal PROJECT_ROOT/world (or /meta) that is NOT the configured root
    # is a cruft location NO write-time layer can prevent when the file is
    # created by a script body (a `py -3 patch.py` builds its paths
    # internally, invisible to command-text scanning and to the Write/Edit
    # hook alike). Detection is the closure: this hook fires before every
    # write-shaped Bash call, so one isdir here surfaces the stray within a
    # tool call or two of its creation regardless of the lane that made it.
    # Measured cost of the gap: the asp-370 SDLC charter sat at repo-root
    # world/ for ~7h, foundationally blocking the aspiration fleet-wide,
    # while every local read of it succeeded (own-cloud reads never consult
    # S3 for a path outside the cache). Advisory, not deny — the current
    # command is usually unrelated; the message names the migration. It
    # deliberately repeats on every matching call until the stray is gone.
    # Skips: a box whose conf legitimately points WORLD/META at the repo-root
    # dir (compared against the configured root), and conf-absent sessions
    # (cannot know the configured root — stay silent, fail-open).
    if conf_present:
        try:
            for _label, _sub in (("WORLD_PATH", "world"), ("META_PATH", "meta")):
                _stray = os.path.join(project_root, _sub)
                _configured = norm_path(paths.get(_label) or "")
                if os.path.isdir(_stray) and norm_path(_stray) != _configured:
                    emit_advisory(
                        f"[stray-root-advisory] A literal '{_sub}/' directory "
                        f"exists at the repo root:\n  {_stray}\n"
                        f"It is NOT the {_label} — the configured root is:\n"
                        f"  {paths.get(_label) or '(unset)'}\n"
                        f"Anything in it is invisible to the authoritative "
                        f"store, the fleet, and git (2026-08-28: the asp-370 "
                        f"SDLC charter sat there ~7h, blocking the aspiration "
                        f"fleet-wide while local reads kept succeeding).\n"
                        f"Fix NOW: migrate contents to the configured root "
                        f"(own-cloud: fenced storage_backend.mirror_put, or "
                        f"the relevant store script), verify with "
                        f"read_authoritative_bytes, then remove the stray "
                        f"directory. This advisory repeats until it is gone."
                    )
        except SystemExit:
            raise
        except Exception:
            pass

    # Allowed roots — the SAME list path-resolution-hook.py builds, from the
    # SAME helper, so the two tool surfaces cannot answer "is this path in
    # bounds?" differently. Includes each AGENT_WRITE_PATH entry (multi-root,
    # ';'-separated), which is what keeps product-repo writes usable here.
    allowed_roots = compute_allowed_roots(project_root, paths)

    agent_dir_norm = norm_path(_resolve_agent_dir(project_root, agent))
    wp_norm = norm_path(paths.get("WORLD_PATH") or "")
    mp_norm = norm_path(paths.get("META_PATH") or "")

    # Build governed-root list (label, normalized_path)
    governed = []
    if agent_dir_norm:
        governed.append(("bound agent dir", agent_dir_norm))
    if wp_norm:
        governed.append(("WORLD_PATH", wp_norm))
    if mp_norm:
        governed.append(("META_PATH", mp_norm))
    if not governed:
        approve_no_mutation()

    # Extract candidate targets
    targets = extract_targets(cmd)
    if not targets:
        approve_no_mutation()

    # Check each target
    for verb, raw_path in targets:
        # Resolve to absolute candidate path.
        # The Windows drive-prefix test REQUIRES an alphabetic first character.
        # A bare `raw_path[1] == ":"` also matched ordinary Python/shell
        # comparisons that leak in as redirect captures — `if count > 0:`
        # yields the token `0:`, which then read as an absolute drive path.
        # Measured 2026-07-26 (g-115-3338): 3 of 4 surviving out-of-root
        # candidates over 11,554 real Bash calls were this exact `0:` shape.
        # Harmless while the only check is is_new_toplevel (a bogus drive is
        # under no governed root), but it becomes a live refusal the moment an
        # out-of-root branch is added — the direction rb-401 warns to re-check
        # when tightening a static-scan regex.
        if os.path.isabs(raw_path) or (
            len(raw_path) >= 2 and raw_path[1] == ":" and raw_path[0].isalpha()
        ):
            abs_path = raw_path
        elif raw_path.startswith("/"):
            abs_path = raw_path
        else:
            # Relative — treat as relative to PROJECT_ROOT (the dominant Bash cwd).
            # Only consider relative paths that START with `agents/`, `world/`,
            # or `meta/` — these are the shapes that could land cruft in
            # governed roots. Everything else: skip (false-positive risk).
            lead = raw_path.split("/", 1)[0] if "/" in raw_path else raw_path
            if lead not in ("agents", "world", "meta"):
                continue
            abs_path = os.path.join(project_root, raw_path)

        target_norm = norm_path(abs_path)
        if not target_norm:
            continue

        for label, root in governed:
            if is_under(target_norm, root) and is_new_toplevel(target_norm, root):
                # Compute the offending new top-level for the deny message
                rel = target_norm[len(root) + 1:]
                new_seg = rel.split("/", 1)[0] if "/" in rel else rel
                reason = (
                    f"Path-resolution hook (L1-Bash) blocked Bash command:\n"
                    f"  verb: {verb}\n"
                    f"  target: {raw_path}\n"
                    f"This would create a new top-level entry under "
                    f"{label} ({root}):\n"
                    f"  new top-level: {new_seg}/\n\n"
                    f"New top-level entries under governed roots require "
                    f"explicit approval. The Write/Edit hook (path-resolution-"
                    f"hook.py) blocks this for Write tool calls; this is the "
                    f"Bash sibling closing the same gap.\n\n"
                    f"Options:\n"
                    f"  (a) Place under an EXISTING top-level dir within the "
                    f"governed root.\n"
                    f"  (b) Ask the user where the new entry should live; once "
                    f"the dir exists this command will succeed on retry.\n"
                    f"  (c) For ephemeral files, use "
                    f"{agent}/session/ (cross-session) or "
                    f"agents/{agent}/sessions/<bound-SID>/ (per-session "
                    f"scratch — L1-sanctioned).\n"
                    f"See: .claude/rules/path-resolution.md "
                    f"\"L1 Cruft Prevention\" section."
                )
                emit_deny(reason)

        # --- Block: write target is absolute AND outside all allowed roots ---
        # Tool-surface parity with path-resolution-hook.py's closing branch
        # (g-115-3338). Without this, the identical write was refused on the
        # Edit surface and approved on the Bash surface minutes later.
        #
        # Scoped to WRITE INTENT only. An Edit is a write by definition, so a
        # path-shaped refusal there has no false-positive surface; shell
        # commands are mostly READS, so a refusal keyed on the mere presence
        # of an outside path would refuse nearly all of them. `targets` is
        # already the write-intent set (redirect / tee / cp / mv destinations,
        # mkdir / touch operands, and heredoc sinks via the opener-line
        # redirect), so the refusal applies to that set alone.
        #
        # Conf-missing fail-open, mirroring the Edit side: without
        # local-paths.conf only PROJECT_ROOT is known, and an external target
        # cannot be validated against unknown WORLD/META roots.
        if not conf_present or not allowed_roots:
            continue
        if not os.path.isabs(target_norm) and not (
            len(target_norm) >= 2 and target_norm[1] == ":"
        ):
            continue
        if is_write_exempt_sink(target_norm):
            continue
        if any(is_under(target_norm, root) for _, root in allowed_roots):
            continue
        # DENY — restored by g-115-3349 after the two false-positive classes
        # that forced the advisory downgrade were bounded by the span rule in
        # extract_targets() (see its docstring for the design and the rejected
        # alternative).
        #
        # HOW THE RESTORE WAS EARNED, since a previous restore was NOT:
        # this branch originally shipped as a hard deny on a 0.000% measurement
        # taken over a SUBSET (11,559 calls / 2 transcripts). A wider replay
        # (48,348 / 4) then found 0.062% and two live FP classes that broke
        # documented capabilities — the remote-exec wrapper argument and quoted
        # prose on a non-echo verb. That is the guard-1557 lesson: a clean rate
        # bounds only the FP classes the corpus CONTAINS.
        #
        # So the restore required BOTH directions measured, not one:
        #   FP  full corpus, ALL 4 transcripts, 48,646 calls -> residual EMPTY,
        #       with a positive control proving the instrument still flags
        #       genuine writes (a bare zero is what misled the first restore).
        #   FN  five local-exec forms (`bash -c`, `sh -c`, `env ... bash -c`,
        #       `bash -lc`, quoted-redirect) verified STILL flagged, because a
        #       span rule alone would have approved every one of them (rb-401).
        # Harness: core/scripts/bash-hook-corpus-replay.py — durable, defaults
        # to ALL transcripts; re-run it before touching this branch again.
        # Contract: core/scripts/tests/test_bash_path_hook_out_of_root.py holds
        # both floors, including test_local_exec_quoted_write_must_stay_flagged,
        # which must keep passing. Do NOT relax it to make a span change pass.
        reason = (
            f"[l1-bash-path] DENY: write target outside all configured roots\n"
            f"  verb: {verb}\n"
            f"  write target: {raw_path}\n"
            f"{chr(10).join(f'  {label} = {root}' for label, root in allowed_roots)}\n"
            f"The Write/Edit hook REFUSES this same target. If this is a real "
            f"local write, redirect it under a configured root — for scratch use "
            f"agents/{agent}/temp/. System temp (/tmp, /var/tmp) and device sinks "
            f"(/dev, /proc) are exempt. For a product repo, add the root to "
            f"AGENT_WRITE_PATH in <agent>/local-paths.conf (user-authorized).\n"
            f"If this is command TEXT rather than a local write (a remote-exec "
            f"wrapper argument, or prose in a quoted flag), it should already be "
            f"approved by the quoted-span rule — if it is not, that is a bug "
            f"worth reporting, not a reason to weaken the branch.\n"
            f"See: .claude/rules/path-resolution.md.\n"
        )
        emit_deny(reason)

    approve_no_mutation()


try:
    main()
except Exception:
    # Catch-all fail-open
    pass

sys.exit(0)
