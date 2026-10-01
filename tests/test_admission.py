import tempfile
import unittest
from pathlib import Path

from agent_pool.admission import Admission, classify
from agent_pool.state import State
from agent_pool.config import scheduler


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / 'state.db')
        self.now = 10000
        self.config = {'scheduler': {'groups': {
            'swe': {'models': ['swe-2-high'], 'initial': 2, 'ceiling': 4},
            'muse': {'models': ['opencode/muse'], 'initial': 2, 'ceiling': 4}},
            'quiet_seconds': 60, 'cooldown_seconds': 10, 'max_cooldown_seconds': 80}}
        self.a = Admission(self.state, self.config, clock=lambda: self.now, jitter=lambda: 0)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def start(self, owner, model='swe-2-high', clone=None):
        self.assertTrue(self.a.reserve(owner, model, clone or owner, 5))
        meta = {'invocation': owner + '-run', 'started_at': self.now}
        self.a.attach(owner, meta)
        return meta

    def test_atomic_capacity_across_connections_and_clone_exclusion(self):
        self.start('one')
        other = State(Path(self.tmp.name) / 'state.db')
        try:
            a = Admission(other, self.config, clock=lambda: self.now)
            self.assertFalse(a.reserve('two', 'opencode/muse', 'one', 5))
            self.assertTrue(a.reserve('two', 'swe-2-high', 'two', 5))
            self.assertFalse(self.a.reserve('three', 'swe-2-high', 'three', 5))
        finally:
            other.close()

    def test_quota_is_scoped_and_deduplicated_with_one_probe(self):
        m = self.start('one')
        self.a.finish('one', m, 'rate', retry_after=30)
        self.a.finish('one', m, 'rate', retry_after=30)
        self.assertFalse(self.a.reserve('two', 'swe-2-high', 'two', 5))
        self.start('muse', 'opencode/muse')
        self.now += 29
        self.assertFalse(self.a.reserve('two', 'swe-2-high', 'two', 5))
        self.now += 1
        self.start('probe')
        self.assertFalse(self.a.reserve('another', 'swe-2-high', 'another', 5))
        self.assertFalse(self.state.paused())
        self.assertEqual(self.a.summary()['outcomes'][0]['count'], 1)

    def test_auth_requires_explicit_group_reset(self):
        m = self.start('one')
        self.a.finish('one', m, 'auth')
        self.now += 86400
        self.assertFalse(self.a.reserve('two', 'swe-2-high', 'two', 5))
        self.a.reset('swe')
        self.assertTrue(self.a.reserve('two', 'swe-2-high', 'two', 5))

    def test_manual_pause_prevents_admission_and_persists(self):
        self.state.pause()
        self.assertFalse(self.a.reserve('one', 'swe-2-high', 'one', 5))
        self.assertTrue(self.state.paused())

    def test_useful_loaded_window_required_for_growth(self):
        self.now += 100
        self.start('one')
        self.assertEqual(self.a.summary()['groups']['swe']['target'], 2)
        m = self.start('two')
        self.assertFalse(self.a.reserve('waiting', 'swe-2-high', 'waiting', 5))
        self.a.finish('two', m, 'success')
        self.a.useful(m)
        self.now += 61
        self.start('three')
        self.assertEqual(self.a.summary()['groups']['swe']['target'], 3)

    def test_new_quota_event_resets_growth_window(self):
        m = self.start('one')
        self.a.finish('one', m, 'rate')
        self.now += 11
        m = self.start('probe')
        self.a.finish('probe', m, 'success')
        self.a.useful(m)
        self.start('next')
        self.assertEqual(self.a.summary()['groups']['swe']['target'], 1)

    def test_configured_floor_recovers_capacity_after_quota_probe(self):
        self.config['scheduler']['groups']['swe'].update(initial=4, minimum=3)
        # Start four sessions, then a real quota receipt enters cooldown.
        metas = [self.start(str(i)) for i in range(4)]
        self.a.finish('0', metas[0], 'rate')
        self.assertEqual(self.a.summary()['groups']['swe']['target'], 3)
        for i in range(1, 4):
            self.a.finish(str(i), metas[i], 'success')
        self.assertFalse(self.a.reserve('waiting', 'swe-2-high', 'waiting', 5))
        self.now += 10
        probe = self.start('probe')
        self.assertFalse(self.a.reserve('second-probe', 'swe-2-high', 'second-probe', 5))
        self.a.finish('probe', probe, 'success')
        self.a.useful(probe)
        for i in range(3):
            self.start('recovered-' + str(i))
        self.assertFalse(self.a.reserve('fourth', 'swe-2-high', 'fourth', 5))
        self.now += 60
        self.a.finish('recovered-0', {'invocation': 'recovered-0-run'}, 'success')
        self.start('fourth')
        self.assertEqual(self.a.summary()['groups']['swe']['target'], 4)

    def test_group_minimum_configuration_rejects_invalid_bounds(self):
        self.config['scheduler']['groups'].pop('muse')
        group = self.config['scheduler']['groups']['swe']
        for value in (0, -1, 3, 1.5, True, None):
            with self.subTest(value=value):
                group['minimum'] = value
                with self.assertRaises(ValueError):
                    scheduler(self.config['scheduler'])
        group['minimum'] = 2
        scheduler(self.config['scheduler'])

    def test_parent_group_and_external_headroom(self):
        self.config['scheduler']['groups']['account'] = {
            'models': ['swe-2-high', 'opencode/muse'], 'initial': 2, 'ceiling': 3,
            'external_slots': 1}
        self.start('one')
        self.assertFalse(self.a.reserve('two', 'opencode/muse', 'two', 5))

    def test_failure_classification_does_not_retry_auth_or_timeout(self):
        self.assertEqual(classify({'exit_code': 1}, 'authentication failed')[0], 'auth')
        self.assertEqual(classify({'status': 'timeout', 'exit_code': -15}, 'rate limit')[0], 'timeout')
        self.assertEqual(classify({'exit_code': 1}, 'Rate limit exceeded. Retry-After: 300'), ('rate', 300))
        self.assertEqual(classify({'exit_code': 0}, 'test verifies rate limit handling')[0], 'success')

    def test_provider_reset_message_gates_until_the_stated_window(self):
        message = 'Reached free model rate limit. Your limit will reset in 1 hour 34 minutes.'
        meta = self.start('limited')
        category, delay = classify({'exit_code': 1}, message)
        self.a.finish('limited', meta, category, retry_after=delay)
        self.now += 34 * 60
        self.assertFalse(self.a.reserve('early', 'swe-2-high', 'early', 5))
        self.now += 60 * 60
        self.start('probe')

    def test_provider_reset_duration_formats_and_explicit_header_precedence(self):
        cases = [('39 minutes', 2340), ('1 hour 34 minutes', 5640),
                 ('2 hours', 7200), ('17 seconds', 17),
                 ('1 hour 2 minutes 3 seconds', 3723), ('0 seconds', 1)]
        for duration, seconds in cases:
            message = 'Reached free model rate limit. Your limit will reset in ' + duration + '.'
            with self.subTest(duration=duration):
                self.assertEqual(classify({'exit_code': 1}, message), ('rate', seconds))
        message = 'Reached free model rate limit. Your limit will reset in 39 minutes. Retry-After: 60'
        self.assertEqual(classify({'exit_code': 1}, message), ('rate', 60))
        self.assertEqual(classify({'exit_code': 0}, message), ('success', None))
        self.assertEqual(classify({'exit_code': 1}, 'Rate limit. Your limit will reset in tomorrow.'), ('rate', None))
        stream = 'stream error: Reached free model rate limit. Your limit will reset in 17 seconds.'
        self.assertEqual(classify({'status': 'timeout'}, stream), ('rate', 17))

    def test_timeout_with_provider_stream_error_scopes_cooldown(self):
        tail = 'level=ERROR message="stream error" error.error="AI_APICallError: Rate limit exceeded"'
        self.assertEqual(classify({'status': 'timeout', 'exit_code': -15}, tail)[0], 'rate')
        tail = 'level=ERROR message="stream error" error.error="unexpected server error"'
        self.assertEqual(classify({'status': 'timeout', 'exit_code': -15}, tail)[0], 'endpoint')
        self.assertEqual(classify({'status': 'timeout', 'exit_code': -15}, 'rate limit')[0], 'timeout')

    def _inflight_probe(self, advance=11):
        """A probe granted under an expired cooldown, one stale lease still running.

        Four leases fill the group; the rate receipt opens the congestion
        episode and halves the target, the other two drain, and the probe
        takes the group's single recovery slot after the cooldown elapsed.
        """
        self.config['scheduler']['groups']['swe'].update(initial=4, ceiling=8)
        leases = [self.start('L' + str(i)) for i in range(4)]
        self.a.finish('L0', leases[0], 'rate')
        self.assertEqual(self.a.summary()['groups']['swe']['mode'], 'probing')
        self.a.finish('L2', leases[2], 'success')
        self.a.finish('L3', leases[3], 'success')
        self.now += advance
        probe = self.start('probe')
        granted = self.state.db.execute('SELECT probe,granted FROM admission_leases WHERE owner=?',
                                        ('probe',)).fetchone()
        self.assertEqual(granted['probe'], 1)
        self.assertEqual(granted['granted'], self.now)
        return leases[1], probe

    def test_probe_success_keeps_a_block_recorded_after_its_grant(self):
        stale, probe = self._inflight_probe()
        self.a.finish('L1', stale, 'auth')
        self.assertEqual(self.a.summary()['groups']['swe']['mode'], 'blocked')
        self.a.finish('probe', probe, 'success')
        groups = self.a.summary()['groups']['swe']
        self.assertEqual(groups['mode'], 'blocked')
        self.assertIsNone(groups['cooldown_until'])
        # Sleeping never fixes credentials, so the probe's success is no
        # substitute for the operator reset the group is documented to need.
        self.now += 86400
        self.assertFalse(self.a.reserve('next', 'swe-2-high', 'next', 5))
        self.a.reset('swe')
        self.assertTrue(self.a.reserve('next', 'swe-2-high', 'next', 5))

    def test_probe_success_keeps_a_reset_window_recorded_after_its_grant(self):
        stale, probe = self._inflight_probe()
        self.a.finish('L1', stale, 'rate', retry_after=3600)
        window = self.a.summary()['groups']['swe']['cooldown_until']
        self.assertEqual(window, self.now + 3600)
        self.a.finish('probe', probe, 'success')
        groups = self.a.summary()['groups']['swe']
        self.assertEqual(groups['mode'], 'probing')
        self.assertEqual(groups['cooldown_until'], window)
        # The provider-stated window still gates the whole group, and past it
        # recovery is a single fresh probe rather than normal dispatch.
        self.now += 3599
        self.assertFalse(self.a.reserve('next', 'swe-2-high', 'next', 5))
        self.now += 1
        self.assertTrue(self.a.reserve('next', 'swe-2-high', 'next', 5))
        self.assertFalse(self.a.reserve('second', 'swe-2-high', 'second', 5))
        self.assertEqual(self.a.summary()['groups']['swe']['mode'], 'probing')

    def test_probe_success_keeps_a_reset_recorded_in_its_grant_tick(self):
        stale, probe = self._inflight_probe(advance=10)
        self.a.finish('L1', stale, 'rate', retry_after=3600)
        window = self.a.summary()['groups']['swe']['cooldown_until']
        self.a.finish('probe', probe, 'success')
        groups = self.a.summary()['groups']['swe']
        self.assertEqual(groups['mode'], 'probing')
        self.assertEqual(groups['cooldown_until'], window)

    def test_probe_success_clears_the_episode_it_was_granted_for(self):
        _stale, probe = self._inflight_probe()
        self.a.finish('probe', probe, 'success')
        groups = self.a.summary()['groups']['swe']
        self.assertEqual(groups['mode'], 'normal')
        self.assertIsNone(groups['cooldown_until'])
        self.assertTrue(self.a.reserve('next', 'swe-2-high', 'next', 5))


if __name__ == '__main__':
    unittest.main()
