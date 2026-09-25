"""The operator page's feed-state logic, harnessed without a browser.

`crates/dcs-monitor/src/page.html` is a single dependency-free asset
whose feed-state rules — the publication-gap latch, the trend
watermark/seq fallback, and the bounded poll's named degradations —
previously had no executable coverage below a browser. `PageReplica`
below mirrors those rules poll-for-poll over a stubbed transport:
synthetic `/snapshot`, `/role`, `/history`, and `/journal` answers
drive the same ordering `refresh()` runs, including the abort bound a
hanging listener degrades through.

Every mirrored statement is pinned to the page.html line that carries
it — the `PINS` table asserts the exact line content, so a page edit
that moves or rewrites the mirrored logic fails here naming the line,
keeping the replica honest instead of silently drifting. The mirrored
spans, in page.html 1-based lines:

- `journalEntries`/`journalSeen`/`journalRun` records — L571–587;
  `feed` record — L600; the `ackReleases` record — L638; the
  `pendingCommands` abandoned-submission records — L645–646;
  `POLL_MS`/`pollFetch` abort bound — L757–759
- `pollRoles` fetch/error bookkeeping — L766–774; `selectSource`/
  `switchSource` — L788–818; `activePeer`/`noActiveVerdict` —
  L836–851; the "unreachable" pair render — L983
- `notePublication`/`noteRestart`/`noteFeedGap`/`renderFeed` —
  L1025–1103
- `refresh()`'s feed ordering, the ack pulse's held-true arming audit
  and its retried-until-receipted release loop, and the failed-poll
  stale mark — L1352–1441, L1474–1475, L1494–1495
- `submitAck`'s press: the receipted write of true arming the release
  on an accepted/applied outcome — L2990–3007
- `refreshTrends`' since cursor, run-marker restart check, gap note,
  and the refetch-gated watermark / seq fallback — L3215–3264
- `journalKey`'s run-qualified merge identity and `refreshJournal`'s
  cursor, head and consecutive-pair gap notes, attribution re-mark,
  dedupe, run_boundary restart check, merge, the pending-submission
  settle resolution, ordering, and bound — L3333–3442
- `isAbortError`'s abort/timeout split, `abandonedSubmission`'s
  indeterminate verdict and pending record, `sameSubmission`'s
  settle-to-submission match, `postCommand`'s bounded POST,
  `isNotActive`, and `submitCommand`'s active-peer routing with the
  not_active re-poll and the abort-verdict ordering — L3728–3901
"""
import json
import unittest
from pathlib import Path


PAGE = (Path(__file__).resolve().parents[1]
        / 'crates' / 'dcs-monitor' / 'src' / 'page.html')
PAGE_LINES = PAGE.read_text().splitlines()


def page_line(number):
    """The stripped content of page.html's 1-based line `number`."""
    return PAGE_LINES[number - 1].strip()


# The page.html lines this harness mirrors, pinned by exact content: an
# edit that moves or rewrites the mirrored logic fails the pin test
# naming the line, so the replica is re-verified against the page.
PINS = [
    # The retained journal window, its run-qualified merge set, and the
    # lifetime attribution the served run_boundary markers re-mark.
    (575, 'const journalEntries = [];'),
    (582, 'const journalSeen = new Set();'),
    (591, 'let journalRun = 0;'),
    # The feed record, the armed ack releases, the abandoned-submission
    # pending record, and the shared poll bound.
    (604, 'const feed = { publication: null, gap: null, stale: null, restart: null };'),
    (642, 'const ackReleases = new Map();'),
    (649, 'const pendingCommands = [];'),
    (650, 'const PENDING_LIMIT = 16;'),
    (761, 'const POLL_MS = 1000;'),
    (762, 'function pollFetch(url) {'),
    (763, 'return fetch(url, { signal: AbortSignal.timeout(POLL_MS) });'),
    # pollRoles — bounded role fetches, errors recorded not thrown.
    (770, 'async function pollRoles() {'),
    (773, 'const response = await pollFetch(peer.base + "/role");'),
    (775, 'peerState[i].report = await response.json();'),
    (776, 'peerState[i].error = null;'),
    (778, 'peerState[i].error = String(error);'),
    # Source selection and the switch's bookkeeping reset — the run
    # marker and lifetime attribution clear with the seq cursor since
    # each peer numbers its own.
    (792, 'function selectSource() {'),
    (808, 'function switchSource(next) {'),
    (810, 'for (const state of trends.values()) {'),
    (811, 'state.lastSeq = 0;'),
    (812, 'state.run = null;'),
    (814, 'journalSince = 0;'),
    (815, 'journalRun = 0;'),
    (819, 'feed.publication = null;'),
    (820, 'feed.gap = null;'),
    (821, 'feed.stale = null;'),
    (822, 'feed.restart = null;'),
    # The unique settled-active peer every command targets — and the
    # not-sent verdict while the pair has none.
    (840, 'function activePeer() {'),
    (848, 'function noActiveVerdict() {'),
    # The unreachable peer fault the role poll's error names.
    (906, 'faults.push(was + peer.name + " unreachable");'),
    (907, 'fault_kinds.push("peer_unreachable");'),
    (987, 'const unreachable = state.error !== null;'),
    # Publication freshness bookkeeping — and the regressed-identity
    # restart observation.
    (1042, 'function notePublication(snapshot) {'),
    (1049, 'feed.stale = last !== null &&'),
    (1050, 'current.published === last.published && current.tick <= last.tick'),
    (1053, 'feed.publication = current;'),
    (1054, 'if (last !== null && ((current.published !== null &&'),
    (1057, 'noteRestart("the source\'s publication identity regressed");'),
    # The same-source restart seam every stream's observation funnels
    # into: cursors reset, and a restarted tick domain clears the drawn
    # series rather than stitching across lifetimes.
    (1069, 'function noteRestart(detail) {'),
    (1070, 'const freshDomain = [...trends.values()].some(state =>'),
    (1071, 'state.lastTick !== null && feed.publication.tick <= state.lastTick);'),
    (1072, 'for (const state of trends.values()) {'),
    (1073, 'state.lastSeq = 0;'),
    (1074, 'state.run = null;'),
    (1075, 'if (freshDomain) {'),
    (1076, 'state.samples = [];'),
    (1077, 'state.lastTick = null;'),
    (1080, 'journalSince = 0;'),
    (1081, 'feed.restart = detail;'),
    # The gap note and the feed-state render — restart and gap share the
    # "gap" severity class, stale the quieter one.
    (1089, 'function noteFeedGap(stream, from, through) {'),
    (1091, 'feed.gap = { stream: stream, from: from, through: through };'),
    (1099, 'function renderFeed() {'),
    (1103, 'notices.push("source restarted — " + feed.restart +'),
    (1107, 'notices.push("publication gap: " + feed.gap.stream + " seqs " +'),
    (1112, 'notices.push("stale publication: " +'),
    (1118, 'line.hidden = notices.length === 0;'),
    (1120, 'feed.gap !== null || feed.restart !== null'),
    # refresh()'s feed ordering and its failed-poll stale mark — the
    # marks reset before notePublication so its restart observation
    # survives the poll.
    (1369, 'async function refresh() {'),
    (1373, 'await pollRoles();'),
    (1382, 'pollFetch(base + "/snapshot").then(r => r.json()),'),
    (1390, 'feed.gap = null;'),
    (1391, 'feed.restart = null;'),
    (1392, 'notePublication(snapshot);'),
    # The ack pulse's release half: a snapshot serving a writable `ack`
    # input held true arms its release at this tick — the serving scan
    # already observed the level, so an acknowledge write that applied
    # past its abort-bound receipt (or a reload over a held input)
    # still gets its pulse completed — and a due release stays armed
    # until the receipted write answers accepted or applied, an aborted
    # or refused submission retrying on a later poll rather than
    # dropping the release and latching the input against later presses.
    (1416, 'for (const descriptor of snapshot.descriptors || []) {'),
    (1418, 'p.name === "ack" && p.direction === "in" && p.kind === "bool");'),
    (1419, 'if (!ack || ack.point == null || ackReleases.has(ack.point)) {'),
    (1422, 'const meta = metaByPoint.get(ack.point);'),
    (1423, 'const ackSample = (telemetry.get(ack.point) || {}).sample;'),
    (1426, 'ackReleases.set(ack.point, snapshot.tick);'),
    (1439, 'for (const [point, applyTick] of [...ackReleases]) {'),
    (1441, 'const held = sample && sample.value && sample.value.bool === true;'),
    (1442, 'if (snapshot.tick < applyTick && !held) continue;'),
    (1443, 'if (sample && sample.value && sample.value.bool === false) {'),
    (1444, 'ackReleases.delete(point);'),
    (1448, 'const answer = await submitCommand({ write_value: {'),
    (1449, 'point: point, kind: "bool", value: { bool: false } } });'),
    (1451, '("accepted" in answer.outcome || "applied" in answer.outcome)) {'),
    (1452, 'ackReleases.delete(point);'),
    (1455, 'document.getElementById("receipt").textContent ='),
    (1456, '"command failed: " + error;'),
    (1491, 'await Promise.all([refreshTrends(base), refreshJournal(base)]);'),
    (1492, 'renderFeed();'),
    (1511, 'if (feed.stale === null && feed.publication !== null) {'),
    (1512, 'feed.stale = feed.publication;'),
    # submitAck — the press posting write_value true through the
    # receipted path and arming the release only on an accepted or
    # applied outcome, so a rejection never schedules a release and a
    # lost answer leaves the held-true audit to catch the latch.
    (3007, 'async function submitAck(button) {'),
    (3010, 'if (!meta || !meta.writable) return;'),
    (3012, 'const answer = await submitCommand({ write_value: {'),
    (3016, 'ackReleases.set(point, answer.outcome.accepted.apply_tick);'),
    (3018, 'ackReleases.set(point, answer.outcome.applied.tick);'),
    # refreshTrends: the common since cursor, the served run marker's
    # restart check, the served gap note, and the refetch-gated tick
    # watermark / seq fallback.
    (3232, 'async function refreshTrends(base) {'),
    (3238, 'const histories = await (await pollFetch(base + "/history?since=" + since)).json();'),
    (3248, 'if (history.run !== undefined) {'),
    (3249, 'if (state.run !== null && history.run !== state.run) {'),
    (3250, 'noteRestart("the served history run marker advanced to run " +'),
    (3253, 'state.run = history.run;'),
    (3258, 'const first = history.samples.find(entry => entry.seq > state.lastSeq);'),
    (3259, 'if (state.lastSeq > 0 && first && first.seq > state.lastSeq + 1) {'),
    (3260, 'noteFeedGap("history", state.lastSeq + 1, first.seq - 1);'),
    (3271, 'const refetch = state.lastSeq === 0;'),
    (3272, 'const watermark = state.lastTick;'),
    (3274, 'if (entry.seq <= state.lastSeq) continue;'),
    (3275, 'state.lastSeq = entry.seq;'),
    (3276, 'if (refetch && watermark !== null && entry.sample.tick <= watermark) {'),
    (3279, 'state.samples.push(entry.sample);'),
    (3281, 'state.lastTick = entry.sample.tick;'),
    # journalKey — the run-qualified merge identity — and
    # refreshJournal's served gap notes, cursor advance, the boundary's
    # attribution re-mark ahead of the dedupe, the merge itself, the
    # run_boundary restart observation, the abandoned-submission
    # settle resolution, and the pane's ordering/bound.
    (3350, 'function journalKey(entry) {'),
    (3351, 'return entry.run + " " + entry.tick + " " + JSON.stringify(entry.event);'),
    (3361, 'const entries = await (await pollFetch(base + "/journal?since=" + journalSince)).json();'),
    (3366, 'if (journalSince === 0) journalRun = 0;'),
    (3376, 'noteFeedGap("journal", journalSince + 1, entries[0].seq - 1);'),
    # The consecutive-pair half of the journal gap check: a pinned
    # run_boundary ahead of the ring's evicted tail serves one answer
    # with an internal discontinuity, at any cursor including 0.
    (3378, 'for (let i = 1; i < entries.length; i++) {'),
    (3379, 'if (entries[i].seq > entries[i - 1].seq + 1) {'),
    (3380, 'noteFeedGap("journal", entries[i - 1].seq + 1, entries[i].seq - 1);'),
    (3384, 'journalSince = Math.max(journalSince, entry.seq);'),
    (3390, 'journalRun = entry.event.run_boundary.run;'),
    (3392, 'entry.run = journalRun;'),
    (3393, 'const key = journalKey(entry);'),
    (3394, 'if (journalSeen.has(key)) continue;'),
    (3395, 'journalSeen.add(key);'),
    (3396, 'journalEntries.push(entry);'),
    (3402, 'if ("run_boundary" in entry.event) {'),
    (3403, 'noteRestart("the journal recorded run " +'),
    (3436, 'const abandoned = pendingCommands.findIndex(pending =>'),
    (3437, 'sameSubmission(settled.receipt, pending));'),
    (3439, 'const pending = pendingCommands.splice(abandoned, 1)[0];'),
    (3450, 'journalEntries.sort((a, b) => a.tick - b.tick || a.seq - b.seq);'),
    (3452, 'for (const entry of journalEntries.splice(0, journalEntries.length - JOURNAL_LIMIT)) {'),
    (3453, 'journalSeen.delete(journalKey(entry));'),
    # The indeterminate-outcome path — the abort split, the abandoned
    # submission's pending record and receipt-shaped answer, and the
    # settle match — then postCommand's bounded POST every command
    # rides, isNotActive's rejected-not_active shape, and
    # submitCommand's active-peer routing with the single re-poll/retry
    # the abort verdict precedes.
    (3769, 'function isAbortError(error) {'),
    (3771, '(error.name === "AbortError" || error.name === "TimeoutError");'),
    (3800, 'function sameSubmission(receipt, pending) {'),
    (3813, 'function abandonedSubmission(command, reason, error) {'),
    (3816, 'outcome: { indeterminate: { detail: String(error) } },'),
    (3824, 'pendingCommands.push({'),
    (3852, 'async function postCommand(base, command, reason) {'),
    (3863, 'signal: AbortSignal.timeout(POLL_MS),'),
    (3868, 'function isNotActive(receipt) {'),
    (3894, 'async function submitCommand(command, reason) {'),
    (3908, 'answer = await postCommand(peers[target].base, command, reason);'),
    (3910, 'if (isAbortError(error)) {'),
    (3911, 'return abandonedSubmission(command, reason, error);'),
    (3915, 'if (isNotActive(answer)) {'),
    (3933, 'receipt.textContent = JSON.stringify(answer, null, 2);'),
    # The cadence both tickers share.
    (4065, 'setInterval(refreshOverview, POLL_MS);'),
    (4282, 'setInterval(refresh, POLL_MS);'),
]


POLL_MS = 1000           # page.html:757 — the shared poll cadence/bound
JOURNAL_LIMIT = 300      # page.html:657 — the pane's retention bound
HANG = object()          # a scripted listener that never answers


def journal_key(entry):
    """The merge identity the pane dedupes on: the attributed lifetime
    plus tick plus serialized event — tick alone is unique only inside
    one tick domain, so the run qualifier is what keeps a restarted
    lifetime's re-journaled sweep from colliding with the old domain's
    identical rows. `run` is the page's own merge-time attribution
    stamp — the served JournalEntry carries no run field."""
    return '%s %s %s' % (entry['run'], entry['tick'],
                         json.dumps(entry['event'],
                                    separators=(',', ':'),
                                    sort_keys=True))


def describe_outcome(outcome):
    """page.html:3603-3615 — the outcome's rendered verdict: applied
    at tick / accepted for tick / rejected / the page-minted
    indeterminate an abort-abandoned submission answers with. (The
    rejected branch names its reason through describeReason on the
    page; the replica, mirroring no renderer, dumps it.)"""
    if 'applied' in outcome:
        return 'applied at tick %s' % outcome['applied']['tick']
    if 'accepted' in outcome:
        return 'accepted for tick %s' % outcome['accepted']['apply_tick']
    if 'rejected' in outcome:
        return 'rejected: %s' % json.dumps(outcome['rejected']['reason'])
    if 'indeterminate' in outcome:
        return ('outcome unknown — the submission outlived the abort '
                'bound and may still apply; the journaled settled '
                'receipt is the verdict')
    return json.dumps(outcome)


class PollError(Exception):
    """A poll read's terminal failure — HTTP error or the abort the
    POLL_MS bound lands on a hanging listener."""


class Response:
    """The fetch Response slice the page reads: `ok` and `json()`."""

    def __init__(self, payload, ok=True):
        self.payload = payload
        self.ok = ok

    def json(self):
        return self.payload


class StubTransport:
    """Scripted endpoint answers. Each `script(path, [...])` queues the
    responses successive fetches of that path return; queueing HANG
    instead makes the listener never answer, leaving the poll's abort
    bound to decide the outcome. `fetch`'s optional `body` carries the
    JSON a POST submits — recorded on `posts` — and a scripted callable
    answers from it, so a script can apply a command and still choose
    the response. A `handler(path, body)` fallback answers any path
    whose script is absent or drained — the simulated plant the
    ack-release tests drive."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        self.posts = []
        self.handler = None

    def script(self, path, responses):
        self.routes[path.lstrip('/')] = list(responses)

    def fetch(self, url, body=None):
        self.requests.append(url)
        if body is not None:
            self.posts.append((url, body))
        path = url.split('?')[0].rstrip('/').rsplit('/', 1)[-1]
        script = self.routes.get(path)
        if script:
            answer = script.pop(0)
        elif self.handler is not None:
            answer = self.handler(path, body)
        else:
            raise AssertionError('unscripted or exhausted fetch: ' + url)
        return answer(body) if callable(answer) else answer


class PageReplica:
    """The page's feed-state logic, mirrored poll-for-poll against a
    StubTransport. `now` is the virtual clock the abort bound reads —
    a hanging fetch resolves at poll start + POLL_MS, the deadline the
    page's AbortSignal.timeout lands every hanging read on."""

    def __init__(self, transport, peers=("",), points=(1,), metas=None):
        self.transport = transport
        self.peers = [{'base': base, 'name': base or 'origin'}
                      for base in peers]
        self.peer_state = [{'report': None, 'error': None,
                            'unsyncedSince': None}
                           for _ in self.peers]
        self.source = 0
        self.feed = {'publication': None, 'gap': None, 'stale': None,
                     'restart': None}
        self.trends = {point: {'samples': [], 'lastSeq': 0,
                               'lastTick': None, 'run': None}
                       for point in points}
        self.journal_since = 0
        # page.html:571,578,587 — the retained journal window the pane
        # renders from, its merge set, and the lifetime attribution the
        # served run_boundary markers re-mark.
        self.journal_entries = []
        self.journal_seen = set()
        self.journal_run = 0
        # page.html:554,638,645 — the /signals metadata the command
        # affordances gate on (each meta a dict like the served
        # PointSignal: writable, value_type), the armed ack-release map
        # — ack point -> the earliest snapshot tick its release write
        # may go at — and the abandoned-submission pending records a
        # journaled settle resolves (each {command, actor, reason,
        # notice} like the page's).
        self.meta_by_point = dict(metas or {})
        self.ack_releases = {}
        self.pending_commands = []
        # The receipt pane's text — submitCommand's rendered answer, an
        # abandoned submission's indeterminate "outcome unknown"
        # notice, or a refused send's "command failed: …".
        self.receipt = ''
        self.polling = False
        self.now = 0
        self.poll_started = 0
        self.status = ''
        self.feed_line = {'hidden': True, 'class': '', 'text': ''}
        self.peer_rows = ['—' for _ in self.peers]

    # --- the bounded fetch every poll read rides: page.html:757-759 ---

    def poll_fetch(self, url):
        answer = self.transport.fetch(url)
        if answer is HANG:
            # AbortSignal.timeout(POLL_MS): the hanging read aborts at
            # the poll's deadline rather than freezing the view — the
            # clock lands on poll start + POLL_MS however the listener
            # behaves.
            self.now = max(self.now, self.poll_started + POLL_MS)
            raise PollError('AbortError: signal timed out')
        return answer

    # --- the role poll and pair render: page.html:766-774, 788-818 ---

    def poll_roles(self):
        for i, peer in enumerate(self.peers):
            state = self.peer_state[i]
            try:
                response = self.poll_fetch(peer['base'] + '/role')
                if not response.ok:
                    raise PollError('HTTP %s' % response.status)
                state['report'] = response.json()
                state['error'] = None
            except Exception as error:
                state['error'] = str(error)
            self.note_sync_age(state)
        self.select_source()

    def note_sync_age(self, state):
        # page.html:739-750 — the convergence-grace clock.
        if (state['error'] is None and state['report'] is not None
                and state['report'].get('sync') == 'unsynchronized'):
            state['unsyncedSince'] = state['unsyncedSince'] or self.now
        else:
            state['unsyncedSince'] = None

    def _reachable(self, i):
        state = self.peer_state[i]
        return state['error'] is None and state['report'] is not None

    def _actives(self):
        return [i for i, state in enumerate(self.peer_state)
                if state['error'] is None and state['report']
                and state['report'].get('role') == 'active']

    def select_source(self):
        actives = self._actives()
        if self.source in actives:
            nxt = self.source
        else:
            nxt = actives[0] if actives else -1
        if nxt < 0:
            nxt = next((i for i, state in enumerate(self.peer_state)
                        if self._reachable(i)
                        and state['report'].get('role') == 'promoting'),
                       -1)
        if nxt < 0:
            nxt = (self.source if self._reachable(self.source)
                   else next((i for i in range(len(self.peer_state))
                              if self._reachable(i)), -1))
        if 0 <= nxt != self.source:
            self.switch_source(nxt)

    def switch_source(self, nxt):
        # page.html:804-818 — the new peer's streams re-read whole and
        # the feed bookkeeping starts over.
        self.source = nxt
        for state in self.trends.values():
            state['lastSeq'] = 0
            state['run'] = None
        self.journal_since = 0
        self.journal_run = 0
        self.feed['publication'] = None
        self.feed['gap'] = None
        self.feed['stale'] = None
        self.feed['restart'] = None

    def render_pair(self):
        # page.html:983 — the per-peer row's reachability column.
        self.peer_rows = [
            'unreachable' if self.peer_state[i]['error'] is not None
            else ('serving' if i == self.source else 'reachable')
            for i in range(len(self.peers))
        ]

    # --- the receipted command path: page.html:836-851, 3717-3890 ---

    def active_peer(self):
        # page.html:836-839 — the pair's unique settled-active peer's
        # index, or -1 mid-transition, without one, or under the
        # dual-active fault.
        actives = self._actives()
        return actives[0] if len(actives) == 1 else -1

    def no_active_verdict(self):
        # page.html:844-851 — the not-sent verdict a control surfaces
        # when submitCommand found no legitimate target.
        actives = len(self._actives())
        if actives > 1:
            return ('not sent: %s peers report role active — the '
                    'dual-active fault leaves no unique command target'
                    % actives)
        return ('not sent: no peer reports role active — the pair is '
                'mid-transition')

    @staticmethod
    def is_abort_error(error):
        # page.html:3724-3727 — the abort bound's two error names
        # (the replica's PollError carries the name in its text):
        # either means the wait ended, not provably the server's work.
        return str(error).startswith(('AbortError', 'TimeoutError'))

    @staticmethod
    def same_command(a, b):
        # page.html:3742-3747 — two wire Commands name the same
        # operation when the same known variant carries structurally
        # equal fields; Python's dict equality already numbers
        # 1.0 == 1, the sameJson numeric-equivalence rule.
        for variant in ('write_value', 'force_point', 'unforce_point',
                        'set_parameter', 'invoke'):
            if variant in a:
                return variant in b and a[variant] == b[variant]
        return False

    @staticmethod
    def same_submission(receipt, pending):
        # page.html:3755-3759 — the settled receipt answers an
        # abandoned submission when the command matches and the
        # declared actor and reason the receipt echoes agree, so
        # another console's identical command cannot claim the pending
        # entry.
        return (PageReplica.same_command(receipt.get('command') or {},
                                         pending['command'])
                and (receipt.get('actor') or None) == pending['actor']
                and (receipt.get('reason') or None) == pending['reason'])

    def abandoned_submission(self, command, reason, error):
        # page.html:3768-3788 — the receipt-shaped indeterminate answer
        # an abort-abandoned submission reports, the pending record the
        # journaled settle resolves, and the receipt pane's "outcome
        # unknown" notice — never "command failed". (The page's notice
        # names the command through describeCommand; the replica, which
        # mirrors no renderer, records the command itself.)
        answer = {'command': command,
                  'outcome': {'indeterminate': {'detail': str(error)}}}
        if reason:
            answer['reason'] = reason
        notice = ('command outcome unknown — the request outlived the '
                  '%s ms abort bound and may still apply: %s. The '
                  'journaled settled receipt is the verdict — '
                  'resubmitting now risks applying the command twice.'
                  % (POLL_MS, json.dumps(command)))
        self.pending_commands.append({'command': command,
                                      'actor': None,
                                      'reason': reason or None,
                                      'notice': notice})
        if len(self.pending_commands) > 16:   # page.html:646
            self.pending_commands.pop(0)
        self.receipt = notice
        return answer

    def post_command(self, base, command, reason=None):
        # page.html:3807-3821 — the attributed envelope when a reason
        # rides (the replica declares no operator identity), the POST
        # itself, and the same POLL_MS abort bound every poll read
        # rides: a hanging listener answers nothing and the bound
        # decides — here, at the submission's own deadline.
        body = command
        if reason is not None:
            body = {'command': command, 'reason': reason}
        started = self.now
        answer = self.transport.fetch(base + '/command', body=body)
        if answer is HANG:
            self.now = max(self.now, started + POLL_MS)
            raise PollError('AbortError: signal timed out')
        return answer.json()

    @staticmethod
    def is_not_active(receipt):
        # page.html:3823-3827 — the rejected receipt carrying the
        # not_active reason.
        rejected = ((receipt or {}).get('outcome') or {}).get('rejected')
        return bool(rejected) and 'not_active' in rejected.get('reason', {})

    def submit_command(self, command, reason=None):
        # page.html:3849-3890 — active-peer routing, the roles re-poll
        # while none reports, the one not_active re-poll/retry, the
        # receipt pane's rendered answer, and the answer itself (null
        # when nothing was sent). The abort bound firing answers the
        # indeterminate verdict instead of throwing — the request may
        # already be buffered server-side — and never runs the retry:
        # resending a command whose fate is unresolved doubles a landed
        # one.
        target = self.active_peer()
        if target < 0:
            self.poll_roles()
            self.render_pair()
            target = self.active_peer()
        if target < 0:
            self.receipt = self.no_active_verdict()
            return None
        try:
            answer = self.post_command(self.peers[target]['base'],
                                       command, reason)
        except PollError as error:
            if self.is_abort_error(error):
                return self.abandoned_submission(command, reason, error)
            raise
        if self.is_not_active(answer):
            self.poll_roles()
            self.render_pair()
            target = self.active_peer()
            if target >= 0:
                try:
                    answer = self.post_command(self.peers[target]['base'],
                                               command, reason)
                except PollError as error:
                    # The first post answered a named refusal — only
                    # the retry's fate is open when the bound fires.
                    if self.is_abort_error(error):
                        return self.abandoned_submission(command, reason,
                                                         error)
                    raise
        self.receipt = json.dumps(answer, indent=2)
        return answer

    # --- the ack pulse's press half: page.html:2990-3007 ---

    def submit_ack(self, point):
        """The acknowledge press: an ordinary receipted write_value of
        true on the `ack` input's bound point — gated on the metadata's
        writable mark like every command affordance. An accepted or
        applied outcome arms the release at its apply/settle tick; a
        rejection arms nothing, and a lost answer leaves the poll's
        held-true audit to catch a write that landed anyway."""
        meta = self.meta_by_point.get(point)
        if not meta or not meta.get('writable'):
            return
        try:
            answer = self.submit_command({'write_value': {
                'point': point, 'kind': meta.get('value_type', 'bool'),
                'value': {'bool': True}}})
            if answer and answer.get('outcome'):
                if 'accepted' in answer['outcome']:
                    self.ack_releases[point] = \
                        answer['outcome']['accepted']['apply_tick']
                elif 'applied' in answer['outcome']:
                    self.ack_releases[point] = \
                        answer['outcome']['applied']['tick']
        except PollError as error:
            self.receipt = 'command failed: %s' % error
        self.refresh()

    def release_acks(self, snapshot):
        """The ack pulse's release half, mirrored from
        page.html:1390-1441 — refresh() runs it over each landed
        snapshot before the since-polls. A snapshot serving a writable
        `ack` input held true arms a release at that tick (the serving
        scan already observed the level, covering a press whose receipt
        the abort bound swallowed or a reload over a held input); a
        release is due at its armed apply tick or as soon as the point
        serves true; and the entry stays armed until the release write
        answers accepted or applied — an aborted POST, a refused
        receipt, or a poll without an active peer retries on a later
        poll rather than dropping the release and latching the input
        true against every later press."""
        telemetry = {p['point']: p for p in snapshot.get('points', [])}
        for descriptor in snapshot.get('descriptors') or []:
            ack = next((p for p in descriptor.get('ports') or []
                        if p.get('name') == 'ack'
                        and p.get('direction') == 'in'
                        and p.get('kind') == 'bool'), None)
            if (not ack or ack.get('point') is None
                    or ack['point'] in self.ack_releases):
                continue
            meta = self.meta_by_point.get(ack['point'])
            ack_sample = (telemetry.get(ack['point']) or {}).get('sample')
            if (meta and meta.get('writable') and ack_sample
                    and (ack_sample.get('value') or {}).get('bool')
                        is True):
                self.ack_releases[ack['point']] = snapshot['tick']
        for point, apply_tick in list(self.ack_releases.items()):
            sample = (telemetry.get(point) or {}).get('sample')
            held = bool(sample
                        and (sample.get('value') or {}).get('bool')
                        is True)
            if snapshot['tick'] < apply_tick and not held:
                continue
            if (sample and (sample.get('value') or {}).get('bool')
                    is False):
                del self.ack_releases[point]
                continue
            try:
                answer = self.submit_command({'write_value': {
                    'point': point, 'kind': 'bool',
                    'value': {'bool': False}}})
                if (answer and answer.get('outcome')
                        and ('accepted' in answer['outcome']
                             or 'applied' in answer['outcome'])):
                    del self.ack_releases[point]
            except PollError as error:
                self.receipt = 'command failed: %s' % error

    # --- the feed record: page.html:1025-1103 ---

    def note_publication(self, snapshot):
        health = snapshot.get('publication') or None
        current = {
            'published': health['published'] if health else None,
            'tick': snapshot['tick'],
        }
        last = self.feed['publication']
        self.feed['stale'] = (
            current
            if last is not None
            and current['published'] == last['published']
            and current['tick'] <= last['tick']
            else None)
        self.feed['publication'] = current
        if (last is not None and (
                (current['published'] is not None
                 and last['published'] is not None
                 and current['published'] < last['published'])
                or current['tick'] < last['tick'])):
            self.note_restart(
                "the source's publication identity regressed")

    def note_restart(self, detail):
        # page.html:1052-1065 — the same-source restart every stream's
        # observation funnels into: all stream cursors reset so the
        # next reads re-fetch whole, and a restarted tick domain — the
        # served tick at or below a drawn sample's — clears the series
        # rather than stitching across lifetimes.
        fresh_domain = any(
            state['lastTick'] is not None
            and self.feed['publication']['tick'] <= state['lastTick']
            for state in self.trends.values())
        for state in self.trends.values():
            state['lastSeq'] = 0
            state['run'] = None
            if fresh_domain:
                state['samples'] = []
                state['lastTick'] = None
        self.journal_since = 0
        self.feed['restart'] = detail

    def note_feed_gap(self, stream, from_, through):
        if self.feed['gap'] is None or through > self.feed['gap']['through']:
            self.feed['gap'] = {'stream': stream, 'from': from_,
                                'through': through}

    def render_feed(self):
        notices = []
        if self.feed['restart'] is not None:
            notices.append(
                'source restarted — ' + self.feed['restart']
                + '; new process lifetime, streams re-read and merged '
                  'by tick')
        if self.feed['gap'] is not None:
            gap = self.feed['gap']
            notices.append(
                'publication gap: %s seqs %s–%s evicted before this '
                'page read them — showing the retained tail'
                % (gap['stream'], gap['from'], gap['through']))
        if self.feed['stale'] is not None:
            stale = self.feed['stale']
            notices.append(
                'stale publication: '
                + ('tick %s' % stale['tick'] if stale['published'] is None
                   else 'seq %s at tick %s'
                        % (stale['published'], stale['tick']))
                + ' re-served — values shown are last-known, not current')
        self.feed_line = {
            'hidden': len(notices) == 0,
            'class': ('gap' if (self.feed['gap'] is not None
                                or self.feed['restart'] is not None)
                      else 'stale' if self.feed['stale'] is not None
                      else ''),
            'text': '; '.join(notices),
        }
        return self.feed_line

    # --- the stream polls: page.html:3215-3264, 3343-3431 ---

    def refresh_trends(self):
        states = list(self.trends.values())
        since = 0
        if states and all(state['lastSeq'] > 0 for state in states):
            since = min(state['lastSeq'] for state in states)
        histories = self.poll_fetch(
            '/history?since=%s' % since).json()
        for history in histories:
            state = self.trends.get(history['point'])
            if state is None:
                continue
            # The envelope's served lifetime marker: a `run` that
            # changed under a standing cursor means the seq axis
            # restarted with a new process lifetime. A peer predating
            # the marker serves no `run` and seeds nothing.
            if 'run' in history:
                if (state['run'] is not None
                        and history['run'] != state['run']):
                    self.note_restart(
                        'the served history run marker advanced to run '
                        + str(history['run']))
                state['run'] = history['run']
            first = next((entry for entry in history['samples']
                          if entry['seq'] > state['lastSeq']), None)
            if (state['lastSeq'] > 0 and first
                    and first['seq'] > state['lastSeq'] + 1):
                self.note_feed_gap('history', state['lastSeq'] + 1,
                                   first['seq'] - 1)
            # The tick watermark dedupes only a whole-stream re-read;
            # entries the seq cursor proves new take the seq fallback.
            refetch = state['lastSeq'] == 0
            watermark = state['lastTick']
            for entry in history['samples']:
                if entry['seq'] <= state['lastSeq']:
                    continue
                state['lastSeq'] = entry['seq']
                if (refetch and watermark is not None
                        and entry['sample']['tick'] <= watermark):
                    continue
                state['samples'].append(entry['sample'])
                if (state['lastTick'] is None
                        or entry['sample']['tick'] > state['lastTick']):
                    state['lastTick'] = entry['sample']['tick']

    def refresh_journal(self):
        # page.html:3343-3442 — the since-read, the head and
        # consecutive-pair gap notes, the run-attributed merge loop,
        # the abandoned-submission settle resolution, and the pane's
        # tick-order bounded render set.
        entries = self.poll_fetch(
            '/journal?since=%s' % self.journal_since).json()
        # A whole re-read answers from the stream's oldest retained
        # lifetime: the attribution walk starts over and the served
        # run_boundary markers re-mark it as the fold proceeds.
        if self.journal_since == 0:
            self.journal_run = 0
        if (self.journal_since > 0 and entries
                and entries[0]['seq'] > self.journal_since + 1):
            self.note_feed_gap('journal', self.journal_since + 1,
                               entries[0]['seq'] - 1)
        # page.html:3342-3346 — the same discontinuity can sit wholly
        # inside one answer: a pinned run_boundary precedes the ring's
        # retained tail, so consecutive served seqs step over an
        # evicted stretch at any cursor, the whole re-read's 0 too.
        for prev, nxt in zip(entries, entries[1:]):
            if nxt['seq'] > prev['seq'] + 1:
                self.note_feed_gap('journal', prev['seq'] + 1,
                                   nxt['seq'] - 1)
        for entry in entries:
            self.journal_since = max(self.journal_since, entry['seq'])
            # A run_boundary re-marks the lifetime the entries after it
            # belong to — ahead of the dedupe, so a re-served boundary
            # still attributes the entries following it.
            if 'run_boundary' in entry['event']:
                self.journal_run = entry['event']['run_boundary']['run']
            entry['run'] = self.journal_run
            key = journal_key(entry)
            if key in self.journal_seen:
                continue
            self.journal_seen.add(key)
            self.journal_entries.append(entry)
            # A run_boundary the page had not yet rendered is the
            # journal stream's own restart marker.
            if 'run_boundary' in entry['event']:
                self.note_restart(
                    'the journal recorded run '
                    + str(entry['event']['run_boundary']['run'])
                    + ' beginning')
            # The receipted contract's truth answering an abandoned
            # submission (page.html:3403-3419): a settle matching a
            # pending entry resolves the indeterminate verdict the
            # abort left — the receipt pane's notice replaced only
            # while that submission's notice still stands there.
            settled = entry['event'].get('command_settled')
            if settled:
                for index, pending in enumerate(self.pending_commands):
                    if self.same_submission(settled['receipt'], pending):
                        pending = self.pending_commands.pop(index)
                        if self.receipt == pending['notice']:
                            self.receipt = (
                                'the abandoned submission settled — '
                                '%s — %s\n\n%s'
                                % (json.dumps(settled['receipt']
                                              ['command']),
                                   describe_outcome(
                                       settled['receipt']['outcome']),
                                   json.dumps(settled['receipt'],
                                              indent=2)))
                        break
        self.journal_entries.sort(key=lambda e: (e['tick'], e['seq']))
        while len(self.journal_entries) > JOURNAL_LIMIT:
            self.journal_seen.discard(
                journal_key(self.journal_entries.pop(0)))

    # --- the poll ordering: page.html:1352-1501 ---

    def refresh(self):
        """One poll's feed-relevant ordering: roles, the bounded
        snapshot/schema/resources read, the freshness bookkeeping, the
        since-polls, the feed render — and the failed-poll stale mark."""
        if self.polling:
            return
        self.polling = True
        self.poll_started = self.now
        try:
            self.poll_roles()
            self.render_pair()
            snapshot = self.poll_fetch('/snapshot').json()
            # /schema and /resources ride the same publication and the
            # same abort bound, swallowed to null like the page's
            # `.catch(() => null)`; neither feeds the mirrored state.
            for path in ('/schema', '/resources'):
                try:
                    self.poll_fetch(path)
                except PollError:
                    pass
            # The marks reset before notePublication so a regressed
            # identity's restart observation survives the poll —
            # page.html:1373-1374.
            self.feed['gap'] = None
            self.feed['restart'] = None
            self.note_publication(snapshot)
            self.release_acks(snapshot)
            self.refresh_trends()
            self.refresh_journal()
            self.render_feed()
            self.status = 'tick %s — polled' % snapshot['tick']
        except PollError as error:
            if (self.feed['stale'] is None
                    and self.feed['publication'] is not None):
                self.feed['stale'] = self.feed['publication']
                self.render_feed()
            self.status = 'poll failed: %s' % error
        finally:
            self.polling = False


def sample(value, tick=0, quality='good'):
    """One served HistorySample.sample — the tick-0 stamp a point whose
    value last changed before the first scan carries on every read."""
    return {'value': {'float': value}, 'quality': quality, 'tick': tick}


def entry(seq, value, tick=0):
    return {'seq': seq, 'sample': sample(value, tick)}


def snapshot(tick, published):
    return {'tick': tick, 'publication': {'published': published},
            'points': []}


class SimAckPlant:
    """The smallest controller side the ack pulse exercises: receipted
    `write_value` commands applied at their apply tick over the plant's
    points, and a latching alarm whose `unack` flag sets on the alarm
    condition's rising edge and clears on the `ack` input's — the
    edge-consumed input the page's pulse exists for. A scan is one
    `advance()`: due writes land first, then the block reads its
    inputs, exactly the order an apply tick's rising edge is consumed.

    The transport handler doubles as the fault-injection seam the QA
    reproduction scripts: `blackhole_next` makes the next POST answer
    nothing — the hung listener the abort bound lands on, the write
    never reaching admission — `apply_then_hang` queues the write and
    then answers nothing — the aborts-but-applies variant — and
    `reject_next` answers one refused receipt.
    """

    def __init__(self, pv, ack, alarm, unack, high=90.0):
        self.tick = 0
        self.published = 0
        self.high = high
        self.pv_point = pv
        self.ack_point = ack
        self.alarm_point = alarm
        self.unack_point = unack
        self.points = {pv: 50.0, ack: False, alarm: False, unack: False}
        self.pending = []          # (apply_tick, write_value payload)
        self.prev_ack = False
        self.prev_alarm = False
        self.blackhole_next = False
        self.apply_then_hang = False
        self.reject_next = None    # a rejection-reason dict, or None

    def set_pv(self, value):
        self.points[self.pv_point] = value

    def command(self, body):
        """The /command endpoint: a write_value admits for the next
        tick — the accepted receipt carrying its apply tick — with the
        scripted failures deciding what the listener answers."""
        write = (body.get('command') or body)['write_value']
        if self.blackhole_next:
            # The hung listener: the POST never reached admission, so
            # the abort bound is the page's only answer.
            self.blackhole_next = False
            return HANG
        if self.reject_next is not None:
            reason, self.reject_next = self.reject_next, None
            return Response({'command': {'write_value': write},
                             'outcome': {'rejected': {'reason': reason}}})
        self.pending.append((self.tick + 1, write))
        if self.apply_then_hang:
            # Aborts-but-applies: admission queued the write, the
            # answer never arrived.
            self.apply_then_hang = False
            return HANG
        return Response({'command': {'write_value': write},
                         'outcome': {'accepted': {
                             'apply_tick': self.tick + 1}}})

    def advance(self):
        """One scan: every write whose apply tick has arrived lands on
        the image, then the latching-alarm block reads its inputs —
        `unack` latching on the alarm's rising edge, clearing on the
        ack input's."""
        self.tick += 1
        due = [p for p in self.pending if p[0] <= self.tick]
        self.pending = [p for p in self.pending if p[0] > self.tick]
        for _apply_tick, write in due:
            self.points[write['point']] = write['value']['bool']
        alarm = self.points[self.pv_point] > self.high
        if alarm and not self.prev_alarm:
            self.points[self.unack_point] = True
        ack = self.points[self.ack_point]
        if ack and not self.prev_ack:
            self.points[self.unack_point] = False
        self.points[self.alarm_point] = alarm
        self.prev_alarm, self.prev_ack = alarm, ack
        self.published += 1

    def _row(self, point):
        value = self.points[point]
        return {'point': point, 'sample': {
            'value': {'bool': value} if isinstance(value, bool)
                     else {'float': value},
            'quality': 'good', 'tick': self.tick}}

    def _snapshot(self):
        return {
            'tick': self.tick,
            'publication': {'published': self.published},
            'points': [self._row(p) for p in self.points],
            'descriptors': [{'name': 'lal-1', 'ports': [
                {'name': 'in', 'direction': 'in', 'kind': 'float',
                 'role': 'measurement', 'point': self.pv_point},
                {'name': 'ack', 'direction': 'in', 'kind': 'bool',
                 'role': 'status', 'point': self.ack_point},
                {'name': 'alarm', 'direction': 'out', 'kind': 'bool',
                 'role': 'status', 'point': self.alarm_point},
                {'name': 'unacknowledged', 'direction': 'out',
                 'kind': 'bool', 'role': 'status',
                 'point': self.unack_point}]}],
            'forces': []}

    def serve(self, path, body):
        """The StubTransport fallback: reads answer from the plant's
        state, POST /command admits through `command`."""
        if path == 'role':
            return Response({'role': 'active', 'tick': self.tick,
                             'sync': None})
        if path == 'snapshot':
            return Response(self._snapshot())
        if path in ('schema', 'resources'):
            return Response({})
        if path in ('history', 'journal'):
            return Response([])
        if path == 'command':
            return self.command(body)
        raise AssertionError('unhandled path ' + path)


class PageLogicPins(unittest.TestCase):
    """The line-reference pins: every page.html statement the replica
    mirrors, asserted at its exact line so a drifted page fails here
    naming the line rather than silently unmooring the harness."""

    def test_mirrored_page_logic_lines(self):
        for number, expected in PINS:
            self.assertEqual(
                expected, page_line(number),
                'page.html:%s drifted — the harness mirrors this line; '
                're-verify the replica against the page' % number)


class FeedGapLatch(unittest.TestCase):
    """The publication-gap latch: a since-poll stepping over an evicted
    stretch notes the named gap, and the next in-sequence poll clears it
    within that one poll — feed.gap recomputes per refresh, never
    latching past the first in-sequence answer."""

    def rig(self):
        transport = StubTransport()
        return PageReplica(transport, points=(1,)), transport

    def test_gap_marks_then_in_sequence_poll_clears(self):
        page, transport = self.rig()
        transport.script('/role', [Response({'role': 'active', 'tick': t,
                                             'sync': None})
                                   for t in (1, 2, 3)])
        transport.script('/schema', [Response({})] * 3)
        transport.script('/resources', [Response({})] * 3)
        transport.script('/snapshot', [Response(snapshot(1, 1)),
                                       Response(snapshot(2, 2)),
                                       Response(snapshot(3, 3))])
        # The priming answer seats the cursor, then the two synthetic
        # /history responses the issue names: one stepping over the
        # cursor's successor — seqs 3–4 evicted before the page read
        # them — then one returning first.seq == cursor + 1.
        transport.script('/history', [
            Response([{'point': 1, 'samples': [entry(1, 1.0),
                                              entry(2, 1.0)]}]),
            Response([{'point': 1, 'samples': [entry(5, 2.0)]}]),
            Response([{'point': 1, 'samples': [entry(6, 3.0)]}]),
        ])
        transport.script('/journal', [Response([])] * 3)

        page.refresh()
        self.assertTrue(page.feed_line['hidden'])
        self.assertEqual(2, page.trends[1]['lastSeq'])

        page.refresh()
        self.assertEqual({'stream': 'history', 'from': 3, 'through': 4},
                         page.feed['gap'])
        self.assertFalse(page.feed_line['hidden'])
        self.assertEqual('gap', page.feed_line['class'])
        self.assertIn('publication gap: history seqs 3–4',
                      page.feed_line['text'])

        # The in-sequence poll clears the mark within that one poll:
        # the refresh resets the note and no stepped-over stretch
        # re-marks it.
        page.refresh()
        self.assertIsNone(page.feed['gap'])
        self.assertTrue(page.feed_line['hidden'],
                        'feed.gap latched past an in-sequence poll: %r'
                        % page.feed_line)
        self.assertEqual(6, page.trends[1]['lastSeq'])


class JournalPinnedBoundaryGap(unittest.TestCase):
    """qa-journal-pinned-boundary-internal-gap-unseen: a run_boundary
    the bounded journal tail evicted survives pinned ahead of the ring,
    so one /journal answer reads [boundary@low-seq, ring@high-seq] — an
    evicted stretch wholly inside the response. The head-only check was
    blind to it: skipped outright at cursor 0 (a fresh page or a
    post-restart refetch) and naming only the head stretch at a cursor
    below the boundary. The consecutive-pair check marks the internal
    jump at either."""

    def rig(self, polls):
        transport = StubTransport()
        transport.script('/role', [Response({'role': 'active', 'tick': t,
                                             'sync': None})
                                   for t in range(1, polls + 1)])
        transport.script('/schema', [Response({})] * polls)
        transport.script('/resources', [Response({})] * polls)
        return PageReplica(transport, points=(1,)), transport

    @staticmethod
    def jentry(seq, tick):
        return {'seq': seq, 'tick': tick,
                'event': {'point_changed': {'point': 1, 'from': None,
                                            'to': {'float': 1.0}}}}

    @staticmethod
    def boundary(seq, run=2):
        return {'seq': seq, 'tick': 0,
                'event': {'run_boundary': {'run': run}}}

    def test_internal_jump_marks_the_gap_at_cursor_zero(self):
        # The QA reproduction's served shape: the restart journaled
        # run 2's boundary at seq 450, a >capacity flood evicted the
        # ring to 648+, and the fresh page's since=0 read answers
        # [450, 648, 649, ...] — the internal jump the head-only check
        # could not see.
        page, transport = self.rig(polls=2)
        transport.script('/snapshot', [Response(snapshot(10, 6)),
                                       Response(snapshot(11, 7))])
        transport.script('/history', [Response([]), Response([])])
        transport.script('/journal', [
            Response([self.boundary(450), self.jentry(648, 10),
                      self.jentry(649, 10)]),
            Response([self.jentry(650, 11)]),
        ])

        page.refresh()
        self.assertEqual({'stream': 'journal', 'from': 451,
                          'through': 647},
                         page.feed['gap'])
        self.assertFalse(page.feed_line['hidden'])
        self.assertEqual('gap', page.feed_line['class'])
        self.assertIn('publication gap: journal seqs 451–647',
                      page.feed_line['text'])
        # The merged boundary still names the restart beside the gap.
        self.assertIn('source restarted', page.feed_line['text'])

        # The next in-sequence poll clears both marks.
        page.refresh()
        self.assertIsNone(page.feed['gap'])
        self.assertIsNone(page.feed['restart'])
        self.assertTrue(page.feed_line['hidden'])

    def test_internal_jump_marks_past_a_flagged_head_stretch(self):
        # The cursor-below-the-boundary variant from the evidence: the
        # head check already flags the 301–449 stretch it can see, but
        # the internal 451–647 jump is the wider gap — the note must
        # name it rather than stopping at the head.
        page, transport = self.rig(polls=2)
        transport.script('/snapshot', [Response(snapshot(10, 6)),
                                       Response(snapshot(11, 7))])
        transport.script('/history', [Response([]), Response([])])
        transport.script('/journal', [
            Response([self.jentry(298, 9), self.jentry(299, 9),
                      self.jentry(300, 9)]),
            Response([self.boundary(450), self.jentry(648, 10),
                      self.jentry(649, 10)]),
        ])

        page.refresh()
        self.assertTrue(page.feed_line['hidden'])
        self.assertEqual(300, page.journal_since)

        page.refresh()
        self.assertEqual({'stream': 'journal', 'from': 451,
                          'through': 647},
                         page.feed['gap'])
        self.assertIn('publication gap: journal seqs 451–647',
                      page.feed_line['text'])


class TickZeroSeqFallback(unittest.TestCase):
    """The tick-0 trend: a point whose served stamps never advance the
    tick domain still extends its series on each new served entry — the
    seq cursor, not the tick watermark, proves novelty on an
    incremental poll."""

    def rig(self, history_answers, polls):
        transport = StubTransport()
        transport.script('/role', [Response({'role': 'active', 'tick': t,
                                             'sync': None})
                                   for t in range(1, polls + 1)])
        transport.script('/schema', [Response({})] * polls)
        transport.script('/resources', [Response({})] * polls)
        transport.script('/snapshot', [Response(snapshot(t, t))
                                       for t in range(1, polls + 1)])
        transport.script('/history', history_answers)
        transport.script('/journal', [Response([])] * polls)
        return PageReplica(transport, points=(1,))

    def test_value_change_at_tick_zero_appends_via_seq(self):
        page = self.rig([
            Response([{'point': 1, 'samples': [entry(1, 1.0)]}]),
            Response([{'point': 1, 'samples': [entry(2, 2.0)]}]),
        ], polls=2)
        page.refresh()
        self.assertEqual([1.0],
                         [s['value']['float']
                          for s in page.trends[1]['samples']])
        page.refresh()
        # The second poll's entry advances seq with the stamp still at
        # tick 0: the seq fallback draws the change instead of the tick
        # watermark freezing the series on the first sample.
        self.assertEqual([1.0, 2.0],
                         [s['value']['float']
                          for s in page.trends[1]['samples']])

    def test_tick_zero_batch_draws_every_served_sample(self):
        # The first poll re-reads the whole ring: nothing drawn yet, so
        # the watermark has nothing to dedupe against — every served
        # tick-0 sample lands.
        page = self.rig([
            Response([{'point': 1, 'samples': [entry(1, 1.0),
                                              entry(2, 2.0),
                                              entry(3, 2.0)]}]),
        ], polls=1)
        page.refresh()
        self.assertEqual([1.0, 2.0, 2.0],
                         [s['value']['float']
                          for s in page.trends[1]['samples']])

    def test_source_switch_refetch_still_dedupes_by_tick(self):
        # The watermark's surviving job: a source switch's whole-stream
        # re-read skips the ticks already drawn and appends only what
        # the new source adds — the pair shares one tick domain.
        transport = StubTransport()
        page = PageReplica(transport, peers=('', 'http://b'), points=(1,))
        transport.script('/role', [
            Response({'role': 'active', 'tick': 9, 'sync': None}),
            Response({'role': 'standby', 'tick': 9, 'sync': None}),
            Response({'role': 'standby', 'tick': 10, 'sync': None}),
            Response({'role': 'active', 'tick': 10, 'sync': None}),
        ])
        transport.script('/schema', [Response({})] * 2)
        transport.script('/resources', [Response({})] * 2)
        transport.script('/snapshot', [Response(snapshot(9, 4)),
                                       Response(snapshot(10, 2))])
        transport.script('/history', [
            Response([{'point': 1, 'samples': [entry(1, 1.0, tick=7),
                                              entry(2, 1.5, tick=8),
                                              entry(3, 2.0, tick=9)]}]),
            # The new source's whole ring re-serves ticks 7–9 (already
            # drawn — its own seq numbering says nothing about the old
            # stream) plus the tick-10 sample it adds.
            Response([{'point': 1, 'samples': [entry(1, 1.0, tick=7),
                                              entry(2, 1.5, tick=8),
                                              entry(3, 2.0, tick=9),
                                              entry(4, 2.5, tick=10)]}]),
        ])
        transport.script('/journal', [Response([]), Response([])])
        page.refresh()
        self.assertEqual([1.0, 1.5, 2.0],
                         [s['value']['float']
                          for s in page.trends[1]['samples']])
        page.refresh()
        self.assertEqual(1, page.source)
        self.assertEqual([1.0, 1.5, 2.0, 2.5],
                         [s['value']['float']
                          for s in page.trends[1]['samples']])
        self.assertIsNone(page.feed['gap'])


def envelope(point, run, samples):
    """One served PointHistory — the envelope carries the producing
    run's lifetime marker beside the samples."""
    return {'point': point, 'run': run, 'samples': samples}


class SourceRestartDetection(unittest.TestCase):
    """The same-source restart — #884's consumer half. A monitor restart
    regresses the publication identity, advances the served history
    `run` marker, and lands a journal `run_boundary`; each observation
    resets the page's stream cursors and marks the feed 'source
    restarted' rather than stitching the new lifetime's restarted
    numbering onto the old cursor."""

    def rig(self, polls):
        transport = StubTransport()
        transport.script('/role', [Response({'role': 'active', 'tick': t,
                                             'sync': None})
                                   for t in range(1, polls + 1)])
        transport.script('/schema', [Response({})] * polls)
        transport.script('/resources', [Response({})] * polls)
        return PageReplica(transport, points=(1,)), transport

    def test_cold_restart_marks_restart_and_redraws_the_new_lifetime(self):
        # The defect's shape: the restarted run's seqs renumber from 1
        # under a pre-restart cursor — an empty since-read the old page
        # stitched across. Now the regressed publication identity names
        # the restart, the cursors reset mid-poll, and the restarted
        # tick domain clears the old lifetime's drawn series.
        page, transport = self.rig(polls=3)
        transport.script('/snapshot', [Response(snapshot(9, 5)),
                                       Response(snapshot(1, 1)),
                                       Response(snapshot(2, 2))])
        transport.script('/history', [
            Response([envelope(1, 1, [entry(9, 1.0, tick=9)])]),
            # The restart poll: the since read re-fetches whole — the
            # reset cursor's since=0 — serving the new run's ring.
            Response([envelope(1, 2, [entry(1, 2.0, tick=1)])]),
            Response([envelope(1, 2, [entry(2, 3.0, tick=2)])]),
        ])
        transport.script('/journal', [Response([])] * 3)

        page.refresh()
        self.assertTrue(page.feed_line['hidden'])
        self.assertEqual(9, page.trends[1]['lastSeq'])

        # The regressed publication (published 5→1, tick 9→1) proves the
        # restart: the mark names it, the trend's old-lifetime series
        # clears rather than stitching tick-1 samples onto tick-9 ones.
        page.refresh()
        self.assertFalse(page.feed_line['hidden'])
        self.assertEqual('gap', page.feed_line['class'])
        self.assertIn('source restarted', page.feed_line['text'])
        self.assertIn('publication identity regressed',
                      page.feed_line['text'])
        self.assertEqual([2.0],
                         [s['value']['float']
                          for s in page.trends[1]['samples']])
        self.assertEqual(1, page.trends[1]['lastSeq'])
        self.assertEqual(2, page.trends[1]['run'])

        # The next in-sequence poll clears the mark — restart state is
        # per-poll observation, not a latch.
        page.refresh()
        self.assertIsNone(page.feed['restart'])
        self.assertTrue(page.feed_line['hidden'])
        self.assertEqual([2.0, 3.0],
                         [s['value']['float']
                          for s in page.trends[1]['samples']])

    def test_continued_tick_domain_restart_merges_by_tick(self):
        # The checkpoint-adopted restart: the seq axis rides the
        # continuing tick domain, so the post-restart refetch merges the
        # new run's ring onto the drawn series — only the never-served
        # stretch is absent, exactly the gap the axis exists to show.
        page, transport = self.rig(polls=2)
        transport.script('/snapshot', [Response(snapshot(9, 5)),
                                       Response(snapshot(12, 1))])
        transport.script('/history', [
            Response([envelope(1, 1, [entry(9, 1.0, tick=9)])]),
            # The new run's whole ring: seqs continue the tick domain —
            # 10–12 — while the envelope's marker names run 2.
            Response([envelope(1, 2, [entry(9, 1.0, tick=9),
                                      entry(10, 1.5, tick=10),
                                      entry(11, 2.0, tick=11),
                                      entry(12, 2.5, tick=12)])]),
        ])
        transport.script('/journal', [Response([])] * 2)

        page.refresh()
        page.refresh()
        self.assertIn('source restarted', page.feed_line['text'])
        # The publication regressed (5→1) but the tick domain continued
        # (12 > 9): the drawn series belongs to the line's domain, so it
        # merges rather than clearing — the tick-9 sample serves once.
        self.assertEqual([1.0, 1.5, 2.0, 2.5],
                         [s['value']['float']
                          for s in page.trends[1]['samples']])
        self.assertEqual(12, page.trends[1]['lastSeq'])
        self.assertEqual(2, page.trends[1]['run'])

    def test_run_marker_change_alone_marks_the_restart(self):
        # The run-marker observation on its own: an envelope whose run
        # advanced under a standing cursor — the phantom-idle answer of
        # the defect — names the restart even with no samples and no
        # publication regression to show it.
        page, transport = self.rig(polls=2)
        transport.script('/snapshot', [Response(snapshot(9, 5)),
                                       Response(snapshot(10, 6))])
        transport.script('/history', [
            Response([envelope(1, 1, [entry(9, 1.0, tick=9)])]),
            # since=9 filters the restarted axis's samples out — but
            # the envelope still carries run 2.
            Response([envelope(1, 2, [])]),
        ])
        transport.script('/journal', [Response([])] * 2)

        page.refresh()
        page.refresh()
        self.assertIn('source restarted', page.feed_line['text'])
        self.assertIn('run marker advanced to run 2',
                      page.feed_line['text'])

    def test_journal_run_boundary_marks_the_restart(self):
        # The journal stream's own restart marker: a served
        # run_boundary the page had not yet merged notes the same seam.
        page, transport = self.rig(polls=3)
        transport.script('/snapshot', [Response(snapshot(9, 5)),
                                       Response(snapshot(10, 6)),
                                       Response(snapshot(11, 7))])
        transport.script('/history', [
            Response([envelope(1, 1, [entry(9, 1.0, tick=9)])]),
            Response([envelope(1, 2, [entry(10, 2.0, tick=10)])]),
            Response([envelope(1, 2, [entry(11, 3.0, tick=11)])]),
        ])
        transport.script('/journal', [
            Response([]),
            Response([{'seq': 8, 'tick': 10,
                       'event': {'run_boundary': {'run': 2}}}]),
            Response([]),
        ])

        page.refresh()
        page.refresh()
        self.assertIn('source restarted', page.feed_line['text'])
        self.assertIn('the journal recorded run 2 beginning',
                      page.feed_line['text'])
        # The boundary already merged: a journal re-fetch re-serving it
        # does not re-fire the observation.
        page.refresh()
        self.assertIsNone(page.feed['restart'])
        self.assertTrue(page.feed_line['hidden'])


class JournalRestartMerge(unittest.TestCase):
    """The journal pane across a same-source restart — the
    journal-pane-dedupe-swallows-new-lifetime-entries defect. A cold
    restart with a retained journal re-serves the old lifetime's
    entries, a run_boundary marker at tick 0, and the new lifetime's
    first-observation sweep — every new entry byte-identical in
    (tick, event) to an old-lifetime row. The pane's merge must not
    dedupe the new lifetime away: the merge key's run qualifier
    attributes each entry to its process lifetime, so the new run's
    rows render beside the old's behind the boundary marker."""

    EVENTS = [
        {'point_changed': {'point': point, 'from': None,
                           'to': {'int': point}}}
        for point in (1, 2, 3)
    ]

    def rig(self, polls):
        transport = StubTransport()
        transport.script('/role', [Response({'role': 'active', 'tick': t,
                                             'sync': None})
                                   for t in range(1, polls + 1)])
        transport.script('/schema', [Response({})] * polls)
        transport.script('/resources', [Response({})] * polls)
        return PageReplica(transport, points=(1,)), transport

    @classmethod
    def sweep(cls, seqs):
        """One lifetime's first-observation sweep: identical events
        stamped on the shared colliding tick, distinguished only by the
        seq axis the merge must not key on."""
        return [{'seq': seq, 'tick': 1, 'event': dict(event)}
                for seq, event in zip(seqs, cls.EVENTS)]

    @staticmethod
    def pane(page):
        """The rendered rows' attribution: (run, seq, tick) per merged
        entry, in the pane's tick-then-seq order."""
        return [(e['run'], e['seq'], e['tick'])
                for e in page.journal_entries]

    def test_cold_restart_sweep_renders_the_new_lifetime(self):
        # The reported reproduction's shape: the page established on
        # run 1's sweep, then the source cold-restarts with the journal
        # retained — the next since-poll re-reads the whole two-lifetime
        # journal and every run-2 entry collides with a run-1 row.
        page, transport = self.rig(polls=2)
        transport.script('/snapshot', [Response(snapshot(9, 5)),
                                       Response(snapshot(1, 1))])
        transport.script('/history', [
            Response([envelope(1, 1, [entry(9, 1.0, tick=9)])]),
            Response([envelope(1, 2, [entry(1, 2.0, tick=1)])]),
        ])
        run1 = self.sweep(seqs=(1, 2, 3))
        boundary = {'seq': 4, 'tick': 0,
                    'event': {'run_boundary': {'run': 2}}}
        run2 = self.sweep(seqs=(5, 6, 7))
        transport.script('/journal', [
            Response(run1),
            Response(run1 + [boundary] + run2),
        ])

        page.refresh()
        self.assertEqual([(0, 1, 1), (0, 2, 1), (0, 3, 1)],
                         self.pane(page))

        page.refresh()
        # The run-qualified merge key attributes the colliding rows to
        # their own lifetimes: the boundary marker draws the seam and
        # all of run 2's sweep renders beside run 1's — none of the
        # three (tick, event)-identical new-lifetime entries dropped.
        self.assertEqual([(2, 4, 0)]
                         + [(0, s, 1) for s in (1, 2, 3)]
                         + [(2, s, 1) for s in (5, 6, 7)],
                         self.pane(page))
        self.assertIn('source restarted', page.feed_line['text'])
        self.assertIn('the journal recorded run 2 beginning',
                      page.feed_line['text'])

    def test_boundary_alone_then_whole_refetch_renders_both_lifetimes(self):
        # The variant the finding names: the boundary arrives as its
        # batch's last entry, so the cursor reset survives the loop and
        # the next poll re-reads the whole journal. The re-served
        # boundary re-marks the attribution ahead of the dedupe, so the
        # old lifetime's rows still merge under run 0 and run 2's sweep
        # renders behind the seam — nothing deduped away either way.
        page, transport = self.rig(polls=3)
        transport.script('/snapshot', [Response(snapshot(9, 5)),
                                       Response(snapshot(10, 6)),
                                       Response(snapshot(11, 7))])
        transport.script('/history', [
            Response([envelope(1, 1, [entry(9, 1.0, tick=9)])]),
            Response([envelope(1, 2, [entry(10, 2.0, tick=10)])]),
            Response([envelope(1, 2, [entry(11, 3.0, tick=11)])]),
        ])
        run1 = self.sweep(seqs=(1, 2, 3))
        boundary = {'seq': 4, 'tick': 0,
                    'event': {'run_boundary': {'run': 2}}}
        run2 = self.sweep(seqs=(5, 6, 7))
        transport.script('/journal', [
            Response(run1),
            Response([boundary]),
            # journalSince survived at 0: the whole-journal re-read.
            Response(run1 + [boundary] + run2),
        ])

        page.refresh()
        page.refresh()
        # The boundary merged and marked the restart; run 1's rows keep
        # rendering ahead of the seam — the marker is a lifetime
        # boundary, not a deletion.
        self.assertEqual([(2, 4, 0)]
                         + [(0, s, 1) for s in (1, 2, 3)],
                         self.pane(page))
        self.assertIn('the journal recorded run 2 beginning',
                      page.feed_line['text'])

        page.refresh()
        # The whole re-read re-walks the attribution: run 1's rows
        # dedupe under their original run-0 keys (still rendered, not
        # duplicated), the re-served boundary dedupes under run 2, and
        # run 2's sweep renders — the tick-0 marker heads the pane.
        self.assertEqual([(2, 4, 0)]
                         + [(0, s, 1) for s in (1, 2, 3)]
                         + [(2, s, 1) for s in (5, 6, 7)],
                         self.pane(page))
        self.assertIsNone(page.feed['restart'])

    def test_warm_restart_boundary_keeps_the_continuing_domain(self):
        # The finding's control: a state-file resume continues the tick
        # domain — the boundary rides the restored tick, no
        # re-observation sweep follows, and the pane keeps merging
        # across the seam on the continued domain's ticks.
        page, transport = self.rig(polls=2)
        transport.script('/snapshot', [Response(snapshot(9, 5)),
                                       Response(snapshot(12, 1))])
        transport.script('/history', [
            Response([envelope(1, 1, [entry(9, 1.0, tick=9)])]),
            Response([envelope(1, 2, [entry(10, 1.5, tick=10)])]),
        ])
        run1 = [{'seq': 1, 'tick': 8, 'event': dict(self.EVENTS[0])},
                {'seq': 2, 'tick': 9, 'event': dict(self.EVENTS[1])}]
        boundary = {'seq': 3, 'tick': 9,
                    'event': {'run_boundary': {'run': 2}}}
        run2 = [{'seq': 4, 'tick': 10, 'event': dict(self.EVENTS[2])}]
        transport.script('/journal', [
            Response(run1),
            Response(run1 + [boundary] + run2),
        ])

        page.refresh()
        page.refresh()
        # The boundary sorts into the seam it marks and both lifetimes'
        # rows stand — attributed run 0 and run 2.
        self.assertEqual([(0, 1, 8), (0, 2, 9), (2, 3, 9), (2, 4, 10)],
                         self.pane(page))
        self.assertIn('source restarted', page.feed_line['text'])


class HangingPollDegradation(unittest.TestCase):
    """The bounded poll: a listener that never answers degrades to the
    page's named state inside one poll period instead of freezing the
    view — an unreachable peer for the role read, a stale publication
    for a failed data read."""

    def test_hanging_history_marks_stale_within_one_poll_period(self):
        transport = StubTransport()
        page = PageReplica(transport, points=(1,))
        transport.script('/role', [Response({'role': 'active', 'tick': 1,
                                             'sync': None}),
                                   Response({'role': 'active', 'tick': 2,
                                             'sync': None})])
        transport.script('/schema', [Response({})] * 2)
        transport.script('/resources', [Response({})] * 2)
        transport.script('/snapshot', [Response(snapshot(1, 1)),
                                       Response(snapshot(2, 2))])
        # The first poll's since-read lands; the second's hangs — the
        # listener never answers and the abort bound decides.
        transport.script('/history', [Response([{'point': 1, 'samples':
                                                 [entry(1, 1.0)]}]),
                                      HANG])
        transport.script('/journal', [Response([])])
        page.refresh()
        self.assertTrue(page.feed_line['hidden'])
        start = page.now
        page.refresh()
        self.assertLessEqual(page.now - start, POLL_MS,
                             'the hanging read held the poll past one '
                             'period: %sms' % (page.now - start))
        self.assertEqual({'published': 2, 'tick': 2}, page.feed['stale'])
        self.assertFalse(page.feed_line['hidden'])
        self.assertEqual('stale', page.feed_line['class'])
        self.assertIn('stale publication', page.feed_line['text'])
        self.assertIn('poll failed', page.status)

    def test_hanging_role_marks_peer_unreachable_within_one_period(self):
        transport = StubTransport()
        page = PageReplica(transport, peers=('', 'http://b'), points=(1,))
        transport.script('/role', [
            Response({'role': 'active', 'tick': 1, 'sync': None}),
            Response({'role': 'standby', 'tick': 1, 'sync': None}),
            Response({'role': 'active', 'tick': 2, 'sync': None}),
            HANG,
        ])
        transport.script('/schema', [Response({})] * 2)
        transport.script('/resources', [Response({})] * 2)
        transport.script('/snapshot', [Response(snapshot(1, 1)),
                                       Response(snapshot(2, 2))])
        transport.script('/history', [
            Response([{'point': 1, 'samples': [entry(1, 1.0)]}]),
            Response([{'point': 1, 'samples': [entry(2, 2.0)]}]),
        ])
        transport.script('/journal', [Response([]), Response([])])
        page.refresh()
        self.assertEqual('reachable', page.peer_rows[1])
        start = page.now
        page.refresh()
        self.assertLessEqual(page.now - start, POLL_MS)
        self.assertIsNotNone(page.peer_state[1]['error'])
        self.assertEqual('unreachable', page.peer_rows[1])
        # The surviving peer's data path keeps polling — one peer's
        # hang degrades to its own named fault, never the view's.
        self.assertFalse(page.status.startswith('poll failed'))
        self.assertEqual([1.0, 2.0],
                         [s['value']['float']
                          for s in page.trends[1]['samples']])

    def test_command_post_inside_the_poll_carries_the_same_bound(self):
        # postCommand is the one fetch refresh() awaits that is not a
        # pollFetch — the ack-release's write inside the poll loop. A
        # listener answering nothing there would hold `polling` forever,
        # and every feed mark set before the hang would stay set: the
        # page's read bound must cover the command post too.
        body = PAGE.read_text()
        start = body.index('async function postCommand')
        end = body.index('return response.json()', start)
        self.assertIn(
            'signal: AbortSignal.timeout(POLL_MS)', body[start:end],
            'postCommand lost its abort bound — an unanswered /command '
            'POST wedges the poll loop and freezes the feed marks')


class AckReleaseRetry(unittest.TestCase):
    """The ack pulse's release half — the dropped-release defect. The
    release entry must stay armed until the release write is receipted:
    a hung listener past POLL_MS, a transport error, or a refused
    receipt retries on a later poll rather than latching the `ack`
    input true — held true, every later press receipts `applied` while
    landing no rising edge, acknowledging nothing. The held-true audit
    likewise arms a release for an input standing true without one —
    the acknowledge write that applied past its abort-bound answer."""

    def rig(self):
        plant = SimAckPlant(pv=10, ack=11, alarm=20, unack=21)
        transport = StubTransport()
        transport.handler = plant.serve
        page = PageReplica(
            transport, points=(10, 11, 20, 21),
            metas={11: {'point': 11, 'name': 'lal-1.ack',
                        'direction': 'in', 'value_type': 'bool',
                        'writable': True}})
        return page, transport, plant

    @staticmethod
    def posted_writes(transport, point, value):
        """The write_value bodies the page posted for `point` at
        `value` — the press's true and the release's false."""
        return [body['write_value'] for _url, body in transport.posts
                if body.get('write_value', {}).get('point') == point
                and body['write_value'].get('value') == {'bool': value}]

    def test_dropped_release_retries_and_the_next_press_still_edges(self):
        # The QA reproduction: the fault trips the alarm unacknowledged;
        # the press's release POST hangs past the abort bound and never
        # reached admission; the fault cycles and re-asserts unack; and
        # the next press must still land an edge.
        page, transport, plant = self.rig()
        plant.set_pv(95.0)
        plant.advance()
        page.refresh()
        self.assertTrue(plant.points[21])

        # The press receipts accepted for the next tick and arms the
        # release at the apply tick; the applying scan consumes the
        # edge — the latch clears while the input stands true.
        page.submit_ack(11)
        self.assertEqual({11: 2}, page.ack_releases)
        plant.advance()
        self.assertTrue(plant.points[11])
        self.assertFalse(plant.points[21])

        # The release POST blackholes past POLL_MS: the entry stays
        # armed — the release is retried, not dropped — and the
        # receipt pane reports the indeterminate outcome, not a
        # failure: the abandoned request may still apply server-side.
        plant.blackhole_next = True
        page.refresh()
        self.assertIn(11, page.ack_releases,
                      'the aborted release was dropped — the input '
                      'stays held against every later press')
        self.assertTrue(plant.points[11])
        self.assertTrue(
            page.receipt.startswith('command outcome unknown'),
            'an abort-abandoned submission must report an '
            'indeterminate outcome, not a failure: %r' % page.receipt)
        self.assertNotIn('command failed', page.receipt)
        self.assertEqual(1, len(page.pending_commands))
        self.assertLessEqual(len(self.posted_writes(transport, 11, False)),
                             1)

        # The fault cycles and the alarm re-asserts unacknowledged while
        # the input still stands — the defect's dead-press setup.
        plant.set_pv(50.0)
        plant.advance()
        plant.set_pv(95.0)
        plant.advance()
        page.refresh()
        self.assertTrue(plant.points[21])
        self.assertTrue(plant.points[11])
        # The poll retried the release: the accepted answer disarms the
        # entry, and the write drops the input at its apply tick.
        self.assertNotIn(11, page.ack_releases)
        self.assertEqual(2,
                         len(self.posted_writes(transport, 11, False)))
        plant.advance()
        self.assertFalse(plant.points[11])

        # The subsequent press lands its rising edge: unacknowledged is
        # consumed, exactly what the latched input defeated.
        page.submit_ack(11)
        self.assertEqual({11: 6}, page.ack_releases)
        plant.advance()
        self.assertFalse(plant.points[21],
                         'the retried release left the input held — '
                         'the press receipted but acknowledged nothing')

    def test_refused_release_receipt_keeps_the_release_armed(self):
        # The silent-drop variant through the answer, not the throw: a
        # release POST answered with a refused receipt returns normally
        # from submitCommand — the loop must keep the entry armed and
        # retry on the next poll rather than ignoring the answer.
        page, _transport, plant = self.rig()
        plant.set_pv(95.0)
        plant.advance()
        page.refresh()
        page.submit_ack(11)
        plant.advance()
        self.assertTrue(plant.points[11])
        self.assertEqual({11: 2}, page.ack_releases)

        plant.reject_next = {'point_forced': {'point': 11}}
        page.refresh()
        self.assertIn(11, page.ack_releases,
                      'a rejected release receipt dropped the release')
        self.assertTrue(plant.points[11])

        page.refresh()
        self.assertNotIn(11, page.ack_releases)
        plant.advance()
        self.assertFalse(plant.points[11])

    def test_release_with_no_active_peer_stays_armed_and_retries(self):
        # The null-answer variant the finding names: the pair reports
        # no settled-active peer — mid-transition — so submitCommand
        # answers null without sending. The release must stay armed and
        # retry once an active peer reports again rather than dropping
        # with the input held.
        page, transport, plant = self.rig()
        plant.set_pv(95.0)
        plant.advance()
        page.refresh()
        page.submit_ack(11)
        plant.advance()
        self.assertTrue(plant.points[11])
        self.assertEqual({11: 2}, page.ack_releases)

        # Both role reads the poll makes — the refresh's own and the
        # re-poll inside submitCommand — report standby: the release
        # has no legitimate target and nothing is posted.
        transport.script('/role', [
            Response({'role': 'standby', 'tick': 2, 'sync': None}),
            Response({'role': 'standby', 'tick': 2, 'sync': None}),
        ])
        page.refresh()
        self.assertIn(11, page.ack_releases,
                      'the unsent release was dropped — the input '
                      'stays held against every later press')
        self.assertTrue(plant.points[11])
        self.assertEqual(0,
                         len(self.posted_writes(transport, 11, False)))
        self.assertIn('not sent', page.receipt)

        # The pair settles: the next poll retries the release, the
        # accepted answer disarms the entry, and the write drops the
        # input at its apply tick.
        page.refresh()
        self.assertNotIn(11, page.ack_releases)
        self.assertEqual(1,
                         len(self.posted_writes(transport, 11, False)))
        plant.advance()
        self.assertFalse(plant.points[11])

        # A re-asserted alarm still acknowledges on the next press —
        # the retried release left the input low for the edge.
        plant.set_pv(50.0)
        plant.advance()
        plant.set_pv(95.0)
        plant.advance()
        self.assertTrue(plant.points[21])
        page.submit_ack(11)
        plant.advance()
        self.assertFalse(plant.points[21])

    def test_lost_press_receipt_releases_via_the_held_input_audit(self):
        # The aborts-but-applies variant — the issue-948 reproduction:
        # the acknowledge write lands at its apply tick but the answer
        # never arrives — no receipt, no armed release. The page must
        # report the indeterminate outcome rather than "command
        # failed"; the journaled settle then resolves the pending
        # record, and the poll's audit arms the release when the
        # snapshot serves the input held true.
        page, transport, plant = self.rig()
        plant.set_pv(95.0)
        plant.advance()
        page.refresh()
        self.assertTrue(plant.points[21])

        plant.apply_then_hang = True
        page.submit_ack(11)
        self.assertEqual({}, page.ack_releases,
                         'the lost receipt armed a release anyway')
        self.assertTrue(
            page.receipt.startswith('command outcome unknown'),
            'the abandoned press must report an indeterminate '
            'outcome, not a failure: %r' % page.receipt)
        self.assertNotIn('command failed', page.receipt)
        self.assertEqual(1, len(page.pending_commands))

        # The queued write applies: the edge consumes the latch and the
        # input stands true with nothing scheduled to drop it.
        plant.advance()
        self.assertTrue(plant.points[11])
        self.assertFalse(plant.points[21])

        # The held-true observation arms and issues the release in the
        # same poll; the accepted answer disarms the entry again. The
        # journaled command_settled — the receipted contract's truth —
        # retires the abandoned press's pending record.
        press = {'write_value': {'point': 11, 'kind': 'bool',
                                 'value': {'bool': True}}}
        transport.script('journal', [Response([{
            'seq': 1, 'tick': plant.tick,
            'event': {'command_settled': {'receipt': {
                'command': press,
                'outcome': {'applied': {'tick': plant.tick}}}}}}])])
        page.refresh()
        self.assertEqual(1,
                         len(self.posted_writes(transport, 11, False)))
        self.assertNotIn(11, page.ack_releases)
        self.assertEqual([], page.pending_commands,
                         'the journaled settle left the abandoned '
                         'press unresolved')
        plant.advance()
        self.assertFalse(plant.points[11])

        # A re-asserted alarm still acknowledges on the next press —
        # the pulse completed instead of latching the input.
        plant.set_pv(50.0)
        plant.advance()
        plant.set_pv(95.0)
        plant.advance()
        self.assertTrue(plant.points[21])
        page.submit_ack(11)
        plant.advance()
        self.assertFalse(plant.points[21])

    def test_abandoned_submission_pane_notice_resolves_on_the_settle(self):
        # The pane-facing half of the reproduction: a bare submitCommand
        # — no intervening submission repaints the receipt — whose
        # applied settle the journal then serves must replace the
        # standing "outcome unknown" notice with the settled verdict.
        # The write targets the alarm point, not the ack input, so the
        # resolving poll arms no release post that would repaint the
        # pane ahead of the settle.
        page, transport, plant = self.rig()
        plant.set_pv(95.0)
        plant.advance()
        page.refresh()

        write = {'write_value': {'point': 20, 'kind': 'bool',
                                 'value': {'bool': False}}}
        plant.apply_then_hang = True
        answer = page.submit_command(write)
        self.assertEqual({'indeterminate': {
                             'detail': 'AbortError: signal timed out'}},
                         answer['outcome'])
        self.assertTrue(
            page.receipt.startswith('command outcome unknown'))
        self.assertNotIn('command failed', page.receipt)
        # No retry: the unresolved submission posted exactly once —
        # resending a command whose fate is open doubles a landed one.
        self.assertEqual(1, len(transport.posts))

        plant.advance()
        transport.script('journal', [Response([{
            'seq': 1, 'tick': plant.tick,
            'event': {'command_settled': {'receipt': {
                'command': write,
                'outcome': {'applied': {'tick': plant.tick}}}}}}])])
        page.refresh()
        self.assertEqual([], page.pending_commands)
        self.assertTrue(
            page.receipt.startswith('the abandoned submission settled'),
            'the journaled settle must resolve the standing notice: '
            '%r' % page.receipt)
        self.assertIn('applied at tick', page.receipt)


if __name__ == '__main__':
    unittest.main()
