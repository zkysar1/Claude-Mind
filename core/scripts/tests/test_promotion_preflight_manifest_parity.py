# Parity between the promotion drift gate's CHECK set and the seed manifest's
# COPY set (). Runnable two ways:
#   py -3 core/scripts/tests/test_promotion_preflight_manifest_parity.py
#   py -3 -m pytest core/scripts/tests/test_promotion_preflight_manifest_parity.py -q
#
# WHY THIS FILE EXISTS. promotion-preflight.py answers "does the TARGET lead on
# anything?" over FRAMEWORK_PATHS, and promote-to-upstream.sh then copies
# whatever core/config/seed-manifest.yaml `include:` names. Those are two lists
# maintained in two files, and nothing connected them: mind_api/ was COPIED and
# never CHECKED, so a downstream that had evolved its own daemon code got it
# overwritten while the gate exited 0 = "safe to promote". The defect IS the
# disagreement between the two sets, so the durable fix is this assertion, not
# the one-line list edit that accompanies it.
#
# THE EXPECTED SET IS DERIVED, NEVER RESTATED. Every expectation below is read
# out of seed-manifest.yaml and promotion-preflight.py at run time. A hardcoded
# copy of either list here would be the same drift defect moved one layer out --
# it would keep passing while the real lists diverged underneath it.
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "core" / "scripts" / "promotion-preflight.py"
MANIFEST = REPO / "core" / "config" / "seed-manifest.yaml"


def _load_gate():
    """Import promotion-preflight.py by path (its name is not importable)."""
    spec = importlib.util.spec_from_file_location("promotion_preflight", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest_include():
    import yaml
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    entries = data.get("include") or []
    assert entries, (
        "seed-manifest.yaml `include:` parsed as EMPTY. That is a parse failure, "
        "not an empty copy set -- an empty include would make every assertion "
        "below vacuously true (guard-2421: positive-control the zero)."
    )
    return [str(e["path"]) for e in entries]


def _norm(p: str) -> str:
    return p.replace("\\", "/").rstrip("/")


def _covered_by(entry: str, checked: list[str]) -> bool:
    """Is manifest `entry` drift-checked by some FRAMEWORK_PATHS member?

    Coverage is one-directional ON PURPOSE. A manifest entry is covered when it
    IS a checked path or lies UNDER one. It is NOT covered when it merely
    CONTAINS a checked path: `core/` is copied whole while only core/config and
    core/scripts are checked, so treating "contains" as coverage would report
    the largest real gap in the tree as closed.
    """
    e = _norm(entry)
    for c in (_norm(x) for x in checked):
        if e == c or e.startswith(c + "/"):
            return True
    return False


def test_every_copied_path_is_checked_or_explicitly_excused():
    gate = _load_gate()
    checked = list(gate.FRAMEWORK_PATHS)
    excused = {_norm(k) for k in gate.MANIFEST_NOT_DRIFT_CHECKED}
    gaps = [
        e for e in _manifest_include()
        if not _covered_by(e, checked) and _norm(e) not in excused
    ]
    assert not gaps, (
        "seed-manifest.yaml COPIES these paths, and promotion-preflight.py "
        f"neither CHECKS them for drift nor excuses them: {sorted(gaps)}. "
        "A promote will overwrite them while the gate reports 'safe to "
        "promote'. Either add the path to FRAMEWORK_PATHS or add it to "
        "MANIFEST_NOT_DRIFT_CHECKED with the reason it is safe to clobber."
    )


def test_excuse_list_has_no_stale_entries():
    """An excuse for a path the manifest no longer copies is dead weight that
    makes the gap list look shorter than it is."""
    gate = _load_gate()
    manifest = {_norm(p) for p in _manifest_include()}
    stale = sorted(
        k for k in gate.MANIFEST_NOT_DRIFT_CHECKED if _norm(k) not in manifest
    )
    assert not stale, (
        f"MANIFEST_NOT_DRIFT_CHECKED excuses paths the seed manifest no longer "
        f"copies: {stale}. Remove them so the excuse list stays a true "
        "statement about the copy set."
    )


def test_every_excuse_carries_a_reason():
    gate = _load_gate()
    empty = sorted(
        k for k, v in gate.MANIFEST_NOT_DRIFT_CHECKED.items()
        if not (isinstance(v, str) and v.strip())
    )
    assert not empty, (
        f"These excuses carry no reason: {empty}. An unexplained excuse is "
        "indistinguishable from an oversight to the next reader."
    )


def test_mind_api_runtime_tree_is_drift_checked():
    """The  pin, stated against the RUNTIME path specifically.

    The wrappers are daemon-only, so mind_api/src is where behaviour actually
    lives. A generic parity assertion would pass if someone excused mind_api
    instead of checking it, which is why this names it directly.
    """
    gate = _load_gate()
    checked = list(gate.FRAMEWORK_PATHS)
    for required in ("mind_api/src", "mind_api/tests"):
        assert _covered_by(required, checked), (
            f"{required} is COPIED by the seed manifest but not in "
            "FRAMEWORK_PATHS -- the drift gate is blind to the live daemon tree."
        )
        assert _norm(required) not in {
            _norm(k) for k in gate.MANIFEST_NOT_DRIFT_CHECKED
        }, f"{required} must be CHECKED, not excused."


def test_bench_is_excluded_by_construction_not_by_a_second_rule():
    """mind_api/bench is deliberately absent from the seed (Q27 lean).

    FRAMEWORK_PATHS names the manifest's two entries rather than a bare
    "mind_api", so bench stays out because the MANIFEST leaves it out. If a
    future edit widens this to bare "mind_api", the gate would start reporting
    drift on a tree the promote never copies -- flagging files a promote cannot
    clobber.
    """
    gate = _load_gate()
    assert not _covered_by("mind_api/bench", list(gate.FRAMEWORK_PATHS)), (
        "mind_api/bench is drift-checked but is NOT in the seed manifest's copy "
        "set, so the gate would report drift a promote can never cause."
    )
    assert "mind_api/bench" not in {
        _norm(p) for p in _manifest_include()
    }, ("mind_api/bench is now in the seed manifest -- this test encoded the "
        "opposite. Re-decide coverage rather than deleting this assertion.")


def _main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  [PASS] {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  [FAIL] {name}: {exc}")
    total = sum(1 for n in globals() if n.startswith("test_"))
    print(f"\nmanifest-parity: {total - failures}/{total} passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(_main())
