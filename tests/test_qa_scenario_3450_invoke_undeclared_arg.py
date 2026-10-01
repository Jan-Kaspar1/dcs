"""The 3450_invoke_undeclared_arg leg's scenario unit coverage — the
feed fake and TestCase classes for scenario_invoke_undeclared_arg,
following the module-per-leg convention (#928, #940). The shared
fakes and helpers live in tests/qa_scenario_support.py;
EXPECTED_CASES pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'InvokeUndeclaredArgTests.test_registered_in_scenarios',
    'InvokeUndeclaredArgTests.test_clean_feed_passes_and_validates',
    'InvokeUndeclaredArgTests.test_predating_rig_is_inconclusive',
    'InvokeUndeclaredArgTests.test_queue_full_before_validation_is_inconclusive',
    'InvokeUndeclaredArgTests.test_no_kind_declared_command_is_inconclusive',
    'InvokeUndeclaredArgTests.test_unreachable_rig_is_inconclusive',
    'InvokeUndeclaredArgTests.test_wrong_refusal_reason_fails',
    'InvokeUndeclaredArgTests.test_refusal_queued_fails',
    'InvokeUndeclaredArgTests.test_served_receipt_disagrees_is_nondeterministic',
    'InvokeUndeclaredArgTests.test_journal_silent_fails',
    'InvokeUndeclaredArgTests.test_journaled_settle_disagrees_is_nondeterministic',
    'InvokeUndeclaredArgTests.test_double_journaled_settle_is_nondeterministic',
    'InvokeUndeclaredArgTests.test_declared_subset_refused_fails',
    'InvokeUndeclaredArgTests.test_omitted_invoke_never_settles_fails',
    'InvokeUndeclaredArgTests.test_dispatch_refusal_on_available_fails',
    'InvokeUndeclaredArgTests.test_second_pass_admission_is_nondeterministic',
    'InvokeUndeclaredArgTests.test_two_runs_produce_identical_evidence',
})


class InvokeArgFeed:
    """A stubbed monitor pair for the invoke-undeclared-arg scenario.
    ctrl-a serves the settled active; ctrl-b reports a tracking
    standby. The served model: a sequencer instance `seq:9` whose
    kind declares `advance` — request schema `count` int,
    kind-declared availability, served invocable — beside `reset`
    (empty request), plus a `motor:22` carrying port-adapted commands
    only. POST /command runs the admission contract: an argument name
    outside the declared request refuses at admission — the terminal
    `unknown_argument` rejection receipted on the wire answer,
    mirrored in the served log, journaled `command_settled` at once,
    and never queued — while a declared-subset invoke queues and
    applies at the next read's scan boundary, the kind's
    absent-argument default pacing the table one step (`dispatched`
    records the argument map dispatch actually saw, `step` the
    advances). Fault flags stage each named failure and the
    inconclusive paths."""

    def __init__(self):
        self.tick = 0
        self.receipts = []
        self.attempts = 0
        self.journal = []
        self.next_seq = 1
        self.extra_pending = 0  # pending depth a refused submission took
        self.step = 0           # the sequencer's served step position
        self.dispatched = []    # the argument maps dispatch received
        self.extra_seen = 0     # undeclared-name submissions so far
        # Fault flags staging the named failures and inconclusives.
        self.down = False               # every request refuses
        self.no_declared = False        # the registry drops 'declared' specs
        self.predates = False           # undeclared names admit — no bound
        self.queue_full = False         # undeclared names meet the bound
        self.wrong_reason = False       # refused with another CommandError
        self.served_disagrees = False   # the log's copy names another reason
        self.journal_silent = False     # the refusal never journals
        self.journal_lies = False       # the journal carries it applied
        self.double_journal = False     # the refusal journals twice
        self.queued = False             # the refused submission sits pending
        self.refuses_omitted = False    # a declared subset refused by name
        self.never_settles = False      # the accepted invoke never settles
        self.dispatch_refuses = False   # invocable row, dispatch refuses
        self.second_pass_admits = False  # pass 2 admits the undeclared name

    def _commands(self):
        if self.no_declared:
            return []
        return [{'name': 'advance',
                 'request': [{'name': 'count', 'kind': 'int'}],
                 'availability': 'kind_declared',
                 'adapted': 'declared'},
                {'name': 'reset', 'request': [],
                 'availability': 'always', 'adapted': 'declared'}]

    def _interfaces(self):
        return [{'name': 'seq:9', 'interface': {
                    'version': 1, 'kind': 'sequencer',
                    'measurements': [], 'configuration': [],
                    'state': [{'name': 'step', 'kind': 'int'}],
                    'commands': self._commands(), 'events': []}},
                {'name': 'motor:22', 'interface': {
                    'version': 1, 'kind': 'motor',
                    'measurements': [], 'configuration': [],
                    'state': [],
                    'commands': [{'name': 'write_value:oos',
                                  'request': [{'name': 'value',
                                               'kind': 'bool'}],
                                  'availability':
                                      'bound_point_writable',
                                  'adapted': 'write_value',
                                  'point': 302}],
                    'events': []}}]

    def _resources(self):
        rows = [] if self.no_declared else [
            {'name': 'advance', 'available': True},
            {'name': 'reset', 'available': True}]
        return {'publication': self.tick, 'tick': self.tick,
                'components': [
                    {'name': 'seq:9', 'kind': 'sequencer',
                     'measurements': [], 'configuration': [],
                     'state': [{'name': 'step',
                                'value': {'int': self.step}}],
                     'commands': rows, 'events': []},
                    {'name': 'motor:22', 'kind': 'motor',
                     'measurements': [], 'configuration': [],
                     'state': [],
                     'commands': [{'name': 'write_value:oos',
                                   'point': 302, 'available': True}],
                     'events': []}]}

    def _spec_for(self, invoke):
        for entry in self._interfaces():
            if entry['name'] != invoke.get('component'):
                continue
            for spec in entry['interface']['commands']:
                if spec['name'] == invoke.get('command') \
                        and spec.get('adapted') == 'declared':
                    return spec
        return None

    def _journal(self, receipt):
        self.journal.append({'seq': self.next_seq, 'tick': self.tick,
                             'event': {'command_settled': {
                                 'receipt': json.loads(
                                     json.dumps(receipt))}}})
        self.next_seq += 1

    def _extra(self, invoke):
        """The argument names the submission carries that the declared
        request schema does not — the names the schema bound owns."""
        spec = self._spec_for(invoke)
        names = {arg['name'] for arg in spec['request']} \
            if spec else set()
        return sorted(name for name in (invoke.get('arguments') or {})
                      if name not in names)

    def _advance(self):
        # One completed scan per measurement read: the boundary settles
        # every queued receipt whose apply_tick has arrived — dispatch
        # records the argument map it saw and applies the kind's
        # absent-`count` default of one step.
        self.tick += 1
        for receipt in self.receipts:
            accepted = receipt['outcome'].get('accepted')
            if not accepted or self.tick < accepted['apply_tick']:
                continue
            if self.never_settles:
                continue
            invoke = receipt['command']['invoke']
            self.dispatched.append(dict(invoke.get('arguments') or {}))
            count = (invoke.get('arguments') or {}) \
                .get('count', {}).get('int', 1)
            if self.dispatch_refuses:
                receipt['outcome'] = {'rejected': {'reason': {
                    'command_refused': {
                        'component': invoke['component'],
                        'command': invoke['command'],
                        'reason': 'the step table is complete'}}}}
            else:
                self.step += count
                receipt['outcome'] = {'applied': {'tick': self.tick}}
            self._journal(receipt)

    def _admit(self, body):
        # The admission contract: validation precedes the bounded
        # queue — an undeclared name takes its named reason even at a
        # full queue, so a queue_full answer here is the pre-contract
        # shape too.
        self.attempts += 1
        command = body['command']
        invoke = (command or {}).get('invoke') or {}
        extra = self._extra(invoke)
        if extra:
            self.extra_seen += 1
            if self.predates \
                    or (self.second_pass_admits and self.extra_seen > 1):
                receipt = {'command': command,
                           'outcome': {'accepted': {
                               'apply_tick': self.tick + 1}},
                           'actor': body.get('actor')}
                self.receipts.append(receipt)
                return receipt
            if self.queue_full:
                reason = {'queue_full': {'point': None, 'capacity': 4}}
            elif self.wrong_reason:
                reason = {'unknown_command': {
                    'component': invoke.get('component'),
                    'command': invoke.get('command')}}
            else:
                reason = {'unknown_argument': {
                    'component': invoke.get('component'),
                    'command': invoke.get('command'),
                    'argument': extra[0]}}
            receipt = {'command': command,
                       'outcome': {'rejected': {'reason': reason}},
                       'actor': body.get('actor')}
            logged = json.loads(json.dumps(receipt))
            if self.served_disagrees:
                logged['outcome'] = {'rejected': {'reason': {
                    'argument_type_mismatch': {
                        'component': invoke.get('component'),
                        'command': invoke.get('command'),
                        'argument': extra[0],
                        'expected': 'int', 'found': 'bool'}}}}
            self.receipts.append(logged)
            if self.queued:
                self.extra_pending += 1
            if not self.journal_silent:
                echoed = json.loads(json.dumps(logged))
                if self.journal_lies:
                    echoed['outcome'] = {'applied': {'tick': self.tick}}
                self._journal(echoed)
                if self.double_journal:
                    self._journal(echoed)
            return receipt
        if self.refuses_omitted:
            receipt = {'command': command,
                       'outcome': {'rejected': {'reason': {
                           'unknown_argument': {
                               'component': invoke.get('component'),
                               'command': invoke.get('command'),
                               'argument': 'count'}}}},
                       'actor': body.get('actor')}
            self.receipts.append(receipt)
            self._journal(receipt)
            return receipt
        receipt = {'command': command,
                   'outcome': {'accepted': {'apply_tick': self.tick + 1}},
                   'actor': body.get('actor')}
        self.receipts.append(receipt)
        return receipt

    def http_json(self, method, url, body=None, timeout=10):
        if self.down:
            raise urllib.error.URLError('connection refused')
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self._advance()
        if host == 'ctrl-b:2':
            if (method, route) == ('GET', '/role'):
                return 200, {'role': 'standby', 'tick': self.tick,
                             'sync': {'tracking': {
                                 'aligned': self.tick}}}
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/schema'):
            return 200, {'publication': self.tick, 'tick': self.tick,
                         'interfaces': self._interfaces()}
        if (method, route) == ('GET', '/resources'):
            return 200, self._resources()
        if (method, route) == ('GET', '/snapshot'):
            pending = sum(1 for entry in self.receipts
                          if 'accepted' in entry['outcome'])
            return 200, {'tick': self.tick, 'points': [],
                         'command_queue': {
                             'attempts': self.attempts,
                             'capacity': 4,
                             'depth': pending + self.extra_pending}}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts)
        if (method, route) == ('GET', '/checkpoint'):
            return 200, {'receipts': list(self.receipts),
                         'command_admission': {
                             'attempts': self.attempts}}
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1])
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/command'):
            return 200, self._admit(body)
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class InvokeUndeclaredArgTests(unittest.TestCase):
    """scenario_invoke_undeclared_arg against the stubbed pair: the
    clean feed proves the named unknown_argument admission refusal —
    terminal on the wire, mirrored in the served log, journaled once,
    nothing queued — and the omitted-argument invoke resolving to the
    kind's default and applying; every fault flag stages a named
    failure or an inconclusive path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = InvokeArgFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, evidence=None):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'evidence_dir': str(evidence or self.evidence)}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'INVOKE_ARG_DEADLINE', 0.5):
            return scenarios.scenario_invoke_undeclared_arg(
                ctx or self._ctx())

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_invoke_undeclared_arg,
                      scenarios.SCENARIOS)
        self.assertIs(
            verify.case_function('invoke-undeclared-arg'),
            scenarios.scenario_invoke_undeclared_arg)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = {entry['ref'] for entry in record['evidence']}
        self.assertEqual(refs, {
            'evidence/invoke-undeclared-arg-schema.json',
            'evidence/invoke-undeclared-arg-pass-1.json',
            'evidence/invoke-undeclared-arg-pass-2.json'})
        for entry in record['evidence']:
            self.assertTrue(
                (self.evidence.parent / entry['ref']).exists(), entry)

        # The probe is the sequencer's `advance` — the kind-declared
        # command whose request schema declares `count`.
        first = json.loads(
            (self.evidence / 'invoke-undeclared-arg-pass-1.json')
            .read_text())
        self.assertEqual(first['component'], 'seq:9')
        self.assertEqual(first['command'], 'advance')
        self.assertEqual(first['undeclared_argument'], 'qa_undeclared')
        self.assertEqual(first['omitted_argument'], 'count')
        refused = first['undeclared']
        self.assertEqual(
            refused['receipt']['outcome'],
            {'rejected': {'reason': {'unknown_argument': {
                'component': 'seq:9', 'command': 'advance',
                'argument': 'qa_undeclared'}}}})
        self.assertEqual(refused['served']['outcome'],
                         refused['receipt']['outcome'])
        self.assertEqual(len(refused['journaled']), 1)
        self.assertEqual(refused['queue_depth'], [0, 0])
        omitted = first['omitted']
        self.assertEqual(omitted['command']['invoke']['arguments'], {})
        self.assertIn('applied', omitted['settled']['outcome'])
        self.assertEqual(len(omitted['journaled']), 1)

        # The kind's absent-argument default resolved at dispatch:
        # neither pass carried `count`, each applied one step.
        self.assertEqual(self.feed.dispatched, [{}, {}])
        self.assertEqual(self.feed.step, 2)

    def test_predating_rig_is_inconclusive(self):
        self.feed.predates = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates the invoke-admission contract',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_queue_full_before_validation_is_inconclusive(self):
        # Validation precedes admission on a contract build, so a
        # queue_full answer to an undeclared name means the schema
        # bound is absent.
        self.feed.queue_full = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_kind_declared_command_is_inconclusive(self):
        self.feed.no_declared = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no kind-declared command',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record.get('detail', ''))
        report.validate_scenario(record)

    def test_wrong_refusal_reason_fails(self):
        self.feed.wrong_reason = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('invoke-undeclared-arg-failed',
                      record.get('detail', ''))
        self.assertIn('rejected:unknown_command',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refusal_queued_fails(self):
        self.feed.queued = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('invoke-undeclared-arg-failed',
                      record.get('detail', ''))
        self.assertIn('entered the pending queue',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_served_receipt_disagrees_is_nondeterministic(self):
        self.feed.served_disagrees = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('invoke-undeclared-arg-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('served receipt disagrees',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_journal_silent_fails(self):
        self.feed.journal_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('invoke-undeclared-arg-failed',
                      record.get('detail', ''))
        self.assertIn('never recorded the admission refusal',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_journaled_settle_disagrees_is_nondeterministic(self):
        self.feed.journal_lies = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('invoke-undeclared-arg-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('journaled settle disagrees',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_double_journaled_settle_is_nondeterministic(self):
        self.feed.double_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('invoke-undeclared-arg-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('command_settled records',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_declared_subset_refused_fails(self):
        self.feed.refuses_omitted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('invoke-undeclared-arg-failed',
                      record.get('detail', ''))
        self.assertIn('bounds names, not presence',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_omitted_invoke_never_settles_fails(self):
        self.feed.never_settles = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('invoke-undeclared-arg-failed',
                      record.get('detail', ''))
        self.assertIn('never settled', record.get('detail', ''))
        report.validate_scenario(record)

    def test_dispatch_refusal_on_available_fails(self):
        self.feed.dispatch_refuses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('invoke-undeclared-arg-failed',
                      record.get('detail', ''))
        self.assertIn('served-available', record.get('detail', ''))
        report.validate_scenario(record)

    def test_second_pass_admission_is_nondeterministic(self):
        self.feed.second_pass_admits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('invoke-undeclared-arg-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('admitted on pass 2', record.get('detail', ''))
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        feed2 = InvokeArgFeed()
        record2 = self.run_scenario(ctx=self._ctx(evidence2),
                                    feed=feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)


if __name__ == '__main__':
    unittest.main()
