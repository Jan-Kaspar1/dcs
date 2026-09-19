"""The checked-in rolling-revision recipe's unit coverage: derivation
is deterministic, additive, collision-guarded, and produces a document
that validates and lints clean — including against the run's real
mounted fixture, whose maximum declared point id the added point must
stay below so assembly's synthesized-link allocation does not shift —
and the checked-in incompatible post-derivation step's coverage: it
retypes exactly the spec's named carried point, rewires its connection
ends onto the named same-kind point so the document still validates,
and refuses every spec violation with a named RevisionError."""
import json
import tempfile
import unittest
from pathlib import Path

from qa_lane import revision

FIXTURE = (Path(__file__).resolve().parent.parent / 'crates'
           / 'dcs-demo' / 'fixtures' / 'pump_station.json')


def _document():
    """A minimal structurally-plausible mounted model."""
    return {'version': 1,
            'devices': [{'id': 1, 'kind': 'sim-di',
                         'channels': [{'name': 'ch0',
                                       'direction': 'in',
                                       'value_type': 'bool'}]}],
            'io_points': [
                {'id': 10, 'direction': 'in', 'value_type': 'bool',
                 'channel': {'device': 1, 'channel': 'ch0'}},
                {'id': 300, 'direction': 'in', 'value_type': 'bool',
                 'writable': True, 'journaled': True,
                 'initial': {'bool': False}}],
            'signals': [{'id': 10300, 'name': 'oos', 'source': 300,
                         'unit': '', 'group': 'pumps'}],
            'components': [], 'connections': []}


def _recipe(**overrides):
    recipe = {'add_io_points': [
        {'id': 900, 'direction': 'in', 'value_type': 'bool',
         'writable': True, 'initial': {'bool': False}}],
        'add_signals': [{'id': 10900, 'name': 'marker', 'source': 900}]}
    recipe.update(overrides)
    return recipe


class RecipeDerivationTests(unittest.TestCase):
    def test_checked_in_recipe_loads(self):
        recipe = revision.load_recipe()
        self.assertTrue(recipe.get('add_io_points'))
        self.assertTrue(recipe.get('add_signals'))

    def test_derivation_is_deterministic_and_additive(self):
        document = _document()
        recipe = revision.load_recipe()
        first = revision.derive(document, recipe)
        second = revision.derive(document, recipe)
        self.assertEqual(first, second)
        # The input document is untouched and everything it carried
        # survives verbatim — the recipe can only append.
        self.assertEqual(document['io_points'], first['io_points'][:-1])
        self.assertEqual(document['signals'], first['signals'][:-1])
        self.assertEqual(document['devices'], first['devices'])
        self.assertEqual(document['connections'],
                         first['connections'])
        added = first['io_points'][-1]
        self.assertEqual(added['id'], 900)
        self.assertNotIn('channel', added)
        signal = first['signals'][-1]
        self.assertEqual(signal['source'], 900)

    def test_derived_document_lints_clean(self):
        revised = revision.derive(_document(), revision.load_recipe())
        self.assertEqual(revision.lint(revised), [])

    def test_derivation_changes_the_document_semantically(self):
        # The model fingerprint hashes canonical content: a document
        # that differs in an added point and signal fingerprints
        # differently — that is what makes the peer's crossing
        # reinitialize rather than track.
        document = _document()
        revised = revision.derive(document, revision.load_recipe())
        self.assertNotEqual(revised, document)

    def test_fixture_derivation_lints_clean_below_max_id(self):
        # The run's real mounted model: the added point id sits below
        # the fixture's maximum declared id so synthesized link ids
        # survive the boundary unrenamed.
        document = json.loads(FIXTURE.read_text())
        revised = revision.derive(document, revision.load_recipe())
        self.assertEqual(revision.lint(revised), [])
        maximum = max(p['id'] for p in document['io_points'])
        for point in revision.load_recipe()['add_io_points']:
            self.assertLess(point['id'], maximum)
        self.assertEqual(len(revised['io_points']),
                         len(document['io_points']) + 1)

    def test_write_is_deterministic_and_leaves_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / 'plant.json'
            model.write_text(json.dumps(_document()))
            before = model.read_text()
            first = Path(tmp) / 'a.json'
            second = Path(tmp) / 'b.json'
            info = revision.derive_revised_model(model, first)
            revision.derive_revised_model(model, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(model.read_text(), before)
            self.assertEqual(info['added_points'], [900])
            self.assertEqual(info['added_signals'], [10900])

    def test_colliding_point_id_rejected(self):
        recipe = _recipe(add_io_points=[
            {'id': 300, 'direction': 'in', 'value_type': 'bool',
             'initial': {'bool': False}}])
        with self.assertRaises(revision.RevisionError):
            revision.derive(_document(), recipe)

    def test_channel_binding_rejected(self):
        recipe = _recipe(add_io_points=[
            {'id': 900, 'direction': 'in', 'value_type': 'bool',
             'channel': {'device': 1, 'channel': 'ch0'}}])
        with self.assertRaises(revision.RevisionError):
            revision.derive(_document(), recipe)

    def test_missing_initial_rejected(self):
        recipe = _recipe(add_io_points=[
            {'id': 900, 'direction': 'in', 'value_type': 'bool'}])
        with self.assertRaises(revision.RevisionError):
            revision.derive(_document(), recipe)

    def test_wrong_initial_kind_rejected(self):
        recipe = _recipe(add_io_points=[
            {'id': 900, 'direction': 'in', 'value_type': 'bool',
             'initial': {'int': 3}}])
        with self.assertRaises(revision.RevisionError):
            revision.derive(_document(), recipe)

    def test_writable_out_rejected(self):
        recipe = _recipe(add_io_points=[
            {'id': 900, 'direction': 'out', 'value_type': 'bool',
             'writable': True, 'initial': {'bool': False}}])
        with self.assertRaises(revision.RevisionError):
            revision.derive(_document(), recipe)

    def test_journaled_float_rejected(self):
        recipe = _recipe(add_io_points=[
            {'id': 900, 'direction': 'in', 'value_type': 'float',
             'journaled': True, 'initial': {'float': 0.0}}])
        with self.assertRaises(revision.RevisionError):
            revision.derive(_document(), recipe)

    def test_colliding_signal_id_rejected(self):
        recipe = _recipe(add_signals=[
            {'id': 10300, 'name': 'other', 'source': 900}])
        with self.assertRaises(revision.RevisionError):
            revision.derive(_document(), recipe)

    def test_colliding_signal_name_rejected(self):
        recipe = _recipe(add_signals=[
            {'id': 10900, 'name': 'oos', 'source': 900}])
        with self.assertRaises(revision.RevisionError):
            revision.derive(_document(), recipe)

    def test_dangling_signal_source_rejected(self):
        recipe = _recipe(add_signals=[
            {'id': 10900, 'name': 'marker', 'source': 42}])
        with self.assertRaises(revision.RevisionError):
            revision.derive(_document(), recipe)

    def test_unreadable_inputs_raise_named_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / 'nope.json'
            with self.assertRaises(revision.RevisionError):
                revision.derive_revised_model(missing,
                                              Path(tmp) / 'out.json')
            with self.assertRaises(revision.RevisionError):
                revision.load_recipe(missing)


class IncompatibleDerivationTests(unittest.TestCase):
    """The checked-in incompatible post-derivation step: it must
    produce a document that still validates and loads — the carryover
    crossing, not the model load, is what refuses — while retyping
    exactly the spec's named carried point."""

    def _spec(self, **overrides):
        spec = {'retype_io_point': {'id': 300, 'value_type': 'int',
                                    'initial': {'int': 0},
                                    'rewire_to': 301}}
        spec.update(overrides)
        return spec

    def _wired_document(self):
        """A mounted model whose writable internal point 300 feeds a
        component port — the carried-point shape the checked-in spec
        retypes — with 301 as a same-kind rewire target."""
        document = _document()
        document['io_points'].append(
            {'id': 301, 'direction': 'in', 'value_type': 'bool',
             'writable': True, 'initial': {'bool': False}})
        document['components'] = [
            {'id': 20, 'kind': 'bool-gate',
             'ports': [{'name': 'in', 'direction': 'in',
                        'value_type': 'bool'}]}]
        document['connections'] = [
            {'from': {'point': 300},
             'to': {'port': {'component': 20, 'name': 'in'}}}]
        return document

    def test_checked_in_spec_loads_and_names_the_retype(self):
        spec = revision.load_incompatible()
        retype = spec['retype_io_point']
        self.assertEqual(retype['id'], 302)
        self.assertEqual(retype['value_type'], 'int')
        self.assertEqual(retype['initial'], {'int': 0})
        self.assertEqual(retype['rewire_to'], 900)
        # The refusal is named in the spec's own description.
        self.assertIn('302', spec['description'])
        self.assertIn('retype', spec['description'].lower())

    def test_apply_retypes_and_rewires(self):
        document = self._wired_document()
        applied = revision.apply_incompatible(document, self._spec())
        incompatible = applied['document']
        points = {p['id']: p for p in incompatible['io_points']}
        self.assertEqual(points[300]['value_type'], 'int')
        self.assertEqual(points[300]['initial'], {'int': 0})
        # The retyped point stays a writable internal in-point — the
        # carried set — and its wiring moved onto the same-kind 301.
        self.assertTrue(points[300]['writable'])
        self.assertNotIn('channel', points[300])
        self.assertEqual(applied['retyped_point'], 300)
        self.assertEqual(applied['rewired_connections'], 1)
        connection = incompatible['connections'][0]
        self.assertEqual(connection['from']['point'], 301)
        # The input document is untouched.
        self.assertEqual(document['io_points'][1]['value_type'],
                         'bool')
        self.assertEqual(document['connections'][0]['from']['point'],
                         300)

    def test_fixture_derivation_lints_clean_and_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / 'a.json'
            second = Path(tmp) / 'b.json'
            info = revision.derive_incompatible_model(FIXTURE, first)
            revision.derive_incompatible_model(FIXTURE, second)
            self.assertEqual(first.read_bytes(),
                             second.read_bytes())
            document = json.loads(first.read_text())
            self.assertEqual(revision.lint(document), [])
            points = {p['id']: p for p in document['io_points']}
            self.assertEqual(points[302]['value_type'], 'int')
            self.assertTrue(points[302]['writable'])
            self.assertEqual(points[900]['value_type'], 'bool')
            # No connection end still names the retyped point.
            for connection in document['connections']:
                for end in ('from', 'to'):
                    endpoint = connection.get(end) or {}
                    self.assertNotEqual(endpoint.get('point'), 302)
            self.assertEqual(info['retyped_point'], 302)
            self.assertEqual(info['added_points'], [900])
            self.assertGreater(info['rewired_connections'], 0)

    def test_unknown_retype_point_rejected(self):
        spec = self._spec()
        spec['retype_io_point']['id'] = 4242
        with self.assertRaises(revision.RevisionError):
            revision.apply_incompatible(self._wired_document(), spec)

    def test_channel_bound_retype_rejected(self):
        # A field point is not the carried set — the checkpoint's
        # internal section never crosses it.
        spec = self._spec()
        spec['retype_io_point']['id'] = 10
        spec['retype_io_point']['rewire_to'] = 301
        with self.assertRaises(revision.RevisionError):
            revision.apply_incompatible(self._wired_document(), spec)

    def test_same_kind_retype_rejected(self):
        spec = self._spec()
        spec['retype_io_point']['value_type'] = 'bool'
        spec['retype_io_point']['initial'] = {'bool': False}
        with self.assertRaises(revision.RevisionError):
            revision.apply_incompatible(self._wired_document(), spec)

    def test_mismatched_initial_rejected(self):
        spec = self._spec()
        spec['retype_io_point']['initial'] = {'bool': True}
        with self.assertRaises(revision.RevisionError):
            revision.apply_incompatible(self._wired_document(), spec)

    def test_unknown_rewire_target_rejected(self):
        spec = self._spec()
        spec['retype_io_point']['rewire_to'] = 4242
        with self.assertRaises(revision.RevisionError):
            revision.apply_incompatible(self._wired_document(), spec)

    def test_wrong_kind_rewire_target_rejected(self):
        document = self._wired_document()
        document['io_points'][2]['value_type'] = 'float'
        document['io_points'][2]['initial'] = {'float': 0.0}
        with self.assertRaises(revision.RevisionError):
            revision.apply_incompatible(document, self._spec())

    def test_malformed_spec_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / 'bad.json'
            bad.write_text(json.dumps({'retype_io_point':
                                       {'id': 300}}))
            with self.assertRaises(revision.RevisionError):
                revision.load_incompatible(bad)
            bad.write_text(json.dumps({'unknown': 1}))
            with self.assertRaises(revision.RevisionError):
                revision.load_incompatible(bad)
            with self.assertRaises(revision.RevisionError):
                revision.load_incompatible(Path(tmp) / 'nope.json')


class LintTests(unittest.TestCase):
    def test_lint_names_duplicates_and_dangling_sources(self):
        document = _document()
        document['io_points'].append(dict(document['io_points'][-1]))
        document['signals'].append({'id': 1, 'name': 'x',
                                    'source': 999})
        problems = revision.lint(document)
        self.assertTrue(any('duplicate io_points id' in p
                            for p in problems))
        self.assertTrue(any('unknown io point 999' in p
                            for p in problems))

    def test_lint_names_missing_and_mismatched_initials(self):
        document = _document()
        document['io_points'].append(
            {'id': 900, 'direction': 'in', 'value_type': 'bool'})
        document['io_points'].append(
            {'id': 901, 'direction': 'in', 'value_type': 'bool',
             'initial': {'int': 1}})
        problems = revision.lint(document)
        self.assertTrue(any('no initial value' in p for p in problems))
        self.assertTrue(any('mismatches' in p for p in problems))


if __name__ == '__main__':
    unittest.main()
