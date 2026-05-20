#!/usr/bin/env bash
# Test runner for test_jsonl_id_race.py — concurrency smoke test for
# rb_add / guard_add auto-id allocation under concurrent load.
set -euo pipefail
cd "$(dirname "$0")/../../.."
exec py -3 core/scripts/tests/test_jsonl_id_race.py
