#!/usr/bin/env python3
"""seed-damage-scan — did the seed TRANSFORM damage a derived copy?

This answers a question none of the sibling tools answer, and the distinction
is the whole point:

  promotion-preflight     is the target AHEAD of the source? (would I clobber?)
  promotion-plan-triage   WHY is the target ahead -- 4 causes, one of which stops
                          the promotion.               (dest-only lines: dest HAS)
  THIS                    did the transform DELETE something it should not have?
                                                  (source-only: dest LACKS)

The first two look at lines the DEST carries and the seed lacks. This looks the
opposite direction, and "how far behind is the dest" is exactly the confound it
has to survive: a derived copy is legitimately behind on most files, so a naive
"token present upstream, absent downstream" scan measures the VERSION GAP and
calls it damage.

Measured (rb-6267, g-115-3563, the dev -> staging hop @ 8ca28eca, 2026-08-01):
the naive scan reported 4,829 sites across 1,033 files. All noise.
Three constraints dropped 4,829 -> 2, and BOTH survivors were the transform
working as designed. Those three constraints ARE this tool:

  1. SCOPE TO THE TRANSFORM'S OWN VOCABULARY, read from the manifest at scan
     time, never hardcoded. A damage site is a token the TRANSFORM could have
     touched -- not any token that differs.
  2. USE THE TRANSFORM'S OWN CONTEXT PREDICATE by importing it. A comment-context
     bug is precisely a disagreement about where the context ENDS, so a
     hand-rolled splitter measures YOUR boundary rather than the code's
     (.claude/rules/probe-with-canonical-code-path.md). We import `_check_context`
     and `_applies_to` from `_seed_transforms.py` -- the same functions the
     transform itself calls -- so this scanner cannot drift from the transform.
  3. REQUIRE A PURE DELETION. If anything was ADDED to the line it is a
     substitution -- ordinary evolution -- and must not count.

WHY A ZERO HERE IS THE DANGEROUS ANSWER, and what this tool does about it.
The honest output on a healthy hop is 0, and 0 is also what you get from a
mistyped path, an empty vocabulary, or a glob that matched nothing. Those are
indistinguishable in a bare count, and this is the one verdict an operator most
wants to believe (guard-1587). So the denominator is ALWAYS reported -- rules,
vocabulary size, files compared, line pairs -- and a scan whose denominator is
structurally empty EXITS 3 rather than printing a reassuring zero. A zero is
only evidence when it comes with the population it was drawn from.

For the same reason `--source`/`--target` are reported verbatim: where a clone
sits changes which files the walk enumerates, and nothing else in the output
would show that the scope moved (guard-3099).

Usage:
  py -3 core/scripts/seed-damage-scan.py --source <seed-repo> --target <dest-clone>
  py -3 core/scripts/seed-damage-scan.py --source S --target T --json

Exit codes:
  0  scanned a non-empty population, found no damage
  2  DAMAGE SITES FOUND -- each is a pure deletion of a transform-vocabulary
     token in the context the transform acts on. Read them; the transform
     working as designed also lands here (both 2026-08-01 survivors did).
  3  usage / empty-population error -- the scan could not address anything, so
     no verdict is available. NOT a pass.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# The transform's OWN predicates -- imported, never re-implemented (constraint 2).
from _seed_transforms import _applies_to, _check_context  # noqa: E402

DEFAULT_MANIFEST = SCRIPT_DIR.parent / "config" / "seed-manifest.yaml"

# Only the transform types that can REMOVE text are damage vectors. inline_edit
# and file_replace substitute rather than strip, and a substitution that went
# wrong is a content bug this scan is not built to find -- saying so is cheaper
# than a reader assuming the scan covered them.
DELETION_TYPES = ("word_list_strip",)


def load_vocabulary(manifest_path: Path) -> list:
    """Deletion rules from the manifest, as {id, words, context, applies_to}."""
    import yaml
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = yaml.safe_load(fh)
    rules = []
    for rule in manifest.get("transformations") or []:
        if rule.get("type") not in DELETION_TYPES:
            continue
        words = [w for w in (rule.get("words") or []) if str(w).strip()]
        if not words:
            continue
        rules.append({
            "id": rule.get("id") or "(unnamed)",
            "words": words,
            "context": rule.get("when_in_context") or "comment",
            "applies_to": rule.get("applies_to") or ["**/*"],
            "raw": rule,
        })
    return rules


def _tokens(line: str) -> set:
    return set(line.split())


def is_pure_deletion(up_line: str, down_line: str) -> bool:
    """True when `down_line` only LOST content relative to `up_line`.

    Constraint 3. A downstream token absent upstream means the line was edited,
    not stripped -- ordinary divergence between two repos at different vintages.
    Token-set containment rather than a character diff, deliberately: the
    transform strips whole words, and a character-level test would flag
    re-indentation and rewrapping as additions.
    """
    return _tokens(down_line) <= _tokens(up_line)


def scan_file(rel: str, up_text: str, down_text: str, rules: list) -> tuple:
    """(damage_sites, pairs_compared) for one file present in both trees."""
    sites = []
    up_lines = up_text.splitlines()
    down_lines = down_text.splitlines()
    suffix = Path(rel).suffix
    pairs = 0

    matcher = difflib.SequenceMatcher(None, up_lines, down_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            # 'delete' is a whole line gone -- that is a version gap, not a
            # word-strip; 'equal'/'insert' cannot lose a word from a line.
            continue
        for off in range(min(i2 - i1, j2 - j1)):
            up_line = up_lines[i1 + off]
            down_line = down_lines[j1 + off]
            pairs += 1
            prev = up_lines[i1 + off - 1] if (i1 + off) > 0 else ""
            if not is_pure_deletion(up_line, down_line):
                continue                                    # constraint 3
            for rule in rules:
                if not _applies_to(rel, rule["raw"]):
                    continue                                # constraint 1 (scope)
                if not _check_context(up_line, rule["context"], suffix, prev):
                    continue                                # constraint 2
                for word in rule["words"]:
                    if word in up_line and word not in down_line:
                        sites.append({
                            "file": rel, "rule": rule["id"], "word": word,
                            "context": rule["context"],
                            "upstream_line": up_line.strip()[:200],
                            "downstream_line": down_line.strip()[:200],
                        })
    return sites, pairs


def _repo_files(root: Path) -> dict:
    """repo-relative posix path -> Path, skipping .git and binary-ish trees."""
    out = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel.startswith(".git/") or "/__pycache__/" in f"/{rel}":
            continue
        out[rel] = p
    return out


def run(source: Path, target: Path, manifest_path: Path) -> dict:
    rules = load_vocabulary(manifest_path)
    vocab_size = sum(len(r["words"]) for r in rules)

    src_files = _repo_files(source)
    dst_files = _repo_files(target)
    both = sorted(set(src_files) & set(dst_files))

    in_scope = [rel for rel in both
                if any(_applies_to(rel, r["raw"]) for r in rules)]

    sites, pairs, compared = [], 0, 0
    for rel in in_scope:
        try:
            up = src_files[rel].read_text(encoding="utf-8")
            down = dst_files[rel].read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue                       # binary / unreadable: not a damage claim
        compared += 1
        s, p = scan_file(rel, up, down, rules)
        sites.extend(s)
        pairs += p

    return {
        # The verdict is meaningless without these; see the module docstring.
        "source": str(source), "target": str(target),
        "manifest": str(manifest_path),
        "rules_scanned": [r["id"] for r in rules],
        "vocabulary_size": vocab_size,
        "files_in_both_trees": len(both),
        "files_in_transform_scope": len(in_scope),
        "files_compared": compared,
        "line_pairs_compared": pairs,
        "damage_sites": sites,
        "damage_count": len(sites),
    }


def empty_population_reason(result: dict):
    """Why this scan could not address anything -- or None if it could.

    guard-1587: a zero from a scan that never looked at anything is not a pass,
    and it reads exactly like one. Each branch names the specific denominator
    that was empty so the operator fixes the right thing.
    """
    if not result["rules_scanned"]:
        return ("no deletion-type transformations in the manifest -- the "
                "vocabulary is empty, so no damage COULD be reported")
    if result["vocabulary_size"] == 0:
        return "every deletion rule has an empty word list"
    if result["files_in_both_trees"] == 0:
        return ("no file path exists in BOTH trees -- check --source/--target "
                "(a wrong root yields a clean zero)")
    if result["files_in_transform_scope"] == 0:
        return ("no shared file matches any rule's applies_to globs -- the "
                "transform vocabulary cannot apply to this pair")
    if result["files_compared"] == 0:
        return "every in-scope file was unreadable or binary"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", required=True, help="the seed repo the transform ran FROM")
    ap.add_argument("--target", required=True, help="the derived clone to check for damage")
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    source, target = Path(args.source), Path(args.target)
    manifest = Path(args.manifest)
    for label, p in (("--source", source), ("--target", target), ("--manifest", manifest)):
        if not p.exists():
            print(f"ERROR: {label} does not exist: {p}", file=sys.stderr)
            return 3

    result = run(source, target, manifest)
    reason = empty_population_reason(result)
    if reason:
        result["verdict"] = "no-population"
        result["reason"] = reason
        if args.json:
            print(json.dumps(result, indent=1))
        else:
            print(f"[seed-damage-scan] NO VERDICT AVAILABLE: {reason}", file=sys.stderr)
        return 3

    result["verdict"] = "damage-found" if result["damage_count"] else "clean"
    if args.json:
        print(json.dumps(result, indent=1))
        return 2 if result["damage_count"] else 0

    print(f"[seed-damage-scan] {source} -> {target}")
    print(f"  rules={','.join(result['rules_scanned'])}  "
          f"vocabulary={result['vocabulary_size']} word(s)")
    print(f"  files in both trees={result['files_in_both_trees']}  "
          f"in transform scope={result['files_in_transform_scope']}  "
          f"compared={result['files_compared']}")
    print(f"  line pairs compared={result['line_pairs_compared']}")
    if not result["damage_count"]:
        print("  CLEAN — no pure-deletion of a transform-vocabulary token. "
              "The population above is what that zero was drawn from.")
        return 0
    print(f"  DAMAGE SITES: {result['damage_count']}")
    for s in result["damage_sites"][:40]:
        print(f"    {s['file']} [{s['rule']}/{s['word']}]")
        print(f"      up:   {s['upstream_line']}")
        print(f"      down: {s['downstream_line']}")
    if result["damage_count"] > 40:
        print(f"    ... {result['damage_count'] - 40} more (use --json for all)")
    print("  NOTE: the transform working AS DESIGNED also lands here — both "
          "survivors of the 4829->2 reduction did. Read each site.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
