""": sub-materiality interval proposals are suppressed, not emitted.

_propose_new_interval clamps the multiplier step to `original * cap_ratio`
via min(). Once a goal's current interval approaches that cap, the clamp
truncates the step to a RESIDUE instead of suppressing it, and the old
`return proposed if proposed > interval_h else None` admitted any positive
residue. Those rows cannot alter behaviour on a multi-week cadence yet still
consume a batch-review slot and ask an agent for a judgement whose two
outcomes are indistinguishable.

The two cases below are the REAL measured goals (2026-08-26, world queue),
not synthetic fixtures -- their exact interval/original pairs reproduce the
residues that motivated the fix:
    g-248-07  1093h  orig 364.500  -> min(1639.5, 1093.500) = +0.5000h (+0.0457%)
    g-115-03  1230h  orig 410.062  -> min(1845.0, 1230.186) = +0.1860h (+0.0151%)

Load-bearing regressions pinned here:
  - the two measured residues return None (the fix)
  - POSITIVE CONTROL: a partial cap-bind at +28.57% still proposes. This is
    the half that matters -- a suppression fix that also eats legitimate
    partial steps would pass a fix-only test. The delta distribution is
    bimodal (empty band 0.05% -> 28.57%), so this is the nearest real
    neighbour on the admit side, not an arbitrary large number.
  - a clean uncapped +50% step still proposes
  - min_materiality_ratio=0 (feature disabled) still suppresses a ZERO-delta
    proposal via the explicit proposed<=interval_h check -- the ratio test
    alone would admit it, since 0 < 0 is False
  - calibration_exempt precedence is unchanged

Run: STORAGE_BACKEND=local py -3 -m pytest core/scripts/tests/test_cargo_cult_materiality_floor.py -v
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
DETECTOR_PY = CORE_SCRIPTS / "cargo-cult-detector.py"

CFG = {"multiplier": 1.5, "cap_ratio": 3.0}


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "cargo_cult_detector_materiality", DETECTOR_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _propose(interval_h, original_h, cfg=None, **extra):
    # Load INSIDE the call, as all three sibling cargo-cult tests do. The
    # detector's import is not side-effect free -- sys.path.insert,
    # sys.stdout.reconfigure, and `import _rt` (the daemon client) all run at
    # import -- so a module-level MOD = _load_module() would hoist them to
    # pytest COLLECTION time for the entire suite. Caught by fresh-eyes review
    # of this file, 2026-08-26.
    mod = _load_module()
    goal = {"recurring": True, "interval_hours": interval_h,
            "original_interval_hours": original_h}
    goal.update(extra)
    return mod._propose_new_interval(goal, CFG if cfg is None else cfg)


def test_g_248_07_residue_suppressed():
    """+0.5h on a 1093h cadence (+0.0457%) is inert -- suppress."""
    assert _propose(1093, 364.5) is None


def test_g_115_03_residue_suppressed():
    """+0.186h on a 1230h cadence (+0.0151%) is inert -- suppress."""
    assert _propose(1230, 410.062) is None


def test_partial_cap_bind_still_proposes():
    """POSITIVE CONTROL:  (168h, orig 72h) binds the cap at 216h.

    That is +28.57% -- clamped, but materially so. The nearest real neighbour
    above the empty band. If this ever returns None the fix has over-reached.
    """
    assert _propose(168, 72.0) == 216.0


def test_clean_uncapped_step_still_proposes():
    """ (16.2h, orig 16.2h): cap 48.6h does not bind; full +50%."""
    proposed = _propose(16.2, 16.2)
    assert proposed is not None
    assert abs(proposed - 24.3) < 1e-9


def test_zero_delta_suppressed_even_with_feature_disabled():
    """min_materiality_ratio=0 must NOT re-admit a zero-delta proposal.

    interval already == original*cap_ratio, so proposed == interval and the
    ratio test (0 < 0) is False. The explicit proposed<=interval_h check is
    what holds here; deleting it as redundant would regress this case.
    """
    cfg = {"multiplier": 1.5, "cap_ratio": 3.0, "min_materiality_ratio": 0}
    assert _propose(300.0, 100.0, cfg=cfg) is None


def test_calibration_exempt_precedence_unchanged():
    """An exempt review-ritual goal is suppressed before any arithmetic."""
    assert _propose(16.2, 16.2, calibration_exempt=True) is None


def test_config_key_name_is_wired_and_falsifiable():
    """The aspirations.yaml key must actually reach the function.

    FRESH-EYES FINDING (2026-08-26): `cargo_cult.min_materiality_ratio` is 0.01
    in config and the code's fallback is ALSO 0.01, so a misspelled key changes
    nothing. Positive control run at review time: a `min_matereality_ratio` typo
    returned None/216.0 -- byte-identical to the correct key on both a
    suppressed and a proposing goal, with all six other tests still green. This
    test is the only thing that can catch that.

    Asserts (a) the real config carries the exact key under cargo_cult, and
    (b) the function's behaviour genuinely tracks that key's VALUE -- a large
    ratio must suppress a step the default admits.
    """
    import yaml
    cfg_all = yaml.safe_load((CORE_SCRIPTS.parent / "config" / "aspirations.yaml").read_text())
    block = (cfg_all or {}).get("cargo_cult") or {}
    assert "min_materiality_ratio" in block, (
        "cargo_cult.min_materiality_ratio missing from aspirations.yaml -- the "
        "code silently falls back to 0.01, so the knob would be dead")
    assert isinstance(block["min_materiality_ratio"], (int, float))

    # (b) behaviour tracks the VALUE, not just the key's presence.
    # : 168h -> 216h is +28.57%, admitted at the 0.01 default.
    admits = {"multiplier": 1.5, "cap_ratio": 3.0, "min_materiality_ratio": 0.01}
    blocks = {"multiplier": 1.5, "cap_ratio": 3.0, "min_materiality_ratio": 0.50}
    assert _propose(168, 72.0, cfg=admits) == 216.0
    assert _propose(168, 72.0, cfg=blocks) is None
