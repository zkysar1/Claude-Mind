""": pin the daemon-code boundary to the daemon's REAL import surface.

core/scripts/mind-api-code-changed.sh is the SINGLE SOURCE OF TRUTH for "did the
daemon's code change?" — three callers (post-commit, post-merge, _runtime.sh
rt_check_staleness) restart the daemon only when it says yes. Its guarantee is
"never serve stale daemon code" (rb-711, rb-936, guard-559).

Its pathspec enumerates the NON-underscore core/scripts modules the daemon loads.
That list is hand-maintained, and it has silently drifted TWICE:

  * g-115-2190 — owncloud_backend.py was absent while being transitively loaded
    (storage_backend.py instantiates OwnCloudBackend.from_env()). A commit
    touching only it diffed CLEAN -> no restart -> daemon served a stale storage
    backend.
  * g-115-2195 — FIVE more were absent: coordination_merge, owncloud_sync,
    retrieve, tree_idf, trigger_firings. `retrieve` is the worst: the retrieval
    ENGINE, imported EAGERLY by mind_api/src/endpoints/retrieve.py:68, so every
    commit to it since the daemon-only cutover skipped the restart.

The root cause is not any one missing name — it is that a hand-maintained list
drifts silently. This test recomputes the surface from source and FAILS when the
pathspec falls behind, converting silent staleness into a loud test failure.

METHOD — AST transitive closure, not a grep and not a runtime snapshot:

  * A GREP of top-of-file imports cannot see lazy in-function imports, and 3 of
    the 5 g-115-2195 gaps were exactly that. Grepping is how the gap survived.
  * A RUNTIME sys.modules snapshot only shows what happened to have been
    EXERCISED. A module lazily imported in a rarely-hit branch is absent from the
    snapshot yet is still a stale-code hole the moment that branch runs.
  * AST closure walks every import at ANY nesting depth (module level, inside
    functions, inside try/except, inside class bodies) and transitively closes.
    It is a SUPERSET of a runtime snapshot, and that over-approximation errs
    toward MORE files in the pathspec -> toward RESTART, which is the predicate's
    stated correctness direction.

KNOWN LIMITATION: a purely dynamic import (importlib with a computed name) is
invisible to AST. None exist in the daemon surface today. If one is added, this
test will not catch it — prefer a static import in daemon code.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "core" / "scripts"
SRC = ROOT / "mind_api" / "src"
PREDICATE = SCRIPTS / "mind-api-code-changed.sh"

# experience.py is in the pathspec but NOT in the daemon's surface:
# mind_api/src/store_registry.py:327 — "Do NOT `from experience import` —
# daemon-import-unsafe". It is OVER-INCLUSIVE (forces a restart the daemon does
# not need). Left in deliberately: removing a pathspec entry moves AWAY from the
# fail-toward-restart guarantee and deserves its own goal. Allowlisted here so
# the over-inclusion assertion below still catches any NEW one.
KNOWN_OVER_INCLUSIONS = {"experience"}


def _imported_names(path: Path) -> set[str]:
    """Every top-level module name imported anywhere in this file, ANY AST depth."""
    out: set[str] = set()
    # D2 (): FAIL LOUD on parse/read error — do NOT swallow into an
    # empty set. An empty return UNDER-approximates the surface (fewer modules ->
    # smaller pathspec passes -> fewer restarts -> STALE daemon code), inverting
    # the predicate's own fail-toward-restart contract (mind-api-code-changed.sh
    # L100-104). Latent today (0 files fail to parse) but a mid-edit SyntaxError
    # or a Windows file lock would silently shrink the surface and green the suite.
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError) as exc:
        raise RuntimeError(
            f"daemon-import-surface probe could not parse {path}: {exc}. A file in "
            "the daemon's import surface is unreadable/unparseable; returning an "
            "empty set would hide a stale-code hole. Fix the file, do not ignore."
        ) from exc
    for node in ast.walk(tree):  # ast.walk => any depth, incl. inside functions
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import -> inside mind_api, not core/scripts
                continue
            if node.module:
                out.add(node.module.split(".")[0])
    return out


def daemon_import_surface() -> set[str]:
    """core/scripts modules the daemon loads (transitive, incl. lazy).

    Stems for top-level modules (`storage_backend`); `<pkg>/<stem>` path-keys for
    PACKAGE members (`gates/goal_duplication`). The package form is D1(a),
    g-115-2200: a `from gates.X import Y` yields only the top-level name `gates`
    (split on '.'), and `gates.py` is not a FILE, so the pre-fix closure dropped
    every gate evaluator — the gates half of the boundary pinned NOTHING.
    """
    closure: set[str] = set()
    seen_pkgs: set[str] = set()
    frontier: set[str] = set()
    for seed in SRC.rglob("*.py"):
        frontier |= _imported_names(seed)
    while frontier:
        name = frontier.pop()
        if name in closure:
            continue
        candidate = SCRIPTS / f"{name}.py"
        if candidate.exists():
            closure.add(name)
            frontier |= _imported_names(candidate) - closure
            continue
        pkg_dir = SCRIPTS / name
        if pkg_dir.is_dir() and name not in seen_pkgs:
            # PACKAGE import (e.g. `from gates.X import Y`). Enumerate every
            # member: each is a daemon-loaded module that must be pinned, AND its
            # OWN imports must be transitively closed (a gate importing a
            # NON-underscore core/scripts module must demand that module of the
            # pathspec too).
            seen_pkgs.add(name)
            for member in sorted(pkg_dir.glob("*.py")):
                if member.name == "__init__.py":
                    continue
                mod_key = f"{name}/{member.stem}"
                closure.add(mod_key)
                frontier |= _imported_names(member) - closure
        # else: stdlib / third-party / not a core/scripts module
    return closure


def _pathspec_tokens() -> list[str]:
    """The DAEMON_PATHSPEC entries, read OUT of the shell script — never restated.

    A second copy of the list in this file would just be a new thing to drift.
    The script is the single source of truth; this test reads it.

    Reads the `DAEMON_PATHSPEC=( ... )` ARRAY, not the `git diff` line the array is
    expanded into. g-115-3587 hoisted the list into that array and left the diff
    invocation holding only `"${DAEMON_PATHSPEC[@]}"`, so the previous regex over the
    diff block matched a 45-byte span containing no paths at all and every derived set
    came back EMPTY — every daemon module then read as uncovered and this file reported
    all 77 as missing (g-115-8287). Parse the declaration, not a use site.
    """
    text = PREDICATE.read_text(encoding="utf-8")
    block = re.search(r"^DAEMON_PATHSPEC=\((.*?)^\)", text, re.S | re.M)
    assert block, (
        f"could not locate the DAEMON_PATHSPEC array in {PREDICATE.name}. The pathspec "
        f"declaration moved again — fix THIS parser; do not restate the list here."
    )
    tokens: list[str] = []
    for line in block.group(1).splitlines():
        line = line.split("#", 1)[0].strip().strip("\\").strip()
        if line:
            # The single quotes on `_*.py` are load-bearing IN THE SHELL (they stop
            # glob expansion at array-construction time); git receives the bare
            # pattern, so strip them to get the pathspec as git actually sees it.
            tokens.append(line.strip("'\""))
    return tokens


def pathspec_entries() -> set[str]:
    """FILE entries of the form `core/scripts/<name>.py`, as bare names."""
    out: set[str] = set()
    for tok in _pathspec_tokens():
        m = re.fullmatch(r"core/scripts/([A-Za-z0-9_*]+)\.py", tok)
        if m:
            out.add(m.group(1))
    return out


def pathspec_dir_entries() -> set[str]:
    """DIRECTORY pathspec entries under core/scripts (e.g. `core/scripts/gates`).

    Members of such a directory are covered by the directory entry, not by an
    individual `.py` entry — so a gate module is pinned IFF `gates` appears here.
    Derived from the SAME tokens as pathspec_entries(); never restated.
    """
    dirs: set[str] = set()
    for tok in _pathspec_tokens():
        if not tok.startswith("core/scripts/"):
            continue
        rest = tok[len("core/scripts/"):]
        # a directory entry is a bare name: no `.py`, no glob, no nested path
        if rest and "." not in rest and "*" not in rest and "/" not in rest:
            dirs.add(rest)
    return dirs


def _covered(mod: str, entries: set[str], dir_entries: set[str] | None = None) -> bool:
    if mod.startswith("_") and "_*" in entries:
        return True  # the 'core/scripts/_*.py' glob
    if "/" in mod:
        # a package member like 'gates/goal_duplication' — covered IFF its package
        # DIRECTORY is a pathspec entry (D1(b), : MEMBERSHIP, not a
        # filesystem check. The pre-fix `(SCRIPTS/'gates'/f'{mod}.py').exists()`
        # returned True even after the dir was deleted from the pathspec, so the
        # mutation "delete core/scripts/gates" still passed 9/9).
        if dir_entries is None:
            dir_entries = pathspec_dir_entries()
        return mod.split("/", 1)[0] in dir_entries
    return mod in entries


def test_probe_is_not_vacuous():
    """POSITIVE CONTROL. An empty/degenerate surface would make every other
    assertion below pass vacuously — a broken probe is indistinguishable from a
    clean result unless you check it against a case you KNOW is positive."""
    surface = daemon_import_surface()
    # known-loaded, and the two historical gaps — the probe MUST see all three
    assert "storage_backend" in surface, "probe failed to find a known-loaded module"
    assert "owncloud_backend" in surface, "probe failed to find the g-115-2190 gap"
    assert "coordination_merge" in surface, "probe failed to find the g-115-2195 gap"
    # NEGATIVE control: it must not simply be returning every file in core/scripts
    all_py = len(list(SCRIPTS.glob("*.py")))
    assert len(surface) < all_py / 2, (
        f"surface={len(surface)} of {all_py} core/scripts modules — the probe is "
        f"over-matching, not computing a real closure"
    )


def test_pathspec_parse_is_not_vacuous():
    """POSITIVE CONTROL on the OTHER derived set (guard-1638).

    `test_probe_is_not_vacuous` above floors the AST surface. Nothing floored the
    PATHSPEC side, and that asymmetry is exactly what made g-115-8287 silent in the
    wrong DIRECTION: the parse returned an EMPTY set, so every daemon module read as
    uncovered and `test_every_loaded_module_is_in_the_pathspec` failed telling the
    reader to edit the SHELL SCRIPT — when the broken code was the parser in THIS
    file. A floor that runs independently of the list assertion names the real cause.

    Both derived sets now have a floor. Keep it that way.
    """
    tokens = _pathspec_tokens()
    assert len(tokens) >= 10, (
        f"DAEMON_PATHSPEC parsed to {len(tokens)} token(s). The parser in THIS file "
        f"has stopped matching {PREDICATE.name} — do NOT 'fix' the pathspec, and do "
        f"NOT restate the list here. Fix the parser."
    )
    # Sentinels spanning all three token SHAPES the parse must preserve: a plain
    # path, the quoted glob (its shell quotes must be stripped, since git receives
    # the bare pattern), and a bare directory.
    assert "mind_api/src" in tokens, "the daemon's own source root is absent"
    assert "_*" in pathspec_entries(), "the `core/scripts/_*.py` glob did not survive"
    assert "gates" in pathspec_dir_entries(), "the `core/scripts/gates` dir did not survive"
    # NEGATIVE control: the block regex must stop at the array's closing paren
    # rather than swallowing the rest of the script.
    assert len(tokens) < 60, (
        f"parsed {len(tokens)} tokens — the block regex is over-matching past the "
        f"array's closing paren and is picking up ordinary script lines"
    )


def test_every_loaded_module_is_in_the_pathspec():
    """The load-bearing assertion: no daemon-loaded module may be unmatched.

    An unmatched module means a commit touching ONLY that file diffs clean, the
    post-commit hook skips the restart, and the daemon keeps serving the OLD code.
    """
    entries = pathspec_entries()
    dir_entries = pathspec_dir_entries()
    missing = sorted(m for m in daemon_import_surface() if not _covered(m, entries, dir_entries))
    assert not missing, (
        "Daemon-loaded core/scripts modules are MISSING from the pathspec in "
        f"{PREDICATE.name}: {missing}\n"
        "A commit touching only one of these diffs CLEAN, so post-commit skips the "
        "daemon restart and the daemon serves STALE code (rb-711/rb-936/guard-559).\n"
        "Fix: add `core/scripts/<name>.py` to the pathspec AND to the "
        "import-surface list in the script's header comment (they must stay in sync)."
    )


def test_no_new_over_inclusions():
    """The other direction: a pathspec entry the daemon does NOT load forces a
    restart it does not need ('pure churn' — the thing the predicate exists to
    avoid). Known ones are allowlisted; a NEW one fails here."""
    entries = {e for e in pathspec_entries() if not e.startswith("_") and "*" not in e}
    surface = daemon_import_surface()
    over = sorted(e for e in entries - surface if e not in KNOWN_OVER_INCLUSIONS)
    assert not over, (
        f"Pathspec entries the daemon does not actually load: {over}. Each forces a "
        f"restart on every commit that touches it, for no benefit. Either remove it, "
        f"or add it to KNOWN_OVER_INCLUSIONS with the evidence that it is deliberate."
    )


@pytest.mark.parametrize("mod", ["coordination_merge", "owncloud_sync", "retrieve",
                                 "tree_idf", "trigger_firings", "owncloud_backend"])
def test_historical_gaps_stay_covered(mod: str):
    """Regression pins for the six modules that were actually found missing
    (g-115-2190: owncloud_backend; g-115-2195: the other five). Named explicitly so
    a future pathspec edit that drops one fails loudly and by name."""
    assert _covered(mod, pathspec_entries()), (
        f"{mod} was a real staleness hole and has been removed from the pathspec"
    )


def test_gate_evaluators_are_in_the_surface():
    """D1(a) : every core/scripts/gates/*.py evaluator must appear in the
    computed surface. The daemon imports all 16 EAGERLY (aspirations_write.py). Pre-fix,
    daemon_import_surface() resolved `from gates.X import Y` to a `gates.py` FILE that
    does not exist and dropped every one — so the gates half of the boundary pinned
    NOTHING and a commit touching only a gate evaluator served stale daemon code."""
    surface = daemon_import_surface()
    gate_members = {f"gates/{p.stem}" for p in (SCRIPTS / "gates").glob("*.py")
                    if p.name != "__init__.py"}
    assert gate_members, "no gate evaluators found on disk — test fixture is wrong"
    missing = sorted(gate_members - surface)
    assert not missing, (
        f"gate evaluators absent from daemon_import_surface(): {missing}. The daemon "
        "imports these eagerly — each is a stale-code hole if the pathspec omits gates."
    )


def test_gates_pin_is_membership_not_filesystem():
    """D1(b)  mutation regression: the gates dir entry must be enforced by
    pathspec MEMBERSHIP, not filesystem existence. Simulate the pathspec WITHOUT
    `core/scripts/gates` and assert coverage goes RED for every gate module. Pre-fix,
    _covered consulted the filesystem, so deleting the entry still passed 9/9 — the
    exact false-pass this goal was filed to close."""
    surface = daemon_import_surface()
    gate_mods = sorted(m for m in surface if m.startswith("gates/"))
    assert gate_mods, "no gate modules in surface — cannot exercise the pin (see D1a)"
    entries = pathspec_entries()
    without_gates = pathspec_dir_entries() - {"gates"}
    uncovered = [m for m in gate_mods if not _covered(m, entries, without_gates)]
    assert uncovered == gate_mods, (
        "removing `core/scripts/gates` from the pathspec must leave EVERY gate module "
        f"uncovered, but these stayed covered: {sorted(set(gate_mods) - set(uncovered))}. "
        "_covered is consulting the filesystem, not pathspec membership (bug b)."
    )


def test_imported_names_fails_loud_on_parse_error(tmp_path):
    """D2 : a parse/read error must RAISE, not silently return {}. An empty
    set UNDER-approximates the surface (fewer modules -> smaller pathspec passes ->
    stale daemon code), inverting the predicate's fail-toward-restart contract."""
    bad = tmp_path / "broken.py"
    bad.write_text("def (:  this is not valid python\n", encoding="utf-8")
    with pytest.raises((SyntaxError, RuntimeError)):
        _imported_names(bad)
