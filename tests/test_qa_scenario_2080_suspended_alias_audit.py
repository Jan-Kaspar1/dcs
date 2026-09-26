"""The 2080_suspended_alias_audit leg's scenario unit coverage — the
feed fakes and TestCase classes for
scenario_suspended_alias_audit, split out per the #940 convention.
The shared fakes and helpers live in tests/qa_scenario_support.py;
EXPECTED_CASES pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam
from test_qa_scenario_1900_demote_carry_settle import (
    DemoteCarryFeed, DemoteCarryPeer)


EXPECTED_CASES = frozenset({
    'SuspendedAliasTests.test_registered',
    'SuspendedAliasTests.test_clean_rig_passes_and_validates',
    'SuspendedAliasTests.test_suspended_vanished_reports_failed',
    'SuspendedAliasTests.test_double_settle_reports_nondeterministic',
    'SuspendedAliasTests.test_wrong_outcome_reports_nondeterministic',
    'SuspendedAliasTests.test_final_sync_carry_reports_failed',
    'SuspendedAliasTests.test_owner_never_demotes_reports_failed',
    'SuspendedAliasTests.test_never_resolves_reports_failed',
    'SuspendedAliasTests.test_driven_promote_refused_reports_failed',
    'SuspendedAliasTests.test_restore_fails',
    'SuspendedAliasTests.test_no_active_reports_failed',
    'SuspendedAliasTests.'
    'test_unconverged_pair_reports_inconclusive',
    'SuspendedAliasTests.test_unreachable_reports_inconclusive',
    'SuspendedAliasTests.test_predates_contract_reports_inconclusive',
    'SuspendedAliasTests.'
    'test_missing_driven_action_reports_inconclusive',
    'SuspendedAliasTests.'
    'test_missing_journal_files_reports_inconclusive',
    'SuspendedAliasTests.test_missing_plant_reports_inconclusive',
    'SuspendedAliasTests.test_launch_failure_reports_inconclusive',
    'SuspendedAliasTests.'
    'test_driven_never_serves_reports_inconclusive',
    'SuspendedAliasTests.'
    'test_diverging_digests_report_nondeterministic',
    'SuspendedAliasTests.test_two_runs_produce_identical_evidence',
    'SuspendedAliasTests.test_silent_audit_reports_unchecked',
})


class SuspendedAliasPeer(DemoteCarryPeer):
    """One alias-audit endpoint: the carry peer's fields plus the
    checkpoint source its tracking pulls follow (a configured
    --standby or the resolved re-target), its liveness under the
    runner's stop/start seam, and the announced-hint set its own
    checkpoint answers record."""

    def __init__(self, name, token):
        super().__init__(name, token)
        self.source = None      # tracked checkpoint source name
        self.announced = []     # newest-first announced pullers
        self.stamp = None       # the propagated line-owner name


class SuspendedAliasFeed(DemoteCarryFeed):
    """A stubbed three-peer rig for the suspended-alias leg,
    arbitrating the field claim through a real ClaimPlantPeer:
    ctrl-a owns the field at launch under TOKEN_A, ctrl-b tracks it
    through its configured --standby pull, and the driven third
    controller stands on ctrl-d --standby ctrl-b, its scans and
    pulls running only inside a POST /scan batch.

    The alias contract the leg pins (#1080): a tracking adoption's
    carry test compares the whole submission record — command,
    actor, reason — so an identical (point, value) write minted by a
    different admission at the suspended entry's index does not
    carry it; the suspended entry resolves rejected:superseded,
    journaled once. The alias_vanish doctor degrades the carry test
    to the pre-fix command equality — the identical adopted entry
    carries the suspended admission's slot and the admission
    vanishes unaudited. The driven peer's frozen pulls are what
    mint the collision: stopped from pulling the suspended
    admission's window, its promote lands a stale high-water and
    its identical command takes the collision index. Every paced
    endpoint call is one scan; the driven peer scans only inside
    /scan batches; every journaled event mirrors into the
    journal_files paths the leg's durable audit reads."""

    TOKEN_D = 0xD1EF0
    TOKENS = {DemoteCarryFeed.TOKEN_A: 'a',
              DemoteCarryFeed.TOKEN_B: 'b', TOKEN_D: 'd'}
    KEYS = {'active': 'a', 'standby': 'b', 'driven': 'd'}

    def __init__(self, plant, journal_files=None):
        super().__init__(plant)
        # The pair members as alias peers — the carry feed's own
        # a/b lack the tracking fields this leg's pull model needs.
        self.a = SuspendedAliasPeer('a', self.TOKEN_A)
        self.a.role = 'active'
        self.a.stamp = 'a'
        self.b = SuspendedAliasPeer('b', self.TOKEN_B)
        self.b.sync = 'tracking'
        self.b.stamp = 'a'
        self.d = SuspendedAliasPeer('d', self.TOKEN_D)
        self.d.sync = 'unsynchronized'
        # Liveness under the runner's stop/start seam — a down
        # peer's monitor answers nothing.
        self.up = {'a': True, 'b': True, 'd': False}
        # The checkpoint source each standby pulls: a configures
        # nothing — a fenced demote leaves it resolving onto the
        # field's claimed owner — b/d carry configured --standby
        # binds (d's set by start_driven).
        self.b.source = 'a'
        self.a.announced = ['b']     # b's pulls announced it on a
        # The durable half: journal_files maps ctx keys to the
        # runner's --journal-file paths — 'driven' binds ctrl-d.
        self.journal_paths = {}
        for peer, key in ((self.a, 'active'), (self.b, 'standby'),
                          (self.d, 'driven')):
            path = (journal_files or {}).get(key)
            if path is not None:
                path = Path(path)
                path.write_text(
                    json.dumps({'run_boundary': {'run': 1,
                                                 'tick': 0}}) + '\n')
                self.journal_paths[peer.name] = path
        # The doctors staging each named defect.
        self.alias_vanish = False   # the carry test degrades to
                                    # command equality — the #1080
                                    # defect, the suspended admission
                                    # absorbed and vanished unaudited
        self.double_settle = False  # the suspended admission's
                                    # superseded settle journals twice
        self.wrong_outcome = False  # the suspended admission settles
                                    # under a verdict other than the
                                    # named superseded rejection
        self.no_demote = False      # the owner never detects the
                                    # lost claim (carried over)
        self.never_resolves = False # the demoted peers' probes never
                                    # re-target the promoted owner
        self.final_sync_carries = False  # the driven peer's promote
                                    # still lands a live checkpoint —
                                    # its high-water moves, the
                                    # collision never mints
        self.driven_promote_refused = False  # the driven peer's
                                             # promote is refused
        self.restore_fails = False  # the driven owner's demote is
                                    # refused — no restore path
        self.launch_fails = False   # start_driven raises
        self.driven_down = False    # the launched peer never serves
        self.unreachable = False    # every monitor is unreachable
        self.no_active = False      # no peer reports active
        self.no_tracking = False    # the sibling never converges
        self.predates_contract = False  # the contract's surfaces —
                                    # checkpoint receipt window and
                                    # admission counters, receipt
                                    # actor/reason — are absent
        self.stop_fails = False     # stop_controller raises
        self._doubled = False

    def _peers(self):
        return {'a': self.a, 'b': self.b, 'd': self.d}

    # ---- the contract halves --------------------------------------

    @staticmethod
    def _record(receipt):
        """A receipt's submission identity — the (command, actor,
        reason) triple the #1080 carry test compares: the same
        submission, verbatim or settled, is the only carry."""
        return (json.dumps(receipt.get('command'), sort_keys=True),
                receipt.get('actor'), receipt.get('reason'))

    def _carried(self, adopted, receipt):
        """Whether an adopted entry is this suspended submission's
        carry — under alias_vanish the pre-fix command equality that
        aliases a different admission's identical command."""
        if self.alias_vanish:
            return adopted.get('command') == receipt.get('command')
        return self._record(adopted) == self._record(receipt)

    def _superseded_mint(self, peer, receipt):
        """The rejected:superseded verdict copy of a suspended
        receipt — the adopted-window collision's only adjudication,
        carrying the submission's whole record."""
        verdict = dict(receipt)
        verdict['outcome'] = {'rejected': {'reason': {
            'superseded': {
                'point': receipt['command']['write_value']
                ['point']}}}}
        if self.wrong_outcome \
                and str(receipt.get('actor')) \
                .startswith('qa-alias-susp'):
            verdict['outcome'] = {'applied': {'tick': peer.tick}}
        return verdict

    def _adopt(self, peer, src):
        """The tracking pull's adoption: the source's log replaces
        the peer's own — suspended admissions the adopted window
        still names (the same submission record at any outcome)
        carry live, the ones its high-water passed settle
        superseded journaled once, and anything beyond it stays
        suspended. A source behind the peer's high-water regresses
        nothing."""
        if src.attempts < peer.attempts:
            return
        suspended = [receipt for receipt in peer.receipts
                     if 'accepted' in receipt['outcome']]
        peer.receipts = copy.deepcopy(src.receipts)
        peer.attempts = src.attempts
        peer.image = dict(src.image)
        for receipt in suspended:
            if any(self._carried(carried, receipt)
                   for carried in peer.receipts):
                continue            # carried — the adopted copy
                                    # journals the line's verdict
            if receipt['index'] < peer.attempts:
                self._settle(peer,
                             self._superseded_mint(peer, receipt))
                if self.double_settle and not self._doubled \
                        and str(receipt.get('actor')) \
                        .startswith('qa-alias-susp'):
                    self._doubled = True
                    self._mark(peer, {'command_settled': {
                        'receipt': dict(
                            self._superseded_mint(peer, receipt))}})
            else:
                peer.receipts.append(receipt)
        self._observe(peer)

    # ---- the tracking model ----------------------------------------

    def _claimed(self):
        """The field's standing claim's declared monitor — a peer
        name when a controller's claim stands, None for the
        monitor-less rogue claim."""
        return self.TOKENS.get((self.plant.claim or {})
                               .get('owner'))

    def _probe_source(self, peer):
        """The re-resolution probe: the field's claimed monitor
        leads, then the recorded announcers — a candidate must be
        up and serving the line as its field owner. The
        never_resolves doctor wedges only the covering path —
        the driven peer's convergence on the live owner still
        resolves."""
        if self.never_resolves and self.a.role != 'active':
            return None
        for candidate in [self._claimed()] + list(peer.announced):
            if candidate is not None and candidate != peer.name \
                    and self.up.get(candidate) \
                    and self._peers()[candidate].role == 'active':
                return candidate
        return None

    def _pull(self, peer):
        """One tracking pull: a sourceless peer probes first — the
        claimed monitor and the announced hints — then pulls its
        source's checkpoint; a dead source leaves the peer
        unsynchronized, an adopted non-owner's document leaves it
        orphaned and re-resolving."""
        source = peer.source
        if source is None:
            source = self._probe_source(peer)
            if source is not None:
                peer.source = source
        if source is None or not self.up[source] \
                or self.no_tracking:
            peer.sync = 'unsynchronized'
            return
        src = self._peers()[source]
        if peer.name not in src.announced:
            src.announced.insert(0, peer.name)
            src.announced[:] = src.announced[:4]
        self._adopt(peer, src)
        peer.stamp = src.stamp or (
            source if src.role == 'active' else None)
        if src.role == 'active':
            peer.sync = 'tracking'
            return
        peer.sync = 'orphaned'
        candidate = self._probe_source(peer)
        if candidate is not None:
            peer.source = candidate

    def _scan(self, peer):
        """One completed scan: pending role transitions settle, a
        field-owning scan applies its pending admissions or — the
        claim preempted — detects the fence and demotes in place
        with its pending receipts suspended, and a standby pulls
        its tracked source."""
        peer.tick += 1
        if peer.role == 'demoting':
            peer.role = 'standby'
            peer.sync = 'unsynchronized'
            self._mark(peer, {'role_changed': {'from': 'demoting',
                                               'to': 'standby'}})
        elif peer.role == 'promoting':
            peer.role = 'active'
            peer.sync = None
            peer.stamp = peer.name
            self._mark(peer, {'role_changed': {'from': 'promoting',
                                               'to': 'active'}})
        if peer.role == 'active':
            claim = self.plant.claim or {}
            if self.no_demote or claim.get('owner') == peer.token:
                self._apply(peer)
            else:
                # The detection scan: the claim the rogue
                # preempted — the fenced demote suspends every
                # pending admission for the surviving line to
                # adjudicate.
                self._mark(peer, {'field_claim_lost': {
                    'tick': peer.tick, 'point': 0}})
                peer.role = 'demoting'
                self._mark(peer, {'role_changed': {
                    'from': 'active', 'to': 'demoting'}})
        elif peer.role == 'standby':
            self._pull(peer)
        self._observe(peer)

    # ---- the runner-owned lifecycle actions ------------------------

    def start_driven(self, key):
        assert key in ('active', 'standby'), key
        if self.launch_fails:
            raise RuntimeError('docker run failed: launch refused')
        self.up['d'] = not self.driven_down
        self.d.role = 'standby'
        self.d.sync = 'unsynchronized'
        self.d.source = self.KEYS[key]
        self.d.stamp = None
        return {'container': 'dcs-hw-qa-1-d'}

    def stop_driven(self):
        self.up['d'] = False

    def stop_controller(self, key):
        if self.stop_fails:
            raise RuntimeError('docker stop failed: stop refused')
        self.up[self.KEYS[key]] = False

    def start_controller(self, key):
        self.up[self.KEYS[key]] = True

    # ---- the endpoint dispatch --------------------------------------

    def _report(self, peer):
        role = 'standby' if peer.name == 'a' and self.no_active \
            else peer.role
        report = {'role': role, 'tick': peer.tick}
        if role == 'standby':
            sync = peer.sync or 'unsynchronized'
            report['sync'] = {sync: {'aligned': peer.tick}} \
                if sync in ('tracking', 'orphaned') \
                else 'unsynchronized'
        return report

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        host = url.split('://', 1)[1].split(':')[0]
        peer = self._peers()[host.split('-', 1)[1]]
        if not self.up[peer.name]:
            raise urllib.error.URLError('connection refused')
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if (method, route) == ('POST', '/scan'):
            if peer.name != 'd':
                self._raise(409, 'paced')
            for _ in range(int((body or {}).get('scans', 0))):
                self._scan(self.d)
            return 200, {'role': self.d.role, 'tick': self.d.tick}
        if (method, route) == ('POST', '/command'):
            # An admission lands pending between scans — the call
            # itself is not a scan, so the suspended-window
            # submission meets the detection's suspension.
            receipt = {'command': (body or {}).get('command'),
                       'actor': (body or {}).get('actor'),
                       'reason': (body or {}).get('reason'),
                       'index': peer.attempts,
                       'outcome': {'accepted': {
                           'apply_tick': peer.tick + 1}}}
            if self.predates_contract:
                receipt.pop('actor', None)
                receipt.pop('reason', None)
            if peer.role != 'active':
                receipt['outcome'] = {'rejected': {'reason': {
                    'not_active': {}}}}
                self._settle(peer, receipt)
            else:
                peer.attempts += 1
                peer.receipts.append(receipt)
            return 200, copy.deepcopy(receipt)
        if (method, route) == ('POST', '/demote'):
            if peer.role != 'active' \
                    or (peer.name == 'd' and self.restore_fails):
                self._raise(409, {'not_active': {}})
            peer.role = 'demoting'
            self._mark(peer, {'role_changed': {'from': 'active',
                                               'to': 'demoting'}})
            return 200, {'role': 'demoting', 'tick': peer.tick}
        if (method, route) == ('POST', '/promote'):
            if peer.role in ('active', 'promoting'):
                self._raise(409, {'already_active': {}})
            if peer.role != 'standby' \
                    or peer.sync not in ('tracking', 'orphaned') \
                    or (peer.name == 'd'
                        and self.driven_promote_refused):
                self._raise(409, {'not_converged': {
                    'sync': peer.sync or 'unsynchronized'}})
            # The promotion boundary's final-sync fetch pulls the
            # tracked source: the stopped holder answers nothing,
            # so the promoted peer's adopted window predates the
            # suspended admission — the stale high-water the
            # identical command mints on. The final_sync_carries
            # doctor lands the holder's checkpoint anyway.
            source = peer.source
            if peer.name == 'd' and self.final_sync_carries:
                self._adopt(peer, self.a)
            elif source is not None and self.up.get(source):
                self._adopt(peer, self._peers()[source])
            self._wire_claim(peer.token)
            peer.role = 'promoting'
            return 200, {'role': 'promoting', 'tick': peer.tick}
        if peer.name != 'd':
            # The paced pair: every other endpoint call is one scan.
            self._scan(peer)
        if (method, route) == ('GET', '/role'):
            return 200, self._report(peer)
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
            if self.predates_contract:
                return 200, {'format_version': 1,
                             'model_fingerprint': 'alias-fp'}
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


class SuspendedAliasTests(unittest.TestCase):
    """The suspended-alias leg against the stubbed three-peer rig
    over a real claim-arbitrating plant: a clean rig passes with
    identical digests and evidence — the suspended admission's
    superseded adjudication journaled once on its holder, the
    identical admission applied once per peer on its own index —
    each doctored defect reports the named diagnostic, and an
    unreachable, unconverged, seam-less, or pre-contract run is
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_dir = Path(self.tmp.name) / 'journals'
        self.journal_dir.mkdir()
        self.plant = ClaimPlantPeer()
        self.feed = SuspendedAliasFeed(
            self.plant, journal_files=self._journal_files())

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _journal_files(self):
        return {key: str(self.journal_dir
                         / (key + '.jsonl'))
                for key in ('active', 'standby', 'driven')}

    def _ctx(self, **overrides):
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'driven': 'http://ctrl-d:3',
               'plant': self.plant.address,
               'journal_files': self._journal_files(),
               'evidence_dir': str(self.evidence),
               'start_driven': self.feed.start_driven,
               'stop_driven': self.feed.stop_driven,
               'stop_controller': self.feed.stop_controller,
               'start_controller': self.feed.start_controller}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'ALIAS_SETTLE', 2.0), \
                patch.object(scenarios, 'ALIAS_AUDIT', 1.0), \
                patch.object(scenarios, 'ALIAS_POLL', 0.001), \
                patch.object(scenarios, 'ALIAS_WATCH', 0.001):
            return scenarios.scenario_suspended_alias_audit(
                ctx or self._ctx())

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The suspended-alias leg's window: behind the stale-island
        # and journal-boundary legs, still inside the launch-layout
        # window the tune case's a->b switch closes.
        self.assertLess(
            order.index(scenarios.scenario_stale_island_resolution),
            order.index(scenarios.scenario_suspended_alias_audit))
        self.assertLess(
            order.index(scenarios.scenario_suspended_alias_audit),
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(
            verify.case_function('suspended-alias-audit'),
            scenarios.scenario_suspended_alias_audit)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('suspended-alias-signals.json',
                     'suspended-alias-pass-1.json',
                     'suspended-alias-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads((self.evidence / name).read_text())
                  for name in ('suspended-alias-pass-1.json',
                               'suspended-alias-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'suspended': 'adjudicated', 'identical': 'applied',
             'alias': 'engaged', 'roles': 'restored'})
        # The collision minted on the suspended admission's own
        # index — the adopted window's identical entry.
        suspended, identical = passes[0]['admissions']
        self.assertEqual(suspended['command'], identical['command'])
        self.assertEqual(suspended['index'], identical['index'])
        self.assertNotEqual(suspended['actor'], identical['actor'])
        # The audit window: the suspended admission superseded once
        # on its holder, the identical admission applied once on
        # each peer it crossed.
        window = passes[0]['window']['journaled']
        holder = passes[0]['entry_owner']
        self.assertEqual(
            [scenarios._outcome_key(receipt)
             for receipt in window[holder][suspended['actor']]],
            ['rejected:superseded'])
        self.assertEqual(
            window['driven'][suspended['actor']], [])
        for name in (holder, 'driven'):
            self.assertEqual(
                [scenarios._outcome_key(receipt)
                 for receipt in
                 window[name][identical['actor']]], ['applied'],
                name)
        # The durable half agrees with the served monitors.
        durable = passes[0]['durable']
        self.assertEqual(durable[holder][suspended['actor']],
                         ['rejected:superseded'])
        self.assertEqual(durable['driven'][suspended['actor']], [])
        for name in (holder, 'driven'):
            self.assertEqual(
                durable[name][identical['actor']], ['applied'], name)
        report.validate_scenario(record)

    def test_suspended_vanished_reports_failed(self):
        # The defect #1080 fixed: the adopted window's identical
        # entry aliases the suspended admission as carried — it
        # vanishes with no journaled settle and no attributable
        # receipt.
        self.feed.alias_vanish = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'suspended-alias-audit-failed'), record['detail'])
        self.assertIn('vanished unaudited', record['detail'])
        report.validate_scenario(record)

    def test_double_settle_reports_nondeterministic(self):
        self.feed.double_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'suspended-alias-audit-nondeterministic'),
            record['detail'])
        self.assertIn('exactly one', record['detail'])
        report.validate_scenario(record)

    def test_wrong_outcome_reports_nondeterministic(self):
        self.feed.wrong_outcome = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'suspended-alias-audit-nondeterministic'),
            record['detail'])
        self.assertIn('superseded', record['detail'])
        report.validate_scenario(record)

    def test_final_sync_carry_reports_failed(self):
        # The promoted peer's final-sync pull still landed a live
        # checkpoint carrying the suspended admission — its
        # high-water moved, the collision never minted.
        self.feed.final_sync_carries = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'suspended-alias-audit-failed'), record['detail'])
        self.assertIn('high-water', record['detail'])
        report.validate_scenario(record)

    def test_owner_never_demotes_reports_failed(self):
        self.feed.no_demote = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'suspended-alias-audit-failed'), record['detail'])
        self.assertIn('never demoted', record['detail'])
        report.validate_scenario(record)

    def test_never_resolves_reports_failed(self):
        self.feed.never_resolves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'suspended-alias-audit-failed'), record['detail'])
        self.assertIn('reconverged', record['detail'])
        report.validate_scenario(record)

    def test_driven_promote_refused_reports_failed(self):
        self.feed.driven_promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'suspended-alias-audit-failed'), record['detail'])
        self.assertIn('/promote', record['detail'])
        report.validate_scenario(record)

    def test_restore_fails(self):
        self.feed.restore_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'suspended-alias-audit-failed'), record['detail'])
        self.assertIn('/demote', record['detail'])
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

    def test_unreachable_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_predates_contract_reports_inconclusive(self):
        self.feed.predates_contract = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])
        report.validate_scenario(record)

    def test_missing_driven_action_reports_inconclusive(self):
        record = self.run_scenario(self._ctx(start_driven=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('start_driven', record['detail'])
        report.validate_scenario(record)

    def test_missing_journal_files_reports_inconclusive(self):
        record = self.run_scenario(self._ctx(journal_files={}))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal', record['detail'])
        report.validate_scenario(record)

    def test_missing_plant_reports_inconclusive(self):
        record = self.run_scenario(self._ctx(plant=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('plant', record['detail'])
        report.validate_scenario(record)

    def test_launch_failure_reports_inconclusive(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('launch', record['detail'])
        report.validate_scenario(record)

    def test_driven_never_serves_reports_inconclusive(self):
        self.feed.driven_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('/role', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'roles': 'restored'}, {}, {'pass': 1}),
                       ({'roles': 'unrestored'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_alias_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'suspended-alias-audit-nondeterministic'),
            record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            plant = ClaimPlantPeer()
            feed = SuspendedAliasFeed(
                plant, journal_files=self._journal_files())
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

    def test_silent_audit_reports_unchecked(self):
        # The self-check leg: a suspended-verdict audit monkey-
        # patched silent can no longer name the vanished-admission
        # diagnostic — the leg reports it unchecked.
        with patch.object(scenarios, '_suspended_verdict',
                          lambda *a: 'adjudicated'):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'suspended-alias-audit-unchecked'), record['detail'])
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
