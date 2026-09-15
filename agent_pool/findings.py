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

Contract: the wire report is qa_lane.report's versioned schema — the single
source of truth. validate_report delegates to it, then adapt_report derives
the lane's internal finding records from the report's evidence channels:

    failed scenario              -> defect finding (managed issue fields)
    capability_limitations[]     -> capability finding (planner candidate)
    infrastructure_failures[]    -> infrastructure finding (operational record)
    blocked/inconclusive case    -> infrastructure finding (rig-side cause)

Report schema v2 carries the fix-verification channel: a dedicated
verification run on the Lenovo lane replays the finding's original case on a
revision proven to contain the merged fix (git merge-base --is-ancestor in
the lane's bare mirror) and records the verdict, the ancestry check, and the
evidence. apply_verification certifies only a report whose entry matches the
finding key, the original case identity, and the tested revision, carries
real evidence, and survives the supervisor's own containment lookup — a
generic passing run or an unrelated case can never mark a fix verified.
GitHub lookup failures (network, auth, ambiguous merge) leave the
verification pending and are retried on the next poll. The finding fields
the wire schema does not carry (module, severity, confidence) are derived
conservatively by the adapter.
"""
import hashlib
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from qa_lane import report as qa_report

from . import planning

SCHEMA_VERSION = qa_report.SCHEMA_VERSION
KEY = re.compile(r'^[a-z0-9][a-z0-9-]{0,79}$')
SHA40 = re.compile(r'^[0-9a-f]{40}$')
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

FINDING_FIELDS = {'key', 'kind', 'module', 'severity', 'confidence', 'title',
                  'summary', 'reproduction', 'expected', 'evidence',
                  'test_requirements', 'product_cause'}
REQUIRED_FINDING = {'key', 'kind', 'module', 'severity', 'confidence',
                    'title', 'summary', 'evidence'}
VERIFICATION_FIELDS = {'finding_key', 'fix_sha', 'case', 'tested_sha',
                       'fix_ancestry', 'outcome', 'evidence', 'detail'}

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


VERDICT_OUTCOMES = ('passed', 'failed')  # runs whose assessment completed


def validate_verification(entry):
    """One fix-verification result (finding -> fix SHA -> case -> outcome).

    Wire reports carry them in the schema-v2 'verifications' channel;
    the internal seam (tests, supervisor-built reports) validates the
    same shape.
    """
    if not isinstance(entry, dict) \
            or not {'finding_key', 'outcome'} <= set(entry) <= VERIFICATION_FIELDS:
        raise ValueError('Invalid verification fields')
    if not isinstance(entry['finding_key'], str) \
            or not KEY.match(entry['finding_key']):
        raise ValueError('Invalid verification finding_key')
    if entry['outcome'] not in ('passed', 'failed', 'inconclusive'):
        raise ValueError('Invalid verification outcome')
    if entry.get('fix_sha') is not None and not SHA40.match(entry['fix_sha']):
        raise ValueError('verification fix_sha must be 40-hex')
    if entry.get('tested_sha') is not None \
            and not SHA40.match(entry['tested_sha']):
        raise ValueError('verification tested_sha must be 40-hex')
    if entry.get('case') is not None:
        _text(entry['case'], 'verification.case', 4000)
    ancestry = entry.get('fix_ancestry')
    if ancestry is not None:
        if not isinstance(ancestry, dict) \
                or not set(ancestry) <= {'checked', 'contained', 'method',
                                         'detail'}:
            raise ValueError('Invalid verification fix_ancestry')
        if 'checked' in ancestry and type(ancestry['checked']) is not bool:
            raise ValueError('fix_ancestry.checked must be boolean')
        if 'contained' in ancestry \
                and ancestry['contained'] is not None and type(ancestry['contained']) is not bool:
            raise ValueError('fix_ancestry.contained must be boolean')
        if ancestry.get('method') is not None:
            _text(ancestry['method'], 'fix_ancestry.method', 200)
        if ancestry.get('detail') is not None:
            _text(ancestry['detail'], 'fix_ancestry.detail', 2000)
    if entry.get('evidence') is not None:
        if not isinstance(entry['evidence'], list) \
                or len(entry['evidence']) > 20:
            raise ValueError('Invalid verification evidence')
        for ev in entry['evidence']:
            _text(ev if isinstance(ev, str) else ev.get('detail'),
                  'verification.evidence', 2000)
    if entry.get('detail') is not None:
        _text(entry['detail'], 'verification.detail', 4000)
    return _redact_tree(entry)


def _scenario_finding(scenario, data):
    """A failed case is a reproduced product defect; a blocked or
    inconclusive one is rig-side evidence, recorded as infrastructure."""
    key, outcome = scenario['key'], scenario['outcome']
    source = 'run %s scenario %s' % (data['run_id'], key)
    evidence = [{'detail': e.get('detail') or e['ref'],
                 'source': '%s %s:%s' % (source, e['kind'], e['ref'])}
                for e in scenario.get('evidence') or []]
    evidence += [{'detail': o, 'source': source}
                 for o in scenario.get('observations') or []]
    if not evidence:
        evidence = [{'detail': 'no evidence captured; see the run report',
                     'source': source}]
    if outcome != 'failed':
        return {'key': key, 'kind': 'infrastructure',
                'module': 'qa-lane/' + key,
                'severity': 'low', 'confidence': 'medium',
                'title': scenario['title'],
                'summary': ('%s: %s' % (outcome, scenario.get('detail')
                                        or scenario['expected']))[:4000],
                'evidence': evidence}
    return {'key': key, 'kind': 'defect',
            'module': 'qa-lane/' + key,
            'severity': 'medium', 'confidence': 'high',
            'title': scenario['title'],
            'summary': scenario.get('detail') or scenario['expected'],
            'reproduction': 'Automated scenario %s of QA run %s on the '
                            'Lenovo simulated rig.' % (key, data['run_id']),
            'expected': scenario['expected'],
            'evidence': evidence}


def _keyed_finding(item, data, kind):
    """capability_limitations / infrastructure_failures share one shape."""
    key = item['key']
    detail = item['detail']
    if kind == 'infrastructure' and item.get('phase'):
        detail = '%s (phase: %s)' % (detail, item['phase'])
    return {'key': key, 'kind': kind, 'module': 'qa-lane/' + key,
            'severity': 'medium'
            if kind == 'capability' and item.get('blocking') else 'low',
            'confidence': 'high',
            'title': key, 'summary': detail[:4000],
            'evidence': [{'detail': detail[:2000],
                          'source': 'run ' + data['run_id']}]}


def derive_findings(data):
    """Map a validated report's evidence channels onto finding records.

    Keys are the scenario/limitation/failure keys themselves — stable across
    runs, so re-observation dedups by finding key. First record wins on a
    cross-channel key collision inside one report.
    """
    derived = []
    for item in data['capability_limitations']:
        derived.append(_keyed_finding(item, data, 'capability'))
    for item in data['infrastructure_failures']:
        derived.append(_keyed_finding(item, data, 'infrastructure'))
    for scenario in data['scenarios']:
        if scenario['outcome'] != 'passed':
            derived.append(_scenario_finding(scenario, data))
    findings, seen = [], set()
    for item in derived:
        if item['key'] in seen:
            continue
        seen.add(item['key'])
        findings.append(validate_finding(item))
    return findings


def adapt_report(data):
    """Adapt a validated qa_lane report to the lane's internal report shape.

    sha <- completed_sha or attempted_sha; status <- 'completed' when the run
    produced a verdict (passed/failed) so routing gates on completed
    assessments only, otherwise the real outcome; rig <- the sanitized host
    name; findings are derived from the evidence channels.
    """
    report = _redact_tree(dict(data))
    report['sha'] = data['completed_sha'] or data['attempted_sha']
    report['status'] = ('completed' if data['outcome'] in VERDICT_OUTCOMES
                        else data['outcome'])
    report['rig'] = (data.get('host') or {}).get('name') or 'qa-rig'
    report['model'] = 'qa-lane'
    report['ended_at'] = data['finished_at']
    if 'notes' in report:
        report['notes'] = redact(report['notes'])
    report['findings'] = derive_findings(data)
    report['verifications'] = [
        validate_verification(dict(v))
        for v in data.get('verifications') or []]
    return report


def validate_report(text):
    """Parse and validate a QA run report; raise ValueError on any violation.

    qa_lane.report owns the wire contract; the validated document is then
    adapted into the internal shape the routing pipeline consumes.
    """
    return adapt_report(qa_report.validate_report(text))


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
    state.record_qa_report(report['run_id'], report['sha'], report.get('outcome') or report['status'],
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
    if cfg['enabled'] and cfg['mode'] == 'route':
        for entry in report['verifications']:
            summary['verifications'][entry['finding_key']] = \
                apply_verification(state, github, cfg, entry, report, log)
    summary['created'] = created
    state.record_qa_report(report['run_id'], report['sha'], report.get('outcome') or report['status'],
                           len(report['findings']), json.dumps(summary)[:2000])
    return summary


RETRY_KEY = 'qa:verification_retries'


def _stash_verification(state, entry, report):
    """Park a verification the GitHub lookup could not settle; the next
    poll retries it instead of losing it with the consumed report."""
    stash = dict(state.get(RETRY_KEY, {}))
    stash[entry['finding_key']] = {'entry': entry,
                                   'run_id': report.get('run_id'),
                                   'sha': report.get('sha')}
    state.set(RETRY_KEY, stash)


def _unstash_verification(state, key):
    stash = dict(state.get(RETRY_KEY, {}))
    if key in stash:
        del stash[key]
        state.set(RETRY_KEY, stash)


def apply_verification(state, github, cfg, entry, report, log):
    """Gate a verification result before it moves the findings chain.

    A fix is verified only when the report entry names the awaiting
    finding, the original case identity, and the revision actually
    tested (this report's assessed SHA); carries the runner's ancestry
    proof and real evidence; and the supervisor's own containment lookup
    agrees the tested revision contains the merged fix. Lookup failures
    and unproven ancestry never advance the chain: the finding stays
    fix-merged and transient failures retry on the next poll.
    """
    finding = state.qa_finding(entry['finding_key'])
    if not finding:
        return 'unknown-finding'
    if finding['status'] != 'fix-merged':
        return 'not-awaiting-verification'
    if not finding.get('fix_sha'):
        # Never advance a chain on an unknown fix SHA.
        return 'fix-sha-unknown'
    if entry.get('fix_sha') != finding['fix_sha']:
        return 'sha-mismatch'
    if entry.get('case') != finding['key']:
        # An unrelated passing case cannot certify this finding.
        return 'case-mismatch'
    if not entry.get('tested_sha') or entry['tested_sha'] != report.get('sha'):
        return 'untested-revision'
    if not (entry.get('fix_ancestry') or {}).get('contained'):
        return 'ancestry-unproven'
    if entry['outcome'] == 'inconclusive':
        return 'inconclusive'
    if not entry.get('evidence'):
        return 'missing-evidence'
    try:
        contained = github is not None and github.includes_main(
            entry['tested_sha'], finding['fix_sha'])
    except Exception as exc:
        _stash_verification(state, entry, report)
        log('QA finding %s verification lookup failed; pending retry: %s'
            % (finding['key'], str(exc)[:200]))
        return 'lookup-failed'
    if not contained:
        return 'not-contained'
    if entry['outcome'] == 'passed':
        state.set_qa_finding(finding['key'], status='verified')
        log('QA finding %s verified against fix %s on %s'
            % (finding['key'], finding['fix_sha'][:12],
               entry['tested_sha'][:12]))
        return 'verified'
    state.set_qa_finding(finding['key'], status='failed')
    reason = 'Verification of fix %s failed. Evidence: %s' % (
        (finding.get('fix_sha') or '')[:12],
        '; '.join(e if isinstance(e, str) else e.get('detail', '')
                  for e in (entry.get('evidence') or []))[:1000] or 'see QA report')
    _redispatch(state, github, cfg, finding, report, log, reason)
    return 'failed'


def retry_verifications(state, github, cfg, log):
    """Re-apply verifications a transient GitHub failure parked."""
    for key, item in list(state.get(RETRY_KEY, {}).items()):
        result = apply_verification(state, github, cfg, item['entry'],
                                    {'run_id': item.get('run_id'),
                                     'sha': item.get('sha')}, log)
        if result != 'lookup-failed':
            _unstash_verification(state, key)
            log('QA finding %s verification retry settled: %s'
                % (key, result))


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
    """fix-merged transition: the tracked issue's job finished and merged.

    A failed or ambiguous merge-SHA lookup leaves the finding issue-open
    — pending — so the next poll retries; a chain never advances on an
    unknown fix SHA.
    """
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
                continue  # transient: retried on the next poll
            if not sha:
                log('QA finding %s: PR %s has no merge commit yet; pending'
                    % (finding['key'], job['pr']))
                continue  # ambiguous merge: retried on the next poll
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
        if cfg['mode'] == 'route':
            retry_verifications(state, github, cfg, log)
        write_verification_queue(state, cfg, log)
    if cfg['dashboard']:
        publish_dashboard(state, cfg, log)


def verification_queue(state):
    """The Lenovo-facing pending-verification queue (qa-verifications/1).

    One item per fix-merged finding whose merged fix SHA is known: the
    finding key, the original case identity, the fix SHA, and the stored
    reproduction/expected pair — everything the lane needs to replay the
    original reproduction on a revision proven to contain the fix.
    """
    items = []
    for row in state.pending_verifications():
        if not row.get('fix_sha'):
            continue  # an unknown fix can never be verified
        payload = json.loads(row['payload'])
        items.append({'finding_key': row['key'], 'case': row['key'],
                      'fix_sha': row['fix_sha'], 'issue': row['issue'],
                      'reproduction': payload.get('reproduction'),
                      'expected': payload.get('expected')})
    return {'schema': 'qa-verifications/1', 'generated_at': int(time.time()),
            'items': items}


def write_verification_queue(state, cfg, log):
    """Drop qa/verifications.json beside the report inbox; content-hashed
    so rewrites (and the relay's push) stay idempotent. The WSL relay
    picks the file up and delivers it to the Lenovo lane."""
    if not cfg.get('report_dir'):
        return False
    doc = verification_queue(state)
    stable = json.dumps({**doc, 'generated_at': 0}, sort_keys=True)
    digest = hashlib.sha256(stable.encode()).hexdigest()
    if state.get('qa:queue_hash') == digest:
        return False
    path = Path(cfg['report_dir']).parent / 'verifications.json'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(doc, indent=1) + '\n')
        tmp.replace(path)
    except OSError as exc:
        log('QA verification queue write failed: ' + str(exc)[:300])
        return False
    state.set('qa:queue_hash', digest)
    return True


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
