#!/usr/bin/env python3
"""hand-command-audit.py — Layer-C detective for the FOURTH capability-routing lane.

Three routing surfaces inspect a WRITTEN RECORD and each has a preventive gate:
`participants:[user]`, `defer_reason`, and outbound email. Handing the user a
command block in a chat reply routes identical work to the identical human and
passes through none of them — nothing is written and no tool is called, so a
PreToolUse hook has nothing to intercept. That lane is ungateable by
construction (`.claude/rules/probe-before-defer.md` § Enforcement).

What IS possible is post-hoc detection: assistant prose is preserved in the
session transcript. This script reads recent transcripts, extracts command
blocks that were HANDED TO THE USER, and cross-checks each against
`capability-gate.py` — the same gate the three written lanes use. A hit means
the agent asked a human to run something it could have run itself.

Shape mirrors `aspirations-rejection-audit.py`: report-only by default, with
`--exit-on-hits` for a recurring goal or cron wrapper that should fail loudly.

WHAT IT DELIBERATELY DOES NOT FLAG, because a detective nobody believes is
worse than none (guard-1802 — measure what a predicate EXCLUDES):
  - Fenced blocks with no second-person instruction near them. Agents post
    command blocks constantly as EVIDENCE ("I ran this") and as illustration;
    those are not routing. The discriminator is an imperative addressed to the
    reader, not the presence of a fence.
  - Blocks the user explicitly asked for ("show me the command", "what would
    I run"). Answering a direct request is not routing work away.
  - `!`-prefixed suggestions, which are the SANCTIONED form: the session
    guidance tells the agent to offer `! <command>` for genuinely interactive
    things like an interactive login, and that lands the output back in the
    conversation.

Its blind spot, stated rather than left implied: prose that routes work
WITHOUT a fenced block ("you'll need to log into the console and click
Deploy") is invisible here. This catches the command-block shape only.

Usage:
  py -3 core/scripts/hand-command-audit.py [--hours 24] [--json] [--exit-on-hits]
  py -3 core/scripts/hand-command-audit.py --transcript <path>   # audit one file
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Fenced blocks that plausibly contain shell commands. An unlabeled fence
# counts only when its body looks like a command line (see _looks_like_shell).
_FENCE = re.compile(r"```(bash|sh|shell|console|zsh)?\n(.*?)```", re.S)

# A second-person instruction to RUN something. Deliberately narrow: this is
# the discriminator between "here is what I ran" (evidence) and "here is what
# you should run" (routing). Under-matching is the intended failure direction.
_HANDOFF = re.compile(
    r"\b("
    r"you (?:can |could |should |'ll |will |need to |may )?(?:just )?run\b"
    r"|please run\b|please execute\b"
    r"|(?:could|can) you run\b"
    r"|run (?:this|these|the following)\b"
    r"|(?:here|here's|here is) (?:the |a )?(?:command|commands|steps)\b"
    r"|on your (?:machine|box|laptop|end)\b"
    r"|from your terminal\b"
    r"|you'll need to run\b"
    r")",
    re.I,
)

# The user ASKED for the command — answering is not routing.
_SOLICITED = re.compile(
    r"\b(what (?:command|would i run)|show me the command|how do i run|"
    r"give me the command|paste the command)\b", re.I,
)

_SHELLISH = re.compile(
    r"^\s*(sudo|ssh|scp|curl|wget|git|aws|docker|kubectl|systemctl|python3?|"
    r"py |npm|node|bash|sh |cd |ls |cat |mkdir|rm |cp |mv |chmod|chown|export|"
    r"gradlew|\./)",
    re.M,
)


def _looks_like_shell(lang: str, body: str) -> bool:
    if lang:
        return True
    return bool(_SHELLISH.search(body))


def _iter_assistant_text(path: Path):
    """Yield each assistant TEXT block. Tool calls are not text and are skipped."""
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") != "assistant":
                continue
            content = (rec.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for chunk in content:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    text = chunk.get("text") or ""
                    if text.strip():
                        yield rec.get("timestamp") or "", text


def _preceding_window(text: str, start: int, chars: int = 400) -> str:
    return text[max(0, start - chars):start]


def find_handoffs(text: str):
    """Return command blocks in `text` that were handed to the user to run."""
    out = []
    for m in _FENCE.finditer(text):
        lang, body = (m.group(1) or ""), (m.group(2) or "")
        if not body.strip() or not _looks_like_shell(lang, body):
            continue
        # The sanctioned `! <command>` form is not routing — it runs in-session.
        if body.lstrip().startswith("!"):
            continue
        window = _preceding_window(text, m.start())
        if not _HANDOFF.search(window):
            continue
        if _SOLICITED.search(window):
            continue
        out.append({"lang": lang or "(unlabeled)",
                    "command": body.strip()[:600],
                    "cue": (_HANDOFF.search(window).group(0) if _HANDOFF.search(window) else "")})
    return out


def _gate(command: str, root: Path):
    """Ask capability-gate.py whether this is agent-provisionable. Fail-open."""
    gate = root / "core" / "scripts" / "capability-gate.py"
    if not gate.exists():
        return None
    try:
        proc = subprocess.run(
            [sys.executable, str(gate), "--failure-reason", command,
             "--intended-participants", "user", "--output", "json"],
            capture_output=True, text=True, timeout=30,
        )
        return json.loads(proc.stdout or "{}")
    except Exception:
        return None  # a detective that crashes must not look like a clean run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--transcript", default="")
    ap.add_argument("--project-dir", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--exit-on-hits", action="store_true")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent.parent

    if args.transcript:
        files = [Path(args.transcript)]
    else:
        base = Path(args.project_dir) if args.project_dir else (
            Path(os.path.expanduser("~")) / ".claude" / "projects"
            / ("-" + str(root).lstrip("/").replace("/", "-"))
        )
        cutoff = time.time() - args.hours * 3600
        files = sorted(
            (p for p in base.glob("*.jsonl") if p.stat().st_mtime >= cutoff),
            key=lambda p: p.stat().st_mtime, reverse=True,
        ) if base.exists() else []

    hits, scanned_blocks = [], 0
    for f in files:
        for ts, text in _iter_assistant_text(f):
            scanned_blocks += 1
            for h in find_handoffs(text):
                verdict = _gate(h["command"], root)
                h.update({"transcript": f.name, "timestamp": ts,
                          "agent_capable": (verdict or {}).get("would_block"),
                          "matched_capability": ((verdict or {}).get("matches") or [{}])[0]
                          .get("matched_keyword", "")})
                hits.append(h)

    # A zero must be interpretable, not vacuous: report the denominator too.
    result = {"transcripts_scanned": len(files),
              "assistant_text_blocks_scanned": scanned_blocks,
              "handoffs_found": len(hits),
              "agent_capable_handoffs": sum(1 for h in hits if h.get("agent_capable")),
              "interpretable": scanned_blocks > 0,
              "hits": hits}

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if not files:
            print("hand-command-audit: NO transcripts in window — "
                  "this is CANNOT-CHECK, not a clean result.", file=sys.stderr)
        print(f"scanned {len(files)} transcript(s), {scanned_blocks} assistant text block(s)")
        print(f"command blocks handed to the user: {len(hits)}")
        for h in hits:
            flag = "AGENT-CAPABLE" if h.get("agent_capable") else "not gate-matched"
            print(f"  [{flag}] {h['transcript'][:8]} {h['timestamp'][:19]} "
                  f"cue={h['cue']!r}")
            print(f"      {h['command'].splitlines()[0][:110]}")

    if args.exit_on_hits and result["agent_capable_handoffs"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
