import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import test_lanectl as fixtures

LANECTL, PLUGIN = fixtures.LANECTL, fixtures.PLUGIN

sys.path.insert(0, str(LANECTL.parent))
import lane_cache

spec = importlib.util.spec_from_file_location("lane_test_ctl", LANECTL)
ctl_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ctl_module
spec.loader.exec_module(ctl_module)


class ReviewCacheTest(unittest.TestCase):
    setUp = fixtures.LaneCtlTest.setUp
    tearDown = fixtures.LaneCtlTest.tearDown
    git = fixtures.LaneCtlTest.git
    ctl = fixtures.LaneCtlTest.ctl
    new_run = fixtures.LaneCtlTest.new_run
    add_lane = fixtures.LaneCtlTest.add_lane
    brief = fixtures.LaneCtlTest.brief
    calls = fixtures.LaneCtlTest.calls
    wait_for = fixtures.LaneCtlTest.wait_for
    commit_in = fixtures.LaneCtlTest.commit_in

    def review_fixture(self, tool="claude"):
        run = self.new_run()
        self.add_lane(run, "L1", owns="src/**,docs.md")
        self.commit_in(Path(run) / "worktrees/L1", "src/app.py", "x = 2\n")
        self.ctl("run", "set", "--run", run, "--validate", "echo checked")
        self.ctl("lane", "add", "--run", run, "--id", "R1", "--kind", "review", "--review-of", "L1",
                 "--tool", tool, "--model", "m1", "--effort", "medium")
        self.criteria = self.brief("The assignment updates x to 2.")
        return run

    def prepare(self, run, **kwargs):
        args = ["review", "prepare", "--run", run, "--id", "R1", "--acceptance-file", self.criteria,
                "--ci-status", "not-required"]
        for key, value in kwargs.items():
            args += ["--" + key.replace("_", "-"), str(value)]
        return self.ctl(*args)

    def state(self, run):
        return json.loads((Path(run) / "run.json").read_text())

    def report(self, run, findings=None, status="ready", evidence=None):
        r = self.state(run)["lanes"]["R1"]["review"]
        return self.brief(json.dumps({"head_sha": r["head_sha"], "status": status,
                                     "criteria": [{"id": "C1", "status": "pass", "evidence": evidence or
                                                   [{"path": "src/app.py", "line": 1, "current": "x = 2"}]}],
                                     "findings": findings or [], "notes": []}))

    def findings(self, run, path, check=True):
        return self.ctl("review", "findings", "--run", run, "--id", "R1", "--report-file", path, check=check)

    def test_reviewer_requires_target_and_pins_separate_detached_checkout(self):
        run = self.new_run()
        out = self.ctl("lane", "add", "--run", run, "--id", "R", "--kind", "review",
                       "--tool", "claude", "--model", "m1", "--effort", "medium", check=False)
        self.assertIn("require --review-of", out.stderr)
        self.add_lane(run, "L1")
        tree = Path(run) / "worktrees/L1"
        self.commit_in(tree, "src/app.py", "x = 2\n")
        self.ctl("lane", "add", "--run", run, "--id", "R1", "--kind", "review", "--review-of", "L1",
                 "--tool", "claude", "--model", "m1", "--effort", "medium")
        r = self.state(run)["lanes"]["R1"]
        self.assertIsNone(r["branch"])
        self.assertNotEqual(r["worktree"], str(tree))
        self.assertEqual((Path(r["worktree"]) / "src/app.py").read_text(), "x = 2\n")
        self.assertEqual((self.repo / "src/app.py").read_text(), "x = 1\n")
        self.assertEqual(subprocess.run(["git", "branch", "--show-current"], cwd=r["worktree"],
                                        text=True, capture_output=True).stdout, "")

    def test_reviewers_cannot_bypass_tool_restrictions_or_launch_without_packet(self):
        run = self.review_fixture()
        refused = self.ctl("launch", "--run", run, "--id", "R1", "--brief-file", self.brief(), check=False)
        self.assertIn("review prepare", refused.stderr)
        bad = self.ctl("lane", "add", "--run", run, "--id", "R2", "--kind", "review", "--review-of", "L1",
                       "--tool", "claude", "--model", "m1", "--effort", "medium", "--extra-tools", "Bash",
                       check=False)
        self.assertIn("cannot have", bad.stderr)
        self.prepare(run)
        (self.home / "adapters.json").write_text(json.dumps({"claude": {"new": ["claude", "--tools", "Bash"]}}))
        argv = json.loads(self.ctl("launch", "--run", run, "--id", "R1", "--brief-file", self.brief(),
                                  "--dry-run").stdout)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--exclude-dynamic-system-prompt-sections", argv)
        self.assertIn("Edit,Write,Bash,Agent,Read(**/.env*)", argv)
        self.assertIn('{"disableAllHooks":true}', argv)

    def test_codex_review_is_read_only_without_writable_gitdir_or_connectors(self):
        run = self.review_fixture(tool="codex")
        self.prepare(run)
        argv = json.loads(self.ctl("launch", "--run", run, "--id", "R1", "--brief-file", self.brief(),
                                  "--dry-run").stdout)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertNotIn("--add-dir", argv)
        self.assertIn("mcp_servers.test-server.enabled=false", argv)
        self.assertIn("plugins", argv)
        self.assertIn("apps", argv)
        self.assertIn('approval_policy="never"', argv)

    def test_gates_reject_missing_checks_red_checks_and_wrong_ci_sha(self):
        run = self.review_fixture()
        self.ctl("run", "set", "--run", run, "--validate", "false")
        out = self.ctl("review", "prepare", "--run", run, "--id", "R1", "--acceptance-file", self.criteria,
                       "--ci-status", "not-required", check=False)
        self.assertIn("check gate failed", out.stderr)
        self.ctl("run", "set", "--run", run, "--validate", "")
        out = self.ctl("review", "prepare", "--run", run, "--id", "R1", "--acceptance-file", self.criteria,
                       "--ci-status", "not-required", check=False)
        self.assertIn("no checks configured", out.stderr)
        self.ctl("run", "set", "--run", run, "--validate", "echo ok")
        out = self.ctl("review", "prepare", "--run", run, "--id", "R1", "--acceptance-file", self.criteria,
                       "--ci-status", "green", "--ci-sha", "deadbeef", "--ci-evidence", "CI run 12", check=False)
        self.assertIn("matching HEAD", out.stderr)

    def test_packet_bounds_diff_and_contains_acceptance_checks_and_exact_commit(self):
        run = self.review_fixture()
        self.prepare(run, max_diff_bytes=1)
        r = self.state(run)["lanes"]["R1"]["review"]
        packet = Path(r["packet"]).read_text()
        self.assertIn(r["head_sha"], packet)
        self.assertIn("C1:", packet)
        self.assertIn("PASS echo checked", packet)
        self.assertIn("Diff exceeds inline limit", packet)
        self.assertNotIn("+x = 2", packet)
        self.assertIn("+x = 2", Path(r["diffs"][0]["patch"]).read_text())

    def test_findings_validate_quotes_and_only_forward_blocking_changes(self):
        run = self.review_fixture()
        self.prepare(run)
        block = {"priority": 1, "blocking": True, "path": "src/app.py", "line": 1,
                 "current": "x = 2", "change_to": "x = 3", "why": "a specific triggering case fails"}
        suggestion = {**block, "priority": 3, "blocking": False, "change_to": "x = 4", "why": "optional follow-up"}
        path = self.report(run, [block, suggestion], "blocked")
        self.findings(run, path)
        folder = Path(self.state(run)["lanes"]["R1"]["review"]["packet"]).parent
        changes = (folder / "changes.md").read_text()
        self.assertIn("x = 3", changes)
        self.assertNotIn("x = 4", changes)
        self.assertIn("optional follow-up", (folder / "follow-ups.json").read_text())
        # The implementer's existing session is required; forwarding does not create a new lane.
        data = self.state(run)
        data["lanes"]["L1"]["session"] = "implementation-session"
        (Path(run) / "run.json").write_text(json.dumps(data))
        self.ctl("send", "--run", run, "--id", "L1", "--review-from", "R1", "--dry-run")
        (folder / "changes.md").write_text("tampered change list")
        refused = self.ctl("send", "--run", run, "--id", "L1", "--review-from", "R1", check=False)
        self.assertIn("change list changed", refused.stderr)
        for invalid in [{**block, "current": "imaginary"}, {**block, "line": 2},
                        {**block, "path": "../src/app.py"}, {**block, "priority": 3}]:
            result = self.findings(run, self.report(run, [invalid], "blocked"), check=False)
            self.assertNotEqual(result.returncode, 0)

    def test_criteria_require_evidence_and_stale_approval_cannot_queue(self):
        run = self.review_fixture()
        self.prepare(run)
        path = self.report(run)
        report = json.loads(Path(path).read_text())
        report["criteria"] = []
        self.assertNotEqual(self.findings(run, self.brief(json.dumps(report)), check=False).returncode, 0)
        self.findings(run, path)
        self.ctl("queue", "--run", run, "--add", "L1")
        self.commit_in(Path(run) / "worktrees/L1", "src/app.py", "x = 3\n")
        result = self.ctl("queue", "--run", run, "--add", "L1", check=False)
        self.assertIn("changed after review", result.stderr)

    def test_assigned_review_and_changed_gates_cannot_be_bypassed(self):
        run = self.review_fixture()
        self.prepare(run)
        result = self.ctl("queue", "--run", run, "--add", "L1", check=False)
        self.assertIn("requires a validated review", result.stderr)
        self.findings(run, self.report(run))
        self.ctl("run", "set", "--run", run, "--validate", "echo new-gate")
        result = self.ctl("queue", "--run", run, "--add", "L1", check=False)
        self.assertIn("gates changed", result.stderr)

    def test_unvalidated_prior_packet_does_not_hide_original_changes(self):
        run = self.review_fixture()
        self.prepare(run)
        self.commit_in(Path(run) / "worktrees/L1", "docs.md", "new docs\n")
        self.prepare(run)
        manifest = self.state(run)["lanes"]["R1"]["review"]
        self.assertIsNone(manifest["previous_head_sha"])
        self.assertEqual({item["path"] for item in manifest["diffs"]}, {"src/app.py", "docs.md"})

    def test_correction_packet_is_delta_and_reviewer_session_reused_for_other_target(self):
        run = self.review_fixture()
        self.prepare(run)
        self.findings(run, self.report(run))
        previous = self.state(run)["lanes"]["R1"]["review"]["head_sha"]
        self.commit_in(Path(run) / "worktrees/L1", "docs.md", "correction docs\n")
        self.prepare(run)
        r = self.state(run)["lanes"]["R1"]["review"]
        self.assertEqual(r["previous_head_sha"], previous)
        self.assertEqual([f["path"] for f in r["diffs"]], ["docs.md"])
        self.assertIn("Prior validated findings", Path(r["packet"]).read_text())
        self.add_lane(run, "L2", owns="docs.md")
        self.prepare(run, target="L2")
        r = self.state(run)["lanes"]["R1"]["review"]
        self.assertIsNone(r["previous_head_sha"])
        self.assertEqual(self.state(run)["lanes"]["R1"]["review_of"], "L2")

    def test_reviewer_mutations_are_detected_and_readiness_blocked(self):
        run = self.review_fixture()
        self.prepare(run)
        review_tree = Path(self.state(run)["lanes"]["R1"]["worktree"])
        env = {**self.env, "FAKE_TOUCH": str(review_tree / "src/app.py")}
        self.ctl("launch", "--run", run, "--id", "R1", "--brief-file", self.brief(), env=env)
        self.wait_for(run)
        r = self.state(run)["lanes"]["R1"]
        self.assertEqual(r["state"], "failed")
        self.assertEqual(r["report_status"], "blocked")

    def test_risk_is_conservative_and_required_reviews_cannot_be_skipped(self):
        self.ctl("profile", "set", "--repo", ".", "review=required")
        run = self.new_run()
        self.add_lane(run, "L1")
        self.commit_in(Path(run) / "worktrees/L1", "docs.md", "edited docs\n")
        result = json.loads(self.ctl("risk", "--run", run, "--id", "L1", "--json").stdout)
        self.assertEqual(result["level"], "low")
        self.assertFalse(result["skip_eligible"])
        self.assertTrue(result["review_required"])
        self.assertIn("requires a validated review", self.ctl("queue", "--run", run, "--add", "L1", check=False).stderr)
        self.commit_in(Path(run) / "worktrees/L1", "src/auth.py", "authorize = False\n")
        result = json.loads(self.ctl("risk", "--run", run, "--id", "L1", "--json").stdout)
        self.assertEqual((result["level"], result["effort"]), ("high", "high"))

    def test_contract_snapshots_survive_asset_updates_and_tools_are_canonical(self):
        root = Path(self.temp.name) / "plugin"
        (root / "assets").mkdir(parents=True)
        for name in ("lane-contract.md", "review-contract.md"):
            (root / "assets" / name).write_text("old shared rules")
        data = {"contracts": lane_cache.snapshot_contracts(root)}
        (root / "assets/lane-contract.md").write_text("new shared rules")
        self.assertEqual(lane_cache.contract(data, {"kind": "implement"}, root), "old shared rules")
        run = self.new_run()
        self.add_lane(run, "L1", extra_tools="WebSearch,WebFetch,Read")
        argv = json.loads(self.ctl("launch", "--run", run, "--id", "L1", "--brief-file", self.brief(),
                                  "--dry-run").stdout)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Edit,Write,Glob,Grep,Bash,WebFetch,WebSearch")
        self.assertIn("--exclude-dynamic-system-prompt-sections", argv)
        self.assertEqual(argv[argv.index("--system-prompt-snapshot") + 1], "on")

    def test_first_call_metrics_deduplicate_message_updates_and_keep_unknowns(self):
        path = Path(self.temp.name) / "events.jsonl"
        first = {"type": "assistant", "message": {"id": "a1", "model": "m1", "content": [],
                 "usage": {"input_tokens": 20, "cache_read_input_tokens": 200, "output_tokens": 1}}}
        updated = json.loads(json.dumps(first)); updated["message"]["usage"]["output_tokens"] = 9
        second = json.loads(json.dumps(first)); second["message"]["id"] = "a2"
        result = {"type": "result", "usage": {"input_tokens": 40, "cache_read_input_tokens": 400, "output_tokens": 10}}
        path.write_text("\n".join(json.dumps(e) for e in (first, updated, second, result)))
        parsed = ctl_module.parse_stream(path, "claude")
        self.assertEqual(len(parsed["model_calls"]), 2)
        self.assertEqual(parsed["first_call"]["out"], 9)
        self.assertEqual(parsed["tokens_in"], 440)
        self.assertTrue(parsed["response_started"])
        run = self.new_run(); self.add_lane(run, "L2", tool="codex")
        self.ctl("launch", "--run", run, "--id", "L2", "--brief-file", self.brief())
        self.wait_for(run)
        usage = json.loads(self.ctl("usage", "--run", run, "--json").stdout)
        self.assertIsNone(usage["lanes"]["L2"]["cache_write"])
        self.assertIsNone(usage["lanes"]["L2"]["cost_usd"])
        self.assertIn("unknown", self.ctl("usage", "--run", run).stdout)

    def test_warm_group_waits_for_response_and_does_not_launch_after_timeout(self):
        run = self.new_run(); self.add_lane(run, "L1"); self.add_lane(run, "L2")
        briefs = self.brief(json.dumps({"L1": self.brief(), "L2": self.brief()}))
        self.ctl("launch-group", "--run", run, "--briefs-file", briefs, "--warm-cache", "--warm-timeout", "3")
        self.wait_for(run)
        self.assertEqual(len(self.calls()), 2)
        self.assertIn("cache_profile", self.state(run)["lanes"]["L1"])
        self.add_lane(run, "L3"); self.add_lane(run, "L4")
        briefs = self.brief(json.dumps({"L3": self.brief(), "L4": self.brief()}))
        out = self.ctl("launch-group", "--run", run, "--briefs-file", briefs, "--warm-cache", "--warm-timeout", "0.1",
                       env={**self.env, "FAKE_SLEEP": "30"}, check=False)
        self.assertIn("followers not launched", out.stderr)
        self.assertEqual(self.state(run)["lanes"]["L4"]["attempt"], 0)
        self.ctl("stop", "--run", run, "--id", "L3", "--reason", "test timeout cleanup")

    def test_codex_developer_experiment_requires_preserved_prefix(self):
        bad = self.ctl("run", "new", "--repo", ".", "--name", "experiment", "--base", "main",
                       "--codex-contract", "developer", check=False)
        self.assertIn("preserving existing instructions", bad.stderr)
        run = self.ctl("run", "new", "--repo", ".", "--name", "experiment", "--base", "main",
                       "--codex-contract", "developer", "--codex-developer-prefix-file",
                       self.brief("Keep existing policy.")).stdout.strip()
        self.add_lane(run, "L1", tool="codex")
        self.ctl("launch", "--run", run, "--id", "L1", "--brief-file", self.brief())
        self.wait_for(run)
        args = self.calls()[0]["argv"]
        dev = next(arg for arg in args if arg.startswith("developer_instructions="))
        self.assertIn("Keep existing policy.", dev)
        self.assertIn("# Lane Contract", dev)
        self.assertTrue(args[-1].startswith("# Lane L1"))


if __name__ == "__main__":
    unittest.main()
