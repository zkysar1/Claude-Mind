"""Tests for guardrail_manifest.build — the prime/worker-loop guardrail id manifest.

The property under test is the SAFETY FLOOR, not the byte saving: every guardrail
id present in `guardrails-read.sh --summary` must appear in the manifest exactly
once. guard-841 forbids substituting a ranked slice for the 100%-coverage index,
so a manifest that silently drops a record is the one failure that matters.
"""
import importlib.util
import io
import json
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


# ── : emit-time budget enforcement ─────────────────────────────────
# The budget lived in prose with no gate and the first breach reached 3.2x over
# in silence. These pin the WARN-never-refuse contract with the REAL constants
# (no monkeypatch): a 9,000-id corpus genuinely exceeds the 100,000-byte
# threshold (40k tokens x 2.5 B/token), a 200-id corpus genuinely does not.


def _run_main(monkeypatch, capsys, src, argv=None):
    monkeypatch.setattr(sys, "stdin", io.StringIO(src))
    monkeypatch.setattr(sys, "argv", ["guardrail_manifest.py"] + (argv or []))
    rc = gm.main()
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_over_budget_warns_loudly_but_never_refuses(monkeypatch, capsys):
    src = "\n".join(
        "guard-{:05d}: [cat-{}] rule text".format(i, i % 5) for i in range(1, 9001)
    )
    rc, out, err = _run_main(monkeypatch, capsys, src)
    assert rc == 0, "WARN-never-refuse: an absent index is worse than an oversized one"
    assert "BUDGET BREACH" in err
    # Denominators travel with the estimate (guard-2529/guard-2129 class).
    assert "9000 records" in err
    assert str(gm.BUDGET_TOKENS) in err
    # The manifest itself is still complete — coverage floor unaffected.
    assert "guard-00001" in out and "guard-09000" in out


def test_under_budget_is_silent(monkeypatch, capsys):
    src = "\n".join(
        "guard-{:03d}: [cat-{}] rule text".format(i, i % 5) for i in range(1, 201)
    )
    rc, out, err = _run_main(monkeypatch, capsys, src)
    assert rc == 0
    assert "BUDGET BREACH" not in err, "a warning that always fires is noise, not a gate"


def test_stats_line_carries_token_estimate_and_budget(monkeypatch, capsys):
    src = "guard-001: [alpha] rule\n"
    rc, out, err = _run_main(monkeypatch, capsys, src, argv=["--stats"])
    assert rc == 0
    assert "est tokens" in err and str(gm.BUDGET_TOKENS) in err


# ── : CRITICAL always-load core ────────────────────────────────────
# The id manifest realizes the coverage floor but loads NO rule text — including
# for the CRITICAL tier, whose admission rule (prime-store-load-budget.md) is
# precisely that the trigger zone is not self-announcing, so expand-on-demand
# structurally cannot cover it. These pin the section's three contracts: rules
# render IN FULL above the manifest, the ids STAY in the rollup (assert-total
# untouched), and any failure DEGRADES to yesterday's shape — never refuses.


def test_critical_rules_render_in_full_above_the_id_manifest(monkeypatch, capsys, tmp_path):
    crit = tmp_path / "crit.json"
    crit.write_text(json.dumps([
        {"id": "guard-002", "category": "beta", "rule": "FULL RULE TEXT TWO"},
        {"id": "guard-001", "category": "alpha", "rule": "FULL RULE TEXT ONE"},
    ]))
    src = "guard-001: [alpha] a\nguard-002: [beta] b\nguard-003: [alpha] c\n"
    rc, out, err = _run_main(
        monkeypatch, capsys, src,
        argv=["--critical-json-file", str(crit), "--stats", "--assert-total", "3"],
    )
    assert rc == 0
    assert "=== CRITICAL guardrails (2)" in out
    # Full rule text, sorted by id, ABOVE the rollup.
    assert out.index("FULL RULE TEXT ONE") < out.index("FULL RULE TEXT TWO")
    assert out.index("=== CRITICAL") < out.index("[alpha] (2)")
    # The ids ALSO stay in the rollup — the coverage floor and --assert-total
    # semantics are untouched by the section (it is additive, not a filter).
    assert "guard-001 guard-003" in out
    assert "2 CRITICAL rule(s) in full" in err


def test_malformed_critical_file_degrades_with_a_warn_and_never_refuses(monkeypatch, capsys, tmp_path):
    """The asymmetry with build()'s refusals is deliberate: those guard the
    id-coverage floor; this section is additive, and its absence is exactly
    yesterday's behavior — so failures WARN and degrade, never rc!=0."""
    crit = tmp_path / "crit.json"
    crit.write_text("NOT JSON {{{")
    src = "guard-001: [alpha] a\n"
    rc, out, err = _run_main(monkeypatch, capsys, src, argv=["--critical-json-file", str(crit)])
    assert rc == 0
    assert "=== CRITICAL" not in out
    assert "guard-001" in out, "the id manifest must be intact"
    assert "CRITICAL section unavailable" in err

    rc, out, err = _run_main(
        monkeypatch, capsys, src,
        argv=["--critical-json-file", str(tmp_path / "does-not-exist.json")],
    )
    assert rc == 0 and "guard-001" in out and "CRITICAL section unavailable" in err


def test_empty_critical_list_emits_no_section_and_no_warning(monkeypatch, capsys, tmp_path):
    crit = tmp_path / "crit.json"
    crit.write_text("[]")
    rc, out, err = _run_main(
        monkeypatch, capsys, "guard-001: [alpha] a\n",
        argv=["--critical-json-file", str(crit), "--stats"],
    )
    assert rc == 0
    assert "=== CRITICAL" not in out
    assert "CRITICAL section unavailable" not in err, "a clean zero is not a failure"
    assert "0 CRITICAL rule(s) in full" in err


def test_budget_accounting_covers_the_critical_section_bytes(monkeypatch, capsys, tmp_path):
    """Prime pays for every byte on stdout — a critical section that pushed the
    payload over budget while the rollup alone sat under it must still warn."""
    crit = tmp_path / "crit.json"
    crit.write_text(json.dumps(
        [{"id": "guard-001", "category": "alpha", "rule": "x" * 120_000}]
    ))
    src = "guard-001: [alpha] a\n"
    rc, out, err = _run_main(monkeypatch, capsys, src, argv=["--critical-json-file", str(crit)])
    assert rc == 0
    assert "BUDGET BREACH" in err


# ── wrapper: the --severity CRITICAL fetch () ──────────────────────


def _branching_reader_env(tmp_path, summary_lines, crit_json_text=None, crit_rc=0):
    """Fake guardrails-read.sh that answers --summary AND --severity distinctly —
    the wrapper now calls it twice, and the two payloads have different shapes."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    for real in (_WRAPPER, _WRAPPER.parent / "guardrail_manifest.py"):
        shutil.copy(real, tmp_path / real.name)
    (tmp_path / "_paths.sh").write_text("# stub\n")
    reader = tmp_path / "guardrails-read.sh"
    body = "#!/usr/bin/env bash\n"
    body += 'if [ "${1:-}" = "--severity" ]; then\n'
    if crit_json_text is not None:
        body += "  printf '%s\\n' {}\n".format(shlex.quote(crit_json_text))
    body += "  exit {}\nfi\n".format(crit_rc)
    for ln in summary_lines:
        body += "echo {}\n".format(shlex.quote(ln))
    body += "exit 0\n"
    reader.write_text(body)
    reader.chmod(0o755)
    return tmp_path / _WRAPPER.name


@pytest.mark.skipif(not _WRAPPER.exists(), reason="wrapper not present")
def test_wrapper_fetches_critical_tier_and_renders_it_in_full(tmp_path):
    script = _branching_reader_env(
        tmp_path,
        ["guard-001: [alpha] truncated summary text", "guard-002: [beta] b"],
        crit_json_text=json.dumps(
            [{"id": "guard-001", "category": "alpha", "rule": "THE FULL UNTRUNCATED RULE"}]
        ),
    )
    r = _run(script, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "=== CRITICAL guardrails (1)" in r.stdout
    assert "THE FULL UNTRUNCATED RULE" in r.stdout
    assert "guard-001" in r.stdout.split("===")[-1], "id must also stay in the rollup"


@pytest.mark.skipif(not _WRAPPER.exists(), reason="wrapper not present")
def test_wrapper_degrades_to_plain_manifest_when_severity_fetch_fails(tmp_path):
    """Fail-open contract: the CRITICAL section is an enhancement. A reader that
    cannot answer --severity (older daemon, transient error) must cost us the
    section only — never the manifest (the g-115-6703 lesson, applied forward)."""
    script = _branching_reader_env(
        tmp_path, ["guard-001: [alpha] a", "guard-002: [beta] b"], crit_rc=1,
    )
    r = _run(script, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "guard-001" in r.stdout and "guard-002" in r.stdout
    assert "=== CRITICAL" not in r.stdout
    assert "without the CRITICAL-full section" in r.stderr
