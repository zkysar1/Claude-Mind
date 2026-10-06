"""A ratchet baseline cannot be raised by a hand edit through the own-cloud lanes
(g-115-9813, 2026-10-03).

The report (bravo, cc-05, 2026-09-12): an Edit-tool change of a baseline in
meta/audit-baselines.yaml from 459 to 460 read back as 459, with the Edit tool
reporting success and the local file carrying a key order the edit did not
produce. This file pins the mechanism end to end, against moto's real S3
conditional-write semantics, with ONE manifest per simulated machine (the
harness of test_sync_file_baseline_fast_path_g115_8029.py), so each result is
the own-cloud sync code's, not a local backend's (a local backend has no merge
and would hand-test green, guard-1943).

What the six tests establish:

1. The PostToolUse push lane (sync_file) LANDS an upward edit while no peer has
   moved the store since this machine's baseline (the g-115-8029 fast path).
2. Reproduction of the 2026-09-12 probes: drop the baseline from that lane (the
   shape before g-115-8029, 2026-09-15, when sync_file passed none) and the
   same lone edit, with no peer anywhere, takes the union lane for the object
   S3 already holds. The store's handler,
   coordination_merge.merge_audit_baselines, merges `baseline` by MIN, so the
   merge keeps the LOWER value, writes the merged bytes over the local file,
   and reports a landing counter (pushed_merged), which the hook counts as
   `landed`. That is the silent revert, and before the fix it was deterministic.
3. With the baseline passed, the same revert still happens whenever a peer has
   moved the store (both moved -> diverged_merged -> MIN).
4. A landed upward edit does not survive the next peer write either: the
   peer's own union merge takes the MIN again.
5. Control: with no merge handler the same divergence is FROZEN, not reverted,
   so the value revert needs the registered MIN rule.
6. Counterfactual: swap the handler for a MAX merge and the edit sticks, so the
   MIN rule alone is what turns the edit into a revert.

The design is deliberate (core/config/conventions/audit-baselines.md): a
baseline is the lowest count ever recorded, and a merge that took the higher
side would silently un-ratchet the metric for the whole fleet.
"""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")
from moto import mock_aws  # noqa: E402

BUCKET = "zds-data"
LOCKS = "zds-locks"
SESSIONS = "zds-sessions"
REGION = "us-east-2"
ENV_ID = "ayoai-mind"

BASELINES = "audit-baselines.yaml"          # merge-registered by basename
UNREGISTERED = "g9813-unregistered-baselines.yaml"   # same shape, no handler
KEY = "unchecked_writes"
ROW1 = {"recorded_at": "2026-09-12T07:00:00", "drift_total": 459,
        "verdict": "stable", "breakdown": {"unverified": 459}}
ROW2 = {"recorded_at": "2026-09-12T07:31:00", "drift_total": 459,
        "verdict": "stable", "breakdown": {"unverified": 459}}


def _doc(baseline, rows, last):
    entry = {"baseline": baseline, "matcher": "strict_unverified",
             "last_recorded": last, "last_verdict": "stable", "history": rows}
    return yaml.dump({KEY: entry}, default_flow_style=False,
                     sort_keys=False).encode("utf-8")


V1 = _doc(459, [ROW1], "2026-09-12T07:00:00")
# The hand edit, literally: one line of the file changes, key order untouched.
A_EDIT = V1.replace(b"baseline: 459", b"baseline: 460", 1)
# A peer's ratchet run: it appends a history row and leaves the floor at 459.
B_WRITE = _doc(459, [ROW1, ROW2], "2026-09-12T07:31:00")


def _baseline(raw: bytes) -> int:
    return yaml.safe_load(raw)[KEY]["baseline"]


def _rows(raw: bytes) -> list:
    return yaml.safe_load(raw)[KEY]["history"]


@pytest.fixture(autouse=True)
def _default_machine_id(monkeypatch):
    monkeypatch.setenv("MACHINE_ID", "test-machine-ci")


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
              "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        ddb = boto3.client("dynamodb", region_name=REGION)
        s3.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION})
        for table, key in ((LOCKS, "lock_key"), (SESSIONS, "session_key")):
            ddb.create_table(
                TableName=table,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST")
        yield {"s3": s3, "ddb": ddb, "root": tmp_path}


def _machine(cloud, machine_id, name=BASELINES):
    """One simulated machine: its own meta cache root + backend, sharing the
    mock S3/DDB with every other machine (root_map so both map to the SAME
    `<env>/meta/...` key, the shape two writers actually collide on)."""
    from owncloud_backend import OwnCloudBackend
    meta_root = cloud["root"] / machine_id / "meta"
    meta_root.mkdir(parents=True, exist_ok=True)
    be = OwnCloudBackend(
        env_id=ENV_ID, bucket=BUCKET, lock_table=LOCKS,
        sessions_table=SESSIONS, root_map=[(meta_root, "meta")],
        machine_id=machine_id, region=REGION,
        s3=cloud["s3"], ddb=cloud["ddb"])
    return be, meta_root / name


def _act_as(monkeypatch, cloud, machine_id):
    """Point the sync manifest at THIS machine's runtime dir (see the 
    harness): switching RUNTIME_DIR IS switching machines."""
    monkeypatch.setenv("RUNTIME_DIR",
                       str(cloud["root"] / machine_id / "_owncloud_rt"))


def _s3_body(cloud, be, path) -> bytes:
    key = be._s3_key(path)
    return cloud["s3"].get_object(Bucket=BUCKET, Key=key)["Body"].read()


def _push(be, path) -> tuple[int, dict]:
    """The production PostToolUse lane: owncloud-push-on-write.sh POSTs the
    daemon route, which calls exactly this."""
    from owncloud_sync import sync_file
    stats: dict = {}
    rc = sync_file(be, path, dry_run=False, stats_out=stats)
    return rc, stats


def _seed_with_peer_write(cloud, monkeypatch, name=BASELINES):
    """A pushes V1 (the baseline), then peer B pushes its ratchet write.
    Returns (be_a, file_a, be_b, file_b)."""
    be_a, file_a = _machine(cloud, "A", name)
    be_b, file_b = _machine(cloud, "B", name)
    _act_as(monkeypatch, cloud, "A")
    file_a.write_bytes(V1)
    rc, st = _push(be_a, file_a)
    assert rc == 0 and st.get("pushed") == 1, st
    _act_as(monkeypatch, cloud, "B")
    file_b.write_bytes(B_WRITE)
    return be_a, file_a, be_b, file_b


def test_upward_edit_lands_while_no_peer_has_moved_the_store(
        cloud, monkeypatch):
    """The  fast path: remote == this machine's baseline and local
    changed -> fenced mirror_put. No merge runs, so the MIN rule is never asked
    and a hand-raised baseline reaches the store."""
    be_a, file_a = _machine(cloud, "A")
    _act_as(monkeypatch, cloud, "A")
    file_a.write_bytes(V1)
    rc, st = _push(be_a, file_a)
    assert rc == 0 and st.get("pushed") == 1, st

    file_a.write_bytes(A_EDIT)
    rc, st = _push(be_a, file_a)
    assert rc == 0 and st.get("pushed") == 1, st
    assert not st.get("pushed_merged") and not st.get("diverged_merged"), st
    assert _baseline(_s3_body(cloud, be_a, file_a)) == 460


def test_before_g115_8029_the_same_lone_edit_reverted_with_no_peer_involved(
        cloud, monkeypatch):
    """Reproduces the 2026-09-12 probes. Drop the manifest baseline from the hook
    lane (the pre-g-115-8029 shape: sync_file passed none) and A's lone edit,
    with no peer anywhere, takes the union lane for the object S3 already
    holds: MIN(460, 459) -> 459, written back over the local file and reported
    as a landed `pushed_merged`. Same edit as the first test; only the baseline
    differs, which is what 'edit, immediate read-back, 459' measured."""
    import owncloud_sync
    monkeypatch.setattr(owncloud_sync, "_manifest_entry",
                        lambda entry: (None, None))
    be_a, file_a = _machine(cloud, "A")
    _act_as(monkeypatch, cloud, "A")
    file_a.write_bytes(V1)
    rc, st = _push(be_a, file_a)
    assert rc == 0 and st.get("pushed") == 1, st             # S3 absent: plain put

    file_a.write_bytes(A_EDIT)
    rc, st = _push(be_a, file_a)
    assert rc == 0 and st.get("pushed_merged") == 1, st
    assert _baseline(_s3_body(cloud, be_a, file_a)) == 459
    assert _baseline(file_a.read_bytes()) == 459
    assert file_a.read_bytes() != A_EDIT


def test_upward_edit_is_reverted_by_the_min_merge_when_a_peer_moved_the_store(
        cloud, monkeypatch):
    """THE REPORTED SYMPTOM. A peer's ratchet write moved the store off A's
    baseline. A's hand edit (459 -> 460) now meets the union lane: both moved
    -> merge_audit_baselines -> baseline = MIN(460, 459) = 459. The merged
    bytes are written to the store AND over A's local file, so the edit is gone
    from disk, and the push reports a landing counter (`*_merged`), which the
    hook's verdict counts as `landed`: success is reported over a reverted
    edit. History is a content union, so no row is lost."""
    be_a, file_a, be_b, file_b = _seed_with_peer_write(cloud, monkeypatch)
    rc, st = _push(be_b, file_b)
    assert rc == 0 and st.get("pushed_merged") == 1, st     # B's write landed
    assert _baseline(_s3_body(cloud, be_b, file_b)) == 459

    _act_as(monkeypatch, cloud, "A")
    file_a.write_bytes(A_EDIT)
    rc, st = _push(be_a, file_a)
    assert rc == 0 and st.get("diverged_merged") == 1, st    # a "landed" counter
    assert st.get("errors", 0) == 0

    assert _baseline(_s3_body(cloud, be_a, file_a)) == 459   # the store kept MIN
    assert _baseline(file_a.read_bytes()) == 459             # the edit is gone locally
    assert file_a.read_bytes() != A_EDIT
    stamps = sorted(r["recorded_at"] for r in _rows(file_a.read_bytes()))
    assert stamps == [ROW1["recorded_at"], ROW2["recorded_at"]]


def test_a_landed_upward_edit_is_reverted_by_the_next_peer_write(
        cloud, monkeypatch):
    """Landing is not surviving. A's edit reaches the store (the fast path), then
    peer B, which never pulled it, pushes its own ratchet write from a copy that
    still carries the 459 floor. B's union merge takes MIN again: the store is
    back at 459 with A's edit gone. Peers write this file continuously, so an
    upward edit lives only until the next one."""
    be_a, file_a, be_b, file_b = _seed_with_peer_write(cloud, monkeypatch)

    _act_as(monkeypatch, cloud, "A")
    file_a.write_bytes(A_EDIT)
    rc, st = _push(be_a, file_a)
    assert rc == 0 and st.get("pushed") == 1, st
    assert _baseline(_s3_body(cloud, be_a, file_a)) == 460

    _act_as(monkeypatch, cloud, "B")
    rc, st = _push(be_b, file_b)
    assert rc == 0 and st.get("pushed_merged") == 1, st
    assert _baseline(_s3_body(cloud, be_b, file_b)) == 459


def test_control_without_a_handler_the_divergence_freezes_instead_of_reverting(
        cloud, monkeypatch):
    """Isolation: the same lane, the same divergence, a file with NO merge
    handler. Nothing merges, so nothing is lowered: the push is a CONFLICT skip
    (`diverged_skipped`), A's local keeps its edit and the store keeps the
    peer's write. The value revert therefore needs the registered MIN rule."""
    be_a, file_a, be_b, file_b = _seed_with_peer_write(
        cloud, monkeypatch, UNREGISTERED)
    rc, st = _push(be_b, file_b)
    # B has no manifest entry and nothing merges for an unregistered basename,
    # so its push is a plain put: the store now holds B's write (measured).
    assert rc == 0 and st.get("pushed") == 1 and not st.get("pushed_merged"), st
    assert _s3_body(cloud, be_b, file_b) == B_WRITE

    _act_as(monkeypatch, cloud, "A")
    file_a.write_bytes(A_EDIT)
    rc, st = _push(be_a, file_a)
    assert rc == 0 and st.get("diverged_skipped") == 1, st
    assert not st.get("diverged_merged") and not st.get("pushed_merged"), st
    assert _s3_body(cloud, be_a, file_a) == B_WRITE           # the store is untouched
    assert file_a.read_bytes() == A_EDIT                      # never rewritten locally


def test_counterfactual_a_max_merge_would_let_the_edit_stick(
        cloud, monkeypatch):
    """Attribution: replace the registered handler with one that takes the HIGHER
    baseline and rerun the symptom scenario. The edit then survives the union,
    so the MIN rule alone converts the edit into a revert. This also proves the
    third test (the symptom scenario) is not vacuous (rb-1910)."""
    import coordination_merge

    real = coordination_merge._HANDLERS[BASELINES]

    def _max_merge(local: bytes, remote: bytes) -> bytes:
        merged = yaml.safe_load(real(local, remote))
        hi = max(_baseline(local), _baseline(remote))
        merged[KEY]["baseline"] = hi
        return yaml.dump(merged, default_flow_style=False,
                         sort_keys=False).encode("utf-8")

    monkeypatch.setitem(coordination_merge._HANDLERS, BASELINES, _max_merge)
    be_a, file_a, be_b, file_b = _seed_with_peer_write(cloud, monkeypatch)
    rc, st = _push(be_b, file_b)
    assert rc == 0 and st.get("pushed_merged") == 1, st

    _act_as(monkeypatch, cloud, "A")
    file_a.write_bytes(A_EDIT)
    rc, st = _push(be_a, file_a)
    assert rc == 0 and st.get("diverged_merged") == 1, st
    assert _baseline(_s3_body(cloud, be_a, file_a)) == 460
