#!/usr/bin/env bash
# _daemon_host_hold.sh — WHO OWNS the pid in daemon.pid: this host or another one.
# Sourced by BOTH spawn/kill sites (one definition, two callers — the
# _daemon_env_scrub.sh pattern, guard-2676):
#
#   core/scripts/_runtime.sh        rt_spawn, rt_daemon_kill   (wrapper auto-respawn)
#   core/scripts/mind-api-start.sh                             (direct / --restart launcher)
#
# THE DEFECT (, measured 2026-09-25 on an arc-agi-3 vessel mind-workspace).
# The state dir is on a shared network filesystem, so a pid recorded in daemon.pid can belong to a
# daemon on the PEER host. Both sites decide "stale" with `kill -0 <pid>` on THIS
# host, which reads the peer's live daemon as dead and removes its pid/port files
# (spawn.log line 256: "stale PID file ... cleaning up", 32 s after the peer
# started and while it was still serving). When the number happens to name a local
# process, the same pid is read as an unresponsive daemon and signalled.
#
# THE SIGNAL. The daemon writes daemon.host (its hostname) when it publishes the
# pid, and refreshes that file's mtime every 10 s (mind_api/src/lifecycle.py
# touch_host_marker). So one file says whose pid it is AND whether that host is
# still serving. A heartbeat rather than a lock: it works however the daemon was
# started and on every platform this repo supports.
#
# daemon_host_hold PID_FILE — sets three variables, prints nothing, returns 0:
#   _dhh_state  local | held | stale
#     local  no daemon.host beside the pid file, an empty one, or one naming this
#            host. Every daemon that predates the marker lands here: unchanged.
#     held   names ANOTHER host and was refreshed < _DHH_HOLD_SECONDS ago. That
#            daemon is live: leave its files alone, signal nothing, spawn nothing.
#     stale  names another host, not refreshed within the window. Its daemon is
#            gone so the files may go, but its pid is still not this host's to
#            probe or signal (a same-numbered local process is unrelated).
#   _dhh_host  the recorded host (empty when local)
#   _dhh_age   seconds since the last heartbeat (empty when local)
#
# _DHH_HOLD_SECONDS is the twin of HOST_HOLD_SECONDS in mind_api/src/lifecycle.py;
# core/scripts/tests/test_daemon_foreign_host_hold.py pins the pair.
_DHH_HOLD_SECONDS=120

daemon_host_hold() {
    local pid_file="$1" marker recorded me mtime now
    _dhh_state=local
    _dhh_host=""
    _dhh_age=""
    marker="$(dirname "$pid_file")/daemon.host"
    [ -f "$marker" ] || return 0
    recorded="$(tr -d '[:space:]' < "$marker" 2>/dev/null | tr '[:upper:]' '[:lower:]')" || recorded=""
    [ -n "$recorded" ] || return 0
    me="$( (hostname 2>/dev/null || uname -n 2>/dev/null) | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')" || me=""
    # A host that cannot name itself cannot call another one foreign: do not guess.
    [ -n "$me" ] || return 0
    [ "$recorded" != "$me" ] || return 0
    mtime="$(stat -c %Y "$marker" 2>/dev/null || stat -f %m "$marker" 2>/dev/null || echo 0)"
    now="$(date +%s)"
    _dhh_host="$recorded"
    _dhh_age=$(( now - mtime ))
    if [ "$_dhh_age" -lt "$_DHH_HOLD_SECONDS" ]; then
        _dhh_state=held
    else
        _dhh_state=stale
    fi
    return 0
}
