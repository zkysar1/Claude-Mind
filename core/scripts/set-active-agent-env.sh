#!/usr/bin/env bash
# FileChanged hook — previously propagated agent binding to .session-env.
# Now a no-op: agent resolution uses AYOAI_AGENT env prefix (LLM) or
# session_id from stdin (hooks). No shared env file to update.
exit 0
