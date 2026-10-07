#!/usr/bin/env python3
"""temp_decisions.py — the decision log that governs agents/<agent>/temp/.

WHY THIS EXISTS (user directive, 2026-10-05)
--------------------------------------------
Until this file, temp-drain-purge.sh deleted most of temp/ WITHOUT anyone
looking: .log/.txt/.py/.sh/.err/.raw/.out/.bak and empty files after 120
minutes, and every folder untouched for 120 minutes, recursively. Scripts were
in that list, nothing recorded what went, and 20 cited .py files had already
been lost that way (tree node temp-store-reference-integrity). The user's four
principles, verbatim in spirit:

  1. Nothing is deleted until a review has seen it — scripts, logs and folders
     too, not only odd file types.
  2. The review has real destinations: scripts to world/scripts or
     core/scripts, knowledge to the tree, lessons to the lesson stores, data
     worth keeping to a receipted archive, junk deleted.
  3. Every decision is recorded, so anyone can see what was deleted and why.
  4. Obvious junk is handled in bulk (empty files, logs of finished runs),
     decided by name in one pass, but still recorded per item.

So the purge no longer decides anything. This module owns the decisions; the
purge only executes `discard` decisions, behind its existing guards.

THE LOG
-------
temp/.temp-decisions.jsonl, append-only, written ONLY by this module. A
decision row:
  {"ts","item","kind","bytes","files"(dir),"fp","decision","why","where"?,"by","host"}
A deletion row (written by the purge through `log-deleted`):
  {"ts","item","kind","event":"deleted","lane","why","fp"?,"bytes"?,"by","host"}

The CURRENT decision for a name is the latest decision row after the last
deletion row for that name, and it applies only while the item's fingerprint
still matches: a file edited after review is pending again. `keep` expires
after KEEP_DAYS so a kept item comes back instead of becoming permanent slush.

MACHINE-LOCAL BY DESIGN. The log describes THIS box's temp/: each box's purge
deletes only that box's files, temp/ is pulled between boxes only on request
(owncloud-pull.sh --with-temp, .md/.json only), and a box without the agent's
runner claim may not write the agent's files to the store at all (the backend
raises NoClaimError). It is listed in owncloud_sync._EXCLUDE_NAMES and written
with plain local I/O under a local lock, never through the storage backend.
The durable cross-box record is the drain's journal entry.

IN FLIGHT: an item touched within DEFAULT_AGE_MIN minutes (for a folder, the
newest entry at any depth) is not reviewed, not counted as pressure and never
purged: the purge's age guard, applied identically in all three places.

A `discard` the purge could never execute (cited by a durable record, a
receipted folder, a folder holding unpushed or dirty git work) is refused at
`decide`, so no decision sits forever as "awaiting purge".

FINGERPRINTS
------------
files: sha256 of the content — what the reviewer actually looked at.
dirs:  sha256 over sorted (relative path, size, mtime_ns) of every file — a
       stat walk, so a large folder stays cheap; a touch only causes a
       re-review, which is the safe direction.

CLI (run through temp-decisions.sh so _paths.sh resolves python3)
---
  pending   [--json] [--summary] [--limit N] [--class C] [--include-fresh]
                                                            what still needs review
  bulk-junk [--dry-run] [--age-min N]                       principle 4
  decide                                                    JSON records on stdin
  deletable                                                 kind<TAB>name per line, for the purge
  log-deleted --lane L [--why W]                            kind<TAB>name on stdin, after deletion
  show      [--item N] [--deleted] [--since D] [--limit N] [--json]
  pressure                                                  cheap counts (precheck metric)
Global: --temp-dir PATH (default: the bound agent's temp/).
Exit: 0 ok; 1 refused (invalid decision, nothing written); 2 usage/environment;
      3 a lookup this command depends on failed (nothing printed/written).
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _paths import AGENT_DIR, AGENT_NAME, PROJECT_ROOT, WORLD_DIR  # noqa: E402

LEDGER_NAME = ".temp-decisions.jsonl"
# Dotfiles the framework writes on purpose. Twin of temp-drain-purge.sh
# _DOTFILE_ALLOWLIST_DEFAULT (Lane 0); edit both (guard-130).
MANAGED_DOTFILES = (".gitkeep", ".archive-marker", LEDGER_NAME, ".temp-decisions.lock")
DECISIONS = ("discard", "encode", "promote", "archive", "keep")
NEEDS_WHERE = ("encode", "promote", "archive")
KEEP_DAYS = 30
DEFAULT_AGE_MIN = 120
# Principle 4's "logs from finished runs". Deliberately short: a suffix missing
# here is merely reviewed one by one (the safe direction). .txt and .bak are NOT
# here — both are as often notes and safety copies as they are dumps.
BULK_JUNK_SUFFIXES = (".log", ".out", ".err", ".raw")
SCRIPT_SUFFIXES = (".py", ".sh", ".ps1", ".bat", ".cmd", ".js", ".mjs", ".ts",
                   ".lua", ".rb", ".pl", ".go", ".sql")
DOC_SUFFIXES = (".md", ".json")
# Same sentinel and stem rule as temp-drain-purge.sh _purge_find_predicate, so
# a citation pattern the purge refuses to honor is refused here too.
_OVERBROAD_SENTINEL = "zzz-overbroad-sentinel-9f3a2c"
_BAD_NAME = re.compile(r"[\x00-\x1f\x7f/\\]")
_RECEIPT = re.compile(r"(?i)^receipt(\..*)?$")
_TS_FMT = "%Y-%m-%dT%H:%M:%S"


# ── basics ───────────────────────────────────────────────────────────────────

def _now_ts():
    return datetime.now(timezone.utc).strftime(_TS_FMT)


def _parse_ts(value):
    try:
        return datetime.strptime(str(value)[:19], _TS_FMT)
    except (TypeError, ValueError):
        return None


def _host():
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def _native(path_str):
    """A /c/... path from a shell with path conversion off becomes C:/... ."""
    if os.name == "nt":
        m = re.match(r"^/([A-Za-z])/(.*)$", path_str)
        if m:
            return f"{m.group(1).upper()}:/{m.group(2)}"
    return path_str


def resolve_temp_dir(arg=None):
    """The temp dir to operate on, or None when it cannot be resolved safely."""
    if arg:
        p = Path(_native(arg))
    elif AGENT_DIR is not None:
        p = Path(AGENT_DIR) / "temp"
    else:
        return None
    if p.name != "temp" or not p.is_absolute():
        return None
    return p


def valid_item_name(name):
    """A top-level temp/ name this log can govern. Names that cannot round-trip
    through the purge's one-name-per-line protocol (control characters, path
    separators, undecodable bytes) are refused, so such an item is never
    deleted; it stays pending for a person to rename or decide by hand."""
    if not (isinstance(name, str) and name not in ("", ".", "..")
            and not name.startswith(".") and name != "drained"
            and not _BAD_NAME.search(name)):
        return False
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# ── the log ──────────────────────────────────────────────────────────────────

def ledger_path(temp_dir):
    return Path(temp_dir) / LEDGER_NAME


def read_ledger(temp_dir):
    """(rows, bad_line_count). A torn or hand-mangled line is skipped and
    counted, never fatal: one bad line must not freeze every decision."""
    p = ledger_path(temp_dir)
    rows, bad = [], 0
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
                else:
                    bad += 1
    except FileNotFoundError:
        pass
    return rows, bad


def append_rows(temp_dir, rows):
    """Append rows under a LOCAL lock (the log is machine-local; see header)."""
    if not rows:
        return
    from storage_backend import LocalBackend
    lb = LocalBackend()
    path = ledger_path(temp_dir)
    lock = path.with_suffix(".lock")
    lb.acquire_lock(lock, timeout=30, stale_seconds=60)
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        lb.release_lock(lock)


def current_decisions(rows):
    """name -> the decision row in force (latest decision after the last
    deletion of that name)."""
    cur = {}
    for r in rows:
        item = r.get("item")
        if not isinstance(item, str):
            continue
        if r.get("event") == "deleted":
            cur.pop(item, None)
        elif r.get("decision") in DECISIONS:
            cur[item] = r
    return cur


def keep_expired(row, now=None):
    ts = _parse_ts(row.get("ts"))
    if ts is None:
        return True
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return now - ts > timedelta(days=KEEP_DAYS)


# ── the items ────────────────────────────────────────────────────────────────

def scan(temp_dir):
    """Top-level entries the log governs: (name, kind, path). Dotfiles (incl.
    the log itself), drained/ and symlinks are excluded; symlinks are counted."""
    items, links = [], 0
    try:
        entries = sorted(os.scandir(temp_dir), key=lambda e: e.name)
    except FileNotFoundError:
        return items, links
    for e in entries:
        if e.name.startswith(".") or e.name == "drained":
            continue
        if e.is_symlink():
            links += 1
            continue
        if e.is_dir(follow_symlinks=False):
            items.append((e.name, "dir", Path(e.path)))
        elif e.is_file(follow_symlinks=False):
            items.append((e.name, "file", Path(e.path)))
    return items, links


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def newest_mtime(path, kind, cutoff=None, cap=20000):
    """The item's newest mtime: a file's own; for a folder, the newest of the
    folder and everything under it, files AND subfolders at any depth, which is
    the set `find <dir> -mmin -N` inspects in the purge's age guard. One
    freshness rule for the census, the pressure count and the purge (guard-5207:
    the metric and the remedy must see the same population). With `cutoff`, it
    returns as soon as anything newer is seen. A folder with more than `cap`
    entries returns the newest seen so far."""
    newest = os.lstat(path).st_mtime
    if kind != "dir":
        return newest
    seen = 0
    for root, dirs, names in os.walk(path, followlinks=False):
        for n in dirs + names:
            try:
                m = os.lstat(os.path.join(root, n)).st_mtime
            except OSError:
                continue
            seen += 1
            if m > newest:
                newest = m
                if cutoff is not None and newest > cutoff:
                    return newest
            if seen >= cap:
                return newest
    return newest


def is_fresh(path, kind, age_min, now=None):
    """Touched within the last `age_min` minutes (in flight: not reviewed yet,
    not counted as pressure, never purged)."""
    cutoff = (now or time.time()) - age_min * 60
    return newest_mtime(path, kind, cutoff=cutoff) > cutoff


def stats(path, kind):
    """(fingerprint, bytes, files)."""
    if kind == "file":
        st = os.lstat(path)
        return "sha256:" + file_sha256(path), st.st_size, 1
    h = hashlib.sha256()
    total = files = 0
    for root, dirs, names in os.walk(path, followlinks=False):
        dirs.sort()
        for n in sorted(names):
            fp = os.path.join(root, n)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            rel = Path(fp).relative_to(path).as_posix()
            h.update(f"{rel}\t{st.st_size}\t{st.st_mtime_ns}\n"
                     .encode("utf-8", "surrogateescape"))
            files += 1
            total += st.st_size
    return "tree:" + h.hexdigest(), total, files


def item_class(name, kind, size):
    if kind == "dir":
        return "dir"
    if size == 0:
        return "empty"
    suffix = Path(name).suffix.lower()
    if suffix in BULK_JUNK_SUFFIXES:
        return "run-output"
    if suffix in SCRIPT_SUFFIXES:
        return "script"
    if suffix in DOC_SUFFIXES:
        return "doc"
    return "other"


def is_receipted_dir(path):
    try:
        for child in os.scandir(path):
            if child.name == ".archive-marker":
                return True
            if child.is_file(follow_symlinks=False) and _RECEIPT.match(child.name):
                return True
    except OSError:
        pass
    return False


def tracked_names(temp_dir):
    """Depth-1 names git tracks in temp_dir; empty when not a work tree; None
    when git could not answer (callers decide what unknown means)."""
    try:
        r = subprocess.run(["git", "-C", str(temp_dir), "rev-parse",
                            "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or r.stdout.strip() != "true":
            return set()
        r = subprocess.run(["git", "-C", str(temp_dir), "ls-files", "-z"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return {p for p in r.stdout.split("\0") if p and "/" not in p}


def cited_index():
    """{pattern: [citing temp paths]} for every temp path a durable record
    cites, from the same source the purge uses (temp-citation-ratchet.py
    --cited-paths). None when the cited set is UNKNOWN.

    Each cited path contributes its basename (the key the purge's own guard
    matches on, across all agents, so the two agree) and its first segment
    after /temp/ (so a folder whose CONTENTS are cited counts as cited).
    Over-broad and stem-less patterns are dropped, exactly as the purge drops
    them. The source can return a PARTIAL set when some stores are unreadable
    (it fails whole only when nothing was readable), so this is a backstop
    behind the recorded decision, never the thing that authorizes a delete."""
    script = SCRIPT_DIR / "temp-citation-ratchet.py"
    if not script.is_file():
        return None
    try:
        r = subprocess.run([sys.executable, str(script), "--cited-paths"],
                           capture_output=True, text=True, timeout=180,
                           cwd=str(PROJECT_ROOT))
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    index = {}
    for line in r.stdout.splitlines():
        p = line.strip().rstrip("/")
        if not p:
            continue
        keys = {p.rsplit("/", 1)[-1]}
        if "/temp/" in p:
            keys.add(p.split("/temp/", 1)[1].split("/", 1)[0])
        for pat in keys:
            if not pat or fnmatch.fnmatchcase(_OVERBROAD_SENTINEL, pat):
                continue
            if re.search(r"[*?\[]", pat) and re.split(r"[*?\[]", pat, 1)[0] == "":
                continue
            index.setdefault(pat, set()).add(p)
    return {k: sorted(v) for k, v in index.items()}


def citing_paths(name, index):
    """The cited temp paths that protect `name` (empty when it is not cited)."""
    hits = set()
    for pat, paths in index.items():
        if fnmatch.fnmatchcase(name, pat):
            hits.update(paths)
    return sorted(hits)


def git_state(path):
    """For a folder holding a git repo: unpushed commits / dirty tracked files.

    --no-optional-locks keeps this a pure read. A plain `git status` rewrites
    .git/index when its cached stat info is stale, which would change the
    folder's fingerprint (so its decision stops applying) and make it look
    touched to the purge's in-flight guard.
    """
    if not (Path(path) / ".git").exists():
        return None
    try:
        u = subprocess.run(["git", "-C", str(path), "log", "--branches",
                            "--not", "--remotes", "-1", "--format=%H"],
                           capture_output=True, text=True, timeout=30)
        d = subprocess.run(["git", "-C", str(path), "--no-optional-locks",
                            "status", "--porcelain", "-uno"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {"unreadable": True}
    if u.returncode != 0 or d.returncode != 0:
        return {"unreadable": True}
    return {"unpushed": bool(u.stdout.strip()), "dirty": bool(d.stdout.strip())}


def head_text(path, kind):
    """A few lines a reviewer can decide from without opening the item."""
    if kind == "dir":
        try:
            names = sorted(os.listdir(path))
        except OSError:
            return ""
        shown = ", ".join(names[:10])
        return shown + (f" (+{len(names) - 10} more)" if len(names) > 10 else "")
    try:
        with open(path, "rb") as fh:
            raw = fh.read(4096)
    except OSError:
        return ""
    if b"\x00" in raw:
        return "(binary)"
    lines = [ln.strip() for ln in raw.decode("utf-8", "replace").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#!")]
    return " | ".join(ln[:160] for ln in lines[:3])


# ── pending (the census a review works from) ─────────────────────────────────

def build_pending(temp_dir, detail=True, include_fresh=False, age_min=DEFAULT_AGE_MIN):
    """(items, summary). An item is pending when it has no decision in force:
    undecided, changed since its decision, keep expired, or decided
    encode/promote/archive but still sitting here. Items touched within
    `age_min` minutes are in flight: counted as `fresh`, listed only with
    include_fresh."""
    entries, links = scan(temp_dir)
    rows, bad = read_ledger(temp_dir)
    cur = current_decisions(rows)
    tracked = tracked_names(temp_dir)
    cited = cited_index() if detail else None
    now = time.time()
    summary = {"pending": 0, "by_class": {}, "fresh": 0, "kept": 0, "awaiting_purge": 0,
               "receipted_dirs": 0, "tracked_skipped": 0, "bad_names": [],
               "symlinks_skipped": links, "ledger_rows": len(rows), "ledger_bad_lines": bad,
               "tracked_lookup": "ok" if tracked is not None else "failed",
               "cited_lookup": ("ok" if cited is not None else "failed") if detail else "skipped"}
    items, hashes = [], {}
    for name, kind, path in entries:
        if not valid_item_name(name):
            # Cannot be decided (see valid_item_name), so never deleted: a
            # person renames it. Listed by escaped name so it is not invisible.
            summary["bad_names"].append(ascii(name))
            continue
        if kind == "dir" and is_receipted_dir(path):
            summary["receipted_dirs"] += 1
            continue
        if kind == "file" and tracked and name in tracked:
            summary["tracked_skipped"] += 1
            continue
        row = cur.get(name)
        try:
            newest = newest_mtime(path, kind)
            if row is not None or detail:
                fp, size, files = stats(path, kind)
            else:
                # Summary of an undecided item: nothing to verify, so no
                # hashing — size only classifies empties.
                fp, size, files = None, (os.lstat(path).st_size if kind == "file" else 0), None
        except OSError:
            continue
        why_pending = "undecided"
        if row is not None:
            if row.get("fp") != fp:
                why_pending = "changed-since-decision"
            elif row.get("decision") == "keep" and keep_expired(row):
                why_pending = "keep-expired"
            elif row.get("decision") == "keep":
                summary["kept"] += 1
                continue
            elif row.get("decision") == "discard":
                # A discard the purge now refuses (cited after the decision, or
                # a folder that has since gained git work) would otherwise sit
                # as "awaiting purge" with nobody looking (guard-5488): it comes
                # back for review, with the reason, by the same test decide uses.
                blocked = (_discard_refusal(name, kind, path, cited)
                           if detail and cited is not None else None)
                if not blocked:
                    summary["awaiting_purge"] += 1
                    continue
                why_pending = "discard-blocked: " + blocked
            else:
                why_pending = "decided-not-executed"
        cls = item_class(name, kind, size)
        fresh = now - newest < age_min * 60
        if fresh:
            summary["fresh"] += 1
            if not include_fresh:
                continue
        else:
            summary["pending"] += 1
            summary["by_class"][cls] = summary["by_class"].get(cls, 0) + 1
        item = {"name": name, "kind": kind, "class": cls, "bytes": size,
                "age_h": round((now - newest) / 3600.0, 1),
                "why_pending": why_pending}
        if fresh:
            item["fresh"] = True
        if kind == "dir" and files is not None:
            item["files"] = files
        if row is not None:
            item["last_decision"] = {k: row.get(k) for k in
                                     ("ts", "decision", "why", "where") if row.get(k)}
        if detail:
            if cited is not None:
                by = citing_paths(name, cited)
                item["cited"] = bool(by)
                if by:
                    item["cited_by"] = by[:3]
            if kind == "dir":
                g = git_state(path)
                if g:
                    item["git"] = g
            item["head"] = head_text(path, kind)
            if kind == "file":
                hashes.setdefault(fp, []).append(item)
                item["_fp"] = fp
        items.append(item)
    if detail:
        _annotate_same_as(temp_dir, items, hashes)
    items.sort(key=lambda it: -it["age_h"])
    return items, summary


def _annotate_same_as(temp_dir, items, hashes):
    """Identical copies a reviewer should know about: other pending items, the
    drained/ twin (an already-drained copy re-materialized, rb-3498), and for
    scripts the durable home it may already have."""
    homes = [("core/scripts", Path(PROJECT_ROOT) / "core" / "scripts")]
    if WORLD_DIR is not None:
        homes.insert(0, ("world/scripts", Path(WORLD_DIR) / "scripts"))
    for it in items:
        fp = it.pop("_fp", None)
        if fp is None:
            continue
        same = [o["name"] for o in hashes.get(fp, []) if o is not it]
        twin = Path(temp_dir) / "drained" / it["name"]
        try:
            if twin.is_file() and "sha256:" + file_sha256(twin) == fp:
                same.append("drained/" + it["name"])
        except OSError:
            pass
        if it["class"] == "script":
            for label, base in homes:
                cand = base / it["name"]
                try:
                    if cand.is_file():
                        ident = "sha256:" + file_sha256(cand) == fp
                        same.append(f"{label}/{it['name']} ({'identical' if ident else 'differs'})")
                except OSError:
                    pass
        if same:
            it["same_as"] = same


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_pending(temp_dir, args):
    items, summary = build_pending(temp_dir, detail=not args.summary,
                                   include_fresh=args.include_fresh)
    if args.cls:
        items = [it for it in items if it["class"] == args.cls]
    shown = items[:args.limit] if args.limit else items
    if args.json or args.summary:
        out = {"summary": summary}
        if not args.summary:
            out["items"] = shown
            out["shown"] = len(shown)
        print(json.dumps(out, indent=1))
        return 0
    s = summary
    print(f"pending {s['pending']} {s['by_class']} | fresh (in flight) {s['fresh']} | "
          f"kept {s['kept']} | awaiting purge {s['awaiting_purge']} | receipted dirs "
          f"{s['receipted_dirs']} | cited lookup {s['cited_lookup']}")
    for it in shown:
        flags = [it["why_pending"]]
        if it.get("fresh"):
            flags.append("FRESH")
        if it.get("cited"):
            flags.append("CITED by " + ", ".join(it.get("cited_by", [])))
        if it.get("same_as"):
            flags.append("same as " + "; ".join(it["same_as"]))
        if it.get("git"):
            flags.append("git " + json.dumps(it["git"]))
        size = f"{it['bytes']}B" + (f"/{it['files']}f" if "files" in it else "")
        print(f"[{it['class']}] {it['name']}  {size}  {it['age_h']}h  "
              f"{', '.join(flags)}\n    {it.get('head', '')}")
    if len(shown) < len(items):
        print(f"... {len(items) - len(shown)} more (raise --limit)")
    return 0


def cmd_bulk_junk(temp_dir, args):
    """Principle 4: decide the obvious junk by name, one row per item."""
    entries, _ = scan(temp_dir)
    rows, _ = read_ledger(temp_dir)
    cur = current_decisions(rows)
    tracked = tracked_names(temp_dir)
    cited = cited_index()
    if tracked is None or cited is None:
        print(json.dumps({"decided": 0, "refused": "tracked or cited lookup failed; "
                          "bulk-junk decides nothing it cannot check"}))
        return 3
    now, ts, host = time.time(), _now_ts(), _host()
    out_rows, skipped = [], {"cited": 0, "tracked": 0, "too_fresh": 0, "decided": 0}
    for name, kind, path in entries:
        if kind != "file" or not valid_item_name(name):
            continue
        try:
            st = os.lstat(path)
        except OSError:
            continue
        suffix = Path(name).suffix.lower()
        if st.st_size == 0:
            why = "bulk: empty file"
        elif suffix in BULK_JUNK_SUFFIXES:
            why = f"bulk: run output ({suffix}), untouched {round((now - st.st_mtime) / 3600.0, 1)} h"
        else:
            continue
        if name in tracked:
            skipped["tracked"] += 1
            continue
        if citing_paths(name, cited):
            skipped["cited"] += 1
            continue
        if now - st.st_mtime < args.age_min * 60:
            skipped["too_fresh"] += 1
            continue
        fp = "sha256:" + file_sha256(path)
        row = cur.get(name)
        if row is not None and row.get("fp") == fp and not (
                row.get("decision") == "keep" and keep_expired(row)):
            skipped["decided"] += 1
            continue
        out_rows.append({"ts": ts, "item": name, "kind": "file", "bytes": st.st_size,
                         "fp": fp, "decision": "discard", "why": why,
                         "by": "bulk-junk", "host": host})
    if not args.dry_run:
        append_rows(temp_dir, out_rows)
    print(json.dumps({"decided": len(out_rows), "dry_run": args.dry_run,
                      "skipped": skipped,
                      "items": [{"item": r["item"], "why": r["why"], "bytes": r["bytes"]}
                                for r in out_rows]}, indent=1))
    return 0


def _read_records(text):
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except ValueError:
        return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def _discard_refusal(name, kind, path, cited):
    """Why a `discard` could never be executed by the purge (None when it can).
    Refusing it here keeps the log free of decisions that would sit forever as
    'awaiting purge' and drop out of the pressure count."""
    if kind == "dir" and is_receipted_dir(path):
        return ("a receipted archive (top-level RECEIPT or .archive-marker), which the "
                "purge never deletes; retire it through the archive-before-delete protocol")
    if kind == "dir":
        g = git_state(path)
        if g and (g.get("unreadable") or g.get("unpushed") or g.get("dirty")):
            return (f"holds a git repo with work the purge protects ({json.dumps(g)}); "
                    "push or archive that work first")
    if cited is None:
        return "the cited set is unknown, so a discard cannot be checked; retry"
    by = citing_paths(name, cited)
    if by:
        return (f"cited by a durable record ({', '.join(by[:3])}); the purge would keep it. "
                "Decide archive, encode or keep, or retire the citation first")
    return None


def cmd_decide(temp_dir, args):
    """Record reviewed decisions. All-or-nothing: one invalid record and nothing
    is written, so a caller fixes the batch and resends it whole."""
    try:
        records = _read_records(sys.stdin.buffer.read().decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as exc:
        print(json.dumps({"recorded": 0, "errors": [f"stdin is not UTF-8 JSON: {exc}"]}))
        return 1
    tracked = tracked_names(temp_dir)
    if tracked is None:
        print(json.dumps({"recorded": 0, "errors": [
            "git could not say which temp files are tracked; nothing recorded, retry"]}))
        return 3
    cited, cited_done = None, False
    errors, rows, seen = [], [], set()
    ts, host, by = _now_ts(), _host(), args.by or AGENT_NAME or "unknown"
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            errors.append(f"record {i}: not an object")
            continue
        name, decision = rec.get("item"), rec.get("decision")
        why, where = rec.get("why"), rec.get("where")
        tag = f"record {i} ({name!r})"
        if not valid_item_name(name):
            errors.append(f"{tag}: item must be a top-level name in temp/ (no '/', no dotfile, not drained)")
            continue
        if name in seen:
            errors.append(f"{tag}: listed twice")
            continue
        seen.add(name)
        if decision not in DECISIONS:
            errors.append(f"{tag}: decision must be one of {', '.join(DECISIONS)}")
            continue
        if not (isinstance(why, str) and why.strip()):
            errors.append(f"{tag}: why is required")
            continue
        if decision in NEEDS_WHERE and not (isinstance(where, str) and where.strip()):
            errors.append(f"{tag}: where is required for {decision} (the destination)")
            continue
        path = Path(temp_dir) / name
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            errors.append(f"{tag}: not found in {temp_dir}")
            continue
        if name in tracked:
            errors.append(f"{tag}: git-tracked, so not temp scratch; change it through git")
            continue
        kind = "dir" if path.is_dir() else "file"
        if decision == "discard":
            if not cited_done:
                cited, cited_done = cited_index(), True
            refusal = _discard_refusal(name, kind, path, cited)
            if refusal:
                errors.append(f"{tag}: {refusal}")
                continue
        try:
            fp, size, files = stats(path, kind)
        except OSError as exc:
            errors.append(f"{tag}: unreadable ({exc})")
            continue
        row = {"ts": ts, "item": name, "kind": kind, "bytes": size, "fp": fp,
               "decision": decision, "why": why.strip()}
        if kind == "dir":
            row["files"] = files
        if isinstance(where, str) and where.strip():
            row["where"] = where.strip()
        row["by"] = by
        row["host"] = host
        rows.append(row)
    if errors:
        print(json.dumps({"recorded": 0, "errors": errors}, indent=1))
        return 1
    append_rows(temp_dir, rows)
    print(json.dumps({"recorded": len(rows),
                      "items": [{"item": r["item"], "decision": r["decision"]} for r in rows]},
                     indent=1))
    return 0


def cmd_deletable(temp_dir, args):
    """What the purge may delete: a `discard` in force whose fingerprint still
    matches, that nothing durable cites and git does not track. Age, receipts
    and git repos are the purge's own guards, applied at the delete itself.
    Names go out as UTF-8 bytes, one `kind<TAB>name` per line, so a name the
    console encoding cannot represent is not rewritten on its way to `find`."""
    cited = cited_index()
    tracked = tracked_names(temp_dir)
    if cited is None or tracked is None:
        print("temp_decisions: cited set or git-tracked set UNKNOWN; listing nothing "
              "(fail-closed)", file=sys.stderr)
        return 3
    entries, _ = scan(temp_dir)
    rows, _ = read_ledger(temp_dir)
    cur = current_decisions(rows)
    out = sys.stdout.buffer
    for name, kind, path in entries:
        row = cur.get(name)
        if row is None or row.get("decision") != "discard" or row.get("kind") != kind:
            continue
        if not valid_item_name(name) or name in tracked:
            continue
        if citing_paths(name, cited):
            print(f"temp_decisions: '{name}' is decided discard but CITED by a durable "
                  "record; kept", file=sys.stderr)
            continue
        try:
            fp = stats(path, kind)[0]
        except OSError:
            continue
        if fp != row.get("fp"):
            continue
        out.write(f"{kind}\t{name}\n".encode("utf-8"))
    out.flush()
    return 0


def cmd_log_deleted(temp_dir, args):
    """Record deletions the purge actually performed (it calls this after the
    delete, with what it deleted). A path that still exists is not recorded."""
    rows_in, _ = read_ledger(temp_dir)
    cur = current_decisions(rows_in)
    ts, host = _now_ts(), _host()
    out, skipped = [], 0
    text = sys.stdin.buffer.read().decode("utf-8", "surrogateescape")
    for line in text.splitlines():
        if "\t" not in line:
            continue
        kind, name = line.split("\t", 1)
        name = name.strip().rstrip("/")
        if not name or name.startswith("/") or ".." in name.split("/"):
            continue
        if (Path(temp_dir) / name).exists():
            skipped += 1
            continue
        row = {"ts": ts, "item": name, "kind": kind.strip() or "file",
               "event": "deleted", "lane": args.lane}
        dec = cur.get(name) if args.lane == "decided" else None
        if dec is not None:
            row["why"] = dec.get("why")
            row["fp"] = dec.get("fp")
            row["bytes"] = dec.get("bytes")
        else:
            row["why"] = args.why or args.lane
        row["by"] = "temp-drain-purge"
        row["host"] = host
        out.append(row)
    append_rows(temp_dir, out)
    print(json.dumps({"logged": len(out), "still_present": skipped}))
    return 0


def _since(value):
    if not value:
        return None
    m = re.fullmatch(r"(\d+)([hd])", value)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(hours=n) if unit == "h" else timedelta(days=n)
        return datetime.now(timezone.utc).replace(tzinfo=None) - delta
    return _parse_ts(value)


def cmd_show(temp_dir, args):
    rows, bad = read_ledger(temp_dir)
    since = _since(args.since)
    sel = []
    for r in rows:
        if args.item and r.get("item") != args.item:
            continue
        if args.deleted and r.get("event") != "deleted":
            continue
        if since is not None:
            ts = _parse_ts(r.get("ts"))
            if ts is None or ts < since:
                continue
        sel.append(r)
    sel = sel[-args.limit:] if args.limit else sel
    if args.json:
        print(json.dumps({"rows": sel, "matched": len(sel), "bad_lines": bad}, indent=1))
        return 0
    for r in sel:
        what = "DELETED" if r.get("event") == "deleted" else r.get("decision", "?")
        where = f" -> {r['where']}" if r.get("where") else ""
        lane = f" [{r['lane']}]" if r.get("lane") else ""
        print(f"{r.get('ts')}  {what:<8} {r.get('item')}{lane}{where}  -- {r.get('why')}"
              f"  ({r.get('by')}@{r.get('host')})")
    if not sel:
        print("(no matching rows)")
    return 0


def pressure_counts(temp_dir, age_min=DEFAULT_AGE_MIN):
    """Cheap counts for the precheck metric: name-based, no hashing, no
    citation lookup, so it can run every iteration. The same population as the
    review's `pending` (same exclusions, same freshness rule), except that an
    item edited after its decision is under-counted here until the next
    review's exact `pending` finds it; the review is what the count schedules,
    so it still moves."""
    entries, links = scan(temp_dir)
    rows, bad = read_ledger(temp_dir)
    cur = current_decisions(rows)
    tracked = tracked_names(temp_dir) or set()
    now = time.time()
    out = {"pending": 0, "by_class": {}, "fresh": 0, "kept": 0, "awaiting_purge": 0,
           "receipted_dirs": 0, "tracked_skipped": 0, "bad_names": 0,
           "symlinks_skipped": links, "unmanaged_dotfiles": 0,
           "ledger_rows": len(rows), "ledger_bad_lines": bad}
    # Outside the review and never deleted (the purge's Lane 0 reports them),
    # so counted for visibility only.
    try:
        out["unmanaged_dotfiles"] = sum(
            1 for e in os.scandir(temp_dir)
            if e.name.startswith(".") and e.name not in MANAGED_DOTFILES
            and e.is_file(follow_symlinks=False))
    except OSError:
        pass
    for name, kind, path in entries:
        if not valid_item_name(name):
            out["bad_names"] += 1
            continue
        if kind == "dir" and is_receipted_dir(path):
            out["receipted_dirs"] += 1
            continue
        if kind == "file" and name in tracked:
            out["tracked_skipped"] += 1
            continue
        row = cur.get(name)
        if row is not None and row.get("decision") == "discard":
            out["awaiting_purge"] += 1
            continue
        if row is not None and row.get("decision") == "keep" and not keep_expired(row):
            out["kept"] += 1
            continue
        try:
            size = os.lstat(path).st_size if kind == "file" else 1
            fresh = is_fresh(path, kind, age_min, now)
        except OSError:
            continue
        if fresh:
            out["fresh"] += 1
            continue
        cls = item_class(name, kind, size)
        out["pending"] += 1
        out["by_class"][cls] = out["by_class"].get(cls, 0) + 1
    return out


def cmd_pressure(temp_dir, args):
    print(json.dumps(pressure_counts(temp_dir)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="The decision log that governs temp/.")
    ap.add_argument("--temp-dir", help="temp dir (default: the bound agent's)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pending")
    p.add_argument("--json", action="store_true")
    p.add_argument("--summary", action="store_true",
                   help="counts only (skips citations, first lines and duplicate checks)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--class", dest="cls",
                   choices=("doc", "script", "run-output", "empty", "other", "dir"))
    p.add_argument("--include-fresh", action="store_true",
                   help=f"also list items touched within {DEFAULT_AGE_MIN} min (in flight)")
    p = sub.add_parser("bulk-junk")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--age-min", type=int, default=DEFAULT_AGE_MIN)
    p = sub.add_parser("decide")
    p.add_argument("--by", help="who decided (default: the bound agent)")
    sub.add_parser("deletable")
    p = sub.add_parser("log-deleted")
    p.add_argument("--lane", required=True, choices=("decided", "drained-gc", "migration"))
    p.add_argument("--why")
    p = sub.add_parser("show")
    p.add_argument("--item")
    p.add_argument("--deleted", action="store_true")
    p.add_argument("--since", help="ISO time, or Nh / Nd")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    sub.add_parser("pressure")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # a cp1252 console must not crash a report
        except (AttributeError, ValueError):
            pass

    temp_dir = resolve_temp_dir(args.temp_dir)
    if temp_dir is None:
        print("temp_decisions: no safe temp dir (need an absolute path ending in "
              "/temp, or a bound agent)", file=sys.stderr)
        return 2
    if not temp_dir.is_dir():
        if args.cmd == "deletable":
            return 0                                   # nothing exists, nothing to list
        if args.cmd in ("pending", "pressure", "show"):
            print(json.dumps({"note": "temp dir does not exist", "temp_dir": str(temp_dir)}))
            return 0
        print(f"temp_decisions: {temp_dir} does not exist", file=sys.stderr)
        return 2
    handler = {"pending": cmd_pending, "bulk-junk": cmd_bulk_junk, "decide": cmd_decide,
               "deletable": cmd_deletable, "log-deleted": cmd_log_deleted,
               "show": cmd_show, "pressure": cmd_pressure}[args.cmd]
    return handler(temp_dir, args)


if __name__ == "__main__":
    sys.exit(main())
