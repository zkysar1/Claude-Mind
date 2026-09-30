"""Member edits of one learned item, as a pure decision core ().

The member-facing knowledge view (``knowledge_projection.project``) publishes each
exposed wiki node, hypothesis and guardrail with an opaque ``handle``
(:func:`knowledge_projection.item_handle`). This module is the WRITE half's decision:
it turns ``(kind, record, op, text)`` into the exact change that should land, or into a
refusal. It performs no I/O: resolution lives in
``knowledge_projection.resolve_item_handle`` and the change lands through each store's
own writer, so this file stays unit-testable and side-effect free. Sibling of
``planned_verbs.py``, which does the same job for goals.

TWO OPERATIONS
==============
``edit`` replaces the one member-visible text field of the item: a wiki node's body, a
hypothesis's claim, a guardrail's rule. ``forget`` removes the item from what the
resident uses and from every member surface; HOW each store removes it is the applier's
job, and this core only names the removal.

WHY NO EDIT MAY WRITE A FIELD THE EXPOSURE PREDICATE READS
===========================================================
``knowledge_projection._exposed_knowledge`` is the single predicate deciding what a
member can SEE and what a member can ADDRESS (rb-10157). It reads a node's ``category``
or ``file`` and ``key``, and a hypothesis's or guardrail's ``category``. An edit that
wrote one of those could move the item out of the exposed set, leaving the member a
handle that no longer resolves: the change would be unreachable and unrevertable by the
person who made it. So an edit writes only the text field, and :func:`plan_knowledge_edit`
checks its own output against :data:`EXPOSURE_READ_FIELDS` at runtime, because the table
is what a future editor changes. Leaving the exposed set is ``forget``'s whole purpose,
and it is the only operation allowed to do it.

MEMBER TEXT IS UNTRUSTED INPUT
==============================
Edited text lands in a store the resident reads as knowledge. It is DATA, never an
instruction: it is length-capped and stripped of control characters here, and no consumer
may interpret it as a directive.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "KNOWLEDGE_OPS",
    "EDIT_FIELDS",
    "EXPOSURE_READ_FIELDS",
    "EDIT_TEXT_CAP",
    "KnowledgeEditPlan",
    "plan_knowledge_edit",
]

#: The two member operations on one learned item.
KNOWLEDGE_OPS = ("edit", "forget")

#: The one member-visible text field an edit replaces, per item kind. These are the
#: fields ``knowledge_projection.project`` publishes as the item's text (a node's
#: ``body``, a hypothesis's ``claim`` shown as ``statement``, a guardrail's ``rule``).
EDIT_FIELDS: dict[str, str] = {"node": "body", "hypothesis": "claim", "guardrail": "rule"}

#: The fields ``knowledge_projection._exposed_knowledge`` reads. No edit may write any of
#: them; see the module docstring.
EXPOSURE_READ_FIELDS = frozenset({"category", "file", "key"})

#: Longest accepted member text. A wiki page is the longest item, so the cap is sized for
#: a page rather than for a one-line rule; the transport enforces its own bound upstream.
EDIT_TEXT_CAP = 20000


class KnowledgeEditPlan:
    """The outcome of planning one operation: a change, or a ``refusal``.

    ``writes`` maps a field to its new value for an edit; ``remove`` is True for a
    forget. ``refusal`` is a short machine-stable reason, and exactly one of the three is
    meaningful.
    """

    __slots__ = ("writes", "remove", "refusal")

    def __init__(
        self,
        writes: dict[str, Any] | None = None,
        remove: bool = False,
        refusal: str | None = None,
    ) -> None:
        self.writes: dict[str, Any] = writes or {}
        self.remove = remove
        self.refusal = refusal

    @property
    def ok(self) -> bool:
        return self.refusal is None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (f"KnowledgeEditPlan(writes={self.writes!r}, remove={self.remove!r}, "
                f"refusal={self.refusal!r})")


def _sanitize_text(value: Any) -> str:
    """Cap length and strip control characters, keeping newlines and tabs."""
    text = "".join(ch for ch in str(value or "") if ch >= " " or ch in "\n\t")
    return text.replace("\r", "").strip()[:EDIT_TEXT_CAP]


def plan_knowledge_edit(
    kind: str, record: Mapping[str, Any] | None, op: str, text: Any = ""
) -> KnowledgeEditPlan:
    """Plan the change for one addressed operation, or refuse.

    ``record`` is the RAW store record, already resolved from a handle by the caller.
    ``None`` means the handle resolved to nothing and is refused as ``not_addressable``,
    never told apart from any other miss: a caller must not learn which handles exist.

    Refusal reasons are machine-stable: ``unknown_op``, ``unknown_kind``,
    ``not_addressable``, ``invalid_value``, ``forbidden_field``.
    """
    op_name = str(op or "").strip().lower()
    if op_name not in KNOWLEDGE_OPS:
        return KnowledgeEditPlan(refusal="unknown_op")
    field = EDIT_FIELDS.get(str(kind or "").strip())
    if field is None:
        return KnowledgeEditPlan(refusal="unknown_kind")
    if not record:
        return KnowledgeEditPlan(refusal="not_addressable")

    if op_name == "forget":
        return KnowledgeEditPlan(remove=True)

    new_text = _sanitize_text(text)
    if not new_text:
        # An empty edit is not a forget in disguise: forgetting is its own explicit op.
        return KnowledgeEditPlan(refusal="invalid_value")
    plan = KnowledgeEditPlan(writes={field: new_text})

    # Runtime enforcement of the module's invariant, checked on the TABLE'S OUTPUT.
    if EXPOSURE_READ_FIELDS & set(plan.writes):
        return KnowledgeEditPlan(refusal="forbidden_field")
    return plan
