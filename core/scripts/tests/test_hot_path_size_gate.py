"""Tests for core/scripts/hot-path-size-gate.py + core/githooks/commit-msg ().

The gate's whole value is DIRECTION: a hot-path file may shrink, never grow, and
a shrink must tighten the cap for the next commit. So the load-bearing cases are
the polarity pins — `decide()` is PURE (no I/O) and is branch-tested directly —
and the tighten-on-shrink pin, which grows a file back to its ORIGINAL size
after a diet commit and expects a refusal.

Two shapes, deliberately:
  - PURE: decide(), glob_to_regex(), parse_override().
  - PRODUCTION-SHAPE: a tmp git repo whose `core/scripts` is a SYMLINK to the
    real one (so the gate imports `_paths`/`_fileops` exactly as in production)
    and whose `core/githooks/commit-msg` is a byte copy of the real hook, wired
    via `core.hooksPath`. Commits are made with real `git commit`, so what is
    pinned is the hook contract itself — including that a PATHSPEC commit is
    judged on the temporary index git hands the hook (GIT_INDEX_FILE), and that
    a MERGE commit is not gated. MIND_WORLD/MIND_META point at tmp dirs and
    STORAGE_BACKEND=local (guard-955), so the override ledger and the
    audit-baselines ratchet land in tmp.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parent.parent
_ROOT = _SCRIPTS.parent.parent
SCRIPT = _SCRIPTS / "hot-path-size-gate.py"
HOOK = _ROOT / "core" / "githooks" / "commit-msg"
sys.path.insert(0, str(_SCRIPTS))


def _load():
    spec = importlib.util.spec_from_file_location("hpsg", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gate():
    return _load()


# ─── PURE ────────────────────────────────────────────────────────────────────

def test_decide_polarity(gate):
    assert gate.decide(1001, 1000, 8192) == ("grew", 1000)
    assert gate.decide(1000, 1000, 8192) == ("ok", 1000)
    assert gate.decide(999, 1000, 8192) == ("ok", 1000)
    # a NEW file is capped by new_file_cap, not by HEAD
    assert gate.decide(8193, None, 8192) == ("new_over_cap", 8192)
    assert gate.decide(8192, None, 8192) == ("ok", 8192)


def test_decide_ceiling_tier(gate):
    """The SECOND rule (): a ceiling, not a ratchet.

    Below the line a file grows freely — a ratchet across 120 skills would
    generate constant override noise for changes that cost nothing. Above it,
    only GROWTH is refused.

    THE LAST TWO CASES ARE THE LOAD-BEARING ONES. Sixteen skills were already
    over the line the day this shipped, holding 62% of all skill bytes. If an
    over-ceiling file could not be committed at all, every one of them would be
    frozen — including against the very extraction that fixes them (g-115-6689
    has to rewrite a 1.2 MB SKILL.md down). So over-ceiling SHRINK and
    over-ceiling FLAT must both pass, or the tier blocks its own remedy.
    """
    C = 65536
    # under the ceiling: growth is free, and HEAD is irrelevant
    assert gate.decide(C - 1, C - 500, 8192, ceiling=C) == ("ok", C)
    assert gate.decide(C, None, 8192, ceiling=C) == ("ok", C)      # new, exactly at
    # a NEW file may not be born over the line — there is no HEAD to shrink from
    assert gate.decide(C + 1, None, 8192, ceiling=C) == ("new_over_cap", C)
    # already over: GROWTH refused …
    assert gate.decide(200_000, 199_999, 8192, ceiling=C) == ("grew_over_ceiling", C)
    # … but shrinking and holding flat must BOTH pass, or the fix is blocked
    assert gate.decide(199_999, 200_000, 8192, ceiling=C) == ("ok", C)
    assert gate.decide(200_000, 200_000, 8192, ceiling=C) == ("ok", C)


def test_ceiling_and_ratchet_disagree_on_the_same_numbers(gate):
    """POSITIVE CONTROL for the two-tier split.

    Both tests above could pass with `ceiling` quietly ignored and everything
    falling through to the ratchet — `load_budget` dropped the key exactly that
    way during development, and the only visible symptom was a total reading
    `on-demand 0 B`. This pins that the two rules produce DIFFERENT verdicts on
    identical inputs, so an ignored ceiling cannot pass silently.
    """
    grew_under_ceiling = (5000, 4000, 8192)
    assert gate.decide(*grew_under_ceiling) == ("grew", 4000)              # ratchet: refused
    assert gate.decide(*grew_under_ceiling, ceiling=65536) == ("ok", 65536)  # ceiling: allowed


def _load_sets(gate, tmp_path, sets):
    (tmp_path / "core" / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "core" / "config" / "hot-path-budget.yaml").write_text(
        yaml.safe_dump({"version": 1, "sets": sets}), encoding="utf-8")
    return gate.load_budget(tmp_path)


def test_ceiling_set_owns_the_new_file_bound(gate, tmp_path):
    """ONE KNOB, NOT TWO THAT SILENTLY DISAGREE (found in review of 37bc8a671).

    decide()'s ceiling branch never consults new_file_cap — the ceiling bounds
    new and existing files alike. So a ceiling set declaring a DIFFERENT
    new_file_cap had that value accepted, validated, and then ignored: measured,
    `ceiling 65536 + new_file_cap 8192` admitted a brand-new 65,536 B file. The
    loader now refuses the conflict instead of silently preferring one.
    """
    # the dead-parameter proof: new_file_cap is not consulted on the ceiling path
    assert gate.decide(8193, None, 8192, ceiling=65536) == ("ok", 65536)
    assert gate.decide(65537, None, 8192, ceiling=65536) == ("new_over_cap", 65536)

    # omitted -> defaults to the ceiling
    b = _load_sets(gate, tmp_path, [{"name": "x", "paths": ["a/*.md"], "ceiling": 65536}])
    assert b["sets"][0]["new_file_cap"] == 65536

    # equal -> accepted (the shape the real budget used before it was trimmed)
    b = _load_sets(gate, tmp_path,
                   [{"name": "x", "paths": ["a/*.md"], "ceiling": 4096, "new_file_cap": 4096}])
    assert b["sets"][0]["ceiling"] == 4096

    # conflicting -> refused, loudly, naming both numbers
    with pytest.raises(ValueError) as e:
        _load_sets(gate, tmp_path,
                   [{"name": "x", "paths": ["a/*.md"], "ceiling": 65536, "new_file_cap": 8192}])
    assert "65536" in str(e.value) and "8192" in str(e.value)

    # a RATCHET set still requires new_file_cap — the relaxation is ceiling-only
    with pytest.raises(ValueError):
        _load_sets(gate, tmp_path, [{"name": "x", "paths": ["a/*.md"]}])


def test_glob_star_does_not_cross_slash(gate):
    rx = gate.glob_to_regex(".claude/rules/*.md")
    assert rx.match(".claude/rules/foo.md")
    assert not rx.match(".claude/rules/sub/foo.md")
    rx2 = gate.glob_to_regex(".claude/skills/aspirations*/SKILL.md")
    assert rx2.match(".claude/skills/aspirations/SKILL.md")
    assert rx2.match(".claude/skills/aspirations-precheck/SKILL.md")
    assert not rx2.match(".claude/skills/aspirations-precheck/other.md")
    assert not rx2.match(".claude/skills/verify-learning/SKILL.md")
    assert gate.glob_to_regex("CLAUDE.md").match("CLAUDE.md")
    assert not gate.glob_to_regex("CLAUDE.md").match("docs/CLAUDE.md")


def test_parse_override(gate):
    assert gate.parse_override("feat: x\n\nsize-budget-override: a real reason here\n")[0] == "a real reason here"
    assert gate.parse_override("feat: x\n\nSize-Budget-Override:   spaced justification  \n")[0] == "spaced justification"
    assert gate.parse_override("feat: x\n\nno trailer at all\n") == (None, "")
    # a commented-out trailer (git comment line) does not count
    assert gate.parse_override("feat: x\n# size-budget-override: in a comment\n")[0] is None
    just, note = gate.parse_override("feat: x\n\nsize-budget-override: short\n")
    assert just is None and "too short" in note


def test_real_budget_parses_and_covers_the_hot_path(gate):
    budget = gate.load_budget(_ROOT)
    for p in ("CLAUDE.md", ".claude/rules/verify-before-assuming.md",
              ".claude/skills/aspirations-precheck/SKILL.md", ".claude/skills/aspirations/SKILL.md",
              ".claude/skills/worker-loop/SKILL.md", ".claude/skills/boot/SKILL.md",
              ".claude/skills/prime/SKILL.md", ".claude/skills/respond/SKILL.md",
              "core/config/aspirations-loop-digest.md"):
        assert gate.set_for(p, budget) is not None, p
    for p in ("core/config/conventions/board.md",
              "core/config/rationale/deadman-switch.md", "core/scripts/aspirations.py"):
        assert gate.set_for(p, budget) is None, p


# ─── ALWAYS-LOADED INSTRUCTION CEILING () ──────────────────────────

def test_instruction_chars_counts_like_the_notice(gate):
    """Calibrated against Claude Code's notice: the body after front matter, in
    UTF-16 code units, with every newline counted as CRLF."""
    assert gate.instruction_chars("abc") == 3
    assert gate.instruction_chars("a\nb\n") == 6                      # 4 chars + 2 newlines as CRLF
    assert gate.instruction_chars("a\r\nb\r\n") == 6                  # a CRLF blob is not double-counted
    assert gate.instruction_chars("---\ndescription: \"x\"\n---\nbody") == 4
    assert gate.instruction_chars("\U0001F600") == 2                  # astral char: 2 UTF-16 units
    assert gate.instruction_chars("—") == 1                      # em dash: 1 char, not 3 UTF-8 bytes
    # `paths:` front matter makes a rule on-demand: not always-loaded, not counted
    assert gate.instruction_chars("---\npaths: [\"src/**\"]\n---\nbody") is None
    assert gate.instruction_chars("---\ndescription: x\npaths:\n  - src/**\n---\nbody") is None
    # a `paths:` line in the BODY is prose, not front matter
    assert gate.instruction_chars("body\npaths: x\n") == 16


def _load_raw(gate, root, data):
    (root / "core" / "config").mkdir(parents=True, exist_ok=True)
    (root / "core" / "config" / "hot-path-budget.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return gate.load_budget(root)


def test_instruction_ceiling_survives_the_loader(gate, tmp_path):
    # load_budget REBUILDS its structures from explicit keys, so a key it does not
    # carry is dropped and the whole rule goes silently inert (the `ceiling` trap).
    b = _load_raw(gate, tmp_path, {**BUDGET, "instruction_ceiling": {"chars": 500, "paths": ["CLAUDE.md"]}})
    assert b["instruction_ceiling"]["chars"] == 500
    assert b["instruction_ceiling"]["patterns"][0].match("CLAUDE.md")
    assert _load_raw(gate, tmp_path, BUDGET)["instruction_ceiling"] is None       # optional
    for bad in ({"chars": 0, "paths": ["CLAUDE.md"]}, {"chars": 500, "paths": []},
                {"chars": "150k", "paths": ["CLAUDE.md"]}, ["CLAUDE.md"]):
        with pytest.raises(ValueError):
            _load_raw(gate, tmp_path, {**BUDGET, "instruction_ceiling": bad})


def test_real_budget_declares_the_instruction_ceiling(gate):
    ic = gate.load_budget(_ROOT)["instruction_ceiling"]
    assert ic is not None and ic["chars"] == 150000
    covers = lambda p: any(rx.match(p) for rx in ic["patterns"])
    for p in ("CLAUDE.md", ".claude/CLAUDE.md", ".claude/rules/self.md", ".claude/rules/sub/nested.md"):
        assert covers(p), p
    for p in (".claude/skills/respond/SKILL.md", "core/config/conventions/board.md", "docs/CLAUDE.md"):
        assert not covers(p), p


def test_hot_skills_keep_the_ratchet_and_on_demand_skills_get_the_ceiling(gate):
    """FIRST-MATCH ORDERING IS THE CONTRACT ().

    `.claude/skills/*/SKILL.md` matches EVERY skill, the hot ones included. It
    is placed LAST in the budget so the hot-path sets claim their members first
    and keep the stricter RATCHET rule (no growth at all). Move that set up and
    every loop skill silently converts to a 64 KB CEILING — free growth below
    the line, on the exact files that load on every iteration. Nothing else in
    the suite would notice: sizes stay legal, the gate stays green, and the
    corpus total the ratchet watches simply drifts upward.

    So this pins the DISCRIMINATION, not just the coverage. `ceiling is None`
    is the tell for "ratchet-governed".
    """
    budget = gate.load_budget(_ROOT)
    for p in ("CLAUDE.md", ".claude/rules/verify-before-assuming.md",
              ".claude/skills/aspirations/SKILL.md", ".claude/skills/worker-loop/SKILL.md",
              ".claude/skills/boot/SKILL.md", ".claude/skills/prime/SKILL.md",
              ".claude/skills/respond/SKILL.md", "core/config/aspirations-loop-digest.md"):
        s = gate.set_for(p, budget)
        assert s is not None and s.get("ceiling") is None, \
            f"{p} must stay RATCHET-governed, got ceiling={s and s.get('ceiling')}"

    # The seven files this tier newly governs — nothing constrained them before.
    for p in (".claude/skills/verify-learning/SKILL.md", ".claude/skills/tree/SKILL.md",
              ".claude/skills/fresh-eyes-review/SKILL.md", ".claude/skills/start/SKILL.md",
              ".claude/skills/reflect-on-outcome/SKILL.md"):
        s = gate.set_for(p, budget)
        assert s is not None, f"{p} is ungoverned — the on-demand tier does not reach it"
        assert s["ceiling"] == 65536, f"{p} should carry the injection ceiling, got {s}"

    # A forged skill is caught by the same glob — the tier is about the
    # INJECTION cost, which does not care who authored the skill.
    s = gate.set_for(".claude/skills/some-forged-skill/SKILL.md", budget)
    assert s is not None and s["ceiling"] == 65536


def test_hook_is_wired_and_executable_in_git():
    text = HOOK.read_text(encoding="utf-8")
    assert "hot-path-size-gate.py" in text and "--commit-msg-file" in text
    mode = subprocess.run(["git", "ls-files", "-s", "core/githooks/commit-msg"], cwd=_ROOT,
                          capture_output=True, text=True).stdout.split()
    # An untracked file (first run before the commit lands) has no index mode.
    if mode:
        assert mode[0] == "100755", "commit-msg must be committed executable — git skips non-exec hooks"
    installer = (_ROOT / "core/scripts/install-git-hooks.sh").read_text(encoding="utf-8")
    assert "commit-msg" in installer, "install-git-hooks.sh must restore +x on commit-msg too"


# ─── PRODUCTION-SHAPE ────────────────────────────────────────────────────────

BUDGET = {
    "version": 1,
    "override_trailer": "size-budget-override:",
    "sets": [
        {"name": "rules", "paths": [".claude/rules/*.md"], "new_file_cap": 2000},
        {"name": "digest", "paths": ["core/config/loop-digest.md"], "new_file_cap": 5000},
    ],
}


def _git(repo, *args, env=None, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


@pytest.fixture()
def repo(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlink unavailable")
    root = tmp_path / "repo"
    (root / "core" / "githooks").mkdir(parents=True)
    (root / "core" / "config").mkdir(parents=True)
    (root / ".claude" / "rules").mkdir(parents=True)
    try:
        os.symlink(_SCRIPTS, root / "core" / "scripts", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot symlink core/scripts into the tmp repo")
    shutil.copyfile(HOOK, root / "core" / "githooks" / "commit-msg")
    os.chmod(root / "core" / "githooks" / "commit-msg", 0o755)
    (root / "core" / "config" / "hot-path-budget.yaml").write_text(yaml.safe_dump(BUDGET), encoding="utf-8")

    world = tmp_path / "world"
    meta = tmp_path / "meta"
    world.mkdir()
    meta.mkdir()
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("GIT_"):
            env.pop(k)
    env.update({
        "STORAGE_BACKEND": "local",
        "MIND_WORLD": str(world),
        "MIND_META": str(meta),
        "MIND_AGENT": "gatetest",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig-empty"),
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    (tmp_path / "gitconfig-empty").write_text("", encoding="utf-8")

    _git(root, "init", "-q", "-b", "main", env=env)
    _git(root, "config", "user.name", "gate test", env=env)
    _git(root, "config", "user.email", "gate@test.local", env=env)
    _git(root, "config", "commit.gpgsign", "false", env=env)
    _git(root, "config", "core.hooksPath", "core/githooks", env=env)

    (root / ".claude" / "rules" / "a.md").write_text("x" * 1000, encoding="utf-8")
    (root / "b.txt").write_text("b\n", encoding="utf-8")
    (root / ".gitignore").write_text("core/scripts\n", encoding="utf-8")
    _git(root, "add", "-A", env=env)
    _git(root, "commit", "-q", "-m", "seed", env=env)
    return {"root": root, "env": env, "world": world, "meta": meta}


def _write(repo, rel, size, ch="x"):
    p = repo["root"] / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(ch * size, encoding="utf-8")


def _commit(repo, msg, *pathspec):
    _git(repo["root"], "add", "-A", env=repo["env"])
    args = ["commit", "-q", "-m", msg]
    if pathspec:
        args += ["--", *pathspec]
    return _git(repo["root"], *args, env=repo["env"], check=False)


def test_refuses_growth_allows_shrink_and_tightens_the_cap(repo):
    _write(repo, ".claude/rules/a.md", 1010)
    r = _commit(repo, "grow a little")
    assert r.returncode != 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "REFUSED" in out and ".claude/rules/a.md" in out and "1,000 → 1,010" in out
    assert "size-budget-override:" in out and "core/config/rationale/" in out
    # the index is left intact — a refusal is not a reset
    assert ".claude/rules/a.md" in _git(repo["root"], "diff", "--cached", "--name-only", env=repo["env"]).stdout

    _write(repo, ".claude/rules/a.md", 700)
    r = _commit(repo, "diet")
    assert r.returncode == 0, r.stdout + r.stderr

    # TIGHTEN-ON-SHRINK: back to the ORIGINAL 1000 is now growth (cap is 700 at HEAD)
    _write(repo, ".claude/rules/a.md", 1000)
    r = _commit(repo, "regrow to old size")
    assert r.returncode != 0
    assert "700 → 1,000" in (r.stdout + r.stderr)

    # equal size passes; a text change without growth is fine
    _write(repo, ".claude/rules/a.md", 700, ch="y")
    assert _commit(repo, "same size, new text").returncode == 0


def _add_ceiling_set(repo, ceiling=2000):
    """Append an on-demand ceiling set to the tmp repo's budget, LAST.

    Mirrors the real config's ordering constraint: the ceiling glob is broad and
    must not out-match the stricter sets above it.
    """
    b = dict(BUDGET)
    b["sets"] = list(BUDGET["sets"]) + [{
        "name": "on-demand-skills",
        "paths": [".claude/skills/*/SKILL.md"],
        "ceiling": ceiling,
        "new_file_cap": ceiling,
    }]
    (repo["root"] / "core" / "config" / "hot-path-budget.yaml").write_text(
        yaml.safe_dump(b), encoding="utf-8")


def test_ceiling_tier_end_to_end_through_the_real_hook(repo):
    """PRODUCTION SHAPE for the ceiling tier ().

    `decide()` is pure and unit-tested above, but g-115-4695 is the standing
    lesson that a green unit matrix says nothing about the shape production
    actually takes — that gate's unit tests passed while a real command walked
    through a hole in it. So this drives the REAL commit-msg hook with REAL git
    commits, and pins the two behaviours that make this tier different from the
    ratchet next to it.
    """
    # Seed a skill ALREADY over the line, before the set exists to govern it —
    # reproducing the real situation exactly: 16 skills were over the ceiling on
    # the day it was introduced, and none of them could have been born under it.
    _write(repo, ".claude/skills/big/SKILL.md", 5000)
    assert _commit(repo, "seed an oversized skill").returncode == 0
    _add_ceiling_set(repo)
    assert _commit(repo, "introduce the on-demand ceiling").returncode == 0

    # 1. GROWTH on an already-over-ceiling file is refused …
    _write(repo, ".claude/skills/big/SKILL.md", 5001)
    r = _commit(repo, "grow the oversized skill")
    assert r.returncode != 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "injection ceiling" in out and "may shrink, not grow" in out
    assert "5,000 → 5,001" in out

    # 2. … and SHRINKING it is allowed. THIS is the case that keeps the tier
    #    from blocking its own remedy: every over-ceiling file must stay
    #    committable downward, or the extraction that fixes it cannot land.
    _write(repo, ".claude/skills/big/SKILL.md", 4000)
    assert _commit(repo, "diet the oversized skill").returncode == 0, "the FIX was blocked"

    # 3. BELOW the ceiling, growth is free — the property that separates this
    #    tier from the ratchet beside it. A ratchet across 120 skills would
    #    refuse every ordinary edit that added a line, and the override noise
    #    would train readers to bypass the gate that also guards the hot path.
    _write(repo, ".claude/skills/small/SKILL.md", 500)
    assert _commit(repo, "new small skill").returncode == 0
    _write(repo, ".claude/skills/small/SKILL.md", 1900)
    assert _commit(repo, "grow it, still under the ceiling").returncode == 0, \
        "growth BELOW the ceiling must be free — a ratchet here would make 120 " \
        "skills un-editable without an override"

    # 4. Crossing the line from below is refused.
    _write(repo, ".claude/skills/small/SKILL.md", 2100)
    r = _commit(repo, "cross the ceiling")
    assert r.returncode != 0
    assert "injection ceiling" in (r.stdout + r.stderr)

    # 5. The override trailer still works on this tier.
    _write(repo, ".claude/skills/small/SKILL.md", 2100)
    r = _commit(repo, "cross the ceiling\n\nsize-budget-override: deliberate, tracked by a goal")
    assert r.returncode == 0, r.stdout + r.stderr


def test_explain_is_tier_aware(repo):
    """`--explain` must answer for the tier the file is actually in.

    FOUND BY USING IT (g-115-6690): for the first hour of the ceiling tier this
    path still printed the RATCHET framing for every file — reporting a Tier-2
    skill's cap as its own current size, i.e. "cannot grow", while it had 40 KB
    of headroom. Every test was green; `--explain` is a display path and nothing
    pinned it. It now derives its verdict by asking `decide()` whether one more
    byte is legal, so the output cannot drift from the engine again.
    """
    _write(repo, ".claude/skills/roomy/SKILL.md", 500)
    _write(repo, ".claude/skills/over/SKILL.md", 5000)
    assert _commit(repo, "seed skills").returncode == 0
    _add_ceiling_set(repo)
    assert _commit(repo, "add ceiling set").returncode == 0

    def explain(path):
        r = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo["root"]),
                            "--explain", path], capture_output=True, text=True, env=repo["env"])
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout

    under = explain(".claude/skills/roomy/SKILL.md")
    assert "CEILING" in under and "headroom" in under and "may grow" in under, under
    assert "1,500 B of headroom" in under, under          # 2,000 ceiling − 500

    over = explain(".claude/skills/over/SKILL.md")
    assert "CEILING" in over and "OVER" in over and "REFUSED" in over, over

    hot = explain(".claude/rules/a.md")
    assert "RATCHET" in hot and "REFUSED" in hot, hot     # ratchet: no growth at all
    assert "CEILING" not in hot, hot

    assert "not budgeted" in explain("core/scripts/aspirations.py")


def test_new_file_cap_and_non_budgeted_files(repo):
    _write(repo, ".claude/rules/new-rule.md", 2500)
    r = _commit(repo, "new big rule")
    assert r.returncode != 0
    assert "NEW at 2,500 B > new_file_cap 2,000" in (r.stdout + r.stderr)
    _write(repo, ".claude/rules/new-rule.md", 1900)
    assert _commit(repo, "new small rule").returncode == 0
    # anything outside the sets is never gated, however big
    _write(repo, "core/config/conventions/huge.md", 200000)
    assert _commit(repo, "convention growth is free").returncode == 0


def test_override_trailer_allows_and_writes_ledger(repo):
    _write(repo, ".claude/rules/a.md", 1042)
    r = _commit(repo, "grow with reason\n\nsize-budget-override: imperative the loop must see every turn")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OVERRIDE accepted" in (r.stdout + r.stderr)
    ledger = repo["world"] / "override-bypass-ledger.jsonl"
    assert ledger.exists(), "override must be audited to world/override-bypass-ledger.jsonl"
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["gate"] == "hot-path-size-gate"
    assert row["justification"] == "imperative the loop must see every turn"
    assert row["agent"] == "gatetest"
    assert row["context"]["net_bytes"] == 42
    assert row["context"]["files"][0]["path"] == ".claude/rules/a.md"
    assert row["context"]["files"][0]["kind"] == "grew"
    assert "slots_filled" not in row     # single-gate record shape (gate-overrides.md)
    # a too-short justification is NOT an override
    _write(repo, ".claude/rules/a.md", 1050)
    r = _commit(repo, "grow again\n\nsize-budget-override: meh")
    assert r.returncode != 0 and "too short" in (r.stdout + r.stderr)


def test_pathspec_commit_is_seen_as_committed(repo):
    # Growth is STAGED in the working index, but the commit is pathspec-limited to
    # b.txt — git hands the hook a temporary index without a.md, and the gate must
    # judge THAT (this is the iteration-commit.sh shape: `commit -F - -- <paths>`).
    _write(repo, ".claude/rules/a.md", 1300)
    (repo["root"] / "b.txt").write_text("b2\n", encoding="utf-8")
    r = _commit(repo, "only b", "b.txt")
    assert r.returncode == 0, r.stdout + r.stderr
    # the growth is still staged; committing IT is refused
    r = _commit(repo, "now a", ".claude/rules/a.md")
    assert r.returncode != 0 and "1,000 → 1,300" in (r.stdout + r.stderr)


def test_rename_keeps_old_head_size_as_cap(repo):
    root, env = repo["root"], repo["env"]
    _git(root, "mv", ".claude/rules/a.md", ".claude/rules/a-renamed.md", env=env)
    r = _git(root, "commit", "-q", "-m", "rename only", env=env, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    _git(root, "mv", ".claude/rules/a-renamed.md", ".claude/rules/a-again.md", env=env)
    _write(repo, ".claude/rules/a-again.md", 1500)
    _git(root, "add", "-A", env=env)
    r = _git(root, "commit", "-q", "-m", "rename and grow", env=env, check=False)
    assert r.returncode != 0
    # cap came from the OLD path's HEAD size (1000), not from new_file_cap (2000)
    assert "1,000 → 1,500" in (r.stdout + r.stderr)


def test_rename_into_the_hot_path_takes_new_file_cap(repo):
    # A 3 KB file that was never hot is `git mv`-ed into .claude/rules — it is NEW
    # to the hot path, so new_file_cap (2000) applies, not its old HEAD size.
    root, env = repo["root"], repo["env"]
    (root / "docs").mkdir()
    (root / "docs" / "big.md").write_text("z" * 3000, encoding="utf-8")
    assert _commit(repo, "a big non-hot doc").returncode == 0
    _git(root, "mv", "docs/big.md", ".claude/rules/big.md", env=env)
    r = _git(root, "commit", "-q", "-m", "smuggle it into the rules", env=env, check=False)
    assert r.returncode != 0
    assert "NEW at 3,000 B > new_file_cap 2,000" in (r.stdout + r.stderr)


def test_merge_commit_is_not_gated(repo):
    root, env = repo["root"], repo["env"]
    _git(root, "checkout", "-q", "-b", "feature", env=env)
    _write(repo, ".claude/rules/a.md", 1200)
    r = _commit(repo, "grow on branch\n\nsize-budget-override: branch-side deliberate growth")
    assert r.returncode == 0, r.stdout + r.stderr
    _git(root, "checkout", "-q", "main", env=env)
    (root / "b.txt").write_text("main moved\n", encoding="utf-8")
    assert _commit(repo, "main advances").returncode == 0
    r = _git(root, "merge", "--no-ff", "--no-edit", "feature", env=env, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    log = _git(root, "log", "--oneline", "-1", env=env).stdout
    assert "Merge" in log
    assert (root / ".claude/rules/a.md").stat().st_size == 1200


def test_budget_unreadable_fails_open(repo):
    (repo["root"] / "core/config/hot-path-budget.yaml").write_text("sets: {not: a list}\n", encoding="utf-8")
    _write(repo, ".claude/rules/a.md", 1100)
    r = _commit(repo, "grow with broken registry")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN: budget unreadable" in (r.stdout + r.stderr)


def test_check_reports_and_ratchets(repo):
    root, env, meta = repo["root"], repo["env"], repo["meta"]

    def check(*extra):
        return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(root), "--check", *extra],
                              capture_output=True, text=True, env=env)
    r = check()
    assert r.returncode == 0 and "PASS:" in r.stdout and "seeded" in r.stdout, r.stdout + r.stderr
    base = yaml.safe_load((meta / "audit-baselines.yaml").read_text(encoding="utf-8"))
    assert base["hot_path_total_bytes"]["baseline"] == 1000
    assert base["hot_path_total_bytes"]["polarity"] == "lower_is_better"

    _write(repo, ".claude/rules/a.md", 800)
    assert _commit(repo, "diet").returncode == 0
    r = check()
    assert "ratcheted" in r.stdout and "800 B" in r.stdout, r.stdout

    _write(repo, ".claude/rules/a.md", 900)
    assert _commit(repo, "regrow\n\nsize-budget-override: measured regrowth for the test").returncode == 0
    r = check()
    assert r.returncode == 0 and "FAIL:" in r.stdout and "GREW" in r.stdout, r.stdout
    r = check("--hard-gate")
    assert r.returncode == 1
    # the baseline never moved the wrong way
    base = yaml.safe_load((meta / "audit-baselines.yaml").read_text(encoding="utf-8"))
    assert base["hot_path_total_bytes"]["baseline"] == 800
    assert base["hot_path_total_bytes"]["last_verdict"] == "regressed"
    r = check("--no-ratchet", "--json")
    data = json.loads(r.stdout)
    assert data["total_bytes"] == 900 and data["ratchet"] is None


# ─── INSTRUCTION CEILING: git I/O on EVERY box (no hook, no symlink) ─────────
# The hook-driven tests above skip wherever a symlink cannot be made (Windows
# without Developer Mode), so the corpus arm's git reads are also pinned here,
# against a plain repo, where nothing can skip them.

_INSTRUCTION_PATHS = ["CLAUDE.md", ".claude/rules/*.md", ".claude/rules/**/*.md"]


@pytest.fixture()
def plain_repo(tmp_path, monkeypatch):
    for k in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(k)
    (tmp_path / "gitconfig-empty").write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig-empty"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "plain"
    (root / ".claude" / "rules").mkdir(parents=True)
    for args in (("init", "-q", "-b", "main"), ("config", "user.name", "t"),
                 ("config", "user.email", "t@t.local"), ("config", "commit.gpgsign", "false")):
        _git(root, *args)
    return root


def _stage_commit(root, msg="c"):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--no-verify", "-m", msg)


def _corpus_row(gate, root, budget):
    violations, checked = gate.evaluate(root, budget)
    rows = [c for c in checked if c["set"] == "instruction-ceiling"]
    return (rows[0] if rows else None), violations


def test_instruction_ceiling_git_io_on_every_box(gate, plain_repo):
    root = plain_repo
    (root / "CLAUDE.md").write_bytes(b"a\nb\n")                                   # 6
    (root / ".claude/rules/r.md").write_bytes(b"---\ndescription: d\n---\nxyz")    # 3
    scoped = b"---\npaths: [\"src/**\"]\n---\n" + b"z" * 500                       # not counted
    (root / ".claude/rules/scoped.md").write_bytes(scoped)
    (root / "notes.md").write_bytes(b"q" * 999)                                  # not an instruction path
    budget = _load_raw(gate, root, {**BUDGET, "instruction_ceiling":
                                    {"chars": 10, "paths": _INSTRUCTION_PATHS}})
    _stage_commit(root)
    ic = budget["instruction_ceiling"]
    assert gate.instruction_total(root, "HEAD", ic) == (9, 2)

    # the INDEX is read, never the working tree — that is what a pathspec commit commits
    (root / ".claude/rules/r.md").write_bytes(b"---\ndescription: d\n---\n" + b"y" * 50)
    assert gate.instruction_total(root, "", ic) == (9, 2)
    _git(root, "add", "-A")
    assert gate.instruction_total(root, "", ic) == (56, 2)
    row, violations = _corpus_row(gate, root, budget)
    assert (row["head_bytes"], row["staged_bytes"], row["kind"], row["unit"]) == \
        (9, 56, "instructions_over_ceiling", "chars")
    assert row in violations
    _stage_commit(root, "grow")

    # no instruction path staged: the arm is not consulted at all
    (root / "notes.md").write_bytes(b"q" * 5)
    _git(root, "add", "-A")
    assert _corpus_row(gate, root, budget)[0] is None
    _stage_commit(root, "unrelated")

    # over the ceiling, a SHRINK is still allowed
    (root / ".claude/rules/r.md").write_bytes(b"---\ndescription: d\n---\n" + b"y" * 40)
    _git(root, "add", "-A")
    row, _ = _corpus_row(gate, root, budget)
    assert (row["head_bytes"], row["staged_bytes"], row["kind"]) == (56, 46, "ok")
    _stage_commit(root, "shrink")

    # dropping `paths:` front matter: the FILE shrinks, the always-loaded total grows
    (root / ".claude/rules/scoped.md").write_bytes(b"z" * 500)
    _git(root, "add", "-A")
    row, violations = _corpus_row(gate, root, budget)
    assert (row["head_bytes"], row["staged_bytes"], row["kind"]) == (46, 546, "instructions_over_ceiling")
    assert [v["kind"] for v in violations] == ["instructions_over_ceiling"]    # the ratchet saw nothing


def test_check_reports_the_instruction_ceiling_as_corpus_state(gate, plain_repo):
    root = plain_repo
    (root / ".claude/rules/a.md").write_bytes(b"x" * 1000)

    def set_ceiling(chars):
        _load_raw(gate, root, {**BUDGET, "instruction_ceiling": {"chars": chars, "paths": _INSTRUCTION_PATHS}})
        _stage_commit(root, f"ceiling {chars}")

    def check(*extra):
        return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(root), "--check", "--no-ratchet",
                               *extra], capture_output=True, text=True)
    set_ceiling(900)
    r = check()
    assert r.returncode == 0 and "FAIL:" in r.stdout, r.stdout + r.stderr
    assert "always-loaded instructions 1,000 chars in 1 files, OVER the 900-char ceiling" in r.stdout
    assert check("--hard-gate").returncode == 1
    assert json.loads(check("--json").stdout)["instructions"] == {
        "chars": 1000, "files": 1, "ceiling": 900, "over": True}
    set_ceiling(5000)
    r = check("--hard-gate")
    assert r.returncode == 0 and "PASS:" in r.stdout and "within the 5,000-char ceiling" in r.stdout, r.stdout


# ─── INSTRUCTION CEILING through the REAL hook (skips without symlinks) ──────

def _set_instruction_ceiling(repo, chars):
    b = dict(BUDGET)
    b["instruction_ceiling"] = {"chars": chars, "paths": _INSTRUCTION_PATHS}
    (repo["root"] / "core" / "config" / "hot-path-budget.yaml").write_text(yaml.safe_dump(b), encoding="utf-8")
    r = _commit(repo, f"budget: instruction ceiling {chars}")     # the budget is not an instruction file
    assert r.returncode == 0, r.stdout + r.stderr


def test_instruction_ceiling_closes_the_new_rule_door(repo):
    """Every new rule here is under new_file_cap (2,000), so the per-file ratchet
    passes each one — only the corpus ceiling can refuse. That is the door
    (a NEW always-loaded rule) this arm exists to close."""
    _set_instruction_ceiling(repo, 3000)                           # seed: a.md = 1,000 chars
    _write(repo, ".claude/rules/new-1.md", 1900)                   # total 2,900: under
    r = _commit(repo, "first new rule")
    assert r.returncode == 0, r.stdout + r.stderr

    _write(repo, ".claude/rules/new-2.md", 1500)                   # total 4,400: over AND grew
    r = _commit(repo, "second new rule")
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "always-loaded instructions (3 files)  2,900 → 4,400 chars" in out
    assert "`paths:` front matter" in out and "size-budget-override:" in out

    scoped = "---\npaths: [\"src/**\"]\n---\n" + "x" * 1500
    (repo["root"] / ".claude/rules/new-2.md").write_text(scoped, encoding="utf-8")
    r = _commit(repo, "scoped rule loads on demand")
    assert r.returncode == 0, r.stdout + r.stderr

    _write(repo, ".claude/rules/new-2.md", 1500)                   # unscoped: file shrinks, total grows
    r = _commit(repo, "unscope it")
    assert r.returncode != 0 and "2,900 → 4,400 chars" in (r.stdout + r.stderr)
    (repo["root"] / ".claude/rules/new-2.md").write_text(scoped, encoding="utf-8")

    # rules nest, and `*` in the per-file `rules` set does not cross `/`
    _write(repo, ".claude/rules/sub/deep.md", 1200)
    r = _commit(repo, "nested rule")
    assert r.returncode != 0 and "2,900 → 4,100 chars" in (r.stdout + r.stderr)


def test_instruction_ceiling_override_is_ledgered_in_chars(repo):
    _set_instruction_ceiling(repo, 1500)
    _write(repo, ".claude/rules/new.md", 700)                      # total 1,700: over and grew
    r = _commit(repo, "needed now\n\nsize-budget-override: an always-on imperative that must ship today")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "OVERRIDE accepted" in out and "+700 chars" in out
    rows = [json.loads(l) for l in (repo["world"] / "override-bypass-ledger.jsonl")
            .read_text(encoding="utf-8").splitlines() if l.strip()]
    files = rows[-1]["context"]["files"]
    assert [f["kind"] for f in files] == ["instructions_over_ceiling"] and files[0]["unit"] == "chars"
    assert rows[-1]["context"]["net_bytes"] == 0                   # chars never leak into the byte total
