"""test_experience_content_path_two_era.py —  (asp-115).

`validate_record`'s content_path existence check resolved ONE path era. The
experience corpus is deliberately TWO-era: records written before the
Phase-2.5.D relocation carry the agent name as the FIRST segment with no
AGENTS_PARENT_DIR parent, and CLAUDE.md's experience-orphan-ratchet row records
845 of 3621 live rows still in that shape — which is why that ratchet joins on
BASENAME rather than on the path.

Because `cmd_update_field` re-runs FULL record validation after every field
write (guard-330 / rb-364 — deliberately, so update-field is not a back-door
around add-time validation), a legacy-shape record took no field update EVER
again through the fenced path. Measured 2026-08-10 on cc-05: 374 of bravo's
1266 records (29.5%).

WHAT THESE TESTS ARE WEIGHTED TOWARD. The fix RELAXES an existence check, and
guard-2860 is explicit that an ownership/existence predicate must never be
relaxed into a pattern. So the load-bearing tests here are the ones proving the
check still REFUSES: a genuinely dangling pointer, a wrong-agent path, and a
basename that exists somewhere else entirely. A fix that made those pass would
be worse than the defect, because it would let a dangling pointer validate
silently and the orphan ratchet would then have nothing to catch.

Run:
  STORAGE_BACKEND=local python -m pytest \
      core/scripts/tests/test_experience_content_path_two_era.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "experience_mod", CORE_SCRIPTS / "experience.py")
experience = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(experience)


def _record(content_path: str) -> dict:
    return {
        "id": "exp-two-era-probe",
        "type": "research",
        "category": "framework-architecture",
        "summary": "a probe record for the two-era content_path check",
        "content_path": content_path,
    }


@pytest.fixture()
def rooted(tmp_path, monkeypatch):
    """Point the validator at a tmp PROJECT_ROOT laid out like the real one."""
    monkeypatch.setattr(experience, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(experience, "AGENTS_PARENT_DIR", "agents")
    modern = tmp_path / "agents" / "bravo" / "experience"
    modern.mkdir(parents=True)
    (modern / "foo.md").write_text("# body", encoding="utf-8")
    return tmp_path


# ------------------------- the defect, now fixed -------------------------

def test_legacy_shape_validates(rooted):
    """`bravo/experience/foo.md` — the pre-relocation shape. THE defect."""
    experience.validate_record(_record("bravo/experience/foo.md"))


def test_modern_shape_still_validates(rooted):
    experience.validate_record(_record("agents/bravo/experience/foo.md"))


def test_absolute_path_still_validates(rooted):
    abs_p = rooted / "agents" / "bravo" / "experience" / "foo.md"
    experience.validate_record(_record(str(abs_p)))


# ------------- the negatives (load-bearing — guard-2860) -------------

def test_genuinely_missing_file_is_still_refused(rooted):
    """The whole point of the check. A relaxation that lost this would let a
    dangling pointer validate, and the orphan ratchet would have nothing left
    to catch."""
    with pytest.raises(ValueError, match="content_path file does not exist"):
        experience.validate_record(_record("bravo/experience/nope.md"))


def test_wrong_agent_is_still_refused(rooted):
    """Only `bravo` has the file. A path naming another agent must NOT resolve
    just because the tail matches."""
    with pytest.raises(ValueError, match="content_path file does not exist"):
        experience.validate_record(_record("zeta/experience/foo.md"))


def test_same_basename_elsewhere_is_not_accepted(rooted):
    """The explicit anti-basename-match pin. `foo.md` exists under
    agents/bravo/experience/, and this path's basename matches it — but the
    PATH does not resolve under either era, so it must be refused."""
    (rooted / "somewhere").mkdir()
    (rooted / "somewhere" / "foo.md").write_text("decoy", encoding="utf-8")
    with pytest.raises(ValueError, match="content_path file does not exist"):
        experience.validate_record(_record("bravo/notes/foo.md"))


def test_absolute_missing_path_gets_no_legacy_fallback(rooted):
    """An absolute path is taken at its word — prepending the agents parent to
    an absolute path would be meaningless, and silently doing so would hide a
    real bad pointer."""
    with pytest.raises(ValueError, match="content_path file does not exist"):
        experience.validate_record(_record(str(rooted / "no" / "such.md")))


# ------------------------- shape of the fallback -------------------------

def test_no_double_prefix_is_attempted(rooted, monkeypatch):
    """A path already carrying the agents parent must not be retried as
    `agents/agents/...`. Proven by recording every path the check tests."""
    seen = []
    real_exists = Path.exists

    def spy(self):
        seen.append(str(self))
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", spy)
    with pytest.raises(ValueError):
        experience.validate_record(_record("agents/bravo/experience/gone.md"))
    assert not any("agents/agents" in s.replace("\\", "/") for s in seen), seen


def test_empty_agents_parent_dir_disables_the_fallback(rooted, monkeypatch):
    """AGENTS_PARENT_DIR == "" is the legacy layout, where both forms are the
    SAME path. The fallback must be skipped rather than probing PROJECT_ROOT a
    second time under a different name."""
    monkeypatch.setattr(experience, "AGENTS_PARENT_DIR", "")
    with pytest.raises(ValueError, match="content_path file does not exist"):
        experience.validate_record(_record("bravo/experience/foo.md"))


def test_the_fallback_is_routed_through_the_constant():
    """CLAUDE.md Agent-dir Resolution: never write a literal `agents/` segment.
    The source must reach the fallback via AGENTS_PARENT_DIR so a future rename
    of the constant moves this check with it."""
    src = (CORE_SCRIPTS / "experience.py").read_text(encoding="utf-8")
    assert "PROJECT_ROOT / AGENTS_PARENT_DIR / content_path" in src
    assert "AGENTS_PARENT_DIR" in src.split("def validate_record")[1]


def test_usage_example_advertises_a_shape_its_own_validator_accepts():
    """The second instance of the same split: experience.py's --help example
    advertised `bravo/experience/foo.md`, the legacy shape. A caller copying the
    documented example got validation_failed. It now shows the current era."""
    src = (CORE_SCRIPTS / "experience.py").read_text(encoding="utf-8")
    assert 'content_path\\":\\"agents/bravo/experience/foo.md' in src
