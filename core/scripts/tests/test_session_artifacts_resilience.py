"""9: ONE malformed tree node must not blind the encoding signal.

THE BUG THIS PINS
-----------------
`session_artifacts_count.py::count_tree_writes` used to call `yaml.safe_load()` on
every knowledge-tree node's front matter with NO guard. A single node with malformed
YAML raised, killing the whole count. `productivity-stop-gate.sh` then converted that
nonzero exit into `counts = all zero` and printed `encoding_ratio=0.00` as though it
had MEASURED something.

That is not a cosmetic logging bug:

  * `encoding_ratio` carries WEIGHT 0.3 in the composite productivity score. A crashed
    counter subtracts up to 0.30 and can push a genuinely-productive agent BELOW
    `stop_threshold` — so a YAML typo in ONE node could FALSELY STOP AN AGENT.
  * `world/knowledge/tree` is SHARED, so the blast radius is the whole fleet: one bad
    node blinds every agent's encoding signal simultaneously.
  * It actively misinforms — it tells an agent it encoded nothing while it encoded
    plenty, which can drive exactly the wrong self-correction.

The fix is deliberately NOT "except Exception: pass". A silent skip would trade one
silent hole for another. The contract is: catch NARROWLY (yaml.YAMLError), name the
offending path LOUDLY on stderr, skip that ONE node, KEEP COUNTING the rest, and
report the degradation via `fm_parse_errors` so a consumer can distinguish "you
encoded nothing" from "I could not read N nodes".

MUTATION-VERIFIED: with the try/except removed from count_tree_writes,
`test_one_bad_node_does_not_zero_the_count` raises yaml.YAMLError instead of passing
— i.e. this test actually fails when the guard is gone. (The g-115-2195 lesson: a
regression test that still passes when you delete the thing it guards is not a test.)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import session_artifacts_count as sac  # noqa: E402

CUTOFF = "2026-07-01"

GOOD = """---
topic: "good node"
last_updated: '2026-07-14'
last_update_trigger:
  type: goal_execution
  source: "g-1"
---
body
"""

# The EXACT corruption shape observed in the wild (9): a regex front-matter
# edit replaced the nested MAPPING key `last_update_trigger:` with a SCALAR, orphaning
# its child keys at their old indent -> "expected <block end>, but found
# '<block mapping start>'".
BAD = """---
topic: "bad node"
last_updated: '2026-07-14'
last_update_trigger: 'now a scalar
  type: goal_execution
  source: "orphaned child keys"
---
body
"""


@pytest.fixture()
def tree(tmp_path):
    t = tmp_path / "tree"
    (t / "sub").mkdir(parents=True)
    (t / "good1.md").write_text(GOOD, encoding="utf-8")
    (t / "sub" / "good2.md").write_text(GOOD, encoding="utf-8")
    return t


@pytest.fixture(autouse=True)
def _reset_errors():
    sac._FM_PARSE_ERRORS.clear()
    yield
    sac._FM_PARSE_ERRORS.clear()


def test_probe_is_not_vacuous(tree):
    """NEGATIVE CONTROL. If a clean tree counted 0, every assertion below would pass
    vacuously — a broken probe is indistinguishable from a clean result unless you
    check it against a case you KNOW is positive."""
    assert sac.count_tree_writes(tree, CUTOFF) == 2
    assert sac._FM_PARSE_ERRORS == []


def test_one_bad_node_does_not_zero_the_count(tree):
    """THE LOAD-BEARING ASSERTION. A malformed node must not crash the counter, and
    must not take the GOOD nodes down with it."""
    (tree / "sub" / "bad.md").write_text(BAD, encoding="utf-8")

    # Must not raise. (Mutation check: delete the try/except in count_tree_writes and
    # this line raises yaml.YAMLError.)
    n = sac.count_tree_writes(tree, CUTOFF)

    assert n == 2, (
        f"the 2 GOOD nodes must still be counted; got {n}. A partial failure that "
        f"zeroes the whole measurement is the bug this test exists to prevent."
    )


def test_degradation_is_reported_not_silent(tree):
    """The skip must be LOUD. A silent skip trades one silent hole for another: the
    consumer would see a plausible low count and report it as a measurement."""
    (tree / "sub" / "bad.md").write_text(BAD, encoding="utf-8")
    sac.count_tree_writes(tree, CUTOFF)

    assert len(sac._FM_PARSE_ERRORS) == 1, "the unparseable node must be reported"
    assert "bad.md" in sac._FM_PARSE_ERRORS[0], "the report must NAME the offending path"


def test_bad_front_matter_really_is_unparseable():
    """Guards the FIXTURE, not the code. If a future PyYAML happily parses BAD, the two
    tests above would pass for the wrong reason (nothing to skip) and silently stop
    testing anything. Pin the premise."""
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(BAD.split("---")[1])


def test_clean_tree_reports_no_errors(tree):
    """fm_parse_errors must stay EMPTY on a healthy tree — otherwise every consumer
    would permanently see a DEGRADED banner and learn to ignore it (guard-1090)."""
    assert sac.count_tree_writes(tree, CUTOFF) == 2
    assert sac._FM_PARSE_ERRORS == [], "a clean tree must not report degradation"
