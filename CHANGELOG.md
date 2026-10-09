# Changelog

### Additional v0.2.0 draft changes

- Freeze role contracts per run and adapter overrides on first use; canonicalize Claude extra tools. Use supported dynamic-system-context exclusion and explicit system-prompt snapshots.
- Add configuration fingerprints, deduplicated first-call Claude cache samples, and unknown values for unreported Codex cache-write counts and cost.
- Add optional `launch-group --warm-cache`, preserving unstarted followers after timeout/failure. Add an opt-in Codex developer-instruction boundary experiment and a reproducible live CLI benchmark, requiring external-call authorization.
- Require reviewer targets; create separate detached checkouts at exact commits. Add a dedicated JSON review contract. Claude reviewers have no Bash/Edit/Write/MCP/hooks; Codex reviewers have read-only shell sandboxing and separately disabled connectors/plugins/hooks/delegation/MCP.
- Add `review prepare` with checks, scope, CI disposition, criteria, decisions and bounded inline diffs. Add `review findings` to verify source quotes/lines and separate blockers from follow-ups; `send --review-from` forwards only validated current blockers and enforces one correction round.
- Re-review validated corrections with deltas. Reuse one reviewer sequentially across targets with a fresh packet. Changed gates/criteria require full review; stale approvals cannot queue a changed commit.
- Add advisory, configurable `risk` recommendations; required reviews and sensitive changes cannot skip validated review at the merge queue.
- Local synthetic packet measurement: 180,261 diff bytes became a 1,054-byte initial packet with patch references; a one-line fix produced a 127-byte delta. Reproduce it with `scripts/benchmark_packets.py`. This does not measure total model-token savings. New live benchmarks remain pending authorization.

## 0.2.0 — 2026-10-09

### Changed

- Claude lanes load only Read, Edit, Write, Glob, Grep and Bash, with no MCP servers and no skills. This cuts the fixed input cost per lane step from about 45,000 to 8,000 tokens. Lanes can opt in with `--extra-tools` and `--mcp-config`.
- The lane contract is identical for every lane and comes first: as an appended system prompt for Claude lanes, and at the top of the prompt for Codex lanes. Lane-specific values move to a separate assignment, so lanes share a cacheable prefix.
- The lane contract adds rules for economical reading and editing, and for writing little: no narration, no summaries outside the report, no echoed files or logs, one-line commit messages.
- Setup reads a repository profile from a managed block in `AGENTS.md` and asks only about missing fields. `run new` takes base, mode, validation and protected rules from it.
- Updates to the user arrive in batches, and open lane questions are asked as one numbered list.
- After a merge, idle lanes are rebased without a lane turn when the rebase applies cleanly. Lanes are woken only for conflicts.
- Lane cost in `status` is the sum over all turns.
- `Bash(cat *)` is no longer on the Claude lane allowlist.

### Added

- `lanectl profile show|set` for the `AGENTS.md` profile.
- `lanectl map`, which ranks files and definition lines matching ticket terms, for ownership, sizing and start files.
- `lanectl lane add --start` and `--validate` for per-lane start files and validation.
- `lanectl check`, which runs a lane's validation and prints only pass/fail and failing lines. Claude lanes may run it without prompting.
- A merge queue: `lanectl queue`, `lanectl merged` and `lanectl sync`, with ordering by reserved resources.
- `lanectl wait --settle` and `lanectl questions` for batching.
- `lanectl usage` with fresh input, cache writes, cache reads, output, cost and time per lane.
- README sections on token use and on updating, for users and agents.

### Upgrade Notes

- Update the plugin with the commands in the README, then restart Codex or run `/reload-plugins` in Claude Code.
- Open runs keep working. Lanes started before the update keep their original session and system prompt.
- If `~/.agent-lanes/adapters.json` overrides a Claude launch command, it still wins and won't get the new lean flags. Compare it with `lanectl launch --dry-run`.
- Separate validation commands with `;`. Comma-separated lists still work when no `;` is present.

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
