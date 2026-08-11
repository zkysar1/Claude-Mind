""": regression test for skillmd-flag-audit.py — the detector had none.

skillmd-flag-audit.py (g-115-3112) is wired into /verify-learning as a ratchet, but
the CHECKER ITSELF had no persistent test. Its correctness was proven by hand once,
and a hand proof is not repeatable — which left the tool in exactly the failure class
it exists to prevent: a silent regression in the detector reads as "no findings",
which reads as "no drift".

Concrete precedent from the same session: phase-4-26-gate.py is 100% inert in
production (g-115-3113) and its tests pass ONLY because every fixture hand-sets a
shape the real writer never produces. A detector with no adversarial test is
indistinguishable from a detector that always returns clean.

TWO HARNESSES, and the split is forced by the code rather than chosen:

  * SUBPROCESS (ratchet + CLI surface). `_ratchet()` imports `_paths.META_DIR` and
    `_fileops.locked_modify_yaml` INSIDE the function body. Python caches modules in
    sys.modules, so an in-process MIND_META override cannot take effect if anything
    already imported _paths — the env var must be read by a FRESH interpreter. Mirrors
    the sibling test_temp_citation_ratchet.py exactly.
  * IN-PROCESS (detection + conservatism + surface parsing). Everything below
    audit_line() keys off the module-level SCRIPTS_DIR constant, which has no CLI
    override, so synthetic wrappers require monkeypatching it. _fresh() loads a NEW
    module object per test so a patched SCRIPTS_DIR can never leak between tests.

POSITIVE CONTROLS ARE LOAD-BEARING (guard-1220). Every "is NOT flagged" assertion is
paired with an "IS flagged" one. Without the pair, a detector that silently returns
nothing — the exact regression this file guards — passes every conservatism test in
here. test_detects_unknown_flag and test_finding_survives_each_skip_neighbour are
those pairs; do not delete them as redundant.

STORAGE_BACKEND=local is pinned on every subprocess (guard-955 / rb-2983): under
own-cloud, OwnCloudBackend._s3_key derives from customer_prefix+env_id+filename and
NOT from the MIND_META tmp override, so a tmp write would collide on the production
S3 key.

Run: STORAGE_BACKEND=local py -3 -m pytest core/scripts/tests/test_skillmd_flag_audit.py -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "skillmd-flag-audit.py"


# ---------------------------------------------------------------- in-process


def _fresh():
    """Load a NEW module object so a patched SCRIPTS_DIR cannot leak between tests.

    The file name is hyphenated, so a plain `import` is impossible; and reusing one
    cached module across tests would make ordering significant — a test that patches
    SCRIPTS_DIR would silently change what a later test measures.
    """
    spec = importlib.util.spec_from_file_location("_sfa_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scripts(tmp: Path, files: dict[str, str]) -> Path:
    d = tmp / "scripts"
    d.mkdir(exist_ok=True)
    for name, body in files.items():
        (d / name).write_text(body, encoding="utf-8")
    return d


# ---- (2) DETECTION — the part that must not silently weaken -----------------


def test_detects_unknown_flag():
    """POSITIVE CONTROL for the whole file: a bad flag IS reported."""
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        m.SCRIPTS_DIR = _scripts(Path(t), {"one.sh": 'case "$1" in\n  --ok) ;;\nesac\n'})
        finding, skip = m.audit_line("Bash: one.sh --nope", {})
        assert skip is None, f"expected a finding, got skip={skip}"
        assert finding is not None, "a flag the wrapper does not accept MUST be reported"
        assert finding["unknown_flags"] == ["--nope"], finding
        assert finding["wrapper"] == "one.sh", finding


def test_accepted_flag_is_not_flagged():
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        m.SCRIPTS_DIR = _scripts(Path(t), {"one.sh": 'case "$1" in\n  --ok) ;;\nesac\n'})
        finding, skip = m.audit_line("Bash: one.sh --ok", {})
        assert finding is None, f"an accepted flag must never be flagged: {finding}"
        assert skip is None, skip


def test_universal_flags_never_flagged():
    """--json/--source/--agent are common plumbing; flagging them would be noise."""
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        m.SCRIPTS_DIR = _scripts(Path(t), {"one.sh": 'case "$1" in\n  --ok) ;;\nesac\n'})
        for flag in ("--json", "--output", "--source", "--agent", "--help"):
            finding, _ = m.audit_line(f"Bash: one.sh {flag}", {})
            assert finding is None, f"{flag} is universal and must not be flagged"


# ---- (3) THE CONSERVATISM CONTRACT -----------------------------------------
# Each skip is an honesty guarantee. Silently dropping one turns a skip into a
# FALSE POSITIVE, which costs the check its credibility — the failure mode the
# detector's own docstring calls out as worse than a miss.

CONSERVATISM = [
    ("multiple-wrappers-on-line", "Bash: one.sh --nope && two.sh --nope"),
    ("unvalidatable-wrapper", "Bash: efs-ssh.sh --anything"),
    ("documents-flag-absence", "the real flag is --ok; one.sh --nope does not exist"),
    ("unparseable-flag-surface", "Bash: bad.py --nope"),
    ("empty-flag-surface", "Bash: empty.sh --nope"),
]


def _conservatism_scripts(tmp: Path) -> Path:
    return _scripts(tmp, {
        "one.sh": 'case "$1" in\n  --ok) ;;\nesac\n',
        "two.sh": 'case "$1" in\n  --ok) ;;\nesac\n',
        "empty.sh": "echo hi\n",          # parses fine, but declares no flags
        "bad.py": "def (\n",              # SyntaxError => surface is None
        "efs-ssh.sh": 'case "$1" in\n  --x) ;;\nesac\n',   # in UNVALIDATABLE
    })


@pytest.mark.parametrize("expected_skip,line", CONSERVATISM,
                         ids=[c[0] for c in CONSERVATISM])
def test_conservatism_skips(expected_skip, line):
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        m.SCRIPTS_DIR = _conservatism_scripts(Path(t))
        finding, skip = m.audit_line(line, {})
        assert finding is None, f"{expected_skip} must NOT produce a finding: {finding}"
        assert skip == expected_skip, f"expected skip={expected_skip}, got {skip}"


def test_finding_survives_each_skip_neighbour():
    """POSITIVE CONTROL for the parametrized block above.

    Every case there asserts ABSENCE. A detector that returned (None, <reason>) for
    everything would pass all five. This proves the same fixture set still reports a
    genuine mismatch, so the five skips are discriminating rather than blanket.
    """
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        m.SCRIPTS_DIR = _conservatism_scripts(Path(t))
        finding, skip = m.audit_line("Bash: one.sh --nope", {})
        assert skip is None and finding is not None, (finding, skip)
        assert finding["unknown_flags"] == ["--nope"]


def test_unparseable_surface_is_never_a_finding():
    """`wrapper_surface` returning None means UNKNOWN, which must never read WRONG."""
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        m.SCRIPTS_DIR = _scripts(Path(t), {"bad.py": "def (\n"})
        assert m.wrapper_surface("bad.py", {}) is None
        finding, skip = m.audit_line("Bash: bad.py --whatever", {})
        assert finding is None and skip == "unparseable-flag-surface"


# ---- (4) BOTH .sh FLAG-DECLARATION FORMS ------------------------------------


def test_sh_case_arm_form():
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        d = _scripts(Path(t), {
            "caseform.sh": 'case "$1" in\n  --alpha) ;;\n  --beta|-b) ;;\n  --gamma=*) ;;\nesac\n'})
        assert m.sh_flags(d / "caseform.sh") == {"--alpha", "--beta", "-b", "--gamma"}


def test_sh_string_comparison_form():
    """heartbeat-tick.sh declares --bypass-state ONLY this way.

    Parsing case-arms alone reported both of its legitimate call sites as mismatches,
    so this form is not academic — it is the measured false-positive source.
    """
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        d = _scripts(Path(t), {
            "cmp.sh": 'if [ "${1:-}" != "--bypass-state" ]; then exit 1; fi\n'})
        assert m.sh_flags(d / "cmp.sh") == {"--bypass-state"}


def test_sh_comment_lines_do_not_register_flags():
    """A `# ... --flag ...` note must not make the parser claim the flag is accepted."""
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        d = _scripts(Path(t), {
            "commented.sh": "# --from-a-comment is only documentation\n"
                            'case "$1" in\n  --real) ;;\nesac\n'})
        assert m.sh_flags(d / "commented.sh") == {"--real"}


def test_refusal_arm_is_not_an_acceptance():
    """A case arm naming a flag in order to REJECT it must not read as accepting it.

    The detector's original consumer is a skill-file `Bash:` call site, so reporting a
    refused flag as accepted INVERTS the guard: aspirations-add-goal.sh's --title arm
    exists solely to refuse --title with an educational error, and the tool was telling
    the next LLM to type it. An over-report is worse in kind than an under-report — it
    makes the caller confident (g-115-3122 FIX 4).
    """
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        d = _scripts(Path(t), {
            "refuses.sh": 'case "$1" in\n'
                          '  --good) shift ;;\n'
                          '  --nope) echo "refused" >&2; exit 1 ;;\n'
                          'esac\n'})
        accepted, refused = m.sh_flag_surface(d / "refuses.sh")
        assert "--nope" not in accepted, "a refusal must never appear as accepted"
        assert "--good" in accepted
        assert "--nope" in refused
        # The compat wrapper must expose the SAME accepted set, since wrapper-surface.py
        # and the audit both read flags through it.
        assert m.sh_flags(d / "refuses.sh") == accepted


def test_help_flag_exiting_nonzero_is_still_accepted():
    """--help/-h legitimately exit non-zero; that is not a refusal.

    Measured false positive from FIX 4's first cut (platform-check.sh:70). Without the
    exemption the refusal count read 27 across 10 wrappers against a hand-verified
    ground truth of 1.
    """
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        d = _scripts(Path(t), {
            "helper.sh": 'case "$1" in\n'
                         '  --help|-h) usage; exit 1 ;;\n'
                         '  --real) shift ;;\n'
                         'esac\n'})
        accepted, refused = m.sh_flag_surface(d / "helper.sh")
        assert {"--help", "-h"} <= accepted
        assert not ({"--help", "-h"} & refused), "help flags must never be called refusals"


def test_unterminated_arm_is_unknown_not_refused():
    """No `;;` inside the window means UNKNOWN EXTENT — never classify from a neighbour.

    FIX 4's first cut walked a fixed 15-line window forward and ran past the arm's own
    terminator into the NEXT arm's `exit 2`, labelling arms REFUSED on evidence that
    belonged to a different arm. A saturated bound feeding a classifier does not merely
    under-count, it MISLABELS (guard-1590 applied to a classifier; guard-1205).
    """
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        # --first is UNTERMINATED; the only `exit 1` belongs to the arm BELOW it,
        # placed 12 lines down so it is well INSIDE the scan window. That
        # placement is the whole test: with the neighbour outside the window the
        # assertion passes for the wrong reason and pins nothing (the first cut
        # of this test used 60 pad lines and survived its own mutation probe).
        body = ('case "$1" in\n'
                '  --first)\n'
                + "    echo pad\n" * 10
                + '  --second) exit 1 ;;\n'
                  'esac\n')
        d = _scripts(Path(t), {"overrun.sh": body})
        accepted, refused = m.sh_flag_surface(d / "overrun.sh")
        assert "--first" not in refused, (
            "an arm whose extent could not be resolved must not be classified as refused")
        # The neighbour itself is genuinely a refusal — the fix must not silence
        # that too, or it trades a false positive for a false negative.
        assert "--second" in refused


def test_py_flags_ignore_docstring_mentions():
    """AST-based, so `add_argument` inside a docstring must not register a flag."""
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        d = _scripts(Path(t), {
            "p.py": '"""Docs mentioning add_argument("--ghost") in prose."""\n'
                    "import argparse\n"
                    "p = argparse.ArgumentParser()\n"
                    'p.add_argument("--real")\n'})
        assert m.py_flags(d / "p.py") == {"--real"}


# ---- (5) TRANSITIVE DELEGATION ---------------------------------------------


def test_transitive_delegation_unions_all_three_surfaces():
    """.sh -> .sh -> .py must union every hop.

    agent-aspirations-read.sh is the live example: it execs another .sh, so .py-only
    resolution left its surface empty and silently skipped 31 call sites.
    """
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        m.SCRIPTS_DIR = _scripts(Path(t), {
            "outer.sh": 'case "$1" in\n  --outer) ;;\nesac\n'
                        'exec bash "$(dirname "$0")/mid.sh" "$@"\n',
            "mid.sh": 'case "$1" in\n  --mid) ;;\nesac\nexec python3 inner.py "$@"\n',
            "inner.py": "import argparse\n"
                        "p = argparse.ArgumentParser()\n"
                        'p.add_argument("--inner")\n',
        })
        assert m.wrapper_surface("outer.sh", {}) == {"--outer", "--mid", "--inner"}
        # and a flag from the DEEPEST hop must not be reported against the outer one
        finding, skip = m.audit_line("Bash: outer.sh --inner", {})
        assert finding is None, f"forwarded flag must not be flagged: {finding}"


def test_mutual_delegation_terminates():
    """Cycle guard: a.sh <-> b.sh must return, not recurse forever.

    NOTE the FRESH `{}` cache on each call — that is what makes this a
    TERMINATION test and nothing more. The shared-cache truncation it cannot see
    is pinned by the next test; do not merge the two.
    """
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        m.SCRIPTS_DIR = _scripts(Path(t), {
            "a.sh": 'case "$1" in\n  --a) ;;\nesac\nbash b.sh "$@"\n',
            "b.sh": 'case "$1" in\n  --b) ;;\nesac\nbash a.sh "$@"\n',
        })
        assert m.wrapper_surface("a.sh", {}) == {"--a", "--b"}
        assert m.wrapper_surface("b.sh", {}) == {"--a", "--b"}


@pytest.mark.parametrize("order", [("a.sh", "b.sh"), ("b.sh", "a.sh")])
def test_shared_cache_does_not_memoize_cycle_truncation(order):
    """A cycle-truncated union must never be cached ( FIX 3).

    `main()` resolves every call site through ONE shared cache, which is the
    condition the bug needs and the condition test_mutual_delegation_terminates
    deliberately avoids. Resolving a.sh recurses into b.sh; b.sh's recursion back
    into a.sh correctly contributes nothing; b.sh then cached that truncated
    union as complete, so the NEXT top-level lookup of b.sh got {--b} alone and
    reported --a as unknown. False positive — the direction this detector exists
    to avoid.

    Parametrized over both traversal orders because the truncation lands on
    whichever node is entered SECOND, so a single order pins only half the fix.
    """
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        m.SCRIPTS_DIR = _scripts(Path(t), {
            "a.sh": 'case "$1" in\n  --a) ;;\nesac\nbash b.sh "$@"\n',
            "b.sh": 'case "$1" in\n  --b) ;;\nesac\nbash a.sh "$@"\n',
        })
        shared: dict = {}
        first, second = order
        assert m.wrapper_surface(first, shared) == {"--a", "--b"}
        assert m.wrapper_surface(second, shared) == {"--a", "--b"}, (
            f"{second} resolved from a shared cache lost the cycle partner's "
            f"flags — a truncated union was memoized as complete")


def test_unparseable_delegate_poisons_surface_to_none():
    """An unparseable hop makes the WHOLE surface unknown — never a partial surface.

    A partial surface would report every forwarded flag as unknown, which is the
    false-positive direction the detector is built to avoid.
    """
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        m.SCRIPTS_DIR = _scripts(Path(t), {
            "uses.sh": 'case "$1" in\n  --x) ;;\nesac\npython3 broken.py "$@"\n',
            "broken.py": "def (\n",
        })
        assert m.wrapper_surface("uses.sh", {}) is None


def test_bare_mention_in_comment_is_not_a_delegate():
    """`utilization-gate.sh` names tree.py only in a comment.

    Unioning a merely-MENTIONED script's surface is over-permissive: it hides real
    mismatches, and when the mentioned file is unparseable it manufactures skips.
    """
    with tempfile.TemporaryDirectory() as t:
        m = _fresh()
        d = _scripts(Path(t), {
            "mentions.sh": "# see helper.py for the real logic\n"
                           'case "$1" in\n  --own) ;;\nesac\n',
            "helper.py": "import argparse\n"
                         "p = argparse.ArgumentParser()\n"
                         'p.add_argument("--helper-only")\n',
        })
        m.SCRIPTS_DIR = d
        assert "helper.py" not in m.delegate_targets(d / "mentions.sh")
        assert m.wrapper_surface("mentions.sh", {}) == {"--own"}


# ---------------------------------------------------------------- subprocess


def _run(skills: Path, meta: Path, *extra, hard_gate=False, expect_rc=0, cwd=None):
    """Run the audit in a FRESH interpreter so MIND_META is read at import time."""
    env = os.environ.copy()
    env["MIND_META"] = str(meta)
    env["STORAGE_BACKEND"] = "local"        # guard-955 / rb-2983
    if hard_gate:
        env["VERIFY_LEARNING_DRIFT_HARD_GATE"] = "1"
    else:
        env.pop("VERIFY_LEARNING_DRIFT_HARD_GATE", None)
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--skills-dir", str(skills),
         "--output", "json", *extra],
        capture_output=True, text=True, encoding="utf-8", env=env,
        cwd=str(cwd) if cwd else None)
    assert r.returncode == expect_rc, (
        f"expected rc={expect_rc}, got {r.returncode}\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}")
    return json.loads(r.stdout) if r.stdout.strip() else {}


def _skills_with(tmp: Path, n_bad: int) -> Path:
    """A skills dir whose SKILL.md yields exactly n_bad findings.

    Deliberately references a REAL wrapper (wm-read.sh): the subprocess resolves
    against the real SCRIPTS_DIR, which has no CLI override.

    KNOWN COUPLING, recorded so a future failure here is not a mystery. This is the
    one place in this file that depends on the live tree, and it is forced rather
    than chosen — the in-process tests monkeypatch SCRIPTS_DIR, and a subprocess
    cannot. Three properties of wm-read.sh must hold for the counts above to mean
    what they say: it exists, its flag surface parses, and it rejects `--bogusN`.
    The third is safe by construction (no wrapper will ever declare those). The
    first two are not: if wm-read.sh is renamed, or its surface becomes
    unparseable, every ratchet test silently measures ZERO findings instead of
    n_bad, because an unknown surface is a SKIP, not a finding — so the failure
    arrives as a confusing `finding_count == 0` rather than a missing-file error.
    If these tests break together with no change to skillmd-flag-audit.py, probe
    `wrapper_surface("wm-read.sh", {})` first; a None there is the answer.
    """
    d = tmp / "skills" / "demo"
    d.mkdir(parents=True, exist_ok=True)
    lines = ["# Demo", ""]
    lines += [f"Bash: bash core/scripts/wm-read.sh --bogus{i}" for i in range(1, n_bad + 1)]
    (d / "SKILL.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp / "skills"


# ---- (1) RATCHET VERDICTS ---------------------------------------------------


def test_ratchet_seeds_then_stable():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        meta = tmp / "meta"
        meta.mkdir()
        skills = _skills_with(tmp, 1)
        first = _run(skills, meta, "--ratchet")
        assert first["verdict"] == "seeded", first
        assert first["baseline"] == 1, first
        second = _run(skills, meta, "--ratchet")
        assert second["verdict"] == "stable", second
        assert second["baseline"] == 1, second


def test_ratchet_regresses_and_never_raises_baseline():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        meta = tmp / "meta"
        meta.mkdir()
        assert _run(_skills_with(tmp, 1), meta, "--ratchet")["baseline"] == 1
        out = _run(_skills_with(tmp, 2), meta, "--ratchet")
        assert out["verdict"] == "regressed", out
        assert out["finding_count"] == 2, out
        assert out["baseline"] == 1, "baseline must NEVER rise on regression"


def test_ratchet_lowers_baseline_on_shrink():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        meta = tmp / "meta"
        meta.mkdir()
        assert _run(_skills_with(tmp, 2), meta, "--ratchet")["baseline"] == 2
        out = _run(_skills_with(tmp, 1), meta, "--ratchet")
        assert out["verdict"] == "ratcheted", out
        assert out["baseline"] == 1, out


def test_hard_gate_exits_1_only_on_regressed():
    """The tripwire: exit 1 on regressed, exit 0 on every other verdict.

    A gate that exits non-zero on ratcheted/stable would be reverted for noise; one
    that exits 0 on regressed is not a gate at all. Both directions are pinned.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        meta = tmp / "meta"
        meta.mkdir()
        # seed at 1, no hard gate yet
        _run(_skills_with(tmp, 1), meta, "--ratchet")
        # stable  -> 0
        _run(_skills_with(tmp, 1), meta, "--ratchet", hard_gate=True, expect_rc=0)
        # ratcheted (0 < 1) -> 0, and lowers the baseline to 0
        _run(_skills_with(tmp, 0), meta, "--ratchet", hard_gate=True, expect_rc=0)
        # regressed (2 > 0) -> 1
        out = _run(_skills_with(tmp, 2), meta, "--ratchet", hard_gate=True, expect_rc=1)
        assert out["verdict"] == "regressed", out


def test_ratchet_persists_under_the_shared_key():
    """Sibling ratchets share audit-baselines.yaml; the key must be namespaced."""
    import yaml
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        meta = tmp / "meta"
        meta.mkdir()
        _run(_skills_with(tmp, 1), meta, "--ratchet")
        data = yaml.safe_load((meta / "audit-baselines.yaml").read_text(encoding="utf-8"))
        assert "skillmd_flag_mismatches" in data, data
        entry = data["skillmd_flag_mismatches"]
        assert entry["baseline"] == 1
        assert entry["last_verdict"] == "seeded"
        assert entry["history"], "history must record the run"


# ---- (6) --skills-dir OUTSIDE THE REPO --------------------------------------


def test_skills_dir_outside_repo_does_not_crash():
    """`relative_to(PROJECT_ROOT)` raised ValueError for ANY external dir.

    That made the documented test override unusable, and it was found only because
    the branch matrix was actually exercised — the reason this file exists.
    """
    with tempfile.TemporaryDirectory() as t:          # /tmp is outside PROJECT_ROOT
        tmp = Path(t)
        meta = tmp / "meta"
        meta.mkdir()
        out = _run(_skills_with(tmp, 1), meta)
        assert out["finding_count"] == 1, out
        # the absolute-path fallback must be used, not a crash or a mangled path
        assert Path(out["findings"][0]["file"]).is_absolute(), out["findings"][0]


def test_plain_run_exit_codes():
    """Exit 1 when findings exist (for CI), 0 when clean — non-ratchet path."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        meta = tmp / "meta"
        meta.mkdir()
        env = os.environ.copy()
        env["MIND_META"] = str(meta)
        env["STORAGE_BACKEND"] = "local"

        def rc(n, output):
            r = subprocess.run(
                [sys.executable, str(SCRIPT), "--skills-dir", str(_skills_with(tmp, n)),
                 "--output", output],
                capture_output=True, text=True, encoding="utf-8", env=env)
            return r.returncode

        # text mode carries the CI signal
        assert rc(1, "text") == 1, "findings must exit 1 in text mode"
        assert rc(0, "text") == 0, "clean must exit 0"
        # json mode is for consumers and always exits 0 (documented behavior)
        assert rc(1, "json") == 0, "json mode exits 0 by design"


def test_show_skips_reports_counts():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        meta = tmp / "meta"
        meta.mkdir()
        d = tmp / "skills" / "demo"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "# Demo\n"
            "Bash: bash core/scripts/wm-read.sh --x && bash core/scripts/wm-set.sh --y\n",
            encoding="utf-8")
        out = _run(tmp / "skills", meta, "--show-skips")
        assert out["finding_count"] == 0, out
        assert out["skipped"].get("multiple-wrappers-on-line") == 1, out
