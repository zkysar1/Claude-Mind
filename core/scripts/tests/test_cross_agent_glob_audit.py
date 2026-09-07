"""Tests for cross-agent-glob-audit.py ().

The load-bearing test here is `test_depth1_drift_is_flagged` — the goal's own
verification check, and guard-1836's requirement: a coverage assertion has zero
discriminating power until something is shown to FAIL it. Its twin,
`test_skills_dir_glob_is_not_flagged`, is the over-match control; a checker that
flags everything passes the first test and is worthless.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AUDIT_PY = HERE.parent / "cross-agent-glob-audit.py"

_spec = importlib.util.spec_from_file_location("cross_agent_glob_audit", AUDIT_PY)
audit_mod = importlib.util.module_from_spec(_spec)
sys.modules["cross_agent_glob_audit"] = audit_mod
_spec.loader.exec_module(audit_mod)


CONVENTION_STUB = """# Agent-dir resolution

Some preamble with a DIFFERENT table that must not be read as consumers:

| file | constant |
|---|---|
| `core/scripts/session-state-get.sh` | `SESSION_DIRNAME` |

**Plus cross-agent glob consumers** (invisible to the greps above):

| file | glob | incident |
|---|---|---|
| `core/scripts/documented_one.py` | `*/local-paths.conf` | zeroed skill discovery 2026-07 |
| `core/scripts/documented_bash.sh` | `*/session` | bash consumer, no Python glob |

Trailing prose after the table.
"""


def _mkroot(tmp_path, files: dict, convention=CONVENTION_STUB):
    """Build a minimal project root: scan dirs + the convention file."""
    (tmp_path / "core" / "scripts").mkdir(parents=True)
    (tmp_path / "mind_api" / "src").mkdir(parents=True)
    conv = tmp_path / "core" / "config" / "conventions"
    conv.mkdir(parents=True)
    (conv / "agent-dir-resolution.md").write_text(convention, encoding="utf-8")
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------
# The negative control the goal names explicitly (guard-1836)
# --------------------------------------------------------------------------

def test_depth1_drift_is_flagged(tmp_path):
    """A consumer written with PROJECT_ROOT.glob('*/...') must be FLAGGED.

    This is the redrift the convention's caveat warns about: post-relocation the
    glob matches NOTHING and every audit grep stays silent. If this test passes
    trivially the whole mechanism is decorative.
    """
    root = _mkroot(tmp_path, {
        "core/scripts/drifted.py": (
            "from _paths import PROJECT_ROOT\n"
            "def sweep():\n"
            "    return list(PROJECT_ROOT.glob('*/aspirations.jsonl'))\n"
        ),
    })
    res = audit_mod.audit(root)
    assert [s["file"] for s in res["drift"]] == ["core/scripts/drifted.py"]
    assert res["drift"][0]["line"] == 3
    # and it must NOT be laundered into the covered set
    assert res["code_files"] == []


def test_drift_makes_the_audit_exit_nonzero(tmp_path, capsys):
    root = _mkroot(tmp_path, {
        "core/scripts/drifted.py":
            "from _paths import PROJECT_ROOT\n"
            "x = PROJECT_ROOT.glob('*/self.md')\n",
    })
    rc = audit_mod.render(audit_mod.audit(root))
    assert rc == 1
    assert "DEPTH-1 DRIFT" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Over-match controls — the other half of guard-1836
# --------------------------------------------------------------------------

def test_skills_dir_glob_is_not_flagged(tmp_path):
    """`*/SKILL.md` under a skills dir is a depth-1 glob but NOT cross-agent.

    A pattern-only matcher reports 59 sites where 36 are real; this is the
    control that keeps the check from becoming one.
    """
    root = _mkroot(tmp_path, {
        "core/scripts/skills.py":
            "SKILLS_DIR = object()\n"
            "def read(base=SKILLS_DIR):\n"
            "    return sorted(base.glob('*/SKILL.md'))\n",
    })
    res = audit_mod.audit(root)
    assert res["drift"] == []
    assert res["unresolved"] == []
    assert res["code_files"] == []


def test_per_function_scoping_does_not_leak_a_binding(tmp_path):
    """Two functions, same local name, different bases (skill-freshness-report).

    Module-wide binding merged these and reported the skills-dir glob as a
    cross-agent consumer. The failure is silent and lands in a trusted table.
    """
    root = _mkroot(tmp_path, {
        "core/scripts/two_bases.py": (
            "from _paths import agents_root\n"
            "SKILLS_DIR = object()\n"
            "def read_skills(skills_dir=None):\n"
            "    base = skills_dir if skills_dir is not None else SKILLS_DIR\n"
            "    return sorted(base.glob('*/SKILL.md'))\n"
            "def read_agents(r=None):\n"
            "    base = r if r is not None else agents_root()\n"
            "    return sorted(base.glob('*/skill-invocations.jsonl'))\n"
        ),
    })
    res = audit_mod.audit(root)
    lines = sorted(s["line"] for s in res["routed"])
    assert lines == [8], f"only the agents_root-bound glob is cross-agent: {res['routed']}"


def test_comment_and_docstring_mentions_are_not_sites(tmp_path):
    """guard-1099: a grep-based check flags the WARNING as readily as the code.

    Measured while building this — a regex prototype reported three docstrings
    that merely warn about the drift pattern as live drifted call sites.
    """
    root = _mkroot(tmp_path, {
        "core/scripts/warns.py": (
            '"""Never write PROJECT_ROOT.glob(\'*/aspirations.jsonl\') here."""\n'
            "# also forbidden: PROJECT_ROOT.glob('*/self.md')\n"
            "def f():\n"
            "    '''See: PROJECT_ROOT.glob(\"*/local-paths.conf\") is drift.'''\n"
            "    return None\n"
        ),
    })
    res = audit_mod.audit(root)
    assert res["drift"] == []
    assert res["glob_sites_total"] == 0


def test_constant_built_per_agent_sessions_glob_is_not_cross_agent(tmp_path):
    """`root / AGENTS_PARENT_DIR / agent / SESSIONS_DIRNAME` then `*/x` globs
    SESSIONS within ONE agent — and it routes through the constants, so the
    existing CLAUDE.md greps already see it. Not this table's population."""
    root = _mkroot(tmp_path, {
        "core/scripts/telemetry.py": (
            "from _paths import AGENTS_PARENT_DIR, SESSIONS_DIRNAME\n"
            "def probe(project_root, agent):\n"
            "    sessions_root = project_root / AGENTS_PARENT_DIR / agent / SESSIONS_DIRNAME\n"
            "    return list(sessions_root.glob('*/body-heartbeat'))\n"
        ),
    })
    res = audit_mod.audit(root)
    assert res["unresolved"] == []
    assert res["drift"] == []
    assert [s["kind"] for s in res["constant_routed"]] == ["constant-routed"]


# --------------------------------------------------------------------------
# The diff against the documented table
# --------------------------------------------------------------------------

def test_documented_consumer_is_not_reported(tmp_path):
    root = _mkroot(tmp_path, {
        "core/scripts/documented_one.py":
            "from _paths import agents_root\n"
            "x = agents_root().glob('*/local-paths.conf')\n",
    })
    res = audit_mod.audit(root)
    assert res["undocumented"] == []
    assert res["code_files"] == ["core/scripts/documented_one.py"]


def test_undocumented_consumer_is_reported_and_exits_nonzero(tmp_path):
    root = _mkroot(tmp_path, {
        "core/scripts/documented_one.py":
            "from _paths import agents_root\n"
            "x = agents_root().glob('*/local-paths.conf')\n",
        "core/scripts/brand_new.py":
            "from _paths import agents_root\n"
            "y = agents_root().glob('*/experience.jsonl')\n",
    })
    res = audit_mod.audit(root)
    assert res["undocumented"] == ["core/scripts/brand_new.py"]
    assert audit_mod.render(res) == 1


def test_receiver_names_resolve_through_a_call_wrapper(tmp_path):
    """`Path(ar()).glob(...)` with `ar` bound to agents_root — splitting the
    receiver on '.'/'(' reads the name as `Path` and misses `ar` entirely."""
    root = _mkroot(tmp_path, {
        "core/scripts/wrapped.py": (
            "from pathlib import Path\n"
            "from _paths import agents_root\n"
            "def tick(agents_root_fn=None):\n"
            "    ar = agents_root_fn if agents_root_fn is not None else agents_root\n"
            "    return list(Path(ar()).glob('*/experience.jsonl'))\n"
        ),
    })
    res = audit_mod.audit(root)
    assert [s["kind"] for s in res["routed"]] == ["routed"]
    assert res["unresolved"] == []


def test_parameter_named_agents_root_path_binds(tmp_path):
    """_frontier.py takes the root as `agents_root_path`; a \\b after
    `agents_root` fails on the underscore and demoted the convention's own
    reference consumer to UNRESOLVED."""
    root = _mkroot(tmp_path, {
        "core/scripts/frontier.py": (
            "from pathlib import Path\n"
            "def census(agents_root_path):\n"
            "    r = Path(agents_root_path)\n"
            "    return list(r.glob('*/sessions/*/body-manifest.yaml'))\n"
        ),
    })
    res = audit_mod.audit(root)
    assert [s["kind"] for s in res["routed"]] == ["routed"]


# --------------------------------------------------------------------------
# Could-not-scan is never a silent pass (rb-245 / guard-1091)
# --------------------------------------------------------------------------

def test_unparseable_file_is_a_scan_error_not_a_clean_file(tmp_path):
    root = _mkroot(tmp_path, {
        "core/scripts/broken.py": "def f(:\n  pass\n",
    })
    res = audit_mod.audit(root)
    assert res["scan_errors"], "a parse failure must not read as zero consumers"
    assert any("broken.py" in e and "unparseable" in e
               for e in res["scan_errors"]), res["scan_errors"]
    assert audit_mod.render(res) == 2


def test_missing_table_anchor_is_a_scan_error(tmp_path):
    root = _mkroot(tmp_path, {}, convention="# no anchor here\n")
    res = audit_mod.audit(root)
    assert any("anchor" in e for e in res["scan_errors"])
    assert audit_mod.render(res) == 2


def test_tests_dir_is_excluded_from_the_scan(tmp_path):
    """A test may legitimately build a depth-1 glob over a tmp tree as a
    fixture — flagging those trains readers to ignore the output."""
    root = _mkroot(tmp_path, {
        "core/scripts/tests/test_thing.py":
            "from _paths import PROJECT_ROOT\n"
            "x = PROJECT_ROOT.glob('*/aspirations.jsonl')\n",
    })
    res = audit_mod.audit(root)
    assert res["drift"] == []
    assert res["glob_sites_total"] == 0


# --------------------------------------------------------------------------
# Against the live repo
# --------------------------------------------------------------------------

def test_live_repo_is_clean():
    """The real invariant, and the reason this file is in the suite.

    A new depth-1 drifted site, or a new cross-agent consumer that never reached
    the convention, fails HERE — instead of waiting for someone to rename
    AGENTS_PARENT_DIR and read a stale table. Remedy for the undocumented case
    is one command:
        py -3 core/scripts/cross-agent-glob-audit.py --write
    """
    res = audit_mod.audit(audit_mod.PROJECT_ROOT)
    assert res["scan_errors"] == [], res["scan_errors"]
    assert res["drift"] == [], f"depth-1 drift: {res['drift']}"
    assert res["unresolved"] == [], f"unclassifiable receiver: {res['unresolved']}"
    assert res["undocumented"] == [], (
        "new cross-agent glob consumer(s) not in the convention; regenerate with "
        "`py -3 core/scripts/cross-agent-glob-audit.py --write`: "
        f"{res['undocumented']}")


def test_generated_block_round_trips_and_still_catches_new_drift(tmp_path):
    """--write must record what EXISTS, never bless what arrives later.

    A generated record that swallowed the next consumer would be strictly worse
    than the stale table it replaces: it would look maintained AND be blind.
    """
    root = _mkroot(tmp_path, {
        "core/scripts/documented_one.py":
            "from _paths import agents_root\n"
            "x = agents_root().glob('*/local-paths.conf')\n",
        "core/scripts/newcomer.py":
            "from _paths import agents_root\n"
            "y = agents_root().glob('*/self.md')\n",
    })
    res = audit_mod.audit(root)
    assert res["undocumented"] == ["core/scripts/newcomer.py"]

    audit_mod.write_generated_block(root, res)
    after = audit_mod.audit(root)
    assert after["undocumented"] == [], "the write must clear what it recorded"

    # A LATER consumer is still caught — the block is a record, not an amnesty.
    (root / "core/scripts/later.py").write_text(
        "from _paths import agents_root\n"
        "z = agents_root().glob('*/curriculum.yaml')\n", encoding="utf-8")
    assert audit_mod.audit(root)["undocumented"] == ["core/scripts/later.py"]

    # ...and a depth-1 drift is never recorded as covered by --write either.
    (root / "core/scripts/drift.py").write_text(
        "from _paths import PROJECT_ROOT\n"
        "w = PROJECT_ROOT.glob('*/local-paths.conf')\n", encoding="utf-8")
    d = audit_mod.audit(root)
    audit_mod.write_generated_block(root, d)
    assert audit_mod.audit(root)["drift"], "drift must survive a --write"


def test_write_refuses_on_an_incomplete_scan(tmp_path, monkeypatch):
    """Writing a derived record from a partial scan would bless a parse failure
    as 'these are all the consumers' (rb-245)."""
    root = _mkroot(tmp_path, {"core/scripts/broken.py": "def f(:\n  pass\n"})
    assert audit_mod.audit(root)["scan_errors"]
    conv = root / audit_mod.CONVENTION
    before = conv.read_text(encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["audit", "--root", str(root), "--write"])
    assert audit_mod.main() == 2
    assert conv.read_text(encoding="utf-8") == before, \
        "a refused write must not touch the file"


def test_live_repo_finds_the_documented_reference_consumers():
    """Positive control: the scan must actually FIND consumers. An empty
    routed set would pass every drift assertion above trivially."""
    res = audit_mod.audit(audit_mod.PROJECT_ROOT)
    assert len(res["routed"]) > 20, f"only {len(res['routed'])} sites — scan is blind?"
    assert "core/scripts/_frontier.py" in res["code_files"]
    assert "core/scripts/skill-discovery.py" in res["code_files"]
