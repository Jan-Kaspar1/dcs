"""The scenarios-package split (#928): SCENARIOS is discovered over the
per-leg modules' scenario_* functions in filename order, the migrated
schedule must reproduce the pre-split registry order exactly, a new
leg filed as a single NNNN_<slug>.py module joins the run order with
no shared-file edit, and the facade keeps propagating
patch.object(scenarios, ...) writes into common and the leg modules
so the existing suite's module-attribute seams resolve unchanged."""
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qa_lane import scenarios
from qa_lane.scenarios import common

# The pre-split registry order — the load-bearing schedule the split
# must reproduce: later cases degrade to inconclusive when rig state
# an earlier case was to establish never landed, and the dcs-ctl case
# deliberately closes the schedule.
EXPECTED_ORDER = """
scenario_controller_active scenario_standby_tracking
scenario_operator_command scenario_controller_restart
scenario_source_restart scenario_stale_freshness
scenario_field_claim scenario_fenced_writer_degrade
scenario_dead_peer_latency scenario_monitor_starvation
scenario_duty_rotation scenario_pump_out_of_service
scenario_force_carryover scenario_backup_health
scenario_source_failover scenario_lag_staging scenario_standby_loss
scenario_demote_settle_uniqueness scenario_demote_carry_settle
scenario_peer_announce scenario_parameter_tune_carryover
scenario_failover scenario_checkpoint_negotiation
scenario_doomed_startup_claim scenario_incompatible_revision
scenario_model_revision scenario_evidence_capture
scenario_served_interface scenario_event_retention
scenario_alarm_rationalization scenario_force_release
scenario_consumer_schedule scenario_command_admission
scenario_command_availability scenario_plant_link_loss
scenario_field_fault scenario_unclaimed_rearm
scenario_unavailable_fallback scenario_power_fail_trip
scenario_dcs_ctl""".split()


class DiscoveryOrderTests(unittest.TestCase):
    """The discovered registry pins the migrated run order."""

    def test_registry_order_matches_the_pre_split_schedule(self):
        self.assertEqual([fn.__name__ for fn in scenarios.SCENARIOS],
                         EXPECTED_ORDER)

    def test_each_leg_lives_in_its_own_numbered_module(self):
        stems = scenarios._leg_stems()
        self.assertEqual(len(stems), len(EXPECTED_ORDER))
        for fn, stem in zip(scenarios.SCENARIOS, stems):
            number, _, slug = stem.partition('_')
            self.assertTrue(number.isdigit())
            self.assertEqual('scenario_' + slug, fn.__name__)
            self.assertIs(getattr(scenarios, fn.__name__), fn)

    def test_new_leg_module_joins_with_no_shared_file_edit(self):
        # A leg filed as one new NNNN_<slug>.py module is discovered
        # into the schedule position its number names — no edit to
        # __init__.py, common.py, or any sibling leg.
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, '1950_synthetic_leg.py').write_text(
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
        self.assertEqual(names[:19], EXPECTED_ORDER[:19])
        self.assertEqual(names[19], 'scenario_synthetic_leg')
        self.assertEqual(names[20:], EXPECTED_ORDER[19:])


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
