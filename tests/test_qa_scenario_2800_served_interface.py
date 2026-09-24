"""The 2800_served_interface leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_served_interface, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'ServedInterfaceTests.test_clean_feed_passes_and_validates',
    'ServedInterfaceTests.test_missing_declared_kind_fails',
    'ServedInterfaceTests.test_missing_collection_fails',
    'ServedInterfaceTests.test_command_without_receipt_fails',
    'ServedInterfaceTests.test_unobserved_event_fails',
})


class ServedFeed:
    """A stubbed monitor pair for the served-interface scenario. The rig
    declares two instances whose served interfaces both bind the
    preferred writable bool point — the pump-station model's p101-oos
    fan-out — and the command path settles every submission into a
    journaled receipt `/resources` attributes per instance, mirroring
    the served contract's own rules. Fault flags stage each named
    failure the issue calls out."""

    COMPONENTS = ({'name': 'digital-input:12', 'kind': 'digital-input'},
                  {'name': 'motor:21', 'kind': 'motor'})

    def __init__(self):
        self.tick = 0
        self.journal = []
        self.next_seq = 1
        self.bound = {'digital-input:12': {302}, 'motor:21': {302}}
        # Fault injection for the named-failure cases.
        self.missing_kind = False        # /schema drops an instance
        self.missing_collection = False  # interfaces serve four of five
        self.no_receipt = False          # /command answers an HTTP error
        self.no_events = False           # the produced event never shows

    def _interface(self, kind, port, point):
        interface = {
            'version': 1, 'kind': kind,
            'measurements': [{'name': 'out', 'direction': 'out',
                              'kind': 'bool', 'point': point + 1}],
            'configuration': [{'name': 'invert', 'kind': 'bool',
                               'capability': 'tunable'}],
            'state': [{'name': 'fault', 'direction': 'out',
                       'kind': 'bool', 'persistence': 'bound_point'}],
            'commands': [
                {'name': 'write_value:' + port,
                 'request': [{'name': 'value', 'kind': 'bool'}],
                 'availability': 'bound_point_writable',
                 'adapted': 'write_value', 'point': point},
                {'name': 'set_parameter:invert',
                 'request': [{'name': 'value', 'kind': 'bool'}],
                 'availability': 'always',
                 'adapted': 'set_parameter'}],
            'events': [{'name': 'command_settled', 'payload': [],
                        'retention': 'journal',
                        'emission': 'on_command_settled',
                        'adapted': 'command_settled'}]}
        if self.missing_collection:
            del interface['events']
        return interface

    def _attributed(self, entry, name):
        """The resource view's join: a settled receipt belongs to the
        instance its command names or whose bound points it writes."""
        event = entry.get('event') or {}
        command = ((event.get('command_settled') or {})
                   .get('receipt') or {}).get('command') or {}
        for variant in ('invoke', 'set_parameter'):
            if (command.get(variant) or {}).get('component') == name:
                return True
        for variant in ('write_value', 'force_point', 'unforce_point'):
            point = (command.get(variant) or {}).get('point')
            if point is not None and point in self.bound.get(name, ()):
                return True
        return False

    def http_json(self, method, url, body=None, timeout=10):
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        self.tick += 1
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 302, 'signal': 10302, 'name': 'p101-oos',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True},
                {'point': 10, 'signal': 10010, 'name': 'level-primary',
                 'direction': 'in', 'value_type': 'float',
                 'writable': False}],
                'components': [dict(c) for c in self.COMPONENTS]}
        if (method, route) == ('GET', '/schema'):
            interfaces = [
                {'name': 'digital-input:12',
                 'interface': self._interface('digital-input', 'in', 302)},
                {'name': 'motor:21',
                 'interface': self._interface('motor', 'oos', 302)}]
            if self.missing_kind:
                interfaces = interfaces[:1]
            return 200, {'publication': self.tick, 'tick': self.tick,
                         'interfaces': interfaces}
        if (method, route) == ('GET', '/resources'):
            return 200, {
                'publication': self.tick, 'tick': self.tick,
                'components': [
                    {'name': c['name'], 'kind': c['kind'],
                     'measurements': [], 'configuration': [],
                     'state': [], 'commands': [],
                     'events': [] if self.no_events else
                     [entry for entry in self.journal
                      if self._attributed(entry, c['name'])]}
                    for c in self.COMPONENTS]}
        if (method, route) == ('POST', '/command'):
            if self.no_receipt:
                error = urllib.error.HTTPError(
                    url, 500, 'command path broken', {}, None)
                error.close()
                raise error
            receipt = {'command': body['command'],
                       'outcome': {'applied': {'tick': self.tick}},
                       'actor': body.get('actor')}
            self.journal.append(
                {'seq': self.next_seq, 'tick': self.tick,
                 'event': {'command_settled': {'receipt': receipt}}})
            self.next_seq += 1
            return 200, receipt
        raise AssertionError('unexpected request %s %s' % (method, url))


class ServedInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = ServedFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'CONTRACT_DEADLINE', 0.05):
            return scenarios.scenario_served_interface(ctx)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_missing_declared_kind_fails(self):
        # The served registry drops an instance the model declares —
        # the coverage check must name the uncovered kind.
        self.feed.missing_kind = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('misses declared kinds', record.get('detail', ''))
        self.assertIn('motor', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_collection_fails(self):
        self.feed.missing_collection = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('miss collections', record.get('detail', ''))
        report.validate_scenario(record)

    def test_command_without_receipt_fails(self):
        self.feed.no_receipt = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no receipt', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unobserved_event_fails(self):
        self.feed.no_events = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never reflected a produced event',
                      record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
