"""The 0800_fenced_writer_degrade leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_fenced_writer_degrade, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'FencedWriterDegradeTests.test_registered_in_scenarios',
    'FencedWriterDegradeTests.test_passed',
    'FencedWriterDegradeTests.test_fails_when_the_superseded_peer_exits',
    'FencedWriterDegradeTests.test_fails_when_the_fenced_owner_never_demotes',
    'FencedWriterDegradeTests.test_fails_when_the_walk_skips_demoting',
    'FencedWriterDegradeTests.test_fails_when_the_preemption_is_silent',
    'FencedWriterDegradeTests.test_fails_on_a_second_process_lifetime',
    'FencedWriterDegradeTests.test_fails_when_the_misordered_promote_refuses',
    'FencedWriterDegradeTests.test_fails_when_the_promotion_claims_nothing',
    'FencedWriterDegradeTests.test_fails_when_the_promoted_peer_is_fenced',
    'FencedWriterDegradeTests.test_fails_when_the_command_is_not_not_active',
    'FencedWriterDegradeTests.test_fails_when_the_refused_write_reaches_the_field',
    'FencedWriterDegradeTests.test_fails_when_the_demoted_writer_never_reconverges',
    'FencedWriterDegradeTests.test_inconclusive_without_a_plant',
    'FencedWriterDegradeTests.test_inconclusive_without_journal_paths',
    'FencedWriterDegradeTests.test_inconclusive_without_owner_tokens',
    'FencedWriterDegradeTests.test_inconclusive_when_the_peer_never_tracks',
    'FencedWriterDegradeTests.test_inconclusive_when_the_pair_is_down',
    'FencedWriterDegradeTests.test_fails_when_nothing_is_settled',
    'FencedWriterDegradeTests.test_two_runs_produce_identical_evidence',
})


class DegradePlantPeer(ClaimPlantPeer):
    """The claim-enforcing plant extended with the writable bool
    in-point the fenced-writer-degrade scenario's command legs
    target."""

    def __init__(self):
        super().__init__()
        self.samples[10] = {'value': {'bool': False},
                            'quality': 'good', 'tick': 0}
        self.directions[10] = 'in'


class FencedDegradeFeed:
    """A stubbed pair for the fenced-writer-degrade scenario. ctrl-a
    launches active holding the plant's writer claim under TOKEN_A
    through its own sim-net attachment; ctrl-b tracks it. Every
    request on a peer is one completed scan on that peer: a
    field-owning scan writes the field through its claim, and a
    fenced answer drives the contract's degrade path — one
    field_claim_lost and the role_changed walk onto the peer's own
    append-only --journal-file — while POST /promote takes the
    unconditional claim that preempts the standing owner, POST
    /demote walks the documented demotion, and POST /command settles
    the receipted verdicts — applied at the next boundary on the
    field owner, rejected:not_active at admission on any other role.
    Every transition is call-count keyed — never wall-clock — so two
    runs emit identical evidence. Fault flags stage each named
    failure the issue calls out."""

    TOKENS = {'a': 0xD5C00A, 'b': 0xD5C00B}
    FIELD = 200   # the field out-point each field-owning scan writes
    POINT = 10    # the writable bool in-point the command legs target

    def __init__(self, plant, journal_a, journal_b):
        self.plant = plant
        self.tick = {'a': 0, 'b': 0}
        self.role = {'a': 'active', 'b': 'standby'}
        self.sync = {'a': None, 'b': 'tracking'}
        self.track_left = {'a': 0, 'b': 0}
        self.seq = {'a': 1, 'b': 1}
        self.receipts = {'a': [], 'b': []}
        self.pending = {'a': [], 'b': []}
        self.failed_writes = {'a': 0, 'b': 0}
        self.dead = {'a': False, 'b': False}
        self.paths = {'a': Path(journal_a), 'b': Path(journal_b)}
        self.streams = {'a': self._connect(), 'b': self._connect()}
        for path in self.paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            self._append(path, {'run_boundary': {'run': 1, 'tick': 0}})
        self._plant_call('a', {'op': 'claim_writer',
                               'owner': self.TOKENS['a']})
        # Fault injection — each named failure the issue calls out.
        self.promote_refuses = False  # the misordered promote refuses
        self.dies_on_fence = False    # the superseded owner exits
        self.never_demotes = False    # the fenced write never demotes
        self.skips_demoting = False   # the walk jumps to standby
        self.no_journal = False       # the claim loss goes unrecorded
        self.extra_boundary = False   # a second lifetime lands on a's file
        self.command_wrong = False    # the demoted peer settles otherwise
        self.command_leaks = False    # the refused write reaches the field
        self.peer_fenced = False      # the promoted peer's writes fence
        self.never_tracks = False     # the demoted writer never reconverges
        self.never_claims = False     # the promote takes no claim

    def close(self):
        for stream in self.streams.values():
            stream.close()

    def _connect(self):
        host, _, port = self.plant.address.rpartition(':')
        return socket.create_connection((host, int(port)), timeout=5)

    def _plant_call(self, name, request):
        stream = self.streams[name]
        stream.sendall(json.dumps(request).encode() + b'\n')
        line = b''
        while not line.endswith(b'\n'):
            chunk = stream.recv(65536)
            if not chunk:
                raise ConnectionError('the plant closed mid-answer')
            line += chunk
        return json.loads(line)

    def _append(self, path, record):
        with path.open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    def _entry(self, name, event):
        entry = {'seq': self.seq[name], 'tick': self.tick[name],
                 'event': event}
        self._append(self.paths[name], {'entry': entry})
        self.seq[name] += 1

    def _conflict(self, url, payload):
        return urllib.error.HTTPError(
            url, 409, 'conflict', {},
            io.BytesIO(json.dumps(payload).encode()))

    def _fenced(self, response):
        error = (response or {}).get('error') or {}
        inner = error.get('error')
        return error.get('kind') == 'fenced' or (
            error.get('kind') == 'io' and isinstance(inner, dict)
            and 'fenced' in inner)

    def _supersede(self, name):
        # The contract's degrade path on the first fenced write: the
        # loss counted and journaled, then the demoting walk — a
        # degrade, never a death.
        self.failed_writes[name] += 1
        if not self.no_journal:
            self._entry(name, {'field_claim_lost': {'point': self.FIELD}})
        if self.extra_boundary and name == 'a':
            self._append(self.paths['a'], {'run_boundary': {
                'run': 2, 'tick': self.tick['a']}})
        if self.dies_on_fence:
            self.dead[name] = True
            return
        if self.never_demotes:
            return
        if self.skips_demoting:
            self.role[name] = 'standby'
            self._entry(name, {'role_changed': {'from': 'active',
                                                'to': 'standby'}})
            self.track_left[name] = 2
            self.sync[name] = 'unsynchronized'
            return
        self.role[name] = 'demoting'
        self._entry(name, {'role_changed': {'from': 'active',
                                            'to': 'demoting'}})

    def _scan(self, name):
        self.tick[name] += 1
        role = self.role[name]
        if role == 'demoting':
            self.role[name] = 'standby'
            self._entry(name, {'role_changed': {'from': 'demoting',
                                                'to': 'standby'}})
            self.track_left[name] = 2
            self.sync[name] = 'unsynchronized'
            return
        if role == 'standby':
            # A demoted peer's pulls reconverge it behind its
            # successor — a fixed count of scans, never wall-clock.
            if self.track_left[name] > 0:
                self.track_left[name] -= 1
                if self.track_left[name] == 0 \
                        and not self.never_tracks:
                    self.sync[name] = 'tracking'
            return
        if role == 'promoting':
            self.role[name] = 'active'
            self._entry(name, {'role_changed': {'from': 'promoting',
                                                'to': 'active'}})
        # A field-owning scan writes the field through the claim.
        response = self._plant_call(
            name, {'op': 'write', 'point': self.FIELD,
                   'value': {'bool': False}})
        if self._fenced(response) \
                or (self.peer_fenced and name == 'b'):
            self._supersede(name)
            return
        for receipt in self.pending[name]:
            write = receipt['command']['write_value']
            self._plant_call(name, {'op': 'write',
                                    'point': write['point'],
                                    'value': write['value']})
            receipt['outcome'] = {'applied': {'tick': self.tick[name]}}
            self._entry(name, {'command_settled': {'receipt': receipt}})
        self.pending[name] = []

    def _role_report(self, name):
        report = {'role': self.role[name], 'tick': self.tick[name]}
        if self.role[name] == 'standby':
            report['sync'] = {'tracking': {'aligned': self.tick[name]}} \
                if self.sync[name] == 'tracking' \
                else {'unsynchronized': {}}
        return report

    def _snapshot(self, name):
        return {'tick': self.tick[name], 'points': [
            {'point': p, 'sample': dict(self.plant.samples[p])}
            for p in sorted(self.plant.samples)],
            'io_health': {
                'failed_reads': 0,
                'failed_writes': self.failed_writes[name],
                'consecutive_failures': 0,
                'last_error': None,
                'driver': {'link': 'connected'}}}

    def _promote(self, name, url):
        if self.role[name] == 'active':
            raise self._conflict(url, {'already_active': {}})
        if self.role[name] != 'standby' \
                or self.sync[name] != 'tracking' \
                or self.promote_refuses:
            raise self._conflict(url, {'not_converged': {
                'sync': self.sync[name] or 'unsynchronized'}})
        # The unconditional claim preempts the standing owner — the
        # misorder's fencing induction.
        if not self.never_claims:
            self._plant_call(name, {'op': 'claim_writer',
                                    'owner': self.TOKENS[name]})
        self.role[name] = 'promoting'
        self._entry(name, {'role_changed': {'from': 'standby',
                                            'to': 'promoting'}})
        return 200, {'role': 'promoting', 'tick': self.tick[name]}

    def _demote(self, name, url):
        if self.role[name] != 'active':
            raise self._conflict(url, {'not_active': {}})
        self.role[name] = 'demoting'
        self._entry(name, {'role_changed': {'from': 'active',
                                            'to': 'demoting'}})
        return 200, {'role': 'demoting', 'tick': self.tick[name]}

    def _command(self, name, body):
        receipt = {'command': body['command'],
                   'actor': body.get('actor')}
        if self.role[name] != 'active':
            # The admission-time refusal #522 settled: a receipted
            # rejection, never a write that fails on the field.
            if self.command_wrong:
                receipt['outcome'] = {'accepted': {
                    'apply_tick': self.tick[name] + 1}}
            else:
                receipt['outcome'] = {'rejected': {'reason': {
                    'not_active': {
                        'point': body['command']['write_value']
                                 ['point'],
                        'role': self.role[name]}}}}
                if self.command_leaks:
                    write = body['command']['write_value']
                    self.plant.samples[write['point']]['value'] \
                        = write['value']
            self._entry(name, {'command_settled': {'receipt': receipt}})
            self.receipts[name].append(receipt)
            return 200, receipt
        receipt['outcome'] = {'accepted': {
            'apply_tick': self.tick[name] + 1}}
        self.pending[name].append(receipt)
        self.receipts[name].append(receipt)
        return 200, receipt

    def _serve(self, name, method, route, url, body):
        if self.dead[name]:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            self._scan(name)
            return 200, self._role_report(name)
        if (method, route) == ('GET', '/snapshot'):
            self._scan(name)
            return 200, self._snapshot(name)
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': self.POINT, 'signal': None,
                 'name': 'p101-oos', 'direction': 'in',
                 'value_type': 'bool', 'writable': True}]}
        if (method, route) == ('GET', '/receipts'):
            self._scan(name)
            return 200, list(self.receipts[name])
        if (method, route) == ('POST', '/command'):
            return self._command(name, body)
        if (method, route) == ('POST', '/promote'):
            return self._promote(name, url)
        if (method, route) == ('POST', '/demote'):
            return self._demote(name, url)
        raise AssertionError('unexpected request %s %s' % (method, url))

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        name = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}[host]
        return self._serve(name, method, route, url, body)


class FencedWriterDegradeTests(unittest.TestCase):
    """The fenced-writer-degrade scenario under fakes: the plant
    enforces the single-writer claim and the stubbed pair walks the
    misordered promote, the demote-in-place degrade, the not_active
    refusal, the undisturbed promoted writer, and the role restore —
    plus each named failure and inconclusive path the issue calls
    out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_a = Path(self.tmp.name) / 'controllers' / 'a' \
            / 'journal.jsonl'
        self.journal_b = Path(self.tmp.name) / 'controllers' / 'b' \
            / 'journal.jsonl'
        self.plant = DegradePlantPeer()
        self.feed = FencedDegradeFeed(self.plant, self.journal_a,
                                      self.journal_b)

    def tearDown(self):
        self.feed.close()
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': self.plant.address,
                'plant_ctl': self.plant.ctl,
                'plant_owner': {
                    'active': FencedDegradeFeed.TOKENS['a'],
                    'standby': FencedDegradeFeed.TOKENS['b']},
                'journal_files': {'active': str(self.journal_a),
                                  'standby': str(self.journal_b)},
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, feed=None, **ctx_overrides):
        feed = feed or self.feed
        ctx = self._ctx()
        ctx.update(ctx_overrides)
        defaults = {'POLL_INTERVAL': 0.001,
                    'FENCED_DEGRADE_WATCH': 0.001,
                    'FENCED_DEGRADE_POLL': 0.001,
                    'FENCED_DEGRADE_DEADLINE': 2.0,
                    'FENCED_DEGRADE_JOURNAL': 1.0,
                    'FENCED_DEGRADE_SETTLE': 1.0}
        with patch.object(scenarios, 'http_json', feed.http_json):
            for key, value in defaults.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            return scenarios.scenario_fenced_writer_degrade(ctx)

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_fenced_writer_degrade, order)
        # The restored pre-switch window — beside the field-claim
        # case whose claim machinery it exercises, ahead of the tune
        # case's a->b switch.
        self.assertLess(
            order.index(scenarios.scenario_field_claim),
            order.index(scenarios.scenario_fenced_writer_degrade))
        self.assertLess(
            order.index(scenarios.scenario_fenced_writer_degrade),
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(verify.case_function('fenced-writer-degrade'),
                      scenarios.scenario_fenced_writer_degrade)

    def test_passed(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertTrue(report.validate_scenario(record))
        for name in ('fenced-degrade-signals.json',
                     'fenced-degrade-promote.json',
                     'fenced-degrade-watch.json',
                     'fenced-degrade-journal.json',
                     'fenced-degrade-field.json',
                     'fenced-degrade-refusal.json',
                     'fenced-degrade-writes.json',
                     'fenced-degrade-restored.json'):
            path = os.path.join(str(self.evidence), name)
            self.assertTrue(os.path.exists(path), name)
            json.loads(Path(path).read_text())
        # The pair ends back on its entry roles with one process
        # lifetime per journal.
        self.assertEqual(self.feed.role, {'a': 'active',
                                          'b': 'standby'})
        for path in (self.journal_a, self.journal_b):
            bounds = [item['run_boundary'] for item in
                      scenarios._journal_entries(path)
                      if 'run_boundary' in item]
            self.assertEqual(len(bounds), 1)

    def test_fails_when_the_superseded_peer_exits(self):
        # The pre-#510 shape: the fenced writer dies instead of
        # degrading.
        self.feed.dies_on_fence = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('exited instead of degrading',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_fails_when_the_fenced_owner_never_demotes(self):
        self.feed.never_demotes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never demoted', record.get('detail', ''))
        report.validate_scenario(record)

    def test_fails_when_the_walk_skips_demoting(self):
        self.feed.skips_demoting = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never walked demoting',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_fails_when_the_preemption_is_silent(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never carried the demotion contract',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_fails_on_a_second_process_lifetime(self):
        self.feed.extra_boundary = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('restarted instead of degrading',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_fails_when_the_misordered_promote_refuses(self):
        self.feed.promote_refuses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('the misordered promote',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_fails_when_the_promotion_claims_nothing(self):
        # A promotion that never takes the claim leaves the standing
        # owner writing — the supersession never happens.
        self.feed.never_claims = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never demoted', record.get('detail', ''))
        report.validate_scenario(record)

    def test_fails_when_the_promoted_peer_is_fenced(self):
        self.feed.peer_fenced = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        report.validate_scenario(record)

    def test_fails_when_the_command_is_not_not_active(self):
        self.feed.command_wrong = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not_active', record.get('detail', ''))
        report.validate_scenario(record)

    def test_fails_when_the_refused_write_reaches_the_field(self):
        self.feed.command_leaks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reached the field', record.get('detail', ''))
        report.validate_scenario(record)

    def test_fails_when_the_demoted_writer_never_reconverges(self):
        self.feed.never_tracks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never reconverged', record.get('detail', ''))
        report.validate_scenario(record)

    def test_inconclusive_without_a_plant(self):
        record = self.run_scenario(plant=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_inconclusive_without_journal_paths(self):
        record = self.run_scenario(journal_files={})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_inconclusive_without_owner_tokens(self):
        record = self.run_scenario(plant_owner={})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_inconclusive_when_the_peer_never_tracks(self):
        self.feed.sync['b'] = 'unsynchronized'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no target', record.get('detail', ''))
        report.validate_scenario(record)

    def test_inconclusive_when_the_pair_is_down(self):
        def down(method, url, body=None, timeout=10):
            raise urllib.error.URLError('connection refused')
        ctx = self._ctx()
        with patch.object(scenarios, 'http_json', down), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'FENCED_DEGRADE_DEADLINE',
                             0.5):
            record = scenarios.scenario_fenced_writer_degrade(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_fails_when_nothing_is_settled(self):
        self.feed.role['a'] = 'standby'
        self.feed.sync['a'] = 'tracking'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role=active', record.get('detail', ''))
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for index in range(2):
            evidence = Path(self.tmp.name) / ('run' + str(index))
            evidence.mkdir()
            self.evidence = evidence
            plant = DegradePlantPeer()
            journal_a = Path(self.tmp.name) / ('jr' + str(index)) \
                / 'a' / 'journal.jsonl'
            journal_b = Path(self.tmp.name) / ('jr' + str(index)) \
                / 'b' / 'journal.jsonl'
            feed = FencedDegradeFeed(plant, journal_a, journal_b)
            self.journal_a, self.journal_b = journal_a, journal_b
            self.plant = plant
            try:
                record = self.run_scenario(feed=feed)
            finally:
                feed.close()
                plant.close()
            runs.append((
                record, {p.name: p.read_text()
                         for p in sorted(evidence.iterdir())}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
