"""The 2055_announced_source_verify leg's scenario unit coverage — the
feed fakes and TestCase classes for scenario_announced_source_verify,
split out per the #940 convention. The shared fakes and helpers live
in tests/qa_scenario_support.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


class AnnouncedSourceFeed:
    """A stubbed deployed pair for the announced-source-verify leg:
    ctrl-a owns the field with no configured tracking source and a
    cleared announced-hint set after each warm restart; ctrl-b is the
    tracking standby the leg stops to open the announced-only window;
    and the `start_forge`/`stop_forge` ctx actions stand the tokenless
    forged endpoint on the rig bridge — the staged document it serves
    is the file the action writes, the `?peer=` announce it lands is
    the announce hit the action appends, and any verify pull reads the
    file and answers signed exactly when the endpoint launched keyed.

    The demote verify models #867's rule: on a keyed run each announced
    hint is pulled under a `?prove=` nonce and only a signed answer can
    arm the demotion — the tokenless endpoint's unsigned document
    refuses there, journalling the probe refusal when the build records
    it; on an unkeyed run a bare hint is no tracking source at all and
    earns not even a verify pull. Doctor flags stage each named
    defect the issue calls out."""

    FORGE_ADDR = '172.18.0.9:8090'
    PEER_ADDR = '172.18.0.8:8081'
    FORGE_PORT = 8090
    GENERATION = 515151

    def __init__(self, tmp, keyed=False):
        self.tmp = Path(tmp)
        self.tick = 100
        self.up = {'a': True, 'b': True}
        self.role = {'a': 'active', 'b': 'standby'}
        self.announced = []       # ctrl-a's recorded ?peer= hints
        self.drop_claim = False   # the owner stops claiming the field
        self.configured_source = False  # the owner carries a
                                        # configured --standby slot
        self.pair_token = 'test-pair' if keyed else None
        self.forge = None
        self.seq = 0
        self.run_count = 1
        self.b_track_left = 0
        self.rejoined = False
        self.a_polls = None
        self.journal_a = self.tmp / 'journal-a.jsonl'
        self.journal_b = self.tmp / 'journal-b.jsonl'
        self.journal_a.write_text(json.dumps(
            {'run_boundary': {'run': 1, 'tick': self.tick}}) + '\n')
        self.journal_b.write_text(json.dumps(
            {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
        # Fault injection — each named failure the issue calls out.
        self.adopt_unproven = False   # an unsigned forged document arms
                                      # the demotion — the rule regressed
        self.adopt_bumped = False    # only the strictly-ahead replay
                                      # arms it — the replay shape's
                                      # refusal regressed
        self.journaled_adoption = False   # a refused demote still
                                          # journals the adoption
        self.role_moved = False          # a refused demote still
                                          # demotes the active
        self.wrong_refusal = False       # the refusal carries another
                                         # verdict than no_tracking_source
        self.pull_on_unkeyed = False     # an unkeyed bare hint earns a
                                         # verify pull
        self.forge_signed = False        # the tokenless endpoint
                                         # answers a signed pull
        self.forge_silent = False        # the endpoint never answers a
                                         # pull — it crashed serving
        self.no_field_stamp = False      # the served checkpoint predates
                                         # the ownership stamps
        self.dual_active = False         # the peer took the field too
        self.dual_armed = False          # ... on its restore re-join
        self.no_tracking = False         # the peer never tracks at all
        self.never_rejoin = False        # ... nor again after its
                                         # restore re-join
        self.dropped_reads = False       # a served read drops
        self.unreachable = False         # the monitors never answer
        self.no_active = False           # no peer reports role=active

    # ---- journal + hits ledgers ----------------------------------

    def _journal(self, path, event, body, tick=None):
        self.seq += 1
        record = {'entry': {'seq': self.seq,
                            'tick': self.tick if tick is None else tick,
                            'event': {event: body}}}
        with open(path, 'a') as handle:
            handle.write(json.dumps(record) + '\n')

    def _journal_a(self, event, body):
        self._journal(self.journal_a, event, body)

    def _hit(self, record):
        if self.forge is None:
            return
        with open(self.forge['hits'], 'a') as handle:
            handle.write(json.dumps(record) + '\n')

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://pair', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    # ---- the runner-owned lifecycle actions ----------------------

    def stop_controller(self, name):
        self.up[{'active': 'a', 'standby': 'b'}[name]] = False

    def start_controller(self, name):
        peer = {'active': 'a', 'standby': 'b'}[name]
        self.up[peer] = True
        if peer == 'b':
            # The resumed peer's configured --standby pull re-tracks
            # ctrl-a after a pull cadence.
            self.b_track_left = 2
            self.rejoined = True
            # The dual-active doctor takes the field on that re-join,
            # so the entry settle is unaffected.
            self.dual_armed = self.dual_active

    def restart_controller(self, name):
        # The warm restart: the persisted state resumes — role, field
        # claim — and the fresh monitor's recorded-hint set is empty.
        self.run_count += 1
        self.announced = []
        self._journal({'active': self.journal_a,
                       'standby': self.journal_b}[name],
                      'run_boundary', {'run': self.run_count,
                                       'tick': self.tick})

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
        # The endpoint's first `?peer=` announce pull already answered
        # — the owner recorded its hint.
        self.announced = [self.FORGE_ADDR]
        self._hit({'kind': 'announce', 'target': 'ctrl-a:8080',
                   'ok': True})
        return self.forge

    def stop_forge(self):
        self.forge = None

    # ---- the announced-source demote verify -----------------------

    def _verify_pull(self):
        """The demote verify's pull on the forged endpoint: the staged
        document re-read per pull, the serve hit ledgered, the
        signature answered only when the endpoint holds a pair key."""
        if self.forge is None or self.forge_silent:
            return 'dead'
        try:
            json.loads(Path(self.forge['document']).read_text())
        except Exception:
            return 'dead'
        signed = bool(self.forge['keyed']) or self.forge_signed
        self._hit({'kind': 'serve', 'remote': 'ctrl-a:8080',
                   'query': 'prove=%d' % (self.tick + 1)
                   if self.pair_token is not None else '',
                   'signed': signed, 'status': 200})
        return 'ok' if signed else 'unproven'

    def _demote_a(self):
        if self.role['a'] != 'active':
            self._raise(409, 'not_active')
        if self.configured_source:
            # A configured --standby slot covers the demotion: the
            # announced hint is never consulted, and the adoption names
            # the configured peer.
            self._journal_a('tracking_source_adopted',
                            {'source': self.PEER_ADDR})
            self._journal_a('role_changed', {'from': 'active',
                                             'to': 'demoting'})
            self.role['a'] = 'standby'
            self.a_polls = 2
            return 200, {'role': 'demoting', 'tick': self.tick}
        if not self.announced:
            self._raise(409, 'no_tracking_source')
        verdict = 'refused'
        if self.pair_token is not None:
            verdict = self._verify_pull()
        elif self.pull_on_unkeyed:
            verdict = self._verify_pull()
        if verdict == 'unproven' and self.adopt_unproven:
            verdict = 'ok'
        if verdict == 'unproven' and self.adopt_bumped:
            # Only the strictly-ahead replay arms this demotion: the
            # verbatim replay still refuses.
            try:
                staged = json.loads(
                    Path(self.forge['document']).read_text())
            except Exception:
                staged = {}
            tick = staged.get('tick')
            if isinstance(tick, int) and tick > self.tick:
                verdict = 'ok'
        if verdict == 'ok':
            # The forged endpoint armed the demotion: the adoption is
            # journaled naming it and the active walks away.
            self._journal_a('tracking_source_adopted',
                            {'source': self.FORGE_ADDR})
            self._journal_a('role_changed', {'from': 'active',
                                             'to': 'demoting'})
            self.role['a'] = 'standby'
            self.a_polls = 2
            return 200, {'role': 'demoting', 'tick': self.tick}
        if self.journaled_adoption:
            self._journal_a('tracking_source_adopted',
                            {'source': self.FORGE_ADDR})
        if self.role_moved:
            # A refused demote that still walks the role: the field
            # claim rides the run until it settles as a standby.
            self._journal_a('role_changed', {'from': 'active',
                                             'to': 'demoting'})
            self.role['a'] = 'demoting'
            self.a_polls = 2
        if self.pair_token is not None:
            # The keyed probe refusal is durable audit where the build
            # records it.
            self._journal_a('tracking_source_refused',
                            {'source': self.FORGE_ADDR,
                             'detail': "the pull's line proof did "
                                       'not verify under the pair key'})
        self._raise(409, 'wrong_refusal' if self.wrong_refusal
                    else 'no_tracking_source')

    def _promote_a(self):
        if self.role['a'] != 'standby' or self.a_polls is None \
                or self.a_polls > 0:
            self._raise(409, 'not_converged')
        self._journal_a('role_changed', {'from': 'standby',
                                         'to': 'promoting'})
        self.role['a'] = 'promoting'
        self.a_polls = None
        return 200, {'role': 'promoting', 'tick': self.tick}

    # ---- the served documents ------------------------------------

    def _owns_field(self):
        """Whether this run still owns the field — the demoting run
        holds its claim until it settles as a standby, so the served
        `source_owns_field` stamp rides the transition."""
        return self.role['a'] in ('active', 'demoting')

    def _role_a(self):
        if self.no_active:
            return {'role': 'standby', 'tick': self.tick,
                    'sync': 'unsynchronized'}
        if self.role['a'] == 'promoting':
            self.role['a'] = 'active'
            return {'role': 'active', 'tick': self.tick}
        if self.role['a'] == 'demoting':
            self.role['a'] = 'standby'
            return {'role': 'standby', 'tick': self.tick,
                    'sync': 'unsynchronized'}
        report = {'role': self.role['a'], 'tick': self.tick}
        if self.role['a'] == 'standby':
            if self.a_polls is not None:
                self.a_polls -= 1
                if self.a_polls <= 0:
                    report['sync'] = {'orphaned': {'aligned': self.tick}}
                    return report
            report['sync'] = 'unsynchronized'
        return report

    def _role_b(self):
        report = {'role': self.role['b'], 'tick': self.tick}
        if self.role['b'] == 'standby':
            if self.b_track_left > 0 and not (
                    self.never_rejoin and self.rejoined):
                self.b_track_left -= 1
            if self.b_track_left == 0 and self.dual_armed:
                # The resumed peer takes the field too: the refusal left
                # the pair with two actives.
                self.role['b'] = 'active'
                return {'role': 'active', 'tick': self.tick}
            report['sync'] = {'tracking': {'aligned': self.tick}} \
                if self.b_track_left == 0 and not self.no_tracking \
                else 'unsynchronized'
        return report

    def _checkpoint_a(self):
        document = {
            'format_version': 1,
            'model_fingerprint': 'announced-source-fp',
            'generation': self.GENERATION,
            'tick': self.tick,
            'components': {}, 'driver': None, 'outputs': {},
            'internal': {'10': {'value': {'bool': False},
                                'quality': 'good', 'tick': self.tick}},
            'forces': {}, 'receipts': [],
            'command_admission': {'attempts': 0, 'full_rejections': 0,
                                  'high_water': 0},
            'source_owns_field': self._owns_field(),
            'line_owner': 'ctrl-a:8080'
            if self._owns_field() else None}
        if self.no_field_stamp:
            document.pop('source_owns_field')
        return document

    # ---- the endpoint dispatch ------------------------------------

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        host = url.split('/')[2]
        peer = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}[host]
        if not self.up[peer]:
            raise urllib.error.URLError('connection refused')
        self.tick += 1
        # The field claim drops from the announced forged replay
        # onward — the endpoint is up, so the demote has already run.
        if self.drop_claim and peer == 'a' and self.forge is not None \
                and method == 'GET' and self.role['a'] == 'active':
            self.role['a'] = 'demoting'
        path = '/' + url.split('/', 3)[3]
        route = path.partition('?')[0]
        if (method, route) == ('GET', '/role'):
            return 200, self._role_a() if peer == 'a' \
                else self._role_b()
        if (method, route) == ('GET', '/checkpoint'):
            if self.dropped_reads and peer == 'a' \
                    and self.forge is not None:
                raise urllib.error.URLError('connection reset')
            return 200, self._checkpoint_a() if peer == 'a' else {
                'format_version': 1,
                'model_fingerprint': 'announced-source-fp',
                'generation': self.GENERATION, 'tick': self.tick,
                'receipts': [], 'command_admission': {
                    'attempts': 0, 'full_rejections': 0,
                    'high_water': 0},
                'source_owns_field': False}
        if (method, route) == ('POST', '/demote'):
            if peer == 'a':
                return self._demote_a()
            if self.role['b'] != 'active':
                self._raise(409, 'not_active')
            self._raise(409, 'no_tracking_source')
        if (method, route) == ('POST', '/promote'):
            if peer == 'a':
                return self._promote_a()
            if self.role['b'] == 'active':
                self._raise(409, 'already_active')
            self._raise(409, 'not_converged')
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class AnnouncedSourceTests(unittest.TestCase):
    """The announced-source-verify leg against the stubbed pair: a
    clean unkeyed or keyed rig passes with identical digests — the
    announced forged replay and its strictly-ahead sibling each
    refusing no_tracking_source while the active keeps its role and
    field claim — each doctored defect reports the named diagnostic,
    the forged announce arming an unkeyed demotion names the
    pre-contract signature instead, and an unconverged, unreachable,
    pre-contract, or forge-less run is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = AnnouncedSourceFeed(self.tmp.name)

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
               'stop_controller': feed.stop_controller,
               'start_controller': feed.start_controller,
               'restart_controller': feed.restart_controller,
               'start_forge': feed.start_forge,
               'stop_forge': feed.stop_forge}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, **overrides):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'ANNOUNCED_SETTLE', 1.0), \
                patch.object(scenarios, 'ANNOUNCED_POLL', 0.001), \
                patch.object(scenarios, 'ANNOUNCED_HINT_SETTLE', 1.0):
            return scenarios.scenario_announced_source_verify(
                self.ctx(feed, **overrides))

    def passed_evidence(self, name='announced-source-verify-pass-1'
                              '.json'):
        return json.loads((self.evidence / name).read_text())

    def details(self, key):
        """The pass evidence's per-clause violation detail — every
        clause the run broke, keyed leg-and-clause, so a doctored
        defect is nameable even where the report's summary detail
        carries only the first few."""
        return self.passed_evidence()['details'][key]

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The leg's window: behind the forged-standby demote-verify
        # leg, still inside the launch-layout window the tune case's
        # a->b switch closes.
        self.assertLess(
            order.index(
                scenarios.scenario_demote_forged_standby_source),
            order.index(scenarios.scenario_announced_source_verify))
        self.assertLess(
            order.index(scenarios.scenario_announced_source_verify),
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(
            verify.case_function('announced-source-verify'),
            scenarios.scenario_announced_source_verify)

    def test_unkeyed_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('announced-source-verify-pass-1.json',
                     'announced-source-verify-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        first = self.passed_evidence()
        second = self.passed_evidence(
            'announced-source-verify-pass-2.json')
        self.assertEqual(first['digest'], second['digest'])
        self.assertEqual(
            first['digest'],
            {'announce': 'landed', 'replay': 'refused',
             'bumped': 'refused', 'adoption': 'none',
             'field': 'kept', 'verify': 'no-pull',
             'roles': 'restored'})
        self.assertEqual(first['posture'], 'unkeyed')
        for label in ('replay', 'bumped'):
            leg = first['legs'][label]
            self.assertEqual(leg['demote']['status'], 409, label)
            self.assertEqual(leg['demote']['body'],
                             'no_tracking_source', label)
            self.assertEqual(leg['verify_pulls'], [], label)
            self.assertEqual(leg['journaled_adoptions'], [], label)
            self.assertEqual(leg['journaled_role_changes'], [], label)
            self.assertEqual(leg['durable_adoptions'], [], label)
            self.assertEqual(leg['role_after']['role'], 'active', label)
            self.assertTrue(leg['document_after']['source_owns_field'],
                            label)
        self.assertEqual(first['legs']['replay']['forgery']['keys'],
                         ['source_owns_field'])
        self.assertEqual(first['legs']['bumped']['forgery']['keys'],
                         ['source_owns_field', 'tick'])
        report.validate_scenario(record)

    def test_keyed_rig_passes_and_validates(self):
        feed = AnnouncedSourceFeed(self.tmp.name, keyed=True)
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'passed', record)
        first = self.passed_evidence()
        self.assertEqual(first['posture'], 'keyed')
        self.assertEqual(first['digest']['verify'], 'unsigned-pull')
        for label in ('replay', 'bumped'):
            leg = first['legs'][label]
            self.assertEqual(leg['verify_pulls'][0].get('signed'), False,
                             label)
            self.assertTrue(leg['verify_pulls'][0]['query'].startswith(
                'prove='), label)
            self.assertEqual(leg['refusals'][0]['source'],
                             feed.FORGE_ADDR, label)
        report.validate_scenario(record)

    def test_adopted_forged_document_fails(self):
        # The doctor's negative: the forged announce arms the demotion
        # on a keyed pair, where the proof gate refused this shape long
        # before #867 — a contract failure, not a pre-contract run.
        self.feed.pair_token = 'test-pair'
        self.feed.adopt_unproven = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'announced-source-verify-failed'), record['detail'])
        self.assertIn('no_tracking_source', record['detail'])
        first = self.passed_evidence()
        self.assertEqual(first['legs']['replay']['demote']['status'], 200)
        self.assertEqual(
            first['legs']['replay']['journaled_adoptions'],
            [{'source': self.feed.FORGE_ADDR}])
        self.assertEqual(first['digest']['replay'], 'adopted')
        report.validate_scenario(record)

    def test_bumped_replay_alone_arming_fails(self):
        # Only the strictly-ahead replay arms the demotion: the leg's
        # two documents are verified separately, so a refusal on the
        # verbatim replay never masks the bumped leg's own verdict.
        self.feed.pair_token = 'test-pair'
        self.feed.adopt_bumped = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'announced-source-verify-failed'), record['detail'])
        first = self.passed_evidence()
        self.assertEqual(first['digest']['replay'], 'refused')
        self.assertEqual(first['digest']['bumped'], 'adopted')
        self.assertEqual(first['legs']['bumped']['demote']['status'], 200)
        self.assertEqual(
            first['legs']['replay']['verify_pulls'][0].get('signed'),
            False)
        report.validate_scenario(record)

    def test_adopted_forged_document_predates_unkeyed(self):
        # The same observation on an unkeyed pair is the pre-contract
        # signature: the bare hint earned a verify pull and its forged
        # document armed the demotion, exactly the shape every build
        # before #867's fix presents, so the leg names it rather than a
        # defect.
        self.feed.pull_on_unkeyed = True
        self.feed.adopt_unproven = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('announced-source-verify-predates',
                      record['detail'])
        self.assertIn('predates the contract', record['detail'])
        first = self.passed_evidence()
        self.assertEqual(first['legs']['replay']['demote']['status'], 200)
        report.validate_scenario(record)

    def test_released_field_claim_fails(self):
        self.feed.drop_claim = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('released its field claim',
                      self.details('replay:field-claim'))
        report.validate_scenario(record)

    def test_wrong_refusal_verdict_fails(self):
        self.feed.wrong_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'announced-source-verify-failed'), record['detail'])
        self.assertIn('wrong_refusal',
                      self.details('replay:demote-answer'))
        report.validate_scenario(record)

    def test_journaled_adoption_on_refusal_fails(self):
        self.feed.journaled_adoption = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journaled a tracking-source adoption',
                      self.details('replay:adoption'))
        report.validate_scenario(record)

    def test_role_changed_on_refusal_fails(self):
        self.feed.role_moved = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role change',
                      self.details('replay:role-change'))
        report.validate_scenario(record)

    def test_durable_adoption_naming_the_forge_fails(self):
        # A pin an earlier demotion left in the durable file: outside
        # the pass's journal window, inside the whole-file audit.
        self.feed._journal(self.feed.journal_a, 'tracking_source_adopted',
                           {'source': self.feed.FORGE_ADDR},
                           tick=self.feed.tick - 5)
        self.feed.journaled_adoption = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('durable journal names the forged endpoint',
                      self.details('replay:durable-adoption'))
        first = self.passed_evidence()
        self.assertEqual(first['legs']['replay']['journaled_adoptions'],
                         [])
        self.assertEqual(first['legs']['replay']['durable_adoptions'],
                         [self.feed.FORGE_ADDR])
        report.validate_scenario(record)

    def test_unkeyed_verify_pull_fails(self):
        # A bare hint on an unkeyed run earns not even a verify pull.
        self.feed.pull_on_unkeyed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('earns not even a verify pull',
                      self.details('replay:verify-pull'))
        report.validate_scenario(record)

    def test_keyed_verify_never_pulled_fails(self):
        self.feed.pair_token = 'test-pair'
        self.feed.forge_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ledgered no verify pull',
                      self.details('replay:verify-pull'))
        report.validate_scenario(record)

    def test_signed_forged_pull_fails(self):
        # The tokenless endpoint answering signed: the proof gate this
        # leg exercises never ran.
        self.feed.pair_token = 'test-pair'
        self.feed.forge_signed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('answered a signed pull',
                      self.details('replay:verify-pull'))
        report.validate_scenario(record)

    def test_unstaged_forgery_fails(self):
        # A staging that is not the victim's replay — the forged shape
        # the leg names never reached the endpoint.
        def stripped(victim):
            document = dict(victim)
            document.pop('internal')
            document['source_owns_field'] = False
            return document
        with patch.object(scenarios, '_replay_document', stripped):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('replay this leg stages changes',
                      self.details('replay:shape'))
        report.validate_scenario(record)

    def test_unrestored_pair_fails(self):
        # The resumed peer never re-tracks behind the refused demotion.
        self.feed.never_rejoin = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never re-joined tracking',
                      self.details('restore:tracking'))
        report.validate_scenario(record)

    def test_dual_active_pair_fails(self):
        self.feed.dual_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('did not reconverge to one active',
                      self.details('restore:actives'))
        report.validate_scenario(record)

    def test_silent_forge_fails(self):
        # The endpoint's announce never lands: nothing stood for the
        # demote to refuse.
        class SilentForge(AnnouncedSourceFeed):
            def start_forge(self, document, owner, keyed=True):
                forge = super().start_forge(document, owner, keyed)
                Path(forge['hits']).write_text('')
                self.announced = []
                return forge
        record = self.run_scenario(feed=SilentForge(self.tmp.name))
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('announce never landed',
                      self.details('announce'))
        report.validate_scenario(record)

    def test_unreachable_pair_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
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

    def test_missing_forge_action_reports_inconclusive(self):
        record = self.run_scenario(start_forge=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('start_forge', record['detail'])
        report.validate_scenario(record)

    def test_host_placed_forge_reports_inconclusive(self):
        record = self.run_scenario(
            endpoint_placement={'forge': 'host'})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('bridge', record['detail'])
        report.validate_scenario(record)

    def test_missing_journal_files_reports_inconclusive(self):
        record = self.run_scenario(journal_files={'active': '',
                                                   'standby': ''})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal files', record['detail'])
        report.validate_scenario(record)

    def test_pre_contract_checkpoint_reports_inconclusive(self):
        self.feed.no_field_stamp = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates the announced-source ownership stamps',
                      record['detail'])
        report.validate_scenario(record)

    def test_configured_source_reports_inconclusive(self):
        # The field owner carries a configured --standby slot: its
        # demotion path is that source, not the announced hint the leg
        # stages, so the leg never claimed to exercise it.
        self.feed.configured_source = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('configured --standby source', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'roles': 'restored'}, {}, {'pass': 1}),
                       ({'roles': 'unrestored'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_announced_verify_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'announced-source-verify-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_unchecked_diagnostic_self_check_reports_by_name(self):
        # The self-check leg: the clean pass already proves no
        # planted negative slips with every judge live (a slip fails
        # the run), and here the refusal judge is deaf, so exactly the
        # negatives it must name are reported under
        # announced-source-verify-unchecked.
        passes = iter([
            ({'announce': 'landed', 'replay': 'refused',
              'bumped': 'refused', 'adoption': 'none',
              'field': 'kept', 'verify': 'no-pull',
              'roles': 'restored'}, {}, {'pass': 1}),
            ({'announce': 'landed', 'replay': 'refused',
              'bumped': 'refused', 'adoption': 'none',
              'field': 'kept', 'verify': 'no-pull',
              'roles': 'restored'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_announced_verify_pass',
                          lambda *a: next(passes)), \
                patch.object(scenarios, '_judge_announced_refusal',
                             lambda record, keyed, port, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'announced-source-verify-unchecked'), record['detail'])
        for name in ('forged-announce-adopted', 'field-claim-released',
                     'silent-announce', 'durable-adoption',
                     'unkeyed-verify-pulled', 'keyed-verify-unpulled'):
            self.assertIn(name, record['detail'])
        self.assertNotIn('unflipped-replay', record['detail'])
        self.assertNotIn('pair-not-reconverged', record['detail'])
        report.validate_scenario(record)

    def test_dropped_read_reports_inconclusive(self):
        # A served checkpoint read that drops is a lost observation,
        # never the refusal verdict.
        self.feed.dropped_reads = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('read dropped', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for index in range(2):
            feed = AnnouncedSourceFeed(self.tmp.name)
            evidence = Path(self.tmp.name) / ('run' + str(index))
            evidence.mkdir()
            self.evidence = evidence
            record = self.run_scenario(feed=feed)
            runs.append((record, {path.name: path.read_text()
                                  for path in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()