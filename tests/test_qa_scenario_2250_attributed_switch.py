"""The 2250_attributed_switch leg's scenario unit coverage — the
feed fakes and TestCase classes for scenario_attributed_switch,
split per the one-module-per-leg convention (#940). The shared fakes
and helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case coverage so a
dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'AttributedSwitchTests.test_attributed_cycle_and_failover_pass',
    'AttributedSwitchTests.test_switched_entry_runs_the_cycle',
    'AttributedSwitchTests.test_missing_actor_fails',
    'AttributedSwitchTests.test_failover_read_as_request_fails',
    'AttributedSwitchTests.test_failover_carrying_actor_fails',
    'AttributedSwitchTests.test_restore_demote_refused_fails',
    'AttributedSwitchTests.test_unconverged_entry_fails',
})


class AttributedSwitchFeed:
    """A stubbed pair for the attributed-switch leg in either role
    layout, journaling its own `role_changed` records into the real
    journal files the ctx names — the durable records the leg audits.
    `switched` models the pair after an earlier case's a->b switch:
    ctrl-b settled active, ctrl-a tracking. `drop_actor` models a
    release that drops the declared actor off its request-origin
    entries; `failover_origin`/`failover_actor` model a
    misclassified automatic record — a failover journaling
    `origin: request` or carrying an operator actor."""

    def __init__(self, journal_files, switched=False, drop_actor=False,
                 failover_origin='failover', failover_actor=None,
                 refuse_restore_demote=False, unconverged=None):
        self.journal_files = journal_files
        self.unconverged = unconverged
        self.tick = 0
        self.role = {'a': 'standby' if switched else 'active',
                     'b': 'active' if switched else 'standby'}
        self.a_down = False
        self.misses = 0
        self.budget = 3
        self.drop_actor = drop_actor
        self.failover_origin = failover_origin
        self.failover_actor = failover_actor
        self.refuse_restore_demote = refuse_restore_demote
        self.seq = {'a': 1, 'b': 1}
        self.last_actor = {}
        self.peer_for = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}
        self.journal_for = {'a': 'active', 'b': 'standby'}
        for path in journal_files.values():
            Path(path).write_text(
                '{"run_boundary":{"run":1,"tick":0}}\n')

    def _journal(self, peer, change):
        self.tick += 1
        entry = {'seq': self.seq[peer], 'tick': self.tick,
                 'event': {'role_changed': change}}
        self.seq[peer] += 1
        path = self.journal_files[self.journal_for[peer]]
        with open(path, 'a') as handle:
            handle.write(json.dumps({'entry': entry}) + '\n')

    def _request_change(self, peer, frm, to):
        change = {'from': frm, 'to': to, 'origin': 'request'}
        actor = self.last_actor.get(peer)
        if not self.drop_actor and actor is not None:
            change['actor'] = actor
        return change

    def _failover_change(self, frm, to):
        change = {'from': frm, 'to': to,
                  'origin': self.failover_origin}
        if self.failover_actor is not None:
            change['actor'] = self.failover_actor
        return change

    def _refuse(self, url):
        error = urllib.error.HTTPError(url, 409, 'conflict', {}, None)
        error.close()
        raise error

    def stop_controller(self, name):
        """The severed-owner induction: ctrl-a's monitor goes silent —
        its checkpoint serving and every other endpoint with it."""
        if name == 'active':
            self.a_down = True

    def start_controller(self, name):
        """The returned duty peer's warm resume: a launched active
        whose startup claim meets the promoted peer's live claim
        exits FieldClaimFailed — the conditional grant's
        live-incumbent refusal — while the demotion's yielded claim
        lets the same resume take the field."""
        if name == 'active':
            if self.role['b'] in ('promoting', 'active'):
                return      # the start ran; the refused startup
                            # claim exited the process — still down
            self.a_down = False
            self.role['a'] = 'active'

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route = path.partition('?')[0]
        peer = self.peer_for[host]
        if peer == 'a' and self.a_down:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            # One scan per poll: the pending transition settles and
            # the armed standby counts a checkpoint miss toward its
            # failover budget while the owner is severed.
            if self.role[peer] == 'demoting':
                self._journal(peer, self._request_change(
                    peer, 'demoting', 'standby'))
                self.role[peer] = 'standby'
            elif self.role[peer] == 'promoting':
                self._journal(peer, self._request_change(
                    peer, 'promoting', 'active'))
                self.role[peer] = 'active'
            elif peer == 'b' and self.a_down:
                self.misses += 1
                if self.misses >= self.budget \
                        and self.role['b'] == 'standby':
                    self._journal(peer, self._failover_change(
                        'standby', 'promoting'))
                    self._journal(peer, self._failover_change(
                        'promoting', 'active'))
                    self.role['b'] = 'active'
            report = {'role': self.role[peer], 'tick': self.tick}
            if self.role[peer] == 'standby' \
                    and peer != self.unconverged:
                report['sync'] = {'tracking': {'aligned': self.tick}}
            return 200, report
        if (method, route) == ('POST', '/demote'):
            if self.role[peer] != 'active':
                self._refuse(url)
            if self.refuse_restore_demote and peer == 'b' \
                    and self.a_down:
                self._refuse(url)
            self.last_actor[peer] = (body or {}).get('actor')
            self.role[peer] = 'demoting'
            self._journal(peer, self._request_change(
                peer, 'active', 'demoting'))
            return 200, {'role': 'demoting', 'tick': self.tick}
        if (method, route) == ('POST', '/promote'):
            if self.role[peer] != 'standby':
                self._refuse(url)
            self.last_actor[peer] = (body or {}).get('actor')
            self.role[peer] = 'promoting'
            self._journal(peer, self._request_change(
                peer, 'standby', 'promoting'))
            return 200, {'role': 'promoting', 'tick': self.tick}
        raise AssertionError('unexpected request %s %s' % (method, url))


class AttributedSwitchTests(unittest.TestCase):
    """scenario_attributed_switch cycles the pair through attributed
    switches in both directions, severs the owner for the automatic
    half, and restores the launch roles — the durable journal must
    name the declared actor on every requested transition and
    origin=failover with no actor on the self-promotion."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, switched=False, drop_actor=False,
                     failover_origin='failover', failover_actor=None,
                     refuse_restore_demote=False, unconverged=None):
        journals = {
            'active': str(self.evidence / 'journal-a.jsonl'),
            'standby': str(self.evidence / 'journal-b.jsonl')}
        self.feed = AttributedSwitchFeed(
            journals, switched=switched, drop_actor=drop_actor,
            failover_origin=failover_origin,
            failover_actor=failover_actor,
            refuse_restore_demote=refuse_restore_demote,
            unconverged=unconverged)
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'journal_files': journals,
               'failover_misses': self.feed.budget,
               'pair_token': 'qa-pair',
               'stop_controller': self.feed.stop_controller,
               'start_controller': self.feed.start_controller}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, 'ATTRIBUTION_POLL', 0.001), \
                patch.object(scenarios, 'ATTRIBUTION_SETTLE',
                             0.05 if unconverged
                             else scenarios.ATTRIBUTION_SETTLE), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001):
            return scenarios.scenario_attributed_switch(ctx)

    def test_attributed_cycle_and_failover_pass(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.role['a'], 'active')
        self.assertEqual(self.feed.role['b'], 'standby')
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_switched_entry_runs_the_cycle(self):
        record = self.run_scenario(switched=True)
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.role['a'], 'active')
        self.assertEqual(self.feed.role['b'], 'standby')
        report.validate_scenario(record)

    def test_missing_actor_fails(self):
        record = self.run_scenario(drop_actor=True)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('actor', record['detail'])
        report.validate_scenario(record)

    def test_failover_read_as_request_fails(self):
        record = self.run_scenario(failover_origin='request')
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('origin', record['detail'])
        report.validate_scenario(record)

    def test_failover_carrying_actor_fails(self):
        record = self.run_scenario(failover_actor='operator-9')
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('operator actor', record['detail'])
        report.validate_scenario(record)

    def test_restore_demote_refused_fails(self):
        """A promoted peer that refuses the restore demote leaves its
        live claim standing — the duty peer's resumed startup grant
        would exit FieldClaimFailed against it, so the leg reports
        the refused demotion rather than starting into it."""
        record = self.run_scenario(refuse_restore_demote=True)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('restore demote', record['detail'])
        report.validate_scenario(record)

    def test_unconverged_entry_fails(self):
        """The leg's precondition is a converged pair, not merely a
        settled one: the failover leg it orders behind ends at the
        promotion, so a tracking peer that never converges must fail
        the leg at its entry wait rather than demoting into the
        monitor's no_tracking_source refusal."""
        record = self.run_scenario(unconverged='b')
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tracking', record['detail'])
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
