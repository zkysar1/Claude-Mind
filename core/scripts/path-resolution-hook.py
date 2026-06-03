#!/usr/bin/env python3
"""PreToolUse[Write|Edit|MultiEdit] hook — L1 path-resolution enforcement.

Blocks writes/edits to paths outside the three canonical roots:
  (1) PROJECT_ROOT — local repo checkout
  (2) WORLD_PATH  — from <agent>/local-paths.conf
  (3) META_PATH   — from <agent>/local-paths.conf

This is the L1 layer of the three-layer path defense (see g-115-33 and
.claude/rules/path-resolution.md):
  L1 = this hook (write-time enforcement)
  L2 = .claude/settings.local.json allowlist (permission prune, g-115-34)
  L3 = validate-paths.sh in /prime (session-start observability, g-115-35)

Root cause being prevented: the 2026-04-02 and 2026-04-17 bugs where the LLM
derived `meta/` or `world/` paths by navigating from the project root instead of
resolving via local-paths.conf, silently writing to stale external mirrors.

SAFETY (matches bash-agent-inject.py): fail open on ANY error. sys.exit(0) with
empty stdout = "approve with no mutation" per Claude Code's PreToolUse contract.
Any uncaught exception is swallowed at the bottom. Blocking requires a
structured JSON response on stdout; the catch-all suppresses stdout on error
so a broken hook never accidentally emits a partial deny.
"""

import sys
import os
from pathlib import Path

# Shared helpers (): approve_no_mutation, emit_deny,
# stdin_json_or_approve. extract_file_path/is_absolute_path are local
# below because path-resolution adds tool_name dispatch and a Windows-form
# absolute-path check — kept inline to preserve clarity.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_helpers import (  # noqa: E402
    approve_no_mutation,
    emit_deny,
    extract_file_path,
    is_absolute_path,
    stdin_json_or_approve,
)


# --- Agent-dir resolution (Phase 2.5.C) ---
# Sync invariant: AGENTS_PARENT_DIR matches core/scripts/_paths.py.
# Inlined here (not imported) to keep hook fail-open and import-cycle-safe.
# See CLAUDE.md "Agent-dir Resolution" section.
AGENTS_PARENT_DIR = "agents"

# --- Per-session dirs (Phase 2.6) ---
# Sync invariant with core/scripts/_paths.py (SESSIONS_DIRNAME / SESSION_DIRNAME).
# Used by Task 2.6.G to sanction agents/<name>/sessions/<SID>/scratch/ as
# the L1-approved scratch home.
SESSIONS_DIRNAME = "sessions"
SESSION_DIRNAME = "session"

# --- Agent-dir write-surface allowlist (file-model normalization, 2026-06) ---
# The bound agent dir is a CLOSED write surface: only these top-level dirs
# (writes anywhere beneath them) and these registered top-level files are
# permitted. Everything else — reports/, an invented directory, or a stray
# top-level file — is denied with a redirect to temp/. This is an ALLOWLIST,
# NOT a reports/ blacklist: reports/ is denied because it is not on the list,
# the same mechanism that denies any future invented directory. Keep in sync
# with core/config/conventions/temp-store.md ("Permitted top-level directories"
# + the registered-file list) and init-agent.sh.
_AGENT_DIR_ALLOWLIST_DIRS = frozenset({
    "session", "sessions", "journal", "experience", ".history", "temp",
})
_AGENT_DIR_ALLOWLIST_FILES = frozenset({
    "self.md", "profile.yaml", "developmental-stage.yaml", "curriculum.yaml",
    "curriculum-promotions.jsonl", "aspirations.jsonl", "aspirations-archive.jsonl",
    "aspirations-meta.json", "experience.jsonl", "experience-archive.jsonl",
    "experience-meta.json", "experiential-index.yaml", "infra-health.yaml",
    "prep-tasks.yaml", "journal.jsonl", "changelog.jsonl", "local-paths.conf",
    ".initialized", "insights.jsonl", "weakness-report.yaml", "BACKLOG.md",
    "COMPLETION-REPORT.md",
})


def _resolve_agent_dir(project_root, agent):
    """Compute agent dir path. Mirrors agent_dir() in _paths.py."""
    if AGENTS_PARENT_DIR:
        return os.path.join(project_root, AGENTS_PARENT_DIR, agent)
    return os.path.join(project_root, agent)


def _resolve_agent_sessions_root(project_root, agent):
    """Parent dir for per-session dirs. Mirrors agent_sessions_root() in _paths.py."""
    return os.path.join(_resolve_agent_dir(project_root, agent), SESSIONS_DIRNAME)


def _resolve_agent_session_dir(project_root, agent, sid):
    """Per-session dir. Mirrors agent_session_dir() in _paths.py."""
    return os.path.join(_resolve_agent_dir(project_root, agent),
                        SESSIONS_DIRNAME, sid)


def norm_path(p):
    """Normalize a path for root-prefix comparison across MSYS2/Windows forms.
    Converts `/c/foo` -> `c:/foo`, strips trailing separators, lowercases drive
    letter, and uses forward slashes throughout. Returns empty string on any
    error (caller must treat empty as 'cannot compare')."""
    if not p:
        return ""
    try:
        p = p.replace("\\", "/")
        # MSYS2 form: /c/Users/... -> c:/Users/...
        if len(p) >= 3 and p[0] == "/" and p[2] == "/" and p[1].isalpha():
            p = p[1].lower() + ":" + p[2:]
        # //C:/... (Claude Code allowlist style) -> c:/...
        if p.startswith("//") and len(p) >= 5 and p[3] == ":":
            p = p[2:].lower()[0] + p[3:]
        # Lowercase drive letter if present (C:/... -> c:/...)
        if len(p) >= 2 and p[1] == ":":
            p = p[0].lower() + p[1:]
        # Collapse double slashes, strip trailing slash
        while "//" in p:
            p = p.replace("//", "/")
        if len(p) > 1 and p.endswith("/"):
            p = p[:-1]
        return p
    except Exception:
        return ""


def is_under(child, root):
    """True if normalized child path is at or below normalized root path."""
    if not child or not root:
        return False
    if child == root:
        return True
    return child.startswith(root + "/")


def is_new_toplevel(target, root):
    """True if writing to `target` would create a new top-level entry under
    `root`. The "top-level entry" is the first path segment under root —
    e.g. for `WORLD/handoffs/foo.txt` it is `handoffs`; for `WORLD/scratch.md`
    it is `scratch.md`. If that entry does not exist on disk at write time,
    this write would create it.

    Root-agnostic by design — caller picks which roots to apply this against.
    Currently applied to WORLD_PATH, META_PATH, and the bound agent dir; see
    the allow-check loop in main() and `.claude/rules/path-resolution.md`
    "L1 Cruft Prevention" section."""
    if not is_under(target, root) or target == root:
        return False
    rel = target[len(root) + 1:]
    if not rel:
        return False
    first_segment = rel.split("/", 1)[0]
    if not first_segment:
        return False
    toplevel_path = root + "/" + first_segment
    return not os.path.exists(toplevel_path)


def is_allowlisted_agent_toplevel(target, agent_root):
    """True if `target` is at or below an ALLOWLISTED top-level entry of the
    bound agent dir — an allowlisted directory (writes anywhere beneath it) or
    a registered top-level file. Everything else returns False and is denied by
    the caller.

    Mirrors is_new_toplevel's first-segment extraction but checks allowlist
    MEMBERSHIP instead of disk existence — so it denies `reports/` even though
    `reports/` exists on disk (the file-model normalization froze reports/ and
    routes working docs to temp/). See core/config/conventions/temp-store.md
    and `.claude/rules/path-resolution.md`.
    """
    if not is_under(target, agent_root) or target == agent_root:
        return False
    rel = target[len(agent_root) + 1:]
    if not rel:
        return False
    parts = rel.split("/", 1)
    first_segment = parts[0]
    if not first_segment:
        return False
    if len(parts) == 1:
        # A file (or entry) directly at the agent root — must be registered.
        return first_segment in _AGENT_DIR_ALLOWLIST_FILES
    # A path under a first-segment subdirectory — that dir must be allowlisted.
    return first_segment in _AGENT_DIR_ALLOWLIST_DIRS


def read_paths_conf(conf_path):
    """Parse KEY=VALUE lines. Returns dict with WORLD_PATH, META_PATH, and
    AGENT_WRITE_PATH keys (values may be None if unset/unreadable). Strips
    surrounding quotes and CR. AGENT_WRITE_PATH is an optional additional
    write root outside PROJECT_ROOT/WORLD_PATH/META_PATH — per g-115-156,
    lets an agent modify sibling codebases it is responsible for (e.g.,
    external product code) without relaxing L1's default-deny on arbitrary paths."""
    result = {"WORLD_PATH": None, "META_PATH": None, "AGENT_WRITE_PATH": None}
    try:
        with open(conf_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip().replace("\r", "")
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key in result:
                    result[key] = value
    except Exception:
        pass
    return result


def main():
    data = stdin_json_or_approve()

    tool_name = data.get("tool_name", "") or ""
    # Only gate the three write tools. Other matches (if a wildcard gets wired
    # up) are approved unconditionally.
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        approve_no_mutation()

    tool_input = data.get("tool_input") or {}
    file_path = extract_file_path(tool_input)
    if not file_path:
        approve_no_mutation()

    # Relative paths resolve against cwd at tool execution time. The permission
    # layer (L2) handles those; we gate only absolute paths, where the 2026-04-02
    # bug lives.
    if not is_absolute_path(file_path):
        approve_no_mutation()

    project_root = os.environ.get("PROJECT_ROOT", "")
    if not project_root:
        approve_no_mutation()

    # --- Resolve agent binding ---
    # Phase 2.6: delegate to the canonical resolver which tries the new
    # agents/<name>/sessions/<SID>/binding.yaml layout first, then falls back
    # to the legacy .active-agent-<SID> file at PROJECT_ROOT. Imported lazily
    # so the hook's fail-open contract is preserved on ImportError.
    sid = data.get("session_id", "") or ""
    agent = os.environ.get("MIND_AGENT", "") or ""
    if not agent and sid:
        try:
            from _resolve_agent_from_sid import resolve as _resolve_agent
            from pathlib import Path as _Path
            agent = _resolve_agent(sid, _Path(project_root)) or ""
        except Exception:
            agent = ""

    # No agent resolvable — fail open. The hook can't know the correct external
    # paths without local-paths.conf; blocking blindly would break legitimate
    # one-off writes outside any agent binding.
    if not agent:
        approve_no_mutation()

    # --- Read local-paths.conf ---
    conf_path = os.path.join(_resolve_agent_dir(project_root, agent), "local-paths.conf")
    if not os.path.isfile(conf_path):
        approve_no_mutation()
    paths = read_paths_conf(conf_path)

    # --- Build allowed roots ---
    # Always allow PROJECT_ROOT (local repo). WORLD/META only if configured.
    target = norm_path(file_path)
    if not target:
        approve_no_mutation()

    # Bound agent's dir, used by the cruft-prevention check below for parity
    # with WORLD/META. Refuses new top-level entries under <PROJECT_ROOT>/<agent>/
    # so that silent invention of `bravo/handoffs/`, `alpha/scratch/`, etc.
    # cannot blend into the established canonical layout (see
    # `.claude/rules/path-resolution.md` "L1 Cruft Prevention" section).
    agent_dir_norm = norm_path(_resolve_agent_dir(project_root, agent))

    allowed_roots = []
    pr_norm = norm_path(project_root)
    if pr_norm:
        allowed_roots.append(("PROJECT_ROOT", pr_norm))
    wp_norm = norm_path(paths.get("WORLD_PATH") or "")
    if wp_norm:
        allowed_roots.append(("WORLD_PATH", wp_norm))
    mp_norm = norm_path(paths.get("META_PATH") or "")
    if mp_norm:
        allowed_roots.append(("META_PATH", mp_norm))
    # : optional agent-declared additional write root for cross-repo
    # work (e.g., external product code). User-edited in local-paths.conf; agents
    # cannot self-modify that file. Reversible by removing the line.
    awp_norm = norm_path(paths.get("AGENT_WRITE_PATH") or "")
    if awp_norm:
        allowed_roots.append(("AGENT_WRITE_PATH", awp_norm))

    if not allowed_roots:
        approve_no_mutation()

    # --- Allow check ---
    # A target inside a configured root is allowed UNLESS it would create a
    # new top-level entry under WORLD/META OR under the bound agent dir
    # (cruft prevention — see `.claude/rules/path-resolution.md` "L1 Cruft
    # Prevention" section).
    #
    # First-match-wins: cruft checks fire only against the FIRST matching
    # root, then approve_no_mutation() exits. Safe while allowed_roots stay
    # pairwise disjoint (the default config holds this — PROJECT_ROOT and
    # AGENT_WRITE_PATH are siblings; WORLD/META are external). If a future
    # config nests one root inside another, the inner root's cruft check
    # gets silently bypassed — fix by restructuring to "check all matching
    # roots before approving" rather than papering over with extra checks.
    for label, root in allowed_roots:
        if is_under(target, root):
            if label in ("WORLD_PATH", "META_PATH") and is_new_toplevel(target, root):
                cruft_reason = (
                    f"Path-resolution hook (L1) blocked {tool_name} to:\n"
                    f"  {file_path}\n"
                    f"This would create a new top-level entry under {label} "
                    f"({root}).\n"
                    f"New top-level entries under WORLD_PATH and META_PATH "
                    f"require explicit approval — this is the cruft-prevention "
                    f"layer (see .claude/rules/path-resolution.md "
                    f"\"L1 Cruft Prevention\" section).\n"
                    f"Options:\n"
                    f"  (a) Place under an EXISTING top-level dir if the "
                    f"artifact fits there.\n"
                    f"  (b) For ephemeral artifacts (handoffs, scratch "
                    f"reports, throwaway docs), write under <agent>/session/ "
                    f"— that path is ephemeral and inside PROJECT_ROOT.\n"
                    f"  (c) Ask the user where the new entry should live, OR "
                    f"have them create the directory first; this write "
                    f"succeeds on retry once the entry exists on disk."
                )
                emit_deny(cruft_reason)
            # Cruft prevention for literal virtual-prefix paths under
            # PROJECT_ROOT. Writes to PROJECT_ROOT/world/... or
            # PROJECT_ROOT/meta/... are the canonical L1 failure mode
            # (, ): the LLM derives the path by
            # string-concatenation against the project checkout instead
            # of resolving via local-paths.conf, silently shadowing the
            # configured external location. Refused regardless of whether
            # the literal dir exists on disk — perpetuating cruft is as
            # bad as creating it. See `.claude/rules/path-resolution.md`
            # "External Path Resolution" section.
            if label == "PROJECT_ROOT":
                for virtual_label, virtual_subdir in (
                    ("WORLD_PATH", "world"),
                    ("META_PATH", "meta"),
                ):
                    literal_root = pr_norm + "/" + virtual_subdir
                    if is_under(target, literal_root):
                        configured = (
                            paths.get(virtual_label)
                            or "(unset in local-paths.conf)"
                        )
                        virtual_cruft_reason = (
                            f"Path-resolution hook (L1) blocked {tool_name} to:\n"
                            f"  {file_path}\n"
                            f"This is a literal PROJECT_ROOT/{virtual_subdir}/... "
                            f"path. {virtual_label} is configured as an EXTERNAL "
                            f"location, not a subdirectory of the project "
                            f"checkout.\n"
                            f"Configured {virtual_label}: {configured}\n"
                            f"Use the configured external path above. Read "
                            f"local-paths.conf to resolve {virtual_label}/* "
                            f"virtual prefixes before invoking Write/Edit. See "
                            f"`.claude/rules/path-resolution.md` \"External "
                            f"Path Resolution\" section."
                        )
                        emit_deny(virtual_cruft_reason)
            # Cruft prevention for PROJECT_ROOT new top-level entries
            # OUTSIDE the bound agent dir (plan v1 step 0.9, 2026-05-19).
            # The repo root should contain only the canonical entries
            # (.claude, core, daemon, readme, .env*, .git*, and the
            # per-agent dirs). New top-level entries at PROJECT_ROOT —
            # whether files (LICENSE-yet-to-be-created, scratch.md) or
            # directories (handoffs/, notes/, my-stuff/) — are the
            # canonical cruft-accumulation shape that we are preventing.
            # Excluding paths inside the agent dir avoids double-emitting
            # (the agent-dir check below produces a more specific message).
            #
            # Friction-by-design: legitimate new top-level entries (a real
            # LICENSE file, a new docs/ directory the user approved) need
            # the user (or a sanctioned init-*.sh) to mkdir/touch first —
            # that pre-existence is the explicit-approval signal. See
            # `.claude/rules/path-resolution.md` "L1 Cruft Prevention".
            if (
                label == "PROJECT_ROOT"
                and is_new_toplevel(target, pr_norm)
                and not (agent_dir_norm and is_under(target, agent_dir_norm))
            ):
                project_root_cruft_reason = (
                    f"Path-resolution hook (L1) blocked {tool_name} to:\n"
                    f"  {file_path}\n"
                    f"This would create a new top-level entry directly under "
                    f"PROJECT_ROOT ({pr_norm}).\n"
                    f"The repo root is reserved for canonical entries "
                    f"(.claude/, core/, mind_api/, readme/, .env*, .git*, "
                    f"per-agent dirs). New top-level entries at the repo "
                    f"root are the canonical cruft shape — silent "
                    f"invention of handoffs/, scratch/, notes/, "
                    f"top-level-file.md etc. accumulates as repo bloat "
                    f"(see .claude/rules/path-resolution.md \"L1 Cruft "
                    f"Prevention\" section).\n"
                    f"Options:\n"
                    f"  (a) Place under an EXISTING top-level dir if the "
                    f"artifact fits there (core/logs/ for telemetry, "
                    f"<agent>/session/ for transients, world/ for shared "
                    f"domain state, meta/ for strategies).\n"
                    f"  (b) Have the user create the directory first; "
                    f"this write succeeds on retry once the entry exists.\n"
                    f"  (c) For init scripts, use Bash `mkdir -p` + "
                    f"`touch` which bypasses this hook (init-*.sh is the "
                    f"sanctioned creation path)."
                )
                emit_deny(project_root_cruft_reason)
            # Phase 2.6 scratch sanction: writes under
            # agents/<name>/sessions/<SID>/ are explicitly permitted as
            # the per-session scratch home. The user authorized this
            # explicitly ("session folders are the approved spot for
            # cruft and temp files"). Bypasses the agent-dir new-toplevel
            # check below, so paths like
            # agents/alpha/sessions/abc-123/scratch/notes.md or
            # agents/alpha/sessions/abc-123/experiment-2026-05-19/log.txt
            # land without friction.
            #
            # The per-session dir itself was created at /start time by
            # session-binding-write.py — its existence is the explicit-
            # approval signal. A non-existent SID dir is refused (the
            # write must have a real bound session, not an invented one).
            if (
                label == "PROJECT_ROOT"
                and agent_dir_norm
                and is_under(target, agent_dir_norm)
            ):
                sessions_root = agent_dir_norm + "/sessions"
                if is_under(target, sessions_root) and target != sessions_root:
                    rel = target[len(sessions_root) + 1:]
                    first_seg = rel.split("/", 1)[0] if rel else ""
                    if first_seg:
                        sid_dir_path = sessions_root + "/" + first_seg
                        if os.path.exists(sid_dir_path):
                            approve_no_mutation()
                        else:
                            session_invented_reason = (
                                f"Path-resolution hook (L1) blocked {tool_name} to:\n"
                                f"  {file_path}\n"
                                f"This would create a new top-level entry under "
                                f"{agent}/sessions/ for a SID that was never "
                                f"bound (the dir {sid_dir_path} does not exist).\n"
                                f"Per-session dirs are created only by /start "
                                f"via session-binding-write.py — silent "
                                f"invention of sessions/<arbitrary>/ paths is "
                                f"the same cruft class the rest of L1 prevents.\n"
                                f"Options:\n"
                                f"  (a) Use the bound session SID for the current "
                                f"terminal (echo $MIND_SID).\n"
                                f"  (b) Place the file under an EXISTING agent "
                                f"sub-dir (session/, journal/, etc.) if that's "
                                f"what was intended.\n"
                                f"  (c) /start the agent to create a binding "
                                f"first; this write succeeds on retry."
                            )
                            emit_deny(session_invented_reason)

            # Agent-dir write-surface allowlist (file-model normalization).
            # Fires when the matched root is PROJECT_ROOT AND the target lies
            # inside <PROJECT_ROOT>/<agent>/ AND the target is NOT on the
            # allowlist (permitted dirs + registered files). Denies reports/,
            # invented dirs, and stray top-level files alike. Other PROJECT_ROOT
            # areas (core/, .claude/, the project root itself) are untouched —
            # governed by their own conventions and L2 permission rules. The
            # sessions/<SID>/ block above runs first and exits, so a bound
            # session's scratch is unaffected by this check.
            if (
                label == "PROJECT_ROOT"
                and agent_dir_norm
                and is_under(target, agent_dir_norm)
                and not is_allowlisted_agent_toplevel(target, agent_dir_norm)
            ):
                agent_cruft_reason = (
                    f"Path-resolution hook (L1) blocked {tool_name} to:\n"
                    f"  {file_path}\n"
                    f"This target is not on the bound agent dir's write-surface "
                    f"allowlist ({agent_dir_norm}).\n"
                    f"The agent dir is a CLOSED write surface (file-model "
                    f"normalization). Permitted top-level directories: session, "
                    f"sessions, journal, experience, .history, temp — plus the "
                    f"registered top-level agent files (self.md, the *.jsonl / "
                    f"*.yaml / *.json state files, COMPLETION-REPORT.md, "
                    f"BACKLOG.md, etc.). Any OTHER directory (e.g. "
                    f"`{agent}/reports/`, `{agent}/handoffs/`, `{agent}/notes/`) "
                    f"or stray top-level file is denied. This is an ALLOWLIST, "
                    f"not a reports/ blacklist — reports/ is denied because it is "
                    f"not on the list, the same rule that denies any invented "
                    f"directory (see core/config/conventions/temp-store.md).\n"
                    f"Where this output belongs:\n"
                    f"  - Reports / analyses / briefings / audits / snapshots "
                    f"(working docs that drain to the knowledge tree) -> "
                    f"{agent}/temp/\n"
                    f"  - Per-session IO buffers / probe dumps -> "
                    f"{agent}/session/scratch/\n"
                    f"  - Reusable knowledge -> the knowledge tree "
                    f"(world/knowledge/tree/) via /tree add\n"
                    f"  - A genuinely new registered location -> ask the user to "
                    f"create it (or add an init-*.sh step); shell mkdir bypasses "
                    f"this Write/Edit gate by design."
                )
                emit_deny(agent_cruft_reason)
            approve_no_mutation()

    # --- Block: target is absolute AND outside all configured roots ---
    root_list = "\n".join(f"  {label} = {root}" for label, root in allowed_roots)
    reason = (
        f"Path-resolution hook (L1) blocked {tool_name} to:\n"
        f"  {file_path}\n"
        f"This path is outside the agent's configured roots:\n"
        f"{root_list}\n"
        f"If you meant to write under world/ or meta/, use the configured root "
        f"prefix above, not a hand-derived sibling path. See "
        f".claude/rules/path-resolution.md and <agent>/local-paths.conf."
    )
    emit_deny(reason)


try:
    main()
except Exception:
    # Catch-all fail-open: any unexpected error -> approve no mutation.
    pass

sys.exit(0)
