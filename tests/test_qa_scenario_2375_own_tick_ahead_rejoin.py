"""The 2375_own_tick_ahead_rejoin leg's scenario unit coverage — the
feed fake and TestCase class for scenario_own_tick_ahead_rejoin,
split out per the leg-module convention (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES pins this
module's contribution to the suite's case coverage so a dropped case
fails the discovery check in tests/test_qa_scenario_modules.py.

The feed stages the leg's shape on the deployed pair: ctrl-a owns the
field declaring no configured source at all, ctrl-b tracks it through
its `--standby` pull, and the field is a real ClaimPlantPeer so the
leg reads its own claim arbitration — the standing owner and the
declared monitor on every fencing verdict. The lifecycle seam stages
the ahead-bound lead on the rig itself: `stop_controller` holds the
owner's container down while the tracker keeps pacing, so the
tracker's own run tick advances a scan at a time against a frozen
line. Every promotion really takes the claim: the prior holder's next
scan meets the fence, journals its attributed `field_claim_lost` and
the fenced-origin demote walk, and — the contract under test — the
source-less ex-owner then resolves the standing claim's declared
monitor, journals one `tracking_source_adopted` naming it, and
converges. By default every fault flag stages exactly one named
defect, one nondeterministic surface, or one pre-contract rig shape
the leg declines on."""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'OwnTickAheadRejoinTests.test_registered',
    'OwnTickAheadRejoinTests.test_clean_passes_produce_identical_digests',
    'OwnTickAheadRejoinTests.test_clean_pass_records_the_staging_and_'
    'the_rejoin',
    'OwnTickAheadRejoinTests.test_rejoin_asserted_while_stranded_fails',
    'OwnTickAheadRejoinTests.test_rejoin_past_the_documented_bound_fails',
    'OwnTickAheadRejoinTests.test_adoption_missing_fails',
    'OwnTickAheadRejoinTests.test_adoption_duplicated_fails',
    'OwnTickAheadRejoinTests.test_adoption_foreign_fails',
    'OwnTickAheadRejoinTests.test_source_refusal_journaled_fails',
    'OwnTickAheadRejoinTests.test_restart_performed_the_rejoin_fails',
    'OwnTickAheadRejoinTests.test_served_document_foreign_fails',
    'OwnTickAheadRejoinTests.test_served_document_misnamed_fails',
    'OwnTickAheadRejoinTests.test_loss_silent_fails',
    'OwnTickAheadRejoinTests.test_loss_duplicated_fails',
    'OwnTickAheadRejoinTests.test_loss_unattributed_fails',
    'OwnTickAheadRejoinTests.test_demotion_unwalked_fails',
    'OwnTickAheadRejoinTests.test_demotion_held_fails',
    'OwnTickAheadRejoinTests.test_demotion_unexpected_walk_fails',
    'OwnTickAheadRejoinTests.test_successor_inactive_fails',
    'OwnTickAheadRejoinTests.test_verdict_open_fails',
    'OwnTickAheadRejoinTests.test_verdict_foreign_fails',
    'OwnTickAheadRejoinTests.test_verdict_undeclared_fails',
    'OwnTickAheadRejoinTests.test_verdict_misnamed_fails',
    'OwnTickAheadRejoinTests.test_stream_position_ahead_of_its_tick_'
    'fails',
    'OwnTickAheadRejoinTests.test_restore_refused_fails',
    'OwnTickAheadRejoinTests.test_layout_unrestored_fails',
    'OwnTickAheadRejoinTests.test_launch_roles_lost_after_the_pass_fails',
    'OwnTickAheadRejoinTests.test_staging_lever_refused_is_'
    'nondeterministic',
    'OwnTickAheadRejoinTests.test_lead_never_separated_is_nondeterministic',
    'OwnTickAheadRejoinTests.test_lead_inside_the_bound_is_nondeterministic',
    'OwnTickAheadRejoinTests.test_pair_never_reconverged_is_'
    'nondeterministic',
    'OwnTickAheadRejoinTests.test_successor_not_converged_at_promote_is_'
    'nondeterministic',
    'OwnTickAheadRejoinTests.test_promote_answered_by_the_gate_is_'
    'nondeterministic',
    'OwnTickAheadRejoinTests.test_promote_unanswered_is_nondeterministic',
    'OwnTickAheadRejoinTests.test_durable_sink_unreadable_is_'
    'nondeterministic',
    'OwnTickAheadRejoinTests.test_rejoin_watch_starved_is_nondeterministic',
    'OwnTickAheadRejoinTests.test_field_verdict_unreadable_is_'
    'nondeterministic',
    'OwnTickAheadRejoinTests.test_missing_seams_report_inconclusive',
    'OwnTickAheadRejoinTests.test_unreachable_pair_reports_inconclusive',
    'OwnTickAheadRejoinTests.test_swapped_launch_layout_is_reseated',
    'OwnTickAheadRejoinTests.test_unrecoverable_layout_reports_'
    'inconclusive',
    'OwnTickAheadRejoinTests.test_missing_stamps_report_inconclusive',
    'OwnTickAheadRejoinTests.test_missing_sync_vocabulary_reports_'
    'inconclusive',
    'OwnTickAheadRejoinTests.test_verdict_without_owner_reports_'
    'inconclusive',
    'OwnTickAheadRejoinTests.test_verdict_without_monitor_reports_'
    'inconclusive',
    'OwnTickAheadRejoinTests.test_lead_carrying_document_without_stream_'
    'reports_inconclusive',
    'OwnTickAheadRejoinTests.test_diverging_digests_report_nondeterministic',
    'OwnTickAheadRejoinTests.test_unchecked_self_check_fails',
    'OwnTickAheadRejoinTests.test_self_check_is_complete',
})


OWNER = 'active'
PEER = 'standby'
HOSTS = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}
PORTS = {'a': 8080, 'b': 8081}
MONITORS = {'a': '172.20.0.4:8080', 'b': '172.20.0.5:8081'}
TOKENS = {'a': 424243, 'b': 424244}
# The tracker's own pulls before it resolves the standing claim's
# declared monitor — the bounded retry the contract's rendezvous rides.
ADOPT_POLLS = 3


class AheadRejoinFeed:
    """A stubbed deployed pair and claim-arbitrating field for the
    own-tick-ahead rejoin leg.

    ctrl-a (`active`) is the field owner declaring no configured
    tracking source at all — the member whose re-join can only resolve
    the claim's declared monitor — and ctrl-b (`standby`) tracks it
    through its configured `--standby` pull. Each `/role` read is one
    completed scan, so a run tick advances a scan at a time; while the
    owner is stopped its container is down, the tracker keeps pacing,
    and the lead the promotion carries is accrued on the rig itself.
    Every promotion takes the claim: the prior holder's next scan meets
    the fence, journals its attributed loss and its fenced-origin walk,
    and then — the contract under test — the source-less ex-owner
    resolves the standing claim's declared monitor, journals one
    adoption naming it, and converges. The fault flags stage each
    named defect, each nondeterministic surface, and each pre-contract
    rig shape the leg declines on."""

    def __init__(self, plant, journals):
        self.plant = plant
        self.journals = journals          # {endpoint: path} the runner owns
        self.tick = {'a': 100, 'b': 100}
        self.up = {'a': True, 'b': True}
        self.role = {'a': 'active', 'b': 'standby'}
        self.sync = {'a': None, 'b': 'tracking'}
        self.claim = 'a'                  # the peer holding the field
        self.misses = 0                   # the tracker's pull-miss counter
        self.budget = 120
        self.adopt_pending = 0            # pulls left before the adoption
        self.adopted = set()              # ports this run already pinned
        self.seq = {'a': 0, 'b': 0}
        self.served = {'a': [], 'b': []}
        self.calls = []
        for path in journals.values():
            Path(path).write_text('')
        self._push('a', 'run_boundary', {'run': 1})
        self._push('b', 'run_boundary', {'run': 1})
        if self.swapped:
            self._stand('b')
            self.role = {'a': 'standby', 'b': 'active'}
            self.sync = {'a': 'tracking', 'b': None}
        else:
            self._stand('a')
        # Doctors staging each named defect.
        self.strand = False               # the ex-owner never re-joins
        self.adoption_missing = False     # converges with no adoption
        self.adoption_twice = False       # the adoption journals twice
        self.adoption_foreign = False     # the adoption names a stranger
        self.refusal_journaled = False    # a refusal naming the successor
        self.restart_rejoined = False     # a restart performed the rejoin
        self.document_foreign = False     # the re-joined stamp lies
        self.document_misnamed = False    # the line owner names a stranger
        self.loss_missing = False         # the fence fired, silent
        self.loss_twice = False           # one loss per fenced write
        self.loss_unattributed = False    # the loss names no claimant
        self.walk_missing = False         # no fenced demote walk
        self.walk_unexpected = False      # the walk is not the fenced pair
        self.demotion_held = False        # the ex-owner never stands down
        self.successor_inactive = False   # the promoted peer never active
        self.verdict_open = False         # the field fails open
        self.verdict_foreign = False      # the verdict names another owner
        self.verdict_undeclared = False   # the claim declares no monitor
        self.verdict_misnamed = False     # the claim names another port
        self.verdict_unreadable = False   # the probe never answers
        self.stream_nonsense = False      # a position ahead of its tick
        self.stream_absent = False        # no declared stream position
        self.stamps_absent = False        # no ownership stamps at all
        self.sync_absent = False          # no sync vocabulary
        self.restore_refused = False      # the switch back is refused
        self.layout_unrestored = False    # the pair never returns
        self.layout_lost = False          # the roles move after the pass
        # Doctors staging each nondeterministic surface.
        self.stop_refused = False         # the lifecycle stop is refused
        self.start_refused = False        # the lifecycle restart is refused
        self.no_lead = False              # the outage accrues no lead
        self.never_reconverged = False    # the pair never reconverges
        self.posture_degraded = False     # the tracker drops before promote
        self.promote_gated = False        # the gate answers the promote
        self.promote_unanswered = False   # the post goes nowhere
        self.durable_gone = False         # the durable sinks vanish
        self.watch_starved = False        # the re-join watch goes quiet
        self.silent = False               # every monitor endpoint refuses

    # ---- the runner-owned lifecycle actions -------------------------

    def stop_controller(self, name):
        self.calls.append(('stop_controller', name))
        if self.stop_refused:
            raise RuntimeError('docker stop failed: refused')
        self.up[{'active': 'a', 'standby': 'b'}[name]] = False

    def start_controller(self, name):
        self.calls.append(('start_controller', name))
        if self.start_refused:
            raise RuntimeError('docker start failed: refused')
        peer = {'active': 'a', 'standby': 'b'}[name]
        self.up[peer] = True
        self._push(peer, 'run_boundary', {'run': self.boundaries(peer) + 1})

    def boundaries(self, peer):
        records = self._records(peer)
        return sum(1 for item in records if 'run_boundary' in item)

    # ---- the durable journal ---------------------------------------

    def _records(self, peer):
        path = self.journals.get({'a': OWNER, 'b': PEER}[peer])
        if not path or not Path(path).is_file():
            return []
        out = []
        for line in Path(path).read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def _push(self, peer, kind, body):
        self.seq[peer] += 1
        entry = {'seq': self.seq[peer], 'tick': self.tick[peer],
                 'event': {kind: body}}
        self.served[peer].append(entry)
        path = self.journals.get({'a': OWNER, 'b': PEER}[peer])
        if path:
            with Path(path).open('a') as handle:
                handle.write(json.dumps({'entry': entry}) + '\n')

    def _events(self, peer, kind):
        return [entry['event'][kind] for entry in self.served[peer]
                if kind in entry['event']]

    # ---- the field's own claim arbitration -------------------------

    def _stand(self, peer):
        """The standing claim the plant server holds: this peer's own
        owner token and the monitor it declared. Written straight onto
        the real claim-arbitrating peer so the leg's fencing probe
        reads the shipped verdict shape."""
        self.claim = peer
        self.plant.claim = {'owner': TOKENS[peer], 'holders': {0},
                            'monitor': MONITORS[peer]}

    # ---- the tracking model ----------------------------------------

    def _pull(self, peer):
        """One tracking pull. `b` pulls its configured `--standby`
        source — a produced-nothing miss while the owner is down, a
        clean apply and a `tracking` verdict while it is up. `a`
        declares no source at all, so its only candidate is the
        standing claim's declared monitor, resolved once the bounded
        retry comes due: that adoption journals `tracking_source_
        adopted` naming it and converges `a` onto the line."""
        if peer == 'b':
            if not self.up['a'] or self.never_reconverged \
                    or self.sticky_degraded:
                self.misses += 1
                self.sync['b'] = 'degraded'
                if self.failover_fires and self.misses >= 3:
                    self._promote_applied('b')
                return
            self.misses = 0
            self.sync['b'] = 'tracking'
            return
        # The source-less ex-owner: nothing but the claim's declaration.
        if self.role['a'] != 'standby' or self.claim == 'a' \
                or self.strand:
            return
        if self.adopt_pending > 0:
            self.adopt_pending -= 1
            return
        if PORTS['b'] in self.adopted and not self.refusal_journaled:
            return
        if not self.adopted:
            if self.refusal_journaled:
                self._push('a', 'tracking_source_refused',
                           {'source': MONITORS['b'],
                            'detail': "the pulled document's stream "
                                      'position leads the line'})
            self.adopted.add(PORTS['b'])
            if self.restart_rejoined:
                self._push('a', 'run_boundary',
                           {'run': self.boundaries('a') + 1})
            if not self.adoption_missing:
                self._push('a', 'tracking_source_adopted',
                           {'source': MONITORS['b']})
                if self.adoption_twice:
                    self._push('a', 'tracking_source_adopted',
                               {'source': MONITORS['b']})
            if self.adoption_foreign:
                self._push('a', 'tracking_source_adopted',
                           {'source': '172.20.0.9:8082'})
        self.sync['a'] = 'tracking'

    def _scan(self, peer):
        """One completed scan on `peer`: its own run tick advances, role
        transitions settle at the boundary, a superseded owner's write
        meets the fence, and the standby pulls."""
        if not (self.no_lead and not self.up['a']):
            self.tick[peer] += 1
        if self.role[peer] == 'promoting':
            self.role[peer] = 'active'
            self.sync[peer] = None
        elif self.role[peer] == 'demoting':
            self.role[peer] = 'standby'
            self.sync[peer] = 'unsynchronized'
            self.adopt_pending = ADOPT_POLLS
            if not self.walk_missing:
                self._push(peer, 'role_changed',
                           {'from': 'demoting', 'to': 'standby',
                            'origin': 'fenced'})
        if self.role[peer] == 'active' and self.claim != peer \
                and not self.demotion_held:
            # The superseded owner's first fenced write demotes it in
            # place under the field-arbitration origin.
            if not self.loss_missing and 'field_claim_lost' not in \
                    [kind for entry in self.served[peer] for kind in
                     entry['event']]:
                body = {'point': 20,
                        'claimant': TOKENS[self.claim]
                        if not self.loss_unattributed else None}
                self._push(peer, 'field_claim_lost', body)
                if self.loss_twice:
                    self._push(peer, 'field_claim_lost', body)
            if not self.walk_missing:
                walk = ([('active', 'starting')] if self.walk_unexpected
                        else [('active', 'demoting')])
                self._push(peer, 'role_changed',
                           {'from': walk[0][0], 'to': walk[0][1],
                            'origin': 'fenced'})
            self.role[peer] = 'demoting'
            self.sync[peer] = 'unsynchronized'
            self.adopt_pending = ADOPT_POLLS
        elif self.role[peer] == 'standby':
            self._pull(peer)

    # ---- the control plane -----------------------------------------

    def _promote_applied(self, peer):
        self._stand(peer)

    def _promote(self, peer):
        if self.promote_unanswered:
            raise urllib.error.URLError('connection reset')
        if self.role[peer] in ('active', 'promoting'):
            return 409, {'already_active': {'role': 'active'}}
        if self.role[peer] == 'standby' and self.sync[peer] != 'tracking':
            return 409, {'not_converged': {'sync': self.sync[peer]}}
        if self.promote_gated:
            return 409, {'not_converged': {'sync': 'tracking'}}
        if peer == OWNER and self.restore_refused:
            return 409, {'not_converged': {'sync': 'tracking'}}
        self.role[peer] = 'promoting'
        self._promote_applied(peer)
        if peer == PEER:
            self._push(peer, 'role_changed',
                       {'from': 'standby', 'to': 'promoting',
                        'origin': 'request'})
            if self.durable_gone:
                for path in self.journals.values():
                    Path(path).unlink(missing_ok=True)
        return 200, {'role': 'promoting', 'tick': self.tick[peer]}

    def _demote(self, peer):
        if self.role[peer] != 'active':
            return 409, {'not_active': {'role': self.role[peer]}}
        self.role[peer] = 'demoting'
        self._push(peer, 'role_changed',
                   {'from': 'active', 'to': 'demoting',
                    'origin': 'request'})
        return 200, {'role': 'demoting', 'tick': self.tick[peer]}

    # ---- the endpoint dispatch -------------------------------------

    def _report(self, peer):
        report = {'role': self.role[peer], 'tick': self.tick[peer]}
        if self.role[peer] == 'standby':
            sync = self.sync[peer]
            if self.sync_absent:
                pass
            elif sync in ('tracking', 'orphaned'):
                report['sync'] = {sync: {'aligned': self.tick[
                    'b' if peer == 'b' else 'a']}}
            elif sync == 'degraded':
                report['sync'] = {'degraded': {'detail': 'timed out'}}
            else:
                report['sync'] = sync or 'unsynchronized'
        if peer == PEER and self.role[PEER[1]] == 'standby':
            report['failover'] = {'converged': self.sync['b']
                                  == 'tracking',
                                  'misses': self.misses,
                                  'budget': self.budget}
        return report

    def _checkpoint(self, peer):
        if self.stamps_absent:
            return {'tick': self.tick[peer], 'generation': 7}
        document = {'tick': self.tick[peer], 'generation': 7,
                    'source_owns_field': self.role[peer] == 'active',
                    'line_owner': MONITORS[self.claim]}
        if not self.stream_absent:
            document['stream_tick'] = self.tick[peer] - (
                1 if peer != 'a' or self.role['a'] == 'active' else 0)
            if self.stream_nonsense:
                document['stream_tick'] = self.tick[peer] + 1
        if self.document_foreign and peer == 'a' \
                and self.role['a'] == 'standby':
            document['source_owns_field'] = True
        if self.document_misnamed and peer == 'a' \
                and self.role['a'] == 'standby':
            document['line_owner'] = '172.20.0.9:8082'
        if self.posture_degraded and peer == 'b':
            self.sticky_degraded = True
        return document

    def http_json(self, method, url, body=None, timeout=10):
        if self.silent:
            raise urllib.error.URLError('connection refused')
        peer = HOSTS.get(url.split('/')[2])
        if peer is None or not self.up[peer]:
            raise urllib.error.URLError('connection refused')
        route, _, query = ('/' + url.split('/', 3)[3]).partition('?')
        if peer == 'a' and self.watch_starved \
                and self.role['a'] == 'standby' and self.claim != 'a':
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            self._scan(peer)
            return 200, self._report(peer)
        if (method, route) == ('GET', '/checkpoint'):
            return 200, self._checkpoint(peer)
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if query else 0
            return 200, [entry for entry in self.served[peer]
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/promote'):
            status, answer = self._promote(peer)
            if status == 200:
                return status, answer
            raise urllib.error.HTTPError(
                url, status, 'conflict', {},
                io.BytesIO(json.dumps(answer).encode()))
        if (method, route) == ('POST', '/demote'):
            status, answer = self._demote(peer)
            if status == 200:
                return status, answer
            raise urllib.error.HTTPError(
                url, status, 'conflict', {},
                io.BytesIO(json.dumps(answer).encode()))
        raise AssertionError('unexpected request %s %s' % (method, url))


class OwnTickAheadRejoinTests(unittest.TestCase):
    """scenario_own_tick_ahead_rejoin against the stubbed rig: the
    staged outage leaves the successor's own run tick past the retired
    ahead bound, the routine promote supersedes the claim, and the
    source-less ex-owner re-joins through the claim's declared monitor
    — converged, adopted once by name, promoted back — two passes,
    identical digests. Each fault flag stages a named failure, a
    nondeterministic surface, or a pre-contract rig shape the leg
    declines on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.evidence = self.root / 'evidence'
        self.evidence.mkdir()
        self.plant = ClaimPlantPeer()
        self.addCleanup(self.plant.close)
        self.journals = {name: str(self.root / (name + '.jsonl'))
                         for name in (OWNER, PEER)}
        self.feed = AheadRejoinFeed(self.plant, self.journals)

    def _ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'plant': self.plant.address,
               'evidence_dir': str(self.evidence),
               'pair_token': 'dcs-qa-pair',
               'failover_misses': 120,
               'journal_files': dict(self.journals),
               'plant_owner': {'active': 424243, 'standby': 424244},
               'endpoint_placement': {'active': 'loopback',
                                      'standby': 'loopback',
                                      'plant': 'loopback'},
               'stop_controller': feed.stop_controller,
               'start_controller': feed.start_controller}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'AHEAD_POLL', 0.001), \
                patch.object(scenarios, 'SETTLE', 1.5), \
                patch.object(scenarios, 'LEAD_SETTLE', 1.5), \
                patch.object(scenarios, 'OUTAGE_SETTLE', 1.5), \
                patch.object(scenarios, 'REJOIN_SETTLE', 1.5), \
                patch.object(scenarios, 'RESTORE_SETTLE', 1.5):
            return scenarios.scenario_own_tick_ahead_rejoin(
                ctx or self._ctx(feed))

    def _pass(self, number=1):
        return json.loads((self.evidence
                           / ('own-tick-ahead-rejoin-pass-'
                              + str(number) + '.json')).read_text())

    def _assert_failed(self, key, record):
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ahead-bound-rejoin-failed', record.get('detail', ''))
        self.assertIn(key, str(self._pass(1)['violations']), record)
        report.validate_scenario(record)

    def _assert_nondeterministic(self, key, record):
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ahead-bound-rejoin-nondeterministic',
                      record.get('detail', ''))
        self.assertIn(key, str(self._pass(1)['violations']), record)
        report.validate_scenario(record)

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_own_tick_ahead_rejoin,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('own-tick-ahead-rejoin'),
                      scenarios.scenario_own_tick_ahead_rejoin)
        self.assertLess(
            order.index(scenarios.scenario_stranded_standby_no_resync),
            order.index(scenarios.scenario_own_tick_ahead_rejoin))

    def test_clean_passes_produce_identical_digests(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = sorted(entry['ref'] for entry in record['evidence'])
        self.assertEqual(
            refs, ['evidence/own-tick-ahead-rejoin-pass-1.json',
                   'evidence/own-tick-ahead-rejoin-pass-2.json'])
        for entry in record['evidence']:
            self.assertTrue((self.root / entry['ref']).exists(), entry)
        self.assertEqual(self._pass(1)['digest'], self._pass(2)['digest'])
        self.assertEqual(
            self._pass(1)['digest'],
            {'staging': 'ahead-of-the-bound',
             'lead-declaration': 'honest', 'switch': 'in-place',
             'rejoin': 'tracked', 'adoption': 'claimed-monitor',
             'document': 'honest', 'promotable': 'granted',
             'layout': 'restored'})
        self.assertEqual(self._pass(1)['violations'], {})
        self.assertIn('identical digests', ' '.join(record['observations']))

    def test_clean_pass_records_the_staging_and_the_rejoin(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = self._pass(1)
        # The staging rode the lane's own lifecycle seam, not an
        # injected tick: the owner's container was stopped and started
        # once per pass, and the tracker kept its cadence throughout.
        self.assertEqual(first['stage']['outage'], 'restarted')
        self.assertGreater(first['stage']['accrued']['lead'],
                           scenarios.AHEAD_BOUND + scenarios.LEAD_MARGIN)
        self.assertGreater(first['stage']['lead']['lead'],
                           scenarios.AHEAD_BOUND)
        self.assertLessEqual(first['stage']['document']['stream'],
                             first['stage']['document']['tick'])
        # The supersede: the routine promote preempted the claim and the
        # ex-owner demoted in place under the fenced origin with its
        # loss attributed to the promoted successor's own token.
        self.assertEqual(first['switch']['promote']['status'], 200)
        self.assertEqual(first['switch']['losses'],
                         [{'point': 20, 'claimant': 424244}])
        self.assertEqual(first['switch']['roles'],
                         [('active', 'demoting', 'fenced'),
                          ('demoting', 'standby', 'fenced')])
        self.assertEqual(first['switch']['verdict'],
                         {'fenced': True, 'owner': 424244,
                          'monitor': '172.20.0.5:8081'})
        # The re-join: unsynchronized first, then converged through the
        # claim's declared monitor with one journaled adoption and no
        # refusal, inside the documented bound, with no restart inside
        # the window.
        self.assertEqual([row['sync'] for row in first['rejoin']['rows']],
                         ['unsynchronized', 'tracking'])
        self.assertLessEqual(first['rejoin']['ticks'], scenarios.REJOIN_TICKS)
        self.assertEqual(first['adoption']['adoptions'],
                         [{'source': '172.20.0.5:8081'}])
        self.assertEqual(first['adoption']['refusals'], [])
        self.assertEqual(first['adoption']['boundaries'],
                         first['switch']['boundaries']['active'])
        self.assertIs(first['adoption']['document']['owns'], False)
        # The documented switch back answered the converged path and the
        # pair ended on its launch roles.
        self.assertEqual(first['restore']['promote']['status'], 200)
        self.assertEqual(first['roles']['final']['active']['role'],
                         'active')
        self.assertIs(first['roles']['final']['standby']['tracking'], True)

    def test_rejoin_asserted_while_stranded_fails(self):
        # The issue's doctored negative, staged on the rig: the ex-owner
        # never resolves the claim's declared monitor, so its sync parks
        # unsynchronized where the defect held it.
        self.feed.strand = True
        record = self.run_scenario()
        self._assert_failed('rejoin-stranded', record)
        self.assertEqual(self._pass(1)['digest']['rejoin'], 'stranded')
        self.assertIsNone(self._pass(1)['rejoin']['tracked'])
        self.assertEqual(self._pass(1)['adoption']['adoptions'], [])

    def test_rejoin_past_the_documented_bound_fails(self):
        # The strand's own shape: converged, but only after a wait the
        # documented bound does not admit.
        self.feed.adopt_pending_forever = True
        record = self.run_scenario()
        self._assert_nondeterministic('rejoin-watch', record)

    def test_adoption_missing_fails(self):
        self.feed.adoption_missing = True
        record = self.run_scenario()
        self._assert_failed('adoption-silent', record)
        self.assertEqual(self._pass(1)['digest']['adoption'], 'silent')

    def test_adoption_duplicated_fails(self):
        self.feed.adoption_twice = True
        record = self.run_scenario()
        self._assert_failed('adoption-duplicated', record)

    def test_adoption_foreign_fails(self):
        self.feed.adoption_foreign = True
        record = self.run_scenario()
        self._assert_failed('adoption-foreign', record)

    def test_source_refusal_journaled_fails(self):
        self.feed.refusal_journaled = True
        record = self.run_scenario()
        self._assert_failed('refusal-journaled', record)

    def test_restart_performed_the_rejoin_fails(self):
        # The defect's only recovery: a restart-as-standby landing
        # inside the re-join window converged the line instead of the
        # claimed-monitor rendezvous.
        self.feed.restart_rejoined = True
        record = self.run_scenario()
        self._assert_failed('restart-rejoined', record)

    def test_served_document_foreign_fails(self):
        self.feed.document_foreign = True
        record = self.run_scenario()
        self._assert_failed('document-foreign', record)
        self.assertEqual(self._pass(1)['digest']['document'], 'foreign')

    def test_served_document_misnamed_fails(self):
        self.feed.document_misnamed = True
        record = self.run_scenario()
        self._assert_failed('document-misnamed', record)

    def test_loss_silent_fails(self):
        self.feed.loss_missing = True
        record = self.run_scenario()
        self._assert_failed('loss-silent', record)

    def test_loss_duplicated_fails(self):
        self.feed.loss_twice = True
        record = self.run_scenario()
        self._assert_failed('loss-duplicated', record)

    def test_loss_unattributed_fails(self):
        self.feed.loss_unattributed = True
        record = self.run_scenario()
        self._assert_failed('loss-unattributed', record)

    def test_demotion_unwalked_fails(self):
        self.feed.walk_unexpected = True
        record = self.run_scenario()
        self._assert_failed('demotion-unwalked', record)

    def test_demotion_held_fails(self):
        self.feed.demotion_held = True
        record = self.run_scenario()
        self._assert_failed('demotion-held', record)

    def test_demotion_unexpected_walk_fails(self):
        # The peer walks a shape the demote-in-place does not document.
        feed = self.feed
        original = feed._scan

        def scan(peer):
            original(peer)
            if peer == 'a' and feed.role['a'] == 'demoting' \
                    and feed.walk_unexpected:
                feed.served['a'][-1]['event']['role_changed']['to'] = \
                    'starting'
        feed._scan = scan
        feed.walk_unexpected = False
        record = self.run_scenario()
        self._assert_failed('demotion-walk', record)

    def test_successor_inactive_fails(self):
        # The promoted successor takes the claim but never settles to
        # active — the field would stand unwritten.
        feed = self.feed
        feed.successor_inactive = True
        original = feed._scan

        def scan(peer):
            original(peer)
            if feed.successor_inactive and peer == 'b' \
                    and feed.role['b'] == 'active' and not feed.up['a']:
                feed.role['b'] = 'standby'
        feed._scan = scan
        record = self.run_scenario()
        self._assert_failed('successor-inactive', record)

    def test_verdict_open_fails(self):
        self.plant.open_field = True
        record = self.run_scenario()
        self._assert_failed('verdict-open', record)

    def test_verdict_foreign_fails(self):
        self.feed.verdict_foreign = True
        record = self.run_scenario()
        self._assert_failed('verdict-foreign', record)

    def test_verdict_undeclared_fails(self):
        self.feed.verdict_undeclared = True
        record = self.run_scenario()
        self._assert_failed('verdict-undeclared', record)

    def test_verdict_misnamed_fails(self):
        self.feed.verdict_misnamed = True
        record = self.run_scenario()
        self._assert_failed('verdict-misnamed', record)

    def test_stream_position_ahead_of_its_tick_fails(self):
        self.feed.stream_nonsense = True
        record = self.run_scenario()
        self._assert_failed('stream-nonsense', record)
        self.assertEqual(self._pass(1)['digest']['lead-declaration'],
                         'nonsense')

    def test_restore_refused_fails(self):
        self.feed.restore_refused = True
        record = self.run_scenario()
        self._assert_failed('restore-refused', record)
        self.assertEqual(self._pass(1)['digest']['promotable'], 'refused')

    def test_layout_unrestored_fails(self):
        self.feed.layout_unrestored = True
        record = self.run_scenario()
        self._assert_failed('layout-unrestored', record)
        self.assertEqual(self._pass(1)['digest']['layout'], 'moved')

    def test_launch_roles_lost_after_the_pass_fails(self):
        # The pair held its launch roles through the pass and lost them
        # once the pass's own claim was gone — the framing the final
        # read exists to catch.
        self.feed.layout_lost = True
        record = self.run_scenario()
        self._assert_failed('layout-unrestored', record)

    def test_staging_lever_refused_is_nondeterministic(self):
        for flag, key in (('stop_refused', 'stage-outage'),
                          ('start_refused', 'stage-outage')):
            with self.subTest(flag=flag):
                feed = AheadRejoinFeed(self.plant, self.journals)
                setattr(feed, flag, True)
                record = self.run_scenario(feed=feed)
                self._assert_nondeterministic(key, record)

    def test_lead_never_separated_is_nondeterministic(self):
        # The lifecycle stop held the owner down but its line position
        # kept moving with it, so the promotion never carried the lead.
        feed = AheadRejoinFeed(self.plant, self.journals)
        feed.no_lead = True
        record = self.run_scenario(feed=feed)
        self._assert_nondeterministic('stage-lead', record)
        self.assertEqual(self._pass(1)['digest']['staging'], 'unstaged')

    def test_lead_inside_the_bound_is_nondeterministic(self):
        # The outage separated, but the re-convergence closed the gap
        # back inside the retired window before the promote.
        feed = AheadRejoinFeed(self.plant, self.journals)
        original = feed.start_controller

        def start_controller(name):
            original(name)
            feed.tick['b'] = feed.tick['a']

        feed.start_controller = start_controller
        record = self.run_scenario(feed=feed)
        self._assert_nondeterministic('stage-lead', record)

    def test_pair_never_reconverged_is_nondeterministic(self):
        feed = AheadRejoinFeed(self.plant, self.journals)
        feed.never_reconverged = True
        record = self.run_scenario(feed=feed)
        self._assert_nondeterministic('stage-converge', record)

    def test_successor_not_converged_at_promote_is_nondeterministic(self):
        feed = AheadRejoinFeed(self.plant, self.journals)
        feed.posture_degraded = True
        record = self.run_scenario(feed=feed)
        self._assert_nondeterministic('stage-promote-posture', record)

    def test_promote_answered_by_the_gate_is_nondeterministic(self):
        self.feed.promote_gated = True
        record = self.run_scenario()
        self._assert_nondeterministic('promote-gated', record)

    def test_promote_unanswered_is_nondeterministic(self):
        self.feed.promote_unanswered = True
        record = self.run_scenario()
        self._assert_nondeterministic('promote-unanswered', record)

    def test_durable_sink_unreadable_is_nondeterministic(self):
        self.feed.durable_gone = True
        record = self.run_scenario()
        self._assert_nondeterministic('loss-unreadable', record)

    def test_rejoin_watch_starved_is_nondeterministic(self):
        self.feed.watch_starved = True
        record = self.run_scenario()
        self._assert_nondeterministic('rejoin-watch', record)

    def test_field_verdict_unreadable_is_nondeterministic(self):
        self.feed.verdict_unreadable = True
        original = self.feed._promote

        def promote(peer):
            answer = original(peer)
            if peer == PEER:
                self.plant.dispatch_for = _refuse_probe
            return answer
        self.feed._promote = promote
        record = self.run_scenario()
        self._assert_nondeterministic('verdict-unreadable', record)

    def test_missing_seams_report_inconclusive(self):
        for seam in ('stop_controller', 'start_controller'):
            with self.subTest(seam=seam):
                ctx = self._ctx()
                ctx[seam] = None
                record = self.run_scenario(ctx=ctx)
                self.assertEqual(record['outcome'], 'inconclusive', record)
                self.assertIn(seam, record.get('detail', ''))
                report.validate_scenario(record)
        # A peer with no published monitor, no plant endpoint, a
        # bridge-placed placement, no journal file, and no pinned token
        # are each equally unstageable.
        for key, value, needle in (
                ('active', None, 'both peer monitor endpoints'),
                ('plant', None, 'simulated plant endpoint'),
                ('endpoint_placement', {'active': 'bridge',
                                        'standby': 'bridge',
                                        'plant': 'bridge'},
                 'loopback endpoints'),
                ('journal_files', {'active': self.journals[OWNER]},
                 'journal files'),
                ('plant_owner', {'active': 424243}, '--owner-token')):
            with self.subTest(key=key):
                ctx = self._ctx()
                ctx[key] = value
                record = self.run_scenario(ctx=ctx)
                self.assertEqual(record['outcome'], 'inconclusive',
                                 record)
                self.assertIn(needle, record.get('detail', ''))

    def test_unreachable_pair_reports_inconclusive(self):
        self.feed.silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record.get('detail', ''))
        report.validate_scenario(record)

    def test_swapped_launch_layout_is_reseated(self):
        feed = AheadRejoinFeed(self.plant, self.journals)
        feed.swapped = True
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertIn('re-seated', ' '.join(record['observations']))
        self.assertEqual(self._pass(1)['digest']['layout'], 'restored')

    def test_unrecoverable_layout_reports_inconclusive(self):
        feed = AheadRejoinFeed(self.plant, self.journals)
        feed.swapped = True
        feed.promote_unanswered = True
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('launch layout', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_stamps_report_inconclusive(self):
        self.feed.stamps_absent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('field-ownership stamps', record.get('detail', ''))

    def test_missing_sync_vocabulary_reports_inconclusive(self):
        self.feed.sync_absent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('sync vocabulary', record.get('detail', ''))

    def test_verdict_without_owner_reports_inconclusive(self):
        self.feed.verdict_foreign = True
        feed = self.feed
        original = feed._stand

        def stand(peer):
            original(peer)
            feed.plant.claim = {'owner': None, 'holders': {0},
                                'monitor': MONITORS[peer]}
        feed._stand = stand
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no standing owner', record.get('detail', ''))

    def test_verdict_without_monitor_reports_inconclusive(self):
        feed = self.feed
        feed.verdict_undeclared = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('declares no monitor', record.get('detail', ''))

    def test_lead_carrying_document_without_stream_reports_inconclusive(
            self):
        # The pinned revision predating #1269's declared stream-lead
        # contract: the leg declines rather than asserting, since the
        # lead-carrying document is the surface its own consistency
        # check rides on.
        self.feed.stream_absent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates the own-tick-ahead rejoin contract',
                      record.get('detail', ''))
        self.assertIn('stream position', record.get('detail', ''))
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        # Both passes audit clean, yet their normalized digests differ —
        # the determinism contract's own failure, staged at the digest
        # seam the leg compares.
        digests = iter([
            {'staging': 'ahead-of-the-bound',
             'lead-declaration': 'honest', 'switch': 'in-place',
             'rejoin': 'tracked', 'adoption': 'claimed-monitor',
             'document': 'honest', 'promotable': 'granted',
             'layout': 'restored'},
            {'staging': 'ahead-of-the-bound',
             'lead-declaration': 'honest', 'switch': 'in-place',
             'rejoin': 'stranded', 'adoption': 'claimed-monitor',
             'document': 'honest', 'promotable': 'granted',
             'layout': 'restored'}])
        with patch.object(scenarios, '_ahead_digest',
                          lambda record, violations: next(digests)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ahead-bound-rejoin-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('diverged', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unchecked_self_check_fails(self):
        with patch.object(scenarios, '_ahead_self_check',
                          lambda: ['rejoin-asserted-while-stranded']):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ahead-bound-rejoin-unchecked',
                      record.get('detail', ''))
        self.assertIn('rejoin-asserted-while-stranded',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_self_check_is_complete(self):
        # The self-check plants a negative for every clause class the
        # judge's own naming reaches, and the audit catches each one —
        # so the two lists cannot drift apart, and a judge that let any
        # of them slip fails the leg's self-check.
        clause_classes = {
            'rejoin-asserted-while-stranded': 'rejoin-stranded',
            'rejoin-late-past-the-bound': 'rejoin-late',
            'rejoin-watch-never-answered': 'rejoin-watch',
            'adoption-missing': 'adoption-silent',
            'adoption-duplicated': 'adoption-duplicated',
            'adoption-foreign': 'adoption-foreign',
            'source-refusal-journaled': 'refusal-journaled',
            'restart-rejoined': 'restart-rejoined',
            'served-document-foreign': 'document-foreign',
            'served-document-misnamed': 'document-misnamed',
            'served-document-unreadable': 'document-unreadable',
            'loss-silent': 'loss-silent',
            'loss-duplicated': 'loss-duplicated',
            'loss-unattributed': 'loss-unattributed',
            'demotion-unwalked': 'demotion-unwalked',
            'demotion-held': 'demotion-held',
            'demotion-unexpected-walk': 'demotion-walk',
            'successor-inactive': 'successor-inactive',
            'verdict-open': 'verdict-open',
            'verdict-foreign': 'verdict-foreign',
            'verdict-undeclared': 'verdict-undeclared',
            'verdict-misnamed': 'verdict-misnamed',
            'stream-position-ahead-of-its-own-tick': 'stream-nonsense',
            'restore-refused': 'restore-refused',
            'layout-unrestored': 'layout-unrestored',
            'staging-lever-refused': 'stage-outage',
            'lead-never-separated': 'stage-lead',
            'pair-never-reconverged': 'stage-converge',
            'successor-not-converged-at-promote':
                'stage-promote-posture',
            'promote-answered-by-the-gate': 'promote-gated',
            'promote-unanswered': 'promote-unanswered',
            'durable-loss-unreadable': 'loss-unreadable',
            'durable-walk-unreadable': 'walk-unreadable',
            'durable-adoption-unreadable': 'adoption-unreadable',
            'field-verdict-never-answered': 'verdict-unreadable',
            'restore-unanswered': 'restore-unanswered',
        }
        source = (Path(scenarios.__file__).parent
                  / '2375_own_tick_ahead_rejoin.py').read_text()
        for negative, clause in clause_classes.items():
            with self.subTest(negative=negative):
                self.assertIn("expect('" + negative + "'", source)
                self.assertIn("'" + clause + "'", source)
        self.assertEqual(scenarios._ahead_self_check(), [])


def _refuse_probe(conn, request):
    """A plant peer that closes the connection mid-answer — the field's
    own arbitration refusing to be read for one probe."""
    raise ConnectionError('the plant connection closed mid-probe')
