# Monitor, Review and Merge

## Watch the lanes

Run `lanectl wait --run <dir> --settle 60`: it returns after a lane changes state, plus any other changes within 60 seconds. Run it in the background if your environment notifies you; otherwise in the foreground with `--timeout`. Never poll lane output yourself.

Send one update per batch, one line per lane:

```
L2 (T-14) finished: ready, validation pass, $0.42. Reviewing.
L1 (T-12) failed: <one-line reason>. Suggest: <action>.
```

Collect open questions with `lanectl questions --run <dir>`. Answer what the ticket or repo rules settle; ask the rest as one numbered list, each with a suggested answer.

Run `lanectl scope --run <dir>` whenever a lane's turn ends. On drift or overlap: stop affected running lanes (`lanectl stop --reason`), tell the user which lane touched what and your suggestion (revert, re-split ownership, or re-plan), and ask.

**Failures, crashes, budget stops and loops always go to the user.** Report the cause and your suggestion, and wait. Never retry silently.

## Thin check (always)

When a lane reports `ready`:

1. The report has all fields, and `lanectl check --run <dir> --id <lane>` passes. Use its output as the evidence; never read full logs.
2. `lanectl scope` is clean.
3. Diff stats match the ticket's scope (`git -C <worktree> diff --stat <base>...HEAD`). Don't read the diff body.
4. Required smoke tests passed, if the repo requires them. Run them yourself only if the repo says the coordinator must.

If the thin check fails, send the lane an exact instruction based on the check output ("`pnpm test api` fails: <failing line>. Fix it, re-run, report."). Don't investigate the code.

## Reviewer lane (when warranted)

Add a reviewer when the repo requires review, or when the work is risky (see `references/models.md`). Before launching it, ask the user which tool and model to use, suggesting the other tool than the implementer. A "use X for all reviews this run" answer is a directive; log it and stop asking.

The reviewer returns either "No changes" or a CHANGE LIST. Forward the list unchanged to the implementing lane with `lanectl send`.

**One correction round.** If the work is still not right after the lane applies one change list (or one thin-check fix), stop and escalate to the user with the reviewer's findings and your suggestion.

## Push, PR and merge (coordinator only)

Lanes that pass review join the merge queue, which merges one at a time and respects reserved resources (a lane holding `migration=0043` waits for the one holding `0042`):

```
lanectl queue --run <dir> --add L2
lanectl queue --run <dir>          # ordered; "next" is the lane to merge now
```

For the lane marked `next`:

- Push the lane branch from its worktree. Use `--force-with-lease` only after a rebase.
- Open or update the PR per repo rules: draft or ready, title format, body with the lane report's Outcome, Validation and Remaining.
- **`review` mode:** tell the user the lane is ready to merge with a short summary, and wait for a go.
- **`merge-on-green` mode:** merge once required checks are green, per repo rules.
- If a merge, push or deletion is refused, stop and give the user the exact command. Never work around it.

## After each merge

1. `lanectl merged --run <dir> --id <lane> --pr <url> --commit <sha> --check`. It records the merge, rebases idle lanes that apply cleanly, checks them, and prints the queue. Send the rebase instruction (`references/protocol.md`) only to lanes it reports as conflicted. Push rebased lanes that have a PR with `--force-with-lease`.
2. Update the tracker per repo rules: state, closing comment with validation evidence, what wasn't exercised, and follow-ups.
3. Follow-up work found by lanes: create tickets only if repo rules say so. Otherwise list them for the user and ask.
4. When a lane that was running ends its turn, run `lanectl sync --run <dir> --check` before reviewing it.
5. Launch queued lanes whose blockers are now merged.
6. Clean up per repo rules with `lanectl cleanup`. If the repo has no rule, ask once and log the answer.
7. Generated tracker or index files in the repository conflict often. Regenerate them with the repo's tool after merge; never hand-merge.
