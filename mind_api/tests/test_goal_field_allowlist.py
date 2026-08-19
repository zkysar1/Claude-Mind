""" — `update-goal` must refuse an unregistered goal field at the WRITE.

THE DEFECT. `aspirations-update-goal.sh <goal> <field> <value>` accepted ANY field
name and silently created it on the shared goal record. `aspirations-update-goal.sh
g-115-5642 __probe__ x` wrote a `__probe__` key with no warning, and setting it to
the documented clearing value `null` left the key PRESENT with value None — while
CLAUDE.md forbids the direct JSONL edit that would remove it. One keystroke slip
was a permanent schema mutation on a store the whole fleet reads.

WHY IT IS SELF-CONCEALING, which is the part that makes a write-path gate the only
real fix. Every consumer that reads a goal by field name — goal-selector scoring,
the reclaim lanes, the sweeps, the daemon compose — silently ignores a stray twin.
So the write LOOKS accepted and has no effect, and the author moves on believing
state changed. Measured cost already on the record: a `precondition_unmet` FIELD
(that string is a defer_reason PREFIX, not a field name) on a goal whose author
believed it had been deferred. It never was, and nothing said so.

SCALE AT DERIVATION (2026-08-18, alpha, cc-08): 147 distinct top-level keys across
2,791 live goals in every status, 27 of them strays.

WHY THE DAEMON IS THE PRIMARY TARGET. This framework is daemon-only
(`.claude/rules/no-python-cli-fallback.md`): the wrapper reaches
`rt_call POST /v1/aspirations/update-goal`, so `aspirations.py::cmd_update_goal` is
not on the production path. The CLI twin carries the same refusal, and the last two
tests here pin that BOTH call sites import the one shared list rather than keeping
hand-typed copies — a test that exercised only the CLI copy would pass while the
defect stayed fully open (guard-742/547, the same class this codebase re-learns).
"""
import pathlib
import subprocess
import sys

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC.parent) not in sys.path:
    sys.path.insert(0, str(_SRC.parent))
_CORE_SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "core" / "scripts"
if str(_CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CORE_SCRIPTS))

from src.endpoints.aspirations_write import update_goal  # noqa: E402
import _goal_fields  # noqa: E402

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


class _StubPaths:
    project_root = pathlib.Path(".")
    world = pathlib.Path(".")
    agent_name = "test-agent"


class _StubCtx:
    """Minimal ctx. The allowlist gate fires BEFORE source resolution, path
    resolution, the gates pipeline and the lock — deliberately, so a bad field
    name costs no I/O — which is exactly what lets a stub reach it without any
    live fleet state."""

    def __init__(self, field, headers=None):
        self.query = {"id": "g-115-6573", "field": field}
        self.body = b'"x"'
        self.headers = headers or {}
        self.paths = _StubPaths()


# Every stray shape actually measured on live records, one per failure mode.
@pytest.mark.parametrize("bad", [
    "__probe__",            # the goal's own reproduction case
    "_probe",               # a second agent ran the same schema probe
    "precondition_unmet",   # a defer_reason PREFIX typed into the field slot
    "complete-by",          # hyphenated; matches a SCRIPT name, not a field
    "created",              # twin of created_at (1,935 goals carry the real one)
    "lastAchieved",         # camelCase drift from lastAchievedAt
    "description_append",   # invented to append to description
    "defer_resaon",         # a plain typo — the everyday case, never observed
                            # on disk precisely because the gate is what makes
                            # it visible
])
def test_unknown_field_is_refused_by_the_daemon(bad):
    resp = update_goal(_StubCtx(bad))
    assert resp is not None, f"{bad!r} was admitted — the gate did not fire"
    assert resp.status == 400
    body = str(getattr(resp, "body", ""))
    assert "unknown_goal_field" in body, body[:300]
    # The message must NAME the field. A refusal that does not is unactionable:
    # the caller cannot tell which of its arguments was wrong.
    assert bad in body, body[:300]


def test_refusal_names_the_field_the_author_probably_meant():
    """A near-miss should be repairable from the message alone.

    Every stray measured in the census was either a probe artifact or a near-miss
    of a real field, so the single most useful thing the refusal can carry is the
    canonical name.
    """
    body = str(getattr(update_goal(_StubCtx("created")), "body", ""))
    assert "created_at" in body, body[:300]


@pytest.mark.parametrize("good", [
    "status", "defer_reason", "progress_note", "outcome_class",
    "participants", "verification", "priority", "description",
    # Rare BUT legitimate and documented — rarity was deliberately not used to
    # classify, because these would have been refused by a frequency rule.
    "deliverable_file", "closes_knowledge_debt",
])
def test_known_fields_pass_the_allowlist_gate(good):
    """The control, and the more important half of this file.

    A false refusal on a shared write path breaks live work for the WHOLE fleet,
    which is strictly worse than the drift being fixed. `resp is None` here would
    mean the gate admitted it and execution continued past this point; any
    non-None Response means the gate refused a legitimate field.
    """
    resp = update_goal(_StubCtx(good))
    if resp is not None:
        body = str(getattr(resp, "body", ""))
        assert "unknown_goal_field" not in body, (
            f"{good!r} is legitimate but the allowlist gate refused it: {body[:300]}"
        )


def test_override_header_admits_a_new_field():
    """The escape hatch must actually open, or the gate is a wall.

    A genuinely new field is a deliberate act with a justification on the audit
    ledger — not something the author has to route around by inventing a
    different name, which is how strays are born in the first place.
    """
    resp = update_goal(_StubCtx(
        "genuinely_new_field",
        headers={"x-mind-allow-new-field": "shipping its writer in this change"},
    ))
    body = str(getattr(resp, "body", "")) if resp is not None else ""
    assert "unknown_goal_field" not in body, (
        "the override header did not admit the field: " + body[:300]
    )


def test_cli_twin_refuses_without_touching_the_store():
    """The CLI path carries the same refusal, and refuses BEFORE the lock.

    `g-000-00` does not exist; if the gate ran after the read, the error would be
    'not found' instead. Asserting on the gate's own message pins the ORDER, not
    merely the refusal — a gate placed after the lock would still refuse, while
    paying I/O for every typo.
    """
    proc = subprocess.run(
        [sys.executable, "core/scripts/aspirations.py",
         "update-goal", "g-000-00", "__probe__", "x"],
        cwd=str(_PROJECT_ROOT), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0, "CLI admitted an unknown field"
    err = proc.stderr
    assert "unknown goal field" in err, err[-400:]
    assert "not found" not in err.lower(), (
        "the gate ran AFTER the store read — it should refuse before any I/O: "
        + err[-400:]
    )


def test_both_write_paths_import_the_one_list_rather_than_copying_it():
    """The structural half, and the reason this file exists at all.

    Two hand-typed copies of an allowlist drift, and NOTHING FAILS when they do:
    the CLI-side list keeps looking correct while the daemon — the live path —
    silently diverges. Pinning the import is what makes a future copy-paste a
    test failure instead of an invisible split.
    """
    daemon_src = (_SRC / "endpoints" / "aspirations_write.py").read_text(encoding="utf-8")
    cli_src = (_CORE_SCRIPTS / "aspirations.py").read_text(encoding="utf-8")
    for name, src in (("daemon", daemon_src), ("cli", cli_src)):
        assert "_goal_fields" in src, f"{name} path does not reference the shared list"
        assert "GOAL_KNOWN_FIELDS = frozenset" not in src, (
            f"{name} path defines its own allowlist — it must IMPORT the one in "
            f"core/scripts/_goal_fields.py, never re-declare it"
        )


def test_allowlist_and_stray_set_are_disjoint():
    """A name in both sets would make the gate's verdict depend on lookup order."""
    overlap = _goal_fields.GOAL_KNOWN_FIELDS & set(_goal_fields.GOAL_STRAY_FIELDS)
    assert not overlap, f"fields classified both ways: {sorted(overlap)}"
    assert len(_goal_fields.GOAL_KNOWN_FIELDS) > 100, (
        "the allowlist collapsed — a generator bug once produced a 1-element set "
        "by concatenating adjacent string literals, which refused every field"
    )
