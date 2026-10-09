# Agent Lanes

Agent Lanes turns the agent you're talking to into a thin **coordinator**. The coordinator splits a project into parallel **lanes**, launches the fewest Claude Code or Codex sub-agents needed through their command-line interfaces, reviews their work cheaply, and keeps you in control.

The goal is focus and lower token use. Each lane holds only its own task's context. The coordinator holds the big picture without reading every line of code. You decide anything that isn't already settled by the repository's rules.

- Works in **Codex** and **Claude Code**, and either can launch lanes in the other.
- Follows the repository's own rules (`AGENTS.md`, `CLAUDE.md`, workflow docs). Where they're silent, it asks you once and, with your OK, saves the answers to a short profile in `AGENTS.md` so later runs start without questions.
- Installs nothing else into your repositories. Run state lives in `~/.agent-lanes/`.
- No background service. Lanes are ordinary `claude -p` / `codex exec` processes.

## Install

### Codex

```bash
codex plugin marketplace add dwightpeaster/agent-lanes
codex plugin add agent-lanes@agent-lanes
```

Restart Codex after installation.

### Claude Code

```text
/plugin marketplace add dwightpeaster/agent-lanes
/plugin install agent-lanes@agent-lanes
/reload-plugins
```

Requirements: macOS or Linux (POSIX locks/process groups), Python 3.10+, git, and at least one of the `claude` or `codex` CLIs, signed in.

## Use

The skill only runs when you call it.

| Codex | Claude Code | What it does |
|---|---|---|
| `$agent-lanes` | `/agent-lanes:agent-lanes` | Start: read repo rules, ask what's missing, plan lanes, launch |
| `$agent-lanes resume` | `/agent-lanes:agent-lanes resume` | Pick up an open run from its ledger, in a fresh session |
| `$agent-lanes status` | `/agent-lanes:agent-lanes status` | Compact status of every lane, with tokens and cost |
| `$agent-lanes handoff` | `/agent-lanes:agent-lanes handoff` | Write next actions and hand off to a fresh coordinator |

A typical start:

```text
$agent-lanes
We have the tickets in the Payments project ready. Split them into lanes and get them into staging.
```

## How it works

- **Coordinator.** Plans lanes from the work items and their dependencies, maps each ticket to files with `lanectl map` (file names, sizes and definition lines, never full code), assigns file ownership, start files and reserved resources (migration numbers and similar), writes short briefs, and does all pushes, pull requests and merges. It never edits code.
- **Lanes.** Each lane is a CLI session in its own git worktree with a fixed contract: stay inside its files, commit locally, never push, and end every turn with a structured `LANE REPORT`. Lanes keep their session, so the coordinator talks to them turn by turn with `lanectl send`.
- **Review.** A thin check always runs: the report, validation evidence, ownership, and diff stats. A reviewer lane is added when the repo requires one or the work is risky, and you choose its tool and model. Reviewers return an exact change list that is forwarded unchanged to the implementing lane. After one correction round, it escalates to you.
- **Exact-commit reviewers.** Reviewers get their own detached checkout and a prepared local packet, so they review the implementer's commit without fetching a PR or reading your current branch. Findings are checked against pinned source quotes before only blocking changes reach the implementer. Follow-up suggestions stay separate; correction reviews receive only the new diff.
- **Autonomy.** You choose per run: `review` (default: review automatically, ask before merging) or `merge-on-green`.
- **Effort.** Every launch sets effort explicitly. The ceiling is `high`. Higher levels need your explicit instruction, which is recorded.
- **Merge queue.** Approved lanes merge one at a time, in an order that respects reserved resources. After each merge, `lanectl` rebases every idle lane that applies cleanly and runs its checks, so a lane is woken only for a real conflict.
- **Visibility.** Lane updates arrive in batches, and open questions come to you as one numbered list. Failures and scope drift always wait for your decision.

## The helper script

`plugins/agent-lanes/scripts/lanectl.py` (standard library only) does the deterministic work:

```text
lanectl doctor                                  check CLIs and state location
lanectl profile show|set                        the repo profile kept in AGENTS.md
lanectl run new|list|set                        create, find and configure runs
lanectl map                                     rank files and definitions matching ticket terms
lanectl lane add|set                            register lanes (worktree, ownership, start files, model, effort)
lanectl launch | send | stop                    start a lane, resume it with a message, interrupt it
lanectl launch-group --warm-cache               optionally stagger compatible lanes behind a first response
lanectl risk                                    advisory risk, review requirement and effort
lanectl review prepare | findings              gate exact-commit packets and validate reviewer evidence
lanectl wait | status | report | questions      batched changes, compact status, final report, open questions
lanectl setup                                   prepare dependencies using shared download caches
lanectl brief | approve-brief                   generate and approve a normalized ticket brief
lanectl integration | ci                        combined commit checks and compact exact-commit CI
lanectl summary | compare                       actual totals and paired completed-run comparison
lanectl check                                   run a lane's validation; print only pass/fail and failing lines
lanectl scope                                   detect out-of-ownership changes and cross-lane overlaps
lanectl queue | merged | sync                   merge queue, record a merge, rebase idle lanes
lanectl usage                                   tokens (fresh, cache write, cache read, output), cost and time per lane
lanectl note | packet                           ledger entries and the resume packet
lanectl cleanup                                 safe worktree and branch removal
```

### Prepare real application lanes

Configure `setup` before adding worktrees, for example `pnpm install --frozen-lockfile --store-dir "$LANE_PACKAGE_CACHE/pnpm"`. Each implementation lane runs setup once per lockfile, configured commands, environment templates and tool-version fingerprint. A failure blocks launch and requires an explicit `lanectl setup` retry. Repositories with lockfiles must configure setup or declare `setup_not_required=yes`. Installed dependencies stay in each worktree; only package downloads are shared. npm, pip and uv get shared cache defaults unless already configured.

Every lane receives a unique `LANE_NAMESPACE`, `LANE_DB_SUFFIX`, 16-port range, and temporary directory. Setup, agents and checks receive the same environment. Configure `lane_env` as a JSON object, or supply `run new --env-template-file env-templates.json`:

```json
{"PORT":"{port_start}","TEST_DB_NAME":"test_{namespace}","DATABASE_URL":"${TEST_DATABASE_PREFIX}{db_suffix}"}
```

Inherited values such as `TEST_DATABASE_PREFIX` are substituted at execution time and are not saved. Use test-only credentials and endpoints per repository rules. Applications must actually consume these variables for isolation to work. Reservations coordinate Agent Lanes sharing one home directory; other processes can occupy ports after probing. `LANE_PORT_PROBED=0` means sandbox restrictions prevented probing.

Implementation launches require an isolated worktree, a Goal, acceptance criteria, validation and current setup. An explicit `--no-validation-required '<user directive>'` can waive a lane check. To cut coordinator reading, `brief --ticket-file ticket.json` accepts normalized `title`, `acceptance`, `validation` and dependency item IDs; optional profile `ticket_command` is a JSON argument array with `{id}` placeholders. It proposes start files from `map` and maps known dependencies. Assign ownership, inspect the draft and run `approve-brief` before launch. Missing requirements are rejected rather than invented.

New runs require `integration` before the queue marks a lane next. It merges exact queued commits in dependency/resource order into a disposable checkout and runs the full run gate. Source lanes remain unchanged. Any changed commit, base, gate, environment or order invalidates the proof; fetch/sync the base before preparing it and recompute after each merge. `--integration optional` explicitly opts out. `sync --check` validates branches even when already up to date and returns failure for a failed check.

Optional `check --baseline-command '<focused test command>'` first requires that command to pass on the candidate, then applies changed test files to the base in an isolated checkout. Base failure is supporting regression evidence, not proof of complete coverage; imports, syntax errors and missing commands are inconclusive. Configure `secret_scan` in the profile to run a scanner as an additional gate. Its output is redacted; a missing or failing scanner blocks the check. There is no claim that simple patterns can find every secret.

`lane add --token-budget N` is a soft reported-token limit: CLI events can arrive after substantial work, so it cannot cap in-flight spending. A budget stop is recorded as blocked and never silently retried. Claude also supports its native `--budget-usd`.

`summary` reports elapsed time separately from summed agent time; closing a run saves `summary.json`. `compare` accepts two closed runs with the same starting commit and tasks, a single-session serial baseline, and explicit matching-criteria/fixtures/passing-result evidence. It compares reported agent counts, including cache writes. Coordinator calls outside lanectl remain unmeasured. Repeat controlled trials before claiming whole-workflow savings. Automatic model learning, cache-expiry resets, transient retries and push notifications remain deferred pending evidence and explicit policies.

Claude lane defaults and this repository's `.claude/settings.json` disable commit/PR attribution. Lanes are instructed to omit agent credits and co-author trailers. This does not rewrite Git history.

### Adjusting CLI flags

The CLIs change. Default launch commands are verified against Claude Code 2.1.295 and the Codex CLI docs as of October 2026. To change them without editing the plugin, put overrides in `~/.agent-lanes/adapters.json`:

```json
{
  "codex": {
    "resume": ["codex", "exec", "resume", "{session}", "--json", "-o", "{last}", "{prompt}"]
  }
}
```

Placeholders: `{prompt}`, `{model}`, `{effort}`, `{session}`, `{name}`, `{worktree}`, `{gitdir}`, `{last}`, and the list placeholders `{allow_args}`, `{deny_args}`, `{budget_args}`.

## Token use

Agent Lanes cuts tokens where they actually go:

- **Lean Claude lanes.** A Claude Code session normally loads every built-in tool, your MCP connectors and your skills, and sends them again on every step. Lanes load only Read, Edit, Write, Glob, Grep and Bash, with no MCP servers or skills. Measured on Claude Code 2.1.295, this cuts the fixed cost per step from about 45,000 to 8,000 input tokens. A lane can opt back in with `--extra-tools` or `--mcp-config`. Codex lanes use your Codex setup unchanged.
- **Stable shared rules.** Contracts are frozen per run, and adapter overrides are frozen on first use. Supported Claude CLIs move changing environment context after the system prompt. Tool ordering is deterministic and attempts record configuration fingerprints. Claude's earlier two-lane test reported cache writes falling from 1,859 to 982 (47%), while more input became cache reads; that is not a 47% reduction in total input. Cross-lane hits still depend on the full rendered prefix and provider routing. Codex keeps its existing prompt placement by default; a developer-instruction experiment is opt-in.
- **Less reading.** Each lane gets start files from `lanectl map`, so lanes don't each explore the repository. The contract tells lanes to search first, read only the line ranges they need, and never rewrite a whole file.
- **Less output.** Lanes don't narrate or summarize between steps, never echo files or logs, write one-line commit messages, and end with a short fixed report. `lanectl check` returns only pass/fail and failing lines, so test logs never enter a lane's context.
- **Cheaper review inputs.** Large changes arrive as a compact packet with per-file patch references. Re-review sends only the delta since a validated review. Small documentation-only changes may skip optional review after green gates; required reviews always win. One reviewer session can assess several small lanes sequentially with a fresh packet for each.
- **Fewer turns.** Clean rebases happen without waking the lane, and questions reach you in one batch. Optional `launch-group --warm-cache` waits for a leader response before starting matching candidate lanes; it trades startup latency for potential reuse and is not enabled by default.

Measure a run with `lanectl usage --run <dir>`.

`usage --json` separates first-call Claude samples from resumes and later calls. Codex's unreported cache-write count and dollar cost remain unknown. See [cache experiments](plugins/agent-lanes/references/cache.md) for the opt-in live benchmark and the [review workflow](plugins/agent-lanes/references/review.md) for packets, finding validation and session reuse.

A [local synthetic packet benchmark](benchmarks/packet-size.json) used a 180,261-byte diff: the initial packet was 1,054 bytes plus references, and a one-line correction produced a 127-byte delta. Relevant patches remain available for inspection. These are byte measurements, not production token or dollar savings. Reproduce it with `python3 plugins/agent-lanes/scripts/benchmark_packets.py --output /tmp/packet-size.json`. New live cache measurements have not been run.

## Safety defaults

- Claude lanes run with `acceptEdits`, only file, search and shell tools, no MCP servers or skills, an allowlist of read and local-git commands plus the repo's validation commands and `lanectl check`, and a denylist that blocks `git push`, `gh`, destructive resets and env-file reads.
- Codex lanes run with `--sandbox workspace-write`, which has no network by default.
- Reviewer lanes have separate restrictions: Claude has only Read, Glob and Grep, with no shell, editing, MCP or hooks. Codex uses `--sandbox read-only`, no writable git directory, and disabled apps, plugins, hooks, delegation and MCP servers. These reviewer settings cannot be replaced by adapter overrides.
- Lanes never touch the main checkout; each lane works in its own worktree.
- A refused merge, push or deletion stops the run and hands you the exact command.

## Update

### For users

In Codex, refresh the marketplace and reinstall the plugin, then restart Codex:

```bash
codex plugin marketplace upgrade agent-lanes
codex plugin add agent-lanes@agent-lanes
```

In Claude Code:

```bash
claude plugin marketplace update agent-lanes
claude plugin update agent-lanes@agent-lanes
```

Then run `/reload-plugins` inside Claude Code. Check the installed version with `lanectl doctor`. See [CHANGELOG.md](CHANGELOG.md) for what changed.

There is no per-repository upgrade step. The first run in a repository after updating asks the setup questions once and offers to save them to `AGENTS.md`.

### For agents

When the user asks to update Agent Lanes:

1. Run the commands above for the tool you are running in. Don't edit files under the plugin cache by hand.
2. Run `python3 <package-root>/scripts/lanectl.py doctor` and confirm the version matches the latest release.
3. Tell the user to restart Codex or run `/reload-plugins` in Claude Code before the next `agent-lanes` run.
4. Open runs keep working. Lanes started before the update keep their original session, contract and system prompt. New lanes get the new defaults.
5. If `~/.agent-lanes/adapters.json` overrides a Claude launch command, the override still wins. Compare it with the defaults (`lanectl launch --dry-run`) and tell the user which new flags it lacks. Don't change it without asking.

New runs snapshot shared contracts. Existing runs acquire a snapshot on their next command, while already-started sessions retain their earlier context. Existing implementation lanes need `lanectl setup` before further turns; old runs retain their integration opt-out until explicitly enabled. Old review lanes without `--review-of` must be replaced with exact-commit reviewers. Reviewer overrides are intentionally ignored; implementer overrides remain supported and freeze on first use. No global Codex configuration is changed.

### Upgrading to 0.2.0

- Claude lanes now load only file, search and shell tools, with no MCP servers or skills. If a lane needs one, add `--extra-tools` or `--mcp-config` when creating it.
- Validation commands are separated with `;` (comma-separated lists still work when no `;` is present).
- `run new --base` is optional when the repository's `AGENTS.md` profile sets `base`.

## Test

```bash
python -m unittest discover -s tests -v
```

## License

MIT
