#!/usr/bin/env python3
"""Line-class diff for bulk markdown prose edits ().

WHY THIS EXISTS. A bulk prose-extraction pass over .claude/skills/start/SKILL.md
relocated content in three ways and EVERY conventional check was green
(measured 2026-08-25, g-115-7706): 94 targeted tests passed, domain-leak-check
was clean, and the pre-completion re-read saw nothing because the damage sat at
LINE 1, outside every region the editor had touched.

Tests structurally cannot cover this -- they pin a handful of literals, while
the thing destroyed is a CLASS of line. So this does not test content. It
counts lines by class in HEAD and in the worktree and reports DROPS.

The three measured classes, plus headings:
  front_matter   -- a `---` fence on line 1 (identity: name/description/triggers)
  blockquote     -- `>` lines: text a skill DISPLAYS to the user
  bold_directive -- `**`-led lines: step headings and MUST-directives
  heading        -- `#`-led lines

Report-only by contract: there is no --apply path and no write of any kind.
"""
import argparse
import json
import subprocess
import sys

FENCE = "---"


def classify(lines):
    """Bucket lines into SETS by class. Sets, not counts, on purpose.

    Sets beat counts because they name WHICH lines left, which is what a reader
    needs to judge an edit -- and because an equal-sized swap (N lines out, N
    different lines in) is invisible to a count and loud in a set.

    LIMIT, stated so nobody over-trusts this: set difference is order-INSENSITIVE,
    so a pure REORDER within one file is NOT detected by these three classes. The
    measured g-115-7706 defect was lines moved OUT to another file (a removal,
    caught) and front matter relocated off line 1 (caught POSITIONALLY by fm
    below, not by any set). Reordering-in-place is out of scope, deliberately.
    """
    c = {"blockquote": set(), "bold_directive": set(), "heading": set()}
    fm = 0
    if lines and lines[0].strip() == FENCE:
        for i, ln in enumerate(lines[1:], start=1):
            if ln.strip() == FENCE:
                fm = i + 1
                break
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith(">"):            # lstrip'd: indented blockquotes count
            c["blockquote"].add(s)
        elif s.startswith("**"):
            c["bold_directive"].add(s)
        elif s.startswith("#"):
            c["heading"].add(s)
    return fm, c


class GitUnusable(Exception):
    """git itself failed -- NOT the same as 'this path is absent from the ref'.

    Collapsing the two is how a checker reports CLEAN when it could not look
    (fresh-eyes F-1: --ref with a typo printed CLEAN and exited 0). A checker
    that cannot read its baseline must fail loudly, never quietly pass.
    """


def assert_ref_usable(ref):
    """Resolve the ref ONCE, up front, so a bad ref cannot masquerade as
    'every file is new'."""
    r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise GitUnusable(
            "cannot resolve ref %r (not a git repo, or the ref does not exist). "
            "Refusing to report CLEAN against a baseline that cannot be read." % ref)


def head_text(path, ref):
    """Return the file's content at ref, or None IFF the path is absent there.

    The ref is already known good (assert_ref_usable), so a non-zero exit here
    means path-not-in-ref, which is a legitimate skip for a newly added file.
    """
    r = subprocess.run(["git", "show", f"{ref}:{path}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def check(path, ref):
    before = head_text(path, ref)
    if before is None:
        return {"path": path, "status": "skipped", "reason": "not in " + ref}
    try:
        after = open(path, encoding="utf-8").read()
    except OSError as e:
        return {"path": path, "status": "skipped", "reason": str(e)}

    fm_b, b = classify(before.splitlines())
    fm_a, a = classify(after.splitlines())

    # Set-difference BOTH WAYS per class (outcome 1).
    delta = {}
    for k in b:
        removed = sorted(b[k] - a[k])
        added = sorted(a[k] - b[k])
        if removed or added:
            delta[k] = {"removed": removed, "added": added,
                        "removed_count": len(removed), "added_count": len(added)}

    lost_fm = fm_b > 0 and fm_a == 0
    # REMOVED lines are the destructive direction and set the verdict; additions
    # are reported but never fail the check -- adding a heading is ordinary work.
    any_removed = any(v["removed"] for v in delta.values())
    return {
        "path": path, "front_matter_lines": [fm_b, fm_a],
        "front_matter_lost": lost_fm, "delta": delta,
        "status": "DEFECT" if (any_removed or lost_fm) else "ok",
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="markdown paths (default: staged+unstaged .md)")
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        assert_ref_usable(args.ref)
    except GitUnusable as e:
        print("line-class-diff: ERROR -- %s" % e, file=sys.stderr)
        return 2

    files = args.files
    if not files:
        r = subprocess.run(["git", "diff", "--name-only", args.ref],
                           capture_output=True, text=True)
        if r.returncode != 0:
            # F-2: an unchecked failure here yields empty stdout, which reads
            # as "nothing changed" -- a clean bill of health from a broken git.
            print("line-class-diff: ERROR -- git diff failed (rc=%d): %s"
                  % (r.returncode, (r.stderr or "").strip()), file=sys.stderr)
            return 2
        files = [f for f in r.stdout.split("\n") if f.strip().endswith(".md")]
    if not files:
        print("line-class-diff: no markdown files changed vs %s" % args.ref)
        return 0

    results = [check(f, args.ref) for f in files]
    bad = [r for r in results if r["status"] == "DEFECT"]
    # F-3: a skipped file was never compared, so it must not inflate the
    # reviewed count -- that is how "CLEAN - N files" overstates coverage.
    skipped = [r for r in results if r["status"] == "skipped"]
    compared = len(results) - len(skipped)

    if args.json:
        print(json.dumps({"results": results, "defect_count": len(bad),
                          "compared": compared, "skipped": len(skipped)}, indent=2))
    else:
        for r in bad:
            print("DEFECT: %s" % r["path"])
            if r["front_matter_lost"]:
                print("  FRONT MATTER LOST — the file's identity block is gone "
                      "(name/description/triggers). This is the g-115-7706 class.")
            for k, v in r["delta"].items():
                print("  %-15s -%d / +%d" % (k, v["removed_count"], v["added_count"]))
                for ln in v["removed"][:4]:
                    print("      REMOVED: %s" % ln[:100])
                for ln in v["added"][:2]:
                    print("      added:   %s" % ln[:100])
        if not bad:
            print("line-class-diff: CLEAN — %d markdown file(s) compared, no line-class "
                  "removals vs %s" % (compared, args.ref))
        if skipped:
            # Stated on BOTH the clean and defect paths: a skipped file is
            # unexamined, and silence about it reads as coverage.
            print("  note: %d file(s) SKIPPED (not examined): %s"
                  % (len(skipped), ", ".join("%s [%s]" % (r["path"], r["reason"])
                                             for r in skipped[:5])))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
