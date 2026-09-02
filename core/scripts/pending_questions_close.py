#!/usr/bin/env python3
"""pending-questions-close — safely close a pending question in ANY agent's
``session/pending-questions.yaml`` via the authoritative storage backend, under
a lock. This is the cross-agent CLOSE half that g-353-03 (fleet pending-question
triage) found MISSING: the triage could READ any agent's questions from the
authoritative store, but had no safe way to CLOSE a cross-agent one — a raw
put_object clobbers a live sharded file (no lock, own-cloud race).

Safety model (why this is not a clobber):
  * ``get_backend()`` picks LocalBackend or OwnCloudBackend by ``STORAGE_BACKEND``.
  * ``acquire_lock(path)`` serializes writers (DDB conditional-put on own-cloud,
    file-lock local), so a concurrent writer cannot interleave.
  * The read-modify-write happens UNDER the lock: ``read_text`` fetches the FRESH
    authoritative content, we flip exactly one question's status, PRESERVE every
    other entry, and ``write_text`` puts it back — then re-read to VERIFY.
  * The pre-image of the closed record is emitted in the output JSON
    (archive-before-overwrite: the prior status/answer is recoverable).

Idempotent: an already-terminal question is a no-op success.
Handles both on-disk shapes: a bare list, and a dict ``{questions: [...]}``.
YAML round-trip uses ``safe_dump`` (drops comments — same limitation as the
canonical writer ``pending-questions-add.sh``; pending-questions.yaml carries no
load-bearing comments).

Exit codes: 0 closed/already-terminal/dry-run · 2 input/shape error ·
3 file-or-question not found · 4 lock failure · 5 wrote-but-verify-failed ·
6 write conflict persisted after retry (own-cloud If-Match CAS rejected the
stale write — no clobber, safety intact; the caller should re-run).
"""
import argparse
import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import yaml  # type: ignore
except ImportError:
    print(json.dumps({"error": "pyyaml_missing"}))
    sys.exit(2)

from storage_backend import get_backend  # type: ignore

# : sourced from the shared vocabulary module, NOT re-inlined here.
#
# This is the CLOSER's set: "finished, stop asking". It is a strict superset of
# the sweep's SWEEP_SETTLED, by exactly {answered, agent_answered} -- the states
# this script writes, which are finished for the asker but still owe the sweep's
# canonicalisation pass. That difference is intentional and pinned by a test; it
# is NOT the accidental disagreement this goal was filed about.
#
# Widened by this goal to also cover `retired` and `superseded`, which were absent
# here, so the closer would try to re-close an already-retired question.
from _pending_question_status import CLOSED_STATUSES as TERMINAL  # noqa: F401


def _find(qs, qid):
    for i, q in enumerate(qs):
        if isinstance(q, dict) and str(q.get("id")) == str(qid):
            return i, q
    return None, None


def _split(doc):
    """Return (is_dict_shape, questions_list). Tolerates bare list OR
    dict{questions:[...]}."""
    if isinstance(doc, dict):
        return True, (doc.get("questions") or [])
    return False, (doc or [])


def close_question(agent, qid, answered_by, rationale, dry_run, pq_path=None):
    if pq_path:
        path = Path(pq_path).resolve()
    else:
        from _paths import agent_dir  # type: ignore
        path = Path(agent_dir(agent)) / "session" / "pending-questions.yaml"
    res = {"agent": agent, "id": qid, "path": str(path)}
    be = get_backend()
    # Lock a DERIVED .lock path, NOT the resource itself. LocalBackend.acquire_lock
    # does O_CREAT|O_EXCL on the LITERAL path passed (the caller owns the .lock
    # derivation, per storage_backend.py); passing the resource path would make the
    # lock create/unlink the very file being protected — observed as a lock timeout
    # on an existing file, then release_lock deleting it. OwnCloudBackend maps the
    # same lock_path onto a DDB lock-table key, so the derivation is correct there too.
    lock_path = path.with_suffix(".lock")
    try:
        be.acquire_lock(lock_path, timeout=15)
    except Exception as e:  # lock unavailable — never write without it
        res.update(action="lock_failed", error=f"{type(e).__name__}: {e}")
        return res, 4
    # Read-modify-write-verify, retried once on an If-Match ConflictError
    # (). Own-cloud's CAS fence rejects a stale write when the object
    # changed under our lock (a non-lock-respecting writer or the sync layer
    # bumped the ETag between our read and write); we re-read the FRESH
    # authoritative content and re-apply the flip — a no-op success if the
    # interleaving writer already closed this question. The lock (held across
    # attempts) serialises lock-respecting writers; this absorbs the fence's
    # defense-in-depth rejection. Bounded to one retry; a persistent conflict
    # returns exit 6, never an uncaught traceback that crashes main().
    def _cycle():
        try:
            content = be.read_text(path)
        except Exception as e:
            res.update(action="read_failed", error=f"{type(e).__name__}: {e}")
            return res, 3
        try:
            doc = yaml.safe_load(content)
        except Exception as e:
            res.update(action="yaml_error", error=f"{type(e).__name__}: {e}")
            return res, 2
        is_dict, qs = _split(doc)
        if not isinstance(qs, list):
            res.update(action="unexpected_shape")
            return res, 2
        idx, q = _find(qs, qid)
        if q is None:
            res.update(action="not_found")
            return res, 3
        cur = str(q.get("status", "pending")).lower()
        if cur in TERMINAL:
            res.update(action="already_terminal", status=cur)
            return res, 0  # idempotent
        # archive-before-overwrite: capture the pre-image of the record we touch
        res["pre_image"] = {k: q.get(k) for k in
                            ("id", "status", "question", "text", "default_action")
                            if k in q}
        now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")  # naive UTC (TZ=UTC)
        q["status"] = "answered"
        q["answered_by"] = answered_by
        q["resolved_at"] = now
        if rationale:
            q["answer"] = rationale
        qs[idx] = q
        if is_dict:
            doc["questions"] = qs
            out = doc
        else:
            out = qs
        new_content = yaml.safe_dump(out, sort_keys=False, allow_unicode=True,
                                     default_flow_style=False)
        if dry_run:
            res.update(action="would_close", status="answered", dry_run=True)
            return res, 0
        be.write_text(path, new_content)
        # verify from the authoritative store (re-read under the same lock)
        vdoc = yaml.safe_load(be.read_text(path))
        _, vqs = _split(vdoc)
        _, vq = _find(vqs if isinstance(vqs, list) else [], qid)
        ok = vq is not None and str(vq.get("status", "")).lower() == "answered"
        res.update(action="closed", status="answered", verified=bool(ok))
        return res, (0 if ok else 5)

    # LocalBackend.conflict_error is () (empty tuple) → the except below matches
    # nothing in local mode, so the retry is a transparent single pass there.
    conflict_cls = be.conflict_error
    try:
        for attempt in range(2):  # one retry on an If-Match conflict
            try:
                return _cycle()
            except conflict_cls:
                if attempt >= 1:
                    res.update(action="conflict", error="If-Match rejected "
                               "after retry — object changed under lock")
                    return res, 6
                try:
                    be.refresh(path)  # drop stale cached ETag before re-read
                except Exception as e:
                    try:  # report, never raise — note_swallowed_backend_error ()
                        from storage_backend import note_swallowed_backend_error
                        note_swallowed_backend_error("refresh", path, e)
                    except Exception:
                        pass
    finally:
        try:
            be.release_lock(lock_path)
        except Exception as e:
            print(f"warning: release_lock failed: {type(e).__name__}: {e}",
                  file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Safely close a pending question "
                                 "in an agent's pending-questions.yaml.")
    ap.add_argument("--agent", help="agent whose question to close "
                    "(resolved via agent_dir; omit if --pq-path given)")
    ap.add_argument("--id", required=True, help="pending-question id to close")
    ap.add_argument("--answered-by", required=True,
                    help="who is closing it, e.g. bravo-on-behalf-of-user")
    ap.add_argument("--rationale", default="",
                    help="one-line closure rationale (stored as answer)")
    ap.add_argument("--pq-path", default=None,
                    help="explicit path override (test hook); must be under a "
                    "configured root")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    args = ap.parse_args()
    if not args.agent and not args.pq_path:
        print(json.dumps({"error": "need --agent or --pq-path"}))
        sys.exit(2)
    res, code = close_question(args.agent or "?", args.id, args.answered_by,
                               args.rationale, args.dry_run, args.pq_path)
    print(json.dumps(res, ensure_ascii=False))
    sys.exit(code)


if __name__ == "__main__":
    main()
