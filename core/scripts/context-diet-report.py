#!/usr/bin/env python3
"""context-diet-report — the before/after instrument for the context-diet goal family.

WHY THIS EXISTS (g-115-6472, user directive 2026-08-11 "detection outranks
attribution"). The reducer ran 30 iterations against 19 autocompacts in 12.5h and
nothing reported it; precheck fired in 23% of iterations and nothing reported that
either. The sibling diet goals (g-115-6464/6466/6468-6474) need a before/after
instrument or they will be argued rather than measured. Shared brief: knowledge-tree
node `context-window-diet`.

THREE SECTIONS, and the split is not cosmetic:

  STATIC     deterministic from the git tree — byte-identical on every box, so it
             is the half that can be ratcheted fleet-wide without flapping.
             (Verified 2026-08-17: cc-07 reproduced cc-09's numbers exactly.)
  DYNAMIC    read from THIS session's transcript — role- and box-dependent, noisy,
             and NOT comparable across roles. See the role split below.
  READINESS  does the fixed preamble fit a 125k-token local-inference window.

THE ROLE SPLIT IS THE LOAD-BEARING DESIGN POINT. A worker Body and a reducer are
structurally different populations, not two samples of one:

  - measured 2026-08-17, alpha WORKER on cc-07 (37.4h): 39 compactions / 35 closes
    = 0.90 closes per compaction, channel mix 14% skill-injection / 42% tool-result
    / 37% tool-input.
  - measured 2026-08-17, alpha REDUCER on cc-04 (12.5h, per the brief): 1.6
    iterations per compaction, channel mix 39% skill-injection / 37% tool-result /
    21% tool-input.

Those profiles are INVERTED. Pooling them into one baseline would produce a number
that describes neither and flaps with whichever role ran last (guard-4124: a rate
whose denominator mixes populations is diluted, and it degrades smoothly and
plausibly rather than erroring). So the dynamic ratchet is keyed PER ROLE and the
static ratchet is global.

POSITIVE-CONTROL EVERY ZERO (guard-1760 / guard-1641 / rb-245). Several metrics are
structurally zero for one role and alarming for the other — precheck NEVER fires on
a worker because `/aspirations-precheck` is reducer-only-by-design, so a bare "0%"
would read as catastrophic when it is correct. Every zero this report prints is
tagged `n/a (reducer-only)` or `0 (measured)`, never a bare 0.

Usage:
  context-diet-report.py [--json] [--no-ledger] [--no-ratchet] [--agent NAME]
                         [--transcript PATH]

Exit: 0 always (report), unless --hard-gate and a ratchet verdict is `regressed`.
"""
import argparse
import json
import os
import platform
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from _paths import META_DIR, PROJECT_ROOT, agent_session_dir, agent_state_dir  # type: ignore
from _fileops import locked_modify_yaml, locked_append_jsonl  # type: ignore

BASELINES_PATH = META_DIR / "audit-baselines.yaml"
LEDGER_PATH = META_DIR / "context-diet-ledger.jsonl"

# Ratchet keys. Polarity differs per metric and the ratchet MUST honour it:
# preamble bytes regress by GROWING, iterations-per-compaction regresses by
# FALLING. A single-direction ratchet would silently bless half the family's
# regressions.
KEY_PREAMBLE = "context_diet_preamble_bytes"          # lower is better
KEY_ITERS_FMT = "context_diet_iters_per_compaction_{role}"  # higher is better

# Bytes-per-token. THE DENSE FIGURE IS MEASURED; THE PROSE FIGURE IS NOT, and the
# difference decides this report's headline verdict — at 2.5 the preamble does not
# fit a 125k window, at 4.0 it fits with room. So the provenance of each is stated
# rather than left as a magic constant.
#
# DENSE — three independent measurements of framework markdown, all clustering:
#   2.48 B/tok  program-alignment-health.md, 99,564 B -> 40,171 tok  (cc-05, .claude/rules/self.md)
#   2.51 B/tok  same file,                   77,690 B -> 30,937 tok  (zeta,  .claude/rules/self.md)
#   2.57 B/tok  a folded framework doc,      55,706 B -> 21,700 tok  (this session's transcript)
# Different boxes, different files, different sizes, same answer. `self.md` warns
# that ID-dense markdown (goal ids, guard ids, shas, timestamps, tables) tokenizes
# far denser than prose and that a 4 B/tok conversion understates tokens ~1.6x.
# The rules corpus is exactly that kind of text — `run-full-suite-after-deep-code.md`
# alone is 26% of it and is nearly all hostnames, kernel versions, ids and tables.
#
# PROSE — `self.md` labels ~4 B/tok an "unverified upper-bound estimate". It is
# reported as the optimistic bound ONLY. Judging readiness on it would be
# re-baselining a detector to make it green (the brief's explicit prohibition).
BYTES_PER_TOKEN_DENSE = 2.5
BYTES_PER_TOKEN_PROSE = 4.0
LOCAL_WINDOW_TOKENS = 125_000
# Brief §2.6: preamble <= 25k tokens of a 125k budget.
PREAMBLE_TOKEN_TARGET = 25_000


def _run(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except Exception:
        return ""


def collect_static(root: Path) -> dict:
    """Deterministic from the git tree. Same on every box; safe to ratchet globally."""
    claude_md = root / "CLAUDE.md"
    rules_dir = root / ".claude" / "rules"
    skills_dir = root / ".claude" / "skills"

    claude_bytes = claude_md.stat().st_size if claude_md.exists() else 0

    rule_files = sorted(rules_dir.glob("*.md")) if rules_dir.is_dir() else []
    rules_bytes = sum(p.stat().st_size for p in rule_files)
    # A rule carrying `paths:` front matter loads ONLY when Claude reads a matching
    # file, so it is not part of the fixed per-turn preamble. Measured 2026-08-17:
    # 0 of 33 carry it, which is why the whole rules corpus is preamble today.
    scoped = [p for p in rule_files
              if p.read_text(errors="replace").lstrip().startswith("---")
              and "\npaths:" in p.read_text(errors="replace")[:2000]]
    scoped_bytes = sum(p.stat().st_size for p in scoped)

    skill_files = sorted(skills_dir.glob("*/SKILL.md")) if skills_dir.is_dir() else []
    skills_bytes = sum(p.stat().st_size for p in skill_files)

    top_rules = sorted(((p.stat().st_size, p.name) for p in rule_files), reverse=True)[:5]
    top_skills = sorted(((p.stat().st_size, p.parent.name) for p in skill_files),
                        reverse=True)[:5]

    preamble = claude_bytes + rules_bytes - scoped_bytes
    return {
        "claude_md_bytes": claude_bytes,
        "rules_bytes": rules_bytes,
        "rules_files": len(rule_files),
        "rules_path_scoped_files": len(scoped),
        "rules_path_scoped_bytes": scoped_bytes,
        "preamble_bytes": preamble,
        "skills_bytes": skills_bytes,
        "skills_files": len(skill_files),
        "top_rules": [{"bytes": b, "name": n} for b, n in top_rules],
        "top_skills": [{"bytes": b, "name": n} for b, n in top_skills],
    }


def _transcript_path(agent: str, explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    sid = os.environ.get("MIND_SID")
    if not sid:
        sid_file = PROJECT_ROOT / "agents" / agent / "session" / "running-session-id"
        if sid_file.exists():
            sid = sid_file.read_text(errors="replace").strip()
    if not sid:
        return None
    slug = "-" + str(PROJECT_ROOT).lstrip("/").replace("/", "-")
    p = Path.home() / ".claude" / "projects" / slug / f"{sid}.jsonl"
    return p if p.exists() else None


def collect_dynamic(path: Path | None) -> dict:
    """Read THIS session's transcript. Role- and box-dependent — never pooled.

    KNOWN LIMITATION, stated rather than left to be rediscovered: every figure
    here is cumulative over the session TO DATE, not a windowed rate. Two
    consequences worth knowing before acting on a verdict.

    (1) Running the report twice in one long session can move
        closes_per_compaction and report `regressed` from ordinary drift — a
        compaction that lands before the next close lowers the ratio until that
        close arrives. Read a single mid-session regression as a prompt to look,
        not as a finding; the `history` list is the trend and one row is not.
    (2) Being cumulative also DAMPS it, so a genuine sustained slowdown shows up
        slowly and late. That is the direction of error to prefer here — a
        detector that cried wolf on every compaction would be tuned out — but it
        does mean this metric is a poor early warning by construction.

    A windowed variant (last N closes) fixes both. It was deliberately NOT built
    when this shipped, on the stated ground that "it needs a cross-session
    baseline to be meaningful, and this goal ships the first row of that history
    rather than assuming its shape."

    THAT GROUND IS NARROWER THAN IT READS, and the distinction is why `--window`
    now exists (g-115-6468 check 3). A cross-session baseline is required to
    RATCHET a windowed value — to say this session is worse than the last. It is
    NOT required to ANSWER a fixed absolute bar, and check 3 is exactly that:
    "Skill injections per iteration < 40 KB avg over 10 iterations", a threshold
    on ONE transcript needing no history at all. So the windowed figure is
    computed and REPORTED, and is deliberately NOT fed to the ratchet — the
    original reasoning is preserved where it applies and lifted where it does not.

    The window is computed from per-close buckets accumulated in this same pass,
    never by differencing ledger rows: measured 2026-08-18, `iteration_closes`
    across 9 ledger rows read [35,35,43,43,43,43,0,0,237] — NOT monotonic,
    because rows span sessions whose counters reset. Differencing a cumulative
    series that resets yields a confident wrong number (guard-3700: append order
    is not time order).
    """
    if path is None:
        # NOT a zero. An unreadable transcript and a session with no activity are
        # different facts, and collapsing them is the rb-245 shape this whole
        # report is meant to avoid.
        return {"available": False,
                "reason": "transcript not found (no MIND_SID / running-session-id, "
                          "or the file does not exist on this box)"}

    compacts = closes = skill_inj = 0
    skill_bytes = tool_res = tool_in = asst_text = 0
    skills: dict[str, int] = {}
    idmap: dict[str, str] = {}
    first = last = None
    lines = 0
    # Per-close buckets for the windowed view. `cur_*` accumulates the injections
    # seen SINCE the previous close; a close flushes one bucket. Whatever remains
    # at EOF is an iteration still in flight and is reported separately rather
    # than folded in — counting a partial iteration as a whole one deflates the
    # per-iteration average by exactly the amount the iteration has left to run.
    buckets: list[dict] = []
    cur_bytes = cur_inj = 0

    with open(path, errors="replace") as fh:
        for line in fh:
            lines += 1
            try:
                o = json.loads(line)
            except Exception:
                continue
            ts = o.get("timestamp")
            if ts:
                first = first or ts
                last = ts
            if o.get("isCompactSummary"):
                compacts += 1
            m = o.get("message") or {}
            role = m.get("role")
            c = m.get("content")
            if isinstance(c, str):
                c = [{"type": "text", "text": c}]
            if not isinstance(c, list):
                continue
            for b in c:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    txt = b.get("text") or ""
                    if role == "assistant":
                        asst_text += len(txt)
                    elif role == "user" and "Base directory for this skill:" in txt[:200]:
                        # A Skill(...) call arrives back as user-role text carrying
                        # the whole SKILL.md body. That injection is the reducer's
                        # dominant cost and it is invisible unless counted here.
                        skill_inj += 1
                        skill_bytes += len(txt)
                        cur_inj += 1
                        cur_bytes += len(txt)
                elif t == "tool_use":
                    nm = b.get("name")
                    idmap[b.get("id")] = nm
                    inp = json.dumps(b.get("input") or {})
                    tool_in += len(inp)
                    if nm == "Skill":
                        s = (b.get("input") or {}).get("skill")
                        if s:
                            skills[s] = skills.get(s, 0) + 1
                    if nm == "Bash" and "iteration-close.sh --phase verify" in inp:
                        # The honest denominator. Skill(...) re-entries are inflated
                        # by post-compaction re-entry (brief: 292 re-entries vs 169
                        # closes), so counting re-entries overstates throughput.
                        closes += 1
                        buckets.append({"skill_bytes": cur_bytes,
                                        "skill_injections": cur_inj})
                        cur_bytes = cur_inj = 0
                elif t == "tool_result":
                    cc = b.get("content")
                    tool_res += len(cc if isinstance(cc, str) else json.dumps(cc))

    total = skill_bytes + tool_res + tool_in + asst_text
    pct = lambda v: round(100.0 * v / total, 1) if total else 0.0
    return {
        "available": True,
        "transcript": str(path),
        "transcript_bytes": path.stat().st_size,
        "transcript_lines": lines,
        "window_start": first,
        "window_end": last,
        "compactions": compacts,
        "iteration_closes": closes,
        "closes_per_compaction": round(closes / compacts, 2) if compacts else None,
        "skill_injections": skill_inj,
        "skill_injection_bytes": skill_bytes,
        "skill_kb_per_close": round(skill_bytes / 1024 / closes, 1) if closes else None,
        "channel_pct": {
            "skill_injection": pct(skill_bytes),
            "tool_result": pct(tool_res),
            "tool_input": pct(tool_in),
            "assistant_text": pct(asst_text),
        },
        "skill_calls": dict(sorted(skills.items(), key=lambda x: -x[1])),
        "per_close_buckets": buckets,
        "in_flight_after_last_close": {"skill_bytes": cur_bytes,
                                       "skill_injections": cur_inj},
    }


def windowed_skill(dy: dict, n: int) -> dict:
    """Average skill-injection KB over the LAST `n` closed iterations.

    This is the metric g-115-6468 check 3 puts the bar on — "Skill injections per
    iteration < 40 KB avg over 10 iterations". NAME THE METRIC THAT CARRIES THE
    BAR before reading any number beside it (guard-3555): the cumulative
    `skill_kb_per_close` printed above is a DIFFERENT quantity over a different
    span, and on a long session the two legitimately disagree. Check 3 is
    answered by `avg_kb_per_close` here and by nothing else.

    NOT RATCHETED, deliberately — see collect_dynamic's docstring. This answers an
    absolute bar; ratcheting it would need the cross-session baseline the original
    author correctly declined to assume.

    Returns `available: False` with a REASON rather than a zero whenever the
    window cannot be filled (rb-245): an unread transcript, a session that has
    closed nothing, and a session that has closed fewer than `n` are three
    different facts and none of them is "0 KB per iteration".
    """
    if not dy.get("available"):
        return {"available": False, "reason": dy.get("reason", "no transcript")}
    b = dy.get("per_close_buckets") or []
    if not b:
        return {"available": False, "n_requested": n, "n_available": 0,
                "reason": "no iteration closed in this transcript yet — an empty "
                          "window is an absence of measurement, NOT 0 KB/iteration"}
    win = b[-n:]
    total = sum(x["skill_bytes"] for x in win)
    return {
        "available": True,
        "n_requested": n,
        "n_available": len(win),
        "short_window": len(win) < n,
        "avg_kb_per_close": round(total / 1024 / len(win), 1),
        "total_kb": round(total / 1024, 1),
        "injections": sum(x["skill_injections"] for x in win),
        # guard-1436: print the COMPLETE bucket census, empties included. An
        # iteration that injected nothing is a real and informative data point,
        # and dropping it would silently raise the average it belongs in.
        "per_close_kb": [round(x["skill_bytes"] / 1024, 1) for x in win],
        "empty_closes": sum(1 for x in win if x["skill_bytes"] == 0),
        "in_flight_excluded_kb": round(
            (dy.get("in_flight_after_last_close") or {}).get("skill_bytes", 0)
            / 1024, 1),
    }


def detect_role(agent: str) -> str:
    """worker | reducer | assistant | unknown.

    A worker Body has a forked per-session WM (or a body-manifest); the reducer
    is the session named by running-session-id. MODE IS CHECKED FIRST: an
    `assistant` / `reader` session never runs the loop, so its iteration-close
    count is STRUCTURALLY zero and must not be ratcheted against a loop role.
    Measured 2026-08-17 (alpha, cc-09): an assistant session that had served as
    a hand-driven worker Body still carried a body-manifest, was classified
    `worker`, and ratcheted 0.0 closes/compaction against the worker baseline
    of 1 -> a REGRESSED verdict from a session in which the loop never ran.
    Paths go through the `_paths` helpers, never a literal `agents/` join
    (CLAUDE.md "Agent-dir Resolution").
    """
    mode_file = agent_state_dir(agent) / "agent-mode"
    if mode_file.exists():
        mode = mode_file.read_text(errors="replace").strip()
        if mode in ("assistant", "reader"):
            return "assistant"
    sid = os.environ.get("MIND_SID")
    if sid:
        sess = agent_session_dir(agent, sid)
        if (sess / "working-memory.yaml").exists() or (sess / "body-manifest.yaml").exists():
            return "worker"
    rsid = agent_state_dir(agent) / "running-session-id"
    if rsid.exists() and sid and rsid.read_text(errors="replace").strip() == sid:
        return "reducer"
    return "unknown"


def readiness(static: dict) -> dict:
    b = static["preamble_bytes"]
    dense = b / BYTES_PER_TOKEN_DENSE
    prose = b / BYTES_PER_TOKEN_PROSE
    return {
        "preamble_bytes": b,
        "preamble_tokens_dense_est": int(dense),
        "preamble_tokens_prose_est": int(prose),
        "local_window_tokens": LOCAL_WINDOW_TOKENS,
        "preamble_token_target": PREAMBLE_TOKEN_TARGET,
        # Judged on the DENSE estimate: rules and CLAUDE.md are id-dense, and
        # `self.md` records the prose ratio understating tokens ~1.6x on such
        # content. Using the flattering figure here would be re-baselining a
        # detector to make it green.
        "fits_125k": dense < LOCAL_WINDOW_TOKENS,
        "meets_25k_target": dense <= PREAMBLE_TOKEN_TARGET,
        "pct_of_local_window": round(100.0 * dense / LOCAL_WINDOW_TOKENS, 1),
        "excess_over_target_tokens": max(0, int(dense - PREAMBLE_TOKEN_TARGET)),
    }


def decide(prior, value, lower_is_better: bool):
    """PURE polarity decision: (prior, value, direction) -> (verdict, new_baseline, msg).

    Extracted with no I/O so the polarity can be branch-tested directly, matching
    `reducer_self_fence.py::decide`. That matters more here than it looks: every
    sibling ratchet in this tree hardcodes lower-is-better, and g-115-6472 needs
    `regressed` on preamble bytes GROWING *and* on iterations-per-compaction
    FALLING — opposite directions through one code path. A ratchet that quietly
    ignored the direction argument would still seed, still record history, and
    still look healthy while blessing every throughput regression it exists to
    catch. Keeping this pure is what lets a test feed the SAME movement to both
    polarities and pin that they disagree.
    """
    if prior is None:
        return "seeded", value, f"Seeded baseline at {value}."
    worse = value > prior if lower_is_better else value < prior
    better = value < prior if lower_is_better else value > prior
    if worse:
        # NEVER move the baseline the wrong way — that is the whole ratchet.
        return ("regressed", prior,
                f"WARN: moved the wrong way: baseline {prior} -> {value}. "
                f"{'Grew' if lower_is_better else 'Fell'} since the baseline.")
    if better:
        return "ratcheted", value, f"OK: improved {prior} -> {value}. Baseline ratcheted."
    return "stable", prior, f"OK: stable at {value}."


def ratchet(key: str, value, lower_is_better: bool, now_iso: str, extra: dict) -> dict:
    """Persist the pure `decide()` verdict into meta/audit-baselines.yaml."""
    captured: dict = {}
    if value is None:
        # NOT a zero. An unmeasurable metric seeded as 0 would set a floor nothing
        # can regress below, permanently disarming the detector (rb-245).
        return {"verdict": "skipped", "reason": "metric unavailable", "baseline": None}

    def _modify(baselines):
        if not isinstance(baselines, dict):
            baselines = {}
        entry = baselines.get(key) or {}
        verdict, new, msg = decide(entry.get("baseline"), value, lower_is_better)
        msg = f"{key}: {msg}"

        history = (entry.get("history") or [])
        history.append({"recorded_at": now_iso, "drift_total": value,
                        "verdict": verdict, **extra})
        baselines[key] = {
            "baseline": new,
            "last_recorded": now_iso,
            "last_verdict": verdict,
            "history": history[-50:],
            "polarity": "lower_is_better" if lower_is_better else "higher_is_better",
        }
        captured.update(verdict=verdict, baseline=new, message=msg)
        return baselines

    try:
        locked_modify_yaml(BASELINES_PATH, _modify, initial={})
    except Exception as e:
        return {"verdict": "error", "reason": str(e), "baseline": None}
    return captured


def check_mode() -> int:
    """READ the ratchet and report. Writes nothing.

    This is the /verify-learning surface (g-115-6472 outcome 2). It deliberately
    READS `meta/audit-baselines.yaml` instead of re-running the report: a
    diagnostic that mutates the baseline every time it is consulted cannot be
    used to investigate a suspected regression without perturbing the quantity
    being measured, and re-running would append a ledger row per verify pass.

    THE ABSENT CASE IS `SKIPPED`, NEVER `PASS`. A baselines file with no diet
    keys means the report has never run on this box — which prints exactly the
    same silence as "no regression" unless the two are made to differ
    (guard-1760 / guard-1641). Reporting PASS there would make this check
    permanently green on every box that never installed the instrument.
    """
    try:
        import yaml
        raw = BASELINES_PATH.read_text(encoding="utf-8") if BASELINES_PATH.exists() else ""
        baselines = yaml.safe_load(raw) or {}
    except Exception as e:
        print(f"SKIPPED: context-diet ratchet unreadable ({e}) — this is NOT a clean "
              f"pass. Fix the read before treating it as no-drift.")
        return 0

    keys = {k: v for k, v in baselines.items()
            if k == KEY_PREAMBLE or k.startswith("context_diet_iters_per_compaction_")}
    if not keys:
        print("SKIPPED: no context-diet baseline present — context-diet-report.sh has "
              "never run on this box. This is NOT a clean pass; run "
              "`bash core/scripts/context-diet-report.sh` to seed it.")
        return 0

    regressed = {k: v for k, v in keys.items() if (v or {}).get("last_verdict") == "regressed"}
    for k, v in sorted(keys.items()):
        print(f"  {k}: {v.get('last_verdict')} baseline={v.get('baseline')} "
              f"({v.get('polarity')}, recorded {v.get('last_recorded')})")

    if not regressed:
        print(f"PASS: context-diet-ratchet — {len(keys)} baseline(s), none regressed.")
        return 0

    for k, v in sorted(regressed.items()):
        latest = (v.get("history") or [{}])[-1].get("drift_total")
        if k == KEY_PREAMBLE:
            print(f"FAIL: context-diet-ratchet REGRESSED — the fixed preamble GREW to "
                  f"{latest} B against a baseline of {v.get('baseline')} B. Every agent "
                  f"pays this on every turn. Fold the added prose out of CLAUDE.md / "
                  f".claude/rules/*.md, or give the new rule `paths:` front matter so it "
                  f"loads conditionally instead of always.")
        else:
            role = k.rsplit("_", 1)[-1]
            print(f"FAIL: context-diet-ratchet REGRESSED — closes-per-compaction FELL to "
                  f"{latest} against a baseline of {v.get('baseline')} for role={role}. "
                  f"The loop is completing less work per context window than it used to. "
                  f"Do NOT re-baseline to make this green.")
    # Advisory by default, mirroring the sibling ratchets: a noisy throughput
    # metric must not be able to fail a whole verify pass on its own.
    return 1 if os.environ.get("VERIFY_LEARNING_DIET_HARD_GATE") == "1" else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="read the ratchet and report PASS/FAIL/SKIPPED; writes nothing")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-ledger", action="store_true")
    ap.add_argument("--no-ratchet", action="store_true")
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT", ""))
    ap.add_argument("--transcript", default=None)
    ap.add_argument("--hard-gate", action="store_true",
                    help="exit 1 when any ratchet verdict is regressed")
    ap.add_argument("--window", type=int, default=10, metavar="N",
                    help="average skill-injection KB over the last N CLOSED "
                         "iterations (default 10, the span g-115-6468 check 3 "
                         "specifies). Reported, never ratcheted.")
    args = ap.parse_args()

    if args.check:
        return check_mode()

    agent = args.agent or "unknown"
    role = detect_role(agent)
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    st = collect_static(PROJECT_ROOT)
    dy = collect_dynamic(_transcript_path(agent, args.transcript))
    dy["windowed"] = windowed_skill(dy, args.window)
    rd = readiness(st)

    box = {"hostname": socket.gethostname(), "kernel": platform.release(),
           "agent": agent, "role": role}

    ratchets = {}
    if not args.no_ratchet:
        ratchets["preamble_bytes"] = ratchet(
            KEY_PREAMBLE, st["preamble_bytes"], True, now_iso,
            {"claude_md": st["claude_md_bytes"], "rules": st["rules_bytes"],
             "rules_files": st["rules_files"]})
        if role in ("worker", "reducer") and dy.get("closes_per_compaction") is not None:
            ratchets["closes_per_compaction"] = ratchet(
                KEY_ITERS_FMT.format(role=role), dy["closes_per_compaction"],
                False, now_iso,
                {"closes": dy["iteration_closes"], "compactions": dy["compactions"],
                 "hostname": box["hostname"]})
        else:
            ratchets["closes_per_compaction"] = {
                "verdict": "skipped",
                "reason": (f"role={role} — the loop never runs in assistant/reader "
                           f"mode, so iteration closes are structurally zero here; "
                           f"not a regression and not ratcheted")
                if role == "assistant" else
                (f"role={role} — the dynamic ratchet is keyed per role "
                 f"because worker and reducer are structurally different "
                 f"populations; an unknown role would pollute both keys")
                if role not in ("worker", "reducer")
                else "no transcript available on this box",
                "baseline": None}

    report = {"generated_at": now_iso, "box": box, "static": st,
              "dynamic": dy, "readiness": rd, "ratchets": ratchets}

    if not args.no_ledger:
        try:
            locked_append_jsonl(LEDGER_PATH, {
                "ts": now_iso, **box,
                "preamble_bytes": st["preamble_bytes"],
                "rules_bytes": st["rules_bytes"],
                "rules_files": st["rules_files"],
                "skills_bytes": st["skills_bytes"],
                "preamble_tokens_dense_est": rd["preamble_tokens_dense_est"],
                "fits_125k": rd["fits_125k"],
                "compactions": dy.get("compactions"),
                "iteration_closes": dy.get("iteration_closes"),
                "closes_per_compaction": dy.get("closes_per_compaction"),
                "skill_kb_per_close": dy.get("skill_kb_per_close"),
                # The windowed figure rides in the ledger row so the history it
                # would need to BE ratcheted actually accumulates — recording it
                # is what makes a future cross-session baseline possible without
                # assuming its shape today.
                "skill_kb_per_close_windowed":
                    (dy.get("windowed") or {}).get("avg_kb_per_close"),
                "windowed_n": (dy.get("windowed") or {}).get("n_available"),
                "channel_pct": dy.get("channel_pct"),
            })
            report["ledger"] = str(LEDGER_PATH)
        except Exception as e:
            report["ledger_error"] = str(e)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)

    if args.hard_gate and any(r.get("verdict") == "regressed" for r in ratchets.values()):
        return 1
    return 0


def _kb(n):
    return f"{n/1024:,.1f} KB"


def _print_human(r):
    b, st, dy, rd = r["box"], r["static"], r["dynamic"], r["readiness"]
    print("=" * 72)
    print(f"CONTEXT-DIET REPORT  {r['generated_at']}")
    print(f"  box={b['hostname']}  kernel={b['kernel']}  agent={b['agent']}  role={b['role']}")
    print("=" * 72)

    print("\n-- STATIC (deterministic from the git tree; same on every box) --")
    print(f"  CLAUDE.md                {st['claude_md_bytes']:>10,} B  {_kb(st['claude_md_bytes'])}")
    print(f"  .claude/rules/*.md       {st['rules_bytes']:>10,} B  {_kb(st['rules_bytes'])}"
          f"  ({st['rules_files']} files)")
    print(f"    of which path-scoped   {st['rules_path_scoped_bytes']:>10,} B"
          f"  ({st['rules_path_scoped_files']} files carry `paths:` -> NOT in the fixed preamble)")
    print(f"  FIXED PREAMBLE           {st['preamble_bytes']:>10,} B  {_kb(st['preamble_bytes'])}"
          f"   <- loaded every turn, every agent")
    print(f"  all SKILL.md             {st['skills_bytes']:>10,} B  {_kb(st['skills_bytes'])}"
          f"  ({st['skills_files']} skills, loaded on demand)")
    print("  largest rules:")
    for x in st["top_rules"]:
        print(f"    {x['bytes']:>9,} B  {x['name']}")

    print("\n-- DYNAMIC (this session's transcript; role- and box-dependent) --")
    if not dy.get("available"):
        # Explicitly NOT zeros. See the module docstring.
        print(f"  UNAVAILABLE: {dy.get('reason')}")
        print("  (this is an absence of measurement, NOT a measurement of zero)")
    else:
        print(f"  window                   {dy['window_start']} -> {dy['window_end']}")
        print(f"  transcript               {_kb(dy['transcript_bytes'])}"
              f"  ({dy['transcript_lines']:,} lines)")
        print(f"  compactions              {dy['compactions']}")
        print(f"  iteration closes         {dy['iteration_closes']}"
              f"   (iteration-close.sh --phase verify; NOT Skill re-entries,"
              f" which post-compaction re-entry inflates)")
        print(f"  CLOSES PER COMPACTION    {dy['closes_per_compaction']}"
              f"   <- higher is better")
        print(f"  skill injections         {dy['skill_injections']}"
              f"  ({_kb(dy['skill_injection_bytes'])} total,"
              f" {dy['skill_kb_per_close']} KB per close, CUMULATIVE)")
        w = dy.get("windowed") or {}
        if w.get("available"):
            short = ("  [SHORT WINDOW: only %d closed]" % w["n_available"]
                     if w.get("short_window") else "")
            print(f"  WINDOWED skill/close     {w['avg_kb_per_close']} KB"
                  f"   <- last {w['n_available']} closed iterations{short}")
            print(f"    per-close KB           {w['per_close_kb']}"
                  f"   ({w['empty_closes']} of {w['n_available']} injected nothing)")
            if w["in_flight_excluded_kb"]:
                print(f"    excluded               {w['in_flight_excluded_kb']} KB in the"
                      f" iteration still in flight (not yet closed)")
        else:
            print(f"  WINDOWED skill/close     UNAVAILABLE: {w.get('reason')}")
        ch = dy["channel_pct"]
        print(f"  channel mix              skill-injection {ch['skill_injection']}% |"
              f" tool-result {ch['tool_result']}% |"
              f" tool-input {ch['tool_input']}% |"
              f" assistant-text {ch['assistant_text']}%")
        if dy["skill_calls"]:
            print("  skill calls:", ", ".join(f"{k}={v}" for k, v in
                                              list(dy["skill_calls"].items())[:8]))
        if b["role"] == "worker":
            # The positive control. A bare 0% here would read as catastrophic.
            print("  precheck fire rate       n/a (reducer-only) — /aspirations-precheck is"
                  " reducer-only-by-design, so a worker MUST show no firings."
                  " This is not a zero.")

    print("\n-- READINESS (125k-token local-inference budget) --")
    print(f"  preamble tokens (dense 2.5 B/tok)  ~{rd['preamble_tokens_dense_est']:,}"
          f"   <- the honest figure for id-dense text")
    print(f"  preamble tokens (prose 4.0 B/tok)  ~{rd['preamble_tokens_prose_est']:,}"
          f"   (upper-bound estimate; understates id-dense content ~1.6x)")
    print(f"  target                             {rd['preamble_token_target']:,} tokens")
    print(f"  FITS IN 125k WINDOW                {'YES' if rd['fits_125k'] else 'NO'}"
          f"   ({rd['pct_of_local_window']}% of the window is preamble)")
    print(f"  MEETS 25k PREAMBLE TARGET          {'YES' if rd['meets_25k_target'] else 'NO'}"
          f"   (excess {rd['excess_over_target_tokens']:,} tokens)")

    print("\n-- RATCHET (meta/audit-baselines.yaml) --")
    for name, res in (r.get("ratchets") or {}).items():
        v = res.get("verdict", "?")
        print(f"  {name:24s} {v.upper():10s} baseline={res.get('baseline')}"
              f"  {res.get('message') or res.get('reason') or ''}")
    if r.get("ledger"):
        print(f"\n  ledger row appended: {r['ledger']}")
    if r.get("ledger_error"):
        print(f"\n  LEDGER WRITE FAILED: {r['ledger_error']}")
    print()


if __name__ == "__main__":
    sys.exit(main())
