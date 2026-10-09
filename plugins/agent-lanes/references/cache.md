# Cache Profiles and Measurement

Shared contracts are frozen in each run. Adapter overrides are frozen on first command construction. New Claude lanes and reviewers use the installed CLI's dynamic-context exclusion and system-prompt snapshot flags when supported. Old CLIs fall back without those flags. Existing sessions keep their recorded context; a frozen contract cannot change their earlier prefix retroactively.

Tool order is deterministic. Do not force unrelated lanes onto one model to chase cache hits. Compare lanes with matching model, effort, CLI version, contract and tool/MCP configuration. Each attempt records a configuration fingerprint, not a hash of the provider's hidden rendered prompt. Permission rules and worktree context can still differ. Changing an MCP configuration under an existing session is refused; use a fresh lane.

For compatible ready lanes, optionally use:

```
lanectl launch-group --run <dir> --briefs-file <json> --warm-cache --warm-timeout 30
```

The JSON maps lane IDs to brief paths. Claude followers start after a successful leader response is observed, not its init event. Codex's CLI exposes turn completion rather than a first-response cache signal, so its warmup waits for that. Groups are candidate sharing opportunities, not guarantees. Timeout or leader failure leaves followers unstarted for a coordinator decision. This can trade latency for fewer initial writes; measure before making it the default.

`lanectl usage --json` includes per-attempt profiles and Claude first-call samples, deduplicated by message ID. Compare initial calls of distinct lanes separately from resumed calls and later tool steps. Missing Codex cache-write and dollar-cost values remain unknown. Cache reads alone do not prove cross-lane reuse or total savings.

Codex keeps prompt placement by default. The experimental run options are:

```
lanectl run new ... --codex-contract developer --codex-developer-prefix-file <file>
```

Supply the effective existing developer instructions in that file; use an empty file only if none exist. The contract is appended and passed through per-launch `developer_instructions`. This is an opt-in boundary experiment, not a proven cross-session cache fix. It changes no global Codex configuration and does not replace built-in model instructions.

The opt-in `scripts/benchmark_cache.py` uses real CLI/model calls in distinct disposable worktrees. It requires explicit model selection and consumes provider usage. Claude compares current flags, dynamic-context exclusion and the smaller review profile; each comparison uses two lanes. Codex compares prompt placement with developer instructions. Add `--parallel` to test simultaneous starts. Cache temperature and routing remain provider-controlled. This mechanical benchmark does not establish production review savings.

Do not run live benchmarks without authorization for the external calls and usage. Example after authorization:

```
python3 <package-root>/scripts/benchmark_cache.py --tool claude --model claude-haiku-5-5 --effort low --output <results.json>
```

Claude's default per-call budget is $0.10. First-call token counts, output, reported cost and time are saved without raw prompts or transcripts. API-level breakpoint controls and cache-affinity changes are outside the CLI adapter's guarantees.

For a local byte-only experiment with no model calls, use `scripts/benchmark_packets.py --output <file>`. It creates synthetic changes and a simulated validated report to measure initial packets and correction deltas. Patch references defer inspection; they do not eliminate necessary review work.
