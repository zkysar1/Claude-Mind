"""okf-bundle-conformance — check an OKF transfer bundle against the SHAPE contract.

Producer-agnostic: point it at any bundle directory and it asserts the invariants
in ``core/config/conventions/transfer-bundle-export-shape.md``. It exists because
that format now has TWO producers (this repo's ``knowledge-export.py
write_okf_bundle`` and the sibling wiki daemon's ``_okf_doc``), and the first
divergence between them — one emitting ``type: concept`` where the other emitted
``type: node`` — was caught by hand. Nothing would have caught the second.

WHAT THIS DELIBERATELY DOES NOT CHECK (g-115-3266): field-set parity between
producers. The convention is explicit — "Not a field-by-field schema ... field
names are the producer's choice" — and invariant 4 makes unknown keys a
CONSUMER obligation, not a producer one. A checker that demanded both producers
emit the same keys would enforce the opposite of the contract it claims to
verify. So the checks below are all statements the convention actually makes.

Checks, each traced to its invariant:

  fm_parseable   (inv 2) every concept .md opens with a ``---``-fenced YAML block
                         that parses to a mapping.
  type_present   (inv 3) that mapping carries a non-empty string ``type`` — the
                         one required key, the thing a consumer routes on.
  prose_in_fm    (inv 2) a document whose frontmatter holds a long prose scalar
                         while its body is EMPTY. Invariant 2 defines a concept as
                         "one markdown document ... human- and agent-readable as
                         plain text"; prose parked in metadata with nothing under
                         the heading defeats that, and any reader rendering the
                         body shows a blank page. This is a real, measured
                         producer bug shape, not a hypothetical: a passthrough
                         emitter that does not recognise a field name routes the
                         payload into frontmatter and leaves the body empty.

Dangling links are NOT checked — invariant 6 makes a broken link a frontier
marker rather than an error. A missing index is NOT checked — invariant 7 makes
it optional.

Usage:
    py -3 core/scripts/okf-bundle-conformance.py <bundle-dir> [--json]
                                                 [--prose-threshold N]

Exit: 0 conforms · 1 violations found · 2 unusable input.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

#: A frontmatter scalar at least this long is prose, not metadata. Chosen well
#: above real metadata values (titles, keys, ISO stamps, category names all sit
#: far below) so a long title cannot trip the check on its own.
DEFAULT_PROSE_THRESHOLD = 200


def split_front_matter(text: str) -> tuple[str | None, str]:
    """Return ``(frontmatter_yaml, body)``. ``None`` frontmatter = no valid fence."""
    if not text.startswith("---"):
        return None, text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, text  # unterminated fence


def body_is_empty(body: str) -> bool:
    """True when the body carries no prose beyond markdown heading lines.

    A document rendered as `# Title` and nothing else has an empty body for a
    reader's purposes, even though the string is non-empty.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return False
    return True


def check_document(path: Path, rel: str, prose_threshold: int) -> list[dict[str, str]]:
    """Check one concept document. Returns a list of violation dicts (empty = conforms)."""
    out: list[dict[str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [{"file": rel, "check": "fm_parseable", "detail": f"unreadable: {exc}"}]

    fm_raw, body = split_front_matter(text)
    if fm_raw is None:
        return [{"file": rel, "check": "fm_parseable",
                 "detail": "no ---fenced YAML frontmatter block (invariant 2)"}]
    try:
        fm = yaml.safe_load(fm_raw)
    except yaml.YAMLError as exc:
        return [{"file": rel, "check": "fm_parseable",
                 "detail": f"frontmatter is not parseable YAML: {exc}"}]
    if not isinstance(fm, dict):
        return [{"file": rel, "check": "fm_parseable",
                 "detail": f"frontmatter parsed to {type(fm).__name__}, not a mapping"}]

    kind = fm.get("type")
    if not isinstance(kind, str) or not kind.strip():
        out.append({"file": rel, "check": "type_present",
                    "detail": "missing or empty `type` discriminator (invariant 3) — "
                              "a consumer has nothing to route on"})

    if body_is_empty(body):
        for key, value in fm.items():
            if key == "type" or not isinstance(value, str):
                continue
            if len(value) >= prose_threshold:
                out.append({
                    "file": rel, "check": "prose_in_fm",
                    "detail": f"frontmatter key `{key}` holds {len(value)} chars of prose "
                              f"while the document body is empty (invariant 2) — a reader "
                              f"rendering the body sees a blank page",
                })
    return out


def check_bundle(root: Path, prose_threshold: int = DEFAULT_PROSE_THRESHOLD) -> dict[str, object]:
    """Check every ``.md`` under ``root``. Returns a JSON-shaped report."""
    docs = sorted(root.rglob("*.md"))
    violations: list[dict[str, str]] = []
    for path in docs:
        violations.extend(
            check_document(path, path.relative_to(root).as_posix(), prose_threshold)
        )
    by_check: dict[str, int] = {}
    for v in violations:
        by_check[v["check"]] = by_check.get(v["check"], 0) + 1
    return {
        "bundle": str(root),
        "documents": len(docs),
        "conforms": not violations,
        "violation_count": len(violations),
        "by_check": by_check,
        "violations": violations,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check an OKF bundle against the shape contract.")
    ap.add_argument("bundle_dir")
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    ap.add_argument("--prose-threshold", type=int, default=DEFAULT_PROSE_THRESHOLD)
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    root = Path(args.bundle_dir)
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    report = check_bundle(root, args.prose_threshold)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        # A zero-document run is reported as such, never as a pass — an empty
        # directory and a clean bundle must not read identically (rb-245).
        if not report["documents"]:
            print(f"NO DOCUMENTS under {root} — nothing was checked (this is not a pass)")
            return 2
        print(f"{root}: {report['documents']} document(s)")
        if report["conforms"]:
            print("CONFORMS — all shape invariants hold")
        else:
            print(f"VIOLATIONS: {report['violation_count']} ({report['by_check']})")
            for v in report["violations"]:
                print(f"  [{v['check']}] {v['file']}: {v['detail']}")
    if not report["documents"]:
        return 2
    return 0 if report["conforms"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
