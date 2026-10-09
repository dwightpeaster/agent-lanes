# Planning Lanes

## Read the work, not the code

For each candidate item, read the ticket with its relations (blocks, blocked-by, execution order). Skim titles and acceptance criteria. Don't open source files. A lane will do that.

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
  --reserves "migration=0042" [--blocked-by L0] [--queued] [--budget-usd 5]
lanectl launch --run <dir> --id L1 --brief-file <brief.md>
```

Write each brief from `references/protocol.md`. The lane contract is added automatically. Write only the task-specific part.

Tell the user the plan in a compact table before launching: lane, items, tool/model/effort, why, and what's queued behind what. In `review` mode, launch after the user agrees. If they already said to go ahead, launch right away.
