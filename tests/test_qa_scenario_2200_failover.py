"""The 2200_failover leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_failover, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'FailoverTests.test_fresh_pair_switches',
    'FailoverTests.test_switched_pair_recycles_the_active',
})


class FailoverFeed:
    """A stubbed pair for the failover leg in either role layout.
    `switched` False models a fresh rig — ctrl-a active, ctrl-b
    tracking — while True models the pair after the parameter-tune
    case already ran the a->b switch: ctrl-b is the settled active,
    and the leg must demote it and let it reconverge before its
    promote-back succeeds."""

    def __init__(self, switched=False):
        self.tick = 0
        self.role = {'a': 'standby' if switched else 'active',
                     'b': 'active' if switched else 'standby'}
        self.reconverge_left = 0

    def _refuse(self, url):
        error = urllib.error.HTTPError(url, 409, 'conflict', {}, None)
        error.close()
        raise error

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route = path.partition('?')[0]
        self.tick += 1
        peer = 'b' if host == 'ctrl-b:2' else 'a'
        if (method, route) == ('GET', '/role'):
            body = {'role': self.role[peer], 'tick': self.tick}
            if self.role[peer] == 'standby' and peer == 'b' \
                    and not self.reconverge_left:
                body['sync'] = {'tracking': {'aligned': self.tick}}
            return 200, body
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': self.tick, 'points': []}
        if (method, route) == ('POST', '/demote'):
            if self.role[peer] != 'active':
                self._refuse(url)
            self.role[peer] = 'standby'
            # The demoted peer needs a scan boundary behind it before
            # its reconverged state lets a promote through.
            self.reconverge_left = 1
            return 200, {'role': 'demoting', 'tick': self.tick}
        if (method, route) == ('POST', '/promote'):
            if peer != 'b' or self.role['b'] != 'standby' \
                    or self.reconverge_left:
                if self.reconverge_left:
                    self.reconverge_left -= 1
                self._refuse(url)
            self.role['b'] = 'active'
            return 200, {'role': 'promoting', 'tick': self.tick}
        raise AssertionError('unexpected request %s %s' % (method, url))


class FailoverTests(unittest.TestCase):
    """scenario_failover demotes whichever peer reports settled active:
    ctrl-a on a fresh rig, or ctrl-b when the parameter-tune case
    already ran the a->b switch — the converged ctrl-b takes the
    promote either way."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, switched=False):
        self.feed = FailoverFeed(switched)
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001):
            return scenarios.scenario_failover(ctx)

    def test_fresh_pair_switches(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.role['b'], 'active')
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_switched_pair_recycles_the_active(self):
        record = self.run_scenario(switched=True)
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.role['b'], 'active')
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
