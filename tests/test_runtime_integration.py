import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import test_lanectl as fixtures
import test_review_cache as review

sys.path.insert(0, str(fixtures.LANECTL.parent))
import lane_runtime
import lane_integration
import lane_planning

c = review.ctl_module


class RuntimeIntegrationTest(unittest.TestCase):
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
    state = review.ReviewCacheTest.state

    def save(self, run, data):
        (Path(run) / 'run.json').write_text(json.dumps(data))

    def tree(self, run, lane='L1'):
        return Path(run) / 'worktrees' / lane

    def test_quoted_command_separators_remain_inside_the_command(self):
        command = "python3 -c 'import os,pathlib; print(os.getcwd())'"
        self.assertEqual(c.split_commands(command + '; echo next'), [command, 'echo next'])
        self.assertEqual(c.split_commands("echo 'a,b'"), ["echo 'a,b'"])
        self.assertEqual(c.split_commands('echo one,echo two'), ['echo one', 'echo two'])

    def test_setup_refuses_main_checkout_before_executing_commands(self):
        run = self.new_run()
        self.ctl('run', 'set', '--run', run, '--setup', 'touch unintended-main-file')
        result = self.ctl('lane', 'add', '--run', run, '--id', 'L1', '--tool', 'claude', '--model', 'm1',
                          '--effort', 'medium', '--worktree', str(self.repo), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.repo / 'unintended-main-file').exists())

    def test_sync_checks_even_an_up_to_date_branch(self):
        run = self.new_run(); self.add_lane(run, 'L1')
        self.ctl('run', 'set', '--run', run, '--validate', 'echo failing-fixture; false')
        result = self.ctl('sync', '--run', run, '--no-fetch', '--check', check=False)
        self.assertEqual(result.returncode, 4)
        self.assertIn('up to date; check FAILED', result.stdout)
        self.assertEqual(self.state(run)['lanes']['L1']['state'], 'blocked')

    def test_setup_reuses_fingerprint_and_invalidates_lock_changes(self):
        run = self.new_run()
        self.ctl('run', 'set', '--run', run, '--setup', 'echo install >> .installed')
        self.add_lane(run, 'L1')
        self.ctl('setup', '--run', run, '--id', 'L1')
        self.assertEqual((self.tree(run) / '.installed').read_text(), 'install\n')
        self.commit_in(self.tree(run), 'requirements.txt', '# dependency pin\n')
        rejected = self.ctl('launch', '--run', run, '--id', 'L1', '--brief-file', self.brief(), check=False)
        self.assertIn('setup is missing or stale', rejected.stderr)
        self.ctl('setup', '--run', run, '--id', 'L1')
        self.assertEqual((self.tree(run) / '.installed').read_text(), 'install\ninstall\n')
        self.assertFalse(self.calls())

    def test_failed_setup_is_recorded_without_installer_output(self):
        run = self.new_run()
        self.ctl('run', 'set', '--run', run, '--setup', 'echo PRIVATE_SETUP_VALUE; false')
        result = self.ctl('lane', 'add', '--run', run, '--id', 'L1', '--tool', 'claude', '--model', 'm1',
                          '--effort', 'medium', '--worktree', 'auto', '--create-worktree', '--branch', 'lane/L1', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('PRIVATE_SETUP_VALUE', result.stdout + result.stderr)
        self.assertEqual(self.state(run)['lanes']['L1']['setup']['status'], 'failed')
        self.assertEqual(self.state(run)['lanes']['L1']['state'], 'blocked')
        self.assertFalse(self.calls())

    def test_nested_lock_requires_configured_setup(self):
        (self.repo / 'src/requirements.txt').write_text('# nested dependency\n')
        self.git('add', '.'); self.git('commit', '-qm', 'nested lock')
        run = self.new_run()
        result = self.ctl('lane', 'add', '--run', run, '--id', 'L1', '--tool', 'claude', '--model', 'm1',
                          '--effort', 'medium', '--worktree', 'auto', '--create-worktree', '--branch', 'lane/L1', check=False)
        self.assertIn('dependency lockfile', result.stderr)
        self.ctl('run', 'set', '--run', run, '--setup-not-required')
        self.ctl('setup', '--run', run, '--id', 'L1')

    def test_environment_namespaces_cross_runs_and_shared_download_cache(self):
        run = self.new_run(); self.add_lane(run, 'L1'); self.add_lane(run, 'L2')
        other = self.ctl('run', 'new', '--repo', '.', '--name', 'Second run', '--base', 'main').stdout.splitlines()[0]
        self.ctl('lane', 'add', '--run', other, '--id', 'L1', '--tool', 'claude', '--model', 'm1', '--effort', 'medium',
                 '--worktree', 'auto', '--create-worktree', '--branch', 'second/L1')
        envs = [self.state(run)['lanes'][key]['environment'] for key in ('L1', 'L2')]
        envs += [self.state(other)['lanes']['L1']['environment']]
        for key in ('LANE_NAMESPACE', 'LANE_PORT_START', 'TMPDIR'):
            self.assertEqual(len({env[key] for env in envs}), 3)
        self.assertEqual(len({env['LANE_PACKAGE_CACHE'] for env in envs}), 1)
        data = self.state(run); lane = data['lanes']['L1']
        with mock.patch.dict(os.environ, self.env):
            full = lane_runtime.environment(data, lane)
        self.assertEqual(full['UV_CACHE_DIR'], str(self.home / 'package-cache/uv'))
        self.ctl('cleanup', '--run', run, '--id', 'L2', '--remove-worktree')
        leases = json.loads((self.home / 'resources.json').read_text())
        self.assertNotIn(str(Path(run).resolve()) + ':L2', leases)

    def test_runtime_templates_used_for_setup_checks_and_agent(self):
        templates = Path(self.temp.name) / 'env.json'
        templates.write_text(json.dumps({'APP_NAMESPACE': '{namespace}', 'RUNTIME_SECRET': '${TEST_RUNTIME_SECRET}'}))
        run = self.new_run()
        self.ctl('run', 'set', '--run', run, '--env-template-file', str(templates),
                 '--setup', "python3 -c 'import os,pathlib; pathlib.Path(\".namespace\").write_text(os.environ[\"APP_NAMESPACE\"])'")
        env = {**self.env, 'TEST_RUNTIME_SECRET': 'runtime-private-value'}
        self.env = env; self.add_lane(run, 'L1')
        namespace = self.state(run)['lanes']['L1']['environment']['LANE_NAMESPACE']
        self.assertEqual((self.tree(run) / '.namespace').read_text(), namespace)
        self.ctl('lane', 'set', '--run', run, '--id', 'L1', '--validate',
                 "python3 -c 'import os,pathlib; assert pathlib.Path(\".namespace\").read_text() == os.environ[\"APP_NAMESPACE\"]; assert os.environ[\"RUNTIME_SECRET\"]'")
        self.ctl('check', '--run', run, '--id', 'L1')
        agent = self.bin / 'claude'
        agent.write_text(agent.read_text().replace('target = os.environ.get', 'assert os.environ["APP_NAMESPACE"] == ' + repr(namespace) + '\nassert os.environ["RUNTIME_SECRET"]\ntarget = os.environ.get'))
        self.ctl('launch', '--run', run, '--id', 'L1', '--brief-file', self.brief()); self.wait_for(run)
        self.assertEqual(len(self.calls()), 1)
        self.assertNotIn('runtime-private-value', (Path(run) / 'run.json').read_text())

    def test_readiness_rejects_goal_criteria_and_validation_gaps(self):
        run = self.new_run(); self.add_lane(run, 'L1')
        for text, error in [('Acceptance:\n- observable\n', 'concrete Goal'), ('Goal: useful work\n', 'acceptance criteria')]:
            path = Path(self.temp.name) / 'raw.md'; path.write_text(text)
            result = self.ctl('launch', '--run', run, '--id', 'L1', '--brief-file', str(path), check=False)
            self.assertIn(error, result.stderr)
        self.ctl('run', 'set', '--run', run, '--validate', '')
        result = self.ctl('launch', '--run', run, '--id', 'L1', '--brief-file', self.brief(), check=False)
        self.assertIn('needs validation', result.stderr)
        self.assertFalse(self.calls())

    def test_ticket_briefs_require_ownership_approval_and_preserve_dependencies(self):
        run = self.new_run(); self.add_lane(run, 'L0'); self.add_lane(run, 'L1')
        ticket = Path(self.temp.name) / 'ticket.json'
        ticket.write_text(json.dumps({'title': 'Update application behavior', 'acceptance': ['x is correct'],
                                     'validation': ['echo checked'], 'dependencies': ['T-L0']}))
        self.ctl('brief', '--run', run, '--id', 'L1', '--ticket-file', str(ticket), '--terms', 'app')
        lane = self.state(run)['lanes']['L1']
        self.assertEqual(lane['blocked_by'], ['L0'])
        self.assertIn('src/app.py', lane['start'])
        self.assertIn('ownership', self.ctl('approve-brief', '--run', run, '--id', 'L1', check=False).stderr)
        self.ctl('lane', 'set', '--run', run, '--id', 'L1', '--owns', 'src/**')
        self.ctl('lane', 'set', '--run', run, '--id', 'L0', '--state', 'merged')
        draft = Path(lane['brief_draft']['path']); draft.write_text(draft.read_text().replace('x is correct', 'x equals two'))
        self.assertIn('coordinator approval', self.ctl('launch', '--run', run, '--id', 'L1', '--brief-file', str(draft), check=False).stderr)
        self.ctl('approve-brief', '--run', run, '--id', 'L1')
        self.assertEqual(self.state(run)['lanes']['L1']['acceptance'], ['x equals two'])
        draft.write_text(draft.read_text() + 'New constraint\n')
        self.assertIn('reapproval', self.ctl('launch', '--run', run, '--id', 'L1', '--brief-file', str(draft), check=False).stderr)

    def combined_fixture(self):
        (self.repo / 'src/a.py').write_text('x = 1\n'); (self.repo / 'src/b.py').write_text('x = 1\n')
        self.git('add', '.'); self.git('commit', '-qm', 'pair')
        run = self.new_run()
        command = "python3 -c 'exec(open(\"src/a.py\").read()); a=x; exec(open(\"src/b.py\").read()); assert not(a==2 and x==2)'"
        self.ctl('run', 'set', '--run', run, '--validate', command, '--integration', 'required')
        self.add_lane(run, 'L1', owns='src/a.py'); self.add_lane(run, 'L2', owns='src/b.py')
        for key, name in [('L1', 'src/a.py'), ('L2', 'src/b.py')]:
            self.commit_in(self.tree(run, key), name, 'x = 2\n')
            self.ctl('check', '--run', run, '--id', key)
            self.ctl('lane', 'set', '--run', run, '--id', key, '--state', 'merge-queued')
        return run

    def test_combined_failure_does_not_mutate_source_lanes(self):
        run = self.combined_fixture()
        before = {key: subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.tree(run, key)) for key in ('L1', 'L2')}
        result = self.ctl('integration', '--run', run, check=False)
        self.assertIn('combined validation failed', result.stderr)
        self.assertEqual(self.state(run)['integration']['status'], 'failed')
        self.assertIn('needs integration proof', self.ctl('queue', '--run', run).stdout)
        for key, head in before.items():
            self.assertEqual(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.tree(run, key)), head)

    def test_integration_proof_invalidates_commits_gates_environment_order_and_base(self):
        run = self.combined_fixture()
        self.ctl('run', 'set', '--run', run, '--validate', 'echo full-gate')
        self.ctl('integration', '--run', run)
        data = self.state(run)
        self.assertTrue(lane_integration.current(c, data))
        self.assertIn('next', self.ctl('queue', '--run', run).stdout)
        for mutate in (lambda d: d['rules']['validate'].append('echo extra'),
                       lambda d: d['environment_templates'].update({'TEST_NAMESPACE': '{namespace}'}),
                       lambda d: d['lanes']['L1']['reserves'].append('migration=2'),
                       lambda d: d['lanes']['L1'].update({'queued_at': 'later'})):
            changed = json.loads(json.dumps(data)); mutate(changed)
            self.assertFalse(lane_integration.current(c, changed))
        self.commit_in(self.tree(run), 'src/a.py', 'x = 3\n')
        self.assertFalse(lane_integration.current(c, data))
        self.commit_in(self.repo, 'docs.md', 'new base\n')
        self.assertFalse(lane_integration.current(c, data))

    def test_baseline_checks_candidate_and_base_test_overlay(self):
        run = self.new_run(); self.add_lane(run, 'L1', owns='src/**,tests/**')
        tree = self.tree(run); (tree / 'tests').mkdir()
        self.commit_in(tree, 'src/app.py', 'x = 2\n')
        self.commit_in(tree, 'tests/test_new.py', 'exec(open("src/app.py").read())\nassert x == 2\n')
        result = self.ctl('check', '--run', run, '--id', 'L1', '--baseline-command', 'python3 tests/test_new.py')
        self.assertIn('base-fails', result.stdout)
        self.assertNotIn('inconclusive', result.stdout)
        self.assertFalse((self.repo / 'tests').exists())
        self.commit_in(tree, 'tests/test_new.py', 'import does_not_exist\n')
        result = self.ctl('check', '--run', run, '--id', 'L1', '--baseline-command', 'python3 tests/test_new.py', check=False)
        self.assertIn('must first pass', result.stderr)

    def test_secret_scan_is_gated_and_redacted(self):
        self.ctl('profile', 'set', '--repo', '.', 'secret_scan=echo PRIVATE_SCANNER_VALUE; false')
        run = self.new_run(); self.add_lane(run, 'L1')
        result = self.ctl('check', '--run', run, '--id', 'L1', check=False)
        self.assertEqual(result.returncode, 4)
        self.assertIn('FAIL secret scan', result.stdout)
        self.assertNotIn('PRIVATE_SCANNER_VALUE', result.stdout + result.stderr)

    def test_comparison_counts_cache_writes_and_rejects_mismatches(self):
        left = self.new_run(); self.add_lane(left, 'L1')
        right = self.ctl('run', 'new', '--repo', '.', '--name', 'Serial', '--base', 'main').stdout.splitlines()[0]
        self.ctl('lane', 'add', '--run', right, '--id', 'S1', '--items', 'T-L1', '--tool', 'claude', '--model', 'm1',
                 '--effort', 'medium', '--worktree', 'auto', '--create-worktree', '--branch', 'serial')
        for run, key, amount in [(left, 'L1', 100), (right, 'S1', 200)]:
            data = self.state(run); data['state'] = 'closed'; lane = data['lanes'][key]; lane['state'] = 'closed'
            lane['usage'] = {'1': {'in': amount, 'fresh': 10, 'cache_write': amount - 20, 'cache_read': 10, 'out': 0, 'cost_usd': None}}
            self.save(run, data)
        evidence = Path(self.temp.name) / 'evidence.json'
        evidence.write_text(json.dumps({'same_acceptance': True, 'same_fixtures': True, 'both_passed': True}))
        output = Path(self.temp.name) / 'comparison.json'
        self.ctl('compare', '--lanes-run', left, '--serial-run', right, '--evidence-file', str(evidence), '--output', str(output))
        result = json.loads(output.read_text()); self.assertEqual(result['reported_token_reduction_percent'], 50)
        self.assertFalse(result['lanes']['cost_complete'])
        data = self.state(right); data['initial_base_sha'] = 'different'; self.save(right, data)
        self.assertIn('starting commit', self.ctl('compare', '--lanes-run', left, '--serial-run', right,
                                                '--evidence-file', str(evidence), '--output', str(output), check=False).stderr)

    def test_ci_requires_known_green_exact_head(self):
        run = self.new_run(); self.add_lane(run, 'L1')
        self.ctl('lane', 'set', '--run', run, '--id', 'L1', '--pr', 'https://example/pr/1')
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.tree(run), text=True).strip()
        gh = self.bin / 'gh'
        def respond(value):
            gh.write_text('#!' + sys.executable + '\nprint(' + repr(json.dumps(value)) + ')\n'); gh.chmod(0o755)
        respond({'headRefOid': head, 'statusCheckRollup': [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]})
        self.assertIn('CI green', self.ctl('ci', '--run', run, '--id', 'L1').stdout)
        respond({'headRefOid': head, 'statusCheckRollup': [{'status': 'COMPLETED', 'conclusion': 'UNKNOWN'}]})
        self.assertNotEqual(self.ctl('ci', '--run', run, '--id', 'L1', check=False).returncode, 0)
        respond({'headRefOid': 'wrong', 'statusCheckRollup': []})
        self.assertIn('differs', self.ctl('ci', '--run', run, '--id', 'L1', check=False).stderr)

    def test_soft_token_budget_stops_reported_overspend_and_blocks_resume(self):
        run = self.new_run(); self.add_lane(run, 'L1', tool='codex', token_budget='100')
        tool = self.bin / 'codex'
        tool.write_text(tool.read_text().replace('import json, os, sys', 'import json, os, sys, time') + '\nsys.stdout.flush()\ntime.sleep(20)\n')
        self.ctl('launch', '--run', run, '--id', 'L1', '--brief-file', self.brief())
        self.wait_for(run)
        lane = self.state(run)['lanes']['L1']
        self.assertEqual(lane['state'], 'blocked'); self.assertIn('threshold', lane['error'])
        result = self.ctl('send', '--run', run, '--id', 'L1', '--message-file', self.brief('Continue'), check=False)
        self.assertIn('budget is exhausted', result.stderr)

    def test_attribution_disabled_for_repo_and_claude_launch(self):
        settings = json.loads((fixtures.ROOT / '.claude/settings.json').read_text())
        self.assertEqual(settings['attribution'], {'commit': '', 'pr': '', 'sessionUrl': False})
        run = self.new_run(); self.add_lane(run, 'L1')
        self.ctl('launch', '--run', run, '--id', 'L1', '--brief-file', self.brief()); self.wait_for(run)
        argv = self.calls()[0]['argv']
        self.assertEqual(json.loads(argv[argv.index('--settings') + 1])['attribution'], settings['attribution'])
        self.assertIn('Never add agent attribution', (fixtures.PLUGIN / 'assets/lane-contract.md').read_text())


if __name__ == '__main__':
    unittest.main()
