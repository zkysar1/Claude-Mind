#!/usr/bin/env python3
"""Build a compact, constraint-rich context block for spawned agent prompts.

Output: plain text suitable for embedding in an Agent() prompt parameter.
Read-only — does not modify any state files or increment retrieval counters.

Usage:
    build-agent-context.sh --category <cat> [--repo <path>] [--max-tokens N] [--role role]

The host agent calls this BEFORE spawning a sub-agent, captures stdout,
and embeds it in the prompt. The sub-agent receives context as data,
not as an instruction to execute.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import PROJECT_ROOT, WORLD_DIR, AGENT_DIR, META_DIR, AGENT_NAME

#  utilization seam. Imported at TOP LEVEL here — deliberately unlike
# guardrail_retire/weakness-signals, which defer it: this module already imports
# _paths on the line above, so the WORLD_DIR resolution is paid either way and a
# lazy import would buy nothing. Both shapes carry their own reason so neither
# gets "fixed" to match the other. load_all_counters (not load_counters) because
# the two sorts below span BOTH stores — guardrails and reasoning bank.
from _utilization_store import (
    load_all_counters as _load_all_counters,
    utilization_of as _utilization_of,
)

# Store paths
TREE_PATH = WORLD_DIR / "knowledge" / "tree" / "_tree.yaml"
RB_PATH = WORLD_DIR / "reasoning-bank.jsonl"
GUARD_PATH = WORLD_DIR / "guardrails.jsonl"

# Operation tags that executor-role agents always receive (regardless of category)
EXECUTOR_OPERATION_TAGS = {"commit", "push", "git", "ci-cd", "deployment", "staging"}

ROLE_DESCRIPTIONS = {
    "researcher": "READ-ONLY \u2014 do not write files, invoke skills, or call state-mutating scripts",
    "executor": "Can write/commit/push \u2014 follow all CONSTRAINTS below",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_jsonl(path):
    """Read JSONL file, return list of dicts. Returns [] if missing/empty."""
    p = Path(path)
    if not p.exists():
        return []
    items = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                try:
                    items.append(json.loads(stripped))
                except json.JSONDecodeError:
                    continue
    return items


def read_yaml(path):
    """Read YAML file, return dict. Returns {} if missing/empty."""
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def extract_md_body(path, max_lines=3):
    """Read a markdown file, skip YAML front matter, return first N non-empty lines."""
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    # Strip YAML front matter
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3:]
    lines = [l.strip() for l in text.strip().splitlines() if l.strip() and not l.strip().startswith("#")]
    return "\n".join(lines[:max_lines]) if lines else None


def category_matches(item_category, query_categories):
    """Check if an item's category matches any query category (substring, bidirectional)."""
    ic = (item_category or "").lower()
    if not ic:
        return False
    for qc in query_categories:
        qc_lower = qc.lower()
        if qc_lower in ic or ic in qc_lower:
            return True
    return False


def tag_matches(item_tags, target_tags):
    """Check if any item tag overlaps with target tags."""
    return bool(set(t.lower() for t in (item_tags or [])) & target_tags)


def truncate(text, max_chars):
    """Truncate text to max_chars, adding ellipsis if needed."""
    if not text or len(text) <= max_chars:
        return text or ""
    return text[:max_chars - 3] + "..."


# Chars per token for the text THIS script emits. MEASURED here, never inherited
# (guard-2026: justify the base independently instead of adopting the literal you
# are replacing). Ground truth 2026-09-03 (alpha, DESKTOP-O91DLK2): a real
# `--category infrastructure --role executor` build of 205,007 B was reported as
# 85,232 tokens by the Read tool's own truncation notice -> 2.405 B/token. Two
# further builds, counted with tiktoken and corrected by the factor that same
# file calibrates (x1.6903), gave 2.406 (framework-maintenance) and 2.370
# (knowledge-management). Independent corroboration: rb-9606 measures chars/4 at
# ~40% low on id-dense Mind text, which is the same 1.69x from other content.
#
# The prior value (4) underestimated by 1.67-1.71x, and did so in the UNSAFE
# direction: this estimate gates the trimming loops in build_context(), so an
# undercount stops trimming early and emits a block BIGGER than the caller
# budgeted — which the consumer then truncates at the far end, silently. 2.3 is
# set deliberately BELOW the measured floor (2.370) so the estimate runs ~3% HIGH
# and errs toward trimming one node too many; that degrades gracefully, because
# matched_nodes is confidence-sorted and popped from the bottom.
#
# DO NOT UNIFY WITH tree.py / tree_write_fence.py CHARS_PER_TOKEN (also 2.3).
# Same number, DIFFERENT invariant: theirs is measured on tree-node markdown
# against a ~25k-token Read cap; this one on guardrail one-liners against an
# output budget. They agree today by a coincidence of content density, not by
# construction, so a shared helper would silently re-tune this site whenever that
# one is recalibrated (guard-3810). Keep them independent and re-measure this
# constant against THIS script's own output.
CHARS_PER_TOKEN = 2.3


def estimate_tokens(text):
    """Token estimate for this script's own output class (see CHARS_PER_TOKEN)."""
    return int(len(text) / CHARS_PER_TOKEN) if text else 0


# ---------------------------------------------------------------------------
# Repo CLAUDE.md extraction
# ---------------------------------------------------------------------------

def extract_repo_context(repo_path):
    """Read a repo's CLAUDE.md and extract safety tier, test commands, and context."""
    claude_md = Path(repo_path) / "CLAUDE.md"
    if not claude_md.exists():
        return None

    text = claude_md.read_text(encoding="utf-8")
    lines = text.splitlines()
    repo_name = Path(repo_path).name

    # Extract safety tier
    tier_match = re.search(r"[Ss]afety\s+[Tt]ier\s+(\d+)", text)
    tier = int(tier_match.group(1)) if tier_match else None

    # Extract test command (look for common patterns)
    test_cmd = None
    for line in lines:
        ll = line.lower().strip()
        if any(p in ll for p in ["pytest", "npm test", "gradlew test", "test command", "run tests"]):
            test_cmd = line.strip().lstrip("- ").lstrip("`").rstrip("`")
            break

    # First 20 meaningful lines for context
    context_lines = []
    for line in lines[:30]:
        stripped = line.strip()
        if stripped and not stripped.startswith("```"):
            context_lines.append(stripped)
        if len(context_lines) >= 20:
            break

    return {
        "repo_name": repo_name,
        "tier": tier,
        "test_cmd": test_cmd,
        "context": "\n".join(context_lines),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_context(categories, role, repo_path, max_tokens):
    """Build the full context block as a string."""
    sections = []

    # --- Header ---
    role_desc = ROLE_DESCRIPTIONS.get(role, ROLE_DESCRIPTIONS["researcher"])
    sections.append(f"\u2550\u2550\u2550 AGENT CONTEXT \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550")
    sections.append(f"Role: {role} ({role_desc})")
    sections.append(f"Category: {', '.join(categories)}")
    sections.append("")

    # --- Identity ---
    sections.append("\u2500\u2500 IDENTITY \u2500\u2500")
    if AGENT_DIR:
        self_body = extract_md_body(AGENT_DIR / "self.md", max_lines=3)
        sections.append(self_body or "No agent identity configured")
    else:
        sections.append("No agent bound")
    sections.append("")

    # --- Program ---
    program_path = WORLD_DIR / "program.md"
    sections.append("\u2500\u2500 PROGRAM \u2500\u2500")
    program_body = extract_md_body(program_path, max_lines=3)
    sections.append(program_body or "Not configured")
    sections.append("")

    # --- Resolved paths (always rendered; never truncated) ---
    # Sub-agents that read/grep/enumerate data under world/, meta/, or an
    # agent directory need the resolved on-disk paths. The virtual prefixes
    # "world/" and "meta/" in skill docs map to user-configured external
    # paths — without these resolved values, a sub-agent that searches only
    # the local repo misses 100% of canonical world/ artifacts.
    sections.append("\u2500\u2500 RESOLVED PATHS \u2500\u2500")
    sections.append(f"PROJECT_ROOT: {PROJECT_ROOT}")
    sections.append(f"WORLD_DIR:    {WORLD_DIR}")
    sections.append(f"META_DIR:     {META_DIR}")
    if AGENT_DIR is not None:
        sections.append(f"AGENT_DIR:    {AGENT_DIR}  (agent: {AGENT_NAME})")
    else:
        sections.append("AGENT_DIR:    (no agent bound)")
    sections.append(
        "Use these paths directly when grepping JSONL stores "
        "(reasoning-bank.jsonl, guardrails.jsonl, aspirations.jsonl, "
        "pipeline.jsonl) or tree nodes. Do NOT search only the local repo."
    )
    sections.append("")

    # --- Repo context (if --repo provided) ---
    repo_info = None
    if repo_path:
        repo_info = extract_repo_context(repo_path)
        if repo_info:
            sections.append(f"\u2500\u2500 REPO: {repo_info['repo_name']} \u2500\u2500")
            if repo_info["tier"] is not None:
                tier = repo_info["tier"]
                if tier >= 4:
                    sections.append(f"Safety Tier: {tier} (local-only \u2014 NO CI/CD, NO GitHub Actions)")
                elif tier >= 3:
                    sections.append(f"Safety Tier: {tier} (DANGEROUS \u2014 destroys data, launches EC2, costs money)")
                elif tier >= 2:
                    sections.append(f"Safety Tier: {tier} (CAUTION \u2014 writes data, sends emails)")
                else:
                    sections.append(f"Safety Tier: {tier} (read-only, safe)")
            if repo_info["test_cmd"]:
                sections.append(f"Test command: {repo_info['test_cmd']}")
            sections.append(repo_info["context"])
            sections.append("")

    # --- Guardrails as constraints ---
    all_guards = read_jsonl(GUARD_PATH)
    active_guards = [g for g in all_guards if g.get("status") == "active"]

    # Category-matched guardrails
    cat_guards = [g for g in active_guards if category_matches(g.get("category", ""), categories)]

    # For executor role: also include operation-tagged guardrails
    if role == "executor":
        op_guards = [g for g in active_guards
                     if tag_matches(g.get("tags", []), EXECUTOR_OPERATION_TAGS)
                     and g not in cat_guards]
        cat_guards.extend(op_guards)

    # Fallback: if nothing matched, include top 3 most-retrieved
    if not cat_guards:
        _counters = _load_all_counters()
        active_guards.sort(
            key=lambda g: _utilization_of(g, _counters).get("retrieval_count", 0),
            reverse=True,
        )
        cat_guards = active_guards[:3]

    # Deduplicate by id
    seen_ids = set()
    deduped_guards = []
    for g in cat_guards:
        gid = g.get("id", "")
        if gid not in seen_ids:
            seen_ids.add(gid)
            deduped_guards.append(g)
    cat_guards = deduped_guards

    sections.append(f"\u2500\u2500 CONSTRAINTS ({len(cat_guards)} guardrails) \u2500\u2500")
    sections.append("YOU MUST follow these rules:")

    # Add repo safety constraint if tier >= 4 and executor
    if repo_info and repo_info.get("tier") is not None and repo_info["tier"] >= 4 and role == "executor":
        sections.append(f"- REPO SAFETY: This repo is Safety Tier {repo_info['tier']}. "
                        "Do NOT add CI/CD workflows, GitHub Actions, or automated deployment.")

    for g in cat_guards:
        rule = truncate(g.get("rule", ""), 200)
        trigger = g.get("trigger_condition", "")
        line = f"- [{g.get('id', '?')}] {rule}"
        if trigger:
            line += f" (trigger: {truncate(trigger, 80)})"
        sections.append(line)
    sections.append("")

    # --- Reasoning bank (lessons) ---
    all_rb = read_jsonl(RB_PATH)
    active_rb = [r for r in all_rb if r.get("status") == "active"]

    # Category-matched entries
    cat_rb = [r for r in active_rb if category_matches(r.get("category", ""), categories)]

    # For executor role: also include operation-tagged reasoning entries
    if role == "executor":
        op_rb = [r for r in active_rb
                 if tag_matches(r.get("tags", []), EXECUTOR_OPERATION_TAGS)
                 and r not in cat_rb]
        cat_rb.extend(op_rb)

    # Fallback: if nothing matched, include top 3 most-retrieved
    if not cat_rb:
        _counters = _load_all_counters()
        active_rb.sort(
            key=lambda r: _utilization_of(r, _counters).get("retrieval_count", 0),
            reverse=True,
        )
        cat_rb = active_rb[:3]

    # Deduplicate by id
    seen_ids = set()
    deduped_rb = []
    for r in cat_rb:
        rid = r.get("id", "")
        if rid not in seen_ids:
            seen_ids.add(rid)
            deduped_rb.append(r)
    cat_rb = deduped_rb

    sections.append(f"\u2500\u2500 LESSONS ({len(cat_rb)} from reasoning bank) \u2500\u2500")
    for r in cat_rb:
        title = r.get("title", "untitled")
        content = truncate(r.get("content", ""), 120)
        sections.append(f"- [{r.get('id', '?')}] {title}: {content}")
    sections.append("")

    # --- Knowledge tree nodes (summaries only) ---
    tree = read_yaml(TREE_PATH) if TREE_PATH.exists() else {}
    nodes = tree.get("nodes", {})
    entity_index = tree.get("entity_index", {})

    # Simple category matching on tree nodes (lighter than full retrieve.py pipeline)
    matched_nodes = []
    for key, node in nodes.items():
        summary = (node.get("summary", "") or "").lower()
        topic = (node.get("topic", "") or "").lower()
        node_key_lower = key.lower()
        for cat in categories:
            cat_lower = cat.lower()
            if (cat_lower in node_key_lower or cat_lower in summary or cat_lower in topic
                    or node_key_lower in cat_lower):
                matched_nodes.append((key, node))
                break

    # Also check entity index
    for cat in categories:
        for entity_key, entity_nodes in entity_index.items():
            if cat.lower() in entity_key.lower() or entity_key.lower() in cat.lower():
                for nk in entity_nodes:
                    if nk in nodes and (nk, nodes[nk]) not in matched_nodes:
                        matched_nodes.append((nk, nodes[nk]))

    # Sort by confidence descending
    matched_nodes.sort(key=lambda kn: kn[1].get("confidence", 0), reverse=True)

    sections.append(f"\u2500\u2500 KNOWLEDGE ({len(matched_nodes)} nodes) \u2500\u2500")
    for key, node in matched_nodes:
        summary = truncate(node.get("summary", ""), 100)
        cap = node.get("capability_level", "unknown")
        sections.append(f"- [{key}] {summary} (capability: {cap})")
    sections.append("")

    sections.append("\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550")

    # --- Token budget enforcement ---
    result = "\n".join(sections)
    tokens = estimate_tokens(result)

    if tokens > max_tokens:
        # Truncation priority: knowledge nodes first, then reasoning, then guardrails
        # Identity, program, repo, and constraints header are never cut

        # Try removing knowledge nodes from the bottom
        # CRITICAL: must assign back to `sections` so the reasoning loop sees truncated knowledge
        while matched_nodes and estimate_tokens(result) > max_tokens:
            matched_nodes.pop()
            sections = _rebuild_knowledge_section(sections, matched_nodes)
            result = "\n".join(sections)

        # If still over, trim reasoning entries
        while cat_rb and estimate_tokens(result) > max_tokens:
            cat_rb.pop()
            sections = _rebuild_reasoning_section(sections, cat_rb)
            result = "\n".join(sections)

    return result


def _rebuild_knowledge_section(sections, matched_nodes):
    """Replace the KNOWLEDGE section in sections list."""
    result = []
    in_knowledge = False
    for line in sections:
        if line.startswith("\u2500\u2500 KNOWLEDGE"):
            in_knowledge = True
            result.append(f"\u2500\u2500 KNOWLEDGE ({len(matched_nodes)} nodes) \u2500\u2500")
            for key, node in matched_nodes:
                summary = truncate(node.get("summary", ""), 100)
                cap = node.get("capability_level", "unknown")
                result.append(f"- [{key}] {summary} (capability: {cap})")
            result.append("")
        elif in_knowledge:
            if line.startswith("\u2500\u2500 ") or line.startswith("\u2550"):
                in_knowledge = False
                result.append(line)
        else:
            result.append(line)
    return result


def _rebuild_reasoning_section(sections, cat_rb):
    """Replace the LESSONS section in sections list."""
    result = []
    in_reasoning = False
    for line in sections:
        if line.startswith("\u2500\u2500 LESSONS"):
            in_reasoning = True
            result.append(f"\u2500\u2500 LESSONS ({len(cat_rb)} from reasoning bank) \u2500\u2500")
            for r in cat_rb:
                title = r.get("title", "untitled")
                content = truncate(r.get("content", ""), 120)
                result.append(f"- [{r.get('id', '?')}] {title}: {content}")
            result.append("")
        elif in_reasoning:
            if line.startswith("\u2500\u2500 ") or line.startswith("\u2550"):
                in_reasoning = False
                result.append(line)
        else:
            result.append(line)
    return result


def main():
    parser = argparse.ArgumentParser(description="Build agent context block for spawned agent prompts")
    parser.add_argument("--category", required=True, help="Target category (comma-separated)")
    parser.add_argument("--repo", default=None, help="Path to target repo (reads its CLAUDE.md)")
    parser.add_argument("--max-tokens", type=int, default=4000, help="Approximate output token budget")
    parser.add_argument("--role", default="researcher", choices=["researcher", "executor"],
                        help="Agent role: researcher (read-only) or executor (can write)")
    args = parser.parse_args()

    categories = [c.strip() for c in args.category.split(",") if c.strip()]
    if not categories:
        print("Error: --category requires at least one non-empty category", file=sys.stderr)
        sys.exit(1)

    result = build_context(categories, args.role, args.repo, args.max_tokens)
    print(result)


if __name__ == "__main__":
    main()
