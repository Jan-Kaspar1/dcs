"""QA findings lane: validate run reports, route findings, chain fix verification.

The WSL supervisor is the sole GitHub publisher: the Lenovo QA runner and the
exploratory agent never receive GitHub credentials. The runner drops one
versioned JSON report per assessed revision into the configured inbox; each
poll cycle this lane validates new reports, reconciles findings against the
existing backlog by stable finding key, and routes them per
docs/lenovo-hardware-qa-plan.md:

    reproduced product defect            -> validated managed issue
    missing capability                   -> planner candidate (review-lane path)
    rig/build/credential/agent failure   -> operational record only

Severity is recorded separately from confidence, and QA issues never claim
P0 (that priority stays reserved for human escalation). The concurrency group
is the affected module, not a serializing 'hw-bugs' group.

The chain finding -> issue -> merged fix SHA -> verification case -> result is
persisted in state: merged does not mean verified. A failed fix produces a
linked follow-up issue bounded by max_fix_cycles, never a bare reopen, because
the dispatcher skips issues it has already recorded.

Contract seam: the qa_lane/ report schema lands with Task 1. validate_report
accepts the vendored copy of the contract documented in
docs/lenovo-hardware-qa-plan.md ('Findings and roadmap integration'); reconcile
field names against qa_lane/ when that schema merges.
"""
import hashlib
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import planning

SCHEMA_VERSION = 1
KEY = re.compile(r'^[a-z0-9][a-z0-9-]{0,79}$')
SHA40 = re.compile(r'^[0-9a-f]{40}$')
RUN_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
GROUP = re.compile(r'^[a-z0-9][a-z0-9_-]{0,79}$')
DOC_NAME = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')
ISSUE_PREFIX = 'qa-'
MARKER = re.compile(r'<!--\s*dcs-agent-key:(qa-[a-z0-9][a-z0-9-]{0,98})\s*-->')
MAX_FIELD = 12000
MAX_DOC = 900 * 1024  # stay below the 1 MiB report-channel cap

SEVERITIES = ('low', 'medium', 'high', 'critical')
CONFIDENCES = ('low', 'medium', 'high')
KINDS = ('defect', 'capability', 'infrastructure')
# QA findings never publish P0: the lowest label the lane emits is P1, so a
# blanket import of hardware noise cannot outrank every human-set priority.
PRIORITY = {'critical': 1, 'high': 1, 'medium': 2, 'low': 3}
# Findings in flight that count against max_open. 'held' is the overflow queue
# itself and must not count, or held findings would permanently block their own
# promotion. Everything settled (verified, unresolved, infra, deferred,
# rejected, accepted) is out of the bound.
OPEN_STATUSES = ('recorded', 'issue-open', 'redispatched',
                 'fix-merged', 'failed', 'candidate')
# Statuses surfaced as open on the dashboard.
DASHBOARD_OPEN = OPEN_STATUSES + ('held',)

TOP_LEVEL = {'schema_version', 'run_id', 'sha', 'image_digest', 'model', 'rig',
             'started_at', 'ended_at', 'status', 'capabilities', 'limitations',
             'findings', 'verifications', 'notes'}
REQUIRED_TOP = TOP_LEVEL - {'image_digest', 'capabilities', 'limitations',
                            'verifications', 'notes'}
FINDING_FIELDS = {'key', 'kind', 'module', 'severity', 'confidence', 'title',
                  'summary', 'reproduction', 'expected', 'evidence',
                  'test_requirements', 'product_cause'}
REQUIRED_FINDING = {'key', 'kind', 'module', 'severity', 'confidence',
                    'title', 'summary', 'evidence'}
VERIFICATION_FIELDS = {'finding_key', 'fix_sha', 'case', 'outcome', 'evidence'}

DEFAULT_PUBLISH = {'document': 'findings',
                   'identity': '~/.ssh/dcs_qa_report_ed25519',
                   'host': 'jan-kaspar@192.168.178.81',
                   'known_hosts': '~/.ssh/known_hosts_pi'}
DEFAULTS = {'enabled': False, 'mode': 'record', 'dashboard': False,
            'report_dir': None, 'max_open': 20, 'max_issues_per_report': 5,
            'max_fix_cycles': 2, 'publish': dict(DEFAULT_PUBLISH)}

REDACTIONS = [
    re.compile(r'(?i)(password|passwd|token|secret|api[_-]?key|authorization)'
               r'([\s"\']*[:=][\s"\']*)[^\s"\',}]+'),
    re.compile(r'gh[opsu]_[A-Za-z0-9]{20,}'),
    re.compile(r'(?i)bearer\s+[A-Za-z0-9._~+/=-]{10,}'),
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
]


def settings(config):
    """Validate and default the `qa` section of the pool configuration."""
    cfg = dict(DEFAULTS)
    cfg['publish'] = dict(DEFAULT_PUBLISH)
    section = config.get('qa') or {}
    cfg.update({k: v for k, v in section.items() if k != 'publish'})
    publish = dict(DEFAULT_PUBLISH)
    publish.update(section.get('publish') or {})
    cfg['publish'] = publish
    if type(cfg['enabled']) is not bool or type(cfg['dashboard']) is not bool:
        raise ValueError('qa.enabled and qa.dashboard must be boolean')
    if cfg['mode'] not in ('record', 'route'):
        raise ValueError('qa.mode must be record or route')
    for key in ('max_open', 'max_issues_per_report', 'max_fix_cycles'):
        if type(cfg[key]) is not int or cfg[key] < 1:
            raise ValueError('qa.' + key + ' must be a positive integer')
    if cfg['report_dir'] is not None and not isinstance(cfg['report_dir'], str):
        raise ValueError('qa.report_dir must be a path string or null')
    if cfg['report_dir'] is None and config.get('state_root'):
        cfg['report_dir'] = str(Path(config['state_root']) / 'qa' / 'reports')
    for key in ('identity', 'host', 'known_hosts'):
        if not isinstance(publish[key], str) or not publish[key]:
            raise ValueError('qa.publish.' + key + ' must be a nonempty string')
    if not DOC_NAME.match(publish['document']):
        raise ValueError('qa.publish.document must be a lowercase doc name')
    return cfg


def _text(value, field, limit=MAX_FIELD):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('Missing or oversized text: ' + field)
    return value


def _iso(value, field):
    if not isinstance(value, str):
        raise ValueError(field + ' must be an ISO-8601 string')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(field + ' must be an ISO-8601 string')


def redact(text):
    """Strip credential-shaped material before persistence or publication."""
    text = REDACTIONS[0].sub(lambda m: m.group(1) + m.group(2) + '[redacted]', text)
    for pattern in REDACTIONS[1:]:
        text = pattern.sub('[redacted]', text)
    return text


def _redact_tree(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_tree(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_tree(v) for k, v in value.items()}
    return value


def validate_finding(item):
    if not isinstance(item, dict) or not REQUIRED_FINDING <= set(item) <= FINDING_FIELDS:
        raise ValueError('Invalid finding fields')
    if not KEY.match(item['key']):
        raise ValueError('Invalid finding key')
    if item['kind'] not in KINDS:
        raise ValueError('Finding kind must be defect, capability, or infrastructure')
    _text(item['module'], 'module', 500)
    if item['severity'] not in SEVERITIES:
        raise ValueError('Invalid severity')
    if item['confidence'] not in CONFIDENCES:
        raise ValueError('Invalid confidence')
    _text(item['title'], 'title', 200)
    _text(item['summary'], 'summary', 4000)
    if item['kind'] == 'defect':
        _text(item.get('reproduction'), 'reproduction', 8000)
        _text(item.get('expected'), 'expected', 4000)
    elif item.get('reproduction') is not None:
        _text(item['reproduction'], 'reproduction', 8000)
    if item.get('expected') is not None:
        _text(item['expected'], 'expected', 4000)
    if item.get('test_requirements') is not None:
        _text(item['test_requirements'], 'test_requirements', 4000)
    if 'product_cause' in item and type(item['product_cause']) is not bool:
        raise ValueError('product_cause must be boolean')
    evidence = item['evidence']
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 20:
        raise ValueError('Finding requires 1-20 evidence entries')
    normalized = []
    for entry in evidence:
        if isinstance(entry, str):
            entry = {'detail': entry}
        if not isinstance(entry, dict) or not set(entry) <= {'detail', 'source'}:
            raise ValueError('Evidence entries need detail and optional source')
        _text(entry['detail'], 'evidence.detail', 2000)
        if entry.get('source') is not None:
            _text(entry['source'], 'evidence.source', 500)
        normalized.append(entry)
    item['evidence'] = normalized
    return _redact_tree(item)


def validate_report(text):
    """Parse and validate a QA run report; raise ValueError on any violation."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError('Report is not JSON: ' + str(exc))
    if not isinstance(data, dict) or not REQUIRED_TOP <= set(data) <= TOP_LEVEL:
        raise ValueError('Invalid report top-level fields')
    if data['schema_version'] != SCHEMA_VERSION:
        raise ValueError('Unsupported report schema version')
    if not isinstance(data['run_id'], str) or not RUN_ID.match(data['run_id']):
        raise ValueError('Invalid run_id')
    if not isinstance(data['sha'], str) or not SHA40.match(data['sha']):
        raise ValueError('sha must be the exact 40-hex tested revision')
    if data.get('image_digest') is not None:
        _text(data['image_digest'], 'image_digest', 200)
    _text(data['model'], 'model', 100)
    _text(data['rig'], 'rig', 200)
    started = _iso(data['started_at'], 'started_at')
    ended = _iso(data['ended_at'], 'ended_at')
    if ended < started:
        raise ValueError('ended_at precedes started_at')
    if data['status'] not in ('completed', 'inconclusive', 'failed'):
        raise ValueError('Invalid report status')
    for field in ('capabilities', 'limitations'):
        value = data.get(field) or []
        if not isinstance(value, list) or len(value) > 40:
            raise ValueError('Invalid ' + field)
        for entry in value:
            _text(entry, field, 500)
        data[field] = value
    if 'notes' in data:
        data['notes'] = _text(data['notes'], 'notes', 4000)
    findings = data['findings']
    if not isinstance(findings, list) or len(findings) > 50:
        raise ValueError('Invalid findings list')
    seen = set()
    data['findings'] = []
    for item in findings:
        item = validate_finding(item)
        if item['key'] in seen:
            raise ValueError('Duplicate finding key in one report')
        seen.add(item['key'])
        data['findings'].append(item)
    verifications = data.get('verifications') or []
    if not isinstance(verifications, list) or len(verifications) > 50:
        raise ValueError('Invalid verifications list')
    data['verifications'] = []
    for entry in verifications:
        if not isinstance(entry, dict) or not {'finding_key', 'outcome'} <= set(entry) <= VERIFICATION_FIELDS:
            raise ValueError('Invalid verification fields')
        if not isinstance(entry['finding_key'], str) or not KEY.match(entry['finding_key']):
            raise ValueError('Invalid verification finding_key')
        if entry['outcome'] not in ('passed', 'failed', 'inconclusive'):
            raise ValueError('Invalid verification outcome')
        if entry.get('fix_sha') is not None and not SHA40.match(entry['fix_sha']):
            raise ValueError('verification fix_sha must be 40-hex')
        if entry.get('case') is not None:
            _text(entry['case'], 'verification.case', 4000)
        if entry.get('evidence') is not None:
            if not isinstance(entry['evidence'], list) or len(entry['evidence']) > 20:
                raise ValueError('Invalid verification evidence')
            for ev in entry['evidence']:
                _text(ev if isinstance(ev, str) else ev.get('detail'),
                      'verification.evidence', 2000)
        data['verifications'].append(_redact_tree(entry))
    if 'notes' in data:
        data['notes'] = redact(data['notes'])
    return data


def issue_key(finding_key):
    return ISSUE_PREFIX + finding_key


def concurrency_group(module):
    """The affected module is the serialization group, not a shared bug lane."""
    tail = module.strip().rstrip('/').split('/')[-1].lower()
    return tail if GROUP.match(tail) else 'qa'


def priority(finding):
    return PRIORITY[finding['severity']]


def issue_body(finding, report, dependencies=(), preamble=None, key=None):
    meta = {'key': key or issue_key(finding['key']),
            'group': concurrency_group(finding['module']),
            'dependencies': list(dependencies), 'priority': priority(finding)}
    evidence = '\n'.join('- ' + e['detail'] for e in finding['evidence'])
    sections = [
        planning.PREFIX + json.dumps(meta, separators=(',', ':')) + ' -->',
        '## Scope\n\nQA finding `{key}` (defect; severity {sev}; confidence {conf}), '
        'reported by run `{run}` against revision `{sha}` on rig `{rig}`. '
        'Module: `{module}`.\n\n{summary}'.format(
            key=finding['key'], sev=finding['severity'], conf=finding['confidence'],
            run=report['run_id'], sha=report['sha'], rig=report['rig'],
            module=finding['module'], summary=finding['summary']),
        '## Reproduction\n\n' + (finding.get('reproduction')
                                 or 'No automated reproduction; see the evidence and summary. '
                                 'Infrastructure findings only reach an issue when the '
                                 'evidence identifies a product cause.'),
        '## Expected behavior\n\n' + (finding.get('expected') or 'See reproduction.'),
        '## Evidence\n\n' + evidence,
        '## Acceptance criteria\n\nThe reproduction above no longer produces the '
        'defect. Workers reproduce through simulation or captured data; physical '
        'rig access stays in the QA lane, which re-verifies the merged fix '
        'against this original reproduction.',
        '## Required tests\n\n' + (finding.get('test_requirements')
                                   or 'A regression test exercising the reproduction above.'),
        '## Milestone\n\nqa-findings',
    ]
    if preamble:
        sections.insert(1, preamble)
    return '\n\n'.join(sections)


def candidate_payload(finding, report):
    return {'key': finding['key'], 'title': finding['title'],
            'outcome': 'capability', 'source': 'qa',
            'module': finding['module'], 'severity': finding['severity'],
            'confidence': finding['confidence'], 'summary': finding['summary'],
            'evidence': finding['evidence'],
            'run_id': report['run_id'], 'sha': report['sha'],
            'rig': report['rig']}


def marker_map(issues):
    """issue-key -> issue for every QA-managed issue in the inventory."""
    found = {}
    for issue in issues:
        for match in MARKER.finditer(issue.get('body') or ''):
            found.setdefault(match.group(1), issue)
    return found


def _publish_defect(github, finding, report, dependencies=(), suffix='', preamble=None):
    """Create the managed issue once; returns the issue number."""
    key = issue_key(finding['key']) + suffix
    body = issue_body(finding, report, dependencies=dependencies,
                      preamble=preamble, key=key)
    labels = ['agent:ready', 'priority:P' + str(priority(finding))]
    title = ('[QA] ' + finding['title'])[:200]
    return github.create_issue(title, body, labels, key=key)


def _route(state, github, cfg, finding, report, issues, log, can_create=True):
    """Route one new finding; returns (status, extra fields, created_issue)."""
    known = marker_map(issues)
    key = issue_key(finding['key'])
    if key in known:
        # Reconcile before creating anything: the issue already exists.
        issue = known[key]
        if issue.get('state') == 'CLOSED':
            # A closed mapped issue plus a fresh reproduction is a regression,
            # not a duplicate: the dispatcher never re-dispatches recorded jobs.
            log('QA finding %s regressed against closed #%s'
                % (finding['key'], issue['number']))
            return 'failed', {'issue': issue['number'], 'cycles': 1}, False
        return 'issue-open', {'issue': issue['number'], 'cycles': 1}, False
    if finding['kind'] == 'infrastructure' and not finding.get('product_cause'):
        return 'infra', {}, False
    if finding['kind'] == 'capability':
        state.record_candidates(report['run_id'], [candidate_payload(finding, report)])
        return 'candidate', {}, False
    # Reproduced product defect (or an infrastructure finding whose evidence
    # identifies a product cause): bounded issue publication.
    if not can_create or state.qa_count(OPEN_STATUSES) >= cfg['max_open']:
        return 'held', {}, False
    try:
        number = _publish_defect(github, finding, report)
    except Exception as exc:
        log('QA finding %s issue publication failed: %s' % (finding['key'], exc))
        return 'recorded', {}, False
    log('QA finding %s routed to issue #%s (group %s, P%s)'
        % (finding['key'], number, concurrency_group(finding['module']),
           priority(finding)))
    return 'issue-open', {'issue': number, 'cycles': 1}, True


def _report_stub(finding):
    payload = json.loads(finding['payload'])
    return {'run_id': payload['_run_id'], 'sha': payload['_sha'],
            'rig': payload['_rig']}


def _redispatch(state, github, cfg, finding, report, log, reason):
    """Create a linked follow-up for a failed fix; bounded by max_fix_cycles.

    Reopening the original issue is insufficient: the dispatcher skips issues
    already recorded in the jobs table, so a new managed issue is required.
    """
    if finding['issue'] is None:
        state.set_qa_finding(finding['key'], status='unresolved')
        return None
    if finding['cycles'] >= cfg['max_fix_cycles']:
        state.set_qa_finding(finding['key'], status='unresolved')
        log('QA finding %s exhausted %s fix cycles; left unresolved'
            % (finding['key'], finding['cycles']))
        return None
    suffix = '-fix' + str(finding['cycles'] + 1)
    preamble = ('Follow-up to #%s - fix attempt %s%s failed verification.\n\n%s'
                % (finding['issue'], finding['cycles'],
                   ' (`%s`)' % finding['fix_sha'][:12] if finding.get('fix_sha') else '',
                   reason))
    try:
        number = _publish_defect(github, json.loads(finding['payload']),
                                 report or _report_stub(finding),
                                 dependencies=[finding['issue']],
                                 suffix=suffix, preamble=preamble)
    except Exception as exc:
        log('QA finding %s follow-up publication failed: %s' % (finding['key'], exc))
        return None
    state.set_qa_finding(finding['key'], status='redispatched', issue=number,
                         cycles=finding['cycles'] + 1, fix_sha=None)
    log('QA finding %s redispatched as follow-up issue #%s' % (finding['key'], number))
    return number


def ingest_report(state, github, cfg, report, issues, log):
    """Validate, deduplicate, and route one report. Returns a summary dict.

    Assumes validate_report already ran. The report row is recorded before
    routing so a mid-route failure leaves findings 'recorded' for the next
    sweep instead of re-ingesting the file.
    """
    if state.qa_report(report['run_id']):
        return {'run_id': report['run_id'], 'result': 'duplicate'}
    routing = cfg['enabled'] and cfg['mode'] == 'route' and report['status'] == 'completed'
    summary = {'run_id': report['run_id'], 'routed': routing,
               'findings': {}, 'verifications': {}}
    state.record_qa_report(report['run_id'], report['sha'], report['status'],
                           len(report['findings']), 'ingesting')
    created = 0
    for finding in report['findings']:
        existing = state.qa_finding(finding['key'])
        if existing:
            # Repeat evidence updates the record; it never spawns a second issue.
            state.upsert_qa_finding(finding, report, existing['status'])
            if routing and existing['status'] in ('recorded', 'held'):
                # A completed run re-observed an unrouted finding; route it now.
                status, fields, made = _route(state, github, cfg, finding, report,
                                              issues, log,
                                              can_create=created < cfg['max_issues_per_report'])
                created += 1 if made else 0
                if fields:
                    state.set_qa_finding(finding['key'], **fields)
                if status != existing['status']:
                    state.set_qa_finding(finding['key'], status=status)
                summary['findings'][finding['key']] = status
            elif existing['status'] == 'verified' and finding['kind'] == 'defect':
                state.set_qa_finding(finding['key'], status='failed')
                summary['findings'][finding['key']] = 'regressed'
            else:
                summary['findings'][finding['key']] = 'known:' + existing['status']
            continue
        status, fields = 'recorded', {}
        if routing:
            status, fields, made = _route(state, github, cfg, finding, report,
                                          issues, log,
                                          can_create=created < cfg['max_issues_per_report'])
            created += 1 if made else 0
        state.upsert_qa_finding(finding, report, status)
        if fields:
            state.set_qa_finding(finding['key'], **fields)
        summary['findings'][finding['key']] = status
    if routing:
        for entry in report['verifications']:
            summary['verifications'][entry['finding_key']] = \
                apply_verification(state, github, cfg, entry, report, log)
    summary['created'] = created
    state.record_qa_report(report['run_id'], report['sha'], report['status'],
                           len(report['findings']), json.dumps(summary)[:2000])
    return summary


def apply_verification(state, github, cfg, entry, report, log):
    finding = state.qa_finding(entry['finding_key'])
    if not finding:
        return 'unknown-finding'
    if finding['status'] != 'fix-merged':
        return 'not-awaiting-verification'
    if entry.get('fix_sha') and finding.get('fix_sha') \
            and entry['fix_sha'] != finding['fix_sha']:
        return 'sha-mismatch'
    if entry['outcome'] == 'inconclusive':
        return 'inconclusive'
    if entry['outcome'] == 'passed':
        state.set_qa_finding(finding['key'], status='verified')
        log('QA finding %s verified against fix %s'
            % (finding['key'], (finding.get('fix_sha') or '')[:12]))
        return 'verified'
    state.set_qa_finding(finding['key'], status='failed')
    reason = 'Verification of fix %s failed. Evidence: %s' % (
        (finding.get('fix_sha') or '')[:12],
        '; '.join(e if isinstance(e, str) else e.get('detail', '')
                  for e in (entry.get('evidence') or []))[:1000] or 'see QA report')
    _redispatch(state, github, cfg, finding, report, log, reason)
    return 'failed'


def ingest_inbox(state, github, cfg, issues, log):
    """Validate and ingest every report file in the inbox; returns summaries."""
    inbox = Path(cfg['report_dir']) if cfg.get('report_dir') else None
    if inbox is None or not inbox.is_dir():
        return []
    results = []
    for path in sorted(inbox.glob('*.json')):
        try:
            report = validate_report(path.read_text(errors='replace'))
        except (ValueError, OSError) as exc:
            _quarantine(path, inbox.parent / 'rejected', str(exc), log)
            results.append({'file': path.name, 'result': 'rejected',
                            'error': str(exc)[:500]})
            continue
        summary = ingest_report(state, github, cfg, report, issues, log)
        _quarantine(path, inbox.parent / 'processed', None, log)
        summary['file'] = path.name
        results.append(summary)
    return results


def _quarantine(path, dest_dir, error, log):
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / path.name
        if target.exists():
            target = dest_dir / (path.stem + '-' + str(int(time.time())) + path.suffix)
        path.replace(target)
        if error:
            target.with_suffix(target.suffix + '.error.txt').write_text(error)
    except OSError as exc:
        log('Could not move %s into %s: %s' % (path, dest_dir, exc))


def route_pending(state, github, cfg, issues, log, budget=None):
    """Sweep findings that still owe routing: failed, held, and recorded.

    Bounded per pass by max_issues_per_report so one bad stretch cannot flood
    the backlog; 'failed' findings get a linked follow-up via _redispatch.
    'recorded' findings only sweep when their report was completed — findings
    from inconclusive runs stay evidence-only until a completed run re-observes
    them.
    """
    budget = cfg['max_issues_per_report'] if budget is None else budget
    for finding in state.qa_findings(('failed',)):
        if budget <= 0:
            return
        if _redispatch(state, github, cfg, finding, None, log,
                       'A previous fix attempt failed verification; see the '
                       'linked issue and QA evidence.') is not None:
            budget -= 1
    known = marker_map(issues)
    for finding in state.qa_findings(('held', 'recorded')):
        if budget <= 0 or state.qa_count(OPEN_STATUSES) >= cfg['max_open']:
            return
        payload = json.loads(finding['payload'])
        if finding['status'] == 'recorded' and payload.get('_report_status') != 'completed':
            continue
        if finding['kind'] == 'infrastructure' and not payload.get('product_cause'):
            state.set_qa_finding(finding['key'], status='infra')
            continue
        if finding['kind'] == 'capability':
            state.record_candidates(finding['last_run'],
                                    [candidate_payload(payload, _report_stub(finding))])
            state.set_qa_finding(finding['key'], status='candidate')
            continue
        key = issue_key(finding['key'])
        if key in known:
            issue = known[key]
            state.set_qa_finding(finding['key'],
                                 status='issue-open' if issue.get('state') != 'CLOSED' else 'failed',
                                 issue=issue['number'], cycles=max(1, finding['cycles']))
            continue
        try:
            number = _publish_defect(github, payload, _report_stub(finding))
        except Exception as exc:
            log('QA finding %s issue publication failed: %s' % (finding['key'], exc))
            continue
        state.set_qa_finding(finding['key'], status='issue-open', issue=number,
                             cycles=max(1, finding['cycles']))
        budget -= 1
        log('QA finding %s routed to issue #%s' % (finding['key'], number))


def sync_candidates(state, log):
    """Mirror planner dispositions onto capability findings."""
    for finding in state.qa_findings(('candidate',)):
        cand = state.candidate(finding['key'])
        if not cand or cand['disposition'] == 'pending':
            continue
        fields = {'status': cand['disposition']}
        if cand['issue'] is not None:
            fields['issue'] = cand['issue']
        state.set_qa_finding(finding['key'], **fields)
        log('QA capability %s %s by planner%s'
            % (finding['key'], cand['disposition'],
               ' (issue #%s)' % cand['issue'] if cand['issue'] else ''))


def reconcile_merged(state, github, cfg, log):
    """fix-merged transition: the tracked issue's job finished and merged."""
    for finding in state.qa_findings(('issue-open', 'redispatched')):
        job = state.job(finding['issue']) if finding['issue'] else None
        if not job or job['status'] != 'done':
            continue
        sha = None
        if job.get('pr'):
            try:
                sha = github.pr(job['pr']).get('merge_commit_sha')
            except Exception as exc:
                log('QA finding %s: could not read merge SHA for PR %s: %s'
                    % (finding['key'], job['pr'], exc))
        state.set_qa_finding(finding['key'], status='fix-merged', fix_sha=sha)
        log('QA finding %s fix merged at %s; verification queued'
            % (finding['key'], (sha or '')[:12]))


def poll(state, github, cfg, issues, log):
    """One findings-lane cycle, called from the supervisor loop."""
    if cfg['enabled']:
        summaries = ingest_inbox(state, github, cfg, issues, log)
        if cfg['mode'] == 'route':
            sync_candidates(state, log)
            # The per-report issue cap applies to the whole pass, so a report
            # that already created its share cannot trigger an immediate sweep.
            spent = sum(s.get('created', 0) for s in summaries)
            route_pending(state, github, cfg, issues, log,
                          budget=max(0, cfg['max_issues_per_report'] - spent))
        reconcile_merged(state, github, cfg, log)
    if cfg['dashboard']:
        publish_dashboard(state, cfg, log)


def document(state):
    """The qa/findings.json dashboard payload; pending verifications first."""
    rows = state.qa_findings()
    findings = [{'key': r['key'], 'kind': r['kind'], 'module': r['module'],
                 'severity': r['severity'], 'confidence': r['confidence'],
                 'status': r['status'], 'issue': r['issue'],
                 'fix_sha': r['fix_sha'], 'cycles': r['cycles'],
                 'occurrences': r['occurrences'], 'title': r['title'],
                 'updated': int(r['updated'])}
                for r in sorted(rows, key=lambda r: r['updated'], reverse=True)]
    pending = []
    for row in rows:
        if row['status'] != 'fix-merged':
            continue
        payload = json.loads(row['payload'])
        pending.append({'key': row['key'], 'issue': row['issue'],
                        'fix_sha': row['fix_sha'], 'module': row['module'],
                        'reproduction': payload.get('reproduction'),
                        'expected': payload.get('expected')})
    dispositions = []
    for cand in state.candidates():
        payload = json.loads(cand['payload'])
        if payload.get('source') != 'qa':
            continue
        dispositions.append({'key': cand['key'], 'disposition': cand['disposition'],
                             'reason': cand['reason'], 'revisit': cand['revisit'],
                             'issue': cand['issue']})
    doc = {'schema_version': 1, 'generated_at': int(time.time()),
           'pending_verifications': pending, 'findings': findings,
           'dispositions': dispositions,
           'open': sum(1 for r in rows if r['status'] in DASHBOARD_OPEN)}
    # Stay under the receiver cap: drop settled findings oldest-first.
    settled = ('verified', 'infra', 'rejected', 'unresolved')
    while len(json.dumps(doc)) > MAX_DOC \
            and any(f['status'] in settled for f in doc['findings']):
        for index in range(len(doc['findings']) - 1, -1, -1):
            if doc['findings'][index]['status'] in settled:
                del doc['findings'][index]
                break
    return doc


def publish_dashboard(state, cfg, log, runner=None):
    """Publish qa/<document>.json over the report channel; tolerate outages."""
    doc = document(state)
    stable = json.dumps({**doc, 'generated_at': 0}, sort_keys=True)
    digest = hashlib.sha256(stable.encode()).hexdigest()
    if state.get('qa:published_hash') == digest:
        return False
    pub = cfg['publish']
    command = ['ssh', '-i', str(Path(pub['identity']).expanduser()),
               '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
               '-o', 'UserKnownHostsFile=' + str(Path(pub['known_hosts']).expanduser()),
               pub['host'], pub['document']]
    try:
        result = (runner or subprocess.run)(command, input=json.dumps(doc).encode(),
                                            capture_output=True, timeout=30)
    except Exception as exc:
        state.set('qa:publish_error', str(exc)[:500])
        log('QA findings publication failed: ' + str(exc)[:300])
        return False
    if getattr(result, 'returncode', 1) != 0:
        error = (getattr(result, 'stderr', b'') or b'').decode(errors='replace')[:500]
        state.set('qa:publish_error', error or 'publication command failed')
        log('QA findings publication failed: ' + (error or 'nonzero exit'))
        return False
    state.set('qa:published_hash', digest)
    state.set('qa:published_at', time.time())
    state.set('qa:publish_error', None)
    log('QA findings published as qa/%s.json (%s findings, %s pending verifications)'
        % (pub['document'], len(doc['findings']), len(doc['pending_verifications'])))
    return True
