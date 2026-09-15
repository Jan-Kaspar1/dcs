"""WSL-side evidence relay: sanitize run reports and publish to the Pi.

The Pi channel is a forced-command SSH receiver that stores one JSON
document per connection at /srv/metrics/site/qa/<doc>.json. This module
owns the sanitization contract — host paths, usernames, and LAN addresses
never leave — and re-validates the report after redaction so only
schema-valid documents are published.

  python3 -m qa_lane.relay publish <report.json>   push run + latest docs
  python3 -m qa_lane.relay sanitize <report.json>  print the sanitized doc
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
        'published_at': datetime.now(timezone.utc).isoformat(
            timespec='seconds'),
    }


def push(doc_name, payload):
    """Publish one JSON document through the Pi forced-command receiver."""
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
    if len(args) != 2 or args[0] not in ('publish', 'sanitize'):
        print(__doc__)
        return 2
    if args[0] == 'sanitize':
        print(json.dumps(sanitize(Path(args[1]).read_text()), indent=1))
    else:
        print('published run-' + publish(args[1]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
