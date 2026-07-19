# domain-leak-exempt: store-reader for the projection; reads world/ store schemas and
# builds the redactor from injected box specifics. No domain terms are hardcoded.
"""knowledge-export — read the Mind's knowledge stores, project them safe, emit a bundle.

The store-I/O wrapper around the pure :mod:`knowledge_projection` core. Reads the tree
index + reasoning-bank + guardrails + hypotheses from ``WORLD_PATH``, builds a
:class:`~knowledge_projection.Redactor` from the box's agent names, workspace paths, and
environment secret VALUES, then writes a filtered + redacted + shape-preserving JSON
bundle (the kid-facing wiki). Intended as a PERIODIC job (PEARL §10.7 P3-4) that
regenerates the projected bundle the env-server serves read-only — NOT a request-time
endpoint.

Secret hygiene: env secret values are loaded ONLY to strip them OUT of the output
(redaction). They are never logged, echoed, or written anywhere but as the replacement
target ``[redacted]`` (guard-724).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402 — PyYAML, available in the framework venv

from knowledge_projection import ProjectedBundle, Redactor, project  # noqa: E402

#: Env var name suffixes whose VALUES are stripped from exposed text (never their names).
_SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    """Parse a JSONL store into a list of dicts. Missing file → []; bad lines skipped."""
    out: list[dict[str, object]] = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _humanize_key(key: str) -> str:
    """Turn a kebab-case node key into a readable title.

    ``"9-action-matrix-design"`` → ``"Action matrix design"``; a leading numeric segment
    (tree ordering) is dropped.
    """
    s = re.sub(r"^\d+-", "", key or "").replace("-", " ").replace("_", " ").strip()
    return s[:1].upper() + s[1:] if s else key


def read_tree_nodes(world_path: Path) -> list[dict[str, object]]:
    """Read ``_tree.yaml`` into node dicts shaped for :func:`project`.

    Uses the index-level ``summary`` (already in the tree yaml, so no per-node file read)
    and a humanized key for the title. ``category`` is taken from the node ``file`` path
    so the projection's system/-subtree suppression works on the reliable path segment.
    """
    tree_yaml = world_path / "knowledge" / "tree" / "_tree.yaml"
    if not tree_yaml.is_file():
        return []
    data = yaml.safe_load(tree_yaml.read_text(encoding="utf-8")) or {}
    nodes = data.get("nodes") or {}
    out: list[dict[str, object]] = []
    if isinstance(nodes, dict):
        items = nodes.items()
    else:  # tolerate a list-of-nodes shape
        items = ((str(n.get("key") or ""), n) for n in nodes if isinstance(n, dict))
    for key, node in items:
        if not isinstance(node, dict):
            continue
        out.append(
            {
                "key": key,
                "title": _humanize_key(key),
                "summary": str(node.get("summary") or ""),
                "parent": str(node.get("parent") or ""),
                "children": [str(c) for c in (node.get("children") or []) if c],
                "category": str(node.get("file") or ""),  # file path → top-level category
            }
        )
    return out


def _agent_names(project_root: Path) -> list[str]:
    """Agent directory names under ``agents/`` — redacted to "the agent" in output."""
    agents = project_root / "agents"
    if not agents.is_dir():
        return []
    return [p.name for p in agents.iterdir() if p.is_dir() and not p.name.startswith(".")]


def _secret_values(env: dict[str, str]) -> list[str]:
    """Env var VALUES whose name ends in a secret suffix — stripped from output.

    Only non-trivial values (len ≥ 8) so a short placeholder like "0" can't blank the
    text. Never returns the var NAMES, never logs the values.
    """
    return [
        v
        for name, v in env.items()
        if any(name.upper().endswith(sfx) for sfx in _SECRET_SUFFIXES) and isinstance(v, str) and len(v) >= 8
    ]


def build_bundle(
    world_path: Path,
    project_root: Path,
    *,
    extra_paths: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> ProjectedBundle:
    """Read all four stores from ``world_path`` and project them into a safe bundle."""
    env = dict(os.environ if env is None else env)
    redactor = Redactor(
        agent_names=tuple(_agent_names(project_root)),
        workspace_paths=(str(world_path), str(project_root), *extra_paths),
        secret_values=tuple(_secret_values(env)),
    )
    return project(
        tree_nodes=read_tree_nodes(world_path),
        reasoning=_read_jsonl(world_path / "reasoning-bank.jsonl"),
        guardrails=_read_jsonl(world_path / "guardrails.jsonl"),
        hypotheses=_read_jsonl(world_path / "pipeline.jsonl"),
        redactor=redactor,
    )


# ── OKF markdown bundle (PEARL §10.5) ────────────────────────────────────────
# The durable, human-readable download form: a git-shippable directory of one
# concept per markdown file, each with a required `type` frontmatter discriminator
# (the OKF-aligned transfer-bundle shape, core/config/conventions/
# transfer-bundle-export-shape.md). Distinct from the JSON `/export` the daemon
# serves live — this is the "portable wiki" the box-side job zips to S3.


def _okf_frontmatter(meta: dict[str, object]) -> str:
    """A ``---``-fenced YAML frontmatter block. ``type`` is the one required key."""
    return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n"


def _okf_slug(text: str, n: int) -> str:
    """A readable, collision-free filename stem for a keyless concept (hypothesis, etc.)."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:48]
    return f"{s or 'item'}-{n:03d}"


def _okf_safe_key(key: str, seen: set[str]) -> str:
    """A filesystem-safe, unique stem for a node key (keys are near-kebab already)."""
    base = re.sub(r"[^A-Za-z0-9_-]", "-", key or "node")[:80] or "node"
    stem, i = base, 2
    while stem in seen:
        stem = f"{base}-{i}"
        i += 1
    seen.add(stem)
    return stem


def write_okf_bundle(bundle: ProjectedBundle, out_dir: Path) -> dict[str, int]:
    """Write ``bundle`` as an OKF markdown directory under ``out_dir``. Returns counts.

    Layout: ``index.md`` (progressive-disclosure index) + ``nodes/<key>.md`` (the wiki
    articles) + ``hypotheses/`` + ``guardrails/`` + ``lessons/`` — one concept per file,
    each frontmatter carrying the required ``type`` discriminator. Every string is
    already redacted by :func:`project`; this writer only shapes, never re-filters.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = bundle.counts()

    nodes_dir = out_dir / "nodes"
    nodes_dir.mkdir(exist_ok=True)
    seen: set[str] = set()
    node_stems: list[tuple[str, str]] = []  # (stem, title) — reused for the index links
    for n in bundle.tree:
        stem = _okf_safe_key(str(n.get("key") or ""), seen)
        title = str(n.get("title") or n.get("key") or stem)
        node_stems.append((stem, title))
        fm = {
            "type": "node",
            "key": n.get("key") or stem,
            "title": title,
            "parent": n.get("parent") or "",
            "children": list(n.get("children") or []),
        }
        body = str(n.get("summary") or "")
        (nodes_dir / f"{stem}.md").write_text(
            f"{_okf_frontmatter(fm)}\n# {title}\n\n{body}\n", encoding="utf-8"
        )

    hyp_dir = out_dir / "hypotheses"
    hyp_dir.mkdir(exist_ok=True)
    for i, h in enumerate(bundle.hypotheses, 1):
        statement = str(h.get("statement") or "")
        fm = {
            "type": "hypothesis",
            "horizon": h.get("horizon") or "",
            "status": h.get("status") or "",
        }
        outcome = str(h.get("outcome") or "")
        body = statement + (f"\n\n**Outcome:** {outcome}" if outcome else "")
        (hyp_dir / f"{_okf_slug(statement, i)}.md").write_text(
            f"{_okf_frontmatter(fm)}\n{body}\n", encoding="utf-8"
        )

    guard_dir = out_dir / "guardrails"
    guard_dir.mkdir(exist_ok=True)
    for i, g in enumerate(bundle.guardrails, 1):
        rule = str(g.get("rule") or "")
        (guard_dir / f"{_okf_slug(rule, i)}.md").write_text(
            f"{_okf_frontmatter({'type': 'guardrail'})}\n{rule}\n", encoding="utf-8"
        )

    lesson_dir = out_dir / "lessons"
    lesson_dir.mkdir(exist_ok=True)
    for i, lesson in enumerate(bundle.lessons, 1):
        title = str(lesson.get("title") or "")
        text = str(lesson.get("lesson") or "")
        (lesson_dir / f"{_okf_slug(title or text, i)}.md").write_text(
            f"{_okf_frontmatter({'type': 'lesson', 'title': title})}\n{text}\n", encoding="utf-8"
        )

    # index.md — the optional progressive-disclosure index (invariant 7).
    lines = [
        "---",
        "type: index",
        "---",
        "",
        "# Knowledge base",
        "",
        f"- Wiki articles: {counts['tree']}",
        f"- Hypotheses: {counts['hypotheses']}",
        f"- Guardrails: {counts['guardrails']}",
        f"- Lessons: {counts['lessons']}",
        "",
        "## Articles",
        "",
    ]
    lines += [f"- [{title}](nodes/{stem}.md)" for stem, title in node_stems]
    (out_dir / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return counts


def _resolve_world() -> Path:
    """Resolve WORLD_PATH from the environment (set by _paths.sh) or fail loudly."""
    wp = os.environ.get("WORLD_PATH") or os.environ.get("MIND_WORLD")
    if not wp:
        raise SystemExit("WORLD_PATH not set — source core/scripts/_paths.sh first.")
    return Path(wp)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out_path = None
    out_dir = None
    fmt = "json"
    for i, a in enumerate(argv):
        if a in ("-o", "--out") and i + 1 < len(argv):
            out_path = argv[i + 1]
        elif a == "--out-dir" and i + 1 < len(argv):
            out_dir = argv[i + 1]
        elif a == "--format" and i + 1 < len(argv):
            fmt = argv[i + 1]
    world = _resolve_world()
    project_root = Path(__file__).resolve().parents[2]
    meta = os.environ.get("META_PATH")
    bundle = build_bundle(
        world, project_root, extra_paths=(meta,) if meta else ()
    )

    if fmt == "okf":
        if not out_dir:
            raise SystemExit("--format okf requires --out-dir <dir>")
        counts = write_okf_bundle(bundle, Path(out_dir))
        print(f"wrote OKF bundle to {out_dir}: {counts}")
        return 0

    payload = {
        "counts": bundle.counts(),
        "tree": bundle.tree,
        "hypotheses": bundle.hypotheses,
        "guardrails": bundle.guardrails,
        "lessons": bundle.lessons,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
        print(f"wrote {out_path}: {bundle.counts()}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
