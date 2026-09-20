"""The redundant-pair leg's event-parity seam, unit-tested against
faked served resources: pair.event_records collects every
`event_emitted` across all components in served order — outer resource
name as `component`, tick and retention carried, the full inner
EmittedEvent with `fields` the ordered `list(fields.items())` —
excluding local seq and publication markers, and pair.assert_event_parity
returns the collected records when both peers equal the expected list
and raises pair.Abort('event-parity-failed: …') otherwise, with the
named divergences — empty streams against a nonempty expected, a
missing record, a reattributed inner or outer component, changed
identity, value, field order, tick, or retention — each failing and an
unrelated local-seq difference alone never failing."""
import importlib.util
import sys
import unittest
from pathlib import Path

_CI_DIR = Path(__file__).resolve().parents[1] / "reference-plant" / "ci"
sys.path.insert(0, str(_CI_DIR))
_PAIR_PATH = _CI_DIR / "pair.py"
_pair_spec = importlib.util.spec_from_file_location("pair", _PAIR_PATH)
pair = importlib.util.module_from_spec(_pair_spec)
_pair_spec.loader.exec_module(pair)


def emitted(component, event, **fields):
    """One served resource `events` entry — a routed emission record
    with a local stream `seq`, the `retention` class, and the inner
    EmittedEvent naming its producer and payload field map under the
    journal variant's `event` key."""
    return {
        "seq": emitted.next_seq,
        "tick": 10,
        "retention": "journal",
        "event": {
            "event_emitted": {
                "event": {
                    "component": component,
                    "event": event,
                    "fields": fields,
                }
            }
        },
    }


emitted.next_seq = 1


def resources(name, events):
    """One served component resource view — the outer resource `name`
    the component's events attribute under."""
    return {"name": name, "events": events}


def active_view():
    """The active peer's served resources: the sequencer's settled
    step event, then the pump's completed step — two components, two
    retention classes, ordered fields in the inner payload maps."""
    return {
        "components": [
            resources(
                "sequencer",
                [
                    emitted(
                        "sequencer",
                        "step_completed",
                        ticks=4,
                        note="first",
                    ),
                ],
            ),
            resources(
                "pump",
                [
                    emitted(
                        "pump",
                        "stroke_complete",
                        ticks=9,
                        note="second",
                    ),
                ],
            ),
        ]
    }


def expected_records():
    """The extraction's declared record shape for `active_view` —
    component, tick, retention, and the full inner event whose `fields`
    is the ordered item list."""
    return [
        {
            "component": "sequencer",
            "tick": 10,
            "retention": "journal",
            "event": {
                "component": "sequencer",
                "event": "step_completed",
                "fields": [("ticks", 4), ("note", "first")],
            },
        },
        {
            "component": "pump",
            "tick": 10,
            "retention": "journal",
            "event": {
                "component": "pump",
                "event": "stroke_complete",
                "fields": [("ticks", 9), ("note", "second")],
            },
        },
    ]


class EventRecordsTests(unittest.TestCase):
    """The extraction half: served order, record shape, and the local
    seq/publication exclusion."""

    def test_collects_all_in_served_order_with_full_inner_event(self):
        view = active_view()
        view["publication"] = 10
        records = pair.event_records(view)
        self.assertEqual(records, expected_records())
        inner = records[1]["event"]
        self.assertEqual(inner["component"], "pump")
        self.assertEqual(inner["event"], "stroke_complete")
        self.assertEqual(inner["fields"], [("ticks", 9), ("note", "second")])

    def test_empty_resources_collect_no_records(self):
        self.assertEqual(
            pair.event_records(
                {"publication": 3, "components": [resources("pump", [])]}
            ),
            [],
        )
        self.assertEqual(pair.event_records({"components": []}), [])

    def test_local_seq_and_retention_absent_from_inner_event(self):
        view = active_view()
        records = pair.event_records(view)
        self.assertNotIn("seq", records[0])
        self.assertNotIn("seq", records[0]["event"])
        self.assertEqual(records[0]["retention"], "journal")


class ParityTests(unittest.TestCase):
    """The assertion half: records returned when both peers match the
    expected list, pair.Abort carrying the event-parity-failed message
    for each named divergence, and local-seq-only drift ignored."""

    def setUp(self):
        self.expected = expected_records()
        self.active = active_view()
        self.standby = active_view()

    def parity(self):
        return pair.assert_event_parity(
            self.active, self.standby, self.expected
        )

    def test_matching_peers_return_the_expected_records(self):
        self.assertEqual(self.parity(), self.expected)

    def test_empty_streams_against_nonempty_expected_fail(self):
        self.active["components"] = [resources("pump", [])]
        self.standby["components"] = [resources("pump", [])]
        with self.assertRaises(pair.Abort) as raised:
            self.parity()
        self.assertIn("event-parity-failed", str(raised.exception))

    def test_missing_record_fails(self):
        del self.standby["components"][1]["events"][0]
        with self.assertRaises(pair.Abort) as raised:
            self.parity()
        message = str(raised.exception)
        self.assertIn("event-parity-failed", message)
        self.assertIn("pump", message)

    def test_inner_component_reattribution_fails(self):
        self.standby["components"][1]["events"][0]["event"][
            "event_emitted"
        ]["event"]["component"] = "blower"
        with self.assertRaises(pair.Abort):
            self.parity()

    def test_outer_component_reattribution_fails(self):
        self.standby["components"][1]["name"] = "pump-b"
        with self.assertRaises(pair.Abort):
            self.parity()

    def test_changed_field_value_fails(self):
        self.standby["components"][0]["events"][0]["event"][
            "event_emitted"
        ]["event"]["fields"]["note"] = "drifted"
        with self.assertRaises(pair.Abort):
            self.parity()

    def test_changed_field_order_fails(self):
        fields = self.standby["components"][0]["events"][0]["event"][
            "event_emitted"
        ]["event"]["fields"]
        self.standby["components"][0]["events"][0]["event"][
            "event_emitted"
        ]["event"]["fields"] = dict(reversed(list(fields.items())))
        with self.assertRaises(pair.Abort):
            self.parity()

    def test_changed_identity_fails(self):
        inner = self.standby["components"][0]["events"][0]["event"][
            "event_emitted"
        ]["event"]
        inner["event"] = "stroke_complete"
        with self.assertRaises(pair.Abort):
            self.parity()

    def test_changed_tick_fails(self):
        self.standby["components"][0]["events"][0]["tick"] = 11
        with self.assertRaises(pair.Abort):
            self.parity()

    def test_changed_retention_fails(self):
        self.standby["components"][0]["events"][0]["retention"] = "history"
        with self.assertRaises(pair.Abort):
            self.parity()

    def test_unrelated_local_seq_difference_is_ignored(self):
        for view, seq in ((self.active, 5), (self.standby, 9)):
            view["components"][1]["events"][0]["seq"] = seq
        self.assertEqual(self.parity(), self.expected)


if __name__ == "__main__":
    unittest.main()
