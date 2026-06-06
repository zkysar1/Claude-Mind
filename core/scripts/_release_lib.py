#!/usr/bin/env python3
"""_release_lib.py — Pure-logic core for the cross-world versioning rails.

This module holds ALL of release.sh's decision logic so it is directly
unit-testable (daemon-free, no subprocess, no real git/repo mutation). The
shell wrapper (`release.sh`) is a thin orchestrator that wires git/curl/
atomic-mv around the functions here.

It is NOT a daemon wrapper — it talks to no daemon and reads/writes only
plain files passed by path/env. The no-python-cli-fallback rule
(.claude/rules/no-python-cli-fallback.md) governs the 35 daemon wrappers,
not pure-logic helpers like this one, so the CLI dispatch at the bottom is
legitimate.

Subcommands (called by release.sh; inputs via env per guard-165 — never
interpolate bash vars into a `-c` source string):

    bump <current> <kind>          -> prints new semver (stdout), exit 0
    validate                        -> env-driven; KEY=VALUE on stdout + exit 0
                                       on success, ERROR lines + exit 1 on fail
    compare <a> <b>                 -> prints -1|0|1 (a vs b), exit 0
    seed-latest <path>              -> prints newest version from a RELEASES.json
                                       file (parse-or-fail; exit 1 on malformed)
    build-prepended                 -> env-driven; prints new RELEASES.json array

Design references: rails design (A.1-A.10), omni review deltas H1/M1/M2/M3/M4/Q3,
CW1/CW3 guardrails.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# A strict semver triple. No pre-release / build metadata in v1 of the rails.
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


# --------------------------------------------------------------------------- #
# Semver primitives
# --------------------------------------------------------------------------- #
def parse_version(s: str) -> Tuple[int, int, int]:
    """Parse 'X.Y.Z' into (major, minor, patch). Raise ValueError on bad input."""
    m = VERSION_RE.match(s.strip())
    if not m:
        raise ValueError(f"not a valid semver MAJOR.MINOR.PATCH: {s!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def format_version(t: Tuple[int, int, int]) -> str:
    return f"{t[0]}.{t[1]}.{t[2]}"


def bump_version(current: str, kind: str) -> str:
    """Return the next version. major zeros minor+patch; minor zeros patch."""
    maj, minor, patch = parse_version(current)
    if kind == "major":
        return format_version((maj + 1, 0, 0))
    if kind == "minor":
        return format_version((maj, minor + 1, 0))
    if kind == "patch":
        return format_version((maj, minor, patch + 1))
    raise ValueError(f"unknown bump kind: {kind!r} (expected major|minor|patch)")


def compare_version(a: str, b: str) -> int:
    """-1 if a<b, 0 if equal, 1 if a>b (semver ordering)."""
    ta, tb = parse_version(a), parse_version(b)
    return (ta > tb) - (ta < tb)


# --------------------------------------------------------------------------- #
# RELEASES.json — load (parse-or-fail, M1), chain integrity, duplicate check
# --------------------------------------------------------------------------- #
def load_releases(path: str) -> List[dict]:
    """Load and validate a RELEASES.json file.

    Parse-or-fail (M1): a missing OR present-but-empty/whitespace-only file
    returns [] — BOTH are legitimate first-run bootstrap states. A fresh
    seed/downstream repo that has cut no release yet has an empty (or absent)
    RELEASES.json, and release.sh's FIRST cut must be able to load it as an
    empty chain; raising on empty would make the first-ever release impossible.
    A present file whose content is non-empty but does NOT parse as a JSON
    array raises ValueError rather than silently degrading to "no releases".

    Safety net: the downstream update-classification path (classify_update /
    _cmd_classify_chain) treats even an empty result as FAIL-CLOSED (exit 2) —
    it never proceeds on [] — so an empty/missing feed can never be mistaken
    for "no updates available" there. (review F2/F6: empty==bootstrap is
    deliberate, not a silent-degradation bug.)
    """
    p = Path(path)
    if not p.exists():
        return []
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"RELEASES.json is present but malformed: {e}") from e
    if not isinstance(data, list):
        raise ValueError("RELEASES.json must be a JSON array")
    return data


def newest_version(releases: List[dict]) -> Optional[str]:
    """The version of the first (newest) entry, or None for an empty list."""
    if not releases:
        return None
    v = releases[0].get("version")
    if not v:
        raise ValueError("newest RELEASES.json entry has no 'version'")
    return v


def check_duplicate(releases: List[dict], version: str) -> bool:
    """True if `version` already appears in any entry."""
    return any(e.get("version") == version for e in releases)


def validate_chain(releases: List[dict]) -> Tuple[bool, List[str]]:
    """Verify the newest-first chain is well-formed.

    Rules:
      - versions strictly descending (newest first)
      - each entry's previous_version == the next entry's version
      - the oldest entry's previous_version may be anything (chain root)
    Returns (ok, errors).
    """
    errors: List[str] = []
    if not releases:
        return True, errors
    for i, entry in enumerate(releases):
        v = entry.get("version")
        if not v:
            errors.append(f"entry {i} missing 'version'")
            continue
        try:
            parse_version(v)
        except ValueError as e:
            errors.append(f"entry {i} version invalid: {e}")
            continue
        # Descending order + previous_version linkage with the NEXT (older) entry.
        if i + 1 < len(releases):
            nxt = releases[i + 1].get("version")
            if nxt:
                try:
                    if compare_version(v, nxt) <= 0:
                        errors.append(
                            f"chain not strictly descending: {v} !> {nxt} "
                            f"(entry {i} vs {i + 1})"
                        )
                except ValueError:
                    pass  # malformed next version reported on its own iteration
            prev = entry.get("previous_version")
            if prev != nxt:
                errors.append(
                    f"chain break: entry {i} ({v}) previous_version={prev!r} "
                    f"but next entry is {nxt!r}"
                )
    return (len(errors) == 0), errors


# --------------------------------------------------------------------------- #
# Breaking / cross-world classification (Q3 fail-closed override)
# --------------------------------------------------------------------------- #
def compute_breaking_cross_world(
    bump_kind: str, cross_world: bool, allow_non_breaking_cross_world: bool
) -> Tuple[bool, bool, List[str]]:
    """Return (breaking, cross_world, errors).

    Rules:
      - major bump  -> breaking=True ALWAYS (no escape hatch).
      - cross_world -> breaking=True by DEFAULT (fail-closed) unless the
        explicit, audited --allow-non-breaking-cross-world override is set (Q3).
      - minor/patch non-cross-world -> breaking=False.
    """
    errors: List[str] = []
    breaking = bump_kind == "major"
    if cross_world and not breaking:
        if allow_non_breaking_cross_world:
            breaking = False  # explicit, audited override (Q3)
        else:
            breaking = True  # fail-closed default
    if bump_kind == "major" and allow_non_breaking_cross_world:
        errors.append(
            "--allow-non-breaking-cross-world is invalid for a major bump "
            "(major is breaking by definition)"
        )
    return breaking, cross_world, errors


# --------------------------------------------------------------------------- #
# Recipe gate (CW3) + structural contract validation (H3)
# --------------------------------------------------------------------------- #
def rollback_path_for(recipe_path: str) -> str:
    """Convention: v0.3.0.sh -> v0.3.0-rollback.sh (sibling, same dir)."""
    p = Path(recipe_path)
    stem = p.name[:-3] if p.name.endswith(".sh") else p.name
    # .as_posix() (NOT str()) so the recorded path uses forward slashes on every
    # platform. RELEASES.json is the cross-world artifact: a Windows-native
    # backslash path (str(WindowsPath(...))) would be read by a POSIX seed/
    # downstream as a single literal filename, so validate_recipe_structure's
    # Path(rollback_path).exists() check fails with "rollback file not found".
    # Mirrors upgrade_recipe, which is stored as-passed (already forward-slash).
    return p.with_name(f"{stem}-rollback.sh").as_posix()


# Contract markers (regex, case-insensitive, MULTILINE) a recipe must carry.
# Validated structurally — we never EXECUTE the recipe at release-cut (H3:
# smoke test is syntax-only; the data migration touches external world/+meta/
# state that cannot be safely reproduced in a temp clone).
_RECIPE_REQUIRED = {
    "pre-check": re.compile(r"pre-check", re.IGNORECASE),
    "post-check": re.compile(r"post-check", re.IGNORECASE),
}
# cross_world recipes MUST snapshot the external world/+meta/ paths BEFORE
# migrating, because a git-tag rollback cannot restore paths outside the repo.
_RECIPE_CROSS_WORLD = {
    "world-meta-snapshot": re.compile(r"snapshot", re.IGNORECASE),
    "world-path-copy": re.compile(r"WORLD_PATH", re.IGNORECASE),
    "meta-path-copy": re.compile(r"META_PATH", re.IGNORECASE),
}
_ROLLBACK_REQUIRED = {
    "idempotent": re.compile(r"idempotent", re.IGNORECASE),
    "pre-check": re.compile(r"pre-check", re.IGNORECASE),
    "post-check": re.compile(r"post-check", re.IGNORECASE),
}
# Symmetric to _RECIPE_CROSS_WORLD (H3b, omni#1). A git-tag rollback CANNOT
# restore the external world/+meta/ paths, so a cross_world UPGRADE must
# snapshot them first (enforced above). The matching ROLLBACK is only a real
# rollback if it can RESTORE world/+meta/ FROM that snapshot — otherwise the
# snapshot is a dead artifact and a corrupting migration is irreversible. The
# rollback must therefore reference the snapshot source (snap/SNAP_DIR) AND the
# two restore targets, in EXECUTABLE code (a comment promising a restore is not
# a restore — same comment-stripping discipline as the upgrade snapshot check).
_ROLLBACK_CROSS_WORLD = {
    "snapshot-restore": re.compile(r"snap", re.IGNORECASE),
    "world-path-restore": re.compile(r"WORLD_PATH", re.IGNORECASE),
    "meta-path-restore": re.compile(r"META_PATH", re.IGNORECASE),
}


def validate_recipe_structure(
    recipe_path: str, rollback_path: str, cross_world: bool
) -> Tuple[bool, List[str]]:
    """Structural validation of an upgrade recipe + its rollback (H3 contract).

    Returns (ok, missing_markers). Does NOT execute either script.
    """
    missing: List[str] = []
    rp, bp = Path(recipe_path), Path(rollback_path)
    if not rp.is_file():
        missing.append(f"recipe file not found: {recipe_path}")
        recipe_txt = ""
    else:
        recipe_txt = rp.read_text(encoding="utf-8", errors="replace")
    if not bp.is_file():
        missing.append(f"rollback file not found: {rollback_path}")
        rollback_txt = ""
    else:
        rollback_txt = bp.read_text(encoding="utf-8", errors="replace")

    # Required markers (pre-check/post-check) are section headers — comments are
    # the intended form, so match against the full text.
    for name, rx in _RECIPE_REQUIRED.items():
        if recipe_txt and not rx.search(recipe_txt):
            missing.append(f"recipe missing contract marker: {name}")
    # cross_world markers (snapshot/WORLD_PATH/META_PATH) must appear in
    # EXECUTABLE code — a comment claiming the recipe snapshots world/+meta/ is
    # not a snapshot. Strip comment lines before matching so a comment-only
    # recipe cannot satisfy the H3 snapshot contract.
    if cross_world:
        recipe_code = "\n".join(
            ln for ln in recipe_txt.splitlines() if not ln.lstrip().startswith("#")
        )
        for name, rx in _RECIPE_CROSS_WORLD.items():
            if recipe_txt and not rx.search(recipe_code):
                missing.append(
                    f"cross_world recipe missing marker (must be in executable code, not a comment): {name}"
                )
    for name, rx in _ROLLBACK_REQUIRED.items():
        if rollback_txt and not rx.search(rollback_txt):
            missing.append(f"rollback missing contract marker: {name}")
    # cross_world rollback MUST restore world/+meta/ from the snapshot in
    # EXECUTABLE code (H3b, omni#1). Strip comments before matching, identical
    # to the upgrade snapshot check — a comment claiming a restore is not one.
    if cross_world:
        rollback_code = "\n".join(
            ln for ln in rollback_txt.splitlines() if not ln.lstrip().startswith("#")
        )
        for name, rx in _ROLLBACK_CROSS_WORLD.items():
            if rollback_txt and not rx.search(rollback_code):
                missing.append(
                    f"cross_world rollback missing restore marker (must be in executable code, not a comment): {name}"
                )

    return (len(missing) == 0), missing


# --------------------------------------------------------------------------- #
# CW1 — version SSOT. No file may re-declare a competing system version.
# --------------------------------------------------------------------------- #
def check_version_ssot(project_root: str) -> Tuple[bool, List[str]]:
    """Verify mind_api/src/__init__.py __version__ is the SOLE version source.

    Scans for the known competing-declaration shapes (the dual-source class
    bug, exp-g-115-220). Deliberately narrow to avoid false positives on
    files that legitimately list version DATA (RELEASES.json, compatibility
    matrices use `version:` as data, not as a competing SSOT declaration).

    Returns (ok, violations).
    """
    root = Path(project_root)
    violations: List[str] = []
    canonical = (root / "mind_api" / "src" / "__init__.py").resolve()

    # (1) profile.yaml: a semver `version:` key (the historical dual source).
    profile = root / "core" / "config" / "profile.yaml"
    if profile.is_file():
        for i, line in enumerate(profile.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^\s+version:\s*[\"']?\d+\.\d+\.\d+", line):
                violations.append(f"{profile}:{i}: competing semver version declaration")

    # (2) stray __version__ assignments in any .py other than the canonical one.
    for sub in ("mind_api", "core/scripts"):
        base = root / sub
        if not base.exists():
            continue
        for py in base.rglob("*.py"):
            try:
                if py.resolve() == canonical:
                    continue
            except OSError:
                continue
            try:
                txt = py.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(r"^__version__\s*=", txt, re.MULTILINE):
                violations.append(f"{py}: stray __version__ assignment")

    # (3) the identifier tokens that only exist to name a competing version SSOT.
    token_rx = re.compile(r"\b(system_version|core_version)\b")
    for sub in ("core/config", "mind_api"):
        base = root / sub
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if not f.is_file() or f.suffix not in (".py", ".yaml", ".yml"):
                continue
            try:
                txt = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(txt.splitlines(), 1):
                if token_rx.search(line):
                    violations.append(f"{f}:{i}: competing version identifier")

    return (len(violations) == 0), violations


# --------------------------------------------------------------------------- #
# RELEASES.json entry construction
# --------------------------------------------------------------------------- #
def build_entry(
    version: str,
    previous_version: Optional[str],
    date: str,
    breaking: bool,
    cross_world: bool,
    summary: str,
    upgrade_recipe: Optional[str],
    rollback_recipe: Optional[str],
    min_source: Optional[str],
) -> dict:
    """Build a single RELEASES.json entry dict (schema A.2)."""
    return {
        "version": version,
        "previous_version": previous_version,
        "date": date,
        "breaking": bool(breaking),
        "cross_world": bool(cross_world),
        "summary": summary,
        "upgrade_recipe": upgrade_recipe or None,
        "rollback_recipe": rollback_recipe or None,
        "min_source": min_source or None,
    }


def serialize_releases(releases: List[dict]) -> str:
    """Canonical serialization: indent=2, trailing newline, unicode preserved."""
    return json.dumps(releases, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- #
# Wave 2 — cross-world update classification + promotion-chain order
# --------------------------------------------------------------------------- #
def releases_above(releases: List[dict], local_version: str) -> List[dict]:
    """Entries with version > local_version, sorted OLDEST-first (apply order).

    Raises ValueError on any non-semver version in an above-local entry — a
    malformed upstream feed must fail, never be silently skipped (M1).
    """
    above = []
    for e in releases:
        v = e.get("version")
        if not v:
            raise ValueError("upstream entry missing 'version'")
        if compare_version(v, local_version) > 0:  # parse_version raises on junk
            above.append(e)
    above.sort(key=lambda e: parse_version(e["version"]))
    return above


def classify_update(upstream_releases: List[dict], local_version: str) -> dict:
    """Classify what a downstream at local_version should do given the upstream feed.

    Chain-walk rule: if ANY release between local and latest is breaking, the
    ENTIRE update is breaking (no leapfrogging a breaking change). Raises
    ValueError on malformed input (parse-or-fail, M1).
    """
    above = releases_above(upstream_releases, local_version)
    breaking = any(bool(e.get("breaking")) for e in above)
    return {
        "has_updates": len(above) > 0,
        "breaking": breaking,
        "count": len(above),
        "versions": [e["version"] for e in above],
        "latest": above[-1]["version"] if above else None,
        # recipes for the breaking entries, in apply order (downstream runs in order)
        "upgrade_recipes": [e.get("upgrade_recipe") for e in above if e.get("breaking")],
    }


def promotion_allowed(chain: List[str], from_role: str, to_role: str) -> bool:
    """True iff from_role may promote DIRECTLY to to_role: exactly one step
    downstream in the chain. frontier->seed OK, seed->downstream OK,
    frontier->downstream NO (skip), downstream->* NO (CW4)."""
    if from_role not in chain or to_role not in chain:
        return False
    return chain.index(to_role) == chain.index(from_role) + 1


def upstream_role(chain: List[str], self_role: str) -> Optional[str]:
    """The role one step UP from self_role (the role this repo pulls updates from).
    None if self_role is the frontier (top of chain) or not in the chain."""
    if self_role not in chain:
        return None
    i = chain.index(self_role)
    return chain[i - 1] if i > 0 else None


# --------------------------------------------------------------------------- #
# CLI dispatch (called by release.sh; env-driven for guard-165 safety)
# --------------------------------------------------------------------------- #
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _cmd_validate() -> int:
    """Full release validation. Inputs via env. KEY=VALUE + exit 0 on success;
    ERROR lines + exit 1 on failure."""
    project_root = _env("PROJECT_ROOT")
    releases_path = _env("RELEASES_PATH")
    current = _env("CURRENT_VERSION")
    new = _env("NEW_VERSION")
    bump_kind = _env("BUMP_KIND")
    cross_world = _env("CROSS_WORLD") == "1"
    allow_nb_cw = _env("ALLOW_NB_CW") == "1"
    recipe_path = _env("RECIPE_PATH")  # may be empty

    errors: List[str] = []

    # --- semver: new > current ---
    try:
        if compare_version(new, current) <= 0:
            errors.append(f"new version {new} is not greater than current {current}")
    except ValueError as e:
        errors.append(f"version parse error: {e}")

    # --- RELEASES.json load + chain + duplicate ---
    try:
        releases = load_releases(releases_path)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1
    if check_duplicate(releases, new):
        errors.append(f"version {new} already present in RELEASES.json")
    nv = newest_version(releases) if releases else None
    if nv is not None and nv != current:
        errors.append(
            f"chain anchor mismatch: RELEASES.json newest is {nv} but "
            f"__version__ is {current} — run release.sh from a synced state"
        )
    chain_ok, chain_errs = validate_chain(releases)
    if not chain_ok:
        errors.extend(chain_errs)

    # --- breaking / cross_world (Q3) ---
    breaking, cw, cw_errs = compute_breaking_cross_world(bump_kind, cross_world, allow_nb_cw)
    errors.extend(cw_errs)

    # --- CW3 recipe invariant ---
    rollback_recipe = ""
    min_source = ""
    if breaking:
        if not recipe_path:
            errors.append("breaking release requires --recipe (CW3)")
        else:
            rb = rollback_path_for(recipe_path)
            ok, missing = validate_recipe_structure(recipe_path, rb, cw)
            if not ok:
                errors.extend(f"CW3: {m}" for m in missing)
            else:
                rollback_recipe = rb
                min_source = current
    else:
        if recipe_path:
            errors.append("non-breaking release must NOT provide --recipe")

    # --- CW1 version SSOT ---
    ssot_ok, ssot_viol = check_version_ssot(project_root)
    if not ssot_ok:
        errors.extend(f"CW1: {v}" for v in ssot_viol)

    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        return 1

    print(f"BREAKING={1 if breaking else 0}")
    print(f"CROSS_WORLD={1 if cw else 0}")
    print(f"UPGRADE_RECIPE={recipe_path}")
    print(f"ROLLBACK_RECIPE={rollback_recipe}")
    print(f"MIN_SOURCE={min_source}")
    return 0


def _cmd_build_prepended() -> int:
    """Prepend a new entry to RELEASES.json and print the new array."""
    releases_path = _env("RELEASES_PATH")
    try:
        releases = load_releases(releases_path)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    entry = build_entry(
        version=_env("NEW_VERSION"),
        previous_version=_env("CURRENT_VERSION") or None,
        date=_env("DATE"),
        breaking=_env("BREAKING") == "1",
        cross_world=_env("CROSS_WORLD") == "1",
        summary=_env("SUMMARY"),
        upgrade_recipe=_env("UPGRADE_RECIPE") or None,
        rollback_recipe=_env("ROLLBACK_RECIPE") or None,
        min_source=_env("MIN_SOURCE") or None,
    )
    sys.stdout.write(serialize_releases([entry] + releases))
    return 0


def _cmd_classify_chain() -> int:
    """Classify a downstream update. env: UPSTREAM_RELEASES_PATH, LOCAL_VERSION.
    KEY=VALUE + exit 0 on success; exit 2 (FAIL-CLOSED) on malformed/unreadable
    upstream feed — the caller (check-upstream.sh) must treat 2 as a potential
    breaking change, never as 'no updates' (M1 / CW5)."""
    path = _env("UPSTREAM_RELEASES_PATH")
    local = _env("LOCAL_VERSION")
    try:
        upstream = load_releases(path)
        if not upstream:
            print("ERROR: upstream RELEASES.json empty/unreadable", file=sys.stderr)
            return 2
        c = classify_update(upstream, local)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"HAS_UPDATES={1 if c['has_updates'] else 0}")
    print(f"BREAKING={1 if c['breaking'] else 0}")
    print(f"COUNT={c['count']}")
    print(f"LATEST={c['latest'] or ''}")
    print(f"UPSTREAM_NEWEST={newest_version(upstream) or ''}")
    print(f"VERSIONS={','.join(c['versions'])}")
    print(f"UPGRADE_RECIPES={','.join(r for r in c['upgrade_recipes'] if r)}")
    return 0


def main(argv: List[str]) -> int:
    if not argv:
        print("usage: _release_lib.py {bump|validate|compare|seed-latest|build-prepended|classify-chain|check-promotion-order} ...", file=sys.stderr)
        return 2
    cmd = argv[0]
    try:
        if cmd == "bump":
            print(bump_version(argv[1], argv[2]))
            return 0
        if cmd == "compare":
            print(compare_version(argv[1], argv[2]))
            return 0
        if cmd == "seed-latest":
            releases = load_releases(argv[1])  # raises on malformed (M1)
            nv = newest_version(releases)
            if nv is None:
                print("ERROR: seed RELEASES.json has no entries", file=sys.stderr)
                return 1
            print(nv)
            return 0
        if cmd == "validate":
            return _cmd_validate()
        if cmd == "build-prepended":
            return _cmd_build_prepended()
        if cmd == "classify-chain":
            return _cmd_classify_chain()
        if cmd == "check-promotion-order":
            # argv: chain (comma-sep), from_role, to_role -> "OK" exit 0 / "DENY" exit 1
            chain = [r.strip() for r in argv[1].split(",") if r.strip()]
            if promotion_allowed(chain, argv[2], argv[3]):
                print("OK")
                return 0
            print(f"DENY: {argv[2]} -> {argv[3]} is not a single downstream step in {chain}")
            return 1
    except (ValueError, IndexError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
