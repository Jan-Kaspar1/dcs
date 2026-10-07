"""The 2270_failover_proof_report leg's scenario unit coverage — the
feed fakes and TestCase classes for scenario_failover_proof_report,
split per the one-module-per-leg convention (#940). The shared fakes
and helpers live in tests/qa_scenario_support.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


class FailoverProofFeed:
    """A stubbed pair for the failover-proof-report leg in either role
    layout, journaling its own `role_changed` records into the real
    journal files the ctx names — the durable records the leg audits.
    ctrl-b carries the armed failover budget; ctrl-a is the unarmed
    duty peer whose stop/start is the lane's partition convention.
    `switched` models the pair after an earlier case's a->b switch.
    The doctored variants model a release whose served contract
    diverges from the record: `predating` serves no failover
    accounting at all, `unarmed_gate` serves it on the unarmed peer,
    `suppress_window` drops it inside the miss window, `never_fires`
    lets the miss run pass the budget, `fire_offset` fires off the
    armed boundary, `voided_early` lowers the standing proof inside
    it, and `failover_origin`/`failover_actor` misattribute the
    automatic record."""

    def __init__(self, journal_files, budget=3, switched=False,
                 predating=False, unarmed_gate=False,
                 suppress_window=False, never_fires=False,
                 fire_offset=0, voided_early=False,
                 failover_origin='failover', failover_actor=None):
        self.journal_files = journal_files
        self.budget = budget
        self.fire_at = budget + fire_offset
        self.predating = predating
        self.unarmed_gate = unarmed_gate
        self.suppress_window = suppress_window
        self.never_fires = never_fires
        self.voided_early = voided_early
        self.failover_origin = failover_origin
        self.failover_actor = failover_actor
        self.tick = 0
        self.role = {'a': 'standby' if switched else 'active',
                     'b': 'active' if switched else 'standby'}
        self.a_down = False
        self.misses = 0
        self.converged = True
        self.pending_origin = 'request'
        self.seq = {'a': 1, 'b': 1}
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
        return {'from': frm, 'to': to, 'origin': 'request'}

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
        """The checkpoint-source partition: ctrl-a's monitor goes
        silent — its checkpoint serving and every other endpoint
        with it."""
        if name == 'active':
            self.a_down = True

    def start_controller(self, name):
        """The returned duty peer's warm resume: a launched active
        whose startup claim meets the promoted peer's live claim
        exits FieldClaimFailed, while the demotion's yielded claim
        lets the same resume take the field."""
        if name == 'active':
            if self.role['b'] in ('promoting', 'active'):
                return      # the start ran; the refused startup
                            # claim exited the process — still down
            self.a_down = False
            self.role['a'] = 'active'

    def _report(self, peer):
        role = self.role[peer]
        report = {'role': role, 'tick': self.tick, 'sync': None,
                  'field_claim': 'held'}
        if role == 'standby':
            if peer == 'b' and self.a_down:
                report['sync'] = {
                    'degraded': {'detail': 'fetch failed'}}
            else:
                report['sync'] = {
                    'tracking': {'aligned': self.tick}}
        elif role != 'active':
            report['sync'] = {'degraded': {'detail': 'fetch failed'}}
        armed = peer == 'b' or self.unarmed_gate
        suppressed = peer == 'b' and self.suppress_window \
            and role == 'standby' and self.misses >= 1
        if armed and not self.predating and not suppressed:
            report['failover'] = {
                'converged': self.converged,
                'misses': self.misses if peer == 'b' else 0,
                'budget': self.budget}
        return report

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        peer = self.peer_for[host]
        if peer == 'a' and self.a_down:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            # One scan per poll: the pending transition settles and
            # the armed standby counts a checkpoint miss toward its
            # failover budget while the source is partitioned.
            if self.role[peer] == 'demoting':
                self._journal(peer, self._request_change(
                    peer, 'demoting', 'standby'))
                self.role[peer] = 'standby'
                if peer == 'b':
                    self.misses = 0
                    self.converged = True
            elif self.role[peer] == 'promoting':
                change = {'from': 'promoting', 'to': 'active',
                          'origin': self.pending_origin}
                if self.pending_origin != 'request' \
                        and self.failover_actor is not None:
                    change['actor'] = self.failover_actor
                self._journal(peer, change)
                self.role[peer] = 'active'
            elif peer == 'b' and self.a_down \
                    and self.role['b'] == 'standby':
                self.misses += 1
                if self.voided_early and self.misses >= 2:
                    self.converged = False
                if self.misses > self.budget:
                    self.converged = False
                if not self.never_fires \
                        and self.misses >= self.fire_at:
                    self.pending_origin = self.failover_origin
                    self._journal(peer, self._failover_change(
                        'standby', 'promoting'))
                    self.role['b'] = 'promoting'
            return 200, self._report(peer)
        if (method, route) == ('POST', '/demote'):
            if self.role[peer] != 'active':
                self._refuse(url)
            self.role[peer] = 'demoting'
            self._journal(peer, self._request_change(
                peer, 'active', 'demoting'))
            return 200, {'role': 'demoting', 'tick': self.tick}
        if (method, route) == ('POST', '/promote'):
            if self.role[peer] != 'standby':
                self._refuse(url)
            self.pending_origin = 'request'
            self.role[peer] = 'promoting'
            self._journal(peer, self._request_change(
                peer, 'standby', 'promoting'))
            return 200, {'role': 'promoting', 'tick': self.tick}
        raise AssertionError('unexpected request %s %s' % (method, url))


class FailoverProofReportTests(unittest.TestCase):
    """scenario_failover_proof_report partitions the armed standby's
    checkpoint source and reads its served /role through the degraded
    window: the miss accounting k of the armed budget N beside the
    standing proof, the armed gate's self-promotion at the N-th miss
    journaled origin=failover, the unarmed peer's absent accounting
    field, the pre-field payload's unchanged load, the launch layout
    restored, and two passes producing identical digests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, lever=True, **feed_args):
        journals = {
            'active': str(self.evidence / 'journal-a.jsonl'),
            'standby': str(self.evidence / 'journal-b.jsonl')}
        self.feed = FailoverProofFeed(journals, **feed_args)
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'journal_files': journals,
               'failover_misses': self.feed.budget,
               'stop_controller': self.feed.stop_controller,
               'start_controller': self.feed.start_controller}
        if not lever:
            ctx.pop('stop_controller')
            ctx.pop('start_controller')
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'PROOF_POLL', 0.001):
            return scenarios.scenario_failover_proof_report(ctx)

    def _pass_evidence(self, number):
        return json.loads(
            (self.evidence
             / ('failover-proof-report-pass-%d.json' % number))
            .read_text())

    def test_two_identical_digest_passes(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        first, second = self._pass_evidence(1), self._pass_evidence(2)
        self.assertEqual(first['digest'], second['digest'])
        self.assertEqual(first['digest'], {
            'baseline': 'served', 'unarmed': 'absent',
            'pre_field': 'unchanged',
            'window': 'k-of-n-proof-standing',
            'boundary': 'fired-at-budget',
            'record': 'failover-origin',
            'restore': 'launch-layout'})
        # The served accounting climbed the miss run beside the
        # standing proof, and the armed boundary report fired at the
        # declared budget with the proof standing.
        misses = [row['misses'] for row in first['window']
                  if row['role'] == 'standby'
                  and row['sync'] == 'degraded']
        self.assertEqual(misses, list(range(1, self.feed.budget)))
        self.assertTrue(all(row['converged'] is True
                            for row in first['window']
                            if row['role'] == 'standby'))
        self.assertEqual(first['boundary']['failover']['misses'],
                         self.feed.budget)
        self.assertTrue(first['boundary']['failover']['converged'])
        self.assertIn(first['boundary']['role'],
                      ('promoting', 'active'))
        # The durable record carries the failover-origin walk, no
        # actor — the promotion the leg observed, recorded from
        # scratch rather than bare-asserted.
        pairs = [row[2:4] for row in first['journal']]
        self.assertEqual(pairs, [['standby', 'promoting'],
                                 ['promoting', 'active']])
        self.assertTrue(all(row[4] == 'failover'
                            for row in first['journal']))
        self.assertTrue(all('actor' not in row[5]
                            for row in first['journal']))
        # The unarmed peer's report omitted the accounting field —
        # the record's no-bare-flag-without-its-bound — and the
        # pre-field payload the consumer reconstructs loads
        # unchanged.
        self.assertNotIn('failover', first['unarmed'])
        self.assertIsNone(first['pre_field']['decoded']['failover'])
        self.assertEqual(first['pre_field']['reserialized'],
                         first['pre_field']['payload'])

    def test_switched_entry_restores_before_running(self):
        record = self.run_scenario(switched=True)
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        self.assertEqual(self.feed.role['a'], 'active')
        self.assertEqual(self.feed.role['b'], 'standby')

    def test_predating_release_is_inconclusive(self):
        """A staged release whose armed peer serves no `failover`
        accounting predates the contract — the leg reports
        inconclusive rather than false-red."""
        record = self.run_scenario(predating=True)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])
        report.validate_scenario(record)

    def test_missing_partition_lever_is_inconclusive(self):
        record = self.run_scenario(lever=False)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('partition lever', record['detail'])
        report.validate_scenario(record)

    def test_suppressed_evidence_fails(self):
        """The doctored negative: an armed report that drops the
        failover accounting inside the miss window fails the run —
        suppressed evidence can never produce a green pass."""
        record = self.run_scenario(suppress_window=True)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('failover-proof-report-failed',
                      record['detail'])
        self.assertIn('accounting', record['detail'])
        report.validate_scenario(record)

    def test_unarmed_serving_accounting_fails(self):
        record = self.run_scenario(unarmed_gate=True)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('bare flag', record['detail'])
        report.validate_scenario(record)

    def test_never_firing_gate_fails(self):
        record = self.run_scenario(never_fires=True)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('budget', record['detail'])
        report.validate_scenario(record)

    def test_off_budget_fire_fails(self):
        record = self.run_scenario(fire_offset=1)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('armed boundary', record['detail'])
        report.validate_scenario(record)

    def test_request_origin_record_fails(self):
        record = self.run_scenario(failover_origin='request')
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('origin', record['detail'])
        report.validate_scenario(record)

    def test_failover_record_with_actor_fails(self):
        record = self.run_scenario(failover_actor='operator-9')
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('operator actor', record['detail'])
        report.validate_scenario(record)

    def test_pre_field_payload_loads_unchanged(self):
        """The consumer's reconstruction under the served contract's
        optional-field convention: a payload shaped before the
        failover field existed decodes the absent accounting as no
        accounting — never a stood proof — and re-serves unchanged."""
        payload = {'role': 'standby', 'tick': 9,
                   'sync': {'tracking': {'aligned': 7}},
                   'field_claim': 'held'}
        decoded = scenarios._decode_role_report(payload)
        self.assertIsNone(decoded['failover'])
        self.assertEqual(
            scenarios._reserialize_role_report(decoded), payload)


if __name__ == '__main__':
    unittest.main()
