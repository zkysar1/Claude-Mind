"""Deterministic aspirations-loop driver -- file-based, fully-isolated (parent = pure orchestrator).

Called by the Stop hook in driver-mode. The driver sequences the loop in CODE; the model is called
only inside ISOLATED sub-agents. The PARENT never accumulates cognitive content -- the driver writes
each step's full prompt to a FILE, and the parent only emits a tiny "spawn a sub-agent that reads
<file>" instruction. So the parent's per-turn context stays minimal and CONSTANT regardless of goal
count -> the loop scales (the prior design drifted as the parent accumulated inlined prompts/findings).

Per goal (each phase = one parent turn that just spawns sub-agent(s), then ends with a concrete word):
  execute   -> driver writes prompt_1..N.txt (primed, angle-diverse research prompts); parent spawns N
               sub-agents reading them -> each web_searches + writes findings_k.json. (best-of-N)
  synthesize-> driver writes critic-prompt.txt (read findings_1..N.json, merge -> findings.json); parent
               spawns 1 critic sub-agent reading it. ISOLATED + unbiased.
  verify    -> driver writes judge-prompt.txt (read findings.json, score 1-5 -> verdict.json); parent
               spawns 1 judge sub-agent reading it. ISOLATED + unbiased.
  encode    -> (deterministic) structural gate + judge verdict -> winner to the knowledge tree / redo.

State: driver-state.json {"goals":[{"id","topic"}], "n":3, "cursor":0, "pending":null, "retries":0,
"encoded":[]}.  pending in {null,"execute","synthesize","verify"} = which phase's sub-agents are
in-flight. Same Stop-hook contract on Claude Code and Zak Code.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

PASS_THRESHOLD = 4
MAX_RETRIES = 1


def emit(decision: dict) -> None:
    sys.stdout.write(json.dumps(decision))


def _read_json(path: pathlib.Path) -> dict:
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _prime(ws: pathlib.Path, agent_dir: pathlib.Path, agent: str) -> str:
    parts = [f"You ARE '{agent}', an autonomous Mind agent (acting for one bounded research step)."]
    program = ws / "world" / "program.md"
    if program.exists():
        body = program.read_text(encoding="utf-8", errors="replace").strip()
        if body:
            parts.append("YOUR PROGRAM / MISSION:\n" + body[:1200])
    for ident in ("self.md", "identity.md", "persona.md", "agent.md"):
        f = agent_dir / ident
        if f.exists():
            body = f.read_text(encoding="utf-8", errors="replace").strip()
            if body:
                parts.append(f"WHO YOU ARE:\n{body[:800]}")
            break
    parts.append(
        "STANCE: research rigorously, ground every claim in the search results, prefer specific "
        "verifiable facts."
    )
    return "\n\n".join(parts)


def _gate(findings: dict) -> tuple[bool, str]:
    facts = findings.get("facts")
    if not isinstance(facts, list) or not facts:
        return False, "no facts list"
    clean = [f for f in facts if isinstance(f, str) and f.strip()]
    if len(clean) < 3:
        return False, f"only {len(clean)} non-empty facts (need >=3)"
    if len({f.strip().lower() for f in clean}) < len(clean):
        return False, "duplicate facts"
    return True, "ok"


def _clean(ws: pathlib.Path, n: int) -> None:
    names = ["findings.json", "verdict.json", "critic-prompt.txt", "judge-prompt.txt"]
    names += [f"findings_{k}.json" for k in range(1, n + 1)]
    names += [f"prompt_{k}.txt" for k in range(1, n + 1)]
    for nm in names:
        p = ws / nm
        if p.exists():
            p.unlink()


def _encode(prev: dict, findings: dict, verdict: dict, ws: pathlib.Path, session: pathlib.Path, n: int) -> None:
    kdir = ws / "world" / "knowledge" / "driver"
    kdir.mkdir(parents=True, exist_ok=True)
    out = dict(findings)
    out["_verdict"] = verdict
    (kdir / f"{prev['id']}.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    archive = [_read_json(ws / f"findings_{k}.json") for k in range(1, n + 1) if (ws / f"findings_{k}.json").exists()]
    (kdir / f"{prev['id']}_attempts.json").write_text(json.dumps(archive, indent=2, ensure_ascii=False), encoding="utf-8")
    with (session / "driver-journal.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"goal": prev["id"], "n_attempts": len(archive),
                            "winner_facts": len(findings.get("facts", []) or []), "verdict": verdict}) + "\n")


def _write_research_prompts(ws: pathlib.Path, agent_dir: pathlib.Path, agent: str, goal: dict, n: int) -> None:
    """Write the N angle-diverse, primed research prompts to prompt_1..N.txt (sub-agents read them)."""
    angles = [
        "the foundations, definitions, and historical context",
        "the key methods, instruments, projects, and mechanisms",
        "the current state, recent developments, and open debates",
    ]
    prime = _prime(ws, agent_dir, agent)
    for k in range(1, n + 1):
        angle = angles[(k - 1) % len(angles)]
        body = (
            f"{prime}\n\n"
            f"YOUR TASK: research the topic below. Focus ESPECIALLY on {angle}.\n"
            f"TOPIC: {goal['topic']}\n\n"
            f"Work EFFICIENTLY: call web_search ONCE (twice at most); do NOT use web_fetch -- the "
            f"search-result snippets are sufficient. From them, write a file findings_{k}.json at the "
            f'workspace root: {{"topic": "...", "facts": ["fact", ...]}} with 3-6 short, grounded facts. '
            f"Then reply with ONLY the word: done."
        )
        (ws / f"prompt_{k}.txt").write_text(body, encoding="utf-8")


def _write_critic_prompt(ws: pathlib.Path, goal: dict, n: int) -> None:
    files = ", ".join(f"findings_{k}.json" for k in range(1, n + 1))
    (ws / "critic-prompt.txt").write_text(
        f"You are synthesizing research on: {goal['topic']}\n\n"
        f"Read these files at the workspace root: {files}. Each has {{topic, facts:[...]}} from an "
        f"independent attempt. Produce the SINGLE BEST set of 4-6 facts: merge the strongest, most "
        f"accurate facts across all of them, drop errors and duplicates, prefer specific verifiable "
        f'claims. Write the result to findings.json at the workspace root: {{"topic": "{goal["topic"]}", '
        f'"facts": ["...", ...]}}. Do NOT web_search. Then reply with ONLY the word: done.',
        encoding="utf-8",
    )


def _write_judge_prompt(ws: pathlib.Path, goal: dict) -> None:
    (ws / "judge-prompt.txt").write_text(
        f"You are a STRICT research QA reviewer. Read the file findings.json at the workspace root "
        f"(it has {{topic, facts:[...]}} on: {goal['topic']}). Score the facts 1-5 (5=excellent) on "
        f"accuracy, specificity, grounding, and on-topic relevance -- be strict. Write a file "
        f'verdict.json at the workspace root: {{"score": <integer 1-5>, "pass": <true if score>=4 else '
        f'false>, "issues": ["..."]}}. Do NOT web_search. Then reply with ONLY the word: done.',
        encoding="utf-8",
    )


def main() -> int:
    agent_dir = pathlib.Path(os.environ["HOOK_AGENT_DIR"])
    agent = os.environ.get("HOOK_AGENT", "")
    session = agent_dir / "session"
    ws = agent_dir.parent.parent

    state_path = session / "driver-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    goals = state["goals"]
    n = int(state.get("n", 3))
    cursor = int(state.get("cursor", 0))
    pending = state.get("pending")  # None | execute | synthesize | verify
    retries = int(state.get("retries", 0))

    # ── Handle the phase whose sub-agents just finished; pick the next phase. ──
    next_phase = "execute"  # default for the first turn (pending is None) and after encode/redo
    if pending == "execute":
        next_phase = "synthesize"  # research done (findings_k.json staged) -> synthesize
    elif pending == "synthesize":
        ok, _why = _gate(_read_json(ws / "findings.json"))  # critic wrote findings.json
        if ok:
            next_phase = "verify"
        else:
            _clean(ws, n)
            if retries >= MAX_RETRIES:  # structural failure persists -> skip (do not encode garbage)
                with (session / "driver-journal.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"goal": goals[cursor]["id"], "skipped": _why}) + "\n")
                state.setdefault("encoded", []).append(goals[cursor]["id"])
                cursor, retries = cursor + 1, 0
            else:
                retries += 1  # redo this goal
            next_phase = "execute"
    elif pending == "verify":
        prev = goals[cursor]
        verdict = _read_json(ws / "verdict.json")  # judge wrote verdict.json
        findings = _read_json(ws / "findings.json")
        try:
            score = int(verdict.get("score", 0))
        except Exception:
            score = 0
        if score >= PASS_THRESHOLD or retries >= MAX_RETRIES:
            _encode(prev, findings, verdict or {"score": score}, ws, session, n)
            state.setdefault("encoded", []).append(prev["id"])
            cursor, retries = cursor + 1, 0
        else:
            retries += 1  # redo this goal
        _clean(ws, n)
        next_phase = "execute"

    # ── Loop complete? ──
    if cursor >= len(goals):
        state.update(cursor=cursor, pending=None, retries=retries)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        (session / "driver-done").write_text("done", encoding="utf-8")
        return 0

    goal = goals[cursor]
    gpos = f"goal {cursor + 1}/{len(goals)}"
    redo = " (REDO -- prior attempt failed verify)" if retries else ""

    # ── Emit the next phase: the parent only SPAWNS sub-agents that read file-based prompts. ──
    if next_phase == "execute":
        _write_research_prompts(ws, agent_dir, agent, goal, n)
        pending = "execute"
        state.update(cursor=cursor, pending=pending, retries=retries)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        task_lines = "\n".join(
            f"  task {k}: prompt = `Read the file prompt_{k}.txt at the workspace root and follow it "
            f"exactly.`"
            for k in range(1, n + 1)
        )
        reason = (
            f"AUTONOMOUS RESEARCH LOOP -- {gpos}, execute {n} attempts IN PARALLEL{redo}. Make a "
            f"SINGLE `task` tool call whose `tasks` array has EXACTLY {n} entries, with exactly these "
            f"prompts:\n{task_lines}\n"
            f"Each sub-agent reads its own file and researches in isolation. The moment that one task "
            f"call returns, reply with ONLY the word `spawned` and STOP -- call no other tool, do not "
            f"research or synthesize yourself."
        )
    elif next_phase == "synthesize":
        _write_critic_prompt(ws, goal, n)
        pending = "synthesize"
        state.update(cursor=cursor, pending=pending, retries=retries)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        reason = (
            f"AUTONOMOUS RESEARCH LOOP -- {gpos}, SYNTHESIS. Delegate to an isolated critic. Make ONE "
            f"`task` call with a single task whose `prompt` is EXACTLY: `Read the file critic-prompt.txt "
            f"at the workspace root and follow it exactly.` Then reply with ONLY the word `synthesized` "
            f"and STOP -- do not synthesize yourself, call no other tool."
        )
    else:  # verify
        _write_judge_prompt(ws, goal)
        pending = "verify"
        state.update(cursor=cursor, pending=pending, retries=retries)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        reason = (
            f"AUTONOMOUS RESEARCH LOOP -- {gpos}, VERIFY. Delegate to an isolated judge (it does not "
            f"see how the findings were produced). Make ONE `task` call with a single task whose "
            f"`prompt` is EXACTLY: `Read the file judge-prompt.txt at the workspace root and follow it "
            f"exactly.` Then reply with ONLY the word `judged` and STOP -- do not score it yourself, "
            f"call no other tool."
        )

    emit({"decision": "block", "reason": reason})
    return 0


if __name__ == "__main__":
    sys.exit(main())
