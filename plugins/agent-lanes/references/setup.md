# Session Setup

Run at the start of every new run. Keep it short; the goal is a correct profile, not a survey.

## 1. Check the tools and the saved profile

`lanectl doctor`. Note which CLIs exist. If only one of Claude Code or Codex is installed, every lane uses that one.

`lanectl profile show --repo .`. The repository's profile lives in a managed block in its `AGENTS.md`. If it has no missing fields, skip to step 4 and ask only for the run's scope. Otherwise do steps 2 and 3 for the missing fields only.

## 2. Read the repository's rules

Read only what exists of: `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, workflow or agent docs they point to, the PR template, CI config, and the task-runner scripts. Don't read source code.

If the repository has its own ticket or tracker tooling described in those files, use that tooling for every ticket change. Never edit ticket files by hand, and never copy ticket content into the ledger. Refer to tickets by ID.

## 3. Fill the profile

Mark each field **repo** (found) or **ask** (missing). Profile keys are in brackets.

| Field | Need |
|---|---|
| Work source [work_source] | Tracker or ticket tool, project, and how to read a ticket with its relations |
| Base branch [base] | Where lanes branch from and PRs target |
| Branch naming [branch] | Pattern for lane branches |
| Worktree location [worktrees] | Where lane worktrees go. Default suggestion: `auto` (inside the run folder, outside the repo) |
| Validation [validate] | The PR gate and per-lane test commands |
| Smoke tests [smoke] | Required or not, the command, what "green" means |
| PR flow [pr] | Draft first? Labels? Required reviewers? |
| Merge [merge] | Who authorizes, method (squash/merge/rebase), subject format, delete branch after |
| Review [review] | Does the repo require a review agent or human review? |
| Tracker updates [tracker] | Which states to set and which comments are required, and by whom |
| Protected [protected] | Environments, stacks or data that must never be mutated, and what is allowed |
| Ordered resources [resources] | Migration numbers or similar, and how they are applied |
| Deploy [deploy] | Whether the coordinator deploys, the trigger and the procedure |
| Cleanup [cleanup] | Remove worktrees and branches after merge? |

## 4. Ask once

Ask every **ask** field in one batch, with a suggested default for each. Also ask the **scope of this run** (which project, tickets or goal), and the **autonomy mode** [mode] if the profile lacks it: `review` (default: review automatically, ask before merging) or `merge-on-green` (review and merge when green per repo rules).

When you asked about any profile field, also ask: "Save these answers to AGENTS.md so future runs skip setup?" On yes, write them and tell the user the file is changed but uncommitted. This is the only edit you make in the main checkout.

```
lanectl profile set --repo . base=main "validate=npm test; npm run lint" merge=squash cleanup=yes
```

Keep values short; every Codex lane reads `AGENTS.md`.

## 5. Create the run

```
lanectl run new --repo . --name "<short name>"
```

`run new` takes base, mode, validation and protected rules from the profile. Override with `--base`, `--mode`, or `lanectl run set --run <dir> --validate "<cmd>; <cmd>" --protected "<rule>; <rule>"`.

Log the user's answers that change behavior with `lanectl note --kind directive`. Then confirm the profile to the user in five lines or fewer and move to planning.
