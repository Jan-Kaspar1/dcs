"""The 3400_command_availability leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_command_availability, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'CommandAvailabilityTests.test_registered_in_scenarios',
    'CommandAvailabilityTests.test_clean_feed_passes_and_validates',
    'CommandAvailabilityTests.test_unnamed_refusal_fails',
    'CommandAvailabilityTests.test_refusal_on_available_fails',
    'CommandAvailabilityTests.test_applies_refused_fails',
    'CommandAvailabilityTests.test_mismatched_refusal_fails',
    'CommandAvailabilityTests.test_refuses_available_fails',
    'CommandAvailabilityTests.test_diverged_standby_fails',
    'CommandAvailabilityTests.test_no_command_rows_is_inconclusive',
    'CommandAvailabilityTests.test_undeclared_model_notes_the_coverage_limitation',
    'CommandAvailabilityTests.test_no_tracking_peer_notes_the_uncovered_leg',
    'CommandAvailabilityTests.test_two_runs_produce_identical_evidence',
})


class AvailabilityFeed:
    """A stubbed monitor pair for the command-availability scenario.
    ctrl-a serves the settled active; ctrl-b reports a tracking
    standby answering the same verdict rows. The served model: a
    managed-latching-alarm instance whose `shelve`/`suppress` ports
    bind non-writable bool points — the BoundPointWritable refusals
    the pump-station rig declares — a motor whose `oos` port binds the
    writable p101-oos point, a tunable parameter, and, while
    `declared` is set, a sequencer instance whose kind-declared
    `advance` command stands refused on its completed table. POST
    /command settles receipts through the same shapes the receipted
    path answers: a refused submission rejects at admission, an
    accepted one settles at the next read's scan boundary. Fault
    flags stage each named failure the issue calls out."""

    def __init__(self):
        self.tick = 0
        self.receipts = []
        self.point = False
        self.declared = True  # a kind-declared command rides the model
        # Fault injection for the named-failure cases.
        self.unnamed_refusal = False    # an available:false row lacks
                                        # its named refusal
        self.refusal_on_available = False  # an available row serves one
        self.applies_refused = False    # dispatch applies the refused
                                        # probe anyway
        self.mismatched_refusal = False  # settled reason != served text
        self.refuses_available = False  # served-available probe settles
                                        # a named refusal
        self.diverged_standby = False   # the tracking peer flips a row
        self.no_commands = False        # /resources serves no rows
        self.no_tracking = False        # ctrl-b reports no convergence

    def _components(self):
        components = [('lah-alarm:5', 'managed-latching-alarm'),
                      ('motor:22', 'motor')]
        if self.declared:
            components.append(('seq:9', 'sequencer'))
        return components

    def _commands(self, standby=False):
        commands = {
            'lah-alarm:5': [
                {'name': 'write_value:shelve', 'point': 1001,
                 'available': False,
                 'refusal': 'I/O point PointId(1001) is not declared '
                            'writable'},
                {'name': 'write_value:suppress', 'point': 331,
                 'available': False,
                 'refusal': 'I/O point PointId(331) is not declared '
                            'writable'},
                {'name': 'set_parameter:high_limit', 'available': True}],
            'motor:22': [
                {'name': 'write_value:oos', 'point': 302,
                 'available': True}],
        }
        if self.declared:
            commands['seq:9'] = [
                {'name': 'advance', 'available': False,
                 'refusal': 'the step table is complete'},
                {'name': 'reset', 'available': True}]
        if self.unnamed_refusal:
            commands['lah-alarm:5'][0] = {
                'name': 'write_value:shelve', 'point': 1001,
                'available': False}
        if self.refusal_on_available:
            commands['motor:22'][0] = dict(commands['motor:22'][0],
                                           refusal='served anyway')
        if standby and self.diverged_standby:
            commands['motor:22'][0] = {
                'name': 'write_value:oos', 'point': 302,
                'available': False,
                'refusal': 'I/O point PointId(302) is not declared '
                           'writable'}
        return commands

    def _interface(self, kind):
        if kind == 'managed-latching-alarm':
            commands = [
                {'name': 'write_value:shelve',
                 'request': [{'name': 'value', 'kind': 'bool'}],
                 'availability': 'bound_point_writable',
                 'adapted': 'write_value', 'point': 1001},
                {'name': 'write_value:suppress',
                 'request': [{'name': 'value', 'kind': 'bool'}],
                 'availability': 'bound_point_writable',
                 'adapted': 'write_value', 'point': 331},
                {'name': 'set_parameter:high_limit',
                 'request': [{'name': 'value', 'kind': 'float'}],
                 'availability': 'always', 'adapted': 'set_parameter'}]
        elif kind == 'motor':
            commands = [{'name': 'write_value:oos',
                         'request': [{'name': 'value', 'kind': 'bool'}],
                         'availability': 'bound_point_writable',
                         'adapted': 'write_value', 'point': 302}]
        else:
            commands = [
                {'name': 'advance',
                 'request': [{'name': 'count', 'kind': 'int'}],
                 'availability': 'kind_declared',
                 'adapted': 'declared'},
                {'name': 'reset', 'request': [],
                 'availability': 'always', 'adapted': 'declared'}]
        return {'version': 1, 'kind': kind, 'measurements': [],
                'configuration': [], 'state': [], 'commands': commands,
                'events': []}

    def _advance(self):
        # One completed scan per measurement read: the boundary settles
        # every queued receipt whose apply_tick has arrived.
        self.tick += 1
        for receipt in self.receipts:
            accepted = receipt['outcome'].get('accepted')
            if not accepted or self.tick < accepted['apply_tick']:
                continue
            command = receipt['command']
            write = command.get('write_value')
            invoke = command.get('invoke')
            if write and write['point'] == 302:
                self.point = write['value']['bool']
                receipt['outcome'] = {'applied': {'tick': self.tick}} \
                    if not self.refuses_available \
                    else {'rejected': {'reason': {
                        'not_writable': {'point': 302}}}}
            elif invoke and invoke['command'] == 'advance' \
                    and not self.applies_refused:
                reason = 'the step table is complete'
                if self.mismatched_refusal:
                    reason = 'a different refusal'
                receipt['outcome'] = {'rejected': {'reason': {
                    'command_refused': {
                        'component': invoke['component'],
                        'command': invoke['command'],
                        'reason': reason}}}}
            else:
                receipt['outcome'] = {'applied': {'tick': self.tick}}

    def _admit(self, body):
        command = body['command']
        write = command.get('write_value')
        if write and write['point'] != 302 and not self.applies_refused:
            reason = {'not_writable': {'point': write['point']}}
            if self.mismatched_refusal:
                reason = {'unknown_point': {'point': write['point']}}
            receipt = {'command': command,
                       'outcome': {'rejected': {'reason': reason}},
                       'actor': body.get('actor')}
            self.receipts.append(receipt)
            return receipt
        receipt = {'command': command,
                   'outcome': {'accepted': {'apply_tick': self.tick + 1}},
                   'actor': body.get('actor')}
        self.receipts.append(receipt)
        return receipt

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        self._advance()
        if host == 'ctrl-b:2':
            if (method, route) == ('GET', '/role'):
                sync = {'tracking': {'aligned': self.tick}} \
                    if not self.no_tracking else {'degraded': {}}
                return 200, {'role': 'standby', 'tick': self.tick,
                             'sync': sync}
            if (method, route) == ('GET', '/resources'):
                return 200, {
                    'publication': self.tick, 'tick': self.tick,
                    'components': [
                        {'name': name, 'kind': kind,
                         'measurements': [], 'configuration': [],
                         'state': [],
                         'commands': [] if self.no_commands
                         else self._commands(standby=True).get(name, []),
                         'events': []}
                        for name, kind in self._components()]}
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 302, 'signal': 10302, 'name': 'p101-oos',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True},
                {'point': 1001, 'signal': 11001, 'name': 'lah-shelve',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': False},
                {'point': 331, 'signal': 10331, 'name': 'lah-suppress',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': False}],
                'components': [{'name': name, 'kind': kind}
                               for name, kind in self._components()]}
        if (method, route) == ('GET', '/schema'):
            return 200, {'publication': self.tick, 'tick': self.tick,
                         'interfaces': [
                             {'name': name,
                              'interface': self._interface(kind)}
                             for name, kind in self._components()]}
        if (method, route) == ('GET', '/resources'):
            return 200, {
                'publication': self.tick, 'tick': self.tick,
                'components': [
                    {'name': name, 'kind': kind,
                     'measurements': [], 'configuration': [],
                     'state': [],
                     'commands': [] if self.no_commands
                     else self._commands().get(name, []),
                     'events': []}
                    for name, kind in self._components()]}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts)
        if (method, route) == ('POST', '/command'):
            return 200, self._admit(body)
        raise AssertionError('unexpected request %s %s' % (method, url))


class CommandAvailabilityTests(unittest.TestCase):
    """scenario_command_availability against the stubbed pair: every
    fault flag stages a named acceptance failure — an inconsistent
    verdict row, a refused command that applies anyway, a settled
    refusal naming a different reason than the served row, an
    available command settling refused, a diverged tracking peer —
    and the inconclusive and coverage-limitation paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = AvailabilityFeed()

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
                patch.object(scenarios, 'AVAILABILITY_DEADLINE', 0.5):
            return scenarios.scenario_command_availability(
                ctx or self._ctx())

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_command_availability,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('command-availability'),
                      scenarios.scenario_command_availability)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

        # The refused probe is the BoundPointWritable write the
        # managed-alarm shelve port declares; the available probe is
        # the writable p101-oos write.
        refused = json.loads(
            (self.evidence / 'command-availability-refused.json')
            .read_text())
        self.assertEqual(refused['command'],
                         {'write_value': {'point': 1001, 'kind': 'bool',
                                          'value': {'bool': True}}})
        self.assertIn('rejected', refused['settled']['outcome'])
        applied = json.loads(
            (self.evidence / 'command-availability-available.json')
            .read_text())
        self.assertEqual(applied['command'],
                         {'write_value': {'point': 302, 'kind': 'bool',
                                          'value': {'bool': True}}})
        self.assertIn('applied', applied['settled']['outcome'])

    def test_unnamed_refusal_fails(self):
        self.feed.unnamed_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('without a named refusal',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refusal_on_available_fails(self):
        self.feed.refusal_on_available = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('available with a refusal',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_applies_refused_fails(self):
        self.feed.applies_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('settled applied anyway',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_mismatched_refusal_fails(self):
        self.feed.mismatched_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('names a different reason than the served row',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refuses_available_fails(self):
        self.feed.refuses_available = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('settled a named refusal',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_diverged_standby_fails(self):
        self.feed.diverged_standby = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reports different verdicts',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_command_rows_is_inconclusive(self):
        self.feed.no_commands = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no command rows', record.get('detail', ''))
        report.validate_scenario(record)

    def test_undeclared_model_notes_the_coverage_limitation(self):
        # The pump-station rig shape: no kind-declared-availability
        # command — the limitation is recorded, the bound-point
        # verdicts still assert.
        self.feed.declared = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertTrue(any('no kind-declared-availability command'
                            in note
                            for note in record['observations']),
                        record['observations'])
        report.validate_scenario(record)

    def test_no_tracking_peer_notes_the_uncovered_leg(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertTrue(any('no tracking standby' in note
                            for note in record['observations']),
                        record['observations'])
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
        feed2 = AvailabilityFeed()
        record2 = self.run_scenario(ctx=self._ctx(evidence2), feed=feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)


if __name__ == '__main__':
    unittest.main()
