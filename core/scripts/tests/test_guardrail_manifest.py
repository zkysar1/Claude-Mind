"""Tests for guardrail_manifest.build — the prime/worker-loop guardrail id manifest.

The property under test is the SAFETY FLOOR, not the byte saving: every guardrail
id present in `guardrails-read.sh --summary` must appear in the manifest exactly
once. guard-841 forbids substituting a ranked slice for the 100%-coverage index,
so a manifest that silently drops a record is the one failure that matters.
"""
import importlib.util
import io
import os
import pathlib
import shlex
import shutil
import subprocess
import sys

import pytest

# conftest.py puts this dir on sys.path for collected tests; the insert keeps a
# direct `py -3 <file>` invocation working too (see _bash_helpers docstring).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare "bash" argv[0])

_SPEC = importlib.util.spec_from_file_location(
    "guardrail_manifest",
    pathlib.Path(__file__).resolve().parents[1] / "guardrail_manifest.py",
)
gm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gm)


def build(text):
    return gm.build(io.StringIO(text))


def test_every_id_survives():
    src = "\n".join(
        "guard-{:03d}: [cat-{}] some rule text here".format(i, i % 7) for i in range(1, 201)
    )
    text, count, ncats, bad, cont = build(src)
    assert count == 200
    assert ncats == 7
    assert bad == [] and cont == 0
    for i in range(1, 201):
        assert "guard-{:03d}".format(i) in text


def test_no_rule_text_leaks_into_the_manifest():
    text, _, _, _, _ = build("guard-001: [alpha] SECRET_RULE_BODY should not appear\n")
    assert "guard-001" in text
    assert "SECRET_RULE_BODY" not in text


def test_ids_are_grouped_under_their_category_with_a_count():
    text, _, _, _, _ = build(
        "guard-001: [alpha] a\nguard-002: [beta] b\nguard-003: [alpha] c\n"
    )
    lines = {ln.split("]")[0].lstrip("["): ln for ln in text.strip().split("\n")}
    assert "(2) guard-001 guard-003" in lines["alpha"]
    assert "(1) guard-002" in lines["beta"]


def test_categories_are_sorted_so_output_is_stable():
    text, _, _, _, _ = build("guard-001: [zulu] z\nguard-002: [alpha] a\n")
    assert text.index("[alpha]") < text.index("[zulu]")


def test_wrapped_rule_text_is_folded_not_dropped_and_not_fatal():
    """--summary is NOT strictly one line per record: a rule containing an embedded
    newline wraps. Measured 2026-08-19: 3 of 4,102 lines. The continuation carries
    no id, so it belongs to the record above it — counted, never fatal."""
    text, count, _, bad, cont = build(
        "guard-001: [alpha] first half of the rule\n"
        "second half with no id or bracket\n"
        "guard-002: [beta] b\n"
    )
    assert count == 2, "the wrap must not be counted as a third record"
    assert cont == 1
    assert bad == [], "a wrap is normal input, not garbage"
    assert "guard-001" in text and "guard-002" in text


def test_leading_garbage_has_no_record_to_attach_to_and_IS_reported():
    """A non-matching line with nothing above it cannot be a wrap. Reporting it is
    what stops a malformed corpus from silently yielding a thin manifest."""
    _, _, _, bad, _ = build("TOTAL GARBAGE\nguard-001: [alpha] a\n")
    assert len(bad) == 1


def test_colon_after_the_id_is_optional():
    a, _, _, _, _ = build("guard-001: [alpha] a\n")
    b, _, _, _, _ = build("guard-001 [alpha] a\n")
    assert a == b


def test_empty_input_yields_zero_records_so_the_caller_can_refuse():
    text, count, ncats, bad, cont = build("")
    assert count == 0 and ncats == 0 and text == "" and bad == [] and cont == 0


def test_blank_lines_are_ignored_without_being_treated_as_wraps():
    _, count, _, bad, cont = build("guard-001: [alpha] a\n\n   \nguard-002: [alpha] b\n")
    assert count == 2 and cont == 0 and bad == []


@pytest.mark.parametrize("width", [1, 80, 400])
def test_manifest_size_is_independent_of_rule_text_length(width):
    """The whole point: manifest bytes track RECORD COUNT, not text volume — which
    is why the fix survives growth that per-line compression could not."""
    text, _, _, _, _ = build("guard-001: [alpha] {}\n".format("x" * width))
    assert text == "[alpha] (1) guard-001\n"


# ── wrapper (guardrail-manifest.sh) ──────────────────────────────────────────
# The tests above exercise build() in isolation. This one runs the SHELL WRAPPER
# as a subprocess, because the one defect that reached production lived there and
# not in the transformer ( fresh-eyes, 2026-08-19).

_WRAPPER = pathlib.Path(__file__).resolve().parents[1] / "guardrail-manifest.sh"


def _fake_reader_env(tmp_path, stderr_line, stdout_lines, rc=0):
    """Stand up a tmp SCRIPT_DIR holding the real wrapper + transformer beside a
    FAKE guardrails-read.sh. The wrapper resolves everything off its own location,
    so copying it is what makes this hermetic — no daemon, no live store."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    for real in (_WRAPPER, _WRAPPER.parent / "guardrail_manifest.py"):
        shutil.copy(real, tmp_path / real.name)
    (tmp_path / "_paths.sh").write_text("# stub: the wrapper sources this; nothing here is used\n")
    reader = tmp_path / "guardrails-read.sh"
    body = "#!/usr/bin/env bash\n"
    if stderr_line:
        body += 'echo {} >&2\n'.format(shlex.quote(stderr_line))
    for ln in stdout_lines:
        body += "echo {}\n".format(shlex.quote(ln))
    body += "exit {}\n".format(rc)
    reader.write_text(body)
    reader.chmod(0o755)
    return tmp_path / _WRAPPER.name


def _run(script, tmp_path):
    # guard-580: never a bare "bash" argv[0] (System32 WSL on win32 hangs).
    # guard-581: pass the script path via .as_posix(), never str(WindowsPath).
    env = dict(os.environ, STORAGE_BACKEND="local")  # guard-955
    return subprocess.run(
        [BASH, pathlib.Path(script).as_posix()], cwd=str(tmp_path), env=env,
        capture_output=True, text=True, timeout=60,
    )


@pytest.mark.skipif(not _WRAPPER.exists(), reason="wrapper not present")
def test_wrapper_does_not_merge_stderr_into_the_parsed_payload(tmp_path):
    """REGRESSION (guard-1963 / guard-659 / guard-1675). The wrapper captures
    guardrails-read.sh's stdout as a PAYLOAD the transformer parses. Capturing it
    with `2>&1` folds any stderr diagnostic into that payload — and _runtime.sh
    emits `[runtime] WARNING: daemon is running stale code ...` on stderr while
    still returning rc=0, a state that arises right after every framework commit.
    With the merge, the warning lands first, the transformer sees a leading
    unparseable line with nothing above it, and REFUSES: prime and worker-loop get
    no guardrail index at all. Re-adding `2>&1` to the wrapper reddens this test."""
    warning = "[runtime] WARNING: daemon is running stale code (sha abc12345, on-disk def67890)."
    script = _fake_reader_env(
        tmp_path, warning,
        ["guard-001: [alpha] a real rule", "guard-002: [beta] another real rule"],
    )
    r = _run(script, tmp_path)
    assert r.returncode == 0, "wrapper refused a healthy read: {!r}".format(r.stderr)
    assert "guard-001" in r.stdout and "guard-002" in r.stdout
    assert "[runtime]" not in r.stdout, "stderr leaked into the manifest payload"
    assert "[runtime]" in r.stderr, "the warning must stay VISIBLE — 2>/dev/null is not the fix (guard-659 refinement)"


@pytest.mark.skipif(not _WRAPPER.exists(), reason="wrapper not present")
def test_wrapper_refuses_when_the_reader_fails_and_when_it_returns_empty(tmp_path):
    """Both refusal branches. An empty index reads as HEALTHY downstream, so an
    empty-with-rc-0 read must exit non-zero rather than emit an empty manifest."""
    failed = _fake_reader_env(tmp_path / "a", "boom: daemon unreachable", [], rc=1)
    r = _run(failed, tmp_path / "a")
    assert r.returncode != 0 and "not emitting a manifest" in r.stderr

    empty = _fake_reader_env(tmp_path / "b", "", [], rc=0)
    r = _run(empty, tmp_path / "b")
    assert r.returncode != 0 and "refusing to emit an empty manifest" in r.stderr
