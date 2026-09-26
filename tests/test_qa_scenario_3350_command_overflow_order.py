"""The 3350_command_overflow_order leg's scenario unit coverage — the feed
fakes and TestCase classes for scenario_command_overflow_order, split out
of the test_qa_scenarios monolith (#940). The shared fakes and helpers
live in tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'CommandOverflowOrderTests.test_registered_in_scenarios',
    'CommandOverflowOrderTests.test_clean_pair_passes_and_validates',
    'CommandOverflowOrderTests.test_inverted_deferral_reports_failed',
    'CommandOverflowOrderTests.test_inverted_journal_reports_failed',
    'CommandOverflowOrderTests.test_silent_past_bound_reports_failed',
    'CommandOverflowOrderTests.test_unbounded_deck_reports_failed',
    'CommandOverflowOrderTests.test_unsettled_journal_reports_failed',
    'CommandOverflowOrderTests.test_moved_roles_report_nondeterministic',
    'CommandOverflowOrderTests.test_silent_audit_reports_unchecked',
    'CommandOverflowOrderTests.test_no_active_reports_failed',
    'CommandOverflowOrderTests.test_two_runs_produce_identical_evidence',
})


class OrderSocket(FakeSocket):
    """The leg's raw connection: the pinning head arrives with its
    declared body half-sent and answers only once the rest lands; the
    labeled wave's pipelined requests answer on the first read, in
    request order."""

    def __init__(self, feed):
        super().__init__()
        self.feed = feed

    def recv(self, count):
        if not self.response:
            self.response = self.feed.answers_for(self.sent)
        return super().recv(count)


class OverflowOrderFeed:
    """A stubbed pair for the command-overflow-order leg. ctrl-a is the
    field owner: each measurement read is one paced scan, settling the
    pending receipts it adopted; its command lane and overflow deck
    mirror the monitor's declared bounds — the wave's first `lane`
    submissions queue, the next `deck` defer and mint through the
    hand-back, and the past-bound tail answers the named 500 — while
    ctrl-b is the tracking standby the role audit reads."""
    POINT = 204

    def __init__(self):
        self.tick = 0
        self.capacity = 4       # the served command_queue bound — small
        self.receipts = []      # the receipt log, mint order
        self.attempts = 0
        self.full_rejections = 0
        self.high_water = 0
        self.pending = 0        # the executor's pending-command depth
        self.journal = []
        self.seq = 0
        self.sockets = []
        # The doctors staging each named defect.
        self.invert_deferred = False  # deferreds mint ahead of the queue
        self.invert_journal = False   # rejected settles journal reversed
        self.silent_refusal = False   # past-bound submissions never answer
        self.unbounded = False        # the deck never bounds — all mint
        self.never_settle = False     # admissions never settle
        self.role_moved = False       # the standby leaves tracking
        self.moved = False            # the doctor's landed state
        self.no_active = False        # no peer reports role=active

    # --- the runner-owned transport seam ---

    def connect(self, base, timeout=5):
        stream = OrderSocket(self)
        self.sockets.append(stream)
        return stream

    # --- the lane emulation ---

    def _is_stalled(self, bodies):
        return len(bodies) == 1

    def answers_for(self, raw):
        """The pipelined stream's answers: parse the complete requests
        (a still-stalled head truncates the parse), mint the lane/deck
        share — in dispatch order unless the inversion doctor runs —
        refuse the past-bound tail, and answer in request order."""
        bodies = []
        rest = raw
        while rest:
            _line, sep, tail = rest.partition(b'POST /command')
            if not sep:
                break
            header, sep, body = tail.partition(b'\r\n\r\n')
            if not sep:
                break
            length = 0
            for line in header.split(b'\r\n'):
                if line.lower().startswith(b'content-length:'):
                    length = int(line.split(b':', 1)[1])
            if len(body) < length:
                break
            bodies.append(json.loads(body[:length]))
            rest = body[length:]
        if not bodies:
            return b''
        if self._is_stalled(bodies):
            receipt = self._mint(bodies[0])
            payload = json.dumps(receipt).encode()
            return (b'HTTP/1.1 200 X\r\nContent-Type: application/json'
                    b'\r\nContent-Length: ' + str(len(payload)).encode()
                    + b'\r\n\r\n' + payload)
        lane = scenarios.LANE_QUEUE + 2 * self.capacity
        deck = scenarios.LANE_QUEUE
        mint = []
        for index, body in enumerate(bodies):
            if index < lane + deck or self.unbounded:
                mint.append((index, body))
        order = list(mint)
        if self.invert_deferred:
            order = [entry for entry in order if entry[0] >= lane] \
                + [entry for entry in order if entry[0] < lane]
        receipts = {}
        staged = []
        for index, body in order:
            receipts[index] = self._mint(body, stage=staged)
        if self.invert_journal:
            staged.reverse()
        for entry in staged:
            self._journal_push(entry)
        silent = self.silent_refusal
        replies = []
        for index, body in enumerate(bodies):
            if index in receipts and not silent:
                payload = json.dumps(receipts[index]).encode()
                replies.append(
                    b'HTTP/1.1 200 X\r\nContent-Type: application/json'
                    b'\r\nContent-Length: '
                    + str(len(payload)).encode() + b'\r\n\r\n'
                    + payload)
            elif not silent:
                replies.append(b'HTTP/1.1 500 X\r\nContent-Length: 0'
                               b'\r\n\r\n')
        self.moved = self.role_moved
        return b''.join(replies)

    def _mint(self, body, stage=None):
        """One submission's admission: the bounded pending queue admits
        while it has room, else the named queue_full rejection —
        journaled at the mint, like the runtime's note_command. The
        receipt appends to the log in mint order."""
        self.attempts += 1
        write = body['command']['write_value']
        if write['point'] == self.POINT and self.pending < self.capacity:
            receipt = {'command': body['command'],
                       'outcome': {'accepted': {
                           'apply_tick': self.tick + 1}},
                       'actor': body.get('actor')}
            self.pending += 1
            self.high_water = max(self.high_water, self.pending)
        else:
            self.full_rejections += 1
            receipt = {'command': body['command'],
                       'outcome': {'rejected': {'reason': {
                           'queue_full': {'point': write['point'],
                                          'capacity': self.capacity}}}},
                       'actor': body.get('actor')}
            entry = {'tick': self.tick,
                     'event': {'command_settled': {
                         'receipt': copy.deepcopy(receipt)}}}
            if stage is not None:
                stage.append(entry)
            else:
                self._journal_push(entry)
        self.receipts.append(receipt)
        return receipt

    def _journal_push(self, entry):
        self.seq += 1
        entry['seq'] = self.seq
        self.journal.append(entry)

    def _advance(self):
        """One paced scan: the tick climbs and the pending admissions
        settle applied in receipt-log order, each journaling its
        command_settled."""
        self.tick += 1
        if self.never_settle:
            return
        for receipt in self.receipts:
            accepted = receipt['outcome'].get('accepted')
            if accepted and self.tick >= accepted['apply_tick']:
                receipt['outcome'] = {'applied': {'tick': self.tick}}
                self.pending -= 1
                self._journal_push(
                    {'tick': self.tick,
                     'event': {'command_settled': {
                         'receipt': copy.deepcopy(receipt)}}})

    # --- the stubbed monitor surface ---

    def http_json(self, method, url, body=None, timeout=5):
        host, _, path = url[7:].partition('/')
        path = '/' + path
        if host.startswith('ctrl-a'):
            self._advance()
            return self.active(method, path, body)
        if host.startswith('ctrl-b'):
            return self.standby(method, path)
        raise urllib.error.URLError('unknown host ' + host)

    def active(self, method, path, body):
        if path == '/role':
            role = 'standby' if self.no_active else 'active'
            return 200, {'role': role, 'tick': self.tick}
        if path == '/signals':
            return 200, {'points': [
                {'name': 'p101-oos', 'point': self.POINT,
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True},
                {'name': 'level-primary', 'point': 20,
                 'direction': 'in', 'value_type': 'float',
                 'writable': False}], 'components': []}
        if path == '/snapshot':
            return 200, {'tick': self.tick,
                         'points': [{'point': self.POINT,
                                     'sample': {'value': {
                                         'bool': False}}}],
                         'command_queue': {
                             'attempts': self.attempts,
                             'full_rejections': self.full_rejections,
                             'capacity': self.capacity,
                             'depth': self.pending,
                             'high_water': self.high_water}}
        if path == '/checkpoint':
            return 200, {'receipts': list(self.receipts),
                         'command_admission': {
                             'attempts': self.attempts,
                             'full_rejections': self.full_rejections,
                             'high_water': self.high_water}}
        if path == '/receipts':
            return 200, list(self.receipts)
        if path.startswith('/journal'):
            since = int(path.split('since=', 1)[1]) \
                if 'since=' in path else 0
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
        raise urllib.error.URLError('no route ' + path)

    def standby(self, method, path):
        if path == '/role':
            if getattr(self, 'moved', False):
                return 200, {'role': 'promoting', 'tick': self.tick}
            return 200, {'role': 'standby', 'tick': self.tick,
                         'sync': {'tracking': {'aligned': self.tick}}}
        if path.startswith('/journal'):
            return 200, []
        raise urllib.error.URLError('no route ' + path)


class CommandOverflowOrderTests(unittest.TestCase):
    """scenario_command_overflow_order against the stubbed pair: a
    clean rig passes with identical digests, each doctored defect
    reports the named diagnostic, and a silenced audit reports the
    self-check leg's unchecked name."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = OverflowOrderFeed()

    def _ctx(self, feed=None, **extra):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        ctx.update(extra)
        return ctx

    def run_scenario(self, feed=None, ctx=None, **patches):
        feed = feed or self.feed
        constants = {'POLL_INTERVAL': 0.001, 'LEG_POLL': 0.001,
                     'LANE_QUEUE': 8, 'OVERFLOW_MARGIN': 4,
                     'PIN_LEAD': 0.001, 'PIN_HOLD': 0.001,
                     'PAIR_SETTLE': 2.0,
                     'REPLY_DEADLINE': 2.0, 'SETTLE_DEADLINE': 2.0,
                     'SETTLE_POLL': 0.001}
        constants.update(patches)
        with patch.multiple(scenarios, **constants), \
                patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, '_connect', feed.connect):
            return scenarios.scenario_command_overflow_order(
                ctx or self._ctx(feed))

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_command_overflow_order, order)
        # The ordering leg sits directly behind the admission leg it
        # extends.
        self.assertEqual(
            order.index(scenarios.scenario_command_overflow_order),
            order.index(scenarios.scenario_command_admission) + 1)
        self.assertIs(
            verify.case_function('command-overflow-order'),
            scenarios.scenario_command_overflow_order)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        self.assertEqual(
            record['observations'][-2:],
            ['two overflow passes, identical digests',
             'the self-check leg\'s planted negatives each reported '
             'their named diagnostic'])
        for name in ('command-overflow-order-signals.json',
                     'command-overflow-order-pass-1.json',
                     'command-overflow-order-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passed = json.loads(
            (self.evidence
             / 'command-overflow-order-pass-1.json').read_text())
        self.assertEqual(passed['digest'], {
            'wave': 'answered', 'minted': 'ordered', 'deck': 'engaged',
            'past_bound': 'named', 'journal': 'ordered',
            'roles': 'unchanged'})
        self.assertEqual(passed['violations'], {})
        # capacity 4, lane 16, deck 8, margin 4: submissions 0..24
        # minted, 25..28 refused — 25 receipts, 21 of them the named
        # rejection.
        self.assertEqual(passed['minted'], 25)
        second = json.loads(
            (self.evidence
             / 'command-overflow-order-pass-2.json').read_text())
        self.assertEqual(second['digest'], passed['digest'])

    def test_inverted_deferral_reports_failed(self):
        self.feed.invert_deferred = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'command-overflow-order-failed'), record['detail'])
        self.assertIn('ahead of an earlier', record['detail'])
        report.validate_scenario(record)

    def test_inverted_journal_reports_failed(self):
        self.feed.invert_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'command-overflow-order-failed'), record['detail'])
        self.assertIn('out of mint order', record['detail'])
        report.validate_scenario(record)

    def test_silent_past_bound_reports_failed(self):
        self.feed.silent_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'command-overflow-order-failed'), record['detail'])
        self.assertIn('never answered', record['detail'])
        report.validate_scenario(record)

    def test_unbounded_deck_reports_failed(self):
        self.feed.unbounded = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'command-overflow-order-failed'), record['detail'])
        self.assertIn('refusal', record['detail'])
        report.validate_scenario(record)

    def test_unsettled_journal_reports_failed(self):
        self.feed.never_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'command-overflow-order-failed'), record['detail'])
        self.assertIn('never landed', record['detail'])
        report.validate_scenario(record)

    def test_moved_roles_report_nondeterministic(self):
        self.feed.role_moved = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'command-overflow-order-nondeterministic'),
            record['detail'])
        self.assertIn('launch roles', record['detail'])
        report.validate_scenario(record)

    def test_silent_audit_reports_unchecked(self):
        # The self-check leg: the audits, silenced, must turn the
        # planted negatives into the unchecked diagnostic.
        with patch.object(scenarios, '_mint_order_violation',
                          lambda pairs: None), \
                patch.object(scenarios, '_journal_order_violation',
                             lambda settles: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'command-overflow-order-unchecked'), record['detail'])
        report.validate_scenario(record)

    def test_no_active_reports_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            feed = OverflowOrderFeed()
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.evidence = evidence
            record = self.run_scenario(feed=feed,
                                       ctx=self._ctx(feed))
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
