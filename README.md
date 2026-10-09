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

Requirements: Python 3.10+, git, and at least one of the `claude` or `codex` CLIs, signed in.

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
lanectl check                                   run a lane's validation; print only pass/fail and failing lines
lanectl scope                                   detect out-of-ownership changes and cross-lane overlaps
lanectl queue | merged | sync                   merge queue, record a merge, rebase idle lanes
lanectl usage                                   tokens (fresh, cache write, cache read, output), cost and time per lane
lanectl note | packet                           ledger entries and the resume packet
lanectl cleanup                                 safe worktree and branch removal
```

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

New runs snapshot shared contracts. Existing runs acquire a snapshot on their next command, while already-started sessions retain their earlier context. Old review lanes without `--review-of` must be replaced with exact-commit reviewers. Reviewer overrides are intentionally ignored; implementer overrides remain supported and freeze on first use. No global Codex configuration is changed.

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
