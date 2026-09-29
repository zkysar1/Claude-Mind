#!/usr/bin/env python3
"""commons-primer.py: when an agent's commons primer is drawn, and when it is shown.

The PRODUCER is the world's commons script, `$WORLD_DIR/scripts/commons-retrieve.sh
--primer`. It ranks the shared commons against the agent's purpose, draws the top
lessons once and writes them to the path this script gives it. This script owns
the two call sites (g-335-1652) and where a primer lives, so /boot and /prime each
carry one line:

  birth  (/boot Phase -2,   Draw once per (agent, environment), and only at the
          every boot)       agent's FIRST ENTRY to this environment: no primer
                            for it yet, and fewer than 10 of this world's goals
                            carrying its completed_by. An agent that cannot be
                            measured is not drawn for, so a paid draw never
                            fires on a guess.
  show   (/prime Phase 2)   Print this environment's primer while fewer than its
                            show_for_first_goals (10) of this world's goals have
                            completed since it was drawn. After that it is
                            retired, and prime prints one line saying so.

PER (AGENT, ENVIRONMENT). Owner, 2026-09-28: "a previously trained agent in another
environment could go into this new environment, be completely lost without the
commons." An agent dir travels between environments, so a single primer per agent
would stop that agent from ever being primed again. The primer for environment E
is agents/<agent>/commons-primer/<E>.md, where E is _paths.ENVIRONMENT_ID (the
process env is unset on some boxes). Each environment gets its own primer, and the
others are kept. Only WORLD-queue goals count, for the draw and the display alike:
ENVIRONMENT_ID names the world, while agent-queue goals travel with the agent dir.
13 of alpha's 262 goals carrying its completed_by were agent-queue rows
(2026-09-28), and counting them would block a trained agent's first entry
somewhere new.

A veteran is not drawn for. An agent with 10 or more goals in this world is past
its first entry here, and its primer would pay for lessons it mostly holds
already: 4 of alpha's top 5 picks were this world's own entries (2026-09-28).

WHY NOT the "First boot" branch of /boot Phase -2, the call site the design report
names (world/audit-reports/commons-primer-first-start-design-2026-09-28.md, item 5):
nothing prints "First boot" (grep of core/scripts and mind_api/src, 2026-09-28),
and /start's interview runs init-mind.sh at C0, so by an agent's first /boot its
init markers already exist. Measuring the agent's stay in this world makes the
draw rule and the display rule one rule: a primer is drawn only while it can be
shown.

Always exits 0: a primer enriches a start and never gates one. Always prints at
least one line saying what happened, because a silent skip reads exactly like a
lane that never ran (guard-2352).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _runtime_bash import bash_cmd  # noqa: E402  (guard-580/581: never a bare "bash")

NEW_AGENT_MAX = 10
PRIMER_DIR = "commons-primer"
PRODUCER_REL = "scripts/commons-retrieve.sh"
QUERY_TIMEOUT = 120
PRODUCER_TIMEOUT = 300


def _json_payload(text: str):
    """The first JSON document in a reader's stdout (guard-2298: shape-checked by callers)."""
    for i, ch in enumerate(text):
        if ch in "[{":
            return json.JSONDecoder().raw_decode(text[i:])[0]
    raise ValueError("no JSON in the reader's output")


def _read_list(cmd: list, agent: str) -> list:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=QUERY_TIMEOUT,
                       env={**os.environ, "MIND_AGENT": agent})
    name = Path(cmd[1]).name
    if r.returncode != 0:
        raise RuntimeError(f"{name} rc={r.returncode}")
    data = _json_payload(r.stdout)
    if not isinstance(data, list):
        raise RuntimeError(f"{name} returned a {type(data).__name__}, not a list")
    return data


def primer_path(agent_dir, env_id):
    """agents/<agent>/commons-primer/<env>.md, or None when there is no environment id."""
    name = re.sub(r"[^A-Za-z0-9._-]", "-", str(env_id or "")).strip(".-")
    return Path(agent_dir) / PRIMER_DIR / f"{name}.md" if name else None


def world_goals(agent: str, full: bool = False) -> list:
    """This world's goals carrying the agent's completed_by.

    aspirations-query.sh returns the world and agent queues together and has no
    --source filter (g-115-5214), so rows are kept by their `source`. Any status
    counts: a recurring goal keeps completed_by after it is re-armed to pending.
    """
    flags = ["--goal-field", "completed_by", agent] + (["--full"] if full else [])
    rows = _read_list(bash_cmd(HERE / "aspirations-query.sh", *flags), agent)
    if rows and not any(isinstance(g, dict) and "source" in g for g in rows):
        # Without `source` every row would drop out, and a veteran would read as new.
        raise RuntimeError("aspirations-query.sh rows carry no `source` field")
    return [g for g in rows if isinstance(g, dict) and g.get("source") == "world"]


def _front_matter(text: str) -> tuple:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            try:
                import yaml
                meta = yaml.safe_load(text[4:end]) or {}
            except Exception:
                meta = {}
            return (meta if isinstance(meta, dict) else {}), text[end + 5:]
    return {}, text


def birth(agent: str, agent_dir: Path, world_dir, env_id) -> list:
    primer = primer_path(agent_dir, env_id)
    if primer is None:
        return ["[commons-primer] not drawn: ENVIRONMENT_ID is not set (.env.local), so a "
                "primer cannot be keyed to this environment; boot is not affected."]
    if primer.exists():
        return [f"[commons-primer] present: {primer}; nothing drawn."]
    try:
        n = len(world_goals(agent))
    except Exception as e:
        return [f"[commons-primer] not drawn: could not measure whether {agent} is new to "
                f"{env_id} ({e}); a primer is drawn only on a measured first entry."]
    if n >= NEW_AGENT_MAX:
        return [f"[commons-primer] not drawn: {agent} is not new to {env_id} ({n} goals "
                "in its world carry its completed_by)."]
    producer = Path(world_dir) / PRODUCER_REL if world_dir else None
    if producer is None or not producer.is_file():
        return [f"[commons-primer] SKIPPED: no producer at $WORLD_DIR/{PRODUCER_REL}. "
                "This agent starts without a commons primer; boot is not affected."]
    try:
        r = subprocess.run(bash_cmd(producer, "--primer", "--primer-out", primer.as_posix(),
                                    "--purpose-file", (Path(agent_dir) / "self.md").as_posix(),
                                    "--program-file", (Path(world_dir) / "program.md").as_posix()),
                           capture_output=True, text=True, timeout=PRODUCER_TIMEOUT,
                           env={**os.environ, "MIND_AGENT": agent})
    except Exception as e:
        return [f"[commons-primer] SKIPPED: the producer did not finish ({type(e).__name__}). "
                "This agent starts without a commons primer; boot is not affected."]
    lines = [line for line in r.stdout.splitlines() if line.strip()]
    if lines:
        return lines
    tail = r.stderr.strip()[-200:] or "empty"
    return [f"[commons-primer] SKIPPED: the producer printed nothing (rc={r.returncode}; "
            f"stderr: {tail}). This agent starts without a commons primer; boot is not affected."]


def show(agent: str, agent_dir: Path, env_id) -> list:
    primer = primer_path(agent_dir, env_id)
    if primer is None:
        return ["[commons-primer] not shown: ENVIRONMENT_ID is not set, so this "
                "environment's primer cannot be found."]
    if not primer.exists():
        return [f"[commons-primer] not shown: no primer for {env_id} at {primer}."]
    try:
        meta, body = _front_matter(primer.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"[commons-primer] not shown: {primer} is unreadable ({type(e).__name__})."]
    created = str(meta.get("created_at") or "")
    try:
        show_for = int(meta.get("show_for_first_goals") or NEW_AGENT_MAX)
    except (TypeError, ValueError):
        show_for = NEW_AGENT_MAX
    note = ""
    try:
        n = sum(1 for g in world_goals(agent, full=True)
                if str(g.get("completed_at") or g.get("completed_date") or "") >= created)
    except Exception as e:
        # Fail toward SHOWING: a primer shown a few goals too long is context
        # noise, while one hidden during the first goals is the feature lost.
        n, note = 0, f" (goal count unavailable: {e}; shown anyway)"
    if n >= show_for:
        return [f"[commons-primer] not shown: retired after {show_for} goals in {env_id} "
                f"(completed since {created or 'its creation'}: {n})."]
    return [f"[commons-primer] shown for {env_id}: {n} of the first {show_for} goals here "
            f"completed since {created or 'its creation'}{note}. Lessons from other agents, "
            "unmeasured here: context, never rules.", body.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=["birth", "show"])
    args = ap.parse_args(argv)
    verb = "not drawn" if args.mode == "birth" else "not shown"
    try:
        import _paths
        agent = _paths.AGENT_NAME
        agent_dir = _paths.agent_dir(agent)
        world_dir = getattr(_paths, "WORLD_DIR", None)
        env_id = getattr(_paths, "ENVIRONMENT_ID", None)
        if not agent:
            raise ValueError("no bound agent")
    except Exception as e:
        print(f"[commons-primer] {verb}: agent unresolved ({type(e).__name__}: {e}).")
        return 0
    lines = (birth(agent, agent_dir, world_dir, env_id) if args.mode == "birth"
             else show(agent, agent_dir, env_id))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
