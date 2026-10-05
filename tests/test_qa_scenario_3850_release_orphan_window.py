"""The 3850_release_orphan_window leg's scenario unit coverage — the
feed fakes and TestCase classes for
scenario_release_orphan_window, in the
tests/test_qa_scenario_NNNN_<slug>.py split layout (#940). The shared
fakes and helpers live in tests/qa_scenario_support.py; the field
arbitration rides the shared claim peer and the pumps' own interlock
chain is computed by the stub, so the leg races the same release the
shipped station propagates. EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'ReleaseOrphanWindowTests.test_registered',
    'ReleaseOrphanWindowTests.test_clean_pair_passes_and_validates',
    'ReleaseOrphanWindowTests.test_energized_past_the_bound_reports_failed',
    'ReleaseOrphanWindowTests.test_wedged_promote_reports_failed',
    'ReleaseOrphanWindowTests.test_diverged_peer_reports_failed',
    'ReleaseOrphanWindowTests.test_orphan_unjournaled_reports_failed',
    'ReleaseOrphanWindowTests.test_hand_run_receipt_unsettled_reports_failed',
    'ReleaseOrphanWindowTests.test_release_not_the_declared_trip_reports_failed',
    'ReleaseOrphanWindowTests.test_pair_never_reconverges_reports_failed',
    'ReleaseOrphanWindowTests.test_restore_leaves_the_field_tripped_reports_failed',
    'ReleaseOrphanWindowTests.test_restore_leaves_the_pumps_hand_run_reports_failed',
    'ReleaseOrphanWindowTests.test_demote_refused_reports_failed',
    'ReleaseOrphanWindowTests.test_pre_contract_build_reports_inconclusive',
    'ReleaseOrphanWindowTests.test_both_halves_of_the_contract_digest_identically',
    'ReleaseOrphanWindowTests.test_digests_diverge_reports_nondeterministic',
    'ReleaseOrphanWindowTests.test_silent_judge_reports_unchecked',
    'ReleaseOrphanWindowTests.test_lost_field_reads_report_nondeterministic',
    'ReleaseOrphanWindowTests.test_refused_trip_write_reports_nondeterministic',
    'ReleaseOrphanWindowTests.test_unreachable_pair_reports_inconclusive',
    'ReleaseOrphanWindowTests.test_missing_endpoint_reports_inconclusive',
    'ReleaseOrphanWindowTests.test_missing_plant_reports_inconclusive',
    'ReleaseOrphanWindowTests.test_missing_tokens_reports_inconclusive',
    'ReleaseOrphanWindowTests.test_unwired_model_reports_inconclusive',
    'ReleaseOrphanWindowTests.test_two_runs_produce_identical_evidence',
})


def _without_timings(observations):
    """The scenario's observations without the one carrying the measured
    orphan window — its seconds are this rig's own scan timing."""
    return [text for text in observations or []
            if 'the energized orphan window, bounded by name'
            not in text]


class OrphanPlantPeer(ClaimPlantPeer):
    """The station's field half for the bounded orphan-window leg: the
    shared claim arbitration every leg rides, plus the pump station's
    own points — the journaled `power-fail` contact and each pump's
    operator, availability and command points. The doctor flags stage
    the instability the contract does not answer for."""

    POWER_FAIL = 120
    POWER_OK = 206
    AVAIL = (328, 360)
    PUMPS = ((300, 301, 100), (332, 333, 101))
    WRITABLE = (300, 301, 332, 333)

    def __init__(self):
        super().__init__()
        self.samples = {
            20: {'value': {'float': 1.5}, 'quality': 'good', 'tick': 0},
            self.POWER_FAIL: {'value': {'bool': False}, 'quality': 'good',
                              'tick': 0},
            self.POWER_OK: {'value': {'bool': True}, 'quality': 'good',
                            'tick': 0},
            300: {'value': {'bool': False}, 'quality': 'good', 'tick': 0},
            301: {'value': {'bool': False}, 'quality': 'good', 'tick': 0},
            332: {'value': {'bool': False}, 'quality': 'good', 'tick': 0},
            333: {'value': {'bool': False}, 'quality': 'good', 'tick': 0},
            328: {'value': {'bool': True}, 'quality': 'good', 'tick': 0},
            360: {'value': {'bool': True}, 'quality': 'good', 'tick': 0},
            100: {'value': {'bool': False}, 'quality': 'good', 'tick': 0},
            101: {'value': {'bool': False}, 'quality': 'good', 'tick': 0},
        }
        self.directions = {
            20: 'in', self.POWER_FAIL: 'in', 300: 'in', 301: 'in',
            332: 'in', 333: 'in', self.POWER_OK: 'out', 328: 'out',
            360: 'out', 100: 'out', 101: 'out'}
        # The doctors staging the named failures.
        self.drop_writes = False       # every field write is refused
        self.drop_restore_writes = False  # the restoring writes only
        self.drop_reads = False        # every field read drops

    def _respond(self, conn, request):
        if request.get('op') == 'write' and (
                self.drop_writes
                or (self.drop_restore_writes
                    and (request.get('value') or {}).get('bool') is False)):
            return {'result': 'error',
                    'error': {'kind': 'io',
                              'error': {'disconnected': request['point']}}}
        if self.drop_reads and request.get('op') == 'read':
            raise ConnectionError('the field read dropped')
        return super()._respond(conn, request)


class _OrphanMember:
    """One stubbed controller: the served role and sync posture, the
    served tick, and the durable journal the leg's attribution reads."""

    def __init__(self, key, host, token, role, sync):
        self.key = key      # the ctx endpoint name
        self.host = host    # the served monitor's host name
        self.token = token
        self.role = role
        self.sync = sync
        self.tick = 0
        self.seq = 0
        self.journal = []


class OrphanPairFeed:
    """A stubbed pair for the bounded orphan-window leg: ctrl-a
    ('active') is the launched field owner and ctrl-b ('standby') its
    tracking sibling. Every served monitor request is one scan of the
    addressed member. A field-owning scan runs the station's own
    release chain — `power-fail` inverted into `power-ok`, `power-ok`
    dropping each pump's availability, the protection interlock
    guarding the command, and the hand leg (`mode` and `hand`) standing
    only where the protection is clear — and writes the image's `out`
    points to the field; a standby scan pulls its tracker's stamp and
    reads `tracking` while the line has an owner, `orphaned` while it
    owns no field writes (journaled once per contiguous episode), or
    the promote-blocking `diverged` under the doctor flag. `POST
    /command` receipts the operator writes and settles them at the
    owner's next scan; `POST /demote` runs the keep-claim release,
    abandons the pending writes and walks the owner `demoting`;
    `POST /promote` claims unconditionally and walks the sibling
    `promoting`. The doctor flags stage each named failure."""

    TOKENS = {'active': 424243, 'standby': 424244}
    SIGNALS = (('power-fail', 120), ('power-ok', 206),
               ('p101-cmd', 100), ('p102-cmd', 101),
               ('p101-avail', 328), ('p102-avail', 360),
               ('p101-mode', 300), ('p101-hand', 301),
               ('p102-mode', 332), ('p102-hand', 333))
    LATCHES = (('power-fail-unacknowledged', 1071), ('power-fail-ack', 1070),
               ('none-available-unacknowledged', 5003),
               ('none-available-ack', 5002))

    def __init__(self, plant):
        self.plant = plant
        self.a = _OrphanMember('active', 'ctrl-a', self.TOKENS['active'],
                               'active', 'unsynchronized')
        self.b = _OrphanMember('standby', 'ctrl-b', self.TOKENS['standby'],
                               'standby', 'tracking')
        self.members = {'ctrl-a': self.a, 'ctrl-b': self.b}
        self.keys = {'active': self.a, 'standby': self.b}
        self.pending = []
        self.orphaned = False
        for _name, point in self.LATCHES:
            plant.samples.setdefault(point, {'value': {'bool': False},
                                             'quality': 'good', 'tick': 0})
        plant.claim = {'owner': self.a.token, 'holders': {'ctrl-a'},
                       'monitor': 'ctrl-a:8080', 'controller': True,
                       'yielded': False}
        # The doctors staging the named failures.
        self.silent = False               # both endpoints unreachable
        self.unwired = False              # the model serves no contact
        self.command_refused = False      # the receipted path refuses
        self.receipts_never_settle = False  # submissions never journal
        self.demote_refused = False       # /demote answers 409
        self.promote_wedged = False       # /promote answers not_converged
        self.diverged_rows = False        # the ownerless pull reads diverged
        self.no_orphan_journal = False    # the orphan transition is silent
        self.successor_never_writes = False  # the promoted owner writes
                                          # no field — the release never
                                          # lands after the promote
        self.never_tracks = False         # the demoted peer never converges
        self.tracking_broken = False      # …armed by the demote
        self.restore_refused = False      # the operator state cannot return

    # --- the field ------------------------------------------------
    def _image(self):
        """One scan's own computation of the station: the declared
        interlock chain over the hand-run operator points."""
        tripped = self.plant.samples[self.plant.POWER_FAIL]['value']['bool']
        power_ok = not tripped
        image = {self.plant.POWER_OK: power_ok,
                 self.plant.AVAIL[0]: power_ok,
                 self.plant.AVAIL[1]: power_ok}
        for mode, hand, cmd in self.plant.PUMPS:
            image[cmd] = bool(
                self.plant.samples[mode]['value']['bool']
                and self.plant.samples[hand]['value']['bool']
                and power_ok)
        return image

    def _journal(self, member, event):
        member.seq += 1
        member.journal.append({'seq': member.seq, 'tick': member.tick,
                               'event': event})

    def _owns_field(self, member):
        claim = self.plant.claim
        return claim is not None and claim['owner'] == member.token \
            and member.host in claim['holders']

    def _scan(self, member):
        """One controller scan of the addressed member."""
        member.tick += 1
        if member.role == 'demoting':
            member.role = 'standby'
            member.sync = 'unsynchronized'
            self._journal(member, {'role_changed': {
                'from': 'demoting', 'to': 'standby', 'origin': 'request'}})
            return
        if member.role == 'promoting':
            member.role = 'active'
            member.sync = 'unsynchronized'
            self._journal(member, {'role_changed': {
                'from': 'promoting', 'to': 'active', 'origin': 'request'}})
            self.orphaned = False
            return
        if member.role != 'active':
            self._pull(member)
            return
        self._settle_pending(member)
        if self.successor_never_writes and member is self.b:
            return
        image = self._image()
        for point, value in image.items():
            self.plant.plant_tick += 1
            self.plant.samples[point] = {'value': {'bool': value},
                                         'quality': 'good',
                                         'tick': self.plant.plant_tick}

    def _pull(self, member):
        """One standby scan's tracking half: the tracker's checkpoint
        stamp names the line's owner, and an ownerless line reads
        `orphaned` (or the promote-blocking `diverged` under the
        doctor flag) and journals the transition once per episode."""
        if self.tracking_broken:
            # The doctor: the demoted peer applies no checkpoint, so it
            # never converges on whichever successor took the field.
            member.sync = 'unsynchronized'
            return
        source = self.b if member is self.a else self.a
        if self._owns_field(source):
            member.sync = 'tracking'
            self.orphaned = False
            return
        member.sync = 'diverged' if self.diverged_rows else 'orphaned'
        if not self.orphaned:
            self.orphaned = True
            if not self.no_orphan_journal:
                self._journal(member, {'field_orphaned':
                                       {'aligned': member.tick}})

    def _settle_pending(self, member):
        """The owner's scan boundary applies the admitted operator
        writes and settles their receipts in `seq` order."""
        for entry in list(self.pending):
            self.plant.samples[entry['write']['point']] = {
                'value': entry['write']['value'], 'quality': 'good',
                'tick': self.plant.plant_tick}
            self.pending.remove(entry)
            if not self.receipts_never_settle:
                self._journal(member, {'command_settled': {'receipt': {
                    'actor': entry['actor'],
                    'command': {'write_value': entry['write']},
                    'outcome': {'applied': {}}}}})

    # --- the served surface ---------------------------------------
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2].split(':')[0]
        member = self.members.get(host)
        if member is None or self.silent:
            raise urllib.error.URLError('unreachable')
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        since = int(query.split('=', 1)[1]) \
            if query.startswith('since=') else 0
        if (method, route) == ('POST', '/command'):
            return self._command(member, body or {})
        if (method, route) == ('POST', '/demote'):
            return self._demote(member)
        if (method, route) == ('POST', '/promote'):
            return self._promote(member)
        self._scan(member)
        if (method, route) == ('GET', '/role'):
            report = {'role': member.role, 'tick': member.tick,
                      'field_claim': 'held'
                      if self._owns_field(member) else 'unclaimed'}
            if member.role == 'standby':
                report['sync'] = {member.sync: {'aligned': member.tick}}
            return 200, report
        if (method, route) == ('GET', '/snapshot'):
            # The scan's own served image: every `out` point the block
            # graph computed this scan — a demoted standby keeps
            # computing them — over the field `in` points it consumed.
            image = self._image()
            served = {}
            for point, sample in self.plant.samples.items():
                if not isinstance(point, int):
                    continue
                if point in image:
                    served[point] = {'value': {'bool': image[point]},
                                     'quality': 'good', 'tick': member.tick}
                else:
                    served[point] = dict(sample)
            return 200, {'tick': member.tick, 'points': [
                {'point': point, 'sample': served[point]}
                for point in sorted(served)]}
        if (method, route) == ('GET', '/signals'):
            if self.unwired:
                return 200, {'points': [
                    {'name': 'p101-cmd', 'point': 100, 'direction': 'out',
                     'value_type': 'bool'}]}
            points = [{'name': name, 'point': point,
                       'direction': 'in' if point in self.plant.WRITABLE
                       or point == self.plant.POWER_FAIL else 'out',
                       'value_type': 'bool', 'writable': True}
                      for name, point in self.SIGNALS]
            points.extend({'name': name, 'point': point, 'direction': 'out',
                           'value_type': 'bool', 'writable': False}
                          for name, point in self.LATCHES)
            return 200, {'points': points}
        if (method, route) == ('GET', '/journal'):
            return 200, [entry for entry in member.journal
                         if entry['seq'] > since]
        raise AssertionError('unexpected request %s %s' % (method, url))

    def _command(self, member, body):
        """`POST /command` — the bounded receipted operator path: the
        declared writable operator points are admitted and settle at
        the owner's next scan; anything else, and the restore the doctor
        refuses, is rejected by name."""
        write = ((body or {}).get('command') or {}).get('write_value') or {}
        reason = 'not_writable' \
            if write.get('point') not in self.plant.WRITABLE \
            else 'not_active'
        if self.command_refused or member.role != 'active' \
                or (self.restore_refused
                    and (write.get('value') or {}).get('bool') is False):
            return 200, {'outcome': {'rejected': {'reason': {reason: {}}}}}
        self.pending.append({'write': write, 'actor': body.get('actor')})
        return 200, {'outcome': {'accepted': {'index': 1}}}

    def _demote(self, member):
        """`POST /demote` — the deliberate hand-back: the claim stands
        yielded with the controller's hold dropped, the pending writes
        are abandoned, and the owner walks `demoting`."""
        if self.demote_refused:
            return 409, {'error': 'no_tracking_source'}
        if member.role not in ('active', 'promoting'):
            return 409, {'error': 'not_active'}
        claim = self.plant.claim
        if claim is not None and claim['owner'] == member.token:
            claim['holders'] = {holder for holder in claim['holders']
                                if not isinstance(holder, str)}
            claim['yielded'] = True
        self.pending.clear()
        if self.never_tracks:
            self.tracking_broken = True
        member.role = 'demoting'
        member.sync = 'unsynchronized'
        self._journal(member, {'role_changed': {
            'from': 'active', 'to': 'demoting', 'origin': 'request'}})
        return 200, {'role': 'demoting', 'tick': member.tick}

    def _promote(self, member):
        """`POST /promote` — the unconditional claim, answered
        `promoting`, or the divergence gate's `not_converged`
        refusal the finding recorded."""
        if member.role in ('active', 'promoting'):
            return 409, {'error': 'already_active'}
        if self.promote_wedged or (member.sync == 'unsynchronized'
                                   and not self.tracking_broken):
            return 409, {'error': {'not_converged': {
                'detail': 'the served image has not converged'}}}
        self.plant.claim = {'owner': member.token,
                            'holders': {member.host},
                            'monitor': member.host + ':8080',
                            'controller': True, 'yielded': False}
        member.role = 'promoting'
        member.sync = 'unsynchronized'
        self._journal(member, {'role_changed': {
            'from': 'standby', 'to': 'promoting', 'origin': 'request'}})
        return 200, {'role': 'promoting', 'tick': member.tick}


class ReleaseOrphanWindowTests(unittest.TestCase):
    """The release_orphan_window scenario against the stubbed pair: a
    clean rig passes — the hand-run pumps energized on the field, the
    driven power-fail contact raced against the demote, the sibling's
    promote flushing the abandoned release inside the declared bound,
    and the rig restored — while each doctored defect reports the named
    diagnostic, the pre-wedge build reports inconclusive, and the
    unreachable, seam-less, or unwired rig is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = OrphanPlantPeer()
        self.feed = OrphanPairFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'plant': self.plant.address, 'plant_owner': dict(feed.TOKENS),
               'evidence_dir': str(self.evidence)}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        ctx = ctx or self._ctx(feed)
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'ROW_SETTLE', 1.5), \
                patch.object(scenarios, 'ROW_HAND', 1.5), \
                patch.object(scenarios, 'ROW_BOUND', 1.2), \
                patch.object(scenarios, 'ROW_ROUNDS', 2), \
                patch.object(scenarios, 'ROW_POLL', 0.001), \
                patch.object(scenarios, 'ROW_RESTORE', 1.5):
            return scenarios.scenario_release_orphan_window(ctx)

    def _passes(self):
        return [json.loads(
            (self.evidence / name).read_text())
            for name in ('release-orphan-window-pass-1.json',
                         'release-orphan-window-pass-2.json')]

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_release_orphan_window),
            order.index(scenarios.scenario_power_fail_trip))
        self.assertIs(
            verify.case_function('release-orphan-window'),
            scenarios.scenario_release_orphan_window)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('passed', record['outcome'], record)
        # The launch layout and the field restored: ctrl-a owns the
        # field again with both pumps out of hand and the contact
        # released.
        self.assertEqual('active', self.feed.a.role)
        self.assertEqual('standby', self.feed.b.role)
        self.assertEqual('tracking', self.feed.b.sync)
        self.assertIs(False,
                      self.plant.samples[self.plant.POWER_FAIL]['value']
                      ['bool'])
        for mode, hand, _cmd in self.plant.PUMPS:
            self.assertIs(False, self.plant.samples[mode]['value']['bool'])
            self.assertIs(False, self.plant.samples[hand]['value']['bool'])
        self.assertTrue((self.evidence
                         / 'release-orphan-window-pass-1.json').is_file())
        passes = self._passes()
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        digest = passes[0]['digest']
        self.assertEqual('bounded', digest['window'])
        self.assertEqual('granted', digest['promote'])
        self.assertEqual('propagated', digest['trip'])
        self.assertEqual('clean', digest['journal'])
        self.assertEqual('reconverged', digest['pair'])
        self.assertEqual('restored', digest['rig'])
        self.assertEqual('complete', digest['reads'])
        # The staged episode ran its documented sequence: the demote
        # raced the release, the field stayed energized across the
        # orphan window, and the sibling's promote flushed it.
        stage = passes[0]['record']
        self.assertEqual('orphan', stage['window_shape'])
        self.assertEqual(200, stage['demote']['status'])
        self.assertEqual(200, stage['promote']['status'])
        self.assertIsNotNone(stage['released'])
        # …and the rig came back the way the leg found it: the launch
        # owner owns the field again.
        self.assertEqual('active', stage['restored_owner'])

    def test_energized_past_the_bound_reports_failed(self):
        # The issue's named doctored negative: the field outputs stay
        # energized past the declared bound because no field-owning
        # scan ever writes the abandoned release.
        self.feed.successor_never_writes = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'release-orphan-window-failed'), record['detail'])
        self.assertIn('past the declared bound', record['detail'])

    def test_wedged_promote_reports_failed(self):
        # The divergence gate refusing the successor: no `diverged` ever
        # reported, so the wedge is the contract's own failure rather
        # than the pre-wedge build's shape.
        self.feed.promote_wedged = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('POST /promote', record['detail'])

    def test_diverged_peer_reports_failed(self):
        # A peer reporting the promote-blocking `diverged` while the
        # promote itself still lands.
        self.feed.diverged_rows = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('promote-blocking diverged', record['detail'])

    def test_orphan_unjournaled_reports_failed(self):
        # The ownerless line reported while neither journal carries the
        # named transition.
        self.feed.no_orphan_journal = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('field_orphaned', record['detail'])

    def test_hand_run_receipt_unsettled_reports_failed(self):
        # The receipted hand run admitted but never settled applied.
        self.feed.receipts_never_settle = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('never settled applied', record['detail'])

    def test_release_not_the_declared_trip_reports_failed(self):
        # The pumps return to automatic while the contact stands: the
        # release the leg watched is not the declared trip's, and the
        # hand-run baseline never stands.
        self.feed.command_refused = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_pair_never_reconverges_reports_failed(self):
        # The demoted peer applied no checkpoint, so it never converges
        # on the successor that flushed the release.
        self.feed.never_tracks = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('reconverge', record['detail'])

    def test_restore_leaves_the_field_tripped_reports_failed(self):
        # The restore cannot release the driven contact, so the rig is
        # left tripped under a leg that drove it.
        self.plant.drop_restore_writes = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('restore', record['detail'].lower())

    def test_restore_leaves_the_pumps_hand_run_reports_failed(self):
        # The operator state the leg found is never returned: the
        # restore's own writes are refused.
        self.feed.restore_refused = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('left in a driven operator state', record['detail'])

    def test_demote_refused_reports_failed(self):
        self.feed.demote_refused = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('demote', record['detail'].lower())

    def test_pre_contract_build_reports_inconclusive(self):
        # The pre-contract shape: the ownerless window's rows report the
        # staged-versus-field divergence and the sibling's promote
        # answers not_converged forever.
        self.feed.diverged_rows = True
        self.feed.promote_wedged = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)
        self.assertIn('predating', record['detail'])

    def test_both_halves_of_the_contract_digest_identically(self):
        # The orphan window's shape is the contract's own either-half:
        # the pending write landing before the demotion completes and
        # the promotion flushing it inside the bound are both the
        # answer, so both must digest alike — a digest that recorded
        # the shape would report a conformant rig nondeterministic.
        raced = scenarios._row_clean_record()
        flushed = scenarios._row_clean_record()
        flushed['window_shape'] = 'flushed'
        flushed['released'] = dict(
            flushed['rows'][0],
            commands={'cmd1': False, 'cmd2': False},
            seconds=round(flushed['bound'] / 20.0, 3))
        flushed['window_seconds'] = flushed['released']['seconds']
        digests = []
        for stage in (raced, flushed):
            found = {}
            scenarios._row_judge(stage, lambda key, diagnostic, detail:
                                 found.setdefault(key, (diagnostic, detail)))
            self.assertEqual({}, found)
            digests.append(scenarios._row_digest(stage, found))
        self.assertEqual(digests[0], digests[1])

    def test_digests_diverge_reports_nondeterministic(self):
        # Two passes whose normalized verdicts disagree — the leg's own
        # two-pass comparison, reported by name.
        digests = iter([{'window': 'bounded'}, {'window': 'unbounded'}])
        clean = scenarios._row_clean_record()
        passes = iter([(clean, {'pass': 1}), (clean, {'pass': 2})])

        def fake_pass(*args, **kwargs):
            return next(passes)

        def fake_digest(*args, **kwargs):
            return next(digests)

        with patch.object(scenarios, '_row_pass', fake_pass), \
                patch.object(scenarios, '_row_digest', fake_digest):
            record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'release-orphan-window-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])

    def test_silent_judge_reports_unchecked(self):
        # The unchecked-diagnostic self-check leg: a judge silenced
        # mid-run lets every planted negative slip and the leg reports
        # its own unchecked name.
        with patch.object(scenarios, '_row_judge', lambda *a, **k: None):
            record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'release-orphan-window-unchecked'), record['detail'])

    def test_lost_field_reads_report_nondeterministic(self):
        # Every field read drops: the window answered no row whose
        # reads both landed, which is instability, not a verdict.
        self.plant.drop_reads = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'release-orphan-window-nondeterministic'), record['detail'])

    def test_refused_trip_write_reports_nondeterministic(self):
        # The field refuses the trip write under the shared claim: the
        # staging call never completed.
        self.plant.drop_writes = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'release-orphan-window-nondeterministic'), record['detail'])

    def test_unreachable_pair_reports_inconclusive(self):
        self.feed.silent = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_endpoint_reports_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(standby=None))
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_plant_reports_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(plant=None))
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_tokens_reports_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(plant_owner={}))
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_unwired_model_reports_inconclusive(self):
        self.feed.unwired = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)
        self.assertIn('power-fail', record['detail'])

    def test_two_runs_produce_identical_evidence(self):
        # Two independent rigs pass alike. The comparison skips the one
        # observation carrying the measured window: its seconds are a
        # wall-clock measurement of this rig's scan timing, exactly the
        # reading the digest deliberately excludes — the two-pass
        # *digest* comparison inside the leg is the contract's own
        # determinism check.
        first = self.run_scenario()
        plant2 = OrphanPlantPeer()
        feed2 = OrphanPairFeed(plant2)
        try:
            self.plant.close()
            self.plant = plant2
            second = self.run_scenario(feed=feed2, ctx=self._ctx(feed2))
        finally:
            plant2.close()
        self.assertEqual(first['outcome'], second['outcome'])
        self.assertEqual(_without_timings(first.get('observations')),
                         _without_timings(second.get('observations')))
        self.assertEqual(self._passes()[0]['digest'],
                         self._passes()[1]['digest'])


if __name__ == '__main__':
    unittest.main()