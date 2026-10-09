# Ledger, Resume and Handoff

All run state lives outside the repository in `~/.agent-lanes/<repo>/<run>/` (override with `AGENT_LANES_HOME`):

- `run.json`: lanes, models, worktrees, sessions, states, usage. Written only by `lanectl`.
- `ledger.md`: directives, decisions, questions, merges and next actions. Written only through `lanectl note`.
- `lanes/<id>/`: each lane's prompts, output stream and report.

Runs are isolated. A new coordinator never reads another run.

## Keep the ledger current

Record these as they happen:

- **directive:** every instruction from the user that changes behavior, word for word.
- **decision:** choices you made that a successor must preserve.
- **question:** what is waiting on the user.
- **merge:** what landed, as which commit.
- **next:** before any handoff, the exact next actions.

Don't copy ticket content, diffs or logs into the ledger.

## Handoff (`agent-lanes handoff`, or when your context is getting long)

1. Let running lanes keep running. They don't depend on your session.
2. Write the next actions: `lanectl note --kind next --text "<ordered list>"`.
3. Make sure every open question to the user is logged.
4. Tell the user: "Handoff ready. Start a fresh session in either tool and run `agent-lanes resume` in this repo." Then stop.

Suggest a handoff yourself when your thread is long enough that re-reading it costs more than a fresh start with the packet.

## Resume (`agent-lanes resume`)

1. `lanectl run list --repo .` and ask which run if there is more than one.
2. `lanectl packet --run <dir>`. This is your entire context.
3. Check the live state: `lanectl status`, `git fetch`, and the open PRs for the lane branches.
4. Tell the user in three lines or fewer what's running, what's waiting on them, and what you'll do next. Then continue.

Resumed lanes keep their own sessions; `lanectl send` reaches them from any coordinator.

## Closing a run

Once every lane is merged or closed, give the user a final summary (merged, follow-ups, anything not exercised, and the totals line from `lanectl usage`), then `lanectl run set --run <dir> --state closed`.
