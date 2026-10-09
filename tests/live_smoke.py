"""Opt-in live smoke test: a real Claude lane and a real Claude reviewer in a scratch repository.

The unit tests use fake CLIs, so they can't catch permission or path problems in a real launch.
Run this before a release. It makes real model calls (a few cents on Haiku):

    python3 tests/live_smoke.py --yes-spend [--model claude-haiku-5-5]
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LANECTL = Path(__file__).resolve().parents[1] / "plugins" / "agent-lanes" / "scripts" / "lanectl.py"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes-spend", action="store_true", help="confirm real model calls")
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    args = parser.parse_args()
    if not args.yes_spend:
        print("This makes real model calls. Re-run with --yes-spend.")
        return 2
    scratch = Path(tempfile.mkdtemp(prefix="agent-lanes-smoke-"))
    repo, env = scratch / "repo", {**os.environ, "AGENT_LANES_HOME": str(scratch / "home")}
    repo.mkdir()

    def sh(*cmd, cwd=repo):
        return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()

    def ctl(*cmd, check=True):
        proc = subprocess.run([sys.executable, str(LANECTL), *cmd], cwd=repo, env=env, text=True, capture_output=True)
        if check and proc.returncode:
            raise SystemExit(f"lanectl {' '.join(cmd)} failed:\n{proc.stdout}\n{proc.stderr}")
        return proc

    def write(name, text):
        path = scratch / name
        path.write_text(text)
        return str(path)

    def denials(run, lane_id):
        stream = sorted((Path(run) / "lanes" / lane_id).glob("attempt-*.jsonl"))[-1]
        events = [json.loads(line) for line in stream.read_text().splitlines() if line.startswith("{")]
        return [d for e in events if e.get("type") == "result" for d in e.get("permission_denials") or []]

    sh("git", "init", "-q", "-b", "main")
    sh("git", "config", "user.email", "smoke@example.com")
    sh("git", "config", "user.name", "smoke")
    (repo / "calc.py").write_text("def sub(a, b):\n    return a - b\n")
    (repo / "test_calc.py").write_text("import unittest\nfrom calc import sub\n\n\nclass T(unittest.TestCase):\n"
                                       "    def test_sub(self):\n        self.assertEqual(sub(3, 1), 2)\n")
    sh("git", "add", ".")
    sh("git", "commit", "-qm", "init")

    run = ctl("run", "new", "--repo", ".", "--name", "smoke", "--base", "main").stdout.splitlines()[0]
    ctl("run", "set", "--run", run, "--validate", "python3 -m unittest -q")
    ctl("lane", "add", "--run", run, "--id", "L1", "--items", "T-1", "--tool", "claude", "--model", args.model,
        "--effort", "low", "--worktree", "auto", "--create-worktree", "--branch", "lane/L1",
        "--owns", "calc.py,test_calc.py", "--start", "calc.py,test_calc.py")
    brief = write("brief.md", "Goal: add add(a, b) to calc.py returning a + b, with a unit test.\n"
                              "Acceptance:\n- add(2, 3) == 5 is tested in test_calc.py\n"
                              "Commit, run your check, report.\n")
    began = time.monotonic()
    ctl("launch", "--run", run, "--id", "L1", "--brief-file", brief)
    ctl("wait", "--run", run, "--timeout", "300", "--interval", "2")
    report = ctl("report", "--run", run, "--id", "L1").stdout
    problems = []
    if "Status: ready" not in report:
        problems.append("implementation lane did not report ready")
    if denials(run, "L1"):
        problems.append(f"implementation lane permission denials: {denials(run, 'L1')}")
    print(f"implementation lane: {time.monotonic() - began:.0f}s, report ready={'Status: ready' in report}")

    ctl("lane", "add", "--run", run, "--id", "R1", "--kind", "review", "--review-of", "L1", "--tool", "claude",
        "--model", args.model, "--effort", "medium")
    criteria = write("criteria.md", "add(2, 3) == 5 is tested in test_calc.py\n")
    packet = ctl("review", "prepare", "--run", run, "--id", "R1", "--acceptance-file", criteria,
                 "--ci-status", "not-required").stdout.strip()
    began = time.monotonic()
    ctl("launch", "--run", run, "--id", "R1", "--brief-file", brief)
    ctl("wait", "--run", run, "--timeout", "300", "--interval", "2")
    review_denials = denials(run, "R1")
    if review_denials:
        problems.append(f"reviewer permission denials (packet unreadable?): {review_denials}")
    findings = ctl("review", "findings", "--run", run, "--id", "R1", check=False)
    print(f"reviewer: {time.monotonic() - began:.0f}s, packet={packet}")
    print("review findings:", (findings.stdout or findings.stderr).strip())
    if findings.returncode and "different commit" in findings.stderr:
        problems.append("reviewer report is not bound to the prepared commit")
    print(ctl("usage", "--run", run).stdout)
    if not args.keep:
        subprocess.run(["rm", "-rf", str(scratch)])
    else:
        print(f"kept: {scratch}")
    for problem in problems:
        print("FAIL:", problem)
    print("live smoke: " + ("FAILED" if problems else "PASS"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
