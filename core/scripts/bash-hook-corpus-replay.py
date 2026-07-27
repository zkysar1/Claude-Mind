#!/usr/bin/env python3
"""bash-hook-corpus-replay.py — replay real Bash calls through the L1 hook.

Measurement harness for `bash-path-resolution-hook.py`'s out-of-root branch
(g-115-3338 / g-115-3349). Exists as a DURABLE script rather than an ad-hoc
snippet for one reason: the original measurement replayed a SUBSET (11,559
calls / 2 transcripts) and reported 0.000%, which is what justified shipping
the branch as a hard deny. A wider replay (48,348 / 4) found 0.062% and two
live false-positive classes that broke documented capabilities. A subset is
not a corpus, and an ad-hoc snippet makes the subset mistake easy to repeat.
Defaults to ALL transcripts; narrowing requires an explicit flag.

TWO-STAGE BY DESIGN (probe-with-canonical-code-path.md — "canonical BINARY is
not canonical INVOCATION"):
  stage 1  in-process triage over the FULL corpus, using the hook module's OWN
           extract_targets() + _path_roots helpers. Same logic, no per-call
           subprocess (48k subprocesses is hours).
  stage 2  every command stage 1 flags is RE-RUN through the real hook as a
           subprocess with the production stdin-JSON shape — the same call
           shape the tests' bash_verdict() uses. Small N, so the cost is
           trivial, and the reported verdicts come from the production entry
           point rather than from a reimplementation of it.

A stage-1/stage-2 disagreement is itself a finding (it means the in-process
triage has drifted from the real hook) and is reported explicitly rather than
silently reconciled.

Usage:
    py -3 core/scripts/bash-hook-corpus-replay.py                # all transcripts
    py -3 core/scripts/bash-hook-corpus-replay.py --json
    py -3 core/scripts/bash-hook-corpus-replay.py --limit-transcripts 2   # NOT the default, on purpose
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

BASH_HOOK = SCRIPT_DIR / "bash-path-resolution-hook.py"
DEFAULT_CORPUS = Path("/root/.claude/projects/-opt-ayoai-mind")

import types  # noqa: E402


def _load_hook_functions():
    """Load the hook's REAL function defs without running it.

    bash-path-resolution-hook.py is a hook script, not a module: it has NO
    `if __name__ == "__main__"` guard — it calls `main()` unconditionally and
    then `sys.exit(0)` at module level (line ~488). A plain import therefore
    EXECUTES the hook against empty stdin and kills the importing process,
    which presents as rc=0 with zero output and no traceback. (Observed while
    building this harness; it is also the likely reason earlier ad-hoc replays
    reimplemented the extraction logic instead of importing it.)

    So: read the real source, truncate at the trailing driver block, exec the
    definitions. The functions used for triage (`extract_targets`,
    `strip_payload_spans`, `strip_heredoc_bodies`, `is_under`) are then the
    ACTUAL ones the live hook runs — not a copy that can drift. Stage 2 still
    re-runs the production entry point as a subprocess, so this shortcut only
    ever affects which commands get SHORTLISTED, never a reported verdict.

    A documented ALTERNATIVE exists and is equally valid: stub stdin and
    swallow the `SystemExit`, which imports the module cleanly (see the
    `hook-authoring-pitfalls` tree node, which retracts an earlier "cannot be
    exec_module'd" claim). Truncation is used here because it never executes
    `main()` at all, so the harness cannot be perturbed by a future hook that
    acquires side effects before its exit — but either is fine, and neither
    should be re-derived from scratch next time.
    """
    src = BASH_HOOK.read_text(encoding="utf-8")
    marker = "\ntry:\n    main()"
    idx = src.find(marker)
    if idx == -1:
        raise RuntimeError(
            "bash-path-resolution-hook.py driver block not found — its tail "
            "changed shape. Re-check the truncation marker before trusting "
            "any replay output."
        )
    mod = types.ModuleType("bash_hook_defs")
    mod.__file__ = str(BASH_HOOK)
    exec(compile(src[:idx], str(BASH_HOOK), "exec"), mod.__dict__)
    for fn in ("extract_targets", "is_under"):
        if not callable(getattr(mod, fn, None)):
            raise RuntimeError(f"hook function {fn}() missing after load")
    return mod


_hook = _load_hook_functions()

from _path_roots import (  # noqa: E402
    compute_allowed_roots,
    is_write_exempt_sink,
    norm_path,
    read_paths_conf,
)


def iter_bash_commands(paths):
    """Yield (transcript_name, command) for every Bash tool_use in the corpus."""
    for p in paths:
        name = p.name[:12]
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"Bash"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    msg = rec.get("message") or {}
                    content = msg.get("content")
                    if not isinstance(content, list):
                        continue
                    for c in content:
                        if (isinstance(c, dict) and c.get("type") == "tool_use"
                                and c.get("name") == "Bash"):
                            cmd = (c.get("input") or {}).get("command")
                            if isinstance(cmd, str) and cmd.strip():
                                yield name, cmd
        except Exception as e:
            print(f"[replay] WARN: unreadable transcript {p}: {e}", file=sys.stderr)


def build_root_context(agent):
    """Resolve the same roots the live hook resolves for this agent."""
    # Mirrors bash-path-resolution-hook.py main() L316-323 exactly — same
    # conf path, same read_paths_conf/compute_allowed_roots call shape. Do not
    # "simplify" this: a divergence here changes which commands get shortlisted.
    conf = PROJECT_ROOT / "agents" / agent / "local-paths.conf"
    conf_present = conf.is_file()
    paths_conf = read_paths_conf(str(conf)) if conf_present else {}
    allowed_roots = compute_allowed_roots(str(PROJECT_ROOT), paths_conf)
    return conf_present, allowed_roots


def triage(cmd, conf_present, allowed_roots):
    """Stage 1: would the out-of-root branch fire? Mirrors main()'s guard order.

    Returns a list of (verb, raw_path) that reach the out-of-root branch.
    Fail-open on any error — a triage bug must never masquerade as a finding.
    """
    hits = []
    try:
        for verb, raw_path in _hook.extract_targets(cmd):
            target_norm = norm_path(raw_path)
            if not conf_present or not allowed_roots:
                continue
            if not os.path.isabs(target_norm) and not (
                len(target_norm) >= 2 and target_norm[1] == ":"
            ):
                continue
            if is_write_exempt_sink(target_norm):
                continue
            if any(_hook.is_under(target_norm, root) for _, root in allowed_roots):
                continue
            hits.append((verb, raw_path))
    except Exception:
        return []
    return hits


def real_hook_verdict(cmd, agent):
    """Stage 2: the PRODUCTION entry point — subprocess, stdin JSON.

    Identical call shape to core/scripts/tests/test_bash_path_hook_out_of_root.py
    bash_verdict(). Three-valued so an advisory is never collapsed into approve.
    """
    env = dict(os.environ)
    env["PROJECT_ROOT"] = str(PROJECT_ROOT)
    env["MIND_AGENT"] = agent
    try:
        p = subprocess.run(
            [sys.executable, str(BASH_HOOK)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
            capture_output=True, text=True, env=env,
            cwd=str(PROJECT_ROOT), timeout=60,
        )
    except Exception as e:
        return f"error:{e}"
    if p.stdout.strip():
        return "deny"
    if "[l1-bash-path] ADVISORY" in p.stderr:
        return "advisory"
    return "approve"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default=str(DEFAULT_CORPUS),
                    help="directory of *.jsonl transcripts")
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT") or "zeta")
    ap.add_argument("--limit-transcripts", type=int, default=0,
                    help="DEBUG ONLY. A subset is what produced the 0.000%% "
                         "overstatement; default 0 means ALL.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    corpus = Path(args.corpus)
    paths = sorted(corpus.glob("*.jsonl"))
    if args.limit_transcripts:
        paths = paths[: args.limit_transcripts]
    if not paths:
        print(f"[replay] no transcripts under {corpus}", file=sys.stderr)
        return 2

    conf_present, allowed_roots = build_root_context(args.agent)

    total = 0
    flagged = []          # (transcript, cmd, [(verb, path)])
    for name, cmd in iter_bash_commands(paths):
        total += 1
        hits = triage(cmd, conf_present, allowed_roots)
        if hits:
            flagged.append((name, cmd, hits))

    # Stage 2 — production entry point on the flagged set only.
    verified = []
    disagreements = []
    for name, cmd, hits in flagged:
        v = real_hook_verdict(cmd, args.agent)
        verified.append({
            "transcript": name,
            "verdict": v,
            "targets": [{"verb": vb, "path": pth} for vb, pth in hits],
            "command": cmd if len(cmd) <= 400 else cmd[:400] + "…",
        })
        # Stage 1 said "reaches the out-of-root branch"; the real hook should
        # therefore DENY. `advisory` is still accepted here rather than
        # reported, because it is not a stage-1/stage-2 drift — it is the
        # branch wearing its pre- shape, which the verdict tally
        # already surfaces on its own line.
        if v == "approve":
            disagreements.append({"transcript": name, "command": cmd[:300]})

    by_prefix = {}
    for item in verified:
        for t in item["targets"]:
            pref = "/".join(t["path"].split("/")[:2]) or t["path"][:12]
            by_prefix[pref] = by_prefix.get(pref, 0) + 1

    report = {
        "transcripts": [p.name for p in paths],
        "transcript_count": len(paths),
        "total_bash_calls": total,
        "flagged_stage1": len(flagged),
        "flagged_pct": round(100.0 * len(flagged) / total, 4) if total else 0.0,
        "verdicts": {
            v: sum(1 for x in verified if x["verdict"] == v)
            for v in sorted({x["verdict"] for x in verified})
        },
        "by_path_prefix": dict(sorted(by_prefix.items(), key=lambda kv: -kv[1])),
        "stage1_stage2_disagreements": disagreements,
        "conf_present": conf_present,
        "allowed_roots": [{"label": lbl, "root": r} for lbl, r in allowed_roots],
        "residual": verified,
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"[replay] transcripts={len(paths)} bash_calls={total}")
        print(f"[replay] out-of-root flagged: {len(flagged)} "
              f"({report['flagged_pct']}%)")
        print(f"[replay] verdicts (production entry point): {report['verdicts']}")
        print(f"[replay] by path prefix: {report['by_path_prefix']}")
        if disagreements:
            print(f"[replay] !! stage1/stage2 DISAGREEMENTS: {len(disagreements)} "
                  f"— in-process triage has drifted from the real hook")
        for item in verified:
            print("-" * 68)
            print(f"  [{item['transcript']}] {item['verdict']}  "
                  f"targets={[(t['verb'], t['path']) for t in item['targets']]}")
            print(f"    {item['command'][:220]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
