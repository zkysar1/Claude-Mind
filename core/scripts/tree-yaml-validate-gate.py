#!/usr/bin/env python3
"""PreToolUse[Write|Edit|MultiEdit] hook - knowledge-tree YAML parse-validation.

Layer-B preventive gate (g-115-1073). Catches the failure mode where a direct
Edit/Write/MultiEdit on a knowledge-tree file (_tree.yaml or a node *.md) would
land YAML that no longer parses - the g-115-1067 / g-115-1070 incident class
where an unquoted multi-line scalar with an internal colon broke tree-read.sh
with a ScannerError and silently disabled ALL tree retrieval until hand-repaired.

tree.py's own writer is parse-safe (test_tree_yaml_colon_emit_safety.py pins the
emitter representer); the gap is the LLM editing a tree file via the Edit/Write
TOOLS, which bypass that emitter. This hook closes that gap at write time.

Design - option (b) of g-115-1073 (VALIDATE, do not restrict the path):
  - Compute the PROPOSED post-edit content (Write: content; Edit: current file
    with old_string -> new_string applied; MultiEdit: edits applied in order).
  - Parse it as YAML (_tree.yaml: whole file; node *.md: front matter only).
  - DENY only when the CURRENT content parses (or the file is new) AND the
    PROPOSED content does NOT - i.e. the edit INTRODUCES a parse break. An edit
    to an already-broken file is allowed (it may be the fix). A parse-clean
    direct edit is allowed (validate, do not restrict).

SAFETY: fail open on ANY error (matches path-resolution-hook.py). exit 0 with
empty stdout = approve. emit_deny() = structured deny per the PreToolUse
contract. Any uncaught exception is swallowed at the bottom catch-all.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_helpers import (  # noqa: E402
    approve_no_mutation,
    emit_deny,
    extract_file_path,
    stdin_json_or_approve,
)

TREE_DIR_MARKER = "knowledge/tree/"
TREE_INDEX_BASENAME = "_tree.yaml"


def _norm(p):
    """Forward-slash + MSYS2->drive normalization, enough to (a) substring-match
    the tree dir and (b) hand a path that Python's open() accepts on Windows.
    Returns "" on falsy input."""
    if not p:
        return ""
    p = p.replace("\\", "/")
    # MSYS2 form: /c/Users/... -> c:/Users/...
    if len(p) >= 3 and p[0] == "/" and p[2] == "/" and p[1].isalpha():
        p = p[1].lower() + ":" + p[2:]
    return p


def _is_tree_file(path):
    """True when path is a knowledge-tree file we validate: _tree.yaml or a
    node *.md living under a knowledge/tree/ directory."""
    np = _norm(path)
    if TREE_DIR_MARKER not in np:
        return False
    base = np.rsplit("/", 1)[-1]
    return base == "_tree.yaml" or base.endswith(".md")


def _front_matter(text):
    """Return the YAML front-matter block of a node .md (text between the first
    '---' line and the next '---' line), or None when no clean block exists.
    Conservative: an unterminated or absent block returns None (nothing to
    validate -> approve), so body-only edits never false-deny."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i])
    return None


def _parses(path, content):
    """True if `content` parses for the tree-file kind at `path`. _tree.yaml is
    parsed whole; a node *.md has only its front matter parsed (None front
    matter -> nothing to validate -> True)."""
    import yaml
    base = _norm(path).rsplit("/", 1)[-1]
    if base == "_tree.yaml":
        blob = content
    else:  # node *.md
        blob = _front_matter(content)
        if blob is None:
            return True
    try:
        yaml.safe_load(blob)
        return True
    except yaml.YAMLError:
        return False


def _duplicate_keys(path, content):
    """Return a Counter of keys appearing MORE THAN ONCE inside the SAME YAML
    mapping, anywhere in the validated blob (nested mappings included).

    WHY _parses CANNOT SEE THIS (g-115-8294). A duplicate key is not a parse
    error: yaml.safe_load accepts it, raises nothing, and silently keeps only
    the LAST occurrence (guard-2388). The file stays valid, every downstream
    read succeeds, and the earlier value is simply gone -- no error, no warning,
    no changelog signal. This gate asked "does it parse", so it was structurally
    blind to the whole class.

    MEASURED 2026-08-29 across 2,940 tree nodes: 19 carried literally repeated
    front-matter keys, worst .../arc-agi-3/arc-environment-models/ls20-class.md
    with `prior_source` SEVEN times. A dict-based writer cannot emit two
    identical keys, so the producer is hand-editing -- exactly the
    CONCURRENT-AUTHORSHIP hazard guard-2388 names: shared front matter
    accumulates keys from multiple agents, and the key you add may already exist
    from a peer edit you never saw.

    Counter rather than a set, so a key going 2 -> 3 still counts as INTRODUCED.
    Fail-open (empty Counter) on any error, matching every other helper here.
    """
    import collections
    import yaml
    base = _norm(path).rsplit("/", 1)[-1]
    if base == TREE_INDEX_BASENAME:
        blob = content
    else:
        blob = _front_matter(content)
        if blob is None:
            return collections.Counter()
    found = []

    class _DupRecordingLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            seen = set()
            for key_node, _value_node in node.value:
                try:
                    k = self.construct_object(key_node, deep=deep)
                except Exception:
                    continue
                try:
                    if k in seen:
                        found.append(str(k))
                    else:
                        seen.add(k)
                except TypeError:
                    continue  # unhashable key -> not our concern
            return super().construct_mapping(node, deep)

    try:
        yaml.load(blob, Loader=_DupRecordingLoader)
    except Exception:
        return collections.Counter()
    return collections.Counter(found)


def _proposed_content(tool_name, tool_input, current):
    """Compute the post-edit file content for the three write tools. Returns
    None when it cannot be computed (caller fails open)."""
    if tool_name == "Write":
        c = tool_input.get("content")
        return c if isinstance(c, str) else None
    if tool_name == "Edit":
        if current is None:
            return None
        old = tool_input.get("old_string")
        new = tool_input.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or old not in current:
            return None  # Edit will fail on its own; nothing to validate
        if tool_input.get("replace_all"):
            return current.replace(old, new)
        return current.replace(old, new, 1)
    if tool_name == "MultiEdit":
        if current is None:
            return None
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return None
        out = current
        for e in edits:
            if not isinstance(e, dict):
                return None
            old = e.get("old_string")
            new = e.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str) or old not in out:
                return None
            if e.get("replace_all"):
                out = out.replace(old, new)
            else:
                out = out.replace(old, new, 1)
        return out
    return None


def _new_nodes_missing_body(tree_yaml_path, current, proposed):
    """: for a _tree.yaml edit, return a list of (key, expected_md_path)
    for node keys ADDED by this edit (present in proposed 'nodes', absent in
    current) whose 'file:' .md body does NOT already exist on disk. Empty list =
    nothing to block. Fail-open (return []) on any error.

    The world root is derived from the edited _tree.yaml path itself
    (<root>/knowledge/tree/_tree.yaml -> <root>), so a node file field
    'world/knowledge/tree/foo/bar.md' resolves to <root>/knowledge/tree/foo/bar.md
    with no _paths dependency. Non-'world/'-prefixed file fields (rare) and
    file-less (interior-only) new nodes are skipped."""
    import yaml
    try:
        np = _norm(tree_yaml_path)
        suffix = "/knowledge/tree/_tree.yaml"
        if not np.endswith(suffix):
            return []
        world_root = np[: -len(suffix)]
        cur = (yaml.safe_load(current) if current else {}) or {}
        prop = (yaml.safe_load(proposed) or {})
        if not isinstance(cur, dict) or not isinstance(prop, dict):
            return []
        cur_nodes = cur.get("nodes", {}) or {}
        prop_nodes = prop.get("nodes", {}) or {}
        new_keys = set(prop_nodes) - set(cur_nodes)
        missing = []
        for k in sorted(new_keys):
            node = prop_nodes.get(k) or {}
            ff = node.get("file") if isinstance(node, dict) else None
            if not ff:
                continue  # no body-bearing file -> no requirement (interior-only)
            p = str(ff).replace("\\", "/")
            if not p.startswith("world/"):
                continue  # non-world path -> fail open (skip)
            md = Path(world_root) / p[len("world/"):]
            if not md.exists():
                missing.append((k, str(md)))
        return missing
    except Exception:
        return []


def main():
    data = stdin_json_or_approve()
    tool_name = data.get("tool_name", "") or ""
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        approve_no_mutation()

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        approve_no_mutation()
    file_path = extract_file_path(tool_input)
    if not file_path or not _is_tree_file(file_path):
        approve_no_mutation()

    open_path = _norm(file_path)
    file_exists = os.path.isfile(open_path)
    current = None
    if file_exists:
        try:
            with open(open_path, "r", encoding="utf-8") as f:
                current = f.read()
        except Exception:
            approve_no_mutation()  # cannot read -> fail open

    proposed = _proposed_content(tool_name, tool_input, current)
    if proposed is None:
        approve_no_mutation()  # cannot compute -> fail open

    # Block ONLY an edit that INTRODUCES a break: the current content must parse
    # (or the file is new). An edit to an already-broken file may be the fix.
    if file_exists and current is not None and not _parses(file_path, current):
        approve_no_mutation()

    # : body-presence gate. A direct _tree.yaml edit that ADDS a node
    # key must have that node's .md body already on disk (write the body FIRST,
    # then add the index entry). Closes the PRODUCER of Pattern-B index-body
    # desync (rb-4597 / ) for the direct-edit path (path 3); the
    # CLI/daemon add-child writers bypass this tool hook. Only meaningful when
    # the proposed content parses (else the parse-break DENY below handles it);
    # the helper fails open on any error.
    if _norm(file_path).rsplit("/", 1)[-1] == "_tree.yaml" and _parses(file_path, proposed):
        missing = _new_nodes_missing_body(file_path, current, proposed)
        if missing:
            listing = "\n".join(f"    - {k}  (expected body: {p})" for k, p in missing[:20])
            more = f"\n    ... and {len(missing) - 20} more" if len(missing) > 20 else ""
            emit_deny(
                f"tree-yaml-validate-gate (Layer B, body-presence) blocked {tool_name} to:\n"
                f"  {file_path}\n"
                f"This edit ADDS {len(missing)} knowledge-tree node key(s) to _tree.yaml whose "
                f".md body does NOT exist on disk:\n"
                f"{listing}{more}\n"
                f"Adding an index entry without its body creates a Pattern-B index-body desync "
                f"(orphaned index entry, silent knowledge hole; rb-4597 / g-115-2886 / g-115-2891).\n"
                f"Fix: WRITE each node .md body FIRST (with its YAML front matter), THEN add the "
                f"_tree.yaml index entry — or use tree.py add-child which couples both writes. "
                f"Re-run this edit once each new node's .md exists."
            )

    # : duplicate-key gate. Runs ONLY when the proposed content parses
    # (an unparseable proposal is handled by the DENY below), and denies ONLY a
    # duplication this edit INTRODUCES -- the same "current must be clean" rule
    # the parse gate uses, so an edit REPAIRING an already-duplicated file is
    # always allowed through.
    if _parses(file_path, proposed):
        import collections as _c
        prop_dups = _duplicate_keys(file_path, proposed)
        cur_dups = (_duplicate_keys(file_path, current)
                    if (file_exists and current) else _c.Counter())
        introduced = {k: n - cur_dups.get(k, 0) for k, n in prop_dups.items()
                      if n > cur_dups.get(k, 0)}
        if introduced:
            listing = "\n".join(
                f"    - {k!r}: this edit adds {n} more occurrence(s); "
                f"the key would appear {prop_dups[k] + 1} times in one mapping"
                for k, n in sorted(introduced.items())[:20])
            emit_deny(
                f"tree-yaml-validate-gate (Layer B, duplicate-key) blocked {tool_name} to:\n"
                f"  {file_path}\n"
                f"This edit would put the SAME key twice in one YAML mapping:\n"
                f"{listing}\n"
                f"That is NOT a parse error -- yaml.safe_load accepts it, raises nothing, and "
                f"silently keeps only the LAST occurrence, so the other value is lost with no "
                f"error, no warning and no changelog signal (guard-2388).\n"
                f"Measured 2026-08-29: 19 of 2,940 tree nodes had already accumulated repeated "
                f"front-matter keys exactly this way, the worst carrying `prior_source` seven "
                f"times.\n"
                f"Fix: grep the block for the key FIRST. To REPLACE the existing value, edit "
                f"that line instead of adding a second one; to keep both, use a distinct key "
                f"or a list value."
            )

    if _parses(file_path, proposed):
        approve_no_mutation()

    # The proposed content would not parse - report the precise YAML error.
    import yaml
    base = _norm(file_path).rsplit("/", 1)[-1]
    kind = "_tree.yaml (whole file)" if base == "_tree.yaml" else "node front matter"
    try:
        blob = proposed if base == "_tree.yaml" else (_front_matter(proposed) or "")
        yaml.safe_load(blob)
        detail = "(parse error not reproducible)"
    except yaml.YAMLError as exc:
        detail = str(exc).replace("\n", " ")[:400]

    reason = (
        f"tree-yaml-validate-gate (Layer B) blocked {tool_name} to:\n"
        f"  {file_path}\n"
        f"The proposed edit would make this knowledge-tree file UNPARSEABLE as "
        f"YAML ({kind}):\n"
        f"  {detail}\n"
        f"A parse break here silently disables ALL tree retrieval until the file "
        f"is hand-repaired (the g-115-1067 / g-115-1070 incident class).\n"
        f"Fix options:\n"
        f"  (a) For a summary change use: bash core/scripts/tree.py update --set "
        f"<node> summary '<value>' - tree.py's emitter quotes structural markers "
        f"(':' '#') safely so a wrap-then-break shape cannot reach disk.\n"
        f"  (b) For a node body / front-matter change, correct the YAML so it "
        f"parses (common cause: an unquoted multi-line value whose wrap puts a "
        f"':' or '#' at the start of a continuation line - single-quote the value).\n"
        f"  (c) This gate VALIDATES, it does not restrict the path: a parse-clean "
        f"direct edit is allowed."
    )
    emit_deny(reason)


try:
    main()
except Exception:
    # Catch-all fail-open: any unexpected error -> approve no mutation.
    pass

sys.exit(0)
