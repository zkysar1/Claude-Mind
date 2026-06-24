#!/usr/bin/env python3
"""PreToolUse[Bash] hook -- auto-stamp MIND_AGENT on every Bash command.

Resolves the agent binding for the current SID via the canonical resolver
(Phase 2.6: tries agents/<name>/sessions/<SID>/binding.yaml first, falls back
to the legacy .active-agent-<SID> file at PROJECT_ROOT). Prepends
`export MIND_AGENT=<name>; ` to tool_input.command, unless the LLM already
wrote MIND_AGENT= explicitly (override) or the resolved name is invalid.

SAFETY: fail open on ANY error. sys.exit(0) with no stdout on every reject
path. Claude Code treats "exit 0 with empty stdout" as "approve with no
mutation".

Invoked by bash-agent-inject.sh, which handles Python discovery on Windows.
"""

import sys
import json
import re
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# Hooks run with cwd set by Claude Code, not this file's dir, so the resolver
# module is not importable by relative name without a sys.path hint.
sys.path.insert(0, str(SCRIPT_DIR))
# resolve_binding_with_diagnostics returns (binding, failure_reason) so the
# miss log can name the SPECIFIC failure mode instead of a single opaque
# "no_active_agent_binding" (7). The legacy resolve() wrapper is no
# longer imported here — the diagnostics variant supersedes it for the hook.
from _session_binding import (
    resolve_binding_with_diagnostics,
    _agent_dir,
    _SESSIONS_DIRNAME,
)

# Shared helpers (): approve_no_mutation, stdin_json_or_approve.
# emit_deny added 2026-05-19 for the F4 cruft gate below — bash-agent-inject
# now has a deny path for agent-dir-shaped Bash cruft creation, in addition
# to its primary auto-injection role.
from hook_helpers import approve_no_mutation, stdin_json_or_approve, emit_deny  # noqa: E402


def _sanitize_reason(reason: str) -> str:
    """Reduce a reason string to a filesystem-safe sentinel-key fragment.

    Reasons are short kebab-case tokens from resolve_binding_with_diagnostics
    (e.g. "binding-yaml-missing", "session-id-mismatch"). Defensive scrub:
    keep [a-z0-9-], collapse anything else to '-', cap length. Guarantees the
    `<sid>__<reason>` sentinel path never escapes the sentinels dir even if a
    future reason carries unexpected characters.
    """
    safe = re.sub(r"[^a-z0-9-]", "-", (reason or "unknown").lower())
    return safe[:48] or "unknown"


def _binding_resolved_dir(project_root: Path) -> Path:
    return project_root / "core" / "logs" / "bash-inject-resolved"


def _mark_binding_resolved(sid: str, project_root: Path, agent: str = "") -> None:
    """Record that this SID resolved to an agent at least once this session.

    A zero-byte memo at `core/logs/bash-inject-resolved/<sid>`. Its presence
    lets a SUBSEQUENT miss distinguish "binding was never there"
    (first Bash before /start — expected, low-signal) from
    "binding-yaml-mid-session-disappeared" (the canonical g-115-1146 bug:
    injection worked, then silently stopped). Fail-open: any error swallowed.
    """
    try:
        if not sid or any(c in sid for c in ("/", "\\", "\n", "\r", " ")) or ".." in sid:
            return
        d = _binding_resolved_dir(project_root)
        d.mkdir(parents=True, exist_ok=True)
        # Store the resolved agent NAME (was a zero-byte touch) so a later
        # transient resolve failure can fail SAFE -- reuse this SID's bound agent
        # instead of fail-open to the first agent (6 cross-agent hazard).
        (d / sid).write_text(agent or "", encoding="utf-8")
    except Exception:
        pass


def _binding_was_resolved(sid: str, project_root: Path) -> bool:
    """True if a resolution memo exists for this SID. Fail-open False."""
    try:
        if not sid or any(c in sid for c in ("/", "\\", "\n", "\r", " ")) or ".." in sid:
            return False
        return (_binding_resolved_dir(project_root) / sid).exists()
    except Exception:
        return False


def _last_resolved_agent(sid: str, project_root: Path) -> str:
    """The agent this SID last resolved to (from the memo), or '' if none.

    Lets a transient binding-resolve failure fail SAFE: a SID is bound to one
    agent for its session lifetime, so reusing the last-known name routes the
    call to the CORRECT agent rather than fail-open to the first agent. The
    name is strictly validated before any caller injects it into a shell
    export clause (defense vs a tampered memo). Fail-open '' on any error.
    """
    try:
        if not sid or any(c in sid for c in ("/", "\\", "\n", "\r", " ")) or ".." in sid:
            return ""
        p = _binding_resolved_dir(project_root) / sid
        if not p.exists():
            return ""
        name = p.read_text(encoding="utf-8").strip()
        return name if re.fullmatch(r"[A-Za-z0-9._-]+", name or "") else ""
    except Exception:
        return ""


def _log_binding_miss_once(sid: str, project_root: Path, reason: str = "binding-yaml-missing") -> None:
    """Defense-in-depth diagnostic when binding resolution returns no agent.

    Fires only when bash-agent-inject attempted to resolve the SID binding
    AND the caller did NOT write an explicit `MIND_AGENT=` override. Per-(SID,
    reason) one-shot via a zero-byte sentinel — a NO_AGENT session logs each
    DISTINCT failure mode once, not once per Bash call, and not collapsed
    across modes.

    Per-(SID, reason) rather than per-SID (g-115-1187): the prior per-SID
    sentinel suppressed EVERY miss after the first for a SID. The canonical
    g-115-1146 incident — injection working then silently stopping mid-session
    — produced ZERO additional log lines because the session-start miss had
    already set the SID's only sentinel. Keying the sentinel on (sid, reason)
    means a "binding-yaml-mid-session-disappeared" miss logs even when an
    earlier "binding-yaml-missing" miss for the same SID already fired.

    Discoverability path (the whole reason this exists):
      1. User notices that MIND_AGENT injection isn't happening for some session.
      2. User greps `core/logs/bash-inject-misses.jsonl`.
      3. Each line names a SID + the SPECIFIC failure reason that hit this hook.
      4. The `hint` field tells them the two fixes: `/start <agent-name>` to
         create the binding, or prefix the Bash command with `MIND_AGENT=<name>`
         for one-off cross-agent probes.

    Sink location (2026-05-19 relocation, plan v1 step 0.13-0.14):
      Sentinels under `core/logs/bash-inject-sentinels/<sid>__<reason>`; log at
      `core/logs/bash-inject-misses.jsonl`. Both gitignored.

    Fail-open invariant (PreToolUse hook contract):
      ANY exception here is swallowed. The hook MUST never block the Bash call
      because of a diagnostic write. The whole function is wrapped in try/except.

    SID-shape protection: rejects path-traversal characters before constructing
    the sentinel path. `reason` is additionally scrubbed via _sanitize_reason,
    so neither component can escape the sentinels dir.
    """
    try:
        if not sid or any(c in sid for c in ("/", "\\", "\n", "\r", " ")) or ".." in sid:
            return
        safe_reason = _sanitize_reason(reason)
        logs_dir = project_root / "core" / "logs"
        sentinels_dir = logs_dir / "bash-inject-sentinels"
        sentinels_dir.mkdir(parents=True, exist_ok=True)
        sentinel = sentinels_dir / f"{sid}__{safe_reason}"
        if sentinel.exists():
            return
        # Touch first so a concurrent hook instance sees the sentinel and
        # short-circuits, even before our log-line append flushes. Race-tolerant
        # by design: if two hooks both write the same record, that's a single
        # extra line — not a correctness problem.
        sentinel.touch()
        log_path = logs_dir / "bash-inject-misses.jsonl"
        # Rotation (2026-05-19, plan v1 step 0.4): cap the log at ~100 KB by
        # truncating to the last 200 lines before append. Without this the
        # file grew forever (one record per unique NO_AGENT SID across the
        # repo's lifetime). 200 lines is plenty for diagnostic context — every
        # entry is a single SID + reason + hint. Rotation failure is swallowed
        # so it never blocks the log write (PreToolUse fail-open contract).
        try:
            if log_path.exists() and log_path.stat().st_size > 100_000:
                lines = log_path.read_text(encoding="utf-8").splitlines()
                if len(lines) > 200:
                    log_path.write_text(
                        "\n".join(lines[-200:]) + "\n", encoding="utf-8"
                    )
        except Exception:
            pass
        record = json.dumps({
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "sid": sid,
            "reason": reason,
            "hint": "Run /start <agent-name>, or prefix the Bash command with MIND_AGENT=<name>.",
        })
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(record + "\n")
    except Exception:
        pass


_AGENT_DIR_CRUFT_PATTERNS_CACHE = {}


def _strip_bash_strings(command: str) -> str:
    """Approximate bash quote-stripping: replace contents of '...', "...", and
    `...` literals with empty markers so a regex scan doesn't false-positive
    on patterns that appear ONLY inside string arguments to other commands.

    Limitations: doesn't handle backslash-escaped quotes, $'...' ANSI-C
    quoting, or heredoc bodies. Good enough for the F4 gate's purpose —
    the common false positives (echo '... bravo/session ...', python -c
    "...bravo/session...", grep "bravo/session" file) all use straight
    quoting and get neutralized.

    Heredoc bodies (<<EOF ... EOF) are NOT stripped here — content piped
    into a tool via heredoc is exactly the case F4 wants to catch (e.g.,
    `cat <<EOF > bravo/session/X`), so leaving heredoc content visible to
    the regex is correct. The redirect target `> bravo/session/X` lives
    on the heredoc opener line, NOT inside the heredoc body.
    """
    out = []
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if c == "'":
            j = command.find("'", i + 1)
            if j == -1:
                break  # unterminated string — stop, don't mangle the rest
            out.append("''")
            i = j + 1
        elif c == '"':
            j = command.find('"', i + 1)
            if j == -1:
                break
            out.append('""')
            i = j + 1
        elif c == '`':
            j = command.find('`', i + 1)
            if j == -1:
                break
            out.append('``')
            i = j + 1
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def _detect_agent_dir_cruft(command: str, project_root: Path):
    """F4 cruft gate (2026-05-19): detect Bash commands that would create
    `<agentname>/session/...` at PROJECT_ROOT instead of the canonical
    `agents/<agentname>/session/...`. The L1 path-resolution hook gates
    Write/Edit/MultiEdit; Bash redirects + mkdir bypass it. This Bash-level
    gate closes that hole.

    Two write-context patterns are detected:
      A. `mkdir [-flags] <agent>/session[/...]`
      B. `>` or `>>` redirect target `<agent>/session/...`

    String literals are stripped first via _strip_bash_strings so patterns
    that appear ONLY inside quoted arguments to other commands (test data
    in echo, search patterns in grep, JSON content in python -c) don't
    fire the gate. The L1 path-resolution hook applies the same regex
    strategy implicitly by only inspecting tool_input.file_path — the
    F4 gate has to do quote-stripping explicitly because Bash command
    text is a string blob.

    Negative lookbehind on the agent name excludes path-component characters
    (alphanumeric, /, ., -, _, \\) so `agents/bravo/session/...` and
    `./bravo/session/...` and `lib/bravo/session/...` are NOT matched —
    only the bare `bravo/session/...` cruft form.

    Returns the matched agent name on hit, None otherwise. Fail-open on
    any exception — F4 is preventive, not a safety gate.
    """
    try:
        if not command or "/session" not in command:
            return None
        agents_dir = project_root / "agents"
        if not agents_dir.is_dir():
            return None
        # Cache per project_root; agents/ dirlist is stable across a single
        # session and re-reading on every Bash call is wasted I/O.
        cache_key = str(agents_dir)
        names = _AGENT_DIR_CRUFT_PATTERNS_CACHE.get(cache_key)
        if names is None:
            names = sorted(
                d.name for d in agents_dir.iterdir()
                if d.is_dir() and not d.name.startswith('.')
            )
            _AGENT_DIR_CRUFT_PATTERNS_CACHE[cache_key] = names
        if not names:
            return None
        # Strip quoted strings before scanning. See _strip_bash_strings.
        scan_text = _strip_bash_strings(command)
        if "/session" not in scan_text:
            return None
        name_alt = '|'.join(re.escape(n) for n in names)
        # Pattern A: mkdir with -flags then bare <agent>/session
        pat_mkdir = re.compile(
            r'\bmkdir\b\s+(?:-[a-zA-Z]+\s+)*'
            r'(?<![A-Za-z0-9_/.\\-])(' + name_alt + r')/session\b'
        )
        # Pattern B: redirect target — > or >> followed by bare <agent>/session
        pat_redirect = re.compile(
            r'>>?\s*(?<![A-Za-z0-9_/.\\-])(' + name_alt + r')/session(?:/|\b)'
        )
        m = pat_mkdir.search(scan_text) or pat_redirect.search(scan_text)
        if m:
            return m.group(1)
    except Exception:
        return None
    return None


def main():
    data = stdin_json_or_approve()

    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command", "") or ""
    sid = data.get("session_id", "") or ""

    # F4 cruft gate (2026-05-19) — fires regardless of SID. A Bash command
    # without a SID context is still capable of creating PROJECT_ROOT
    # cruft via the agent-dir-shaped pattern, and the educational message
    # below tells the caller exactly how to fix it. Runs BEFORE the SID
    # short-circuit so the gate covers all Bash invocations.
    cruft_agent = _detect_agent_dir_cruft(command, SCRIPT_DIR.parent.parent)
    if cruft_agent:
        emit_deny(
            f"Bash cruft gate (F4) refused command: detected "
            f"`{cruft_agent}/session/` pattern without the required "
            f"`agents/` parent prefix.\n\n"
            f"Canonical path: agents/{cruft_agent}/session/...\n"
            f"Or via helper:  $(bash -c 'source core/scripts/_paths.sh && "
            f"agent_dir {cruft_agent}')/session/...\n\n"
            f"Background: the L1 path-resolution hook gates Write/Edit/"
            f"MultiEdit tools but NOT Bash, so a `mkdir -p {cruft_agent}/"
            f"session && echo X > {cruft_agent}/session/Y` would silently "
            f"create PROJECT_ROOT/{cruft_agent}/ cruft. This Bash-level "
            f"gate was added 2026-05-19 (fresh-eyes-code F4) to catch the "
            f"missing-prefix case. If your intent is to write under the "
            f"canonical location, add `agents/` before `{cruft_agent}`; "
            f"if you have a legitimate non-cruft use, restructure to avoid "
            f"the bare-name shape (e.g., use the canonical `agents/...` "
            f"path or invoke a helper that resolves it)."
        )

    # Without a session_id we have nothing useful to inject. Approve as-is.
    if not sid:
        approve_no_mutation()

    # CRITICAL (do not "simplify" back to bare .as_posix()): MSYS bash's
    # directory lookup resolves only POSIX drive form (/c/...) — a PATH
    # entry in Windows form (C:/...) survives the export string intact
    # but executables inside it are unreachable (stat fails). Empirically
    # verified 2026-04-19 on this machine: `PATH="C:/tmp/x:$P" which ...`
    # reports "not found" while `PATH="/c/tmp/x:$P" which ...` succeeds.
    # On POSIX systems the regex doesn't match → identity → no-op.
    _posix = (SCRIPT_DIR / ".python-shim").as_posix()
    _m = re.match(r"^([A-Za-z]):(.*)", _posix)
    shim_path = f"/{_m.group(1).lower()}{_m.group(2)}" if _m else _posix

    # INJECTION POLICY — session-scoped vs agent-scoped:
    #   PATH (shim)    → ALWAYS injected when SID is set. Makes bare `python`
    #                    work without sourcing _paths.sh (Windows Store stub
    #                    eats system `python` otherwise).
    #   MIND_SID      → ALWAYS injected when SID is set. It is the authoritative
    #                    this-session SID for skill pseudocode (/stop runner vs
    #                    observer detection, /start Step 0 binding). See
    #                    guard-341 / rb-386. DO NOT make MIND_SID conditional —
    #                    it is the single source of truth for the current SID.
    #   MIND_AGENT    → CONDITIONAL. Skipped when (a) caller wrote an explicit
    #                    `MIND_AGENT=` at a command boundary (override — caller
    #                    chose the agent context), or (b) no `.active-agent-$SID`
    #                    binding exists yet (first Bash call of /start Step 0).
    agent_clause = ""
    if not re.search(r"(^|[\s;&|(])MIND_AGENT=", command):
        project_root = SCRIPT_DIR.parent.parent
        binding, fail_reason = resolve_binding_with_diagnostics(sid, project_root)
        # Transient I/O (OneDrive latency / AV scan / handle contention) can make
        # resolve spuriously return None for a SID whose binding is fine. Retry --
        # re-reading the actual binding yields the CORRECT current agent (no
        # staleness), unlike the memo fallback below. 6 hazard.
        if binding is None or not getattr(binding, "agent", ""):
            for _retry in range(2):
                binding, fail_reason = resolve_binding_with_diagnostics(sid, project_root)
                if binding is not None and binding.agent:
                    break
        if binding is not None and binding.agent:
            agent_clause = f"export MIND_AGENT={binding.agent}; "
            # Record that this SID resolved at least once. A later miss can then
            # be classified as mid-session-disappeared rather than expected
            # first-call-before-/start. See _mark_binding_resolved (7).
            _mark_binding_resolved(sid, project_root, binding.agent)
        else:
            # Defense-in-depth: surface SIDs that ran without a binding so the
            # silent-no-injection failure class becomes greppable instead of
            # invisible. The hook still fails open — no exception path here can
            # block the Bash call. See _log_binding_miss_once for the rationale.
            #
            # Mid-session-disappeared upgrade (7): a "binding-yaml-missing"
            # miss AFTER a prior successful resolve for this SID is the canonical
            # 6 bug (injection worked, then stopped) — promote the reason
            # so it greps distinctly from the expected first-Bash-before-/start miss.
            last = _last_resolved_agent(sid, project_root)
            if last:
                # Fail SAFE: transient resolve failure for a SID that resolved
                # earlier this session -- reuse its (immutable) bound agent rather
                # than fall through to the first agent. 6 hazard.
                agent_clause = f"export MIND_AGENT={last}; "
                _log_binding_miss_once(sid, project_root, "binding-resolve-transient-failsafe")
            else:
                if fail_reason == "binding-yaml-missing" and _binding_was_resolved(sid, project_root):
                    fail_reason = "binding-yaml-mid-session-disappeared"
                _log_binding_miss_once(sid, project_root, fail_reason)

    # Phase 1A () + reducer-aware routing (): per-Body WM routing
    # env. Inject BODY_WM_PATH when the bound session has a forked body-WM-FILE
    # (`sessions/<sid>/working-memory.yaml`), so CLI WM consumers (wm.py + the
    # gate scripts) route to the Body's forked WM instead of the agent-wide one.
    # The body-WM-file is created by /start FORK-BODY ONLY for a NON-reducer Body
    # (a 2nd+ worker); the REDUCER (the Body holding running-session-id) gets a
    # manifest but NO body-WM-file, so it stays on the agent-wide WM. Routing on
    # the FILE's existence (not the manifest's) is the backward-compat keystone:
    # one Body == the reducer == today's behavior. Fail-open: any stat error ->
    # no env -> agent-wide WM. See conventions/session-state.md "Phase 1B".
    body_clause = ""
    _agent_m = (re.search(r"export MIND_AGENT=(\S+);", agent_clause)
                or re.search(r"(?:^|[\s;&|(])MIND_AGENT=([^\s;&|)]+)", command))
    if _agent_m:
        try:
            # F7 (): route the body-WM path through the rename-tracking
            # helpers instead of literal "agents"/"sessions" segments so an
            # AGENTS_PARENT_DIR / SESSIONS_DIRNAME rename stays single-sourced
            # in _session_binding (CLAUDE.md Agent-dir Resolution). Behavior-
            # identical today: _agent_dir(root, name) == root/"agents"/name and
            # _SESSIONS_DIRNAME == "sessions". No new module import (hot-path safe).
            _body_wm = (_agent_dir(SCRIPT_DIR.parent.parent, _agent_m.group(1))
                        / _SESSIONS_DIRNAME / sid / "working-memory.yaml")
            if _body_wm.exists():
                body_clause = f'export BODY_WM_PATH="{_body_wm.as_posix()}"; '
        except OSError:
            body_clause = ""

    expected_prefix = f'export PATH="{shim_path}:$PATH"; {agent_clause}{body_clause}export MIND_SID={sid};'
    if command.startswith(expected_prefix):
        approve_no_mutation()

    new_command = f"{expected_prefix} {command}"

    # updatedInput replaces the whole tool_input per Claude Code's hook
    # contract, so preserve any other fields the LLM set.
    updated = {"command": new_command}
    for key in ("description", "timeout", "run_in_background"):
        if key in tool_input:
            updated[key] = tool_input[key]

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated,
        }
    }))


# Guard module-level execution so the helpers above are importable for tests
# (7). The hook is invoked as `python3 bash-agent-inject.py` by
# bash-agent-inject.sh, so __name__ == "__main__" and behavior is unchanged;
# importing the module (test harness) no longer runs main() + sys.exit(0).
if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
