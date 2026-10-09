# Monitor, Review and Merge

## Watch the lanes

Run `lanectl wait --run <dir>`. It returns as soon as any running lane changes state. If your environment can run it in the background and notify you, do that; otherwise run it in the foreground with `--timeout`. Never poll by reading lane output yourself.

On every change, send the user a one-line update:

```
L2 (T-14) finished: ready, validation pass, $0.42. Reviewing.
L3 (T-15) has a question: <question>. Suggest: <answer>. OK?
L1 (T-12) failed: <one-line reason>. Suggest: <action>. How do you want to proceed?
```

Run `lanectl scope --run <dir>` whenever a lane's turn ends. On scope drift or overlap: stop any affected running lane (`lanectl stop --reason`), then tell the user which lane touched what, what you suggest (revert those files, re-split ownership, or accept and re-plan), and ask.

**Failures, crashes, budget stops and loops always go to the user.** Report the cause and your suggestion, and wait. Never retry silently.

## Thin check (always)

When a lane reports `ready`:

1. The report has all fields, and validation lists every required command as passing.
2. `lanectl scope` is clean.
3. Diff stats match the ticket's scope (`git -C <worktree> diff --stat <base>...HEAD`). Don't read the diff body.
4. Required smoke tests passed, if the repo requires them. Run them yourself only if the repo says the coordinator must.

If the thin check fails, send the lane an exact instruction based on the evidence ("`pnpm test api` fails: <failing line>. Fix it, re-run, report."). Don't investigate the code.

## Reviewer lane (when warranted)

Add a reviewer when the repo requires review, or when the work is risky (see `references/models.md`). Before launching it, ask the user which tool and model to use, suggesting the other tool than the implementer. A "use X for all reviews this run" answer is a directive; log it and stop asking.

The reviewer returns either "No changes" or a CHANGE LIST. Forward the list unchanged to the implementing lane with `lanectl send`.

**One correction round.** If the work is still not right after the lane applies one change list (or one thin-check fix), stop and escalate to the user with the reviewer's findings and your suggestion.

## Push, PR and merge (coordinator only)

- Push the lane branch from its worktree. Use `--force-with-lease` only after a rebase.
- Open or update the PR per repo rules: draft or ready, title format, body with the lane report's Outcome, Validation and Remaining.
- **`review` mode:** tell the user the lane is ready to merge with a short summary, and wait for a go.
- **`merge-on-green` mode:** merge once required checks are green, per repo rules.
- If a merge, push or deletion is refused, stop and give the user the exact command. Never work around it.

## After each merge

1. `lanectl lane set --state merged --pr <url>` and `lanectl note --kind merge`.
2. Update the tracker per repo rules: state, closing comment with validation evidence, what wasn't exercised, and follow-ups.
3. Follow-up work found by lanes: create tickets only if repo rules say so. Otherwise list them for the user and ask.
4. Tell running lanes that the base moved, using the rebase instruction in `references/protocol.md` (send after their current turn ends).
5. Launch queued lanes whose blockers are now merged.
6. Clean up per repo rules with `lanectl cleanup`. If the repo has no rule, ask once and log the answer.
7. If the repo keeps generated tracker or index files in the repository, expect conflicts in them. Regenerate them with the repo's own tool after merge rather than hand-merging.
