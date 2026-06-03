"""B16: lifecycle.runtime_dir honors MIND_RUNTIME_DIR so a daemon-integration
test can isolate its runtime files (daemon.pid/port) into a tmp dir instead of
hijacking the live daemon's PROJECT_ROOT/mind_api/state (the daemon-storm)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mind_api.src import lifecycle  # noqa: E402


def test_override_redirects_runtime_dir(tmp_path, monkeypatch):
    override = tmp_path / "isolated_state"
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(override))
    # project_root arg is IGNORED when the override is set
    d = lifecycle.runtime_dir(tmp_path / "proj" / "root")
    assert d == override
    assert d.is_dir()                      # created
    # all derived runtime files now live under the override
    assert lifecycle.pid_file(tmp_path / "proj").parent == override
    assert lifecycle.port_file(tmp_path / "proj").parent == override


def test_default_without_override(tmp_path, monkeypatch):
    monkeypatch.delenv("MIND_RUNTIME_DIR", raising=False)
    pr = tmp_path / "proj"
    d = lifecycle.runtime_dir(pr)
    assert d == pr / "mind_api" / "state"  # byte-identical to prior behavior
    assert d.is_dir()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
