#!/usr/bin/env python3
"""Reproduce packet/delta byte measurements locally, with simulated findings and no model calls."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("lanectl.py")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="lane-packet-benchmark-") as temp:
        root = Path(temp); repo = root / "repo"; repo.mkdir()
        env = {**os.environ, "AGENT_LANES_HOME": str(root / "state")}

        def git(*parts, cwd=repo):
            return subprocess.run(["git", *parts], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

        def ctl(*parts):
            return subprocess.run([sys.executable, str(SCRIPT), *parts], cwd=repo, env=env,
                                  capture_output=True, text=True, check=True).stdout.strip()

        git("init", "-q", "-b", "main"); git("config", "user.name", "packet benchmark")
        git("config", "user.email", "benchmark@example.invalid")
        (repo / "empty-hooks").mkdir(); git("config", "core.hooksPath", str(repo / "empty-hooks"))
        (repo / "src").mkdir(); (repo / "src/app.py").write_text("x = 1\n")
        (repo / ".gitignore").write_text("__pycache__/\n")
        git("add", "."); git("commit", "-q", "-m", "base")
        run = Path(ctl("run", "new", "--repo", ".", "--name", "packet-size", "--base", "main"))
        ctl("run", "set", "--run", str(run), "--validate", f"{sys.executable} -m py_compile src/app.py")
        ctl("lane", "add", "--run", str(run), "--id", "L1", "--tool", "claude", "--model", "offline-fixture",
            "--effort", "medium", "--worktree", "auto", "--create-worktree", "--branch", "lane/fixture", "--owns", "src/**")
        tree = run / "worktrees/L1"
        (tree / "src/app.py").write_text("x = 2\n")
        (tree / "src/bulk.py").write_text("".join(
            f"# reference line {i:04d}: identical synthetic background material for the packet benchmark.\n" for i in range(2000)))
        git("add", "src", cwd=tree); git("commit", "-q", "-m", "synthetic change", cwd=tree)
        ctl("lane", "add", "--run", str(run), "--id", "R1", "--kind", "review", "--review-of", "L1",
            "--tool", "claude", "--model", "offline-fixture", "--effort", "high")
        criteria = root / "criteria.txt"; criteria.write_text("The change sets x to 2.\n")

        def prepare():
            ctl("review", "prepare", "--run", str(run), "--id", "R1", "--acceptance-file", str(criteria),
                "--ci-status", "not-required")
            return json.loads((run / "run.json").read_text())["lanes"]["R1"]["review"]

        first = prepare()
        report = root / "simulated-report.json"
        report.write_text(json.dumps({"head_sha": first["head_sha"], "status": "ready", "criteria": [
            {"id": "C1", "status": "pass", "evidence": [{"path": "src/app.py", "line": 1, "current": "x = 2"}]}],
            "findings": [], "notes": []}))
        ctl("review", "findings", "--run", str(run), "--id", "R1", "--report-file", str(report))
        (tree / "src/app.py").write_text("x = 3\n")
        git("add", "src/app.py", cwd=tree); git("commit", "-q", "-m", "one-line correction", cwd=tree)
        delta = prepare()
        metrics = {"benchmark": "synthetic local packet size, no model calls",
                   "original_diff_bytes": sum(item["bytes"] for item in first["diffs"]),
                   "initial_packet_bytes": len(Path(first["packet"]).read_bytes()),
                   "correction_diff_bytes": sum(item["bytes"] for item in delta["diffs"]),
                   "correction_packet_bytes": len(Path(delta["packet"]).read_bytes()),
                   "limitations": ["bytes are not tokens", "large patches remain available for reviewer inspection",
                                   "simulated findings are not an actual model review"]}
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(metrics, indent=2) + "\n")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
