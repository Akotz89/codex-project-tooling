# Factory operations

This application runs on a pinned Archon SDLC source. See
[factory/MIGRATION.md](factory/MIGRATION.md) for installing and upgrading it.

Shared workflow gates validate the selected workflow. Authorization comes from
current user scope, accepted Linear work and native coordinator boundaries.
Installation, readiness or a passing gate grants no additional authority. Inspect
a run's identity and status before responding to a declared gate.

Project runtime scenarios: `harness/END-TO-END.md`.
Holdout scenarios: `.factory/holdout/HOLDOUT.md`.
Ordinary checks: `python harness/ci.py`.

Record this application's tested source SHA, candidate, run IDs, coverage,
fresh-environment evidence and open integration limits here after testing.
