"""Regression tests for the daemon-vs-CLI environment-registry split ().

THE BUG. The daemon derives STORAGE_* from ``ENVIRONMENT_ID`` +
``core/config/environments/<id>.yaml`` (``_apply_environment_registry`` in
mind_api/src/__main__.py). Nothing on the bare-subprocess lane did —
``get_backend()`` -> ``_bootstrap_env_defaults`` read ``.env.local`` and stopped
there. So a box configured the registry-native way (ENVIRONMENT_ID only) got a
correct daemon and a CLI that silently resolved to **LocalBackend**: local-only
appends the authoritative store never sees.

WHY IT IS A TRAP, not a one-box slip. The daemon's own deprecation warning
instructs operators to "remove them from .env.local and keep ONLY
ENVIRONMENT_ID" — following that advice is what breaks the CLI lane. Observed on
cc-02: g-115-2158 removed precisely the five registry-derived keys on
2026-07-14; bare subprocesses read LocalBackend for 11 days until the keys were
restored by hand on 2026-07-25.

These tests exercise ``_apply_registry_defaults`` directly (a pure function) so
they never touch a real backend, a real bucket, or the network.
"""
import ast
import sys
from pathlib import Path

import pytest

# core/scripts on sys.path so `import storage_backend` resolves (conftest also
# does this; mirrored here so the module is importable standalone).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage_backend as sb  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]

_STORAGE_KEYS = tuple(sb.REGISTRY_KEY_TO_ENV.values())


def _mkroot(tmp_path: Path, env_id: str, body: str) -> Path:
    """Build a throwaway project root holding one registry file."""
    d = tmp_path / "core" / "config" / "environments"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{env_id}.yaml").write_text(body, encoding="utf-8")
    return tmp_path


@pytest.fixture
def clean_env(monkeypatch):
    """Clear the storage vars conftest pins, so derivation is observable."""
    for k in _STORAGE_KEYS + ("ENVIRONMENT_ID",):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


OWN_CLOUD_REGISTRY = (
    "backend: own-cloud\n"
    "bucket: test-bucket\n"
    "sessions_table: test-sessions\n"
    "lock_table: test-locks\n"
    "region: us-east-2\n"
)


def test_registry_fills_backend_when_env_local_omits_it(clean_env, tmp_path):
    """THE REGRESSION: ENVIRONMENT_ID set, no STORAGE_BACKEND -> own-cloud.

    Before the fix this left STORAGE_BACKEND unset, and get_backend()'s
    ``os.environ.get("STORAGE_BACKEND", "local")`` selected LocalBackend.
    """
    root = _mkroot(tmp_path, "test-env", OWN_CLOUD_REGISTRY)
    clean_env.setenv("ENVIRONMENT_ID", "test-env")

    sb._apply_registry_defaults(root)

    import os
    assert os.environ["STORAGE_BACKEND"] == "own-cloud"
    assert os.environ["STORAGE_S3_BUCKET"] == "test-bucket"
    assert os.environ["STORAGE_DDB_SESSIONS_TABLE"] == "test-sessions"
    assert os.environ["STORAGE_DDB_LOCK_TABLE"] == "test-locks"
    assert os.environ["AWS_DEFAULT_REGION"] == "us-east-2"


def test_explicit_env_wins_over_registry(clean_env, tmp_path):
    """The guard-955 ``STORAGE_BACKEND=local`` test pin must stay authoritative.

    setdefault semantics — identical to the .env.local pass and the daemon's own
    derivation. If this ever inverts, every hermetic test on an own-cloud box
    starts writing to the production S3 key (guard-955 / rb-2983).
    """
    root = _mkroot(tmp_path, "test-env", OWN_CLOUD_REGISTRY)
    clean_env.setenv("ENVIRONMENT_ID", "test-env")
    clean_env.setenv("STORAGE_BACKEND", "local")

    sb._apply_registry_defaults(root)

    import os
    assert os.environ["STORAGE_BACKEND"] == "local", "explicit env must win"
    # Unset siblings still fill — only the explicit one is protected.
    assert os.environ["STORAGE_S3_BUCKET"] == "test-bucket"


def test_no_environment_id_is_noop(clean_env, tmp_path):
    """Legacy N-var mode preserved: no ENVIRONMENT_ID -> registry not in play."""
    root = _mkroot(tmp_path, "test-env", OWN_CLOUD_REGISTRY)

    sb._apply_registry_defaults(root)

    import os
    for k in _STORAGE_KEYS:
        assert k not in os.environ, f"{k} must not be set without ENVIRONMENT_ID"


@pytest.mark.parametrize("env_id,body", [
    ("missing-env", None),                       # registry file absent
    ("bad-env", "just a string, not a mapping"),  # not a YAML mapping
    ("broken-env", "backend: [unclosed\n"),       # unparseable YAML
])
def test_fail_open_never_raises(clean_env, tmp_path, env_id, body):
    """FAIL-OPEN, unlike the daemon's fail-loud counterpart.

    get_backend() is reached from never-raises callers (``_gate_log.log``);
    raising here would convert a config gap into dropped records.
    """
    root = tmp_path if body is None else _mkroot(tmp_path, env_id, body)
    if body is None:
        (root / "core" / "config" / "environments").mkdir(parents=True, exist_ok=True)
    clean_env.setenv("ENVIRONMENT_ID", env_id)

    sb._apply_registry_defaults(root)  # must not raise

    import os
    assert "STORAGE_BACKEND" not in os.environ


@pytest.mark.parametrize("env_id,body", [
    ("missing-env", None),                       # registry file absent
    ("bad-env", "just a string, not a mapping"),  # not a YAML mapping
    ("broken-env", "backend: [unclosed\n"),       # unparseable YAML
])
def test_fail_open_is_loud_not_silent(clean_env, tmp_path, capsys, env_id, body):
    """: fail-open must not mean fail-SILENT.

    Sibling of test_fail_open_never_raises above — same three shapes, asserting
    the other half of the contract. Returning quietly made a config-presence
    failure indistinguishable from a deliberate ``STORAGE_BACKEND=local`` pin:
    both leave the var unset, and get_backend()'s ``.get("STORAGE_BACKEND",
    "local")`` then picks LocalBackend with errors=0 everywhere downstream.
    That is how 28 of 49 daemon starts on 2026-07-26 came up local-only
    unnoticed, stranding ~8 encodings.

    NOTE the "not a YAML mapping" case warns via a DIFFERENT path than the other
    two — it parses fine and is rejected by the isinstance check below the
    try/except — so this parametrization pins all three exits, not just the
    two inside the try block.
    """
    root = tmp_path if body is None else _mkroot(tmp_path, env_id, body)
    if body is None:
        (root / "core" / "config" / "environments").mkdir(parents=True, exist_ok=True)
    clean_env.setenv("ENVIRONMENT_ID", env_id)

    sb._apply_registry_defaults(root)

    err = capsys.readouterr().err
    assert "WARNING" in err, f"silent fail-open for {env_id!r} — the g-115-3410 regression"
    assert env_id in err, "the warning must name the ENVIRONMENT_ID in play"
    assert "LOCAL-ONLY" in err or "LocalBackend" in err, (
        "the warning must state the CONSEQUENCE (local-only writes), not just "
        "that a read failed — the consequence is the part operators act on"
    )


@pytest.mark.parametrize("label,body", [
    ("empty file", ""),                 # safe_load -> None -> `or {}` launders it
    ("mapping without backend", "unrelated: 1\n"),
])
def test_unresolved_backend_after_loop_warns(clean_env, tmp_path, capsys, label, body):
    """The FOURTH silent path — no `return` fires on it at all.

    Found by a fresh-eyes probe of this very fix (msg-20260727-045024). The
    three loudness cases above all exit via an explicit `return`; this one
    falls off the end of the derivation loop having matched zero keys. The
    empty-file case is actively MASKED by `yaml.safe_load(...) or {}`, which
    turns None into a valid-looking empty mapping that clears the isinstance
    guard. Both inputs leave STORAGE_BACKEND unset -> LocalBackend, which is
    precisely the class this instrumentation exists to eliminate.
    """
    root = _mkroot(tmp_path, "hollow-env", body)
    clean_env.setenv("ENVIRONMENT_ID", "hollow-env")

    sb._apply_registry_defaults(root)

    import os
    assert "STORAGE_BACKEND" not in os.environ, "precondition: nothing derived"
    err = capsys.readouterr().err
    assert "WARNING" in err, f"silent fall-through for {label!r}"
    assert "backend" in err


def test_explicit_pin_with_hollow_registry_stays_silent(clean_env, tmp_path, capsys):
    """CONTROL for the post-condition check — the reason it is a POST-condition.

    With STORAGE_BACKEND already pinned (the guard-955 test-runner pin, or any
    deliberate override), the setdefault in the loop is a CORRECT no-op. Had the
    check been written as "did the registry carry a backend key?" instead of "is
    the backend still unresolved?", it would false-fire on every pinned run —
    including the entire test suite, which pins STORAGE_BACKEND=local.
    """
    root = _mkroot(tmp_path, "hollow-env", "unrelated: 1\n")
    clean_env.setenv("ENVIRONMENT_ID", "hollow-env")
    clean_env.setenv("STORAGE_BACKEND", "local")

    sb._apply_registry_defaults(root)

    assert capsys.readouterr().err == "", (
        "an explicit pin must not warn — the no-op setdefault is correct here"
    )


def test_no_environment_id_stays_silent(clean_env, tmp_path, capsys):
    """CONTROL for the loudness assertions above (guard-1214).

    A legacy N-var / purely-local box has no ENVIRONMENT_ID and is NOT
    misconfigured. It must never see the warning, or the signal becomes noise
    everyone learns to ignore. Without this control, an unconditional print
    would satisfy every assertion in the test above.
    """
    sb._apply_registry_defaults(tmp_path)
    assert capsys.readouterr().err == ""


def test_successful_derivation_stays_silent(clean_env, tmp_path, capsys):
    """Second CONTROL: the healthy own-cloud path is silent.

    Pins that the warning is bound to the FAILURE branch specifically, not
    emitted on every registry read.
    """
    root = _mkroot(tmp_path, "ok-env", OWN_CLOUD_REGISTRY)
    clean_env.setenv("ENVIRONMENT_ID", "ok-env")

    sb._apply_registry_defaults(root)

    import os
    assert os.environ.get("STORAGE_BACKEND") == "own-cloud", "derivation should succeed"
    assert capsys.readouterr().err == ""


def test_local_registry_omitting_bucket_sets_only_backend(clean_env, tmp_path):
    """A local-backend registry has no bucket/tables — absent keys are skipped,
    not written as empty strings (an empty bucket would fail from_env() later)."""
    root = _mkroot(tmp_path, "local-env", "backend: local\n")
    clean_env.setenv("ENVIRONMENT_ID", "local-env")

    sb._apply_registry_defaults(root)

    import os
    assert os.environ["STORAGE_BACKEND"] == "local"
    assert "STORAGE_S3_BUCKET" not in os.environ


def test_integration_path_through_bootstrap(clean_env, tmp_path):
    """INTEGRATION PATH (sq-019), not just the leaf function.

    Every test above calls _apply_registry_defaults directly. This one walks the
    lane get_backend() actually takes — _bootstrap_env_defaults reads .env.local
    for ENVIRONMENT_ID, then hands off to the registry derivation — including the
    pytest guard (ENV_BOOTSTRAP_ALLOW_PYTEST) that lane applies. A regression
    that unwires the helper, or wires it BEFORE the .env.local pass that supplies
    ENVIRONMENT_ID, fails here while every leaf test keeps passing.
    """
    root = _mkroot(tmp_path, "test-env", OWN_CLOUD_REGISTRY)
    (root / ".env.local").write_text("ENVIRONMENT_ID=test-env\n", encoding="utf-8")
    clean_env.setenv("ENV_BOOTSTRAP_ALLOW_PYTEST", "1")

    sb._bootstrap_env_defaults(root=root)

    import os
    assert os.environ["ENVIRONMENT_ID"] == "test-env", "from .env.local"
    assert os.environ["STORAGE_BACKEND"] == "own-cloud", \
        "derived from the registry — ordering broke if this is unset"
    assert os.environ["STORAGE_S3_BUCKET"] == "test-bucket"


def test_mapping_matches_daemon():
    """LOCKSTEP GUARD — the reason two copies of the mapping are tolerable.

    The daemon cannot import storage_backend at its derivation point:
    core/scripts reaches sys.path only inside _start_owncloud_sync_thread, which
    main() calls well AFTER _apply_environment_registry. So the mapping is
    duplicated rather than shared, and this test is what makes divergence
    impossible to ship silently. Parsed with ast — importing the daemon's
    __main__ for a dict literal would drag in its module-level side effects.
    """
    src = (PROJECT_ROOT / "mind_api" / "src" / "__main__.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    daemon_map = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_REGISTRY_KEY_TO_ENV"
                for t in node.targets):
            daemon_map = ast.literal_eval(node.value)
            break

    assert daemon_map is not None, \
        "_REGISTRY_KEY_TO_ENV not found in mind_api/src/__main__.py"
    assert daemon_map == sb.REGISTRY_KEY_TO_ENV, (
        "registry mapping drifted between the daemon and the CLI lane — "
        "update BOTH mind_api/src/__main__.py::_REGISTRY_KEY_TO_ENV and "
        "core/scripts/storage_backend.py::REGISTRY_KEY_TO_ENV")


def test_live_registry_is_derivable():
    """The real committed registry for this repo parses and yields a backend.

    Catches a malformed/renamed registry landing in core/config/environments/
    without anyone noticing until a bare subprocess silently goes local.
    """
    env_dir = PROJECT_ROOT / "core" / "config" / "environments"
    registries = sorted(env_dir.glob("*.yaml"))
    assert registries, "no environment registry files committed"

    import os
    import yaml
    for reg in registries:
        data = yaml.safe_load(reg.read_text(encoding="utf-8")) or {}
        assert isinstance(data, dict), f"{reg.name} must be a YAML mapping"
        backend = str(data.get("backend", "")).strip().lower()
        assert backend in ("local", "local-files", "own-cloud"), \
            f"{reg.name} declares unroutable backend {backend!r}"
        if backend == "own-cloud":
            for key in ("bucket", "sessions_table", "lock_table", "region"):
                assert str(data.get(key, "")).strip(), \
                    f"{reg.name} is own-cloud but missing {key}"
    assert os.environ.get("STORAGE_BACKEND") == "local", \
        "conftest pin must survive this module (guard-955)"
