"""The 1900_demote_carry_settle leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_demote_carry_settle, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'DemoteCarrySettleTests.test_registered',
    'DemoteCarrySettleTests.test_clean_pair_passes_and_validates',
    'DemoteCarrySettleTests.test_superseded_then_applied_reports_nondeterministic',
    'DemoteCarrySettleTests.test_dropped_carry_reports_failed',
    'DemoteCarrySettleTests.test_owner_never_demotes_reports_failed',
    'DemoteCarrySettleTests.test_refused_promote_reports_failed',
    'DemoteCarrySettleTests.test_no_active_reports_failed',
    'DemoteCarrySettleTests.test_unconverged_pair_reports_inconclusive',
    'DemoteCarrySettleTests.test_unreachable_peer_reports_inconclusive',
    'DemoteCarrySettleTests.test_diverging_digests_report_nondeterministic',
    'DemoteCarrySettleTests.test_two_runs_produce_identical_evidence',
})


class DemoteCarryPeer(DemoteSettlePeer):
    """The carry leg's endpoint record — the settle peer's fields
    plus the claim token its field-owning scans write under and the
    following-standby sync name its /role report serves."""

    def __init__(self, name, token):
        super().__init__(name)
        self.token = token
        self.sync = 'unsynchronized'  # tracking | orphaned | unsynchronized


class DemoteCarryFeed(DemoteSettleFeed):
    """A stubbed pair for the demote-carry-settle leg, arbitrating
    the field claim through a real ClaimPlantPeer: ctrl-a owns the
    field under TOKEN_A at launch, ctrl-b tracks. Every endpoint
    call is one scan except POST /command, whose admission lands
    pending between scans — the raced submissions the detection scan
    owes a verdict to. The rogue claim_writer the scenario plants
    through the wire protocol preempts the owner's claim; its next
    scan detects the fence, re-suspends the boundary's provisional
    settlements per the #829 contract — receipts stay Accepted for
    the surviving line — journals field_claim_lost, and walks
    demoting->standby, while the tracking peer's pulls keep carrying
    the suspended admissions until its promote applies them once.
    Doctor flags stage each named defect the issue calls out."""

    POINTS = (302, 300, 301, 332, 333)
    SIGNALS = [{'point': point,
                'name': 'p101-oos' if point == 302
                        else 'pt-' + str(point),
                'direction': 'in', 'value_type': 'bool',
                'writable': True} for point in POINTS]
    TOKEN_A, TOKEN_B = 0xA11CE, 0xB0B5E

    def __init__(self, plant):
        self.plant = plant       # the wire-arbitrating ClaimPlantPeer
        self.a = DemoteCarryPeer('a', self.TOKEN_A)
        self.a.role = 'active'
        self.b = DemoteCarryPeer('b', self.TOKEN_B)
        self.journal_paths = {}
        self._wire_claim(self.TOKEN_A)
        # The doctors staging each named defect.
        self.mint_then_carry = False  # the demote journals superseded
                                      # beside the carried applied
        self.drop_carry = False       # the tracking pulls drop the
                                      # suspended admissions
        self.no_demote = False        # the owner never detects the
                                      # lost claim
        self.promote_refused = False  # every promote is refused
        self.no_tracking = False      # the standby never converges
        self.no_active = False        # no peer reports active
        self.unreachable = False      # ctrl-b's monitor never answers

    def _wire_claim(self, token):
        """A claim_writer request through the real wire protocol —
        the standing owner the rogue's preemption meets, and the
        promoted peer's own grant."""
        host, _, port = self.plant.address.rpartition(':')
        with socket.create_connection((host, int(port)),
                                      timeout=5) as conn:
            conn.sendall(json.dumps(
                {'op': 'claim_writer', 'owner': token}).encode()
                + b'\n')
            json.loads(conn.recv(65536).split(b'\n')[0])

    @staticmethod
    def _superseded(receipt):
        """The rejected:superseded verdict copy of a receipt — the
        shape the demote boundary mints when it settles an admission
        the surviving line has not adjudicated."""
        return {'command': receipt['command'],
                'actor': receipt.get('actor'),
                'index': receipt.get('index'),
                'outcome': {'rejected': {'reason': {
                    'superseded': {
                        'point': receipt['command']
                        ['write_value']['point']}}}}}

    def _adopt(self, peer, other):
        """The tracking pull: the source's log replaces the peer's
        own — active or quiesced, the checkpoint carries the same
        receipts — suspended admissions the window carries re-queue
        live, the ones the adopted high-water passed settle
        superseded, and anything beyond it stays suspended. A source
        whose submission high-water sits behind this run's cannot
        regress its log — the pull lands on sync posture alone."""
        if not self.no_tracking:
            peer.sync = 'tracking' if other.role == 'active' \
                else 'orphaned'
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
        if self.drop_carry:
            peer.receipts = [
                receipt for receipt in peer.receipts
                if 'accepted' not in receipt['outcome']
                or not str(receipt.get('actor'))
                .startswith('qa-lane-carry')]
        self._observe(peer)

    def _advance(self, peer, adopt=True):
        """One completed scan: pending role transitions settle; a
        field-owning scan applies its pending admissions, or — the
        claim preempted — detects the fence, re-suspends the
        boundary's provisional settles, journals the loss, and
        demotes in place; a standby pulls its tracked source's
        checkpoint."""
        peer.tick += 1
        if peer.role == 'demoting':
            peer.role = 'standby'
            self._mark(peer, {'role_changed': {'from': 'demoting',
                                               'to': 'standby'}})
        elif peer.role == 'promoting':
            peer.role = 'active'
            self._mark(peer, {'role_changed': {'from': 'promoting',
                                               'to': 'active'}})
        if peer.role == 'active':
            claim = self.plant.claim or {}
            if self.no_demote or claim.get('owner') == peer.token:
                self._apply(peer)
            else:
                # The detection scan: the #829 contract re-suspends
                # the boundary's provisional settlements — every
                # receipt stays Accepted for the surviving line to
                # adjudicate — and the run journals the claim loss,
                # never a terminal verdict of its own.
                if self.mint_then_carry:
                    # The defect this leg pins: the demote mints
                    # superseded beside the applied the carry lands —
                    # two terminal outcomes for one admission.
                    for receipt in peer.receipts:
                        if 'accepted' in receipt['outcome'] \
                                and str(receipt.get('actor')) \
                                .startswith('qa-lane-carry'):
                            self._mark(peer, {'command_settled': {
                                'receipt': self._superseded(receipt)}})
                self._mark(peer, {'field_claim_lost': {
                    'tick': peer.tick, 'point': 0}})
                peer.role = 'demoting'
                self._mark(peer, {'role_changed': {'from': 'active',
                                                   'to': 'demoting'}})
        elif peer.role == 'standby' and adopt:
            self._adopt(peer, self._other(peer))
        self._observe(peer)

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('://', 1)[1].split(':')[0]
        peer = self._peers()[host.split('-', 1)[1]]
        if self.unreachable and peer.name == 'b':
            raise urllib.error.URLError('unreachable')
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if (method, route) == ('POST', '/demote'):
            # The gate closes at the request boundary: the demote
            # lands before the quiesced scans that follow it.
            if peer.role != 'active':
                self._raise(409, {'not_active': {}})
            peer.role = 'demoting'
            self._advance(peer, adopt=False)
            return 200, {'role': 'demoting'}
        if (method, route) == ('POST', '/command'):
            # An admission lands pending between scans — the call
            # itself is not a scan, so a submission racing the
            # preemption meets the detection's suspension.
            receipt = {'command': (body or {}).get('command'),
                       'actor': (body or {}).get('actor'),
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
        if (method, route) == ('POST', '/promote'):
            if peer.role == 'active':
                self._raise(409, {'already_active': {}})
            if peer.sync not in ('tracking', 'orphaned') \
                    or self.promote_refused:
                self._raise(409, {'not_converged': {
                    'sync': {'unsynchronized': {}}}})
            other = self._other(peer)
            # The promotion boundary's final-sync transfer — the
            # source's whole log, suspended admissions carried live —
            # plus the field claim the rogue's token loses.
            peer.receipts = copy.deepcopy(other.receipts)
            peer.attempts = other.attempts
            peer.image = dict(other.image)
            self._wire_claim(peer.token)
            peer.role = 'promoting'
            return 200, {'role': 'promoting'}
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class DemoteCarrySettleTests(unittest.TestCase):
    """The demote-carry-settle leg against the stubbed pair over a
    real claim-arbitrating plant: a clean rig passes with identical
    digests and evidence — every raced admission carried live and
    settled applied once — each doctored defect reports the named
    diagnostic, and an unreachable or unconverged peer is
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = ClaimPlantPeer()
        self.feed = DemoteCarryFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': self.plant.address,
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'DEMOTE_CARRY_SETTLE', 2.0), \
                patch.object(scenarios, 'DEMOTE_CARRY_AUDIT', 2.0), \
                patch.object(scenarios, 'DEMOTE_CARRY_POLL', 0.001), \
                patch.object(scenarios, 'DEMOTE_CARRY_WATCH', 0.001):
            return scenarios.scenario_demote_carry_settle(
                ctx or self._ctx())

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The same restored window as the demote-settle-uniqueness
        # leg, ahead of the peer-announce case and the tune case's
        # a->b switch.
        self.assertEqual(
            order.index(scenarios.scenario_demote_settle_uniqueness)
            + 1,
            order.index(scenarios.scenario_demote_carry_settle))
        self.assertEqual(
            order.index(scenarios.scenario_demote_carry_settle) + 1,
            order.index(scenarios.scenario_peer_announce))
        self.assertIs(
            verify.case_function('demote-carry-settle'),
            scenarios.scenario_demote_carry_settle)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('demote-carry-signals.json',
                     'demote-carry-pass-1.json',
                     'demote-carry-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads((self.evidence / name).read_text())
                  for name in ('demote-carry-pass-1.json',
                               'demote-carry-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(passes[0]['digest']['outcomes'], 'single')
        self.assertEqual(passes[0]['digest']['carried'], 'some')
        # Every raced admission settled applied exactly once on each
        # peer — the carried copy's one application.
        for passed in passes:
            for audit in passed['audit']:
                self.assertIsNotNone(audit['window'], audit)
                outcomes = {
                    scenarios._outcome_key(receipt)
                    for entries in audit['window']['journaled'].values()
                    for receipt in entries}
                self.assertEqual({'applied'}, outcomes, audit)
        # The standing claim and the rogue preemption ran through the
        # real wire protocol.
        self.assertGreaterEqual(
            self.plant.requests.count('claim_writer'), 3)
        report.validate_scenario(record)

    def test_superseded_then_applied_reports_nondeterministic(self):
        self.feed.mint_then_carry = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-carry-settle-nondeterministic'), record['detail'])
        self.assertIn('never more than one terminal outcome',
                      record['detail'])
        report.validate_scenario(record)

    def test_dropped_carry_reports_failed(self):
        self.feed.drop_carry = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-carry-settle-failed'), record['detail'])
        self.assertIn('never carried', record['detail'])
        report.validate_scenario(record)

    def test_owner_never_demotes_reports_failed(self):
        self.feed.no_demote = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-carry-settle-failed'), record['detail'])
        self.assertIn('never demoted', record['detail'])
        report.validate_scenario(record)

    def test_refused_promote_reports_failed(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-carry-settle-failed'), record['detail'])
        self.assertIn('promote', record['detail'])
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
        passes = iter([({'outcomes': 'single'}, {}, {'pass': 1}),
                       ({'outcomes': 'diverged'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_demote_carry_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-carry-settle-nondeterministic'),
            record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            plant = ClaimPlantPeer()
            feed = DemoteCarryFeed(plant)
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.plant, self.evidence = plant, evidence
            try:
                record = self.run_scenario(feed=feed)
            finally:
                plant.close()
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
