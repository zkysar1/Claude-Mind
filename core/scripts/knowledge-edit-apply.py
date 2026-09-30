#!/usr/bin/env python3
"""Apply one member edit to one learned item ().

The box-side leg of "you own what it learns": ``handle + op + text`` -> resolved item ->
planned change -> the item's own store. The sibling of ``planned-verb-apply.py``, and thin
glue for the same reason: the real work is done by pieces that already exist.

* ``knowledge-export.resolve_item`` turns an opaque published item handle back into
  exactly one item, walking only what the projection exposes (rb-10157);
* ``knowledge_edits.plan_knowledge_edit`` decides what an operation writes, sanitises the
  member's text, and refuses any write to a field the exposure predicate reads;
* each kind's writer below lands the text where the resident reads it on its next run.

WHAT LANDS, PER KIND
  node        The ``.md`` body is replaced. The front matter is kept line for line apart
              from ``last_updated`` and ``last_update_trigger`` (type direct_correction,
              source member-edit), so the edit is attributed to the member and not to the
              node's previous editor (rb-4193). The index ``last_updated`` is bumped too,
              because that is the date the published bundle carries.
  hypothesis  ``claim``, through the pipeline daemon's update-field endpoint.
  guardrail   REFUSED (edit_unsupported). The store makes ``rule`` immutable on purpose
              (guard-6210). The framework's idiom is to supersede (retire, then add), but
              that changes the item's handle and leaves the retired row published, since
              the exposure predicate does not read ``status``. That design is still open.
  forget      REFUSED (forget_pending_ruling) for every kind. Whether a forgotten item's
              text is kept for undo or erased is the owner's decision, so nothing is
              removed before it is made.

Three refusals are specific to a store. Each exists because landing the text would change
more than the member asked for:
  view_truncated      a node body longer than the export's cap. The member saw a cut page,
                      and replacing the whole body would delete a tail they never saw.
  store_would_coerce  a hypothesis text the pipeline endpoint parses to a non-string
                      ("42", "true", "null", a JSON literal): the claim would stop being text.
  too_long_for_store  a hypothesis text whose encoded request line is over what the
                      daemon's HTTP server accepts. That is a 414 on every retry, not a
                      transient failure.

REPORT-ONLY BY DEFAULT. ``--apply`` writes, because this is a write path against a
member's live data and the default output is the plan to read before authorising it.

EXIT CODES
  0  plan produced (``--apply``: the change landed and read back)
  1  a write failed
  2  usage error
  3  refused: every reason above, and every ``plan_knowledge_edit`` refusal

An unresolvable handle is refused as not_addressable and prints nothing on stdout, exactly
like a handle for an item the projection hides.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import sys
import urllib.parse
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import yaml  # noqa: E402

from knowledge_edits import EDIT_FIELDS, plan_knowledge_edit  # noqa: E402
from knowledge_projection import KNOWLEDGE_ITEM_KINDS  # noqa: E402

#: How an edited node's ``last_update_trigger`` records the change. ``direct_correction``
#: is one of the categorisations the tree-edit protocol names (guard-503).
TRIGGER_TYPE = "direct_correction"
TRIGGER_SOURCE = "member-edit"

#: Front-matter keys the node writer restamps. Every other front-matter line is kept.
_STAMPED_KEYS = ("last_updated", "last_update_trigger")

#: The daemon's HTTP server (stdlib ``http.server``) answers 414 to a request line over
#: 65,536 bytes, and pipeline update-field carries the value in the query string. The
#: margin leaves room for the method, the path, the id and the protocol on that line.
QUERY_BUDGET = 60_000


def _load_export_mod():
    """``knowledge-export.py`` is hyphenated, so it needs the importlib shape.

    Same loader ``planned-verb-apply.py`` uses, so the module that resolves a handle here
    is loaded exactly as the one that publishes it.
    """
    path = SCRIPT_DIR / "knowledge-export.py"
    spec = importlib.util.spec_from_file_location("knowledge_export_for_edits", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import plumbing
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _today() -> str:
    """Today's date in UTC. The box that runs the drain may not have ``TZ=UTC`` set."""
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


# --- node -------------------------------------------------------------------


def _node_file(export, world: Path, record: dict) -> tuple[Path, Path] | None:
    """``(tree_dir, path)`` of the node's ``.md``, kept inside the tree, or ``None``."""
    tree_dir = export._resolve_tree_dir(world)
    if tree_dir is None:
        return None
    path = export.node_body_path(tree_dir, str(record.get("category") or ""))
    if path is None:
        return None
    try:
        path.resolve().relative_to(tree_dir.resolve())
    except ValueError:
        return None
    return tree_dir, path


def _split_node(content: str) -> tuple[str, str]:
    """``(front_matter_text, body)``, fenced the way the export strips it.

    A file with no front matter has an empty one. An opening fence that never closes is
    refused: the export would publish the whole file as the body, and there is no safe
    line at which to cut it.
    """
    if not content.startswith("---"):
        return "", content
    lines = content.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    raise ValueError("front matter opens but never closes")


def restamp_front_matter(fm_text: str, today: str, session: str | None) -> str:
    """Rewrite ``last_updated`` and ``last_update_trigger``, keeping every other line.

    guard-1092: the result is re-parsed. It must equal the original mapping on every other
    key and carry exactly the new values on these two, or ``ValueError`` is raised and
    nothing is written.
    """
    before = yaml.safe_load(fm_text) if fm_text.strip() else {}
    if not isinstance(before, dict):
        raise ValueError("front matter is not a mapping")

    kept: list[str] = []
    skipping = False
    for line in fm_text.split("\n"):
        # A non-blank line at column 0 opens a top-level entry; it and its indented
        # continuation are dropped when it is one of the stamped keys.
        if line.strip() and line[:1] not in (" ", "\t"):
            skipping = line.split(":", 1)[0].strip() in _STAMPED_KEYS
        if not skipping:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    kept += [
        f"last_updated: {today}",
        "last_update_trigger:",
        f"  type: {TRIGGER_TYPE}",
        f"  source: {TRIGGER_SOURCE}",
        f"  session: {json.dumps(session) if session else 'null'}",
    ]
    new_text = "\n".join(kept)

    after = yaml.safe_load(new_text)
    if not isinstance(after, dict):
        raise ValueError("restamped front matter is not a mapping")

    def rest(d: dict) -> dict:
        return {k: v for k, v in d.items() if k not in _STAMPED_KEYS}

    if rest(after) != rest(before):
        raise ValueError("restamping changed a front-matter key it does not own")
    trigger = {"type": TRIGGER_TYPE, "source": TRIGGER_SOURCE, "session": session}
    if str(after.get("last_updated")) != today or after.get("last_update_trigger") != trigger:
        raise ValueError("restamped front matter does not carry the new stamp")
    return new_text


def _bump_index_last_updated(tree_dir: Path, key: str, today: str) -> None:
    """Bump the node's ``last_updated`` in the index, the date the published bundle carries.

    The same change ``tree-front-matter-sync.py`` makes after a tool edit, through the same
    locked writer, but on the index this world actually has (``_resolve_tree_dir``), so the
    sidecar layout is covered. Best effort: the body is the edit itself.
    """
    from _fileops import locked_modify_yaml  # noqa: PLC0415 - keeps the dry run light

    def _do(data):
        if isinstance(data, dict):
            nodes = data.get("nodes")
            if isinstance(nodes, dict) and isinstance(nodes.get(key), dict):
                nodes[key]["last_updated"] = today
                data["last_updated"] = today
        return data

    try:
        locked_modify_yaml(str(tree_dir / "_tree.yaml"), _do)
    except Exception as exc:  # noqa: BLE001 - the index stamp must not undo the edit
        sys.stderr.write(f"knowledge-edit-apply: index last_updated not bumped for {key}: {exc}\n")


def _write_node(export, world: Path, item_id: str, record: dict, text: str) -> int:
    located = _node_file(export, world, record)
    if located is None:
        sys.stderr.write(f"knowledge-edit-apply: no file inside the tree for node {item_id}\n")
        return 1
    tree_dir, path = located
    today = _today()
    try:
        raw = path.read_bytes()
        # Keep the file's own line ending, as tree-front-matter-sync.py does.
        lineend = "\r\n" if b"\r\n" in raw else "\n"
        fm_text, _old_body = _split_node(raw.decode("utf-8").replace("\r\n", "\n"))
        fm = restamp_front_matter(fm_text, today, os.environ.get("MIND_SID", "").strip() or None)
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        sys.stderr.write(f"knowledge-edit-apply: write failed for node {item_id}: {exc}\n")
        return 1

    out = f"---\n{fm}\n---\n\n{text}\n".replace("\n", lineend).encode("utf-8")
    tmp = path.with_name(path.name + ".tmp-member-edit")
    try:
        tmp.write_bytes(out)
        os.replace(tmp, path)
        landed = path.read_bytes() == out
    except OSError as exc:
        sys.stderr.write(f"knowledge-edit-apply: write failed for node {item_id}: {exc}\n")
        return 1
    if not landed:
        sys.stderr.write(f"knowledge-edit-apply: node {item_id} did not read back as written\n")
        return 1
    _bump_index_last_updated(tree_dir, item_id, today)
    return 0


# --- hypothesis ---------------------------------------------------------------


def store_would_coerce(text: str) -> bool:
    """True when pipeline update-field would store ``text`` as something other than text.

    Mirrors ``_parse_value`` in the daemon's pipeline writer, and the tests pin the two
    together: a claim of "42", "true", "null" or a JSON literal comes back as a number, a
    boolean, nothing, or an object.
    """
    if text in ("true", "false", "null", "[]"):
        return True
    if text[:1] in ("{", "["):
        try:
            json.loads(text)
            return True
        except ValueError:
            pass
    for convert in (int, float):
        try:
            convert(text)
            return True
        except ValueError:
            pass
    return False


def _claim_query(item_id: str, text: str) -> str:
    """The update-field query, encoded as the ``.sh`` wrapper encodes it."""
    return "&".join(
        f"{k}={urllib.parse.quote(v, safe='')}"
        for k, v in (("id", item_id), ("field", "claim"), ("value", text))
    )


def _write_hypothesis(export, world: Path, item_id: str, record: dict, text: str) -> int:
    # guard-555: a Python caller reaches a daemon-only write through _rt, never a nested
    # bash. It also keeps a text that starts with "-" from being read as a flag.
    import _rt  # noqa: PLC0415 - lazy, like the drain's directive path

    try:
        body = _rt.rt_call("POST", "/v1/pipeline/update-field", query=_claim_query(item_id, text))
        resp = json.loads(body)
    except Exception as exc:  # noqa: BLE001 - RtError, transport and decode alike
        sys.stderr.write(f"knowledge-edit-apply: write failed for hypothesis {item_id}: {exc}\n")
        return 1
    # A 2xx is not evidence the stored claim is the member's text: read it back from the
    # record the endpoint returns.
    stored = resp.get("record") or resp if isinstance(resp, dict) else {}
    if not isinstance(stored, dict) or stored.get("claim") != text:
        sys.stderr.write(f"knowledge-edit-apply: hypothesis {item_id} claim did not read back as written\n")
        return 1
    return 0


# --- dispatch -----------------------------------------------------------------

_WRITERS = {"node": _write_node, "hypothesis": _write_hypothesis}


def store_refusal(export, world: Path, kind: str, item_id: str, record: dict, text: str) -> str | None:
    """A refusal only the item's own store can explain, or ``None``. Runs before any write."""
    if kind not in _WRITERS:
        return "edit_unsupported"
    if kind == "node":
        located = _node_file(export, world, record)
        if located is not None:
            try:
                body = export._strip_front_matter(located[1].read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                body = ""  # the writer reports an unreadable file as a failed write
            if len(body) > export._NODE_BODY_CAP:
                return "view_truncated"
    if kind == "hypothesis":
        if store_would_coerce(text):
            return "store_would_coerce"
        if len(_claim_query(item_id, text)) > QUERY_BUDGET:
            return "too_long_for_store"
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Apply one member edit to one learned item.")
    ap.add_argument("--handle", required=True, help="opaque published item handle")
    ap.add_argument("--op", required=True, help="edit | forget")
    ap.add_argument("--text", default="", help="the member's replacement text (edit only)")
    ap.add_argument("--apply", action="store_true", help="perform the write")
    ap.add_argument("--json", action="store_true", help="machine-readable plan")
    args = ap.parse_args(argv)

    export = _load_export_mod()
    world = export._resolve_world()
    hit = export.resolve_item(world, args.handle)
    # An unresolved handle has no kind. Any valid kind gets the core's own not_addressable
    # refusal, which by design never tells one miss from another.
    kind, item_id, record = hit if hit else (KNOWLEDGE_ITEM_KINDS[0], "", None)
    plan = plan_knowledge_edit(kind, record, args.op, args.text)
    refusal = plan.refusal
    if refusal is None and plan.remove:
        refusal = "forget_pending_ruling"
    field = EDIT_FIELDS.get(kind, "")
    text = str(plan.writes.get(field, ""))
    if refusal is None:
        refusal = store_refusal(export, world, kind, item_id, record, text)
    if refusal:
        sys.stderr.write(f"knowledge-edit-apply: refused ({refusal})\n")
        return 3

    out = {"kind": kind, "id": item_id, "op": "edit", "field": field, "chars": len(text),
           "applied": False}
    if args.apply:
        if _WRITERS[kind](export, world, item_id, record, text) != 0:
            return 1
        out["applied"] = True

    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(f"{kind} {item_id}: edit -> {field} ({len(text)} chars)"
              + ("" if args.apply else "   (dry run — pass --apply)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
