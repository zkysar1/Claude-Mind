"""Activation pins for the three header-shape-activated capability-routing loaders.

WHY THIS TEST READS THE LIVE WORLD (and a fixture-based test cannot replace it).

Three loaders across two scripts read the SAME human-edited file
(`world/conventions/capability-routing.md`) and decide what they load by
`stripped.lower().startswith("## <prefix>")` on its prose section headers:

  1. capability.py `_load_capability_routing`  "## agent-provisionable"
  2. capability.py `_load_human_only`          "## human-only"
  3. audit-user-to-agent.py `_parse_standing_grants`
                                               "## standing user grants"

On 2026-08-01 loader 2 went from inert to ARMED because that section header was
renamed. No code changed. No test moved. Seven assertions across four files went
red and the capability gate began failing OPEN (g-115-4408; class rb-6216,
rail guard-2233).

That is the whole reason these assertions run against the LIVE file. The defect
lived in the INPUT, not the code, so every fixture-based test in the suite stayed
green throughout — a synthetic fixture pins the parser, and the parser was never
wrong. Only a live read can observe the drift.

WHAT IS PINNED, AND WHY EACH PIN DIFFERS

The three loaders all fail OPEN when their section goes empty, but they no longer
share a live consequence, so they do not share a pin:

  * loader 1 empty -> `keyword_block` False -> `would_block` False -> the gate
    stops refusing `participants:[user]` routing. Agent-doable work routes to a
    human unchallenged. LIVE HARM -> pinned by row count.
  * loader 3 empty -> the lane-P reclaim auditor sees zero standing grants and
    can never reclaim anything. Per guard-1802 / rb-5650 a zero-result run and a
    genuinely clean queue produce IDENTICAL output, so it would report clean
    forever. LIVE HARM -> pinned by row count.
  * loader 2 empty -> the veto cannot fire. Since g-115-4408 the veto is inert by
    default ANYWAY, so an empty load costs nothing. NO live harm -> pinning its
    row count would pin a fact the system no longer depends on. What IS pinned is
    the activation VERDICT itself: the switch that replaced the prose header.

The rename control below is what gives the two row-count pins teeth. An
"assert non-empty" that has never been shown capable of returning empty is not
evidence (guard-1665 / guard-2188): it would pass just as happily against a
loader that could never return zero.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from gates import capability as cap  # noqa: E402
import _paths  # noqa: E402

AUDIT_SCRIPT = SCRIPTS / "audit-user-to-agent.py"


def _import_audit():
    spec = importlib.util.spec_from_file_location("audit_user_to_agent", AUDIT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_user_to_agent"] = mod
    spec.loader.exec_module(mod)
    return mod


AUDIT = _import_audit()

CONV_REL = Path("conventions") / "capability-routing.md"


def _count_grants(parsed: dict) -> int:
    """Rows behind `_parse_standing_grants`' envelope.

    It returns a fixed 3-key dict `{by_scope, unkeyed, error}`, so `len()` on it
    is ALWAYS 3 — a "> 0" pin written against `len()` passes identically whether
    seven grants loaded or none did. That is guard-1802's own failure mode
    (a zero-result run and a clean queue producing identical output) reproduced
    inside the pin meant to prevent it; the rename control below caught it.
    """
    return sum(len(v) for v in parsed["by_scope"].values()) + len(parsed["unkeyed"])


# name, loader, header prefix the loader matches on, how to count its rows.
# The count is per-loader because the return SHAPES differ — see _count_grants.
LOADERS = [
    ("agent-provisionable", cap._load_capability_routing, "## agent-provisionable", len),
    ("human-only", cap._load_human_only, "## human-only", len),
    ("standing-user-grants", AUDIT._parse_standing_grants,
     "## standing user grants", _count_grants),
]

# Loaders whose EMPTY state is a live fail-open on this deployment today.
# See the module docstring for why human-only is deliberately absent here.
MUST_BE_ARMED = {"agent-provisionable", "standing-user-grants"}


def _live_world():
    # WORLD_DIR is None on a box with no external world configured — that is the
    # documented terminal fallback in _paths.py ("MIND_WORLD > WORLD_PATH > None";
    # the PROJECT_ROOT/world fallback was deliberately removed 2026-05-19). Check it
    # BEFORE Path(), because Path(None) raises TypeError, which would ERROR these
    # tests on exactly the unconfigured box this guard exists to skip on.
    if _paths.WORLD_DIR is None:
        pytest.skip("no external world configured on this box — nothing to pin")
    world = Path(_paths.WORLD_DIR)
    if not (world / CONV_REL).is_file():
        pytest.skip(f"no live {CONV_REL} under {world} — nothing to pin")
    return world


def _rename_headers(text: str, prefix: str) -> str:
    """Break `prefix`'s startswith match by prepending a word to that header.

    Generic on purpose: it names no section content, so this file stays free of
    domain terms, and one rename shape covers all three loaders.
    """
    out, hits = [], 0
    for line in text.splitlines():
        s = line.strip()
        if s.lower().startswith(prefix):
            out.append("## Renamed " + s[len("## "):])
            hits += 1
        else:
            out.append(line)
    assert hits > 0, f"control is vacuous: no live header matched {prefix!r}"
    return "\n".join(out) + "\n"


def _world_with(tmp_path: Path, text: str) -> Path:
    dest = tmp_path / CONV_REL
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return tmp_path


# --- the pins ---------------------------------------------------------------


@pytest.mark.parametrize("name,loader,prefix,count_fn", LOADERS)
def test_live_header_activation(name, loader, prefix, count_fn):
    """A loader that must be armed is armed; the rest is reported, not asserted."""
    world = _live_world()
    count = count_fn(loader(world))
    if name in MUST_BE_ARMED:
        assert count > 0, (
            f"loader {name!r} loaded 0 rows from the live {CONV_REL}. Its section "
            f"header no longer starts with {prefix!r} (or the section is gone). "
            f"This loader FAILS OPEN when empty — see the module docstring for "
            f"what that costs. Fix the header or the prefix; do not delete this pin."
        )
    else:
        # Reported for observability. Asserting it would pin a fact the system
        # stopped depending on when the veto became explicitly gated.
        print(f"[activation] {name}: {count} rows (not pinned — veto is flag-gated)")


@pytest.mark.parametrize("name,loader,prefix,count_fn", LOADERS)
def test_header_rename_disarms_loader(tmp_path, name, loader, prefix, count_fn):
    """POSITIVE CONTROL: each loader really does go to zero on a header rename.

    Without this, `assert count > 0` above is untested machinery — it would pass
    against a loader incapable of returning zero, and the pin would be decoration.
    It is not a formality: it caught `_parse_standing_grants`' fixed-size envelope
    (see _count_grants), where the first draft of that pin was exactly such a
    decoration.
    """
    world = _live_world()
    live_text = (world / CONV_REL).read_text(encoding="utf-8")

    # Both worlds are built from the SINGLE `live_text` snapshot above. An earlier
    # draft re-read the live file here to prove the copy was faithful; that is a
    # second read of a file partner agents edit, so a mid-test edit would fail the
    # control spuriously. It was also redundant: the copy is byte-identical to
    # live_text by construction, and `baseline > 0` already catches a _world_with
    # that wrote where the loader does not look — the only thing the re-read added.
    baseline = count_fn(loader(_world_with(tmp_path / "same", live_text)))
    assert baseline > 0, (
        f"{name}: the control starts from an ALREADY-empty load, so watching it "
        f"stay empty after a rename proves nothing about the pin's sensitivity"
    )

    renamed = _rename_headers(live_text, prefix)
    assert count_fn(loader(_world_with(tmp_path / "renamed", renamed))) == 0, (
        f"{name}: renaming its section header did NOT empty the loader. The pin "
        f"in test_live_header_activation cannot detect the drift it exists for."
    )


def test_all_three_loaders_read_one_file():
    """The class: one human-edited file silently arms three separate consumers."""
    world = _live_world()
    assert (world / CONV_REL).resolve().is_file()
    for name, loader, _, _ in LOADERS:
        assert loader(world) is not None, (
            f"{name} returned None rather than an empty container"
        )


# --- the human-only veto verdict (the switch that replaced the prose header) --


def test_human_only_veto_is_inert_by_default(monkeypatch):
    """b51c84dd9's shipped intent, now explicit instead of prose-derived.

    The commit that added the veto shipped it inert and recorded that arming it
    first needs predicate tuning, because naive arming was measured to over-veto.
    That tuning was never done, and a header rename armed it anyway.
    """
    monkeypatch.delenv("MIND_HUMAN_ONLY_VETO", raising=False)
    assert cap.HUMAN_ONLY_VETO_DEFAULT_ARMED is False
    assert cap._human_only_veto_armed() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "armed", "on", "yes", " Yes "])
def test_env_can_arm_the_veto(monkeypatch, raw):
    monkeypatch.setenv("MIND_HUMAN_ONLY_VETO", raw)
    assert cap._human_only_veto_armed() is True


@pytest.mark.parametrize("raw", ["0", "false", "inert", "off", "no"])
def test_env_can_state_inert_explicitly(monkeypatch, raw):
    monkeypatch.setenv("MIND_HUMAN_ONLY_VETO", raw)
    assert cap._human_only_veto_armed() is False


@pytest.mark.parametrize("raw", ["", "  ", "armd", "enabled", "2", "null"])
def test_unrecognised_env_value_falls_through_to_inert(monkeypatch, raw):
    """A typo must fail INERT, never silently arm a fail-open veto."""
    monkeypatch.setenv("MIND_HUMAN_ONLY_VETO", raw)
    assert cap._human_only_veto_armed() is False


def test_inert_veto_does_not_load_rows(monkeypatch, tmp_path):
    """While inert the rows are not read at all — the veto cannot fire at any cost.

    Pins the short-circuit, not just the flag: gating the flag but still calling
    the loader would leave `human_only_matches` populated and the conjunct wrong.
    """
    monkeypatch.delenv("MIND_HUMAN_ONLY_VETO", raising=False)
    calls = []
    monkeypatch.setattr(
        cap, "_load_human_only", lambda w: calls.append(w) or [{"row": "x"}]
    )
    # Deliberately hermetic: the short-circuit is a property of the flag, not of
    # any world's contents, so this asserts it without depending on live state.
    # (An earlier draft passed `Path(os.environ.get("MIND_WORLD", str(_paths.WORLD_DIR)))`,
    # which degrades to the literal path "None" on an unconfigured box — it happened
    # to still pass, but for a reason unrelated to what the test claims to check.)
    cap.evaluate(
        failure_reason="needs a human to click approve in the console",
        intended_participants="user",
        world_dir=tmp_path,
        agent_name="alpha",
    )
    assert calls == [], "the human-only rows were loaded while the veto was inert"
