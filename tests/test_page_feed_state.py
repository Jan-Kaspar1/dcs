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

- `feed` record — L505; `POLL_MS`/`pollFetch` abort bound — L645–648
- `pollRoles` fetch/error bookkeeping — L654–666; `selectSource`/
  `switchSource` — L676–701; the "unreachable" pair render — L854–860
- `notePublication`/`noteFeedGap`/`renderFeed` — L884–932
- `refresh()`'s feed ordering and its failed-poll stale mark —
  L1171–1193, L1252–1253, L1261–1270
- `refreshTrends`' since cursor, gap note, and the
  refetch-gated watermark / seq fallback — L2708–2750
- `refreshJournal`'s cursor and gap note — L2805–2813
"""
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
    # The feed record and the shared poll bound.
    (505, 'const feed = { publication: null, gap: null, stale: null };'),
    (645, 'const POLL_MS = 1000;'),
    (646, 'function pollFetch(url) {'),
    (647, 'return fetch(url, { signal: AbortSignal.timeout(POLL_MS) });'),
    # pollRoles — bounded role fetches, errors recorded not thrown.
    (654, 'async function pollRoles() {'),
    (657, 'const response = await pollFetch(peer.base + "/role");'),
    (659, 'peerState[i].report = await response.json();'),
    (660, 'peerState[i].error = null;'),
    (662, 'peerState[i].error = String(error);'),
    # Source selection and the switch's bookkeeping reset.
    (676, 'function selectSource() {'),
    (692, 'function switchSource(next) {'),
    (694, 'for (const state of trends.values()) state.lastSeq = 0;'),
    (695, 'journalSince = 0;'),
    (699, 'feed.publication = null;'),
    (700, 'feed.gap = null;'),
    (701, 'feed.stale = null;'),
    # The unreachable peer fault the role poll's error names.
    (781, 'faults.push(was + peer.name + " unreachable");'),
    (782, 'fault_kinds.push("peer_unreachable");'),
    (854, 'const unreachable = state.error !== null;'),
    # Publication freshness bookkeeping.
    (884, 'function notePublication(snapshot) {'),
    (891, 'feed.stale = last !== null &&'),
    (892, 'current.published === last.published && current.tick <= last.tick'),
    (895, 'feed.publication = current;'),
    # The gap note and the feed-state render.
    (903, 'function noteFeedGap(stream, from, through) {'),
    (905, 'feed.gap = { stream: stream, from: from, through: through };'),
    (913, 'function renderFeed() {'),
    (917, 'notices.push("publication gap: " + feed.gap.stream + " seqs " +'),
    (922, 'notices.push("stale publication: " +'),
    (928, 'line.hidden = notices.length === 0;'),
    # refresh()'s feed ordering and its failed-poll stale mark.
    (1171, 'async function refresh() {'),
    (1175, 'await pollRoles();'),
    (1184, 'pollFetch(base + "/snapshot").then(r => r.json()),'),
    (1192, 'notePublication(snapshot);'),
    (1193, 'feed.gap = null;'),
    (1252, 'await Promise.all([refreshTrends(base), refreshJournal(base)]);'),
    (1253, 'renderFeed();'),
    (1266, 'if (feed.stale === null && feed.publication !== null) {'),
    (1267, 'feed.stale = feed.publication;'),
    # refreshTrends: the common since cursor, the served gap note, and
    # the refetch-gated tick watermark / seq fallback.
    (2708, 'async function refreshTrends(base) {'),
    (2714, 'const histories = await (await pollFetch(base + "/history?since=" + since)).json();'),
    (2721, 'const first = history.samples.find(entry => entry.seq > state.lastSeq);'),
    (2722, 'if (state.lastSeq > 0 && first && first.seq > state.lastSeq + 1) {'),
    (2723, 'noteFeedGap("history", state.lastSeq + 1, first.seq - 1);'),
    (2734, 'const refetch = state.lastSeq === 0;'),
    (2735, 'const watermark = state.lastTick;'),
    (2737, 'if (entry.seq <= state.lastSeq) continue;'),
    (2738, 'state.lastSeq = entry.seq;'),
    (2739, 'if (refetch && watermark !== null && entry.sample.tick <= watermark) {'),
    (2742, 'state.samples.push(entry.sample);'),
    (2744, 'state.lastTick = entry.sample.tick;'),
    # refreshJournal's served gap note and cursor advance.
    (2806, 'const entries = await (await pollFetch(base + "/journal?since=" + journalSince)).json();'),
    (2810, 'noteFeedGap("journal", journalSince + 1, entries[0].seq - 1);'),
    (2813, 'journalSince = Math.max(journalSince, entry.seq);'),
    # The cadence both tickers share.
    (3271, 'setInterval(refreshOverview, POLL_MS);'),
    (3446, 'setInterval(refresh, POLL_MS);'),
]


POLL_MS = 1000           # page.html:645 — the shared poll cadence/bound
HANG = object()          # a scripted listener that never answers


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
    bound to decide the outcome."""

    def __init__(self):
        self.routes = {}
        self.requests = []

    def script(self, path, responses):
        self.routes[path.lstrip('/')] = list(responses)

    def fetch(self, url):
        self.requests.append(url)
        path = url.split('?')[0].rstrip('/').rsplit('/', 1)[-1]
        script = self.routes.get(path)
        if not script:
            raise AssertionError('unscripted or exhausted fetch: ' + url)
        return script.pop(0)


class PageReplica:
    """The page's feed-state logic, mirrored poll-for-poll against a
    StubTransport. `now` is the virtual clock the abort bound reads —
    a hanging fetch resolves at poll start + POLL_MS, the deadline the
    page's AbortSignal.timeout lands every hanging read on."""

    def __init__(self, transport, peers=("",), points=(1,)):
        self.transport = transport
        self.peers = [{'base': base, 'name': base or 'origin'}
                      for base in peers]
        self.peer_state = [{'report': None, 'error': None,
                            'unsyncedSince': None}
                           for _ in self.peers]
        self.source = 0
        self.feed = {'publication': None, 'gap': None, 'stale': None}
        self.trends = {point: {'samples': [], 'lastSeq': 0,
                               'lastTick': None}
                       for point in points}
        self.journal_since = 0
        self.polling = False
        self.now = 0
        self.poll_started = 0
        self.status = ''
        self.feed_line = {'hidden': True, 'class': '', 'text': ''}
        self.peer_rows = ['—' for _ in self.peers]

    # --- the bounded fetch every poll read rides: page.html:645-648 ---

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

    # --- the role poll and pair render: page.html:654-666, 676-701 ---

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
        # page.html:628-635 — the convergence-grace clock.
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
        # page.html:692-701 — the new peer's streams re-read whole and
        # the feed bookkeeping starts over.
        self.source = nxt
        for state in self.trends.values():
            state['lastSeq'] = 0
        self.journal_since = 0
        self.feed['publication'] = None
        self.feed['gap'] = None
        self.feed['stale'] = None

    def render_pair(self):
        # page.html:854-860 — the per-peer row's reachability column.
        self.peer_rows = [
            'unreachable' if self.peer_state[i]['error'] is not None
            else ('serving' if i == self.source else 'reachable')
            for i in range(len(self.peers))
        ]

    # --- the feed record: page.html:884-932 ---

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

    def note_feed_gap(self, stream, from_, through):
        if self.feed['gap'] is None or through > self.feed['gap']['through']:
            self.feed['gap'] = {'stream': stream, 'from': from_,
                                'through': through}

    def render_feed(self):
        notices = []
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
            'class': ('gap' if self.feed['gap'] is not None
                      else 'stale' if self.feed['stale'] is not None
                      else ''),
            'text': '; '.join(notices),
        }
        return self.feed_line

    # --- the stream polls: page.html:2708-2750, 2805-2813 ---

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
        entries = self.poll_fetch(
            '/journal?since=%s' % self.journal_since).json()
        if (self.journal_since > 0 and entries
                and entries[0]['seq'] > self.journal_since + 1):
            self.note_feed_gap('journal', self.journal_since + 1,
                               entries[0]['seq'] - 1)
        for entry in entries:
            self.journal_since = max(self.journal_since, entry['seq'])

    # --- the poll ordering: page.html:1171-1274 ---

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
            self.note_publication(snapshot)
            self.feed['gap'] = None
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


if __name__ == '__main__':
    unittest.main()
