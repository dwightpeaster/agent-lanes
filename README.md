# Agent Lanes

Agent Lanes turns the agent you're talking to into a thin **coordinator**. The coordinator splits a project into parallel **lanes**, launches the fewest Claude Code or Codex sub-agents needed through their command-line interfaces, reviews their work cheaply, and keeps you in control.

The goal is focus and lower token use. Each lane holds only its own task's context. The coordinator holds the big picture without reading every line of code. You decide anything that isn't already settled by the repository's rules.

- Works in **Codex** and **Claude Code**, and either can launch lanes in the other.
- Follows the repository's own rules (`AGENTS.md`, `CLAUDE.md`, workflow docs). Where they're silent, it asks you.
- Installs nothing into your repositories. Run state lives in `~/.agent-lanes/`.
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

- **Coordinator.** Plans lanes from the work items and their dependencies, assigns file ownership and reserved resources (migration numbers and similar), writes short briefs, and does all pushes, pull requests and merges. It never edits code.
- **Lanes.** Each lane is a CLI session in its own git worktree with a fixed contract: stay inside its files, commit locally, never push, and end every turn with a structured `LANE REPORT`. Lanes keep their session, so the coordinator talks to them turn by turn with `lanectl send`.
- **Review.** A thin check always runs: the report, validation evidence, ownership, and diff stats. A reviewer lane is added when the repo requires one or the work is risky, and you choose its tool and model. Reviewers return an exact change list that is forwarded unchanged to the implementing lane. After one correction round, it escalates to you.
- **Autonomy.** You choose per run: `review` (default: review automatically, ask before merging) or `merge-on-green`.
- **Effort.** Every launch sets effort explicitly. The ceiling is `high`. Higher levels need your explicit instruction, which is recorded.
- **Visibility.** Lane state changes, questions, failures and scope drift are reported to you as they happen. Failures and drift always wait for your decision.

## The helper script

`plugins/agent-lanes/scripts/lanectl.py` (standard library only) does the deterministic work:

```text
lanectl doctor                                  check CLIs and state location
lanectl run new|list|set                        create, find and configure runs
lanectl lane add|set                            register lanes (worktree, ownership, model, effort)
lanectl launch | send | stop                    start a lane, resume it with a message, interrupt it
lanectl wait | status | report                  watch for changes, compact status with cost, final report
lanectl scope                                   detect out-of-ownership changes and cross-lane overlaps
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

## Safety defaults

- Claude lanes run with `acceptEdits`, an allowlist of read and local-git commands plus the repo's validation commands, and a denylist that blocks `git push`, `gh`, destructive resets and env-file reads.
- Codex lanes run with `--sandbox workspace-write`, which has no network by default.
- Lanes never touch the main checkout; each lane works in its own worktree.
- A refused merge, push or deletion stops the run and hands you the exact command.

## Update

In Codex, refresh the marketplace and reinstall the plugin:

```bash
codex plugin marketplace upgrade agent-lanes
codex plugin add agent-lanes@agent-lanes
```

In Claude Code:

```bash
claude plugin marketplace update agent-lanes
claude plugin update agent-lanes@agent-lanes
```

Then run `/reload-plugins` inside Claude Code. Agent Lanes installs nothing into your repositories, so there is no per-repository upgrade step.

## Test

```bash
python -m unittest discover -s tests -v
```

## License

MIT
