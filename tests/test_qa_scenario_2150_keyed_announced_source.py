"""The 2150_keyed_announced_source leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_keyed_announced_source, split out per the #940 convention.
The shared fakes and helpers live in tests/qa_scenario_support.py;
EXPECTED_CASES pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'KeyedAnnouncedTests.test_registered',
    'KeyedAnnouncedTests.test_clean_probe_pair_passes_and_validates',
    'KeyedAnnouncedTests.test_no_probe_subject_reports_inconclusive',
    'KeyedAnnouncedTests.test_unkeyed_probe_subject_reports_'
    'inconclusive',
    'KeyedAnnouncedTests.test_missing_forge_action_reports_'
    'inconclusive',
    'KeyedAnnouncedTests.test_host_placed_forge_reports_inconclusive',
    'KeyedAnnouncedTests.test_unreachable_pair_reports_inconclusive',
    'KeyedAnnouncedTests.test_unconverged_pair_reports_inconclusive',
    'KeyedAnnouncedTests.test_swapped_layout_reports_inconclusive',
    'KeyedAnnouncedTests.test_unproven_peer_reports_inconclusive',
    'KeyedAnnouncedTests.test_forged_endpoint_adopted_fails',
    'KeyedAnnouncedTests.test_stale_proof_verified_fails',
    'KeyedAnnouncedTests.test_involuntary_wedge_fails',
    'KeyedAnnouncedTests.test_involuntary_held_owner_fails',
    'KeyedAnnouncedTests.test_involuntary_silent_loss_fails',
    'KeyedAnnouncedTests.test_involuntary_foreign_adoption_fails',
    'KeyedAnnouncedTests.test_lifecycle_refused_fails',
    'KeyedAnnouncedTests.test_planted_pull_applied_fails',
    'KeyedAnnouncedTests.test_silent_forge_fails',
    'KeyedAnnouncedTests.test_diverging_digests_report_'
    'nondeterministic',
    'KeyedAnnouncedTests.test_silent_judge_reports_unchecked',
    'KeyedAnnouncedTests.test_two_runs_produce_identical_evidence',
})


class KeyedAnnouncedFeed:
    """A stubbed keyed probe pair for the announced-source lifecycle
    leg: probe-a owns the probe field with no configured tracking
    source; probe-b is the tracking standby whose configured pull
    re-announces after every restart; and the `start_forge`/
    `stop_forge` ctx actions stand the hostile endpoint on the rig
    bridge — the staged document it serves is the file the action
    writes, the announce it lands is the hit record the action
    appends, and the demote verify's `?prove=` pull reads the file
    and answers signed exactly when the endpoint launched keyed.

    The keyed contract the feed models: `?prove=` answers carry a
    `line_proof` bound to the nonce; the demote verify pulls every
    announced hint, refuses `no_tracking_source` when no answer is
    signed under the pull's own nonce, and audits a signed document's
    receipt window and internal `In` samples against the owner's
    settled record — the same mini audit the 2050 feed runs. The
    involuntary half models the field-claim preempt: a misordered
    promote on the tracking standby claims the probe plant, the
    superseded owner's fenced write journals the attributed
    FieldClaimLost and walks it to standby, and the demoted peer —
    configured with no source — resolves the verified endpoint,
    journals one TrackingSourceAdopted, and rejoins tracking. Once
    adopted, each standing pull re-reads the staged document: an
    honest one applies and converges `orphaned`, a convicted one
    reports `degraded` and lands nothing. Every endpoint call is one
    completed scan, so two runs emit identical evidence. Doctor flags
    stage each named defect the issue calls out."""

    FORGE_ADDR = '172.18.0.9:8090'
    FORGE_PORT = 8090
    POINT = 10
    GENERATION = 777777
    PAIR_ADDR = {'a': 'probe-a:8080', 'b': 'probe-b:8081'}
    TOKENS = {'a': 424248, 'b': 424249}

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.tick = 100
        self.up = {'a': True, 'b': True}
        self.role = {'a': 'active', 'b': 'standby'}
        # probe-a's tracked source: None while it owns the field,
        # 'b' once it resolves the promoted peer, 'forge' once a
        # verified announced endpoint adopts it.
        self.a_track = None
        # probe-a's involuntary-demote countdowns: polls until the
        # fenced write lands, then until the verified source resolves.
        self.a_fenced = None
        self.a_rejoin = None
        # probe-a's adopted-forge convergence: polls of pulling the
        # staged document before the first converged report.
        self.a_adopted_pulls = 0
        # probe-b's configured standby pull: tracking countdown.
        self.b_track_left = 0
        self.b_demote = False
        self.receipts = []        # probe-a's settled log
        self.attempts = 0         # lifetime submissions
        self.pending = []         # (apply_tick, receipt) accepted
        self.image = {self.POINT: False}
        self.announced = []       # probe-a's recorded hints
        self.run_count = 1
        self.seq = 0
        self.forge = None
        self.pair_token = 'probe-pair'
        self.journal_a = self.tmp / 'journal-probe-a.jsonl'
        self.journal_b = self.tmp / 'journal-probe-b.jsonl'
        self.journal_a.write_text(json.dumps(
            {'run_boundary': {'run': 1, 'tick': self.tick}}) + '\n')
        self.journal_b.write_text(json.dumps(
            {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
        # Fault injection — each named failure the issue calls out.
        self.adopted_forged = False   # the forged signed document arms
                                      # the demotion — the verify audit
                                      # regressed
        self.stale_verified = False   # a replayed line_proof verifies
                                      # under a rolled nonce window
        self.journaled_adoption = False  # a refused demote still
                                         # journals the adoption
        self.role_moved = False       # a refused demote still demotes
        self.honest_refused = False   # the honest document is refused
                                      # — the follow-peer path closed
        self.adoption_silent = False  # the honest demote adopts
                                      # unjournaled
        self.never_converges = False  # the adopted peer strands
                                      # unsynchronized
        self.planted_applied = False  # the planted document applies
                                      # on the standing pull path
        self.pull_journaled = False   # the hostile pull journals a
                                      # switch
        self.promote_refused = False  # the reconverged peer cannot
                                      # re-promote
        self.forge_silent = False     # the staged document never
                                      # answers a pull
        self.no_demote = False        # the superseded owner holds
                                      # 'active' — the fenced write
                                      # never demotes it
        self.loss_silent = False      # no field_claim_lost journaled
        self.loss_misattributed = False
        self.no_rejoin = False        # the demoted peer wedges
                                      # unsynchronized
        self.adoption_missing = False  # rejoins with no journaled
                                       # adoption
        self.adoption_foreign = False  # the journaled adoption names
                                       # the forge's port
        self.slow_rejoin = False      # the rejoin lands beyond the
                                      # lane's tick bound
        self.unreachable = False
        self.no_active = False
        self.no_tracking = False
        self.unproven_peer = False    # probe-b answers ?prove= with
                                      # no line_proof — pre-contract

    # ---- journal + hits ledgers ----------------------------------

    def _journal(self, name, event, body):
        self.seq += 1
        record = {'entry': {'seq': self.seq, 'tick': self.tick,
                            'event': {event: body}}}
        path = {'a': self.journal_a, 'b': self.journal_b}[name]
        with open(path, 'a') as handle:
            handle.write(json.dumps(record) + '\n')

    def _hit(self, record):
        if self.forge is None:
            return
        with open(self.forge['hits'], 'a') as handle:
            handle.write(json.dumps(record) + '\n')

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://pair', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    # ---- the runner-owned lifecycle actions ----------------------

    def stop_controller(self, name):
        self.up[{'active': 'a', 'standby': 'b'}[name]] = False

    def start_controller(self, name):
        peer = {'active': 'a', 'standby': 'b'}[name]
        self.up[peer] = True
        if peer == 'b':
            self.b_track_left = 2

    def restart_controller(self, name):
        # The warm restart: the journal carries the run boundary, the
        # persisted state resumes — role, receipts, image — and the
        # fresh monitor's announced-hint set begins empty.
        self.run_count += 1
        if name == 'active':
            self.announced = []
        path = {'active': self.journal_a,
                'standby': self.journal_b}[name]
        with open(path, 'a') as handle:
            handle.write(json.dumps({'run_boundary': {
                'run': self.run_count, 'tick': self.tick}}) + '\n')

    def start_forge(self, document, owner, keyed=True):
        directory = self.tmp / 'forge'
        directory.mkdir(exist_ok=True)
        document_path = directory / 'checkpoint.json'
        document_path.write_text(json.dumps(document))
        hits = directory / 'hits.jsonl'
        hits.write_text('')
        self.forge = {'container': 'dcs-hw-qa-1-probe-forge',
                      'dir': str(directory),
                      'document': str(document_path),
                      'hits': str(hits),
                      'keyed': bool(keyed and self.pair_token),
                      'port': self.FORGE_PORT}
        # The endpoint's first announce pull already answered — the
        # owner recorded its hint.
        self.announced = [self.FORGE_ADDR]
        self._hit({'kind': 'announce', 'target': 'probe-a:8080',
                   'ok': True})
        return self.forge

    def stop_forge(self):
        self.forge = None

    # ---- the scan advance ----------------------------------------

    def _advance(self):
        self.tick += 1
        settled = []
        for apply_tick, receipt in self.pending:
            if self.tick >= apply_tick:
                write = receipt['command']['write_value']
                self.image[write['point']] = write['value']['bool']
                receipt['outcome'] = {'applied': {'tick': self.tick}}
                self._journal('a', 'command_settled',
                              {'receipt': receipt})
                settled.append((apply_tick, receipt))
        for item in settled:
            self.pending.remove(item)

    # ---- the announced-source demote verify -----------------------

    def _receipted(self, receipts, doc_base, point):
        """The newest applied write `point` in the pulled document's
        window that post-dates the owner's settled tail — the
        receipted-cause override the internal audit consults."""
        own_base = self.attempts - len(self.receipts)
        for offset in range(len(receipts) - 1, -1, -1):
            if doc_base + offset < own_base:
                continue
            receipt = receipts[offset]
            if 'applied' not in (receipt.get('outcome') or {}):
                continue
            write = (receipt.get('command') or {}).get('write_value')
            if isinstance(write, dict) and write.get('point') == point:
                return (write.get('value') or {}).get('bool')
        return None

    def _audit(self, document):
        """The mini command-record audit a verified pull runs on a
        signed document: the receipt window must agree with the
        owner's settled log at covered indices, and every internal
        `In` sample must match the held image or a receipted write
        the owner never produced convicts it."""
        receipts = document.get('receipts') or []
        attempts = (document.get('command_admission') or {}) \
            .get('attempts') or 0
        doc_base = attempts - len(receipts)
        own_base = self.attempts - len(self.receipts)
        for offset, receipt in enumerate(receipts):
            position = doc_base + offset - own_base
            if not 0 <= position < len(self.receipts):
                continue
            own = self.receipts[position]
            if own.get('command') != receipt.get('command') \
                    and 'accepted' not in (own.get('outcome') or {}):
                return 'fork'
        if doc_base + len(receipts) < own_base + len(self.receipts):
            return 'ok'   # a stale view skips the internal audit
        for key, sample in (document.get('internal') or {}).items():
            point = int(key)
            value = ((sample or {}).get('value') or {}).get('bool')
            held = self.image.get(point)
            if isinstance(value, bool) and held is not None \
                    and value != held \
                    and self._receipted(receipts, doc_base, point) \
                    != value:
                return 'planted'
        return 'ok'

    def _staged(self):
        """The document the forge's staged file currently serves."""
        try:
            return json.loads(
                Path(self.forge['document']).read_text())
        except Exception:
            return None

    def _verify_pull(self):
        """The demote verify's `?prove=` pull on the announced forge:
        the staged document re-read per pull, the serve hit ledgered
        under a freshly minted nonce, the keyed signature answered
        only when the endpoint holds the pair's token — then the
        replayed-proof and document checks."""
        if self.forge is None or self.forge_silent:
            return 'dead'
        document = self._staged()
        if document is None:
            return 'dead'
        nonce = self.tick + 1   # every verify pull mints a fresh one
        signed = bool(self.forge['keyed'])
        self._hit({'kind': 'serve', 'remote': 'probe-a:8080',
                   'query': 'prove=%d' % nonce,
                   'signed': signed, 'status': 200})
        proven = signed
        if document.get('line_proof') is not None \
                and self.stale_verified:
            # The replayed proof verifies — the nonce window never
            # rolled: the regression the replay leg names.
            proven = True
        if not proven:
            return 'unproven'
        if document.get('generation') != self.GENERATION:
            return 'foreign'
        verdict = self._audit(document)
        if verdict != 'ok' and self.adopted_forged:
            return 'ok'
        return verdict

    def _demote_a(self):
        if self.role['a'] != 'active':
            self._raise(409, 'not_active')
        if not self.announced:
            self._raise(409, 'no_tracking_source')
        verdict = self._verify_pull()
        if self.honest_refused and verdict == 'ok':
            verdict = 'unproven'
        if verdict != 'ok':
            if self.journaled_adoption:
                self._journal('a', 'tracking_source_adopted',
                              {'source': self.FORGE_ADDR})
            if self.role_moved:
                self._journal('a', 'role_changed', {
                    'from': 'active', 'to': 'demoting'})
                self.role['a'] = 'standby'
            self._raise(409, 'no_tracking_source')
        # The verified adoption: the journaled source, the role
        # transition, and the demoted peer following the announced
        # endpoint's standing pulls.
        if not self.adoption_silent:
            self._journal('a', 'tracking_source_adopted',
                          {'source': self.FORGE_ADDR})
        self._journal('a', 'role_changed', {'from': 'active',
                                            'to': 'demoting'})
        self.role['a'] = 'standby'
        self.a_track = 'forge'
        self.a_adopted_pulls = 0
        return 200, {'role': 'demoting', 'tick': self.tick}

    def _apply_pull(self):
        """The adopted peer's standing pull on the staged document:
        every pull is proof-bearing — an unsigned answer resets the
        track — and an applied document lands its internal image; a
        convicted one degrades the sync and lands nothing."""
        if self.forge is None or self.forge_silent:
            return 'dead'
        document = self._staged()
        if document is None:
            return 'dead'
        signed = bool(self.forge['keyed'])
        self._hit({'kind': 'serve', 'remote': 'probe-a:8080',
                   'query': 'prove=%d' % self.tick,
                   'signed': signed, 'status': 200})
        if not signed:
            return 'unproven'
        verdict = self._audit(document)
        if verdict == 'planted' and self.planted_applied:
            sample = (document.get('internal') or {}) \
                .get(str(self.POINT)) or {}
            value = (sample.get('value') or {}).get('bool')
            if isinstance(value, bool):
                self.image[self.POINT] = value
            if self.pull_journaled:
                self._journal('a', 'tracking_source_adopted',
                              {'source': self.FORGE_ADDR})
            return 'ok'
        if verdict == 'ok':
            for key, sample in (document.get('internal')
                                or {}).items():
                value = ((sample or {}).get('value') or {}) \
                    .get('bool')
                if isinstance(value, bool):
                    self.image[int(key)] = value
        return verdict

    # ---- the involuntary demote ----------------------------------

    def _promote_b(self):
        # The misordered promote: promotable from the tracking
        # standby — the owner is never asked first.
        if self.role['b'] == 'active':
            self._raise(409, 'already_active')
        if self.b_track_left > 0 or self.no_tracking \
                or self.role['b'] != 'standby':
            self._raise(409, 'not_converged')
        self._journal('b', 'role_changed', {'from': 'standby',
                                            'to': 'promoting'})
        self.role['b'] = 'promoting'
        # The claim preempt lands on the next scan — the owner's
        # first fenced write then demotes it in place.
        self.a_fenced = 1
        return 200, {'role': 'promoting', 'tick': self.tick}

    def _promote_a(self):
        # Promotable from any converged standby — tracked or
        # orphaned onto the adopted line.
        if self.role['a'] != 'standby' or self.a_track is None \
                or (self.a_track == 'forge'
                    and self.a_adopted_pulls == 0) \
                or self.promote_refused:
            self._raise(409, 'not_converged')
        self._journal('a', 'role_changed', {'from': 'standby',
                                            'to': 'promoting'})
        self.role['a'] = 'promoting'
        self.a_track = None
        return 200, {'role': 'promoting', 'tick': self.tick}

    def _demote_b(self):
        if self.role['b'] != 'active':
            self._raise(409, 'not_active')
        self._journal('b', 'role_changed', {'from': 'active',
                                            'to': 'demoting'})
        self.role['b'] = 'standby'
        self.b_track_left = 2
        return 200, {'role': 'demoting', 'tick': self.tick}

    # ---- the served reports and documents -------------------------

    def _role_a(self):
        if self.no_active:
            return {'role': 'standby', 'tick': self.tick,
                    'sync': 'unsynchronized'}
        if self.role['a'] == 'promoting':
            self.role['a'] = 'active'
            return {'role': 'active', 'tick': self.tick}
        if self.a_fenced is not None:
            # The fenced write lands this scan: the attributed loss
            # journals and the role walks demoting -> standby.
            if self.a_fenced > 0:
                self.a_fenced -= 1
                if not self.loss_silent:
                    claimant = 999999 if self.loss_misattributed \
                        else self.TOKENS['b']
                    self._journal('a', 'field_claim_lost', {
                        'point': self.POINT, 'claimant': claimant})
                self._journal('a', 'role_changed', {
                    'from': 'active', 'to': 'demoting'})
                if not self.no_demote:
                    self.role['a'] = 'demoting'
                return {'role': 'demoting' if not self.no_demote
                        else 'active', 'tick': self.tick}
            if not self.no_demote:
                self.role['a'] = 'standby'
                self.a_track = None
                self.a_rejoin = 70 if self.slow_rejoin else 2
            self.a_fenced = None
            return {'role': self.role['a'], 'tick': self.tick,
                    'sync': 'unsynchronized'
                    if self.role['a'] == 'standby' else None}
        report = {'role': self.role['a'], 'tick': self.tick}
        if self.role['a'] != 'standby':
            return report
        if self.a_rejoin is not None:
            # The verified-source resolution under keying: a bounded
            # countdown to the journaled adoption and the tracking
            # rejoin — or the wedge the contract names.
            if self.no_rejoin:
                report['sync'] = 'unsynchronized'
                return report
            self.a_rejoin -= 1
            if self.a_rejoin > 0:
                report['sync'] = 'unsynchronized'
                return report
            if self.adoption_foreign:
                self._journal('a', 'tracking_source_adopted',
                              {'source': self.FORGE_ADDR})
            elif not self.adoption_missing:
                self._journal('a', 'tracking_source_adopted',
                              {'source': self.PAIR_ADDR['b']})
            self.a_track = 'b'
            self.a_rejoin = None
            report['sync'] = {'tracking': {'aligned': self.tick}}
            return report
        if self.a_track == 'b':
            report['sync'] = {'tracking': {'aligned': self.tick}}
            return report
        if self.a_track == 'forge':
            verdict = self._apply_pull()
            self.a_adopted_pulls += 1
            if self.never_converges:
                report['sync'] = 'unsynchronized'
            elif verdict in ('planted', 'fork'):
                report['sync'] = {'degraded': {
                    'detail': 'unaccounted %s' % verdict}}
            elif verdict == 'ok':
                report['sync'] = {'orphaned': {
                    'aligned': self.tick}}
            else:
                report['sync'] = 'unsynchronized'
            return report
        report['sync'] = 'unsynchronized'
        return report

    def _role_b(self):
        if self.role['b'] == 'promoting':
            self.role['b'] = 'active'
            return {'role': 'active', 'tick': self.tick}
        report = {'role': self.role['b'], 'tick': self.tick}
        if self.role['b'] == 'standby':
            if self.b_track_left > 0:
                self.b_track_left -= 1
            tracking = self.b_track_left == 0 \
                and not self.no_tracking and self.up['a']
            report['sync'] = {'tracking': {'aligned': self.tick}} \
                if tracking else 'unsynchronized'
            if tracking and self.PAIR_ADDR['b'] not in self.announced:
                self.announced.append(self.PAIR_ADDR['b'])
        return report

    def _checkpoint_a(self):
        if self.a_track == 'b':
            # The adopted line's document — the tracked line names
            # the promoted peer's field ownership.
            return {'format_version': 1,
                    'model_fingerprint': 'keyed-fp',
                    'generation': self.GENERATION,
                    'tick': self.tick,
                    'source_owns_field': True,
                    'line_owner': self.PAIR_ADDR['b']}
        return {
            'format_version': 1,
            'model_fingerprint': 'keyed-fp',
            'generation': self.GENERATION,
            'tick': self.tick,
            'components': {}, 'driver': None, 'outputs': {},
            'internal': {str(self.POINT): {
                'value': {'bool': self.image[self.POINT]},
                'quality': 'good', 'tick': self.tick}},
            'forces': {},
            'receipts': list(self.receipts),
            'command_admission': {'attempts': self.attempts,
                                  'full_rejections': 0,
                                  'high_water': 0},
            'source_owns_field': self.role['a'] == 'active',
            'line_owner': self.PAIR_ADDR['a']
            if self.role['a'] == 'active' else None}

    def _checkpoint_b(self, query):
        document = {
            'format_version': 1,
            'model_fingerprint': 'keyed-fp',
            'generation': self.GENERATION,
            'tick': self.tick,
            'internal': {str(self.POINT): {
                'value': {'bool': self.image[self.POINT]},
                'quality': 'good', 'tick': self.tick}},
            'receipts': list(self.receipts),
            'command_admission': {'attempts': self.attempts,
                                  'full_rejections': 0,
                                  'high_water': 0},
            'source_owns_field': self.role['b'] == 'active',
            'line_owner': self.PAIR_ADDR['b']
            if self.role['b'] == 'active' else None}
        if 'prove=' in query and not self.unproven_peer:
            nonce = query.partition('prove=')[2].split('&')[0]
            document['line_proof'] = 'proof-%s' % nonce
        return document

    def _admit_a(self, body):
        self.attempts += 1
        write = (body or {}).get('command', {}).get('write_value', {})
        if self.role['a'] == 'active' and write.get('point') \
                == self.POINT:
            receipt = {'command': body['command'],
                       'actor': body.get('actor'),
                       'outcome': {'accepted': {
                           'apply_tick': self.tick + 1}}}
            self.pending.append((self.tick + 1, receipt))
        else:
            receipt = {'command': (body or {}).get('command'),
                       'actor': (body or {}).get('actor'),
                       'outcome': {'rejected': {'reason': {
                           'not_active': {'point': write.get('point'),
                                          'role': self.role['a']}}}}}
            self._journal('a', 'command_settled', {'receipt': receipt})
        self.receipts.append(receipt)
        return receipt

    # ---- the endpoint dispatch ------------------------------------

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        host = url.split('/')[2]
        peer = {'probe-a:1': 'a', 'probe-b:2': 'b'}[host]
        if not self.up[peer]:
            raise urllib.error.URLError('connection refused')
        self._advance()
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if (method, route) == ('GET', '/role'):
            return 200, self._role_a() if peer == 'a' \
                else self._role_b()
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': self.POINT, 'signal': None,
                 'name': 'p101-oos', 'direction': 'in',
                 'value_type': 'bool', 'writable': True}]}
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': self.tick, 'points': [
                {'point': self.POINT, 'sample': {
                    'value': {'bool': self.image[self.POINT]},
                    'quality': 'good'}}]}
        if (method, route) == ('GET', '/checkpoint'):
            return 200, self._checkpoint_a() if peer == 'a' \
                else self._checkpoint_b(query)
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts) if peer == 'a' else []
        if (method, route) == ('POST', '/command'):
            if peer == 'a':
                return 200, self._admit_a(body)
            self._raise(409, 'not_active')
        if (method, route) == ('POST', '/demote'):
            if peer == 'a':
                return self._demote_a()
            return self._demote_b()
        if (method, route) == ('POST', '/promote'):
            if peer == 'a':
                return self._promote_a()
            return self._promote_b()
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class KeyedAnnouncedTests(unittest.TestCase):
    """The keyed announced-source lifecycle leg against the stubbed
    probe pair: a clean rig passes with identical digests and
    evidence — the involuntary demote resolving its verified source,
    the forged keyed endpoint and the replayed proof each refusing
    no_tracking_source, the honest lifecycle completing through the
    hostile restage — each doctored defect reports the named
    diagnostic, and a probe-less, unkeyed, unreachable, or
    forge-less run is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = KeyedAnnouncedFeed(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def probe_ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://probe-a:1',
               'standby': 'http://probe-b:2',
               'driven': 'http://probe-d:3',
               'revised': None, 'foreign': None, 'plant': None,
               'evidence_dir': str(self.evidence),
               'pair_token': feed.pair_token,
               'plant_owner': {'active': feed.TOKENS['a'],
                               'standby': feed.TOKENS['b'],
                               'driven': 424250},
               'endpoint_placement': {'forge': 'bridge'},
               'journal_files': {'active': str(feed.journal_a),
                                 'standby': str(feed.journal_b)},
               'stop_controller': feed.stop_controller,
               'start_controller': feed.start_controller,
               'restart_controller': feed.restart_controller,
               'start_forge': feed.start_forge,
               'stop_forge': feed.stop_forge}
        ctx.update(overrides)
        return ctx

    def ctx(self, feed=None, **overrides):
        # The deployed ctx: unkeyed — the leg must never select it;
        # the probe subject it re-points at is the keyed pair.
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'pair_token': None,
               'probe': self.probe_ctx(feed)}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, **overrides):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'KEYED_SETTLE', 1.0), \
                patch.object(scenarios, 'KEYED_POLL', 0.001), \
                patch.object(scenarios, 'ANNOUNCE_SETTLE', 1.0):
            return scenarios.scenario_keyed_announced_source(
                self.ctx(feed, **overrides))

    def test_registered(self):
        order = [function.__name__ for function
                 in scenarios.SCENARIOS]
        self.assertLess(
            order.index('scenario_demote_forged_standby_source'),
            order.index('scenario_keyed_announced_source'))
        self.assertIs(verify.case_function('keyed-announced-source'),
                      scenarios.scenario_keyed_announced_source)

    def test_clean_probe_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertIn('probe pair',
                      ' '.join(record['observations']))
        for name in ('keyed-announced-signals.json',
                     'keyed-announced-pass-1.json',
                     'keyed-announced-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads((self.evidence / name).read_text())
                  for name in ('keyed-announced-pass-1.json',
                               'keyed-announced-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'involuntary': 'resolved', 'forged_verify': 'refused',
             'stale_proof': 'refused', 'lifecycle': 'completed',
             'forged_pull': 'refused', 'roles': 'restored'})
        legs = passes[0]['legs']
        self.assertEqual(legs['involuntary']['promote']['status'],
                         200)
        self.assertEqual(
            legs['involuntary']['tracking_source_adopted'],
            [{'source': 'probe-b:8081'}])
        for key in ('forged_verify', 'stale_proof'):
            self.assertEqual(legs[key]['demote']['status'], 409, key)
            self.assertEqual(legs[key]['demote']['body'],
                             'no_tracking_source', key)
            self.assertEqual(legs[key]['journaled_adoptions'], [],
                             key)
            self.assertEqual(legs[key]['journaled_role_changes'],
                             [], key)
        self.assertTrue(legs['forged_verify']['verify_pulls'][0]
                        .get('signed'))
        self.assertFalse(legs['stale_proof']['verify_pulls'][0]
                         .get('signed'))
        self.assertIsNotNone(legs['stale_proof']['staged_document']
                             ['line_proof'])
        self.assertEqual(legs['lifecycle']['demote']['status'], 200)
        self.assertEqual(
            len(legs['lifecycle']['journaled_adoptions']), 1)
        self.assertTrue(
            legs['lifecycle']['journaled_adoptions'][0]['source']
            .endswith(':8090'))
        self.assertIsNotNone(legs['forged_pull']['degraded'])
        self.assertEqual(legs['forged_pull']['samples'],
                         [True] * len(legs['forged_pull']['samples']))
        report.validate_scenario(record)

    def test_no_probe_subject_reports_inconclusive(self):
        record = self.run_scenario(probe=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('probe', record['detail'])
        report.validate_scenario(record)

    def test_unkeyed_probe_subject_reports_inconclusive(self):
        record = self.run_scenario(probe=self.probe_ctx(
            pair_token=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('probe', record['detail'])
        report.validate_scenario(record)

    def test_missing_forge_action_reports_inconclusive(self):
        probe = self.probe_ctx()
        probe['start_forge'] = None
        record = self.run_scenario(probe=probe)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('start_forge', record['detail'])
        report.validate_scenario(record)

    def test_host_placed_forge_reports_inconclusive(self):
        probe = self.probe_ctx(
            endpoint_placement={'forge': 'host'})
        record = self.run_scenario(probe=probe)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('bridge', record['detail'])
        report.validate_scenario(record)

    def test_unreachable_pair_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_unconverged_pair_reports_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])
        report.validate_scenario(record)

    def test_swapped_layout_reports_inconclusive(self):
        # probe-b holds the field and cannot hand it back — the leg
        # needs the unconfigured peer owning for the involuntary
        # adoption it owes, and the layout restore is refused.
        self.feed.role = {'a': 'standby', 'b': 'active'}
        self.feed.a_track = 'b'
        self.feed.promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('launch layout', record['detail'])
        report.validate_scenario(record)

    def test_unproven_peer_reports_inconclusive(self):
        # The keyed peer answers ?prove= with no line_proof — the
        # capture lever the replay leg needs is absent.
        self.feed.unproven_peer = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('line_proof', record['detail'])
        report.validate_scenario(record)

    def test_forged_endpoint_adopted_fails(self):
        # The keyed endpoint's convicted document arms the demotion
        # — the doctored negative the issue names: asserting the
        # forged keyed endpoint accepted fails the run by name.
        self.feed.adopted_forged = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'keyed-announced-source-failed'), record['detail'])
        self.assertIn('no_tracking_source', record['detail'])
        report.validate_scenario(record)

    def test_stale_proof_verified_fails(self):
        # The replayed line_proof verifies under the rolled nonce —
        # the second doctored negative the issue names.
        self.feed.stale_verified = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'keyed-announced-source-failed'), record['detail'])
        self.assertIn('no_tracking_source', record['detail'])
        report.validate_scenario(record)

    def test_involuntary_wedge_fails(self):
        self.feed.no_rejoin = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'keyed-announced-source-failed'), record['detail'])
        self.assertIn('re-joined', record['detail'])
        report.validate_scenario(record)

    def test_involuntary_held_owner_fails(self):
        # The superseded peer holds 'active' alongside the promoted
        # one — the demote-in-place the contract names never lands.
        self.feed.no_demote = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'keyed-announced-source-failed'), record['detail'])
        self.assertIn('demote-in-place', record['detail'])
        report.validate_scenario(record)

    def test_involuntary_silent_loss_fails(self):
        self.feed.loss_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'keyed-announced-source-failed'), record['detail'])
        self.assertIn('field_claim_lost', record['detail'])
        report.validate_scenario(record)

    def test_involuntary_foreign_adoption_fails(self):
        self.feed.adoption_foreign = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'keyed-announced-source-failed'), record['detail'])
        self.assertIn('tracking_source_adopted', record['detail'])
        report.validate_scenario(record)

    def test_lifecycle_refused_fails(self):
        self.feed.honest_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'keyed-announced-source-failed'), record['detail'])
        self.assertIn('follow-peer', record['detail'])
        report.validate_scenario(record)

    def test_planted_pull_applied_fails(self):
        self.feed.planted_applied = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'keyed-announced-source-failed'), record['detail'])
        report.validate_scenario(record)

    def test_silent_forge_fails(self):
        self.feed.forge_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'keyed-announced-source-failed'), record['detail'])
        self.assertIn('no checkpoint pull', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'roles': 'restored'}, {}, {'pass': 1}),
                       ({'roles': 'unrestored'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_keyed_pass',
                          lambda *a, **kw: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'keyed-announced-source-nondeterministic'),
            record['detail'])
        self.assertIn('diverged', record['detail'])
        report.validate_scenario(record)

    def test_silent_judge_reports_unchecked(self):
        # The unchecked-diagnostic self-check leg: a judge that
        # names nothing lets every planted negative slip — the run
        # reports its named diagnostic, never a clean pass.
        with patch.object(scenarios, '_judge_refusal',
                          lambda *a, **kw: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'keyed-announced-source-unchecked'), record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            feed = KeyedAnnouncedFeed(self.tmp.name)
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.evidence = evidence
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
