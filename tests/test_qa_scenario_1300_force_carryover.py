"""The 1300_force_carryover leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_force_carryover, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'ForceCarryoverTests.test_registered_and_replayable',
    'ForceCarryoverTests.test_clean_feed_passes_and_validates',
    'ForceCarryoverTests.test_two_runs_produce_identical_records',
    'ForceCarryoverTests.test_mirrored_layout_passes',
    'ForceCarryoverTests.test_standby_never_reporting_the_force_fails',
    'ForceCarryoverTests.test_standby_sample_never_substituting_fails',
    'ForceCarryoverTests.test_promoted_peer_dropping_the_force_fails',
    'ForceCarryoverTests.test_release_never_settling_applied_fails',
    'ForceCarryoverTests.test_released_point_resubstituting_fails',
    'ForceCarryoverTests.test_unrestored_pair_fails',
    'ForceCarryoverTests.test_demoted_peer_never_retracking_fails',
    'ForceCarryoverTests.test_standby_never_converging_is_inconclusive',
})


class CarryoverPeer:
    """One endpoint of the carryover pair: role, tracking state, the
    force the adopted state holds, and its own receipt/journal log."""

    def __init__(self, name):
        self.name = name
        self.tick = 0
        self.role = 'standby'      # active | standby | demoting | promoting
        self.tracking = False
        self.force = None          # the forced value while a force stands
        self.last_value = False    # the held/stamped sample value
        self.released_once = False
        self.receipts = []
        self.journal = []
        self.next_seq = 1


class CarryoverPair:
    """A stubbed redundant pair for the force-carryover scenario: two
    monitor endpoints over one checkpoint line. The active's force
    applies at a scan boundary and lands on the line, the tracking
    peer's per-pull scans adopt it — badge and substituted stamp
    included — and demote/promote move the writer role, the demoted
    peer following its successor the way the announced-peer contract
    describes. Every call on the measurement channel is one completed
    scan. Fault flags stage each named failure the issue calls out."""

    def __init__(self):
        self.a = CarryoverPeer('a')
        self.b = CarryoverPeer('b')
        self.a.role = 'active'
        self.b.role = 'standby'
        self.b.tracking = True
        self.line_force = None   # the checkpoint-carried force set
        self.held = False        # p101-oos's held operator value
        # Fault injection for the named-failure cases.
        self.badge_omitted_on = set()       # snapshot.forces stays empty
        self.force_unseen_on = set()        # the sample never substitutes
        self.promoted_drops_on = set()      # the promoted peer loses the force
        self.release_never_applies = False  # the release stays accepted
        self.resubstitutes_on = set()       # the released point re-substitutes
        self.never_activates_on = set()     # the restore leg never re-activates
        self.never_retracks_on = set()      # the demoted peer stays unsynchronized

    def _advance(self, peer):
        """One completed scan on `peer`: pending role transitions
        settle, the tracking peer pulls the line's force set, and each
        accepted command whose apply_tick has arrived applies and
        journals its settlement."""
        peer.tick += 1
        if peer.role == 'demoting':
            peer.role = 'standby'
            # the demoted peer follows its successor — unless the
            # never_retracks_on fault holds it unsynchronized
            peer.tracking = peer.name not in self.never_retracks_on
        elif peer.role == 'promoting':
            peer.role = 'standby' \
                if peer.name in self.never_activates_on else 'active'
            if peer.name in self.promoted_drops_on:
                peer.force = None
                peer.last_value = self.held
        if peer.role != 'active' and peer.tracking:
            peer.force = self.line_force
            if peer.force is not None:
                peer.last_value = peer.force
        for receipt in peer.receipts:
            accepted = receipt['outcome'].get('accepted')
            if accepted is None or peer.tick < accepted['apply_tick']:
                continue
            command = receipt['command']
            if 'force_point' in command:
                value = command['force_point']['value']['bool']
                self.line_force = value
                peer.force = value
                peer.last_value = value
            elif 'unforce_point' in command:
                self.line_force = None
                peer.force = None
                peer.released_once = True
            elif 'write_value' in command:
                self.held = command['write_value']['value']['bool']
                peer.last_value = self.held
            if 'unforce_point' in command and self.release_never_applies:
                continue
            receipt['outcome'] = {'applied': {'tick': peer.tick}}
            peer.journal.append(
                {'seq': peer.next_seq, 'tick': peer.tick,
                 'event': {'command_settled':
                           {'receipt': dict(receipt)}}})
            peer.next_seq += 1

    def _oos_sample(self, peer):
        """Point 302's scan read: the forced value at Substituted while
        the adopted force stands, else the last stamp re-stamped Good
        — the held-value rule; the resubstitution fault holds the
        stamp at Substituted past release."""
        if peer.force is not None \
                and peer.name not in self.force_unseen_on:
            return {'value': {'bool': peer.force},
                    'quality': {'uncertain': 'substituted'},
                    'tick': peer.tick}
        quality = {'uncertain': 'substituted'} \
            if peer.name in self.resubstitutes_on \
            and peer.released_once else 'good'
        return {'value': {'bool': peer.last_value}, 'quality': quality,
                'tick': peer.tick}

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://pair', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('://', 1)[1].split(':')[0]
        peer = self.a if host == 'ctrl-a' else self.b
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self._advance(peer)
        if (method, route) == ('GET', '/role'):
            report = {'role': peer.role, 'tick': peer.tick}
            if peer.role == 'standby':
                report['sync'] = {'tracking': {'aligned': peer.tick}} \
                    if peer.tracking else {'unsynchronized': {}}
            return 200, report
        if (method, route) == ('GET', '/signals'):
            return 200, {
                'points': [
                    {'point': 302, 'signal': 10302, 'name': 'p101-oos',
                     'direction': 'in', 'value_type': 'bool',
                     'writable': True}],
                'components': []}
        if (method, route) == ('GET', '/snapshot'):
            forces = [] if peer.force is None \
                or peer.name in self.badge_omitted_on \
                else [{'point': 302, 'value': {'bool': peer.force}}]
            return 200, {
                'tick': peer.tick, 'forces': forces,
                'points': [{'point': 302, 'direction': 'in',
                            'sample': self._oos_sample(peer)}]}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(peer.receipts)
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if '=' in query else 0
            return 200, [entry for entry in peer.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/command'):
            receipt = {'command': body['command'],
                       'outcome': {'accepted': {
                           'apply_tick': peer.tick + 1}},
                       'actor': body.get('actor')}
            peer.receipts.append(receipt)
            return 200, receipt
        if (method, route) == ('POST', '/demote'):
            if peer.role != 'active':
                self._raise(409, {'not_active': {}})
            peer.role = 'demoting'
            return 200, {'role': 'demoting'}
        if (method, route) == ('POST', '/promote'):
            if peer.role == 'active':
                self._raise(409, {'already_active': {}})
            if not peer.tracking:
                self._raise(409, {'not_converged':
                                  {'sync': {'unsynchronized': {}}}})
            peer.role = 'promoting'
            return 200, {'role': 'promoting'}
        raise AssertionError('unexpected request %s %s' % (method, url))


class ForceCarryoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.pair = CarryoverPair()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.pair.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'FORCE_DEADLINE', 0.5):
            return scenarios.scenario_force_carryover(ctx)

    def test_registered_and_replayable(self):
        self.assertIn(scenarios.scenario_force_carryover,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('force-carryover'),
                      scenarios.scenario_force_carryover)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        self.assertTrue(
            any('tracking standby' in note
                for note in record['observations']))
        self.assertTrue(
            any('restored' in note for note in record['observations']))

    def test_two_runs_produce_identical_records(self):
        first = self.run_scenario()
        pair, self.pair = self.pair, CarryoverPair()
        try:
            second = self.run_scenario()
        finally:
            self.pair = pair
        self.assertEqual(first, second)

    def test_mirrored_layout_passes(self):
        # The post-failover layout: ctrl-b owns the field, ctrl-a
        # follows through the announced-peer pull — the case runs the
        # same legs mirrored and restores ctrl-b active.
        self.pair.a.role = 'standby'
        self.pair.a.tracking = True
        self.pair.b.role = 'active'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.pair.b.role, 'active')
        report.validate_scenario(record)

    def test_standby_never_reporting_the_force_fails(self):
        self.pair.badge_omitted_on.add('b')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tracking standby never reported the force',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_standby_sample_never_substituting_fails(self):
        self.pair.force_unseen_on.add('b')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tracking standby never reported the force',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_promoted_peer_dropping_the_force_fails(self):
        self.pair.promoted_drops_on.add('b')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('the promoted peer lost',
                      record.get('detail', ''))
        self.assertIn('did not ride the checkpoint',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_release_never_settling_applied_fails(self):
        self.pair.release_never_applies = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('did not settle applied',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_released_point_resubstituting_fails(self):
        self.pair.resubstitutes_on.add('b')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('did not recover', record.get('detail', ''))
        self.assertIn('re-substituted', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unrestored_pair_fails(self):
        self.pair.never_activates_on.add('a')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not restored', record.get('detail', ''))
        report.validate_scenario(record)

    def test_demoted_peer_never_retracking_fails(self):
        # The original active re-activates but the demoted successor
        # stays unsynchronized — the launch role pair is not restored.
        self.pair.never_retracks_on.add('b')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not restored', record.get('detail', ''))
        report.validate_scenario(record)

    def test_standby_never_converging_is_inconclusive(self):
        self.pair.b.tracking = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking convergence',
                      record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
