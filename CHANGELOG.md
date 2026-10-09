# Changelog

## 0.1.0 — 2026-10-09

### Added

- `agent-lanes` skill for Codex and Claude Code. It makes the current agent a thin coordinator with `start`, `resume`, `status` and `handoff` commands, and runs only when invoked explicitly.
- Phase-loaded references for setup, planning, models and effort, message protocol, review and merge, and handoff.
- Lane contract injected into every first turn, with a structured `LANE REPORT`.
- `lanectl.py`, standard library only:
  - runs and lanes, with a ledger outside the repository
  - detached CLI launches with session resume
  - stop, wait and status with token and cost reporting
  - ownership and overlap checks
  - resume packets and safe cleanup
- An effort ceiling of `high`, enforced by `lanectl`. Going above it requires a recorded user instruction.
- A dated model roster and selection guide for Claude and Codex models.
- Adapter overrides for CLI flag changes.
- Tests with fake CLIs, activation cases, context budgets and manifest checks.
