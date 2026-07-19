"""Phase 3 — Historical audit backfill (Track E, §9.7) + §14.11 P3 extension.

Walks git log for all 4 tracked file_kinds and writes retroactive event-stream
entries to the appropriate JSONL stream. Idempotent two ways: skips commits
whose revision_id already exists in the target stream (sweep-vs-sweep), AND —
by default — commits whose (file_path, after_hash) already appears on ANY
existing entry (live-vs-sweep: a live-captured edit that was then committed
would otherwise re-enter as a sweep entry, since live revision_ids are random
and can never collide with the deterministic sweep ids — g-115-2566/g-115-2567).
With live-dedup on, the "N new entries" backfill count measures REAL capture
misses. `--no-live-dedup` restores the old behavior for forensic re-sweeps.

Tracked path patterns:
  - <agent>/self.md             (agent_self)        — git-tracked
  - .claude/skills/**/SKILL.md  (skill_edit)        — git-tracked
  - .claude/rules/*.md          (rule_edit)         — git-tracked
  - world/program.md            (program)           — NOT git-tracked; special-case

The 4 streams (under WORLD_DIR):
  - self-evolution.jsonl
  - program-evolution.jsonl
  - skill-evolution.jsonl
  - rule-evolution.jsonl

Retroactive entry shape per §5.2:
  status            = "final"
  signal_source     = "git-sweep"
  signal_evidence   = [{"type": "git_commit", "id": "<sha>", "subject": "<msg>"}]
  reasoning         = "[RETROACTIVE — backfilled by evolution-git-sweep.py from commit <sha8>]"
  ts                = git commit author date (NOT now)
  agent             = derived from commit author OR self.md path agent dir
  by_session_id     = null  (no live session existed)
  revision_id       = "<prefix>-<commit-ts>-<agent>-<sha[:4]>"  (deterministic for idempotency)

CLI:
  --since YYYY-MM-DD     — earliest commit date to scan
  --until YYYY-MM-DD     — latest commit date to scan
  --file-kind <kind>     — limit to single kind (default: all)
  --dry-run              — report what would be written; no JSONL writes
  --apply                — actually write entries
  --no-live-dedup        — disable the (file_path, after_hash) skip (forensic re-sweeps)
  --verbose              — per-commit progress

Per world/conventions/self-program-evolution.md
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import PROJECT_ROOT, WORLD_DIR, agents_root as _agents_root, agent_dir as _agent_dir


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def run_git(args, cwd=None):
    """Run a git command, return stdout text (utf-8). Empty string on error."""
    try:
        r = subprocess.run(["git"] + args, cwd=str(cwd or PROJECT_ROOT),
                           capture_output=True, text=True, encoding="utf-8")
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def list_commits(path_globs, since=None, until=None):
    """List commits that touch any path in path_globs.

    Returns: list of dicts {hash, ts, author_name, subject, files: [...]}
    """
    args = ["log", "--name-only", "--format=COMMIT|%H|%aI|%an|%s", "--no-renames"]
    if since:
        args += [f"--since={since}"]
    if until:
        args += [f"--until={until}"]
    args += ["--"] + path_globs
    raw = run_git(args)
    if not raw:
        return []

    commits = []
    current = None
    for line in raw.splitlines():
        if line.startswith("COMMIT|"):
            if current is not None:
                commits.append(current)
            parts = line.split("|", 4)
            if len(parts) < 5:
                current = None
                continue
            _, sha, ts, author, subject = parts
            current = {
                "hash": sha,
                "ts": ts,
                "author_name": author,
                "subject": subject,
                "files": [],
            }
        elif line.strip() and current is not None:
            current["files"].append(line.strip())
    if current is not None:
        commits.append(current)
    return commits


def get_blob(commit, path):
    """Get file blob at commit:path. None if file didn't exist."""
    r = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, encoding="utf-8",
    )
    return r.stdout if r.returncode == 0 else None


def get_diff_unified(commit, path):
    """Get unified diff for commit:path."""
    return run_git(["show", commit, "--", path])


def get_parent(commit):
    r = run_git(["rev-parse", f"{commit}^"]).strip()
    return r if r and len(r) >= 40 else None


# ---------------------------------------------------------------------------
# Classifier (from option-c-classify.py)
# ---------------------------------------------------------------------------

_STOPWORDS = {"and", "or", "of", "the", "a", "an", "for", "to", "in", "on"}


def light_stem(tok):
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def canonicalize_section(name):
    if not name:
        return ""
    s = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    s = re.sub(r"\s+", " ", s).strip()
    tokens = [light_stem(t) for t in s.split() if t not in _STOPWORDS]
    return " ".join(tokens[:3])


def body_hash(content):
    if not content:
        return None
    s = content
    if s.startswith("---"):
        m = re.match(r"^---\n.*?\n---\n", s, re.DOTALL)
        if m:
            s = s[m.end():]
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    s = s.strip() + "\n"
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def section_at_line(blob, line_num):
    if not blob:
        return "__no_blob__"
    lines = blob.split("\n")
    if line_num > len(lines):
        line_num = len(lines)
    for i in range(min(line_num, len(lines)) - 1, -1, -1):
        if i < len(lines) and lines[i].startswith("## ") and not lines[i].startswith("### "):
            return lines[i][3:].strip()
    return "__frontmatter_or_preamble__"


def parse_diff_sections(diff_text, path, before_blob=None, after_blob=None):
    lines = diff_text.split("\n")
    in_target = False
    is_creation = False
    section_changes = {}
    current_section = "__frontmatter_or_preamble__"
    current_before_line = 0
    current_after_line = 0
    first_change_line = None

    for line in lines:
        if line.startswith("diff --git "):
            in_target = path.replace("\\", "/") in line.replace("\\", "/")
            continue
        if not in_target:
            continue
        if line.startswith("new file mode "):
            is_creation = True
        if line.startswith("@@ "):
            m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@\s*(.*)$", line)
            if m:
                current_before_line = int(m.group(1))
                current_after_line = int(m.group(2))
                ctx = m.group(3).strip()
                if ctx.startswith("## "):
                    current_section = ctx[3:].strip()
                else:
                    section_from_blob = section_at_line(before_blob or after_blob, current_before_line or current_after_line)
                    if section_from_blob != "__no_blob__":
                        current_section = section_from_blob
                if first_change_line is None:
                    first_change_line = current_before_line
            continue
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            if content.startswith("## "):
                sec = content[3:].strip()
                section_changes.setdefault("+" + sec, {"added": 0, "removed": 0})["added"] += 1
                current_section = sec
            else:
                section_changes.setdefault(current_section, {"added": 0, "removed": 0})["added"] += 1
                current_after_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            content = line[1:]
            if content.startswith("## "):
                sec = content[3:].strip()
                section_changes.setdefault("-" + sec, {"added": 0, "removed": 0})["removed"] += 1
            else:
                section_changes.setdefault(current_section, {"added": 0, "removed": 0})["removed"] += 1
                current_before_line += 1
        else:
            current_before_line += 1
            current_after_line += 1

    return section_changes, is_creation, first_change_line


def detect_rename(section_changes):
    plus_secs = [k[1:] for k in section_changes if k.startswith("+")]
    minus_secs = [k[1:] for k in section_changes if k.startswith("-")]
    if len(plus_secs) == 1 and len(minus_secs) == 1:
        a = canonicalize_section(plus_secs[0]).split()
        b = canonicalize_section(minus_secs[0]).split()
        if a and b:
            j = len(set(a) & set(b)) / max(1, len(set(a) | set(b)))
            if j >= 0.5:
                return (minus_secs[0], plus_secs[0])
    return None


def classify(added, removed, section_changes, is_creation):
    if is_creation or (removed == 0 and added > 50):
        return "bootstrap"
    rename = detect_rename(section_changes)
    if rename:
        return "material-rename"
    n = added + removed
    structural_change = any(k.startswith("+") or k.startswith("-") for k in section_changes)
    if n <= 5 and not structural_change:
        return "cosmetic"
    return "material"


# ---------------------------------------------------------------------------
# Path classification
# ---------------------------------------------------------------------------

def classify_path(rel_path):
    """Return (file_kind, key) for a tracked path, or (None, None).

    Operates on repo-relative paths from git log output.
    """
    p = rel_path.replace("\\", "/")
    parts = p.split("/")

    # SKILL.md: .claude/skills/<name>/SKILL.md
    if len(parts) >= 4 and parts[0] == ".claude" and parts[1] == "skills" and parts[-1] == "SKILL.md":
        return ("skill_edit", parts[2])
    # Rule: .claude/rules/<name>.md  (exact depth 3)
    if len(parts) == 3 and parts[0] == ".claude" and parts[1] == "rules" and parts[2].endswith(".md"):
        return ("rule_edit", parts[2][:-3])
    # Reject other .claude/ paths
    if len(parts) >= 2 and parts[0] == ".claude":
        return (None, None)
    # Agent self.md — BOTH layout eras appear in git history: legacy
    # <agent>/self.md (pre-2.5.D, 2 parts) AND relocated
    # <AGENTS_PARENT_DIR>/<agent>/self.md (depth follows _agents_root()).
    # The 2-part-only form silently zeroed agent_self backfill after the
    # relocation (fresh-eyes 2026-07-11: stream newest 2026-05-29 while
    # self.md carried 3 newer revisions).
    if len(parts) == 2 and parts[1] == "self.md":
        if (_agent_dir(parts[0]) / "local-paths.conf").exists():
            return ("agent_self", parts[0])
    _aprel = _agents_root().relative_to(PROJECT_ROOT).parts
    if (
        _aprel
        and parts[-1] == "self.md"
        and len(parts) == len(_aprel) + 2
        and tuple(parts[: len(_aprel)]) == _aprel
    ):
        if (_agent_dir(parts[-2]) / "local-paths.conf").exists():
            return ("agent_self", parts[-2])
    return (None, None)


# ---------------------------------------------------------------------------
# Revision-id derivation (deterministic for idempotency)
# ---------------------------------------------------------------------------

_PREFIX = {
    "agent_self": "self",
    "program": "program",
    "skill_edit": "skill",
    "rule_edit": "rule",
}


def make_revision_id(file_kind, commit_ts_iso, agent, sha):
    """Build a deterministic revision_id for a git-sweep entry.

    Format: <prefix>-<YYYYMMDDTHHMMSS>-<agent>-<sha[:4]>
    Same shape as runtime IDs from evolution-record.py — the chain reads uniformly.
    """
    # Parse 2026-04-22T13:30:22+00:00 → 20260422T133022
    ts_compact = re.sub(r"[-:]", "", commit_ts_iso.split("+")[0].replace("Z", ""))[:15]
    prefix = _PREFIX[file_kind]
    return f"{prefix}-{ts_compact}-{agent}-{sha[:4]}"


def commit_ts_to_entry_ts(commit_ts_iso):
    """Convert git author-date ISO (with timezone) to our entry timestamp format."""
    # 2026-04-22T13:30:22+00:00 → 2026-04-22T13:30:22
    return commit_ts_iso.split("+")[0].replace("Z", "")


# ---------------------------------------------------------------------------
# Idempotency: load existing revision_ids per stream
# ---------------------------------------------------------------------------

_STREAM_FILENAME = {
    "agent_self": "self-evolution.jsonl",
    "program": "program-evolution.jsonl",
    "skill_edit": "skill-evolution.jsonl",
    "rule_edit": "rule-evolution.jsonl",
}


def load_existing_revision_ids(world_dir):
    """Return dict[file_kind] -> set of revision_ids already in that stream."""
    out = {kind: set() for kind in _STREAM_FILENAME}
    for kind, fname in _STREAM_FILENAME.items():
        path = Path(world_dir) / fname
        if not path.exists():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        rid = e.get("revision_id")
                        if rid:
                            out[kind].add(rid)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            continue
    return out


def _norm_path(p):
    """Normalize a file_path for dedup joins: posix separators only.

    Both live (evolution-record) and sweep entries currently write
    repo-relative posix paths (probed 2439/2439 on 2026-07-18), but a future
    Windows writer drifting to backslashes must not silently break the join.
    """
    return (p or "").replace("\\", "/")


def load_existing_after_hashes(world_dir):
    """Return dict[file_kind] -> dict[(file_path, after_hash) -> revision_id].

    Covers entries of ANY signal_source (live D1, git-sweep, guard-380, ...):
    the live-dedup question is "is this content-state for this file already
    recorded in the stream?", not "who recorded it?". Last-seen entry wins so
    a skip chains previous_revision_id to the most recent matching record.
    Entries with a null after_hash are excluded (never matchable).
    """
    out = {kind: {} for kind in _STREAM_FILENAME}
    for kind, fname in _STREAM_FILENAME.items():
        path = Path(world_dir) / fname
        if not path.exists():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ah = e.get("after_hash")
                    rid = e.get("revision_id")
                    if ah and rid:
                        out[kind][(_norm_path(e.get("file_path")), ah)] = rid
        except Exception:
            continue
    return out


def live_dedup_match(rel_path, after_blob, pair_to_rid):
    """Return the existing revision_id whose (file_path, after_hash) matches
    this commit's post-state, or None.

    body_hash normalizes front matter, CRLF, and trailing whitespace, so a
    live capture and the committed blob of the same edit hash identically.
    Deliberate scope choices:
    - None-hash blobs (file deleted at the commit) never match.
    - A revert commit restoring an OLD content-state matches the old entry
      and is skipped — the content-state is already recorded, so it is not a
      capture MISS; the chain points at the matched entry.
    - Only PRE-EXISTING stream entries participate: entries built earlier in
      the same sweep run are not added to the map, so a fresh-world
      full-history sweep still reconstructs complete edit chains.
    """
    ah = body_hash(after_blob)
    if ah is None:
        return None
    return pair_to_rid.get((_norm_path(rel_path), ah))


# ---------------------------------------------------------------------------
# Build retroactive entry
# ---------------------------------------------------------------------------

def build_entry(commit, rel_path, file_kind, key, parent_sha, before_blob, after_blob,
                diff_text, agent):
    """Build a single retroactive event-stream entry per §5.2 schema."""
    section_changes, is_creation, first_change_line = parse_diff_sections(
        diff_text, rel_path, before_blob, after_blob
    )
    added = sum(v["added"] for v in section_changes.values())
    removed = sum(v["removed"] for v in section_changes.values())
    change_class = classify(added, removed, section_changes, is_creation)
    rename = detect_rename(section_changes)

    # Section attribution: use the section the first change landed in
    section = "__frontmatter_or_preamble__"
    if first_change_line is not None:
        section = section_at_line(before_blob, first_change_line)
        if section == "__no_blob__":
            section = section_at_line(after_blob, first_change_line)

    # Diff excerpt: first 500 chars of the diff body
    diff_excerpt = ""
    if diff_text:
        # Skip diff header (everything up to the first @@)
        m = re.search(r"^@@.+$", diff_text, re.MULTILINE)
        if m:
            diff_excerpt = diff_text[m.start():m.start() + 500]

    revision_id = make_revision_id(file_kind, commit["ts"], agent, commit["hash"])
    entry_ts = commit_ts_to_entry_ts(commit["ts"])
    sha8 = commit["hash"][:8]

    entry = {
        "revision_id": revision_id,
        "previous_revision_id": None,  # caller fills in from chain
        "ts": entry_ts,
        "file_kind": file_kind,
        "file_path": rel_path,
        "agent": agent,
        "by_session_id": None,
        "by_runner_token": None,
        "change_class": change_class if not is_creation else "bootstrap",
        "section_changed": section,
        "diff_excerpt": diff_excerpt,
        "diff_lines": {"added": added, "removed": removed},
        "before_hash": body_hash(before_blob),
        "after_hash": body_hash(after_blob),
        "history_snapshot": None,  # No live .history/ for retroactive entries
        "signal_source": "git-sweep",
        "signal_evidence": [
            {"type": "git_commit", "id": commit["hash"], "subject": commit["subject"][:120]}
        ],
        "reasoning": f"[RETROACTIVE — backfilled by evolution-git-sweep.py from commit {sha8}]",
        "user_notified": False,
        "user_notify_ref": None,
        "board_post_id": None,
        "verification_monitor_id": None,
        "status": "final",
    }

    if rename:
        entry["section_renamed_from"] = rename[0]
        entry["section_renamed_to"] = rename[1]
    if file_kind == "skill_edit":
        entry["skill_name"] = key
    elif file_kind == "rule_edit":
        entry["rule_name"] = key

    return entry


# ---------------------------------------------------------------------------
# Agent extraction
# ---------------------------------------------------------------------------

def derive_agent(commit, file_kind, key):
    """For agent_self, agent = the dir name (key). For others, derive from commit author."""
    if file_kind == "agent_self":
        return key
    # For skill/rule/program: use author name lowercased if it looks like an agent name,
    # else "framework" (means committed by a non-agent context, e.g. user manually).
    author = commit.get("author_name", "").strip().lower()
    # Known agents from project structure
    known_agents = set()
    for child in _agents_root().iterdir():
        if child.is_dir() and (child / "local-paths.conf").exists():
            known_agents.add(child.name)
    if author in known_agents:
        return author
    return "framework"


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def sweep_file_kind(file_kind, path_glob, since, until, world_dir, dry_run, verbose,
                    live_dedup=True):
    """Sweep one file kind. Returns (new_entries, skipped, skipped_live)."""
    globs = path_glob if isinstance(path_glob, list) else [path_glob]
    commits = list_commits(globs, since=since, until=until)
    if verbose:
        print(f"  [{file_kind}] found {len(commits)} commits matching {path_glob}")

    existing_ids = load_existing_revision_ids(world_dir).get(file_kind, set())
    pair_to_rid = load_existing_after_hashes(world_dir).get(file_kind, {}) if live_dedup else {}

    # Group commits by file → chain previous_revision_id correctly per file
    by_file = {}
    for c in commits:
        for f in c["files"]:
            kind, key = classify_path(f)
            if kind != file_kind:
                continue
            by_file.setdefault(f, []).append((c, key))

    new_entries = []
    skipped = 0
    skipped_live = 0
    for rel_path, commit_list in by_file.items():
        # Sort by commit timestamp ascending (oldest first → chain naturally)
        commit_list.sort(key=lambda ck: ck[0]["ts"])

        prev_rid_for_this_file = None
        for commit, key in commit_list:
            agent = derive_agent(commit, file_kind, key)
            revision_id = make_revision_id(file_kind, commit["ts"], agent, commit["hash"])

            if revision_id in existing_ids:
                if verbose:
                    print(f"    SKIP existing: {revision_id}")
                # Still need to track this as the predecessor for next entry
                prev_rid_for_this_file = revision_id
                skipped += 1
                continue

            parent = get_parent(commit["hash"])
            before_blob = get_blob(parent, rel_path) if parent else None
            after_blob = get_blob(commit["hash"], rel_path)

            live_rid = live_dedup_match(rel_path, after_blob, pair_to_rid)
            if live_rid is not None:
                if verbose:
                    print(f"    SKIP live-captured: {revision_id} (after_hash matches {live_rid})")
                # Chain the next entry to the matched existing record so
                # previous_revision_id stays meaningful across the skip.
                prev_rid_for_this_file = live_rid
                skipped_live += 1
                continue

            diff_text = get_diff_unified(commit["hash"], rel_path)

            entry = build_entry(commit, rel_path, file_kind, key, parent,
                                before_blob, after_blob, diff_text, agent)
            entry["previous_revision_id"] = prev_rid_for_this_file
            new_entries.append(entry)
            prev_rid_for_this_file = revision_id

    if verbose:
        print(f"  [{file_kind}] would write {len(new_entries)} new entries, "
              f"skipping {skipped} (existing) + {skipped_live} (live-captured)")

    if new_entries and not dry_run:
        # Append all entries in one atomic batch via _fileops.locked_append_jsonl
        from _fileops import locked_append_jsonl
        stream_path = Path(world_dir) / _STREAM_FILENAME[file_kind]
        for e in new_entries:
            locked_append_jsonl(stream_path, e)

    return new_entries, skipped, skipped_live


def sweep_program(world_dir, dry_run, verbose):
    """Special case: program.md is NOT git-tracked. Emit ONE bootstrap-retroactive
    entry from current file state if no entry yet exists in program-evolution.jsonl.
    """
    program_path = Path(world_dir) / "program.md"
    if not program_path.exists():
        if verbose:
            print("  [program] program.md does not exist; skipping")
        return [], 0

    existing_ids = load_existing_revision_ids(world_dir).get("program", set())
    if existing_ids:
        if verbose:
            print(f"  [program] {len(existing_ids)} existing entries; skipping bootstrap")
        return [], len(existing_ids)

    raw = program_path.read_text(encoding="utf-8")
    bh = body_hash(raw)
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    ts_compact = ts.replace("-", "").replace(":", "").replace(" ", "")[:15]

    entry = {
        "revision_id": f"program-{ts_compact}-framework-boot",
        "previous_revision_id": None,
        "ts": ts,
        "file_kind": "program",
        "file_path": "world/program.md",
        "agent": "framework",
        "by_session_id": None,
        "change_class": "bootstrap",
        "section_changed": "__bootstrap__",
        "diff_excerpt": "",
        "diff_lines": {"added": 0, "removed": 0},
        "before_hash": None,
        "after_hash": bh,
        "history_snapshot": None,
        "signal_source": "git-sweep",
        "signal_evidence": [{"type": "bootstrap-retroactive", "id": "phase-3-special-case",
                              "note": "world/program.md is not git-tracked; bootstrap recorded from current file state"}],
        "reasoning": "[RETROACTIVE — Phase 3 special-case bootstrap. WORLD is not git-tracked; this entry anchors the program-evolution chain from current state.]",
        "user_notified": False,
        "user_notify_ref": None,
        "board_post_id": None,
        "verification_monitor_id": None,
        "status": "final",
    }

    if not dry_run:
        from _fileops import locked_append_jsonl
        locked_append_jsonl(Path(world_dir) / _STREAM_FILENAME["program"], entry)

    return [entry], 0


def main():
    parser = argparse.ArgumentParser(description="Phase 3 historical audit backfill (§9.7, §14.11 P3)")
    parser.add_argument("--since", help="Earliest commit date (e.g., 2026-03-01)")
    parser.add_argument("--until", help="Latest commit date")
    parser.add_argument("--file-kind", default="all",
                        choices=["all", "agent_self", "program", "skill_edit", "rule_edit"],
                        help="Limit to one file kind (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    parser.add_argument("--apply", action="store_true", help="Write entries")
    parser.add_argument("--no-live-dedup", action="store_true",
                        help="Disable the (file_path, after_hash) live-entry skip "
                             "(forensic re-sweeps; restores pre-g-115-2567 behavior)")
    parser.add_argument("--verbose", action="store_true", help="Per-commit progress")
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        args.dry_run = True
        print("[default: dry-run]  Use --apply to write entries.")

    if WORLD_DIR is None:
        print("ERROR: WORLD_DIR not resolvable. Run with MIND_AGENT set + valid local-paths.conf.", file=sys.stderr)
        return 2

    world_dir = WORLD_DIR

    if args.file_kind == "all":
        kinds_to_run = ["agent_self", "skill_edit", "rule_edit"]
    elif args.file_kind == "program":
        kinds_to_run = []  # program is special-cased; not in path_globs
    else:
        kinds_to_run = [args.file_kind]
    do_program = args.file_kind in ("all", "program")

    # agent_self carries BOTH pathspecs so git log surfaces pre-relocation
    # (<agent>/self.md) AND post-relocation (agents/<agent>/self.md) commits;
    # the second is derived from _agents_root() so a future rename tracks.
    _aprel = _agents_root().relative_to(PROJECT_ROOT).as_posix()
    path_globs = {
        "agent_self": ["*/self.md"]
        + ([f"{_aprel}/*/self.md"] if _aprel != "." else []),
        "skill_edit": [".claude/skills/**/SKILL.md"],
        "rule_edit": [".claude/rules/*.md"],
    }

    totals = {"written": 0, "skipped": 0, "skipped_live": 0}
    for kind in kinds_to_run:
        glob = path_globs[kind]
        print(f"\n=== {kind} ({glob}) ===")
        new, skipped, skipped_live = sweep_file_kind(
            kind, glob, args.since, args.until, world_dir,
            args.dry_run, args.verbose, live_dedup=not args.no_live_dedup)
        totals["written"] += len(new)
        totals["skipped"] += skipped
        totals["skipped_live"] += skipped_live
        if not args.verbose:
            print(f"  {len(new)} new entries, {skipped} skipped (existing), "
                  f"{skipped_live} skipped (live-captured)")

    if do_program:
        print(f"\n=== program (special-case bootstrap) ===")
        new, skipped = sweep_program(world_dir, args.dry_run, args.verbose)
        totals["written"] += len(new)
        totals["skipped"] += skipped
        print(f"  {len(new)} new entries, {skipped} skipped (existing)")

    print()
    print(f"Total: {totals['written']} new entries {'(dry-run, not written)' if args.dry_run else 'written'}, "
          f"{totals['skipped']} skipped (existing), {totals['skipped_live']} skipped (live-captured)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
