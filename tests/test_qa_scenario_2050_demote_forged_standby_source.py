"""The 2050_demote_forged_standby_source leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_demote_forged_standby_source, split out per the #940
convention. The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'ForgedStandbyTests.test_registered',
    'ForgedStandbyTests.test_clean_rig_passes_and_validates',
    'ForgedStandbyTests.test_adopted_unproven_document_fails',
    'ForgedStandbyTests.test_adopted_receipt_fork_fails',
    'ForgedStandbyTests.test_adopted_planted_internal_fails',
    'ForgedStandbyTests.test_journaled_adoption_on_refusal_fails',
    'ForgedStandbyTests.test_role_moved_on_refusal_fails',
    'ForgedStandbyTests.test_wrong_refusal_verdict_fails',
    'ForgedStandbyTests.test_refused_honest_document_fails',
    'ForgedStandbyTests.test_stranded_adoption_fails',
    'ForgedStandbyTests.test_refused_promote_fails',
    'ForgedStandbyTests.test_silent_forge_fails',
    'ForgedStandbyTests.test_no_active_reports_failed',
    'ForgedStandbyTests.test_unconverged_pair_reports_inconclusive',
    'ForgedStandbyTests.test_unreachable_pair_reports_inconclusive',
    'ForgedStandbyTests.test_unkeyed_run_reports_inconclusive',
    'ForgedStandbyTests.'
    'test_unkeyed_deployed_pair_runs_on_the_probe_pair',
    'ForgedStandbyTests.test_missing_forge_action_reports_inconclusive',
    'ForgedStandbyTests.test_host_placed_forge_reports_inconclusive',
    'ForgedStandbyTests.test_diverging_digests_report_nondeterministic',
    'ForgedStandbyTests.test_two_runs_produce_identical_evidence',
})


class ForgedStandbyFeed:
    """A stubbed pair for the forged-standby-source scenario: ctrl-a
    owns the field with no configured tracking source and a cleared
    announced-hint set after each warm restart; ctrl-b is the tracking
    standby the leg stops to open the announced-only window; and the
    `start_forge`/`stop_forge` ctx actions stand the hostile endpoint
    on the rig bridge — the staged document it serves is the file the
    action writes, the announce it lands is the hit record the action
    appends, and the demote verify's `?prove=` pull reads the file and
    answers signed exactly when the endpoint launched keyed.

    The demote-verify half models the #863 contract: a keyed run with
    only announced hints pulls each one, refuses `no_tracking_source`
    when no answer is signed, and audits a signed document's receipt
    window against the owner's settled log and its internal `In`
    samples against the held image plus the receipted writes — a
    receipt claiming a different settled command is the fork, an
    internal sample claiming a value no settled verdict produced is
    the plant. Every endpoint call is one completed scan, so two runs
    emit identical evidence. Doctor flags stage each named defect the
    issue calls out."""

    FORGE_ADDR = '172.18.0.9:8090'
    FORGE_PORT = 8090
    POINT = 10
    GENERATION = 777777

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.tick = 100
        self.up = {'a': True, 'b': True}
        self.role = {'a': 'active', 'b': 'standby'}
        # ctrl-b's configured standby pull: tracking once restarted.
        self.b_track_left = 0
        # ctrl-a's adopted-source convergence countdown: None until an
        # adoption follows the announced forge, then polls to orphaned.
        self.a_orphan_polls = None
        self.receipts = []        # ctrl-a's settled log
        self.attempts = 0         # lifetime submissions — the
                                  # log's high-water mark
        self.pending = []         # (apply_tick, receipt) accepted writes
        self.image = {self.POINT: False}
        self.announced = []       # the owner monitor's recorded hints
        self.run_count = 1
        self.seq = 0
        self.forge = None
        self.pair_token = 'test-pair'
        self.journal_a = self.tmp / 'journal-a.jsonl'
        self.journal_b = self.tmp / 'journal-b.jsonl'
        self.journal_a.write_text(json.dumps(
            {'run_boundary': {'run': 1, 'tick': self.tick}}) + '\n')
        self.journal_b.write_text(json.dumps(
            {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
        # Fault injection — each named failure the issue calls out.
        self.adopted_unproven = False  # an unsigned answer arms the
                                       # demotion — the proof gate
                                       # regressed
        self.adopted_convicted = False  # a document the audit
                                        # convicts still arms the
                                        # demotion — the #863
                                        # regression
        self.journaled_adoption = False  # a refused demote still
                                         # journals the adoption
        self.role_moved = False       # a refused demote still demotes
        self.wrong_refusal = False    # the refusal carries another
                                      # verdict than no_tracking_source
        self.honest_refused = False   # the honest document is refused
                                      # — the follow-peer path closed
        self.never_converges = False  # the adopted peer strands
                                      # unsynchronized
        self.promote_refused = False  # the reconverged peer cannot
                                      # promote back
        self.forge_silent = False     # the staged document never
                                      # answers a pull — the endpoint
                                      # crashed serving it
        self.unreachable = False      # the monitors never answer
        self.no_active = False        # no peer reports role=active
        self.no_tracking = False      # the standby never converges

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

    def restart_controller(self, name):
        # The warm restart: the journal carries the run boundary, the
        # persisted state resumes — role, receipts, image — and the
        # fresh monitor's announced-hint set begins empty.
        self.run_count += 1
        self.announced = []
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
        # The endpoint's first announce pull already answered — the
        # owner recorded its hint.
        self.announced = [self.FORGE_ADDR]
        self._hit({'kind': 'announce', 'target': 'ctrl-a:8080',
                   'ok': True})
        return self.forge

    def stop_forge(self):
        self.forge = None

    # ---- the scan advance ----------------------------------------

    def _advance(self):
        self.tick += 1
        settled = []
        for apply_tick, receipt in self.pending:
            if self.tick >= apply_tick:
                write = receipt['command']['write_value']
                self.image[write['point']] = write['value']['bool']
                receipt['outcome'] = {'applied': {'tick': self.tick}}
                self._journal_a('command_settled', {'receipt': receipt})
                settled.append((apply_tick, receipt))
        for item in settled:
            self.pending.remove(item)

    # ---- the announced-source demote verify -----------------------

    def _receipted(self, receipts, doc_base, point):
        """The newest applied write `point` in the pulled document's
        window that post-dates the owner's settled tail — the
        receipted-cause override the internal audit consults."""
        own_base = self.attempts - len(self.receipts)
        for offset in range(len(receipts) - 1, -1, -1):
            if doc_base + offset < own_base:
                continue
            receipt = receipts[offset]
            if 'applied' not in (receipt.get('outcome') or {}):
                continue
            write = (receipt.get('command') or {}).get('write_value')
            if isinstance(write, dict) and write.get('point') == point:
                return (write.get('value') or {}).get('bool')
        return None

    def _audit(self, document):
        """The mini command-record audit the demote verify runs on a
        signed pull: the receipt window must agree with the owner's
        settled log at covered indices, and every internal `In` sample
        must match the held image or a receipted write the owner never
        produced convicts it."""
        receipts = document.get('receipts') or []
        attempts = (document.get('command_admission') or {}) \
            .get('attempts') or 0
        doc_base = attempts - len(receipts)
        own_base = self.attempts - len(self.receipts)
        for offset, receipt in enumerate(receipts):
            position = doc_base + offset - own_base
            if not 0 <= position < len(self.receipts):
                continue
            own = self.receipts[position]
            if own.get('command') != receipt.get('command') \
                    and 'accepted' not in (own.get('outcome') or {}):
                return 'fork'
        if doc_base + len(receipts) < own_base + len(self.receipts):
            return 'ok'   # a stale view skips the internal audit
        for key, sample in (document.get('internal') or {}).items():
            point = int(key)
            value = ((sample or {}).get('value') or {}).get('bool')
            held = self.image.get(point)
            if isinstance(value, bool) and held is not None \
                    and value != held \
                    and self._receipted(receipts, doc_base, point) \
                    != value:
                return 'planted'
        return 'ok'

    def _verify_pull(self):
        """The demote verify's `?prove=` pull on the announced forge:
        the staged document re-read per pull, the serve hit ledgered,
        the keyed signature answered only when the endpoint holds the
        pair's token — then the audit."""
        if self.forge is None or self.forge_silent:
            return 'dead'
        try:
            document = json.loads(
                Path(self.forge['document']).read_text())
        except Exception:
            return 'dead'   # a pull the endpoint never served leaves
                            # no ledger record
        signed = bool(self.forge['keyed'])
        self._hit({'kind': 'serve', 'remote': 'ctrl-a:8080',
                   'query': 'prove=%d' % (self.tick + 1),
                   'signed': signed, 'status': 200})
        if not signed:
            return 'ok' if self.adopted_unproven else 'unproven'
        if document.get('generation') != self.GENERATION:
            return 'foreign'
        verdict = self._audit(document)
        if verdict != 'ok' and self.adopted_convicted:
            return 'ok'
        return verdict

    def _demote_a(self):
        if self.role['a'] != 'active':
            self._raise(409, 'not_active')
        if not self.announced:
            self._raise(409, 'no_tracking_source')
        verdict = self._verify_pull()
        if self.honest_refused and verdict == 'ok':
            verdict = 'unproven'
        if verdict != 'ok':
            if self.journaled_adoption:
                self._journal_a('tracking_source_adopted',
                                {'source': self.FORGE_ADDR})
            if self.role_moved:
                self._journal_a('role_changed', {
                    'from': 'active', 'to': 'demoting'})
                self.role['a'] = 'standby'
            self._raise(409, 'not_active' if self.wrong_refusal
                        else 'no_tracking_source')
        # The verified adoption: the journaled source, the role
        # transition, and the demoted peer following the announced
        # endpoint's pulls.
        self._journal_a('tracking_source_adopted',
                        {'source': self.FORGE_ADDR})
        self._journal_a('role_changed', {'from': 'active',
                                         'to': 'demoting'})
        self.role['a'] = 'standby'
        self.a_orphan_polls = 2
        return 200, {'role': 'demoting', 'tick': self.tick}

    def _promote_a(self):
        # Promotable only from a converged adoption — orphaned counts.
        if self.role['a'] != 'standby' \
                or self.a_orphan_polls is None \
                or self.a_orphan_polls > 0 \
                or self.promote_refused:
            self._raise(409, 'not_converged')
        self._journal_a('role_changed', {'from': 'standby',
                                         'to': 'promoting'})
        self.role['a'] = 'promoting'
        self.a_orphan_polls = None
        return 200, {'role': 'promoting', 'tick': self.tick}

    # ---- the served documents ------------------------------------

    def _role_a(self):
        if self.no_active:
            return {'role': 'standby', 'tick': self.tick,
                    'sync': 'unsynchronized'}
        if self.role['a'] == 'promoting':
            self.role['a'] = 'active'
            return {'role': 'active', 'tick': self.tick}
        report = {'role': self.role['a'], 'tick': self.tick}
        if self.role['a'] == 'standby':
            if self.a_orphan_polls is not None and not \
                    self.never_converges:
                self.a_orphan_polls -= 1
                if self.a_orphan_polls <= 0:
                    report['sync'] = {'orphaned': {'aligned':
                                                   self.tick}}
                    return report
            report['sync'] = 'unsynchronized'
        return report

    def _role_b(self):
        report = {'role': self.role['b'], 'tick': self.tick}
        if self.role['b'] == 'standby':
            if self.b_track_left > 0:
                self.b_track_left -= 1
            report['sync'] = {'tracking': {'aligned': self.tick}} \
                if self.b_track_left == 0 and not self.no_tracking \
                else 'unsynchronized'
        return report

    def _checkpoint_a(self):
        return {
            'format_version': 1,
            'model_fingerprint': 'demote-forged-fp',
            'generation': self.GENERATION,
            'tick': self.tick,
            'components': {}, 'driver': None, 'outputs': {},
            'internal': {str(self.POINT): {
                'value': {'bool': self.image[self.POINT]},
                'quality': 'good', 'tick': self.tick}},
            'forces': {},
            'receipts': list(self.receipts),
            'command_admission': {'attempts': self.attempts,
                                  'full_rejections': 0,
                                  'high_water': 0},
            'source_owns_field': self.role['a'] == 'active',
            'line_owner': 'ctrl-a:8080'
            if self.role['a'] == 'active' else None}

    def _admit_a(self, body):
        self.attempts += 1
        write = (body or {}).get('command', {}).get('write_value', {})
        if self.role['a'] == 'active' and write.get('point') \
                == self.POINT:
            receipt = {'command': body['command'],
                       'actor': body.get('actor'),
                       'outcome': {'accepted': {
                           'apply_tick': self.tick + 1}}}
            self.pending.append((self.tick + 1, receipt))
        else:
            receipt = {'command': (body or {}).get('command'),
                       'actor': (body or {}).get('actor'),
                       'outcome': {'rejected': {'reason': {
                           'not_active': {'point': write.get('point'),
                                          'role': self.role['a']}}}}}
            self._journal_a('command_settled', {'receipt': receipt})
        self.receipts.append(receipt)
        return receipt

    # ---- the endpoint dispatch ------------------------------------

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        host = url.split('/')[2]
        peer = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}[host]
        if not self.up[peer]:
            raise urllib.error.URLError('connection refused')
        self._advance()
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if (method, route) == ('GET', '/role'):
            return 200, self._role_a() if peer == 'a' \
                else self._role_b()
        if (method, route) == ('GET', '/signals'):
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
            return 200, self._checkpoint_a() if peer == 'a' else {
                'format_version': 1,
                'model_fingerprint': 'demote-forged-fp',
                'generation': self.GENERATION, 'tick': self.tick,
                'receipts': [], 'command_admission': {
                    'attempts': 0, 'full_rejections': 0,
                    'high_water': 0},
                'source_owns_field': False}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts) if peer == 'a' else []
        if (method, route) == ('POST', '/command'):
            if peer == 'a':
                return 200, self._admit_a(body)
            self._raise(409, 'not_active')
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


class ForgedStandbyTests(unittest.TestCase):
    """The forged-standby-source leg against the stubbed pair: a clean
    rig passes with identical digests and evidence — the unproven,
    receipt-fork, and planted-internal documents each refusing
    no_tracking_source while the honest document adopts and
    reconverges — each doctored defect reports the named diagnostic,
    and an unconverged, unreachable, unkeyed, or forge-less run is
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = ForgedStandbyFeed(self.tmp.name)

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
                patch.object(scenarios, 'DEMOTE_FORGED_SETTLE', 1.0), \
                patch.object(scenarios, 'DEMOTE_FORGED_POLL', 0.001), \
                patch.object(scenarios, 'FORGE_ANNOUNCE_SETTLE', 1.0):
            return scenarios.scenario_demote_forged_standby_source(
                self.ctx(feed, **overrides))

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The announced-source leg's window: behind the peer-announce
        # leg, still inside the launch-layout window the tune case's
        # a->b switch closes.
        self.assertLess(
            order.index(scenarios.scenario_peer_announce),
            order.index(
                scenarios.scenario_demote_forged_standby_source))
        self.assertLess(
            order.index(
                scenarios.scenario_demote_forged_standby_source),
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(verify.case_function(
            'demote-forged-standby-source'),
            scenarios.scenario_demote_forged_standby_source)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('demote-forged-standby-signals.json',
                     'demote-forged-standby-pass-1.json',
                     'demote-forged-standby-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads((self.evidence / name).read_text())
                  for name in ('demote-forged-standby-pass-1.json',
                               'demote-forged-standby-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'unproven': 'refused', 'receipt_fork': 'refused',
             'planted_internal': 'refused', 'honest': 'adopted',
             'roles': 'restored'})
        legs = passes[0]['legs']
        for key in ('unproven', 'receipt_fork', 'planted_internal'):
            self.assertEqual(legs[key]['demote']['status'], 409, key)
            self.assertEqual(legs[key]['demote']['body'],
                             'no_tracking_source', key)
            self.assertEqual(legs[key]['journaled_adoptions'], [], key)
            self.assertEqual(legs[key]['journaled_role_changes'],
                             [], key)
        self.assertFalse(legs['unproven']['verify_pulls'][0]
                         .get('signed'))
        self.assertTrue(legs['receipt_fork']['verify_pulls'][0]
                        .get('signed'))
        self.assertEqual(legs['honest']['demote']['status'], 200)
        self.assertEqual(len(legs['honest']['journaled_adoptions']),
                         1)
        self.assertTrue(legs['honest']['journaled_adoptions'][0]
                        ['source'].endswith(':8090'))
        report.validate_scenario(record)

    def test_adopted_unproven_document_fails(self):
        # The unsigned endpoint's document arms the demotion — the
        # proof gate regressed.
        self.feed.adopted_unproven = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-forged-standby-failed'), record['detail'])
        self.assertIn('no_tracking_source', record['detail'])
        report.validate_scenario(record)

    def test_adopted_receipt_fork_fails(self):
        # The keyed endpoint's convicted documents arm the demotion —
        # the receipt-window audit regressed, so the fork leg is the
        # first adoption the pass records.
        self.feed.adopted_convicted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-forged-standby-failed'), record['detail'])
        self.assertIn('no_tracking_source', record['detail'])
        legs = json.loads((self.evidence
                           / 'demote-forged-standby-pass-1.json')
                          .read_text())['legs']
        self.assertEqual(legs['receipt_fork']['demote']['status'],
                         200)
        self.assertEqual(len(legs['receipt_fork']
                             ['journaled_adoptions']), 1)
        report.validate_scenario(record)

    def test_adopted_planted_internal_fails(self):
        class OnlyPlantedAdopts(ForgedStandbyFeed):
            """Adopt the staged document only when the internal audit
            convicts it — the planted-`In` check regressed while the
            receipt fork still refuses."""
            def _audit(self, document):
                verdict = super()._audit(document)
                return 'ok' if verdict == 'planted' else verdict
        record = self.run_scenario(
            feed=OnlyPlantedAdopts(self.tmp.name))
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-forged-standby-failed'), record['detail'])
        self.assertIn('no_tracking_source', record['detail'])
        legs = json.loads((self.evidence
                           / 'demote-forged-standby-pass-1.json')
                          .read_text())['legs']
        self.assertEqual(legs['planted_internal']['demote']['status'],
                         200)
        report.validate_scenario(record)

    def test_journaled_adoption_on_refusal_fails(self):
        self.feed.journaled_adoption = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-forged-standby-failed'), record['detail'])
        self.assertIn('journaled a tracking-source adoption',
                      record['detail'])
        report.validate_scenario(record)

    def test_role_moved_on_refusal_fails(self):
        self.feed.role_moved = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-forged-standby-failed'), record['detail'])
        report.validate_scenario(record)

    def test_wrong_refusal_verdict_fails(self):
        self.feed.wrong_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-forged-standby-failed'), record['detail'])
        self.assertIn('no_tracking_source', record['detail'])
        report.validate_scenario(record)

    def test_refused_honest_document_fails(self):
        self.feed.honest_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-forged-standby-failed'), record['detail'])
        self.assertIn('follow-peer', record['detail'])
        report.validate_scenario(record)

    def test_stranded_adoption_fails(self):
        self.feed.never_converges = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-forged-standby-failed'), record['detail'])
        self.assertIn('reconverged', record['detail'])
        report.validate_scenario(record)

    def test_refused_promote_fails(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-forged-standby-failed'), record['detail'])
        self.assertIn('promote', record['detail'])
        report.validate_scenario(record)

    def test_silent_forge_fails(self):
        # The verify pull reaches no endpoint — the refusal evidence
        # has no serve record to stand on.
        self.feed.forge_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-forged-standby-failed'), record['detail'])
        self.assertIn('no checkpoint pull', record['detail'])
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

    def test_unreachable_pair_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_unkeyed_run_reports_inconclusive(self):
        self.feed.pair_token = None
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('pair-token', record['detail'])
        report.validate_scenario(record)

    def test_unkeyed_deployed_pair_runs_on_the_probe_pair(self):
        # The deployed pair carries no --pair-token; the lane-staged
        # probe pair the ctx['probe'] subject names is keyed — the
        # leg exercises the contract on it and reports a real
        # verdict instead of a capability skip (#1058).
        record = self.run_scenario(pair_token=None,
                                   probe=self.ctx())
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertIn('probe pair',
                      ' '.join(record['observations']))
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

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'roles': 'restored'}, {}, {'pass': 1}),
                       ({'roles': 'unchanged'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_demote_forged_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-forged-standby-nondeterministic'),
            record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            feed = ForgedStandbyFeed(self.tmp.name)
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.evidence = evidence
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
