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

import datetime
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402 — PyYAML, available in the framework venv

from knowledge_projection import ProjectedBundle, Redactor, is_domain_tree_node, project  # noqa: E402

#: Env var name suffixes whose VALUES are stripped from exposed text (never their names).
_SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


def _generated_at() -> str:
    """Naive ISO 8601 UTC stamp — the bundle's freshness marker (g-335-270).

    Without this the served bundle is a PRE-PROJECTED artifact with no age: every
    ``/knowledge/*`` route happily returns whatever the last export wrote, however
    old, and a failed or unscheduled export is indistinguishable from a current one
    from the serving side. The hourly ``mind-knowledge-export@.timer`` reduces how
    often that happens but cannot make it detectable — a timer that stops firing
    (box down, stale seed, export FATAL) leaves the previous bundle in place and
    silent. The stamp is the detection mechanism, not the timer.

    Shape matches the repo-wide naive-no-suffix convention (CLAUDE.md "Naming
    Rules"), but is derived from ``timezone.utc`` EXPLICITLY rather than from a bare
    ``now()``: this script's production caller is a systemd unit on the sidecar box,
    which does NOT inherit the ``TZ=UTC`` env that ``.claude/settings.json`` pins on
    agent boxes. A bare ``now()`` there would silently stamp box-local time and make
    every age comparison wrong by the offset.
    """
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


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


#: Cap on the per-node markdown body carried into the export bundle (chars). PEARL renders
#: the full article; 32K bounds the very largest nodes without truncating normal ones.
_NODE_BODY_CAP = 32_000


def _strip_front_matter(text: str) -> str:
    """Return the markdown body after a leading ``---``-fenced YAML front-matter block.

    A node ``.md`` opens with ``---\\n<yaml>\\n---\\n<body>``. Strip that block so the
    exported body is prose only — the front matter carries framework-internal fields
    (topic, last_update_trigger, agent/session ids) that must never reach the kid-facing
    UI. A file with no front matter (or an unterminated fence) is returned unchanged.
    """
    if not text.startswith("---"):
        return text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:]).lstrip("\n")
    return text  # unterminated fence → treat the whole file as body (malformed front matter)


def _read_node_body(tree_dir: Path, file_rel: str) -> str:
    """Read a node's ``.md`` body (front matter stripped, capped) for the export bundle.

    ``file_rel`` is the node ``file`` field — a repo-relative path shaped like
    ``world/knowledge/tree/<cat>/<node>.md``. Resolve it under ``tree_dir`` by dropping the
    ``world/knowledge/tree`` prefix. Any read failure → ``""`` (a missing/unreadable body
    must never fail the export; the node still carries its summary).
    """
    if not file_rel:
        return ""
    parts = file_rel.strip("/").split("/")
    if parts[:3] == ["world", "knowledge", "tree"]:
        parts = parts[3:]
    if not parts:
        return ""
    try:
        text = tree_dir.joinpath(*parts).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    return _strip_front_matter(text)[:_NODE_BODY_CAP]


def read_tree_nodes(world_path: Path) -> list[dict[str, object]]:
    """Read ``_tree.yaml`` into node dicts shaped for :func:`project`.

    Uses the index-level ``summary`` (already in the tree yaml) and a humanized key for the
    title. ``category`` is taken from the node ``file`` path so the projection's
    system/-subtree suppression works on the reliable path segment. Each DOMAIN node also
    carries its full ``.md`` ``body`` (front matter stripped, capped) so the kid-facing UI
    can render the article on click; framework (``system/``) node bodies are NOT read — the
    projection suppresses that subtree anyway, so skipping the read keeps framework bodies
    out of memory entirely (defense in depth) and halves the per-export file reads.
    """
    tree_dir = world_path / "knowledge" / "tree"
    tree_yaml = tree_dir / "_tree.yaml"
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
        file_rel = str(node.get("file") or "")  # file path → top-level category + body source
        # Read the full body ONLY for domain nodes (see docstring — framework bodies are
        # suppressed downstream, so we never even load them).
        body = _read_node_body(tree_dir, file_rel) if is_domain_tree_node(file_rel) else ""
        out.append(
            {
                "key": key,
                "title": _humanize_key(key),
                "summary": str(node.get("summary") or ""),
                "body": body,
                "parent": str(node.get("parent") or ""),
                "children": [str(c) for c in (node.get("children") or []) if c],
                "category": file_rel,
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
        # Prefer the full node body (carried end-to-end for the kid-facing wiki); fall back
        # to the index summary when a node has no body. Already redacted by project().
        body = str(n.get("body") or n.get("summary") or "")
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
    # `generated_at` rides in the frontmatter AND as a visible line: the downloadable
    # wiki outlives the box it came from, so a reader holding an unzipped copy needs
    # to know how old it is without access to the exporter (g-335-270).
    # Frontmatter goes through _okf_frontmatter (the same emitter every other concept
    # file uses) rather than hand-built literal lines: safe_dump QUOTES the stamp, so a
    # consumer's yaml.safe_load gets a `str` matching the JSON payload's type. Written
    # unquoted by hand, YAML auto-types it to a `datetime` whose str() renders
    # "2026-07-26 09:15:53" — a space, not a T — so the same value would read back in a
    # different shape from the two bundle formats.
    generated_at = _generated_at()
    lines = [
        _okf_frontmatter({"type": "index", "generated_at": generated_at}).rstrip("\n"),
        "",
        "# Knowledge base",
        "",
        f"- Generated: {generated_at} UTC",
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
        # First key so a consumer reading the head of the file sees the age
        # immediately. Additive — every existing key keeps its name and shape.
        "generated_at": _generated_at(),
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
