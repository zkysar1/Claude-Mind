#!/usr/bin/env python3
"""Session Manifest Write Gate — block unregistered writes under <agent>/session/.

Origin: g-254-02 (asp-254). Pattern model: session_desync_check.py.

Decides whether a write to a target path is permitted, based on whether the
file's basename is registered in core/config/session-manifest.yaml files[]:

  out-of-scope   (path not under any agent's session/)            → exit 0
  registered     (basename listed in manifest files[])            → exit 0
  unregistered + agent-mode == autonomous                         → exit 1 (block)
  unregistered + agent-mode == assistant                          → exit 1 (warn)
  unregistered + agent-mode == reader                             → exit 0 (info)
  unregistered + agent-mode unknown                               → exit 0 (info, fail-open)

Fail-open philosophy: any failure to load _paths, PyYAML, or the manifest emits
a stderr warning and exits 0. A gate that breaks framework operation when its
own dependencies fail is worse than a gate that lets unregistered writes
through. The gate's job is to catch drift, not to be a security boundary.

Output modes:
  human (default) — one-line stderr message with severity prefix; no stdout
  json            — JSON object on stdout: {allowed, reason, severity, mode,
                                            agent, basename}
"""
import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
try:
    from _paths import PROJECT_ROOT  # type: ignore
except Exception as e:
    print(f"[session-manifest-gate] WARN: failed to import _paths: {e}", file=sys.stderr)
    sys.exit(0)

try:
    import yaml  # type: ignore
except Exception as e:
    print(f"[session-manifest-gate] WARN: PyYAML unavailable: {e}", file=sys.stderr)
    sys.exit(0)


MANIFEST_PATH = Path(PROJECT_ROOT) / "core" / "config" / "session-manifest.yaml"


def _emit(result: dict, output_mode: str) -> None:
    """Emit the result and exit with the appropriate code."""
    if output_mode == "json":
        print(json.dumps(result))
    else:
        sev = result.get("severity", "none")
        if sev != "none":
            label = {"block": "BLOCK", "warn": "WARN", "info": "INFO"}.get(sev, "INFO")
            print(f"[session-manifest-gate] {label}: {result['reason']}", file=sys.stderr)
    sys.exit(0 if result["allowed"] else 1)


def _find_owning_agent(target: Path):
    """Map an absolute path to (agent_name, basename, ancestor_dirs) if it lives
    under <repo>/agents/<agent>/session/. Otherwise (None, None, None).

    Resolution does NOT require the target to exist on disk — writers call this
    BEFORE creating the file. We rely on PathLib's lexical resolution.

    ancestor_dirs is a tuple of intermediate directory names between session/
    and the target file, used by the dir-type registration check (g-115-840).
    For agents/<agent>/session/foo.txt the tuple is empty; for
    agents/<agent>/session/scratch/foo.txt it is ("scratch",); for
    agents/<agent>/session/scratch/sub/foo.txt it is ("scratch", "sub").

    Phase 2.5.D layout: agent dirs live under agents/ parent (must match
    AGENTS_PARENT_DIR in _paths.py / agent_paths.py).
    """
    project_parts = Path(PROJECT_ROOT).resolve().parts
    target_parts = target.resolve().parts

    # Need at least: project_parts + ["agents", "agent", "session", "basename"]
    if len(target_parts) < len(project_parts) + 4:
        return None, None, None
    if target_parts[: len(project_parts)] != project_parts:
        return None, None, None

    rel_parts = target_parts[len(project_parts):]
    if rel_parts[0] != "agents":
        return None, None, None
    if rel_parts[2] != "session":
        return None, None, None

    agent_name = rel_parts[1]
    basename = target_parts[-1]
    # Anything between "session" and the basename is a directory segment we may
    # match against dir-type manifest entries.
    ancestor_dirs = tuple(rel_parts[3:-1])

    # Sanity check: is this actually an agent directory? Real agents have a
    # local-paths.conf. Without it, treat as out-of-scope so foreign repos
    # nested under PROJECT_ROOT don't get gated by our manifest.
    agent_conf = Path(PROJECT_ROOT) / "agents" / agent_name / "local-paths.conf"
    if not agent_conf.exists():
        return None, None, None

    return agent_name, basename, ancestor_dirs


def _load_manifest_entries():
    """Return (file_names, dir_names) — each a set of registered basenames split
    by entry `type` field. Entries with no `type` (or type=="file") populate
    file_names; entries with type=="dir" populate dir_names. Returns
    (None, None) if the manifest cannot be loaded / parsed / has unexpected
    shape. None signals fail-open to the caller.

    g-115-840: the legacy `_load_manifest_basenames` collapsed both into a
    single set, leaving dir-type entries (e.g., `scratch`) registered for the
    clear-pipeline but invisible to the write-gate dispatch. The split lets
    the gate honor either form symmetrically.
    """
    try:
        with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        return None, None
    except yaml.YAMLError:
        return None, None
    except OSError:
        return None, None

    if not isinstance(data, dict):
        return None, None
    files = data.get("files")
    if not isinstance(files, list):
        return None, None

    file_names: set = set()
    dir_names: set = set()
    for entry in files:
        if not isinstance(entry, dict):
            continue
        name = entry.get("file")
        if not isinstance(name, str) or not name:
            continue
        entry_type = entry.get("type", "file")
        if entry_type == "dir":
            dir_names.add(name)
        else:
            file_names.add(name)
    return file_names, dir_names


def _path_under_registered_dir(ancestor_dirs, dir_names) -> bool:
    """Return True if any path segment in ancestor_dirs matches a registered
    dir-type entry. Implements g-115-840: writes under a manifest-registered
    `type: dir` subtree are allowed, mirroring the clear-pipeline which
    already walks the dir tree on recovery (`recovery_action: clear`). Without
    this, dir-type registrations are half-implemented — registered for
    clearing, not for writing.

    Matches any depth (`scratch/` matches `scratch/foo.txt`, `scratch/sub/`,
    `scratch/sub/deeper/file.txt`). Does NOT prefix-match basename — the
    segment match is exact-equality per directory component.
    """
    if not ancestor_dirs or not dir_names:
        return False
    return any(seg in dir_names for seg in ancestor_dirs)


def _read_agent_mode(agent_name: str) -> str:
    """Read <agent>/session/agent-mode. Returns 'reader'/'assistant'/'autonomous'
    or 'unknown' on any read error. Lowercased for case-insensitive comparison.
    """
    p = Path(PROJECT_ROOT) / "agents" / agent_name / "session" / "agent-mode"
    try:
        return p.read_text(encoding="utf-8").strip().lower()
    except Exception:
        return "unknown"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Gate writes under <agent>/session/ against session-manifest.yaml "
            "registration. Allows out-of-scope and registered paths; gates "
            "unregistered paths by agent-mode."
        )
    )
    ap.add_argument("path", help="Target path the writer wants to create or modify.")
    ap.add_argument("--output", default="human", choices=["json", "human"],
                    help="Output format. 'human' writes severity-prefixed stderr; "
                         "'json' writes a single object to stdout.")
    args = ap.parse_args(argv)

    target = Path(args.path)

    # (a) Out-of-scope: not under any agent's session/.
    agent_name, basename, ancestor_dirs = _find_owning_agent(target)
    if agent_name is None:
        _emit({
            "allowed": True,
            "reason": "out_of_scope",
            "severity": "none",
            "mode": None,
            "agent": None,
            "basename": None,
        }, args.output)
        return

    # (b) Load manifest. Fail-open on any load error.
    loaded = _load_manifest_entries()
    registered_files, registered_dirs = loaded
    mode = _read_agent_mode(agent_name)

    if registered_files is None:
        if args.output != "json":
            print(
                f"[session-manifest-gate] WARN: cannot load {MANIFEST_PATH} — "
                f"failing open", file=sys.stderr
            )
        _emit({
            "allowed": True,
            "reason": "manifest_load_error",
            "severity": "none",
            "mode": mode,
            "agent": agent_name,
            "basename": basename,
        }, args.output)
        return

    # (c) Registered as a file? Allow.
    if basename in registered_files:
        _emit({
            "allowed": True,
            "reason": "registered",
            "severity": "none",
            "mode": mode,
            "agent": agent_name,
            "basename": basename,
        }, args.output)
        return

    # (c.5) Registered under a dir-type entry? Allow. ()
    # Symmetric with the clear-pipeline, which walks dir-type registrations on
    # recovery. Before this branch, scratch/* was registered for clearing but
    # blocked from writing — registered, but not actually usable. The
    # dispatch lives here (between exact-file match and the unregistered
    # mode dispatch) so that file-specific registrations still take
    # precedence and the reason payload distinguishes the two paths.
    if _path_under_registered_dir(ancestor_dirs, registered_dirs):
        matched_segment = next(seg for seg in ancestor_dirs if seg in registered_dirs)
        _emit({
            "allowed": True,
            "reason": f"registered_dir:{matched_segment}",
            "severity": "none",
            "mode": mode,
            "agent": agent_name,
            "basename": basename,
        }, args.output)
        return

    # (d) Unregistered — dispatch by mode.
    reason_text = (
        f"unregistered file '{basename}' under {agent_name}/session/ "
        f"(mode={mode})"
    )

    if mode == "autonomous":
        _emit({
            "allowed": False,
            "reason": reason_text,
            "severity": "block",
            "mode": "autonomous",
            "agent": agent_name,
            "basename": basename,
        }, args.output)
    elif mode == "assistant":
        _emit({
            "allowed": False,
            "reason": reason_text,
            "severity": "warn",
            "mode": "assistant",
            "agent": agent_name,
            "basename": basename,
        }, args.output)
    elif mode == "reader":
        _emit({
            "allowed": True,
            "reason": reason_text,
            "severity": "info",
            "mode": "reader",
            "agent": agent_name,
            "basename": basename,
        }, args.output)
    else:
        _emit({
            "allowed": True,
            "reason": f"{reason_text} — agent-mode unknown, failing open",
            "severity": "info",
            "mode": mode,
            "agent": agent_name,
            "basename": basename,
        }, args.output)


if __name__ == "__main__":
    main()
