"""Tests for the verify-learning check registry ().

Every case here pins a defect that was MEASURED during the extraction, not one
imagined afterward. In order of how much they cost:

  * `load_registry` bound REGISTRY as a default argument, so patching the
    module constant silently read the real file — four deliberate corruptions
    all "verified OK" through that path (TestFixedPoint).
  * `raw` carries its own trailing newline; `add` omitted it and three lines
    concatenated into one (TestAdd::test_raw_carries_its_own_newline).
  * `extract --write` re-parsed the now-thin SKILL.md and would have replaced
    2,235 checks with 0 (TestShrinkGuard).
  * A section can exceed the injection ceiling on its own — 4T is 224 KB
    against 63,515 — so `show` has to window (TestPaging).
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]  # tests -> scripts -> core -> repo
SCRIPT = REPO / "core" / "scripts" / "verify-check-registry.py"


def load_module():
    spec = importlib.util.spec_from_file_location("vcr_under_test", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# A synthetic corpus in the real grammar: records at indent 3, a multi-line
# command body at indent 0, and a step header. Mirrors the shapes the live
# corpus actually contains.
CORPUS = """## Step 3: Evidence Check

   # A rationale comment (Section ZZ)
   Check: something is true
   Bash (named-check): py -3 -c "print('hi')"
   Bash: grep -c foo bar.txt
   Check: another thing
   Bash (multi-line): py -3 -c "
import sys
print('body at indent zero')
"
   Check: a third thing

## Step 4: Summary Report

   # Another section (Section YY)
   Check: step four check
"""


@pytest.fixture()
def mod():
    return load_module()


@pytest.fixture()
def registry(tmp_path, mod):
    """A written registry over CORPUS, with the module pointed at it."""
    blocks, _ = mod.parse(CORPUS)
    p = tmp_path / "reg.jsonl"
    p.write_text("".join(json.dumps(mod._slim(b), ensure_ascii=False) + "\n"
                         for b in blocks), encoding="utf-8")
    mod.REGISTRY = p
    return p


class TestRoundTrip:
    def test_parse_regenerate_is_byte_exact(self, mod):
        blocks, _ = mod.parse(CORPUS)
        assert mod.regenerate(blocks) == CORPUS

    def test_every_raw_ends_with_newline(self, mod):
        # The property regenerate() depends on: it joins raw values directly.
        blocks, _ = mod.parse(CORPUS)
        assert all(b["raw"].endswith("\n") for b in blocks)

    def test_indent_zero_is_a_body_bound_to_its_parent(self, mod):
        blocks, _ = mod.parse(CORPUS)
        bodies = [b for b in blocks if b["kind"] == "body"]
        assert bodies, "the multi-line command body must be classified as body"
        seqs = {b["seq"] for b in blocks}
        for b in bodies:
            assert b["parent_seq"] in seqs

    def test_registry_write_then_load_preserves_blocks(self, mod, registry):
        blocks, _ = mod.parse(CORPUS)
        assert mod.load_registry(registry) == blocks


class TestFixedPoint:
    """`verify` with no --against must be able to FAIL.

    It could not, for a while: load_registry's `path: Path = REGISTRY` default
    froze the constant at import, so every corruption below was scored against
    the real registry and passed.
    """

    class Args:
        against = None

    def run(self, mod, path, lines):
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        mod.REGISTRY = path
        return mod.cmd_verify(self.Args())

    def test_untouched_passes(self, mod, registry, capsys):
        lines = registry.read_text(encoding="utf-8").splitlines()
        assert self.run(mod, registry, lines) == 0
        assert "fixed point : OK" in capsys.readouterr().out

    def test_deindented_check_fails(self, mod, registry):
        lines = registry.read_text(encoding="utf-8").splitlines()
        for i, ln in enumerate(lines):
            o = json.loads(ln)
            if o["k"] == "check":
                o["raw"] = o["raw"].lstrip()  # indent 3 -> 0 reclassifies it
                lines[i] = json.dumps(o, ensure_ascii=False)
                break
        assert self.run(mod, registry, lines) == 1

    def test_deleted_middle_block_fails(self, mod, registry):
        lines = registry.read_text(encoding="utf-8").splitlines()
        assert self.run(mod, registry, lines[:4] + lines[5:]) == 1

    def test_tail_truncation_is_NOT_internally_detectable(self, mod, registry):
        """An honest limit, pinned so nobody mistakes the fixed point for more.

        Dropping the LAST block leaves a smaller registry that is still
        perfectly self-consistent — its seqs stay contiguous, so regenerate →
        parse reproduces it exactly. It is byte-identical to a registry that
        was always that size, and no internal invariant can tell them apart.
        The defenses against truncation are external and must stay: the shrink
        guard on `extract --write`, and git.
        """
        lines = registry.read_text(encoding="utf-8").splitlines()
        assert self.run(mod, registry, lines[:-1]) == 0

    def test_duplicated_seq_fails(self, mod, registry, capsys):
        lines = registry.read_text(encoding="utf-8").splitlines()
        assert self.run(mod, registry, lines + [lines[2]]) == 1
        assert "seq" in capsys.readouterr().out

    def test_load_registry_honours_an_explicit_path(self, mod, tmp_path):
        """The default-arg defect, pinned directly."""
        mod.REGISTRY = tmp_path / "does-not-exist.jsonl"
        other = tmp_path / "other.jsonl"
        blocks, _ = mod.parse(CORPUS)
        other.write_text("".join(json.dumps(mod._slim(b), ensure_ascii=False) + "\n"
                                 for b in blocks), encoding="utf-8")
        assert mod.load_registry(other) == blocks


class TestShrinkGuard:
    class Args:
        write = True
        source = None
        allow_shrink = False

    def test_refuses_a_rebuild_that_would_drop_records(self, mod, registry, tmp_path, capsys):
        thin = tmp_path / "thin.md"
        thin.write_text("## Step 3: Evidence Check\n\n   Check: only one\n", encoding="utf-8")
        a = self.Args()
        a.source = str(thin)
        assert mod.cmd_extract(a) == 1
        assert "REFUSING to write" in capsys.readouterr().err
        # and the registry is untouched
        assert len(mod.load_registry(registry)) == len(mod.parse(CORPUS)[0])

    def test_allow_shrink_bypasses(self, mod, registry, tmp_path):
        thin = tmp_path / "thin.md"
        thin.write_text("## Step 3: Evidence Check\n\n   Check: only one\n", encoding="utf-8")
        a = self.Args()
        a.source, a.allow_shrink = str(thin), True
        assert mod.cmd_extract(a) == 0
        assert len(mod.load_registry(registry)) < len(mod.parse(CORPUS)[0])


class TestPaging:
    """A slice must fit the injection ceiling, and pages must reassemble exactly."""

    class Args:
        section = "ZZ"
        step = None
        checks_only = False
        offset = 0
        max_bytes = 40000

    def test_pages_reassemble_byte_identically(self, mod, registry, capsys):
        a = self.Args()
        a.max_bytes = 10 ** 9
        mod.cmd_show(a)
        whole = capsys.readouterr().out

        parts, off, guard = [], 0, 0
        while True:
            b = self.Args()
            b.offset, b.max_bytes = off, 80  # tiny window forces many pages
            mod.cmd_show(b)
            cap = capsys.readouterr()
            parts.append(cap.out)
            if "MORE REMAIN" not in cap.err:
                break
            off = int(cap.err.split("--offset ")[1].split()[0])
            guard += 1
            assert guard < 200, "paging did not terminate"
        assert "".join(parts) == whole
        assert guard > 0, "the tiny window must actually have paged"

    def test_footer_goes_to_stderr_so_stdout_stays_verbatim(self, mod, registry, capsys):
        a = self.Args()
        a.max_bytes = 80
        mod.cmd_show(a)
        cap = capsys.readouterr()
        assert "MORE REMAIN" in cap.err
        assert "MORE REMAIN" not in cap.out

    def test_unknown_section_exits_nonzero(self, mod, registry, capsys):
        a = self.Args()
        a.section = "NOPE"
        assert mod.cmd_show(a) == 1


class TestAdd:
    class Args:
        section = "ZZ"
        check = "a newly added check"
        why = None
        dry_run = False

    def test_raw_carries_its_own_newline(self, mod, registry, capsys):
        """The measured defect: without it, added lines concatenate."""
        a = self.Args()
        a.why = "why this check exists"
        assert mod.cmd_add(a) == 0
        capsys.readouterr()
        blocks = mod.load_registry(registry)
        assert all(b["raw"].endswith("\n") for b in blocks)
        assert mod.cmd_verify(TestFixedPoint.Args()) == 0

    def test_insertion_renumbers_and_keeps_the_fixed_point(self, mod, registry, capsys):
        before = mod.load_registry(registry)
        n_before = sum(1 for b in before if b["kind"] in ("check", "bash_named"))
        assert mod.cmd_add(self.Args()) == 0
        capsys.readouterr()
        after = mod.load_registry(registry)
        n_after = sum(1 for b in after if b["kind"] in ("check", "bash_named"))
        assert n_after == n_before + 1
        seqs = [b["seq"] for b in after]
        assert seqs == sorted(set(seqs)), "seq must stay strictly increasing"
        known = set(seqs)
        assert all(b["parent_seq"] in known
                   for b in after if b.get("parent_seq") is not None), \
            "parent_seq must be shifted with its parent"
        assert mod.cmd_verify(TestFixedPoint.Args()) == 0

    def test_lands_in_the_requested_section(self, mod, registry, capsys):
        assert mod.cmd_add(self.Args()) == 0
        capsys.readouterr()
        added = [b for b in mod.load_registry(registry)
                 if b["kind"] == "check" and "newly added check" in b["raw"]]
        assert len(added) == 1
        assert added[0]["section"] == "ZZ"

    def test_dry_run_writes_nothing(self, mod, registry, capsys):
        before = registry.read_bytes()
        a = self.Args()
        a.dry_run = True
        assert mod.cmd_add(a) == 0
        assert "DRY RUN" in capsys.readouterr().out
        assert registry.read_bytes() == before

    def test_unknown_section_exits_nonzero_and_lists_known(self, mod, registry, capsys):
        a = self.Args()
        a.section = "NOPE"
        assert mod.cmd_add(a) == 1
        err = capsys.readouterr().err
        assert "unknown section" in err and "ZZ" in err


class TestSectionsIndex:
    def test_counts_agree_with_count_subcommand(self, mod, registry, capsys):
        class A:
            output = "json"
        mod.cmd_sections(A())
        idx = json.loads(capsys.readouterr().out)
        mod.cmd_count(object())
        cnt = json.loads(capsys.readouterr().out)
        assert idx["total_checks"] == cnt["corpus_checks"]


class TestLiveCorpus:
    """The live registry is the artifact everything else depends on."""

    def test_live_registry_round_trips(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "verify"],
                           capture_output=True, text=True, cwd=str(REPO))
        if "does not exist" in r.stderr:
            pytest.skip("registry not present on this box")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "fixed point : OK" in r.stdout

    def test_thin_skill_is_under_the_injection_ceiling(self):
        skill = REPO / ".claude" / "skills" / "verify-learning" / "SKILL.md"
        if not skill.is_file():
            pytest.skip("verify-learning SKILL.md not present on this box")
        n = len(skill.read_bytes())
        assert n < 63515, (
            f"verify-learning SKILL.md is {n:,} B, over the 63,515 B skill-injection "
            f"ceiling — checks past the cut are silently invisible (g-115-6689)")
