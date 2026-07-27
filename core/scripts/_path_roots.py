#!/usr/bin/env python3
"""Shared root resolution for the two L1 path-resolution hooks (g-115-3338).

`path-resolution-hook.py` (PreToolUse[Write|Edit|MultiEdit]) and
`bash-path-resolution-hook.py` (PreToolUse[Bash]) both need the same four
primitives — path normalization, containment, new-top-level detection, and
`local-paths.conf` parsing — plus the same notion of "which roots is this
agent allowed to write to". Before this module each hook carried its own copy;
the bash-side docstrings said "Same algorithm as path-resolution-hook.py"
verbatim, which is a drift warning written down rather than fixed. Verified
2026-07-26 (g-115-3338): all four copies were semantically identical, so the
consolidation is behavior-preserving.

Both importers are fail-open at the wrapper level (`python3 ... 2>/dev/null;
exit 0`), so an ImportError here degrades to "approve", never to a wedged
tool call.

# domain-leak-exempt: the write-exempt sink prefixes below are POSIX
# pseudo-filesystem and system-temp literals — the module's detection
# contract, not domain bleed.
"""

import os

# --- Write-exempt sinks (measured carve-out, g-115-3338) --------------------
#
# An absolute WRITE target outside every configured root is the refusal set
# for the Bash surface. These prefixes are excluded from it, because a write
# there is not a governed-filesystem write at all:
#
#   /dev   — device / pseudo-file sinks (`> /dev/null`, `/dev/stderr`,
#            `/dev/fd/N`). Nothing is created; nothing persists.
#   /proc  — procfs control writes. Same reasoning.
#   /tmp, /var/tmp — the system temp tree, which CONTAINS the Claude Code
#            session scratchpad (`/tmp/claude-*/…`) that CLAUDE.md explicitly
#            sanctions for temporary files.
#
# MEASURED, not assumed. Over 11,559 real Bash tool calls drawn from two
# independent sessions, the per-call false-positive rate of the refusal is:
#
#   allowlist                     FP rate
#   ---------------------------   --------
#   (none)                        26.343%
#   /tmp + /var/tmp only           25.859%   <- still unusable
#   + /dev                          0.000%
#   + /dev + /proc                  0.000%
#
# The ordering matters and is counter-intuitive: the goal's pre-mortem
# predicted the session scratchpad and temp tree would be the largest
# false-positive source. They are not — carving them out alone moves the rate
# by half a percentage point. `/dev` carries essentially the entire mass,
# because nearly every wrapper invocation in the loop ends in some form of
# `> /dev/null`. An allowlist built from the pre-mortem alone would have been
# measured at ~26% and the whole approach abandoned as unworkable.
WRITE_EXEMPT_PREFIXES = ("/dev", "/proc", "/tmp", "/var/tmp")


def norm_path(p):
    """Normalize a path for comparison: forward slashes, lowercased Windows
    drive letter, collapsed duplicate separators, no trailing slash.

    Canonical copy — previously duplicated verbatim in both hooks.
    """
    if not p:
        return ""
    try:
        p = p.replace("\\", "/")
        if len(p) >= 3 and p[0] == "/" and p[2] == "/" and p[1].isalpha():
            p = p[1].lower() + ":" + p[2:]
        if p.startswith("//") and len(p) >= 5 and p[3] == ":":
            p = p[2:].lower()[0] + p[3:]
        if len(p) >= 2 and p[1] == ":":
            p = p[0].lower() + p[1:]
        while "//" in p:
            p = p.replace("//", "/")
        if len(p) > 1 and p.endswith("/"):
            p = p[:-1]
        return p
    except Exception:
        return ""


def is_under(child, root):
    """True if `child` is at or below `root` (both already norm_path'd)."""
    if not child or not root:
        return False
    if child == root:
        return True
    return child.startswith(root + "/")


def is_new_toplevel(target, root):
    """True if `target`'s first path segment under `root` does not yet exist.

    This is the cruft-prevention primitive: `WORLD/handoffs/foo.txt` yields
    `handoffs`, `WORLD/scratch.md` yields `scratch.md`.
    """
    if not is_under(target, root) or target == root:
        return False
    rel = target[len(root) + 1:]
    if not rel:
        return False
    first_segment = rel.split("/", 1)[0]
    if not first_segment:
        return False
    return not os.path.exists(root + "/" + first_segment)


def read_paths_conf(conf_path):
    """Parse WORLD_PATH / META_PATH / AGENT_WRITE_PATH from local-paths.conf.

    Values may be quoted (bash-source safety); quotes and CR are stripped.
    Missing keys read back as None. Never raises.
    """
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


def compute_allowed_roots(project_root, paths):
    """Return [(label, norm_root), ...] — every root this agent may write to.

    Order is significant for the Edit-side hook's first-match-wins cruft
    checks: PROJECT_ROOT, then WORLD_PATH, META_PATH, then each
    AGENT_WRITE_PATH entry.

    `paths` is a read_paths_conf() result. AGENT_WRITE_PATH may name several
    roots separated by ';' (g-321-05 multi-root), each becoming an independent
    allowed root; a single-path value is unchanged.
    """
    roots = []
    pr = norm_path(project_root)
    if pr:
        roots.append(("PROJECT_ROOT", pr))
    for key in ("WORLD_PATH", "META_PATH"):
        v = norm_path((paths or {}).get(key) or "")
        if v:
            roots.append((key, v))
    for part in ((paths or {}).get("AGENT_WRITE_PATH") or "").split(";"):
        v = norm_path(part.strip())
        if v:
            roots.append(("AGENT_WRITE_PATH", v))
    return roots


def is_write_exempt_sink(target):
    """True if `target` (norm_path'd) is a device/procfs/system-temp sink.

    See WRITE_EXEMPT_PREFIXES for the measurement that fixed this set.
    """
    if not target:
        return False
    for prefix in WRITE_EXEMPT_PREFIXES:
        if target == prefix or target.startswith(prefix + "/"):
            return True
    return False
