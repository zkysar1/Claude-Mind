""": temp-citation ratchet over the three uncovered durable stores.

The experience lane had an anti-orphan guard (`experience-content-path-no-temp`);
the knowledge tree, reasoning bank and guardrails had none. This pins the ratchet
that closes that gap.

The load-bearing test here is `test_count_is_box_independent`. The filing goal
measured DANGLING citations, but dangling-ness is a property of the box doing the
measuring — `agents/zeta/temp/x` exists on zeta's machine and nowhere else
(measured: 0 of 45 other-agent cited paths exist on the Studio host). A
dangling-based baseline would report a different number per machine, so the two
sides of a ratchet delta would not share a predicate (guard-1951). The shipped
metric counts CITATIONS, and that test is what stops a future edit from
"improving" it into a filesystem probe.

Run: STORAGE_BACKEND=local py -3 -m pytest core/scripts/tests/test_temp_citation_ratchet.py -q
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "temp-citation-ratchet.py"


def _mkworld(tmp: Path, tree_cites=(), rb_cites=(), guard_cites=(), sig_cites=()):
    """Build a minimal world with the four scanned stores."""
    world = tmp / "world"
    (world / "knowledge" / "tree").mkdir(parents=True)
    for i, cite in enumerate(tree_cites):
        (world / "knowledge" / "tree" / f"node-{i}.md").write_text(
            f"# Node {i}\n\nEvidence: {cite}\n", encoding="utf-8")
    if not tree_cites:
        # A clean node still makes the population non-empty, so a 0 result is a
        # real zero rather than the vacuous kind rb-245 warns about.
        (world / "knowledge" / "tree" / "clean.md").write_text(
            "# Clean\n\nNo citations here.\n", encoding="utf-8")

    def _jsonl(name, cites, prefix):
        rows = []
        for i, c in enumerate(cites):
            rows.append(json.dumps({"id": f"{prefix}-{i}", "content": f"see {c}"}))
        if not cites:
            rows.append(json.dumps({"id": f"{prefix}-clean", "content": "nothing"}))
        (world / name).write_text("\n".join(rows) + "\n", encoding="utf-8")

    _jsonl("reasoning-bank.jsonl", rb_cites, "rb")
    _jsonl("guardrails.jsonl", guard_cites, "guard")
    _jsonl("pattern-signatures.jsonl", sig_cites, "sig")
    return world


def _run(world: Path, meta: Path, extra=(), cwd=None):
    env = os.environ.copy()
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env["STORAGE_BACKEND"] = "local"
    env.pop("VERIFY_LEARNING_DRIFT_HARD_GATE", None)
    r = subprocess.run([sys.executable, str(SCRIPT), "--json", *extra],
                       capture_output=True, text=True, encoding="utf-8", env=env,
                       cwd=str(cwd) if cwd else None)
    assert r.returncode == 0, f"ratchet exited {r.returncode}: {r.stderr}"
    return json.loads(r.stdout)


def test_seeds_then_reports_stable():
    with tempfile.TemporaryDirectory(prefix="tcr_seed_") as d:
        tmp = Path(d)
        meta = tmp / "meta"
        meta.mkdir()
        world = _mkworld(tmp, tree_cites=["agents/alpha/temp/a.md"],
                         rb_cites=["agents/bravo/temp/b.md"])
        first = _run(world, meta)
        assert first["verdict"] == "seeded", first
        assert first["baseline"] == 2, first
        second = _run(world, meta)
        assert second["verdict"] == "stable", second
        assert second["baseline"] == 2, second


def test_growth_regresses_and_never_raises_baseline():
    with tempfile.TemporaryDirectory(prefix="tcr_grow_") as d:
        tmp = Path(d)
        meta = tmp / "meta"
        meta.mkdir()
        world = _mkworld(tmp, tree_cites=["agents/alpha/temp/a.md"])
        assert _run(world, meta)["baseline"] == 1
        # A NEW node cites a purgeable path — exactly what the guard must catch.
        (world / "knowledge" / "tree" / "new-node.md").write_text(
            "See agents/echo/temp/fresh.md\n", encoding="utf-8")
        out = _run(world, meta)
        assert out["verdict"] == "regressed", out
        assert out["current"]["total"] == 2, out
        assert out["baseline"] == 1, "baseline must NEVER rise on regression"


def test_shrink_ratchets_baseline_down():
    with tempfile.TemporaryDirectory(prefix="tcr_shrink_") as d:
        tmp = Path(d)
        meta = tmp / "meta"
        meta.mkdir()
        world = _mkworld(tmp, tree_cites=["agents/alpha/temp/a.md",
                                          "agents/bravo/temp/b.md"])
        assert _run(world, meta)["baseline"] == 2
        (world / "knowledge" / "tree" / "node-1.md").write_text(
            "# cleaned\n\nEvidence folded inline.\n", encoding="utf-8")
        out = _run(world, meta)
        assert out["verdict"] == "ratcheted", out
        assert out["baseline"] == 1, out


def test_count_is_box_independent():
    """The metric must NOT depend on whether the cited file exists locally.

    This is the design decision, not an implementation detail: dangling-ness
    varies per machine, so a filesystem probe here would make the baseline
    unusable across the fleet (guard-1951). Same world, same stores; the only
    difference is that the cited path is materialised on disk.
    """
    with tempfile.TemporaryDirectory(prefix="tcr_box_") as d:
        tmp = Path(d)
        meta = tmp / "meta"
        meta.mkdir()
        cited = "agents/alpha/temp/exists.md"
        world = _mkworld(tmp, tree_cites=[cited])
        # cwd is the tmp dir throughout, so the "materialised" file lands inside
        # the sandbox — this test must not create cross-agent paths in the repo.
        absent = _run(world, meta, extra=("--dry-run",), cwd=tmp)["current"]["total"]

        # Materialise the cited path relative to cwd, where a naive
        # os.path.exists() probe would find it.
        real = tmp / cited
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("now on disk\n", encoding="utf-8")
        present = _run(world, meta, extra=("--dry-run",), cwd=tmp)["current"]["total"]

        assert absent == present == 1, (
            f"count changed with on-disk presence ({absent} vs {present}) — the "
            "ratchet has become a dangling probe and its baseline is no longer "
            "comparable across boxes")


def test_vanished_store_holds_baseline_instead_of_ratcheting():
    """A store that VANISHES looks identical to a store that got CLEANED.

    Both drop the count, so ratcheting on a drop would bake a transient read
    failure into the baseline permanently — and the store's return would then
    read as a phantom regression. The baseline must be HELD.
    """
    with tempfile.TemporaryDirectory(prefix="tcr_vanish_") as d:
        tmp = Path(d)
        meta = tmp / "meta"
        meta.mkdir()
        world = _mkworld(tmp, tree_cites=["agents/alpha/temp/a.md"],
                         rb_cites=["agents/bravo/temp/b.md",
                                   "agents/echo/temp/c.md"])
        assert _run(world, meta)["baseline"] == 3

        (world / "reasoning-bank.jsonl").unlink()          # store vanishes
        out = _run(world, meta)
        assert out["verdict"] == "skipped", out
        assert out["baseline"] == 3, "baseline must be HELD, not lowered to 1"
        assert "reasoning-bank.jsonl" in out["message"], out

        # And the store's RETURN must not read as a regression — which is the
        # damage the hold prevents.
        (world / "reasoning-bank.jsonl").write_text(
            "\n".join([json.dumps({"id": "rb-0", "content": "see agents/bravo/temp/b.md"}),
                       json.dumps({"id": "rb-1", "content": "see agents/echo/temp/c.md"})]) + "\n",
            encoding="utf-8")
        back = _run(world, meta)
        assert back["verdict"] == "stable", f"phantom regression on store return: {back}"


def test_regression_still_reported_when_a_store_is_missing():
    """The hold is scoped to DROPS. More citations from fewer stores is
    genuinely worse, so it must still surface as a regression.

    What this actually pins is BRANCH ORDER, not the `cur < prior` conjunction.
    Measured: broadening the guard to `elif current["stores_missing"]:` leaves
    this test GREEN, because the `cur > prior` branch sits upstream and a
    regression never reaches the guard. The mutation it does catch is hoisting
    the missing-store branch ABOVE the regression branch — the plausible
    "simplification" a future editor would reach for. Do not read a green here
    as proof the conjunction is load-bearing; it is the ordering that is.
    """
    with tempfile.TemporaryDirectory(prefix="tcr_vanish_reg_") as d:
        tmp = Path(d)
        meta = tmp / "meta"
        meta.mkdir()
        world = _mkworld(tmp, tree_cites=["agents/alpha/temp/a.md"])
        assert _run(world, meta)["baseline"] == 1
        (world / "guardrails.jsonl").unlink()
        (world / "knowledge" / "tree" / "n2.md").write_text(
            "agents/echo/temp/x.md and agents/zeta/temp/y.md\n", encoding="utf-8")
        out = _run(world, meta)
        assert out["verdict"] == "regressed", out
        assert out["baseline"] == 1, "baseline must not rise"


def test_persist_failure_is_reported_as_error_not_as_the_computed_verdict():
    """A write that FAILED must not be reported with the verdict it would have
    written. `_modify` runs inside the locked write and fills in the verdict
    before the write happens, so a `setdefault` in the error path is a no-op and
    the tool reports "seeded"/"ratcheted" for a baseline it never persisted —
    stderr being the only contradicting signal, which no JSON consumer reads.
    (Adversarial fresh-eyes finding, g-115-3946.)

    ⚠ THIS TEST DOES NOT EXERCISE THAT DEFECT, and passes identically on
    the broken code. An unwritable baseline path fails at READ time, INSIDE
    locked_modify_yaml but BEFORE `_modify` is called -- so `captured` is
    still empty and `setdefault` behaves correctly. Measured 2026-08-27
    (g-115-4275): pre-patch experience-orphan-ratchet returned
    verdict='error' under exactly this condition. What this test DOES pin
    is the read-time-failure path, which is worth keeping.
    The defect itself is pinned by
    test_ratchet_persist_failure_family.py, which fails the write AFTER the
    modifier has run (7 of 8 family members fail there at pre-patch HEAD).
    """
    with tempfile.TemporaryDirectory(prefix="tcr_persistfail_") as d:
        tmp = Path(d)
        meta = tmp / "meta"
        meta.mkdir()
        world = _mkworld(tmp, tree_cites=["agents/alpha/temp/a.md"])
        # Make the baseline path unwritable by making it a DIRECTORY.
        (meta / "audit-baselines.yaml").mkdir()

        env = os.environ.copy()
        env["MIND_WORLD"] = str(world)
        env["MIND_META"] = str(meta)
        env["STORAGE_BACKEND"] = "local"
        r = subprocess.run([sys.executable, str(SCRIPT), "--json"],
                           capture_output=True, text=True, encoding="utf-8", env=env)
        out = json.loads(r.stdout)
        assert out["verdict"] == "error", (
            f"a failed persist reported verdict={out['verdict']!r} — the caller "
            "will record a baseline write that never happened")
        assert out["baseline"] is None, out
        assert "FAILED" in out["message"], out


def test_empty_population_skips_rather_than_passing_at_zero():
    """A world with no readable node or row must SKIP, not report a clean 0."""
    with tempfile.TemporaryDirectory(prefix="tcr_empty_") as d:
        tmp = Path(d)
        meta = tmp / "meta"
        meta.mkdir()
        world = tmp / "world"
        (world / "knowledge" / "tree").mkdir(parents=True)
        out = _run(world, meta)
        assert out["verdict"] == "skipped", out
        assert "nothing measured" in out["message"], out


def test_pattern_signatures_store_is_scanned():
    """The filing goal measured pattern-signatures at 0 and asked that it be
    stated rather than re-measured. Scanned means a citation there is caught."""
    with tempfile.TemporaryDirectory(prefix="tcr_sig_") as d:
        tmp = Path(d)
        meta = tmp / "meta"
        meta.mkdir()
        world = _mkworld(tmp, sig_cites=["agents/zeta/temp/sig-evidence.json"])
        out = _run(world, meta, extra=("--dry-run",))
        assert out["current"]["breakdown"]["pattern-signatures"] == 1, out


def test_same_path_cited_by_two_records_counts_twice():
    """The unit is the (record, path) PAIR. A new record citing an
    already-cited path is a new latent orphan; a distinct-path count would
    silently absorb it."""
    with tempfile.TemporaryDirectory(prefix="tcr_pair_") as d:
        tmp = Path(d)
        meta = tmp / "meta"
        meta.mkdir()
        same = "agents/alpha/temp/shared.md"
        world = _mkworld(tmp, tree_cites=[same, same])
        out = _run(world, meta, extra=("--dry-run",))
        assert out["current"]["total"] == 2, out
