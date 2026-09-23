import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from qa_lane import netpolicy, runner, state as qa_state


SHA_A = 'a' * 40
SHA_B = 'b' * 40
SHA_C = 'c' * 40
DAY = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


class Result:
    def __init__(self, stdout='', returncode=0, stderr=''):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def cfg_for(root):
    cfg = dict(runner.DEFAULT_CONFIG)
    cfg['state_dir'] = str(Path(root) / 'state')
    cfg['src_dir'] = str(Path(root) / 'state' / 'src')
    cfg['egress_required'] = False
    Path(cfg['state_dir']).mkdir(parents=True, exist_ok=True)
    Path(cfg['src_dir']).mkdir(parents=True, exist_ok=True)
    return cfg


def fake_docker_ok(*args, **kw):
    return Result('')


@unittest.skipUnless(os.name == 'posix',
                     'runner.cycle serializes through a POSIX flock')
class OwnershipGateTests(unittest.TestCase):
    """Failed teardown must block the next cycle with a named error."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        self.st = qa_state.State(Path(self.cfg['state_dir']) / 'state.db')

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def test_leftover_container_blocks_cycle(self):
        # An interrupted run's container survives reconcile because
        # `docker rm` keeps failing -> the cycle must refuse to start.
        self.st.enqueue('qa-1', SHA_A, 1.0, '2026-09-15')
        self.st.begin('qa-1', 424242, 2.0)
        self.st.enqueue('qa-2', SHA_B, 3.0, '2026-09-15')

        def fake_docker(*args, timeout=120, check=True):
            if args[0] == 'ps':
                return Result('dead1 qa-1\n')
            if args[:2] == ('network', 'ls'):
                return Result('')
            if args[0] == 'rm':
                return Result('', returncode=1, stderr='device busy')
            return Result('')

        logs = []
        with patch.object(runner, 'docker', fake_docker), \
                patch.object(runner, '_pid_alive', return_value=False), \
                patch.object(runner, 'run') as mock_run:
            runner.cycle(self.cfg, log=logs.append)
        mock_run.assert_not_called()
        self.assertTrue(any('cleanup-incomplete' in m for m in logs))
        self.assertIn('cleanup-container-dead1',
                      self.st.cleanup_errors())
        blocked = self.st.get('blocked')
        self.assertEqual(blocked['reason'], 'cleanup-incomplete')

    def test_ledger_clears_and_cycle_recovers(self):
        self.st.record_cleanup_error('cleanup-network-zz', 'x', now=1.0)
        logs = []
        with patch.object(runner, 'docker', fake_docker_ok), \
                patch.object(runner, 'run') as mock_run:
            runner.cycle(self.cfg, log=logs.append)
        mock_run.assert_not_called()  # nothing queued
        self.assertTrue(any('nothing queued' in m for m in logs))

    def test_active_record_with_foreign_pid_blocks(self):
        self.st.enqueue('qa-1', SHA_A, 1.0, '2026-09-15')
        self.st.begin('qa-1', 4321, 2.0)
        logs = []
        with patch.object(runner, 'docker', fake_docker_ok), \
                patch.object(runner, '_pid_alive', return_value=True), \
                patch.object(runner, 'run') as mock_run:
            runner.cycle(self.cfg, log=logs.append)
        mock_run.assert_not_called()
        self.assertEqual(self.st.get('blocked')['reason'],
                         'active-run-conflict')


class StorageBoundTests(unittest.TestCase):
    """The run must refuse when the lane footprint exceeds its bound."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        self.st = qa_state.State(Path(self.cfg['state_dir']) / 'state.db')

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def _run_record(self, run_id='qa-1', sha=SHA_A):
        self.st.enqueue(run_id, sha, 1.0, '2026-09-15')
        return self.st.run(run_id)

    def test_storage_bound_blocks_with_named_infra(self):
        self.cfg['qa_storage_max_bytes'] = 1  # any usage exceeds it
        record = self._run_record()
        with patch.object(runner, 'docker', fake_docker_ok):
            runner.run(self.st, record, self.cfg, log=lambda m: None)
        rec = self.st.run('qa-1')
        self.assertEqual(rec['status'], 'finished')
        self.assertEqual(rec['outcome'], 'blocked')
        report = json.loads(Path(rec['report']).read_text())
        keys = [f['key'] for f in report['infrastructure_failures']]
        self.assertIn('preflight-storage-bound', keys)
        # A blocked run must not suppress redispatch of the SHA.
        self.assertIsNone(self.st.last_attempted_sha())

    def test_run_proceeds_when_under_bound(self):
        record = self._run_record()
        calls = []

        def fake(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'image' and args[1] == 'inspect':
                return Result('sha256:' + 'ab' * 32)
            return Result('')

        # build produces no binary -> inconclusive is fine here; the
        # point is the run was not storage-blocked.
        (Path(self.cfg['src_dir']) / SHA_A).mkdir(parents=True)
        with patch.object(runner, 'docker', fake):
            runner.run(self.st, record, self.cfg, log=lambda m: None)
        self.assertEqual(self.st.run('qa-1')['outcome'], 'inconclusive')
        self.assertIn('network', str(calls))  # builder net was created


class ReclaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        self.st = qa_state.State(Path(self.cfg['state_dir']) / 'state.db')
        self.src = Path(self.cfg['src_dir'])
        self.runs = Path(self.cfg['state_dir']) / 'runs'
        self.reports = Path(self.cfg['state_dir']) / 'reports'
        self.runs.mkdir()
        self.reports.mkdir()

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def _finished(self, run_id, sha, finished):
        self.st.enqueue(run_id, sha, finished - 10, '2026-09-15')
        self.st.begin(run_id, 1, finished - 9)
        self.st.finish(run_id, 'passed', sha,
                       str(self.runs / run_id / 'report.json'), finished)
        (self.runs / run_id).mkdir(exist_ok=True)
        (self.runs / run_id / 'report.json').write_text('{}')
        (self.reports / (run_id + '.json')).write_text('{}')

    def test_old_artifacts_reaped_but_pins_survive(self):
        self.cfg['runs_keep'] = 1
        self.cfg['src_keep'] = 1
        self._finished('qa-1', SHA_A, 100.0)
        self._finished('qa-2', SHA_B, 200.0)
        self._finished('qa-3', SHA_C, 300.0)
        for sha in (SHA_A, SHA_B, SHA_C):
            (self.src / sha).mkdir(exist_ok=True)
            (self.src / (sha + '.tar')).write_text('x')
        self.st.set_preserve('run', 'qa-1', True)
        self.st.set_preserve('sha', SHA_A, True)
        with patch.object(runner, 'docker', fake_docker_ok):
            runner.reclaim(self.st, self.cfg, log=lambda m: None)
        # newest run dir + pinned run survive; qa-2 is reaped
        self.assertTrue((self.runs / 'qa-3').is_dir())
        self.assertTrue((self.runs / 'qa-1').is_dir())
        self.assertFalse((self.runs / 'qa-2').exists())
        # newest sha + pinned sha survive; SHA_B reaped
        self.assertTrue((self.src / SHA_C).is_dir())
        self.assertTrue((self.src / SHA_A).is_dir())
        self.assertFalse((self.src / SHA_B).exists())
        self.assertFalse((self.src / (SHA_B + '.tar')).exists())
        # staged reports persist while records exist
        for rid in ('qa-1', 'qa-2', 'qa-3'):
            self.assertTrue((self.reports / (rid + '.json')).is_file())

    def test_queued_and_unreported_runs_never_reaped(self):
        self.cfg['runs_keep'] = 0
        self.st.enqueue('qa-9', SHA_A, 100.0, '2026-09-15')
        (self.src / SHA_A).mkdir()
        # interrupted run whose report was never persisted
        self.st.enqueue('qa-8', SHA_B, 90.0, '2026-09-15')
        self.st.begin('qa-8', 1, 95.0)
        self.st.interrupt('qa-8', 'died', 99.0)
        (self.runs / 'qa-8').mkdir()
        with patch.object(runner, 'docker', fake_docker_ok):
            runner.reclaim(self.st, self.cfg, log=lambda m: None)
        self.assertTrue((self.src / SHA_A).is_dir())
        self.assertTrue((self.runs / 'qa-8').is_dir())

    def test_foreign_files_untouched(self):
        (self.src / 'notasha').mkdir()
        (self.runs / 'other-dir').mkdir()
        with patch.object(runner, 'docker', fake_docker_ok):
            runner.reclaim(self.st, self.cfg, log=lambda m: None)
        self.assertTrue((self.src / 'notasha').is_dir())
        self.assertTrue((self.runs / 'other-dir').is_dir())

    def test_stale_image_pruned_recent_kept(self):
        self.cfg['images_keep'] = 1
        self._finished('qa-1', SHA_A, 100.0)
        self._finished('qa-2', SHA_B, 200.0)
        removed = []

        def fake(*args, timeout=120, check=True):
            if args[:2] == ('image', 'ls'):
                return Result(
                    'dcs-hwtest/controller|' + SHA_A + '|i1|119MB\n'
                    'dcs-hwtest/controller|' + SHA_B + '|i2|119MB\n'
                    'dcs-hwtest/plant|' + SHA_B + '|i3|116MB\n'
                    'rust|1.98.1-bookworm|b1|2.2GB\n'
                    'immich|v3|x9|2GB\n')
            if args[:2] == ('image', 'rm'):
                removed.append(args[2])
                return Result('')
            return Result('')

        with patch.object(runner, 'docker', fake):
            runner.reclaim(self.st, self.cfg, log=lambda m: None)
        self.assertIn('dcs-hwtest/controller:' + SHA_A, removed)
        self.assertNotIn('dcs-hwtest/controller:' + SHA_B, removed)
        self.assertFalse(any('immich' in r or 'x9' in r
                             for r in removed))


class TeardownVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        self.st = qa_state.State(Path(self.cfg['state_dir']) / 'state.db')

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def test_teardown_failure_enters_ledger_and_report(self):
        def fake(*args, timeout=120, check=True):
            if args[0] == 'ps':
                return Result('c1 qa-1\n')
            if args[:2] == ('network', 'ls'):
                return Result('')
            if args[0] == 'rm':
                return Result('', returncode=1, stderr='denied')
            return Result('')

        with patch.object(runner, 'docker', fake):
            failures = runner._teardown_rig('qa-1', lambda e, d=None: None,
                                            self.st)
        self.assertEqual(failures[0]['key'], 'cleanup-container-c1')
        self.assertIn('cleanup-container-c1', self.st.cleanup_errors())
        # a subsequent report carries the unresolved failure
        self.st.enqueue('qa-2', SHA_B, 1.0, '2026-09-15')
        self.st.begin('qa-2', 1, 2.0)
        with patch.object(runner, 'docker', fake):
            runner._persist_report(self.st, self.st.run('qa-2'), self.cfg,
                                   'inconclusive', None, None, [], [],
                                   [], log=lambda m: None)
        report = json.loads(
            Path(self.st.run('qa-2')['report']).read_text())
        keys = [f['key'] for f in report['infrastructure_failures']]
        self.assertIn('cleanup-container-c1', keys)


class NetpolicyTests(unittest.TestCase):
    def test_verify_reports_missing_rules(self):
        with patch.object(netpolicy, '_run',
                          lambda tool, args, runner:
                              Result('', returncode=1)):
            missing = netpolicy.verify()
        self.assertTrue(missing)
        self.assertTrue(any('DOCKER-USER' in m for m in missing))
        self.assertTrue(any('ip6tables' in m for m in missing))

    def test_apply_then_verify_with_fake_firewall(self):
        fw = {'chains': set(), 'rules': set()}

        def fake_run(tool, args, runner):
            if args[0] == '-N':
                fw['chains'].add((tool, args[1]))
            elif args[0] == '-F':
                fw['rules'] = {r for r in fw['rules']
                               if r[:2] != (tool, args[1])}
            elif args[0] == '-A':
                fw['rules'].add((tool, args[1], tuple(args[2:])))
            elif args[0] == '-C':
                ok = (tool, args[1], tuple(args[2:])) in fw['rules']
                return Result('', returncode=0 if ok else 1)
            elif args[0] == '-L':
                ok = (tool, args[1]) in fw['chains']
                return Result('', returncode=0 if ok else 1)
            return Result('')

        with patch.object(netpolicy, '_run', fake_run):
            netpolicy.apply()
            self.assertEqual(netpolicy.verify(), [])
            # a second apply is idempotent
            netpolicy.apply()
            self.assertEqual(netpolicy.verify(), [])
        # drops are installed for both QA interfaces
        self.assertTrue(any(r[2][-1] == 'DROP' and 'dcsqar' in r[2]
                            for r in fw['rules']))
        self.assertTrue(any(r[1] == 'INPUT' and r[2][-1] == 'DROP'
                            for r in fw['rules']))
        # builder gets 80/443 accept before its final drop
        accepts = [r for r in fw['rules']
                   if r[2][-1] == 'ACCEPT' and 'dcsqab' in r[2]]
        self.assertTrue(any('80,443' in r[2] for r in accepts))

    def test_rig_sourced_host_socket_attempt_is_dropped(self):
        # The rig bridge-to-host reachability rule the
        # qax-20260922-001, qax-20260922-005, and qax-20260923-001
        # runs demonstrated: a new connection a rig container opens
        # toward a host socket — host loopback, the LAN address, or
        # another stack's published port via the host — meets the
        # INPUT catch-all drop; only established replies to
        # host-originated connections pass. Recorded as the lane's
        # endpoint-placement rule (runner's endpoint_placement).
        checks = netpolicy.required_checks()
        self.assertIn(
            ('iptables', 'INPUT',
             ['-i', 'dcsqa+', '-m', 'conntrack', '--ctstate',
              'ESTABLISHED,RELATED', '-j', 'ACCEPT']), checks)
        self.assertIn(
            ('iptables', 'INPUT', ['-i', 'dcsqa+', '-j', 'DROP']),
            checks)
        self.assertIn(
            ('ip6tables', 'INPUT', ['-i', 'dcsqa+', '-j', 'DROP']),
            checks)


if __name__ == '__main__':
    unittest.main()
