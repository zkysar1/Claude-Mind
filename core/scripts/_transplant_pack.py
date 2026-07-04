#!/usr/bin/env python3
# domain-leak-exempt: SECRET_PATTERNS below are FUNCTIONAL credential-detection
# regexes (AWS access-key/secret shapes, the MIND_AWS_*/AYO_OPERATOR_KEY env
# names, private-key headers) used to REFUSE packing a mind that carries a
# secret. They must match the real identifiers, so they are not genericizable.
# No domain example text leaks — these are detection patterns, not prose.
"""Engine for the /transplant skill — relocate a LIVING mind to another machine.

Source-side packer. Four sub-commands:

  own-cloud  Verify the storage backend is own-cloud and the repo is pushed,
             then print the destination bring-up checklist (no archive — git
             carries code+identity, S3 carries world/meta).
  offline    Pack a portable archive: `git archive HEAD` (tracked repo: code +
             agent identity) PLUS the external world/ and meta/ data, minus the
             heavy .history/ snapshots and machine-local transient state. Scans
             for secrets and REFUSES if any are found. Emits a RESTORE.md guide.
  land       Destination-side resume helper. offline: unpack + wire each agent's
             local-paths.conf at the unpacked world/meta. own-cloud: sanity-check
             .env.local + a distinct MACHINE_ID, then point at /start.
  verify     Post-land smoke test at a destination repo.

This script NEVER writes agent-state/agent-mode (guard-340) and never calls
/start — the resume is /start Phase A-0's job. It is read-only on the source
mind except the (separate) own-cloud continuity flush the skill triggers.
"""

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

# Functional credential-detection patterns (see domain-leak-exempt marker above).
# Value-shaped, not name-only, to avoid false positives on knowledge content that
# merely MENTIONS a variable name. Each entry: (compiled-or-raw regex, label).
SECRET_PATTERNS = [
    (r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", "private key block"),
    (r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "aws access key id"),
    (r"(?:AWS_SECRET_ACCESS_KEY|MIND_AWS_SECRET_ACCESS_KEY)\s*[=:]\s*[\"']?[A-Za-z0-9/+]{40}",
     "aws secret access key"),
    (r"(?:AYO_OPERATOR_KEY|MIND_AWS_ACCESS_KEY_ID|AWS_ACCESS_KEY_ID)\s*[=:]\s*[\"']?[A-Za-z0-9/+_\-]{16,}",
     "credential assignment"),
]
_SECRET_RE = [(re.compile(p), label) for p, label in SECRET_PATTERNS]

# Dirs never copied out of world/meta (machine-local / transient / bloat).
# .history is conditional on --include-history (added at runtime).
_BASE_EXCLUDE_DIRS = {"__pycache__", "session", "sessions", ".git", ".pytest_cache"}
_EXCLUDE_SUFFIXES = (".lock", ".pyc", ".pyo")
# Files that, if present at a destination, indicate machine-local runner state
# leaked into the pack (must never travel).
_LEAK_NAMES = ("agent-state", "running-session-id", "runner-token", "runner-heartbeat")


def _eprint(*a):
    print(*a, file=sys.stderr)


def _fail(msg, code=1):
    _eprint(f"[transplant] ERROR: {msg}")
    sys.exit(code)


def _is_archive(path: Path) -> bool:
    n = path.name.lower()
    return n.endswith(".zip") or n.endswith(".tar.gz") or n.endswith(".tgz")


def _win_long(p):
    r"""Windows >260-char (MAX_PATH) support: prefix an absolute path with the
    \\?\ extended-length marker so direct open() / os.makedirs() / os.unlink()
    can exceed 260 chars. No-op off Windows and for already-prefixed paths; maps
    a UNC \\server\share to \\?\UNC\server\share.

    Reliable ONLY for direct single-path file ops — NOT for shutil.copytree /
    shutil.rmtree / os.walk enumeration, which proved flaky against \\?\ on this
    platform (2026-06-04). That is why the offline pack streams source->archive
    (relative members, no staging copy) and land extracts members one-by-one to
    \\?\-prefixed targets, instead of copytree-ing deep trees on disk."""
    if os.name != "nt":
        return os.fspath(p)
    s = os.fspath(p)
    if s.startswith("\\\\?\\"):          # already extended-length — idempotent, do not re-normalize
        return s
    s = os.path.abspath(s)               # absolute + backslash-normalized on Windows
    if s.startswith("\\\\"):             # UNC \\server\share -> \\?\UNC\server\share
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def _safe_member(arcname):
    """Reject path-traversal / absolute archive members (the safety the tar
    'data' filter gave us; we now extract members by hand). Returns the cleaned
    POSIX-relative arcname, or None if it must be skipped."""
    arc = arcname.replace("\\", "/").lstrip("/")
    parts = [seg for seg in arc.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts) or (len(arcname) > 1 and arcname[1] == ":"):
        return None
    return "/".join(parts)


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _git_state(project_root: Path):
    """Return (clean, ahead, branch, has_origin, detail). ahead is int or None."""
    st = _git(["status", "--porcelain"], project_root)
    clean = st.returncode == 0 and not st.stdout.strip()
    br = _git(["branch", "--show-current"], project_root)
    branch = br.stdout.strip() or "(detached)"
    remotes = _git(["remote"], project_root)
    has_origin = "origin" in remotes.stdout.split()
    ahead = None
    detail = ""
    if has_origin:
        # Count commits local has that origin/<branch> does not.
        rl = _git(["rev-list", "--count", f"origin/{branch}..HEAD"], project_root)
        if rl.returncode == 0 and rl.stdout.strip().isdigit():
            ahead = int(rl.stdout.strip())
        else:
            detail = "could not compare to origin/" + branch + " (fetch first?)"
    return clean, ahead, branch, has_origin, detail


def _scan_secrets(root: Path):
    """Walk text files under root; return list of (relpath, label) secret hits."""
    hits = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            if p.stat().st_size > 2_000_000:  # skip large binaries/blobs
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rx, label in _SECRET_RE:
            if rx.search(text):
                hits.append((str(p.relative_to(root)), label))
                break
    return hits


def _agent_names(repo_root: Path):
    """Agent dir names under repo_root/agents that carry a .initialized marker."""
    base = repo_root / "agents"
    if not base.is_dir():
        return []
    return sorted(d.name for d in base.iterdir()
                  if d.is_dir() and (d / ".initialized").is_file())


# ---------------------------------------------------------------------------
# own-cloud
# ---------------------------------------------------------------------------
def cmd_own_cloud(args):
    project_root = Path(args.project_root)
    if args.backend != "own-cloud":
        _fail(
            f"storage backend is '{args.backend}', not 'own-cloud'. "
            "For a no-cloud / emailable move use:  /transplant offline --out <path>",
            code=2,
        )

    clean, ahead, branch, has_origin, detail = _git_state(project_root)
    blocking = False
    print("=== /transplant own-cloud — source readiness ===")
    print(f"backend         : own-cloud  OK")
    print(f"branch          : {branch}")
    if not clean:
        print("git tree        : DIRTY  — commit before transplant (clone would be stale)")
        blocking = True
    else:
        print("git tree        : clean  OK")
    if not has_origin:
        print("origin remote   : MISSING — destination clones from origin; add a remote + push")
        blocking = True
    elif ahead is None:
        print(f"origin/{branch} : UNKNOWN — {detail}")
    elif ahead > 0:
        print(f"origin/{branch} : behind by {ahead} commit(s) — push before transplant")
        blocking = True
    else:
        print(f"origin/{branch} : up to date  OK")

    if blocking and not args.force:
        _eprint("")
        _eprint("[transplant] REFUSING: the destination clones the repo, so it must be "
                "committed + pushed first. Commit and push (agent-capable per "
                "world/conventions/post-execution.md), then re-run. --force overrides.")
        # Still print the checklist below so the user sees the full picture, but
        # exit non-zero so callers know it is not yet ready.

    agents = [a for a in (args.agents or args.owned_agents or "").replace(",", " ").split() if a]
    agents_str = ",".join(agents) if agents else "<agent>"

    print("")
    print("=== destination bring-up checklist (run on the NEW machine) ===")
    print(f"1. git clone <origin-url>            # brings core/, .claude/, and tracked agents/")
    print(f"2. py -3 -m pip install -r mind_api/requirements-owncloud.txt  # boto3 + base deps")
    print(f"     # the own-cloud daemon will not start without boto3")
    print(f"3. create .env.local from .env.example with:")
    print(f"     STORAGE_BACKEND=own-cloud")
    print(f"     ENVIRONMENT_ID=<same as source>            # shared world")
    print(f"     MACHINE_ID=<DISTINCT per-machine id>   # MUST differ from source (G5 fail-closed)")
    print(f"     STORAGE_S3_BUCKET / STORAGE_DDB_LOCK_TABLE / STORAGE_DDB_SESSIONS_TABLE  # same as source")
    print(f"     MIND_AWS_ACCESS_KEY_ID / MIND_AWS_SECRET_ACCESS_KEY  # the scoped least-privilege keys")
    print(f"     AWS_DEFAULT_REGION=<region>")
    print(f"4. point each moved agent's local-paths.conf at FRESH empty WORLD_PATH/META_PATH")
    print(f"     (cache populates from S3 on read; /start writes a default if absent)")
    print(f"5. ON THIS (source) machine: /stop {agents_str}  # flushes to S3 + releases DDB claim")
    print(f"6. /start {agents_str}               # Phase A-0 detects the clone and resumes it")
    print(f"     # Dynamic ownership: DDB claim IS the ownership signal. No env edits needed.")
    print("")
    print("Next (this machine): the skill flushes continuity to S3 (owncloud-flush.sh) "
          "so the destination picks up your latest handoff/working-memory on first read.")

    sys.exit(1 if (blocking and not args.force) else 0)


# ---------------------------------------------------------------------------
# offline
# ---------------------------------------------------------------------------
def cmd_offline(args):
    project_root = Path(args.project_root)
    world = Path(args.world) if args.world else None
    meta = Path(args.meta) if args.meta else None

    if not world or not world.is_dir():
        _fail(f"WORLD_PATH not resolved or missing: {args.world!r}. "
              "Configure agents/<agent>/local-paths.conf or run from a bound agent.")
    if not meta or not meta.is_dir():
        _fail(f"META_PATH not resolved or missing: {args.meta!r}.")

    if args.backend == "own-cloud":
        _eprint("[transplant] WARNING: backend is own-cloud — your local world/meta are a "
                "PARTIAL S3 cache, so an offline pack may be incomplete. Prefer "
                "/transplant own-cloud, or fully materialize the cache from S3 first.")

    # `git archive HEAD` packs the last COMMIT, so a dirty working tree means
    # uncommitted changes to TRACKED files (code, agent identity) won't travel.
    # own-cloud mode REFUSES on dirty; offline only WARNS (world/meta are packed
    # live from disk regardless, so the pack is still usable — just commit first
    # if recent tracked edits matter).
    _st = _git(["status", "--porcelain"], project_root)
    if _st.returncode == 0 and _st.stdout.strip():
        _eprint("[transplant] WARNING: working tree is dirty — `git archive HEAD` packs the "
                "last COMMIT, so uncommitted changes to TRACKED files will NOT be in the pack "
                "(world/meta ARE packed live). Commit first if those edits should travel.")

    exclude_dirs = set(_BASE_EXCLUDE_DIRS)
    if not args.include_history:
        exclude_dirs.add(".history")

    # Enumerate world/ + meta/ source files (honoring excludes) as
    # (abspath, archive-member) pairs, streamed STRAIGHT into the archive with
    # RELATIVE member names — there is NO on-disk staging copy. The previous
    # design copytree'd world/meta into PROJECT_ROOT/.transplant-stage-* first,
    # which DOUBLED the path prefix and blew Windows MAX_PATH (260) on deep
    # knowledge-tree nodes (a 242-char source became a 266-char stage path ->
    # WinError 3), AND left an un-deletable partial stage on failure. Source
    # paths are read-safe (<260) and archive members are relative (no path
    # limit), so streaming sidesteps the limit at the root. (2026-06-04.)
    def _collect(root, prefix):
        items = []
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in sorted(dns) if d not in exclude_dirs]
            for f in sorted(fns):
                if f.endswith(_EXCLUDE_SUFFIXES):
                    continue
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                items.append((full, f"{prefix}/{rel}"))
        return items

    world_items = _collect(world, "world")
    meta_items = _collect(meta, "meta")

    # secret-scan the SOURCE files we are about to pack — REFUSE on any hit.
    # (Nothing staged yet, so on a hit we simply never write the archive.)
    hits = _scan_files(world_items) + _scan_files(meta_items)
    if hits:
        lines = "\n".join(f"    {arc}  ({label})" for arc, label in hits[:20])
        _fail("secret-like content found in world/meta — REFUSING to pack. "
              "Secrets belong only in .env.local (never packed). Remove these, "
              f"then re-run:\n{lines}")

    agents = _agent_names(project_root)
    restore = _restore_md(agents)

    # git archive HEAD -> a SHORT-path temp tar (tracked repo: code + identity).
    # git-archive member paths are repo-relative (<100 chars), so the tar carries
    # no long-path risk; it lives under the system temp (never PROJECT_ROOT), so
    # a failed pack can never leave stage cruft in the repo.
    tmpdir = Path(tempfile.mkdtemp(prefix="mind-tp-"))
    try:
        repo_tar = tmpdir / "repo.tar"
        with open(repo_tar, "wb") as fh:
            p = subprocess.run(["git", "archive", "HEAD"], cwd=str(project_root),
                               stdout=fh, stderr=subprocess.PIPE)
        if p.returncode != 0:
            _fail(f"git archive failed: {p.stderr.decode(errors='replace').strip()}")

        with tarfile.open(repo_tar) as tf:
            repo_members = [m for m in tf.getmembers() if m.isfile()]
        repo_nfiles = len(repo_members)
        repo_bytes = sum(m.size for m in repo_members)
        wm_bytes = sum(os.path.getsize(f) for f, _ in world_items + meta_items)
        total = repo_bytes + wm_bytes
        nfiles = repo_nfiles + len(world_items) + len(meta_items) + 1  # +RESTORE.md

        if args.dry_run:
            print("=== /transplant offline (dry run) ===")
            print(f"would pack : {nfiles} files, {_human(total)} uncompressed "
                  f"(the .zip/.tar.gz will be notably smaller — text compresses ~3x)")
            print(f"repo (git) : {repo_nfiles} tracked files")
            print(f"world from : {world}  ({len(world_items)} files)")
            print(f"meta from  : {meta}  ({len(meta_items)} files)")
            print(f"agents     : {', '.join(agents) or '(none with .initialized)'}")
            print(f"excludes   : {'(.history INCLUDED) ' if args.include_history else '.history, '}"
                  "*.lock, __pycache__, session/, sessions/")
            print("secret-scan: clean")
            print("(dry run — no archive written)")
            return

        out = _resolve_out(args.out, project_root)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.name.lower().endswith(".zip"):
            skipped = _build_zip(out, repo_tar, world_items, meta_items, restore)
        else:
            skipped = _build_targz(out, repo_tar, world_items, meta_items, restore)
        size = out.stat().st_size
        if skipped:
            _eprint(f"[transplant] WARN: {len(skipped)} world/meta file(s) vanished or became "
                    f"unreadable mid-pack (live cache) and were skipped: {skipped[:5]}")

        print("=== /transplant offline ===")
        print(f"archive : {out}")
        print(f"size    : {_human(size)}  ({nfiles} files)")
        print(f"agents  : {', '.join(agents) or '(none)'}")
        if size > 25 * 1024 * 1024:
            print(f"WARN    : {_human(size)} exceeds ~25 MB (typical email-attachment limit). "
                  "Use a drive/USB or a transfer service, or confirm --include-history "
                  "was not set by mistake.")
        print("next    : copy to the new machine, then  /transplant land "
              + out.name + "  (or unzip + /start)")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)  # holds only repo.tar (short) — safe


def _restore_md(agents):
    agent_list = ", ".join(agents) if agents else "<agent>"
    first = agents[0] if agents else "<agent>"
    return f"""# RESTORE — offline transplant of a living mind

This archive carries a *living* mind: the framework repo (code + agent identity)
plus its `world/` and `meta/` learned data. Machine-local runner state and
secrets were intentionally NOT packed.

Agents in this pack: {agent_list}

## Restore on the new machine

1. Unpack to where you want the repo to live (or use `/transplant land <this-archive>`,
   which does steps 1-3 for you):
   - `repo/`  -> the repo root (e.g. a fresh empty dir or a clean clone)
   - `world/` and `meta/` -> a data dir, e.g. `<repo>/.mind-data/world` and `.../meta`
2. For each agent, write `agents/<name>/local-paths.conf`:
   ```
   WORLD_PATH=<repo>/.mind-data/world
   META_PATH=<repo>/.mind-data/meta
   ```
   (`/transplant land` writes these for you. If absent, /start Phase A-0 would
   instead write an own-cloud cache default — not what you want offline.)
3. Storage is LOCAL files (no S3) — no AWS creds needed. If your domain work needs
   other API keys, create `.env.local` from `.env.example` and fill them in. This
   is the only step that can't be automated (secrets never travel).
4. Install prerequisites:  `py -3 -m pip install -r requirements.txt`  (PyYAML + psutil; offline is local-files, so no cloud SDK / boto3 needed)
5. Resume the agent:  `/start {first}`
   Phase A-0 detects the unpacked agent (its `.initialized` marker travelled) and
   resumes it as an existing agent — no re-initialization, no identity clobber.
"""


# ---------------------------------------------------------------------------
# land
# ---------------------------------------------------------------------------
def cmd_land(args):
    dest = Path(args.dest) if args.dest else Path(args.project_root)
    target = Path(args.target) if args.target else None

    # own-cloud land: no archive — sanity-check the env, point at /start.
    if not target or not _is_archive(target):
        envf = dest / ".env.local"
        print("=== /transplant land (own-cloud) ===")
        if not envf.is_file():
            _fail(f".env.local missing at {dest}. Create it from .env.example "
                  "(distinct MACHINE_ID, scoped MIND_AWS_* creds) before /start.")
        txt = envf.read_text(encoding="utf-8", errors="ignore")
        mid = ""
        for line in txt.splitlines():
            if line.strip().startswith("MACHINE_ID="):
                _val = line.split("=", 1)[1].strip().split()  # empty value -> [] (no IndexError)
                mid = _val[0] if _val else ""
                mid = "" if mid.startswith("#") else mid
        if not mid:
            _fail("MACHINE_ID is empty in .env.local — set a DISTINCT per-machine "
                  "id (G5 fail-closed) before /start.")
        print(f".env.local      : present  OK")
        print(f"MACHINE_ID : {mid}  (confirm this DIFFERS from the source machine)")
        print("next            : /start <agent>   (Phase A-0 resumes the cloned agent)")
        return

    # offline land: unpack the archive DIRECTLY to final paths (long-path-safe
    # single-file writes) and wire local-paths.conf. We do NOT extract to a temp
    # dir then copytree into place — that double-copy re-hit Windows MAX_PATH on
    # deep world trees, the same class the pack fix removes. Each member is
    # written via _win_long() so a destination path > 260 chars still succeeds,
    # and traversal/absolute members are rejected by _safe_member. (2026-06-04.)
    if not target.is_file():
        _fail(f"pack not found: {target}")
    print("=== /transplant land (offline) ===")
    data = dest / ".mind-data"

    def _route(arc):
        # repo/<p> -> dest/<p> ; world/<p> -> dest/.mind-data/world/<p> ;
        # meta/<p> -> dest/.mind-data/meta/<p> ; RESTORE.md etc -> dest/<name>.
        clean = _safe_member(arc)
        if clean is None:
            return None
        parts = clean.split("/")
        if parts[0] in ("repo", "world", "meta") and len(parts) == 1:
            return None  # bare top-level dir entry, nothing to write
        if parts[0] == "repo":
            return dest.joinpath(*parts[1:])
        if parts[0] == "world":
            return data.joinpath("world", *parts[1:])
        if parts[0] == "meta":
            return data.joinpath("meta", *parts[1:])
        return dest.joinpath(*parts)

    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()) and not args.force:
        _fail(f"destination {dest} is not empty. Use --force to merge the pack in.")

    is_zip = target.name.lower().endswith(".zip")
    # Fail fast if this is not a /transplant offline pack (must carry repo/).
    if is_zip:
        with zipfile.ZipFile(target) as z:
            names = z.namelist()
    else:
        with tarfile.open(target) as t:
            names = [m.name for m in t.getmembers()]
    if not any((_safe_member(n) or "").startswith("repo/") for n in names):
        _fail("pack has no repo/ — not a /transplant offline archive?")

    nwritten = 0
    if is_zip:
        with zipfile.ZipFile(target) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                tgt = _route(info.filename)
                if tgt is None:
                    continue
                os.makedirs(_win_long(tgt.parent), exist_ok=True)
                with z.open(info) as src, open(_win_long(tgt), "wb") as dst:
                    shutil.copyfileobj(src, dst)
                nwritten += 1
    else:
        with tarfile.open(target) as t:
            for m in t.getmembers():
                if not m.isfile():
                    continue
                tgt = _route(m.name)
                if tgt is None:
                    continue
                src = t.extractfile(m)
                if src is None:
                    continue
                os.makedirs(_win_long(tgt.parent), exist_ok=True)
                with src, open(_win_long(tgt), "wb") as dst:
                    shutil.copyfileobj(src, dst)
                nwritten += 1

    agents = _agent_names(dest)
    for name in agents:
        conf = dest / "agents" / name / "local-paths.conf"
        conf.write_text(
            "# Written by /transplant land (offline). Points at the unpacked\n"
            "# world/meta data. Local-files backend — no S3.\n"
            f"WORLD_PATH={(data / 'world').as_posix()}\n"
            f"META_PATH={(data / 'meta').as_posix()}\n",
            encoding="utf-8",
        )
    print(f"unpacked repo   : {dest}")
    print(f"world/meta data : {data}")
    print(f"files written   : {nwritten}")
    print(f"agents wired    : {', '.join(agents) or '(none)'}")
    print("next            : create .env.local if your domain needs API keys, then")
    print(f"                  py -3 -m pip install -r requirements.txt   # PyYAML + psutil (offline is local-files, no boto3)")
    print(f"                  /start {agents[0] if agents else '<agent>'}")


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
def cmd_verify(args):
    dest = Path(args.dest) if args.dest else Path(args.project_root)
    passes, fails, warns = [], [], []

    agents = _agent_names(dest)
    if agents:
        passes.append(f"identity: {len(agents)} agent(s) with .initialized ({', '.join(agents)})")
    else:
        fails.append("identity: no agents/<name>/.initialized found at destination")
    for name in agents:
        adir = dest / "agents" / name
        for f in ("self.md", "aspirations.jsonl", "curriculum.yaml"):
            if not (adir / f).is_file():
                warns.append(f"identity: {name}/{f} missing")

    # machine-local leakage
    leaked = []
    for name in agents:
        adir = dest / "agents" / name
        if (adir / "session").is_dir():
            leaked.append(f"{name}/session/")
        if (adir / "sessions").is_dir():
            leaked.append(f"{name}/sessions/")
    if list(dest.glob(".active-agent-*")) or (dest / ".active-agent").exists():
        leaked.append(".active-agent-*")
    for name in agents:
        for f in dest.glob(f"agents/{name}/session/*"):
            if f.name in _LEAK_NAMES:
                leaked.append(f"{name}/session/{f.name}")
    if leaked:
        warns.append("machine-local state present (will be re-scaffolded by /start, "
                     f"but should not have travelled): {', '.join(sorted(set(leaked)))}")
    else:
        passes.append("no machine-local runner state leaked")

    # secret leakage
    if (dest / ".env.local").is_file():
        warns.append(".env.local present at destination (fine if you created it here; "
                     "a problem only if it travelled in a pack)")
    data = dest / ".mind-data"
    scan_roots = [d for d in (data / "world", data / "meta") if d.is_dir()]
    sec = []
    for r in scan_roots:
        sec += _scan_secrets(r)
    if sec:
        fails.append("secret-like content in packed world/meta: "
                     + ", ".join(f"{rel} ({lbl})" for rel, lbl in sec[:10]))
    elif scan_roots:
        passes.append("secret-scan of packed world/meta: clean")

    # world/meta reachable (offline: local-paths.conf points at existing dirs)
    for name in agents:
        conf = dest / "agents" / name / "local-paths.conf"
        if conf.is_file():
            wp = ""
            for line in conf.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("WORLD_PATH="):
                    wp = line.split("=", 1)[1].strip()
            if wp and Path(wp).is_dir() and any(Path(wp).iterdir()):
                passes.append(f"{name}: world path reachable ({wp})")
            elif wp:
                warns.append(f"{name}: WORLD_PATH {wp} missing/empty "
                             "(own-cloud rehydrates from S3; offline expects data here)")
            break

    print("=== /transplant verify ===")
    for p in passes:
        print(f"  PASS  {p}")
    for w in warns:
        print(f"  WARN  {w}")
    for f in fails:
        print(f"  FAIL  {f}")
    print(f"--- {len(passes)} pass / {len(warns)} warn / {len(fails)} fail ---")
    sys.exit(1 if fails else 0)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def _resolve_out(out, project_root):
    if out:
        p = Path(out)
    else:
        ext = ".zip" if os.name == "nt" else ".tar.gz"
        p = project_root.parent / f"mind-transplant-{datetime.now():%Y%m%d-%H%M%S}{ext}"
    if not _is_archive(p):
        p = Path(str(p) + (".zip" if os.name == "nt" else ".tar.gz"))
    return p


def _scan_files(items):
    """Secret-scan an explicit list of (abspath, arcname); return [(arcname,
    label)] hits. Like _scan_secrets but over a file list, so it honors the
    pack's exclude rules and scans the source in place (no staging copy)."""
    hits = []
    for full, arc in items:
        try:
            if os.path.getsize(full) > 2_000_000:  # skip large binaries/blobs
                continue
            with open(full, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        for rx, label in _SECRET_RE:
            if rx.search(text):
                hits.append((arc, label))
                break
    return hits


def _build_zip(out, repo_tar, world_items, meta_items, restore):
    """Stream the repo (from the git-archive tar, under repo/), then the
    world/meta SOURCE files (relative members), then RESTORE.md, into a .zip.
    Source paths are <260 (read-safe) and members are relative (no path limit),
    so there is no MAX_PATH exposure even for deep trees. Returns the list of
    arcnames skipped because the source vanished/became unreadable mid-pack
    (a live own-cloud cache can mutate under us — a snapshot skips, never crashes)."""
    skipped = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        with tarfile.open(repo_tar) as src:
            for m in src.getmembers():
                if not m.isfile():
                    continue
                f = src.extractfile(m)
                if f is not None:
                    z.writestr(f"repo/{m.name}", f.read())
        for full, arc in world_items + meta_items:
            try:
                z.write(full, arc)
            except OSError:
                skipped.append(arc)
        z.writestr("RESTORE.md", restore)
    return skipped


def _build_targz(out, repo_tar, world_items, meta_items, restore):
    """tar.gz analogue of _build_zip (the default archive format off Windows).
    Returns the list of arcnames skipped because the source vanished mid-pack."""
    skipped = []
    with tarfile.open(out, "w:gz") as t:
        with tarfile.open(repo_tar) as src:
            for m in src.getmembers():
                if not m.isfile():
                    continue
                f = src.extractfile(m)
                if f is None:
                    continue
                ti = tarfile.TarInfo(f"repo/{m.name}")
                ti.size, ti.mtime, ti.mode = m.size, m.mtime, m.mode
                t.addfile(ti, f)
        for full, arc in world_items + meta_items:
            try:
                t.add(full, arcname=arc)
            except OSError:
                skipped.append(arc)
        rb = restore.encode("utf-8")
        ti = tarfile.TarInfo("RESTORE.md")
        ti.size = len(rb)
        t.addfile(ti, io.BytesIO(rb))
    return skipped


def main():
    ap = argparse.ArgumentParser(prog="_transplant_pack.py")
    ap.add_argument("subcommand", choices=["own-cloud", "offline", "land", "verify"])
    ap.add_argument("--project-root", required=True)
    ap.add_argument("--world", default="")
    ap.add_argument("--meta", default="")
    ap.add_argument("--backend", default="local")
    ap.add_argument("--owned-agents", default="")
    # sub-command flags
    ap.add_argument("--agents", default="")          # own-cloud checklist override
    ap.add_argument("--out", default="")             # offline output path
    ap.add_argument("--include-history", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dest", default="")            # land/verify destination repo
    ap.add_argument("target", nargs="?", default="")  # land: pack path
    args = ap.parse_args()

    if args.subcommand == "own-cloud":
        cmd_own_cloud(args)
    elif args.subcommand == "offline":
        cmd_offline(args)
    elif args.subcommand == "land":
        cmd_land(args)
    elif args.subcommand == "verify":
        cmd_verify(args)


if __name__ == "__main__":
    main()
