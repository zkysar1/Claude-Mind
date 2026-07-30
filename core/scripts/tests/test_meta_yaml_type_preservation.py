""": meta-YAML set refuses a silent structured -> scalar-string replacement.

THE BUG (live-reproduced during g-115-3433, real data loss). `_parse_value`
attempts ``json.loads`` ONLY when the stripped value starts with ``[`` or ``{``,
and its invalid-JSON branch falls through to returning the raw string. Nothing
compared the NEW value's type against the value already at the dotpath. So a
dotpath whose current value was a 33-element list of dicts was silently replaced
by one scalar string — at HTTP 200 / exit code 0, with nothing logged as wrong.

Measured: a caller built a JSON array and a stray line of stdout got prepended,
so the payload began ``prepared 34 gaps (was 33)\\n[...``. It starts with ``p``,
so no JSON parse was even attempted, and all 33 gap dicts in meta/skill-gaps.yaml
became a single 50,359-char string. `meta-read.sh skill-gaps.yaml` then returned
`gaps` as type ``str``. Recovered from a snapshot; nothing errored anywhere.

Why the existing guard did not cover it: g-115-1263 closed only the WELL-FORMED
JSON half of this class (a JSON array now round-trips to a real YAML list instead
of stringifying). The malformed/garbage-prefixed half was explicitly documented as
falling through to the string return, so the class was HALF closed.

WHY BOTH IMPLEMENTATIONS ARE TESTED. `meta-set.sh` is daemon-routed
(``rt_call POST /v1/meta/yaml/set``, no Python CLI fallback per
.claude/rules/no-python-cli-fallback.md), so ``mind_api/src/meta/meta_yaml.py``
is the LIVE path and ``core/scripts/meta-yaml.py`` is its CLI twin. Fixing only
the CLI would have been a production no-op. The last test pins that the two stay
in sync, because a divergence is invisible to any test that exercises one alone.

REACHABILITY. `meta-set.sh` builds its body with ``'value': sys.argv[3]`` — always
a STRING — so ``_coerce_set_value`` always reaches ``_parse_value``. The defect is
reachable through the production call shape, not just theoretically.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1]        # core/scripts
_ROOT = _SCRIPTS.parents[1]                           # PROJECT_ROOT
for _p in (str(_SCRIPTS), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mind_api.src.meta import meta_yaml as MY  # noqa: E402


def _gaps(n=33):
    return [{"id": "gap-%03d" % i} for i in range(1, n + 1)]


def _verdict(prev, payload, string_flag=False, dotpath="gaps"):
    """Run the production coerce+assert pair; return 'allow' or the error code."""
    value = MY._coerce_set_value(payload, string_flag)
    try:
        MY._assert_type_preserved(prev, value, string_flag, dotpath)
    except MY._MetaYamlError as e:
        return e.code
    return "allow"


# --- the defect itself -----------------------------------------------------

def test_garbage_prefixed_payload_is_refused():
    """The exact incident shape: a stray stdout line prepended to a JSON array."""
    payload = 'prepared 34 gaps (was 33)\n[{"id": "gap-034"}]'
    assert _verdict(_gaps(), payload) == "type_destruction"


def test_malformed_json_payload_is_refused():
    """Starts with '[' so json.loads IS attempted, but fails -> falls through to
    str. This is the branch parse_value's own docstring documents; it destroys
    the key just as thoroughly as the no-parse-attempted branch above."""
    assert _verdict(_gaps(), '[{"id": "gap-034"') == "type_destruction"


def test_dict_valued_key_is_protected_too():
    """The guard is on list OR dict — not lists alone."""
    assert _verdict({"a": 1, "b": 2}, "oops not json") == "type_destruction"


def test_refusal_message_names_the_knob_and_both_shapes():
    """rb-5242's transferable half: name the knob. A refusal the caller cannot
    act on just converts silent corruption into a silent stall."""
    value = MY._coerce_set_value("oops", False)
    with pytest.raises(MY._MetaYamlError) as ei:
        MY._assert_type_preserved(_gaps(), value, False, "gaps")
    detail = ei.value.detail
    assert "gaps" in detail                      # the knob
    assert "list of 33 item(s)" in detail        # what is being destroyed
    assert "--string" in detail                  # the deliberate override
    assert "g-115-3462" in detail                # provenance


# --- everything that must STILL work (false-refusal guards) ----------------

@pytest.mark.parametrize("prev,payload,string_flag,why", [
    (_gaps(), '[{"id": "gap-034"}]', False, "well-formed JSON array round-trips (g-115-1263)"),
    (_gaps(), "deliberate literal",  True,  "--string is an explicit override"),
    ({"k": 1}, "null",               False, "null disables a dict-valued knob"),
    ({"k": 1}, "42",                 False, "int is an unambiguous deliberate scalar"),
    ({"k": 1}, "true",               False, "bool likewise"),
    ("old string", "new string",     False, "str -> str is not a structure loss"),
    (None, "brand new value",        False, "writing a NEW key has nothing to preserve"),
    (7, "now a string",              False, "scalar -> scalar is not a structure loss"),
])
def test_legitimate_writes_are_not_refused(prev, payload, string_flag, why):
    assert _verdict(prev, payload, string_flag) == "allow", why


def test_wellformed_json_still_becomes_a_real_list():
    """Pins the  fix itself, not just that it is un-refused: the value
    must still arrive as a list, otherwise 'allow' would be hiding a regression
    where the array silently stringified before the guard ever saw it."""
    value = MY._coerce_set_value('[{"id": "gap-034"}]', False)
    assert isinstance(value, list) and value == [{"id": "gap-034"}]


# --- append semantics (the goal's second, separate defect) -----------------

def test_whole_element_append_navigates_but_nested_new_index_does_not():
    """Precise characterization, correcting a coarser claim in the goal text and
    in guard-661 ("meta-set.sh cannot APPEND to a list").

    It CAN: `gaps[N]` on a length-N list navigates fine, and set_field's
    `key == len(parent)` branch appends it. What fails is `gaps[N].field` —
    _navigate bounds-checks INTERMEDIATE segments but not the final key, so a
    nested write into a not-yet-existing index raises. That is exactly the form
    aspirations-spark/SKILL.md used to instruct for new-gap registration.
    """
    data = {"gaps": [{"id": "gap-001"}, {"id": "gap-002"}]}

    parent, key = MY._navigate(data, "gaps[2]")       # whole element at index==len
    assert isinstance(parent, list) and key == 2 == len(parent)

    with pytest.raises(MY._MetaYamlError) as ei:
        MY._navigate(data, "gaps[2].id")              # nested into a new index
    assert "out of range" in ei.value.detail


# --- twin sync -------------------------------------------------------------

def test_cli_twin_carries_the_same_guard():
    """meta-set.sh is daemon-routed, so the daemon copy is the live path and the
    CLI copy is easy to forget. A divergence is invisible to every test that
    exercises only one of them — hence this explicit pairing.
    """
    cli_src = (_SCRIPTS / "meta-yaml.py").read_text(encoding="utf-8")
    assert "def assert_type_preserved(" in cli_src, \
        "CLI twin lost its type-preservation helper (core/scripts/meta-yaml.py)"
    assert "assert_type_preserved(old_value, value, args.string, args.dotpath)" in cli_src, \
        "CLI twin defines the helper but cmd_set no longer calls it"

    import importlib.util
    spec = importlib.util.spec_from_file_location("_meta_yaml_cli", _SCRIPTS / "meta-yaml.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Same verdicts as the daemon on the two branches that matter.
    with pytest.raises(SystemExit) as ei:
        mod.assert_type_preserved(_gaps(), "oops not json", False, "gaps")
    assert ei.value.code == 1
    mod.assert_type_preserved(_gaps(), "deliberate literal", True, "gaps")   # override: no exit
    mod.assert_type_preserved(None, "brand new", False, "k")                 # new key: no exit
