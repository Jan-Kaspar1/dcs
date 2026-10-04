"""The 1980_command_across_promotion leg's scenario unit coverage — the
feed fakes and TestCase classes for scenario_command_across_promotion,
split per the one-module-per-leg convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam
from test_qa_scenario_1900_demote_carry_settle import DemoteCarryFeed


EXPECTED_CASES = frozenset({
    'CommandAcrossPromotionTests.test_registered',
    'CommandAcrossPromotionTests.test_clean_pair_passes_and_validates',
    'CommandAcrossPromotionTests.test_refused_resubmission_reports_failed',
    'CommandAcrossPromotionTests.test_never_active_reports_inconclusive',
    'CommandAcrossPromotionTests.test_double_settlement_reports_failed',
    'CommandAcrossPromotionTests.test_replayed_history_reports_failed',
    'CommandAcrossPromotionTests.test_reordered_events_report_failed',
    'CommandAcrossPromotionTests.test_drifted_attribution_reports_failed',
    'CommandAcrossPromotionTests.test_no_translatable_command_reports_inconclusive',
    'CommandAcrossPromotionTests.test_pre_settlement_not_applied_reports_failed',
    'CommandAcrossPromotionTests.test_unconverged_pair_reports_inconclusive',
    'CommandAcrossPromotionTests.test_unreachable_peer_reports_inconclusive',
    'CommandAcrossPromotionTests.test_missing_journal_files_reports_inconclusive',
    'CommandAcrossPromotionTests.test_roles_not_restored_reports_failed',
    'CommandAcrossPromotionTests.test_two_runs_produce_identical_evidence',
})


COMPONENT = 'sequencer:seq-1'


class AcrossPromotionFeed(DemoteCarryFeed):
    """A stubbed pair for the command-across-promotion leg,
    arbitrating the field claim through a real ClaimPlantPeer: ctrl-a
    owns the field under TOKEN_A at launch, ctrl-b tracks. Every
    endpoint call is one scan on the called peer except POST
    /command, whose admission lands pending between scans.

    The served registry declares one kind-declared `advance` command on
    the sequencer instance, so the leg's schema-driven pick reaches the
    receipted path, and every applied invocation also emits the
    instance's `step_completed` event — the emitted record the
    continuity clauses read. Doctor flags stage each named defect."""

    SCHEMA = {
        'interfaces': [
            {'name': COMPONENT,
             'interface': {
                 'kind': 'sequencer',
                 'commands': [
                     {'name': 'advance', 'adapted': 'declared',
                      'request': [{'name': 'count', 'kind': 'int'}]},
                 ],
             }},
        ],
    }

    def __init__(self, plant, journal_files=None):
        super().__init__(plant)
        for peer, key in ((self.a, 'active'), (self.b, 'standby')):
            path = (journal_files or {}).get(key)
            if path is not None:
                path = Path(path)
                path.write_text(
                    json.dumps({'run_boundary': {'run': 1,
                                                 'tick': 0}}) + '\n')
                self.journal_paths[peer.name] = path
        self.a.sync = 'tracking'
        # The doctors staging each named defect.
        self.refuse_post = False       # the promoted peer refuses the
                                       # resubmitted command
        self.double_settle = False     # a settlement journals twice
        self.replay_history = False    # the promoted peer replays the
                                       # demoted peer's whole record
        self.reorder_events = False    # the promoted peer's record
                                       # steps a tick backward
        self.attribution_drift = False  # the promoted peer's record
                                       # drifts off the pre-promotion
                                       # attribution
        self.reject_pre = False        # the pre-promotion submission
                                       # settles refused
        self.never_active = False      # the promoted peer never
                                       # settles into the active role
        self.no_restore = False        # the pair never lands back on
                                       # its pre-scenario roles
        self.no_commands = False       # the served registry declares no
                                       # translatable command
        self._doubled = False

    def _settled_once(self, peer, receipt):
        """One boundary's settlement journaling — the recorder's
        outcome diff, with the leg's double-apply doctor."""
        self._settle(peer, receipt)
        if self.double_settle and not self._doubled:
            self._doubled = True
            self._mark(peer, {'command_settled': {'receipt':
                                                 dict(receipt)}})

    def _emit(self, peer, receipt):
        """The invocation's emitted record — the instance's
        `step_completed` event, attributed to the component that
        produced it."""
        command = receipt['command'].get('invoke') or {}
        component = command.get('component')
        if component is None:
            return
        self._mark(peer, {'event_emitted': {'event': {
            'event': 'step_completed',
            'component': ('sequencer:seq-2' if getattr(self, '_drift',
                                                        False)
                          else component),
            'fields': {'step': 1}}}})

    def _apply(self, peer):
        """The field-owning scan boundary: pending admissions apply and
        settle, each invocation emitting its component's event."""
        for receipt in peer.receipts:
            if 'accepted' not in receipt['outcome']:
                continue
            if self.reject_pre and str(receipt.get('actor') or '').endswith(
                    '-pre'):
                receipt['outcome'] = {'rejected': {'reason': {
                    'unavailable': {}}}}
                self._settled_once(peer, receipt)
                continue
            receipt['outcome'] = {'applied': {'tick': peer.tick}}
            self._settled_once(peer, receipt)
            self._emit(peer, receipt)
            if self.reorder_events and peer.name == 'b':
                # The emitted record stamped before the settlement it
                # follows — the tick order the continuity clause reads.
                peer.journal[-1]['tick'] = max(
                    0, peer.tick - 1)
                self._rewrite(peer, peer.journal[-1])

    def _rewrite(self, peer, entry):
        """Mirror a journal entry's corrected tick into the durable
        file the leg's durable audit reads."""
        path = self.journal_paths.get(peer.name)
        if path is None:
            return
        lines = path.read_text().splitlines()
        lines[-1] = json.dumps(
            {'entry': {'seq': entry['seq'], 'tick': entry['tick'],
                       'event': entry['event']}})
        path.write_text('\n'.join(lines) + '\n')

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('://', 1)[1].split(':')[0]
        peer = self._peers()[host.split('-', 1)[1]]
        if self.unreachable and peer.name == 'b':
            raise urllib.error.URLError('unreachable')
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if (method, route) == ('POST', '/command'):
            actor = (body or {}).get('actor') or ''
            receipt = {'command': (body or {}).get('command'),
                       'actor': actor,
                       'reason': (body or {}).get('reason'),
                       'index': peer.attempts,
                       'outcome': {'accepted': {
                           'apply_tick': peer.tick + 1}}}
            if peer.role != 'active' or (
                    self.refuse_post and actor.endswith('-post')):
                receipt['outcome'] = {'rejected': {'reason': {
                    'not_active': {}}}}
                self._settle(peer, receipt)
            else:
                peer.attempts += 1
                peer.receipts.append(receipt)
            return 200, copy.deepcopy(receipt)
        if (method, route) == ('POST', '/demote'):
            if peer.role != 'active':
                self._raise(409, {'not_active': {}})
            peer.role = 'demoting'
            self._advance(peer, adopt=False)
            return 200, {'role': 'demoting'}
        if (method, route) == ('POST', '/promote'):
            if peer.role == 'active':
                self._raise(409, {'already_active': {}})
            if peer.sync not in ('tracking', 'orphaned') \
                    or (self.no_restore and peer.name == 'a'):
                self._raise(409, {'not_converged': {
                    'sync': {'unsynchronized': {}}}})
            other = self._other(peer)
            peer.receipts = copy.deepcopy(other.receipts)
            peer.attempts = other.attempts
            peer.image = dict(other.image)
            self._wire_claim(peer.token)
            if self.replay_history and peer.name == 'b' \
                    and not getattr(self, '_replayed', False):
                # The defect: the promoted peer's journal carries the
                # demoted peer's whole settlement history replayed onto
                # it rather than one adopted record per admission.
                self._replayed = True
                for entry in other.journal:
                    self._mark(peer, copy.deepcopy(entry['event']))
            if self.attribution_drift and peer.name == 'b':
                # The defect: the promoted peer emits the command's
                # record under a sibling instance — the component
                # attribution the switch must not let drift.
                self.attribution_drift = False
                self._drift = True
            if self.never_active and peer.name == 'b':
                self.never_active = False
                peer.role = 'standby'      # the promotion never lands
                return 200, {'role': 'standby'}
            peer.role = 'promoting'
            return 200, {'role': 'promoting'}
        self._advance(
            peer,
            adopt=not (method == 'POST' and route == '/promote'))
        if (method, route) == ('GET', '/role'):
            role = 'standby' if peer.name == 'a' and self.no_active \
                else peer.role
            report = {'role': role, 'tick': peer.tick}
            if role == 'standby':
                report['sync'] = {'unsynchronized': {}} \
                    if peer.sync == 'unsynchronized' \
                    else {peer.sync: {'aligned': peer.tick}}
            return 200, report
        if (method, route) == ('GET', '/schema'):
            schema = copy.deepcopy(self.SCHEMA)
            if self.no_commands:
                schema['interfaces'] = []
            return 200, schema
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': list(self.SIGNALS),
                         'components': [{'name': COMPONENT,
                                         'kind': 'sequencer'}]}
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': peer.tick, 'points': [
                {'point': point, 'direction': 'in',
                 'sample': {'value': {'bool': peer.image.get(point,
                                                           False)},
                            'quality': 'good', 'tick': peer.tick}}
                for point in self.POINTS]}
        if (method, route) == ('GET', '/checkpoint'):
            return 200, {'receipts': list(peer.receipts),
                         'command_admission': {
                             'attempts': peer.attempts}}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(peer.receipts)
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if '=' in query else 0
            return 200, [copy.deepcopy(entry) for entry in peer.journal
                         if entry['seq'] > since]
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class CommandAcrossPromotionTests(unittest.TestCase):
    """The command-across-promotion leg against the stubbed pair: a
    clean rig passes with identical evidence — the declared command
    settles once on each peer, the promoted peer's record continues
    the pre-promotion entries in tick order, and the pair restores its
    roles — each doctored defect reports the named failure, and an
    unreachable, unconverged, or un-promoted rig reports
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_files = {
            'active': str(Path(self.tmp.name) / 'a-journal.jsonl'),
            'standby': str(Path(self.tmp.name) / 'b-journal.jsonl')}
        self.plant = ClaimPlantPeer()
        self.feed = AcrossPromotionFeed(self.plant, self.journal_files)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self, journal_files=None):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': self.plant.address,
                'journal_files': (journal_files
                                  if journal_files is not None
                                  else self.journal_files),
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'ACROSS_SETTLE', 2.0), \
                patch.object(scenarios, 'ACROSS_AUDIT', 2.0), \
                patch.object(scenarios, 'ACROSS_POLL', 0.001), \
                patch.object(scenarios, 'ACROSS_WATCH', 0.001):
            return scenarios.scenario_command_across_promotion(
                ctx or self._ctx())

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The same restored window as the gossip and
        # settled-receipt-arbitration legs, ahead of the peer-announce
        # case and the tune case's a->b switch.
        self.assertEqual(
            order.index(scenarios.scenario_settled_receipt_arbitration)
            + 1,
            order.index(
                scenarios.scenario_command_across_promotion))
        self.assertEqual(
            order.index(
                scenarios.scenario_command_across_promotion) + 1,
            order.index(scenarios.scenario_peer_announce))
        self.assertIs(
            verify.case_function('command-across-promotion'),
            scenarios.scenario_command_across_promotion)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('across-promotion-roles-before.json',
                     'across-promotion-picked.json',
                     'across-promotion-pre-receipt.json',
                     'across-promotion-pre-settled.json',
                     'across-promotion-roles-promoted.json',
                     'across-promotion-post-receipt.json',
                     'across-promotion-post-settled.json',
                     'across-promotion-settlements.json',
                     'across-promotion-records.json',
                     'across-promotion-roles-after.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        # The schema-driven pick reached the declared command.
        picked = json.loads(
            (self.evidence / 'across-promotion-picked.json').read_text())
        self.assertEqual(picked['component'], COMPONENT)
        self.assertEqual(picked['command']['invoke']['command'], 'advance')
        # Both settlements are `applied`, one journal record each, and
        # the promoted peer's record continues the pre-promotion entry
        # with the component attribution unchanged.
        records = json.loads(
            (self.evidence / 'across-promotion-records.json').read_text())
        before = records['before']['standby']
        after = records['after']['standby']
        self.assertEqual(after[:len(before)], before)
        self.assertTrue(after)
        kinds = [record[1] for record in after]
        self.assertIn('command_settled', kinds)
        for entry in after:
            if entry[1] == 'event_emitted':
                self.assertTrue(entry[2].startswith(COMPONENT), entry)
        ticks = [record[0] for record in after]
        self.assertEqual(ticks, sorted(ticks))
        settlements = json.loads(
            (self.evidence / 'across-promotion-settlements.json')
            .read_text())
        for name, per_actor in settlements.items():
            for actor, records in per_actor.items():
                self.assertLessEqual(len(records), 1, (name, actor))
                for receipt in records:
                    self.assertEqual('applied',
                                     scenarios._outcome_key(receipt))
        self.assertEqual(len(settlements['active']
                             ['qa-across-promotion-pre']), 1)
        self.assertEqual(len(settlements['standby']
                             ['qa-across-promotion-post']), 1)
        # The promotion claimed the field through the real wire
        # protocol.
        self.assertGreaterEqual(
            self.plant.requests.count('claim_writer'), 3)
        report.validate_scenario(record)

    def test_refused_resubmission_reports_failed(self):
        self.feed.refuse_post = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('refused', record['detail'])
        report.validate_scenario(record)

    def test_never_active_reports_inconclusive(self):
        self.feed.never_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never reported active', record['detail'])
        report.validate_scenario(record)

    def test_double_settlement_reports_failed(self):
        self.feed.double_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('exactly one settlement', record['detail'])
        report.validate_scenario(record)

    def test_replayed_history_reports_failed(self):
        self.feed.replay_history = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('replayed onto the promoted peer', record['detail'])
        report.validate_scenario(record)

    def test_reordered_events_report_failed(self):
        self.feed.reorder_events = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tick order', record['detail'])
        report.validate_scenario(record)

    def test_drifted_attribution_reports_failed(self):
        self.feed.attribution_drift = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('attribution must not drift', record['detail'])
        report.validate_scenario(record)

    def test_no_translatable_command_reports_inconclusive(self):
        self.feed.no_commands = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no served declared command', record['detail'])
        report.validate_scenario(record)

    def test_pre_settlement_not_applied_reports_failed(self):
        self.feed.reject_pre = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pre-promotion declared command settled',
                      record['detail'])
        report.validate_scenario(record)

    def test_unconverged_pair_reports_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])
        report.validate_scenario(record)

    def test_unreachable_peer_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_missing_journal_files_reports_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(journal_files={}))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no journal files', record['detail'])
        report.validate_scenario(record)

    def test_roles_not_restored_reports_failed(self):
        self.feed.no_restore = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pre-scenario roles', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for index in (0, 1):
            plant = ClaimPlantPeer()
            journal_files = {
                'active': str(Path(self.tmp.name)
                              / ('a-' + str(index) + '.jsonl')),
                'standby': str(Path(self.tmp.name)
                               / ('b-' + str(index) + '.jsonl'))}
            feed = AcrossPromotionFeed(plant, journal_files)
            evidence = Path(self.tmp.name) / ('run' + str(index))
            evidence.mkdir()
            ctx = {'active': 'http://ctrl-a:1',
                   'standby': 'http://ctrl-b:2',
                   'plant': plant.address,
                   'journal_files': journal_files,
                   'evidence_dir': str(evidence)}
            try:
                record = self.run_scenario(ctx=ctx, feed=feed)
            finally:
                plant.close()
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()