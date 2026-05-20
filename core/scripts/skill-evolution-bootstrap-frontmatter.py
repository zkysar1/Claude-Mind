"""Phase 2 bootstrap migration — add revision_id + previous_revision_id front-matter
fields to every .claude/skills/**/SKILL.md.

Sister script of self-evolution-bootstrap-frontmatter.py — same algorithm,
different file set. Rule files (.claude/rules/*.md) are SKIPPED per
world/conventions/self-program-evolution.md U-9 (rule files use external chain only;
no in-file revision_id).

Idempotent: re-runs are no-ops if the fields are already present.
Dry-run flag: prints proposed diffs without writing.

Per world/conventions/self-program-evolution.md:
  - For each .claude/skills/<name>/SKILL.md:
      - Parse YAML front matter (all SKILL.md files have it — verified 2026-05-13)
      - Add `revision_id: skill-bootstrap-<name>-<6hex>` if not present
      - Add `previous_revision_id: null` if not present
      - Preserve all existing fields (description, conventions, companion_scripts, etc.)

Schema source: world/conventions/self-program-evolution.md.

NOTE: This migration does NOT use the locked_write_* helpers — same reasoning
as self-evolution-bootstrap-frontmatter.py (one-time bootstrap, not a protocol
write; avoids changelog noise before the protocol fires its first real event).

Snapshot location: .claude/.history/skills/<name>/SKILL.md/<ts>_bootstrap-frontmatter-migration_<author>.md
(uses the G1b PROJECT_ROOT/.claude/ base dir introduced in Phase 2.1).

Auth model: not gated by MIND_AGENT (applies to shared .claude/ tree).
"""
import argparse
import hashlib
import os
import shutil
import sys
import re
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _paths import PROJECT_ROOT


SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"
HISTORY_BASE = PROJECT_ROOT / ".claude" / ".history" / "skills"


def find_skill_md_files():
    """Find every .claude/skills/<name>/SKILL.md."""
    if not SKILLS_DIR.exists():
        return []
    out = []
    for child in sorted(SKILLS_DIR.iterdir()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if skill_md.exists():
            out.append((child.name, skill_md))
    return out


def parse_front_matter(text):
    """Return (front_matter_block_str_or_None, body, has_front_matter)."""
    if not text.startswith("---\n"):
        return None, text, False
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text, False
    fm_block = text[4:end]
    body = text[end + 5:]
    return fm_block, body, True


def fm_to_lines(fm_block):
    return fm_block.split("\n") if fm_block is not None else []


def get_field(fm_lines, key):
    """Return value for a top-level key in front-matter lines, or None.

    Handles nested YAML by only matching top-level (no leading whitespace).
    """
    pat = re.compile(rf"^{re.escape(key)}\s*:\s*(.+?)\s*$")
    for line in fm_lines:
        m = pat.match(line)
        if m:
            return m.group(1)
    return None


def body_hash(body):
    """Body-only sha256 per §5.4a — strip trailing whitespace per line, single trailing newline."""
    s = body.replace("\r\n", "\n").replace("\r", "\n")
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    s = s.strip() + "\n"
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def make_revision_id(skill_name, content_hash):
    """skill-bootstrap-<skill-name>-<6hex>."""
    short = content_hash.split(":")[1][:6]
    return f"skill-bootstrap-{skill_name}-{short}"


def migrate_skill_md(skill_md_path, skill_name, *, dry_run=False, author=""):
    """Add revision_id + previous_revision_id to SKILL.md front matter if missing."""
    text = skill_md_path.read_text(encoding="utf-8")
    fm_block, body, has_fm = parse_front_matter(text)
    if not has_fm:
        return {
            "skill": skill_name,
            "path": str(skill_md_path),
            "status": "ERROR",
            "msg": "no front matter detected — manual review required",
        }

    fm_lines = fm_to_lines(fm_block)
    existing_rev = get_field(fm_lines, "revision_id")
    existing_prev = get_field(fm_lines, "previous_revision_id")

    needs_rev = existing_rev is None
    needs_prev = existing_prev is None

    if not needs_rev and not needs_prev:
        return {
            "skill": skill_name,
            "path": str(skill_md_path),
            "status": "noop",
            "msg": "fields already present",
        }

    bh = body_hash(body)
    new_rev_id = make_revision_id(skill_name, bh)

    new_fm_lines = list(fm_lines)
    if needs_rev:
        new_fm_lines.append(f'revision_id: "{new_rev_id}"')
    if needs_prev:
        new_fm_lines.append("previous_revision_id: null")

    new_fm_block = "\n".join(new_fm_lines)
    new_text = f"---\n{new_fm_block}\n---\n{body}"

    if dry_run:
        return {
            "skill": skill_name,
            "path": str(skill_md_path),
            "status": "DRY_RUN_WOULD_ADD",
            "added": {
                "revision_id": new_rev_id if needs_rev else "(present)",
                "previous_revision_id": "null" if needs_prev else "(present)",
            },
            "body_hash": bh,
        }

    # Snapshot to .claude/.history/skills/<name>/SKILL.md/<ts>_bootstrap_<author>.md
    snap_dir = HISTORY_BASE / skill_name / "SKILL.md"
    snap_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    snap = snap_dir / f"{ts}_bootstrap-frontmatter-migration_{author}.md"
    shutil.copy2(str(skill_md_path), str(snap))

    tmp = skill_md_path.with_suffix(skill_md_path.suffix + ".tmp-bootstrap")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(str(tmp), str(skill_md_path))

    return {
        "skill": skill_name,
        "path": str(skill_md_path),
        "status": "APPLIED",
        "added": {
            "revision_id": new_rev_id if needs_rev else "(present)",
            "previous_revision_id": "null" if needs_prev else "(present)",
        },
        "body_hash": bh,
        "snapshot": str(snap),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Phase 2 bootstrap migration for SKILL.md front matter (§14 carryover)"
    )
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run).")
    parser.add_argument("--only-skill", help="Migrate only this skill name.")
    parser.add_argument("--author", default="", help="Author name for snapshot filenames (default: empty).")
    args = parser.parse_args()

    dry_run = not args.apply

    if dry_run:
        print("[DRY RUN] No files will be modified. Use --apply to commit.")
    else:
        print("[APPLY] Writing changes.")
    print()

    skills = find_skill_md_files()
    if args.only_skill:
        skills = [(n, p) for n, p in skills if n == args.only_skill]
        if not skills:
            print(f"ERROR: skill '{args.only_skill}' not found under .claude/skills/")
            return 1

    print(f"Found {len(skills)} SKILL.md files:")
    print()

    counts = {"APPLIED": 0, "noop": 0, "DRY_RUN_WOULD_ADD": 0, "ERROR": 0}
    for name, path in skills:
        r = migrate_skill_md(path, name, dry_run=dry_run, author=args.author)
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        # Compact line output
        if r["status"] in ("DRY_RUN_WOULD_ADD", "APPLIED"):
            rid = r["added"]["revision_id"]
            print(f"  [{r['status']}] {name:40s}  {rid}")
        elif r["status"] == "noop":
            print(f"  [noop ] {name:40s}  (already migrated)")
        else:
            print(f"  [ERR  ] {name:40s}  {r.get('msg', '?')}")

    print()
    print("Summary:")
    for k, v in counts.items():
        if v > 0:
            print(f"  {k}: {v}")

    if dry_run and counts.get("DRY_RUN_WOULD_ADD", 0) > 0:
        print()
        print("Re-run with --apply to commit.")

    return 0 if counts.get("ERROR", 0) == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
