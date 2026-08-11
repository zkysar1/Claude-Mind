#!/usr/bin/env python3
"""promotion-plan-triage — mechanical triage of a DO-NOT-PROMOTE plan verdict.

The promote --plan verdict flags files whose DEST copy carries lines the
transformed seed lacks ("prod-ahead"). That single observation has FOUR
causes, only one of which should stop a promotion (see
core/config/conventions/promotion-runbook.md Phase 4):

  DEST-FROZEN   dest repo has zero commits since the last promote-PR merge
                and a clean tree -> nothing at dest CAN be authored; every
                flag is seed-forward-motion.               (repo-level proof)
  SEED_MOTION   dest file byte-equal to transform(prior-tag:file) -> the
                file is untouched since the prior plant; the frontier moved.
                                                            (per-file proof)
  SYNC_VINTAGE  dest file's last-writer commit is a framework sync/plant
                commit -> corroborates seed-motion where the byte-compare
                cannot run (file absent at prior tag, decode issues, etc).
  AUTHORED      none of the above -> a resident agent wrote it. STOP and
                back-port UP (guard-119) before forcing.

Both hand-runs of this triage (v2.8.10, 2026-08-01: 18/18 at the staging
hop, 108/108 at the prod hop) reached zero unexplained residue; this tool
is that procedure made deterministic. It is READ-ONLY: no repo mutation,
no world access, no daemon.

Usage:
  py -3 core/scripts/promotion-plan-triage.py \
     --source <repo-that-ran-the-promote> --target <dest-clone> \
     --plan-log <promote-run-log-with-flag-lines> \
     [--prior-tag vX.Y.Z] [--json]

  --prior-tag is auto-detected from the dest's newest promote-PR merge
  subject ("... promote/vX.Y.Z") when omitted.

Exit codes:
  0  no AUTHORED residue -- every flag mechanically excused; the printed
     ledger is force-past-plan-ready
  2  AUTHORED residue present -- back-port those files UP, then re-run
  3  usage / environment error (missing repo, unparseable log, no flags)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# Plan flag lines: "   <flag-emoji> path/to/file  (N dest-only line(s)) — DO NOT PROMOTE OVER"
# Parsed by the sentence tail, not the emoji, so log encoding mangling can't hide flags.
_FLAG_RE = re.compile(r"^\s*\S*\s+(?P<path>[^\s]+)\s+\((?P<n>\d+) dest-only line\(s\)\)")
_PROMOTE_MERGE_RE = re.compile(r"Merge pull request #\d+ .*promote/(?P<tag>v\d+\.\d+\.\d+)")
# Last-writer subjects that mean "a plant/sync wrote this, not a resident agent".
_SYNC_SUBJECT_RE = re.compile(
    r"(^chore: sync framework)"
    r"|(^Merge pull request #\d+ .*promote/)"
    r"|(seed[- ]plant)"
    r"|(framework cutover)",
    re.IGNORECASE,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=False, check=False,
    )


def _git_text(repo: Path, *args: str) -> str:
    r = _git(repo, *args)
    return r.stdout.decode("utf-8", errors="replace").strip()


def parse_plan_flags(log_path: Path) -> list:
    flags = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "dest-only line(s)" not in line:
            continue
        m = _FLAG_RE.match(line)
        if m:
            flags.append({"path": m.group("path"), "dest_only_lines": int(m.group("n"))})
    # de-dup preserving order (a log may contain several plan sections)
    seen, out = set(), []
    for f in flags:
        if f["path"] not in seen:
            seen.add(f["path"])
            out.append(f)
    return out


def detect_prior_tag(target: Path) -> str | None:
    subjects = _git_text(target, "log", "--format=%s", "-50").splitlines()
    for s in subjects:
        m = _PROMOTE_MERGE_RE.search(s)
        if m:
            return m.group("tag")
    return None


def dest_frozen_at_last_plant(target: Path) -> dict:
    """Repo-level proof: HEAD is the newest promote-PR merge and tree clean."""
    head_subject = _git_text(target, "log", "-1", "--format=%s")
    dirty = _git_text(target, "status", "--porcelain")
    is_promote_merge = bool(_PROMOTE_MERGE_RE.search(head_subject))
    return {
        "frozen": is_promote_merge and not dirty,
        "head_subject": head_subject,
        "dirty_files": len(dirty.splitlines()) if dirty else 0,
    }


def _load_transformations(source: Path):
    import yaml
    manifest = yaml.safe_load(
        (source / "core" / "config" / "seed-manifest.yaml").read_text(encoding="utf-8")
    )
    return manifest.get("transformations", [])


def classify_file(source: Path, target: Path, rel: str, prior_tag: str,
                  transformations, xform) -> dict:
    entry = {"path": rel, "class": None, "evidence": None}

    dest_file = target / rel
    if not dest_file.is_file():
        entry["class"] = "GONE_AT_DEST"
        entry["evidence"] = "flagged file no longer exists at dest"
        return entry

    # (a) byte-compare dest vs transform(prior-tag:file)
    show = _git(source, "show", f"{prior_tag}:{rel}")
    if show.returncode == 0:
        try:
            prior = show.stdout.decode("utf-8", errors="strict")
            transformed, _ids, _skip = xform.transform_file(
                rel, prior, transformations, source)
            dest_raw = dest_file.read_bytes().decode("utf-8", errors="strict")
            if dest_raw == transformed:
                entry["class"] = "SEED_MOTION"
                entry["evidence"] = f"dest byte-equal transform({prior_tag}:{rel})"
                return entry
            # dest clones may CRLF-translate on checkout; a newline-normalized
            # match is still proof the CONTENT is the prior plant's.
            if dest_raw.replace("\r\n", "\n") == transformed.replace("\r\n", "\n"):
                entry["class"] = "SEED_MOTION"
                entry["evidence"] = (f"dest equals transform({prior_tag}:{rel}) "
                                     "after newline normalization")
                return entry
        except UnicodeDecodeError:
            pass  # binary-ish; fall through to last-writer

    # (b) last-writer classification
    last = _git_text(target, "log", "-1", "--format=%H %s", "--", rel)
    if last:
        sha, _, subject = last.partition(" ")
        if _SYNC_SUBJECT_RE.search(subject):
            entry["class"] = "SYNC_VINTAGE"
            entry["evidence"] = f"last-writer {sha[:8]} '{subject[:70]}' is a sync/plant commit"
            return entry
        entry["class"] = "AUTHORED"
        entry["evidence"] = f"last-writer {sha[:8]} '{subject[:70]}'"
        return entry

    entry["class"] = "AUTHORED"
    entry["evidence"] = "untracked at dest (no git history) — treat as authored"
    return entry


def build_ledger(result: dict) -> str:
    lines = []
    frozen = result["dest_frozen"]
    if frozen["frozen"]:
        lines.append(
            f"REPO-LEVEL PROOF: dest HEAD is the promote-PR merge "
            f"('{frozen['head_subject'][:70]}'), tree clean, 0 commits since — "
            f"no dest-only line can be prod-authored; all "
            f"{len(result['files'])} flags are seed-forward-motion.")
    counts = result["counts"]
    lines.append("Per-file: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for f in result["files"]:
        lines.append(f"  [{f['class']}] {f['path']} — {f['evidence']}")
    if counts.get("AUTHORED"):
        lines.append(
            f"RESIDUE: {counts['AUTHORED']} AUTHORED file(s) — back-port UP "
            "(guard-119) before forcing. DO NOT use this ledger as a "
            "--force-past-plan justification.")
    else:
        lines.append("No authored residue — ledger is force-past-plan-ready.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, type=Path,
                    help="repo that ran the promote (holds tags + manifest)")
    ap.add_argument("--target", required=True, type=Path, help="dest clone")
    ap.add_argument("--plan-log", required=True, type=Path,
                    help="promote run log containing the plan flag lines")
    ap.add_argument("--prior-tag", default=None,
                    help="release last planted at dest (auto-detected when omitted)")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    for p, name in ((args.source, "source"), (args.target, "target")):
        if not (p / ".git").exists() and not (p / ".git").is_file():
            print(f"promotion-plan-triage: --{name} is not a git repo: {p}",
                  file=sys.stderr)
            return 3
    if not args.plan_log.is_file():
        print(f"promotion-plan-triage: --plan-log not found: {args.plan_log}",
              file=sys.stderr)
        return 3

    flags = parse_plan_flags(args.plan_log)
    if not flags:
        print("promotion-plan-triage: no plan flag lines found in log "
              "(nothing to triage)", file=sys.stderr)
        return 3

    prior_tag = args.prior_tag or detect_prior_tag(args.target)
    if not prior_tag:
        print("promotion-plan-triage: could not auto-detect prior tag from dest "
              "promote-PR merges; pass --prior-tag", file=sys.stderr)
        return 3

    import _seed_transforms as xform
    transformations = _load_transformations(args.source)

    frozen = dest_frozen_at_last_plant(args.target)
    files = []
    for f in flags:
        c = classify_file(args.source, args.target, f["path"], prior_tag,
                          transformations, xform)
        c["dest_only_lines"] = f["dest_only_lines"]
        # Repo-level proof upgrades every non-authored-provable file: with zero
        # commits since the plant, even a failed byte-compare cannot be authored
        # (the mismatch is transform-config motion, not dest writes).
        if frozen["frozen"] and c["class"] in ("AUTHORED", "SYNC_VINTAGE", "GONE_AT_DEST"):
            c["class"] = "SEED_MOTION"
            c["evidence"] = f"repo-frozen proof (was: {c['evidence']})"
        files.append(c)

    counts: dict = {}
    for c in files:
        counts[c["class"]] = counts.get(c["class"], 0) + 1

    result = {
        "prior_tag": prior_tag,
        "dest_frozen": frozen,
        "counts": counts,
        "files": files,
        "authored_residue": counts.get("AUTHORED", 0),
    }
    if args.as_json:
        print(json.dumps(result, indent=2))
    else:
        print(build_ledger(result))
    return 2 if counts.get("AUTHORED") else 0


if __name__ == "__main__":
    sys.exit(main())
