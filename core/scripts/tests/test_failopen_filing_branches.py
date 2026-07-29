#!/usr/bin/env python3
"""Static guards for the g-115-3575 defect class: fail-open-guarded FILING
branches that are structurally incapable of firing.

The class (generalized from g-115-3565): a branch that (a) only runs on a rare
failure condition and (b) is wrapped in a fail-open guard that swallows its exit
code. The guard makes it safe AND silent, so a wrong flag / unregistered prefix /
renamed script inside it never surfaces. Nothing exercises the branch, so nothing
notices it is dead.

The g-115-3575 sweep found 2 of 5 such branches dead:

  1. skill-quality-staleness-check.sh passed --title/--description/--priority to
     aspirations-add-goal.sh. Those are HARD-REJECTED (exit 2) -- goal fields go
     in the JSON body on stdin -- and `2>&1 | tail -3 || true` swallowed the
     rejection. Invoked live from aspirations-consolidate at every session end;
     it had never filed anything.
  2. iteration-close.sh's aspirations.jsonl corruption canary used the
     origin_signal "canary-fired:<basename>:<ts>". That prefix is not in the
     registry, so the origin-signal gate refused every payload, and
     `>/dev/null 2>&1` hid the reason behind a generic non-fatal WARN.

Both are STATICALLY detectable, which is the point of this file: these two checks
would each have caught their defect the day it was written, without needing the
rare condition to occur. Runtime-exercising the branches proved the repairs
(g-115-3583, g-115-3584 both landed and were retired); these tests keep them
repaired.
"""
import re
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = Path(__file__).resolve().parents[1]
PROJECT_ROOT = CORE_SCRIPTS.parents[1]

sys.path.insert(0, str(CORE_SCRIPTS))
from gates.origin_signal import ALLOWED_PREFIXES  # noqa: E402  (SSOT, never copied)

# guard-580: resolve bash explicitly - a bare 'bash' argv[0] hits System32 WSL
# on win32. BASH is computed once at helper-module import time.
sys.path.insert(0, str(TESTS_DIR))
from _bash_helpers import BASH  # noqa: E402

# Field-shaped flags aspirations-add-goal.sh rejects with exit 2. Kept in sync
# with the reject arm of its arg parser; the test below asserts that sync so a
# rename there fails HERE rather than silently weakening the check.
REJECTED_FIELD_FLAGS = (
    "--title", "--description", "--priority", "--status",
    "--participants", "--category", "--skill", "--asp-id", "--asp_id",
)


def _scan_roots():
    """Directories whose scripts get scanned.

    `core/scripts` is always in scope; the world script dir is added WHEN
    RESOLVABLE. World scripts call the same filing consumers (`alert-sweep.sh`,
    `ohs-husk-cluster-check.py` both invoke `aspirations-add-goal.sh`), and the
    sibling guard for the parse-error half of this class learned the hard way
    that a core-only scope skips the surface its canonical incidents happened on
    — the guard-1291 failure mode. The world dir is EXTERNAL and gitignored, so
    it is genuinely absent on a fresh clone or in CI; coverage then narrows to
    core, which is a supported configuration rather than masking.

    sys.path is restored afterwards: a bare insert leaves core/scripts on the
    path for the rest of the pytest process, where it can shadow modules for
    unrelated tests sharing the chunk.
    """
    roots = [CORE_SCRIPTS]
    saved = list(sys.path)
    try:
        sys.path.insert(0, str(CORE_SCRIPTS))
        from _paths import WORLD_DIR  # noqa: PLC0415

        if WORLD_DIR:
            world_scripts = Path(WORLD_DIR) / "scripts"
            if world_scripts.is_dir():
                roots.append(world_scripts)
    except Exception:
        pass
    finally:
        sys.path[:] = saved
    return roots


def _shell_scripts():
    out = []
    for root in _scan_roots():
        out.extend(sorted(root.glob("*.sh")))
    return out


def _python_scripts():
    out = []
    for root in _scan_roots():
        out.extend(sorted(root.glob("*.py")))
    return out


def _strip_comments(text):
    """Blank out `#` comment lines AND triple-quoted docstrings. Docstrings
    matter: `check-origin-signal-drift.py` documents this very defect class and
    quotes a placeholder signal in its module docstring, which a comment-only
    stripper reports as a live offender."""
    text = re.sub(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'',
                  lambda m: "\n" * m.group(0).count("\n"), text)
    return "\n".join(
        "" if ln.lstrip().startswith("#") else ln for ln in text.split("\n")
    )


def _logical_blocks(text):
    """Join backslash/pipe/&&/|| continuations so a multi-line pipeline reads as
    one command. Without this the canonical defect shape -- payload built into a
    var on one line, piped on the next, guard on a third -- is invisible."""
    raw = text.split("\n")
    out, i = [], 0
    while i < len(raw):
        start, buf = i + 1, raw[i]
        while i + 1 < len(raw):
            s = buf.rstrip()
            if s.endswith(("\\", "|", "&&", "||")):
                i += 1
                buf += " " + raw[i].strip()
                continue
            break
        out.append((start, buf))
        i += 1
    return out


def test_the_scan_actually_finds_something():
    """Non-vacuity. Every assertion below is of the form "no offenders found" --
    which a scan that silently matches NOTHING satisfies trivially, forever. Pin
    that the corpus is non-empty AND that the two predicates each still see live
    material, so a broken glob or a regex that stops matching fails HERE with a
    clear cause rather than turning the real checks green and hollow."""
    shells, pys = _shell_scripts(), _python_scripts()
    assert len(shells) > 50, f"shell corpus collapsed to {len(shells)} files"
    assert len(pys) > 50, f"python corpus collapsed to {len(pys)} files"

    filers = sum(
        1 for p in shells
        if "aspirations-add-goal" in _strip_comments(p.read_text(encoding="utf-8", errors="replace"))
    )
    assert filers >= 3, f"only {filers} scripts reference the goal-filer; predicate may be dead"

    lit = re.compile(r"""["']origin_signal["']\s*:\s*(?:f?["'])([^"'{]*)""")
    signals = 0
    for p in shells + pys:
        for m in lit.finditer(_strip_comments(p.read_text(encoding="utf-8", errors="replace"))):
            if re.match(r"^[a-z][\w-]*:", m.group(1).strip()):
                signals += 1
    assert signals >= 3, f"only {signals} decidable origin_signal literals; regex may be dead"


def test_reject_flag_list_matches_the_add_goal_parser():
    """If aspirations-add-goal.sh renames or drops a rejected flag, this file's
    constant must move with it -- otherwise the check below silently stops
    covering that flag (a vacuous test, the failure mode guard-1470 names)."""
    src = (CORE_SCRIPTS / "aspirations-add-goal.sh").read_text(encoding="utf-8")
    m = re.search(r"^\s*(--title(?:\|--[a-z_-]+)+)\)", src, re.M)
    assert m, "could not locate the field-flag reject arm in aspirations-add-goal.sh"
    parser_flags = set(m.group(1).split("|"))
    assert parser_flags == set(REJECTED_FIELD_FLAGS), (
        "REJECTED_FIELD_FLAGS drifted from aspirations-add-goal.sh's reject arm.\n"
        f"  parser: {sorted(parser_flags)}\n"
        f"  test:   {sorted(REJECTED_FIELD_FLAGS)}"
    )


def test_no_script_passes_rejected_field_flags_to_add_goal():
    """Defect 1. A call carrying --title/--description/--priority can only ever
    exit 2, so the branch containing it can never file a goal."""
    offenders = []
    for path in _shell_scripts():
        text = _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        for lineno, block in _logical_blocks(text):
            if "aspirations-add-goal" not in block:
                continue
            bad = [f for f in REJECTED_FIELD_FLAGS if re.search(rf"(?<![\w-]){re.escape(f)}(?![\w-])", block)]
            if bad:
                offenders.append(f"{path.name}:{lineno} passes {bad}")
    assert not offenders, (
        "aspirations-add-goal.sh rejects field-shaped flags with exit 2 -- goal "
        "fields go in the JSON body on stdin. These call sites can never file:\n  "
        + "\n  ".join(offenders)
    )


def test_every_origin_signal_literal_carries_a_registered_prefix():
    """Defect 2. An origin_signal whose prefix is not registered is refused by
    the origin-signal gate, so its branch can never file."""
    lit = re.compile(r"""["']origin_signal["']\s*:\s*(?:f?["'])([^"'{]*)""")
    offenders = []
    for path in _shell_scripts() + _python_scripts():
        if path.name == Path(__file__).name:
            continue
        text = _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        for m in lit.finditer(text):
            value = m.group(1).strip()
            # Only judge literals that actually begin with a prefix token; a
            # value opening on an interpolation is not statically decidable.
            if not value or not re.match(r"^[a-z][\w-]*:", value):
                continue
            if not any(value.startswith(p) for p in ALLOWED_PREFIXES if p.endswith(":")):
                lineno = text[: m.start()].count("\n") + 1
                offenders.append(f"{path.name}:{lineno} origin_signal={value!r}")
    assert not offenders, (
        "origin_signal literals whose prefix is NOT in gates/origin_signal.py "
        "ALLOWED_PREFIXES. The gate refuses these, so the branch cannot file:\n  "
        + "\n  ".join(offenders)
    )


def test_repaired_branches_surface_the_consumer_error():
    """Both defects hid behind a guard that discarded the consumer's stderr, so
    a gate refusal was indistinguishable from a dead daemon. The repairs capture
    stderr into the WARN; keep it that way -- this is what makes the next
    instance of this class self-reporting instead of silent."""
    for name, marker in (
        ("iteration-close.sh", "canary-Investigate goal-file failed"),
        ("skill-quality-staleness-check.sh", "skill-quality staleness goal-file failed"),
    ):
        text = (CORE_SCRIPTS / name).read_text(encoding="utf-8")
        idx = text.find(marker)
        assert idx != -1, f"{name}: expected WARN marker {marker!r} not found"
        line = text[text.rfind("\n", 0, idx) + 1 : text.find("\n", idx)]
        assert "_err" in line or "err}" in line, (
            f"{name}: the goal-file WARN no longer interpolates the captured "
            f"consumer error. Restore it -- a bare 'failed (non-fatal)' is what "
            f"hid both g-115-3575 defects.\n  line: {line.strip()}"
        )


def test_shell_scripts_still_parse():
    """Cheap backstop: a parse break inside a fail-open branch is the original
    g-115-3565 defect, and `bash -n` catches it without executing anything."""
    broken = []
    for path in (CORE_SCRIPTS / "iteration-close.sh",
                 CORE_SCRIPTS / "skill-quality-staleness-check.sh"):
        # Resolve bash EXPLICITLY via the shared helper (guard-580, g-115-725).
        # Two wrong forms were tried before this one, and both are traps:
        #   "/bin/bash" -- no such path on Windows, so CreateProcess dies
        #     WinError 2 at spawn, before parsing anything. This test therefore
        #     failed on every Windows box from the day it shipped.
        #   bare "bash"  -- resolves via System32 FIRST, which is the WSL
        #     launcher; on a box with a wedged LxssManager it hangs forever.
        #     It only appears to work here because conftest's _normalize_bash
        #     rewrites it at runtime -- a pytest-only safety net that does not
        #     exist for any other caller. The pre-commit gate refuses it.
        # BASH is computed once at import and honors AYOAI_SHELL / Git-Bash.
        r = subprocess.run([BASH, "-n", str(path)], capture_output=True, text=True)
        if r.returncode != 0:
            broken.append(f"{path.name}: {r.stderr.strip()}")
    assert not broken, "shell parse failure:\n  " + "\n  ".join(broken)


if __name__ == "__main__":
    import traceback
    failures = 0
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError:
            failures += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
