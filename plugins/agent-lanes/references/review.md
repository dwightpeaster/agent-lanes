# Monitor, Review and Merge

## Monitor and gate

Use `lanectl wait --run <dir> --settle 60` with a bounded timeout, then give one line per changed lane. Collect questions with `lanectl questions`; answer from repo rules or tickets, and ask the rest as one numbered batch. Failures, crashes, budget stops, loops and scope drift require a user decision. Stop affected running lanes; never silently retry or upgrade models.

Before review, check the ready report, run `lanectl check`, require clean `lanectl scope`, compare diff stats with scope, and verify smoke tests and CI. `review prepare` reruns local checks and rejects dirty targets and scope problems. For green CI, supply its exact commit and evidence. `not-required` is only for repos whose rules allow it. Missing evidence is never green.

## Decide whether review is needed

`lanectl risk --run <dir> --id <lane>` recommends effort from sensitive paths, diff size, directory count and changed tests. Add repo-specific globs with `run set --risk-patterns`. It never overrides required review. Only small documentation-only changes are skip-eligible after checks and CI pass. Other changes need coordinator judgment. Risky work requires review; select high effort. Mechanical models cannot perform judgment reviews.

Ask which reviewer tool/model to use unless already authorized; suggest the other tool than the implementer. Reuse reviewers sequentially for small lanes, assessing each new target afresh.

## Prepare and launch

```
lanectl lane add --run <dir> --id R1 --kind review --review-of L1 --tool <tool> --model <id> --effort medium
lanectl review prepare --run <dir> --id R1 --acceptance-file <criteria> --ci-status green --ci-sha <head> --ci-evidence <run>
lanectl launch --run <dir> --id R1 --brief-file <brief>
```

Criteria files contain one criterion per nonempty line. The helper generates the prompt; the brief cannot redirect a reviewer. Reviewers use separate detached worktrees at the target commit. Claude gets Read, Glob and Grep, no shell/edit tools, no MCP, and disabled hooks. Codex gets a read-only sandbox without writable git directories; connectors, plugins, hooks and delegation are disabled separately. Safety settings override custom adapters.

Packets include exact commits, criteria, passing checks, scope, CI disposition and implementer decisions. Large diffs are referenced as per-file patches instead of pasted into the initial prompt. Decisions are evidence, not proof. Dedicated reviewer rules require criterion evidence and separate blockers from suggestions.

## Validate and correct

```
lanectl review findings --run <dir> --id R1
lanectl send --run <dir> --id L1 --review-from R1
```

Findings are checked against pinned source text and line numbers. This rejects stale or invented anchors, not semantic mistakes. Only validated blocking replacements are forwarded; follow-ups remain separate. Unknown criteria block readiness. One correction round is allowed; another needs the user's explicit `--correction-override` directive.

After fixes, run `review prepare` again, then `send` to R1. It supplies the delta from the last validated review plus prior findings. Changed criteria or gates force a full review. Use `--target L2` to reuse R1 for another lane. Rewritten history needs a fresh reviewer. Stale approval cannot authorize a changed commit.

## Merge and follow up

Add green lanes to `lanectl queue`; risky or mandatory reviews need validated approval for the exact commit. Push and open/update PRs per repo rules. Only the coordinator pushes and merges. In `review` mode, wait for merge authorization; in `merge-on-green`, obey repo gates and merge rules. A refused push, merge or deletion stops the run; give the exact command.

After merge, call `lanectl merged --pr <url> --commit <sha> --check`, update the tracker per repo rules, and send rebase instructions only for conflicts. Sync formerly running lanes before review. Launch unblocked lanes and clean up per saved policy. Regenerate generated files with the repo tool. Create follow-up tickets only when authorized; otherwise keep suggestions for the user.
