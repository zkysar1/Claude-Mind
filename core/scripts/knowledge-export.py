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
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402 — PyYAML, available in the framework venv

from _paths import AGENTS_PARENT_DIR

# The SHARED atomic-write policy (tmp -> os.replace with backoff -> in-place fallback),
# taken as the LOCAL backend EXPLICITLY rather than through ``get_backend()``. The
# bundle is an export ARTIFACT at an arbitrary ``--out`` path, not a governed store:
# under an own-cloud deployment ``get_backend()`` returns a backend whose
# ``atomic_write`` does an S3 PUT of the target and hands the callback an in-memory
# buffer — so the bundle would be shipped to object storage instead of the local path
# the reader actually opens, and the fsync below would raise ``UnsupportedOperation``
# on a handle with no ``fileno``. Both were MEASURED here before this line was written.
# Never inherit the caller's storage backend for a non-store write (guard-955, rb-2983).
from storage_backend import LocalBackend  # noqa: E402
from knowledge_projection import (  # noqa: E402
    KNOWLEDGE_COUNT_KEYS,
    ProjectedBundle,
    Redactor,
    is_domain_tree_node,
    project,
)

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


#: Where ``_tree.yaml`` is looked for, in preference order. The FIRST entry is the
#: framework convention every other reader shares (``<world>/knowledge/tree`` — CLAUDE.md,
#: ``tree.py``, ``retrieve.sh``) and stays first so a conformant world is never ambiguous.
#: The second is the mind-sidecar layout: ``bootstrap.sh`` has set
#: ``WORLD_PATH=<workspace>/knowledge`` and created ``<workspace>/knowledge/tree`` since
#: 5996fa7 (2026-07-17) and has never written the conformant path — so ``<world>/tree`` is
#: not drift to be migrated away, it is a second live layout with 402 nodes in it.
_TREE_LAYOUTS: tuple[tuple[str, ...], ...] = (("knowledge", "tree"), ("tree",))


def _resolve_tree_dir(world_path: Path) -> Path | None:
    """The directory whose ``_tree.yaml`` this world actually has, or ``None``.

    Selection is by PRESENCE OF THE INDEX, never by presence of the directory — an empty
    ``knowledge/tree/`` next to a populated ``tree/`` must not shadow it. That is the exact
    regression a "just mkdir the conformant path in the provisioner" fix would have
    introduced, so the ordering above is load-bearing only as a tie-break between two
    layouts that BOTH carry an index.

    Taking the non-conformant branch is announced on stderr: a compatibility fallback that
    is silent is indistinguishable from the empty world it produces, which is the whole
    defect this function exists to end (g-368-34; ``communication-clarity.md`` rule 5 —
    the objection to fallbacks is that they MASK the source of a failure).
    """
    for i, parts in enumerate(_TREE_LAYOUTS):
        candidate = world_path.joinpath(*parts)
        if (candidate / "_tree.yaml").is_file():
            if i:
                print(
                    f"[knowledge-export] WARNING: tree index found at non-conformant "
                    f"{candidate} (framework layout is {world_path / 'knowledge' / 'tree'}). "
                    f"Exporting it, but every OTHER framework tree reader will read it as EMPTY.",
                    file=sys.stderr,
                )
            return candidate
    return None


def world_store_evidence(world_path: Path) -> dict[str, int]:
    """Byte counts for the stores this world holds, whatever the bundle ended up saying.

    The positive control for an all-zero bundle (``guard-2298``: print the unfiltered
    population beside the zero). A world with readable bytes here and zeros in the bundle
    is a BROKEN EXPORT; a world with zeros here is simply new, and must still export.
    """
    out: dict[str, int] = {}
    tree_dir = _resolve_tree_dir(world_path)
    if tree_dir is not None:
        try:
            out["tree_index_bytes"] = (tree_dir / "_tree.yaml").stat().st_size
        except OSError:
            pass
    for name in ("reasoning-bank.jsonl", "guardrails.jsonl", "pipeline.jsonl"):
        try:
            size = (world_path / name).stat().st_size
        except OSError:
            continue
        if size:
            out[name] = size
    return {k: v for k, v in out.items() if v}


def _degrade(
    status: dict[str, object] | None, path: Path, error: str
) -> list[dict[str, object]]:
    """Record a tree-index read failure on ``status`` and return an empty node list.

    Called on the two unreadable-index paths in :func:`read_tree_nodes`. The message names
    the MEASURED condition (the exception class and text, or the actual root type) rather
    than a generic "could not read" — a degrade whose reason is generic is only marginally
    better than the silent failure it replaces (guard-1946).
    """
    if status is not None:
        status["degraded"] = True
        status["store"] = "tree"
        status["path"] = str(path)
        status["error"] = error
    print(
        f"[knowledge-export] WARNING: tree index at {path} is unreadable ({error}). "
        f"Exporting tree=0 and marking the bundle degraded so the failure is visible to a "
        f"reader rather than hidden behind a stale bundle.",
        file=sys.stderr,
    )
    return []


def read_tree_nodes(
    world_path: Path, *, status: dict[str, object] | None = None
) -> list[dict[str, object]]:
    """Read ``_tree.yaml`` into node dicts shaped for :func:`project`.

    Uses the index-level ``summary`` (already in the tree yaml) and a humanized key for the
    title. ``category`` is taken from the node ``file`` path so the projection's
    system/-subtree suppression works on the reliable path segment. Each DOMAIN node also
    carries its full ``.md`` ``body`` (front matter stripped, capped) so the kid-facing UI
    can render the article on click; framework (``system/``) node bodies are NOT read — the
    projection suppresses that subtree anyway, so skipping the read keeps framework bodies
    out of memory entirely (defense in depth) and halves the per-export file reads.

    Two index shapes beyond the canonical one are handled, both measured live across 18
    sidecar worlds (g-368-53, filed off g-369-49):

    * A **flat string mapping** (``<key>: "<filename>.md"``) instead of a node mapping —
      2 of 18 indexes. Coerced to ``{"file": <str>}``; everything downstream already
      derives category and body from ``file``, and ``top_level_category`` strips the
      extension, so a coerced node classifies exactly like a canonical one (verified: the
      framework root ``system.md`` still reads non-domain, so the coercion cannot leak a
      framework body).
    * An **unparseable or non-mapping index** — 5 of 18. Reported through ``status``
      rather than raised, so the caller writes a bundle that SAYS it is degraded instead
      of dying and leaving the previous (hollow) bundle in place, where a malformed index
      is byte-indistinguishable from a healthy no-op.

    ``status``, when a dict is passed, is populated in place on the degraded path and left
    untouched on the healthy one — so ``status.get("degraded")`` is the caller's test.
    """
    tree_dir = _resolve_tree_dir(world_path)
    if tree_dir is None:
        return []
    tree_yaml = tree_dir / "_tree.yaml"
    # DEFECT C (g-368-53). The exception classes are deliberate and narrow (guard-373 —
    # never a blanket ``except Exception``, which would mask a bug in this reader as a
    # benign degrade):
    #   * ``yaml.YAMLError``     — the ScannerError/ParserError parent (a stray backtick,
    #                              an unquoted structural marker; guard-610 is the WRITE
    #                              side of the same defect).
    #   * ``UnicodeDecodeError`` — corrupt bytes. It is a ``ValueError``, NOT an
    #                              ``OSError``, so an ``(OSError, YAMLError)`` tuple would
    #                              let it escape silently (guard-2441).
    #   * ``OSError``            — the index vanished or is unreadable between the
    #                              ``is_file()`` probe in ``_resolve_tree_dir`` and here.
    try:
        data = yaml.safe_load(tree_yaml.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return _degrade(status, tree_yaml, f"{type(exc).__name__}: {exc}")
    # ``safe_load(x) or {}`` does NOT cover valid-YAML-that-is-not-a-mapping: ``or``
    # substitutes only on a falsey value, so a non-empty list or a bare scalar reaches
    # ``.get`` and raises ``AttributeError``. Guard it STRUCTURALLY rather than catching
    # that AttributeError — catching it would make a bug in this function
    # indistinguishable from the malformed input it guards against (guard-2441,
    # guard-1946).
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return _degrade(
            status, tree_yaml, f"index root is {type(data).__name__}, expected a mapping"
        )
    # DEFECT F1/F3 (g-368-55). The root guard above stopped ONE LEVEL SHORT: ``nodes``
    # itself was unguarded, so ``nodes: 42`` and ``nodes: true`` reached ``for n in
    # nodes`` and raised TypeError — the die-instead-of-degrade behaviour DEFECT C was
    # written to eliminate, surviving one level down — while ``nodes: "a-string"``
    # iterated its CHARACTERS and rendered as a clean empty world. Guard it
    # STRUCTURALLY, exactly as the root is guarded and for the same reason: catching
    # the TypeError would make a bug in this reader indistinguishable from the
    # malformed input it guards against (guard-2441, guard-1946).
    #
    # An ABSENT ``nodes`` key is NOT malformed — an index with no nodes is an honest
    # empty world — so only ``None`` becomes ``{}``. ``{}`` and ``[]`` pass the
    # isinstance check and yield zero naturally. Every other type (str, int, bool,
    # float) degrades with a NAMED cause, because those render identically to an empty
    # world and a reader has no way to tell them apart otherwise. Note ``or {}`` was
    # the original and cannot do this job: it substitutes on any falsey value, so
    # ``nodes: false`` and ``nodes: 0`` would pass as "empty" while being malformed.
    nodes = data.get("nodes")
    if nodes is None:
        nodes = {}
    if not isinstance(nodes, (dict, list)):
        return _degrade(
            status,
            tree_yaml,
            f"index `nodes` is {type(nodes).__name__}, expected a mapping or a list",
        )
    out: list[dict[str, object]] = []
    if isinstance(nodes, dict):
        items = nodes.items()
    else:  # tolerate a list-of-nodes shape
        # DEFECT F2 (g-368-55): this generator used to filter with ``if isinstance(n,
        # dict)``, which dropped every element BEFORE the string coercion below could
        # reach it — so a list of filename STRINGS (the list analogue of the flat
        # mapping DEFECT B exists to handle) exported zero nodes from a perfectly
        # healthy index. Yield every element and let the ONE coercion + guard in the
        # loop body decide, so the dict and list shapes cannot diverge again.
        items = (
            (str(n.get("key") or "") if isinstance(n, dict) else "", n) for n in nodes
        )
    for key, node in items:
        # DEFECT B (g-368-53): a flat ``<key>: "<filename>.md"`` index maps every node to a
        # STRING, so the guard below dropped all of them and the export read tree=0 while
        # the index was perfectly healthy. Coerce before the guard, not inside it.
        if isinstance(node, str):
            node = {"file": node}
            # A LIST element carries no mapping key to supply one, so derive it from
            # the filename — the list analogue of what the flat mapping gives for free
            # (F2, g-368-55). Without this every list-of-strings node falls to
            # ``_okf_safe_key``'s "node" fallback and they collide into
            # node/node-2/node-3 with empty titles, which is "not dropped" only in the
            # narrowest sense. ``key or`` so the dict branch's real key always wins.
            key = key or Path(str(node["file"])).stem
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
                # Present on 1379/1379 nodes in the live index (measured 2026-08-12).
                # DATE-ONLY ("2026-04-28"), never an instant — so a consumer computing
                # "changed since X" has ONE-DAY granularity and must compare dates, not
                # timestamps. Treating this as midnight and comparing against an instant
                # silently drops every same-day change, which for a what-changed view
                # means hiding exactly the newest learning (g-335-1146).
                "last_updated": str(node.get("last_updated") or ""),
            }
        )
    return out


def _agent_names(project_root: Path) -> list[str]:
    """Agent directory names under ``agents/`` — redacted to "the agent" in output."""
    agents = project_root / AGENTS_PARENT_DIR
    if not agents.is_dir():
        return []
    return [p.name for p in agents.iterdir() if p.is_dir() and not p.name.startswith(".")]


def _read_self(project_root: Path, env: Mapping[str, str]) -> tuple[dict[str, object], str]:
    """Read the bound agent's ``self.md`` -> ``(front_matter, body)``; ``({}, "")`` on any miss.

    Store I/O only -- the projection/redaction decision lives in
    :func:`knowledge_projection.project_self`, per PEARL 10.3 filter-at-the-source.

    Agent resolution is deliberately narrow and fail-closed. ``MIND_AGENT`` is the
    framework's ONE agent-resolution mechanism (CLAUDE.md "Agent-Session Binding"), so it
    wins. A sidecar environment holds exactly one agent, and that unambiguous case is the
    fallback. Two or more agent dirs with no binding is AMBIGUOUS, and guessing there
    would publish one agent's identity under another's environment -- so it returns
    nothing. An absent, empty or unreadable file returns nothing too: no identity
    published beats a wrong or a hollow one.
    """
    name = str(env.get("MIND_AGENT") or "").strip()
    agents_dir = project_root / AGENTS_PARENT_DIR
    if not name:
        try:
            candidates = [d.name for d in agents_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
        except OSError:
            return {}, ""
        if len(candidates) != 1:
            return {}, ""   # zero or ambiguous -> publish nothing
        name = candidates[0]

    try:
        text = (agents_dir / name / "self.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}, ""

    body = _strip_front_matter(text)
    if body == text:          # no front matter at all
        return {}, body
    lines = text.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, body
    try:
        fm = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), body


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
    tree_status: dict[str, object] | None = None,
) -> ProjectedBundle:
    """Read all four stores from ``world_path`` and project them into a safe bundle.

    ``tree_status`` is an opt-in out-parameter: pass a dict to learn whether the tree
    index was readable (see :func:`read_tree_nodes`). It is keyword-only and defaults to
    ``None`` so every existing caller is byte-identical; the degraded marker is a
    reporting concern of this I/O layer and deliberately does NOT enter the pure
    :class:`ProjectedBundle`, which describes the projection and not how the read went.
    """
    env = dict(os.environ if env is None else env)
    redactor = Redactor(
        agent_names=tuple(_agent_names(project_root)),
        workspace_paths=(str(world_path), str(project_root), *extra_paths),
        secret_values=tuple(_secret_values(env)),
    )
    self_fm, self_body = _read_self(project_root, env)
    return project(
        tree_nodes=read_tree_nodes(world_path, status=tree_status),
        reasoning=_read_jsonl(world_path / "reasoning-bank.jsonl"),
        guardrails=_read_jsonl(world_path / "guardrails.jsonl"),
        hypotheses=_read_jsonl(world_path / "pipeline.jsonl"),
        redactor=redactor,
        self_front_matter=self_fm,
        self_body=self_body,
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
            # `summary` rides in frontmatter, not only as the body fallback below
            # (g-115-3266). It is the ONE projected field this writer used to lose:
            # a node WITH a body rendered `body` and dropped `summary` entirely, so a
            # consumer had no short description to preview a node by without parsing
            # (and truncating) the article. In frontmatter it is machine-addressable.
            # Empty for nodes that have no summary — invariant 5 (consumers tolerate
            # missing optional fields), same as `parent` already is.
            "summary": str(n.get("summary") or ""),
            "parent": n.get("parent") or "",
            "children": list(n.get("children") or []),
            # The second field this writer lost, and it was caught by the guard the
            # first one left behind (test_okf_writer_loses_no_projected_field) rather
            # than by review — the field was invisible to that test until the fixture
            # carried it. Date-only; empty for an undated node, per invariant 5 and
            # because a consumer must be able to tell "no date" from "old" (g-335-1146).
            "last_updated": str(n.get("last_updated") or ""),
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

    # self.md — the identity concept, one file like every other concept, carrying the
    # required `type` discriminator (transfer-bundle-export-shape.md invariant: one
    # concept = one md + a `type`). Written ONLY when the projection is non-empty: an
    # absent file means "no identity published", which is the same signal the JSON
    # payload's `{}` carries, in the shape this format has for absence.
    if bundle.agent_self:
        _self = bundle.agent_self
        _self_fm = {"type": "self"}
        for _k in ("created", "last_updated"):
            if _self.get(_k):
                _self_fm[_k] = str(_self[_k])
        (out_dir / "self.md").write_text(
            f"{_okf_frontmatter(_self_fm)}\n# About this agent\n\n{_self.get('purpose', '')}\n",
            encoding="utf-8",
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
    ]
    if counts.get("self"):
        lines.append("- [About this agent](self.md)")
    lines += [
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


def _write_bundle_atomically(out_path: Path, text: str) -> None:
    """Write the JSON bundle tmp -> fsync -> rename, so a crash cannot truncate it.

    DEFECT F4 (g-368-55). ``Path(out_path).write_text(text)`` is an open-truncate-write:
    a process death or ENOSPC mid-write replaces a GOOD bundle with a truncated,
    unparseable one — strictly worse than the stale bundle it clobbered. The g-368-34
    all-zero refusal cannot catch this, because that refusal runs BEFORE the write
    decides anything. The window is not negligible: a 1248-node bundle carries every
    node body and the sidecar reads it over a network filesystem.

    Routed through the SHARED local-backend writer rather than a hand-rolled
    ``os.replace``, which is exactly what g-115-7257 is open about — OneDrive
    Files-On-Demand reparse points refuse the rename with WinError 5 — and that writer
    owns the retry-with-backoff plus in-place-fallback policy for the case, as a single
    source of truth (guard-1179). ``LocalBackend`` is named EXPLICITLY, never
    ``get_backend()``: see the import comment for the measured own-cloud failure.

    The ``fsync`` is the half the writer does NOT do (it closes the handle, then
    renames). NTFS journals a rename while the data blocks are still unflushed, so a
    renamed file can come back as all-0x00 content — flush+fsync pins the bytes before
    the rename can make them live (guard-1179, ``_fileops.durable_write_text``).

    NOT a claim of reader safety: an atomic rename does not make a process that is
    ALREADY reading the bundle immune to the swap, it only changes the failure
    signature (guard-4299). What it buys is that whatever is on disk is always a
    COMPLETE bundle — the old one or the new one, never a half-written one.
    """

    def _write(handle: object) -> None:
        handle.write(text)  # type: ignore[attr-defined]
        handle.flush()  # type: ignore[attr-defined]
        os.fsync(handle.fileno())  # type: ignore[attr-defined]

    LocalBackend().atomic_write(out_path, _write)


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
    tree_status: dict[str, object] = {}
    bundle = build_bundle(
        world, project_root, extra_paths=(meta,) if meta else (), tree_status=tree_status
    )

    # An all-zero bundle over a world that demonstrably HOLDS knowledge is a broken
    # export, and it is byte-indistinguishable from an honest export of a brand-new world
    # — which is why 17 sidecar envs published empty wikis for weeks with nothing raising a
    # hand. Refuse, loudly, and name the evidence; the caller (knowledge-export.sh) then
    # leaves the previous bundle in place rather than replacing it with nothing. A world
    # with no stores yet still exports its empty bundle, unchanged (g-368-34).
    _counts = bundle.counts()
    if not any(_counts[k] for k in KNOWLEDGE_COUNT_KEYS):
        evidence = world_store_evidence(world)
        # WHEN THE REFUSAL EARNS ITS KEEP, AND WHEN IT ONLY SILENCES (g-368-57).
        # The refusal and the `degraded` marker below serve DIFFERENT consumers: the
        # refusal protects the PUBLISHED artifact (knowledge-export.sh only ``mv``s on
        # rc=0, so a non-zero rc leaves the last good bundle standing), while the marker
        # tells a reader OF a hollow bundle why it is hollow. Refusing unconditionally
        # made the marker unreachable — a malformed tree index zeroes every count (the
        # non-tree stores are gated on the tree walk) and store-evidence counts that same
        # index's bytes, so evidence was truthy on exactly the inputs that set `degraded`
        # and this `return 2` fired ~30 lines above the emit, every time.
        # So refuse on either of two conditions, and only those:
        #   prior_exists — refusing PRESERVES a real previous bundle. Keep doing it; the
        #                  stale `generated_at` on that surviving bundle is itself the
        #                  staleness signal, and replacing good content with a hollow
        #                  marker would trade one detector for another, not add one.
        #   not degraded — we cannot say WHY it is hollow. That unexplained all-zero is
        #                  the silent-corruption case the refusal was built for (17 envs)
        #                  and it must keep refusing whether or not a prior bundle exists.
        # The remaining cell — hollow, cause KNOWN, and nothing to preserve — is where
        # refusing publishes NOTHING and the consumer cannot even tell an export was
        # attempted. There a bundle that names its cause is strictly better than absence,
        # so fall through and let the `degraded` emit below do its job. That is the only
        # behaviour change: a hollow export with a known cause is not an unexplained one.
        prior_exists = bool(out_path) and Path(out_path).exists()
        if evidence and (prior_exists or not tree_status.get("degraded")):
            cause = (
                f" CAUSE: {tree_status.get('error')} (at {tree_status.get('path')})."
                if tree_status.get("degraded")
                else ""
            )
            print(
                f"[knowledge-export] REFUSING to write an all-zero bundle: {world} holds "
                f"readable stores {evidence} but the projection produced "
                f"{bundle.counts()}. This is a broken export, not an empty world. "
                f"Nothing was written.{cause}",
                file=sys.stderr,
            )
            return 2

    if fmt == "okf":
        if not out_dir:
            raise SystemExit("--format okf requires --out-dir <dir>")
        counts = write_okf_bundle(bundle, Path(out_dir))
        print(f"wrote OKF bundle to {out_dir}: {counts}")
        return 0

    payload: dict[str, object] = {
        # First key so a consumer reading the head of the file sees the age
        # immediately. Additive — every existing key keeps its name and shape.
        "generated_at": _generated_at(),
    }
    # ABSENT on a healthy export, so a consumer testing ``"degraded" in bundle`` gets a
    # clean signal and no existing reader sees a new key. Second when present, for the
    # same head-of-file reason ``generated_at`` is first: a hollow bundle must say why it
    # is hollow, or it reads exactly like an honest export of an empty world (g-368-53).
    if tree_status.get("degraded"):
        payload["degraded"] = [tree_status]
    payload.update(
        {
            "counts": bundle.counts(),
            "tree": bundle.tree,
            "hypotheses": bundle.hypotheses,
            "guardrails": bundle.guardrails,
            "lessons": bundle.lessons,
            # The customer-facing identity view. `{}` when nothing is exposable, so a
            # consumer can tell "no identity published" from "published and blank" -- the
            # key is always present, its emptiness is the signal (guard-5144 class: read
            # the content, never mere presence). Projection/redaction happened at the
            # source in knowledge_projection.project_self; this writer only shapes.
            "self": bundle.agent_self,
        }
    )
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if out_path:
        _write_bundle_atomically(Path(out_path), text)
        print(f"wrote {out_path}: {bundle.counts()}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
