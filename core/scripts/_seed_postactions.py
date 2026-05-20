"""Run post_copy_actions from the seed manifest.

Each action has:
  id, description, type (command | message), required, on_failure (warn | fail)
  If type == command: command (string), run_at (source | destination)
  If type == message: text (string)
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import _seed_engine as eng  # noqa: E402


def run_action(action: dict, dest_root: Path, source_root: Path) -> bool:
    aid = action.get("id", "?")
    desc = action.get("description", "")
    typ = action.get("type", "command")
    print(f"[post-action {aid}] {desc}")

    if typ == "message":
        print(action.get("text", ""))
        return True

    if typ == "command":
        cmd = action.get("command", "")
        run_at = action.get("run_at", "destination")
        cwd = dest_root if run_at == "destination" else source_root
        on_failure = action.get("on_failure", "warn")
        try:
            # Use shell=True on Windows since `py -3` is a launcher
            result = subprocess.run(
                cmd, shell=True, cwd=str(cwd),
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                print(f"  exit={result.returncode}")
                if result.stderr:
                    print(f"  stderr: {result.stderr[:500]}")
                if on_failure == "fail":
                    return False
                return True  # warn
            else:
                if result.stdout:
                    # Print first 5 lines
                    for line in result.stdout.splitlines()[:5]:
                        print(f"  {line}")
                return True
        except subprocess.TimeoutExpired:
            print(f"  TIMEOUT after 300s")
            return on_failure != "fail"
        except Exception as e:
            print(f"  ERROR: {e}")
            return on_failure != "fail"
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", required=True)
    p.add_argument("--dest", required=True)
    p.add_argument("--source", required=True)
    args = p.parse_args()

    manifest = eng.load_manifest(Path(args.manifest))
    dest = Path(args.dest).resolve()
    source = Path(args.source).resolve()

    actions = manifest.get("post_copy_actions", [])
    if not actions:
        print("[post-actions] none defined")
        return

    failed = 0
    for action in actions:
        ok = run_action(action, dest, source)
        if not ok:
            failed += 1

    if failed:
        print(f"[post-actions] {failed} required action(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
