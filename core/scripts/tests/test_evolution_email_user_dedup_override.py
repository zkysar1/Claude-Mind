"""evolution-complete._email_user — the guard-380 identity notification must
survive the prior-outreach dedup gate.

Pins the g-115-7025 fix (2026-08-21). The failure this prevents is SILENT: the
material-self body cites "guard-380" by name, notification_outreach.entity_ids()
regex-extracts that as a topic entity, and match_reason() tests `shared ids`
THIRD — before subject or body similarity. So every identity notification
collides with every other one inside the 7-day window regardless of agent,
section, or content.

Measured before the fix, over world/notifications-sent.jsonl: 13 identity
notifications, 13/13 carrying guard-380 in entity_ids, 12 suppressed rc=4.
Only the first was delivered — it had nothing prior to collide against. The
one agent who noticed re-sent by hand with an override, and that punch-through
became the new dedup anchor that suppressed the next two agents.

Nothing tested _email_user before this file, which is why a function carrying
an explicit user-facing promise regressed unnoticed for four days. The promise
is guard-380's 2026-04-22 trade: the agent was released from pre-approval for
material Self edits IN EXCHANGE FOR post-notification.

Run: py -3 core/scripts/tests/test_evolution_email_user_dedup_override.py
"""
import importlib.util
import subprocess
import sys
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

ENV_KEY = "EMAIL_SEND_ALLOW_DUPLICATE"


def load_mod():
    spec = importlib.util.spec_from_file_location(
        "evo_complete", SCRIPT_DIR / "evolution-complete.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _entry(**over):
    e = {
        "file_kind": "agent_self",
        "file_path": "agents/bravo/self.md",
        "revision_id": "self-20260821T000000-bravo-test",
        "agent": "bravo",
        "change_class": "material",
        "section_changed": "Product Stage Awareness",
        "reasoning": "test",
        "signal_source": "encode-session",
    }
    e.update(over)
    return e


class _Captured:
    """Stub for subprocess.run that records the env the send would have used.

    `_email_user` does `import subprocess` INSIDE the function body, so the
    name resolves through sys.modules at call time — patching the real module's
    attribute is what reaches it. Patching an attribute on the loaded
    evolution-complete module does NOT (measured: AttributeError, the module
    has no `subprocess` attribute at all).
    """

    def __init__(self):
        self.env = None
        self.calls = 0

    def __call__(self, cmd, **kw):
        self.calls += 1
        self.env = kw.get("env") or {}

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()


def _run(event, entry):
    m = load_mod()
    cap = _Captured()
    real = subprocess.run
    subprocess.run = cap
    try:
        ref = m._email_user(entry, event)
    finally:
        subprocess.run = real
    return ref, cap


def test_material_self_sets_allow_duplicate():
    """The fix: an identity notification must not be silently deduped away."""
    ref, cap = _run("material-self", _entry())
    assert cap.calls == 1, f"transport not invoked (calls={cap.calls})"
    val = cap.env.get(ENV_KEY)
    assert val, (
        f"{ENV_KEY} not set on the material-self branch — the notification "
        "will be suppressed rc=4 by the prior-outreach gate on the shared "
        "guard-380 id, and _email_user will report it as a failure")
    assert ref and ref.startswith("email:"), f"unexpected ref: {ref!r}"


def test_override_reason_names_agent_section_and_revision():
    """The override is audited (ledger duplicate_override_reason), so it has to
    say what is actually new — a constant string would re-create the very
    indiscriminability the fix exists to remove."""
    _, cap = _run("material-self", _entry())
    val = cap.env.get(ENV_KEY) or ""
    for token in ("bravo", "Product Stage Awareness",
                  "self-20260821T000000-bravo-test"):
        assert token in val, f"override reason omits {token!r}: {val!r}"


def test_override_is_distinct_per_edit():
    """Two different edits must produce two different reasons. If they collide,
    the audit trail cannot distinguish them even though the send succeeds."""
    _, a = _run("material-self", _entry())
    _, b = _run("material-self", _entry(
        section_changed="Task Selection",
        revision_id="self-20260821T111111-bravo-other"))
    assert a.env.get(ENV_KEY) != b.env.get(ENV_KEY), \
        "override reason is identical across two distinct edits"


def test_rollback_branch_keeps_normal_dedup():
    """Scope guard. Rollback is `completion`-class status the user asked NOT to
    receive; it must keep normal dedup. A blanket override would silently widen
    the bypass to a class the routing policy deliberately suppresses."""
    _, cap = _run("rollback", _entry())
    if cap.calls:
        assert cap.env.get(ENV_KEY) is None, (
            "rollback must not set the duplicate override — only material-self "
            "is declared ALWAYS_SEND")


def test_missing_section_produces_no_dangling_separator():
    """section_changed is optional; the reason must stay well-formed without it."""
    _, cap = _run("material-self", _entry(section_changed=""))
    val = cap.env.get(ENV_KEY) or ""
    assert val, "override missing when section_changed is empty"
    assert "section:" not in val, f"dangling section separator: {val!r}"
    assert "bravo" in val and "self-20260821T000000" in val, \
        f"override lost its discriminators: {val!r}"


def run_all():
    tests = [
        test_material_self_sets_allow_duplicate,
        test_override_reason_names_agent_section_and_revision,
        test_override_is_distinct_per_edit,
        test_rollback_branch_keeps_normal_dedup,
        test_missing_section_produces_no_dangling_separator,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            print(f"  [FAIL] {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print()
    if failed:
        print(f"FAILED: {failed}/{len(tests)}")
        return 1
    print(f"OK: {len(tests)}/{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(run_all())
