"""The 2445_pending_source_pull leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_pending_source_pull, split out per the leg-module
convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


class PendingPullFeed:
    """A stubbed rig for the pending-source-pull leg. The deployed
    pair is the incumbent: 'active' owns the field's claim, 'standby'
    tracks it. `start_born_field('serving')` stages the leg's own
    scratch field; `pause_born_field` freezes it so the 'driven'
    born-active's conditional grant never answers — the pending
    source serves standby/unsynchronized/no-claim while its
    /checkpoint starves — and the 'foreign' standby launched
    tracking it inside the window reports the bounded pull miss.
    `unpause_born_field` thaws the field: the source claims, its
    checkpoint serves, and the tracker converges tracking inside one
    process lifetime, the planted journaled write's point_changed
    landing on its durable journal once the carrying checkpoint
    applies. Every transition is staged by the leg's own lever calls
    — never wall-clock — so two passes emit identical evidence.
    Fault flags stage each named failure and each pre-contract
    shape."""
    SEATS = ('driven', 'foreign', 'revised')
    HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby',
             'ctrl-c:3': 'revised', 'ctrl-f:4': 'foreign',
             'ctrl-d:5': 'driven'}
    REMOTE = 'dcs-hw-qa-1-plant:9001'
    BORN_REMOTE = 'dcs-hw-qa-1-born-plant:9003'
    TARGETS = {'driven': 'dcs-hw-qa-1-d:8082',
               'foreign': 'dcs-hw-qa-1-foreign:8082',
               'revised': 'dcs-hw-qa-1-c:8082',
               'active': 'dcs-hw-qa-1-a:8080',
               'standby': 'dcs-hw-qa-1-b:8080'}
    PENDING_DETAIL = ('fetch from dcs-hw-qa-1-d:8082: Resource '
                      'temporarily unavailable')
    FRESH_DETAIL = ('fetch from dcs-hw-qa-1-d:8082: checkpoint pull '
                    'still in flight')
    ADOPTION = {'point': 1011, 'from': {'bool': False},
                'to': {'bool': True}}
    ORPHAN = {'aligned': 7}

    def __init__(self, root):
        self.root = Path(root)
        self.journals = {
            seat: self.root / 'controllers' / seat / 'journal.jsonl'
            for seat in self.SEATS}
        self.paused = False
        self.thaws = 0
        self.planted = False
        self.members = {
            'active': {'role': 'active', 'tick': 0,
                       'field_claim': 'held'},
            'standby': {'role': 'standby', 'tick': 0,
                        'field_claim': 'held'}}
        self.seats = {}
        self.served = {seat: [] for seat in self.SEATS}
        self.calls = []
        self.seq = {}
        # Fault injection for the named-failure and pre-contract
        # cases.
        self.field_fails = False       # the born-field lever raises
        self.pause_fails = False       # the born-field pause lever
        self.launch_fails = False      # the born-controller lever
        self.freeze_exits = False      # pre-contract: the pending
                                       # source dies on the frozen
                                       # field
        self.thaw_exits = False        # pre-contract: the pending
                                       # source exits at the thaw
        self.pending_starves = False   # the source's pending monitor
                                       # never answers
        self.pending_reports_active = False
        self.window_probe_serves = False  # the frozen window's
                                          # checkpoint fetch answers —
                                          # no bounded misses to enter
        self.watch_starves = False     # the tracker's monitor never
                                       # answers inside the window
        self.tracker_window_tracking = False
        self.tracker_latch = False     # pre-contract: the verbatim
                                       # refusal replays forever
        self.tracker_stuck = False     # the doctored negative: fresh
                                       # degraded details forever
        self.tracker_orphans = False   # orphaned behind a claimed
                                       # source — the wrong verdict
        self.tracker_exits = False
        self.tracker_restarts = False  # a second run boundary lands
        self.pull_lapses = False       # converges, then degrades
        self.probe_refuses = False     # the endpoint keeps refusing
        self.adoption_unjournaled = False
        self.served_silent = False     # the served journal omits the
                                       # adoption the file carries
        self.source_unjournaled = False  # the planted write never
                                         # lands on the source
        self.source_never_serves = False
        self.second_pass_orphans = False  # pass 2 digests diverge
        self.control_never_converges = False
        self.control_exits = False
        self.control_restarts = False
        self.incumbent_demoted = False
        self.incumbent_stalls = False
        self.member_disturbed = False
        self.no_active = False         # the pair never settled

    # --- the runner's levers, faked ---------------------------------

    def start_field(self, mode):
        self.calls.append(('start_born_field', mode))
        if self.field_fails:
            raise RuntimeError('docker run failed: image missing')
        return {'container': 'dcs-hw-qa-1-born-plant',
                'remote': self.BORN_REMOTE, 'mode': mode}

    def pause_field(self):
        self.calls.append(('pause_born_field',))
        if self.pause_fails:
            raise RuntimeError('docker pause failed')
        self.paused = True

    def unpause_field(self):
        self.calls.append(('unpause_born_field',))
        self.paused = False
        self.thaws += 1
        if self.incumbent_demoted:
            self.members['active'].update(
                {'role': 'standby', 'field_claim': None})
        if self.member_disturbed:
            self.members['standby'].update({'role': 'active'})
        self._settle_source()

    def stop_field(self):
        self.calls.append(('stop_born_field',))

    def start_controller(self, seat, remote, peer=None, standby=None,
                         document=None):
        self.calls.append(('start_born_controller', seat, remote,
                           peer, standby))
        if self.launch_fails:
            raise RuntimeError('docker run failed: name in use')
        old = self.seats.get(seat)
        if old is not None and old['launched'] and not old['exited'] \
                and old['claimed']:
            raise RuntimeError('start_born_controller refuses to '
                               'replace dcs-hw-qa-1-' + seat
                               + ': it reports role active')
        self._boundary(seat)
        state = {'launched': True, 'exited': False, 'exit': None,
                 'logs': '', 'tick': 0, 'track': standby,
                 'pending': False, 'claimed': False, 'serving': False,
                 'adopted': False, 'orphaned': False,
                 'converged_once': False}
        self.seats[seat] = state
        if seat == 'driven':
            if self.paused:
                if self.freeze_exits:
                    # The pre-record disposition — the pending
                    # launch died on the frozen field.
                    state['exited'] = True
                    state['exit'] = 1
                    state['logs'] = 'remote driver connect failed'
                else:
                    state['pending'] = True
            else:
                state['claimed'] = True
                state['serving'] = True
        elif (seat == 'foreign' and self.tracker_exits) \
                or (seat == 'revised' and self.control_exits):
            state['exited'] = True
            state['exit'] = 1
            state['logs'] = 'checkpoint pull worker failed'
        if (seat == 'foreign' and self.tracker_restarts) \
                or (seat == 'revised' and self.control_restarts):
            # A second process lifetime lands on the journal — the
            # restart the contract forbids the convergence needing.
            self._journal_raw(seat,
                              {'run_boundary': {'run': 2, 'tick': 0}})
        return {'container': 'dcs-hw-qa-1-' + seat,
                'seat': seat, 'remote': remote,
                'standby': self.TARGETS.get(standby, standby)}

    def stop_controller(self, seat):
        self.calls.append(('stop_born_controller', seat))
        self.seats.pop(seat, None)

    def state(self, seat):
        self.calls.append(('born_controller_state', seat))
        state = self.seats.get(seat)
        if state is None or not state['launched']:
            return {'container': 'dcs-hw-qa-1-' + seat,
                    'running': False, 'exit': None, 'logs': '',
                    'absent': True}
        return {'container': 'dcs-hw-qa-1-' + seat,
                'running': not state['exited'],
                'exit': state['exit'], 'logs': state['logs'],
                'absent': False}

    # --- the staged field's thaw ------------------------------------

    def _settle_source(self):
        """The thaw's first answered contact: the pending born-active
        re-issues its conditional grant and — on the unclaimed
        scratch field — lands it, claiming and serving."""
        state = self.seats.get('driven')
        if state is None or not state['launched'] \
                or not state['pending']:
            return
        state['pending'] = False
        if self.thaw_exits:
            # The pre-contract disposition: the pending run exits
            # rather than claiming.
            state['exited'] = True
            state['exit'] = 1
            state['logs'] = 'the deferred grant never landed'
            return
        if self.source_never_serves:
            return
        state['serving'] = True
        if self.second_pass_orphans and self.thaws >= 2:
            return  # serving but never claimed — the orphaned path
        state['claimed'] = True

    # --- the monitor channel — replaces scenarios.http_json ---------

    def _advance(self):
        """One scan for every live member and seat — the request
        boundary is the tick boundary, so served ticks are call-count
        deterministic."""
        for name, member in self.members.items():
            if name == 'active' and self.incumbent_stalls:
                continue
            member['tick'] += 1
        for state in self.seats.values():
            if state['launched'] and not state['exited']:
                state['tick'] += 1

    def _sync(self, seat):
        state = self.seats[seat]
        if seat == 'revised':
            if self.control_never_converges:
                return 'unsynchronized'
            return {'tracking': {
                'aligned': self.members['active']['tick']}}
        if seat == 'driven':
            return 'unsynchronized'
        # 'foreign' — the tracking standby. Its verdict follows the
        # staged source's served state.
        source = self.seats.get('driven')
        if source is None or not source['launched'] \
                or source['exited']:
            return 'unsynchronized'
        if source['pending'] or not source['serving']:
            # The pending window: the pull meets the bounded miss the
            # defect latched.
            if self.tracker_window_tracking:
                return {'tracking': {'aligned': source['tick']}}
            return {'degraded': {'detail': self.PENDING_DETAIL}}
        if self.tracker_latch:
            # The pre-#1315 shape: the window's verbatim refusal
            # replays for process life.
            return {'degraded': {'detail': self.PENDING_DETAIL}}
        if self.tracker_stuck:
            return {'degraded': {'detail': self.FRESH_DETAIL}}
        if source['claimed'] and not self.tracker_orphans:
            if self.pull_lapses and state['converged_once']:
                return {'degraded': {'detail': self.FRESH_DETAIL}}
            state['converged_once'] = True
            if self.planted and not state['adopted']:
                state['adopted'] = True
                if not self.adoption_unjournaled:
                    self._journal(seat, {'point_changed':
                                         dict(self.ADOPTION)})
            return {'tracking': {'aligned': source['tick']}}
        if not source['orphaned']:
            source['orphaned'] = True
            self._journal(seat, {'field_orphaned': dict(self.ORPHAN)})
        return {'orphaned': {'aligned': source['tick']}}

    def _role(self, name):
        state = self.seats[name]
        if state['exited']:
            raise urllib.error.URLError('connection refused')
        report = {'tick': state['tick'], 'role': 'standby'}
        if name == 'driven':
            if state['claimed']:
                report.update({'role': 'active',
                               'field_claim': 'held'})
            else:
                if self.pending_starves:
                    raise urllib.error.URLError('connection refused')
                report['sync'] = 'unsynchronized'
                if self.pending_reports_active:
                    report.update({'role': 'active',
                                   'field_claim': 'held'})
            return report
        if name == 'foreign':
            source = self.seats.get('driven')
            if self.watch_starves and source is not None \
                    and not source['serving']:
                raise urllib.error.URLError('connection refused')
            report['sync'] = self._sync(name)
            if isinstance(report['sync'], dict) \
                    and 'tracking' in report['sync']:
                report['field_claim'] = 'held'
            return report
        report['sync'] = self._sync(name)
        report['field_claim'] = 'held'
        return report

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        name = self.HOSTS.get(host)
        if name is None:
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        self._advance()
        if name in ('active', 'standby'):
            if (method, route) != ('GET', '/role'):
                raise AssertionError('unexpected request %s %s'
                                     % (method, url))
            report = dict(self.members[name])
            report['sync'] = (
                'unsynchronized' if name == 'active'
                else {'tracking': {'aligned':
                                   self.members['active']['tick']}})
            if self.no_active:
                report['role'] = 'standby'
            return 200, report
        state = self.seats.get(name)
        if state is None or not state['launched']:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._role(name)
        if (method, route) == ('GET', '/checkpoint'):
            refused = state['pending'] or not state['serving'] \
                or self.probe_refuses
            if name == 'driven' and state['pending'] \
                    and self.window_probe_serves:
                refused = self.probe_refuses
            if name == 'driven' and refused:
                raise urllib.error.URLError('timed out')
            return 200, {'tick': state['tick'],
                         'source_owns_field': state['claimed']}
        if (method, route) == ('GET', '/journal'):
            served = [entry for entry in self.served[name]
                      if not (self.served_silent and entry['event']
                              and ('point_changed' in entry['event']
                                   or 'field_orphaned'
                                   in entry['event']))]
            return 200, served
        if (method, route) == ('POST', '/command'):
            if name != 'driven' or not state['claimed']:
                return 200, {'outcome': {'rejected': {'reason': {
                    'not_active': {}}}}}
            self.planted = True
            if not self.source_unjournaled:
                self._journal('driven', {'point_changed':
                                         dict(self.ADOPTION)})
            return 200, {'outcome': {'accepted': {
                'apply_tick': state['tick'] + 1}}}
        raise AssertionError('unexpected request %s %s' % (method, url))

    # --- the seat's runner-owned --journal-file ---------------------

    def _boundary(self, seat):
        path = self.journals[seat]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
        self.seq[seat] = 0
        self.served[seat] = []

    def _journal_raw(self, seat, record):
        path = self.journals[seat]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    def _journal(self, seat, event):
        self.seq[seat] = self.seq.get(seat, 0) + 1
        entry = {'seq': self.seq[seat],
                 'tick': self.seats[seat]['tick'], 'event': event}
        self._journal_raw(seat, {'entry': entry})
        self.served[seat].append(entry)


class PendingSourcePullTests(unittest.TestCase):
    """scenario_pending_source_pull against the stubbed rig: the
    feed's transitions are lever-call keyed so each pass emits
    identical evidence, and every fault flag stages a named
    acceptance failure — the latched or fresh-detail degradation the
    contract answers, the verdict the field's claim state cannot
    support, convergence by exit or restart, pulls that keep missing
    the serving endpoint, unjournaled evidence, the failed control,
    the disturbed deployed pair — plus the pre-contract shapes that
    must report inconclusive and the instability that must report
    nondeterministic."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = PendingPullFeed(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, feed=None):
        feed = feed or self.feed
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'foreign': 'http://ctrl-f:4',
                'driven': 'http://ctrl-d:5',
                'plant_remote': feed.REMOTE,
                'plant_owner': {'active': 424243, 'standby': 424244},
                'evidence_dir': str(self.evidence),
                'journal_files': {seat: str(feed.journals[seat])
                                  for seat in feed.SEATS},
                'start_born_field': feed.start_field,
                'pause_born_field': feed.pause_field,
                'unpause_born_field': feed.unpause_field,
                'stop_born_field': feed.stop_field,
                'start_born_controller': feed.start_controller,
                'stop_born_controller': feed.stop_controller,
                'born_controller_state': feed.state}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'PULL_PENDING', 2.0), \
                patch.object(scenarios, 'PULL_SETTLE', 2.0), \
                patch.object(scenarios, 'PULL_POLL', 0.001), \
                patch.object(scenarios, 'WINDOW_HOLD', 0.001):
            return scenarios.scenario_pending_source_pull(
                ctx or self._ctx(feed))

    def test_registered_in_the_born_seat_window(self):
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_pending_serving_bound),
            order.index(scenarios.scenario_pending_source_pull))
        self.assertLess(
            order.index(scenarios.scenario_ownerless_remote_backoff),
            order.index(scenarios.scenario_pending_source_pull))
        for later in (scenarios.scenario_sim_cyclic_fencing_loss_demote,
                      scenarios.scenario_sim_bus_startup_claim_refusal,
                      scenarios.scenario_incompatible_revision,
                      scenarios.scenario_model_revision):
            self.assertLess(
                order.index(scenarios.scenario_pending_source_pull),
                order.index(later))

    def test_fixed_shape_passes_validates_and_tears_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = [entry['ref'] for entry in record['evidence']]
        self.assertEqual(
            sorted(refs),
            ['evidence/pending-source-pull-pass-1.json',
             'evidence/pending-source-pull-pass-2.json'])
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        kinds = [call[0] for call in self.feed.calls]
        # Each pass freezes and thaws the scratch field once and
        # sweeps all three seats and the field.
        self.assertEqual(kinds.count('start_born_field'), 2)
        self.assertEqual(kinds.count('pause_born_field'), 2)
        self.assertEqual(kinds.count('unpause_born_field'), 2)
        self.assertEqual(kinds.count('stop_born_controller'), 6)
        self.assertEqual(kinds.count('stop_born_field'), 2)
        self.assertFalse(self.feed.seats)
        self.assertFalse(self.feed.paused)
        # The staged launches: the tracker wired --standby at the
        # pending seat on the scratch field, the control at the
        # deployed incumbent on the pair's own plant.
        launches = [call for call in self.feed.calls
                    if call[0] == 'start_born_controller']
        self.assertEqual(launches[0],
                         ('start_born_controller', 'driven',
                          self.feed.BORN_REMOTE, None, None))
        self.assertEqual(launches[1],
                         ('start_born_controller', 'foreign',
                          self.feed.BORN_REMOTE, None, 'driven'))
        self.assertEqual(launches[2],
                         ('start_born_controller', 'revised',
                          self.feed.REMOTE, None, 'active'))

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        feed2 = PendingPullFeed(second_tmp.name)
        ctx2 = self._ctx(feed2)
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx2, feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    # The doctored negatives — each wrong disposition must fail the
    # run by the named diagnostic.

    def test_converged_but_degraded_fails(self):
        # The issue's doctored negative: the standby stays degraded
        # past the documented bound — on post-contract details, not
        # the pending window's verbatim refusal.
        self.feed.tracker_stuck = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-failed',
                      record.get('detail', ''))
        self.assertIn('latched', record.get('detail', ''))
        report.validate_scenario(record)

    def test_wrong_verdict_fails(self):
        # The field is claimed — orphaned is not the honest verdict.
        self.feed.tracker_orphans = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_tracker_exit_fails(self):
        self.feed.tracker_exits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_tracker_restart_fails(self):
        self.feed.tracker_restarts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-failed',
                      record.get('detail', ''))
        self.assertIn('restart', record.get('detail', ''))
        report.validate_scenario(record)

    def test_pull_endpoint_refuses_fails(self):
        self.feed.probe_refuses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pull_lapse_fails(self):
        self.feed.pull_lapses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_adoption_unjournaled_fails(self):
        self.feed.adoption_unjournaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_served_journal_silent_fails(self):
        self.feed.served_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_never_converges_fails(self):
        self.feed.control_never_converges = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_exits_fails(self):
        self.feed.control_exits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_restarts_fails(self):
        self.feed.control_restarts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_demoted_fails(self):
        self.feed.incumbent_demoted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-failed',
                      record.get('detail', ''))
        self.assertIn('incumbent', record.get('detail', ''))
        report.validate_scenario(record)

    def test_member_disturbed_fails(self):
        self.feed.member_disturbed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_window_tracker_dishonest_fails(self):
        # The wedge shape: tracking behind a source that owns nothing.
        self.feed.tracker_window_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pending_reports_active_fails(self):
        self.feed.pending_reports_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_latched_refusal_is_inconclusive(self):
        # The pre-#1315 shape: the pending window's verbatim refusal
        # replays forever while the same endpoint serves — the staged
        # revision predates the contract, so the leg cannot assert.
        self.feed.tracker_latch = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        report.validate_scenario(record)

    def test_pending_source_exit_is_inconclusive(self):
        # The pre-record disposition: the born-active died on the
        # frozen field rather than standing pending.
        self.feed.freeze_exits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        report.validate_scenario(record)

    def test_thaw_exit_is_inconclusive(self):
        self.feed.thaw_exits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_field_launch_failure_is_nondeterministic(self):
        self.feed.field_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pause_failure_is_nondeterministic(self):
        self.feed.pause_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_launch_failure_is_nondeterministic(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_tracker_watch_starved_is_nondeterministic(self):
        self.feed.watch_starves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_source_watch_starved_is_nondeterministic(self):
        self.feed.pending_starves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_window_probe_served_is_nondeterministic(self):
        # The frozen window's checkpoint fetch answered — the
        # bounded misses the standby's pull entered never staged.
        self.feed.window_probe_serves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unserved_source_is_nondeterministic(self):
        # The thawed field never produced a serving source — the
        # convergence trigger never landed.
        self.feed.source_never_serves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_plant_is_nondeterministic(self):
        # The planted write applied on the source but never journaled
        # there — the adoption stimulus never provably landed.
        self.feed.source_unjournaled = True
        self.feed.adoption_unjournaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_divergent_digests_are_nondeterministic(self):
        # Pass 2's source serves but never claims — the tracker
        # converges orphaned, so the digests diverge.
        self.feed.second_pass_orphans = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('digest', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_seams_are_inconclusive(self):
        ctx = self._ctx()
        for key in ('start_born_field', 'pause_born_field',
                    'unpause_born_field', 'stop_born_field',
                    'start_born_controller', 'stop_born_controller',
                    'born_controller_state', 'plant_remote'):
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
        with patch.object(scenarios, '_judge_pull',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-source-pull-unchecked',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_judge_self_check_is_complete(self):
        # Every planted negative the leg can stage names the
        # diagnostic it must — the self-check slips nothing.
        self.assertEqual(scenarios._pull_self_check(), [])
