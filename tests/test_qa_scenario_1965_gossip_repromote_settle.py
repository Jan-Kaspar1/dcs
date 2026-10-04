"""The 1965_gossip_repromote_settle leg's scenario unit coverage — the
feed fakes and TestCase classes for scenario_gossip_repromote_settle,
split per the one-module-per-leg convention (#940). The shared fakes
and helpers live in tests/qa_scenario_support.py; EXPECTED_CASES pins
this module's contribution to the suite's case coverage so a dropped
case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam
from test_qa_scenario_1900_demote_carry_settle import (
    DemoteCarryFeed, DemoteCarryPeer)


EXPECTED_CASES = frozenset({
    'GossipRepromoteSettleTests.test_registered',
    'GossipRepromoteSettleTests.test_clean_pair_passes_and_validates',
    'GossipRepromoteSettleTests.test_parked_receipt_reports_failed',
    'GossipRepromoteSettleTests.test_double_settle_reports_nondeterministic',
    'GossipRepromoteSettleTests.test_settle_without_apply_reports_failed',
    'GossipRepromoteSettleTests.test_divergent_verdict_reports_nondeterministic',
    'GossipRepromoteSettleTests.test_never_reconverges_reports_failed',
    'GossipRepromoteSettleTests.test_refused_repromote_reports_failed',
    'GossipRepromoteSettleTests.test_zombie_resurrection_reports_failed',
    'GossipRepromoteSettleTests.test_closed_window_still_resolves_once',
    'GossipRepromoteSettleTests.test_missed_window_restages_and_passes',
    'GossipRepromoteSettleTests.test_no_active_reports_failed',
    'GossipRepromoteSettleTests.test_unconverged_pair_reports_inconclusive',
    'GossipRepromoteSettleTests.test_unreachable_peer_reports_inconclusive',
    'GossipRepromoteSettleTests.test_diverging_digests_report_nondeterministic',
    'GossipRepromoteSettleTests.test_silent_judge_reports_unchecked',
    'GossipRepromoteSettleTests.test_two_runs_produce_identical_evidence',
})


class GossipFeed(DemoteCarryFeed):
    """A stubbed pair for the gossip-repromote-settle leg,
    arbitrating the field claim through a real ClaimPlantPeer: ctrl-a
    owns the field under TOKEN_A at launch, ctrl-b tracks. Every
    endpoint call is one scan on the called peer except POST
    /command, whose admission lands pending between scans.

    The leg's staging is the #708 reproduction on the rig's own scans:
    the receipted write and the holder's own demote land back to back,
    so the demote's boundary suspends the still-pending receipt
    `Accepted` — the peer never scans a command boundary in between.
    The gossip window is the sibling's next tracking pull: with
    `hold_peer_pull` the sibling's reads settle its sync posture but
    do not merge the demoted holder's post-admission log, so no
    covering adoption ever reaches the index before the in-window
    re-promote; clearing it stages the covered variant, where the
    sibling's pull lands first and the re-promote is no longer what
    settles the admission. Doctor flags stage each named defect."""

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
        # The gossip window: the sibling has not yet run the pull that
        # would carry the admission back covered.
        self.hold_peer_pull = True
        # The doctors staging each named defect.
        self.park_suspended = False    # the re-promoted holder never
                                       # re-queues the tail — parked
        self.double_settle = False     # the settle journals twice
        self.settle_no_apply = False   # the settle journals without
                                       # the write landing
        self.divergent = False         # the adopted record mints the
                                       # wrong verdict beside it
        self.no_reconverge = False     # the demoted holder never
                                       # reports promotable
        self.repromote_refused = False  # the in-window promote refused
        self.zombie_resurrect = False  # the successor re-applies the
                                       # stale suspended write
        self.miss_first_demote = False  # the first staged admission is
                                       # applied before the demote —
                                       # the restage path
        self._miss_armed = False
        self._doubled = False
        self._diverged = False

    @staticmethod
    def _staged(receipt):
        """Whether a receipt is one of the leg's staged suspended
        admissions — the exact reason the pass declares, never the
        contract probe's `-contract` sibling."""
        return receipt.get('reason') == 'gossip-repromote-settle'

    def _adopt(self, peer, other):
        """The tracking pull: the sync posture follows the source's
        field ownership, but while `hold_peer_pull` stands and the
        source's log still carries the staged admission suspended, the
        sibling's pull has not yet run — the gossip window the
        re-promote lands in. Once the source's own boundary settles the
        admission, the next pull carries the line's verdict across.
        Otherwise the source's log replaces the peer's own, the
        unreached suspended tail rides past the adopted window, and a
        source whose submission high-water sits behind this run's
        cannot regress the log."""
        if not self.no_tracking \
                and not (self.no_reconverge and peer.name == 'a'):
            peer.sync = 'tracking' \
                if other.role in ('active', 'promoting') \
                else 'orphaned'
        if self.hold_peer_pull and peer.name == 'b':
            staged = [receipt for receipt in other.receipts
                      if receipt.get('reason') == 'gossip-repromote-settle']
            if any('accepted' in receipt['outcome']
                   for receipt in staged):
                return
        if other.attempts < peer.attempts:
            return
        suspended = [receipt for receipt in peer.receipts
                     if 'accepted' in receipt['outcome']]
        peer.receipts = copy.deepcopy(other.receipts)
        peer.attempts = other.attempts
        peer.image = dict(other.image)
        for receipt in suspended:
            if any(self._key(carried) == self._key(receipt)
                   for carried in peer.receipts):
                continue            # carried — the adopted copy
                                    # journals the line's verdict
            if receipt['index'] < peer.attempts:
                self._settle(peer, self._superseded(receipt))
            else:
                peer.receipts.append(receipt)
        self._observe(peer)
        if self.divergent and peer.name == 'b' \
                and not self._diverged:
            raced = [receipt for receipt in peer.receipts
                     if 'applied' in receipt['outcome']
                     and self._staged(receipt)]
            if raced:
                self._diverged = True
                # The defect: the adopted record mints a second,
                # divergent verdict for the same admission.
                self._mark(peer, {'command_settled': {
                    'receipt': self._superseded(raced[0])}})

    def _apply(self, peer):
        """The field-owning scan boundary: pending admissions apply
        and settle — the re-promoted holder's requeue lands here."""
        for receipt in peer.receipts:
            if 'accepted' not in receipt['outcome']:
                continue
            write = receipt['command']['write_value']
            if self.park_suspended and self._staged(receipt):
                continue        # the defect: the tail never re-queued,
                                # parked Accepted on the live active
            receipt['outcome'] = {'applied': {'tick': peer.tick}}
            self._settle(peer, receipt)
            if not self.settle_no_apply:
                peer.image[write['point']] = write['value']['bool']
            if self.double_settle and not self._doubled \
                    and self._staged(receipt):
                self._doubled = True
                self._mark(peer, {'command_settled': {
                    'receipt': dict(receipt)}})

    def _zombie(self, peer):
        """The finding's zombie half: the successor the switch promotes
        re-opens the settled admission as pending, so its first
        field-owning scan writes the older value over the newer one."""
        for receipt in peer.receipts:
            if self._staged(receipt) and 'accepted' not in receipt[
                    'outcome']:
                receipt['outcome'] = {'accepted': {
                    'apply_tick': peer.tick + 1}}

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('://', 1)[1].split(':')[0]
        peer = self._peers()[host.split('-', 1)[1]]
        if self.unreachable and peer.name == 'b':
            raise urllib.error.URLError('unreachable')
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if (method, route) == ('POST', '/command'):
            if self.miss_first_demote and peer.name == 'a' \
                    and not self._miss_armed \
                    and (body or {}).get('reason') \
                    == 'gossip-repromote-settle':
                # The one-scan window is the staging race: arm the
                # holder's own scan to beat the demote that follows.
                self.miss_first_demote = False
                self._miss_armed = True
            receipt = {'command': (body or {}).get('command'),
                       'actor': (body or {}).get('actor'),
                       'reason': (body or {}).get('reason'),
                       'index': peer.attempts,
                       'outcome': {'accepted': {
                           'apply_tick': peer.tick + 1}}}
            if peer.role != 'active':
                receipt['outcome'] = {'rejected': {'reason': {
                    'not_active': {}}}}
                self._settle(peer, receipt)
            else:
                peer.attempts += 1
                peer.receipts.append(receipt)
            # The wire answer is the admission's snapshot — later
            # scans settling the logged receipt do not rewrite it.
            return 200, copy.deepcopy(receipt)
        if (method, route) == ('POST', '/demote'):
            # The gate closes at the request boundary: the demote
            # suspends the still-pending queue before the quiesced
            # scans that follow it.
            if peer.role != 'active':
                self._raise(409, {'not_active': {}})
            if self._miss_armed and peer.name == 'a':
                # The window closed first: the holder's own scan
                # applied the admission before the demote landed.
                self._miss_armed = False
                self._advance(peer)
            peer.role = 'demoting'
            self._advance(peer, adopt=False)
            return 200, {'role': 'demoting'}
        if (method, route) == ('POST', '/promote'):
            if peer.role == 'active':
                self._raise(409, {'already_active': {}})
            if peer.sync not in ('tracking', 'orphaned') \
                    or (self.repromote_refused and peer.name == 'a'):
                self.repromote_refused = False
                self._raise(409, {'not_converged': {
                    'sync': {'unsynchronized': {}}}})
            other = self._other(peer)
            # The promotion boundary's final-sync transfer — the
            # sibling's log lands, while suspended receipts its window
            # never covered stay on this run's log, the unreached tail
            # the re-promoted holder re-queues at its own boundary —
            # and the claim preempts the standing owner.
            suspended = [receipt for receipt in peer.receipts
                         if 'accepted' in receipt['outcome']
                         and receipt['index'] >= other.attempts
                         and not any(
                             self._key(carried) == self._key(receipt)
                             for carried in other.receipts)]
            peer.receipts = copy.deepcopy(other.receipts) + [
                copy.deepcopy(receipt) for receipt in suspended]
            peer.attempts = max(peer.attempts, other.attempts)
            peer.image = dict(other.image)
            self._wire_claim(peer.token)
            if self.zombie_resurrect and peer.name == 'b':
                self._zombie(peer)
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
                sync = 'unsynchronized' \
                    if peer.sync == 'unsynchronized' or (
                        self.no_reconverge and peer.name == 'a') \
                    else peer.sync
                report['sync'] = {sync: {'aligned': peer.tick}}
            return 200, report
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': list(self.SIGNALS),
                         'components': []}
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
            return 200, [dict(entry) for entry in peer.journal
                         if entry['seq'] > since]
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class GossipRepromoteSettleTests(unittest.TestCase):
    """The gossip-window re-promote settle leg against the stubbed
    pair: a clean rig passes with identical digests and evidence — the
    admission suspended `Accepted` at its own holder's demote settles
    applied exactly once on each peer, a newer write on the same point
    settles after it, and the successor applies nothing — each
    doctored defect reports the named diagnostic, and an unreachable
    or unconverged rig reports inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_files = {
            'active': str(Path(self.tmp.name) / 'a-journal.jsonl'),
            'standby': str(Path(self.tmp.name) / 'b-journal.jsonl')}
        self.plant = ClaimPlantPeer()
        self.feed = GossipFeed(self.plant, self.journal_files)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': self.plant.address,
                'journal_files': self.journal_files,
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'GOSSIP_SETTLE', 2.0), \
                patch.object(scenarios, 'GOSSIP_AUDIT', 2.0), \
                patch.object(scenarios, 'GOSSIP_POLL', 0.001), \
                patch.object(scenarios, 'GOSSIP_WATCH', 0.001):
            return scenarios.scenario_gossip_repromote_settle(
                ctx or self._ctx())

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The same restored window as the repromote leg, ahead of the
        # command-across-promotion case and the peer-announce leg.
        self.assertEqual(
            order.index(
                scenarios.scenario_repromote_suspended_settle) + 1,
            order.index(scenarios.scenario_gossip_repromote_settle))
        self.assertEqual(
            order.index(scenarios.scenario_gossip_repromote_settle) + 1,
            order.index(scenarios.scenario_command_across_promotion))
        self.assertIs(
            verify.case_function('gossip-repromote-settle'),
            scenarios.scenario_gossip_repromote_settle)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('gossip-settle-signals.json',
                     'gossip-settle-pass-1.json',
                     'gossip-settle-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads((self.evidence / name).read_text())
                  for name in ('gossip-settle-pass-1.json',
                               'gossip-settle-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(passes[0]['digest']['suspended'], 'accepted')
        self.assertEqual(passes[0]['digest']['settle'], 'single')
        self.assertEqual(passes[0]['digest']['newer'], 'single')
        self.assertEqual(passes[0]['digest']['order'], 'ordered')
        self.assertEqual(passes[0]['digest']['successor'], 'single')
        self.assertEqual(passes[0]['digest']['roles'], 'restored')
        # The window stood open on the clean rig: the sibling's pull
        # had not covered the admission when the re-promote landed.
        self.assertEqual(passes[0]['pre_repromote']['window'], 'open')
        for passed in passes:
            # The suspended admission settled applied exactly once on
            # each peer — the re-promoted boundary plus the adopted
            # record — through the serving monitors and the durable
            # journals alike.
            window = passed['window']
            for name in ('active', 'standby'):
                self.assertEqual(
                    ['applied'],
                    [scenarios._outcome_key(receipt)
                     for receipt in window['journaled'][name]],
                    window)
                self.assertEqual(
                    ['applied'],
                    [scenarios._outcome_key(receipt)
                     for receipt in window['logged'][name]],
                    window)
                self.assertEqual(1, len(passed['durable'][name]),
                                 passed['durable'])
        # The re-promote claimed the field through the real wire
        # protocol.
        self.assertGreaterEqual(
            self.plant.requests.count('claim_writer'), 3)
        report.validate_scenario(record)

    def test_parked_receipt_reports_failed(self):
        self.feed.park_suspended = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'gossip-repromote-settle-failed'), record['detail'])
        self.assertIn('parked Accepted', record['detail'])
        report.validate_scenario(record)

    def test_double_settle_reports_nondeterministic(self):
        self.feed.double_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'gossip-repromote-settle-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_settle_without_apply_reports_failed(self):
        self.feed.settle_no_apply = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'gossip-repromote-settle-failed'), record['detail'])
        self.assertIn('application', record['detail'])
        report.validate_scenario(record)

    def test_divergent_verdict_reports_nondeterministic(self):
        self.feed.divergent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'gossip-repromote-settle-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_never_reconverges_reports_failed(self):
        self.feed.no_reconverge = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'gossip-repromote-settle-failed'), record['detail'])
        self.assertIn('never reached a promotable verdict',
                      record['detail'])
        report.validate_scenario(record)

    def test_refused_repromote_reports_failed(self):
        self.feed.repromote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'gossip-repromote-settle-failed'), record['detail'])
        self.assertIn('promote', record['detail'])
        report.validate_scenario(record)

    def test_zombie_resurrection_reports_failed(self):
        # The finding's zombie half: the successor the switch promotes
        # re-applies the stale suspended write over the newer value.
        self.feed.zombie_resurrect = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'gossip-repromote-settle-failed'), record['detail'])
        self.assertIn('stale suspended write', record['detail'])
        report.validate_scenario(record)

    def test_closed_window_still_resolves_once(self):
        # The peer's pull closed the gossip window before the
        # re-promote: the admission is carried covered, so the settle
        # rides the carry rather than the re-promoted boundary. The
        # exactly-once contract stands either way — which window the
        # rig admitted is evidence, never the verdict — and the case
        # says so.
        self.feed.hold_peer_pull = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passed = json.loads(
            (self.evidence / 'gossip-settle-pass-1.json').read_text())
        self.assertEqual(passed['pre_repromote']['window'], 'closed')
        self.assertEqual(passed['digest']['settle'], 'single')
        self.assertEqual(passed['digest']['successor'], 'single')
        self.assertIn('gossip window in 0 of 2 passes',
                      ' '.join(record['observations']))
        report.validate_scenario(record)

    def test_missed_window_restages_and_passes(self):
        self.feed.miss_first_demote = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passed = json.loads(
            (self.evidence / 'gossip-settle-pass-1.json').read_text())
        self.assertGreaterEqual(len(passed['attempts']), 2)
        self.assertEqual(passed['attempts'][0]['missed'],
                         'applied before the demote')
        report.validate_scenario(record)

    def test_no_active_reports_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active', record['detail'])
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

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'settle': 'single'}, {}, {'pass': 1}),
                       ({'settle': 'diverged'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_gossip_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'gossip-repromote-settle-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_silent_judge_reports_unchecked(self):
        with patch.object(scenarios, '_gossip_judge',
                          lambda *a, **k: 'single'):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'gossip-repromote-settle-unchecked'), record['detail'])
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
            feed = GossipFeed(plant, journal_files)
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