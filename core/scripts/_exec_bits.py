"""Executable-bit resolution shared by the promotion preflight and the seed plant.

WHY THIS MODULE EXISTS (g-360-13)
---------------------------------
The exec bit fails in the SILENT direction: a git hook or wrapper that loses
u+x does not error, it simply never runs, so a downstream Mind adopting a tag
runs with its commit gates absent and nothing red anywhere.

Detection landed twice before preservation did -- g-360-07 (source-index
mode-strip block) and g-360-09 (preflight exec_bits / mode_differing fields)
both DETECT a strip; neither made the plant PRESERVE the bit. Measured on
v2.12.5: staging held 0 x 100755 and all 628 source-executable paths came out
100644, restored by hand as a757343.

These three helpers were authored in promotion-preflight.py by g-360-09 and are
hosted here so the plant consumes the SAME logic rather than a second copy
(communication-clarity rule 5 -- one source of truth, no parallel drift).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def exec_bits(p: Path) -> int | None:
    """The EXECUTE bits of a file (0o111 mask), or None if unreadable.

    Only the execute bits are compared. Read/write bits track each box's umask
    and would make mode drift a pure noise generator; the execute bit is the one
    that changes BEHAVIOUR, and it fails in the silent direction (g-360-09).
    """
    try:
        return p.stat().st_mode & 0o111
    except OSError:
        return None


def index_exec_map(repo_root: Path) -> dict[str, bool]:
    """rel-path -> is-executable, read from git's INDEX (`git ls-tree -r HEAD`).

    THE INDEX MODE IS THE ONE THAT PROPAGATES (g-360-07, guard-844). A promotion
    commits what the index says; `chmod +x` alone does not travel. The filesystem
    mode is also the dimension that DISAPPEARS on a checkout whose mount flattens
    permissions -- precisely the boxes where exec_bits() reports "cannot see" and
    the whole check goes blind. Reading the index restores the signal there.

    Returns {} on any failure (not a git repo, git absent, detached/empty HEAD).
    An empty map is a DECLINE, not a clean result: callers fall back to
    exec_bits() per file and must never read {} as "no drift".
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-tree", "-r", "HEAD"],
            capture_output=True, text=True, timeout=120,
        )
    except Exception:
        return {}
    if out.returncode != 0:
        return {}
    m: dict[str, bool] = {}
    for line in out.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        fields = parts[0].split()
        if not fields:
            continue
        # 100755 = executable blob, 100644 = regular. Anything else (120000
        # symlink, 160000 gitlink) is not a mode this check reasons about.
        if fields[0] in ("100755", "100644"):
            m[parts[1].replace("\\", "/")] = fields[0] == "100755"
    return m


def resolve_exec(rel: str, abs_path: Path, idx: dict[str, bool]) -> tuple[bool | None, str]:
    """(is_executable, source) preferring the INDEX, falling back to the filesystem.

    source is "index" or "fs" so a reader can tell which dimension produced a
    verdict -- a file staged-but-not-in-HEAD legitimately has no index entry and
    must not be silently treated as non-executable (that would invent a
    regression out of an absence).
    """
    if rel in idx:
        return idx[rel], "index"
    bits = exec_bits(abs_path)
    return (None if bits is None else bool(bits)), "fs"


def carry_exec_bit(rel: str, src: Path, dst: Path, idx: dict[str, bool]) -> bool:
    """Apply the SOURCE's executable bit to a freshly-WRITTEN dest file.

    ADD-ONLY, and that asymmetry is the whole safety argument. resolve_exec
    returns None when neither dimension can see the bit; `not None` is falsey so
    an unknown leaves the destination exactly as written. A false negative
    therefore reproduces the pre-fix status quo (0644, recoverable by the same
    hand chmod that shipped before), while a blind two-way sync could STRIP a
    bit the destination legitimately holds -- the silent-failure direction this
    module exists to close.

    Returns True only when a bit was actually added.
    """
    is_exec, _source = resolve_exec(rel, src, idx)
    if not is_exec:
        return False
    try:
        mode = dst.stat().st_mode
        if mode & 0o111 == 0o111:
            return False
        dst.chmod(mode | 0o111)
        return True
    except OSError:
        return False


def staged_exec_map(repo_root: Path) -> dict[str, bool]:
    """rel-path -> is-executable, read from git's INDEX proper (`git ls-files -s`).

    index_exec_map() reads the HEAD TREE, which is right for a committed source
    (a worktree pinned at a tag). The DESTINATION of a plant is the opposite
    case: right after `git add -A` the planted files are staged and not yet
    committed, so HEAD is the pre-plant tree and says nothing about them. This
    reads the index itself -- which is also exactly what the commit will record.

    Same decline semantics as index_exec_map: {} means "could not read", never
    "nothing is executable".
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "-c", "core.quotePath=false",
             "ls-files", "-s", "-z"],
            capture_output=True, text=True, timeout=120,
        )
    except Exception:
        return {}
    if out.returncode != 0:
        return {}
    m: dict[str, bool] = {}
    for entry in out.stdout.split("\0"):
        if not entry:
            continue
        meta, _tab, rel = entry.partition("\t")
        fields = meta.split()
        # "<mode> <object> <stage>\t<path>" -- skip unmerged stages and non-blobs.
        if len(fields) < 3 or fields[2] != "0" or fields[0] not in ("100755", "100644"):
            continue
        m[rel.replace("\\", "/")] = fields[0] == "100755"
    return m


def carry_index_exec_bits(source_root: Path, dest_root: Path,
                          source_map: dict[str, bool] | None = None) -> dict:
    """Set 100755 in the DESTINATION INDEX for every path the SOURCE marks
    executable -- the git-level carry that works where the filesystem one cannot.

    WHY (g-360-16): carry_exec_bit() chmods the staged FILE, and that is real
    only where the filesystem has an execute bit. On a Windows clone
    (core.fileMode=false, git's default there) os.chmod cannot set one and does
    not error, the counters above say "carried", and the destination's `git add`
    records every NEW file at 100644 while already-tracked files keep their
    mode -- measured on the v2.12.47 hop, 15 files. The INDEX mode is the one
    that propagates, so set it there directly (`git update-index --chmod=+x`)
    after the destination's `git add` has staged the planted files.

    ADD-ONLY like carry_exec_bit: a path the source does not mark executable is
    never touched; a source-executable path absent from the destination index
    (excluded by the manifest, renamed by a transform) is counted, not invented.
    Verified by RE-READING the index afterwards -- the artifact, never the rc.
    """
    src = source_map if source_map is not None else index_exec_map(source_root)
    if not src:
        return {"pass": False, "candidates": 0, "updated": 0,
                "error": f"source executable map unreadable at {source_root} "
                         "(not a git repo, or an empty HEAD)"}
    if not (dest_root / ".git").exists():
        return {"pass": False, "candidates": 0, "updated": 0,
                "error": f"destination has no git index at {dest_root}"}
    dst = staged_exec_map(dest_root)
    want = [rel for rel, is_x in src.items() if is_x]
    to_set = sorted(rel for rel in want if rel in dst and not dst[rel])
    result = {
        "pass": True,
        "candidates": len(want),
        "updated": 0,
        "already_executable": sum(1 for rel in want if rel in dst and dst[rel]),
        "not_in_dest": sum(1 for rel in want if rel not in dst),
        "updated_paths": to_set[:200],
    }
    if not to_set:
        return result
    try:
        proc = subprocess.run(
            ["git", "-C", str(dest_root), "update-index", "--chmod=+x", "-z", "--stdin"],
            input="".join(rel + "\0" for rel in to_set),
            capture_output=True, text=True, timeout=300,
        )
    except Exception as e:  # git absent / timeout: the carry did not happen
        result["pass"] = False
        result["error"] = f"git update-index failed to run: {e}"
        return result
    if proc.returncode != 0:
        result["pass"] = False
        result["error"] = f"git update-index rc={proc.returncode}: {proc.stderr.strip()[:300]}"
        return result
    after = staged_exec_map(dest_root)
    still = [rel for rel in to_set if not after.get(rel)]
    result["updated"] = len(to_set) - len(still)
    if still:
        result["pass"] = False
        result["still_stripped"] = still[:50]
        result["error"] = f"{len(still)} path(s) still 100644 after update-index"
    return result


def verify_index_exec_bits(source_root: Path, dest_root: Path,
                           source_map: dict[str, bool] | None = None) -> dict:
    """Compare executable modes SOURCE index vs DESTINATION index for every path
    both carry -- the post-plant check the plant's own counters cannot be
    (guard-5806): the counters report what the copy DID, this reads what git
    RECORDED. Blindness is reported as a failure, never as clean (guard-1947);
    a destination with no git index yet is a SKIP, because modes only exist
    once something is committed there.
    """
    src = source_map if source_map is not None else index_exec_map(source_root)
    if not src:
        return {"pass": False, "checked": 0, "stripped": [],
                "error": f"source executable map unreadable at {source_root} "
                         "(not a git repo, or an empty HEAD)"}
    if not (dest_root / ".git").exists():
        return {"pass": True, "checked": 0, "stripped": [],
                "skipped": f"destination has no git index at {dest_root} "
                           "-- index modes exist only once committed"}
    dst = staged_exec_map(dest_root)
    if not dst:
        return {"pass": False, "checked": 0, "stripped": [],
                "error": f"destination index unreadable or empty at {dest_root}"}
    shared = [rel for rel, is_x in src.items() if is_x and rel in dst]
    stripped = sorted(rel for rel in shared if not dst[rel])
    return {
        "pass": not stripped,
        "checked": len(shared),
        "stripped": stripped,
        "not_in_dest": sum(1 for rel, is_x in src.items() if is_x and rel not in dst),
    }
