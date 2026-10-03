#!/usr/bin/env python3
"""Apply one member edit to one learned item ().

The box-side leg of "you own what it learns": ``handle + op + text + base`` -> resolved item ->
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
  guardrail   SUPERSEDED. The store makes ``rule`` immutable on purpose (guard-6210), so
              the correction lands the framework's own way: a successor guardrail is
              added with the member's text as its rule, then the old one is retired and
              its ``retirement_reason`` names the successor. The member's next view shows
              the successor under a NEW handle (a handle is keyed on the id) and no longer
              shows the old rule, which the exposure predicate hides once retired.
  forget      node: SOFT. The page is kept in a retention record outside the world
              (``knowledge_retention``, verified by read-back before anything else is touched),
              the node is dropped from the index, and its file is blanked in place. The index
              is the one list the member's view, the handle resolver and the resident's
              retrieval all walk, so dropping the entry hides the node from all three at once,
              and the blanked file leaves the text in exactly one place. A file is never
              unlinked: a plain delete does not stick on a synced store, and the text would
              come back as an orphan. If the blanking fails the forget still stands, and the
              report says ``blanked: False``.
              hypothesis: SOFT, and the record STAYS. A pipeline record is never deleted and its
              lifecycle only moves forward, so the stage is left alone: an archive would copy the
              text into an append-only file and could not be undone. The statement the member
              saw (``knowledge_projection.item_text_fields``: ``claim`` and ``title``) is
              retained and read back first, ``forgotten_at`` is stamped, which is the one field
              the exposure predicate reads to hide the record from the member's view and the
              handle resolver, and the statement is then blanked in place. The record's
              supporting free text (rationale, position, resolution notes) is not blanked, and
              is residue for the 30-day erase. If a blank fails the forget still stands, and the
              report says ``blanked: False``.
              guardrail: SOFT, and the record STAYS. The rule is retained and read back first,
              then the guardrail is retired with a ``retirement_reason`` that starts "Forgotten
              by the member". The export and retrieval both publish active guardrails only, so
              retiring hides it from the member and the resident at once, and the store's merge
              keeps a retirement (retired dominates), so a stale copy cannot bring the rule
              back. The rule is NOT blanked at the forget: it is immutable (guard-6210) because
              a rule edited in place forks the record at the next cross-box merge, so the
              retired record keeps the text until the 30-day erase (``knowledge_erase``). That
              erase blanks it through the store's erase mode, which only a local backend allows
              (``_write_guardrail_field``), and on any other leaves it, counted pending. The
              report says ``blanked: False``.
  undo        node: the retained page goes back to the file it came from and its entry back
              into the index, in the slot it left, from the retention record the handle names.
              The file is restored BEFORE the entry, because an entry with no file behind it is
              a phantom node (guard-4836). Undo addresses an item the exposure predicate no
              longer shows, so it resolves from retention, not from the projection.
              hypothesis: ``restored_at`` is stamped FIRST, which is what lets the store merge
              rank the undo above a stale copy of the forget. The retained statement then goes
              back into the record and the marker is cleared LAST, so the item is never visible
              half restored.
              guardrail: the rule comes back as a NEW guardrail, the way a correction lands
              (tag ``restores:<id>``, the report's ``restored_as`` and new ``handle``). A
              retirement is terminal in the store's merge, so the retired record cannot be
              reactivated.

Six refusals are specific to a store. Each exists because landing the text would change
more than the member asked for:
  view_truncated      a node body longer than the export's cap. The member saw a cut page,
                      and replacing the whole body would delete a tail they never saw.
  view_redacted       a node body, hypothesis claim or guardrail rule that the published
                      view altered: a path, a name, an id or a secret replaced, or spaces
                      collapsed. The member corrected the altered text, so landing it would
                      drop what the redaction hid. The export marks the safe rows
                      ``unredacted`` for the member UI to gate on; this is the box-side
                      backstop.
  view_base_missing   no ``--base``, or one that is not a SHA-256 hex digest. ``base`` is the
                      digest of the view the member corrected
                      (``knowledge_projection.view_digest``). Without it the box cannot
                      tell whether that view is the text it holds now.
  view_stale          a ``base`` that is not the digest of the item's view now. The text
                      changed after the member saw it (the resident edited it, or this box
                      redacts it differently from the box that published it), so the
                      correction would overwrite text the member never saw.
  store_would_coerce  a hypothesis text the pipeline endpoint parses to a non-string
                      ("42", "true", "null", a JSON literal): the claim would stop being text.
  too_long_for_store  a hypothesis text whose encoded request line is over what the
                      daemon's HTTP server accepts. That is a 414 on every retry, not a
                      transient failure.

Seven more belong to forget and undo. Each is a refusal because the write could not be made
safely, never because the member asked for too much:
  no_retention_store  no ``--retention-dir``. The retained text would have nowhere to live
                      outside the world, and a forget with no way back is not the soft forget
                      that was ruled. The caller supplies the directory; nothing here derives
                      one.
  forget_not_a_leaf   the node has children, and dropping it would orphan them.
  index_unsupported   the index is not a mapping that holds the node (an unreadable file, or a
                      list of nodes). This writer edits only a shape it can restore.
  undo_expired        the retention window has passed.
  undo_conflict       the index holds that key again, or a page was written at the node's file
                      since the forget, so restoring would overwrite what the member never saw.
  undo_parent_missing the node's parent is gone, and restoring would strand it.
  not_restorable      a guardrail that lacks a field the store requires to add one (category or
                      trigger_condition), or holds a carried field in a type the live store never
                      has, so an undo could not add the rule back.
A hypothesis forget can also be refused ``store_would_coerce`` or ``too_long_for_store``,
for a statement the pipeline's update-field could not put back as the same text. A forget
that cannot be undone is not the soft forget that was ruled. An undo of a hypothesis is
refused ``undo_conflict`` when the record is not marked forgotten, or when a statement field
holds neither the retained text nor the forget's marker text. An undo of a guardrail is
refused ``undo_conflict`` when the record is not retired with a forget's reason, or its rule
is not the retained one.

REPORT-ONLY BY DEFAULT. ``--apply`` writes, because this is a write path against a
member's live data and the default output is the plan to read before authorising it.

EXIT CODES
  0  plan produced (``--apply``: the change landed and read back)
  1  a write failed
  2  usage error
  3  refused: every reason above, and every ``plan_knowledge_edit`` refusal

An unresolvable handle is refused as not_addressable and prints nothing on stdout, exactly
like a handle for an item the projection hides. So is an undo whose retained record is
unusable: its text fails its digest, it names no item, or it names a file outside the tree.

A caller in the same process (the inbound drain) passes ``main`` a ``report`` dict and reads
the outcome there instead of parsing output: the refusal code under ``refused``, or the plan
and what landed (a guardrail's ``superseded_by`` or ``restored_as`` and new ``handle``).
Output is unchanged.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import hmac
import importlib.util
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import yaml  # noqa: E402

import knowledge_retention  # noqa: E402
from knowledge_edits import EDIT_FIELDS, plan_knowledge_edit  # noqa: E402
from knowledge_projection import (  # noqa: E402
    FORGOTTEN_FIELD,
    KNOWLEDGE_ITEM_KINDS,
    RESTORED_FIELD,
    is_active_guardrail,
    is_forgotten,
    is_unredacted,
    item_handle,
    item_text,
    item_text_fields,
    resolve_item_handle,
    view_digest,
)

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

#: A well-formed ``--base``: a SHA-256 digest in hex, read case-insensitively.
_BASE_RE = re.compile(r"[0-9a-f]{64}")


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


def _export_redactor(export, world: Path):
    """The redactor the export CLI publishes with: this world, the repo root, META_PATH and
    the environment, as ``knowledge-export.py`` main() passes them."""
    meta = os.environ.get("META_PATH")
    return export._build_redactor(world, SCRIPT_DIR.parents[1], (meta,) if meta else (),
                                  dict(os.environ))


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


def _write_node(export, world: Path, item_id: str, record: dict, text: str) -> dict | None:
    located = _node_file(export, world, record)
    if located is None:
        sys.stderr.write(f"knowledge-edit-apply: no file inside the tree for node {item_id}\n")
        return None
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
        return None

    out = f"---\n{fm}\n---\n\n{text}\n".replace("\n", lineend).encode("utf-8")
    tmp = path.with_name(path.name + ".tmp-member-edit")
    try:
        tmp.write_bytes(out)
        os.replace(tmp, path)
        landed = path.read_bytes() == out
    except OSError as exc:
        sys.stderr.write(f"knowledge-edit-apply: write failed for node {item_id}: {exc}\n")
        return None
    if not landed:
        sys.stderr.write(f"knowledge-edit-apply: node {item_id} did not read back as written\n")
        return None
    _bump_index_last_updated(tree_dir, item_id, today)
    return {}


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


def _field_query(item_id: str, field: str, value: str) -> str:
    """The update-field query for one field, encoded as the ``.sh`` wrapper encodes it."""
    return "&".join(
        f"{k}={urllib.parse.quote(v, safe='')}"
        for k, v in (("id", item_id), ("field", field), ("value", value))
    )


def _claim_query(item_id: str, text: str) -> str:
    """The update-field query for a claim edit."""
    return _field_query(item_id, "claim", text)


def _write_hypothesis(export, world: Path, item_id: str, record: dict, text: str) -> dict | None:
    # guard-555: a Python caller reaches a daemon-only write through _rt, never a nested
    # bash. It also keeps a text that starts with "-" from being read as a flag.
    import _rt  # noqa: PLC0415 - lazy, like the drain's directive path

    try:
        body = _rt.rt_call("POST", "/v1/pipeline/update-field", query=_claim_query(item_id, text))
        resp = json.loads(body)
    except Exception as exc:  # noqa: BLE001 - RtError, transport and decode alike
        sys.stderr.write(f"knowledge-edit-apply: write failed for hypothesis {item_id}: {exc}\n")
        return None
    # A 2xx is not evidence the stored claim is the member's text: read it back from the
    # record the endpoint returns.
    stored = resp.get("record") or resp if isinstance(resp, dict) else {}
    if not isinstance(stored, dict) or stored.get("claim") != text:
        sys.stderr.write(f"knowledge-edit-apply: hypothesis {item_id} claim did not read back as written\n")
        return None
    return {}


# --- guardrail ----------------------------------------------------------------

#: What a superseding guardrail keeps of the one it replaces: WHEN the rule applies. The
#: category also keeps it in the exposed set. The member corrected the rule itself, so the
#: fields that restate or extend the old text (``title``, ``action_hint``) are not carried:
#: the member never saw them, and carried over they could put back what the member
#: corrected. Nor is ``severity``, the resident's rating of the old text. The retired
#: record keeps all of them.
_SUCCESSOR_CARRIES = ("category", "trigger_condition", "trigger_pattern", "context_triggers",
                      "phases", "when_to_use")

#: The tag prefix that names the guardrail a member's correction superseded.
_SUPERSEDES_TAG = "supersedes:"

#: The reason a correction retires the guardrail it replaced with. The erase sweep reads it to tell
#: a predecessor this module retired from one something else retired, so it is one string here.
_SUPERSEDED_REASON = "superseded by {new_id}: a member corrected this rule"


def _guard_store(path: str, query: str, body: dict | None = None) -> dict:
    """POST one guardrails-store call; return the record the store wrote."""
    import _rt  # noqa: PLC0415 - lazy, like the hypothesis writer (guard-555)

    resp = json.loads(_rt.rt_call("POST", path, query=query,
                                  body=None if body is None else json.dumps(body)))
    stored = resp.get("record") or resp if isinstance(resp, dict) else {}
    return stored if isinstance(stored, dict) else {}


def _tags(record: dict) -> list:
    """A record's tags, or ``[]`` when the field is not a list (a string would make
    ``in`` a substring test)."""
    tags = record.get("tags")
    return tags if isinstance(tags, list) else []


def _retire_guardrail(item_id: str, reason: str) -> dict:
    """Retire one guardrail with ``reason``; return the record the last write left.

    ``status`` is written last, so a failure part way leaves the guardrail published, never
    retired without its reason."""
    retired: dict = {}
    for field, value in (("retirement_reason", reason), ("retirement_date", _today()),
                         ("status", "retired")):
        retired = _guard_store("/v1/store/set-field", "&".join(
            f"{k}={urllib.parse.quote(v, safe='')}"
            for k, v in (("store", "guardrails"), ("id", item_id), ("field", field), ("value", value))))
    return retired


def _write_guardrail_field(item_id: str, field: str, value: str | list | dict) -> str:
    """One guardrails-store set-field write for the erase sweep: ``"ok"`` only when the record the
    store returns holds ``value``. A statement field (the rule) is written in erase mode, the one
    write the store lets change an immutable field, and the store refuses it on a record that is
    not retired and on a backend that is not one box's. ``"not_local"`` is that second refusal: an
    obligation this box cannot meet, not a failure to retry. A list or a mapping goes as its JSON,
    which the store parses back to the same container.

    A 2xx is not evidence the value landed, the rule :func:`_write_hypothesis_field` applies too.
    """
    import _rt  # noqa: PLC0415 - lazy, like the other daemon writers (guard-555)

    wire = value if isinstance(value, str) else json.dumps(value)
    params = [("store", "guardrails"), ("id", item_id), ("field", field), ("value", wire)]
    if field in item_text_fields("guardrail"):
        params.append(("erase", "1"))
    query = "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in params)
    try:
        resp = json.loads(_rt.rt_call("POST", "/v1/store/set-field", query=query))
    except Exception as exc:  # noqa: BLE001 - RtError, transport and decode alike
        if isinstance(exc, _rt.RtError) and "erase_not_local" in f"{exc.body or ''} {exc}":
            return "not_local"
        sys.stderr.write(f"knowledge-edit-apply: write failed for guardrail {item_id} {field}: {exc}\n")
        return "failed"
    stored = resp.get("record") or resp if isinstance(resp, dict) else {}
    if not isinstance(stored, dict) or stored.get(field) != value:
        sys.stderr.write(f"knowledge-edit-apply: guardrail {item_id} {field} did not read back as written\n")
        return "failed"
    return "ok"


def _write_guardrail(export, world: Path, item_id: str, record: dict, text: str) -> dict | None:
    """Supersede: add a guardrail carrying the member's rule, then retire this one.

    ``rule`` cannot change in place (guard-6210), so the correction lands the way the
    framework corrects its own rules: a successor with the member's text, and the old rule
    retired with ``retirement_reason`` naming it, so a citation of the old id still leads
    to the rule in force (guard-5936: the pointer goes in a field the store keeps).
    ``status`` is written last, so a failure part way leaves both rules published, never
    neither.

    The add sends ``allow_near_dup``. A correction restates the rule it corrects, so the
    store's near-duplicate refusal would fire on exactly that rule and credit it with a
    use it never had.

    Safe to re-run after a partial failure. The drain leaves a failed record in
    processing/ for an operator to requeue and never retries it itself. An active
    successor already tagged for this guardrail and carrying this text is reused, not
    added again.
    """
    tag = f"{_SUPERSEDES_TAG}{item_id}".lower()
    successor = next((g for g in reversed(export._read_jsonl(world / "guardrails.jsonl"))
                      if is_active_guardrail(g) and g.get("rule") == text and tag in _tags(g)),
                     None)
    try:
        if successor is None:
            # By key presence, the test the store applies to required fields: a present
            # but empty trigger_condition is still the old rule's, and dropping it fails the add.
            carried = {k: record[k] for k in _SUCCESSOR_CARRIES if k in record}
            # A carried supersedes tag would name a guardrail this one never replaced.
            tags = [t for t in _tags(record)
                    if isinstance(t, str) and not t.lower().startswith(_SUPERSEDES_TAG)]
            successor = _guard_store("/v1/store/append", "store=guardrails", {
                **carried, "rule": text, "source": TRIGGER_SOURCE,
                "tags": [*tags, TRIGGER_SOURCE, tag], "allow_near_dup": True})
            if (successor.get("rule") != text or not is_active_guardrail(successor)
                    or not successor.get("id")):
                sys.stderr.write(f"knowledge-edit-apply: guardrail {item_id} successor did not read back as written\n")
                return None
        new_id = str(successor.get("id") or "")
        retired = _retire_guardrail(item_id, _SUPERSEDED_REASON.format(new_id=new_id))
    except Exception as exc:  # noqa: BLE001 - RtError, transport and decode alike
        sys.stderr.write(f"knowledge-edit-apply: write failed for guardrail {item_id}: {exc}\n")
        return None
    if retired.get("status") != "retired":
        sys.stderr.write(f"knowledge-edit-apply: guardrail {item_id} did not read back as retired\n")
        return None
    handle = item_handle("guardrail", new_id, os.environ.get(export._GOAL_HANDLE_SECRET_VAR, ""),
                         os.environ.get("ENVIRONMENT_ID", ""))
    return {"superseded_by": new_id, "handle": handle}


# --- forget and undo (node) ---------------------------------------------------

#: What a forgotten node's file is left holding. Nothing of the original survives in it, not
#: even the front matter, whose ``topic`` would still name what the member forgot.
_TOMBSTONE = "---\ntopic: forgotten\n---\n\nForgotten by the member on {today}.\n"
_TOMBSTONE_HEAD = _TOMBSTONE.split("\n\n", 1)[0] + "\n"


def _mapping_nodes(tree_dir: Path) -> dict | None:
    """The index's ``nodes`` mapping as the file holds it now, or ``None`` for a shape this
    writer does not edit: an unreadable index, or a list of nodes."""
    try:
        data = yaml.safe_load((tree_dir / "_tree.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    nodes = data.get("nodes") if isinstance(data, dict) else None
    return nodes if isinstance(nodes, dict) else None


def _entry_file(entry) -> str:
    """The ``file`` of an index entry: a mapping's field or, in a flat index, the string."""
    if isinstance(entry, dict):
        return str(entry.get("file") or "")
    return entry if isinstance(entry, str) else ""


def _parent_slot(nodes: dict, key: str, entry) -> tuple[str | None, int | None]:
    """``(parent, position)``: the parent the entry names, and where that parent lists it."""
    parent = entry.get("parent") if isinstance(entry, dict) else None
    if not isinstance(parent, str) or not parent:
        return None, None
    holder = nodes.get(parent)
    kids = holder.get("children") if isinstance(holder, dict) else None
    return parent, (kids.index(key) if isinstance(kids, list) and key in kids else None)


def _index_snapshot(tree_dir: Path, key: str):
    """``(entry, parent, position)`` for ``key`` as the index holds it now, or ``None``."""
    nodes = _mapping_nodes(tree_dir)
    if nodes is None or key not in nodes:
        return None
    entry = copy.deepcopy(nodes[key])
    return (entry, *_parent_slot(nodes, key, entry))


def _drop_index_entry(tree_dir: Path, key: str, entry, parent: str | None, today: str) -> bool:
    """Remove ``key`` from the index and from its parent's children, when its entry is still
    exactly the one that was retained.

    ``False`` writes nothing: the index moved on, and a retained copy of a page that has
    since changed is not a copy of what the member forgot. A parent left with no children
    flips back to a leaf, as ``tree.py`` remove-child does.
    """
    from _fileops import locked_modify_yaml  # noqa: PLC0415 - keeps the dry run light

    done = {"dropped": False}  # the verdict lives here: the modifier's return is the data (rb-2162)

    def _do(data):
        done["dropped"] = False  # the sync layer may re-run the modifier after a conflict
        nodes = data.get("nodes") if isinstance(data, dict) else None
        if not isinstance(nodes, dict) or nodes.get(key) != entry:
            return data
        holder = nodes.get(parent) if parent else None
        kids = holder.get("children") if isinstance(holder, dict) else None
        if isinstance(kids, list) and key in kids:
            kids.remove(key)
            if "child_count" in holder:
                holder["child_count"] = len(kids)
            if not kids and holder.get("node_type") == "interior":
                holder["node_type"] = "leaf"
        del nodes[key]
        data["last_updated"] = today
        done["dropped"] = True
        return data

    locked_modify_yaml(str(tree_dir / "_tree.yaml"), _do, skip_if_unchanged=True)
    return done["dropped"]


def _restore_index_entry(tree_dir: Path, key: str, restore: dict, today: str) -> str:
    """Put a retained entry back in the index, in the slot it left. Returns ``""`` on success,
    else why nothing was written: ``index_unreadable``, ``key_taken`` or ``parent_missing``."""
    from _fileops import locked_modify_yaml  # noqa: PLC0415 - keeps the dry run light

    verdict = {"why": "index_unreadable"}

    def _do(data):
        verdict["why"] = "index_unreadable"  # the sync layer may re-run the modifier
        nodes = data.get("nodes") if isinstance(data, dict) else None
        if not isinstance(nodes, dict):
            return data
        if key in nodes:
            verdict["why"] = "key_taken"
            return data
        parent = restore.get("parent")
        holder = nodes.get(parent) if parent else None
        if parent and not isinstance(holder, dict):
            verdict["why"] = "parent_missing"
            return data
        entry = copy.deepcopy(restore.get("index_entry"))
        if isinstance(entry, dict):
            entry["last_updated"] = today
        nodes[key] = entry
        position = restore.get("parent_position")
        if isinstance(position, int) and isinstance(holder, dict):
            kids = holder.get("children")
            if not isinstance(kids, list):
                kids = holder["children"] = []
            kids.insert(min(position, len(kids)), key)
            if "child_count" in holder:
                holder["child_count"] = len(kids)
            if holder.get("node_type") == "leaf":
                holder["node_type"] = "interior"
        data["last_updated"] = today
        verdict["why"] = ""
        return data

    locked_modify_yaml(str(tree_dir / "_tree.yaml"), _do, skip_if_unchanged=True)
    return verdict["why"]


def _replace_file(path: Path, data: bytes) -> bool:
    """Replace ``path`` with ``data`` through a temp file, as ``_write_node`` does, and report
    whether it read back as written."""
    tmp = path.with_name(path.name + ".tmp-member-edit")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return path.read_bytes() == data
    except OSError:
        return False


def _restore_path(export, tree_dir: Path | None, restore: dict) -> Path | None:
    """Where a retained page goes back to, kept inside the tree, or ``None``.

    The record names the file and its digest does not cover that name, so the path is held
    to the rule ``_node_file`` applies to a live node: a record that points out of the tree
    is unusable, and nothing is read from or written to what it points at.
    """
    if tree_dir is None:
        return None
    path = export.node_body_path(tree_dir, _entry_file(restore.get("index_entry")))
    if path is None:
        return None
    try:
        path.resolve().relative_to(tree_dir.resolve())
    except (ValueError, OSError):
        return None
    return path


def forget_refusal(export, world: Path, kind: str, item_id: str, record: dict,
                   retention_dir: str) -> str | None:
    """A refusal only a forget has, or ``None``. Runs before any write."""
    if kind not in _FORGETTERS:
        return "forget_pending_ruling"
    if not retention_dir:
        return "no_retention_store"
    if kind == "hypothesis":
        return _statement_refusal(item_id, _hypothesis_statement(record))
    if kind == "guardrail":
        return _guardrail_forget_refusal(record)
    if record.get("children"):
        return "forget_not_a_leaf"
    located = _node_file(export, world, record)
    if located is not None and _index_snapshot(located[0], item_id) is None:
        return "index_unsupported"
    return None


def undo_refusal(export, world: Path, record: dict) -> str | None:
    """A refusal only an undo has, or ``None``. ``record`` is the retained record. Runs before
    any write."""
    if not knowledge_retention.in_window(record, knowledge_retention.now_utc()):
        return "undo_expired"
    if record.get("kind") == "hypothesis":
        return _hypothesis_undo_refusal(export, world, record)
    if record.get("kind") == "guardrail":
        return _guardrail_undo_refusal(export, world, record)
    tree_dir = export._resolve_tree_dir(world)
    nodes = _mapping_nodes(tree_dir) if tree_dir else None
    if nodes is None:
        return "index_unsupported"
    restore = record.get("restore") or {}
    path = _restore_path(export, tree_dir, restore)
    if path is None:
        return "not_addressable"  # a record that names no file inside the tree is as good as none
    if record["item_id"] in nodes:
        return "undo_conflict"
    parent = restore.get("parent")
    if parent and not isinstance(nodes.get(parent), dict):
        return "undo_parent_missing"
    # The file must still be the one a forget left (the marker, or the page itself if the
    # blanking never ran), or absent. Anything else is a page written since, and a restore
    # would overwrite what the member never saw.
    if path.exists():
        try:
            current = path.read_bytes()
        except OSError:
            return "undo_conflict"
        if not (current.startswith(_TOMBSTONE_HEAD.encode("utf-8"))
                or current == knowledge_retention.retained_content(record)):
            return "undo_conflict"
    return None


def _retained_item(export, handle: str, retention_dir: str):
    """``(kind, item_id, retained record)`` for the live record ``handle`` names, or ``None``.

    A node is matched by :func:`resolve_item_handle` over the retained nodes presented as
    nodes, so it is matched by the same keyed digest, and refused as ambiguous by the same
    rule, as a handle resolved against the exposed set. A hypothesis or a guardrail is matched
    by the same keyed digest directly: its category allowlist is derived from the tree, which a
    retained one has no part of, and a record exists only because the item was exposed when it
    was forgotten. One match across all kinds, or ``None``.
    """
    if not retention_dir:
        return None
    live = {(r["kind"], r["item_id"]): r for _path, r in knowledge_retention.list_records(retention_dir)
            if r.get("kind") in _FORGETTERS and knowledge_retention.is_live(r)}
    secret = os.environ.get(export._GOAL_HANDLE_SECRET_VAR, "")
    environment_id = os.environ.get("ENVIRONMENT_ID", "")
    present = [{"key": item_id, "category": _entry_file((r.get("restore") or {}).get("index_entry"))}
               for (kind, item_id), r in live.items() if kind == "node"]
    hit = resolve_item_handle(handle, tree_nodes=present, hypotheses=(), guardrails=(),
                              secret=secret, environment_id=environment_id)
    found = [("node", hit[1])] if hit else []
    want = str(handle or "").strip().lower()
    if want and secret:
        for kind, item_id in live:
            computed = item_handle(kind, item_id, secret, environment_id) if kind != "node" else ""
            if computed and hmac.compare_digest(computed, want):
                found.append((kind, item_id))
    if len(found) != 1:
        return None
    return (*found[0], live[found[0]])


def _forget_node(export, world: Path, item_id: str, record: dict, retention_dir: str) -> dict | None:
    """Soft forget: retain the page, drop its index entry, blank its file, in that order.

    The order is the safety. The page is retained and read back FIRST, so nothing is lost
    whatever follows (guard-6223). Dropping the entry is next because it is what hides the
    node from the member and the resident, so a stop after it leaves the node hidden and the
    text only in the retained record and the file. Blanking is last and is cleanup: when it
    fails the forget stands and the report says ``blanked: False``. A stop before the drop
    leaves a retained record for a node that is still there, which is inert: an undo of it is
    refused as ``undo_conflict``.
    """
    located = _node_file(export, world, record)
    if located is None:
        sys.stderr.write(f"knowledge-edit-apply: no file inside the tree for node {item_id}\n")
        return None
    tree_dir, path = located
    snapshot = _index_snapshot(tree_dir, item_id)
    if snapshot is None:
        sys.stderr.write(f"knowledge-edit-apply: the index has no entry for node {item_id}\n")
        return None
    entry, parent, position = snapshot
    try:
        page = path.read_bytes()
        if page.startswith(_TOMBSTONE_HEAD.encode("utf-8")):
            # A ghost: an entry that came back over a page already blanked, as a merge from a
            # stale copy of the index can bring one. It holds no text to retain, and a record
            # written from it would replace the retained text of the forget that blanked it.
            # Hide it again and leave that record alone.
            earlier = knowledge_retention.read_record(
                knowledge_retention.record_path(retention_dir, "node", item_id))
            live = earlier is not None and knowledge_retention.is_live(earlier)
            undo_until = earlier.get("undo_until") if live else None
        else:
            retained = knowledge_retention.build_record(
                "node", item_id, page, now=knowledge_retention.now_utc(),
                restore={"index_entry": entry, "parent": parent, "parent_position": position})
            knowledge_retention.write_record(retention_dir, retained)
            undo_until = retained["undo_until"]
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"knowledge-edit-apply: node {item_id} not forgotten, its text was not retained: {exc}\n")
        return None
    today = _today()
    try:
        dropped = _drop_index_entry(tree_dir, item_id, entry, parent, today)
    except Exception as exc:  # noqa: BLE001 - the lock and the sync layer raise their own errors
        sys.stderr.write(f"knowledge-edit-apply: node {item_id} not forgotten, the index was not written: {exc}\n")
        return None
    if not dropped:
        sys.stderr.write(f"knowledge-edit-apply: node {item_id} not forgotten, its index entry changed while it was retained\n")
        return None
    blanked = _replace_file(path, _TOMBSTONE.format(today=today).encode("utf-8"))
    if not blanked:
        # Still forgotten: the node is hidden and its text is retained, which is what the member
        # asked for. Reporting a failure would tell them nothing happened, and a retry would find
        # no node. The page is left for the erase to blank, and the report says so.
        sys.stderr.write(f"knowledge-edit-apply: node {item_id} is forgotten and retained, but its file was not blanked: {path}\n")
    return {"forgotten": True, "undo_until": undo_until, "blanked": blanked}


def _undo_node(export, world: Path, item_id: str, record: dict, retention_dir: str) -> dict | None:
    """Undo a soft forget: restore the retained page to its file, then its entry to the index."""
    tree_dir = export._resolve_tree_dir(world)
    restore = record.get("restore") or {}
    content = knowledge_retention.retained_content(record)
    path = _restore_path(export, tree_dir, restore)
    if path is None or content is None:
        sys.stderr.write(f"knowledge-edit-apply: nothing to restore for node {item_id}\n")
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.stderr.write(f"knowledge-edit-apply: node {item_id} not restored, its folder cannot be made: {exc}\n")
        return None
    today = _today()
    # The file first: an index entry with no file behind it is a phantom node (guard-4836).
    if not _replace_file(path, content):
        sys.stderr.write(f"knowledge-edit-apply: node {item_id} did not read back as restored\n")
        return None
    try:
        why = _restore_index_entry(tree_dir, item_id, restore, today)
    except Exception as exc:  # noqa: BLE001 - the lock and the sync layer raise their own errors
        why = f"{type(exc).__name__}: {exc}"
    if why:
        # Nothing points at the file, so it must not hold the text: put the marker back.
        _replace_file(path, _TOMBSTONE.format(today=today).encode("utf-8"))
        sys.stderr.write(f"knowledge-edit-apply: node {item_id} not restored: {why}\n")
        return None
    try:
        knowledge_retention.mark_undone(
            knowledge_retention.record_path(retention_dir, "node", item_id), record,
            knowledge_retention.now_utc())
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"knowledge-edit-apply: node {item_id} is restored, but its retained copy was not cleared: {exc}\n")
    return {"restored": True}


# --- forget and undo (hypothesis) ---------------------------------------------

#: What a forgotten hypothesis's statement fields are left holding, and the reason a forgotten
#: guardrail is retired with. Nothing of the original survives in a statement. It is over 20
#: characters because the whole-record writers (add and update) refuse a shorter claim on a record
#: past the discovered stage, so a forgotten record can still be rewritten whole; update-field,
#: which writes it here, checks no claim length. Its head is how an undo recognises what a forget
#: left, and tells a guardrail a member's forget retired from one retired for another cause.
_FORGOTTEN_TEXT = "Forgotten by the member on {today}."
_FORGOTTEN_HEAD = _FORGOTTEN_TEXT.split("{", 1)[0]


def _stored_hypothesis(export, world: Path, item_id: str) -> dict | None:
    """The record ``item_id`` has in the pipeline store NOW, read fresh, or ``None``."""
    for record in export._read_jsonl(world / "pipeline.jsonl"):
        if isinstance(record, dict) and str(record.get("id") or "").strip() == item_id:
            return record
    return None


def _hypothesis_statement(record: dict) -> dict:
    """The statement a member saw and an edit replaces: each text field the record holds.

    Truthy, the test :func:`item_text` applies, so a field the member was shown nothing from
    is not retained, blanked or restored.
    """
    return {name: record[name] for name in item_text_fields("hypothesis") if record.get(name)}


def _statement_refusal(item_id: str, statement: dict) -> str | None:
    """Why this statement could not be put back through the pipeline's update-field, or ``None``.

    A forget that cannot be undone is not the soft forget that was ruled, so a forget refuses
    it up front, and an undo checks again what it reads back from retention.
    """
    for name, value in statement.items():
        if not isinstance(value, str) or store_would_coerce(value):
            return "store_would_coerce"
        if len(_field_query(item_id, name, value)) > QUERY_BUDGET:
            return "too_long_for_store"
    return None


def _retained_statement(record: dict) -> dict | None:
    """The statement a live retained hypothesis record holds, or ``None`` when it is unusable.

    The record names the fields to restore and its digest does not cover that list, so it is
    held to the table a forget writes from: only a hypothesis's own text fields, each a
    non-empty string. A record naming anything else (a stage, a category, the marker) would
    turn an undo into an arbitrary write, so it is as good as none.
    """
    content = knowledge_retention.retained_content(record)
    if content is None:
        return None
    try:
        statement = json.loads(content)
    except ValueError:
        return None
    if not isinstance(statement, dict) or not set(statement) <= set(item_text_fields("hypothesis")):
        return None
    if not all(isinstance(value, str) and value for value in statement.values()):
        return None
    return statement


def _hypothesis_undo_refusal(export, world: Path, record: dict) -> str | None:
    """A refusal only an undo of a hypothesis has, or ``None``. Runs before any write."""
    statement = _retained_statement(record)
    live = _stored_hypothesis(export, world, record["item_id"])
    if statement is None or live is None:
        return "not_addressable"  # a record that restores nothing usable is as good as none
    if not is_forgotten(live):
        # A retained record for a hypothesis that is still shown: a forget that stopped before
        # it hid the record, which is inert, or a marker something cleared.
        return "undo_conflict"
    for name, value in statement.items():
        now = live.get(name)
        # The text a forget left, or the retained text itself (a blank that never ran, or an
        # undo that stopped part way). Anything else was written since, and a restore would
        # overwrite what the member never saw.
        if now != value and not (isinstance(now, str) and now.startswith(_FORGOTTEN_HEAD)):
            return "undo_conflict"
    return _statement_refusal(record["item_id"], statement)


def _write_hypothesis_field(item_id: str, field: str, value: str | list | dict | None) -> bool:
    """One pipeline update-field write, true only when the record the endpoint returns holds
    ``value``. ``None`` is written as ``null``, which the endpoint stores as nothing, and a
    list or a mapping as its JSON, which the endpoint parses back to the same container (the
    erase sweep empties a nested value to its own type with it).

    A 2xx is not evidence the value landed, the rule :func:`_write_hypothesis` applies too.
    """
    import _rt  # noqa: PLC0415 - lazy, like the other daemon writers (guard-555)

    wire = "null" if value is None else value if isinstance(value, str) else json.dumps(value)
    try:
        resp = json.loads(_rt.rt_call("POST", "/v1/pipeline/update-field",
                                      query=_field_query(item_id, field, wire)))
    except Exception as exc:  # noqa: BLE001 - RtError, transport and decode alike
        sys.stderr.write(f"knowledge-edit-apply: write failed for hypothesis {item_id} {field}: {exc}\n")
        return False
    stored = resp.get("record") or resp if isinstance(resp, dict) else {}
    if not isinstance(stored, dict) or stored.get(field) != value:
        sys.stderr.write(f"knowledge-edit-apply: hypothesis {item_id} {field} did not read back as written\n")
        return False
    return True


def _forget_hypothesis(export, world: Path, item_id: str, record: dict, retention_dir: str) -> dict | None:
    """Soft forget: retain the statement, stamp the marker, blank the statement, in that order.

    The statement is retained and read back FIRST, so nothing is lost whatever follows
    (guard-6223). The marker is next because it is what hides the record from the member and
    the handle resolver, so a stop after it leaves the hypothesis hidden and its text in the
    retained record and the record. Blanking is last and is cleanup: when it fails the forget
    stands and the report says ``blanked: False``. A stop before the marker leaves a retained
    record for a hypothesis that is still shown, which is inert: an undo of it is refused as
    ``undo_conflict``.

    The record is read again between retaining and writing, and left alone when its
    statement is no longer the one that was retained: the decision was made on a snapshot,
    and a retained copy of text that has since changed is not a copy of what the member
    forgot (guard-3881). The write itself holds the pipeline's own lock, but no lock spans the
    gap between these two reads and the endpoint's, so that gap is what is left.

    The marker's stamp comes AFTER the undo stamp the record may carry (a shown record has no
    marker), even when the clock has not moved on: the store merge ranks a forget against an undo
    by their stamps, and a tie or an inversion would let a stale copy win.
    """
    statement = _hypothesis_statement(record)
    moment = knowledge_retention.strictly_after(knowledge_retention.now_utc(), record.get(RESTORED_FIELD))
    earlier = knowledge_retention.read_record(
        knowledge_retention.record_path(retention_dir, "hypothesis", item_id))
    earlier_live = earlier is not None and knowledge_retention.is_live(earlier)
    if earlier_live and any(isinstance(value, str) and value.startswith(_FORGOTTEN_HEAD)
                            for value in statement.values()):
        # A ghost: a record without the marker that holds text a forget already blanked, as a
        # merge from a stale copy can leave. It holds nothing to retain, and a record written
        # from it would replace the retained text of the forget that blanked it. Hide it again
        # and leave that record alone. It takes a LIVE retained record to be a ghost: without
        # one the same words are the member's own statement, and are retained like any other.
        undo_until = earlier.get("undo_until")
        stamp = moment.isoformat()
    else:
        try:
            retained = knowledge_retention.build_record(
                "hypothesis", item_id, json.dumps(statement, sort_keys=True).encode("utf-8"),
                now=moment, restore={})
            knowledge_retention.write_record(retention_dir, retained)
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"knowledge-edit-apply: hypothesis {item_id} not forgotten, its text was not retained: {exc}\n")
            return None
        undo_until = retained["undo_until"]
        stamp = retained["forgotten_at"]
        fresh = _stored_hypothesis(export, world, item_id)
        if fresh is None or _hypothesis_statement(fresh) != statement:
            sys.stderr.write(f"knowledge-edit-apply: hypothesis {item_id} not forgotten, its text changed while it was retained\n")
            return None
    if not _write_hypothesis_field(item_id, FORGOTTEN_FIELD, stamp):
        return None
    tombstone = _FORGOTTEN_TEXT.format(today=_today())
    # Every field is tried: a blank that fails must not leave the others holding their text.
    blanked = all([_write_hypothesis_field(item_id, name, tombstone) for name in statement])
    if not blanked:
        sys.stderr.write(f"knowledge-edit-apply: hypothesis {item_id} is forgotten and retained, but its text was not all blanked\n")
    return {"forgotten": True, "undo_until": undo_until, "blanked": blanked}


def _undo_hypothesis(export, world: Path, item_id: str, record: dict, retention_dir: str) -> dict | None:
    """Undo a soft forget: stamp ``restored_at``, restore the retained statement, then clear the
    marker.

    The stamp goes FIRST and comes after every stamp the record carries, so from its first write
    the undo outranks any stale copy of the forget in the store merge, and a copy caught part way
    can never be put back to the forgotten state. The text goes back next, while the marker still
    hides the record, and the marker is cleared LAST, so the member never sees a hypothesis with
    a blanked statement. A stop part way leaves it hidden with some text restored, and the
    retained record still holds it all, so the same undo can run again.
    """
    statement = _retained_statement(record)
    if statement is None:
        sys.stderr.write(f"knowledge-edit-apply: nothing to restore for hypothesis {item_id}\n")
        return None
    live = _stored_hypothesis(export, world, item_id)
    if live is None:
        # The stamp has to come after the stamps the record carries, so it is not written blind.
        sys.stderr.write(f"knowledge-edit-apply: hypothesis {item_id} not restored, its record could not be read\n")
        return None
    stamp = knowledge_retention.strictly_after(
        knowledge_retention.now_utc(), live.get(FORGOTTEN_FIELD), live.get(RESTORED_FIELD)).isoformat()
    if not _write_hypothesis_field(item_id, RESTORED_FIELD, stamp):
        return None
    for name, value in statement.items():
        if not _write_hypothesis_field(item_id, name, value):
            return None
    if not _write_hypothesis_field(item_id, FORGOTTEN_FIELD, None):
        return None
    try:
        knowledge_retention.mark_undone(
            knowledge_retention.record_path(retention_dir, "hypothesis", item_id), record,
            knowledge_retention.now_utc())
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"knowledge-edit-apply: hypothesis {item_id} is restored, but its retained copy was not cleared: {exc}\n")
    return {"restored": True}


# --- forget and undo (guardrail) ----------------------------------------------

#: The tag prefix that names the guardrail a member's undo put back.
_RESTORES_TAG = "restores:"

#: What the store requires to add a guardrail and a forget cannot supply on its own. An undo adds
#: the rule back as a new record, so a guardrail that lacks one of these could not be put back.
_ADD_NEEDS = frozenset({"category", "trigger_condition"})

#: The types a carried field has in the live store (7,097 active guardrails, read 2026-10-03:
#: category and trigger_condition always a string, trigger_pattern null on 7,063 and a string on
#: 34, context_triggers and phases lists of strings, when_to_use a mapping on 6,959 and a string
#: on 138). The store checks which keys an add carries and never what they hold, so a retained
#: record is held to this table here: it stands between a file on disk and what an undo writes.
_CARRIED_TYPES = {"category": (str,), "trigger_condition": (str,), "trigger_pattern": (str, type(None)),
                  "context_triggers": (list,), "phases": (list,), "when_to_use": (str, dict)}


def _carried_typed(carried: dict) -> bool:
    """True when every carried field has a type the live store holds it as. A key with no row
    in the table fails, which is what keeps a retained record to the fields a forget writes from
    and stops a new carried field going untyped by accident."""
    return all(isinstance(value, _CARRIED_TYPES.get(key, ())) for key, value in carried.items())


def _stored_guardrail(export, world: Path, item_id: str) -> dict | None:
    """The record ``item_id`` has in the guardrails store NOW, read fresh, or ``None``."""
    for record in export._read_jsonl(world / "guardrails.jsonl"):
        if isinstance(record, dict) and str(record.get("id") or "").strip() == item_id:
            return record
    return None


def _guardrail_forget_refusal(record: dict) -> str | None:
    """Why this guardrail could not be put back by an undo, or ``None``.

    A forget that cannot be undone is not the soft forget that was ruled, so a guardrail that
    lacks a field the store requires to add one is refused up front, by key presence, the test
    the store applies. So is one that holds a carried field in a type the live store never has,
    because the undo holds a retained record to the same table.
    """
    if not (isinstance(record.get("rule"), str) and record["rule"]):
        return "not_addressable"
    if not _ADD_NEEDS <= set(record):
        return "not_restorable"
    if not _carried_typed({k: record[k] for k in _SUCCESSOR_CARRIES if k in record}):
        return "not_restorable"
    return None


def _retained_guardrail(record: dict) -> dict | None:
    """``{rule, carried, tags}`` from a live retained guardrail record, or ``None`` when unusable.

    The digest covers the rule and nothing else, so the fields to put back are held to the table
    a forget writes from: only the keys of :data:`_SUCCESSOR_CARRIES`, each in a type the live
    store holds it as (:data:`_CARRIED_TYPES`), and tags that are all strings. A record naming
    anything else would turn an undo into an arbitrary write into the guardrails store, so it is
    as good as none.
    """
    content = knowledge_retention.retained_content(record)
    if content is None:
        return None
    try:
        rule = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    restore = record.get("restore") or {}
    carried, tags = restore.get("carried"), restore.get("tags")
    if (not rule or not isinstance(carried, dict) or not _carried_typed(carried)
            or not isinstance(tags, list) or not all(isinstance(t, str) for t in tags)):
        return None
    return {"rule": rule, "carried": carried, "tags": tags}


def _guardrail_undo_refusal(export, world: Path, record: dict) -> str | None:
    """A refusal only an undo of a guardrail has, or ``None``. Runs before any write."""
    held = _retained_guardrail(record)
    live = _stored_guardrail(export, world, record["item_id"])
    if held is None or live is None:
        return "not_addressable"  # a record that restores nothing usable is as good as none
    reason = live.get("retirement_reason")
    if (live.get("status") != "retired" or live.get("rule") != held["rule"]
            or not (isinstance(reason, str) and reason.startswith(_FORGOTTEN_HEAD))):
        # Not the retirement a forget made: the guardrail is still shown (a forget that stopped
        # before it retired it, which is inert) or something retired it for its own cause since.
        return "undo_conflict"
    if not _ADD_NEEDS <= set(held["carried"]):
        return "not_restorable"
    return None


def _forget_guardrail(export, world: Path, item_id: str, record: dict, retention_dir: str) -> dict | None:
    """Soft forget: retain the rule, then retire the guardrail with a member-forget reason.

    The rule is retained and read back FIRST, so nothing is lost whatever follows (guard-6223).
    Retiring is what hides the guardrail from the member and the resident, because the export
    and retrieval both publish active guardrails only, and ``status`` is written last, so a stop
    part way leaves it published, never retired without a way back. A stop before the status
    leaves a retained record for a guardrail that is still shown, which is inert: an undo of it
    is refused as ``undo_conflict``.

    The record is read again between retaining and retiring, and left alone when it is no longer
    the active guardrail with the rule that was retained (guard-3881).

    The rule is NOT blanked, and the report says ``blanked: False`` for that reason. It is
    immutable by design (guard-6210): the store's merge keys a guardrail on ``created`` and the
    whole rule, so a rule edited in place forks the record at the next cross-box merge instead
    of changing it. The retired record keeps the text until an erase that can remove it. What
    the merge does protect is the retirement: ``status`` retired dominates, so a stale copy that
    is still active cannot bring the rule back.
    """
    rule = record["rule"]
    try:
        retained = knowledge_retention.build_record(
            "guardrail", item_id, rule.encode("utf-8"), now=knowledge_retention.now_utc(),
            restore={"carried": {k: record[k] for k in _SUCCESSOR_CARRIES if k in record},
                     "tags": [t for t in _tags(record) if isinstance(t, str)]})
        knowledge_retention.write_record(retention_dir, retained)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"knowledge-edit-apply: guardrail {item_id} not forgotten, its rule was not retained: {exc}\n")
        return None
    fresh = _stored_guardrail(export, world, item_id)
    if fresh is None or not is_active_guardrail(fresh) or fresh.get("rule") != rule:
        sys.stderr.write(f"knowledge-edit-apply: guardrail {item_id} not forgotten, it changed while its rule was retained\n")
        return None
    try:
        retired = _retire_guardrail(item_id, _FORGOTTEN_TEXT.format(today=_today()))
    except Exception as exc:  # noqa: BLE001 - RtError, transport and decode alike
        sys.stderr.write(f"knowledge-edit-apply: write failed for guardrail {item_id}: {exc}\n")
        return None
    if retired.get("status") != "retired":
        sys.stderr.write(f"knowledge-edit-apply: guardrail {item_id} did not read back as retired\n")
        return None
    return {"forgotten": True, "undo_until": retained["undo_until"], "blanked": False}


def _undo_guardrail(export, world: Path, item_id: str, record: dict, retention_dir: str) -> dict | None:
    """Undo a soft forget: add the retained rule back as a new guardrail, then mark the record undone.

    The retirement cannot be reversed in place. The store's merge keeps a retirement over any
    copy that is still active, which is what makes a forget stick across boxes, so a record
    reactivated here would be retired again by the next merge from a box that never saw the undo.
    The rule returns the way a member's correction lands: a successor carrying the retained rule
    and the fields that say when it applies, tagged ``restores:<id>``, under a NEW id and so a new
    handle. The retired record is left as it is, and the new one starts without the use counters,
    title, action hint and severity it had.

    Safe to re-run. An active guardrail already tagged for this one and carrying this rule is
    reused, not added again, so a stop before the record is marked undone leaves the same undo
    runnable.
    """
    held = _retained_guardrail(record)
    if held is None:
        sys.stderr.write(f"knowledge-edit-apply: nothing to restore for guardrail {item_id}\n")
        return None
    tag = f"{_RESTORES_TAG}{item_id}".lower()
    restored = next((g for g in reversed(export._read_jsonl(world / "guardrails.jsonl"))
                     if is_active_guardrail(g) and g.get("rule") == held["rule"] and tag in _tags(g)),
                    None)
    try:
        if restored is None:
            # A carried supersedes or restores tag would name a guardrail this one never replaced
            # or restored.
            tags = [t for t in held["tags"]
                    if not t.lower().startswith((_SUPERSEDES_TAG, _RESTORES_TAG))]
            restored = _guard_store("/v1/store/append", "store=guardrails", {
                **held["carried"], "rule": held["rule"], "source": TRIGGER_SOURCE,
                "tags": [*tags, TRIGGER_SOURCE, tag], "allow_near_dup": True})
            if (restored.get("rule") != held["rule"] or not is_active_guardrail(restored)
                    or not restored.get("id")):
                sys.stderr.write(f"knowledge-edit-apply: guardrail {item_id} restored record did not read back as written\n")
                return None
    except Exception as exc:  # noqa: BLE001 - RtError, transport and decode alike
        sys.stderr.write(f"knowledge-edit-apply: write failed for guardrail {item_id}: {exc}\n")
        return None
    try:
        knowledge_retention.mark_undone(
            knowledge_retention.record_path(retention_dir, "guardrail", item_id), record,
            knowledge_retention.now_utc())
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"knowledge-edit-apply: guardrail {item_id} is restored, but its retained copy was not cleared: {exc}\n")
    new_id = str(restored.get("id") or "")
    handle = item_handle("guardrail", new_id, os.environ.get(export._GOAL_HANDLE_SECRET_VAR, ""),
                         os.environ.get("ENVIRONMENT_ID", ""))
    return {"restored": True, "restored_as": new_id, "handle": handle}


# --- dispatch -----------------------------------------------------------------

_WRITERS ={"node": _write_node, "hypothesis": _write_hypothesis, "guardrail": _write_guardrail}

#: The kinds a forget and an undo can act on. A kind that gains an edit before it gains a
#: forgetter is refused ``forget_pending_ruling``, never forgotten some other way.
_FORGETTERS = {"node": _forget_node, "hypothesis": _forget_hypothesis, "guardrail": _forget_guardrail}
_UNDOERS = {"node": _undo_node, "hypothesis": _undo_hypothesis, "guardrail": _undo_guardrail}


def store_refusal(export, world: Path, kind: str, item_id: str, record: dict, text: str,
                  base: str) -> str | None:
    """A refusal only the item's own store can explain, or ``None``. Runs before any write."""
    if kind not in _WRITERS:
        return "edit_unsupported"
    redactor = _export_redactor(export, world)
    if kind == "node":
        located = _node_file(export, world, record)
        if located is not None:
            try:
                body = export._strip_front_matter(located[1].read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                body = ""  # the writer reports an unreadable file as a failed write
            if len(body) > export._NODE_BODY_CAP:
                return "view_truncated"
            if not is_unredacted(body, redactor):
                return "view_redacted"
    # The text the export publishes for the item, which is what the member saw.
    shown = item_text(kind, record)
    if kind in ("hypothesis", "guardrail") and not is_unredacted(shown, redactor):
        return "view_redacted"
    want = (base or "").strip().lower()
    if not _BASE_RE.fullmatch(want):
        return "view_base_missing"
    if view_digest(redactor(shown)) != want:
        return "view_stale"
    if kind == "hypothesis":
        if store_would_coerce(text):
            return "store_would_coerce"
        if len(_claim_query(item_id, text)) > QUERY_BUDGET:
            return "too_long_for_store"
    return None


def main(argv: list[str] | None = None, report: dict | None = None) -> int:
    ap = argparse.ArgumentParser(description="Apply one member edit to one learned item.")
    ap.add_argument("--handle", required=True, help="opaque published item handle")
    ap.add_argument("--op", required=True, help="edit | forget | undo")
    ap.add_argument("--text", default="", help="the member's replacement text (edit only)")
    ap.add_argument("--base", default="",
                    help="SHA-256 hex digest of the view the member corrected (edit only)")
    ap.add_argument("--retention-dir", default="",
                    help="where a forgotten item's text is retained for undo (forget and undo "
                         "only); supplied by the caller, never derived")
    ap.add_argument("--apply", action="store_true", help="perform the write")
    ap.add_argument("--json", action="store_true", help="machine-readable plan")
    args = ap.parse_args(argv)

    export = _load_export_mod()
    world = export._resolve_world()
    # An undo names an item the exposure predicate no longer shows, so it resolves from
    # retention; every other op resolves from what the projection exposes.
    if str(args.op or "").strip().lower() == "undo":
        hit = _retained_item(export, args.handle, args.retention_dir)
    else:
        hit = export.resolve_item(world, args.handle)
    # An unresolved handle has no kind. Any valid kind gets the core's own not_addressable
    # refusal, which by design never tells one miss from another.
    kind, item_id, record = hit if hit else (KNOWLEDGE_ITEM_KINDS[0], "", None)
    plan = plan_knowledge_edit(kind, record, args.op, args.text)
    refusal = plan.refusal
    if refusal is None and plan.remove:
        refusal = forget_refusal(export, world, kind, item_id, record, args.retention_dir)
    if refusal is None and plan.restore:
        refusal = undo_refusal(export, world, record)
    field = EDIT_FIELDS.get(kind, "")
    text = str(plan.writes.get(field, ""))
    if refusal is None and not (plan.remove or plan.restore):
        refusal = store_refusal(export, world, kind, item_id, record, text, args.base)
    if refusal:
        sys.stderr.write(f"knowledge-edit-apply: refused ({refusal})\n")
        if report is not None:
            report["refused"] = refusal
        return 3

    if plan.remove or plan.restore:
        out = {"kind": kind, "id": item_id, "op": "forget" if plan.remove else "undo",
               "applied": False}
    else:
        out = {"kind": kind, "id": item_id, "op": "edit", "field": field, "chars": len(text),
               "applied": False}
    if args.apply:
        if plan.remove:
            landed = _FORGETTERS[kind](export, world, item_id, record, args.retention_dir)
        elif plan.restore:
            landed = _UNDOERS[kind](export, world, item_id, record, args.retention_dir)
        else:
            landed = _WRITERS[kind](export, world, item_id, record, text)
        if landed is None:
            return 1
        out.update(landed)
        out["applied"] = True
    if report is not None:
        report.update(out)

    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    elif plan.remove or plan.restore:
        print(f"{kind} {item_id}: {out['op']}" + ("" if args.apply else "   (dry run — pass --apply)"))
    else:
        print(f"{kind} {item_id}: edit -> {field} ({len(text)} chars)"
              + (f", superseded by {out['superseded_by']}" if "superseded_by" in out else "")
              + ("" if args.apply else "   (dry run — pass --apply)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
