#!/usr/bin/env bash
# notifications-read.sh — read world/notifications-sent.jsonl over a time window.
#
# PURE PASSTHROUGH to `notification_outreach.py list`. It adds NO arguments and
# NO parsing of its own, deliberately: a wrapper with its own flag vocabulary is
# a second place for the store's shape to live, which is the whole defect this
# file exists to close.
#
# WHY IT EXISTS WHEN THE READER ALREADY DID (). The reader was never
# missing — `notification_outreach.py list --since-hours N --json` has always
# read `ts` from one place and taken a time window. What was missing is the
# SURFACE an agent reaches for. Every other governed store answers to
# `core/scripts/<store>-read.sh` (23 of them), so `ls core/scripts/*-read.sh` is
# the census an agent runs, and notifications was the one store absent from it.
#
# MEASURED COST OF THAT ABSENCE, 2026-09-15: needing exactly this query, I ran
# the census, saw no notifications reader, hand-parsed the JSONL on a guessed
# field name (`sent_at` / `timestamp`; the field is `ts`), got a clean zero from
# 282 rows, and emailed the principal an apology for a delivery that had gone out
# 16 minutes after he asked. The row was there the whole time — this wrapper
# returns it.
#
# rc IS LOAD-BEARING IN THE OUTPUT, and this wrapper deliberately does not filter
# it for you. The ledger records ATTEMPTS, not deliveries: a send refused by the
# duplicate gate is appended with rc=4 and a `suppressed_duplicate_of` field.
# Counting a refused attempt as "they were told" is how draft-signoff-digest.py
# once suppressed an ask forever (see its own comment). Filter on rc yourself,
# knowing that is what you are doing.
#
# Usage: bash core/scripts/notifications-read.sh --since-hours <n> [--json] [--world <dir>]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/notification_outreach.py" list "$@"
