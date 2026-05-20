"""Daemon-safe gate logic — pure-function modules for orchestration gates.

Each module exports `evaluate(...)` that returns a JSON-serializable dict
matching the legacy CLI's stdout payload. The CLI wrappers in
`core/scripts/<name>-gate.py` are thin argv → evaluate() → print shims
that preserve byte-identical output with the pre-extraction scripts.

The daemon imports these modules directly (skip subprocess startup cost)
once the corresponding writer endpoint is daemonised. Until then, the
subprocess.run callers in aspirations.py and elsewhere continue to spawn
the CLI wrappers — both paths route through the same evaluate() function.

Invariant: NO module here imports state that would change per-request
(stdio reconfigure, _gate_log MIND_AGENT bake-in, etc.). Every input
comes through evaluate() args, every side-effect is explicit and bounded.
"""
