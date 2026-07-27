"""_vendor_path.py — make the per-box vendored encoder stack importable
(g-115-3115).

WHY THIS EXISTS. The retrieval embedding stack needs numpy + an encoder
(fastembed/onnxruntime) IN-PROCESS on the query hot path: cosine_scores ->
_get_model -> load_encoder runs inside the daemon, not only inside the
offline index builder. The fleet boxes run the daemon on the SYSTEM python
(`python3 -m mind_api.src`) under PEP 668 (/usr/lib/python3.12/
EXTERNALLY-MANAGED), where pip refuses to install and
`--break-system-packages` would put a 28-package tree into the interpreter
the daemon and every agent share — not an acceptable blast radius for a
retrieval optimization.

The provisioning decision (g-115-3115, box cc-02) was therefore:
`pip install --target ~/.ayoai-vendor/py`, which PEP 668 does NOT block (it
writes to a named directory, not the system environment), plus this module to
put that directory on sys.path.

WHY OUTSIDE THE REPO. The first attempt put the tree at
mind_api/state/vendor/py — gitignored, beside the per-box index cache it
serves. That broke `check_version_ssot` (test_release.py, 9 failures): the
scanner rglobs `mind_api/` for stray `__version__` assignments, and 28
third-party packages legitimately declare their own. Patching that one scanner
would have been treating the symptom — a 199MB third-party tree inside the repo
sits in the path of EVERY tool that walks it. It also follows the precedent
this subsystem already set: `load_encoder` caches its ONNX model at
`Path.home()/".ayoai-emb"`, i.e. per-box binary artifacts for the embedding
stack already live in the home dir, not in git's tree.

APPEND, NEVER PREPEND. The vendor dir is a FALLBACK provider, not an
override: it supplies what the interpreter otherwise lacks and must never
shadow a package the box already has. A box that later gets a proper
system/venv install keeps using that install and this module goes inert.
That ordering is what makes importing this module safe everywhere.

Idempotent and silent. A box with no vendor dir (the fleet default) is a
no-op, which preserves the stack's structural graceful degradation:
cosine_scores returns {} and callers fall back to token-overlap.
"""
import os
import sys
from pathlib import Path

# Per-box, outside the repo (see docstring). MIND_VENDOR_DIR overrides, which
# is what a box with a different layout — or a test — should set.
VENDOR_DIR = Path(os.environ.get("MIND_VENDOR_DIR")
                  or (Path.home() / ".ayoai-vendor" / "py"))


def ensure_vendor_path():
    """Append the per-box vendor dir to sys.path. Returns True if it is on
    the path afterwards, False when the box has no vendored stack."""
    try:
        if not VENDOR_DIR.is_dir():
            return False
        p = str(VENDOR_DIR)
        if p not in sys.path:
            sys.path.append(p)
        return True
    except OSError:
        # Never raise into the retrieval hot path — a stat failure just
        # means no vendored stack, same as the absent case.
        return False


ensure_vendor_path()
