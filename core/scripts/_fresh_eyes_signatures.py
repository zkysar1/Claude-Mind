#!/usr/bin/env python3
""": compute sha1[:12] content signatures for fresh-eyes-code's
last_fresh_eyes_run record (and the gate's fresh_eyes_last_fire WM record).

Input: JSON array of repo-relative paths via --files-json or stdin.
Output: JSON dict {path: sha1_12} on stdout.

Files unreadable at hash time are silently omitted from the output dict.
The reader treats missing entries as "no signature available for this path"
and falls back to path-only coverage (backward-compat with pre-573 records).

Path resolution: PROJECT_ROOT env var, fallback to CWD.

file_sig is the ONE signature algorithm. Both writers (this CLI and the
post-state-update-gate.sh fresh_eyes_last_fire writer) and the reader
(_fresh_eyes_coverage_check.py) import it, so a recorded signature is always
compared with one computed the same way.

Usage:
  py -3 core/scripts/_fresh_eyes_signatures.py --files-json '["a.sh","b.sh"]'
  echo '["a.sh","b.sh"]' | py -3 core/scripts/_fresh_eyes_signatures.py
"""
import argparse
import hashlib
import json
import os
import sys

#  / : force utf-8 on stdin/stdout/stderr (covers Windows
# cp1252 fallback when callers bypass the _platform.sh PYTHONIOENCODING=utf-8
# shim). Closes acceptance (4) of  — stdin-ingest sweep.
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()


def file_sig(rel_path, root):
    """sha1[:12] of a file's content with CRLF read as LF; None if unreadable.

    git checks one commit out with CRLF line endings on a Windows box
    (core.autocrlf) and with LF elsewhere. Over raw bytes, a record written on
    one box never matched a reading on the other, so every cross-platform
    record read as an amend."""
    full = os.path.join(root, rel_path) if root else rel_path
    try:
        with open(full, "rb") as f:
            return hashlib.sha1(f.read().replace(b"\r\n", b"\n")).hexdigest()[:12]
    except (OSError, IOError):
        return None


def compute_signatures(files, root):
    sigs = {}
    for p in files:
        if not isinstance(p, str) or not p.strip():
            continue
        sig = file_sig(p, root)
        if sig is not None:
            sigs[p] = sig
    return sigs


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--files-json", help="JSON array of paths; default: read stdin")
    args = ap.parse_args()
    raw = args.files_json if args.files_json is not None else sys.stdin.read()
    try:
        files = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        print("{}")
        return 0
    if not isinstance(files, list):
        print("{}")
        return 0
    root = os.environ.get("PROJECT_ROOT") or os.getcwd()
    print(json.dumps(compute_signatures(files, root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
