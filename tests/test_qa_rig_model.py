"""The lane-owned derived rig models' unit coverage: both variants
derive deterministically, additively, and collision-guarded from the
run's real mounted fixture, re-check against the structural lint a
derivation can break, and produce a document the model contract
assembles — the two-station `sim-cyclic` variant that gives decision
78's per-station attribution something to attribute, and the writable
field `In` point that gives a receipted command a driver write to
reach. Every violation each derivation can meet is refused by name,
and the pinned fixture is proven untouched: these documents are
derived per run, never edited into the byte-pinned artifacts."""
import json
import unittest
from pathlib import Path

from qa_lane import revision, rig_model

FIXTURE = (Path(__file__).resolve().parent.parent / 'crates'
           / 'dcs-demo' / 'fixtures' / 'pump_station.json')
CYCLIC = (Path(__file__).resolve().parent.parent / 'crates'
          / 'dcs-demo' / 'fixtures' / 'wago_rig_cyclic.json')


def _fixture():
    return json.loads(FIXTURE.read_text())


def _cyclic():
    return json.loads(CYCLIC.read_text())


def _channels(document, device):
    """The `(device, channel)` pairs the document's io_points bind —
    the map a driver binds at most once, so a derived document that
    duplicated one would refuse to assemble."""
    return [(point['channel']['device'], point['channel']['name'])
            for point in document['io_points'] if 'channel' in point]


class TwoStationCyclicTests(unittest.TestCase):
    """The two-station `sim-cyclic` derivation: the attribution clause
    needs a document where one station's points can degrade beside
    another's staying fresh."""

    def test_derivation_is_deterministic_and_additive(self):
        document = _cyclic()
        first = rig_model.two_station_cyclic(document)
        second = rig_model.two_station_cyclic(document)
        self.assertEqual(first['document'], second['document'])
        # The mounted document is untouched, and everything it carried
        # survives: the derivation only adds.
        stations = document['devices'][0]['parameters']['stations']
        self.assertNotIn(rig_model.DERIVED_STATION, stations)
        for point in document['io_points']:
            self.assertIn(point, first['document']['io_points'])
        self.assertEqual(len(first['document']['io_points']),
                         len(document['io_points']) + 1)
        self.assertEqual(len(first['document']['signals']),
                         len(document['signals']) + 1)

    def test_the_derived_document_lints_clean_and_assembles_by_contract(self):
        derivation = rig_model.two_station_cyclic(_cyclic())
        self.assertEqual(revision.lint(derivation['document']), [])
        document = derivation['document']
        device = document['devices'][0]
        self.assertEqual(device['kind'], 'sim-cyclic')
        # The added station declares its own channel and its own
        # registers, so a shortfall on it leaves the coupler station's
        # registers alone.
        self.assertIn(derivation['station_channel'], device['channels'])
        self.assertEqual(
            device['channels'][derivation['station_channel']],
            {'direction': 'in', 'value_type': 'bool'})
        self.assertIn(derivation['station'], device['parameters']['stations'])
        # The coupler station's registers are the mounted ones, untouched.
        mounted = _cyclic()['devices'][0]['parameters']['stations']
        for name, registers in mounted['750-354'].items():
            self.assertEqual(
                device['parameters']['stations']['750-354'][name],
                registers)
        added = next(point for point in document['io_points']
                     if point['id'] == derivation['station_point'])
        self.assertEqual(added['direction'], 'in')
        self.assertEqual(added['channel'],
                         {'device': device['id'],
                          'name': derivation['station_channel']})
        signal = next(entry for entry in document['signals']
                      if entry['source'] == derivation['station_point'])
        self.assertEqual(signal['name'],
                         'rig-' + derivation['station_channel'])
        # Every channel is still bound once, which is what assembly
        # refuses a duplicate of.
        bound = _channels(document, device['id'])
        self.assertEqual(len(bound), len(set(bound)))

    def test_the_derived_point_id_stays_above_the_mounted_maximum(self):
        document = _cyclic()
        derivation = rig_model.two_station_cyclic(document)
        self.assertGreater(derivation['station_point'],
                           max(point['id'] for point in document['io_points']))

    def test_every_violation_is_refused_by_name(self):
        with self.assertRaises(rig_model.RigModelError) as raised:
            rig_model.two_station_cyclic({'devices': [], 'io_points': [],
                                          'signals': []})
        self.assertIn('declares no device', str(raised.exception))

        document = _cyclic()
        document['devices'][0]['kind'] = 'sim-di'
        with self.assertRaises(rig_model.RigModelError) as raised:
            rig_model.two_station_cyclic(document)
        self.assertIn('not sim-cyclic', str(raised.exception))

        document = _cyclic()
        document['devices'][0]['parameters']['stations'] = {}
        with self.assertRaises(rig_model.RigModelError) as raised:
            rig_model.two_station_cyclic(document)
        self.assertIn('no stations map', str(raised.exception))

        document = _cyclic()
        document['devices'][0]['parameters']['stations'][
            rig_model.DERIVED_STATION] = {'di3': 7}
        with self.assertRaises(rig_model.RigModelError) as raised:
            rig_model.two_station_cyclic(document)
        self.assertIn('already declares the derived station',
                      str(raised.exception))

        document = _cyclic()
        document['devices'][0]['channels'][rig_model.EXTRA_CYCLIC_CHANNEL] = {
            'direction': 'in', 'value_type': 'bool'}
        with self.assertRaises(rig_model.RigModelError) as raised:
            rig_model.two_station_cyclic(document)
        self.assertIn('already declares the channel', str(raised.exception))

        with self.assertRaises(rig_model.RigModelError) as raised:
            rig_model.two_station_cyclic('not a document')
        self.assertIn('not a JSON object', str(raised.exception))

    def test_describe_reports_what_it_added_without_the_document(self):
        description = rig_model.describe(
            rig_model.two_station_cyclic(_cyclic()))
        self.assertNotIn('document', description)
        self.assertEqual(description['station'], rig_model.DERIVED_STATION)
        self.assertIn('station_point', description)


class WritableFieldPointTests(unittest.TestCase):
    """The writable field `In` point derivation: the plant every
    command-writable point of which is internal and channel-less has no
    receipted command that reaches a driver write, so this variant adds
    the one the settle contract needs."""

    def test_derivation_is_deterministic_and_additive(self):
        document = _fixture()
        first = rig_model.writable_field_point(document, 2, 'write-back')
        second = rig_model.writable_field_point(document, 2, 'write-back')
        self.assertEqual(first['document'], second['document'])
        self.assertEqual(len(document['io_points']),
                         len(_fixture()['io_points']))
        for point in document['io_points']:
            self.assertIn(point, first['document']['io_points'])
        self.assertEqual(len(first['document']['io_points']),
                         len(document['io_points']) + 1)

    def test_the_derived_document_lints_clean_and_declares_the_channel(self):
        derivation = rig_model.writable_field_point(_fixture(), 2,
                                                    'write-back')
        document = derivation['document']
        self.assertEqual(revision.lint(document), [])
        device = next(entry for entry in document['devices']
                      if entry['id'] == 2)
        self.assertIn(derivation['channel'], device['channels'])
        self.assertEqual(device['channels'][derivation['channel']],
                         {'direction': 'in', 'value_type': 'bool'})
        point = next(entry for entry in document['io_points']
                     if entry['id'] == derivation['point'])
        self.assertEqual(point['direction'], 'in')
        self.assertTrue(point['writable'])
        self.assertEqual(point['channel'],
                         {'device': 2, 'name': derivation['channel']})
        signal = next(entry for entry in document['signals']
                      if entry['id'] == derivation['signal'])
        self.assertEqual(signal['source'], derivation['point'])
        # A field point carries no `initial`: the field asserts its own
        # value, and an image-held one would be the internal-point rule.
        self.assertNotIn('initial', point)
        # Every channel is bound once — a driver refuses a second
        # binding of one channel, so a derived point riding a mounted
        # channel would make the document unassemblable.
        bound = _channels(document, 2)
        self.assertEqual(len(bound), len(set(bound)))

    def test_an_already_bound_channel_is_never_shared(self):
        # The pinned fixture binds every channel its devices declare, so
        # asking for one of them is the common case: the derivation adds
        # a channel of its own name rather than binding a channel
        # twice.
        derivation = rig_model.writable_field_point(_fixture(), 2, 'p101-run')
        self.assertEqual(derivation['requested_channel'], 'p101-run')
        self.assertNotEqual(derivation['channel'], 'p101-run')
        self.assertTrue(derivation['added_channel'])
        self.assertEqual(derivation['value_type'], 'bool')
        bound = _channels(derivation['document'], 2)
        self.assertEqual(len(bound), len(set(bound)))
        self.assertIn((2, 'p101-run'), bound)
        self.assertIn((2, derivation['channel']), bound)

    def test_an_unbound_declared_channel_is_bound_as_it_stands(self):
        document = _fixture()
        # A declared, in-direction, unbound channel: the derivation
        # binds it without adding a declaration of its own.
        device = next(entry for entry in document['devices']
                      if entry['id'] == 2)
        device['channels']['spare'] = {'direction': 'in',
                                       'value_type': 'bool'}
        derivation = rig_model.writable_field_point(document, 2, 'spare')
        self.assertEqual(derivation['channel'], 'spare')
        self.assertFalse(derivation['added_channel'])
        self.assertEqual(
            next(entry for entry in derivation['document']['devices']
                 if entry['id'] == 2)['channels']['spare'],
            {'direction': 'in', 'value_type': 'bool'})

    def test_the_derived_ids_stay_above_the_mounted_maximum(self):
        document = _fixture()
        derivation = rig_model.writable_field_point(document, 2, 'write-back')
        self.assertGreater(derivation['point'],
                           max(point['id'] for point in document['io_points']))
        self.assertGreater(derivation['signal'],
                           max(signal['id'] for signal in document['signals']))

    def test_every_violation_is_refused_by_name(self):
        document = _fixture()
        device = next(entry for entry in document['devices']
                      if entry['id'] == 2)
        device['channels']['do1'] = {'direction': 'out',
                                      'value_type': 'bool'}
        with self.assertRaises(rig_model.RigModelError) as raised:
            rig_model.writable_field_point(document, 2, 'do1')
        self.assertIn('must ride an input channel', str(raised.exception))

        document = _fixture()
        next(entry for entry in document['devices']
             if entry['id'] == 2)['channels']['broken'] = 'nonsense'
        with self.assertRaises(rig_model.RigModelError) as raised:
            rig_model.writable_field_point(document, 2, 'broken')
        self.assertIn('no usable shape', str(raised.exception))

        document = _fixture()
        next(entry for entry in document['devices']
             if entry['id'] == 2)['channels'] = {'p101-run': 'nonsense'}
        with self.assertRaises(rig_model.RigModelError) as raised:
            rig_model.writable_field_point(document, 2, 'write-back')
        self.assertIn('no usable value_type', str(raised.exception))

        document = _fixture()
        with self.assertRaises(rig_model.RigModelError) as raised:
            rig_model.writable_field_point(document, 2, 'write-back',
                                           point_id=300)
        self.assertIn('collides with the mounted model',
                      str(raised.exception))

        with self.assertRaises(rig_model.RigModelError) as raised:
            rig_model.writable_field_point(_fixture(), 2, 'write-back',
                                           name='level-primary')
        self.assertIn('collides with the mounted model',
                      str(raised.exception))

        with self.assertRaises(rig_model.RigModelError) as raised:
            rig_model.writable_field_point(_fixture(), 2, 'write-back',
                                           signal_id=10010)
        self.assertIn('collides with the mounted model',
                      str(raised.exception))

        with self.assertRaises(rig_model.RigModelError) as raised:
            rig_model.writable_field_point({'devices': []}, 2, 'write-back')
        self.assertIn('lacks a', str(raised.exception))

    def test_the_pinned_fixtures_are_never_written(self):
        before = FIXTURE.read_bytes(), CYCLIC.read_bytes()
        rig_model.writable_field_point(_fixture(), 2, 'write-back')
        rig_model.two_station_cyclic(_cyclic())
        self.assertEqual((FIXTURE.read_bytes(), CYCLIC.read_bytes()), before)
