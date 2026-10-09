"""Skip a test that needs the frontier origin's own world, in a world that is not that origin ().

Some tests assert on records only the frontier origin's world holds: its completed goal ids, its
capability catalog, its fleet's agent dirs. Run in a downstream deployment they fail for a reason
that is not a defect in the code under test (the v2.12.95 adopt rolled back on 95 such reds).

The signal is the world's own declaration: config/compatibility.yaml `self_role`, the same field
promote-to-upstream.sh reads. `frontier` runs the test. A downstream or seed role skips it with the
reason in the report. An unreadable role RUNS it: a wrong skip deletes coverage everywhere, a wrong
run costs a red on one box, the same direction run-full-suite takes for fleet_layout.
"""
import functools
from pathlib import Path

import pytest


def read_role(world_dir):
    """self_role from <world_dir>/config/compatibility.yaml, or None when it cannot be read."""
    try:
        import yaml
        text = (Path(world_dir) / "config" / "compatibility.yaml").read_text(encoding="utf-8")
        role = (yaml.safe_load(text) or {}).get("self_role")
    except Exception:
        return None
    return role if isinstance(role, str) else None


@functools.lru_cache(maxsize=1)
def world_self_role():
    import _paths
    return read_role(_paths.WORLD_DIR)


def skip_reason(role):
    """None when the test should run, else the reason it is skipped."""
    if role in (None, "frontier"):
        return None
    return "needs the frontier origin's own world data; this world's self_role is %r (g-358-244)" % role


def requires_frontier_world(why):
    """A pytest mark (module `pytestmark` or test decorator): skip unless this world is the frontier's."""
    reason = skip_reason(world_self_role())
    return pytest.mark.skipif(reason is not None, reason="%s: %s" % (why, reason) if reason else why)
