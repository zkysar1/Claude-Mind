#!/usr/bin/env bash
# Test runner for test_jsonl_id_race.py — concurrency smoke test for
# rb_add / guard_add auto-id allocation under concurrent load.
set -euo pipefail
# Pin storage backend to local so the concurrent rb_add/guard_add writes in the
# .py can never inherit an ambient STORAGE_BACKEND=own-cloud and collide on a
# production S3 key (guard-955, rb-3208).
export STORAGE_BACKEND=local
cd "$(dirname "$0")/../../.."
exec py -3 core/scripts/tests/test_jsonl_id_race.py
