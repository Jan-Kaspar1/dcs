"""The 0850_superseded_restart leg's scenario unit coverage — the feed
fake and TestCase classes for scenario_superseded_restart, split out
of the test_qa_scenarios monolith (#940). The shared fakes and helpers
live in tests/qa_scenario_support.py; EXPECTED_CASES pins this
module's contribution to the suite's case coverage so a dropped case
fails the discovery check in tests/test_qa_scenario_modules.py.
"""
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'SupersededRestartTests.test_registered_after_fenced_writer_before_failover',
    'SupersededRestartTests.test_clean_run_passes_and_validates',
    'SupersededRestartTests.test_two_runs_produce_identical_evidence',
    'SupersededRestartTests.test_evidence_names_every_leg',
    'SupersededRestartTests.test_the_pair_ends_on_its_launch_roles',
    'SupersededRestartTests.test_missing_restart_action_is_inconclusive',
    'SupersededRestartTests.test_missing_plant_endpoint_is_inconclusive',
    'SupersededRestartTests.test_missing_monitors_are_inconclusive',
    'SupersededRestartTests.test_missing_owner_tokens_are_inconclusive',
    'SupersededRestartTests.test_missing_journals_are_inconclusive',
    'SupersededRestartTests.test_unreachable_pair_is_inconclusive',
    'SupersededRestartTests.test_unsettled_pair_is_failed',
    'SupersededRestartTests.test_unwritable_model_is_inconclusive',
    'SupersededRestartTests.test_unreadable_journal_is_inconclusive',
    'SupersededRestartTests.test_refused_switchover_demote_fails',
    'SupersededRestartTests.test_refused_switchover_promote_fails',
    'SupersededRestartTests.test_stranded_demoted_peer_fails',
    'SupersededRestartTests.test_handback_demote_refused_fails',
    'SupersededRestartTests.test_handback_promote_refused_fails',
    'SupersededRestartTests.test_handback_not_settling_fails',
    'SupersededRestartTests.test_a_cleared_divergence_is_audited',
    'SupersededRestartTests.test_unjournaled_resolution_fails',
    'SupersededRestartTests.test_double_resolution_fails',
    'SupersededRestartTests.test_resolution_without_compared_points_fails',
    'SupersededRestartTests.test_resolution_on_incomplete_comparison_fails',
    'SupersededRestartTests.test_restart_lever_failure_is_inconclusive',
    'SupersededRestartTests.test_incumbent_never_promoted_fails',
    'SupersededRestartTests.test_dual_active_window_fails',
    'SupersededRestartTests.test_stalled_field_fails',
    'SupersededRestartTests.test_unanswered_window_is_inconclusive',
    'SupersededRestartTests.test_unjournaled_demotion_fails',
    'SupersededRestartTests.test_unwalked_demotion_fails',
    'SupersededRestartTests.test_skipped_demoting_fails',
    'SupersededRestartTests.test_double_claim_loss_fails',
    'SupersededRestartTests.test_restarted_preempted_peer_fails',
    'SupersededRestartTests.test_claim_not_on_the_restartee_fails',
    'SupersededRestartTests.test_unclaimed_field_fails',
    'SupersededRestartTests.test_restartee_fenced_writes_fail',
    'SupersededRestartTests.test_command_applying_on_fenced_peer_fails',
    'SupersededRestartTests.test_command_misnamed_refusal_fails',
    'SupersededRestartTests.test_refused_write_reaching_point_fails',
    'SupersededRestartTests.test_shared_token_refused_fails',
    'SupersededRestartTests.test_shared_token_naming_another_owner_fails',
    'SupersededRestartTests.test_field_opened_under_shared_token_fails',
    'SupersededRestartTests.test_superseded_token_reclaimed_field_fails',
    'SupersededRestartTests.test_restore_demote_refused_fails',
    'SupersededRestartTests.test_restore_promote_refused_fails',
})


MEMBERS = ('active', 'standby')
HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby'}
RESTARTEES_TOKEN = 0x5350A
PREEMPTED_TOKEN = 0x5350B
POINT = 10


class SupersededRestartPlant(ClaimPlantPeer):
    """The claim-enforcing plant the superseded-restart scenario fences
    through, with the writable bool in-point its command leg targets
    and the two verdict misreadings the leg must catch: a shared
    claim naming another owner, and a field opened to third-party
    mutations under a standing claim.
    """

    def __init__(self):
        super().__init__()
        self.samples[POINT] = {'value': {'bool': False},
                               'quality': 'good', 'tick': 0}
        self.directions[POINT] = 'in'
        self.shared_names_other = False

    def _respond(self, conn, request):
        answer = super()._respond(conn, request)
        if (self.shared_names_other
                and request.get('op') == 'ensure_writer'
                and answer.get('result') == 'claimed_shared'):
            answer['owner'] = PREEMPTED_TOKEN
        return answer


class SupersededRestartFeed:
    """A stubbed pair for the superseded-restart leg.

    ctrl-a is the launched active — no `--standby`, so a restart returns
    it as an active whose cold-start claim preempts the field; ctrl-b
    is the launched standby, whose promote takes the field. Every
    request on a peer is one completed scan on that peer, so the served
    ticks advance per request and every transition is call-count keyed
    rather than wall-clock: two runs on a fresh feed emit identical
    evidence.

    Each peer's `--journal-file` is a real append-only record: a
    `role_changed` entry per transition, the demoted peer's
    `divergence_detected`/`divergence_resolved` pair where the
    divergence flags stage one, the promoted peer's single
    `field_claim_lost` plus its fenced demote walk once the restartee
    preempts it, and the standby's `source_restarted` record for the
    new stream generation. Fault flags stage each named failure and
    each inconclusive the leg classifies.
    """

    def __init__(self, plant, journal_a, journal_b):
        self.plant = plant
        self.tick = 300
        self.point = False
        self.roles = {'active': 'active', 'standby': 'standby'}
        self.syncs = {'active': None, 'standby': 'tracking'}
        self.owner = 'active'
        self.down = {'active': False, 'standby': False}
        self.seq = {'active': 0, 'standby': 0}
        self.runs = {'active': 1, 'standby': 1}
        self.paths = {'active': Path(journal_a), 'standby': Path(journal_b)}
        self.receipts = []
        self.pending = []
        self.restarts = 0
        self.demotes = 0
        self.promotes = 0
        self.calls = []
        self.streams = {name: self._connect() for name in MEMBERS}
        for path in self.paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            self._append(path, {'run_boundary': {'run': 1, 'tick': 0}})
        self._plant_claim('active')
        # Fault injection — each named failure the issue calls out.
        self.never_settles = False     # the launch shape never holds
        self.stages_divergence = False  # the switchover opens a divergence
        self.signals_empty = False    # the model declares no writable bool
        self.demote_refuses = False   # every demote is refused
        self.demote_refuses_at = -1   # the nth demote (0-based) refused
        self.promote_refuses = False  # every promote is refused
        self.stranded = False         # the demoted peer never reconverges
        self.no_resolution = False    # a cleared divergence, no record
        self.double_resolution = False
        self.empty_resolution = False   # the record names no compared rows
        self.partial_resolution = False  # the record proves nothing
        self.restart_fails = False    # the restart lever raises
        self.handback_stalls = False  # the hand-back never settles the owner
        self.restore_stalls = False   # the restore never settles the owner
        self.resumed_never_owns = False  # the restartee never owns the field
        self.dual_active = False      # both peers report active together
        self.field_stalls = False     # the restartee's served tick freezes
        self.silent = False           # neither monitor answers in the window
        self.unwalked = False         # the preempted peer never demotes
        self.skips_demoting = False   # it stands down without the walk
        self.double_loss = False      # two claim-loss records land
        self.restarted_preempted = False  # the preempted peer restarted
        self.fenced_writes = False    # the restartee's own writes fence
        self.claim_drops = False      # the field goes unclaimed after the takeover
        self.command_applies = False  # the fenced peer applied the command
        self.command_misnamed = False  # the refusal named something else
        self.refused_write_lands = False

    def close(self):
        for stream in self.streams.values():
            try:
                stream.close()
            except OSError:
                pass

    # --- the plant attachment --------------------------------------

    def _connect(self):
        host, _, port = self.plant.address.rpartition(':')
        return socket.create_connection((host, int(port)), timeout=5)

    def _plant_call(self, request):
        stream = self.streams['active']
        stream.sendall(json.dumps(request).encode() + b'\n')
        line = b''
        while not line.endswith(b'\n'):
            chunk = stream.recv(65536)
            if not chunk:
                raise ConnectionError('the plant closed mid-answer')
            line += chunk
        return json.loads(line)

    def _plant_claim(self, name):
        """The controller's own unconditional claim under its pinned
        `--owner-token` — the launched active's startup claim."""
        return self._plant_call({'op': 'claim_writer',
                                 'owner': _token(name)})

    # --- the durable records ---------------------------------------

    def _append(self, path, record):
        with path.open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    def _entry(self, name, event):
        self.seq[name] += 1
        self._append(self.paths[name], {'entry': {
            'seq': self.seq[name], 'tick': self.tick, 'event': event}})

    def _served_journal(self, name):
        served = []
        for line in self.paths[name].read_text().splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if 'entry' in record:
                served.append(record['entry'])
        return served

    # --- the runner's lifecycle lever -------------------------------

    def restart_controller(self, name):
        self.calls.append(('restart_controller', name))
        self.restarts += 1
        if self.restart_fails:
            raise RuntimeError('docker stop failed: no such container')
        self.runs[name] += 1
        self.seq[name] = 0
        self._append(self.paths[name], {
            'run_boundary': {'run': self.runs[name], 'tick': self.tick}})
        if name == 'standby':
            # A launched standby rejoins as a tracker behind the owner.
            self.roles['standby'] = 'standby'
            self.syncs['standby'] = 'tracking'
            return
        # A launched active's cold-start claim preempts unconditionally:
        # the field moves to the restartee's own pinned token.
        self._plant_claim('active')
        self.owner = 'active'
        self.roles['active'] = 'active'
        self.syncs['active'] = None
        if self.claim_drops:
            self.plant.claim = None
        if self.roles['standby'] not in ('active', 'promoting'):
            return
        self.roles['standby'] = 'standby'
        self.syncs['standby'] = 'tracking'
        if self.restarted_preempted:
            self.runs['standby'] += 1
            self.seq['standby'] = 0
            self._append(self.paths['standby'], {'run_boundary': {
                'run': self.runs['standby'], 'tick': self.tick}})
        if self.skips_demoting:
            self._entry('standby', {'role_changed': {
                'from': 'active', 'to': 'standby', 'origin': 'fenced'}})
        elif not self.unwalked:
            self._entry('standby', {'role_changed': {
                'from': 'active', 'to': 'demoting',
                'origin': 'fenced'}})
            self._entry('standby', {'role_changed': {
                'from': 'demoting', 'to': 'standby',
                'origin': 'fenced'}})
        for index in range(2 if self.double_loss else 1):
            self._entry('standby', {'field_claim_lost': {
                'point': 200, 'claimant': RESTARTEES_TOKEN,
                'at': index}})

    # --- the monitor channel ---------------------------------------

    def _advance(self):
        if self.silent or not self.field_stalls:
            self.tick += 1
        self._settle()

    def _settle(self):
        for receipt in self.pending:
            write = receipt['command']['write_value']
            receipt['outcome'] = {'applied': {'tick': self.tick}}
            new = write['value']['bool']
            if new != self.point:
                self.point = new
            self._entry(self.owner, {'command_settled': {
                'receipt': receipt}})
        self.pending = []

    def _report(self, name):
        report = {'role': self.roles[name], 'tick': self.tick}
        if self.never_settles:
            report['role'] = 'demoting'
        if self.resumed_never_owns and self.restarts and name == 'active':
            report['role'] = 'standby'
            report['sync'] = {'tracking': {}}
        if self.roles[name] == 'standby':
            if name == 'active' and self.stranded:
                report['sync'] = 'unsynchronized'
            else:
                report['sync'] = {self.syncs[name] or 'tracking': {}}
        if self.dual_active and self.restarts:
            report['role'] = 'active'
        return report

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://pair', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    def _health(self):
        return {'failed_writes': 1
                if self.fenced_writes and self.restarts else 0}

    def _snapshot(self):
        return {'tick': self.tick, 'io_health': self._health(),
                'points': [{'point': POINT, 'direction': 'in',
                            'sample': {'value': {'bool': self.point},
                                       'quality': 'good'}}]}

    def _demote(self, name):
        self.roles[name] = 'standby'
        self.syncs[name] = 'tracking'
        if self.owner == name:
            self.owner = 'standby'
        self._entry(name, {'role_changed': {
            'from': 'active', 'to': 'demoting', 'origin': 'request'}})
        self._entry(name, {'role_changed': {
            'from': 'demoting', 'to': 'standby', 'origin': 'request'}})
        self._entry(name, {'role_changed': {
            'from': 'demoting', 'to': 'standby', 'origin': 'request'}})
        self._entry(name, {'role_changed': {
            'from': 'demoting', 'to': 'standby', 'origin': 'request'}})

    def _promote(self, name):
        if self.roles[name] == 'active':
            self._raise(409, 'already_active')
        if self.promote_refuses:
            self._raise(409, {'not_converged': {'sync': 'tracking'}})
        self.promotes += 1
        self._plant_call({'op': 'claim_writer', 'owner': _token(name)})
        self.roles[name] = 'active'
        self.syncs[name] = None
        self.owner = name
        if (self.handback_stalls and self.promotes >= 2
                or self.restore_stalls and self.promotes >= 5) \
                and name == 'active':
            self.roles['active'] = 'standby'
            self.syncs['active'] = 'tracking'
            self.owner = 'standby'
            self._entry(name, {'role_changed': {
                'from': 'promoting', 'to': 'active', 'origin': 'request'}})
            return {'role': 'promoting'}
        self._entry(name, {'role_changed': {
            'from': 'standby', 'to': 'promoting', 'origin': 'request'}})
        if name == 'standby':
            # The demoted peer walks back to a converged verdict on the
            # new stream, journaling its source-restart record.
            self.roles['active'] = 'standby'
            self.syncs['active'] = 'tracking'
            self.owner = 'standby'
            self._entry('active', {'role_changed': {
                'from': 'demoting', 'to': 'standby', 'origin': 'request'}})
            self._divergence_events()
            self._entry('standby', {'source_restarted': {
                'was_aligned': self.tick, 'resumed_at': 0}})
        else:
            self._entry(name, {'role_changed': {
                'from': 'promoting', 'to': 'active', 'origin': 'request'}})
            self._divergence_events()
        return {'role': 'promoting'}

    def _divergence_events(self):
        """The demoted peer's divergence detection and the resolution
        that returns it to `tracking`, staged on the switch the leg
        drives."""
        staged = (self.stages_divergence or self.no_resolution
                  or self.double_resolution or self.empty_resolution
                  or self.partial_resolution)
        if not staged:
            return
        self._entry('active', {'divergence_detected': {'mismatches': [
            {'point': POINT, 'staged': True, 'field': False}]}})
        if self.no_resolution:
            return
        rows = [] if self.empty_resolution else [
            {'point': POINT, 'staged': True,
             'field': False if self.partial_resolution else True}]
        for _ in range(2 if self.double_resolution else 1):
            self._entry('active', {'divergence_resolved': {
                'compared': rows}})

    def _serve(self, name, method, route, url, body):
        if (method, route) == ('GET', '/role'):
            return 200, self._report(name)
        if (method, route) == ('GET', '/signals'):
            if self.signals_empty:
                return 200, {'points': []}
            return 200, {'points': [
                {'point': POINT, 'name': 'p101-oos', 'direction': 'in',
                 'value_type': 'bool', 'writable': True}]}
        if (method, route) == ('GET', '/snapshot'):
            return 200, self._snapshot()
        if (method, route) == ('GET', '/journal'):
            return 200, self._served_journal(name)
        if (method, route) == ('GET', '/checkpoint'):
            return 200, {
                'tracking_source': '10.9.9.2:8081' if name == 'active'
                else None,
                'source_owns_field': self.owner == name}
        if (method, route) == ('GET', '/receipts'):
            return 200, [dict(receipt) for receipt in self.receipts]
        if (method, route) == ('POST', '/command'):
            if name == 'standby':
                return self._standby_command(body)
            if self.roles[name] != 'active':
                self._raise(409, {'not_active': {}})
            receipt = {'command': (body or {}).get('command'),
                       'actor': (body or {}).get('actor'),
                       'outcome': {'accepted': {
                           'apply_tick': self.tick + 1}}}
            self.pending.append(receipt)
            self.receipts.append(receipt)
            return 200, dict(receipt)
        if (method, route) == ('POST', '/demote'):
            if (self.roles[name] != 'active' or self.demote_refuses
                    or self.demotes == self.demote_refuses_at):
                self._raise(409, {'not_active': {}})
            self.demotes += 1
            self._demote(name)
            return 200, {'role': 'demoting'}
        if (method, route) == ('POST', '/promote'):
            return 200, self._promote(name)
        raise AssertionError('unexpected request %s %s' % (method, url))

    def _standby_command(self, body):
        """The fenced peer's receipted path: the named `not_active`
        admission refusal, never an application."""
        if self.command_applies:
            receipt = {'command': (body or {}).get('command'),
                       'actor': (body or {}).get('actor'),
                       'outcome': {'accepted': {
                           'apply_tick': self.tick + 1}}}
            self.pending.append(receipt)
            self.receipts.append(receipt)
            return 200, dict(receipt)
        if self.refused_write_lands:
            self.point = (body or {})['command']['write_value'][
                'value']['bool']
        reason = 'command_full' if self.command_misnamed else 'not_active'
        return 200, {'command': (body or {}).get('command'),
                     'actor': (body or {}).get('actor'),
                     'outcome': {'rejected': {'reason': {
                         reason: {'point': POINT}}}}}

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        name = HOSTS.get(host)
        if name is None:
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if self.silent:
            raise urllib.error.URLError('connection refused')
        self._advance()
        return self._serve(name, method, route, url, body)


def _token(name):
    """The peer's pinned `--owner-token` — the launched active's, the
    launched standby's."""
    return RESTARTEES_TOKEN if name == 'active' else PREEMPTED_TOKEN


class SupersededRestartTests(unittest.TestCase):
    """scenario_superseded_restart against the stubbed pair and the
    claim-enforcing plant: the demote-reconvergence, divergence
    resolution, hand-back, superseded-restart, one-active window,
    demote-in-place, fenced command, and shared-owner-token legs —
    plus each named failure and inconclusive induction the leg
    classifies."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_a = Path(self.tmp.name) / 'controllers' / 'a' \
            / 'journal.jsonl'
        self.journal_a.parent.mkdir(parents=True)
        self.journal_b = Path(self.tmp.name) / 'controllers' / 'b' \
            / 'journal.jsonl'
        self.journal_b.parent.mkdir(parents=True)
        self.plant = SupersededRestartPlant()
        self.addCleanup(self.plant.close)
        self.feed = SupersededRestartFeed(self.plant, self.journal_a,
                                          self.journal_b)
        self.addCleanup(self.feed.close)

    def _ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'plant': self.plant.address,
               'plant_owner': {'active': RESTARTEES_TOKEN,
                               'standby': PREEMPTED_TOKEN},
               'restart_controller': feed.restart_controller,
               'journal_files': {'active': str(feed.paths['active']),
                                 'standby': str(feed.paths['standby'])},
               'evidence_dir': str(self.evidence)}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        defaults = {'POLL_INTERVAL': 0.001, 'RESTART_POLL': 0.001,
                    'RESTART_RECONVERGE': 0.5,
                    'RESTART_WINDOW': 0.5,
                    'RESTART_SETTLE_DEADLINE': 0.5,
                    'RESTART_FIELD_TICKS': 2}
        with patch.object(scenarios, 'http_json', feed.http_json):
            for key, value in defaults.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            return scenarios.scenario_superseded_restart(
                ctx or self._ctx(feed))

    def _read(self, ref):
        return json.loads((self.evidence.parent / ref).read_text())

    def test_registered_after_fenced_writer_before_failover(self):
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_fenced_writer_degrade),
            order.index(scenarios.scenario_superseded_restart))
        self.assertLess(
            order.index(scenarios.scenario_superseded_restart),
            order.index(scenarios.scenario_failover))

    def test_clean_run_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        self.assertEqual(self.feed.restarts, 1)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes() for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        root = Path(second_tmp.name)
        evidence2 = root / 'evidence'
        evidence2.mkdir()
        plant2 = SupersededRestartPlant()
        self.addCleanup(plant2.close)
        feed2 = SupersededRestartFeed(
            plant2,
            root / 'controllers' / 'a' / 'journal.jsonl',
            root / 'controllers' / 'b' / 'journal.jsonl')
        self.addCleanup(feed2.close)
        ctx2 = self._ctx(feed2, evidence_dir=str(evidence2))
        record2 = self.run_scenario(ctx2, feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_evidence_names_every_leg(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        refs = [entry['ref'] for entry in record['evidence']]
        for name in ('reconverge', 'divergence', 'window', 'journal',
                     'field', 'command', 'token', 'signals'):
            self.assertTrue(any(name in ref for ref in refs), name)
        window = self._read('evidence/superseded-restart-window.json')
        self.assertTrue(window['polls'])
        self.assertEqual([len(poll['actives'])
                          for poll in window['polls']],
                         [1] * len(window['polls']))
        self.assertGreaterEqual(len(window['field_ticks']), 2)
        journal = self._read('evidence/superseded-restart-journal.json')
        self.assertEqual(journal['walk'],
                         [['active', 'demoting'], ['demoting', 'standby']])
        self.assertEqual(journal['losses'], 1)
        token = self._read('evidence/superseded-restart-token.json')
        self.assertEqual(token['shared']['result'], 'claimed_shared')
        self.assertEqual(token['shared']['owner'], RESTARTEES_TOKEN)
        divergence = self._read(
            'evidence/superseded-restart-divergence.json')
        # A clean switchover opens no divergence, so the audit records
        # none; the staged legs below carry the resolution.
        self.assertEqual(divergence['detected'], [])
        self.assertEqual(divergence['resolved'], [])

    def test_the_pair_ends_on_its_launch_roles(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.roles['active'], 'active')
        self.assertEqual(self.feed.roles['standby'], 'standby')
        self.assertEqual(self.feed.owner, 'active')

    # --- the inconclusive inductions -------------------------------

    def test_missing_restart_action_is_inconclusive(self):
        record = self.run_scenario(self._ctx(restart_controller=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('restart action', record['detail'])

    def test_missing_plant_endpoint_is_inconclusive(self):
        record = self.run_scenario(self._ctx(plant=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('plant endpoint', record['detail'])

    def test_missing_monitors_are_inconclusive(self):
        record = self.run_scenario(self._ctx(standby=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('monitor', record['detail'])

    def test_missing_owner_tokens_are_inconclusive(self):
        record = self.run_scenario(self._ctx(plant_owner={}))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('owner tokens', record['detail'])

    def test_missing_journals_are_inconclusive(self):
        record = self.run_scenario(self._ctx(journal_files={}))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal-file', record['detail'])

    def test_unreachable_pair_is_inconclusive(self):
        self.feed.silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])

    def test_unsettled_pair_is_failed(self):
        self.feed.never_settles = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('launch shape', record['detail'])

    def test_unwritable_model_is_inconclusive(self):
        self.feed.signals_empty = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('writable bool', record['detail'])

    def test_unreadable_journal_is_inconclusive(self):
        self.journal_a.write_text('not a journal record\n')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreadable', record['detail'])

    # --- the switchover and the reconvergence ----------------------

    def test_refused_switchover_demote_fails(self):
        self.feed.demote_refuses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('switchover demote', record['detail'])

    def test_refused_switchover_promote_fails(self):
        self.feed.promote_refuses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('switchover promote', record['detail'])

    def test_stranded_demoted_peer_fails(self):
        self.feed.stranded = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never reconverged', record['detail'])
        self.assertIn('strands', record['detail'])

    def test_handback_demote_refused_fails(self):
        # The hand-back's demote — the second of the leg's three.
        self.feed.demote_refuses_at = 1
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('hand-back', record['detail'])

    def test_handback_promote_refused_fails(self):
        self.feed.promote_refuses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('switchover promote', record['detail'])

    def test_handback_not_settling_fails(self):
        self.feed.handback_stalls = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('hand-back switch', record['detail'])

    # --- the divergence leg ----------------------------------------

    def test_a_cleared_divergence_is_audited(self):
        self.feed.partial_resolution = False
        self.feed.empty_resolution = False
        self.feed.double_resolution = False
        self.feed.no_resolution = False
        self.feed.stages_divergence = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        divergence = self._read(
            'evidence/superseded-restart-divergence.json')
        self.assertEqual(len(divergence['detected']), 1)
        self.assertEqual(len(divergence['resolved']), 1)
        self.assertEqual(divergence['resolved'][0]['compared'],
                         [{'point': POINT, 'staged': True,
                           'field': True}])

    def test_unjournaled_resolution_fails(self):
        self.feed.no_resolution = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('divergence_resolved records', record['detail'])

    def test_double_resolution_fails(self):
        self.feed.double_resolution = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('divergence_resolved records', record['detail'])

    def test_resolution_without_compared_points_fails(self):
        self.feed.empty_resolution = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('names no compared', record['detail'])

    def test_resolution_on_incomplete_comparison_fails(self):
        self.feed.partial_resolution = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('incomplete', record['detail'])

    # --- the restart induction -------------------------------------

    def test_restart_lever_failure_is_inconclusive(self):
        self.feed.restart_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('restart never completed', record['detail'])

    def test_incumbent_never_promoted_fails(self):
        self.feed.resumed_never_owns = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('advanced its served field', record['detail'])

    # --- the one-active window -------------------------------------

    def test_dual_active_window_fails(self):
        self.feed.dual_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('two peers reporting active', record['detail'])

    def test_stalled_field_fails(self):
        self.feed.field_stalls = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('advanced its served field', record['detail'])

    def test_unanswered_window_is_inconclusive(self):
        self.feed.silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)

    # --- the demote-in-place leg -----------------------------------

    def test_unjournaled_demotion_fails(self):
        self.feed.unwalked = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role transition', record['detail'])

    def test_unwalked_demotion_fails(self):
        self.feed.skips_demoting = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role walk', record['detail'])

    def test_skipped_demoting_fails(self):
        self.feed.skips_demoting = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)

    def test_double_claim_loss_fails(self):
        self.feed.double_loss = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('field_claim_lost records', record['detail'])

    def test_restarted_preempted_peer_fails(self):
        self.feed.restarted_preempted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('process lifetime', record['detail'])

    # --- the field's own account -----------------------------------

    def test_claim_not_on_the_restartee_fails(self):
        # The field's claim released behind the takeover: the preempted
        # owner's own token finds the field unclaimed and its writes
        # reach the field again.
        self.feed.claim_drops = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('still reach the field', record['detail'])

    def test_unclaimed_field_fails(self):
        self.feed.claim_drops = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)

    def test_restartee_fenced_writes_fail(self):
        self.feed.fenced_writes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('io_health moved', record['detail'])

    # --- the fenced command path -----------------------------------

    def test_command_applying_on_fenced_peer_fails(self):
        self.feed.command_applies = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not_active', record['detail'])

    def test_command_misnamed_refusal_fails(self):
        self.feed.command_misnamed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not_active', record['detail'])

    def test_refused_write_reaching_point_fails(self):
        self.feed.refused_write_lands = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('untouched', record['detail'])

    # --- the shared-owner-token settlement -------------------------

    def test_shared_token_refused_fails(self):
        self.plant.refuse_ensure = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('several attachments share', record['detail'])

    def test_shared_token_naming_another_owner_fails(self):
        self.plant.shared_names_other = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('several attachments share', record['detail'])

    def test_field_opened_under_shared_token_fails(self):
        self.plant.open_field = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('went unclaimed', record['detail'])

    def test_superseded_token_reclaimed_field_fails(self):
        self.feed.claim_drops = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('still reach the field', record['detail'])

    # --- the restore -----------------------------------------------

    def test_restore_demote_refused_fails(self):
        self.feed.demote_refuses_at = 4
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('restore switch', record['detail'])

    def test_restore_promote_refused_fails(self):
        self.feed.restore_stalls = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('launch roles', record['detail'])


if __name__ == '__main__':
    unittest.main()