"""Decide what a user's REPLY to a digest email asks us to do ().

Pure decision logic -- no I/O, no network, no store writes. The caller supplies
the already-decoded message and applies the returned action through the canonical
goal writers. Keeping this pure is what makes the safety properties testable.

WHY THE SLICES ARE KEPT APART (rb-5258). A reply INHERITS its parent's subject AND
quotes the parent's body, so any classifier that reads the whole message is reading
the DIGEST's words as if the user had written them. That defect shipped once already:
a user reply countermanding a success notice classified identically to the success it
was countermanding, because the flags came from the inherited subject. Therefore:

  * the VERB is read from the `new` slice ONLY -- never from quoted text, never from
    the subject. The digest body itself contains the words "done" and "not needed"
    (it documents the contract), so reading the full slice would make every reply to
    every digest look like a close command.
  * the GOAL ID may be recovered from the quoted slice, because an id is a REFERENT,
    not an intent. Quoting the digest line is the sanctioned way to name the item.

AMBIGUITY IS ALWAYS A NO-OP. A reply that quotes a whole digest names many ids; a
reply carrying two verbs states two intents. Neither is guessed at. The cost of a
wrong guess is asymmetric -- closing the wrong goal silently deletes real work
(guard-1227) while an ack costs the user one more line -- so every unresolved case
returns `noop` with a reason the caller can put in that ack.
"""

import re
from typing import Dict, List, Optional

# g-NNN-NN through g-NNN-NNNN (CLAUDE.md ID Formats; widened 2026-05-19).
GOAL_ID_RE = re.compile(r"\bg-\d{1,4}-\d{2,4}\b", re.IGNORECASE)

ACTION_COMPLETE = "complete"
ACTION_DROP_USER_LEG = "drop_user_leg"
ACTION_NOOP = "noop"

# Verb patterns are anchored to a WHOLE line so that prose merely containing the
# word ("I'm not sure this is done yet") cannot trigger a close. The user is told
# the exact phrasing in the digest footer, so requiring the line to BE the verb is
# a contract, not a guess.
_DONE_RE = re.compile(r"^\s*done\b[\s.!]*", re.IGNORECASE)
_NOT_NEEDED_RE = re.compile(r"^\s*not[\s-]+needed\b[\s.!]*", re.IGNORECASE)


def _strip_signature(text: str) -> str:
    """Drop everything at or below a standard signature separator line."""
    out: List[str] = []
    for line in (text or "").splitlines():
        if line.strip() in ("--", "-- ", "__"):
            break
        out.append(line)
    return "\n".join(out)


def _candidate_lines(new_text: str) -> List[str]:
    """Lines of the reply the user actually typed, quoted lines removed."""
    lines = []
    for line in _strip_signature(new_text).splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(">"):          # a quote that survived the slice split
            continue
        if s.lower().startswith("on ") and s.rstrip().endswith("wrote:"):
            continue                    # attribution line
        lines.append(s)
    return lines


def find_verbs(new_text: str) -> List[str]:
    """Verbs stated in the reply's OWN text. Never reads quoted content."""
    verbs: List[str] = []
    for s in _candidate_lines(new_text):
        # Order matters: check the two-word verb first so "not needed" is never
        # mistaken for an unrecognised line while a bare "done" elsewhere wins.
        if _NOT_NEEDED_RE.match(s):
            verbs.append("not_needed")
        elif _DONE_RE.match(s):
            verbs.append("done")
    return verbs


def find_goal_ids(text: str) -> List[str]:
    """Distinct goal ids in `text`, lowercased, in first-seen order."""
    seen: List[str] = []
    for m in GOAL_ID_RE.finditer(text or ""):
        gid = m.group(0).lower()
        if gid not in seen:
            seen.append(gid)
    return seen


def decide(
    *,
    verified_sender: bool,
    sender: str,
    owner_address: str,
    new_slice: str,
    full_slice: str,
    message_id: str,
    already_processed: bool = False,
) -> Dict[str, Optional[str]]:
    """Return {action, goal_id, reason, verb} for one inbound reply.

    `already_processed` is the idempotency input (constraint 4): the caller checks
    its ledger for `message_id` and passes the answer in, so re-delivery of the same
    message is a no-op here rather than at some later, less-testable layer.
    """
    def noop(reason: str, **extra) -> Dict[str, Optional[str]]:
        out = {"action": ACTION_NOOP, "goal_id": None, "verb": None, "reason": reason,
               "message_id": message_id, "ack": False}
        out.update(extra)
        return out

    if already_processed:
        # Silent: an ack here would re-mail the user on every redelivery.
        return noop("duplicate: message already processed")

    # --- Gate 1: sender. Checked FIRST and independently of any content, because
    # content is exactly what an unverified sender controls (constraint 1).
    if not verified_sender:
        return noop("ignored: sender not verified", logged=True)
    if not sender or not owner_address:
        return noop("ignored: sender or owner address missing", logged=True)
    if sender.strip().lower() != owner_address.strip().lower():
        return noop("ignored: verified sender is not the owner address", logged=True)

    # --- Gate 2: verb, from the reply's own words only (rb-5258).
    verbs = find_verbs(new_slice)
    if not verbs:
        return noop("no recognised verb in the reply text", ack=True)
    if len(set(verbs)) > 1:
        return noop("ambiguous: reply states more than one verb", ack=True)
    verb = verbs[0]
    if len(verbs) > 1:
        return noop("ambiguous: verb repeated for multiple items", ack=True)

    # --- Gate 3: referent. The id may come from quoted text; the intent may not.
    ids_new = find_goal_ids(_strip_signature(new_slice))
    if len(ids_new) == 1:
        goal_id, where = ids_new[0], "reply text"
    elif len(ids_new) > 1:
        return noop("ambiguous: reply names %d goal ids" % len(ids_new), ack=True)
    else:
        ids_quoted = find_goal_ids(full_slice)
        if len(ids_quoted) == 1:
            goal_id, where = ids_quoted[0], "quoted digest line"
        elif len(ids_quoted) > 1:
            return noop(
                "ambiguous: no goal id in the reply and the quoted text names %d "
                "-- quote only the one line you mean" % len(ids_quoted), ack=True)
        else:
            return noop("no goal id in the reply or the quoted text", ack=True)

    action = ACTION_COMPLETE if verb == "done" else ACTION_DROP_USER_LEG
    return {
        "action": action,
        "goal_id": goal_id,
        "verb": verb,
        "reason": "verb '%s' from reply text; goal id from %s" % (verb, where),
        "message_id": message_id,
        "ack": False,
    }


def outcome_note(decision: Dict[str, Optional[str]], *, user_only: bool = False) -> str:
    """The outcome_note text for an applied decision.

    guard-1227: a status-terminating write must leave a recoverable trace naming the
    RULE that fired and the EVIDENCE it matched. The message id is that evidence --
    it is what lets anyone reconstruct who said so and when.
    """
    mid = decision.get("message_id") or "unknown"
    if decision["action"] == ACTION_COMPLETE:
        return ("closed by owner reply %s (reply-to-close, g-353-51): the owner "
                "replied 'done'; no agent verification was performed because the "
                "owner is the authority for this goal's user leg." % mid)
    if decision["action"] == ACTION_DROP_USER_LEG:
        if user_only:
            return ("skipped by owner reply %s (reply-to-close, g-353-51): the owner "
                    "replied 'not needed' and this goal had no agent leg to keep."
                    % mid)
        return ("user leg dropped by owner reply %s (reply-to-close, g-353-51): the "
                "owner replied 'not needed'; the agent leg is retained and the goal "
                "stays actionable." % mid)
    return ""
