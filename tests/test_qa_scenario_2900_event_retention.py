"""The 2900_event_retention leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_event_retention, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'EventRetentionTests.test_registered_and_replayable',
    'EventRetentionTests.test_clean_feed_passes_and_validates',
    'EventRetentionTests.test_two_runs_produce_identical_evidence',
    'EventRetentionTests.test_history_event_journaled_fails',
    'EventRetentionTests.test_history_not_growing_on_repeat_fails',
    'EventRetentionTests.test_latest_accumulating_records_fails',
    'EventRetentionTests.test_latest_stale_on_repeat_fails',
    'EventRetentionTests.test_standby_journaling_routed_events_fails',
    'EventRetentionTests.test_standby_admitting_the_drive_fails',
    'EventRetentionTests.test_standby_without_descriptor_fails',
    'EventRetentionTests.test_no_qualifying_component_is_inconclusive',
    'EventRetentionTests.test_declared_but_unserved_component_is_inconclusive',
    'EventRetentionTests.test_no_emission_path_is_inconclusive',
    'EventRetentionTests.test_drives_without_emissions_is_inconclusive',
})


class RetentionFeed:
    """A stubbed pair for the event-retention scenario. ctrl-a is the
    active and ctrl-b its tracking standby; the served model declares
    one component whose interface carries a journal-, a history-, and
    a latest-retained kind-emitted event beside a kind-declared
    command whose every accepted submission emits all three — the
    emit-identical record the standby mirrors, so its journal carries
    the journal-retained `fired` too while the routed `shift`/`beat`
    only ever land in their own stores. Fault flags stage each named
    failure the issue calls out."""

    COMPONENTS = ({'name': 'seq:1', 'kind': 'sequencer'},)

    def __init__(self):
        self.tick = 0
        self.journal = []        # the active's journal entries
        self.peer_journal = []   # the tracking standby's — emit-identical
        self.history = []        # the routed event-history ring's records
        self.latest = {}         # (component, event) -> standing record
        self.latest_extra = []   # spurious second standing records
        self.seq = 1             # the active journal's next seq
        self.peer_seq = 1        # the standby journal's next seq
        self.event_seq = 1       # the routed stream's next seq
        self.emissions = 0
        # Fault injection for the named-failure cases.
        self.history_to_journal = False  # `shift` lands in the journal
        self.history_freeze = False      # repeats add no `shift` records
        self.latest_accumulates = False  # `beat` stands twice
        self.latest_stale = False        # repeats leave `beat` unchanged
        self.standby_journals = False    # standby journals `shift`
        self.standby_admits = False      # standby accepts the drive
        self.peer_undocumented = False   # standby serves no descriptor
        self.no_qualifier = False        # no routed retentions declared
        self.unserved = False            # /resources omits the component
        self.no_path = False             # no commands to drive through
        self.no_emissions = False        # accepted drives emit nothing

    def _interface(self):
        events = [{'name': 'fired', 'payload': [],
                   'retention': 'journal', 'emission': 'kind_emitted',
                   'adapted': 'declared'}]
        if not self.no_qualifier:
            events += [
                {'name': 'shift', 'payload': [],
                 'retention': 'history', 'emission': 'kind_emitted',
                 'adapted': 'declared'},
                {'name': 'beat', 'payload': [],
                 'retention': 'latest', 'emission': 'kind_emitted',
                 'adapted': 'declared'}]
        commands = [] if self.no_path else [
            {'name': 'advance',
             'request': [{'name': 'count', 'kind': 'int'}],
             'availability': 'kind_declared', 'adapted': 'declared'}]
        return {'version': 1, 'kind': 'sequencer',
                'measurements': [], 'configuration': [], 'state': [],
                'commands': commands, 'events': events}

    def _record(self, name, retention, n):
        record = {'seq': self.event_seq, 'tick': self.tick,
                  'retention': retention,
                  'event': {'event_emitted': {'event': {
                      'event': name, 'component': 'seq:1',
                      'fields': {'n': {'int': n}}}}}}
        self.event_seq += 1
        return record

    # One emission batch per applied drive: `fired` journals on both
    # peers — the emit-identical durable record — `shift` routes to the
    # event-history ring, `beat` overwrites the standing record.
    def _emit(self):
        if self.no_emissions:
            return
        self.emissions += 1
        n = self.emissions
        for journal, key in ((self.journal, 'seq'),
                             (self.peer_journal, 'peer_seq')):
            journal.append({'seq': getattr(self, key),
                            'tick': self.tick,
                            'event': {'event_emitted': {'event': {
                                'event': 'fired', 'component': 'seq:1',
                                'fields': {'n': {'int': n}}}}}})
            setattr(self, key, getattr(self, key) + 1)
        if self.history_to_journal:
            self.journal.append(
                {'seq': self.seq, 'tick': self.tick,
                 'event': {'event_emitted': {'event': {
                     'event': 'shift', 'component': 'seq:1',
                     'fields': {'n': {'int': n}}}}}})
            self.seq += 1
        elif not (self.history_freeze and n > 1):
            self.history.append(self._record('shift', 'history', n))
        if self.standby_journals:
            self.peer_journal.append(
                {'seq': self.peer_seq, 'tick': self.tick,
                 'event': {'event_emitted': {'event': {
                     'event': 'shift', 'component': 'seq:1',
                     'fields': {'n': {'int': n}}}}}})
            self.peer_seq += 1
        if self.latest_accumulates:
            self.latest_extra.append(
                self._record('beat', 'latest', n))
        elif not (self.latest_stale and n > 1):
            self.latest[('seq:1', 'beat')] = \
                self._record('beat', 'latest', n)

    def _events(self, journal):
        # The resource view's join: the attributed journal tail beside
        # the routed history ring and the standing latest records.
        attributed = [entry for entry in journal
                      if ((entry.get('event') or {})
                          .get('event_emitted', {}).get('event') or {})
                      .get('component') == 'seq:1'
                      or (((entry.get('event') or {})
                           .get('command_settled', {})
                           .get('receipt') or {})
                          .get('command') or {})
                      .get('invoke', {}).get('component') == 'seq:1']
        return [dict(entry, retention='journal')
                for entry in attributed] \
            + list(self.history) + list(self.latest.values()) \
            + self.latest_extra

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        self.tick += 1
        standby = host == 'ctrl-b:2'
        if (method, route) == ('GET', '/role'):
            if standby:
                return 200, {'role': 'standby', 'tick': self.tick,
                             'sync': {'tracking': {
                                 'aligned': self.tick}}}
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 302, 'signal': 10302, 'name': 'p101-oos',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True}],
                'components': [dict(c) for c in self.COMPONENTS]}
        if (method, route) == ('GET', '/schema'):
            if standby and self.peer_undocumented:
                return 200, {'publication': self.tick,
                             'tick': self.tick, 'interfaces': []}
            return 200, {'publication': self.tick, 'tick': self.tick,
                         'interfaces': [
                             {'name': 'seq:1',
                              'interface': self._interface()}]}
        if (method, route) == ('GET', '/resources'):
            journal = self.peer_journal if standby else self.journal
            components = [] if (not standby and self.unserved) else [
                {'name': 'seq:1', 'kind': 'sequencer',
                 'measurements': [], 'configuration': [],
                 'state': [], 'commands': [],
                 'events': self._events(journal)}]
            return 200, {'publication': self.tick, 'tick': self.tick,
                         'components': components}
        if (method, route) == ('GET', '/journal'):
            journal = self.peer_journal if standby else self.journal
            return 200, list(journal)
        if (method, route) == ('GET', '/history'):
            return 200, []
        if (method, route) == ('POST', '/command'):
            command = body['command']
            if standby and not self.standby_admits:
                return 200, {'command': command,
                             'outcome': {'rejected': {'reason': {
                                 'not_active': {'point': None,
                                                'role': 'standby'}}}},
                             'actor': body.get('actor')}
            receipt = {'command': command,
                       'outcome': {'applied': {'tick': self.tick}},
                       'actor': body.get('actor')}
            journal = self.peer_journal if standby else self.journal
            key = 'peer_seq' if standby else 'seq'
            journal.append({'seq': getattr(self, key),
                            'tick': self.tick,
                            'event': {'command_settled': {
                                'receipt': receipt}}})
            setattr(self, key, getattr(self, key) + 1)
            if 'invoke' in command:
                self._emit()
            return 200, receipt
        raise AssertionError('unexpected request %s %s' % (method, url))


class EventRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = RetentionFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'RETENTION_DEADLINE', 0.05):
            return scenarios.scenario_event_retention(ctx)

    def test_registered_and_replayable(self):
        self.assertIn(scenarios.scenario_event_retention,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('event-retention'),
                      scenarios.scenario_event_retention)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_two_runs_produce_identical_evidence(self):
        first = self.run_scenario()
        names = sorted(entry['ref'] for entry in first['evidence'])
        contents = {name: (self.evidence.parent / name).read_text()
                    for name in names}
        self.tmp2 = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp2.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = RetentionFeed()
        try:
            second = self.run_scenario()
            self.assertEqual(second['outcome'], 'passed', second)
            self.assertEqual(
                names,
                sorted(entry['ref'] for entry in second['evidence']))
            for name, text in contents.items():
                self.assertEqual(
                    (self.evidence.parent / name).read_text(), text,
                    name)
        finally:
            self.tmp2.cleanup()

    def test_history_event_journaled_fails(self):
        # A History-declared emission landing in the durable journal —
        # the routed record it never reached.
        self.feed.history_to_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shift', record.get('detail', ''))
        self.assertIn('journal', record.get('detail', ''))
        report.validate_scenario(record)

    def test_history_not_growing_on_repeat_fails(self):
        self.feed.history_freeze = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('did not grow the event-history record',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_latest_accumulating_records_fails(self):
        self.feed.latest_accumulates = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('accumulates standing latest records',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_latest_stale_on_repeat_fails(self):
        self.feed.latest_stale = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stale', record.get('detail', ''))
        report.validate_scenario(record)

    def test_standby_journaling_routed_events_fails(self):
        self.feed.standby_journals = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('event_emitted', record.get('detail', ''))
        report.validate_scenario(record)

    def test_standby_admitting_the_drive_fails(self):
        self.feed.standby_admits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('admitted the driven command',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_standby_without_descriptor_fails(self):
        self.feed.peer_undocumented = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('publishes no descriptor',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_qualifying_component_is_inconclusive(self):
        self.feed.no_qualifier = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_declared_but_unserved_component_is_inconclusive(self):
        # The registry declares the qualifying interface but the
        # resource view never serves the instance — locating runs
        # through both endpoints, so nothing qualifies.
        self.feed.unserved = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_no_emission_path_is_inconclusive(self):
        self.feed.no_path = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_drives_without_emissions_is_inconclusive(self):
        self.feed.no_emissions = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
