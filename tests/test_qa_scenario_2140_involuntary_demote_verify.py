"""The 2140_involuntary_demote_verify leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_involuntary_demote_verify, split out per the #940
convention. The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'InvoluntaryHintTests.test_registered',
    'InvoluntaryHintTests.test_keyed_rig_passes_and_validates',
    'InvoluntaryHintTests.test_unkeyed_rig_passes_and_validates',
    'InvoluntaryHintTests.test_chased_hint_fails',
    'InvoluntaryHintTests.test_unkeyed_hint_pulled_fails',
    'InvoluntaryHintTests.test_unkeyed_refusal_journaled_fails',
    'InvoluntaryHintTests.test_silent_keyed_refusal_fails',
    'InvoluntaryHintTests.test_adopted_foreign_hint_fails',
    'InvoluntaryHintTests.test_no_adoption_fails',
    'InvoluntaryHintTests.test_followed_forged_document_fails',
    'InvoluntaryHintTests.test_split_pair_fails',
    'InvoluntaryHintTests.test_unrejoined_peer_fails',
    'InvoluntaryHintTests.test_no_demotion_fails',
    'InvoluntaryHintTests.test_refused_preempt_fails',
    'InvoluntaryHintTests.test_unsettled_command_fails',
    'InvoluntaryHintTests.test_unannounced_hint_fails',
    'InvoluntaryHintTests.test_unrestored_roles_fail',
    'InvoluntaryHintTests.test_diverging_digests_report_nondeterministic',
    'InvoluntaryHintTests.test_two_runs_produce_identical_evidence',
    'InvoluntaryHintTests.test_no_active_reports_failed',
    'InvoluntaryHintTests.test_unconverged_pair_reports_inconclusive',
    'InvoluntaryHintTests.test_unreachable_pair_reports_inconclusive',
    'InvoluntaryHintTests.test_missing_forge_action_reports_inconclusive',
    'InvoluntaryHintTests.test_host_placed_forge_reports_inconclusive',
    'InvoluntaryHintTests.test_missing_journal_reports_inconclusive',
    'InvoluntaryHintTests.test_unplantable_point_reports_inconclusive',
    'InvoluntaryHintTests.test_self_check_catches_every_planted_negative',
})


class InvoluntaryHintFeed:
    """A stubbed pair for the involuntary-demote-verify scenario: ctrl-a
    owns the field with no configured tracking source, ctrl-b is its
    converged tracking standby, and the `start_forge`/`stop_forge` ctx
    actions stand the hostile endpoint on the rig bridge — the staged
    document it serves is the file the action writes, the announce it
    lands is the hit record the action appends.

    The episode is the one no request boundary guards: `POST /promote`
    on the tracking standby preempts the field claim, and the
    superseded owner's next fenced write demotes it in place. The feed
    models the lazy half of the announced-source contract on that path —
    `probe_announced_hints` on a keyed run takes one bounded pull at the
    announced endpoint, journals the refusal by name, and pins the
    successor the field's own arbitration names; an unkeyed run earns no
    pull at all and journals nothing, because nothing was served to
    refuse. Every endpoint call is one completed scan, so two runs emit
    identical evidence. Doctor flags stage each named failure the issue
    calls out."""

    FORGE_ADDR = '172.18.0.9:8090'
    FORGE_PORT = 8090
    PEER_B = 'ctrl-b:8081'
    OWNER_A = 'ctrl-a:8080'
    CLAIMANT_B = 5150
    POINT = 10
    GENERATION = 424242

    def __init__(self, tmp, pair_token='test-pair'):
        self.tmp = Path(tmp)
        self.tick = 200
        self.role = {'a': 'active', 'b': 'standby'}
        self.sync = {'a': None, 'b': 'tracking'}
        self.b_promoting = False
        self.a_left = None          # polls until the demoted owner
                                   # reports its converged verdict
        self.receipts = []
        self.attempts = 0
        self.pending = []
        self.image = {self.POINT: False}
        self.announced = []
        self.forge = None
        self.pair_token = pair_token
        self.run_count = 1
        self.seq = 0
        self.follows_forgery = False
        self.journal_a = self.tmp / 'journal-a.jsonl'
        self.journal_b = self.tmp / 'journal-b.jsonl'
        self.journal_a.write_text(json.dumps(
            {'run_boundary': {'run': 1, 'tick': self.tick}}) + '\n')
        self.journal_b.write_text(json.dumps(
            {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')

        # Fault injection — each named failure the issue calls out.
        self.chased_hint = False     # the demoted peer dials the
                                     # announced endpoint every scan
        self.unkeyed_pulled = False  # an unkeyed run pulls the bare
                                     # hint anyway
        self.unkeyed_refused = False  # an unkeyed run journals a
                                      # refusal it never earned
        self.silent_refusal = False  # a keyed run's refused probe
                                     # leaves no durable record
        self.adopted_hint = False    # the announced hint is adopted
        self.no_adoption = False     # no verified source resolves
        self.no_demotion = False     # the fenced write never demotes
        self.never_converges = False  # the demoted peer strands
        self.split_pair = False      # two field owners at settle
        self.promote_refused = False  # the claim preempt is refused
        self.forge_silent = False    # the announce never lands
        self.command_refused = False  # the pass's write never applies
        self.no_restore = False      # the launch layout never restores
        self.unreachable = False     # the monitors never answer
        self.no_active = False       # no peer reports role=active
        self.no_tracking = False     # the standby never converges
        self.unplantable = False     # no bool sample to plant against
        self.chase_hits = 0

    @property
    def keyed(self):
        """Whether the run carries the shared tracking secret — the
        posture that decides whether a bare hint earns a verify pass."""
        return bool(self.pair_token)

    # ---- journal + hits ledgers ----------------------------------

    def _journal_a(self, event, body):
        self.seq += 1
        record = {'entry': {'seq': self.seq, 'tick': self.tick,
                            'event': {event: body}}}
        with open(self.journal_a, 'a') as handle:
            handle.write(json.dumps(record) + '\n')

    def _hit(self, record):
        if self.forge is None:
            return
        with open(self.forge['hits'], 'a') as handle:
            handle.write(json.dumps(record) + '\n')

    def _serve(self, count=1):
        for index in range(count):
            self._hit({'kind': 'serve', 'remote': 'ctrl-a:8080',
                       'query': 'prove=%d' % (self.tick + index),
                       'signed': bool(self.forge
                                      and self.forge['keyed']),
                       'status': 200})

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://pair', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    # ---- the runner-owned lifecycle actions ----------------------

    def stop_controller(self, name):
        pass

    def start_controller(self, name):
        pass

    def restart_controller(self, name):
        self.run_count += 1
        with open({'active': self.journal_a,
                   'standby': self.journal_b}[name], 'a') as handle:
            handle.write(json.dumps({'run_boundary': {
                'run': self.run_count, 'tick': self.tick}}) + '\n')

    def start_forge(self, document, owner, keyed=True):
        directory = self.tmp / 'forge'
        directory.mkdir(exist_ok=True)
        document_path = directory / 'checkpoint.json'
        document_path.write_text(json.dumps(document))
        hits = directory / 'hits.jsonl'
        hits.write_text('')
        self.forge = {'container': 'dcs-hw-qa-1-forge',
                      'dir': str(directory),
                      'document': str(document_path),
                      'hits': str(hits),
                      'keyed': bool(keyed and self.pair_token),
                      'port': self.FORGE_PORT}
        self.announced = [self.FORGE_ADDR]
        if not self.forge_silent:
            self._hit({'kind': 'announce', 'target': 'ctrl-a:8080',
                       'ok': True})
        return self.forge

    def stop_forge(self):
        self.forge = None

    # ---- the scan advance ----------------------------------------

    def _advance(self):
        self.tick += 1
        for apply_tick, receipt in list(self.pending):
            if self.tick < apply_tick:
                continue
            write = receipt['command']['write_value']
            self.image[write['point']] = write['value']['bool']
            receipt['outcome'] = {'applied': {'tick': self.tick}}
            self._journal_a('command_settled', {'receipt': receipt})
            self.pending.remove((apply_tick, receipt))
        if self.b_promoting:
            self.b_promoting = False
            self.role['b'] = 'active'
            self.sync['b'] = None
            self._superseed_a()
        if self.chased_hint and self.role['a'] == 'standby':
            # A recorded hint followed as a pull target is dialed once
            # per scan, past any bounded verify window.
            self._serve(5)

    def _lazy_verify(self):
        """The involuntary path's lazy announced-hint verification — the
        half `POST /demote` would have run across its request boundary:
        keyed, one bounded pull per hinted endpoint with the refusal
        journaled by name; unkeyed, no pull at all and nothing to
        refuse. Either way the pin is the endpoint the field's own
        arbitration names."""
        if self.keyed:
            self._serve()
            if not self.silent_refusal:
                self._journal_a('tracking_source_refused', {
                    'source': self.FORGE_ADDR,
                    'detail': "the pull's line proof did not verify "
                              'under the pair key'})
        elif self.unkeyed_pulled:
            self._serve()
        if not self.keyed and self.unkeyed_refused:
            self._journal_a('tracking_source_refused', {
                'source': self.FORGE_ADDR,
                'detail': 'a refusal an unkeyed run never earned'})
        if self.no_adoption:
            return
        self._journal_a('tracking_source_adopted', {
            'source': self.FORGE_ADDR if self.adopted_hint
                      else self.PEER_B})
        self.follows_forgery = bool(self.adopted_hint)

    def _superseed_a(self):
        """The superseded owner's fenced write: the attributed claim
        loss, the protective demotion the fencing path journals, the
        lazy verification, and the walk down to standby."""
        if self.role['a'] != 'active' or self.no_demotion:
            return
        self._journal_a('field_claim_lost', {'point': self.POINT,
                                             'claimant': self.CLAIMANT_B})
        self._journal_a('role_changed', {'from': 'active',
                                         'to': 'demoting',
                                         'origin': 'fenced'})
        self.role['a'] = 'demoting'
        self.sync['a'] = None
        self._lazy_verify()
        self._journal_a('role_changed', {'from': 'demoting',
                                         'to': 'standby',
                                         'origin': 'fenced'})
        self.role['a'] = 'standby'
        self.a_left = 2 if not self.never_converges else None
        self.a_rejoined = False

    # ---- the documented requests ---------------------------------

    def _promote_b(self):
        if self.role['b'] == 'active':
            self._raise(409, 'already_active')
        if self.sync['b'] != 'tracking' or self.promote_refused:
            self._raise(409, 'not_converged')
        self.role['b'] = 'promoting'
        self.sync['b'] = None
        self.b_promoting = True
        return 200, {'role': 'promoting', 'tick': self.tick}

    def _demote_b(self):
        """The promoted peer demoting hands the field's single-writer
        claim back, and the entry owner's conditional claim re-seats it
        for itself — the walk back onto the launch roles."""
        if self.role['b'] not in ('active', 'demoting'):
            self._raise(409, 'not_active')
        self._journal_b('role_changed', {'from': self.role['b'],
                                         'to': 'standby',
                                         'origin': 'request'})
        self.role['b'] = 'standby'
        self.sync['b'] = 'tracking'
        if not self.no_restore:
            self.role['a'] = 'active'
            self.sync['a'] = None
        return 200, {'role': 'demoting', 'tick': self.tick}

    def _promote_a(self):
        if self.role['a'] == 'active':
            self._raise(409, 'already_active')
        if self.no_restore:
            self._raise(409, 'not_converged')
        self.role['a'] = 'active'
        self.sync['a'] = None
        return 200, {'role': 'promoting', 'tick': self.tick}

    def _journal_b(self, event, body):
        with open(self.journal_b, 'a') as handle:
            handle.write(json.dumps(
                {'entry': {'seq': 0, 'tick': self.tick,
                           'event': {event: body}}}) + '\n')

    # ---- the served documents ------------------------------------

    def _role(self, peer):
        report = {'role': self.role[peer], 'tick': self.tick}
        if self.role[peer] == 'promoting':
            self.role[peer] = 'active'
        sync = self.sync[peer]
        if peer == 'b' and self.no_tracking:
            sync = 'unsynchronized'
        if peer == 'a' and self.a_left is not None:
            self.a_left -= 1
            if self.a_left <= 0 and not self.never_converges:
                sync = 'tracking'
                if self.split_pair and self.a_rejoined:
                    # Two field owners at the episode's settle: the
                    # superseded owner re-took the field the moment it
                    # had re-joined.
                    self.role['a'] = 'active'
                self.a_rejoined = True
        if self.role[peer] == 'standby':
            report['sync'] = {sync: {'aligned': self.tick}} \
                if sync else 'unsynchronized'
        elif self.role[peer] == 'active':
            report['sync'] = None
        return report

    def _checkpoint(self, peer):
        owns = self.role[peer] == 'active'
        internal = {str(self.POINT): {
            'value': {'bool': self.image[self.POINT]},
            'quality': 'good', 'tick': self.tick}}
        line_owner = self.OWNER_A if owns else self.PEER_B
        if self.follows_forgery and not owns:
            # The staged forgery: the planted internal sample applied
            # and the line stamp the real successor carries gone.
            internal[str(self.POINT)]['value'] = {
                'bool': not self.image[self.POINT]}
            line_owner = None
        return {
            'format_version': 1,
            'model_fingerprint': 'demote-hint-fp',
            'generation': self.GENERATION,
            'tick': self.tick,
            'components': {}, 'driver': None, 'outputs': {},
            'internal': internal,
            'forces': {},
            'receipts': list(self.receipts) if peer == 'a' else [],
            'command_admission': {'attempts': self.attempts,
                                  'full_rejections': 0,
                                  'high_water': 0},
            'source_owns_field': owns,
            'line_owner': line_owner}

    def _admit_a(self, body):
        self.attempts += 1
        write = (body or {}).get('command', {}).get('write_value', {})
        admitted = (self.role['a'] == 'active'
                    and not self.command_refused)
        if admitted:
            receipt = {'command': body['command'],
                       'actor': body.get('actor'),
                       'outcome': {'accepted': {
                           'apply_tick': self.tick + 1}}}
            self.pending.append((self.tick + 1, receipt))
        else:
            receipt = {'command': (body or {}).get('command'),
                       'actor': (body or {}).get('actor'),
                       'outcome': {'rejected': {'reason': {
                           'not_active': {'point': write.get('point')}}}}
                       if not self.command_refused
                       else {'rejected': {'reason': {'queue_full': {}}}}}
        self.receipts.append(receipt)
        return receipt

    # ---- the endpoint dispatch ------------------------------------

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        host = url.split('/')[2]
        peer = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}[host]
        self._advance()
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if (method, route) == ('GET', '/role'):
            if self.no_active and peer == 'a':
                return 200, {'role': 'standby', 'tick': self.tick,
                             'sync': 'unsynchronized'}
            return 200, self._role(peer)
        if (method, route) == ('GET', '/signals'):
            if self.unplantable:
                return 200, {'points': [
                    {'point': self.POINT, 'signal': None,
                     'name': 'p101-level', 'direction': 'out',
                     'value_type': 'float', 'writable': False}]}
            return 200, {'points': [
                {'point': self.POINT, 'signal': None,
                 'name': 'p101-oos', 'direction': 'in',
                 'value_type': 'bool', 'writable': True}]}
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': self.tick, 'points': [
                {'point': self.POINT, 'sample': {
                    'value': {'bool': self.image[self.POINT]},
                    'quality': 'good'}}]}
        if (method, route) == ('GET', '/checkpoint'):
            return 200, self._checkpoint(peer)
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts) if peer == 'a' else []
        if (method, route) == ('POST', '/command'):
            if peer == 'a':
                return 200, self._admit_a(body)
            self._raise(409, 'not_active')
        if (method, route) == ('POST', '/promote'):
            if peer == 'a':
                return self._promote_a()
            return self._promote_b()
        if (method, route) == ('POST', '/demote'):
            if peer == 'b':
                return self._demote_b()
            # The request path is not this leg's subject; the entry
            # owner never demotes by request while the episode runs.
            self._raise(409, 'no_tracking_source')
        raise AssertionError('unexpected request %s %s' % (method, url))


class InvoluntaryHintTests(unittest.TestCase):
    """The involuntary-demote-verify leg against the stubbed pair: a
    clean keyed rig passes with identical digests and evidence — the
    bounded verify pass at the announced endpoint, the refusal journaled
    by name, no adoption naming it, a verified successor pinned, and
    the pair one active plus one tracking standby — and a clean
    unkeyed rig pins the inert-hint clause instead, where the bare hint
    earns no pull at all. Each doctored defect reports the named
    diagnostic, and an unconverged, unreachable, forge-less, or
    unjournaled run is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = InvoluntaryHintFeed(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'pair_token': feed.pair_token,
               'endpoint_placement': {'forge': 'bridge'},
               'journal_files': {'active': str(feed.journal_a),
                                 'standby': str(feed.journal_b)},
               'restart_controller': feed.restart_controller,
               'start_forge': feed.start_forge,
               'stop_forge': feed.stop_forge}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, **overrides):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'HINT_SETTLE', 1.0), \
                patch.object(scenarios, 'HINT_POLL', 0.001), \
                patch.object(scenarios, 'FORGE_ANNOUNCE_SETTLE', 1.0):
            return scenarios.scenario_involuntary_demote_verify(
                self.ctx(feed, **overrides))

    def passes(self):
        return [json.loads((self.evidence / name).read_text())
                for name in ('demote-hint-verify-pass-1.json',
                             'demote-hint-verify-pass-2.json')]

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_peer_announce),
            order.index(scenarios.scenario_involuntary_demote_verify))
        self.assertLess(
            order.index(scenarios.scenario_demote_forged_standby_source),
            order.index(scenarios.scenario_involuntary_demote_verify))
        self.assertIs(verify.case_function('involuntary-demote-verify'),
                      scenarios.scenario_involuntary_demote_verify)

    def test_keyed_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertTrue(
            (self.evidence / 'demote-hint-verify-signals.json').is_file())
        passes = self.passes()
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'hints': 'proved', 'audit': 'journaled', 'source': 'verified',
             'pair': 'settled', 'roles': 'restored'})
        self.assertEqual(passes[0]['posture'], 'keyed')
        audit = passes[0]['audit']
        self.assertEqual(audit['served'], 1)
        self.assertEqual(len(audit['refused']), 1)
        self.assertEqual(audit['adopted'], ['ctrl-b:8081'])
        self.assertNotIn('172.18.0.9:8090', audit['durable_adoptions'])
        self.assertEqual(audit['sync'], 'tracking')
        self.assertEqual(audit['document']['line_owner'], 'ctrl-b:8081')
        # The demotion is the fencing path's alone: no request boundary
        # ran a verify, and the claim loss is attributed.
        self.assertEqual(passes[0]['promote']['status'], 200)
        report.validate_scenario(record)

    def test_unkeyed_rig_passes_and_validates(self):
        # The inert-hint clause: on an unkeyed run the announced
        # contract cannot authenticate anything, so the bare hint earns
        # not even a bounded verify pass.
        self.feed.pair_token = None
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passes = self.passes()
        self.assertEqual(passes[0]['posture'], 'unkeyed')
        self.assertEqual(
            passes[0]['digest'],
            {'hints': 'inert', 'audit': 'never-earned',
             'source': 'verified', 'pair': 'settled',
             'roles': 'restored'})
        audit = passes[0]['audit']
        self.assertEqual(audit['served'], 0)
        self.assertEqual(audit['refused'], [])
        self.assertEqual(audit['adopted'], ['ctrl-b:8081'])
        report.validate_scenario(record)

    def test_chased_hint_fails(self):
        # The demoted peer dialing the announced endpoint every scan —
        # a recorded hint followed as a pull target rather than proved.
        self.feed.chased_hint = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-hint-verify-failed'), record['detail'])
        self.assertIn('verify candidate, never a pull target',
                      record['detail'])
        report.validate_scenario(record)

    def test_unkeyed_hint_pulled_fails(self):
        self.feed.pair_token = None
        self.feed.unkeyed_pulled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no tracking source at all', record['detail'])
        report.validate_scenario(record)

    def test_unkeyed_refusal_journaled_fails(self):
        self.feed.pair_token = None
        self.feed.unkeyed_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('nothing can have been served to refuse',
                      record['detail'])
        report.validate_scenario(record)

    def test_silent_keyed_refusal_fails(self):
        # A refused probe that left no durable record is silence, not
        # audit — the contract's auditability clause.
        self.feed.silent_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('auditable, never silent', record['detail'])
        report.validate_scenario(record)

    def test_adopted_foreign_hint_fails(self):
        self.feed.adopted_hint = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('the announced hint was adopted', record['detail'])
        report.validate_scenario(record)

    def test_no_adoption_fails(self):
        # No verified source resolved at all — the strand.
        self.feed.no_adoption = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('resolved no verified tracking source',
                      record['detail'])
        report.validate_scenario(record)

    def test_followed_forged_document_fails(self):
        # The demoted peer serving the staged forgery: its planted
        # internal sample applied and the successor's line stamp gone.
        self.feed.adopted_hint = True
        self.feed.follows_forgery = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('the pulls moved somewhere else',
                      record['detail'])
        self.assertIn('the announced hint was adopted', record['detail'])
        report.validate_scenario(record)

    def test_split_pair_fails(self):
        # Two field owners at the episode's settle.
        self.feed.split_pair = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('the promoted successor alone was expected',
                      record['detail'])
        report.validate_scenario(record)

    def test_unrejoined_peer_fails(self):
        self.feed.never_converges = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never re-joined', record['detail'])
        report.validate_scenario(record)

    def test_no_demotion_fails(self):
        # The preempted claim never demoted its owner — the demotion is
        # the superseded owner's own fenced write, with no request to
        # fall back on.
        self.feed.no_demotion = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never reported standby', record['detail'])
        report.validate_scenario(record)

    def test_refused_preempt_fails(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('field claim could not be preempted',
                      record['detail'])
        report.validate_scenario(record)

    def test_unsettled_command_fails(self):
        self.feed.command_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never settled', record['detail'])
        report.validate_scenario(record)

    def test_unannounced_hint_fails(self):
        # The hostile endpoint's announce never landed — no hint was
        # recorded to verify at all.
        self.feed.forge_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('announce never landed', record['detail'])
        report.validate_scenario(record)

    def test_unrestored_roles_fail(self):
        self.feed.no_restore = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never settled back to its entry role layout',
                      record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        with patch.object(scenarios, '_digest_hint_verify',
                          side_effect=[{'hints': 'proved'},
                                       {'hints': 'chased'}]):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-hint-verify-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        first = self.run_scenario()
        first_evidence = self.passes()
        second = self.run_scenario()
        second_evidence = self.passes()
        self.assertEqual(first['outcome'], second['outcome'], 'passed')
        self.assertEqual(first['evidence'], second['evidence'])
        self.assertEqual(first_evidence[0]['digest'],
                         second_evidence[0]['digest'])
        self.assertEqual(
            first_evidence[0]['violations'],
            second_evidence[0]['violations'])

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
        self.assertIn('no tracking standby', record['detail'])
        report.validate_scenario(record)

    def test_unreachable_pair_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_missing_forge_action_reports_inconclusive(self):
        record = self.run_scenario(start_forge=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no start_forge action', record['detail'])
        report.validate_scenario(record)

    def test_host_placed_forge_reports_inconclusive(self):
        record = self.run_scenario(
            endpoint_placement={'forge': 'host'})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('rig bridge', record['detail'])
        report.validate_scenario(record)

    def test_missing_journal_reports_inconclusive(self):
        record = self.run_scenario(journal_files={})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no per-controller journal files',
                      record['detail'])
        report.validate_scenario(record)

    def test_unplantable_point_reports_inconclusive(self):
        self.feed.unplantable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no writable bool in-point', record['detail'])
        report.validate_scenario(record)

    def test_self_check_catches_every_planted_negative(self):
        # Every planted negative must be reported by the leg's own
        # judges — an audit that never fires is an audit the leg cannot
        # claim to have exercised.
        self.assertEqual(
            scenarios._self_check(),
            [])
        self.assertEqual(
            scenarios._digest_hint_verify({}, True),
            {'hints': 'proved', 'audit': 'journaled', 'source': 'verified',
             'pair': 'settled', 'roles': 'restored'})
        self.assertEqual(
            scenarios._digest_hint_verify({}, False)['audit'],
            'never-earned')


if __name__ == '__main__':
    unittest.main()
