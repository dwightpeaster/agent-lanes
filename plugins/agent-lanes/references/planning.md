# Planning Lanes

## Read the work, then map it

For each candidate item, read the ticket with its relations (blocks, blocked-by, execution order). Skim titles and acceptance criteria.

Then map each ticket to files without reading code:

```
lanectl map --repo . --terms "refund,invoice,PaymentService" [--paths "src/**"]
```

It ranks matching files with their size and a few definition lines. Use it to set ownership, judge size (3 small files or 20 across modules) for model and effort, and pick each lane's start files. Don't open source files yourself; if the map is ambiguous, run it again with sharper terms or launch a research lane.

## Shape the lanes

- **A lane is usually one ticket.** Group tickets into one lane only when they touch the same files and must land together. Split a large ticket into phase lanes only when the phases can land separately.
- **Research is a lane too** (`--kind research`). It reads and reports, with no worktree changes.
- **Use the fewest lanes that keep work parallel.** Launch every unblocked lane at once. A suggested range is 4–6 running; go higher when the work is truly independent, and say so.
- **Dependent work** is queued with `--blocked-by`. When a chain is strictly sequential and small, reuse one lane: send the next item to the same session instead of launching a new lane.

## Prevent collisions up front

- **File ownership per lane** (`--owns` globs). Shared files are append or extend only. Where two lanes must touch one file, split it by region in both briefs; the second to land rebases.
- **Ordered resources** (migration numbers and similar) are reserved per lane (`--reserves`). A later one never lands before an earlier one.
- Contracts, schemas and public interfaces are append-only unless the ticket says otherwise.

## Choose tool, model and effort

Use `references/models.md`. Pick the best value for each lane: the cheapest setup likely to get it right the first time. Either tool can implement or review. When both are installed, prefer a reviewer from the other tool than the implementer, but always ask the user which to use before launching any reviewer.

Exact-spec lanes (`--kind spec`) are only for work whose exact edits are already written down: in the ticket, a reviewer's change list, or an earlier lane's report. Never read code yourself to write a spec.

## Create and launch

```
lanectl lane add --run <dir> --id L1 --items T-12 --tool claude --model <id> --effort medium \
  --worktree auto --create-worktree --branch <branch> --owns "src/api/**,tests/api/**" \
  --start "src/api/refunds.py,tests/api/test_refunds.py" --validate "pytest tests/api" \
  --reserves "migration=0042" [--blocked-by L0] [--queued] [--budget-usd 5]
lanectl launch --run <dir> --id L1 --brief-file <brief.md>
```

`--start` lists the files the lane opens first, so it doesn't explore. `--validate` narrows the lane's check to its own tests when the repo allows; the full gate still runs in CI.

Claude lanes load only file, search and shell tools, with no MCP servers or skills. Add them only when a lane needs them: `--extra-tools WebFetch,WebSearch` for research, `--mcp-config <file>` for a required server. Codex lanes use the user's Codex setup unchanged.

Write each brief from `references/protocol.md`. The lane contract is added automatically. Write only the task-specific part.

Tell the user the plan in a compact table before launching: lane, items, tool/model/effort, why, and what's queued behind what. In `review` mode, launch after the user agrees. If they already said to go ahead, launch right away.

Initial implementation launches require Goal, acceptance criteria, validation and current setup. Missing requirements block launch. `--no-validation-required` records an explicit user waiver. Setup must also be current on resume.

For normalized ticket JSON, use `brief --ticket-file <file>` (title, acceptance list, validation list, dependency item IDs), or configure a JSON argv `ticket_command` and use `--ticket-id`. Unknown dependencies fail. Map results propose start files, never ownership. Inspect/edit the draft, assign `--owns`, then `approve-brief`. Edits after approval require reapproval. Explicit validation commands remain in lane settings.

Reserve test databases/ports using lane_env templates and assigned LANE_* variables. `--token-budget N` is a soft reported-token guard, not a per-request spending cap. No automatic resets or retries.
