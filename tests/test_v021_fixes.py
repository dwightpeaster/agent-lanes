"""Regression tests for the 0.2.1 review fixes."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

import test_lanectl as fixtures
import test_review_cache as review

sys.path.insert(0, str(fixtures.LANECTL.parent))
import lane_cache
import lane_review
import lane_runtime


class FixesTest(unittest.TestCase):
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
    review_fixture = review.ReviewCacheTest.review_fixture
    prepare = review.ReviewCacheTest.prepare
    state = review.ReviewCacheTest.state
    report = review.ReviewCacheTest.report
    findings = review.ReviewCacheTest.findings

    def dry(self, run, lane_id, brief=None):
        result = self.ctl("launch", "--run", run, "--id", lane_id, "--brief-file", brief or self.brief(), "--dry-run")
        return json.loads(result.stdout), result.stderr

    def approved(self, run):
        self.prepare(run)
        self.findings(run, self.report(run))
        self.ctl("queue", "--run", run, "--add", "L1")

    # ------------------------------------------------------------ reviewers

    def test_claude_reviewer_can_read_its_packet_and_ignores_reviewed_project_settings(self):
        run = self.review_fixture()
        self.ctl("lane", "set", "--run", run, "--id", "R1", "--state", "planned")
        packet = Path(self.prepare(run).stdout.strip())
        argv, _ = self.dry(run, "R1")
        review_dir = Path(argv[argv.index("--add-dir") + 1])
        self.assertEqual(review_dir.resolve(), (Path(run) / "lanes" / "R1" / "reviews").resolve())
        self.assertTrue(packet.resolve().is_relative_to(review_dir.resolve()))
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "user")

    def test_codex_reviewer_skips_what_it_cannot_disable_instead_of_failing(self):
        run = self.review_fixture(tool="codex")
        self.prepare(run)
        argv, stderr = self.dry(run, "R1")
        self.assertIn("mcp_servers.test-server.enabled=false", argv)
        self.assertFalse(any("off-server" in token for token in argv))
        self.assertFalse(any("odd.server" in token for token in argv))
        self.assertIn("odd.server", stderr)
        self.assertNotIn("multi_agent", argv)  # unknown to this Codex, so not passed

    def test_fenced_json_report_is_accepted(self):
        run = self.review_fixture()
        self.prepare(run)
        report = Path(self.report(run)).read_text()
        fenced = self.brief("```json\n" + report + "\n```")
        self.assertIn("ready", self.findings(run, fenced).stdout)

    def test_reviewer_send_keeps_the_message(self):
        run = self.review_fixture()
        self.prepare(run)
        self.ctl("launch", "--run", run, "--id", "R1", "--brief-file", self.brief())
        self.wait_for(run)
        self.ctl("send", "--run", run, "--id", "R1", "--message-file", self.brief("ANSWER: C1 means x == 2"))
        self.wait_for(run)
        self.assertIn("ANSWER: C1 means x == 2", self.calls()[-1]["argv"][1])

    def test_review_policy_and_sensitive_paths_read_sensibly(self):
        for text in ("not required", "No.", "optional", "none", ""):
            self.assertFalse(lane_review.review_required(text), text)
        for text in ("required", "yes, agent review", "Always"):
            self.assertTrue(lane_review.review_required(text), text)
        for path in ("src/author.py", "src/clock.py", "docs/settled.md"):
            self.assertFalse(lane_review.sensitive(path, []), path)
        for path in ("src/auth/login.py", "package-lock.json", "db/migrations/0001.sql", ".github/workflows/ci.yml"):
            self.assertTrue(lane_review.sensitive(path, []), path)

    # ------------------------------------------------------------ merge queue

    def test_clean_rebase_with_same_changes_carries_approval(self):
        run = self.review_fixture()
        self.approved(run)
        (self.repo / "docs.md").write_text("docs moved on\n")
        self.git("commit", "-qam", "main moves")
        out = self.ctl("sync", "--run", run, "--no-fetch").stdout
        self.assertIn("review approval carried forward", out)
        lane = self.state(run)["lanes"]["L1"]
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=lane["worktree"], text=True).strip()
        self.assertEqual(lane["review_approval"]["head_sha"], head)
        self.assertEqual(lane["state"], "merge-queued")

    def test_rebase_that_changes_the_lane_diff_clears_approval_and_dequeues(self):
        (self.repo / "src/app.py").write_text("".join(f"v{i} = {i}\n" for i in range(10)))
        self.git("commit", "-qam", "longer file")
        run = self.new_run()
        self.add_lane(run, "L1", owns="src/**,docs.md")
        self.commit_in(Path(run) / "worktrees/L1", "src/app.py",
                       "".join(f"v{i} = {'changed' if i == 4 else i}\n" for i in range(10)))
        self.ctl("run", "set", "--run", run, "--validate", "echo checked")
        self.ctl("lane", "add", "--run", run, "--id", "R1", "--kind", "review", "--review-of", "L1",
                 "--tool", "claude", "--model", "m1", "--effort", "medium")
        self.criteria = self.brief("v4 is changed.")
        self.prepare(run)
        self.findings(run, self.report(run, evidence=[{"path": "src/app.py", "line": 5, "current": "v4 = changed"}]))
        self.ctl("queue", "--run", run, "--add", "L1")
        (self.repo / "src/app.py").write_text("".join(f"v{i} = {'main' if i == 6 else i}\n" for i in range(10)))
        self.git("commit", "-qam", "main edits a nearby line")
        out = self.ctl("sync", "--run", run, "--no-fetch").stdout
        self.assertIn("rebased cleanly", out)
        self.assertIn("review approval cleared", out)
        lane = self.state(run)["lanes"]["L1"]
        self.assertNotIn("review_approval", lane)
        self.assertEqual(lane["state"], "in-review")

    def test_queue_reports_dirty_lanes_and_unknown_dependencies_instead_of_failing(self):
        run = self.new_run()
        self.add_lane(run, "L1")
        self.add_lane(run, "L2", blocked_by="L9")
        for lane_id in ("L1", "L2"):
            self.ctl("lane", "set", "--run", run, "--id", lane_id, "--state", "merge-queued")
        tree = Path(run) / "worktrees/L1"
        (tree / "build.out").write_text("artifact\n")
        self.assertNotIn("uncommitted", self.ctl("queue", "--run", run).stdout)
        (tree / "src/app.py").write_text("x = 99\n")
        out = self.ctl("queue", "--run", run).stdout
        self.assertIn("L1 T-L1 lane/L1  uncommitted changes", out)
        self.assertIn("unknown dependency L9", out)

    def test_integration_checkout_is_removed_after_a_pass_that_leaves_files(self):
        run = self.new_run()
        self.ctl("run", "set", "--run", run, "--validate", "touch build.out", "--integration", "required")
        self.add_lane(run, "L1", owns="src/**")
        self.commit_in(Path(run) / "worktrees/L1", "src/app.py", "x = 2\n")
        self.ctl("lane", "set", "--run", run, "--id", "L1", "--state", "merge-queued")
        out = self.ctl("integration", "--run", run).stdout
        self.assertIn("integration PASS", out)
        self.assertNotIn("retained", out)
        self.assertNotIn("integration", self.git("worktree", "list"))

    # ------------------------------------------------------------ launches and briefs

    def test_brief_text_with_placeholders_is_not_rewritten(self):
        run = self.new_run()
        self.ctl("run", "set", "--run", run, "--validate", "echo ok")
        self.add_lane(run, "L1")
        argv, _ = self.dry(run, "L1", self.brief("Goal: document {model} and {contract} tokens\nAcceptance:\n- done"))
        self.assertIn("<prompt>", argv)

    def test_dry_run_saves_nothing(self):
        run = self.new_run()
        self.ctl("run", "set", "--run", run, "--validate", "echo ok")
        self.add_lane(run, "L1")
        before = (Path(run) / "run.json").read_text()
        self.dry(run, "L1")
        after = json.loads((Path(run) / "run.json").read_text())
        self.assertNotIn("adapters", after)
        self.assertEqual(json.loads(before)["lanes"]["L1"].get("attempt"), after["lanes"]["L1"].get("attempt"))

    def test_cli_probe_failure_is_not_fatal(self):
        self.assertEqual(lane_cache.cli_info(str(Path(self.temp.name) / "missing-cli")), ("unknown", ""))

    def test_numbered_acceptance_criteria_are_recognized(self):
        self.assertEqual(lane_runtime.criteria_from_brief("Acceptance:\n1. first\n2) second\n- third"),
                         ["first", "second", "third"])

    def test_brief_keeps_explicit_validation_and_never_stores_the_scanner(self):
        run = self.new_run()
        self.ctl("run", "set", "--run", run, "--validate", "echo run-gate")
        self.add_lane(run, "L1", validate="echo lane-gate")
        ticket = self.brief(json.dumps({"title": "Update app value", "acceptance": ["x is 2"],
                                        "validation": ["echo ticket-gate"]}))
        self.ctl("brief", "--run", run, "--id", "L1", "--ticket-file", ticket, "--terms", "app")
        self.assertEqual(self.state(run)["lanes"]["L1"]["validate"], ["echo lane-gate"])
        self.add_lane(run, "L2")
        self.ctl("brief", "--run", run, "--id", "L2", "--ticket-file",
                 self.brief(json.dumps({"title": "Update app value", "acceptance": ["x is 2"]})), "--terms", "app")
        self.assertEqual(self.state(run)["lanes"]["L2"]["validate"], [])

    # ------------------------------------------------------------ runtime

    def test_timeout_kills_the_whole_process_group(self):
        pidfile = Path(self.temp.name) / "child.pid"
        code, _ = lane_runtime.run_shell(f"sleep 30 & echo $! > {pidfile}; wait", self.repo, None, 1)
        self.assertEqual(code, 124)
        time.sleep(0.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_setup_does_not_hold_the_run_lock(self):
        run = self.new_run()
        self.add_lane(run, "L1")
        self.ctl("run", "set", "--run", run, "--setup", "sleep 3")
        proc = subprocess.Popen([sys.executable, str(fixtures.LANECTL), "setup", "--run", run, "--id", "L1"],
                                cwd=self.repo, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(0.8)
            began = time.monotonic()
            self.ctl("status", "--run", run)
            self.assertLess(time.monotonic() - began, 1.5)
            self.assertEqual(self.state(run)["lanes"]["L1"]["setup"]["status"], "running")
        finally:
            proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.state(run)["lanes"]["L1"]["setup"]["status"], "pass")

    def test_open_runs_from_0_1_keep_working(self):
        run = self.new_run()
        self.ctl("run", "set", "--run", run, "--validate", "echo legacy-ok")
        self.add_lane(run, "L1")
        data = self.state(run)
        data["schema"] = 1
        data["lanes"]["L1"].pop("setup", None)
        (Path(run) / "run.json").write_text(json.dumps(data))
        self.assertIn("PASS echo legacy-ok", self.ctl("check", "--run", run, "--id", "L1").stdout)
        self.assertIn("check pass", self.ctl("sync", "--run", run, "--no-fetch", "--check").stdout)
        self.assertNotEqual(self.state(run)["lanes"]["L1"]["state"], "blocked")


if __name__ == "__main__":
    unittest.main()
