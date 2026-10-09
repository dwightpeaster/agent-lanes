import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "agent-lanes"
LANECTL = PLUGIN / "scripts" / "lanectl.py"

FAKE_CLAUDE = textwrap.dedent("""\
    #!{python}
    import json, os, sys, time, subprocess
    log = os.environ["FAKE_LOG"]
    with open(log, "a") as f:
        f.write(json.dumps({{"tool": "claude", "argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
    if os.environ.get("FAKE_SLEEP"):
        time.sleep(float(os.environ["FAKE_SLEEP"]))
    target = os.environ.get("FAKE_TOUCH")
    if target:
        open(target, "a").write("change\\n")
    print(json.dumps({{"type": "system", "subtype": "init", "session_id": "sess-claude-1"}}))
    print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Edit"}}]}}}}))
    report = "LANE REPORT L1\\nStatus: ready\\nOutcome: done\\nValidation: unit: pass"
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": report,
                       "session_id": "sess-claude-1", "total_cost_usd": 0.42,
                       "usage": {{"input_tokens": 1000, "cache_read_input_tokens": 2000, "output_tokens": 500}}}}))
    """)

FAKE_CODEX = textwrap.dedent("""\
    #!{python}
    import json, os, sys
    log = os.environ["FAKE_LOG"]
    args = sys.argv[1:]
    with open(log, "a") as f:
        f.write(json.dumps({{"tool": "codex", "argv": args, "cwd": os.getcwd()}}) + "\\n")
    out = args[args.index("-o") + 1]
    report = "LANE REPORT L2\\nStatus: question\\nQuestion/Blocker: which table?"
    open(out, "w").write(report)
    print(json.dumps({{"type": "thread.started", "thread_id": "thread-codex-9"}}))
    print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": report}}}}))
    print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 3000, "cached_input_tokens": 0, "output_tokens": 700}}}}))
    """)


class LaneCtlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.repo = base / "repo"
        self.home = base / "home"
        self.bin = base / "bin"
        self.log = base / "calls.jsonl"
        self.bin.mkdir()
        for name, body in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
            path = self.bin / name
            path.write_text(body.format(python=sys.executable))
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.env = {**os.environ, "AGENT_LANES_HOME": str(self.home), "FAKE_LOG": str(self.log),
                    "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        (self.repo / "src").mkdir()
        (self.repo / "src/app.py").write_text("x = 1\n")
        (self.repo / "docs.md").write_text("docs\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "init")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, check=True, text=True, capture_output=True).stdout

    def ctl(self, *args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
        proc = subprocess.run([sys.executable, str(LANECTL), *args], cwd=self.repo, text=True,
                              capture_output=True, env=env or self.env)
        if check and proc.returncode != 0:
            self.fail(f"lanectl {' '.join(args)} failed: {proc.stderr}")
        return proc

    def new_run(self) -> str:
        return self.ctl("run", "new", "--repo", ".", "--name", "Wave one", "--base", "main").stdout.strip()

    def add_lane(self, run: str, lane_id: str, tool: str = "claude", **extra) -> None:
        args = ["lane", "add", "--run", run, "--id", lane_id, "--items", f"T-{lane_id}", "--tool", tool,
                "--model", "m1", "--effort", extra.pop("effort", "medium"), "--worktree", "auto",
                "--create-worktree", "--branch", f"lane/{lane_id}"]
        for key, value in extra.items():
            flag = f"--{key.replace('_', '-')}"
            args += [flag] if value is True else [flag, value]
        self.ctl(*args)

    def brief(self, text: str = "Goal: do it") -> str:
        path = Path(self.temp.name) / f"brief-{time.monotonic_ns()}.md"
        path.write_text(text)
        return str(path)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def wait_for(self, run: str) -> str:
        return self.ctl("wait", "--run", run, "--timeout", "20", "--interval", "0.2").stdout

    # ------------------------------------------------------------ tests

    def test_run_state_lives_outside_repository(self) -> None:
        run = self.new_run()
        self.assertTrue(run.startswith(str(self.home)))
        self.assertTrue((Path(run) / "ledger.md").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")
        listed = self.ctl("run", "list", "--repo", ".").stdout
        self.assertIn("Wave one", listed)
        self.ctl("run", "set", "--run", run, "--state", "closed")
        self.assertNotIn("Wave one", self.ctl("run", "list", "--repo", ".").stdout)

    def test_effort_ceiling_requires_recorded_user_override(self) -> None:
        run = self.new_run()
        refused = self.ctl("lane", "add", "--run", run, "--id", "L1", "--tool", "claude", "--model", "m",
                           "--effort", "xhigh", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("exceeds ceiling high", refused.stderr)
        self.ctl("lane", "add", "--run", run, "--id", "L1", "--tool", "claude", "--model", "m",
                 "--effort", "xhigh", "--override", "Dwight: use xhigh for the migration lane")
        ledger = (Path(run) / "ledger.md").read_text()
        self.assertIn("**directive**", ledger)
        self.assertIn("use xhigh for the migration lane", ledger)
        self.assertNotEqual(self.ctl("lane", "add", "--run", run, "--id", "L2", "--tool", "claude",
                                     "--model", "m", "--effort", "turbo", check=False).returncode, 0)

    def test_claude_lane_launch_report_cost_and_resume(self) -> None:
        run = self.new_run()
        self.add_lane(run, "L1", owns="src/**")
        worktree = Path(run) / "worktrees" / "L1"
        self.assertTrue((worktree / "src/app.py").exists())
        self.ctl("launch", "--run", run, "--id", "L1", "--brief-file", self.brief("Goal: add feature X"))
        changed = self.wait_for(run)
        self.assertIn("turn-ended", changed)
        status = self.ctl("status", "--run", run).stdout
        self.assertIn("report=ready", status)
        self.assertIn("$0.42", status)
        self.assertIn("in 3k/out 500", status)
        self.assertIn("Status: ready", self.ctl("report", "--run", run, "--id", "L1").stdout)

        first = self.calls()[0]
        self.assertEqual(Path(first["cwd"]).resolve(), worktree.resolve())
        argv = first["argv"]
        self.assertEqual(argv[0], "-p")
        prompt = argv[1]
        self.assertIn("Lane Contract", prompt)
        self.assertIn("Goal: add feature X", prompt)
        self.assertIn("src/**", prompt)
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        self.assertIn("Bash(git push *)", argv[argv.index("--disallowedTools") + 1])

        self.ctl("send", "--run", run, "--id", "L1", "--message-file", self.brief("REBASE: main moved"))
        self.wait_for(run)
        second = self.calls()[1]["argv"]
        self.assertEqual(second[1], "REBASE: main moved")
        self.assertEqual(second[second.index("--resume") + 1], "sess-claude-1")
        packet = self.ctl("packet", "--run", run).stdout
        self.assertIn("session=sess-claude-1", packet)

    def test_codex_lane_uses_sandbox_effort_and_last_message(self) -> None:
        run = self.new_run()
        self.add_lane(run, "L2", tool="codex", effort="low")
        self.ctl("launch", "--run", run, "--id", "L2", "--brief-file", self.brief())
        self.wait_for(run)
        status = self.ctl("status", "--run", run).stdout
        self.assertIn("report=question", status)
        self.assertIn("in 3k/out 700", status)
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[:2], ["exec", "--json"])
        self.assertIn("model_reasoning_effort=low", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertIn("which table?", self.ctl("report", "--run", run, "--id", "L2").stdout)
        dry = json.loads(self.ctl("send", "--run", run, "--id", "L2", "--message-file", self.brief("ANSWER: users"),
                                  "--dry-run").stdout)
        self.assertEqual(dry[dry.index("resume") + 1], "thread-codex-9")

    def test_scope_detects_out_of_ownership_and_overlap(self) -> None:
        run = self.new_run()
        self.add_lane(run, "L1", owns="src/**")
        self.add_lane(run, "L2", owns="docs.md")
        self.assertEqual(self.ctl("scope", "--run", run).stdout.strip(), "scope ok")
        (Path(run) / "worktrees/L1/docs.md").write_text("edited by L1\n")
        (Path(run) / "worktrees/L2/docs.md").write_text("edited by L2\n")
        result = self.ctl("scope", "--run", run, check=False)
        self.assertEqual(result.returncode, 3)
        self.assertIn("L1 outside ownership: docs.md", result.stdout)
        self.assertIn("overlap docs.md: L1, L2", result.stdout)

    def test_blocked_lane_waits_for_merge_and_stop_interrupts(self) -> None:
        run = self.new_run()
        self.add_lane(run, "L1")
        self.add_lane(run, "L3", blocked_by="L1", queued=True)
        refused = self.ctl("launch", "--run", run, "--id", "L3", "--brief-file", self.brief(), check=False)
        self.assertIn("blocked by L1", refused.stderr)
        slow = {**self.env, "FAKE_SLEEP": "30"}
        self.ctl("launch", "--run", run, "--id", "L1", "--brief-file", self.brief(), env=slow)
        time.sleep(0.5)
        again = self.ctl("launch", "--run", run, "--id", "L1", "--brief-file", self.brief(), check=False)
        self.assertIn("is running", again.stderr)
        self.ctl("stop", "--run", run, "--id", "L1", "--reason", "scope drift")
        self.assertIn("stopped", self.ctl("status", "--run", run, "--id", "L1").stdout)
        self.ctl("lane", "set", "--run", run, "--id", "L1", "--state", "merged", "--pr", "https://example/pr/1")
        self.ctl("launch", "--run", run, "--id", "L3", "--brief-file", self.brief())
        self.wait_for(run)

    def test_adapter_override_and_cleanup(self) -> None:
        run = self.new_run()
        self.add_lane(run, "L1")
        self.home.mkdir(exist_ok=True)
        (self.home / "adapters.json").write_text(json.dumps({"claude": {"new": ["claude", "-p", "{prompt}", "--x", "{effort}"]}}))
        dry = json.loads(self.ctl("launch", "--run", run, "--id", "L1", "--brief-file", self.brief(), "--dry-run").stdout)
        self.assertEqual(dry, ["claude", "-p", "<prompt>", "--x", "medium"])
        out = self.ctl("cleanup", "--run", run, "--id", "L1", "--remove-worktree", "--delete-branch").stdout
        self.assertIn("worktree removed", out)
        self.assertNotIn("lane/L1", self.git("branch"))


class SkillSurfaceTest(unittest.TestCase):
    def test_manifests_versions_and_explicit_invocation(self) -> None:
        version = "0.1.0"
        codex = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text())
        claude = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        self.assertEqual({codex["version"], claude["version"], market["plugins"][0]["version"]}, {version})
        self.assertIn(f'VERSION = "{version}"', LANECTL.read_text())
        self.assertIn("$agent-lanes", codex["interface"]["defaultPrompt"])
        for meta in (PLUGIN / "agents/openai.yaml", PLUGIN / "skills/agent-lanes/agents/openai.yaml"):
            text = meta.read_text()
            self.assertIn("allow_implicit_invocation: false", text)
            short = re.search(r'^  short_description: "(.+)"$', text, re.MULTILINE)
            self.assertTrue(25 <= len(short.group(1)) <= 64)

    def test_context_budgets_and_references_exist(self) -> None:
        skill = (PLUGIN / "skills/agent-lanes/SKILL.md").read_text()
        description = next(l.split(":", 1)[1].strip() for l in skill.splitlines() if l.startswith("description:"))
        self.assertLessEqual(len(description), 400)
        self.assertLessEqual(len(re.findall(r"\b[\w$-]+\b", skill)), 450)
        for ref in set(re.findall(r"references/[\w-]+\.md", skill)):
            path = PLUGIN / ref
            self.assertTrue(path.exists(), ref)
            self.assertLessEqual(len(path.read_text().split()), 700, ref)
        self.assertLessEqual(len((PLUGIN / "assets/lane-contract.md").read_text().split()), 450)

    def test_activation_cases(self) -> None:
        cases = json.loads((ROOT / "tests/skill_activation_cases.json").read_text())
        self.assertTrue(any(c["expected_skill"] == "agent-lanes" for c in cases))
        self.assertTrue(any(c["expected_skill"] is None for c in cases))
        for case in cases:
            self.assertTrue(case["prompt"].strip())
            self.assertIn(case["expected_skill"], {"agent-lanes", None})

    def test_independent_of_other_plugins(self) -> None:
        for path in PLUGIN.rglob("*"):
            if path.is_file() and path.suffix in {".md", ".py", ".json", ".yaml"}:
                text = path.read_text().lower()
                self.assertNotIn("ticketing os", text, path)
                self.assertNotIn("agent-ticketing", text, path)
                self.assertNotIn("ticketctl", text, path)


if __name__ == "__main__":
    unittest.main()
