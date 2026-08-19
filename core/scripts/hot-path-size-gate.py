#!/usr/bin/env python3
"""hot-path-size-gate — the always-loaded prose surface may not grow ().

WHAT IS GATED
-------------
The "hot path" is every file that loads on EVERY turn (CLAUDE.md, the unscoped
`.claude/rules/*.md`) or on EVERY loop iteration (the aspirations* skills,
worker-loop, boot / prime / respond, the loop digest). The set is declared in
`core/config/hot-path-budget.yaml` — this script never hardcodes a path.

THE RULE, AND WHY THE CAP IS HEAD
---------------------------------
For a budgeted file the cap is its size AT HEAD: a commit may leave it equal or
smaller, never larger. That makes the ratchet automatic — every diet commit
tightens the cap the moment it lands, on every box, with no registry number to
edit and nothing that can drift out of sync (a per-file cap stored in YAML
would have to be re-written by the very hook that cannot reliably stage a
file into a pathspec commit). A file that is NEW at HEAD is capped by its
set's `new_file_cap`. A rename keeps the OLD path's HEAD size as its cap.

WHY A commit-msg HOOK AND NOT A pre-commit GATE
------------------------------------------------
The sanctioned bypass is a commit-message TRAILER — `size-budget-override:
<why>` — because the justification then lives in `git log` forever, travels
through promotion, and can be counted by anyone with the repo. A pre-commit
hook runs BEFORE the message exists (`-F -` is read afterwards), so it cannot
see a trailer; commit-msg receives the message file as $1 and, for a pathspec
commit, inherits GIT_INDEX_FILE pointing at the temporary index — so
`git diff --cached` inside it sees exactly what the commit will contain.
The hook chain in core/githooks/pre-commit is untouched.

Every accepted override is ALSO appended to world/override-bypass-ledger.jsonl
under `gate: hot-path-size-gate` (the daemon-gate record shape from
core/config/conventions/gate-overrides.md — no `slots_filled`), so the weekly
count is one grep. Growth via override moves HEAD, so the cap follows it: an
override permanently loosens that file's cap by the bytes it added. That is
deliberate — a file that stays "over budget" would demand a trailer on every
later touch and drown the ledger in noise — and it is exactly why the ledger
count and the `hot_path_total_bytes` ratchet exist.

FAIL-OPEN SURFACES (never wedge a commit on plumbing)
------------------------------------------------------
  * MERGE_HEAD present (a merge commit): skipped. A merge combines commits that
    were each gated on their own box; gating the merge would refuse every
    fleet pull the moment any box overrode.
  * budget file missing / unparseable: WARN and allow — `--check` reports it,
    and a broken registry must not stop the whole fleet committing.
  * ledger write failure on an override: WARN and allow — the trailer in the
    commit message is the durable record; the ledger is the index.

MODES
-----
  (hook)   --commit-msg-file <path>   the commit-msg hook shape; exit 1 = refuse
  --check                              HEAD-based report of every budgeted file
                                       + ratchet `hot_path_total_bytes` in
                                       meta/audit-baselines.yaml (lower_is_better).
                                       Prints one PASS:/FAIL: line. --no-ratchet
                                       reads only; --hard-gate exits 1 on FAIL.
  --explain <path>                     what cap this path would get right now.

Tests: core/scripts/tests/test_hot_path_size_gate.py (refuse-growth,
tighten-on-shrink, new-file cap, rename, override->ledger, merge skip,
pathspec-commit index visibility, trailer parsing).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

BUDGET_REL = Path("core/config/hot-path-budget.yaml")
GATE_ID = "hot-path-size-gate"
DEFAULT_TRAILER = "size-budget-override:"
MIN_JUSTIFICATION = 8
RATCHET_KEY = "hot_path_total_bytes"
# Tier 2 gets its OWN key so tier 1's series stays comparable across the
# 2026-08-18 split (). Never widen RATCHET_KEY's population instead.
ONDEMAND_RATCHET_KEY = "on_demand_skill_bytes"
TAG = "[hot-path-size-gate]"


# ─── budget ──────────────────────────────────────────────────────────────────

def glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """`*`/`?` never cross a `/`; `**` does; no wildcard = literal path."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def load_budget(repo: Path) -> dict:
    """Parse + validate the registry. Raises ValueError with a one-line reason."""
    import yaml  # local: keep the hook's happy path import-light
    p = repo / BUDGET_REL
    if not p.exists():
        raise ValueError(f"{BUDGET_REL} missing")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    sets = data.get("sets")
    if not isinstance(sets, list) or not sets:
        raise ValueError(f"{BUDGET_REL}: `sets` must be a non-empty list")
    compiled = []
    for s in sets:
        name = s.get("name") if isinstance(s, dict) else None
        paths = s.get("paths") if isinstance(s, dict) else None
        cap = s.get("new_file_cap") if isinstance(s, dict) else None
        if not name or not isinstance(paths, list) or not paths:
            raise ValueError(f"{BUDGET_REL}: set needs `name` and non-empty `paths`")
        # `ceiling` is OPTIONAL and selects the second tier's rule ().
        # It MUST be carried through explicitly: this loader rebuilds each set
        # from a fixed key list rather than copying the dict, so a key added to
        # the YAML and not added here is silently dropped and its whole tier is
        # INERT — the config reads correct, the gate reads correct, and nothing
        # fires. Measured during authoring: the first run reported the entire
        # corpus as tier 1 and `on-demand 0 B`.
        ceiling = s.get("ceiling")
        if ceiling is not None and (not isinstance(ceiling, int) or ceiling <= 0):
            raise ValueError(f"{BUDGET_REL}: set {name!r} `ceiling` must be a positive int")
        # ONE KNOB, NOT TWO SILENTLY-DISAGREEING ONES. decide()'s ceiling branch
        # never consults new_file_cap — the ceiling bounds new and existing files
        # alike — so a ceiling set declaring a DIFFERENT new_file_cap would have
        # that value accepted, validated, and then ignored. Measured: ceiling
        # 65536 with new_file_cap 8192 admits a brand-new 65,536 B file. Refuse
        # the conflict at load time rather than silently preferring one
        # (communication-clarity rule 5: fail visibly, never fall back quietly),
        # and let a ceiling set omit new_file_cap entirely.
        if ceiling is not None:
            if cap is None:
                cap = ceiling
            elif cap != ceiling:
                raise ValueError(
                    f"{BUDGET_REL}: set {name!r} sets ceiling={ceiling} and "
                    f"new_file_cap={cap}. A ceiling set bounds new files by its "
                    f"ceiling, so new_file_cap is not consulted — omit it, or "
                    f"make the two equal. (If you genuinely want new files held "
                    f"below the ceiling, that is a second policy and needs its "
                    f"own set.)")
        if not isinstance(cap, int) or cap <= 0:
            raise ValueError(f"{BUDGET_REL}: set {name!r} needs a positive int `new_file_cap`")
        compiled.append({"name": name, "patterns": [glob_to_regex(x) for x in paths],
                         "globs": list(paths), "new_file_cap": cap, "ceiling": ceiling})
    trailer = str(data.get("override_trailer") or DEFAULT_TRAILER)
    return {"sets": compiled, "trailer": trailer}


def set_for(path: str, budget: dict):
    for s in budget["sets"]:
        if any(rx.match(path) for rx in s["patterns"]):
            return s
    return None


# ─── the decision (pure) ─────────────────────────────────────────────────────

def decide(staged_size: int, head_size, new_file_cap: int, ceiling=None):
    """(kind, cap). kind ∈ {'ok', 'grew', 'new_over_cap', 'grew_over_ceiling'}.

    Kept free of I/O so the polarity can be pinned directly (the same reason
    context-diet-report.decide and reducer_self_fence.decide are pure): a gate
    that quietly compared the wrong way would still print, still exit 0, and
    look healthy while every diet gain regrew underneath it.

    TWO TIERS WITH DELIBERATELY DIFFERENT RULES (g-115-6690):

    * RATCHET (ceiling=None) — the hot path. Cap IS the size at HEAD, so NO
      growth is allowed at all and every shrink tightens the cap by itself.
      Correct for prose paid on every turn of every agent.

    * CEILING — on-demand skills. A skill is paid only when INJECTED, so the
      binding constraint is not "never grow", it is "must fit in one
      injection". Below the ceiling a skill may grow freely; that freedom is
      the point, because a ratchet over 120 skills would generate constant
      override noise for changes that cost nothing.

    The ceiling branch's third case is what makes the tier usable rather than a
    wedge: a file ALREADY over the ceiling may shrink or stay flat, and is
    refused only if it GROWS. Without that, every commit touching an
    over-ceiling file — including the extraction commits that fix it — would
    need an override, and the gate would punish exactly the work it exists to
    provoke.
    """
    if ceiling is not None:
        if staged_size <= ceiling:
            return ("ok", ceiling)
        if head_size is None:
            return ("new_over_cap", ceiling)
        return ("grew_over_ceiling" if staged_size > head_size else "ok", ceiling)
    if head_size is None:
        return ("new_over_cap" if staged_size > new_file_cap else "ok", new_file_cap)
    return ("grew" if staged_size > head_size else "ok", head_size)


# ─── override trailer ────────────────────────────────────────────────────────

def parse_override(message: str, trailer: str = DEFAULT_TRAILER):
    """Return (justification | None, note). Comment lines (`#`) are ignored —
    the hook sees the raw message file, before git's cleanup strips them."""
    rx = re.compile(r"^\s*" + re.escape(trailer) + r"\s*(.*?)\s*$", re.IGNORECASE)
    for line in message.splitlines():
        if line.startswith("#"):
            continue
        m = rx.match(line)
        if not m:
            continue
        just = m.group(1)
        if len(just) < MIN_JUSTIFICATION:
            return None, (f"trailer found but the justification is too short "
                          f"({len(just)} chars; need >= {MIN_JUSTIFICATION})")
        return just, ""
    return None, ""


# ─── git ─────────────────────────────────────────────────────────────────────

def _git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def in_merge(repo: Path) -> bool:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                       capture_output=True, text=True)
    return r.returncode == 0


def blob_size(repo: Path, spec: str):
    r = subprocess.run(["git", "-C", str(repo), "cat-file", "-s", spec],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def staged_changes(repo: Path):
    """[(status, old_path, new_path)] for staged A/C/M/R entries. Honors the
    GIT_INDEX_FILE git exports to hooks, so a pathspec commit is seen as git
    will commit it, not as the working index."""
    out = _git(repo, "-c", "core.quotePath=false", "diff", "--cached", "--name-status", "-M",
               "--diff-filter=ACMR")
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            rows.append((parts[0], parts[1], parts[1]))
        elif len(parts) == 3:              # R100\told\tnew  /  C100\told\tnew
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def evaluate(repo: Path, budget: dict):
    """Return (violations, checked): each entry a dict with path/set/staged/head/cap/kind."""
    checked, violations = [], []
    for status, old, new in staged_changes(repo):
        s = set_for(new, budget)
        if s is None:
            continue
        staged = blob_size(repo, f":{new}")
        if staged is None:
            continue
        # A rename/copy keeps the OLD path's HEAD size as its cap — but only when
        # the old path was itself hot. A file renamed INTO the hot path from
        # outside is NEW to the hot path and takes new_file_cap, or a 40 KB
        # convention could become a rule by `git mv` with no gate at all.
        renamed_in = status.startswith(("R", "C"))
        if renamed_in and set_for(old, budget) is not None:
            head = blob_size(repo, f"HEAD:{old}")
        elif renamed_in:
            head = None
        else:
            head = blob_size(repo, f"HEAD:{new}")
        kind, cap = decide(staged, head, s["new_file_cap"], s.get("ceiling"))
        row = {"path": new, "set": s["name"], "staged_bytes": staged,
               "head_bytes": head, "cap_bytes": cap, "kind": kind,
               "delta_bytes": staged - (head if head is not None else 0)}
        checked.append(row)
        if kind != "ok":
            violations.append(row)
    return violations, checked


# ─── refusal text ────────────────────────────────────────────────────────────

def _fmt(n) -> str:
    return "—" if n is None else f"{n:,}"


def refusal_text(violations, trailer: str, note: str = "") -> str:
    ceiling_hits = [v for v in violations if v["kind"] == "grew_over_ceiling"]
    hot_hits = [v for v in violations if v["kind"] != "grew_over_ceiling"]
    header = ("REFUSED — hot-path files may not grow (g-115-6470)" if hot_hits
              else "REFUSED — an on-demand skill may not grow past its injection ceiling (g-115-6690)")
    lines = [f"{TAG} {header}:"]
    for v in violations:
        if v["kind"] == "grew":
            lines.append(f"  {v['path']}  {_fmt(v['head_bytes'])} → {_fmt(v['staged_bytes'])} B "
                         f"(+{v['delta_bytes']:,}; cap = size at HEAD)")
        elif v["kind"] == "grew_over_ceiling":
            lines.append(f"  {v['path']}  {_fmt(v['head_bytes'])} → {_fmt(v['staged_bytes'])} B "
                         f"(+{v['delta_bytes']:,}; already over the {_fmt(v['cap_bytes'])} B injection "
                         f"ceiling for set '{v['set']}' — it may shrink, not grow)")
        else:
            lines.append(f"  {v['path']}  NEW at {_fmt(v['staged_bytes'])} B > "
                         f"new_file_cap {_fmt(v['cap_bytes'])} for set '{v['set']}'")
    if note:
        lines.append(f"  ({note})")
    if ceiling_hits:
        lines += [
            "A skill is paid when it is INJECTED, and an injection is bounded: a skill larger than",
            "the ceiling reaches the model TRUNCATED, so its later content is silently not there.",
            "Measured 2026-08-18: 4 injections averaged 63,515 B and an 88,887 B skill arrived cut.",
            "Below the ceiling a skill may grow freely — this fires only above it, and only on GROWTH,",
            "so the extraction that fixes the file is never blocked. Route the bulk out:",
            "  • a long list of checks / cases / rules → a DATA registry (YAML/JSONL) the skill iterates,",
            "      the shape core/config/gates.yaml and core/config/hot-path-budget.yaml already use",
            "  • per-item WHY narrative              → a registry field, or core/config/rationale/<kebab>.md",
            "  • reference catalogs                  → core/config/conventions/<name>.md, loaded on demand",
        ]
    if hot_hits:
        lines += [
            "These files load on every turn or every loop iteration; every byte is paid on every",
            "compaction cycle of every agent. Route the new prose to an on-demand home instead:",
            "  • WHY-narrative behind pseudocode  → core/config/rationale/<kebab>.md, leaving",
            "      `# Rationale (WHY <phrase>): core/config/rationale/<kebab>.md` in the source",
            "  • schema / API / protocol detail    → core/config/conventions/<name>.md (load-conventions.sh <name>)",
            "  • incident, lesson, measurement     → reasoning bank / knowledge tree / a ledger file, not the rule",
            "  • a NEW rule that only matters for some files → `paths:` front matter so it loads on demand",
        ]
    # SHARED TAIL — applies to BOTH tiers. It used to live inside the hot-path
    # block, so a ceiling-only refusal would have named no escape hatch at all.
    lines += [
        "Before cutting existing prose: `py -3 core/scripts/doc-pin-map.py --file <path>` lists the",
        "lines a verify-learning or test pin reads.",
        "If the growth genuinely belongs where it is, add a trailer to the commit message — it is",
        "audited to world/override-bypass-ledger.jsonl:",
        f"  {trailer} <why this must live here>",
        f"  (iteration-commit.sh: --message \"{trailer} <why>\")",
        "Sets + caps: core/config/hot-path-budget.yaml. Report: bash core/scripts/hot-path-size-gate.sh --check",
    ]
    return "\n".join(lines)


# ─── ledger ──────────────────────────────────────────────────────────────────

def _ledger_path():
    from _paths import WORLD_DIR  # type: ignore
    if WORLD_DIR is None:
        raise RuntimeError("WORLD_DIR unresolved")
    return Path(WORLD_DIR) / "override-bypass-ledger.jsonl"


def write_ledger(repo: Path, violations, justification: str, message: str) -> str:
    """Append the override record. Returns "" on success, else a WARN reason."""
    try:
        from _fileops import locked_append_jsonl  # type: ignore
        subject = next((ln.strip() for ln in message.splitlines()
                        if ln.strip() and not ln.startswith("#")), "")
        try:
            head = _git(repo, "rev-parse", "--short", "HEAD").strip()
        except Exception:
            head = None
        record = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "gate": GATE_ID,
            "override_token": hashlib.sha1(justification.encode("utf-8", errors="replace")).hexdigest()[:12],
            "justification": justification[:1000],
            "agent": os.environ.get("MIND_AGENT") or None,
            "session_id": os.environ.get("MIND_SID") or None,
            "context": {
                "caller": "core/githooks/commit-msg",
                "head_before": head,
                "commit_subject": subject[:200],
                "files": [{k: v[k] for k in ("path", "set", "head_bytes", "staged_bytes",
                                            "cap_bytes", "kind", "delta_bytes")}
                          for v in violations],
                "net_bytes": sum(v["delta_bytes"] for v in violations),
            },
        }
        locked_append_jsonl(_ledger_path(), record)
        return ""
    except Exception as e:  # never wedge the commit on the audit write
        return f"ledger write failed: {e}"


# ─── hook mode ───────────────────────────────────────────────────────────────

def run_gate(repo: Path, msg_file, out=sys.stdout) -> int:
    if in_merge(repo):
        print(f"{TAG} merge commit — not gated (merges combine already-gated commits)", file=out)
        return 0
    try:
        budget = load_budget(repo)
    except Exception as e:
        print(f"{TAG} WARN: budget unreadable ({e}) — allowing commit; fix the registry", file=out)
        return 0
    try:
        violations, _checked = evaluate(repo, budget)
    except Exception as e:
        print(f"{TAG} WARN: could not evaluate staged sizes ({e}) — allowing commit", file=out)
        return 0
    if not violations:
        return 0
    message = ""
    if msg_file:
        try:
            message = Path(msg_file).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"{TAG} WARN: cannot read commit message ({e})", file=out)
    justification, note = parse_override(message, budget["trailer"])
    if justification:
        warn = write_ledger(repo, violations, justification, message)
        grew = ", ".join(f"{v['path']} {'+' if v['delta_bytes'] >= 0 else ''}{v['delta_bytes']:,} B"
                         for v in violations)
        print(f"{TAG} OVERRIDE accepted — {grew} — recorded to override-bypass-ledger.jsonl"
              f"{'' if not warn else ' (WARN: ' + warn + ')'}", file=out)
        return 0
    print(refusal_text(violations, budget["trailer"], note), file=out)
    return 1


# ─── --check ─────────────────────────────────────────────────────────────────

def head_sizes(repo: Path, budget: dict):
    """{path: bytes} for every budgeted file at HEAD (one ls-tree call)."""
    out = _git(repo, "ls-tree", "-r", "-l", "HEAD")
    sizes = {}
    for line in out.splitlines():
        # <mode> <type> <sha> <size>\t<path>
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) < 4 or parts[1] != "blob":
            continue
        if set_for(path, budget) is None:
            continue
        try:
            sizes[path] = int(parts[3])
        except ValueError:
            continue
    return sizes


def _ratchet_lower(key: str, value: int, extra: dict) -> dict:
    """lower_is_better ratchet into meta/audit-baselines.yaml (audit-baselines.md schema)."""
    from _paths import META_DIR  # type: ignore
    from _fileops import locked_modify_yaml  # type: ignore
    if META_DIR is None:
        return {"verdict": "error", "reason": "META_DIR unresolved"}
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    captured: dict = {}

    def _modify(baselines):
        if not isinstance(baselines, dict):
            baselines = {}
        entry = baselines.get(key) or {}
        prior = entry.get("baseline")
        if prior is None:
            verdict, new = "seeded", value
        elif value > prior:
            verdict, new = "regressed", prior       # never move the baseline the wrong way
        elif value < prior:
            verdict, new = "ratcheted", value
        else:
            verdict, new = "stable", prior
        history = (entry.get("history") or [])
        history.append({"recorded_at": now, "drift_total": value, "verdict": verdict, **extra})
        baselines[key] = {"baseline": new, "last_recorded": now, "last_verdict": verdict,
                          "history": history[-50:], "polarity": "lower_is_better",
                          "unit": "bytes_at_HEAD_across_hot_path_budget_sets"}
        captured.update(verdict=verdict, baseline=new, prior=prior)
        return baselines

    try:
        locked_modify_yaml(Path(META_DIR) / "audit-baselines.yaml", _modify, initial={})
    except Exception as e:
        return {"verdict": "error", "reason": str(e)}
    return captured


def run_check(repo: Path, no_ratchet: bool, hard_gate: bool, as_json: bool, out=sys.stdout) -> int:
    try:
        budget = load_budget(repo)
    except Exception as e:
        print(f"FAIL: {TAG} budget unreadable: {e}", file=out)
        return 1 if hard_gate else 0
    try:
        head = _git(repo, "rev-parse", "--short", "HEAD").strip()
        sizes = head_sizes(repo, budget)
    except Exception as e:
        print(f"FAIL: {TAG} cannot read HEAD: {e}", file=out)
        return 1 if hard_gate else 0
    per_set = {}
    for path, n in sizes.items():
        s = set_for(path, budget)["name"]
        per_set.setdefault(s, {"files": 0, "bytes": 0})
        per_set[s]["files"] += 1
        per_set[s]["bytes"] += n
    # TWO TIERS, TWO METRICS (). Folding the on-demand skills into
    # hot_path_total_bytes would silently REDEFINE a metric that already has
    # history — its baseline was seeded 2026-08-18 at 1,532,051 B, and the
    # corpus would appear to triple overnight purely from a config edit, making
    # every prior reading incomparable. Partition on the tier's RULE (does the
    # set carry a ceiling?) rather than on set names, so a future ceiling set
    # joins the right total without editing this function.
    ceiling_sets = {s["name"] for s in budget["sets"] if s.get("ceiling") is not None}
    in_ceiling = {p: (set_for(p, budget)["name"] in ceiling_sets) for p in sizes}
    hot_total = sum(n for p, n in sizes.items() if not in_ceiling[p])
    ondemand_total = sum(n for p, n in sizes.items() if in_ceiling[p])
    total = hot_total + ondemand_total
    largest = sorted(sizes.items(), key=lambda kv: -kv[1])[:8]
    _meta = {"head": head, "files": len(sizes),
             "per_set": {k: v["bytes"] for k, v in per_set.items()}}
    ratchet = None if no_ratchet else _ratchet_lower(RATCHET_KEY, hot_total, _meta)
    ondemand_ratchet = None if no_ratchet else _ratchet_lower(
        ONDEMAND_RATCHET_KEY, ondemand_total, _meta)

    # One PASS:/FAIL: line is the contract verify-learning greps; --json carries
    # the same line inside the object so the stream stays parseable.
    rc = 0
    if ratchet is None:
        verdict = (f"PASS: {TAG} {len(sizes)} files, hot {hot_total:,} B + on-demand "
                   f"{ondemand_total:,} B = {total:,} B at HEAD (ratchet not written: --no-ratchet)")
    else:
        # Either tier regressing is a FAIL, and the message names WHICH — a
        # single blended number would leave the reader unable to tell an
        # always-loaded regression (expensive, every turn) from an on-demand
        # one (cheap until injected).
        bad = [(k, r) for k, r in ((RATCHET_KEY, ratchet),
                                   (ONDEMAND_RATCHET_KEY, ondemand_ratchet))
               if r and r.get("verdict") in ("regressed", "error")]
        if bad:
            parts = []
            for key, r in bad:
                if r.get("verdict") == "error":
                    parts.append(f"{key} ratchet write error: {r.get('reason')}")
                else:
                    now = hot_total if key == RATCHET_KEY else ondemand_total
                    parts.append(f"{key} GREW to {now:,} B (baseline {r.get('baseline'):,} B)")
            verdict = (f"FAIL: {TAG} " + "; ".join(parts) +
                       f" — an override or a merge added prose; grep world/override-bypass-ledger.jsonl "
                       f"for gate {GATE_ID} and route it out (rationale/conventions/tree/a data registry)")
            rc = 1 if hard_gate else 0
        else:
            verdict = (f"PASS: {TAG} {RATCHET_KEY} {ratchet.get('verdict')} at {hot_total:,} B "
                       f"(baseline {ratchet.get('baseline'):,} B); "
                       f"{ONDEMAND_RATCHET_KEY} {ondemand_ratchet.get('verdict')} at {ondemand_total:,} B "
                       f"(baseline {ondemand_ratchet.get('baseline'):,} B)")

    if as_json:
        # `total_bytes` keeps its name and its meaning (the whole corpus), but a
        # reader who only had that key would see it triple at the tier split and
        # have no way to tell which half moved. The subtotals are additive, so
        # existing consumers keep working and new ones can discriminate.
        print(json.dumps({"head": head, "total_bytes": total,
                          "hot_path_bytes": hot_total, "on_demand_bytes": ondemand_total,
                          "files": len(sizes),
                          "per_set": per_set, "largest": largest, "ratchet": ratchet,
                          "on_demand_ratchet": ondemand_ratchet,
                          "verdict": verdict}, indent=1), file=out)
        return rc
    print(f"{TAG} hot-path corpus at HEAD {head}: {len(sizes)} files, {total:,} B", file=out)
    for name, v in per_set.items():
        print(f"  {name:<16} {v['files']:>3} files {v['bytes']:>10,} B", file=out)
    print("  largest:", file=out)
    for path, n in largest:
        print(f"    {n:>9,}  {path}", file=out)
    print(verdict, file=out)
    return rc


# ─── cli ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", help="repo root (default: git toplevel of cwd)")
    ap.add_argument("--commit-msg-file", help="commit-msg hook shape: path to the message file")
    ap.add_argument("--check", action="store_true", help="HEAD-based report + audit-baselines ratchet")
    ap.add_argument("--no-ratchet", action="store_true", help="with --check: read only, write nothing")
    ap.add_argument("--hard-gate", action="store_true", help="with --check: exit 1 on FAIL")
    ap.add_argument("--json", action="store_true", help="with --check: JSON output")
    ap.add_argument("--explain", metavar="PATH", help="print the set + cap this path would get now")
    args = ap.parse_args(argv)

    if args.repo:
        repo = Path(args.repo).resolve()
    else:
        try:
            repo = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        except Exception as e:
            print(f"{TAG} WARN: not in a git work tree ({e}) — nothing to gate")
            return 0

    if args.explain:
        budget = load_budget(repo)
        s = set_for(args.explain, budget)
        if s is None:
            print(f"{args.explain}: not budgeted")
            return 0
        head = blob_size(repo, f"HEAD:{args.explain}")
        ceiling = s.get("ceiling")
        # ASK THE ENGINE, never restate the rule. "would one more byte be
        # refused?" is exactly the operator's question, and routing it through
        # decide() means this output cannot drift from the gate's actual
        # polarity — the failure mode that made --explain wrong for the whole
        # first hour of the ceiling tier, reporting a Tier-2 file's cap as its
        # own current size (i.e. "cannot grow") while it had 40 KB of headroom.
        one_more = decide((head or 0) + 1, head, s["new_file_cap"], ceiling)[0]
        verdict = "one more byte is REFUSED" if one_more != "ok" else "may grow"
        if ceiling is not None:
            if head is None:
                state = f"new file — must be born at or under {ceiling:,} B"
            elif head > ceiling:
                state = f"at {head:,} B, already {head - ceiling:,} B OVER"
            else:
                state = f"at {head:,} B, {ceiling - head:,} B of headroom"
            print(f"{args.explain}: set '{s['name']}', CEILING {ceiling:,} B; {state}; {verdict}")
        else:
            cap = head if head is not None else s["new_file_cap"]
            src = "size at HEAD" if head is not None else "new_file_cap — not at HEAD"
            print(f"{args.explain}: set '{s['name']}', RATCHET cap {cap:,} B ({src}); {verdict}")
        return 0
    if args.check:
        return run_check(repo, args.no_ratchet, args.hard_gate, args.json)
    return run_gate(repo, args.commit_msg_file)


if __name__ == "__main__":
    sys.exit(main())
