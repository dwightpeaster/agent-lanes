# Changelog

## 0.2.1 — 2026-10-09

### Fixed

- Claude reviewers can read their review packet. They ran in their own checkout without access to the packet folder, so every Claude review was denied. Reviewers now get `--add-dir` for their packet folder. A live test with real Claude lanes confirms it.
- A clean rebase keeps a review approval only when the lane's own changes are identical (same `git patch-id`). Otherwise the approval is cleared and the lane leaves the merge queue for a correction review. Before, an approval for an older commit could still let a rebased lane through.
- Brief text is no longer rewritten. Placeholders were also substituted inside the prompt, so a brief mentioning `{model}` or `{contract}` was corrupted.
- `lanectl queue` and `merged` no longer fail on a lane with uncommitted changes or an unknown `--blocked-by` lane. The queue shows the reason instead.
- `setup`, `lane add`, `review prepare`, `sync --check` and `merged --check` no longer hold the run lock while commands run. `status`, `wait` and `stop` stay responsive during long installs and tests.
- A timeout now stops the whole process group, so installers and test runners don't keep writing into the worktree.
- Codex reviewers disable only features the installed Codex knows and MCP servers it can address; anything else is reported as a warning instead of failing every Codex review.
- Runs created before 0.2.0 keep working: their lanes don't need `lanectl setup`, and `sync --check` doesn't block them. After a merge that changes a lockfile, `sync --check` re-runs setup before checking.
- Integration and baseline checkouts are removed after they finish, even when checks leave files behind. Failed integration checkouts are kept for inspection.
- Untracked files left by checks (build output, coverage files) no longer block review, integration or the queue. Only uncommitted changes to tracked files do.
- `lanectl brief` keeps a lane's explicit `--validate`, and the secret scanner no longer runs twice or counts as configured validation.
- `launch-group --warm-cache` launches independent groups even when one group's leader fails, honours `--warm-timeout` above 60 seconds, and treats a Codex leader as warm once its first call returns.
- `launch --dry-run` no longer saves anything to the run, and launches no longer fail when the CLI can't be inspected.
- A free-text review policy such as "not required" or "No." no longer makes review mandatory.
- `send` to a reviewer keeps the message, and works without one.
- Claude reviewers load only your user settings, not project settings from the commit under review, and keep their lane's deny rules.
- Timeouts and failed git commands print an error instead of a traceback.
- Sensitive paths are matched as whole words, so `author.py` and `clock.py` no longer count as risky.
- Numbered acceptance criteria are recognized.
- Integration merges no longer depend on your git identity or run repository hooks.
- `run new` accepts a base that doesn't resolve locally yet, as 0.1 did.
- Dependency tool versions are read from the lane's worktree, so per-directory version managers give the right answer.
- `run.json` keeps only a few per-call cache samples, and the token-budget watcher re-reads a lane's output only when it grows.
- The cache benchmark measures implementation lanes with the flags they actually use.

### Added

- `tests/live_smoke.py`: an opt-in test that runs a real Claude implementation lane and a real Claude reviewer before a release. It fails on 0.2.0 and passes on 0.2.1.
- `benchmarks/claude-cache-2026-10-09.json` with the raw measurements behind the README's token figures.

### Upgrade Notes

- Update the plugin as in the README. No run or repository changes are needed.
- Lanes in runs created with 0.2.0 keep their state. A lane that 0.2.0 blocked only because of a stale setup can be unblocked with `lanectl setup`.

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

### Added: isolated lanes, gated reviews and integration

- Fix `sync --check` skipping validation for already-current branches; propagate check failures through sync/merged exit codes.
- Add fingerprinted dependency setup, shared download caches, cross-run test namespaces/port leases, runtime environment templates and launch readiness checks.
- Generate normalized ticket briefs with mapped start files/dependencies and explicit coordinator approval after assigning ownership.
- Require disposable combined-result validation for new runs; bind proofs to exact commits, gates, environments and dependency/resource order. Add opt-in candidate/base regression evidence and redacted configured secret scanners.
- Add compact exact-commit CI, run summaries and paired completed-run comparisons including reported cache writes. Keep unreported coordinator cost and dollar savings unknown.
- Add soft reported-token limits with explicit blocked state; document macOS/Linux support. Disable Claude attribution in lane defaults and repository settings; omit agent co-author trailers.
- Freeze role contracts per run and adapter overrides on first use; canonicalize Claude extra tools. Use supported dynamic-system-context exclusion and explicit system-prompt snapshots.
- Add configuration fingerprints, deduplicated first-call Claude cache samples, and unknown values for unreported Codex cache-write counts and cost.
- Add optional `launch-group --warm-cache`, preserving unstarted followers after timeout/failure. Add an opt-in Codex developer-instruction boundary experiment and a reproducible live CLI benchmark, requiring external-call authorization.
- Require reviewer targets; create separate detached checkouts at exact commits. Add a dedicated JSON review contract. Claude reviewers have no Bash/Edit/Write/MCP/hooks; Codex reviewers have read-only shell sandboxing and separately disabled connectors/plugins/hooks/delegation/MCP.
- Add `review prepare` with checks, scope, CI disposition, criteria, decisions and bounded inline diffs. Add `review findings` to verify source quotes/lines and separate blockers from follow-ups; `send --review-from` forwards only validated current blockers and enforces one correction round.
- Re-review validated corrections with deltas. Reuse one reviewer sequentially across targets with a fresh packet. Changed gates/criteria require full review; stale approvals cannot queue a changed commit.
- Add advisory, configurable `risk` recommendations; required reviews and sensitive changes cannot skip validated review at the merge queue.
- Local synthetic packet measurement: 180,261 diff bytes became a 1,054-byte initial packet with patch references; a one-line fix produced a 127-byte delta. Reproduce it with `scripts/benchmark_packets.py`. This does not measure total model-token savings. New live benchmarks remain pending authorization.

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
