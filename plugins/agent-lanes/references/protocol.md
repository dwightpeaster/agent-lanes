# Messages Between Coordinator and Lanes

Every message to a lane is explicit, self-contained and short. Lanes keep their own context across turns, so never repeat what a lane already has.

- First turn: `lanectl launch --brief-file`. The lane contract (rules, ownership, report format) is added automatically.
- Later turns: `lanectl send --message-file`. This resumes the same session.
- Lanes answer only with the `LANE REPORT` defined in the contract. Read it with `lanectl report`.

## Brief: implement or research lane

```
Goal: <what must be true when done>
Acceptance:
- <observable criterion>
Ticket: <ID>. Read it with <command or tool> for full detail.
Context: <files or helpers to start from, prior decisions, links. No pasted code>
Constraints: <region splits in shared files, reserved resources, append-only contracts>
Effort reason: <only when effort is high: why>
Out of scope: <what not to do>
```

## Brief: exact-spec lane

Use only when every edit is already written down.

```
Apply exactly these edits and nothing else.

1. <path> near line <n> (anchor: `<unique nearby text>`)
   CURRENT:
   <exact text>
   REPLACE WITH:
   <exact text>

2. <path> NEW FILE
   CONTENT:
   <exact text>

Then run: `<command>` -> expect <result>
If any CURRENT text is not found exactly, or a result differs: stop and report with Status: blocked. Do not improvise.
```

## Brief: reviewer lane

Reviewer lanes are read-only, so give them no ownership.

```
Review PR <url> (branch <branch>) against ticket <ID>.
Check, in order: correctness, security/permission/data boundaries, regressions, acceptance criteria, validation quality, reuse of existing patterns.
Output: if everything is correct, Status: ready and "No changes".
Otherwise Status: blocked, then a CHANGE LIST in the exact format below. Every item must be
specific enough to apply without reading anything else. No style-only items unless the repo rules require them.
```

## Change list (reviewer to coordinator to implementer)

Forward it to the implementing lane unchanged. Don't rewrite it and don't re-read the code.

```
CHANGE LIST for lane <id>
1. <path>:<line>
   CURRENT: <exact text>
   CHANGE TO: <exact text>
   WHY: <one line>
2. <path> after line <n>
   INSERT: <exact text>
   WHY: <one line>
3. <path>:<line>
   DELETE: <exact text>
   WHY: <one line>
Then re-run: <commands>. Report.
```

## Rebase instruction

```
REBASE: <base> moved. Merged since your branch point:
- <PR or commit>: touched <files>
Rebase onto <base> (the coordinator has already fetched it). Keep both sides of any conflict, re-run validation, report.
```

Use the same form when the user merges work out of order (for example, PR 10 before PR 9): tell the PR 9 lane exactly which merged change it must rebase over.

## Answering a lane's question

```
ANSWER: <the decision, in one or two sentences>
Source: <user directive | ticket | repo rule>
Continue from where you stopped.
```

Answer from the ticket or repo rules when they settle it. Otherwise ask the user, log the answer as a directive, then send it.
