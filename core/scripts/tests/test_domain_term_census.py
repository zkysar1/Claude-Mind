#!/usr/bin/env python3
"""Pin domain-term-census.py against the scanner it claims to measure ().

The census INVERTS the scanner's loop (one pass over the corpus instead of one
recursive grep per term) because the scanner's shape is O(terms x corpus) and a
census runs hundreds of terms. The price of that inversion is that the census
carries its OWN copy of the scan scope and the exemption cascade.

That copy is the whole risk. If `domain-leak-check.sh` gains a scan dir, an
include suffix, or a filter and the census does not, the census silently reports
on a DIFFERENT corpus than the wall enforces -- and both would look healthy. So
every duplicated constant is pinned against the scanner's literal text here
(rb-1915: pin both sides to a shared fixture; the fixture is the scanner).

The marker predicate is deliberately NOT re-pinned: the census imports it from
`_domain_leak_marker`, which is already the single source of truth with its own
cross-engine test. What IS pinned is that the census imports it rather than
re-deriving it -- the exact regression g-115-10246 removed.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPTS.parent.parent
MODULE_PATH = SCRIPTS / "domain-term-census.py"
SCANNER_PATH = SCRIPTS / "domain-leak-check.sh"

sys.path.insert(0, str(SCRIPTS))


def _load():
    spec = importlib.util.spec_from_file_location("domain_term_census", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()
SCANNER = SCANNER_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Scope parity with the scanner
# --------------------------------------------------------------------------

def test_every_scan_dir_appears_in_the_scanner():
    """A dir the census walks that the scanner does not scan is an over-count."""
    for rel in M.CORE_SCAN_DIRS + M.EXTRA_SCAN_DIRS:
        assert f'PROJECT_ROOT/{rel}"' in SCANNER, (
            f"census scans {rel!r} but domain-leak-check.sh does not list it"
        )


def test_every_scanner_scan_dir_appears_in_the_census():
    """The direction that matters more: a dir the SCANNER covers and the census
    misses is an UNDER-count, and an under-count reads as coverage."""
    found = set(re.findall(r'"\$PROJECT_ROOT/([A-Za-z0-9_./-]+)"', SCANNER))
    # The scanner also references the blocklist and sub-paths under scan dirs;
    # keep only the ones it actually pushes onto SCAN_DIRS.
    scan_dirs = {d for d in found
                 if f'SCAN_DIRS+=("$PROJECT_ROOT/{d}")' in SCANNER.replace(" ", "")
                 or f'"$PROJECT_ROOT/{d}"' in SCANNER.split("SCAN_DIRS=(")[1].split(")")[0]}
    census = set(M.CORE_SCAN_DIRS) | set(M.EXTRA_SCAN_DIRS)
    missing = scan_dirs - census
    assert not missing, (
        f"domain-leak-check.sh scans {sorted(missing)} but the census does not walk "
        "them -- the census would report a clean term over an unscanned corpus"
    )


def test_include_suffixes_match_the_scanner_include_globs():
    globs = set(re.findall(r'--include="\*(\.[a-z]+)"', SCANNER))
    assert globs, "could not find any --include globs in the scanner (parse broke)"
    assert set(M.INCLUDE_SUFFIXES) == globs, (
        f"census suffixes {sorted(M.INCLUDE_SUFFIXES)} != scanner globs {sorted(globs)}"
    )


def test_self_referential_filters_match_the_scanner():
    for name in M.SELF_REFERENTIAL:
        assert f'grep -v "{name}"' in SCANNER, (
            f"census drops {name!r} as self-referential but the scanner does not"
        )


def test_test_fixture_regex_matches_the_scanner_regex():
    """The scanner's regex is applied to `path:line:text`; the census applies an
    end-anchored form to the path. Both must accept and reject the same paths."""
    m = re.search(r"grep -vE '([^']+)'", SCANNER)
    assert m, "could not locate the scanner's test-fixture regex"
    scanner_rx = re.compile(m.group(1))
    for path, is_fixture in (
        ("core/scripts/tests/test_foo.py", True),
        ("core/scripts/test-capability-gate.sh", True),
        ("core/scripts/tests/conftest.py", False),
        ("core/scripts/domain-term-census.py", False),
        ("core/config/testing.md", False),
    ):
        census_says = M.TEST_FIXTURE_RX.search("/" + path) is not None
        scanner_says = scanner_rx.search(path + ":1:x") is not None
        assert census_says == scanner_says == is_fixture, (
            f"{path}: census={census_says} scanner={scanner_says} expected={is_fixture}"
        )


def test_forged_front_matter_predicate_matches_the_scanner():
    assert "forged:" in SCANNER and "true" in SCANNER
    assert M.FORGED_FRONT_MATTER_RX.search("forged: true\n")
    assert M.FORGED_FRONT_MATTER_RX.search("forged:true\n")
    # Prose must NOT exempt -- the same over-match guard-6989 removed elsewhere.
    assert not M.FORGED_FRONT_MATTER_RX.search("a forged: true skill is exempt\n")


def test_census_imports_the_shared_marker_predicate_and_does_not_redefine_it():
    """guard-6989 / : the marker predicate has ONE home."""
    src = MODULE_PATH.read_text(encoding="utf-8")
    assert "from _domain_leak_marker import claims_exemption" in src
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert 'domain-leak-exempt:"' not in code, (
        "the census re-declares the marker token in code -- import the predicate"
    )


# --------------------------------------------------------------------------
# Counting semantics
# --------------------------------------------------------------------------

def test_single_token_counting_equals_grep_w_semantics():
    """A single-token term is counted by token-set membership. That is only
    legitimate because grep's word constituents are exactly [A-Za-z0-9_]."""
    c = M.TermCounter(["Roblox"])
    c.feed("we ship Roblox today", exempt=False)          # bare word
    c.feed("path/to/Roblox-Integration/x", exempt=False)  # hyphen is a boundary
    c.feed("RobloxStudio is one token", exempt=False)     # NOT a match
    assert c.in_scope["Roblox"] == 2
    assert c.unmarked["Roblox"] == 2


def test_case_sensitivity_is_tracked_separately():
    c = M.TermCounter(["EFS"])
    c.feed("mount the efs share", exempt=False)
    c.feed("mount the EFS share", exempt=False)
    assert c.in_scope["EFS"] == 2        # case-insensitive
    assert c.unmarked_cs["EFS"] == 1     # case-sensitive


def test_multi_token_terms_use_a_real_word_boundary_regex():
    c = M.TermCounter(["security group"])
    c.feed("open the security group now", exempt=False)
    c.feed("security groupings are different", exempt=False)
    assert c.in_scope["security group"] == 1


def test_exempt_files_count_toward_in_scope_but_not_unmarked():
    """The GAP between these two columns is the census's whole point."""
    c = M.TermCounter(["Roblox"])
    c.feed("Roblox", exempt=True)
    c.feed("Roblox", exempt=False)
    assert c.in_scope["Roblox"] == 2
    assert c.unmarked["Roblox"] == 1


# --------------------------------------------------------------------------
# Exemption cascade
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rel,text,expected", [
    ("core/config/domain-term-blocklist.txt", "x", "self-referential"),
    ("core/scripts/tests/test_x.py", "x", "test-fixture"),
    (".claude/skills/verify-learning/SKILL.md", "x", "meta-documentation"),
    ("core/scripts/x.py", "# domain-leak-exempt: why\n", "exempt-marker"),
    ("core/scripts/x.py", "forged: true\n", "forged-front-matter"),
    ("core/scripts/x.py", "ordinary content\n", None),
    # The  defect: PROSE about the marker must not exempt.
    ("core/config/x.md", "the domain-leak-exempt: marker is described here\n", None),
])
def test_exemption_reason(rel, text, expected):
    assert M.exemption_reason(rel, text) == expected


# --------------------------------------------------------------------------
# The positive control, and proof it can fail
# --------------------------------------------------------------------------

def test_sentinel_is_not_a_contiguous_literal_in_the_source():
    """guard-6087: this file is in scope, so a spelled-out sentinel would be
    found in the census's own source and the control would report the term
    present before anything was planted. The first build did exactly that."""
    src = MODULE_PATH.read_text(encoding="utf-8")
    assert M.SENTINEL not in src, (
        f"{M.SENTINEL!r} appears verbatim in the census source -- the positive "
        "control is falsified by its own installation"
    )


def test_counter_reports_zero_for_an_absent_term_and_one_after_planting():
    """The control's logic, exercised without touching the real tree."""
    c = M.TermCounter([M.SENTINEL])
    c.feed("nothing relevant here", exempt=False)
    assert c.in_scope[M.SENTINEL] == 0
    c.feed(f"planted {M.SENTINEL} here", exempt=False)
    assert c.in_scope[M.SENTINEL] == 1


# --------------------------------------------------------------------------
# Void, never partial (guard-2081)
# --------------------------------------------------------------------------

def test_missing_class_a_registry_voids_the_census(tmp_path):
    """A registry that cannot be read must VOID the aggregate. Silently
    shrinking the candidate universe would report improved coverage."""
    empty_world = tmp_path / "world"
    empty_world.mkdir()
    with pytest.raises(M.CensusVoid):
        M.derive_candidates(empty_world)


def test_unresolved_world_voids_rather_than_returning_class_b_only():
    with pytest.raises(M.CensusVoid):
        M.derive_candidates(None)


def test_forged_skill_dirs_fails_open_like_the_scanner(tmp_path):
    """The scanner scans everything when the registry is absent; so does this.
    Fail-open here is an OVER-count, which is the safe direction."""
    assert M.forged_skill_dirs(tmp_path) == set()
    assert M.forged_skill_dirs(None) == set()


# --------------------------------------------------------------------------
# Derivation contract
# --------------------------------------------------------------------------

def test_triggers_are_not_a_derivation_source():
    """Outcome 2 names forged-skill NAMES. Deriving from `triggers:` (natural
    language) flooded the first build's universe with English -- 1,788 terms
    topped by `path`, `never`, `source`."""
    src = MODULE_PATH.read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "forged-skills/trigger" not in code


def test_no_hand_typed_stopword_list():
    """The defect this goal exists to remove, one layer down: an unauditable
    typed list deciding what the audit looks at."""
    assert not hasattr(M, "STOPWORDS"), (
        "a hand-typed stopword list reappeared -- the filter must be measured"
    )


def test_default_share_filter_hides_nothing():
    """No measured threshold separates domain terms from framework vocabulary
    (the band closed once the derivation was corrected), so a default filter
    would discard real terms while looking principled."""
    assert M.DEFAULT_MAX_SHARE == 1.0
