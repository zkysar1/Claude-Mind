#!/usr/bin/env bash
# harness-capabilities.sh -- what the hosting harness can do for the loop ().
#
# Usage:
#   harness-capabilities.sh                       # harness=<name> background_job_notify=<true|false>
#   harness-capabilities.sh --get <capability>    # one value, e.g. --get background_job_notify
#   harness-capabilities.sh --json
#
# Consumers: aspirations-all-blocked B7.2 (the yield branch), idle-tick.sh and
# the cycle-cache directive printers. Logic lives in _harness_caps.py (pure,
# env-only); this wrapper exists so skill pseudocode and shell scripts get the
# python-invocation-safe path (python-invocation.md: python3 only inside a .sh
# that sources _paths.sh).
source "$(dirname "${BASH_SOURCE[0]}")/_paths.sh"
exec python3 "$(dirname "${BASH_SOURCE[0]}")/_harness_caps.py" "$@"
