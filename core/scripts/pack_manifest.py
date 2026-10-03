"""pack_manifest — the loading manifest for knowledge packs: what an agent is about to load.

Reads one or more pack bundles (knowledge pack format v1: a directory holding ``pack.md``
plus ``nodes/``, ``guardrails/`` and ``lessons/``) and returns, per pack, what a front door
can show while the agent starts: the pack's name, version and language, how many nodes,
rules (guardrails) and lessons it carries, and a few node titles. The counts come from the
files, never from a number the pack declares about itself.

    manifest = build_manifest([Path("bundles/a"), Path("bundles/b")])
    manifest["packs"][0]["nodes"]      # nodes in the first pack
    manifest["totals"]["nodes"]        # DISTINCT node ids across the packs (format 5.6)

SCOPE. This is the read side only. What is refused here is whatever would make the numbers
false: a bundle that cannot be read as the format lays it out (sections 3-5) and a request that
names one pack twice; every reason is listed, not the first only (8.1). Refusals that depend on
the installing account or on the publisher's current status (rating, rights confirmation, a
withdrawn or taken-down pack: 8.1 items 3-5), the shape check (``okf-bundle-conformance.py``,
8.1 item 1) and the format's length limits belong to the installer and are not repeated here.
The durable record of an installed pack is the install ledger (8.8); this manifest is a view of
a bundle, and its ``pack_sha256`` is the ledger's ``pack_sha256`` (section 7).

CLI:  py -3 core/scripts/pack_manifest.py <bundle-dir>... [--samples N]
      exit 0 manifest on stdout | 1 refused, reasons on stdout | 2 bad usage
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

import yaml

FORMAT_VERSION = 1
DEFAULT_SAMPLES = 3

#: A bound of this reader, not a format rule: a pack file is prose of a few thousand
#: characters (format 5.3), so a file this large is not one, and refusing it keeps a single
#: hostile bundle from pinning memory.
MAX_FILE_BYTES = 1 << 20

#: directory -> (front matter ``type``, id field, id pattern). The directory names and the
#: type values are the ones the framework's own export writes (format section 3).
_ITEMS = {
    "nodes": ("node", "node_id", re.compile(r"^kn-[0-9a-f]{32}$")),
    "guardrails": ("guardrail", "guardrail_id", re.compile(r"^kg-[0-9a-f]{32}$")),
    "lessons": ("lesson", "lesson_id", re.compile(r"^kl-[0-9a-f]{32}$")),
}
_FAMILY_ID = re.compile(r"^kp-[0-9a-f]{32}$")
_LANGUAGE = re.compile(r"^[a-z]{2,8}(-[A-Za-z0-9]{1,8})*$")
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class PackRefused(ValueError):
    """The request cannot be described truthfully.

    ``reasons`` maps each bundle path, exactly as given, to EVERY reason it was refused; the
    key ``""`` carries a problem with the request itself.
    """

    def __init__(self, reasons: dict[str, list[str]]) -> None:
        self.reasons = reasons
        super().__init__(
            "; ".join(f"{b or 'request'}: {r}" for b, rs in reasons.items() for r in rs)
        )


def _front_matter(data: bytes, rel: str, reasons: list[str]) -> dict | None:
    """The file's front matter as a mapping, or ``None`` after appending why not."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        reasons.append(f"{rel}: is not valid UTF-8")
        return None
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        reasons.append(f"{rel}: does not open with a --- front matter line")
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    if end is None:
        reasons.append(f"{rel}: front matter is never closed")
        return None
    try:
        front = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        reasons.append(f"{rel}: front matter is not valid YAML ({type(exc).__name__})")
        return None
    if not isinstance(front, dict):
        reasons.append(f"{rel}: front matter is not a mapping")
        return None
    return front


def _check_manifest(front: dict, reasons: list[str]) -> None:
    """Append a reason for each ``pack.md`` field this reader relies on that is wrong."""

    def bad(field: str, why: str) -> None:
        reasons.append(f"pack.md: {field} {why}")

    if front.get("type") != "pack-manifest":
        bad("type", f"is {front.get('type')!r}, not 'pack-manifest'")
    declared = front.get("format_version")
    if type(declared) is not int or declared != FORMAT_VERSION:
        bad("format_version", f"is {declared!r}; this reader knows only {FORMAT_VERSION}")
    family, language = front.get("family_id"), front.get("language")
    if not (isinstance(family, str) and _FAMILY_ID.match(family)):
        bad("family_id", "is missing or not kp- plus 32 lowercase hex characters")
    if not (isinstance(language, str) and _LANGUAGE.match(language)):
        bad("language", f"is {language!r}, not a language tag with a lowercase primary subtag")
    elif isinstance(family, str) and front.get("pack_id") != f"{family}.{language}":
        bad("pack_id", f"is {front.get('pack_id')!r}, not family_id.language")
    version = front.get("version")
    if version is None:
        bad("version", "is missing")
    elif not isinstance(version, str):
        # 2.1: an unquoted 1.10 loads as the number 1.1, a loss that cannot be undone.
        bad("version", f"loaded as a number ({version!r}); quote it in YAML, as '1.0'")
    elif not _VERSION.match(version):
        bad("version", f"is {version!r}, not MAJOR.MINOR")
    name = front.get("name")
    if not (isinstance(name, str) and name.strip()):
        bad("name", "is missing, empty or not text")


def _list_files(root: Path, reasons: list[str]) -> list[tuple[str, int]]:
    """``(relative posix path, size)`` of every regular file, in byte order of the path.

    A symlink or any other non-regular entry is a refusal: a bundle is one self-contained
    directory (format 3, invariant 1), and following a link would let a bundle name a file
    that is not in it (or open a pipe and block).
    """
    found: list[tuple[str, int]] = []

    def unlistable(exc: OSError) -> None:  # os.walk would skip the directory in silence
        where = Path(exc.filename).relative_to(root).as_posix() if exc.filename else "."
        reasons.append(f"{where}: cannot be listed ({exc.strerror or type(exc).__name__})")

    for dirpath, dirnames, filenames in os.walk(root, onerror=unlistable):
        dirnames.sort()
        for name in dirnames + sorted(filenames):
            path = os.path.join(dirpath, name)
            rel = Path(path).relative_to(root).as_posix()
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                reasons.append(f"{rel}: is a symlink; a bundle is one self-contained directory")
            elif name in filenames:
                if stat.S_ISREG(info.st_mode):
                    found.append((rel, info.st_size))
                else:
                    reasons.append(f"{rel}: is not a regular file")
    found.sort(key=lambda entry: entry[0].encode("utf-8"))
    return found


def _read_pack(root: Path, samples: int) -> tuple[dict | None, dict[str, set[str]], list[str]]:
    """Read one bundle -> ``(its manifest entry or None, item ids by kind, every reason)``."""
    reasons: list[str] = []
    ids: dict[str, set[str]] = {kind: set() for kind in _ITEMS}
    if not root.is_dir():
        return None, ids, ["is not a directory"]
    manifest: dict | None = None
    saw_manifest = False
    item_files = 0
    hash_lines: list[str] = []
    unrecognized: list[str] = []
    node_samples: list[tuple[bool, str, str]] = []
    for rel, size in _list_files(root, reasons):
        parts = rel.split("/")
        is_item = len(parts) == 2 and parts[0] in _ITEMS and parts[1].endswith(".md")
        saw_manifest = saw_manifest or rel == "pack.md"
        if is_item:
            item_files += 1
        if size > MAX_FILE_BYTES:
            reasons.append(f"{rel}: is {size} bytes, over this reader's {MAX_FILE_BYTES}-byte bound")
            continue
        try:
            data = (root / rel).read_bytes()
        except OSError as exc:
            reasons.append(f"{rel}: is unreadable ({exc.strerror or type(exc).__name__})")
            continue
        hash_lines.append(f"{hashlib.sha256(data).hexdigest()}  {rel}\n")
        if rel == "pack.md":
            front = _front_matter(data, rel, reasons)
            if front is not None:
                _check_manifest(front, reasons)
                manifest = front
        elif is_item:
            kind = parts[0]
            want_type, id_field, id_pattern = _ITEMS[kind]
            front = _front_matter(data, rel, reasons)
            if front is None:
                continue
            if front.get("type") != want_type:
                reasons.append(
                    f"{rel}: type is {front.get('type')!r}, but a file under {kind}/ "
                    f"must have type {want_type!r}"
                )
            item_id = front.get(id_field)
            if not (isinstance(item_id, str) and id_pattern.match(item_id)):
                reasons.append(f"{rel}: {id_field} is missing or malformed")
            elif item_id in ids[kind]:
                reasons.append(f"{rel}: {id_field} {item_id} is already used in this bundle")
            else:
                ids[kind].add(item_id)
            title = front.get("title")
            if not (isinstance(title, str) and title.strip()):
                reasons.append(f"{rel}: title is missing, empty or not text")
            elif kind == "nodes":
                # Top-level nodes first: they name the pack's subjects, a leaf does not.
                node_samples.append((bool(front.get("parent")), rel, title.strip()))
        else:
            unrecognized.append(rel)
    if not saw_manifest:
        reasons.append("pack.md: is missing from the bundle root")
    if not item_files:
        reasons.append("holds no items: a pack carries at least one node, lesson or guardrail")
    if reasons or manifest is None:
        return None, ids, reasons
    entry = {
        "pack_id": manifest["pack_id"],
        "version": manifest["version"],
        "name": manifest["name"].strip(),
        "language": manifest["language"],
        "nodes": len(ids["nodes"]),
        "guardrails": len(ids["guardrails"]),
        "lessons": len(ids["lessons"]),
        "sample_titles": [title for _, _, title in sorted(node_samples)[:samples]],
        "pack_sha256": hashlib.sha256("".join(hash_lines).encode("utf-8")).hexdigest(),
        "unrecognized": unrecognized,
    }
    return entry, ids, []


def build_manifest(bundle_dirs, samples: int = DEFAULT_SAMPLES) -> dict:
    """The loading manifest for these bundles, in the order given.

    ``{"packs": [{pack_id, version, name, language, nodes, guardrails, lessons, sample_titles,
    pack_sha256, unrecognized}], "totals": {packs, nodes, guardrails, lessons}}``.
    ``totals`` counts DISTINCT item ids: a node carried by two packs lands once (format 5.6),
    so it need not equal the sum of the per-pack counts. ``unrecognized`` lists the files a
    pack carries that are neither its manifest nor an item (format 3, rule 8); they are in
    ``pack_sha256`` but in no count. Raises :class:`PackRefused` listing every reason.
    """
    if samples < 0:
        raise ValueError("samples must be 0 or more")
    if isinstance(bundle_dirs, (str, os.PathLike)):
        bundle_dirs = [bundle_dirs]
    roots = [Path(b) for b in bundle_dirs]
    if not roots:
        raise PackRefused({"": ["no packs given"]})
    refused: dict[str, list[str]] = {}
    packs: list[dict] = []
    distinct: dict[str, set[str]] = {kind: set() for kind in _ITEMS}
    given_as: dict[str, str] = {}
    for root in roots:
        entry, ids, reasons = _read_pack(root, samples)
        if entry is not None and entry["pack_id"] in given_as:
            reasons = [f"pack_id {entry['pack_id']} was already given as {given_as[entry['pack_id']]}"]
        if reasons:
            refused[str(root)] = reasons
            continue
        given_as[entry["pack_id"]] = str(root)
        packs.append(entry)
        for kind, found in ids.items():
            distinct[kind] |= found
    if refused:
        raise PackRefused(refused)
    totals = {"packs": len(packs), **{kind: len(found) for kind, found in distinct.items()}}
    return {"packs": packs, "totals": totals}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Loading manifest for knowledge pack bundles.")
    ap.add_argument("bundles", nargs="+", help="bundle directory, one pack edition each")
    ap.add_argument("--samples", type=int, default=DEFAULT_SAMPLES, help="node titles per pack")
    args = ap.parse_args(argv)
    try:
        manifest = build_manifest(args.bundles, samples=args.samples)
    except PackRefused as exc:
        print(json.dumps({"refused": exc.reasons}, indent=2))
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
