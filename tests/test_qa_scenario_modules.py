"""The scenario suite's split layout: SCENARIOS is discovered over
the per-leg scenario modules in filename order (#928), each leg's
unit coverage lives in its own tests/test_qa_scenario_NNNN_<slug>.py
module (#940), and the ordering pin is distributed — every leg
declares the window it needs as RUNS_AFTER/RUNS_BEFORE/RUNS_LAST in
its own scenario module, and the derivation check below validates
those declarations against the discovered run order. A new leg is
exactly one scenario module plus one test module and edits no
shared file: no registry append, no ordering-list append. The
facade keeps propagating patch.object(scenarios, ...) writes into
common and the leg modules so the suite's module-attribute seams
resolve unchanged."""
import importlib
import inspect
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qa_lane import scenarios
from qa_lane.scenarios import common

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

# The test-module naming contract: a leg's tests live in
# test_qa_scenario_<leg stem>.py beside the shared seam module
# qa_scenario_support.py. The two modules below match the glob but
# are not leg modules — this file (the structure checks) and the
# common-seam coverage.
LEG_TEST_PATTERN = re.compile(r'test_qa_scenario_(\d+_[a-z0-9_]+)$')
NON_LEG_MODULES = {'test_qa_scenario_modules', 'test_qa_scenario_common'}


def _leg_scenario_names(module):
    """The scenario_* function names a leg module defines."""
    return sorted(name for name, value in vars(module).items()
                  if name.startswith('scenario_')
                  and callable(value))


def _declared_constraints(module):
    """A leg module's ordering intent: (runs_after, runs_before,
    runs_first, runs_last) — all optional, defaulting unconstrained."""
    return (frozenset(getattr(module, 'RUNS_AFTER', ())),
            frozenset(getattr(module, 'RUNS_BEFORE', ())),
            bool(getattr(module, 'RUNS_FIRST', False)),
            bool(getattr(module, 'RUNS_LAST', False)))


def _check_constraints(modules, order, case):
    """Validate every leg's declared constraints against a run order —
    used for the discovered schedule and for synthetic legs."""
    positions = {name: index for index, name in enumerate(order)}
    for module in modules:
        names = _leg_scenario_names(module)
        after, before, first, last = _declared_constraints(module)
        declared = after | before
        unknown = declared - positions.keys()
        case.assertFalse(
            unknown,
            '%s constrains against undiscovered legs: %s'
            % (module.__name__, sorted(unknown)))
        for name in names:
            for dep in sorted(after):
                case.assertLess(
                    positions[dep], positions[name],
                    '%s must run after %s' % (name, dep))
            for dep in sorted(before):
                case.assertLess(
                    positions[name], positions[dep],
                    '%s must run before %s' % (name, dep))
            if first:
                case.assertEqual(
                    positions[name], 0,
                    '%s declares RUNS_FIRST' % name)
            if last:
                case.assertEqual(
                    positions[name], len(order) - 1,
                    '%s declares RUNS_LAST' % name)


def _iter_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_cases(item)
        else:
            yield item


def _defined_cases(module):
    """The Class.test_* case names on TestCase classes a module
    defines."""
    names = set()
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if (issubclass(cls, unittest.TestCase)
                and cls.__module__ == module.__name__):
            names.update(
                cls.__name__ + '.' + method
                for method in
                unittest.defaultTestLoader.getTestCaseNames(cls))
    return names


class OrderingConstraintTests(unittest.TestCase):
    """The distributed ordering pin: each leg module declares the
    window it needs, and the declarations derive the schedule check
    the pre-split EXPECTED_ORDER list used to pin."""

    def test_each_leg_lives_in_its_own_numbered_module(self):
        stems = scenarios._leg_stems()
        functions = list(scenarios.SCENARIOS)
        self.assertEqual(len(stems), len(functions))
        for fn, stem in zip(functions, stems):
            number, _, slug = stem.partition('_')
            self.assertTrue(number.isdigit())
            self.assertEqual('scenario_' + slug, fn.__name__)
            self.assertIs(getattr(scenarios, fn.__name__), fn)

    def test_declared_constraints_resolve_to_discovered_legs(self):
        names = {fn.__name__ for fn in scenarios.SCENARIOS}
        for module in scenarios._LEG_MODULES:
            after, before, first, last = _declared_constraints(module)
            unknown = (after | before) - names
            self.assertFalse(
                unknown,
                '%s constrains against undiscovered legs: %s'
                % (module.__name__, sorted(unknown)))

    def test_discovered_order_satisfies_every_declared_constraint(self):
        order = [fn.__name__ for fn in scenarios.SCENARIOS]
        _check_constraints(scenarios._LEG_MODULES, order, self)

    def test_new_leg_module_joins_with_no_shared_file_edit(self):
        # A leg filed as one new NNNN_<slug>.py module — carrying its
        # own ordering declarations — is discovered into the schedule
        # position its number names and its constraints derive against
        # the extended order: no edit to __init__.py, common.py, or any
        # sibling leg.
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, '1950_synthetic_leg.py').write_text(
                "RUNS_AFTER = frozenset({'scenario_demote_carry_settle'})\n"
                "RUNS_BEFORE = frozenset({'scenario_peer_announce'})\n"
                'def scenario_synthetic_leg(ctx):\n'
                '    return None\n')
            scenarios.__path__.append(tmp)
            try:
                modules = scenarios._leg_modules()
            finally:
                scenarios.__path__.remove(tmp)
            sys.modules.pop('qa_lane.scenarios.1950_synthetic_leg',
                            None)
        names = [fn.__name__
                 for fn in scenarios._scenario_functions(modules)]
        index = names.index('scenario_demote_carry_settle') + 1
        self.assertEqual(names[index], 'scenario_synthetic_leg')
        _check_constraints(modules, names, self)


class TestModuleDiscoveryTests(unittest.TestCase):
    """The test-side half of the convention: each leg's cases live in
    tests/test_qa_scenario_NNNN_<slug>.py and unittest discovery picks
    them up by filename — no shared registry. EXPECTED_CASES in each
    module pins the case set the split migrated, so coverage parity
    with the pre-split monolith is asserted per leg."""

    def _test_module_stems(self):
        return sorted(path.stem for path in
                      TESTS_DIR.glob('test_qa_scenario_*.py'))

    def test_every_leg_test_module_pairs_with_a_scenario_leg(self):
        leg_stems = set(scenarios._leg_stems())
        for stem in self._test_module_stems():
            if stem in NON_LEG_MODULES:
                continue
            match = LEG_TEST_PATTERN.match(stem)
            self.assertIsNotNone(
                match, stem + ' does not name a leg test module')
            self.assertIn(
                match.group(1), leg_stems,
                stem + ' pairs with no scenario leg module')

    def test_discovery_reproduces_the_pre_split_case_coverage(self):
        declared = set()
        for stem in self._test_module_stems():
            if stem == 'test_qa_scenario_modules':
                continue
            module = importlib.import_module(stem)
            defined = _defined_cases(module)
            self.assertEqual(
                defined, set(module.EXPECTED_CASES),
                stem + ' case set drifted from its EXPECTED_CASES pin')
            declared.update(stem + '.' + case for case in defined)
        suite = unittest.defaultTestLoader.discover(
            str(TESTS_DIR), pattern='test_qa_scenario_*.py',
            top_level_dir=str(TESTS_DIR))
        discovered = {case.id() for case in _iter_cases(suite)}
        own = {case.id() for case in _iter_cases(
            unittest.defaultTestLoader.loadTestsFromModule(
                sys.modules[__name__]))}
        self.assertEqual(discovered, declared | own)

    def test_synthetic_leg_joins_run_and_test_discovery_as_two_files(self):
        # The full convention: one scenario module plus one test
        # module, no shared-file edit on either side.
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'test_qa_scenario_1950_synthetic_leg.py'
                 ).write_text(
                'import unittest\n\n'
                "EXPECTED_CASES = frozenset("
                "{'SyntheticLegTests.test_case'})\n\n\n"
                'class SyntheticLegTests(unittest.TestCase):\n'
                '    def test_case(self):\n'
                '        pass\n')
            suite = unittest.defaultTestLoader.discover(
                tmp, pattern='test_qa_scenario_*.py',
                top_level_dir=tmp)
            ids = {case.id() for case in _iter_cases(suite)}
        self.assertIn(
            'test_qa_scenario_1950_synthetic_leg'
            '.SyntheticLegTests.test_case', ids)


class PatchSeamTests(unittest.TestCase):
    """patch.object(scenarios, name) propagates into common and every
    leg module binding the name — the monolith's single-namespace
    behavior the existing suite's patches rely on."""

    def test_shared_tunable_writes_through_to_common_and_legs(self):
        leg = importlib.import_module(
            'qa_lane.scenarios.0100_controller_active')
        original = common.POLL_INTERVAL
        with patch.object(scenarios, 'POLL_INTERVAL', 0.001):
            self.assertEqual(common.POLL_INTERVAL, 0.001)
            self.assertEqual(leg.POLL_INTERVAL, 0.001)
        self.assertIs(common.POLL_INTERVAL, original)
        self.assertIs(leg.POLL_INTERVAL, original)

    def test_leg_private_helper_writes_through_to_its_module(self):
        leg = importlib.import_module(
            'qa_lane.scenarios.2000_peer_announce')
        sentinel = object()
        with patch.object(scenarios, '_peer_announce_pass', sentinel):
            self.assertIs(leg._peer_announce_pass, sentinel)
        self.assertIsNot(leg._peer_announce_pass, sentinel)


if __name__ == '__main__':
    unittest.main()
