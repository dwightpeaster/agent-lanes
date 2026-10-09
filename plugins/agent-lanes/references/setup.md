# Session Setup

Run at the start of every new run. Keep it short; the goal is a correct profile, not a survey.

## 1. Check the tools

`lanectl doctor`. Note which CLIs exist. If only one of Claude Code or Codex is installed, every lane uses that one.

## 2. Read the repository's rules

Read only what exists of: `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, workflow or agent docs they point to, the PR template, CI config, and the task-runner scripts. Don't read source code.

If the repository has its own ticket or tracker tooling described in those files, use that tooling for every ticket change. Never edit ticket files by hand, and never copy ticket content into the ledger. Refer to tickets by ID.

## 3. Fill the profile

Mark each field **repo** (found) or **ask** (missing).

| Field | Need |
|---|---|
| Work source | Tracker or ticket tool, project, and how to read a ticket with its relations |
| Base branch | Where lanes branch from and PRs target |
| Branch naming | Pattern for lane branches |
| Worktree location | Where lane worktrees go. Default suggestion: `auto` (inside the run folder, outside the repo) |
| Validation | The PR gate and per-lane test commands |
| Smoke tests | Required or not, the command, what "green" means |
| PR flow | Draft first? Labels? Required reviewers? |
| Merge | Who authorizes, method (squash/merge/rebase), subject format, delete branch after |
| Review | Does the repo require a review agent or human review? |
| Tracker updates | Which states to set and which comments are required, and by whom |
| Protected | Environments, stacks or data that must never be mutated, and what is allowed |
| Ordered resources | Migration numbers or similar, and how they are applied |
| Deploy | Whether the coordinator deploys, the trigger and the procedure |
| Cleanup | Remove worktrees and branches after merge? |

## 4. Ask once

Ask every **ask** field in one batch, with a suggested default for each. Also ask:

- **Autonomy mode:** `review` (default: review automatically, ask before merging) or `merge-on-green` (review and merge when green per repo rules).
- **Scope of this run:** which project, tickets or goal.

## 5. Create the run

```
lanectl run new --repo . --name "<short name>" --base <base> --mode <review|merge-on-green>
lanectl run set --run <dir> --validate "<cmd>,<cmd>" --protected "<rule>,<rule>"
```

Log the user's answers that change behavior with `lanectl note --kind directive`. Then confirm the profile to the user in five lines or fewer and move to planning.
