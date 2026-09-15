"""WSL-side evidence relay: sanitize run reports and publish to the Pi.

The Pi channel is a forced-command SSH receiver that stores one JSON
document per connection at /srv/metrics/site/qa/<doc>.json. This module
owns the sanitization contract — host paths, usernames, and LAN addresses
never leave — and re-validates the report after redaction so only
schema-valid documents are published.

  python3 -m qa_lane.relay publish <report.json>   push run + latest docs
  python3 -m qa_lane.relay sanitize <report.json>  print the sanitized doc
  python3 -m qa_lane.relay index <reports-dir>     print the runs index doc
  python3 -m qa_lane.relay activity <status.json>  print the activity doc
  python3 -m qa_lane.relay push <doc-name> <file>  push one JSON document
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import report as qa_report

PI_TARGET = 'jan-kaspar@192.168.178.81'
PI_KEY = str(Path.home() / '.ssh' / 'dcs_qa_report_ed25519')
PI_KNOWN_HOSTS = str(Path.home() / '.ssh' / 'known_hosts_pi')

DOC_NAME = re.compile(r'^[a-z0-9][a-z0-9-]{0,79}$')

REDACTIONS = (
    (re.compile(r'/(srv|home|etc|var|root|tmp|mnt)[^\s"\'`,)\]]*'), '<path>'),
    (re.compile(r'\b192\.168\.\d{1,3}\.\d{1,3}\b'), '<lan-ip>'),
    (re.compile(r'jan-kaspar'), '<user>'),
)


def _redact(value):
    if isinstance(value, str):
        for pattern, repl in REDACTIONS:
            value = pattern.sub(repl, value)
        return value
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    return value


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def sanitize(text):
    """Validate a raw report, then return a credential/path-free copy."""
    data = qa_report.validate_report(text)
    doc = _redact(data)
    if 'host' in doc:
        doc['host'] = {'name': 'lenovo', 'os': doc['host'].get('os', '')}
    # Re-validate: redaction must never produce an invalid document.
    return qa_report.validate_report(json.dumps(doc))


def summary(doc):
    """The compact qa/latest.json document derived from a run report."""
    return {
        'schema': 'qa-summary',
        'schema_version': 1,
        'run_id': doc['run_id'],
        'attempted_sha': doc['attempted_sha'],
        'completed_sha': doc['completed_sha'],
        'outcome': doc['outcome'],
        'attempt': doc.get('attempt', 1),
        'started_at': doc['started_at'],
        'finished_at': doc['finished_at'],
        'scenarios': {s['key']: s['outcome'] for s in doc['scenarios']},
        'capability_limitations': [c['key']
                                   for c in doc['capability_limitations']],
        'infrastructure_failures': [f['key']
                                    for f in doc['infrastructure_failures']],
        'published_at': _utcnow(),
    }


def _index_entry(doc):
    """One runs.json row derived from a validated run report."""
    counts = {}
    for scenario in doc['scenarios']:
        outcome = scenario['outcome']
        counts[outcome] = counts.get(outcome, 0) + 1
    total = len(doc['scenarios'])
    outcome = doc['outcome']
    if outcome == 'passed':
        text = 'all %d scenarios passed' % total
    elif outcome == 'failed':
        text = '%d of %d scenarios failed' % (counts.get('failed', 0), total)
    elif outcome == 'interrupted':
        text = 'runner interrupted; see infrastructure_failures'
    elif outcome == 'blocked':
        text = 'run blocked before the assessment started'
    else:
        text = 'assessment inconclusive'
    return {'run_id': doc['run_id'], 'doc': 'run-' + doc['run_id'],
            'status': outcome, 'mode': 'simulation',
            'attempted_sha': doc['attempted_sha'],
            'assessed_sha': doc['completed_sha'],
            'finished_at': doc['finished_at'], 'summary': text}


def index(reports_dir):
    """Rebuild the qa/runs.json index from a directory of raw reports.

    Invalid or unreadable files are skipped — one corrupt report must not
    blank the index. Entries sort newest first by finished_at.
    """
    entries = []
    for path in sorted(Path(reports_dir).glob('*.json')):
        try:
            doc = qa_report.validate_report(path.read_text())
        except (ValueError, OSError):
            continue
        entries.append(_index_entry(_redact(doc)))
    entries.sort(key=lambda e: e['finished_at'], reverse=True)
    return {'schema': 'dcs-qa-index/1', 'updated_at': _utcnow(),
            'runs': entries}


def activity(status):
    """The qa/activity.json document: what the Lenovo lane is doing now.

    `status` is the parsed output of `python3 -m qa_lane status` on the
    Lenovo. Older lane pins report running as a bare run-id list; newer
    ones include attempted_sha and the start timestamp used for elapsed.
    """
    running = []
    for entry in status.get('running') or []:
        if isinstance(entry, dict):
            row = {'run_id': entry['run_id'],
                   'attempted_sha': entry.get('attempted_sha')}
            if entry.get('started'):
                row['started_at'] = datetime.fromtimestamp(
                    entry['started'], timezone.utc).isoformat(
                        timespec='seconds')
        else:
            row = {'run_id': entry}
        running.append(row)
    return _redact({'schema': 'qa-activity/1', 'generated_at': _utcnow(),
                    'running': running,
                    'queued': len(status.get('queued') or []),
                    'last_attempted_sha': status.get('last_attempted_sha')})


def push(doc_name, payload):
    """Publish one JSON document through the Pi forced-command receiver."""
    if not DOC_NAME.match(doc_name):
        raise ValueError('invalid doc name: ' + doc_name)
    body = json.dumps(payload).encode()
    if len(body) > 1024 * 1024:
        raise ValueError('document exceeds the 1 MiB receiver bound')
    subprocess.run(
        ['ssh', '-i', PI_KEY, '-o', 'BatchMode=yes',
         '-o', 'UserKnownHostsFile=' + PI_KNOWN_HOSTS,
         PI_TARGET, doc_name],
        input=body, check=True)


def publish(report_path):
    doc = sanitize(Path(report_path).read_text())
    push('run-' + doc['run_id'], doc)
    push('latest', summary(doc))
    return doc['run_id']


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    if args[0] == 'sanitize' and len(args) == 2:
        print(json.dumps(sanitize(Path(args[1]).read_text()), indent=1))
        return 0
    if args[0] == 'index' and len(args) == 2:
        print(json.dumps(index(Path(args[1])), indent=1))
        return 0
    if args[0] == 'activity' and len(args) == 2:
        print(json.dumps(activity(json.loads(Path(args[1]).read_text())),
                         indent=1))
        return 0
    if args[0] == 'push' and len(args) == 3:
        push(args[1], json.loads(Path(args[2]).read_text()))
        return 0
    if args[0] == 'publish' and len(args) == 2:
        print('published run-' + publish(args[1]))
        return 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
