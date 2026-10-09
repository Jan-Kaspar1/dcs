"""The 3560_sim_bus_driver_reattach leg's scenario unit coverage — the
feed fake and TestCase class for scenario_sim_bus_driver_reattach. The
shared fakes and helpers live in tests/qa_scenario_support.py.


The feed stages the leg's shape: the lane's sim-bus device server is
staged through the run context's sim_bus_device levers, a controller
pair is born-launched onto the driven/foreign seats with the staged
document, and the restart/freeze/thaw/serving levers replace the
runner's docker actions — every /snapshot read on a member is one
completed scan whose io_health carries the point-wise driver's
diagnostics: the link disconnected with the severing failure
named in last_error while the device is out, the first exchange after
re-attach clearing the standing record — the #1351 contract the leg
exists to prove. Doctor flags stage each named defect, each
nondeterministic surface, and each pre-contract shape the leg
inconcludes on."""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


# The register-mapped model the fake device launch stages — the same
# shape the run config's sim_bus_device block names: one `sim-bus`
# device carrying the placeholder the lane binds, so the leg's
# staged-document read finds its subject.
BUS_MODEL = {
    'version': 1,
    'devices': [{'id': 1, 'kind': 'sim-bus',
                 'parameters': {'address': '__BUS_ADDR__',
                                'registers': {'level_raw': 0}},
                 'channels': {'level_raw': {'direction': 'in',
                                            'value_type': 'float'}}}],
    'io_points': [
        {'id': 10, 'direction': 'in', 'value_type': 'float',
         'channel': {'device': 1, 'name': 'level_raw'}}],
    'signals': [], 'components': [], 'connections': []}


class BusFeed:
    """A stubbed register-field rig for the sim-bus driver-reattach
    scenario. The driven seat owns the device; the foreign seat is the
    tracking standby. `start_device`/`restart`/`freeze`/`thaw`/
    `serving` replace the runner's sim-bus device levers and the born
    launches stand the seats' containers up — flipping
    `device_up`/`link_ok`.
    A restart leaves the device unanswered for a fixed number of
    monitor reads before it re-binds; once the device serves again the
    link re-attaches after REATTACH_DELAY reads — the lazy-reattach
    spacing the fix names — so the outage surfaces on the served
    diagnostics before recovery lands. Every /snapshot read is one
    completed scan: the severed link counts the boundary failures and
    stands the named record; the first exchange after re-attach clears
    it — unless a doctor holds it. Doctor flags stage each named
    defect, each nondeterministic surface, and each inconclusive rig
    state the issue calls out."""

    HOSTS = {'ctrl-a:3': 'active', 'ctrl-b:4': 'standby'}
    SEATS = {'driven': 'http://ctrl-a:3', 'foreign': 'http://ctrl-b:4'}
    LINK_ERROR = 'register exchange timed out'
    REATTACH_DELAY = 2     # reads between the device's return and the
                         # link reporting connected again
    RESTART_DELAY = 3      # reads the restarted device stays dead
    STALL_DELAY = 3        # reads between a thaw and the re-attach —
                           # the degraded watch opens on the thaw, so
                           # the link must stay severed for a full
                           # poll round to surface on both members

    def __init__(self, tmp=None):
        self.tmp = tmp or tempfile.mkdtemp()
        self.tick = 0
        self.reads = 0            # /snapshot reads served
        self.device_up = True
        self.link_ok = True
        self.returns_at = None    # read count a restart re-binds at
        self.reattach_at = None   # read count the link re-attaches at
        self.owner = 'ctrl-a:3'   # the field-owning member's host
        self.calls = []           # the device levers run
        self.failed_reads = 0
        self.failed_writes = 0
        self.consecutive = 0
        self.io_fault = None      # io_health's recorded boundary fault
        self.driver_error = None  # the driver's standing last_error
        # The contract defect doctors.
        self.link_stuck = False      # never reports connected again
        self.healthy_through = False # the link reports connected anyway
        self.lingering = False       # the standing record never clears
        self.stuck_streak = False    # the streak stands past recovery
        self.resets_health = False   # io_health zeroes on recovery
        # The nondeterministic surfaces.
        self.tick_regress = False    # the members re-serve tick 0
        self.promote_noop = False    # /promote answers but never lands
        self.restore_noop = False    # the restore promote never lands
        # The staging and rig-state doctors.
        self.restart_raises = False  # the restart lever itself fails
        self.never_returns = False   # a restart leaves the device dead
        self.swift = False           # the outage lands entirely
                                    # between two of the leg's polls
        self.ctl_raises = False      # the device tool cannot be asked
        self.silent_rig = False      # every endpoint refuses
        self.untracked = False       # the standby never tracks
        self.both_standby = False    # no member reports active
        self.precontract = False     # io_health carries no backend
        self.aborts = False          # a monitor dies on the outage
        self.aborts_recovery = False  # ... or during the recovery watch
        self.promote_status = None   # the promote probe answers non-200
        self.restore_status = None   # the restore promote answers non-200
        # The staging fakes' call records.
        self.staged = []             # start_sim_bus_device kwargs
        self.born = []               # start_born_controller calls
        self.stopped = []            # teardown order: seats, device
        self.stage_raises = False    # the device launch itself fails
        self.launch_raises = False   # the born launch never runs

    # The runner's sim-bus device levers — replace the ctx's
    # start/restart/stop/freeze/thaw/sim_bus_device_serving. The
    # severing failure is named by the failed scan, never by the
    # lever itself: a pause or a restart severs the socket, and the
    # driver learns of it on its next exchange.
    def start_device(self, fixture=None, timeout_ms=None):
        self.staged.append({'fixture': fixture,
                            'timeout_ms': timeout_ms})
        if self.stage_raises:
            raise RuntimeError('docker run failed')
        self.device_up = True
        self.link_ok = True
        document = Path(self.tmp) / 'sim-bus' / 'model.json'
        document.parent.mkdir(parents=True, exist_ok=True)
        staged = json.loads(json.dumps(BUS_MODEL))
        device = staged['devices'][0]
        device['parameters']['address'] = 'dcs-hw-qa-1-bus:9005'
        if timeout_ms is not None:
            device['parameters']['timeout_ms'] = timeout_ms
        document.write_text(json.dumps(staged) + '\n')
        return {'container': 'dcs-hw-qa-1-bus',
                'address': 'dcs-hw-qa-1-bus:9005', 'port': 9005,
                'device': 1, 'model': str(document)}

    def stop_device(self):
        self.stopped.append('device')
        self.device_up = False
        self.link_ok = False

    def start_born(self, seat, remote, peer=None, standby=None,
                   document=None):
        self.born.append({'seat': seat, 'remote': remote,
                          'peer': peer, 'standby': standby,
                          'document': document})
        if self.launch_raises:
            raise RuntimeError('docker run failed')
        return {'seat': seat, 'monitor': self.SEATS[seat]}

    def stop_born(self, seat):
        self.stopped.append(seat)

    def restart(self):
        self.calls.append('restart')
        if self.restart_raises:
            raise RuntimeError('docker restart failed')
        if self.swift:
            # A driver faster than the leg's poll cadence: the device
            # is back and the link re-attached before the leg read
            # anything, so the only trace of the outage is the
            # boundary accounting it left behind.
            self.failed_reads += 5
            self.failed_writes += 2
            self.io_fault = {'tick': self.tick, 'point': 11,
                             'direction': 'in', 'error': {'timeout': 11}}
            return
        self.device_up = False
        self.link_ok = False
        self.returns_at = None if self.never_returns \
            else self.reads + self.RESTART_DELAY
        self.reattach_at = None

    def freeze(self):
        self.calls.append('freeze')
        self.device_up = False
        self.link_ok = False

    def thaw(self):
        self.calls.append('thaw')
        self.device_up = True
        if not self.link_stuck:
            self.reattach_at = self.reads + self.STALL_DELAY

    def serving(self, device):
        if self.ctl_raises:
            raise RuntimeError('docker inspect failed')
        return self.device_up and (
            self.returns_at is None or self.reads >= self.returns_at)

    # One completed scan per /snapshot read: the severed link counts
    # the boundary failures and stands the named record; the first
    # healthy exchange clears it — unless a doctor holds it.
    def _scan(self):
        self.reads += 1
        self.tick += 1
        if self.returns_at is not None \
                and self.reads >= self.returns_at:
            self.device_up = True
            self.returns_at = None
            if not self.link_stuck:
                self.reattach_at = self.reads + self.REATTACH_DELAY
        if self.reattach_at is not None \
                and self.reads >= self.reattach_at:
            self.link_ok = True
            self.reattach_at = None
            if self.tick_regress:
                self.tick = 0  # a restarted process re-serves tick 0
                               # — mid-watch, against the running peak
        if not self.link_ok:
            self.failed_reads += 5
            self.failed_writes += 2
            self.consecutive += 7
            self.io_fault = {'tick': self.tick, 'point': 11,
                             'direction': 'in',
                             'error': {'timeout': 11}}
            if not self.healthy_through:
                self.driver_error = self.LINK_ERROR
            return
        if not self.lingering:
            self.driver_error = None
        self.consecutive = max(self.consecutive, 3) \
            if self.stuck_streak else 0
        if self.resets_health:
            self.failed_reads = 0
            self.failed_writes = 0
            self.io_fault = None

    def _link(self):
        if self.healthy_through:
            return 'connected'
        return 'connected' if self.link_ok else 'disconnected'

    def _health(self):
        if self.precontract:
            return {'failed_reads': self.failed_reads,
                    'failed_writes': self.failed_writes,
                    'consecutive_failures': self.consecutive,
                    'last_error': self.io_fault}
        return {'failed_reads': self.failed_reads,
                'failed_writes': self.failed_writes,
                'consecutive_failures': self.consecutive,
                'last_error': self.io_fault,
                'driver': {'link': self._link(),
                           'last_error': self.driver_error,
                           'exchange': None}}

    def _role(self, host):
        if self.both_standby:
            return {'role': 'standby', 'tick': self.tick,
                    'sync': {'tracking': {'aligned': self.tick}}}
        if host == self.owner:
            return {'role': 'active', 'tick': self.tick}
        report = {'role': 'standby', 'tick': self.tick}
        report['sync'] = {'tracking': {'aligned': self.tick}} \
            if not self.untracked \
            else {'degraded': {'reason': 'test'}}
        return report

    # The monitor surface — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        if self.silent_rig or host not in self.HOSTS:
            raise urllib.error.URLError('connection refused')
        name = self.HOSTS[host]
        if self.aborts and not self.link_ok:
            raise urllib.error.URLError('connection refused')
        if self.aborts_recovery and self.link_ok \
                and self.reads > self.RESTART_DELAY:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._role(host)
        if (method, route) == ('GET', '/snapshot'):
            self._scan()
            return 200, {'tick': self.tick, 'points': [],
                         'io_health': self._health()}
        if (method, route) == ('POST', '/promote'):
            doctor = self.promote_status if self.owner == 'ctrl-a:3' \
                else self.restore_status
            if doctor is not None:
                return doctor, {'outcome': {'refused': 'doctored'}}
            if name == 'standby' \
                    or (name == 'active' and self.owner == 'ctrl-b:4'):
                noop = self.promote_noop if self.owner == 'ctrl-a:3' \
                    else self.restore_noop
                if not noop:
                    self.owner = host
            return 200, {'outcome': 'promoted'}
        raise AssertionError('unexpected request %s %s' % (method, url))


class BusDriverReattachTests(unittest.TestCase):
    """scenario_sim_bus_driver_reattach against the stubbed rig: the
    restart and freeze/thaw levers cycle the register device while the
    monitors' served io_health carries the point-wise driver's
    link health — the outage named and counted, the re-attach landing
    inside the bound with the standing record cleared and the outage's
    history kept, roles held, and the promote probe answering — twice
    per class with identical digests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = BusFeed(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, feed=None):
        feed = feed or self.feed
        return {'active': 'http://deployed-a:1',
                'standby': 'http://deployed-b:2',
                'driven': feed.SEATS['driven'],
                'foreign': feed.SEATS['foreign'],
                'evidence_dir': str(self.evidence),
                'sim_bus_device': {'device': 1, 'port': 9005,
                                   'model_fixture': 'bus.json',
                                   'cyclic_model': 'cyclic.json'},
                'start_sim_bus_device': feed.start_device,
                'restart_sim_bus_device': feed.restart,
                'stop_sim_bus_device': feed.stop_device,
                'freeze_sim_bus_device': feed.freeze,
                'thaw_sim_bus_device': feed.thaw,
                'sim_bus_device_serving': feed.serving,
                'start_born_controller': feed.start_born,
                'stop_born_controller': feed.stop_born}

    def _subject(self, ctx):
        """The subject shape the staged run builds — the seats' monitor
        URLs under their launch-role names plus the ctx's device
        levers."""
        return {'active': ctx['driven'], 'standby': ctx['foreign'],
                'device': ctx['sim_bus_device']['device'],
                'restart_device': ctx['restart_sim_bus_device'],
                'freeze_device': ctx['freeze_sim_bus_device'],
                'thaw_device': ctx['thaw_sim_bus_device'],
                'device_serving': ctx['sim_bus_device_serving']}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'BUS_REATTACH_BOUND', 0.5), \
                patch.object(scenarios, 'BUS_REATTACH_DEGRADE', 0.5), \
                patch.object(scenarios, 'BUS_REATTACH_PROMOTE', 0.5), \
                patch.object(scenarios, 'BUS_REATTACH_SETTLE', 0.5), \
                patch.object(scenarios, 'BUS_REATTACH_STALL', 0.001), \
                patch.object(scenarios, 'BUS_REATTACH_POLL', 0.001):
            return scenarios.scenario_sim_bus_driver_reattach(
                ctx or self._ctx(feed))

    def _evidence(self, name):
        return json.loads((self.evidence / name).read_text())

    def run_leg(self, leg, ctx=None, feed=None):
        """One outage class through the leg's own driver, for the
        assertions a whole-run verdict cannot isolate."""
        feed = feed or self.feed
        ctx = ctx or self._ctx(feed)
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'BUS_REATTACH_BOUND', 0.5), \
                patch.object(scenarios, 'BUS_REATTACH_DEGRADE', 0.5), \
                patch.object(scenarios, 'BUS_REATTACH_PROMOTE', 0.5), \
                patch.object(scenarios, 'BUS_REATTACH_SETTLE', 0.5), \
                patch.object(scenarios, 'BUS_REATTACH_STALL', 0.001), \
                patch.object(scenarios, 'BUS_REATTACH_POLL', 0.001):
            return scenarios._bus_outage(
                ctx, self._subject(ctx), 'active', 'standby',
                leg, 1)

    def test_registered(self):
        self.assertIn(scenarios.scenario_sim_bus_driver_reattach,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('sim-bus-driver-reattach'),
                      scenarios.scenario_sim_bus_driver_reattach)
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_remote_driver_recovery),
            order.index(
                scenarios.scenario_sim_bus_driver_reattach))

    def test_clean_passes_validate_and_restore(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.calls,
                         ['restart', 'freeze', 'thaw'] * 2)
        # The staging: the device server launched with the leg's
        # per-request timeout stamped, the pair born-launched onto the
        # driven/foreign seats with the staged document and no
        # --remote, and the run's teardown removed seats and device.
        self.assertEqual(len(self.feed.staged), 1)
        self.assertEqual(self.feed.staged[0]['timeout_ms'],
                         scenarios.BUS_FIELD_TIMEOUT)
        self.assertEqual(
            [call['seat'] for call in self.feed.born],
            ['driven', 'foreign'])
        self.assertIsNone(self.feed.born[0]['remote'])
        self.assertEqual(self.feed.born[0]['peer'], 'foreign')
        self.assertEqual(self.feed.born[1]['standby'], 'driven')
        self.assertTrue(all(
            call['document'].endswith('sim-bus/model.json')
            for call in self.feed.born))
        staged = json.loads(
            Path(self.feed.born[0]['document']).read_text())
        device = staged['devices'][0]
        self.assertEqual(device['kind'], 'sim-bus')
        self.assertEqual(device['parameters']['timeout_ms'],
                         scenarios.BUS_FIELD_TIMEOUT)
        self.assertEqual(self.feed.stopped,
                         ['driven', 'foreign', 'device'])
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        stall = self._evidence(
            'sim-bus-driver-reattach-field-stall-pass-1.json')
        # The stall class holds the device down until every member
        # has surfaced the outage, so the degraded trace is a fact
        # the leg witnessed, not one it raced the re-attach for.
        self.assertEqual(stall['degraded_members'],
                         ['active', 'standby'])
        degraded = stall['degrade_trace'][-1]
        self.assertEqual(degraded['link'], 'disconnected')
        self.assertEqual(degraded['last_error'], BusFeed.LINK_ERROR)
        self.assertGreater(degraded['consecutive'], 0)
        self.assertTrue(degraded['fault'])
        self.assertTrue(stall['device_serving'])
        # Both classes: the first serve reporting the link connected
        # after the outage carries the cleared record, the reset
        # streak, and the counted history — the durable contract.
        for leg in ('device-restart', 'field-stall'):
            first = self._evidence(
                'sim-bus-driver-reattach-' + leg + '-pass-1.json')
            self.assertEqual(
                first['digest']['recovered'], 're-attached', leg)
            self.assertEqual(first['digest']['outage'], 'counted', leg)
            self.assertEqual(first['digest']['cadence'], 'advancing', leg)
            self.assertEqual(first['digest']['roles'], 'held', leg)
            for name in ('active', 'standby'):
                base = first['baseline'][name]
                connected = first['first_connected'][name]
                self.assertEqual(connected['driver']['link'],
                                 'connected', leg)
                self.assertIsNone(connected['driver']['last_error'], leg)
                self.assertEqual(connected['io_health']
                                 ['consecutive_failures'], 0, leg)
                self.assertTrue(connected['io_health']['last_error'], leg)
                self.assertGreater(connected['io_health']['failed_reads'],
                                   base['io_health']['failed_reads'], leg)
                self.assertGreater(connected['tick'], base['tick'], leg)
        digest = self._evidence('sim-bus-driver-reattach-digest.json')
        for leg in ('device-restart', 'field-stall'):
            self.assertEqual(len(digest['passes'][leg]), 2)
            self.assertEqual(digest['passes'][leg][0],
                             digest['passes'][leg][1])
        probe = self._evidence('sim-bus-driver-reattach-promote.json')
        self.assertEqual(probe['status'], 200)
        self.assertEqual(probe['restore_status'], 200)
        self.assertEqual(self.feed.owner, 'ctrl-a:3')

    def test_evidence_entries_cover_the_leg(self):
        record = self.run_scenario()
        refs = [entry['ref'] for entry in record['evidence']]
        self.assertEqual(refs, [
            'evidence/sim-bus-driver-reattach-device-restart-pass-1.json',
            'evidence/sim-bus-driver-reattach-field-stall-pass-1.json',
            'evidence/sim-bus-driver-reattach-device-restart-pass-2.json',
            'evidence/sim-bus-driver-reattach-field-stall-pass-2.json',
            'evidence/sim-bus-driver-reattach-digest.json',
            'evidence/sim-bus-driver-reattach-promote.json'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_digests(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = self._evidence('sim-bus-driver-reattach-digest.json')
        tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(tmp2.cleanup)
        evidence2 = Path(tmp2.name) / 'evidence'
        evidence2.mkdir()
        feed2 = BusFeed(tmp2.name)
        ctx2 = self._ctx(feed2)
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx2, feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        self.assertEqual(first, json.loads(
            (evidence2 / 'sim-bus-driver-reattach-digest.json').read_text()))

    # The doctored negatives — each named defect must fail the run by
    # the named diagnostic.

    def test_never_reattached_fails(self):
        # The issue's own doctored negative: recovery asserted while
        # the driver stays link:disconnected past the documented bound
        # — the device answers but the link never returns.
        self.feed.link_stuck = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'sim-bus-reattach-failed'), record['detail'])
        self.assertIn('never re-attached', record['detail'])
        report.validate_scenario(record)

    def test_never_degraded_fails(self):
        # The stall class is the one whose window the leg owns, so it
        # is the class that can demand the outage be surfaced: the
        # freeze is held past the documented stall until both members
        # report it, which no one-second re-attach can outrun.
        self.feed.healthy_through = True
        _digest, violations, _evidence = self.run_leg('field-stall')
        self.assertEqual(sorted(violations), ['never-degraded'])
        diagnostic, detail = violations['never-degraded']
        self.assertEqual(diagnostic, 'sim-bus-reattach-failed')
        self.assertIn('never surfaced', detail)
        # The lane leaves the device running even on the failed leg.
        self.assertEqual(self.feed.calls[-1], 'thaw')

    def test_swallowed_diagnostics_fail_the_run(self):
        # The same doctor through the restart class, whose window the
        # device sets: the served link claims connected while the
        # boundary still fails, so the failure streak stands behind a
        # healthy-looking link.
        self.feed.healthy_through = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'sim-bus-reattach-failed'), record['detail'])
        self.assertIn('failure streak still stood', record['detail'])
        report.validate_scenario(record)

    def test_outage_between_polls_still_counts(self):
        # The fix re-attaches on a one-second bound, so a driver can
        # drop and restore between two of the leg's reads. That is a
        # recovery the contract permits: the boundary accounting is
        # the other witness, and the leg must not read the missed
        # transient as a defect.
        self.feed.swift = True
        digest, violations, evidence = self.run_leg('device-restart')
        self.assertEqual(violations, {})
        self.assertEqual(digest['recovered'], 're-attached')
        self.assertEqual(digest['outage'], 'counted')
        self.assertEqual(evidence['degraded_members'], [])
        for name in ('active', 'standby'):
            self.assertIsNone(
                evidence['first_connected'][name]['driver']['last_error'])
            self.assertGreater(
                evidence['first_connected'][name]['io_health']
                ['failed_reads'],
                evidence['baseline'][name]['io_health']['failed_reads'])

    def test_lingering_error_fails(self):
        self.feed.lingering = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('lingered', record['detail'])
        report.validate_scenario(record)

    def test_streak_standing_fails(self):
        self.feed.stuck_streak = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('failure streak still stood', record['detail'])
        report.validate_scenario(record)

    def test_reset_history_fails(self):
        self.feed.resets_health = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('counted outage history reset',
                      record['detail'])
        report.validate_scenario(record)

    def test_baseline_linger_fails(self):
        # A standing record the healthy exchanges never clear — the
        # defect caught ahead of any staging.
        self.feed.lingering = True
        self.feed.driver_error = 'a refuse record left standing'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'sim-bus-reattach-failed'), record['detail'])
        self.assertIn('ahead of the device-restart', record['detail'])
        self.assertEqual(self.feed.calls, [])
        report.validate_scenario(record)

    def test_aborted_run_fails(self):
        self.feed.aborts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('monitor stopped answering', record['detail'])
        report.validate_scenario(record)

    def test_aborted_recovery_fails(self):
        self.feed.aborts_recovery = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('monitor stopped answering', record['detail'])
        report.validate_scenario(record)

    def test_rewound_tick_reports_nondeterministic(self):
        self.feed.tick_regress = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'sim-bus-reattach-nondeterministic'), record['detail'])
        self.assertIn('rewound', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        calls = []
        real = scenarios._bus_outage

        def diverging(*args, **kwargs):
            digest, violations, evidence = real(*args, **kwargs)
            calls.append(1)
            if len(calls) > 1 and not violations:
                digest = dict(digest)
                digest['roles'] = 'wiggled'
            return digest, violations, evidence

        with patch.object(scenarios, '_bus_outage', diverging):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'sim-bus-reattach-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_promotion_refusal_fails(self):
        self.feed.promote_status = 409
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'sim-bus-reattach-failed'), record['detail'])
        self.assertIn('409', record['detail'])
        report.validate_scenario(record)

    def test_promotion_never_settles_fails(self):
        self.feed.promote_noop = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never settled active', record['detail'])
        report.validate_scenario(record)

    def test_unsettled_restore_is_nondeterministic(self):
        self.feed.restore_noop = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'sim-bus-reattach-nondeterministic'), record['detail'])
        self.assertIn('never regained active', record['detail'])
        report.validate_scenario(record)

    def test_restore_refusal_fails(self):
        self.feed.restore_status = 409
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('restoring the launch roles failed',
                      record['detail'])
        report.validate_scenario(record)

    # Rig states the contract cannot answer for report inconclusive.

    def test_no_subject_is_inconclusive(self):
        ctx = self._ctx()
        ctx['sim_bus_device'] = None
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('stages no sim-bus device server',
                      record['detail'])
        report.validate_scenario(record)

    def test_missing_lever_is_inconclusive(self):
        ctx = self._ctx()
        ctx['freeze_sim_bus_device'] = None
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('freeze_sim_bus_device', record['detail'])
        report.validate_scenario(record)

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.silent_rig = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.untracked = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never settled tracking', record['detail'])
        report.validate_scenario(record)

    def test_leaderless_pair_fails(self):
        self.feed.both_standby = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no bus pair member reports role=active',
                      record['detail'])
        report.validate_scenario(record)

    def test_precontract_surface_is_inconclusive(self):
        # The staged revision's served surface predates the contract:
        # io_health carries no backend driver diagnostics at all.
        self.feed.precontract = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])
        self.assertEqual(self.feed.calls, [])
        report.validate_scenario(record)

    def test_unreturned_device_is_inconclusive(self):
        # The link never re-attaches and the device itself never
        # serves again — the restart's restore never landed, a rig
        # state the contract cannot answer for.
        self.feed.never_returns = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never served again', record['detail'])
        report.validate_scenario(record)

    def test_unclassifiable_restore_is_inconclusive(self):
        self.feed.link_stuck = True
        self.feed.ctl_raises = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unclassifiable', record['detail'])
        report.validate_scenario(record)

    def test_failed_staging_is_inconclusive(self):
        # Each staging seam, refused: the device-server launch, the
        # born pair launch, and the restart lever mid-outage — all rig
        # states the contract cannot answer for.
        self.feed.stage_raises = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never staged', record['detail'])
        report.validate_scenario(record)

        feed = BusFeed(self.tmp.name)
        feed.launch_raises = True
        record = self.run_scenario(self._ctx(feed), feed)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('pair launch never ran', record['detail'])
        report.validate_scenario(record)

        feed = BusFeed(self.tmp.name)
        feed.restart_raises = True
        record = self.run_scenario(self._ctx(feed), feed)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('staging lever never completed',
                      record['detail'])
        report.validate_scenario(record)

    # The leg's own auditors must catch their planted negatives.

    def test_unchecked_self_check_fails(self):
        with patch.object(scenarios, '_bus_reattach_audit',
                          lambda *args, **kwargs: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-reattach-unchecked',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_self_check_is_complete(self):
        self.assertEqual(scenarios._bus_reattach_self_check(), [])


if __name__ == '__main__':
    unittest.main()
