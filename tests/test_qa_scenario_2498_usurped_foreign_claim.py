"""The 2498_usurped_foreign_claim leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_usurped_foreign_claim, in the
tests/test_qa_scenario_NNNN_<slug>.py split layout (#940). The shared
fakes and helpers live in tests/qa_scenario_support.py; the probe
subject here carries the leg's raw field-attachment seams
(field_request/hold_field_claim/drop_field_claim), the unkeyed
driven-writer launch (start_driven keyed=False), and the
keyed-posture relaunch lever as plain callables the feed backs.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


class UsurpedPlant:
    """The probe pair's bridge-placed field: the claim-arbitration
    state the leg's raw attachment seam drives. The standing claim
    records an owner token, its live holder names, and the declared
    monitor endpoint — the key absent entirely on a monitor-less
    claim so a probe verdict names no endpoint, matching the field's
    own arbitration. The doctor flags stage the predating/absent
    surfaces the leg reports inconclusive on."""

    def __init__(self, owner, monitor):
        self.claim = {'owner': owner, 'holders': {'active'},
                      'monitor': monitor, 'controller': True}
        self.requests = []
        # The doctors staging each named inconclusive.
        self.down = False           # the listener answers nothing
        self.probe_absent = False   # probe_writer is unknown
        self.undeclared = False     # fenced verdicts carry no monitor
        # The doctor staging each named defect.
        self.conditional_preempts = False  # the conditional grant
                                            # ignores a live foreign
                                            # holder
        self.unkeyed_preempts = False      # ... on the unkeyed
                                            # member only
        self.refuses_holderless = False    # the conditional grant
                                            # refuses the holderless
                                            # claim too — the restore
                                            # path it exists to serve

    def probe_reply(self):
        """probe_writer on a fresh attachment — a non-holder's
        observation: fenced naming the standing claim's owner and
        declared monitor while one stands, unclaimed while none
        does."""
        if self.probe_absent:
            return {'result': 'error', 'error': {
                'kind': 'invalid_request', 'detail': 'unknown op'}}
        if self.claim is None:
            return {'result': 'error', 'error': {
                'kind': 'unclaimed',
                'detail': 'no attachment holds field writes'}}
        error = {'kind': 'fenced',
                 'detail': 'writer claim held by another attachment',
                 'owner': self.claim['owner']}
        if 'monitor' in self.claim and not self.undeclared:
            error['monitor'] = self.claim['monitor']
        return {'result': 'error', 'error': error}

    def hold(self, request):
        """The held-claim seam's unconditional claim_writer — the
        foreign owner preempts whatever stands and the holder
        container keeps the claim's holder set occupied until the
        drop."""
        self.requests.append(request)
        claim = {'owner': request['owner'], 'holders': {'holder'},
                 'controller': request.get('controller', True)}
        if 'monitor' in request:
            claim['monitor'] = request['monitor']
        self.claim = claim
        return {'result': 'done'}

    def drop(self):
        """The holder container's end — the connection drops the
        attachment's hold, leaving the claim standing holderless for
        the pair's own reclaim."""
        if self.claim is not None:
            self.claim['holders'].discard('holder')

    def conditional_grant(self, member):
        """claim_writer_unless_held — the orphan verdict's conditional
        grant: refused while a live holder stands under a different
        owner, granted over an unclaimed field, a same-owner claim, or
        a holderless one."""
        claim = self.claim
        if claim is not None and claim['owner'] != member.token:
            if self.refuses_holderless:
                return False
            if claim.get('holders'):
                if not self.conditional_preempts \
                        and not (self.unkeyed_preempts
                                 and not member.keyed):
                    return False
        return True

    def claim_writer(self, member):
        """The unconditional claim — the deliberate takeover the
        tracking promote runs and the usurped verdict's reclaim. The
        claim lands naming this member's declared monitor; the
        standing claim's monitor answers what the field was taken
        from."""
        standing = (self.claim or {}).get('monitor')
        self.claim = {'owner': member.token, 'holders': {member.key},
                      'monitor': member.bridge, 'controller': True}
        return standing


class _Member:
    """One stubbed probe-pair run: role, sync verdict, keyed posture,
    claim ownership marks, and its journaled record. `keyed` is the
    --pair-token posture the relaunch lever rewrites — the
    foreign-writer diagnosis is a reading only a key-holding run can
    make."""

    def __init__(self, key, token, bridge, keyed=True, live=True):
        self.key = key          # 'active'/'standby'/'driven'
        self.token = token      # the pinned --owner-token
        self.bridge = bridge    # the monitor's rig-bridge address —
                                # the claim's declared endpoint
        self.keyed = keyed
        self.live = live
        self.role = 'standby'
        self.sync = 'unsynchronized'
        self.tick = 0
        self.seq = 0
        self.journal = []
        self.was_owner = False


class UsurpedPairFeed:
    """A stubbed probe pair plus its driven seat for the
    usurped-claim leg: 'probe-a' launches active owning the field,
    'probe-b' tracks it on its configured --standby source, and
    'probe-d' is the unkeyed driven attachment start_driven stages —
    paced through POST /scan, tracking the probe owner through the
    public checkpoint pull its tokenless posture still allows, and
    preempting the field through the unconditional claim its own
    promote runs.

    Every served monitor request on 'probe-a'/'probe-b' is one scan of
    the addressed member: an active or promoting member writes the
    field — a fenced write journals the attributed field_claim_lost
    and walks demoting -> standby — while a standby member pulls its
    tracking source and runs the foreign-writer diagnosis the keyed
    monitor owns: the standing claim's declared monitor dialed under
    ?prove=, convicting a live endpoint that cannot prove the pair key
    as `usurped` where every answerless shape — an unkeyed run, no
    monitor, a dead endpoint, the run's own monitor — stays
    `orphaned`. POST /promote runs the unconditional claim under
    tracking or usurped — the granted usurped claim journaling
    foreign_claim_preempted naming the endpoint it took the field
    from — and the conditional grant under orphaned, refusing a live
    different-owner incumbent. The driven member scans only inside
    POST /scan batches. Every journaled event mirrors into the
    --journal-file paths the leg's durable audit reads. The doctor
    flags stage each named defect — including the pre-#1410 shape
    (never_convicts: the foreign held claim reads plain orphaned and
    the conditional grant refuses it forever)."""

    TOKENS = {'active': 424248, 'standby': 424249, 'driven': 424250}
    BRIDGE = {'active': '172.18.0.2:8090',
              'standby': '172.18.0.3:8091',
              'driven': '172.18.0.5:8092'}
    HOLDER_ADDRESS = '172.18.0.9:9000'   # the held-claim seam's
                                          # container
    PLANT_ADDRESS = '172.18.0.4:9000'

    def __init__(self, plant, journal_files=None):
        self.plant = plant
        self.a = _Member('active', self.TOKENS['active'],
                         self.BRIDGE['active'])
        self.b = _Member('standby', self.TOKENS['standby'],
                         self.BRIDGE['standby'])
        self.a.role, self.b.role = 'active', 'standby'
        self.a.was_owner = True
        self.b.sync = 'tracking'
        self.d = _Member('driven', self.TOKENS['driven'],
                         self.BRIDGE['driven'], keyed=False,
                         live=False)
        self.members = {'probe-a': self.a, 'probe-b': self.b,
                        'probe-d': self.d}
        # The rig's live monitor endpoints — the dialable registry the
        # foreign-writer diagnosis resolves a declared monitor
        # through: an endpoint inside the keyed line proves the pair
        # key; a live endpoint outside it cannot; an absent entry is
        # a dead declaration the diagnosis cannot convict.
        self.endpoints = {self.a.bridge: self.a, self.b.bridge: self.b}
        # The durable half: journal_files maps ctx keys to the
        # runner's --journal-file paths.
        self.journal_paths = {}
        for member, key in ((self.a, 'active'), (self.b, 'standby'),
                            (self.d, 'driven')):
            path = (journal_files or {}).get(key)
            if path is not None:
                path = Path(path)
                path.write_text(json.dumps(
                    {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
                self.journal_paths[member] = path
        # The doctors staging each named defect.
        self.silent = False            # the pair never answers
        self.unsettled = False         # probe-b never reports tracking
        self.promote_refused = False   # /promote answers an error
        self.usurper_refused = False   # the driven promote is refused —
                                       # the staging never preempts
        self.never_demotes = False     # the fenced owner never demotes
        self.no_loss = False           # the demotion goes unrecorded
        self.never_convicts = False    # the pre-fix shape: a live
                                       # foreign writer reads plain
                                       # orphaned
        self.convicts_absent = False   # a monitor-less claim convicts —
                                       # preemption armed on no evidence
        self.convicts_dead = False     # a dead declared endpoint
                                       # convicts
        self.unkeyed_convicts = False  # an unkeyed run reports usurped
        self.conditional_reclaim = False  # the usurped promote routes
                                          # the conditional grant —
                                          # the reclaim refuses forever
        self.silent_preempt = False    # the preempted claim never
                                       # journals
        self.wrong_writer = None       # the preemption names this
                                       # endpoint instead
        self.silent_durable = False    # the journal file mirror drops
                                       # the preemption record
        self.usurper_survives = False  # the fenced usurper never
                                       # demotes
        self.never_clears = False      # the sibling latches usurped
                                       # under the keyed writer

    # ---- the pair's run semantics -------------------------------

    def _journal(self, member, event):
        """One journaled record — the served journal plus the durable
        --journal-file mirror the leg's file audit reads."""
        member.seq += 1
        entry = {'seq': member.seq, 'tick': member.tick,
                 'event': event}
        member.journal.append(entry)
        path = self.journal_paths.get(member)
        if path is not None and not (
                self.silent_durable
                and 'foreign_claim_preempted' in event):
            with path.open('a') as stream:
                stream.write(json.dumps({'entry': entry}) + '\n')

    def _owns(self, member):
        """The member's checkpoint stamp — its run holds the field's
        write-ownership claim."""
        claim = self.plant.claim
        return claim is not None and claim['owner'] == member.token \
            and member.key in (claim.get('holders') or set())

    def _diagnose(self, member):
        """The keyed monitor's foreign-writer diagnosis — the standing
        claim's declared monitor pulled under a fresh ?prove= nonce:
        a live endpoint that cannot prove the pair key convicts as
        `usurped`; every shape that produces no such endpoint — an
        unkeyed run, no declared monitor, the run's own monitor, a
        dead declaration — is the plain `orphaned` the conditional
        grant stands on."""
        if not member.keyed:
            return 'usurped' if self.unkeyed_convicts else 'orphaned'
        claim = self.plant.claim
        monitor = (claim or {}).get('monitor')
        if monitor is None:
            return 'usurped' if self.convicts_absent else 'orphaned'
        if monitor == member.bridge:
            return 'orphaned'
        target = self.endpoints.get(monitor)
        if target is None or not target.live:
            return 'usurped' if self.convicts_dead else 'orphaned'
        if self.never_convicts:
            return 'orphaned'
        return 'orphaned' if target.keyed else 'usurped'

    def _resolve(self, member):
        """The tracking source this scan pulls — probe-b follows its
        configured --standby source outright; a demoted probe-a
        adopted its sibling."""
        return self.a if member is self.b else self.b

    def _field_write(self, member):
        """One controller-scan field write: fenced while the member
        stands outside the standing claim's holders — the verdict
        that demotes the owner — else the write lands."""
        return 'landed' if self._owns(member) else 'fenced'

    def _supersede(self, member):
        """The contract's demote-in-place: journal the attributed
        loss and walk demoting -> standby."""
        claim = self.plant.claim or {}
        if not self.no_loss:
            self._journal(member, {'field_claim_lost': {
                'point': 200, 'claimant': claim.get('owner')}})
        if self.never_demotes:
            return
        member.role = 'demoting'
        self._journal(member, {'role_changed': {
            'from': 'active', 'to': 'demoting', 'origin': 'fenced'}})

    def _standby(self, member):
        """One standby scan — the tracking pull plus the
        foreign-writer diagnosis the keyed run layers on an
        ownerless verdict."""
        source = self._resolve(member)
        if source is not None and source.role == 'active' \
                and self._owns(source) \
                and not (self.unsettled and member is self.b):
            member.sync = 'tracking'
        else:
            member.sync = self._diagnose(member)
        # The latched-verdict defect: a member that once reported
        # usurped keeps it pinned under the keyed writer.
        if self.never_clears and member is self.b \
                and member.sync == 'tracking' \
                and getattr(member, '_latched', False):
            member.sync = 'usurped'
        if member.sync == 'usurped':
            member._latched = True

    def _scan(self, member):
        """One controller scan of the addressed member."""
        member.tick += 1
        if member.role == 'demoting':
            member.role = 'standby'
            self._journal(member, {'role_changed': {
                'from': 'demoting', 'to': 'standby',
                'origin': 'fenced'}})
            return
        if member.role == 'promoting':
            member.role = 'active'
            self._journal(member, {'role_changed': {
                'from': 'promoting', 'to': 'active',
                'origin': 'request'}})
            return
        if member.role == 'active':
            if self._field_write(member) == 'fenced':
                self._supersede(member)
            return
        self._standby(member)

    def _driven_scan(self, member):
        """One paced scan of the driven attachment — its checkpoint
        pulls, promote claim, and field writes all run inside POST
        /scan."""
        member.tick += 1
        if member.role == 'demoting':
            member.role = 'standby'
            return
        if member.role == 'promoting':
            member.role = 'active'
            return
        if member.role == 'active':
            if not self.usurper_survives \
                    and self._field_write(member) == 'fenced':
                self._supersede(member)
            return
        # The driven standby tracks the probe owner through the
        # public pull its tokenless posture allows — orphaned under
        # an ownerless source: an unkeyed run never reads usurped.
        member.sync = 'tracking' if self._owns(self.a) \
            and self.a.role == 'active' else 'orphaned'

    def _report(self, member):
        report = {'role': member.role, 'tick': member.tick,
                  'field_claim': 'held' if self.plant.claim
                  else 'unclaimed'}
        if member.role == 'standby':
            report['sync'] = member.sync \
                if member.sync == 'unsynchronized' \
                else {member.sync: {'aligned': member.tick}}
        return report

    # ---- the leg's staging levers -------------------------------

    def start_driven(self, name, keyed=True):
        """ctx['start_driven'] — the unkeyed driven attachment the
        leg launches against the probe plant: `--standby <member>
        --driven` outside the pair line."""
        self.d.live = True
        self.d.keyed = keyed
        self.d.role = 'standby'
        self.d.sync = 'unsynchronized'
        if keyed:
            self.endpoints[self.d.bridge] = self.d
        else:
            # The usurper's monitor answers but cannot prove the line —
            # still a live endpoint the diagnosis convicts.
            self.endpoints[self.d.bridge] = self.d
        return {'container': 'dcs-hw-qa-probe-d',
                'owner': self.TOKENS['driven'],
                'address': self.BRIDGE['driven']}

    def stop_driven(self):
        self.d.live = False
        self.endpoints.pop(self.d.bridge, None)

    def relaunch(self, name, track=None, share_state_with=None,
                 keyed=True):
        """ctx['relaunch_controller'] — the keyed-posture lever the
        unkeyed-run half drives: the member resumes without (or with)
        the pair token."""
        member = self.members['probe-' + ('a' if name == 'active'
                                         else 'b')]
        member.keyed = keyed
        member.role = 'standby'
        member.sync = 'unsynchronized'
        member._latched = False

    def field_request(self, request):
        """ctx['field_request'] — the raw one-shot probe against the
        probe plant's claim surface: the docker-exec CompletedProcess
        shape, the reply JSON on stdout."""
        if self.plant.down:
            return _ctl_process(stderr='no such container',
                                returncode=1)
        return _ctl_process(self.plant.probe_reply())

    def hold_field_claim(self, request):
        """ctx['hold_field_claim'] — the held foreign claim: a
        labeled bridge attachment's claim_writer kept open so the
        holder set stays occupied."""
        if self.plant.down:
            return {'container': 'dcs-hw-qa-probe-claimhold',
                    'address': self.HOLDER_ADDRESS,
                    'plant': self.PLANT_ADDRESS,
                    'request': request, 'reply': None}
        reply = self.plant.hold(request)
        return {'container': 'dcs-hw-qa-probe-claimhold',
                'address': self.HOLDER_ADDRESS,
                'plant': self.PLANT_ADDRESS,
                'request': request, 'reply': reply}

    def drop_field_claim(self):
        """ctx['drop_field_claim'] — the holder's end: the claim
        stands holderless."""
        self.plant.drop()

    # ---- the served monitor surface -----------------------------

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        member = self.members.get(host.split(':')[0])
        if member is None:
            raise urllib.error.URLError('unknown host ' + host)
        if self.silent or not member.live:
            raise urllib.error.URLError('unreachable')
        if member is self.d:
            if (method, route) == ('POST', '/scan'):
                for _ in range((body or {}).get('scans', 1)):
                    self._driven_scan(member)
                return 200, {'scans': (body or {}).get('scans', 1)}
            # A driven member's other reads never advance its scan.
            if (method, route) == ('GET', '/role'):
                return 200, self._report(member)
            if (method, route) == ('POST', '/promote'):
                return self._promote(member)
            raise AssertionError('unhandled ' + url)
        self._scan(member)
        if (method, route) == ('GET', '/role'):
            return 200, self._report(member)
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) \
                if query.startswith('since=') else 0
            return 200, [entry for entry in member.journal
                         if entry['seq'] > since]
        if (method, route) == ('GET', '/checkpoint'):
            return 200, {'tick': member.tick, 'generation': 7,
                         'source_owns_field': self._owns(member)}
        if (method, route) == ('POST', '/promote'):
            return self._promote(member)
        if (method, route) == ('POST', '/demote'):
            if member.role in ('active', 'promoting'):
                if self._owns(member):
                    self.plant.claim['holders'].discard(member.key)
                member.role = 'demoting'
                self._journal(member, {'role_changed': {
                    'from': 'active', 'to': 'demoting',
                    'origin': 'request'}})
            return 200, self._report(member)
        raise AssertionError('unhandled ' + url)

    def _promote(self, member):
        """POST /promote on a standby run — the unconditional claim
        under tracking or usurped (the granted usurped claim
        journaling foreign_claim_preempted naming the endpoint the
        field was taken from), the conditional grant under orphaned —
        refused while a live different-owner incumbent stands."""
        if self.promote_refused \
                or (self.usurper_refused and member is self.d):
            return 500, {'error': 'refused'}
        if member.role != 'standby':
            return 409, 'already_active'
        if member.sync == 'unsynchronized':
            return 409, {'not_converged': {'sync': 'unsynchronized'}}
        if member.sync == 'orphaned' \
                or (member.sync == 'usurped'
                    and self.conditional_reclaim):
            if not self.plant.conditional_grant(member):
                return 409, {'field_claim_failed': {
                    'detail': 'a live controller holds the field\'s '
                              'write-ownership claim'}}
        taken_from = self.plant.claim_writer(member)
        if member.sync == 'usurped' and taken_from is not None \
                and not self.silent_preempt:
            self._journal(member, {'foreign_claim_preempted': {
                'writer': self.wrong_writer or taken_from}})
        member.role = 'promoting'
        member.was_owner = True
        self._journal(member, {'role_changed': {
            'from': 'standby', 'to': 'promoting',
            'origin': 'request'}})
        return 200, self._report(member)


class UsurpedForeignClaimTests(unittest.TestCase):
    """The usurped-claim scenario against the stubbed probe pair: a
    clean rig passes with identical digests — the unkeyed driven
    usurper preempting the keyed owner into the `usurped` verdict
    beside field_claim held, the keyed promote reclaiming through the
    unconditional claim with the journaled foreign_claim_preempted
    naming the usurper's endpoint, the sibling's verdict clearing to
    tracking, and each absence half keeping orphaned plus the
    conditional refusal — each doctored defect reports the named
    diagnostic, and the absent-subject or predating-surface rig is
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_dir = Path(self.tmp.name) / 'journals'
        self.journal_dir.mkdir()
        self.plant = UsurpedPlant(UsurpedPairFeed.TOKENS['active'],
                                  UsurpedPairFeed.BRIDGE['active'])
        self.feed = UsurpedPairFeed(
            self.plant, journal_files=self._journal_files())

    def tearDown(self):
        self.tmp.cleanup()

    def _journal_files(self, subdir=''):
        base = self.journal_dir / subdir if subdir \
            else self.journal_dir
        Path(base).mkdir(parents=True, exist_ok=True)
        return {key: str(Path(base) / (key + '.jsonl'))
                for key in ('active', 'standby', 'driven')}

    def _subject(self, **overrides):
        subject = {'active': 'http://probe-a:1',
                   'standby': 'http://probe-b:2',
                   'driven': 'http://probe-d:3',
                   'revised': None, 'foreign': None, 'plant': None,
                   'pair_token': 'dcs-qa-pair',
                   'plant_owner': dict(self.feed.TOKENS),
                   'journal_files': self._journal_files(),
                   'field_request': self.feed.field_request,
                   'hold_field_claim': self.feed.hold_field_claim,
                   'drop_field_claim': self.feed.drop_field_claim,
                   'start_driven': self.feed.start_driven,
                   'stop_driven': self.feed.stop_driven,
                   'relaunch_controller': self.feed.relaunch}
        subject.update(overrides)
        return subject

    def _ctx(self, **subject_overrides):
        probe = subject_overrides.pop('probe', ...)
        return {'evidence_dir': str(self.evidence),
                'probe': (self._subject(**subject_overrides)
                          if probe is ... else probe)}

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        ctx = ctx or self._ctx()
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'USURPED_SETTLE', 2), \
                patch.object(scenarios, 'USURPED_POLL', 0.001), \
                patch.object(scenarios, 'USURPED_ROUNDS', 2):
            return scenarios.scenario_usurped_foreign_claim(ctx)

    def test_registered(self):
        self.assertIn(
            scenarios.scenario_usurped_foreign_claim,
            scenarios.SCENARIOS)

    def test_clean_passes_and_validates(self):
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('passed', record['outcome'], record)
        # The launch layout restored: probe-a owns the field again
        # and probe-b tracks it.
        self.assertEqual('active', self.feed.a.role)
        self.assertEqual('standby', self.feed.b.role)
        self.assertEqual('tracking', self.feed.b.sync)
        self.assertTrue(self.feed.b.keyed)
        self.assertEqual(
            self.feed.TOKENS['active'],
            (self.plant.claim or {}).get('owner'))
        # The staged foreign claims: the raw seam's monitor-less and
        # dead-declared claim_writers plus the unkeyed-run half's
        # live-monitor claim — each under the leg's foreign token,
        # never a rig pin.
        foreign = [request for request in self.plant.requests
                   if request.get('op') == 'claim_writer'
                   and request.get('owner')
                   == scenarios.FOREIGN_OWNER]
        self.assertEqual(6, len(foreign))   # three halves, two passes
        # The usurped reclaims journaled the preemption naming the
        # usurper's endpoint — once in the usurped half's reclaim and
        # once in the unkeyed half's restore, each pass: the claim it
        # preempted still declared the usurper's live monitor.
        preempted = [entry['event']['foreign_claim_preempted']
                     for entry in self.feed.a.journal
                     if 'foreign_claim_preempted'
                     in (entry.get('event') or {})]
        self.assertEqual([{'writer': self.feed.BRIDGE['driven']}] * 4,
                         preempted)

    def test_never_convicts_reports_failed(self):
        # The pre-#1410 shape: a live foreign writer beside a held
        # field claim reads plain orphaned — the reclaim's premise.
        self.feed.never_convicts = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-failed',
                      record.get('detail', ''))
        self.assertIn('usurped', record.get('detail', ''))

    def test_field_unclaimed_reports_failed(self):
        # The defect the verdict must not be asserted beside: the
        # report claiming the field unclaimed while a writer stands.
        report_orig = self.feed._report

        def lying_report(member):
            body = report_orig(member)
            if member is self.feed.a and member.role == 'standby':
                body['field_claim'] = 'unclaimed'
            return body
        self.feed._report = lying_report
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-failed',
                      record.get('detail', ''))
        self.assertIn('field_claim', record.get('detail', ''))

    def test_conditional_reclaim_reports_failed(self):
        # The orphaned verdict's gate on a usurped run: the live
        # foreign incumbent refuses the conditional grant forever.
        self.feed.conditional_reclaim = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-failed',
                      record.get('detail', ''))
        self.assertIn('unconditional', record.get('detail', ''))

    def test_silent_preempt_reports_failed(self):
        self.feed.silent_preempt = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-failed',
                      record.get('detail', ''))
        self.assertIn('foreign_claim_preempted',
                      record.get('detail', ''))

    def test_wrong_writer_reports_failed(self):
        self.feed.wrong_writer = '172.18.0.9:8092'
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-failed',
                      record.get('detail', ''))
        self.assertIn('writer', record.get('detail', ''))

    def test_usurper_survives_reports_failed(self):
        # The reclaimed peer reports active while the fenced writer
        # never walked down — the reclaim fenced nothing.
        self.feed.usurper_survives = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-failed',
                      record.get('detail', ''))

    def test_latched_usurped_reports_failed(self):
        # The unconditional-preemptor defect: the sibling keeps
        # reporting usurped under a keyed field owner.
        self.feed.never_clears = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-failed',
                      record.get('detail', ''))

    def test_monitorless_convicted_reports_failed(self):
        # A claim declaring no monitor convicts nothing — the
        # diagnosed-usurped reading the honest-absence half forbids.
        self.feed.convicts_absent = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-failed',
                      record.get('detail', ''))
        self.assertIn('monitor-less', record.get('detail', ''))

    def test_dead_convicted_reports_failed(self):
        # A dead declared endpoint cannot answer a ?prove= pull — no
        # conviction may stand on it.
        self.feed.convicts_dead = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-failed',
                      record.get('detail', ''))
        self.assertIn('dead', record.get('detail', ''))

    def test_live_holder_preempted_reports_failed(self):
        # The conditional grant ignoring a live foreign holder — the
        # unconditional preemption the absent evidence must never arm.
        self.plant.conditional_preempts = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-failed',
                      record.get('detail', ''))

    def test_unkeyed_convicts_reports_failed(self):
        # The unkeyed run reporting usurped — a diagnosis a run
        # holding no key cannot produce.
        self.feed.unkeyed_convicts = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-failed',
                      record.get('detail', ''))

    def test_unkeyed_preempts_reports_failed(self):
        # The unkeyed member's promote granted against the live
        # foreign holder.
        self.plant.unkeyed_preempts = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-failed',
                      record.get('detail', ''))

    def test_never_demotes_reports_failed(self):
        self.feed.never_demotes = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-failed',
                      record.get('detail', ''))

    def test_no_loss_reports_failed(self):
        self.feed.no_loss = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('field_claim_lost', record.get('detail', ''))

    def test_broken_restore_reports_failed(self):
        # The holderless claim's ordinary-grant restore never lands —
        # the conditional grant refuses the holderless foreign claim
        # it exists to preempt.
        self.plant.refuses_holderless = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-failed',
                      record.get('detail', ''))
        self.assertIn('holderless', record.get('detail', ''))

    def test_usurper_refused_reports_nondeterministic(self):
        # The staged preemption never landed — the unkeyed
        # attachment's promote met a refusal, so the pass judged
        # nothing.
        self.feed.usurper_refused = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-nondeterministic',
                      record.get('detail', ''))

    def test_durable_silent_reports_nondeterministic(self):
        # The served journal carries the preemption but the durable
        # --journal-file mirror dropped it — the file audit cannot
        # confirm.
        self.feed.silent_durable = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-nondeterministic',
                      record.get('detail', ''))

    def test_diverging_digests_report_nondeterministic(self):
        digests = iter(({'pass': 1}, {'pass': 2}))
        with patch.object(
                scenarios, '_usurped_digest',
                lambda record, violations: next(digests)):
            record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('diverged', record.get('detail', ''))

    def test_silent_judge_reports_unchecked(self):
        # A judge silenced outright must trip the unchecked
        # diagnostic — every planted negative slips.
        with patch.object(scenarios, '_judge_usurped',
                          lambda record, note: None):
            record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('usurped-foreign-claim-unchecked',
                      record.get('detail', ''))

    def test_no_probe_reports_inconclusive(self):
        record = self.run_scenario(ctx={'probe': None,
                                        'evidence_dir':
                                        str(self.evidence)})
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)
        self.assertIn('probe', record.get('detail', ''))

    def test_unkeyed_pair_reports_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(pair_token=None))
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)
        self.assertIn('unkeyed', record.get('detail', ''))

    def test_missing_seams_report_inconclusive(self):
        for seam in ('start_driven', 'stop_driven', 'field_request',
                     'hold_field_claim', 'drop_field_claim',
                     'relaunch_controller'):
            record = self.run_scenario(
                ctx=self._ctx(**{seam: None}))
            report.validate_scenario(record)
            self.assertEqual('inconclusive', record['outcome'],
                             (seam, record))

    def test_silent_pair_reports_inconclusive(self):
        self.feed.silent = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_probe_refused_reports_inconclusive(self):
        self.plant.down = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_unclaimed_baseline_reports_inconclusive(self):
        self.plant.claim = None
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_undeclared_monitor_reports_inconclusive(self):
        self.plant.undeclared = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)
        self.assertIn('monitor', record.get('detail', ''))

    def test_foreign_baseline_reports_inconclusive(self):
        self.plant.claim['owner'] = 424243
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_unsettled_standby_reports_inconclusive(self):
        self.feed.unsettled = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_two_runs_produce_identical_evidence(self):
        first = self.run_scenario()
        second = self.run_scenario()
        self.assertEqual('passed', first['outcome'], first)
        self.assertEqual('passed', second['outcome'], second)
        self.assertEqual(first['observations'],
                         second['observations'])
        self.assertEqual(
            [entry['ref'] for entry in first['evidence']],
            [entry['ref'] for entry in second['evidence']])


if __name__ == '__main__':
    unittest.main()
