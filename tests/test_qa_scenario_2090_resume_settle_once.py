"""The 2090_resume_settle_once leg's scenario unit coverage — the
feed fakes and TestCase classes for scenario_resume_settle_once,
split out per the #940 convention. The shared fakes and helpers live
in tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails
the discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam
from test_qa_scenario_1900_demote_carry_settle import (
    DemoteCarryFeed, DemoteCarryPeer)


EXPECTED_CASES = frozenset({
    'ResumeSettleTests.test_registered',
    'ResumeSettleTests.test_clean_rig_passes_and_validates',
    'ResumeSettleTests.test_stale_reapply_reports_failed',
    'ResumeSettleTests.test_stray_settle_reports_nondeterministic',
    'ResumeSettleTests.test_verdict_regress_reports_failed',
    'ResumeSettleTests.test_newer_never_settles_reports_failed',
    'ResumeSettleTests.test_carry_never_forms_reports_inconclusive',
    'ResumeSettleTests.test_owner_never_demotes_reports_inconclusive',
    'ResumeSettleTests.test_missed_window_recovers_and_passes',
    'ResumeSettleTests.test_promote_refused_reports_failed',
    'ResumeSettleTests.test_rejoin_never_converges_reports_failed',
    'ResumeSettleTests.'
    'test_resume_grant_refused_reports_inconclusive',
    'ResumeSettleTests.test_stop_fails_reports_inconclusive',
    'ResumeSettleTests.test_no_active_reports_failed',
    'ResumeSettleTests.'
    'test_inverted_launch_reports_inconclusive',
    'ResumeSettleTests.'
    'test_unconverged_pair_reports_inconclusive',
    'ResumeSettleTests.test_unreachable_reports_inconclusive',
    'ResumeSettleTests.'
    'test_predates_contract_reports_inconclusive',
    'ResumeSettleTests.test_missing_actions_report_inconclusive',
    'ResumeSettleTests.'
    'test_missing_state_files_reports_inconclusive',
    'ResumeSettleTests.'
    'test_missing_journal_files_reports_inconclusive',
    'ResumeSettleTests.test_missing_plant_reports_inconclusive',
    'ResumeSettleTests.'
    'test_diverging_digests_report_nondeterministic',
    'ResumeSettleTests.test_two_runs_produce_identical_evidence',
})


class ResumeSettlePeer(DemoteCarryPeer):
    """One resume-leg endpoint: the carry peer's fields plus the
    tracked checkpoint source, the announced-hint set its checkpoint
    answers record, and the park set a demoted checkpoint's resume
    marks its restored `Accepted` receipts under — the private shape
    the leg never reads but the fake needs to model the suspend."""

    def __init__(self, name, token):
        super().__init__(name, token)
        self.source = None      # tracked checkpoint source name
        self.announced = []     # newest-first announced pullers
        self.stamp = None       # the propagated line-owner name
        self.parked = set()     # submission keys a restored demoted
                                # checkpoint parks — the first
                                # field-owning scan never re-queues
                                # them


class ResumeSettleFeed(DemoteCarryFeed):
    """A stubbed pair for the resume-settle leg, arbitrating the
    field claim through a real ClaimPlantPeer: ctrl-a owns the field
    under TOKEN_A at launch and ctrl-b tracks it through its
    configured --standby pull. Every endpoint call is one scan
    except the control POSTs — an admission lands pending between
    scans, so a rogue claim_writer planted after it meets the
    detection scan's suspension: pending receipts stay `Accepted`,
    the demoted run's cycle-end --state-file persist stamps
    `source_owns_field: false`, and while the monitor-less rogue
    stands no tracking source can be proved — the stopped
    container's frozen document is the suspended shape the resumed
    run must park rather than re-apply. `stop_controller` drops a
    peer's liveness, `start_controller`/`restart_controller` resume
    it from its persisted file — the launched-active through the
    conditional startup grant the dead incumbent's holder-less
    claim yields to, the launched-standby into a tracking pull.
    Every journaled event mirrors into the journal_files paths the
    leg's durable audit reads."""

    TOKENS = {DemoteCarryFeed.TOKEN_A: 'a',
              DemoteCarryFeed.TOKEN_B: 'b'}
    KEYS = {'active': 'a', 'standby': 'b'}

    def __init__(self, plant, journal_files=None, state_files=None):
        super().__init__(plant)
        self.a = ResumeSettlePeer('a', self.TOKEN_A)
        self.a.role = 'active'
        self.a.stamp = 'a'
        self.b = ResumeSettlePeer('b', self.TOKEN_B)
        self.b.sync = 'tracking'
        self.b.stamp = 'a'
        self.b.source = 'a'
        self.a.announced = ['b']
        self.up = {'a': True, 'b': True}
        self.journal_paths = {}
        for peer, key in ((self.a, 'active'), (self.b, 'standby')):
            path = (journal_files or {}).get(key)
            if path is not None:
                path = Path(path)
                path.write_text(
                    json.dumps({'run_boundary': {'run': 1,
                                                 'tick': 0}}) + '\n')
                self.journal_paths[peer.name] = path
        # The --state-file seam: each peer's persisted checkpoint,
        # rewritten under write-then-rename on every scan — the
        # suspended capture the leg polls and the document a restart
        # resumes.
        self.state_paths = {}
        for peer, key in ((self.a, 'active'), (self.b, 'standby')):
            path = (state_files or {}).get(key)
            if path is not None:
                path = Path(path)
                self.state_paths[peer.name] = path
                self._persist(peer)
        # The doctors staging each named defect.
        self.reapply = False        # the #1056 defect: a demoted
                                    # checkpoint's restored `Accepted`
                                    # re-queues on the first owning
                                    # scan instead of parking
        self.stray_settle = False   # the resumed run journals a
                                    # settle for the admission its
                                    # successor already settled
        self.verdict_regress = False  # the rejoin's adoption regresses
                                      # the run's own settled verdict
                                      # to the adopted `Accepted`
        self.newer_dropped = False  # the successor's boundary never
                                    # applies the newer admission
        self.no_carry = False       # the pulls drop the suspended
                                    # admission — the carry never
                                    # forms
        self.apply_first = False    # the fenced scan applies before
                                    # detecting — the missed
                                    # suspension window, the stage's
                                    # restage path
        self.grant_refused = False  # the conditional startup grant
                                    # refuses even a dead incumbent's
                                    # claim
        self.rejoin_fails = False   # the incumbent's restart never
                                    # serves
        self.stop_fails = False     # stop_controller raises
        self.predates_contract = False  # the contract's surfaces —
                                    # checkpoint receipt window,
                                    # admission counters, ownership
                                    # stamp, receipt actor/reason —
                                    # are absent

    def _peers(self):
        return {'a': self.a, 'b': self.b}

    def _claimed(self):
        """The field's standing claim's declared monitor — a peer
        name when a controller's claim stands, None for the
        monitor-less rogue claim."""
        return self.TOKENS.get((self.plant.claim or {})
                               .get('owner'))

    # ---- the persisted checkpoint -----------------------------------

    def _persist(self, peer):
        """The paced loop's cycle-end --state-file persist: the
        checkpoint document under write-then-rename, stamped with
        the scan's ownership so a demoted run's file carries the
        suspended shape the resume gate reads."""
        path = self.state_paths.get(peer.name)
        if path is None:
            return
        document = {'format_version': 1,
                    'model_fingerprint': 'resume-fp',
                    'tick': peer.tick,
                    'source_owns_field': peer.role == 'active',
                    'receipts': copy.deepcopy(peer.receipts),
                    'command_admission': {'attempts': peer.attempts},
                    'image': dict(peer.image)}
        staging = path.with_suffix('.tmp')
        staging.write_text(json.dumps(document) + '\n')
        staging.replace(path)

    # ---- the contract halves ----------------------------------------

    def _probe_source(self, peer):
        """The demoted peer's re-resolution probe: the field's
        claimed monitor leads, then the recorded announcers — a
        candidate must be up and serving the line as its field
        owner, the owner-stamp shape a standby's document cannot
        produce. While the monitor-less rogue stands, nothing
        resolves."""
        for candidate in [self._claimed()] + list(peer.announced):
            if candidate is not None and candidate != peer.name \
                    and self.up.get(candidate) \
                    and self._peers()[candidate].role == 'active':
                return candidate
        return None

    def _pull(self, peer):
        """One tracking pull: a sourceless peer probes first, then
        pulls its source's checkpoint; a dead source leaves the
        orphan standing — the missed pull degrades only a
        still-resolving peer — and an adopted non-owner's document
        leaves the puller orphaned and re-resolving."""
        source = peer.source
        if source is None:
            source = self._probe_source(peer)
            if source is not None:
                peer.source = source
        if source is None or not self.up.get(source) \
                or self.no_tracking:
            if peer.sync != 'orphaned':
                peer.sync = 'unsynchronized'
            return
        src = self._peers()[source]
        if peer.name not in src.announced:
            src.announced.insert(0, peer.name)
            src.announced[:] = src.announced[:4]
        self._adopt(peer, src)
        peer.stamp = src.stamp or (
            source if src.role == 'active' else None)
        if src.role == 'active':
            peer.sync = 'tracking'
            return
        peer.sync = 'orphaned'
        candidate = self._probe_source(peer)
        if candidate is not None:
            peer.source = candidate

    def _adopt(self, peer, src):
        """The tracking pull's per-entry adoption: the source's log
        merges over the peer's own — an adopted entry is the same
        submission at any outcome, this run's receipts beyond the
        window stay, and a local terminal verdict stands over an
        adopted still-`Accepted` view — the receipt-as-truth rule
        the rejoin's audit pins, degraded by the verdict_regress
        doctor. The adopted image replaces the peer's own."""
        merged = copy.deepcopy(src.receipts)
        if self.no_carry:
            merged = [receipt for receipt in merged
                      if 'accepted' not in receipt['outcome']]
        for local in peer.receipts:
            hit = next((carried for carried in merged
                        if self._key(carried) == self._key(local)),
                       None)
            if hit is None:
                merged.append(copy.deepcopy(local))
                continue
            local_terminal = 'accepted' not in local['outcome']
            hit_terminal = 'accepted' not in hit['outcome']
            if local_terminal and not hit_terminal \
                    and not self.verdict_regress:
                merged[merged.index(hit)] = copy.deepcopy(local)
        merged.sort(key=lambda receipt: receipt.get('index') or 0)
        peer.receipts = merged
        peer.attempts = max(peer.attempts, src.attempts)
        peer.image = dict(src.image)
        self._observe(peer)

    def _apply(self, peer):
        """The field-owning boundary: every pending admission the
        parked set does not cover applies, settles, and lands its
        value on the served image — the boundary a re-queued stale
        command would re-run its write through."""
        for receipt in peer.receipts:
            if 'accepted' not in receipt['outcome'] \
                    or self._key(receipt) in peer.parked \
                    or (self.newer_dropped
                        and str(receipt.get('actor'))
                        .endswith('-newer')):
                continue
            write = receipt['command']['write_value']
            receipt['outcome'] = {'applied': {'tick': peer.tick}}
            peer.image[write['point']] = write['value']['bool']
            self._settle(peer, receipt)

    def _scan(self, peer):
        """One completed scan: pending role transitions settle; a
        field-owning scan applies its pending admissions or — the
        claim preempted — detects the fence, journals the loss, and
        demotes in place with its receipts suspended; a standby
        pulls its tracked source. The cycle ends on the --state-file
        persist."""
        peer.tick += 1
        if peer.role == 'demoting':
            peer.role = 'standby'
            peer.sync = 'unsynchronized'
            self._mark(peer, {'role_changed': {'from': 'demoting',
                                               'to': 'standby'}})
        elif peer.role == 'promoting':
            peer.role = 'active'
            peer.sync = None
            peer.stamp = peer.name
            self._mark(peer, {'role_changed': {'from': 'promoting',
                                               'to': 'active'}})
        if peer.role == 'active':
            claim = self.plant.claim or {}
            if self.no_demote or claim.get('owner') == peer.token:
                self._apply(peer)
            else:
                if self.apply_first:
                    # The preemption landed past the apply
                    # boundary: the admission settled on the
                    # owner's own line — a correct run, not the
                    # suspended shape. The window misses once —
                    # the stage's restage path covers the retry.
                    self.apply_first = False
                    self._apply(peer)
                self._mark(peer, {'field_claim_lost': {
                    'tick': peer.tick, 'point': 0}})
                peer.role = 'demoting'
                self._mark(peer, {'role_changed': {
                    'from': 'active', 'to': 'demoting'}})
        elif peer.role == 'standby':
            self._pull(peer)
        self._observe(peer)
        self._persist(peer)

    # ---- the runner-owned lifecycle actions --------------------------

    def stop_controller(self, key):
        if self.stop_fails:
            raise RuntimeError('docker stop failed: stop refused')
        self.up[self.KEYS[key]] = False

    def _startup_grant(self, peer):
        """The launched-active's conditional startup claim: granted
        while the standing claim names no live peer — the dead
        incumbent's holder-less claim preempts — and refused into a
        live incumbent's field, the resuming process exiting
        unserved."""
        if self.grant_refused:
            return False
        standing = self._claimed()
        if standing is not None and standing != peer.name \
                and self.up.get(standing, False):
            return False
        self._wire_claim(peer.token)
        return True

    def start_controller(self, key):
        peer = self._peers()[self.KEYS[key]]
        path = self.state_paths.get(peer.name)
        document = None
        if path is not None and path.exists():
            try:
                document = json.loads(path.read_text())
            except ValueError:
                document = None
        if isinstance(document, dict):
            # The checkpoint's resume: the run continues its tick,
            # its receipt window, its admission counters, and its
            # image — and a document stamped non-owning parks every
            # restored `Accepted` under the #1056 gate, the
            # requeue_suspended skip the reapply doctor drops.
            peer.tick = document.get('tick') or peer.tick
            peer.receipts = copy.deepcopy(
                document.get('receipts') or [])
            peer.attempts = (document.get('command_admission')
                             or {}).get('attempts') or peer.attempts
            peer.image = dict(document.get('image') or {})
            peer.parked = set()
            if document.get('source_owns_field') is False \
                    and not self.reapply:
                peer.parked = {
                    self._key(receipt)
                    for receipt in peer.receipts
                    if 'accepted' in receipt['outcome']}
        if peer.name == 'a':
            if not self._startup_grant(peer):
                return
            peer.role = 'active'
            peer.sync = None
            peer.stamp = 'a'
            peer.source = None
            if self.stray_settle:
                for receipt in peer.receipts:
                    if 'accepted' in receipt['outcome'] \
                            and str(receipt.get('actor')) \
                            .startswith('qa-resume'):
                        verdict = dict(receipt)
                        verdict['outcome'] = {
                            'applied': {'tick': peer.tick}}
                        self._mark(peer, {'command_settled': {
                            'receipt': verdict}})
        else:
            if self.rejoin_fails:
                return
            peer.role = 'standby'
            peer.sync = 'unsynchronized'
            peer.source = 'a'
            peer.stamp = None
        self.up[peer.name] = True
        self._persist(peer)

    def restart_controller(self, key):
        self.stop_controller(key)
        self.start_controller(key)

    def invert(self):
        """The pair launched with its roles exchanged — the standby
        member owns the field, a layout the resume stage cannot
        play: only the launched-active resumes restart-as-active."""
        self.a.role = 'standby'
        self.a.sync = 'tracking'
        self.a.source = 'b'
        self.a.stamp = 'b'
        self.a.announced = []
        self.b.role = 'active'
        self.b.sync = None
        self.b.stamp = 'b'
        self.b.source = None
        self.b.announced = ['a']
        self._wire_claim(self.TOKEN_B)
        self._persist(self.a)
        self._persist(self.b)

    # ---- the endpoint dispatch ---------------------------------------

    def _report(self, peer):
        role = 'standby' if peer.name == 'a' and self.no_active \
            else peer.role
        report = {'role': role, 'tick': peer.tick}
        if role == 'standby':
            sync = peer.sync or 'unsynchronized'
            report['sync'] = {sync: {'aligned': peer.tick}} \
                if sync in ('tracking', 'orphaned', 'reinitialized') \
                else {'unsynchronized': {}}
        return report

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        host = url.split('://', 1)[1].split(':')[0]
        peer = self._peers()[host.split('-', 1)[1]]
        if not self.up[peer.name]:
            raise urllib.error.URLError('connection refused')
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if (method, route) == ('POST', '/command'):
            # An admission lands pending between scans — the call
            # itself is not a scan, so the suspension-window
            # submission meets the detection's suspension.
            receipt = {'command': (body or {}).get('command'),
                       'actor': (body or {}).get('actor'),
                       'reason': (body or {}).get('reason'),
                       'index': peer.attempts,
                       'outcome': {'accepted': {
                           'apply_tick': peer.tick + 1}}}
            if self.predates_contract:
                receipt.pop('actor', None)
                receipt.pop('reason', None)
            if peer.role != 'active':
                receipt['outcome'] = {'rejected': {'reason': {
                    'not_active': {}}}}
                self._settle(peer, receipt)
            else:
                peer.attempts += 1
                peer.receipts.append(receipt)
                self._persist(peer)
            return 200, copy.deepcopy(receipt)
        if (method, route) == ('POST', '/demote'):
            if peer.role != 'active':
                self._raise(409, {'not_active': {}})
            peer.role = 'demoting'
            self._mark(peer, {'role_changed': {'from': 'active',
                                               'to': 'demoting'}})
            self._persist(peer)
            return 200, {'role': 'demoting', 'tick': peer.tick}
        if (method, route) == ('POST', '/promote'):
            if peer.role in ('active', 'promoting'):
                self._raise(409, {'already_active': {}})
            if peer.role != 'standby' \
                    or peer.sync not in ('tracking', 'orphaned',
                                         'reinitialized') \
                    or self.promote_refused:
                self._raise(409, {'not_converged': {
                    'sync': peer.sync or 'unsynchronized'}})
            # The promotion boundary's final-sync fetch — the
            # tracked source's live document, nothing from a
            # stopped holder — then the field claim the standing
            # token loses.
            source = peer.source
            if source is not None and self.up.get(source):
                self._adopt(peer, self._peers()[source])
            self._wire_claim(peer.token)
            peer.role = 'promoting'
            self._persist(peer)
            return 200, {'role': 'promoting', 'tick': peer.tick}
        # The paced pair: every other endpoint call is one scan.
        self._scan(peer)
        if (method, route) == ('GET', '/role'):
            return 200, self._report(peer)
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': list(self.SIGNALS),
                         'components': []}
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': peer.tick, 'points': [
                {'point': point, 'direction': 'in',
                 'sample': {'value': {'bool': peer.image.get(point,
                                                           False)},
                            'quality': 'good', 'tick': peer.tick}}
                for point in self.POINTS]}
        if (method, route) == ('GET', '/checkpoint'):
            if self.predates_contract:
                return 200, {'format_version': 1,
                             'model_fingerprint': 'resume-fp'}
            return 200, {'receipts': list(peer.receipts),
                         'command_admission': {
                             'attempts': peer.attempts},
                         'source_owns_field':
                             peer.role == 'active'}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(peer.receipts)
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if '=' in query else 0
            return 200, [dict(entry) for entry in peer.journal
                         if entry['seq'] > since]
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class ResumeSettleTests(unittest.TestCase):
    """The resume-settle leg against the stubbed pair over a real
    claim-arbitrating plant: a clean rig passes with identical
    digests and evidence — the suspended admission carried and
    applied once on the successor, the newer command's value
    standing on both monitors, the resumed peer parking its
    restored `Accepted` — each doctored defect reports the named
    diagnostic, and an unreachable, unconverged, seam-less, or
    pre-contract run is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_dir = Path(self.tmp.name) / 'journals'
        self.journal_dir.mkdir()
        self.state_dir = Path(self.tmp.name) / 'state'
        self.state_dir.mkdir()
        self.plant = ClaimPlantPeer()
        self.feed = ResumeSettleFeed(
            self.plant, journal_files=self._journal_files(),
            state_files=self._state_files())

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _journal_files(self):
        return {key: str(self.journal_dir / (key + '.jsonl'))
                for key in ('active', 'standby')}

    def _state_files(self):
        return {key: str(self.state_dir / (key + '.json'))
                for key in ('active', 'standby')}

    def _ctx(self, **overrides):
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'plant': self.plant.address,
               'state_files': self._state_files(),
               'journal_files': self._journal_files(),
               'evidence_dir': str(self.evidence),
               'stop_controller': self.feed.stop_controller,
               'start_controller': self.feed.start_controller,
               'restart_controller': self.feed.restart_controller}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'RESUME_SETTLE', 2.0), \
                patch.object(scenarios, 'RESUME_AUDIT', 1.0), \
                patch.object(scenarios, 'RESUME_POLL', 0.001), \
                patch.object(scenarios, 'RESUME_WATCH', 0.001):
            return scenarios.scenario_resume_settle_once(
                ctx or self._ctx())

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The resume-settle leg's window: behind the suspended-alias
        # audit, still inside the launch-layout window the tune
        # case's a->b switch closes.
        self.assertLess(
            order.index(scenarios.scenario_suspended_alias_audit),
            order.index(scenarios.scenario_resume_settle_once))
        self.assertLess(
            order.index(scenarios.scenario_resume_settle_once),
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(
            verify.case_function('resume-settle-once'),
            scenarios.scenario_resume_settle_once)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('resume-settle-signals.json',
                     'resume-settle-pass-1.json',
                     'resume-settle-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads((self.evidence / name).read_text())
                  for name in ('resume-settle-pass-1.json',
                               'resume-settle-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'point': 'newer', 'resumed': 'parked',
             'suspended_verdict': 'stable',
             'newer_verdict': 'stable',
             'journals': 'once-each', 'roles': 'restored'})
        # The staged admission froze `Accepted` into the demoted
        # peer's state file; the successor applied it and the newer
        # command, each journaled once on its own record only.
        self.assertEqual(
            passes[0]['attempts'][0]['capture']['owns_field'],
            False)
        self.assertEqual(
            scenarios._outcome_key(
                passes[0]['attempts'][0]['capture']['receipt']),
            'accepted')
        counts = passes[0]['counts']
        self.assertEqual(
            counts['suspended']['standby'],
            {'served': 1, 'durable': 1, 'outcomes': ['applied']})
        self.assertEqual(
            counts['suspended']['active'],
            {'served': 0, 'durable': 0, 'outcomes': []})
        self.assertEqual(
            counts['newer']['standby'],
            {'served': 1, 'durable': 1, 'outcomes': ['applied']})
        self.assertEqual(
            counts['newer']['active'],
            {'served': 0, 'durable': 0, 'outcomes': []})
        # The newer write is what both monitors serve — the stale
        # re-apply's stomp absent: the baseline value the second
        # command restored.
        self.assertEqual(passes[0]['images'],
                         {'active': False, 'standby': False})
        self.assertGreaterEqual(
            self.plant.requests.count('claim_writer'), 2)
        report.validate_scenario(record)

    def test_stale_reapply_reports_failed(self):
        # The #1056 defect: the resumed demotion's restored
        # `Accepted` re-queues — the stale command re-applies,
        # stomping the newer settle and minting the second
        # settlement the journals count.
        self.feed.reapply = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'resume-settle-once-failed'), record['detail'])
        report.validate_scenario(record)

    def test_stray_settle_reports_nondeterministic(self):
        # One admission, two journaled settlements — the
        # second-mint signature without the image stomp.
        self.feed.stray_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'resume-settle-once-nondeterministic'),
            record['detail'])
        report.validate_scenario(record)

    def test_verdict_regress_reports_failed(self):
        # The rejoin's adoption regressed the run's own settled
        # verdict to the adopted `Accepted` — the served receipt's
        # terminal truth mutated post-settle.
        self.feed.verdict_regress = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'resume-settle-once-failed'), record['detail'])
        self.assertIn('mutated', record['detail'])
        report.validate_scenario(record)

    def test_newer_never_settles_reports_failed(self):
        self.feed.newer_dropped = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'resume-settle-once-failed'), record['detail'])
        self.assertIn('newer', record['detail'])
        report.validate_scenario(record)

    def test_carry_never_forms_reports_inconclusive(self):
        self.feed.no_carry = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('adopted', record['detail'])
        report.validate_scenario(record)

    def test_owner_never_demotes_reports_inconclusive(self):
        self.feed.no_demote = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('checkpoint', record['detail'])
        report.validate_scenario(record)

    def test_missed_window_recovers_and_passes(self):
        # The preemption landed past the apply boundary on the
        # first staged admission — a correct settle, not the
        # suspended shape: the pair re-takes the field and the
        # second admission stages.
        self.feed.apply_first = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passed = json.loads(
            (self.evidence / 'resume-settle-pass-1.json')
            .read_text())
        self.assertTrue(
            passed['attempts'][0]['missed'].startswith('settled-'),
            passed['attempts'])
        self.assertTrue(passed['attempts'][0]['recovered'])
        self.assertEqual(len(passed['attempts']), 2)
        report.validate_scenario(record)

    def test_promote_refused_reports_failed(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'resume-settle-once-failed'), record['detail'])
        self.assertIn('/promote', record['detail'])
        report.validate_scenario(record)

    def test_rejoin_never_converges_reports_failed(self):
        self.feed.rejoin_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'resume-settle-once-failed'), record['detail'])
        self.assertIn('reconverged', record['detail'])
        report.validate_scenario(record)

    def test_resume_grant_refused_reports_inconclusive(self):
        # The resumed peer's conditional startup grant refuses even
        # the dead incumbent's claim — the field never comes back
        # to the launched active, a pre-contract surface.
        self.feed.grant_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('active', record['detail'])
        report.validate_scenario(record)

    def test_stop_fails_reports_inconclusive(self):
        self.feed.stop_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('stop_controller', record['detail'])
        report.validate_scenario(record)

    def test_no_active_reports_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active', record['detail'])
        report.validate_scenario(record)

    def test_inverted_launch_reports_inconclusive(self):
        self.feed.invert()
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('launched-active', record['detail'])
        report.validate_scenario(record)

    def test_unconverged_pair_reports_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])
        report.validate_scenario(record)

    def test_unreachable_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_predates_contract_reports_inconclusive(self):
        self.feed.predates_contract = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])
        report.validate_scenario(record)

    def test_missing_actions_report_inconclusive(self):
        for action in ('stop_controller', 'start_controller',
                       'restart_controller'):
            with self.subTest(action=action):
                record = self.run_scenario(
                    self._ctx(**{action: None}))
                self.assertEqual(record['outcome'], 'inconclusive',
                                 record)
                self.assertIn(action, record['detail'])
                report.validate_scenario(record)

    def test_missing_state_files_reports_inconclusive(self):
        record = self.run_scenario(self._ctx(state_files={}))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('state file', record['detail'])
        report.validate_scenario(record)

    def test_missing_journal_files_reports_inconclusive(self):
        record = self.run_scenario(self._ctx(journal_files={}))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal', record['detail'])
        report.validate_scenario(record)

    def test_missing_plant_reports_inconclusive(self):
        record = self.run_scenario(self._ctx(plant=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('plant', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'roles': 'restored'}, {}, {'pass': 1}),
                       ({'roles': 'unrestored'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_resume_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'resume-settle-once-nondeterministic'),
            record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            plant = ClaimPlantPeer()
            feed = ResumeSettleFeed(
                plant, journal_files=self._journal_files(),
                state_files=self._state_files())
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.plant, self.evidence = plant, evidence
            try:
                record = self.run_scenario(feed=feed)
            finally:
                plant.close()
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
