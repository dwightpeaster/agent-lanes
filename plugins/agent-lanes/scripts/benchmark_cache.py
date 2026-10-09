#!/usr/bin/env python3
"""Opt-in CLI cache experiment. Uses live model calls on disposable, distinct worktrees."""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import lane_cache
import lanectl


def execute(command, cwd, folder):
    began = time.monotonic()
    proc = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=180)
    stream = folder / "stream.jsonl"
    stream.write_text(proc.stdout)
    parsed = lanectl.parse_stream(stream, "claude" if command[0] == "claude" else "codex")
    if proc.returncode or parsed["error"] or not parsed["done"]:
        raise RuntimeError("CLI benchmark failed; no retry performed: " + (parsed["error"] or proc.stderr[-800:]))
    return {"fresh": parsed["fresh"], "cache_write": parsed["cache_write"], "cache_read": parsed["cache_read"],
            "output": parsed["tokens_out"], "cost_usd": parsed["cost_usd"], "first_call": parsed["first_call"],
            "seconds": round(time.monotonic() - began, 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", choices=("claude", "codex"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", choices=("low", "medium", "high"), default="low")
    parser.add_argument("--output", required=True)
    parser.add_argument("--parallel", action="store_true", help="compare simultaneous cold starts rather than sequential starts")
    parser.add_argument("--developer-prefix-file", help="required for Codex; preserve effective developer instructions")
    parser.add_argument("--budget-usd", type=float, default=0.10, help="Claude per-call budget; default $0.10")
    args = parser.parse_args()
    if args.tool == "codex" and not args.developer_prefix_file:
        parser.error("Codex experiment requires --developer-prefix-file (empty only if there are no existing instructions)")
    shared = (lanectl.PLUGIN_ROOT / "assets/lane-contract.md").read_text().strip()
    review = (lanectl.PLUGIN_ROOT / "assets/review-contract.md").read_text().strip()
    prefix = Path(args.developer_prefix_file).read_text() if args.developer_prefix_file else ""
    scenarios = ("claude-current", "claude-stable", "claude-review-tools") if args.tool == "claude" else ("codex-prompt", "codex-developer")
    rows = []
    with tempfile.TemporaryDirectory(prefix="agent-lanes-cache-") as temp:
        root = Path(temp); repo = root / "repo"; repo.mkdir()
        for command in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.name", "cache benchmark"],
                        ["git", "config", "user.email", "benchmark@example.invalid"],
                        ["git", "commit", "-q", "--allow-empty", "-m", "benchmark base"]):
            subprocess.run(command, cwd=repo, check=True, capture_output=True)
        version, _ = lane_cache.cli_info(args.tool)
        for scenario in scenarios:
            jobs = []
            for index in range(2):
                folder = root / f"{scenario}-{index}"; folder.mkdir()
                checkout = folder / "checkout"
                subprocess.run(["git", "worktree", "add", "--detach", str(checkout), "HEAD"], cwd=repo,
                               check=True, capture_output=True)
                rules = review if scenario == "claude-review-tools" else shared
                prompt = f"Mechanical CLI test, lane {index}, checkout {checkout}. Reply only OK. Do not call any tools."
                if args.tool == "claude":
                    tools = "Read,Glob,Grep" if scenario == "claude-review-tools" else "Read,Edit,Write,Glob,Grep,Bash"
                    command = ["claude", "-p", prompt, "--model", args.model, "--effort", args.effort,
                               "--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk",
                               "--append-system-prompt", rules, "--tools", tools, "--strict-mcp-config",
                               "--mcp-config", lanectl.NO_MCP, "--disable-slash-commands",
                               "--settings", '{"disableAllHooks":true}', "--max-budget-usd", str(args.budget_usd)]
                    if scenario != "claude-current":
                        flags = lane_cache.claude_cache_flags()
                        if "--exclude-dynamic-system-prompt-sections" not in flags:
                            raise RuntimeError("installed Claude CLI lacks the cache experiment flag")
                        command += flags
                else:
                    command = ["codex", "exec", "--json", "-o", str(folder / "last.md"), "-m", args.model,
                               "-c", "model_reasoning_effort=" + args.effort, "--sandbox", "read-only",
                               "-c", 'approval_policy="never"', "-C", str(checkout)]
                    command += lane_cache.reviewer_codex_flags(str(checkout))
                    if scenario == "codex-developer":
                        command += ["-c", "developer_instructions=" + json.dumps(prefix + "\n\n" + rules)]
                    else:
                        prompt = rules + "\n\n" + prompt
                    command += [prompt]
                jobs.append((command, checkout, folder))
            if args.parallel:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    metrics = list(pool.map(lambda job: execute(*job), jobs))
            else:
                metrics = [execute(*job) for job in jobs]
            rows += [{"scenario": scenario, "lane": i + 1, **value} for i, value in enumerate(metrics)]
            print(scenario + ": " + json.dumps(metrics), flush=True)
    output = {"tool": args.tool, "model": args.model, "effort": args.effort, "cli": version,
              "launch": "simultaneous" if args.parallel else "sequential", "worktrees": "distinct detached checkouts",
              "rows": rows, "limitations": ["small mechanical prompts, not production reviews",
              "cache temperature/routing are provider-controlled; this experiment cannot flush caches",
              "Codex CLI reports turn totals, not first-call cache writes or exact dollar cost"]}
    Path(args.output).write_text(json.dumps(output, indent=2) + "\n")


if __name__ == "__main__":
    main()
