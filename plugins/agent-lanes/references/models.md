# Models and Effort

Roster verified 2026-10-09. Models change often. At the start of a run, confirm that the IDs below are accepted (the CLI errors fast on an unknown model). If a newer model has replaced one, use it in the same role and tell the user. The user's instructions override this file.

## Roster

Prices are per million input/output tokens.

| Tool | Model ID | Price | Use for |
|---|---|---|---|
| Claude | `claude-fable-5-1` | $10 / $50 | Only on the user's instruction |
| Claude | `claude-opus-5-5` | $4 / $20 | Complex, multi-file or risky implementation; hard reviews |
| Claude | `claude-sonnet-5-5` | $2 / $10 | Standard implementation, exact-spec edits, research |
| Claude | `claude-haiku-5-5` | from $0.10 / $0.50 | Mechanical, checkable work only (renames, formatting, extraction) |
| Codex | `gpt-6-astra` | $10 / $50 | Only on the user's instruction |
| Codex | `gpt-6.1-sol` | $2 / $10 | Codex default for implementation, review and research |
| Codex | `gpt-6-luna` | $0.10 / $0.50 | Mechanical, checkable work only |

Don't use Haiku or Luna for judgment work, code review, or anything whose output someone acts on without checking. Luna has a high reported hallucination rate on knowledge work.

## Effort policy

Always pass effort explicitly. Some models and configs default to `high`.

| Lane | Effort |
|---|---|
| Exact-spec or mechanical | `low`; `medium` if low proves unreliable for that change |
| Standard implementation or research | `medium` |
| Risky: migrations, concurrency, transactions, security or access paths, data integrity, cross-module refactors | `high`; state the reason in the brief and the ledger |
| Review of risky work | `high` |
| Above `high` (`xhigh`, `max` and up) | Only when the user explicitly says so, for that lane. Use `lanectl lane add --override "<user's words>"` |

## Choosing a lane's tool and model

1. Use the cheapest model and lowest effort likely to get it right the first time. A failed lane plus a retry costs more than one correct run.
2. Either tool can implement or review. Pick by fit and cost, not habit. Sol at medium is usually the best value for standard Codex work; Opus at medium for complex Claude work.
3. A reviewer should come from the other tool than the implementer when both are installed. It catches different mistakes. Always ask the user which reviewer to use; suggest the other tool.
4. If the user names a tool or model for a lane, use it and log the directive.
5. When a lane fails because the model was too weak, don't silently upgrade. Tell the user and suggest the upgrade.
