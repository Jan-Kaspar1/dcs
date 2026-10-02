"""The 2497_self_standby_refusal leg's scenario unit coverage — the
feed fakes and TestCase classes for
scenario_self_standby_refusal, split out per the leg-module
convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py and the field's claim half reuses the
claim-rendezvous leg's plant peer, the same read-only `probe_writer`
induction the claim legs stage; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam
from test_qa_scenario_2380_claim_monitor_rendezvous import \
    RendezvousPlantPeer


EXPECTED_CASES = frozenset({
    'SelfStandbyTests.test_registered_after_released_before_revisions',
    'SelfStandbyTests.test_fixed_shape_passes_validates_and_sweeps',
    'SelfStandbyTests.test_two_runs_produce_identical_evidence',
    'SelfStandbyTests.test_self_seat_legitimate_fails',
    'SelfStandbyTests.test_zero_exit_fails',
    'SelfStandbyTests.test_unnamed_refusal_fails',
    'SelfStandbyTests.test_refusal_without_own_socket_fails',
    'SelfStandbyTests.test_refusal_without_resolution_fails',
    'SelfStandbyTests.test_refusal_without_staged_target_fails',
    'SelfStandbyTests.test_silent_refusal_fails',
    'SelfStandbyTests.test_refused_seat_served_fails',
    'SelfStandbyTests.test_refused_seat_journaled_fails',
    'SelfStandbyTests.test_refused_seat_holds_field_fails',
    'SelfStandbyTests.test_unclaimed_field_fails',
    'SelfStandbyTests.test_foreign_claim_owner_fails',
    'SelfStandbyTests.test_control_exit_fails',
    'SelfStandbyTests.test_control_over_refusal_fails',
    'SelfStandbyTests.test_control_never_tracks_fails',
    'SelfStandbyTests.test_control_claim_unobserved_fails',
    'SelfStandbyTests.test_control_unjournaled_fails',
    'SelfStandbyTests.test_control_staged_on_own_target_fails',
    'SelfStandbyTests.test_incumbent_demotion_fails',
    'SelfStandbyTests.test_incumbent_stall_fails',
    'SelfStandbyTests.test_member_disturbance_fails',
    'SelfStandbyTests.test_unstaged_self_address_is_inconclusive',
    'SelfStandbyTests.test_unsettled_pair_is_inconclusive',
    'SelfStandbyTests.test_no_incumbent_is_inconclusive',
    'SelfStandbyTests.test_launch_failure_is_nondeterministic',
    'SelfStandbyTests.test_vanished_container_is_nondeterministic',
    'SelfStandbyTests.test_lost_claim_probe_is_nondeterministic',
    'SelfStandbyTests.test_unremovable_seat_is_nondeterministic',
    'SelfStandbyTests.test_second_pass_defect_fails',
    'SelfStandbyTests.test_missing_seams_are_inconclusive',
    'SelfStandbyTests.test_missing_journals_are_inconclusive',
    'SelfStandbyTests.test_unchecked_self_check_fails',
    'SelfStandbyTests.test_judge_self_check_is_complete',
})


class SelfStandbyFeed:
    """A stubbed rig for the self-standby refusal leg. The deployed
    pair is settled on its launch layout: 'active' owns the field's
    claim, 'standby' tracks it. `start_born_controller` stands the two
    labeled seats the leg launches, resolving a tracking target the
    way the runner's own target resolution does — a seat key to that
    seat's rig-bridge monitor address, a pair member key to that
    member's — so the self-addressed staging really names the seat's
    own announced address:

    - the self-addressed seat (`--standby` naming its own seat) is
      refused at boot: a nonzero exit whose stderr carries the named
      verdict, no monitor ever served, no durable run record, and the
      field's claim left where it stood under the incumbent's own
      token. Nothing else in the launch spells "me" — the target is a
      different string from the wildcard `--listen` bind and resolves
      to the seat's bridge address, so only the product's resolution
      can refuse it;
    - the legitimate seat (`--standby` naming the incumbent) runs, and
      converges tracking on it with the incumbent's claim observed,
      journaling its own run boundary.

    A `--standby` naming the seat's own address that is *not* refused
    is the pre-contract shape: the run lives as the pair's standby
    seat, serving a monitor and journaling, indistinguishable on the
    monitor from a legitimate one. Every transition is staged by the
    leg's own lever calls — never wall-clock — so two passes emit
    identical evidence. Fault flags stage each named failure and the
    instability shapes."""
    SEATS = ('driven', 'foreign')
    HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby',
             'ctrl-d:5': 'driven', 'ctrl-f:4': 'foreign'}
    TOKENS = {'active': 424243, 'standby': 424244, 'revised': 424245,
              'foreign': 424246, 'driven': 424247}
    CONTAINERS = {'driven': 'd', 'foreign': 'foreign'}
    BORN_PORT = 8082
    PAIR = {'active': ('a', 8080), 'standby': ('b', 8081)}
    REMOTE = 'dcs-hw-qa-1-plant:9001'

    def __init__(self, root):
        self.root = Path(root)
        self.journals = {
            seat: self.root / 'controllers' / seat / 'journal.jsonl'
            for seat in self.SEATS}
        self.members = {
            'active': {'role': 'active', 'tick': 0, 'field_claim': 'held'},
            'standby': {'role': 'standby', 'tick': 0,
                        'field_claim': 'held'}}
        self.seats = {}
        self.calls = []
        self.controls = 0
        self.seq = {}
        # Fault injection for the named-failure and pre-contract cases.
        self.launch_fails = False       # the born-controller lever raises
        self.own_target_mismatch = False  # the rig staged another target
        self.self_legitimate = False    # pre-contract: the run lives
        self.self_serves = False        # the refused run bound a monitor
        self.self_journaled = False     # the refused run left a run record
        self.self_vanished = False      # the seat's container vanished
        self.self_exit_zero = False
        self.self_unnamed = False
        self.self_no_resolution = False
        self.self_no_socket = False
        self.self_no_target = False
        self.self_silent = False
        self.control_exits = False
        self.control_over_refused = False
        self.control_never_tracks = False
        self.control_claim_unobserved = False
        self.control_unjournaled = False
        self.control_staged_own = False
        self.incumbent_demoted = False  # the staging disturbs the pair
        self.incumbent_stalls = False
        self.member_disturbed = False
        self.no_active = False          # the pair never settled
        self.unsettled = False          # the incumbent holds no claim
        self.sweep_refuses = False      # a teardown call is refused
        self.second_pass_defect = False

    # --- the runner's levers, faked ---------------------------------

    def _address(self, value):
        """The rig-bridge monitor address a tracking target resolves
        to — the runner's own resolution, so the self-addressed
        staging really names the seat's own announced address."""
        if value in self.CONTAINERS:
            return ('dcs-hw-qa-1-' + self.CONTAINERS[value] + ':'
                    + str(self.BORN_PORT))
        suffix, port = self.PAIR[value]
        return 'dcs-hw-qa-1-' + suffix + ':' + str(port)

    def start_controller(self, seat, remote, peer=None, standby=None):
        self.calls.append(('start_born_controller', seat, peer, standby))
        if self.launch_fails:
            raise RuntimeError('docker run failed: name in use')
        # A cold launch: the runner-owned persistence trio is dropped
        # before the process starts, so a surviving file is the run's
        # own.
        self.journals[seat].parent.mkdir(parents=True, exist_ok=True)
        self.journals[seat].unlink(missing_ok=True)
        self.seq[seat] = 0
        target = None
        if standby is not None:
            target = (self._address('driven')
                      if self.control_staged_own and seat != 'driven'
                      else self._address(standby))
        state = {'launched': True, 'running': True, 'exit': None,
                 'logs': '', 'own': standby == seat, 'tick': 0,
                 'served_left': 0, 'boundary': False}
        self.seats[seat] = state
        launch = {'container': 'dcs-hw-qa-1-' + self.CONTAINERS[seat],
                  'seat': seat, 'remote': remote, 'peer': None,
                  'standby': target,
                  'address': self._address('foreign')
                  if self.own_target_mismatch else self._address(seat)}
        if state['own'] and not self.self_legitimate:
            # The boot refusal: the usage error lands before the run
            # exists, so nothing durable, no monitor, and a nonzero
            # exit carrying the named verdict.
            state['running'] = False
            state['exit'] = 0 if self.self_exit_zero else 2
            state['logs'] = self._refusal_logs(target)
            if self.self_serves:
                state['served_left'] = 1
            if self.self_journaled:
                self._boundary(seat)
                self._journal(seat, {'role_changed': {
                    'from': None, 'to': 'standby'}})
            return launch
        if not state['own'] and self.control_over_refused:
            # The gate over-refusing: a tracking source naming a
            # different instance refused with the self-standby verdict.
            state['running'] = False
            state['exit'] = 2
            state['logs'] = self._refusal_logs(target)
            return launch
        # A run that exists: its own lifetime boundary, then the
        # posture the seat serves.
        if not (not state['own'] and self.control_unjournaled):
            self._boundary(seat)
        if not state['own']:
            # The legitimate standby: tracking the incumbent while the
            # pair's own roles are disturbed underneath it.
            self.controls += 1
            if self.incumbent_demoted:
                self.members['active'].update({'role': 'standby',
                                               'field_claim': None})
            if self.member_disturbed:
                self.members['standby'].update({'role': 'active'})
            if self.control_exits:
                state['running'] = False
                state['exit'] = 1
                state['logs'] = 'error: the tracking source refused'
            return launch
        if self.self_legitimate:
            # The pre-contract shape: the run lives as the pair's
            # standby seat, journaling its own lifetime.
            self._journal(seat, {'role_changed': {'from': None,
                                                  'to': 'standby'}})
        return launch

    def stop_controller(self, seat):
        self.calls.append(('stop_born_controller', seat))
        if self.sweep_refuses:
            raise RuntimeError('docker rm -f failed')
        self.seats.pop(seat, None)

    def state(self, seat):
        self.calls.append(('born_controller_state', seat))
        seat_state = self.seats.get(seat)
        if seat_state is None or not seat_state['launched'] \
                or (self.self_vanished and seat_state['own']):
            return {'container': 'dcs-hw-qa-1-' + self.CONTAINERS[seat],
                    'running': False, 'exit': None, 'logs': '',
                    'absent': True}
        return {'container': 'dcs-hw-qa-1-' + self.CONTAINERS[seat],
                'running': seat_state['running'],
                'exit': seat_state['exit'],
                'logs': seat_state['logs'], 'absent': False}

    def _refusal_logs(self, target):
        """The boot refusal's own stderr — the flag it refused, the
        resolution that made the target this run's own socket, that
        socket, the reason the tracking source is a different
        instance, and the staged address. Each doctor strips exactly
        one of those, the shape a usage boilerplate echo would
        satisfy."""
        if self.self_silent:
            return ''
        named = ('dcs-hw-qa-1-d:9999' if self.self_no_target
                 else str(target))
        if self.self_unnamed:
            return ('error: a tracking source is required; the tracking '
                    'source must be a different instance')
        if self.self_no_resolution:
            return ('error: --standby ' + named + " is this run's own "
                    '--listen socket 0.0.0.0:' + str(self.BORN_PORT)
                    + ': the tracking source must be a different '
                      'instance')
        if self.self_no_socket:
            return ('error: --standby ' + named + ' resolves to this '
                    "instance's tracking source; the tracking source "
                    'must be a different instance')
        return ('error: --standby ' + named + ' resolves to this '
                "instance's own --listen socket 0.0.0.0:"
                + str(self.BORN_PORT) + ': the tracking source must be '
                'a different instance — a run pulling its own '
                'checkpoints tracks no peer, and an armed standby '
                'self-promotes on the heartbeat misses its own '
                'non-owning documents score')

    # --- the monitor channel — replaces scenarios.http_json ---------

    def _advance(self):
        """One scan per served request — the request boundary is the
        tick boundary, so served ticks are call-count deterministic."""
        for name, member in self.members.items():
            if name == 'active' and self.incumbent_stalls:
                continue
            member['tick'] += 1
        for state in self.seats.values():
            if state['launched'] and state['running']:
                state['tick'] += 1

    def _seat_sync(self, seat, state):
        if state['own']:
            # The pre-contract shape: every pull returned the run's
            # own non-owning documents, so every apply scored the
            # heartbeat miss a missing peer reports.
            return 'unsynchronized'
        if self.control_never_tracks or (self.second_pass_defect
                                         and self.controls >= 2):
            return 'unsynchronized'
        return {'tracking': {'aligned': self.members['active']['tick']}}

    def _seat_role(self, seat, state):
        if not state['running']:
            if state['served_left'] > 0:
                state['served_left'] -= 1
            else:
                raise urllib.error.URLError('connection refused')
        report = {'tick': state['tick'], 'role': 'standby',
                  'sync': self._seat_sync(seat, state)}
        if not state['own'] and not self.control_claim_unobserved:
            report['field_claim'] = 'held'
        return report

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        name = self.HOSTS.get(host)
        if name is None:
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if (method, route) != ('GET', '/role'):
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        self._advance()
        state = self.seats.get(name)
        if state is not None:
            return 200, self._seat_role(name, state)
        if name not in self.members:
            raise urllib.error.URLError('connection refused')
        member = self.members[name]
        if self.no_active and name == 'active':
            return 200, {'tick': member['tick'], 'role': 'standby',
                         'sync': 'unsynchronized',
                         'field_claim': member['field_claim']}
        if self.unsettled and name == 'active':
            return 200, {'tick': member['tick'], 'role': 'active',
                         'field_claim': None}
        return 200, {'tick': member['tick'], 'role': member['role'],
                     'field_claim': member['field_claim'],
                     'sync': 'unsynchronized' if name == 'active' else
                     {'tracking': {'aligned':
                                   self.members['active']['tick']}}}

    # --- the seats' runner-owned --journal-file ---------------------

    def _boundary(self, seat):
        path = self.journals[seat]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
        self.seats[seat]['boundary'] = True
        self.seq[seat] = 0

    def _journal(self, seat, event):
        path = self.journals[seat]
        self.seq[seat] = self.seq.get(seat, 0) + 1
        with path.open('a') as stream:
            stream.write(json.dumps({'entry': {
                'seq': self.seq[seat],
                'tick': self.seats[seat]['tick'],
                'event': event}}) + '\n')


class SelfStandbyTests(unittest.TestCase):
    """scenario_self_standby_refusal against the stubbed rig: the
    feed's transitions are lever-call keyed so each pass emits
    identical evidence, and every fault flag stages a named
    acceptance failure — the self-standby that lives a legitimate
    seat, the refusal that never named itself, the field that moved,
    the legitimate standby the gate over-refused, the deployed pair's
    disturbance, and the instability that must report
    nondeterministic."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = SelfStandbyFeed(self.tmp.name)
        self.plant = RendezvousPlantPeer(self.feed.TOKENS['active'])
        self.addCleanup(self.plant.close)

    def _ctx(self, feed=None, plant=None):
        feed = feed or self.feed
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'driven': 'http://ctrl-d:5',
                'foreign': 'http://ctrl-f:4',
                'plant': (plant or self.plant).address,
                'plant_remote': feed.REMOTE,
                'plant_owner': dict(feed.TOKENS),
                'evidence_dir': str(self.evidence),
                'journal_files': {seat: str(feed.journals[seat])
                                  for seat in feed.SEATS},
                'start_born_controller': feed.start_controller,
                'stop_born_controller': feed.stop_controller,
                'born_controller_state': feed.state}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'SR_REFUSE', 1.5), \
                patch.object(scenarios, 'SR_CONVERGE', 1.5), \
                patch.object(scenarios, 'SR_WATCH', 4), \
                patch.object(scenarios, 'SR_POLL', 0.001):
            return scenarios.scenario_self_standby_refusal(
                ctx or self._ctx(feed))

    def test_registered_after_released_before_revisions(self):
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_demote_release_stays_released),
            order.index(scenarios.scenario_self_standby_refusal))
        self.assertLess(
            order.index(scenarios.scenario_self_standby_refusal),
            order.index(scenarios.scenario_incompatible_revision))
        self.assertLess(
            order.index(scenarios.scenario_self_standby_refusal),
            order.index(scenarios.scenario_model_revision))

    def test_fixed_shape_passes_validates_and_sweeps(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = [entry['ref'] for entry in record['evidence']]
        self.assertEqual(
            sorted(refs),
            ['evidence/self-standby-refusal-pass-1.json',
             'evidence/self-standby-refusal-pass-2.json'])
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        kinds = [call[0] for call in self.feed.calls]
        # Each pass stands both seats and sweeps them again.
        self.assertEqual(kinds.count('start_born_controller'), 4)
        self.assertEqual(kinds.count('stop_born_controller'), 4)
        self.assertFalse(self.feed.seats)
        # The digests the two passes agreed on.
        self.assertIn('"self-standby": "refused-at-boot"',
                      record['observations'][0])

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        feed2 = SelfStandbyFeed(second_tmp.name)
        plant2 = RendezvousPlantPeer(feed2.TOKENS['active'])
        self.addCleanup(plant2.close)
        ctx2 = self._ctx(feed2, plant2)
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx2, feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        self.assertEqual(record2['observations'], record['observations'])
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    # The doctored negatives — each wrong disposition must fail the
    # run by the named diagnostic.

    def test_self_seat_legitimate_fails(self):
        # The issue's doctored negative: the self-standby refusing
        # nothing and living the pair's live standby seat.
        self.feed.self_legitimate = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('self-standby-refusal-failed',
                      record.get('detail', ''))
        self.assertIn("still standing as the pair's live standby",
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_zero_exit_fails(self):
        self.feed.self_exit_zero = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('self-standby-refusal-failed',
                      record.get('detail', ''))
        self.assertIn('did not exit nonzero', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unnamed_refusal_fails(self):
        self.feed.self_unnamed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never named the verdict',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refusal_without_own_socket_fails(self):
        self.feed.self_no_socket = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never named the verdict',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refusal_without_resolution_fails(self):
        # The usage block's echo, not the verdict: the flag, the
        # reason, and the address are all named, but nothing said the
        # target resolves to this instance.
        self.feed.self_no_resolution = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never named the verdict',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refusal_without_staged_target_fails(self):
        self.feed.self_no_target = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never named the verdict',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_silent_refusal_fails(self):
        self.feed.self_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never named the verdict',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_seat_served_fails(self):
        # A refused run that bound a monitor and served once before
        # it died — the seat is occupied for a scan.
        self.feed.self_serves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('answered during the refusal window',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_seat_journaled_fails(self):
        # A run record in the refused seat's journal — the launch was
        # never a pre-run usage error.
        self.feed.self_journaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('durable journal carries run records',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_seat_holds_field_fails(self):
        self.plant.claim = dict(self.plant.claim,
                                owner=self.feed.TOKENS['driven'])
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn("refused self-standby seat's own token",
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unclaimed_field_fails(self):
        self.plant.claim = None
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claim stood unclaimed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_foreign_claim_owner_fails(self):
        self.plant.misattributed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn("not the incumbent's own pinned token",
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_exit_fails(self):
        self.feed.control_exits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('self-standby-refusal-failed',
                      record.get('detail', ''))
        self.assertIn('naming a different instance must run',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_over_refusal_fails(self):
        # The gate passing by refusing every tracking source.
        self.feed.control_over_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('refusing every tracking source',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_never_tracks_fails(self):
        self.feed.control_never_tracks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never converged tracking on the incumbent',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_claim_unobserved_fails(self):
        self.feed.control_claim_unobserved = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn("does not observe the incumbent's held claim",
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_unjournaled_fails(self):
        self.feed.control_unjournaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('carries no run boundary',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_staged_on_own_target_fails(self):
        # The false-positive guard staged on the self-addressed
        # target — nothing proved the gate leaves other seats alone.
        self.feed.control_staged_own = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('staged on the self-addressed target',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_demotion_fails(self):
        self.feed.incumbent_demoted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn("pair's incumbent was disturbed",
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_stall_fails(self):
        self.feed.incumbent_stalls = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn("pair's incumbent was disturbed",
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_member_disturbance_fails(self):
        self.feed.member_disturbed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tracking member left standby',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unstaged_self_address_is_inconclusive(self):
        # The rig resolved the seat key to another seat's address: the
        # self-referential condition was never staged, so the leg has
        # nothing to judge.
        self.feed.own_target_mismatch = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('own announced address', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.unsettled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('settled shape', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_incumbent_is_inconclusive(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_launch_failure_is_nondeterministic(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('self-standby-refusal-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_vanished_container_is_nondeterministic(self):
        self.feed.self_vanished = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('self-standby-refusal-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_lost_claim_probe_is_nondeterministic(self):
        self.plant.close()
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('self-standby-refusal-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unremovable_seat_is_nondeterministic(self):
        self.feed.sweep_refuses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('self-standby-refusal-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('not every staged seat was removed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_second_pass_defect_fails(self):
        # Pass 2's legitimate standby never converges — a defect on
        # either pass is the contract's failure, and the pass's own
        # judge names it before the digests ever compare.
        self.feed.second_pass_defect = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('self-standby-refusal-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_seams_are_inconclusive(self):
        ctx = self._ctx()
        for key in ('plant_remote', 'plant', 'start_born_controller',
                    'stop_born_controller', 'born_controller_state'):
            ctx[key] = None
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('staging', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_journals_are_inconclusive(self):
        ctx = self._ctx()
        ctx['journal_files'] = {}
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_unchecked_self_check_fails(self):
        # A judge that names nothing slips every planted negative —
        # the leg reports itself unchecked rather than passing.
        with patch.object(scenarios, '_judge_self',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('self-standby-refusal-unchecked',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_judge_self_check_is_complete(self):
        # Every planted negative the leg can stage names the
        # diagnostic it must — the self-check slips nothing.
        self.assertEqual(scenarios._self_self_check(), [])


if __name__ == '__main__':
    unittest.main()
