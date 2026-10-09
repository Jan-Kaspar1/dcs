"""The 3360_command_abort_verdict leg's scenario unit coverage — the feed
fakes and TestCase classes for scenario_command_abort_verdict. The
shared fakes and helpers live in tests/qa_scenario_support.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


class AbortVerdictFeed:
    """A stubbed pair for the command-abort-verdict leg. ctrl-a is the
    field owner: its single command worker reads buffered POST /command
    bodies only when the pinned lane drains — the released pin's drain
    mints every buffered body in arrival order, applying the last write
    to the served image, journaling each command_settled on the owner
    and the tracking peer's adopted line alike, and appending the same
    records to both peers' durable journal files — while ctrl-b serves
    the tracking-standby reads the audits and the launch-role restore
    poll."""
    POINT = 204

    def __init__(self):
        self.tick = 0
        self.image = False       # ctrl-a's served bool at POINT
        self.peer_image = False  # ctrl-b's adopted copy
        self.receipts = {'active': [], 'standby': []}
        seed = {'seq': 1, 'tick': 0, 'event': {'scan_started': {}}}
        self.journal = {'active': [dict(seed)],
                        'standby': [dict(seed)]}
        self.seq = {'active': 1, 'standby': 1}
        self.records = {'active': [], 'standby': []}  # durable entries
        self.paths = {}
        self.sockets = []
        # The doctors staging each named defect.
        self.never_settle = False  # the drained admission never mints
        self.double_settle = False  # the peer journals the settle twice
        self.contradict = False    # the peer journals a different verdict
        self.pending_log = False   # the peer's adopted log stays accepted
        self.missing_file = False  # the owner's durable file drops it
        self.lagged_image = False  # the peer's adopted image never took
        self.role_moved = False    # the standby leaves tracking on drain
        self.moved = False         # the doctor's landed state
        self.no_active = False     # no peer reports role=active

    # --- the runner-owned transport seam ---

    def connect(self, base, timeout=5):
        stream = FakeSocket()
        self.sockets.append(stream)
        return stream

    def _bodies(self, raw):
        """The complete POST /command bodies `raw` carries — a
        still-stalled head truncates the parse."""
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
        return bodies

    def drain(self, stream, deadline):
        """The released pin's drain answer — the leg's patched
        _drain_reply: the command worker reads every body buffered
        behind the stalled head in arrival order, mints each receipt
        applied at this scan boundary, journals the settle on both
        peers and into both durable files, and lands the last write on
        the served images — then answers the pin itself."""
        bodies = []
        for sock in self.sockets:
            if getattr(sock, 'drained', False):
                continue
            sock.drained = True
            bodies += self._bodies(sock.sent)
        if self.never_settle:
            return 200, {'command': {}, 'actor': 'pin',
                         'outcome': {'applied': {'tick': self.tick}}}
        self.tick += 1
        self.moved = self.role_moved
        minted = []
        for body in bodies:
            write = (body.get('command') or {}).get('write_value') or {}
            want = (write.get('value') or {}).get('bool')
            receipt = {'command': body.get('command'),
                       'actor': body.get('actor'),
                       'outcome': {'applied': {'tick': self.tick}}}
            if 'reason' in body:
                receipt['reason'] = body['reason']
            minted.append(receipt)
            self.receipts['active'].append(copy.deepcopy(receipt))
            adopted = copy.deepcopy(receipt)
            if self.pending_log:
                adopted['outcome'] = {'accepted': {
                    'apply_tick': self.tick}}
            self.receipts['standby'].append(adopted)
            self._settle('active', receipt)
            adopted_settle = copy.deepcopy(receipt)
            if self.contradict:
                adopted_settle['outcome'] = {'rejected': {'reason': {
                    'queue_full': {'point': write.get('point'),
                                   'capacity': 4}}}}
            self._settle('standby', adopted_settle)
            if self.double_settle:
                self._settle('standby', adopted_settle)
            if want is not None:
                self.image = want
                self.peer_image = (not want) if self.lagged_image \
                    else want
        pin = minted[0] if minted else {
            'command': {}, 'actor': 'pin',
            'outcome': {'applied': {'tick': self.tick}}}
        return 200, pin

    def _settle(self, name, receipt):
        self.seq[name] += 1
        entry = {'seq': self.seq[name], 'tick': self.tick,
                 'event': {'command_settled': {
                     'receipt': copy.deepcopy(receipt)}}}
        self.journal[name].append(entry)
        if not (self.missing_file and name == 'active'):
            self.records[name].append({'entry': copy.deepcopy(entry)})
            self._flush(name)

    def _flush(self, name):
        if name not in self.paths:
            return
        lines = ['{"run_boundary": {"attempt": 1}}'] \
            + [json.dumps(record) for record in self.records[name]]
        Path(self.paths[name]).write_text('\n'.join(lines) + '\n')

    # --- the stubbed monitor surface ---

    def http_json(self, method, url, body=None, timeout=5):
        host, _, path = url[7:].partition('/')
        path = '/' + path
        if host.startswith('ctrl-a'):
            return self.serve('active', method, path, body)
        if host.startswith('ctrl-b'):
            return self.serve('standby', method, path, body)
        raise urllib.error.URLError('unknown host ' + host)

    def serve(self, name, method, path, body):
        if path == '/role':
            if name == 'active':
                role = 'standby' if self.no_active else 'active'
                return 200, {'role': role, 'tick': self.tick}
            if self.moved:
                return 200, {'role': 'promoting', 'tick': self.tick}
            return 200, {'role': 'standby', 'tick': self.tick,
                         'sync': {'tracking': {'aligned': self.tick}}}
        if path in ('/demote', '/promote'):
            self.moved = False
            return 200, {'role': 'standby' if path == '/demote'
                         else 'active'}
        if path == '/signals':
            return 200, {'points': [
                {'name': 'p101-oos', 'point': self.POINT,
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True},
                {'name': 'level-primary', 'point': 20,
                 'direction': 'in', 'value_type': 'float',
                 'writable': False}], 'components': []}
        if path == '/snapshot':
            value = self.image if name == 'active' else self.peer_image
            return 200, {'tick': self.tick,
                         'points': [{'point': self.POINT,
                                     'sample': {'value': {
                                         'bool': value}}}]}
        if path == '/receipts':
            return 200, list(self.receipts[name])
        if path.startswith('/journal'):
            since = int(path.split('since=', 1)[1]) \
                if 'since=' in path else 0
            return 200, [entry for entry in self.journal[name]
                         if entry['seq'] > since]
        raise urllib.error.URLError('no route ' + path)


class CommandAbortVerdictTests(unittest.TestCase):
    """Audit command settlement after an abandoned transport wait."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.runs = 0
        self.feed = AbortVerdictFeed()

    def _ctx(self, feed):
        root = Path(self.tmp.name) / ('run' + str(self.runs))
        self.runs += 1
        root.mkdir(exist_ok=True)
        paths = {'active': str(root / 'journal-active.jsonl'),
                 'standby': str(root / 'journal-standby.jsonl')}
        feed.paths = dict(paths)
        feed._flush('active')
        feed._flush('standby')
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'journal_files': paths,
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, feed=None, ctx=None, post=None):
        feed = feed or self.feed
        constants = {'ABORT_LEAD': 0.001, 'ABORT_HOLD': 0.001,
                     'ABORT_BOUND': 0.001, 'ABORT_SETTLE': 1.0,
                     'ABORT_REPLY': 1.0, 'ABORT_POLL': 0.001}
        patches = [patch.multiple(scenarios, **constants),
                   patch.object(scenarios, 'http_json', feed.http_json),
                   patch.object(scenarios, '_connect', feed.connect),
                   patch.object(scenarios, '_drain_reply', feed.drain)]
        if post is not None:
            patches.append(patch.object(
                scenarios, '_post_command', return_value=post))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return scenarios.scenario_command_abort_verdict(
            ctx or self._ctx(feed))

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_command_abort_verdict, order)
        # The abort-verdict leg sits directly behind the ordering leg
        # whose lane pin it reuses.
        self.assertEqual(
            order.index(scenarios.scenario_command_abort_verdict),
            order.index(scenarios.scenario_command_overflow_order) + 1)
        self.assertIs(
            verify.case_function('command-abort-verdict'),
            scenarios.scenario_command_abort_verdict)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        self.assertTrue(record['observations'][-1].startswith(
            'two aborted-post passes, identical digests'))
        for name in ('command-abort-verdict-signals.json',
                     'command-abort-verdict-pass-1.json',
                     'command-abort-verdict-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passed = json.loads(
            (self.evidence
             / 'command-abort-verdict-pass-1.json').read_text())
        self.assertEqual(passed['digest'], {
            'receipt': 'applied',
            'journal': 'single', 'roles': 'held'})
        self.assertEqual(passed['violations'], {})
        second = json.loads(
            (self.evidence
             / 'command-abort-verdict-pass-2.json').read_text())
        self.assertEqual(second['digest'], passed['digest'])

    def test_never_settled_reports_failed(self):
        self.feed.never_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'command-abort-verdict-failed'), record['detail'])
        self.assertIn('terminal journaled verdict', record['detail'])
        report.validate_scenario(record)

    def test_double_settle_reports_nondeterministic(self):
        self.feed.double_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'command-abort-verdict-nondeterministic'),
            record['detail'])
        self.assertIn('exactly one stands per peer', record['detail'])
        report.validate_scenario(record)

    def test_contradicting_peer_reports_nondeterministic(self):
        self.feed.contradict = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'command-abort-verdict-nondeterministic'),
            record['detail'])
        self.assertIn('never more than one terminal outcome',
                      record['detail'])
        report.validate_scenario(record)

    def test_pending_log_reports_nondeterministic(self):
        self.feed.pending_log = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'command-abort-verdict-nondeterministic'),
            record['detail'])
        self.assertIn('receipt log', record['detail'])
        report.validate_scenario(record)

    def test_missing_file_settle_reports_failed(self):
        self.feed.missing_file = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'command-abort-verdict-failed'), record['detail'])
        self.assertIn('durable journal', record['detail'])
        report.validate_scenario(record)

    def test_lagged_image_reports_nondeterministic(self):
        self.feed.lagged_image = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'command-abort-verdict-nondeterministic'),
            record['detail'])
        self.assertIn('served image', record['detail'])
        report.validate_scenario(record)

    def test_moved_roles_report_nondeterministic(self):
        self.feed.role_moved = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'command-abort-verdict-nondeterministic'),
            record['detail'])
        self.assertIn('launch roles', record['detail'])
        report.validate_scenario(record)

    def test_answered_post_reports_inconclusive(self):
        # The pinned lane answering inside the bound means the abort
        # window never staged — the leg reports inconclusive, never a
        # verdict.
        record = self.run_scenario(post=(
            'answered', {'outcome': {'applied': {'tick': 1}}}))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('abort window never staged', record['detail'])
        report.validate_scenario(record)

    def test_no_journal_files_reports_inconclusive(self):
        ctx = self._ctx(self.feed)
        del ctx['journal_files']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal-file', record['detail'])
        report.validate_scenario(record)

    def test_no_active_reports_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active', record['detail'])
        report.validate_scenario(record)

    def test_silent_audit_reports_unchecked(self):
        # The self-check leg: the settlement judge, silenced, must turn the
        # planted negatives into the unchecked diagnostic.
        with patch.object(scenarios, '_judge_settle',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'command-abort-verdict-unchecked'), record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            feed = AbortVerdictFeed()
            evidence = Path(self.tmp.name) / ('evidence-run'
                                              + str(len(runs)))
            evidence.mkdir()
            self.evidence = evidence
            record = self.run_scenario(feed=feed,
                                       ctx=self._ctx(feed))
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
